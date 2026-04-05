ALTER TABLE Globals DROP COLUMN last_update;
ALTER TABLE Globals DROP COLUMN last_update_message_id;
ALTER TABLE Globals DROP COLUMN last_update_filename_time;
ALTER TABLE Globals ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE Games ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
