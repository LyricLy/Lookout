import datetime
import os
import uuid
import re
import sqlite3
import logging
import importlib
from typing import TypedDict, Callable, Awaitable, Literal, assert_never

import asqlite
import sqlite_spellfix
import gamelogs
import msgpack

from .timecode import Timecode


log = logging.getLogger(__name__)


class JIdentity(TypedDict):
    role: str
    faction: str | None
    tt: bool

JDayTime = tuple[str, int]

class JProsecution(TypedDict):
    ty: Literal["prosecution"]

class JTribunal(TypedDict):
    ty: Literal["tribunal"]

class JVote(TypedDict):
    ty: Literal["vote"]
    guilty: int
    innocent: int

JHangCause = JProsecution | JTribunal | JVote

class JPlayer(TypedDict):
    number: int
    game_name: str
    account_name: str
    starting_ident: JIdentity
    ending_ident: JIdentity
    will: str | None
    died: JDayTime | None
    won: bool
    hanged: JHangCause | None
    dced: bool

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

def ser_hang_cause(cause: gamelogs.HangCause) -> JHangCause:
    match cause:
        case gamelogs.Prosecution():
            return {"ty": "prosecution"}
        case gamelogs.Tribunal():
            return {"ty": "tribunal"}
        case gamelogs.Vote(guilty, innocent):
            return {"ty": "vote", "guilty": guilty, "innocent": innocent}
        case u:
            assert_never(u)

def ser_faction(faction: gamelogs.Faction | None) -> str | None:
    return repr(faction) if faction else None

def ser_ident(ident: gamelogs.Identity) -> JIdentity:
    return {
        "role": ident.role.name,
        "faction": repr(ident.faction) if ident.faction else None,
        "tt": ident.tt,
    }

def ser_player(player: gamelogs.Player) -> JPlayer:
    return {
        "number": player.number,
        "game_name": player.game_name,
        "account_name": player.account_name,
        "starting_ident": ser_ident(player.starting_ident),
        "ending_ident": ser_ident(player.ending_ident),
        "will": player.will,
        "died": ser_day_time(player.died) if player.died else None,
        "won": player.won,
        "hanged": ser_hang_cause(player.hanged) if player.hanged else None,
        "dced": player.dced,
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

def de_hang_cause(cause: JHangCause | bool) -> gamelogs.HangCause:
    match cause:
        case {"ty": "prosecution"}:
            return gamelogs.Prosecution()
        case {"ty": "tribunal"}:
            return gamelogs.Tribunal()
        case {"ty": "vote", "guilty": guilty, "innocent": innocent}:
            return gamelogs.Vote(guilty, innocent)
        case _:
            return gamelogs.Vote(0, 0)

def de_faction(faction: str | None) -> gamelogs.Faction | None:
    return getattr(gamelogs, faction) if faction else None  # type: ignore

def de_ident(ident: JIdentity) -> gamelogs.Identity:
    return gamelogs.Identity(
        role := gamelogs.by_name(ident["role"]),
        faction := de_faction(ident["faction"]),
        ident.get("tt", role.default_faction == gamelogs.town and faction == gamelogs.coven),
    )

def de_player(player: JPlayer) -> gamelogs.Player:
    return gamelogs.Player(
        player["number"],
        player["game_name"],
        player["account_name"],
        de_ident(player["starting_ident"]),
        de_ident(player["ending_ident"]),
        player.get("will"),
        de_day_time(player["died"]) if player["died"] else None,
        player["won"],
        de_hang_cause(hanged) if (hanged := player.get("hanged")) else None,
        player.get("dced", False),
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
type Migration = Callable[[asqlite.Connection], Awaitable[None]]

def get_migration(name: str) -> tuple[int, Migration] | None:
    m = VALID_MIGRATION_REGEX.fullmatch(name)
    if not m:
        return None
    if m[3] == "sql":
        async def migrate(db: asqlite.Connection) -> None:
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


def init(db: sqlite3.Connection):
    db.enable_load_extension(True)
    db.load_extension(sqlite_spellfix.extension_path())  # type: ignore
    sqlite3.register_adapter(gamelogs.GameResult, lambda game: msgpack.packb(ser_game_result(game)))
    sqlite3.register_converter("GAME", lambda data: de_game_result(msgpack.unpackb(data)))
    sqlite3.register_adapter(gamelogs.Role, lambda role: role.name)
    sqlite3.register_converter("ROLE", lambda s: gamelogs.by_name(s.decode()))
    sqlite3.register_adapter(gamelogs.Faction, ser_faction)
    sqlite3.register_converter("FACTION", lambda s: de_faction(s.decode()))
    sqlite3.register_adapter(dict, msgpack.packb)
    sqlite3.register_adapter(list, msgpack.packb)
    sqlite3.register_converter("MSGPACK", msgpack.unpackb)
    sqlite3.register_adapter(datetime.datetime, datetime.datetime.isoformat)
    sqlite3.register_converter("DATETIME", lambda s: datetime.datetime.fromisoformat(s.decode()))
    sqlite3.register_adapter(Timecode, Timecode.to_str)
    sqlite3.register_converter("TIMECODE", lambda s: Timecode.from_str(s.decode()))
    db.execute("PRAGMA synchronous = NORMAL")

async def create_pool(path: str) -> asqlite.Pool:
    db = await asqlite.create_pool(path, init=init, timeout=30, detect_types=asqlite.PARSE_DECLTYPES)

    async with db.acquire() as conn:
        current_version, = await conn.fetchone("PRAGMA user_version")  # type: ignore
        migrations = get_migrations()
        for n in range(current_version, len(migrations)):
            log.info("migrating database from version %d to %d", n, n+1)
            await conn.executescript("PRAGMA foreign_keys = OFF; BEGIN")

            try:
                await migrations[n](conn)
            except sqlite3.Error as e:
                raise RuntimeError(f"failed to migrate database from version {n} to {n+1}") from e

            if r := await conn.fetchone("PRAGMA foreign_key_check"):
                raise RuntimeError(f"foreign key violation when migrating database from version {n} to {n+1}: {r[0]} has a dangling reference to {r[2]} (rowid {r[1]}, constraint {r[3]})")

            await conn.execute(f"PRAGMA user_version = {n + 1}")
            await conn.commit()

    log.info("database connected")
    return db


def rand_ident() -> str:
    return f"_{uuid.uuid4().hex}"
