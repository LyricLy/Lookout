import datetime
import os
import re
import logging
import importlib
from sqlite3 import PARSE_DECLTYPES
from typing import TypedDict, Callable, Awaitable

import aiosqlite
import sqlite_spellfix
import gamelogs
import msgpack


log = logging.getLogger(__name__)


class JIdentity(TypedDict):
    role: str
    faction: str | None

JDayTime = tuple[str, int]

class JPlayer(TypedDict):
    number: int
    game_name: str
    account_name: str
    starting_ident: JIdentity
    ending_ident: JIdentity
    died: JDayTime | None
    won: bool 

class JGameResult(TypedDict):
    players: list[JPlayer]
    victor: str | None
    hunt_reached: int | None
    modifiers: list[str]
    vip: int | None
    ended: JDayTime
    outcome: int


def ser_day_time(daytime: gamelogs.DayTime) -> JDayTime:
    return (daytime.time.name.lower(), daytime.day)

def ser_faction(faction: gamelogs.Faction | None) -> str | None:
    return repr(faction) if faction else None

def ser_ident(ident: gamelogs.Identity) -> JIdentity:
    return {
        "role": ident.role.name,
        "faction": repr(ident.faction) if ident.faction else None,
    }

def ser_player(player: gamelogs.Player) -> JPlayer:
    return {
        "number": player.number,
        "game_name": player.game_name,
        "account_name": player.account_name,
        "starting_ident": ser_ident(player.starting_ident),
        "ending_ident": ser_ident(player.ending_ident),
        "died": ser_day_time(player.died) if player.died else None,
        "won": player.won,
    }

def ser_game_result(game: gamelogs.GameResult) -> JGameResult:
    return {
        "players": [ser_player(p) for p in game.players],
        "victor": repr(game.victor) if game.victor else None,
        "hunt_reached": game.hunt_reached.day if game.hunt_reached else None,
        "modifiers": game.modifiers,
        "vip": game.vip.number - 1 if game.vip else None,
        "ended": ser_day_time(game.ended),
        "outcome": game.outcome.value,
    }


def de_day_time(daytime: JDayTime) -> gamelogs.DayTime:
    return gamelogs.DayTime(daytime[1], gamelogs.Time[daytime[0].upper()])

def de_faction(faction: str | None) -> gamelogs.Faction | None:
    return getattr(gamelogs, faction) if faction else None  # type: ignore

def de_ident(ident: JIdentity) -> gamelogs.Identity:
    return gamelogs.Identity(
        gamelogs.by_name[ident["role"]],
        de_faction(ident["faction"]),
    )

def de_player(player: JPlayer) -> gamelogs.Player:
    return gamelogs.Player(
        player["number"],
        player["game_name"],
        player["account_name"],
        de_ident(player["starting_ident"]),
        de_ident(player["ending_ident"]),
        de_day_time(player["died"]) if player["died"] else None,
        player["won"],
    )

def de_game_result(game: JGameResult) -> gamelogs.GameResult:
    return gamelogs.GameResult(
        (players := tuple(map(de_player, game["players"]))),
        de_faction(game["victor"]),
        gamelogs.DayTime(game["hunt_reached"]) if game["hunt_reached"] else None,
        game["modifiers"],
        players[game["vip"]] if game["vip"] is not None else None,
        de_day_time(game["ended"]),
        gamelogs.Outcome(outcome) if (outcome := game.get("outcome")) else gamelogs.Outcome.NORMAL,
    )


VALID_MIGRATION_REGEX = re.compile(r"(v(\d+).*)\.(sql|py)")
MIGRATION_DIR = "migrations"
type Migration = Callable[[aiosqlite.Connection], Awaitable[None]]

def get_migration(name: str) -> tuple[int, Migration] | None:
    m = VALID_MIGRATION_REGEX.fullmatch(name)
    if not m:
        return None
    if m[3] == "sql":
        async def migrate(db: aiosqlite.Connection) -> None:
            with open(f"{MIGRATION_DIR}/{name}") as f:
                script = f.read()
            await db.executescript(script)
    else:
        migrate = importlib.import_module(f"{MIGRATION_DIR}.{m[1]}").migrate
    return int(m[2]), migrate

def get_migrations() -> list[Migration]:
    l = [m for p in os.listdir(MIGRATION_DIR) if (m := get_migration(p))]
    l.sort(key=lambda t: t[0])
    return [x for _, x in l]


async def connect(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path, detect_types=PARSE_DECLTYPES, autocommit=False)
    db.row_factory = aiosqlite.Row
    await db.enable_load_extension(True)
    await db.load_extension(sqlite_spellfix.extension_path())  # type: ignore

    aiosqlite.register_adapter(gamelogs.GameResult, lambda game: msgpack.packb(ser_game_result(game)))
    aiosqlite.register_converter("GAME", lambda data: de_game_result(msgpack.unpackb(data)))
    aiosqlite.register_adapter(gamelogs.Role, lambda role: role.name)
    aiosqlite.register_converter("ROLE", lambda s: gamelogs.by_name[s.decode()])
    aiosqlite.register_adapter(gamelogs.Faction, ser_faction)
    aiosqlite.register_converter("FACTION", lambda s: de_faction(s.decode()))
    aiosqlite.register_adapter(dict, msgpack.packb)
    aiosqlite.register_converter("MSGPACK", msgpack.unpackb)
    aiosqlite.register_adapter(datetime.datetime, datetime.datetime.isoformat)
    aiosqlite.register_converter("DATETIME", lambda s: datetime.datetime.fromisoformat(s.decode()))

    current_version, = await (await db.execute("PRAGMA user_version")).fetchone()  # type: ignore
    migrations = get_migrations()
    for n in range(current_version, len(migrations)):
        log.info("migrating database from version %d to %d", n, n+1)
        await db.executescript("COMMIT; PRAGMA foreign_keys = OFF; BEGIN")

        try:
            await migrations[n](db)
        except aiosqlite.Error as e:
            raise RuntimeError(f"failed to migrate database from version {n} to {n+1}") from e

        if await (await db.execute("PRAGMA foreign_key_check")).fetchone():
            raise RuntimeError(f"foreign key violation when migrating database from version {n} to {n+1}: {r[0]} has a dangling reference to {r[2]} (rowid {r[1]}, constraint {r[3]})")

        await db.execute(f"PRAGMA user_version = {n + 1}")
        await db.commit()

    await db.executescript("""
        COMMIT;
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = WAL;
        PRAGMA busy_timeout = 5000;
        PRAGMA synchronous = NORMAL;
        PRAGMA cache_size = -128000;
        BEGIN DEFERRED
    """)
    log.info("database connected")
    return db
