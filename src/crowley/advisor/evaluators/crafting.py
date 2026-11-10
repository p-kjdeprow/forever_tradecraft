"""Crafting pathways: ``analysis.rank`` for the hot tier, ``analysis.candidates`` for the cold pool.

Rank supplies the economics it always did (reagent cost, margin at its target price, P(sale)
from independent replay starts, market units/h, capital).  This evaluator adds one thing: the
headline profit is priced at 1c under the CURRENT lowest listing of each output (the price a
human would actually post now), with rank's target margin kept beside it.

P(sale) stays the one rank measured, at rank's target price.  When the current-lowest price is
above that target (rank clamps its target to a cap or floor) the measured P is for a cheaper
listing than the headline; the row carries ``listed_above_measured_price`` for exactly that case.

A cold-pool candidate (``analysis.candidates``) becomes a pathway only for a recipe that rank
excluded (a cold item has no book in the latest sweep, or too few to replay) and that the
candidate view flags.  It has no P(sale); its
volume is the observed any-gap bottom depletion per hour and its confidence is that of its
depletion evidence.  Every recipe rank considered is counted exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil

from crowley.advisor.estimates import PriceEstimate
from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import (
    Evidence,
    Pathway,
    Step,
    StrategyResult,
    combined_confidence,
)
from crowley.analysis.candidates import (
    DEFAULT_MIN_OBSERVATIONS,
    Candidate,
    OutputStats,
    build_candidates,
)
from crowley.analysis.rank import (
    Evaluation,
    RankConfig,
    TradeRow,
    _Evaluator,
    batch_net_revenue,
    evaluate_trades,
)
from crowley.trading.strategies.pricing import Source

SECONDS_PER_HOUR = 3600
LISTING_HOURS = 24  # rank's listing duration for the crafted batch


@dataclass(frozen=True)
class CraftRow:
    """One rank row with the current-lowest repricing next to rank's target."""

    row: TradeRow
    lowest: dict[int, int | None]  # output item -> current lowest listing
    price_lowest: dict[int, int] | None  # output item -> 1c under it; None when any is unpriceable
    margin_lowest: Fraction | None


@dataclass(frozen=True)
class CraftingAnalysis:
    evaluation: Evaluation
    rank_config: RankConfig
    rows: tuple[CraftRow, ...]
    candidates: dict[int, Candidate]  # flagged cold-pool candidates by spell id
    candidate_notes: dict[int, str]  # spell id -> why a candidate was not usable
    reagents: _Evaluator


def rank_config_for(ctx: Context) -> RankConfig:
    config = ctx.config
    missing = sorted(config.professions - ctx.state.professions)
    if missing:
        raise ValueError(f"state was loaded without professions {missing}")
    return RankConfig(
        professions=config.professions,
        lookback_hours=config.lookback_hours,
        horizon_hours=config.horizon_hours,
        share=config.share,
        batch_crafts=config.batch_crafts,
        min_independent_starts=config.min_independent_starts,
        listing_duration_hours=LISTING_HOURS,
    )


def margin_at(
    ctx: Context,
    outputs: dict[int, int],
    prices: dict[int, int | Fraction],
    reagent_cost_per_craft: Fraction,
) -> Fraction:
    """Net margin per craft listing every output at ``prices`` (rank's one revenue formula)."""

    batch = ctx.config.batch_crafts
    revenue = Fraction(0)
    for item_id, quantity in outputs.items():
        net, _ = batch_net_revenue(
            ctx.state.facts[item_id], ctx.state.rules, quantity * batch, prices[item_id], LISTING_HOURS
        )
        revenue += net
    return (revenue - reagent_cost_per_craft * batch) / batch


def _undercut(lowest: int | None) -> int | None:
    return lowest - 1 if lowest is not None and lowest > 1 else None


def _craft_row(ctx: Context, row: TradeRow) -> CraftRow:
    estimates: dict[int, PriceEstimate | None] = {
        item: ctx.estimates.estimate(item) for item, _ in row.outputs
    }
    lowest = {item: None if est is None else est.lowest for item, est in estimates.items()}
    prices = {item: _undercut(value) for item, value in lowest.items()}
    if any(price is None for price in prices.values()):
        return CraftRow(row, lowest, None, None)
    priced = {item: price for item, price in prices.items() if price is not None}
    margin = margin_at(ctx, dict(row.outputs), dict(priced), row.reagent_cost_per_craft)
    return CraftRow(row, lowest, priced, margin)


def analyze(ctx: Context) -> CraftingAnalysis:
    """Rank evaluation, repriced rows and the usable cold candidates, computed once per run."""

    state = ctx.state
    rank_config = rank_config_for(ctx)
    evaluation = evaluate_trades(state.inputs, rank_config)
    rows = tuple(_craft_row(ctx, row) for row in evaluation.rows)
    usable: dict[int, Candidate] = {}
    notes: dict[int, str] = {}
    if state.cold_pool is not None:
        report = build_candidates(
            state.inputs,
            rank_config,
            state.cold_pool,
            min_observations=DEFAULT_MIN_OBSERVATIONS,
            evaluation=evaluation,
        )
        for candidate in report.candidates:
            if candidate.flagged:
                usable[candidate.spell_id] = candidate
            else:
                notes[candidate.spell_id] = "candidate not flagged: " + "; ".join(candidate.flag_failures)
        for excluded in report.excluded:
            notes[excluded.spell_id] = f"candidate excluded: {excluded.reason}: {excluded.detail}"
    return CraftingAnalysis(
        evaluation, rank_config, rows, usable, notes, _Evaluator(state.inputs, rank_config)
    )


def headline_margin(ctx: Context, craft: CraftRow) -> Fraction | None:
    """The configured headline margin per craft (None: unpriceable at the current lowest)."""

    if ctx.config.crafting_price == "lowest":
        return craft.margin_lowest
    return craft.row.margin_per_craft


def _headline_prices(ctx: Context, craft: CraftRow) -> dict[int, int]:
    if ctx.config.crafting_price == "lowest":
        assert craft.price_lowest is not None
        return craft.price_lowest
    return dict(craft.row.target_prices)


def _buy_step(source: Source, quantity: int, vendor_note: str = "") -> Step:
    if source.kind == "ah":
        return Step("buy_ah", item_id=source.item_id, quantity=quantity, price=source.unit_limit)
    return Step(
        "buy_vendor",
        item_id=source.item_id,
        quantity=quantity,
        price=ceil(source.full_cost / quantity),
        note=vendor_note or "vendor price, whole bundles",
    )


def _steps(
    ctx: Context,
    analysis: CraftingAnalysis,
    spell_id: int,
    reagents: tuple[tuple[int, int], ...],
    outputs: dict[int, int],
    prices: dict[int, int],
) -> tuple[Step, ...]:
    batch = ctx.config.batch_crafts
    steps: list[Step] = []
    for item_id, per_craft in reagents:
        quantity = per_craft * batch
        source = analysis.reagents.reagent_source(item_id, quantity)
        if source is None:
            steps.append(Step("buy_ah", item_id=item_id, quantity=quantity, note="reprice at the AH"))
        else:
            steps.append(_buy_step(source, quantity))
    steps.append(Step("craft", spell_id=spell_id, quantity=batch))
    for item_id, quantity in sorted(outputs.items()):
        steps.append(Step("list_ah", item_id=item_id, quantity=quantity * batch, price=prices[item_id]))
    return tuple(steps)


def _title(ctx: Context, outputs: dict[int, int], spell_id: int) -> str:
    label = ", ".join(f"{qty}x {ctx.state.name(item)}" for item, qty in sorted(outputs.items()))
    return f"craft {label} (spell {spell_id})"


def _demand_evidence(estimates: list[PriceEstimate | None]) -> Evidence | None:
    counts = [est.demand_intervals for est in estimates if est is not None and est.kind == "commodity"]
    if not counts:
        return None
    return Evidence("demand_intervals", min(counts), detail="usable book intervals, scarcest output")


def row_pathway(ctx: Context, analysis: CraftingAnalysis, craft: CraftRow) -> Pathway:
    """The crafting pathway of one rank row; the caller has checked the headline margin exists."""

    row = craft.row
    margin = headline_margin(ctx, craft)
    assert margin is not None
    outputs = dict(row.outputs)
    prices = _headline_prices(ctx, craft)
    estimates = [ctx.estimates.estimate(item) for item in outputs]
    demand = _demand_evidence(estimates)
    evidence = Evidence(
        "independent_starts",
        row.independent_starts,
        successes=row.sold_starts,
        usable=row.usable_starts,
        detail="replayed listings at rank's target price, one horizon apart",
    )
    unit_count = sum(outputs.values())
    measured = dict(row.target_prices)
    listed_above = craft.price_lowest is not None and any(
        craft.price_lowest[item] > measured[item] for item in outputs
    )
    return Pathway(
        strategy="crafting",
        key=f"crafting:{row.spell_id}",
        title=_title(ctx, outputs, row.spell_id),
        steps=_steps(ctx, analysis, row.spell_id, tuple((i, q) for i, q, _ in row.reagents), outputs, prices),
        profit_per_unit=margin / unit_count,
        profit_per_craft=margin,
        volume_per_hour=row.market_units_per_hour,
        sale_probability=row.sale_probability,
        evidence=evidence,
        demand_evidence=demand,
        listed_units=min((est.listed_units for est in estimates if est is not None), default=None),
        capital=row.capital_per_batch,
        profit_per_hour=margin * row.crafts_per_hour,
        confidence=combined_confidence(evidence, demand),
        details={
            "kind": row.kind,
            "price_basis": ctx.config.crafting_price,
            "margin_at_lowest": craft.margin_lowest,
            "margin_at_target": row.margin_per_craft,
            "price_lowest": craft.price_lowest,
            "current_lowest": craft.lowest,
            "target_prices": measured,
            "sale_probability_measured_at": "rank target price",
            "listed_above_measured_price": listed_above,
            "target_above_lowest": row.target_above_lowest,
            "reagent_cost_per_craft": row.reagent_cost_per_craft,
            "crafts_per_hour": row.crafts_per_hour,
            "batch_crafts": ctx.config.batch_crafts,
            "recipe_status": row.status or "unchecked (no character)",
            "professions": row.professions,
        },
    )


def _stat_estimate(stat: OutputStats) -> Fraction | None:
    return None if stat.latest_lowest is None else Fraction(stat.latest_lowest)


def candidate_pathway(ctx: Context, analysis: CraftingAnalysis, candidate: Candidate) -> Pathway | None:
    """A flagged cold-pool candidate as a pathway; None when an output has no listing to undercut."""

    outputs = {stat.item_id: stat.quantity for stat in candidate.outputs}
    prices: dict[int, int] = {}
    for stat in candidate.outputs:
        lowest = _stat_estimate(stat)
        if lowest is None or lowest <= 1:
            return None
        prices[stat.item_id] = int(lowest) - 1
    margin_lowest = margin_at(ctx, outputs, dict(prices), candidate.reagent_cost_per_craft)
    margin_target = candidate.margin_per_craft
    if ctx.config.crafting_price == "lowest":
        margin = margin_lowest
    elif margin_target is None:
        return None
    else:
        margin = margin_target
    limiting = next(stat for stat in candidate.outputs if stat.item_id == candidate.limiting_item_id)
    hours = Fraction(round(limiting.depletion_seconds), SECONDS_PER_HOUR)
    units_per_hour = Fraction(limiting.observed_bottom_depletion_any_gap) / hours if hours else Fraction(0)
    crafts_per_hour = ctx.config.share * units_per_hour / limiting.quantity
    batch = ctx.config.batch_crafts
    evidence = Evidence(
        "demand_intervals",
        limiting.depletion_intervals,
        usable=limiting.depletion_intervals + limiting.unusable_intervals,
        detail="any-gap depletion pairs of the limiting output (cold tier, no P(sale) is measurable)",
    )
    return Pathway(
        strategy="crafting",
        key=f"crafting:{candidate.spell_id}",
        title=_title(ctx, outputs, candidate.spell_id) + " [cold]",
        steps=_steps(
            ctx,
            analysis,
            candidate.spell_id,
            tuple((r.item_id, r.quantity) for r in candidate.reagents),
            outputs,
            prices,
        ),
        profit_per_unit=margin / sum(outputs.values()),
        profit_per_craft=margin,
        volume_per_hour=units_per_hour,
        sale_probability=None,
        evidence=None,
        demand_evidence=evidence,
        listed_units=min(stat.supply for stat in candidate.outputs),
        capital=candidate.reagent_cost_per_craft * batch,
        profit_per_hour=margin * crafts_per_hour,
        confidence=combined_confidence(evidence, cap="low"),
        details={
            "kind": "cold",
            "price_basis": ctx.config.crafting_price,
            "margin_at_lowest": margin_lowest,
            "margin_at_target": margin_target,
            "margin_at_median": candidate.margin_at_median,
            "price_lowest": prices,
            "observation_age_hours": max(stat.latest_age_hours for stat in candidate.outputs),
            "observations": min(stat.observations for stat in candidate.outputs),
            "crafts_per_hour": crafts_per_hour,
            "batch_crafts": batch,
            "recipe_status": candidate.status or "unchecked (no character)",
            "note": "cold tier: prices are the latest observation, not the latest sweep; P(sale) unmeasured",
            "professions": candidate.professions,
        },
    )


class CraftingEvaluator:
    name = "crafting"

    def evaluate(self, ctx: Context) -> StrategyResult:
        analysis = ctx.cached("crafting", lambda: analyze(ctx))
        out = Collector(self.name)
        for craft in analysis.rows:
            row = craft.row
            key = f"crafting:{row.spell_id}"
            if row.status is not None and row.status != "known":
                out.exclude(key, "recipe_not_known", f"recipe status {row.status} (see recipe_purchase)")
                continue
            if headline_margin(ctx, craft) is None:
                out.exclude(key, "no_current_listing", "an output has no current listing to undercut")
                continue
            if row.sale_probability < ctx.config.min_sale_prob:
                out.exclude(
                    key,
                    "low_sale_probability",
                    f"P(sale) {row.sale_probability:.2f} below {ctx.config.min_sale_prob:.2f}",
                )
                continue
            pathway = row_pathway(ctx, analysis, craft)
            if pathway.profit_per_craft is not None and pathway.profit_per_craft <= 0:
                out.exclude(
                    key, "non_positive_profit", f"margin {float(pathway.profit_per_craft):.1f}c per craft"
                )
                continue
            out.keep(pathway)
        for item in analysis.evaluation.excluded:
            key = f"crafting:{item.spell_id}"
            candidate = analysis.candidates.get(item.spell_id)
            if candidate is not None:
                if candidate.status is not None and candidate.status != "known":
                    out.exclude(key, "recipe_not_known", f"recipe status {candidate.status}")
                    continue
                pathway = candidate_pathway(ctx, analysis, candidate)
                if pathway is None:
                    out.exclude(key, "no_current_listing", "cold candidate output has no listing to undercut")
                elif pathway.profit_per_craft is not None and pathway.profit_per_craft <= 0:
                    out.exclude(key, "non_positive_profit", "cold candidate margin is not positive")
                else:
                    out.keep(pathway)
                continue
            detail = item.detail
            if item.spell_id in analysis.candidate_notes:
                detail += f" | {analysis.candidate_notes[item.spell_id]}"
            out.exclude(key, f"rank:{item.reason}", detail)
        return out.result()
