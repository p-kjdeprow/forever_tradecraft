"""Exact listing and recipe input calculations used by the retained summary."""

from __future__ import annotations

from crowley.core.auction_rules import AuctionHouseRules

from .models import ItemFacts, Recipe


def deposit(sell_price: int, quantity: int, duration_hours: int, rules: AuctionHouseRules) -> int:
    """Return the house-specific deposit for a listing."""

    return rules.deposit_policy.calculate(sell_price, quantity, duration_hours)


def vendor_purchase(facts: ItemFacts, quantity: int) -> tuple[int, int] | None:
    """Return whole units and copper needed for a vendor purchase."""

    if quantity <= 0:
        return 0, 0
    size = facts.vendor_bundle_size
    price = facts.vendor_bundle_price
    if size is None or price is None or size <= 0 or price < 0:
        return None
    bundles = (quantity + size - 1) // size
    return bundles * size, bundles * price


def recipe_requirements(recipe: Recipe, times: int) -> dict[int, int]:
    """Total reagent units needed to craft ``recipe`` ``times`` times."""

    result: dict[int, int] = {}
    for item_id, quantity in recipe.reagents:
        if quantity > 0:
            result[item_id] = result.get(item_id, 0) + quantity * times
    return result


__all__ = ["deposit", "recipe_requirements", "vendor_purchase"]
