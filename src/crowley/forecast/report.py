"""Render walk-forward validation evidence and the chosen as-of forecasts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from math import inf
from statistics import median
from typing import Any

from crowley.analysis.common import jsonable
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .models import DEMAND_MODELS, PRICE_MODELS, DemandContext
from .series import Series
from .validate import (
    MIN_ORIGINS,
    VALIDATION_WINDOW_S,
    Comparison,
    ItemForecast,
    ItemValidation,
    forecast,
)


def _comparison_map(validation: ItemValidation, target: str) -> dict[str, Comparison]:
    selected = validation.price if target == "price" else validation.demand
    return {comparison.model: comparison for comparison in selected.comparisons}


def _model_summary(
    validations: Mapping[int, ItemValidation], forecasts: Mapping[int, ItemForecast], target: str
) -> dict[str, dict[str, float | int | None]]:
    target_models = PRICE_MODELS if target == "price" else DEMAND_MODELS
    result: dict[str, dict[str, float | int | None]] = {}
    for model in target_models:
        eligible: list[Comparison] = []
        for validation in validations.values():
            target_validation = validation.price if target == "price" else validation.demand
            if model.name == target_validation.baseline:
                if target_validation.baseline_n >= MIN_ORIGINS:
                    eligible.append(
                        Comparison(
                            model.name,
                            target_validation.baseline_n,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
            else:
                comparison = _comparison_map(validation, target).get(model.name)
                if comparison is not None and comparison.paired_n >= MIN_ORIGINS:
                    eligible.append(comparison)
        chosen = sum(
            (forecast_item.price_choice if target == "price" else forecast_item.demand_choice).model
            == model.name
            for forecast_item in forecasts.values()
        )
        skills = [comparison.skill for comparison in eligible if comparison.skill is not None]
        result[model.name] = {
            "items": len(eligible),
            "paired_origins": sum(comparison.paired_n for comparison in eligible),
            "median_skill": None if not skills else float(median(skills)),
            "chosen_items": chosen,
            "chosen_share": chosen / len(forecasts) if forecasts else 0.0,
        }
    return result


def _item_row(item_id: int, item_name: str, item: ItemForecast) -> dict[str, Any]:
    skills = [choice.skill for choice in (item.price_choice, item.demand_choice) if choice.skill is not None]
    return {
        "item_id": item_id,
        "name": item_name,
        "price": item.price,
        "price_choice": {
            "model": item.price_choice.model,
            "skill": item.price_choice.skill,
            "paired_n": item.price_choice.paired_n,
        },
        "units_per_hour": item.units_per_hour,
        "demand_choice": {
            "model": item.demand_choice.model,
            "skill": item.demand_choice.skill,
            "paired_n": item.demand_choice.paired_n,
        },
        "chosen_skill": None if not skills else max(skills),
    }


def build_report(
    *,
    house: str,
    build: str,
    sweep_id: int | str,
    as_of_t: float,
    horizon_s: float,
    lookback_s: float,
    series_by_item: Mapping[int, Series],
    validations: Mapping[int, ItemValidation],
    context: DemandContext,
    names: Mapping[int, str],
    limit: int,
) -> dict[str, Any]:
    """Build the complete JSON-ready report model."""

    if limit < 1:
        raise ValueError("limit must be positive")
    forecasts = {
        item_id: forecast(series_by_item[item_id], validation, as_of_t, horizon_s, context)
        for item_id, validation in sorted(validations.items())
    }
    rows = [_item_row(item_id, names[item_id], item) for item_id, item in forecasts.items()]
    rows.sort(
        key=lambda row: (
            -(row["chosen_skill"] if row["chosen_skill"] is not None else -inf),
            row["item_id"],
        )
    )
    as_of = datetime.fromtimestamp(as_of_t, UTC).isoformat()
    return {
        "house": house,
        "build": build,
        "sweep_id": sweep_id,
        "as_of": as_of,
        "as_of_epoch": as_of_t,
        "validation_window": {
            "start": as_of_t - VALIDATION_WINDOW_S,
            "end": as_of_t,
            "seconds": VALIDATION_WINDOW_S,
        },
        "horizon_hours": horizon_s / SECONDS_PER_HOUR,
        "lookback_hours": lookback_s / SECONDS_PER_HOUR,
        "items_loaded": len(series_by_item),
        "items_scored": len(validations),
        "models": {
            "price": _model_summary(validations, forecasts, "price"),
            "demand": _model_summary(validations, forecasts, "demand"),
        },
        "error_metrics": {
            "price": "mean absolute log error",
            "demand": "mean squared error",
        },
        "items": rows[:limit],
    }


def report_json(report: Mapping[str, Any]) -> str:
    """Render a report through the analysis package's stable JSON conversion."""

    return json.dumps(jsonable(dict(report)), indent=2, sort_keys=True)


def report_text(report: Mapping[str, Any]) -> str:
    """Render the report as a compact human-readable validation table."""

    lines = [
        f"house {report['house']} build {report['build']} sweep {report['sweep_id']}",
        f"as-of {report['as_of']} validation window {report['validation_window']['seconds'] / 3600:g} h "
        f"horizon {report['horizon_hours']:g} h",
        f"items loaded {report['items_loaded']} scored {report['items_scored']}",
        "price error: mean absolute log error; demand error: mean squared error",
        "",
        "target model                 items paired origins median skill chosen share",
    ]
    for target in ("price", "demand"):
        for model, summary in report["models"][target].items():
            skill = "-" if summary["median_skill"] is None else f"{summary['median_skill']:.3f}"
            lines.append(
                f"{target:<6} {model:<22} {summary['items']:>5} {summary['paired_origins']:>14} "
                f"{skill:>12} {summary['chosen_share']:>12.1%}"
            )
    lines.extend(("", "top items:"))
    for row in report["items"]:
        price = row["price_choice"]
        demand = row["demand_choice"]
        skill = "-" if row["chosen_skill"] is None else f"{row['chosen_skill']:.3f}"
        lines.append(
            f"{row['item_id']} {row['name']}: price={price['model']} demand={demand['model']} "
            f"skill={skill} price_n={price['paired_n']} demand_n={demand['paired_n']}"
        )
    return "\n".join(lines)


__all__ = ["build_report", "report_json", "report_text"]
