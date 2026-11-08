"""Focused checks for packaged targets and deterministic generation."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.core.game_target import (
    GameTargetError,
    canonical_plan,
    load_game_target,
    load_target,
    packaged_game_target,
    selected_target,
)
from crowley.core.watchlist import load_catalog
from crowley.session import daemon

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "generate_launch_target.py"
TARGET_PATH = ROOT / "src" / "crowley" / "data" / "targets" / "verified-beta.json"
CATALOG_PATH = ROOT / "src" / "crowley" / "data" / "first_run.json"
ROUTE_PATH = ROOT / "src" / "crowley" / "data" / "routes" / "verified-beta" / "stormwind.json"


def test_selected_target_is_identity_only_and_plan_is_cached() -> None:
    target = selected_target()
    assert target == load_target("verified-beta")
    assert target == packaged_game_target()
    value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    assert "installation" not in value
    assert not hasattr(target, "installation")
    assert not hasattr(target, "game_root")
    assert not hasattr(target, "wine_prefix")
    assert len(canonical_plan(target)["itemIDs"]) == 428
    assert canonical_plan(target) is canonical_plan(target)
    assert target.full_build == load_catalog().client_build


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda value: value.update({"unknown": True}), "unknown fields"),
        (lambda value: value["client"].pop("windowClass"), "missing fields"),
        (lambda value: value.update({"installation": {}}), "unknown fields"),
        (lambda value: value["client"].update({"interface": True}), "integer"),
        (lambda value: value["context"].update({"house": "beta-normal-horde"}), "does not match"),
    ],
)
def test_target_contract_rejects_unknown_or_inconsistent_values(
    tmp_path: Path, edit: object, message: str
) -> None:
    value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    assert callable(edit)
    edit(value)
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(GameTargetError, match=message):
        load_game_target(path)


def _copy_generator_trees(root: Path) -> None:
    for relative in ("addon/Crowley/Crowley.toc", "addon/Crowley_Data/Crowley_Data.toc"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


def test_generator_checks_current_target_derived_artifacts() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_generated_wire_schema_artifact_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    schemas = (ROOT / "addon" / "Crowley" / "GeneratedSchemas.lua").read_text(encoding="utf-8")
    assert '["ackedScans"] = true' in schemas
    assert '["actions"] = true' in schemas
    assert "buy_commodity" in schemas


def test_generator_writes_target_catalog_lua_and_tocs_without_toml_staging(tmp_path: Path) -> None:
    _copy_generator_trees(tmp_path)
    target = tmp_path / "verified-beta.json"
    catalog = tmp_path / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(ROOT / "src/crowley/data/first_run.json", catalog)
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--write",
            "--target",
            str(target),
            "--catalog",
            str(catalog),
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(
        (tmp_path / "src/crowley/data/targets/verified-beta.json").read_text(encoding="utf-8")
    )
    assert "installation" not in rendered
    assert (tmp_path / "addon/Crowley/GeneratedTarget.lua").is_file()
    assert not (tmp_path / "stage").exists()


def test_generator_rebinds_catalog_for_an_alternate_target(tmp_path: Path) -> None:
    _copy_generator_trees(tmp_path)
    catalog_value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_value["items"][0]["status"] = "research"
    catalog = tmp_path / "first_run.json"
    catalog.write_text(json.dumps(catalog_value), encoding="utf-8")
    target_value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    target_value["targetId"] = "synthetic-target"
    target = tmp_path / "synthetic-target.json"
    target.write_text(json.dumps(target_value), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--write",
            "--target",
            str(target),
            "--catalog",
            str(catalog),
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = json.loads(
        (tmp_path / "src/crowley/data/targets/synthetic-target.json").read_text(encoding="utf-8")
    )
    assert rendered["targetId"] == "synthetic-target"
    generated = (tmp_path / "addon/Crowley/GeneratedTarget.lua").read_text(encoding="utf-8")
    assert "count = 427" in generated


def test_generator_rejects_broken_toc_load_order() -> None:
    from tools.generate_launch_target import GenerationError, _validate_toc_load_order

    with pytest.raises(GenerationError, match="exactly one GeneratedTarget"):
        _validate_toc_load_order("Context.lua\nScanner.lua\n", "fixture.toc")
    with pytest.raises(GenerationError, match="exactly one Util.lua"):
        _validate_toc_load_order("GeneratedTarget.lua\nContext.lua\nScanner.lua\n", "fixture.toc")
    with pytest.raises(GenerationError, match="Util.lua immediately after GeneratedTarget.lua"):
        _validate_toc_load_order(
            "GeneratedTarget.lua\nCore.lua\nUtil.lua\nContext.lua\nScanner.lua\n", "fixture.toc"
        )
    with pytest.raises(GenerationError, match="before Context.lua"):
        _validate_toc_load_order("Context.lua\nGeneratedTarget.lua\nUtil.lua\nScanner.lua\n", "fixture.toc")


def test_generator_check_mode_never_writes_missing_artifacts(tmp_path: Path) -> None:
    _copy_generator_trees(tmp_path)
    target = tmp_path / "verified-beta.json"
    catalog = tmp_path / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)

    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--check",
            "--target",
            str(target),
            "--catalog",
            str(catalog),
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not (tmp_path / "addon/Crowley/GeneratedTarget.lua").exists()


def test_historical_route_is_refused_for_changed_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = tmp_path / "routes.json"
    shutil.copy2(ROUTE_PATH, route)
    alternate = replace(packaged_game_target(), full_build="1.60.1.70451", native_build="70451")
    monkeypatch.setattr(daemon, "packaged_game_target", lambda: alternate)
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))


def test_historical_route_requires_target_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    route = tmp_path / "routes.json"
    shutil.copy2(ROUTE_PATH, route)
    alternate = replace(packaged_game_target(), route_provenance="different measured provenance")
    monkeypatch.setattr(daemon, "packaged_game_target", lambda: alternate)
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))


def test_route_file_requires_exact_target_provenance(tmp_path: Path) -> None:
    route = tmp_path / "routes.json"
    value = json.loads(ROUTE_PATH.read_text(encoding="utf-8"))
    value["kinematics"]["provenance"] = "measured on an unverified client"
    route.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(daemon.DriverError, match="provenance"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))
