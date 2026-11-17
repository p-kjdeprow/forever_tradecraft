"""Pure regression coverage for the token-free crafting dashboard summary."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest
from crowley_common.auction_rules import rules_for_house
from crowley_common.house import House
from crowley_market import client_data
from crowley_market.analysis import trading_summary as summary
from crowley_market.analysis.books import Book, Group
from crowley_market.analysis.item_books import Auction, ItemBook
from crowley_market.models import (
    ItemFacts,
    Recipe,
    RecipeAcquisition,
    SkillRequirement,
    TeachingRequirement,
)

HOUSE = "us-normal-alliance"
BUILD = "1.60.1.70938"
ASOF = 1_000_000


def _recipe(
    spell_id: int = 1,
    *,
    profession: str = "alchemy",
    output_id: int = 1000,
    output_quantity: int = 1,
    reagent_id: int = 2000,
    reagent_quantity: int = 1,
    learning_rank: int | None = 195,
    yellow: int = 100,
) -> Recipe:
    skill_line_id = 171 if profession == "alchemy" else 197
    skill_line = SkillRequirement(skill_line_id, profession, 1, yellow, 80, 50, 0)
    teaching = () if learning_rank is None else (TeachingRequirement(5000, skill_line_id, learning_rank),)
    return Recipe(
        spell_id=spell_id,
        professions=frozenset({profession}),
        reagents=((reagent_id, reagent_quantity),),
        outputs=((output_id, output_quantity),),
        cooldown_group=None,
        cooldown_seconds=0,
        learn_evidence="client",
        acquisition=RecipeAcquisition(skill_lines=(skill_line,), teaching_requirements=teaching),
    )


def _commodity_history(
    item_id: int,
    *,
    interval_seconds: int = 900,
    units_removed: int = 10,
    unit_price: int = 100,
    first_quantity: int = 10_000,
) -> list[Book]:
    books = []
    steps = 6 * 3600 // interval_seconds
    for index in range(steps + 1):
        at = ASOF - 6 * 3600 + index * interval_seconds
        books.append(
            Book(
                float(at),
                [Group("level", Fraction(unit_price), first_quantity - index * units_removed, 100_000, 1)],
                None,
                item_id,
                index + 1,
            )
        )
    return books


def _commodity_points(
    item_id: int,
    points: list[tuple[int, int]],
    *,
    ceiling: Fraction | None = None,
) -> list[Book]:
    """Build exact-price fixture books from (epoch offset, remaining quantity) pairs."""

    return [
        Book(
            float(ASOF + offset),
            [Group("level", Fraction(100), quantity, 100_000, 1)],
            ceiling,
            item_id,
            index + 1,
        )
        for index, (offset, quantity) in enumerate(points)
    ]


def _inputs(
    recipes: tuple[Recipe, ...] | list[Recipe],
    *,
    books: dict[int, list[Book]] | None = None,
    facts: dict[int, ItemFacts] | None = None,
    item_books: dict[int, list[ItemBook]] | None = None,
    sweep_plans: dict[int, summary.SweepSamplingPlan] | None = None,
    names: dict[int, str] | None = None,
) -> summary.TradingSummaryInputs:
    book_map = books or {}
    item_book_map = item_books or {}
    fixture_sweep_ids = {
        int(book.sweep_id)
        for histories in (*book_map.values(), *item_book_map.values())
        for book in histories
        if type(book.sweep_id) is int
    }
    fixture_plans = (
        {sweep_id: summary.SweepSamplingPlan(900, 0, frozenset()) for sweep_id in fixture_sweep_ids}
        if sweep_plans is None
        else sweep_plans
    )
    return summary.TradingSummaryInputs(
        house=HOUSE,
        client_build=BUILD,
        sweep_id=583,
        finished_epoch=ASOF,
        scan={"planned": 484, "queried": 484, "successful": 483},
        recipes=recipes,
        facts=facts or {},
        commodity_books=book_map,
        item_books=item_book_map,
        sweep_plans=fixture_plans,
        names=names or {},
        rules=rules_for_house(House.parse(HOUSE)),
    )


def _facts(
    *,
    output_sell: int = 7,
    output_max_stack: int = 3,
    reagent_vendor_size: int | None = None,
    reagent_vendor_price: int | None = None,
) -> dict[int, ItemFacts]:
    return {
        1000: ItemFacts(1000, output_sell, None, None, output_max_stack, 2),
        2000: ItemFacts(2000, 1, reagent_vendor_size, reagent_vendor_price, 20, 0),
    }


def _wire_item_row(
    result_id: int, price: int, *, key: str = "1000", owner: str | None = "seller"
) -> tuple[Auction, dict]:
    grouped = owner is None
    names = {"seller-a": True, "seller-b": True} if grouped else {owner: True}
    owners = {
        "names": names,
        "identityKind": "grouped" if grouped else "individual",
        "containsOwnerItem": False,
        "containsAccountItem": False,
        "totalNumberOfOwners": 2 if grouped else 1,
    }
    row = {
        "resultID": result_id,
        "quantity": 1,
        "unitPrice": {"numerator": price, "denominator": 1},
        "itemKey": key,
        "rawItemKey": {"itemID": int(key)},
        "owners": owners,
        "timeLeft": {"kind": "band", "minimumSeconds": 100_000, "maximumSeconds": 100_001},
    }
    auction = Auction(result_id, Fraction(price), 1, key, owner, False)
    return auction, row


def _item_book(
    index: int, at: int, *, grouped: bool = False, key: str = "1000", quantity: int = 1
) -> ItemBook:
    auction, row = _wire_item_row(index + 1, 100, owner=None if grouped else "seller", key=key)
    row["quantity"] = quantity
    auction = Auction(auction.result_id, auction.unit, quantity, auction.item_key, auction.owner, auction.own)
    record = {
        "observation": {
            "kind": "item",
            "coverage": "full",
            "sourceComplete": True,
            "cutoff": {"kind": "unbounded"},
            "query": {"kind": "queried", "startedAt": at, "finishedAt": at + 10},
            "itemID": 1000,
            "rows": [row],
        },
        "envelope": {
            "house": {"key": HOUSE},
            "context": {"clientVersion": "1.60.1", "clientBuild": "70938"},
            "planHash": "a" * 64,
        },
    }
    return ItemBook(float(at), 1000, index + 1, (auction,), record)


def test_batch_economics_use_whole_vendor_bundles_and_per_stack_fees():
    recipe = _recipe(output_quantity=4, reagent_quantity=4)
    inputs = _inputs(
        [recipe],
        books={
            1000: _commodity_history(1000, units_removed=10),
            # A fresh, insufficient AH quote still allows the known vendor source.
            2000: [Book(float(ASOF), [Group("few", Fraction(100), 1, 100_000)], None, 2000, 583)],
        },
        facts=_facts(reagent_vendor_size=3, reagent_vendor_price=100),
        names={1000: "Output", 2000: "Vendor reagent"},
    )

    result = summary.build_trading_summary(inputs)
    row = result["opportunities"][0]

    assert row["crafts"] == 2
    assert row["quantity"] == 8
    assert row["unit_list_price"] == "99"
    assert row["reagents"] == [
        {
            "item_id": 2000,
            "name": "Vendor reagent",
            "quantity": 8,
            "source": "vendor",
            "cost": "300",
            "bundle_quantity": 9,
        }
    ]
    assert row["material_cost"] == "300"
    assert row["deposit_cost"] == "11"
    assert row["cost"] == "311"
    assert row["sale_proceeds"] == "752"
    assert row["profit"] == "441"
    assert row["profit_per_item"] == "441/8"
    assert row["market_units_per_hour"] == 40
    assert row["sale_probability"] is None
    assert row["volume_status"] == "observed_activity"
    assert any("assume a 10% share of observed market activity" in note for note in result["notes"])


def test_profit_per_item_uses_exact_total_output_quantity_for_five_crafts():
    recipe = _recipe(output_quantity=4, reagent_quantity=1)
    inputs = _inputs(
        [recipe],
        books={
            1000: _commodity_history(1000, units_removed=250, unit_price=101),
            2000: [
                Book(
                    float(ASOF),
                    [Group("fractional", Fraction(1, 3), 100, 100_000)],
                    None,
                    2000,
                    583,
                )
            ],
        },
        facts=_facts(output_sell=7, output_max_stack=3),
    )

    row = summary.build_trading_summary(inputs)["candidates"][0]

    assert row["crafts"] == 5
    assert row["quantity"] == 20
    exact_per_item = Fraction(row["profit"]) / row["quantity"]
    assert Fraction(row["profit_per_item"]) == exact_per_item
    assert exact_per_item.denominator > 1


def test_vendor_only_reagent_needs_no_auction_book():
    inputs = _inputs(
        [_recipe(output_quantity=4, reagent_quantity=2)],
        books={1000: _commodity_history(1000)},
        facts=_facts(reagent_vendor_size=5, reagent_vendor_price=40),
    )

    row = summary.build_trading_summary(inputs)["opportunities"][0]

    assert row["reagents"][0]["source"] == "vendor"
    assert row["crafts"] == 2
    assert row["reagents"][0]["quantity"] == 4
    assert row["reagents"][0]["bundle_quantity"] == 5
    assert row["reagents"][0]["cost"] == "40"


def test_insufficient_ah_depth_shrinks_batch_before_excluding_recipe():
    recipe = _recipe(output_quantity=1, reagent_quantity=2)
    inputs = _inputs(
        [recipe],
        books={
            1000: _commodity_history(1000, units_removed=25),
            2000: [Book(float(ASOF), [Group("depth", Fraction(1), 2, 100_000)], None, 2000, 583)],
        },
        facts=_facts(output_sell=1, output_max_stack=20),
    )

    row = summary.build_trading_summary(inputs)["opportunities"][0]

    assert row["market_units_per_hour"] == 100
    assert row["crafts"] == 1
    assert row["reagents"][0]["cost"] == "2"


def test_cooldown_caps_ranking_flow_and_rows_expose_shared_group():
    quick = _recipe(spell_id=20, output_quantity=1, reagent_quantity=1)
    cooldown = replace(quick, spell_id=21, cooldown_seconds=3600, cooldown_group="shared transmute")
    inputs = _inputs(
        [cooldown, quick],
        books={
            1000: _commodity_history(1000, units_removed=250),
        },
        facts=_facts(output_sell=0, output_max_stack=100, reagent_vendor_size=1, reagent_vendor_price=1),
    )

    result = summary.build_trading_summary(inputs)
    rows = {row["spell_id"]: row for row in result["opportunities"]}

    assert [row["spell_id"] for row in result["opportunities"]] == [20, 21]
    assert result["opportunities"] == result["candidates"]
    assert rows[20]["crafts"] == 5
    assert rows[21]["crafts"] == 1
    assert rows[21]["cooldown_seconds"] == 3600
    assert rows[21]["cooldown_group"] == "shared transmute"
    assert any("share one timer" in note for note in result["notes"])
    assert not any("unmeasured trials" in note for note in result["notes"])


@pytest.mark.parametrize("profession", ["alchemy", "tailoring"])
@pytest.mark.parametrize("units_removed", [0, 10])
def test_higher_skill_recipe_can_outrank_ready_recipe_without_training_penalty(profession, units_removed):
    ready = _recipe(spell_id=10, profession=profession, output_id=1000, learning_rank=140)
    higher = _recipe(spell_id=11, profession=profession, output_id=1001, learning_rank=300)
    facts = _facts(output_sell=0, reagent_vendor_size=1, reagent_vendor_price=1)
    facts[1001] = ItemFacts(1001, 0, None, None, 20, 2)
    result = summary.build_trading_summary(
        _inputs(
            [ready, higher],
            books={
                1000: _commodity_history(1000, units_removed=units_removed, unit_price=100),
                1001: _commodity_history(1001, units_removed=units_removed, unit_price=200),
            },
            facts=facts,
        )
    )
    rows = result["candidates"]
    assert [row["spell_id"] for row in rows] == [11, 10]
    assert rows[0]["needs_leveling"] is True
    assert rows[1]["needs_leveling"] is False
    assert Fraction(rows[0]["profit_per_item"]) > Fraction(rows[1]["profit_per_item"])
    assert [row["spell_id"] for row in rows if row["eligibility_skill"] <= 225] == [10]
    assert [row["spell_id"] for row in rows if row["eligibility_skill"] <= 300] == [11, 10]


def test_tailoring_caps_distinguish_exact_rank_from_yellow_threshold():
    exact_leveling = _recipe(
        spell_id=10,
        profession="tailoring",
        output_id=1000,
        reagent_id=2000,
        learning_rank=195,
    )
    yellow_leveling = _recipe(
        spell_id=11,
        profession="tailoring",
        output_id=1000,
        reagent_id=2000,
        learning_rank=None,
        yellow=205,
    )
    over_cap = _recipe(spell_id=12, profession="tailoring", output_id=1000, learning_rank=301)
    unknown = replace(exact_leveling, spell_id=13, acquisition=RecipeAcquisition())
    inputs = _inputs(
        [exact_leveling, yellow_leveling, over_cap, unknown],
        books={1000: _commodity_history(1000)},
        facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
    )

    rows = {row["spell_id"]: row for row in summary.build_trading_summary(inputs)["opportunities"]}

    assert rows[10]["needs_leveling"] is True
    assert rows[10]["required_skill"] == 195
    assert rows[10]["eligibility_skill"] == 195
    assert rows[11]["needs_leveling"] is True
    assert rows[11]["required_skill"] is None
    assert rows[11]["eligibility_skill"] == 205
    assert rows[11]["skill_note"] == "Eligibility by 205; learning rank unknown; verify training"
    assert 12 not in rows and 13 not in rows


@pytest.mark.parametrize("profession", ["alchemy", "tailoring"])
def test_candidate_pool_includes_skill_boundaries_through_300(profession):
    ranks = [150, 151, 225, 226, 300, 301]
    recipes = [_recipe(spell_id=rank, profession=profession, learning_rank=rank) for rank in ranks]
    result = summary.build_trading_summary(
        _inputs(
            recipes,
            books={1000: _commodity_history(1000)},
            facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
        )
    )
    rows = {row["spell_id"]: row for row in result["candidates"]}
    assert set(rows) == {150, 151, 225, 226, 300}
    assert all(row["eligibility_skill"] == rank for rank, row in rows.items())
    assert rows[300]["needs_leveling"] is True


def test_long_gaps_and_grouped_item_books_keep_profitable_rows_with_unknown_activity():
    recipe = _recipe(output_id=1000, reagent_id=2000)
    long_gap_inputs = _inputs(
        [recipe],
        books={
            1000: _commodity_history(1000, interval_seconds=5_400),
        },
        facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
    )
    row = summary.build_trading_summary(long_gap_inputs)["opportunities"][0]
    assert row["volume_status"] == "unknown"
    assert row["market_units_per_hour"] is None

    item_books = [_item_book(index, ASOF - 6 * 3600 + index * 900, grouped=True) for index in range(25)]
    grouped_inputs = _inputs(
        [recipe],
        item_books={1000: item_books},
        facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
    )
    row = summary.build_trading_summary(grouped_inputs)["opportunities"][0]
    assert row["volume_status"] == "unknown"
    assert row["market_units_per_hour"] is None


def test_unknown_activity_remains_in_candidate_pool_and_preserves_existing_ranking():
    qualified_ids = range(1000, 1013)
    unknown_ids = range(1013, 1025)
    recipes = [
        _recipe(spell_id=item_id, output_id=item_id, reagent_id=2000)
        for item_id in (*qualified_ids, *unknown_ids)
    ]
    books = {item_id: _commodity_history(item_id, units_removed=2) for item_id in qualified_ids}
    books.update(
        {
            item_id: [
                Book(
                    float(ASOF),
                    [Group(f"high-ask-{item_id}", Fraction(1_000_000), 1, 1_000_000, 1)],
                    None,
                    item_id,
                    583,
                )
            ]
            for item_id in unknown_ids
        }
    )
    facts = {item_id: ItemFacts(item_id, 7, None, None, 20, 2) for item_id in (*qualified_ids, *unknown_ids)}
    facts[2000] = ItemFacts(2000, 1, 1, 1, 20, 0)

    result = summary.build_trading_summary(_inputs(recipes, books=books, facts=facts))

    assert len(result["opportunities"]) == 10
    assert len(result["candidates"]) == len(qualified_ids) + len(unknown_ids)
    assert [row["item_id"] for row in result["candidates"]] == [*qualified_ids, *unknown_ids]
    assert result["opportunities"] == result["candidates"][:10]
    assert {row["item_id"] for row in result["opportunities"]} == set(range(1000, 1010))
    assert all(row["volume_status"] == "observed_activity" for row in result["opportunities"])
    assert all(row["market_units_per_hour"] >= 6 for row in result["opportunities"])


@pytest.mark.parametrize("interval_seconds", [3601, 3600, 3599])
def test_activity_near_six_units_per_hour_does_not_filter_profitable_rows(interval_seconds):
    books = _commodity_points(1000, [(-interval_seconds, 100), (0, 94)])
    plans = {int(book.sweep_id): summary.SweepSamplingPlan(900, 4, frozenset({1000})) for book in books}
    result = summary.build_trading_summary(
        _inputs(
            [_recipe()],
            books={1000: books},
            sweep_plans=plans,
            facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
        )
    )
    assert result["candidates"]
    assert result["opportunities"][0]["market_units_per_hour"] == round(6 * 3600 / interval_seconds, 2)


def test_usable_24h_history_with_zero_depletion_keeps_profitable_rows():
    recipe = _recipe()
    result = summary.build_trading_summary(
        _inputs(
            [recipe],
            books={
                1000: _commodity_history(1000, units_removed=0),
            },
            facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
        )
    )

    assert len(result["candidates"]) == 1
    row = result["opportunities"][0]
    assert row["market_units_per_hour"] == 0
    assert row["volume_status"] == "observed_no_depletion"


def test_short_positive_history_after_six_hours_qualifies_without_filling_missing_time():
    recipe = _recipe()
    points = [(-26 * 3600, 2_000), (-26 * 3600 + 900, 1_000), (-10 * 3600, 100)]
    points.extend((-10 * 3600 + step * 900, 100 - step * 5) for step in range(1, 5))
    points.extend([(0, 80), (900, 0)])
    books = _commodity_points(1000, points)

    row = summary.build_trading_summary(
        _inputs([recipe], books={1000: books}, facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1))
    )["opportunities"][0]

    assert row["market_units_per_hour"] == 20
    assert row["usable_hours"] == 1
    assert row["usable_intervals"] == 4
    assert row["volume_status"] == "observed_activity"


def test_activity_rate_weights_depletion_by_usable_interval_hours():
    books = _commodity_points(1000, [(-2_100, 100), (-1_200, 90), (0, 60)])
    row = summary.build_trading_summary(
        _inputs([_recipe()], books={1000: books}, facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1))
    )["opportunities"][0]

    assert row["market_units_per_hour"] == 68.57
    assert row["usable_intervals"] == 2
    assert row["usable_hours"] == 2_100 / 3_600


def test_cold_commodity_observations_use_their_recorded_schedule_and_pool_transition():
    books = _commodity_points(1000, [(-5_400, 100), (0, 90)])
    plans = {
        1: summary.SweepSamplingPlan(900, 0, frozenset()),
        2: summary.SweepSamplingPlan(900, 6, frozenset({1000})),
    }
    row = summary.build_trading_summary(
        _inputs(
            [_recipe()],
            books={1000: books},
            sweep_plans=plans,
            facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
        )
    )["opportunities"][0]

    assert row["market_units_per_hour"] == 6.67
    assert row["usable_hours"] == 1.5
    assert row["usable_intervals"] == 1


def test_cold_item_books_qualify_but_hot_gaps_and_multi_hour_outages_do_not():
    recipe = _recipe()
    cold_plans = {
        1: summary.SweepSamplingPlan(900, 6, frozenset({1000})),
        2: summary.SweepSamplingPlan(900, 6, frozenset({1000})),
    }
    cold_books = [_item_book(0, ASOF - 5_400, quantity=10), _item_book(1, ASOF, quantity=1)]
    cold = summary.build_trading_summary(
        _inputs(
            [recipe],
            item_books={1000: cold_books},
            sweep_plans=cold_plans,
            facts=_facts(reagent_vendor_size=1, reagent_vendor_price=1),
        )
    )["opportunities"][0]
    assert cold["volume_status"] == "observed_activity"
    assert cold["usable_hours"] == 1.5
    assert cold["market_units_per_hour"] == 6

    hot_books = _commodity_points(1000, [(-5_400, 100), (0, 90)])
    hot = summary._activity(
        _inputs([recipe], books={1000: hot_books}),
        1000,
        summary._latest_quote(_inputs([recipe], books={1000: hot_books}), 1000),
        99,
    )
    assert hot.status == "unknown"

    outage_books = _commodity_points(1000, [(-4 * 3600, 100), (0, 90)])
    outage_inputs = _inputs(
        [recipe],
        books={1000: outage_books},
        sweep_plans={
            1: summary.SweepSamplingPlan(900, 6, frozenset({1000})),
            2: summary.SweepSamplingPlan(900, 6, frozenset({1000})),
        },
    )
    outage_quote = summary._latest_quote(outage_inputs, 1000)
    assert outage_quote is not None
    assert summary._activity(outage_inputs, 1000, outage_quote, 99).status == "unknown"


def test_commodity_ceiling_and_item_expiry_repost_rules_remain_conservative():
    bounded = _commodity_points(1000, [(-900, 100), (0, 90)], ceiling=Fraction(90))
    bounded_inputs = _inputs([_recipe()], books={1000: bounded})
    bounded_quote = summary._latest_quote(bounded_inputs, 1000)
    assert bounded_quote is not None
    assert summary._activity(bounded_inputs, 1000, bounded_quote, 99).status == "unknown"

    expiry_books = [_item_book(0, ASOF - 900, quantity=2), _item_book(1, ASOF, quantity=1)]
    expiry_books[0].record["observation"]["rows"][0]["timeLeft"] = {
        "kind": "band",
        "minimumSeconds": 100,
        "maximumSeconds": 101,
    }
    expiry_inputs = _inputs([_recipe()], item_books={1000: expiry_books})
    expiry_quote = summary._latest_quote(expiry_inputs, 1000)
    assert expiry_quote is not None
    assert summary._activity(expiry_inputs, 1000, expiry_quote, 99).status == "observed_no_depletion"

    repost_books = [_item_book(0, ASOF - 900, quantity=2), _item_book(1, ASOF, quantity=1)]
    repost_books[1].auctions += (Auction(99, Fraction(101), 1, "1000", "seller", False),)
    repost_books[1].record["observation"]["rows"].append(_wire_item_row(99, 101)[1])
    repost_inputs = _inputs([_recipe()], item_books={1000: repost_books})
    repost_quote = summary._latest_quote(repost_inputs, 1000)
    assert repost_quote is not None
    assert summary._activity(repost_inputs, 1000, repost_quote, 99).status == "observed_no_depletion"


def test_missing_historical_plan_fails_loudly_for_candidate_interval():
    books = _commodity_points(1000, [(-900, 100), (0, 90)])
    inputs = _inputs([_recipe()], books={1000: books}, sweep_plans={})
    quote = summary._latest_quote(inputs, 1000)
    assert quote is not None

    with pytest.raises(ValueError, match="historical sampling plan is unavailable"):
        summary._activity(inputs, 1000, quote, 99)


def test_sampling_plan_loader_reads_immutable_cold_metadata_and_rejects_missing_rows():
    plan_body = {
        "schemaVersion": 2,
        "cadenceSeconds": 900,
        "coldSlices": 6,
        "pools": {"cold": [1000, 1001, 1002, 1003, 1004, 1005]},
    }

    class FakeConnection:
        def __init__(self, rows):
            self.rows = rows
            self.parameters = None

        def execute(self, query, parameters):
            assert "market_plans" in query and "ms.plan_hash" in query
            self.parameters = parameters
            return self

        def fetchall(self):
            return self.rows

    conn = FakeConnection([(77, "a" * 64, plan_body)])
    plans = summary._load_sweep_sampling_plans(conn, HOUSE, BUILD, {77})

    assert conn.parameters == (HOUSE, BUILD, [77])
    assert plans == {77: summary.SweepSamplingPlan(900, 6, frozenset({1000, 1001, 1002, 1003, 1004, 1005}))}

    missing = FakeConnection([])
    with pytest.raises(ValueError, match="metadata is missing for sweeps"):
        summary._load_sweep_sampling_plans(missing, HOUSE, BUILD, {77})


def test_missing_output_or_reagent_prices_are_excluded_and_empty_summary_is_valid():
    missing_reagent = summary.build_trading_summary(
        _inputs([_recipe()], books={1000: _commodity_history(1000)}, facts=_facts())
    )
    missing_output = summary.build_trading_summary(
        _inputs(
            [_recipe()],
            books={2000: _commodity_history(2000)},
            facts=_facts(),
        )
    )
    empty = summary.build_trading_summary(_inputs([], facts={}))

    assert missing_reagent["opportunities"] == []
    assert missing_reagent["candidates"] == []
    assert missing_output["opportunities"] == []
    assert missing_output["candidates"] == []
    assert empty["opportunities"] == []
    assert empty["candidates"] == []
    assert empty["profile"] == {
        "alchemy": 225,
        "tailoring": 150,
        "alchemy_planning_cap": 300,
        "tailoring_planning_cap": 300,
        "budget_copper": 100_000,
        "recipes_assumed_owned": True,
    }


def test_summary_loader_uses_fact_recipe_loader_return_order(monkeypatch):
    recipe = _recipe(reagent_quantity=2)
    facts = _facts(reagent_vendor_size=3, reagent_vendor_price=20)
    books = {1000: _commodity_history(1000)}
    commodity_loads = []

    def load_books(*args, **kwargs):
        commodity_loads.append(kwargs)
        return books

    monkeypatch.setattr(summary, "load_client_data", lambda conn, build: (facts, (recipe,)))
    monkeypatch.setattr(summary, "load_commodity_books", load_books)
    monkeypatch.setattr(summary, "load_item_books", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        summary,
        "_load_sweep_sampling_plans",
        lambda conn, house, build, sweep_ids: {
            sweep_id: summary.SweepSamplingPlan(900, 0, frozenset()) for sweep_id in sweep_ids
        },
    )
    monkeypatch.setattr(summary, "item_names", lambda *args, **kwargs: {1000: "Output"})

    result = summary._load_summary_in_snapshot(
        object(),
        (HOUSE, BUILD, 583, ASOF, {"planned": 484, "queried": 484, "successful": 483}),
    )

    assert result["opportunities"][0]["reagents"][0]["cost"] == "80"
    assert result["scan"] == {"planned": 484, "queried": 484, "successful": 483}
    assert commodity_loads[0]["start_t"] == ASOF - 24 * 3600


def test_client_fact_loader_filters_status_and_rejects_missing_required_facts():
    class Cursor:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self, rows):
            self.rows = rows
            self.queries = []

        def execute(self, query, _params):
            self.queries.append(query)
            if "latest_client_export" in query:
                return Cursor(one=(1, "build"))
            return Cursor(rows=self.rows)

    good = Connection([(1000, 100, None, None, 20, 0)])
    loaded = client_data._load_facts(good, BUILD)
    assert loaded[1000].sell_price == 100
    assert "status = 'ok'" in good.queries[-1]

    missing = Connection([(1000, None, None, None, 20, 0)])
    with pytest.raises(ValueError, match="status ok but missing facts"):
        client_data._load_facts(missing, BUILD)


def test_recipe_loader_collapses_identical_skill_rows_and_rejects_conflicts():
    class Cursor:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.one

    class Connection:
        def __init__(self, skill_rows):
            self.skill_rows = skill_rows

        def execute(self, query, _params):
            if "latest_client_export" in query:
                return Cursor(one=(7, "1.0.1"))
            if "FROM client_recipes" in query:
                return Cursor(rows=[(1, 0, 0, None, "no_teaching_item")])
            if "client_recipe_skill_lines" in query:
                return Cursor(rows=self.skill_rows)
            if "client_recipe_reagents" in query:
                return Cursor(rows=[(1, 10, 1)])
            if "client_recipe_outputs" in query:
                return Cursor(rows=[(1, 11, 1, "fixed")])
            return Cursor()

    row = (1, 202, "engineering", 1, 105, 130, 155, 0)
    loaded = client_data._load_recipes(Connection([row, row]), "1.0.1")
    assert loaded[0].acquisition.skill_lines == (SkillRequirement(202, "engineering", 1, 105, 130, 155, 0),)

    conflicting = (1, 202, "engineering", 5, 105, 130, 155, 0)
    with pytest.raises(ValueError, match="conflicting requirements in skill line 202"):
        client_data._load_recipes(Connection([row, conflicting]), "1.0.1")


def test_recipe_loader_preserves_tool_focus_and_teaching_requirements():
    class Cursor:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.one

    class Connection:
        def execute(self, query, _params):
            if "latest_client_export" in query:
                return Cursor(one=(7, "1.0.1"))
            if "FROM client_recipes" in query:
                return Cursor(rows=[(1, 0, 0, None, "teaching_item_with_item_data")])
            if "client_recipe_skill_lines" in query:
                return Cursor(rows=[(1, 202, "engineering", 100, 110, 120, 130, 1)])
            if "client_recipe_teachers" in query:
                return Cursor(rows=[(1, 55, 202, 300), (1, 77, 202, 225)])
            if "client_recipe_reagents" in query:
                return Cursor(rows=[(1, 10, 1)])
            if "client_recipe_outputs" in query:
                return Cursor(rows=[(1, 11, 1, "fixed")])
            if "client_recipe_totems" in query:
                return Cursor(rows=[(1, 0, 3938)])
            if "client_recipe_totem_categories" in query:
                return Cursor(rows=[(1, 0, 162)])
            if "client_recipe_spell_focus" in query:
                return Cursor(rows=[(1, 1)])
            raise AssertionError(query)

    loaded = client_data._load_recipes(Connection(), "1.0.1")
    recipe = loaded[0]
    assert recipe.totems == (3938,)
    assert recipe.totem_categories == (162,)
    assert recipe.spell_focus == 1
    assert recipe.acquisition == RecipeAcquisition(
        (55, 77),
        (SkillRequirement(202, "engineering", 100, 110, 120, 130, 1),),
        (TeachingRequirement(55, 202, 300), TeachingRequirement(77, 202, 225)),
    )


def test_vendor_bundle_catalog_still_covers_retained_vendor_costs():
    assert client_data.vendor_supply_ids() == {
        814,
        2320,
        2321,
        2324,
        2325,
        2604,
        2605,
        2678,
        2692,
        2880,
        3371,
        3372,
        4289,
        4291,
        4340,
        4341,
        4470,
        6260,
        6261,
        8343,
        8925,
        10290,
        14341,
    }
