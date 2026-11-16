from contextlib import nullcontext
from fractions import Fraction

import pytest

from crowley.analysis.books import Book, Group, bottom_walk, load_commodity_books, valid_intervals


def _group(result_id, unit, quantity, time_left=10_000):
    return Group(result_id, Fraction(unit), quantity, time_left)


def _book(t, *groups, ceiling=None, item_id=1, sweep_id=None):
    return Book(
        t,
        list(groups),
        None if ceiling is None else Fraction(ceiling),
        item_id=item_id,
        sweep_id=sweep_id,
    )


def test_bottom_walk_stops_at_untouched_group_and_respects_expiry_and_ceiling():
    before = _book(0, _group("a", 1, 10), _group("b", 2, 20), _group("c", 3, 30), ceiling=2)
    after = _book(60, _group("a", 1, 0), _group("b", 2, 5), _group("new", 1.5, 10), ceiling=2)
    walk = bottom_walk(before, after)
    assert walk.consumed == [(Fraction(1), 10), (Fraction(2), 15)]
    assert walk.reached_ceiling is True

    untouched = _book(0, _group("a", 1, 10), _group("b", 2, 20))
    cancellation = _book(60, _group("a", 1, 10))
    assert bottom_walk(untouched, cancellation).consumed == []

    expiring = _book(0, _group("a", 1, 10, time_left=30), _group("b", 2, 20))
    after_expiry = _book(60, _group("b", 2, 5))
    assert bottom_walk(expiring, after_expiry).consumed == [(Fraction(2), 15)]


def test_commodity_intervals_exclude_outages_and_nonpositive_time():
    books = [
        _book(0, _group("a", 1, 10), sweep_id=1),
        _book(1260, _group("a", 1, 9), sweep_id=2),
        _book(2521, _group("a", 1, 8), sweep_id=3),
        _book(2521, _group("a", 1, 7), sweep_id=4),
    ]
    intervals = valid_intervals(books)
    assert intervals[0] is not None and intervals[0].walk.consumed == [(Fraction(1), 1)]
    assert intervals[1:] == [None, None]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, migration, rows=()):
        self.migration = migration
        self.rows = rows

    def transaction(self):
        return nullcontext()

    def execute(self, sql, _params=()):
        if "schema_migrations" in sql:
            return _Cursor((1,) if self.migration else None)
        return _Cursor(self.rows)


def _loader_row(seconds=100):
    return (
        1,
        100,
        7,
        "commodity",
        "full",
        {"kind": "unbounded"},
        {"kind": "queried", "startedAt": 100},
        1,
        2,
        5,
        1,
        {"timeLeft": {"kind": "exact", "seconds": seconds}, "owners": {"totalNumberOfOwners": "1"}},
    )


def test_commodity_loader_requires_applied_migration_and_corrected_time_left():
    with pytest.raises(RuntimeError, match="migration 003"):
        load_commodity_books(_Connection(False), "house")
    with pytest.raises(ValueError, match="exceeds"):
        load_commodity_books(_Connection(True, [_loader_row(172_801)]), "house")
    loaded = load_commodity_books(_Connection(True, [_loader_row()]), "house")
    assert loaded[7][0].groups[0].unit == Fraction(5)


def test_commodity_loader_requires_query_time_and_matching_cutoff():
    missing_started_at = list(_loader_row())
    missing_started_at[6] = {"kind": "queried"}
    with pytest.raises(ValueError, match="startedAt"):
        load_commodity_books(_Connection(True, [tuple(missing_started_at)]), "house")

    mismatch = list(_loader_row())
    mismatch[4] = "bounded"
    with pytest.raises(ValueError, match="price cutoff"):
        load_commodity_books(_Connection(True, [tuple(mismatch)]), "house")
