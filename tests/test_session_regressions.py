"""Deterministic production regressions for the repeating-session engine.

These tests exercise the public session boundaries with fake clocks, files,
drivers, and model processes.  They deliberately keep the actual format-2
payload and the actual four-key companion schema in the fixtures so an engine
change cannot pass by teaching a test fixture a legacy shape.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import socket
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from crowley.core.game_target import packaged_game_target, target_scan_plan
from crowley.core.luawriter import render_assignment
from crowley.core.scanformat import (
    ScanFormatError,
    decode_json_payload,
    encode_json_payload,
    validate_v2_envelope,
)
from crowley.core.watchlist import load_catalog
from crowley.session import Controller, ControllerState, SessionConfig, SessionDaemon
from crowley.session.calibration import InputCalibrationEvidence, NativeCalibrationEvidence
from crowley.session.config import load_config
from crowley.session.decoder import PixelDecoder, ProtocolError, Sample
from crowley.session.driver import DesktopDriver, DriverProfile, RecordingBackend, WindowIdentity
from crowley.session.evidence import EvidenceError, SweepEvidence, read_data_acknowledged_ids
from crowley.session.guardian import GuardianError
from crowley.session.journal import DEFAULT_STATE, Journal
from crowley.session.model import LunaAdapter, ModelError
from crowley.session.protocol import decode_frame, encode_frame, pack_cells


def _executor_none() -> dict:
    return {
        "availability": "none",
        "queueId": "",
        "house": "",
        "character": "",
        "publishedArmed": False,
        "sessionArmed": False,
        "buttonVisible": False,
        "buttonName": "",
        "currentSeq": 0,
        "currentKind": "",
        "phase": "none",
        "inFlight": False,
        "clickReady": False,
        "workDone": 0,
        "workTotal": 0,
        "blocked": False,
        "latched": False,
        "ambiguous": False,
        "clickOrdinal": 0,
        "lastClickSource": "none",
        "resultCount": 0,
        "lastResult": {"seq": 0, "kind": "", "status": "none"},
    }


def _snapshot(
    *,
    wall: int = 1_000,
    boot: str = "boot-1",
    sequence: int = 1,
    phase: str = "item",
    pending: list[str] | None = None,
    loaded: list[str] | None = None,
    ah_open: bool = True,
    schedule_fence_state: str = "inactive",
    saved_uid: str = "",
) -> dict:
    plan = load_catalog().scan_plan()
    active = phase in {"item", "owned"}
    started = wall if active else 0
    due = started + 900 if active else wall
    return {
        "schema": 6,
        "boot": boot,
        "sequence": sequence,
        "wallTime": wall,
        "uptimeMs": sequence,
        "context": {
            "addonVersion": "0.2.0",
            "interface": 16001,
            "clientBuild": "70450",
            "character": "Arthur",
            "realm": "Classic Beta PvE 2",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
        },
        "house": {"key": "beta-normal-alliance", "source": "auto"},
        "plan": {
            "hash": plan["planHash"],
            "count": 428,
            "cadenceSeconds": 900,
            "sweepCapSeconds": 600,
            "initialPriceLevels": 20,
        },
        "ahOpen": ah_open,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": _executor_none(),
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": True,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
        "scanner": {
            "phase": phase,
            "activeSweepId": "",
            "startedAt": started,
            "dueAt": due,
            "ordinal": 1 if active else 0,
            "planned": 428,
            "cursorItemID": plan["itemIDs"][0] if active else 0,
            "scheduleFence": {
                "state": schedule_fence_state,
                "reason": "",
                "command": "/crowley session fence",
            },
        },
        "lastSaved": {
            "memoryUid": saved_uid,
            "finishedAt": wall if saved_uid else 0,
            "planned": 428 if saved_uid else 0,
            "attempted": 428 if saved_uid else 0,
            "failures": 0,
            "rows": 1 if saved_uid else 0,
            "persistence": "unknown",
        },
        "pending": {
            "ids": sorted(pending or []),
            "count": len(pending or []),
            "cap": 6,
            "blocked": False,
            "reason": "",
        },
        "loaded": {"ackedIds": sorted(loaded or []), "generatedAt": wall},
        "chatEdit": {"active": False, "focused": False, "text": ""},
        "motion": {
            "position": {"x": 100.25, "y": 200.5, "z": 30.75, "instance": 1},
            "facing": 1.25,
            "speed": 0,
            "combat": False,
            "deadOrGhost": False,
            "bindings": {
                "moveForward": "W",
                "moveBackward": "S",
                "strafeLeft": "A",
                "strafeRight": "D",
                "turnLeft": "Q",
                "jump": "SPACE",
                "turnRight": "E",
            },
            "native": {
                "ctm": {
                    "accessor": "C_CVar.GetCVarBool",
                    "cvar": "autointeract",
                    "state": "disabled",
                    "reason": "",
                },
                "ground": {
                    "state": "grounded",
                    "falling": "false",
                    "flying": "false",
                    "swimming": "false",
                    "submerged": "false",
                    "mounted": "false",
                    "taxi": "false",
                    "reason": "",
                },
                "speedTuple": {
                    "current": 0,
                    "run": 0,
                    "flight": 0,
                    "swim": 0,
                    "unitStatus": "unverified",
                },
            },
        },
        "render": {
            "constructionSeconds": 0.001,
            "updateCount": 100,
            "updateP99Ms": 1,
            "textureCount": 16_384,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": True,
                "beforeKiB": 1,
                "afterKiB": 2,
                "netKiB": 1,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        },
    }


def _sample(value: dict, *, monotonic: float = 1.0) -> Sample:
    frame = decode_frame(encode_frame(value))
    return Sample("FRESH", frame, monotonic, (80, 48, 256, 256), frame.crc32, "fake-window")


def _image_for_frame(value: dict) -> Image.Image:
    bits = pack_cells(encode_frame(value))
    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    for row, cells in enumerate(bits):
        for column, bit in enumerate(cells):
            if bit:
                for dy in range(2):
                    for dx in range(2):
                        pixels[column * 2 + dx, row * 2 + dy] = (255, 255, 255)
    return image


class _FakeDriver:
    """No-input driver with deterministic identity and action recording."""

    backend = object()

    def __init__(self, journal: Journal | None = None) -> None:
        self.events: list[dict] = []
        self.journal = journal
        self.guardian = SimpleNamespace(generation=0)
        self.guardian.revoke = self.revoke

    def resolve_identity(
        self, *, require_foreground: bool = True, deadline_monotonic: float | None = None
    ) -> SimpleNamespace:
        del require_foreground, deadline_monotonic
        return SimpleNamespace(fingerprint="fake-client")

    def run(self, name: str, generation: int, **kwargs: object) -> None:
        self.events.append({"name": name, "generation": generation, **kwargs})

    def revoke(self, reason: str) -> int:
        self.guardian.generation += 1
        return self.guardian.generation


def _state_file(path: Path, **changes: object) -> Journal:
    value = copy.deepcopy(DEFAULT_STATE)
    for key, child in changes.items():
        if isinstance(child, dict) and isinstance(value.get(key), dict):
            value[key].update(child)
        else:
            value[key] = child
    journal = Journal(path)
    journal.write(value)
    return journal


def _write_data(
    path: Path,
    acknowledged: list[str],
    *,
    extra: dict | None = None,
    include_reference_item_ids: bool = True,
) -> None:
    # Match the companion's current CROWLEY_DATA payload and pinned item catalog.
    catalog = load_catalog()
    plan = catalog.scan_plan()
    value: dict = {
        "generatedAt": 1_760_000_000,
        "ackedScans": sorted(acknowledged, key=lambda item: item.encode()),
        "scanPlan": plan,
        "referenceItemIDs": sorted(item.item_id for item in catalog.items),
        "shoppingLists": [],
    }
    if not include_reference_item_ids:
        del value["referenceItemIDs"]
    if extra:
        value.update(extra)
    path.write_text(render_assignment("CROWLEY_DATA", value, "strict captured Data.lua"), encoding="utf-8")


def _write_companion(path: Path, uid: str) -> None:
    path.write_text(json.dumps({"uploaded": {uid: 1_760_000_001}, "probes": []}), encoding="utf-8")


def _write_complete_saved_scan(path: Path, uid: str, started_at: int) -> None:
    """Write one complete production format-2 scan for daemon ACK recovery."""

    envelope = _strict_production_envelope(started_at)
    envelope["id"] = uid
    validate_v2_envelope(envelope)
    decoded = decode_json_payload(envelope["payload"])
    plan = load_catalog().scan_plan()
    first_item = str(plan["itemIDs"][0])
    owned_rows = copy.deepcopy(_production_item_evidence()[first_item]["ownedRows"])
    for row in owned_rows:
        row["itemLink"] = f"item:{first_item}:0:0:0:0:0:0:0:0:0:0:0:0"
    decoded["owned"] = {
        "status": "full",
        "query": {"kind": "queried", "startedAt": started_at, "finishedAt": started_at + 2},
        "reasonCode": "ok",
        "rowCount": len(owned_rows),
        "rows": {str(index): copy.deepcopy(row) for index, row in enumerate(owned_rows, 1)},
    }
    envelope["payload"] = encode_json_payload(decoded)
    path.write_text(
        render_assignment(
            "CrowleyDB",
            {"scans": {uid: envelope}, "sweeps": {}},
            "complete format-2 ACK recovery fixture",
        ),
        encoding="utf-8",
    )


def _config(
    tmp_path: Path,
    *,
    data: Path | None = None,
    companion: Path | None = None,
    saved_variables: Path | None = None,
) -> SessionConfig:
    return SessionConfig(
        profile=DriverProfile(
            house="beta-normal-alliance",
            character="Arthur",
            realm="Classic Beta PvE 2",
            plan_count=428,
            canvas_crop=(80, 48, 256, 256),
            model_crop=(80, 48, 1760, 984),
        ),
        journal=tmp_path / "session.json",
        socket=tmp_path / "session.sock",
        capture_dir=tmp_path / "captures",
        saved_variables=saved_variables or tmp_path / "Crowley.lua",
        companion_state=companion,
        data_lua=data,
        plan_hash=load_catalog().scan_plan()["planHash"],
        plan_json=load_catalog().scan_plan()["planJSON"],
        client_build="70450",
    )


def test_session_config_accepts_canonical_short_build_and_rejects_previous_build(tmp_path: Path) -> None:
    plan = load_catalog().scan_plan()
    fixture_target = replace(
        packaged_game_target(),
        game_root=str(tmp_path / "game"),
        wine_prefix=str(tmp_path / "wine"),
    )
    wow_root = Path(fixture_target.game_root) / fixture_target.product_dir
    saved = wow_root / "WTF/Account/Test/SavedVariables/Crowley.lua"
    companion = tmp_path / "companion-state.json"
    data = wow_root / "Interface/AddOns/Crowley_Data/Data.lua"
    saved.parent.mkdir(parents=True)
    data.parent.mkdir(parents=True)
    for path in (saved, companion, data):
        path.write_text("", encoding="utf-8")
    socket_dir = tmp_path / "session-runtime"
    socket_dir.mkdir(mode=0o700)
    socket_dir.chmod(0o700)
    socket = socket_dir / "session.sock"

    def write_config(path: Path, client_build: str) -> None:
        path.write_text(
            "\n".join(
                (
                    "[profile]",
                    f'wine_prefix = "{fixture_target.wine_prefix}"',
                    f'window_class = "{fixture_target.window_class}"',
                    f'executable = "{fixture_target.executable}"',
                    f'realm = "{fixture_target.realm}"',
                    f'house = "{fixture_target.house}"',
                    "canvas_crop = [80, 48, 256, 256]",
                    "model_crop = [80, 48, 1760, 984]",
                    f'client_build = "{client_build}"',
                    f'plan_hash = "{plan["planHash"]}"',
                    f"plan_json = {json.dumps(plan['planJSON'])}",
                    "",
                    "[session]",
                    f'socket = "{socket}"',
                    f'saved_variables = "{saved}"',
                    f'companion_state = "{companion}"',
                    f'data_lua = "{data}"',
                )
            ),
            encoding="utf-8",
        )

    accepted_path = tmp_path / "accepted.toml"
    write_config(accepted_path, "70450")
    accepted = load_config(accepted_path, expected_target=fixture_target)
    assert accepted.client_build == "70450"
    assert accepted.client_version == "1.60.1"

    for rejected_build in ("70425", "70374", "99999"):
        rejected_path = tmp_path / f"rejected-{rejected_build}.toml"
        write_config(rejected_path, rejected_build)
        with pytest.raises(ValueError, match="70450/interface 16001"):
            load_config(rejected_path, expected_target=fixture_target)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("wine_prefix", "/tmp/stale-wine-prefix"),
        ("window_class", "stale-window-class"),
        ("executable", "Stale.exe"),
        ("realm", "Stale realm"),
        ("house", "us-normal-horde"),
    ],
)
def test_session_config_rejects_stale_packaged_identity(tmp_path: Path, field: str, replacement: str) -> None:
    target = replace(
        packaged_game_target(),
        game_root=str(tmp_path / "game"),
        wine_prefix=str(tmp_path / "wine"),
    )
    wow_root = Path(target.game_root) / target.product_dir
    saved = wow_root / "WTF/Account/Test/SavedVariables/Crowley.lua"
    data = wow_root / "Interface/AddOns/Crowley_Data/Data.lua"
    companion = tmp_path / "companion-state.json"
    saved.parent.mkdir(parents=True)
    data.parent.mkdir(parents=True)
    for path in (saved, data, companion):
        path.write_text("", encoding="utf-8")
    socket_dir = tmp_path / "session-runtime"
    socket_dir.mkdir(mode=0o700)
    socket_dir.chmod(0o700)
    socket = socket_dir / "session.sock"
    plan = target_scan_plan(load_catalog(), target)
    values = {
        "wine_prefix": target.wine_prefix,
        "window_class": target.window_class,
        "executable": target.executable,
        "realm": target.realm,
        "house": target.house,
    }
    values[field] = replacement
    config = tmp_path / f"stale-{field}.toml"
    config.write_text(
        "\n".join(
            (
                "[profile]",
                *(f'{name} = "{value}"' for name, value in values.items()),
                'client_build = "70450"',
                f"plan_count = {len(plan['itemIDs'])}",
                f'plan_hash = "{plan["planHash"]}"',
                f"plan_json = {json.dumps(plan['planJSON'])}",
                "",
                "[session]",
                f'socket = "{socket}"',
                f'saved_variables = "{saved}"',
                f'companion_state = "{companion}"',
                f'data_lua = "{data}"',
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="packaged launch target"):
        load_config(config, expected_target=target)


def _retained_input_evidence(tmp_path: Path) -> InputCalibrationEvidence:
    # Reuse the complete captured-format fixture owned by the native
    # regressions.  Retained evidence must satisfy the same sealed keyboard,
    # modifier, echo, frame, and ancestry contract as freshly calibrated data.
    from .test_session_native_regressions import _evidence

    return _evidence(tmp_path)


def _native_proof_evidence(
    tmp_path: Path, *, process_start: str, capture_name: str
) -> NativeCalibrationEvidence:
    """Build complete sealed native evidence for an archive or a fresh gate."""

    capture = tmp_path / capture_name
    capture.write_bytes(b"archived native capture")
    native = NativeCalibrationEvidence._issue(
        fingerprint="old-native-fingerprint",
        process_pid=os.getpid(),
        process_executable="/usr/bin/python3",
        process_start=process_start,
        window_xid=0x600100,
        child_xid=0x600101,
        geometry=(0, 0, 1920, 1080),
        xinput_hierarchy=((4, 2, "slave", "xtest pointer"), (5, 3, "slave", "xtest keyboard")),
        crop=(80, 48, 256, 256),
        boot="old-boot",
        sequence=101,
        crc32=0xABCD,
        render={
            "constructionSeconds": 0.1,
            "updateCount": 102,
            "updateP99Ms": 1,
            "textureCount": 16_384,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": True,
                "beforeKiB": 1,
                "afterKiB": 2,
                "netKiB": 1,
            },
            "nativePhase": "held",
            "nativeOrdinal": 100,
        },
        construction_update_count=1,
        held_update_count=2,
        resume_update_count=103,
        memory_measurements=(
            _memory_measurement(
                phase="construction",
                ordinal=0,
                process_start=process_start,
                limit_bytes=128 * 1024 * 1024,
            ),
            _memory_measurement(
                phase="updates",
                ordinal=0,
                process_start=process_start,
                limit_bytes=16 * 1024 * 1024,
            ),
        ),
        native_identity_fingerprint="old-native-identity",
        resume_sequence=102,
        resume_crc32=0xABCE,
        resume_render={
            "constructionSeconds": 0.1,
            "updateCount": 103,
            "updateP99Ms": 1,
            "textureCount": 16_384,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": True,
                "beforeKiB": 1,
                "afterKiB": 2,
                "netKiB": 1,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        },
        resume_capture_monotonic=2.0,
        capture_path=str(capture),
        capture_sha256=hashlib.sha256(capture.read_bytes()).hexdigest(),
        capture_monotonic=1.0,
        xauthority=str(tmp_path / "Xauthority"),
        wine_prefix=str(tmp_path / "wine"),
        preconstruction_fingerprint="old-preconstruction",
    )
    native.verify_seal()
    return native


def _memory_measurement(*, phase: str, ordinal: int, process_start: str, limit_bytes: int):
    from crowley.session.calibration import NativeMemoryMeasurement

    reset = 1.0 if phase == "construction" else 2.0
    return NativeMemoryMeasurement(
        method="linux_proc_status_vmhwm_reset5",
        phase=phase,
        ordinal=ordinal,
        pid=os.getpid(),
        process_start=process_start,
        process_executable="/usr/bin/python3",
        identity_fingerprint="old-native-identity",
        uid=os.getuid(),
        kernel_release="test-kernel",
        historical_hwm_bytes=100,
        rss_before_reset_bytes=100,
        reset_completed_monotonic=reset,
        post_reset_hwm_bytes=100,
        rss_after_reset_bytes=100,
        workload_start_monotonic=reset + 0.1,
        workload_end_monotonic=reset + 0.2,
        workload_start_ordinal=0 if phase == "construction" else 1,
        workload_end_ordinal=0 if phase == "construction" else 100,
        final_hwm_bytes=100,
        final_rss_bytes=100,
        growth_bytes=0,
        limit_bytes=limit_bytes,
    )


def _archived_native_proof(tmp_path: Path) -> dict[str, object]:
    """Build one complete old native record for a restart archive."""

    return Controller._evidence_record(
        _native_proof_evidence(
            tmp_path,
            process_start="old-process-start",
            capture_name="old-native.capture",
        )
    )


def _qualified_sweep(uid: str, started_at: int) -> SweepEvidence:
    payload = validate_v2_envelope(_strict_production_envelope(started_at))["decoded"]
    plan = load_catalog().scan_plan()
    item_evidence = _production_item_evidence()
    return SweepEvidence(
        uid=uid,
        house="beta-normal-alliance",
        plan_hash=plan["planHash"],
        observations=428,
        attempted=428,
        full=428,
        bounded=0,
        incomplete=0,
        unqueried=0,
        priced=428,
        row_count=428,
        kinds=("commodity", "item"),
        query_durations=(1,),
        envelope={"startedAt": started_at, "finishedAt": started_at + 2},
        payload=payload,
        stable_verified=True,
        item_evidence=item_evidence,
        owned_rows=item_evidence[str(plan["itemIDs"][0])]["ownedRows"],
        owned_priced_row_count=1,
    )


def _production_item_evidence(*, bounded_commodity: bool = False) -> dict[str, dict]:
    """Build complete admission facts for the configured 428-item plan."""

    catalog = load_catalog()
    active = [item for item in catalog.items if item.status == "active"]
    assert len(active) == 428
    first_id = str(active[0].item_id)
    categories = {
        "recipes": "recipe",
        "containers": "container",
        "equipment": "equipment",
    }
    result: dict[str, dict] = {}
    for item in active:
        item_id = str(item.item_id)
        owned_rows = (
            [
                {
                    "auctionID": 1,
                    "itemID": item.item_id,
                    "itemKey": item_id,
                    "rawItemKey": {
                        "itemID": item.item_id,
                        "itemLevel": 0,
                        "itemSuffix": 0,
                        "battlePetSpeciesID": 0,
                        "bonusIDs": [],
                    },
                    "itemLink": {"kind": "unknown"},
                    "quantity": 1,
                    "buyout": {"kind": "known", "copper": 1},
                    "status": "active",
                }
            ]
            if item_id == first_id
            else []
        )
        category = categories.get(item.category, "commodity")
        bounded = bounded_commodity and item_id == first_id
        result[item_id] = {
            "coverage": "bounded" if bounded else "full",
            "sourceComplete": not bounded,
            "priced": True,
            "rowCount": 20 if bounded else 1,
            "pricedRowCount": 20 if bounded else 1,
            "ownedRows": owned_rows,
            "ownedPricedRowCount": len(owned_rows),
            "category": category,
            "query": "queried",
        }
    return result


def _strict_production_envelope(started_at: int, *, bounded_commodity: bool = False) -> dict:
    """Return a complete validated envelope for the pinned 428-item plan."""

    plan = load_catalog().scan_plan()
    active = {item.item_id: item for item in load_catalog().items if item.status == "active"}
    categories = {"recipes": "recipe", "containers": "container", "equipment": "equipment"}
    bounded_id = next(
        item_id
        for item_id in plan["itemIDs"]
        if categories.get(active[item_id].category, "commodity") == "commodity"
    )
    observations: dict[str, dict] = {}
    for ordinal, item_id in enumerate(plan["itemIDs"], 1):
        kind = categories.get(active[item_id].category, "commodity")
        bounded = bounded_commodity and item_id == bounded_id
        price = 200 if bounded else 100 + ordinal
        raw = {
            "itemID": item_id,
            "itemLevel": 0,
            "itemSuffix": 0,
            "battlePetSpeciesID": 0,
            "bonusIDs": [],
        }
        row_values = range(price - 19, price + 1) if bounded else (price,)
        rows = {}
        for row_index, row_price in enumerate(row_values, 1):
            rows[f"row:{row_index}"] = {
                "resultID": ordinal * 100 + row_index,
                "itemID": item_id,
                "itemKey": str(item_id),
                "rawItemKey": raw,
                "link": {"kind": "unknown"}
                if kind == "commodity"
                else {"kind": "known", "value": f"item:{item_id}:0:0:0:0:0:0:0:0:0:0:0:0"},
                "quantity": 1,
                "price": {
                    "kind": "unit" if kind == "commodity" else "auction",
                    "copper": row_price,
                },
                "unitPrice": {"numerator": row_price, "denominator": 1},
                "owners": {
                    "names": [],
                    "totalNumberOfOwners": 0,
                    "containsOwnerItem": False,
                    "containsAccountItem": False,
                    "identityKind": "grouped",
                },
                "timeLeft": {"kind": "unknown"},
            }
        observations[str(item_id)] = {
            "itemID": item_id,
            "ordinal": ordinal,
            "kind": "commodity" if kind == "commodity" else "item",
            "query": {"kind": "queried", "startedAt": started_at, "finishedAt": started_at + 2},
            "coverage": "bounded" if bounded else "full",
            "sourceComplete": not bounded,
            "cutoff": {"kind": "price", "numerator": price, "denominator": 1}
            if bounded
            else {"kind": "unbounded"},
            "higherPriceWitness": {"kind": "known", "numerator": price + 1, "denominator": 1}
            if bounded
            else {"kind": "unknown"},
            "reasonCode": "ok",
            "rowCount": len(rows),
            "rows": rows,
        }
    plan_payload = {
        "planHash": plan["planHash"],
        "planJSON": plan["planJSON"],
        "observations": observations,
        "owned": {
            "status": "unqueried",
            "query": {"kind": "unqueried"},
            "reasonCode": "not_started",
            "rowCount": 0,
            "rows": [],
        },
    }
    return {
        "id": f"production-{started_at}",
        "format": 2,
        "startedAt": started_at,
        "finishedAt": started_at + 2,
        "house": {
            "region": "beta",
            "ruleset": "normal",
            "faction": "alliance",
            "source": "auto",
            "key": "beta-normal-alliance",
        },
        "context": {
            "character": "Arthur",
            "realm": "Classic Beta PvE 2",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
            "clientBuild": "70450",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "numRows": 447 if bounded_commodity else 428,
        "numObservations": 428,
        "payload": encode_json_payload(plan_payload),
    }


def _prepare_qualification(
    controller: Controller, evidence: SweepEvidence, *, started_at: int, index: int = 0
) -> None:
    """Drive one complete durable action cycle until qualification is admitted."""

    controller.begin_scan(started_at=started_at)
    controller.saved(evidence.uid, stable=True)
    controller.reload_sent()
    controller.expect_boot("boot-1")
    controller.observe_frame(boot="boot-2", sequence=index + 1, crc32=index + 1)
    controller.stable_saved(evidence)
    controller.uploaded(
        evidence.uid,
        accepted=True,
        upload_time=started_at + 10,
        data_ack_uid=evidence.uid,
    )
    controller.record_ack_publication(
        evidence.uid,
        companion_uid=evidence.uid,
        data_uid=evidence.uid,
        publication_time=started_at + 11,
        boot="boot-2",
        sequence=index + 1,
    )
    controller.ack_reload_command_sent()
    controller.expect_boot("boot-2", acknowledgement=True)
    controller.observe_frame(boot="boot-3", sequence=index + 2, crc32=index + 2)
    controller.ack_reload_complete(
        pending_ids=set(),
        loaded_uid=evidence.uid,
        loaded_at=started_at + 12,
        boot="boot-3",
        sequence=index + 2,
    )


def _strict_bounded_commodity() -> dict:
    """Return a real format-2 bounded commodity envelope with 20 levels."""

    plan_body = {
        "schemaVersion": 2,
        "id": "first-run",
        "revision": 1,
        "catalogHash": "a" * 64,
        "targetClientBuild": "1.60.1.70425",
        "cadenceSeconds": 900,
        "initialPriceLevels": 20,
        "itemIDs": [10],
        "pools": {"core": [10], "discovery": []},
    }
    plan_json = json.dumps(plan_body, sort_keys=True, separators=(",", ":"))
    import hashlib

    observation = {
        "itemID": 10,
        "ordinal": 1,
        "kind": "commodity",
        "query": {"kind": "queried", "startedAt": 1_759_999_990, "finishedAt": 1_760_000_000},
        "coverage": "bounded",
        "sourceComplete": False,
        "cutoff": {"kind": "price", "numerator": 20, "denominator": 1},
        "higherPriceWitness": {"kind": "known", "numerator": 21, "denominator": 1},
        "reasonCode": "ok",
        "rowCount": 20,
        "rows": {},
    }
    for index in range(1, 21):
        observation["rows"][f"row:{index}"] = {
            "resultID": index,
            "itemID": 10,
            "itemKey": "10",
            "rawItemKey": {
                "itemID": 10,
                "itemLevel": 0,
                "itemSuffix": 0,
                "battlePetSpeciesID": 0,
                "bonusIDs": [],
            },
            "link": {"kind": "unknown"},
            "quantity": 1,
            "price": {"kind": "unit", "copper": index},
            "unitPrice": {"numerator": index, "denominator": 1},
            "owners": {
                "names": [],
                "totalNumberOfOwners": 0,
                "containsOwnerItem": False,
                "containsAccountItem": False,
                "identityKind": "grouped",
            },
            "timeLeft": {"kind": "unknown"},
        }
    payload = {
        "planHash": hashlib.sha256(plan_json.encode()).hexdigest(),
        "planJSON": plan_json,
        "observations": {"10": observation},
        "owned": {
            "status": "unqueried",
            "query": {"kind": "unqueried"},
            "reasonCode": "not_started",
            "rowCount": 0,
            "rows": [],
        },
    }
    return {
        "id": "bounded-commodity",
        "format": 2,
        "startedAt": 1_759_999_990,
        "finishedAt": 1_760_000_000,
        "numRows": 20,
        "numObservations": 1,
        "house": {
            "region": "us",
            "ruleset": "normal",
            "faction": "alliance",
            "source": "auto",
            "key": "us-normal-alliance",
        },
        "context": {
            "character": "Tester",
            "realm": "Normal",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
            "clientBuild": "70425",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "payload": encode_json_payload(payload),
    }


def test_actual_data_schema_matches_current_producer_and_rejects_bad_reference_ids(tmp_path: Path) -> None:
    data = tmp_path / "Data.lua"
    _write_data(data, ["uid-1"])
    assert read_data_acknowledged_ids(data) == ("uid-1",)

    reference_item_ids = sorted(item.item_id for item in load_catalog().items)
    invalid_reference_ids = (
        reference_item_ids[:-1],
        reference_item_ids[::-1],
        [*reference_item_ids[:-1], reference_item_ids[-1], reference_item_ids[-1]],
        [True, *reference_item_ids[1:]],
        [*reference_item_ids[:-1], 999_999_999],
    )
    for invalid_ids in invalid_reference_ids:
        _write_data(data, ["uid-1"], extra={"referenceItemIDs": invalid_ids})
        with pytest.raises(EvidenceError, match="CROWLEY_DATA.referenceItemIDs is invalid"):
            read_data_acknowledged_ids(data)

    _write_data(data, ["uid-1"], include_reference_item_ids=False)
    with pytest.raises(EvidenceError, match="CROWLEY_DATA is malformed"):
        read_data_acknowledged_ids(data)

    _write_data(data, ["uid-1"], extra={"ackedSweeps": []})
    with pytest.raises(EvidenceError, match="CROWLEY_DATA is malformed"):
        read_data_acknowledged_ids(data)


def test_strict_format2_bounded_commodity_allows_source_incomplete() -> None:
    envelope = _strict_bounded_commodity()
    parsed = validate_v2_envelope(envelope)
    observation = parsed["decoded"]["observations"]["10"]
    assert observation["coverage"] == "bounded"
    assert observation["sourceComplete"] is False
    assert len(observation["rows"]) == 20

    observation["rows"]["row:20"]["unitPrice"] = {"numerator": 19, "denominator": 1}
    envelope["payload"] = encode_json_payload(parsed["decoded"])
    with pytest.raises(ScanFormatError):
        validate_v2_envelope(envelope)


def test_qualifier_accepts_only_strict_bounded_commodity_source_incomplete(tmp_path: Path) -> None:
    # Preflight the same bounded wire fixture before passing its evidence into
    # the session qualification boundary.
    parsed = validate_v2_envelope(_strict_bounded_commodity())["decoded"]
    assert parsed["observations"]["10"]["sourceComplete"] is False
    parsed = validate_v2_envelope(_strict_production_envelope(1_000, bounded_commodity=True))["decoded"]
    plan = load_catalog().scan_plan()
    item_evidence = _production_item_evidence(bounded_commodity=True)
    commodity_id = next(item_id for item_id, item in item_evidence.items() if item["category"] == "commodity")
    assert item_evidence[commodity_id]["sourceComplete"] is False
    evidence = SweepEvidence(
        uid="bounded-uid",
        house="beta-normal-alliance",
        plan_hash=plan["planHash"],
        observations=428,
        attempted=428,
        full=427,
        bounded=1,
        incomplete=0,
        unqueried=0,
        priced=447,
        row_count=447,
        kinds=("commodity", "item"),
        query_durations=(1,),
        envelope={"startedAt": 1_000, "finishedAt": 1_002},
        payload=parsed,
        stable_verified=True,
        item_evidence=item_evidence,
        owned_rows=item_evidence[next(iter(item_evidence))]["ownedRows"],
        owned_priced_row_count=1,
    )
    journal = _state_file(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    controller = Controller(journal, driver=_FakeDriver(journal), recover_on_restart=False)
    _prepare_qualification(controller, evidence, started_at=1_000)
    assert controller.qualify(evidence, started_at=1_000).streak == 1


def test_ack_reload_waits_for_data_publication_and_retains_loaded_ack_after_refresh(tmp_path: Path) -> None:
    uid = "uid-publication"
    data = tmp_path / "Data.lua"
    companion = tmp_path / "companion.json"
    _write_data(data, [])
    _write_companion(companion, uid)
    journal = _state_file(
        tmp_path / "session.json",
        state=ControllerState.ACK_RELOAD_SENT.value,
        current={
            "uid": uid,
            "pending": True,
            "action": "ack_reload",
            "actionResult": "dispatched",
            "deadlineAt": 2_000,
            "evidence": {
                "uid": uid,
                "finishedAt": 990,
                "stableSavedUid": uid,
                "uploadTimestamp": 995,
            },
        },
    )
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(
        _config(tmp_path, data=data, companion=companion), controller=controller, driver=driver
    )
    commands: list[str] = []
    daemon._command = lambda _snapshot, command: commands.append(command) or True  # type: ignore[method-assign]
    frame = _snapshot(phase="ready", loaded=[], ah_open=True)

    daemon._advance(frame)
    assert commands == [], "ACK reload must not be dispatched before Data.lua publishes the UID"

    _write_data(data, [uid])

    def inspect_reload(_snapshot: dict, command: str) -> bool:
        current = journal.read()["current"]["evidence"]
        assert current["atomicCompanionUploadedUid"] == uid
        assert current["dataAckUid"] == uid
        commands.append(command)
        return True

    daemon._command = inspect_reload  # type: ignore[method-assign]
    daemon._advance(frame)
    assert commands == ["/reload"]
    # The dispatch boundary remains ``ack_reload`` until a fresh frame proves
    # the new boot; only then does the controller enter the wait intent.
    assert controller.status().action_intent == "ack_reload"

    journal = _state_file(
        tmp_path / "loaded-session.json",
        state=ControllerState.ACK_RELOAD_WAIT.value,
        current={
            "uid": uid,
            "pending": True,
            "startedAt": 990,
            "action": "ack_reload_wait",
            "actionResult": "dispatched",
            "deadlineAt": 2_000,
            "evidence": {
                "uid": uid,
                "finishedAt": 990,
                "stableSavedUid": uid,
                "uploadTimestamp": 995,
                "atomicCompanionUploadedUid": uid,
                "dataAckUid": uid,
                "ackPublicationAt": 996,
            },
        },
    )
    _write_complete_saved_scan(tmp_path / "Crowley.lua", uid, 990)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(
        _config(tmp_path, data=data, companion=companion), controller=controller, driver=driver
    )
    loaded = _snapshot(phase="ready", loaded=[uid], pending=[], ah_open=True)
    daemon._advance(loaded)
    assert controller.status().state == ControllerState.REOPEN

    # A later companion/Data refresh may drop the publication, but cannot undo
    # the loaded acknowledgement already proven by the fresh Agent frame.
    _write_data(data, [])
    daemon._advance(loaded)
    assert controller.status().state == ControllerState.WAIT_DUE


def test_session_ipc_resume_uses_matching_passive_drill_then_reconciles(tmp_path: Path) -> None:
    from crowley.session.cli import SessionIPC
    from crowley.session.startup import TakeoverDrillRecord
    from tests.test_session_startup import _runtime_evidence

    _identity, environment, evidence = _runtime_evidence()
    journal = _state_file(
        tmp_path / "session.json",
        state=ControllerState.PAUSED.value,
        resumeState=ControllerState.WAIT_DUE.value,
        nativeCalibration=True,
        inputCalibration=True,
    )
    config = _config(tmp_path)
    config = replace(config, profile=replace(config.profile, display=environment.display, xauthority=None))
    driver = DesktopDriver(
        profile=config.profile,
        backend=RecordingBackend(),
        lease_path=tmp_path / "desktop.lock",
    )
    # This regression isolates the public IPC/controller routing.  Other
    # startup tests exercise the real read-only native measurement; this fake
    # arm delegates only the resulting sealed capability to the real Guardian.
    driver.arm = lambda runtime_evidence: driver.guardian.arm(runtime_evidence)  # type: ignore[method-assign]
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(config, controller=controller, driver=driver)
    try:
        paused = controller.pause("fixture takeover drill setup")
        assert paused.state == ControllerState.PAUSED
        controller.persist_takeover_drill(
            TakeoverDrillRecord.create(environment.fingerprint, 18, observed_at=1_800_000_000)
        )

        def passive_startup():
            daemon._startup_evidence = evidence
            daemon._startup_operation = {"state": "ready", "advancingFrames": 3}
            return evidence

        daemon.passive_startup = passive_startup
        response = SessionIPC(controller, daemon).dispatch(
            {"command": "resume", "generation": paused.generation}
        )
        assert response["ok"] is True, response
        assert response["status"]["state"] == ControllerState.RECONCILE.value
        assert response["status"]["controllerState"] == ControllerState.RECONCILE.value
        assert driver.guardian.armed
        resume_generation = response["status"]["generation"]
        reconciled = controller.reconcile(
            identity_ok=True,
            expected_generation=resume_generation,
            expected_state=ControllerState.RECONCILE,
        )
        assert reconciled.state == ControllerState.WAIT_DUE
        assert reconciled.lease_generation == driver.guardian.generation
        assert not reconciled.action_intent
        assert controller.status().state == ControllerState.WAIT_DUE
        assert controller.status().generation == resume_generation
    finally:
        driver.close()


def test_cadence_is_relative_to_global_seed_not_adjacent_interval_drift(tmp_path: Path) -> None:
    def qualify_sequence(starts: tuple[int, ...]) -> tuple[list[ControllerState], dict]:
        journal = _state_file(
            tmp_path / f"cadence-{starts[0]}-{starts[-1]}.json", state=ControllerState.WAIT_DUE.value
        )
        driver = _FakeDriver(journal)
        controller = Controller(journal, driver=driver, recover_on_restart=False)
        states: list[ControllerState] = []
        for index, started_at in enumerate(starts):
            uid = f"uid-{starts[0]}-{index}"
            evidence = _qualified_sweep(uid, started_at)
            # Drive the real scan -> save -> upload -> acknowledgement reload
            # boundary before calling the qualification admission.  The old
            # direct SCANNING -> qualify shortcut bypassed the durable state
            # machine and concealed cadence failures behind a RuntimeError.
            _prepare_qualification(controller, evidence, started_at=started_at, index=index)
            status = controller.qualify(evidence, started_at=started_at)
            states.append(status.state)
            if status.state == ControllerState.REOPEN and index + 1 < len(starts):
                controller.reopen_complete()
        return states, journal.read()

    steady, _steady_state = qualify_sequence((100, 1_000, 1_900))
    assert steady == [
        ControllerState.REOPEN,
        ControllerState.REOPEN,
        ControllerState.REOPEN,
    ]

    drifted, drift_state = qualify_sequence((100, 1_005, 1_910))
    assert drifted[:2] == [ControllerState.REOPEN, ControllerState.REOPEN]
    assert drifted[2] == ControllerState.REOPEN
    assert drift_state["blocker"] == "cadence interval failed"
    assert drift_state["qualification"]["streak"] == 0


def test_ipc_generation_check_and_mutation_are_one_atomic_transition(tmp_path: Path) -> None:
    from crowley.session.cli import SessionIPC

    journal = Journal(tmp_path / "session.json")
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    raced = False
    original_status = controller.status

    def blocked_status():
        nonlocal raced
        status = original_status()
        if not raced:
            raced = True
            controller.pause("operator takeover")
        return status

    controller.status = blocked_status  # type: ignore[method-assign]
    response = SessionIPC(controller).dispatch({"command": "start", "generation": 0})
    assert response == {"ok": False, "error": "stale generation"}
    assert controller.status().generation == 1
    assert controller.status().state == ControllerState.PAUSED


def test_telemetry_write_cannot_overwrite_durable_pause(tmp_path: Path) -> None:
    class RacedJournal(Journal):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.telemetry_entered = threading.Event()
            self.release_telemetry = threading.Event()

        def update(self, change):  # type: ignore[no-untyped-def]
            value = self.read()
            if "telemetry" in value and not self.telemetry_entered.is_set():
                self.telemetry_entered.set()
                assert self.release_telemetry.wait(2)
            change(value)
            self.write(value)
            return copy.deepcopy(value)

    journal = RacedJournal(tmp_path / "session.json")
    journal.write({**copy.deepcopy(DEFAULT_STATE), "state": ControllerState.WAIT_DUE.value})
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)
    daemon.capture_status = lambda: _sample(_snapshot(phase="item"))  # type: ignore[method-assign]
    worker_errors: list[BaseException] = []

    def run_once() -> None:
        try:
            daemon.run_once()
        except BaseException as exc:  # asserted below; do not hide worker failures
            worker_errors.append(exc)

    pause_started = threading.Event()
    pause_done = threading.Event()
    pause_errors: list[BaseException] = []

    def pause() -> None:
        pause_started.set()
        try:
            controller.pause("operator takeover")
        except BaseException as exc:  # asserted below; do not hide pause failures
            pause_errors.append(exc)
        finally:
            pause_done.set()

    worker = threading.Thread(target=run_once)
    worker.start()
    assert journal.telemetry_entered.wait(2)
    pauser = threading.Thread(target=pause)
    pauser.start()
    assert pause_started.wait(2)
    journal.release_telemetry.set()
    worker.join(timeout=2)
    pauser.join(timeout=2)
    assert not worker.is_alive()
    assert not pauser.is_alive()
    assert pause_done.is_set()
    assert worker_errors == []
    assert pause_errors == []
    assert journal.read()["state"] == ControllerState.PAUSED.value
    assert journal.read()["blocker"] == "operator takeover"


def test_second_daemon_cannot_mutate_state_before_process_lease(tmp_path: Path) -> None:
    lease = tmp_path / "desktop.lock"
    journal = Journal(tmp_path / "session.json")
    journal.write(copy.deepcopy(DEFAULT_STATE))
    first = DesktopDriver(profile=DriverProfile(), backend=RecordingBackend(), lease_path=lease)
    try:
        with pytest.raises(GuardianError, match="another process"):
            DesktopDriver(profile=DriverProfile(), backend=RecordingBackend(), lease_path=lease)
        assert journal.read()["state"] == ControllerState.STOPPED.value
        assert journal.read()["generation"] == 0
    finally:
        first.close()


def test_crc_and_boot_churn_are_rejected_by_capture_decoder() -> None:
    first = _snapshot(sequence=1, boot="boot-1")
    second = _snapshot(sequence=1, boot="boot-2")
    decoder = PixelDecoder()
    decoder.decode(_image_for_frame(first), monotonic=1, window_identity="window")
    with pytest.raises(ProtocolError, match="boot|stale"):
        decoder.decode(_image_for_frame(second), monotonic=2, window_identity="window")

    changed_payload = _snapshot(sequence=1, boot="boot-1")
    changed_payload["context"]["zone"] = "Darnassus"
    decoder = PixelDecoder()
    decoder.decode(_image_for_frame(first), monotonic=1, window_identity="window")
    with pytest.raises(ProtocolError, match="CRC"):
        decoder.decode(_image_for_frame(changed_payload), monotonic=2, window_identity="window")


def test_capture_sample_past_deadline_is_blocked_before_scan_input(tmp_path: Path) -> None:
    journal = _state_file(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)
    daemon.capture_status = lambda: _sample(_snapshot(phase="item"), monotonic=-100)  # type: ignore[method-assign]
    result = daemon.run_once()
    assert result["state"] == ControllerState.BLOCKED.value
    assert "stale" in result["blocker"] or "deadline" in result["blocker"]


def test_corrupt_telemetry_blocks_without_entering_model_recovery(tmp_path: Path) -> None:
    journal = _state_file(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    config = _config(tmp_path)
    config = replace(
        config,
        profile=DriverProfile(
            house="beta-normal-alliance",
            character="Arthur",
            realm="Classic Beta PvE 2",
            plan_count=428,
            canvas_crop=(80, 48, 256, 256),
            model_crop=(80, 48, 1760, 984),
        ),
    )
    daemon = SessionDaemon(config, controller=controller, driver=driver)
    daemon.capture_status = lambda: (_ for _ in ()).throw(ProtocolError("CRC mismatch"))  # type: ignore[method-assign]
    daemon._recover_login = lambda: pytest.fail("corrupt telemetry must never invoke model recovery")  # type: ignore[method-assign]

    result = daemon.run_once()
    assert result["state"] == ControllerState.BLOCKED.value
    assert "CRC mismatch" in result["blocker"]
    assert result["runtimeReadinessInvalidation"] == "telemetry_invalid"


def test_recovery_exception_is_durable_blocked_state(tmp_path: Path) -> None:
    journal = _state_file(tmp_path / "session.json", state=ControllerState.LOGIN_REQUIRED.value)
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)
    daemon.capture_status = lambda: (_ for _ in ()).throw(ValueError("capture failed"))  # type: ignore[method-assign]
    daemon._capture_model = lambda: b"verified crop"  # type: ignore[method-assign]
    daemon.model.decide = lambda *_args, **_kwargs: (_ for _ in ()).throw(ModelError("model unavailable"))  # type: ignore[method-assign]

    result = daemon.run_once()
    assert result["state"] == ControllerState.BLOCKED.value
    assert "model unavailable" in result["blocker"]


def test_action_intent_is_durable_before_fake_backend_effect(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "session.json")
    journal.write({**copy.deepcopy(DEFAULT_STATE), "state": ControllerState.WAIT_DUE.value})
    driver = _FakeDriver(journal)
    # This regression reaches the admitted external boundary; production
    # dispatch still requires the explicit armed bit.
    driver.guardian.armed = True
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)

    def inspect_before_effect(name: str, generation: int, **kwargs: object) -> None:
        assert journal.read()["current"]["action"] == name
        driver.events.append({"name": name, "generation": generation})

    driver.run = inspect_before_effect  # type: ignore[method-assign]
    daemon._run_action("open_ah", _snapshot(phase="ready", ah_open=False))
    assert driver.events[0]["name"] == "open_ah"


def test_restart_pauses_uncertain_action_and_resume_is_explicit(tmp_path: Path) -> None:
    from crowley.session.cli import SessionIPC

    journal = _state_file(
        tmp_path / "session.json",
        state=ControllerState.SAVE_RELOAD_SENT.value,
        nativeCalibration=True,
        inputCalibration=True,
        current={
            "uid": "uid-uncertain",
            "pending": True,
            "action": "save_reload",
            "actionResult": "dispatched",
            "expectedBoot": "boot-2",
        },
    )
    prior_generation = int(journal.read()["generation"])
    driver = _FakeDriver(journal)
    restarted = Controller(journal, driver=driver)
    assert restarted.status().state == ControllerState.PAUSED
    assert restarted.status().action_intent == "save_reload"
    assert driver.events == []
    assert restarted.status().generation == prior_generation + 1
    assert journal.read()["generation"] == prior_generation + 1
    stale = SessionIPC(restarted).dispatch({"command": "resume", "generation": prior_generation})
    assert stale == {"ok": False, "error": "stale generation"}
    resumed = restarted.resume()
    assert resumed.state in {ControllerState.INPUT_CALIBRATION, ControllerState.RECONCILE}
    assert driver.events == []


def test_restart_reconciliation_keeps_unresolved_intent_until_complete_facts(tmp_path: Path) -> None:
    """Changed boot and partial files never clear an unknown reload effect."""

    uid = "uid-reconcile"
    journal = _state_file(
        tmp_path / "restart-reconcile.json",
        state=ControllerState.SAVE_RELOAD_SENT.value,
        nativeCalibration=True,
        inputCalibration=True,
        current={
            "uid": uid,
            "pending": True,
            "action": "save_reload",
            "actionResult": "dispatched",
            "expectedBoot": "boot-old",
            "frameBoot": "boot-old",
            "restartReferenceBoot": "boot-old",
        },
    )
    controller = Controller(journal, driver=_FakeDriver(journal))
    assert controller.status().state is ControllerState.PAUSED
    resumed = controller.resume()
    assert resumed.state is ControllerState.RECONCILE
    assert journal.read()["nativeCalibration"] is False
    assert journal.read()["inputCalibration"] is False

    unresolved = controller.reconcile_restart(
        observed_boot="boot-new",
        observed_uid=uid,
        observed_scene="world",
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={uid},
        stable_sweep_ids=set(),
        stable_format2_uid=uid,
        stable_format2_verified=True,
        stable_files_complete=False,
    )
    assert unresolved.state is ControllerState.RECONCILE
    unresolved_value = journal.read()
    assert unresolved_value["restartUncertain"] is True
    assert unresolved_value["current"]["action"] == "save_reload"
    assert unresolved_value["current"]["actionResult"] == "dispatched"
    assert unresolved_value["current"]["pending"] is True

    stable_evidence = _qualified_sweep(uid, 1_000)
    resolved = controller.reconcile_restart(
        observed_boot="boot-new",
        observed_uid=uid,
        observed_scene="world",
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids={uid},
        stable_sweep_ids=set(),
        stable_format2_uid=uid,
        stable_format2_evidence=stable_evidence,
        stable_format2_verified=True,
        stable_files_complete=True,
        stable_saved_uid=uid,
    )
    assert resolved.state is ControllerState.NATIVE_CALIBRATION
    assert resolved.action_intent == "upload_wait"
    assert journal.read()["restartUncertain"] is False
    assert journal.read()["current"]["pending"] is True


def test_fresh_driver_restart_does_not_reuse_archived_native_proof(tmp_path: Path) -> None:
    """An old native archive records history; a new driver still needs input calibration."""

    journal = _state_file(
        tmp_path / "fresh-restart.json",
        state=ControllerState.WAIT_DUE.value,
        nativeCalibration=True,
        inputCalibration=True,
    )
    old_record = _archived_native_proof(tmp_path)
    old_input = _retained_input_evidence(tmp_path)
    journal.append_archive("evidence", {"kind": "native_calibration", "evidence": old_record})
    journal.append_archive(
        "evidence",
        {"kind": "input_calibration", "evidence": Controller._evidence_record(old_input)},
    )
    identity_args = dict(
        xid=0x600100,
        pid=os.getpid(),
        executable="/usr/bin/python3",
        title="World of Warcraft",
        wm_class="wowb.exe",
        child_xid=0x600101,
        outer_geometry=(0, 0, 1928, 1092),
        child_geometry=(0, 0, 1920, 1080),
        foreground_xid=0x600101,
        process_start="current-process-start",
        display=":test",
    )
    old_backend = RecordingBackend(identity=WindowIdentity(**identity_args, fingerprint="old-game"))
    old_driver = DesktopDriver(
        profile=DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority")),
        backend=old_backend,
        lease_path=tmp_path / "desktop.lock",
    )
    old_controller = Controller(journal, driver=old_driver)
    assert old_controller.status().state is ControllerState.PAUSED
    assert journal.read()["state"] == ControllerState.PAUSED.value
    assert journal.read()["nativeCalibration"] is False
    assert journal.read()["inputCalibration"] is False
    old_driver.close()

    fresh_backend = RecordingBackend(identity=WindowIdentity(**identity_args, fingerprint="current-game"))
    fresh_driver = DesktopDriver(
        profile=DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority")),
        backend=fresh_backend,
        lease_path=tmp_path / "desktop.lock",
    )
    try:
        restarted = Controller(journal, driver=fresh_driver)
        assert restarted.status().state is ControllerState.PAUSED
        assert restarted.status().generation == old_controller.status().generation + 1
        assert fresh_driver.resolve_identity().fingerprint == "current-game"
        archives = journal.archives("evidence")
        assert archives[0]["evidence"]["fingerprint"] == old_record["fingerprint"]
        assert archives[1]["kind"] == "input_calibration"
        assert journal.read()["nativeCalibration"] is False
        assert journal.read()["inputCalibration"] is False
        assert not fresh_driver.guardian.armed
        resumed = restarted.resume()
        assert resumed.state is ControllerState.NATIVE_CALIBRATION
        rejected_input = restarted.input_calibrated(evidence=old_input)
        assert rejected_input.state is ControllerState.BLOCKED
        assert not fresh_driver.guardian.armed
    finally:
        fresh_driver.close()


def test_closeout_waits_for_idle_frame_and_clears_flag_only_after_cleanup(tmp_path: Path) -> None:
    uid = "uid-closeout"
    data = tmp_path / "Data.lua"
    companion = tmp_path / "companion.json"
    saved = tmp_path / "Crowley.lua"
    _write_data(data, [])
    _write_companion(companion, uid)
    saved.write_text(render_assignment("CrowleyDB", {"scans": {}, "sweeps": {}}, "empty"), encoding="utf-8")
    journal = _state_file(
        tmp_path / "session.json",
        state=ControllerState.DISK_CLEANUP_SENT.value,
        closeoutRequested=True,
        current={"uid": uid, "pending": True, "action": "logout"},
    )
    driver = _FakeDriver(journal)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    config = _config(tmp_path, data=data, companion=companion, saved_variables=saved)
    daemon = SessionDaemon(config, controller=controller, driver=driver)
    daemon._command = lambda _snapshot, command: driver.events.append({"name": command}) or True  # type: ignore[method-assign]

    active_scan = _snapshot(phase="item", pending=[], loaded=[], ah_open=True)
    idle = _snapshot(
        phase="ready",
        pending=[],
        loaded=[],
        ah_open=True,
        schedule_fence_state="inactive",
        saved_uid=uid,
    )
    idle_after_fence = _snapshot(
        phase="ready",
        pending=[],
        loaded=[],
        ah_open=True,
        schedule_fence_state="armed",
        saved_uid=uid,
        sequence=2,
    )
    closeout_samples = iter((_sample(active_scan), _sample(idle), _sample(idle_after_fence)))
    daemon.capture_status = lambda: next(closeout_samples)  # type: ignore[method-assign]
    daemon._advance(active_scan)
    assert driver.events == [], "closeout cannot issue /logout while an automatic scan is active"

    daemon._advance(idle)
    assert driver.events == [{"name": "/crowley session fence"}]

    daemon._advance(idle_after_fence)
    assert driver.events == [{"name": "/crowley session fence"}, {"name": "/logout"}]
    assert controller.status().state == ControllerState.DISK_CLEANUP_WAIT
    controller.cleanup_complete(logout_observed=True)
    assert controller.status().state == ControllerState.STOPPED
    assert controller.status().closeout_requested is False


def test_nested_codex_tool_events_are_rejected_even_with_valid_agent_message() -> None:
    output = (
        '{"type":"item.completed","item":{"type":"tool_call","name":"computer"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":'
        '"{\\"action\\":\\"enter_world\\",\\"target\\":\\"game-client\\"}"}}'
    )
    with pytest.raises(ModelError, match="tool|command"):
        LunaAdapter._parse(output)

    observation = (
        '{"type":"item.completed","item":{"type":"tool_call","name":"computer"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":'
        '"{\\"state\\":\\"world\\",\\"facts\\":{\\"scene\\":\\"world\\",'
        '\\"character\\":\\"Arthur\\",\\"realm\\":\\"Classic Beta PvE 2\\",'
        '\\"modal\\":\\"none\\",\\"code\\":\\"\\",\\"control\\":\\"world\\",'
        '\\"idle\\":\\"true\\"}}"}}'
    )
    with pytest.raises(ModelError, match="tool|command"):
        LunaAdapter._parse_observation(observation)


def test_recovery_has_one_attempt_budget_for_each_named_state(tmp_path: Path) -> None:
    journal = _state_file(tmp_path / "session.json", state=ControllerState.LOGIN_REQUIRED.value)
    driver = _FakeDriver(journal)
    driver.guardian.armed = True
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)
    daemon._capture_model = lambda: b"verified crop"  # type: ignore[method-assign]
    decisions = iter(
        [
            SimpleNamespace(action="dismiss_disconnect"),
            SimpleNamespace(action="reconnect_game"),
            SimpleNamespace(action="select_arthur"),
            SimpleNamespace(action="enter_world"),
            SimpleNamespace(action="enter_world"),
        ]
    )
    daemon.model.decide = lambda *_args, **_kwargs: next(decisions)  # type: ignore[method-assign]
    for _ in range(4):
        daemon._recover_login()
    with pytest.raises(ModelError, match="budget|attempt"):
        daemon._recover_login()
    assert [event["name"] for event in driver.events] == [
        "dismiss_disconnect",
        "reconnect_game",
        "select_arthur",
        "enter_world",
    ]


def test_pause_revocation_cancels_model_process_group(tmp_path: Path, monkeypatch) -> None:
    """A pause must terminate the whole bounded model turn before it can act."""

    class BlockingProcess:
        pid = 42_424

        def __init__(self) -> None:
            self.started = threading.Event()
            self.exited = threading.Event()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.started.set()
            if not self.exited.wait(timeout):
                raise subprocess.TimeoutExpired(["codex"], timeout)
            return "", ""

        def wait(self, timeout: float | None = None) -> int:
            self.exited.wait(timeout)
            if self.returncode is None:
                self.returncode = -signal.SIGTERM
            return self.returncode

    adapter = LunaAdapter(neutral_dir=tmp_path / "neutral", timeout=30)
    process = BlockingProcess()
    killed: list[tuple[int, int]] = []

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout=f"{adapter.cli_version}\n", stderr=""
        )

    def fake_popen(*_args: object, **kwargs: object) -> BlockingProcess:
        assert kwargs["start_new_session"] is True
        return process

    def fake_killpg(pid: int, signum: int) -> None:
        killed.append((pid, signum))
        # Keep TERM blocked so the model loop must observe its cancellation
        # flag and complete the bounded TERM-then-KILL cleanup.  SIGKILL
        # releases the fake child deterministically.
        if signum == signal.SIGKILL:
            process.returncode = -signum
            process.exited.set()

    monkeypatch.setattr("crowley.session.model.subprocess.run", fake_run)
    monkeypatch.setattr("crowley.session.model.subprocess.Popen", fake_popen)
    monkeypatch.setattr("crowley.session.model.os.killpg", fake_killpg)

    journal = _state_file(tmp_path / "session.json", state=ControllerState.WAIT_DUE.value)
    driver = DesktopDriver(
        profile=DriverProfile(),
        backend=RecordingBackend(),
        lease_path=tmp_path / "desktop.lock",
    )
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(_config(tmp_path), controller=controller, driver=driver)
    # The daemon callback is bound to ``self.model`` and therefore follows the
    # injected adapter while retaining the production revoke wiring.
    daemon.model = adapter
    errors: list[BaseException] = []

    def run_model() -> None:
        try:
            adapter.decide(b"verified crop", verified_crop=(0, 0, 640, 480))
        except BaseException as exc:  # asserted below; do not hide worker failures
            errors.append(exc)

    worker = threading.Thread(target=run_model)
    try:
        worker.start()
        assert process.started.wait(2)
        status = controller.pause("operator pause")
        worker.join(timeout=4)
        assert status.state == ControllerState.PAUSED
        assert not worker.is_alive()
        assert killed
        assert killed[0] == (process.pid, signal.SIGTERM)
        assert all(pid == process.pid for pid, _signum in killed)
        assert signal.SIGKILL in [signum for _pid, signum in killed]
        assert len(errors) == 1
        assert isinstance(errors[0], ModelError)
        assert str(errors[0]) == "model decision cancelled"
    finally:
        adapter.cancel()
        worker.join(timeout=4)
        driver.close()


def test_addon_publishes_lua_heap_diagnostic_across_encoding_lifecycle() -> None:
    from .wow_harness import WowClient

    client = WowClient()
    client.login()
    events: list[str] = []
    lua = client.lua
    original_collect = lua.globals().collectgarbage
    original_serialize = lua.globals().C_EncodingUtil.SerializeJSON
    original_compress = lua.globals().C_EncodingUtil.CompressString

    def collect(kind: str) -> float:
        events.append(f"memory:{kind}")
        return 100.0 if events.count("memory:count") == 1 else 125.0

    def serialize(value):
        events.append("serialize")
        return original_serialize(value)

    def compress(value, method):
        events.append("compress")
        return original_compress(value, method)

    lua.globals().collectgarbage = collect
    lua.globals().C_EncodingUtil.SerializeJSON = serialize
    lua.globals().C_EncodingUtil.CompressString = compress
    try:
        client.ns.Agent.Publish()
        result = client.ns.Agent.Publish()
    finally:
        lua.globals().collectgarbage = original_collect
        lua.globals().C_EncodingUtil.SerializeJSON = original_serialize
        lua.globals().C_EncodingUtil.CompressString = original_compress
    snapshot = client._json_table(result)
    heap = snapshot["render"]["luaHeap"]
    assert heap["method"] == "collectgarbage_count"
    assert heap["available"] is True
    assert heap["beforeKiB"] == 100.0
    assert heap["afterKiB"] == 125.0
    assert heap["netKiB"] == 25.0
    assert snapshot["render"]["nativePhase"] == "normal"
    assert snapshot["render"]["nativeOrdinal"] == 0
    assert events.index("memory:count") < events.index("serialize") < events.index("compress")


def test_cli_status_uses_journal_and_socket_from_loaded_config(tmp_path: Path, monkeypatch, capsys) -> None:
    from crowley.session import cli

    config = _config(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    def request(path: Path, payload: dict, **kwargs: object) -> dict:
        seen["path"] = path
        seen["payload"] = payload
        seen["timeout"] = kwargs["timeout"]
        return {"ok": True, "status": {"state": "STOPPED"}}

    monkeypatch.setattr(cli, "_socket_request", request)
    assert cli.main(["--config", str(tmp_path / "session.toml"), "status"]) == 0
    assert seen["path"] == config.socket
    assert seen["payload"] == {"command": "status"}
    assert seen["timeout"] == 5.0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_worker_loop_failure_reaches_service_supervisor() -> None:
    daemon = object.__new__(SessionDaemon)
    daemon._shutdown_event = threading.Event()

    def fail_once() -> None:
        raise RuntimeError("capture worker failed")

    daemon.run_once = fail_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="capture worker failed"):
        daemon.run_forever(interval=0)


def test_cli_socket_supervises_background_worker_failure(tmp_path: Path) -> None:
    """The real CLI socket loop must surface a failed owner thread to its caller."""

    from crowley.session import cli

    class FailingOwner:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.failure = RuntimeError("capture worker failed")

        def run_forever(self) -> None:
            self.started.set()
            assert self.release.wait(2)
            raise self.failure

    socket_path = tmp_path / "session.sock"
    owner = FailingOwner()
    errors: list[BaseException] = []

    def run_server() -> None:
        try:
            # This is the exact supervisor path used by ``main daemon``; the
            # malformed request only exercises the live socket while the owner
            # remains healthy, without requiring a controller fixture.
            cli.serve(socket_path, object(), owner, supervise_owner=True)
        except BaseException as exc:  # asserted after the thread exits
            errors.append(exc)

    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    try:
        assert owner.started.wait(2)
        for _ in range(40):
            if socket_path.exists():
                break
            threading.Event().wait(0.025)
        assert socket_path.exists()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(socket_path))
            connection.sendall(b"{not-json}\n")
            response = json.loads(connection.makefile("r", encoding="utf-8").readline())
        assert response["ok"] is False
        assert response["error"]

        owner.release.set()
        server.join(timeout=3)
        assert not server.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "session capture/action loop failed"
        assert errors[0].__cause__ is owner.failure
        assert not socket_path.exists()
    finally:
        owner.release.set()
        server.join(timeout=3)
