-- Canonical simulation capabilities are part of a paper run's immutable input.
-- Existing rows intentionally receive the empty (fail-closed) capability set.
ALTER TABLE paper_runs
    ADD COLUMN capabilities jsonb NOT NULL DEFAULT
        '{"spell_focus_ids": [], "tool_category_ids": [], "tool_item_ids": []}'::jsonb,
    ADD COLUMN capabilities_hash text NOT NULL DEFAULT
        'ccdc3b5d95b9c7ec441822f39661fd4b847d22a5f5f21bfd816d81ce8dbc71d1'
        CHECK (capabilities_hash ~ '^[0-9a-f]{64}$');

-- Defaults are only for backfilling rows that predate this migration.  New
-- paper runs must always provide their immutable capability input explicitly.
ALTER TABLE paper_runs
    ALTER COLUMN capabilities DROP DEFAULT,
    ALTER COLUMN capabilities_hash DROP DEFAULT;
