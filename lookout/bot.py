import logging

import discord
from discord.ext import commands

from . import db


log = logging.getLogger(__name__)

extensions = [
    "jishaku",
    "..blacklist",
    "..logs",
    "..stats",
    "..search",
    "..gaming",
]


class Lookout(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="lo!",
            description="Official bot of the TT server, by LyricLy",
            allowed_mentions=discord.AllowedMentions.none(),
            intents=discord.Intents(
                guilds=True,
                messages=True,
                members=True,
                message_content=True,
            ),
            max_messages=None,  # type: ignore
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.CommandInvokeError, commands.ConversionError)):
            assert ctx.command is not None
            log.exception("In %s:", ctx.command.qualified_name, exc_info=error.original)
            await ctx.send("Unknown error occurred.")

    async def setup_hook(self) -> None:
        self.db = await db.connect("the.db")
        for extension in extensions:
            await self.load_extension(extension, package=__name__)

    async def close(self) -> None:
        await self.db.close()
        await super().close()
