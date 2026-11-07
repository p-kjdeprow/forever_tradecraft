from fractions import Fraction
from math import log, sqrt
from statistics import stdev

import pytest

from crowley.analysis.books import Book, Group, load_commodity_books
from crowley.analysis.metrics import (
    capacity_metrics,
    competition_metrics,
    compute_metrics,
    demand_metrics,
    depth_metrics,
    fair_value,
    opportunity_metrics,
    risk_metrics,
)


def group(result_id, unit, quantity, owners=1, time_left=10_000):
    return Group(result_id, Fraction(unit), quantity, time_left, owners)


def book(t, *groups, ceiling=None, item_id=1):
    return Book(t, list(groups), None if ceiling is None else Fraction(ceiling), item_id=item_id)


def test_fair_value_strict_window_minimum_and_percentiles():
    books = [book(t, group("ref", t + 1, 20)) for t in range(9)]
    books.append(book(8, group("ref", 99, 20)))
    fair = fair_value(books, 9)
    assert fair["value"] == Fraction(9, 2)
    assert fair["band"] == {"p25": Fraction(11, 4), "p75": Fraction(25, 4)}
    assert fair["n"] == 8
    assert set(fair) == {"value", "band", "trend_per_day", "n"}
    assert fair_value(books, 7)["value"] is None


def test_fair_value_trend_and_24_hour_boundary():
    books = [
        book(-86_400, group("boundary", Fraction(1, 65_536), 20)),
        *[book(-8 * 3_600 + index * 3_600, group("ref", 2**index, 20)) for index in range(8)],
        book(0, group("same-time", 4096, 20)),
        book(0, group("current", 1, 20)),
    ]
    fair = fair_value(books)
    assert fair == {
        "value": Fraction(8),
        "band": {"p25": Fraction(2), "p75": Fraction(32)},
        "trend_per_day": pytest.approx(24 * log(2)),
        "n": 9,
    }


def test_depth_bounded_books_keep_unknowns_and_fair_thresholds():
    bounded = book(0, group("a", 5, 3), group("b", 7, 2), ceiling=6)
    depth = depth_metrics(bounded, Fraction(10))
    assert depth["quantities"]["1"]["total_cost"] == Fraction(5)
    assert depth["quantities"]["20"]["total_cost"] is None
    assert depth["units_at_or_below_fair"]["0.7"] is None
    assert depth["units_at_or_below_fair"]["0.8"] is None


def test_demand_uses_only_valid_intervals_and_uses_upper_removals():
    books = [
        book(0, group("low", 2, 3), group("high", 5, 4)),
        book(60, group("low", 2, 1), group("high", 5, 2)),
        book(2_000, group("low", 2, 1), group("high", 5, 2)),
        book(2_060, group("low", 2, 0), group("high", 5, 1)),
    ]
    demand = demand_metrics(books)
    assert demand["covered_seconds"] == 120
    assert demand["purchased_units"] == 4
    assert demand["purchased_copper"] == Fraction(11)
    assert demand["non_bottom_units"] == 2
    assert demand["purchased_units_per_day"] == Fraction(2_880)


def test_competition_undercut_tenure_and_sellers():
    books = [
        book(0, group("old", 10, 5, owners=2), group("other", 12, 1, owners=3)),
        book(60, group("old", 10, 5, owners=2), group("new", 9, 2, owners=4)),
        book(120, group("cheap", 8, 1, owners=1), group("new", 9, 2, owners=4)),
        book(180, group("old", 10, 5, owners=2)),
    ]
    competition = competition_metrics(books)
    assert competition["undercut_events"] == 2
    assert competition["median_relative_depth"] == (Fraction(1, 10) + Fraction(1, 9)) / 2
    assert competition["p75_relative_depth"] == Fraction(13, 120)
    assert "events" not in competition
    assert competition["tenure"]["median_seconds"] == 60
    assert competition["tenure"]["p_undercut_within_1h"] == 0.5
    assert competition["sellers"]["median_owners_cheapest_5"] == 5


def test_tied_cheapest_groups_each_start_its_own_tenure():
    books = [
        book(0, group("a", 10, 1), group("b", 10, 1)),
        book(60, group("a", 10, 1), group("b", 10, 1), group("c", 10, 1)),
        book(120, group("a", 10, 1), group("b", 10, 1), group("c", 10, 1), group("d", 9, 1)),
    ]
    tenure = competition_metrics(books)["tenure"]
    assert tenure["starts"] == 2
    assert tenure["events"] == 1
    assert tenure["median_seconds"] == 60


def test_sellers_scope_group_count_and_owners_to_five_price_levels():
    competition = competition_metrics(
        [book(0, *(group(str(index), index, 1, owners=index) for index in range(1, 7)))]
    )
    assert competition["sellers"] == {"median_groups": 5, "median_owners_cheapest_5": 15}


def test_opportunity_arrival_survival_and_lifetime():
    books = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("new", 6, 2)),
        book(120, group("old", 10, 5), group("new", 6, 2)),
        book(180, group("old", 10, 5)),
    ]
    fairs = [{"value": Fraction(10)}] * len(books)
    opportunities = opportunity_metrics(books, fairs)
    row = opportunities["0.6"]
    assert row["arrivals"] == 1
    assert row["units"] == 2
    assert row["discount_value"] == Fraction(8)
    assert row["survival"] == 1.0
    assert row["median_lifetime_seconds"] == 120
    assert "lifetime_km" not in row


def test_risk_and_capacity_strict_price_boundary():
    risk_books = [
        book(0, group("ref", 1, 20)),
        book(60, group("ref", 2, 20)),
        book(120, group("ref", 4, 20)),
        book(180, group("ref", 8, 20)),
    ]
    fairs = [{"value": Fraction(1)}] * len(risk_books)
    risk = risk_metrics(risk_books, fairs)
    assert risk["mean_reversion_b"] == pytest.approx(1.0)
    assert risk["half_life_hours"] is None

    capacity_books = [
        book(0, group("a", 9, 1), group("b", 10, 1), group("c", 11, 1)),
        book(60),
    ]
    capacity = capacity_metrics(capacity_books, [{"value": Fraction(10)}, {"value": Fraction(10)}])
    assert capacity["0.9"]["units"] == 2
    assert capacity["1"]["units"] == 1
    assert capacity["1.1"]["units"] == 0


def test_volatility_scales_each_change_by_interval_hours():
    books = [
        book(0, group("ref", 1, 20)),
        book(360, group("ref", 2, 20)),
        book(1_260, group("ref", 4, 20)),
    ]
    values = [log(2) / sqrt(360 / 3_600), log(2) / sqrt(900 / 3_600)]
    risk = risk_metrics(books, [{"value": Fraction(1)}] * 3)
    assert risk["volatility"] == pytest.approx(stdev(values))
    assert risk["volatility_eligible_covered_seconds"] == 1_260


def test_mean_reversion_half_life_in_range():
    books = [
        book(0, group("ref", 16, 20)),
        book(60, group("ref", 8, 20)),
        book(120, group("ref", 2, 20)),
    ]
    fairs = [{"value": Fraction(1)}, {"value": Fraction(2)}, {"value": Fraction(1)}]
    risk = risk_metrics(books, fairs)
    assert risk["mean_reversion_b"] == pytest.approx(0.5)
    assert risk["half_life_hours"] == pytest.approx(1 / 60)


def test_capacity_uses_only_observable_fair_intervals_and_exposes_coverage():
    books = [
        book(0, group("old", 5, 1), ceiling=10),
        book(60, group("old", 5, 0), ceiling=10),
        book(120, group("fresh", 5, 1), group("later", 11, 1)),
        book(180, ceiling=10),
    ]
    fairs = [{"value": None}, {"value": Fraction(10)}, {"value": Fraction(10)}, {"value": Fraction(10)}]
    capacity = capacity_metrics(books, fairs)
    assert capacity["0.9"]["eligible_covered_seconds"] == 60
    assert capacity["0.9"]["units"] == 0
    assert capacity["1.1"]["eligible_covered_seconds"] == 0
    assert capacity["1.1"]["units_per_day"] is None


def test_opportunity_rates_use_fair_eligible_time_and_previous_ceiling():
    books = [
        book(0, group("old", 10, 1)),
        book(60, group("old", 10, 1), group("arrival", 6, 2)),
        book(120, group("old", 10, 1), group("warmup", 5, 1), ceiling=5),
        book(180, group("old", 10, 1), group("late", 6, 1)),
    ]
    fairs = [
        {"value": None},
        {"value": Fraction(10)},
        {"value": None},
        {"value": Fraction(10)},
    ]
    row = opportunity_metrics(books, fairs)["0.6"]
    assert row["arrivals"] == 1
    assert row["eligible_covered_seconds"] == 120
    assert row["arrivals_per_day"] == Fraction(720)
    assert row["arrivals_per_day_covered_seconds"] == 120


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def transaction(self):
        return FakeTransaction()

    def execute(self, sql, params=()):
        if "schema_migrations" in sql:
            return FakeCursor((1,))
        return FakeCursor(self.rows)


def test_loader_rejects_invalid_owner_count():
    row = (
        1,
        100,
        7,
        "commodity",
        "full",
        {"kind": "unbounded"},
        {"kind": "queried", "startedAt": 100},
        1,
        2,
        5,
        1,
        {
            "timeLeft": {"kind": "exact", "seconds": 100},
            "owners": {"totalNumberOfOwners": "-1"},
        },
    )
    with pytest.raises(ValueError, match="totalNumberOfOwners"):
        load_commodity_books(FakeConnection([row]), "house")


def test_loader_parses_positive_owner_count():
    row = (
        1,
        100,
        7,
        "commodity",
        "full",
        {"kind": "unbounded"},
        {"kind": "queried", "startedAt": 100},
        1,
        2,
        5,
        1,
        {
            "timeLeft": {"kind": "exact", "seconds": 100},
            "owners": {"totalNumberOfOwners": "3"},
        },
    )
    loaded = load_commodity_books(FakeConnection([row]), "house")
    assert loaded[7][0].groups[0].owners == 3


def test_compute_metrics_has_all_model_sections():
    result = compute_metrics({7: [book(0, group("a", 5, 20))]})[7]
    assert set(result) == {
        "fair",
        "depth",
        "demand",
        "competition",
        "opportunities",
        "risk",
        "capacity",
        "coverage",
    }
