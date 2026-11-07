"""Small end-to-end mail-loop proofs over the public session path.

The fixture models the owned capture/input boundary only.  Route planning,
motion admission, mailbox hover/click, executor admission, Lua publication,
pixel decoding, IPC, scheduling, and AH reopening remain production code.
"""

from __future__ import annotations

import copy
import hashlib
import math
import subprocess
import time
import traceback
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crowley.core.queue import validate_queue
from crowley.core.watchlist import load_catalog
from crowley.session import SessionConfig
from crowley.session.cli import SessionIPC
from crowley.session.controller import Controller, ControllerState
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import PixelDecoder
from crowley.session.driver import (
    DesktopDriver,
    DriverProfile,
    X11Backend,
)
from crowley.session.evidence import read_data_acknowledged_ids
from crowley.session.input_events import EventClass
from crowley.session.journal import DEFAULT_STATE, Journal
from crowley.session.model import ModelObservation
from crowley.session.route_walk import KeyEvent, Pose, Route, RouteFile, simulate
from crowley.session.startup import StartupEnvironment, mint_runtime_evidence
from tests.support.companion_state import write_companion_state

from .test_session_runtime import (
    LifecycleBackend,
    _FixtureInputObserver,
    _FixtureLunaAdapter,
    _RuntimeFixtureModel,
    _VirtualMonotonic,
)
from .wow_harness import WowClient

UID = "mail-composition-ack-1"
WALL_TIME = 1_760_000_000
QUEUE_ID = f"beta-normal-alliance-Arthur-{WALL_TIME}-a1b2"
ROUTES = Path("deploy/config/routes/stormwind.json").resolve()


def _trace_kwargs(kwargs: dict) -> dict[str, str]:  # type: ignore[type-arg]
    """Keep bounded, printable kwargs for a missing-deadline diagnostic."""

    return {str(key): repr(value) for key, value in kwargs.items()}


def _compact_traceback() -> tuple[str, ...]:
    """Capture the caller chain without changing the wrapped call."""

    return tuple(
        f"{frame.filename}:{frame.lineno}:{frame.name}:{(frame.line or '').strip()}"
        for frame in traceback.extract_stack(limit=14)
    )


def _queue(*, armed: bool, max_messages: int) -> dict:
    actions = [{"seq": 1, "kind": "mail_collect", "maxMessages": max_messages}]
    if armed:
        actions.append({"seq": 2, "kind": "craft", "recipeID": 2963, "times": 1})
    return {
        "format": 1,
        "queueId": QUEUE_ID,
        "generatedAt": WALL_TIME,
        "expiresAt": WALL_TIME + 100_000,
        "house": "beta-normal-alliance",
        "character": "Arthur",
        "armed": armed,
        "actions": actions,
    }


def _render_data_lua(client: WowClient, path: Path) -> None:
    """Render and execute the actual Data.lua before ADDON_LOADED/login."""

    plan = client._json_table(client.lua.globals().CROWLEY_DATA.scanPlan)
    value = {
        "generatedAt": WALL_TIME,
        "ackedScans": [UID],
        "scanPlan": plan,
        "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
    }
    path.write_bytes(client.lua.globals()._serialize("CROWLEY_DATA", client._to_lua(value)).encode("latin-1"))
    client.run_file(path)
    assert read_data_acknowledged_ids(
        path,
        expected_plan_json=plan["planJSON"],
        expected_plan_hash=plan["planHash"],
        expected_client_build=plan["targetClientBuild"],
    ) == (UID,)


class _MailBackend(LifecycleBackend):
    """Owned capture/input boundary with production Lua state behind it."""

    def __init__(self, root: Path, *, armed: bool, max_messages: int) -> None:
        super().__init__(root)
        self.lifecycle_agent = True
        packaged_routes = RouteFile.from_json(ROUTES.read_text(encoding="utf-8"))
        anchor = packaged_routes.routes["auctioneer_to_mailbox"].start
        # Keep both tiny modeled legs outside the arrival tolerance while
        # making each endpoint face the next leg's measured travel direction;
        # this guarantees a real guarded key timeline without a long final
        # corrective turn dominating the bounded composition case.
        mailbox = Pose(anchor.x + 1.2, anchor.y, 0.0)
        return_pose = Pose(mailbox.x + 1.2, mailbox.y, 0.0)
        fixture_style = replace(
            packaged_routes.style,
            forward_gap=packaged_routes.style.forward_gap[:3],
            key_gap=packaged_routes.style.key_gap[:3],
        )
        self.route_file = RouteFile(
            routes={
                "auctioneer_to_mailbox": Route(
                    "auctioneer_to_mailbox",
                    1,
                    packaged_routes.routes["auctioneer_to_mailbox"].instance,
                    anchor,
                    mailbox,
                    ((anchor.x, anchor.y), (anchor.x + 0.6, anchor.y), (mailbox.x, mailbox.y)),
                    "fixture-mailbox",
                ),
                "mailbox_to_auctioneer": Route(
                    "mailbox_to_auctioneer",
                    1,
                    packaged_routes.routes["auctioneer_to_mailbox"].instance,
                    mailbox,
                    return_pose,
                    ((mailbox.x, mailbox.y), (mailbox.x + 0.6, mailbox.y), (return_pose.x, return_pose.y)),
                    "fixture-auctioneer",
                ),
            },
            style=fixture_style,
            kinematics=packaged_routes.kinematics,
            sources=("packaged Stormwind beta route endpoints; bounded fixture segment",),
        )
        self.routes_path = root / "mail-routes.json"
        self.routes_path.write_text(self.route_file.to_json(), encoding="utf-8")
        self._keymap: dict[str, int] = {}
        self.command_buffer = ""
        self.keyboard_effects: list[dict[str, object]] = []
        self._character_hud_pending = False
        self.character_hud_flushes = 0
        self._route_name = ""
        self.route_timelines: list[tuple[str, tuple[KeyEvent, ...], Pose, Pose]] = []
        self.native_calls: list[dict[str, object]] = []
        self.action_receipts: list[dict[str, object]] = []
        self.operation_trace: list[dict[str, object]] = []
        self._operation_stack: list[dict[str, object]] = []
        self._pending_world_operation: dict[str, object] | None = None
        self._pending_return_operation: dict[str, object] | None = None
        self.prepared_commands: list[str] = []
        self.reload_calls = 0
        self.wire_records: list[dict[str, object]] = []
        self.model_route_endpoint: tuple[float, float] | None = None
        self.last_model_capture_digest = ""
        self._world_clicks = 0
        self._executor_clicks = 0
        self._mail_messages = max_messages
        self.queue = _queue(armed=armed, max_messages=max_messages)
        assert validate_queue(self.queue)["queueId"] == QUEUE_ID
        self.data_lua = root / "Data.lua"
        self.saved_variables = root / "Crowley.lua"
        self._configure_client(armed=armed)

        mailbox = self.route_file.routes["auctioneer_to_mailbox"]
        self.current["motion"]["position"] = {
            "x": mailbox.start.x,
            "y": mailbox.start.y,
            "z": 30.75,
            "instance": mailbox.instance,
        }
        self.current["motion"]["facing"] = mailbox.start.facing
        self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
        self.current["ahOpen"] = True
        self.current["mailOpen"] = False
        self._publish_lifecycle_agent()
        self.saved_variables.write_bytes(self.agent_client.saved_variables())

    def _configure_client(self, *, armed: bool) -> None:
        client = WowClient(
            faction="Alliance",
            realm="Classic Beta PvE 2",
            zone="Stormwind City",
            current_region=110,
            current_region_name="beta",
            queue=self.queue,
        )
        client.set_player_name("Arthur")
        client.set_canonical_plan()
        client.lua.globals()._now = WALL_TIME
        _render_data_lua(client, self.data_lua)
        # Execute the rendered Data.lua before ADDON_LOADED/PLAYER_LOGIN.
        client.login()
        client.set_mail_items([{"money": 20} for _ in range(self._mail_messages)])
        client.state.mailOpen = False
        client.ns.ahOpen = True
        client.set_chat_edit(active=False, focused=False)
        if armed:
            client.set_auto_postconditions(False)
        self.agent_client = client

    def _sync_lua_from_current(self) -> None:
        client = self.agent_client
        assert client is not None
        motion = self.current["motion"]
        position = motion["position"]
        client.position = (
            float(position["x"]),
            float(position["y"]),
            float(position["z"]),
            int(position["instance"]),
        )
        client.facing = float(motion["facing"])
        client.speed = float(motion["speed"])
        client.state.positionX = float(position["x"])
        client.state.positionY = float(position["y"])
        client.state.positionZ = float(position["z"])
        client.state.positionInstance = int(position["instance"])
        client.state.facing = float(motion["facing"])
        client.state.speed = float(motion["speed"])
        client.state.mailOpen = bool(self.current["mailOpen"])
        client.ns.ahOpen = bool(self.current["ahOpen"])
        client.set_chat_edit(**self.current["chatEdit"])
        auctioneer = self.current["auctioneer"]
        client.state.targetPresent = bool(auctioneer["targetPresent"])
        client.state.targetName = auctioneer["targetName"]
        client.state.rangeAvailable = bool(auctioneer["rangeAvailable"])
        client.state.withinTradeRange = bool(auctioneer["withinTradeRange"])
        client.state.bindingAvailable = bool(auctioneer["bindingAvailable"])
        client.state.ctrlF12Action = auctioneer["ctrlF12Action"]
        hover = self.current["hover"]
        if hover["kind"] == "object":
            client.set_hover_object(hover["name"])
        else:
            client.clear_hover()

    def _publish_lifecycle_agent(self) -> None:
        had_pending_character_hud = self._character_hud_pending
        self._sync_lua_from_current()
        client = self.agent_client
        assert client is not None
        self.lifecycle_sequence += 1
        client.publish_agent()
        raw = client.agent_frame
        sample = PixelDecoder().decode(
            client.render_agent_capture(),
            monotonic=time.monotonic(),
            window_identity=self.identity.fingerprint,
        )
        assert sample.kind == "FRESH" and sample.frame is not None
        self.wire_records.append(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "length": len(raw),
                "sequence": sample.frame.sequence,
                "snapshot": copy.deepcopy(sample.frame.snapshot.value),
            }
        )
        self._character_hud_pending = False
        if had_pending_character_hud:
            self.character_hud_flushes += 1
        # Keep the model fixture's scene facts tied to the same production Lua
        # snapshot that the daemon will decode from AgentFrameBytes.
        decoded = client.snapshot_agent()
        for key in (
            "ahOpen",
            "mailOpen",
            "hover",
            "context",
            "motion",
            "scanner",
            "loaded",
            "pending",
            "executor",
            "blockingUI",
            "auctioneer",
        ):
            if key in decoded:
                self.current[key] = copy.deepcopy(decoded[key])
        self.current["sequence"] = self.lifecycle_sequence

    def _route_for_pose(self) -> tuple[str, object]:
        pose = self.current["motion"]["position"]
        point = (float(pose["x"]), float(pose["y"]))
        choices = sorted(
            (
                math.dist(point, (route.start.x, route.start.y)),
                name,
                route,
            )
            for name, route in self.route_file.routes.items()
        )
        distance, name, route = choices[0]
        if distance > 2.0:
            raise AssertionError(f"route fixture pose is not at a packaged route start: {point}")
        self._route_name = name
        return name, route

    def _parse_motion_events(self, args: list[str]) -> tuple[KeyEvent, ...]:
        current = 0.0
        events: list[KeyEvent] = []
        logical = {
            "w": "W",
            "W": "W",
            "q": "Left",
            "Q": "Left",
            "e": "Right",
            "E": "Right",
            "space": "space",
        }
        index = 1
        while index < len(args):
            token = args[index]
            if token == "sleep":
                current += float(args[index + 1])
                index += 2
                continue
            if token not in {"keydown", "keyup"}:
                raise AssertionError(f"unexpected route input token: {args}")
            keysym = args[index + 1]
            events.append(KeyEvent(current, logical[keysym], token == "keydown"))
            index += 2
        return tuple(events)

    def _emit_key_events(self, args: list[str], *, route_symbols: bool = False) -> None:
        observer = self.runtime_observer
        if observer is None:
            raise AssertionError("mail fixture has no owned input observer")
        index = 1
        held_modifiers: set[str] = set()
        modifier_masks = {"Shift_L": 1, "Control_L": 4}
        inverse = {f"{code:03d}": keysym for keysym, code in self._keymap.items()}
        while index < len(args):
            if args[index] == "sleep":
                index += 2
                continue
            operation, raw_code = args[index], args[index + 1]
            code = self._keymap[raw_code] if route_symbols else int(raw_code)
            keysym = raw_code if route_symbols else inverse.get(raw_code, "")
            if not keysym:
                raise AssertionError(f"keyboard event lacks measured keysym: {args!r}")
            modifier_value = sum(modifier_masks.get(key, 0) for key in held_modifiers)
            # The sealed runtime profile stores the X11 mask in the base
            # component of the XI2 modifier triple; preserve that exact
            # measured representation for delivered events.
            modifiers = (0, 0, modifier_value)
            raw = EventClass.RAW_KEY_PRESS if operation == "keydown" else EventClass.RAW_KEY_RELEASE
            delivered = EventClass.KEY_PRESS if operation == "keydown" else EventClass.KEY_RELEASE
            observer._add(self._runtime_event(raw, master=3, source=5, detail=code, child=None))
            observer._add(
                self._runtime_event(
                    delivered,
                    master=5,
                    source=5,
                    detail=code,
                    child=self.identity.child_xid,
                    modifiers=modifiers,
                    modifier_effective=modifier_value,
                )
            )
            if operation == "keydown" and keysym in modifier_masks:
                held_modifiers.add(keysym)
            elif operation == "keyup" and keysym in modifier_masks:
                held_modifiers.discard(keysym)
            index += 2

    def _decode_keyboard_steps(self, args: list[str]) -> tuple[str, ...]:
        if len(args) < 3 or args[0] != "xdotool" or (len(args) - 1) % 2:
            raise AssertionError(f"malformed keyboard-steps argv: {args!r}")
        inverse: dict[str, str] = {}
        for keysym, code in self._keymap.items():
            inverse.setdefault(f"{code:03d}", keysym)
        symbols: list[str] = []
        for index in range(1, len(args), 2):
            symbol = inverse.get(args[index + 1])
            if symbol is None:
                raise AssertionError(f"keyboard step lacks measured keysym: {args!r}")
            symbols.append(symbol)
        return tuple(symbols)

    @staticmethod
    def _typed_character(symbols: tuple[str, ...]) -> str | None:
        named = {
            "slash": "/",
            "space": " ",
            "minus": "-",
            "equal": "=",
            "semicolon": ";",
            "apostrophe": "'",
            "comma": ",",
        }
        if len(symbols) == 2 and symbols[0] == symbols[1]:
            symbol = symbols[0]
            if len(symbol) == 1 and symbol.isascii() and symbol.isalnum():
                return symbol
            return named.get(symbol)
        if len(symbols) == 4 and symbols[0] == symbols[3] == "Shift_L" and symbols[1] == symbols[2]:
            symbol = symbols[1]
            if symbol in {"9", "0"}:
                return {"9": "(", "0": ")"}[symbol]
            if len(symbol) == 1 and symbol.isascii() and symbol.isalnum():
                return symbol.upper()
            return None
        return None

    def _apply_keyboard_steps(self, args: list[str]) -> None:
        symbols = self._decode_keyboard_steps(args)
        client = self.agent_client
        if client is None:
            raise AssertionError("keyboard effect has no Lua client")
        if symbols == ("Return", "Return"):
            guardian = self.runtime_driver.guardian if self.runtime_driver is not None else None
            if guardian is not None and guardian.action is not None and not guardian.action.complete:
                raise AssertionError(
                    "Return native event did not complete Guardian action: "
                    f"revoked={guardian.revoked_reason!r} action={guardian.action!r}"
                )
            if self.current["chatEdit"]["active"] and self.current["chatEdit"]["focused"]:
                command = self.command_buffer
                self.command_buffer = ""
                self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
                self.keyboard_effects.append({"kind": "submit_command", "command": command})
                if command == DesktopDriver.TARGET_AUCTIONEER_COMMAND:
                    self.current["auctioneer"].update(
                        targetPresent=True,
                        targetName="Auctioneer Jaxon",
                        rangeAvailable=True,
                        withinTradeRange=True,
                    )
                elif command == DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND:
                    self.current["auctioneer"]["ctrlF12Action"] = "INTERACTTARGET"
                else:
                    raise AssertionError(f"unexpected submitted keyboard command: {command!r}")
            else:
                self.current["chatEdit"] = {"active": True, "focused": True, "text": ""}
                self.keyboard_effects.append({"kind": "open_chat"})
            self._publish_lifecycle_agent()
            return
        if symbols == ("Escape", "Escape"):
            self.command_buffer = ""
            self.current["chatEdit"] = {"active": False, "focused": False, "text": ""}
            self.keyboard_effects.append({"kind": "escape_command_buffer"})
            self._publish_lifecycle_agent()
            return
        if symbols == ("Control_L", "F12", "F12", "Control_L"):
            if (
                self.current["chatEdit"] != {"active": False, "focused": False, "text": ""}
                or self.current["auctioneer"]["targetPresent"] is not True
                or self.current["auctioneer"]["ctrlF12Action"] != "INTERACTTARGET"
            ):
                raise AssertionError("CTRL-F12 was emitted before target and binding proof")
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
            self.keyboard_effects.append({"kind": "open_ah", "target": "Auctioneer Jaxon"})
            self._publish_lifecycle_agent()
            return
        character = self._typed_character(symbols)
        if character is None:
            raise AssertionError(f"unmodeled keyboard-steps effect: {symbols!r}")
        if self.current["chatEdit"] != {"active": True, "focused": True, "text": self.command_buffer}:
            raise AssertionError("typed command key was emitted outside the focused chat buffer")
        self.command_buffer += character
        self.current["chatEdit"]["text"] = self.command_buffer
        self.keyboard_effects.append({"kind": "type_character", "character": character})
        # Character input still emits every measured key/modifier event and
        # mutates the real command buffer above.  The HUD publication/render/
        # decode is intentionally deferred until the next semantic event or
        # actual capture/readback, where _publish_lifecycle_agent executes the
        # real Lua serializer and fresh PixelDecoder path once for the batch.
        self._character_hud_pending = True

    def _emit_motion_events(self, args: list[str]) -> None:
        observer = self.runtime_observer
        if observer is None:
            raise AssertionError("mail fixture has no owned input observer")
        driver = self.runtime_driver
        if driver is None:
            raise AssertionError("mail fixture has no runtime driver")
        left, top, _width, _height = self.identity.child_geometry
        previous_root = (float(left + self.pointer[0]), float(top + self.pointer[1]))
        index = 1
        while index < len(args):
            if args[index] == "sleep":
                index += 2
                continue
            if args[index] != "mousemove":
                raise AssertionError(f"unexpected pointer input token: {args}")
            root_x, root_y = int(args[index + 1]), int(args[index + 2])
            x, y = root_x - left, root_y - top
            endpoint = (float(root_x), float(root_y))
            origin = previous_root
            valuators = ((0, float(root_x), None), (1, float(root_y), None))
            action = driver.guardian.action
            canonical_motion = bool(action is not None and action.canonical_motion)
            delivered_root = origin if canonical_motion else endpoint
            delivered_origin = origin if canonical_motion else (endpoint[0] - left, endpoint[1] - top)
            delivered_endpoint = endpoint if canonical_motion else (endpoint[0] - left, endpoint[1] - top)
            raw = replace(
                self._runtime_event(
                    EventClass.RAW_MOTION,
                    master=2,
                    source=4,
                    detail=0,
                    child=None,
                ),
                valuators=valuators,
            )
            event = replace(
                self._runtime_event(
                    EventClass.MOTION,
                    master=4,
                    source=4,
                    detail=0,
                    child=self.identity.child_xid,
                    root_coordinates=delivered_root,
                    event_coordinates=delivered_origin,
                ),
                valuators=valuators,
                timestamp_ms=raw.timestamp_ms,
            )
            for emitted in (
                raw,
                event,
                self._runtime_event(
                    EventClass.MOTION,
                    master=10,
                    source=10,
                    detail=0,
                    child=self.identity.child_xid,
                    root_coordinates=endpoint,
                    event_coordinates=delivered_endpoint,
                    modifiers=(0, 0, 0),
                    modifier_effective=0,
                ),
            ):
                observer._add(emitted)
                if driver.guardian.revoked_reason:
                    return
            previous_root = endpoint
            self.pointer = (x, y)
            index += 3

    @staticmethod
    def _record_nested_deadline(operation, deadline, boundary: str) -> None:  # type: ignore[no-untyped-def]
        operation.setdefault("deadlines", []).append(deadline)
        if deadline is None:
            return
        if operation.get("firstNestedDeadline") is None:
            operation["firstNestedDeadline"] = deadline
            operation["firstNestedBoundary"] = boundary
        if operation.get("name") == "postcondition" and operation.get("observedDeadline") is None:
            operation["observedDeadline"] = deadline
            operation["observedBoundary"] = boundary

    def capture(self, path: Path, **kwargs):  # type: ignore[no-untyped-def]
        result = super().capture(path, **kwargs)
        operation = self._operation_stack[-1] if self._operation_stack else None
        if operation is not None:
            deadline = kwargs.get("deadline_monotonic")
            self._record_nested_deadline(operation, deadline, "capture")
        self.native_calls.append(
            {
                "kind": "capture",
                "path": str(path),
                "deadline": kwargs.get("deadline_monotonic"),
                "kwargs": _trace_kwargs(kwargs),
                "operation": operation.get("name") if operation is not None else None,
            }
        )
        return result

    def _run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        schema = kwargs.get("input_schema")
        operation = self._operation_stack[-1] if self._operation_stack else None
        if operation is not None:
            deadline = kwargs.get("deadline_monotonic")
            self._record_nested_deadline(operation, deadline, "_run")
        self.native_calls.append(
            {
                "kind": "_run",
                "schema": schema,
                "argv": tuple(args),
                "deadline": kwargs.get("deadline_monotonic"),
                "kwargs": _trace_kwargs(kwargs),
                "operation": operation.get("name") if operation is not None else None,
            }
        )
        if len(args) >= 2 and args[1] == "windowactivate":
            return subprocess.CompletedProcess(args, 0, "", "")
        if schema == "motion-timeline":
            events = self._parse_motion_events(args)
            route_name, route = self._route_for_pose()
            pose_value = self.current["motion"]["position"]
            start = Pose(
                float(pose_value["x"]),
                float(pose_value["y"]),
                float(self.current["motion"]["facing"]),
            )
            expected = simulate(events, start, self.route_file.kinematics)[-1][1]
            self.route_timelines.append((route_name, events, start, expected))
            self.current["motion"]["position"].update(x=expected.x, y=expected.y)
            self.current["motion"]["facing"] = expected.facing
            self.current["motion"]["speed"] = 0
            if route_name == "mailbox_to_auctioneer" and self.current["mailOpen"]:
                self.current["mailOpen"] = False
                client = self.agent_client
                assert client is not None
                client.set_frame_open("mailbox", False)
            self._emit_key_events(args, route_symbols=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if schema == "keyboard-steps":
            self._emit_key_events(args)
            self._apply_keyboard_steps(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if schema == "pointer-motion":
            self._emit_motion_events(args)
            self.current["hover"] = {"kind": "object", "name": "Mailbox"}
            return subprocess.CompletedProcess(args, 0, "", "")
        if schema == "single-click":
            x, y = self.pointer
            self._emit_runtime_pointer_click(
                1,
                coordinates=(
                    (float(self.identity.child_geometry[0] + x), float(self.identity.child_geometry[1] + y)),
                    (float(x), float(y)),
                ),
            )
            client = self.agent_client
            assert client is not None
            if not self.current["mailOpen"]:
                self._world_clicks += 1
                self.current["mailOpen"] = True
                self.current["ahOpen"] = False
                client.set_frame_open("mailbox", True)
            else:
                self._executor_clicks += 1
                client.click_executor()
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unmodeled native fixture command: {args!r} ({schema!r})")

    def action(self, name: str, **kwargs):  # type: ignore[no-untyped-def]
        self.events.append({"name": name, **kwargs})
        operation = self._operation_stack[-1] if self._operation_stack else None
        if operation is not None:
            deadline = kwargs.get("deadline_monotonic")
            self._record_nested_deadline(operation, deadline, "action")
        # Exercise the reviewed X11Backend action implementation.  Only the
        # fixture's lower _run boundary models the post-emission game state;
        # LifecycleBackend.action would be a closed-loop action emulator.
        result = X11Backend.action(self, name, **kwargs)
        if name == "type_allowlisted_command" and self.command_buffer != kwargs.get("command"):
            raise AssertionError(
                "lower keyboard model diverged from emitted command: "
                f"expected={kwargs.get('command')!r} actual={self.command_buffer!r} "
                f"effects={self.keyboard_effects[-8:]!r}"
            )
        self.action_receipts.append(
            {
                "name": name,
                "deadline": kwargs.get("deadline_monotonic"),
                "nativeCallCount": len(self.native_calls),
                "operation": operation.get("name") if operation is not None else None,
                "kwargs": _trace_kwargs(kwargs),
                "result": copy.deepcopy(result),
            }
        )
        if name == "open_ah" and self.current["ahOpen"] is not True:
            raise AssertionError("real X11Backend.action did not produce AH-open from emitted CTRL-F12")
        if name in {"open_ah", "executor_click"} and self.agent_client is not None:
            self._publish_lifecycle_agent()
        return result


class _MailModel(_FixtureLunaAdapter):
    def __init__(self, backend: _MailBackend) -> None:
        super().__init__(backend)
        self.backend = backend

    def observe(
        self, image, *, target, verified_crop, requested_state=None, requested_control=None, timeout=None
    ):
        if requested_state == "mailbox_world":
            route = self.backend.route_file.routes["auctioneer_to_mailbox"]
            position = self.backend.current["motion"]["position"]
            actual_endpoint = (float(position["x"]), float(position["y"]))
            route_endpoint = (float(route.end.x), float(route.end.y))
            if math.dist(actual_endpoint, route_endpoint) > 0.05:
                raise AssertionError(
                    f"mailbox model observed pose {actual_endpoint}, expected route endpoint {route_endpoint}"
                )
            image_digest = hashlib.sha256(bytes(image)).hexdigest()
            if image_digest != self.backend.last_model_capture_digest:
                raise AssertionError("mailbox model did not receive the retained capture bytes")
            self.backend.model_route_endpoint = actual_endpoint
            crop_w, crop_h = verified_crop[2], verified_crop[3]
            x = int(abs(round(route_endpoint[0] * 100))) % (crop_w - 64)
            y = int(abs(round(route_endpoint[1] * 100))) % (crop_h - 64)
            return ModelObservation(
                "mailbox_world",
                {
                    "scene": "world",
                    "control": "mailbox_object",
                    "character": "Arthur",
                    "realm": "Classic Beta PvE 2",
                    "modal": "",
                    "code": "",
                    "idle": "true",
                },
                anchor=(x, y, 64, 64),
            )
        return super().observe(
            image,
            target=target,
            verified_crop=verified_crop,
            requested_state=requested_state,
            requested_control=requested_control,
            timeout=timeout,
        )


def _install_runtime_input(driver: DesktopDriver, backend: _MailBackend) -> object:
    hierarchy = (
        (2, 3, "master", "pointer"),
        (3, 2, "master", "keyboard"),
        (4, 2, "slave", "XTEST pointer"),
        (5, 3, "slave", "XTEST keyboard"),
        (7, 2, "slave", "human pointer"),
        (9, 3, "slave", "human keyboard"),
        (10, 2, "slave", "pointer:xwayland-relative-pointer:17"),
    )
    keymap = backend.observe_keymap()
    keymap.update({str(digit): 100 + digit for digit in range(10) if str(digit) not in keymap})
    # The shared lifecycle fixture intentionally uses a compact teaching
    # keymap where number-row/punctuation slots overlap alphabet keycodes.
    # The native runtime contract requires one physical code per measured
    # level-0 keysym for this composition proof, so preserve the observed
    # bindings while assigning the colliding helper slots unused physical
    # codes before sealing the private fixture evidence.
    used_codes = set(keymap.values())
    for keysym in ("9", "0", "minus", "equal"):
        code = keymap[keysym]
        if sum(existing == code for existing in keymap.values()) > 1:
            code = max(used_codes) + 1
            keymap[keysym] = code
            used_codes.add(code)
    levels_by_code: dict[int, list[str]] = {}
    for symbol, code in keymap.items():
        levels_by_code.setdefault(code, []).append(symbol)
    levels = {code: tuple(sorted(set(symbols))) for code, symbols in levels_by_code.items()}
    backend._keymap = keymap
    backend.observe_keymap = lambda **_kwargs: keymap  # type: ignore[method-assign]
    driver.input_hierarchy = lambda **_kwargs: hierarchy  # type: ignore[method-assign]
    backend.observe_keymap_levels = lambda **_kwargs: levels  # type: ignore[method-assign]
    backend.observe_native_input_state = lambda _root: ((0, 0, 0), (), ())  # type: ignore[method-assign]
    keyboard_profile = DesktopDriver._runtime_keyboard_profile(
        tuple(driver.COMMANDS), levels, keymap, (0, 0, 0), (1, 4)
    )
    environment = StartupEnvironment(
        display=driver.profile.display,
        hierarchy=hierarchy,
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(10,),
        xtest_profile=backend.xtest_input_profile(hierarchy),
        keymap_levels=tuple(sorted(levels.items())),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version=backend.input_tool.version,
        input_tool_sha256=backend.input_tool.digest,
    )
    evidence = mint_runtime_evidence(
        readiness_boot=backend.current["boot"],
        readiness_sequence=1,
        environment=environment,
        identity=backend.identity,
        keyboard_keymap=tuple(sorted(keymap.items())),
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=keyboard_profile,
    )
    observer = _FixtureInputObserver(driver._external_input, [], [])
    driver._monitor = observer
    driver._monitor_calibration = False
    backend.runtime_observer = observer
    backend.runtime_driver = driver
    driver.start_input_observer = lambda **_kwargs: observer  # type: ignore[method-assign]
    driver.stop_input_observer = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    anchor_sequence = 0
    last_anchor_monotonic = 0.0

    def provide_anchor(identity, action):
        nonlocal anchor_sequence, last_anchor_monotonic
        anchor_sequence += 1
        capture = backend.root / f"mail-anchor-{anchor_sequence}.png"
        capture.parent.mkdir(mode=0o700, exist_ok=True)
        capture.write_bytes(f"mailbox anchor {action} {anchor_sequence}".encode())
        capture_monotonic = max(float(driver._clock()), last_anchor_monotonic + 1e-6)
        last_anchor_monotonic = capture_monotonic
        anchor = driver.build_measured_anchor(
            action=action,
            x=320,
            y=240,
            capture_path=capture,
            capture_id=hashlib.sha256(capture.read_bytes()).hexdigest(),
            capture_sequence=anchor_sequence,
            capture_monotonic=capture_monotonic,
            scene="executor_mailbox",
            bounds=(280, 200, 80, 80),
            target_xid=identity.child_xid,
            target_ancestry=(identity.child_xid,),
            generation=driver.guardian.generation,
            source="native-visual",
        )
        return anchor

    driver.measured_anchor_provider = provide_anchor
    driver.deadline_trace: list[dict[str, object]] = []
    original_run = driver.run

    def observed_run(name, generation, **kwargs):  # type: ignore[no-untyped-def]
        driver.deadline_trace.append(
            {
                "boundary": "DesktopDriver.run",
                "name": name,
                "deadline": kwargs.get("deadline_monotonic"),
                "kwargs": _trace_kwargs(kwargs),
                "operation": backend._operation_stack[-1]["name"] if backend._operation_stack else None,
            }
        )
        return original_run(name, generation, **kwargs)

    driver.run = observed_run  # type: ignore[method-assign]
    original_prepare_command = driver.prepare_command

    def observed_prepare_command(generation, command, telemetry, **kwargs):  # type: ignore[no-untyped-def]
        backend.prepared_commands.append(command)
        if command == "/reload":
            backend.reload_calls += 1
        driver.deadline_trace.append(
            {
                "boundary": "DesktopDriver.prepare_command",
                "name": command,
                "deadline": kwargs.get("deadline_monotonic"),
                "kwargs": _trace_kwargs(kwargs),
                "operation": backend._operation_stack[-1]["name"] if backend._operation_stack else None,
            }
        )
        return original_prepare_command(generation, command, telemetry, **kwargs)

    driver.prepare_command = observed_prepare_command  # type: ignore[method-assign]
    original_resolve_identity = driver.resolve_identity

    def observed_resolve_identity(*args, **kwargs):  # type: ignore[no-untyped-def]
        entry = {
            "boundary": "DesktopDriver.resolve_identity",
            "deadline": kwargs.get("deadline_monotonic"),
            "kwargs": _trace_kwargs(kwargs),
            "operation": backend._operation_stack[-1]["name"] if backend._operation_stack else None,
        }
        if kwargs.get("deadline_monotonic") is None:
            entry["stack"] = _compact_traceback()
        driver.deadline_trace.append(entry)
        return original_resolve_identity(*args, **kwargs)

    driver.resolve_identity = observed_resolve_identity  # type: ignore[method-assign]
    original_capture_client_crop = driver.capture_client_crop

    def observed_capture_client_crop(*args, **kwargs):  # type: ignore[no-untyped-def]
        driver.deadline_trace.append(
            {
                "boundary": "DesktopDriver.capture_client_crop",
                "deadline": kwargs.get("deadline_monotonic"),
                "kwargs": _trace_kwargs(kwargs),
                "operation": backend._operation_stack[-1]["name"] if backend._operation_stack else None,
            }
        )
        result = original_capture_client_crop(*args, **kwargs)
        if kwargs.get("model") is True:
            backend.last_model_capture_digest = hashlib.sha256(result.read_bytes()).hexdigest()
        return result

    driver.capture_client_crop = observed_capture_client_crop  # type: ignore[method-assign]
    generation = driver.arm(evidence)
    assert generation == 1
    backend.runtime_evidence = evidence
    return evidence


def _make_case(tmp_path: Path, *, armed: bool, max_messages: int):
    backend = _MailBackend(tmp_path, armed=armed, max_messages=max_messages)
    clock = _VirtualMonotonic()
    backend.virtual_clock = clock
    driver = DesktopDriver(
        profile=DriverProfile(xauthority="/tmp/test-Xauthority", wine_prefix="/tmp/test-wine-prefix"),
        backend=backend,
        monotonic=clock.monotonic,
        lease_path=tmp_path / "desktop.lock",
    )
    plan = load_catalog().scan_plan()
    config = SessionConfig(
        profile=driver.profile,
        journal=tmp_path / "session.json",
        socket=tmp_path / "session.sock",
        plan_hash=plan["planHash"],
        plan_json=plan["planJSON"],
        capture_dir=tmp_path / "captures",
        saved_variables=tmp_path / "Crowley.lua",
        companion_state=tmp_path / "companion.json",
        data_lua=tmp_path / "Data.lua",
        routes_file=backend.routes_path,
    )
    assert config.data_lua == backend.data_lua and config.data_lua.exists()
    assert read_data_acknowledged_ids(config.data_lua) == (UID,)
    # RECONCILE reads the configured companion writer independently of the
    # addon Data.lua acknowledgement.  This mail-only composition has no
    # scan/upload producer, so provide the strict empty durable history file
    # rather than allowing the public reconciliation path to treat absence as
    # malformed state.
    assert config.companion_state is not None
    write_companion_state(config.companion_state)
    value = copy.deepcopy(DEFAULT_STATE)
    value.update(
        {
            "state": ControllerState.WAIT_DUE.value,
            "mode": "repeat",
            "generation": 1,
            "leaseGeneration": 1,
            "restartRequiresResume": False,
            "nativeCalibration": True,
            "inputCalibration": True,
            "resumeState": ControllerState.WAIT_DUE.value,
            "qualification": {
                "streak": 1,
                "totalAccepted": 1,
                "cadenceSeedStart": WALL_TIME - 1_800,
                "accepted": [{"uid": UID, "startedAt": WALL_TIME - 100}],
            },
        }
    )
    value["current"].update(
        {
            "uid": UID,
            "pending": False,
            "startedAt": WALL_TIME - 100,
            "action": "",
            "deadlineAt": 0,
            "evidence": {"ackLoadedUID": UID, "qualification": "accepted"},
        }
    )
    journal = Journal(config.journal)
    journal.write(value)
    controller = Controller(
        journal,
        driver=driver,
        clock=clock.monotonic,
        wall_clock=lambda: WALL_TIME,
        recover_on_restart=False,
    )
    with patch("crowley.session.daemon.LunaAdapter", _RuntimeFixtureModel):
        daemon = SessionDaemon(config, controller=controller, driver=driver)
    daemon.model = _MailModel(backend)

    # Controller admission and the injected driver use this virtual monotonic
    # source for absolute deadlines.  Native settle polling still needs to
    # progress in wall time, so use a local offset clock anchored to the same
    # virtual value instead of mixing an unrelated real-monotonic origin into
    # the operation-deadline intersection.
    operation_clock = clock.monotonic

    def begin_operation(  # type: ignore[no-untyped-def]
        label: str,
        *,
        operation_limit: float,
        requested_deadline=None,
        observed_deadline=None,
        observed_boundary: str | None = None,
    ):
        start = float(operation_clock())
        capability = controller.mail_cycle_capability()
        cycle_deadline = float(capability.deadline_monotonic) if capability is not None else float("inf")
        operation = {
            "name": label,
            "start": start,
            "end": None,
            "cycleDeadline": cycle_deadline,
            "operationLimit": operation_limit,
            "requestedDeadline": requested_deadline,
            "observedDeadline": observed_deadline,
            "observedBoundary": observed_boundary,
            "serializedOwnerStart": None,
            "serializedOwnerDeadline": None,
            "serializedOwnerBoundary": None,
            "deadlines": [],
            "driverStart": len(driver.deadline_trace),
            "nativeStart": len(backend.native_calls),
            "actionStart": len(backend.action_receipts),
        }
        backend._operation_stack.append(operation)
        return operation

    def finish_operation(operation: dict[str, object]) -> None:
        if not backend._operation_stack or backend._operation_stack[-1] is not operation:
            raise AssertionError("mail operation stack lost its active owner")
        operation["end"] = float(operation_clock())
        if operation.get("observedDeadline") is None:
            first_nested = operation.get("firstNestedDeadline")
            if first_nested is not None:
                operation["observedDeadline"] = first_nested
                operation["observedBoundary"] = operation.get("firstNestedBoundary")
        operation["driverEnd"] = len(driver.deadline_trace)
        operation["nativeEnd"] = len(backend.native_calls)
        operation["actionEnd"] = len(backend.action_receipts)
        backend.operation_trace.append(operation)
        backend._operation_stack.pop()

    def run_operation(label: str, original, *args, operation_limit: float, **kwargs):  # type: ignore[no-untyped-def]
        operation = begin_operation(
            label,
            operation_limit=operation_limit,
            requested_deadline=kwargs.get("deadline_monotonic"),
            observed_deadline=None if label == "executor_click" else kwargs.get("deadline_monotonic"),
            observed_boundary=label,
        )
        prior_observation = daemon._deadline_observation_operation
        if label == "executor_click":
            daemon._deadline_observation_operation = operation
        try:
            return original(*args, **kwargs)
        finally:
            daemon._deadline_observation_operation = prior_observation
            finish_operation(operation)

    original_route_walk = daemon.execute_route_walk

    def observed_route_walk(route_name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if route_name == "mailbox_to_auctioneer":
            operation = begin_operation(
                f"route:{route_name}",
                operation_limit=60.0,
                requested_deadline=kwargs.get("deadline_monotonic"),
                observed_deadline=kwargs.get("deadline_monotonic"),
                observed_boundary=f"route:{route_name}",
            )
            try:
                result = original_route_walk(route_name, *args, **kwargs)
            except BaseException:
                finish_operation(operation)
                raise
            backend._pending_return_operation = operation
            return result
        result = run_operation(
            f"route:{route_name}",
            original_route_walk,
            route_name,
            *args,
            operation_limit=60.0,
            **kwargs,
        )
        capability = controller.mail_cycle_capability()
        if (
            capability is not None
            and route_name == "auctioneer_to_mailbox"
            and backend._pending_world_operation is None
        ):
            backend._pending_world_operation = begin_operation(
                "world_click",
                operation_limit=60.0,
                requested_deadline=None,
            )
        return result

    daemon.execute_route_walk = observed_route_walk  # type: ignore[method-assign]
    original_world_click = daemon.execute_world_click

    def observed_world_click(*args, **kwargs):  # type: ignore[no-untyped-def]
        operation = backend._pending_world_operation
        if operation is None:
            return run_operation(
                "world_click",
                original_world_click,
                *args,
                operation_limit=60.0,
                **kwargs,
            )
        try:
            return original_world_click(*args, **kwargs)
        except BaseException:
            if backend._pending_world_operation is operation:
                finish_operation(operation)
                backend._pending_world_operation = None
            raise

    daemon.execute_world_click = observed_world_click  # type: ignore[method-assign]
    original_executor_click = daemon.execute_executor_click

    def observed_executor_click(*args, **kwargs):  # type: ignore[no-untyped-def]
        operation = backend._pending_world_operation
        if operation is not None:
            finish_operation(operation)
            backend._pending_world_operation = None
        return run_operation(
            "executor_click",
            original_executor_click,
            *args,
            # The serialized executor owner is a 60-second physical
            # operation. The later 30-second publication/readback is traced
            # separately as postcondition and remains a future budget.
            operation_limit=60.0,
            **kwargs,
        )

    daemon.execute_executor_click = observed_executor_click  # type: ignore[method-assign]
    original_postcondition = daemon._observe_mail_completion

    def observed_postcondition(*args, **kwargs):  # type: ignore[no-untyped-def]
        operation = begin_operation("postcondition", operation_limit=30.0)
        capability = args[0] if args else kwargs.get("capability")
        fixed_wait_deadline = None
        cycle_id = getattr(capability, "cycle_id", None)
        if cycle_id is not None:
            # Read the daemon's original per-attempt wait bound without
            # deriving a new poll-start deadline or changing the call.
            fixed_wait_deadline = daemon._mail_cycle_wait_deadlines.get(cycle_id)
        operation["fixedWaitDeadline"] = fixed_wait_deadline
        if fixed_wait_deadline is not None:
            operation["requestedDeadline"] = fixed_wait_deadline
            operation["observedDeadline"] = fixed_wait_deadline
            operation["observedBoundary"] = "_mail_cycle_wait_deadlines"
        try:
            return original_postcondition(*args, **kwargs)
        finally:
            finish_operation(operation)

    daemon._observe_mail_completion = observed_postcondition  # type: ignore[method-assign]
    original_run_action = daemon._run_action

    def observed_run_action(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        operation = backend._pending_return_operation
        if operation is not None:
            finish_operation(operation)
            backend._pending_return_operation = None
        return run_operation(
            f"action:{name}",
            original_run_action,
            name,
            *args,
            operation_limit=45.0 if name == "open_ah" else 60.0,
            **kwargs,
        )

    daemon._run_action = observed_run_action  # type: ignore[method-assign]
    original_measure_mailbox_anchor = daemon._measure_mailbox_anchor

    def observed_measure_mailbox_anchor(*args, **kwargs):  # type: ignore[no-untyped-def]
        operation = backend._operation_stack[-1] if backend._operation_stack else None
        if operation is not None and operation.get("name") == "world_click":
            operation["observedDeadline"] = kwargs.get("operation_deadline")
            operation["observedBoundary"] = "_measure_mailbox_anchor"
        return original_measure_mailbox_anchor(*args, **kwargs)

    daemon._measure_mailbox_anchor = observed_measure_mailbox_anchor  # type: ignore[method-assign]
    original_mail_loop_step = daemon._mail_loop_step

    def observed_mail_loop_step(snapshot, capability):  # type: ignore[no-untyped-def]
        phase = controller.mail_cycle_checkpoint().get("phase")
        if phase == "mailbox_open" and backend._pending_world_operation is None:
            backend._pending_world_operation = begin_operation("world_click", operation_limit=60.0)
        try:
            return original_mail_loop_step(snapshot, capability)
        finally:
            operation = backend._pending_world_operation
            if operation is not None:
                finish_operation(operation)
                backend._pending_world_operation = None
            operation = backend._pending_return_operation
            if operation is not None:
                finish_operation(operation)
                backend._pending_return_operation = None

    daemon._mail_loop_step = observed_mail_loop_step  # type: ignore[method-assign]

    def aligned_native_clock() -> float:
        # LifecycleBackend.capture reanchors its virtual cadence to the
        # current wall clock after each published frame.  ``max`` advances
        # settle waits during a sleep while preserving that reanchored value,
        # keeping native deadline comparisons in the controller's domain.
        return max(float(clock.value), time.monotonic())

    # The fixture's driver clock remains virtual for deterministic public
    # deadlines; this private daemon clock advances with wall time only for
    # the production settle observation and never patches stdlib time.
    daemon._native_clock = aligned_native_clock
    daemon.motion_provider._clock = daemon._native_clock
    daemon._deadline_observation_operation = None
    original_native_clock = daemon._native_clock

    def observed_native_clock(*args, **kwargs):  # type: ignore[no-untyped-def]
        value = original_native_clock(*args, **kwargs)
        operation = daemon._deadline_observation_operation
        if (
            operation is not None
            and operation.get("name") == "executor_click"
            and operation.get("serializedOwnerStart") is None
        ):
            # Return the exact original source value; this wrapper only
            # records the serialized owner boundary.
            operation["serializedOwnerStart"] = float(value)
        return value

    daemon._native_clock = observed_native_clock  # type: ignore[method-assign]
    original_require_motion_interaction = daemon._require_motion_interaction

    def observed_require_motion_interaction(*args, **kwargs):  # type: ignore[no-untyped-def]
        operation = daemon._deadline_observation_operation
        local = getattr(daemon, "_motion_deadline_local", None)
        if (
            operation is not None
            and operation.get("name") == "executor_click"
            and operation.get("serializedOwnerDeadline") is None
            and local is not None
            and hasattr(local, "deadline")
        ):
            operation["serializedOwnerDeadline"] = float(local.deadline)
            operation["serializedOwnerBoundary"] = "_require_motion_interaction"
            if operation.get("observedDeadline") is None:
                operation["observedDeadline"] = float(local.deadline)
                operation["observedBoundary"] = "_require_motion_interaction"
        return original_require_motion_interaction(*args, **kwargs)

    daemon._require_motion_interaction = observed_require_motion_interaction  # type: ignore[method-assign]
    evidence = _install_runtime_input(driver, backend)
    controller._input_evidence = evidence
    controller._runtime_startup_evidence = evidence
    return daemon, backend, driver, controller, clock


def _start_request(controller: Controller, daemon: SessionDaemon) -> dict:
    generation = controller.status().generation
    return SessionIPC(controller, daemon).dispatch(
        {
            "command": "mail_loop_start",
            "generation": generation,
            "queueId": QUEUE_ID,
            "seq": 1,
            "confirmQueueId": "",
        }
    )


def _cycle_id(controller: Controller) -> str:
    cycle = controller.mail_cycle_checkpoint()
    assert isinstance(cycle["cycleId"], str) and cycle["cycleId"]
    return cycle["cycleId"]


def _executor_request(controller: Controller, daemon: SessionDaemon, *, confirm: bool) -> dict:
    request = {
        "command": "executor_click",
        "generation": controller.status().generation,
        "queueId": QUEUE_ID,
        "seq": 1,
        "confirmQueueId": QUEUE_ID if confirm else "",
        "cycleId": _cycle_id(controller),
    }
    return SessionIPC(controller, daemon).dispatch(request)


def _assert_executor_archive(record: dict, *, lease_generation: int, result: str) -> None:
    """Check one physical archive, including its real receipt and CRS bytes."""

    receipt = record["receipt"]
    anchor = record["anchor"]
    assert receipt["action"] == "executor_click"
    assert receipt["generation"] == lease_generation
    assert receipt["guardianCompleted"] is True
    assert receipt["admittedAnchorEvidenceHash"] == anchor["evidenceHash"]
    assert receipt["admittedAnchorCaptureId"] == anchor["captureId"]
    assert receipt["admittedAnchorCaptureSequence"] == anchor["captureSequence"]
    assert receipt["anchorEvidenceHash"] != anchor["evidenceHash"]
    assert receipt["anchorCaptureId"] != anchor["captureId"]
    assert receipt["anchorCaptureSequence"] > anchor["captureSequence"]
    assert receipt["rateGate"] == "canonical-user-session"
    ordered = [
        receipt[key]
        for key in (
            "clickPrecommitMonotonicNs",
            "clickDispatchStartedMonotonicNs",
            "pressReleaseMonotonicNs",
            "clickAckMonotonicNs",
            "rateRecordMonotonicNs",
            "receiptFinalizedMonotonicNs",
        )
    ]
    assert all(isinstance(value, int) and value > 0 for value in ordered)
    assert ordered == sorted(ordered)
    assert record["result"] == result
    assert record["postExecutor"]["lastResult"]["status"] == result
    assert record["postExecutor"]["lastClickSource"] == "button"
    assert record["postExecutor"]["clickOrdinal"] == record["preExecutor"]["clickOrdinal"] + 1
    assert record["postExecutor"]["resultCount"] == max(1, record["preExecutor"]["resultCount"])
    assert record["postCrs"]["sequence"] > record["preCrs"]["sequence"]
    for evidence in (record["preEvidence"], record["postEvidence"]):
        raw = bytes.fromhex(evidence["rawFrameHex"])
        assert evidence["length"] == 2048
        assert len(raw) == 2048
        assert hashlib.sha256(raw).hexdigest() == evidence["sha256"]


def _assert_auctioneer_keyboard_progression(backend: _MailBackend) -> None:
    """Require target/binding/AH state to come from the emitted keyboard plan."""

    effects = backend.keyboard_effects
    submitted = [effect["command"] for effect in effects if effect.get("kind") == "submit_command"]
    assert submitted == [
        DesktopDriver.TARGET_AUCTIONEER_COMMAND,
        DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND,
    ]
    typed = ""
    for effect in effects:
        if effect.get("kind") == "type_character":
            typed += str(effect["character"])
        elif effect.get("kind") == "submit_command":
            assert typed == effect["command"]
            typed = ""
    assert typed == ""
    assert backend.character_hud_flushes >= len(submitted)
    target_index = next(
        index
        for index, effect in enumerate(effects)
        if effect.get("kind") == "submit_command"
        and effect.get("command") == DesktopDriver.TARGET_AUCTIONEER_COMMAND
    )
    binding_index = next(
        index
        for index, effect in enumerate(effects)
        if effect.get("kind") == "submit_command"
        and effect.get("command") == DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND
    )
    ah_index = next(index for index, effect in enumerate(effects) if effect.get("kind") == "open_ah")
    assert any(effect.get("kind") == "open_chat" for effect in effects[:target_index])
    assert target_index < binding_index < ah_index
    assert backend.current["auctioneer"]["targetPresent"] is True
    assert backend.current["auctioneer"]["targetName"] == "Auctioneer Jaxon"
    assert backend.current["auctioneer"]["ctrlF12Action"] == "INTERACTTARGET"
    assert backend.current["ahOpen"] is True


def _assert_cycle_deadlines(daemon: SessionDaemon, backend: _MailBackend, driver: DesktopDriver, start: dict):
    capability = daemon.controller.mail_cycle_capability()
    cycle_deadline = start.get("deadline")
    if capability is not None:
        cycle_deadline = capability.deadline_monotonic
    assert isinstance(cycle_deadline, (int, float)) and not isinstance(cycle_deadline, bool)
    assert math.isfinite(float(cycle_deadline))
    # The public start request performs legitimate status/capture work before
    # the admitted operation begins.  The final idle poll is another separate
    # scope.  Keep both slices explicit so only live-cycle boundaries are
    # required to carry an operation owner and deadline.
    pre_driver = list(driver.deadline_trace[start["driverTrace"] : start["cycleDriverTrace"]])
    pre_native = list(backend.native_calls[start["nativeTrace"] : start["cycleNativeTrace"]])
    pre_actions = list(backend.action_receipts[start["actionTrace"] : start["cycleActionTrace"]])
    post_driver = list(driver.deadline_trace[start["postDriverTrace"] :])
    post_native = list(backend.native_calls[start["postNativeTrace"] :])
    post_actions = list(backend.action_receipts[start["postActionTrace"] :])
    assert len(backend.operation_trace) == start["postOperationTrace"]
    for label, entries in (
        ("pre-admission driver", pre_driver),
        ("pre-admission native", pre_native),
        ("pre-admission actions", pre_actions),
        ("post-complete driver", post_driver),
        ("post-complete native", post_native),
        ("post-complete actions", post_actions),
    ):
        assert all(entry.get("operation") is None for entry in entries), label
        assert all(entry.get("deadline") is None for entry in entries), label

    operations = list(backend.operation_trace[start["cycleOperationTrace"] : start["postOperationTrace"]])
    assert operations, "the composed cycle did not record a live operation boundary"
    operation_limits = {
        "world_click": 60.0,
        "executor_click": 60.0,
        "postcondition": 30.0,
        "action:open_ah": 45.0,
    }
    for operation in operations:
        name = operation["name"]
        if str(name).startswith("route:"):
            limit = 60.0
        else:
            limit = operation_limits.get(str(name))
        assert limit is not None, f"unbounded mail-loop operation {name!r}"
        operation_start = operation["start"]
        operation_end = operation["end"]
        assert isinstance(operation_start, (int, float))
        assert isinstance(operation_end, (int, float))
        assert operation_end >= operation_start
        deadline_start = operation_start
        if str(name) == "executor_click":
            serialized_start = operation.get("serializedOwnerStart")
            serialized_deadline = operation.get("serializedOwnerDeadline")
            assert isinstance(serialized_start, (int, float)) and not isinstance(serialized_start, bool)
            assert math.isfinite(float(serialized_start))
            assert isinstance(serialized_deadline, (int, float)) and not isinstance(serialized_deadline, bool)
            assert math.isfinite(float(serialized_deadline))
            assert operation.get("serializedOwnerBoundary") == "_require_motion_interaction"
            deadline_start = serialized_start
        requested_deadline = operation.get("requestedDeadline")
        expected_bounds = [float(deadline_start) + limit, float(cycle_deadline)]
        if requested_deadline is not None:
            assert isinstance(requested_deadline, (int, float)) and not isinstance(requested_deadline, bool)
            assert math.isfinite(float(requested_deadline))
            expected_bounds.append(float(requested_deadline))
        expected_deadline = min(expected_bounds)
        observed_deadline = operation.get("observedDeadline")
        assert isinstance(observed_deadline, (int, float)) and not isinstance(observed_deadline, bool)
        assert math.isfinite(float(observed_deadline))
        assert float(observed_deadline) > float(operation_start)
        assert math.isclose(float(observed_deadline), expected_deadline, rel_tol=0.0, abs_tol=1e-6), (
            operation["name"],
            operation.get("observedBoundary"),
            expected_deadline,
            observed_deadline,
        )
        if str(name) == "executor_click":
            assert math.isclose(
                float(operation["serializedOwnerDeadline"]), expected_deadline, rel_tol=0.0, abs_tol=1e-6
            ), (
                operation["name"],
                operation["serializedOwnerStart"],
                expected_deadline,
                operation["serializedOwnerDeadline"],
            )
        nested_deadlines = [value for value in operation["deadlines"] if value is not None]
        assert nested_deadlines, f"operation {name!r} had no bounded nested calls"
        assert all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in nested_deadlines
        )
        assert all(
            math.isfinite(float(value)) and float(value) > float(operation_start)
            for value in nested_deadlines
        )
        assert all(float(value) <= float(observed_deadline) + 1e-6 for value in nested_deadlines)
        # Nested providers may legitimately clamp to a strictly shorter local
        # helper bound.  They must never reset or widen the parent/cycle
        # intersection; the outer operation above proves the exact own bound.
        assert all(float(value) <= float(cycle_deadline) + 1e-6 for value in nested_deadlines)

    def unscoped(entries, lower: int, upper: int, start_key: str, end_key: str):  # type: ignore[no-untyped-def]
        intervals = [(int(item[start_key]), int(item[end_key])) for item in operations]
        return [
            entry
            for index, entry in enumerate(entries)
            if lower <= index < upper and not any(begin <= index < end for begin, end in intervals)
        ]

    cycle_unscoped = (
        unscoped(
            driver.deadline_trace,
            start["cycleDriverTrace"],
            start["postDriverTrace"],
            "driverStart",
            "driverEnd",
        ),
        unscoped(
            backend.native_calls,
            start["cycleNativeTrace"],
            start["postNativeTrace"],
            "nativeStart",
            "nativeEnd",
        ),
        unscoped(
            backend.action_receipts,
            start["cycleActionTrace"],
            start["postActionTrace"],
            "actionStart",
            "actionEnd",
        ),
    )
    for entries in cycle_unscoped:
        assert all(entry.get("operation") is None for entry in entries)
        assert all(
            entry.get("deadline") is None
            or (
                isinstance(entry.get("deadline"), (int, float))
                and not isinstance(entry.get("deadline"), bool)
                and math.isfinite(float(entry["deadline"]))
                and float(entry["deadline"]) <= float(cycle_deadline) + 1e-6
            )
            for entry in entries
        ), entries

    scoped_traces: list[dict[str, object]] = []
    scoped_native: list[dict[str, object]] = []
    scoped_actions: list[dict[str, object]] = []
    all_missing_deadlines: list[dict[str, object]] = []
    for operation in operations:
        name = str(operation["name"])
        operation_deadline = operation.get("observedDeadline")
        assert isinstance(operation_deadline, (int, float)) and not isinstance(operation_deadline, bool)
        assert math.isfinite(float(operation_deadline))
        assert float(operation_deadline) > float(operation["start"])
        traces = driver.deadline_trace[int(operation["driverStart"]) : int(operation["driverEnd"])]
        native = backend.native_calls[int(operation["nativeStart"]) : int(operation["nativeEnd"])]
        action_receipts = backend.action_receipts[int(operation["actionStart"]) : int(operation["actionEnd"])]
        scoped_traces.extend(traces)
        scoped_native.extend(native)
        scoped_actions.extend(action_receipts)
        scoped_entries = (*traces, *native, *action_receipts)
        assert all(entry.get("operation") == name for entry in scoped_entries)
        missing_deadlines = []
        for scope, entries in (
            ("driver.deadline_trace", traces),
            ("backend.native_calls", native),
            ("backend.action_receipts", action_receipts),
        ):
            for entry in entries:
                if entry.get("deadline") is None:
                    missing_deadlines.append(
                        {
                            "scope": scope,
                            "boundary": entry.get("boundary"),
                            "name": entry.get("name"),
                            "kind": entry.get("kind"),
                            "schema": entry.get("schema"),
                            "operation": entry.get("operation"),
                            "deadline": entry.get("deadline"),
                            "kwargs": entry.get("kwargs"),
                            "argv": entry.get("argv"),
                            "stack": entry.get("stack"),
                            "operationDeadline": operation_deadline,
                            "cycleDeadline": cycle_deadline,
                        }
                    )
        all_missing_deadlines.extend(missing_deadlines)
        for entry in scoped_entries:
            if entry.get("deadline") is None:
                continue
            assert float(entry["deadline"]) <= float(operation_deadline) + 1e-6, (
                name,
                operation.get("observedBoundary"),
                operation.get("requestedDeadline"),
                operation.get("start"),
                operation_deadline,
                entry,
            )
            assert float(entry["deadline"]) <= float(cycle_deadline) + 1e-6

    missing_details = "; ".join(
        "operation={operation!r} scope={scope} boundary={boundary!r} name={name!r} "
        "kind={kind!r} schema={schema!r} deadline={deadline!r} kwargs={kwargs!r} "
        "argv={argv!r} stack={stack!r}".format(**entry)
        for entry in all_missing_deadlines
    )
    assert not all_missing_deadlines, f"unbounded scoped trace entries: {missing_details}"

    assert any(entry.get("boundary") == "DesktopDriver.run" for entry in scoped_traces)
    assert any(entry.get("boundary") == "DesktopDriver.prepare_command" for entry in scoped_traces)
    assert any(entry.get("boundary") == "DesktopDriver.resolve_identity" for entry in scoped_traces)
    assert any(entry.get("boundary") == "DesktopDriver.capture_client_crop" for entry in scoped_traces)
    assert any(entry.get("kind") == "_run" for entry in scoped_native)
    assert any(entry.get("kind") == "capture" for entry in scoped_native)
    assert any(
        entry.get("kind") == "_run" and entry.get("schema") == "keyboard-steps" for entry in scoped_native
    )
    assert any(entry["name"] == "open_ah" and entry["nativeCallCount"] > 0 for entry in scoped_actions)


def test_default_mail_loop_is_real_unarmed_dry_run_and_reconciles(tmp_path: Path) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    initial_generation = controller.status().generation
    initial_queue_actions = copy.deepcopy(backend.queue["actions"])
    trace_start = {
        "driverTrace": len(driver.deadline_trace),
        "nativeTrace": len(backend.native_calls),
        "actionTrace": len(backend.action_receipts),
        "operationTrace": len(backend.operation_trace),
        "deadline": None,
    }
    try:
        status = SessionIPC(controller, daemon).dispatch({"command": "status"})
        assert status["ok"] and status["status"]["state"] == ControllerState.WAIT_DUE.value
        assert status["status"]["mailLoop"]["enabled"] is False
        started = _start_request(controller, daemon)
        assert started["ok"] is True
        assert backend.agent_client is not None
        wire = backend.wire_records[-1]["snapshot"]
        assert wire["loaded"]["ackedIds"] == [UID]
        assert wire["context"]["character"] == "Arthur"
        assert wire["executor"]["queueId"] == QUEUE_ID

        trace_start.update(
            cycleDriverTrace=len(driver.deadline_trace),
            cycleNativeTrace=len(backend.native_calls),
            cycleActionTrace=len(backend.action_receipts),
            cycleOperationTrace=len(backend.operation_trace),
        )
        for _ in range(24):
            daemon.run_once()
            capability = controller.mail_cycle_capability()
            if capability is not None and trace_start["deadline"] is None:
                trace_start["deadline"] = capability.deadline_monotonic
            if controller.mail_cycle_checkpoint()["phase"] == "complete":
                break
        assert controller.mail_cycle_checkpoint()["phase"] == "complete", repr(
            (
                daemon._mail_loop_last_decision,
                controller._mail_cycle,
                daemon._route_walk_status,
                controller.journal.read()["current"].get("mailCycle"),
            )
        )
        assert backend._world_clicks == 1
        assert backend._executor_clicks == 1
        assert {name for name, _events, _start, _end in backend.route_timelines} == {
            "auctioneer_to_mailbox",
            "mailbox_to_auctioneer",
        }
        assert all(start != end for _name, _events, start, end in backend.route_timelines)
        assert backend.agent_client.protected_calls == []
        assert "/crowley arm" not in backend.prepared_commands
        assert backend.reload_calls == 0
        assert backend.queue["queueId"] == QUEUE_ID
        assert backend.queue["actions"] == initial_queue_actions

        proof = daemon._mailbox_model_proof
        proof_path = Path(proof["path"])
        assert proof_path.exists()
        assert hashlib.sha256(proof_path.read_bytes()).hexdigest() == proof["sha256"]
        bounds = tuple(proof["bounds"])
        assert len(bounds) == 4 and bounds[2:] == (64, 64)
        assert 0 <= bounds[0] < driver.profile.model_crop[2]
        assert 0 <= bounds[1] < driver.profile.model_crop[3]
        expected_mailbox_endpoint = backend.route_file.routes["auctioneer_to_mailbox"].end
        assert backend.model_route_endpoint is not None
        assert math.isclose(backend.model_route_endpoint[0], expected_mailbox_endpoint.x, abs_tol=1e-6)
        assert math.isclose(backend.model_route_endpoint[1], expected_mailbox_endpoint.y, abs_tol=1e-6)
        assert proof["identity"] == backend.identity.fingerprint
        assert proof["leaseGeneration"] == controller.mail_cycle_checkpoint()["leaseGeneration"]
        _assert_executor_archive(
            controller.journal.archives("executor")[0],
            lease_generation=controller.mail_cycle_checkpoint()["leaseGeneration"],
            result="dry_run",
        )

        assert backend.current["ahOpen"] is True
        assert backend.current["mailOpen"] is False
        _assert_auctioneer_keyboard_progression(backend)
        binding = SessionIPC(controller, daemon).dispatch({"command": "mail_loop_status"})
        assert binding["ok"] and binding["status"]["enabled"] is False
        archive = controller.journal.archives("mail_cycle")
        assert len(archive) == 1 and archive[0]["ackUid"] == UID
        assert archive[0]["admittedSeq"] == 1
        assert len(backend.agent_trace) >= 3
        assert any(event["name"] == "open_ah" for event in backend.events)
        # Completion durably enters the public RECONCILE state; advance the
        # ordinary daemon loop until its real Guardian-backed lease is
        # re-established rather than injecting a resume or mutating state.
        for _ in range(4):
            if controller.status().state is ControllerState.WAIT_DUE:
                break
            daemon.run_once()
        assert controller.status().state is ControllerState.WAIT_DUE, repr(
            (
                controller.status(),
                daemon._mail_loop_last_decision,
                controller.journal.read()["current"],
            )
        )
        assert controller.status().generation > initial_generation
        assert driver.guardian.armed is True
        assert driver.guardian.generation == controller.status().lease_generation
        assert (
            SessionIPC(controller, daemon).dispatch({"command": "mail_loop_status"})["status"]["enabled"]
            is False
        )
        before_poll = (
            len(controller.journal.archives("mail_cycle")),
            backend._world_clicks,
            backend._executor_clicks,
        )
        trace_start.update(
            postDriverTrace=len(driver.deadline_trace),
            postNativeTrace=len(backend.native_calls),
            postActionTrace=len(backend.action_receipts),
            postOperationTrace=len(backend.operation_trace),
        )
        daemon.run_once()
        assert controller.status().state is ControllerState.WAIT_DUE
        assert (
            len(controller.journal.archives("mail_cycle")),
            backend._world_clicks,
            backend._executor_clicks,
        ) == before_poll
        _assert_cycle_deadlines(daemon, backend, driver, trace_start)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_armed_mail_loop_requires_two_fresh_public_confirmations(tmp_path: Path) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=True, max_messages=2)
    client = backend.agent_client
    assert client is not None
    initial_generation = controller.status().generation
    initial_queue_actions = copy.deepcopy(backend.queue["actions"])
    trace_start = {
        "driverTrace": len(driver.deadline_trace),
        "nativeTrace": len(backend.native_calls),
        "actionTrace": len(backend.action_receipts),
        "operationTrace": len(backend.operation_trace),
        "deadline": None,
    }
    try:
        wire = backend.wire_records[-1]["snapshot"]
        assert wire["loaded"]["ackedIds"] == [UID]
        assert wire["context"]["character"] == "Arthur"
        assert wire["executor"]["queueId"] == QUEUE_ID
        assert client.ns.Executor.Arm(QUEUE_ID) is True
        client.publish_agent()
        assert _start_request(controller, daemon)["ok"] is True
        trace_start.update(
            cycleDriverTrace=len(driver.deadline_trace),
            cycleNativeTrace=len(backend.native_calls),
            cycleActionTrace=len(backend.action_receipts),
            cycleOperationTrace=len(backend.operation_trace),
        )
        daemon.run_once()
        capability = controller.mail_cycle_capability()
        assert capability is not None
        trace_start["deadline"] = capability.deadline_monotonic
        assert controller.mail_cycle_checkpoint()["phase"] == "awaiting_confirmation", repr(
            (
                daemon._mail_loop_last_decision,
                controller._mail_cycle,
                controller.journal.read()["current"].get("mailCycle"),
            )
        )
        assert backend._executor_clicks == 0
        ready = client.snapshot_agent()["executor"]
        assert ready["phase"] == "ready"
        assert ready["workDone"] == 0 and ready["workTotal"] == 2
        first = _executor_request(controller, daemon, confirm=True)
        assert first["ok"] is True, first
        assert first["result"] == "started"
        assert controller.mail_cycle_checkpoint()["phase"] == "executing"
        assert backend._executor_clicks == 1
        assert client.protected_calls == ["AutoLootMailItem"]
        first_token = daemon._executor_continuation_capability
        assert first_token is not None
        assert first_token == controller.executor_continuation_capability()
        first_archive = first["archive"]
        assert first_archive["postExecutor"]["phase"] == "mail_wait"
        assert first_archive["postExecutor"]["workDone"] == 0
        _assert_executor_archive(
            first_archive,
            lease_generation=first_archive["leaseGeneration"],
            result="started",
        )

        # A normal poll is read-only at mail_wait.  A genuine competing input
        # IPC request is refused by the cycle owner and emits no input.
        before = len(backend.events)
        assert SessionIPC(controller, daemon).dispatch({"command": "mail_loop_status"})["ok"]
        daemon.run_once()
        assert len(backend.events) == before
        assert controller.mail_cycle_checkpoint()["phase"] == "executing"
        competing_before = (len(backend.events), backend._world_clicks, backend._executor_clicks)
        competing = SessionIPC(controller, daemon).dispatch(
            {
                "command": "motion_key",
                "generation": controller.status().generation,
                "role": "move_forward",
                "duration": 0.1,
            }
        )
        assert competing["ok"] is False
        assert "mail cycle owns interaction admission" in competing["error"]
        assert (len(backend.events), backend._world_clicks, backend._executor_clicks) == competing_before

        # The actual Lua MAIL_INBOX_UPDATE callback observes the first
        # nonempty message becoming empty and exposes the fresh mail_items
        # boundary without a physical probe.
        client.set_mail_items([{"money": 0}, {"money": 20}])
        client.fire("MAIL_INBOX_UPDATE")
        client.update_agent()
        daemon.run_once()
        first_boundary = client.snapshot_agent()["executor"]
        assert first_boundary["phase"] == "mail_items"
        assert first_boundary["workDone"] == 1
        # The real addon keeps the physical click rate gate closed for one
        # second after the first dispatch; only a fresh post-event frame can
        # authorize the second confirmation.
        client.advance(1)
        client.update_agent()
        daemon.run_once()
        first_boundary = client.snapshot_agent()["executor"]
        assert first_boundary["clickReady"] is True
        assert backend._executor_clicks == 1
        second = _executor_request(controller, daemon, confirm=True)
        assert second["ok"] is True, second
        assert second["result"] == "started"
        assert backend._executor_clicks == 2
        assert daemon._executor_continuation_capability == controller.executor_continuation_capability()
        second_archive = second["archive"]
        assert second_archive["postExecutor"]["phase"] == "mail_wait"
        assert second_archive["postExecutor"]["workDone"] == 1
        _assert_executor_archive(
            second_archive,
            lease_generation=second_archive["leaseGeneration"],
            result="started",
        )

        # A later asynchronous terminal event alone completes the action.  It
        # must not trigger a third physical button probe.
        client.set_mail_items([{"money": 0}, {"money": 0}])
        client.fire("MAIL_INBOX_UPDATE")
        client.update_agent()
        daemon.run_once()
        assert backend._executor_clicks == 2
        assert controller.executor_continuation_capability() is None
        assert daemon._executor_continuation_capability is None
        assert controller.mail_cycle_checkpoint()["phase"] == "to_auctioneer"
        assert client.protected_calls == ["AutoLootMailItem", "AutoLootMailItem"]
        executor_archives = controller.journal.archives("executor")
        assert len([record for record in executor_archives if "receipt" in record]) == 2
        terminal = [record for record in executor_archives if record.get("kind") == "terminal_observation"]
        assert len(terminal) == 1
        assert "receipt" not in terminal[0]
        terminal_raw = bytes.fromhex(terminal[0]["postEvidence"]["rawFrameHex"])
        assert len(terminal_raw) == 2048
        assert hashlib.sha256(terminal_raw).hexdigest() == terminal[0]["postEvidence"]["sha256"]
        assert terminal[0]["postExecutor"]["lastResult"] == {
            "seq": 1,
            "kind": "mail_collect",
            "status": "succeeded",
        }
        assert terminal[0]["postExecutor"]["clickOrdinal"] == second_archive["postExecutor"]["clickOrdinal"]
        assert terminal[0]["postExecutor"]["resultCount"] == second_archive["postExecutor"]["resultCount"]
        assert client.snapshot_agent()["executor"]["currentKind"] == "craft"

        for _ in range(12):
            daemon.run_once()
            if controller.status().state is ControllerState.WAIT_DUE:
                break
        assert controller.mail_cycle_checkpoint()["phase"] == "complete"
        assert controller.status().state is ControllerState.WAIT_DUE
        assert controller.executor_continuation_capability() is None
        assert daemon._executor_continuation_capability is None
        assert backend._executor_clicks == 2
        assert controller.status().generation > initial_generation
        assert len(controller.journal.archives("mail_cycle")) == 1
        assert "/crowley arm" not in backend.prepared_commands
        assert backend.reload_calls == 0
        assert backend.queue["queueId"] == QUEUE_ID
        assert backend.queue["actions"] == initial_queue_actions
        assert driver.guardian.armed is True
        assert driver.guardian.generation == controller.status().lease_generation
        _assert_auctioneer_keyboard_progression(backend)
        assert (
            SessionIPC(controller, daemon).dispatch({"command": "mail_loop_status"})["status"]["enabled"]
            is False
        )
        before_poll = (
            len(controller.journal.archives("mail_cycle")),
            backend._world_clicks,
            backend._executor_clicks,
        )
        trace_start.update(
            postDriverTrace=len(driver.deadline_trace),
            postNativeTrace=len(backend.native_calls),
            postActionTrace=len(backend.action_receipts),
            postOperationTrace=len(backend.operation_trace),
        )
        daemon.run_once()
        assert (
            len(controller.journal.archives("mail_cycle")),
            backend._world_clicks,
            backend._executor_clicks,
        ) == before_poll
        _assert_cycle_deadlines(daemon, backend, driver, trace_start)
    finally:
        daemon.request_shutdown()
        driver.close()
