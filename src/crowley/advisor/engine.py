"""Run the evaluators over one shared ``MarketState`` and apply the user's filters."""

from __future__ import annotations

from dataclasses import replace

from crowley.advisor.estimates import EstimateProvider, SnapshotEstimates
from crowley.advisor.evaluators import EVALUATORS, Context
from crowley.advisor.model import (
    SECTIONS,
    STRATEGIES,
    Advice,
    AdviceConfig,
    Exclusion,
    Pathway,
    StrategyResult,
)
from crowley.advisor.state import MarketState


def apply_filters(result: StrategyResult, config: AdviceConfig) -> StrategyResult:
    """Move pathways under ``--min-profit`` / ``--min-volume`` into the exclusions, so they stay counted."""

    kept: list[Pathway] = []
    excluded = list(result.exclusions)
    for pathway in result.pathways:
        if pathway.threshold_profit < config.min_profit:
            detail = f"{float(pathway.threshold_profit):.1f}c below {config.min_profit}c"
            excluded.append(Exclusion(result.strategy, pathway.key, "below_min_profit", detail))
        elif pathway.volume_per_hour < config.min_volume:
            detail = f"{float(pathway.volume_per_hour):.2f}/h below {float(config.min_volume):.2f}/h"
            excluded.append(Exclusion(result.strategy, pathway.key, "below_min_volume", detail))
        else:
            kept.append(pathway)
    return StrategyResult(result.strategy, tuple(kept), tuple(excluded), result.considered, result.notes)


def order(pathways: tuple[Pathway, ...]) -> tuple[Pathway, ...]:
    """Group by section, each by descending ``sort_value``."""

    return tuple(
        sorted(
            pathways, key=lambda pathway: (SECTIONS.index(pathway.section), -pathway.sort_value, pathway.key)
        )
    )


def filter_advice(advice: Advice, config: AdviceConfig) -> Advice:
    """``advice`` under ``config``'s ``min_profit`` / ``min_volume`` (everything else must match).

    The cache stores the unfiltered evaluation, so these two options are applied per request.
    """

    base = replace(advice.config, min_profit=config.min_profit, min_volume=config.min_volume)
    if base != config:
        raise ValueError("filter_advice can only change min_profit and min_volume")
    results = tuple(apply_filters(result, config) for result in advice.results)
    return replace(
        advice,
        config=config,
        results=results,
        pathways=order(tuple(pathway for result in results for pathway in result.pathways)),
    )


def advise(state: MarketState, config: AdviceConfig, estimates: EstimateProvider | None = None) -> Advice:
    """Evaluate the configured strategies against ``state`` (never the database)."""

    ctx = Context(state, SnapshotEstimates(state) if estimates is None else estimates, config)
    results = tuple(EVALUATORS[name].evaluate(ctx) for name in STRATEGIES if name in config.strategies)
    unfiltered = Advice(
        house_key=state.house_key,
        client_build=state.client_build,
        as_of_t=state.as_of_t,
        sweep_id=state.sweep_id,
        config=config,
        results=results,
        pathways=(),
    )
    return filter_advice(unfiltered, config)
