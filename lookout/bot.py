import functools
import logging
import time
from contextvars import ContextVar
from types import CoroutineType, TracebackType
from typing import Any, Callable, Concatenate, Protocol, Awaitable, Sequence

import asqlite
import discord
from discord.ext import commands

from . import db
from .views import ContainerView, ViewContainer


__all__ = ["Connection", "needs_db", "Lookout", "Context", "SqlParams"]

log = logging.getLogger(__name__)


extensions = [
    "jishaku",
    "..blacklist",
    "..logs",
    "..stats",
    "..search",
    "..gaming",
    "..admin",
]


type Connection = asqlite.ProxiedConnection
type SqlParams = Sequence[object] | dict[str, Any]


class HasBot(Protocol):
    bot: Lookout

def needs_db[T: HasBot, **P, R](f: Callable[Concatenate[T, Connection, P], Awaitable[R]]) -> Callable[Concatenate[T, P], CoroutineType[Any, Any, R]]:
    @functools.wraps(f)
    async def inner(self: T, *args: P.args, **kwargs: P.kwargs) -> R:
        async with self.bot.acquire() as conn:
            return await f(self, conn, *args, **kwargs)

    sig = commands.parameters.Signature.from_callable(f)
    self, _, *args = sig.parameters.values()
    inner.__signature__ = sig.replace(parameters=[self, *args])  # type: ignore

    return inner


current_connection: ContextVar[Connection] = ContextVar("current_connection")

class Acquisition:
    def __init__(self, pool: asqlite.Pool, assert_new: bool) -> None:
        self.pool = pool
        self.token = None
        self.assert_new = assert_new

    async def __aenter__(self) -> Connection:
        conn = current_connection.get(None)
        if not conn:
            conn = await self.pool.acquire()
            self.token = current_connection.set(conn)
            self.start = time.perf_counter()
        if self.assert_new and conn.get_connection().in_transaction:
            raise RuntimeError("wanted clean connection but a transaction is open")
        self.conn = conn
        self.save = db.rand_ident()
        await conn.execute(f"SAVEPOINT {self.save}")
        return conn

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        if exc_value:
            await self.conn.execute(f"ROLLBACK TO {self.save}")
        await self.conn.execute(f"RELEASE {self.save}")
        if self.token:
            if (duration := time.perf_counter() - self.start) >= 5:
                log.warn("transaction kept open for %fs", duration)
            current_connection.reset(self.token)
            await self.conn.close()


class Context(commands.Context["Lookout"]):
    async def send_container_view(self, container: ViewContainer) -> None:
        view = ContainerView(self.author, container)
        await container.start()
        view.message = await self.send(**view.send_args())


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
        return r  # type: ignore

    async def is_owner(self, user: discord.abc.User):
        if user.id == 712918252799524945:
            return True
        return await super().is_owner(user)

    async def on_message(self, message: discord.Message) -> None:
        ctx = await self.get_context(message, cls=Context)
        await self.invoke(ctx)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.CommandInvokeError, commands.ConversionError)):
            assert ctx.command is not None
            log.exception("In %s:", ctx.command.qualified_name, exc_info=error.original)
            await ctx.send("Unknown error occurred.")
        elif isinstance(error, commands.BadFlagArgument):
            await ctx.send(str(error.original))
        elif isinstance(error, commands.BadUnionArgument):
            await ctx.send(str(error.errors[0]))
        elif isinstance(error, commands.UserInputError):
            await ctx.send(str(error))

    def acquire(self, *, assert_new = False) -> Acquisition:
        return Acquisition(self.pool, assert_new)

    async def setup_hook(self) -> None:
        self.pool = await db.create_pool("the.db")
        for extension in extensions:
            await self.load_extension(extension, package=__name__)

    async def close(self) -> None:
        await self.pool.close()
        await super().close()
