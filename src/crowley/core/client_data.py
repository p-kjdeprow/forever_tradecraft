"""Normalize the local client's DB2 export into recipe and item facts.

The client is the only source used here.  Names are carried only as display
labels; all joins use numeric IDs.  A profession recipe is a
``SkillLineAbility`` row on one of :data:`PROFESSION_SKILL_LINES` whose spell
has at least one ``SpellEffect`` row with ``Effect == 24``.  Client data does
not determine whether a recipe is available in Forever: the shared client
contains Season-of-Discovery content and omits the server's profession-window
decision.  Availability will come from in-game evidence (the trainer window
and recipe items observed on the AH or vendors).  The evidence fields here
only say whether a TriggerType-6 teaching item was found and whether that item
has an ItemSparse row.  ``compare_profession_pages`` is a report-only
comparison with locally saved pages; it never changes normalized data and
never fetches pages.

Assumptions are named constants so callers can copy them into operational
documentation:

* A1: DBCache status-3 entries are deletes.
* A2: an Effect-24 quantity of zero means one.
* A3: green is derived as ``floor((yellow + grey) / 2)``.
* A4: BuyPrice is for VendorStackCount units.
* A5: an absent ItemSparse row is retained as ``no_sparse_row``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .game_target import packaged_game_target
from .watchlist import Catalog, load_catalog

EXPECTED_CLIENT_BUILD = packaged_game_target().full_build
PROFESSION_SKILL_LINES: dict[int, str] = {
    171: "alchemy",
    164: "blacksmithing",
    333: "enchanting",
    202: "engineering",
    165: "leatherworking",
    197: "tailoring",
    185: "cooking",
    129: "first aid",
}
PROFESSION_NAMES = {
    171: "Alchemy",
    164: "Blacksmithing",
    333: "Enchanting",
    202: "Engineering",
    165: "Leatherworking",
    197: "Tailoring",
    185: "Cooking",
    129: "First Aid",
}

ASSUMPTION_A1 = "DBCache status-3 entries are deletes."
ASSUMPTION_A2 = "Effect-24 quantity 0 means 1."
ASSUMPTION_A3 = "green_derived is floor((yellow + grey) / 2)."
ASSUMPTION_A4 = "BuyPrice is per VendorStackCount units."
ASSUMPTION_A5 = "Missing ItemSparse rows are retained with status no_sparse_row."
ASSUMPTIONS = {
    "A1": ASSUMPTION_A1,
    "A2": ASSUMPTION_A2,
    "A3": ASSUMPTION_A3,
    "A4": ASSUMPTION_A4,
    "A5": ASSUMPTION_A5,
}


class ClientDataError(ValueError):
    """Raised when an export violates a client-data contract."""


@dataclass(frozen=True)
class Reagent:
    item_id: int
    count: int


@dataclass(frozen=True)
class Output:
    item_id: int
    base_quantity: Decimal
    variance: Decimal
    yield_kind: str


@dataclass(frozen=True)
class ItemFact:
    item_id: int
    status: str
    name: str | None
    quality: int | None
    item_level: int | None
    class_id: int | None
    subclass_id: int | None
    sell_price: int | None
    buy_price: int | None
    vendor_stack_count: int | None
    bonding: int | None
    max_stack: int | None
    required_skill: int | None
    required_skill_rank: int | None


@dataclass(frozen=True)
class RecipeSkillLine:
    ability_id: int
    spell_id: int
    skill_line_id: int
    profession: str
    min_rank: int
    yellow: int
    green_derived: int
    grey: int
    acquire_method: int
    flags: int
    supersedes_spell: int | None


@dataclass(frozen=True)
class Recipe:
    spell_id: int
    name: str | None
    kind: str
    learn_kind: str
    learn_evidence: str
    associations: tuple[RecipeSkillLine, ...] = ()
    reagents: tuple[Reagent, ...] = ()
    outputs: tuple[Output, ...] = ()
    teaching_items: tuple[int, ...] = ()
    cooldown_ms: int | None = None
    category_cooldown_ms: int | None = None
    category_id: int | None = None
    category_name: str | None = None
    totems: tuple[int, ...] = ()
    totem_categories: tuple[int, ...] = ()
    spell_focus: int | None = None


@dataclass(frozen=True)
class NormalizedExport:
    manifest: dict[str, Any]
    recipes: tuple[Recipe, ...]
    items: tuple[ItemFact, ...]

    @property
    def recipes_by_profession(self) -> dict[str, tuple[RecipeSkillLine, ...]]:
        result: dict[str, tuple[RecipeSkillLine, ...]] = {}
        for profession in PROFESSION_SKILL_LINES.values():
            result[profession] = tuple(
                association
                for recipe in self.recipes
                for association in recipe.associations
                if association.profession == profession
            )
        return result

    @property
    def recipe_associations(self) -> tuple[RecipeSkillLine, ...]:
        return tuple(association for recipe in self.recipes for association in recipe.associations)

    @property
    def random_yield_count(self) -> int:
        return sum(1 for recipe in self.recipes for output in recipe.outputs if output.yield_kind == "random")

    @property
    def no_sparse_row_count(self) -> int:
        return sum(item.status == "no_sparse_row" for item in self.items)


@dataclass(frozen=True)
class ProfessionPageComparison:
    profession: str
    client_count: int
    expected_count: int
    client_only: tuple[int, ...]
    expected_only: tuple[int, ...]
    evidence_counts: dict[str, int]


def _int(value: Any, *, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise ClientDataError(f"expected integer, got {value!r}") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _int(value)


def _decimal(value: Any, *, default: Decimal = Decimal(0)) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ClientDataError(f"expected decimal, got {value!r}") from exc


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "Item": (
        "ID",
        "ClassID",
        "SubclassID",
        "Material",
        "InventoryType",
        "SheatheType",
        "ItemPetFoodID",
        "Sound_override_subclassID",
        "IconFileDataID",
        "ItemGroupSoundsID",
        "ContentTuningID",
        "ModifiedCraftingReagentItemID",
        "AmmunitionType",
        "CraftingQualityID",
        "ItemSquishEraID",
        "RecraftReagentCountPercentage",
        "OrderSource",
    ),
    "ItemEffect": (
        "ID",
        "LegacySlotIndex",
        "TriggerType",
        "Charges",
        "CoolDownMSec",
        "CategoryCoolDownMSec",
        "SpellCategoryID",
        "SpellID",
        "ChrSpecializationID",
        "PlayerConditionID",
    ),
    "ItemSparse": (
        "ID",
        "Description_lang",
        "Display3_lang",
        "Display2_lang",
        "Display1_lang",
        "Display_lang",
        "ExpansionID",
        "DmgVariance",
        "LimitCategory",
        "DurationInInventory",
        "QualityModifier",
        "BagFamily",
        "StartQuestID",
        "LanguageID",
        "ItemRange",
        *(f"StatPercentageOfSocket[{index}]" for index in range(10)),
        *(f"StatPercentEditor[{index}]" for index in range(10)),
        *(f"StatModifier_bonusStat[{index}]" for index in range(10)),
        "Stackable",
        "MaxCount",
        "MinReputation",
        "RequiredAbility",
        "AllowableRace[0]",
        "AllowableRace[1]",
        "SellPrice",
        "BuyPrice",
        "VendorStackCount",
        "PriceVariance",
        "PriceRandomValue",
        *(f"Flags[{index}]" for index in range(5)),
        "OppositeFactionItemID",
        "ModifiedCraftingReagentItemID",
        "ContentTuningID",
        "PlayerLevelToItemLevelCurveID",
        "ItemLevelOffsetCurveID",
        "ItemLevelOffsetItemLevel",
        "ItemSquishEraID",
        "ItemNameDescriptionID",
        "RequiredTransmogHoliday",
        "RequiredHoliday",
        "Gem_properties",
        "Socket_match_enchantment_ID",
        "TotemCategoryID",
        "InstanceBound",
        "ZoneBound[0]",
        "ZoneBound[1]",
        "ItemSet",
        "LockID",
        "PageID",
        "ItemDelay",
        "MinFactionID",
        "RequiredSkillRank",
        "RequiredSkill",
        "ItemLevel",
        "AllowableClass",
        "ArtifactID",
        "SpellWeight",
        "SpellWeightCategory",
        "SocketType[0]",
        "SocketType[1]",
        "SocketType[2]",
        "SheatheType",
        "Material",
        "PageMaterialID",
        "Bonding",
        "DamageType",
        "ContainerSlots",
        "RequiredPVPMedal",
        "RequiredPVPRank",
        "RequiredLevel",
        "InventoryType",
        "OverallQualityID",
        "AmmunitionType",
    ),
    "ItemXItemEffect": ("ID", "ItemEffectID", "ItemID"),
    "SkillLine": (
        "DisplayName_lang",
        "AlternateVerb_lang",
        "Description_lang",
        "HordeDisplayName_lang",
        "NeutralDisplayName",
        "ID",
        "CategoryID",
        "SpellIconFileID",
        "CanLink",
        "ParentSkillLineID",
        "ParentTierIndex",
        "Flags",
        "SpellBookSpellID",
        "ExpansionNameSharedStringID",
        "HordeExpansionNameSharedStringID",
    ),
    "SkillLineAbility": (
        "AbilityVerb_lang",
        "AbilityAllVerb_lang",
        "ID",
        "SkillLine",
        "Spell",
        "MinSkillLineRank",
        "ClassMask",
        "SupercedesSpell",
        "AcquireMethod",
        "TrivialSkillLineRankHigh",
        "TrivialSkillLineRankLow",
        "Flags",
        "NumSkillUps",
        "UniqueBit",
        "TradeSkillCategoryID",
        "SkillupSkillLineID",
        "Field_5_5_4_67090_014[0]",
        "Field_5_5_4_67090_014[1]",
        "RaceMasks[0]",
        "RaceMasks[1]",
    ),
    "Spell": ("ID", "NameSubtext_lang", "Description_lang", "AuraDescription_lang"),
    "SpellCastingRequirements": (
        "ID",
        "SpellID",
        "FacingCasterFlags",
        "MinFactionID",
        "MinReputation",
        "RequiredAreasID",
        "RequiredAuraVision",
        "RequiresSpellFocus",
    ),
    "SpellCategories": (
        "ID",
        "DifficultyID",
        "Category",
        "DefenseType",
        "DiminishType",
        "DispelType",
        "Mechanic",
        "PreventionType",
        "StartRecoveryCategory",
        "ChargeCategory",
        "SpellID",
    ),
    "SpellCategory": (
        "ID",
        "Name_lang",
        "Flags",
        "UsesPerWeek",
        "MaxCharges",
        "ChargeRecoveryTime",
        "TypeMask",
    ),
    "SpellCooldowns": (
        "ID",
        "DifficultyID",
        "CategoryRecoveryTime",
        "RecoveryTime",
        "StartRecoveryTime",
        "AuraSpellID",
        "SpellID",
    ),
    "SpellEffect": (
        "ID",
        "EffectAura",
        "DifficultyID",
        "EffectIndex",
        "Effect",
        "EffectAmplitude",
        "EffectAttributes",
        "EffectAuraPeriod",
        "EffectBonusCoefficient",
        "EffectChainAmplitude",
        "EffectChainTargets",
        "EffectItemType",
        "EffectMechanic",
        "EffectPointsPerResource",
        "EffectPos_facing",
        "EffectRealPointsPerLevel",
        "EffectTriggerSpell",
        "BonusCoefficientFromAP",
        "PvpMultiplier",
        "Coefficient",
        "Variance",
        "ResourceCoefficient",
        "GroupSizeBasePointsCoefficient",
        "EffectBasePointsF",
        "ScalingClass",
        "Node__Field_12_0_0_63534_001",
        "EffectMiscValue[0]",
        "EffectMiscValue[1]",
        "EffectRadiusIndex[0]",
        "EffectRadiusIndex[1]",
        "EffectSpellClassMask[0]",
        "EffectSpellClassMask[1]",
        "EffectSpellClassMask[2]",
        "EffectSpellClassMask[3]",
        "ImplicitTarget[0]",
        "ImplicitTarget[1]",
        "SpellID",
    ),
    "SpellName": ("ID", "Name_lang"),
    "SpellReagents": (
        "ID",
        "SpellID",
        *(f"Reagent[{index}]" for index in range(8)),
        *(f"ReagentCount[{index}]" for index in range(8)),
        *(f"ReagentReCraftCount[{index}]" for index in range(8)),
        *(f"ReagentSource[{index}]" for index in range(8)),
    ),
    "SpellTotems": (
        "ID",
        "SpellID",
        "RequiredTotemCategoryID[0]",
        "RequiredTotemCategoryID[1]",
        "Totem[0]",
        "Totem[1]",
    ),
}


def _field(row: Mapping[str, Any], name: str, *, default: Any = None) -> Any:
    if name in row:
        return row[name]
    if not row and default is not None:
        return default
    raise ClientDataError(f"row is missing required column {name!r}")


def _rows(export_dir: Path, table: str) -> list[dict[str, str]]:
    path = export_dir / "csv" / f"{table}.csv"
    if not path.exists():
        raise ClientDataError(f"export is missing required table {table}.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ClientDataError(f"export table {table}.csv is empty") from exc
        expected = list(TABLE_COLUMNS[table])
        if header != expected:
            raise ClientDataError(f"export table {table}.csv has columns {header!r}; expected {expected!r}")
        rows = []
        for values in reader:
            if len(values) != len(header):
                raise ClientDataError(f"export table {table}.csv has a row with the wrong number of columns")
            rows.append(dict(zip(header, values, strict=True)))
        return rows


def _manifest(export_dir: Path, supplied: Mapping[str, Any] | str | Path | None = None) -> dict[str, Any]:
    if supplied is not None:
        if isinstance(supplied, (str, Path)):
            value = json.loads(Path(supplied).read_text(encoding="utf-8"))
        else:
            value = dict(supplied)
        if not isinstance(value, dict):
            raise ClientDataError("manifest must be a JSON object")
        build = value.get("client_build")
        if build != EXPECTED_CLIENT_BUILD:
            raise ClientDataError(f"manifest client build {build!r} != {EXPECTED_CLIENT_BUILD}")
        return value
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.exists():
        raise ClientDataError("export is missing manifest.json")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ClientDataError("manifest must be a JSON object")
    build = value.get("client_build")
    if build != EXPECTED_CLIENT_BUILD:
        raise ClientDataError(f"manifest client build {build!r} != {EXPECTED_CLIENT_BUILD}")
    return value


def _array_indexes(row: Mapping[str, Any], prefix: str) -> list[int]:
    indexes = []
    for key in row:
        match = re.fullmatch(re.escape(prefix) + r"\[(\d+)\]", key)
        if match:
            indexes.append(int(match.group(1)))
    return sorted(set(indexes))


def _verify_professions(rows: Iterable[Mapping[str, Any]]) -> None:
    by_id = {_int(_field(row, "ID")): row for row in rows}
    for skill_id, expected in PROFESSION_NAMES.items():
        row = by_id.get(skill_id)
        if row is None:
            raise ClientDataError(f"SkillLine is missing profession {skill_id}")
        actual = _field(row, "DisplayName_lang")
        if actual != expected:
            raise ClientDataError(f"SkillLine {skill_id} name {actual!r} != {expected!r}")


def _spell_names(rows: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    return {_int(_field(row, "ID")): str(_field(row, "Name_lang")) for row in rows}


def _teaching_items(
    item_effect_rows: Iterable[Mapping[str, Any]],
    item_relation_rows: Iterable[Mapping[str, Any]],
) -> dict[int, set[int]]:
    effect_to_items: dict[int, set[int]] = {}
    for row in item_relation_rows:
        item_id = _optional_int(_field(row, "ItemID"))
        effect_id = _optional_int(_field(row, "ItemEffectID"))
        if item_id is not None and effect_id is not None:
            effect_to_items.setdefault(effect_id, set()).add(item_id)

    result: dict[int, set[int]] = {}
    for row in item_effect_rows:
        effect_id = _optional_int(_field(row, "ID"))
        if effect_id is None or _int(_field(row, "TriggerType")) != 6:
            continue
        spell_id = _optional_int(_field(row, "SpellID"))
        if spell_id is None:
            continue
        for item_id in effect_to_items.get(effect_id, ()):
            result.setdefault(spell_id, set()).add(item_id)
    return result


def _recipe_rows(export_dir: Path) -> tuple[list[Recipe], set[int]]:
    skill_rows = _rows(export_dir, "SkillLine")
    _verify_professions(skill_rows)
    names = _spell_names(_rows(export_dir, "SpellName"))
    ability_rows = _rows(export_dir, "SkillLineAbility")
    effects = _rows(export_dir, "SpellEffect")
    spell_effects: dict[int, list[dict[str, str]]] = {}
    for row in effects:
        spell_effects.setdefault(_int(_field(row, "SpellID")), []).append(row)
    create_effects = {
        spell_id: [row for row in rows if _int(_field(row, "Effect")) == 24]
        for spell_id, rows in spell_effects.items()
    }
    create_effects = {spell_id: rows for spell_id, rows in create_effects.items() if rows}
    sparse_item_ids = {_int(_field(row, "ID")) for row in _rows(export_dir, "ItemSparse")}
    teaching = _teaching_items(
        _rows(export_dir, "ItemEffect"),
        _rows(export_dir, "ItemXItemEffect"),
    )

    reagents_by_spell: dict[int, list[Reagent]] = {}
    for row in _rows(export_dir, "SpellReagents"):
        spell_id = _int(_field(row, "SpellID"))
        pairs: list[Reagent] = []
        indexes = sorted(set(_array_indexes(row, "Reagent")) | set(_array_indexes(row, "ReagentCount")))
        for index in indexes:
            item_id = _int(_field(row, f"Reagent[{index}]"))
            count = _int(_field(row, f"ReagentCount[{index}]"))
            if count > 0 and item_id > 0:
                pairs.append(Reagent(item_id, count))
        reagents_by_spell.setdefault(spell_id, []).extend(pairs)

    cooldowns = {_int(_field(row, "SpellID")): row for row in _rows(export_dir, "SpellCooldowns")}
    spell_categories = {_int(_field(row, "SpellID")): row for row in _rows(export_dir, "SpellCategories")}
    categories = {_int(_field(row, "ID")): row for row in _rows(export_dir, "SpellCategory")}
    _rows(export_dir, "Spell")
    totem_rows = {_int(_field(row, "SpellID")): row for row in _rows(export_dir, "SpellTotems")}
    focus_rows = {_int(_field(row, "SpellID")): row for row in _rows(export_dir, "SpellCastingRequirements")}

    qualifying: dict[int, list[RecipeSkillLine]] = {}
    for row in ability_rows:
        skill_line_id = _int(_field(row, "SkillLine"))
        if skill_line_id not in PROFESSION_SKILL_LINES:
            continue
        spell_id = _int(_field(row, "Spell"))
        if spell_id not in create_effects:
            continue
        yellow = _int(_field(row, "TrivialSkillLineRankLow"))
        grey = _int(_field(row, "TrivialSkillLineRankHigh"))
        association = RecipeSkillLine(
            ability_id=_int(_field(row, "ID")),
            spell_id=spell_id,
            skill_line_id=skill_line_id,
            profession=PROFESSION_SKILL_LINES[skill_line_id],
            min_rank=_int(_field(row, "MinSkillLineRank")),
            yellow=yellow,
            green_derived=math.floor((yellow + grey) / 2),
            grey=grey,
            acquire_method=_int(_field(row, "AcquireMethod")),
            flags=_int(_field(row, "Flags")),
            supersedes_spell=_optional_int(_field(row, "SupercedesSpell")),
        )
        qualifying.setdefault(spell_id, []).append(association)

    recipes: list[Recipe] = []
    referenced_items: set[int] = set()
    for spell_id in sorted(qualifying):
        associations = tuple(
            sorted(
                qualifying[spell_id],
                key=lambda association: (association.skill_line_id, association.ability_id),
            )
        )
        output_rows: list[Output] = []
        for effect in create_effects[spell_id]:
            item_id = _int(_field(effect, "EffectItemType"))
            if item_id <= 0:
                continue
            base = _decimal(_field(effect, "EffectBasePointsF"))
            if base == 0:
                base = Decimal(1)
            variance = _decimal(_field(effect, "Variance"))
            kind = "random" if base != base.to_integral_value() or variance != 0 else "fixed"
            output_rows.append(Output(item_id, base, variance, kind))
            referenced_items.add(item_id)
        reagent_rows = tuple(reagents_by_spell.get(spell_id, ()))
        referenced_items.update(reagent.item_id for reagent in reagent_rows)
        teachers = tuple(sorted(teaching.get(spell_id, ())))
        referenced_items.update(teachers)
        cooldown = cooldowns.get(spell_id, {})
        category = spell_categories.get(spell_id, {})
        category_id = _optional_int(_field(category, "Category", default=""))
        category_name = None
        if category_id is not None:
            category_name = str(_field(categories.get(category_id, {}), "Name_lang", default=""))
        totem = totem_rows.get(spell_id, {})
        totems = tuple(
            _int(totem[f"Totem[{index}]"])
            for index in _array_indexes(totem, "Totem")
            if _int(totem[f"Totem[{index}]"]) > 0
        )
        totem_categories = tuple(
            _int(totem[f"RequiredTotemCategoryID[{index}]"])
            for index in _array_indexes(totem, "RequiredTotemCategoryID")
            if _int(totem[f"RequiredTotemCategoryID[{index}]"]) > 0
        )
        focus_row = focus_rows.get(spell_id, {})
        spell_focus = _optional_int(_field(focus_row, "RequiresSpellFocus", default=""))
        evidence = (
            "no_teaching_item"
            if not teachers
            else (
                "teaching_item_with_item_data"
                if any(item_id in sparse_item_ids for item_id in teachers)
                else "teaching_item_without_item_data"
            )
        )
        recipes.append(
            Recipe(
                spell_id=spell_id,
                name=names.get(spell_id),
                kind="item" if output_rows else "non_item",
                learn_kind="recipe_item" if teachers else "skill_line",
                learn_evidence=evidence,
                associations=associations,
                reagents=reagent_rows,
                outputs=tuple(output_rows),
                teaching_items=teachers,
                cooldown_ms=_optional_int(_field(cooldown, "RecoveryTime", default="")),
                category_cooldown_ms=_optional_int(_field(cooldown, "CategoryRecoveryTime", default="")),
                category_id=category_id,
                category_name=category_name,
                totems=totems,
                totem_categories=totem_categories,
                spell_focus=spell_focus,
            )
        )
    return recipes, referenced_items


def _catalog_item_ids(catalog_path: Path | None) -> set[int]:
    if catalog_path is None:
        catalog = load_catalog()
    else:
        if not catalog_path.exists():
            raise ClientDataError(f"catalog does not exist: {catalog_path}")
        try:
            catalog = Catalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ClientDataError(f"catalog is invalid: {catalog_path}") from exc
    return {item.item_id for item in catalog.items}


def _item_facts(export_dir: Path, item_ids: set[int]) -> tuple[ItemFact, ...]:
    item_rows = {_int(_field(row, "ID")): row for row in _rows(export_dir, "Item")}
    sparse_rows = {_int(_field(row, "ID")): row for row in _rows(export_dir, "ItemSparse")}
    result: list[ItemFact] = []
    for item_id in sorted(item_ids):
        base = item_rows.get(item_id, {})
        sparse = sparse_rows.get(item_id)
        if sparse is None:
            result.append(
                ItemFact(
                    item_id=item_id,
                    status="no_sparse_row",
                    name=None,
                    quality=None,
                    item_level=None,
                    class_id=_optional_int(_field(base, "ClassID", default="")),
                    subclass_id=_optional_int(_field(base, "SubclassID", default="")),
                    sell_price=None,
                    buy_price=None,
                    vendor_stack_count=None,
                    bonding=None,
                    max_stack=None,
                    required_skill=None,
                    required_skill_rank=None,
                )
            )
            continue
        result.append(
            ItemFact(
                item_id=item_id,
                status="ok",
                name=str(_field(sparse, "Display_lang")),
                quality=_optional_int(_field(sparse, "OverallQualityID")),
                item_level=_optional_int(_field(sparse, "ItemLevel")),
                class_id=_optional_int(_field(base, "ClassID", default="")),
                subclass_id=_optional_int(_field(base, "SubclassID", default="")),
                sell_price=_optional_int(_field(sparse, "SellPrice")),
                buy_price=_optional_int(_field(sparse, "BuyPrice")),
                vendor_stack_count=_optional_int(_field(sparse, "VendorStackCount")),
                bonding=_optional_int(_field(sparse, "Bonding")),
                max_stack=_optional_int(_field(sparse, "Stackable")),
                required_skill=_optional_int(_field(sparse, "RequiredSkill")),
                required_skill_rank=_optional_int(_field(sparse, "RequiredSkillRank")),
            )
        )
    return tuple(result)


def normalize_export(
    export_dir: str | Path,
    *,
    catalog_path: str | Path | None = None,
    manifest: Mapping[str, Any] | str | Path | None = None,
) -> NormalizedExport:
    """Normalize one export directory without writing any files."""

    directory = Path(export_dir)
    if directory.name == "csv" and directory.is_dir():
        directory = directory.parent
    manifest_value = _manifest(directory, manifest)
    recipes, referenced_items = _recipe_rows(directory)
    catalog_ids = _catalog_item_ids(Path(catalog_path) if catalog_path is not None else None)
    items = _item_facts(directory, referenced_items | catalog_ids)
    return NormalizedExport(manifest_value, tuple(sorted(recipes, key=lambda recipe: recipe.spell_id)), items)


class _RecipeIDParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[int] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "li":
            return
        for key, value in attrs:
            if key == "id" and value and value.startswith("r-"):
                try:
                    self.ids.add(int(value[2:]))
                except ValueError:
                    pass


def _page_ids(page: str | Path) -> set[int]:
    parser = _RecipeIDParser()
    parser.feed(Path(page).read_text(encoding="utf-8", errors="replace"))
    return parser.ids


def compare_profession_pages(
    normalized: NormalizedExport, pages: Mapping[str, str | Path]
) -> tuple[ProfessionPageComparison, ...]:
    """Compare normalized spell IDs to locally saved profession HTML pages."""

    result = []
    for profession, page in pages.items():
        profession_associations = normalized.recipes_by_profession.get(profession, ())
        recipes_by_spell = {recipe.spell_id: recipe for recipe in normalized.recipes}
        client = {association.spell_id for association in profession_associations}
        # Count unique spells, matching client_count: a spell may have several associations
        # on the same profession's skill line.
        evidence_counts = {
            evidence: sum(recipes_by_spell[spell_id].learn_evidence == evidence for spell_id in client)
            for evidence in (
                "teaching_item_with_item_data",
                "teaching_item_without_item_data",
                "no_teaching_item",
            )
        }
        expected = _page_ids(page)
        result.append(
            ProfessionPageComparison(
                profession=profession,
                client_count=len(client),
                expected_count=len(expected),
                client_only=tuple(sorted(client - expected)),
                expected_only=tuple(sorted(expected - client)),
                evidence_counts=evidence_counts,
            )
        )
    return tuple(result)


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Return the canonical digest used by the loader."""

    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ASSUMPTIONS",
    "EXPECTED_CLIENT_BUILD",
    "TABLE_COLUMNS",
    "ItemFact",
    "NormalizedExport",
    "Output",
    "PROFESSION_NAMES",
    "PROFESSION_SKILL_LINES",
    "ProfessionPageComparison",
    "Recipe",
    "RecipeSkillLine",
    "Reagent",
    "ClientDataError",
    "compare_profession_pages",
    "manifest_sha256",
    "normalize_export",
]
