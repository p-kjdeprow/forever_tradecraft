"""Typed strictly-past market statistics."""

from __future__ import annotations

import math
import os
from bisect import bisect_left, insort
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from heapq import heappop, heappush
from multiprocessing import get_all_start_methods, get_context

from crowley.analysis.books import MAX_GAP, Book, Interval, valid_intervals
from crowley.analysis.time_to_sale import _reference


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


@dataclass(frozen=True)
class StatisticsAccumulator:
    """Fixed-shape sufficient statistics for continuing one item replay."""

    last_t: float | None = None
    covered_seconds: Fraction = Fraction(0)
    purchased_units: int = 0
    purchased_copper: Fraction = Fraction(0)
    volatility_eligible_seconds: Fraction = Fraction(0)
    volatility_count: int = 0
    volatility_sum: Fraction = Fraction(0)
    volatility_sum_squares: Fraction = Fraction(0)
    regression_count: int = 0
    regression_sum_x: Fraction = Fraction(0)
    regression_sum_y: Fraction = Fraction(0)
    regression_sum_xx: Fraction = Fraction(0)
    regression_sum_xy: Fraction = Fraction(0)
    duration_counts: tuple[int, ...] = field(default_factory=lambda: (0,) * (int(MAX_GAP) + 1))
    duration_other: tuple[float, ...] = ()
    fair_window: tuple[tuple[float, Fraction | None], ...] = ()


def _exact_fraction(value: Fraction | float | int) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(value)


class _AccumulatorCursor(Sequence[StatisticsAccumulator]):
    """Single-pass cursor over accumulators without retaining history."""

    def __init__(
        self,
        item_books: Sequence[Book],
        references: Sequence[Fraction | None],
        fairs: Sequence[FairStatistics],
        intervals: Sequence[Interval | None],
        initial: StatisticsAccumulator,
    ) -> None:
        self._books = item_books
        self._references = references
        self._fairs = fairs
        self._intervals = intervals
        self._last_index = -1
        self._last_t = initial.last_t
        self._covered = _exact_fraction(initial.covered_seconds)
        self._purchased_units = initial.purchased_units
        self._purchased_copper = initial.purchased_copper
        self._volatility_eligible = _exact_fraction(initial.volatility_eligible_seconds)
        self._volatility_count = initial.volatility_count
        self._volatility_sum = _exact_fraction(initial.volatility_sum)
        self._volatility_sum_squares = _exact_fraction(initial.volatility_sum_squares)
        self._regression_count = initial.regression_count
        self._regression_sum_x = _exact_fraction(initial.regression_sum_x)
        self._regression_sum_y = _exact_fraction(initial.regression_sum_y)
        self._regression_sum_xx = _exact_fraction(initial.regression_sum_xx)
        self._regression_sum_xy = _exact_fraction(initial.regression_sum_xy)
        self._duration_counts = list(initial.duration_counts)
        if initial.duration_other:
            raise ValueError("statistics accumulator has unsupported non-integral duration samples")
        self._fair_window = list(initial.fair_window)

    def __len__(self) -> int:
        return len(self._books)

    def __getitem__(self, index: int) -> StatisticsAccumulator:
        if isinstance(index, slice):
            raise TypeError("statistics accumulator cursor does not support slices")
        if index < 0 or index >= len(self._books):
            raise IndexError("statistics accumulator index is out of range")
        if index < self._last_index:
            raise ValueError("statistics accumulator cursor cannot move backwards")
        while self._last_index < index:
            self._advance(self._last_index + 1)
        return self._current()

    def _advance(self, index: int) -> None:
        book = self._books[index]
        if self._last_t is None or book.t > self._last_t:
            self._fair_window.append((book.t, self._references[index]))
        self._fair_window = [entry for entry in self._fair_window if entry[0] >= book.t - 86400]
        if index > 0:
            interval = self._intervals[index - 1]
            if interval is not None and (self._last_t is None or interval.after.t > self._last_t):
                interval_seconds = _exact_fraction(interval.dt)
                if interval_seconds.denominator != 1:
                    raise ValueError("statistics intervals must have integral seconds")
                self._covered += interval_seconds
                self._purchased_units += sum(quantity for _, quantity in interval.walk.consumed)
                self._purchased_copper += sum(
                    (unit * quantity for unit, quantity in interval.walk.consumed), Fraction(0)
                )
                before_ref = self._references[index - 1]
                after_ref = self._references[index]
                if before_ref is not None and after_ref is not None:
                    change = math.log(float(after_ref)) - math.log(float(before_ref))
                    volatility_value = Fraction.from_float(change / math.sqrt(float(interval_seconds) / 3600))
                    self._volatility_count += 1
                    self._volatility_sum += volatility_value
                    self._volatility_sum_squares += volatility_value * volatility_value
                    self._volatility_eligible += interval_seconds
                    before_fair = self._fairs[index - 1].value
                    after_fair = self._fairs[index].value
                    if before_fair is not None and after_fair is not None:
                        before_deviation = Fraction.from_float(
                            math.log(float(before_ref)) - math.log(float(before_fair))
                        )
                        after_deviation = Fraction.from_float(
                            math.log(float(after_ref)) - math.log(float(after_fair))
                        )
                        self._regression_count += 1
                        self._regression_sum_x += before_deviation
                        self._regression_sum_y += after_deviation
                        self._regression_sum_xx += before_deviation * before_deviation
                        self._regression_sum_xy += before_deviation * after_deviation
                        duration = int(interval_seconds)
                        if not 0 <= duration < len(self._duration_counts):
                            raise ValueError("statistics interval exceeds the duration accumulator range")
                        self._duration_counts[duration] += 1
        if self._last_t is None or book.t > self._last_t:
            self._last_t = book.t
        self._last_index = index

    def _current(self) -> StatisticsAccumulator:
        return StatisticsAccumulator(
            last_t=self._last_t,
            covered_seconds=self._covered,
            purchased_units=self._purchased_units,
            purchased_copper=self._purchased_copper,
            volatility_eligible_seconds=self._volatility_eligible,
            volatility_count=self._volatility_count,
            volatility_sum=self._volatility_sum,
            volatility_sum_squares=self._volatility_sum_squares,
            regression_count=self._regression_count,
            regression_sum_x=self._regression_sum_x,
            regression_sum_y=self._regression_sum_y,
            regression_sum_xx=self._regression_sum_xx,
            regression_sum_xy=self._regression_sum_xy,
            duration_counts=tuple(self._duration_counts),
            duration_other=(),
            fair_window=tuple(self._fair_window),
        )


class StatisticsAccumulatorSeries(Sequence[StatisticsAccumulator]):
    """Factory for bounded single-pass accumulator cursors."""

    def __init__(
        self,
        item_books: Sequence[Book],
        references: Sequence[Fraction | None],
        fairs: Sequence[FairStatistics],
        intervals: Sequence[Interval | None],
        initial: StatisticsAccumulator,
    ) -> None:
        self._item_books = tuple(item_books)
        self._references = tuple(references)
        self._fairs = tuple(fairs)
        self._intervals = tuple(intervals)
        self._initial = initial

    def cursor(self) -> _AccumulatorCursor:
        return _AccumulatorCursor(
            self._item_books,
            self._references,
            self._fairs,
            self._intervals,
            self._initial,
        )

    def __len__(self) -> int:
        return len(self._item_books)

    def __getitem__(self, index: int) -> StatisticsAccumulator:
        return self.cursor()[index]


@dataclass(frozen=True)
class StatisticsPrecomputation:
    """Statistics and the interval/reference index used to derive them."""

    statistics: dict[int, list[StatisticsPoint]]
    intervals: dict[int, tuple[Interval | None, ...]]
    accumulators: dict[int, StatisticsAccumulator]
    accumulator_series: dict[int, StatisticsAccumulatorSeries]


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


class _DurationHistogram:
    """A fixed-domain duration histogram with logarithmic median queries."""

    def __init__(self, counts: Sequence[int]) -> None:
        self._counts = list(counts)
        self._tree = [0] * (len(self._counts) + 1)
        self._total = 0
        for duration, count in enumerate(self._counts):
            if count:
                self._add_tree(duration, count)
                self._total += count

    def _add_tree(self, duration: int, amount: int) -> None:
        index = duration + 1
        while index < len(self._tree):
            self._tree[index] += amount
            index += index & -index

    def add(self, duration: int) -> None:
        self._counts[duration] += 1
        self._total += 1
        self._add_tree(duration, 1)

    def _select(self, rank: int) -> int:
        if not 0 <= rank < self._total:
            raise ValueError("duration histogram rank is out of range")
        target = rank + 1
        index = 0
        bit = 1 << (len(self._tree).bit_length() - 1)
        while bit:
            candidate = index + bit
            if candidate < len(self._tree) and self._tree[candidate] < target:
                target -= self._tree[candidate]
                index = candidate
            bit >>= 1
        return index

    def median(self) -> Fraction | None:
        if not self._total:
            return None
        left = self._select((self._total - 1) // 2)
        right = self._select(self._total // 2)
        return Fraction(left + right, 2)


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


def _fair_series(
    books: Sequence[Book], references: Sequence[Fraction | None] | None = None
) -> list[FairStatistics]:
    references = [_reference(book) for book in books] if references is None else list(references)
    log_references = [None if reference is None else math.log(float(reference)) for reference in references]
    active: list[int] = []
    sorted_references: list[Fraction] = []
    slopes = _RollingMedian()
    pairs_by_observation: list[list[int]] = [[] for _ in books]
    result: list[FairStatistics] = []

    def log_reference(index: int) -> float:
        value = log_references[index]
        if value is None:
            raise ValueError("fair statistics pair has no reference price")
        return value

    def slope(left: int, right: int) -> float:
        return (log_reference(right) - log_reference(left)) * 86400 / (books[right].t - books[left].t)

    for index, book in enumerate(books):
        window_start = book.t - 86400
        while active and books[active[0]].t < window_start:
            expired = active.pop(0)
            slopes.remove_many(pairs_by_observation[expired])
            pairs_by_observation[expired].clear()
            reference = references[expired]
            if reference is None:
                raise ValueError("fair statistics active observation has no reference price")
            position = bisect_left(sorted_references, reference)
            sorted_references.pop(position)
        prior = index - 1
        prior_reference = references[prior] if prior >= 0 else None
        if prior >= 0 and prior_reference is not None and window_start <= books[prior].t < book.t:
            values = [slope(other, prior) for other in active]
            pair_ids = slopes.add_many(values)
            for other, pair_id in zip(active, pair_ids, strict=True):
                pairs_by_observation[other].append(pair_id)
            active.append(prior)
            insort(sorted_references, prior_reference)
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


def _fair_series_worker(
    item: tuple[int, Sequence[Book]],
) -> tuple[int, tuple[tuple[int, int] | None, ...], list[FairStatistics]]:
    item_id, books = item
    references = tuple(_reference(book) for book in books)
    encoded_references = tuple(
        None if reference is None else (reference.numerator, reference.denominator)
        for reference in references
    )
    return item_id, encoded_references, _fair_series(books, references)


def _statistics_from_fair_results(
    item_mapping: Mapping[int, Sequence[Book]],
    fair_results: Iterable[tuple[int, tuple[tuple[int, int] | None, ...], list[FairStatistics]]],
    initial_accumulators: Mapping[int, StatisticsAccumulator] | None = None,
) -> StatisticsPrecomputation:
    intervals_by_item: dict[int, tuple[Interval | None, ...]] = {}
    result: dict[int, list[StatisticsPoint]] = {}
    final_accumulators: dict[int, StatisticsAccumulator] = {}
    accumulator_series: dict[int, StatisticsAccumulatorSeries] = {}
    for (
        item_id,
        item_books_sequence,
    ), (result_item_id, encoded_references, fairs) in zip(item_mapping.items(), fair_results, strict=True):
        if result_item_id != item_id:
            raise ValueError(f"statistics result item mismatch: expected {item_id}, got {result_item_id}")
        item_books = list(item_books_sequence)
        references = tuple(None if value is None else Fraction(*value) for value in encoded_references)
        intervals = tuple(valid_intervals(item_books))
        intervals_by_item[item_id] = intervals
        points: list[StatisticsPoint] = []
        initial = (initial_accumulators or {}).get(item_id, StatisticsAccumulator())
        covered = _exact_fraction(initial.covered_seconds)
        purchased_units = initial.purchased_units
        purchased_copper = initial.purchased_copper
        volatility_eligible = _exact_fraction(initial.volatility_eligible_seconds)
        volatility_count = initial.volatility_count
        volatility_sum = _exact_fraction(initial.volatility_sum)
        volatility_sum_squares = _exact_fraction(initial.volatility_sum_squares)
        regression_count = initial.regression_count
        regression_sum_x = _exact_fraction(initial.regression_sum_x)
        regression_sum_y = _exact_fraction(initial.regression_sum_y)
        regression_sum_xx = _exact_fraction(initial.regression_sum_xx)
        regression_sum_xy = _exact_fraction(initial.regression_sum_xy)
        duration_counts = list(initial.duration_counts)
        if len(duration_counts) != int(MAX_GAP) + 1:
            raise ValueError(f"statistics accumulator for item {item_id} has an invalid duration shape")
        fair_window = list(initial.fair_window)
        if initial.duration_other:
            raise ValueError("statistics accumulator has unsupported non-integral duration samples")
        duration_histogram = _DurationHistogram(duration_counts)
        duration_other: list[float] = []
        last_accumulator = initial
        last_t = initial.last_t
        for index, _book in enumerate(item_books):
            fair = fairs[index]
            if last_t is None or item_books[index].t > last_t:
                fair_window.append((item_books[index].t, references[index]))
            fair_window = [entry for entry in fair_window if entry[0] >= item_books[index].t - 86400]
            demand_rate = None if covered <= 0 else Fraction(purchased_units) * 86400 / covered
            copper_rate = None if covered <= 0 else purchased_copper * 86400 / covered
            if volatility_count > 1:
                average = volatility_sum / volatility_count
                variance = (volatility_sum_squares - volatility_count * average * average) / (
                    volatility_count - 1
                )
                volatility = math.sqrt(float(max(Fraction(0), variance)))
            elif volatility_count:
                volatility = 0.0
            else:
                volatility = None
            if regression_count > 1:
                x_bar = regression_sum_x / regression_count
                y_bar = regression_sum_y / regression_count
                denominator = regression_sum_xx - regression_count * x_bar * x_bar
                slope = (
                    (regression_sum_xy - regression_count * x_bar * y_bar) / denominator
                    if denominator
                    else None
                )
            else:
                slope = None
            if slope is not None and 0 < slope < 1:
                median_duration = duration_histogram.median()
                half_life = (
                    None
                    if median_duration is None
                    else math.log(0.5) / math.log(float(slope)) * float(median_duration) / 3600
                )
            else:
                half_life = None
            slope_value = None if slope is None else float(slope)
            points.append(
                StatisticsPoint(
                    fair=fair,
                    demand=DemandStatistics(
                        covered_seconds=float(covered),
                        covered_days=covered / 86400 if covered else Fraction(0),
                        purchased_units=purchased_units,
                        purchased_copper=purchased_copper,
                        purchased_units_per_day=demand_rate,
                        copper_per_day=copper_rate,
                        eligible_covered_seconds=float(covered),
                    ),
                    risk=RiskStatistics(
                        volatility_per_hour=volatility,
                        eligible_covered_seconds=float(volatility_eligible),
                        reference_intervals=volatility_count,
                        mean_reversion_b=slope_value,
                        half_life_hours=half_life,
                        mean_reversion_intervals=regression_count,
                    ),
                    books=index + 1,
                )
            )
            if index > 0:
                interval = intervals[index - 1]
                if interval is not None and (last_t is None or interval.after.t > last_t):
                    interval_seconds = _exact_fraction(interval.dt)
                    if interval_seconds.denominator != 1:
                        raise ValueError("statistics intervals must have integral seconds")
                    covered += interval_seconds
                    purchased_units += sum(quantity for _, quantity in interval.walk.consumed)
                    purchased_copper += sum(
                        (unit * quantity for unit, quantity in interval.walk.consumed), Fraction(0)
                    )
                    before_ref = references[index - 1]
                    after_ref = references[index]
                    if before_ref is not None and after_ref is not None:
                        change = math.log(float(after_ref)) - math.log(float(before_ref))
                        volatility_value = Fraction.from_float(
                            change / math.sqrt(float(interval_seconds) / 3600)
                        )
                        volatility_count += 1
                        volatility_sum += volatility_value
                        volatility_sum_squares += volatility_value * volatility_value
                        volatility_eligible += interval_seconds
                        before_fair = fairs[index - 1].value
                        after_fair = fairs[index].value
                        if before_fair is not None and after_fair is not None:
                            before_deviation = Fraction.from_float(
                                math.log(float(before_ref)) - math.log(float(before_fair))
                            )
                            after_deviation = Fraction.from_float(
                                math.log(float(after_ref)) - math.log(float(after_fair))
                            )
                            regression_count += 1
                            regression_sum_x += before_deviation
                            regression_sum_y += after_deviation
                            regression_sum_xx += before_deviation * before_deviation
                            regression_sum_xy += before_deviation * after_deviation
                            duration = int(interval_seconds)
                            if 0 <= duration <= int(MAX_GAP):
                                duration_counts[duration] += 1
                                duration_histogram.add(duration)
                            else:
                                raise ValueError("statistics interval exceeds the duration accumulator range")
            if last_t is None or item_books[index].t > last_t:
                last_t = item_books[index].t
            accumulator = StatisticsAccumulator(
                last_t=last_t,
                covered_seconds=covered,
                purchased_units=purchased_units,
                purchased_copper=purchased_copper,
                volatility_eligible_seconds=volatility_eligible,
                volatility_count=volatility_count,
                volatility_sum=volatility_sum,
                volatility_sum_squares=volatility_sum_squares,
                regression_count=regression_count,
                regression_sum_x=regression_sum_x,
                regression_sum_y=regression_sum_y,
                regression_sum_xx=regression_sum_xx,
                regression_sum_xy=regression_sum_xy,
                duration_counts=tuple(duration_counts),
                duration_other=tuple(duration_other),
                fair_window=tuple(fair_window),
            )
            last_accumulator = accumulator
        result[int(item_id)] = points
        final_accumulators[int(item_id)] = last_accumulator
        accumulator_series[int(item_id)] = StatisticsAccumulatorSeries(
            item_books,
            references,
            fairs,
            intervals,
            initial,
        )
    return StatisticsPrecomputation(result, intervals_by_item, final_accumulators, accumulator_series)


def prepare_statistics(
    books: Mapping[int, Sequence[Book]] | Sequence[Book],
    initial_accumulators: Mapping[int, StatisticsAccumulator] | None = None,
) -> StatisticsPrecomputation:
    """Precompute statistics and share their interval index with Market."""

    item_mapping = _books_mapping(books)
    cpu_count = os.cpu_count()
    if len(item_mapping) >= 64 and cpu_count and cpu_count > 1:
        context = get_context("fork") if "fork" in get_all_start_methods() else None
        with ProcessPoolExecutor(max_workers=min(8, cpu_count), mp_context=context) as executor:
            fair_results = executor.map(_fair_series_worker, item_mapping.items(), chunksize=4)
            return _statistics_from_fair_results(item_mapping, fair_results, initial_accumulators)
    fair_results = (_fair_series_worker(item) for item in item_mapping.items())
    return _statistics_from_fair_results(item_mapping, fair_results, initial_accumulators)


def precompute_statistics(
    books: Mapping[int, Sequence[Book]] | Sequence[Book],
) -> dict[int, list[StatisticsPoint]]:
    """Precompute one strictly-past typed metric point for every book."""

    return prepare_statistics(books).statistics


__all__ = [
    "DemandStatistics",
    "FairStatistics",
    "RiskStatistics",
    "StatisticsAccumulator",
    "StatisticsAccumulatorSeries",
    "StatisticsPrecomputation",
    "StatisticsPoint",
    "prepare_statistics",
    "precompute_statistics",
]
