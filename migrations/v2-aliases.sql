CREATE TABLE Aliases (
    src TEXT NOT NULL PRIMARY KEY,
    dst TEXT NOT NULL
);

CREATE INDEX bls_by_account ON Blacklists (account_name);
