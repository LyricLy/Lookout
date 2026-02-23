DROP TABLE Blacklists;
CREATE TABLE Blacklists (
    thread_id INTEGER NOT NULL,
    account_name TEXT NOT NULL COLLATE NOCASE,
    reason TEXT,
    no_retrial INTEGER NOT NULL
);
CREATE INDEX bls_by_account ON Blacklists (account_name);
