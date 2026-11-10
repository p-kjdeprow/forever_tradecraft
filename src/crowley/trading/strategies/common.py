"""Small, deterministic calculations shared by trading strategies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor

from crowley.core.auction_rules import AuctionHouseRules
from crowley.trading.api import AccountView, ItemFacts, ItemSnapshot, Level, MarketView, VendorSell
from crowley.trading.numbers import FractionInput, as_fraction

SECONDS_PER_HOUR = 3_600
MIN_DEMAND_ELIGIBLE_SECONDS = 6 * SECONDS_PER_HOUR
MIN_FAIR_N = 16
MAX_BAND_WIDTH = Fraction(35, 100)


def floor_fraction(value: FractionInput) -> int:
    return floor(as_fraction(value))


def ceil_fraction(value: FractionInput) -> int:
    return ceil(as_fraction(value))


def fraction_parameter(params: Mapping[str, object], name: str) -> Fraction:
    """Validate and convert one numeric strategy parameter at its config boundary."""

    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float, Decimal, str)):
        raise TypeError(f"strategy parameter {name!r} must be a numeric value")
    return as_fraction(value)


def trustworthy_fair(
    snapshot: ItemSnapshot,
    *,
    min_fair_n: int = MIN_FAIR_N,
    max_band_width: FractionInput = MAX_BAND_WIDTH,
    min_demand_eligible_seconds: float = MIN_DEMAND_ELIGIBLE_SECONDS,
) -> bool:
    """Return whether the snapshot has enough history for a price decision."""

    fair = snapshot.fair
    band = snapshot.fair_band
    demand = snapshot.demand_units_per_day
    if fair is None or fair <= 0 or band is None or demand is None:
        return False
    p25, p75 = band
    if p75 < p25 or snapshot.fair_n < min_fair_n:
        return False
    if (p75 - p25) / fair > as_fraction(max_band_width):
        return False
    return demand > 0 and snapshot.demand_eligible_seconds >= min_demand_eligible_seconds


def fair_value(snapshot: ItemSnapshot) -> Fraction:
    """Return a snapshot's fair value after a strategy trust check."""

    if snapshot.fair is None:
        raise ValueError("strategy decision requires a fair value")
    return snapshot.fair


def held_quantity(account: AccountView, item_id: int) -> int:
    """Units held by the account, including listings and delayed mail."""

    inventory = max(0, int(account.inventory().get(item_id, 0)))
    listed = sum(
        max(0, listing.quantity_remaining) for listing in account.listings() if listing.item_id == item_id
    )
    transit = max(0, int(account.in_transit().get(item_id, 0)))
    return inventory + listed + transit


def capacity_limit(snapshot: ItemSnapshot, account: AccountView, capacity_share: FractionInput) -> int:
    """Remaining units allowed by the demand-based capacity cap."""

    assert snapshot.demand_units_per_day is not None
    cap = floor_fraction(as_fraction(capacity_share) * snapshot.demand_units_per_day)
    return max(0, cap - held_quantity(account, snapshot.item_id))


def deposit_per_unit(market: MarketView, item_id: int, duration_hours: int) -> Fraction:
    """The current auction deposit for one unit of an item."""

    return Fraction(market.deposit(item_id, 1, duration_hours))


def net_proceeds_per_unit(
    rules: AuctionHouseRules,
    sale_price: FractionInput,
    deposit: FractionInput = 0,
) -> Fraction:
    """Sale proceeds after the auction cut and a per-unit deposit."""

    return as_fraction(sale_price) * (1 - rules.seller_cut) - as_fraction(deposit)


def margin_clears(
    rules: AuctionHouseRules,
    sale_price: FractionInput,
    buy_price: FractionInput,
    deposit: FractionInput,
    minimum_margin: FractionInput,
) -> bool:
    """Whether net proceeds meet the requested profit as a fraction of cost."""

    buy = as_fraction(buy_price)
    return buy > 0 and net_proceeds_per_unit(rules, sale_price, deposit) >= buy * (
        1 + as_fraction(minimum_margin)
    )


def cost_to_buy_n_units(levels: Iterable[Level], quantity: int) -> Fraction | None:
    """Worst-case cost of requesting ``quantity`` units from visible levels."""

    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if quantity == 0:
        return Fraction(0)
    remaining = quantity
    highest = 0
    for level in sorted(levels, key=lambda entry: (entry.unit_price, entry.quantity)):
        available = max(0, level.quantity)
        take = min(remaining, available)
        highest = max(highest, level.unit_price) if take else highest
        remaining -= take
        if remaining == 0:
            return Fraction(quantity * highest)
    return None


def marginal_purchase(levels: Iterable[Level], quantity: int) -> tuple[Fraction, int] | None:
    """Return worst-case cost and the maximum Buy price for a purchase."""

    if quantity <= 0:
        return Fraction(0), 0
    remaining = quantity
    highest = 0
    for level in sorted(levels, key=lambda entry: (entry.unit_price, entry.quantity)):
        take = min(remaining, max(0, level.quantity))
        if take:
            highest = max(highest, level.unit_price)
            remaining -= take
        if remaining == 0:
            return Fraction(quantity * highest), highest
    return None


def maximum_purchase_quantity(
    levels: Iterable[Level], max_unit_price: int, budget: int, capacity: int
) -> int:
    """Largest safe request under both a unit-price and a total-spend limit."""

    if max_unit_price <= 0 or budget <= 0 or capacity <= 0:
        return 0
    remaining_budget = budget
    quantity = 0
    for level in sorted(levels, key=lambda entry: (entry.unit_price, entry.quantity)):
        if level.unit_price > max_unit_price:
            break
        available = min(max(0, level.quantity), capacity - quantity)
        if available <= 0:
            break
        if level.unit_price <= 0:
            continue
        affordable = min(available, remaining_budget // max_unit_price)
        quantity += affordable
        remaining_budget -= affordable * max_unit_price
        if affordable < available:
            break
        if quantity == capacity:
            break
    return quantity


def target_sell_price(
    snapshot: ItemSnapshot,
    buy_limit: int,
    *,
    floor_ratio: FractionInput = 0.9,
    cap_ratio: FractionInput = 1.1,
) -> int:
    """Compute the integer undercut target described by the strategy contract."""

    if snapshot.fair is None:
        raise ValueError("target price requires a fair value")
    lower = ceil_fraction(as_fraction(floor_ratio) * snapshot.fair)
    upper = floor_fraction(as_fraction(cap_ratio) * snapshot.fair)
    competitors = [level.unit_price for level in snapshot.levels if level.unit_price > buy_limit]
    next_competitor = min(competitors) if competitors else snapshot.fair
    candidate = as_fraction(next_competitor) - 1
    return ceil_fraction(max(as_fraction(lower), min(as_fraction(upper), candidate)))


def vendor_floor(rules: AuctionHouseRules, facts: ItemFacts) -> Fraction:
    """Auction price whose post-cut proceeds equal the vendor sale price."""

    return Fraction(facts.sell_price) / (1 - rules.seller_cut)


def clears_vendor_floor(rules: AuctionHouseRules, unit_price: int, facts: ItemFacts) -> bool:
    return Fraction(unit_price) >= vendor_floor(rules, facts)


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


def vendor_sell(item_id: int, quantity: int) -> VendorSell | None:
    """Create a vendor-sale intent only when it has positive quantity."""

    return VendorSell(item_id, quantity) if quantity > 0 else None


def item_inventory(inventory: Mapping[int, int], item_id: int) -> int:
    return max(0, int(inventory.get(item_id, 0)))


def output_value(
    market: MarketView,
    item_id: int,
    quantity: int,
    unit_price: int,
    duration_hours: int = 24,
) -> Fraction:
    """Post-cut value of a quantity, including its auction deposit."""

    if quantity <= 0:
        return Fraction(0)
    return Fraction(quantity) * unit_price * (1 - market.rules.seller_cut) - market.deposit(
        item_id, quantity, duration_hours
    )


__all__ = [
    "MAX_BAND_WIDTH",
    "MIN_DEMAND_ELIGIBLE_SECONDS",
    "MIN_FAIR_N",
    "SECONDS_PER_HOUR",
    "as_fraction",
    "capacity_limit",
    "ceil_fraction",
    "clears_vendor_floor",
    "cost_to_buy_n_units",
    "deposit_per_unit",
    "floor_fraction",
    "held_quantity",
    "item_inventory",
    "margin_clears",
    "marginal_purchase",
    "maximum_purchase_quantity",
    "net_proceeds_per_unit",
    "output_value",
    "target_sell_price",
    "trustworthy_fair",
    "vendor_purchase",
    "vendor_sell",
    "vendor_floor",
]
