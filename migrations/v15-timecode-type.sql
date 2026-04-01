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
    timecode TIMECODE NOT NULL,
    PRIMARY KEY (game, player),
    FOREIGN KEY (game) REFERENCES Games (gist)
);
INSERT INTO NewAppearances SELECT * FROM Appearances;
DROP TABLE Appearances;
ALTER TABLE NewAppearances RENAME TO Appearances;

CREATE INDEX appearances_by_player ON Appearances (player, timecode);
CREATE TRIGGER appearances_to_fuzzy_names AFTER INSERT ON Appearances BEGIN
    UPDATE FuzzyNames SET rank = rank + 1 WHERE word = NEW.account_name;
END;

ALTER TABLE Globals ADD COLUMN last_update TIMECODE NOT NULL DEFAULT '-52a95777800000001970-01-01T00:00:00+00:00';
UPDATE Globals SET last_update = format('%016x%s', last_update_message_id, last_update_filename_time);

ALTER TABLE Gamelogs ADD COLUMN timecode TIMECODE AS (format('%016x%s', message_id, filename_time));
