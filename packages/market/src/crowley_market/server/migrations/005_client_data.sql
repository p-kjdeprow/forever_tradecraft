CREATE TABLE client_data_exports (
    id                  bigserial PRIMARY KEY,
    export_key          text NOT NULL UNIQUE,
    client_build        text NOT NULL,
    manifest            jsonb NOT NULL,
    manifest_sha256     text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    loaded_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE client_recipes (
    export_id               bigint NOT NULL REFERENCES client_data_exports(id) ON DELETE CASCADE,
    spell_id                bigint NOT NULL,
    name                    text,
    kind                    text NOT NULL,
    learn_kind              text NOT NULL,
    learn_evidence          text NOT NULL CHECK (learn_evidence IN (
        'teaching_item_with_item_data',
        'teaching_item_without_item_data',
        'no_teaching_item'
    )),
    cooldown_ms             bigint,
    category_cooldown_ms    bigint,
    category_id             int,
    category_name           text,
    PRIMARY KEY (export_id, spell_id)
);

CREATE TABLE client_recipe_skill_lines (
    export_id               bigint NOT NULL,
    spell_id                bigint NOT NULL,
    ability_id              bigint NOT NULL,
    skill_line_id           int NOT NULL,
    profession              text NOT NULL,
    min_rank                int NOT NULL,
    yellow                  int NOT NULL,
    green_derived            int NOT NULL,
    grey                    int NOT NULL,
    acquire_method          int NOT NULL,
    flags                   bigint NOT NULL,
    supersedes_spell        bigint,
    PRIMARY KEY (export_id, spell_id, ability_id),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_reagents (
    export_id  bigint NOT NULL,
    spell_id   bigint NOT NULL,
    item_id    bigint NOT NULL,
    count      int NOT NULL,
    PRIMARY KEY (export_id, spell_id, item_id),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_outputs (
    export_id      bigint NOT NULL,
    spell_id       bigint NOT NULL,
    item_id        bigint NOT NULL,
    base_quantity  numeric NOT NULL,
    variance       numeric NOT NULL,
    yield_kind     text NOT NULL,
    PRIMARY KEY (export_id, spell_id, item_id),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_teachers (
    export_id          bigint NOT NULL,
    spell_id           bigint NOT NULL,
    teaching_item_id   bigint NOT NULL,
    PRIMARY KEY (export_id, spell_id, teaching_item_id),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_totems (
    export_id  bigint NOT NULL,
    spell_id   bigint NOT NULL,
    ordinal    int NOT NULL,
    totem_id   bigint NOT NULL,
    PRIMARY KEY (export_id, spell_id, ordinal),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_totem_categories (
    export_id   bigint NOT NULL,
    spell_id    bigint NOT NULL,
    ordinal     int NOT NULL,
    category_id bigint NOT NULL,
    PRIMARY KEY (export_id, spell_id, ordinal),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_recipe_spell_focus (
    export_id      bigint NOT NULL,
    spell_id       bigint NOT NULL,
    spell_focus_id bigint NOT NULL,
    PRIMARY KEY (export_id, spell_id),
    FOREIGN KEY (export_id, spell_id)
        REFERENCES client_recipes(export_id, spell_id) ON DELETE CASCADE
);

CREATE TABLE client_items (
    export_id           bigint NOT NULL REFERENCES client_data_exports(id) ON DELETE CASCADE,
    item_id             bigint NOT NULL,
    status              text NOT NULL,
    name                text,
    quality             int,
    item_level          int,
    class_id            int,
    subclass_id         int,
    sell_price          bigint,
    buy_price           bigint,
    vendor_stack_count  int,
    bonding             int,
    max_stack           int,
    required_skill      int,
    required_skill_rank int,
    PRIMARY KEY (export_id, item_id)
);

CREATE VIEW latest_client_export AS
SELECT id, export_key, client_build, manifest, manifest_sha256, loaded_at
FROM (
    SELECT e.*, row_number() OVER (
        PARTITION BY client_build ORDER BY loaded_at DESC, id DESC
    ) AS row_number
    FROM client_data_exports AS e
) AS ranked
WHERE row_number = 1;
