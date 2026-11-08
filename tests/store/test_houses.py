"""Database-gated tests for house repository isolation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import psycopg
import pytest

from crowley.core.house import House
from crowley.server.db import migrate
from crowley.store.houses import ensure_house, house_id, list_houses

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]


@pytest.fixture
def db_conn() -> psycopg.Connection:
    assert DB_URL is not None
    from tests.test_migrations import MIGRATION_NAMES, _apply_migrations, _reset_public_schema

    _reset_public_schema()
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:9])
        persisted_id = conn.execute(
            """
            INSERT INTO houses (key, region, ruleset, faction)
            VALUES ('eu-hardcore-alliance', 'eu', 'hardcore', 'alliance')
            RETURNING id
            """
        ).fetchone()[0]
        legacy_times = (
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 3, tzinfo=UTC),
        )
        for index, (scanned_at, listings) in enumerate(zip(legacy_times, (11, 22), strict=True), start=1):
            scan_id = conn.execute(
                """
                INSERT INTO scans
                  (scan_uid, house_id, house_source, started_at, scanned_at, character,
                   client_build, interface, num_listings, num_items, skipped, context, raw_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    f"legacy-{index}",
                    persisted_id,
                    "auto",
                    scanned_at,
                    scanned_at,
                    "Tester",
                    "70338",
                    16001,
                    listings,
                    1,
                    0,
                    json.dumps({}),
                    f"legacy-{index}.json.gz",
                ),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO listings
                  (scanned_at, house_id, scan_id, item_id, item_key, quantity, buyout,
                   min_bid, bid, time_left, owner)
                VALUES (%s, %s, %s, 2589, '2589', %s, 400, 300, 300, 4, 'Seller')
                """,
                (scanned_at, persisted_id, scan_id, listings),
            )
        conn.commit()
        assert migrate(conn) == list(MIGRATION_NAMES[9:])
        yield conn


def test_house_repository_round_trips_identity_without_cross_house_join(db_conn: psycopg.Connection) -> None:
    with db_conn.transaction():
        house = House("eu", "hardcore", "alliance")
        persisted_id = ensure_house(db_conn, house)
        assert house_id(db_conn, house) == persisted_id

        plan_hash = "a" * 64
        db_conn.execute(
            "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, %s, %s)",
            (plan_hash, "{}", json.dumps({})),
        )
        sweep_times = (
            datetime(2026, 1, 2, 12, tzinfo=UTC),
            datetime(2026, 1, 4, tzinfo=UTC),
        )
        for index, finished_at in enumerate(sweep_times, start=1):
            db_conn.execute(
                """
                INSERT INTO market_sweeps
                  (sweep_uid, house_id, plan_hash, started_at, finished_at, character,
                   client_version, client_build, interface, house_source, num_rows,
                   num_observations, context, payload, content_digest, raw_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"sweep-{index}",
                    persisted_id,
                    plan_hash,
                    finished_at,
                    finished_at,
                    "Tester",
                    "1.60.1",
                    "70338",
                    16001,
                    "auto",
                    0,
                    0,
                    json.dumps({}),
                    json.dumps({}),
                    "b" * 64,
                    f"sweep-{index}.json.gz",
                ),
            )

        rows = list_houses(db_conn)
        persisted = next(row for row in rows if row["key"] == house.key)
        assert persisted["legacy_scans"] == 2
        assert persisted["sweeps"] == 2
        assert persisted["scans"] == 4
        assert persisted["last_scan_at"] == sweep_times[-1]
        assert persisted["last_scan_listings"] == 22
