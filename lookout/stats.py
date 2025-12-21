from __future__ import annotations

import asyncio
import datetime
import enum
import math
import logging
import textwrap
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Literal, Callable, Any

import aiosqlite
import discord
import gamelogs
import msgpack
from discord.ext import commands
from openskill.models import PlackettLuce, PlackettLuceRating

import config
from .bot import Lookout
from .logs import gist_of


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


class RoleClass(enum.Enum):
    TOWN = 0
    COVEN = 1
    TT = 2
    TOWN_HUNT = 3
    TT_HUNT = 4

class Part(enum.Enum):
    TOWN = [RoleClass.TOWN, RoleClass.TOWN_HUNT]
    TOWN_HUNT = [RoleClass.TOWN_HUNT]
    COVEN = [RoleClass.COVEN]
    TT = [RoleClass.TT, RoleClass.TT_HUNT]
    TT_HUNT = [RoleClass.TT_HUNT]
    PURPLE = COVEN + TT
    ALL = TOWN + PURPLE

@dataclass
class PlayerInfo:
    id: int
    names: list[str]
    user: discord.User | None
    hidden: bool
    rank: int
    rating: PlackettLuceRating
    winrates: dict[RoleClass, Winrate]

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

    def winrate_in(self, classes: Part) -> Winrate:
        return sum([self.winrates.get(c, Winrate()) for c in classes.value], Winrate())

    def played_in(self, classes: Part) -> int:
        return sum([self.winrates.get(c, Winrate()).n for c in classes.value])

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> PlayerInfo:
        async with ctx.bot.db.execute("SELECT player FROM Names WHERE name = ?", (argument,)) as cur:
            r = await cur.fetchone()
        if not r:
            try:
                member = await commands.MemberConverter().convert(ctx, argument)
            except commands.MemberNotFound:
                await ctx.send("I don't know that player.")
                raise commands.BadArgument()
            else:
                async with ctx.bot.db.execute("SELECT player FROM DiscordConnections WHERE discord_id = ?", (member.id,)) as cur:
                    r = await cur.fetchone()
                if not r:
                    await ctx.send(f"I don't know what {member.mention}'s ToS2 account is.")
                    raise commands.BadArgument()
        return await ctx.bot.get_cog("Stats").fetch_player(r[0])

@dataclass
class PlayerStats:
    rating: PlackettLuceRating
    winrates: defaultdict[RoleClass, Winrate]


type Players = defaultdict[int, PlayerStats]
type Criterion = Part | Literal["rating", "played"]


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
            for idx, player in enumerate(self.container.players):
                if any([name.casefold() == t.casefold() for name in player.names]):
                    break
            else:
                await interaction.response.send_message(f"I don't know a player called '{t}'.", ephemeral=True)
                return
            self.container.go_to_page(idx // self.container.per_page)
        else:
            if not self.container.has_page(page):
                await interaction.response.send_message(f"Page number {page} is out of bounds.", ephemeral=True)
                return
            self.container.go_to_page(page)
        self.container.draw()
        await interaction.response.edit_message(view=self.container.view)


class TopPaginator(discord.ui.Container):
    display = discord.ui.TextDisplay("")

    def __init__(self, players: Iterable[PlayerInfo], part: Criterion) -> None:
        super().__init__(accent_colour=discord.Colour(0x6bfc03))
        self.part: Criterion = part
        self.players = sorted(players, key=self.key, reverse=True)
        self.per_page = 15
        self.page = 0
        self.draw()

    def key(self, player: PlayerInfo) -> Winrate | float:
        if self.part == "rating":
            return player.ordinal()
        elif self.part == "played":
            return player.played_in(Part.ALL)
        else:
            return player.winrate_in(self.part)

    def key_desc(self) -> str:
        match self.part:
            case Part.TOWN:
                n = "Town winrate"
            case Part.PURPLE:
                n = "purple winrate"
            case Part.COVEN:
                n = "Coven winrate"
            case Part.TT:
                n = "TT winrate"
            case Part.TOWN_HUNT:
                n = "Town winrate in hunt"
            case Part.TT_HUNT:
                n = "TT winrate in hunt (data is scarce)"
            case Part.ALL:
                n = "overall winrate"
            case "played":
                return "Sorting by number of games played."
            case _:
                return ""
        return f"Sorting by {n}. Confidence intervals are ordered by lower bound, not the centre."

    def show_key(self, player: PlayerInfo) -> str:
        k = self.key(player)
        if self.part == "rating":
            return f"{k:.0f}"
        elif self.part == "played":
            return f"{k:,}"
        else:
            return f"{k}"

    def has_page(self, num: int) -> bool:
        return 0 <= num*self.per_page < len(self.players)

    def draw(self, *, obscure: bool = False) -> None:
        start = self.page*self.per_page
        lb = "\n".join([f"{start+1}. {f'{player.user.mention}' if player.user else ('\u200b'*obscure).join(player.names[0])} - {self.show_key(player)}" for player in self.players[start:start+self.per_page]])
        self.display.content = f"# Leaderboard\n{self.key_desc()}\n{lb}\n-# Page {self.page+1} of {len(self.players) // self.per_page}"

    def go_to_page(self, num: int) -> None:
        self.page = num
        self.previous.disabled = not self.has_page(num - 1)
        self.next.disabled = not self.has_page(num + 1)

    ar = discord.ui.ActionRow()

    @ar.button(label="Prev", emoji="⬅️", disabled=True)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page - 1)
        self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.go_to_page(self.page + 1)
        self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Jump", emoji="↪️")
    async def jump(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(Jump(self))

    def destroy(self) -> None:
        self.draw(obscure=True)
        self.remove_item(self.ar)


class TopPaginatorView(discord.ui.LayoutView):
    message: discord.Message

    def __init__(self, owner: discord.User | discord.Member, players: Iterable[PlayerInfo], part: Criterion) -> None:
        super().__init__()
        self.owner = owner
        self.container = TopPaginator(players, part)
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message("You can't control this element.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.container.destroy()
        await self.message.edit(view=self)


class Stats(commands.Cog):
    """Player statistics."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.task = asyncio.create_task(self.update_loop())

    async def cog_unload(self) -> None:
        self.task.cancel()

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

    async def games(self) -> AsyncIterator[gamelogs.GameResult]:
        async with self.bot.db.execute("SELECT gist, analysis, analysis_version, from_log FROM Games") as cur:
            async for gist, game, version, from_log in cur:
                if version < gamelogs.version:
                    asyncio.create_task(self.update_game(gist, from_log))
                yield game

    async def run_games(self) -> Players:
        players = defaultdict(lambda: PlayerStats(model.rating(), defaultdict(Winrate)))

        async for game in self.games():
            teams = [[], []]
            ratings = [[], []]
            for player in game.players:
                await self.bot.db.execute("INSERT OR IGNORE INTO Names VALUES (?, (SELECT COALESCE(MAX(player), 0) + 1 FROM Names))", (player.account_name,))

                async with self.bot.db.execute("SELECT player FROM Names WHERE name = ?", (player.account_name,)) as cur:
                    key, = await cur.fetchone()  # type: ignore

                # update winrates
                saw_hunt = game.hunt_reached and (not player.died or player.died >= (game.hunt_reached, "day"))
                if player.ending_ident.faction == gamelogs.town:
                    c = RoleClass.TOWN_HUNT if saw_hunt else RoleClass.TOWN
                elif player.ending_ident.role.default_faction == gamelogs.coven:
                    c = RoleClass.COVEN
                else:
                    c = RoleClass.TT_HUNT if saw_hunt else RoleClass.TT
                if player.won:
                    players[key].winrates[c].s += 1
                players[key].winrates[c].n += 1

                i = player.ending_ident.faction == gamelogs.coven
                teams[i].append(key)
                ratings[i].append(players[key].rating)

            # pad coven
            avg = sum([r.mu for r in ratings[1]]) / len(ratings[1])
            ratings[1].extend([model.rating(mu=avg, sigma=avg/3) for _ in range(len(ratings[0]) - len(ratings[1]))])

            new_ratings = model.rate(ratings, ranks=[1, 2] if game.victor == gamelogs.town else [2, 1])
            for team, team_ratings in zip(teams, new_ratings):
                for player, new_rating in zip(team, team_ratings):
                    players[player].rating = new_rating

        return players

    async def save_stats(self, players: Players) -> None:
        for player, stats in players.items():
            await self.bot.db.execute("INSERT OR REPLACE INTO RatingCache (player, mu, sigma, winrates) VALUES (?, ?, ?, ?)", (
                player,
                stats.rating.mu,
                stats.rating.sigma,
                {str(k.value): [v.s, v.n] for k, v in stats.winrates.items()},
            ))
        await self.bot.db.commit()

    async def update_loop(self) -> None:
        while True:
            try:
                async with self.bot.db.execute("SELECT last_update FROM Globals") as cur:
                    last_update, = await cur.fetchone()  # type: ignore
                next_update = config.next_update(last_update)
                await discord.utils.sleep_until(next_update - config.grace_period)

                players = await self.run_games()
                old = await self.fetch_discord_players()
                await discord.utils.sleep_until(next_update)

                await self.save_stats(players)
                new = await self.fetch_discord_players()
                changes = []
                for old_you, new_you in zip(old, new):
                    if not new_you.user or old_you.rating == new_you.rating:
                        continue
                    recents = new_you.winrate_in(Part.ALL) - old_you.winrate_in(Part.ALL)
                    changes.append(f"{new_you.user.mention} {old_you.ordinal():.0f} -> {new_you.ordinal():.0f} (W-L {recents.s}-{recents.n-recents.s})")
                embed = discord.Embed(
                    title=f"{last_update.strftime('%B %-d') if last_update.year == next_update.year else last_update.strftime('%B %-d %Y')} - {next_update.strftime('%B %-d %Y')}",
                    description="\n".join(changes),
                    colour=discord.Colour.dark_purple(),
                )
                report_channel = self.bot.get_partial_messageable(config.report_channel_id)
                await report_channel.send(embed=embed)

                await self.bot.db.execute("UPDATE Globals SET last_update = ?", (datetime.datetime.now(datetime.timezone.utc),))
            except Exception:
                log.exception("error in update loop")
                await asyncio.sleep(10)

    async def _row_to_player_info(self, r: aiosqlite.Row) -> PlayerInfo:
        async with self.bot.db.execute("SELECT name FROM Names WHERE player = ?", (r["player"],)) as cur:
            names = await cur.fetchall()
        return PlayerInfo(
            r["player"],
            [x[0] for x in names],
            self.bot.get_user(r["discord_id"]) if r["discord_id"] else None,
            r["rank"] is None,
            r["rank"],
            model.rating(r["mu"], r["sigma"]),
            {RoleClass(int(k)): Winrate(s, n) for k, (s, n) in r["winrates"].items()},
        )

    async def fetch_player(self, player: int) -> PlayerInfo:
        async with self.bot.db.execute("SELECT * FROM RatingCache LEFT JOIN Ranks USING (player) LEFT JOIN DiscordConnections USING (player) WHERE player = ?", (player,)) as cur:
            r = await cur.fetchone()
            assert r is not None, "oh, I thought this was impossible"
        return await self._row_to_player_info(r)

    async def fetch_players(self) -> list[PlayerInfo]:
        async with self.bot.db.execute("SELECT * FROM RatingCache INNER JOIN Ranks USING (player) LEFT JOIN DiscordConnections USING (player)") as cur:
            return [await self._row_to_player_info(r) async for r in cur]

    async def fetch_discord_players(self) -> list[PlayerInfo]:
        async with self.bot.db.execute("SELECT * FROM RatingCache INNER JOIN Ranks USING (player) INNER JOIN DiscordConnections USING (player) ORDER BY player") as cur:
            return [await self._row_to_player_info(r) async for r in cur]

    @commands.command()
    async def info(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute("SELECT last_update FROM Globals") as cur:
            last_update, = await cur.fetchone()  # type: ignore

        r = Counter()
        async for game in self.games():
            town_won = game.victor == gamelogs.town
            r[town_won, bool(game.hunt_reached)] += 1

        embed = discord.Embed(
            title="Lookout",
            description=textwrap.dedent(f"""
                Dutifully tracking {sum(r.values()):,} games,
                {r[True, False] + r[True, True]:,} Town victories,
                {r[False, False] + r[False, True]:,} Coven victories ({r[False, False]:,} by majority, {r[False, True]:,} by countdown),
                {r[False, True] + r[True, True]:,} hunts ({r[True, True]:,} won by Town, {r[False, True]:,} won by Coven),
                and {sum(r.values())*15:,} crashouts.

                Last rating update on {discord.utils.format_dt(last_update, "D")}, next on {discord.utils.format_dt(config.next_update(last_update))}
                Made with love by {config.me_emoji} <3
            """),
            colour=discord.Colour.dark_green(),
        )

        await ctx.send(embed=embed)

    @commands.command()
    async def player(self, ctx: commands.Context, *, player: PlayerInfo) -> None:
        r = None
        for name in player.names:
            async with self.bot.db.execute("SELECT thread_id FROM Blacklists WHERE account_name = ?", (name,)) as cur:
                r = await cur.fetchone()
            if r:
                break

        names = ", ".join(player.names)
        title = f"{player.user.mention} ({names})" if player.user else names
        if player.hidden:
            embed = discord.Embed(description=f"### {title}\nThis player has chosen to hide their profile.")
        else:
            embed = discord.Embed(description=f"### {title}\nRated {player.ordinal():.0f} (#{player.rank:,})")
            embed.add_field(name="Winrates", value=textwrap.dedent(f"""
                - Overall {player.winrate_in(Part.ALL)}
                - Town {player.winrate_in(Part.TOWN)}
                - Purple {player.winrate_in(Part.PURPLE)}
                  - Coven {player.winrate_in(Part.COVEN)}
                  - TT {player.winrate_in(Part.TT)}
            """))
            embed.add_field(name="Winrates in hunt", value=textwrap.dedent(f"""
                - Town {player.winrate_in(Part.TOWN_HUNT)}
                - TT {player.winrate_in(Part.TT_HUNT)}
            """))
        if r:
            embed.add_field(name="Player blacklisted", value=f"<#{r[0]}>", inline=False)
        game_count = f"{n:,} games" if (n := player.played_in(Part.ALL)) != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")
        await ctx.send(embed=embed)

    @commands.command(aliases=["lb", "leaderboard", "players"])
    async def top(self, ctx: commands.Context, *, criterion: str = "rating") -> None:
        criterion = criterion.casefold()
        if "hunt" in criterion:
            part = Part.TOWN_HUNT if "town" in criterion or "green" in criterion else Part.TT_HUNT
        else:
            part = {
                "winrate": Part.ALL,
                "overall": Part.ALL,
                "town": Part.TOWN,
                "green": Part.TOWN,
                "purple": Part.PURPLE,
                "coven": Part.COVEN,
                "tt": Part.TT,
                "played": "played",
            }.get(criterion, "rating")
        view = TopPaginatorView(ctx.author, await self.fetch_players(), part)
        view.message = await ctx.send(view=view)

    @commands.command()
    async def hide(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute("INSERT OR REPLACE INTO Hidden (player) SELECT player FROM DiscordConnections WHERE discord_id = ? RETURNING player", (ctx.author.id,)) as cur:
            r = await cur.fetchone()
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    async def show(self, ctx: commands.Context) -> None:
        async with self.bot.db.execute("SELECT player FROM DiscordConnections WHERE discord_id = ?", (ctx.author.id,)) as cur:
            r = await cur.fetchone()
        if not r:
            await ctx.send("Sorry, I don't know who you are.")
            return
        await self.bot.db.execute("DELETE FROM Hidden WHERE player = ?", (r[0],))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command(name="is")
    @commands.is_owner()
    async def _is(self, ctx: commands.Context, a: PlayerInfo, b: PlayerInfo) -> None:
        await self.bot.db.execute("UPDATE DiscordConnections SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.execute("UPDATE Names SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.commit()
        await ctx.send(":+1:")

    @commands.command()
    @commands.check_any(commands.has_role("Game host"), commands.is_owner())
    async def connect(self, ctx: commands.Context, who: discord.Member, *, player: PlayerInfo) -> None:
        await self.bot.db.execute("INSERT OR REPLACE INTO DiscordConnections (discord_id, player) VALUES (?, ?)", (who.id, player.id))
        await self.bot.db.commit()
        await ctx.send(":+1:")


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
