"""Forecast model and hour-profile behavior tests."""

from __future__ import annotations

from fractions import Fraction
from random import Random

import pytest

from crowley.analysis.books import Book, Group
from crowley.forecast.models import DEMAND_MODELS, PRICE_MODELS, DemandContext
from crowley.forecast.profile import HourProfile, current_context, fit_profile
from crowley.forecast.series import build_series, index_at


def chain(count: int = 384, seed: int = 7) -> list[Book]:
    random = Random(seed)
    books: list[Book] = []
    elapsed = 0.0
    for index in range(count):
        if index and index % 53 == 0:
            elapsed += 1800.0
        t = elapsed
        quantity = 40 - (index % 5) * 5 + random.randrange(4)
        groups = [Group("g", Fraction(10 + index % 5), quantity, 100_000, 1)]
        books.append(Book(t, groups, item_id=1, sweep_id=index))
        elapsed += 900.0
    return books


def test_every_model_is_past_only_when_profile_is_refit_on_truncated_data() -> None:
    books = chain()
    full = build_series(1, books)
    for origin_index in (240, 288, 336):
        origin = full.t[origin_index]
        truncated = build_series(1, books[: origin_index + 1])
        full_context = current_context({1: full}, origin, 24 * 3600)
        truncated_context = current_context({1: truncated}, origin, 24 * 3600)
        full_index = index_at(full, origin)
        truncated_index = index_at(truncated, origin)
        assert full_index == truncated_index == origin_index
        full_prices = [
            model.predict(full, full_index, origin, 2 * 3600)  # type: ignore[arg-type]
            for model in PRICE_MODELS
        ]
        truncated_prices = [
            model.predict(truncated, truncated_index, origin, 2 * 3600)  # type: ignore[arg-type]
            for model in PRICE_MODELS
        ]
        full_demand = [
            model.predict(full, full_index, origin, 2 * 3600, full_context)  # type: ignore[arg-type]
            for model in DEMAND_MODELS
        ]
        truncated_demand = [
            model.predict(truncated, truncated_index, origin, 2 * 3600, truncated_context)  # type: ignore[arg-type]
            for model in DEMAND_MODELS
        ]
        assert sum(value is not None for value in full_prices) == len(PRICE_MODELS)
        assert sum(value is not None for value in full_demand) == len(DEMAND_MODELS)
        assert sum(value is not None for value in full_context.profile.multipliers) >= 20
        assert full_context.profile == truncated_context.profile
        assert full_prices == truncated_prices
        assert full_demand == truncated_demand


def test_appending_books_after_profile_cutoff_does_not_change_profile() -> None:
    books = chain()
    base = build_series(1, [book for book in books if book.t <= 72 * 3600])
    appended = build_series(1, books)
    assert fit_profile({1: base}, 72 * 3600, 24 * 3600) == fit_profile({1: appended}, 72 * 3600, 24 * 3600)


def test_three_days_recover_an_intermittent_hour_shape_by_ratio_of_sums() -> None:
    step = 900
    books = [
        Book(
            index * step,
            [Group("g", Fraction(10), 0 if index % 96 == 49 else 100, 100_000, 1)],
            item_id=1,
        )
        for index in range(3 * 24 * 4 + 1)
    ]
    profile = fit_profile({1: build_series(1, books)}, 3 * 24 * 3600, 24 * 3600)
    assert all(multiplier is not None for multiplier in profile.multipliers)
    assert profile.multipliers[12] > 10  # type: ignore[operator]
    assert profile.multipliers[0] == pytest.approx(0)  # type: ignore[arg-type]


def test_hour_profile_requires_two_days_and_rejects_lookahead() -> None:
    one_day = build_series(1, chain(24))
    profile = fit_profile({1: one_day}, 24 * 3600, 12 * 3600)
    assert profile.multipliers == (None,) * 24
    with pytest.raises(ValueError, match="before"):
        profile.multiplier(24 * 3600 - 1, 7200)


def test_profile_horizon_with_an_unavailable_hour_is_unavailable() -> None:
    profile = HourProfile(100, tuple(1.0 if hour != 1 else None for hour in range(24)))
    assert profile.multiplier(3_590, 100) is None
    assert profile.multiplier(2 * 3600, 3600) == 1.0


def test_demand_context_rejects_a_short_lookback() -> None:
    profile = HourProfile(0, (1.0,) * 24)
    with pytest.raises(ValueError, match="one hour"):
        DemandContext(3599, profile)
