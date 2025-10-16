CREATE TABLE Aliases (
    src TEXT NOT NULL PRIMARY KEY,
    dst TEXT NOT NULL
);

-- manual for now
INSERT INTO Aliases VALUES ("Jeg11", "Jona1");
INSERT INTO Aliases VALUES ("Machete", "Jona1");
INSERT INTO Aliases VALUES ("townofsalem69", "Obviously");
INSERT INTO Aliases VALUES ("Danman34682", "Waffleslice");
INSERT INTO Aliases VALUES ("MobilePhoin", "Phoin");
INSERT INTO Aliases VALUES ("skasineALT", "skasine");
INSERT INTO Aliases VALUES ("vallourn", "Jinq");
INSERT INTO Aliases VALUES ("Emberweed", "Carnifloran");

CREATE INDEX bls_by_account ON Blacklists (account_name);
