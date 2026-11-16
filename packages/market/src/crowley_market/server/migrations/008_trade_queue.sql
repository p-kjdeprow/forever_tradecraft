CREATE TABLE trade_queues (
    id           bigserial PRIMARY KEY,
    queue_id     text NOT NULL UNIQUE,
    house_id     int NOT NULL REFERENCES houses(id),
    character    text NOT NULL,
    generated_at timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    armed        boolean NOT NULL,
    actions      jsonb NOT NULL,
    source       text NOT NULL CHECK (source IN ('spec', 'paper_pending')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz
);
CREATE UNIQUE INDEX trade_queues_one_active ON trade_queues (house_id, character)
    WHERE superseded_at IS NULL;
CREATE INDEX trade_queues_active_expiry ON trade_queues (house_id, character, expires_at)
    WHERE superseded_at IS NULL;

CREATE TABLE trade_execution_records (
    queue_id      text PRIMARY KEY REFERENCES trade_queues(queue_id),
    received_at   timestamptz NOT NULL DEFAULT now(),
    raw           jsonb NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE trade_execution_results (
    queue_id text NOT NULL REFERENCES trade_queues(queue_id) ON DELETE CASCADE,
    ordinal  int NOT NULL,
    seq      int NOT NULL,
    kind     text NOT NULL,
    status   text NOT NULL,
    "at"     timestamptz NOT NULL,
    detail   jsonb NOT NULL,
    PRIMARY KEY (queue_id, ordinal)
);
