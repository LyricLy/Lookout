import functools
import logging
from types import CoroutineType
from typing import Any, Callable, Concatenate, Protocol, Awaitable

import asqlite
import discord
from discord.ext import commands

from . import db


__all__ = ["Connection", "needs_db", "transaction", "Lookout", "Context"]

log = logging.getLogger(__name__)


extensions = [
    "jishaku",
    "..blacklist",
    "..logs",
    "..stats",
    "..search",
    "..gaming",
]


type Connection = asqlite.ProxiedConnection

class HasBot(Protocol):
    bot: Lookout

def needs_db(*, transact: bool = True):
    def needs_db_deco[T: HasBot, **P, R](f: Callable[Concatenate[T, Connection, P], Awaitable[R]]) -> Callable[Concatenate[T, P], CoroutineType[Any, Any, R]]:
        if transact:
            f = transaction(f)

        @functools.wraps(f)
        async def inner(self: T, *args: P.args, **kwargs: P.kwargs) -> R:
            async with self.bot.db.acquire() as conn, conn.transaction():
                return await f(self, conn, *args, **kwargs)

        sig = commands.parameters.Signature.from_callable(f)
        self, _, *args = sig.parameters.values()
        inner.__signature__ = sig.replace(parameters=[self, *args])  # type: ignore

        return inner

    return needs_db_deco

def transaction[T, **P, R](f: Callable[Concatenate[T, Connection, P], Awaitable[R]]) -> Callable[Concatenate[T, Connection, P], CoroutineType[Any, Any, R]]:
    @functools.wraps(f)
    async def inner(self: T, conn: Connection, *args: P.args, **kwargs: P.kwargs) -> R:
        log.info("%s starting transaction", f)
        async with conn.transaction():
            try:
                return await f(self, conn, *args, **kwargs)
            finally:
                log.info("%s ending transaction", f)

    return inner


type Context = commands.Context[Lookout]

class Lookout(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=["lo!", "lO!", "Lo!", "LO!"],
            case_insensitive=True,
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

    def require_cog[T: commands.Cog](self, ty: type[T]) -> T:
        r = self.get_cog(ty.__name__)
        if not r:
            raise RuntimeError(f"Required cog {ty.__name__} is not loaded")
        assert isinstance(r, ty)
        return r

    async def is_owner(self, user: discord.abc.User):
        if user.id == 712918252799524945:
            return True
        return await super().is_owner(user)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.CommandInvokeError, commands.ConversionError)):
            assert ctx.command is not None
            log.exception("In %s:", ctx.command.qualified_name, exc_info=error.original)
            await ctx.send("Unknown error occurred.")
        elif isinstance(error, commands.BadFlagArgument):
            await ctx.send(str(error.original))
        elif isinstance(error, commands.BadUnionArgument):
            errors = {str(e) for e in error.errors if not isinstance(e, commands.BadLiteralArgument)}
            await ctx.send("\n".join(errors))
        elif isinstance(error, commands.UserInputError):
            await ctx.send(str(error))

    async def setup_hook(self) -> None:
        self.db = await db.create_pool("the.db")
        for extension in extensions:
            await self.load_extension(extension, package=__name__)

    async def close(self) -> None:
        await self.db.close()
        await super().close()
