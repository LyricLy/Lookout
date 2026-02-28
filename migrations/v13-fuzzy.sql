CREATE VIRTUAL TABLE FuzzyNames USING spellfix1;
INSERT INTO FuzzyNames (word, rank) SELECT name, (SELECT COUNT(*) FROM Appearances WHERE account_name = name) FROM Names;

CREATE TRIGGER names_to_fuzzy_names AFTER INSERT ON Names BEGIN
    INSERT INTO FuzzyNames (word, rank) VALUES (name, 0);
END;

CREATE TRIGGER appearances_to_fuzzy_names AFTER INSERT ON Appearances BEGIN
    UPDATE FuzzyNames SET rank = rank + 1 WHERE word = account_name;
END;
