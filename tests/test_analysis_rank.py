from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from types import MappingProxyType

import pytest

from crowley.analysis.books import Book, Group, valid_intervals
from crowley.analysis.common import jsonable, reference
from crowley.analysis.rank import (
    RankConfig,
    RankInputs,
    build_parser,
    config_from_args,
    evaluate_trades,
    horizon_observable,
    levels_snapshot,
    parse_as_of,
    rank_trades,
    select_rows,
)
from crowley.analysis.time_to_sale import replay_listing, replay_many
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.trading.account_gate import AccountSnapshot
from crowley.trading.api import ItemFacts, Recipe, RecipeAcquisition, SkillRequirement, TeachingRequirement
from crowley.trading.fees import deposit
from crowley.trading.strategies.common import target_sell_price
from crowley.trading.strategies.crafting import DEFAULT_PARAMS
from crowley.trading.strategies.pricing import output_target_price, source_reagent

RULES = rules_for_house(House.parse("us-normal-alliance"))
ALCHEMY_LINE = 171
BOOKS = 60
STEP = 600
LAST_T = (BOOKS - 1) * STEP


def facts(item_id: int, sell_price: int = 10, bundle: tuple[int, int] | None = None) -> ItemFacts:
    size, price = bundle if bundle else (None, None)
    return ItemFacts(item_id, sell_price, size, price, 20, 0)


def moving_book(item_id: int, index: int, consumed_unit: int) -> Book:
    """Five units sit at ``consumed_unit``; the next book consumes them (demand 30 units/h)."""

    groups = [
        Group(f"low-{index}", Fraction(consumed_unit), 5, 100_000, 1),
        Group("ref", Fraction(100), 30, 100_000, 1),
    ]
    return Book(index * STEP, groups, None, item_id=item_id, sweep_id=index)


def consuming_books(item_id: int, consumed_unit: int) -> list[Book]:
    return [moving_book(item_id, index, consumed_unit) for index in range(BOOKS)]


def static_book(item_id: int, unit: int, quantity: int = 1000) -> list[Book]:
    return [
        Book(index * STEP, [Group("only", Fraction(unit), quantity, 100_000, 1)], None, item_id, index)
        for index in range(BOOKS)
    ]


def recipe(
    spell_id: int,
    outputs: tuple[tuple[int, int], ...],
    reagents: tuple[tuple[int, int], ...],
    profession: str = "alchemy",
    teaching: tuple[int, ...] = (),
    requirements: tuple[TeachingRequirement, ...] = (),
) -> Recipe:
    line = SkillRequirement(ALCHEMY_LINE, profession, 1, 1, 1, 1, 1)
    return Recipe(
        spell_id=spell_id,
        professions=frozenset({profession}),
        reagents=reagents,
        outputs=outputs,
        cooldown_group=None,
        cooldown_seconds=0,
        learn_evidence="test",
        acquisition=RecipeAcquisition(teaching, (line,), requirements),
    )


CONFIG = RankConfig(
    professions=frozenset({"alchemy"}),
    batch_crafts=2,
    min_fair_n=8,
    max_band_width=1.0,
    min_demand_hours=1.0,
    min_independent_starts=3,
)


def make_inputs(recipes, account=None, teaching_prices=None, **overrides) -> RankInputs:
    books = {
        1001: consuming_books(1001, 95),  # sells: consumed above the 94 target
        1002: consuming_books(1002, 90),  # consumed only at the 90 target: ties never fill
        2001: static_book(2001, 10),
    }
    all_facts = {
        1001: facts(1001),
        1002: facts(1002),
        1003: facts(1003),
        2001: facts(2001),
        2002: facts(2002, bundle=(1, 5)),  # vendor supply, no AH book
    }
    values = {
        "house_key": "us-normal-alliance",
        "as_of_t": float(LAST_T),
        "latest_sweep_id": BOOKS - 1,
        "recipes": recipes,
        "books": books,
        "facts": all_facts,
        "rules": RULES,
        "account": account,
        "teaching_prices": teaching_prices or {},
    }
    values.update(overrides)
    return RankInputs(**values)


SELLER = recipe(1, ((1001, 1),), ((2001, 2), (2002, 1)))


def test_margin_probability_and_profit_per_hour_math():
    evaluation = evaluate_trades(make_inputs([SELLER]), CONFIG)
    assert evaluation.excluded == ()
    (row,) = evaluation.rows
    assert row.target_prices == ((1001, 94),)
    # batch of 2: 4 AH units of 2001 at 10, 2 vendor units of 2002 at 5.
    assert row.reagents == ((2001, 2, "ah"), (2002, 1, "vendor"))
    assert row.reagent_cost_per_craft == Fraction(40 + 10, 2)
    batch_deposit = deposit(10, 2, 24, RULES)
    revenue = 2 * 94 * (1 - RULES.seller_cut) - batch_deposit
    assert row.net_revenue_per_craft == revenue / 2
    assert row.margin_per_craft == (revenue - 50) / 2
    assert row.capital_per_batch == 50 + batch_deposit
    assert row.sale_probability == 1.0
    # starts need 8 prior books and a full 2 h ahead, and independent ones are >= 2 h apart
    assert (row.usable_starts, row.independent_starts, row.sold_starts) == (40, 4, 4)
    assert row.market_units_per_hour == 30
    assert row.crafts_per_hour == Fraction(30, 4)  # share 1/4 of 30 units/h, one unit per craft
    assert row.profit_per_hour == row.margin_per_craft * Fraction(30, 4)


def test_reagent_sourcing_and_target_match_the_crafting_functions():
    row = evaluate_trades(make_inputs([SELLER]), CONFIG).rows[0]
    snapshot = levels_snapshot(2001, static_book(2001, 10)[-1])
    source = source_reagent(snapshot, facts(2001), 2001, 4)
    assert source is not None and source.kind == "ah" and source.full_cost == 40
    cheaper_vendor = source_reagent(snapshot, facts(2001, bundle=(1, 3)), 2001, 4)
    assert cheaper_vendor is not None and cheaper_vendor.kind == "vendor" and cheaper_vendor.full_cost == 12
    assert source_reagent(None, facts(2002, bundle=(1, 5)), 2002, 1) is not None
    assert source_reagent(None, None, 2002, 1) is None
    # the original inline expression, applied to the same snapshot, equals the extracted one
    output = replace(
        levels_snapshot(1001, moving_book(1001, 5, 95)), fair=Fraction(100), fair_band=(Fraction(100),) * 2
    )
    inline = target_sell_price(
        output,
        int(Fraction(str(DEFAULT_PARAMS["buy_ratio"])) * output.fair),
        floor_ratio=Fraction(str(DEFAULT_PARAMS["floor_ratio"])),
        cap_ratio=Fraction(str(DEFAULT_PARAMS["cap_ratio"])),
    )
    assert output_target_price(output, DEFAULT_PARAMS) == inline == row.target_prices[0][1]


def test_probability_filter_sort_and_all_flag():
    never = recipe(2, ((1002, 1),), ((2001, 2),))
    evaluation = evaluate_trades(make_inputs([SELLER, never]), CONFIG)
    by_spell = {row.spell_id: row for row in evaluation.rows}
    assert by_spell[1].sale_probability == 1.0
    assert by_spell[2].sale_probability == 0.0
    assert [row.spell_id for row in select_rows(evaluation.rows, CONFIG)] == [1]
    everything = select_rows(evaluation.rows, replace(CONFIG, include_all=True))
    profits = [row.profit_per_hour for row in everything]
    assert profits == sorted(profits, reverse=True)
    assert {row.spell_id for row in everything} == {1, 2}
    assert [row.spell_id for row in rank_trades(make_inputs([SELLER, never]), CONFIG)] == [1]


def test_exclusion_reasons_are_counted_not_dropped():
    missing_output = recipe(3, ((1003, 1),), ((2001, 1),))
    missing_reagent = recipe(4, ((1001, 1),), ((2999, 1),))
    other_profession = recipe(5, ((1001, 1),), ((2001, 1),), profession="tailoring")
    evaluation = evaluate_trades(
        make_inputs([SELLER, missing_output, missing_reagent, other_profession]), CONFIG
    )
    assert evaluation.considered == 3
    assert [row.spell_id for row in evaluation.rows] == [1]
    assert evaluation.exclusion_counts() == {"output_not_in_plan": 1, "reagent_unpriceable": 1}
    assert {item.spell_id for item in evaluation.excluded} == {3, 4}


def test_missing_book_causes_are_distinguished():
    not_commodity = recipe(10, ((1003, 1),), ((2001, 1),))
    not_in_plan = recipe(11, ((1004, 1),), ((2001, 1),))
    stale = recipe(12, ((1001, 1),), ((2001, 1),))
    base = make_inputs([not_commodity, not_in_plan], observed_kinds={1003: "item"})
    base.facts  # noqa: B018 - facts for 1004 are irrelevant: the book is checked first
    assert evaluate_trades(base, CONFIG).exclusion_counts() == {
        "output_not_commodity": 1,
        "output_not_in_plan": 1,
    }
    assert evaluate_trades(make_inputs([stale], latest_sweep_id=BOOKS), CONFIG).exclusion_counts() == {
        "output_no_current_book": 1
    }


def test_random_yield_empty_and_invalid_outputs_are_excluded_not_raised():
    empty = recipe(20, (), ((2001, 1),))
    zero = recipe(21, ((1001, 0),), ((2001, 1),))
    evaluation = evaluate_trades(make_inputs([empty, zero], random_yield_spells=(900, 901)), CONFIG)
    assert evaluation.rows == ()
    assert evaluation.considered == 4
    assert evaluation.exclusion_counts() == {
        "invalid_output_quantity": 1,
        "no_outputs": 1,
        "random_yield": 2,
    }


def test_thin_history_is_reported_not_faked():
    strict = replace(CONFIG, min_fair_n=100)
    evaluation = evaluate_trades(make_inputs([SELLER]), strict)
    assert evaluation.rows == ()
    assert evaluation.exclusion_counts() == {"output_fair_untrusted": 1}
    demanding = replace(CONFIG, min_demand_hours=24.0)
    assert evaluate_trades(make_inputs([SELLER]), demanding).exclusion_counts() == {
        "output_fair_untrusted": 1
    }


def test_correlated_overlapping_starts_are_not_independent_evidence():
    # 40 overlapping usable starts are still only four independent 2 h windows.
    demanding = replace(CONFIG, min_independent_starts=5)
    evaluation = evaluate_trades(make_inputs([SELLER]), demanding)
    assert evaluation.rows == ()
    assert evaluation.exclusion_counts() == {"insufficient_independent_evidence": 1}
    assert "4 independent starts (40 usable)" in evaluation.excluded[0].detail
    # a shorter horizon gives more independent windows over the same books
    shorter = replace(CONFIG, min_independent_starts=5, horizon_hours=0.5)
    (row,) = evaluate_trades(make_inputs([SELLER]), shorter).rows
    assert row.independent_starts >= 5 and row.sold_starts == row.independent_starts


def test_one_off_depletion_cannot_make_a_certain_sale():
    # Only a single book transition consumes anything; every other interval is untouched.
    books = []
    for index in range(BOOKS):
        groups = [Group("ref", Fraction(100), 30, 100_000, 1)]
        if index == 20:
            groups.append(Group("blip", Fraction(95), 5, 100_000, 1))
        books.append(Book(index * STEP, groups, None, 1001, index))
    data = make_inputs([SELLER], books={1001: books, 2001: static_book(2001, 10)})
    row = evaluate_trades(data, replace(CONFIG, min_independent_starts=1)).rows
    # nothing sells at the relative target except through the one blip
    (only,) = row
    assert only.sold_starts <= 1 < only.independent_starts
    assert only.sale_probability < 0.5


def test_target_above_the_lowest_listing_is_flagged_and_never_fills_from_cheaper_units():
    # Five cheap units at 30 are consumed every interval; the fair value (100) sets a target of 99.
    cheap = [
        Book(
            index * STEP,
            [Group(f"low-{index}", Fraction(30), 5, 100_000, 1), Group("ref", Fraction(100), 30, 100_000, 1)],
            None,
            1001,
            index,
        )
        for index in range(BOOKS)
    ]
    row = evaluate_trades(
        make_inputs([SELLER], books={1001: cheap, 2001: static_book(2001, 10)}), CONFIG
    ).rows[0]
    assert row.target_prices == ((1001, 99),)
    assert row.target_above_lowest
    assert row.sale_probability == 0.0
    assert evaluate_trades(make_inputs([SELLER]), CONFIG).rows[0].target_above_lowest is False


def test_multi_output_recipe_takes_min_probability_limiting_rate_and_sums_duplicates():
    multi = recipe(30, ((1001, 1), (1002, 1), (1001, 1)), ((2001, 2),))
    (row,) = evaluate_trades(make_inputs([multi]), CONFIG).rows
    assert row.outputs == ((1001, 2), (1002, 1))
    assert row.sale_probability == 0.0  # 1002 only ever sells at its own tie price
    assert row.sold_starts == 0
    # market 30 units/h each; 1001 needs 2 per craft so it limits: 1/4 * 30 / 2
    assert row.crafts_per_hour == Fraction(15, 4)
    assert row.market_units_per_hour == 30
    assert {item for item, _ in row.target_prices} == {1001, 1002}
    per_craft = {item: price for item, price in row.target_prices}
    batch_deposit = deposit(10, 4, 24, RULES) + deposit(10, 2, 24, RULES)
    revenue = (4 * per_craft[1001] + 2 * per_craft[1002]) * (1 - RULES.seller_cut) - batch_deposit
    assert row.margin_per_craft == (revenue - 40) / 2


def _gapped(item_id: int, consumed_unit: int, gap: int, after: int) -> list[Book]:
    """Books at STEP spacing with a ``gap``-second hole before index ``after``."""

    books = consuming_books(item_id, consumed_unit)
    return [replace(book, t=book.t + (gap if index >= after else 0)) for index, book in enumerate(books)]


def test_gap_inside_the_horizon_excludes_sold_and_unsold_starts_alike():
    gapped = _gapped(1001, 95, 2400, 30)  # 40 min hole between books 29 and 30 (> MAX_GAP)
    intervals = valid_intervals(gapped)
    assert intervals[29] is None
    horizon = 7200
    # k=28 would sell within 10 min and k=18 is unsold when the break arrives: both are ineligible,
    # because eligibility ignores the outcome; k=10 sees the whole horizon.
    assert not horizon_observable(gapped, intervals, 28, 94, horizon)
    assert not horizon_observable(gapped, intervals, 18, 94, horizon)
    assert horizon_observable(gapped, intervals, 17, 94, horizon)
    assert horizon_observable(gapped, intervals, 30, 94, horizon)
    assert not horizon_observable(gapped, intervals, 55, 94, horizon)  # horizon past the data

    data = make_inputs(
        [SELLER], books={1001: gapped, 2001: static_book(2001, 10)}, as_of_t=float(LAST_T + 2400)
    )
    (row,) = evaluate_trades(data, replace(CONFIG, min_independent_starts=1)).rows
    assert row.usable_starts == 28  # starts 8..17 and 30..47


def test_unsold_start_at_a_break_is_not_dropped_differently_from_a_sold_one():
    # Demand stops before the hole, so late pre-gap starts neither sell nor survive the horizon.
    books = consuming_books(1001, 95)
    for index in range(20, BOOKS):
        books[index] = Book(books[index].t, [Group("ref", Fraction(100), 30, 100_000, 1)], None, 1001, index)
    books = [replace(book, t=book.t + (2400 if index >= 30 else 0)) for index, book in enumerate(books)]
    data = make_inputs(
        [SELLER], books={1001: books, 2001: static_book(2001, 10)}, as_of_t=float(LAST_T + 2400)
    )
    (row,) = evaluate_trades(data, replace(CONFIG, min_independent_starts=1)).rows
    # every eligible start either sells in the first stretch or sees a full unbroken 2 h unsold
    assert row.usable_starts == 28
    assert row.sold_starts < row.independent_starts


def test_early_starts_use_the_extra_history():
    # Books before the lookback start still supply fair value for starts inside it.
    books = consuming_books(1001, 95)
    shifted = [replace(book, t=book.t + 100_000) for book in books]
    data = make_inputs(
        [SELLER],
        books={1001: shifted, 2001: [replace(b, t=b.t + 100_000) for b in static_book(2001, 10)]},
        as_of_t=float(LAST_T + 100_000),
    )
    narrow = replace(CONFIG, lookback_hours=3.0, min_independent_starts=1)
    row = evaluate_trades(data, narrow).rows[0]
    # the 3 h lookback starts at book 41; without extra history no start would have 8 prior books
    assert row.usable_starts == 7  # starts 41..47: inside the 3 h lookback with a full 2 h ahead


def test_unknown_profession_fails_loudly():
    with pytest.raises(ValueError, match="no client recipes"):
        evaluate_trades(
            make_inputs([SELLER]), replace(CONFIG, professions=frozenset({"alchemy", "jewelcrafting"}))
        )


def _account(known: frozenset[int]) -> AccountSnapshot:
    return AccountSnapshot(
        1,
        "uid",
        datetime(2026, 1, 1, tzinfo=UTC),
        "0" * 64,
        "Char",
        known,
        MappingProxyType({ALCHEMY_LINE: 150}),
        frozenset({ALCHEMY_LINE}),
    )


def test_recipe_status_price_and_payback():
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
    account = _account(frozenset({1}))
    prices = {5001: 900, 5002: 700}
    evaluation = evaluate_trades(make_inputs([SELLER, learnable], account, prices), CONFIG)
    known, buyable = evaluation.rows
    assert known.status == "known" and known.recipe_price is None and known.recipe_price_state is None
    assert buyable.status == "learnable_now"
    assert (buyable.recipe_price, buyable.recipe_item_id, buyable.recipe_price_state) == (700, 5002, "listed")
    assert buyable.payback_crafts == Fraction(700) / buyable.margin_per_craft
    assert buyable.payback_hours == buyable.payback_crafts / buyable.crafts_per_hour

    unlisted = evaluate_trades(make_inputs([learnable], account, {5001: None, 5002: None}), CONFIG).rows[0]
    assert (unlisted.recipe_price_state, unlisted.recipe_price, unlisted.payback_crafts) == (
        "not_listed",
        None,
        None,
    )
    unobserved = evaluate_trades(make_inputs([learnable], account, {}), CONFIG).rows[0]
    assert unobserved.recipe_price_state == "not_observed"
    trainer_recipe = recipe(7, ((1001, 1),), ((2001, 2),))
    (trainer,) = evaluate_trades(make_inputs([trainer_recipe], account), CONFIG).rows
    assert trainer.status == "requirement_unknown" and trainer.recipe_price_state == "trainer"
    no_character = evaluate_trades(make_inputs([learnable], None, prices), CONFIG).rows[0]
    assert no_character.status is None and no_character.recipe_price is None


def test_replay_many_matches_replay_listing():
    books = consuming_books(1001, 95)
    many = replay_many(books, [(index, 94) for index in range(len(books))], 2)
    for index, result in enumerate(many):
        single = replay_listing(books, index, 94, 2)
        assert (result.event_time, result.censor_time) == (single.event_time, single.censor_time)


def test_cli_arguments_and_validation():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--house",
            "us-normal-alliance",
            "--build",
            "1.60.1.70862",
            "--character",
            "Dandune",
            "--professions",
            "alchemy, tailoring",
            "--horizon-hours",
            "4",
            "--min-sale-prob",
            "0.7",
            "--share",
            "0.1",
            "--all",
            "--json",
        ]
    )
    config = config_from_args(args)
    assert config.professions == {"alchemy", "tailoring"}
    assert (config.horizon_hours, config.min_sale_prob, config.share) == (4.0, 0.7, Fraction(1, 10))
    assert config.include_all and args.json and args.character == "Dandune"
    defaults = config_from_args(parser.parse_args(["--house", "us-normal-alliance"]))
    assert (defaults.horizon_hours, defaults.min_sale_prob, defaults.share, defaults.lookback_hours) == (
        2.0,
        0.5,
        Fraction(1, 4),
        24.0,
    )
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(ValueError, match="horizon_hours"):
        config_from_args(parser.parse_args(["--house", "x", "--horizon-hours", "30"]))


def test_as_of_must_be_timezone_aware():
    assert parse_as_of("2026-11-09T12:00:00+00:00").utcoffset() is not None
    with pytest.raises(ValueError, match="UTC offset"):
        parse_as_of("2026-11-09T12:00:00")


def test_common_helpers():
    assert jsonable({"a": (Fraction(3), Fraction(1, 2))}) == {"a": [3, "1/2"]}
    assert reference(moving_book(1, 0, 95)) == 100
    assert reference(Book(0, [Group("x", Fraction(5), 19, 1)])) is None
