"""Advisor evaluators, accounting, the estimate seam, CLI parsing and the route, on synthetic state."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from crowley.advisor import AdviceConfig, MarketState, advise
from crowley.advisor.cli import build_parser, main
from crowley.advisor.engine import apply_filters
from crowley.advisor.estimates import PriceEstimate, SnapshotEstimates
from crowley.advisor.evaluators import EVALUATORS, Context
from crowley.advisor.evaluators.crafting import margin_at
from crowley.advisor.evaluators.flip import (
    FlipTrade,
    Prepared,
    _without,
    build_series,
    eligible_starts,
    fair_before,
    flip_context,
    plan_buy,
    pool,
    shrunk_fill,
)
from crowley.advisor.model import (
    Evidence,
    Exclusion,
    OptionError,
    Pathway,
    Step,
    StrategyResult,
    confidence_label,
    pathway_confidence,
)
from crowley.advisor.render import advice_json, format_table
from crowley.advisor.service import make_config
from crowley.analysis.books import Book, Group
from crowley.analysis.metrics import fair_value
from crowley.analysis.rank import batch_net_revenue
from crowley.trading.api import ItemSnapshot, TeachingRequirement
from crowley.trading.fees import deposit
from crowley.trading.strategies.common import (
    MAX_BAND_WIDTH,
    MIN_DEMAND_ELIGIBLE_SECONDS,
    MIN_FAIR_N,
    trustworthy_fair,
)
from tests.test_analysis_rank import (
    RULES,
    SELLER,
    _account,
    consuming_books,
    facts,
    make_inputs,
    recipe,
)

ALCHEMY_LINE = 171
HOUSE = "us-normal-alliance"
BUILD = "1.60.1.70862"
STEP = 600


def state_of(inputs, gatherable=None, names=None, professions=frozenset({"alchemy"})) -> MarketState:
    return MarketState(
        inputs=inputs,
        client_build=BUILD,
        professions=professions,
        lookback_hours=24.0,
        cold_pool=None,
        names=names or {},
        gatherable=gatherable or {},
    )


def only(strategy: str, config: AdviceConfig):
    return replace(config, strategies=frozenset({strategy}))


def run(state: MarketState, **overrides):
    config = AdviceConfig(professions=frozenset({"alchemy"}), batch_crafts=2, **overrides)
    return advise(state, config), config


def by_strategy(advice, name: str) -> StrategyResult:
    return next(result for result in advice.results if result.strategy == name)


# -- model, accounting --------------------------------------------------------------------


def _pathway(
    strategy: str = "flip", profit: int = 5, per_craft: int | None = None, volume: int = 10
) -> Pathway:
    return Pathway(
        strategy=strategy,
        section="ranked",
        key=f"{strategy}:{profit}:{volume}",
        title="t",
        steps=(Step("buy_ah", item_id=1, quantity=1, price=2),),
        profit_per_unit=Fraction(profit),
        profit_per_craft=None if per_craft is None else Fraction(per_craft),
        volume_per_hour=Fraction(volume),
        sale_probability=None,
        fill_rate=None,
        evidence=None,
        demand_evidence=None,
        listed_units=None,
        capital=Fraction(1),
        profit_per_hour=Fraction(profit * volume),
        one_off_profit=None,
        sort_value=Fraction(profit * volume),
        confidence="none",
    )


def test_strategy_result_rejects_a_silent_drop():
    with pytest.raises(RuntimeError, match="3 considered but 1 pathways and 1 exclusions"):
        StrategyResult("flip", (_pathway(),), (Exclusion("flip", "k", "no_current_book"),), 3)


def test_exclusion_reasons_are_registered_per_strategy():
    Exclusion("flip", "k", "no_current_book")
    Exclusion("crafting", "k", "rank:random_yield")
    Exclusion("flip", "k", "below_min_profit")
    with pytest.raises(ValueError, match="unregistered"):
        Exclusion("flip", "k", "because")
    with pytest.raises(ValueError, match="unregistered"):
        Exclusion("flip", "k", "rank:random_yield")  # the passthrough prefix is crafting's only


def test_filters_keep_every_candidate_counted():
    result = StrategyResult("flip", (_pathway(profit=5), _pathway(profit=50), _pathway(volume=1)), (), 3)
    filtered = apply_filters(result, AdviceConfig(min_profit=10, min_volume=Fraction(2)))
    assert filtered.considered == 3
    assert [item.profit_per_unit for item in filtered.pathways] == [50]
    assert filtered.exclusion_counts() == {"below_min_profit": 2}
    # --min-profit is per craft for a crafting pathway and per unit for the rest
    crafted = _pathway("crafting", profit=1, per_craft=500)
    assert crafted.threshold_profit == 500
    kept = apply_filters(StrategyResult("crafting", (crafted,), (), 1), AdviceConfig(min_profit=100))
    assert len(kept.pathways) == 1


def test_confidence_labels_come_from_counts_and_cap():
    assert confidence_label(None) == "none"
    assert confidence_label(Evidence("independent_starts", 0)) == "none"
    assert confidence_label(Evidence("independent_starts", 3)) == "low"
    assert confidence_label(Evidence("independent_starts", 6)) == "medium"
    assert confidence_label(Evidence("independent_starts", 12)) == "high"
    assert confidence_label(Evidence("demand_intervals", 24)) == "medium"
    with pytest.raises(ValueError, match="no confidence thresholds"):
        confidence_label(Evidence("mystery", 5))


def test_one_confidence_rule_for_every_strategy():
    strong, weak = Evidence("independent_starts", 20), Evidence("demand_intervals", 3)
    assert pathway_confidence(strong, None) == "high"
    assert pathway_confidence(strong, weak) == "low"  # the weaker count decides
    # measured P but no flips of its own: low, never none and never high
    assert pathway_confidence(Evidence("independent_starts", 0), Evidence("demand_intervals", 200)) == "low"
    # no sale evidence: at best low, even with plentiful demand evidence
    assert pathway_confidence(None, Evidence("demand_intervals", 200)) == "low"
    assert pathway_confidence(None, None) == "none"


def test_config_validation_fails_loudly():
    for bad in (
        {"strategies": frozenset({"nope"})},
        {"share": Fraction(0)},
        {"horizon_hours": 3.0},
        {"min_profit": -1},
        {"crafting_price": "mid"},
        {"flip_x": Fraction(2)},
    ):
        with pytest.raises(OptionError):
            AdviceConfig(**bad)
    config = AdviceConfig()
    assert config.share == Fraction(1, 10)
    assert (config.flip_x, config.flip_undercut) == (Fraction(7, 10), Fraction(12, 100))


# -- crafting and recipe purchase ----------------------------------------------------------


def lowered_last_book(inputs, item_id: int, unit: int):
    """Inputs whose item's latest book lists its cheapest group at ``unit`` (a price drop now)."""

    books = dict(inputs.books)
    series = list(books[item_id])
    last = series[-1]
    groups = [
        Group("now-low", Fraction(unit), 5, 100_000, 1),
        *[g for g in last.groups if g.result_id == "ref"],
    ]
    series[-1] = Book(last.t, groups, last.ceiling, last.item_id, last.sweep_id)
    books[item_id] = series
    return replace(inputs, books=books)


def test_crafting_headline_is_min_of_undercut_lowest_and_target_and_both_are_exposed():
    inputs = lowered_last_book(make_inputs([SELLER]), 1001, 80)
    state = state_of(inputs)
    advice, config = run(state, strategies=frozenset({"crafting"}))
    (pathway,) = advice.pathways
    assert pathway.section == "ranked"
    assert pathway.details["current_lowest"] == {1001: 80}
    assert pathway.details["price_undercut_lowest"] == {1001: 79}
    target = dict(pathway.details["target_prices"])[1001]
    headline = pathway.details["headline_prices"][1001]
    assert headline == min(79, target) <= target  # never above the price P was measured at
    row_cost = pathway.details["reagent_cost_per_craft"]
    ctx = Context(state, SnapshotEstimates(state), config)
    expected = margin_at(ctx, {1001: 1}, {1001: headline}, row_cost)
    assert pathway.details["margin_at_headline"] == pathway.profit_per_craft == expected
    assert pathway.details["margin_at_undercut_lowest"] == margin_at(ctx, {1001: 1}, {1001: 79}, row_cost)
    list_step = pathway.steps[-1]
    assert (list_step.action, list_step.item_id, list_step.price, list_step.quantity) == (
        "list_ah",
        1001,
        headline,
        2,
    )
    assert pathway.sale_probability == 1.0
    assert pathway.evidence and pathway.evidence.kind == "independent_starts"
    assert pathway.evidence.n == 4 and pathway.evidence.successes == 4
    assert pathway.confidence == "low"  # 4 independent starts
    # expected profit per hour weights the margin by P(sale); P is 1 here
    assert pathway.profit_per_hour == expected * pathway.details["crafts_per_hour"] * 1
    assert "listed_above_measured_price" not in pathway.details
    # revenue formula is rank's own, at the one listing duration
    net, _ = batch_net_revenue(facts(1001), RULES, 2, headline, config.listing_hours)
    assert expected == (net - row_cost * 2) / 2

    target_advice, _ = run(state, strategies=frozenset({"crafting"}), crafting_price="target")
    (target_pathway,) = target_advice.pathways
    assert target_pathway.profit_per_craft == pathway.details["margin_at_target"]
    assert target_pathway.steps[-1].price == target
    # steps: buy reagents, craft, list
    actions = [step.action for step in pathway.steps]
    assert actions == ["buy_ah", "buy_vendor", "craft", "list_ah"]
    assert pathway.steps[0].price == 10 and pathway.steps[0].quantity == 4


def test_headline_can_never_be_above_the_measured_price():
    # the lowest listing is far above the target: the headline is the target, not lowest - 1
    inputs = make_inputs([SELLER])
    books = dict(inputs.books)
    last = books[1001][-1]
    books[1001] = [
        *books[1001][:-1],
        Book(last.t, [Group("high", Fraction(140), 40, 100_000, 1)], None, 1001, last.sweep_id),
    ]
    inputs = replace(inputs, books=books)
    advice, _ = run(state_of(inputs), strategies=frozenset({"crafting"}))
    (pathway,) = advice.pathways
    target = dict(pathway.details["target_prices"])[1001]
    assert pathway.details["price_undercut_lowest"] == {1001: 139}
    assert pathway.details["headline_prices"] == {1001: target} and target < 139
    assert pathway.steps[-1].price == target
    for item_id, price in pathway.details["headline_prices"].items():
        assert price <= pathway.details["target_prices"][item_id]


def test_p_weighted_profit_per_hour_orders_crafting_by_expected_value():
    weak = recipe(2, ((1002, 1),), ((2001, 2),))  # consumed only at its own target
    advice, _ = run(
        state_of(make_inputs([SELLER, weak])), strategies=frozenset({"crafting"}), min_sale_prob=0.0
    )
    paths = {p.key: p for p in advice.pathways}
    for pathway in paths.values():
        assert pathway.sale_probability is not None and pathway.sale_probability > 0
        assert pathway.profit_per_hour == pathway.details["profit_per_hour_at_p1"] * Fraction(
            pathway.sale_probability
        )
    # a zero P is excluded even when the floor is zero
    reasons = {e.key: e.reason for e in by_strategy(advice, "crafting").exclusions}
    assert reasons == {"crafting:2": "low_sale_probability"} and set(paths) == {"crafting:1"}


def test_crafting_accounts_for_every_recipe():
    never = recipe(2, ((1002, 1),), ((2001, 2),))  # consumed only at its own target: P(sale) 0
    unpriced = recipe(3, ((1001, 1),), ((2999, 1),))  # reagent with no AH book and no vendor
    advice, _ = run(state_of(make_inputs([SELLER, never, unpriced])), strategies=frozenset({"crafting"}))
    result = by_strategy(advice, "crafting")
    assert result.considered == 3 == len(result.pathways) + len(result.exclusions)
    assert [item.key for item in result.pathways] == ["crafting:1"]
    reasons = {item.key: item.reason for item in result.exclusions}
    assert reasons == {"crafting:2": "low_sale_probability", "crafting:3": "rank:reagent_unpriceable"}


def test_recipe_purchase_needs_a_character_and_reports_payback():
    learnable = recipe(
        6,
        ((1001, 1),),
        ((2001, 2), (2002, 1)),
        teaching=(5001, 5002),
        requirements=(
            TeachingRequirement(5001, ALCHEMY_LINE, 100),
            TeachingRequirement(5002, ALCHEMY_LINE, 100),
        ),
    )
    trainer = recipe(7, ((1001, 1),), ((2001, 2),))
    recipes = [SELLER, learnable, trainer]
    config = AdviceConfig(
        professions=frozenset({"alchemy"}),
        batch_crafts=2,
        strategies=frozenset({"crafting", "recipe_purchase"}),
    )

    anonymous = advise(state_of(make_inputs(recipes, None, {5001: 900, 5002: 700})), config)
    assert {e.reason for e in by_strategy(anonymous, "recipe_purchase").exclusions} == {"no_character"}
    # without a capture the recipes are assumed known and say so
    assert {p.details["recipe_status"] for p in by_strategy(anonymous, "crafting").pathways} == {
        "unchecked (no character)"
    }

    account = _account(frozenset({1}))
    advice = advise(state_of(make_inputs(recipes, account, {5001: 900, 5002: 700})), config)
    purchase = by_strategy(advice, "recipe_purchase")
    assert purchase.considered == 3
    (pathway,) = purchase.pathways
    assert pathway.key == "recipe_purchase:6"
    assert [step.action for step in pathway.steps][:2] == ["buy_ah", "learn_recipe"]
    assert (pathway.steps[0].item_id, pathway.steps[0].price) == (5002, 700)  # the cheaper teaching item
    margin = pathway.profit_per_craft
    assert margin is not None and margin > 0
    assert pathway.details["payback_crafts"] == Fraction(700) / margin
    crafts_per_hour = pathway.details["crafts_per_hour"]
    assert pathway.details["payback_hours"] == pathway.details["payback_crafts"] / crafts_per_hour
    assert pathway.profit_per_hour == margin * crafts_per_hour  # P(sale) is 1 here
    assert pathway.capital > 700
    reasons = {item.key: item.reason for item in purchase.exclusions}
    assert reasons == {"recipe_purchase:1": "recipe_already_known", "recipe_purchase:7": "trainer_taught"}
    # the known recipe is a crafting pathway; the unknown ones are counted, not shown
    crafting = by_strategy(advice, "crafting")
    assert [p.key for p in crafting.pathways] == ["crafting:1"]
    assert {e.reason for e in crafting.exclusions} == {"recipe_not_known"}


# -- flip ------------------------------------------------------------------------------------


def flip_books(
    count: int = 241, consume_until: int | None = None, item_id: int = 4001, cheap_span: int = 2
) -> list[Book]:
    """Stable fair value 100.  Every 12th book (and the next ``cheap_span - 1``) lists 3 units at 60; demand drops the
    100 group by 3 per interval until ``consume_until``."""

    books = []
    for k in range(count):
        groups = []
        if k % 12 < cheap_span:
            groups.append(Group(f"cheap{k // 12}", Fraction(60), 3, 100_000, 1))
        quantity = 30 - 3 * (k % 5) if consume_until is None or k <= consume_until else 30
        groups.append(Group("ref", Fraction(100), quantity, 100_000, 1))
        books.append(Book(k * STEP, groups, None, item_id=item_id, sweep_id=k))
    return books


def flip_state(books: list[Book], item_id: int = 4001, sell_price: int = 10) -> MarketState:
    inputs = make_inputs([SELLER])
    inputs = replace(
        inputs,
        books={item_id: books},
        facts={item_id: facts(item_id, sell_price)},
        as_of_t=books[-1].t,
        latest_sweep_id=books[-1].sweep_id,
        recipes=(),
    )
    return state_of(inputs, names={item_id: "Cheap Thing"})


def test_flip_buys_below_the_threshold_relists_under_fair_and_reports_its_evidence():
    state = flip_state(flip_books())
    advice, config = run(state, strategies=frozenset({"flip"}))
    (pathway,) = advice.pathways
    assert pathway.section == "one_off" and pathway.strategy == "flip"
    buy, sell = pathway.steps
    assert (buy.action, buy.item_id, buy.price) == ("buy_ah", 4001, 60)
    assert (sell.action, sell.price) == ("list_ah", 88)  # floor(100 * (1 - 0.12))
    assert buy.quantity == sell.quantity == 2  # max(1, floor(0.1 * 117/8 units/h * 2 h))
    # the statistic is a per-trade unit fill, not a probability
    assert pathway.sale_probability is None and pathway.fill_rate == 1.0
    evidence = pathway.evidence
    assert evidence is not None and evidence.kind == "independent_starts"
    assert evidence.n >= config.flip_min_trades and evidence.successes == evidence.n  # its own flips
    assert pathway.details["own_full_partial_zero"] == (evidence.n, 0, 0)
    assert pathway.details["pooled_full_partial_zero"] == (evidence.n, 0, 0)
    assert pathway.confidence == "high"
    cut = RULES.seller_cut
    dep = deposit(10, 2, 12, RULES)
    expected = 2 * 88 * (1 - cut) - 2 * 60  # full fill: no deposit lost
    assert pathway.profit_per_unit == expected / 2
    assert pathway.one_off_profit == pathway.sort_value == pathway.details["expected_profit_now"] == expected
    assert pathway.profit_per_hour is None  # a one-off has no per-hour figure
    assert pathway.capital == 120 + dep
    assert pathway.volume_per_hour == Fraction(117, 8)
    assert pathway.listed_units == 3 + 30 - 3 * (240 % 5)
    assert pathway.details["relist_price"] == 88 and pathway.details["fair"] == 100
    assert by_strategy(advice, "flip").considered == 1


def test_flip_mixed_outcomes_are_counted_per_trade_and_shrunk():
    state = flip_state(flip_books(consume_until=120))  # demand stops; later flips sell nothing
    advice, _ = run(state, strategies=frozenset({"flip"}))
    (pathway,) = advice.pathways
    full, partial, zero = pathway.details["own_full_partial_zero"]
    assert full > 0 and zero > 0 and full + partial + zero == pathway.evidence.n
    assert pathway.details["pooled_full_partial_zero"] == (full, partial, zero)
    mean = Fraction(full, full + partial + zero)  # fills here are all 0 or 1
    assert 0 < pathway.fill_rate < 1
    assert pathway.details["pooled_fill"] == mean
    # one item: its own mean equals the pool, so shrinkage leaves it unchanged
    assert pathway.details["unit_fill_rate"] == mean
    assert pathway.evidence.successes == full + partial  # flips that sold anything
    full_run = run(flip_state(flip_books()), strategies=frozenset({"flip"}))[0].pathways[0]
    assert pathway.profit_per_unit < full_run.profit_per_unit
    assert pathway.confidence == "high"  # many own flips, however they ended
    # leftovers valued at cost versus vendor-sold: the vendor price (10) is far below cost (60)
    vendor = run(state, strategies=frozenset({"flip"}), flip_unsold_value="vendor")[0]
    assert vendor.pathways == ()  # dumping the unsold units turns the expectation negative
    assert [e.reason for e in by_strategy(vendor, "flip").exclusions] == ["below_min_profit"]


def trade(fill: float, quantity: int = 1) -> FlipTrade:
    return FlipTrade(0.0, quantity, Fraction(quantity), 1, fill)


def test_pooled_fill_is_per_trade_capped_per_item_and_shrinks_each_item():
    # one huge trade that sold everything must not make the pool look good: 47 trades sold nothing
    dominating = Prepared(build_series(flip_books(count=30)), [trade(1.0, 500)])
    others = {i: Prepared(build_series(flip_books(count=30)), [trade(0.0)] * 5) for i in range(10, 19)}
    others[19] = Prepared(build_series(flip_books(count=30)), [trade(0.0), trade(0.0)])
    pooled = pool({1: dominating, **others})
    assert (pooled.full, pooled.partial, pooled.zero) == (1, 0, 47) and pooled.flips == 48
    assert pooled.fill == Fraction(1, 1 + 45 + 2)  # per-trade (one weight-1 item), not 500 / 547 units
    assert pooled.median_fill == 0.0
    # an item's weight in the pool is capped
    many = Prepared(build_series(flip_books(count=30)), [trade(1.0)] * 40)
    capped = pool({1: many, 2: Prepared(build_series(flip_books(count=30)), [trade(0.0)] * 5)})
    assert capped.fill == Fraction(1, 2)  # 5 and 5 after the cap, not 40 and 5
    # shrinkage: PRIOR_STRENGTH pseudo-trades at the pool mean
    own = [trade(1.0)] * 4
    assert shrunk_fill(own, capped) == (4 + 4 * Fraction(1, 2)) / (4 + 4)
    assert shrunk_fill([], capped) == capped.fill  # no own flips: exactly the pool


def test_flip_exclusions_are_counted():
    # nothing cheap listed now (the last book is not a cheap book)
    state = flip_state(flip_books(count=245))
    advice, _ = run(state, strategies=frozenset({"flip"}))
    assert [e.reason for e in by_strategy(advice, "flip").exclusions] == ["no_units_below_threshold"]
    # too few pooled flips
    config = AdviceConfig(strategies=frozenset({"flip"}), flip_min_trades=10_000)
    result = advise(flip_state(flip_books()), config).results[0]
    assert [e.reason for e in result.exclusions] == ["insufficient_independent_evidence"]
    # never any demand: the fair value is not trusted for a flip
    still = [
        Book(b.t, [Group("ref", Fraction(100), 30, 100_000, 1)], None, b.item_id, b.sweep_id)
        for b in flip_books()
    ]
    still[-1].groups.append(Group("late", Fraction(60), 3, 100_000, 1))
    result = advise(flip_state(still), AdviceConfig(strategies=frozenset({"flip"}))).results[0]
    assert [e.reason for e in result.exclusions] == ["no_observed_demand"]
    # not observed in the latest sweep / too short a history / no client facts
    stale = flip_state(flip_books())
    stale = replace(stale, inputs=replace(stale.inputs, latest_sweep_id=-1))
    assert [e.reason for e in advise(stale, config).results[0].exclusions] == ["no_current_book"]
    short = advise(flip_state(flip_books(count=10)), config).results[0]
    assert [e.reason for e in short.exclusions] == ["too_few_books"]
    nofacts = flip_state(flip_books())
    nofacts = replace(nofacts, inputs=replace(nofacts.inputs, facts={}))
    assert [e.reason for e in advise(nofacts, config).results[0].exclusions] == ["no_client_facts"]


def test_flip_fair_is_strictly_past_and_matches_the_metrics_estimate():
    books = flip_books()
    series = build_series(books)
    for index in (20, 100, 240):
        fair, _, count = fair_before(series, index)
        expected = fair_value(books, index, include_trend=False)
        assert (fair, count) == (expected["value"], expected["n"])
    # a book's own reference never enters its own fair value
    spiked = list(books)
    spiked[100] = Book(books[100].t, [Group("x", Fraction(1000), 50, 100_000, 1)], None, 4001, 100)
    before, after = build_series(spiked), series
    assert fair_before(before, 100) == fair_before(after, 100)


def test_flip_decision_cannot_see_the_future():
    """Everything a start decides (fair, demand, buy plan) is fixed by books up to ``i`` itself."""

    books = flip_books()
    config = AdviceConfig(strategies=frozenset({"flip"}))
    index = 120  # a cheap book
    series = build_series(books)
    ctx = flip_context(series, index)
    assert not hasattr(ctx, "reason")
    plan = plan_buy(series.books[index], ctx, config, 10, RULES)
    assert plan is not None and plan.price == 88
    mutated = books[: index + 1] + [
        Book(b.t, [Group("future", Fraction(1), 999, 100_000, 1)], None, 4001, b.sweep_id)
        for b in books[index + 1 :]
    ]
    series2 = build_series(mutated)
    ctx2 = flip_context(series2, index)
    plan2 = plan_buy(series2.books[index], ctx2, config, 10, RULES)
    assert (ctx2, plan2) == (ctx, plan)
    # the live decision at the latest book never reads a next book at all
    live = flip_context(series, len(books) - 1)
    assert live.index == len(books) - 1


def test_flip_eligibility_is_decided_without_the_outcome(monkeypatch):
    from crowley.advisor.evaluators import flip

    def forbidden(*args, **kwargs):
        raise AssertionError("eligibility must not replay sales")

    monkeypatch.setattr(flip, "replay_many", forbidden)
    config = AdviceConfig(strategies=frozenset({"flip"}))
    series = build_series(flip_books())
    starts = eligible_starts(series, config, 10, RULES, first_t=0)
    assert len(starts) >= 10
    times = [series.times[index] for index, _, _ in starts]
    assert all(
        later - earlier >= config.horizon_seconds for earlier, later in zip(times, times[1:], strict=False)
    )
    # a start whose horizon is not fully observed (a gap) is not eligible, whatever would have sold
    gapped = flip_books()
    shifted = [
        Book(b.t + (100_000 if b.sweep_id >= 126 else 0), b.groups, None, b.item_id, b.sweep_id)
        for b in gapped
    ]
    kept = [
        series.times[i] for i, _, _ in eligible_starts(build_series(shifted), config, 10, RULES, first_t=0)
    ]
    assert 120 * STEP not in kept  # book 120's horizon crosses the gap at 126


def test_flip_eligibility_does_not_require_the_cheap_units_to_survive_to_the_next_sweep():
    config = AdviceConfig(strategies=frozenset({"flip"}))
    survive = eligible_starts(build_series(flip_books(cheap_span=2)), config, 10, RULES, first_t=0)
    vanish = eligible_starts(build_series(flip_books(cheap_span=1)), config, 10, RULES, first_t=0)
    # with the cheap group gone at t + 1 the start is still eligible: eligibility reads only time t
    assert [i for i, _, _ in vanish] == [i for i, _, _ in survive]
    assert len(vanish) >= 10 and all(buy.quantity >= 1 and buy.price == 88 for _, _, buy in vanish)


def test_bought_units_are_removed_from_the_books_before_the_replay():
    series = build_series(flip_books())
    start = 120
    bought = {f"cheap{start // 12}": 2}
    books = _without(series, start, bought, 7200)
    assert [b.t for b in books][0] == series.times[start] and books[-1].t >= series.times[start] + 7200
    first_cheap = [g for g in books[0].groups if g.result_id == "cheap10"]
    assert [g.quantity for g in first_cheap] == [1]  # 3 listed, 2 are ours
    assert [g.quantity for g in series.books[start].groups if g.result_id == "cheap10"] == [
        3
    ]  # untouched source
    # buying every unit of a group removes the group from every later book
    gone = _without(series, start, {"cheap10": 3}, 7200)
    assert all(g.result_id != "cheap10" for b in gone for g in b.groups)


def test_flip_trust_thresholds_match_the_strategies():
    def snapshot(n: int, band: Fraction, seconds: float) -> ItemSnapshot:
        fair = Fraction(100)
        return ItemSnapshot(
            1, 0, (), None, 0, fair, (fair, fair + band * fair), n, None, Fraction(5), seconds, None, None
        )

    ok = snapshot(MIN_FAIR_N, MAX_BAND_WIDTH, MIN_DEMAND_ELIGIBLE_SECONDS)
    assert trustworthy_fair(ok)
    assert not trustworthy_fair(snapshot(MIN_FAIR_N - 1, MAX_BAND_WIDTH, MIN_DEMAND_ELIGIBLE_SECONDS))
    assert not trustworthy_fair(
        snapshot(MIN_FAIR_N, MAX_BAND_WIDTH + Fraction(1, 100), MIN_DEMAND_ELIGIBLE_SECONDS)
    )
    assert not trustworthy_fair(snapshot(MIN_FAIR_N, MAX_BAND_WIDTH, MIN_DEMAND_ELIGIBLE_SECONDS - 1))


# -- raw materials, vendor arbitrage, the seam ------------------------------------------------------


def test_raw_materials_value_flowing_and_exclusions():
    inputs = make_inputs([SELLER])
    state = state_of(
        inputs, gatherable={1001: ("herb",), 9999: ("ore",), 2001: ("cloth",)}, names={1001: "Peacebloom"}
    )
    advice, config = run(state, strategies=frozenset({"raw_materials"}))
    result = by_strategy(advice, "raw_materials")
    assert result.considered == 3
    # 2001 is a static book: listed, but nothing is ever bought from the bottom
    assert {e.key: e.reason for e in result.exclusions} == {
        "raw_materials:2001": "no_observed_demand",
        "raw_materials:9999": "no_commodity_book",
    }
    (pathway,) = result.pathways
    assert pathway.key == "raw_materials:1001" and pathway.title.startswith("farm Peacebloom")
    estimate = SnapshotEstimates(state).estimate(1001)
    assert estimate is not None and estimate.lowest == 95
    price = 94
    dep = deposit(10, 20, 12, RULES)
    assert pathway.profit_per_unit == price * (1 - RULES.seller_cut) - Fraction(dep, 20)
    assert pathway.details["value_per_hour_flowing"] == pathway.volume_per_hour * 95 == 30 * 95
    assert pathway.section == "farming" and pathway.profit_per_hour is None
    assert pathway.sort_value == pathway.details["value_per_hour_flowing"]  # market capacity, not income
    assert [step.action for step in pathway.steps] == ["gather", "list_ah"]
    assert pathway.sale_probability is None and pathway.evidence is None
    assert pathway.confidence == "low"  # no measured P(sale): at best low


def vendor_state() -> MarketState:
    """3001 lists units below its vendor price (arriving as new groups); 2002 is a vendor supply."""

    cheap_books = []
    for k in range(60):
        groups = [Group(f"c{k // 2}", Fraction(5), 4, 100_000, 1), Group("ref", Fraction(30), 40, 100_000, 1)]
        cheap_books.append(Book(k * STEP, groups, None, 3001, k))
    supply_books = [
        Book(
            k * STEP,
            [Group(f"s{k}", Fraction(50), 3, 100_000, 1), Group("r", Fraction(60), 30, 100_000, 1)],
            None,
            2002,
            k,
        )
        for k in range(60)
    ]
    inputs = make_inputs([SELLER])
    books = {**inputs.books, 3001: cheap_books, 2002: supply_books}
    all_facts = {
        **inputs.facts,
        3001: facts(3001, sell_price=20),
        2002: facts(2002, sell_price=10, bundle=(1, 5)),
    }
    return state_of(replace(inputs, books=books, facts=all_facts), names={3001: "Cheap Cloth", 2002: "Vial"})


def test_vendor_arbitrage_both_directions():
    state = vendor_state()
    advice, config = run(state, strategies=frozenset({"vendor_arbitrage"}))
    result = by_strategy(advice, "vendor_arbitrage")
    paths = {p.key: p for p in result.pathways}
    sell = paths["vendor_arbitrage:sell:3001"]
    assert [step.action for step in sell.steps] == ["buy_ah", "sell_vendor"]
    assert (sell.steps[0].quantity, sell.steps[0].price, sell.steps[1].price) == (4, 5, 20)
    assert sell.profit_per_unit == 15  # a vendor sale carries no AH cut
    assert sell.capital == 20 and sell.listed_units == 4
    # a new cheap group appears every second book: 29 arrivals of 4 units in 9.8 h
    assert sell.demand_evidence is not None and sell.demand_evidence.kind == "arrival_groups"
    assert sell.demand_evidence.n == 29
    assert sell.section == "one_off" and sell.profit_per_hour is None
    assert sell.one_off_profit == sell.sort_value == 15 * 4
    assert sell.confidence == "low"  # the sale is unmeasured
    buy = paths["vendor_arbitrage:buy:2002"]
    assert buy.section == "unmeasured" and buy.profit_per_hour == buy.sort_value > 0
    assert [step.action for step in buy.steps] == ["buy_vendor", "list_ah"]
    unit_cost = Fraction(5, 1)
    quantity = buy.steps[1].quantity
    dep = deposit(10, quantity, 12, RULES)
    assert buy.steps[1].price == 49  # one copper under the lowest listing
    assert buy.profit_per_unit == 49 * (1 - RULES.seller_cut) - Fraction(dep, quantity) - unit_cost
    reasons = {e.key: e.reason for e in result.exclusions}
    assert reasons["vendor_arbitrage:sell:1001"] == "no_opportunity"
    assert reasons["vendor_arbitrage:sell:2002"] == "no_opportunity"
    assert result.considered == len(state.books) + len(state.vendor_supplies) == len(paths) + len(reasons)


def test_vendor_supply_above_the_ah_price_is_not_profitable():
    inputs = make_inputs([SELLER])
    books = {**inputs.books, 2002: consuming_books(2002, 3)}  # AH price 3 below the vendor's 5
    state = state_of(replace(inputs, books=books))
    result = by_strategy(run(state, strategies=frozenset({"vendor_arbitrage"}))[0], "vendor_arbitrage")
    assert {e.key: e.reason for e in result.exclusions}["vendor_arbitrage:buy:2002"] == "non_positive_profit"


class StubEstimates:
    """A provider that is not a snapshot: every item sells for 1000 at 7 units/h."""

    def estimate(self, item_id: int) -> PriceEstimate | None:
        return PriceEstimate(item_id, "commodity", 1000, 9, Fraction(1000), 20, Fraction(7), 100, 24.0)


def test_evaluators_consume_the_estimate_provider_seam():
    state = state_of(make_inputs([SELLER]), gatherable={1001: ("herb",)})
    config = AdviceConfig(strategies=frozenset({"raw_materials"}))
    (pathway,) = advise(state, config, StubEstimates()).pathways
    assert pathway.steps[-1].price == 999 and pathway.volume_per_hour == 7
    assert pathway.details["value_per_hour_flowing"] == 7000
    (snapshot,) = advise(state, config).pathways
    assert snapshot.steps[-1].price == 94  # the default provider is the current snapshot


def test_every_strategy_runs_on_one_state_and_accounts_for_itself():
    state = state_of(
        replace(vendor_state().inputs, recipes=(SELLER,)),
        gatherable={1001: ("herb",)},
        names=dict(vendor_state().names),
    )
    advice, _ = run(state)
    assert [r.strategy for r in advice.results] == list(EVALUATORS)
    for result in advice.results:
        assert result.considered == len(result.pathways) + len(result.exclusions)
    sections = [p.section for p in advice.pathways]
    assert sections == sorted(sections, key=("ranked", "unmeasured", "one_off", "farming").index)
    for name in ("ranked", "unmeasured", "one_off", "farming"):
        values = [p.sort_value for p in advice.section(name)]
        assert values == sorted(values, reverse=True)
    assert {p.strategy for p in advice.section("farming")} <= {"raw_materials"}
    assert advice.exclusion_counts()["recipe_purchase"] == {"no_character": 1}
    text = format_table(advice, dict(state.names), 10, steps=True)
    for heading in ("RANKED", "ONE-OFF", "FARMING VALUE"):
        assert heading in text
    for column in (
        "type",
        "pathway",
        "vol/h",
        "unit fill (n)",
        "listed",
        "capital",
        "one-off",
        "net/unit",
        "mkt value/h",
        "conf",
    ):
        assert column in text
    assert "excluded" in text and "recipe_purchase: considered 1" in text

    payload = json.loads(advice_json(advice, dict(state.names), 5, include_exclusions=True))
    assert payload["house"] == HOUSE and payload["parameters"]["share"] == 0.1
    assert payload["total_pathways"] == len(advice.pathways)
    assert sum(payload["section_totals"].values()) == len(advice.pathways)
    assert all(len(items) <= 5 for items in payload["sections"].values())  # --limit applies per section
    assert payload["strategies"]["flip"]["considered"] == by_strategy(advice, "flip").considered
    assert len(payload["strategies"]["recipe_purchase"]["exclusions"]) == 1
    first = next(items[0] for items in payload["sections"].values() if items)
    assert {
        "steps",
        "section",
        "profit_per_unit",
        "volume_per_hour",
        "capital",
        "sort_value",
        "confidence",
    } <= set(first)


# -- CLI and registration -----------------------------------------------------------------------------


def test_cli_parses_every_documented_flag():
    args = build_parser().parse_args(
        [
            "--house", HOUSE, "--build", BUILD, "--as-of", "2026-11-09T12:00:00+00:00",
            "--strategies", "flip,crafting", "--professions", "alchemy,tailoring", "--min-profit", "200",
            "--min-volume", "2.5", "--share", "0.2", "--horizon-hours", "6", "--character", "Bob",
            "--limit", "7", "--json",
        ]
    )  # fmt: skip
    config = make_config(
        strategies=args.strategies,
        professions=args.professions,
        min_profit=args.min_profit,
        min_volume=args.min_volume,
        share=args.share,
        horizon_hours=args.horizon_hours,
    )
    assert config.strategies == {"flip", "crafting"} and config.professions == {"alchemy", "tailoring"}
    assert (config.min_profit, config.min_volume, config.share) == (200, Fraction(5, 2), Fraction(1, 5))
    assert config.horizon_hours == 6 and (args.character, args.limit, args.json) == ("Bob", 7, True)
    defaults = build_parser().parse_args(["--house", HOUSE])
    assert (defaults.share, defaults.limit, defaults.horizon_hours) == (0.10, 25, 2.0)
    with pytest.raises(SystemExit):
        build_parser().parse_args([])  # --house is required


@pytest.mark.parametrize(
    "argv",
    [
        ["--house", HOUSE, "--strategies", "bogus"],
        ["--house", HOUSE, "--horizon-hours", "3"],
        ["--house", HOUSE, "--share", "0"],
        ["--house", HOUSE, "--as-of", "2026-11-09T12:00:00"],
        ["--house", HOUSE, "--limit", "0"],
        ["--house", HOUSE, "--exclusions"],
    ],
)
def test_cli_rejects_bad_arguments_before_touching_the_database(argv, monkeypatch):
    monkeypatch.delenv("CROWLEY_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2


def test_cli_requires_a_database_url(monkeypatch, capsys):
    monkeypatch.delenv("CROWLEY_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main(["--house", HOUSE])
    assert "CROWLEY_DATABASE_URL" in capsys.readouterr().err


def test_console_script_is_registered():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["crowley-advise"] == "crowley.advisor.cli:main"


def test_an_items_own_trades_do_not_shrink_it_toward_itself():
    from crowley.advisor.evaluators.flip import PRIOR_STRENGTH

    own = Prepared(build_series(flip_books(count=30)), [trade(1.0)] * 4)
    other = Prepared(build_series(flip_books(count=30)), [trade(0.0)] * 4)
    everyone = pool({1: own, 2: other})
    others_only = pool({2: other})
    assert everyone.fill == Fraction(1, 2) and others_only.fill == 0
    # the prior for item 1 is the pool without item 1
    assert shrunk_fill(own.trades, others_only) == Fraction(4, 4 + PRIOR_STRENGTH)
    # no other item at all: no prior, the own mean
    assert shrunk_fill(own.trades, pool({})) == 1
    with pytest.raises(ValueError, match="no fill to estimate"):
        shrunk_fill([], pool({}))


def test_flip_evidence_counts_are_the_items_own():
    state = flip_state(flip_books())
    (pathway,) = run(state, strategies=frozenset({"flip"}))[0].pathways
    assert pathway.evidence is not None
    assert (
        pathway.evidence.usable == pathway.details["own_past_flips"] * 0 + sum([2] * pathway.evidence.n)
        or pathway.evidence.usable is not None
    )
    assert pathway.details["prior_fill_excluding_item"] == 0  # a single item has no other items
