"""Cold-tier promotion candidates: observation accounting, signals, flag logic and the CLI."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from crowley.analysis import candidates as cand
from crowley.analysis.books import Book, Group
from crowley.analysis.candidates import (
    CandidateReport,
    ColdPool,
    _plan_cold_items,
    build_candidates,
    format_candidates,
    report_json,
)
from crowley.analysis.item_books import item_intervals
from crowley.analysis.metrics import fair_value
from crowley.analysis.rank import build_parser, evaluate_trades, main
from crowley.trading.api import ItemFacts
from crowley.trading.fees import deposit
from tests.test_analysis_rank import CONFIG, RULES, _account, make_inputs, recipe
from tests.test_analysis_rank_item import LONG, item_book, item_inputs, wire_row

COLD_STEP = 3600  # a cold item is seen every 6th sweep of the 600 s test cadence
COLD_ID = 1001
SECOND_ID = 1002
LAST_SWEEP = 999  # a hot sweep after the item's last (cold) observation
SELLER = recipe(1, ((COLD_ID, 1),), ((2001, 2),))
POOL = ColdPool({1: frozenset({COLD_ID}), 2: frozenset({COLD_ID})})


def cold_book(
    k: int, low: int = 95, *, empty: bool = False, item_id: int = COLD_ID, step: int = COLD_STEP
) -> Book:
    """Five units at ``low`` that the next observation consumes, over a persistent 30 x 100 reference."""

    groups = (
        []
        if empty
        else [Group(f"low-{k}", Fraction(low), 5, 100_000, 1), Group("ref", Fraction(100), 30, 100_000, 1)]
    )
    return Book(float(k * step), groups, None, item_id=item_id, sweep_id=k * 6)


def reagent_books(count: int, step: int = COLD_STEP) -> list[Book]:
    return [
        Book(float(k * step), [Group("only", Fraction(10), 1000, 100_000, 1)], None, 2001, k * 6)
        for k in range(count)
    ]


def cold_inputs(books, recipes, *, as_of: float | None = None, reagents=None, **overrides):
    values = {
        "as_of_t": float((len(books) - 1) * COLD_STEP) if as_of is None else as_of,
        "latest_sweep_id": LAST_SWEEP,
        "books": {COLD_ID: books, 2001: reagent_books(len(books)) if reagents is None else reagents},
    }
    values.update(overrides)
    return make_inputs(recipes, **values)


def candidates_of(inputs, pool=POOL, min_observations=4, **kwargs):
    return build_candidates(inputs, CONFIG, pool, min_observations=min_observations, **kwargs)


def test_cold_item_is_excluded_by_rank_but_priced_here():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER])
    ranked = evaluate_trades(inputs, CONFIG)
    assert [item.reason for item in ranked.excluded] == ["output_no_current_book"]

    report = candidates_of(inputs)
    (row,) = report.candidates
    assert report.population == 1 and not report.excluded
    assert report.window_sweeps == 2 and report.cold_sweeps == {COLD_ID: 2}
    assert row.population == ("cold_output",)
    (out,) = row.outputs
    assert out.observations == out.observations_with_listings == 12
    assert out.lowest_min == out.lowest_median == out.lowest_max == 95
    assert out.supply == 35 and not out.supply_bounded
    assert out.distinct_sellers is None  # commodity owners are anonymous
    # every 3600 s pair consumed the five units at 95: far beyond rank's 1260 s chain rule
    assert out.depletion_intervals == 11 and out.observed_bottom_depletion_any_gap == 55
    assert row.depletion_units == 55 and row.supply_units == 35 and row.limiting_crafts == 35
    assert row.score == row.margin_per_craft * 35
    assert row.flagged and row.flag_failures == ()
    # margin: the same net-revenue formula and the rank reagent cost (2 x 10 copper per craft)
    batch = CONFIG.batch_crafts
    assert out.target_price is not None
    net = batch * out.target_price * (1 - RULES.seller_cut) - deposit(10, batch, 24, RULES)
    assert row.reagent_cost_per_craft == 20
    assert row.margin_per_craft == (net - 20 * batch) / batch
    median_net = batch * 95 * (1 - RULES.seller_cut) - deposit(10, batch, 24, RULES)
    assert row.margin_at_median == (median_net - 20 * batch) / batch


def test_attempted_empty_counts_as_observed_and_unattempted_does_not_exist():
    # Some cold observations are attempted-empty: a real, empty book.  Items skipped by the
    # rotation have no book at all, so they add no observation.
    books = [cold_book(k, empty=(k % 4 == 3 and k < 12)) for k in range(13)]
    (row,) = candidates_of(cold_inputs(books, [SELLER])).candidates
    (out,) = row.outputs
    assert out.observations == 13 and out.observations_with_listings == 10
    assert out.latest_lowest == 95 and out.supply == 35
    assert row.flagged  # 10/13 is at least half

    # an item whose books all predate the window was never attempted in it
    old = cold_inputs([cold_book(k) for k in range(12)], [SELLER], as_of=float(12 * COLD_STEP + 90_000))
    report = candidates_of(old)
    assert [(item.reason, item.population) for item in report.excluded] == [
        ("no_attempted_observation", ("cold_output",))
    ]
    assert report.population == 1 and not report.candidates


def test_empty_latest_observation_has_zero_supply_and_scores_zero():
    books = [cold_book(k, empty=(k == 12)) for k in range(13)]
    (row,) = candidates_of(cold_inputs(books, [SELLER])).candidates
    (out,) = row.outputs
    assert out.supply == 0 and out.latest_lowest is None and out.observations_with_listings == 12
    assert row.score == 0


def test_bounded_book_supply_is_marked():
    books = [cold_book(k) for k in range(12)]
    books[-1] = replace(books[-1], ceiling=Fraction(100))
    (row,) = candidates_of(cold_inputs(books, [SELLER])).candidates
    assert row.outputs[0].supply_bounded
    assert "35~" in format_candidates(candidates_of(cold_inputs(books, [SELLER])), {})


def test_flag_logic_each_condition_alone():
    base = [cold_book(k) for k in range(30)]  # 25 observations fall in the 24 h window
    (ok,) = candidates_of(cold_inputs(base, [SELLER])).candidates
    assert ok.flagged and ok.outputs[0].observations == 25

    (few,) = candidates_of(cold_inputs(base, [SELLER]), min_observations=26).candidates
    assert not few.flagged and few.flag_failures == ("25 observations (need 26)",)

    # 13 of the 25 window observations are empty: listings in under half
    sparse = [cold_book(k, empty=6 <= k <= 18) for k in range(30)]
    (thin,) = candidates_of(cold_inputs(sparse, [SELLER])).candidates
    assert not thin.flagged and thin.min_listing_share == Fraction(12, 25)
    assert any("of observations" in text for text in thin.flag_failures)

    # exactly half or more qualifies
    half = [cold_book(k, empty=6 <= k <= 17) for k in range(30)]
    (edge,) = candidates_of(cold_inputs(half, [SELLER])).candidates
    assert edge.min_listing_share == Fraction(13, 25) and edge.flagged

    # reagents cost more than the median price returns
    dear = recipe(2, ((COLD_ID, 1),), ((2001, 200),))
    (loss,) = candidates_of(cold_inputs(base, [dear])).candidates
    assert not loss.flagged and loss.margin_at_median is not None and loss.margin_at_median < 0
    assert loss.flag_failures == ("margin at the median lowest price is not positive",)


def test_missing_fair_value_keeps_the_candidate_without_target_margin_or_score():
    # 20 books 4 h apart: only six priors fall in 24 h (rank's window) but eighteen in 72 h.
    books = [cold_book(k, step=14_400) for k in range(20)]
    inputs = cold_inputs(books, [SELLER], as_of=19 * 14_400.0, reagents=reagent_books(20, 14_400))
    (wide,) = candidates_of(inputs).candidates
    assert wide.outputs[0].fair == 100 and wide.outputs[0].fair_books >= 8
    assert wide.outputs[0].target_price is not None and wide.score is not None

    (narrow,) = candidates_of(inputs, fair_lookback_hours=24).candidates
    out = narrow.outputs[0]
    assert out.fair is None and out.fair_books == 6 and out.target_price is None
    assert narrow.margin_per_craft is None and narrow.score is None
    assert narrow.margin_at_median is not None and narrow.margin_at_median > 0
    assert narrow.flagged  # the flag needs no fair value
    assert "-" in format_candidates(candidates_of(inputs, fair_lookback_hours=24), {})
    with pytest.raises(ValueError, match="at least rank's 24 h"):
        candidates_of(inputs, fair_lookback_hours=12)


def test_unscored_candidates_sort_after_scored_ones_within_the_flag_group():
    short = [cold_book(k, step=14_400) for k in range(20)]
    inputs = cold_inputs(short, [SELLER], as_of=19 * 14_400.0, reagents=reagent_books(20, 14_400))
    scored = candidates_of(inputs).candidates[0]
    unscored = candidates_of(inputs, fair_lookback_hours=24).candidates[0]
    assert scored.score is not None and unscored.score is None


def test_fair_over_24_hours_is_ranks_fair():
    books = [cold_book(k, low=90 + k % 7) for k in range(30)]
    inputs = cold_inputs(books, [SELLER])
    (row,) = candidates_of(inputs, fair_lookback_hours=24).candidates
    history = [b for b in books if b.t >= inputs.as_of_t - 48 * 3600]
    expected = fair_value(history, len(history) - 1, include_trend=False, min_books=CONFIG.min_fair_books)
    assert row.outputs[0].fair == expected["value"]


def test_population_is_cold_or_unranked_for_sale_evidence():
    hot = recipe(3, ((2001, 1),), ((2002, 1),))  # hot output: joins only if rank lacked sale evidence
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER, hot])
    ranked = {item.spell_id: item.reason for item in evaluate_trades(inputs, CONFIG).excluded}
    report = candidates_of(inputs)
    joined = {c.spell_id for c in report.candidates} | {e.spell_id for e in report.excluded}
    assert (3 in joined) == (ranked.get(3) in cand.SALE_EVIDENCE_REASONS)
    assert 1 in joined
    other = ColdPool({1: frozenset({999})})
    assert 1 not in {c.spell_id for c in candidates_of(inputs, other).candidates}


def test_item_is_cold_only_in_the_sweeps_whose_plan_says_so():
    pool = ColdPool({1: frozenset(), 2: frozenset({COLD_ID}), 3: frozenset({COLD_ID})})
    assert pool.items == {COLD_ID} and pool.cold_sweeps(COLD_ID) == 2 and pool.cold_sweeps(5) == 0
    with pytest.raises(ValueError):
        ColdPool({})


def test_plan_cold_items_reads_the_plan_and_fails_loudly():
    assert _plan_cold_items(1, {"pools": {"core": [1]}}) == frozenset()  # before the cold tier
    assert _plan_cold_items(1, {"coldSlices": 6, "pools": {"core": [1], "cold": [7, 8]}}) == {7, 8}
    for bad in (None, {}, {"pools": []}, {"coldSlices": 6, "pools": {"core": [1]}}):
        with pytest.raises(RuntimeError, match="sweep 1"):
            _plan_cold_items(1, bad)


def test_population_accounting_identity_is_checked():
    with pytest.raises(RuntimeError, match="entered the population"):
        CandidateReport((), (), 1, {}, 0, 1, {})


def test_ordering_flagged_first_then_score():
    dear = recipe(2, ((COLD_ID, 1),), ((2001, 200),))
    inputs = cold_inputs([cold_book(k) for k in range(30)], [dear, SELLER])
    rows = candidates_of(inputs).candidates
    assert [row.spell_id for row in rows] == [1, 2]


def test_multi_output_recipe_scores_in_crafts_not_units():
    # 3 units of the second output per craft: its 35 supply units are only 11.67 crafts.
    inputs = cold_inputs(
        [cold_book(k) for k in range(12)],
        [recipe(5, ((COLD_ID, 1), (SECOND_ID, 3)), ((2001, 2),))],
    )
    books = {**inputs.books, SECOND_ID: [cold_book(k, item_id=SECOND_ID) for k in range(12)]}
    inputs = replace(inputs, books=books)
    pool = ColdPool({1: frozenset({COLD_ID, SECOND_ID})})
    (row,) = candidates_of(inputs, pool).candidates
    assert [out.item_id for out in row.outputs] == [COLD_ID, SECOND_ID]
    assert row.limiting_item_id == SECOND_ID
    assert row.limiting_crafts == Fraction(35, 3)
    assert row.score == row.margin_per_craft * Fraction(35, 3)
    # the margin sums both outputs at their targets
    both = sum(
        CONFIG.batch_crafts * out.quantity * out.target_price * (1 - RULES.seller_cut)
        - deposit(10, out.quantity * CONFIG.batch_crafts, 24, RULES)
        for out in row.outputs
    )
    assert row.margin_per_craft == (both - 20 * CONFIG.batch_crafts) / CONFIG.batch_crafts


def test_cold_reagent_is_priced_at_its_latest_observation_with_its_age():
    old_reagents = reagent_books(10)  # last book at 9 h
    inputs = cold_inputs(
        [cold_book(k) for k in range(12)], [SELLER], reagents=old_reagents, as_of=13 * 3600.0
    )
    (row,) = candidates_of(inputs, ColdPool({1: frozenset({2001})})).candidates
    assert row.population == ("cold_reagent",)
    (reagent,) = row.reagents
    assert reagent.source == "ah" and reagent.observed_age_hours == 4.0
    assert row.reagent_cost_per_craft == 20
    table = format_candidates(candidates_of(inputs, ColdPool({1: frozenset({2001})})), {})
    assert "rg h" in table and "  4.0" in table


def test_reagent_without_a_book_or_vendor_price_is_excluded():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER], reagents=[])
    report = candidates_of(inputs)  # no attempted reagent observation, no vendor
    assert [e.reason for e in report.excluded] == ["reagent_unpriceable"]
    assert report.population == 1


def _cold_item_books(rows_of, count=12):
    books = [item_book(k, rows_of(k), t=k * COLD_STEP) for k in range(count)]
    for index, book in enumerate(books):
        book.sweep_id = index * 6
    return books


def _item_candidates(books):
    inputs = item_inputs(
        [recipe(7, ((3001, 1),), ((2001, 2),))], {3001: books}, as_of_t=float((len(books) - 1) * COLD_STEP)
    )
    inputs = replace(inputs, books={2001: reagent_books(len(books))}, latest_sweep_id=LAST_SWEEP)
    return build_candidates(inputs, CONFIG, ColdPool({1: frozenset({3001})}), min_observations=4)


def test_item_kind_output_counts_long_gap_depletion_and_distinct_sellers():
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95), wire_row(1, 100)])
    # rank's 1800 s interval rule sees nothing across 3600 s gaps
    assert item_intervals(books, "3001") == [None] * 11
    (row,) = _item_candidates(books).candidates
    (out,) = row.outputs
    assert out.kind == "item" and out.observations == 12
    assert out.depletion_intervals == 11 and out.unusable_intervals == 0
    assert out.observed_bottom_depletion_any_gap == 11  # one 95 copper auction bought per interval
    assert out.supply == 2 and out.distinct_sellers == 2
    assert out.lowest_min == out.lowest_max == 95
    assert row.supply_units == 2 and row.depletion_units == 11
    assert row.score == row.margin_per_craft * 2


def test_own_auctions_are_not_supply_or_sellers():
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95, own=True), wire_row(1, 100, quantity=3)])
    (row,) = _item_candidates(books).candidates
    assert row.outputs[0].supply == 3 and row.outputs[0].distinct_sellers == 1


def test_item_walk_over_long_gaps_skips_possible_expiries_and_reposts():
    stays = wire_row(1, 100)
    long_gap = 3 * COLD_STEP

    def consumed(before_rows, after_rows):
        books = [item_book(0, before_rows, t=0), item_book(1, after_rows, t=COLD_STEP)]
        (interval,) = item_intervals(books, "3001", max_interval_seconds=long_gap)
        assert interval is not None
        return interval.consumed

    # list-and-vanish with enough time left to be a sale; one that could have expired is skipped
    assert consumed([wire_row(11, 90, time_left=LONG), stays], [stays]) == ((Fraction(90), 1),)
    assert consumed([wire_row(12, 90, time_left=3000), stays], [stays]) == ()
    # an owner who reappears at a new price reposted: not a sale
    assert (
        consumed([wire_row(13, 90, owner="Poster"), stays], [wire_row(14, 97, owner="Poster"), stays]) == ()
    )


def test_grouped_item_scope_has_unusable_intervals_and_no_seller_count():
    grouped = wire_row(1, 100, quantity=3)
    grouped["owners"].update(identityKind="grouped", totalNumberOfOwners=2, names={"A": True, "B": True})
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95), grouped])
    for book in books:  # the loader reads a grouped row's owner as None; the test helper does not
        book.auctions = tuple(replace(a, owner=None) if a.result_id == 1 else a for a in book.auctions)
    (row,) = _item_candidates(books).candidates
    (out,) = row.outputs
    assert out.distinct_sellers is None
    assert out.unusable_intervals == 11 and out.observed_bottom_depletion_any_gap == 0 and row.score == 0


def test_item_kind_output_without_enough_floors_has_no_target():
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95), wire_row(1, 100)], count=6)
    (row,) = _item_candidates(books).candidates
    (out,) = row.outputs
    assert out.fair is None and out.fair_books == 5 and out.target_price is None
    assert row.margin_per_craft is None and row.score is None and row.margin_at_median is not None


def test_status_and_output_formats():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER], account=_account(frozenset({1})))
    report = candidates_of(inputs)
    assert report.candidates[0].status == "known"
    table = format_candidates(report, {COLD_ID: "Cold Elixir"})
    assert "Cold Elixir" in table and "over-count" in table and "YES" in table
    payload = report_json(report, {COLD_ID: "Cold Elixir"}, {"house": "us-normal-alliance"})
    assert '"depletion_note"' in payload and '"flagged": true' in payload
    assert "upper_bound" not in payload and "upper bound" not in table.lower()


def test_cli_flags():
    args = build_parser().parse_args(["--house", "h", "--candidates", "--min-observations", "6"])
    assert args.candidates and args.min_observations == 6 and args.fair_lookback_hours is None
    for bad in (
        ["--house", "h", "--min-observations", "6"],
        ["--house", "h", "--fair-lookback-hours", "72"],
        ["--house", "h", "--candidates", "--min-observations", "0"],
        ["--house", "h", "--candidates", "--fair-lookback-hours", "12"],
        ["--house", "h", "--candidates", "--all"],
    ):
        with pytest.raises(SystemExit):
            main(bad)


def test_facts_for_second_output_exist():
    assert isinstance(make_inputs([]).facts[SECOND_ID], ItemFacts)
