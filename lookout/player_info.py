from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import discord
from discord.ext import commands

from .bot import *

if TYPE_CHECKING:
    from openskill.models import PlackettLuceRating

    from .stats import Stats


@dataclass
class PlayerInfo:
    id: int
    rank: int
    rating: PlackettLuceRating
    _stats: Stats = field(repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        self.bot = self._stats.bot

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

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
    async def convert(cls, ctx: commands.Context, argument: str) -> PlayerInfo:
        stats: Stats = ctx.bot.get_cog("Stats")

        if player := await stats.fetch_player_by_name(argument.replace("\u200b", ""), stats.now()):
            return player

        async with ctx.bot.db.acquire() as conn:
            try:
                member = await commands.MemberConverter().convert(ctx, argument)
            except commands.MemberNotFound:
                r = await conn.fetchone("SELECT word FROM FuzzyNames WHERE word MATCH ? AND top = 1", (argument.rstrip("*"),))
                did_you_mean = f"\nDid you mean {r[0]}?" if r else ""

                raise commands.BadArgument(f"I don't know the player '{argument}'.{did_you_mean}")

            r = await conn.fetchone("SELECT player FROM DiscordConnections WHERE discord_id = ?", (member.id,))
            if not r:
                raise commands.BadArgument(f"I don't know what {member.mention}'s ToS2 account is.")

        return await stats.fetch_player(r[0], stats.now())
