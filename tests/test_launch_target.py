"""Focused checks for packaged targets and deterministic generation."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from crowley.companion.config import CompanionSettings
from crowley.companion.sync import Companion
from crowley.core import savedvars
from crowley.core.game_target import (
    GameTargetError,
    canonical_plan,
    load_game_target,
    load_target,
    packaged_game_target,
    selected_target,
    target_scan_plan,
)
from crowley.core.watchlist import load_catalog
from crowley.session import daemon
from crowley.session.controller import Controller
from crowley.session.decoder import PixelDecoder
from crowley.session.evidence import qualify_saved_scan, read_data_acknowledged_ids
from crowley.session.journal import DEFAULT_STATE, Journal
from crowley.session.protocol import ProtocolContract, decode_frame
from tests.wow_harness import Auction, WowClient

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


def test_alternate_target_runs_through_lua_wire_readers_and_qualification(tmp_path: Path) -> None:
    base_catalog = load_catalog()
    alternate_catalog = base_catalog.model_copy(
        update={
            "client_build": "1.60.1.70451",
            "items": [
                base_catalog.items[0].model_copy(update={"status": "research"}),
                *base_catalog.items[1:],
            ],
        }
    )
    alternate = replace(
        packaged_game_target(),
        target_id="synthetic-target",
        full_build="1.60.1.70451",
        native_build="70451",
    )
    alternate.validate_catalog(alternate_catalog)
    plan = target_scan_plan(alternate_catalog, alternate)
    contract = ProtocolContract.from_plan(plan, sweep_cap_seconds=alternate.sweep_cap_seconds)

    client = WowClient(
        faction="Alliance",
        realm=alternate.realm,
        target=alternate,
        catalog=alternate_catalog,
    )
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(
                itemID=item_id,
                name=f"Item {item_id}",
                count=1,
                buyout=100 + ordinal,
                isCommodity=ordinal == 1,
                classID={"recipes": 9, "containers": 1, "equipment": 2}.get(
                    next(item.category for item in alternate_catalog.items if item.item_id == item_id),
                    7,
                ),
            )
            for ordinal, item_id in enumerate(plan["itemIDs"], 1)
        ]
    )
    client.set_owned([Auction(plan["itemIDs"][0], "Owned item", 1, 1)])
    client.state.fastCanonical = False
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    for _ in range(700):
        # Keep the full canonical fixture within the harness clock while the
        # addon still performs every real query and persists every result.
        client.lua.globals().CrowleyDB.querySendTimes = client.lua.table()
        client.state.sendLog = client.lua.table()
        client.run_timers()
        if list(client.lua.globals().CrowleyDB.scans.items()):
            break
        client.advance_with_timers(1)
    saved_root = savedvars.loads(client.saved_variables())
    saved_db = saved_root["CrowleyDB"]
    scans = saved_db["scans"]
    assert len(scans) == 1
    uid, persisted_envelope = next(iter(scans.items()))
    assert persisted_envelope["numObservations"] == 427
    client.publish_agent()
    decoded_frame = decode_frame(client.agent_frame, contract=contract)
    assert decoded_frame.snapshot.value["plan"]["count"] == 427
    decoded_pixels = PixelDecoder(contract=contract).decode(
        client.render_agent_capture(), monotonic=1.0, window_identity="alternate-target"
    )
    assert decoded_pixels.frame is not None
    assert decoded_pixels.frame.snapshot.value["context"]["clientBuild"] == "70451"

    game_dir = tmp_path / alternate.product_dir
    account_dir = game_dir / "WTF" / "Account" / "account#1"
    saved = account_dir / "SavedVariables" / "Crowley.lua"
    data = game_dir / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    queue = game_dir / "Interface" / "AddOns" / "Crowley_Data" / "Queue.lua"
    saved.parent.mkdir(parents=True)
    data.parent.mkdir(parents=True)
    saved.write_bytes(client.saved_variables())
    saved.chmod(0o600)
    settings = CompanionSettings(
        target=alternate,
        catalog=alternate_catalog,
        canonical_plan=plan,
        route_provenance=alternate.route_provenance,
        wow_dir=game_dir,
        account_dir=account_dir,
        server_url="http://server.invalid",
        api_token=SecretStr("test-token"),
        wtf_account="account#1",
        character=None,
        house_key=None,
        state_file=tmp_path / "companion.json",
        saved_variables=saved,
        data_lua=data,
        queue_lua=queue,
        addons_dir=game_dir / "Interface" / "AddOns",
    )
    companion = Companion(settings)

    reference_ids = sorted(item.item_id for item in alternate_catalog.items)

    def request(
        method: str,
        endpoint: str,
        *,
        json_body: object,
        params: object = None,
    ) -> httpx.Response:
        del method, json_body, params
        if endpoint == "/v1/scans":
            return httpx.Response(200, json={"house": alternate.house, "listings": 427, "status": "accepted"})
        if endpoint == "/v1/addon-data":
            return httpx.Response(
                200,
                json={
                    "generatedAt": 1,
                    "ackedScans": [uid],
                    "scanPlan": plan,
                    "referenceItemIDs": reference_ids,
                },
            )
        if endpoint in {"/v1/account-state", "/v1/item-reference"}:
            return httpx.Response(200, json={"status": "accepted", "items": 1})
        if endpoint == "/v1/probes":
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(f"unexpected companion endpoint: {endpoint}")

    companion._request = request
    companion.once()

    persisted = savedvars.load_file(saved)
    persisted_envelope = persisted["CrowleyDB"]["scans"][uid]
    expected_context = persisted_envelope["context"]
    evidence = qualify_saved_scan(
        saved.read_bytes(),
        uid=uid,
        house=alternate.house,
        plan_hash=plan["planHash"],
        expected_plan_json=plan["planJSON"],
        expected_context=expected_context,
        catalog=alternate_catalog,
    )
    assert evidence.observations == evidence.attempted == 427

    assert read_data_acknowledged_ids(
        data,
        expected_plan_json=plan["planJSON"],
        expected_plan_hash=plan["planHash"],
        expected_client_build=plan["targetClientBuild"],
        expected_reference_item_ids=reference_ids,
    ) == (uid,)

    def reloaded_client() -> WowClient:
        loaded = WowClient(
            faction="Alliance",
            realm=alternate.realm,
            target=alternate,
            catalog=alternate_catalog,
            initial_savedvars=persisted["CrowleyDB"],
        )
        loaded.run_file(data)
        loaded.login()
        loaded.publish_agent()
        return loaded

    save_reload_client = reloaded_client()
    ack_reload_client = reloaded_client()
    save_frame = decode_frame(save_reload_client.agent_frame, contract=contract)
    ack_frame = decode_frame(ack_reload_client.agent_frame, contract=contract)
    assert save_frame.boot != decoded_frame.boot
    assert ack_frame.boot != save_frame.boot

    journal = Journal(tmp_path / "session.json")
    journal.write({**DEFAULT_STATE, "state": "WAIT_DUE"})
    controller = Controller(
        journal,
        recover_on_restart=False,
        cadence_seconds=alternate.cadence_seconds,
        sweep_cap_seconds=alternate.sweep_cap_seconds,
        plan_count=len(plan["itemIDs"]),
        protocol_contract=contract,
    )
    started_at = persisted_envelope["startedAt"]
    finished_at = persisted_envelope["finishedAt"]
    controller.begin_scan(started_at=started_at)
    controller.saved(uid, stable=True)
    controller.reload_sent()
    controller.expect_boot(decoded_frame.boot)
    controller.observe_frame(
        boot=save_frame.boot,
        sequence=save_frame.sequence,
        crc32=save_frame.crc32,
    )
    controller.stable_saved(evidence)
    controller.uploaded(uid, accepted=True, upload_time=finished_at + 1, data_ack_uid=uid)
    controller.record_ack_publication(
        uid,
        companion_uid=uid,
        data_uid=uid,
        publication_time=finished_at + 2,
        boot=save_frame.boot,
        sequence=save_frame.sequence,
    )
    controller.ack_reload_command_sent()
    controller.expect_boot(save_frame.boot, acknowledgement=True)
    controller.observe_frame(
        boot=ack_frame.boot,
        sequence=ack_frame.sequence,
        crc32=ack_frame.crc32,
    )
    controller.ack_reload_complete(
        pending_ids=set(),
        loaded_uid=uid,
        loaded_at=finished_at + 3,
        boot=ack_frame.boot,
        sequence=ack_frame.sequence,
    )
    assert controller.qualify(evidence, started_at=started_at).streak == 1


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


def test_historical_route_is_refused_for_changed_target(tmp_path: Path) -> None:
    route = tmp_path / "routes.json"
    shutil.copy2(ROUTE_PATH, route)
    alternate = replace(packaged_game_target(), full_build="1.60.1.70451", native_build="70451")
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(
            SimpleNamespace(
                routes_file=route,
                target=alternate,
                route_provenance=alternate.route_provenance,
            )
        )


def test_historical_route_requires_target_provenance(tmp_path: Path) -> None:
    route = tmp_path / "routes.json"
    shutil.copy2(ROUTE_PATH, route)
    alternate = replace(packaged_game_target(), route_provenance="different measured provenance")
    with pytest.raises(daemon.DriverError, match="historical beta kinematics"):
        daemon.SessionDaemon._load_routes(
            SimpleNamespace(
                routes_file=route,
                target=alternate,
                route_provenance=alternate.route_provenance,
            )
        )


def test_route_file_requires_exact_target_provenance(tmp_path: Path) -> None:
    route = tmp_path / "routes.json"
    value = json.loads(ROUTE_PATH.read_text(encoding="utf-8"))
    value["kinematics"]["provenance"] = "measured on an unverified client"
    route.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(daemon.DriverError, match="provenance"):
        target = packaged_game_target()
        daemon.SessionDaemon._load_routes(
            SimpleNamespace(
                routes_file=route,
                target=target,
                route_provenance=target.route_provenance,
            )
        )
