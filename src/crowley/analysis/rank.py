"""Rank crafting trades by expected profit per hour, restricted to ones that sell.

One house, one client build, as of one sweep.  The core (``evaluate_trades`` /
``rank_trades``) is pure; ``load_rank_inputs`` reads the database read-only.

Estimators, all stated here so the table can be read honestly:

* Reagent cost per craft: the crafting strategy's own sourcing (``source_reagent``: AH
  worst-case fill at the batch quantity versus vendor, cheaper wins) divided by the batch.
* Target sell price: the crafting strategy's ``output_target_price`` on a snapshot rebuilt
  from the latest book (levels) and the strictly-past 24 h fair value.
* Net margin per craft: ``qty * target * (1 - seller_cut) - deposit`` per output, minus
  reagent cost.  The deposit is for the whole batch listing, shared out per craft.  It is
  charged in full: the simulator refunds it pro rata on sale, so this is a deliberately
  conservative assumption (margin is understated by roughly ``P(sale) * deposit``).
* Sale probability: replay of one hypothetical listing of the whole batch.  The price is
  recomputed at every start book with the same target rule (fair value strictly before that
  book, that book's levels), so each start undercuts the book it is replayed against.  A
  start counts only when its fair value exists and its outcome is determined (sold within
  the horizon, or observed unsold through it).  Overlapping starts see the same depletions,
  so evidence is the number of *independent* starts: a greedy subset at least one horizon
  apart.  ``P`` is the sold share of those independent starts; ``independent_starts`` and
  ``sold_starts`` are reported per row, and rows below ``min_independent_starts`` are
  excluded rather than shown.  A start is eligible only if ``[t, t + horizon]`` is an unbroken,
  observable run of books (no gap above MAX_GAP, no price-ceiling break), decided before and
  regardless of outcome.  The loader reads an extra 24 h of history so early starts have a full
  fair-value window.  A multi-output
  recipe takes the output with the lowest P.
* Market units per hour: commodity bottom-walk depletion (``demand_metrics``
  ``purchased_units_per_day``) over the window, divided by 24.  It counts only units
  consumed from the cheap end of the book, so it understates demand.
* Expected profit per hour: ``margin_per_craft * crafts_per_hour`` where
  ``crafts_per_hour = min over outputs (share * market_units_per_hour / output_qty)``;
  ``share`` is the capture share of the observed market flow.
* Capital per batch: reagent cost for the batch plus the batch listing deposits.
* Recipe price (recipes not known by the character): the cheapest item-kind listing of any
  teaching item in the latest sweep; payback in crafts is ``price / margin_per_craft``.
  The recipe status uses the character's latest capture at or before ``as_of`` (the client
  export is a static per-build dataset and is not selected by time).

Item-kind (non-commodity) outputs, such as gear, bags and gadgets, are evaluated on a separate path
(``analysis/item_books.py``) with the same estimators, restated for one-unit auctions:

* Book: the ``coverage = 'full'`` item-kind observation of each sweep for this house and build;
  rows are auctions (``quantity`` is the availability of identical one-unit auctions, the unit
  price is the per-auction buyout).  An item whose books carry more than one ``itemKey`` is
  excluded as ``output_item_variants`` rather than mixing variants.
* Target: ``output_target_price`` unchanged, on levels built from the book's auctions and a fair
  value that is the strictly-past 24 h median of the per-book *lowest buyout* (same ``min_fair_n``,
  band and demand-coverage trust checks).  So the target undercuts the lowest buyout above
  ``buy_ratio * fair`` by one copper, clamped to ``[floor_ratio, cap_ratio] * fair``.
* Sale evidence: auctions that disappear between consecutive full observations, judged by
  ``core.volume.compare_observations`` (grouped scopes, changed identities and unknown time-left
  give no evidence; a disappearance that could be an expiry is skipped, never counted).  A
  cheapest-first walk per interval yields the consumed units; a hypothetical listing sells when
  walk units strictly above its price reach its quantity within the horizon.  Starts, eligibility
  (an unbroken usable ``[t, t + horizon]``, decided before the outcome) and independence are the
  commodity design.  Units/h is the walk's consumed units over the interval exposures.
* Item-kind reagents are priced from the same item-kind book: the cheapest auctions of the latest
  sweep, worst-case fill at the batch quantity, versus vendor (``source_reagent`` on the book's
  levels).  A reagent whose latest book mixes ``itemKey`` variants is excluded as
  ``reagent_item_variants``.
* Outputs whose client bonding is bind-on-pickup are excluded as ``output_bind_on_pickup``; an
  output with no client item data (bonding null) as ``output_bonding_unknown``.  Both are decided
  before any pricing, after the check that the item was scanned at all.

Strategy-only parameters ``min_margin``, ``max_spend_share`` and ``capacity_share`` are not
applied here; ``share`` is the analogue of ``capacity_share``.  Recipes with random yields are
excluded as ``random_yield`` (the trading loader leaves them out of the engine's recipes).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from fractions import Fraction
from typing import Any, Protocol

import psycopg

from crowley.analysis.books import Book, Interval, load_commodity_books, valid_intervals
from crowley.analysis.common import jsonable
from crowley.analysis.item_books import (
    ItemBook,
    ItemInterval,
    cheapest_cost,
    floor_price,
    item_fair,
    item_intervals,
    load_item_books,
    replay_item_listing,
)
from crowley.analysis.metrics import FAIR_MIN_BOOKS, demand_metrics, fair_value
from crowley.analysis.time_to_sale import HORIZON, replay_many
from crowley.core.auction_rules import AuctionHouseRules, rules_for_house
from crowley.core.errors import MarketDataNotFound
from crowley.core.house import House
from crowley.trading.account_gate import AccountSnapshot, classify_recipe, load_account_snapshot
from crowley.trading.api import ItemFacts, ItemSnapshot, Level, Recipe
from crowley.trading.data import load_facts, load_recipes
from crowley.trading.fees import deposit
from crowley.trading.numbers import as_fraction
from crowley.trading.strategies.common import (
    MIN_DEMAND_ELIGIBLE_SECONDS,
    fraction_parameter,
    trustworthy_fair,
)
from crowley.trading.strategies.crafting import DEFAULT_PARAMS as CRAFTING_PARAMS
from crowley.trading.strategies.pricing import (
    Source,
    output_target_price,
    recipe_requirements,
    source_reagent,
)

DEFAULT_PROFESSIONS = frozenset({"alchemy", "tailoring", "engineering"})
SECONDS_PER_HOUR = 3600
BIND_ON_PICKUP = 1
TRADEABLE_BONDING = frozenset({0, 2, 3})  # none, bind on equip, bind on use (client_items.bonding)
# Every reason a recipe can be excluded for; _Excluded rejects anything else.
EXCLUSION_REASONS = (
    "insufficient_independent_evidence",
    "invalid_output_quantity",
    "no_outputs",
    "output_bind_on_pickup",
    "output_bonding_unknown",
    "output_fair_unavailable",
    "output_fair_untrusted",
    "output_item_variants",
    "output_no_current_book",
    "output_no_observed_demand",
    "output_not_commodity",
    "output_not_in_plan",
    "random_yield",
    "reagent_item_variants",
    "reagent_unpriceable",
)
HISTORY_HOURS = 24  # extra history loaded so early starts have a full 24 h fair-value window
DEFAULT_MIN_FAIR_N = int(fraction_parameter(CRAFTING_PARAMS, "min_fair_n"))
DEFAULT_MAX_BAND_WIDTH = float(fraction_parameter(CRAFTING_PARAMS, "max_band_width"))


@dataclass(frozen=True)
class RankConfig:
    professions: frozenset[str] = DEFAULT_PROFESSIONS
    lookback_hours: float = 24.0
    horizon_hours: float = 2.0
    min_sale_prob: float = 0.5
    share: Fraction = Fraction(1, 4)
    batch_crafts: int = 10
    listing_duration_hours: int = 24
    include_all: bool = False
    min_fair_n: int = DEFAULT_MIN_FAIR_N
    max_band_width: float = DEFAULT_MAX_BAND_WIDTH
    min_demand_hours: float = MIN_DEMAND_ELIGIBLE_SECONDS / SECONDS_PER_HOUR
    min_independent_starts: int = 4
    min_fair_books: int = FAIR_MIN_BOOKS
    pricing: Mapping[str, object] = field(default_factory=lambda: dict(CRAFTING_PARAMS))

    def __post_init__(self) -> None:
        if not self.professions:
            raise ValueError("at least one profession is required")
        if self.lookback_hours <= 0:
            raise ValueError("lookback_hours must be positive")
        if not 0 < self.horizon_hours * SECONDS_PER_HOUR <= HORIZON:
            raise ValueError(f"horizon_hours must be in (0, {HORIZON // SECONDS_PER_HOUR}]")
        if not 0 <= self.min_sale_prob <= 1:
            raise ValueError("min_sale_prob must be in [0, 1]")
        if not 0 < self.share <= 1:
            raise ValueError("share must be in (0, 1]")
        if self.batch_crafts < 1:
            raise ValueError("batch_crafts must be at least 1")
        if self.min_independent_starts < 1:
            raise ValueError("min_independent_starts must be at least 1")

    @property
    def horizon_seconds(self) -> int:
        return round(self.horizon_hours * SECONDS_PER_HOUR)


@dataclass(frozen=True)
class RankInputs:
    house_key: str
    as_of_t: float
    latest_sweep_id: int | str
    recipes: Sequence[Recipe]
    books: Mapping[int, Sequence[Book]]
    facts: Mapping[int, ItemFacts]
    rules: AuctionHouseRules
    account: AccountSnapshot | None = None
    # Teaching item id -> cheapest unit price listed in the latest sweep.  A present key
    # means the item was observed in that sweep (None: observed, nothing listed); an absent
    # key means it was not observed at all.
    teaching_prices: Mapping[int, int | None] = field(default_factory=dict)
    # Item id -> observation kind ("commodity" | "item") for items ever observed for this
    # house and build; absent means never scanned.  Only used to explain missing books.
    observed_kinds: Mapping[int, str] = field(default_factory=dict)
    # Spells of the requested professions whose outputs are not fixed-yield.
    random_yield_spells: tuple[int, ...] = ()
    # Spells of the requested professions that have no client output row at all.
    no_output_spells: tuple[int, ...] = ()
    # Professions of the two spell lists above (for the per-profession breakdown only).
    excluded_spell_professions: Mapping[int, frozenset[str]] = field(default_factory=dict)
    # Item id -> full item-kind books.  None: item-kind data was not loaded, so item-kind
    # outputs are excluded as output_not_commodity (the commodity-only behaviour).
    item_books: Mapping[int, Sequence[ItemBook]] | None = None


@dataclass(frozen=True)
class TradeRow:
    spell_id: int
    professions: tuple[str, ...]
    outputs: tuple[tuple[int, int], ...]
    reagents: tuple[tuple[int, int, str], ...]  # item id, units per craft, "ah" | "vendor"
    target_prices: tuple[tuple[int, int], ...]  # output item id, copper
    reagent_cost_per_craft: Fraction
    net_revenue_per_craft: Fraction
    margin_per_craft: Fraction
    sale_probability: float
    usable_starts: int  # replay starts with a fair value and a determined outcome
    independent_starts: int  # of those, at least one horizon apart (the evidence for P)
    sold_starts: int  # independent starts that sold within the horizon
    target_above_lowest: bool  # a target above the current lowest listing: expect a wait
    market_units_per_hour: Fraction  # limiting output
    crafts_per_hour: Fraction
    profit_per_hour: Fraction
    capital_per_batch: Fraction
    status: str | None = None
    recipe_price_state: str | None = None  # listed | not_listed | not_observed | trainer
    recipe_price: int | None = None
    recipe_item_id: int | None = None
    payback_crafts: Fraction | None = None
    payback_hours: Fraction | None = None
    kind: str = "commodity"  # "commodity" | "item" | "mixed" (a multi-output recipe of both)


@dataclass(frozen=True)
class SaleEstimate:
    probability: float
    usable_starts: int
    independent_starts: int
    sold_starts: int


@dataclass(frozen=True)
class Exclusion:
    spell_id: int
    reason: str
    detail: str
    professions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evaluation:
    rows: tuple[TradeRow, ...]
    excluded: tuple[Exclusion, ...]
    considered: int

    def __post_init__(self) -> None:
        if self.considered != len(self.rows) + len(self.excluded):
            raise RuntimeError(
                f"{self.considered} recipes considered but {len(self.rows)} evaluated and "
                f"{len(self.excluded)} excluded"
            )

    def exclusion_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.excluded).items()))

    def exclusion_counts_by_profession(self) -> dict[str, dict[str, int]]:
        """Reason counts per profession; a recipe of two professions counts under each."""

        counts: dict[str, Counter[str]] = {}
        for item in self.excluded:
            for profession in item.professions or ("(unattributed)",):
                counts.setdefault(profession, Counter())[item.reason] += 1
        return {name: dict(sorted(counter.items())) for name, counter in sorted(counts.items())}


class _Excluded(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)
        if reason not in EXCLUSION_REASONS:
            raise ValueError(f"unregistered exclusion reason {reason!r}")
        self.reason = reason
        self.detail = detail


def _levels(book: Book) -> tuple[Level, ...]:
    by_price: dict[int, int] = {}
    for group in book.groups:
        if group.unit.denominator != 1:
            raise ValueError(f"commodity {book.item_id} has a non-integral unit price {group.unit}")
        by_price[int(group.unit)] = by_price.get(int(group.unit), 0) + group.quantity
    return tuple(Level(price, by_price[price]) for price in sorted(by_price))


def levels_snapshot(item_id: int, book: Book) -> ItemSnapshot:
    """A snapshot carrying only the observed levels; statistics are unavailable."""

    levels = _levels(book)
    return ItemSnapshot(
        item_id=item_id,
        t=int(book.t),
        levels=levels,
        visible_min=levels[0].unit_price if levels else None,
        visible_units=sum(level.quantity for level in levels),
        fair=None,
        fair_band=None,
        fair_n=0,
        trend_per_day=None,
        demand_units_per_day=None,
        demand_eligible_seconds=0.0,
        volatility_per_hour=None,
        half_life_hours=None,
    )


def snapshot_from_books(item_id: int, books: Sequence[Book]) -> ItemSnapshot:
    """Snapshot at the last book, with strictly-past 24 h fair value and bottom-walk demand."""

    base = levels_snapshot(item_id, books[-1])
    fair = fair_value(books)
    band = fair["band"]
    demand = demand_metrics(books)
    return ItemSnapshot(
        item_id=item_id,
        t=base.t,
        levels=base.levels,
        visible_min=base.visible_min,
        visible_units=base.visible_units,
        fair=fair["value"],
        fair_band=None if band["p25"] is None or band["p75"] is None else (band["p25"], band["p75"]),
        fair_n=fair["n"],
        trend_per_day=fair["trend_per_day"],
        demand_units_per_day=demand["purchased_units_per_day"],
        demand_eligible_seconds=demand["eligible_covered_seconds"],
        volatility_per_hour=None,
        half_life_hours=None,
    )


def horizon_observable(
    books: Sequence[Book], intervals: Sequence[Interval | None], start: int, price: int, horizon: float
) -> bool:
    """Whether ``[t, t + horizon]`` from book ``start`` has no chain break or unobservable interval."""

    end_t = books[start].t + horizon
    end = next((index for index in range(start, len(books)) if books[index].t >= end_t), None)
    if end is None:
        return False
    for interval in intervals[start:end]:
        if interval is None:
            return False
        if interval.ceiling is not None and (price > interval.ceiling or interval.walk.reached_ceiling):
            return False
    return True


class _Timed(Protocol):
    @property
    def t(self) -> float: ...


def _history[B: _Timed](books: Sequence[B], inputs: RankInputs, config: RankConfig) -> list[B]:
    start = inputs.as_of_t - (config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR
    return [book for book in books if start <= book.t <= inputs.as_of_t]


def _window[B: _Timed](books: Sequence[B], inputs: RankInputs, config: RankConfig) -> list[B]:
    start = inputs.as_of_t - config.lookback_hours * SECONDS_PER_HOUR
    return [book for book in books if start <= book.t <= inputs.as_of_t]


def item_levels_snapshot(item_id: int, book: ItemBook, item_key: str | None) -> ItemSnapshot:
    """A snapshot of one item-kind book's auctions as price levels; statistics are unavailable."""

    by_price: dict[int, int] = {}
    for auction in book.scoped(item_key):
        if auction.unit.denominator != 1:
            raise ValueError(f"item {item_id} has a non-integral buyout {auction.unit}")
        by_price[int(auction.unit)] = by_price.get(int(auction.unit), 0) + auction.quantity
    levels = tuple(Level(price, by_price[price]) for price in sorted(by_price))
    return ItemSnapshot(
        item_id=item_id,
        t=int(book.t),
        levels=levels,
        visible_min=levels[0].unit_price if levels else None,
        visible_units=sum(level.quantity for level in levels),
        fair=None,
        fair_band=None,
        fair_n=0,
        trend_per_day=None,
        demand_units_per_day=None,
        demand_eligible_seconds=0.0,
        volatility_per_hour=None,
        half_life_hours=None,
    )


def item_snapshot_from_books(
    item_id: int,
    books: Sequence[ItemBook],
    floors: Sequence[Fraction | None],
    intervals: Sequence[ItemInterval | None],
    item_key: str | None,
    min_books: int,
) -> ItemSnapshot:
    """Item-kind snapshot at the last book: strictly-past fair value of the floor and walk demand.

    ``intervals`` are the usable intervals among ``books`` (one slot per adjacent pair).
    """

    base = item_levels_snapshot(item_id, books[-1], item_key)
    fair = item_fair(books, floors, len(books) - 1, min_books=min_books)
    band = fair["band"]
    usable = [interval for interval in intervals if interval is not None]
    seconds = sum(interval.exposure for interval in usable)
    units = sum(units for interval in usable for _, units in interval.consumed)
    return replace(
        base,
        fair=fair["value"],
        fair_band=None if band["p25"] is None or band["p75"] is None else (band["p25"], band["p75"]),
        fair_n=fair["n"],
        demand_units_per_day=Fraction(units * 86400, seconds) if seconds else None,
        demand_eligible_seconds=float(seconds),
    )


def _independent_estimate(determined: Sequence[tuple[float, bool]], horizon: float) -> SaleEstimate:
    """P(sale) over a greedy subset of determined starts at least one horizon apart."""

    independent = sold = 0
    last: float | None = None
    for start_t, was_sold in determined:
        if last is None or start_t >= last + horizon:
            last = start_t
            independent += 1
            sold += was_sold
    return SaleEstimate(sold / independent if independent else 0.0, len(determined), independent, sold)


def batch_net_revenue(
    facts: ItemFacts, rules: AuctionHouseRules, quantity: int, unit_price: Fraction | int, duration_hours: int
) -> tuple[Fraction, int]:
    """Net proceeds of listing ``quantity`` units at ``unit_price`` (after the AH cut and the
    deposit, charged in full) and the deposit itself.  The one revenue formula of the rank views."""

    batch_deposit = deposit(facts.sell_price, quantity, duration_hours, rules)
    return quantity * unit_price * (1 - rules.seller_cut) - batch_deposit, batch_deposit


class RankEvaluator:
    def __init__(self, inputs: RankInputs, config: RankConfig) -> None:
        self.inputs = inputs
        self.config = config
        self._windows: dict[int, list[Book]] = {}
        self._outputs: dict[int, ItemSnapshot] = {}
        self._estimates: dict[tuple[int, int], SaleEstimate] = {}
        self._kinds: dict[int, str] = {}
        self._demand_notes: dict[int, str] = {}
        self._item_keys: dict[int, str | None] = {}
        self._item_series: dict[
            int, tuple[list[ItemBook], list[Fraction | None], list[ItemInterval | None]]
        ] = {}

    def window(self, item_id: int) -> list[Book] | None:
        if item_id not in self._windows:
            books = self.inputs.books.get(item_id)
            window = [] if books is None else _window(books, self.inputs, self.config)
            self._windows[item_id] = window
        window = self._windows[item_id]
        # Only an item observed in the latest sweep has a current book.
        if not window or window[-1].sweep_id != self.inputs.latest_sweep_id:
            return None
        return window

    def history(self, item_id: int) -> list[Book]:
        return _history(self.inputs.books.get(item_id, ()), self.inputs, self.config)

    def item_series(
        self, item_id: int
    ) -> tuple[list[ItemBook], list[Fraction | None], list[ItemInterval | None]]:
        """Item-kind history books, their floors and usable intervals (one variant, cached)."""

        if item_id not in self._item_series:
            books = _history((self.inputs.item_books or {}).get(item_id, ()), self.inputs, self.config)
            keys = sorted({key for book in books for key in book.item_keys})
            if len(keys) > 1:
                raise _Excluded("output_item_variants", f"item {item_id} books carry itemKeys {keys}")
            key = keys[0] if keys else None
            self._item_keys[item_id] = key
            floors = [floor_price(book, key) for book in books]
            self._item_series[item_id] = (books, floors, item_intervals(books, key))
        return self._item_series[item_id]

    def reagent_source(self, item_id: int, quantity: int) -> Source | None:
        window = self.window(item_id)
        snapshot = None if window is None else levels_snapshot(item_id, window[-1])
        if window is None:
            latest = [
                book
                for book in (self.inputs.item_books or {}).get(item_id, ())
                if book.sweep_id == self.inputs.latest_sweep_id
            ]
            if latest:
                keys = sorted(latest[-1].item_keys)
                if len(keys) > 1:
                    raise _Excluded(
                        "reagent_item_variants", f"reagent {item_id} book carries itemKeys {keys}"
                    )
                return self._item_reagent_source(item_id, quantity, latest[-1])
        return source_reagent(snapshot, self.inputs.facts.get(item_id), item_id, quantity)

    def _item_reagent_source(self, item_id: int, quantity: int, book: ItemBook) -> Source | None:
        """Exact sum of the cheapest buyable auctions (not ours) versus vendor; a tie goes to the AH."""

        vendor = source_reagent(None, self.inputs.facts.get(item_id), item_id, quantity)
        ah = cheapest_cost(book, None, quantity)
        if ah is not None and (vendor is None or ah[0] <= vendor.full_cost):
            return Source(item_id, quantity, "ah", ah[1], ah[0])
        return vendor

    def output_kind(self, item_id: int) -> str:
        """``"commodity"`` or ``"item"``; an output that was never scanned is excluded."""

        if self.inputs.books.get(item_id):
            return "commodity"
        kind = self.inputs.observed_kinds.get(item_id)
        if kind is None:
            raise _Excluded("output_not_in_plan", f"item {item_id} was never scanned for this house/build")
        return kind

    def check_tradeable(self, item_id: int) -> None:
        """Bind-on-pickup outputs cannot be auctioned; unknown bonding is never assumed tradeable."""

        facts = self.inputs.facts.get(item_id)
        if facts is None:
            raise _Excluded("output_bonding_unknown", f"item {item_id} has no client item data")
        if facts.bonding == BIND_ON_PICKUP:
            raise _Excluded("output_bind_on_pickup", f"item {item_id} is bind on pickup")
        if facts.bonding not in TRADEABLE_BONDING:
            raise ValueError(f"item {item_id} has unrecognised client bonding {facts.bonding}")

    def output_snapshot(self, item_id: int) -> ItemSnapshot:
        if item_id in self._outputs:
            return self._outputs[item_id]
        kind = self.output_kind(item_id)
        self.check_tradeable(item_id)
        self._kinds[item_id] = kind
        if kind == "item" and self.inputs.item_books is not None:
            snapshot = self._item_output_snapshot(item_id)
        else:
            window = self.window(item_id)
            if window is None:
                raise self._missing_book(item_id)
            snapshot = snapshot_from_books(item_id, window)
        return self._trusted(item_id, snapshot)

    def _item_output_snapshot(self, item_id: int) -> ItemSnapshot:
        books, floors, intervals = self.item_series(item_id)
        window = _window(books, self.inputs, self.config)
        if not window or window[-1].sweep_id != self.inputs.latest_sweep_id:
            raise _Excluded(
                "output_no_current_book", f"item {item_id} has no full item-kind book in the latest sweep"
            )
        offset = len(books) - len(window)
        usable = sum(interval is not None for interval in intervals[offset:])
        self._demand_notes[item_id] = (
            f" ({usable} of {len(window) - 1} window intervals usable for item-kind sale evidence)"
        )
        return item_snapshot_from_books(
            item_id,
            window,
            floors[offset:],
            intervals[offset:],
            self._item_keys[item_id],
            self.config.min_fair_books,
        )

    def _trusted(self, item_id: int, snapshot: ItemSnapshot) -> ItemSnapshot:
        if snapshot.fair is None:
            raise _Excluded(
                "output_fair_unavailable",
                f"item {item_id}: {snapshot.fair_n} strictly-past reference books in window",
            )
        if snapshot.demand_units_per_day is None or snapshot.demand_units_per_day <= 0:
            raise _Excluded(
                "output_no_observed_demand",
                f"item {item_id} shows no bottom-walk demand{self._demand_notes.get(item_id, '')}",
            )
        if not trustworthy_fair(
            snapshot,
            min_fair_n=self.config.min_fair_n,
            max_band_width=self.config.max_band_width,
            min_demand_eligible_seconds=self.config.min_demand_hours * SECONDS_PER_HOUR,
        ):
            raise _Excluded(
                "output_fair_untrusted",
                f"item {item_id}: fair_n={snapshot.fair_n} (need {self.config.min_fair_n}), "
                f"band={snapshot.fair_band}, covered={snapshot.demand_eligible_seconds:.0f}s",
            )
        self._outputs[item_id] = snapshot
        return snapshot

    def _missing_book(self, item_id: int) -> _Excluded:
        if self.inputs.books.get(item_id):
            return _Excluded(
                "output_no_current_book", f"item {item_id} was observed earlier but not in the latest sweep"
            )
        kind = self.inputs.observed_kinds[item_id]  # output_kind() already required it
        if kind != "commodity":
            return _Excluded(
                "output_not_commodity",
                f"item {item_id} is scanned as kind {kind!r} and this input set carries no item-kind data",
            )
        return _Excluded("output_no_current_book", f"commodity {item_id} has no book in the lookback window")

    def sale_estimate(self, item_id: int, quantity: int) -> SaleEstimate:
        key = (item_id, quantity)
        if key in self._estimates:
            return self._estimates[key]
        if self._kinds[item_id] == "item" and self.inputs.item_books is not None:
            estimate = self._item_sale_estimate(item_id, quantity)
        else:
            estimate = self._commodity_sale_estimate(item_id, quantity)
        self._estimates[key] = estimate
        return estimate

    def _item_sale_estimate(self, item_id: int, quantity: int) -> SaleEstimate:
        history, floors, intervals = self.item_series(item_id)
        horizon = self.config.horizon_seconds
        first_start_t = self.inputs.as_of_t - self.config.lookback_hours * SECONDS_PER_HOUR
        item_key = self._item_keys[item_id]
        determined: list[tuple[float, bool]] = []
        for index, book in enumerate(history):
            if book.t < first_start_t:
                continue  # earlier books only supply fair-value history and replay context
            fair = item_fair(history, floors, index, min_books=self.config.min_fair_books)["value"]
            if fair is None:
                continue
            snapshot = replace(item_levels_snapshot(item_id, book, item_key), fair=fair)
            price = output_target_price(snapshot, self.config.pricing)
            sold = replay_item_listing(history, intervals, index, price, quantity, horizon)
            if sold is not None:
                determined.append((book.t, sold))
        return _independent_estimate(determined, horizon)

    def _commodity_sale_estimate(self, item_id: int, quantity: int) -> SaleEstimate:
        assert self.window(item_id) is not None
        history = self.history(item_id)
        horizon = self.config.horizon_seconds
        first_start_t = self.inputs.as_of_t - self.config.lookback_hours * SECONDS_PER_HOUR
        intervals = valid_intervals(history)
        starts: list[tuple[int, int]] = []
        for index, book in enumerate(history):
            if book.t < first_start_t:
                continue  # earlier books only supply fair-value history and replay context
            fair = fair_value(history, index, include_trend=False, min_books=self.config.min_fair_books)[
                "value"
            ]
            if fair is None:
                continue
            snapshot = replace(levels_snapshot(item_id, book), fair=fair)
            starts.append((index, output_target_price(snapshot, self.config.pricing)))
        results = replay_many(history, starts, quantity)
        determined: list[tuple[float, bool]] = []  # (start time, sold within the horizon)
        for (index, price), result in zip(starts, results, strict=True):
            # Eligibility is decided before, and independently of, the outcome: the whole
            # [t, t + horizon] must lie in one unbroken, fully observable run of books.
            if not horizon_observable(history, intervals, index, price, horizon):
                continue
            sold_in_time = result.event_time is not None and result.event_time <= horizon
            determined.append((history[index].t, sold_in_time))
        return _independent_estimate(determined, horizon)

    def evaluate(self, recipe: Recipe) -> TradeRow:
        config = self.config
        rules = self.inputs.rules
        batch = config.batch_crafts

        outputs: dict[int, int] = {}
        for item_id, quantity in recipe.outputs:
            outputs[item_id] = outputs.get(item_id, 0) + quantity
        if not outputs:
            raise _Excluded("no_outputs", "recipe has no outputs")
        bad = sorted(item_id for item_id, quantity in outputs.items() if quantity <= 0)
        if bad:
            raise _Excluded("invalid_output_quantity", f"non-positive output quantity for items {bad}")
        snapshots = {item_id: self.output_snapshot(item_id) for item_id in sorted(outputs)}

        sources: list[Source] = []
        for item_id, quantity in recipe_requirements(recipe, batch).items():
            source = self.reagent_source(item_id, quantity)
            if source is None:
                raise _Excluded(
                    "reagent_unpriceable",
                    f"reagent {item_id} x{quantity} has neither an AH fill nor a vendor price",
                )
            sources.append(source)
        reagent_cost = sum((source.full_cost for source in sources), Fraction(0))

        targets: dict[int, int] = {}
        revenue = Fraction(0)
        deposits = Fraction(0)
        limiting: SaleEstimate | None = None
        above_lowest = False
        for item_id, snapshot in snapshots.items():
            target = output_target_price(snapshot, config.pricing)
            targets[item_id] = target
            quantity = outputs[item_id] * batch
            net, batch_deposit = batch_net_revenue(
                self.inputs.facts[item_id], rules, quantity, target, config.listing_duration_hours
            )
            revenue += net
            deposits += batch_deposit
            estimate = self.sale_estimate(item_id, quantity)
            if estimate.independent_starts < config.min_independent_starts:
                raise _Excluded(
                    "insufficient_independent_evidence",
                    f"item {item_id}: {estimate.independent_starts} independent starts "
                    f"({estimate.usable_starts} usable) over {config.horizon_hours} h "
                    f"(need {config.min_independent_starts})",
                )
            if limiting is None or estimate.probability < limiting.probability:
                limiting = estimate
            if snapshot.visible_min is not None and target > snapshot.visible_min:
                above_lowest = True
        assert limiting is not None

        crafts_per_hour: Fraction | None = None
        limiting_units = Fraction(0)
        for item_id, snapshot in snapshots.items():
            assert snapshot.demand_units_per_day is not None
            units_per_hour = as_fraction(snapshot.demand_units_per_day) / 24
            rate = config.share * units_per_hour / outputs[item_id]
            if crafts_per_hour is None or rate < crafts_per_hour:
                crafts_per_hour, limiting_units = rate, units_per_hour
        assert crafts_per_hour is not None

        margin = (revenue - reagent_cost) / batch
        kinds = {self._kinds[item_id] for item_id in snapshots}
        kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        return TradeRow(
            spell_id=recipe.spell_id,
            professions=tuple(sorted(recipe.professions)),
            outputs=tuple(sorted(outputs.items())),
            reagents=tuple((source.item_id, source.quantity // batch, source.kind) for source in sources),
            target_prices=tuple(sorted(targets.items())),
            reagent_cost_per_craft=reagent_cost / batch,
            net_revenue_per_craft=revenue / batch,
            margin_per_craft=margin,
            sale_probability=limiting.probability,
            usable_starts=limiting.usable_starts,
            independent_starts=limiting.independent_starts,
            sold_starts=limiting.sold_starts,
            target_above_lowest=above_lowest,
            market_units_per_hour=limiting_units,
            crafts_per_hour=crafts_per_hour,
            profit_per_hour=margin * crafts_per_hour,
            capital_per_batch=reagent_cost + deposits,
            kind=kind,
        )

    def annotate(self, row: TradeRow, recipe: Recipe) -> TradeRow:
        account = self.inputs.account
        if account is None:
            return row
        status = classify_recipe(recipe, account).status
        fields: dict[str, Any] = {"status": status}
        if status != "known" and recipe.acquisition.teaching_item_ids:
            teaching = self.inputs.teaching_prices
            observed = [item_id for item_id in recipe.acquisition.teaching_item_ids if item_id in teaching]
            priced = [(price, item_id) for item_id in observed if (price := teaching[item_id]) is not None]
            if priced:
                price, item_id = min(priced)
                fields.update(recipe_price_state="listed", recipe_price=price, recipe_item_id=item_id)
                if row.margin_per_craft > 0:
                    crafts = Fraction(price) / row.margin_per_craft
                    fields["payback_crafts"] = crafts
                    fields["payback_hours"] = crafts / row.crafts_per_hour
            else:
                fields["recipe_price_state"] = "not_listed" if observed else "not_observed"
        elif status != "known":
            fields["recipe_price_state"] = "trainer"
        return replace(row, **fields)


def evaluate_trades(inputs: RankInputs, config: RankConfig) -> Evaluation:
    """Evaluate every recipe in the requested professions; nothing is dropped silently."""

    known_professions = {profession for recipe in inputs.recipes for profession in recipe.professions}
    missing = sorted(config.professions - known_professions)
    if missing:
        raise ValueError(f"no client recipes for profession(s) {missing}; known: {sorted(known_professions)}")
    evaluator = RankEvaluator(inputs, config)
    rows: list[TradeRow] = []
    excluded: list[Exclusion] = []
    considered = len(inputs.random_yield_spells) + len(inputs.no_output_spells)
    lookup = inputs.excluded_spell_professions
    for spell_id in inputs.random_yield_spells:
        excluded.append(
            Exclusion(
                spell_id,
                "random_yield",
                "recipe output is not a fixed yield",
                tuple(sorted(lookup.get(spell_id, ()))),
            )
        )
    for spell_id in inputs.no_output_spells:
        excluded.append(
            Exclusion(
                spell_id,
                "no_outputs",
                "recipe has no client output row",
                tuple(sorted(lookup.get(spell_id, ()))),
            )
        )
    for recipe in sorted(inputs.recipes, key=lambda entry: entry.spell_id):
        if not recipe.professions & config.professions:
            continue
        considered += 1
        try:
            rows.append(evaluator.annotate(evaluator.evaluate(recipe), recipe))
        except _Excluded as exc:
            excluded.append(
                Exclusion(
                    recipe.spell_id,
                    exc.reason,
                    exc.detail,
                    tuple(sorted(recipe.professions & config.professions)),
                )
            )
    return Evaluation(tuple(rows), tuple(excluded), considered)


def select_rows(rows: Sequence[TradeRow], config: RankConfig) -> tuple[TradeRow, ...]:
    """Apply the sale-probability filter (unless ``include_all``) and sort by profit/h."""

    kept = [
        row
        for row in rows
        if config.include_all or (row.sale_probability >= config.min_sale_prob and row.profit_per_hour > 0)
    ]
    return tuple(sorted(kept, key=lambda row: (-row.profit_per_hour, row.spell_id)))


def rank_trades(inputs: RankInputs, config: RankConfig) -> tuple[TradeRow, ...]:
    return select_rows(evaluate_trades(inputs, config).rows, config)


# -- database loading ------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedInputs:
    inputs: RankInputs
    client_build: str
    names: Mapping[int, str]  # display only


def latest_sweep(
    conn: Any, house_key: str, client_build: str | None, as_of: datetime | None
) -> tuple[int, float, str]:
    row = conn.execute(
        """
        SELECT ms.id, EXTRACT(EPOCH FROM ms.finished_at), ms.client_version || '.' || ms.client_build
        FROM market_sweeps AS ms
        JOIN houses AS h ON h.id = ms.house_id
        WHERE h.key = %s
          AND (%s::text IS NULL OR ms.client_version || '.' || ms.client_build = %s)
          AND (%s::timestamptz IS NULL OR ms.finished_at <= %s)
        ORDER BY ms.finished_at DESC, ms.id DESC
        LIMIT 1
        """,
        (house_key, client_build, client_build, as_of, as_of),
    ).fetchone()
    if row is None:
        raise MarketDataNotFound(
            f"no market sweep for house {house_key!r} build {client_build!r} as of {as_of}"
        )
    return int(row[0]), float(row[1]), str(row[2])


def _teaching_prices(conn: Any, sweep_id: int, item_ids: Sequence[int]) -> dict[int, int | None]:
    """Cheapest unit price per teaching item in one sweep (item-kind goods, keyed by id).

    Reads ``market_observations``/``market_captured_rows``: an item-kind observation with
    ``coverage = 'full'`` in the sweep means the item was observed; its rows' unit price is
    the buyout of one auction (non-commodity results group identical one-unit auctions, so
    ``quantity`` is availability, not a stack size; migration 003 sets ``unit_numerator`` to it).
    """

    if not item_ids:
        return {}
    observed = conn.execute(
        """
        SELECT mo.item_id, MIN(r.unit_numerator::numeric / r.unit_denominator)
        FROM market_observations AS mo
        LEFT JOIN market_captured_rows AS r
          ON r.sweep_id = mo.sweep_id AND r.item_id = mo.item_id
        WHERE mo.sweep_id = %s AND mo.kind = 'item' AND mo.coverage = 'full'
          AND mo.item_id = ANY(%s)
        GROUP BY mo.item_id
        """,
        (sweep_id, list(item_ids)),
    ).fetchall()
    result: dict[int, int | None] = {}
    for item_id, price in observed:
        if price is not None and price != int(price):
            raise ValueError(f"item {item_id} has a non-integral buyout {price}")
        result[int(item_id)] = None if price is None else int(price)
    return result


def _observed_kinds(conn: Any, house_key: str, client_build: str, item_ids: Sequence[int]) -> dict[int, str]:
    """Observation kind per item ever scanned for this house and build."""

    rows = conn.execute(
        """
        SELECT DISTINCT mo.item_id, mo.kind
        FROM market_observations AS mo
        JOIN market_sweeps AS ms ON ms.id = mo.sweep_id
        JOIN houses AS h ON h.id = ms.house_id
        WHERE h.key = %s AND ms.client_version || '.' || ms.client_build = %s
          AND mo.kind IN ('commodity', 'item') AND mo.item_id = ANY(%s)
        """,
        (house_key, client_build, list(item_ids)),
    ).fetchall()
    kinds: dict[int, str] = {}
    for item_id, kind in rows:
        if kinds.setdefault(int(item_id), str(kind)) != kind:
            raise ValueError(f"item {item_id} was observed as both commodity and item")
    return kinds


def _excluded_spells(
    conn: Any, client_build: str, professions: frozenset[str], loaded: frozenset[int]
) -> tuple[tuple[int, ...], tuple[int, ...], dict[int, frozenset[str]]]:
    """Requested-profession spells the trading loader does not return, each with its reason.

    Returns (random-yield spells, spells with no client output row, their professions).  Every
    spell of the professions must be either loaded or one of these; anything else is a loader
    gap and fails loudly rather than vanishing from the accounting.
    """

    rows = conn.execute(
        """
        SELECT s.spell_id, array_agg(DISTINCT s.profession), r.spell_id IS NOT NULL,
               EXISTS (SELECT 1 FROM client_recipe_outputs o
                       WHERE o.export_id = e.id AND o.spell_id = s.spell_id),
               EXISTS (SELECT 1 FROM client_recipe_outputs o
                       WHERE o.export_id = e.id AND o.spell_id = s.spell_id AND o.yield_kind <> 'fixed')
        FROM latest_client_export AS e
        JOIN client_recipe_skill_lines AS s ON s.export_id = e.id
        LEFT JOIN client_recipes AS r ON r.export_id = e.id AND r.spell_id = s.spell_id
        WHERE e.client_build = %s AND s.profession = ANY(%s)
        GROUP BY s.spell_id, r.spell_id, e.id
        ORDER BY s.spell_id
        """,
        (client_build, sorted(professions)),
    ).fetchall()
    random_yield: list[int] = []
    no_output: list[int] = []
    attribution: dict[int, frozenset[str]] = {}
    seen: set[int] = set()
    for raw_spell, spell_professions, in_recipes, has_output, has_random in rows:
        spell_id = int(raw_spell)
        seen.add(spell_id)
        if not in_recipes:
            raise RuntimeError(f"skill-line spell {spell_id} has no client_recipes row")
        if has_random:
            random_yield.append(spell_id)
        elif not has_output:
            no_output.append(spell_id)
        elif spell_id not in loaded:
            raise RuntimeError(f"fixed-yield spell {spell_id} was not returned by the recipe loader")
        else:
            continue
        attribution[spell_id] = frozenset(spell_professions)
    unknown = sorted(loaded - seen)
    if unknown:
        raise RuntimeError(f"loaded recipes {unknown} have no skill line in the requested professions")
    return tuple(random_yield), tuple(no_output), attribution


def item_names(conn: Any, client_build: str, item_ids: Sequence[int]) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT i.item_id, i.name
        FROM client_items AS i
        JOIN latest_client_export AS e ON e.id = i.export_id
        WHERE e.client_build = %s AND i.item_id = ANY(%s) AND i.name IS NOT NULL
        """,
        (client_build, list(item_ids)),
    ).fetchall()
    return {int(item_id): str(name) for item_id, name in rows}


def load_rank_inputs(
    conn: Any,
    house_key: str,
    config: RankConfig,
    *,
    client_build: str | None = None,
    character: str | None = None,
    as_of: datetime | None = None,
) -> LoadedInputs:
    """Read everything ``evaluate_trades`` needs for one house and build (read-only)."""

    house = House.parse(house_key)
    sweep_id, finished_t, build = latest_sweep(conn, house_key, client_build, as_of)
    facts = load_facts(conn, house_key, build)  # raises when there is no export for the build
    recipes = tuple(
        recipe for recipe in load_recipes(conn, house_key, build) if recipe.professions & config.professions
    )
    books = load_commodity_books(
        conn,
        house_key,
        start_t=int(finished_t - (config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR),
        end_t=int(finished_t),
        client_build=build,
    )
    account = None if character is None else load_account_snapshot(conn, house_key, character, as_of=as_of)
    teaching_ids = sorted(
        {item_id for recipe in recipes for item_id in recipe.acquisition.teaching_item_ids}
        if account is not None
        else ()
    )
    teaching_prices = _teaching_prices(conn, sweep_id, teaching_ids)
    display_ids = sorted(
        {
            item_id
            for recipe in recipes
            for item_id in (
                *(i for i, _ in recipe.outputs),
                *(i for i, _ in recipe.reagents),
                *recipe.acquisition.teaching_item_ids,
            )
        }
    )
    names = item_names(conn, build, display_ids)
    output_ids = sorted({item_id for recipe in recipes for item_id, _ in recipe.outputs})
    reagent_ids = sorted({item_id for recipe in recipes for item_id, _ in recipe.reagents})
    observed_kinds = _observed_kinds(conn, house_key, build, sorted({*output_ids, *reagent_ids}))
    item_outputs = [item_id for item_id in output_ids if observed_kinds.get(item_id) == "item"]
    item_books = load_item_books(
        conn,
        house_key,
        item_outputs,
        client_build=build,
        start_t=int(finished_t - (config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR),
        end_t=int(finished_t),
    )
    # Reagents are priced from the latest sweep's book only.
    item_reagents = [
        item_id
        for item_id in reagent_ids
        if observed_kinds.get(item_id) == "item" and item_id not in item_outputs
    ]
    item_books.update(load_item_books(conn, house_key, item_reagents, client_build=build, sweep_id=sweep_id))
    random_yield, no_output, excluded_professions = _excluded_spells(
        conn, build, config.professions, frozenset(recipe.spell_id for recipe in recipes)
    )
    inputs = RankInputs(
        house_key=house_key,
        as_of_t=finished_t,
        latest_sweep_id=sweep_id,
        recipes=recipes,
        books=books,
        facts=facts,
        rules=rules_for_house(house),
        account=account,
        teaching_prices=teaching_prices,
        observed_kinds=observed_kinds,
        random_yield_spells=random_yield,
        no_output_spells=no_output,
        excluded_spell_professions=excluded_professions,
        item_books=item_books,
    )
    return LoadedInputs(inputs, build, names)


# -- presentation ----------------------------------------------------------------------


def gold(copper: Fraction | int | None) -> str:
    """Compact g/s/c rendering; fractions of a copper are rounded for display only."""

    if copper is None:
        return "-"
    total = round(copper)
    sign = "-" if total < 0 else ""
    gold, rest = divmod(abs(total), 10000)
    silver, cents = divmod(rest, 100)
    return f"{sign}{gold}g{silver:02d}s{cents:02d}c"


def format_table(
    rows: Sequence[TradeRow], evaluation: Evaluation, names: Mapping[int, str], limit: int | None = None
) -> str:
    shown = rows if limit is None else rows[:limit]
    header = (
        f"{'spell':>7} {'kind':<4} {'output (item id)':<38} {'cost':>9} {'margin':>9} {'P(sale)':>7} "
        f"{'sold/ind':>8} "
        f"{'units/h':>8} {'profit/h':>10} {'capital':>10} {'status':<20} {'recipe':>10} {'payback':>8}"
    )
    lines = [header]
    for row in shown:
        label = ", ".join(f"{qty}x {names.get(item, '?')[:22]} ({item})" for item, qty in row.outputs)
        payback = "-" if row.payback_crafts is None else f"{float(row.payback_crafts):.1f}"
        recipe = row.recipe_price_state or "-"
        if row.recipe_price is not None:
            recipe = gold(row.recipe_price)
        lines.append(
            f"{row.spell_id:>7} {row.kind[:4]:<4} {label[:38]:<38} {gold(row.reagent_cost_per_craft):>9} "
            f"{gold(row.margin_per_craft):>9} {row.sale_probability:>7.2f} "
            f"{f'{row.sold_starts}/{row.independent_starts}':>8}{'*' if row.target_above_lowest else ' '}"
            f"{float(row.market_units_per_hour):>8.2f} {gold(row.profit_per_hour):>10} "
            f"{gold(row.capital_per_batch):>10} {(row.status or '-'):<20} {recipe:>10} {payback:>8}"
        )
    counts = evaluation.exclusion_counts()
    lines.append(
        f"shown {len(shown)} of {len(rows)} selected; evaluated {len(evaluation.rows)}, "
        f"excluded {len(evaluation.excluded)} of {evaluation.considered} recipes"
    )
    lines.append(
        "sold/ind: independent starts that sold / all independent starts; * target above lowest listing"
    )
    for reason, count in counts.items():
        lines.append(f"  excluded {reason}: {count}")
    for profession, by_reason in evaluation.exclusion_counts_by_profession().items():
        lines.append(
            f"  {profession}: " + ", ".join(f"{reason} {count}" for reason, count in by_reason.items())
        )
    return "\n".join(lines)


def report_json(
    rows: Sequence[TradeRow], evaluation: Evaluation, names: Mapping[int, str], meta: Mapping[str, Any]
) -> str:
    payload = {
        **meta,
        "rows": [{**asdict(row), "names": {str(i): names.get(i) for i, _ in row.outputs}} for row in rows],
        "excluded": [asdict(item) for item in evaluation.excluded],
        "exclusion_counts": evaluation.exclusion_counts(),
        "exclusion_counts_by_profession": evaluation.exclusion_counts_by_profession(),
        "considered": evaluation.considered,
    }
    return json.dumps(jsonable(payload), indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crowley.analysis rank", description=__doc__)
    parser.add_argument("--database-url", help="defaults to $CROWLEY_DATABASE_URL")
    parser.add_argument("--house", required=True, help="explicit house key, e.g. us-normal-alliance")
    parser.add_argument("--build", help="full client build, e.g. 1.60.1.70862 (default: latest sweep's)")
    parser.add_argument("--character", help="classify recipes against this character's latest capture")
    parser.add_argument("--professions", default=",".join(sorted(DEFAULT_PROFESSIONS)))
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--horizon-hours", type=float, default=2.0)
    parser.add_argument("--min-sale-prob", type=float, default=0.5)
    parser.add_argument("--share", type=float, default=0.25, help="capture share of observed market units/h")
    parser.add_argument("--batch", type=int, default=10, help="crafts per batch")
    parser.add_argument("--min-fair-n", type=int, default=DEFAULT_MIN_FAIR_N)
    parser.add_argument("--max-band-width", type=float, default=DEFAULT_MAX_BAND_WIDTH)
    parser.add_argument(
        "--min-demand-hours", type=float, default=MIN_DEMAND_ELIGIBLE_SECONDS / SECONDS_PER_HOUR
    )
    parser.add_argument("--min-independent-starts", type=int, default=4)
    parser.add_argument("--min-fair-books", type=int, default=FAIR_MIN_BOOKS)
    parser.add_argument("--as-of", help="ISO timestamp; default is the latest sweep")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--all", action="store_true", help="keep rows below the sale-probability threshold")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--candidates",
        action="store_true",
        help="cold-tier promotion candidates (analysis/candidates.py) instead of the ranking",
    )
    parser.add_argument(
        "--fair-lookback-hours",
        type=float,
        help="with --candidates: hours of prior books behind the fair value (default 72; rank uses 24)",
    )
    parser.add_argument(
        "--min-observations",
        type=int,
        help="with --candidates: observations a recipe's outputs need to be flagged (default 4)",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> RankConfig:
    professions = frozenset(part.strip() for part in args.professions.split(",") if part.strip())
    return RankConfig(
        professions=professions,
        lookback_hours=args.lookback_hours,
        horizon_hours=args.horizon_hours,
        min_sale_prob=args.min_sale_prob,
        share=as_fraction(args.share),
        batch_crafts=args.batch,
        include_all=args.all,
        min_fair_n=args.min_fair_n,
        max_band_width=args.max_band_width,
        min_demand_hours=args.min_demand_hours,
        min_independent_starts=args.min_independent_starts,
        min_fair_books=args.min_fair_books,
    )


def parse_as_of(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("--as-of must include a UTC offset, e.g. 2026-11-09T12:00:00+00:00")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
        as_of = None if args.as_of is None else parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.candidates and (args.min_observations is not None or args.fair_lookback_hours is not None):
        parser.error("--min-observations and --fair-lookback-hours require --candidates")
    if args.candidates:
        from crowley.analysis.candidates import DEFAULT_FAIR_LOOKBACK_HOURS, DEFAULT_MIN_OBSERVATIONS, run

        if args.all:
            parser.error("--all does not apply to --candidates")
        fair_hours = (
            DEFAULT_FAIR_LOOKBACK_HOURS if args.fair_lookback_hours is None else args.fair_lookback_hours
        )
        if fair_hours < 24:
            parser.error("--fair-lookback-hours must be at least 24")

        minimum = DEFAULT_MIN_OBSERVATIONS if args.min_observations is None else args.min_observations
        if minimum < 1:
            parser.error("--min-observations must be at least 1")
        return run(args, config, as_of, minimum, fair_hours)
    url = args.database_url or os.environ.get("CROWLEY_DATABASE_URL")
    if not url:
        parser.error("--database-url or CROWLEY_DATABASE_URL is required")
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        loaded = load_rank_inputs(
            conn, args.house, config, client_build=args.build, character=args.character, as_of=as_of
        )
    evaluation = evaluate_trades(loaded.inputs, config)
    rows = select_rows(evaluation.rows, config)
    meta = {
        "house": args.house,
        "client_build": loaded.client_build,
        "latest_sweep_id": loaded.inputs.latest_sweep_id,
        "as_of_epoch": loaded.inputs.as_of_t,
        "character": args.character,
        "parameters": {
            "professions": sorted(config.professions),
            "lookback_hours": config.lookback_hours,
            "horizon_hours": config.horizon_hours,
            "min_sale_prob": config.min_sale_prob,
            "share": config.share,
            "batch_crafts": config.batch_crafts,
            "min_fair_n": config.min_fair_n,
            "min_fair_books": config.min_fair_books,
            "max_band_width": config.max_band_width,
            "min_demand_hours": config.min_demand_hours,
            "min_independent_starts": config.min_independent_starts,
            "include_all": config.include_all,
        },
    }
    if args.json:
        print(report_json(rows, evaluation, loaded.names, meta))
    else:
        print(
            f"house {args.house} build {loaded.client_build} sweep {loaded.inputs.latest_sweep_id} "
            f"professions {','.join(sorted(config.professions))} horizon {config.horizon_hours} h "
            f"min P(sale) {config.min_sale_prob}"
        )
        print(format_table(rows, evaluation, loaded.names, args.limit))
    return 0


__all__ = [
    "EXCLUSION_REASONS",
    "SaleEstimate",
    "parse_as_of",
    "Evaluation",
    "Exclusion",
    "LoadedInputs",
    "RankConfig",
    "RankInputs",
    "batch_net_revenue",
    "TradeRow",
    "build_parser",
    "evaluate_trades",
    "item_levels_snapshot",
    "item_snapshot_from_books",
    "levels_snapshot",
    "load_rank_inputs",
    "main",
    "rank_trades",
    "select_rows",
    "snapshot_from_books",
]
