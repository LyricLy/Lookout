from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from openskill.models import PlackettLuceRating

    from .stats import Stats


@dataclass
class PlayerInfo:
    id: int
    rank: int
    rating: PlackettLuceRating
    _stats: Stats = field(repr=False, compare=False, kw_only=True)

    def ordinal(self) -> float:
        return self.rating.ordinal(target=1000, alpha=21)

    async def names(self) -> list[str]:
        async with self._stats.bot.db.execute("SELECT name FROM Names WHERE player = ? ORDER BY LENGTH(name), name", (self.id,)) as cur:
            return [r[0] async for r in cur]

    async def hidden(self) -> Literal["user", "cheated"] | None: 
        async with self._stats.bot.db.execute("SELECT why FROM Hidden WHERE player = ?", (self.id,)) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def user(self) -> discord.User | None:
        async with self._stats.bot.db.execute("SELECT discord_id FROM DiscordConnections WHERE player = ?", (self.id,)) as cur:
            r = await cur.fetchone()
        return self._stats.bot.get_user(r[0]) if r else None

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> PlayerInfo:
        stats: Stats = ctx.bot.get_cog("Stats")

        if player := await stats.fetch_player_by_name(argument.replace("\u200b", ""), stats.now()):
            return player

        try:
            member = await commands.MemberConverter().convert(ctx, argument)
        except commands.MemberNotFound:
            async with stats.bot.db.execute("SELECT word FROM FuzzyNames WHERE word MATCH ? AND top = 1", (argument.rstrip("*"),)) as cur:
                r = await cur.fetchone()
            did_you_mean = f"\nDid you mean '{r[0]}'?" if r else ""

            raise commands.BadArgument(f"I don't know the player '{argument}'.{did_you_mean}")

        async with ctx.bot.db.execute("SELECT player FROM DiscordConnections WHERE discord_id = ?", (member.id,)) as cur:
            r = await cur.fetchone()
        if not r:
            raise commands.BadArgument(f"I don't know what {member.mention}'s ToS2 account is.")

        return await stats.fetch_player(r[0], stats.now())
