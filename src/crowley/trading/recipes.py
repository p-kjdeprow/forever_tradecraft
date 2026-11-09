"""Validated fixed-yield recipe contracts."""

from __future__ import annotations

from collections.abc import Sequence

from .api import Recipe, RecipeAcquisition
from .requirements import positive_id

CLIENT_LEARN_EVIDENCE = frozenset(
    {"teaching_item_with_item_data", "teaching_item_without_item_data", "no_teaching_item"}
)


def _validate_acquisition(
    recipe: Recipe,
    acquisition: RecipeAcquisition,
    professions: frozenset[str],
    spell_id: int,
    client_data: bool,
) -> RecipeAcquisition:
    label = f"recipe {spell_id}"
    teachers = tuple(
        positive_id(value, f"{label} teaching item id") for value in acquisition.teaching_item_ids
    )
    if list(teachers) != sorted(set(teachers)):
        raise ValueError(f"{label} teaching item ids must be sorted and unique")
    line_ids: list[int] = []
    for line in acquisition.skill_lines:
        line_ids.append(positive_id(line.skill_line_id, f"{label} skill line id"))
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
        positive_id(requirement.skill_line_id, f"{label} teaching item skill line id")
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
        evidence = recipe.learn_evidence
        if evidence not in CLIENT_LEARN_EVIDENCE:
            raise ValueError(f"{label} has unknown learn evidence {evidence!r}")
        if (evidence == "no_teaching_item") != (not teachers):
            raise ValueError(f"{label} learn evidence {evidence!r} contradicts its teaching items")
        if (evidence == "teaching_item_with_item_data") != bool(requirements):
            raise ValueError(f"{label} learn evidence {evidence!r} contradicts its teaching requirements")
    return RecipeAcquisition(teachers, tuple(acquisition.skill_lines), tuple(requirements))


def validate_recipes(recipes: Sequence[Recipe], *, client_data: bool = False) -> tuple[Recipe, ...]:
    """Validate and normalize recipe quantities before account mutation.

    ``client_data`` is set for recipes loaded from the client export: they must then carry
    skill lines and learn evidence consistent with their teaching items."""

    def normalize(
        entries: Sequence[tuple[int, int]], label: str, spell_id: int
    ) -> tuple[tuple[int, int], ...]:
        totals: dict[int, int] = {}
        order: list[int] = []
        for raw_item_id, raw_quantity in entries:
            item_id = positive_id(raw_item_id, f"recipe {spell_id} {label} item id")
            if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, int):
                raise ValueError(f"recipe {spell_id} {label} quantity must be an integer")
            quantity = raw_quantity
            if item_id <= 0 or quantity <= 0:
                raise ValueError(f"recipe {spell_id} has non-positive {label} quantity")
            if item_id not in totals:
                order.append(item_id)
                totals[item_id] = 0
            totals[item_id] += quantity
        return tuple((item_id, totals[item_id]) for item_id in order)

    normalized: list[Recipe] = []
    seen_spells: set[int] = set()
    for recipe in recipes:
        spell_id = positive_id(recipe.spell_id, "recipe spell id")
        if spell_id in seen_spells:
            raise ValueError(f"duplicate recipe spell id {spell_id}")
        seen_spells.add(spell_id)
        reagents = normalize(recipe.reagents, "reagent", recipe.spell_id)
        outputs = normalize(recipe.outputs, "output", recipe.spell_id)
        totems = tuple(positive_id(value, f"recipe {spell_id} tool id") for value in recipe.totems)
        categories = tuple(
            positive_id(value, f"recipe {spell_id} tool category id") for value in recipe.totem_categories
        )
        spell_focus = recipe.spell_focus
        if spell_focus is not None:
            spell_focus = positive_id(spell_focus, f"recipe {spell_id} spell focus id")
        if isinstance(recipe.cooldown_seconds, bool) or int(recipe.cooldown_seconds) < 0:
            raise ValueError(f"recipe {spell_id} has invalid cooldown seconds")
        normalized.append(
            Recipe(
                spell_id=spell_id,
                professions=frozenset(recipe.professions),
                reagents=reagents,
                outputs=outputs,
                cooldown_group=recipe.cooldown_group,
                cooldown_seconds=int(recipe.cooldown_seconds),
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


__all__ = ["validate_recipes"]
