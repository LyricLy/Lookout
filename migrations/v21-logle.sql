CREATE TABLE NewRegleGames (
    player_id INTEGER NOT NULL,
    guessed TEXT,
    correct TEXT NOT NULL,
    game TEXT NOT NULL,
    FOREIGN KEY (game) REFERENCES Games (gist)
);
INSERT INTO NewRegleGames SELECT * FROM RegleGames;
DROP TABLE RegleGames;
ALTER TABLE NewRegleGames RENAME TO RegleGames;

CREATE TABLE NewWilleGames (
    player_id INTEGER NOT NULL,
    guessed INTEGER,
    correct INTEGER NOT NULL,
    game TEXT NOT NULL,
    FOREIGN KEY (game) REFERENCES Games (gist)
);
INSERT INTO NewWilleGames SELECT * FROM WilleGames;
DROP TABLE WilleGames;
ALTER TABLE NewWilleGames RENAME TO WilleGames;

CREATE TABLE LogleGames (
    player_id INTEGER NOT NULL,
    guessed MSGPACK,
    correct MSGPACK NOT NULL,
    game TEXT NOT NULL,
    num_targets INTEGER NOT NULL,
    FOREIGN KEY (game) REFERENCES Games (gist)
)
