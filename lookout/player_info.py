from dataclasses import dataclass, field
from typing import Literal, Self

import discord
from discord.ext import commands
from openskill.models import PlackettLuce, PlackettLuceRating

from .bot import *
from .logs import Timecode


model = PlackettLuce(limit_sigma=True)
RATINGS = "(SELECT player, game, mu_after AS mu, sigma_after AS sigma, MAX(timecode) FROM Appearances WHERE timecode < ? GROUP BY player) AS Ratings"


class PlayerRating:
    def __init__(self, rating: PlackettLuceRating, at: Timecode, bot: Lookout) -> None:
        self.rating = rating
        self.at = at
        self.bot = bot

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

    @needs_db
    async def rank(self, conn: Connection) -> int:
        rank, = await conn.fetchone(
            f"SELECT 1 + COUNT(*) FROM {RATINGS} WHERE mu - 3.0 * sigma > ? AND NOT EXISTS(SELECT 1 FROM Hidden WHERE player = Ratings.player)",
            (self.at, self.rating.ordinal()),
        )
        return rank


@dataclass
class PlayerInfo:
    id: int
    bot: Lookout = field(repr=False, compare=False)

    @needs_db
    async def rating(self, conn: Connection, at: Timecode, *, this_gen: bool = False) -> PlayerRating | None:
        if this_gen:
            r = await conn.fetchone(f"SELECT mu, sigma, Games.generation = Globals.generation FROM {RATINGS} INNER JOIN Games ON gist = game, Globals WHERE player = ?", (at, self.id))
        else:
            r = await conn.fetchone(f"SELECT mu, sigma, 1 FROM {RATINGS} WHERE player = ?", (at, self.id))
        assert r is None or r[-1], "rating came from previous generation"
        return PlayerRating(model.rating(r["mu"], r["sigma"]), at, self.bot) if r else None

    @needs_db
    async def names(self, conn: Connection) -> list[str]:
        return [r[0] for r in await conn.fetchall("SELECT name FROM Names WHERE player = ? ORDER BY LENGTH(name), name", (self.id,))]

    @needs_db
    async def hidden(self, conn: Connection) -> Literal["user", "cheated"] | None: 
        r = await conn.fetchone("SELECT why FROM Hidden WHERE player = ?", (self.id,))
        return r[0] if r else None

    @needs_db
    async def user(self, conn: Connection) -> discord.User | None:
        r = await conn.fetchone("SELECT discord_id FROM DiscordConnections WHERE player = ?", (self.id,))
        return self.bot.get_user(r[0]) if r else None

    @classmethod
    async def by_name(cls, conn: Connection, name: str, bot: Lookout) -> PlayerInfo | None:
        r = await conn.fetchone("SELECT player FROM Names WHERE name = ?", (name,))
        return PlayerInfo(r[0], bot) if r else None

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> PlayerInfo:
        from .stats import Stats

        stats = ctx.bot.require_cog(Stats)

        async with ctx.bot.acquire() as conn:
            if player := await PlayerInfo.by_name(conn, argument.replace("\u200b", ""), ctx.bot):
                return player

            try:
                member = await commands.MemberConverter().convert(ctx, argument)
            except commands.MemberNotFound:
                r = await conn.fetchone("SELECT word FROM FuzzyNames WHERE word MATCH ? AND top = 1", (argument.rstrip("*"),))
                did_you_mean = f"\nDid you mean {r[0]}?" if r else ""

                raise commands.BadArgument(f"I don't know the player '{argument}'.{did_you_mean}")

            r = await conn.fetchone("SELECT player FROM DiscordConnections WHERE discord_id = ?", (member.id,))
            if not r:
                raise commands.BadArgument(f"I don't know what {member.mention}'s ToS2 account is.")

        return PlayerInfo(r[0], ctx.bot)
