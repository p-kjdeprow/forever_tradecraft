CREATE TABLE account_captures (
    id              bigserial PRIMARY KEY,
    capture_uid     text NOT NULL UNIQUE,
    house_id        int NOT NULL REFERENCES houses(id),
    character       text NOT NULL,
    captured_at     timestamptz NOT NULL,
    gold            bigint,
    raw             jsonb NOT NULL,
    content_digest  text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX account_captures_character_time
    ON account_captures (house_id, character, captured_at DESC, id DESC);

CREATE TABLE account_items (
    capture_id      bigint NOT NULL REFERENCES account_captures(id) ON DELETE CASCADE,
    location        text NOT NULL CHECK (location IN ('bag', 'mail')),
    container       text NOT NULL,
    slot            int NOT NULL,
    item_id         bigint NOT NULL,
    count           int NOT NULL,
    bound           boolean,
    PRIMARY KEY (capture_id, location, container, slot)
);

CREATE TABLE account_professions (
    capture_id      bigint NOT NULL REFERENCES account_captures(id) ON DELETE CASCADE,
    skill_line_id   int NOT NULL,
    name            text NOT NULL,
    level           int NOT NULL,
    max_level       int NOT NULL,
    PRIMARY KEY (capture_id, skill_line_id)
);

CREATE TABLE account_recipes (
    capture_id      bigint NOT NULL REFERENCES account_captures(id) ON DELETE CASCADE,
    skill_line_id   int NOT NULL,
    spell_id        bigint NOT NULL,
    PRIMARY KEY (capture_id, skill_line_id, spell_id)
);

CREATE TABLE account_cooldowns (
    capture_id          bigint NOT NULL REFERENCES account_captures(id) ON DELETE CASCADE,
    spell_id            bigint NOT NULL,
    start_seconds       numeric NOT NULL,
    duration_seconds    numeric NOT NULL,
    PRIMARY KEY (capture_id, spell_id)
);

CREATE VIEW latest_account_state AS
SELECT capture_id, house_id, capture_uid, character, captured_at, gold, raw,
       content_digest, received_at
FROM (
    SELECT c.id AS capture_id, c.house_id, c.capture_uid, c.character,
           c.captured_at, c.gold, c.raw, c.content_digest, c.received_at,
           row_number() OVER (
               PARTITION BY c.house_id, c.character
               ORDER BY c.captured_at DESC, c.id DESC
           ) AS row_number
    FROM account_captures AS c
) AS ranked
WHERE row_number = 1;

