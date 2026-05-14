import copy
import logging
import random
import re
import html
from functools import cached_property
from typing import Iterable

import discord
import gamelogs
import parse_discord
from discord.ext import commands
from gamelogs import messages
from lxml.html import HtmlElement

import config
from .bot import *
from .logs import Gamelogs, gist_of
from .views import ViewContainer, ContainerView


log = logging.getLogger(__name__)

FREE_SKINS = ["John", "Macy", "Deodat", "Mary", "Giles", "Jack", "Brokk", "Artemys", "Francisco", "Avery", "Jackie", "Davey", "Catherine", "Martha", "Samuel", "Liric", "Blyte"]
PAID_SKINS = ["Bridget", "Lupin", "Vladimir", "Nikki", "Petra", "Thomas", "Rosemary", "Gerald", "Robert", "Shinrin", "Famine", "War", "Pestilence", "Betty", "Widow", "White Witch", "Archmage", "Husky", "Anubis", "Dusty", "Lockwood", "Shadow Wolf", "Sabrina", "Grave Digger", "Nevermore", "Headless Horseman", "Iron Chef", "Firebug", "Kande", "Odin", "Krampus", "Jekyll/Hyde", "Clef", "Piper", "Archibald", "Lauf", "Giles Quarry", "Gorgon", "Radu", "Ivy", "Sun Wukong", "Duchess", "McBrains", "Joao", "Sister", "Tabitha", "Midknight", "Poisoner", "Summer", "Helsing", "Glinda", "Blueflame", "Hermes", "Sisyphus", "Zeus", "Spartan", "Pillarman", "Minotaur", "Cupid", "Heartless Horseman", "Drachen", "Cat Jester", "Jestilence", "Card Dealer", "Godfather", "Consigliere", "Mafioso", "Isabella", "Janitor"]
D2 = gamelogs.DayTime(2, gamelogs.Time.DAY)
N2 = gamelogs.DayTime(2, gamelogs.Time.NIGHT)


class PovAnalyzer(gamelogs.Analyzer[gamelogs.Player | None]):
    def __init__(self, players: Iterable[gamelogs.Player]) -> None:
        self.players = {p.game_name: p for p in players}
        self.last_deaths = []
        self.dcs = set()
        self.deaths_last_night = []
        self.going = True
        self.pov_just_died = False

    def flush_deaths(self) -> None:
        for death in self.last_deaths:
            self.players.pop(death, None)
        self.last_deaths.clear()

    def add_deaths(self, deaths: list[str]) -> None:
        if self.pov_just_died:
            for name in list(self.players):
                if name not in deaths:
                    self.cant_be_pov(name)
            self.pov_just_died = False
        self.last_deaths.extend(deaths)

    def cant_be_pov(self, who: str) -> None:
        self.players.pop(who, None)
        if len(self.players) == 1:
            self.going = False

    def get_message(self, message: messages.Message) -> None:
        if not self.going:
            return

        match message:
            case messages.VoteAgainst(who, against) | messages.VoteGuilty(who, against) | messages.Abstain(who, against) | messages.VoteInnocent(who, against):
                if who != "You":
                    self.cant_be_pov(who)
                else:
                    self.cant_be_pov(against)
                    # POV is still alive, so anyone dead isn't POV
                    self.flush_deaths()
                    # nobody DCed currently either
                    for dc in self.dcs:
                        self.cant_be_pov(dc)
                    self.dcs.clear()
            case messages.NightDeath(who):
                self.deaths_last_night.append(who)
            case messages.Chat(_, who):
                self.add_deaths(self.deaths_last_night)
                self.deaths_last_night.clear()
            case messages.DayDeath(who):
                self.add_deaths([who])
            case messages.Disconnect(who):
                self.dcs.add(who)
            case messages.Reconnect(who):
                # it's ok if we already popped this because DCed people can't vote, even if they didn't die
                self.dcs.discard(who)
            case messages.PovDied():
                self.flush_deaths()
                self.pov_just_died = True

    def result(self) -> gamelogs.Player | None:
        if len(self.players) == 1:
            return next(iter(self.players.values()))


class LogleAnalyzer(gamelogs.Analyzer[tuple[list[str], list[gamelogs.Player]]]):
    def __init__(self, players: dict[str, gamelogs.Player], targets: list[gamelogs.Player], renames: dict[str, str]) -> None:
        self.players = players
        self.renames = renames
        self.targets = targets
        self.messages: list[str] = []
        self.tribbed: list[gamelogs.Player] = []
        self.going = False
        self.has_chat = False
        self.trib = False

    @cached_property
    def alive_d2(self) -> list[gamelogs.Player]:
        return [p for p in self.players.values() if p.lived_to(D2)]

    def render_message(self, content: HtmlElement) -> parse_discord.Markup:
        out = []

        def add_text(text: str) -> None:
            for dst, src in self.renames.items():
                text = re.sub(fr"\b{re.escape(src)}\b", re.escape(dst), text)
            out.append(parse_discord.Text(text))

        if content.text:
            out.append(parse_discord.Text(content.text))

        for child in content.getchildren():
            out.extend(self.render_message(child).nodes)
            if child.tail:
                out.append(parse_discord.Text(child.tail))

        m = parse_discord.Markup(out)
        if content.tag == "b":
            return parse_discord.Markup([parse_discord.Bold(m)])
        else:
            return m

    def _(self, who: gamelogs.Player) -> str:
        return f"[{who.number}] {who.game_name}"

    def get_message(self, message: messages.Message) -> None:
        if message in (messages.DayStart(1), messages.DayStart(2)):
            self.going = True
        if not self.going:
            return

        match message:
            case messages.Chat(number, who, content):
                who = self.players[who]
                if who in self.tribbed:
                    return
                target = who in self.targets
                if target:
                    self.has_chat = True
                if target or any([re.search(rf"\b{self.renames.get(target.game_name, target.game_name)}\b|\b{target.number}\b", messages.get_text(content)) for target in self.targets]):
                    self.messages.append(f"{self._(who)}{self.render_message(content)}")
            case messages.Whispering(who, to) | messages.VoteAgainst(who, to) | messages.VoteGuilty(who, to) | messages.Abstain(who, to) | messages.VoteInnocent(who, to):
                msg = {
                    messages.Whispering: "{} is whispering to {}.",
                    messages.VoteAgainst: "{} voted against {}.",
                    messages.VoteAgainstInstead: "{} instead voted against {}.",
                    messages.VoteToExecute: "{} voted to execute {}.",
                    messages.VoteGuilty: "{} voted {} **guilty!**",
                    messages.Abstain: "{} **abstained** on {}!",
                    messages.VoteInnocent: "{} voted {} **innocent!**",
                }[type(message)]
                who = self.players[who]
                to = self.players[to]
                if who in self.targets or to in self.targets:
                    self.messages.append(msg.format(self._(who), self._(to)))
            case messages.CancelVote(who):
                who = self.players[who]
                if who in self.targets:
                    self.messages.append(f"{self._(who)} cancelled their vote.")
            case messages.Upped(who):
                who = self.players[who]
                small = "-# "*(who not in self.targets)
                self.messages.append(f"{small}{self._(who)} was voted up to trial.")
                if self.trib:
                    self.tribbed.append(who)
                    if set(self.tribbed) == set(self.targets):
                        # fix order
                        self.targets = self.tribbed
                        self.going = False
            case messages.Pardoned(who, innocent, guilty):
                who = self.players[who]
                small = "-# "*(who not in self.targets)
                self.messages.append(f"{small}The Town decided to pardon {self._(who)} by a vote of **{innocent}** to **{guilty}**.")
            case messages.PutToDeath(who, guilty, innocent):
                who = self.players[who]
                not_target = who not in self.targets
                small = "-# "*not_target
                self.messages.append(f"{small}The Town decided to put {self._(who)} to death by a vote of **{guilty}** to **{innocent}**.")
                self.going = not_target
            case messages.Prosecuted(who):
                who = self.players[who]
                self.messages.append(f"-# {self._(who)} has been judged **guilty** and will be put to death.")
            case messages.DayDeath(who):
                victim = self.players[who]
                if victim in self.targets:
                    return

                self.messages.append(f"-# {self._(victim)} died today.")
                match victim.hanged:
                    case gamelogs.Prosecution():
                        self.messages.append("-# They were Prosecuted.")
                    case gamelogs.Tribunal() | gamelogs.Vote(_, _):
                        self.messages.append("-# They were convicted and executed.")
                    case None:
                        can_be_conj = any([gamelogs.by_name("Conjurer") in (p.starting_ident.role, p.ending_ident.role) for p in self.alive_d2])
                        can_be_dep = (
                            any([(dep := p).ending_ident.role == gamelogs.by_name("Deputy") for p in self.alive_d2])
                       and (dep.ending_ident.tt
                         or any([p.starting_ident.role in (gamelogs.by_name("Enchanter"), gamelogs.by_name("Soul Collector")) for p in self.players.values()])
                        ))
                        if victim.ending_ident.role.default_faction == gamelogs.coven or can_be_dep and not can_be_conj:
                            self.messages.append("-# They were shot by a **Deputy**.")
                        elif can_be_conj and not can_be_dep:
                            self.messages.append("-# They were killed by a **Conjurer**.")
                    case u:
                        assert_never(u)

                if victim.ending_ident.role != gamelogs.by_name("Illusionist"):
                    self.messages.append(f"-# Their role was **{victim.ending_ident}**.")
            case messages.TrialsRemaining(count):
                self.messages.append(f"-# There are **{count}** possible trials remaining today.")
            case messages.MayorReveal(who):
                who = self.players[who]
                small = "-# "*(who not in self.targets)
                self.messages.append(f"{small}{self._(who)} has revealed themselves as the **Mayor**!")
            case messages.TribunalDeclaration(who):
                who = self.players[who]
                small = "-# "*(who not in self.targets)
                self.messages.append(f"{small}{self._(who)}, the **Marshal**, has declared a **Tribunal**.")
                self.trib = True
            case messages.TribunalCount(2):
                self.messages.append("-# You may execute 2 people today.")
            case messages.TribunalCount(1):
                self.messages.append("-# You may execute 1 person today.")
            case messages.DayStart(day):
                self.messages.append(f"### Day {day}")
            case messages.NightStart():
                self.going = False

    def result(self) -> tuple[list[str], list[gamelogs.Player]]:
        return self.messages if self.has_chat else [], self.targets


class ReglePanel(ViewContainer):
    display = discord.ui.Section("", accessory=discord.ui.Thumbnail(f"{config.base_url}/static/who_wins.png"))
    sep = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)

    def __init__(self, bot: Lookout, game: gamelogs.GameResult) -> None:
        super().__init__()
        self.bot = bot
        self.game = game
        self.draw_players("Who won?")

    def draw_players(self, header: str, *, obscure: bool = False) -> None:
        players = sorted(self.game.players, key=lambda p: (
            (i := p.starting_ident).faction == gamelogs.coven,
            (gamelogs.town, gamelogs.coven, gamelogs.apocalypse).index(i.role.default_faction),
            gamelogs.bucket_of(i.role) == "Coven Deception",
            gamelogs.bucket_of(i.role),
            i.role.name,
            p.account_name,
        ))
        lines = []
        for player in players:
            if player.starting_ident.tt:
                emoji = config.tt_emoji
            else:
                emoji = config.bucket_emoji[gamelogs.bucket_of(player.starting_ident.role)]
            lines.append(f"{emoji} {('\u200b'*obscure).join(player.account_name)} - {player.starting_ident}")
        self.display.children[0].content = f"# {header}\n{'\n'.join(lines)}"  # type: ignore

    ar = discord.ui.ActionRow()

    async def finish(self, guess: gamelogs.Faction, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        log = await self.bot.require_cog(Gamelogs).fetch_log(self.game)

        self.accent_colour = discord.Colour(0x06e00c if self.game.victor == gamelogs.town else 0xb545ff)
        thumbnail = "town_wins.png" if self.game.victor == gamelogs.town else "coven_wins.png"
        self.display.accessory.media = f"{config.base_url}/static/{thumbnail}"  # type: ignore

        correct = self.game.victor == guess
        button.style = discord.ButtonStyle.green if correct else discord.ButtonStyle.red
        header = "Correct!" if correct else "Aw..."
        self.end(f"{header}\nUploaded {log.format_upload_time()}")

        self.insert_item_before(await log.to_item(), self.sep)

        async with self.bot.acquire() as conn:
            await conn.execute("INSERT INTO RegleGames (player_id, guessed, correct, game) VALUES (?, ?, ?, ?)", (interaction.user.id, guess, self.game.victor, gist_of(self.game)))

        self.view.stop()
        await interaction.response.edit_message(**self.edit_args())

    @ar.button(label="Town", emoji=config.town_emoji, style=discord.ButtonStyle.grey)
    async def guess_town(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(gamelogs.town, interaction, button)

    @ar.button(label="Coven", emoji=config.coven_emoji, style=discord.ButtonStyle.grey)
    async def guess_coven(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(gamelogs.coven, interaction, button)

    def end(self, header: str) -> None:
        self.draw_players(header, obscure=True)
        self.guess_town.disabled = True
        self.guess_coven.disabled = True

    @needs_db
    async def destroy(self, conn: Connection) -> None:
        await conn.execute("INSERT INTO RegleGames (player_id, guessed, correct, game) VALUES (?, NULL, ?, ?)", (self.view.owner.id, self.game.victor, gist_of(self.game)))
        self.end("Timed out")


class WillePanel(ViewContainer):
    display = discord.ui.Section("", accessory=discord.ui.Thumbnail(f"{config.base_url}/static/who_wins.png"))
    sep = discord.ui.Separator()
    will = discord.ui.TextDisplay("")
    sep2 = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)

    def __init__(self, bot: Lookout, game: gamelogs.GameResult, player: gamelogs.Player, member: discord.Member, correct: int) -> None:
        super().__init__(accent_colour=discord.Colour(0xdaa36f))
        self.bot = bot
        self.game = game
        self.player = player
        self.member = member
        self.correct = correct
        self.draw("# Whose will is this?")

    def draw(self, header: str, *, obscure: bool = False) -> None:
        assert self.player.will

        will = parse_discord.Markup([])
        for chunk in re.finditer(r'(?:<b>)+(.*?)(?:</b>)+|((?:(?!<b>).)+)', html.unescape(self.player.will.replace("<br/>", "\n")), re.DOTALL):
            text = parse_discord.Text(("\u200b"*obscure).join(chunk[1] or chunk[2]))
            if chunk[1]:
                will.nodes.append(parse_discord.Bold(parse_discord.Markup([text])))
            else:
                will.nodes.append(text)

        self.display.children[0].content = header  # type: ignore
        self.will.content = str(will)

    ar = discord.ui.ActionRow()
    @ar.select(cls=discord.ui.UserSelect, placeholder="Make a guess")
    async def guess(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        guess = select.values[0]
        async with self.bot.acquire() as conn:
            r = await conn.fetchone("SELECT player, (SELECT COUNT(*) >= 50 FROM Appearances WHERE player = DiscordConnections.player) FROM DiscordConnections WHERE discord_id = ?", (guess.id,))
        if not r:
            await interaction.response.send_message("I don't know what their ToS2 account is.", ephemeral=True)
            return
        guessed, reg = r
        if not reg:
            await interaction.response.send_message("Players must have played 50 or more games to appear in Wille.", ephemeral=True)
            return

        log = await self.bot.require_cog(Gamelogs).fetch_log(self.game)

        self.display.accessory.media = self.member.display_avatar.url  # type: ignore

        correct = guessed == self.correct
        header = "Will done!" if correct else "Unlucky..."
        select.placeholder = f"You guessed {guess.global_name}"
        self.end(f"# {header}\n{self.member.mention} ({'\u200b'.join(self.player.account_name)}) — {self.player.ending_ident}\n")

        item = await log.to_item()
        if isinstance(item, discord.ui.TextDisplay):
            self.display.add_item(item)
        else:
            self.insert_item_before(item, self.will)

        async with self.bot.acquire() as conn:
            await conn.execute("INSERT INTO WilleGames (player_id, guessed, correct, game) VALUES (?, ?, ?, ?)", (interaction.user.id, guessed, self.correct, gist_of(self.game)))

        self.view.stop()
        await interaction.response.edit_message(**self.edit_args())

    def end(self, header: str) -> None:
        self.draw(header, obscure=True)
        self.guess.disabled = True

    @needs_db
    async def destroy(self, conn: Connection) -> None:
        await conn.execute("INSERT INTO WilleGames (player_id, guessed, correct, game) VALUES (?, NULL, ?, ?)", (self.view.owner.id, self.correct, gist_of(self.game)))
        self.end("# Timed out")


class LoglePrompt(discord.ui.ActionRow[ContainerView["LoglePanel"]]):
    def __init__(self, target: gamelogs.Player) -> None:
        super().__init__()
        self.target = target
        self.guess = None

    @discord.ui.button(label="Town", emoji=config.town_emoji, style=discord.ButtonStyle.grey)
    async def guess_town(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.style = discord.ButtonStyle.blurple
        self.guess_coven.style = discord.ButtonStyle.grey
        self.guess = gamelogs.town
        assert self.view
        await self.view.container.on_guess()
        await interaction.response.edit_message(**self.view.edit_args())

    @discord.ui.button(label="Coven", emoji=config.coven_emoji, style=discord.ButtonStyle.grey)
    async def guess_coven(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.style = discord.ButtonStyle.blurple
        self.guess_town.style = discord.ButtonStyle.grey
        self.guess = gamelogs.coven
        assert self.view
        await self.view.container.on_guess()
        await interaction.response.edit_message(**self.view.edit_args())


class LoglePanel(ViewContainer):
    log = discord.ui.TextDisplay("")
    sep = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)

    def __init__(self, bot: Lookout, gist: str, logs: str, targets: list[gamelogs.Player]) -> None:
        super().__init__()
        self.bot = bot
        self.gist = gist
        self.targets = targets

        self.log.content = logs

        self.questions: list[discord.ui.TextDisplay] = []
        self.prompts: list[LoglePrompt] = []
        for target in targets:
            if target.died == D2:
                if target.hanged == gamelogs.Tribunal():
                    question = discord.ui.TextDisplay(f"[{target.number}] {target.game_name} died today.\nThey were executed in a **Tribunal**.\nWhat were they?")
                else:
                    question = discord.ui.TextDisplay(f"[{target.number}] {target.game_name} died today.\nThey were convicted and executed.\nWhat were they?")
            else:
                question = discord.ui.TextDisplay(f"What was {target.number}?")
            prompt = LoglePrompt(target)
            self.questions.append(question)
            self.prompts.append(prompt)
            self.add_item(question)
            self.add_item(prompt)

    async def on_guess(self) -> None:
        guessed = [p.guess for p in self.prompts]
        if not all(guessed):
            return
        answers = [p.ending_ident.faction for p in self.targets]

        log = await self.bot.require_cog(Gamelogs).fetch_log_with_gist(self.gist)

        match answers:
            case [gamelogs.town]:
                self.accent_colour = discord.Colour(0x06e00c)
                thumbnail = "town_wins.png"
            case [gamelogs.coven]:
                self.accent_colour = discord.Colour(0xb545ff)
                thumbnail = "coven_wins.png"
            case [gamelogs.town, gamelogs.town]:
                self.accent_colour = discord.Colour(0x06e00c)
                thumbnail = "zero_two.png"
            case [gamelogs.town, gamelogs.coven]:
                self.accent_colour = discord.Colour(0x5e9386)
                thumbnail = "town_coven.png"
            case [gamelogs.coven, gamelogs.town]:
                self.accent_colour = discord.Colour(0x5e9386)
                thumbnail = "coven_town.png"
            case [gamelogs.coven, gamelogs.coven]:
                self.accent_colour = discord.Colour(0xb545ff)
                thumbnail = "two_two.png"
            case _:
                assert False

        for prompt, answer in zip(self.prompts, answers):
            prompt.guess_town.disabled = True
            prompt.guess_coven.disabled = True
            button = prompt.guess_town if prompt.guess == gamelogs.town else prompt.guess_coven
            button.style = discord.ButtonStyle.green if prompt.guess == answer else discord.ButtonStyle.red

        correct = guessed == answers
        report = ["# Good job!" if correct else "# That's too bad..."]
        for answer in self.targets:
            report.append(f"{'\u200b'.join(answer.account_name)} — {answer.short_ident}")
        
        self.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        section = discord.ui.Section("\n".join(report), accessory=discord.ui.Thumbnail(f"{config.base_url}/static/{thumbnail}"))
        self.add_item(section)
        item = await log.to_item()
        if isinstance(item, discord.ui.TextDisplay):
            section.add_item(item)
        else:
            self.add_item(item)

        async with self.bot.acquire() as conn:
            await conn.execute(
                "INSERT INTO LogleGames (player_id, guessed, correct, game, num_targets) VALUES (?, ?, ?, ?, ?)",
                (self.view.owner.id, [str(x) for x in guessed], [str(x) for x in answers], self.gist, len(self.targets)),
            )

        self.view.stop()

    @needs_db
    async def destroy(self, conn: Connection) -> None:
        await conn.execute(
            "INSERT INTO LogleGames (player_id, guessed, correct, game, num_targets) VALUES (?, NULL, ?, ?, ?)",
            (self.view.owner.id, [str(p.ending_ident.faction) for p in self.targets], self.gist, len(self.targets)),
        )
        for prompt in self.prompts:
            prompt.guess_town.disabled = True
            prompt.guess_coven.disabled = True


class Gaming(commands.Cog):
    """Little minigames you can play on Discord."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    @commands.command()
    @needs_db
    async def regle(self, conn: Connection, ctx: Context) -> None:
        """Guess which faction won a game, given only the lineup.

        Games won by Town and Coven appear equally as often.
        """
        victor = random.choice([gamelogs.town, gamelogs.coven])
        game, = await conn.fetchone("SELECT analysis FROM Games WHERE victor = ?1 LIMIT 1 OFFSET ABS(RANDOM()) % (SELECT COUNT(*) FROM Games WHERE victor = ?1)", (victor,))
        await ctx.send_container_view(ReglePanel(self.bot, game))

    @commands.command()
    @commands.guild_only()
    @needs_db
    async def wille(self, conn: Connection, ctx: Context) -> None:
        """With only a will, guess who wrote it.

        Only players with a connected Discord account on this server and at least 50 games can appear. All such players appear equally as often. 
        """
        players = await conn.fetchall("SELECT player, discord_id FROM DiscordConnections WHERE (SELECT COUNT(*) >= 50 FROM Appearances WHERE player = DiscordConnections.player)")

        assert ctx.guild
        while True:
            player_id, discord_id = random.choice(players)
            if member := ctx.guild.get_member(discord_id):
                break

        while True:
            game, account_name = await conn.fetchone(
                "SELECT analysis, account_name FROM Appearances INNER JOIN Games ON gist = game WHERE player = ?1 LIMIT 1 OFFSET ABS(RANDOM()) % (SELECT COUNT(*) FROM Appearances WHERE player = ?1)",
                (player_id,),
            )
            for player in game.players:
                if player.account_name == account_name:
                    break
            else:
                assert False
            if player.will and player.lived_to(N2):
                break

        await ctx.send_container_view(WillePanel(self.bot, game, player, member, player_id))

    @commands.command()
    @needs_db
    async def logle(self, conn: Connection, ctx: Context) -> None:
        """Guess a player's faction from their D2 logs.

        Both answers are equally likely.
        If Anon Players is not enabled, there is a 20% chance the player's name will be anonymised anyway.
        """
        factions = random.choices([gamelogs.town, gamelogs.coven], k=2)
        special = random.random() < 0.050603

        while True:
            digest, content, game, gist = await conn.fetchone("SELECT hash, clean_content, analysis, gist FROM Games INNER JOIN Gamelogs ON hash = from_log LIMIT 1 OFFSET ABS(RANDOM()) % (SELECT COUNT(*) FROM Games)")
            if any([p.game_name == "You" for p in game.players]):
                continue
            alive_n2 = game.alive_players(N2)

            if not special:
                candidates = [random.choice([p for p in alive_n2 if p.ending_ident.faction == factions[0]])]
            else:
                candidates = [p for p in game.players
                    if p.died == D2
                    if gamelogs.bucket_of(p.ending_ident.role) != "Town Power"
                    if (hr := p.hanged) == gamelogs.Tribunal() 
                    or isinstance(hr, gamelogs.Vote) and (hr.guilty + (len(alive_n2) - hr.guilty - hr.innocent) / 2) / len(alive_n2) < 0.75
                ]
                if not candidates or [p.ending_ident.faction for p in candidates] != factions[:len(candidates)]:
                    continue

            players: dict[str, gamelogs.Player] = {p.game_name: p for p in game.players}
            pov = gamelogs.parse(content, PovAnalyzer(players.values()), clean_tags=False)
            if pov:
                players["You"] = pov

            rng = random.Random(int(digest, 16))
            anonymize = rng.random() < 0.2
            renames = {}
            for player in candidates:
                if anonymize and player.game_name not in FREE_SKINS or player.game_name in PAID_SKINS:
                    while True:
                        new_name = rng.choice(FREE_SKINS)
                        if not any(new_name == p.game_name for p in players.values()):
                            break
                    renames[new_name] = player.game_name
                    player.game_name = new_name

            try:
                logs, candidates = gamelogs.parse(content, LogleAnalyzer(players, candidates, renames), clean_tags=False)
            except KeyError as e:
                if e.args[0] == "You":
                    log.warning(f"couldn't find POV for log {digest} when it was necessary :(")
                    continue
                raise

            if (logs := "\n".join(logs)) and len(logs) <= 3_500:
                break

        await ctx.send_container_view(LoglePanel(self.bot, gist, logs, candidates))


async def setup(bot: Lookout):
    await bot.add_cog(Gaming(bot))
