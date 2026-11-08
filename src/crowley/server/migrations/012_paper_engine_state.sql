-- 012 is intentionally unapplied. It establishes the replay contract for
-- new paper runs while preserving pre-contract rows as read-only legacy data.

ALTER TABLE paper_runs
    ADD COLUMN engine_contract text;

UPDATE paper_runs
SET engine_contract = 'legacy';

ALTER TABLE paper_runs
    ALTER COLUMN engine_contract SET NOT NULL,
    ADD CONSTRAINT paper_runs_engine_contract
        CHECK (engine_contract IN ('legacy', 'replay-v1'));

ALTER TABLE paper_steps
    ADD COLUMN replay_ordinal bigint,
    ADD COLUMN previous_digest text,
    ADD COLUMN step_digest text,
    ADD CONSTRAINT paper_steps_replay_ordinal_nonnegative
        CHECK (replay_ordinal IS NULL OR replay_ordinal >= 0),
    ADD CONSTRAINT paper_steps_previous_digest_shape
        CHECK (previous_digest IS NULL OR previous_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT paper_steps_step_digest_shape
        CHECK (step_digest IS NULL OR step_digest ~ '^[0-9a-f]{64}$');

-- NULL is deliberate for legacy rows: 012 does not invent replay ordinals or
-- digests for a run whose original engine contract is no longer resumable.
CREATE UNIQUE INDEX paper_steps_run_replay_ordinal
    ON paper_steps (run_id, replay_ordinal);

ALTER TABLE paper_pending
    ADD COLUMN replay_ordinal bigint,
    ADD CONSTRAINT paper_pending_replay_ordinal_nonnegative
        CHECK (replay_ordinal IS NULL OR replay_ordinal >= 0);

CREATE TABLE paper_engine_state (
    run_id         bigint NOT NULL,
    replay_ordinal bigint NOT NULL CHECK (replay_ordinal >= 0),
    step_t         bigint NOT NULL,
    state_version  integer NOT NULL CHECK (state_version > 0),
    state          jsonb NOT NULL,
    state_digest   text NOT NULL CHECK (state_digest ~ '^[0-9a-f]{64}$'),
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, replay_ordinal),
    FOREIGN KEY (run_id, replay_ordinal)
        REFERENCES paper_steps(run_id, replay_ordinal) ON DELETE CASCADE
);

CREATE TABLE paper_ledger (
    run_id         bigint NOT NULL,
    replay_ordinal bigint NOT NULL,
    account_name   text NOT NULL,
    event_ordinal  integer NOT NULL CHECK (event_ordinal >= 0),
    event          jsonb NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, replay_ordinal, account_name, event_ordinal),
    FOREIGN KEY (run_id, replay_ordinal)
        REFERENCES paper_steps(run_id, replay_ordinal) ON DELETE CASCADE
);

CREATE TABLE paper_trades (
    run_id         bigint NOT NULL,
    replay_ordinal bigint NOT NULL,
    account_name   text NOT NULL,
    trade_ordinal  integer NOT NULL CHECK (trade_ordinal >= 0),
    trade          jsonb NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, replay_ordinal, account_name, trade_ordinal),
    FOREIGN KEY (run_id, replay_ordinal)
        REFERENCES paper_steps(run_id, replay_ordinal) ON DELETE CASCADE
);

-- Per-step purchase deltas are append-only rows. They are never copied into
-- a checkpoint and are not a materialized cumulative purchase map.
CREATE TABLE paper_purchases (
    run_id         bigint NOT NULL,
    replay_ordinal bigint NOT NULL,
    item_id        bigint NOT NULL CHECK (item_id > 0),
    result_key     text NOT NULL,
    result_id      jsonb NOT NULL,
    quantity       bigint NOT NULL CHECK (quantity > 0),
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, replay_ordinal, item_id, result_key),
    FOREIGN KEY (run_id, replay_ordinal)
        REFERENCES paper_steps(run_id, replay_ordinal) ON DELETE CASCADE
);

CREATE INDEX paper_purchases_run_ordinal
    ON paper_purchases (run_id, replay_ordinal);

CREATE FUNCTION crowley_reject_finalized_paper_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- A cascading run cleanup is allowed; a direct mutation of finalized
    -- replay output is never allowed.
    IF pg_trigger_depth() = 1 THEN
        RAISE EXCEPTION 'finalized paper rows are immutable: % %', TG_OP, TG_TABLE_NAME;
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER paper_steps_finalized_guard
BEFORE UPDATE OR DELETE ON paper_steps
FOR EACH ROW EXECUTE FUNCTION crowley_reject_finalized_paper_mutation();

CREATE TRIGGER paper_ledger_finalized_guard
BEFORE UPDATE OR DELETE ON paper_ledger
FOR EACH ROW EXECUTE FUNCTION crowley_reject_finalized_paper_mutation();

CREATE TRIGGER paper_trades_finalized_guard
BEFORE UPDATE OR DELETE ON paper_trades
FOR EACH ROW EXECUTE FUNCTION crowley_reject_finalized_paper_mutation();

CREATE TRIGGER paper_purchases_finalized_guard
BEFORE UPDATE OR DELETE ON paper_purchases
FOR EACH ROW EXECUTE FUNCTION crowley_reject_finalized_paper_mutation();
