from typing import Self, Protocol, TYPE_CHECKING

import discord
from discord.ext import commands

from .bot import *
from .player_info import PlayerInfo, PlayerRating, RATINGS, model
from .specifiers import IdentitySpecifier
from .timecode import Timecode
from .winrate import Winrate


class DisplayablePlayer(Protocol):
    async def names(self) -> list[str]: ...
    async def user(self) -> discord.User | None: ...


class ReglePlayerInfo:
    def __init__(self, user: discord.User) -> None:
        self._user = user

    async def names(self) -> list[str]:
        r = [self._user.name, str(self._user.id)]
        if self._user.global_name:
            r.append(self._user.global_name)
        return r

    async def user(self) -> discord.User | None:
        return self._user


class Key(Protocol):
    def __lt__(self, other: Self, /) -> bool: ...
    def __le__(self, other: Self, /) -> bool: ...
    def __ge__(self, other: Self, /) -> bool: ...
    def __gt__(self, other: Self, /) -> bool: ...


class Criterion[K: Key]:
    def desc(self) -> str | None:
        raise NotImplementedError

    def show_key(self, key: K) -> str:
        raise NotImplementedError

    async def decorate_players(self, at: Timecode) -> list[tuple[DisplayablePlayer, K]]:
        raise NotImplementedError

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        leaves = cls.__subclasses__()
        errors = []

        for leaf in leaves:
            try:
                return await leaf.convert(ctx, argument)
            except commands.BadArgument as e:
                errors.append(e)

        raise commands.BadUnionArgument(
            ctx.current_parameter,  # type: ignore
            tuple(leaves),
            errors,
        )


class RatingCriterion(Criterion[float]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return None

    def show_key(self, key: float) -> str:
        return f"{key:.0f}"

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, float]]:
        return [
            (PlayerInfo(player, self.bot), PlayerRating(model.rating(mu, sigma), at, self.bot).ordinal())
            for player, mu, sigma in await conn.fetchall(f"SELECT player, mu, sigma FROM {RATINGS} WHERE NOT EXISTS(SELECT 1 FROM Hidden WHERE player = Ratings.player)", (at,))
        ]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() != "rating":
            raise commands.BadArgument()
        return cls(ctx.bot)


HIDDEN_CLAUSE = "NOT EXISTS(SELECT 1 FROM Hidden WHERE player = Appearances.player) AND "


class WinrateCriterion(Criterion[Winrate]):
    def __init__(self, bot: Lookout, spec: IdentitySpecifier) -> None:
        self.bot = bot
        self.spec = spec

    def desc(self) -> str | None:
        n = self.spec.desc()
        if not n:
            return None

        return f"winrate{' in hunt'*bool(self.spec.hunt)} as {n} (ordered by lower bound)"

    def show_key(self, key: Winrate) -> str:
        return str(key)

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, Winrate]]:
        # NOTE: `at` is ignored currently
        c, p = self.spec.to_sql()
        rs = await conn.fetchall(f"SELECT player, COALESCE(SUM(won), 0), COUNT(*) FROM Appearances WHERE {HIDDEN_CLAUSE}{c} GROUP BY player", p)
        return [(PlayerInfo(player, self.bot), Winrate(s, n)) for player, s, n in rs]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() == "overall":
            return cls(ctx.bot, IdentitySpecifier())
        spec = await IdentitySpecifier.convert(ctx, argument)
        if spec.won is not None:
            raise commands.BadArgument()
        return cls(ctx.bot, spec)


class GamesPlayedCriterion(Criterion[int]):
    def __init__(self, bot: Lookout, spec: IdentitySpecifier) -> None:
        self.bot = bot
        self.spec = spec

    def desc(self) -> str | None:
        n = self.spec.desc()
        if not n:
            return None

        games = "hunts" if self.spec.hunt else "games"
        played = {True: "won", False: "lost", None: "played"}[self.spec.won]
        return f"number of {games} {played} as {n}"

    def show_key(self, key: int) -> str:
        return f"{key:,}"

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, int]]:
        # NOTE: `at` is ignored currently
        c, p = self.spec.to_sql()
        rs = await conn.fetchall(f"SELECT player, COUNT(*) FROM Appearances WHERE {HIDDEN_CLAUSE*(self.spec.won is not None)}{c} GROUP BY player", p)
        return [(PlayerInfo(player, self.bot), n) for player, n in rs]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        is_played = True
        argument = argument.lower()
        if argument.startswith("played"):
            argument = argument.removeprefix("played")
        elif argument.endswith("played"):
            argument = argument.removesuffix("played")
        else:
            is_played = False

        spec = await IdentitySpecifier.convert(ctx, argument)
        if not is_played and spec.won is None:
            raise commands.BadArgument()
        return cls(ctx.bot, spec)


class RegleWinrateCriterion(Criterion[Winrate]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "winrate in Regle"

    def show_key(self, key: Winrate) -> str:
        return str(key)

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, Winrate]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COALESCE(SUM(guessed = correct), 0), COUNT(*) FROM RegleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), Winrate(s, n)) for player_id, s, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() != "regle":
            raise commands.BadArgument()
        return cls(ctx.bot)


class RegleGamesPlayedCriterion(Criterion[int]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "number of games played of Regle"

    def show_key(self, key: int) -> str:
        return f"{key:,}"

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, int]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COUNT(*) FROM RegleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), n) for player_id, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() not in ["played regle", "regle played"]:
            raise commands.BadArgument()
        return cls(ctx.bot)


class WilleWinrateCriterion(Criterion[Winrate]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "winrate in Wille"

    def show_key(self, key: Winrate) -> str:
        return str(key)

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, Winrate]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COALESCE(SUM(guessed = correct), 0), COUNT(*) FROM WilleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), Winrate(s, n)) for player_id, s, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() != "wille":
            raise commands.BadArgument()
        return cls(ctx.bot)


class WilleGamesPlayedCriterion(Criterion[int]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "number of games played of Wille"

    def show_key(self, key: int) -> str:
        return f"{key:,}"

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, int]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COUNT(*) FROM WilleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), n) for player_id, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() not in ["played wille", "wille played"]:
            raise commands.BadArgument()
        return cls(ctx.bot)


class LogleWinrateCriterion(Criterion[Winrate]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "winrate in Logle"

    def show_key(self, key: Winrate) -> str:
        return str(key)

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, Winrate]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COALESCE(SUM((guessed = correct) * (2*num_targets-1)), 0), COUNT(*) FROM LogleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), Winrate(s, n)) for player_id, s, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() != "logle":
            raise commands.BadArgument()
        return cls(ctx.bot)


class LogleGamesPlayedCriterion(Criterion[int]):
    def __init__(self, bot: Lookout) -> None:
        self.bot = bot

    def desc(self) -> str | None:
        return "number of games played of Logle"

    def show_key(self, key: int) -> str:
        return f"{key:,}"

    @needs_db
    async def decorate_players(self, conn: Connection, at: Timecode) -> list[tuple[DisplayablePlayer, int]]:
        # `at` is unused
        rs = await conn.fetchall("SELECT player_id, COUNT(*) FROM LogleGames GROUP BY player_id")
        return [(ReglePlayerInfo(player), n) for player_id, n in rs if (player := self.bot.get_user(player_id))]

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        if argument.lower() not in ["played logle", "logle played"]:
            raise commands.BadArgument()
        return cls(ctx.bot)
