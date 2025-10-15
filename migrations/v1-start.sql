CREATE TABLE Gamelogs (
    hash TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    uploader INTEGER NOT NULL,
    approx_date DATE NOT NULL AS (coalesce(date(substring(filename, instr(filename, '-') + 1, 10)), date((message_id >> 22) / 1000 + 1420070400, 'unixepoch'))) STORED,
    clean_content TEXT NOT NULL
);

CREATE INDEX logs_by_message_id ON Gamelogs (message_id);
CREATE INDEX logs_by_date ON Gamelogs (approx_date);

CREATE TABLE Games (
    gist TEXT PRIMARY KEY,
    from_log TEXT UNIQUE NOT NULL,
    message_count INTEGER NOT NULL,
    analysis GAME NOT NULL,
    analysis_version INTEGER NOT NULL,
    FOREIGN KEY (from_log) REFERENCES GameLogs (hash)
);

CREATE INDEX games_by_version ON Games (analysis_version);

CREATE TABLE Blacklists (
    thread_id INTEGER NOT NULL,
    account_name TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE BlacklistGames (
    thread_id INTEGER NOT NULL,
    gist TEXT NOT NULL,
    UNIQUE (gist, thread_id)
);
