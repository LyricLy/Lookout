CREATE TABLE NewAppearances (
    player INTEGER NOT NULL,
    starting_role ROLE NOT NULL,
    ending_role ROLE NOT NULL,
    faction FACTION NOT NULL,
    game TEXT NOT NULL,
    account_name TEXT NOT NULL COLLATE NOCASE,
    game_name TEXT NOT NULL,
    won INTEGER NOT NULL,
    saw_hunt INTEGER NOT NULL,
    mu_after DOUBLE NOT NULL,
    sigma_after DOUBLE NOT NULL,
    timecode TEXT NOT NULL,
    PRIMARY KEY (game, player),
    FOREIGN KEY (game) REFERENCES Games (gist)
);
INSERT INTO NewAppearances SELECT * FROM Appearances;
DROP TABLE Appearances;
ALTER TABLE NewAppearances RENAME TO Appearances;

CREATE INDEX appearances_by_player ON Appearances (player, timecode);
