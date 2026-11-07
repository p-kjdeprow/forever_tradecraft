"""Construction of the immutable market view used by strategies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crowley.analysis.books import Book, Interval, valid_intervals
from crowley.core.auction_rules import AuctionHouseRules

from .api import ItemFacts, ItemSnapshot, Level, MarketView, Recipe
from .fees import deposit
from .numbers import as_fraction
from .recipes import validate_recipes
from .statistics import StatisticsPoint, precompute_statistics


def _as_item_books(books: Mapping[int, Sequence[Book]] | Sequence[Book]) -> dict[int, list[Book]]:
    if isinstance(books, Mapping):
        grouped = {
            int(item_id): sorted(list(item_books), key=lambda book: (book.t, str(book.sweep_id)))
            for item_id, item_books in books.items()
        }
    else:
        grouped: dict[int, list[Book]] = {}
        for book in books:
            if book.item_id is None:
                raise ValueError("a sequence of books must carry item_id on every Book")
            grouped.setdefault(int(book.item_id), []).append(book)
        for item_books in grouped.values():
            item_books.sort(key=lambda book: (book.t, str(book.sweep_id)))
    return grouped


def _whole_seconds(value: float | int) -> int:
    """Query timestamps are whole epoch seconds; refuse anything else instead of truncating."""

    if value != int(value):
        raise ValueError(f"book time must be whole seconds, got {value!r}")
    return int(value)


def _sweep_key(book: Book) -> Any:
    # Synthetic callers from the first engine revision did not have sweep ids.
    return book.sweep_id if book.sweep_id is not None else ("timestamp", book.t)


@dataclass(frozen=True)
class _Sweep:
    key: Any
    t: float
    indices: Mapping[int, int]


class Market:
    """Book, sweep, statistics, and static-data owner for a backtest."""

    def __init__(
        self,
        books: Mapping[int, Sequence[Book]] | Sequence[Book],
        statistics: Mapping[int, Sequence[StatisticsPoint]] | None,
        facts: Mapping[int, ItemFacts],
        recipes: Sequence[Recipe],
        *,
        rules: AuctionHouseRules,
    ) -> None:
        if not isinstance(rules, AuctionHouseRules):
            raise TypeError("rules must be AuctionHouseRules")
        self.rules = rules
        self.books = _as_item_books(books)
        if not statistics:
            statistics = precompute_statistics(self.books)
        self.statistics: Mapping[int, Sequence[StatisticsPoint]] = statistics
        self._facts = {int(item_id): fact for item_id, fact in facts.items()}
        # Keep direct ExecutionContext users on the same validated recipe
        # contract as run_backtest; malformed requirement ids must never reach
        # a Craft mutation path.
        self._recipes = validate_recipes(tuple(recipes))
        for item_books in self.books.values():
            for book in item_books:
                for group in book.groups:
                    unit = as_fraction(group.unit)
                    if unit.denominator != 1:
                        raise ValueError(
                            f"commodity price for item {book.item_id} is not integral: {group.unit}"
                        )

        sweep_books: dict[Any, dict[int, int]] = {}
        sweep_times: dict[Any, int] = {}
        for item_id, item_books in self.books.items():
            for index, book in enumerate(item_books):
                key = _sweep_key(book)
                by_item = sweep_books.setdefault(key, {})
                if item_id in by_item:
                    raise ValueError(f"item {item_id} has multiple books in sweep {key!r}")
                by_item[item_id] = index
                book_t = _whole_seconds(book.t)
                sweep_times[key] = max(sweep_times[key], book_t) if key in sweep_times else book_t
        ordered_keys = sorted(sweep_books, key=lambda key: (sweep_times[key], str(key)))
        self.sweeps = tuple(_Sweep(key, sweep_times[key], dict(sweep_books[key])) for key in ordered_keys)
        self._intervals: dict[tuple[int, int], Interval | None] = {}
        self._previous_step: dict[tuple[int, int], int] = {}
        self._next_step: dict[tuple[int, int], int] = {}
        for item_id, item_books in self.books.items():
            cached = valid_intervals(item_books)
            item_steps = [step for step, sweep in enumerate(self.sweeps) if item_id in sweep.indices]
            for position, step in enumerate(item_steps):
                item_index = self.sweeps[step].indices[item_id]
                if position:
                    previous_step = item_steps[position - 1]
                    self._intervals[(item_id, step)] = cached[item_index - 1]
                    self._previous_step[(item_id, step)] = previous_step
                else:
                    self._intervals[(item_id, step)] = None
                if position + 1 < len(item_steps):
                    self._next_step[(item_id, step)] = item_steps[position + 1]
        self._current_t: float | None = None
        self._current_sweep_index = 0
        self._purchased: Mapping[tuple[int, Any], int] = {}
        self._validate_statistics()

    def _validate_statistics(self) -> None:
        for item_id, item_books in self.books.items():
            points = self.statistics.get(item_id)
            if points is None:
                raise ValueError(f"missing indexed statistics for item {item_id}")
            if len(points) != len(item_books):
                raise ValueError(f"statistics for item {item_id} do not cover every book")

    @property
    def times(self) -> tuple[float, ...]:
        return tuple(sweep.t for sweep in self.sweeps)

    @property
    def sweep_ids(self) -> tuple[Any, ...]:
        return tuple(sweep.key for sweep in self.sweeps)

    def books_at(self, sweep_index: int) -> Mapping[int, int]:
        return self.sweeps[sweep_index].indices

    def book_index(self, item_id: int, sweep_index: int) -> int | None:
        if not 0 <= sweep_index < len(self.sweeps):
            return None
        return self.sweeps[sweep_index].indices.get(item_id)

    def book(self, item_id: int, index: int) -> Book:
        return self.books[item_id][index]

    def interval(self, item_id: int, sweep_index: int) -> Interval | None:
        return self._intervals.get((item_id, sweep_index))

    def interval_start_step(self, item_id: int, sweep_index: int) -> int | None:
        return self._previous_step.get((item_id, sweep_index))

    def next_interval(self, item_id: int, sweep_index: int) -> tuple[int, Interval | None] | None:
        next_step = self._next_step.get((item_id, sweep_index))
        if next_step is None:
            return None
        return next_step, self._intervals.get((item_id, next_step))

    def set_step(self, t: float, sweep_index: int, purchased: Mapping[tuple[int, Any], int]) -> None:
        self._current_t = t
        self._current_sweep_index = sweep_index
        self._purchased = purchased

    def _purchased_for(self, item_id: int, result_id: Any) -> int:
        return max(0, int(self._purchased.get((item_id, result_id), 0)))

    def _snapshot(self, item_id: int, index: int) -> ItemSnapshot:
        book = self.books[item_id][index]
        quantities: list[Level] = []
        for group in sorted(book.groups, key=lambda group: (group.unit, str(group.result_id))):
            if book.ceiling is not None and group.unit > book.ceiling:
                continue
            quantity = max(0, group.quantity - self._purchased_for(item_id, group.result_id))
            if quantity:
                unit = as_fraction(group.unit)
                if unit.denominator != 1:
                    raise ValueError(f"commodity price for item {item_id} is not integral: {unit}")
                quantities.append(Level(unit.numerator, quantity))
        point = self.statistics[item_id][index]
        fair = point.fair.value
        fair_band = (
            None if point.fair.p25 is None or point.fair.p75 is None else (point.fair.p25, point.fair.p75)
        )
        return ItemSnapshot(
            item_id=item_id,
            t=_whole_seconds(book.t),
            levels=tuple(quantities),
            visible_min=quantities[0].unit_price if quantities else None,
            visible_units=sum(level.quantity for level in quantities),
            fair=fair,
            fair_band=fair_band,
            fair_n=point.fair.n,
            trend_per_day=point.fair.trend_per_day,
            demand_units_per_day=point.demand.purchased_units_per_day,
            demand_eligible_seconds=point.demand.eligible_covered_seconds,
            volatility_per_hour=point.risk.volatility_per_hour,
            half_life_hours=point.risk.half_life_hours,
        )

    def view(self) -> MarketView:
        if self._current_t is None:
            raise RuntimeError("market step has not been selected")
        snapshots = {
            item_id: self._snapshot(item_id, index)
            for item_id, index in self.books_at(self._current_sweep_index).items()
        }
        return _MarketView(
            t=int(self._current_t),
            sweep_index=self._current_sweep_index,
            snapshots=snapshots,
            facts=self._facts,
            recipes=self._recipes,
            rules=self.rules,
        )

    def facts(self, item_id: int) -> ItemFacts | None:
        return self._facts.get(item_id)

    def deposit(self, item_id: int, quantity: int, duration_hours: int) -> int:
        fact = self._facts.get(item_id)
        if fact is None:
            raise ValueError(f"unknown item facts: {item_id}")
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        return deposit(fact.sell_price, quantity, duration_hours, self.rules)


class _MarketView:
    def __init__(
        self,
        *,
        t: int,
        sweep_index: int,
        snapshots: Mapping[int, ItemSnapshot],
        facts: Mapping[int, ItemFacts],
        recipes: Sequence[Recipe],
        rules: AuctionHouseRules,
    ) -> None:
        self.t = t
        self.sweep_index = sweep_index
        self._snapshots = dict(snapshots)
        self._facts = dict(facts)
        self._recipes = tuple(recipes)
        self.rules = rules

    def items(self) -> Mapping[int, ItemSnapshot]:
        return self._snapshots.copy()

    def facts(self, item_id: int) -> ItemFacts | None:
        return self._facts.get(item_id)

    def recipes(self) -> Sequence[Recipe]:
        return self._recipes

    def deposit(self, item_id: int, quantity: int, duration_hours: int) -> int:
        fact = self._facts.get(item_id)
        if fact is None:
            raise ValueError(f"unknown item facts: {item_id}")
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        return deposit(fact.sell_price, quantity, duration_hours, self.rules)


__all__ = ["Market"]
