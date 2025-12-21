CREATE TABLE RatingCache (
    player INTEGER PRIMARY KEY,
    mu REAL NOT NULL,
    sigma REAL NOT NULL,
    winrates MSGPACK NOT NULL,
    ordinal AS (mu - 3.0 * sigma) STORED
);

CREATE INDEX by_ordinal ON RatingCache (ordinal);

CREATE TABLE Globals (
    unit INTEGER PRIMARY KEY CHECK (unit = 1),
    last_update DATETIME NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'
);

INSERT INTO Globals DEFAULT VALUES;

CREATE VIEW Ranks AS SELECT
    player,
    rank() OVER (ORDER BY ordinal DESC) as rank
FROM RatingCache WHERE NOT EXISTS(SELECT 1 FROM Hidden WHERE player = RatingCache.player);
