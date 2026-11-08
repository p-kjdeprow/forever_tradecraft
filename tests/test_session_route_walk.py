from __future__ import annotations

import math
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session import cli, pointer_execution
from crowley.session import config as config_module
from crowley.session import daemon as daemon_module
from crowley.session import driver as driver_module
from crowley.session.config import open_routes_descriptor
from crowley.session.daemon import SessionDaemon
from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend
from crowley.session.driver_runner import GuardedActionRunner
from crowley.session.executor import BindingMetadata, Point, PointerTarget, Rect
from crowley.session.guardian import GuardianError, InputGuardian
from crowley.session.input_broker import InputBroker, RateReservation
from crowley.session.motion import MAX_FRAME_AGE
from crowley.session.route_walk import KeyEvent, Kinematics, Pose, Route, RouteFile, Style, simulate
from tests.test_session_input_tool import fixture_input_tool


class _Owner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def _native_construction_flush_expiry(self) -> None:
        return None

    def _native_construction_blocks_competing_entry(self) -> bool:
        return False

    def status_dict(self) -> dict[str, object]:
        return {"routeWalk": {"state": "complete"}}

    def execute_route_walk(self, route: str, *, generation: int) -> object:
        self.calls.append(("route", (route, generation)))
        return SimpleNamespace(
            boot="boot",
            sequence=2,
            identity_fingerprint="client",
            zone="zone",
            instance=1,
            x=1.0,
            y=2.0,
            z=0.0,
            facing=0.0,
            speed=0.0,
            hover_kind="none",
            hover_name="",
            mail_open=False,
        )

    def execute_world_click(
        self,
        target: str,
        hint_x: float,
        hint_y: float,
        radius: float,
        *,
        expect: str,
        generation: int,
    ) -> object:
        self.calls.append(("world", (target, hint_x, hint_y, radius, expect, generation)))
        return SimpleNamespace(
            boot="boot",
            sequence=3,
            identity_fingerprint="client",
            zone="zone",
            instance=1,
            x=1.0,
            y=2.0,
            z=0.0,
            facing=0.0,
            speed=0.0,
            hover_kind="object",
            hover_name=target,
            mail_open=expect == "mailOpen",
        )

    @staticmethod
    def _motion_result(frame: object) -> dict[str, object]:
        return {"hover": {"kind": frame.hover_kind, "name": frame.hover_name}}


def test_interaction_ipc_rejects_route_and_world_click_field_drift() -> None:
    owner = _Owner()
    ipc = cli.SessionIPC(SimpleNamespace(), owner)  # type: ignore[arg-type]

    assert ipc.dispatch({"command": "route_walk", "generation": 4}) == {
        "ok": False,
        "error": "interaction request fields are malformed",
    }
    assert ipc.dispatch(
        {
            "command": "world_click",
            "generation": 4,
            "target": "Mailbox",
            "hintX": 10,
            "hintY": 20,
            "radius": 60,
            "expect": "mailOpen",
            "extra": True,
        }
    ) == {"ok": False, "error": "interaction request fields are malformed"}
    assert owner.calls == []


def test_interaction_ipc_rejects_world_click_ah_open_expectation() -> None:
    owner = _Owner()
    ipc = cli.SessionIPC(SimpleNamespace(), owner)  # type: ignore[arg-type]

    assert ipc.dispatch(
        {
            "command": "world_click",
            "generation": 4,
            "target": "Auctioneer Jaxon",
            "hintX": 10,
            "hintY": 20,
            "radius": 60,
            "expect": "ahOpen",
        }
    ) == {"ok": False, "error": "world click expected state is not allowlisted"}
    assert owner.calls == []


def test_interaction_ipc_accepts_new_commands_and_returns_hover() -> None:
    owner = _Owner()
    ipc = cli.SessionIPC(SimpleNamespace(), owner)  # type: ignore[arg-type]

    route = ipc.dispatch({"command": "route_walk", "generation": 4, "route": "mailbox"})
    assert route["ok"] is True
    assert route["motion"]["hover"] == {"kind": "none", "name": ""}
    world = ipc.dispatch(
        {
            "command": "world_click",
            "generation": 4,
            "target": "Mailbox",
            "hintX": 10,
            "hintY": 20,
            "radius": 60,
            "expect": "mailOpen",
        }
    )
    assert world["ok"] is True
    assert world["hover"] == {"kind": "object", "name": "Mailbox"}
    assert owner.calls == [
        ("route", ("mailbox", 4)),
        ("world", ("Mailbox", 10.0, 20.0, 60.0, "mailOpen", 4)),
    ]


def test_cli_serializes_route_walk_and_world_click(monkeypatch, tmp_path: Path, capsys) -> None:
    config = SimpleNamespace(socket=tmp_path / "session.sock", journal=tmp_path / "session.json")
    requests: list[dict[str, object]] = []
    timeouts: list[float] = []
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "_socket_request",
        lambda _path, request, *, timeout: (
            requests.append(request) or timeouts.append(timeout) or {"ok": True, "status": {}}
        ),
    )

    assert cli.main(["route-walk", "--generation", "7", "--route", "mailbox"]) == 0
    assert (
        cli.main(
            [
                "world-click",
                "--generation",
                "7",
                "--target",
                "Mailbox",
                "--hint",
                "10,20",
                "--radius",
                "120",
                "--expect",
                "mailOpen",
            ]
        )
        == 0
    )
    assert requests == [
        {"command": "route_walk", "route": "mailbox", "generation": 7},
        {
            "command": "world_click",
            "target": "Mailbox",
            "hintX": 10.0,
            "hintY": 20.0,
            "radius": 120.0,
            "expect": "mailOpen",
            "generation": 7,
        },
    ]
    assert timeouts == [60.0 + cli.MOTION_TRANSPORT_MARGIN_SECONDS] * 2
    assert capsys.readouterr().out.count('"ok": true') == 2


def test_cli_rejects_world_click_ah_open_expectation() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "world-click",
                "--generation",
                "7",
                "--target",
                "Auctioneer Jaxon",
                "--hint",
                "10,20",
                "--radius",
                "120",
                "--expect",
                "ahOpen",
            ]
        )
    assert exc_info.value.code == 2


def test_motion_timeline_argv_schema_accepts_only_balanced_sealed_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = X11Backend(
        DriverProfile(display=":route", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    backend._active_generation = 1
    backend._admission = lambda generation, dispatch: dispatch()
    backend._register_process_group = lambda _generation, _pgid: None
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: "/bin/true")
    argv = [
        "xdotool",
        "keydown",
        "w",
        "sleep",
        "0.040",
        "keydown",
        "q",
        "sleep",
        "0.040",
        "keyup",
        "w",
        "sleep",
        "0.040",
        "keyup",
        "q",
    ]
    try:
        with pytest.raises(DriverError, match="sealed expected argv"):
            backend._run(
                argv,
                generation=1,
                held_keys={"w", "q"},
                input_schema="motion-timeline",
            )
        result = backend._run(
            argv,
            generation=1,
            held_keys={"w", "q"},
            input_schema="motion-timeline",
            expected_argv=tuple(argv),
        )
        assert result.returncode == 0
        for invalid in (
            ["xdotool", "keydown", "W", "sleep", "0.040", "keyup", "W"],
            ["xdotool", "keydown", "w", "keydown", "w", "keyup", "w"],
            ["xdotool", "keydown", "w", "sleep", "0.040", "keyup", "q"],
        ):
            with pytest.raises(DriverError, match="allowlisted|holds|sealed"):
                backend._run(
                    invalid,
                    generation=1,
                    held_keys={"w"},
                    input_schema="motion-timeline",
                    expected_argv=tuple(invalid),
                )
        over_event_limit: list[str] = []
        for _ in range(9):
            over_event_limit.extend(
                [
                    "keydown",
                    "w",
                    "keydown",
                    "q",
                    "keydown",
                    "e",
                    "keydown",
                    "space",
                    "sleep",
                    "0.030",
                    "keyup",
                    "w",
                    "keyup",
                    "q",
                    "keyup",
                    "e",
                    "keyup",
                    "space",
                    "sleep",
                    "0.030",
                ]
            )
        over_event_argv = ["xdotool", *over_event_limit]
        with pytest.raises(DriverError, match="allowlisted"):
            backend._run(
                over_event_argv,
                generation=1,
                held_keys={"w", "q", "e", "space"},
                input_schema="motion-timeline",
                expected_argv=tuple(over_event_argv),
            )
        over_time = [
            "xdotool",
            "keydown",
            "w",
            "sleep",
            "0.030",
            "keyup",
            "w",
            "sleep",
            "2.000",
            "sleep",
            "2.000",
            "sleep",
            "2.000",
            "sleep",
            "0.100",
        ]
        with pytest.raises(DriverError, match="allowlisted"):
            backend._run(
                over_time,
                generation=1,
                held_keys={"w"},
                input_schema="motion-timeline",
                expected_argv=tuple(over_time),
            )
        for hold in ("0.460", "0.020"):
            too_short_or_long = [
                "xdotool",
                "keydown",
                "w",
                "sleep",
                hold,
                "keyup",
                "w",
            ]
            with pytest.raises(DriverError, match="allowlisted"):
                backend._run(
                    too_short_or_long,
                    generation=1,
                    held_keys={"w"},
                    input_schema="motion-timeline",
                    expected_argv=tuple(too_short_or_long),
                )
    finally:
        backend.release_held_input()


def test_interleaved_timeline_seals_guardian_trace_and_revokes_on_failure() -> None:
    captured: dict[str, object] = {}
    revoked: list[str] = []

    class Guardian:
        armed = True
        generation = 7

        def begin_action(self, _generation: int, **kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(complete=True)

        def dispatch_admission(self, _generation: int, dispatch: object) -> None:
            assert callable(dispatch)
            dispatch()

        def check(self, _generation: int) -> None:
            return None

        def finish_action(self, _generation: int, _action: object) -> None:
            return None

    driver = object.__new__(DesktopDriver)
    driver.guardian = Guardian()
    driver.backend = SimpleNamespace(_run=lambda *_args, **_kwargs: None)
    driver._monitor = SimpleNamespace(wait_for_event=lambda predicate, *, timeout: predicate([]))
    driver._held_input = False
    driver._motion_input_context = lambda *_args, **_kwargs: (
        SimpleNamespace(child_xid=41, fingerprint="client"),
        SimpleNamespace(),
        4,
        2,
        (0, 0, 0),
        {"w": 25, "q": 24, "e": 26, "space": 65},
    )
    driver.revoke = lambda reason: revoked.append(reason)
    driver._guarded_actions = GuardedActionRunner(driver.guardian, driver.revoke)
    frame = SimpleNamespace(
        identity_fingerprint="client",
        binding=lambda name: {
            "moveForward": "W",
            "turnLeft": "Q",
            "turnRight": "E",
            "jump": "SPACE",
        }[name],
    )
    events = (
        KeyEvent(0.0, "W", True),
        KeyEvent(0.1, "Left", True),
        KeyEvent(0.2, "space", True),
        KeyEvent(0.3, "W", False),
        KeyEvent(0.4, "Left", False),
        KeyEvent(0.5, "space", False),
    )
    driver.run_key_timeline(events, frame, generation=7)
    assert captured["required_sequence"] == (
        "RawKeyPress",
        "KeyPress",
        "RawKeyPress",
        "KeyPress",
        "RawKeyPress",
        "KeyPress",
        "RawKeyRelease",
        "KeyRelease",
        "RawKeyRelease",
        "KeyRelease",
        "RawKeyRelease",
        "KeyRelease",
    )
    assert captured["expected_details"] == (25, 25, 24, 24, 65, 65, 25, 25, 24, 24, 65, 65)
    assert captured["held_keys"] == {"w", "q", "space"}

    def fail(*_args: object, **_kwargs: object) -> None:
        raise GuardianError("trace failed")

    driver.guardian.check = fail
    with pytest.raises(DriverError, match="trace failed"):
        driver.run_key_timeline(events, frame, generation=7)
    assert revoked == ["motion timeline Guardian failure: trace failed"]


def test_driver_rejects_a_hold_above_the_autorepeat_safe_cap() -> None:
    driver = object.__new__(DesktopDriver)
    frame = SimpleNamespace(identity_fingerprint="client", binding=lambda _name: "W")
    with pytest.raises(DriverError, match="human bound"):
        driver.run_key_timeline(
            (KeyEvent(0.0, "W", True), KeyEvent(0.5, "W", False)),
            frame,
            generation=1,
        )


def test_real_guardian_releases_on_an_injected_key_repeat(tmp_path: Path) -> None:
    released: list[str] = []

    class Evidence:
        is_live = True
        fingerprint = "route-evidence"

    guardian = InputGuardian(
        tmp_path / "repeat.lock",
        parent_pid=os.getpid(),
        release_input=lambda: released.append("released"),
    )
    generation = guardian.arm(Evidence())
    driver = object.__new__(DesktopDriver)
    driver.guardian = guardian
    driver._guarded_actions = GuardedActionRunner(guardian, driver.revoke)
    driver._held_input = False
    identity = SimpleNamespace(child_xid=41, fingerprint="client")
    driver._is_owned_client_target = lambda xid, _identity: xid == 41

    def event(event_class: str, timestamp: int, *, raw: bool) -> SimpleNamespace:
        return SimpleNamespace(
            event_class=event_class,
            source_device=4,
            master_device=2 if raw else 4,
            target_xid=None if raw else 41,
            detail=25,
            timestamp_ms=timestamp,
            modifiers=None if raw else (0, 0, 0),
            modifier_effective=None if raw else 0,
            is_raw=raw,
        )

    def backend_run(*_args: object, **_kwargs: object) -> None:
        assert guardian.observe(event("RawKeyPress", 1, raw=True))
        assert guardian.observe(event("KeyPress", 2, raw=False))
        assert not guardian.observe(event("KeyPress", 3, raw=False))

    driver.backend = SimpleNamespace(_run=backend_run)
    driver._motion_input_context = lambda *_args, **_kwargs: (
        identity,
        SimpleNamespace(),
        4,
        2,
        (0, 0, 0),
        {"w": 25, "q": 24, "e": 26, "space": 65},
    )
    frame = SimpleNamespace(
        identity_fingerprint="client",
        binding=lambda name: {
            "moveForward": "W",
            "turnLeft": "Q",
            "turnRight": "E",
            "jump": "SPACE",
        }[name],
    )
    try:
        with pytest.raises(DriverError, match="input lease is revoked"):
            driver.run_key_timeline(
                (KeyEvent(0.0, "W", True), KeyEvent(0.1, "W", False)),
                frame,
                generation=generation,
            )
        assert released == ["released"]
        assert not guardian.armed
    finally:
        guardian.close()


def test_real_guardian_accepts_interleaved_trace_and_releases_on_reordering(tmp_path: Path) -> None:
    released: list[str] = []

    class Evidence:
        is_live = True
        fingerprint = "route-evidence"

    guardian = InputGuardian(
        tmp_path / "interleaved.lock",
        parent_pid=os.getpid(),
        release_input=lambda: released.append("released"),
    )
    generation = guardian.arm(Evidence())
    identity = SimpleNamespace(child_xid=41, fingerprint="client")
    driver = object.__new__(DesktopDriver)
    driver.guardian = guardian
    driver._guarded_actions = GuardedActionRunner(guardian, driver.revoke)
    driver._held_input = False
    driver._is_owned_client_target = lambda xid, _identity: xid == 41
    driver._motion_input_context = lambda *_args, **_kwargs: (
        identity,
        SimpleNamespace(),
        4,
        2,
        (0, 0, 0),
        {"w": 25, "q": 24, "e": 26, "space": 65},
    )
    driver._monitor = SimpleNamespace(wait_for_event=lambda predicate, *, timeout: predicate([]))
    runs = [0]

    def event(event_class: str, detail: int, timestamp: int, *, raw: bool) -> SimpleNamespace:
        return SimpleNamespace(
            event_class=event_class,
            source_device=4,
            master_device=2 if raw else 4,
            target_xid=None if raw else 41,
            detail=detail,
            timestamp_ms=timestamp,
            modifiers=None if raw else (0, 0, 0),
            modifier_effective=None if raw else 0,
            is_raw=raw,
        )

    exact = (
        ("RawKeyPress", 25),
        ("KeyPress", 25),
        ("RawKeyPress", 24),
        ("KeyPress", 24),
        ("RawKeyPress", 65),
        ("KeyPress", 65),
        ("RawKeyRelease", 25),
        ("KeyRelease", 25),
        ("RawKeyRelease", 24),
        ("KeyRelease", 24),
        ("RawKeyRelease", 65),
        ("KeyRelease", 65),
    )

    def backend_run(*_args: object, **_kwargs: object) -> None:
        runs[0] += 1
        if runs[0] == 1:
            for timestamp, (event_class, detail) in enumerate(exact, start=1):
                assert guardian.observe(
                    event(event_class, detail, timestamp, raw=event_class.startswith("Raw"))
                )
            return
        assert guardian.observe(event("RawKeyPress", 25, 100, raw=True))
        assert guardian.observe(event("KeyPress", 25, 101, raw=False))
        assert not guardian.observe(event("KeyRelease", 25, 102, raw=False))

    driver.backend = SimpleNamespace(_run=backend_run)
    frame = SimpleNamespace(
        identity_fingerprint="client",
        binding=lambda name: {
            "moveForward": "W",
            "turnLeft": "Q",
            "turnRight": "E",
            "jump": "SPACE",
        }[name],
    )
    events = (
        KeyEvent(0.0, "W", True),
        KeyEvent(0.1, "Left", True),
        KeyEvent(0.2, "space", True),
        KeyEvent(0.3, "W", False),
        KeyEvent(0.4, "Left", False),
        KeyEvent(0.5, "space", False),
    )
    try:
        driver.run_key_timeline(events, frame, generation=generation)
        with pytest.raises(DriverError, match="input lease is revoked"):
            driver.run_key_timeline(events, frame, generation=generation)
        assert released == ["released"]
        assert not guardian.armed
    finally:
        guardian.close()


def _daemon_route_fixture() -> tuple[Route, Style, Kinematics]:
    route = Route(
        "mailbox",
        1,
        1,
        Pose(0.0, 0.0, 0.0),
        Pose(2.0, 0.0, 0.0),
        ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)),
        "Mailbox",
    )
    style = Style(
        (0.15, 0.2, 0.25),
        (0.15, 0.2, 0.25),
        (0.15, 0.2, 0.25),
        (0.08, 0.1, 0.12),
        (0.08, 0.1, 0.12),
        (0.08, 0.1, 0.12),
        (0.7, 0.9, 1.1),
        (0.4, 0.5, 0.6),
    )
    return route, style, Kinematics(7.0, 3.141592653589793, -3.141592653589793, 0.6)


def _motion_frame(
    *, x: float, instance: int = 1, combat: bool = False, chat: bool = False, capture: float | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        boot="boot",
        sequence=int(x * 10) + 1,
        capture_monotonic=float(x if capture is None else capture),
        identity_fingerprint="client",
        zone="zone",
        instance=instance,
        x=x,
        y=0.0,
        z=0.0,
        facing=0.0,
        speed=0.0,
        combat=combat,
        dead_or_ghost=False,
        chat_focused=chat,
        hover_kind="none",
        hover_name="",
        mail_open=False,
    )


def _virtual_route_daemon(
    monkeypatch: pytest.MonkeyPatch,
    *,
    route: Route,
    actuation_error: float = 0.9,
    bad_fix: bool = False,
    overrun: bool = False,
) -> tuple[SessionDaemon, dict[str, object]]:
    """Use the production route executor/planner against a deterministic fake world."""

    _route, style, kinematics = _daemon_route_fixture()
    route_file = RouteFile({route.name: route}, style, kinematics, ("test",))
    daemon = object.__new__(SessionDaemon)
    clock = [100.0]
    pose = [route.start]
    sequences = [0]
    calls: list[tuple[tuple[KeyEvent, ...], int]] = []
    plan_inputs: list[Pose] = []
    planned_segments: list[object] = []
    daemon.route_file = route_file
    daemon._motion_intent_lock = threading.RLock()
    daemon._route_walk_lock = threading.RLock()
    daemon._route_walk_status = {"state": "idle", "route": "", "generation": 0, "segments": [], "error": ""}
    daemon._native_clock = lambda: clock[0]
    daemon._require_motion_interaction = lambda _generation: 91
    daemon._require_native_gate = lambda *_args, **_kwargs: None
    daemon._invalidate_motion_admission = lambda: None
    daemon._motion_deadline = lambda: 160.0
    daemon._route_frame_guard = SessionDaemon._route_frame_guard.__get__(daemon)

    real_plan_segment = daemon_module.plan_segment

    def record_plan_segment(
        planned_route: Route,
        planned_pose: Pose,
        planned_style: Style,
        planned_kinematics: Kinematics,
        seed: int,
    ) -> object:
        plan_inputs.append(planned_pose)
        segment = real_plan_segment(planned_route, planned_pose, planned_style, planned_kinematics, seed)
        planned_segments.append(segment)
        return segment

    monkeypatch.setattr(daemon_module, "plan_segment", record_plan_segment)

    def frame() -> SimpleNamespace:
        sequences[0] += 1
        current = pose[0]
        return SimpleNamespace(
            boot="boot",
            sequence=sequences[0],
            capture_monotonic=clock[0],
            identity_fingerprint="client",
            zone="zone",
            instance=route.instance,
            x=current.x,
            y=current.y,
            z=0.0,
            facing=current.facing,
            speed=0.0,
            combat=False,
            dead_or_ghost=False,
            chat_focused=False,
            hover_kind="none",
            hover_name="",
            mail_open=False,
            binding=lambda name: {
                "moveForward": "W",
                "turnLeft": "Q",
                "turnRight": "E",
                "jump": "SPACE",
            }[name],
        )

    daemon._observe_motion = lambda *_args, **_kwargs: frame()
    original_sleep = daemon_module.time.sleep

    def virtual_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(daemon_module.time, "sleep", virtual_sleep)

    def advance_world(events: tuple[KeyEvent, ...], generation: int) -> None:
        calls.append((tuple(events), generation))
        error_kinematics = Kinematics(
            kinematics.run_speed,
            kinematics.turn_rate_left * actuation_error,
            kinematics.turn_rate_right * actuation_error,
            kinematics.jump_airtime,
        )
        result = simulate(events, pose[0], error_kinematics)[-1][1]
        pose[0] = Pose(result.x + (5.0 if bad_fix else 0.0), result.y, result.facing)
        clock[0] += events[-1].t if events else 0.0
        if overrun:
            clock[0] += 61.0

    class World:
        def run_key_timeline(
            self, events: tuple[KeyEvent, ...], _frame: object, *, generation: int, **_kwargs: object
        ) -> None:
            advance_world(events, generation)

    production_driver: DesktopDriver | None = None
    timeline_argv: list[tuple[str, ...]] = []
    finished_actions: list[int] = []
    if overrun:

        class Guardian:
            armed = True
            generation = 91

            def begin_action(self, _generation: int, **_kwargs: object) -> object:
                return SimpleNamespace(complete=True)

            def dispatch_admission(self, _generation: int, dispatch: object) -> None:
                assert callable(dispatch)
                dispatch()

            def check(self, _generation: int) -> None:
                return None

            def finish_action(self, _generation: int, _action: object) -> None:
                finished_actions.append(1)

            def revoke(self, _reason: str) -> int:
                self.armed = False
                return self.generation + 1

        class Backend:
            events: tuple[KeyEvent, ...] = ()

            def _run(self, argv: list[str], **_kwargs: object) -> None:
                timeline_argv.append(tuple(argv))
                advance_world(self.events, 91)

        production_driver = object.__new__(DesktopDriver)
        production_driver.guardian = Guardian()
        production_driver._guarded_actions = GuardedActionRunner(
            production_driver.guardian, production_driver.revoke
        )
        production_driver.backend = Backend()
        production_driver._monitor = SimpleNamespace(
            wait_for_event=lambda predicate, **_kwargs: predicate([])
        )
        production_driver._held_input = False
        production_driver._input_evidence = None
        production_driver._motion_input_context = lambda *_args, **_kwargs: (
            SimpleNamespace(child_xid=41, fingerprint="client"),
            SimpleNamespace(),
            4,
            2,
            (0, 0, 0),
            {"w": 25, "q": 24, "e": 26, "space": 65},
        )

        class ProductionWorld:
            def run_key_timeline(
                self, events: tuple[KeyEvent, ...], frame: object, *, generation: int, **kwargs: object
            ) -> None:
                production_driver.backend.events = tuple(events)
                DesktopDriver.run_key_timeline(
                    production_driver, events, frame, generation=generation, **kwargs
                )

        daemon.driver = ProductionWorld()
    else:
        daemon.driver = World()
    monkeypatch.setattr(daemon_module.time, "sleep", virtual_sleep)
    assert callable(original_sleep)
    return daemon, {
        "calls": calls,
        "clock": clock,
        "pose": pose,
        "plan_inputs": plan_inputs,
        "planned_segments": planned_segments,
        "production_driver": production_driver,
        "timeline_argv": timeline_argv,
        "finished_actions": finished_actions,
    }


def _closed_loop_route() -> Route:
    return Route(
        "mailbox",
        1,
        1,
        Pose(0.0, 0.0, 0.0),
        Pose(2.0, 8.0, math.pi / 2),
        (
            (0.0, 0.0),
            (1.0, 0.0),
            (2.0, 0.0),
            (2.0, 1.0),
            (2.0, 2.0),
            (2.0, 3.0),
            (2.0, 4.0),
            (2.0, 5.0),
            (2.0, 6.0),
            (2.0, 7.0),
            (2.0, 8.0),
        ),
        "Mailbox",
    )


def test_route_walk_reaches_end_with_systematic_actuation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    route = _closed_loop_route()
    daemon, state = _virtual_route_daemon(monkeypatch, route=route)
    final = daemon.execute_route_walk("mailbox", generation=7)
    assert math.dist((final.x, final.y), (route.end.x, route.end.y)) <= 1.0
    assert daemon._route_walk_status["state"] == "complete"
    assert state["calls"]
    assert all(generation == 91 for _events, generation in state["calls"])
    assert len(state["plan_inputs"]) >= 2
    assert any(
        math.dist(
            (state["plan_inputs"][index].x, state["plan_inputs"][index].y),
            (
                state["planned_segments"][index - 1].expected.x,
                state["planned_segments"][index - 1].expected.y,
            ),
        )
        > 1e-6
        for index in range(1, len(state["plan_inputs"]))
    )

    ideal_daemon, ideal_state = _virtual_route_daemon(
        monkeypatch, route=_closed_loop_route(), actuation_error=1.0
    )
    ideal_daemon.execute_route_walk("mailbox", generation=7)
    assert len(state["calls"]) > len(ideal_state["calls"])


def test_route_walk_stops_on_bad_fix_before_next_input(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, state = _virtual_route_daemon(monkeypatch, route=_closed_loop_route(), bad_fix=True)
    with pytest.raises(DriverError, match="fix failed"):
        daemon.execute_route_walk("mailbox", generation=7)
    assert len(state["calls"]) == 1
    assert daemon._route_walk_status["state"] == "error"


def test_route_walk_deadline_releases_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, state = _virtual_route_daemon(monkeypatch, route=_closed_loop_route(), overrun=True)
    with pytest.raises(DriverError, match="deadline"):
        daemon.execute_route_walk("mailbox", generation=7)
    assert state["clock"][0] > daemon._motion_deadline()
    assert state["finished_actions"] == [1]
    assert state["timeline_argv"]
    assert any("keyup" in argv for argv in state["timeline_argv"])
    assert state["production_driver"]._held_input is False
    assert daemon._motion_intent_lock.acquire(blocking=False)
    daemon._motion_intent_lock.release()
    assert daemon._route_walk_status["state"] == "error"


@pytest.mark.parametrize(
    "frame, message",
    [
        (_motion_frame(x=0.0, combat=True), "combat"),
        (_motion_frame(x=0.0, instance=2), "instance changed"),
        (_motion_frame(x=4.0), "start pose"),
    ],
)
def test_route_walk_refuses_unsafe_or_mismatched_start(
    monkeypatch: pytest.MonkeyPatch, frame: SimpleNamespace, message: str
) -> None:
    route, style, kinematics = _daemon_route_fixture()
    daemon = object.__new__(SessionDaemon)
    daemon.route_file = RouteFile({route.name: route}, style, kinematics, ("test",))
    daemon.driver = SimpleNamespace()
    daemon._motion_intent_lock = threading.RLock()
    daemon._route_walk_lock = threading.RLock()
    daemon._route_walk_status = {"state": "idle", "route": "", "generation": 0, "segments": [], "error": ""}
    daemon._native_clock = lambda: 100.0
    daemon._require_motion_interaction = lambda _generation: 91
    daemon._require_native_gate = lambda *_args, **_kwargs: None
    daemon._invalidate_motion_admission = lambda: None
    daemon._route_frame_guard = SessionDaemon._route_frame_guard.__get__(daemon)
    daemon._observe_settled_motion = lambda *_args, **_kwargs: frame
    generations: list[int] = []
    daemon.driver.run_key_timeline = lambda *_args, generation, **_kwargs: generations.append(generation)
    with pytest.raises(DriverError, match=message):
        daemon.execute_route_walk("mailbox", generation=7)
    assert generations == []


def test_initial_settled_fix_waits_for_the_full_settle_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = object.__new__(SessionDaemon)
    now = [10.0]
    observed: list[float | None] = []
    daemon._native_clock = lambda: now[0]
    daemon._motion_deadline = lambda: 100.0
    daemon._require_motion_interaction = lambda _generation: 1
    daemon._remaining_motion_budget = lambda _deadline: None
    monkeypatch.setattr(daemon_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    first = _motion_frame(x=0.0)
    second = _motion_frame(x=0.0)

    def observe(
        _generation: int, previous: object = None, *, not_before: float | None = None, **_kwargs: object
    ):
        observed.append(not_before)
        return first if previous is None else second

    daemon._observe_motion = observe
    result = daemon._observe_settled_motion(7)
    assert result is second
    assert observed[0] == pytest.approx(11.2)


_CLEAR_BLOCKING_UI = {
    "staticPopupAvailable": True,
    "staticPopupShown": False,
    "gameMenuAvailable": True,
    "gameMenuShown": False,
}


@pytest.mark.parametrize(
    "proof_blocking_ui",
    [
        _CLEAR_BLOCKING_UI,
        {**_CLEAR_BLOCKING_UI, "gameMenuShown": True},
        {**_CLEAR_BLOCKING_UI, "staticPopupShown": True},
    ],
    ids=["clear", "game-menu-appeared", "popup-appeared"],
)
def test_world_click_retakes_hover_proof_after_permit_and_uses_release_boundary(
    monkeypatch: pytest.MonkeyPatch,
    proof_blocking_ui: dict[str, bool],
) -> None:
    daemon = object.__new__(SessionDaemon)
    daemon._motion_intent_lock = threading.RLock()
    clock = [10.0]
    daemon._native_clock = lambda: clock[0]
    daemon._motion_deadline = lambda: 100.0
    daemon._require_motion_interaction = lambda _generation: 44
    daemon._require_native_gate = lambda *_args, **_kwargs: None
    identity = SimpleNamespace(fingerprint="client", child_geometry=(0, 0, 640, 480))
    daemon._resolve_identity = lambda **_kwargs: identity
    initial = _motion_frame(x=0.0)
    hover = _motion_frame(x=0.1, capture=10.1)
    hover.hover_kind, hover.hover_name = "object", "Mailbox"
    proof = _motion_frame(x=0.2, capture=10.2)
    proof.hover_kind, proof.hover_name = "object", "Mailbox"
    final = _motion_frame(x=0.3, capture=10.3)
    snapshots = {
        (initial.boot, initial.sequence, initial.identity_fingerprint): {
            "mailOpen": False,
            "blockingUI": {
                "staticPopupAvailable": True,
                "staticPopupShown": False,
                "gameMenuAvailable": True,
                "gameMenuShown": False,
            },
        },
        (hover.boot, hover.sequence, hover.identity_fingerprint): {"mailOpen": False},
        (proof.boot, proof.sequence, proof.identity_fingerprint): {
            "mailOpen": False,
            "blockingUI": proof_blocking_ui,
        },
        (final.boot, final.sequence, final.identity_fingerprint): {"mailOpen": True},
    }
    daemon._motion_snapshots = snapshots
    frames = iter((hover, proof, final))
    after_release: list[bool] = []

    first_observation = True

    def observe(_generation: int, **kwargs: object) -> SimpleNamespace:
        nonlocal first_observation
        after_release.append(bool(kwargs.get("after_release", False)))
        if first_observation:
            first_observation = False
            return initial
        value = next(frames)
        if value is proof:
            clock[0] = 10.25
        return value

    daemon._observe_motion = observe
    clicked: list[tuple[int, str]] = []

    class Permit:
        is_open = True

        def close(self) -> None:
            self.is_open = False

    permit = Permit()

    class Driver:
        class Broker:
            def reserve_button_rate(self, **_kwargs: object) -> object:
                return permit

            def release_reservation(self, reservation: object) -> None:
                assert reservation is permit
                permit.close()

        input_broker = Broker()

        def run_pointer_motion(self, _point: object, *, generation: int, **_kwargs: object) -> object:
            assert generation == 44
            clock[0] = 10.05
            return SimpleNamespace()

        def run_hover_click(
            self, _point: object, frame: object, *, generation: int, **kwargs: object
        ) -> None:
            assert generation == 44
            assert frame is proof
            assert kwargs["rate_reservation"] is permit
            clicked.append((generation, frame.hover_name))

    daemon.driver = Driver()
    if proof_blocking_ui is not _CLEAR_BLOCKING_UI:
        with pytest.raises(DriverError, match="blocking UI"):
            daemon.execute_world_click("Mailbox", 50, 50, 60, expect="mailOpen", generation=7)
        assert clicked == []
        assert permit.is_open is False
        return
    result = daemon.execute_world_click("Mailbox", 50, 50, 60, expect="mailOpen", generation=7)
    assert result is final
    assert clicked == [(44, "Mailbox")]
    assert after_release == [False, False, False, True]
    assert permit.is_open is False


def _real_click_permit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pointer_execution.ClickRatePermit:
    state_dir = tmp_path / "click-state"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: state_dir)
    return pointer_execution.acquire_click_rate_permit()


def _click_driver_fixture(
    permit: pointer_execution.ClickRatePermit,
    *,
    capture: float,
    kind: str = "object",
    name: str = "Mailbox",
    chat: bool = False,
    moved: bool = False,
) -> tuple[DesktopDriver, SimpleNamespace, Point, float, list[int]]:
    identity = SimpleNamespace(child_xid=41, fingerprint="client", child_geometry=(10, 20, 640, 480))
    point = Point(120, 140)
    binding = BindingMetadata("client", "world", 1, 44, 41, (41,))
    target = PointerTarget("world-hover", Rect(120, 140, 1, 1), binding, padding=0)
    gesture = pointer_execution.plan_human_pointer(
        Point(100, 120), target, Rect(0, 0, 640, 480), seed=91, button=None
    )
    assert gesture.endpoint == point
    clicks: list[int] = []
    driver = object.__new__(DesktopDriver)
    driver.input_broker = InputBroker()
    driver._motion_input_context = lambda *_args, **_kwargs: (
        identity,
        SimpleNamespace(xtest_profile=None),
        4,
        2,
        (0, 0, 0),
        {"w": 25, "q": 24, "e": 26, "space": 65},
    )
    driver._is_owned_client_target = lambda xid, _identity: xid == 41
    driver.backend = SimpleNamespace(
        pointer_position=lambda **_kwargs: (
            10 + int(point.x) + (1 if moved else 0),
            20 + int(point.y),
            41,
        ),
        _run=lambda *_args, **_kwargs: clicks.append(1),
    )
    frame = SimpleNamespace(
        identity_fingerprint="client",
        capture_monotonic=capture,
        hover_kind=kind,
        hover_name=name,
        chat_focused=chat,
    )
    return driver, frame, point, permit.acquired_monotonic - 0.1, clicks


@pytest.mark.parametrize(
    ("case", "kwargs", "message"),
    [
        ("wrong-kind", {"kind": "nameplate"}, "no clickable target"),
        ("pointer-moved", {"moved": True}, "native pointer moved"),
        ("chat-focused", {"chat": True}, "focused chat"),
        ("too-old", {}, "stale or predates"),
    ],
)
def test_hover_admission_rejects_each_invalid_property(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    permit = _real_click_permit(monkeypatch, tmp_path)
    try:
        gesture_end = permit.acquired_monotonic + 0.1
        capture = permit.acquired_monotonic + 0.2
        driver, frame, point, _default_gesture_end, clicks = _click_driver_fixture(
            permit,
            capture=capture,
            kind=str(kwargs.get("kind", "object")),
            name=str(kwargs.get("name", "Mailbox")),
            chat=bool(kwargs.get("chat", False)),
            moved=bool(kwargs.get("moved", False)),
        )
        now = capture + (MAX_FRAME_AGE + 0.1 if case == "too-old" else 0.05)
        monkeypatch.setattr(driver_module.time, "monotonic", lambda: now)
        with pytest.raises(DriverError, match=message):
            driver.run_hover_click(
                point,
                frame,
                gesture=pointer_execution.plan_human_pointer(
                    Point(100, 120),
                    PointerTarget(
                        "world-hover",
                        Rect(120, 140, 1, 1),
                        BindingMetadata("client", "world", 1, 44, 41, (41,)),
                        padding=0,
                    ),
                    Rect(0, 0, 640, 480),
                    seed=91,
                    button=None,
                ),
                gesture_ended_monotonic=gesture_end,
                generation=44,
                click_rate_permit=permit,
                target_name="Mailbox",
            )
        assert clicks == []
    finally:
        permit.close()


def test_run_hover_click_requires_canonical_open_permit_and_target_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    permit = _real_click_permit(monkeypatch, tmp_path)
    driver, frame, point, gesture_end, clicks = _click_driver_fixture(permit, capture=time.monotonic())
    gesture = pointer_execution.plan_human_pointer(
        Point(100, 120),
        PointerTarget(
            "world-hover",
            Rect(120, 140, 1, 1),
            BindingMetadata("client", "world", 1, 44, 41, (41,)),
            padding=0,
        ),
        Rect(0, 0, 640, 480),
        seed=91,
        button=None,
    )
    try:
        with pytest.raises(DriverError, match="target name"):
            driver.run_hover_click(
                point,
                frame,
                gesture=gesture,
                gesture_ended_monotonic=gesture_end,
                generation=44,
                click_rate_permit=permit,
                target_name="Auctioneer Jaxon",
            )
        assert clicks == []
        permit.close()
        with pytest.raises(DriverError, match="held post-hover rate permit"):
            driver.run_hover_click(
                point,
                frame,
                gesture=gesture,
                gesture_ended_monotonic=gesture_end,
                generation=44,
                click_rate_permit=object(),  # type: ignore[arg-type]
                target_name="Mailbox",
            )
    finally:
        if permit.is_open:
            permit.close()


def test_real_click_rate_permits_are_spaced_by_one_second(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _real_click_permit(monkeypatch, tmp_path)
    try:
        first.precommit()
        first.record_press()
    finally:
        first.close()
    second = _real_click_permit(monkeypatch, tmp_path)
    try:
        assert second.acquired_monotonic - first.acquired_monotonic >= 1.0
        with pytest.raises(AttributeError):
            second.acquired_monotonic = 0.0  # type: ignore[misc]
    finally:
        second.close()


def _world_click_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    expected_state: bool,
) -> tuple[SessionDaemon, dict[str, object]]:
    daemon = object.__new__(SessionDaemon)
    daemon._motion_intent_lock = threading.RLock()
    clock = [time.monotonic()]
    sequence = [0]
    observations: list[bool] = []
    clicks: list[int] = []
    press_times: list[int] = []
    identity = SimpleNamespace(fingerprint="client", child_geometry=(0, 0, 640, 480))
    daemon._native_clock = lambda: clock[0]
    daemon._motion_deadline = lambda: 200.0
    daemon._require_motion_interaction = lambda _generation: 44
    daemon._require_native_gate = lambda *_args, **_kwargs: None
    daemon._resolve_identity = lambda **_kwargs: identity
    daemon._motion_snapshots = {}

    def make_frame(*, hover: bool = False, state: bool = False) -> SimpleNamespace:
        sequence[0] += 1
        frame = SimpleNamespace(
            boot="boot",
            sequence=sequence[0],
            capture_monotonic=clock[0],
            identity_fingerprint="client",
            zone="zone",
            instance=1,
            x=0.0,
            y=0.0,
            z=0.0,
            facing=0.0,
            speed=0.0,
            combat=False,
            dead_or_ghost=False,
            chat_focused=False,
            hover_kind="object" if hover else "none",
            hover_name="Mailbox" if hover else "",
            mail_open=state,
            ah_open=state,
        )
        daemon._motion_snapshots[(frame.boot, frame.sequence, frame.identity_fingerprint)] = {
            "mailOpen": state,
            "ahOpen": state,
            "blockingUI": {
                "staticPopupAvailable": True,
                "staticPopupShown": False,
                "gameMenuAvailable": True,
                "gameMenuShown": False,
            },
        }
        return frame

    initial = make_frame()

    def observe(_generation: int, **kwargs: object) -> SimpleNamespace:
        after_release = bool(kwargs.get("after_release", False))
        if after_release:
            observations.append(True)
        previous = kwargs.get("previous")
        if previous is None:
            return initial
        if after_release:
            clock[0] += 0.5
            return make_frame(hover=True, state=expected_state)
        clock[0] += 0.01
        if previous is initial:
            return make_frame(hover=True)
        return make_frame(hover=True)

    daemon._observe_motion = observe

    backend = SimpleNamespace(_run=lambda *_args, **_kwargs: press_times.append(time.monotonic_ns()))

    class Driver:
        input_broker = InputBroker()

        def run_pointer_motion(self, _point: object, *, generation: int, **_kwargs: object) -> object:
            assert generation == 44
            clock[0] += 0.01
            return SimpleNamespace()

        def run_hover_click(
            self, _point: object, _frame: object, *, rate_reservation: object, **_kwargs: object
        ) -> None:
            assert isinstance(rate_reservation, RateReservation)
            rate_reservation._permit.precommit()  # type: ignore[attr-defined]
            backend._run()
            rate_reservation._permit.record_press()  # type: ignore[attr-defined]
            clicks.append(1)

    daemon.driver = Driver()
    daemon.driver.backend = backend
    monkeypatch.setattr(
        pointer_execution,
        "_session_state_directory",
        lambda: tmp_path / "click-state-daemon",
    )
    (tmp_path / "click-state-daemon").mkdir(mode=0o700, exist_ok=True)
    return daemon, {"clicks": clicks, "observations": observations, "press_times": press_times}


def test_consecutive_world_clicks_use_the_real_rate_permit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, state = _world_click_daemon(monkeypatch, tmp_path, expected_state=True)
    daemon.execute_world_click("Mailbox", 100, 100, 60, expect="mailOpen", generation=7)
    daemon.execute_world_click("Mailbox", 100, 100, 60, expect="mailOpen", generation=7)
    assert len(state["press_times"]) == 2
    assert (state["press_times"][1] - state["press_times"][0]) / 1_000_000_000 >= 1.0


@pytest.mark.parametrize("expected_state", [True, False])
def test_world_click_emits_exactly_one_click_and_uses_after_release_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, expected_state: bool
) -> None:
    daemon, state = _world_click_daemon(monkeypatch, tmp_path, expected_state=expected_state)
    if expected_state:
        result = daemon.execute_world_click("Mailbox", 100, 100, 60, expect="mailOpen", generation=7)
        assert result.mail_open is True
    else:
        with pytest.raises(DriverError, match="did not produce mailOpen"):
            daemon.execute_world_click("Mailbox", 100, 100, 60, expect="mailOpen", generation=7)
    assert state["clicks"] == [1]
    assert state["observations"][0] is True


def _patch_route_group(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gid: int,
    group_name: str = "route-user",
    members: list[str] | None = None,
    other_primary_users: list[str] | None = None,
) -> None:
    user_name = "route-user"
    monkeypatch.setattr(config_module.os, "getgid", lambda: gid)
    monkeypatch.setattr(
        config_module.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name=user_name),
    )
    monkeypatch.setattr(
        config_module.grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name=group_name, gr_mem=members or []),
    )
    uid = os.getuid()
    primary_users = [SimpleNamespace(pw_name=user_name, pw_uid=uid, pw_gid=gid)]
    primary_users.extend(
        SimpleNamespace(pw_name=name, pw_uid=uid + 1 + index, pw_gid=gid)
        for index, name in enumerate(other_primary_users or [])
    )
    monkeypatch.setattr(config_module.pwd, "getpwall", lambda: primary_users)


def _patch_route_stat_gid(monkeypatch: pytest.MonkeyPatch, gid: int) -> None:
    real_fstat = config_module.os.fstat

    def with_gid(fd: int) -> object:
        metadata = real_fstat(fd)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=metadata.st_uid,
            st_gid=gid,
        )

    monkeypatch.setattr(config_module.os, "fstat", with_gid)


def test_routes_descriptor_accepts_private_group_writable_parent_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    os.chmod(routes, 0o664)
    os.chmod(tmp_path, 0o775)
    _patch_route_group(monkeypatch, gid=41000)
    _patch_route_stat_gid(monkeypatch, 41000)
    descriptor = open_routes_descriptor(routes)
    os.close(descriptor)


@pytest.mark.parametrize("members", [[], ["route-user"]], ids=["no-members", "only-the-user"])
def test_routes_descriptor_accepts_private_group_membership_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: list[str]
) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    os.chmod(routes, 0o664)
    os.chmod(tmp_path, 0o775)
    _patch_route_group(monkeypatch, gid=41000, members=members)
    _patch_route_stat_gid(monkeypatch, 41000)
    os.close(open_routes_descriptor(routes))


@pytest.mark.parametrize("writable", ["parent", "file"])
@pytest.mark.parametrize(
    ("group", "message"),
    [
        ({"members": ["route-user", "other-user"]}, "members other"),
        ({"other_primary_users": ["other-user"]}, "primary group"),
        # Same name, different uid: still a foreign account.
        ({"other_primary_users": ["route-user"]}, "primary group"),
        ({"group_name": "shared"}, "does not match user"),
        ({"gid": 41001}, "differs"),
    ],
    ids=["shared-member", "other-primary-account", "same-name-other-uid", "group-name-mismatch", "other-gid"],
)
def test_routes_descriptor_rejects_non_private_group_write_on_parent_or_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writable: str,
    group: dict[str, object],
    message: str,
) -> None:
    # Only one of parent and file is group-writable, so each check is exercised on its own.
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    os.chmod(routes, 0o664 if writable == "file" else 0o644)
    os.chmod(tmp_path, 0o775 if writable == "parent" else 0o755)
    settings = {"gid": 41000, **group}
    _patch_route_group(monkeypatch, **settings)  # type: ignore[arg-type]
    _patch_route_stat_gid(monkeypatch, 41000)
    label = "parent directory" if writable == "parent" else "routes_file group write"
    with pytest.raises(ValueError, match=message) as error:
        open_routes_descriptor(routes)
    assert label in str(error.value)


def test_routes_descriptor_rejects_world_writable_file_or_parent(tmp_path: Path) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    os.chmod(routes, 0o666)
    with pytest.raises(ValueError, match="world writable"):
        open_routes_descriptor(routes)
    os.chmod(routes, 0o600)
    os.chmod(tmp_path, 0o777)
    try:
        with pytest.raises(ValueError, match="world writable"):
            open_routes_descriptor(routes)
    finally:
        os.chmod(tmp_path, 0o700)


def test_world_click_spiral_is_bounded_and_stays_inside_verified_client() -> None:
    bounds = Rect(0, 0, 180, 120)
    probes = SessionDaemon._world_probe_points(Point(90, 60), 300, bounds)
    assert len(probes) <= 16
    assert all(bounds.contains(point) for point in probes)
    assert all(math.dist((90, 60), (point.x, point.y)) <= 300.0 for point in probes)
    assert probes[0] == Point(90, 60)


def test_routes_descriptor_rejects_symlink_nonregular_and_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = tmp_path / "routes.json"
    routes.write_text("{}", encoding="utf-8")
    os.chmod(routes, 0o600)
    link = tmp_path / "routes-link.json"
    link.symlink_to(routes)
    with pytest.raises(OSError):
        open_routes_descriptor(link)

    directory = tmp_path / "routes-directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="regular file"):
        open_routes_descriptor(directory)

    real_fstat = config_module.os.fstat
    calls = [0]

    def wrong_owner(fd: int) -> object:
        calls[0] += 1
        if calls[0] == 1:
            return real_fstat(fd)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1)

    monkeypatch.setattr(config_module.os, "fstat", wrong_owner)
    with pytest.raises(ValueError, match="user-owned regular file"):
        open_routes_descriptor(routes)
