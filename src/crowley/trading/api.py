"""Contract between the trading engine and its strategies.

Strategies see only this module's types: an immutable view of the market at one sweep
(strictly-past statistics only), a view of their own account, and the intents they may
emit. The engine owns execution, accounting and every realism rule. Neither side may
change this contract without changing both.

Money is integer copper. Prices in views may be Fractions (medians); intents carry
integer copper unit prices because the auction house only accepts whole copper.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol, cast

from crowley.core.auction_rules import AuctionHouseRules

COPPER_PER_GOLD = 10_000


# -- market view ----------------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    """Units at one unit price that this strategy could buy at this sweep."""

    unit_price: int
    quantity: int


@dataclass(frozen=True)
class ItemSnapshot:
    """One commodity's state at the current sweep.

    ``levels`` is the observed book at this sweep, ascending by price, net of units any
    strategy already bought in this backtest. It reveals nothing about the future: the
    engine decides privately which of these units a Buy can actually obtain, so a Buy may
    fill less than requested. ``visible_min``/``visible_units`` describe ``levels``.
    Every statistic below uses only books strictly before ``t``; None means not observable.
    """

    item_id: int
    t: int  # when this item's book was observed (within the current sweep)
    levels: tuple[Level, ...]
    visible_min: int | None
    visible_units: int
    fair: Fraction | None
    fair_band: tuple[Fraction, Fraction] | None
    fair_n: int
    trend_per_day: float | None
    demand_units_per_day: Fraction | None
    demand_eligible_seconds: float
    volatility_per_hour: float | None
    half_life_hours: float | None


@dataclass(frozen=True)
class ItemFacts:
    """Static client facts. A vendor sells ``vendor_bundle_size`` units for
    ``vendor_bundle_price`` copper; both are None unless the item is a known vendor supply
    (only those may be bought from a vendor). VendorBuy rounds up to whole bundles."""

    item_id: int
    sell_price: int
    vendor_bundle_size: int | None
    vendor_bundle_price: int | None
    max_stack: int
    bonding: int


@dataclass(frozen=True)
class SkillRequirement:
    """One profession skill line a recipe is learned and crafted under (client data)."""

    skill_line_id: int
    profession: str
    min_rank: int
    yellow: int
    green: int
    grey: int
    acquire_method: int


@dataclass(frozen=True)
class TeachingRequirement:
    """The skill a teaching item requires to use, from its client item row."""

    item_id: int
    skill_line_id: int
    rank: int


@dataclass(frozen=True)
class RecipeAcquisition:
    """How a recipe is learned.

    ``teaching_item_ids`` (sorted, unique) are every item that teaches it.
    ``teaching_requirements`` (sorted by item id) cover only those with client item data;
    the real learn requirement of an item-taught recipe is there, not in
    ``SkillRequirement.min_rank`` (which is 1 for most professions).
    Empty ``skill_lines`` means not loaded from client data.
    """

    teaching_item_ids: tuple[int, ...] = ()
    skill_lines: tuple[SkillRequirement, ...] = ()
    teaching_requirements: tuple[TeachingRequirement, ...] = ()


@dataclass(frozen=True)
class Recipe:
    """A crafting recipe with fixed yields only (random-yield recipes are not offered)."""

    spell_id: int
    professions: frozenset[str]
    reagents: tuple[tuple[int, int], ...]
    outputs: tuple[tuple[int, int], ...]
    cooldown_group: str | None
    cooldown_seconds: int
    learn_evidence: str
    # ``totems`` are exact item ids which must be held while casting.  The
    # client calls these spell totems; they are not consumed as reagents.
    totems: tuple[int, ...] = ()
    totem_categories: tuple[int, ...] = ()
    spell_focus: int | None = None
    acquisition: RecipeAcquisition = RecipeAcquisition()


@dataclass(frozen=True)
class CraftingCapabilities:
    """Explicit, immutable capabilities available to a simulated character.

    Empty sets deliberately mean unavailable.  The engine never infers a tool
    or station from item names, professions, or recipe text.
    """

    tool_item_ids: frozenset[int] = frozenset()
    tool_category_ids: frozenset[int] = frozenset()
    spell_focus_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for field_name, label, values in (
            ("tool_item_ids", "tool item", self.tool_item_ids),
            ("tool_category_ids", "tool category", self.tool_category_ids),
            ("spell_focus_ids", "spell focus", self.spell_focus_ids),
        ):
            if values is None or isinstance(values, (str, bytes, bytearray)):
                raise TypeError(f"{label} capabilities must be an iterable of ids")
            try:
                raw_values = tuple(values)
            except TypeError as exc:
                raise TypeError(f"{label} capabilities must be an iterable of ids") from exc
            seen: set[int] = set()
            for value in raw_values:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{label} capability ids must be positive integers")
                if value in seen:
                    raise ValueError(f"{label} capability ids must not contain duplicates")
                seen.add(value)
            canonical = frozenset(raw_values)
            object.__setattr__(self, field_name, canonical)

    @classmethod
    def from_values(
        cls,
        tool_item_ids: Sequence[int] = (),
        tool_category_ids: Sequence[int] = (),
        spell_focus_ids: Sequence[int] = (),
    ) -> CraftingCapabilities:
        return cls(
            cast(frozenset[int], tool_item_ids),
            cast(frozenset[int], tool_category_ids),
            cast(frozenset[int], spell_focus_ids),
        )

    def as_dict(self) -> dict[str, list[int]]:
        return {
            "spell_focus_ids": sorted(self.spell_focus_ids),
            "tool_category_ids": sorted(self.tool_category_ids),
            "tool_item_ids": sorted(self.tool_item_ids),
        }

    @property
    def canonical(self) -> dict[str, list[int]]:
        return self.as_dict()


class MarketView(Protocol):
    """One decision step per recorded sweep, taken when that sweep finished (``t``)."""

    t: int
    replay_ordinal: int
    rules: AuctionHouseRules

    def items(self) -> Mapping[int, ItemSnapshot]:
        """Commodities with an observed full or bounded book at this sweep."""
        ...

    def facts(self, item_id: int) -> ItemFacts | None: ...

    def recipes(self) -> Sequence[Recipe]: ...

    def deposit(self, item_id: int, quantity: int, duration_hours: int) -> int:
        """Copper deposit the engine charges for this post."""
        ...


# -- account view ---------------------------------------------------------------------


@dataclass(frozen=True)
class ListingView:
    """An own listing exposed with epoch-second timestamps, never sweep indices."""

    listing_id: int
    item_id: int
    unit_price: int
    quantity_remaining: int
    posted_t: float
    expires_t: float


class AccountView(Protocol):
    name: str
    professions: frozenset[str]
    crafting_capabilities: CraftingCapabilities

    def cash(self) -> int:
        """Copper available to spend now."""
        ...

    def pending_cash(self) -> int:
        """Sale proceeds still in the mail."""
        ...

    def inventory(self) -> Mapping[int, int]:
        """Units owned and not listed, by item."""
        ...

    def in_transit(self) -> Mapping[int, int]:
        """Units owned but still in the mail (e.g. returning from an expired listing), by item.
        They are not yet usable, but they are held: count them against capacity."""
        ...

    def listings(self) -> Sequence[ListingView]: ...

    def cooldown_ready(self, cooldown_group: str) -> bool: ...


# -- intents --------------------------------------------------------------------------


@dataclass(frozen=True)
class Buy:
    """Buy up to ``quantity`` units, cheapest first, never above ``max_unit_price``."""

    item_id: int
    max_unit_price: int
    quantity: int


@dataclass(frozen=True)
class Post:
    item_id: int
    unit_price: int
    quantity: int
    duration_hours: int


@dataclass(frozen=True)
class Cancel:
    listing_id: int


@dataclass(frozen=True)
class VendorSell:
    item_id: int
    quantity: int


@dataclass(frozen=True)
class VendorBuy:
    item_id: int
    quantity: int


@dataclass(frozen=True)
class Craft:
    spell_id: int
    times: int


Intent = Buy | Post | Cancel | VendorSell | VendorBuy | Craft


class Strategy(Protocol):
    """A deterministic strategy with an explicit replay-state contract."""

    name: str
    professions: frozenset[str]
    params: Mapping[str, object]

    def decide(self, market: MarketView, account: AccountView) -> Sequence[Intent]: ...

    def checkpoint(self) -> Mapping[str, object]:
        """Return exactly the mutable state needed by the next decision."""
        ...

    def restore(self, state: Mapping[str, object]) -> None:
        """Restore a state previously returned by :meth:`checkpoint`."""
        ...
