"""Adds the "channel_id", "filename_time", and "game" columns to Gamelogs, and the "first_log" column to Games."""

import aiosqlite
import gamelogs

import config
from lookout.logs import datetime_of_filename, gist_of


async def migrate(db: aiosqlite.Connection):
    # here goes
    await db.executescript("""
        CREATE TABLE NewGamelogs (
            hash TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            attachment_id INTEGER NOT NULL,
            filename_time DATETIME,
            uploader INTEGER NOT NULL,
            clean_content TEXT NOT NULL,
            game TEXT,
            FOREIGN KEY (game) REFERENCES NewGames (gist)
        );

        CREATE TABLE NewGames (
            gist TEXT PRIMARY KEY,
            from_log TEXT UNIQUE NOT NULL,
            first_log TEXT UNIQUE NOT NULL,
            message_count INTEGER NOT NULL,
            analysis GAME NOT NULL,
            analysis_version INTEGER NOT NULL,
            FOREIGN KEY (from_log) REFERENCES NewGamelogs (hash),
            FOREIGN KEY (first_log) REFERENCES NewGamelogs (hash)
        );
    """)

    counts = {}

    async with db.execute("SELECT * FROM Gamelogs ORDER BY message_id") as cur:
        async for row in cur:
            await db.execute("INSERT INTO NewGamelogs (hash, filename, channel_id, message_id, attachment_id, filename_time, uploader, clean_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                row["hash"],
                row["filename"],
                config.guess_channel_id(row["message_id"]),
                row["message_id"],
                row["attachment_id"],
                datetime_of_filename(row["filename"]),
                row["uploader"],
                row["clean_content"],
            ))

            try:
                game, message_count = gamelogs.parse(row["clean_content"], gamelogs.ResultAnalyzer() & gamelogs.MessageCountAnalyzer(), clean_tags=False)
            except gamelogs.BadLogError:
                continue

            if game.modifiers != ["Town Traitor"] or any(gamelogs.bucket_of[player.ending_ident.role].startswith("Neutral") for player in game.players):
                continue

            gist = gist_of(game)
            game_row = gist, row["hash"], message_count, game, gamelogs.version
            if not (c := counts.get(gist)):
                await db.execute("INSERT INTO NewGames (gist, from_log, first_log, message_count, analysis, analysis_version) VALUES (?1, ?2, ?2, ?3, ?4, ?5)", game_row)
                counts[gist] = message_count
            elif message_count >= c:
                await db.execute("UPDATE NewGames SET from_log = ?2, message_count = ?3, analysis = ?4, analysis_version = ?5 WHERE gist = ?1", game_row)
                counts[gist] = message_count

            await db.execute("UPDATE NewGamelogs SET game = ? WHERE hash = ?", (gist, row["hash"]))

    await db.executescript("""
        DROP TABLE Gamelogs;
        DROP TABLE Games;
        ALTER TABLE NewGamelogs RENAME TO Gamelogs;
        ALTER TABLE NewGames RENAME TO Games;

        CREATE INDEX logs_by_message_id ON Gamelogs (message_id);
        CREATE INDEX games_by_version ON Games (analysis_version);
    """)
