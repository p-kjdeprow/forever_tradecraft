CREATE TABLE paper_runs (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    house_id        int NOT NULL REFERENCES houses(id),
    client_build    text NOT NULL,
    start_t         bigint NOT NULL,
    capital_copper  bigint NOT NULL,
    strategies      jsonb NOT NULL,
    weights         jsonb NOT NULL,
    demand_share    text NOT NULL,
    engine_digest   text NOT NULL CHECK (engine_digest ~ '^[0-9a-f]{64}$'),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE paper_steps (
    run_id       bigint NOT NULL REFERENCES paper_runs(id) ON DELETE CASCADE,
    step_t       bigint NOT NULL,
    sweep_key    text NOT NULL,
    equity       jsonb NOT NULL,
    realized     jsonb NOT NULL,
    cash         jsonb NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, step_t)
);

CREATE TABLE paper_intents (
    run_id    bigint NOT NULL,
    step_t    bigint NOT NULL,
    strategy  text NOT NULL,
    ordinal   int NOT NULL,
    intent    jsonb NOT NULL,
    outcome   text NOT NULL,
    detail    jsonb NOT NULL,
    PRIMARY KEY (run_id, step_t, strategy, ordinal),
    FOREIGN KEY (run_id, step_t)
        REFERENCES paper_steps(run_id, step_t) ON DELETE CASCADE
);

CREATE TABLE paper_pending (
    run_id       bigint PRIMARY KEY REFERENCES paper_runs(id) ON DELETE CASCADE,
    step_t       bigint NOT NULL,
    sweep_key    text NOT NULL,
    intents      jsonb NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now()
);
