import logging
from collections import defaultdict
from typing import Any

import aiosqlite
import discord
from discord.ext import commands
from jishaku.features import sql

from . import db


log = logging.getLogger(__name__)


@sql.adapter(aiosqlite.Connection)
class AiosqliteConnectionAdapter(sql.Adapter[aiosqlite.Connection]):
    connection: aiosqlite.Connection

    def info(self) -> str:
        return f"aiosqlite {aiosqlite.__version__} Connection"

    async def fetchrow(self, query: str) -> dict[str, Any]:
        row = await (await self.connector.execute(query)).fetchone()
        return dict(row) if row else None  # type: ignore

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        return [dict(row) async for row in await self.connector.execute(query)]

    async def execute(self, query: str) -> str:
        return str((await self.connector.execute(query)).rowcount)

    async def table_summary(self, table_query: str | None) -> dict[str, dict[str, str]]:
        tables = defaultdict(dict)

        if table_query:
            names = [table_query]
        else:
            names = [name async for name, in await self.connector.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]

        for name in names:
            async for row in await self.connector.execute("SELECT name, type, `notnull`, dflt_value, pk FROM pragma_table_info(?)", (name,)):
                tables[name][row["name"]] = self.format_column_row(row)

        return tables

    def format_column_row(self, row: aiosqlite.Row) -> str:
        not_null = " NOT NULL"*row["notnull"]
        default = row["dflt_value"]
        default_value = f" DEFAULT {default}" if default else ""
        primary_key = " PRIMARY KEY"*bool(row["pk"])
        return f"{row['type']}{not_null}{default_value}{primary_key}"


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
        elif isinstance(error, commands.BadFlagArgument):
            await ctx.send(str(error.original))
        elif isinstance(error, commands.BadUnionArgument):
            errors = [str(e) for e in error.errors if not isinstance(e, commands.BadLiteralArgument)]
            await ctx.send("\n".join(errors))
        elif isinstance(error, commands.UserInputError):
            await ctx.send(str(error))

    async def setup_hook(self) -> None:
        self.db = await db.connect("the.db")
        for extension in extensions:
            await self.load_extension(extension, package=__name__)

    async def close(self) -> None:
        await self.db.close()
        await super().close()
