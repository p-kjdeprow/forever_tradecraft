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
from typing import Any

import psycopg

from crowley.analysis.books import Book, Interval, load_commodity_books, valid_intervals
from crowley.analysis.common import jsonable
from crowley.analysis.metrics import FAIR_MIN_BOOKS, demand_metrics, fair_value
from crowley.analysis.time_to_sale import HORIZON, replay_many
from crowley.core.auction_rules import AuctionHouseRules, rules_for_house
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

DEFAULT_PROFESSIONS = frozenset({"alchemy", "tailoring"})
SECONDS_PER_HOUR = 3600
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


@dataclass(frozen=True)
class Evaluation:
    rows: tuple[TradeRow, ...]
    excluded: tuple[Exclusion, ...]
    considered: int

    def exclusion_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.excluded).items()))


class _Excluded(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)
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


def _horizon_observable(
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


def _history(books: Sequence[Book], inputs: RankInputs, config: RankConfig) -> list[Book]:
    start = inputs.as_of_t - (config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR
    return [book for book in books if start <= book.t <= inputs.as_of_t]


def _window(books: Sequence[Book], inputs: RankInputs, config: RankConfig) -> list[Book]:
    start = inputs.as_of_t - config.lookback_hours * SECONDS_PER_HOUR
    return [book for book in books if start <= book.t <= inputs.as_of_t]


class _Evaluator:
    def __init__(self, inputs: RankInputs, config: RankConfig) -> None:
        self.inputs = inputs
        self.config = config
        self._windows: dict[int, list[Book]] = {}
        self._outputs: dict[int, ItemSnapshot] = {}
        self._estimates: dict[tuple[int, int], SaleEstimate] = {}

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

    def reagent_source(self, item_id: int, quantity: int) -> Source | None:
        window = self.window(item_id)
        snapshot = None if window is None else levels_snapshot(item_id, window[-1])
        return source_reagent(snapshot, self.inputs.facts.get(item_id), item_id, quantity)

    def output_snapshot(self, item_id: int) -> ItemSnapshot:
        if item_id in self._outputs:
            return self._outputs[item_id]
        window = self.window(item_id)
        if window is None:
            raise self._missing_book(item_id)
        if item_id not in self.inputs.facts:
            raise _Excluded("output_no_client_facts", f"item {item_id} has no client facts")
        snapshot = snapshot_from_books(item_id, window)
        if snapshot.fair is None:
            raise _Excluded(
                "output_fair_unavailable",
                f"item {item_id}: {snapshot.fair_n} strictly-past reference books in window",
            )
        if snapshot.demand_units_per_day is None or snapshot.demand_units_per_day <= 0:
            raise _Excluded("output_no_observed_demand", f"item {item_id} shows no bottom-walk demand")
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
        kind = self.inputs.observed_kinds.get(item_id)
        if kind is None:
            return _Excluded("output_not_in_plan", f"item {item_id} was never scanned for this house/build")
        if kind != "commodity":
            return _Excluded("output_not_commodity", f"item {item_id} is scanned as kind {kind!r}")
        return _Excluded("output_no_current_book", f"commodity {item_id} has no book in the lookback window")

    def sale_estimate(self, item_id: int, quantity: int) -> SaleEstimate:
        key = (item_id, quantity)
        if key in self._estimates:
            return self._estimates[key]
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
            if not _horizon_observable(history, intervals, index, price, horizon):
                continue
            sold_in_time = result.event_time is not None and result.event_time <= horizon
            determined.append((history[index].t, sold_in_time))
        independent = sold = 0
        last: float | None = None
        for start_t, was_sold in determined:
            if last is None or start_t >= last + horizon:
                last = start_t
                independent += 1
                sold += was_sold
        estimate = SaleEstimate(
            sold / independent if independent else 0.0, len(determined), independent, sold
        )
        self._estimates[key] = estimate
        return estimate

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
            batch_deposit = deposit(
                self.inputs.facts[item_id].sell_price, quantity, config.listing_duration_hours, rules
            )
            revenue += quantity * target * (1 - rules.seller_cut) - batch_deposit
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
    evaluator = _Evaluator(inputs, config)
    rows: list[TradeRow] = []
    excluded: list[Exclusion] = []
    considered = len(inputs.random_yield_spells)
    for spell_id in inputs.random_yield_spells:
        excluded.append(Exclusion(spell_id, "random_yield", "recipe output is not a fixed yield"))
    for recipe in sorted(inputs.recipes, key=lambda entry: entry.spell_id):
        if not recipe.professions & config.professions:
            continue
        considered += 1
        try:
            rows.append(evaluator.annotate(evaluator.evaluate(recipe), recipe))
        except _Excluded as exc:
            excluded.append(Exclusion(recipe.spell_id, exc.reason, exc.detail))
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


def _latest_sweep(
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
        raise RuntimeError(f"no market sweep for house {house_key!r} build {client_build!r} as of {as_of}")
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


def _random_yield_spells(conn: Any, client_build: str, professions: frozenset[str]) -> tuple[int, ...]:
    """Spells of the professions with a non-fixed output (the trading loader skips them)."""

    rows = conn.execute(
        """
        SELECT DISTINCT o.spell_id
        FROM latest_client_export AS e
        JOIN client_recipe_outputs AS o ON o.export_id = e.id
        JOIN client_recipe_skill_lines AS s ON s.export_id = o.export_id AND s.spell_id = o.spell_id
        WHERE e.client_build = %s AND o.yield_kind <> 'fixed' AND s.profession = ANY(%s)
        ORDER BY o.spell_id
        """,
        (client_build, sorted(professions)),
    ).fetchall()
    return tuple(int(row[0]) for row in rows)


def _names(conn: Any, client_build: str, item_ids: Sequence[int]) -> dict[int, str]:
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
    sweep_id, finished_t, build = _latest_sweep(conn, house_key, client_build, as_of)
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
    names = _names(conn, build, display_ids)
    output_ids = sorted({item_id for recipe in recipes for item_id, _ in recipe.outputs})
    observed_kinds = _observed_kinds(conn, house_key, build, output_ids)
    random_yield = _random_yield_spells(conn, build, config.professions)
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
    )
    return LoadedInputs(inputs, build, names)


# -- presentation ----------------------------------------------------------------------


def _gold(copper: Fraction | int | None) -> str:
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
        f"{'spell':>7} {'output (item id)':<38} {'cost':>9} {'margin':>9} {'P(sale)':>7} {'sold/ind':>8} "
        f"{'units/h':>8} {'profit/h':>10} {'capital':>10} {'status':<20} {'recipe':>10} {'payback':>8}"
    )
    lines = [header]
    for row in shown:
        label = ", ".join(f"{qty}x {names.get(item, '?')[:22]} ({item})" for item, qty in row.outputs)
        payback = "-" if row.payback_crafts is None else f"{float(row.payback_crafts):.1f}"
        recipe = row.recipe_price_state or "-"
        if row.recipe_price is not None:
            recipe = _gold(row.recipe_price)
        lines.append(
            f"{row.spell_id:>7} {label[:38]:<38} {_gold(row.reagent_cost_per_craft):>9} "
            f"{_gold(row.margin_per_craft):>9} {row.sale_probability:>7.2f} "
            f"{f'{row.sold_starts}/{row.independent_starts}':>8}{'*' if row.target_above_lowest else ' '}"
            f"{float(row.market_units_per_hour):>8.2f} {_gold(row.profit_per_hour):>10} "
            f"{_gold(row.capital_per_batch):>10} {(row.status or '-'):<20} {recipe:>10} {payback:>8}"
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
    return "\n".join(lines)


def report_json(
    rows: Sequence[TradeRow], evaluation: Evaluation, names: Mapping[int, str], meta: Mapping[str, Any]
) -> str:
    payload = {
        **meta,
        "rows": [{**asdict(row), "names": {str(i): names.get(i) for i, _ in row.outputs}} for row in rows],
        "excluded": [asdict(item) for item in evaluation.excluded],
        "exclusion_counts": evaluation.exclusion_counts(),
        "considered": evaluation.considered,
    }
    return json.dumps(jsonable(payload), indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crowley.analysis rank", description=__doc__)
    parser.add_argument("--database-url", help="defaults to $CROWLEY_DATABASE_URL")
    parser.add_argument("--house", required=True, help="explicit house key, e.g. us-normal-alliance")
    parser.add_argument("--build", help="full client build, e.g. 1.60.1.70862 (default: latest sweep's)")
    parser.add_argument("--character", help="classify recipes against this character's latest capture")
    parser.add_argument("--professions", default="alchemy,tailoring")
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
    "SaleEstimate",
    "parse_as_of",
    "Evaluation",
    "Exclusion",
    "LoadedInputs",
    "RankConfig",
    "RankInputs",
    "TradeRow",
    "build_parser",
    "evaluate_trades",
    "levels_snapshot",
    "load_rank_inputs",
    "main",
    "rank_trades",
    "select_rows",
    "snapshot_from_books",
]
