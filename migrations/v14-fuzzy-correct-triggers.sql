DROP TRIGGER names_to_fuzzy_names;
CREATE TRIGGER names_to_fuzzy_names AFTER INSERT ON Names BEGIN
    INSERT INTO FuzzyNames (word, rank) VALUES (NEW.name, 0);
END;

DROP TRIGGER appearances_to_fuzzy_names;
CREATE TRIGGER appearances_to_fuzzy_names AFTER INSERT ON Appearances BEGIN
    UPDATE FuzzyNames SET rank = rank + 1 WHERE word = NEW.account_name;
END;
