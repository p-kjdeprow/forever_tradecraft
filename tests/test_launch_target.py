"""Focused checks for the packaged launch target and deterministic renderer."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.core.game_target import GameTargetError, load_game_target, packaged_game_target, target_scan_plan
from crowley.core.watchlist import Catalog, load_catalog
from crowley.session import controller as controller_module
from crowley.session import daemon, protocol
from crowley.session.controller import Controller, ControllerState
from crowley.session.journal import DEFAULT_STATE, Journal

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "generate_launch_target.py"
TARGET_PATH = ROOT / "src" / "crowley" / "data" / "game_target.json"
CATALOG_PATH = ROOT / "src" / "crowley" / "data" / "first_run.json"


def test_packaged_target_and_catalog_are_coherent_and_generated_bytes_are_current():
    target = packaged_game_target()
    catalog = load_catalog()
    plan = target_scan_plan(catalog, target)
    assert target.full_build == catalog.client_build
    assert target.native_build == "70450"
    assert target.native_region_id == 110
    assert (
        target.game_root == "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft"
    )
    assert target.wine_prefix == "/home/dev/Games/world-of-warcraft"
    assert len(plan["itemIDs"]) == 428
    assert plan["planHash"] == "f1745d85c071b350ebea242add2a7b38e3f53df5bcc8a6df1f19002f995a31d4"
    assert protocol.PLAN_COUNT == len(plan["itemIDs"])
    assert protocol.PLAN_CADENCE == target.cadence_seconds
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_generated_wire_schema_artifact_is_current():
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


def test_default_watchlist_cli_exports_the_packaged_target_plan():
    result = subprocess.run(
        [sys.executable, "-m", "crowley.core.watchlist", "export"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == target_scan_plan(load_catalog(), packaged_game_target())


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("unknown", "unknown fields"),
        ("missing", "missing fields"),
        ("installation_missing", "missing fields"),
        ("installation_relative", "absolute non-root path"),
        ("installation_root", "absolute non-root path"),
        ("installation_traversal", "absolute non-root path"),
        ("native_region_missing", "missing fields"),
        ("bool", "integer"),
        ("native_region_bool", "integer"),
        ("native_region_zero", "integer"),
        ("schema_float", "must be 1"),
        ("region", "house values are invalid"),
        ("ruleset", "house values are invalid"),
        ("product_dir", "one relative path component"),
        ("locale", "supported enUS"),
        ("house", "does not match"),
    ],
)
def test_target_validation_rejects_unknown_or_inconsistent_identity(tmp_path: Path, path: str, message: str):
    value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    if path == "unknown":
        value["unexpected"] = True
    elif path == "missing":
        del value["client"]["windowClass"]
    elif path == "installation_missing":
        del value["installation"]["winePrefix"]
    elif path == "installation_relative":
        value["installation"]["gameRoot"] = "games/world-of-warcraft"
    elif path == "installation_root":
        value["installation"]["winePrefix"] = "/"
    elif path == "installation_traversal":
        value["installation"]["winePrefix"] = "/home/dev/Games/../Games/world-of-warcraft"
    elif path == "native_region_missing":
        del value["context"]["nativeRegionId"]
    elif path == "bool":
        value["client"]["interface"] = True
    elif path == "native_region_bool":
        value["context"]["nativeRegionId"] = True
    elif path == "native_region_zero":
        value["context"]["nativeRegionId"] = 0
    elif path == "schema_float":
        value["schemaVersion"] = 1.0
    elif path == "region":
        value["context"]["region"] = "invalid"
    elif path == "ruleset":
        value["context"]["ruleset"] = "invalid"
    elif path == "product_dir":
        value["client"]["productDir"] = "../_classic_beta_"
    elif path == "locale":
        value["client"]["locale"] = "deDE"
    else:
        value["context"]["house"] = "beta-normal-horde"
    target = tmp_path / "target.json"
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(GameTargetError, match=message):
        load_game_target(target)


def _copy_generator_tree(root: Path) -> None:
    for relative in (
        "addon/Crowley/Crowley.toc",
        "addon/Crowley_Data/Crowley_Data.toc",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)


def test_generator_rebinds_catalog_after_target_edit_and_consumes_plan_size(tmp_path: Path):
    _copy_generator_tree(tmp_path)
    source = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    source["items"][0]["status"] = "research"
    catalog = tmp_path / "first_run.json"
    catalog.write_text(json.dumps(source), encoding="utf-8")
    target_value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    target_value["targetId"] = "synthetic-target"
    target_value["client"]["fullBuild"] = "1.60.1.70451"
    target = tmp_path / "game_target.json"
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
    rendered_target = json.loads((tmp_path / "src/crowley/data/game_target.json").read_text(encoding="utf-8"))
    assert rendered_target["client"]["fullBuild"] == "1.60.1.70451"
    assert rendered_target["context"]["nativeRegionId"] == 110
    generated = (tmp_path / "addon/Crowley/GeneratedTarget.lua").read_text(encoding="utf-8")
    assert 'fullBuild = "1.60.1.70451"' in generated
    assert "nativeRegionId = 110" in generated
    assert "count = 427" in generated
    rebound = Catalog.model_validate_json(
        (tmp_path / "src/crowley/data/first_run.json").read_text(encoding="utf-8")
    )
    assert rebound.client_build == "1.60.1.70451"
    assert len(rebound.scan_plan()["itemIDs"]) == 427


def test_generator_uses_complete_checkout_tocs_for_empty_alternate_root(tmp_path: Path):
    target = tmp_path / "game_target.json"
    catalog = tmp_path / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)
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
    toc = (tmp_path / "addon/Crowley/Crowley.toc").read_text(encoding="utf-8")
    entries = [line.strip() for line in toc.splitlines() if line.strip() and not line.startswith("##")]
    assert entries.count("GeneratedTarget.lua") == 1
    assert "Bridge.lua" not in entries
    assert "Auctionator" not in toc
    assert entries.index("GeneratedTarget.lua") < entries.index("Context.lua") < entries.index("Scanner.lua")


def test_generator_rejects_broken_target_load_order():
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


def test_check_never_writes_missing_or_drifted_artifacts(tmp_path: Path):
    _copy_generator_tree(tmp_path)
    target = tmp_path / "game_target.json"
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


def test_staged_config_render_does_not_touch_live_deploy_files(tmp_path: Path):
    output_root = tmp_path / "output"
    _copy_generator_tree(output_root)
    (output_root / "deploy/config").mkdir(parents=True)
    for name in ("session.toml", "companion.toml"):
        shutil.copy2(ROOT / "deploy/config" / name, output_root / "deploy/config" / name)
    target = output_root / "game_target.json"
    catalog = output_root / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)
    live_before = {
        name: (ROOT / "deploy/config" / name).read_bytes() for name in ("session.toml", "companion.toml")
    }
    stage = tmp_path / "stage"
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
            str(output_root),
            "--stage-config-dir",
            str(stage),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    staged = (stage / "session.toml").read_text(encoding="utf-8")
    staged_companion = (stage / "companion.toml").read_text(encoding="utf-8")
    assert 'client_build = "70450"' in staged
    assert 'house = "beta-normal-alliance"' in staged
    assert 'wine_prefix = "/home/dev/Games/world-of-warcraft"' in staged
    assert (
        'saved_variables = "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_/WTF/Account/850981908#1/SavedVariables/Crowley.lua"'
        in staged
    )
    assert (
        'data_lua = "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_/Interface/AddOns/Crowley_Data/Data.lua"'
        in staged
    )
    assert (
        'wow_dir = "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_"'
        in staged_companion
    )
    assert all((ROOT / "deploy/config" / name).read_bytes() == before for name, before in live_before.items())
    assert (stage / "session.toml").stat().st_mode & 0o777 == 0o600


def test_staging_requires_saved_variables_and_data_paths_before_any_write(tmp_path: Path):
    output_root = tmp_path / "output"
    (output_root / "deploy/config").mkdir(parents=True)
    for relative in ("deploy/config/session.toml", "deploy/config/companion.toml"):
        source = ROOT / relative
        destination = output_root / relative
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    session = output_root / "deploy/config/session.toml"
    session.write_text(
        "\n".join(
            line
            for line in session.read_text(encoding="utf-8").splitlines()
            if not line.startswith("data_lua =")
        )
        + "\n",
        encoding="utf-8",
    )
    target = output_root / "game_target.json"
    catalog = output_root / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)
    stage = tmp_path / "stage"
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
            str(output_root),
            "--stage-config-dir",
            str(stage),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "session.data_lua is required" in result.stderr
    assert not (output_root / "addon/Crowley/GeneratedTarget.lua").exists()
    assert not stage.exists()


def test_staging_rebinds_changed_installation_and_preserves_unrelated_pins(tmp_path: Path):
    output_root = tmp_path / "output"
    _copy_generator_tree(output_root)
    (output_root / "deploy/config").mkdir(parents=True)
    session = (
        (ROOT / "deploy/config/session.toml")
        .read_text(encoding="utf-8")
        .replace(
            "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_",
            "/fixture/old-game/_old_",
        )
    )
    companion = (ROOT / "deploy/config/companion.toml").read_text(encoding="utf-8")
    (output_root / "deploy/config/session.toml").write_text(session, encoding="utf-8")
    (output_root / "deploy/config/companion.toml").write_text(
        companion.replace(
            'wow_dir = "/home/dev/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_classic_beta_"',
            'wow_dir = "/fixture/old-game/_old_"',
        ).replace(
            'server_url = "http://127.0.0.1:8080"',
            'server_url = "http://fixture.example:8080"\nhouse_key = "beta-normal-alliance"',
        ),
        encoding="utf-8",
    )
    target_value = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    target_value["installation"] = {
        "gameRoot": "/fixture/new-game",
        "winePrefix": "/fixture/new-prefix",
    }
    target_value["client"]["productDir"] = "_alternate_"
    target = output_root / "game_target.json"
    catalog = output_root / "first_run.json"
    target.write_text(json.dumps(target_value), encoding="utf-8")
    shutil.copy2(CATALOG_PATH, catalog)
    stage = tmp_path / "stage"
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
            str(output_root),
            "--stage-config-dir",
            str(stage),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    staged_session = (stage / "session.toml").read_text(encoding="utf-8")
    staged_companion = (stage / "companion.toml").read_text(encoding="utf-8")
    assert 'wine_prefix = "/fixture/new-prefix"' in staged_session
    assert (
        'saved_variables = "/fixture/new-game/_alternate_/WTF/Account/850981908#1/SavedVariables/Crowley.lua"'
        in staged_session
    )
    assert (
        'data_lua = "/fixture/new-game/_alternate_/Interface/AddOns/Crowley_Data/Data.lua"' in staged_session
    )
    assert 'wow_dir = "/fixture/new-game/_alternate_"' in staged_companion
    assert 'house_key = "beta-normal-alliance"' in staged_companion
    assert 'server_url = "http://fixture.example:8080"' in staged_companion
    assert "/home/dev/Games/world-of-warcraft" not in staged_session
    assert "/home/dev/Games/world-of-warcraft" not in staged_companion


def test_staged_config_rejects_live_config_directory(tmp_path: Path):
    output_root = tmp_path / "output"
    _copy_generator_tree(output_root)
    (output_root / "deploy/config").mkdir(parents=True)
    for name in ("session.toml", "companion.toml"):
        shutil.copy2(ROOT / "deploy/config" / name, output_root / "deploy/config" / name)
    target = output_root / "game_target.json"
    catalog = output_root / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)
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
            str(output_root),
            "--stage-config-dir",
            str(output_root / "deploy/config"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "live deploy/config" in result.stderr


def test_custom_output_root_cannot_stage_into_checkout_live_config(tmp_path: Path):
    output_root = tmp_path / "output"
    _copy_generator_tree(output_root)
    (output_root / "deploy/config").mkdir(parents=True)
    for name in ("session.toml", "companion.toml"):
        shutil.copy2(ROOT / "deploy/config" / name, output_root / "deploy/config" / name)
    target = output_root / "game_target.json"
    catalog = output_root / "first_run.json"
    shutil.copy2(TARGET_PATH, target)
    shutil.copy2(CATALOG_PATH, catalog)
    live_before = {
        name: (ROOT / "deploy/config" / name).read_bytes() for name in ("session.toml", "companion.toml")
    }
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
            str(output_root),
            "--stage-config-dir",
            str(ROOT / "deploy/config"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "live deploy/config" in result.stderr
    assert all((ROOT / "deploy/config" / name).read_bytes() == before for name, before in live_before.items())


def test_historical_route_is_refused_for_changed_target(monkeypatch, tmp_path: Path):
    route = tmp_path / "routes.json"
    shutil.copy2(ROOT / "deploy/config/routes/stormwind.json", route)
    alternate = replace(packaged_game_target(), full_build="1.60.1.70451", native_build="70451")
    monkeypatch.setattr(daemon, "packaged_game_target", lambda: alternate)
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))


def test_historical_route_requires_target_provenance_even_for_beta_build(monkeypatch, tmp_path: Path):
    route = tmp_path / "routes.json"
    shutil.copy2(ROOT / "deploy/config/routes/stormwind.json", route)
    alternate = replace(packaged_game_target(), route_provenance="different measured provenance")
    monkeypatch.setattr(daemon, "packaged_game_target", lambda: alternate)
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))


def test_historical_route_requires_exact_target_provenance(tmp_path: Path):
    route = tmp_path / "routes.json"
    value = json.loads((ROOT / "deploy/config/routes/stormwind.json").read_text(encoding="utf-8"))
    value["kinematics"]["provenance"] = "measured on an unverified client"
    route.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(daemon.DriverError, match="provenance"):
        daemon.SessionDaemon._load_routes(SimpleNamespace(routes_file=route))


def test_controller_qualification_consumes_alternate_canonical_plan_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A real alternate producer/consumer plan reaches controller qualification."""

    from crowley.session import evidence as evidence_module

    from .test_addon import non_beta_target, run_sweep
    from .wow_harness import Auction, WowClient

    catalog = load_catalog()
    selected = {873, 2555, 2589, 4238}
    alternate_catalog = catalog.model_copy(
        update={
            "items": [
                item.model_copy(update={"status": "active" if item.item_id in selected else "excluded"})
                for item in catalog.items
            ]
        }
    )
    alternate_target = replace(non_beta_target(), faction="Horde", house="us-normal-horde")
    alternate_plan = target_scan_plan(alternate_catalog, alternate_target)
    assert len(alternate_plan["itemIDs"]) == 4
    monkeypatch.setattr(controller_module, "_CANONICAL_PLAN", alternate_plan)
    monkeypatch.setattr(controller_module, "_PLAN_COUNT", len(alternate_plan["itemIDs"]))
    monkeypatch.setattr(protocol, "PLAN_COUNT", len(alternate_plan["itemIDs"]))
    monkeypatch.setattr(evidence_module, "_CANONICAL_PLAN", alternate_plan)
    monkeypatch.setattr(evidence_module, "_CANONICAL_CATALOG", alternate_catalog)
    monkeypatch.setattr(evidence_module, "_PLAN_COUNT", len(alternate_plan["itemIDs"]))
    monkeypatch.setattr(evidence_module, "_TARGET", alternate_target)

    client = WowClient(
        target=alternate_target,
        catalog=alternate_catalog,
        faction="Horde",
        realm=alternate_target.realm,
        current_region=alternate_target.native_region_id,
    )
    client.set_owned([Auction(2589, "Linen Cloth", 1, 100, status="sold")], complete=True)
    raw = run_sweep(client)
    assert client.ns.GeneratedTarget.count == 4
    produced_snapshot = client.publish_agent()
    validated_snapshot = protocol.validate_snapshot(
        produced_snapshot,
        plan_item_ids=alternate_plan["itemIDs"],
    )
    decoded_frame = protocol.decode_frame(
        client.agent_frame,
        plan_item_ids=alternate_plan["itemIDs"],
    )
    assert decoded_frame.snapshot.value == validated_snapshot.value
    saved_path = tmp_path / "SavedVariables.lua"
    saved_path.write_bytes(client.saved_variables())
    data_path = tmp_path / "Data.lua"
    data_value = client.lua.globals().CROWLEY_DATA
    data_value.ackedScans = client._to_lua([raw["id"]])
    data_value.referenceItemIDs = client._to_lua(sorted(item.item_id for item in alternate_catalog.items))
    data_path.write_bytes(client.lua.globals()._serialize("CROWLEY_DATA", data_value).encode("latin-1"))
    assert {key for key, _ in data_value.items()} == {
        "generatedAt",
        "ackedScans",
        "scanPlan",
        "referenceItemIDs",
    }
    evidence = evidence_module.read_stable_saved_scan(
        saved_path,
        uid=raw["id"],
        house=alternate_target.house,
        plan_hash=alternate_plan["planHash"],
        expected_plan_json=alternate_plan["planJSON"],
        expected_context={
            "clientBuild": alternate_target.native_build,
            "interface": alternate_target.interface,
            "locale": alternate_target.locale,
            "realm": alternate_target.realm,
            "playerFaction": alternate_target.faction,
        },
        planned=4,
    )
    assert evidence.house == alternate_target.house
    assert evidence.plan_hash == alternate_plan["planHash"]
    assert evidence.observations == 4
    data_acknowledged_ids = evidence_module.read_data_acknowledged_ids(
        data_path,
        expected_plan_json=alternate_plan["planJSON"],
        expected_plan_hash=alternate_plan["planHash"],
        expected_client_build=alternate_target.full_build,
    )
    assert data_acknowledged_ids == (raw["id"],)
    data_ack_uid = data_acknowledged_ids[0]
    initial = json.loads(json.dumps(DEFAULT_STATE))
    initial["state"] = ControllerState.WAIT_DUE.value
    initial["resumeState"] = ControllerState.WAIT_DUE.value
    controller = Controller(
        Journal(tmp_path / "controller.json", initial=initial),
        recover_on_restart=False,
    )
    started_at = int(evidence.envelope["startedAt"])
    finished_at = int(evidence.envelope["finishedAt"])
    controller.begin_scan(started_at=started_at)
    controller.saved(evidence.uid, stable=True)
    controller.reload_sent()
    controller.expect_boot("boot-1")
    controller.observe_frame(boot="boot-2", sequence=1, crc32=1)
    controller.stable_saved(evidence)
    controller.uploaded(
        evidence.uid,
        accepted=True,
        upload_time=finished_at + 10,
        data_ack_uid=data_ack_uid,
    )
    controller.record_ack_publication(
        evidence.uid,
        companion_uid=evidence.uid,
        data_uid=data_ack_uid,
        publication_time=finished_at + 11,
    )
    controller.ack_reload_command_sent()
    controller.expect_boot("boot-2", acknowledgement=True)
    controller.observe_frame(boot="boot-3", sequence=2, crc32=2)
    controller.ack_reload_complete(
        pending_ids=set(),
        loaded_uid=evidence.uid,
        loaded_at=finished_at + 12,
        boot="boot-3",
        sequence=2,
    )
    assert controller.status().state is ControllerState.REOPEN
    assert controller.qualify(evidence, started_at=started_at).streak == 1
