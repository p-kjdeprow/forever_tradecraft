"""Strict packaged client targets.

Targets describe a client and its operational identity.  Installation and
operator-specific values are deliberately loaded from :mod:`crowley.config`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Protocol, cast

from .house import House

_BUILD_RE = re.compile(r"^(?P<version>\d+\.\d+\.\d+)\.(?P<build>\d+)$")
_HOUSE_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$")
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FIXED_COLLECTION_CADENCE_SECONDS = 900


class GameTargetError(ValueError):
    """Raised when a target is not an exact operational contract."""


class CatalogLike(Protocol):
    id: str
    revision: int
    client_build: str
    initial_price_levels: int

    def model_copy(self, *, update: dict[str, object]) -> CatalogLike: ...

    def scan_plan(self, *, cadence_seconds: int) -> dict[str, object]: ...


def _object(value: object, expected: set[str], where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GameTargetError(f"{where} must be an object")
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise GameTargetError(f"{where} has unknown fields: {sorted(unknown)}")
    if missing:
        raise GameTargetError(f"{where} is missing fields: {sorted(missing)}")
    return cast(dict[str, object], value)


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise GameTargetError(f"{where} must be a non-empty string")
    return value


def _integer(value: object, where: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GameTargetError(f"{where} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class GameTarget:
    target_id: str
    full_build: str
    client_version: str
    native_build: str
    interface: int
    executable: str
    window_class: str
    product: str
    product_dir: str
    locale: str
    region: str
    native_region_id: int
    ruleset: str
    realm: str
    faction: str
    house: str
    catalog_id: str
    catalog_revision: int
    cadence_seconds: int
    sweep_cap_seconds: int
    initial_price_levels: int
    route_provenance: str

    @property
    def windows_argv0(self) -> str:
        """argv0 Wine reports for the game process started by Battle.net."""

        return rf"C:\Program Files (x86)\World of Warcraft\{self.product_dir}\{self.executable}"

    @classmethod
    def from_mapping(cls, raw: object) -> GameTarget:
        top = _object(
            raw,
            {"schemaVersion", "targetId", "client", "context", "scan", "routeProvenance"},
            "target",
        )
        schema = top["schemaVersion"]
        if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
            raise GameTargetError("target.schemaVersion must be 1")
        target_id = _string(top["targetId"], "target.targetId")
        if _TARGET_ID_RE.fullmatch(target_id) is None:
            raise GameTargetError("target.targetId must contain lowercase letters, digits, or hyphens")

        client = _object(
            top["client"],
            {"fullBuild", "interface", "executable", "windowClass", "product", "productDir", "locale"},
            "target.client",
        )
        full_build = _string(client["fullBuild"], "target.client.fullBuild")
        match = _BUILD_RE.fullmatch(full_build)
        if match is None:
            raise GameTargetError("target.client.fullBuild must be version.build")
        interface = _integer(client["interface"], "target.client.interface")
        executable = _string(client["executable"], "target.client.executable")
        window_class = _string(client["windowClass"], "target.client.windowClass")
        product = _string(client["product"], "target.client.product")
        product_dir = _string(client["productDir"], "target.client.productDir")
        if (
            product_dir in {".", ".."}
            or "/" in product_dir
            or "\\" in product_dir
            or len(Path(product_dir).parts) != 1
            or Path(product_dir).is_absolute()
        ):
            raise GameTargetError("target.client.productDir must be one relative path component")
        locale = _string(client["locale"], "target.client.locale")
        if locale != "enUS":
            raise GameTargetError("target.client.locale must be the supported enUS locale")

        context = _object(
            top["context"],
            {"region", "ruleset", "realm", "faction", "house", "nativeRegionId"},
            "target.context",
        )
        region = _string(context["region"], "target.context.region")
        ruleset = _string(context["ruleset"], "target.context.ruleset")
        realm = _string(context["realm"], "target.context.realm")
        faction = _string(context["faction"], "target.context.faction")
        native_region_id = _integer(context["nativeRegionId"], "target.context.nativeRegionId")
        house = _string(context["house"], "target.context.house")
        if _HOUSE_RE.fullmatch(house) is None:
            raise GameTargetError("target.context.house must be a lowercase region-ruleset-faction key")
        try:
            house_identity = House(region, ruleset, faction.lower())
        except ValueError as exc:
            raise GameTargetError(f"target.context house values are invalid: {exc}") from exc
        if house != house_identity.key:
            raise GameTargetError("target.context.house does not match region/ruleset/faction")

        scan = _object(
            top["scan"],
            {"catalogId", "catalogRevision", "cadenceSeconds", "sweepCapSeconds", "initialPriceLevels"},
            "target.scan",
        )
        catalog_id = _string(scan["catalogId"], "target.scan.catalogId")
        catalog_revision = _integer(scan["catalogRevision"], "target.scan.catalogRevision")
        cadence_seconds = _integer(scan["cadenceSeconds"], "target.scan.cadenceSeconds")
        sweep_cap_seconds = _integer(scan["sweepCapSeconds"], "target.scan.sweepCapSeconds")
        initial_price_levels = _integer(scan["initialPriceLevels"], "target.scan.initialPriceLevels")
        if cadence_seconds != FIXED_COLLECTION_CADENCE_SECONDS:
            raise GameTargetError(
                "target.scan.cadenceSeconds must use the fixed collection policy "
                f"{FIXED_COLLECTION_CADENCE_SECONDS}"
            )
        if sweep_cap_seconds > cadence_seconds:
            raise GameTargetError("target.scan.sweepCapSeconds cannot exceed cadenceSeconds")

        return cls(
            target_id=target_id,
            full_build=full_build,
            client_version=match.group("version"),
            native_build=match.group("build"),
            interface=interface,
            executable=executable,
            window_class=window_class,
            product=product,
            product_dir=product_dir,
            locale=locale,
            region=region,
            native_region_id=native_region_id,
            ruleset=ruleset,
            realm=realm,
            faction=faction,
            house=house,
            catalog_id=catalog_id,
            catalog_revision=catalog_revision,
            cadence_seconds=cadence_seconds,
            sweep_cap_seconds=sweep_cap_seconds,
            initial_price_levels=initial_price_levels,
            route_provenance=_string(top["routeProvenance"], "target.routeProvenance"),
        )

    def validate_catalog(self, catalog: CatalogLike) -> None:
        """Reject a catalog whose generated identity differs from this target."""

        if catalog.id != self.catalog_id:
            raise GameTargetError("catalog id does not match the selected target")
        if catalog.revision != self.catalog_revision:
            raise GameTargetError("catalog revision does not match the selected target")
        if catalog.client_build != self.full_build:
            raise GameTargetError("catalog client_build does not match the selected target")
        if catalog.initial_price_levels != self.initial_price_levels:
            raise GameTargetError("catalog initial_price_levels does not match the selected target")


def _parse_target(text: str, source: str) -> GameTarget:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GameTargetError(f"{source} is not valid JSON") from exc
    return GameTarget.from_mapping(value)


def _target_resource(target_id: str):
    if _TARGET_ID_RE.fullmatch(target_id) is None:
        raise GameTargetError("target_id must contain lowercase letters, digits, or hyphens")
    return resources.files("crowley.data").joinpath("targets", f"{target_id}.json")


def load_target(target_id: str) -> GameTarget:
    """Load one named packaged target contract."""

    if not isinstance(target_id, str):
        raise GameTargetError("target_id must be a string")
    source = _target_resource(target_id)
    try:
        return _parse_target(source.read_text(encoding="utf-8"), str(source))
    except FileNotFoundError as exc:
        raise GameTargetError(f"packaged target does not exist: {target_id}") from exc
    except OSError as exc:
        raise GameTargetError(f"packaged target is unavailable: {target_id}") from exc


def selected_target() -> GameTarget:
    """Load the target named by the one-line packaged selection file."""

    source = resources.files("crowley.data").joinpath("targets", "selected")
    try:
        selected = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise GameTargetError("packaged target selection is unavailable") from exc
    if not selected.endswith("\n") or selected.count("\n") != 1:
        raise GameTargetError("packaged target selection must contain exactly one targetId line")
    return load_target(selected[:-1])


def load_game_target(path: str | Path) -> GameTarget:
    """Load an explicit target file for tools and fixture tests."""

    target_path = Path(path)
    try:
        return _parse_target(target_path.read_text(encoding="utf-8"), str(target_path))
    except OSError as exc:
        raise GameTargetError(f"target is unavailable: {target_path}") from exc


@lru_cache(maxsize=1)
def packaged_game_target() -> GameTarget:
    """Compatibility name for consumers migrating to :func:`selected_target`."""

    return selected_target()


def target_catalog(catalog: CatalogLike, target: GameTarget | None = None) -> CatalogLike:
    """Return a catalog bound to a selected target without performing I/O."""

    selected = packaged_game_target() if target is None else target
    selected.validate_catalog(catalog)
    return catalog.model_copy(
        update={"client_build": selected.full_build, "initial_price_levels": selected.initial_price_levels}
    )


def target_scan_plan(catalog: CatalogLike, target: GameTarget | None = None) -> dict[str, object]:
    selected = packaged_game_target() if target is None else target
    return target_catalog(catalog, selected).scan_plan(cadence_seconds=selected.cadence_seconds)


@lru_cache(maxsize=1)
def canonical_plan(target: GameTarget | None = None) -> dict[str, object]:
    """Return the selected target's canonical scan plan, computed once."""

    from .watchlist import load_catalog

    selected = packaged_game_target() if target is None else target
    catalog = load_catalog(enforce_target=False)
    return target_scan_plan(cast(CatalogLike, catalog), selected)


__all__ = [
    "CatalogLike",
    "FIXED_COLLECTION_CADENCE_SECONDS",
    "GameTarget",
    "GameTargetError",
    "canonical_plan",
    "load_game_target",
    "load_target",
    "packaged_game_target",
    "selected_target",
    "target_catalog",
    "target_scan_plan",
]
