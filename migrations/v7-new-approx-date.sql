-- this just changes the definition of approx_date but has to remake 2 tables to do it

ALTER TABLE Gamelogs RENAME TO OldGamelogs;
CREATE TABLE Gamelogs (
    hash TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    uploader INTEGER NOT NULL,
    approx_date DATE NOT NULL AS (date((message_id >> 22) / 1000 + 1420070400, 'unixepoch')) STORED,
    clean_content TEXT NOT NULL
);
INSERT INTO Gamelogs SELECT hash, filename, message_id, attachment_id, uploader, clean_content FROM OldGamelogs;
DROP INDEX logs_by_message_id;
CREATE INDEX logs_by_message_id ON Gamelogs (message_id);
DROP INDEX logs_by_date;
CREATE INDEX logs_by_date ON Gamelogs (approx_date);

-- prevent breaking foreign key constraint
ALTER TABLE Games RENAME TO OldGames;
CREATE TABLE Games (
    gist TEXT PRIMARY KEY,
    from_log TEXT UNIQUE NOT NULL,
    message_count INTEGER NOT NULL,
    analysis GAME NOT NULL,
    analysis_version INTEGER NOT NULL,
    FOREIGN KEY (from_log) REFERENCES Gamelogs (hash)
);
INSERT INTO Games SELECT * FROM OldGames;
DROP INDEX games_by_version;
CREATE INDEX games_by_version ON Games (analysis_version);

DROP TABLE OldGames;
DROP TABLE OldGamelogs;
