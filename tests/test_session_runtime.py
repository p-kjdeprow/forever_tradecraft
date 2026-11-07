"""End-to-end fake-clock session lifecycle through the real socket dispatcher."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image

from crowley.core.luawriter import render_assignment
from crowley.core.scanformat import encode_json_payload
from crowley.core.watchlist import load_catalog
from crowley.session import Controller, ControllerState, DesktopDriver, SessionConfig, SessionDaemon
from crowley.session import pointer_execution as pointer_execution_module
from crowley.session.calibration import (
    LinuxProcStatusVmHwmReset5,
    NativeCalibrationRunner,
    NativeMemoryMeasurement,
    NativeMemoryResetRecord,
    OwnedWindow,
    XTestInputProfile,
)
from crowley.session.cli import SessionIPC, _socket_request, serve
from crowley.session.daemon import _NativeObservationDiagnostic
from crowley.session.decoder import PixelDecoder, Sample
from crowley.session.driver import (
    DriverError,
    DriverProfile,
    WindowIdentity,
    X11Backend,
    _proc_start,
)
from crowley.session.evidence import EvidenceError, read_stable_saved_scan
from crowley.session.input_events import EventClass, XI2Event
from crowley.session.journal import DEFAULT_STATE, Journal, JournalError
from crowley.session.model import ModelDecision, ModelObservation
from crowley.session.protocol import decode_frame
from crowley.session.source_binding import SAFETY_MODULES, manifest_digest
from crowley.session.startup import (
    InvalidationCause,
    StartupEnvironment,
    TakeoverDrillRecord,
    mint_runtime_evidence,
)
from tests.support.companion_state import write_companion_state
from tests.test_session_input_tool import fixture_input_tool

from .wow_harness import Auction, WowClient

_REVERSE_BITS = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


@pytest.fixture(autouse=True)
def _private_pointer_click_rate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real click gate isolated from host-global state in lifecycle tests."""

    state_directory = tmp_path / "pointer-click-rate"
    state_directory.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution_module, "_session_state_directory", lambda: state_directory)


class _FixtureNativeMemoryMeter(LinuxProcStatusVmHwmReset5):
    """Deterministic reset5 contract for the headless daemon lifecycle."""

    def __init__(self, identity: WindowIdentity, *, monotonic: Callable[[], float] | None = None) -> None:
        self.pid = identity.pid
        self.process_start = identity.process_start
        self.process_executable = identity.executable
        self.identity_fingerprint = identity.fingerprint
        self.expected_uid = os.getuid()
        self.kernel_release = os.uname().release
        self._monotonic = monotonic or time.monotonic
        self._pending = None

    def begin_window(self, phase: str, ordinal: int, *, limit_bytes: int, before_reset):
        if self._pending is not None:
            raise AssertionError("fixture reset5 window was not finished")
        record = NativeMemoryResetRecord(
            method=self.METHOD,
            phase=phase,
            ordinal=ordinal,
            pid=self.pid,
            process_start=self.process_start,
            process_executable=self.process_executable,
            identity_fingerprint=self.identity_fingerprint,
            uid=self.expected_uid,
            kernel_release=self.kernel_release,
            historical_hwm_bytes=0,
            rss_before_reset_bytes=0,
            limit_bytes=limit_bytes,
        )
        before_reset(record)
        self._pending = SimpleNamespace(
            meter=self,
            method=self.METHOD,
            phase=phase,
            ordinal=ordinal,
            pid=self.pid,
            process_start=self.process_start,
            process_executable=self.process_executable,
            identity_fingerprint=self.identity_fingerprint,
            uid=self.expected_uid,
            kernel_release=self.kernel_release,
            historical_hwm_bytes=0,
            rss_before_reset_bytes=0,
            reset_completed_monotonic=self._monotonic(),
            post_reset_hwm_bytes=0,
            rss_after_reset_bytes=0,
            limit_bytes=limit_bytes,
        )
        return self._pending

    def finish_window(
        self,
        pending,
        *,
        workload_start_monotonic: float,
        workload_end_monotonic: float,
        workload_start_ordinal: int,
        workload_end_ordinal: int,
    ) -> NativeMemoryMeasurement:
        if pending is not self._pending:
            raise AssertionError("fixture reset5 token was replayed")
        self._pending = None
        return NativeMemoryMeasurement(
            method=self.METHOD,
            phase=pending.phase,
            ordinal=pending.ordinal,
            pid=self.pid,
            process_start=self.process_start,
            process_executable=self.process_executable,
            identity_fingerprint=self.identity_fingerprint,
            uid=self.expected_uid,
            kernel_release=self.kernel_release,
            historical_hwm_bytes=0,
            rss_before_reset_bytes=0,
            reset_completed_monotonic=pending.reset_completed_monotonic,
            post_reset_hwm_bytes=0,
            rss_after_reset_bytes=0,
            workload_start_monotonic=workload_start_monotonic,
            workload_end_monotonic=workload_end_monotonic,
            workload_start_ordinal=workload_start_ordinal,
            workload_end_ordinal=workload_end_ordinal,
            final_hwm_bytes=0,
            final_rss_bytes=0,
            growth_bytes=0,
            limit_bytes=pending.limit_bytes,
        )


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


def _snapshot(wall: int, *, boot: str = "boot-1", sequence: int = 1) -> dict:
    return {
        "schema": 6,
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
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
            "zone": "",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
        },
        "house": {"key": "beta-normal-alliance", "source": "auto"},
        "plan": {
            "hash": load_catalog().scan_plan()["planHash"],
            "count": 428,
            "cadenceSeconds": 900,
            "sweepCapSeconds": 600,
            "initialPriceLevels": 20,
        },
        "ahOpen": True,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": _executor_none(),
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
            "dueAt": wall,
            "ordinal": 0,
            "planned": 428,
            "cursorItemID": 0,
            "scheduleFence": {"state": "inactive", "reason": "", "command": "/crowley session fence"},
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
        "chatEdit": {"active": True, "focused": True, "text": ""},
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
                "available": False,
                "beforeKiB": 0,
                "afterKiB": 0,
                "netKiB": 0,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        },
    }


def _saved_fixture(path: Path, uid: str, started: int) -> None:
    plan = load_catalog().scan_plan()
    observations: dict[str, dict] = {}
    for ordinal, item_id in enumerate(plan["itemIDs"], 1):
        observation = {
            "itemID": item_id,
            "ordinal": ordinal,
            "kind": "commodity",
            "query": {"kind": "queried", "startedAt": started, "finishedAt": started + 1},
            "coverage": "full",
            "sourceComplete": True,
            "cutoff": {"kind": "unbounded"},
            "higherPriceWitness": {"kind": "unknown"},
            "reasonCode": "ok",
            "rowCount": 1,
            "rows": {
                "row:1": {
                    "resultID": ordinal,
                    "itemID": item_id,
                    "itemKey": str(item_id),
                    "rawItemKey": {
                        "itemID": item_id,
                        "itemLevel": 0,
                        "itemSuffix": 0,
                        "battlePetSpeciesID": 0,
                        "bonusIDs": [],
                    },
                    "link": {"kind": "unknown"},
                    "quantity": 1,
                    "price": {"kind": "unit", "copper": 100 + ordinal},
                    "unitPrice": {"numerator": 100 + ordinal, "denominator": 1},
                    "owners": {
                        "names": [],
                        "totalNumberOfOwners": 0,
                        "containsOwnerItem": False,
                        "containsAccountItem": False,
                        "identityKind": "grouped",
                    },
                    "timeLeft": {"kind": "unknown"},
                }
            },
        }
        observations[str(item_id)] = observation
    first_owned_id = plan["itemIDs"][0]
    owned_row = {
        "auctionID": 1,
        "itemID": first_owned_id,
        "itemKey": str(first_owned_id),
        "rawItemKey": {
            "itemID": first_owned_id,
            "itemLevel": 0,
            "itemSuffix": 0,
            "battlePetSpeciesID": 0,
            "bonusIDs": [],
        },
        "itemLink": f"item:{first_owned_id}:0:0:0:0:0:0:0:0:0:0:0:0",
        "quantity": 1,
        "buyout": {"kind": "known", "copper": 1},
        "status": "active",
    }
    payload = {
        "planHash": plan["planHash"],
        "planJSON": plan["planJSON"],
        "observations": observations,
        "owned": {
            "status": "full",
            "query": {"kind": "queried", "startedAt": started, "finishedAt": started + 1},
            "reasonCode": "ok",
            "rowCount": 1,
            "rows": {"1": owned_row},
        },
    }
    envelope = {
        "id": uid,
        "format": 2,
        "startedAt": started,
        "finishedAt": started + 2,
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
            "zone": "",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
            "clientBuild": "70450",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "numRows": 428,
        "numObservations": 428,
        "payload": encode_json_payload(payload),
    }
    path.write_text(render_assignment("CrowleyDB", {"scans": {uid: envelope}}, "fixture"))


def _attach_recording_anchor_provider(driver: DesktopDriver, root: Path) -> None:
    """Supply one complete, sealed visual record immediately before each action."""

    scenes = {
        "dismiss_disconnect": "disconnect_dialog",
        "reconnect_game": "game_client",
        "select_arthur": "character_select",
        "enter_world": "character_select",
        "logout": "world",
    }
    state = {"generation": 0, "sequence": 0}

    def provider(identity: WindowIdentity, action: str):
        state["sequence"] += 1
        capture = root / f"anchor-{action}-{state['sequence']}.capture"
        capture.write_bytes(f"owned visual capture:{action}:{state['sequence']}".encode())
        captured_at = driver._clock()
        return driver.build_measured_anchor(
            action=action,
            x=80,
            y=60,
            capture_path=capture,
            capture_id=hashlib.sha256(capture.read_bytes()).hexdigest(),
            capture_sequence=state["sequence"],
            capture_monotonic=captured_at,
            scene=scenes[action],
            bounds=(40, 30, 80, 60),
            target_xid=identity.child_xid,
            target_ancestry=(identity.child_xid,),
            generation=state["generation"],
            facts=(("fixture", "owned-recording-backend"),),
        )

    driver.measured_anchor_provider = provider
    original_run = driver.run

    def run(name: str, generation: int, **kwargs: object) -> None:
        if name in scenes:
            state["generation"] = generation
            driver.measure_pointer_anchor(name, generation=generation)
        if name == "submit_command" and "precondition" not in kwargs:
            expected = driver.resolve_identity().fingerprint
            kwargs["precondition"] = lambda identity: identity.fingerprint == expected
        original_run(name, generation, **kwargs)

    driver.run = run  # type: ignore[method-assign]


class LifecycleBackend(X11Backend):
    def __init__(self, root: Path):
        super().__init__(
            DriverProfile(xauthority="/tmp/test-Xauthority", wine_prefix="/tmp/test-wine-prefix"),
            input_tool=fixture_input_tool(root / "tool"),
        )
        self.root = root
        self.events: list[dict[str, object]] = []
        self.capture_paths: list[Path] = []
        self.command_buffer = ""
        self.current = _snapshot(1000)
        self.captures = 0
        self.cycle = 0
        self.uid = ""
        self.boot_number = 1
        self.character_select = False
        self.saved_reload_done = False
        self.agent_client: WowClient | None = None
        self.lifecycle_agent = False
        self.lifecycle_sequence = 0
        self.agent_load_requested = False
        self.agent_commands: list[tuple[str, int]] = []
        self.agent_last_capture_sequence = 0
        self.agent_frame_sampling_callback: Callable[[], None] | None = None
        self.virtual_clock: object | None = None
        self.agent_trace: list[tuple[int, str, int, float]] = []
        self.runtime_observer: _FixtureInputObserver | None = None
        self.runtime_driver: DesktopDriver | None = None
        self.runtime_event_number = 1000
        self.runtime_timestamp = 1000
        self.identity = WindowIdentity(
            1,
            os.getpid(),
            "test",
            "World of Warcraft",
            "wowb.exe",
            2,
            (0, 0, 1928, 1092),
            (0, 0, 1920, 1080),
            1,
            "test",
            ":0",
            "test",
            0x1,
            1,
            frozenset({1, 2}),
        )
        self.pointer = (20, 20)

    def xtest_input_profile(self, hierarchy, **_kwargs):
        """Return the sealed native fixture profile without querying host X11."""

        return XTestInputProfile(
            4,
            2,
            "XTEST pointer",
            3,
            ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
            1,
            1,
            (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            0x374,
            (0, 0, 1920, 1080),
            tuple(hierarchy),
        )

    def _run(self, args, **_kwargs):
        del args
        raise DriverError("unmodeled native fixture command refused locally")

    def resolve_identity(
        self,
        *,
        require_foreground: bool = True,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        del require_foreground
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("fixture identity query exceeded its deadline")
        return self.identity

    def pointer_position(
        self,
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, int, int]:
        del generation
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("fixture pointer query exceeded its deadline")
        left, top, _width, _height = self.identity.child_geometry
        hit_xid = getattr(self, "calibration_target_xid", self.identity.child_xid)
        return left + self.pointer[0], top + self.pointer[1], hit_xid

    def verify_identity(self, identity: WindowIdentity) -> WindowIdentity:
        if identity.fingerprint != self.identity.fingerprint:
            raise DriverError("fixture identity changed")
        return self.identity

    def calibration_focus(self, window_id: str, **kwargs: object) -> None:
        self.events.append({"name": "calibration_focus", "window": window_id, **kwargs})

    def activate_generation(self, generation: int) -> None:
        self.events.append({"name": "activate_generation", "generation": generation})

    def observe_keymap(self, *, deadline_monotonic: float | None = None) -> dict[str, int]:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("fixture keymap query exceeded its deadline")
        return {
            "Return": 36,
            "Escape": 9,
            "Control_L": 37,
            "Shift_L": 50,
            "F12": 96,
            "slash": 61,
            "space": 65,
            "9": 18,
            "0": 19,
            "minus": 20,
            "equal": 21,
            "semicolon": 47,
            "apostrophe": 48,
            "comma": 59,
            **{character: index for index, character in enumerate("abcdefghijklmnopqrstuvwxyz", 10)},
        }

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("fixture capture exceeded its deadline")
        self.captures += 1
        self.capture_paths.append(path)
        self._load_real_agent()
        if self.lifecycle_agent and self.current["scanner"]["phase"] in {"item", "owned"} and not self.uid:
            self.uid = f"uid-{self.cycle}"
            self.current["lastSaved"] = {
                "memoryUid": self.uid,
                "finishedAt": self.current["wallTime"] + 2,
                "planned": 428,
                "attempted": 428,
                "failures": 0,
                "rows": 428,
                "persistence": "unknown",
            }
            _saved_fixture(self.root / "Crowley.lua", self.uid, self.current["scanner"]["startedAt"])
            write_companion_state(
                self.root / "companion.json", uploaded={self.uid: self.current["wallTime"] + 3}
            )
            data = {
                "generatedAt": self.current["wallTime"],
                "ackedScans": [self.uid],
                "scanPlan": load_catalog().scan_plan(),
                "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
                "shoppingLists": [],
            }
            (self.root / "Data.lua").write_text(render_assignment("CROWLEY_DATA", data, "fixture"))
        if self.agent_client is None:
            # Let bounded daemon waits progress while the real addon is
            # absent.  This is the same virtual monotonic source injected
            # into the runner, so the negative no-Agent interval cannot hang
            # forever behind a wall-clock sleep.
            advance = getattr(self.virtual_clock, "advance", None)
            if callable(advance) and not getattr(self, "suppress_fresh_capture_clock", False):
                advance(1.0)
            Image.new("RGB", (1920, 1080), (17, 17, 17)).save(path, compress_level=0)
            return self.identity
        if self.lifecycle_agent:
            self._publish_lifecycle_agent()
        else:
            self._service_agent_operator()
        sample_callback = self.agent_frame_sampling_callback
        if callable(sample_callback):
            sample_callback()
        raw = self.agent_client.agent_frame
        frame = decode_frame(raw, plan_item_ids=load_catalog().scan_plan()["itemIDs"])
        render = frame.snapshot["render"]
        previous_sequence = self.agent_last_capture_sequence
        # Agent's scheduled one-hertz callbacks are driven by the fixture's
        # explicit clock, but publication is scheduled only after a capture
        # completes.  The next daemon capture-start clock read consumes that
        # schedule, so capture timestamps and publication cadence share one
        # coherent namespace.
        if (
            frame.sequence == self.agent_last_capture_sequence
            and render["nativePhase"] == "updates"
            and render["nativeOrdinal"] < 100
        ):
            schedule = (
                getattr(self.virtual_clock, "schedule_native_publication", None)
                if getattr(self.virtual_clock, "native_schedule", False)
                else None
            )
            if callable(schedule):
                raise AssertionError("native publication was not consumed between captures")
            else:
                publisher = getattr(self, "native_publication_callback", None)
                if callable(publisher):
                    publisher()
                else:
                    advance = getattr(self.virtual_clock, "advance", None)
                    if not callable(advance):
                        raise AssertionError("runtime fixture requires a virtual clock for native cadence")
                    advance(1.0)
                    self.agent_client.advance_with_timers(1)
            raw = self.agent_client.agent_frame
            frame = decode_frame(raw, plan_item_ids=load_catalog().scan_plan()["itemIDs"])
        if self.lifecycle_agent:
            # The lifecycle producer is exercised against the daemon's
            # wall-clock visual path.  Keep the injected driver clock in that
            # same namespace while preserving the explicit virtual cadence
            # used by the native handshake above.
            self.virtual_clock.value = time.monotonic()
        elif frame.sequence != previous_sequence and not getattr(self, "suppress_fresh_capture_clock", False):
            advance = getattr(self.virtual_clock, "advance", None)
            if not callable(advance):
                raise AssertionError("runtime fixture requires a virtual clock for native timestamps")
            advance(0.01)
        self.agent_last_capture_sequence = frame.sequence
        now = getattr(self.virtual_clock, "value", time.monotonic())
        render = frame.snapshot["render"]
        self.agent_trace.append((frame.sequence, render["nativePhase"], render["nativeOrdinal"], float(now)))
        schedule = (
            getattr(self.virtual_clock, "schedule_native_publication", None)
            if getattr(self.virtual_clock, "native_schedule", False)
            else None
        )
        if callable(schedule) and render["nativePhase"] == "updates" and render["nativeOrdinal"] < 100:
            schedule(1.0, self.agent_client.advance_with_timers)
        # The protocol packs each row LSB-first while Pillow's mode-1 image
        # stores rows MSB-first.  Reverse each row, scale the actual
        # AgentFrameBytes cell surface, and paste it into the verified desktop
        # crop.  This keeps the fixture production-decoded without a Python
        # pixel loop on every one-hertz update.
        packed_cells = b"".join(raw[row * 16 : (row + 1) * 16].translate(_REVERSE_BITS) for row in range(128))
        cells = Image.frombytes("1", (128, 128), packed_cells)
        crop = cells.resize((256, 256), Image.Resampling.NEAREST).convert("RGB")
        image = Image.new("RGB", (1920, 1080), (0, 0, 0))
        image.paste(crop, (80, 48))
        image.save(path, compress_level=0)
        return self.identity

    def _load_real_agent(self) -> None:
        if not self.agent_load_requested or self.agent_client is not None:
            return
        self.agent_load_requested = False
        self.agent_client = WowClient(
            faction="Alliance",
            realm="Classic Beta PvE 2",
            zone="Stormwind City",
            current_region=110,
            current_region_name="beta",
        )
        self.agent_client.set_player_name("Arthur")
        self.agent_client.set_canonical_plan()
        self.agent_client.set_auctions(
            [
                Auction(itemID=item_id, name=f"item-{item_id}", count=1, buyout=101, isCommodity=True)
                for item_id in load_catalog().scan_plan()["itemIDs"]
            ]
        )
        self.agent_client.lua.globals()._now = self.current["wallTime"]
        self.agent_client.login()
        # Keep the first production Agent frame aligned with the backend's
        # initial world observation.  Assigning the addon-owned flag avoids
        # firing AUCTION_HOUSE_SHOW, whose production handler would also
        # schedule a real scanner run before the controller's command.
        self.agent_client.ns.ahOpen = True
        self.agent_client.set_chat_edit(active=True, focused=True)
        self.agent_client.publish_agent()

    def use_lifecycle_agent(self) -> None:
        """Switch the post-calibration lifecycle to production wire encoding."""

        client = WowClient(
            faction="Alliance",
            realm="Classic Beta PvE 2",
            zone="Stormwind City",
            current_region=110,
            current_region_name="beta",
        )
        client.set_player_name("Arthur")
        client.set_canonical_plan()
        client.login()
        client.set_chat_edit(active=True, focused=True)
        self.agent_client = client
        self.lifecycle_agent = True
        self.lifecycle_sequence = 0
        self.current["context"]["zone"] = "Stormwind City"
        self._publish_lifecycle_agent()

    def _publish_lifecycle_agent(self) -> None:
        if self.agent_client is None:
            return
        self.lifecycle_sequence += 1
        value = json.loads(json.dumps(self.current))
        value["sequence"] = self.lifecycle_sequence
        value["uptimeMs"] = self.lifecycle_sequence
        value["render"] = {
            "constructionSeconds": 0.001,
            "updateCount": 100,
            "updateP99Ms": 1,
            "textureCount": 16_384,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": False,
                "beforeKiB": 0,
                "afterKiB": 0,
                "netKiB": 0,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        }
        client = self.agent_client
        client.ns.Agent.Snapshot = lambda: client._to_lua(value)
        client.publish_agent()

    def _restart_real_agent(self) -> None:
        """Give the production Agent a new boot while retaining durable scan facts."""

        old = self.agent_client
        if old is None:
            return
        client = WowClient(
            faction="Alliance",
            realm="Classic Beta PvE 2",
            zone="Stormwind City",
            current_region=110,
            current_region_name="beta",
        )
        client.set_player_name("Arthur")
        client.set_canonical_plan()
        client.set_auctions(
            [
                Auction(itemID=item_id, name=f"item-{item_id}", count=1, buyout=101, isCommodity=True)
                for item_id in load_catalog().scan_plan()["itemIDs"]
            ]
        )
        client.lua.globals()._now = self.current["wallTime"]
        client.login()
        client.ns.ahOpen = bool(self.current["ahOpen"])
        client.set_chat_edit(active=True, focused=True)
        db = client.lua.globals().CrowleyDB
        db.agentLastSaved = client._to_lua(self.current["lastSaved"])
        scanner = self.current["scanner"]
        db.lastSweepStart = int(scanner.get("dueAt", self.current["wallTime"])) - 900
        if self.uid:
            db.scans[self.uid] = client._to_lua({"id": self.uid})
        client.lua.globals().CROWLEY_DATA.ackedScans = client._to_lua(self.current["loaded"]["ackedIds"])
        client.lua.globals().CROWLEY_DATA.generatedAt = self.current["loaded"].get("generatedAt", 0)
        client.publish_agent()
        self.agent_client = client

    def _service_agent_operator(self) -> None:
        if self.agent_client is None:
            return
        if not self.agent_commands:
            return
        command, remaining = self.agent_commands[0]
        if remaining > 0:
            self.agent_commands[0] = (command, remaining - 1)
            return
        self.agent_commands.pop(0)
        self.agent_client.slash(f"agent {command}")

    def load_agent(self) -> None:
        self.agent_load_requested = True

    def request_agent_command(self, command: str, *, delayed_captures: int = 3) -> None:
        self.agent_commands.append((command, delayed_captures))

    def _runtime_event(
        self,
        event_class: EventClass,
        *,
        master: int,
        source: int,
        detail: int,
        child: int | None = None,
        root_coordinates: tuple[float, float] | None = None,
        event_coordinates: tuple[float, float] | None = None,
        modifiers: tuple[int, int, int] | None = None,
        modifier_effective: int | None = None,
    ) -> XI2Event:
        event = _fixture_event(
            self.runtime_event_number,
            event_class,
            master,
            source,
            self.runtime_timestamp,
            detail,
            self.identity.child_xid if child is None and not event_class.name.startswith("RAW_") else child,
            root_xy=root_coordinates,
            event_xy=event_coordinates,
            modifiers=modifiers,
            modifier_effective=modifier_effective,
        )
        self.runtime_event_number += 1
        self.runtime_timestamp += 1
        return event

    def _emit_runtime_key_operation(self, operation: str, payload: str = "") -> None:
        observer = self.runtime_observer
        driver = self.runtime_driver
        evidence = getattr(driver, "_input_evidence", None)
        if observer is None or driver is None or evidence is None:
            raise AssertionError("runtime fixture has no owned observer/input evidence")
        for phase, _keysym, detail, modifiers, effective in driver._keyboard_operation_facts(
            operation, payload, evidence
        ):
            raw_class = EventClass.RAW_KEY_PRESS if phase == "press" else EventClass.RAW_KEY_RELEASE
            delivered_class = EventClass.KEY_PRESS if phase == "press" else EventClass.KEY_RELEASE
            observer._add(self._runtime_event(raw_class, master=3, source=5, detail=detail, child=None))
            observer._add(
                self._runtime_event(
                    delivered_class,
                    master=5,
                    source=5,
                    detail=detail,
                    child=self.identity.child_xid,
                    modifiers=tuple(modifiers),
                    modifier_effective=int(effective),
                )
            )

    def _emit_runtime_pointer_click(
        self, button: int, *, coordinates: tuple[tuple[float, float], tuple[float, float]]
    ) -> None:
        observer = self.runtime_observer
        if observer is None:
            raise AssertionError("runtime fixture has no owned observer")
        observer._add(
            self._runtime_event(EventClass.RAW_BUTTON_PRESS, master=2, source=4, detail=button, child=None)
        )
        observer._add(
            self._runtime_event(
                EventClass.BUTTON_PRESS,
                master=4,
                source=4,
                detail=button,
                child=self.identity.child_xid,
                root_coordinates=coordinates[0],
                event_coordinates=coordinates[1],
            )
        )
        observer._add(
            self._runtime_event(EventClass.RAW_BUTTON_RELEASE, master=2, source=4, detail=button, child=None)
        )
        observer._add(
            self._runtime_event(
                EventClass.BUTTON_RELEASE,
                master=4,
                source=4,
                detail=button,
                child=self.identity.child_xid,
                root_coordinates=coordinates[0],
                event_coordinates=coordinates[1],
            )
        )

    def _emit_runtime_pointer_gesture(self, gesture: object) -> None:
        observer = self.runtime_observer
        trajectory = getattr(gesture, "trajectory", None)
        samples = getattr(trajectory, "samples", None)
        if observer is None or not isinstance(samples, tuple) or len(samples) < 2:
            raise AssertionError("runtime fixture has no complete seeded pointer trajectory")
        left, top, _width, _height = self.identity.child_geometry
        previous = samples[0].point
        for sample in samples[1:]:
            point = sample.point
            if point == previous:
                continue
            x, y = int(point.x), int(point.y)
            observer._add(
                self._runtime_event(
                    EventClass.MOTION,
                    master=4,
                    source=4,
                    detail=0,
                    root_coordinates=(float(left + x), float(top + y)),
                    event_coordinates=(float(x), float(y)),
                )
            )
            previous = point
        endpoint = trajectory.endpoint
        self.pointer = int(endpoint.x), int(endpoint.y)

    def action(self, name: str, **kwargs) -> None:
        command = self.command_buffer if name == "submit_command" else ""
        self.events.append({"name": name, **kwargs})
        if name == "type_allowlisted_command":
            self._emit_runtime_key_operation("type_allowlisted_command", str(kwargs["command"]))
        elif name == "open_chat":
            self._emit_runtime_key_operation("open_chat")
        elif name == "submit_command":
            self._emit_runtime_key_operation("submit_command")
        elif name == "escape_command_buffer":
            self._emit_runtime_key_operation("escape_command_buffer")
        elif name == "open_ah":
            self._emit_runtime_key_operation("interact_target")
        if name == "type_allowlisted_command":
            self.command_buffer = str(kwargs["command"])
            self.current["chatEdit"]["text"] = self.command_buffer
            if self.agent_client is not None:
                self.agent_client.set_chat_edit(active=True, focused=True, text=self.command_buffer)
                if self.lifecycle_agent:
                    self._publish_lifecycle_agent()
                else:
                    self.agent_client.publish_agent()
        elif name == "submit_command":
            self.command_buffer = ""
            self.current["chatEdit"]["text"] = ""
            if command == DesktopDriver.TARGET_AUCTIONEER_COMMAND:
                self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
                self.current["auctioneer"].update(
                    targetPresent=True,
                    targetName="Auctioneer Jaxon",
                    rangeAvailable=True,
                    withinTradeRange=True,
                )
                if self.lifecycle_agent:
                    self._publish_lifecycle_agent()
            elif command == DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND:
                self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
                self.current["auctioneer"]["ctrlF12Action"] = "INTERACTTARGET"
                if self.lifecycle_agent:
                    self._publish_lifecycle_agent()
            elif command == "/crowley scan":
                self.cycle += 1
                started = self.current["wallTime"]
                self.uid = ""
                self.saved_reload_done = False
                self.current["scanner"] = {
                    "phase": "item",
                    "activeSweepId": "",
                    "startedAt": started,
                    "dueAt": started + 900,
                    "ordinal": 1,
                    "planned": 428,
                    "cursorItemID": load_catalog().scan_plan()["itemIDs"][0],
                    "scheduleFence": {"state": "inactive", "reason": "", "command": "/crowley session fence"},
                }
                if self.agent_client is not None and not self.lifecycle_agent:
                    self.agent_client.lua.globals()._now = started
                    self.agent_client.ns.ahOpen = True
                    self.agent_client.slash("scan")
                    self.agent_client.set_chat_edit(active=True, focused=True)
                    self.agent_client.publish_agent()
                elif self.lifecycle_agent:
                    self._publish_lifecycle_agent()
            elif command == "/crowley session fence":
                scanner = self.current["scanner"]
                if scanner["phase"] in {"ready", "scheduled"} and not scanner["activeSweepId"]:
                    scanner["scheduleFence"] = {
                        "state": "armed",
                        "reason": "",
                        "command": "/crowley session fence",
                    }
                if self.agent_client is not None and not self.lifecycle_agent:
                    self.agent_client.lua.globals()._now = self.current["wallTime"]
                    self.agent_client.slash("session fence")
                    self.agent_client.set_chat_edit(active=True, focused=True)
                    self.agent_client.publish_agent()
                elif self.lifecycle_agent:
                    self._publish_lifecycle_agent()
                else:
                    scanner["scheduleFence"] = {
                        "state": "refused",
                        "reason": "scan_active",
                        "command": "/crowley session fence",
                    }
            elif command == "/reload":
                self.boot_number += 1
                self.current["boot"] = f"boot-{self.boot_number}"
                # Advance the fake addon clock across the companion upload
                # before the controller records either publication witness.
                self.current["wallTime"] += 3
                if self.saved_reload_done and self.uid:
                    self.advance_ack()
                    self.current["ahOpen"] = False
                else:
                    self.saved_reload_done = True
                if self.agent_client is not None and not self.lifecycle_agent:
                    self._restart_real_agent()
                if self.agent_client is not None and not self.lifecycle_agent:
                    self.agent_client.ns.ahOpen = bool(self.current["ahOpen"])
                    self.agent_client.set_chat_edit(active=True, focused=True)
                    self.agent_client.publish_agent()
                elif self.lifecycle_agent:
                    self._publish_lifecycle_agent()
            elif command == "/logout":
                self.character_select = True
                self.current["context"]["character"] = ""
                self.current["context"]["realm"] = ""
                (self.root / "Crowley.lua").write_text(
                    render_assignment("CrowleyDB", {"scans": {}}, "logout flush")
                )
                empty_data = {
                    "generatedAt": self.current["wallTime"],
                    "ackedScans": [],
                    "scanPlan": load_catalog().scan_plan(),
                    "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
                    "shoppingLists": [],
                }
                (self.root / "Data.lua").write_text(
                    render_assignment("CROWLEY_DATA", empty_data, "logout flush")
                )
                if self.agent_client is not None and not self.lifecycle_agent:
                    self.agent_client.set_chat_edit(active=True, focused=True)
                    self.agent_client.publish_agent()
                elif self.lifecycle_agent:
                    self._publish_lifecycle_agent()
        elif name == "escape_command_buffer":
            self.command_buffer = ""
            self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
            if self.agent_client is not None:
                self.agent_client.set_chat_edit(active=False, focused=False, text="")
                if self.lifecycle_agent:
                    self._publish_lifecycle_agent()
                else:
                    self.agent_client.publish_agent()
        elif name == "open_chat":
            if self.agent_client is not None:
                self.current["chatEdit"] = {"active": True, "focused": True, "text": ""}
                self.agent_client.set_chat_edit(active=True, focused=True)
                if self.lifecycle_agent:
                    self._publish_lifecycle_agent()
                else:
                    self.agent_client.publish_agent()
        elif name == "open_ah":
            self.current["ahOpen"] = True
            self.current["wallTime"] = self.cycle * 900 + 1000
            self.current["scanner"] = {
                "phase": "ready",
                "activeSweepId": "",
                "startedAt": 0,
                "dueAt": self.current["wallTime"],
                "ordinal": 0,
                "planned": 428,
                "cursorItemID": 0,
                "scheduleFence": {"state": "inactive", "reason": "", "command": "/crowley session fence"},
            }
            if self.agent_client is not None and not self.lifecycle_agent:
                self.agent_client.ns.ahOpen = True
                self.agent_client.set_chat_edit(active=True, focused=True)
                self.agent_client.publish_agent()
            elif self.lifecycle_agent:
                self._publish_lifecycle_agent()

    def advance_ack(self) -> None:
        if self.uid:
            data = {
                "generatedAt": self.current["wallTime"],
                "ackedScans": [self.uid],
                "scanPlan": load_catalog().scan_plan(),
                "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
                "shoppingLists": [],
            }
            (self.root / "Data.lua").write_text(render_assignment("CROWLEY_DATA", data, "fixture"))
            if self.agent_client is not None:
                self.agent_client.lua.globals().CROWLEY_DATA.ackedScans = self.agent_client._to_lua(
                    [self.uid]
                )
                self.agent_client.lua.globals().CROWLEY_DATA.generatedAt = self.current["wallTime"]
            self.current["loaded"]["ackedIds"] = [self.uid]
            self.current["loaded"]["generatedAt"] = self.current["wallTime"]
            self.current["pending"]["ids"] = []
            self.current["pending"]["count"] = 0
            self.current["scanner"] = {
                "phase": "ready",
                "activeSweepId": "",
                "startedAt": 0,
                "dueAt": self.current["wallTime"] + 900,
                "ordinal": 0,
                "planned": 428,
                "cursorItemID": 0,
                "scheduleFence": {"state": "inactive", "reason": "", "command": "/crowley session fence"},
            }


def test_lifecycle_fixture_rejects_unmodeled_native_command_without_host_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lifecycle fixture must remain closed even if production gains a new query."""

    def host_dispatch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unmodeled native fixture command reached host dispatch")

    monkeypatch.setattr(X11Backend, "_run", host_dispatch)
    backend = LifecycleBackend(tmp_path)
    with pytest.raises(DriverError, match="unmodeled native fixture command"):
        backend._run(["xinput", "list", "--future-command"])


class _NativeProviderBackend(LifecycleBackend):
    """Real Agent producer with controls for one bounded provider wait."""

    def __init__(
        self,
        root: Path,
        *,
        load_after_captures: int | None = 0,
        publication_fault: str | None = None,
    ) -> None:
        super().__init__(root)
        self.load_after_captures = load_after_captures
        self.publication_fault = publication_fault
        self.capture_started = threading.Event()
        self.advance_cached_clock = False
        self.native_publication_callback: Callable[[], None] | None = None
        self.suppress_fresh_capture_clock = False

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        if (
            self.agent_client is None
            and self.load_after_captures is not None
            and self.captures >= self.load_after_captures
        ):
            self.load_agent()
        self.capture_started.set()
        observed = super().capture(
            path,
            identity=identity,
            deadline_monotonic=deadline_monotonic,
        )
        if self.advance_cached_clock and self.agent_client is not None:
            advance = getattr(self.virtual_clock, "advance", None)
            if not callable(advance):
                raise AssertionError("native provider fixture requires a virtual clock")
            advance(1.0)
        return observed

    def _service_agent_operator(self) -> None:
        if self.agent_client is not None and self.publication_fault is not None:
            fault, self.publication_fault = self.publication_fault, None
            if fault == "wrong_phase":
                # A real operator transition publishes held/0 while construct
                # is still waiting for the required normal/0 frame.
                self.agent_client.slash("agent hold")
            elif fault == "skipped_ordinal":
                # Publish actual Agent bytes at updates/3, skipping the
                # requested updates/1 frame.
                self.agent_client.slash("agent hold")
                self.agent_client.slash("agent measure")
                self.agent_client.advance_with_timers(2)
            else:
                raise AssertionError(f"unknown native provider fixture fault {fault!r}")
        super()._service_agent_operator()


class _VirtualMonotonic:
    def __init__(self) -> None:
        self.value = time.monotonic()
        self.native_schedule = False
        self._pending_native_publication: tuple[float, object] | None = None

    def monotonic(self) -> float:
        if self.native_schedule and self._pending_native_publication is not None:
            seconds, callback = self._pending_native_publication
            self._pending_native_publication = None
            self.value += seconds
            if not callable(callback):
                raise AssertionError("native publication callback is not callable")
            callback(1)
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def schedule_native_publication(self, seconds: float, callback: object) -> None:
        if not self.native_schedule:
            raise AssertionError("native publication scheduling is not enabled")
        if self._pending_native_publication is not None:
            raise AssertionError("native publication schedule was not consumed")
        self._pending_native_publication = (seconds, callback)


def test_native_capture_start_uses_driver_clock_domain(tmp_path: Path) -> None:
    """Native capture freshness remains comparable with the driver's clock."""

    virtual_clock = _VirtualMonotonic()
    virtual_clock.value = 123.0
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_clock = virtual_clock.monotonic
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    daemon.capture_index = 0
    daemon._native_owned_capture_paths = set()
    daemon._native_bootstrap_lock = threading.RLock()
    capture_path = tmp_path / "capture.png"
    capture_path.write_bytes(b"capture")
    daemon._capture_native_region_for_identity = lambda identity, **_kwargs: capture_path
    daemon._remove_native_capture = lambda path: None
    daemon.driver = SimpleNamespace(
        decode_native_capture=lambda path, *, identity, decoder: Sample(
            "FRESH", None, 999.0, (0, 0, 256, 256), 1, identity.fingerprint
        )
    )
    identity = SimpleNamespace(fingerprint="client-1")

    sample, _digest, retained, _receipt = daemon._capture_native_frame_with_capture(identity, object())

    assert sample.capture_monotonic == pytest.approx(123.0)
    assert retained.is_file()


def test_native_generic_capture_does_not_receive_private_receipt_sink(tmp_path: Path) -> None:
    """Legacy/custom capture callables keep their original closed keyword set."""

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_clock = lambda: 123.0
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    daemon.capture_index = 0
    daemon._native_owned_capture_paths = set()
    daemon._native_bootstrap_lock = threading.RLock()
    capture_path = tmp_path / "capture.png"
    capture_path.write_bytes(b"custom-capture")
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    prior_receipt = SimpleNamespace(
        nonce="prior-receipt",
        output_sha256="0" * 64,
        capture_started=122.0,
        attempt_retired=True,
    )
    fresh_receipt = SimpleNamespace(
        nonce="custom-receipt",
        output_sha256=digest,
        capture_started=122.5,
        attempt_retired=True,
    )
    backend = SimpleNamespace(last_region_receipt=prior_receipt)
    calls: list[object] = []

    def capture(
        _identity: object,
        *,
        deadline: float | None = None,
        expected_generation: int | None = None,
        expected_state: object | None = None,
        native_capability: object | None = None,
        native_update_ordinal: int | None = None,
    ) -> Path:
        del deadline, expected_generation, expected_state, native_capability, native_update_ordinal
        calls.append(_identity)
        backend.last_region_receipt = fresh_receipt
        return capture_path

    daemon._capture_native_region_for_identity = capture
    daemon.driver = SimpleNamespace(
        backend=backend,
        decode_native_capture=lambda path, *, identity, decoder: Sample(
            "FRESH", None, 123.4, (0, 0, 256, 256), 1, identity.fingerprint
        ),
    )
    daemon._remove_native_capture = lambda path: path.unlink(missing_ok=True)

    sample, _digest, _retained, receipt = daemon._capture_native_frame_with_capture(
        SimpleNamespace(fingerprint="client-1"), object()
    )

    assert sample.kind == "FRESH"
    assert calls
    assert receipt is backend.last_region_receipt


def test_native_provider_deadline_keeps_driver_fifteen_second_cap(tmp_path: Path) -> None:
    """A thirty-second Agent wait cannot extend the real producer bound."""

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_construction_lock = threading.RLock()
    daemon._native_construction_operation_deadline = 190.0
    daemon._native_construction_reservation = {"expiresMonotonic": 180.0}
    capability = SimpleNamespace(expires_monotonic=170.0)

    deadline = daemon._native_measurement_deadline(100.0, capability)  # type: ignore[arg-type]

    assert deadline == pytest.approx(115.0)
    assert daemon._native_measurement_deadline(
        100.0, capability, include_producer_limit=False
    ) == pytest.approx(130.0)
    assert daemon._native_measurement_deadline(100.0, capability, frame_deadline=130.0) == pytest.approx(
        115.0
    )
    daemon._native_construction_operation_deadline = 108.0
    assert daemon._native_measurement_deadline(100.0, capability) == pytest.approx(108.0)


@pytest.mark.parametrize("stale_digest", [True, False], ids=["same-digest", "mismatched-digest"])
def test_native_eligible_capture_rejects_stale_or_mismatched_receipt(
    tmp_path: Path, stale_digest: bool
) -> None:
    """An eligible update may only consume the receipt created by this capture."""

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_clock = lambda: 123.5
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    daemon.capture_index = 0
    daemon._native_owned_capture_paths = set()
    daemon._native_bootstrap_lock = threading.RLock()
    capture_path = tmp_path / "capture.png"
    capture_path.write_bytes(b"fresh-pixels")
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    backend = SimpleNamespace(
        last_region_receipt=SimpleNamespace(
            output_sha256=digest if stale_digest else "0" * 64,
            capture_started=123.0,
            attempt_retired=True,
        )
    )
    daemon.driver = SimpleNamespace(backend=backend)
    daemon._capture_native_region_for_identity = lambda identity, **_kwargs: capture_path
    daemon._remove_native_capture = lambda path: path.unlink(missing_ok=True)

    with pytest.raises(DriverError, match="fresh bound receipt"):
        daemon._capture_native_frame_with_capture(
            SimpleNamespace(fingerprint="client-1"),
            object(),
            native_capability=object(),
            native_update_ordinal=1,
        )
    assert not capture_path.exists()


def test_native_eligible_capture_uses_per_call_receipt_before_global_substitution(tmp_path: Path) -> None:
    """A same-digest later global receipt cannot replace the capture's receipt."""

    daemon = SessionDaemon.__new__(SessionDaemon)
    clock_values = iter((123.0, 123.5, 123.5, 123.5))
    daemon._native_clock = lambda: next(clock_values, 123.5)
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    daemon.capture_index = 0
    daemon._native_owned_capture_paths = set()
    daemon._native_bootstrap_lock = threading.RLock()
    capture_path = tmp_path / "capture.png"
    capture_path.write_bytes(b"fresh-pixels")
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    origin = SimpleNamespace(
        nonce="origin-receipt",
        capture_started=123.1,
        output_sha256=digest,
        attempt_retired=True,
    )
    substitute = SimpleNamespace(
        nonce="substitute-receipt",
        capture_started=123.2,
        output_sha256=digest,
        attempt_retired=True,
    )
    backend = SimpleNamespace(last_region_receipt=None)

    def capture(_identity: object, **kwargs: object) -> Path:
        sink = kwargs.get("_receipt_sink")
        assert callable(sink)
        sink(origin)
        backend.last_region_receipt = substitute
        return capture_path

    daemon.driver = SimpleNamespace(
        backend=backend,
        decode_native_capture=lambda path, *, identity, decoder: Sample(
            "FRESH", None, 123.4, (0, 0, 256, 256), 1, identity.fingerprint
        ),
    )
    daemon._capture_native_region_for_identity = capture
    daemon._remove_native_capture = lambda path: path.unlink(missing_ok=True)

    sample, _digest, _retained, receipt = daemon._capture_native_frame_with_capture(
        SimpleNamespace(fingerprint="client-1"),
        object(),
        native_capability=object(),
        native_update_ordinal=1,
    )

    assert sample.kind == "FRESH"
    assert receipt is origin
    assert receipt.nonce == "origin-receipt"
    assert backend.last_region_receipt is substitute


def test_native_update_alignment_keeps_capture_receipt_across_backend_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update anchor uses the receipt validated by its own capture."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-receipt-origin.lock",
    )
    producer = tmp_path / "region-producer"
    producer.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    producer.chmod(0o700)
    producer_sha256 = hashlib.sha256(producer.read_bytes()).hexdigest()
    profile = replace(
        driver.profile,
        telemetry_region_executable=producer,
        telemetry_region_executable_sha256=producer_sha256,
    )
    driver.profile = profile
    backend.profile = profile
    monkeypatch.setattr(daemon, "_native_region_enabled", lambda: True)
    identity = backend.identity
    try:
        _prime_native_update_budget_owner(daemon, driver, identity)
        provider = driver.native_measurement_provider
        capability = daemon._native_update_identity_budget_capability(provider, identity, "updates", 1)
        assert capability is not None
        token = daemon._native_update_alignment_enter(provider, identity)
        assert token is not None
        digest = "a" * 64
        origin = SimpleNamespace(
            nonce="origin-receipt",
            rectangle=(
                identity.child_geometry[0] + profile.canvas_crop[0],
                identity.child_geometry[1] + profile.canvas_crop[1],
                profile.canvas_crop[2],
                profile.canvas_crop[3],
            ),
            executable_sha256=producer_sha256,
            owner_pid=os.getpid(),
            owner_process_start=_proc_start(os.getpid()),
            capture_started=clock.value,
            output_sha256=digest,
            attempt_retired=True,
        )
        daemon._native_update_alignment_record(
            provider=provider,
            identity=identity,
            capability=capability,
            ordinal=1,
            capture_monotonic=clock.value,
            publisher_uptime_ms=1000,
            receipt=origin,
            capture_digest=digest,
            previous_uptime_ms=None,
        )
        backend.last_region_receipt = SimpleNamespace(
            **{**origin.__dict__, "nonce": "same-digest-substitute", "capture_started": clock.value}
        )
        with daemon._native_construction_lock:
            anchor = copy.deepcopy(daemon._native_update_alignment_anchor)
        assert anchor is not None
        assert anchor["lastReceiptNonce"] == "origin-receipt"
        assert anchor["lastCaptureDigest"] == digest
    finally:
        daemon._native_update_alignment_exit(token if "token" in locals() else None)
        daemon.request_shutdown()
        driver.close()


def test_native_provider_preserves_receipt_across_real_backend_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real eligible provider anchors the receipt handed off by its capture."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-real-receipt-origin.lock",
    )
    monkeypatch.setattr(daemon, "_native_region_enabled", lambda: True)
    daemon._native_provider_sleep_hook = clock.advance  # type: ignore[attr-defined]
    identity = backend.identity
    origin_receipts: list[Any] = []
    try:
        producer_sha256 = "a" * 64
        profile = replace(
            driver.profile,
            telemetry_region_executable_sha256=producer_sha256,
        )
        driver.profile = profile
        backend.profile = profile

        def capture_fixture(
            identity_value: WindowIdentity,
            _decoder: Any,
            *,
            native_capability: Any | None = None,
            **_kwargs: Any,
        ) -> tuple[Sample, str, Path, Any | None]:
            if backend.agent_client is None:
                backend.load_agent()
                backend._load_real_agent()
            backend._service_agent_operator()
            assert backend.agent_client is not None
            frame = decode_frame(
                backend.agent_client.agent_frame,
                plan_item_ids=load_catalog().scan_plan()["itemIDs"],
            )
            clock.advance(0.01)
            path = tmp_path / f"capture-{len(origin_receipts)}.png"
            path.write_bytes(f"capture-{len(origin_receipts)}".encode("ascii"))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            sample = Sample(
                "FRESH", frame, clock.value, (0, 0, 256, 256), frame.crc32, identity_value.fingerprint
            )
            if native_capability is None:
                backend.last_region_receipt = None
                return sample, digest, path, None
            receipt = SimpleNamespace(
                nonce=f"origin-{len(origin_receipts)}",
                rectangle=(80, 48, 256, 256),
                executable_sha256=producer_sha256,
                owner_pid=os.getpid(),
                owner_process_start=_proc_start(os.getpid()),
                capture_started=daemon._native_clock(),
                output_sha256=digest,
                attempt_retired=True,
            )
            origin_receipts.append(receipt)
            backend.last_region_receipt = SimpleNamespace(
                **{
                    **receipt.__dict__,
                    "nonce": "late-global-substitute",
                    "capture_started": float(receipt.capture_started) + 0.001,
                }
            )
            return sample, digest, path, receipt

        monkeypatch.setattr(daemon, "_capture_native_frame_with_capture", capture_fixture)
        constructed = daemon._native_measurement_provider(identity, "construct", 0)
        daemon.release_native_capture(Path(constructed.capture_path))
        backend.request_agent_command("hold", delayed_captures=0)
        held = daemon._native_measurement_provider(identity, "held", 0)
        daemon.release_native_capture(Path(held.capture_path))
        _prime_native_update_budget_owner(daemon, driver, identity)
        provider = driver.native_measurement_provider
        token = daemon._native_update_alignment_enter(provider, identity)
        assert token is not None
        backend.request_agent_command("measure", delayed_captures=0)
        update = daemon._native_measurement_provider(identity, "updates", 1)
        assert update.native_phase == "updates"
        assert update.native_ordinal == 1
        assert len(origin_receipts) == 1
        with daemon._native_construction_lock:
            anchor = copy.deepcopy(daemon._native_update_alignment_anchor)
        assert anchor is not None
        assert anchor["lastReceiptNonce"] == origin_receipts[0].nonce
        assert backend.last_region_receipt.nonce == "late-global-substitute"
        daemon.release_native_capture(Path(update.capture_path))
    finally:
        daemon._native_update_alignment_exit(token if "token" in locals() else None)
        daemon.request_shutdown()
        driver.close()


def test_native_alignment_rejects_unfit_target_before_sleep() -> None:
    """A target at the earliest deadline is refused before any wait chunk."""

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_construction_lock = threading.RLock()
    daemon._native_update_alignment_runner = {
        "identity": "client-1",
        "generation": 4,
        "epoch": 2,
    }
    daemon._native_update_alignment_anchor = {
        "nextOrdinal": 1,
        "runId": "run-1",
        "generation": 4,
        "identity": "client-1",
        "originCaptureStarted": 100.0,
        "originUptimeMs": 0,
        "lastUptimeMs": 0,
    }
    daemon._native_construction_operation_deadline = 100.5
    daemon._native_construction_reservation = {"expiresMonotonic": 100.5}
    daemon._native_clock = lambda: 100.0
    daemon._check_native_poll_identity = lambda *_args, **_kwargs: None
    identity = SimpleNamespace(fingerprint="client-1")
    capability = SimpleNamespace(
        epoch=2,
        run_id="run-1",
        generation=4,
        expires_monotonic=100.5,
    )

    daemon._native_alignment_sleep = lambda _seconds: (_ for _ in ()).throw(AssertionError("unexpected wait"))
    with pytest.raises(DriverError, match="cannot fit its deadline"):
        daemon._native_update_alignment_wait(
            identity=identity,
            generation=4,
            capability=capability,
            ordinal=1,
            deadline=100.5,
        )


def _runtime_daemon(
    root: Path,
    *,
    journal_changes: dict[str, object] | None = None,
    backend: LifecycleBackend | None = None,
    lease_path: Path | None = None,
    recover_on_restart: bool = False,
) -> tuple[SessionDaemon, LifecycleBackend, DesktopDriver, Controller, SessionConfig, _VirtualMonotonic]:
    """Build one real daemon consumer around the production fixture backend."""

    virtual_clock = _VirtualMonotonic()
    backend = backend or LifecycleBackend(root)
    backend.virtual_clock = virtual_clock
    effective_lease_path = lease_path or root / "desktop.lock"
    driver = DesktopDriver(
        profile=DriverProfile(xauthority="/tmp/test-Xauthority", wine_prefix="/tmp/test-wine-prefix"),
        backend=backend,
        monotonic=virtual_clock.monotonic,
        lease_path=effective_lease_path,
    )
    plan = load_catalog().scan_plan()
    config = SessionConfig(
        profile=driver.profile,
        journal=root / "session.json",
        socket=root / "session.sock",
        plan_hash=plan["planHash"],
        plan_json=plan["planJSON"],
        capture_dir=root / "captures",
        saved_variables=root / "Crowley.lua",
        companion_state=root / "companion.json",
        data_lua=root / "Data.lua",
    )
    value = copy.deepcopy(DEFAULT_STATE)
    for key, child in (journal_changes or {}).items():
        if isinstance(child, dict) and isinstance(value.get(key), dict):
            value[key].update(copy.deepcopy(child))
        else:
            value[key] = copy.deepcopy(child)
    journal = Journal(config.journal)
    journal.write(value)
    controller = Controller(
        journal,
        driver=driver,
        clock=virtual_clock.monotonic,
        wall_clock=lambda: backend.current["wallTime"],
        recover_on_restart=recover_on_restart,
    )
    with patch("crowley.session.daemon.LunaAdapter", _RuntimeFixtureModel):
        daemon = SessionDaemon(config, controller=controller, driver=driver)
    return daemon, backend, driver, controller, config, virtual_clock


def test_default_session_ipc_observes_real_unarmed_executor_frame(tmp_path: Path) -> None:
    """Compose Lua publication, daemon decoding, and the public IPC status path."""

    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(tmp_path)
    queue = {
        "format": 1,
        "queueId": "beta-normal-alliance-1760000000-ipc",
        "generatedAt": 1_759_990_000,
        "expiresAt": 1_760_000_100,
        "house": "beta-normal-alliance",
        "character": "Arthur",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 1}],
    }
    client = WowClient(
        faction="Alliance",
        realm="Classic Beta PvE 2",
        zone="Stormwind City",
        current_region=110,
        current_region_name="beta",
        queue=queue,
    )
    client.set_player_name("Arthur")
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    backend.agent_client = client
    backend.agent_load_requested = False
    try:
        sample = daemon.capture_status()
        assert sample.kind == "FRESH" and sample.frame is not None
        executor = sample.frame.snapshot["executor"]
        assert executor["availability"] == "valid"
        assert executor["publishedArmed"] is False
        assert executor["sessionArmed"] is False
        assert executor["clickReady"] is False

        status = SessionIPC(controller, daemon).dispatch({"command": "status"})
        assert status["ok"] is True
        assert client.protected_calls == []
        assert not any("/crowley arm" in message for message in client.chat)
        assert not any(event.get("name") in {"click", "type_allowlisted_command"} for event in backend.events)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_fresh_repeat_owner_automatically_passively_resumes_without_replaying_intent(
    tmp_path: Path,
) -> None:
    """The real default owner path resumes only a restart-created active repeat pause."""

    from tests.test_session_startup import _runtime_evidence

    _identity, environment, evidence = _runtime_evidence()
    drill = TakeoverDrillRecord.create(environment.fingerprint, 41, observed_at=1_800_000_000)
    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        recover_on_restart=True,
        journal_changes={
            "state": ControllerState.SCANNING.value,
            "mode": "repeat",
            "generation": 40,
            "nativeCalibration": True,
            "inputCalibration": True,
            "takeoverDrill": drill.to_json(),
            "current": {
                "pending": True,
                "uid": "uid-before-owner-restart",
                "startedAt": 1_800_000_001,
                "expectedBoot": "boot-before-owner-restart",
                "action": "scan",
                "actionAttempt": 9,
                "actionResult": "pending",
                "actionStartedAt": 1_800_000_002,
                "frameBoot": "boot-before-owner-restart",
                "frameSequence": 77,
                "frameCrc32": 123,
                "deadlineAt": 1_800_000_500,
                "evidence": {},
            },
        },
    )

    def passive_startup():
        daemon._startup_operation = {"state": "ready"}
        return evidence

    daemon.passive_startup = passive_startup
    assert controller._restart_startup_resume_pending, controller.journal.read()
    try:
        recovered = daemon.run_once()

        assert controller.status().state is ControllerState.RECONCILE
        assert recovered["state"] == ControllerState.RECONCILE.value
        assert controller.status().action_intent == "scan"
        assert controller.journal.read()["current"]["actionAttempt"] == 9
        assert controller.journal.read()["current"]["actionResult"] == "pending"
        assert controller.journal.read()["restartUncertain"] is True
        assert daemon._startup_operation["state"] == "ready"
        assert daemon._takeover_drill_operation["state"] == "idle"
        assert not driver.guardian.armed
        assert backend.events == []
        assert daemon._calibration_thread is None
    finally:
        daemon.request_shutdown()


def test_failed_automatic_restart_startup_persists_block_and_explicit_resume_recovers(
    tmp_path: Path,
) -> None:
    """One failed passive attempt is durable and an explicit fresh resume can recover."""

    from tests.test_session_startup import _runtime_evidence

    _identity, environment, evidence = _runtime_evidence()
    drill = TakeoverDrillRecord.create(environment.fingerprint, 41, observed_at=1_800_000_000)
    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        recover_on_restart=True,
        journal_changes={
            "state": ControllerState.SCANNING.value,
            "mode": "repeat",
            "generation": 40,
            "nativeCalibration": True,
            "inputCalibration": True,
            "takeoverDrill": drill.to_json(),
            "current": {
                "pending": True,
                "uid": "uid-before-failed-startup",
                "startedAt": 1_800_000_001,
                "expectedBoot": "boot-before-owner-restart",
                "action": "scan",
                "actionAttempt": 9,
                "actionResult": "pending",
                "actionStartedAt": 1_800_000_002,
                "frameBoot": "boot-before-owner-restart",
                "frameSequence": 77,
                "frameCrc32": 123,
                "deadlineAt": 1_800_000_500,
                "evidence": {},
            },
        },
    )

    class ObserverLost(DriverError):
        invalidation_cause = InvalidationCause.OBSERVER_LOST

    attempts = []

    def passive_startup():
        attempts.append("passive")
        if len(attempts) == 1:
            raise ObserverLost("registered XI2 observer died during passive startup")
        daemon._startup_operation = {"state": "ready", "advancingFrames": 3}
        return evidence

    daemon.passive_startup = passive_startup
    assert controller._restart_startup_resume_pending
    try:
        failed = daemon.run_once()
        journal = controller.journal.read()
        assert failed["state"] == ControllerState.BLOCKED.value
        assert controller.status().state is ControllerState.BLOCKED
        assert journal["runtimeReadinessInvalidation"] == InvalidationCause.OBSERVER_LOST.value
        assert "automatic restart startup failed" in journal["blocker"]
        assert journal["current"]["actionAttempt"] == 9
        assert journal["current"]["actionResult"] == "pending"
        assert backend.events == []
        assert not driver.guardian.armed

        resumed = daemon.runtime_start(expected_generation=controller.status().generation, resume=True)
        assert resumed["controllerState"] == ControllerState.RECONCILE.value
        assert controller.status().action_intent == "scan"
        assert controller.journal.read()["current"]["actionAttempt"] == 9
        assert controller.journal.read()["current"]["actionResult"] == "pending"
        assert attempts == ["passive", "passive"]
        assert backend.events == []
        assert not driver.guardian.armed
    finally:
        daemon.request_shutdown()


def test_restart_reconcile_rearms_fresh_lease_before_next_admitted_driver_action(tmp_path: Path) -> None:
    """Resolve one old named effect read-only, then admit only a new action."""

    backend = LifecycleBackend(tmp_path)
    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        recover_on_restart=True,
        journal_changes={
            "state": ControllerState.REOPEN.value,
            "mode": "repeat",
            "generation": 40,
            "current": {
                "uid": "",
                "pending": False,
                "action": "open_ah",
                "actionAttempt": 9,
                "actionResult": "dispatched",
                "actionStartedAt": 1_800_000_002,
                "frameBoot": "boot-before-owner-restart",
                "frameSequence": 77,
                "frameCrc32": 123,
            },
        },
    )

    hierarchy = (
        (2, 3, "master", "pointer:Virtual core pointer"),
        (3, 2, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (6, 2, "slave", "pointer:xwayland-pointer:17"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
        (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
        (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
    )
    keymap = backend.observe_keymap()
    # The ordinary lifecycle fixture exposes only the digits used by older
    # commands. Model the additional number-row keysyms used by the complete
    # default command profile without changing its shared keymap contract.
    keymap.update({str(digit): 100 + digit for digit in range(10) if str(digit) not in keymap})
    levels_by_code: dict[int, list[str]] = {}
    for symbol, code in keymap.items():
        levels_by_code.setdefault(code, []).append(symbol)
        if len(symbol) == 1 and symbol.isascii() and symbol.isalpha() and symbol.islower():
            levels_by_code[code].append(symbol.upper())
    levels = {code: tuple(sorted(set(symbols))) for code, symbols in levels_by_code.items()}
    modifier_masks = (1, 4)
    keyboard_profile = DesktopDriver._runtime_keyboard_profile(
        tuple(driver.COMMANDS), levels, keymap, (0, 0, 0), modifier_masks
    )
    xtest_profile = backend.xtest_input_profile(hierarchy)
    environment = StartupEnvironment(
        display=driver.profile.display,
        hierarchy=hierarchy,
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=xtest_profile,
        keymap_levels=tuple(sorted(levels.items())),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version=backend.input_tool.version,
        input_tool_sha256=backend.input_tool.digest,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-before-owner-restart",
        readiness_sequence=80,
        environment=environment,
        identity=backend.identity,
        keyboard_keymap=tuple(sorted(keymap.items())),
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=keyboard_profile,
    )
    record = TakeoverDrillRecord.create(environment.fingerprint, 18, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)

    def passive_startup():
        daemon._startup_evidence = evidence
        daemon._startup_operation = {"state": "ready", "advancingFrames": 3}
        start_runtime_observer()
        return evidence

    daemon.passive_startup = passive_startup
    backend.observe_keymap = lambda **_kwargs: keymap
    backend.observe_keymap_levels = lambda **_kwargs: levels
    backend.observe_native_input_state = lambda _root: ((0, 0, 0), (), ())
    driver.input_hierarchy = lambda **_kwargs: hierarchy

    class RuntimeObserver(_FixtureInputObserver):
        alive = True
        failure_diagnostic = None

        def wait_ready(self) -> None:
            return None

        def wait_for_event(self, predicate, *, timeout: float) -> None:
            del timeout
            if not predicate(self.events()):
                raise AssertionError("the actual admitted runtime event trace did not complete")

        def stop(self) -> str:
            self.alive = False
            return _fixture_event_trace(self.events())

    observer = RuntimeObserver(driver._external_input, [], [])

    def start_runtime_observer(**_kwargs):
        driver._monitor = observer
        driver._monitor_calibration = False
        backend.runtime_observer = observer
        backend.runtime_driver = driver
        return observer

    def stop_runtime_observer(_monitor=None, **_kwargs):
        observer.stop()
        driver._monitor = None
        driver._monitor_calibration = False

    driver.start_input_observer = start_runtime_observer  # type: ignore[method-assign]
    driver.stop_input_observer = stop_runtime_observer  # type: ignore[method-assign]

    try:
        automatic = daemon.run_once()
        assert automatic["state"] == ControllerState.RECONCILE.value
        assert automatic["actionIntent"] == "open_ah"
        assert controller.journal.read()["restartUncertain"] is True
        assert not driver.guardian.armed
        assert backend.events == []

        before_resolution = controller.status()
        resolved = controller.reconcile_restart(
            observed_scene="auction_house",
            observed_ah_open=True,
            action_postcondition=True,
            expected_generation=before_resolution.generation,
            expected_state=ControllerState.RECONCILE,
        )
        assert resolved.state is ControllerState.ARMED
        assert resolved.action_intent == ""
        assert controller.journal.read()["restartUncertain"] is False
        assert backend.events == []

        reconciled = controller.reconcile(
            identity_ok=True,
            world_ready=True,
            expected_generation=resolved.generation,
            expected_state=ControllerState.ARMED,
        )
        assert reconciled.state is ControllerState.WAIT_DUE
        assert driver.guardian.armed
        assert reconciled.lease_generation == driver.guardian.generation
        assert observer.alive

        driver.run(
            "open_chat",
            reconciled.lease_generation,
            precondition=lambda identity: identity.fingerprint == backend.identity.fingerprint,
            postcondition=lambda identity: identity.fingerprint == backend.identity.fingerprint,
        )
        assert driver.guardian.armed
        assert driver.guardian.generation == reconciled.lease_generation
        dispatched = [item["name"] for item in backend.events]
        assert dispatched == ["activate_generation", "open_chat"]
        assert "open_ah" not in dispatched
        assert controller.journal.read()["current"]["action"] == ""
        assert [event.event_class.value for event in observer.events()] == [
            EventClass.RAW_KEY_PRESS.value,
            EventClass.KEY_PRESS.value,
            EventClass.RAW_KEY_RELEASE.value,
            EventClass.KEY_RELEASE.value,
        ]
    finally:
        daemon.request_shutdown()


@pytest.mark.parametrize("held_state", [ControllerState.PAUSED, ControllerState.STOPPED])
def test_fresh_owner_keeps_persisted_human_hold_until_public_resume(
    tmp_path: Path, held_state: ControllerState
) -> None:
    """A pre-existing operator hold is not mistaken for a restart-created pause."""

    from tests.test_session_startup import _runtime_evidence

    _identity, environment, evidence = _runtime_evidence()
    drill = TakeoverDrillRecord.create(environment.fingerprint, 41, observed_at=1_800_000_000)
    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        recover_on_restart=True,
        journal_changes={
            "state": held_state.value,
            "mode": "repeat",
            "generation": 40,
            "takeoverDrill": drill.to_json(),
        },
    )

    def passive_startup():
        daemon._startup_operation = {"state": "ready"}
        return evidence

    daemon.passive_startup = passive_startup
    try:
        held = daemon.run_once()
        assert held["state"] == held_state.value
        assert daemon._startup_operation["state"] == "not_checked"
        assert not driver.guardian.armed
        assert backend.events == []

        # This is the real IPC dispatcher and the real daemon resume method;
        # only the physical arm is replaced because the fixture backend is not
        # a live X11 environment.
        def fixture_arm(_evidence):
            driver.guardian.generation += 1
            driver.guardian.armed = True
            return driver.guardian.generation

        driver.arm = fixture_arm
        ipc = SessionIPC(controller, daemon)
        result = ipc.dispatch({"command": "resume", "generation": controller.status().generation})
        assert result["ok"] is True
        assert result["status"]["controllerState"] == ControllerState.RECONCILE.value
        assert controller.status().state is ControllerState.RECONCILE
        assert daemon._startup_operation["state"] == "ready"
        assert not backend.events
    finally:
        daemon.request_shutdown()


def _fixture_event(
    number: int,
    event_class: EventClass,
    master: int,
    source: int,
    timestamp: int,
    detail: int | None,
    child: int | None,
    coordinates: tuple[float, float] | None = None,
    root_xy: tuple[float, float] | None = None,
    event_xy: tuple[float, float] | None = None,
    valuators: tuple[tuple[int, float, float | None], ...] = (),
    modifiers: tuple[int, int, int] | None = None,
    modifier_effective: int | None = None,
) -> XI2Event:
    delivered = event_class in {
        EventClass.KEY_PRESS,
        EventClass.KEY_RELEASE,
        EventClass.BUTTON_PRESS,
        EventClass.BUTTON_RELEASE,
    }
    has_modifiers = delivered or event_class is EventClass.MOTION
    modifier_values = modifiers if modifiers is not None else (0, 0, 0)
    effective_value = (
        modifier_effective
        if modifier_effective is not None
        else modifier_values[0] | modifier_values[1] | modifier_values[2]
    )
    if coordinates is None:
        coordinates = (1.0, 1.0) if child is not None else None
    if root_xy is None:
        root_xy = coordinates
    if event_xy is None:
        event_xy = coordinates
    return XI2Event(
        number,
        event_class,
        master,
        source,
        timestamp,
        detail,
        0x374 if child is not None else None,
        0x374 if child is not None else None,
        child,
        root_xy,
        event_xy,
        modifier_values if has_modifiers else None,
        (),
        effective_value if has_modifiers else None,
        valuators,
    )


def _fixture_event_trace(events: list[XI2Event]) -> str:
    blocks: list[str] = []
    for event in events:
        block = [
            f"EVENT type {event.type_number} ({event.event_class.value})",
            f"    device: {event.master_device} ({event.source_device})",
            f"    time: {event.timestamp_ms}",
        ]
        if event.detail is not None:
            block.append(f"    detail: {event.detail}")
        if event.is_raw or (event.event_class is EventClass.MOTION and event.source_device == 4):
            block.append("    valuators:")
            for axis, value, raw_value in event.valuators:
                suffix = f" ({raw_value:.2f})" if raw_value is not None else ""
                block.append(f"    {axis}: {value:.2f}{suffix}")
        if event.child_xid is not None:
            root_xid = event.root_xid if event.root_xid is not None else 0x374
            event_xid = event.event_xid if event.event_xid is not None else root_xid
            root_xy = event.root_xy or (1.0, 1.0)
            event_xy = event.event_xy or root_xy
            block.extend(
                (
                    f"    root: {root_xy[0]:.2f}/{root_xy[1]:.2f}",
                    f"    event: {event_xy[0]:.2f}/{event_xy[1]:.2f}",
                    "    modifiers: locked 0 latched 0 base 0 effective: 0",
                    f"    windows: root {hex(root_xid)} event {hex(event_xid)} child {hex(event.child_xid)}",
                )
            )
        blocks.append("\n".join(block))
    return "\n".join(blocks)


class _FixtureInputObserver:
    def __init__(self, callback: object, synthetic: list[XI2Event], human: list[XI2Event]):
        self.callback = callback
        self.synthetic = synthetic
        self.human = human
        self.alive = True
        self.failure_diagnostic = None
        self._events: list[XI2Event] = []

    def _add(self, event: XI2Event) -> None:
        self._events.append(event)
        self.callback(event)  # type: ignore[operator]

    def events(self) -> list[XI2Event]:
        return list(self._events)

    def collect_until(self, predicate: object, *, timeout: float) -> str:
        del timeout
        for event in self.synthetic:
            self._add(event)
        assert predicate(self.events())  # type: ignore[operator]
        return _fixture_event_trace(self.synthetic)

    def wait_for_human(self, predicate: object, *, timeout: float) -> None:
        del timeout
        for event in self.human:
            self._add(event)
        assert predicate(self.events())  # type: ignore[operator]

    def wait_for_event(self, predicate: object, *, timeout: float) -> None:
        del timeout
        assert predicate(self.events()), "the actual admitted runtime event trace did not complete"  # type: ignore[operator]

    def stop(self) -> str:
        self.alive = False
        return _fixture_event_trace(self._events)


class _FixtureInputTarget:
    last_created = None

    def __init__(self, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self.prompt_acknowledged = False
        type(self).last_created = self
        self.window = OwnedWindow(
            os.getpid(),
            0x600119,
            "CROWLEY INPUT CALIBRATION ONLY",
            (0, 0, 480, 240),
            object(),
            frozenset({0x600119}),
            "/usr/bin/python3",
            "fixture-target-start",
            0x600118,
            (0, 0, 500, 260),
            frozenset({0x600118, 0x600119}),
        )

    def create(self) -> OwnedWindow:
        return self.window

    def close(self) -> None:
        return None

    def show_human_prompt(self) -> None:
        # The real GTK child ACKs after label.set_text and the parent then
        # publishes readiness.  Keep that ordering observable in the
        # deterministic target instead of silently treating prompt() as a
        # readiness assertion.
        self.prompt_acknowledged = True
        self.trace.append("prompt_ack")


class _FixtureLunaAdapter:
    """Bounded visual/decision adapter for the headless lifecycle fixture."""

    def __init__(self, backend: LifecycleBackend) -> None:
        self.backend = backend

    def cancel(self) -> None:
        return None

    def shutdown(self) -> None:
        self.cancel()

    def decide(self, image: bytes, *, target: str, verified_crop: object) -> ModelDecision:
        del image, verified_crop
        assert target == "game-client"
        return ModelDecision("enter_world", target)

    def observe(
        self,
        image: bytes,
        *,
        target: str,
        verified_crop: object,
        requested_state: str | None = None,
        requested_control: str | None = None,
        timeout: float | None = None,
    ) -> ModelObservation:
        del image, verified_crop, requested_control, timeout
        assert target == "game-client"
        if requested_state is not None:
            state = requested_state
        elif self.backend.character_select:
            state = "character_select"
        elif self.backend.current["ahOpen"]:
            state = "auction_house_open"
        else:
            state = "world"
        facts = {
            "disconnect_dialog": {
                "scene": "disconnect_dialog",
                "character": "",
                "realm": "",
                "modal": "WOW51900319",
                "code": "WOW51900319",
                "control": "dismiss_disconnect",
                "idle": "false",
            },
            "game_client": {
                "scene": "game_client",
                "character": "",
                "realm": "",
                "modal": "none",
                "code": "",
                "control": "client",
                "idle": "false",
            },
            "character_select": {
                "scene": "character_select",
                "character": "Arthur",
                "realm": "Classic Beta PvE 2",
                "modal": "character_select",
                "code": "",
                "control": "character_list",
                "idle": "true",
            },
            "arthur_selected": {
                "scene": "character_select",
                "character": "Arthur",
                "realm": "Classic Beta PvE 2",
                "modal": "character_select",
                "code": "",
                "control": "arthur_row",
                "idle": "true",
            },
            "world": {
                "scene": "world",
                "character": "Arthur",
                "realm": "Classic Beta PvE 2",
                "modal": "none",
                "code": "",
                "control": "world",
                "idle": "true",
            },
            "auction_house_open": {
                "scene": "world",
                "character": "Arthur",
                "realm": "Classic Beta PvE 2",
                "modal": "auction_house",
                "code": "",
                "control": "auction_house",
                "idle": "true",
            },
        }[state]
        return ModelObservation(state, facts, anchor=(40, 30, 80, 60))


def test_three_cycle_daemon_and_socket_lifecycle(tmp_path: Path, monkeypatch) -> None:
    virtual_clock = _VirtualMonotonic()
    virtual_clock.native_schedule = True
    real_sleep = time.sleep
    backend = LifecycleBackend(tmp_path)
    backend.virtual_clock = virtual_clock

    driver = DesktopDriver(
        profile=DriverProfile(xauthority="/tmp/test-Xauthority", wine_prefix="/tmp/test-wine-prefix"),
        backend=backend,
        monotonic=virtual_clock.monotonic,
        lease_path=tmp_path / "desktop.lock",
    )
    journal = tmp_path / "session.json"
    plan = load_catalog().scan_plan()
    config = SessionConfig(
        profile=driver.profile,
        journal=journal,
        socket=tmp_path / "session.sock",
        plan_hash=plan["planHash"],
        plan_json=plan["planJSON"],
        capture_dir=tmp_path / "captures",
        saved_variables=tmp_path / "Crowley.lua",
        companion_state=tmp_path / "companion.json",
        data_lua=tmp_path / "Data.lua",
    )
    controller = Controller(
        journal, driver=driver, clock=lambda: 0, wall_clock=lambda: backend.current["wallTime"]
    )
    with patch("crowley.session.daemon.LunaAdapter", _RuntimeFixtureModel):
        daemon = SessionDaemon(config, controller=controller, driver=driver)
    driver.native_memory_meter = _FixtureNativeMemoryMeter(backend.identity)
    try:
        daemon.model = _FixtureLunaAdapter(backend)
        hierarchy = (
            (2, 3, "master", "pointer"),
            (3, 2, "master", "keyboard"),
            (4, 2, "slave", "XTEST pointer"),
            (5, 3, "slave", "XTEST keyboard"),
            (7, 2, "slave", "human pointer"),
            (9, 3, "slave", "human keyboard"),
            (10, 2, "slave", "pointer:xwayland-relative-pointer:17"),
        )
        synthetic = [
            _fixture_event(13, EventClass.RAW_KEY_PRESS, 3, 5, 100, 8, None),
            _fixture_event(2, EventClass.KEY_PRESS, 5, 5, 100, 8, 0x600119),
            _fixture_event(14, EventClass.RAW_KEY_RELEASE, 3, 5, 110, 8, None),
            _fixture_event(3, EventClass.KEY_RELEASE, 5, 5, 110, 8, 0x600119),
            _fixture_event(
                17,
                EventClass.RAW_MOTION,
                2,
                4,
                120,
                0,
                None,
                valuators=((0, 2.0, 2.0), (1, 2.0, 2.0)),
            ),
            # XI2 delivered Motion reports the source device in both the
            # master/source fields on this host (the preflight trace has
            # device 4 for the synthetic pointer).  Keep the fixture aligned
            # with the production pairing validator; device 2 is the raw
            # master and is used only by the raw button events below.
            _fixture_event(
                6,
                EventClass.MOTION,
                4,
                4,
                120,
                0,
                0x600119,
                coordinates=(2.0, 2.0),
                root_xy=(1.0, 1.0),
                event_xy=(1.0, 1.0),
                valuators=((0, 2.0, 2.0), (1, 2.0, 2.0)),
            ),
            _fixture_event(15, EventClass.RAW_BUTTON_PRESS, 2, 4, 121, 1, None),
            _fixture_event(4, EventClass.BUTTON_PRESS, 4, 4, 121, 1, 0x600119, coordinates=(2.0, 2.0)),
            _fixture_event(16, EventClass.RAW_BUTTON_RELEASE, 2, 4, 130, 1, None),
            _fixture_event(5, EventClass.BUTTON_RELEASE, 4, 4, 130, 1, 0x600119, coordinates=(2.0, 2.0)),
        ]
        # The real calibration owns the complete sealed keyboard vocabulary,
        # including every allowlisted command and the Control+A chord.  Feed
        # the same finite raw/delivered pairs to the deterministic observer so
        # this headless lifecycle exercises that production validator.
        keymap = backend.observe_keymap()
        keymap.update({str(digit): 100 + digit for digit in range(10) if str(digit) not in keymap})
        next_keycode = max(keymap.values(), default=31) + 1
        for operation in driver.calibration_keyboard_operations():
            for step in operation.steps:
                base = step.keysym.rsplit("+", 1)[-1]
                for symbol in (step.keysym, base):
                    if symbol not in keymap:
                        keymap[symbol] = next_keycode
                        next_keycode += 1
        event_number = 100
        timestamp = 300
        for operation in driver.calibration_keyboard_operations():
            for step in operation.steps:
                detail = keymap.get(step.keysym, keymap.get(step.keysym.rsplit("+", 1)[-1]))
                assert detail is not None
                raw_class = EventClass.RAW_KEY_PRESS if step.phase == "press" else EventClass.RAW_KEY_RELEASE
                delivered_class = EventClass.KEY_PRESS if step.phase == "press" else EventClass.KEY_RELEASE
                synthetic.extend(
                    (
                        _fixture_event(event_number, raw_class, 3, 5, timestamp, detail, None),
                        _fixture_event(event_number + 1, delivered_class, 5, 5, timestamp, detail, 0x600119),
                    )
                )
                event_number += 2
                timestamp += 1
        human = [
            _fixture_event(13, EventClass.RAW_KEY_PRESS, 3, 9, 200, 8, None),
            _fixture_event(2, EventClass.KEY_PRESS, 9, 9, 200, 8, 0x600119),
            _fixture_event(14, EventClass.RAW_KEY_RELEASE, 3, 9, 210, 8, None),
            _fixture_event(3, EventClass.KEY_RELEASE, 9, 9, 210, 8, 0x600119),
            # The live XI2 trace delivers human Motion with its source device
            # in both device fields.  Keep this event in the X11-backed
            # fixture: the workflow requires a measured human motion, and the
            # native seal must admit it only as an exact controlled echo.
            _fixture_event(6, EventClass.MOTION, 7, 7, 220, 0, 0x600119, coordinates=(2.0, 2.0)),
            _fixture_event(15, EventClass.RAW_BUTTON_PRESS, 2, 7, 221, 1, None),
            _fixture_event(4, EventClass.BUTTON_PRESS, 7, 7, 221, 1, 0x600119, coordinates=(2.0, 2.0)),
            _fixture_event(16, EventClass.RAW_BUTTON_RELEASE, 2, 7, 230, 1, None),
            _fixture_event(5, EventClass.BUTTON_RELEASE, 7, 7, 230, 1, 0x600119, coordinates=(2.0, 2.0)),
        ]

        driver.input_hierarchy = lambda **_kwargs: hierarchy  # type: ignore[method-assign]

        def synthetic_target_key_click(_window):
            backend._last_synthetic_motion_query = (1, 1, 0x600119, 2, 2, 2, 2, 0x600119)

        driver.synthetic_target_key_click = synthetic_target_key_click  # type: ignore[method-assign]
        driver.synthetic_target_keyboard_profile = (  # type: ignore[method-assign]
            lambda _window, *, operations, calibration_ticket: setattr(
                backend,
                "_last_synthetic_motion_query",
                (1, 1, 0x600119, 2, 2, 2, 2, 0x600119),
            )
        )

        def start_input_observer(
            *, calibration: bool = False, callback: object = None
        ) -> _FixtureInputObserver:
            if calibration:
                assert callback is not None
                observer = _FixtureInputObserver(callback, synthetic, human)
            else:
                observer = _FixtureInputObserver(driver._external_input, (), ())
            driver._monitor = observer
            driver._monitor_calibration = calibration
            if not calibration:
                backend.runtime_observer = observer
                backend.runtime_driver = driver
            return observer

        def stop_input_observer(observer=None, *, already_stopped: bool = False) -> None:
            del already_stopped
            if observer is None or driver._monitor is observer:
                driver._monitor = None
                driver._monitor_calibration = False

        driver.start_input_observer = start_input_observer  # type: ignore[method-assign]
        driver.stop_input_observer = stop_input_observer  # type: ignore[method-assign]

        # Public validation startup now passes the same passive-startup and
        # environment-bound takeover gates as ordinary repeat startup.  Seed a
        # matching durable drill and return a freshly minted private fixture
        # environment; this test remains focused on the three-cycle native
        # calibration/runtime integration below.
        levels_by_code: dict[int, list[str]] = {}
        for symbol, code in keymap.items():
            levels_by_code.setdefault(code, []).append(symbol)
            if len(symbol) == 1 and symbol.isascii() and symbol.isalpha() and symbol.islower():
                levels_by_code[code].append(symbol.upper())
        levels = {code: tuple(sorted(set(symbols))) for code, symbols in levels_by_code.items()}
        keyboard_profile = DesktopDriver._runtime_keyboard_profile(
            tuple(driver.COMMANDS), levels, keymap, (0, 0, 0), (1, 4)
        )
        xtest_profile = backend.xtest_input_profile(hierarchy)
        startup_environment = StartupEnvironment(
            display=driver.profile.display,
            hierarchy=hierarchy,
            keyboard_source=5,
            keyboard_master=3,
            pointer_source=4,
            pointer_master=2,
            forwarded_echo_sources=(10,),
            xtest_profile=xtest_profile,
            keymap_levels=tuple(sorted(levels.items())),
            modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
            input_tool_version=backend.input_tool.version,
            input_tool_sha256=backend.input_tool.digest,
        )
        startup_evidence = mint_runtime_evidence(
            readiness_boot="boot-runtime-fixture",
            readiness_sequence=44,
            environment=startup_environment,
            identity=backend.identity,
            keyboard_keymap=tuple(sorted(keymap.items())),
            native_modifiers=(0, 0, 0),
            native_held_keycodes=(),
            native_held_buttons=(),
            keyboard_profile=keyboard_profile,
        )
        controller.pause("fixture takeover drill setup")
        controller.persist_takeover_drill(
            TakeoverDrillRecord.create(startup_environment.fingerprint, 18, observed_at=1_800_000_000)
        )
        backend.observe_keymap = lambda **_kwargs: keymap
        backend.observe_keymap_levels = lambda **_kwargs: levels
        backend.observe_native_input_state = lambda _root: ((0, 0, 0), (), ())
        driver.input_hierarchy = lambda **_kwargs: hierarchy  # type: ignore[method-assign]

        def passive_startup():
            if driver._monitor is None:
                driver.start_input_observer()
            daemon._startup_evidence = startup_evidence
            daemon._startup_operation = {"state": "ready", "advancingFrames": 3}
            return startup_evidence

        daemon.passive_startup = passive_startup

        server = threading.Thread(target=serve, args=(config.socket, controller, daemon), daemon=True)
        server.start()
        for _ in range(100):
            if config.socket.exists():
                break
            __import__("time").sleep(0.01)

        def wait_for_operation(kind: str) -> dict:
            # The Lua Agent itself drives hold/measure/resume.  These requests
            # are delayed a few captures so the daemon must wait for actual
            # fresh publications rather than accepting its readiness signal.
            operator_actions = {"load": False, "hold": False, "measure": False, "resume": False}
            for _ in range(30000):
                response = _socket_request(config.socket, {"command": "calibration_status"})
                handshake = response["status"]["nativeHandshake"]
                if handshake["state"] == "bootstrap_ready" and not operator_actions["load"]:
                    backend.load_agent()
                    operator_actions["load"] = True
                if handshake["holdRequired"] and not operator_actions["hold"]:
                    backend.request_agent_command("hold")
                    operator_actions["hold"] = True
                if handshake["measureReady"] and not operator_actions["measure"]:
                    backend.request_agent_command("measure")
                    operator_actions["measure"] = True
                if handshake["resumeRequired"] and not operator_actions["resume"]:
                    backend.request_agent_command("resume")
                    operator_actions["resume"] = True
                operation = response["status"]["calibrationOperation"]
                if operation["kind"] == kind and operation["state"] in {"complete", "error"}:
                    details = operation | {"agentTrace": backend.agent_trace[-12:]}
                    if operation["state"] != "complete":
                        raise AssertionError(details)
                    return response["status"]
                real_sleep(0.005)
            raise AssertionError(f"{kind} calibration did not finish")

        start = _socket_request(
            config.socket,
            {"command": "start", "generation": controller.status().generation, "mode": "validation"},
        )
        assert start.get("ok") is True, start
        start_generation = start["status"]["generation"]
        bootstrap_calls: list[int | None] = []

        native_bootstrap = daemon.native_bootstrap

        def fixture_native_bootstrap(*, expected_generation: int | None = None) -> dict:
            bootstrap_calls.append(expected_generation)
            assert expected_generation == start_generation
            assert daemon.status_dict()["state"] == "NATIVE_CALIBRATION"
            return native_bootstrap(expected_generation=expected_generation)

        monkeypatch.setattr(daemon, "native_bootstrap", fixture_native_bootstrap)
        bootstrap = _socket_request(
            config.socket,
            {"command": "native_bootstrap", "generation": start_generation},
        )
        assert bootstrap["ok"]
        assert bootstrap_calls == [start_generation]

        native_start = _socket_request(
            config.socket,
            {"command": "native_calibrate", "generation": start_generation},
        )
        assert native_start["ok"]
        native_status = wait_for_operation("native")
        assert native_status["state"] == "INPUT_CALIBRATION"
        native_handshake = native_status["nativeHandshake"]
        assert native_handshake["state"] == "resume_ready"
        assert native_handshake["resumeRequired"]
        assert native_handshake["resumeReady"]
        assert native_handshake["restored"] is True
        assert [receipt["phase"] for receipt in native_handshake["archiveReceipts"]] == [
            "construction",
            "updates",
        ]
        assert all(receipt["recordSha256"] for receipt in native_handshake["archiveReceipts"])
        native_record = json.loads(journal.read_text())["calibrationEvidence"]["native"]
        retained_native_capture = Path(native_record["capture_path"])
        assert retained_native_capture.is_file()
        assert retained_native_capture.parent == journal.parent / "native-evidence"
        assert retained_native_capture.stat().st_mode & 0o777 == 0o600
        assert [path for path in retained_native_capture.parent.iterdir()] == [retained_native_capture]
        native_generation = native_status["generation"]

        from crowley.session import calibration as calibration_module

        real_input_workflow = calibration_module.InputCalibrationWorkflow
        calibration_trace: list[str] = []

        class FixtureInputCalibrationWorkflow(real_input_workflow):
            def __init__(self, owned_driver, *, display, on_ready):
                super().__init__(
                    owned_driver,
                    display=display,
                    on_ready=on_ready,
                    window_factory=lambda: _FixtureInputTarget(calibration_trace),
                )

            def run(self) -> object:
                return super().run(human_timeout=0.05)

        monkeypatch.setattr(
            calibration_module,
            "InputCalibrationWorkflow",
            FixtureInputCalibrationWorkflow,
        )
        real_issue_ticket = driver.issue_calibration_ticket
        real_focus_target = driver.focus_calibration_target
        real_notify_ready = driver.notify_input_calibration_ready
        real_queue_delayed = driver.queue_delayed_calibration_action
        pointer_before_focus = backend.pointer_position()

        def issue_ticket(window):
            calibration_trace.append("ticket")
            return real_issue_ticket(window)

        def focus_target(window, *, calibration_ticket):
            calibration_trace.append("focus")
            result = real_focus_target(window, calibration_ticket=calibration_ticket)
            backend.calibration_target_xid = window.xid
            pointer_after_focus = backend.pointer_position()
            assert pointer_after_focus[:2] == pointer_before_focus[:2]
            assert pointer_after_focus[2] == window.xid
            return result

        def notify_ready(readiness):
            calibration_trace.append("ready")
            return real_notify_ready(readiness)

        def queue_delayed(window, *, delay, calibration_ticket):
            calibration_trace.append("schedule")
            return real_queue_delayed(
                window,
                delay=delay,
                calibration_ticket=calibration_ticket,
            )

        def synthetic_profile(_window, *, operations, calibration_ticket):
            assert operations
            assert calibration_ticket is not None
            calibration_trace.append("synthetic")
            backend._last_synthetic_motion_query = (1, 1, 0x600119, 2, 2, 2, 2, 0x600119)

        driver.issue_calibration_ticket = issue_ticket  # type: ignore[method-assign]
        driver.focus_calibration_target = focus_target  # type: ignore[method-assign]
        driver.notify_input_calibration_ready = notify_ready  # type: ignore[method-assign]
        driver.queue_delayed_calibration_action = queue_delayed  # type: ignore[method-assign]
        driver.synthetic_target_keyboard_profile = synthetic_profile  # type: ignore[method-assign]
        input_start = _socket_request(
            config.socket,
            {"command": "input_calibrate", "generation": native_generation},
        )
        assert input_start["ok"]
        paused_status = wait_for_operation("input")
        assert paused_status["state"] == "PAUSED"
        assert _FixtureInputTarget.last_created is not None
        assert _FixtureInputTarget.last_created.prompt_acknowledged
        # The delayed-action due fence is sealed before readiness is published;
        # a consumer cannot observe a human window before its timer exists.
        assert calibration_trace == ["ticket", "focus", "synthetic", "prompt_ack", "schedule", "ready"]
        paused_generation = paused_status["generation"]
        assert paused_generation > native_generation
        # Native cadence deliberately advances the injected clock by one
        # second per real Agent tick.  Rebase the headless lifecycle clock
        # before handing control to the daemon's model-backed visual
        # producer, whose timing remains wall-clock scoped in production.
        virtual_clock.value = time.monotonic()
        backend.use_lifecycle_agent()

        resumed = _socket_request(
            config.socket,
            {"command": "resume", "generation": paused_generation},
        )
        assert resumed["ok"], resumed
        assert resumed["status"]["state"] == "RECONCILE"
        _attach_recording_anchor_provider(driver, tmp_path)

        advanced_uids: set[str] = set()
        for _ in range(160):
            daemon.run_once()
            if backend.uid and backend.current["loaded"]["ackedIds"] and backend.uid not in advanced_uids:
                backend.advance_ack()
                advanced_uids.add(backend.uid)
            status = _socket_request(config.socket, {"command": "status"})["status"]
            if status["state"] == "STOPPED":
                break
        final_status = _socket_request(config.socket, {"command": "status"})["status"]
        assert final_status["streak"] == 3, final_status
        assert final_status["state"] == "STOPPED", final_status
        pointer_events = [event for event in backend.events if event["name"] in driver.POINTER_ACTIONS]
        gesture_receipts = driver.pointer_gesture_receipts
        assert len(gesture_receipts) == len(pointer_events)
        if pointer_events:
            assert all(
                isinstance(receipt["seed"], int)
                and len(receipt["frozenEndpoint"]) == 2
                and receipt["movementDuration"] > 0
                and receipt["submovementCount"] > 0
                and receipt["accepted"] is True
                for receipt in gesture_receipts
            )
            rate_state = json.loads((tmp_path / "pointer-click-rate" / "pointer-click-rate.json").read_text())
            assert isinstance(rate_state["lastPressMonotonicNs"], int)
            assert rate_state["nextAllowedMonotonicNs"] - rate_state["lastPressMonotonicNs"] == 1_000_000_000
        interaction_events = [event for event in backend.events if event["name"] == "open_ah"]
        assert interaction_events
        assert backend.runtime_observer is not None
        keyboard_facts = driver._keyboard_operation_facts("interact_target", "", driver._input_evidence)
        expected_chord = []
        for phase, _keysym, detail, _modifiers, _effective in keyboard_facts:
            raw = EventClass.RAW_KEY_PRESS if phase == "press" else EventClass.RAW_KEY_RELEASE
            delivered = EventClass.KEY_PRESS if phase == "press" else EventClass.KEY_RELEASE
            expected_chord.extend(((raw, detail), (delivered, detail)))
        observed = [(event.event_class, event.detail) for event in backend.runtime_observer.events()]
        matched_chords = 0
        index = 0
        while index <= len(observed) - len(expected_chord):
            if observed[index : index + len(expected_chord)] == expected_chord:
                matched_chords += 1
                index += len(expected_chord)
            else:
                index += 1
        assert matched_chords == len(interaction_events)
        assert any(
            event["name"] == "submit_command" and event.get("command") == "/logout"
            for event in backend.events
        )
        assert any(
            event["name"] == "submit_command" and event.get("command") == "/crowley session fence"
            for event in backend.events
        )
    finally:
        daemon.request_shutdown()
        driver.close()


def test_live_daemon_binds_owned_visual_and_render_providers(tmp_path: Path) -> None:
    plan = load_catalog().scan_plan()
    profile = DriverProfile(
        xauthority="/tmp/test-Xauthority",
        wine_prefix="/measured/wine-prefix",
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "desktop.lock")
    config = SessionConfig(
        profile=profile,
        journal=tmp_path / "session.json",
        socket=tmp_path / "session.sock",
        plan_hash=plan["planHash"],
        plan_json=plan["planJSON"],
    )
    with patch("crowley.session.daemon.LunaAdapter", _RuntimeFixtureModel):
        daemon = SessionDaemon(config, driver=driver)
    try:
        assert getattr(driver._named_state_observer, "__self__", None) is daemon
        assert getattr(driver.native_measurement_provider, "__self__", None) is daemon
        assert getattr(driver.measured_anchor_provider, "__self__", None) is daemon
    finally:
        daemon.request_shutdown()
        driver.close()


def test_default_native_meter_binds_real_child_and_closes_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = subprocess.Popen(["/bin/sleep", "30"])
    stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
    process_start = stat[stat.rfind(")") + 2 :].split()[19]
    linux_executable = os.path.realpath(os.readlink(f"/proc/{child.pid}/exe"))
    profile = DriverProfile(xauthority="/tmp/test-Xauthority", wine_prefix="/tmp/test-wine-prefix")
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    identity = WindowIdentity(
        0x1800001,
        child.pid,
        "WowB.exe",
        "World of Warcraft",
        "wowb.exe,wowb.exe",
        0x1600011,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x1800001,
        process_start,
        ":test",
        "default-meter-child",
        0x1,
        0x1800001,
        frozenset({0x1800001, 0x1600011}),
    )
    backend.resolve_identity = lambda require_foreground=True: identity  # type: ignore[attr-defined]
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "default-meter.lock")
    plan = load_catalog().scan_plan()
    config = SessionConfig(
        profile=profile,
        journal=tmp_path / "session.json",
        socket=tmp_path / "session.sock",
        plan_hash=plan["planHash"],
        plan_json=plan["planJSON"],
        codex_path="/home/dev/.cache/crowley/codex/0.162.1/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex",
    )
    journal = Journal(config.journal)
    journal.write(
        {**copy.deepcopy(DEFAULT_STATE), "state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4}
    )
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    daemon = SessionDaemon(config, controller=controller, driver=driver)
    meters: list[LinuxProcStatusVmHwmReset5] = []
    outcomes = iter(("failure", "success"))

    class ProbeRunner:
        def __init__(
            self, owned_driver: DesktopDriver, *, monotonic: object, diagnostic: object | None = None
        ) -> None:
            del monotonic
            del diagnostic
            meter = owned_driver.native_memory_meter
            assert isinstance(meter, LinuxProcStatusVmHwmReset5)
            assert meter.pid == identity.pid
            assert meter.process_start == identity.process_start
            assert meter.process_executable == linux_executable
            assert meter.identity_fingerprint == identity.fingerprint
            meters.append(meter)

        def run(self) -> object:
            if next(outcomes) == "failure":
                raise DriverError("default meter probe failure")
            return SimpleNamespace()

    monkeypatch.setattr("crowley.session.daemon.NativeCalibrationRunner", ProbeRunner)

    def native_calibrated(
        _evidence: object,
        *,
        preconstruction_proof: dict[str, object] | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | None = None,
    ) -> object:
        del preconstruction_proof
        assert expected_generation == 4
        assert expected_state is ControllerState.NATIVE_CALIBRATION
        return replace(controller.status(), state=ControllerState.INPUT_CALIBRATION)

    controller.native_calibrated = native_calibrated  # type: ignore[method-assign]
    try:
        with pytest.raises(DriverError, match="default meter probe failure"):
            daemon.run_native_calibration(expected_generation=4)
        assert daemon._native_handshake["state"] == "error"
        assert daemon._native_handshake["restored"] is False
        assert driver.native_memory_meter is None
        assert meters[0]._closed

        result = daemon.run_native_calibration(expected_generation=4)
        assert result["calibration"] == "native"
        assert len(meters) == 2
        assert meters[1] is not meters[0]
        assert meters[1]._closed
        assert driver.native_memory_meter is None
    finally:
        daemon.request_shutdown()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_runtime_reconcile_ready_scanner_is_idle_with_pending_saved_uid(tmp_path: Path) -> None:
    daemon, backend, driver, _controller, config, _clock = _runtime_daemon(tmp_path)
    uid = "runtime-pending-uid"
    _saved_fixture(config.saved_variables, uid, 100)  # type: ignore[arg-type]
    assert config.companion_state is not None
    assert config.data_lua is not None
    write_companion_state(config.companion_state, uploaded={uid: 103})
    config.data_lua.write_text(
        render_assignment(
            "CROWLEY_DATA",
            {
                "generatedAt": 103,
                "ackedScans": [uid],
                "scanPlan": load_catalog().scan_plan(),
                "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
                "shoppingLists": [],
            },
            "runtime pending UID",
        )
    )
    snapshot = _snapshot(1000)
    snapshot["lastSaved"] = {
        "memoryUid": uid,
        "finishedAt": 102,
        "planned": 428,
        "attempted": 428,
        "failures": 0,
        "rows": 428,
        "persistence": "unknown",
    }
    snapshot["pending"] = {"ids": [uid], "count": 1, "cap": 6, "blocked": False, "reason": ""}
    try:
        facts = daemon._reconcile_durable_files(snapshot)
        assert facts["scanner_phase"] == "ready"
        assert facts["scanner_idle"] is True
        assert facts["stable_format2_uid"] == uid
        assert facts["stable_format2_evidence"] is not None
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_reconcile_rejects_legacy_sweeps_queue(tmp_path: Path) -> None:
    daemon, _backend, driver, _controller, config, _clock = _runtime_daemon(tmp_path)
    assert config.saved_variables is not None
    config.saved_variables.write_text(
        render_assignment("CrowleyDB", {"scans": {}, "sweeps": {}}, "legacy queue")
    )
    try:
        with pytest.raises(EvidenceError, match="legacy sweeps"):
            daemon._reconcile_durable_files(_snapshot(1000))
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_uncertain_no_agent_never_recovers_login_or_input(tmp_path: Path) -> None:
    cases = ("logout", "enter_world")
    for action in cases:
        root = tmp_path / action
        uid = f"runtime-{action}-uid"
        daemon, backend, driver, controller, config, _clock = _runtime_daemon(
            root,
            journal_changes={
                "state": ControllerState.RECONCILE.value,
                "generation": 4,
                "restartUncertain": True,
                "reconciliationRequired": True,
                "current": {
                    "uid": uid,
                    "pending": True,
                    "startedAt": 100,
                    "action": action,
                    "actionAttempt": 2,
                    "actionResult": "dispatched",
                },
            },
        )
        try:
            _saved_fixture(config.saved_variables, uid, 100)  # type: ignore[arg-type]
            assert config.companion_state is not None
            assert config.data_lua is not None
            write_companion_state(config.companion_state, uploaded={uid: 103})
            config.data_lua.write_text(
                render_assignment(
                    "CROWLEY_DATA",
                    {
                        "generatedAt": 103,
                        "ackedScans": [uid],
                        "scanPlan": load_catalog().scan_plan(),
                        "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
                        "shoppingLists": [],
                    },
                    "runtime unsettled queue",
                )
            )
            backend.current = _snapshot(1000)
            backend.current["lastSaved"]["memoryUid"] = uid
            backend.current["pending"] = {
                "ids": [uid],
                "count": 1,
                "cap": 6,
                "blocked": False,
                "reason": "",
            }
            if action == "logout":
                backend.character_select_observed = lambda: True  # type: ignore[attr-defined]
            else:

                def loading_capture() -> object:
                    raise OSError("telemetry canvas absent while world loads")

                daemon.capture_status = loading_capture  # type: ignore[method-assign]

            def forbidden_recovery() -> None:
                raise AssertionError("uncertain restart dispatched login recovery")

            daemon._recover_login = forbidden_recovery  # type: ignore[method-assign]
            before = controller.journal.read()
            status = daemon.run_once()
            assert status["state"] == ControllerState.RECONCILE.value
            assert status["actionIntent"] == action
            assert controller.journal.read() == before
            assert backend.events == []
        finally:
            daemon.request_shutdown()
            driver.close()


def test_runtime_ack_reload_missing_full_evidence_does_not_complete(tmp_path: Path) -> None:
    uid = "runtime-missing-evidence"
    daemon, _backend, driver, controller, config, _clock = _runtime_daemon(
        tmp_path,
        journal_changes={
            "state": ControllerState.ACK_RELOAD_WAIT.value,
            "generation": 4,
            "current": {
                "uid": uid,
                "pending": True,
                "startedAt": 100,
                "action": "ack_reload",
                "actionAttempt": 2,
                "actionResult": "dispatched",
                "frameBoot": "ack-boot",
                "frameSequence": 2,
                "evidence": {
                    "uid": uid,
                    "stableSavedUid": uid,
                    "finishedAt": 102,
                    "uploadTimestamp": 103,
                    "ackPublicationAt": 103,
                    "atomicCompanionUploadedUid": uid,
                    "dataAckUid": uid,
                },
            },
        },
    )
    try:
        # Deliberately remove the stable SavedVariables path.  The loaded/pending
        # telemetry is positive, but no complete SweepEvidence is available to
        # qualify this UID.
        object.__setattr__(daemon.config, "saved_variables", None)
        snapshot = _snapshot(104, boot="ack-boot", sequence=2)
        snapshot["loaded"] = {"ackedIds": [uid], "generatedAt": 104}
        snapshot["pending"] = {"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": ""}
        try:
            daemon._advance(snapshot, fence=controller.observation_fence())
        except EvidenceError as exc:
            assert "stable evidence path" in str(exc)
        status = controller.status()
        assert status.state is not ControllerState.REOPEN
        assert status.state in {ControllerState.ACK_RELOAD_WAIT, ControllerState.BLOCKED}
        assert controller.journal.read()["current"]["pending"] is True
        assert controller.journal.read()["qualification"]["totalAccepted"] == 0
    finally:
        daemon.request_shutdown()
        driver.close()


def test_due_at_zero_is_anchored_to_the_last_actual_scan_start(tmp_path: Path) -> None:
    daemon, backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        journal_changes={
            "state": ControllerState.READY.value,
            "generation": 4,
            "current": {"startedAt": 1_000},
        },
    )
    commands: list[str] = []
    daemon._command = lambda _snapshot, command: commands.append(command) or True  # type: ignore[method-assign]
    try:
        early = _snapshot(1_500)
        early["scanner"]["dueAt"] = 0
        early["ahOpen"] = True
        daemon._advance(early, fence=controller.observation_fence())
        assert commands == []
        assert controller.status().state is ControllerState.READY

        due = _snapshot(1_900, sequence=2)
        due["scanner"]["dueAt"] = 0
        due["ahOpen"] = True
        backend.current = due
        daemon._advance(due, fence=controller.observation_fence())
        assert commands == ["/crowley scan"]
        assert controller.status().state is ControllerState.SCANNING
        assert controller.status().started_at == 1_900
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_qualification_retry_journal_error_preserves_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = "runtime-qualification-retry"
    daemon, _backend, driver, controller, config, _clock = _runtime_daemon(
        tmp_path,
        journal_changes={"state": ControllerState.WAIT_DUE.value},
    )
    assert config.saved_variables is not None
    _saved_fixture(config.saved_variables, uid, 100)
    snapshot = _snapshot(1000)
    evidence = read_stable_saved_scan(
        config.saved_variables,
        uid=uid,
        planned=428,
        **daemon._saved_scan_kwargs(snapshot),
    )
    try:
        controller.begin_scan(started_at=100)
        controller.saved(uid)
        controller.reload_sent()
        controller.expect_boot("save-old")
        controller.observe_frame(boot="save-boot", sequence=1, crc32=1)
        controller.stable_saved(evidence)
        controller.uploaded(uid, accepted=True, upload_time=103, data_ack_uid=uid)
        controller.record_ack_publication(
            uid,
            companion_uid=uid,
            data_uid=uid,
            publication_time=103,
            boot="save-boot",
            sequence=1,
        )
        controller.ack_reload_command_sent()
        controller.expect_boot("ack-old", acknowledgement=True)
        controller.observe_frame(boot="ack-boot", sequence=2, crc32=2)
        controller.ack_reload_complete(
            pending_ids=set(),
            loaded_uid=uid,
            loaded_at=105,
            boot="ack-boot",
            sequence=2,
        )

        original_write = controller.journal._write_archive_artifact
        first_failure = True

        def fail_first_publication(kind: str, ordinal: int, record: dict) -> None:
            nonlocal first_failure
            if first_failure and kind == "runs":
                first_failure = False
                raise JournalError("runtime injected qualification publication failure")
            original_write(kind, ordinal, record)

        monkeypatch.setattr(controller.journal, "_write_archive_artifact", fail_first_publication)
        with pytest.raises(JournalError):
            controller.qualify(evidence, started_at=100)

        def fail_retry_publication(kind: str, ordinal: int, record: dict) -> None:
            del kind, ordinal, record
            raise JournalError("runtime retry publication failure")

        monkeypatch.setattr(controller.journal, "_write_archive_artifact", fail_retry_publication)
        durable_before_retry = controller.journal.read()
        try:
            daemon.run_once()
        except JournalError:
            pass
        durable_after_retry = controller.journal.read()
        assert durable_after_retry == durable_before_retry
        assert controller.status().state is ControllerState.QUALIFY_PENDING
        assert controller.journal.read()["qualification"]["totalAccepted"] == 0
    finally:
        daemon.request_shutdown()
        driver.close()


def _assert_native_provider_cleanup(
    root: Path, backend: _NativeProviderBackend, daemon: SessionDaemon
) -> None:
    assert daemon._native_owned_capture_paths == set()
    assert all(not path.exists() for path in backend.capture_paths)
    evidence_dir = root / "native-evidence"
    if evidence_dir.exists():
        assert not list(evidence_dir.glob("*.png"))


def _diagnostic_fixture_daemon(root: Path) -> SimpleNamespace:
    source_binding = {name: "a" * 64 for name in SAFETY_MODULES}
    return SimpleNamespace(
        config=SimpleNamespace(journal=root / "session.json"),
        _runtime_source_binding=source_binding,
        _runtime_source_binding_digest=manifest_digest(source_binding),
        _loaded_config_semantic_sha256="c" * 64,
        driver=SimpleNamespace(profile=SimpleNamespace(telemetry_region_executable_sha256="d" * 64)),
        controller=SimpleNamespace(status=lambda: SimpleNamespace(generation=7)),
    )


class _RuntimeFixtureModel:
    def __init__(self, **_kwargs: object) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def shutdown(self) -> None:
        self.cancelled = True


def test_runtime_native_observation_diagnostic_is_bounded_private_and_bound(tmp_path: Path) -> None:
    daemon = _diagnostic_fixture_daemon(tmp_path)
    diagnostic = _NativeObservationDiagnostic(daemon)
    diagnostic.start_run()
    with diagnostic.span("identity_pre_provider", requested_stage="updates", requested_ordinal=1):
        pass
    for ordinal in range(1, 20):
        diagnostic.begin_attempt("updates", ordinal)
        diagnostic.finish_attempt(status="no_change")

    capture = tmp_path / "candidate.png"
    Image.new("RGB", (256, 256), (0, 0, 0)).save(capture, format="PNG")
    frame = SimpleNamespace(
        snapshot=SimpleNamespace(
            value={
                "render": {
                    "nativePhase": "updates",
                    "nativeOrdinal": 20,
                    "updateCount": 19,
                    "luaHeap": {
                        "method": "collectgarbage_count",
                        "available": True,
                        "beforeKiB": 100.0,
                        "afterKiB": 161.5,
                        "netKiB": 61.5,
                    },
                    "textureCount": 2048,
                    "updateP99Ms": 3.25,
                }
            }
        ),
        boot="boot-7",
        sequence=27,
        uptime_ms=20_000,
    )
    sample = SimpleNamespace(kind="FRESH", frame=frame, capture_monotonic=123.5)
    diagnostic.begin_attempt("updates", 20)
    diagnostic.record_candidate(sample, "e" * 64, capture)
    diagnostic.set_status(status="mismatch", error="wrong publication")
    with diagnostic.span("identity_poll"):
        pass
    diagnostic.fail(
        "native updates/20 returned 'updates'/21",
        stage="updates",
        ordinal=20,
        offending_path=capture,
        mismatch=True,
    )
    capture.unlink()
    diagnostic.mark_release(capture)

    failure = diagnostic.failure_path
    offending = diagnostic.offending_path
    assert failure is not None and failure.is_file()
    assert offending is not None and offending.is_file()
    with Image.open(offending) as image:
        assert image.size == (256, 256)
    assert stat.S_IMODE(failure.stat().st_mode) == 0o600
    assert stat.S_IMODE(failure.parent.stat().st_mode) == 0o700
    payload = json.loads(failure.read_text())
    assert payload["trace_schema"] == "native-observation-diagnostic-v1"
    assert payload["protocol_sha256"] == payload["source_binding"]["crowley.session.protocol"]
    assert isinstance(payload["source_binding_current"], dict)
    assert isinstance(payload["source_binding_current_sha256"], str)
    assert isinstance(payload["calibration_source_sha256_current"], str)
    assert payload["source_binding_error"] is None
    assert payload["source_drift"] is True
    assert payload["binary_sha256"] == "d" * 64
    assert len(payload["attempts"]) == 16
    assert payload["attempts"][0]["requested_ordinal"] == 5
    assert payload["attempts"][-1]["actual"]["publisher_uptime_ms"] == 20_000
    assert payload["attempts"][-1]["actual"]["lua_heap"] == {
        "method": "collectgarbage_count",
        "available": True,
        "before_kib": 100.0,
        "after_kib": 161.5,
        "net_kib": 61.5,
    }
    assert payload["attempts"][-1]["actual"]["texture_count"] == 2048
    assert payload["attempts"][-1]["actual"]["update_p99_ms"] == 3.25
    assert payload["attempts"][-1]["spans"]["identity_poll"]["duration_ns"] is not None
    assert payload["attempts"][-1]["spans"]["cells"]["duration_ns"] is None
    assert payload["attempts"][-1]["release_outcome"] == "released"
    assert payload["run_spans"][0]["name"] == "identity_pre_provider"
    assert payload["run_spans"][0]["requested_stage"] == "updates"
    assert payload["run_spans"][0]["requested_ordinal"] == 1
    assert payload["run_spans"][0]["duration_ns"] is not None
    assert str(tmp_path) not in failure.read_text()
    assert (
        payload["failure"]["offending_capture_sha256"] == hashlib.sha256(offending.read_bytes()).hexdigest()
    )


def test_runtime_native_observation_render_metrics_are_closed_and_unavailable(tmp_path: Path) -> None:
    daemon = _diagnostic_fixture_daemon(tmp_path)
    diagnostic = _NativeObservationDiagnostic(daemon)
    diagnostic.start_run()

    known_heap = {
        "method": "collectgarbage_count",
        "available": True,
        "beforeKiB": 10.0,
        "afterKiB": 12.5,
        "netKiB": 2.5,
    }
    known_render = {
        "nativePhase": "updates",
        "nativeOrdinal": 1,
        "updateCount": 0,
        "luaHeap": known_heap,
        "textureCount": 2048,
        "updateP99Ms": 1.75,
    }
    client = WowClient(
        faction="Alliance",
        realm="Classic Beta PvE 2",
        zone="Stormwind",
        current_region=110,
        current_region_name="beta",
    )
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    encoded_snapshot = client.snapshot_agent()
    encoded_snapshot["sequence"] = 8
    encoded_snapshot["uptimeMs"] = 1_000
    encoded_snapshot["render"].update(known_render)
    client.ns.Agent.Snapshot = lambda: client._to_lua(encoded_snapshot)
    client.publish_agent()

    raw = client.agent_frame
    packed_cells = b"".join(raw[row * 16 : (row + 1) * 16].translate(_REVERSE_BITS) for row in range(128))
    crop = (
        Image.frombytes("1", (128, 128), packed_cells)
        .resize((256, 256), Image.Resampling.NEAREST)
        .convert("RGB")
    )
    decoded_sample = PixelDecoder().decode(crop, monotonic=10.0, window_identity="diagnostic-frame")
    assert decoded_sample.frame is not None
    assert decoded_sample.frame.snapshot.value["render"]["luaHeap"] == known_heap
    diagnostic.begin_attempt("updates", 1)
    diagnostic.record_candidate(decoded_sample, "a" * 64)
    decoded_sample.frame.snapshot.value["render"]["luaHeap"]["afterKiB"] = 999.0
    diagnostic.finish_attempt(status="no_change")

    unavailable_sample = SimpleNamespace(
        kind="FRESH",
        frame=SimpleNamespace(
            snapshot=SimpleNamespace(
                value={
                    "render": {
                        "nativePhase": "updates",
                        "nativeOrdinal": 2,
                        "luaHeap": {
                            "method": "collectgarbage_count",
                            "available": False,
                            "beforeKiB": 0,
                            "afterKiB": 0,
                            "netKiB": 0,
                        },
                        "textureCount": "unknown",
                        "updateP99Ms": float("nan"),
                        "privateDiagnosticField": "must not escape",
                    }
                }
            ),
            boot="boot-7",
            sequence=9,
            uptime_ms=2_000,
        ),
        capture_monotonic=11.0,
    )
    diagnostic.begin_attempt("updates", 2)
    diagnostic.record_candidate(unavailable_sample, "b" * 64)
    diagnostic.fail("diagnostic fixture mismatch", stage="updates", ordinal=2)

    assert diagnostic.failure_path is not None
    payload = json.loads(diagnostic.failure_path.read_text())
    known_actual = payload["attempts"][-2]["actual"]
    assert known_actual["lua_heap"] == {
        "method": "collectgarbage_count",
        "available": True,
        "before_kib": 10.0,
        "after_kib": 12.5,
        "net_kib": 2.5,
    }
    assert known_actual["texture_count"] == 2048
    assert known_actual["update_p99_ms"] == 1.75
    unavailable_actual = payload["attempts"][-1]["actual"]
    assert unavailable_actual["lua_heap"] == {
        "method": "collectgarbage_count",
        "available": False,
        "before_kib": 0,
        "after_kib": 0,
        "net_kib": 0,
    }
    assert unavailable_actual["texture_count"] is None
    assert unavailable_actual["update_p99_ms"] is None
    assert "privateDiagnosticField" not in unavailable_actual


def test_runtime_native_observation_diagnostic_persistence_failure_preserves_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _diagnostic_fixture_daemon(tmp_path)
    diagnostic = _NativeObservationDiagnostic(daemon)
    diagnostic.start_run()
    monkeypatch.setattr(diagnostic, "_failure_directory", lambda: Path("/proc/does-not-exist"))
    diagnostic.fail("original native mismatch", stage="updates", ordinal=4)
    assert diagnostic.failure_path is None
    assert diagnostic._persistence_error is not None


def test_runtime_native_observation_diagnostic_success_writes_no_artifact(tmp_path: Path) -> None:
    daemon = _diagnostic_fixture_daemon(tmp_path)
    diagnostic = _NativeObservationDiagnostic(daemon)
    diagnostic.start_run()
    diagnostic.begin_attempt("construct", 0)
    diagnostic.finish_attempt(status="accepted")
    diagnostic.finish_success()
    assert not (tmp_path / "native-observation-diagnostics").exists()


def test_runtime_native_observation_recording_overhead_is_bounded(tmp_path: Path) -> None:
    daemon = _diagnostic_fixture_daemon(tmp_path)
    diagnostic = _NativeObservationDiagnostic(daemon)
    diagnostic.start_run()
    iterations = 128
    baseline_started = time.perf_counter_ns()
    for ordinal in range(iterations):
        diagnostic.begin_attempt("updates", ordinal)
        with nullcontext():
            hashlib.sha256(f"recorded-{ordinal}".encode()).hexdigest()
        with nullcontext():
            json.dumps({"ordinal": ordinal}, sort_keys=True)
        diagnostic.finish_attempt(status="no_change")
    baseline_ns = time.perf_counter_ns() - baseline_started

    recorded_started = time.perf_counter_ns()
    for ordinal in range(iterations):
        diagnostic.begin_attempt("updates", ordinal)
        with diagnostic.span("provider_hash"):
            hashlib.sha256(f"recorded-{ordinal}".encode()).hexdigest()
        with diagnostic.span("provider_decode"):
            json.dumps({"ordinal": ordinal}, sort_keys=True)
        diagnostic.finish_attempt(status="no_change")
    recorded_ns = time.perf_counter_ns() - recorded_started
    assert recorded_ns - baseline_ns < 250_000_000


def test_runtime_native_provider_delayed_agent_and_hold_use_real_publications(tmp_path: Path) -> None:
    backend = _NativeProviderBackend(tmp_path, load_after_captures=3)
    daemon, _backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    try:
        identity = backend.identity
        constructed = daemon._native_measurement_provider(identity, "construct", 0)
        assert constructed.native_phase == "normal"
        assert constructed.native_ordinal == 0
        assert backend.agent_client is not None
        assert backend.captures >= 4  # three genuine no-Agent polls precede addon load
        daemon.release_native_capture(Path(constructed.capture_path))

        backend.request_agent_command("hold", delayed_captures=3)
        held = daemon._native_measurement_provider(identity, "held", 0)
        assert held.native_phase == "held"
        assert held.native_ordinal == 0
        held_index = next(
            index for index, item in enumerate(backend.agent_trace) if item[1] == "held" and item[2] == 0
        )
        assert all(item[1] == "normal" for item in backend.agent_trace[:held_index])
        daemon.release_native_capture(Path(held.capture_path))

        backend.request_agent_command("measure", delayed_captures=3)
        update = daemon._native_measurement_provider(identity, "updates", 1)
        assert update.native_phase == "updates"
        assert update.native_ordinal == 1
        daemon.release_native_capture(Path(update.capture_path))

        backend.agent_client.advance_with_timers(99)
        held_final = daemon._native_measurement_provider(identity, "held", 100)
        assert held_final.native_phase == "held"
        assert held_final.native_ordinal == 100
        held_final_sequence = held_final.sequence
        daemon.release_native_capture(Path(held_final.capture_path))

        backend.request_agent_command("resume", delayed_captures=3)
        resumed = daemon._native_measurement_provider(identity, "resume", 0)
        assert resumed.native_phase == "normal"
        assert resumed.native_ordinal == 0
        assert resumed.sequence == held_final_sequence + 1
        daemon.release_native_capture(Path(resumed.capture_path))
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_observation_couples_provider_and_runner_spans(tmp_path: Path) -> None:
    """The real provider and runner hooks share one attributed attempt trace."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider-coupled.lock",
    )
    diagnostic = daemon._native_observation_diagnostic
    runner = NativeCalibrationRunner(driver, diagnostic=diagnostic)
    decoder = driver.new_native_decoder()
    released = False
    try:
        identity = backend.identity
        diagnostic.start_run()
        with runner._diagnostic_span(
            "identity_pre_provider", requested_stage="construct", requested_ordinal=0
        ):
            assert driver.resolve_identity().fingerprint == identity.fingerprint
        observation = daemon._native_measurement_provider(identity, "construct", 0)
        with runner._diagnostic_span("runner_hash"):
            capture_path = runner._capture_observation(identity, observation)
        with runner._diagnostic_span("runner_decode"):
            decoded = driver.decode_native_capture(capture_path, identity=identity, decoder=decoder)
        assert decoded.frame is not None

        attempt = diagnostic._attempts[-1]
        assert attempt["requested_stage"] == "construct"
        assert attempt["requested_ordinal"] == 0
        for name in ("provider_hash", "provider_decode", "runner_hash", "runner_decode"):
            assert attempt["spans"][name]["duration_ns"] is not None
            assert attempt["spans"][name]["outcome"] == "ok"
        assert diagnostic._current is attempt
        runner._release_capture(Path(observation.capture_path))
        released = True
        assert attempt["release_outcome"] == "released"
        assert diagnostic._current is None
        with runner._diagnostic_span("identity_pre_provider", requested_stage="updates", requested_ordinal=1):
            assert driver.resolve_identity().fingerprint == identity.fingerprint
        pre_provider = [span for span in diagnostic._run_spans if span["name"] == "identity_pre_provider"]
        assert len(pre_provider) == 2
        assert (pre_provider[0]["requested_stage"], pre_provider[0]["requested_ordinal"]) == (
            "construct",
            0,
        )
        assert (pre_provider[1]["requested_stage"], pre_provider[1]["requested_ordinal"]) == (
            "updates",
            1,
        )
    finally:
        if not released and "observation" in locals() and observation.capture_path is not None:
            daemon.release_native_capture(Path(observation.capture_path))
        daemon.request_shutdown()
        driver.close()


def _prime_native_update_budget_owner(
    daemon: SessionDaemon,
    driver: DesktopDriver,
    identity: WindowIdentity,
    *,
    deferred: bool = True,
) -> None:
    """Install one private consumed owner as a real daemon admission fixture."""

    guardian = driver.guardian
    with guardian._lock:
        guardian_generation = guardian.generation
        guardian_armed = bool(guardian.armed)
        lease_held = bool(guardian.lease.held)
    epoch = 23
    reservation_id = "native-update-budget-owner"
    profile_fingerprint = daemon._native_construction_profile_fingerprint(driver.profile)
    owner_start = _proc_start(os.getpid())
    assert owner_start
    operation_deadline = daemon._native_clock() + 30.0
    with daemon._native_construction_lock:
        daemon._native_construction_epoch = epoch
        daemon._native_construction_reservation = {
            "reservationId": reservation_id,
            "reservationKind": "measure",
            "reservationState": "consumed",
            "controllerGeneration": 4,
            "desktopLeaseHeld": lease_held,
            "guardianGeneration": guardian_generation,
            "guardianArmed": guardian_armed,
            "ownerPid": os.getpid(),
            "ownerProcessStart": owner_start,
            "profileFingerprint": profile_fingerprint,
            "identityFingerprint": identity.fingerprint,
            "B0": "boot-1",
            "expectedConstructorBoot": "native-construction:boot-1",
            "expiresMonotonic": operation_deadline,
        }
        daemon._native_construction_run_context = {
            "runId": "native-update-budget-run",
            "epoch": epoch,
            "generation": 4,
            "guardianGeneration": guardian_generation,
            "constructionB0": "boot-1",
            "expectedConstructorBoot": "native-construction:boot-1",
            "deferred": deferred,
            "reservationId": reservation_id,
            "closed": False,
        }
        daemon._native_construction_sealed_run_id = "native-update-budget-run"
        daemon._native_construction_operation_deadline = operation_deadline


def test_native_update_fast_eligibility_is_closed_and_revoke_blocks_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-update-fast.lock",
    )
    monkeypatch.setattr(daemon, "_native_region_enabled", lambda: True)
    identity = backend.identity
    try:
        _prime_native_update_budget_owner(daemon, driver, identity)
        provider = driver.native_measurement_provider
        capability = daemon._native_update_identity_budget_capability(provider, identity, "updates", 1)
        assert capability is not None
        assert capability.expected_state is ControllerState.NATIVE_CALIBRATION
        assert capability.identity_fingerprint == identity.fingerprint
        semantic_mutations = {
            "generation": capability.generation + 1,
            "guardian_generation": capability.guardian_generation + 1,
            "epoch": capability.epoch + 1,
            "reservation_id": "other-reservation",
            "identity_fingerprint": "other-client",
            "expected_state": ControllerState.PAUSED,
            "profile_fingerprint": "other-profile",
            "owner_pid": os.getpid() + 1,
            "owner_process_start": "other-process",
            "desktop_lease_held": False,
            "guardian_armed": True,
            "backend_id": capability.backend_id + 1,
            "b0": "other-boot",
            "expected_constructor_boot": "native-construction:other-boot",
            "expires_monotonic": capability.expires_monotonic - 1.0,
        }
        for field, value in semantic_mutations.items():
            mutated = replace(capability, **{field: value})
            with daemon._native_construction_lock:
                assert not daemon._native_update_capability_matches_locked(mutated, identity)
        # Repeat the same checks against live owner state.  A self-consistent
        # copied token must not survive a process, profile, lease, Guardian,
        # epoch, or reservation handover in the daemon itself.
        live_owner_mutations = {
            "ownerPid": os.getpid() + 1,
            "ownerProcessStart": "live-owner-replaced",
            "profileFingerprint": "live-profile-replaced",
            "controllerGeneration": capability.generation + 1,
            "guardianGeneration": capability.guardian_generation + 1,
            "B0": "live-boot-replaced",
            "expectedConstructorBoot": "native-construction:live-boot-replaced",
            "reservationId": "live-reservation-replaced",
        }
        with daemon._native_construction_lock:
            reservation = daemon._native_construction_reservation
            assert reservation is not None
            for field, value in live_owner_mutations.items():
                original = reservation[field]
                reservation[field] = value
                try:
                    assert not daemon._native_update_capability_matches_locked(capability, identity)
                finally:
                    reservation[field] = original
            original_epoch = daemon._native_construction_epoch
            daemon._native_construction_epoch += 1
            try:
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
            finally:
                daemon._native_construction_epoch = original_epoch
            context = daemon._native_construction_run_context
            assert context is not None
            original_run_id = context["runId"]
            context["runId"] = "handover-run"
            try:
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
            finally:
                context["runId"] = original_run_id
            original_b0 = reservation["B0"]
            original_echo = reservation["expectedConstructorBoot"]
            reservation["B0"] = "minted-after-owner-change"
            try:
                # A B0-only mutation must reject a newly minted witness.
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
                # The derived echo cannot make that changed owner self-consistent.
                reservation["expectedConstructorBoot"] = "native-construction:minted-after-owner-change"
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
            finally:
                reservation["B0"] = original_b0
                reservation["expectedConstructorBoot"] = original_echo
        changed_identity = replace(
            identity, process_start="fresh-process-handover", fingerprint="fresh-client-handover"
        )
        assert (
            daemon._native_update_identity_budget_capability(provider, changed_identity, "updates", 1) is None
        )
        guardian = driver.guardian
        with guardian._lock:
            original_guardian_generation = guardian.generation
            original_armed = guardian.armed
            guardian.generation += 1
            try:
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
            finally:
                guardian.generation = original_guardian_generation
            guardian.armed = True
            try:
                assert (
                    daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
                )
            finally:
                guardian.armed = original_armed
        original_lease = guardian.lease
        monkeypatch.setattr(guardian, "lease", SimpleNamespace(held=False))
        try:
            assert daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
        finally:
            monkeypatch.setattr(guardian, "lease", original_lease)
        original_profile = driver.profile
        monkeypatch.setattr(driver, "profile", replace(original_profile, wine_prefix="/handover-profile"))
        try:
            assert daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
        finally:
            monkeypatch.setattr(driver, "profile", original_profile)
        assert daemon._native_update_identity_budget_capability(provider, identity, "construct", 0) is None

        def wrapped_provider(*args: object, **kwargs: object) -> object:
            return provider(*args, **kwargs)

        assert (
            daemon._native_update_identity_budget_capability(wrapped_provider, identity, "updates", 1) is None
        )
        resolve_calls: list[int] = []
        original_resolve = driver.resolve_identity

        def count_resolve(**kwargs: object) -> WindowIdentity:
            resolve_calls.append(1)
            return original_resolve(**kwargs)

        monkeypatch.setattr(driver, "resolve_identity", count_resolve)
        daemon._check_native_poll_identity(identity, 4)
        assert resolve_calls
        with daemon._native_construction_lock:
            daemon._native_construction_run_context["deferred"] = False
        assert daemon._native_update_identity_budget_capability(provider, identity, "updates", 1) is None
        with daemon._native_construction_lock:
            daemon._native_construction_run_context["deferred"] = True
        cheap_passed = threading.Event()
        resume = threading.Event()
        failures: list[BaseException] = []
        match_calls = 0
        original_match = daemon._native_update_capability_matches_locked

        def observe_cheap_match(candidate: object, candidate_identity: object = None) -> bool:
            nonlocal match_calls
            result = original_match(candidate, candidate_identity)
            match_calls += 1
            if result and match_calls == 1:
                cheap_passed.set()
            return result

        monkeypatch.setattr(daemon, "_native_update_capability_matches_locked", observe_cheap_match)
        observer_ready = threading.Event()
        producer_starts: list[int] = []

        def capture_native_region(
            path: Path,
            *,
            identity: WindowIdentity,
            rectangle: tuple[int, int, int, int],
            observer_admission: object,
            observer_reaped: object,
            **_kwargs: object,
        ) -> WindowIdentity:
            assert rectangle == (80, 48, 256, 256)

            class Gated:
                def __init__(self, process: subprocess.Popen[bytes]) -> None:
                    self.process = process

                def release_admission_gate(self) -> None:
                    return None

            def spawn() -> Gated:
                producer_starts.append(1)
                return Gated(subprocess.Popen(["/bin/sleep", "2"]))

            observer_ready.set()
            assert resume.wait(timeout=2)
            admitted = observer_admission(spawn)
            process = admitted.process
            process.terminate()
            process.wait(timeout=2)
            observer_reaped(process.pid)
            return identity

        monkeypatch.setattr(backend, "capture_native_region", capture_native_region)

        def capture() -> None:
            try:
                daemon._capture_native_region_for_identity(
                    identity,
                    expected_generation=4,
                    expected_state=ControllerState.NATIVE_CALIBRATION,
                    native_capability=capability,
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=capture, name="native-revoke-after-cheap")

        def revoke_owner() -> None:
            assert observer_ready.wait(timeout=2)
            with daemon._native_construction_lock:
                daemon._native_construction_reservation["reservationId"] = "handover"
            resume.set()

        revoker = threading.Thread(target=revoke_owner, name="native-revoke-owner")
        revoker.start()
        worker.start()
        assert cheap_passed.wait(timeout=2)
        worker.join(timeout=2)
        revoker.join(timeout=2)
        assert not worker.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], DriverError)
        assert "capability was revoked" in str(failures[0])
        assert producer_starts == []
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize("mutation", ["backend_pre", "backend_post"])
def test_native_update_fast_identity_mutation_rejects_and_cleans_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """The eligible capture path still fences fresh identity at producer boundaries."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / f"native-fast-{mutation}.lock",
    )
    monkeypatch.setattr(daemon, "_native_region_enabled", lambda: True)
    identity = backend.identity
    producer_starts: list[int] = []
    captured_paths: list[Path] = []
    try:
        _prime_native_update_budget_owner(daemon, driver, identity)
        capability = daemon._native_update_identity_budget_capability(
            driver.native_measurement_provider, identity, "updates", 1
        )
        assert capability is not None

        def capture_native_region(
            path: Path,
            *,
            identity: WindowIdentity,
            observer_admission: Callable[[Callable[[], Any]], Any],
            observer_reaped: Callable[[int], None],
            **_kwargs: object,
        ) -> WindowIdentity:
            captured_paths.append(path)
            if mutation == "backend_pre":
                backend.identity = replace(identity, fingerprint="backend-pre-handover")
                assert backend.resolve_identity().fingerprint != identity.fingerprint
                raise DriverError("telemetry region identity changed before capture")
            assert backend.resolve_identity().fingerprint == identity.fingerprint
            Image.new("RGB", (256, 256), (0, 0, 0)).save(path)

            def spawn() -> object:
                producer_starts.append(1)
                return SimpleNamespace(process=subprocess.Popen(["/bin/sleep", "2"]))

            admitted = observer_admission(spawn)
            process = admitted.process
            backend.identity = replace(identity, fingerprint="backend-post-handover")
            process.terminate()
            process.wait(timeout=2)
            observer_reaped(process.pid)
            try:
                raise DriverError("telemetry region identity changed during capture")
            finally:
                # Match the configured producer's own failed-attempt cleanup
                # contract: a post-capture identity refusal cannot publish a
                # crop or leave its temporary directory behind.
                daemon._remove_native_capture(path)

        monkeypatch.setattr(backend, "capture_native_region", capture_native_region)
        with pytest.raises(DriverError, match="identity changed"):
            daemon._capture_native_frame_with_capture(
                identity,
                driver.new_native_decoder(),
                expected_generation=4,
                expected_state=ControllerState.NATIVE_CALIBRATION,
                native_capability=capability,
            )
        assert captured_paths and all(not path.exists() for path in captured_paths)
        assert producer_starts == ([] if mutation == "backend_pre" else [1])
    finally:
        backend.identity = identity
        daemon.request_shutdown()
        driver.close()


# The retained fast-path eligibility/revocation tests cover the same guard;
# retire this duplicate real-runner trace replay.


def test_runtime_native_budget_rejects_fabricated_bound_witness(tmp_path: Path) -> None:
    """A truthy witness from a same-owner fabricated trio cannot skip guards."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-fabricated-witness.lock",
    )
    try:
        identity = backend.identity
        runner = NativeCalibrationRunner(driver)
        provider = driver.native_measurement_provider
        original_eligibility = driver._native_identity_budget_eligibility
        original_verifier = driver._native_identity_budget_capability_valid

        def fabricated_eligibility(*_args: object) -> object:
            return object()

        def fabricated_verifier(*_args: object) -> bool:
            return True

        # Bind both fabricated methods to the real daemon so owner identity
        # alone cannot make this pass; implementation identity is required.
        driver._native_identity_budget_eligibility = fabricated_eligibility.__get__(daemon, type(daemon))
        driver._native_identity_budget_capability_valid = fabricated_verifier.__get__(daemon, type(daemon))
        try:
            assert runner._native_update_budget_capability(provider, identity, 1) is None
        finally:
            driver._native_identity_budget_eligibility = original_eligibility
            driver._native_identity_budget_capability_valid = original_verifier
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_budget_rejects_same_shape_foreign_owner(tmp_path: Path) -> None:
    """Matching private method names on another owner are not daemon proof."""

    backend = _NativeProviderBackend(tmp_path, load_after_captures=0)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-foreign-witness.lock",
    )

    class ForeignOwner:
        def __init__(self, bound_driver: DesktopDriver) -> None:
            self.driver = bound_driver

        def _native_measurement_provider(self, *_args: object) -> object:
            return object()

        def _native_update_identity_budget_capability(self, *_args: object) -> object:
            return object()

        def _native_update_identity_budget_capability_valid(self, *_args: object) -> bool:
            return True

    try:
        foreign = ForeignOwner(driver)
        runner = NativeCalibrationRunner(driver)
        provider = foreign._native_measurement_provider
        driver._native_identity_budget_eligibility = foreign._native_update_identity_budget_capability
        driver._native_identity_budget_capability_valid = (
            foreign._native_update_identity_budget_capability_valid
        )
        assert runner._native_update_budget_capability(provider, backend.identity, 1) is None
        assert foreign.driver is driver
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_provider_absence_times_out_and_cleans_artifacts(tmp_path: Path) -> None:
    backend = _NativeProviderBackend(tmp_path, load_after_captures=None)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    try:
        with pytest.raises(DriverError, match="did not produce the requested fresh frame"):
            daemon._native_measurement_provider(backend.identity, "construct", 0)
        assert backend.agent_client is None
        assert backend.captures >= 30
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_provider_generation_revocation_cleans_artifacts(tmp_path: Path) -> None:
    backend = _NativeProviderBackend(tmp_path, load_after_captures=None)
    daemon, _backend, driver, controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    failures: list[BaseException] = []

    def wait_for_frame() -> None:
        try:
            daemon._native_measurement_provider(backend.identity, "construct", 0)
        except BaseException as exc:  # the assertion below reports the exact provider failure
            failures.append(exc)

    worker = threading.Thread(target=wait_for_frame, name="runtime-native-provider")
    try:
        worker.start()
        assert backend.capture_started.wait(timeout=2)
        controller.pause("runtime native generation revoked")
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], DriverError)
        assert "generation was revoked" in str(failures[0])
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        if worker.is_alive():
            daemon.request_shutdown()
            worker.join(timeout=2)
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("wrong_phase", "native construct/0 returned 'held'/0"),
        ("skipped_ordinal", "native updates/1 returned 'updates'/3"),
    ],
)
def test_runtime_native_provider_rejects_wrong_or_skipped_real_publication(
    tmp_path: Path, fault: str, message: str
) -> None:
    backend = _NativeProviderBackend(tmp_path, publication_fault=fault if fault == "wrong_phase" else None)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    with daemon._native_construction_lock:
        daemon._native_construction_handshake = {"state": "running"}
        daemon._native_construction_reset_history_locked()
        daemon._native_construction_start_context_locked(generation=4, deferred=False)
    try:
        if fault == "wrong_phase":
            with pytest.raises(DriverError, match=re.escape(message)):
                daemon._native_measurement_provider(backend.identity, "construct", 0)
        else:
            constructed = daemon._native_measurement_provider(backend.identity, "construct", 0)
            daemon.release_native_capture(Path(constructed.capture_path))
            backend.publication_fault = "skipped_ordinal"
            with pytest.raises(DriverError, match=re.escape(message)):
                daemon._native_measurement_provider(backend.identity, "updates", 1)
        diagnostic_path = daemon._native_observation_diagnostic.failure_path
        assert diagnostic_path is not None and diagnostic_path.is_file()
        diagnostic_payload = json.loads(diagnostic_path.read_text())
        assert diagnostic_payload["failure"]["stage"] in {"construct", "updates"}
        assert diagnostic_payload["failure"]["ordinal"] in {0, 1}
        assert diagnostic_payload["failure"]["offending_capture_name"] == "offending-capture.png"
        offending = diagnostic_path.parent / "offending-capture.png"
        assert offending.is_file()
        with Image.open(offending) as image:
            assert image.size == (256, 256)
        assert (
            diagnostic_payload["failure"]["offending_capture_sha256"]
            == hashlib.sha256(offending.read_bytes()).hexdigest()
        )
        assert diagnostic_payload["source_drift"] is False
        assert diagnostic_payload["source_binding_error"] is None
        assert diagnostic_payload["source_binding_start_error"] is None
        assert diagnostic_payload["calibration_source_error"] is None
        original_run_id = diagnostic_payload["run_id"]
        original_attempts = diagnostic_payload["attempts"]
        # The production runner reports the provider failure and then its outer
        # exception path reports again.  The latter must remain idempotent: one
        # run, one directory, and the first causal reason/ring are retained.
        daemon.notify_native_measurement_failed(backend.identity, "outer native calibration failure")
        diagnostic_dirs = list((tmp_path / "native-observation-diagnostics").iterdir())
        assert len(diagnostic_dirs) == 1
        refreshed = json.loads(diagnostic_path.read_text())
        assert refreshed["run_id"] == original_run_id
        assert refreshed["failure"]["reason"] == diagnostic_payload["failure"]["reason"]
        assert refreshed["attempts"] == original_attempts
        assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o600
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_observation_trace_write_failure_preserves_provider_error_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _NativeProviderBackend(tmp_path, publication_fault="wrong_phase")
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    diagnostic = daemon._native_observation_diagnostic

    def fail_write(_payload: dict[str, object]) -> Path:
        raise OSError("diagnostic filesystem unavailable")

    monkeypatch.setattr(diagnostic, "_write_payload", fail_write)
    try:
        with pytest.raises(DriverError, match=re.escape("native construct/0 returned 'held'/0")):
            daemon._native_measurement_provider(backend.identity, "construct", 0)
        assert diagnostic.failure_path is None
        assert diagnostic._persistence_error is not None
        assert not list(tmp_path.rglob("offending-capture.png"))
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_observation_cleanup_failure_is_retained_and_traced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _NativeProviderBackend(tmp_path, publication_fault="wrong_phase")
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    with daemon._native_construction_lock:
        daemon._native_construction_handshake = {"state": "running"}
        daemon._native_construction_reset_history_locked()
        daemon._native_construction_start_context_locked(generation=4, deferred=False)
    original_remove = daemon._remove_native_capture
    monkeypatch.setattr(daemon, "_remove_native_capture", lambda _path: None)
    try:
        with pytest.raises(DriverError, match="native construct/0 returned 'held'/0"):
            daemon._native_measurement_provider(backend.identity, "construct", 0)
        diagnostic_path = daemon._native_observation_diagnostic.failure_path
        assert diagnostic_path is not None and diagnostic_path.is_file()
        payload = json.loads(diagnostic_path.read_text())
        attempt = payload["attempts"][-1]
        assert attempt["release_outcome"] == "retained"
        assert attempt["spans"]["cleanup"]["outcome"] == "failed"
        assert payload["failure"]["reason"] == "native construct/0 returned 'held'/0"
    finally:
        monkeypatch.setattr(daemon, "_remove_native_capture", original_remove)
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_observation_identity_mutation_preserves_error_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _NativeProviderBackend(tmp_path)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    original_capture = backend.capture

    def mutate_during_capture(path: Path, **kwargs: object) -> WindowIdentity:
        captured = original_capture(path, **kwargs)
        backend.identity = replace(backend.identity, fingerprint="mutated-identity")
        return captured

    monkeypatch.setattr(backend, "capture", mutate_during_capture)
    try:
        identity = backend.identity
        with pytest.raises(DriverError, match="native client identity changed during operator wait"):
            constructed = daemon._native_measurement_provider(identity, "construct", 0)
            daemon.release_native_capture(Path(constructed.capture_path))
            daemon._native_measurement_provider(identity, "held", 0)
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_runtime_native_provider_cached_publication_times_out_cleanly(tmp_path: Path) -> None:
    backend = _NativeProviderBackend(tmp_path)
    daemon, _backend, driver, _controller, _config, _clock = _runtime_daemon(
        tmp_path,
        backend=backend,
        journal_changes={"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 4},
        lease_path=tmp_path / "native-provider.lock",
    )
    try:
        constructed = daemon._native_measurement_provider(backend.identity, "construct", 0)
        daemon.release_native_capture(Path(constructed.capture_path))
        backend.advance_cached_clock = True
        with pytest.raises(DriverError, match="did not produce the requested fresh frame"):
            daemon._native_measurement_provider(backend.identity, "held", 0)
        _assert_native_provider_cleanup(tmp_path, backend, daemon)
    finally:
        daemon.request_shutdown()
        driver.close()
