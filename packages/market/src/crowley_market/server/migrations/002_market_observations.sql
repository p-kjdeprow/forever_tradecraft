-- Format 2 market observations are additive.  The legacy scans/listings/item_stats
-- tables remain available for historical data, but are never used by volume queries.
CREATE TABLE market_plans (
    plan_hash text PRIMARY KEY CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    plan_json text NOT NULL,
    plan_body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE market_sweeps (
    id bigserial PRIMARY KEY,
    sweep_uid text NOT NULL UNIQUE,
    house_id int NOT NULL REFERENCES houses(id),
    plan_hash text NOT NULL REFERENCES market_plans(plan_hash),
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    character text NOT NULL,
    client_version text NOT NULL,
    client_build text NOT NULL,
    interface int NOT NULL,
    house_source text NOT NULL,
    num_rows int NOT NULL,
    num_observations int NOT NULL,
    context jsonb NOT NULL,
    payload jsonb NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    raw_path text NOT NULL
);
CREATE INDEX market_sweeps_house_time ON market_sweeps (house_id, finished_at DESC);
CREATE INDEX market_sweeps_house_plan_time ON market_sweeps (house_id, plan_hash, started_at, finished_at);

CREATE TABLE market_observations (
    sweep_id bigint NOT NULL REFERENCES market_sweeps(id) ON DELETE CASCADE,
    house_id int NOT NULL REFERENCES houses(id),
    item_id bigint NOT NULL,
    item_key text NOT NULL,
    ordinal int NOT NULL,
    kind text NOT NULL,
    query jsonb NOT NULL,
    coverage text NOT NULL,
    source_complete boolean NOT NULL,
    cutoff jsonb NOT NULL,
    higher_price_witness jsonb NOT NULL,
    reason_code text NOT NULL,
    row_count int NOT NULL,
    observation jsonb NOT NULL,
    PRIMARY KEY (sweep_id, item_id),
    UNIQUE (sweep_id, ordinal)
);
CREATE INDEX market_observations_house_item ON market_observations (house_id, item_key, sweep_id);
-- ``item_key`` is the base query identity.  Exact variants are projected from
-- captured rows by canonical key and raw identity at read time.
CREATE INDEX market_observations_house_item_id ON market_observations (house_id, item_id, sweep_id);

CREATE TABLE market_captured_rows (
    sweep_id bigint NOT NULL,
    item_id bigint NOT NULL,
    row_key text NOT NULL,
    result_id bigint NOT NULL,
    item_key text NOT NULL,
    raw_item_key jsonb NOT NULL,
    quantity bigint NOT NULL,
    unit_numerator bigint NOT NULL,
    unit_denominator bigint NOT NULL,
    row_data jsonb NOT NULL,
    PRIMARY KEY (sweep_id, item_id, row_key),
    FOREIGN KEY (sweep_id, item_id) REFERENCES market_observations(sweep_id, item_id) ON DELETE CASCADE
);
CREATE INDEX market_rows_item_price ON market_captured_rows (item_id, item_key, unit_numerator, unit_denominator);
CREATE INDEX market_rows_scope ON market_captured_rows (sweep_id, item_id, item_key);

CREATE TABLE owned_observations (
    sweep_id bigint PRIMARY KEY REFERENCES market_sweeps(id) ON DELETE CASCADE,
    house_id int NOT NULL REFERENCES houses(id),
    character text NOT NULL,
    status text NOT NULL,
    query jsonb NOT NULL,
    reason_code text NOT NULL,
    row_count int NOT NULL,
    rows jsonb NOT NULL
);

CREATE TABLE own_sale_evidence (
    house_id int NOT NULL REFERENCES houses(id),
    character text NOT NULL,
    auction_id bigint NOT NULL,
    first_seen_sold_at timestamptz NOT NULL,
    first_seen_sweep_id bigint NOT NULL REFERENCES market_sweeps(id),
    item_id bigint NOT NULL,
    item_key text NOT NULL,
    raw_item_key jsonb NOT NULL,
    quantity bigint NOT NULL,
    listed_buyout bigint,
    item_link text NOT NULL,
    observation_window jsonb NOT NULL,
    inconsistent boolean NOT NULL DEFAULT false,
    PRIMARY KEY (house_id, character, auction_id)
);
