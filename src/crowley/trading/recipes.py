"""Validated fixed-yield recipe contracts."""

from __future__ import annotations

from collections.abc import Sequence

from .api import Recipe
from .requirements import positive_id


def validate_recipes(recipes: Sequence[Recipe]) -> tuple[Recipe, ...]:
    """Validate and normalize recipe quantities before account mutation."""

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
            )
        )
    return tuple(normalized)


__all__ = ["validate_recipes"]
