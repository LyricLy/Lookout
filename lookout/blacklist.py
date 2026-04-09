import asyncio
import re
import io

import discord
import gamelogs
import logging
from discord.ext import commands

import config
from .bot import *
from .logs import gist_of


log = logging.getLogger(__name__)


class Blacklist(commands.Cog):
    """Monitoring of the blacklist channel."""

    def __init__(self, bot: Lookout) -> None:
        self.bot = bot
        self.channel: discord.ForumChannel | None = None

    @needs_db
    async def check_thread(self, conn: Connection, thread: discord.Thread, *, catchup: bool = False) -> None:
        await conn.execute("DELETE FROM Blacklists WHERE thread_id = ?", (thread.id,))

        if any(t.id in config.damning_tags for t in thread.applied_tags):
            starter = thread.starter_message or await thread.fetch_message(thread.id)
            reason = starter.content if starter.content else None
            no_retrial = any(t.id in config.no_retrial_tags for t in thread.applied_tags)

            concerned_players = [r for x in re.split(r",|&|\band\b", thread.name.strip("()").rsplit(":", 1)[-1], flags=re.I) if (r := x.strip()) and not r.isdigit()]
            for player in concerned_players:
                log.debug("%s is blacklisted in thread %d (reason: %s)", player, thread.id, reason)
                await conn.execute("INSERT INTO Blacklists (thread_id, account_name, reason, no_retrial) VALUES (?, ?, ?, ?)", (thread.id, player, reason, no_retrial))

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
        async with self.bot.acquire() as conn:
            keep_ids = ",".join(map(str, seen))
            await conn.execute(f"DELETE FROM Blacklists WHERE thread_id NOT IN ({keep_ids})")
            await conn.execute(f"DELETE FROM BlacklistGames WHERE thread_id NOT IN ({keep_ids})")

    @commands.Cog.listener()
    @needs_db
    async def on_thread_create(self, conn: Connection, thread: discord.Thread) -> None:
        await asyncio.sleep(1)
        await self.check_thread(thread)

        if not await conn.fetchone("SELECT 1 FROM BlacklistGames WHERE thread_id = ?", (thread.id,)):
            await thread.add_tags(discord.Object(id=config.no_logs_tag))

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        if self.channel and payload.parent_id == config.channel_id:
            thread = await self.channel.guild.fetch_channel(payload.thread_id)
            assert isinstance(thread, discord.Thread)
            await self.check_thread(thread)

    @commands.Cog.listener()
    @needs_db
    async def on_raw_thread_delete(self, conn: Connection, payload: discord.RawThreadDeleteEvent) -> None:
        if payload.parent_id == config.channel_id:
            await conn.execute("DELETE FROM Blacklists WHERE thread_id = ?", (payload.thread_id,))

    @needs_db
    async def check_for_logs(self, conn: Connection, message: discord.Message) -> bool:
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
            await conn.execute("INSERT OR IGNORE INTO BlacklistGames (thread_id, gist) VALUES (?, ?)", (message.channel.id, gist_of(game)))

        return did_anything

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.channel, discord.Thread) or message.channel.parent_id != config.channel_id:
            return

        if await self.check_for_logs(message):
            await message.channel.remove_tags(discord.Object(id=config.no_logs_tag))

    @commands.command()
    @commands.is_owner()
    @needs_db
    async def bldump(self, conn: Connection, ctx: Context, target: discord.ForumChannel) -> None:
        """Write the blacklist to a target forum channel."""
        if target.id == config.channel_id:
            await ctx.send("It's not a good idea to dump into the current blacklist channel.")
            return

        for thread, reason in await conn.fetchall("SELECT DISTINCT thread_id, reason FROM Blacklists"):
            names = [x for x, in await conn.fetchall("SELECT account_name FROM Blacklists WHERE thread_id = ?", (thread,))]
            files = await conn.fetchall(
                "SELECT filename, clean_content FROM BlacklistGames INNER JOIN Gamelogs ON hash = from_log INNER JOIN Games ON BlacklistGames.gist = Games.gist WHERE thread_id = ?",
                (thread,),
            )
            await target.create_thread(name=", ".join(names), content=reason, files=[discord.File(io.BytesIO(content.encode()), filename=filename) for filename, content in files])


async def setup(bot: Lookout):
    await bot.add_cog(Blacklist(bot))
