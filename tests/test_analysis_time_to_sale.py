from fractions import Fraction

import pytest

from crowley.analysis.books import Book, Group, bottom_walk, load_commodity_books
from crowley.analysis.time_to_sale import (
    HORIZON,
    evaluate_rules,
    kaplan_meier,
    real_listings,
    replay_listing,
    rule_prices,
    validate,
)


def group(result_id, unit, quantity, time_left=10_000):
    return Group(result_id, Fraction(unit), quantity, time_left)


def book(t, *groups, ceiling=None, item_id=1):
    return Book(t, list(groups), None if ceiling is None else Fraction(ceiling), item_id=item_id)


def test_bottom_walk_consumption_expiry_cancellation_and_ceiling():
    before = book(0, group("a", 1, 10), group("b", 2, 20), group("c", 3, 30), ceiling=2)
    after = book(60, group("a", 1, 0), group("b", 2, 5), group("new", 1.5, 10), ceiling=2)
    walk = bottom_walk(before, after)
    assert walk.consumed == [(Fraction(1), 10), (Fraction(2), 15)]
    assert walk.reached_ceiling is True

    untouched = book(0, group("a", 1, 10), group("b", 2, 20))
    cancellation = book(60, group("a", 1, 10))
    assert bottom_walk(untouched, cancellation).consumed == []

    partial_stop = book(
        0,
        group("a", 1, 10),
        group("b", 2, 20),
        group("c", 3, 30),
    )
    partial_after = book(60, group("b", 2, 5), group("c", 3, 0))
    partial_walk = bottom_walk(partial_stop, partial_after)
    assert partial_walk.consumed == [(Fraction(1), 10), (Fraction(2), 15)]
    assert all(unit != Fraction(3) for unit, _ in partial_walk.consumed)
    assert partial_walk.reached_ceiling is False

    expiring = book(0, group("a", 1, 10, time_left=30), group("b", 2, 20))
    after_expiry = book(60, group("b", 2, 5))
    assert bottom_walk(expiring, after_expiry).consumed == [(Fraction(2), 15)]

    bounded = book(0, group("a", 1, 10), group("b", 3, 20), ceiling=2)
    bounded_after = book(60, group("a", 1, 10), group("b", 3, 0), ceiling=2)
    assert bottom_walk(bounded, bounded_after).consumed == []


def test_replay_event_tie_share_ceiling_gap_and_horizon():
    books = [
        book(0, group("a", 6, 5), group("b", 7, 15)),
        book(60, group("b", 7, 15)),
        book(120, group("b", 7, 0)),
        book(180, group("b", 7, 0)),
        book(240, group("b", 7, 0)),
    ]
    result = replay_listing(books, 0, 5, 20)
    assert result.event_time == 120
    assert result.filled_fraction[7200] == 1.0

    crossing = [
        book(0, group("low", 1, 10), group("demand", 6, 5)),
        *[book(t, group("low", 1, 10), group("demand", 6, 5)) for t in (1260, 2520, 3780, 5040, 6300)],
        book(7300, group("low", 1, 0), group("demand", 6, 0)),
    ]
    crossing_result = replay_listing(crossing, 0, 5, 5)
    assert crossing_result.event_time == 7300
    assert crossing_result.filled_fraction[7200] == 0.0

    tie_books = [
        book(0, group("tie", 5, 5), group("above", 6, 20)),
        book(60, group("above", 6, 20)),
        book(120, group("above", 6, 20)),
    ]
    assert replay_listing(tie_books, 0, 5, 5).event_time is None
    assert replay_listing(tie_books, 0, 5, 5, tie_share=1).event_time == 60

    bounded = [
        book(0, group("a", 1, 10), ceiling=2),
        book(60, group("a", 1, 0), ceiling=2),
    ]
    assert replay_listing(bounded, 0, 3, 1).censor_time == 60
    gap = [book(0, group("a", 1, 10)), book(1261, group("a", 1, 0))]
    assert replay_listing(gap, 0, 1, 1).censor_time == 0
    horizon = [book(t, group("a", 1, 10)) for t in range(0, HORIZON + 1, 1260)]
    horizon.append(book(HORIZON, group("a", 1, 10)))
    assert replay_listing(horizon, 0, 1, 20).event_time is None
    assert replay_listing(horizon, 0, 1, 20).censor_time == HORIZON
    excluded = [
        book(0, group("excluded", 1, 10), group("keep", 2, 10)),
        book(60, group("excluded", 1, 0), group("keep", 2, 10)),
    ]
    assert replay_listing(excluded, 0, 1, 1, exclude_result_id="excluded").event_time is None


def test_kaplan_meier_with_censoring():
    report = kaplan_meier([(10, True), (20, False), (30, True)], checkpoints=(15, 25, 40))
    assert report["starts"] == 3
    assert report["events"] == 2
    assert report["censored"] == 1
    assert report["probabilities"] == {15: pytest.approx(1 / 3), 25: pytest.approx(1 / 3), 40: 1.0}
    assert report["median_time"] == 30


def test_rules_reference_and_undefined_reference():
    defined = book(0, group("a", 10, 19), group("b", 11, 1))
    prices = rule_prices(defined)
    assert prices["undercut"] == 9
    assert prices["ref×0.8"] == Fraction(44, 5)
    assert prices["ref×1.25"] == Fraction(55, 4)
    assert "ref×1.0" not in rule_prices(book(0, group("a", 10, 19)))
    report = evaluate_rules([defined], stride=1)
    assert report[1]["undercut"][1]["starts"] == 1


def test_real_listing_extraction_event_and_cancellation():
    event_books = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("new", 9, 3)),
        book(120, group("old", 10, 5)),
    ]
    listings = real_listings({1: event_books})
    assert len(listings) == 1
    assert listings[0].result_id == "new"
    assert listings[0].event_time == 60

    cancellation_books = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("new", 11, 3)),
        book(120, group("old", 10, 5)),
    ]
    cancelled = real_listings({1: cancellation_books})[0]
    assert cancelled.event_time is None
    assert cancelled.censor_reason == "cancellation_or_expiry"


def test_real_listing_tracks_growth_before_consumption_and_cancellation():
    growth_books = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("new", 9, 3)),
        book(120, group("old", 10, 5), group("new", 9, 5)),
        book(180, group("old", 10, 5)),
    ]
    grown = real_listings({1: growth_books})[0]
    assert grown.event_time == 120
    assert grown.checkpoint_fractions[7200] == 1.0

    cancellation_after_growth = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("new", 11, 3)),
        book(120, group("old", 10, 5), group("new", 11, 5)),
        book(180, group("old", 10, 5), group("new", 11, 4)),
    ]
    cancelled = real_listings({1: cancellation_after_growth})[0]
    assert cancelled.event_time is None
    assert cancelled.censor_reason == "cancellation_or_expiry"
    assert cancelled.checkpoint_fractions[7200] is None

    partial_consumption_books = [
        book(0, group("old", 10, 5)),
        book(60, group("old", 10, 5), group("partial", 9, 3)),
        book(120, group("old", 10, 5), group("partial", 9, 5)),
        book(180, group("old", 10, 5), group("partial", 9, 2)),
    ]
    partial = real_listings({1: partial_consumption_books})[0]
    assert partial.event_time is None
    assert partial.censor_reason == "data_end"


def test_validation_truncates_training_at_split_and_calibrates():
    books = [
        book(0, group("old", 5, 5)),
        book(60, group("old", 5, 5), group("new", 4, 1)),
        book(120, group("old", 5, 5)),
    ]
    report = validate({1: books}, split_t=30)
    assert report["training_starts"] > 0
    assert report["6h"]["count"] == 1
    assert report["6h"]["brier_model"] == 1.0
    assert report["6h"]["calibration"][0]["observed_frequency"] == 1.0


def test_validation_truncates_post_split_demand_and_conditions_baseline_on_quantity():
    books = [
        book(0, group("a", 20, 1), group("zref", 20, 20), group("above", 21, 1)),
        book(60, group("zref", 20, 20), group("above", 21, 1)),
        book(120, group("zref", 20, 20), group("above", 21, 1)),
        book(180, group("zref", 20, 0), group("above", 21, 0)),
        book(240, group("zref", 20, 20), group("new", 20, 1)),
        book(300, group("zref", 20, 20), group("new", 20, 0)),
    ]
    report = validate({1: books}, split_t=120)
    for horizon in ("2h", "6h", "24h"):
        assert report[horizon]["brier_model"] == 1.0
        assert report[horizon]["brier_baseline"] == pytest.approx((11 / 14) ** 2)
        assert report[horizon]["calibration"][0]["mean_predicted"] == 0.0


def test_validation_uses_six_hour_horizon_and_excludes_missing_model_bucket():
    times = [
        0,
        1200,
        2400,
        3600,
        4800,
        6000,
        7200,
        8400,
        9600,
        10800,
        12000,
        13200,
        14400,
        15600,
        16800,
        18000,
        19200,
        20000,
    ]
    books = []
    for timestamp in times:
        groups = [] if timestamp >= 10800 else [group("low", 10, 1)]
        groups.append(group("ref", 20, 20))
        if timestamp >= 12000:
            groups.append(group("target", 19, 1 if timestamp < 20000 else 0))
        books.append(book(timestamp, *groups))
    report = validate({1: books}, split_t=11000, stride=10)
    assert report["2h"]["brier_model"] == 0.0
    assert report["2h"]["brier_baseline"] == 0.0
    assert report["6h"]["brier_model"] == 0.0
    assert report["6h"]["brier_baseline"] == pytest.approx((6 / 7) ** 2)
    assert report["6h"]["calibration"][0]["mean_predicted"] == 1.0

    missing_bucket = [
        book(0, group("z", 10, 5)),
        book(60, group("z", 10, 5)),
        book(120, group("z", 10, 5), group("a", 10, 1)),
        book(180, group("z", 10, 5), group("a", 10, 0)),
    ]
    missing_report = validate({1: missing_bucket}, split_t=60)
    assert missing_report["6h"]["count"] == 0
    assert missing_report["6h"]["unscorable_model_bucket"] == 1


def test_validation_reports_cancellation_sensitivity_and_single_classes():
    sold = [
        book(0, group("old", 10, 5), item_id=1),
        book(60, group("old", 10, 5), item_id=1),
        book(120, group("old", 10, 5), group("sold", 9, 1), item_id=1),
        book(180, group("old", 10, 5), item_id=1),
    ]
    cancelled = [
        book(0, group("old", 10, 5), group("ref", 20, 20), item_id=2),
        book(60, group("old", 10, 5), group("ref", 20, 20), item_id=2),
        book(
            120,
            group("old", 10, 5),
            group("ref", 20, 20),
            group("cancelled", 21, 1),
            item_id=2,
        ),
        book(180, group("old", 10, 5), group("ref", 20, 20), item_id=2),
    ]
    chain_break = [
        book(0, group("old", 10, 5), group("ref", 20, 20), item_id=3),
        book(60, group("old", 10, 5), group("ref", 20, 20), item_id=3),
        book(
            120,
            group("old", 10, 5),
            group("ref", 20, 20),
            group("broken", 21, 1),
            item_id=3,
        ),
        book(
            1381,
            group("old", 10, 5),
            group("ref", 20, 20),
            group("broken", 21, 1),
            item_id=3,
        ),
    ]
    report = validate({1: sold, 2: cancelled, 3: chain_break}, split_t=60)

    assert report["test_outcomes"] == {
        "event": 1,
        "cancellation_or_expiry": 1,
        "chain_break": 1,
    }
    for horizon in ("2h", "6h", "24h"):
        censored = report[horizon]["cancellation_censored"]
        unsold = report[horizon]["cancellation_unsold"]
        assert censored["count"] == 1
        assert censored["known_sold"] == 1
        assert censored["known_unsold"] == 0
        assert censored["single_class"] is True
        assert censored["brier_model"] == 1.0
        assert censored["brier_baseline"] == 1.0
        assert unsold["count"] == 2
        assert unsold["known_sold"] == 1
        assert unsold["known_unsold"] == 1
        assert unsold["single_class"] is False
        assert unsold["brier_model"] == pytest.approx(0.5)
        assert unsold["brier_baseline"] == pytest.approx(0.5)


def test_validation_rejects_nonpositive_stride():
    with pytest.raises(ValueError, match="stride"):
        validate({1: [book(0, group("a", 1, 1))]}, stride=0)


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
    def __init__(self, migration, rows=()):
        self.migration = migration
        self.rows = rows
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    def execute(self, sql, params=()):
        self.calls.append(sql)
        if "schema_migrations" in sql:
            return FakeCursor((1,) if self.migration else None)
        return FakeCursor(self.rows)


def loader_row(seconds=100):
    return (
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
            "timeLeft": {"kind": "exact", "seconds": seconds},
            "owners": {"totalNumberOfOwners": "1"},
        },
    )


def test_loader_requires_migration_and_corrected_time_left():
    with pytest.raises(RuntimeError, match="migration 003"):
        load_commodity_books(FakeConnection(False), "house")
    with pytest.raises(ValueError, match="exceeds"):
        load_commodity_books(FakeConnection(True, [loader_row(172801)]), "house")
    loaded = load_commodity_books(FakeConnection(True, [loader_row()]), "house")
    assert loaded[7][0].groups[0].unit == Fraction(5)


def test_loader_requires_query_started_at_and_matching_cutoff():
    missing_started_at = list(loader_row())
    missing_started_at[6] = {"kind": "queried"}
    with pytest.raises(ValueError, match="startedAt"):
        load_commodity_books(FakeConnection(True, [tuple(missing_started_at)]), "house")

    mismatch = list(loader_row())
    mismatch[4] = "bounded"
    with pytest.raises(ValueError, match="price cutoff"):
        load_commodity_books(FakeConnection(True, [tuple(mismatch)]), "house")
