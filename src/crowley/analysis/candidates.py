"""Cold-tier promotion candidates: crafts whose goods are too rarely observed to be ranked.

``python -m crowley.analysis rank --candidates``.  Cold-pool items are scanned once every
``coldSlices`` sweeps (about 90 min at the 900 s cadence), so no two observations of one are within
``MAX_GAP`` and ``analysis.rank`` can never give them a P(sale): they end up excluded.  This view does
not model sales.  It shows what a human needs to decide whether a cold item deserves a hot slot.

Population: recipes of the requested professions with a cold-pool output or reagent, plus any recipe
``evaluate_trades`` excluded for want of sale evidence or fair trust (``SALE_EVIDENCE_REASONS``).  The
pool comes from the packaged catalog (active ``pool == "cold"`` items by item id) and applies only to the
build that catalog targets; for any other build there is no cold pool and none is assumed.

What counts as an observation: a sweep's attempted item set is the envelope's observation keys.  A
commodity ``full``/``bounded`` or item ``full`` and source-complete observation is a book, with zero
rows if nothing was listed (an attempted-empty observation is a real, empty book).  An item that the
sweep did not attempt has no row at all, and an ``unqueried`` entry of an aborted sweep (query kind
``unqueried``, coverage ``unqueried``) or an ``incomplete`` one is not loaded: neither is observed.

Per recipe, all at the latest *attempted* observation in the lookback and the rank target rule
(``output_target_price`` on the latest book's levels and the strictly-past 24 h fair value of the books
before it; reagents through ``source_reagent`` / the item-kind cheapest fill):

* margin per craft at that target, and at the median of the output's lowest prices;
* output stats over the lookback: observations, observations with listings, lowest price min / median /
  max, listed supply at the latest observation and distinct sellers (item-kind outputs whose latest
  scope is fully individual; commodity owners are anonymous counts, so none);
* observed depletion between consecutive observations of any length (``books.bottom_walk`` for
  commodities, ``item_books`` owner-continuity walk for item-kind outputs).  It is an UPPER BOUND on
  sales: over gaps of ~90 min a cancel or an expiry is indistinguishable from a purchase.

A recipe with no attempted observation of an output in the window is excluded and counted.  A recipe
is flagged for promotion when its margin at the median lowest price is positive, every output has at
least ``min_observations`` observations and had listings in at least half of them.  Order: flagged
first, then margin x min(depletion, supply) of the limiting output, then spell id.
"""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from fractions import Fraction
from typing import Any

import psycopg

from crowley.analysis.books import Book, bottom_walk
from crowley.analysis.common import jsonable
from crowley.analysis.item_books import (
    ItemBook,
    floor_price,
    item_fair,
    item_intervals,
    load_item_books,
)
from crowley.analysis.metrics import fair_value
from crowley.analysis.rank import (
    HISTORY_HOURS,
    SECONDS_PER_HOUR,
    LoadedInputs,
    RankConfig,
    RankInputs,
    _Evaluator,
    _Excluded,
    _gold,
    _history,
    _window,
    batch_net_revenue,
    evaluate_trades,
    item_levels_snapshot,
    levels_snapshot,
    load_rank_inputs,
)
from crowley.core.game_target import GameTargetError
from crowley.core.watchlist import load_catalog
from crowley.trading.account_gate import classify_recipe
from crowley.trading.api import ItemSnapshot, Recipe
from crowley.trading.strategies.pricing import (
    Source,
    output_target_price,
    recipe_requirements,
    source_reagent,
)

SALE_EVIDENCE_REASONS = (
    "insufficient_independent_evidence",
    "output_fair_unavailable",
    "output_fair_untrusted",
    "output_no_observed_demand",
)
POPULATION_REASONS = ("cold_output", "cold_reagent", *SALE_EVIDENCE_REASONS)
CANDIDATE_EXCLUSION_REASONS = (
    "no_attempted_observation",
    "output_bind_on_pickup",
    "output_bonding_unknown",
    "output_fair_unavailable",
    "output_item_variants",
    "output_not_in_plan",
    "reagent_item_variants",
    "reagent_unpriceable",
)
DEFAULT_MIN_OBSERVATIONS = 4
MIN_LISTING_SHARE = Fraction(1, 2)


class _CandidateExcluded(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason, detail)
        if reason not in CANDIDATE_EXCLUSION_REASONS:
            raise ValueError(f"unregistered candidate exclusion reason {reason!r}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class OutputStats:
    item_id: int
    kind: str  # "commodity" | "item"
    quantity: int  # units per craft
    observations: int  # attempted observations in the lookback
    observations_with_listings: int
    lowest_min: Fraction | None
    lowest_median: Fraction | None
    lowest_max: Fraction | None
    latest_t: float
    latest_age_hours: float
    latest_lowest: Fraction | None
    supply: int  # units listed at the latest attempted observation
    distinct_sellers: int | None  # None: the scope does not identify sellers
    target_price: int
    depletion_units_upper_bound: int  # units the walks consumed over all consecutive pairs
    depletion_intervals: int
    unusable_intervals: int  # pairs the walk could not judge (item-kind scope, identity, ...)
    depletion_seconds: float  # summed start-to-start time of the usable pairs


@dataclass(frozen=True)
class ReagentPrice:
    item_id: int
    quantity: int  # units per craft
    source: str  # "ah" | "vendor"
    observed_age_hours: float | None  # age of the AH book used; None for a vendor purchase


@dataclass(frozen=True)
class Candidate:
    spell_id: int
    professions: tuple[str, ...]
    population: tuple[str, ...]
    outputs: tuple[OutputStats, ...]
    reagents: tuple[ReagentPrice, ...]
    reagent_cost_per_craft: Fraction
    margin_per_craft: Fraction  # at the rank target price
    margin_at_median: Fraction | None  # at the median lowest price of every output
    min_observations: int
    min_listing_share: Fraction
    limiting_item_id: int  # the output behind depletion_units / supply_units
    depletion_units: int
    supply_units: int
    score: Fraction  # margin_per_craft x min(depletion_units, supply_units)
    flagged: bool
    flag_failures: tuple[str, ...]
    status: str | None = None


@dataclass(frozen=True)
class CandidateExclusion:
    spell_id: int
    reason: str
    detail: str
    population: tuple[str, ...]
    professions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateReport:
    candidates: tuple[Candidate, ...]
    excluded: tuple[CandidateExclusion, ...]
    population: int
    population_counts: Mapping[str, int]
    cold_item_count: int

    def __post_init__(self) -> None:
        if self.population != len(self.candidates) + len(self.excluded):
            raise RuntimeError(
                f"{self.population} recipes in the population but {len(self.candidates)} candidates "
                f"and {len(self.excluded)} exclusions"
            )

    def exclusion_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.excluded).items()))


def cold_item_ids(build: str) -> frozenset[int]:
    """Active cold-pool item ids of the packaged catalog, if it targets ``build``; else none."""

    catalog = load_catalog(enforce_target=False)
    if catalog.client_build != build:
        return frozenset()
    return frozenset(
        item.item_id for item in catalog.items if item.status == "active" and item.pool == "cold"
    )


def _units(book: Book) -> int:
    return sum(group.quantity for group in book.groups)


def _lowest(book: Book) -> Fraction | None:
    return min((group.unit for group in book.groups), default=None)


class _Evidence:
    """Per-item observation series, memoised; one instance per report."""

    def __init__(self, inputs: RankInputs, config: RankConfig) -> None:
        self.inputs = inputs
        self.config = config
        self.rank = _Evaluator(inputs, config)
        self._item: dict[int, tuple[list[ItemBook], str | None]] = {}

    def kind(self, item_id: int) -> str:
        try:
            return self.rank.output_kind(item_id)
        except _Excluded as exc:
            raise _CandidateExcluded(exc.reason, exc.detail) from exc

    def commodity(self, item_id: int) -> tuple[list[Book], list[Book]]:
        history = _history(self.inputs.books.get(item_id, ()), self.inputs, self.config)
        return history, _window(history, self.inputs, self.config)

    def item(self, item_id: int, reason: str) -> tuple[list[ItemBook], str | None]:
        if item_id not in self._item:
            books = _history((self.inputs.item_books or {}).get(item_id, ()), self.inputs, self.config)
            keys = sorted({key for book in books for key in book.item_keys})
            if len(keys) > 1:
                raise _CandidateExcluded(reason, f"item {item_id} books carry itemKeys {keys}")
            self._item[item_id] = (books, keys[0] if keys else None)
        return self._item[item_id]

    def age_hours(self, t: float) -> float:
        return (self.inputs.as_of_t - t) / SECONDS_PER_HOUR

    def output(self, item_id: int, quantity: int) -> tuple[OutputStats, ItemSnapshot]:
        kind = self.kind(item_id)
        try:
            self.rank.check_tradeable(item_id)
        except _Excluded as exc:
            raise _CandidateExcluded(exc.reason, exc.detail) from exc
        if kind == "commodity":
            return self._commodity_output(item_id, quantity)
        if kind == "item" and self.inputs.item_books is not None:
            return self._item_output(item_id, quantity)
        raise _CandidateExcluded("no_attempted_observation", f"item {item_id}: scanned as {kind!r}, no books")

    def _stats(
        self,
        item_id: int,
        kind: str,
        quantity: int,
        lowest: Sequence[Fraction | None],
        latest_t: float,
        supply: int,
        sellers: int | None,
        target: int,
        walk: tuple[int, int, int, float],
    ) -> OutputStats:
        priced = sorted(price for price in lowest if price is not None)
        depletion, intervals, unusable, seconds = walk
        return OutputStats(
            item_id=item_id,
            kind=kind,
            quantity=quantity,
            observations=len(lowest),
            observations_with_listings=len(priced),
            lowest_min=priced[0] if priced else None,
            lowest_median=statistics.median(priced) if priced else None,
            lowest_max=priced[-1] if priced else None,
            latest_t=latest_t,
            latest_age_hours=self.age_hours(latest_t),
            latest_lowest=lowest[-1],
            supply=supply,
            distinct_sellers=sellers,
            target_price=target,
            depletion_units_upper_bound=depletion,
            depletion_intervals=intervals,
            unusable_intervals=unusable,
            depletion_seconds=seconds,
        )

    def _commodity_output(self, item_id: int, quantity: int) -> tuple[OutputStats, ItemSnapshot]:
        history, window = self.commodity(item_id)
        if not window:
            raise _CandidateExcluded(
                "no_attempted_observation", f"commodity {item_id} has no attempted observation in the window"
            )
        latest = window[-1]
        fair = fair_value(
            history,
            history.index(latest),
            include_trend=False,
            min_books=self.config.min_fair_books,
        )
        if fair["value"] is None:
            raise _CandidateExcluded(
                "output_fair_unavailable",
                f"commodity {item_id}: {fair['n']} strictly-past reference books "
                f"(need {self.config.min_fair_books})",
            )
        snapshot = replace(levels_snapshot(item_id, latest), fair=fair["value"])
        # Any gap length: this is the signal the 1260 s chain rule exists to refuse in rank.
        walks = [bottom_walk(before, after) for before, after in zip(window, window[1:], strict=False)]
        depletion = sum(units for walk in walks for _, units in walk.consumed)
        seconds = sum(after.t - before.t for before, after in zip(window, window[1:], strict=False))
        stats = self._stats(
            item_id,
            "commodity",
            quantity,
            [_lowest(book) for book in window],
            latest.t,
            _units(latest),
            None,
            output_target_price(snapshot, self.config.pricing),
            (depletion, len(walks), 0, seconds),
        )
        return stats, snapshot

    def _item_output(self, item_id: int, quantity: int) -> tuple[OutputStats, ItemSnapshot]:
        history, key = self.item(item_id, "output_item_variants")
        window = _window(history, self.inputs, self.config)
        if not window:
            raise _CandidateExcluded(
                "no_attempted_observation", f"item {item_id} has no attempted observation in the window"
            )
        floors = [floor_price(book, key) for book in history]
        latest = window[-1]
        index = history.index(latest)
        fair = item_fair(history, floors, index, min_books=self.config.min_fair_books)
        if fair["value"] is None:
            raise _CandidateExcluded(
                "output_fair_unavailable",
                f"item {item_id}: {fair['n']} strictly-past floors (need {self.config.min_fair_books})",
            )
        snapshot = replace(item_levels_snapshot(item_id, latest, key), fair=fair["value"])
        span = int((self.config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR) + 1
        offset = len(history) - len(window)
        intervals = item_intervals(history, key, max_interval_seconds=span)[offset:]
        usable = [interval for interval in intervals if interval is not None]
        depletion = sum(units for interval in usable for _, units in interval.consumed)
        scoped = latest.scoped(key)
        owners = {auction.owner for auction in scoped}
        sellers = None if None in owners else len(owners)
        stats = self._stats(
            item_id,
            "item",
            quantity,
            [floor_price(book, key) for book in window],
            latest.t,
            sum(auction.quantity for auction in scoped),
            sellers,
            output_target_price(snapshot, self.config.pricing),
            (depletion, len(usable), len(intervals) - len(usable), sum(i.dt for i in usable)),
        )
        return stats, snapshot

    def reagent(self, item_id: int, quantity: int) -> tuple[Source, float | None]:
        """Cost of ``quantity`` units at the reagent's latest attempted observation (age: hours)."""

        facts = self.inputs.facts.get(item_id)
        _, window = self.commodity(item_id)
        if window:
            source = source_reagent(levels_snapshot(item_id, window[-1]), facts, item_id, quantity)
            age = self.age_hours(window[-1].t)
        elif self.inputs.observed_kinds.get(item_id) == "item" and self.inputs.item_books is not None:
            history, key = self.item(item_id, "reagent_item_variants")
            item_window = _window(history, self.inputs, self.config)
            if item_window:
                source = self.rank._item_reagent_source(item_id, quantity, item_window[-1])
                age = self.age_hours(item_window[-1].t)
            else:
                source, age = source_reagent(None, facts, item_id, quantity), None
        else:
            source, age = source_reagent(None, facts, item_id, quantity), None
        if source is None:
            raise _CandidateExcluded(
                "reagent_unpriceable",
                f"reagent {item_id} x{quantity} has no AH fill at its latest attempted observation "
                "in the window and no vendor price",
            )
        return source, age if source.kind == "ah" else None


def _evaluate(
    evidence: _Evidence, recipe: Recipe, population: tuple[str, ...], min_observations: int
) -> Candidate:
    config = evidence.config
    batch = config.batch_crafts
    outputs: dict[int, int] = {}
    for item_id, quantity in recipe.outputs:
        outputs[item_id] = outputs.get(item_id, 0) + quantity
    stats: list[OutputStats] = []
    for item_id in sorted(outputs):
        stat, _ = evidence.output(item_id, outputs[item_id])
        stats.append(stat)

    reagents: list[ReagentPrice] = []
    cost = Fraction(0)
    for item_id, quantity in recipe_requirements(recipe, batch).items():
        source, age = evidence.reagent(item_id, quantity)
        cost += source.full_cost
        reagents.append(ReagentPrice(item_id, quantity // batch, source.kind, age))

    rules = evidence.inputs.rules

    def margin(price_of: Mapping[int, Fraction | int | None]) -> Fraction | None:
        revenue = Fraction(0)
        for stat in stats:
            price = price_of[stat.item_id]
            if price is None:
                return None
            net, _ = batch_net_revenue(
                evidence.inputs.facts[stat.item_id],
                rules,
                stat.quantity * batch,
                price,
                config.listing_duration_hours,
            )
            revenue += net
        return (revenue - cost) / batch

    at_target = margin({stat.item_id: stat.target_price for stat in stats})
    at_median = margin({stat.item_id: stat.lowest_median for stat in stats})
    assert at_target is not None

    def limit(stat: OutputStats) -> Fraction:
        return Fraction(min(stat.depletion_units_upper_bound, stat.supply), stat.quantity)

    limiting = min(stats, key=lambda stat: (limit(stat), stat.item_id))
    depletion, supply = limiting.depletion_units_upper_bound, limiting.supply
    shares = [Fraction(stat.observations_with_listings, stat.observations) for stat in stats]
    failures: list[str] = []
    if at_median is None or at_median <= 0:
        failures.append("margin at the median lowest price is not positive")
    fewest = min(stat.observations for stat in stats)
    if fewest < min_observations:
        failures.append(f"{fewest} observations (need {min_observations})")
    if min(shares) < MIN_LISTING_SHARE:
        failures.append(f"listings in {float(min(shares)):.0%} of observations (need 50%)")
    status = None
    if evidence.inputs.account is not None:
        status = classify_recipe(recipe, evidence.inputs.account).status
    return Candidate(
        spell_id=recipe.spell_id,
        professions=tuple(sorted(recipe.professions & config.professions)),
        population=population,
        outputs=tuple(stats),
        reagents=tuple(reagents),
        reagent_cost_per_craft=cost / batch,
        margin_per_craft=at_target,
        margin_at_median=at_median,
        min_observations=fewest,
        min_listing_share=min(shares),
        limiting_item_id=limiting.item_id,
        depletion_units=depletion,
        supply_units=supply,
        score=at_target * min(depletion, supply),
        flagged=not failures,
        flag_failures=tuple(failures),
        status=status,
    )


def build_candidates(
    inputs: RankInputs, config: RankConfig, cold_ids: Collection[int], *, min_observations: int
) -> CandidateReport:
    """Select the population, evaluate each recipe on its latest attempted observations, order them."""

    if min_observations < 1:
        raise ValueError("min_observations must be at least 1")
    evaluation = evaluate_trades(inputs, config)
    unranked = {item.spell_id: item.reason for item in evaluation.excluded}
    evidence = _Evidence(inputs, config)
    candidates: list[Candidate] = []
    excluded: list[CandidateExclusion] = []
    counts: Counter[str] = Counter()
    for recipe in sorted(inputs.recipes, key=lambda entry: entry.spell_id):
        if not recipe.professions & config.professions:
            continue
        population: list[str] = []
        if any(item_id in cold_ids for item_id, _ in recipe.outputs):
            population.append("cold_output")
        if any(item_id in cold_ids for item_id, _ in recipe.reagents):
            population.append("cold_reagent")
        if unranked.get(recipe.spell_id) in SALE_EVIDENCE_REASONS:
            population.append(unranked[recipe.spell_id])
        if not population:
            continue
        counts.update(population)
        try:
            if not recipe.outputs:
                raise _CandidateExcluded("output_not_in_plan", "recipe has no outputs")
            candidates.append(_evaluate(evidence, recipe, tuple(population), min_observations))
        except _CandidateExcluded as exc:
            excluded.append(
                CandidateExclusion(
                    recipe.spell_id,
                    exc.reason,
                    exc.detail,
                    tuple(population),
                    tuple(sorted(recipe.professions & config.professions)),
                )
            )
    ordered = sorted(candidates, key=lambda row: (not row.flagged, -row.score, row.spell_id))
    return CandidateReport(
        tuple(ordered),
        tuple(excluded),
        len(candidates) + len(excluded),
        dict(sorted(counts.items())),
        len(cold_ids),
    )


# -- loading ---------------------------------------------------------------------------


def load_candidate_inputs(
    conn: Any,
    house_key: str,
    config: RankConfig,
    *,
    client_build: str | None,
    character: str | None,
    as_of: datetime | None,
) -> LoadedInputs:
    """``load_rank_inputs`` plus item-kind reagent books over the whole window (read-only).

    Rank prices item-kind reagents from the latest sweep's book only, which a cold reagent
    does not have; here they are read over the lookback like outputs.
    """

    loaded = load_rank_inputs(
        conn, house_key, config, client_build=client_build, character=character, as_of=as_of
    )
    inputs = loaded.inputs
    assert inputs.item_books is not None
    reagent_items = sorted(
        {
            item_id
            for recipe in inputs.recipes
            for item_id, _ in recipe.reagents
            if inputs.observed_kinds.get(item_id) == "item"
        }
    )
    full = load_item_books(
        conn,
        house_key,
        reagent_items,
        client_build=loaded.client_build,
        start_t=int(inputs.as_of_t - (config.lookback_hours + HISTORY_HOURS) * SECONDS_PER_HOUR),
        end_t=int(inputs.as_of_t),
    )
    return LoadedInputs(
        replace(inputs, item_books={**inputs.item_books, **full}), loaded.client_build, loaded.names
    )


# -- presentation ----------------------------------------------------------------------


def _price(value: Fraction | int | None) -> str:
    return "-" if value is None else _gold(value)


def format_candidates(report: CandidateReport, names: Mapping[int, str], limit: int | None = None) -> str:
    shown = report.candidates if limit is None else report.candidates[:limit]
    header = (
        f"{'spell':>7} {'flag':<4} {'why':<5} {'output (item id)':<34} {'obs':>7} "
        f"{'low min':>9} {'median':>9} {'max':>9} {'supply':>6} {'sell':>4} {'depl<=':>6} "
        f"{'cost':>9} {'margin':>9} {'@median':>9} {'age h':>5} {'status':<18}"
    )
    lines = [header]
    for row in shown:
        out = next(stat for stat in row.outputs if stat.item_id == row.limiting_item_id)
        label = ", ".join(
            f"{s.quantity}x {names.get(s.item_id, '?')[:20]} ({s.item_id})" for s in row.outputs
        )
        why = "".join({"cold_output": "O", "cold_reagent": "R"}.get(reason, "E") for reason in row.population)
        sellers = "-" if out.distinct_sellers is None else str(out.distinct_sellers)
        lines.append(
            f"{row.spell_id:>7} {'YES' if row.flagged else '-':<4} {why:<5} {label[:34]:<34} "
            f"{f'{out.observations_with_listings}/{out.observations}':>7} "
            f"{_price(out.lowest_min):>9} {_price(out.lowest_median):>9} {_price(out.lowest_max):>9} "
            f"{row.supply_units:>6} {sellers:>4} {row.depletion_units:>6} "
            f"{_gold(row.reagent_cost_per_craft):>9} {_gold(row.margin_per_craft):>9} "
            f"{_price(row.margin_at_median):>9} {out.latest_age_hours:>5.1f} {(row.status or '-'):<18}"
        )
    flagged = sum(row.flagged for row in report.candidates)
    lines.append(
        f"shown {len(shown)} of {len(report.candidates)} candidates ({flagged} flagged); population "
        f"{report.population} recipes, excluded {len(report.excluded)}; "
        f"cold-pool items {report.cold_item_count}"
    )
    lines.append(
        "why: O cold output, R cold reagent, E excluded from rank for sale evidence / fair trust; "
        "obs: observations with listings / attempted observations in the lookback"
    )
    lines.append(
        "depl<=: units the bottom walk consumed between consecutive observations, any gap length. "
        "UPPER BOUND on sales: a cancel or an expiry over a long gap looks the same. Weak signal, not demand."
    )
    lines.append(
        "sell: distinct sellers at the latest observation (item-kind only; '-' when not identifiable)"
    )
    for reason, count in report.population_counts.items():
        lines.append(f"  population {reason}: {count}")
    for reason, count in report.exclusion_counts().items():
        lines.append(f"  excluded {reason}: {count}")
    return "\n".join(lines)


def report_json(report: CandidateReport, names: Mapping[int, str], meta: Mapping[str, Any]) -> str:
    payload = {
        **meta,
        "candidates": [
            {**asdict(row), "names": {str(stat.item_id): names.get(stat.item_id) for stat in row.outputs}}
            for row in report.candidates
        ],
        "excluded": [asdict(item) for item in report.excluded],
        "exclusion_counts": report.exclusion_counts(),
        "population": report.population,
        "population_counts": dict(report.population_counts),
        "cold_item_count": report.cold_item_count,
        "depletion_note": "upper bound on sales: cancels and expiries are indistinguishable over long gaps",
    }
    return json.dumps(jsonable(payload), indent=2, sort_keys=True)


def run(args: Any, config: RankConfig, as_of: datetime | None, min_observations: int) -> int:
    url = args.database_url or os.environ.get("CROWLEY_DATABASE_URL")
    if not url:
        raise SystemExit("--database-url or CROWLEY_DATABASE_URL is required")
    with psycopg.connect(url, autocommit=True) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        loaded = load_candidate_inputs(
            conn,
            args.house,
            config,
            client_build=args.build,
            character=args.character,
            as_of=as_of,
        )
    try:
        cold_ids = cold_item_ids(loaded.client_build)
    except GameTargetError as exc:
        raise SystemExit(f"cannot read the cold pool: {exc}") from exc
    report = build_candidates(loaded.inputs, config, cold_ids, min_observations=min_observations)
    if args.json:
        meta = {
            "house": args.house,
            "client_build": loaded.client_build,
            "latest_sweep_id": loaded.inputs.latest_sweep_id,
            "as_of_epoch": loaded.inputs.as_of_t,
            "character": args.character,
            "parameters": {
                "professions": sorted(config.professions),
                "lookback_hours": config.lookback_hours,
                "batch_crafts": config.batch_crafts,
                "min_observations": min_observations,
                "min_fair_books": config.min_fair_books,
            },
        }
        print(report_json(report, loaded.names, meta))
    else:
        print(
            f"house {args.house} build {loaded.client_build} sweep {loaded.inputs.latest_sweep_id} "
            f"professions {','.join(sorted(config.professions))} lookback {config.lookback_hours} h "
            f"min observations {min_observations}"
        )
        print(format_candidates(report, loaded.names, args.limit))
    return 0


__all__ = [
    "CANDIDATE_EXCLUSION_REASONS",
    "POPULATION_REASONS",
    "SALE_EVIDENCE_REASONS",
    "Candidate",
    "CandidateExclusion",
    "CandidateReport",
    "OutputStats",
    "ReagentPrice",
    "build_candidates",
    "cold_item_ids",
    "format_candidates",
    "load_candidate_inputs",
    "report_json",
    "run",
]
