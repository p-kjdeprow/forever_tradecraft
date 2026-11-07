"""Shared recipe requirement validation, capability parsing, and gate logic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .api import CraftingCapabilities, Recipe
from .canonical import sha256_canonical_json

EMPTY_CAPABILITIES = CraftingCapabilities()


def positive_id(value: Any, label: str) -> int:
    """Return a strict positive integer id; bool is intentionally invalid."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _id_set(value: Any, label: str) -> frozenset[int]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array of positive integer ids")
    raw_values = tuple(value)
    normalized: list[int] = []
    seen: set[int] = set()
    for item in raw_values:
        item_id = positive_id(item, label)
        if item_id in seen:
            raise ValueError(f"{label} ids must not contain duplicates")
        seen.add(item_id)
        normalized.append(item_id)
    return frozenset(normalized)


def capabilities_from_mapping(value: Mapping[str, Any] | None) -> CraftingCapabilities:
    """Parse the exact canonical three-array capability JSON object."""

    if value is None:
        raise ValueError("capabilities must be a JSON object")
    if not isinstance(value, Mapping):
        raise ValueError("capabilities must be a JSON object")
    expected = {"tool_item_ids", "tool_category_ids", "spell_focus_ids"}
    if set(value) != expected:
        raise ValueError(
            "capabilities must contain exactly tool_item_ids, tool_category_ids, spell_focus_ids"
        )
    tools = value["tool_item_ids"]
    categories = value["tool_category_ids"]
    focuses = value["spell_focus_ids"]
    return CraftingCapabilities(
        _id_set(tools, "tool item"),
        _id_set(categories, "tool category"),
        _id_set(focuses, "spell focus"),
    )


def capabilities_hash(capabilities: CraftingCapabilities) -> str:
    return sha256_canonical_json(capabilities.canonical)


def recipe_gate(
    recipe: Recipe,
    professions: frozenset[str],
    capabilities: CraftingCapabilities | None = None,
) -> str | None:
    """Return the first unmet gate, or ``None`` when every gate is satisfied."""

    if capabilities is None:
        capabilities = EMPTY_CAPABILITIES
    elif not isinstance(capabilities, CraftingCapabilities):
        raise TypeError("capabilities must be CraftingCapabilities")
    if not recipe.professions.intersection(professions):
        return "profession"
    if any(item_id not in capabilities.tool_item_ids for item_id in recipe.totems):
        return "tool"
    if any(category_id not in capabilities.tool_category_ids for category_id in recipe.totem_categories):
        return "category"
    if recipe.spell_focus is not None and recipe.spell_focus not in capabilities.spell_focus_ids:
        return "spell_focus"
    return None


def recipe_available(
    recipe: Recipe,
    professions: frozenset[str],
    capabilities: CraftingCapabilities | None = None,
) -> bool:
    return recipe_gate(recipe, professions, capabilities) is None


def recipe_gate_counts(
    recipes: Sequence[Recipe],
    professions: frozenset[str],
    capabilities: CraftingCapabilities,
) -> dict[str, int]:
    counts = {
        "total": len(recipes),
        "first_unmet_profession": 0,
        "first_unmet_tool": 0,
        "first_unmet_category": 0,
        "first_unmet_spell_focus": 0,
        "missing_tool": 0,
        "missing_category": 0,
        "missing_spell_focus": 0,
        "available": 0,
        "unavailable": 0,
    }
    for recipe in recipes:
        gate = recipe_gate(recipe, professions, capabilities)
        counts["missing_tool"] += sum(item_id not in capabilities.tool_item_ids for item_id in recipe.totems)
        counts["missing_category"] += sum(
            category_id not in capabilities.tool_category_ids for category_id in recipe.totem_categories
        )
        counts["missing_spell_focus"] += int(
            recipe.spell_focus is not None and recipe.spell_focus not in capabilities.spell_focus_ids
        )
        if gate is None:
            counts["available"] += 1
        else:
            counts[f"first_unmet_{gate}"] += 1
            counts["unavailable"] += 1
    return counts


__all__ = [
    "EMPTY_CAPABILITIES",
    "capabilities_from_mapping",
    "capabilities_hash",
    "positive_id",
    "recipe_available",
    "recipe_gate",
    "recipe_gate_counts",
]
