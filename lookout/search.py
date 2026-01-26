from __future__ import annotations

import calendar
import datetime
import io
from dataclasses import dataclass
from typing import Literal

import re2 as re
import discord
import gamelogs
from discord.ext import commands

import config
from .bot import Lookout
from .logs import gist_of
from .stats import Stats, PlayerInfo
from .utils import ContainerView


RE_OPTIONS = re.Options()
RE_OPTIONS.case_sensitive = False
RE_OPTIONS.never_capture = True

ROLES = [r for r in gamelogs.by_name.values() if r.default_faction in (gamelogs.town, gamelogs.coven)]

ROLE_ALIASES = {
    "ti": "town investigative",
    "tk": "town killing",
    "tpow": "town power",
    "tp": "town protective",
    "ts": "town support",
    "to": "town outlier",
    "ct": "common town",
    "rt": "random town",

    "cd": "coven deception",
    "ck": "coven killing",
    "cpow": "coven power",
    "cu": "coven utility",
    "co": "coven outlier",
    "cc": "common coven",
    "rc": "random coven",
    "cov": "random coven",
    "coven": "random coven",

    "coro": "coroner",
    "inv": "investigator",
    "invest": "investigator",
    "lo": "lookout",
    "psy": "psychic",
    "sher": "sheriff",

    "dep": "deputy",
    "trick": "trickster",
    "vet": "veteran",
    "vig": "vigilante",
    "vigi": "vigilante",

    "marsh": "marshal",
    "mayo": "mayor",
    "mon": "monarch",
    "pros": "prosecutor",

    "bg": "bodyguard",
    "cler": "cleric",
    "crus": "crusader",
    "orac": "oracle",

    "admi": "admirer",
    "adm": "admirer",
    "amne": "amnesiac",
    "ret": "retributionist",
    "retri": "retributionist",
    "soc": "socialite",
    "soci": "socialite",
    "tav": "tavern keeper",

    "cata": "catalyst",
    "pil": "pilgrim",

    "dw": "dreamweaver",
    "ench": "enchanter",
    "illu": "illusionist",
    "dusa": "medusa",

    "conj": "conjurer",
    "rit": "ritualist",

    "cl": "coven leader",
    "hex": "hex master",
    "hm": "hex master",

    "necro": "necromancer",
    "pois": "poisoner",
    "pm": "potion master",
    "vm": "voodoo master",
    "wild": "wildling",
    "wl": "wildling",

    "cult": "cultist",
}

STRICT_BUCKETS = {
    **{b.casefold(): set(rs) for b, rs in gamelogs.by_bucket.items()},
    "common town": {r for r in ROLES if r.default_faction == gamelogs.town and gamelogs.bucket_of[r] != "Town Power"},
    "random town": {r for r in ROLES if r.default_faction == gamelogs.town},
    "common coven": {r for r in ROLES if r.default_faction == gamelogs.coven and gamelogs.bucket_of[r] not in ("Coven Killing", "Coven Power")},
    "random coven": {r for r in ROLES if r.default_faction == gamelogs.coven},
}

BUCKETS = {
    **{r.name.lower(): {r} for r in ROLES},
    **STRICT_BUCKETS,
}

for a, b in ROLE_ALIASES.items():
    BUCKETS[a] = BUCKETS[b]

KEYWORDS = {
    "town": lambda ident: ident.faction == gamelogs.town,
    "green": lambda ident: ident.faction == gamelogs.town,
    "purple": lambda ident: ident.faction == gamelogs.coven,
    "tt": lambda ident: ident.faction == gamelogs.coven and ident.role.default_faction == gamelogs.town,
    **{n: lambda ident, rs=rs: ident.role in rs for n, rs in BUCKETS.items()},
}

@dataclass
class PlayerSpecifier:
    names: set[str] | None
    ign: str | None
    idents: set[gamelogs.Identity]

    def matches(self, player: gamelogs.Player) -> bool:
        return (self.names is None or player.account_name.casefold() in self.names) and (self.ign is None or player.game_name == self.ign) and (player.starting_ident in self.idents or player.ending_ident in self.idents)

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> PlayerSpecifier:
        names = None
        ign = None
        idents = []
        for role in ROLES:
            idents.append(gamelogs.Identity(role))
            if role.default_faction == gamelogs.town:
                idents.append(gamelogs.Identity(role, gamelogs.coven))

        words = argument.split()
        while words:
            for izer in (lambda l: (-l, None), lambda l: (None, l)):
                for kw, f in KEYWORDS.items():
                    kw_parts = kw.split()
                    i, j = izer(len(kw_parts))
                    if [w.lower() for w in words[i:j]] == kw_parts and 0 < len(n := [ident for ident in idents if f(ident)]) < len(idents):
                        idents = n
                        del words[i:j]
                        break
                else:
                    continue
                break
            else:
                for i, word in reversed(list(enumerate(words))):
                    if word.startswith("ign:"):
                        ign = " ".join([word.removeprefix("ign:"), *words[i+1:]]).strip()
                        del words[i:]
                        break
                if not words:
                    break

                name = " ".join(words)
                if name.startswith("account:"):
                    names = {name.removeprefix("account:").strip().casefold()}
                elif name:
                    player = await PlayerInfo.convert(ctx, name)
                    names = {name.casefold() for name in player.names}

                break

        return cls(names, ign, set(idents))


@dataclass
class DateRange:
    start: datetime.date
    stop: datetime.date

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> DateRange:
        if season := SEASONS.get(argument.lower()):
            return season

        m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", argument)
        if not m:
            await ctx.send(f"Unknown date '{argument}'.")
            raise commands.BadArgument()
        match [n and int(n) for n in m.groups()]:
            case [year, None, None]:
                return DateRange(datetime.date(year, 1, 1), datetime.date(year, 12, 31))
            case [year, month, None]:
                return DateRange(datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1]))
            case [year, month, day]:
                return DateRange(datetime.date(year, month, day), datetime.date(year, month, day))
            case _:
                assert False

SEASONS = {
    "s4": DateRange(datetime.date(2024, 11, 11), datetime.date(2025, 6, 26)),
    "s5": DateRange(datetime.date(2025, 6, 27), datetime.date(2030, 1, 1)),
}


class Jump(discord.ui.Modal):
    def __init__(self, container: SearchResults) -> None:
        super().__init__(title="Jump to result")
        self.container = container
        self.box.component.default = f"{container.page+1}"  # type: ignore

    box = discord.ui.Label(text="Destination", description="The number of a result to jump to.", component=discord.ui.TextInput(max_length=5))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t: str = self.box.component.value  # type: ignore
        try:
            page = int(t) - 1
        except ValueError:
            await interaction.response.send_message(f"'{t}' isn't a number.")
            return
        else:
            if not self.container.has_page(page):
                await interaction.response.send_message(f"Result number {page+1} is out of bounds.", ephemeral=True)
                return
            self.container.go_to_page(page)
        await self.container.draw()
        await interaction.response.edit_message(view=self.container.view, attachments=[self.container.file_obj])


class SearchResults(discord.ui.Container):
    display = discord.ui.Section("", accessory=discord.ui.Thumbnail(""))
    file = discord.ui.File("")
    sep = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)
    underfile = discord.ui.TextDisplay("")

    def __init__(self, bot: Lookout, results: list[gamelogs.GameResult]) -> None:
        super().__init__()
        self.bot = bot
        self.results = results
        self.page = 0
        self.next.disabled = len(results) == 1

    def has_page(self, num: int) -> bool:
        return 0 <= num < len(self.results)

    async def draw(self, *, obscure: bool = False) -> None:
        game = self.results[self.page]

        async with self.bot.db.execute("SELECT filename, clean_content, message_id FROM Gamelogs INNER JOIN Games ON hash = from_log WHERE gist = ?", (gist_of(game),)) as cur:
            filename, content, message_id = await cur.fetchone()  # type: ignore

        self.accent_colour = discord.Colour(0x06e00c if game.victor == gamelogs.town else 0xb545ff)
        match game.victor == gamelogs.town, bool(game.hunt_reached), game.outcome == gamelogs.Outcome.HEX_BOMB:
            case _, False, True:
                outcome = "Hex bomb • Coven wins"
                thumbnail = "hex_bomb.png"
            case _, True, True:
                outcome = "Hex bomb in hunt • Coven wins"
                thumbnail = "hex_bomb_hunt.png"
            case True, True, _:
                outcome = "TT died in hunt • Town wins"
                thumbnail = "town_wins_hunt.png"
            case True, False, _:
                outcome = "Coven obliterated • Town wins"
                thumbnail = "town_wins.png"
            case False, True, _:
                outcome = "TT survived hunt • Coven wins"
                thumbnail = "coven_wins_hunt.png"
            case False, False, _:
                outcome = "Town eliminated • Coven wins"
                thumbnail = "coven_wins.png"

        rollout = []
        for player in game.players:
            bold = "**"*player.won
            death = "-#"*bool(player.died)
            obsc = ('\u200b'*obscure).join
            role = f"{player.starting_ident.role} {player.ending_ident.role}" if player.starting_ident != player.ending_ident else f"{player.starting_ident.role}"
            faction = " (TT)"*(player.starting_ident.role.default_faction != player.starting_ident.faction)
            rollout.append(f"{death} - [{player.number}] {obsc(player.game_name)} ({obsc(player.account_name)}) - {bold}{role}{faction}{bold}")

        self.display.children[0].content = f"Uploaded {discord.utils.format_dt(discord.utils.snowflake_time(message_id), 'D')}\n{outcome}\n{'\n'.join(rollout)}"
        self.display.accessory.media = f"{config.base_url}/static/{thumbnail}"
        self.file.media = self.file_obj = discord.File(io.BytesIO(content.encode()), filename=filename)
        self.underfile.content = f"-# Result {self.page+1} of {len(self.results)}"

    def go_to_page(self, num: int) -> None:
        self.page = num
        self.previous.disabled = not self.has_page(num - 1)
        self.next.disabled = not self.has_page(num + 1)

    ar = discord.ui.ActionRow()

    @ar.button(label="Prev", emoji="⬅️", disabled=True)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page - 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view, attachments=[self.file_obj])

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page + 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view, attachments=[self.file_obj])

    @ar.button(label="Jump", emoji="↪️")
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(Jump(self))

    async def destroy(self) -> None:
        await self.draw(obscure=True)
        self.remove_item(self.ar)


class SearchQuery(commands.FlagConverter):
    chat: list[str] = []
    author: list[PlayerSpecifier] = commands.flag(name="from", default=[])
    has: list[PlayerSpecifier] = []
    before: DateRange | None = None
    during: DateRange | None = None
    after: DateRange | None = None
    victor: Literal["town", "coven"] | None = commands.flag(aliases=["winner", "won"], default=None)
    hunt: bool | None = None


class Search(commands.Cog):
    """Searching of games."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    @commands.command()
    async def search(self, ctx: commands.Context, *, query: SearchQuery) -> None:
        """Search for games.

        A flag-based syntax is used, similar to Discord's built-in search functionality. The following flags are supported:

        has:
        Filter games with certain players, roles, or both. For instance, "has: Danman34682", "has: tt jailor", or "has: phoin rit".
        May be used multiple times, in which case all clauses must apply.
        Prefix names with `account:` to find a literal account name without further processing, or `ign:` to search in-game names.

        chat:
        Search the content of in-game chat messages.
        May be used multiple times, in which case all clauses must appear. (But not necessarily in the same message.)

        from:
        Filter `chat:` to only find messages from a certain players or roles. Uses the same syntax as `has:`.
        When used without `chat:`, works similarly to `has:`. May be used multiple times, in which case any clause may apply.

        before:, during:, after:
        Specify a time period to search in. Used with dates both full (after: 2025-01-25) and partial (during: 2026-01).
        In place of a date, `s4` or `s5` may be written, which correspond to ranked seasons and the corresponding game updates.
        If multiple of these flags are used at once, only the overlap of all flags specified will be searched.

        victor:, winner:, won:
        Select for the winning faction (`town` or `coven`). Each of these flags has the same effect.

        hunt:
        Find games that did (`yes`) or did not (`no`) reach hunt.
        """
        stats: Stats = self.bot.get_cog("Stats")  # type: ignore

        results = []
        patterns = [text[1:-1] if len(text) >= 2 and text[0] == text[-1] == "/" else fr"\b{re.escape(text)}\b" for text in query.chat]

        # holy absymal dogshit
        approx_date = "(SELECT approx_date FROM Gamelogs WHERE hash = from_log)"
        where = []
        if query.before:
            where.append(f"{approx_date} < '{query.before.start}'")
        if query.during:
            where.append(f"{approx_date} >= '{query.during.start}' AND {approx_date} <= '{query.during.stop}'")
        if query.after:
            where.append(f"{approx_date} > '{query.after.stop}'")

        async for game in stats.games(" AND ".join(where)):
            if query.victor is not None and (query.victor == "town") != (game.victor == gamelogs.town):
                continue

            if query.hunt is not None and bool(game.hunt_reached) != query.hunt:
                continue

            if not all([any([spec.matches(player) for player in game.players]) for spec in query.has]):
                continue

            if query.author and not any([any([spec.matches(player) for player in game.players]) for spec in query.author]):
                continue

            if query.chat:
                async with self.bot.db.execute("SELECT clean_content FROM Gamelogs INNER JOIN Games ON hash = from_log WHERE gist = ?", (gist_of(game),)) as cur:
                    content, = await cur.fetchone()  # type: ignore

                valid_authors = "|".join(re.escape(player.game_name) for player in game.players if not query.author or any([spec.matches(player) for spec in query.author]))
                valid_dash_authors = valid_authors.replace(" ", "-")

                if not all([re.search(
                    fr'<span class=".*">({valid_authors})</span>\n<span>: .*{pattern}.*</span>|<span style=".*">({valid_dash_authors})</span>\n<span style=".*">: .*{pattern}.*</i></span>',
                    content, RE_OPTIONS,
                ) for pattern in patterns]):
                    continue

            results.append(game)

        if not results:
            await ctx.send("No results.")
            return
        results.reverse()
        view = ContainerView(ctx.author, SearchResults(self.bot, results))
        await view.container.draw()
        view.message = await ctx.send(view=view, file=view.container.file_obj)


async def setup(bot: Lookout):
    await bot.add_cog(Search(bot))
