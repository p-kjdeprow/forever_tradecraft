"""Buy discounted, liquid commodities and relist them at a deterministic target."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from crowley.trading.api import (
    AccountView,
    Buy,
    Cancel,
    Intent,
    ItemFacts,
    ItemSnapshot,
    MarketView,
    Post,
    Strategy,
)

from .common import (
    SECONDS_PER_HOUR,
    clears_vendor_floor,
    deposit_per_unit,
    fair_value,
    floor_fraction,
    fraction_parameter,
    held_quantity,
    item_inventory,
    maximum_purchase_quantity,
    net_proceeds_per_unit,
    target_sell_price,
    trustworthy_fair,
    vendor_sell,
)

DEFAULT_PARAMS = {
    "buy_ratio": 0.75,
    "floor_ratio": 0.9,
    "cap_ratio": 1.1,
    "min_margin": 0.15,
    "max_spend_share": 0.25,
    "capacity_share": 0.25,
    "min_fair_n": 16,
    "max_band_width": 0.35,
    "reprice_after_hours": 4,
}


@dataclass(frozen=True)
class _Plan:
    item_id: int
    snapshot: ItemSnapshot
    facts: ItemFacts
    buy_limit: int
    target: int


class SnipeFlipStrategy:
    name = "snipe_flip"
    professions = frozenset()

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        self.params = dict(DEFAULT_PARAMS)
        if params:
            self.params.update(params)

    def _plans(self, market: MarketView) -> dict[int, _Plan]:
        plans: dict[int, _Plan] = {}
        items = market.items()
        for item_id in sorted(items):
            snapshot = items[item_id]
            facts = market.facts(item_id)
            if facts is None or not trustworthy_fair(
                snapshot,
                min_fair_n=int(self.params["min_fair_n"]),
                max_band_width=fraction_parameter(self.params, "max_band_width"),
            ):
                continue
            buy_limit = floor_fraction(fraction_parameter(self.params, "buy_ratio") * fair_value(snapshot))
            plans[item_id] = _Plan(
                item_id,
                snapshot,
                facts,
                buy_limit,
                target_sell_price(
                    snapshot,
                    buy_limit,
                    floor_ratio=fraction_parameter(self.params, "floor_ratio"),
                    cap_ratio=fraction_parameter(self.params, "cap_ratio"),
                ),
            )
        return plans

    def _buy_limit(self, plan: _Plan, market: MarketView) -> tuple[int, Fraction] | None:
        if not clears_vendor_floor(market.rules, plan.target, plan.facts):
            return None
        deposit = deposit_per_unit(market, plan.item_id, 24)
        net = net_proceeds_per_unit(market.rules, plan.target, deposit)
        margin_limit = floor_fraction(net / (1 + fraction_parameter(self.params, "min_margin")))
        effective_limit = min(plan.buy_limit, margin_limit)
        if effective_limit <= 0 or not any(
            level.quantity > 0 and level.unit_price <= effective_limit for level in plan.snapshot.levels
        ):
            return None
        return effective_limit, net

    def decide(self, market: MarketView, account: AccountView) -> Sequence[Intent]:
        plans = self._plans(market)
        if not plans:
            return []

        inventory = {item_id: item_inventory(account.inventory(), item_id) for item_id in account.inventory()}
        held = {item_id: held_quantity(account, item_id) for item_id in plans}
        projected_cash = max(0, int(account.cash()))
        intents: list[Intent] = []
        vendor_exits: list[tuple[int, int]] = []
        post_candidates: list[tuple[Fraction, int, int, int]] = []

        for item_id, plan in plans.items():
            quantity = item_inventory(inventory, item_id)
            if quantity <= 0:
                continue
            if not clears_vendor_floor(market.rules, plan.target, plan.facts):
                vendor_exits.append((item_id, quantity))
                continue
            deposit = market.deposit(item_id, quantity, 24)
            proceeds = (
                net_proceeds_per_unit(market.rules, plan.target, Fraction(deposit) / quantity) * quantity
            )
            post_candidates.append((proceeds, item_id, quantity, deposit))

        for item_id, quantity in sorted(vendor_exits):
            plan = plans[item_id]
            sell = vendor_sell(item_id, quantity)
            if sell is None:
                continue
            intents.append(sell)
            inventory[item_id] = 0
            held[item_id] = max(0, held.get(item_id, 0) - quantity)
            projected_cash += plan.facts.sell_price * quantity

        for _value, item_id, quantity, deposit in sorted(
            post_candidates, key=lambda entry: (-entry[0], entry[1])
        ):
            if deposit <= projected_cash:
                plan = plans[item_id]
                intents.append(Post(item_id, plan.target, quantity, 24))
                projected_cash -= deposit
                inventory[item_id] = 0

        reprice_age = fraction_parameter(self.params, "reprice_after_hours") * SECONDS_PER_HOUR
        for listing in sorted(account.listings(), key=lambda entry: entry.listing_id):
            plan = plans.get(listing.item_id)
            if plan is None or listing.quantity_remaining <= 0 or plan.snapshot.visible_min is None:
                continue
            age = market.t - listing.posted_t
            overpriced = Fraction(listing.unit_price) > Fraction(plan.snapshot.visible_min) * Fraction(
                105, 100
            )
            if age < reprice_age or age < 0 or not overpriced:
                continue
            if not clears_vendor_floor(market.rules, plan.target, plan.facts):
                continue
            repost_deposit = market.deposit(listing.item_id, listing.quantity_remaining, 24)
            if repost_deposit <= projected_cash:
                intents.append(Cancel(listing.listing_id))
                intents.append(Post(listing.item_id, plan.target, listing.quantity_remaining, 24))
                projected_cash -= repost_deposit

        opportunities: list[tuple[Fraction, int, _Plan, int]] = []
        for item_id, plan in plans.items():
            limited = self._buy_limit(plan, market)
            if limited is None:
                continue
            effective_limit, net = limited
            demand = plan.snapshot.demand_units_per_day
            assert demand is not None
            capacity = max(
                0,
                floor_fraction(fraction_parameter(self.params, "capacity_share") * demand)
                - held.get(item_id, 0),
            )
            budget = floor_fraction(fraction_parameter(self.params, "max_spend_share") * projected_cash)
            quantity = maximum_purchase_quantity(plan.snapshot.levels, effective_limit, budget, capacity)
            if quantity > 0:
                opportunities.append((quantity * (net - effective_limit), item_id, plan, effective_limit))

        for _potential, item_id, plan, effective_limit in sorted(
            opportunities, key=lambda entry: (-entry[0], entry[1])
        ):
            budget = floor_fraction(fraction_parameter(self.params, "max_spend_share") * projected_cash)
            demand = plan.snapshot.demand_units_per_day
            assert demand is not None
            capacity = max(
                0,
                floor_fraction(fraction_parameter(self.params, "capacity_share") * demand)
                - held.get(item_id, 0),
            )
            quantity = maximum_purchase_quantity(plan.snapshot.levels, effective_limit, budget, capacity)
            if quantity <= 0:
                continue
            intents.append(Buy(item_id, effective_limit, quantity))
            projected_cash -= quantity * effective_limit
            held[item_id] = held.get(item_id, 0) + quantity
        return intents


def build(params: Mapping[str, object] | None = None) -> Strategy:
    return SnipeFlipStrategy(params)


__all__ = ["DEFAULT_PARAMS", "SnipeFlipStrategy", "build"]
