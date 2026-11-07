from __future__ import annotations

from fractions import Fraction

import pytest

from crowley.trading.accounts import Account, Listing
from crowley.trading.api import Buy, Cancel, Craft, Level, ListingView, Post, Recipe
from crowley.trading.execution import TIME_BUDGET, ExecutionContext, execute_intent
from crowley.trading.market import Market
from crowley.trading.strategies import crafting, snipe_flip
from tests.test_trading_engine import facts as engine_facts
from tests.test_trading_engine import two_sweeps
from tests.test_trading_strategies import (
    RULES,
    FakeAccount,
    FakeMarket,
    snapshot,
)
from tests.test_trading_strategies import (
    facts as strategy_facts,
)

OUTPUT = 2
REAGENT = 9


def output_recipe() -> Recipe:
    return Recipe(1, frozenset({"alchemy"}), ((REAGENT, 1),), ((OUTPUT, 1),), None, 0, "test")


def output_market(*, demand: Fraction = Fraction(100)) -> FakeMarket:
    return FakeMarket(
        {
            OUTPUT: snapshot(OUTPUT, demand=demand, levels=(Level(50, 100),)),
            REAGENT: snapshot(REAGENT, levels=(Level(10, 100),)),
        },
        {OUTPUT: strategy_facts(OUTPUT, sell_price=1), REAGENT: strategy_facts(REAGENT)},
        (output_recipe(),),
        rules=RULES,
    )


class DurationMarket(FakeMarket):
    def deposit(self, item_id: int, quantity: int, hours: int) -> int:
        self.deposit_calls.append((item_id, quantity, hours))
        return self.deposits.get(item_id, 0) * quantity * hours // 12


def strategy_for(**params):
    defaults = {"professions": ["alchemy"], "max_spend_share": 0}
    defaults.update(params)
    return crafting.build(defaults)


def test_horizon_caps_posts_and_accounts_for_existing_listings_across_sweeps():
    market = output_market(demand=Fraction(20))
    strategy = strategy_for(post_horizon_hours=24)

    first = strategy.decide(market, FakeAccount(inventory_map={OUTPUT: 10}))
    assert first == [Post(OUTPUT, 99, 5, 24)]

    second = strategy.decide(
        market,
        FakeAccount(
            inventory_map={OUTPUT: 5},
            listing_list=[ListingView(7, OUTPUT, 99, 5, market.t - 900, market.t + 86_400)],
        ),
    )
    assert second == []


def test_horizon_floor_zero_keeps_inventory_unlisted():
    market = output_market(demand=Fraction(3))
    result = strategy_for(post_horizon_hours=24).decide(market, FakeAccount(inventory_map={OUTPUT: 10}))
    assert result == []


@pytest.mark.parametrize("duration", [12, 48])
def test_selected_duration_is_used_for_post_and_deposit_cost(duration):
    market = output_market()
    market.deposits[OUTPUT] = 2
    result = strategy_for(listing_duration_hours=duration).decide(
        market, FakeAccount(inventory_map={OUTPUT: 2})
    )
    assert result == [Post(OUTPUT, 99, 2, duration)]
    assert market.deposit_calls == [(OUTPUT, 2, duration)]


def test_unsupported_listing_duration_is_rejected_by_auction_rules():
    market = output_market()

    with pytest.raises(ValueError, match="not supported by auction-house rules"):
        strategy_for(listing_duration_hours=6).decide(market, FakeAccount(inventory_map={OUTPUT: 1}))


def test_duration_oracle_changes_candidate_profit_and_purchase_plan():
    expected_plan: dict[int, bool] = {}
    for duration in (12, 24, 48):
        base = output_market()
        market = DurationMarket(base.snapshots, base.fact_map, base.recipe_list, rules=RULES)
        market.deposits[OUTPUT] = 6
        market.snapshots[REAGENT] = snapshot(REAGENT, levels=(Level(62, 100),))
        gross_after_cut = Fraction(99 * 95, 100)
        replacement_deposit = Fraction(6 * duration, 12)
        profit = gross_after_cut - replacement_deposit - 62
        qualifies = profit >= Fraction(62 * 15, 100)
        expected_plan[duration] = qualifies
        result = strategy_for(listing_duration_hours=duration, max_spend_share=0.5).decide(
            market, FakeAccount(cash_value=1_000)
        )
        assert any(isinstance(intent, Buy) for intent in result) is qualifies
        if qualifies:
            assert next(intent for intent in result if isinstance(intent, Buy)) == Buy(REAGENT, 62, 8)
        else:
            assert result == []
    assert expected_plan == {12: True, 24: True, 48: False}


@pytest.mark.parametrize(
    "key,value",
    [
        ("listing_duration_hours", True),
        ("listing_duration_hours", 0),
        ("listing_duration_hours", 24.0),
        ("listing_duration_hours", "24"),
        ("post_horizon_hours", True),
        ("post_horizon_hours", 0),
        ("post_horizon_hours", 49),
        ("post_horizon_hours", "24"),
        ("post_horizon_hours", float("nan")),
        ("reprice_after_hours", True),
        ("reprice_after_hours", 0),
        ("reprice_after_hours", "4"),
        ("reprice_after_hours", float("inf")),
        ("reprice_gap", True),
        ("reprice_gap", 0),
        ("reprice_gap", 1),
        ("reprice_gap", "0.05"),
        ("reprice_gap", float("nan")),
        ("max_cancels_per_step", True),
        ("max_cancels_per_step", 0),
        ("max_cancels_per_step", 51),
        ("max_cancels_per_step", 1.0),
    ],
)
def test_listing_policy_rejects_invalid_numeric_controls(key, value):
    with pytest.raises((TypeError, ValueError)):
        strategy_for(**{key: value})


def _aged_listing_market(*, posted_age_hours=5, expiry_hours=2, fair=Fraction(100)):
    market = output_market()
    market.t = posted_age_hours * 3_600
    market.snapshots[OUTPUT] = snapshot(
        OUTPUT,
        fair=fair,
        levels=(Level(50, 100),),
    )
    listing = ListingView(
        7,
        OUTPUT,
        120,
        2,
        0,
        market.t + expiry_hours * 3_600,
    )
    return market, listing


def test_reprice_is_aged_trustworthy_bounded_and_cancel_only():
    market, listing = _aged_listing_market()
    result = strategy_for(reprice_after_hours=4).decide(
        market, FakeAccount(cash_value=10_000, listing_list=[listing])
    )
    assert result == [Cancel(7)]


def test_actual_post_view_uses_epoch_seconds_for_crafting_reprice_age():
    epoch = 1_700_000_000
    market = output_market()
    market.t = epoch
    account = Account("x", frozenset({"alchemy"}), 10_000, 10_000, {OUTPUT: 2})
    context = ExecutionContext(market, [account], epoch, 7, rules=RULES)
    assert execute_intent(context, account, Post(OUTPUT, 120, 2, 24))
    listing = account.listings_by_id[1]
    assert listing.posted_t == epoch
    assert listing.posted_step == 7
    assert account.account_view(epoch).listings()[0].posted_t == epoch

    strategy = strategy_for(reprice_after_hours=4)
    market.t = epoch + 4 * 3_600 - 1
    assert strategy.decide(market, account.account_view(market.t)) == []
    market.t = epoch + 4 * 3_600
    assert strategy.decide(market, account.account_view(market.t)) == [Cancel(1)]


def test_snipe_age_consumer_uses_actual_post_timestamp_without_source_change():
    epoch = 1_700_000_000
    market = FakeMarket(
        {1: snapshot(1, levels=(Level(50, 10), Level(80, 10)))},
        {1: strategy_facts(1, sell_price=1)},
        rules=RULES,
        t=epoch,
    )
    account = Account("x", frozenset(), 10_000, 10_000, {1: 1})
    context = ExecutionContext(market, [account], epoch, 0, rules=RULES)
    assert execute_intent(context, account, Post(1, 120, 1, 24))
    strategy = snipe_flip.build()
    market.t = epoch + 4 * 3_600 - 1
    before_threshold = strategy.decide(market, account.account_view(market.t))
    assert not any(isinstance(intent, Cancel) for intent in before_threshold)
    market.t = epoch + 4 * 3_600
    at_threshold = strategy.decide(market, account.account_view(market.t))
    assert at_threshold[:2] == [
        Cancel(1),
        Post(1, 90, 1, 24),
    ]


@pytest.mark.parametrize(
    "listing_kwargs,account_kwargs,market_kwargs",
    [
        ({"posted_age_hours": 3}, {}, {}),
        ({"expiry_hours": 0.5}, {}, {}),
        ({}, {"cash_value": 0}, {"deposit": 10}),
        ({}, {}, {"fair": None}),
        ({}, {}, {"fair": Fraction(100)}),
    ],
)
def test_reprice_refuses_young_near_expiry_unaffordable_untrusted_or_unprofitable(
    listing_kwargs, account_kwargs, market_kwargs
):
    fair_kwargs = {key: value for key, value in market_kwargs.items() if key == "fair"}
    market, listing = _aged_listing_market(**listing_kwargs, **fair_kwargs)
    if "deposit" in market_kwargs:
        market.deposits[OUTPUT] = market_kwargs["deposit"]
    if market_kwargs.get("fair") is None:
        market.snapshots[OUTPUT] = snapshot(OUTPUT, fair=None)
    elif market_kwargs.get("fair") == Fraction(100):
        market.fact_map[OUTPUT] = strategy_facts(OUTPUT, sell_price=100)
    result = strategy_for(reprice_after_hours=4).decide(
        market, FakeAccount(listing_list=[listing], **account_kwargs)
    )
    assert result == []


def test_reprice_requires_material_undercut_and_orders_oldest_then_listing_id():
    market = output_market()
    market.t = 5 * 3_600
    listings = [
        ListingView(20, OUTPUT, 120, 1, 3_600, market.t + 7_200),
        ListingView(4, OUTPUT, 120, 1, 0, market.t + 7_200),
        ListingView(9, OUTPUT, 100, 1, 0, market.t + 7_200),
    ]
    result = strategy_for(reprice_after_hours=4, max_cancels_per_step=2).decide(
        market, FakeAccount(cash_value=10_000, listing_list=listings)
    )
    assert result == [Cancel(4), Cancel(20)]


def test_engine_cancel_loses_deposit_repost_pays_new_deposit_and_budget_is_shared():
    market = Market(two_sweeps([("a", 10, 1)]), {}, engine_facts(sell=100), (), rules=RULES)
    market.set_step(0, 0, {})
    account = Account("x", frozenset(), 1_000, 1_000)
    account.listings_by_id[7] = Listing(
        listing_id=7,
        item_id=7,
        unit_price=120,
        quantity=2,
        original_quantity=2,
        posted_t=0,
        posted_step=0,
        expires_t=43_200,
        deposit_paid=30,
    )
    context = ExecutionContext(market, [account], 0, 0, {}, next_listing_id=8, rules=RULES)

    assert execute_intent(context, account, Cancel(7))
    assert account.inventory_balance == {7: 2}
    assert account.cash_balance == 1_000
    assert account.fees["deposits_lost"] == 30
    assert context.time_used == 2
    assert execute_intent(context, account, Post(7, 100, 2, 12))
    assert account.cash_balance == 970
    assert context.time_used == 5
    assert account.fees["deposits_paid"] == 30

    constrained = Account("y", frozenset(), 1_000, 1_000)
    constrained.listings_by_id[8] = Listing(
        listing_id=8,
        item_id=7,
        unit_price=120,
        quantity=1,
        original_quantity=1,
        posted_t=0,
        posted_step=0,
        expires_t=43_200,
        deposit_paid=15,
    )
    constrained_context = ExecutionContext(
        market,
        [constrained],
        0,
        0,
        {},
        time_used=TIME_BUDGET - 1,
        rules=RULES,
    )
    assert not execute_intent(constrained_context, constrained, Cancel(8))
    assert 8 in constrained.listings_by_id
    assert constrained.inventory_balance == {}
    assert constrained_context.time_used == TIME_BUDGET - 1


def test_craft_output_remains_deferred_until_following_decision():
    market = output_market(demand=Fraction(188))
    strategy = strategy_for(max_spend_share=0.5)
    strategy.decide(market, FakeAccount())
    first = strategy.decide(market, FakeAccount(inventory_map={REAGENT: 47}))
    assert first == [Craft(1, 47)]
    assert not any(isinstance(intent, Post) for intent in first)

    second = strategy.decide(market, FakeAccount(inventory_map={OUTPUT: 47}))
    assert second == [Post(OUTPUT, 99, 47, 24)]
