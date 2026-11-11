"""The price/demand estimate seam and the validated forecast provider.

Evaluators ask an ``EstimateProvider`` for what an item costs now, what it is worth and how fast
it sells, and never read books for those themselves.  ``SnapshotEstimates`` reports the current
snapshot, while ``ForecastEstimates`` composes it with the walk-forward validated commodity
forecasts.  Historical replays (flip, crafting sale probability) remain outside this seam: they
measure outcomes on past books and are the evidence a forecast is scored against.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import Protocol

from crowley.analysis.books import Book, valid_intervals
from crowley.analysis.metrics import FAIR_MIN_BOOKS, fair_value, trailing_demand
from crowley.forecast import (
    DEMAND_MODELS,
    PRICE_MODELS,
    ItemForecast,
    ItemValidation,
    build_series,
    current_context,
    forecast,
    history_start,
    walk_forward,
)
from crowley.forecast.validate import VALIDATION_WINDOW_S
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .model import AdviceConfig, OptionError
from .state import FAIR_LOOKBACK_HOURS, MarketState


@dataclass(frozen=True)
class ForecastNote:
    """The validated model choices and evidence attached to one commodity estimate."""

    price_model: str
    price_skill: float | None
    price_n: int
    demand_model: str
    demand_skill: float | None
    demand_n: int


@dataclass(frozen=True)
class ForecastSummary:
    """The forecast population, model choices and validation skill for one advice run."""

    horizon_hours: float
    window_hours: float
    items_scored: int
    items_forecast: int
    price_models: Mapping[str, int]
    demand_models: Mapping[str, int]
    median_price_skill: float | None
    median_demand_skill: float | None


@dataclass(frozen=True)
class PriceEstimate:
    item_id: int
    kind: str  # "commodity" | "item"
    lowest: int | None  # lowest current listing (copper per unit); None when nothing is listed
    listed_units: int
    fair: Fraction | None  # strictly-past 24 h fair value (commodities only)
    fair_n: int
    units_per_hour: Fraction | None  # forecast-selected demand (snapshot: trailing observed demand)
    demand_intervals: int  # usable consecutive-book intervals behind units_per_hour
    covered_hours: float
    sale_price: int | None = None  # expected sale price within the horizon (snapshot: current lowest)
    forecast: ForecastNote | None = None  # None for snapshots and item-kind estimates


class EstimateProvider(Protocol):
    def estimate(self, item_id: int) -> PriceEstimate | None:
        """The estimate for an item observed in the latest sweep, else None."""
        ...

    def summary(self) -> ForecastSummary | None:
        """The provider summary, or None when this provider is not forecasting."""
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

    def summary(self) -> ForecastSummary | None:
        return None

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
        demand = trailing_demand(intervals)
        return PriceEstimate(
            item_id=item_id,
            kind="commodity",
            lowest=None if lowest is None else int(lowest),
            listed_units=sum(group.quantity for group in latest.groups),
            fair=fair["value"],
            fair_n=fair["n"],
            units_per_hour=demand.units_per_hour,
            demand_intervals=demand.usable_intervals,
            covered_hours=demand.covered_seconds / SECONDS_PER_HOUR,
            sale_price=None if lowest is None else int(lowest),
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
            sale_price=None if lowest is None else int(lowest),
        )


class ForecastEstimates:
    """Snapshot estimates with one lazily computed, walk-forward forecast per commodity."""

    def __init__(self, state: MarketState, config: AdviceConfig) -> None:
        if FAIR_LOOKBACK_HOURS * SECONDS_PER_HOUR != VALIDATION_WINDOW_S:
            raise RuntimeError("advisor fair lookback must equal the forecast validation window")
        self._state = state
        self._config = config
        self._snapshot = SnapshotEstimates(state)
        self._ready = False
        self._validations: dict[int, ItemValidation] = {}
        self._forecasts: dict[int, ItemForecast] = {}
        self._snapshot_items: tuple[int, ...] = ()

    def _ensure(self) -> None:
        if self._ready:
            return
        lookback_s = self._state.lookback_hours * SECONDS_PER_HOUR
        start = history_start(self._state.as_of_t, lookback_s)
        series = {
            item_id: build_series(
                item_id,
                [book for book in books if start <= book.t <= self._state.as_of_t],
            )
            for item_id, books in sorted(self._state.books.items())
            if any(start <= book.t <= self._state.as_of_t for book in books)
        }
        # These are deliberately the two provider-wide computations: validation sees the whole
        # market for the hour profile, and the as-of context is shared by every item forecast.
        validations = walk_forward(
            series,
            self._state.as_of_t,
            self._config.horizon_seconds,
            lookback_s,
        )
        context = current_context(series, self._state.as_of_t, lookback_s)
        forecasts = {
            item_id: forecast(
                series[item_id],
                validations[item_id],
                self._state.as_of_t,
                self._config.horizon_seconds,
                context,
            )
            for item_id in sorted(series)
        }
        snapshot_items = tuple(
            item_id
            for item_id in sorted(series)
            if (estimate := self._snapshot.estimate(item_id)) is not None and estimate.kind == "commodity"
        )
        self._validations = validations
        self._forecasts = forecasts
        self._snapshot_items = snapshot_items
        self._ready = True

    @staticmethod
    def _note(item: ItemForecast) -> ForecastNote:
        return ForecastNote(
            price_model=item.price_choice.model,
            price_skill=item.price_choice.skill,
            price_n=item.price_choice.paired_n,
            demand_model=item.demand_choice.model,
            demand_skill=item.demand_choice.skill,
            demand_n=item.demand_choice.paired_n,
        )

    def estimate(self, item_id: int) -> PriceEstimate | None:
        snapshot = self._snapshot.estimate(item_id)
        self._ensure()
        if snapshot is None:
            return None
        item = self._forecasts.get(item_id)
        if item is None or snapshot.kind != "commodity":
            return snapshot
        return PriceEstimate(
            item_id=snapshot.item_id,
            kind=snapshot.kind,
            lowest=snapshot.lowest,
            listed_units=snapshot.listed_units,
            fair=snapshot.fair,
            fair_n=snapshot.fair_n,
            units_per_hour=item.units_per_hour,
            demand_intervals=snapshot.demand_intervals,
            covered_hours=snapshot.covered_hours,
            sale_price=item.price,
            forecast=self._note(item),
        )

    def summary(self) -> ForecastSummary:
        self._ensure()
        price_models = {model.name: 0 for model in PRICE_MODELS}
        demand_models = {model.name: 0 for model in DEMAND_MODELS}
        price_skills: list[float] = []
        demand_skills: list[float] = []
        for item_id in self._snapshot_items:
            item = self._forecasts[item_id]
            price_models[item.price_choice.model] += 1
            demand_models[item.demand_choice.model] += 1
            if item.price_choice.skill is not None:
                price_skills.append(item.price_choice.skill)
            if item.demand_choice.skill is not None:
                demand_skills.append(item.demand_choice.skill)
        items_scored = sum(
            validation.price.baseline_n > 0 or validation.demand.baseline_n > 0
            for validation in self._validations.values()
        )
        return ForecastSummary(
            horizon_hours=self._config.horizon_hours,
            window_hours=VALIDATION_WINDOW_S / SECONDS_PER_HOUR,
            items_scored=items_scored,
            items_forecast=len(self._snapshot_items),
            price_models=price_models,
            demand_models=demand_models,
            median_price_skill=None if not price_skills else float(median(price_skills)),
            median_demand_skill=None if not demand_skills else float(median(demand_skills)),
        )


def make_estimates(state: MarketState, config: AdviceConfig) -> EstimateProvider:
    """Construct the configured estimate provider for one immutable market state."""

    if config.estimates == "snapshot":
        return SnapshotEstimates(state)
    if config.estimates == "forecast":
        return ForecastEstimates(state, config)
    raise OptionError(f"unknown estimates provider {config.estimates!r}")
