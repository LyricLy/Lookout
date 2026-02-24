from __future__ import annotations

import calendar
import datetime
import re
import io
from dataclasses import dataclass
from typing import Literal

import re2
import discord
import gamelogs
from discord.ext import commands

import config
from .bot import Lookout
from .logs import Gamelogs, gist_of
from .specifiers import IdentitySpecifier, PlayerSpecifier, BUCKETS
from .stats import Stats, PlayerInfo
from .views import ViewContainer, ContainerView


RE_OPTIONS = re2.Options()
RE_OPTIONS.case_sensitive = False


@dataclass
class DateRange:
    start: datetime.date
    stop: datetime.date

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> DateRange:
        if season := SEASONS.get(argument.lower()):
            return season

        m = re2.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", argument)
        if not m:
            raise commands.BadArgument(f"Unknown date '{argument}'.")
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
        await interaction.response.edit_message(**self.container.edit_args())


class SearchResults(ViewContainer):
    display = discord.ui.Section("", accessory=discord.ui.Thumbnail(""))
    sep = discord.ui.Separator(spacing=discord.SeparatorSpacing.large)
    underfile = discord.ui.TextDisplay("")

    def __init__(self, bot: Lookout, results: list[gamelogs.GameResult]) -> None:
        super().__init__()
        self.bot = bot
        self.results = results
        self.page = 0
        self.next.disabled = len(results) == 1
        self.file = None

    def has_page(self, num: int) -> bool:
        return 0 <= num < len(self.results)

    async def draw(self, *, obscure: bool = False) -> None:
        game = self.results[self.page]

        logs: Gamelogs = self.bot.get_cog("Gamelogs")  # type: ignore
        log = await logs.fetch_log(game)

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
            faction = " (TT)"*player.starting_ident.is_wrong_faction()
            rollout.append(f"{death} - [{player.number}] {obsc(player.game_name)} ({obsc(player.account_name)}) - {bold}{role}{faction}{bold}")

        if self.file:
            self.file._update_view(None)  # the library doesn't do this...?
            self.remove_item(self.file)
        self.file = log.to_item()
        self.add_item(self.file)
        self._children.insert(self._children.index(self.sep), self._children.pop())

        self.display.children[0].content = f"Uploaded {log.format_upload_time()}\n{outcome}\n{'\n'.join(rollout)}"  # type: ignore
        self.display.accessory.media = f"{config.base_url}/static/{thumbnail}"  # type: ignore
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
        await interaction.response.edit_message(**self.edit_args())

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page + 1)
        await self.draw()
        await interaction.response.edit_message(**self.edit_args())

    @ar.button(label="Jump", emoji="↪️")
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(Jump(self))

    async def destroy(self) -> None:
        await self.draw(obscure=True)
        self.remove_item(self.ar)


class SearchQuery(commands.FlagConverter):
    chat: list[str] = []
    author: list[PlayerSpecifier] = commands.flag(name="from", default=[])
    has: list[PlayerSpecifier] = commands.flag(default=[], positional=True)
    before: DateRange | None = None
    during: DateRange | None = None
    after: DateRange | None = None
    victor: Literal["town", "coven"] | None = commands.flag(aliases=["winner", "won"], default=None)
    hunt: bool | None = None
    team: tuple[PlayerInfo, ...] = ()
    count: list[tuple[int, str]] = []

SearchQuery.__commands_flag_regex__ = re.compile(r"\b" + SearchQuery.__commands_flag_regex__.pattern, SearchQuery.__commands_flag_regex__.flags)


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
        The `hunt` and `won` keywords require that the player reached hunt or won the game, respectively.
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

        team:
        Require that all listed players appear on the same team.

        count:
        Look for games with a minimum number of a given role or alignment.
        """
        joins = []
        where = []
        p = {}

        approx_date = "(SELECT approx_date FROM Gamelogs WHERE hash = first_log)"
        if query.before:
            where.append(f"{approx_date} < :before")
            p["before"] = query.before.start
        if query.during:
            where.append(f"{approx_date} >= :start AND {approx_date} <= :stop")
            p["start"] = query.during.start
            p["stop"] = query.during.stop
        if query.after:
            where.append(f"{approx_date} > :after")
            p["after"] = query.after.stop

        if query.victor:
            where.append(f"victor = :victor")
            p["victor"] = query.victor

        if query.hunt is not None:
            where.append(f"hunt_reached = :hunt")
            p["hunt"] = query.hunt

        for spec in query.has:
            c, p2 = spec.to_sql()
            joins.append(f"INNER JOIN (SELECT DISTINCT game AS gist FROM Appearances WHERE {c}) USING (gist)")
            p.update(p2)

        if query.author:
            arms = []
            for spec in query.author:
                c, p2 = spec.to_sql()
                arms.append(f"EXISTS(SELECT 1 FROM Appearances WHERE game = gist AND {c})")
                p.update(p2)
            where.append(" OR ".join(arms))

        if query.team:
            for i, player in enumerate(query.team):
                joins.append(f"INNER JOIN (SELECT game AS gist, faction FROM Appearances WHERE player = :team_{i}) AS Team{i} USING (gist)")
                p[f"team_{i}"] = player.id
                if i:
                    where.append(f"Team{i}.faction = Team0.faction")

        for i, (count, bucket_name) in enumerate(query.count):
            if count < 0:
                raise commands.BadArgument("Count cannot be negative.")
            bucket = BUCKETS.get(bucket_name.lower())
            if not bucket:
                raise commands.BadArgument(f"Unknown bucket name '{bucket_name}'.")

            c, p2 = IdentitySpecifier(roles=list(bucket), only_starting=True).to_sql()
            where.append(f"(SELECT COUNT(*) >= :count_{i} FROM Appearances WHERE game = gist AND {c})")
            p[f"count_{i}"] = count
            p.update(p2)

        stats: Stats = self.bot.get_cog("Stats")  # type: ignore
        cur = stats.games(f"{' '.join(joins)} WHERE {' AND '.join(where) if where else '1'}", p)

        if query.chat:
            patterns = []
            for text in query.chat:
                pattern = text[1:-1] if len(text) >= 2 and text[0] == text[-1] == "/" else fr"\b{re2.escape(text)}\b"
                patterns.append(fr'<span class=".*">(?<author>.*)</span>\n<span>: .*{pattern}.*</span>|<span style=".*">(?<author_dead>.*)</span>\n<span style=".*">: .*{pattern}.*</i></span>')

            results = []
            async for game in cur:
                async with self.bot.db.execute("SELECT clean_content FROM Gamelogs INNER JOIN Games ON hash = from_log WHERE gist = ?", (gist_of(game),)) as cur:
                    content, = await cur.fetchone()  # type: ignore

                for pattern in patterns:
                    for m in re2.finditer(pattern, content, RE_OPTIONS):
                        if not query.author:
                            break

                        try:
                            if m["author"]:
                                author = next(player for player in game.players if player.game_name == m["author"])
                            else:
                                author = next(player for player in game.players if player.game_name.replace(" ", "-") == m["author_dead"])
                        except StopIteration:
                            continue

                        if any([await spec.matches(game, author) for spec in query.author]):
                            break
                    else:
                        break
                else:
                    results.append(game)
        else:
            results = [game async for game in cur]

        if not results:
            await ctx.send("No results.")
            return
        results.reverse()
        view = ContainerView(ctx.author, SearchResults(self.bot, results))
        await view.container.draw()
        view.message = await ctx.send(**view.send_args())


async def setup(bot: Lookout):
    await bot.add_cog(Search(bot))
