from __future__ import annotations

import asyncio
import enum
import math
import logging
import textwrap
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable

import discord
import gamelogs
from discord.ext import commands
from openskill.models import PlackettLuce, PlackettLuceRating

from .bot import Lookout
from .logs import gist_of


log = logging.getLogger(__name__)

model = PlackettLuce(limit_sigma=True)


@dataclass
class Winrate:
    s: int
    n: int

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
            return f"{centre*100:.2f}% ± {radius*100:.2f}%"

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
class PlayerStats:
    id: int
    names: list[str]
    member: int | None
    rating: PlackettLuceRating = field(default_factory=model.rating)
    games_won: Counter[RoleClass] = field(default_factory=Counter)
    games_in: Counter[RoleClass] = field(default_factory=Counter)

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

    def winrate_in(self, classes: Part) -> Winrate:
        return Winrate(sum([self.games_won[c] for c in classes.value]), self.played_in(classes))

    def played_in(self, classes: Part) -> int:
        return sum([self.games_in[c] for c in classes.value])

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> PlayerStats:
        players = await ctx.bot.get_cog("Stats").players(ctx)
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
        return players[r[0]]

type Players = dict[int, PlayerStats]


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
            self.container.page = idx // self.container.per_page
        else:
            if not self.container.has_page(page):
                await interaction.response.send_message(f"Page number {page} is out of bounds.", ephemeral=True)
                return
            self.container.page = page
        self.container.draw()
        await interaction.response.edit_message(view=self.container.view)


class TopPaginator(discord.ui.Container):
    display = discord.ui.TextDisplay("")

    def __init__(self, players: Iterable[PlayerStats], part: Part | None) -> None:
        super().__init__(accent_colour=discord.Colour(0x6bfc03))
        self.part = part
        self.players = sorted(players, key=self.key, reverse=True)
        self.per_page = 15
        self.page = 0
        self.draw()

    def key(self, player: PlayerStats) -> Winrate | float:
        return player.winrate_in(self.part) if self.part is not None else player.ordinal()

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
            case _:
                return ""
        return f"Sorting by {n}. Confidence intervals are ordered by lower bound, not the centre."

    def show_key(self, player: PlayerStats) -> str:
        return str(player.winrate_in(self.part)) if self.part is not None else f"{player.ordinal():.0f}"

    def has_page(self, num: int) -> bool:
        return 0 <= num*self.per_page < len(self.players)

    def draw(self, *, obscure: bool = False) -> None:
        start = self.page*self.per_page
        lb = "\n".join([f"{start+1}. {f'<@{player.member}>' if player.member else ('\u200b'*obscure).join(player.names[0])} - {self.show_key(player)}" for player in self.players[start:start+self.per_page]])
        self.display.content = f"# Leaderboard\n{self.key_desc()}\n{lb}\n-# Page {self.page+1} of {len(self.players) // self.per_page}"

    ar = discord.ui.ActionRow()

    @ar.button(label="Prev", emoji="⬅️", disabled=True)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        if not self.has_page(self.page - 1):
            button.disabled = True
        self.next.disabled = False
        self.draw()
        await interaction.response.edit_message(view=self.view)

    @ar.button(label="Next", emoji="➡️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        if not self.has_page(self.page + 1):
            button.disabled = True
        self.previous.disabled = False
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

    def __init__(self, owner: discord.User | discord.Member, players: Iterable[PlayerStats], part: Part | None) -> None:
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
        self._players: Players | None = None

    async def update_game(self, gist, from_log):
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

    async def run_game(self, players: Players, game: gamelogs.GameResult) -> None:
        teams = [[], []]
        ratings = [[], []]
        for player in game.players:
            await self.bot.db.execute("INSERT OR IGNORE INTO Names VALUES (?, (SELECT COALESCE(MAX(player), 0) + 1 FROM Names))", (player.account_name,))

            async with self.bot.db.execute("SELECT player FROM Names WHERE name = ?", (player.account_name,)) as cur:
                key, = await cur.fetchone()  # type: ignore
            async with self.bot.db.execute("SELECT name FROM Names WHERE player = ? ORDER BY LENGTH(name), name", (key,)) as cur:
                names = [name for name, in await cur.fetchall()]
            async with self.bot.db.execute("SELECT discord_id FROM DiscordConnections WHERE player = ?", (key,)) as cur:
                member = await cur.fetchone()

            if key not in players:
                players[key] = PlayerStats(key, names, member[0] if member else None)

            # update winrates
            saw_hunt = game.hunt_reached and (not player.died or player.died >= (game.hunt_reached, "day"))
            if player.ending_ident.faction == gamelogs.town:
                c = RoleClass.TOWN_HUNT if saw_hunt else RoleClass.TOWN
            elif player.ending_ident.role.default_faction == gamelogs.coven:
                c = RoleClass.COVEN
            else:
                c = RoleClass.TT_HUNT if saw_hunt else RoleClass.TT
            if player.won:
                players[key].games_won[c] += 1
            players[key].games_in[c] += 1

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

    @commands.Cog.listener()
    async def on_game(self, game: gamelogs.GameResult) -> None:
        if self._players is not None:
            await self.run_game(self._players, game)

    async def players(self, ctx: commands.Context | None = None) -> Players:
        if self._players is not None:
            return self._players
        r = {}
        async with ctx.typing() if ctx else nullcontext():
            async for game in self.games():
                await self.run_game(r, game)
        self._players = r
        return r

    @commands.command()
    async def player(self, ctx: commands.Context, *, player: PlayerStats) -> None:
        players = await self.players()
        rank = sorted([p.ordinal() for p in players.values()], reverse=True).index(player.ordinal()) + 1

        r = None
        for name in player.names:
            async with self.bot.db.execute("SELECT thread_id FROM Blacklists WHERE account_name = ?", (name,)) as cur:
                r = await cur.fetchone()
            if r:
                break

        names = ", ".join(player.names)
        title = f"<@{player.member}> ({names})" if player.member else names
        embed = discord.Embed(description=f"### {title}\nRated {player.ordinal():.0f} (#{rank:,})")
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
            embed.add_field(name="Player blacklisted", value=f"<#{r[0]}>")
        game_count = f"{n:,} games" if (n := player.played_in(Part.ALL)) != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")
        await ctx.send(embed=embed)

    @commands.command()
    async def top(self, ctx: commands.Context, *, criterion: str = "rating") -> None:
        players = await self.players(ctx)

        criterion = criterion.casefold()
        if "hunt" in criterion:
            part = Part.TOWN_HUNT if "town" in criterion or "green" in criterion else Part.TT_HUNT
        else:
            part = {
                "overall": Part.ALL,
                "town": Part.TOWN,
                "green": Part.TOWN,
                "purple": Part.PURPLE,
                "coven": Part.COVEN,
                "tt": Part.TT,
            }.get(criterion)
        view = TopPaginatorView(ctx.author, players.values(), part)
        view.message = await ctx.send(view=view)

    @commands.command(name="is")
    @commands.is_owner()
    async def _is(self, ctx: commands.Context, a: PlayerStats, b: PlayerStats) -> None:
        await self.bot.db.execute("UPDATE DiscordConnections SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.execute("UPDATE Names SET player = ? WHERE player = ?", (a.id, b.id))
        await self.bot.db.commit()
        self._players = None
        await ctx.send(":+1:")

    @commands.command()
    @commands.is_owner()
    async def connect(self, ctx: commands.Context, who: discord.Member, *, player: PlayerStats) -> None:
        await self.bot.db.execute("INSERT OR REPLACE INTO DiscordConnections (discord_id, player) VALUES (?, ?)", (who.id, player.id))
        await self.bot.db.commit()
        player.member = who.id
        await ctx.send(":+1:")


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
