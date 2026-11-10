"""Database-gated attempted-set handling: what the loaders hand the candidates view as observed."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.analysis.books import load_commodity_books
from crowley.analysis.item_books import load_item_books
from crowley.server.db import migrate

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]

BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
BUILD = "1.60.1.70862"
HOUSE = "us-normal-alliance"
COLD_COMMODITY = 7001
COLD_ITEM = 7002
HOT_COMMODITY = 7003


def _sweep(conn: psycopg.Connection, house_id: int, tag: str, minutes: int, cold_slice: int) -> int:
    plan = hashlib.sha256(tag.encode()).hexdigest()
    conn.execute(
        "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, '{}', %s) ON CONFLICT DO NOTHING",
        (plan, Jsonb({})),
    )
    started = BASE + timedelta(minutes=minutes)
    version, number = BUILD.rsplit(".", 1)
    context = {"coldSlice": {"index": cold_slice, "count": 6}}
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
            Jsonb(context),
            Jsonb({}),
            hashlib.sha256(f"digest-{tag}".encode()).hexdigest(),
            f"/dev/null/{tag}",
        ),
    ).fetchone()[0]


def _observe(
    conn: psycopg.Connection,
    house_id: int,
    sweep_id: int,
    item_id: int,
    ordinal: int,
    kind: str,
    prices: tuple[int, ...],
    *,
    minutes: int,
    coverage: str = "full",
    queried: bool = True,
) -> None:
    query = {
        "kind": "queried",
        "startedAt": int((BASE + timedelta(minutes=minutes)).timestamp()),
        "finishedAt": 2,
    }
    if not queried:
        query = {"kind": "unqueried"}
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
            ordinal,
            kind,
            Jsonb(query),
            coverage,
            queried and coverage == "full",
            Jsonb({"kind": "unbounded"}),
            Jsonb({"kind": "unknown"}),
            len(prices),
            Jsonb({}),
        ),
    )
    for index, price in enumerate(prices):
        row_data = {
            "unitPrice": {"numerator": price, "denominator": 1},
            "timeLeft": {"kind": "exact", "seconds": 5000},
            "owners": {
                "totalNumberOfOwners": 1,
                "names": {f"Seller{index}": True},
                "identityKind": "individual",
                "containsOwnerItem": False,
            },
        }
        conn.execute(
            """
            INSERT INTO market_captured_rows
                (sweep_id, item_id, row_key, result_id, item_key, raw_item_key, quantity,
                 unit_numerator, unit_denominator, row_data)
            VALUES (%s, %s, %s, %s, %s, %s, 3, %s, 1, %s)
            """,
            (
                sweep_id,
                item_id,
                f"row:{index}",
                item_id * 100 + index,
                str(item_id),
                Jsonb({"itemID": item_id}),
                price,
                Jsonb(row_data),
            ),
        )


def test_attempted_set_is_the_loaded_observations() -> None:
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house = conn.execute(
                "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, 'us', 'normal', 'alliance') RETURNING id",
                (HOUSE,),
            ).fetchone()[0]
            suffix = uuid.uuid4().hex[:8]
            # Cold slice 0: both cold items attempted, one with listings.
            first = _sweep(conn, house, f"cold-0-{suffix}", 0, 0)
            _observe(conn, house, first, COLD_COMMODITY, 1, "commodity", (50, 60), minutes=0)
            _observe(conn, house, first, COLD_ITEM, 2, "item", (500,), minutes=0)
            _observe(conn, house, first, HOT_COMMODITY, 3, "commodity", (10,), minutes=0)
            # Slice 1: the cold items were not attempted at all (no rows); the hot item was.
            second = _sweep(conn, house, f"cold-1-{suffix}", 15, 1)
            _observe(conn, house, second, HOT_COMMODITY, 1, "commodity", (10,), minutes=15)
            # Slice 0 again, an aborted sweep: the cold items are unqueried tail entries and the
            # hot one is incomplete; none of them is an observation.
            aborted = _sweep(conn, house, f"cold-0b-{suffix}", 90, 0)
            _observe(
                conn,
                house,
                aborted,
                COLD_COMMODITY,
                1,
                "commodity",
                (),
                minutes=90,
                coverage="unqueried",
                queried=False,
            )
            _observe(
                conn,
                house,
                aborted,
                COLD_ITEM,
                2,
                "item",
                (),
                minutes=90,
                coverage="unqueried",
                queried=False,
            )
            _observe(
                conn, house, aborted, HOT_COMMODITY, 3, "commodity", (11,), minutes=90, coverage="incomplete"
            )
            # Slice 0 once more: attempted and empty (a real, empty book).
            last = _sweep(conn, house, f"cold-0c-{suffix}", 180, 0)
            _observe(conn, house, last, COLD_COMMODITY, 1, "commodity", (), minutes=180)
            _observe(conn, house, last, COLD_ITEM, 2, "item", (), minutes=180)

            for start_t, end_t in ((None, None), (0, 2**40)):
                commodities = load_commodity_books(
                    conn, HOUSE, start_t=start_t, end_t=end_t, client_build=BUILD
                )
                cold = commodities[COLD_COMMODITY]
                assert [len(book.groups) for book in cold] == [2, 0]  # attempted-empty is a book
                assert [book.sweep_id for book in cold] == [first, last]  # unqueried and skipped: none
                assert [book.sweep_id for book in commodities[HOT_COMMODITY]] == [
                    first,
                    second,
                ]  # not incomplete

            items = load_item_books(conn, HOUSE, [COLD_ITEM], client_build=BUILD)
            assert [(book.sweep_id, len(book.auctions)) for book in items[COLD_ITEM]] == [
                (first, 1),
                (last, 0),
            ]
            assert load_item_books(conn, HOUSE, [COLD_ITEM], client_build=BUILD, start_t=0, end_t=2**40)

            slices = conn.execute(
                "SELECT context -> 'coldSlice' ->> 'index' FROM market_sweeps ORDER BY started_at"
            ).fetchall()
            assert [row[0] for row in slices] == ["0", "1", "0", "0"]
