"""Focused checks for packaged targets and deterministic generation."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crowley.core.game_target import (
    GameTarget,
    GameTargetError,
    canonical_plan,
    load_game_target,
    load_target,
    packaged_game_target,
)
from crowley.core.watchlist import Catalog

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "generate_launch_target.py"
TARGET_PATH = ROOT / "src" / "crowley" / "data" / "targets" / "verified-beta.json"
CATALOG_PATH = ROOT / "src" / "crowley" / "data" / "first_run.json"
BETA_CATALOG_PATH = ROOT / "tests" / "fixtures" / "catalogs" / "verified-beta.json"


def test_selected_target_is_identity_only_and_plan_is_cached(
    packaged_selection_target: GameTarget,
    packaged_catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crowley.core import watchlist

    target = packaged_selection_target
    canonical_plan.cache_clear()

    def real_catalog_loader(*, enforce_target: bool = True) -> Catalog:
        del enforce_target
        return packaged_catalog

    monkeypatch.setattr(watchlist, "load_catalog", real_catalog_loader)
    assert target == load_target(target.target_id)
    value = json.loads(
        (ROOT / "src" / "crowley" / "data" / "targets" / f"{target.target_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert "installation" not in value
    assert not hasattr(target, "installation")
    assert not hasattr(target, "game_root")
    assert not hasattr(target, "wine_prefix")
    assert len(canonical_plan(target)["itemIDs"]) == 790
    assert len(canonical_plan(target)["pools"]["cold"]) == 368
    assert canonical_plan(target) is canonical_plan(target)
    assert target.full_build == packaged_catalog.client_build


def test_fixture_target_pin_is_independent_of_packaged_selection(
    packaged_selection_target: GameTarget,
) -> None:
    selected_id = (
        (ROOT / "src" / "crowley" / "data" / "targets" / "selected").read_text(encoding="ascii").strip()
    )

    assert packaged_game_target().target_id == "verified-beta"
    assert packaged_selection_target.target_id == selected_id


def test_real_packaged_selection_binds_real_catalog_in_fresh_process(tmp_path: Path) -> None:
    script = """
import sys

sys.path.insert(0, PACKAGE_ROOT)
from crowley.core.game_target import selected_target
from crowley.core.watchlist import load_catalog

target = selected_target()
catalog = load_catalog()
assert target.catalog_id == catalog.id
assert target.catalog_revision == catalog.revision
assert target.full_build == catalog.client_build
assert target.initial_price_levels == catalog.initial_price_levels
print(target.target_id)
""".replace("PACKAGE_ROOT", repr(str(ROOT / "src")))
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


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
    shutil.copy2(BETA_CATALOG_PATH, catalog)
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
    catalog_value = json.loads(BETA_CATALOG_PATH.read_text(encoding="utf-8"))
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
    assert "count = 479" in generated


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
    shutil.copy2(BETA_CATALOG_PATH, catalog)

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
    assert "generated artifacts drifted:" in result.stderr
    assert "GeneratedTarget.lua" in result.stderr
    assert not (tmp_path / "addon/Crowley/GeneratedTarget.lua").exists()
