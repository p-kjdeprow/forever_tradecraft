"""Cold-tier promotion candidates: observation accounting, signals, flag logic and the CLI."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from crowley.analysis import candidates as cand
from crowley.analysis.books import Book, Group
from crowley.analysis.candidates import build_candidates, cold_item_ids, format_candidates, report_json
from crowley.analysis.item_books import item_intervals
from crowley.analysis.rank import build_parser, evaluate_trades, main
from crowley.core.watchlist import load_catalog
from crowley.trading.fees import deposit
from tests.test_analysis_rank import CONFIG, RULES, _account, make_inputs, recipe, static_book
from tests.test_analysis_rank_item import item_book, item_inputs, wire_row

COLD_STEP = 3600  # a cold item is seen every 6th sweep of the 600 s test cadence
COLD_ID = 1001
LAST_SWEEP = 999  # a hot sweep after the item's last (cold) observation
SELLER = recipe(1, ((COLD_ID, 1),), ((2001, 2),))


def cold_book(k: int, low: int = 95, *, empty: bool = False) -> Book:
    """Five units at ``low`` that the next observation consumes, over a persistent 30 x 100 reference."""

    groups = (
        []
        if empty
        else [Group(f"low-{k}", Fraction(low), 5, 100_000, 1), Group("ref", Fraction(100), 30, 100_000, 1)]
    )
    return Book(float(k * COLD_STEP), groups, None, item_id=COLD_ID, sweep_id=k * 6)


def cold_inputs(books, recipes, **overrides):
    values = {
        "as_of_t": float((len(books) - 1) * COLD_STEP),
        "latest_sweep_id": LAST_SWEEP,
        "books": {COLD_ID: books, 2001: static_book(2001, 10)},
    }
    values.update(overrides)
    return make_inputs(recipes, **values)


def candidates_of(inputs, cold=(COLD_ID,), min_observations=4):
    return build_candidates(inputs, CONFIG, set(cold), min_observations=min_observations)


def test_cold_item_is_excluded_by_rank_but_priced_here():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER])
    ranked = evaluate_trades(inputs, CONFIG)
    assert [item.reason for item in ranked.excluded] == ["output_no_current_book"]

    report = candidates_of(inputs)
    (row,) = report.candidates
    assert report.population == 1 and not report.excluded
    assert row.population == ("cold_output",)
    (out,) = row.outputs
    assert out.observations == out.observations_with_listings == 12
    assert out.lowest_min == out.lowest_median == out.lowest_max == 95
    assert out.supply == 35 and out.distinct_sellers is None  # commodity owners are anonymous
    # every 3600 s pair consumed the five units at 95: far beyond rank's 1260 s chain rule
    assert out.depletion_intervals == 11 and out.depletion_units_upper_bound == 55
    assert row.depletion_units == 55 and row.supply_units == 35
    assert row.score == row.margin_per_craft * 35
    assert row.flagged and row.flag_failures == ()
    # margin: the same net-revenue formula and the rank reagent cost (2 x 10 copper per craft)
    batch = CONFIG.batch_crafts
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
    old = cold_inputs([cold_book(k) for k in range(12)], [SELLER], as_of_t=float(12 * COLD_STEP + 90_000))
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


def test_fair_unavailable_is_excluded_not_priced():
    inputs = cold_inputs([cold_book(k) for k in range(5)], [SELLER])  # four priors: below 8 books
    report = candidates_of(inputs)
    assert [item.reason for item in report.excluded] == ["output_fair_unavailable"]


def test_population_is_cold_or_unranked_for_sale_evidence():
    hot = recipe(3, ((2001, 1),), ((2002, 1),))  # hot output: joins only if rank lacked sale evidence
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER, hot])
    ranked = {item.spell_id: item.reason for item in evaluate_trades(inputs, CONFIG).excluded}
    report = candidates_of(inputs)
    assert report.population == len(report.candidates) + len(report.excluded)
    joined = {c.spell_id for c in report.candidates} | {e.spell_id for e in report.excluded}
    assert (3 in joined) == (ranked.get(3) in cand.SALE_EVIDENCE_REASONS)
    assert 1 in joined
    assert 1 not in {c.spell_id for c in candidates_of(inputs, cold=()).candidates}


def test_ordering_flagged_first_then_score():
    dear = recipe(2, ((COLD_ID, 1),), ((2001, 200),))
    inputs = cold_inputs([cold_book(k) for k in range(30)], [dear, SELLER])
    rows = candidates_of(inputs).candidates
    assert [row.spell_id for row in rows] == [1, 2]


def _cold_item_books(rows_of):
    books = [item_book(k, rows_of(k), t=k * COLD_STEP) for k in range(12)]
    for index, book in enumerate(books):
        object.__setattr__(book, "sweep_id", index * 6)
    return books


def _item_candidate(books):
    inputs = item_inputs(
        [recipe(7, ((3001, 1),), ((2001, 2),))], {3001: books}, as_of_t=float(11 * COLD_STEP)
    )
    inputs = replace(inputs, books={2001: static_book(2001, 10)}, latest_sweep_id=LAST_SWEEP)
    (row,) = build_candidates(inputs, CONFIG, {3001}, min_observations=4).candidates
    return row


def test_item_kind_output_counts_long_gap_depletion_and_distinct_sellers():
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95), wire_row(1, 100)])
    # rank's 1800 s interval rule sees nothing across 3600 s gaps
    assert item_intervals(books, "3001") == [None] * 11
    row = _item_candidate(books)
    (out,) = row.outputs
    assert out.kind == "item" and out.observations == 12
    assert out.depletion_intervals == 11 and out.unusable_intervals == 0
    assert out.depletion_units_upper_bound == 11  # one 95 copper auction bought per interval
    assert out.supply == 2 and out.distinct_sellers == 2
    assert out.lowest_min == out.lowest_max == 95
    assert row.supply_units == 2 and row.depletion_units == 11
    assert row.score == row.margin_per_craft * 2


def test_grouped_item_scope_has_unusable_intervals_and_no_seller_count():
    grouped = wire_row(1, 100, quantity=3)
    grouped["owners"].update(identityKind="grouped", totalNumberOfOwners=2, names={"A": True, "B": True})
    books = _cold_item_books(lambda k: [wire_row(10_000 + k, 95), grouped])
    for book in books:  # the loader reads a grouped row's owner as None; the test helper does not
        book.auctions = tuple(replace(a, owner=None) if a.result_id == 1 else a for a in book.auctions)
    row = _item_candidate(books)
    (out,) = row.outputs
    assert out.distinct_sellers is None
    assert out.unusable_intervals == 11 and out.depletion_units_upper_bound == 0 and row.score == 0


def test_status_and_output_formats():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER], account=_account(frozenset({1})))
    report = candidates_of(inputs)
    assert report.candidates[0].status == "known"
    table = format_candidates(report, {COLD_ID: "Cold Elixir"})
    assert "Cold Elixir" in table and "UPPER BOUND" in table and "YES" in table
    payload = report_json(report, {COLD_ID: "Cold Elixir"}, {"house": "us-normal-alliance"})
    assert '"depletion_note"' in payload and '"flagged": true' in payload


def test_cold_pool_comes_from_the_catalog_for_its_own_build(monkeypatch: pytest.MonkeyPatch):
    catalog = load_catalog(enforce_target=False)
    first = next(item for item in catalog.items if item.status == "active")
    items = [item.model_copy(update={"pool": "cold"}) if item is first else item for item in catalog.items]
    patched = catalog.model_copy(update={"items": items})
    monkeypatch.setattr(cand, "load_catalog", lambda *, enforce_target: patched)
    assert cold_item_ids(catalog.client_build) == {first.item_id}
    assert cold_item_ids("1.0.0.1") == frozenset()  # another build has no cold pool here


def test_cli_flags():
    args = build_parser().parse_args(["--house", "h", "--candidates", "--min-observations", "6"])
    assert args.candidates and args.min_observations == 6
    with pytest.raises(SystemExit):
        main(["--house", "h", "--min-observations", "6"])
    with pytest.raises(SystemExit):
        main(["--house", "h", "--candidates", "--min-observations", "0"])
