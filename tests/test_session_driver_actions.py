from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session import pointer_execution
from crowley.session.calibration import XTestInputProfile
from crowley.session.config import SessionConfig
from crowley.session.daemon import SessionDaemon
from crowley.session.driver import (
    DesktopDriver,
    DriverError,
    DriverProfile,
    MeasuredAnchor,
    PinnedX11IdentityObserver,
    WindowIdentity,
    X11Backend,
)
from crowley.session.executor import (
    BindingMetadata,
    CameraDragPlan,
    DemonstrationRole,
    KeyRole,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    Rect,
    plan_pointer_curve,
)
from crowley.session.input_broker import InputBroker, InputBrokerError, RateReservation
from crowley.session.input_events import EventClass, XI2Event
from crowley.session.input_tool import PinnedInputTool
from crowley.session.pointer_execution import PointerExecutionError
from crowley.session.process_admission import AdmissionGate
from tests.test_session_input_tool import fixture_input_tool


def _identity() -> WindowIdentity:
    return WindowIdentity(
        0x100,
        123,
        "game.exe",
        "World of Warcraft",
        "wow",
        0x101,
        (0, 0, 400, 300),
        (0, 0, 400, 300),
        0x100,
        "start",
        ":actions",
        "identity",
        0x1,
        0x100,
        frozenset({0x100, 0x101}),
    )


def _evidence() -> SimpleNamespace:
    hierarchy = ((4, 2, "slave", "pointer:xtest"),)
    return SimpleNamespace(
        is_live=True,
        fingerprint="input-evidence",
        synthetic_key_source=4,
        synthetic_pointer_source=4,
        xinput_hierarchy=hierarchy,
        xtest_profile=XTestInputProfile(
            4,
            2,
            "pointer:xtest",
            3,
            ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
            1,
            1,
            (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            0x1,
            (0, 0, 1920, 1080),
            hierarchy,
        ),
        synthetic_modifiers=(0, 0, 0),
        synthetic_echo_proof=(),
        keyboard_keymap=(
            ("a", 38),
            ("d", 40),
            ("s", 39),
            ("w", 25),
        ),
        event_proof=(
            ("KeyPress", 0, 4, 4, 0, 0, 0, 0, 0, 0, (0, 0, 0), 0),
            ("KeyRelease", 0, 4, 4, 0, 0, 0, 0, 0, 0, (0, 0, 0), 0),
        ),
    )


class _EventMonitor:
    def __init__(self, events: list[SimpleNamespace]):
        self.events = events

    def wait_for_event(self, predicate, *, timeout: float) -> None:
        del timeout
        for event in self.events:
            # The callback is installed by the test on the guardian.  The
            # predicate is only a completion check, so feed events directly.
            self.callback(event)
            if predicate(self.events):
                return
        assert predicate(self.events)

    def set_callback(self, callback) -> None:
        self.callback = callback

    def stop(self) -> None:
        return None


def _event(
    event_class: EventClass,
    timestamp: int,
    *,
    source: int = 4,
    master: int = 4,
    target: int | None = 0x101,
    detail: int = 25,
    root: tuple[int, int] | None = None,
    event_xy: tuple[int, int] | None = None,
    valuators: tuple[tuple[int, float, float | None], ...] = (),
) -> SimpleNamespace:
    raw = event_class.value.startswith("Raw")
    if raw:
        master = 2
        target = None
    type_number = {
        EventClass.RAW_KEY_PRESS: 13,
        EventClass.KEY_PRESS: 2,
        EventClass.RAW_KEY_RELEASE: 14,
        EventClass.KEY_RELEASE: 3,
        EventClass.RAW_BUTTON_PRESS: 15,
        EventClass.BUTTON_PRESS: 4,
        EventClass.RAW_BUTTON_RELEASE: 16,
        EventClass.BUTTON_RELEASE: 5,
        EventClass.MOTION: 6,
        EventClass.RAW_MOTION: 17,
    }[event_class]
    return XI2Event(
        type_number,
        event_class,
        master,
        source,
        timestamp,
        0 if event_class in {EventClass.MOTION, EventClass.RAW_MOTION} else detail,
        0x1 if event_class in {EventClass.MOTION, EventClass.RAW_MOTION} else None,
        0x1 if event_class in {EventClass.MOTION, EventClass.RAW_MOTION} else None,
        target,
        root,
        event_xy if event_xy is not None else root,
        None if raw else (0, 0, 0),
        modifier_effective=None if raw else 0,
        valuators=valuators,
    )


def _motion_pair(
    timestamp: int,
    origin: tuple[int, int],
    endpoint: tuple[int, int],
) -> tuple[SimpleNamespace, SimpleNamespace]:
    valuators = ((0, float(endpoint[0]), float(endpoint[0])), (1, float(endpoint[1]), float(endpoint[1])))
    return (
        _event(
            EventClass.RAW_MOTION,
            timestamp,
            root=None,
            valuators=valuators,
        ),
        _event(
            EventClass.MOTION,
            timestamp,
            root=origin,
            event_xy=origin,
            valuators=valuators,
        ),
    )


def _driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, executable_source: str = "") -> DesktopDriver:
    rate_directory = tmp_path / "pointer-rate-state"
    rate_directory.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: rate_directory)
    backend = X11Backend(
        DriverProfile(display=":actions", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool", executable_source or "/bin/true"),
    )
    identity = _identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    backend.observe_keymap = lambda: {"a": 38, "d": 40, "s": 39, "w": 25}  # type: ignore[method-assign]
    backend.observe_keymap_levels = lambda: {25: ("w", "W"), 38: ("a", "A"), 39: ("s", "S"), 40: ("d", "D")}  # type: ignore[method-assign]
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: "/bin/true")
    driver = DesktopDriver(profile=backend.profile, backend=backend, lease_path=tmp_path / "actions.lock")
    driver._input_evidence = _evidence()
    driver.input_hierarchy = lambda **_kwargs: driver._input_evidence.xinput_hierarchy  # type: ignore[method-assign]
    backend.xtest_input_profile = lambda _hierarchy, **_kwargs: driver._input_evidence.xtest_profile  # type: ignore[method-assign]
    return driver


def _build_logout_anchor(
    driver: DesktopDriver,
    identity: WindowIdentity,
    generation: int,
    capture_path: Path,
    sequence: int,
) -> MeasuredAnchor:
    capture_path.write_bytes(f"capture-{sequence}".encode("ascii"))
    capture_id = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    now = driver._clock()
    return driver.build_measured_anchor(
        action="logout",
        x=260,
        y=210,
        capture_path=capture_path,
        capture_id=capture_id,
        capture_sequence=sequence,
        capture_monotonic=now,
        scene="world",
        bounds=(240, 190, 60, 40),
        target_xid=identity.child_xid,
        target_ancestry=(identity.child_xid,),
        generation=generation,
        facts=(("control", "world"),),
    )


def _install_deadline_keyboard_runtime(
    driver: DesktopDriver,
    monkeypatch: pytest.MonkeyPatch,
    commands: list[tuple[list[str], float | None]],
    *,
    focus=None,
) -> None:
    """Keep the real driver/Guardian/X11 action path while replacing X11 I/O."""

    identity = _identity()
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    backend.observe_keymap = lambda **_kwargs: {  # type: ignore[method-assign]
        "Return": 36,
        "Escape": 9,
        "slash": 61,
        "space": 65,
        **{char: 30 + index for index, char in enumerate("abcdefghijklmnopqrstuvwxyz")},
    }
    backend._focus = (  # type: ignore[method-assign]
        focus if focus is not None else lambda *_args, **_kwargs: None
    )

    def run_native(args, *, deadline_monotonic=None, **_kwargs):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("native command deadline expired")
        commands.append((list(args), deadline_monotonic))
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run_native  # type: ignore[method-assign]
    steps = (
        ("press", "Return", 36, (0, 0, 0), 0),
        ("release", "Return", 36, (0, 0, 0), 0),
    )
    monkeypatch.setattr(
        driver,
        "_keyboard_operation_facts",
        lambda *_args, **_kwargs: steps,
    )
    monkeypatch.setattr(
        driver,
        "_sealed_keyboard_keymap",
        lambda *_args, **_kwargs: {
            "Return": 36,
            "Escape": 9,
            "slash": 61,
            "space": 65,
            **{char: 30 + index for index, char in enumerate("abcdefghijklmnopqrstuvwxyz")},
        },
    )
    monkeypatch.setattr(driver, "_expected_modifiers", lambda *_args, **_kwargs: (0, 0, 0))
    monkeypatch.setattr(
        driver,
        "_expected_key_operation",
        lambda *_args, **_kwargs: (
            ("RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"),
            (36, 36, 36, 36),
            ((0, 0, 0),) * 4,
        ),
    )


def test_anchor_observation_deadline_reaches_initial_and_refresh_providers_and_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    identity = _identity()
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    provider_deadlines: list[float | None] = []
    resolve_deadlines: list[float | None] = []
    original_resolve = driver.resolve_identity
    original_deadline = driver._clock() + 5.0
    refreshed_deadline = original_deadline - 1.0
    sequence = 0

    def observed_resolve(*args, **kwargs):  # type: ignore[no-untyped-def]
        resolve_deadlines.append(kwargs.get("deadline_monotonic"))
        return original_resolve(*args, **kwargs)

    def provider(provider_identity: WindowIdentity, action: str) -> MeasuredAnchor:
        nonlocal sequence
        assert action == "logout"
        assert provider_identity.fingerprint == identity.fingerprint
        provider_deadlines.append(driver.anchor_observation_deadline)
        sequence += 1
        return _build_logout_anchor(
            driver, provider_identity, generation, tmp_path / f"capture-{sequence}.bin", sequence
        )

    driver.resolve_identity = observed_resolve  # type: ignore[method-assign]
    driver.measured_anchor_provider = provider
    try:
        first = driver.measure_pointer_anchor(
            "logout", generation=generation, deadline_monotonic=original_deadline
        )
        assert driver.anchor_observation_deadline is None
        refreshed = driver._refresh_pointer_anchor(
            "logout",
            generation,
            identity,
            first,
            deadline_monotonic=refreshed_deadline,
        )
        assert refreshed.capture_sequence == 2
        assert provider_deadlines == [original_deadline, refreshed_deadline]
        assert resolve_deadlines == [
            original_deadline,
            original_deadline,
            refreshed_deadline,
            refreshed_deadline,
        ]
        assert driver.anchor_observation_deadline is None
    finally:
        driver.close()


def test_anchor_observation_deadline_nested_scope_restores_after_provider_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    outer = driver._clock() + 5.0
    inner = outer - 1.0
    observed: list[float | None] = []

    def provider(_identity: WindowIdentity, _action: str) -> MeasuredAnchor:
        observed.append(driver.anchor_observation_deadline)
        with driver._anchor_observation_scope(outer + 10.0):
            observed.append(driver.anchor_observation_deadline)
        with driver._anchor_observation_scope(inner):
            observed.append(driver.anchor_observation_deadline)
        raise RuntimeError("provider exploded")

    driver.measured_anchor_provider = provider
    try:
        with pytest.raises(DriverError, match="native anchor measurement failed.*provider exploded"):
            driver.measure_pointer_anchor("logout", generation=1, deadline_monotonic=outer)
        assert observed == [outer, outer, inner]
        assert driver.anchor_observation_deadline is None
    finally:
        driver.close()


def test_expired_anchor_observation_deadline_rejects_before_provider_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    provider_calls: list[tuple[WindowIdentity, str]] = []
    driver.measured_anchor_provider = lambda identity, action: provider_calls.append((identity, action))  # type: ignore[assignment]
    try:
        with pytest.raises(DriverError, match="expired before identity"):
            driver.measure_pointer_anchor("logout", generation=1, deadline_monotonic=driver._clock() - 1.0)
        assert provider_calls == []
    finally:
        driver.close()


def test_legacy_unbounded_anchor_provider_retains_none_scope_and_call_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    resolve_kwargs: list[dict[str, object]] = []
    original_resolve = driver.resolve_identity

    def observed_resolve(*args, **kwargs):  # type: ignore[no-untyped-def]
        resolve_kwargs.append(dict(kwargs))
        return original_resolve(*args, **kwargs)

    def provider(provider_identity: WindowIdentity, _action: str) -> MeasuredAnchor:
        assert driver.anchor_observation_deadline is None
        return _build_logout_anchor(driver, provider_identity, generation, tmp_path / "legacy-capture.bin", 1)

    driver.resolve_identity = observed_resolve  # type: ignore[method-assign]
    driver.measured_anchor_provider = provider
    try:
        anchor = driver.measure_pointer_anchor("logout", generation=generation)
        assert anchor.capture_id == hashlib.sha256((tmp_path / "legacy-capture.bin").read_bytes()).hexdigest()
        assert resolve_kwargs == [{"deadline_monotonic": None}, {"require_foreground": False}]
        assert driver.anchor_observation_deadline is None
    finally:
        driver.close()


def _fd_identities() -> set[tuple[int, int, str]]:
    """Return stable process-FD identities, tolerating concurrent closes."""

    identities: set[tuple[int, int, str]] = set()
    for raw_fd in os.listdir("/proc/self/fd"):
        try:
            fd = int(raw_fd)
            descriptor = os.fstat(fd)
            link = os.readlink(f"/proc/self/fd/{fd}")
        except (OSError, ValueError):
            continue
        identities.add((descriptor.st_dev, descriptor.st_ino, link))
    return identities


def _fd_identity(fd: int) -> tuple[int, int, str]:
    descriptor = os.fstat(fd)
    return descriptor.st_dev, descriptor.st_ino, os.readlink(f"/proc/self/fd/{fd}")


def test_run_motion_key_hold_uses_real_driver_entrypoint_and_sealed_level_zero_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    monitor = _EventMonitor(
        [
            _event(EventClass.RAW_KEY_PRESS, 1, detail=25),
            _event(EventClass.KEY_PRESS, 2, detail=25),
            _event(EventClass.RAW_KEY_RELEASE, 3, detail=25),
            _event(EventClass.KEY_RELEASE, 4, detail=25),
        ]
    )
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    frame = SimpleNamespace(binding=lambda role: {"moveForward": "W"}[role])
    try:
        driver.run_motion_key_hold(KeyRole.MOVE_FORWARD, 0.05, frame, generation=generation)
        assert driver.guardian.armed
    finally:
        driver.close()


def test_run_motion_key_hold_rejects_shift_level_drift_before_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    driver.backend.observe_keymap_levels = lambda: {25: ("W", "w")}  # type: ignore[method-assign]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    frame = SimpleNamespace(binding=lambda role: {"moveForward": "W"}[role])
    try:
        with pytest.raises(DriverError, match="level-zero"):
            driver.run_motion_key_hold(KeyRole.MOVE_FORWARD, 0.05, frame, generation=generation)
    finally:
        driver.close()


@pytest.mark.parametrize("operation", ["click", "camera"])
def test_native_pointer_producers_reject_profile_drift_before_input(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    sealed = driver._input_evidence.xtest_profile
    commands: list[list[str]] = []
    monkeypatch.setattr(
        driver.backend,
        "_run",
        lambda args, **_kwargs: commands.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    driver.backend.xtest_input_profile = lambda _hierarchy, **_kwargs: replace(  # type: ignore[method-assign]
        sealed, root_geometry=(0, 0, 1920, 1081)
    )
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    try:
        if operation == "click":
            with pytest.raises(DriverError, match="profile changed"):
                driver.run(
                    "logout",
                    generation,
                    precondition=lambda _identity: True,
                    postcondition=lambda _identity: True,
                )
        else:
            binding = BindingMetadata("identity", "capture", 1, generation, 4, (4,), boot="boot")
            plan = CameraDragPlan(
                PointerButton.RIGHT,
                (PointerSample(0.0, Point(100, 100)), PointerSample(0.05, Point(110, 120))),
                0.05,
                Point(10, 20),
                binding,
            )
            with pytest.raises(DriverError, match="profile changed"):
                driver.run_camera_drag(plan, generation=generation)
        assert commands == []
    finally:
        driver.close()


def test_x11_pointer_backend_refuses_unplanned_direct_click_before_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        driver.backend,
        "_run",
        lambda args, **_kwargs: commands.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    try:
        with pytest.raises(DriverError, match="validated planned gesture"):
            driver.backend.action("logout", generation=1)
        assert commands == []
    finally:
        driver.close()


def test_runtime_pointer_gesture_seed_and_emission_timing_reach_driver_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    identity = _identity()
    pointer = [20, 20, identity.child_xid]
    commands: list[list[str]] = []
    seed = 0x123456789
    now = time.monotonic()

    def run_action(args, *, input_schema=None, **kwargs):
        commands.append(list(args))
        if input_schema == "pointer-motion":
            for index, value in enumerate(args[:-2]):
                if value == "mousemove":
                    pointer[0] = int(args[index + 1])
                    pointer[1] = int(args[index + 2])
                    pointer[2] = identity.child_xid
        elif input_schema == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )

    backend = driver.backend
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    backend._focus = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    backend.pointer_position = lambda **_kwargs: tuple(pointer)  # type: ignore[method-assign]
    backend._run = run_action  # type: ignore[method-assign]
    monkeypatch.setattr(pointer_execution.secrets, "randbits", lambda _bits: seed)
    monkeypatch.setattr(driver, "_verify_current_xtest_profile", lambda *_args, **_kwargs: None)
    anchor = MeasuredAnchor(
        action="logout",
        x=260,
        y=210,
        identity_fingerprint=identity.fingerprint,
        target_xid=identity.child_xid,
        capture_id="capture-one",
        capture_sequence=1,
        source="native-visual",
        observed_monotonic=now,
        capture_monotonic=now,
        facts=(
            ("capture", "capture-one"),
            ("control", "world"),
            ("target", str(identity.child_xid)),
        ),
        generation=1,
        bounds=(240, 190, 60, 40),
        target_ancestry=(identity.child_xid,),
    ).with_hash()
    binding = BindingMetadata(
        identity.fingerprint,
        anchor.capture_id,
        anchor.capture_sequence,
        1,
        anchor.target_xid,
        anchor.target_ancestry,
        source=anchor.source,
    )
    expected = [
        pointer_execution.plan_human_pointer(
            Point(pointer[0], pointer[1]),
            PointerTarget("world", Rect(*anchor.bounds), binding, padding=1),
            Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
            seed=seed,
            button=PointerButton.LEFT,
            profile=driver.pointer_motion_profile,
        )
    ]

    def measure_pointer_anchor(name, *, generation):
        assert name == "logout" and generation == 1
        driver._measured_anchors[name] = anchor
        driver._measured_anchor_generations[name] = generation
        backend._measured_anchors[name] = anchor
        return anchor

    def consume_pointer_anchor(name, measured):
        assert name == "logout" and measured is anchor
        backend._consumed_anchor_seals.add(anchor.evidence_hash)

    def refresh_pointer_anchor(_name, _generation, _identity, previous):
        plan = expected[0]
        assert plan is not None and previous is anchor
        return MeasuredAnchor(
            action="logout",
            x=plan.endpoint.x,
            y=plan.endpoint.y,
            identity_fingerprint=identity.fingerprint,
            target_xid=identity.child_xid,
            capture_id="capture-two",
            capture_sequence=2,
            source="native-visual",
            observed_monotonic=time.monotonic(),
            capture_monotonic=time.monotonic(),
            facts=(("control", "world"),),
            generation=1,
            bounds=anchor.bounds,
            target_ancestry=anchor.target_ancestry,
        ).with_hash()

    driver.measure_pointer_anchor = measure_pointer_anchor  # type: ignore[method-assign]
    driver._refresh_pointer_anchor = refresh_pointer_anchor  # type: ignore[method-assign]
    backend.consume_measured_anchor = consume_pointer_anchor  # type: ignore[method-assign]
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    final_admission: list[int] = []

    try:
        result = driver.run(
            "logout",
            generation,
            precondition=lambda _identity: True,
            postcondition=lambda _identity: True,
            _final_admission=lambda: final_admission.append(len(commands)),
        )
        assert result is None
        assert final_admission == [1]
        receipt = driver.pointer_gesture_receipts[-1]
        execution = receipt["execution"]
        assert receipt["seed"] == seed
        assert receipt["frozenEndpoint"] == [int(expected[0].endpoint.x), int(expected[0].endpoint.y)]
        assert receipt["pathSha256"] == execution["pathSha256"]
        assert receipt["accepted"] is True
        assert execution["seed"] == receipt["seed"]
        assert execution["phase"] == "click_released_and_rate_recorded"
        assert execution["movementDispatchStartedMonotonicNs"] <= execution["movementAckMonotonicNs"]
        assert (
            execution["clickPrecommitMonotonicNs"]
            <= execution["clickDispatchStartedMonotonicNs"]
            <= execution["clickAckMonotonicNs"]
            <= execution["rateRecordMonotonicNs"]
        )
        execution["phase"] = "tampered"
        assert driver.pointer_gesture_receipts[-1]["execution"]["phase"] == (
            "click_released_and_rate_recorded"
        )
        assert len(commands) == 2
        assert pointer[:2] == [int(expected[0].endpoint.x), int(expected[0].endpoint.y)]
    finally:
        driver.close()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_executor_receipt_is_call_local_across_backend_substitution(
    cleanup_failure: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    identity = _identity()
    pointer = [20, 20, identity.child_xid]
    commands: list[list[str]] = []
    backend = driver.backend
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    backend._focus = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    backend.pointer_position = lambda **_kwargs: tuple(pointer)  # type: ignore[method-assign]

    def run_action(args, *, input_schema=None, **_kwargs):
        commands.append(list(args))
        if input_schema == "pointer-motion":
            for index, value in enumerate(args[:-2]):
                if value == "mousemove":
                    pointer[0] = int(args[index + 1])
                    pointer[1] = int(args[index + 2])
                    pointer[2] = identity.child_xid
        elif input_schema == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                _kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT,
                action_generation=_kwargs["emission_action_generation"],
                lease_generation=_kwargs["emission_lease_generation"],
            )
            # A later action could replace mutable backend history while this
            # call is still finalizing.  The returned receipt must not change.
            backend.last_pointer_execution = {"action": "substituted"}

    backend._run = run_action  # type: ignore[method-assign]
    monkeypatch.setattr(driver, "_verify_current_xtest_profile", lambda *_args, **_kwargs: None)
    now = time.monotonic()
    anchor = MeasuredAnchor(
        action="executor_click",
        x=260,
        y=210,
        identity_fingerprint=identity.fingerprint,
        target_xid=identity.child_xid,
        capture_id="executor-capture-one",
        capture_sequence=1,
        source="native-visual",
        observed_monotonic=now,
        capture_monotonic=now,
        facts=(
            ("capture", "executor-capture-one"),
            ("control", "crowley_executor_button"),
            ("target", str(identity.child_xid)),
        ),
        generation=1,
        bounds=(240, 190, 60, 40),
        target_ancestry=(identity.child_xid,),
    ).with_hash()

    def measure_pointer_anchor(name, *, generation):
        assert name == "executor_click" and generation == 1
        driver._measured_anchors[name] = anchor
        driver._measured_anchor_generations[name] = generation
        backend._measured_anchors[name] = anchor
        return anchor

    def refresh_pointer_anchor(_name, _generation, _identity, previous):
        assert previous is anchor
        return replace(
            anchor,
            capture_id="executor-capture-two",
            capture_sequence=2,
            facts=(
                ("capture", "executor-capture-two"),
                ("control", "crowley_executor_button"),
                ("target", str(identity.child_xid)),
            ),
            capture_monotonic=time.monotonic(),
            observed_monotonic=time.monotonic(),
        ).with_hash()

    driver.measure_pointer_anchor = measure_pointer_anchor  # type: ignore[method-assign]
    driver._refresh_pointer_anchor = refresh_pointer_anchor  # type: ignore[method-assign]
    backend.consume_measured_anchor = lambda _name, _anchor: backend._consumed_anchor_seals.add(
        anchor.evidence_hash
    )  # type: ignore[method-assign]
    monkeypatch.setattr(pointer_execution.secrets, "randbits", lambda _bits: 0x123456789)
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    if cleanup_failure:

        def fail_finish(*_args, **_kwargs):
            raise RuntimeError("guardian cleanup failed")

        driver.guardian.finish_action = fail_finish  # type: ignore[method-assign]
    else:
        driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    final_admission: list[int] = []
    try:
        if cleanup_failure:
            result = driver.run(
                "executor_click",
                generation,
                precondition=lambda _identity: True,
                postcondition=lambda _identity: True,
                _final_admission=lambda: final_admission.append(len(commands)),
            )
            assert result is not None
            assert result["guardianCompleted"] is False
        else:
            result = driver.run(
                "executor_click",
                generation,
                precondition=lambda _identity: True,
                postcondition=lambda _identity: True,
                _final_admission=lambda: final_admission.append(len(commands)),
            )
            assert result is not None
            assert result["action"] == "executor_click"
            assert result["guardianCompleted"] is True
            assert result["admittedAnchorEvidenceHash"] == anchor.evidence_hash
            assert result["admittedAnchorCaptureId"] == "executor-capture-one"
            assert result["admittedAnchorCaptureSequence"] == 1
            assert result["anchorEvidenceHash"] != anchor.evidence_hash
            assert result["anchorCaptureId"] == "executor-capture-two"
            assert result["anchorCaptureSequence"] == 2
            result["action"] = "tampered"
        assert final_admission == [1]
        execution = driver.pointer_gesture_receipts[-1]["execution"]
        assert execution["action"] == "executor_click"
        assert execution["guardianCompleted"] is not cleanup_failure
        assert driver.pointer_gesture_receipts[-1]["accepted"] is not cleanup_failure
        assert backend.last_pointer_execution == {"action": "substituted"}
    finally:
        driver.close()


def test_run_camera_drag_requires_position_press_held_motion_and_endpoint_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    pointer_positions = [(20, 20, 0x101), (130, 100, 0x101)]
    driver.backend.pointer_position = lambda **_kwargs: pointer_positions.pop(0)  # type: ignore[method-assign]
    binding = BindingMetadata("identity", "capture", 1, 1, 4, (4,), boot="boot")
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (PointerSample(0.0, Point(100, 100)), PointerSample(0.05, Point(130, 100))),
        0.05,
        Point(30, 0),
        binding,
    )
    positioning = plan_pointer_curve(
        Point(20, 20),
        PointerTarget("camera-viewport", Rect(100, 100, 1, 1), binding, padding=0),
        Rect(0, 0, 400, 300),
        profile=MotionProfile(role=DemonstrationRole.POINTER),
    )
    events: list[SimpleNamespace] = []
    timestamp = 1
    prior = positioning.samples[0].point
    for sample in positioning.samples[1:]:
        if sample.point != prior:
            events.extend(
                _motion_pair(
                    timestamp,
                    (int(prior.x), int(prior.y)),
                    (int(sample.point.x), int(sample.point.y)),
                )
            )
            timestamp += 1
            prior = sample.point
    events.extend(
        [
            _event(EventClass.RAW_BUTTON_PRESS, timestamp, detail=3),
            _event(EventClass.BUTTON_PRESS, timestamp + 1, detail=3, root=(100, 100), event_xy=(100, 100)),
            *_motion_pair(timestamp + 2, (100, 100), (130, 100)),
            _event(EventClass.RAW_BUTTON_RELEASE, timestamp + 3, detail=3),
            _event(EventClass.BUTTON_RELEASE, timestamp + 4, detail=3, root=(130, 100), event_xy=(130, 100)),
        ]
    )
    monitor = _EventMonitor(events)
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    plan = CameraDragPlan(
        plan.button,
        plan.samples,
        plan.duration,
        plan.signed_delta,
        replace(binding, generation=generation),
    )
    try:
        driver.run_camera_drag(plan, generation=generation)
        assert driver.guardian.armed
    finally:
        driver.close()


def test_run_camera_drag_rejects_all_identical_native_samples_before_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    driver.backend.pointer_position = lambda **_kwargs: (100, 100, 0x101)  # type: ignore[method-assign]
    monkeypatch.setattr(
        driver.backend,
        "_run",
        lambda args, **_kwargs: commands.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    binding = BindingMetadata("identity", "capture", 1, 1, 4, (4,), boot="boot")
    # Bypass constructor validation deliberately to exercise the defensive driver guard.
    plan = object.__new__(CameraDragPlan)
    object.__setattr__(plan, "button", PointerButton.RIGHT)
    object.__setattr__(
        plan,
        "samples",
        (PointerSample(0.0, Point(100, 100)), PointerSample(0.05, Point(100, 100))),
    )
    object.__setattr__(plan, "duration", 0.05)
    object.__setattr__(plan, "signed_delta", Point(0, 0))
    object.__setattr__(plan, "binding", replace(binding, generation=1))
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    try:
        with pytest.raises(DriverError, match="no native motion trace"):
            driver.run_camera_drag(plan, generation=generation)
        assert commands == []
    finally:
        driver.close()


def test_run_camera_drag_allows_press_at_start_without_positioning_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    pointer_positions = [(100, 100, 0x101), (130, 100, 0x101)]
    driver.backend.pointer_position = lambda **_kwargs: pointer_positions.pop(0)  # type: ignore[method-assign]
    binding = BindingMetadata("identity", "capture", 1, 1, 4, (4,), boot="boot")
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (
            PointerSample(0.0, Point(100, 100)),
            PointerSample(0.025, Point(115, 100)),
            PointerSample(0.05, Point(130, 100)),
        ),
        0.05,
        Point(30, 0),
        replace(binding, generation=1),
    )
    events = [
        _event(EventClass.RAW_BUTTON_PRESS, 1, detail=3),
        _event(EventClass.BUTTON_PRESS, 2, detail=3, root=(100, 100), event_xy=(100, 100)),
        *_motion_pair(3, (100, 100), (115, 100)),
        *_motion_pair(4, (115, 100), (130, 100)),
        _event(EventClass.RAW_BUTTON_RELEASE, 5, detail=3),
        _event(EventClass.BUTTON_RELEASE, 6, detail=3, root=(130, 100), event_xy=(130, 100)),
    ]
    monitor = _EventMonitor(events)
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    plan = replace(plan, binding=replace(binding, generation=generation))
    try:
        driver.run_camera_drag(plan, generation=generation)
        assert driver.guardian.armed
    finally:
        driver.close()


def _camera_failure_plan(driver: DesktopDriver, generation: int) -> CameraDragPlan:
    driver.backend.pointer_position = lambda **_kwargs: (100, 100, 0x101)  # type: ignore[method-assign]
    binding = BindingMetadata("identity", "capture", 1, generation, 4, (4,), boot="boot")
    return CameraDragPlan(
        PointerButton.RIGHT,
        (PointerSample(0.0, Point(100, 100)), PointerSample(0.05, Point(130, 100))),
        0.05,
        Point(30, 0),
        binding,
    )


def _arm_camera_failure_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DesktopDriver, CameraDragPlan, int]:
    driver = _driver(tmp_path, monkeypatch)
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    return driver, _camera_failure_plan(driver, generation), generation


def test_camera_drag_broker_mint_failure_revokes_guardian_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, plan, generation = _arm_camera_failure_driver(tmp_path, monkeypatch)
    monkeypatch.setattr(
        driver.input_broker,
        "mint_admitted_gesture",
        lambda **_kwargs: (_ for _ in ()).throw(InputBrokerError("mint failed")),
    )
    try:
        with pytest.raises(DriverError, match="mint failed"):
            driver.run_camera_drag(plan, generation=generation, action_generation=generation + 1)
        assert driver.guardian.action is None
        assert not driver.guardian.armed
    finally:
        driver.close()


def test_camera_drag_broker_precommit_failure_revokes_without_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, plan, generation = _arm_camera_failure_driver(tmp_path, monkeypatch)
    emitted: list[list[str]] = []

    class FailingPrecommitPermit:
        def __init__(self) -> None:
            self.is_open = True
            self.acquired_monotonic = 0.0
            self.closed = False

        def precommit(self, *, deadline_monotonic: float | None = None) -> int:
            del deadline_monotonic
            raise PointerExecutionError("precommit failed before physical drag")

        def record_press(self) -> int:
            raise AssertionError("record_press must not run after precommit failure")

        def close(self) -> None:
            self.closed = True
            self.is_open = False

    permit = FailingPrecommitPermit()
    reservation = RateReservation(permit)
    monkeypatch.setattr(
        driver.input_broker,
        "reserve_button_rate",
        lambda **_kwargs: reservation,
    )
    driver.backend._run = (  # type: ignore[method-assign]
        lambda args, **_kwargs: emitted.append(list(args))
    )
    try:
        with pytest.raises(DriverError, match="precommit failed"):
            driver.run_camera_drag(plan, generation=generation, action_generation=generation + 1)
        assert emitted == []
        assert permit.closed
        assert not reservation.is_open
        assert driver.guardian.action is None
        assert not driver.guardian.armed
    finally:
        driver.close()


def test_camera_drag_live_action_authority_change_blocks_native_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, plan, generation = _arm_camera_failure_driver(tmp_path, monkeypatch)
    emitted: list[list[str]] = []
    live_action_generation = [generation]

    class Permit:
        def __init__(self) -> None:
            self.is_open = True
            self.acquired_monotonic = 0.0
            self.closed = False

        def precommit(self, *, deadline_monotonic: float | None = None) -> int:
            del deadline_monotonic
            return 1

        def record_press(self) -> int:
            return 2

        def close(self) -> None:
            self.closed = True
            self.is_open = False

    permit = Permit()

    def acquire(**kwargs: object) -> Permit:
        cancellation_check = kwargs.get("cancellation_check")
        assert callable(cancellation_check)
        cancellation_check()
        live_action_generation[0] = generation + 1
        return permit

    broker = InputBroker.for_tests(clock=time.monotonic, permit_factory=acquire)
    driver.input_broker = broker
    assert isinstance(driver.backend, X11Backend)
    driver.backend.input_broker = broker
    driver.backend._run = (  # type: ignore[method-assign]
        lambda args, **_kwargs: emitted.append(list(args))
    )

    def require_live_action() -> None:
        if live_action_generation[0] != generation:
            raise DriverError("action generation changed while camera drag was waiting")

    try:
        with pytest.raises(DriverError, match="action generation changed"):
            driver.run_camera_drag(
                plan,
                generation=generation,
                action_generation=generation,
                live_admission_check=require_live_action,
            )
        assert emitted == []
        assert permit.closed
        assert driver.guardian.action is None
        assert not driver.guardian.armed
    finally:
        driver.close()


def test_camera_drag_broker_record_failure_revokes_after_physical_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, plan, generation = _arm_camera_failure_driver(tmp_path, monkeypatch)
    emitted: list[list[str]] = []

    class FailingRecordPermit:
        is_open = True
        acquired_monotonic = 0.0

        def precommit(self, *, deadline_monotonic: float | None = None) -> int:
            del deadline_monotonic
            return 1

        def record_press(self) -> int:
            raise PointerExecutionError("record failed after physical drag")

        def close(self) -> None:
            self.is_open = False

    monkeypatch.setattr(
        driver.input_broker,
        "reserve_button_rate",
        lambda **_kwargs: RateReservation(FailingRecordPermit()),
    )

    def emit_and_fail_record(args, **kwargs):  # type: ignore[no-untyped-def]
        driver.input_broker.redeem_button_emission_claim(
            kwargs["button_emission_claim"],
            emitted_chain=tuple(args),
            button=PointerButton.RIGHT,
            action_generation=kwargs["emission_action_generation"],
            lease_generation=kwargs["emission_lease_generation"],
        )
        emitted.append(list(args))

    driver.backend._run = emit_and_fail_record  # type: ignore[method-assign]
    try:
        with pytest.raises(DriverError, match="record failed"):
            driver.run_camera_drag(plan, generation=generation, action_generation=generation + 1)
        assert emitted and emitted[0][1:3] == ["mousedown", "3"]
        assert driver.guardian.action is None
        assert not driver.guardian.armed
    finally:
        driver.close()


def test_camera_drag_admission_cancellation_revokes_open_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, plan, generation = _arm_camera_failure_driver(tmp_path, monkeypatch)

    def cancel_while_waiting(**kwargs: object) -> RateReservation:
        driver.revoke("test camera admission cancellation")
        callback = kwargs.get("cancellation_check")
        assert callable(callback)
        callback()
        raise AssertionError("cancellation callback did not stop admission")

    monkeypatch.setattr(driver.input_broker, "reserve_button_rate", cancel_while_waiting)
    try:
        with pytest.raises(DriverError, match="cancelled|revoked"):
            driver.run_camera_drag(plan, generation=generation, action_generation=generation + 1)
        assert driver.guardian.action is None
    finally:
        driver.close()


@pytest.mark.parametrize(
    ("first", "second"),
    [("click", "camera"), ("camera", "click")],
)
def test_production_click_and_camera_drag_entrypoints_share_the_rate_gate(
    first: str, second: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native click and camera paths consume one shared button-down gate."""

    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    identity = _identity()
    clock = [0.0]
    next_allowed = [0.0]
    emitted: list[tuple[str, list[str]]] = []

    class Permit:
        def __init__(self, acquired_monotonic: float) -> None:
            self.is_open = True
            self.acquired_monotonic = acquired_monotonic

        def precommit(self, *, deadline_monotonic: float | None = None) -> int:
            del deadline_monotonic
            return 1

        def record_press(self) -> int:
            next_allowed[0] = clock[0] + 1.0
            return 2

        def close(self) -> None:
            self.is_open = False

    def acquire(**_kwargs: object) -> Permit:
        if clock[0] < next_allowed[0]:
            raise PointerExecutionError("click spacing wait cannot fit before its deadline")
        return Permit(clock[0])

    broker = InputBroker.for_tests(clock=lambda: clock[0], permit_factory=acquire)
    driver.input_broker = broker
    backend.input_broker = broker
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    backend._focus = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    pointer = [20, 20, identity.child_xid]

    def run_native(args, *, input_schema=None, **kwargs):  # type: ignore[no-untyped-def]
        emitted.append((str(input_schema), list(args)))
        if input_schema == "pointer-motion":
            for index, value in enumerate(args[:-2]):
                if value == "mousemove":
                    pointer[0] = int(args[index + 1])
                    pointer[1] = int(args[index + 2])
        elif input_schema == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
        elif input_schema == "camera-drag":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.RIGHT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )

    backend.pointer_position = lambda **_kwargs: tuple(pointer)  # type: ignore[method-assign]
    backend._run = run_native  # type: ignore[method-assign]

    anchor = MeasuredAnchor(
        action="logout",
        x=100,
        y=100,
        identity_fingerprint=identity.fingerprint,
        target_xid=identity.child_xid,
        capture_id="click-capture-one",
        capture_sequence=1,
        source="native-visual",
        observed_monotonic=time.monotonic(),
        capture_monotonic=time.monotonic(),
        facts=(
            ("capture", "click-capture-one"),
            ("control", "world"),
            ("target", str(identity.child_xid)),
        ),
        generation=generation,
        bounds=(80, 80, 60, 40),
        target_ancestry=(identity.child_xid,),
    ).with_hash()
    backend._measured_anchors["logout"] = anchor
    backend._consumed_anchor_seals.add(anchor.evidence_hash)
    binding = BindingMetadata(
        identity.fingerprint,
        anchor.capture_id,
        anchor.capture_sequence,
        generation,
        anchor.target_xid,
        anchor.target_ancestry,
        source=anchor.source,
    )
    click_gesture = pointer_execution.plan_human_pointer(
        Point(pointer[0], pointer[1]),
        PointerTarget("world", Rect(*anchor.bounds), binding, padding=1),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=0x1234,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )

    def refresh_anchor(_identity: WindowIdentity, _previous: MeasuredAnchor) -> MeasuredAnchor:
        return replace(
            anchor,
            capture_id="click-capture-two",
            capture_sequence=2,
            observed_monotonic=time.monotonic(),
            capture_monotonic=time.monotonic(),
            facts=(
                ("capture", "click-capture-two"),
                ("control", "world"),
                ("target", str(identity.child_xid)),
            ),
        ).with_hash()

    evidence = SimpleNamespace(xtest_profile=None, synthetic_echo_proof=())
    driver._motion_input_context = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        identity,
        evidence,
        4,
        2,
        (0, 0, 0),
        {},
    )
    driver._verify_current_xtest_profile = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    class CompleteAction:
        complete = True

    monkeypatch.setattr(
        driver.guardian,
        "begin_action",
        lambda *_args, **_kwargs: CompleteAction(),
    )
    monkeypatch.setattr(driver.guardian, "dispatch_admission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(driver.guardian, "check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(driver.guardian, "finish_action", lambda *_args, **_kwargs: None)

    class CompleteMonitor:
        def wait_for_event(self, predicate, *, timeout: float) -> None:  # type: ignore[no-untyped-def]
            del timeout
            assert predicate(())

        def stop(self) -> None:
            return None

    driver._monitor = CompleteMonitor()  # type: ignore[assignment]
    camera_plan = CameraDragPlan(
        PointerButton.RIGHT,
        (PointerSample(0.0, Point(100, 100)), PointerSample(0.05, Point(130, 100))),
        0.05,
        Point(30, 0),
        BindingMetadata("identity", "camera-capture", 1, generation, 4, (4,), boot="boot"),
    )

    def run_click() -> None:
        reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
        try:
            backend.action(
                "logout",
                generation=generation,
                _admitted_anchor=anchor,
                _pointer_plan=click_gesture.trajectory,
                _click_plan=click_gesture.click,
                _gesture_plan=click_gesture,
                _rate_reservation=reservation,
                _refresh_anchor=refresh_anchor,
                _guardian_admission_id="production-click",
                _action_generation=generation + 1,
            )
        finally:
            broker.release_reservation(reservation)

    try:
        if first == "click":
            run_click()
            clock[0] = 0.5
            with pytest.raises(DriverError, match="click spacing"):
                driver.run_camera_drag(camera_plan, generation=generation, action_generation=generation + 2)
            assert any(schema == "single-click" for schema, _args in emitted)
        else:
            driver.run_camera_drag(camera_plan, generation=generation, action_generation=generation + 2)
            clock[0] = 0.5
            with pytest.raises(InputBrokerError, match="click spacing"):
                run_click()
            assert any(schema == "camera-drag" for schema, _args in emitted)
    finally:
        driver.close()


def test_guardian_allows_zero_motion_canonical_click_with_button_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    monitor = _EventMonitor(
        [
            _event(EventClass.RAW_BUTTON_PRESS, 1, detail=3),
            _event(EventClass.BUTTON_PRESS, 2, detail=3, root=(100, 100), event_xy=(100, 100)),
            _event(EventClass.RAW_BUTTON_RELEASE, 3, detail=3),
            _event(EventClass.BUTTON_RELEASE, 4, detail=3, root=(100, 100), event_xy=(100, 100)),
        ]
    )
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    try:
        action = driver.guardian.begin_action(
            generation,
            target_xid=0x101,
            source_device=4,
            master_device=2,
            event_classes={
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
            },
            device_by_class={
                EventClass.RAW_BUTTON_PRESS.value: 2,
                EventClass.BUTTON_PRESS.value: 4,
                EventClass.RAW_BUTTON_RELEASE.value: 2,
                EventClass.BUTTON_RELEASE.value: 4,
            },
            required_sequence=(
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
            ),
            expected_modifiers=(0, 0, 0),
            expected_details=(3, 3, 3, 3),
            expected_press_coordinates=(100.0, 100.0),
            expected_press_event_coordinates=(100.0, 100.0),
            expected_release_coordinates=(100.0, 100.0),
            expected_release_event_coordinates=(100.0, 100.0),
            initial_origin=(100, 100, 100, 100),
            expected_root_xid=1,
            expected_event_xid=1,
            expected_child_xids=frozenset({0x101}),
            canonical_motion=True,
            require_motion_before_button=False,
            require_motion_before_release=False,
            held_buttons={3},
        )
        driver.guardian.dispatch_admission(generation, lambda: None)
        assert action is driver.guardian.action
        for event in monitor.events:
            assert driver.guardian.observe(event)
        assert action.complete
        driver.guardian.finish_action(generation, action)
    finally:
        driver.close()


@pytest.mark.parametrize("pointer_matches", [True, False], ids=["measured-origin", "stale-origin"])
def test_run_hover_click_canonical_origin_uses_fresh_pointer_measurement(
    pointer_matches: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    identity = _identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    binding = BindingMetadata(
        identity.fingerprint,
        "mail-gesture",
        1,
        generation,
        identity.child_xid,
        (identity.child_xid,),
        source="native-visual",
    )
    gesture = pointer_execution.plan_human_pointer(
        Point(20, 20),
        PointerTarget("mailbox", Rect(80, 60, 100, 30), binding, padding=0),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=0xCAFE,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )
    endpoint = gesture.endpoint.to_pixel()
    actual = (int(endpoint.x), int(endpoint.y))
    observed = actual if pointer_matches else (actual[0] + 1, actual[1])
    backend.pointer_position = lambda **_kwargs: (*observed, identity.child_xid)  # type: ignore[method-assign]
    commands: list[list[str]] = []

    def run_hover_command(args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("input_schema") == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run_hover_command  # type: ignore[method-assign]
    permit = pointer_execution.acquire_click_rate_permit()
    hover = SimpleNamespace(
        identity_fingerprint=identity.fingerprint,
        capture_monotonic=time.monotonic(),
        hover_kind="unit",
        hover_name="Mailbox",
        chat_focused=False,
    )
    captured: dict[str, object] = {}
    original_begin_action = driver.guardian.begin_action

    def capture_begin_action(action_generation: int, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return original_begin_action(action_generation, **kwargs)

    monkeypatch.setattr(driver.guardian, "begin_action", capture_begin_action)
    monitor = _EventMonitor(
        [
            _event(EventClass.RAW_BUTTON_PRESS, 1, detail=1),
            _event(EventClass.BUTTON_PRESS, 2, detail=1, root=actual, event_xy=actual),
            _event(EventClass.RAW_BUTTON_RELEASE, 3, detail=1),
            _event(EventClass.BUTTON_RELEASE, 4, detail=1, root=actual, event_xy=actual),
        ]
    )
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    try:
        if pointer_matches:
            driver.run_hover_click(
                endpoint,
                hover,
                gesture=gesture,
                gesture_ended_monotonic=permit.acquired_monotonic - 0.1,
                generation=generation,
                click_rate_permit=permit,
                target_name="Mailbox",
            )
            assert captured["canonical_motion"] is True
            assert captured["initial_origin"] == (*actual, *actual)
            assert commands and commands[-1][1:3] == ["mousedown", "1"]
        else:
            with pytest.raises(DriverError, match="native pointer moved"):
                driver.run_hover_click(
                    endpoint,
                    hover,
                    gesture=gesture,
                    gesture_ended_monotonic=permit.acquired_monotonic - 0.1,
                    generation=generation,
                    click_rate_permit=permit,
                    target_name="Mailbox",
                )
            assert captured == {}
            assert commands == []
    finally:
        permit.close()
        driver.close()


@pytest.mark.parametrize(
    "variant",
    [
        "early-release",
        "motion-after-release",
        "wrong-coordinate",
        "motion-before-press",
        "wrong-target",
        "missing-motion",
        "late-motion",
        "wrong-release-root",
        "wrong-release-event-coordinate",
        "wrong-release-target",
    ],
)
def test_run_camera_drag_rejects_invalid_native_phase_trace(
    variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    driver.backend.pointer_position = lambda **_kwargs: (20, 20, 0x101)  # type: ignore[method-assign]
    binding = BindingMetadata("identity", "capture", 1, 1, 4, (4,), boot="boot")
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (
            PointerSample(0.0, Point(100, 100)),
            PointerSample(0.025, Point(115, 100)),
            PointerSample(0.05, Point(130, 100)),
        ),
        0.05,
        Point(30, 0),
        binding,
    )
    positioning = plan_pointer_curve(
        Point(20, 20),
        PointerTarget("camera-viewport", Rect(100, 100, 1, 1), binding, padding=0),
        Rect(0, 0, 400, 300),
        profile=MotionProfile(role=DemonstrationRole.POINTER),
    )
    events: list[SimpleNamespace] = []
    timestamp = 1
    prior = positioning.samples[0].point
    for sample in positioning.samples[1:]:
        if sample.point != prior:
            events.extend(
                _motion_pair(
                    timestamp,
                    (int(prior.x), int(prior.y)),
                    (int(sample.point.x), int(sample.point.y)),
                )
            )
            timestamp += 1
            prior = sample.point
    if variant == "motion-before-press":
        events.extend(_motion_pair(timestamp, (int(prior.x), int(prior.y)), (60, 60)))
        timestamp += 1
    press_root = (101, 100) if variant == "wrong-coordinate" else (100, 100)
    press_target = 0x999 if variant == "wrong-target" else 0x101
    events.extend(
        [
            _event(EventClass.RAW_BUTTON_PRESS, timestamp, detail=3),
            _event(
                EventClass.BUTTON_PRESS,
                timestamp + 1,
                detail=3,
                root=press_root,
                event_xy=press_root,
                target=press_target,
            ),
        ]
    )
    if variant not in {
        "early-release",
        "wrong-coordinate",
        "wrong-target",
        "missing-motion",
    }:
        motion_timestamp = timestamp - 1 if variant == "late-motion" else timestamp + 2
        events.extend(_motion_pair(motion_timestamp, (100, 100), (130, 100)))
    if variant == "motion-after-release":
        events.extend(
            [
                _event(EventClass.RAW_BUTTON_RELEASE, timestamp + 3, detail=3),
                *_motion_pair(timestamp + 4, (100, 100), (130, 100)),
                _event(
                    EventClass.BUTTON_RELEASE,
                    timestamp + 5,
                    detail=3,
                    root=(130, 100),
                    event_xy=(130, 100),
                ),
            ]
        )
    else:
        release_at = timestamp + (2 if variant == "early-release" else 3)
        release_root = (129, 100) if variant == "wrong-release-root" else (130, 100)
        release_event = (129, 100) if variant == "wrong-release-event-coordinate" else (130, 100)
        release_target = 0x999 if variant == "wrong-release-target" else 0x101
        events.extend(
            [
                _event(EventClass.RAW_BUTTON_RELEASE, release_at, detail=3),
                _event(
                    EventClass.BUTTON_RELEASE,
                    release_at + 1,
                    detail=3,
                    root=release_root,
                    event_xy=release_event,
                    target=release_target,
                ),
            ]
        )

    class NonCompletingMonitor(_EventMonitor):
        def wait_for_event(self, predicate, *, timeout: float) -> None:
            del predicate, timeout
            for event in self.events:
                self.callback(event)

    monitor = NonCompletingMonitor(events)
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    plan = replace(plan, binding=replace(binding, generation=generation))
    try:
        with pytest.raises(
            DriverError,
            match="did not complete|unattributed|cancelled|lease is revoked",
        ):
            driver.run_camera_drag(plan, generation=generation)
        if variant in {"wrong-release-root", "wrong-release-event-coordinate", "wrong-release-target"}:
            assert "unattributed XI2 event class=ButtonRelease" in driver.guardian.revoked_reason
    finally:
        driver.close()


def test_run_camera_drag_cancels_after_admitted_held_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must cover a real admitted process and published button hold."""

    driver = _driver(tmp_path, monkeypatch)
    script = tmp_path / "harmless-camera-boundary"
    script.write_text("#!/bin/sh\nexec /bin/sleep 30\n", encoding="utf-8")
    script.chmod(0o700)
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: str(script))
    release_calls: list[list[str]] = []
    driver.backend._release_command = lambda args: release_calls.append(args) or True  # type: ignore[method-assign]
    driver.backend.pointer_position = lambda **_kwargs: (100, 100, 0x101)  # type: ignore[method-assign]
    binding = BindingMetadata("identity", "capture", 1, 1, 4, (4,), boot="boot")
    plan = CameraDragPlan(
        PointerButton.RIGHT,
        (
            PointerSample(0.0, Point(100, 100)),
            PointerSample(0.025, Point(115, 100)),
            PointerSample(0.05, Point(130, 100)),
        ),
        0.05,
        Point(30, 0),
        binding,
    )
    monitor = _EventMonitor([])
    monitor.set_callback(driver.guardian.observe)
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    plan = replace(plan, binding=replace(binding, generation=generation))
    held = threading.Event()
    waiter_errors: list[str] = []
    observed_groups: set[int] = set()

    def revoke_after_admission() -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if (
                driver.guardian.held
                and driver.backend._held_buttons == {3}
                and driver.guardian.active_process_groups
            ):
                observed_groups.update(driver.guardian.active_process_groups)
                held.set()
                driver.revoke("test cancellation during admitted camera hold")
                return
            time.sleep(0.005)
        waiter_errors.append("camera hold was not admitted")

    waiter = threading.Thread(target=revoke_after_admission, daemon=True)
    waiter.start()
    try:
        with pytest.raises(DriverError, match="cancelled|revoked|generation|desktop command failed"):
            driver.run_camera_drag(plan, generation=generation)
        waiter.join(timeout=3.0)
        assert not waiter.is_alive()
        assert not waiter_errors
        assert held.is_set()
        assert observed_groups
        assert not driver.guardian.active_process_groups
        assert not driver.backend.active_process_groups
        assert not driver.guardian.held
        assert not driver.backend._held_buttons
        assert all(not Path(f"/proc/{pid}").exists() for pid in observed_groups)
        assert release_calls == [["xdotool", "mouseup", "3"]]
    finally:
        driver.close()


@pytest.mark.parametrize(
    ("health_mode", "withdraw_failure"),
    [
        ("deadline", False),
        ("health-failure", False),
        ("deadline", True),
        ("health-failure", True),
    ],
)
def test_native_runner_pre_go_hold_publication_never_releases_late_input(
    health_mode: str,
    withdraw_failure: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-GO health/withdrawal failure cannot release native input."""

    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    action = driver.guardian.begin_action(
        generation,
        target_xid=0x101,
        source_device=4,
        master_device=2,
        event_classes={EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value},
        required_sequence=(EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value),
        held_keys={"025"},
    )
    del action
    reached_health = threading.Event()
    release_calls: list[str] = []
    hold_publications: list[tuple[set[str], set[int]]] = []
    gate_releases: list[str] = []
    original_release = AdmissionGate.release
    monkeypatch.setattr(
        AdmissionGate,
        "release",
        lambda gate: (gate_releases.append("release"), original_release(gate))[1],
    )

    def health_barrier() -> None:
        assert backend._held_keys == {"025"}
        reached_health.set()
        if health_mode == "health-failure":
            raise RuntimeError("input watchdog is unhealthy")
        while time.monotonic() < deadline:
            time.sleep(0.005)

    backend.set_admission_health(health_barrier)
    backend._release_command = lambda args: release_calls.append(" ".join(args)) or True  # type: ignore[method-assign]

    def publish_holds(_authority, keys, buttons) -> None:  # type: ignore[no-untyped-def]
        if withdraw_failure and not keys and not buttons:
            raise RuntimeError("watchdog withdrawal failed")
        hold_publications.append((set(keys), set(buttons)))

    backend.set_hold_publisher(publish_holds)
    deadline = time.monotonic() + 0.25
    argv = ["xdotool", "keydown", "025", "keyup", "025"]
    try:
        expected_message = (
            "holds could not be withdrawn"
            if withdraw_failure
            else "watchdog is unhealthy"
            if health_mode == "health-failure"
            else "deadline"
        )
        with pytest.raises(RuntimeError, match=expected_message):
            backend._run(
                argv,
                generation=generation,
                held_keys={"025"},
                input_schema="keyboard-steps",
                expected_argv=tuple(argv),
                deadline_monotonic=deadline,
            )
        assert reached_health.is_set()
        assert gate_releases == []
        assert hold_publications[0] == ({"025"}, set())
        if withdraw_failure:
            assert hold_publications == [({"025"}, set())]
        else:
            assert hold_publications[-1] == (set(), set())
        assert not backend._active_processes
        assert not backend._active_process_groups
        assert not backend._held_keys
        assert not driver.guardian.held
        assert release_calls == []
    finally:
        driver.revoke("test cleanup")
        driver.close()


def test_native_runner_withdraws_before_unconfirmed_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-GO health failure clears holds before failed withdrawal/reaping."""

    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    action = driver.guardian.begin_action(
        generation,
        target_xid=0x101,
        source_device=4,
        master_device=2,
        event_classes={EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value},
        required_sequence=(EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value),
        held_keys={"025"},
    )
    del action
    reaper_release = threading.Event()
    reaper_started = threading.Event()
    hold_publications: list[tuple[set[str], set[int]]] = []
    withdrawal_attempts: list[tuple[set[str], set[int]]] = []
    release_calls: list[str] = []
    termination_observed_holds: list[set[str]] = []
    original_terminate = X11Backend._terminate_process

    def unconfirmed_terminate(process: subprocess.Popen[str]) -> bool:
        reaper_started.set()
        termination_observed_holds.append(set(backend._held_keys))

        def terminate_after_observation() -> None:
            reaper_release.wait(timeout=2.0)
            original_terminate(process)

        threading.Thread(target=terminate_after_observation, daemon=True).start()
        return False

    monkeypatch.setattr(backend, "_terminate_process", unconfirmed_terminate)
    backend._release_command = lambda args: release_calls.append(" ".join(args)) or True  # type: ignore[method-assign]

    def health_failure() -> None:
        assert backend._held_keys == {"025"}
        raise RuntimeError("input watchdog is unhealthy")

    backend.set_admission_health(health_failure)

    def publish_holds(_authority, keys, buttons) -> None:  # type: ignore[no-untyped-def]
        if not keys and not buttons:
            withdrawal_attempts.append((set(keys), set(buttons)))
            raise RuntimeError("watchdog withdrawal failed")
        hold_publications.append((set(keys), set(buttons)))

    backend.set_hold_publisher(publish_holds)
    argv = ["xdotool", "keydown", "025", "keyup", "025"]
    observed_groups: set[int] = set()
    try:
        with pytest.raises(RuntimeError, match="holds could not be withdrawn"):
            backend._run(
                argv,
                generation=generation,
                held_keys={"025"},
                input_schema="keyboard-steps",
                expected_argv=tuple(argv),
                deadline_monotonic=time.monotonic() + 0.5,
            )
        assert reaper_started.is_set()
        assert termination_observed_holds == [set()]
        observed_groups.update(backend.active_process_groups)
        assert observed_groups
        assert hold_publications == [({"025"}, set())]
        assert withdrawal_attempts == [(set(), set()), (set(), set())]
        assert not backend._held_keys
        assert not backend._held_buttons
        assert not driver.guardian.held
        assert release_calls == []
        assert backend.active_process_groups
    finally:
        reaper_release.set()
        deadline = time.monotonic() + 2.0
        while backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not backend.active_process_groups
        assert all(not Path(f"/proc/{pid}").exists() for pid in observed_groups)
        driver.revoke("test cleanup")
        driver.close()


def test_native_resolver_accepts_captured_direct_root_wine_client_certificate(
    tmp_path: Path,
) -> None:
    """The production resolver must emit the exact captured native target set."""

    outer = 0x700100
    client = 0x700101
    pid = 5717
    profile = DriverProfile(
        display=":captured",
        xauthority=str(tmp_path / "Xauthority"),
        wine_prefix=str(tmp_path / "wine"),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    tree = (
        "xwininfo: Window id: 0x374 (the root window)\n"
        f'  {hex(outer)} "World of Warcraft": ("wowb.exe" "wowb.exe") '
        "1928x1092+0+0  +0+0\n"
        f'    {hex(client)} "World of Warcraft": ("wowb.exe" "wowb.exe") '
        "1920x1080+0+0  +0+0\n"
    )
    outer_tree = (
        f'xwininfo: Window id: {hex(outer)} "World of Warcraft"\n'
        f'  {hex(client)} "World of Warcraft": ("wowb.exe" "wowb.exe") '
        "1920x1080+0+0  +0+0\n"
    )

    def command(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args == ["xdotool", "search", "--name", "World of Warcraft"]:
            return subprocess.CompletedProcess(args, 0, f"{hex(outer)}\n", "")
        if args == ["xdotool", "getwindowgeometry", "--shell", hex(outer)]:
            return subprocess.CompletedProcess(
                args,
                0,
                "WINDOW=7340288\nX=0\nY=0\nWIDTH=1928\nHEIGHT=1092\nSCREEN=0\n",
                "",
            )
        if args == ["xprop", "-id", hex(outer), "WM_NAME", "_NET_WM_PID", "WM_CLASS"]:
            return subprocess.CompletedProcess(
                args,
                0,
                f'WM_NAME(STRING) = "World of Warcraft"\n_NET_WM_PID(CARDINAL) = {pid}\n'
                'WM_CLASS(STRING) = "wowb.exe", "wowb.exe"\n',
                "",
            )
        if args == ["xwininfo", "-id", hex(outer), "-tree"]:
            return subprocess.CompletedProcess(args, 0, outer_tree, "")
        if args == ["xwininfo", "-root", "-tree"]:
            return subprocess.CompletedProcess(args, 0, tree, "")
        if args == ["xdotool", "getwindowfocus"]:
            return subprocess.CompletedProcess(args, 0, f"{hex(client)}\n", "")
        if args == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
            return subprocess.CompletedProcess(
                args, 0, f"_NET_ACTIVE_WINDOW(WINDOW): window id # {hex(client)}\n", ""
            )
        raise AssertionError(args)

    backend._run = command  # type: ignore[method-assign]
    backend._matching_game_pids = lambda: {pid}  # type: ignore[method-assign]
    backend._process_executable = lambda _pid: ("WowB.exe", "wine-start")  # type: ignore[method-assign]
    identity = backend.resolve_identity()
    assert identity.root_xid == 0x374
    assert identity.managed_frame_xid == outer
    assert identity.target_event_xids == frozenset({outer, client})
    assert identity.xid == outer
    assert identity.child_xid == client


def test_production_x11_factory_requires_pinned_observer_and_never_falls_back(
    tmp_path: Path,
) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    profile = DriverProfile(
        display=":factory",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
    )
    with pytest.raises(DriverError, match="pinned Rust identity observer is required"):
        X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "missing-pin-tool"))

    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nIFS= read -r _request\nexit 17\n", encoding="utf-8")
    observer.chmod(0o700)
    pinned = replace(
        profile,
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(pinned, input_tool=fixture_input_tool(tmp_path / "pinned-tool"))
    try:
        assert backend.resolver_kind == "pinned-rust-v1"
        assert os.readlink(f"/proc/{os.getpid()}/fd/{backend._identity_observer._fd}").startswith(
            "/memfd:crowley-x11-identity-observer"
        )
        backend._matching_game_pids = lambda: (_ for _ in ()).throw(AssertionError("reference fallback"))  # type: ignore[method-assign]
        with pytest.raises(DriverError, match="identity observer failed"):
            backend.resolve_identity()
        backend.close()
        with pytest.raises(DriverError, match="production X11 backend is closed"):
            backend.resolve_identity()
    finally:
        backend.close()


def test_default_desktop_driver_constructs_the_pinned_production_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    observer.chmod(0o700)
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    profile = DriverProfile(
        display=":0",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    tool = fixture_input_tool(tmp_path / "default-tool")
    monkeypatch.setattr(PinnedInputTool, "load", lambda: tool)
    driver = DesktopDriver(profile=profile, lease_path=tmp_path / "desktop.lock")
    try:
        assert isinstance(driver.backend, X11Backend)
        assert driver.backend.resolver_kind == "pinned-rust-v1"
        assert driver.backend._identity_observer is not None
    finally:
        driver.close()


def test_session_daemon_default_constructs_the_pinned_production_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's shipped default path must retain the pinned observer."""

    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    observer.chmod(0o700)
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    profile = DriverProfile(
        display=":daemon-default",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    plan_json = "{}"
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.162.1\\n'\n", encoding="utf-8")
    codex.chmod(0o700)
    config = SessionConfig(
        profile=profile,
        journal=tmp_path / "journal.json",
        socket=tmp_path / "session.sock",
        capture_dir=tmp_path / "captures",
        plan_hash=hashlib.sha256(plan_json.encode()).hexdigest(),
        plan_json=plan_json,
        codex_path=str(codex),
        model_neutral_dir=tmp_path / "model-neutral",
    )
    monkeypatch.setattr(PinnedInputTool, "load", lambda: fixture_input_tool(tmp_path / "daemon-tool"))
    monkeypatch.setenv("CROWLEY_DESKTOP_LEASE", str(tmp_path / "desktop.lock"))
    daemon = SessionDaemon(config)
    try:
        assert isinstance(daemon.driver.backend, X11Backend)
        assert daemon.driver.backend.resolver_kind == "pinned-rust-v1"
        assert daemon.driver.backend._identity_observer is not None
        assert daemon.driver.backend._production_observer_required is True
    finally:
        daemon.driver.close()


def test_pinned_observer_write_failure_reaps_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request write failure still closes pipes and reaps the spawned group."""

    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    pid_path = tmp_path / "write-failure.pid"
    observer = tmp_path / "write-failure-observer"
    observer.write_text(
        f"#!/bin/sh\necho $$ > {pid_path}\nsleep 30\n",
        encoding="utf-8",
    )
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":write-failure",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))

    def fail_write(process, request, *, deadline, cancel_event, close_event=None):  # type: ignore[no-untyped-def]
        del request, deadline, cancel_event, close_event
        deadline = time.monotonic() + 1
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        raise BrokenPipeError("test request pipe closed")

    monkeypatch.setattr(PinnedX11IdentityObserver, "_write_request", staticmethod(fail_write))
    try:
        with pytest.raises(DriverError, match="invocation failed|pipe"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 2)
        assert pid_path.is_file()
        pid = int(pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not backend._identity_observer._active_processes
    finally:
        backend.close()


def test_pinned_observer_close_cancels_invoke_and_releases_sealed_fd(tmp_path: Path) -> None:
    """Closing the production backend while invoking leaves no observer FD/process."""

    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    pid_path = tmp_path / "close-race.pid"
    observer = tmp_path / "close-race-observer"
    observer.write_text(f"#!/bin/sh\necho $$ > {pid_path}\nsleep 30\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":close-race",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    baseline_fd_identities = _fd_identities()
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    sealed_observer = backend._identity_observer
    assert sealed_observer is not None
    owned_fd_identities = {
        _fd_identity(sealed_observer._fd),
        _fd_identity(backend.input_tool.image.fd),
    }
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 5.0)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        deadline = time.monotonic() + 2.0
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pid_path.is_file()
        backend.close()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DriverError)
        assert "cancel" in str(errors[0]).lower() or "closed" in str(errors[0]).lower()
        assert not sealed_observer._active_processes
        with pytest.raises(DriverError, match="closed"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 1.0)
    finally:
        backend.close()
        backend.input_tool.close()
    assert sealed_observer._fd == -1
    assert backend.input_tool.image.fd == -1
    remaining_fd_identities = _fd_identities()
    assert not owned_fd_identities & remaining_fd_identities
    assert remaining_fd_identities <= baseline_fd_identities


def test_pinned_resolver_captures_observer_before_close_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close between resolver selection and invocation remains a typed failure."""

    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    observer = tmp_path / "close-before-invoke-observer"
    observer.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":close-before-invoke",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    selected = threading.Event()
    release = threading.Event()
    original = X11Backend._resolve_identity_pinned

    def gated(self, pinned_observer, **kwargs):  # type: ignore[no-untyped-def]
        selected.set()
        assert release.wait(timeout=2)
        return original(self, pinned_observer, **kwargs)

    monkeypatch.setattr(X11Backend, "_resolve_identity_pinned", gated)
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        assert selected.wait(timeout=2)
        backend.close()
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DriverError)
        assert "closed" in str(errors[0]).lower()
    finally:
        release.set()
        worker.join(timeout=2)
        backend.close()


def test_failed_guardian_construction_closes_pinned_observer_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Guardian construction failure must not strand the sealed observer FD."""

    from crowley.session import driver as session_driver

    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    observer = tmp_path / "guardian-failure-observer"
    observer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":guardian-failure",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    sealed_observer = backend._identity_observer
    assert sealed_observer is not None
    sealed_fd = sealed_observer._fd

    def fail_guardian(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("fixture Guardian construction failure")

    monkeypatch.setattr(session_driver, "InputGuardian", fail_guardian)
    try:
        with pytest.raises(RuntimeError, match="Guardian construction failure"):
            DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "lease.lock")
        with pytest.raises(OSError):
            os.fstat(sealed_fd)
        assert sealed_observer._fd == -1
        assert not sealed_observer._active_processes
    finally:
        backend.close()
        backend.input_tool.close()


def test_failed_input_lease_closes_second_pinned_observer_fd(tmp_path: Path) -> None:
    """A real lease collision must not retain the second production observer."""

    from crowley.session.guardian import GuardianError

    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    observer = tmp_path / "lease-failure-observer"
    observer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":lease-failure",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    lease_path = tmp_path / "desktop.lock"
    first_backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "first-tool"))
    first_driver = DesktopDriver(profile=profile, backend=first_backend, lease_path=lease_path)
    first_driver.guardian.lease.acquire()
    second_backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "second-tool"))
    second_observer = second_backend._identity_observer
    assert second_observer is not None
    try:
        with pytest.raises(GuardianError, match="input lease"):
            DesktopDriver(profile=profile, backend=second_backend, lease_path=lease_path)
        assert second_observer._fd == -1
        assert not second_observer._active_processes
    finally:
        first_driver.close()
        second_backend.close()
        second_backend.input_tool.close()


def test_pinned_observer_protocol_requires_version_and_timings(tmp_path: Path) -> None:
    observer_path = tmp_path / "observer"
    observer_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observer_path.chmod(0o700)
    observer = PinnedX11IdentityObserver(
        observer_path, hashlib.sha256(observer_path.read_bytes()).hexdigest()
    )
    process = SimpleNamespace(returncode=0)
    base = {
        "protocolVersion": 1,
        "matchingProcessCount": 1,
        "candidateWindowCount": 1,
        "pid": 10,
        "start": "start",
        "exe": "WowB.exe",
        "statusName": "WowB.exe",
        "loader": "wine-preloader",
        "winePrefix": "/tmp/wine",
        "xid": 100,
        "title": "World of Warcraft",
        "class": "wowb.exe,wowb.exe",
        "outer": 100,
        "child": 101,
        "outerRoot": 1,
        "outerParent": 1,
        "outerGeometry": [0, 0, 1928, 1092],
        "childGeometry": [0, 0, 1920, 1080],
        "outerChildren": [101],
        "focus": 101,
        "active": 101,
        "display": ":0",
        "nonce": "nonce",
        "binaryDigest": observer.digest,
        "timingsNs": {"proc": 10, "x11": 20, "total": 30},
    }
    try:
        assert (
            observer._decode_response(json.dumps(base).encode(), b"", process=process, nonce="nonce") == base
        )
        for mutation, message in (
            ({"protocolVersion": 2}, "protocol version"),
            ({"timingsNs": {"proc": 10, "x11": 20}}, "timings"),
            ({"outerParent": True}, "integer facts"),
        ):
            invalid = {**base, **mutation}
            with pytest.raises(DriverError, match=message):
                observer._decode_response(json.dumps(invalid).encode(), b"", process=process, nonce="nonce")
        with pytest.raises(DriverError, match="malformed"):
            observer._decode_response(
                b'{"protocolVersion":1,"protocolVersion":1}',
                b"",
                process=process,
                nonce="nonce",
            )
        missing_parent = dict(base)
        missing_parent.pop("outerParent")
        with pytest.raises(DriverError, match="keys are malformed"):
            observer._decode_response(
                json.dumps(missing_parent).encode(), b"", process=process, nonce="nonce"
            )
    finally:
        observer.close()


def test_pinned_identity_rejects_wrong_outer_parent(tmp_path: Path) -> None:
    profile = DriverProfile(
        display=":0",
        xauthority=str(tmp_path / "Xauthority"),
        wine_prefix="/tmp/wine",
        executable="WowB.exe",
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    facts = {
        "protocolVersion": 1,
        "matchingProcessCount": 1,
        "candidateWindowCount": 1,
        "pid": 10,
        "start": "start",
        "exe": "WowB.exe",
        "statusName": "WowB.exe",
        "loader": "wine-preloader",
        "winePrefix": "/tmp/wine",
        "xid": 100,
        "title": "World of Warcraft",
        "class": "wowb.exe,wowb.exe",
        "outer": 100,
        "child": 101,
        "outerRoot": 1,
        "outerParent": 2,
        "outerGeometry": [0, 0, 1928, 1092],
        "childGeometry": [0, 0, 1920, 1080],
        "outerChildren": [101],
        "focus": 101,
        "active": 101,
        "display": ":0",
        "nonce": "nonce",
        "binaryDigest": "digest",
        "timingsNs": {"proc": 10, "x11": 20, "total": 30},
    }
    backend._identity_observer = SimpleNamespace(invoke=lambda *_args, **_kwargs: facts)
    try:
        with pytest.raises(DriverError, match="geometry/tree facts"):
            backend.resolve_identity()
    finally:
        backend.input_tool.close()


def test_unsafe_observer_mode_is_rejected_before_input_or_lease(tmp_path: Path, monkeypatch) -> None:
    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    observer.chmod(0o600)
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    profile = DriverProfile(
        display=":0",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        PinnedInputTool, "load", lambda: (_ for _ in ()).throw(AssertionError("input loaded"))
    )
    with pytest.raises(DriverError, match="private and executable"):
        X11Backend.production(profile)
    assert not (tmp_path / "desktop.lock").exists()


def test_desktop_driver_default_requires_the_production_observer_pin(tmp_path: Path) -> None:
    with pytest.raises(DriverError, match="pinned Rust identity observer is required"):
        DesktopDriver(
            profile=DriverProfile(display=":default-factory"),
            lease_path=tmp_path / "desktop.lock",
        )
    assert not (tmp_path / "desktop.lock").exists()


def test_pinned_observer_timeout_reaps_its_process_group(tmp_path: Path) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":timeout",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    try:
        with pytest.raises(DriverError, match="timed out"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 0.1)
    finally:
        backend.close()


def test_pinned_observer_timeout_reaps_real_elf_child(tmp_path: Path) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    pid_path = tmp_path / "observer.pid"
    source = tmp_path / "observer.c"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <unistd.h>\n"
        "int main(void) {\n"
        f'  FILE *file = fopen("{pid_path}", "w");\n'
        "  if (!file) return 2;\n"
        '  fprintf(file, "%ld\\n", (long)getpid());\n'
        "  fflush(file);\n"
        "  sleep(30);\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    observer = tmp_path / "observer"
    subprocess.run(["cc", str(source), "-O0", "-o", str(observer)], check=True)
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":timeout-elf",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    try:
        with pytest.raises(DriverError, match="timed out"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 0.1)
        deadline = time.monotonic() + 1
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.is_file()
        pid = int(pid_path.read_text(encoding="utf-8"))
        while time.monotonic() < deadline:
            try:
                result = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            assert result == (0, 0)
            time.sleep(0.01)
        else:
            pytest.fail("timed-out observer child was not reaped")
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        backend.close()


def test_pinned_observer_bounds_concurrent_output_and_descendant_pipes(tmp_path: Path) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    noisy = tmp_path / "noisy-observer"
    noisy.write_text(
        "#!/bin/sh\n"
        '/usr/bin/python3 -c \'import sys; sys.stdout.write("x"*70000); sys.stderr.write("y"*70000)\'\n',
        encoding="utf-8",
    )
    noisy.chmod(0o700)
    profile = DriverProfile(
        display=":noisy",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=noisy,
        identity_observer_executable_sha256=hashlib.sha256(noisy.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    try:
        with pytest.raises(DriverError, match="stdout is oversized"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 2.0)
    finally:
        backend.close()

    duplicate = tmp_path / "duplicate-response-observer"
    duplicate.write_text(
        "#!/bin/sh\nIFS= read -r _request\nprintf '%s\\n' '{\"matchingProcessCount\":1,\"matchingProcessCount\":1}'\n",
        encoding="utf-8",
    )
    duplicate.chmod(0o700)
    duplicate_profile = replace(
        profile,
        identity_observer_executable=duplicate,
        identity_observer_executable_sha256=hashlib.sha256(duplicate.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(
        duplicate_profile, input_tool=fixture_input_tool(tmp_path / "duplicate-tool")
    )
    try:
        with pytest.raises(DriverError, match="identity observer output is malformed"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 2.0)
    finally:
        backend.close()

    held = tmp_path / "held-pipe-observer"
    held.write_text(
        "#!/bin/sh\nIFS= read -r _request\n( /bin/sleep 30 ) &\nprintf '%s\\n' '{\"error\":\"held\"}'\nexit 0\n",
        encoding="utf-8",
    )
    held.chmod(0o700)
    held_profile = replace(
        profile,
        identity_observer_executable=held,
        identity_observer_executable_sha256=hashlib.sha256(held.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(held_profile, input_tool=fixture_input_tool(tmp_path / "held-tool"))
    try:
        with pytest.raises(DriverError, match="identity observer output keys are malformed"):
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 2.0)
    finally:
        backend.close()


def test_pinned_observer_cancellation_reaps_its_process_group(tmp_path: Path) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    observer = tmp_path / "observer"
    observer.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":cancel",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        time.sleep(0.05)
        backend._cancel_active.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DriverError)
        assert "cancelled" in str(errors[0])
    finally:
        backend.close()


def test_pinned_observer_cancellation_reaps_real_elf_child(tmp_path: Path) -> None:
    xauthority = tmp_path / "Xauthority"
    xauthority.touch(mode=0o600)
    pid_path = tmp_path / "cancel-observer.pid"
    source = tmp_path / "cancel-observer.c"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <unistd.h>\n"
        "int main(void) {\n"
        f'  FILE *file = fopen("{pid_path}", "w");\n'
        "  if (!file) return 2;\n"
        '  fprintf(file, "%ld\\n", (long)getpid());\n'
        "  fflush(file);\n"
        "  sleep(30);\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    observer = tmp_path / "cancel-observer"
    subprocess.run(["cc", str(source), "-O0", "-o", str(observer)], check=True)
    observer.chmod(0o700)
    profile = DriverProfile(
        display=":cancel-elf",
        xauthority=str(xauthority),
        wine_prefix=str(tmp_path / "wine"),
        identity_observer_executable=observer,
        identity_observer_executable_sha256=hashlib.sha256(observer.read_bytes()).hexdigest(),
    )
    backend = X11Backend.production(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            backend.resolve_identity(deadline_monotonic=time.monotonic() + 5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        time.sleep(0.05)
        backend._cancel_active.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], DriverError)
        assert "cancelled" in str(errors[0])
        deadline = time.monotonic() + 1
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.is_file()
        pid = int(pid_path.read_text(encoding="utf-8"))
        while time.monotonic() < deadline:
            try:
                result = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            assert result == (0, 0)
            time.sleep(0.01)
        else:
            pytest.fail("cancelled observer child was not reaped")
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        backend.close()


def test_begin_action_does_not_publish_unemitted_planned_holds(tmp_path: Path) -> None:
    from crowley.session.guardian import InputGuardian

    guardian = InputGuardian(tmp_path / "planned.lock", parent_pid=os.getpid())

    class Evidence:
        is_live = True
        fingerprint = "planned"

    generation = guardian.arm(Evidence())
    action = guardian.begin_action(
        generation,
        target_xid=0x101,
        source_device=4,
        master_device=2,
        event_classes={"RawKeyPress"},
        required_sequence=("RawKeyPress",),
        held_keys={"w"},
    )
    try:
        assert not guardian.held
        assert not action.complete
    finally:
        guardian.revoke("test cleanup")
        guardian.close()


def test_publish_holds_rejects_keys_outside_planned_action(tmp_path: Path) -> None:
    from crowley.session.guardian import GuardianError, InputGuardian

    guardian = InputGuardian(tmp_path / "planned-mismatch.lock", parent_pid=os.getpid())

    class Evidence:
        is_live = True
        fingerprint = "planned-mismatch"

    generation = guardian.arm(Evidence())
    guardian.begin_action(
        generation,
        target_xid=0x101,
        source_device=4,
        master_device=2,
        event_classes={"RawKeyPress"},
        required_sequence=("RawKeyPress",),
        held_keys={"w"},
    )
    try:
        with pytest.raises(GuardianError, match="do not match"):
            guardian.publish_current_holds(generation, {"s"}, set())
        assert not guardian.held
    finally:
        guardian.revoke("test cleanup")
        guardian.close()


def test_native_window_certificate_rejects_extra_direct_helper(tmp_path: Path) -> None:
    backend = X11Backend(
        DriverProfile(display=":actions", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    tree = """\
xwininfo: Window id: 0x1 (the root window)
  0x100 "outer": () 400x300+0+0
    0x101 "client": () 400x300+0+0
    0x102 "helper": () 1x1+0+0
"""
    backend._run = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, tree, "")  # type: ignore[method-assign]
    with pytest.raises(DriverError, match="unexpected helper"):
        backend._verified_window_relationship(0x100, 0x101, deadline_monotonic=time.monotonic() + 1)


def test_native_target_membership_requires_nonempty_exact_certificate(tmp_path: Path) -> None:
    backend = X11Backend(
        DriverProfile(display=":actions", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    identity = _identity()
    assert backend._is_owned_client_target(identity.xid, identity)
    assert backend._is_owned_client_target(identity.child_xid, identity)
    for foreign in (identity.root_xid, 0x102, 0x999):
        assert not backend._is_owned_client_target(foreign, identity)
    incomplete = replace(identity, target_event_xids=frozenset())
    assert not backend._is_owned_client_target(identity.xid, incomplete)
    assert not backend._is_owned_client_target(identity.child_xid, incomplete)


def test_native_command_deadline_terminates_before_backend_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = X11Backend(
        DriverProfile(display=":actions", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: "/bin/sleep")
    with pytest.raises(DriverError, match="timed out|deadline"):
        backend._run(
            ["sleep", "0.5"],
            timeout=2.0,
            deadline_monotonic=time.monotonic() + 0.03,
        )
    assert not backend._active_processes


def test_desktop_identity_deadline_reaches_native_resolver(tmp_path: Path) -> None:
    backend = X11Backend(
        DriverProfile(display=":actions", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    identity = _identity()
    received: list[float | None] = []

    def resolve(*, require_foreground: bool = True, deadline_monotonic: float | None = None):
        del require_foreground
        received.append(deadline_monotonic)
        return identity

    backend.resolve_identity = resolve  # type: ignore[method-assign]
    driver = DesktopDriver(profile=backend.profile, backend=backend, lease_path=tmp_path / "actions.lock")
    deadline = time.monotonic() + 1
    try:
        assert driver.resolve_identity(deadline_monotonic=deadline) is identity
        assert received == [deadline]
    finally:
        driver.close()


def test_desktop_run_passes_one_absolute_deadline_through_x11_keyboard_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    native_calls: list[tuple[list[str], float | None]] = []
    _install_deadline_keyboard_runtime(driver, monkeypatch, native_calls)
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    deadline = time.monotonic() + 2.0
    try:
        driver.run(
            "open_chat",
            generation,
            precondition=lambda _identity: True,
            postcondition=lambda _identity: True,
            deadline_monotonic=deadline,
        )
        assert native_calls == [(["xdotool", "keydown", "036", "keyup", "036"], deadline)]
    finally:
        driver.close()


def test_deadline_expiring_inside_focus_blocks_later_keyboard_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary_entered = threading.Event()
    deadline: float

    def block_until_expired(*_args, **_kwargs) -> None:
        boundary_entered.set()
        while time.monotonic() < deadline:
            time.sleep(0.005)

    driver = _driver(tmp_path, monkeypatch)
    native_calls: list[tuple[list[str], float | None]] = []
    _install_deadline_keyboard_runtime(
        driver,
        monkeypatch,
        native_calls,
        focus=block_until_expired,
    )
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    deadline = time.monotonic() + 0.25
    try:
        with pytest.raises(DriverError, match="deadline"):
            driver.run(
                "open_chat",
                generation,
                precondition=lambda _identity: True,
                postcondition=lambda _identity: True,
                deadline_monotonic=deadline,
            )
        assert boundary_entered.is_set()
        assert native_calls == []
        assert not driver.backend._active_processes
        assert not driver.backend._held_keys
    finally:
        driver.close()


def test_prepare_command_deadline_stops_before_submit_after_slow_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    native_calls: list[tuple[list[str], float | None]] = []
    _install_deadline_keyboard_runtime(driver, monkeypatch, native_calls)
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    boundary_entered = threading.Event()
    deadline: float

    def slow_readback() -> str:
        boundary_entered.set()
        while time.monotonic() < deadline:
            time.sleep(0.005)
        return "/crowley status"

    try:
        deadline = time.monotonic() + 0.25
        with pytest.raises(DriverError, match="deadline"):
            driver.prepare_command(
                generation,
                "/crowley status",
                {"chatEdit": {"active": True, "focused": True, "text": ""}},
                readback=slow_readback,
                precondition=lambda _identity: True,
                deadline_monotonic=deadline,
            )
        assert boundary_entered.is_set()
        assert native_calls
        assert not any("036" in args for args, _deadline in native_calls)
        assert all(call_deadline == deadline for _args, call_deadline in native_calls)
    finally:
        driver.close()


def test_prepare_command_forwards_submit_precondition_and_rejects_changed_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    native_calls: list[tuple[list[str], float | None]] = []
    _install_deadline_keyboard_runtime(driver, monkeypatch, native_calls)
    generation = driver.guardian.arm(driver._input_evidence)
    driver.backend.activate_generation(generation)
    driver.guardian.finish_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    precondition_calls: list[object] = []

    def guard_changes_after_typing(identity: object) -> bool:
        precondition_calls.append(identity)
        return len(precondition_calls) == 1

    deadline = time.monotonic() + 2.0
    try:
        with pytest.raises(DriverError, match="submit_command precondition failed"):
            driver.prepare_command(
                generation,
                "/crowley status",
                {"chatEdit": {"active": True, "focused": True, "text": ""}},
                readback=lambda: "/crowley status",
                precondition=guard_changes_after_typing,
                deadline_monotonic=deadline,
            )
        assert len(precondition_calls) == 2
        assert native_calls
        assert not any("Return" in args for args, _deadline in native_calls)
    finally:
        driver.close()


def test_world_click_deadline_after_durable_precommit_refuses_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    identity = _identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    commands: list[list[str]] = []

    def run_deadline_command(args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("input_schema") == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run_deadline_command  # type: ignore[method-assign]
    release_calls: list[list[str]] = []
    backend._release_command = lambda args: release_calls.append(list(args)) or True  # type: ignore[method-assign]
    # Exercise the same world-click admission without requiring a canonical
    # XTEST motion trace; the precommit boundary is reached before emission.
    driver._input_evidence.xtest_profile = None
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    binding = BindingMetadata(
        identity.fingerprint,
        "mail-gesture",
        1,
        generation,
        identity.child_xid,
        (identity.child_xid,),
        source="native-visual",
    )
    gesture = pointer_execution.plan_human_pointer(
        Point(20, 20),
        PointerTarget("mailbox", Rect(80, 60, 100, 30), binding, padding=0),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=0x4321,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )
    endpoint = gesture.endpoint.to_pixel()
    backend.pointer_position = lambda **_kwargs: (endpoint.x, endpoint.y, identity.child_xid)  # type: ignore[method-assign]
    permit = pointer_execution.acquire_click_rate_permit()
    capture_monotonic = time.monotonic()
    boundary_entered = threading.Event()
    deadline: float
    hover = SimpleNamespace(
        identity_fingerprint=identity.fingerprint,
        capture_monotonic=capture_monotonic,
        hover_kind="unit",
        hover_name="Mailbox",
        chat_focused=False,
    )
    original_write_state = pointer_execution._write_state

    def slow_write_state(directory, state):  # type: ignore[no-untyped-def]
        original_write_state(directory, state)
        boundary_entered.set()
        while time.monotonic() < deadline:
            time.sleep(0.005)

    monkeypatch.setattr(pointer_execution, "_write_state", slow_write_state)
    deadline = time.monotonic() + 0.25
    try:
        with pytest.raises(DriverError, match="deadline"):
            driver.run_hover_click(
                endpoint,
                hover,
                gesture=gesture,
                gesture_ended_monotonic=permit.acquired_monotonic - 0.1,
                generation=generation,
                deadline_monotonic=deadline,
                click_rate_permit=permit,
                target_name="Mailbox",
            )
        assert boundary_entered.is_set()
        state = pointer_execution._read_state(tmp_path / "pointer-rate-state")
        assert state is not None
        assert state["lastPressMonotonicNs"] is None
        assert state["nextAllowedMonotonicNs"] > time.monotonic_ns()
        assert commands == []
        assert release_calls == []
        assert not backend._held_buttons
        assert not driver.guardian.held
    finally:
        permit.close()
        driver.close()


def test_world_click_guardian_wait_expiry_retains_actual_press_rate_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver(tmp_path, monkeypatch)
    backend = driver.backend
    assert isinstance(backend, X11Backend)
    identity = _identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    commands: list[list[str]] = []

    def run_wait_expiry_command(args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("input_schema") == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    backend._run = run_wait_expiry_command  # type: ignore[method-assign]
    driver._input_evidence.xtest_profile = None
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    binding = BindingMetadata(
        identity.fingerprint,
        "mail-gesture",
        1,
        generation,
        identity.child_xid,
        (identity.child_xid,),
        source="native-visual",
    )
    gesture = pointer_execution.plan_human_pointer(
        Point(20, 20),
        PointerTarget("mailbox", Rect(80, 60, 100, 30), binding, padding=0),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=0x9876,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )
    endpoint = gesture.endpoint.to_pixel()
    backend.pointer_position = lambda **_kwargs: (endpoint.x, endpoint.y, identity.child_xid)  # type: ignore[method-assign]
    permit = pointer_execution.acquire_click_rate_permit()
    hover = SimpleNamespace(
        identity_fingerprint=identity.fingerprint,
        capture_monotonic=time.monotonic(),
        hover_kind="unit",
        hover_name="Mailbox",
        chat_focused=False,
    )
    boundary_entered = threading.Event()
    deadline: float

    class ExpiringMonitor:
        def wait_for_event(self, _predicate, *, timeout: float) -> None:
            assert 0 < timeout <= 0.25
            boundary_entered.set()
            while time.monotonic() < deadline:
                time.sleep(0.005)

        def stop(self) -> None:
            return None

    driver._monitor = ExpiringMonitor()  # type: ignore[assignment]
    deadline = time.monotonic() + 0.25
    try:
        with pytest.raises(DriverError, match="deadline|event trace"):
            driver.run_hover_click(
                endpoint,
                hover,
                gesture=gesture,
                gesture_ended_monotonic=permit.acquired_monotonic - 0.1,
                generation=generation,
                deadline_monotonic=deadline,
                click_rate_permit=permit,
                target_name="Mailbox",
            )
        assert boundary_entered.is_set()
        state = pointer_execution._read_state(tmp_path / "pointer-rate-state")
        assert state is not None
        assert isinstance(state["lastPressMonotonicNs"], int)
        assert commands and commands[-1][1:3] == ["mousedown", "1"]
        assert not driver.guardian.held
    finally:
        permit.close()
        driver.close()
