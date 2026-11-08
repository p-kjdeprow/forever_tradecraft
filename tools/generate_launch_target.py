#!/usr/bin/env python3
"""Validate and render the packaged game target and its derived artifacts.

The default operation is ``--check``.  ``--write`` only writes generated source
artifacts under the repository (and an explicitly requested staging directory);
it never edits live deployment files, game files, routes, or evidence.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from crowley.core import companion_state, queue, scanformat
from crowley.core.game_target import GameTarget, GameTargetError, load_game_target, target_catalog
from crowley.core.watchlist import Catalog
from crowley.server.routes.addon import AddonDataResponse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "src" / "crowley" / "data" / "game_target.json"
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
        "savedScanEnvelope": scan_sets["raw"],
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


def _compact_entries(plan: dict[str, Any]) -> str:
    pools = plan["pools"]
    pool_by_id = {int(item_id): 0 for item_id in pools["core"]}
    pool_by_id.update({int(item_id): 1 for item_id in pools["discovery"]})
    previous = 0
    entries: list[str] = []
    for item_id in plan["itemIDs"]:
        item_id = int(item_id)
        delta = item_id - previous
        if delta <= 0:
            raise GenerationError("scan plan item IDs must be strictly increasing")
        if item_id not in pool_by_id:
            raise GenerationError(f"scan plan item {item_id} is missing from its pool")
        entries.append(f"{delta}:{pool_by_id[item_id]}")
        previous = item_id
    return ",".join(entries)


def _lua_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_target_json(target: GameTarget) -> str:
    """Render the selected target as the packaged source artifact."""

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
        "installation": {
            "gameRoot": target.game_root,
            "winePrefix": target.wine_prefix,
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
            "initialPriceLevels": target.initial_price_levels,
        },
        "routeProvenance": target.route_provenance,
    }
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def render_generated_lua(target: GameTarget, plan: dict[str, Any]) -> str:
    entries = _compact_entries(plan)
    values = [
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
        ("schema", int(plan["schemaVersion"])),
        ("id", plan["id"]),
        ("revision", int(plan["revision"])),
        ("catalogHash", plan["catalogHash"]),
        ("planHash", plan["planHash"]),
        ("count", len(plan["itemIDs"])),
        ("cadenceSeconds", int(plan["cadenceSeconds"])),
        ("sweepCapSeconds", target.sweep_cap_seconds),
        ("initialPriceLevels", int(plan["initialPriceLevels"])),
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
    replacement = f"## Interface: {interface}"
    result, count = pattern.subn(replacement, text)
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


def _toml_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _replace_section_key(text: str, section: str, key: str, value: Any) -> str:
    section_match = re.search(rf"(?ms)^\[{re.escape(section)}\]\n(?P<body>.*?)(?=^\[|\Z)", text)
    if section_match is None:
        raise GenerationError(f"staged TOML is missing [{section}]")
    body = section_match.group("body")
    line = f"{key} = {_toml_value(value)}"
    key_pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    body, count = key_pattern.subn(line, body)
    if count == 0:
        body = line + "\n" + body
    return text[: section_match.start("body")] + body + text[section_match.end("body") :]


def _replace_root_key(text: str, key: str, value: Any, *, required: bool = True) -> str:
    line = f"{key} = {_toml_value(value)}"
    key_pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    text, count = key_pattern.subn(line, text)
    if required and count != 1:
        raise GenerationError(f"staged TOML must contain exactly one root {key} key")
    if not required and count > 1:
        raise GenerationError(f"staged TOML must contain at most one root {key} key")
    return text


def _absolute_config_path(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise GenerationError(f"{where} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise GenerationError(f"{where} must be an absolute non-root path without traversal")
    return path


def _rewrite_install_path(value: Any, *, old_root: Path, new_root: Path, where: str) -> str:
    path = _absolute_config_path(value, where)
    try:
        suffix = path.relative_to(old_root)
    except ValueError as exc:
        raise GenerationError(f"{where} is not under the configured companion wow_dir") from exc
    if not suffix.parts:
        raise GenerationError(f"{where} must name a file below the configured companion wow_dir")
    return str(new_root / suffix)


def render_session_config(
    text: str,
    target: GameTarget,
    plan: dict[str, Any],
    *,
    old_wow_dir: Path | None = None,
) -> str:
    values = {
        "client_build": target.native_build,
        "interface": target.interface,
        "client_version": target.client_version,
        "wine_prefix": target.wine_prefix,
        "player_faction": target.faction,
        "locale": target.locale,
        "window_class": target.window_class,
        "executable": target.executable,
        "realm": target.realm,
        "house": target.house,
        "plan_count": len(plan["itemIDs"]),
        "plan_hash": plan["planHash"],
        "plan_json": plan["planJSON"],
    }
    for key, value in values.items():
        text = _replace_section_key(text, "profile", key, value)
    for key, value in {
        "cadence_seconds": target.cadence_seconds,
        "sweep_cap_seconds": target.sweep_cap_seconds,
    }.items():
        text = _replace_section_key(text, "session", key, value)
    if old_wow_dir is not None:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise GenerationError(f"session.toml is invalid: {exc}") from exc
        session = parsed.get("session")
        if not isinstance(session, dict):
            raise GenerationError("staged TOML is missing [session]")
        new_wow_dir = Path(target.game_root) / target.product_dir
        for key in ("saved_variables", "data_lua"):
            if key not in session:
                raise GenerationError(f"session.{key} is required for staged launch config")
            value = _rewrite_install_path(
                session[key],
                old_root=old_wow_dir,
                new_root=new_wow_dir,
                where=f"session.{key}",
            )
            text = _replace_section_key(text, "session", key, value)
    return text


def render_companion_config(text: str, target: GameTarget) -> str:
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GenerationError(f"companion.toml is invalid: {exc}") from exc
    _absolute_config_path(values.get("wow_dir"), "companion.wow_dir")
    new_wow_dir = Path(target.game_root) / target.product_dir
    text = _replace_root_key(text, "wow_dir", str(new_wow_dir))
    house_key = values.get("house_key")
    if house_key is not None and not isinstance(house_key, str):
        raise GenerationError("companion.house_key must be a string when configured")
    if isinstance(house_key, str) and house_key:
        text = _replace_root_key(text, "house_key", target.house)
    return text


def _read_catalog(path: Path) -> Catalog:
    try:
        return Catalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenerationError(f"catalog is invalid: {path}: {exc}") from exc


def _source_artifacts(root: Path) -> dict[str, Path]:
    return {
        "target": root / "src" / "crowley" / "data" / "game_target.json",
        "catalog": root / "src" / "crowley" / "data" / "first_run.json",
        "generated_lua": root / "addon" / "Crowley" / "GeneratedTarget.lua",
        "generated_schemas": root / "addon" / "Crowley" / "GeneratedSchemas.lua",
        "crowley_toc": root / "addon" / "Crowley" / "Crowley.toc",
        "data_toc": root / "addon" / "Crowley_Data" / "Crowley_Data.toc",
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
    plan = bound.scan_plan(cadence_seconds=target.cadence_seconds)
    if len(plan["itemIDs"]) <= 0:
        raise GenerationError("target-bound scan plan must contain active items")
    if plan["id"] != target.catalog_id or plan["revision"] != target.catalog_revision:
        raise GenerationError("generated plan identity does not match target")
    catalog_json = json.dumps(bound.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    artifacts = _source_artifacts(root)
    templates = _source_artifacts(ROOT)
    if not templates["crowley_toc"].is_file() or not templates["data_toc"].is_file():
        raise GenerationError("generator requires both checkout addon TOC templates")
    toc = templates["crowley_toc"].read_text(encoding="utf-8")
    data_toc = templates["data_toc"].read_text(encoding="utf-8")
    _validate_toc_load_order(toc, "checkout addon/Crowley/Crowley.toc")
    return {
        artifacts["target"]: render_target_json(target),
        artifacts["catalog"]: catalog_json,
        artifacts["generated_lua"]: render_generated_lua(target, plan),
        artifacts["generated_schemas"]: render_generated_schemas(),
        artifacts["crowley_toc"]: _replace_toc_interface(toc, target.interface),
        artifacts["data_toc"]: _replace_toc_interface(data_toc, target.interface),
    }


def _render_staged_configs(
    root: Path, stage_dir: Path, target: GameTarget, plan: dict[str, Any]
) -> dict[Path, str]:
    config_root = root / "deploy" / "config"
    session_source = config_root / "session.toml"
    companion_source = config_root / "companion.toml"
    if not session_source.is_file() or not companion_source.is_file():
        raise GenerationError(
            "staged config rendering requires deploy/config/session.toml and companion.toml"
        )
    companion = companion_source.read_text(encoding="utf-8")
    try:
        companion_values = tomllib.loads(companion)
    except tomllib.TOMLDecodeError as exc:
        raise GenerationError(f"companion.toml is invalid: {exc}") from exc
    old_wow_dir = _absolute_config_path(companion_values.get("wow_dir"), "companion.wow_dir")
    session = render_session_config(
        session_source.read_text(encoding="utf-8"),
        target,
        plan,
        old_wow_dir=old_wow_dir,
    )
    companion = render_companion_config(companion, target)
    return {
        stage_dir / "session.toml": session,
        stage_dir / "companion.toml": companion,
    }


def _protected_roots(root: Path, target: GameTarget) -> tuple[Path, ...]:
    protected = {
        ROOT / "deploy" / "config",
        Path.home() / ".config" / "crowley",
        root / "deploy" / "config",
        Path(target.game_root),
        Path(target.wine_prefix),
    }
    actual_session = ROOT / "deploy" / "config" / "session.toml"
    actual_companion = ROOT / "deploy" / "config" / "companion.toml"
    try:
        if actual_session.is_file():
            session = tomllib.loads(actual_session.read_text(encoding="utf-8"))
            profile = session.get("profile")
            if isinstance(profile, dict) and profile.get("wine_prefix") is not None:
                protected.add(_absolute_config_path(profile["wine_prefix"], "profile.wine_prefix"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GenerationError(f"cannot inspect current session installation path: {exc}") from exc
    try:
        if actual_companion.is_file():
            companion = tomllib.loads(actual_companion.read_text(encoding="utf-8"))
            wow_dir = _absolute_config_path(companion.get("wow_dir"), "companion.wow_dir")
            protected.update((wow_dir, wow_dir.parent))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GenerationError(f"cannot inspect current companion installation path: {exc}") from exc
    return tuple(path.resolve(strict=False) for path in protected)


def _preflight_destinations(destinations: list[Path], *, root: Path, target: GameTarget) -> None:
    protected = _protected_roots(root, target)
    for destination in destinations:
        probe = destination
        while True:
            if probe.is_symlink():
                raise GenerationError(f"refusing to write symlink destination: {destination}")
            if probe.parent == probe:
                break
            probe = probe.parent
        resolved = destination.resolve(strict=False)
        if any(resolved == guard or guard in resolved.parents for guard in protected):
            raise GenerationError(
                f"refusing to write live deploy/config or protected installation/config path: {destination}"
            )


def _write_or_check(artifacts: dict[Path, str], *, write: bool) -> list[str]:
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


def _write_staged(artifacts: dict[Path, str]) -> None:
    for path, text in artifacts.items():
        if path.is_symlink():
            raise GenerationError(f"refusing to write staged config symlink: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check generated bytes without writing (default)")
    mode.add_argument("--write", action="store_true", help="write generated source artifacts")
    parser.add_argument("--stage-config-dir", type=Path)
    args = parser.parse_args(argv)
    write = bool(args.write)
    try:
        output_root = args.output_root.absolute()
        artifacts = _render(output_root, args.target.absolute(), args.catalog.absolute())
        errors = _write_or_check(artifacts, write=False) if not write else []
        target = load_game_target(args.target)
        catalog = _read_catalog(args.catalog)
        plan = target_catalog(catalog, target).scan_plan(cadence_seconds=target.cadence_seconds)
        staged: dict[Path, str] = {}
        if args.stage_config_dir is not None:
            if not write:
                raise GenerationError("--stage-config-dir requires --write")
            staged = _render_staged_configs(output_root, args.stage_config_dir.absolute(), target, plan)
        if write:
            _preflight_destinations(
                [*artifacts, *staged],
                root=output_root,
                target=target,
            )
            errors = _write_or_check(artifacts, write=True)
            if staged:
                _write_staged(staged)
        if errors:
            raise GenerationError("generated artifacts drifted: " + ", ".join(errors))
    except (GenerationError, GameTargetError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print("launch target generated" if write else "launch target check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
