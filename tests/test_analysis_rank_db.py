"""Database-gated house and build scoping for the rank loaders."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.analysis.rank import _observed_kinds, _teaching_prices, latest_sweep
from crowley.server.db import migrate

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]

BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _house(conn: psycopg.Connection, faction: str) -> int:
    return conn.execute(
        "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, 'us', 'normal', %s) RETURNING id",
        (f"us-normal-{faction}", faction),
    ).fetchone()[0]


def _sweep(conn: psycopg.Connection, house_id: int, tag: str, build: str, minutes: int) -> int:
    plan = hashlib.sha256(tag.encode()).hexdigest()
    conn.execute(
        "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, '{}', %s) ON CONFLICT DO NOTHING",
        (plan, Jsonb({})),
    )
    started = BASE + timedelta(minutes=minutes)
    version, number = build.rsplit(".", 1)
    return conn.execute(
        """
        INSERT INTO market_sweeps
            (sweep_uid, house_id, plan_hash, started_at, finished_at, character, client_version,
             client_build, interface, house_source, num_rows, num_observations, context, payload,
             content_digest, raw_path)
        VALUES (%s, %s, %s, %s, %s, 'Tester', %s, %s, 1, 'auto', 0, 0, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tag,
            house_id,
            plan,
            started,
            started + timedelta(minutes=5),
            version,
            number,
            Jsonb({}),
            Jsonb({}),
            hashlib.sha256(f"digest-{tag}".encode()).hexdigest(),
            f"/dev/null/{tag}",
        ),
    ).fetchone()[0]


def _observe(
    conn: psycopg.Connection, house_id: int, sweep_id: int, item_id: int, kind: str, prices: tuple[int, ...]
) -> None:
    conn.execute(
        """
        INSERT INTO market_observations
            (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage, source_complete,
             cutoff, higher_price_witness, reason_code, row_count, observation)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'full', true, %s, %s, 'ok', %s, %s)
        """,
        (
            sweep_id,
            house_id,
            item_id,
            str(item_id),
            item_id,
            kind,
            Jsonb({"kind": "queried", "startedAt": 1, "finishedAt": 2}),
            Jsonb({"kind": "unbounded"}),
            Jsonb({"kind": "unknown"}),
            len(prices),
            Jsonb({}),
        ),
    )
    for ordinal, price in enumerate(prices):
        conn.execute(
            """
            INSERT INTO market_captured_rows
                (sweep_id, item_id, row_key, result_id, item_key, raw_item_key, quantity,
                 unit_numerator, unit_denominator, row_data)
            VALUES (%s, %s, %s, %s, %s, %s, 2, %s, 1, %s)
            """,
            (
                sweep_id,
                item_id,
                f"row:{ordinal}",
                item_id * 100 + ordinal,
                str(item_id),
                Jsonb({"itemID": item_id}),
                price,
                Jsonb({"price": {"kind": "auction", "copper": price}}),
            ),
        )


def test_loaders_are_scoped_to_house_build_and_sweep() -> None:
    assert DB_URL is not None
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house_a = _house(conn, "alliance")
            house_b = _house(conn, "horde")
            key_a = "us-normal-alliance"
            key_b = "us-normal-horde"
            old_a = _sweep(conn, house_a, f"old-a-{suffix}", "1.60.1.70450", 0)
            new_a = _sweep(conn, house_a, f"new-a-{suffix}", "1.60.1.70862", 60)
            newest_b = _sweep(conn, house_b, f"new-b-{suffix}", "1.60.1.70862", 120)

            _observe(conn, house_a, old_a, 9001, "commodity", (10,))
            _observe(conn, house_a, new_a, 5001, "item", (700, 500))
            _observe(conn, house_a, new_a, 5002, "item", ())
            _observe(conn, house_b, newest_b, 5001, "item", (1,))
            _observe(conn, house_b, newest_b, 5003, "item", (3,))

            assert latest_sweep(conn, key_a, None, None)[0] == new_a
            assert latest_sweep(conn, key_a, "1.60.1.70450", None)[0] == old_a
            assert latest_sweep(conn, key_b, None, None)[0] == newest_b
            assert latest_sweep(conn, key_a, None, BASE + timedelta(minutes=30))[0] == old_a
            assert latest_sweep(conn, key_a, None, None)[2] == "1.60.1.70862"
            with pytest.raises(RuntimeError, match="no market sweep"):
                latest_sweep(conn, key_a, "1.60.1.99999", None)

            # cheapest buyout in that sweep only; observed-with-no-rows is None; unobserved is absent
            assert _teaching_prices(conn, new_a, [5001, 5002, 5003, 9001]) == {5001: 500, 5002: None}
            assert _teaching_prices(conn, newest_b, [5001, 5003]) == {5001: 1, 5003: 3}

            assert _observed_kinds(conn, key_a, "1.60.1.70862", [5001, 9001, 5003]) == {5001: "item"}
            assert _observed_kinds(conn, key_a, "1.60.1.70450", [5001, 9001]) == {9001: "commodity"}
            assert _observed_kinds(conn, key_b, "1.60.1.70862", [5001, 5003]) == {5001: "item", 5003: "item"}


def test_account_snapshot_as_of_ignores_a_later_capture() -> None:
    from crowley.trading.account_gate import load_account_snapshot

    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house = _house(conn, "alliance")
            for uid, minutes, spell in (("early", 0, 111), ("late", 90, 222)):
                tag = f"{uid}-{uuid.uuid4().hex[:8]}"
                capture_id = conn.execute(
                    """
                    INSERT INTO account_captures
                        (capture_uid, house_id, character, captured_at, raw, content_digest)
                    VALUES (%s, %s, 'Dandune', %s, %s, %s) RETURNING id
                    """,
                    (
                        tag,
                        house,
                        BASE + timedelta(minutes=minutes),
                        Jsonb({"recipes": {"171": {}}, "professions": {}}),
                        hashlib.sha256(tag.encode()).hexdigest(),
                    ),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO account_recipes (capture_id, skill_line_id, spell_id) VALUES (%s, 171, %s)",
                    (capture_id, spell),
                )
            key = "us-normal-alliance"
            latest = load_account_snapshot(conn, key, "Dandune")
            assert latest.known_spell_ids == {222}
            as_of = load_account_snapshot(conn, key, "Dandune", as_of=BASE + timedelta(minutes=30))
            assert as_of.known_spell_ids == {111}
            with pytest.raises(RuntimeError, match="at or before"):
                load_account_snapshot(conn, key, "Dandune", as_of=BASE - timedelta(minutes=1))
