from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

import pytest

from crowley.analysis.books import Book, Group

try:
    from crowley.trading.statistics import precompute_statistics
except ImportError:  # pragma: no cover - compatibility with the pre-refactor branch
    from crowley.trading.data import precompute_statistics

EXPECTED_DIGEST = "c333c461d06887cda8ef652f54e5929e61ff658aebec64ef37ca1fa003422775"


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


def canonical_statistics_digest() -> str:
    statistics = precompute_statistics(_synthetic_books())
    payload = {
        str(item_id): [_canonical_point(point) for point in points]
        for item_id, points in sorted(statistics.items())
    }
    encoded = json.dumps(_canonical_json(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_statistics_are_golden() -> None:
    digest = canonical_statistics_digest()
    if EXPECTED_DIGEST == "PARENT_FILLS":
        pytest.fail(f"computed statistics digest: {digest}")
    assert digest == EXPECTED_DIGEST
