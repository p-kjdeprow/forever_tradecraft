"""Ordering guarantees for loading item-kind auction books without a SQL-wide sort."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from typing import Any

from crowley.analysis.item_books import Auction, ItemBook, item_intervals, load_item_books

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


STEP = 900
QUERY_SECONDS = 60


def _wire_row(
    result_id: int,
    price: int,
    quantity: int = 1,
    time_left: int | None = LONG,
    *,
    item_key: str = "3001",
    owner: str | None = None,
    own: bool = False,
) -> dict[str, Any]:
    """Build one item-kind row with explicit auction and owner identity."""

    owner_name = owner or f"seller-{result_id}"
    return {
        "resultID": result_id,
        "quantity": quantity,
        "unitPrice": {"numerator": price, "denominator": 1},
        "itemKey": item_key,
        "rawItemKey": {"itemID": int(item_key)},
        "owners": {
            "names": {owner_name: True},
            "identityKind": "individual",
            "containsOwnerItem": own,
            "containsAccountItem": False,
            "totalNumberOfOwners": 1,
        },
        "timeLeft": (
            {"kind": "band", "minimumSeconds": time_left, "maximumSeconds": time_left + 1}
            if time_left is not None
            else {"kind": "unknown"}
        ),
    }


def _interval_book(
    index: int,
    rows: list[dict[str, Any]],
    *,
    started_at: int | None = None,
    plan_hash: str = "plan",
) -> ItemBook:
    started = index * STEP if started_at is None else started_at
    observation = {
        "kind": "item",
        "coverage": "full",
        "sourceComplete": True,
        "cutoff": {"kind": "unbounded"},
        "query": {
            "kind": "queried",
            "startedAt": started,
            "finishedAt": started + QUERY_SECONDS,
        },
        "itemID": 3001,
        "rows": rows,
    }
    auctions = tuple(
        Auction(
            row["resultID"],
            Fraction(row["unitPrice"]["numerator"], row["unitPrice"]["denominator"]),
            row["quantity"],
            row["itemKey"],
            next(iter(row["owners"]["names"])),
            row["owners"]["containsOwnerItem"],
        )
        for row in rows
    )
    record = {
        "observation": observation,
        "envelope": {
            "house": {"key": HOUSE},
            "context": {"clientVersion": "1.60.1", "clientBuild": "70938"},
            "planHash": plan_hash,
        },
    }
    return ItemBook(float(started), 3001, index + 1, auctions, record)


def test_item_intervals_walk_skips_possible_expiry_and_stops_at_unknown_time():
    cheap_sale = _wire_row(11, 90)
    expiring = _wire_row(12, 91, time_left=0)
    sold_out = _wire_row(13, 92, quantity=3, owner="Trader")
    unknown_time = _wire_row(14, 93, time_left=None)
    after_sale = _wire_row(15, 94)
    stays = _wire_row(1, 100)
    before = _interval_book(0, [cheap_sale, expiring, sold_out, unknown_time, after_sale, stays])
    after = _interval_book(1, [stays])

    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None
    assert interval.consumed == ((Fraction(90), 1), (Fraction(92), 3))
    assert interval.dt == STEP and interval.exposure == STEP

    (skipped,) = item_intervals([_interval_book(0, [expiring, stays]), after], "3001")
    assert skipped is not None and skipped.consumed == ()


def test_item_intervals_reconcile_same_price_relisting_by_owner_quantity():
    stays = _wire_row(1, 100)
    before = _interval_book(0, [_wire_row(51, 90, quantity=12, owner="Seller"), stays])
    after = _interval_book(1, [_wire_row(52, 90, quantity=10, owner="Seller"), stays])
    (sold,) = item_intervals([before, after], "3001")
    assert sold is not None and sold.consumed == ((Fraction(90), 2),)

    prior = _interval_book(
        0,
        [
            _wire_row(51, 90, quantity=12, owner="Seller"),
            _wire_row(55, 95, owner="Seller"),
            stays,
        ],
    )
    relisted = _interval_book(
        1,
        [
            _wire_row(53, 90, quantity=12, owner="Seller"),
            _wire_row(54, 95, owner="Seller"),
            stays,
        ],
    )
    (unchanged,) = item_intervals([prior, relisted], "3001")
    assert unchanged is not None and unchanged.consumed == ()


def test_item_intervals_reject_reposts_grouped_scopes_and_untouched_cheapest_rows():
    stays = _wire_row(1, 100)
    before = _interval_book(0, [_wire_row(61, 30, quantity=6, owner="Poster"), stays])
    reposted = _interval_book(1, [_wire_row(62, 37, quantity=6, owner="Poster"), stays])
    (repost_interval,) = item_intervals([before, reposted], "3001")
    assert repost_interval is not None and repost_interval.consumed == ()

    rows = [_wire_row(21, 90), _wire_row(22, 95), stays]
    (blocked,) = item_intervals([_interval_book(0, rows), _interval_book(1, [rows[0], stays])], "3001")
    assert blocked is not None and blocked.consumed == ()

    grouped = _wire_row(72, 90, quantity=8)
    grouped["owners"].update(identityKind="grouped", totalNumberOfOwners=3, names={"Seller": True, "A": True})
    assert item_intervals([_interval_book(0, [grouped]), _interval_book(1, [])], "3001") == [None]


def test_item_intervals_require_stable_metadata_and_nonoverlapping_bounded_gaps():
    rows = [_wire_row(1, 100)]
    first = _interval_book(0, rows)
    assert item_intervals([first, _interval_book(1, rows, started_at=STEP + 2400)], "3001") == [None]
    assert item_intervals([first, _interval_book(1, rows, plan_hash="other")], "3001") == [None]
    assert item_intervals([first, _interval_book(1, rows, started_at=30)], "3001") == [None]


def test_item_intervals_count_all_units_in_a_genuine_sellout():
    before = _interval_book(0, [_wire_row(81, 90, quantity=7, owner="Gone"), _wire_row(1, 100)])
    after = _interval_book(1, [_wire_row(1, 100)])
    (interval,) = item_intervals([before, after], "3001")
    assert interval is not None and interval.consumed == ((Fraction(90), 7),)
