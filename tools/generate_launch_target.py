#!/usr/bin/env python3
"""Validate and render target-derived Lua, TOC, and catalog artifacts."""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from crowley.core import companion_state, queue, scanformat
from crowley.core.game_target import (
    GameTarget,
    GameTargetError,
    load_game_target,
    selected_target,
    target_catalog,
)
from crowley.core.watchlist import Catalog
from crowley.server.routes.addon import AddonDataResponse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "src" / "crowley" / "data" / "first_run.json"


class GenerationError(ValueError):
    """Raised when target-derived artifacts cannot be rendered safely."""


def _literal_string_set(node: ast.AST, context: str) -> set[str]:
    if not isinstance(node, ast.Set):
        raise GenerationError(f"{context} must be a literal string set")
    values: set[str] = set()
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise GenerationError(f"{context} must contain only string literals")
        values.add(element.value)
    return values


def _validator_named_literal_sets(module_path: Path, function_name: str) -> dict[str, set[str]]:
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, SyntaxError) as exc:
        raise GenerationError(f"cannot read wire contract source {module_path}: {exc}") from exc
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        ),
        None,
    )
    if function is None:
        raise GenerationError(f"wire contract source is missing {function_name}")
    result: dict[str, set[str]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "_keys":
            continue
        if len(node.args) < 2:
            raise GenerationError(f"{function_name} has an incomplete _keys call")
        source = node.args[0]
        if not isinstance(source, ast.Name):
            raise GenerationError(f"{function_name}._keys source must be a named value")
        if source.id in result:
            raise GenerationError(f"{function_name} has duplicate _keys validation for {source.id}")
        result[source.id] = _literal_string_set(node.args[1], f"{function_name}._keys[{source.id}]")
    if not result:
        raise GenerationError(f"{function_name} has no literal _keys calls")
    return result


def _action_literal_sets(module_path: Path) -> dict[str, set[str]]:
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, SyntaxError) as exc:
        raise GenerationError(f"cannot read wire contract source {module_path}: {exc}") from exc
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_validate_action"
        ),
        None,
    )
    if function is None:
        raise GenerationError("queue wire contract source is missing _validate_action")
    assignment = next(
        (
            node
            for node in ast.walk(function)
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "fields" for target in node.targets)
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "fields"
            )
        ),
        None,
    )
    if assignment is None or not isinstance(assignment.value, ast.Dict):
        raise GenerationError("queue _validate_action fields must be a literal mapping")
    result: dict[str, set[str]] = {}
    for key, value in zip(assignment.value.keys, assignment.value.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise GenerationError("queue action field names must be string literals")
        result[key.value] = _literal_string_set(value, f"queue action {key.value}")
    return result


def _wire_schema_sets() -> dict[str, object]:
    """Return wire keys from the Python validators and addon-data writer contract."""

    scan_sets = _validator_named_literal_sets(Path(scanformat.__file__), "validate_envelope")
    if not {"raw", "decoded"}.issubset(scan_sets):
        raise GenerationError("scan validate_envelope must validate raw and decoded envelope keys")
    action_kinds = sorted(queue.ACTION_KINDS)
    action_fields = _action_literal_sets(Path(queue.__file__))
    if set(action_fields) != set(action_kinds):
        raise GenerationError(
            "queue action validator fields do not match ACTION_KINDS; update the contract parser"
        )
    data_fields = set(AddonDataResponse.model_fields)
    if not data_fields:
        raise GenerationError("addon-data response has no fields")
    companion_fields = set(companion_state.CompanionState.__dataclass_fields__)
    return {
        # coldSlice is the one optional envelope key (present only when the plan has cold slices).
        "savedScanEnvelope": scan_sets["raw"] | {"coldSlice"},
        "scanPayload": scan_sets["decoded"],
        "crowleyData": data_fields,
        "crowleyQueue": set(queue.QUEUE_FIELDS),
        "execution": set(queue.EXECUTION_FIELDS),
        "result": set(queue.RESULT_FIELDS),
        "companionState": companion_fields,
        "actionFields": action_fields,
    }


def _render_lua_key_set(value: set[str]) -> str:
    return "{" + ", ".join(f"[{_lua_string(key)}] = true" for key in sorted(value)) + "}"


def render_generated_schemas() -> str:
    schemas = _wire_schema_sets()
    action_fields = schemas.pop("actionFields")
    lines = [
        "local _, ns = ...",
        "",
        "-- Generated by tools/generate_launch_target.py. Do not edit by hand.",
        "ns.GeneratedWireSchemas = {",
    ]
    for name in sorted(schemas):
        value = schemas[name]
        if not isinstance(value, set):
            raise GenerationError(f"wire schema {name} is not a string set")
        lines.append(f"  {name} = {_render_lua_key_set(value)},")
    lines.append("  actionFields = {")
    if not isinstance(action_fields, Mapping):
        raise GenerationError("wire action fields are not a mapping")
    for action in sorted(action_fields):
        fields = action_fields[action]
        if not isinstance(fields, set):
            raise GenerationError(f"wire action schema {action} is not a string set")
        lines.append(f"    {action} = {_render_lua_key_set(fields)},")
    lines.extend(
        [
            "  },",
            "}",
            "",
            'ns.RegisterModule("GeneratedWireSchemas", {',
            '  requires = { "Core" },',
            "  onLoad = function() end,",
            "})",
            "",
        ]
    )
    return "\n".join(lines)


def _default_target_path() -> Path:
    selected = selected_target()
    path = ROOT / "src" / "crowley" / "data" / "targets" / f"{selected.target_id}.json"
    try:
        packaged = load_game_target(path)
    except (OSError, GameTargetError) as exc:
        raise GenerationError(f"selected target source is unavailable: {path}: {exc}") from exc
    if packaged != selected:
        raise GenerationError("targets/selected does not identify the default target source")
    return path


def _compact_entries(plan: Mapping[str, object]) -> str:
    pools_value = plan.get("pools")
    item_ids_value = plan.get("itemIDs")
    if not isinstance(pools_value, dict) or not isinstance(item_ids_value, list):
        raise GenerationError("scan plan is missing pools or itemIDs")
    pools = cast(dict[str, object], pools_value)
    core = pools.get("core")
    discovery = pools.get("discovery")
    if not isinstance(core, list) or not isinstance(discovery, list):
        raise GenerationError("scan plan pools are malformed")
    cold = pools.get("cold")
    if not isinstance(cold, list):
        raise GenerationError("scan plan pools are malformed")
    pool_by_id = {int(item_id): 0 for item_id in core}
    pool_by_id.update({int(item_id): 1 for item_id in discovery})
    pool_by_id.update({int(item_id): 2 for item_id in cold})
    previous = 0
    entries: list[str] = []
    for raw_item_id in item_ids_value:
        if isinstance(raw_item_id, bool) or not isinstance(raw_item_id, int):
            raise GenerationError("scan plan item IDs must be integers")
        delta = raw_item_id - previous
        if delta <= 0:
            raise GenerationError("scan plan item IDs must be strictly increasing")
        if raw_item_id not in pool_by_id:
            raise GenerationError(f"scan plan item {raw_item_id} is missing from its pool")
        entries.append(f"{delta}:{pool_by_id[raw_item_id]}")
        previous = raw_item_id
    return ",".join(entries)


def _lua_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_target_json(target: GameTarget) -> str:
    """Render the identity-only packaged target source artifact."""

    value = {
        "schemaVersion": 1,
        "targetId": target.target_id,
        "client": {
            "fullBuild": target.full_build,
            "interface": target.interface,
            "executable": target.executable,
            "windowClass": target.window_class,
            "product": target.product,
            "productDir": target.product_dir,
            "locale": target.locale,
        },
        "context": {
            "region": target.region,
            "ruleset": target.ruleset,
            "realm": target.realm,
            "faction": target.faction,
            "house": target.house,
            "nativeRegionId": target.native_region_id,
        },
        "scan": {
            "catalogId": target.catalog_id,
            "catalogRevision": target.catalog_revision,
            "cadenceSeconds": target.cadence_seconds,
            "sweepCapSeconds": target.sweep_cap_seconds,
            "coldSlices": target.cold_slices,
            "initialPriceLevels": target.initial_price_levels,
        },
        "routeProvenance": target.route_provenance,
    }
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def render_generated_lua(target: GameTarget, plan: Mapping[str, object]) -> str:
    entries = _compact_entries(plan)
    item_ids = plan.get("itemIDs")
    if not isinstance(item_ids, list):
        raise GenerationError("scan plan itemIDs are malformed")
    values: list[tuple[str, object]] = [
        ("targetId", target.target_id),
        ("fullBuild", target.full_build),
        ("clientVersion", target.client_version),
        ("nativeBuild", target.native_build),
        ("interface", target.interface),
        ("executable", target.executable),
        ("windowClass", target.window_class),
        ("product", target.product),
        ("productDir", target.product_dir),
        ("locale", target.locale),
        ("region", target.region),
        ("nativeRegionId", target.native_region_id),
        ("ruleset", target.ruleset),
        ("realm", target.realm),
        ("faction", target.faction),
        ("house", target.house),
        ("routeProvenance", target.route_provenance),
        ("schema", cast(int, plan["schemaVersion"])),
        ("id", cast(str, plan["id"])),
        ("revision", cast(int, plan["revision"])),
        ("catalogHash", cast(str, plan["catalogHash"])),
        ("planHash", cast(str, plan["planHash"])),
        ("count", len(item_ids)),
        ("cadenceSeconds", cast(int, plan["cadenceSeconds"])),
        ("sweepCapSeconds", target.sweep_cap_seconds),
        ("coldSlices", target.cold_slices),
        ("initialPriceLevels", cast(int, plan["initialPriceLevels"])),
        ("planEntries", entries),
    ]
    lines = [
        "local _, ns = ...",
        "",
        "-- Generated by tools/generate_launch_target.py. Do not edit by hand.",
        "ns.GeneratedTarget = {",
    ]
    for key, value in values:
        encoded = _lua_string(value) if isinstance(value, str) else str(value)
        lines.append(f"  {key} = {encoded},")
    lines.extend(["}", ""])
    return "\n".join(lines)


def _replace_toc_interface(text: str, interface: int) -> str:
    pattern = re.compile(r"(?m)^## Interface: \d+$")
    result, count = pattern.subn(f"## Interface: {interface}", text)
    if count != 1:
        raise GenerationError("addon TOC must contain exactly one Interface header")
    return result


def _validate_toc_load_order(text: str, name: str) -> None:
    entries = [
        line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("##")
    ]
    generated = [index for index, line in enumerate(entries) if line == "GeneratedTarget.lua"]
    if len(generated) != 1:
        raise GenerationError(f"{name} must contain exactly one GeneratedTarget.lua entry")
    util = [index for index, line in enumerate(entries) if line == "Util.lua"]
    if len(util) != 1:
        raise GenerationError(f"{name} must contain exactly one Util.lua entry")
    if util[0] != generated[0] + 1:
        raise GenerationError(f"{name} must load Util.lua immediately after GeneratedTarget.lua")
    for dependency in ("Context.lua", "Scanner.lua"):
        dependency_indexes = [index for index, line in enumerate(entries) if line == dependency]
        if len(dependency_indexes) != 1 or generated[0] > dependency_indexes[0]:
            raise GenerationError(f"{name} must load GeneratedTarget.lua before {dependency}")


def _read_catalog(path: Path) -> Catalog:
    try:
        return Catalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenerationError(f"catalog is invalid: {path}: {exc}") from exc


def _source_artifacts(root: Path, target_id: str) -> dict[Path, str]:
    return {
        root / "src" / "crowley" / "data" / "targets" / f"{target_id}.json": "target",
        root / "src" / "crowley" / "data" / "first_run.json": "catalog",
        root / "addon" / "Crowley" / "GeneratedTarget.lua": "generated_lua",
        root / "addon" / "Crowley" / "GeneratedSchemas.lua": "generated_schemas",
        root / "addon" / "Crowley" / "Crowley.toc": "crowley_toc",
        root / "addon" / "Crowley_Data" / "Crowley_Data.toc": "data_toc",
    }


def _render(root: Path, target_path: Path, catalog_path: Path) -> dict[Path, str]:
    try:
        target = load_game_target(target_path)
    except (OSError, GameTargetError) as exc:
        raise GenerationError(f"target is invalid: {target_path}: {exc}") from exc
    catalog = _read_catalog(catalog_path)
    try:
        bound = target_catalog(catalog, target)
    except (AttributeError, GameTargetError) as exc:
        raise GenerationError(f"catalog identity cannot be bound to target: {exc}") from exc
    plan = bound.scan_plan(cadence_seconds=target.cadence_seconds, cold_slices=target.cold_slices)
    item_ids = plan.get("itemIDs")
    if not isinstance(item_ids, list) or not item_ids:
        raise GenerationError("target-bound scan plan must contain active items")
    if plan["id"] != target.catalog_id or plan["revision"] != target.catalog_revision:
        raise GenerationError("generated plan identity does not match target")
    catalog_json = json.dumps(bound.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    artifacts = _source_artifacts(root, target.target_id)
    templates = _source_artifacts(ROOT, target.target_id)
    toc_path = next(path for path, kind in templates.items() if kind == "crowley_toc")
    data_toc_path = next(path for path, kind in templates.items() if kind == "data_toc")
    toc = toc_path.read_text(encoding="utf-8")
    data_toc = data_toc_path.read_text(encoding="utf-8")
    _validate_toc_load_order(toc, "checkout addon/Crowley/Crowley.toc")
    return {
        path: {
            "target": render_target_json(target),
            "catalog": catalog_json,
            "generated_lua": render_generated_lua(target, plan),
            "generated_schemas": render_generated_schemas(),
            "crowley_toc": _replace_toc_interface(toc, target.interface),
            "data_toc": _replace_toc_interface(data_toc, target.interface),
        }[kind]
        for path, kind in artifacts.items()
    }


def _write_or_check(artifacts: Mapping[Path, str], *, write: bool) -> list[str]:
    errors: list[str] = []
    for path, expected in artifacts.items():
        actual = path.read_text(encoding="utf-8") if path.is_file() else None
        if actual != expected:
            if write:
                if path.is_symlink():
                    raise GenerationError(f"refusing to write generated symlink: {path}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8")
            else:
                errors.append(str(path))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check generated bytes without writing (default)")
    mode.add_argument("--write", action="store_true", help="write generated source artifacts")
    args = parser.parse_args(argv)
    write = bool(args.write)
    try:
        output_root = args.output_root.absolute()
        target_path = _default_target_path() if args.target is None else args.target.absolute()
        artifacts = _render(output_root, target_path, args.catalog.absolute())
        errors = _write_or_check(artifacts, write=False) if not write else []
        if write:
            errors = _write_or_check(artifacts, write=True)
        if errors:
            raise GenerationError("generated artifacts drifted: " + ", ".join(errors))
    except (GenerationError, GameTargetError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print("launch target generated" if write else "launch target check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
