from __future__ import annotations

import io
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from crowley.scan import loop, observe, policy
from crowley.scan.act import ActError
from crowley.scan.observe import ObserveError
from crowley.scan.policy import Action, Decision, LoopState, Observation
from tests.support.scan_frames import snapshot
from tests.test_scan_observe import TARGET


def observation(state: LoopState, *, chat: str = "", **kw: Any) -> Observation:
    value = snapshot(1)
    value["chatEdit"] = {"active": bool(chat), "focused": bool(chat), "text": chat}
    snap = observe.to_snapshot(value, 1000.0)
    base: dict[str, Any] = dict(
        wall=1000.0,
        mono=50.0,
        process_alive=True,
        process_started_at=900.0,
        window_ok=True,
        window_focused=True,
        snapshot=snap,
        last_frame_at=1000.0,
        frames_seen_this_process=3,
        disk_saved_ids=frozenset(),
        uploaded_ids=frozenset(),
        permanently_rejected_ids=frozenset(),
        data_acked_ids=frozenset(),
        companion_last_error="",
        human_active=False,
        human_reason="idle",
        target=TARGET,
        state=state,
    )
    return Observation(**{**base, **kw})


class FakeObserver:
    def __init__(self) -> None:
        self.obs: list[Any] = []
        self.armed = 0
        self.decode_error = ""
        self.window_xid: int | None = 77
        self.states: list[LoopState] = []

    def observe(self, state: LoopState) -> Observation:
        self.states.append(state)
        item = self.obs.pop(0) if len(self.obs) > 1 else self.obs[0]
        if isinstance(item, Exception):
            raise item
        return replace(item, state=state)

    def arm_boot_change(self) -> None:
        self.armed += 1

    def human_active(self) -> tuple[bool, str]:
        return False, "idle"


class FakeActor:
    def __init__(self) -> None:
        self.executed: list[Decision] = []
        self.result: str | Exception = ""
        self.launcher_running = False
        self.polls: list[tuple[bool, str]] = []

    def execute(self, decision: Decision, window_xid: int | None) -> str:
        self.executed.append(decision)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or str(decision.action)

    def poll_launcher(self) -> tuple[bool, str]:
        finished, detail = self.polls.pop(0)
        self.launcher_running = not finished
        return finished, detail


class Time:
    now = 5_000.0

    def __call__(self) -> float:
        return self.now


def make(
    tmp_path: Path, state: LoopState | None = None
) -> tuple[loop.ScanLoop, FakeObserver, FakeActor, io.StringIO, Time]:
    observer, actor, out, clock = FakeObserver(), FakeActor(), io.StringIO(), Time()
    scan = loop.ScanLoop(
        observer=observer,
        actor=actor,
        state=state or LoopState(),
        state_file=tmp_path / "state" / "scan-loop.json",
        status_file=tmp_path / "run" / "scan-status.json",
        out=out,
        clock=clock,
    )
    return scan, observer, actor, out, clock


def events(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_state_roundtrip_is_atomic_private_and_only_written_on_change(tmp_path: Path) -> None:
    scan, observer, _actor, _out, _clock = make(tmp_path)
    state_file = tmp_path / "state" / "scan-loop.json"
    # Snapshot is mostly idle: a healthy frame changes only the not-durable parts + window_bad_since.
    observer.obs = [observation(LoopState(), last_frame_at=None, process_alive=False)]
    scan.tick()  # no process -> relaunch -> durable change
    assert state_file.exists()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    saved = json.loads(state_file.read_text())
    assert saved["relaunch_attempts"] == 1 and saved["last_relaunch_at"] == 1000.0
    restored = loop.load_state(state_file)
    assert policy.durable_fields(restored) == saved
    assert not list(state_file.parent.glob(".*.tmp"))
    mtime = state_file.stat().st_mtime_ns
    scan.tick()  # relaunch backoff wait: nothing durable changed
    assert state_file.stat().st_mtime_ns == mtime


def test_missing_state_file_is_fresh_and_corrupt_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "scan-loop.json"
    assert loop.load_state(path) == LoopState()
    for bad in (
        "not json",
        "[]",
        json.dumps({"relaunch_attempts": 1}),
        json.dumps({**policy.durable_fields(LoopState()), "extra": 1}),
    ):
        path.write_text(bad)
        with pytest.raises(loop.StateError, match="corrupt"):
            loop.load_state(path)


def test_status_file_has_decision_and_no_chat_text_or_frame(tmp_path: Path) -> None:
    scan, observer, _actor, _out, _clock = make(tmp_path)
    secret = "/targetexact Zorro"
    observer.obs = [observation(LoopState(), chat=secret)]
    scan.tick()
    status_file = tmp_path / "run" / "scan-status.json"
    raw = status_file.read_text()
    assert secret not in raw and "Zorro" not in raw
    status = json.loads(raw)
    assert stat.S_IMODE(status_file.stat().st_mode) == 0o600
    assert status["fault_code"] == "chat_foreign_text" and status["detail"] == f"len={len(secret)}"
    assert status["observation"]["snapshot"]["chat_active"] is True
    assert status["observation"]["process_alive"] and status["pid"] == os.getpid()
    assert {"updated_at", "started_at", "action"} <= set(status)
    assert "chat_text" not in raw and "payload" not in raw


def test_status_writes_on_change_and_heartbeat_only(tmp_path: Path) -> None:
    scan, observer, _actor, _out, clock = make(tmp_path)
    observer.obs = [observation(LoopState())]
    scan.tick()
    status_file = tmp_path / "run" / "scan-status.json"
    first = status_file.stat().st_mtime_ns
    clock.now += 10
    scan.tick()
    assert status_file.stat().st_mtime_ns == first
    clock.now += loop.HEARTBEAT_S
    scan.tick()
    assert json.loads(status_file.read_text())["updated_at"] == clock.now


def test_decision_changes_and_actions_are_logged_once(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    observer.obs = [observation(LoopState(), snapshot=None, last_frame_at=None, frames_seen_this_process=0)]
    scan.tick()
    scan.tick()
    decisions = [e for e in events(out) if e["event"] == "decision"]
    assert (
        len(decisions) == 1
        and decisions[0]["action"] == "wait"
        and decisions[0]["detail"] == "no_fresh_frame"
    )
    assert actor.executed == []


def test_action_is_executed_after_state_is_persisted_and_boot_change_armed(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    state_file = tmp_path / "state" / "scan-loop.json"
    seen: list[bool] = []

    def execute(decision: Decision, window_xid: int | None) -> str:
        seen.append(state_file.exists())
        return str(decision.action)

    actor.execute = execute  # type: ignore[method-assign]
    observer.obs = [observation(LoopState(), process_alive=False, last_frame_at=None)]
    scan.tick()
    assert seen == [True]  # relaunch counters hit disk before the relaunch itself
    # A submitted /reload arms the decoder only when the key was actually sent.
    fake = Decision(Action.PRESS_RETURN, LoopState(), arm_boot_change=True)
    scan._decide = lambda obs: fake
    actor.execute = lambda d, x: str(d.action)  # type: ignore[method-assign]
    scan.tick()
    assert observer.armed == 1
    actor.execute = lambda d, x: "skipped:human_active"  # type: ignore[method-assign]
    scan.tick()
    assert observer.armed == 1
    assert any(e["event"] == "action" for e in events(out))


@pytest.mark.parametrize("error", [ObserveError("x"), OSError("y"), ValueError("z")])
def test_operational_observe_errors_are_faults_not_crashes(tmp_path: Path, error: Exception) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    observer.obs = [error]
    scan.tick()
    assert actor.executed == []
    fault = next(e for e in events(out) if e["event"] == "fault")
    assert fault["fault_code"] == "observe_failed"
    assert json.loads((tmp_path / "run" / "scan-status.json").read_text())["fault_code"] == "observe_failed"


def test_unexpected_errors_propagate(tmp_path: Path) -> None:
    scan, observer, _actor, _out, _clock = make(tmp_path)
    observer.obs = [RuntimeError("bug")]
    with pytest.raises(RuntimeError, match="bug"):
        scan.tick()


def test_action_failure_is_reported_and_the_loop_continues(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    actor.result = ActError("launcher missing")
    observer.obs = [observation(LoopState(), process_alive=False, last_frame_at=None)]
    scan.tick()
    fault = next(e for e in events(out) if e["event"] == "fault")
    assert fault["fault_code"] == "action_failed" and fault["action"] == "relaunch"


def test_ticks_only_poll_the_launcher_while_it_runs(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    actor.launcher_running = True
    actor.polls = [(False, "running"), (True, "exit=0")]
    observer.obs = [observation(LoopState())]
    scan.tick()
    scan.tick()
    assert observer.states == []  # no observation, no decision, no input while launching
    assert any(e["event"] == "launcher_finished" for e in events(out))
    status = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert status["action"] == "relaunching" and status["observation"]["human_active"] is False


def test_run_ticks_until_stopped(tmp_path: Path) -> None:
    scan, observer, _actor, _out, _clock = make(tmp_path)
    observer.obs = [observation(LoopState())]
    sleeps: list[float] = []
    count = 0

    def stop() -> bool:
        nonlocal count
        count += 1
        return count > 3

    scan.run(stop=stop, sleep=sleeps.append)
    assert len(sleeps) == 3 and all(0 <= s <= loop.TICK_S for s in sleeps)
    assert len(observer.states) == 3
