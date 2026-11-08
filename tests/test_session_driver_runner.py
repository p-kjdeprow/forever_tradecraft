"""Ordering tests for the shared guarded action template."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from crowley.session.driver import DesktopDriver, WindowIdentity, X11Backend
from crowley.session.driver_runner import GuardedActionRunner
from crowley.session.executor import (
    BindingMetadata,
    CameraDragPlan,
    KeyRole,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    Rect,
)
from crowley.session.guardian import ActionCorrelation, GuardianError
from crowley.session.input_broker import RateReservation
from crowley.session.pointer_execution import plan_human_pointer
from crowley.session.startup import RuntimeInputEvidence


@dataclass
class _Action:
    complete: bool = True


class _Guardian:
    armed = True
    generation = 7

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def finish_action(self, generation: int, action: _Action) -> None:
        assert generation == 7
        assert action.complete
        self.calls.append("finish_action")


def test_runner_preserves_guardian_broker_backend_and_wait_order() -> None:
    calls: list[str] = []
    action = _Action()
    runner = GuardedActionRunner(_Guardian(calls), lambda reason: calls.append(f"revoke:{reason}"))

    runner.run(
        generation=7,
        begin=lambda: calls.append("begin_action") or action,
        dispatch=lambda: calls.append("dispatch_admission"),
        operation=lambda _action: calls.append("broker_and_backend") or "ok",
        wait=lambda _action: calls.append("wait_for_event"),
        failure_reason=lambda exc: str(exc),
        guardian_failure_reason=None,
    )

    assert calls == [
        "begin_action",
        "dispatch_admission",
        "broker_and_backend",
        "wait_for_event",
        "finish_action",
    ]


def test_runner_revokes_after_operation_failure_without_wait_or_finish() -> None:
    calls: list[str] = []
    action = _Action()
    runner = GuardedActionRunner(_Guardian(calls), lambda reason: calls.append(f"revoke:{reason}"))

    def fail(_action: _Action) -> str:
        calls.append("broker_and_backend")
        raise RuntimeError("backend failed")

    with pytest.raises(RuntimeError, match="backend failed"):
        runner.run(
            generation=7,
            begin=lambda: calls.append("begin_action") or action,
            dispatch=lambda: calls.append("dispatch_admission"),
            operation=fail,
            wait=lambda _action: calls.append("wait_for_event"),
            failure_reason=lambda exc: f"operation failed: {exc}",
            guardian_failure_reason=None,
            revoke_on_failure=True,
        )

    assert calls == [
        "begin_action",
        "dispatch_admission",
        "broker_and_backend",
        "revoke:operation failed: backend failed",
    ]


class _InstalledGuardian:
    armed = True
    generation = 7

    def __init__(self, *, finish_error: Exception | None = None) -> None:
        self.installed: ActionCorrelation | None = None
        self.finish_calls = 0
        self.finish_error = finish_error

    def begin_action(self) -> ActionCorrelation:
        if self.installed is not None:
            raise GuardianError("action is still installed")
        self.installed = ActionCorrelation(
            7,
            41,
            4,
            2,
            frozenset(),
            None,
            time.monotonic(),
            time.monotonic() + 10.0,
        )
        return self.installed

    def finish_action(self, generation: int, action: ActionCorrelation) -> None:
        assert generation == 7
        assert action is self.installed
        self.finish_calls += 1
        if not self.armed:
            raise GuardianError("action was revoked")
        if self.generation != generation:
            raise GuardianError("action generation changed")
        self.installed = None
        if self.finish_error is not None:
            raise self.finish_error


@pytest.mark.parametrize(
    ("error_type", "message"),
    [(GuardianError, "guardian operation failed"), (RuntimeError, "operation failed")],
)
def test_runner_finishes_current_action_after_operation_failure(
    error_type: type[Exception], message: str
) -> None:
    guardian = _InstalledGuardian()
    runner = GuardedActionRunner(guardian, lambda _reason: None)
    finalization: list[tuple[bool, Exception | None]] = []

    def fail(_action: ActionCorrelation) -> None:
        raise error_type(message)

    with pytest.raises(error_type, match=message):
        runner.run(
            generation=7,
            begin=guardian.begin_action,
            dispatch=lambda: None,
            operation=fail,
            wait=lambda _action: None,
            failure_reason=lambda exc: str(exc),
            guardian_failure_reason=None,
            finish_when_current=True,
            on_finish=lambda succeeded, error: finalization.append((succeeded, error)),
        )

    assert guardian.finish_calls == 1
    assert guardian.installed is None
    assert finalization == [(True, None)]

    runner.run(
        generation=7,
        begin=guardian.begin_action,
        dispatch=lambda: None,
        operation=lambda _action: None,
        wait=lambda _action: None,
        failure_reason=lambda exc: str(exc),
        guardian_failure_reason=None,
        finish_when_current=True,
    )
    assert guardian.finish_calls == 2
    assert guardian.installed is None


@pytest.mark.parametrize(
    ("invalidate", "message"),
    [
        ("revoke", "action was revoked"),
        ("generation", "action generation changed"),
    ],
)
def test_runner_unconditionally_finishes_after_wait_race(invalidate: str, message: str) -> None:
    guardian = _InstalledGuardian()
    runner = GuardedActionRunner(guardian, lambda _reason: None)

    def wait(_action: ActionCorrelation) -> None:
        if invalidate == "revoke":
            guardian.armed = False
        else:
            guardian.generation = 8

    with pytest.raises(GuardianError, match=message):
        runner.run(
            generation=7,
            begin=guardian.begin_action,
            dispatch=lambda: None,
            operation=lambda _action: None,
            wait=wait,
            failure_reason=lambda exc: str(exc),
            guardian_failure_reason=None,
            on_finish=lambda succeeded, error: None,
        )

    assert guardian.finish_calls == 1


def test_runner_finish_error_overrides_success_and_finalizes_callback() -> None:
    finish_error = RuntimeError("finish failed")
    guardian = _InstalledGuardian(finish_error=finish_error)
    runner = GuardedActionRunner(guardian, lambda _reason: None)
    finalization: list[tuple[bool, Exception | None]] = []

    with pytest.raises(RuntimeError) as raised:
        runner.run(
            generation=7,
            begin=guardian.begin_action,
            dispatch=lambda: None,
            operation=lambda _action: "result",
            wait=lambda _action: None,
            failure_reason=lambda exc: str(exc),
            guardian_failure_reason=None,
            on_finish=lambda succeeded, error: finalization.append((succeeded, error)),
        )

    assert raised.value is finish_error
    assert finalization == [(False, finish_error)]
    assert guardian.finish_calls == 1
    assert guardian.installed is None


def test_runner_finish_error_overrides_exception_and_finalizes_callback() -> None:
    finish_error = RuntimeError("finish failed")
    guardian = _InstalledGuardian(finish_error=finish_error)
    runner = GuardedActionRunner(guardian, lambda _reason: None)
    finalization: list[tuple[bool, Exception | None]] = []

    def fail(_action: ActionCorrelation) -> None:
        raise GuardianError("operation failed")

    with pytest.raises(RuntimeError) as raised:
        runner.run(
            generation=7,
            begin=guardian.begin_action,
            dispatch=lambda: None,
            operation=fail,
            wait=lambda _action: None,
            failure_reason=lambda exc: str(exc),
            guardian_failure_reason=None,
            finish_when_current=True,
            on_finish=lambda succeeded, error: finalization.append((succeeded, error)),
        )

    assert raised.value is finish_error
    assert finalization == [(False, finish_error)]
    assert guardian.finish_calls == 1
    assert guardian.installed is None


def test_runner_can_suppress_finish_error_for_executor_receipt_cleanup() -> None:
    finish_error = RuntimeError("finish failed")
    guardian = _InstalledGuardian(finish_error=finish_error)
    runner = GuardedActionRunner(guardian, lambda _reason: None)
    finalization: list[tuple[bool, Exception | None]] = []

    result = runner.run(
        generation=7,
        begin=guardian.begin_action,
        dispatch=lambda: None,
        operation=lambda _action: "receipt",
        wait=lambda _action: None,
        failure_reason=lambda exc: str(exc),
        guardian_failure_reason=None,
        finish_when_current=True,
        on_finish=lambda succeeded, error: finalization.append((succeeded, error)),
        raise_finish_error=False,
    )

    assert result == "receipt"
    assert finalization == [(False, finish_error)]
    assert guardian.finish_calls == 1
    assert guardian.installed is None


class _PathGuardian:
    armed = True
    generation = 7

    def __init__(self, calls: list[str], *, revoke_before_dispatch: bool = False) -> None:
        self.calls = calls
        self.revoke_before_dispatch = revoke_before_dispatch
        self.action: ActionCorrelation | None = None
        self.admission_depth = 0

    def begin_action(self, generation: int, **_kwargs: object) -> ActionCorrelation:
        self.calls.append("guardian.begin_action")
        self.action = ActionCorrelation(
            generation,
            41,
            4,
            2,
            frozenset(),
            None,
            time.monotonic(),
            time.monotonic() + 10.0,
        )
        return self.action

    def dispatch_admission(self, generation: int, dispatch: Callable[[], object]) -> object:
        assert generation == self.generation
        self.calls.append("guardian.dispatch_admission")
        if self.revoke_before_dispatch:
            self.armed = False
            self.calls.append("guardian.revoke")
            raise GuardianError("admission revoked")
        self.admission_depth += 1
        try:
            return dispatch()
        finally:
            self.admission_depth -= 1

    def check(self, generation: int) -> None:
        assert generation == self.generation
        self.calls.append("guardian.check")

    def finish_action(self, generation: int, action: ActionCorrelation) -> None:
        assert generation == self.generation
        assert action is self.action
        self.calls.append("guardian.finish_action")


class _PathBackend:
    def __init__(self, identity: WindowIdentity, calls: list[str]) -> None:
        self.identity = identity
        self.calls = calls
        self.pointer = (20, 20, identity.child_xid)
        self.final_pointer = self.pointer
        self.fail_run = False
        self.last_pointer_execution: dict[str, object] | None = None
        self.admission_active: Callable[[], bool] | None = None
        self.action_admission_states: list[bool] = []

    def action(self, name: str, **_kwargs: object) -> None:
        in_admission = self.admission_active() if self.admission_active is not None else False
        self.action_admission_states.append(in_admission)
        self.calls.append(
            "backend.action.in_admission" if in_admission else "backend.action.outside_admission"
        )
        if self.fail_run:
            raise RuntimeError(f"{name} failed")

    def _run(self, _argv: list[str], **_kwargs: object) -> None:
        self.calls.append("backend._run")
        if self.fail_run:
            raise RuntimeError("native emission failed")
        self.pointer = self.final_pointer

    def pointer_position(self, **_kwargs: object) -> tuple[int, int, int]:
        return self.pointer


class _PathMonitor:
    alive = True

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def wait_for_event(self, predicate: Callable[[list[object]], bool], *, timeout: float) -> None:
        del timeout
        self.calls.append("wait_for_event")
        assert predicate([])


class _PathPermit:
    is_open = True
    acquired_monotonic = time.monotonic() - 1.0

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        del deadline_monotonic
        return time.monotonic_ns()

    def record_press(self) -> int:
        return time.monotonic_ns()

    def close(self) -> None:
        return None


class _PathBroker:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def reserve_button_rate(self, **_kwargs: object) -> object:
        self.calls.append("broker.reserve")
        return object()

    def release_reservation(self, _reservation: object) -> None:
        self.calls.append("broker.release")

    def mint_admitted_gesture(self, **_kwargs: object) -> object:
        self.calls.append("broker.mint")
        return SimpleNamespace(precommit_monotonic_ns=time.monotonic_ns())

    def emit_button_gesture(
        self, _gesture: object, *, emit: Callable[[object], None], **_kwargs: object
    ) -> int:
        self.calls.append("broker.emit")
        emit(object())
        return time.monotonic_ns()


def _path_identity() -> WindowIdentity:
    return WindowIdentity(
        1,
        2,
        "game",
        "Game",
        "game",
        41,
        (0, 0, 400, 300),
        (0, 0, 400, 300),
        1,
        "start",
        ":test",
        "client",
        target_event_xids=frozenset({41}),
    )


def _path_driver(
    calls: list[str], *, revoke_before_dispatch: bool = False
) -> tuple[DesktopDriver, _PathGuardian, _PathBackend]:
    identity = _path_identity()
    backend = _PathBackend(identity, calls)
    guardian = _PathGuardian(calls, revoke_before_dispatch=revoke_before_dispatch)
    backend.admission_active = lambda: guardian.admission_depth > 0
    driver = object.__new__(DesktopDriver)
    driver.backend = backend
    driver.guardian = guardian
    driver._guarded_actions = GuardedActionRunner(guardian, driver.revoke)
    driver._held_input = False
    driver._monitor = _PathMonitor(calls)
    driver._input_evidence = SimpleNamespace(
        xtest_profile=None,
        synthetic_key_source=4,
        synthetic_pointer_source=4,
        xinput_hierarchy=((4, 2, "slave", "pointer:xtest"),),
        synthetic_echo_proof=(),
        forwarded_echo_sources=(),
        synthetic_modifiers=(0, 0, 0),
    )
    driver._named_capture_sequence = 1
    driver._pointer_gesture_receipts = []
    driver.last_pointer_execution = None
    driver.input_broker = _PathBroker(calls)
    driver.pointer_motion_profile = MotionProfile(min_dwell=0.05, max_dwell=0.05)
    driver._clock = time.monotonic
    driver._consumed_anchor_seals = set()
    driver._last_takeover_hover_failure_context = None
    driver._identity = lambda **_kwargs: identity
    driver.resolve_identity = lambda **_kwargs: identity
    driver._is_owned_client_target = lambda xid, current: xid == current.child_xid
    driver._expected_modifiers = lambda *_args, **_kwargs: (0, 0, 0)
    driver._keyboard_operation_facts = lambda *_args, **_kwargs: ()
    driver._expected_key_operation = lambda *_args, **_kwargs: (
        ("RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"),
        (36, 36, 36, 36),
        ((0, 0, 0),) * 4,
    )
    driver._motion_input_context = lambda *_args, **_kwargs: (
        identity,
        driver._input_evidence,
        4,
        2,
        (0, 0, 0),
        {"w": 25, "a": 38, "s": 39, "d": 40, "space": 65},
    )

    def revoke(reason: str) -> int:
        calls.append("driver.revoke")
        guardian.armed = False
        return guardian.generation + 1

    driver.revoke = revoke
    driver._guarded_actions = GuardedActionRunner(guardian, driver.revoke)
    return driver, guardian, backend


def _path_frame() -> SimpleNamespace:
    return SimpleNamespace(
        identity_fingerprint="client",
        capture_monotonic=time.monotonic() - 0.5,
        hover_kind="object",
        hover_name="target",
        chat_focused=False,
        binding=lambda name: {
            "moveForward": "W",
            "turnLeft": "A",
            "turnRight": "D",
            "jump": "SPACE",
        }[name],
    )


def test_real_driver_motion_hold_uses_the_guarded_sequence() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)
    backend.final_pointer = backend.pointer
    frame = _path_frame()

    driver.run_motion_key_hold(KeyRole.MOVE_FORWARD, 0.05, frame, generation=7)

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend._run",
        "guardian.check",
        "wait_for_event",
        "guardian.finish_action",
    ]


def test_real_driver_timeline_uses_the_guarded_sequence() -> None:
    from crowley.session.route_walk import KeyEvent

    calls: list[str] = []
    driver, _guardian, _backend = _path_driver(calls)
    frame = _path_frame()
    events = (KeyEvent(0.0, "W", True), KeyEvent(0.1, "W", False))

    driver.run_key_timeline(events, frame, generation=7)

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend._run",
        "guardian.check",
        "wait_for_event",
        "guardian.finish_action",
    ]


def test_real_driver_pointer_motion_uses_the_guarded_sequence() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)
    backend.final_pointer = (100, 120, backend.identity.child_xid)

    driver.run_pointer_motion(Point(100, 120), generation=7)

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend._run",
        "guardian.check",
        "wait_for_event",
        "guardian.finish_action",
    ]


def test_real_driver_hover_click_keeps_broker_before_backend_and_finish() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)
    point = Point(100, 120)
    binding = BindingMetadata("client", "hover", 1, 7, 41, (41,))
    gesture = plan_human_pointer(
        Point(100, 120),
        PointerTarget("world-hover", Rect(100, 120, 1, 1), binding, padding=0),
        Rect(0, 0, 400, 300),
        button=None,
    )
    reservation = RateReservation(_PathPermit())
    backend.pointer = (100, 120, backend.identity.child_xid)
    backend.final_pointer = backend.pointer
    frame = _path_frame()

    driver.run_hover_click(
        point,
        frame,
        gesture=gesture,
        gesture_ended_monotonic=time.monotonic() - 2.0,
        generation=7,
        rate_reservation=reservation,
        target_name="target",
    )

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "broker.mint",
        "broker.emit",
        "backend._run",
        "guardian.check",
        "wait_for_event",
        "guardian.finish_action",
    ]


def test_real_driver_camera_drag_releases_broker_after_finish() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)
    binding = BindingMetadata("client", "camera", 1, 7, 41, (41,))
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (PointerSample(0.0, Point(20, 20)), PointerSample(0.1, Point(100, 120))),
        0.1,
        Point(80, 100),
        binding,
    )
    backend.final_pointer = (100, 120, backend.identity.child_xid)

    driver.run_camera_drag(plan, generation=7)

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "broker.reserve",
        "broker.mint",
        "broker.emit",
        "backend._run",
        "guardian.check",
        "wait_for_event",
        "guardian.finish_action",
        "broker.release",
    ]


def test_real_driver_camera_drag_revokes_before_broker_release_on_emission_failure() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)
    backend.fail_run = True
    binding = BindingMetadata("client", "camera", 1, 7, 41, (41,))
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (PointerSample(0.0, Point(20, 20)), PointerSample(0.1, Point(100, 120))),
        0.1,
        Point(80, 100),
        binding,
    )

    with pytest.raises(RuntimeError, match="native emission failed"):
        driver.run_camera_drag(plan, generation=7)

    assert calls == [
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "broker.reserve",
        "broker.mint",
        "broker.emit",
        "backend._run",
        "driver.revoke",
        "broker.release",
    ]


def test_real_driver_focus_admission_fences_non_x11_backend_action() -> None:
    calls: list[str] = []
    driver, _guardian, backend = _path_driver(calls)

    driver.run("focus_game", 7)

    assert calls == [
        "guardian.check",
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend.action.in_admission",
        "guardian.finish_action",
    ]
    assert backend.action_admission_states == [True]


def test_real_driver_focus_and_generic_run_use_guarded_sequences() -> None:
    calls: list[str] = []
    driver, _guardian, _backend = _path_driver(calls)

    driver.run("focus_game", 7)
    assert calls == [
        "guardian.check",
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend.action.in_admission",
        "guardian.finish_action",
    ]

    calls.clear()
    driver.run(
        "open_chat",
        7,
        precondition=lambda _identity: True,
        postcondition=lambda _identity: True,
    )
    assert calls == [
        "guardian.check",
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "backend.action.in_admission",
        "guardian.finish_action",
    ]


def test_real_driver_takeover_hover_uses_guarded_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    driver, _guardian, _backend = _path_driver(calls)
    identity = _path_identity()
    backend = object.__new__(X11Backend)
    backend.last_pointer_execution = None
    backend.pointer_position = lambda **_kwargs: (20, 20, identity.child_xid)
    backend._run = lambda _argv, **_kwargs: calls.append("backend._run")
    driver.backend = backend
    environment = SimpleNamespace(
        fingerprint="environment",
        hierarchy=(),
        keyboard_source=4,
        keyboard_master=2,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(),
        xtest_profile=SimpleNamespace(root_xid=1, verify=lambda: None),
        display=":test",
    )
    evidence = RuntimeInputEvidence(
        process_id=__import__("os").getpid(),
        issued_monotonic_ns=1,
        readiness_boot="boot",
        readiness_sequence=1,
        environment=environment,
        game_identity_fingerprint=identity.fingerprint,
        game_process_start=identity.process_start,
        game_geometry=identity.child_geometry,
        keyboard_keymap=(),
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=(),
        event_provenance="test",
        _seal="test",
    )
    monkeypatch.setattr(RuntimeInputEvidence, "verify_seal", lambda _evidence: None)

    driver._input_evidence = evidence
    driver._monitor = _PathMonitor(calls)
    driver._monitor.alive = True
    driver._last_takeover_hover_failure_context = None

    driver.run_takeover_hover(7, evidence)

    assert calls == [
        "guardian.check",
        "guardian.begin_action",
        "guardian.dispatch_admission",
        "guardian.check",
        "backend._run",
        "wait_for_event",
        "guardian.finish_action",
    ]
