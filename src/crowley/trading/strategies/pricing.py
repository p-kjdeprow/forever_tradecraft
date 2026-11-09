"""Pure pricing calculations shared by the crafting strategy and offline analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from crowley.trading.api import ItemFacts, ItemSnapshot, Recipe

from .common import (
    fair_value,
    floor_fraction,
    fraction_parameter,
    marginal_purchase,
    target_sell_price,
    vendor_purchase,
)


@dataclass(frozen=True)
class Source:
    """The cheaper way to obtain ``quantity`` units of one reagent."""

    item_id: int
    quantity: int
    kind: str  # "ah" or "vendor"
    unit_limit: int  # maximum Buy unit price for "ah"; 0 for "vendor"
    full_cost: Fraction


def recipe_requirements(recipe: Recipe, times: int) -> dict[int, int]:
    """Total reagent units needed to craft ``recipe`` ``times`` times."""

    result: dict[int, int] = {}
    for item_id, quantity in recipe.reagents:
        if quantity > 0:
            result[item_id] = result.get(item_id, 0) + quantity * times
    return result


def source_reagent(
    snapshot: ItemSnapshot | None, facts: ItemFacts | None, item_id: int, quantity: int
) -> Source | None:
    """Price ``quantity`` units from the AH book or a vendor, whichever is cheaper.

    A tie goes to the AH.  Returns None when neither source can supply the quantity.
    """

    ah = marginal_purchase(snapshot.levels, quantity) if snapshot is not None else None
    vendor = None if facts is None else vendor_purchase(facts, quantity)
    if ah is None and vendor is None:
        return None
    if ah is not None and (vendor is None or ah[0] <= vendor[1]):
        return Source(item_id, quantity, "ah", ah[1], ah[0])
    assert vendor is not None
    return Source(item_id, quantity, "vendor", 0, Fraction(vendor[1]))


def output_target_price(snapshot: ItemSnapshot, params: Mapping[str, object]) -> int:
    """The crafting strategy's integer undercut target for one output item.

    ``params`` supplies ``buy_ratio``, ``floor_ratio`` and ``cap_ratio``.
    """

    return target_sell_price(
        snapshot,
        floor_fraction(fraction_parameter(params, "buy_ratio") * fair_value(snapshot)),
        floor_ratio=fraction_parameter(params, "floor_ratio"),
        cap_ratio=fraction_parameter(params, "cap_ratio"),
    )


__all__ = ["Source", "output_target_price", "recipe_requirements", "source_reagent"]
