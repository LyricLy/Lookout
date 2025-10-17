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
                if player.account_name.casefold() == t.casefold():
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
    header = discord.ui.TextDisplay("# Leaderboard")
    display = discord.ui.TextDisplay("")

    def __init__(self, players: list[PlayerStats]) -> None:
        super().__init__(accent_colour=discord.Colour(0x6bfc03))
        self.players = players
        self.per_page = 15
        self.page = 0
        self.draw()

    def has_page(self, num: int) -> bool:
        return 0 <= num*self.per_page < len(self.players)

    def draw(self, *, obscure: bool = False) -> None:
        start = self.page*self.per_page
        lb = "\n".join([f"{start+1}. {('\u200b'*obscure).join(player.account_name)} - {player.ordinal():.0f}" for player in self.players[start:start+self.per_page]])
        self.display.content = f"{lb}\n-# Page {self.page+1} of {len(self.players) // self.per_page}"

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

    def __init__(self, owner: discord.User | discord.Member, players: list[PlayerStats]) -> None:
        super().__init__()
        self.owner = owner
        self.container = TopPaginator(players)
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
        game_count = f"{n:,} games" if (n := player.played_in(ALL)) != 1 else "1 game"
        embed.set_footer(text=f"Seen in {game_count}")
        await ctx.send(embed=embed)

    @commands.command()
    async def top(self, ctx: commands.Context) -> None:
        players = await self.players(ctx)
        view = TopPaginatorView(ctx.author, sorted(players.values(), key=PlayerStats.ordinal, reverse=True))
        view.message = await ctx.send(view=view)


async def setup(bot: Lookout):
    await bot.add_cog(Stats(bot))
