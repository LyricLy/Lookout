CREATE TABLE Names (
    name TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
    player INTEGER NOT NULL
);

CREATE INDEX names_of_player ON Names (player);

INSERT INTO Names SELECT DISTINCT dst, dense_rank() OVER (ORDER BY dst) FROM Aliases;
INSERT INTO Names SELECT src, player FROM Aliases INNER JOIN Names ON name = dst;
DROP TABLE Aliases;

CREATE TABLE DiscordConnections (
    discord_id INTEGER PRIMARY KEY,
    player INTEGER NOT NULL UNIQUE
);

ALTER TABLE Blacklists RENAME TO OldBlacklists;
CREATE TABLE Blacklists (
    thread_id INTEGER NOT NULL,
    account_name TEXT NOT NULL COLLATE NOCASE,
    reason TEXT
);
INSERT INTO Blacklists SELECT * FROM OldBlacklists;
DROP TABLE OldBlacklists;
