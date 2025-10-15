from __future__ import annotations

import discord
from discord.ext import commands

import config
from .bot import Lookout


class BXO(commands.Cog):
    """Things BXO asked me to do."""

    def __init__(self, bot: Lookout):
        self.bot = bot

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id == config.media_channel_id:
            await self.bot.get_partial_messageable(config.uploads_channel_id).send(config.upload_message.format(post=thread.mention))


async def setup(bot: Lookout):
    await bot.add_cog(BXO(bot))
