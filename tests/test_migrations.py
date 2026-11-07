"""Database migration tests for clean installs and the 001-008 upgrade path."""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.server.db import (
    MigrationStateError,
    MigrationVerificationError,
    migrate,
    migration_status,
    verify_current,
)

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")

MIGRATION_NAMES = tuple(
    f"{number:03d}_{name}.sql"
    for number, name in (
        (1, "init"),
        (2, "market_observations"),
        (3, "item_unit_price_and_time_left"),
        (4, "item_reference"),
        (5, "client_data"),
        (6, "paper_trading"),
        (7, "account_state"),
        (8, "trade_queue"),
        (9, "paper_capabilities"),
        (10, "house_integrity"),
        (11, "migration_ledger"),
    )
)


@dataclass(frozen=True)
class _FixtureIds:
    alliance_house_id: int
    horde_house_id: int
    scan_id: int
    sweep_id: int


def _reset_public_schema() -> None:
    """Stop TimescaleDB jobs before replacing the disposable public schema."""

    assert DB_URL is not None
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        has_timescaledb = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        if has_timescaledb:
            conn.execute("SELECT _timescaledb_functions.stop_background_workers()")
            deadline = time.monotonic() + 10
            while True:
                jobs = conn.execute(
                    """
                    SELECT pid, backend_type, application_name, state, query
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND state <> 'idle'
                      AND (
                          backend_type ILIKE '%timescaledb%'
                          OR application_name ILIKE '%timescaledb%'
                          OR query ILIKE '%_timescaledb_functions.%'
                      )
                    """
                ).fetchall()
                if not jobs:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"TimescaleDB jobs did not stop before schema reset: {jobs!r}")
                time.sleep(0.05)
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _migration_sql(name: str) -> str:
    return resources.files("crowley.server.migrations").joinpath(name).read_text(encoding="utf-8")


def _apply_migrations(conn: psycopg.Connection, names: tuple[str, ...]) -> None:
    conn.execute(
        "CREATE TABLE schema_migrations ("
        " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    for name in names:
        with conn.transaction():
            conn.execute(_migration_sql(name))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    conn.commit()


def _insert_upgrade_fixture(conn: psycopg.Connection, *, include_paper_run: bool = True) -> _FixtureIds:
    alliance_house_id = conn.execute(
        """
        INSERT INTO houses (key, region, ruleset, faction)
        VALUES ('us-normal-alliance', 'us', 'normal', 'alliance')
        RETURNING id
        """
    ).fetchone()[0]
    horde_house_id = conn.execute(
        """
        INSERT INTO houses (key, region, ruleset, faction)
        VALUES ('us-normal-horde', 'us', 'normal', 'horde')
        RETURNING id
        """
    ).fetchone()[0]
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    scan_id = conn.execute(
        """
        INSERT INTO scans
          (scan_uid, house_id, house_source, started_at, scanned_at, character,
           client_build, interface, num_listings, num_items, skipped, context, raw_path)
        VALUES ('legacy-scan', %s, 'auto', %s, %s, 'Tester', '70338', 16001,
                1, 1, 0, %s, 'legacy.raw.gz')
        RETURNING id
        """,
        (alliance_house_id, observed_at, observed_at, Jsonb({"source": "test"})),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO listings
          (scanned_at, house_id, scan_id, item_id, item_key, quantity, buyout,
           min_bid, bid, time_left, owner)
        VALUES (%s, %s, %s, 2589, '2589', 4, 400, 300, 300, 4, 'Seller')
        """,
        (observed_at, alliance_house_id, scan_id),
    )
    conn.execute(
        """
        INSERT INTO item_stats
          (scanned_at, house_id, scan_id, item_id, item_key, num_auctions,
           total_quantity, buyout_quantity, min_unit, p10_unit, p25_unit,
           median_unit, market_value)
        VALUES (%s, %s, %s, 2589, '2589', 1, 4, 4, 100, 100, 100, 100, 400)
        """,
        (observed_at, alliance_house_id, scan_id),
    )
    conn.execute(
        """
        INSERT INTO items (item_key, item_id, name, quality, level)
        VALUES ('2589', 2589, 'Linen Cloth', 1, 5)
        """
    )
    plan_hash = "a" * 64
    conn.execute(
        """
        INSERT INTO market_plans (plan_hash, plan_json, plan_body)
        VALUES (%s, '{}', %s)
        """,
        (plan_hash, Jsonb({})),
    )
    sweep_id = conn.execute(
        """
        INSERT INTO market_sweeps
          (sweep_uid, house_id, plan_hash, started_at, finished_at, character,
           client_version, client_build, interface, house_source, num_rows,
           num_observations, context, payload, content_digest, raw_path)
        VALUES ('format2-sweep', %s, %s, %s, %s, 'Tester', '1.60.1', '70338',
                16001, 'auto', 1, 1, %s, %s, %s, 'sweep.raw.gz')
        RETURNING id
        """,
        (
            alliance_house_id,
            plan_hash,
            observed_at,
            observed_at,
            Jsonb({"character": "Tester"}),
            Jsonb({}),
            "b" * 64,
        ),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO market_observations
          (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage,
           source_complete, cutoff, higher_price_witness, reason_code, row_count,
           observation)
        VALUES (%s, %s, 2589, '2589', 1, 'commodity', %s, 'full', true, %s,
                %s, 'ok', 0, %s)
        """,
        (
            sweep_id,
            alliance_house_id,
            Jsonb({"kind": "unqueried"}),
            Jsonb({"kind": "unbounded"}),
            Jsonb({"kind": "unknown"}),
            Jsonb({"rows": {}}),
        ),
    )
    conn.execute(
        """
        INSERT INTO owned_observations
          (sweep_id, house_id, character, status, query, reason_code, row_count, rows)
        VALUES (%s, %s, 'Tester', 'unqueried', %s, 'not_started', 0, %s)
        """,
        (sweep_id, alliance_house_id, Jsonb({"kind": "unqueried"}), Jsonb([])),
    )
    conn.execute(
        """
        INSERT INTO own_sale_evidence
          (house_id, character, auction_id, first_seen_sold_at, first_seen_sweep_id,
           item_id, item_key, raw_item_key, quantity, listed_buyout, item_link,
           observation_window)
        VALUES (%s, 'Tester', 7001, %s, %s, 2589, '2589', %s, 4, 400,
                'item-link', %s)
        """,
        (
            alliance_house_id,
            observed_at,
            sweep_id,
            Jsonb({"itemID": 2589}),
            Jsonb({"kind": "owned", "at": observed_at.isoformat()}),
        ),
    )
    if include_paper_run:
        conn.execute(
            """
            INSERT INTO paper_runs
              (name, house_id, client_build, start_t, capital_copper, strategies,
               weights, demand_share, engine_digest)
            VALUES ('upgrade-paper-run', %s, '1.60.1.70338', 1760000000, 10000,
                    %s, %s, '1/2', %s)
            """,
            (
                alliance_house_id,
                Jsonb(["vendor_arb"]),
                Jsonb(["1"]),
                "c" * 64,
            ),
        )
    return _FixtureIds(alliance_house_id, horde_house_id, scan_id, sweep_id)


def _affected_snapshot(conn: psycopg.Connection, fixture: _FixtureIds) -> tuple[tuple, ...]:
    return (
        tuple(
            conn.execute(
                "SELECT id, house_id, scan_uid FROM scans WHERE id=%s",
                (fixture.scan_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT item_key, item_id, name, quality, level FROM items WHERE item_key='2589'"
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT id, house_id, sweep_uid FROM market_sweeps WHERE id=%s",
                (fixture.sweep_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT house_id, scan_id, item_id, item_key, quantity FROM listings WHERE scan_id=%s",
                (fixture.scan_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT house_id, scan_id, item_id, item_key, total_quantity FROM item_stats WHERE scan_id=%s",
                (fixture.scan_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT sweep_id, house_id, item_id, item_key FROM market_observations WHERE sweep_id=%s",
                (fixture.sweep_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT sweep_id, house_id, character, status FROM owned_observations WHERE sweep_id=%s",
                (fixture.sweep_id,),
            ).fetchall()
        ),
        tuple(
            conn.execute(
                "SELECT house_id, character, auction_id, first_seen_sweep_id FROM own_sale_evidence"
            ).fetchall()
        ),
    )


def test_all_migrations_apply_to_an_empty_database() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        assert migrate(conn) == list(MIGRATION_NAMES)
        assert migrate(conn) == []


def test_upgrade_fixture_preserves_rows_and_drops_009_defaults() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:8])
        fixture = _insert_upgrade_fixture(conn)
        before = _affected_snapshot(conn, fixture)
        conn.commit()

        assert migrate(conn) == list(MIGRATION_NAMES[8:])
        assert _affected_snapshot(conn, fixture) == before
        paper_run = conn.execute(
            "SELECT capabilities, capabilities_hash FROM paper_runs WHERE name='upgrade-paper-run'"
        ).fetchone()
        assert paper_run == (
            {"spell_focus_ids": [], "tool_category_ids": [], "tool_item_ids": []},
            "ccdc3b5d95b9c7ec441822f39661fd4b847d22a5f5f21bfd816d81ce8dbc71d1",
        )
        for column in ("capabilities", "capabilities_hash"):
            assert (
                conn.execute(
                    """
                SELECT column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='paper_runs' AND column_name=%s
                """,
                    (column,),
                ).fetchone()[0]
                is None
            )
        assert (
            conn.execute("SELECT count(*) FROM schema_migrations WHERE checksum IS NULL").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'schema_migrations'
              AND column_name = 'checksum'
            """
            ).fetchone()[0]
            == "NO"
        )


def test_checksum_mismatch_is_refused() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        assert migrate(conn) == list(MIGRATION_NAMES)
        with conn.transaction():
            conn.execute(
                "UPDATE schema_migrations SET checksum = repeat('0', 64) WHERE name = %s",
                (MIGRATION_NAMES[0],),
            )
        with pytest.raises(MigrationStateError, match="checksum mismatch"):
            migrate(conn)


def test_unknown_applied_migration_is_refused() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        with conn.transaction():
            conn.execute(
                "CREATE TABLE schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute("INSERT INTO schema_migrations (name) VALUES ('999_future.sql')")
        with pytest.raises(MigrationStateError, match="unknown to this package"):
            migrate(conn)


def test_status_and_startup_verification_are_read_only() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:10])
        before = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
        status = migration_status(conn)
        assert status.applied == MIGRATION_NAMES[:10]
        assert status.pending == MIGRATION_NAMES[10:]
        with pytest.raises(MigrationVerificationError, match="run `crowley-server migrate`"):
            verify_current(conn)
        after = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
        assert after == before


def test_server_startup_refuses_pending_migrations_without_mutation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:10])
        before_ledger = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
        before_tables = conn.execute(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        ).fetchall()
        before_columns = conn.execute(
            """
            SELECT table_name, column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()

    monkeypatch.setenv("CROWLEY_DATABASE_URL", DB_URL)
    monkeypatch.setenv("CROWLEY_API_TOKEN", "startup-test-token")
    monkeypatch.setenv("CROWLEY_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from crowley.server.app import app
    from crowley.server.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(MigrationVerificationError, match="run `crowley-server migrate`"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()

    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == before_ledger
        assert (
            conn.execute(
                """
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            ).fetchall()
            == before_tables
        )
        assert (
            conn.execute(
                """
                SELECT table_name, column_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            ).fetchall()
            == before_columns
        )


def test_concurrent_apply_is_serialized_by_the_advisory_lock() -> None:
    _reset_public_schema()
    assert DB_URL is not None

    def apply() -> list[str]:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            return migrate(conn)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply(), range(2)))
    assert sorted(results, key=len) == [[], list(MIGRATION_NAMES)]


def test_migration_status_reports_checksum_mismatch() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        assert migrate(conn) == list(MIGRATION_NAMES)
        with conn.transaction():
            conn.execute(
                "UPDATE schema_migrations SET checksum = repeat('f', 64) WHERE name = %s",
                (MIGRATION_NAMES[-1],),
            )
        status = migration_status(conn)
        assert status.checksum_mismatch == (MIGRATION_NAMES[-1],)


def test_status_command_prints_without_applying(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:8])
        before_ledger = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
        before_tables = conn.execute(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        ).fetchall()
        before_columns = conn.execute(
            """
            SELECT table_name, column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()

    from crowley.server.__main__ import main
    from crowley.server.config import get_settings

    monkeypatch.setenv("CROWLEY_DATABASE_URL", DB_URL)
    monkeypatch.setenv("CROWLEY_API_TOKEN", "status-test-token")
    monkeypatch.setattr(sys, "argv", ["crowley-server", "migrate", "--status"])
    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()
    output = capsys.readouterr().out
    assert f"pending: {', '.join(MIGRATION_NAMES[8:])}" in output
    assert "checksum-mismatch: none" in output

    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == before_ledger
        assert (
            conn.execute(
                """
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            ).fetchall()
            == before_tables
        )
        assert (
            conn.execute(
                """
                SELECT table_name, column_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            ).fetchall()
            == before_columns
        )


def test_house_mismatch_is_rejected_by_named_constraint() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:9])
        fixture = _insert_upgrade_fixture(conn, include_paper_run=False)
        conn.commit()
        assert migrate(conn) == list(MIGRATION_NAMES[9:])
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="market_observations_sweep_house_fkey",
        ):
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO market_observations
                      (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage,
                       source_complete, cutoff, higher_price_witness, reason_code, row_count,
                       observation)
                    VALUES (%s, %s, 9999, '9999', 2, 'commodity', %s, 'full', true, %s,
                            %s, 'ok', 0, %s)
                    """,
                    (
                        fixture.sweep_id,
                        fixture.horde_house_id,
                        Jsonb({"kind": "unqueried"}),
                        Jsonb({"kind": "unbounded"}),
                        Jsonb({"kind": "unknown"}),
                        Jsonb({"rows": {}}),
                    ),
                )


def test_migration_010_fails_on_preexisting_house_mismatch() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:9])
        fixture = _insert_upgrade_fixture(conn, include_paper_run=False)
        conn.execute(
            """
            INSERT INTO market_observations
              (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage,
               source_complete, cutoff, higher_price_witness, reason_code, row_count,
               observation)
            VALUES (%s, %s, 9999, '9999', 2, 'commodity', %s, 'full', true, %s,
                    %s, 'ok', 0, %s)
            """,
            (
                fixture.sweep_id,
                fixture.horde_house_id,
                Jsonb({"kind": "unqueried"}),
                Jsonb({"kind": "unbounded"}),
                Jsonb({"kind": "unknown"}),
                Jsonb({"rows": {}}),
            ),
        )
        conn.commit()
        with pytest.raises(
            psycopg.errors.ForeignKeyViolation,
            match="market_observations_sweep_house_fkey",
        ):
            migrate(conn)
        assert conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == [
            (name,) for name in MIGRATION_NAMES[:9]
        ]


def test_legacy_tables_are_read_only_but_readable() -> None:
    _reset_public_schema()
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        _apply_migrations(conn, MIGRATION_NAMES[:9])
        fixture = _insert_upgrade_fixture(conn, include_paper_run=False)
        conn.commit()
        assert migrate(conn) == list(MIGRATION_NAMES[9:])
        before = _affected_snapshot(conn, fixture)
        assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM item_stats").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1

        for table, column in (
            ("scans", "house_id"),
            ("listings", "house_id"),
            ("item_stats", "house_id"),
            ("items", "name"),
        ):
            with pytest.raises(
                psycopg.errors.RaiseException,
                match=f"legacy table {table} is read-only",
            ):
                with conn.transaction():
                    conn.execute(f"UPDATE {table} SET {column} = {column}")
        assert _affected_snapshot(conn, fixture) == before
