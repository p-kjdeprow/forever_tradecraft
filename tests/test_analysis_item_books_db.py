"""Database-gated house, build, coverage and window scoping of item-kind book loading."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from crowley_market.analysis.item_books import load_item_books
from crowley_market.server.db import migrate
from psycopg.types.json import Jsonb

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]


def _house(conn: psycopg.Connection, faction: str) -> int:
    return conn.execute(
        """
        INSERT INTO houses (key, region, ruleset, faction)
        VALUES (%s, 'us', 'normal', %s)
        ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key
        RETURNING id
        """,
        (f"us-normal-{faction}", faction),
    ).fetchone()[0]


def _sweep(conn: psycopg.Connection, house_id: int, tag: str, build: str, minutes: int) -> int:
    plan_hash = hashlib.sha256(tag.encode()).hexdigest()
    conn.execute(
        "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, '{}', %s) ON CONFLICT DO NOTHING",
        (plan_hash, Jsonb({})),
    )
    started = BASE + timedelta(minutes=minutes)
    client_version, client_build = build.rsplit(".", 1)
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
            plan_hash,
            started,
            started + timedelta(minutes=5),
            client_version,
            client_build,
            Jsonb({}),
            Jsonb({}),
            hashlib.sha256(f"digest-{tag}".encode()).hexdigest(),
            f"test-fixture/{tag}.json",
        ),
    ).fetchone()[0]


def _observe_item(
    conn: psycopg.Connection,
    house_id: int,
    sweep_id: int,
    item_id: int,
    *,
    started: int,
    coverage: str = "full",
    source_complete: bool = True,
    kind: str = "item",
    prices: tuple[int, ...] = (),
) -> None:
    conn.execute(
        """
        INSERT INTO market_observations
            (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage, source_complete,
             cutoff, higher_price_witness, reason_code, row_count, observation)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ok', %s, %s)
        """,
        (
            sweep_id,
            house_id,
            item_id,
            str(item_id),
            item_id,
            kind,
            Jsonb({"kind": "queried", "startedAt": started, "finishedAt": started + 60}),
            coverage,
            source_complete,
            Jsonb({"kind": "unbounded"}),
            Jsonb({"kind": "unknown"}),
            len(prices),
            Jsonb({}),
        ),
    )
    for ordinal, price in enumerate(prices):
        result_id = item_id * 100 + ordinal
        row = {
            "resultID": result_id,
            "quantity": 1,
            "unitPrice": {"numerator": price, "denominator": 1},
            "itemKey": str(item_id),
            "rawItemKey": {"itemID": item_id},
            "owners": {
                "names": {"Seller": True},
                "identityKind": "individual",
                "containsOwnerItem": False,
                "containsAccountItem": False,
                "totalNumberOfOwners": 1,
            },
            "timeLeft": {"kind": "band", "minimumSeconds": 7200, "maximumSeconds": 43200},
        }
        conn.execute(
            """
            INSERT INTO market_captured_rows
                (sweep_id, item_id, row_key, result_id, item_key, raw_item_key, quantity,
                 unit_numerator, unit_denominator, row_data)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, 1, %s)
            """,
            (
                sweep_id,
                item_id,
                f"row:{ordinal}",
                result_id,
                str(item_id),
                Jsonb({"itemID": item_id}),
                price,
                Jsonb(row),
            ),
        )


def test_item_book_loader_scopes_house_build_kind_coverage_window_and_empty_books() -> None:
    assert DB_URL is not None
    suffix = uuid.uuid4().hex[:8]
    t0 = int(BASE.timestamp())
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            alliance = _house(conn, "alliance")
            horde = _house(conn, "horde")
            old_alliance = _sweep(conn, alliance, f"old-alliance-{suffix}", "1.60.1.70450", 0)
            new_alliance = _sweep(conn, alliance, f"new-alliance-{suffix}", "1.60.1.70862", 60)
            later_alliance = _sweep(conn, alliance, f"later-alliance-{suffix}", "1.60.1.70862", 120)
            new_horde = _sweep(conn, horde, f"new-horde-{suffix}", "1.60.1.70862", 60)

            _observe_item(conn, alliance, old_alliance, 5001, started=t0, prices=(111,))
            _observe_item(conn, alliance, new_alliance, 5001, started=t0 + 3600, prices=(700, 500))
            _observe_item(conn, alliance, later_alliance, 5001, started=t0 + 7200, prices=(450,))
            _observe_item(
                conn, alliance, new_alliance, 5002, started=t0 + 3600, coverage="bounded", prices=(9,)
            )
            _observe_item(
                conn,
                alliance,
                new_alliance,
                5003,
                started=t0 + 3600,
                coverage="incomplete",
                source_complete=False,
                prices=(9,),
            )
            _observe_item(
                conn, alliance, new_alliance, 5004, started=t0 + 3600, kind="commodity", prices=(9,)
            )
            _observe_item(conn, alliance, new_alliance, 5005, started=t0 + 3600, prices=())
            _observe_item(conn, horde, new_horde, 5001, started=t0 + 3600, prices=(1,))

            item_ids = [5001, 5002, 5003, 5004, 5005, 5006, 5007]
            build = "1.60.1.70862"
            books = load_item_books(conn, "us-normal-alliance", item_ids, client_build=build)
            assert set(books) == {5001, 5005}
            assert [book.sweep_id for book in books[5001]] == [new_alliance, later_alliance]
            assert sorted(auction.unit for auction in books[5001][0].auctions) == [500, 700]
            assert books[5001][0].t == t0 + 3600
            assert books[5005][0].auctions == ()  # queried and empty is distinct from unqueried
            assert books[5001][0].record["envelope"]["house"] == {"key": "us-normal-alliance"}

            horde_books = load_item_books(conn, "us-normal-horde", item_ids, client_build=build)
            assert {
                item_id: [auction.unit for auction in rows[0].auctions]
                for item_id, rows in horde_books.items()
            } == {5001: [1]}
            old_books = load_item_books(conn, "us-normal-alliance", item_ids, client_build="1.60.1.70450")
            assert {
                item_id: [auction.unit for auction in rows[0].auctions] for item_id, rows in old_books.items()
            } == {5001: [111]}

            window = load_item_books(
                conn,
                "us-normal-alliance",
                item_ids,
                client_build=build,
                start_t=t0 + 7000,
                end_t=t0 + 7300,
            )
            assert [book.sweep_id for book in window[5001]] == [later_alliance]
            pinned = load_item_books(
                conn, "us-normal-alliance", item_ids, client_build=build, sweep_id=new_alliance
            )
            assert [book.sweep_id for book in pinned[5001]] == [new_alliance]
            assert load_item_books(conn, "us-normal-alliance", [], client_build=build) == {}
            with pytest.raises(ValueError, match="full client build"):
                load_item_books(conn, "us-normal-alliance", item_ids, client_build="70862")
