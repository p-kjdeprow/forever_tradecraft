"""Typed strictly-past market statistics."""

from __future__ import annotations

import math
import os
from bisect import bisect_left, insort
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from heapq import heappop, heappush
from multiprocessing import get_all_start_methods, get_context
from statistics import median

from crowley.analysis.books import Book, valid_intervals
from crowley.analysis.time_to_sale import _reference

from .numbers import as_fraction


@dataclass(frozen=True)
class FairStatistics:
    value: Fraction | None
    p25: Fraction | None
    p75: Fraction | None
    trend_per_day: float | None
    n: int


@dataclass(frozen=True)
class DemandStatistics:
    covered_seconds: float
    covered_days: Fraction
    purchased_units: int
    purchased_copper: Fraction
    purchased_units_per_day: Fraction | None
    copper_per_day: Fraction | None
    eligible_covered_seconds: float


@dataclass(frozen=True)
class RiskStatistics:
    volatility_per_hour: float | None
    eligible_covered_seconds: float
    reference_intervals: int
    mean_reversion_b: float | None
    half_life_hours: float | None
    mean_reversion_intervals: int


@dataclass(frozen=True)
class StatisticsPoint:
    fair: FairStatistics
    demand: DemandStatistics
    risk: RiskStatistics
    books: int


class _RollingMedian:
    """An exact incremental median over the active rolling slope set."""

    def __init__(self) -> None:
        self._lower: list[tuple[float, int]] = []
        self._upper: list[tuple[float, int]] = []
        self._alive: list[bool] = []
        self._side: list[bool] = []
        self._lower_live = 0
        self._upper_live = 0

    def _clean(self, heap: list[tuple[float, int]]) -> None:
        while heap and not self._alive[heap[0][1]]:
            heappop(heap)

    def _rebalance(self) -> None:
        self._clean(self._lower)
        self._clean(self._upper)
        while self._lower_live > self._upper_live + 1:
            neg_value, pair_id = heappop(self._lower)
            heappush(self._upper, (-neg_value, pair_id))
            self._side[pair_id] = True
            self._lower_live -= 1
            self._upper_live += 1
            self._clean(self._lower)
        while self._upper_live > self._lower_live:
            value, pair_id = heappop(self._upper)
            heappush(self._lower, (-value, pair_id))
            self._side[pair_id] = False
            self._upper_live -= 1
            self._lower_live += 1
            self._clean(self._upper)

    def add_many(self, values: Sequence[float]) -> list[int]:
        pair_ids: list[int] = []
        self._clean(self._lower)
        for value in values:
            pair_id = len(self._alive)
            pair_ids.append(pair_id)
            self._alive.append(True)
            self._side.append(False)
            if not self._lower or value <= -self._lower[0][0]:
                heappush(self._lower, (-value, pair_id))
                self._lower_live += 1
            else:
                heappush(self._upper, (value, pair_id))
                self._side[pair_id] = True
                self._upper_live += 1
        self._rebalance()
        return pair_ids

    def remove_many(self, pair_ids: Sequence[int]) -> None:
        for pair_id in pair_ids:
            if not self._alive[pair_id]:
                continue
            self._alive[pair_id] = False
            if self._side[pair_id]:
                self._upper_live -= 1
            else:
                self._lower_live -= 1
        self._rebalance()

    def median(self) -> float | None:
        if not self._lower_live and not self._upper_live:
            return None
        self._clean(self._lower)
        self._clean(self._upper)
        if self._lower_live > self._upper_live:
            return -self._lower[0][0]
        return (-self._lower[0][0] + self._upper[0][0]) / 2


def _books_mapping(books: Mapping[int, Sequence[Book]] | Sequence[Book]) -> dict[int, list[Book]]:
    if isinstance(books, Mapping):
        return {
            int(item_id): sorted(list(item_books), key=lambda book: (book.t, str(book.sweep_id)))
            for item_id, item_books in books.items()
        }
    grouped: dict[int, list[Book]] = {}
    for book in books:
        if book.item_id is None:
            raise ValueError("a sequence of books must carry item_id on every Book")
        grouped.setdefault(int(book.item_id), []).append(book)
    for item_books in grouped.values():
        item_books.sort(key=lambda book: (book.t, str(book.sweep_id)))
    return grouped


def _percentile_sorted(values: Sequence[Fraction], probability: Fraction) -> Fraction:
    position = probability * (len(values) - 1)
    lower = position.numerator // position.denominator
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _fair_series(books: Sequence[Book]) -> list[FairStatistics]:
    references = [_reference(book) for book in books]
    log_references = [None if reference is None else math.log(float(reference)) for reference in references]
    active: list[int] = []
    sorted_references: list[Fraction] = []
    slopes = _RollingMedian()
    pairs_by_observation: list[list[int]] = [[] for _ in books]
    result: list[FairStatistics] = []

    def slope(left: int, right: int) -> float:
        return (log_references[right] - log_references[left]) * 86400 / (books[right].t - books[left].t)

    for index, book in enumerate(books):
        window_start = book.t - 86400
        while active and books[active[0]].t < window_start:
            expired = active.pop(0)
            slopes.remove_many(pairs_by_observation[expired])
            pairs_by_observation[expired].clear()
            reference = references[expired]
            position = bisect_left(sorted_references, reference)
            sorted_references.pop(position)
        prior = index - 1
        if prior >= 0 and references[prior] is not None and window_start <= books[prior].t < book.t:
            values = [slope(other, prior) for other in active]
            pair_ids = slopes.add_many(values)
            for other, pair_id in zip(active, pair_ids, strict=True):
                pairs_by_observation[other].append(pair_id)
            active.append(prior)
            insort(sorted_references, references[prior])
        if len(sorted_references) < 8:
            result.append(FairStatistics(None, None, None, None, len(sorted_references)))
            continue
        result.append(
            FairStatistics(
                _percentile_sorted(sorted_references, Fraction(1, 2)),
                _percentile_sorted(sorted_references, Fraction(1, 4)),
                _percentile_sorted(sorted_references, Fraction(3, 4)),
                slopes.median(),
                len(sorted_references),
            )
        )
    return result


def _fair_series_worker(item: tuple[int, Sequence[Book]]) -> tuple[int, list[FairStatistics]]:
    item_id, books = item
    return item_id, _fair_series(books)


def precompute_statistics(
    books: Mapping[int, Sequence[Book]] | Sequence[Book],
) -> dict[int, list[StatisticsPoint]]:
    """Precompute one strictly-past typed metric point for every book."""

    item_mapping = _books_mapping(books)
    cpu_count = os.cpu_count()
    if len(item_mapping) >= 64 and cpu_count and cpu_count > 1:
        context = get_context("fork") if "fork" in get_all_start_methods() else None
        with ProcessPoolExecutor(max_workers=min(8, cpu_count), mp_context=context) as executor:
            fairs_by_item = dict(executor.map(_fair_series_worker, item_mapping.items()))
    else:
        fairs_by_item = {item_id: _fair_series(item_books) for item_id, item_books in item_mapping.items()}

    result: dict[int, list[StatisticsPoint]] = {}
    for item_id, item_books_sequence in item_mapping.items():
        item_books = list(item_books_sequence)
        intervals = valid_intervals(item_books)
        fairs = fairs_by_item[item_id]
        points: list[StatisticsPoint] = []
        covered = 0.0
        purchased_units = 0
        purchased_copper = Fraction(0)
        volatility_values: list[float] = []
        volatility_eligible = 0.0
        regression: list[tuple[float, float, float]] = []
        interval_index = 0
        for index, _book in enumerate(item_books):
            target = index - 1
            while interval_index < target:
                interval = intervals[interval_index]
                interval_index += 1
                if interval is None:
                    continue
                covered += interval.dt
                purchased_units += sum(quantity for _, quantity in interval.walk.consumed)
                purchased_copper += sum(
                    (unit * quantity for unit, quantity in interval.walk.consumed), Fraction(0)
                )
                before_ref = _reference(interval.before)
                after_ref = _reference(interval.after)
                if before_ref is not None and after_ref is not None:
                    change = math.log(float(after_ref)) - math.log(float(before_ref))
                    volatility_values.append(change / math.sqrt(interval.dt / 3600))
                    volatility_eligible += interval.dt
                    before_fair = fairs[interval_index - 1].value
                    after_fair = fairs[interval_index].value
                    if before_fair is not None and after_fair is not None:
                        before_deviation = math.log(float(before_ref)) - math.log(float(before_fair))
                        after_deviation = math.log(float(after_ref)) - math.log(float(after_fair))
                        regression.append((before_deviation, after_deviation, interval.dt))
            fair = fairs[index]
            demand_rate = None if covered <= 0 else Fraction(purchased_units) * 86400 / as_fraction(covered)
            copper_rate = None if covered <= 0 else purchased_copper * 86400 / as_fraction(covered)
            if len(volatility_values) > 1:
                average = sum(volatility_values) / len(volatility_values)
                variance = sum((value - average) ** 2 for value in volatility_values) / (
                    len(volatility_values) - 1
                )
                volatility = math.sqrt(variance)
            elif volatility_values:
                volatility = 0.0
            else:
                volatility = None
            if len(regression) > 1:
                x_bar = sum(row[0] for row in regression) / len(regression)
                y_bar = sum(row[1] for row in regression) / len(regression)
                denominator = sum((row[0] - x_bar) ** 2 for row in regression)
                slope = (
                    sum((row[0] - x_bar) * (row[1] - y_bar) for row in regression) / denominator
                    if denominator
                    else None
                )
            else:
                slope = None
            if slope is not None and 0 < slope < 1:
                half_life = math.log(0.5) / math.log(slope) * median(row[2] for row in regression) / 3600
            else:
                half_life = None
            points.append(
                StatisticsPoint(
                    fair=fair,
                    demand=DemandStatistics(
                        covered_seconds=covered,
                        covered_days=as_fraction(covered) / 86400 if covered else Fraction(0),
                        purchased_units=purchased_units,
                        purchased_copper=purchased_copper,
                        purchased_units_per_day=demand_rate,
                        copper_per_day=copper_rate,
                        eligible_covered_seconds=covered,
                    ),
                    risk=RiskStatistics(
                        volatility_per_hour=volatility,
                        eligible_covered_seconds=volatility_eligible,
                        reference_intervals=len(volatility_values),
                        mean_reversion_b=slope,
                        half_life_hours=half_life,
                        mean_reversion_intervals=len(regression),
                    ),
                    books=index + 1,
                )
            )
        result[int(item_id)] = points
    return result


__all__ = [
    "DemandStatistics",
    "FairStatistics",
    "RiskStatistics",
    "StatisticsPoint",
    "precompute_statistics",
]
