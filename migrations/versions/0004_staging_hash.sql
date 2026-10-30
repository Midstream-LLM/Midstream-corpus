ALTER TABLE staging_text
    ADD COLUMN text_hash bytea NOT NULL,
    ADD COLUMN char_count bigint NOT NULL,
    ALTER COLUMN content DROP NOT NULL;
