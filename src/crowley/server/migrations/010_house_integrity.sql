-- The legacy market tables are historical, read-only data.  The trigger is
-- statement-level so one clear error protects every write shape, including
-- statements that would affect no rows.  TimescaleDB supports statement-level
-- triggers on hypertables, including the compression-enabled listings table.
CREATE FUNCTION crowley_reject_legacy_write()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'legacy table % is read-only; writes are not supported after migration 010', TG_TABLE_NAME;
    RETURN NULL;
END;
$$;

-- Legacy scans are retained for collision checks and historical reads only.
CREATE TRIGGER scans_legacy_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON scans
FOR EACH STATEMENT
EXECUTE FUNCTION crowley_reject_legacy_write();

-- Legacy listings are a compression-enabled hypertable and must remain
-- untouched; the trigger is the supported write guard for this table.
CREATE TRIGGER listings_legacy_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON listings
FOR EACH STATEMENT
EXECUTE FUNCTION crowley_reject_legacy_write();

-- Legacy item statistics are retained for historical reads only.
CREATE TRIGGER item_stats_legacy_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON item_stats
FOR EACH STATEMENT
EXECUTE FUNCTION crowley_reject_legacy_write();

-- Legacy item display data is retained for historical reads only.
CREATE TRIGGER items_legacy_write_guard
BEFORE INSERT OR UPDATE OR DELETE ON items
FOR EACH STATEMENT
EXECUTE FUNCTION crowley_reject_legacy_write();

-- Required target key for all sweep-owned child tables below.
ALTER TABLE market_sweeps
    ADD CONSTRAINT market_sweeps_id_house_key UNIQUE (id, house_id);

-- A market observation belongs to the house of its sweep.
ALTER TABLE market_observations
    ADD CONSTRAINT market_observations_sweep_house_fkey
    FOREIGN KEY (sweep_id, house_id) REFERENCES market_sweeps (id, house_id);

-- The owned-observation result belongs to the house of its sweep.
ALTER TABLE owned_observations
    ADD CONSTRAINT owned_observations_sweep_house_fkey
    FOREIGN KEY (sweep_id, house_id) REFERENCES market_sweeps (id, house_id);

-- Sale evidence records the sweep that first observed the sale and must retain
-- that sweep's house.
ALTER TABLE own_sale_evidence
    ADD CONSTRAINT own_sale_evidence_sweep_house_fkey
    FOREIGN KEY (first_seen_sweep_id, house_id) REFERENCES market_sweeps (id, house_id);
