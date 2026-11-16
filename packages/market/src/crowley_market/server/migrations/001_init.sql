CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per (region, ruleset, faction). Every scan belongs to exactly one house.
CREATE TABLE houses (
    id          serial PRIMARY KEY,
    key         text NOT NULL UNIQUE,
    region      text NOT NULL,
    ruleset     text NOT NULL,
    faction     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (region, ruleset, faction)
);

CREATE TABLE scans (
    id            bigserial PRIMARY KEY,
    scan_uid      text NOT NULL UNIQUE,      -- addon-generated id; makes uploads idempotent
    house_id      int NOT NULL REFERENCES houses (id),
    house_source  text NOT NULL,             -- "auto" or which fields were overridden in game
    started_at    timestamptz NOT NULL,
    scanned_at    timestamptz NOT NULL,      -- when the addon finished collecting
    received_at   timestamptz NOT NULL DEFAULT now(),
    character     text NOT NULL,
    client_build  text NOT NULL,
    interface     int NOT NULL,
    num_listings  int NOT NULL,
    num_items     int NOT NULL,
    skipped       int NOT NULL,
    context       jsonb NOT NULL,
    raw_path      text NOT NULL
);
CREATE INDEX scans_house_time ON scans (house_id, scanned_at DESC);

-- Latest known display data per item key (names can differ per suffix).
CREATE TABLE items (
    item_key    text PRIMARY KEY,
    item_id     int NOT NULL,
    name        text NOT NULL,
    quality     smallint NOT NULL,
    level       int NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX items_item_id ON items (item_id);
CREATE INDEX items_name ON items (lower(name));

-- Every auction seen in every scan. Large: compressed after a few days.
CREATE TABLE listings (
    scanned_at  timestamptz NOT NULL,
    house_id    int NOT NULL,
    scan_id     bigint NOT NULL,
    item_id     int NOT NULL,
    item_key    text NOT NULL,
    quantity    int NOT NULL,
    buyout      bigint NOT NULL,             -- copper for the whole auction, 0 = bid only
    min_bid     bigint NOT NULL,
    bid         bigint NOT NULL,
    time_left   smallint NOT NULL,           -- 1 short .. 4 very long
    owner       text NOT NULL                -- empty when the seller is hidden
);
SELECT create_hypertable('listings', 'scanned_at', chunk_time_interval => interval '1 day');
CREATE INDEX listings_house_item_time ON listings (house_id, item_key, scanned_at DESC);
ALTER TABLE listings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'house_id, item_key',
    timescaledb.compress_orderby = 'scanned_at DESC'
);
SELECT add_compression_policy('listings', interval '3 days');

-- Per-scan summary per item key; the main input for strategies and charts.
CREATE TABLE item_stats (
    scanned_at       timestamptz NOT NULL,
    house_id         int NOT NULL,
    scan_id          bigint NOT NULL,
    item_id          int NOT NULL,
    item_key         text NOT NULL,
    num_auctions     int NOT NULL,
    total_quantity   int NOT NULL,
    buyout_quantity  int NOT NULL,
    min_unit         double precision,
    p10_unit         double precision,
    p25_unit         double precision,
    median_unit      double precision,
    market_value     double precision
);
SELECT create_hypertable('item_stats', 'scanned_at', chunk_time_interval => interval '7 days');
CREATE INDEX item_stats_house_item_time ON item_stats (house_id, item_key, scanned_at DESC);
CREATE INDEX item_stats_scan ON item_stats (scan_id);

-- API-surface reports from /crowley probe, used to verify the Forever client.
CREATE TABLE client_probes (
    id           bigserial PRIMARY KEY,
    probed_at    timestamptz NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    probe        jsonb NOT NULL,
    UNIQUE (probed_at)
);
