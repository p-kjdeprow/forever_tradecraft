"""Buy unusually deep, short-lived dips and exit near the observed fair value."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from crowley.trading.api import (
    AccountView,
    Buy,
    Intent,
    ItemFacts,
    ItemSnapshot,
    MarketView,
    Post,
    Strategy,
)

from .common import (
    SECONDS_PER_HOUR,
    as_fraction,
    ceil_fraction,
    clears_vendor_floor,
    deposit_per_unit,
    floor_fraction,
    held_quantity,
    item_inventory,
    maximum_purchase_quantity,
    net_proceeds_per_unit,
    target_sell_price,
    trustworthy_fair,
    vendor_sell,
)

DEFAULT_PARAMS = {
    "dip_ratio": 0.85,
    "exit_ratio": 0.98,
    "max_half_life": 12,
    "min_trend": -0.2,
    "min_margin": 0.10,
    "max_spend_share": 0.25,
    "capacity_share": 0.25,
    "min_fair_n": 16,
    "max_band_width": 0.35,
    "snipe_buy_ratio": 0.75,
    "snipe_floor_ratio": 0.9,
    "snipe_cap_ratio": 1.1,
}


# Mail return delay plus one recorded sweep (900 s cadence).
RETURN_GRACE_SECONDS = SECONDS_PER_HOUR + 900


@dataclass(frozen=True)
class _Plan:
    item_id: int
    snapshot: ItemSnapshot
    facts: ItemFacts


def _twentieth_price(levels) -> int | None:
    remaining = 20
    for level in sorted(levels, key=lambda entry: (entry.unit_price, entry.quantity)):
        if level.quantity <= 0:
            continue
        if level.quantity >= remaining:
            return level.unit_price
        remaining -= level.quantity
    return None


class DipReversionStrategy:
    name = "dip_reversion"
    professions = frozenset()

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        self.params = dict(DEFAULT_PARAMS)
        if params:
            self.params.update(params)
        self._entry_t: dict[int, int] = {}
        # Last step time at which this strategy saw one of its own listings for an item. A listing
        # that ends leaves the item unheld until mailed units return, so new entries wait out that
        # window instead of resetting the position's entry time.
        self._last_listing_t: dict[int, int] = {}

    def _plans(self, market: MarketView) -> dict[int, _Plan]:
        plans: dict[int, _Plan] = {}
        for item_id in sorted(market.items()):
            snapshot = market.items()[item_id]
            facts = market.facts(item_id)
            if facts is not None and trustworthy_fair(
                snapshot,
                min_fair_n=int(self.params["min_fair_n"]),
                max_band_width=self.params["max_band_width"],
            ):
                plans[item_id] = _Plan(item_id, snapshot, facts)
        return plans

    def _snipe_target(self, snapshot: ItemSnapshot) -> int:
        buy_limit = floor_fraction(as_fraction(self.params["snipe_buy_ratio"]) * snapshot.fair)
        return target_sell_price(
            snapshot,
            buy_limit,
            floor_ratio=self.params["snipe_floor_ratio"],
            cap_ratio=self.params["snipe_cap_ratio"],
        )

    def decide(self, market: MarketView, account: AccountView) -> Sequence[Intent]:
        plans = self._plans(market)
        if not plans:
            return []

        inventory = {item_id: item_inventory(account.inventory(), item_id) for item_id in account.inventory()}
        held = {item_id: held_quantity(account, item_id) for item_id in plans}
        for listing in account.listings():
            self._last_listing_t[listing.item_id] = market.t
        projected_cash = max(0, int(account.cash()))
        intents: list[Intent] = []
        vendor_exits: list[tuple[int, int]] = []
        post_exits: list[tuple[Fraction, int, int, int]] = []

        for item_id, plan in plans.items():
            quantity = item_inventory(inventory, item_id)
            if quantity <= 0:
                continue
            entry_t = self._entry_t.get(item_id, market.t)
            age = market.t - entry_t
            target = (
                ceil_fraction(as_fraction(self.params["exit_ratio"]) * plan.snapshot.fair)
                if age <= 48 * SECONDS_PER_HOUR + SECONDS_PER_HOUR
                else self._snipe_target(plan.snapshot)
            )
            if not clears_vendor_floor(market.rules, target, plan.facts):
                vendor_exits.append((item_id, quantity))
                continue
            deposit = market.deposit(item_id, quantity, 48)
            proceeds = net_proceeds_per_unit(market.rules, target, Fraction(deposit) / quantity) * quantity
            post_exits.append((proceeds, item_id, quantity, deposit))

        for item_id, quantity in sorted(vendor_exits):
            plan = plans[item_id]
            sell = vendor_sell(item_id, quantity)
            if sell is None:
                continue
            intents.append(sell)
            inventory[item_id] = 0
            held[item_id] = max(0, held.get(item_id, 0) - quantity)
            projected_cash += plan.facts.sell_price * quantity
            if held[item_id] == 0:
                self._entry_t.pop(item_id, None)

        for _value, item_id, quantity, deposit in sorted(post_exits, key=lambda entry: (-entry[0], entry[1])):
            if deposit > projected_cash:
                continue
            plan = plans[item_id]
            entry_t = self._entry_t.get(item_id, market.t)
            target = (
                ceil_fraction(as_fraction(self.params["exit_ratio"]) * plan.snapshot.fair)
                if market.t - entry_t <= 48 * SECONDS_PER_HOUR + SECONDS_PER_HOUR
                else self._snipe_target(plan.snapshot)
            )
            intents.append(Post(item_id, target, quantity, 48))
            projected_cash -= deposit
            inventory[item_id] = 0

        opportunities: list[tuple[Fraction, int, _Plan, int]] = []
        for item_id, plan in plans.items():
            if held.get(item_id, 0) > 0:
                continue
            last_listing_t = self._last_listing_t.get(item_id)
            if last_listing_t is not None and market.t - last_listing_t <= RETURN_GRACE_SECONDS:
                continue
            snapshot = plan.snapshot
            if (
                snapshot.half_life_hours is None
                or snapshot.half_life_hours > float(self.params["max_half_life"])
                or snapshot.trend_per_day is None
                or snapshot.trend_per_day <= float(self.params["min_trend"])
            ):
                continue
            ref20 = _twentieth_price(snapshot.levels)
            if ref20 is None or ref20 > as_fraction(self.params["dip_ratio"]) * snapshot.fair:
                continue
            target = ceil_fraction(as_fraction(self.params["exit_ratio"]) * snapshot.fair)
            if not clears_vendor_floor(market.rules, target, plan.facts):
                continue
            deposit = deposit_per_unit(market, item_id, 48)
            net = net_proceeds_per_unit(market.rules, target, deposit)
            effective_limit = min(
                floor_fraction(as_fraction(self.params["dip_ratio"]) * snapshot.fair),
                floor_fraction(net / (1 + as_fraction(self.params["min_margin"]))),
            )
            if effective_limit <= 0:
                continue
            demand = snapshot.demand_units_per_day
            assert demand is not None
            capacity = max(
                0,
                floor_fraction(as_fraction(self.params["capacity_share"]) * demand) - held.get(item_id, 0),
            )
            budget = floor_fraction(as_fraction(self.params["max_spend_share"]) * projected_cash)
            quantity = maximum_purchase_quantity(snapshot.levels, effective_limit, budget, capacity)
            if quantity > 0:
                opportunities.append((quantity * (net - effective_limit), item_id, plan, effective_limit))

        for _potential, item_id, plan, effective_limit in sorted(
            opportunities, key=lambda entry: (-entry[0], entry[1])
        ):
            demand = plan.snapshot.demand_units_per_day
            assert demand is not None
            budget = floor_fraction(as_fraction(self.params["max_spend_share"]) * projected_cash)
            capacity = max(
                0,
                floor_fraction(as_fraction(self.params["capacity_share"]) * demand) - held.get(item_id, 0),
            )
            quantity = maximum_purchase_quantity(plan.snapshot.levels, effective_limit, budget, capacity)
            if quantity <= 0:
                continue
            intents.append(Buy(item_id, effective_limit, quantity))
            projected_cash -= quantity * effective_limit
            held[item_id] = held.get(item_id, 0) + quantity
            self._entry_t[item_id] = market.t
        return intents


def build(params: Mapping[str, object] | None = None) -> Strategy:
    return DipReversionStrategy(params)


__all__ = ["DEFAULT_PARAMS", "DipReversionStrategy", "build"]
