"""Ordering guarantees for loading item-kind auction books without a SQL-wide sort."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from typing import Any

from crowley.analysis.item_books import ItemBook, item_intervals, load_item_books

HOUSE = "us-normal-alliance"
BUILD = "1.60.1.70938"
LONG = 100_000


class FakeCursor:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self.rows = list(rows)
        self.sql = ""
        self.parameters: tuple[object, ...] = ()

    def execute(self, sql: str, parameters: tuple[object, ...]) -> FakeCursor:
        self.sql = sql
        self.parameters = parameters
        return FakeCursor(self.rows)


def _db_row(
    *,
    sweep_id: int,
    started: int,
    item_id: int,
    result_id: int,
    item_key: str,
    price: int,
    owner: str,
) -> tuple[Any, ...]:
    row_data = {
        "resultID": result_id,
        "quantity": 1,
        "unitPrice": {"numerator": price, "denominator": 1},
        "itemKey": item_key,
        "rawItemKey": {"itemID": item_id},
        "owners": {
            "names": {owner: True},
            "identityKind": "individual",
            "containsOwnerItem": False,
            "containsAccountItem": False,
            "totalNumberOfOwners": 1,
        },
        "timeLeft": {"kind": "band", "minimumSeconds": LONG, "maximumSeconds": LONG + 1},
    }
    query = {"kind": "queried", "startedAt": started, "finishedAt": started + 60}
    return (
        sweep_id,
        "1.60.1",
        "70938",
        "plan-hash",
        item_id,
        True,
        {"kind": "unbounded"},
        query,
        result_id,
        1,
        price,
        1,
        item_key,
        row_data,
    )


def _row_id(record: dict[str, Any]) -> list[int]:
    rows = record["observation"]["rows"]
    return [row["resultID"] for row in rows]


def _loaded_snapshot(books: dict[int, list[ItemBook]]) -> dict[int, list[tuple[Any, ...]]]:
    return {
        item_id: [
            (
                book.t,
                book.sweep_id,
                book.auctions,
                book.record,
            )
            for book in item_books
        ]
        for item_id, item_books in books.items()
    }


def _fixture_rows() -> list[tuple[Any, ...]]:
    early = 10_000
    late = 10_900
    return [
        # Item 3001 has two variants; its cheapest variant-A row disappears in the next book.
        _db_row(
            sweep_id=50,
            started=early,
            item_id=3001,
            result_id=101,
            item_key="variant-A",
            price=90,
            owner="Alice",
        ),
        _db_row(
            sweep_id=50,
            started=early,
            item_id=3001,
            result_id=102,
            item_key="variant-A",
            price=100,
            owner="Bob",
        ),
        _db_row(
            sweep_id=50,
            started=early,
            item_id=3001,
            result_id=103,
            item_key="variant-B",
            price=50,
            owner="Cara",
        ),
        _db_row(
            sweep_id=40,
            started=late,
            item_id=3001,
            result_id=102,
            item_key="variant-A",
            price=100,
            owner="Bob",
        ),
        _db_row(
            sweep_id=40,
            started=late,
            item_id=3001,
            result_id=103,
            item_key="variant-B",
            price=50,
            owner="Cara",
        ),
        # A second item verifies that grouping and the returned item mapping are stable too.
        _db_row(
            sweep_id=50,
            started=early,
            item_id=3002,
            result_id=201,
            item_key="other-item",
            price=200,
            owner="Dee",
        ),
        _db_row(
            sweep_id=40,
            started=late,
            item_id=3002,
            result_id=201,
            item_key="other-item",
            price=200,
            owner="Dee",
        ),
    ]


def _load(rows: Sequence[tuple[Any, ...]]) -> tuple[dict[int, list[ItemBook]], FakeConnection]:
    conn = FakeConnection(rows)
    books = load_item_books(conn, HOUSE, [3001, 3002], client_build=BUILD)
    return books, conn


def test_item_books_and_variant_intervals_ignore_database_row_order() -> None:
    rows = _fixture_rows()
    forward, forward_conn = _load(rows)
    reversed_books, reversed_conn = _load(list(reversed(rows)))
    shuffled_books, shuffled_conn = _load([rows[index] for index in (4, 1, 6, 0, 5, 3, 2)])

    expected = _loaded_snapshot(forward)
    assert _loaded_snapshot(reversed_books) == expected
    assert _loaded_snapshot(shuffled_books) == expected
    assert list(forward) == list(reversed_books) == list(shuffled_books) == [3001, 3002]

    # Per-book auctions and the wire record's rows both use resultID order. Books retain time order.
    assert [book.sweep_id for book in forward[3001]] == [50, 40]
    assert [auction.result_id for auction in forward[3001][0].auctions] == [101, 102, 103]
    assert _row_id(forward[3001][0].record) == [101, 102, 103]
    assert [book.sweep_id for book in forward[3001]] == [book.sweep_id for book in reversed_books[3001]]

    for variant in ("variant-A", "variant-B"):
        scopes = [book.scoped(variant) for book in forward[3001]]
        reversed_scopes = [book.scoped(variant) for book in reversed_books[3001]]
        shuffled_scopes = [book.scoped(variant) for book in shuffled_books[3001]]
        assert reversed_scopes == scopes
        assert shuffled_scopes == scopes
        assert [book.item_keys for book in forward[3001]] == [book.item_keys for book in reversed_books[3001]]

        intervals = item_intervals(forward[3001], variant)
        assert item_intervals(reversed_books[3001], variant) == intervals
        assert item_intervals(shuffled_books[3001], variant) == intervals

    (variant_a_interval,) = item_intervals(forward[3001], "variant-A")
    assert variant_a_interval is not None
    assert variant_a_interval.consumed == ((Fraction(90), 1),)
    (variant_b_interval,) = item_intervals(forward[3001], "variant-B")
    assert variant_b_interval is not None and variant_b_interval.consumed == ()

    for conn in (forward_conn, reversed_conn, shuffled_conn):
        assert "ORDER BY" not in conn.sql.upper()
        assert "h.key = %s" in conn.sql
        assert "mo.kind = 'item'" in conn.sql
        assert "mo.coverage = 'full'" in conn.sql
        assert "mo.source_complete" in conn.sql
        assert "mo.item_id = ANY(%s)" in conn.sql
        assert "ms.client_version || '.' || ms.client_build = %s" in conn.sql
        assert conn.parameters == (HOUSE, [3001, 3002], BUILD)
