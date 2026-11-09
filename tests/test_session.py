"""Integrated protocol, lease, journal, and controller checks."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image

import crowley.session.protocol as protocol_module
from crowley.session import Controller, ControllerState, SessionMode
from crowley.session.calibration import (
    CalibrationError,
    InputCalibrationEvidence,
    NativeCalibrationEvidence,
    NativeCalibrationRunner,
    NativeMemoryMeasurement,
    XTestInputProfile,
)
from crowley.session.cli import SessionIPC, _socket_request, serve
from crowley.session.decoder import PixelDecoder
from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, RecordingBackend, XI2Observer
from crowley.session.evidence import SweepEvidence
from crowley.session.model import LunaAdapter, ModelError
from crowley.session.protocol import ProtocolError, decode_frame, encode_frame, pack_cells, unpack_cells
from tests.support.fake_host import FAKE_MODEL_DIR, FAKE_WINE_PREFIX, FAKE_XAUTHORITY
from tests.test_session_input_tool import fixture_input_tool

from .wow_harness import WowClient


def snapshot(sequence: int = 1) -> dict:
    return {
        "schema": 6,
        "boot": "boot-1",
        "sequence": sequence,
        "wallTime": 1_000,
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
            "hash": "a" * 64,
            "count": 428,
            "cadenceSeconds": 900,
            "sweepCapSeconds": 600,
            "initialPriceLevels": 20,
        },
        "ahOpen": True,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": {
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
        },
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
            "phase": "ready",
            "activeSweepId": "",
            "startedAt": 0,
            "dueAt": 0,
            "ordinal": 0,
            "planned": 428,
            "cursorItemID": 0,
            "scheduleFence": {"state": "armed", "reason": "", "command": "/crowley session fence"},
        },
        "lastSaved": {
            "memoryUid": "",
            "finishedAt": 0,
            "planned": 0,
            "attempted": 0,
            "failures": 0,
            "rows": 0,
            "persistence": "unknown",
        },
        "pending": {"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": ""},
        "loaded": {"ackedIds": [], "generatedAt": 0},
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
            "textureCount": 2_048,
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


def _varied_ascii(label: str, length: int) -> str:
    value = ""
    index = 0
    while len(value) < length:
        value += hashlib.sha256(f"{label}:{index}".encode()).hexdigest()
        index += 1
    return value[:length]


def evidence(uid: str, started_at: int) -> SweepEvidence:
    item_evidence = {
        str(item_id): {
            "coverage": "full",
            "sourceComplete": True,
            "priced": True,
            "rowCount": 1,
            "pricedRowCount": 1,
            "ownedRows": (
                [
                    {
                        "itemID": item_id,
                        "buyout": {"kind": "known", "copper": 1},
                    }
                ]
                if item_id == 1
                else []
            ),
            "ownedPricedRowCount": 1 if item_id == 1 else 0,
            "category": ("recipe", "container", "equipment")[item_id % 3] if item_id < 4 else "commodity",
            "query": "queried",
        }
        for item_id in range(1, 429)
    }
    return SweepEvidence(
        uid=uid,
        house="beta-normal-alliance",
        plan_hash="plan",
        observations=428,
        attempted=428,
        full=428,
        bounded=0,
        incomplete=0,
        unqueried=0,
        priced=428,
        row_count=428,
        kinds=("commodity", "container", "equipment", "recipe"),
        query_durations=(1,),
        envelope={"startedAt": started_at, "finishedAt": started_at + 2},
        payload={},
        stable_verified=True,
        item_evidence=item_evidence,
        owned_rows=[{"itemID": 1, "buyout": {"kind": "known", "copper": 1}}],
        owned_priced_row_count=1,
        owned_query_complete=True,
    )


def complete_acknowledged_cycle(controller: Controller, value: SweepEvidence, index: int) -> None:
    """Drive one saved/uploaded/acknowledged run through the production states."""

    controller.saved(value.uid)
    controller.reload_sent()
    controller.expect_boot(f"old-save-{index}")
    controller.observe_frame(boot=f"new-save-{index}", sequence=1, crc32=index)
    controller.stable_saved(value)
    controller.uploaded(
        value.uid,
        accepted=True,
        upload_time=value.envelope["finishedAt"] + 1,
        data_ack_uid=value.uid,
    )
    controller.record_ack_publication(
        value.uid,
        companion_uid=value.uid,
        data_uid=value.uid,
        publication_time=value.envelope["finishedAt"] + 1,
        boot=f"new-save-{index}",
        sequence=1,
    )
    controller.ack_reload_command_sent()
    controller.expect_boot(f"old-ack-{index}", acknowledgement=True)
    controller.observe_frame(boot=f"new-ack-{index}", sequence=2, crc32=index + 10)
    controller.ack_reload_complete(
        pending_ids=set(), loaded_uid=value.uid, loaded_at=value.envelope["finishedAt"] + 2
    )


def native_calibration(*, process_start: str = "test-start") -> NativeCalibrationEvidence:
    capture_path = Path(tempfile.gettempdir()) / f"crowley-test-native-{os.getpid()}.capture"
    capture_path.write_bytes(b"owned native calibration capture")
    os.chmod(capture_path, 0o600)
    capture_sha256 = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    return NativeCalibrationEvidence._issue(
        fingerprint="fixture",
        process_pid=os.getpid(),
        process_executable="test",
        process_start=process_start,
        window_xid=1,
        child_xid=2,
        geometry=(0, 0, 1920, 1080),
        xinput_hierarchy=((4, 2, "slave", "xtest pointer"),),
        crop=(80, 48, 256, 256),
        boot="boot-test",
        sequence=1,
        crc32=0,
        render={
            "constructionSeconds": 0.1,
            "updateCount": 102,
            "updateP99Ms": 1,
            "textureCount": 2_048,
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
            NativeMemoryMeasurement(
                method="linux_proc_status_vmhwm_reset5",
                phase="construction",
                ordinal=0,
                pid=os.getpid(),
                process_start=process_start,
                process_executable="test",
                identity_fingerprint="fixture",
                uid=1,
                kernel_release="test-kernel",
                historical_hwm_bytes=0,
                rss_before_reset_bytes=0,
                reset_completed_monotonic=1.0,
                post_reset_hwm_bytes=0,
                rss_after_reset_bytes=0,
                workload_start_monotonic=1.1,
                workload_end_monotonic=1.2,
                workload_start_ordinal=0,
                workload_end_ordinal=0,
                final_hwm_bytes=0,
                final_rss_bytes=0,
                growth_bytes=0,
                limit_bytes=128 * 1024 * 1024,
            ),
            NativeMemoryMeasurement(
                method="linux_proc_status_vmhwm_reset5",
                phase="updates",
                ordinal=0,
                pid=os.getpid(),
                process_start=process_start,
                process_executable="test",
                identity_fingerprint="fixture",
                uid=1,
                kernel_release="test-kernel",
                historical_hwm_bytes=0,
                rss_before_reset_bytes=0,
                reset_completed_monotonic=2.0,
                post_reset_hwm_bytes=0,
                rss_after_reset_bytes=0,
                workload_start_monotonic=2.1,
                workload_end_monotonic=2.2,
                workload_start_ordinal=1,
                workload_end_ordinal=100,
                final_hwm_bytes=0,
                final_rss_bytes=0,
                growth_bytes=0,
                limit_bytes=16 * 1024 * 1024,
            ),
        ),
        native_identity_fingerprint="fixture",
        resume_sequence=2,
        resume_crc32=1,
        resume_render={
            "constructionSeconds": 0.1,
            "updateCount": 103,
            "updateP99Ms": 1,
            "textureCount": 2_048,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": True,
                "beforeKiB": 0,
                "afterKiB": 0,
                "netKiB": 0,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        },
        resume_capture_monotonic=2.0,
        capture_path=str(capture_path),
        capture_sha256=capture_sha256,
        capture_monotonic=1.0,
        xauthority=str(FAKE_XAUTHORITY),
        wine_prefix=str(FAKE_WINE_PREFIX),
        preconstruction_fingerprint="preconstruction-test",
    )


def input_calibration(*, game_identity: str = "game-test-identity") -> InputCalibrationEvidence:
    point = (10, 20)
    endpoint = (11, 21)
    event_proof: list[tuple[object, ...]] = []
    timestamp = 1

    def add_events(source: int, master: int, detail: int, classes: tuple[str, ...]) -> None:
        nonlocal timestamp
        for event_class in classes:
            delivered = not event_class.startswith("Raw")
            event_master = source if delivered else master
            event_proof.append(
                (
                    event_class,
                    event_master,
                    source,
                    detail,
                    timestamp,
                    100 if delivered else None,
                    200 if delivered else None,
                    2 if delivered else None,
                    point if delivered else None,
                    point if delivered else None,
                    (0, 0, 0) if delivered else None,
                    0 if delivered else None,
                )
            )
            timestamp += 1

    # The owned workflow records the baseline key/button probes, then the
    # measured keyboard profile transition, followed by the human controls.
    add_events(5, 3, 8, ("RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"))
    add_events(5, 3, 38, ("RawKeyPress", "KeyPress"))
    # Keep one complete source-4 raw/primary pair in this sealed fixture.  It
    # records the owned pointer query used by the native producer; the later
    # source-7 motion is the controlled forwarded echo.
    motion_timestamp = timestamp
    event_proof.append(("RawMotion", 2, 4, 0, motion_timestamp, None, None, None, None, None, None, None))
    event_proof.append(("Motion", 4, 4, 0, motion_timestamp, 100, 100, 1, point, point, (0, 0, 0), 0))
    timestamp += 1
    add_events(4, 2, 1, ("RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"))
    add_events(9, 3, 8, ("RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"))
    add_events(7, 2, 1, ("RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"))
    event_proof.append(("Motion", 7, 7, 1, timestamp, 100, 100, 1, endpoint, endpoint, (0, 0, 0), 0))
    synthetic_echo_proof = (("MotionEcho", timestamp, 7, 7, 1, endpoint, endpoint, motion_timestamp, 4, 1),)
    hierarchy = (
        (4, 2, "slave", "xtest pointer"),
        (5, 3, "slave", "xtest keyboard"),
        (7, 2, "slave", "human pointer"),
        (9, 3, "slave", "human keyboard"),
    )
    return InputCalibrationEvidence._issue(
        fingerprint="input-test",
        display=":0",
        target_pid=os.getpid(),
        target_xid=1,
        target_child_xid=2,
        synthetic_key_source=5,
        synthetic_pointer_source=4,
        human_key_source=9,
        human_pointer_source=7,
        xinput_hierarchy=hierarchy,
        xtest_profile=XTestInputProfile(
            4,
            2,
            "xtest pointer",
            3,
            ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
            1,
            1,
            (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            100,
            (0, 0, 1920, 1080),
            hierarchy,
        ),
        synthetic_events=(
            ("KeyPress", 5, 8),
            ("RawMotion", 4, 0),
            ("Motion", 4, 0),
            ("ButtonPress", 4, 1),
        ),
        human_events=(("KeyPress", 9, 8), ("ButtonPress", 7, 1)),
        forwarded_events=(("Motion", 7, 1),),
        driver_version="native-x11-3",
        xauthority=str(FAKE_XAUTHORITY),
        event_proof=tuple(event_proof),
        game_identity_fingerprint=game_identity,
        game_geometry=(0, 0, 1920, 1080),
        target_executable="python3",
        target_process_start="target-test-start",
        target_geometry=(0, 0, 480, 240),
        managed_frame_xid=3,
        managed_frame_geometry=(0, 0, 500, 260),
        target_event_xids=(1, 2, 3),
        synthetic_modifiers=(0, 0, 0),
        synthetic_echo_proof=synthetic_echo_proof,
        synthetic_motion_transitions=(
            (
                "XTestMotionTransition",
                motion_timestamp,
                4,
                4,
                2,
                1,
                100,
                100,
                point,
                point,
                endpoint,
                endpoint,
                ((0, 11.0, 11.0), (1, 21.0, 21.0)),
                (0, 0, 0),
                0,
            ),
        ),
        synthetic_motion_query=(point[0], point[1], 1, endpoint[0], endpoint[1], endpoint[0], endpoint[1], 1),
        synthetic_motion_seed=0,
        synthetic_motion_plan=((0.0, point[0], point[1]), (0.5, endpoint[0], endpoint[1])),
        keyboard_keymap=(("A", 38),),
        keyboard_profile=(("type", "A", (("press", "A", 38, (0, 0, 0), 0),)),),
    )


def make_test_driver(backend: RecordingBackend, *, lease_path: Path | None = None) -> DesktopDriver:
    """Use the same explicit Xauthority identity as sealed test evidence."""

    return DesktopDriver(
        profile=DriverProfile(xauthority=str(FAKE_XAUTHORITY)), backend=backend, lease_path=lease_path
    )


def test_lua_frame_roundtrips_through_pixel_packer_and_python_decoder() -> None:
    client = WowClient(faction="Alliance", realm="Classic Beta PvE 2", current_region=110)
    client.set_canonical_plan()
    client.login()
    frame = client.agent_frame
    assert client.texture_count == 2_048
    assert unpack_cells(pack_cells(frame)) == frame
    decoded = decode_frame(frame)
    assert decoded.snapshot.value["plan"]["count"] == 428
    assert decoded.snapshot.value["motion"] == {
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
                "run": 7,
                "flight": 7,
                "swim": 4.7222218513489,
                "unitStatus": "unverified",
            },
        },
    }
    client.update_agent()
    assert client.texture_count == 2_048
    copy_value = client.publish_agent()
    copy_value["pending"]["ids"].append("mutated")
    assert "mutated" not in client.publish_agent()["pending"]["ids"]


def test_protocol_requires_schema6_executor_motion_and_auctioneer_facts_and_rejects_legacy_or_malformed_motion() -> (
    None
):
    legacy = snapshot()
    legacy["schema"] = 1
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)
    legacy["schema"] = 2
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)
    legacy["schema"] = 3
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)
    legacy["schema"] = 4
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)
    legacy["schema"] = 5
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)
    legacy["schema"] = 7
    with pytest.raises(ProtocolError, match="schema"):
        encode_frame(legacy)

    missing = snapshot()
    del missing["motion"]
    with pytest.raises(ProtocolError, match="motion"):
        encode_frame(missing)

    missing = snapshot()
    del missing["blockingUI"]
    with pytest.raises(ProtocolError, match="blockingUI"):
        encode_frame(missing)

    malformed = snapshot()
    malformed["blockingUI"]["staticPopupAvailable"] = 1
    with pytest.raises(ProtocolError, match="blockingUI.staticPopupAvailable"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["blockingUI"]["gameMenuAvailable"] = False
    malformed["blockingUI"]["gameMenuShown"] = True
    with pytest.raises(ProtocolError, match="cannot be true when unavailable"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["motion"]["bindings"]["turnRight"] = "shift w"
    with pytest.raises(ProtocolError, match="motion"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["motion"]["position"]["x"] = float("nan")
    with pytest.raises(ProtocolError, match="motion.position.x"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["motion"]["bindings"]["moveForward"] = "shift w"
    with pytest.raises(ProtocolError, match="moveForward"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["auctioneer"]["targetName"] = "Auctioneer Jaxon"
    with pytest.raises(ProtocolError, match="absent target"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["auctioneer"].update(targetPresent=True, targetName="Auctioneer Jaxon")
    malformed["auctioneer"]["withinTradeRange"] = True
    with pytest.raises(ProtocolError, match="cannot be true"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["auctioneer"]["bindingAvailable"] = False
    malformed["auctioneer"]["ctrlF12Action"] = "INTERACTTARGET"
    with pytest.raises(ProtocolError, match="unavailable binding"):
        encode_frame(malformed)

    malformed = snapshot()
    del malformed["auctioneer"]["interactionMode"]
    with pytest.raises(ProtocolError, match="interactionMode"):
        encode_frame(malformed)

    malformed = snapshot()
    malformed["auctioneer"]["interactionMode"] = "1"
    with pytest.raises(ProtocolError, match="invalid native SoftTargetInteract mode"):
        encode_frame(malformed)


def test_protocol_hover_name_limit_counts_utf8_bytes() -> None:
    value = snapshot()
    exact = "é" * 24
    value["hover"] = {"kind": "unit", "name": exact}
    decoded = decode_frame(encode_frame(value))
    assert decoded.snapshot["hover"] == {"kind": "unit", "name": exact}

    value["hover"]["name"] = "é" * 25
    with pytest.raises(ProtocolError, match="UTF-8 value is too long"):
        encode_frame(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["executor"].update(extra=True),
        lambda value: value["executor"].update(availability="bogus"),
        lambda value: value["executor"].update(queueId="q" * 193),
        lambda value: value["executor"].update(lastClickSource="button", clickOrdinal=0),
        lambda value: value["executor"].update(lastResult={"seq": 0, "kind": "", "status": "started"}),
        lambda value: value["executor"].update(
            availability="valid",
            queueId="queue-1",
            house="beta-normal-alliance",
            character="Arthur",
            buttonVisible=True,
            buttonName="CrowleyTradeExecutorButton",
            currentSeq=1,
            currentKind="vendor_sell",
            phase="sell_stacks",
            clickReady=True,
            blocked=True,
        ),
        lambda value: value["executor"].update(
            availability="valid",
            queueId="queue-1",
            house="beta-normal-alliance",
            character="Arthur",
            buttonVisible=True,
            buttonName="CrowleyTradeExecutorButton",
            currentSeq=1,
            currentKind="vendor_sell",
            phase="sell_stacks",
            clickReady=True,
            blocked=True,
            ambiguous=True,
        ),
        lambda value: value["executor"].update(
            availability="valid",
            queueId="queue-1",
            house="beta-normal-alliance",
            character="Arthur",
            buttonName="CrowleyTradeExecutorButton",
            currentSeq=1,
            currentKind="buy_commodity",
            phase="ready",
            inFlight=True,
        ),
        lambda value: value["executor"].update(
            availability="valid",
            queueId="queue-1",
            house="beta-normal-alliance",
            character="Arthur",
            buttonName="CrowleyTradeExecutorButton",
            currentSeq=1,
            currentKind="post_commodity",
            phase="ready",
            inFlight=True,
            resultCount=1,
            lastResult={"seq": 1, "kind": "post_commodity", "status": "failed"},
        ),
    ],
    ids=(
        "unknown-key",
        "availability",
        "queue-id-limit",
        "button-ordinal",
        "none-result",
        "blocked-click-ready",
        "ambiguous-click-ready",
        "buy-ready-in-flight",
        "post-ready-wrong-result",
    ),
)
def test_protocol_rejects_executor_contract_mutations(mutate) -> None:
    value = snapshot()
    mutate(value)
    with pytest.raises(ProtocolError, match="executor"):
        encode_frame(value)


@pytest.mark.parametrize(
    ("kind", "phase"),
    [
        ("mail_collect", "ready_to_confirm"),
        ("vendor_sell", "await_purchase"),
        ("vendor_buy", "await_craft"),
        ("craft", "await_vendor_buy"),
    ],
)
def test_protocol_rejects_executor_phase_kind_mismatch(kind: str, phase: str) -> None:
    value = snapshot()
    value["executor"].update(
        availability="valid",
        queueId="queue-1",
        house="beta-normal-alliance",
        character="Arthur",
        buttonName="CrowleyTradeExecutorButton",
        currentSeq=1,
        currentKind=kind,
        phase=phase,
        workTotal=1 if kind == "mail_collect" else 0,
    )
    with pytest.raises(ProtocolError, match="incompatible"):
        encode_frame(value)


def test_protocol_accepts_latched_commodity_cancel_click_readiness() -> None:
    value = snapshot()
    value["executor"].update(
        availability="valid",
        queueId="queue-1",
        house="beta-normal-alliance",
        character="Arthur",
        buttonVisible=True,
        buttonName="CrowleyTradeExecutorButton",
        currentSeq=1,
        currentKind="buy_commodity",
        phase="must_cancel",
        inFlight=True,
        clickReady=True,
        latched=True,
    )
    decode_frame(encode_frame(value))


@pytest.mark.parametrize("kind", ["post_commodity", "cancel_auction"], ids=["post", "cancel"])
def test_protocol_accepts_latched_phase_less_post_cancel_blocked_result(kind: str) -> None:
    value = snapshot()
    value["executor"].update(
        availability="valid",
        queueId="queue-1",
        house="beta-normal-alliance",
        character="Arthur",
        currentSeq=1,
        currentKind=kind,
        phase="ready",
        inFlight=True,
        latched=True,
        resultCount=2,
        lastResult={"seq": 1, "kind": kind, "status": "blocked"},
    )
    decode_frame(encode_frame(value))


def test_pixel_decoder_rejects_forged_legacy_wire_frame() -> None:
    valid = encode_frame(snapshot())
    payload_length = int.from_bytes(valid[8:10], "little")
    raw = zlib.decompress(valid[26 : 26 + payload_length], wbits=-15)
    assert b'"schema":6' in raw
    forged_raw = raw.replace(b'"schema":6', b'"schema":5', 1)
    compressor = zlib.compressobj(wbits=-15)
    forged_payload = compressor.compress(forged_raw) + compressor.flush()
    assert 1 <= len(forged_payload) <= 2000
    forged = bytearray(valid[:26] + forged_payload + bytes(2048 - 26 - len(forged_payload)))
    struct.pack_into("<H", forged, 8, len(forged_payload))
    struct.pack_into("<I", forged, 22, binascii.crc32(forged[:22] + forged_payload) & 0xFFFFFFFF)
    forged_frame = bytes(forged)

    with pytest.raises(ProtocolError, match="schema"):
        decode_frame(forged_frame)

    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    for row, bits in enumerate(pack_cells(forged_frame)):
        for column, bit in enumerate(bits):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    pixels[column * 2 + dx, row * 2 + dy] = color
    with pytest.raises(ProtocolError, match="schema"):
        PixelDecoder().decode(image, monotonic=1.0, window_identity="legacy-wire")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["hover"].update(kind="unknown"),
        lambda value: value["hover"].update(kind="object", name=""),
        lambda value: value["hover"].update(kind="none", name="NPC"),
        lambda value: value["hover"].update(kind="unit", name="x" * 49),
        lambda value: value.update(mailOpen=1),
        lambda value: value["motion"]["bindings"].pop("jump"),
        lambda value: value["motion"]["bindings"].pop("turnRight"),
    ],
    ids=[
        "unknown-hover-kind",
        "object-empty-name",
        "none-nonempty-name",
        "overlong-hover-name",
        "nonboolean-mail-open",
        "missing-jump",
        "missing-turn-right",
    ],
)
def test_protocol_rejects_schema6_hover_mail_and_binding_contract_mutations(mutate) -> None:
    value = snapshot()
    mutate(value)
    with pytest.raises(ProtocolError):
        encode_frame(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["motion"].pop("native"),
        lambda value: value["motion"]["native"].update(extra=True),
        lambda value: value["motion"]["native"]["ctm"].update(state="bogus"),
        lambda value: value["motion"]["native"]["ctm"].update(reason="bogus"),
        lambda value: value["motion"]["native"]["ground"].update(
            state="grounded", falling="unknown", reason=""
        ),
        lambda value: value["motion"]["native"]["speedTuple"].update(current=-1),
        lambda value: value["motion"]["native"]["speedTuple"].update(run=float("nan")),
        lambda value: value["motion"]["native"]["speedTuple"].update(unitStatus="meters"),
        lambda value: value["motion"]["native"]["speedTuple"].update(current=1),
    ],
    ids=[
        "missing-native",
        "extra-native",
        "invalid-ctm-state",
        "invalid-failure-code",
        "contradictory-ground",
        "negative-speed",
        "nonfinite-speed",
        "invalid-unit-status",
        "speed-mismatch",
    ],
)
def test_protocol_rejects_native_capability_contract_mutations(mutate) -> None:
    value = snapshot()
    mutate(value)
    with pytest.raises(ProtocolError, match="motion"):
        encode_frame(value)


@pytest.mark.parametrize(
    ("mutate", "path"),
    (
        (
            lambda value: value["motion"]["native"]["ctm"].update(accessor="GetCVar"),
            "motion.native.ctm.accessor",
        ),
        (
            lambda value: value["motion"]["native"]["ctm"].update(cvar="autoInteract"),
            "motion.native.ctm.cvar",
        ),
        (
            lambda value: value["motion"]["native"]["ctm"].update(state="unknown", reason=""),
            "motion.native.ctm",
        ),
        (
            lambda value: value["motion"]["native"]["ctm"].update(state="enabled", reason="missing"),
            "motion.native.ctm",
        ),
        (lambda value: value["motion"]["native"]["ctm"].pop("reason"), "motion.native.ctm"),
        (lambda value: value["motion"]["native"]["ctm"].update(extra="x"), "motion.native.ctm"),
        (
            lambda value: value["motion"]["native"]["ground"].update(state="not_grounded"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].update(falling="true"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].update(reason="call_error"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].update(falling="unknown"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].update(falling="bogus"),
            "motion.native.ground.falling",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].pop("taxi"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["ground"].update(extra="x"),
            "motion.native.ground",
        ),
        (
            lambda value: value["motion"]["native"]["speedTuple"].update(run=-1),
            "motion.native.speedTuple.run",
        ),
        (
            lambda value: value["motion"]["native"]["speedTuple"].update(run=1_001),
            "motion.native.speedTuple.run",
        ),
        (
            lambda value: value["motion"]["native"]["speedTuple"].update(unitStatus="meters"),
            "motion.native.speedTuple.unitStatus",
        ),
        (
            lambda value: value["motion"]["native"]["speedTuple"].pop("swim"),
            "motion.native.speedTuple",
        ),
        (
            lambda value: value["motion"]["native"]["speedTuple"].update(extra=0),
            "motion.native.speedTuple",
        ),
    ),
    ids=(
        "ctm-accessor",
        "ctm-cvar",
        "ctm-unknown-without-reason",
        "ctm-known-with-reason",
        "ctm-missing-key",
        "ctm-extra-key",
        "ground-state-without-predicate",
        "ground-predicate-state",
        "ground-known-reason",
        "ground-unknown-without-reason",
        "ground-invalid-predicate",
        "ground-missing-key",
        "ground-extra-key",
        "speed-negative",
        "speed-over-ceiling",
        "speed-status",
        "speed-missing-key",
        "speed-extra-key",
    ),
)
def test_protocol_reports_each_native_semantic_boundary(mutate, path: str) -> None:
    value = snapshot()
    mutate(value)
    with pytest.raises(ProtocolError, match=re.escape(path)):
        encode_frame(value)


def test_protocol_accepts_the_inclusive_native_speed_ceiling() -> None:
    value = snapshot()
    value["motion"]["speed"] = 1_000
    value["motion"]["native"]["speedTuple"]["current"] = 1_000
    value["motion"]["native"]["speedTuple"]["run"] = 1_000
    assert decode_frame(encode_frame(value)).snapshot.value["motion"]["speed"] == 1_000.0


def _max_executor_client(*, varied_identities: bool, pending_length: int) -> WowClient:
    identity = _varied_ascii if varied_identities else lambda _label, length: "x" * length
    queue_id = "q" * 192
    house = "h" * 256
    character = "c" * 256
    if varied_identities:
        queue_id = identity("queue-id", 192)
        house = identity("house", 256)
        character = identity("character", 256)
    pending_ids = sorted(_varied_ascii(f"pending-{index}", pending_length) for index in range(6))
    queue = {
        "format": 1,
        "queueId": queue_id,
        "generatedAt": 1_759_990_000,
        "expiresAt": 1_760_000_100,
        "house": house,
        "character": character,
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 4_294_967_295}],
    }
    saved = {
        "scans": {
            pending_id: {"id": pending_id, "finishedAt": 1_759_990_000 + index}
            for index, pending_id in enumerate(pending_ids)
        },
    }
    client = WowClient(queue=queue, initial_savedvars=saved)
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(1)
    client.set_frame_open("mailbox")
    return client


def test_real_lua_max_field_bounds_payload_stays_within_wire_caps() -> None:
    # Keep six deterministic, varied pending IDs in the real SavedVariables
    # path while exercising the maximum executor identity/progress fields.
    client = _max_executor_client(varied_identities=True, pending_length=32)
    client.update_agent()
    queue_id = _varied_ascii("queue-id", 192)
    house = _varied_ascii("house", 256)
    character = _varied_ascii("character", 256)
    pending_ids = sorted(_varied_ascii(f"pending-{index}", 32) for index in range(6))

    frame = client.agent_frame
    assert len(frame) == 2048
    payload_length = int.from_bytes(frame[8:10], "little")
    assert 1 <= payload_length < 2000
    direct = decode_frame(frame)
    executor = direct.snapshot.value["executor"]
    assert direct.snapshot.value["schema"] == 6
    assert executor["availability"] == "valid"
    assert executor["queueId"] == queue_id
    assert executor["house"] == house
    assert executor["character"] == character
    assert executor["currentSeq"] == 1
    assert executor["currentKind"] == "mail_collect"
    assert executor["phase"] == "ready"
    assert executor["workDone"] == 0
    assert executor["workTotal"] == 4_294_967_295
    assert executor["resultCount"] == 0
    assert direct.snapshot.value["pending"]["ids"] == pending_ids

    decoder = PixelDecoder()
    captured = decoder.decode(
        client.render_agent_capture(), monotonic=1.0, window_identity="max-executor-wire"
    )
    assert captured.kind == "FRESH" and captured.frame is not None
    assert captured.frame.payload_length == payload_length
    assert captured.frame.snapshot["executor"] == executor

    # Produce a second real Lua frame with a terminal result while retaining
    # the maximum accepted queue identity.  This gives the same bounded
    # producer path both its pre-work progress witness and result witness.
    assert client.ns.Executor.Skip() is True
    client.update_agent()
    terminal = decode_frame(client.agent_frame)
    terminal_executor = terminal.snapshot.value["executor"]
    assert terminal_executor["currentSeq"] == 0
    assert terminal_executor["phase"] == "complete"
    assert terminal_executor["resultCount"] == 1
    assert terminal_executor["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "skipped"}
    terminal_capture = decoder.decode(
        client.render_agent_capture(), monotonic=2.0, window_identity="max-executor-wire"
    )
    assert terminal_capture.kind == "FRESH" and terminal_capture.frame is not None
    assert terminal_capture.frame.snapshot["executor"]["lastResult"] == terminal_executor["lastResult"]


def test_real_lua_canonical_executor_frame_carries_native_scanner_uids() -> None:
    house = "beta-normal-alliance"
    character = "Tester"
    started_at = 1_760_000_000
    pending_ids = [f"{house}-{started_at}-{character}-{index:04d}" for index in range(6)]
    queue = {
        "format": 1,
        "queueId": f"{house}-{character}-{started_at}-abcd",
        "generatedAt": started_at,
        "expiresAt": started_at + 600,
        "house": house,
        "character": character,
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 2}],
    }
    saved = {
        "scans": {
            pending_id: {"id": pending_id, "finishedAt": started_at + index}
            for index, pending_id in enumerate(pending_ids)
        },
    }
    client = WowClient(
        faction="Alliance",
        realm="Classic Beta PvE 2",
        current_region=110,
        queue=queue,
        initial_savedvars=saved,
    )
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(2)
    client.set_frame_open("mailbox")
    client.update_agent()

    frame = client.agent_frame
    payload_length = int.from_bytes(frame[8:10], "little")
    direct = decode_frame(frame)
    executor = direct.snapshot.value["executor"]
    assert 1 <= payload_length < 2_000
    assert direct.snapshot.value["schema"] == 6
    assert direct.snapshot.value["house"] == {"key": house, "source": "auto"}
    assert direct.snapshot.value["pending"]["ids"] == pending_ids
    assert executor["availability"] == "valid"
    assert executor["queueId"] == queue["queueId"]
    assert executor["currentKind"] == "mail_collect"
    assert executor["phase"] == "ready"
    assert executor["clickReady"] is True
    assert executor["workDone"] == 0 and executor["workTotal"] == 2
    assert executor["publishedArmed"] is False and executor["sessionArmed"] is False

    captured = PixelDecoder().decode(
        client.render_agent_capture(), monotonic=1.0, window_identity="canonical-executor-wire"
    )
    assert captured.kind == "FRESH" and captured.frame is not None
    assert captured.frame.payload_length == payload_length
    assert captured.frame.snapshot["executor"] == executor


def test_real_lua_high_entropy_max_bound_payload_is_rejected_without_fallback() -> None:
    # This is deliberately an aggregate oversize case, not a silently reduced
    # positive fixture: all bounded identities and all six pending IDs are
    # deterministic high-entropy values at their accepted byte ceilings.
    client = _max_executor_client(varied_identities=True, pending_length=32)
    client.publish_agent()
    before = client.agent_frame
    oversized_ids = sorted(_varied_ascii(f"pending-{index}", 192) for index in range(6))
    client.lua.globals().CrowleyDB.scans = client._to_lua(
        {
            pending_id: {"id": pending_id, "finishedAt": 1_759_990_000 + index}
            for index, pending_id in enumerate(oversized_ids)
        }
    )
    value = client.snapshot_agent()
    # Prove the aggregate rejection is independent of schema validity: every
    # field remains acceptable to the strict consumer before compression.
    protocol_module.validate_snapshot(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(raw) + compressor.flush()
    assert value["schema"] == 6
    assert value["executor"]["availability"] == "valid"
    assert value["executor"]["workTotal"] == 4_294_967_295
    assert len(compressed) > 2_000
    with pytest.raises(Exception, match="payload exceeds 2000 bytes"):
        client.publish_agent()
    assert client.agent_frame == before


@pytest.mark.parametrize("compressed_size", (2_000, 2_001), ids=("accepted-2000", "refused-2001"))
def test_protocol_preserves_exact_compressed_payload_boundary(
    monkeypatch: pytest.MonkeyPatch, compressed_size: int
) -> None:
    """The fixed CRS1 frame admits exactly 2,000 payload bytes."""

    class SizedCompressor:
        def compress(self, raw: bytes) -> bytes:
            del raw
            return b"x" * compressed_size

        def flush(self) -> bytes:
            return b""

    monkeypatch.setattr(
        protocol_module.zlib,
        "compressobj",
        lambda **kwargs: SizedCompressor(),
    )
    if compressed_size == 2_000:
        frame = encode_frame(snapshot())
        assert len(frame) == protocol_module.CAPACITY
        assert int.from_bytes(frame[8:10], "little") == 2_000
    else:
        with pytest.raises(ProtocolError, match="outside 1..2000"):
            encode_frame(snapshot())


def test_byte_atlas_header_and_all_lsb_first_glyph_rows() -> None:
    atlas_path = Path(__file__).resolve().parents[1] / "addon/Crowley/Textures/ByteAtlas.tga"
    raw = atlas_path.read_bytes()
    assert raw[0] == 0
    assert raw[1] == 0
    assert raw[2] == 2
    assert int.from_bytes(raw[12:14], "little") == 16
    assert int.from_bytes(raw[14:16], "little") == 512
    assert raw[16] == 32
    assert raw[17] == 8  # RGBA with bottom-left origin and eight alpha bits.
    assert len(raw) == 18 + 16 * 512 * 4
    with Image.open(atlas_path) as atlas:
        atlas.load()
        assert atlas.mode == "RGBA"
        assert atlas.size == (16, 512)
        for value in range(256):
            expected = [
                (255, 255, 255) if (value >> bit) & 1 else (0, 0, 0) for bit in range(8) for _ in (0, 1)
            ]
            for row in (0, 1):
                pixels = [atlas.getpixel((x, value * 2 + row)) for x in range(16)]
                assert [pixel[:3] for pixel in pixels] == expected
                assert all(pixel[3] == 255 for pixel in pixels)


@pytest.mark.parametrize("root_ui_scale", (1.0, 0.8, 1.25))
def test_lua_atlas_rasterizes_the_actual_surface_and_decodes(tmp_path: Path, root_ui_scale: float) -> None:
    client = WowClient(
        faction="Alliance", realm="Classic Beta PvE 2", current_region=110, root_ui_scale=root_ui_scale
    )
    client.set_canonical_plan()
    client.login()
    client.update_agent()
    client.assert_native_geometry()
    assert client.texture_count == 2_048
    assert all(
        texture.snap is True
        and texture.bias == 0.5
        and texture.textureArgs.textureAsset == "Interface\\AddOns\\Crowley\\Textures\\ByteAtlas"
        and texture.textureArgs.wrapHorizontal == "CLAMP"
        and texture.textureArgs.wrapVertical == "CLAMP"
        and texture.textureArgs.filterMode == "NEAREST"
        for texture in client.state.textures.values()
    )
    screenshot = client.render_agent_screenshot()
    capture_path = tmp_path / f"agent-{root_ui_scale}.png"
    screenshot.crop((80, 48, 336, 304)).save(capture_path)
    NativeCalibrationRunner._validate_cells(capture_path)
    decoded = PixelDecoder().decode(
        screenshot.crop((80, 48, 336, 304)), monotonic=1.0, window_identity=f"wow-{root_ui_scale}"
    )
    expected = decode_frame(client.agent_frame)
    assert decoded.kind == "FRESH"
    assert decoded.frame is not None
    assert decoded.frame.sequence == expected.sequence
    assert decoded.frame.crc32 == expected.crc32
    assert decoded.frame.snapshot.value == expected.snapshot.value


def test_lua_atlas_wrong_texture_offset_and_scale_fail_actual_cell_validation(tmp_path: Path) -> None:
    shifted = WowClient(root_ui_scale=1.0)
    shifted.set_canonical_plan()
    shifted.login()
    shifted.update_agent()
    first_texture = next(iter(shifted.state.textures.values()))
    first_texture.point[4] = first_texture.point[4] + 1
    shifted_path = tmp_path / "shifted.png"
    shifted.render_agent_screenshot().crop((80, 48, 336, 304)).save(shifted_path)
    with pytest.raises(CalibrationError):
        NativeCalibrationRunner._validate_cells(shifted_path)

    scaled = WowClient(root_ui_scale=1.0)
    scaled.set_canonical_plan()
    scaled.login()
    scaled.update_agent()
    scaled.ns.Agent.Surface().scale = scaled.ns.Agent.Surface().scale * 0.9
    scaled_path = tmp_path / "scaled.png"
    scaled.render_agent_screenshot().crop((80, 48, 336, 304)).save(scaled_path)
    with pytest.raises(CalibrationError):
        NativeCalibrationRunner._validate_cells(scaled_path)


def test_lua_atlas_linear_filter_fails_actual_cell_validation(tmp_path: Path) -> None:
    client = WowClient(root_ui_scale=1.0)
    client.set_canonical_plan()
    client.login()
    first_texture = next(iter(client.state.textures.values()))
    # A half-texel rasterization phase is enough for linear filtering to blend
    # this glyph row with its adjacent atlas row.  Nearest sampling remains on
    # the intended row at the same phase; this exercises the filter choice in
    # the actual screenshot raster rather than only inspecting the call args.
    first_texture.texCoord[3] = first_texture.texCoord[3] + 0.5 / 512
    first_texture.texCoord[4] = first_texture.texCoord[4] + 0.5 / 512
    nearest_path = tmp_path / "nearest.png"
    client.render_agent_screenshot().crop((80, 48, 336, 304)).save(nearest_path)
    NativeCalibrationRunner._validate_cells(nearest_path)

    # Change only the recorded native filter argument.  The duplicate
    # convenience attribute remains NEAREST, so the raster must prove that it
    # follows SetTexture's recorded call rather than a harness default.
    first_texture.textureArgs.filterMode = "LINEAR"
    linear_path = tmp_path / "linear.png"
    client.render_agent_screenshot().crop((80, 48, 336, 304)).save(linear_path)
    with pytest.raises(CalibrationError):
        NativeCalibrationRunner._validate_cells(linear_path)


def test_protocol_rejects_padding_and_duplicate_progress() -> None:
    frame = encode_frame(snapshot())
    damaged = bytearray(frame)
    damaged[-1] = 1
    with pytest.raises(ProtocolError):
        decode_frame(damaged)
    decoded = decode_frame(frame)
    assert decoded.payload_length > 0


def test_protocol_rejects_unverified_peak_capability_claim() -> None:
    value = snapshot()
    value["render"]["luaHeap"] = {
        "method": "collectgarbage_count",
        "available": False,
        "beforeKiB": 1,
        "afterKiB": 2,
        "netKiB": 1,
    }
    with pytest.raises(ProtocolError, match="unavailable measurement"):
        encode_frame(value)


def test_protocol_accepts_compact_texture_diagnostic_and_bounds_it() -> None:
    compact = snapshot()
    compact["render"]["textureCount"] = 2_048
    assert decode_frame(encode_frame(compact)).snapshot.value["render"]["textureCount"] == 2_048
    for count in (0, 16_385):
        invalid = snapshot()
        invalid["render"]["textureCount"] = count
        with pytest.raises(ProtocolError):
            encode_frame(invalid)


def test_protocol_rejects_malformed_native_phase_types() -> None:
    for phase in ([], {}, 1, None):
        value = snapshot()
        value["render"]["nativePhase"] = phase
        with pytest.raises(ProtocolError):
            encode_frame(value)


def test_protocol_rejects_cursor_outside_the_configured_plan() -> None:
    value = snapshot()
    value["scanner"]["cursorItemID"] = 9999
    with pytest.raises(ProtocolError, match="cursor is outside the configured plan"):
        encode_frame(value, plan_item_ids={1, 2, 3})


def test_pixel_decoder_uses_strict_black_white_cells_and_freshness() -> None:
    frame = encode_frame(snapshot())
    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    for row, bits in enumerate(pack_cells(frame)):
        for column, bit in enumerate(bits):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    pixels[column * 2 + dx, row * 2 + dy] = color
    decoder = PixelDecoder()
    first = decoder.decode(image, monotonic=1.0, window_identity="wow-1")
    repeat = decoder.decode(image, monotonic=2.0, window_identity="wow-1")
    assert first.kind == "FRESH" and repeat.kind == "NO_CHANGE"


def test_controller_validation_seed_and_closeout_without_fourth_scan() -> None:
    with tempfile.TemporaryDirectory() as directory:
        driver = make_test_driver(RecordingBackend(), lease_path=Path(directory) / "desktop.lock")
        try:
            controller = Controller(Path(directory) / "session.json", driver=driver)
            assert controller.start(SessionMode.VALIDATION).state == ControllerState.NATIVE_CALIBRATION
            controller.native_calibrated(native_calibration())
            controller.input_calibrated(evidence=input_calibration())
            controller.reconcile(identity_ok=True)
            for index, start in enumerate((100, 1000, 1900), 1):
                controller.begin_scan(started_at=start)
                value = evidence(f"uid-{index}", start)
                complete_acknowledged_cycle(controller, value, index)
                controller.qualify(value, started_at=start)
                if index < 3:
                    controller.reopen_complete()
            assert controller.status().state == ControllerState.DISK_CLEANUP_SENT
            assert controller.status().closeout_requested
        finally:
            driver.close()


def test_stop_flag_preserves_inflight_phase_until_native_save() -> None:
    with tempfile.TemporaryDirectory() as directory:
        driver = make_test_driver(RecordingBackend(), lease_path=Path(directory) / "desktop.lock")
        try:
            controller = Controller(Path(directory) / "session.json", driver=driver)
            controller.start()
            controller.native_calibrated(native_calibration())
            controller.input_calibrated(evidence=input_calibration())
            controller.reconcile(identity_ok=True)
            controller.begin_scan(started_at=100)
            assert controller.stop().state == ControllerState.SCANNING
            assert controller.saved("uid-after-stop").state == ControllerState.STOPPED
        finally:
            driver.close()


def test_pause_revokes_driver_and_ipc_rejects_stale_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        backend = RecordingBackend()
        driver = DesktopDriver(backend=backend, lease_path=Path(directory) / "desktop.lock")
        try:
            controller = Controller(Path(directory) / "session.json", driver=driver)
            controller.start()
            generation = controller.status().generation
            controller.pause()
            response = SessionIPC(controller).dispatch({"command": "stop", "generation": generation})
            assert response == {"ok": False, "error": "stale generation"}
            assert not controller.driver.guardian.armed
            assert not backend.events
        finally:
            driver.close()


def test_unix_socket_controls_the_same_durable_controller() -> None:
    with tempfile.TemporaryDirectory() as directory:
        backend = RecordingBackend()
        driver = DesktopDriver(backend=backend, lease_path=Path(directory) / "desktop.lock")
        try:
            controller = Controller(Path(directory) / "session.json", driver=driver)
            socket_path = Path(directory) / "session.sock"
            thread = threading.Thread(target=serve, args=(socket_path, controller), daemon=True)
            thread.start()
            for _ in range(20):
                if socket_path.exists():
                    break
                time.sleep(0.01)
            started = _socket_request(socket_path, {"command": "start", "generation": 0})
            assert started["status"]["state"] == "NATIVE_CALIBRATION"
            paused = _socket_request(
                socket_path,
                {"command": "pause", "generation": started["status"]["generation"]},
            )
            assert paused["status"]["state"] == "PAUSED"
            assert controller.status().state == ControllerState.PAUSED
        finally:
            driver.close()


def test_socket_exposes_only_owned_calibration_workflows() -> None:
    with tempfile.TemporaryDirectory() as directory:
        driver = DesktopDriver(backend=RecordingBackend(), lease_path=Path(directory) / "desktop.lock")
        try:
            controller = Controller(Path(directory) / "session.json", driver=driver)
            started = controller.start()
            response = SessionIPC(controller).dispatch(
                {"command": "native_calibrated", "generation": started.generation, "measurements": {}}
            )
            assert response == {"ok": False, "error": "unknown command"}
        finally:
            driver.close()


def test_paused_restart_requires_fresh_live_input_evidence(tmp_path: Path) -> None:
    driver = make_test_driver(RecordingBackend(), lease_path=tmp_path / "desktop.lock")
    journal = tmp_path / "session.json"
    controller = Controller(journal, driver=driver)
    controller.start()
    controller.native_calibrated(native_calibration())
    controller.input_calibrated(evidence=input_calibration())
    controller.reconcile(identity_ok=True)
    controller.begin_scan(started_at=100)
    controller.pause("operator takeover")
    driver.close()

    fresh_driver = make_test_driver(RecordingBackend(), lease_path=tmp_path / "desktop-fresh.lock")
    restarted = Controller(journal, driver=fresh_driver)
    assert restarted.status().state == ControllerState.PAUSED
    resumed = restarted.resume()
    assert resumed.state == ControllerState.RECONCILE
    reconcile_fence = restarted.observation_fence()
    resumed = restarted.reconcile_restart(
        observed_boot="fresh-boot",
        scanner_phase="ready",
        scanner_idle=True,
        stable_scan_ids=set(),
        stable_sweep_ids=set(),
        stable_scan_verified=True,
        stable_files_complete=True,
        observed_scene="world",
        expected_generation=reconcile_fence.generation,
        expected_state=ControllerState.RECONCILE,
        fence=reconcile_fence,
    )
    assert resumed.state == ControllerState.NATIVE_CALIBRATION
    assert not fresh_driver.guardian.armed
    restarted.native_calibrated(native_calibration(process_start="fresh-process-start"))
    assert restarted.status().state == ControllerState.INPUT_CALIBRATION
    restarted.input_calibrated(evidence=input_calibration(game_identity="fresh-game-identity"))
    assert fresh_driver.guardian.armed
    assert fresh_driver._input_evidence.game_identity_fingerprint == "fresh-game-identity"
    fresh_driver.close()


def test_same_process_pause_resume_retains_live_calibration(tmp_path: Path) -> None:
    driver = make_test_driver(RecordingBackend(), lease_path=tmp_path / "desktop.lock")
    controller = Controller(tmp_path / "session.json", driver=driver)
    controller.start()
    controller.native_calibrated(native_calibration())
    controller.input_calibrated(evidence=input_calibration())
    controller.reconcile(identity_ok=True)
    controller.pause("operator takeover")

    resumed = controller.resume()

    assert resumed.state == ControllerState.RECONCILE
    assert driver.guardian.armed
    driver.close()


def test_model_boundary_contains_exact_disables_and_rejects_tool_events() -> None:
    command = LunaAdapter.command(Path("schema.json"), Path("screen.png"), FAKE_MODEL_DIR)
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert "--model" in command and command[command.index("--model") + 1] == "gpt-6-luna"
    with pytest.raises(ModelError):
        LunaAdapter._parse('{"type":"tool_call"}')
    output = (
        '{"type":"thread.started","thread_id":"thread-test"}\n'
        '{"type":"item.completed","item":{"id":"diag-1","type":"error","message":"'
        "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
        'enable `features.code_mode_host` and install `codex-code-mode-host`."}}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"answer-1","type":"agent_message","text":"'
        '{\\"action\\":\\"reconnect_game\\",\\"target\\":\\"game-client\\"}"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}'
    )
    assert LunaAdapter._parse(output).action == "reconnect_game"


def test_model_command_appends_prompt_without_orphan_mcp_override() -> None:
    prompt = "bounded prompt regression"
    command = LunaAdapter.command(Path("schema.json"), Path("screen.png"), FAKE_MODEL_DIR, prompt=prompt)

    assert command[-1] == prompt
    assert "mcp_servers.openaiDeveloperDocs.enabled=false" not in command


def _write_fake_model_cli(path: Path, *, stdout: str, stderr: str) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli 0.162.1')\n"
        "else:\n"
        f"    sys.stdout.write({stdout!r})\n"
        f"    sys.stderr.write({stderr!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _valid_fake_model_output(answer: object) -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-test"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "answer-1",
                "type": "agent_message",
                "text": json.dumps(answer, separators=(",", ":")),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
    ]
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events)


def _run_fake_model_cli(tmp_path: Path, *, stdout: str, stderr: str) -> str:
    neutral_dir = tmp_path / "neutral"
    neutral_dir.mkdir()
    adapter = LunaAdapter(
        neutral_dir=neutral_dir,
        codex_path=_write_fake_model_cli(tmp_path / "codex", stdout=stdout, stderr=stderr),
    )
    return adapter._run_cli(
        tmp_path / "schema.json",
        tmp_path / "screen.png",
        prompt="fake child boundary test",
    )


def test_model_cli_accepts_only_the_exact_stdin_notice_from_real_child(tmp_path: Path) -> None:
    output = _run_fake_model_cli(
        tmp_path,
        stdout=_valid_fake_model_output({"action": "reconnect_game", "target": "game-client"}),
        stderr="Reading additional input from stdin...\n",
    )

    assert LunaAdapter._parse(output).action == "reconnect_game"


@pytest.mark.parametrize(
    "stderr",
    (
        "unknown startup diagnostic\n",
        "Reading additional input from stdin...\nauth failure\n",
        "Reading additional input from stdin....\n",
        "Reading additional input from stdin...\nReading additional input from stdin...\n",
    ),
    ids=("unknown", "mixed-auth", "near-miss", "repeated"),
)
def test_model_cli_rejects_unclassified_stderr_from_real_child(tmp_path: Path, stderr: str) -> None:
    with pytest.raises(ModelError, match="unexpected stderr"):
        _run_fake_model_cli(
            tmp_path,
            stdout=_valid_fake_model_output({"action": "reconnect_game", "target": "game-client"}),
            stderr=stderr,
        )


@pytest.mark.parametrize(
    "answer_text",
    (
        "not-json",
        json.dumps({"type": "tool_call"}, separators=(",", ":")),
    ),
    ids=("malformed", "tool"),
)
def test_model_cli_still_rejects_malformed_or_tool_output_after_notice(
    tmp_path: Path, answer_text: str
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-test"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "answer-1", "type": "agent_message", "text": answer_text},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
            },
        },
    ]
    output = _run_fake_model_cli(
        tmp_path,
        stdout="\n".join(json.dumps(event, separators=(",", ":")) for event in events),
        stderr="Reading additional input from stdin...\n",
    )

    with pytest.raises(ModelError):
        LunaAdapter._parse(output)


def test_model_observation_boundary_requires_named_visual_facts() -> None:
    output = (
        '{"type":"thread.started","thread_id":"thread-test"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"answer-1","type":"agent_message","text":"'
        '{\\"state\\":\\"character_select\\",\\"facts\\":{'
        '\\"scene\\":\\"character_select\\",\\"character\\":\\"Arthur\\",'
        '\\"realm\\":\\"Classic Beta PvE 2\\",\\"modal\\":\\"character_select\\",'
        '\\"code\\":\\"\\",\\"control\\":\\"character_list\\",'
        '\\"idle\\":\\"true\\"},\\"anchor\\":{'
        '\\"x\\":10,\\"y\\":20,\\"width\\":80,\\"height\\":30}}"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}'
    )
    observation = LunaAdapter._parse_observation(output)
    assert observation.state == "character_select"
    assert observation.facts["realm"] == "Classic Beta PvE 2"
    with pytest.raises(ModelError):
        LunaAdapter._parse_observation(
            '{"type":"agent_message","text":"{\\"state\\":\\"world\\",'
            '\\"facts\\":{\\"scene\\":\\"world\\"}}"}'
        )


def test_model_boundary_rejects_recursive_nonobject_types_and_late_diagnostics() -> None:
    valid_answer = json.dumps({"action": "reconnect_game", "target": "game-client"}, separators=(",", ":"))

    for malformed in (
        "[]",
        '{"type":[]}',
        '{"type":{}}',
        '{"type":"item.completed","item":[]}',
        '{"type":"item.completed","item":{"id":"answer-1","type":{}}}',
    ):
        with pytest.raises(ModelError):
            LunaAdapter._parse(
                "\n".join(
                    [
                        '{"type":"thread.started","thread_id":"thread-test"}',
                        malformed,
                    ]
                )
            )

    diagnostic = (
        '{"type":"item.completed","item":{"id":"diag-1","type":"error","message":"'
        "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
        'enable `features.code_mode_host` and install `codex-code-mode-host`."}}'
    )
    answer = (
        '{"type":"item.completed","item":{"id":"answer-1","type":"agent_message","text":"'
        + valid_answer.replace('"', '\\"')
        + '"}}'
    )
    with pytest.raises(ModelError):
        LunaAdapter._parse(
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-test"}',
                    '{"type":"turn.started"}',
                    diagnostic,
                    answer,
                    '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
                ]
            )
        )


def test_model_boundary_requires_nonblank_reasoning_and_bounded_usage() -> None:
    answer = '{"action":"reconnect_game","target":"game-client"}'
    reasoning = '{"type":"item.completed","item":{"id":"reason-1","type":"reasoning","text":"   "}}'
    message = (
        '{"type":"item.completed","item":{"id":"answer-1","type":"agent_message","text":"'
        + answer.replace('"', '\\"')
        + '"}}'
    )
    usage = {
        "input_tokens": 2**63 - 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }
    with pytest.raises(ModelError):
        LunaAdapter._parse(
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-test"}',
                    '{"type":"turn.started"}',
                    reasoning,
                    message,
                    json.dumps({"type": "turn.completed", "usage": usage}, separators=(",", ":")),
                ]
            )
        )
    parsed = LunaAdapter._parse(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-test"}',
                '{"type":"turn.started"}',
                message,
                json.dumps({"type": "turn.completed", "usage": usage}, separators=(",", ":")),
            ]
        )
    )
    assert parsed.action == "reconnect_game"


def test_model_boundary_rejects_non_scalar_decision_and_observation_enums() -> None:
    usage = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }

    def stream(answer: object) -> str:
        message = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "answer-1",
                    "type": "agent_message",
                    "text": json.dumps(answer, separators=(",", ":")),
                },
            },
            separators=(",", ":"),
        )
        return "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-test"}',
                '{"type":"turn.started"}',
                message,
                json.dumps({"type": "turn.completed", "usage": usage}, separators=(",", ":")),
            ]
        )

    for action in ([], {}, 1):
        with pytest.raises(ModelError):
            LunaAdapter._parse(stream({"action": action, "target": "game-client"}))

    facts = {
        "scene": "character_select",
        "character": "Arthur",
        "realm": "Classic Beta PvE 2",
        "modal": "character_select",
        "code": "",
        "control": "character_list",
        "idle": "true",
    }
    anchor = {"x": 10, "y": 20, "width": 80, "height": 30}
    for state in ([], {}, 1):
        with pytest.raises(ModelError):
            LunaAdapter._parse_observation(stream({"state": state, "facts": facts, "anchor": anchor}))


def test_xi2_calibration_requires_real_sources_and_forwarded_correlation(tmp_path: Path) -> None:
    trace = "    sourceid: 4\n    sourceid: 5\n    sourceid: 7\n"
    with pytest.raises(DriverError, match="synthetic sourceid fixture"):
        XI2Observer.sources(trace)
    driver = DesktopDriver(backend=RecordingBackend(), lease_path=tmp_path / "desktop.lock")
    driver.close()


def test_driver_requires_an_explicit_runtime_lease_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROWLEY_DESKTOP_LEASE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    with pytest.raises(DriverError, match="XDG_RUNTIME_DIR or CROWLEY_DESKTOP_LEASE"):
        DesktopDriver(backend=RecordingBackend())


def test_native_pointer_action_requires_a_seeded_planned_gesture(tmp_path: Path) -> None:
    from crowley.session.driver import DriverProfile, X11Backend

    profile = DriverProfile(
        display=":test",
        xauthority=str(FAKE_XAUTHORITY),
        wine_prefix=str(FAKE_WINE_PREFIX),
        action_anchors={},
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    backend._run = lambda args, timeout=5.0: type("Result", (), {"stdout": "", "returncode": 0})()  # type: ignore[method-assign]
    with pytest.raises(DriverError, match="validated planned gesture"):
        backend.action("logout", generation=1)
