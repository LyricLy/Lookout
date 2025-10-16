from __future__ import annotations

import enum
import math
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


model = PlackettLuce(limit_sigma=True)

def cut_up(s: int, n: int) -> tuple[float, float]:
    z = 3
    avg = s / n
    divisor = 1 + z*z / n
    return (avg + (z*z/(2*n))) / divisor, z/(2*n) * math.sqrt(4 * n * avg * (1-avg) + z*z) / divisor

def show_rate(s: int, n: int) -> str:
    try:
        centre, plus_or_minus = cut_up(s, n)
    except ZeroDivisionError:
        return "N/A (no games)"
    else:
        return f"{centre*100:.2f}% ± {plus_or_minus*100:.2f}%"

class RoleClass(enum.Enum):
    TOWN = 0
    COVEN = 1
    TT = 2

JUST_TOWN = [RoleClass.TOWN]
JUST_COVEN = [RoleClass.COVEN]
JUST_TT = [RoleClass.TT]
PURPLE = [RoleClass.COVEN, RoleClass.TT]
ALL = [RoleClass.TOWN, *PURPLE]

@dataclass
class PlayerStats:
    account_name: str
    rating: PlackettLuceRating = field(default_factory=model.rating)
    games_won: Counter[RoleClass] = field(default_factory=Counter)
    games_in: Counter[RoleClass] = field(default_factory=Counter)

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

    def winrate_in(self, classes: Iterable[RoleClass]) -> str:
        return show_rate(sum([self.games_won[c] for c in classes]), self.played_in(classes))

    def played_in(self, classes: Iterable[RoleClass]) -> int:
        return sum([self.games_in[c] for c in classes])

type Players = dict[str, PlayerStats]


class Stats(commands.Cog):
    """Player statistics."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self._players: Players | None = None

    async def games(self) -> AsyncIterator[gamelogs.GameResult]:
        async with self.bot.db.execute("SELECT analysis FROM Games") as cur:
            async for game, in cur:
                yield game

    async def run_game(self, players: Players, game: gamelogs.GameResult) -> None:
        teams = [[], []]
        ratings = [[], []]
        for player in game.players:
            async with self.bot.db.execute("SELECT dst FROM Aliases WHERE src = ?", (player.account_name,)) as cur:
                account_name, = (await cur.fetchone()) or (player.account_name,)
            key = account_name.casefold()
            if key not in players:
                players[key] = PlayerStats(account_name)

            # update winrates
            if player.ending_ident.faction == gamelogs.town:
                c = RoleClass.TOWN
            elif player.ending_ident.role.default_faction == gamelogs.coven:
                c = RoleClass.COVEN
            else:
                c = RoleClass.TT
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
    async def player(self, ctx: commands.Context, *, account_name: str) -> None:
        players = await self.players(ctx)
        if not (player := players.get(account_name.casefold())):
            await ctx.send("I don't know that player.")
            return
        rank = sorted([p.ordinal() for p in players.values()], reverse=True).index(player.ordinal()) + 1

        async with self.bot.db.execute("SELECT thread_id FROM Blacklists WHERE account_name = ?", (account_name,)) as cur:
            r = await cur.fetchone()

        embed = discord.Embed(title=player.account_name, description=f"Rated {player.ordinal():.0f} (#{rank})")
        embed.add_field(name="Winrates", value=textwrap.dedent(f"""
        - Overall {player.winrate_in([RoleClass.TOWN, RoleClass.COVEN, RoleClass.TT])}
        - Town {player.winrate_in([RoleClass.TOWN])}
        - Purple {player.winrate_in([RoleClass.COVEN, RoleClass.TT])}
          - Coven {player.winrate_in([RoleClass.COVEN])}
          - TT {player.winrate_in([RoleClass.TT])}
        """))
        if r:
            embed.add_field(name="Player blacklisted", value=f"<#{r[0]}>")
        game_count = f"{n} games" if (n := player.played_in(ALL)) != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")
        await ctx.send(embed=embed)

    @commands.command()
    async def top(self, ctx: commands.Context) -> None:
        n = 25
        r = []
        players = await self.players(ctx)
        for i, (name, stats) in enumerate(sorted(players.items(), key=lambda kv: kv[1].ordinal(), reverse=True)[:n], start=1):
            r.append(f"{i}. {name} - {stats.ordinal():.0f}")
        await ctx.send(embed=discord.Embed(title=f"Top {n}", description="\n".join(r)))


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
