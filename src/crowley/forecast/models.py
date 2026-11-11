"""Past-only prediction models for exact price and demand forecasts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import TYPE_CHECKING, Protocol

from crowley.analysis.common import median_pairwise_slope
from crowley.analysis.metrics import FAIR_MIN_BOOKS
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .series import Series, window_demand

if TYPE_CHECKING:
    from .profile import HourProfile


HOURS_PER_DAY = 24  # number of UTC hours in a day
PRICE_WINDOW_S = HOURS_PER_DAY * SECONDS_PER_HOUR  # recent price observations used by price models
EWMA_HALF_LIFE_S = 6 * SECONDS_PER_HOUR  # EWMA half-life for the price challenger
EWMA_DECAY_BASE = 0.5  # weight retained at one EWMA half-life
TREND_MIN_BUCKETS = 6  # minimum UTC-hour buckets for a trend prediction
SHORT_DEMAND_WINDOW_S = 6 * SECONDS_PER_HOUR  # trailing demand challenger window
MIN_SHORT_COVERAGE_S = SECONDS_PER_HOUR  # minimum coverage for trailing_6h
MAX_RATE_DENOMINATOR = 1_000_000  # maximum denominator for a float-derived exact rate


class PriceModel(Protocol):
    """A model predicting the lowest listing at the end of a horizon."""

    @property
    def name(self) -> str: ...

    def predict(self, s: Series, i: int, o: float, horizon_s: float) -> int | None: ...


class DemandModel(Protocol):
    """A model predicting bottom-walk units per hour over a future horizon."""

    @property
    def name(self) -> str: ...

    def predict(
        self, s: Series, i: int, o: float, horizon_s: float, ctx: DemandContext
    ) -> Fraction | None: ...


@dataclass(frozen=True, slots=True)
class DemandContext:
    """The shared lookback and strictly-past market hour profile."""

    lookback_s: float
    profile: HourProfile

    def __post_init__(self) -> None:
        if self.lookback_s < SECONDS_PER_HOUR:
            raise ValueError("lookback_s must be at least one hour")


def _past_values(s: Series, i: int, o: float) -> list[tuple[float, int]]:
    """Return non-empty lowest observations at or before the origin."""

    return [
        (t, value)
        for t, value in zip(s.t[: i + 1], s.lowest[: i + 1], strict=True)
        if value is not None and o - PRICE_WINDOW_S <= t <= o
    ]


def _price_result(value: float) -> int:
    """Convert one model's floating-point price result to exact copper."""

    if not math.isfinite(value):
        raise ValueError("price model produced a non-finite prediction")
    result = int(round(value))
    if result < 1:
        raise ValueError(f"price model produced an invalid prediction {result}")
    return result


def _past_demand(s: Series, i: int, start: float, end: float):
    """Aggregate demand through the model's last-known book index."""

    return window_demand(s, start, min(end, s.t[i]))


@dataclass(frozen=True, slots=True)
class LastPriceModel:
    """The current lowest listing baseline."""

    name: str = "last"

    def predict(self, s: Series, i: int, o: float, horizon_s: float) -> int | None:
        return s.lowest[i]


@dataclass(frozen=True, slots=True)
class Median24hPriceModel:
    """The median recent lowest listing."""

    name: str = "median_24h"

    def predict(self, s: Series, i: int, o: float, horizon_s: float) -> int | None:
        values = [value for _, value in _past_values(s, i, o)]
        if len(values) < FAIR_MIN_BOOKS:
            return None
        return _price_result(float(median(values)))


@dataclass(frozen=True, slots=True)
class Ewma6hPriceModel:
    """A normalised exponentially weighted recent log-price."""

    name: str = "ewma_6h"

    def predict(self, s: Series, i: int, o: float, horizon_s: float) -> int | None:
        observations = _past_values(s, i, o)
        if len(observations) < FAIR_MIN_BOOKS:
            return None
        weighted = [
            (math.log(value), EWMA_DECAY_BASE ** ((o - t) / EWMA_HALF_LIFE_S)) for t, value in observations
        ]
        total_weight = sum(weight for _, weight in weighted)
        return _price_result(
            math.exp(sum(log_value * weight for log_value, weight in weighted) / total_weight)
        )


def _utc_hour_start(timestamp: float) -> int:
    return math.floor(timestamp / SECONDS_PER_HOUR) * SECONDS_PER_HOUR


@dataclass(frozen=True, slots=True)
class Trend24hPriceModel:
    """A Theil-Sen trend over medians of recent UTC-hour log-price buckets."""

    name: str = "trend_24h"

    def predict(self, s: Series, i: int, o: float, horizon_s: float) -> int | None:
        buckets: dict[int, list[float]] = {}
        for t, value in _past_values(s, i, o):
            buckets.setdefault(_utc_hour_start(t), []).append(math.log(value))
        if len(buckets) < TREND_MIN_BUCKETS:
            return None
        bucket_medians = sorted(
            (bucket + SECONDS_PER_HOUR / 2, float(median(values))) for bucket, values in buckets.items()
        )
        slope = median_pairwise_slope(bucket_medians)
        if slope is None:
            return None
        last_mid, last_median = bucket_medians[-1]
        return _price_result(math.exp(last_median + slope * (o + horizon_s - last_mid)))


@dataclass(frozen=True, slots=True)
class TrailingDemandModel:
    """The exact trailing demand baseline over the configured lookback."""

    name: str = "trailing"

    def predict(self, s: Series, i: int, o: float, horizon_s: float, ctx: DemandContext) -> Fraction | None:
        return _past_demand(s, i, o - ctx.lookback_s, o).units_per_hour


@dataclass(frozen=True, slots=True)
class Trailing6hDemandModel:
    """A short trailing demand model requiring one hour of usable coverage."""

    name: str = "trailing_6h"

    def predict(self, s: Series, i: int, o: float, horizon_s: float, ctx: DemandContext) -> Fraction | None:
        demand = _past_demand(s, i, o - SHORT_DEMAND_WINDOW_S, o)
        return demand.units_per_hour if demand.covered_seconds >= MIN_SHORT_COVERAGE_S else None


@dataclass(frozen=True, slots=True)
class HourProfileDemandModel:
    """Trailing demand scaled by the strictly-past market hour profile."""

    name: str = "hour_profile"

    def predict(self, s: Series, i: int, o: float, horizon_s: float, ctx: DemandContext) -> Fraction | None:
        trailing = _past_demand(s, i, o - ctx.lookback_s, o).units_per_hour
        multiplier = ctx.profile.multiplier(o, horizon_s)
        if trailing is None or multiplier is None:
            return None
        return Fraction(float(trailing) * multiplier).limit_denominator(MAX_RATE_DENOMINATOR)


PRICE_MODELS: tuple[PriceModel, ...] = (
    LastPriceModel(),
    Median24hPriceModel(),
    Ewma6hPriceModel(),
    Trend24hPriceModel(),
)
DEMAND_MODELS: tuple[DemandModel, ...] = (
    TrailingDemandModel(),
    Trailing6hDemandModel(),
    HourProfileDemandModel(),
)


__all__ = [
    "DEMAND_MODELS",
    "DemandContext",
    "DemandModel",
    "Ewma6hPriceModel",
    "HourProfileDemandModel",
    "LastPriceModel",
    "Median24hPriceModel",
    "PRICE_MODELS",
    "PriceModel",
    "Trailing6hDemandModel",
    "TrailingDemandModel",
    "Trend24hPriceModel",
]
