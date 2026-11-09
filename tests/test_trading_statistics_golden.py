from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from fractions import Fraction
from statistics import median
from typing import Any

import pytest

from crowley.analysis.books import Book, Group, valid_intervals
from crowley.analysis.common import reference
from crowley.trading import statistics as statistics_module

try:
    from crowley.trading.statistics import precompute_statistics
except ImportError:  # pragma: no cover - compatibility with the pre-refactor branch
    from crowley.trading.data import precompute_statistics

EXPECTED_DIGEST = "21ce2e0e889bd64fcaff1e02bcc41fb6dfb1511a45a6709817ffccdd3afe3a5d"
PREVIOUS_GOLDEN_DIGEST = "c333c461d06887cda8ef652f54e5929e61ff658aebec64ef37ca1fa003422775"


def _synthetic_books() -> dict[int, list[Book]]:
    books: dict[int, list[Book]] = {}
    for item_index, item_id in enumerate((101, 202, 303), start=1):
        item_books: list[Book] = []
        for sweep_id in range(20):
            base_price = 30 + item_index * 11 + (sweep_id % 6) * 2 - (sweep_id % 4)
            cheap_quantity = 30 - ((sweep_id + item_index) % 5) * 2
            second_quantity = 5 + ((sweep_id * item_index) % 4)
            item_books.append(
                Book(
                    t=sweep_id * 900,
                    groups=[
                        Group(
                            f"{item_id}-cheap",
                            Fraction(base_price),
                            cheap_quantity,
                            100_000,
                        ),
                        Group(
                            f"{item_id}-second",
                            Fraction(base_price + 7),
                            second_quantity,
                            100_000,
                        ),
                    ],
                    item_id=item_id,
                    sweep_id=sweep_id + 1,
                )
            )
        books[item_id] = item_books
    return books


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value[name]
    return dataclasses.asdict(value)[name]


def _books(point: object) -> object:
    # main stores the book count under coverage["books"]; the refactor exposes it as a field.
    if isinstance(point, Mapping):
        return point["coverage"]["books"]
    return _field(point, "books")


def _fraction_json(value: object) -> object:
    if isinstance(value, Fraction):
        return value.numerator if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    return value


def _canonical_point(point: object) -> dict[str, Any]:
    fair = _field(point, "fair")
    fair_band = _field(fair, "band") if isinstance(fair, Mapping) and "band" in fair else None
    fair_p25 = _field(fair_band, "p25") if fair_band is not None else _field(fair, "p25")
    fair_p75 = _field(fair_band, "p75") if fair_band is not None else _field(fair, "p75")

    demand = _field(point, "demand")
    covered_seconds = _field(demand, "covered_seconds")
    purchased_units_per_day = _field(demand, "purchased_units_per_day")
    copper_per_day = _field(demand, "copper_per_day")
    demand_payload = {
        "covered_seconds": covered_seconds,
        "covered_days": _field(demand, "covered_days"),
        "purchased_units": _field(demand, "purchased_units"),
        "purchased_copper": _field(demand, "purchased_copper"),
        "purchased_units_per_day": purchased_units_per_day,
        "copper_per_day": copper_per_day,
        "eligible_covered_seconds": _field(demand, "eligible_covered_seconds"),
        "purchased_units_per_day_covered_seconds": covered_seconds,
        "copper_per_day_covered_seconds": covered_seconds,
        "units_per_day": purchased_units_per_day,
        "units_per_day_covered_seconds": covered_seconds,
    }

    risk = _field(point, "risk")
    volatility_per_hour = _field(risk, "volatility_per_hour")
    eligible_covered_seconds = _field(risk, "eligible_covered_seconds")
    risk_payload = {
        "volatility": volatility_per_hour,
        "volatility_per_hour": volatility_per_hour,
        "eligible_covered_seconds": eligible_covered_seconds,
        "volatility_eligible_covered_seconds": eligible_covered_seconds,
        "reference_intervals": _field(risk, "reference_intervals"),
        "mean_reversion_b": _field(risk, "mean_reversion_b"),
        "half_life_hours": _field(risk, "half_life_hours"),
        "mean_reversion_intervals": _field(risk, "mean_reversion_intervals"),
    }
    fair_payload = {
        "value": _field(fair, "value"),
        "p25": fair_p25,
        "p75": fair_p75,
        "band": {"p25": fair_p25, "p75": fair_p75},
        "trend_per_day": _field(fair, "trend_per_day"),
        "n": _field(fair, "n"),
    }
    return {
        "fair": fair_payload,
        "demand": demand_payload,
        "risk": risk_payload,
        "books": _books(point),
        "coverage": {"books": _books(point), "covered_seconds": covered_seconds},
    }


def _canonical_json(value: object) -> object:
    if isinstance(value, Fraction):
        return _fraction_json(value)
    if isinstance(value, list):
        return [_canonical_json(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_json(item) for key, item in value.items()}
    return value


def _statistics_digest(statistics: Mapping[int, list[object]]) -> str:
    payload = {
        str(item_id): [_canonical_point(point) for point in points]
        for item_id, points in sorted(statistics.items())
    }
    encoded = json.dumps(_canonical_json(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_statistics_digest() -> str:
    return _statistics_digest(precompute_statistics(_synthetic_books()))


def _previous_statistics(books: dict[int, list[Book]]) -> dict[int, list[object]]:
    """Reproduce the pre-Fraction calculation for the one-time golden tolerance check."""

    result: dict[int, list[object]] = {}
    for item_id, item_books in books.items():
        fairs = statistics_module._fair_series(item_books)
        intervals = valid_intervals(item_books)
        covered = 0.0
        purchased_units = 0
        purchased_copper = Fraction(0)
        volatility_values: list[float] = []
        volatility_eligible = 0.0
        regression: list[tuple[float, float, float]] = []
        points: list[object] = []
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
                before_ref = reference(interval.before)
                after_ref = reference(interval.after)
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
            half_life = (
                math.log(0.5) / math.log(slope) * median(row[2] for row in regression) / 3600
                if slope is not None and 0 < slope < 1
                else None
            )
            fair = fairs[index]
            demand_rate = None if covered <= 0 else Fraction(purchased_units) * 86400 / Fraction(covered)
            copper_rate = None if covered <= 0 else purchased_copper * 86400 / Fraction(covered)
            points.append(
                statistics_module.StatisticsPoint(
                    fair=fair,
                    demand=statistics_module.DemandStatistics(
                        covered_seconds=covered,
                        covered_days=Fraction(covered) / 86400 if covered else Fraction(0),
                        purchased_units=purchased_units,
                        purchased_copper=purchased_copper,
                        purchased_units_per_day=demand_rate,
                        copper_per_day=copper_rate,
                        eligible_covered_seconds=covered,
                    ),
                    risk=statistics_module.RiskStatistics(
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
        result[item_id] = points
    return result


def _assert_relative_difference(new: object, old: object, path: str = "statistics") -> None:
    if isinstance(new, dict) and isinstance(old, dict):
        assert set(new) == set(old), path
        for key in new:
            _assert_relative_difference(new[key], old[key], f"{path}.{key}")
        return
    if isinstance(new, list) and isinstance(old, list):
        assert len(new) == len(old), path
        for index, (new_value, old_value) in enumerate(zip(new, old, strict=True)):
            _assert_relative_difference(new_value, old_value, f"{path}[{index}]")
        return
    if isinstance(new, (int, float, Fraction)) and isinstance(old, (int, float, Fraction)):
        if float(old) == 0:
            assert float(new) == 0, path
        else:
            assert abs(float(new) - float(old)) / abs(float(old)) <= 1e-12, path
        return
    assert new == old, path


def test_statistics_are_golden() -> None:
    digest = canonical_statistics_digest()
    if EXPECTED_DIGEST == "PARENT_FILLS":
        pytest.fail(f"computed statistics digest: {digest}")
    assert digest == EXPECTED_DIGEST


def test_statistics_rational_repin_is_within_previous_golden_tolerance() -> None:
    previous = _previous_statistics(_synthetic_books())
    assert _statistics_digest(previous) == PREVIOUS_GOLDEN_DIGEST
    assert PREVIOUS_GOLDEN_DIGEST != EXPECTED_DIGEST
    current = precompute_statistics(_synthetic_books())
    for item_id in sorted(current):
        for new_point, old_point in zip(current[item_id], previous[item_id], strict=True):
            _assert_relative_difference(_canonical_point(new_point), _canonical_point(old_point))
