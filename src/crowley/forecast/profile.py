"""Fit the market-wide UTC hour-of-day demand multiplier without lookahead."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from crowley.analysis.books import Interval
from crowley.analysis.metrics import units_per_hour

from .models import HOURS_PER_DAY, SECONDS_PER_HOUR, DemandContext
from .series import Series, window

PROFILE_WINDOW_S = 72 * 3600  # profile samples end in the last 72 hours before fitting
PROFILE_MIN_DAYS = 2  # distinct UTC days required for an hour multiplier
SECONDS_PER_DAY = HOURS_PER_DAY * SECONDS_PER_HOUR  # seconds in a UTC day


@dataclass(frozen=True, slots=True)
class _Sample:
    end: float
    hour: int
    day: int
    observed: int
    expected: Fraction


@dataclass(frozen=True, slots=True)
class HourProfile:
    """A profile fitted before one timestamp, with one multiplier per UTC hour."""

    before_t: float
    multipliers: tuple[float | None, ...]

    def __post_init__(self) -> None:
        if len(self.multipliers) != HOURS_PER_DAY:
            raise ValueError("hour profile needs exactly 24 multipliers")

    def multiplier(self, o: float, horizon_s: float) -> float | None:
        """Return the duration-weighted multiplier across a future UTC-hour interval."""

        if o < self.before_t:
            raise ValueError("hour profile cannot be used before it was fitted")
        if horizon_s <= 0:
            raise ValueError("horizon_s must be positive")
        end = o + horizon_s
        cursor = o
        weighted = 0.0
        duration = 0.0
        while cursor < end:
            hour_start = math.floor(cursor / SECONDS_PER_HOUR) * SECONDS_PER_HOUR
            segment_end = min(end, hour_start + SECONDS_PER_HOUR)
            segment = segment_end - cursor
            value = self.multipliers[_utc_hour(cursor)]
            if value is None:
                return None
            weighted += segment * value
            duration += segment
            cursor = segment_end
        return weighted / duration


@dataclass(frozen=True, slots=True)
class ProfileSamples:
    """Prepared past-only profile samples reusable across walk-forward origins."""

    samples: tuple[_Sample, ...]

    @classmethod
    def prepare(cls, series_by_item: Mapping[int, Series], lookback_s: float) -> ProfileSamples:
        """Prepare each valid interval's observed and expected demand once."""

        if lookback_s < SECONDS_PER_HOUR:
            raise ValueError("lookback_s must be at least one hour")
        samples: list[_Sample] = []
        for series in series_by_item.values():
            samples.extend(_samples_for_series(series, lookback_s))
        samples.sort(key=lambda sample: sample.end)
        return cls(tuple(samples))

    def fit(self, before_t: float) -> HourProfile:
        """Fit a profile from samples ending in the configured past window."""

        ends = tuple(sample.end for sample in self.samples)
        first = bisect_left(ends, before_t - PROFILE_WINDOW_S)
        last = bisect_right(ends, before_t)
        return _fit_samples(self.samples[first:last], before_t)


def _utc_hour(timestamp: float) -> int:
    return datetime.fromtimestamp(timestamp, UTC).hour


def _utc_day(timestamp: float) -> int:
    return math.floor(timestamp / SECONDS_PER_DAY)


def _normaliser(
    s: Series,
    interval: Interval,
    lookback_s: float,
    units_prefix: list[int],
    covered_prefix: list[float],
) -> Fraction | None:
    """Return the item's trailing hourly rate over the exact trailing interval window."""

    bounds = window(s, interval.before.t - lookback_s, interval.before.t)
    if bounds is None or bounds[0] == bounds[1]:
        return None
    first, last = bounds
    units = units_prefix[last] - units_prefix[first]
    covered = covered_prefix[last] - covered_prefix[first]
    return units_per_hour(units, covered)


def _samples_for_series(s: Series, lookback_s: float) -> list[_Sample]:
    units_prefix = [0]
    covered_prefix = [0.0]
    for interval in s.intervals:
        units_prefix.append(
            units_prefix[-1]
            if interval is None
            else units_prefix[-1] + sum(quantity for _, quantity in interval.walk.consumed)
        )
        covered_prefix.append(covered_prefix[-1] if interval is None else covered_prefix[-1] + interval.dt)

    result: list[_Sample] = []
    for interval in s.intervals:
        if interval is None:
            continue
        normaliser = _normaliser(s, interval, lookback_s, units_prefix, covered_prefix)
        if normaliser is None or normaliser == 0:
            continue
        units = sum(quantity for _, quantity in interval.walk.consumed)
        expected = normaliser * Fraction(str(interval.dt)) / SECONDS_PER_HOUR
        result.append(
            _Sample(
                interval.after.t,
                _utc_hour(interval.before.t),
                _utc_day(interval.before.t),
                units,
                expected,
            )
        )
    return result


def _fit_samples(samples: tuple[_Sample, ...], before_t: float) -> HourProfile:
    buckets: dict[int, list[_Sample]] = defaultdict(list)
    for sample in samples:
        buckets[sample.hour].append(sample)
    multipliers: list[float | None] = []
    for hour in range(HOURS_PER_DAY):
        values = buckets.get(hour, [])
        days = {sample.day for sample in values}
        expected = sum((sample.expected for sample in values), Fraction(0))
        if len(days) < PROFILE_MIN_DAYS or expected == 0:
            multipliers.append(None)
        else:
            observed = sum(sample.observed for sample in values)
            multipliers.append(float(Fraction(observed) / expected))
    return HourProfile(before_t, tuple(multipliers))


def fit_profile(series_by_item: Mapping[int, Series], before_t: float, lookback_s: float) -> HourProfile:
    """Fit one market profile from valid intervals ending no later than ``before_t``."""

    return ProfileSamples.prepare(series_by_item, lookback_s).fit(before_t)


def current_context(series_by_item: Mapping[int, Series], as_of_t: float, lookback_s: float) -> DemandContext:
    """Build the one demand context used by all as-of forecasts."""

    return DemandContext(lookback_s, fit_profile(series_by_item, as_of_t, lookback_s))


__all__ = [
    "HourProfile",
    "PROFILE_MIN_DAYS",
    "PROFILE_WINDOW_S",
    "ProfileSamples",
    "current_context",
    "fit_profile",
]
