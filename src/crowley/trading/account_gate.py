"""Classify recipes against a captured character state and restrict crafting to known ones.

Two facts about the data shape this module:

* A recipe's learn requirement is not ``SkillRequirement.min_rank`` (1 for most
  professions in this client).  For item-taught recipes it is the teaching item's required
  skill and rank.  Trainer-taught recipes (no teaching item) and teaching items without
  item data have no requirement in client data, so they classify as ``requirement_unknown``.
* The addon records a skill line's recipes only while that profession's window was open,
  so an empty ``account_recipes`` does not mean "knows nothing".  Only skill lines present
  in the capture's ``recipes`` section count as captured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

from .api import Recipe

RecipeStatus = Literal[
    "known",
    "learnable_now",
    "needs_skill",
    "requirement_unknown",
    "recipes_not_captured",
    "missing_profession",
]


@dataclass(frozen=True)
class AccountSnapshot:
    """One captured character state (the latest capture, or a pinned one)."""

    capture_id: int
    capture_uid: str
    captured_at: datetime
    content_digest: str
    character: str
    known_spell_ids: frozenset[int]
    skill_levels: Mapping[int, int]  # skill line id -> current level
    captured_skill_lines: frozenset[int]  # skill lines whose recipes were recorded

    def metadata(self) -> dict[str, object]:
        return {
            "character": self.character,
            "capture_uid": self.capture_uid,
            "captured_at": self.captured_at.isoformat(),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class RecipeClassification:
    spell_id: int
    status: RecipeStatus
    # Only for ``learnable_now``; teaching items are the only learn path client data shows.
    source: Literal["teaching_item"] | None = None


def _section(raw: Mapping[str, Any], name: str) -> Any:
    value = raw.get(name)
    if isinstance(value, Mapping) and "kind" in value:
        raise RuntimeError(f"account capture {name} section is an error marker: {dict(value)}")
    return value


def load_account_snapshot(
    conn: Any, house_key: str, character: str, capture_uid: str | None = None
) -> AccountSnapshot:
    """Read the latest capture (or the pinned ``capture_uid``) for one house and character.

    Raises when there is no capture or a needed section is an error marker."""

    row = conn.execute(
        """
        SELECT c.id, c.capture_uid, c.captured_at, c.content_digest,
               c.raw->'recipes', c.raw->'professions'
        FROM account_captures AS c
        JOIN houses AS h ON h.id = c.house_id
        WHERE h.key = %s AND c.character = %s AND (%s::text IS NULL OR c.capture_uid = %s)
        ORDER BY c.captured_at DESC, c.id DESC
        LIMIT 1
        """,
        (house_key, character, capture_uid, capture_uid),
    ).fetchone()
    if row is None:
        which = f"capture {capture_uid!r}" if capture_uid else "capture"
        raise RuntimeError(f"no account {which} for character {character!r} in house {house_key!r}")
    capture_id, uid, captured_at, digest, recipes, professions = row
    raw = {"recipes": recipes, "professions": professions}
    recipes = _section(raw, "recipes")
    _section(raw, "professions")
    if not isinstance(recipes, Mapping):
        raise RuntimeError(f"account capture {uid} has no recipes section")
    skills = conn.execute(
        "SELECT skill_line_id, level FROM account_professions WHERE capture_id = %s", (capture_id,)
    ).fetchall()
    spells = conn.execute(
        "SELECT DISTINCT spell_id FROM account_recipes WHERE capture_id = %s", (capture_id,)
    ).fetchall()
    return AccountSnapshot(
        int(capture_id),
        str(uid),
        captured_at,
        str(digest),
        character,
        frozenset(int(spell[0]) for spell in spells),
        MappingProxyType({int(line): int(level) for line, level in skills}),
        frozenset(int(key) for key in recipes),
    )


def classify_recipe(recipe: Recipe, account: AccountSnapshot) -> RecipeClassification:
    acquisition = recipe.acquisition
    if not acquisition.skill_lines:
        raise ValueError(f"recipe {recipe.spell_id} has no skill requirements to classify against")
    if recipe.spell_id in account.known_spell_ids:
        return RecipeClassification(recipe.spell_id, "known")
    lines = {line.skill_line_id for line in acquisition.skill_lines} & set(account.skill_levels)
    if not lines:
        return RecipeClassification(recipe.spell_id, "missing_profession")
    if not lines & account.captured_skill_lines:
        return RecipeClassification(recipe.spell_id, "recipes_not_captured")
    if not acquisition.teaching_requirements:
        return RecipeClassification(recipe.spell_id, "requirement_unknown")
    recipe_lines = {line.skill_line_id for line in acquisition.skill_lines}
    if any(
        requirement.skill_line_id not in recipe_lines for requirement in acquisition.teaching_requirements
    ):
        # Observed in 70862: First Aid manuals require skill line 2942 while their
        # recipes and the captured profession use 129.  The mapping between those
        # lines is not in client data, so the requirement is not decidable here.
        return RecipeClassification(recipe.spell_id, "requirement_unknown")
    for requirement in acquisition.teaching_requirements:
        if account.skill_levels.get(requirement.skill_line_id, -1) >= requirement.rank:
            return RecipeClassification(recipe.spell_id, "learnable_now", "teaching_item")
    # Every known requirement is unmet; a teaching item without item data could still
    # be usable, which client data cannot rule out.
    if len(acquisition.teaching_requirements) < len(acquisition.teaching_item_ids):
        return RecipeClassification(recipe.spell_id, "requirement_unknown")
    return RecipeClassification(recipe.spell_id, "needs_skill")


def classify_recipes(recipes: Sequence[Recipe], account: AccountSnapshot) -> tuple[RecipeClassification, ...]:
    return tuple(classify_recipe(recipe, account) for recipe in recipes)


def require_captured_professions(
    recipes: Sequence[Recipe], account: AccountSnapshot, professions: frozenset[str]
) -> None:
    """Raise unless every profession in use has a captured recipe record for this character."""

    lines: dict[str, set[int]] = {}
    for recipe in recipes:
        for line in recipe.acquisition.skill_lines:
            lines.setdefault(line.profession, set()).add(line.skill_line_id)
    for profession in sorted(professions):
        if profession not in lines:
            raise RuntimeError(f"no client recipes for profession {profession!r}")
        if not lines[profession] & set(account.skill_levels):
            raise RuntimeError(f"capture {account.capture_uid} shows no {profession!r} profession")
        if not lines[profession] & account.captured_skill_lines:
            raise RuntimeError(
                f"capture {account.capture_uid} has no recipe record for {profession!r}; "
                "open that profession's window in game and capture again"
            )


def known_recipes(recipes: Sequence[Recipe], account: AccountSnapshot) -> tuple[Recipe, ...]:
    """The recipes the character has learned; the only ones a gated strategy may craft."""

    return tuple(recipe for recipe in recipes if classify_recipe(recipe, account).status == "known")


__all__ = [
    "AccountSnapshot",
    "RecipeClassification",
    "classify_recipe",
    "classify_recipes",
    "known_recipes",
    "load_account_snapshot",
    "require_captured_professions",
]
