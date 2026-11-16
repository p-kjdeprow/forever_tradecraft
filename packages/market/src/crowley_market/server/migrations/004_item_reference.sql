CREATE TABLE item_reference_captures (
    id              bigserial PRIMARY KEY,
    capture_uid     text NOT NULL UNIQUE,
    house_id        int NOT NULL REFERENCES houses(id),
    client_build    text NOT NULL,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz NOT NULL,
    ah_open         boolean NOT NULL,
    context         jsonb NOT NULL,
    raw             jsonb NOT NULL,
    content_digest  text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX item_reference_captures_house_time
    ON item_reference_captures (house_id, finished_at DESC);

CREATE TABLE item_reference (
    capture_id          bigint NOT NULL REFERENCES item_reference_captures(id) ON DELETE CASCADE,
    item_id             bigint NOT NULL,
    status              text NOT NULL,
    quality             smallint,
    item_level          int,
    min_level           int,
    class_id            int,
    subclass_id         int,
    max_stack           int,
    sell_price          bigint,
    bind_type           smallint,
    expansion_id        int,
    is_crafting_reagent boolean,
    deposit_kind        text,
    PRIMARY KEY (capture_id, item_id)
);

CREATE TABLE item_deposits (
    capture_id     bigint NOT NULL,
    item_id        bigint NOT NULL,
    duration       smallint NOT NULL,
    quantity       int NOT NULL,
    deposit_copper bigint NOT NULL,
    PRIMARY KEY (capture_id, item_id, duration, quantity),
    FOREIGN KEY (capture_id, item_id)
        REFERENCES item_reference(capture_id, item_id) ON DELETE CASCADE
);

CREATE VIEW latest_item_reference AS
SELECT capture_id, house_id, capture_uid, client_build, started_at, finished_at, ah_open,
       item_id, status, quality, item_level, min_level, class_id, subclass_id, max_stack,
       sell_price, bind_type, expansion_id, is_crafting_reagent, deposit_kind
FROM (
    SELECT c.id AS capture_id, c.house_id, c.capture_uid, c.client_build,
           c.started_at, c.finished_at, c.ah_open, r.item_id, r.status,
           r.quality, r.item_level, r.min_level, r.class_id, r.subclass_id,
           r.max_stack, r.sell_price, r.bind_type, r.expansion_id,
           r.is_crafting_reagent, r.deposit_kind,
           row_number() OVER (
               PARTITION BY c.house_id, c.client_build, r.item_id
               ORDER BY c.finished_at DESC, c.id DESC
           ) AS row_number
    FROM item_reference_captures AS c
    JOIN item_reference AS r ON r.capture_id = c.id
    WHERE r.status = 'ok'
) AS ranked
WHERE row_number = 1;
