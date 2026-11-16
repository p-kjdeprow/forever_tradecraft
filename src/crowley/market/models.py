"""Client facts and fixed-yield recipe models used by market analysis."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ItemFacts:
    """Static client facts needed to price reagent purchases and listings."""

    item_id: int
    sell_price: int
    vendor_bundle_size: int | None
    vendor_bundle_price: int | None
    max_stack: int
    bonding: int


@dataclass(frozen=True)
class SkillRequirement:
    """A profession skill line associated with a recipe."""

    skill_line_id: int
    profession: str
    min_rank: int
    yellow: int
    green: int
    grey: int
    acquire_method: int


@dataclass(frozen=True)
class TeachingRequirement:
    """Skill rank required to learn a recipe from a teaching item."""

    item_id: int
    skill_line_id: int
    rank: int


@dataclass(frozen=True)
class RecipeAcquisition:
    """Client evidence describing how a recipe is learned."""

    teaching_item_ids: tuple[int, ...] = ()
    skill_lines: tuple[SkillRequirement, ...] = ()
    teaching_requirements: tuple[TeachingRequirement, ...] = ()


@dataclass(frozen=True)
class Recipe:
    """A fixed-yield recipe with its client requirements."""

    spell_id: int
    professions: frozenset[str]
    reagents: tuple[tuple[int, int], ...]
    outputs: tuple[tuple[int, int], ...]
    cooldown_group: str | None
    cooldown_seconds: int
    learn_evidence: str
    totems: tuple[int, ...] = ()
    totem_categories: tuple[int, ...] = ()
    spell_focus: int | None = None
    acquisition: RecipeAcquisition = RecipeAcquisition()


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_acquisition(
    recipe: Recipe,
    acquisition: RecipeAcquisition,
    professions: frozenset[str],
    spell_id: int,
    client_data: bool,
) -> RecipeAcquisition:
    label = f"recipe {spell_id}"
    teachers = tuple(
        _positive_id(value, f"{label} teaching item id") for value in acquisition.teaching_item_ids
    )
    if list(teachers) != sorted(set(teachers)):
        raise ValueError(f"{label} teaching item ids must be sorted and unique")
    line_ids: list[int] = []
    for line in acquisition.skill_lines:
        line_ids.append(_positive_id(line.skill_line_id, f"{label} skill line id"))
        for name, value in (
            ("min_rank", line.min_rank),
            ("yellow", line.yellow),
            ("green", line.green),
            ("grey", line.grey),
            ("acquire_method", line.acquire_method),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"{label} skill line {line.skill_line_id} {name} must be a non-negative integer"
                )
    if line_ids != sorted(set(line_ids)):
        raise ValueError(f"{label} skill lines must be sorted and unique by skill line id")
    if acquisition.skill_lines and {line.profession for line in acquisition.skill_lines} != professions:
        raise ValueError(f"{label} skill lines do not match its professions")
    requirements = acquisition.teaching_requirements
    for requirement in requirements:
        _positive_id(requirement.skill_line_id, f"{label} teaching item skill line id")
        if (
            isinstance(requirement.rank, bool)
            or not isinstance(requirement.rank, int)
            or requirement.rank < 0
        ):
            raise ValueError(
                f"{label} teaching item {requirement.item_id} rank must be a non-negative integer"
            )
    item_ids = [requirement.item_id for requirement in requirements]
    if item_ids != sorted(set(item_ids)) or not set(item_ids) <= set(teachers):
        raise ValueError(f"{label} teaching requirements must be sorted, unique and for its teaching items")
    if client_data:
        if professions and not acquisition.skill_lines:
            raise ValueError(f"{label} has professions but no skill lines")
        if recipe.learn_evidence not in {
            "teaching_item_with_item_data",
            "teaching_item_without_item_data",
            "no_teaching_item",
        }:
            raise ValueError(f"{label} has unknown learn evidence {recipe.learn_evidence!r}")
        if (recipe.learn_evidence == "no_teaching_item") != (not teachers):
            raise ValueError(
                f"{label} learn evidence {recipe.learn_evidence!r} contradicts its teaching items"
            )
        if (recipe.learn_evidence == "teaching_item_with_item_data") != bool(requirements):
            raise ValueError(
                f"{label} learn evidence {recipe.learn_evidence!r} contradicts its teaching requirements"
            )
    return RecipeAcquisition(teachers, tuple(acquisition.skill_lines), tuple(requirements))


def validate_recipes(recipes: Sequence[Recipe], *, client_data: bool = False) -> tuple[Recipe, ...]:
    """Validate and normalize recipe quantities from client data."""

    def normalize(
        entries: Sequence[tuple[int, int]], label: str, spell_id: int
    ) -> tuple[tuple[int, int], ...]:
        totals: dict[int, int] = {}
        order: list[int] = []
        for raw_item_id, raw_quantity in entries:
            item_id = _positive_id(raw_item_id, f"recipe {spell_id} {label} item id")
            if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, int) or raw_quantity <= 0:
                raise ValueError(f"recipe {spell_id} {label} quantity must be a positive integer")
            if item_id not in totals:
                order.append(item_id)
                totals[item_id] = 0
            totals[item_id] += raw_quantity
        return tuple((item_id, totals[item_id]) for item_id in order)

    normalized: list[Recipe] = []
    seen_spells: set[int] = set()
    for recipe in recipes:
        spell_id = _positive_id(recipe.spell_id, "recipe spell id")
        if spell_id in seen_spells:
            raise ValueError(f"duplicate recipe spell id {spell_id}")
        seen_spells.add(spell_id)
        reagents = normalize(recipe.reagents, "reagent", spell_id)
        outputs = normalize(recipe.outputs, "output", spell_id)
        totems = tuple(_positive_id(value, f"recipe {spell_id} tool id") for value in recipe.totems)
        categories = tuple(
            _positive_id(value, f"recipe {spell_id} tool category id") for value in recipe.totem_categories
        )
        spell_focus = recipe.spell_focus
        if spell_focus is not None:
            spell_focus = _positive_id(spell_focus, f"recipe {spell_id} spell focus id")
        if isinstance(recipe.cooldown_seconds, bool) or not isinstance(recipe.cooldown_seconds, int):
            raise ValueError(f"recipe {spell_id} has invalid cooldown seconds")
        if recipe.cooldown_seconds < 0:
            raise ValueError(f"recipe {spell_id} has invalid cooldown seconds")
        normalized.append(
            Recipe(
                spell_id=spell_id,
                professions=frozenset(recipe.professions),
                reagents=reagents,
                outputs=outputs,
                cooldown_group=recipe.cooldown_group,
                cooldown_seconds=recipe.cooldown_seconds,
                learn_evidence=recipe.learn_evidence,
                totems=totems,
                totem_categories=categories,
                spell_focus=spell_focus,
                acquisition=_validate_acquisition(
                    recipe, recipe.acquisition, frozenset(recipe.professions), spell_id, client_data
                ),
            )
        )
    return tuple(normalized)


__all__ = [
    "ItemFacts",
    "Recipe",
    "RecipeAcquisition",
    "SkillRequirement",
    "TeachingRequirement",
    "validate_recipes",
]
