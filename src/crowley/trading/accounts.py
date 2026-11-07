"""Mutable trading accounts and the read-only account view exposed to strategies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .api import AccountView, CraftingCapabilities, ListingView
from .requirements import EMPTY_CAPABILITIES


@dataclass
class Mail:
    """A delayed mail parcel.  Items are deliberately kept separate from cash."""

    arrival_t: float
    cash: int = 0
    items: dict[int, int] = field(default_factory=dict)


@dataclass
class Listing:
    listing_id: int
    item_id: int
    unit_price: int
    quantity: int
    original_quantity: int
    posted_t: float
    posted_step: int
    expires_t: float
    deposit_paid: int
    deposit_refunded: int = 0

    def view(self) -> ListingView:
        return ListingView(
            listing_id=self.listing_id,
            item_id=self.item_id,
            unit_price=self.unit_price,
            quantity_remaining=self.quantity,
            posted_t=self.posted_t,
            expires_t=self.expires_t,
        )


@dataclass(frozen=True)
class LedgerEvent:
    t: float
    kind: str
    item_id: int | None = None
    quantity: int = 0
    copper: int = 0
    unit_price: int | None = None
    listing_id: int | None = None
    reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Lot:
    item_id: int
    quantity: int
    unit_cost: int | Fraction
    acquired_t: float
    source: str = "buy"


@dataclass
class Account:
    """The engine-owned state for one strategy.

    ``cooldowns`` is injected by the engine and shared by every account in a run;
    this models one character whose profession cooldowns are not duplicated by
    splitting the capital between strategies.
    """

    name: str
    professions: frozenset[str]
    start_capital: int
    cash_balance: int
    inventory_balance: dict[int, int] = field(default_factory=dict)
    listings_by_id: dict[int, Listing] = field(default_factory=dict)
    mail: list[Mail] = field(default_factory=list)
    ledger: list[LedgerEvent] = field(default_factory=list)
    lots: dict[int, list[Lot]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    fees: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    clicks: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)
    crafting_capabilities: CraftingCapabilities = EMPTY_CAPABILITIES

    def __post_init__(self) -> None:
        if not isinstance(self.crafting_capabilities, CraftingCapabilities):
            raise TypeError("crafting_capabilities must be CraftingCapabilities")

    def add_inventory(
        self, item_id: int, quantity: int, *, unit_cost: int | Fraction | None = None, t: float = 0
    ) -> None:
        if quantity < 0:
            raise ValueError("cannot add a negative inventory quantity")
        if quantity == 0:
            return
        self.inventory_balance[item_id] = self.inventory_balance.get(item_id, 0) + quantity
        if unit_cost is not None:
            self.lots.setdefault(item_id, []).append(Lot(item_id, quantity, unit_cost, t))

    def remove_inventory(self, item_id: int, quantity: int) -> bool:
        if quantity < 0 or self.inventory_balance.get(item_id, 0) < quantity:
            return False
        remaining = self.inventory_balance[item_id] - quantity
        if remaining:
            self.inventory_balance[item_id] = remaining
        else:
            self.inventory_balance.pop(item_id, None)
        return True

    def add_count(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def add_fee(self, key: str, amount: int) -> None:
        self.fees[key] = self.fees.get(key, 0) + amount

    def reject(
        self,
        reason: str,
        t: float,
        *,
        intent: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        event_details = dict(details or {})
        if intent:
            event_details["intent"] = intent
        self.ledger.append(LedgerEvent(t, "rejection", reason=reason, details=event_details))

    def pending_cash(self) -> int:
        return sum(parcel.cash for parcel in self.mail)

    def pending_items(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for parcel in self.mail:
            for item_id, quantity in parcel.items.items():
                result[item_id] = result.get(item_id, 0) + quantity
        return result

    def in_transit(self) -> Mapping[int, int]:
        """Return item units still waiting in the mail."""

        return self.pending_items()

    def listed_units(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for listing in self.listings_by_id.values():
            if listing.quantity:
                result[listing.item_id] = result.get(listing.item_id, 0) + listing.quantity
        return result

    def release_mail(self, t: float) -> None:
        remaining: list[Mail] = []
        for parcel in self.mail:
            if parcel.arrival_t <= t:
                self.cash_balance += parcel.cash
                for item_id, quantity in parcel.items.items():
                    self.add_inventory(item_id, quantity, t=t)
                self.ledger.append(
                    LedgerEvent(t, "mail", copper=parcel.cash, details={"arrival_t": parcel.arrival_t})
                )
            else:
                remaining.append(parcel)
        self.mail = remaining

    def account_view(self, now: float) -> AccountView:
        return _AccountView(self, now)


class _AccountView:
    """A small dynamic read-only adapter; all returned mappings are copies."""

    def __init__(self, account: Account, now: float) -> None:
        self._account = account
        self._now = now
        self.name = account.name
        self.professions = account.professions
        self.crafting_capabilities = account.crafting_capabilities

    def cash(self) -> int:
        return self._account.cash_balance

    def pending_cash(self) -> int:
        return self._account.pending_cash()

    def inventory(self) -> Mapping[int, int]:
        return dict(self._account.inventory_balance)

    def in_transit(self) -> Mapping[int, int]:
        return dict(self._account.in_transit())

    def listings(self) -> Sequence[ListingView]:
        return tuple(
            listing.view()
            for listing in sorted(self._account.listings_by_id.values(), key=lambda x: x.listing_id)
        )

    def cooldown_ready(self, cooldown_group: str) -> bool:
        return self._account.cooldowns.get(cooldown_group, 0) <= self._now


__all__ = [
    "Account",
    "LedgerEvent",
    "Listing",
    "Lot",
    "Mail",
]
