"""Database and statistics loaders for the trading backtester."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, NamedTuple

from crowley.analysis.books import load_commodity_books
from crowley.core.errors import MarketDataNotFound
from crowley.core.watchlist import Catalog, load_catalog

from .api import ItemFacts, Recipe, RecipeAcquisition, SkillRequirement, TeachingRequirement
from .recipes import validate_recipes


def _fixed_integer(value: Any, label: str) -> int:
    decimal = Decimal(str(value))
    if decimal != decimal.to_integral_value():
        raise ValueError(f"{label} is not an integer fixed yield: {value!r}")
    return int(decimal)


class _ClientBuildRow(NamedTuple):
    client_version: str
    client_build: int | str


class _ExportRow(NamedTuple):
    id: int
    client_build: int | str


class _ItemFactsRow(NamedTuple):
    item_id: int
    sell_price: int | None
    buy_price: int | None
    vendor_stack_count: int | None
    max_stack: int | None
    bonding: int | None


class _RecipeRow(NamedTuple):
    spell_id: int
    cooldown_ms: int | None
    category_cooldown_ms: int | None
    category_name: str | None
    learn_evidence: str


class _SkillRow(NamedTuple):
    spell_id: int
    skill_line_id: int
    profession: str
    min_rank: int
    yellow: int
    green: int
    grey: int
    acquire_method: int


class _TeacherRow(NamedTuple):
    spell_id: int
    teaching_item_id: int
    required_skill: int | None
    required_skill_rank: int | None


class _ReagentRow(NamedTuple):
    spell_id: int
    item_id: int
    count: int


class _OutputRow(NamedTuple):
    spell_id: int
    item_id: int
    base_quantity: Decimal | int | float | str
    yield_kind: str


class _TotemRow(NamedTuple):
    spell_id: int
    ordinal: int
    totem_id: int


class _CategoryRow(NamedTuple):
    spell_id: int
    ordinal: int
    category_id: int


class _FocusRow(NamedTuple):
    spell_id: int
    spell_focus_id: int


def catalog_vendor_supply_ids(catalog: Catalog) -> set[int]:
    """Temporarily match vendor supplies by reason prefix pending a catalog field."""

    expected = "The reference route explicitly buys this supply from a vendor"
    return {
        item.item_id
        for item in catalog.items
        if item.status == "excluded" and item.reason.startswith(expected)
    }


def vendor_supply_ids() -> set[int]:
    """Return catalog item IDs matched by the temporary catalog heuristic."""

    return catalog_vendor_supply_ids(load_catalog())


def _client_build(conn: Any, house_key: str) -> str:
    row = conn.execute(
        """
        SELECT ms.client_version, ms.client_build
        FROM market_sweeps AS ms
        JOIN houses AS h ON h.id = ms.house_id
        WHERE h.key = %s
        ORDER BY ms.started_at DESC, ms.id DESC
        LIMIT 1
        """,
        (house_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no market sweeps found for house {house_key!r}")
    parsed = _ClientBuildRow(*row)
    return f"{parsed.client_version}.{parsed.client_build}"


def _latest_export(conn: Any, client_build: str) -> tuple[int, str]:
    row = conn.execute(
        """
        SELECT id, client_build
        FROM latest_client_export
        WHERE client_build = %s
        ORDER BY loaded_at DESC, id DESC
        LIMIT 1
        """,
        (client_build,),
    ).fetchone()
    if row is None:
        raise MarketDataNotFound(f"no latest client export for client build {client_build!r}")
    parsed = _ExportRow(*row)
    return parsed.id, str(parsed.client_build)


def load_facts(conn: Any, house_key: str, client_build: str | None = None) -> dict[int, ItemFacts]:
    """Load migration-005 item facts for the build used by the house's sweeps."""

    client_build = client_build or _client_build(conn, house_key)
    export_id, _ = _latest_export(conn, client_build)
    vendor_ids = vendor_supply_ids()
    rows = tuple(
        _ItemFactsRow(*row)
        for row in conn.execute(
            """
        SELECT item_id, sell_price, buy_price, vendor_stack_count, max_stack, bonding
        FROM client_items
        WHERE export_id = %s AND status = 'ok'
        ORDER BY item_id
        """,
            (export_id,),
        ).fetchall()
    )
    # Items without an ItemSparse row (status 'no_sparse_row', assumption A5) have no client facts;
    # they are absent here, so MarketView.facts() returns None for them.
    facts: dict[int, ItemFacts] = {}
    for row in rows:
        item_id = int(row.item_id)
        sell_value = row.sell_price
        stack_value = row.max_stack
        bonding_value = row.bonding
        if sell_value is None or stack_value is None or bonding_value is None:
            raise ValueError(f"client item {item_id} has status ok but missing facts")
        sell_price = int(sell_value)
        buy_price = row.buy_price
        bundle = row.vendor_stack_count
        max_stack = int(stack_value)
        bonding = int(bonding_value)
        if max_stack <= 0:
            raise ValueError(f"client item {item_id} has no positive max_stack")
        vendor_bundle_size: int | None = None
        vendor_bundle_price: int | None = None
        if item_id in vendor_ids:
            if buy_price is None or bundle is None or int(bundle) <= 0:
                raise ValueError(f"vendor supply {item_id} has no buy price or vendor stack count")
            vendor_bundle_size = int(bundle)
            vendor_bundle_price = int(buy_price)
        fact = ItemFacts(
            item_id,
            sell_price,
            vendor_bundle_size,
            vendor_bundle_price,
            max_stack,
            bonding,
        )
        facts[item_id] = fact
    return facts


def load_recipes(conn: Any, house_key: str, client_build: str | None = None) -> tuple[Recipe, ...]:
    """Load fixed-yield recipes from the latest export matching the house's build."""

    client_build = client_build or _client_build(conn, house_key)
    export_id, _ = _latest_export(conn, client_build)
    recipes = tuple(
        _RecipeRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, cooldown_ms, category_cooldown_ms, category_name, learn_evidence
        FROM client_recipes
        WHERE export_id = %s
        ORDER BY spell_id
        """,
            (export_id,),
        ).fetchall()
    )
    skill_rows = tuple(
        _SkillRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, skill_line_id, profession, min_rank, yellow, green_derived, grey,
               acquire_method
        FROM client_recipe_skill_lines
        WHERE export_id = %s
        ORDER BY spell_id, skill_line_id, ability_id
        """,
            (export_id,),
        ).fetchall()
    )
    teacher_rows = tuple(
        _TeacherRow(*row)
        for row in conn.execute(
            """
        SELECT t.spell_id, t.teaching_item_id, i.required_skill, i.required_skill_rank
        FROM client_recipe_teachers AS t
        LEFT JOIN client_items AS i
          ON i.export_id = t.export_id AND i.item_id = t.teaching_item_id
        WHERE t.export_id = %s
        ORDER BY t.spell_id, t.teaching_item_id
        """,
            (export_id,),
        ).fetchall()
    )
    reagent_rows = tuple(
        _ReagentRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, item_id, count
        FROM client_recipe_reagents
        WHERE export_id = %s
        ORDER BY spell_id, item_id
        """,
            (export_id,),
        ).fetchall()
    )
    output_rows = tuple(
        _OutputRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, item_id, base_quantity, yield_kind
        FROM client_recipe_outputs
        WHERE export_id = %s
        ORDER BY spell_id, item_id
        """,
            (export_id,),
        ).fetchall()
    )
    totem_rows = tuple(
        _TotemRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, ordinal, totem_id
        FROM client_recipe_totems
        WHERE export_id = %s
        ORDER BY spell_id, ordinal
        """,
            (export_id,),
        ).fetchall()
    )
    category_rows = tuple(
        _CategoryRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, ordinal, category_id
        FROM client_recipe_totem_categories
        WHERE export_id = %s
        ORDER BY spell_id, ordinal
        """,
            (export_id,),
        ).fetchall()
    )
    focus_rows = tuple(
        _FocusRow(*row)
        for row in conn.execute(
            """
        SELECT spell_id, spell_focus_id
        FROM client_recipe_spell_focus
        WHERE export_id = %s
        ORDER BY spell_id
        """,
            (export_id,),
        ).fetchall()
    )
    professions: dict[int, set[str]] = {}
    skill_lines: dict[int, list[SkillRequirement]] = {}
    for row in skill_rows:
        professions.setdefault(row.spell_id, set()).add(row.profession)
        requirement = SkillRequirement(
            row.skill_line_id,
            row.profession,
            row.min_rank,
            row.yellow,
            row.green,
            row.grey,
            row.acquire_method,
        )
        lines = skill_lines.setdefault(row.spell_id, [])
        if lines and lines[-1].skill_line_id == row.skill_line_id:
            # The client lists one row per SkillLineAbility; several abilities
            # can teach the same spell in the same skill line.  They must agree.
            if lines[-1] != requirement:
                raise ValueError(
                    f"recipe {row.spell_id} has conflicting requirements in skill line {row.skill_line_id}"
                )
            continue
        lines.append(requirement)
    teachers: dict[int, list[int]] = {}
    teaching: dict[int, list[TeachingRequirement]] = {}
    for row in teacher_rows:
        teachers.setdefault(row.spell_id, []).append(row.teaching_item_id)
        if row.required_skill is not None:
            if row.required_skill_rank is None:
                raise ValueError(f"teaching item {row.teaching_item_id} has a skill but no rank")
            teaching.setdefault(row.spell_id, []).append(
                TeachingRequirement(row.teaching_item_id, row.required_skill, row.required_skill_rank)
            )
    reagents: dict[int, list[tuple[int, int]]] = {}
    for row in reagent_rows:
        reagents.setdefault(row.spell_id, []).append((row.item_id, row.count))
    outputs: dict[int, list[tuple[int, int]]] = {}
    totems: dict[int, list[int]] = {}
    categories: dict[int, list[int]] = {}
    spell_focus: dict[int, int] = {}
    for row in totem_rows:
        totems.setdefault(row.spell_id, []).append(row.totem_id)
    for row in category_rows:
        categories.setdefault(row.spell_id, []).append(row.category_id)
    for row in focus_rows:
        spell_id = row.spell_id
        focus_id = row.spell_focus_id
        if spell_id in spell_focus and spell_focus[spell_id] != focus_id:
            raise ValueError(f"recipe {spell_id} has multiple spell focus ids")
        spell_focus[spell_id] = focus_id
    fixed: dict[int, bool] = {}
    for row in output_rows:
        spell_id = row.spell_id
        if row.yield_kind != "fixed":
            fixed[spell_id] = False
            continue
        fixed.setdefault(spell_id, True)
        outputs.setdefault(spell_id, []).append(
            (
                row.item_id,
                _fixed_integer(row.base_quantity, f"recipe {spell_id} output"),
            )
        )
    result: list[Recipe] = []
    for row in recipes:
        spell_id = row.spell_id
        if not fixed.get(spell_id, False) or not outputs.get(spell_id):
            continue
        category_ms = int(row.category_cooldown_ms or 0)
        spell_ms = int(row.cooldown_ms or 0)
        if category_ms > 0:
            group = row.category_name
            if not group:
                raise ValueError(f"recipe {spell_id} has a category cooldown without a category name")
            cooldown_group = str(group)
            cooldown_seconds = math.ceil(category_ms / 1000)
        elif spell_ms > 0:
            cooldown_group = str(spell_id)
            cooldown_seconds = math.ceil(spell_ms / 1000)
        else:
            cooldown_group = None
            cooldown_seconds = 0
        result.append(
            Recipe(
                spell_id=spell_id,
                professions=frozenset(professions.get(spell_id, set())),
                reagents=tuple(reagents.get(spell_id, [])),
                outputs=tuple(outputs[spell_id]),
                cooldown_group=cooldown_group,
                cooldown_seconds=cooldown_seconds,
                learn_evidence=row.learn_evidence,
                totems=tuple(totems.get(spell_id, ())),
                totem_categories=tuple(categories.get(spell_id, ())),
                spell_focus=spell_focus.get(spell_id),
                acquisition=RecipeAcquisition(
                    tuple(teachers.get(spell_id, ())),
                    tuple(skill_lines.get(spell_id, ())),
                    tuple(teaching.get(spell_id, ())),
                ),
            )
        )
    return validate_recipes(result, client_data=True)


def load_client_data(
    conn: Any, house_key: str, client_build: str | None = None
) -> tuple[dict[int, ItemFacts], tuple[Recipe, ...]]:
    """Load facts and recipes while the caller holds its read-only transaction."""

    client_build = client_build or _client_build(conn, house_key)
    return load_facts(conn, house_key, client_build), load_recipes(conn, house_key, client_build)


__all__ = [
    "load_client_data",
    "load_commodity_books",
    "load_facts",
    "load_recipes",
    "catalog_vendor_supply_ids",
    "vendor_supply_ids",
]
