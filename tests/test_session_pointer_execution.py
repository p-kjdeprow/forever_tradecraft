from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from crowley.gui import click_gate
from crowley.session import pointer_execution as pointer_exec
from crowley.session.executor import BindingMetadata, Point, PointerButton, PointerTarget, Rect


def _binding() -> BindingMetadata:
    return BindingMetadata("identity", "capture", 1, 1, 17, (17,))


def _target(rect: Rect | None = None) -> PointerTarget:
    return PointerTarget("construct", rect or Rect(80, 60, 100, 30), _binding(), padding=2)


def _reserve_then_wait(directory: str, ready: multiprocessing.connection.Connection) -> None:
    with pointer_exec.acquire_click_rate_permit() as permit:
        permit.precommit()
        ready.send("GO_BOUNDARY_REACHED")
        time.sleep(30)


def test_seeded_human_pointer_plan_replays_and_varies_with_bounded_submovements() -> None:
    target = _target()
    bounds = Rect(0, 0, 400, 300)
    first = pointer_exec.plan_human_pointer(Point(20, 20), target, bounds, seed=0x1234)
    replay = pointer_exec.plan_human_pointer(Point(20, 20), target, bounds, seed=0x1234)
    changed = pointer_exec.plan_human_pointer(Point(20, 20), target, bounds, seed=0x5678)

    assert first == replay
    assert first.click is not None and first.click.button is PointerButton.LEFT
    assert target.safe_bounds.contains(first.endpoint)
    assert first.trajectory.samples[0].point == Point(20, 20)
    assert first.trajectory.samples[-1].point == first.endpoint
    assert len(first.waypoints) >= 1
    assert len(set(first.waypoints)) == len(first.waypoints)
    assert all(bounds.contains(sample.point) for sample in first.trajectory.samples)
    assert first.trajectory.duration <= pointer_exec._MAX_GESTURE_DURATION
    assert first.click.dwell > 0
    assert (
        changed.endpoint != first.endpoint
        or changed.waypoints != first.waypoints
        or changed.click.dwell != first.click.dwell
    )


def test_already_at_target_and_edge_target_keep_exact_endpoint_inside_client() -> None:
    bounds = Rect(0, 0, 40, 30)
    tiny = PointerTarget("edge", Rect(0, 0, 1, 1), _binding(), padding=0)
    plan = pointer_exec.plan_human_pointer(Point(0, 0), tiny, bounds, seed=5)
    assert plan.endpoint == Point(0, 0)
    assert len(plan.waypoints) >= 2
    assert plan.trajectory.samples[-1].point == Point(0, 0)
    assert all(bounds.contains(sample.point) for sample in plan.trajectory.samples)
    assert len({sample.point for sample in plan.trajectory.samples}) > 2


def test_native_motion_argv_is_positive_bounded_and_rejects_warp() -> None:
    plan = pointer_exec.plan_human_pointer(Point(20, 20), _target(), Rect(0, 0, 400, 300), seed=9)
    action = pointer_exec.native_pointer_motion_action(plan.trajectory, (10, 20, 400, 300))
    duration = pointer_exec.validate_pointer_motion_action(action)
    assert duration == pytest.approx(plan.movement_duration)
    assert pointer_exec.pointer_action_timeout(action) > duration
    assert action[0] == "sleep"
    assert action.count("mousemove") >= 2
    with pytest.raises(pointer_exec.PointerExecutionError):
        pointer_exec.validate_pointer_motion_action(("mousemove", "--sync", "40", "50"))


def test_failed_durable_precommit_refuses_before_click_go(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr("crowley.gui.click_gate._session_state_directory", lambda: state_dir)
    permit = pointer_exec.acquire_click_rate_permit()
    monkeypatch.setattr(
        "crowley.gui.click_gate._write_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(pointer_exec.PointerExecutionError("disk full")),
    )
    try:
        with pytest.raises(pointer_exec.PointerExecutionError, match="disk full"):
            permit.precommit()
        assert not (state_dir / "pointer-click-rate.json").exists()
    finally:
        permit.close()


def test_deadline_after_durable_precommit_keeps_reservation_without_press(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr("crowley.gui.click_gate._session_state_directory", lambda: state_dir)
    original_write_state = click_gate._write_state
    boundary_entered = threading.Event()
    deadline: float

    def slow_write_state(directory, state):  # type: ignore[no-untyped-def]
        original_write_state(directory, state)
        boundary_entered.set()
        while time.monotonic() < deadline:
            time.sleep(0.005)

    monkeypatch.setattr("crowley.gui.click_gate._write_state", slow_write_state)
    permit = pointer_exec.acquire_click_rate_permit()
    deadline = time.monotonic() + 0.25
    try:
        with pytest.raises(pointer_exec.PointerExecutionError, match="after durable precommit"):
            permit.precommit(deadline_monotonic=deadline)
        assert boundary_entered.is_set()
        state = click_gate._read_state(state_dir)
        assert state is not None
        assert state["lastPressMonotonicNs"] is None
        assert state["nextAllowedMonotonicNs"] > time.monotonic_ns()
        with pytest.raises(pointer_exec.PointerExecutionError, match="cannot be recorded"):
            permit.record_press()
    finally:
        permit.close()


def test_boot_identity_change_resets_only_the_old_monotonic_domain(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr("crowley.gui.click_gate._session_state_directory", lambda: state_dir)
    monkeypatch.setattr(
        "crowley.gui.click_gate._boot_identity", lambda: "11111111-1111-1111-1111-111111111111"
    )
    click_gate._write_state(
        state_dir,
        {
            "version": 1,
            "bootId": "00000000-0000-0000-0000-000000000000",
            "nextAllowedMonotonicNs": time.monotonic_ns() + 8_000_000_000,
            "lastPressMonotonicNs": time.monotonic_ns(),
        },
    )
    start = time.monotonic()
    with pointer_exec.acquire_click_rate_permit() as permit:
        assert permit.is_open
    assert time.monotonic() - start < 0.5


def test_process_death_after_go_boundary_preserves_spacing_for_next_owner(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "runtime"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr("crowley.gui.click_gate._session_state_directory", lambda: state_dir)
    # Keep the non-configurable one-second click interval while making the
    # conservative pre-press latency reserve short enough for this test.
    monkeypatch.setattr("crowley.gui.click_gate._GO_LATENCY_RESERVE_NS", 50_000_000)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    owner = context.Process(target=_reserve_then_wait, args=(str(state_dir), child))
    owner.start()
    child.close()
    try:
        assert parent.poll(3.0)
        assert parent.recv() == "GO_BOUNDARY_REACHED"
        state = click_gate._read_state(state_dir)
        assert state is not None
        assert state["nextAllowedMonotonicNs"] > time.monotonic_ns()
        owner.kill()
        owner.join(timeout=3.0)
        assert not owner.is_alive()
        started = time.monotonic()
        with pointer_exec.acquire_click_rate_permit() as next_owner:
            assert next_owner.is_open
        assert time.monotonic() - started >= 0.95
    finally:
        if owner.is_alive():
            owner.kill()
            owner.join(timeout=3.0)
        parent.close()
