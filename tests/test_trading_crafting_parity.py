"""Crafting decisions on a fixed fixture, pinned from the pre-extraction implementation."""

from __future__ import annotations

from fractions import Fraction

from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.trading.api import CraftingCapabilities, ItemFacts, ItemSnapshot, Level, Recipe
from crowley.trading.strategies.crafting import CraftingStrategy

RULES = rules_for_house(House("us", "normal", "alliance"))


def _snapshot(item_id, levels, fair, band, demand):
    return ItemSnapshot(
        item_id,
        1,
        tuple(Level(price, quantity) for price, quantity in levels),
        levels[0][0],
        sum(quantity for _price, quantity in levels),
        Fraction(fair),
        (Fraction(band[0]), Fraction(band[1])),
        20,
        0,
        Fraction(demand),
        6 * 3600,
        1,
        1,
    )


class Market:
    t = 1
    replay_ordinal = 0
    rules = RULES

    def items(self):
        return {
            10: _snapshot(10, [(5, 3), (9, 100)], 7, (6, 8), 50),  # AH reagent, cheap for a few units
            12: _snapshot(12, [(40, 100)], 40, (39, 41), 50),  # AH reagent; vendor is cheaper
            11: _snapshot(11, [(90, 5), (120, 30)], 110, (100, 118), 40),  # output
        }

    def facts(self, item_id):
        return {
            10: ItemFacts(10, 0, None, None, 20, 0),
            12: ItemFacts(12, 0, 1, 12, 20, 0),
            11: ItemFacts(11, 2, None, None, 20, 0),
        }.get(item_id)

    def recipes(self):
        return (
            Recipe(
                spell_id=1,
                professions=frozenset({"alchemy"}),
                reagents=((10, 2), (12, 1)),
                outputs=((11, 1),),
                cooldown_group=None,
                cooldown_seconds=0,
                learn_evidence="test",
            ),
        )

    def deposit(self, item_id, quantity, duration_hours):
        return 3 * quantity


class Account:
    name = "craft"
    professions = frozenset({"alchemy"})
    crafting_capabilities = CraftingCapabilities()

    def __init__(self, inventory):
        self._inventory = inventory

    def cash(self):
        return 10_000

    def pending_cash(self):
        return 0

    def inventory(self):
        return dict(self._inventory)

    def in_transit(self):
        return {}

    def listings(self):
        return []

    def cooldown_ready(self, group):
        return True


def _decide(strategy, inventory):
    return [repr(intent) for intent in strategy.decide(Market(), Account(inventory))]


def test_crafting_buys_posts_and_crafts_exactly_as_before_the_extraction() -> None:
    strategy = CraftingStrategy({"professions": ("alchemy",)})
    first = _decide(strategy, {})
    # Reagent 10 is bought from the AH at the marginal limit, reagent 12 from the vendor
    # (12 < 40), and the planned batch is remembered for the next step.
    assert first == EXPECTED_FIRST
    assert strategy.checkpoint() == {"planned": [1, 10]}
    second = _decide(strategy, {10: 10, 12: 5, 11: 4})
    assert second == EXPECTED_SECOND


EXPECTED_FIRST = [
    "Buy(item_id=10, max_unit_price=9, quantity=20)",
    "VendorBuy(item_id=12, quantity=10)",
]
EXPECTED_SECOND = [
    "Craft(spell_id=1, times=5)",
    "Post(item_id=11, unit_price=99, quantity=4, duration_hours=24)",
    "Buy(item_id=10, max_unit_price=5, quantity=2)",
    "VendorBuy(item_id=12, quantity=1)",
]
