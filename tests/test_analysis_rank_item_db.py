"""Database-gated house, build, coverage and window scoping of the item-kind book loader."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.analysis.item_books import load_item_books
from crowley.server.db import migrate
from tests.test_analysis_rank_db import BASE, DB_URL, _house, _sweep

pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]


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
        row = {
            "resultID": item_id * 100 + ordinal,
            "quantity": 1,
            "unitPrice": {"numerator": price, "denominator": 1},
            "itemKey": str(item_id),
            "owners": {"names": {"A": True}, "identityKind": "individual", "totalNumberOfOwners": 1},
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
                item_id * 100 + ordinal,
                str(item_id),
                Jsonb({"itemID": item_id}),
                price,
                Jsonb(row),
            ),
        )


def test_item_book_loader_is_scoped_to_house_build_coverage_kind_and_window() -> None:
    assert DB_URL is not None
    suffix = uuid.uuid4().hex[:8]
    t0 = int(BASE.timestamp())
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house_a = _house(conn, "alliance")
            house_b = _house(conn, "horde")
            old_a = _sweep(conn, house_a, f"old-a-{suffix}", "1.60.1.70450", 0)
            new_a = _sweep(conn, house_a, f"new-a-{suffix}", "1.60.1.70862", 60)
            later_a = _sweep(conn, house_a, f"later-a-{suffix}", "1.60.1.70862", 120)
            new_b = _sweep(conn, house_b, f"new-b-{suffix}", "1.60.1.70862", 60)

            _observe_item(conn, house_a, old_a, 5001, started=t0, prices=(111,))  # other build
            _observe_item(conn, house_a, new_a, 5001, started=t0 + 3600, prices=(700, 500))
            _observe_item(conn, house_a, later_a, 5001, started=t0 + 7200, prices=(450,))
            _observe_item(conn, house_a, new_a, 5002, started=t0 + 3600, coverage="bounded", prices=(9,))
            _observe_item(conn, house_a, new_a, 5003, started=t0 + 3600, coverage="incomplete", prices=(9,))
            _observe_item(conn, house_a, new_a, 5004, started=t0 + 3600, kind="commodity", prices=(9,))
            _observe_item(
                conn, house_a, new_a, 5005, started=t0 + 3600, prices=()
            )  # observed, nothing listed
            _observe_item(conn, house_b, new_b, 5001, started=t0 + 3600, prices=(1,))  # other house

            wanted = [5001, 5002, 5003, 5004, 5005, 5006, 5007]
            build = "1.60.1.70862"
            books = load_item_books(conn, "us-normal-alliance", wanted, client_build=build)
            assert set(books) == {5001, 5005}  # bounded, incomplete, commodity and unscanned are absent
            assert [b.sweep_id for b in books[5001]] == [new_a, later_a]  # no other build, no other house
            assert sorted(a.unit for a in books[5001][0].auctions) == [500, 700]
            assert books[5001][0].t == t0 + 3600 and books[5005][0].auctions == ()
            assert books[5001][0].record["envelope"]["house"] == {"key": "us-normal-alliance"}

            horde = load_item_books(conn, "us-normal-horde", wanted, client_build=build)
            assert {k: [a.unit for a in v[0].auctions] for k, v in horde.items()} == {5001: [1]}
            old = load_item_books(conn, "us-normal-alliance", wanted, client_build="1.60.1.70450")
            assert {k: [a.unit for a in v[0].auctions] for k, v in old.items()} == {5001: [111]}

            window = load_item_books(
                conn, "us-normal-alliance", wanted, client_build=build, start_t=t0 + 7000, end_t=t0 + 7300
            )
            assert [b.sweep_id for b in window[5001]] == [later_a]
            one_sweep = load_item_books(
                conn, "us-normal-alliance", wanted, client_build=build, sweep_id=new_a
            )
            assert [b.sweep_id for b in one_sweep[5001]] == [new_a]
            assert load_item_books(conn, "us-normal-alliance", [], client_build=build) == {}
            with pytest.raises(ValueError, match="full client build"):
                load_item_books(conn, "us-normal-alliance", wanted, client_build="70862")
