CREATE TABLE WilleGames (
    player_id INTEGER NOT NULL,
    guessed INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    gist TEXT NOT NULL,
    FOREIGN KEY (gist) REFERENCES Games (gist)
);
