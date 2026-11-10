"""Text table and JSON for an ``Advice``."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from fractions import Fraction
from typing import Any

from crowley.advisor.model import Advice, Pathway, Step
from crowley.analysis.rank import _gold


def plain(value: Any) -> Any:
    """JSON-ready: dataclasses to dicts, fractions to numbers (float only when not whole), keys to text."""

    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else round(float(value), 6)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [plain(item) for item in items]
    return value


def describe_step(step: Step, names: dict[int, str]) -> str:
    what = step.action.replace("_", " ")
    if step.item_id is not None:
        label = names.get(step.item_id, f"item {step.item_id}")
        what += (
            f" {step.quantity}x {label} ({step.item_id})" if step.quantity else f" {label} ({step.item_id})"
        )
    if step.spell_id is not None:
        what += f" spell {step.spell_id}" + (f" x{step.quantity}" if step.quantity else "")
    if step.price is not None:
        what += f" {'at or below' if step.action.startswith('buy') else 'at'} {_gold(step.price)}"
    return what + (f" ({step.note})" if step.note else "")


def _probability(pathway: Pathway) -> str:
    if pathway.sale_probability is None:
        return "-"
    evidence = pathway.evidence
    count = f" {evidence.successes}/{evidence.n}" if evidence and evidence.successes is not None else ""
    return f"{pathway.sale_probability:.2f}{count}"


def format_table(advice: Advice, names: dict[int, str], limit: int | None, steps: bool = False) -> str:
    shown = advice.pathways if limit is None else advice.pathways[:limit]
    config = advice.config
    header = (
        f"{'type':<16} {'pathway':<52} {'profit':>10} {'vol/h':>7} {'P (sold/n)':>11} {'listed':>7} "
        f"{'capital':>11} {'profit/h':>11} {'conf':<6}"
    )
    lines = [
        f"house {advice.house_key} build {advice.client_build} sweep {advice.sweep_id} "
        f"share {float(config.share):.0%} horizon {config.horizon_hours:g} h "
        f"strategies {','.join(sorted(config.strategies))}",
        header,
    ]
    for pathway in shown:
        unit = "/craft" if pathway.profit_per_craft is not None else "/unit"
        listed = "-" if pathway.listed_units is None else str(pathway.listed_units)
        lines.append(
            f"{pathway.strategy:<16} {pathway.title[:52]:<52} {_gold(pathway.threshold_profit):>10} "
            f"{float(pathway.volume_per_hour):>7.1f} {_probability(pathway):>11} {listed:>7} "
            f"{_gold(pathway.capital):>11} {_gold(pathway.profit_per_hour):>11} {pathway.confidence:<6}"
            f" {unit}"
        )
        if steps:
            lines.extend(
                f"    {index}. {describe_step(step, names)}" for index, step in enumerate(pathway.steps, 1)
            )
    lines.append(
        f"shown {len(shown)} of {len(advice.pathways)} pathways; profit is per craft (crafting) or per unit"
    )
    for result in advice.results:
        counts = (
            ", ".join(f"{reason} {count}" for reason, count in result.exclusion_counts().items()) or "none"
        )
        lines.append(
            f"  {result.strategy}: considered {result.considered}, pathways {len(result.pathways)}, "
            f"excluded {len(result.exclusions)} ({counts})"
        )
        lines.extend(f"    note: {note}" for note in result.notes)
    return "\n".join(lines)


def advice_payload(
    advice: Advice, names: dict[int, str], limit: int | None, include_exclusions: bool = False
) -> dict[str, Any]:
    shown = advice.pathways if limit is None else advice.pathways[:limit]
    config = advice.config
    return {
        "house": advice.house_key,
        "client_build": advice.client_build,
        "as_of_epoch": advice.as_of_t,
        "sweep_id": advice.sweep_id,
        "parameters": {
            "strategies": sorted(config.strategies),
            "professions": sorted(config.professions),
            "min_profit": config.min_profit,
            "min_volume": config.min_volume,
            "share": config.share,
            "horizon_hours": config.horizon_hours,
            "min_sale_prob": config.min_sale_prob,
            "crafting_price": config.crafting_price,
            "flip_x": config.flip_x,
            "flip_undercut": config.flip_undercut,
        },
        "total_pathways": len(advice.pathways),
        "pathways": [
            {**plain(pathway), "profit_label": _gold(pathway.threshold_profit)} for pathway in shown
        ],
        "strategies": {
            result.strategy: {
                "considered": result.considered,
                "pathways": len(result.pathways),
                "excluded": len(result.exclusions),
                "exclusion_counts": result.exclusion_counts(),
                "notes": list(result.notes),
                **({"exclusions": plain(list(result.exclusions))} if include_exclusions else {}),
            }
            for result in advice.results
        },
        "names": {str(item): name for item, name in names.items() if _used(shown, item)},
    }


def _used(pathways: Any, item_id: int) -> bool:
    return any(step.item_id == item_id for pathway in pathways for step in pathway.steps)


def advice_json(
    advice: Advice, names: dict[int, str], limit: int | None, include_exclusions: bool = False
) -> str:
    return json.dumps(
        plain(advice_payload(advice, names, limit, include_exclusions)), indent=2, sort_keys=True
    )
