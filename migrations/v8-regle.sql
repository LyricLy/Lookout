-- this violates 3NF because "correct" is dependent on "gist", but it would be a bother if it wasn't this way
CREATE TABLE RegleGames (
    player_id INTEGER NOT NULL,
    guessed TEXT NOT NULL,
    correct TEXT NOT NULL,
    gist TEXT NOT NULL,
    FOREIGN KEY (gist) REFERENCES Games (gist)
);
