"""Walk-forward scoring, model qualification, choices and shared-slope tests."""

from __future__ import annotations

import math
from fractions import Fraction
from random import Random
from types import SimpleNamespace
from typing import cast

import pytest

from crowley.analysis.books import Book, Group
from crowley.analysis.common import median_pairwise_slope
from crowley.analysis.metrics import _fair_at
from crowley.forecast.models import PRICE_MODELS, DemandModel
from crowley.forecast.profile import current_context
from crowley.forecast.series import build_series
from crowley.forecast.validate import (
    MIN_ORIGINS,
    MIN_SKILL,
    Comparison,
    ItemValidation,
    TargetValidation,
    _demand_error,
    _price_error,
    _target_validation,
    forecast,
    qualified,
    walk_forward,
)


def constant_series(count: int = 80, item_id: int = 1) -> list[Book]:
    return [
        Book(
            index * 3600.0,
            [Group(f"g{index}", Fraction(10), 20, 100_000, 1)],
            item_id=item_id,
            sweep_id=index,
        )
        for index in range(count)
    ]


def validation(
    *, price_comparisons: tuple[Comparison, ...] = (), demand_comparisons: tuple[Comparison, ...] = ()
) -> ItemValidation:
    return ItemValidation(
        1,
        TargetValidation("last", 20, price_comparisons),
        TargetValidation("trailing", 20, demand_comparisons),
    )


def test_qualified_uses_minimum_origins_skill_and_best_first() -> None:
    tv = TargetValidation(
        "last",
        MIN_ORIGINS,
        (
            Comparison("median_24h", MIN_ORIGINS, 1.0, 1.0, MIN_SKILL, 0.1, 0.2),
            Comparison("ewma_6h", MIN_ORIGINS, 0.5, 1.0, 0.5, 0.2, 0.3),
            Comparison("trend_24h", MIN_ORIGINS - 1, 0.1, 1.0, 0.9, 0.8, 0.8),
        ),
    )
    assert [comparison.model for comparison in qualified(tv)] == ["ewma_6h", "median_24h"]


def test_choice_uses_baseline_when_unqualified_and_skips_unavailable_qualified_model() -> None:
    series = build_series(1, constant_series(80))
    ctx = current_context({1: series}, 79 * 3600, 24 * 3600)
    tv = validation(
        price_comparisons=(
            Comparison("median_24h", MIN_ORIGINS, 0.1, 1.0, 0.5, 0.2, 0.3),
            Comparison("trend_24h", MIN_ORIGINS, 0.1, 1.0, 0.4, 0.2, 0.3),
        )
    )
    chosen = forecast(series, tv, 79 * 3600, 2 * 3600, ctx)
    assert chosen.price_choice.model == "median_24h"
    assert chosen.price == 10

    sparse = build_series(1, constant_series(5))
    sparse_ctx = current_context({1: sparse}, 4 * 3600, 24 * 3600)
    sparse_choice = forecast(sparse, tv, 4 * 3600, 2 * 3600, sparse_ctx)
    assert sparse_choice.price_choice.model == "last"

    six = build_series(1, constant_series(6))
    six_ctx = current_context({1: six}, 5 * 3600, 24 * 3600)
    six_choice = forecast(six, tv, 5 * 3600, 2 * 3600, six_ctx)
    assert six_choice.price_choice.model == "trend_24h"


def test_paired_error_uses_only_origins_the_challenger_scored() -> None:
    scored = _target_validation(
        PRICE_MODELS,
        [(10, 10, None, None, None), (11, 10, 11, None, None)],
        _price_error,
    )
    comparison = scored.comparisons[0]
    assert scored.baseline_n == 2
    assert comparison.paired_n == 1
    assert comparison.baseline_error == pytest.approx(abs(math.log(10 / 11)))


def test_demand_qualification_uses_squared_error_for_intermittent_demand() -> None:
    models = cast(tuple[DemandModel, ...], (SimpleNamespace(name="trailing"), SimpleNamespace(name="zero")))
    scored = _target_validation(models, [(4, 2, 0), (0, 2, 0)], _demand_error)
    comparison = scored.comparisons[0]
    assert comparison.model_error == pytest.approx(8)
    assert comparison.baseline_error == pytest.approx(4)
    assert comparison.skill == pytest.approx(-1)


def test_qualified_rejects_a_challenger_that_is_unstable_between_halves() -> None:
    comparison = Comparison("median_24h", MIN_ORIGINS, 0.5, 1.0, 0.5, 0.2, -0.1)
    assert qualified(TargetValidation("last", MIN_ORIGINS, (comparison,))) == ()


def test_log_linear_trend_wins_and_flat_noisy_series_keeps_last() -> None:
    trend_books = [
        Book(
            index * 3600.0,
            [Group(str(index), Fraction(round(10 * math.exp(0.05 * index))), 20, 100_000, 1)],
            item_id=1,
        )
        for index in range(130)
    ]
    trend_series = build_series(1, trend_books)
    trend_validation = walk_forward({1: trend_series}, 129 * 3600, 2 * 3600, 24 * 3600)[1]
    trend_forecast = forecast(
        trend_series,
        trend_validation,
        129 * 3600,
        2 * 3600,
        current_context({1: trend_series}, 129 * 3600, 24 * 3600),
    )
    assert trend_forecast.price_choice.model == "trend_24h"

    random = Random(0)
    random_prices = [100 + random.randint(-1, 1) for _ in range(130)]
    noisy_books = [
        Book(
            index * 900.0,
            [Group(str(index), Fraction(random_prices[index]), 20, 100_000, 1)],
            item_id=1,
        )
        for index in range(130)
    ]
    noisy_series = build_series(1, noisy_books)
    noisy_validation = walk_forward({1: noisy_series}, 129 * 900, 2 * 3600, 24 * 3600)[1]
    noisy_forecast = forecast(
        noisy_series,
        noisy_validation,
        129 * 900,
        2 * 3600,
        current_context({1: noisy_series}, 129 * 900, 24 * 3600),
    )
    assert noisy_forecast.price_choice.model == "last"


def test_walk_forward_filter_keeps_scores_and_one_book_has_no_origins() -> None:
    first = build_series(1, constant_series())
    second = build_series(2, constant_series(item_id=2))
    as_of = 79 * 3600.0
    all_scores = walk_forward({1: first, 2: second}, as_of, 2 * 3600, 24 * 3600)
    selected = walk_forward({1: first, 2: second}, as_of, 2 * 3600, 24 * 3600, {1})
    assert set(selected) == {1}
    assert selected[1] == all_scores[1]
    one = build_series(3, [Book(0, [Group("a", Fraction(10), 1, 100_000, 1)], item_id=3)])
    one_score = walk_forward({3: one}, as_of, 2 * 3600, 24 * 3600)[3]
    assert one_score.price.baseline_n == 0 and one_score.demand.baseline_n == 0


def test_median_pairwise_slope_matches_the_old_fair_trend_calculation() -> None:
    points = [(0.0, math.log(2)), (1.0, math.log(4)), (2.0, math.log(8)), (2.0, math.log(16))]
    old = [
        (right_y - left_y) / (right_x - left_x)
        for index, (left_x, left_y) in enumerate(points)
        for right_x, right_y in points[index + 1 :]
        if right_x != left_x
    ]
    assert median_pairwise_slope(points) == pytest.approx(sorted(old)[len(old) // 2])
    books = [
        Book(index * 3600.0, [Group(str(index), Fraction(2**index), 20, 100_000, 1)], item_id=1)
        for index in range(9)
    ]
    expected = median_pairwise_slope(
        [(book.t / 86_400, math.log(float(2**index))) for index, book in enumerate(books[:-1])]
    )
    assert _fair_at(books, 8)["trend_per_day"] == pytest.approx(expected)
