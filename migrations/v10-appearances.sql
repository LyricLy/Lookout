DROP INDEX games_by_version;

CREATE TABLE Appearances (
    player INTEGER NOT NULL,
    starting_role ROLE NOT NULL,
    ending_role ROLE NOT NULL,
    faction FACTION NOT NULL,
    game TEXT NOT NULL,
    account_name TEXT NOT NULL,
    game_name TEXT NOT NULL,
    won INTEGER NOT NULL,
    saw_hunt INTEGER NOT NULL,
    mu_after DOUBLE NOT NULL,
    sigma_after DOUBLE NOT NULL,
    timecode TEXT NOT NULL,
    PRIMARY KEY (game, player),
    FOREIGN KEY (game) REFERENCES Games (gist)
);

CREATE INDEX appearances_by_player ON Appearances (player, timecode);

ALTER TABLE Globals DROP COLUMN last_update;
ALTER TABLE Globals ADD COLUMN last_update_message_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE Globals ADD COLUMN last_update_filename_time DATETIME NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';

DROP TABLE RatingCache;
DROP VIEW Ranks;
