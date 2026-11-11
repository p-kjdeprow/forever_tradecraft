"""Walk-forward validation and the single forecast model choice rule."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from fractions import Fraction

from crowley.analysis.books import MAX_GAP
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .models import DEMAND_MODELS, PRICE_MODELS, DemandContext, DemandModel, PriceModel
from .profile import PROFILE_WINDOW_S, ProfileSamples
from .series import Series, index_at, window_demand

type Prediction = int | float | Fraction | None
type ForecastValue = int | Fraction | None
type ErrorFunction = Callable[[int | float | Fraction, int | float | Fraction], float]
VALIDATION_WINDOW_S = 72 * 3600  # origins are scored over the last 72 hours before as-of
MIN_ORIGINS = 12  # minimum paired origins needed to trust a challenger
MIN_SKILL = 0.05  # minimum relative error improvement needed to trust a challenger
MIN_REALIZED_COVERAGE = 0.75  # fraction of a demand horizon that must be observed
UTC_HOUR_S = SECONDS_PER_HOUR  # validation origins are whole UTC hours


def history_start(as_of_t: float, lookback_s: float) -> float:
    """Return the inclusive start of the shared history span."""

    return as_of_t - lookback_s - VALIDATION_WINDOW_S


@dataclass(frozen=True, slots=True)
class Comparison:
    """One challenger compared with the baseline over paired scored origins."""

    model: str
    paired_n: int
    model_error: float | None
    baseline_error: float | None
    skill: float | None
    early_skill: float | None
    late_skill: float | None


@dataclass(frozen=True, slots=True)
class TargetValidation:
    """Validation evidence for one target of one item."""

    baseline: str
    baseline_n: int
    comparisons: tuple[Comparison, ...]


@dataclass(frozen=True, slots=True)
class ItemValidation:
    """Price and demand validation evidence for one item."""

    item_id: int
    price: TargetValidation
    demand: TargetValidation


@dataclass(frozen=True, slots=True)
class TargetChoice:
    """The validated model selected for one target at one as-of time."""

    model: str
    skill: float | None
    paired_n: int


@dataclass(frozen=True, slots=True)
class ItemForecast:
    """A selected price and demand forecast with provenance."""

    item_id: int
    price: int | None
    price_choice: TargetChoice
    units_per_hour: Fraction | None
    demand_choice: TargetChoice


def _skill(baseline_errors: list[float], model_errors: list[float]) -> float | None:
    if not model_errors:
        return None
    baseline_error = sum(baseline_errors) / len(baseline_errors)
    if baseline_error == 0:
        return None
    model_error = sum(model_errors) / len(model_errors)
    return 1 - model_error / baseline_error


def _comparison(
    model: str,
    baseline_errors: list[float],
    model_errors: list[float],
) -> Comparison:
    paired_n = len(model_errors)
    if paired_n == 0:
        return Comparison(model, 0, None, None, None, None, None)
    model_error = sum(model_errors) / paired_n
    baseline_error = sum(baseline_errors) / paired_n
    early_n = paired_n // 2
    early_skill = _skill(baseline_errors[:early_n], model_errors[:early_n])
    late_skill = _skill(baseline_errors[early_n:], model_errors[early_n:])
    return Comparison(
        model,
        paired_n,
        model_error,
        baseline_error,
        _skill(baseline_errors, model_errors),
        early_skill,
        late_skill,
    )


def _target_validation(
    models: tuple[PriceModel, ...] | tuple[DemandModel, ...],
    observations: list[tuple[Prediction, ...]],
    error: ErrorFunction,
) -> TargetValidation:
    baseline = models[0].name
    baseline_n = 0
    baseline_errors_by_model: list[list[float]] = [[] for _ in models[1:]]
    model_errors_by_model: list[list[float]] = [[] for _ in models[1:]]
    for row in observations:
        realized = row[0]
        predictions = row[1:]
        if realized is None:
            continue
        baseline_prediction = predictions[0]
        if baseline_prediction is None:
            continue
        baseline_n += 1
        for index, prediction in enumerate(predictions[1:]):
            if prediction is None:
                continue
            baseline_errors_by_model[index].append(error(baseline_prediction, realized))
            model_errors_by_model[index].append(error(prediction, realized))
    comparisons = tuple(
        _comparison(model.name, baseline_errors, model_errors)
        for model, baseline_errors, model_errors in zip(
            models[1:], baseline_errors_by_model, model_errors_by_model, strict=True
        )
    )
    return TargetValidation(baseline, baseline_n, comparisons)


def _price_error(predicted: int | float | Fraction, realized: int | float | Fraction) -> float:
    return abs(math.log(float(predicted) / float(realized)))


def _demand_error(predicted: int | float | Fraction, realized: int | float | Fraction) -> float:
    difference = float(predicted) - float(realized)
    return difference * difference


def _origin_grid(as_of_t: float, horizon_s: float) -> list[float]:
    first = math.ceil((as_of_t - VALIDATION_WINDOW_S) / UTC_HOUR_S) * UTC_HOUR_S
    last = as_of_t - horizon_s - MAX_GAP
    if first > last:
        return []
    count = math.floor((last - first) / UTC_HOUR_S)
    return [first + index * UTC_HOUR_S for index in range(count + 1)]


def _realized_price(s: Series, start: float, end: float) -> int | None:
    index = bisect_left(s.t, start)
    return None if index >= len(s.t) or s.t[index] > end else s.lowest[index]


def walk_forward(
    series_by_item: Mapping[int, Series],
    as_of_t: float,
    horizon_s: float,
    lookback_s: float,
    score_item_ids: Collection[int] | None = None,
) -> dict[int, ItemValidation]:
    """Score every requested item on a UTC-hour grid using only its past at each origin."""

    if horizon_s <= 0:
        raise ValueError("horizon_s must be positive")
    if lookback_s < UTC_HOUR_S:
        raise ValueError("lookback_s must be at least one hour")
    selected = None if score_item_ids is None else set(score_item_ids)
    grid = _origin_grid(as_of_t, horizon_s)
    observations: dict[
        int,
        tuple[list[tuple[Prediction, ...]], list[tuple[Prediction, ...]]],
    ] = {
        item_id: ([], [])
        for item_id in sorted(series_by_item)
        if series_by_item[item_id].t and (selected is None or item_id in selected)
    }
    prepared_samples = ProfileSamples.prepare(series_by_item, lookback_s)
    for origin in grid:
        context = DemandContext(lookback_s, prepared_samples.fit(origin))
        for item_id in sorted(observations):
            series = series_by_item[item_id]
            index = index_at(series, origin)
            if index is None or origin - series.t[index] > MAX_GAP:
                continue
            realized_price = _realized_price(series, origin + horizon_s, origin + horizon_s + MAX_GAP)
            if realized_price is not None:
                predictions = tuple(model.predict(series, index, origin, horizon_s) for model in PRICE_MODELS)
                observations[item_id][0].append((realized_price, *predictions))
            realized_demand = window_demand(series, origin, origin + horizon_s)
            if realized_demand.covered_seconds >= MIN_REALIZED_COVERAGE * horizon_s:
                if realized_demand.units_per_hour is None:
                    raise RuntimeError("positive realized demand coverage has no units_per_hour")
                realized_rate = float(realized_demand.units_per_hour)
                predictions = tuple(
                    model.predict(series, index, origin, horizon_s, context) for model in DEMAND_MODELS
                )
                observations[item_id][1].append((realized_rate, *predictions))

    result: dict[int, ItemValidation] = {}
    for item_id in sorted(observations):
        price_rows, demand_rows = observations[item_id]
        price = _target_validation(PRICE_MODELS, price_rows, _price_error)
        demand = _target_validation(DEMAND_MODELS, demand_rows, _demand_error)
        result[item_id] = ItemValidation(item_id, price, demand)
    return result


def qualified(tv: TargetValidation) -> tuple[Comparison, ...]:
    """Return stable challengers that beat the baseline in both chronological halves."""

    eligible = [
        comparison
        for comparison in tv.comparisons
        if comparison.paired_n >= MIN_ORIGINS
        and comparison.skill is not None
        and comparison.skill >= MIN_SKILL
        and comparison.early_skill is not None
        and comparison.early_skill > 0
        and comparison.late_skill is not None
        and comparison.late_skill > 0
    ]
    registry_order = {model.name: index for index, model in enumerate((*PRICE_MODELS, *DEMAND_MODELS))}

    def sort_key(comparison: Comparison) -> tuple[float, int]:
        if comparison.skill is None:
            raise RuntimeError("qualified comparison has no skill")
        return -comparison.skill, registry_order[comparison.model]

    return tuple(sorted(eligible, key=sort_key))


def _forecast_target[ModelT: (PriceModel, DemandModel)](
    target: TargetValidation,
    registry: tuple[ModelT, ...],
    predict: Callable[[ModelT], ForecastValue],
) -> tuple[ForecastValue, TargetChoice]:
    baseline = registry[0]
    baseline_prediction = predict(baseline)
    for comparison in qualified(target):
        model = next(model for model in registry if model.name == comparison.model)
        prediction = predict(model)
        if prediction is not None:
            return prediction, TargetChoice(model.name, comparison.skill, comparison.paired_n)
    return baseline_prediction, TargetChoice(baseline.name, None, target.baseline_n)


def forecast(
    s: Series, validation: ItemValidation, o: float, horizon_s: float, ctx: DemandContext
) -> ItemForecast:
    """Choose the first qualified model available at ``o``, independently per target."""

    index = index_at(s, o)
    baseline_price = TargetChoice(PRICE_MODELS[0].name, None, validation.price.baseline_n)
    baseline_demand = TargetChoice(DEMAND_MODELS[0].name, None, validation.demand.baseline_n)
    if index is None:
        return ItemForecast(s.item_id, None, baseline_price, None, baseline_demand)
    price, price_choice = _forecast_target(
        validation.price,
        PRICE_MODELS,
        lambda model: model.predict(s, index, o, horizon_s),
    )
    demand, demand_choice = _forecast_target(
        validation.demand,
        DEMAND_MODELS,
        lambda model: model.predict(s, index, o, horizon_s, ctx),
    )
    if not isinstance(price, int) and price is not None:
        raise RuntimeError("price registry returned a non-integer prediction")
    if demand is not None and not isinstance(demand, Fraction):
        raise RuntimeError("demand registry returned a non-fraction prediction")
    return ItemForecast(s.item_id, price, price_choice, demand, demand_choice)


__all__ = [
    "Comparison",
    "ItemForecast",
    "ItemValidation",
    "MIN_ORIGINS",
    "MIN_REALIZED_COVERAGE",
    "MIN_SKILL",
    "PROFILE_WINDOW_S",
    "TargetChoice",
    "TargetValidation",
    "VALIDATION_WINDOW_S",
    "forecast",
    "history_start",
    "qualified",
    "walk_forward",
]
