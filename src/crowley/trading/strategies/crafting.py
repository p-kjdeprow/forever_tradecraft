"""Choose profitable recipe batches, craft from inventory, and replenish inputs."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from crowley.trading.api import (
    AccountView,
    Buy,
    Cancel,
    Craft,
    Intent,
    ItemFacts,
    ItemSnapshot,
    MarketView,
    Post,
    Recipe,
    Strategy,
    VendorBuy,
    VendorSell,
)

from ..requirements import recipe_available
from .common import (
    as_fraction,
    clears_vendor_floor,
    fair_value,
    floor_fraction,
    fraction_parameter,
    held_quantity,
    item_inventory,
    marginal_purchase,
    target_sell_price,
    trustworthy_fair,
    vendor_purchase,
)

DEFAULT_PARAMS = {
    "name": "crafting",
    "professions": (),
    "floor_ratio": 0.9,
    "cap_ratio": 1.1,
    "buy_ratio": 0.75,
    "min_margin": 0.15,
    "max_spend_share": 0.5,
    "capacity_share": 0.25,
    "min_fair_n": 16,
    "max_band_width": 0.35,
    "listing_duration_hours": 24,
    "post_horizon_hours": None,
    "reprice_after_hours": None,
    "reprice_gap": 0.05,
    "max_cancels_per_step": 1,
}


@dataclass(frozen=True)
class _Source:
    item_id: int
    quantity: int
    kind: str
    unit_limit: int
    full_cost: Fraction


@dataclass(frozen=True)
class _Candidate:
    recipe: Recipe
    times: int
    cost: Fraction
    value: Fraction
    profit: Fraction
    sources: tuple[_Source, ...]


def _requirements(recipe: Recipe, times: int) -> dict[int, int]:
    result: dict[int, int] = {}
    for item_id, quantity in recipe.reagents:
        if quantity > 0:
            result[item_id] = result.get(item_id, 0) + quantity * times
    return result


def _projected_capacity(
    snapshot,
    held: Mapping[int, int],
    capacity_share: Fraction | int | float,
) -> int:
    assert snapshot.demand_units_per_day is not None
    cap = floor_fraction(as_fraction(capacity_share) * snapshot.demand_units_per_day)
    return max(0, cap - held.get(snapshot.item_id, 0))


def _strict_fraction(value: object, name: str) -> Fraction:
    """Validate a finite numeric policy value without coercing strings or bools."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TypeError(f"{name} must be an int, float, or Fraction")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return as_fraction(value)


def _post_horizon_limit(
    snapshot,
    account: AccountView,
    capacity_share: Fraction | int | float,
    horizon_hours: Fraction | None,
) -> int | None:
    """Return the remaining optional listing exposure for one output item."""

    if horizon_hours is None:
        return None
    assert snapshot.demand_units_per_day is not None
    cap = floor_fraction(
        as_fraction(snapshot.demand_units_per_day) * as_fraction(capacity_share) * horizon_hours / 24
    )
    listed = sum(
        max(0, listing.quantity_remaining)
        for listing in account.listings()
        if listing.item_id == snapshot.item_id
    )
    return max(0, cap - listed)


class CraftingStrategy:
    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        self.params = dict(DEFAULT_PARAMS)
        if params:
            self.params.update(params)
        duration = self.params["listing_duration_hours"]
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise TypeError("listing_duration_hours must be an integer")
        if duration <= 0:
            raise ValueError("listing_duration_hours must be positive")
        horizon = self.params["post_horizon_hours"]
        if horizon is not None:
            horizon = _strict_fraction(horizon, "post_horizon_hours")
            if horizon <= 0 or horizon > 48:
                raise ValueError("post_horizon_hours must be in (0, 48]")
        reprice_after = self.params["reprice_after_hours"]
        if reprice_after is not None:
            reprice_after = _strict_fraction(reprice_after, "reprice_after_hours")
            if reprice_after <= 0:
                raise ValueError("reprice_after_hours must be positive")
        reprice_gap = _strict_fraction(self.params["reprice_gap"], "reprice_gap")
        if not 0 < reprice_gap < 1:
            raise ValueError("reprice_gap must be in (0, 1)")
        max_cancels = self.params["max_cancels_per_step"]
        if isinstance(max_cancels, bool) or not isinstance(max_cancels, int):
            raise TypeError("max_cancels_per_step must be an integer")
        if not 1 <= max_cancels <= 50:
            raise ValueError("max_cancels_per_step must be in [1, 50]")
        self._listing_duration_hours = duration
        self._post_horizon_hours = horizon
        self._reprice_after_hours = reprice_after
        self._reprice_gap = reprice_gap
        self._max_cancels_per_step = max_cancels
        professions = self.params.get("professions", ())
        if isinstance(professions, str):
            professions = (professions,)
        self.professions = frozenset(str(profession) for profession in professions)
        self.name = str(self.params.get("name", "crafting"))
        self._planned: tuple[int, int] | None = None

    def _reprice_intents(
        self,
        market: MarketView,
        account: AccountView,
        output_ids: Sequence[int],
        projected_cash: int,
    ) -> tuple[list[Intent], int]:
        """Cancel only trustworthy, materially undercut own listings.

        A replacement post is deliberately deferred to a later sweep.  The account view is
        immutable here, and execution remains responsible for losing the old deposit and
        charging the replacement deposit if a later decision posts the returned inventory.
        """

        if self._reprice_after_hours is None:
            return [], projected_cash
        output_set = set(output_ids)
        intents: list[Intent] = []
        for listing in sorted(account.listings(), key=lambda entry: (entry.posted_t, entry.listing_id)):
            if len(intents) >= self._max_cancels_per_step:
                break
            if listing.item_id not in output_set or listing.quantity_remaining <= 0:
                continue
            snapshot = market.items().get(listing.item_id)
            facts = market.facts(listing.item_id)
            if (
                snapshot is None
                or facts is None
                or not trustworthy_fair(
                    snapshot,
                    min_fair_n=int(self.params["min_fair_n"]),
                    max_band_width=fraction_parameter(self.params, "max_band_width"),
                )
            ):
                continue
            age = market.t - listing.posted_t
            if age < self._reprice_after_hours * 3_600 or age < 0:
                continue
            if listing.expires_t - market.t < 3_600:
                continue
            target = target_sell_price(
                snapshot,
                floor_fraction(fraction_parameter(self.params, "buy_ratio") * fair_value(snapshot)),
                floor_ratio=fraction_parameter(self.params, "floor_ratio"),
                cap_ratio=fraction_parameter(self.params, "cap_ratio"),
            )
            if not (
                target < listing.unit_price
                and Fraction(target) <= Fraction(listing.unit_price) * (1 - self._reprice_gap)
                and clears_vendor_floor(market.rules, target, facts)
            ):
                continue
            replacement_deposit = market.deposit(
                listing.item_id,
                listing.quantity_remaining,
                self._listing_duration_hours,
            )
            net_after_deposit = (
                Fraction(target * listing.quantity_remaining) * (1 - market.rules.seller_cut)
                - replacement_deposit
            )
            if (
                replacement_deposit > projected_cash
                or net_after_deposit <= facts.sell_price * listing.quantity_remaining
            ):
                continue
            intents.append(Cancel(listing.listing_id))
            projected_cash -= replacement_deposit
        return intents, projected_cash

    def _trusted_outputs(
        self, recipe: Recipe, market: MarketView
    ) -> dict[int, tuple[ItemSnapshot, ItemFacts, int]] | None:
        outputs: dict[int, tuple[ItemSnapshot, ItemFacts, int]] = {}
        for item_id, quantity in recipe.outputs:
            snapshot = market.items().get(item_id)
            facts = market.facts(item_id)
            if (
                quantity <= 0
                or snapshot is None
                or facts is None
                or not trustworthy_fair(
                    snapshot,
                    min_fair_n=int(self.params["min_fair_n"]),
                    max_band_width=fraction_parameter(self.params, "max_band_width"),
                )
            ):
                return None
            previous = outputs.get(item_id, (None, None, 0))[2]
            outputs[item_id] = (snapshot, facts, previous + quantity)
        return outputs or None

    def _source(self, market: MarketView, item_id: int, quantity: int) -> _Source | None:
        snapshot = market.items().get(item_id)
        facts = market.facts(item_id)
        ah = marginal_purchase(snapshot.levels, quantity) if snapshot is not None else None
        vendor = None if facts is None else vendor_purchase(facts, quantity)
        if ah is None and vendor is None:
            return None
        if ah is not None and (vendor is None or ah[0] <= vendor[1]):
            return _Source(item_id, quantity, "ah", ah[1], ah[0])
        assert vendor is not None
        return _Source(item_id, quantity, "vendor", 0, Fraction(vendor[1]))

    def _actual_purchase_cost(
        self,
        market: MarketView,
        source: _Source,
        inventory: Mapping[int, int],
    ) -> Fraction:
        missing = max(0, source.quantity - item_inventory(inventory, source.item_id))
        if missing == 0:
            return Fraction(0)
        if source.kind == "ah":
            return Fraction(missing * source.unit_limit)
        facts = market.facts(source.item_id)
        assert facts is not None
        purchase = vendor_purchase(facts, missing)
        return Fraction(10**30) if purchase is None else Fraction(purchase[1])

    def _candidate(
        self,
        recipe: Recipe,
        market: MarketView,
        account: AccountView,
        inventory: Mapping[int, int],
        held: Mapping[int, int],
        projected_cash: int,
        crafted_groups: set[str],
    ) -> _Candidate | None:
        if not self.professions.issubset(account.professions):
            return None
        if not recipe_available(recipe, self.professions, account.crafting_capabilities):
            return None
        outputs = self._trusted_outputs(recipe, market)
        if outputs is None:
            return None
        if recipe.cooldown_group is not None:
            if not account.cooldown_ready(recipe.cooldown_group):
                return None
            if recipe.cooldown_group in crafted_groups:
                return None
            max_times = 1
        else:
            max_times = 1_000_000_000
        for snapshot, _facts, quantity in outputs.values():
            max_times = min(
                max_times,
                _projected_capacity(snapshot, held, fraction_parameter(self.params, "capacity_share"))
                // quantity,
            )
        if max_times <= 0:
            return None

        best: _Candidate | None = None
        spend_limit = floor_fraction(
            fraction_parameter(self.params, "max_spend_share") * max(0, projected_cash)
        )
        for times in range(1, max_times + 1):
            sources: list[_Source] = []
            for item_id, quantity in _requirements(recipe, times).items():
                source = self._source(market, item_id, quantity)
                if source is None:
                    sources = []
                    break
                sources.append(source)
            if not sources and recipe.reagents:
                break
            purchase_cost = sum(
                (self._actual_purchase_cost(market, source, inventory) for source in sources),
                Fraction(0),
            )
            if purchase_cost > spend_limit:
                continue
            value = Fraction(0)
            for item_id, (snapshot, _facts, quantity) in outputs.items():
                target = target_sell_price(
                    snapshot,
                    floor_fraction(fraction_parameter(self.params, "buy_ratio") * fair_value(snapshot)),
                    floor_ratio=fraction_parameter(self.params, "floor_ratio"),
                    cap_ratio=fraction_parameter(self.params, "cap_ratio"),
                )
                value += quantity * times * target * (1 - market.rules.seller_cut) - market.deposit(
                    item_id, quantity * times, self._listing_duration_hours
                )
            cost = sum((source.full_cost for source in sources), Fraction(0))
            profit = value - cost
            if profit < cost * fraction_parameter(self.params, "min_margin"):
                continue
            candidate = _Candidate(recipe, times, cost, value, profit, tuple(sources))
            if best is None or (candidate.profit, candidate.times) > (best.profit, best.times):
                best = candidate
        return best

    def _craft_previous(
        self,
        market: MarketView,
        account: AccountView,
        recipes: Mapping[int, Recipe],
        inventory: dict[int, int],
        held: dict[int, int],
    ) -> tuple[list[Intent], set[str]]:
        if self._planned is None:
            return [], set()
        spell_id, planned_times = self._planned
        recipe = recipes.get(spell_id)
        if recipe is None or not self.professions.issubset(account.professions):
            self._planned = None
            return [], set()
        if not recipe_available(recipe, self.professions, account.crafting_capabilities):
            self._planned = None
            return [], set()
        before = dict(inventory)
        available = 1_000_000_000
        for item_id, quantity in recipe.reagents:
            if quantity > 0:
                available = min(available, item_inventory(inventory, item_id) // quantity)
        if recipe.cooldown_group is not None:
            if not account.cooldown_ready(recipe.cooldown_group):
                available = 0
            else:
                available = min(available, 1)
        for item_id, quantity in recipe.outputs:
            snapshot = market.items().get(item_id)
            if snapshot is None or snapshot.demand_units_per_day is None:
                available = 0
                break
            available = min(
                available,
                _projected_capacity(snapshot, held, fraction_parameter(self.params, "capacity_share"))
                // quantity,
            )
        times = min(planned_times, available)
        if times <= 0:
            return [], set()
        for item_id, quantity in recipe.reagents:
            inventory[item_id] = item_inventory(inventory, item_id) - quantity * times
        for item_id, quantity in recipe.outputs:
            inventory[item_id] = item_inventory(inventory, item_id) + quantity * times
        for item_id in set(before) | set(inventory):
            held[item_id] = max(0, held.get(item_id, 0) + inventory.get(item_id, 0) - before.get(item_id, 0))
        groups = {recipe.cooldown_group} if recipe.cooldown_group is not None else set()
        return [Craft(recipe.spell_id, times)], groups

    def _purchase_intents(
        self,
        candidate: _Candidate,
        market: MarketView,
        inventory: Mapping[int, int],
    ) -> list[Intent] | None:
        purchases: list[Intent] = []
        for source in candidate.sources:
            missing = max(0, source.quantity - item_inventory(inventory, source.item_id))
            if missing == 0:
                continue
            if source.kind == "ah":
                purchases.append(Buy(source.item_id, source.unit_limit, missing))
                continue
            facts = market.facts(source.item_id)
            assert facts is not None
            purchase = vendor_purchase(facts, missing)
            if purchase is None:
                return None
            purchases.append(VendorBuy(source.item_id, purchase[0]))
        return purchases

    def decide(self, market: MarketView, account: AccountView) -> Sequence[Intent]:
        if self._listing_duration_hours not in market.rules.deposit_policy.supported_durations_hours:
            raise ValueError(
                f"listing_duration_hours {self._listing_duration_hours} is not supported by "
                f"auction-house rules {market.rules.version}"
            )
        recipes = {
            recipe.spell_id: recipe for recipe in sorted(market.recipes(), key=lambda entry: entry.spell_id)
        }
        starting_inventory = {
            item_id: item_inventory(account.inventory(), item_id) for item_id in account.inventory()
        }
        inventory = dict(starting_inventory)
        held = {item_id: held_quantity(account, item_id) for item_id in account.inventory()}
        for recipe in recipes.values():
            for item_id, _quantity in (*recipe.reagents, *recipe.outputs):
                held.setdefault(item_id, held_quantity(account, item_id))
        intents, crafted_groups = self._craft_previous(market, account, recipes, inventory, held)
        post_inventory = dict(starting_inventory)
        for intent in intents:
            if not isinstance(intent, Craft):
                continue
            recipe = recipes[intent.spell_id]
            for item_id, quantity in recipe.reagents:
                post_inventory[item_id] = max(
                    0,
                    item_inventory(post_inventory, item_id) - quantity * intent.times,
                )
        projected_cash = max(0, int(account.cash()))

        output_ids = sorted(
            {
                item_id
                for recipe in recipes.values()
                if recipe.professions.intersection(self.professions)
                for item_id, _quantity in recipe.outputs
            }
        )
        vendor_exits: list[tuple[int, int]] = []
        post_exits: list[tuple[Fraction, int, int, int]] = []
        for item_id in output_ids:
            snapshot = market.items().get(item_id)
            facts = market.facts(item_id)
            # A Craft intent executes before the remaining intents in this decision.  Only
            # units that existed before this decision may be exited; newly crafted output is
            # deliberately left for the next sweep.
            quantity = item_inventory(post_inventory, item_id)
            if (
                snapshot is None
                or facts is None
                or quantity <= 0
                or not trustworthy_fair(
                    snapshot,
                    min_fair_n=int(self.params["min_fair_n"]),
                    max_band_width=fraction_parameter(self.params, "max_band_width"),
                )
            ):
                continue
            target = target_sell_price(
                snapshot,
                floor_fraction(fraction_parameter(self.params, "buy_ratio") * fair_value(snapshot)),
                floor_ratio=fraction_parameter(self.params, "floor_ratio"),
                cap_ratio=fraction_parameter(self.params, "cap_ratio"),
            )
            if not clears_vendor_floor(market.rules, target, facts):
                vendor_exits.append((item_id, quantity))
                continue
            post_limit = _post_horizon_limit(
                snapshot,
                account,
                fraction_parameter(self.params, "capacity_share"),
                self._post_horizon_hours,
            )
            if post_limit is not None:
                quantity = min(quantity, post_limit)
            if quantity <= 0:
                continue
            deposit = market.deposit(item_id, quantity, self._listing_duration_hours)
            proceeds = Fraction(quantity) * target * (1 - market.rules.seller_cut) - deposit
            post_exits.append((proceeds, item_id, quantity, deposit))

        for item_id, quantity in sorted(vendor_exits):
            facts = market.facts(item_id)
            assert facts is not None
            intents.append(VendorSell(item_id, quantity))
            inventory[item_id] = max(0, item_inventory(inventory, item_id) - quantity)
            post_inventory[item_id] = 0
            held[item_id] = max(0, held.get(item_id, 0) - quantity)
            projected_cash += facts.sell_price * quantity

        reprice_intents, projected_cash = self._reprice_intents(
            market,
            account,
            output_ids,
            projected_cash,
        )
        intents.extend(reprice_intents)

        for _value, item_id, quantity, deposit in sorted(post_exits, key=lambda entry: (-entry[0], entry[1])):
            if deposit > projected_cash:
                continue
            facts = market.facts(item_id)
            assert facts is not None
            snapshot = market.items()[item_id]
            target = target_sell_price(
                snapshot,
                floor_fraction(fraction_parameter(self.params, "buy_ratio") * fair_value(snapshot)),
                floor_ratio=fraction_parameter(self.params, "floor_ratio"),
                cap_ratio=fraction_parameter(self.params, "cap_ratio"),
            )
            intents.append(Post(item_id, target, quantity, self._listing_duration_hours))
            projected_cash -= deposit
            inventory[item_id] = max(0, item_inventory(inventory, item_id) - quantity)
            post_inventory[item_id] = max(0, item_inventory(post_inventory, item_id) - quantity)

        candidates: list[_Candidate] = []
        for recipe in recipes.values():
            candidate = self._candidate(
                recipe,
                market,
                account,
                inventory,
                held,
                projected_cash,
                crafted_groups,
            )
            if candidate is not None and self._purchase_intents(candidate, market, inventory) is not None:
                candidates.append(candidate)
        if not candidates:
            self._planned = None
            return intents

        candidate = max(candidates, key=lambda entry: (entry.profit, entry.times, -entry.recipe.spell_id))
        purchases = self._purchase_intents(candidate, market, inventory)
        assert purchases is not None
        self._planned = (candidate.recipe.spell_id, candidate.times)
        intents.extend(purchases)
        return intents


def build(params: Mapping[str, object] | None = None) -> Strategy:
    return CraftingStrategy(params)


__all__ = ["DEFAULT_PARAMS", "CraftingStrategy", "build"]
