"""The price/demand estimate seam.

Evaluators ask an ``EstimateProvider`` for what an item costs now, what it is worth and how fast
it sells, and never read books for those themselves.  Today's provider, ``SnapshotEstimates``,
reports the current snapshot: the lowest listing of the latest sweep, the strictly-past 24 h fair
value and the observed bottom-walk units/h.  Phase 2 replaces it with a forecast provider
(walk-forward validated) that returns the same ``PriceEstimate``; no evaluator changes.

Historical replays (flip, crafting sale probability) are deliberately not behind this seam: they
measure outcomes on past books and are the evidence a forecast would be scored against.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from crowley.analysis.books import Book, valid_intervals
from crowley.analysis.metrics import FAIR_MIN_BOOKS, demand_metrics, fair_value
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .state import MarketState


@dataclass(frozen=True)
class PriceEstimate:
    item_id: int
    kind: str  # "commodity" | "item"
    lowest: int | None  # lowest current listing (copper per unit); None when nothing is listed
    listed_units: int
    fair: Fraction | None  # strictly-past 24 h fair value (commodities only)
    fair_n: int
    units_per_hour: Fraction | None  # observed bottom-walk demand (commodities only)
    demand_intervals: int  # usable consecutive-book intervals behind units_per_hour
    covered_hours: float


class EstimateProvider(Protocol):
    def estimate(self, item_id: int) -> PriceEstimate | None:
        """The estimate for an item observed in the latest sweep, else None."""
        ...


class SnapshotEstimates:
    """The current snapshot as an estimate: nothing is forecast."""

    def __init__(self, state: MarketState) -> None:
        self._state = state
        self._cache: dict[int, PriceEstimate | None] = {}

    def estimate(self, item_id: int) -> PriceEstimate | None:
        if item_id not in self._cache:
            self._cache[item_id] = self._build(item_id)
        return self._cache[item_id]

    def _build(self, item_id: int) -> PriceEstimate | None:
        state = self._state
        books = state.books.get(item_id)
        if books:
            return self._commodity(item_id, books)
        return self._item(item_id)

    def _commodity(self, item_id: int, books: Sequence[Book]) -> PriceEstimate | None:
        state = self._state
        start = state.as_of_t - state.lookback_hours * SECONDS_PER_HOUR
        window = [book for book in books if start <= book.t <= state.as_of_t]
        if not window or window[-1].sweep_id != state.sweep_id:
            return None
        latest = window[-1]
        prices = [group.unit for group in latest.groups]
        lowest = min(prices) if prices else None
        if lowest is not None and lowest.denominator != 1:
            raise ValueError(f"commodity {item_id} has a non-integral unit price {lowest}")
        fair = fair_value(window, include_trend=False, min_books=FAIR_MIN_BOOKS)
        intervals = valid_intervals(window)
        usable = sum(interval is not None for interval in intervals)
        demand = demand_metrics(window, intervals)
        per_day = demand["purchased_units_per_day"]
        return PriceEstimate(
            item_id=item_id,
            kind="commodity",
            lowest=None if lowest is None else int(lowest),
            listed_units=sum(group.quantity for group in latest.groups),
            fair=fair["value"],
            fair_n=fair["n"],
            units_per_hour=None if per_day is None else Fraction(per_day) / 24,
            demand_intervals=usable,
            covered_hours=float(demand["covered_seconds"]) / SECONDS_PER_HOUR,
        )

    def _item(self, item_id: int) -> PriceEstimate | None:
        state = self._state
        books = [book for book in state.item_books.get(item_id, ()) if book.t <= state.as_of_t]
        if not books or books[-1].sweep_id != state.sweep_id:
            return None
        latest = books[-1]
        keys = sorted(latest.item_keys)
        if len(keys) > 1:
            raise ValueError(f"item {item_id} latest book carries itemKeys {keys}; variants are never mixed")
        buyable = [auction for auction in latest.auctions if not auction.own]
        lowest = min((auction.unit for auction in buyable), default=None)
        if lowest is not None and lowest.denominator != 1:
            raise ValueError(f"item {item_id} has a non-integral buyout {lowest}")
        return PriceEstimate(
            item_id=item_id,
            kind="item",
            lowest=None if lowest is None else int(lowest),
            listed_units=sum(auction.quantity for auction in buyable),
            fair=None,
            fair_n=0,
            units_per_hour=None,
            demand_intervals=0,
            covered_hours=0.0,
        )
