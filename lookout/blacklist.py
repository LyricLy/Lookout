from __future__ import annotations

import asyncio
import re

import discord
import gamelogs
import logging
from discord.ext import commands

import config
from .bot import Lookout
from .logs import gist_of


log = logging.getLogger(__name__)


class Blacklist(commands.Cog):
    """Monitoring of the blacklist channel."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self.channel: discord.ForumChannel | None = None

    async def check_thread(self, thread: discord.Thread, *, catchup: bool = False) -> None:
        starter = thread.starter_message or await thread.fetch_message(thread.id)
        reason = starter.content if starter.content else None
        await self.bot.db.execute("DELETE FROM Blacklists WHERE thread_id = ?", (thread.id,))
        if any(t.id in config.damning_tags for t in thread.applied_tags):
            concerned_players = [r for x in re.split(r",|&|\band\b", thread.name.strip("()").rsplit(":", 1)[-1], flags=re.I) if (r := x.strip()) and not r.isdigit()]
            for player in concerned_players:
                log.debug("%s is blacklisted in thread %d (reason: %s)", player, thread.id, reason)
                await self.bot.db.execute("INSERT INTO Blacklists (thread_id, account_name, reason) VALUES (?, ?, ?)", (thread.id, player, reason))
        await self.bot.db.commit()

        if not catchup:
            return
        async with self.bot.db.execute("SELECT EXISTS(SELECT 1 FROM BlacklistGames WHERE thread_id = ?)", (thread.id,)) as cur:
            exists, = await cur.fetchone()  # type: ignore
        if not exists:
            c = 0
            async for msg in thread.history(limit=None):
                c += await self.check_for_logs(msg)
            log.info("checked thread %d for logs and found %d", thread.id, c)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        channel = self.bot.get_channel(config.channel_id)
        assert isinstance(channel, discord.ForumChannel)
        self.channel = channel

        seen = []

        # new threads/updates
        for thread in channel.threads:
            seen.append(thread.id)
            await self.check_thread(thread, catchup=True)
        async for thread in channel.archived_threads(limit=None):
            seen.append(thread.id)
            await self.check_thread(thread, catchup=True)

        # removals
        await self.bot.db.execute(f"DELETE FROM Blacklists WHERE thread_id NOT IN ({','.join(map(str, seen))})")
        await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self.check_thread(thread)

        async with self.bot.db.execute("SELECT EXISTS(SELECT 1 FROM BlacklistGames WHERE thread_id = ?)", (thread.id,)) as cur:
            exists, = await cur.fetchone()  # type: ignore
        if not exists:
            await thread.add_tags(discord.Object(id=config.no_logs_tag))

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        if self.channel and payload.parent_id == config.channel_id:
            thread = await self.channel.guild.fetch_channel(payload.thread_id)
            assert isinstance(thread, discord.Thread)
            await self.check_thread(thread)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if payload.parent_id == config.channel_id:
            await self.bot.db.execute("DELETE FROM Blacklists WHERE thread_id = ?", (payload.thread_id,))
            await self.bot.db.commit()

    async def check_for_logs(self, message: discord.Message) -> bool:
        did_anything = False

        for attach in message.attachments:
            if not attach.filename.endswith(".html"):
                continue
            try:
                content = (await attach.read()).decode()
            except UnicodeDecodeError:
                continue
            try:
                game = gamelogs.parse_result(content)
            except gamelogs.BadLogError:
                continue

            did_anything = True
            await self.bot.db.execute("INSERT OR IGNORE INTO BlacklistGames (thread_id, gist) VALUES (?, ?)", (message.channel.id, gist_of(game)))
            await self.bot.db.commit()

        return did_anything

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.Thread) or message.channel.parent_id != config.channel_id:
            return

        if await self.check_for_logs(message):
            await message.channel.remove_tags(discord.Object(id=config.no_logs_tag))


async def setup(bot: Lookout):
    await bot.add_cog(Blacklist(bot))
