"""Deterministic execution and settlement rules for a backtest."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from crowley.analysis.books import Group
from crowley.core.auction_rules import AuctionHouseRules

from .accounts import Account, LedgerEvent, Listing, Mail
from .api import Buy, Cancel, Craft, Intent, Post, VendorBuy, VendorSell
from .fees import deposit
from .market import Market
from .numbers import as_fraction
from .requirements import recipe_gate

# All realism constants live in this module so the engine has one auditable source.
MAIL_DELAY_SECONDS = 3600
TIE_SHARE = Fraction(0)
PLANNED_CADENCE_SECONDS = 900
TIME_BUDGET = math.floor(0.5 * PLANNED_CADENCE_SECONDS)
BUY_CLICKS = 3
POST_CLICKS = 3
CANCEL_CLICKS = 2
VENDOR_SELL_CLICKS_PER_STACK = 1
VENDOR_BUY_CLICKS = 2
CRAFT_CLICKS = 2
CRAFT_SECONDS = 3


@dataclass
class ExecutionContext:
    market: Market
    accounts: Sequence[Account]
    t: float
    sweep_index: int
    rules: AuctionHouseRules = field(kw_only=True)
    purchased: dict[tuple[int, Any], int] = field(default_factory=dict)
    next_listing_id: int = 1
    time_used: int = 0
    wind_down: bool = False
    demand_share: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        if self.rules != self.market.rules:
            raise ValueError("execution rules do not match market rules")

    def account_index(self, account: Account) -> int:
        for index, candidate in enumerate(self.accounts):
            if candidate is account:
                return index
        raise ValueError("account does not belong to this execution context")

    def reject(self, account: Account, reason: str, intent: Intent | None = None) -> None:
        account.reject(reason, self.t, intent=type(intent).__name__ if intent is not None else None)

    def charge_clicks(self, account: Account, amount: int, intent: Intent) -> bool:
        if self.time_used + amount > TIME_BUDGET:
            self.reject(account, "time_budget", intent)
            return False
        self.time_used += amount
        account.clicks += amount
        return True


def obtainable_units(context: ExecutionContext, item_id: int) -> list[tuple[Group, int]]:
    """Return current groups that satisfy B1, net of every earlier purchase."""

    index = context.market.book_index(item_id, context.sweep_index)
    if index is None:
        return []
    future = context.market.next_interval(item_id, context.sweep_index)
    if future is None:
        return []
    next_step, interval = future
    if interval is None:
        return []
    item_books = context.market.books[item_id]
    next_index = context.market.book_index(item_id, next_step)
    if next_index is None:
        return []
    result: list[tuple[Group, int]] = []
    for group in sorted(item_books[index].groups, key=lambda x: (x.unit, str(x.result_id))):
        if interval.ceiling is not None and group.unit > interval.ceiling:
            continue
        after = next(
            (
                candidate
                for candidate in item_books[next_index].groups
                if candidate.result_id == group.result_id
            ),
            None,
        )
        next_quantity = after.quantity if after is not None else 0
        available = min(group.quantity, next_quantity) - context.purchased.get((item_id, group.result_id), 0)
        if available > 0:
            result.append((group, available))
    return result


def _net_cost(unit: Fraction, quantity: int) -> int:
    # Real captured unit prices are whole copper.  A synthetic fractional price is
    # rounded upward so cash accounting remains integer and conservative.
    return math.ceil(unit * quantity)


def _consume_lots(
    account: Account,
    item_id: int,
    quantity: int,
    total_proceeds: int,
    t: float,
    source: str,
) -> None:
    if quantity <= 0:
        return
    lots = account.lots.setdefault(item_id, [])
    remaining = quantity
    sold_before = 0
    allocated_before = 0
    while remaining:
        if lots:
            lot = lots[0]
            taken = min(remaining, lot.quantity)
            lot.quantity -= taken
            remaining -= taken
            sold_after = sold_before + taken
            allocated_after = math.floor(Fraction(total_proceeds * sold_after, quantity))
            allocated = allocated_after - allocated_before
            cost = lot.unit_cost * taken
            account.closed_trades.append(
                {
                    "item_id": item_id,
                    "quantity": taken,
                    "unit_cost": lot.unit_cost,
                    "unit_price": Fraction(allocated, taken),
                    "proceeds": allocated,
                    "profit": allocated - cost,
                    "t": t,
                    "source": source,
                }
            )
            sold_before = sold_after
            allocated_before = allocated_after
            if lot.quantity == 0:
                lots.pop(0)
        else:
            # Initial inventory is not part of the public run input, but keeping a
            # zero-cost lot makes vendor sales and craft output reportable.
            sold_after = sold_before + remaining
            allocated_after = total_proceeds
            account.closed_trades.append(
                {
                    "item_id": item_id,
                    "quantity": remaining,
                    "unit_cost": 0,
                    "unit_price": Fraction(allocated_after - allocated_before, remaining),
                    "proceeds": allocated_after - allocated_before,
                    "profit": allocated_after - allocated_before,
                    "t": t,
                    "source": source,
                }
            )
            remaining = 0


def _refund_for(listing: Listing, sold: int) -> int:
    """Refund the cumulative sold share, retaining exact integer rounding."""

    total = math.floor(Fraction(listing.deposit_paid * sold, listing.original_quantity))
    return max(0, total - listing.deposit_refunded)


def settle_fills(context: ExecutionContext) -> None:
    """Allocate each valid previous interval's bottom walk to own listings."""

    for item_id, _index in sorted(context.market.books_at(context.sweep_index).items()):
        interval = context.market.interval(item_id, context.sweep_index)
        if interval is None or interval.reached_ceiling:
            continue
        interval_start = context.market.interval_start_step(item_id, context.sweep_index)
        if interval_start is None:
            continue
        common_ceiling = interval.ceiling
        demand_by_price = interval.walk.consumed
        listings: list[tuple[Account, Listing]] = []
        for account in context.accounts:
            listings.extend(
                (account, listing)
                for listing in account.listings_by_id.values()
                if listing.item_id == item_id
                and listing.quantity > 0
                and listing.posted_step < interval_start
            )
        listings.sort(key=lambda pair: (pair[1].unit_price, pair[1].posted_step, pair[1].listing_id))
        filled_before = 0
        for account, listing in listings:
            if common_ceiling is not None and listing.unit_price > common_ceiling:
                continue
            demand = sum(quantity for unit, quantity in demand_by_price if unit > listing.unit_price)
            demand_capacity = math.floor(context.demand_share * demand)
            fill = min(listing.quantity, max(0, demand_capacity - filled_before))
            if fill <= 0:
                continue
            listing.quantity -= fill
            filled_before += fill
            gross = listing.unit_price * fill
            proceeds = math.floor(Fraction(gross) * (1 - context.rules.seller_cut))
            ah_fee = gross - proceeds
            cumulative_sold = listing.original_quantity - listing.quantity
            refund = _refund_for(listing, cumulative_sold)
            listing.deposit_refunded += refund
            account.add_fee("ah_cut", ah_fee)
            account.add_fee("deposits_refunded", refund)
            account.add_count("units_sold", fill)
            account.mail.append(Mail(context.t + MAIL_DELAY_SECONDS, cash=proceeds + refund))
            _consume_lots(account, item_id, fill, proceeds, context.t, "auction")
            account.ledger.append(
                LedgerEvent(
                    context.t,
                    "sale",
                    item_id=item_id,
                    quantity=fill,
                    copper=proceeds,
                    unit_price=listing.unit_price,
                    listing_id=listing.listing_id,
                    details={"gross": gross, "ah_cut": ah_fee, "deposit_refund": refund},
                )
            )
            if listing.quantity == 0:
                account.listings_by_id.pop(listing.listing_id, None)


def settle_expiry(context: ExecutionContext) -> None:
    for account in context.accounts:
        expired = [
            listing
            for listing in account.listings_by_id.values()
            if listing.quantity > 0 and listing.expires_t <= context.t
        ]
        for listing in expired:
            account.mail.append(
                Mail(context.t + MAIL_DELAY_SECONDS, items={listing.item_id: listing.quantity})
            )
            lost = listing.deposit_paid - listing.deposit_refunded
            account.add_fee("deposits_lost", max(0, lost))
            account.add_count("expiries")
            account.ledger.append(
                LedgerEvent(
                    context.t,
                    "expiry",
                    item_id=listing.item_id,
                    quantity=listing.quantity,
                    listing_id=listing.listing_id,
                    details={"deposit_lost": max(0, lost)},
                )
            )
            account.listings_by_id.pop(listing.listing_id, None)


def release_mail(context: ExecutionContext) -> None:
    for account in context.accounts:
        account.release_mail(context.t)


def _execute_buy(context: ExecutionContext, account: Account, intent: Buy) -> bool:
    if intent.quantity <= 0 or intent.max_unit_price < 0:
        context.reject(account, "invalid_quantity", intent)
        return False
    if intent.item_id not in context.market.books:
        context.reject(account, "not_commodity", intent)
        return False
    index = context.market.book_index(intent.item_id, context.sweep_index)
    if index is None:
        context.reject(account, "unobtainable", intent)
        return False
    planned: list[tuple[Group, int, int]] = []
    remaining = intent.quantity
    cash = account.cash_balance
    for group, available in obtainable_units(context, intent.item_id):
        if group.unit > intent.max_unit_price or remaining <= 0:
            continue
        by_cash = math.floor(as_fraction(cash) / group.unit)
        take = min(available, remaining, by_cash)
        if take <= 0:
            continue
        cost = _net_cost(group.unit, take)
        planned.append((group, take, cost))
        cash -= cost
        remaining -= take
    filled = sum(take for _, take, _ in planned)
    if filled == 0:
        has_eligible_units = any(
            group.unit <= intent.max_unit_price
            for group, _available in obtainable_units(context, intent.item_id)
        )
        context.reject(account, "cash" if has_eligible_units else "unobtainable", intent)
        return False
    if not context.charge_clicks(account, BUY_CLICKS, intent):
        return False
    for group, take, cost in planned:
        context.purchased[(intent.item_id, group.result_id)] = (
            context.purchased.get((intent.item_id, group.result_id), 0) + take
        )
        account.cash_balance -= cost
        account.add_inventory(intent.item_id, take, unit_cost=Fraction(cost, take), t=context.t)
        account.add_count("units_bought", take)
        account.ledger.append(
            LedgerEvent(
                context.t,
                "buy",
                item_id=intent.item_id,
                quantity=take,
                copper=cost,
                unit_price=int(group.unit),
            )
        )
    return True


def _execute_post(context: ExecutionContext, account: Account, intent: Post) -> bool:
    if intent.quantity <= 0:
        context.reject(account, "invalid_quantity", intent)
        return False
    if intent.unit_price < 1:
        context.reject(account, "invalid_price", intent)
        return False
    if intent.duration_hours not in context.rules.deposit_policy.supported_durations_hours:
        context.reject(account, "invalid_duration", intent)
        return False
    fact = context.market.facts(intent.item_id)
    if fact is None:
        context.reject(account, "unknown_item", intent)
        return False
    if account.inventory_balance.get(intent.item_id, 0) < intent.quantity:
        context.reject(account, "inventory", intent)
        return False
    required = context.market.deposit(intent.item_id, intent.quantity, intent.duration_hours)
    if account.cash_balance < required:
        context.reject(account, "cash", intent)
        return False
    if not context.charge_clicks(account, POST_CLICKS, intent):
        return False
    account.remove_inventory(intent.item_id, intent.quantity)
    account.cash_balance -= required
    listing = Listing(
        listing_id=context.next_listing_id,
        item_id=intent.item_id,
        unit_price=intent.unit_price,
        quantity=intent.quantity,
        original_quantity=intent.quantity,
        posted_t=context.t,
        posted_step=context.sweep_index,
        expires_t=context.t + intent.duration_hours * 3600,
        deposit_paid=required,
    )
    context.next_listing_id += 1
    account.listings_by_id[listing.listing_id] = listing
    account.add_count("posts")
    account.add_fee("deposits_paid", required)
    account.ledger.append(
        LedgerEvent(
            context.t,
            "post",
            item_id=intent.item_id,
            quantity=intent.quantity,
            copper=required,
            unit_price=intent.unit_price,
            listing_id=listing.listing_id,
        )
    )
    return True


def _execute_cancel(context: ExecutionContext, account: Account, intent: Cancel) -> bool:
    listing = account.listings_by_id.get(intent.listing_id)
    if listing is None or listing.quantity <= 0:
        context.reject(account, "own_listing", intent)
        return False
    if not context.charge_clicks(account, CANCEL_CLICKS, intent):
        return False
    quantity = listing.quantity
    account.add_inventory(listing.item_id, quantity, t=context.t)
    lost = listing.deposit_paid - listing.deposit_refunded
    account.add_fee("deposits_lost", max(0, lost))
    account.add_count("cancels")
    account.listings_by_id.pop(intent.listing_id, None)
    account.ledger.append(
        LedgerEvent(
            context.t,
            "cancel",
            item_id=listing.item_id,
            quantity=quantity,
            listing_id=listing.listing_id,
            details={"deposit_lost": max(0, lost)},
        )
    )
    return True


def _execute_vendor_sell(context: ExecutionContext, account: Account, intent: VendorSell) -> bool:
    if intent.quantity <= 0:
        context.reject(account, "invalid_quantity", intent)
        return False
    fact = context.market.facts(intent.item_id)
    if fact is None or fact.sell_price <= 0:
        context.reject(account, "no_vendor_sell", intent)
        return False
    if account.inventory_balance.get(intent.item_id, 0) < intent.quantity:
        context.reject(account, "inventory", intent)
        return False
    stack = max(1, fact.max_stack)
    clicks = math.ceil(intent.quantity / stack)
    if not context.charge_clicks(account, clicks, intent):
        return False
    account.remove_inventory(intent.item_id, intent.quantity)
    copper = fact.sell_price * intent.quantity
    account.cash_balance += copper
    _consume_lots(
        account,
        intent.item_id,
        intent.quantity,
        fact.sell_price * intent.quantity,
        context.t,
        "vendor",
    )
    account.add_count("units_vendored", intent.quantity)
    account.ledger.append(
        LedgerEvent(context.t, "vendor_sell", item_id=intent.item_id, quantity=intent.quantity, copper=copper)
    )
    return True


def _vendor_bundle(fact: Any) -> tuple[int, int]:
    bundle = fact.vendor_bundle_size
    buy_price = fact.vendor_bundle_price
    if bundle is None or buy_price is None or bundle <= 0 or buy_price < 0:
        raise ValueError("vendor bundle must be positive")
    return int(bundle), int(buy_price)


def _execute_vendor_buy(context: ExecutionContext, account: Account, intent: VendorBuy) -> bool:
    if intent.quantity <= 0:
        context.reject(account, "invalid_quantity", intent)
        return False
    fact = context.market.facts(intent.item_id)
    if fact is None or fact.vendor_bundle_size is None or fact.vendor_bundle_price is None:
        context.reject(account, "not_vendor_supply", intent)
        return False
    try:
        bundle, buy_price = _vendor_bundle(fact)
    except ValueError:
        context.reject(account, "vendor_bundle", intent)
        return False
    bundles = math.ceil(intent.quantity / bundle)
    actual = bundles * bundle
    cost = buy_price * bundles
    if account.cash_balance < cost:
        context.reject(account, "cash", intent)
        return False
    if not context.charge_clicks(account, VENDOR_BUY_CLICKS, intent):
        return False
    account.cash_balance -= cost
    account.add_inventory(intent.item_id, actual, unit_cost=Fraction(cost, actual), t=context.t)
    account.add_count("units_vendor_bought", actual)
    account.ledger.append(
        LedgerEvent(context.t, "vendor_buy", item_id=intent.item_id, quantity=actual, copper=cost)
    )
    return True


def _execute_craft(context: ExecutionContext, account: Account, intent: Craft) -> bool:
    if intent.times <= 0:
        context.reject(account, "invalid_quantity", intent)
        return False
    recipe = next(
        (candidate for candidate in context.market._recipes if candidate.spell_id == intent.spell_id), None
    )
    if recipe is None:
        context.reject(account, "unknown_recipe", intent)
        return False
    if not recipe.professions.intersection(account.professions):
        context.reject(account, "profession", intent)
        return False
    gate = recipe_gate(recipe, account.professions, account.crafting_capabilities)
    if gate is not None and gate != "profession":
        # Capability failures happen before cooldown, time, reagent, or any
        # balance mutation.  The fine-grained gate is retained for audit data.
        account.reject(
            "capability",
            context.t,
            intent=type(intent).__name__,
            details={"gate": gate},
        )
        return False
    if recipe.cooldown_group and account.cooldowns.get(recipe.cooldown_group, 0) > context.t:
        context.reject(account, "cooldown", intent)
        return False
    if recipe.cooldown_group and recipe.cooldown_seconds > 0 and intent.times > 1:
        context.reject(account, "cooldown", intent)
        return False
    craft_times = intent.times
    remaining_time = TIME_BUDGET - context.time_used
    fit = (remaining_time - CRAFT_CLICKS) // CRAFT_SECONDS
    if fit <= 0:
        context.reject(account, "time_budget", intent)
        return False
    if craft_times > fit:
        craft_times = fit
    for item_id, quantity in recipe.reagents:
        if account.inventory_balance.get(item_id, 0) < quantity * craft_times:
            context.reject(account, "reagent", intent)
            return False
    if craft_times < intent.times:
        account.add_count("craft_truncated")
        account.ledger.append(
            LedgerEvent(
                context.t,
                "craft_truncated",
                details={"spell_id": intent.spell_id, "requested": intent.times, "times": craft_times},
            )
        )
    clicks = CRAFT_CLICKS
    if not context.charge_clicks(account, clicks, intent):
        return False
    reagent_cost = 0
    for item_id, quantity in recipe.reagents:
        required = quantity * craft_times
        lots = account.lots.get(item_id, [])
        remaining = required
        for lot in lots:
            reagent_cost += min(remaining, lot.quantity) * lot.unit_cost
            remaining -= min(remaining, lot.quantity)
            if remaining == 0:
                break
        account.remove_inventory(item_id, required)
        _remove_lots(account, item_id, required)
    output_units = sum(quantity for _, quantity in recipe.outputs) * craft_times
    output_cost = reagent_cost / output_units if output_units else Fraction(0)
    for item_id, quantity in recipe.outputs:
        account.add_inventory(item_id, quantity * craft_times, unit_cost=output_cost, t=context.t)
    if recipe.cooldown_group:
        account.cooldowns[recipe.cooldown_group] = context.t + recipe.cooldown_seconds
    account.add_count("crafted", output_units)
    account.add_count("crafts", craft_times)
    context.time_used += craft_times * CRAFT_SECONDS
    account.ledger.append(
        LedgerEvent(context.t, "craft", quantity=output_units, details={"spell_id": intent.spell_id})
    )
    return True


def _remove_lots(account: Account, item_id: int, quantity: int) -> None:
    lots = account.lots.get(item_id, [])
    remaining = quantity
    while remaining and lots:
        taken = min(remaining, lots[0].quantity)
        lots[0].quantity -= taken
        remaining -= taken
        if lots[0].quantity == 0:
            lots.pop(0)
    if not lots:
        account.lots.pop(item_id, None)


def execute_intent(context: ExecutionContext, account: Account, intent: Intent) -> bool:
    if context.wind_down and isinstance(intent, (Buy, VendorBuy, Craft)):
        context.reject(account, "wind_down", intent)
        return False
    if isinstance(intent, Buy):
        return _execute_buy(context, account, intent)
    if isinstance(intent, Post):
        return _execute_post(context, account, intent)
    if isinstance(intent, Cancel):
        return _execute_cancel(context, account, intent)
    if isinstance(intent, VendorSell):
        return _execute_vendor_sell(context, account, intent)
    if isinstance(intent, VendorBuy):
        return _execute_vendor_buy(context, account, intent)
    if isinstance(intent, Craft):
        return _execute_craft(context, account, intent)
    context.reject(account, "unknown_intent")
    return False


__all__ = [
    "BUY_CLICKS",
    "CANCEL_CLICKS",
    "CRAFT_CLICKS",
    "CRAFT_SECONDS",
    "ExecutionContext",
    "MAIL_DELAY_SECONDS",
    "POST_CLICKS",
    "TIE_SHARE",
    "TIME_BUDGET",
    "VENDOR_BUY_CLICKS",
    "deposit",
    "execute_intent",
    "obtainable_units",
    "release_mail",
    "settle_expiry",
    "settle_fills",
]
