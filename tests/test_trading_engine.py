from __future__ import annotations

from fractions import Fraction

import pytest

from crowley.analysis.books import Book, Group
from crowley.analysis.metrics import compute_metrics
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.trading.__main__ import _select_sweeps
from crowley.trading.accounts import Account, Mail
from crowley.trading.api import (
    Buy,
    Cancel,
    Craft,
    ItemFacts,
    Post,
    Recipe,
    VendorBuy,
    VendorSell,
)
from crowley.trading.data import load_facts
from crowley.trading.engine import _mark_equity_details, run_backtest
from crowley.trading.execution import (
    CRAFT_SECONDS,
    TIME_BUDGET,
    ExecutionContext,
    _consume_lots,
    deposit,
    execute_intent,
    obtainable_units,
)
from crowley.trading.market import Market
from crowley.trading.recipes import validate_recipes
from crowley.trading.report import build_report, text_summary
from crowley.trading.statistics import (
    DemandStatistics,
    FairStatistics,
    RiskStatistics,
    StatisticsPoint,
    precompute_statistics,
)
from crowley.trading.steps import sweep_step_times
from crowley.trading.strategies import crafting
from crowley.trading.strategies.vendor_arb import VendorArbStrategy

ITEM = 7
RULES = rules_for_house(House("us", "normal", "alliance"))


def statistics_point(fair: int, demand: int = 10, eligible_seconds: float = 0.0) -> StatisticsPoint:
    return StatisticsPoint(
        fair=FairStatistics(Fraction(fair), Fraction(fair - 10), Fraction(fair + 10), None, 20),
        demand=DemandStatistics(
            0.0,
            Fraction(0),
            0,
            Fraction(0),
            Fraction(demand),
            None,
            eligible_seconds,
        ),
        risk=RiskStatistics(None, 0.0, 0, None, None, 0),
        books=1,
    )


def book(
    t: int,
    groups: list[tuple[str, int, int]],
    *,
    item_id: int = ITEM,
    sweep_id: int | str | None = None,
    ceiling: int | None = None,
    time_left: int = 100_000,
) -> Book:
    return Book(
        t=t,
        groups=[
            Group(result_id, Fraction(price), quantity, time_left) for result_id, price, quantity in groups
        ],
        ceiling=None if ceiling is None else Fraction(ceiling),
        item_id=item_id,
        sweep_id=sweep_id,
    )


def facts(
    *,
    sell: int = 0,
    bundle_size: int | None = None,
    bundle_price: int | None = None,
    stack: int = 20,
    item_id: int = ITEM,
) -> dict[int, ItemFacts]:
    return {item_id: ItemFacts(item_id, sell, bundle_size, bundle_price, stack, 0)}


class Script:
    params = {}

    def __init__(
        self,
        name: str,
        actions: dict[int, list[object]] | None = None,
        professions: frozenset[str] = frozenset(),
    ):
        self.name = name
        self.professions = professions
        self.actions = actions or {}
        self.views: list[tuple[int, int, dict[int, object]]] = []

    def decide(self, market, account):
        self.views.append((market.t, market.replay_ordinal, dict(market.items())))
        return self.actions.get(market.replay_ordinal, ())

    def checkpoint(self):
        return {}

    def restore(self, state):
        if state:
            raise ValueError("script state must be empty")


def run(
    books,
    strategies,
    facts_value=None,
    recipes=(),
    capital=10_000,
    statistics=None,
    **kwargs,
):
    kwargs.setdefault("wind_down_hours", 0)
    return run_backtest(
        books,
        {} if statistics is None else statistics,
        facts_value or facts(),
        recipes,
        strategies,
        None,
        capital,
        rules=RULES,
        **kwargs,
    )


def two_sweeps(groups, *, item_id=ITEM, t0=0, t1=900, ceiling=None):
    return {
        item_id: [
            book(t0, groups, item_id=item_id, sweep_id=1, ceiling=ceiling),
            book(t1, groups, item_id=item_id, sweep_id=2, ceiling=ceiling),
        ]
    }


def test_obtainability_survives_next_sweep_and_respects_purchase_net_and_ceiling():
    books = {
        ITEM: [
            book(0, [("cheap", 10, 5), ("high", 20, 3)], sweep_id=1, ceiling=15),
            book(900, [("cheap", 10, 2), ("high", 20, 3)], sweep_id=2, ceiling=15),
        ]
    }
    market = Market(books, {}, facts(), (), rules=RULES)
    purchased = {(ITEM, "cheap"): 1}
    market.set_step(0, 0, purchased)
    context = ExecutionContext(market, [], 0, 0, purchased, rules=RULES)
    assert [(group.result_id, quantity) for group, quantity in obtainable_units(context, ITEM)] == [
        ("cheap", 1)
    ]

    gap_books = {
        ITEM: [
            book(0, [("cheap", 10, 5)], sweep_id=1),
            book(2_000, [("cheap", 10, 5)], sweep_id=2),
        ]
    }
    gap_market = Market(gap_books, {}, facts(), (), rules=RULES)
    gap_market.set_step(0, 0, {})
    assert obtainable_units(ExecutionContext(gap_market, [], 0, 0, {}, rules=RULES), ITEM) == []


def test_buy_is_cash_limited_and_levels_are_purchase_net():
    books = two_sweeps([("a", 30, 5)])
    buyer = Script("buyer", {0: [Buy(ITEM, 30, 5), Buy(ITEM, 30, 5)]})
    result = run(books, [buyer], capital=100)
    account = result.accounts["buyer"]
    assert account.counts["units_bought"] == 3
    assert account.cash_balance == 10
    assert account.rejections["cash"] == 1

    second = Script("second", {0: [Buy(ITEM, 30, 5)]})
    result = run(books, [Script("first", {0: [Buy(ITEM, 30, 2)]}), second], capital=200)
    assert second.views[0][2][ITEM].levels[0].quantity == 3


def test_fill_allocation_uses_demand_above_price_and_tie_order():
    books = {
        ITEM: [
            book(0, [("a", 50, 3), ("b", 80, 2)], sweep_id=1),
            book(900, [("a", 50, 3), ("b", 80, 2)], sweep_id=2),
            book(1_800, [], sweep_id=3),
            book(2_700, [], sweep_id=4),
        ]
    }
    # Both listings are posted at step 0; step 1 is intentionally quiet.  The
    # step-2 interval starts at sweep 1 and is therefore the first eligible one.
    first = Script("first", {0: [VendorBuy(ITEM, 4), Post(ITEM, 40, 4, 12)]})
    second = Script("second", {0: [VendorBuy(ITEM, 3), Post(ITEM, 60, 3, 12)]})
    result = run(
        books,
        [first, second],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=2_000,
    )
    assert result.accounts["first"].counts["units_sold"] == 4
    assert result.accounts["second"].counts.get("units_sold", 0) == 0

    tie_books = {
        ITEM: [
            book(0, [("a", 80, 1)], sweep_id=1),
            book(900, [("a", 80, 1)], sweep_id=2),
            book(1_800, [], sweep_id=3),
        ]
    }
    a = Script("a", {0: [VendorBuy(ITEM, 1), Post(ITEM, 50, 1, 12)]})
    b = Script("b", {0: [VendorBuy(ITEM, 1), Post(ITEM, 50, 1, 12)]})
    result = run(
        tie_books,
        [a, b],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=2_000,
    )
    assert result.accounts["a"].counts["units_sold"] == 1
    assert result.accounts["b"].counts.get("units_sold", 0) == 0


def test_invalid_intervals_and_post_eligibility_are_conservative():
    invalid = {
        ITEM: [
            book(0, [("a", 50, 1)], sweep_id=1),
            book(2_000, [], sweep_id=2),
        ]
    }
    seller = Script("seller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 40, 1, 12)]})
    result = run(
        invalid,
        [seller],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=1_000,
    )
    assert result.accounts["seller"].counts.get("units_sold", 0) == 0

    # The first interval starts in the posting sweep and cannot fill a listing
    # posted there; the second interval starts later and can.
    eligible = {
        ITEM: [
            book(0, [("a", 50, 1)], sweep_id=1),
            book(900, [("a", 50, 1)], sweep_id=2),
            book(1_800, [], sweep_id=3),
        ]
    }
    seller = Script("seller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 40, 1, 12)]})
    result = run(
        eligible,
        [seller],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=1_000,
    )
    assert result.accounts["seller"].counts["units_sold"] == 1


def test_neutral_house_sale_uses_the_neutral_seller_cut():
    books = {
        ITEM: [
            book(0, [("a", 80, 1)], sweep_id=1),
            book(900, [("a", 80, 1)], sweep_id=2),
            book(1_800, [], sweep_id=3),
        ]
    }
    seller = Script("neutral_seller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 40, 1, 12)]})
    neutral_rules = rules_for_house(House("us", "normal", "neutral"))

    result = run_backtest(
        books,
        {},
        facts(sell=0, bundle_size=1, bundle_price=1, stack=1),
        (),
        [seller],
        None,
        1_000,
        wind_down_hours=0,
        rules=neutral_rules,
    )

    account = result.accounts["neutral_seller"]
    assert account.closed_trades[0]["proceeds"] == 34
    assert account.fees["ah_cut"] == 6


def test_faction_house_sale_retains_the_existing_five_percent_cut():
    books = {
        ITEM: [
            book(0, [("a", 80, 1)], sweep_id=1),
            book(900, [("a", 80, 1)], sweep_id=2),
            book(1_800, [], sweep_id=3),
        ]
    }
    seller = Script("faction_seller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 40, 1, 12)]})

    result = run_backtest(
        books,
        {},
        facts(sell=0, bundle_size=1, bundle_price=1, stack=1),
        (),
        [seller],
        None,
        1_000,
        wind_down_hours=0,
        rules=RULES,
    )

    account = result.accounts["faction_seller"]
    assert account.closed_trades[0]["proceeds"] == 38
    assert account.fees["ah_cut"] == 2


def test_mail_delay_expiry_and_cancel_deposit_accounting():
    assert deposit(100, 3, 12, RULES) == 45
    books = {
        ITEM: [
            book(0, [("a", 10, 1)], sweep_id=1),
            book(43_200, [("a", 10, 1)], sweep_id=2),
            book(46_800, [("a", 10, 1)], sweep_id=3),
        ]
    }
    strategy = Script("seller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 20, 1, 12)]})
    result = run(
        books,
        [strategy],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=1_000,
    )
    account = result.accounts["seller"]
    assert account.counts["expiries"] == 1
    assert account.inventory_balance[ITEM] == 1
    assert account.fees["deposits_lost"] == 15

    cancel_strategy = Script("canceller", {0: [VendorBuy(ITEM, 1), Post(ITEM, 20, 1, 12)]})
    cancel_result = run(
        two_sweeps([("a", 10, 1)]),
        [cancel_strategy],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=1_000,
    )
    cancel_account = cancel_result.accounts["canceller"]
    listing_id = next(iter(cancel_account.listings_by_id))
    market = Market(two_sweeps([("a", 10, 1)]), {}, facts(sell=100), (), rules=RULES)
    market.set_step(0, 0, {})
    context = ExecutionContext(
        market,
        [cancel_account],
        0,
        0,
        {},
        next_listing_id=listing_id + 1,
        rules=RULES,
    )
    assert execute_intent(context, cancel_account, Cancel(listing_id))
    assert cancel_account.inventory_balance[ITEM] == 1
    assert cancel_account.fees["deposits_lost"] == 15


def test_multi_interval_deposit_refunds_use_cumulative_sold_quantity():
    books = {
        ITEM: [
            book(0, [("a", 50, 3)], sweep_id=1),
            book(900, [("a", 50, 3)], sweep_id=2),
            book(1_800, [("a", 50, 2)], sweep_id=3),
            book(2_700, [("a", 50, 1)], sweep_id=4),
            book(3_600, [], sweep_id=5),
        ]
    }
    strategy = Script("seller", {0: [VendorBuy(ITEM, 3), Post(ITEM, 40, 3, 12)]})
    result = run(
        books,
        [strategy],
        facts_value=facts(sell=100, bundle_size=3, bundle_price=3, stack=3),
        capital=1_000,
    )
    account = result.accounts["seller"]
    assert account.counts["units_sold"] == 3
    assert account.fees["deposits_refunded"] == 45
    assert account.fees.get("deposits_lost", 0) == 0
    assert account.pending_cash() == 3 * (40 * 95 // 100) + 45


def test_vendor_bundle_rounds_up_and_vendor_arb_sells_one_step_later():
    books = two_sweeps([("a", 50, 3)])
    fact = facts(sell=100, bundle_size=3, bundle_price=7, stack=20)
    buyer = Script("buyer", {0: [VendorBuy(ITEM, 1)]})
    result = run(books, [buyer], facts_value=fact, capital=100)
    account = result.accounts["buyer"]
    assert account.inventory_balance[ITEM] == 3
    assert account.cash_balance == 93

    result = run_backtest(
        books,
        {},
        facts(sell=100),
        (),
        [VendorArbStrategy()],
        None,
        1_000,
        wind_down_hours=0,
        rules=RULES,
    )
    account = result.accounts["vendor_arb"]
    assert account.cash_balance == 1_150
    assert account.counts["units_bought"] == 3
    assert account.counts["units_vendored"] == 3


def test_craft_profession_reagent_and_shared_cooldown_expiry():
    reagent, output = 8, 9
    recipes = (Recipe(22, frozenset({"alchemy"}), ((reagent, 2),), ((output, 1),), "daily", 3, "x"),)
    a = Script("a", {0: [VendorBuy(reagent, 4), Craft(22, 1)], 1: [Craft(22, 1)]}, frozenset({"alchemy"}))
    b = Script("b", {0: [VendorBuy(reagent, 2), Craft(22, 1)]}, frozenset({"alchemy"}))
    recipe_facts = {
        ITEM: ItemFacts(ITEM, 0, None, None, 5, 0),
        reagent: ItemFacts(reagent, 0, 2, 1, 2, 0),
        output: ItemFacts(output, 0, None, None, 5, 0),
    }
    books = {
        ITEM: [
            book(0, [("a", 10, 1)], sweep_id=1),
            book(900, [("a", 10, 1)], sweep_id=2),
            book(3_600, [("a", 10, 1)], sweep_id=3),
        ]
    }
    result = run(books, [a, b], facts_value=recipe_facts, recipes=recipes, capital=100)
    assert result.accounts["a"].counts["crafted"] == 2
    assert result.accounts["b"].rejections["cooldown"] == 1

    bad = Recipe(23, frozenset(), ((reagent, -1), (reagent, 1)), ((output, 0),), None, 0, "x")
    with pytest.raises(ValueError):
        validate_recipes((bad,))
    normalized = validate_recipes(
        (Recipe(24, frozenset(), ((reagent, 1), (reagent, 2)), ((output, 1), (output, 1)), None, 0, "x"),)
    )[0]
    assert normalized.reagents == ((reagent, 3),)
    assert normalized.outputs == ((output, 2),)


def test_click_budget_rotation_and_one_step_per_recorded_sweep():
    books = {
        ITEM: [book(100, [("a", 10, 1)], sweep_id=1)],
        8: [book(200, [("a", 10, 1)], item_id=8, sweep_id=1)],
    }
    first = Script("first", {0: [VendorBuy(ITEM, 1)] * (TIME_BUDGET // 2 + 1)})
    second = Script("second")
    result = run(
        books,
        [first, second],
        facts_value={
            ITEM: ItemFacts(ITEM, 0, 1, 1, 20, 0),
            8: ItemFacts(8, 0, None, None, 20, 0),
        },
        capital=1_000,
    )
    assert result.times == (200,)
    assert len(first.views) == len(second.views) == 1
    assert first.views[0][2].keys() == {ITEM, 8}
    assert first.views[0][0] == 200
    assert first.views[0][1] == 0
    assert first.views[0][2][ITEM].t == 100
    assert first.views[0][2][8].t == 200

    invocation_order = []

    class Ordering(Script):
        def __init__(self, name):
            super().__init__(name)

        def decide(self, market, account):
            invocation_order.append((market.replay_ordinal, self.name))
            self.views.append((market.t, market.replay_ordinal, dict(market.items())))
            return ()

    a, b = Ordering("a"), Ordering("b")
    books = {
        ITEM: [
            book(0, [("a", 1, 1)], sweep_id=1),
            book(900, [("a", 1, 1)], sweep_id=2),
            book(1_800, [("a", 1, 1)], sweep_id=3),
        ]
    }
    run(books, [a, b], facts_value=facts(), capital=100)
    assert invocation_order == [(0, "a"), (0, "b"), (1, "b"), (1, "a"), (2, "a"), (2, "b")]

    budget_first = Script("budget_first", {0: [VendorBuy(ITEM, 1)] * (TIME_BUDGET // 2)})
    budget_second = Script("budget_second", {0: [VendorBuy(ITEM, 1)]})
    budget_result = run(
        {
            ITEM: [
                book(0, [("a", 1, 1)], sweep_id=1),
                book(900, [("a", 1, 1)], sweep_id=2),
            ]
        },
        [budget_first, budget_second],
        facts_value=facts(bundle_size=1, bundle_price=1),
        capital=1_000,
    )
    assert budget_result.accounts["budget_first"].clicks == TIME_BUDGET
    assert budget_result.accounts["budget_second"].rejections["time_budget"] == 1


def test_marks_equity_attribution_and_no_lookahead():
    books = {ITEM: [book(index * 900, [("a", 10, 20)], sweep_id=index) for index in range(10)]}
    observer = Script("observer")
    result = run(books, [observer], facts_value=facts(sell=0), capital=100)
    assert observer.views[0][2][ITEM].fair_n == 0
    assert observer.views[8][2][ITEM].fair_n == 8
    assert observer.views[8][2][ITEM].fair == 10

    buyer = Script("buyer", {0: [Buy(ITEM, 10, 1)]})
    idle = Script("idle")
    result = run(two_sweeps([("a", 10, 1)]), [buyer, idle], facts_value=facts(), capital=200)
    assert result.accounts["buyer"].cash_balance == 90
    assert result.accounts["idle"].cash_balance == 100
    # Demand is not observable at the first step, so the purchased unit is
    # marked at its zero vendor value rather than the old unconditional book mark.
    assert result.total_equity_curve[0][1] == Fraction(190)


def test_partial_sweep_marks_latest_observation_and_reports_stale_items():
    stale_item, marker_item, unseen_item = 8, 9, 10
    books = {
        stale_item: [
            book(0, [("old", 20, 8)], item_id=stale_item, sweep_id=1),
            book(
                900,
                [("tracked", 40, 2), ("high", 80, 10)],
                item_id=stale_item,
                sweep_id=2,
            ),
            book(1_800, [("tracked", 150, 2)], item_id=stale_item, sweep_id=4),
        ],
        marker_item: [book(1_350, [("marker", 1, 1)], item_id=marker_item, sweep_id=3)],
    }

    def point(fair):
        return statistics_point(fair)

    statistics = {
        stale_item: [point(100), point(100), point(500)],
        marker_item: [point(1)],
    }
    item_facts = {
        stale_item: ItemFacts(stale_item, 10, 1, 1, 20, 0),
        marker_item: ItemFacts(marker_item, 0, None, None, 20, 0),
        unseen_item: ItemFacts(unseen_item, 7, 1, 1, 20, 0),
    }
    strategy = Script(
        "partial",
        {0: [VendorBuy(unseen_item, 1)], 1: [Buy(stale_item, 40, 2)]},
    )
    result = run(
        books,
        [strategy],
        facts_value=item_facts,
        capital=1_000,
        statistics=statistics,
        end_t=1_350,
    )

    # The purchase consumes the latest book's cheap level, so its purchase-net
    # snapshot marks the held units at 80 copper. The final selected sweep omits
    # stale_item, and the later 150-copper snapshot must not be used.
    assert result.accounts["partial"].counts["units_bought"] == 2
    assert result.equity_curves["partial"][-1][1] == Fraction(1_078)
    report = build_report(result)["strategies"]["partial"]
    assert report["stale_marked_items"] == 1
    assert report["equity"] == Fraction(1_078)


def test_truncated_craft_posts_actual_output_quantity_on_the_next_step():
    reagent, output, burner_item = 11, 12, 13
    recipe = Recipe(
        31,
        frozenset({"alchemy"}),
        ((reagent, 1),),
        ((output, 1),),
        None,
        0,
        "test",
    )
    books = {
        output: [
            book(t, [("output", 10, 1)], item_id=output, sweep_id=index)
            for index, t in enumerate((0, 900, 1_800), start=1)
        ]
    }
    trusted = statistics_point(100, 188, 6 * 3_600)
    item_facts = {
        reagent: ItemFacts(reagent, 0, 1, 1, 20, 0),
        output: ItemFacts(output, 1, None, None, 20, 0),
        burner_item: ItemFacts(burner_item, 0, 1, 1, 20, 0),
    }
    burner = Script("burner", {1: [VendorBuy(burner_item, 1)] * 162})
    strategy = crafting.build({"professions": ["alchemy"]})
    result = run(
        books,
        [Script("idle"), burner, strategy],
        facts_value=item_facts,
        recipes=(recipe,),
        capital=3_000,
        statistics={output: [trusted, trusted, trusted]},
    )

    account = result.accounts["crafting"]
    step_one = result.step_log[1]["strategies"]["crafting"]
    assert account.counts["craft_truncated"] == 1
    assert account.counts["crafted"] == 41
    assert account.rejections == {}
    assert [entry["intent"]["type"] for entry in step_one] == ["Craft"]
    assert step_one[0]["outcome"] == "executed"

    step_two = result.step_log[2]["strategies"]["crafting"]
    posts = [entry for entry in step_two if entry["intent"]["type"] == "Post"]
    assert posts == [
        {
            "intent": {
                "type": "Post",
                "item_id": output,
                "unit_price": 99,
                "quantity": 41,
                "duration_hours": 24,
            },
            "outcome": "executed",
            "detail": {},
        }
    ]
    assert account.counts["posts"] == 1
    assert next(iter(account.listings_by_id.values())).quantity == 41


def test_fractional_prices_and_summary_statistics_fail_loudly():
    fractional = {ITEM: [book(0, [("a", 10, 1)], sweep_id=1), book(900, [("a", 10, 1)], sweep_id=2)]}
    fractional[ITEM][0].groups[0] = Group("a", Fraction(21, 2), 1, 100_000)
    with pytest.raises(ValueError, match="not integral"):
        Market(fractional, {}, facts(), (), rules=RULES)

    summary = compute_metrics(two_sweeps([("a", 10, 1)]))
    with pytest.raises(ValueError, match="cover every book"):
        run_backtest(
            two_sweeps([("a", 10, 1)]),
            summary,
            facts(),
            (),
            [Script("x")],
            None,
            100,
            rules=RULES,
        )


def test_sweep_selection_uses_step_times_and_keeps_exclusion_counts_consistent():
    rows = [
        (1, 1_000, "1.0", "a"),
        (2, 2_000, "1.0", "a"),
        (3, 3_000, "1.0", "b"),
        (4, 1_500, "1.0", "a"),
    ]

    class Connection:
        def execute(self, _query, _params):
            return self

        def fetchall(self):
            return rows

    books = {
        ITEM: [
            book(3_000, [("a", 10, 1)], sweep_id=1),
            book(1_000, [("a", 10, 1)], sweep_id=2),
            book(2_000, [("a", 10, 1)], sweep_id=3),
        ]
    }
    all_sweeps, window, selected, client_build, without_books = _select_sweeps(
        Connection(), "house", 1_500, 2_500, None, books
    )
    assert [row[0] for row in all_sweeps] == [1, 2, 3, 4]
    assert [row[0] for row in window] == [3]
    assert [row[0] for row in selected] == [3]
    assert client_build == "1.0.b"
    assert without_books == 1
    assert len(all_sweeps) - without_books - len(window) == 2
    assert len(window) - len(selected) == 0

    _, window, selected, client_build, without_books = _select_sweeps(
        Connection(), "house", None, None, "1.0.a", books
    )
    assert [row[0] for row in window] == [1, 2, 3]
    assert [row[0] for row in selected] == [1, 2]
    assert client_build == "1.0.a"
    assert without_books == 1
    assert (4 - without_books) - len(window) == 0
    assert len(window) - len(selected) == 1


def test_non_integral_sweep_time_is_refused():
    with pytest.raises(ValueError, match="whole seconds"):
        sweep_step_times({ITEM: [book(0.5, [("a", 10, 1)], sweep_id=1)]})


def test_fact_loading_filters_status_and_rejects_null_required_facts():
    class Connection:
        def __init__(self, rows):
            self.rows = rows
            self.queries = []

        def execute(self, query, _params):
            self.queries.append(query)
            return self

        def fetchone(self):
            return (1, "build")

        def fetchall(self):
            return self.rows

    good_connection = Connection([(ITEM, 100, None, None, 20, 0)])
    loaded = load_facts(good_connection, "house", "build")
    assert loaded[ITEM].sell_price == 100
    assert "status = 'ok'" in good_connection.queries[-1]

    null_connection = Connection([(ITEM, None, None, None, 20, 0)])
    with pytest.raises(ValueError, match="status ok but missing facts"):
        load_facts(null_connection, "house", "build")


def test_text_summary_uses_fixed_precision_and_lists_each_strategy():
    report = {
        "total": {
            "start_capital": 12_345,
            "equity": 23_456,
            "realized": 3_456,
            "return_pct": Fraction(123, 10),
            "clicks": 7,
            "max_held_over_capacity": Fraction(3, 2),
        },
        "strategies": {
            "alpha": {
                "start_capital": 10_000,
                "equity": 10_001,
                "realized": 123,
                "return_pct": Fraction(1, 10),
                "clicks": 1,
                "max_held_over_capacity": Fraction(1, 2),
            },
            "beta": {
                "start_capital": 20_000,
                "equity": 20_100,
                "realized": 99,
                "return_pct": Fraction(1, 2),
                "clicks": 2,
                "max_held_over_capacity": Fraction(2, 1),
            },
        },
    }
    assert text_summary(report).splitlines() == [
        "total: start=1.23g, equity=2.35g, realized=0.35g, return=12.3%, clicks=7, "
        "max_held_over_capacity=1.50",
        "alpha: start=1.00g, equity=1.00g, realized=0.01g, return=0.1%, clicks=1, "
        "max_held_over_capacity=0.50",
        "beta: start=2.00g, equity=2.01g, realized=0.01g, return=0.5%, clicks=2, max_held_over_capacity=2.00",
    ]


def test_account_view_counts_mail_as_in_transit_until_the_arrival_boundary():
    account = Account("x", frozenset(), 0, 0)
    account.mail.append(Mail(100, items={ITEM: 2}))
    assert account.account_view(99).in_transit() == {ITEM: 2}
    account.release_mail(99)
    assert account.account_view(99).inventory() == {}
    account.release_mail(100)
    assert account.account_view(100).in_transit() == {}
    assert account.account_view(100).inventory() == {ITEM: 2}


def test_liquidity_aware_mark_uses_purchase_net_and_vendor_for_excess():
    books = {ITEM: [book(0, [("cheap", 30, 15), ("a", 60, 20)], sweep_id=1)]}
    statistics = {ITEM: [statistics_point(100)]}
    market = Market(books, statistics, facts(sell=10), (), rules=RULES)
    market.set_step(0, 0, {(ITEM, "cheap"): 15})
    account = Account("x", frozenset(), 0, 0)
    account.add_inventory(ITEM, 8)
    # The strategy-facing book has only the 60-copper level left: five units are
    # liquid at 60 * 95%, and the remaining three use the vendor value.
    assert _mark_equity_details(account, market, 0)[0] == Fraction(315)


def test_craft_time_truncates_and_then_rejects_when_no_time_fits():
    reagent, output = 8, 9
    recipes = (Recipe(22, frozenset({"alchemy"}), ((reagent, 1),), ((output, 1),), None, 0, "x"),)
    books = two_sweeps([("a", 10, 1)])
    recipe_facts = {
        ITEM: ItemFacts(ITEM, 0, None, None, 20, 0),
        reagent: ItemFacts(reagent, 0, 1, 1, 20, 0),
        output: ItemFacts(output, 0, None, None, 20, 0),
    }
    market = Market(books, {}, recipe_facts, recipes, rules=RULES)
    market.set_step(0, 0, {})
    account = Account("x", frozenset({"alchemy"}), 10_000, 10_000)
    context = ExecutionContext(market, [account], 0, 0, {}, rules=RULES)
    assert execute_intent(context, account, VendorBuy(reagent, TIME_BUDGET))
    assert execute_intent(context, account, Craft(22, TIME_BUDGET))
    assert not execute_intent(context, account, Craft(22, 1))
    assert context.time_used == TIME_BUDGET - 2
    assert account.counts["crafted"] == (TIME_BUDGET - 4) // CRAFT_SECONDS
    assert account.counts["craft_truncated"] == 1
    assert account.rejections["time_budget"] == 1


def test_craft_rejects_wrong_profession_and_insufficient_reagent():
    reagent, output = 8, 9
    recipes = (Recipe(22, frozenset({"alchemy"}), ((reagent, 2),), ((output, 1),), None, 0, "x"),)
    recipe_facts = {
        ITEM: ItemFacts(ITEM, 0, None, None, 20, 0),
        reagent: ItemFacts(reagent, 0, None, None, 20, 0),
        output: ItemFacts(output, 0, None, None, 20, 0),
    }
    market = Market(two_sweeps([("a", 10, 1)]), {}, recipe_facts, recipes, rules=RULES)
    market.set_step(0, 0, {})
    wrong_profession = Account("wrong", frozenset(), 100, 100)
    wrong_profession.add_inventory(reagent, 2)
    missing_reagent = Account("missing", frozenset({"alchemy"}), 100, 100)
    context = ExecutionContext(
        market,
        [wrong_profession, missing_reagent],
        0,
        0,
        {},
        rules=RULES,
    )
    assert not execute_intent(context, wrong_profession, Craft(22, 1))
    assert wrong_profession.rejections["profession"] == 1
    assert not execute_intent(context, missing_reagent, Craft(22, 1))
    assert missing_reagent.rejections["reagent"] == 1


def test_wind_down_rejects_acquisitions_but_allows_vendor_sell_and_reports_checkpoints():
    books = {
        ITEM: [
            book(0, [("a", 10, 1)], sweep_id=1),
            book(900, [("a", 10, 1)], sweep_id=2),
            book(3_600, [("a", 10, 1)], sweep_id=3),
            book(4_500, [("a", 10, 1)], sweep_id=4),
        ]
    }
    strategy = Script(
        "wind_down",
        {
            0: [VendorBuy(ITEM, 1)],
            1: [VendorBuy(ITEM, 1), VendorSell(ITEM, 1)],
        },
    )
    result = run(
        books,
        [strategy],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=100,
        wind_down_hours=1,
    )
    account = result.accounts["wind_down"]
    report = build_report(result)["strategies"]["wind_down"]
    assert account.rejections["wind_down"] == 1
    assert account.counts["units_vendor_bought"] == 1
    assert account.counts["units_vendored"] == 1
    assert report["realized_at_wind_down_start"] == -1
    assert report["realized"] == 99
    assert report["equity_at_wind_down_start"] == 199
    assert report["equity"] == 199


def test_wind_down_rejection_does_not_consume_time_budget():
    books = two_sweeps([("a", 10, 1)])
    market = Market(books, {}, facts(bundle_size=1, bundle_price=1), (), rules=RULES)
    market.set_step(0, 0, {})
    account = Account("x", frozenset(), 100, 100)
    context = ExecutionContext(market, [account], 0, 0, {}, wind_down=True, rules=RULES)
    assert not execute_intent(context, account, VendorBuy(ITEM, 1))
    assert context.time_used == 0
    assert account.clicks == 0
    assert account.rejections == {"wind_down": 1}


def test_demand_share_caps_multiple_own_listings_exactly():
    books = {
        ITEM: [
            book(0, [("a", 10, 4)], sweep_id=1),
            book(900, [("a", 10, 4)], sweep_id=2),
            book(1_800, [], sweep_id=3),
        ]
    }
    strategy = Script(
        "shared_demand",
        {0: [VendorBuy(ITEM, 6), Post(ITEM, 5, 3, 12), Post(ITEM, 6, 3, 12)]},
    )
    result = run(
        books,
        [strategy],
        facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
        capital=1_000,
        demand_share=Fraction(1, 2),
    )
    account = result.accounts["shared_demand"]
    assert account.counts["units_sold"] == 2
    assert {listing.unit_price: listing.quantity for listing in account.listings_by_id.values()} == {
        5: 1,
        6: 3,
    }


def test_capacity_diagnostic_excludes_vendor_only_items_without_a_snapshot():
    reagent = 8
    books = two_sweeps([("a", 10, 1)])
    strategy = Script("diagnostic", {0: [VendorBuy(reagent, 1)]})
    result = run(
        books,
        [strategy],
        facts_value={
            ITEM: ItemFacts(ITEM, 0, None, None, 20, 0),
            reagent: ItemFacts(reagent, 0, 1, 1, 20, 0),
        },
        capital=100,
    )
    observations = result.capacity_observations["diagnostic"]
    assert observations
    assert {observation["item_id"] for observation in observations} == {ITEM}
    report = build_report(result)["strategies"]["diagnostic"]
    assert all(item["item_id"] != reagent for item in report["capacity_top_items"])


def test_capacity_diagnostic_excludes_vendor_supply_with_a_snapshot():
    vendor_item = 8
    books = {
        ITEM: [
            book(0, [("a", 10, 1)], sweep_id=1),
            book(900, [("a", 10, 1)], sweep_id=2),
        ],
        vendor_item: [
            book(0, [("a", 10, 1)], item_id=vendor_item, sweep_id=1),
            book(900, [("a", 10, 1)], item_id=vendor_item, sweep_id=2),
        ],
    }
    result = run(
        books,
        [Script("diagnostic_with_book", {0: [VendorBuy(vendor_item, 1)]})],
        facts_value={
            ITEM: ItemFacts(ITEM, 0, None, None, 20, 0),
            vendor_item: ItemFacts(vendor_item, 0, 1, 1, 20, 0),
        },
        capital=100,
    )
    assert {
        observation["item_id"] for observation in result.capacity_observations["diagnostic_with_book"]
    } == {ITEM}


def test_reached_ceiling_interval_does_not_allocate_own_fills():
    # Post at step 0; the listing is first eligible for the interval starting at step 1. Between
    # steps 1 and 2 the walk consumes the last in-scope group of a bounded book (reached ceiling),
    # so demand above our price is unobservable and must not fill us. The unbounded control proves
    # the same interval would otherwise fill the listing.
    def scenario(ceiling):
        books = {
            ITEM: [
                book(0, [("a", 10, 3)], sweep_id=1, ceiling=ceiling),
                book(900, [("a", 10, 3)], sweep_id=2, ceiling=ceiling),
                book(1800, [], sweep_id=3, ceiling=ceiling),
            ]
        }
        seller = Script("ceiling", {0: [VendorBuy(ITEM, 3), Post(ITEM, 5, 3, 12)]})
        result = run(
            books,
            [seller],
            facts_value=facts(sell=100, bundle_size=1, bundle_price=1, stack=1),
            capital=1_000,
        )
        return result.accounts["ceiling"]

    bounded = scenario(10)
    assert bounded.counts.get("units_sold", 0) == 0
    assert next(iter(bounded.listings_by_id.values())).quantity == 3
    unbounded = scenario(None)
    assert unbounded.counts.get("units_sold", 0) == 3


def test_fifo_profit_uses_exact_cost_and_net_proceeds():
    account = Account("x", frozenset(), 0, 0)
    account.add_inventory(ITEM, 2, unit_cost=Fraction(1, 1))
    account.add_inventory(ITEM, 1, unit_cost=Fraction(2, 1))
    _consume_lots(account, ITEM, 3, 5, 0, "auction")
    assert sum(trade["profit"] for trade in account.closed_trades) == 1
    assert sum(trade["proceeds"] for trade in account.closed_trades) == 5


def test_report_contains_required_fields_and_covers_full_size_statistics():
    result = run(two_sweeps([("a", 10, 1)]), [Script("observer")], facts_value=facts(), capital=100)
    strategy_report = build_report(result)["strategies"]["observer"]
    for key in (
        "start_capital",
        "final_equity",
        "realized",
        "unrealized",
        "fees",
        "counts",
        "clicks",
        "max_drawdown",
        "equity_curve",
        "gold_per_day",
        "top_closed_trades",
    ):
        assert key in strategy_report

    full = {
        item_id: [book(index * 900, [("a", 10, 20)], item_id=item_id, sweep_id=index) for index in range(240)]
        for item_id in range(330)
    }
    statistics = precompute_statistics(full)
    assert len(statistics) == 330
