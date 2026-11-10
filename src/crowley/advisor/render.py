"""Text tables and JSON for an ``Advice``, one section at a time."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from fractions import Fraction
from typing import Any, cast

from crowley.advisor.model import SECTIONS, Advice, Pathway, Step
from crowley.analysis.rank import gold

SECTION_TITLES = {
    "ranked": "RANKED: recurring crafts with a measured P(sale); profit/h = margin x crafts/h x P(sale)",
    "unmeasured": "UNMEASURED: recurring, P(sale) not measurable (cold tier, vendor to AH); "
    "profit/h taken at P = 1, an upper bound",
    "one_off": "ONE-OFF: opportunities listed right now; value is the expected profit of doing it once",
    "farming": "FARMING VALUE: gatherable goods; market value/h is the whole market's capacity, "
    "not your income",
}
# per section: (profit column, volume column, evidence column, last column) headers
COLUMNS = {
    "ranked": ("profit", "vol/h", "P (sold/n)", "profit/h"),
    "unmeasured": ("profit", "vol/h", "P", "profit/h@P1"),
    "one_off": ("profit/unit", "vol/h", "unit fill (n)", "one-off"),
    "farming": ("net/unit", "vol/h", "P", "mkt value/h"),
}


def plain(value: Any) -> Any:
    """JSON-ready: dataclasses to dicts, fractions to numbers (float only when not whole), keys to text."""

    if is_dataclass(value) and not isinstance(value, type):
        return plain(asdict(value))
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else round(float(value), 6)
    if isinstance(value, dict):
        mapping = cast("dict[object, Any]", value)
        return {str(key): plain(item) for key, item in mapping.items()}
    if isinstance(value, (set, frozenset)):
        return [plain(item) for item in sorted(cast("set[Any]", value))]
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in cast("Sequence[Any]", value)]
    return value


def describe_step(step: Step, names: Mapping[int, str]) -> str:
    what = step.action.replace("_", " ")
    if step.item_id is not None:
        label = names.get(step.item_id, f"item {step.item_id}")
        what += (
            f" {step.quantity}x {label} ({step.item_id})" if step.quantity else f" {label} ({step.item_id})"
        )
    if step.spell_id is not None:
        what += f" spell {step.spell_id}" + (f" x{step.quantity}" if step.quantity else "")
    if step.price is not None:
        what += f" {'at or below' if step.action.startswith('buy') else 'at'} {gold(step.price)}"
    return what + (f" ({step.note})" if step.note else "")


def _evidence_cell(pathway: Pathway) -> str:
    evidence = pathway.evidence
    if pathway.section == "one_off":
        if pathway.fill_rate is None:
            return "-"
        return f"{pathway.fill_rate:.2f} ({evidence.n if evidence else 0})"
    if pathway.sale_probability is None:
        return "-"
    count = f" {evidence.successes}/{evidence.n}" if evidence and evidence.successes is not None else ""
    return f"{pathway.sale_probability:.2f}{count}"


def _last_cell(pathway: Pathway) -> Fraction:
    if pathway.section == "one_off":
        value = pathway.one_off_profit
    elif pathway.section == "farming":
        value = pathway.sort_value
    else:
        value = pathway.profit_per_hour
    if value is None:
        raise RuntimeError(f"pathway {pathway.key} has no value for section {pathway.section}")
    return value


def format_section(
    name: str, pathways: tuple[Pathway, ...], limit: int | None, names: Mapping[int, str], steps: bool
) -> list[str]:
    shown = pathways if limit is None else pathways[:limit]
    profit, volume, evidence, last = COLUMNS[name]
    lines = [
        "",
        SECTION_TITLES[name],
        f"{'type':<16} {'pathway':<52} {profit:>11} {volume:>7} {evidence:>14} {'listed':>7} "
        f"{'capital':>11} {last:>12} {'conf':<6}",
    ]
    for pathway in shown:
        listed = "-" if pathway.listed_units is None else str(pathway.listed_units)
        unit = "/craft" if pathway.profit_per_craft is not None else "/unit"
        lines.append(
            f"{pathway.strategy:<16} {pathway.title[:52]:<52} {gold(pathway.threshold_profit):>11} "
            f"{float(pathway.volume_per_hour):>7.1f} {_evidence_cell(pathway):>14} {listed:>7} "
            f"{gold(pathway.capital):>11} {gold(_last_cell(pathway)):>12} {pathway.confidence:<6} {unit}"
        )
        if steps:
            lines.extend(f"    {i}. {describe_step(step, names)}" for i, step in enumerate(pathway.steps, 1))
    lines.append(f"  shown {len(shown)} of {len(pathways)}")
    return lines


def format_table(advice: Advice, names: Mapping[int, str], limit: int | None, steps: bool = False) -> str:
    config = advice.config
    lines = [
        f"house {advice.house_key} build {advice.client_build} sweep {advice.sweep_id} "
        f"share {float(config.share):.0%} horizon {config.horizon_hours:g} h "
        f"strategies {','.join(sorted(config.strategies))}",
        "profit is per craft (crafting) or per unit; --limit applies to each section",
    ]
    for name in SECTIONS:
        section = advice.section(name)
        if section:
            lines.extend(format_section(name, section, limit, names, steps))
    lines.append("")
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
    advice: Advice, names: Mapping[int, str], limit: int | None, include_exclusions: bool = False
) -> dict[str, Any]:
    config = advice.config
    sections = {name: advice.section(name) for name in SECTIONS}
    shown = {name: (items if limit is None else items[:limit]) for name, items in sections.items()}
    used = {step.item_id for items in shown.values() for p in items for step in p.steps}
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
            "listing_hours": config.listing_hours,
            "max_cold_age_hours": config.max_cold_age_hours,
            "flip_x": config.flip_x,
            "flip_undercut": config.flip_undercut,
        },
        "total_pathways": len(advice.pathways),
        "section_totals": {name: len(items) for name, items in sections.items()},
        "sections": {
            name: [{**plain(p), "profit_label": gold(p.threshold_profit)} for p in items]
            for name, items in shown.items()
        },
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
        "names": {str(item): name for item, name in names.items() if item in used},
    }


def advice_json(
    advice: Advice, names: Mapping[int, str], limit: int | None, include_exclusions: bool = False
) -> str:
    return json.dumps(
        plain(advice_payload(advice, names, limit, include_exclusions)), indent=2, sort_keys=True
    )
