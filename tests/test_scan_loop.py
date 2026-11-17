from __future__ import annotations

import io
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crowley_automation.desktop_input import ActError
from crowley_automation.gui.process import ProcessIdentity
from crowley_host.ops import health as health_ops
from crowley_host.scan import loop, observe, policy
from crowley_host.scan.act import ActionNotAdmitted, MaintenanceBlocked
from crowley_host.scan.observe import ObserveError
from crowley_host.scan.policy import Action, CommandTx, Decision, LoopState, Observation, Step

from tests.ops.test_health import NOW as HEALTH_NOW
from tests.ops.test_health import _health as health_result
from tests.support.scan_frames import automation_snapshot, snapshot
from tests.test_scan_observe import TARGET


def observation(
    state: LoopState,
    *,
    chat: str = "",
    ah_open: bool | None = None,
    zone: str | None = None,
    **kw: Any,
) -> Observation:
    scanner = snapshot(1)
    game = automation_snapshot(1)
    game["chatEdit"] = {"active": bool(chat), "focused": bool(chat), "text": chat}
    if ah_open is not None:
        game["ahOpen"] = ah_open
    if zone is not None:
        game["context"]["zone"] = zone
    snap = observe.to_snapshot(scanner, game, 1000.0)
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
        process_identity=ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix"),
        window_xid=77,
        window_fullscreen=True,
        session_fingerprint="session-1",
        native_ready=True,
        native_boot=snap.boot,
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
        self.observations: list[Observation] = []
        self.recovery: FakeRecovery | None = None

    def execute(self, decision: Decision, obs: Observation) -> str:
        self.executed.append(decision)
        self.observations.append(obs)
        if isinstance(self.result, Exception):
            raise self.result
        if decision.action is Action.RELAUNCH and self.recovery is not None:
            self.recovery.requests.append((obs.process_identity, decision.detail))
            return "relaunch_started"
        return self.result or str(decision.action)


class FakeRecovery:
    def __init__(self) -> None:
        self.statuses: list[Any] = []
        self.closed = False
        self.requests: list[tuple[ProcessIdentity | None, str]] = []

    @property
    def active(self) -> bool:
        return False

    def step(self) -> Any:
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def close(self) -> None:
        self.closed = True

    def request_restart(self, identity: ProcessIdentity, reason: str) -> Any:
        self.requests.append((identity, reason))
        from types import SimpleNamespace

        return SimpleNamespace(
            action="wm_close_dispatched", outcome="waiting", reason="normal_window_close_dispatched"
        )


def ready_status(identity: ProcessIdentity | None = None, xid: int | None = 77) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        outcome="ready",
        phase="world_ready",
        reason="native_world_ready",
        action="none",
        started_at=900.0,
        deadline_at=None,
        retry_at=None,
        native_ready=True,
        process_identity=identity or ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix"),
        window_xid=xid,
    )


def startup_status(outcome: str, phase: str, reason: str) -> Any:
    return SimpleNamespace(
        outcome=outcome,
        phase=phase,
        reason=reason,
        action="none",
        started_at=900.0,
        deadline_at=1200.0,
        retry_at=905.0,
        native_ready=False,
        process_identity=None,
        window_xid=None,
    )


class Time:
    now = 5_000.0

    def __call__(self) -> float:
        return self.now


def make(
    tmp_path: Path,
    state: LoopState | None = None,
    maintenance_blocker: Any = lambda: None,
) -> tuple[loop.ScanLoop, FakeObserver, FakeActor, io.StringIO, Time]:
    observer, actor, out, clock, recovery = FakeObserver(), FakeActor(), io.StringIO(), Time(), FakeRecovery()
    recovery.statuses = [ready_status()]
    actor.recovery = recovery
    scan = loop.ScanLoop(
        observer=observer,
        actor=actor,
        state=state or LoopState(),
        state_file=tmp_path / "state" / "scan-loop.json",
        status_file=tmp_path / "run" / "scan-status.json",
        recovery=recovery,
        out=out,
        clock=clock,
        maintenance_blocker=maintenance_blocker,
    )
    return scan, observer, actor, out, clock


def events(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_state_roundtrip_is_atomic_private_and_only_written_on_change(tmp_path: Path) -> None:
    scan, observer, _actor, _out, _clock = make(tmp_path)
    state_file = tmp_path / "state" / "scan-loop.json"
    observer.obs = [observation(LoopState(), last_frame_at=None)]
    scan.tick()  # the first verified identity is bound durably
    assert state_file.exists()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    saved = json.loads(state_file.read_text())
    assert saved["observed_process_pid"] == 111 and saved["observed_process_start_ticks"] == 10_000
    restored = loop.load_state(state_file)
    assert policy.durable_fields(restored) == saved
    assert not list(state_file.parent.glob(".*.tmp"))
    mtime = state_file.stat().st_mtime_ns
    scan.tick()  # same identity: nothing durable changed
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


def test_automation_ledger_guard_holds_before_policy_and_reports_only_stable_reason(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path, maintenance_blocker=lambda: "automation_running")
    observer.obs = [observation(LoopState(), chat="private text")]
    scan.tick()
    assert actor.executed == []
    assert events(out)[-1]["action"] == "wait"
    assert events(out)[-1]["detail"] == "automation_running"
    raw = (tmp_path / "run" / "scan-status.json").read_text()
    assert "private text" not in raw
    status = json.loads(raw)
    assert status["action"] == "wait" and status["detail"] == "automation_running"


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

    def execute(decision: Decision, obs: Observation) -> str:
        seen.append(state_file.exists())
        return str(decision.action)

    actor.execute = execute  # type: ignore[method-assign]
    # A submitted /reload arms the decoder only when the key was actually sent.
    fake = Decision(Action.PRESS_RETURN, LoopState(), arm_boot_change=True)
    scan._decide = lambda obs: fake
    observer.obs = [observation(LoopState(), last_frame_at=None)]
    scan.tick()
    assert seen == [True]
    assert observer.armed == 1
    actor.execute = lambda d, obs: "skipped:human_active"  # type: ignore[method-assign]
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
    actor.result = ActError("input missing")
    scan._decide = lambda obs: Decision(Action.PRESS_RETURN, obs.state)
    observer.obs = [observation(LoopState())]
    scan.tick()
    fault = next(e for e in events(out) if e["event"] == "fault")
    assert fault["fault_code"] == "action_failed" and fault["action"] == "press_return"


def test_inner_lease_guard_converts_a_raced_action_to_wait_and_restores_state(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    actor.result = MaintenanceBlocked("automation_uncertain")
    scan._decide = lambda obs: Decision(Action.PRESS_RETURN, obs.state)
    observer.obs = [observation(LoopState())]
    scan.tick()
    assert scan.state == policy.bind_process_identity(
        LoopState(), ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix")
    )
    assert events(out)[-1]["action"] == "wait"
    assert events(out)[-1]["detail"] == "automation_uncertain"
    status = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert status["action"] == "wait" and status["detail"] == "automation_uncertain"


def test_restart_attempt_is_committed_only_after_guarded_close_dispatch(tmp_path: Path) -> None:
    scan, observer, actor, _out, _clock = make(tmp_path)
    original = LoopState(
        observed_process_pid=111,
        observed_process_start_ticks=10_000,
        relaunch_attempts=1,
        last_relaunch_at=100.0,
    )
    scan.state = original
    scan._persisted = policy.durable_fields(original)
    actor.result = ActionNotAdmitted("fresh_native_world_required_for_restart")
    scan._decide = lambda obs: Decision(
        Action.RELAUNCH,
        replace(obs.state, relaunch_attempts=2, last_relaunch_at=obs.wall),
        detail="no_progress",
    )
    observer.obs = [observation(original)]

    scan.tick()

    assert scan.state == original
    assert not (tmp_path / "state" / "scan-loop.json").exists()


@pytest.mark.parametrize("failure", ["ah_failed", "no_progress"])
def test_consumed_failure_is_reset_for_new_verified_identity_and_ah_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(policy, "AH_FAILED_RELAUNCH_S", 30.0)
    monkeypatch.setattr(policy, "NO_PROGRESS_S", 30.0)
    old_identity = ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix")
    new_identity = ProcessIdentity(222, 20_000, 1000, "C:\\Wow.exe", "/prefix")
    initial = LoopState(
        last_relaunch_at=99.0,
        relaunch_attempts=2,
        last_ack_reload_at=200.0,
        flush_uid="flush-receipt",
        flush_boot="old-boot",
        flush_at=300.0,
        flush_seen_at=400.0,
        ah_attempts=(990.0,),
        ah_failed_at=950.0 if failure == "ah_failed" else None,
        last_new_sweep_at=900.0,
        no_progress_stage=1 if failure == "no_progress" else 0,
        no_progress_at=950.0 if failure == "no_progress" else 0.0,
        observed_process_pid=old_identity.pid,
        observed_process_start_ticks=old_identity.start_ticks,
        ah_stage="interact_sent",
        ah_stage_at=990.0,
        ah_open_since=900.0,
    )
    scan, observer, actor, _out, _clock = make(tmp_path, state=initial)
    recovery = scan._recovery
    recovery.statuses = [ready_status(old_identity), ready_status(new_identity, 88)]
    observer.obs = [
        observation(
            initial,
            ah_open=failure == "no_progress",
            process_identity=old_identity,
        ),
        observation(
            initial,
            ah_open=False,
            zone="Stormwind City",
            process_identity=new_identity,
            window_xid=88,
        ),
    ]

    scan.tick()  # old client receives one exact restart request for the consumed failure
    assert actor.executed, events(_out)
    assert actor.executed[-1].action is Action.RELAUNCH
    assert recovery.requests == [
        (old_identity, "ah_open_failed" if failure == "ah_failed" else "no_progress")
    ]
    scan.tick()  # new independently verified identity enters the AH opening flow

    assert actor.executed[-1].action is Action.OPEN_CHAT
    assert actor.executed[-1].command == f"/targetexact {TARGET.auctioneer}"
    assert scan.state.observed_process_pid == new_identity.pid
    assert scan.state.observed_process_start_ticks == new_identity.start_ticks
    assert scan.state.ah_failed_at is None
    assert scan.state.ah_attempts == (1000.0,)  # a new attempt begins; the old failure window is gone
    assert scan.state.no_progress_stage == 0
    assert scan.state.ah_stage == "targeting"
    assert scan.state.tx is not None and scan.state.tx.command == f"/targetexact {TARGET.auctioneer}"
    assert scan.state.last_ack_reload_at == initial.last_ack_reload_at
    assert scan.state.flush_uid == initial.flush_uid
    assert scan.state.flush_boot == initial.flush_boot
    assert scan.state.flush_at == initial.flush_at
    assert scan.state.flush_seen_at == initial.flush_seen_at
    assert scan.state.relaunch_attempts == 3
    assert scan.state.last_relaunch_at == 1000.0


def test_startup_recovery_holds_before_observation_or_actions(tmp_path: Path) -> None:
    scan, observer, actor, out, _clock = make(tmp_path)
    recovery = scan._recovery
    recovery.statuses = [
        type(ready_status())(
            outcome="waiting",
            phase="login",
            reason="credentials_required",
            action="none",
            started_at=1.0,
            deadline_at=100.0,
            retry_at=3.0,
            native_ready=False,
            process_identity=None,
            window_xid=None,
        )
    ]
    observer.obs = [observation(LoopState())]
    scan.tick()
    assert observer.states == []
    assert actor.executed == []
    status = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert status["action"] == "startup_waiting"
    assert status["observation"]["startup"]["retry_at"] == 3.0


@pytest.mark.parametrize(
    ("outcome", "phase", "reason", "expected_action"),
    [
        ("waiting", "battle_net_login", "battle_net_login_required", "startup_waiting"),
        ("held", "ownership", "gui_busy", "startup_held"),
        ("held", "desktop", "desktop_session_changed", "startup_held"),
        (
            "held",
            "ownership",
            "automation_maintenance_or_uncertain",
            "startup_held",
        ),
    ],
)
def test_startup_wait_and_nonhuman_holds_are_visible_without_health_faults(
    tmp_path: Path, outcome: str, phase: str, reason: str, expected_action: str
) -> None:
    scan, _observer, _actor, _out, clock = make(tmp_path)
    clock.now = HEALTH_NOW.timestamp() - 10
    scan._recovery.statuses = [startup_status(outcome, phase, reason)]

    scan.tick()

    generated = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert generated["action"] == expected_action
    assert generated["fault_code"] == ""
    assert generated["detail"] == reason
    assert generated["observation"]["startup"]["phase"] == phase
    assert generated["observation"]["startup"]["reason"] == reason
    bounded = health_ops._bounded_status(generated)
    assert health_ops._status_issues(bounded, HEALTH_NOW) == []
    assert health_result(tmp_path, status=generated)["classification"] == "operational"


@pytest.mark.parametrize("outcome", ["blocked", "failed"])
def test_blocked_or_failed_startup_remains_a_health_fault(tmp_path: Path, outcome: str) -> None:
    scan, _observer, _actor, _out, clock = make(tmp_path)
    clock.now = HEALTH_NOW.timestamp() - 10
    scan._recovery.statuses = [
        startup_status(outcome, "character_select", "ui_unrecognized_no_input_authorized")
    ]

    scan.tick()

    generated = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    expected_fault = f"startup_{outcome}"
    assert generated["action"] == expected_fault
    assert generated["fault_code"] == expected_fault
    assert generated["observation"]["startup"]["phase"] == "character_select"
    bounded = health_ops._bounded_status(generated)
    assert health_ops._status_issues(bounded, HEALTH_NOW) == [f"scan_fault_{expected_fault}"]
    result = health_result(tmp_path, status=generated)
    assert result["classification"] == "attention"
    assert f"scan_fault_{expected_fault}" in result["issues"]


def test_startup_physical_presence_hold_uses_existing_health_grace(tmp_path: Path) -> None:
    scan, observer, _actor, _out, clock = make(tmp_path)
    clock.now = HEALTH_NOW.timestamp() - 60
    scan._recovery.statuses = [
        startup_status("held", "ownership", "physical_presence_hold"),
        ready_status(),
    ]
    observer.obs = [observation(LoopState())]
    scan._decide = lambda obs: Decision(Action.WAIT, obs.state, detail="test_wait")
    stale_uploads = {"uid": int(HEALTH_NOW.timestamp()) - health_ops.UPLOAD_MAX_AGE_SECONDS - 1}

    scan.tick()
    held = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert held["action"] == "hold_human"
    assert held["fault_code"] == ""
    assert held["detail"] == policy.HUMAN_HOLD_DETAIL
    assert held["observation"]["startup"]["phase"] == "ownership"
    assert held["observation"]["startup"]["reason"] == "physical_presence_hold"
    assert health_result(tmp_path, status=held, uploads=stale_uploads)["classification"] == "gated"

    clock.now = HEALTH_NOW.timestamp() - 30
    scan.tick()
    ready = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert ready["action"] == "wait"
    assert ready["last_hold_ended_at"] == clock.now
    assert ready["startup"]["outcome"] == "ready"
    result = health_result(tmp_path, status=ready, uploads=stale_uploads)
    assert result["classification"] == "operational"
    assert "uploads_stale" not in result["issues"]


def test_startup_hold_retires_chat_transaction_and_preserves_durable_receipts(
    tmp_path: Path,
) -> None:
    identity = ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix")
    state = LoopState(
        last_relaunch_at=900.0,
        relaunch_attempts=2,
        last_ack_reload_at=850.0,
        flush_uid="saved-1",
        flush_boot="boot-before",
        flush_at=800.0,
        flush_seen_at=810.0,
        last_new_sweep_at=790.0,
        observed_process_pid=identity.pid,
        observed_process_start_ticks=identity.start_ticks,
        tx=CommandTx("/reload", "save_flush", Step.TYPED, at=49.0, typed_at=999.0),
    )
    scan, observer, actor, _out, _clock = make(tmp_path, state=state)
    original_durable = policy.durable_fields(state)
    scan._last_ready_evidence = (identity, 77, "session-1", "automation-boot-1")
    waiting = type(ready_status())(
        outcome="held",
        phase="desktop",
        reason="desktop_session_changed",
        action="none",
        started_at=1.0,
        deadline_at=100.0,
        retry_at=3.0,
        native_ready=False,
        process_identity=identity,
        window_xid=77,
    )
    scan._recovery.statuses = [waiting, ready_status(identity)]
    observer.obs = [observation(state, chat="/reload", session_fingerprint="session-2")]

    scan.tick()
    assert scan.state.tx is None and scan.state.tx_cooldown_until == 0.0
    assert policy.durable_fields(scan.state) == original_durable
    scan.tick()
    assert all(item.action is not Action.PRESS_RETURN for item in actor.executed)
    assert policy.durable_fields(scan.state) == original_durable


@pytest.mark.parametrize(
    ("hold", "observation_values", "maintenance"),
    [
        (Action.HOLD_HUMAN, {"human_active": True}, None),
        (Action.WAIT, {"native_automation_busy": True}, None),
        (Action.WAIT, {}, "automation_running"),
    ],
)
def test_policy_ownership_holds_retire_chat_transaction_without_losing_history(
    tmp_path: Path,
    hold: Action,
    observation_values: dict[str, Any],
    maintenance: str | None,
) -> None:
    identity = ProcessIdentity(111, 10_000, 1000, "C:\\Wow.exe", "/prefix")
    state = LoopState(
        last_relaunch_at=900.0,
        relaunch_attempts=2,
        last_ack_reload_at=850.0,
        flush_uid="saved-1",
        flush_boot="boot-before",
        flush_at=800.0,
        flush_seen_at=810.0,
        last_new_sweep_at=790.0,
        observed_process_pid=identity.pid,
        observed_process_start_ticks=identity.start_ticks,
        tx=CommandTx("/reload", "save_flush", Step.TYPED, at=49.0, typed_at=999.0),
        tx_cooldown_until=123.0,
    )
    blocker = (lambda: maintenance) if maintenance is not None else (lambda: None)
    scan, observer, actor, _out, _clock = make(tmp_path, state=state, maintenance_blocker=blocker)
    original_durable = policy.durable_fields(state)
    observer.obs = [observation(state, **observation_values)]

    scan.tick()

    assert actor.executed == []
    assert scan.state.tx is None and scan.state.tx_cooldown_until == 0.0
    assert policy.durable_fields(scan.state) == original_durable
    status = json.loads((tmp_path / "run" / "scan-status.json").read_text())
    assert status["action"] == ("hold_human" if hold is Action.HOLD_HUMAN else "wait")


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
    assert scan._recovery.closed


def test_status_records_when_a_human_hold_ended(tmp_path: Path) -> None:
    scan, observer, _actor, _out, clock = make(tmp_path)
    status_file = tmp_path / "run" / "scan-status.json"
    observer.obs = [observation(LoopState(), human_active=True, human_reason="keyboard")]
    scan.tick()
    status = json.loads(status_file.read_text())
    assert status["action"] == "hold_human" and "last_hold_ended_at" not in status
    clock.now += 30
    observer.obs = [observation(LoopState())]
    scan.tick()
    assert json.loads(status_file.read_text())["last_hold_ended_at"] == clock.now
