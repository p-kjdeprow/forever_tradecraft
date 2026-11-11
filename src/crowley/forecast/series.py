"""Build one immutable commodity time series and expose its past-only windows."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from crowley.analysis.books import Book, Interval, valid_intervals
from crowley.analysis.metrics import TrailingDemand, trailing_demand


@dataclass(frozen=True)
class Series:
    """One item's ordered books, lowest listings, and adjacent valid intervals."""

    item_id: int
    t: tuple[float, ...]
    lowest: tuple[int | None, ...]
    intervals: tuple[Interval | None, ...]


def build_series(item_id: int, books: Sequence[Book]) -> Series:
    """Validate and materialize a commodity series for ``item_id``."""

    times: list[float] = []
    lowest: list[int | None] = []
    previous_t: float | None = None
    ordered = list(books)
    for book in ordered:
        if book.item_id != item_id:
            raise ValueError(f"book item_id {book.item_id!r} does not match series item {item_id}")
        if previous_t is not None and book.t <= previous_t:
            raise ValueError("series book times must be strictly increasing")
        if book.groups:
            minimum = min(group.unit for group in book.groups)
            if minimum.denominator != 1:
                raise ValueError(f"commodity {item_id} has a non-integral unit price {minimum}")
            lowest.append(int(minimum))
        else:
            lowest.append(None)
        times.append(book.t)
        previous_t = book.t
    return Series(item_id, tuple(times), tuple(lowest), tuple(valid_intervals(ordered)))


def index_at(s: Series, o: float) -> int | None:
    """Return the last book at or before the origin, or ``None`` before the series."""

    index = bisect_right(s.t, o) - 1
    return None if index < 0 else index


def window(s: Series, start: float, end: float) -> tuple[int, int] | None:
    """Return the inclusive first and last indices in a time window."""

    first = bisect_left(s.t, start)
    after = bisect_right(s.t, end)
    if first >= after:
        return None
    return first, after - 1


def window_demand(s: Series, start: float, end: float) -> TrailingDemand:
    """Aggregate only the adjacent intervals whose two books are in the inclusive window."""

    bounds = window(s, start, end)
    if bounds is None or bounds[0] == bounds[1]:
        return TrailingDemand(None, 0, 0.0)
    first, last = bounds
    return trailing_demand(s.intervals[first:last])


__all__ = ["Series", "build_series", "index_at", "window", "window_demand"]
