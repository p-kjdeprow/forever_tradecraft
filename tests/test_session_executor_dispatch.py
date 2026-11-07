from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from crowley.session import pointer_execution
from crowley.session.driver import (
    DriverError,
    DriverProfile,
    MeasuredAnchor,
    WindowIdentity,
    X11Backend,
    build_camera_drag_chain,
    build_pointer_motion_chain,
)
from crowley.session.executor import (
    BindingMetadata,
    DemonstrationRole,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    PointerTrajectory,
    Rect,
    plan_camera_drag,
    plan_pointer_curve,
    plan_single_click,
)
from crowley.session.guardian import InputGuardian
from crowley.session.input_broker import ButtonEmissionClaim, InputBroker
from crowley.session.model import LunaAdapter
from tests.test_session_input_tool import fixture_input_tool


class _DispatchRatePermit:
    is_open = True
    acquired_monotonic = 0.0

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        del deadline_monotonic
        return 1

    def record_press(self) -> int:
        return 1

    def close(self) -> None:
        return None


def test_closed_gesture_argv_is_admitted_as_one_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    backend._active_generation = 1
    backend._admission = lambda generation, dispatch: dispatch()
    backend._register_process_group = lambda _generation, _pgid: None
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: "/bin/true")
    admitted_chain = (
        "xdotool",
        "sleep",
        "0.010",
        "mousemove",
        "10",
        "20",
        "mousedown",
        "3",
        "sleep",
        "0.050",
        "mousemove",
        "30",
        "40",
        "sleep",
        "0.020",
        "mouseup",
        "3",
    )
    broker = InputBroker.for_tests(
        clock=lambda: 0.0,
        permit_factory=lambda **_kwargs: _DispatchRatePermit(),
    )
    backend.input_broker = broker
    reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
    gesture = broker.mint_admitted_gesture(
        action_generation=1,
        lease_generation=1,
        button=PointerButton.RIGHT,
        emitted_chain=admitted_chain,
        destination_anchor="dispatch-test",
        target_identity="dispatch-test",
        guardian_admission_id="dispatch-test",
        semantic_authorization="dispatch-test",
        deadline_monotonic=10.0,
        reservation=reservation,
    )
    try:
        result_box: list[CompletedProcess[str]] = []
        claim_box: list[ButtonEmissionClaim] = []

        with pytest.raises(DriverError, match="broker emission claim"):
            backend._run(
                list(admitted_chain),
                generation=1,
                input_schema="camera-drag",
                expected_argv=admitted_chain,
                held_buttons={3},
                admitted_button_gesture=gesture,
            )

        def emit(claim: ButtonEmissionClaim) -> None:
            claim_box.append(claim)
            result_box.append(
                backend._run(
                    list(admitted_chain),
                    generation=1,
                    input_schema="camera-drag",
                    expected_argv=admitted_chain,
                    held_buttons={3},
                    button_emission_claim=claim,
                    emission_action_generation=1,
                    emission_lease_generation=1,
                )
            )

        broker.emit_button_gesture(
            gesture,
            emitted_chain=admitted_chain,
            action_generation=1,
            lease_generation=1,
            destination_anchor="dispatch-test",
            target_identity="dispatch-test",
            guardian_admission_id="dispatch-test",
            semantic_authorization="dispatch-test",
            emit=emit,
        )
        assert len(result_box) == 1
        result = result_box[0]
        assert result.returncode == 0
        assert not backend._active_processes
        with pytest.raises(DriverError, match="foreign or already redeemed"):
            backend._run(
                list(admitted_chain),
                generation=1,
                input_schema="camera-drag",
                expected_argv=admitted_chain,
                held_buttons={3},
                button_emission_claim=claim_box[0],
                emission_action_generation=1,
                emission_lease_generation=1,
            )
        with pytest.raises(DriverError, match="not allowlisted"):
            backend._run(
                [
                    "xdotool",
                    "mousemove",
                    "10",
                    "20",
                    "sleep",
                    "0.010",
                    "mousemove",
                    "30",
                    "40",
                    "mousedown",
                    "3",
                    "mouseup",
                    "3",
                ],
                generation=1,
                held_buttons={3},
                input_schema="camera-drag",
            )
        with pytest.raises(DriverError, match="not allowlisted"):
            backend._run(
                ["xdotool", "keydown", "w", "sleep", "0.010", "keyup", "w"],
                generation=1,
                held_buttons={3},
                input_schema="camera-drag",
            )
    finally:
        broker.release_reservation(reservation)
        backend.release_held_input()


def test_closed_gesture_argv_rejects_shell_or_unbalanced_input(tmp_path: Path) -> None:
    backend = X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    backend._active_generation = 1
    backend._admission = lambda generation, dispatch: dispatch()
    with pytest.raises(DriverError, match="not allowlisted"):
        backend._run(["xdotool", "mousemove", "10", "20", "exec", "sh"], generation=1)
    with pytest.raises(DriverError, match="not allowlisted"):
        backend._run(["xdotool", "mousedown", "3", "sleep", "0.1"], generation=1)
    backend.release_held_input()


def test_motion_key_schema_runs_one_closed_unmodified_pulse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    backend._active_generation = 1
    backend._admission = lambda generation, dispatch: dispatch()
    backend._register_process_group = lambda _generation, _pgid: None
    monkeypatch.setattr("crowley.session.driver._resolve_wrapped_executable", lambda _command: "/bin/true")
    argv = ["xdotool", "keydown", "w", "sleep", "0.010", "keyup", "w"]
    try:
        result = backend._run(
            argv,
            generation=1,
            held_keys={"w"},
            input_schema="motion-key",
            expected_argv=tuple(argv),
        )
        assert result.returncode == 0
        with pytest.raises(DriverError, match="allowlisted"):
            backend._run(
                ["xdotool", "keydown", "W", "sleep", "0.010", "keyup", "W"],
                generation=1,
                input_schema="motion-key",
            )
    finally:
        backend.release_held_input()


def test_pointer_chain_preserves_duplicate_sample_intervals_and_trailing_sleep() -> None:
    binding = BindingMetadata("window", "capture", 1, 1, 7, (7,))
    trajectory = PointerTrajectory(
        (
            PointerSample(0.0, Point(10, 10)),
            PointerSample(0.1, Point(20, 20)),
            PointerSample(0.2, Point(20, 20)),
            PointerSample(0.3, Point(30, 30)),
            PointerSample(0.4, Point(30, 30)),
        ),
        0.4,
        Point(30, 30),
        binding,
        "control",
    )
    chain = build_pointer_motion_chain(trajectory, (100, 200, 400, 300))
    sleeps = [float(chain[index + 1]) for index, token in enumerate(chain) if token == "sleep"]
    assert sleeps == pytest.approx([0.1, 0.1, 0.1, 0.1])
    assert sum(sleeps) == pytest.approx(trajectory.duration)
    assert chain.count("mousemove") == 2


def test_pointer_chain_rejects_malformed_or_off_client_trajectory() -> None:
    binding = BindingMetadata("window", "capture", 1, 1, 7, (7,))
    malformed = PointerTrajectory(
        (PointerSample(0.0, Point(10, 10)), PointerSample(0.2, Point(20, 20))),
        0.2,
        Point(20, 20),
        binding,
        "control",
    )
    with pytest.raises(DriverError, match="escaped"):
        build_pointer_motion_chain(malformed, (100, 200, 5, 5))
    invalid_timing = PointerTrajectory(
        (PointerSample(0.0, Point(1, 1)), PointerSample(0.2, Point(2, 2))),
        0.2,
        Point(2, 2),
        binding,
        "control",
    )
    object.__setattr__(invalid_timing.samples[1], "at", 0.0)
    with pytest.raises(DriverError, match="strictly bounded"):
        build_pointer_motion_chain(invalid_timing, (0, 0, 20, 20))


def test_camera_chain_positions_at_measured_viewport_before_button_down() -> None:
    binding = BindingMetadata("window", "capture", 1, 1, 7, (7,))
    plan = plan_camera_drag(
        Point(100, 100),
        Point(140, 100),
        Rect(0, 0, 400, 300),
        binding,
        profile=MotionProfile(role=DemonstrationRole.CAMERA),
    )
    positioning = plan_pointer_curve(
        Point(20, 20),
        PointerTarget("camera-viewport", Rect(100, 100, 1, 1), binding, padding=0),
        Rect(0, 0, 400, 300),
        profile=MotionProfile(role=DemonstrationRole.POINTER),
    )
    chain = build_camera_drag_chain(plan, (0, 0, 400, 300), positioning)
    assert chain[0] == "xdotool"
    assert chain.index("mousedown") > chain.index("mousemove")
    assert chain[: chain.index("mousedown")].count("mousemove") >= 2
    assert chain[chain.index("mousedown") + 1] == "3"
    assert chain[-2:] == ["mouseup", "3"]


def test_viewport_model_boundary_requires_world_clearance_and_integer_point() -> None:
    usage = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }

    def stream(answer: object) -> str:
        return "\n".join(
            (
                '{"type":"thread.started","thread_id":"viewport-test"}',
                '{"type":"turn.started"}',
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "answer-1",
                            "type": "agent_message",
                            "text": json.dumps(answer, separators=(",", ":")),
                        },
                    },
                    separators=(",", ":"),
                ),
                json.dumps({"type": "turn.completed", "usage": usage}, separators=(",", ":")),
            )
        )

    observation = LunaAdapter._parse_viewport_observation(
        stream({"scene": "world", "point": {"x": 300, "y": 200}, "uiClear": True, "unitsClear": True})
    )
    assert observation.point == (300, 200)
    refused = LunaAdapter._parse_viewport_observation(
        stream(
            {
                "scene": "world",
                "point": {"x": 300, "y": 200},
                "uiClear": False,
                "unitsClear": True,
            }
        )
    )
    assert not refused.ui_clear


def test_pointer_position_accepts_only_complete_integer_shell_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = X11Backend(
        DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority")),
        input_tool=fixture_input_tool(tmp_path / "tool"),
    )
    monkeypatch.setattr(
        backend,
        "_run",
        lambda _args, **_kwargs: CompletedProcess(
            ["xdotool"], 0, "X=12\nY=-4\nSCREEN=0\nWINDOW=0x700101\n", ""
        ),
    )
    assert backend.pointer_position(generation=1) == (12, -4, 0x700101)
    monkeypatch.setattr(
        backend,
        "_run",
        lambda _args, **_kwargs: CompletedProcess(["xdotool"], 0, "X=12\nY=4\nWINDOW=\n", ""),
    )
    with pytest.raises(DriverError, match="malformed"):
        backend.pointer_position(generation=1)


def _test_anchor(
    tmp_path: Path,
    identity: WindowIdentity,
    *,
    sequence: int,
    bounds: tuple[int, int, int, int],
    action: str = "logout",
    control: str = "world",
) -> MeasuredAnchor:
    capture_path = tmp_path / f"anchor-{sequence}.png"
    capture_path.write_bytes(f"capture-{sequence}".encode())
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    return MeasuredAnchor.from_capture(
        action=action,
        x=bounds[0] + bounds[2] // 2,
        y=bounds[1] + bounds[3] // 2,
        identity_fingerprint=identity.fingerprint,
        target_xid=identity.child_xid,
        capture_id=digest,
        capture_sequence=sequence,
        source="native-visual",
        observed_monotonic=float(sequence),
        capture_monotonic=float(sequence),
        scene="world",
        bounds=bounds,
        target_ancestry=(identity.xid, identity.child_xid),
        capture_sha256=digest,
        generation=1,
        capture_path=str(capture_path),
        facts=(
            ("control", control),
            ("capture", digest),
            ("target", str(identity.child_xid)),
        ),
    )


def _gesture(pointer: PointerTrajectory, click) -> pointer_execution.HumanPointerPlan:
    return pointer_execution.HumanPointerPlan(
        seed=1,
        trajectory=pointer,
        waypoints=(),
        click=click,
    )


def _patch_pointer_rate_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "pointer-rate-state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: state)


def test_native_action_uses_open_ah_keyboard_chord(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pointer_rate_state(monkeypatch, tmp_path)
    profile = DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "start",
        profile.display,
        "identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    backend.resolve_identity = lambda *, require_foreground=True, deadline_monotonic=None: identity  # type: ignore[method-assign]
    monkeypatch.setattr(backend, "_focus", lambda *_args, **_kwargs: None)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run",
        lambda args, **_kwargs: commands.append(args) or CompletedProcess(args, 0, "", ""),
    )
    backend.action("open_ah", generation=1, _keyboard_keymap={"Control_L": 37, "F12": 88})
    assert len(commands) == 1
    assert commands[0][1:] == [
        "keydown",
        "037",
        "keydown",
        "088",
        "keyup",
        "088",
        "keyup",
        "037",
    ]


def test_pointer_motion_requires_fresh_unchanged_anchor_before_click(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pointer_rate_state(monkeypatch, tmp_path)
    profile = DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "start",
        profile.display,
        "identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    backend.resolve_identity = lambda *, require_foreground=True, deadline_monotonic=None: identity  # type: ignore[method-assign]
    monkeypatch.setattr(backend, "_focus", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backend, "pointer_position", lambda **_kwargs: (10, 10, identity.child_xid))
    monkeypatch.setattr(backend, "is_descendant", lambda _xid, _ancestor: True)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        backend,
        "_run",
        lambda args, **_kwargs: commands.append(args) or CompletedProcess(args, 0, "", ""),
    )
    old = _test_anchor(tmp_path, identity, sequence=1, bounds=(100, 100, 30, 30))
    fresh = _test_anchor(tmp_path, identity, sequence=2, bounds=(140, 100, 30, 30))
    backend._measured_anchors["logout"] = old
    backend._consumed_anchor_seals.add(old.evidence_hash)
    binding = BindingMetadata(
        identity.fingerprint,
        old.capture_id,
        old.capture_sequence,
        1,
        identity.child_xid,
        old.target_ancestry,
    )
    target = PointerTarget("world", Rect(*old.bounds), binding, padding=1)
    click = plan_single_click(target, Rect(0, 0, 1920, 1080), button=PointerButton.LEFT)
    pointer = plan_pointer_curve(Point(10, 10), target, Rect(0, 0, 1920, 1080))
    gesture = _gesture(pointer, click)
    with pytest.raises(DriverError, match="changed control, bounds, or identity"):
        with pointer_execution.acquire_click_rate_permit() as permit:
            backend.action(
                "logout",
                generation=1,
                _admitted_anchor=old,
                _pointer_plan=pointer,
                _click_plan=click,
                _gesture_plan=gesture,
                _click_rate_permit=permit,
                _refresh_anchor=lambda _identity, _previous: fresh,
            )
    assert any("mousemove" in command for command in commands)
    assert not any(command[1:2] == ["mousedown"] for command in commands)


def test_pointer_click_success_records_protected_boundary_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pointer_rate_state(monkeypatch, tmp_path)
    profile = DriverProfile(display=":dispatch-order", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "start",
        profile.display,
        "identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    order: list[str] = []
    backend.resolve_identity = lambda *, require_foreground=True, deadline_monotonic=None: (  # type: ignore[method-assign]
        order.append("identity-foreground" if require_foreground else "identity-initial") or identity
    )
    monkeypatch.setattr(backend, "_focus", lambda *_args, **_kwargs: order.append("focus"))
    moved = False
    moved_pointer_reads = 0
    endpoint = (115, 115)

    def pointer_position(**_kwargs: object) -> tuple[int, int, int]:
        nonlocal moved_pointer_reads
        if moved:
            moved_pointer_reads += 1
            order.append("pointer-pre" if moved_pointer_reads == 1 else "pointer-post")
        else:
            order.append("pointer-start")
        return (*endpoint, identity.child_xid) if moved else (10, 10, identity.child_xid)

    backend.pointer_position = pointer_position  # type: ignore[method-assign]
    monkeypatch.setattr(backend, "is_descendant", lambda _xid, _ancestor: True)
    old = _test_anchor(tmp_path, identity, sequence=1, bounds=(100, 100, 30, 30))
    fresh = _test_anchor(tmp_path, identity, sequence=2, bounds=(100, 100, 30, 30))
    backend._measured_anchors["logout"] = old

    class RecordingConsumed(set[str]):
        checks = 0

        def __contains__(self, item: object) -> bool:
            self.checks += 1
            order.append("consumed-initial" if self.checks == 1 else "consumed-check")
            return super().__contains__(item)

    consumed = RecordingConsumed({old.evidence_hash})
    backend._consumed_anchor_seals = consumed
    binding = BindingMetadata(
        identity.fingerprint,
        old.capture_id,
        old.capture_sequence,
        1,
        identity.child_xid,
        old.target_ancestry,
    )
    target = PointerTarget("world", Rect(*old.bounds), binding, padding=1)
    click = plan_single_click(target, Rect(0, 0, 1920, 1080), button=PointerButton.LEFT)
    endpoint = (int(click.point.x), int(click.point.y))
    pointer = plan_pointer_curve(Point(10, 10), target, Rect(0, 0, 1920, 1080))

    def run(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        nonlocal moved
        schema = kwargs.get("input_schema")
        if schema == "pointer-motion":
            order.append("motion")
            moved = True
        elif schema == "single-click":
            backend.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
            order.append("click")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backend, "_run", run)

    def refresh(_identity: WindowIdentity, _previous: object) -> object:
        order.append("anchor-refresh")
        return fresh

    with pointer_execution.acquire_click_rate_permit() as permit:
        backend.action(
            "logout",
            generation=1,
            _admitted_anchor=old,
            _pointer_plan=pointer,
            _click_plan=click,
            _gesture_plan=_gesture(pointer, click),
            _click_rate_permit=permit,
            _refresh_anchor=refresh,
        )
    assert order.index("motion") < order.index("identity-foreground") < order.index("anchor-refresh")
    assert order.index("anchor-refresh") < order.index("consumed-check") < order.index("pointer-pre")
    assert order.index("pointer-pre") < order.index("click")
    assert order.index("click") < order.index("pointer-post")


def test_pointer_plan_binding_and_endpoint_must_match_click_before_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pointer_rate_state(monkeypatch, tmp_path)
    profile = DriverProfile(display=":dispatch", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        profile.window_title,
        profile.window_class,
        0x700101,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        0x700100,
        "start",
        profile.display,
        "identity",
        0x1,
        0x700100,
        frozenset({0x700100, 0x700101}),
    )
    backend.resolve_identity = lambda *, require_foreground=True, deadline_monotonic=None: identity  # type: ignore[method-assign]
    monkeypatch.setattr(backend, "_focus", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backend, "pointer_position", lambda **_kwargs: (10, 10, identity.child_xid))
    monkeypatch.setattr(backend, "is_descendant", lambda _xid, _ancestor: True)
    anchor = _test_anchor(tmp_path, identity, sequence=1, bounds=(100, 100, 30, 30))
    backend._measured_anchors["logout"] = anchor
    backend._consumed_anchor_seals.add(anchor.evidence_hash)
    binding = BindingMetadata(
        identity.fingerprint,
        anchor.capture_id,
        anchor.capture_sequence,
        1,
        identity.child_xid,
        anchor.target_ancestry,
    )
    target = PointerTarget("world", Rect(*anchor.bounds), binding, padding=1)
    click = plan_single_click(target, Rect(0, 0, 1920, 1080), button=PointerButton.LEFT)
    pointer = plan_pointer_curve(Point(10, 10), target, Rect(0, 0, 1920, 1080))
    mismatched = pointer
    object.__setattr__(mismatched, "endpoint", Point(pointer.endpoint.x - 1, pointer.endpoint.y))
    with pytest.raises(DriverError, match="cross-match"):
        with pointer_execution.acquire_click_rate_permit() as permit:
            backend.action(
                "logout",
                generation=1,
                _admitted_anchor=anchor,
                _pointer_plan=mismatched,
                _click_plan=click,
                _gesture_plan=_gesture(mismatched, click),
                _click_rate_permit=permit,
                _refresh_anchor=lambda _identity, _previous: anchor,
            )


def _event(
    event_class: str,
    timestamp: int,
    *,
    target: int | None,
    root: tuple[int, int] | None = None,
    source: int = 4,
    master: int = 2,
):
    raw = event_class.startswith("Raw")
    return SimpleNamespace(
        event_class=event_class,
        source_device=source,
        master_device=master,
        target_xid=target,
        detail=3,
        timestamp_ms=timestamp,
        modifiers=None if raw else (0, 0, 0),
        modifier_effective=None if raw else 0,
        is_raw=raw,
        root_xy=root,
        event_xy=root,
    )


def test_guardian_accepts_ordered_coalesced_motion_then_exact_click(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "trace.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "trace-evidence"

    generation = guardian.arm(Evidence())
    action = guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=2,
        event_classes={"Motion", "RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"},
        optional_classes={"Motion"},
        required_sequence=("RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"),
        expected_details=(3, 3, 3, 3),
        expected_motion_trace=((10, 20, 10, 20), (30, 40, 30, 40)),
    )
    try:
        assert guardian.observe(_event("Motion", 1, target=0x700101, root=(10, 20)))
        # The second sample may be coalesced by X11; the ordered endpoint is
        # still required before the button commitment.
        assert guardian.observe(_event("Motion", 2, target=0x700101, root=(30, 40)))
        assert guardian.observe(_event("RawButtonPress", 3, target=None))
        assert guardian.observe(_event("ButtonPress", 4, target=0x700101, root=(30, 40)))
        assert guardian.observe(_event("RawButtonRelease", 5, target=None))
        assert guardian.observe(_event("ButtonRelease", 6, target=0x700101, root=(30, 40)))
        assert action.complete
    finally:
        if guardian.armed and guardian.generation == generation:
            guardian.finish_action(generation, action)
        guardian.close()


def test_guardian_requires_one_forwarded_echo_per_emitted_motion(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "echo-pairs.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "echo-pairs-evidence"

    generation = guardian.arm(Evidence())
    action = guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=2,
        event_classes={"Motion"},
        device_by_class={"Motion": 2},
        optional_classes={"Motion"},
        expected_motion_trace=((10, 20, 10, 20), (30, 40, 30, 40)),
        echo_proof=(("MotionEcho", 100, 7, 7, 0x700101, (10, 20), (10, 20), 90, 4, 0x700101),),
    )
    try:
        assert guardian.observe(_event("Motion", 90, target=0x700101, root=(10, 20)))
        assert guardian.observe(_event("Motion", 91, target=0x700101, root=(30, 40)))
        assert not action.complete
        assert guardian.observe(_event("Motion", 100, target=0x700101, root=(10, 20), source=7, master=7))
        assert not action.complete
        assert guardian.observe(_event("Motion", 101, target=0x700101, root=(30, 40), source=7, master=7))
        assert action.complete
        assert action.echo_index == 2
    finally:
        if guardian.armed and guardian.generation == generation:
            guardian.finish_action(generation, action)
        guardian.close()


def test_guardian_revokes_on_interloper_motion_before_commit(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "interloper.lock", parent_pid=os.getppid())

    class Evidence:
        is_live = True
        fingerprint = "interloper-evidence"

    generation = guardian.arm(Evidence())
    guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=2,
        event_classes={"Motion", "ButtonPress"},
        expected_motion_trace=((10, 20, 10, 20),),
    )
    try:
        assert not guardian.observe(_event("Motion", 1, target=0x700101, root=(11, 20)))
        assert not guardian.armed
    finally:
        guardian.close()
