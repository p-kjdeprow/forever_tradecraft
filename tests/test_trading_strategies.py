from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import pytest

from crowley.core.auction_rules import AuctionHouseRules, rules_for_house
from crowley.core.house import House
from crowley.trading.api import (
    AccountView,
    Buy,
    Cancel,
    Craft,
    CraftingCapabilities,
    ItemFacts,
    ItemSnapshot,
    Level,
    ListingView,
    Post,
    Recipe,
    VendorBuy,
    VendorSell,
)
from crowley.trading.strategies import common, crafting, dip_reversion, snipe_flip

RULES = rules_for_house(House("us", "normal", "alliance"))


def snapshot(
    item_id: int,
    *,
    fair: Fraction | None = Fraction(100),
    levels: tuple[Level, ...] = (Level(50, 10),),
    fair_n: int = 20,
    band: tuple[Fraction, Fraction] | None = (Fraction(90), Fraction(110)),
    demand: Fraction | None = Fraction(100),
    eligible: float = 6 * 3600,
    trend: float | None = 0,
    half_life: float | None = 6,
) -> ItemSnapshot:
    return ItemSnapshot(
        item_id,
        1,
        levels,
        levels[0].unit_price if levels else None,
        sum(level.quantity for level in levels),
        fair,
        band,
        fair_n,
        trend,
        demand,
        eligible,
        1,
        half_life,
    )


def facts(
    item_id: int,
    *,
    sell_price: int = 1,
    bundle_size: int | None = None,
    bundle_price: int | None = None,
) -> ItemFacts:
    return ItemFacts(item_id, sell_price, bundle_size, bundle_price, 20, 0)


@dataclass
class FakeMarket:
    snapshots: dict[int, ItemSnapshot]
    fact_map: dict[int, ItemFacts]
    recipe_list: tuple[Recipe, ...] = ()
    rules: AuctionHouseRules = field(kw_only=True)
    t: int = 1
    deposits: dict[int, int] = field(default_factory=dict)
    deposit_calls: list[tuple[int, int, int]] = field(default_factory=list)
    sweep_index: int = 1

    def items(self):
        return self.snapshots

    def facts(self, item_id: int):
        return self.fact_map.get(item_id)

    def recipes(self):
        return self.recipe_list

    def deposit(self, item_id: int, quantity: int, hours: int) -> int:
        self.deposit_calls.append((item_id, quantity, hours))
        return self.deposits.get(item_id, 0) * quantity


@dataclass
class FakeAccount(AccountView):
    cash_value: int = 10_000
    inventory_map: dict[int, int] = field(default_factory=dict)
    listing_list: list[ListingView] = field(default_factory=list)
    ready: dict[str, bool] = field(default_factory=dict)
    name: str = "test"
    professions: frozenset[str] = frozenset({"alchemy"})
    crafting_capabilities: CraftingCapabilities = CraftingCapabilities()
    pending: int = 0
    transit_map: dict[int, int] = field(default_factory=dict)

    def cash(self) -> int:
        return self.cash_value

    def pending_cash(self) -> int:
        return self.pending

    def inventory(self):
        return self.inventory_map

    def in_transit(self):
        return self.transit_map

    def listings(self):
        return self.listing_list

    def cooldown_ready(self, cooldown_group: str) -> bool:
        return self.ready.get(cooldown_group, True)


def market_for(item_id: int = 1, *, item_facts: ItemFacts | None = None, **kwargs) -> FakeMarket:
    return FakeMarket(
        {item_id: snapshot(item_id, **kwargs)},
        {item_id: item_facts or facts(item_id)},
        rules=RULES,
    )


TRUST_FAILURES = [
    {"fair": None},
    {"fair_n": 15},
    {"band": None},
    {"band": (Fraction(50), Fraction(150))},
    {"demand": Fraction(0)},
    {"eligible": 6 * 3600 - 1},
]


@pytest.mark.parametrize("changes", TRUST_FAILURES)
def test_snipe_does_nothing_without_each_untrustworthy_fair(changes):
    market = market_for(**changes)
    assert snipe_flip.build().decide(market, FakeAccount(inventory_map={1: 2})) == []


@pytest.mark.parametrize("changes", TRUST_FAILURES)
def test_dip_does_nothing_without_each_untrustworthy_fair(changes):
    market = market_for(levels=(Level(80, 20),), **changes)
    assert dip_reversion.build().decide(market, FakeAccount(inventory_map={1: 2})) == []


@pytest.mark.parametrize("changes", TRUST_FAILURES)
def test_crafting_does_nothing_without_each_trustworthy_output(changes):
    recipe = Recipe(1, frozenset({"alchemy"}), ((2, 1),), ((1, 1),), None, 0, "test")
    market = FakeMarket(
        {1: snapshot(1, **changes), 2: snapshot(2, levels=(Level(10, 20),))},
        {1: facts(1), 2: facts(2, bundle_size=5, bundle_price=5)},
        (recipe,),
        rules=RULES,
    )
    assert crafting.build({"professions": ["alchemy"]}).decide(market, FakeAccount()) == []


def test_snipe_uses_worst_case_buy_limit_and_target_clamps():
    market = market_for(levels=(Level(50, 100), Level(76, 20)))
    result = snipe_flip.build().decide(market, FakeAccount(cash_value=10_000))
    # 76 is the next competitor, so the target clamps to ceil(.9 * 100).
    # The request is limited by 75 * quantity, not by the visible 50-copper level.
    assert result == [Buy(1, 74, 25)]

    upper = market_for(levels=(Level(50, 100), Level(200, 1)))
    assert snipe_flip.build().decide(upper, FakeAccount(cash_value=100_000))[-1] == Buy(1, 75, 25)

    fallback = market_for(levels=(Level(50, 100),))
    assert snipe_flip.build().decide(fallback, FakeAccount(cash_value=100_000))[-1] == Buy(1, 75, 25)


def test_snipe_spend_capacity_margin_and_deposit_caps():
    spend = market_for(levels=(Level(50, 100),))
    assert snipe_flip.build().decide(spend, FakeAccount(cash_value=1_000)) == [Buy(1, 75, 3)]

    capacity = market_for(levels=(Level(50, 100),), demand=Fraction(20))
    assert snipe_flip.build().decide(capacity, FakeAccount(cash_value=100_000)) == [Buy(1, 75, 5)]

    margin = market_for(levels=(Level(75, 10),))
    margin.deposits[1] = 20
    assert snipe_flip.build().decide(margin, FakeAccount()) == []

    unaffordable_post = market_for()
    unaffordable_post.deposits[1] = 2
    assert snipe_flip.build().decide(unaffordable_post, FakeAccount(cash_value=0, inventory_map={1: 4})) == []


def test_snipe_counts_returning_mail_against_capacity():
    market = market_for(levels=(Level(50, 100),), demand=Fraction(20))
    account = FakeAccount(cash_value=100_000, transit_map={1: 5})
    assert snipe_flip.build().decide(market, account) == []


def test_held_quantity_requires_the_account_view_transit_contract():
    class MissingTransit:
        def inventory(self):
            return {}

        def listings(self):
            return []

    with pytest.raises(AttributeError):
        common.held_quantity(MissingTransit(), 1)


def test_snipe_reprices_only_when_repost_deposit_is_reserved_and_sells_one_stack_intent():
    market = market_for(levels=(Level(50, 10), Level(80, 10)))
    market.deposits[1] = 1
    listing = ListingView(7, 1, 150, 2, market.t - 5 * 3600, market.t + 3600)
    no_cash = FakeAccount(cash_value=0, listing_list=[listing])
    assert snipe_flip.build().decide(market, no_cash) == []

    assert snipe_flip.build().decide(market, FakeAccount(cash_value=2, listing_list=[listing])) == [
        Cancel(7),
        Post(1, 90, 2, 24),
    ]

    floor_market = market_for(item_facts=facts(1, sell_price=100))
    assert snipe_flip.build().decide(floor_market, FakeAccount(inventory_map={1: 12})) == [VendorSell(1, 12)]


def test_snipe_emits_all_items_without_local_click_quotas_and_is_deterministic():
    snapshots = {item: snapshot(item, levels=(Level(50, 100),)) for item in range(1, 5)}
    market = FakeMarket(snapshots, {item: facts(item) for item in snapshots}, rules=RULES)
    account = FakeAccount(cash_value=100_000, inventory_map={item: 1 for item in snapshots})
    first = snipe_flip.build().decide(market, account)
    second = snipe_flip.build().decide(market, account)
    assert first == second
    assert [intent.item_id for intent in first if isinstance(intent, Post)] == [1, 2, 3, 4]
    assert len([intent for intent in first if isinstance(intent, Buy)]) == 4


def test_snipe_vendor_sells_fund_posts_before_acquisition():
    market = FakeMarket(
        {
            1: snapshot(1, levels=(Level(50, 10),), demand=Fraction(4)),
            2: snapshot(2, levels=(Level(50, 10),), demand=Fraction(4)),
        },
        {1: facts(1, sell_price=100), 2: facts(2)},
        rules=RULES,
    )
    market.deposits[2] = 50
    account = FakeAccount(cash_value=0, inventory_map={1: 1, 2: 1})
    assert snipe_flip.build().decide(market, account) == [
        VendorSell(1, 1),
        Post(2, 99, 1, 24),
    ]


def test_snipe_defers_an_auctionable_post_when_its_deposit_is_unaffordable():
    market = market_for(levels=(Level(50, 10),))
    market.deposits[1] = 20
    assert snipe_flip.build().decide(market, FakeAccount(cash_value=0, inventory_map={1: 1})) == []


def test_snipe_acquisitions_are_ordered_by_expected_profit():
    market = FakeMarket(
        {
            1: snapshot(1, levels=(Level(50, 100),)),
            2: snapshot(2, fair=Fraction(200), levels=(Level(50, 100),)),
        },
        {1: facts(1), 2: facts(2)},
        rules=RULES,
    )
    assert snipe_flip.build().decide(market, FakeAccount(cash_value=100_000)) == [
        Buy(2, 150, 25),
        Buy(1, 75, 25),
    ]


def test_dip_exits_before_entry_and_trims_entry_for_48_hour_margin():
    market = market_for(levels=(Level(80, 20),))
    account = FakeAccount(cash_value=100_000, inventory_map={1: 2})
    result = dip_reversion.build().decide(market, account)
    assert result == [Post(1, 98, 2, 48)]

    assert dip_reversion.build().decide(market, FakeAccount(cash_value=100_000)) == [Buy(1, 84, 20)]

    expensive = market_for(levels=(Level(80, 20),))
    expensive.deposits[1] = 20
    assert dip_reversion.build().decide(expensive, FakeAccount(cash_value=100_000)) == []


def test_dip_signal_filters_apply_to_entries_but_not_existing_exits():
    market = market_for(levels=(Level(100, 20),), half_life=20, trend=-1)
    assert dip_reversion.build().decide(market, FakeAccount(inventory_map={1: 2})) == [Post(1, 98, 2, 48)]


def test_dip_uses_own_position_age_for_exit_target():
    strategy = dip_reversion.build()
    start = market_for(levels=(Level(80, 20),))
    assert strategy.decide(start, FakeAccount(cash_value=100_000)) == [Buy(1, 84, 20)]

    inside_window = market_for(levels=(Level(80, 20),))
    inside_window.t = start.t + 49 * 3600
    assert strategy.decide(inside_window, FakeAccount(inventory_map={1: 20})) == [Post(1, 98, 20, 48)]

    after_window = market_for(levels=(Level(80, 20),))
    after_window.t = start.t + 49 * 3600 + 1
    assert strategy.decide(after_window, FakeAccount(inventory_map={1: 20})) == [Post(1, 90, 20, 48)]


def test_dip_keeps_entry_time_through_the_mail_return_gap():
    strategy = dip_reversion.build()
    start = market_for(levels=(Level(80, 20),))
    assert strategy.decide(start, FakeAccount(cash_value=100_000)) == [Buy(1, 84, 20)]

    listed = market_for(levels=(Level(80, 20),))
    listed.t = start.t + 900
    listing = ListingView(7, 1, 98, 20, listed.t, listed.t + 48 * 3600)
    assert strategy.decide(listed, FakeAccount(listing_list=[listing])) == []
    last_seen = market_for(levels=(Level(80, 20),))
    last_seen.t = listing.expires_t - 900
    assert strategy.decide(last_seen, FakeAccount(listing_list=[listing])) == []

    # The listing expired; its units are in the mail, so nothing is held this sweep. The dip is
    # still visible, but the strategy must neither re-enter nor forget the original entry time.
    in_mail = market_for(levels=(Level(80, 20),))
    in_mail.t = listing.expires_t + 900
    assert strategy.decide(in_mail, FakeAccount(cash_value=100_000)) == []

    returned = market_for(levels=(Level(80, 20),))
    returned.t = listing.expires_t + 3600 + 900
    assert strategy.decide(returned, FakeAccount(inventory_map={1: 20})) == [Post(1, 90, 20, 48)]


def test_dip_does_not_infer_age_from_unrelated_inventory_and_blocks_active_positions():
    strategy = dip_reversion.build()
    market = market_for(levels=(Level(80, 20),))
    listing = ListingView(9, 1, 98, 4, market.t, market.t + 3600)
    assert strategy.decide(market, FakeAccount(cash_value=100_000, listing_list=[listing])) == []

    unrelated = market_for(levels=(Level(80, 20),))
    unrelated.t = market.t + 49 * 3600 + 1
    assert strategy.decide(unrelated, FakeAccount(inventory_map={1: 4})) == [Post(1, 98, 4, 48)]

    active = market_for(levels=(Level(80, 20),))
    assert strategy.decide(active, FakeAccount(cash_value=100_000, listing_list=[listing])) == []


def test_dip_defers_an_auctionable_post_when_its_deposit_is_unaffordable():
    market = market_for(levels=(Level(80, 20),))
    market.deposits[1] = 20
    assert dip_reversion.build().decide(market, FakeAccount(cash_value=0, inventory_map={1: 2})) == []


def test_dip_vendor_floor_and_full_quantity_vendor_sell():
    market = market_for(levels=(Level(80, 20),), item_facts=facts(1, sell_price=100))
    assert dip_reversion.build().decide(market, FakeAccount(inventory_map={1: 12})) == [VendorSell(1, 12)]


def recipe(
    spell_id: int,
    output: int,
    reagent: int,
    *,
    reagent_count: int = 1,
    cooldown: str | None = None,
) -> Recipe:
    return Recipe(
        spell_id,
        frozenset({"alchemy"}),
        ((reagent, reagent_count),),
        ((output, 1),),
        cooldown,
        3600,
        "test",
    )


def crafting_market(recipes: tuple[Recipe, ...], *, vendor: tuple[int, int] | None = None) -> FakeMarket:
    vendor_facts = facts(
        1,
        bundle_size=None if vendor is None else vendor[0],
        bundle_price=None if vendor is None else vendor[1],
    )
    return FakeMarket(
        {
            1: snapshot(1, levels=(Level(10, 100),)),
            2: snapshot(2, levels=(Level(50, 10),)),
            3: snapshot(3, fair=Fraction(200), levels=(Level(50, 10),)),
        },
        {1: vendor_facts, 2: facts(2), 3: facts(3)},
        recipes,
        rules=RULES,
    )


def test_crafting_uses_worst_case_reagent_cost_and_batch_caps():
    market = crafting_market((recipe(1, 2, 1),))
    market.snapshots[1] = snapshot(1, levels=(Level(10, 1), Level(50, 100)))
    result = crafting.build({"professions": ["alchemy"]}).decide(market, FakeAccount(cash_value=1_000))
    # Ten crafts fit the 500-copper spend cap at the worst-case 50-copper Buy price.
    assert result == [Buy(1, 50, 10)]


def test_crafting_prefers_cheaper_whole_vendor_bundle_and_respects_profession():
    market = crafting_market((recipe(1, 2, 1, reagent_count=3),), vendor=(5, 7))
    strategy = crafting.build({"professions": ["alchemy"]})
    assert strategy.decide(market, FakeAccount()) == [VendorBuy(1, 75)]
    assert crafting.build({"professions": ["blacksmithing"]}).decide(market, FakeAccount()) == []

    one_batch = crafting_market((recipe(1, 2, 1, reagent_count=3),), vendor=(5, 7))
    one_batch.snapshots[2] = snapshot(2, demand=Fraction(4), levels=(Level(50, 10),))
    assert crafting.build({"professions": ["alchemy"]}).decide(one_batch, FakeAccount()) == [VendorBuy(1, 5)]


def test_crafting_projects_posted_output_before_buying_it_as_a_reagent():
    first = recipe(1, 2, 1)
    second = recipe(2, 3, 2)
    market = crafting_market((first, second))
    market.snapshots[3] = snapshot(3, fair=Fraction(100), levels=(Level(50, 10),))
    strategy = crafting.build({"professions": ["alchemy"]})
    assert strategy.decide(market, FakeAccount()) == [Buy(1, 10, 25)]
    result = strategy.decide(market, FakeAccount(inventory_map={1: 25}))
    assert result == [Craft(1, 25)]


def test_crafting_posts_newly_crafted_output_on_the_following_step():
    market = crafting_market((recipe(1, 2, 1),), vendor=(1, 1))
    market.snapshots[2] = snapshot(2, demand=Fraction(188), levels=(Level(50, 10),))
    strategy = crafting.build({"professions": ["alchemy"]})

    assert strategy.decide(market, FakeAccount()) == [VendorBuy(1, 47)]
    craft = strategy.decide(market, FakeAccount(cash_value=0, inventory_map={1: 47}))
    assert craft == [Craft(1, 47)]
    assert not any(isinstance(intent, Post) for intent in craft)
    assert strategy.decide(market, FakeAccount(cash_value=0, inventory_map={2: 47})) == [Post(2, 99, 47, 24)]


def test_crafting_cooldown_and_stateful_determinism():
    market = crafting_market((recipe(1, 2, 1, cooldown="daily"),), vendor=(5, 1))
    left = crafting.build({"professions": ["alchemy"]})
    right = crafting.build({"professions": ["alchemy"]})
    first = left.decide(market, FakeAccount())
    assert first == right.decide(market, FakeAccount()) == [VendorBuy(1, 5)]
    account = FakeAccount(inventory_map={1: 5}, ready={"daily": True})
    assert (
        left.decide(market, account)
        == right.decide(market, account)
        == [
            Craft(1, 1),
        ]
    )

    unready = crafting_market((recipe(1, 2, 1, cooldown="daily"),), vendor=(5, 1))
    assert (
        crafting.build({"professions": ["alchemy"]}).decide(unready, FakeAccount(ready={"daily": False}))
        == []
    )


def test_crafting_rejects_margin_even_with_ample_cash():
    market = crafting_market((recipe(1, 2, 1),))
    market.snapshots[1] = snapshot(1, levels=(Level(90, 100),))
    assert crafting.build({"professions": ["alchemy"]}).decide(market, FakeAccount(cash_value=100_000)) == []


def test_crafting_enforces_held_output_capacity_separately():
    market = crafting_market((recipe(1, 2, 1),))
    market.snapshots[2] = snapshot(2, demand=Fraction(4), levels=(Level(50, 10),))
    listing = ListingView(11, 2, 99, 1, market.t, market.t + 3600)
    assert (
        crafting.build({"professions": ["alchemy"]}).decide(
            market, FakeAccount(cash_value=100_000, listing_list=[listing])
        )
        == []
    )


def test_crafting_keeps_output_at_capacity_when_the_previous_batch_returns_in_mail():
    market = crafting_market((recipe(1, 2, 1),))
    market.snapshots[2] = snapshot(2, demand=Fraction(4), levels=(Level(50, 10),))
    strategy = crafting.build({"professions": ["alchemy"]})
    assert strategy.decide(market, FakeAccount(inventory_map={1: 1})) == []
    account = FakeAccount(inventory_map={1: 1}, transit_map={2: 1})
    assert strategy.decide(market, account) == []


def test_crafting_posts_all_outputs_with_cash_projection():
    many_outputs = tuple((item, 1) for item in range(2, 6))
    many = Recipe(8, frozenset({"alchemy"}), ((1, 1),), many_outputs, None, 0, "test")
    snapshots = {1: snapshot(1, levels=(Level(10, 100),))}
    facts_map = {1: facts(1, bundle_size=5, bundle_price=1)}
    for item in range(2, 6):
        snapshots[item] = snapshot(item, levels=(Level(50, 10),))
        facts_map[item] = facts(item)
    account = FakeAccount(inventory_map={item: 1 for item in range(2, 6)}, cash_value=10_000)
    posts = crafting.CraftingStrategy({"professions": ["alchemy"]}).decide(
        FakeMarket(snapshots, facts_map, (many,), rules=RULES), account
    )
    assert [intent.item_id for intent in posts if isinstance(intent, Post)] == [2, 3, 4, 5]


def test_determinism_for_all_strategies():
    market = market_for(levels=(Level(50, 100),))
    account = FakeAccount(cash_value=10_000, inventory_map={1: 2})
    for builder in (snipe_flip.build, dip_reversion.build):
        assert builder().decide(market, account) == builder().decide(market, account)
