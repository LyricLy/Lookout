import random
import re
import html

import discord
import gamelogs
import parse_discord
from discord.ext import commands

import config
from .bot import *
from .logs import Gamelogs, gist_of
from .views import ViewContainer, ContainerView


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
            await conn.execute("INSERT INTO RegleGames (player_id, guessed, correct, gist) VALUES (?, ?, ?, ?)", (interaction.user.id, guess, self.game.victor, gist_of(self.game)))

        self.view.stop()  # type: ignore
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

    async def destroy(self) -> None:
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
        self.draw("Whose will is this?")

    def draw(self, header: str, *, obscure: bool = False) -> None:
        assert self.player.will

        will = parse_discord.Markup([])
        for chunk in re.finditer(r'(?:<b>)+(.*?)(?:</b>)+|((?:(?!<b>).)+)', html.unescape(self.player.will.replace("<br/>", "\n")), re.DOTALL):
            if chunk[1]:
                will.nodes.append(parse_discord.Bold(parse_discord.Markup([parse_discord.Text(chunk[1])])))
            else:
                will.nodes.append(parse_discord.Text(chunk[2]))

        self.display.children[0].content = f"# {header}"  # type: ignore
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
        self.end(f"{header}\n{self.member.mention} ({self.player.account_name}) — {self.player.ending_ident}\n")

        item = await log.to_item()
        if isinstance(item, discord.ui.TextDisplay):
            self.display.add_item(item)
        else:
            self.insert_item_before(item, self.will)

        async with self.bot.acquire() as conn:
            await conn.execute("INSERT INTO WilleGames (player_id, guessed, correct, gist) VALUES (?, ?, ?, ?)", (interaction.user.id, guessed, self.correct, gist_of(self.game)))

        self.view.stop()  # type: ignore
        await interaction.response.edit_message(**self.edit_args())

    def end(self, header: str) -> None:
        self.draw(header, obscure=True)
        self.guess.disabled = True

    async def destroy(self) -> None:
        self.end("Timed out")


class Gaming(commands.Cog):
    """Little minigames you can play on Discord."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    @commands.command()
    @needs_db
    async def regle(self, conn: Connection, ctx: Context) -> None:
        """Guess which faction won a game, given only the lineup."""
        victor = random.choice([gamelogs.town, gamelogs.coven])
        game, = await conn.fetchone("SELECT analysis FROM Games WHERE victor = ?1 LIMIT 1 OFFSET ABS(RANDOM()) % (SELECT COUNT(*) FROM Games WHERE victor = ?1)", (victor,))
        await ctx.send_container_view(ReglePanel(self.bot, game))

    @commands.command()
    @commands.guild_only()
    @needs_db
    async def wille(self, conn: Connection, ctx: Context) -> None:
        """With only a will, guess who wrote it."""
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
            if player.will and (not player.died or player.died >= gamelogs.DayTime(2, gamelogs.Time.NIGHT)):
                break

        await ctx.send_container_view(WillePanel(self.bot, game, player, member, player_id))


async def setup(bot: Lookout):
    await bot.add_cog(Gaming(bot))
