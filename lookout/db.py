from __future__ import annotations

import datetime
import pathlib
import re
from sqlite3 import PARSE_DECLTYPES
from typing import TypedDict, Literal

import aiosqlite
import msgpack

import gamelogs


class JIdentity(TypedDict):
    role: str
    faction: str | None

# reversed froom gamelogs.Time
Time = tuple[Literal["day", "night"], int]

class JPlayer(TypedDict):
    number: int
    game_name: str
    account_name: str
    starting_ident: JIdentity
    ending_ident: JIdentity
    died: Time | None
    won: bool 

class JGameResult(TypedDict):
    players: list[JPlayer]
    victor: str | None
    hunt_reached: int | None
    modifiers: list[str]
    vip: int | None
    ended: Time


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
        "died": (player.died[1], player.died[0]) if player.died else None,
        "won": player.won,
    }

def ser_game_result(game: gamelogs.GameResult) -> JGameResult:
    return {
        "players": [ser_player(p) for p in game.players],
        "victor": repr(game.victor) if game.victor else None,
        "hunt_reached": game.hunt_reached,
        "modifiers": game.modifiers,
        "vip": game.vip.number - 1 if game.vip else None,
        "ended": (game.ended[1], game.ended[0]),
    }


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
        (player["died"][1], player["died"][0]) if player["died"] else None,
        player["won"],
    )

def de_game_result(game: JGameResult) -> gamelogs.GameResult:
    return gamelogs.GameResult(
        (players := tuple(map(de_player, game["players"]))),
        de_faction(game["victor"]),
        game["hunt_reached"],
        game["modifiers"],
        players[game["vip"]] if game["vip"] is not None else None,
        (game["ended"][1], game["ended"][0]),
    )


VALID_MIGRATION_REGEX = re.compile(r"v(\d+).*\.sql")
MIGRATION_PATH = pathlib.Path("migrations")

def get_migrations() -> list[pathlib.Path]:
    l = [(int(m[1]), p) for p in MIGRATION_PATH.iterdir() if (m := VALID_MIGRATION_REGEX.fullmatch(p.name))]
    l.sort(key=lambda t: t[0])
    return [x for _, x in l]

async def connect(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path, detect_types=PARSE_DECLTYPES)
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row

    aiosqlite.register_adapter(gamelogs.GameResult, lambda game: msgpack.packb(ser_game_result(game)))
    aiosqlite.register_converter("GAME", lambda data: de_game_result(msgpack.unpackb(data)))

    current_version, = await (await db.execute("PRAGMA user_version")).fetchone()  # type: ignore
    migrations = get_migrations()
    for n in range(current_version, len(migrations)):
        with open(migrations[n]) as f:
            script = f.read()
        try:
            await db.executescript(f"BEGIN; PRAGMA defer_foreign_keys = true; {script} COMMIT;")
        except aiosqlite.Error as e:
            raise RuntimeError(f"failed to migrate database from version {n} to {n+1}") from e
        else:
            await db.execute(f"PRAGMA user_version = {n + 1}")
            await db.commit()

    return db
