import asyncio
import datetime
import functools
import math
import logging
import textwrap
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Self, Sequence, Any, Protocol

import aiosqlite
import discord
import gamelogs
from discord.ext import commands
from openskill.models import PlackettLuce

import config
from .bot import Lookout
from .logs import gist_of, Timecode, Gamelogs
from .player_info import PlayerInfo
from .specifiers import PURE_BUCKETS, ROLES, IdentitySpecifier
from .views import ViewContainer, ContainerView


log = logging.getLogger(__name__)

model = PlackettLuce(limit_sigma=True)


@dataclass
class Winrate:
    s: int = 0
    n: int = 0

    def interval(self) -> tuple[float, float]:
        z = 3
        s = self.s
        n = self.n
        avg = s / n
        divisor = 1 + z*z / n
        return (avg + (z*z/(2*n))) / divisor, z/(2*n) * math.sqrt(4 * n * avg * (1-avg) + z*z) / divisor

    def centre(self) -> float:
        return self.interval()[0]

    def lower_bound(self) -> float:
        centre, radius = self.interval()
        return centre - radius

    def upper_bound(self) -> float:
        centre, radius = self.interval()
        return centre + radius

    def _ord_key(self) -> float:
        try:
            return self.lower_bound()
        except ZeroDivisionError:
            return float("-inf")

    def __str__(self) -> str:
        try:
            centre, radius = self.interval()
        except ZeroDivisionError:
            return "N/A (no games)"
        else:
            return f"{centre*100:.2f}% ± {radius*100:.2f}% ({self.s}/{self.n})"

    def __add__(self, other: Winrate) -> Winrate:
        return Winrate(self.s + other.s, self.n + other.n)

    def __sub__(self, other: Winrate) -> Winrate:
        return Winrate(self.s - other.s, self.n - other.n)

    def __lt__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() < other._ord_key()

    def __le__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() <= other._ord_key()

    def __gt__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() > other._ord_key()

    def __ge__(self, other: Winrate) -> bool:
        if not isinstance(other, Winrate):
            return NotImplemented
        return self._ord_key() >= other._ord_key()


class DisplayablePlayer(Protocol):
    async def names(self) -> list[str]: ...
    async def user(self) -> discord.User | None: ...

@dataclass
class ReglePlayerInfo:
    _user: discord.User

    async def names(self) -> list[str]:
        r = [self._user.name, str(self._user.id)]
        if self._user.global_name:
            r.append(self._user.global_name)
        return r

    async def user(self) -> discord.User | None:
        return self._user


type Criterion = IdentitySpecifier | Literal["rating", "regle"]
type Key = Winrate | float


class Jump(discord.ui.Modal):
    def __init__(self, container: TopPaginator) -> None:
        super().__init__(title="Jump to page")
        self.container = container
        self.box.component.default = f"{container.page+1}"  # type: ignore

    box = discord.ui.Label(text="Destination", description="A page number or name of a player to jump to.", component=discord.ui.TextInput(max_length=32))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t: str = self.box.component.value  # type: ignore
        try:
            page = int(t) - 1
        except ValueError:
            for idx, (player, _) in enumerate(self.container.players):
                if any([name.casefold() == t.casefold() for name in await player.names()]):
                    break
            else:
                await interaction.response.send_message(f"I don't know a player called '{t}'.", ephemeral=True)
                return
            self.container.go_to_page(idx // self.container.per_page)
        else:
            if not self.container.has_page(page):
                await interaction.response.send_message(f"Page number {page+1} is out of bounds.", ephemeral=True)
                return
            self.container.go_to_page(page)
        await self.container.draw()
        await interaction.response.edit_message(view=self.container.view)


class TopPaginator(ViewContainer):
    display = discord.ui.TextDisplay("")

    def __init__(self, stats: Stats, crit: Criterion, played: bool) -> None:
        super().__init__(accent_colour=discord.Colour(0xfce703))
        self.stats = stats
        self.crit: Criterion = crit
        self.played = isinstance(crit, IdentitySpecifier) and crit.won is not None or played
        self.per_page = 15

    async def start(self) -> None:
        self.players = sorted(await self.decorate_players(), key=lambda p: p[1], reverse=True)
        self.go_to_page(0)
        await self.draw()

    async def decorate_players(self) -> list[tuple[DisplayablePlayer, Key]]:
        if self.crit == "rating":
            return [(player, player.ordinal()) for player in await self.stats.fetch_players(self.stats.now())]

        elif self.crit == "regle":
            async with self.stats.bot.db.execute("SELECT player_id, COALESCE(SUM(guessed = correct), 0), COUNT(*) FROM RegleGames GROUP BY player_id") as cur:
                return [(ReglePlayerInfo(player), n if self.played else Winrate(s, n)) async for player_id, s, n in cur if (player := self.stats.bot.get_user(player_id))]

        c, p = self.crit.to_sql()
        hidden_clause = "NOT EXISTS(SELECT 1 FROM Hidden WHERE player = Appearances.player) AND "
        if self.played:
            if self.crit.won is None:
                hidden_clause = ""
            async with self.stats.bot.db.execute(f"SELECT player, COUNT(*) FROM Appearances WHERE {hidden_clause}{c} GROUP BY player", p) as cur:
                return [(await self.stats.fetch_player(player, self.stats.now(), with_rank=False), c) async for player, c in cur]
        else:
            async with self.stats.bot.db.execute(f"SELECT player, COALESCE(SUM(won), 0), COUNT(*) FROM Appearances WHERE {hidden_clause}{c} GROUP BY player", p) as cur:
                return [(await self.stats.fetch_player(player, self.stats.now(), with_rank=False), Winrate(s, n)) async for player, s, n in cur]

    def key_desc(self) -> str:
        if self.crit == "rating":
            return ""
        elif self.crit == "regle":
            return "Sorting by games played of Regle." if self.played else "Sorting by winrate in Regle."

        key_roles = set(self.crit.roles)
        if self.crit.faction == gamelogs.coven and all([role.default_faction == gamelogs.town for role in self.crit.roles]):
            specifier = "TT"
        elif self.crit.faction == gamelogs.town:
            specifier = "green"
        else:
            specifier = None

        for bucket, roles in PURE_BUCKETS.items():
            if roles == key_roles:
                match bucket:
                    case "random town" if specifier:
                        n = specifier
                    case "random town":
                        n = "Town"
                    case "random coven":
                        n = "Coven"
                    case _ if specifier:
                        n = f"{specifier} {bucket.title()}"
                    case _:
                        n = bucket.title()
                break
        else:
            if self.crit.roles == ROLES:
                n = "any role" if self.crit.faction != gamelogs.coven else "purple"
            else:
                return ""

        match self.crit.hunt, "won" if self.crit.won else "lost" if self.crit.won == False else "played" if self.played else None:
            case None, None:
                return f"Sorting by winrate as {n}. Confidence intervals are ordered by lower bound, not the centre."
            case None, p:
                return f"Sorting by number of games {p} as {n}."
            case True, None:
                return f"Sorting by winrate in hunt as {n}. Confidence intervals are ordered by lower bound, not the centre."
            case True, p:
                return f"Sorting by number of hunts {p} as {n}."

        assert False

    def show_key(self, key: Key) -> str:
        if self.crit == "rating":
            return f"{key:.0f}"
        elif self.played:
            return f"{key:,}"
        else:
            return f"{key}"

    def has_page(self, num: int) -> bool:
        return 0 <= num*self.per_page < len(self.players)

    async def _render_player(self, player: DisplayablePlayer, key: Key, obscure: bool) -> str:
        user = await player.user()
        names = await player.names()
        return f"{f'{user.mention}' if user else ('\u200b'*obscure).join(names[0])} - {self.show_key(key)}"

    async def draw(self, *, obscure: bool = False) -> None:
        start = self.page*self.per_page
        lb = "\n".join([f"{start+1}. {await self._render_player(player, key, obscure)}" for player, key in self.players[start:start+self.per_page]])
        self.display.content = f"# Leaderboard\n{self.key_desc()}\n{lb}\n-# Page {self.page+1} of {math.ceil(len(self.players) / self.per_page):,}"

    def go_to_page(self, num: int) -> None:
        self.page = num
        self.previous.disabled = not self.has_page(num - 1)
        self.next.disabled = not self.has_page(num + 1)

    ar = discord.ui.ActionRow()

    @ar.button(label="Prev", emoji="⬅️")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page - 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page + 1)
        await self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Jump", emoji="↪️")
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(Jump(self))

    async def destroy(self) -> None:
        await self.draw(obscure=True)
        self.remove_item(self.ar)


class Stats(commands.Cog):
    """Player statistics."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self.catchup = asyncio.Lock()

    async def cog_load(self) -> None:
        #self.update_task = asyncio.create_task(self.update_loop())
        self.catchup_task = asyncio.create_task(self.run_games())

    async def cog_unload(self) -> None:
        #self.update_task.cancel()
        self.catchup_task.cancel()

    async def update_game(self, gist: str, from_log: str) -> None:
        async with self.bot.db.execute("SELECT clean_content FROM Gamelogs WHERE hash = ?", (from_log,)) as cur:
            content, = await cur.fetchone()  # type: ignore
        try:
            game, message_count = gamelogs.parse(content, gamelogs.ResultAnalyzer() & gamelogs.MessageCountAnalyzer(), clean_tags=False)
        except gamelogs.BadLogError:
            log.exception("failed to update game from log %s", from_log)
        else:
            assert gist_of(game) == gist
            await self.bot.db.execute("UPDATE Games SET analysis = ?, message_count = ?, analysis_version = ? WHERE gist = ?", (game, message_count, gamelogs.version, gist))
            await self.bot.db.commit()
            log.info("updated game from log %s to version %d", from_log, gamelogs.version)

    async def games(self, injection: str = "", params: Sequence[object] | dict[str, Any] = ()) -> AsyncIterator[gamelogs.GameResult]:
        async with self.bot.db.execute(f"SELECT gist, analysis, analysis_version, from_log FROM Games {injection}", params) as cur:
            async for gist, game, version, from_log in cur:
                if version < gamelogs.version:
                    asyncio.create_task(self.update_game(gist, from_log))
                yield game

    async def run_game(self, game: gamelogs.GameResult):
        logs: Gamelogs = self.bot.get_cog("Gamelogs")  # type: ignore
        log = await logs.fetch_log(game)

        teams = [[], []]
        ratings = [[], []]
        for player in game.players:
            info: PlayerInfo = await self.fetch_player_by_name(player.account_name, log.first_upload, with_rank=False)  # type: ignore
            i = player.ending_ident.faction == gamelogs.coven
            teams[i].append((player, info))
            ratings[i].append(info.rating)

        # pad coven
        avg = sum([r.mu for r in ratings[1]]) / len(ratings[1])
        ratings[1].extend([model.rating(mu=avg, sigma=avg/3) for _ in range(len(ratings[0]) - len(ratings[1]))])

        new_ratings = model.rate(ratings, ranks=[1, 2] if game.victor == gamelogs.town else [2, 1])
        for team, team_ratings in zip(teams, new_ratings):
            for (player, info), new_rating in zip(team, team_ratings):
                await self.bot.db.execute("""
                    INSERT OR REPLACE INTO Appearances (player, starting_role, ending_role, faction, game, account_name, game_name, won, saw_hunt, mu_after, sigma_after, timecode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    info.id, player.starting_ident.role, player.ending_ident.role, player.ending_ident.faction, gist_of(game),
                    player.account_name, player.game_name, player.won, game.saw_hunt(player),
                    new_rating.mu, new_rating.sigma, *log.first_upload,
                ))

        await self.bot.db.execute("UPDATE Globals SET (last_update_message_id, last_update_filename_time) = (?, ?)", (log.first_upload.message_id, log.first_upload.filename_time))
        await self.bot.db.commit()

    async def run_games(self):
        try:
            async with self.catchup:
                log.info("resolving appearances")
                c = 0
                async for game in self.games("""
                    INNER JOIN Gamelogs ON hash = first_log, Globals
                    WHERE (message_id, filename_time) > (last_update_message_id, last_update_filename_time)
                    ORDER BY message_id, filename_time
                """):
                    await self.run_game(game)
                    c += 1
                log.info("resolved appearances from %d games", c)
        except Exception:
            log.exception("error running games")

    @commands.Cog.listener()
    async def on_saw_games(self):
        await self.run_games()

#     async def update_loop(self) -> None:
#         while True:
#             try:
#                 async with self.bot.db.execute("SELECT last_update FROM Globals") as cur:
#                     last_update, = await cur.fetchone()  # type: ignore
#                 next_update = config.next_update(last_update)
#                 await discord.utils.sleep_until(next_update - config.grace_period)
# 
#                 players = await self.run_games()
#                 old = await self.fetch_discord_players()
#                 await discord.utils.sleep_until(next_update)
# 
#                 await self.save_stats(players)
#                 new = await self.fetch_discord_players()
#                 changes = []
#                 for old_you, new_you in zip(old, new):
#                     if not new_you.user or old_you.winrates == new_you.winrates:
#                         continue
#                     recents = new_you.winrate_in(Part.ALL) - old_you.winrate_in(Part.ALL)
#                     changes.append(f"{new_you.user.mention} {old_you.ordinal():.0f} -> {new_you.ordinal():.0f} (W-L {recents.s}-{recents.n-recents.s})")
#                 embed = discord.Embed(
#                     title=f"{last_update.strftime('%B %-d') if last_update.year == next_update.year else last_update.strftime('%B %-d %Y')} - {next_update.strftime('%B %-d %Y')}",
#                     description="\n".join(changes),
#                     colour=discord.Colour(0xd18411),
#                 )
#                 report_channel = self.bot.get_partial_messageable(config.report_channel_id)
#                 await report_channel.send(embed=embed)
# 
#                 await self.bot.db.execute("UPDATE Globals SET last_update = ?", (datetime.datetime.now(datetime.timezone.utc),))
#             except Exception:
#                 log.exception("error in update loop")
#                 await asyncio.sleep(10)

    def prev_update(self) -> datetime.datetime:
        return config.prev_update(datetime.datetime.now(datetime.timezone.utc))

    def next_update(self) -> datetime.datetime | None:
        return config.next_update(datetime.datetime.now(datetime.timezone.utc))

    def now(self) -> Timecode:
        return Timecode.from_datetime(self.prev_update())

    async def _row_to_player_info(self, player: int, row: aiosqlite.Row | None) -> PlayerInfo:
        r: aiosqlite.Row = row or defaultdict(lambda: None)  # type: ignore
        return PlayerInfo(
            player,
            r["rank"],
            model.rating(r["mu_after"], r["sigma_after"]) if r["mu_after"] else model.rating(),
            _stats=self,
        )

    _RATINGS = """(
        WITH Ratings AS (SELECT player, mu_after, sigma_after, MAX(timecode) FROM Appearances WHERE timecode < ? GROUP BY player)
        SELECT player, mu_after, sigma_after, rank
        FROM Ratings LEFT JOIN {}
    ) AS Ratings"""

    RATINGS = _RATINGS.format(
        "(SELECT player, rank() OVER (ORDER BY mu_after - 3.0 * sigma_after DESC) as rank FROM Ratings WHERE NOT EXISTS(SELECT 1 FROM Hidden WHERE player = Ratings.player)) USING (player)"
    )
    RATINGS_WO_RANK = _RATINGS.format("(SELECT NULL as rank)")

    async def fetch_player(self, player: int, at: Timecode, *, with_rank: bool = True) -> PlayerInfo:
        async with self.bot.db.execute(f"SELECT * FROM {Stats.RATINGS if with_rank else Stats.RATINGS_WO_RANK} WHERE player = ?", (*at, player,)) as cur:
            r = await cur.fetchone()
        return await self._row_to_player_info(player, r)

    async def resolve_player_name(self, name: str) -> int | None:
        async with self.bot.db.execute("SELECT player FROM Names WHERE name = ?", (name,)) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def fetch_player_by_name(self, name: str, at: Timecode, *, with_rank: bool = True) -> PlayerInfo | None:
        player = await self.resolve_player_name(name)
        return await self.fetch_player(player, at, with_rank=with_rank) if player else None

    async def fetch_players(self, at: Timecode) -> list[PlayerInfo]:
        async with self.bot.db.execute(f"SELECT * FROM {Stats.RATINGS}", (*at,)) as cur:
            return [await self._row_to_player_info(r["player"], r) async for r in cur]

    async def fetch_discord_players(self, at: Timecode) -> list[PlayerInfo]:
        async with self.bot.db.execute(f"SELECT * FROM {Stats.RATINGS} WHERE EXISTS(SELECT 1 FROM DiscordConnections WHERE player = Ratings.player) ORDER BY player", (*at,)) as cur:
            return [await self._row_to_player_info(r["player"], r) async for r in cur]

    @commands.command()
    async def info(self, ctx: commands.Context) -> None:
        """General information and statistics on stored games."""
        async with self.bot.db.execute("""SELECT
            SUM(victor = 'town' AND NOT hunt_reached), SUM(victor = 'coven' AND NOT hunt_reached),
            SUM(victor = 'town' AND hunt_reached), SUM(victor = 'coven' AND hunt_reached)
        FROM Games""") as cur:
            town_maj, coven_maj, town_hunt, coven_hunt = await cur.fetchone()  # type: ignore
        total = town_maj + town_hunt + coven_maj + coven_hunt

        next_on = f", next on {discord.utils.format_dt(d)}" if (d := self.next_update()) else ""
        embed = discord.Embed(
            title="Lookout",
            description=textwrap.dedent(f"""
                Dutifully tracking {total:,} games,
                {town_maj + town_hunt:,} Town victories,
                {coven_maj + coven_hunt:,} Coven victories ({coven_maj:,} by majority, {coven_hunt:,} by countdown),
                {town_hunt + coven_hunt:,} hunts ({town_hunt:,} won by Town, {coven_hunt:,} won by Coven),
                and {total*15:,} fun times had.

                Last rating update on {discord.utils.format_dt(self.prev_update(), "D")}{next_on}
                Made with love by {config.me_emoji} <3
            """),
            colour=discord.Colour.dark_green(),
        )

        await ctx.send(embed=embed)

    async def winrate_in(self, player: PlayerInfo, spec: IdentitySpecifier = IdentitySpecifier()) -> Winrate:
        c, p = spec.to_sql()
        async with self.bot.db.execute(f"SELECT COALESCE(SUM(won), 0), COUNT(*) FROM Appearances WHERE player = :player AND {c}", {"player": player.id, **p}) as cur:
            s, n = await cur.fetchone()  # type: ignore
        return Winrate(s, n)

    @commands.command()
    async def player(self, ctx: commands.Context, *, player: PlayerInfo) -> None:
        """Show information about a player."""
        embed = discord.Embed()
        names = await player.names()
        user = await player.user()

        r = None
        for name in names:
            async with self.bot.db.execute("SELECT thread_id FROM Blacklists WHERE account_name = ?", (name,)) as cur:
                r = await cur.fetchone()
            if r:
                break

        name_string = ", ".join(names)
        title = f"{user.mention} ({name_string})" if user else name_string

        overall = await self.winrate_in(player)
        game_count = f"{overall.n:,} games" if overall.n != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")

        hidden = await player.hidden()
        if hidden == "user":
            embed.description = f"### {title}\nThis player has chosen to hide their profile."
        elif hidden == "cheated":
            embed.description = f"### {title}\nThis player's profile is hidden because they were found to have played illegitimately."
        else:
            rated = f"Rated {player.ordinal():.0f} (#{player.rank:,})" if player.rank else "Not rated"
            embed.description = f"### {title}\n{rated}"
            embed.add_field(name="Winrates", value=textwrap.dedent(f"""
                - Overall {overall}
                - Town {await self.winrate_in(player, IdentitySpecifier().with_faction(gamelogs.town))}
                - Purple {await self.winrate_in(player, IdentitySpecifier().with_faction(gamelogs.coven))}
                  - Coven {await self.winrate_in(player, IdentitySpecifier().where(lambda role: role.default_faction == gamelogs.coven))}
                  - TT {await self.winrate_in(player, IdentitySpecifier().where(lambda role: role.default_faction == gamelogs.town).with_faction(gamelogs.coven))}
            """))
            embed.add_field(name="Winrates in hunt", value=textwrap.dedent(f"""
                - Town {await self.winrate_in(player, IdentitySpecifier(hunt=True).with_faction(gamelogs.town))}
                - TT {await self.winrate_in(player, IdentitySpecifier(hunt=True).with_faction(gamelogs.coven))}
            """))

        if r:
            embed.add_field(name="Player blacklisted", value=f"<#{r[0]}>", inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=["lb", "leaderboard", "players"])
    async def top(self, ctx: commands.Context, played: Literal["played"] | None, *, criterion: Criterion | Literal["overall"] = "rating") -> None:
        """Rank all players by a specified criterion.

        By default, `top` displays players sorted by their rating. Specifying an alignment or role, such as `town`, will sort by winrate instead.
        Prefix with the `played` keyword to count games played instead of winrate.
        """
        if criterion == "overall" or played and criterion == "rating":
            criterion = IdentitySpecifier()
        view = ContainerView(ctx.author, TopPaginator(self, criterion, bool(played)))
        await view.container.start()
        view.message = await ctx.send(view=view)

    @commands.command(name="is")
    @commands.is_owner()
    async def _is(self, ctx: commands.Context, a: PlayerInfo, b: PlayerInfo) -> None:
        """Treat 2 players as being the same from now on in statistics."""
        if a.id == b.id:
            await ctx.send("I know.")
            return

        # rerun the period we're about to clobber
        async with self.bot.db.execute("SELECT MIN(timecode) FROM Appearances WHERE player = ?", (b.id,)) as cur:
            tc, = await cur.fetchone()  # type: ignore
        timecode = Timecode.from_str(tc)
        await self.bot.db.execute(
            "UPDATE Globals SET (last_update_message_id, last_update_filename_time) = (?, ?)",
            (timecode.message_id, timecode.filename_time - datetime.timedelta(seconds=1)),
        )

        await self.bot.db.execute("UPDATE OR IGNORE DiscordConnections SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.execute("UPDATE Names SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.execute("DELETE FROM Appearances WHERE player = ?", (b.id,))
        await self.bot.db.commit()
        await ctx.send(":+1:")

        await self.run_games()

    @commands.command()
    async def hide(self, ctx: commands.Context) -> None:
        """Hide the information the bot tracks about your gameplay."""
        async with self.bot.db.execute("INSERT OR REPLACE INTO Hidden (player) SELECT player FROM DiscordConnections WHERE discord_id = ? RETURNING player", (ctx.author.id,)) as cur:
            r = await cur.fetchone()
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command(aliases=["unhide"])
    async def show(self, ctx: commands.Context) -> None:
        """Revert the effect of `hide`."""
        async with self.bot.db.execute("SELECT player FROM DiscordConnections WHERE discord_id = ?", (ctx.author.id,)) as cur:
            r = await cur.fetchone()
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await self.bot.db.execute("DELETE FROM Hidden WHERE player = ?", (r[0],))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    @commands.is_owner()
    async def cheated(self, ctx: commands.Context, player: PlayerInfo) -> None:
        """Mark a player as having cheated."""
        await self.bot.db.execute("INSERT OR REPLACE INTO Hidden (player, why) VALUES (?, 'cheated')", (player.id,))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    @commands.is_owner()
    async def uncheated(self, ctx: commands.Context, player: PlayerInfo) -> None:
        """Revert the effect of `cheated`."""
        await self.bot.db.execute("DELETE FROM Hidden WHERE player = ?", (player.id,))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    @commands.check_any(commands.has_role("Game host"), commands.is_owner())
    async def connect(self, ctx: commands.Context, who: discord.Member, *, player: PlayerInfo) -> None:
        """Associate a player with their Discord account."""
        await self.bot.db.execute("INSERT OR REPLACE INTO DiscordConnections (discord_id, player) VALUES (?, ?)", (who.id, player.id))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    @commands.check_any(commands.has_role("Game host"), commands.is_owner())
    async def unconnected(self, ctx: commands.Context, *, guild: discord.Guild = commands.CurrentGuild) -> None:
        """List Discord members not associated with a ToS2 username."""
        members = []
        for member in guild.members:
            if member.bot:
                continue
            async with self.bot.db.execute("SELECT 1 FROM DiscordConnections WHERE discord_id = ?", (member.id,)) as cur:
                exists = await cur.fetchone()
            if not exists:
                members.append(member)
        await ctx.send("\n".join(f"- {member.mention}" for member in members))


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
