-- The runner backfills this nullable column from the exact packaged migration
-- bytes, then sets it NOT NULL in a separate transaction after all migrations
-- through this file have been recorded.
ALTER TABLE schema_migrations
    ADD COLUMN IF NOT EXISTS checksum text;
