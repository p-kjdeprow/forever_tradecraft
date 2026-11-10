"""Crafting pathways: ``analysis.rank`` for the hot tier, ``analysis.candidates`` for the cold pool.

Rank supplies the economics it always did (reagent cost, margin at its target price, P(sale)
from independent replay starts, market units/h, capital).  This evaluator adds the price a human
would actually post now.  The headline price of each output is ``min(lowest - 1, rank target)``:
1c under the CURRENT lowest listing, but never above the target the P(sale) was measured at, so
the headline profit and the measured P refer to the same or a lower price.  ``crafting_price=
"target"`` uses rank's target alone.  Both margins stay in the details.

Expected profit per hour is ``margin x crafts/h x P(sale)``: a listing that sells with probability
P within the horizon earns P of its margin per attempt.  A zero P is always excluded.

A cold-pool candidate (``analysis.candidates``) becomes a pathway only for a recipe that rank
excluded (a cold item has no book in the latest sweep, or too few to replay) and that the
candidate view flags.  It has no P(sale) and goes to the ``unmeasured`` section; its volume is the
observed any-gap bottom depletion per hour and its profit per hour is taken at P = 1.  It is
excluded when it has no depletion intervals (``no_observed_demand``) or when any price behind it
was last observed more than ``max_cold_age_hours`` ago (``stale_observation``), and its steps start
with "verify price".  Every recipe rank considered is counted exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil

from crowley.advisor.estimates import PriceEstimate
from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import Evidence, Pathway, Step, StrategyResult, pathway_confidence
from crowley.analysis.candidates import (
    DEFAULT_MIN_OBSERVATIONS,
    Candidate,
    build_candidates,
)
from crowley.analysis.rank import (
    Evaluation,
    RankConfig,
    RankEvaluator,
    TradeRow,
    batch_net_revenue,
    evaluate_trades,
)
from crowley.trading.strategies.common import SECONDS_PER_HOUR
from crowley.trading.strategies.pricing import Source


@dataclass(frozen=True)
class CraftRow:
    """One rank row with the current-lowest repricing next to rank's target."""

    row: TradeRow
    lowest: dict[int, int | None]  # output item -> current lowest listing
    price_undercut: dict[int, int] | None  # output item -> 1c under it; None when any is unpriceable
    price_headline: dict[int, int] | None  # the configured headline price; None when unpriceable
    margin_undercut: Fraction | None  # margin per craft at ``price_undercut``
    margin_headline: Fraction | None  # margin per craft at ``price_headline``


@dataclass(frozen=True)
class CraftingAnalysis:
    evaluation: Evaluation
    rank_config: RankConfig
    rows: tuple[CraftRow, ...]
    candidates: dict[int, Candidate]  # flagged cold-pool candidates by spell id
    candidate_notes: dict[int, str]  # spell id -> why a candidate was not usable
    reagents: RankEvaluator


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
        listing_duration_hours=config.listing_hours,
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
            ctx.state.facts[item_id],
            ctx.state.rules,
            quantity * batch,
            prices[item_id],
            ctx.config.listing_hours,
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
    undercut = {item: _undercut(value) for item, value in lowest.items()}
    outputs = dict(row.outputs)
    target = dict(row.target_prices)
    price_undercut = (
        None
        if any(p is None for p in undercut.values())
        else {i: p for i, p in undercut.items() if p is not None}
    )
    margin_undercut = (
        None
        if price_undercut is None
        else margin_at(ctx, outputs, dict(price_undercut), row.reagent_cost_per_craft)
    )
    if ctx.config.crafting_price == "target":
        headline: dict[int, int] | None = target
    elif price_undercut is None:
        headline = None
    else:
        headline = {item: min(price_undercut[item], target[item]) for item in outputs}
    margin_headline = (
        None if headline is None else margin_at(ctx, outputs, dict(headline), row.reagent_cost_per_craft)
    )
    return CraftRow(row, lowest, price_undercut, headline, margin_undercut, margin_headline)


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
        evaluation, rank_config, rows, usable, notes, RankEvaluator(state.inputs, rank_config)
    )


def headline_margin(craft: CraftRow) -> Fraction | None:
    """The headline margin per craft (None: unpriceable at the current lowest)."""

    return craft.margin_headline


def _buy_step(source: Source, quantity: int) -> Step:
    if source.kind == "ah":
        return Step("buy_ah", item_id=source.item_id, quantity=quantity, price=source.unit_limit)
    return Step(
        "buy_vendor",
        item_id=source.item_id,
        quantity=quantity,
        price=ceil(source.full_cost / quantity),
        note="vendor price, whole bundles",
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
    margin, prices = craft.margin_headline, craft.price_headline
    if margin is None or prices is None:
        raise RuntimeError(f"spell {row.spell_id}: no headline price")
    outputs = dict(row.outputs)
    estimates = [ctx.estimates.estimate(item) for item in outputs]
    demand = _demand_evidence(estimates)
    evidence = Evidence(
        "independent_starts",
        row.independent_starts,
        successes=row.sold_starts,
        usable=row.usable_starts,
        detail="replayed listings at rank's target price, one horizon apart",
    )
    probability = Fraction(row.sale_probability)
    per_hour = margin * row.crafts_per_hour * probability
    return Pathway(
        strategy="crafting",
        section="ranked",
        key=f"crafting:{row.spell_id}",
        title=_title(ctx, outputs, row.spell_id),
        steps=_steps(ctx, analysis, row.spell_id, tuple((i, q) for i, q, _ in row.reagents), outputs, prices),
        profit_per_unit=margin / sum(outputs.values()),
        profit_per_craft=margin,
        volume_per_hour=row.market_units_per_hour,
        sale_probability=row.sale_probability,
        fill_rate=None,
        evidence=evidence,
        demand_evidence=demand,
        listed_units=min((est.listed_units for est in estimates if est is not None), default=None),
        capital=row.capital_per_batch,
        profit_per_hour=per_hour,
        one_off_profit=None,
        sort_value=per_hour,
        confidence=pathway_confidence(evidence, demand),
        details={
            "kind": row.kind,
            "price_basis": ctx.config.crafting_price,
            "headline_prices": prices,
            "margin_at_headline": margin,
            "margin_at_undercut_lowest": craft.margin_undercut,
            "margin_at_target": row.margin_per_craft,
            "price_undercut_lowest": craft.price_undercut,
            "current_lowest": craft.lowest,
            "target_prices": dict(row.target_prices),
            "sale_probability_measured_at": "rank target price (headline price is never above it)",
            "profit_per_hour_formula": "margin x crafts/h x P(sale)",
            "profit_per_hour_at_p1": margin * row.crafts_per_hour,
            "target_above_lowest": row.target_above_lowest,
            "reagent_cost_per_craft": row.reagent_cost_per_craft,
            "crafts_per_hour": row.crafts_per_hour,
            "batch_crafts": ctx.config.batch_crafts,
            "listing_hours": ctx.config.listing_hours,
            "recipe_status": row.status or "unchecked (no character)",
            "professions": row.professions,
        },
    )


@dataclass(frozen=True)
class Rejected:
    reason: str
    detail: str


def candidate_pathway(ctx: Context, analysis: CraftingAnalysis, candidate: Candidate) -> Pathway | Rejected:
    """A flagged cold-pool candidate as a pathway, or the reason it cannot be one."""

    config = ctx.config
    outputs = {stat.item_id: stat.quantity for stat in candidate.outputs}
    reagent_ages = [r.observed_age_hours for r in candidate.reagents if r.observed_age_hours is not None]
    age = max([stat.latest_age_hours for stat in candidate.outputs] + reagent_ages)
    if age > config.max_cold_age_hours:
        return Rejected("stale_observation", f"a price was last observed {age:.1f} h ago")
    undercut: dict[int, int] = {}
    for stat in candidate.outputs:
        if stat.latest_lowest is None or stat.latest_lowest <= 1:
            return Rejected("no_current_listing", f"output {stat.item_id} has no listing to undercut")
        undercut[stat.item_id] = int(stat.latest_lowest) - 1
    targets = {stat.item_id: stat.target_price for stat in candidate.outputs}
    if config.crafting_price == "target":
        if any(price is None for price in targets.values()):
            return Rejected("no_target_price", "an output has no fair value, so no target price")
        prices = {item: price for item, price in targets.items() if price is not None}
    else:
        prices = {item: min(price, targets[item] or price) for item, price in undercut.items()}
    margin = margin_at(ctx, outputs, dict(prices), candidate.reagent_cost_per_craft)
    limiting = next(stat for stat in candidate.outputs if stat.item_id == candidate.limiting_item_id)
    hours = Fraction(round(limiting.depletion_seconds), SECONDS_PER_HOUR)
    if limiting.depletion_intervals == 0 or hours == 0 or limiting.observed_bottom_depletion_any_gap == 0:
        return Rejected("no_observed_demand", "no depletion observed between the cold observations")
    units_per_hour = Fraction(limiting.observed_bottom_depletion_any_gap) / hours
    crafts_per_hour = config.share * units_per_hour / limiting.quantity
    batch = config.batch_crafts
    evidence = Evidence(
        "demand_intervals",
        limiting.depletion_intervals,
        usable=limiting.depletion_intervals + limiting.unusable_intervals,
        detail="any-gap depletion pairs of the limiting output (cold tier, no P(sale) is measurable)",
    )
    steps = (
        Step(
            "verify_price",
            item_id=candidate.limiting_item_id,
            note=f"cold item last observed {age:.1f} h ago: check the current AH prices first",
        ),
        *_steps(
            ctx,
            analysis,
            candidate.spell_id,
            tuple((r.item_id, r.quantity) for r in candidate.reagents),
            outputs,
            prices,
        ),
    )
    per_hour = margin * crafts_per_hour
    return Pathway(
        strategy="crafting",
        section="unmeasured",
        key=f"crafting:{candidate.spell_id}",
        title=_title(ctx, outputs, candidate.spell_id) + " [cold]",
        steps=steps,
        profit_per_unit=margin / sum(outputs.values()),
        profit_per_craft=margin,
        volume_per_hour=units_per_hour,
        sale_probability=None,
        fill_rate=None,
        evidence=None,
        demand_evidence=evidence,
        listed_units=min(stat.supply for stat in candidate.outputs),
        capital=candidate.reagent_cost_per_craft * batch,
        profit_per_hour=per_hour,
        one_off_profit=None,
        sort_value=per_hour,
        confidence=pathway_confidence(None, evidence),
        details={
            "kind": "cold",
            "price_basis": config.crafting_price,
            "headline_prices": prices,
            "margin_at_headline": margin,
            "margin_at_target": candidate.margin_per_craft,
            "margin_at_median": candidate.margin_at_median,
            "price_undercut_lowest": undercut,
            "observation_age_hours": age,
            "observations": min(stat.observations for stat in candidate.outputs),
            "crafts_per_hour": crafts_per_hour,
            "profit_per_hour_formula": "margin x crafts/h at P = 1 (P unmeasured, an upper bound)",
            "batch_crafts": batch,
            "listing_hours": config.listing_hours,
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
            if craft.margin_headline is None:
                out.exclude(key, "no_current_listing", "an output has no current listing to undercut")
                continue
            if row.sale_probability <= 0 or row.sale_probability < ctx.config.min_sale_prob:
                out.exclude(
                    key,
                    "low_sale_probability",
                    f"P(sale) {row.sale_probability:.2f} below {ctx.config.min_sale_prob:.2f}",
                )
                continue
            if craft.margin_headline <= 0:
                out.exclude(
                    key, "non_positive_profit", f"margin {float(craft.margin_headline):.1f}c per craft"
                )
                continue
            out.keep(row_pathway(ctx, analysis, craft))
        for item in analysis.evaluation.excluded:
            key = f"crafting:{item.spell_id}"
            candidate = analysis.candidates.get(item.spell_id)
            if candidate is not None:
                if candidate.status is not None and candidate.status != "known":
                    out.exclude(key, "recipe_not_known", f"recipe status {candidate.status}")
                    continue
                result = candidate_pathway(ctx, analysis, candidate)
                if isinstance(result, Rejected):
                    out.exclude(key, result.reason, result.detail)
                elif result.profit_per_craft is not None and result.profit_per_craft <= 0:
                    out.exclude(key, "non_positive_profit", "cold candidate margin is not positive")
                else:
                    out.keep(result)
                continue
            detail = item.detail
            if item.spell_id in analysis.candidate_notes:
                detail += f" | {analysis.candidate_notes[item.spell_id]}"
            out.exclude(key, f"rank:{item.reason}", detail)
        return out.result()
