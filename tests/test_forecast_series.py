"""Series construction and exact trailing-demand parity tests."""

from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

import pytest

from crowley.advisor.estimates import SnapshotEstimates
from crowley.analysis.books import Book, Group, valid_intervals
from crowley.analysis.metrics import trailing_demand
from crowley.forecast.series import build_series, index_at, window, window_demand


def group(result_id: str, unit: int, quantity: int, time_left: int = 10_000) -> Group:
    return Group(result_id, Fraction(unit), quantity, time_left, 1)


def book(t: float, *groups: Group, item_id: int = 1) -> Book:
    return Book(t, list(groups), item_id=item_id, sweep_id=int(t))


def test_build_series_rejects_wrong_item_and_non_increasing_times() -> None:
    with pytest.raises(ValueError, match="item_id"):
        build_series(1, [book(0, group("a", 1, 1), item_id=2)])
    with pytest.raises(ValueError, match="strictly increasing"):
        build_series(1, [book(1, group("a", 1, 1)), book(1, group("b", 1, 1))])


def test_build_series_rejects_non_integral_lowest() -> None:
    with pytest.raises(ValueError, match="non-integral"):
        build_series(1, [Book(0, [Group("a", Fraction(3, 2), 1, 10_000)], item_id=1)])


def test_index_and_inclusive_windows_do_not_read_future_books() -> None:
    series = build_series(1, [book(0, group("a", 1, 4)), book(10, group("a", 1, 3)), book(20)])
    assert index_at(series, -1) is None
    assert index_at(series, 10) == 1
    assert index_at(series, 15) == 1
    assert window(series, 10, 10) == (1, 1)
    assert window(series, 11, 19) is None
    assert window_demand(series, 0, 10).covered_seconds == 10
    assert window_demand(series, 20, 30).usable_intervals == 0


def test_trailing_demand_matches_snapshot_rate_with_gaps_expiry_and_bounds() -> None:
    books = [
        book(0, group("a", 10, 5)),
        book(30, group("a", 10, 3)),  # sub-hour coverage and two consumed units
        book(2_000, group("a", 10, 0, time_left=1)),  # gap; expiry is not consumption
        book(2_030, group("b", 8, 4)),
        book(2_060, group("b", 8, 1)),  # three consumed units after the gap
        book(2_090, group("b", 8, 1)),
    ]
    series = build_series(1, books)
    start = 30
    end = 3_630
    interval_demand = trailing_demand(valid_intervals([item for item in books if start <= item.t <= end]))
    series_demand = window_demand(series, start, end)
    assert series_demand == interval_demand

    state = SimpleNamespace(
        as_of_t=3_630.0,
        lookback_hours=1.0,
        sweep_id=books[-1].sweep_id,
        books={1: books},
        item_books={},
    )
    estimate = SnapshotEstimates(state).estimate(1)
    assert estimate is not None
    assert estimate.units_per_hour == series_demand.units_per_hour
    assert estimate.demand_intervals == series_demand.usable_intervals
    assert estimate.covered_hours == series_demand.covered_seconds / 3600


def test_expiring_before_group_is_not_counted_in_a_valid_interval() -> None:
    books = [
        book(0, group("a", 10, 5)),
        book(30, group("a", 10, 3), group("expiring", 8, 5, time_left=10)),
        book(60, group("a", 10, 3)),
        book(90, group("a", 10, 1)),
    ]
    demand = window_demand(build_series(1, books), 0, 90)
    assert demand.usable_intervals == 3
    assert demand.covered_seconds == 90
    assert demand.units_per_hour == Fraction(4 * 3600, 90)


def test_single_book_window_has_zero_coverage() -> None:
    series = build_series(1, [book(100, group("a", 1, 1))])
    assert window_demand(series, 100, 100).units_per_hour is None
    assert window_demand(series, 100, 100).usable_intervals == 0
    assert window_demand(series, 100, 100).covered_seconds == 0.0
