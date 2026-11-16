DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM market_observations
        WHERE kind = 'item' AND coverage = 'bounded'
    ) THEN
        RAISE EXCEPTION 'cannot normalize bounded format-2 item observations';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM market_captured_rows r
        WHERE r.row_data->'timeLeft'->>'kind' = 'exact'
          AND (r.row_data->'timeLeft'->>'seconds')::bigint > 172800
          AND NOT (
              (r.row_data->'timeLeft'->>'seconds')::bigint BETWEEN 4294966996 AND 4294967295
          )
    ) OR EXISTS (
        SELECT 1
        FROM market_observations mo
        CROSS JOIN LATERAL jsonb_each(
            CASE
                WHEN jsonb_typeof(mo.observation->'rows') = 'object'
                    THEN mo.observation->'rows'
                ELSE '{}'::jsonb
            END
        ) AS entries(row_key, row_data)
        WHERE entries.row_data->'timeLeft'->>'kind' = 'exact'
          AND (entries.row_data->'timeLeft'->>'seconds')::bigint > 172800
          AND NOT (
              (entries.row_data->'timeLeft'->>'seconds')::bigint BETWEEN 4294966996 AND 4294967295
          )
    ) THEN
        RAISE EXCEPTION 'cannot normalize exact time-left seconds outside the understood range';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM market_captured_rows r
        JOIN market_observations mo
          ON mo.sweep_id = r.sweep_id AND mo.item_id = r.item_id
        WHERE mo.kind = 'item'
          AND r.row_data->'price'->>'kind' IS DISTINCT FROM 'auction'
    ) OR EXISTS (
        SELECT 1
        FROM market_observations mo
        CROSS JOIN LATERAL jsonb_each(
            CASE
                WHEN jsonb_typeof(mo.observation->'rows') = 'object'
                    THEN mo.observation->'rows'
                ELSE '{}'::jsonb
            END
        ) AS entries(row_key, row_data)
        WHERE mo.kind = 'item'
          AND entries.row_data->'price'->>'kind' IS DISTINCT FROM 'auction'
    ) THEN
        RAISE EXCEPTION 'cannot normalize item rows with a non-auction price';
    END IF;
END
$$;

UPDATE market_captured_rows r
SET unit_numerator = (r.row_data->'price'->>'copper')::bigint,
    unit_denominator = 1,
    row_data = jsonb_set(
        r.row_data,
        '{unitPrice}',
        jsonb_build_object(
            'numerator', (r.row_data->'price'->>'copper')::bigint,
            'denominator', 1
        ),
        true
    )
FROM market_observations mo
WHERE mo.sweep_id = r.sweep_id
  AND mo.item_id = r.item_id
  AND mo.kind = 'item'
  AND r.row_data->'unitPrice' IS DISTINCT FROM jsonb_build_object(
      'numerator', (r.row_data->'price'->>'copper')::bigint,
      'denominator', 1
  );

UPDATE market_captured_rows
SET row_data = jsonb_set(
    row_data,
    '{timeLeft,seconds}',
    to_jsonb((row_data->'timeLeft'->>'seconds')::bigint - 4294967296),
    false
)
WHERE row_data->'timeLeft'->>'kind' = 'exact'
  AND (row_data->'timeLeft'->>'seconds')::bigint BETWEEN 4294966996 AND 4294967295;

UPDATE market_observations mo
SET observation = jsonb_set(
    mo.observation,
    '{rows}',
    (
        SELECT COALESCE(
            jsonb_object_agg(
                entries.row_key,
                jsonb_set(
                    entries.row_data,
                    '{unitPrice}',
                    jsonb_build_object(
                        'numerator', (entries.row_data->'price'->>'copper')::bigint,
                        'denominator', 1
                    ),
                    true
                )
            ),
            '{}'::jsonb
        )
        FROM jsonb_each(mo.observation->'rows') AS entries(row_key, row_data)
    ),
    true
)
WHERE mo.kind = 'item'
  AND jsonb_typeof(mo.observation->'rows') = 'object'
  AND EXISTS (
      SELECT 1
      FROM jsonb_each(mo.observation->'rows') AS entries(row_key, row_data)
      WHERE entries.row_data->'unitPrice' IS DISTINCT FROM jsonb_build_object(
          'numerator', (entries.row_data->'price'->>'copper')::bigint,
          'denominator', 1
      )
  );

UPDATE market_observations mo
SET observation = jsonb_set(
    mo.observation,
    '{rows}',
    (
        SELECT COALESCE(
            jsonb_object_agg(
                entries.row_key,
                CASE
                    WHEN entries.row_data->'timeLeft'->>'kind' = 'exact'
                         AND (entries.row_data->'timeLeft'->>'seconds')::bigint
                             BETWEEN 4294966996 AND 4294967295
                    THEN jsonb_set(
                        entries.row_data,
                        '{timeLeft,seconds}',
                        to_jsonb((entries.row_data->'timeLeft'->>'seconds')::bigint - 4294967296),
                        false
                    )
                    ELSE entries.row_data
                END
            ),
            '{}'::jsonb
        )
        FROM jsonb_each(mo.observation->'rows') AS entries(row_key, row_data)
    ),
    true
)
WHERE jsonb_typeof(mo.observation->'rows') = 'object'
  AND EXISTS (
      SELECT 1
      FROM jsonb_each(mo.observation->'rows') AS entries(row_key, row_data)
      WHERE entries.row_data->'timeLeft'->>'kind' = 'exact'
        AND (entries.row_data->'timeLeft'->>'seconds')::bigint BETWEEN 4294966996 AND 4294967295
  );
