"""The scan loop: observe, decide, act, persist, report.  One tick every 2 s."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

from crowley_automation.desktop_input import ActError
from crowley_automation.gui import capture as gui_capture
from crowley_automation.gui.input_tool import InputToolError
from crowley_automation.gui.keyboard import KeyboardError
from crowley_automation.gui.process import ProcessIdentity
from crowley_common.platform.paths import XdgDirs

from . import policy
from .act import ActionNotAdmitted, MaintenanceBlocked
from .observe import ObserveError
from .policy import HUMAN_HOLD_DETAIL, Decision, LoopState, Observation

TICK_S = 2.0
HEARTBEAT_S = 60.0
STATE_FILE = "scan-loop.json"
STATUS_FILE = "scan-status.json"
POLICY_EVIDENCE_HOLDS = frozenset(
    {
        "automation_running",
        "automation_uncertain",
        "automation_state_invalid",
        "automation_native_busy",
        "startup_recovery",
        "window_invalid",
        "no_fresh_frame",
        "frame_not_fresh",
    }
)

# Operational failures: reported as a fault for that tick, no input sent.  Anything else is
# a bug and ends the process (systemd restarts it) so it cannot be hidden by the loop.
OPERATIONAL_ERRORS = (
    ObserveError,
    ActError,
    InputToolError,
    KeyboardError,
    gui_capture.CaptureError,
    OSError,
    subprocess.SubprocessError,
    ValueError,
)


class StateError(RuntimeError):
    """scan-loop.json exists but cannot be restored."""


def state_path() -> Path:
    return XdgDirs.from_environ(os.environ).crowley_state / STATE_FILE


def status_path() -> Path:
    return XdgDirs.from_environ(os.environ).crowley_runtime / STATUS_FILE


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: directory 0700, file 0600."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_state(path: Path) -> LoopState:
    """Missing file: fresh state.  Corrupt or mismatched file: fail loudly."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoopState()
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return policy.restore(data)
    except (ValueError, TypeError) as exc:
        raise StateError(f"corrupt scan loop state {path}: {exc}") from exc


class Observing(Protocol):
    def observe(self, state: LoopState) -> Observation: ...

    def arm_boot_change(self) -> None: ...

    @property
    def decode_error(self) -> str: ...

    @property
    def window_xid(self) -> int | None: ...

    def human_active(self) -> tuple[bool, str]: ...


class Acting(Protocol):
    def execute(self, decision: Decision, observation: Observation) -> str: ...


class StartupStatusView(Protocol):
    outcome: Literal["ready", "waiting", "held", "blocked", "failed", "stopped"]
    phase: str
    reason: str
    action: str
    started_at: float
    deadline_at: float | None
    retry_at: float | None
    native_ready: bool
    process_identity: ProcessIdentity | None
    window_xid: int | None


class Recovering(Protocol):
    @property
    def active(self) -> bool: ...

    def step(self) -> Any: ...

    def close(self) -> None: ...


def summarize(obs: Observation, decode_error: str) -> dict[str, Any]:
    """Observation summary for status: facts only, never chat text or frame contents."""

    snap = obs.snapshot
    summary: dict[str, Any] = {
        "process_alive": obs.process_alive,
        "window_ok": obs.window_ok,
        "window_focused": obs.window_focused,
        "frames_seen": obs.frames_seen_this_process,
        "last_frame_at": obs.last_frame_at,
        "human_active": obs.human_active,
        "human_reason": obs.human_reason,
        "native_automation_busy": obs.native_automation_busy,
        "native_ready": obs.native_ready,
        "native_boot": obs.native_boot,
        "process_identity": (
            {
                "pid": obs.process_identity.pid,
                "start_ticks": obs.process_identity.start_ticks,
                "uid": obs.process_identity.uid,
            }
            if obs.process_identity is not None
            else None
        ),
        "window_xid": obs.window_xid,
        "window_fullscreen": obs.window_fullscreen,
        "session_fingerprint": obs.session_fingerprint,
        "decode_error": decode_error,
        "snapshot": None,
    }
    if snap is not None:
        summary["snapshot"] = {
            "received_at": snap.received_at,
            "phase": snap.phase,
            "ah_open": snap.ah_open,
            "zone": snap.zone,
            "last_saved_uid": snap.last_saved_uid,
            "pending_count": snap.pending_count,
            "pending_blocked": snap.pending_blocked,
            "blocking_ui": snap.blocking_ui,
            "chat_active": snap.chat_active,
            "due_at": snap.due_at,
            "automation_busy": snap.automation_busy,
        }
    return summary


def summarize_startup(status: StartupStatusView) -> dict[str, Any]:
    """Typed startup progress without OCR, account or chat contents."""

    identity = status.process_identity
    return {
        "outcome": status.outcome,
        "phase": status.phase,
        "reason": status.reason,
        "action": status.action,
        "started_at": status.started_at,
        "deadline_at": status.deadline_at,
        "retry_at": status.retry_at,
        "native_ready": status.native_ready,
        "process_identity": (
            {"pid": identity.pid, "start_ticks": identity.start_ticks, "uid": identity.uid}
            if identity is not None
            else None
        ),
        "window_xid": status.window_xid,
    }


@dataclass
class _Reported:
    signature: tuple[str, str, str] = ("", "", "")


class ScanLoop:
    def __init__(
        self,
        *,
        observer: Observing,
        actor: Acting,
        state: LoopState,
        state_file: Path,
        status_file: Path,
        recovery: Recovering,
        out: TextIO | None = None,
        clock: Callable[[], float] = time.time,
        decide: Callable[[Observation], Decision] = policy.decide,
        started_at: float | None = None,
        maintenance_blocker: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._observer, self._actor, self._recovery = observer, actor, recovery
        self.state = state
        self._state_file, self._status_file = state_file, status_file
        self._out = out if out is not None else sys.stdout
        self._clock = clock
        self._decide = decide
        self._maintenance_blocker = maintenance_blocker
        self._persisted = policy.durable_fields(state)
        self._reported = _Reported()
        self._status_key = ""
        self._status_at = 0.0
        self._held = False
        self._last_hold_ended_at: float | None = None
        self._last_ready_evidence: tuple[ProcessIdentity, int, str, str] | None = None
        self._started_at = clock() if started_at is None else started_at

    def _retire_chat_transaction(self) -> None:
        """Forget only the process-local chat token across any lost evidence boundary."""

        if self.state.tx is not None or self.state.tx_cooldown_until:
            self.state = replace(self.state, tx=None, tx_cooldown_until=0.0)

    def _retire_ready_evidence(self) -> None:
        self._retire_chat_transaction()
        self._last_ready_evidence = None

    def _retire_policy_hold(self, decision: Decision) -> None:
        if decision.action in {policy.Action.HOLD_HUMAN, policy.Action.FAULT} or (
            decision.action is policy.Action.WAIT and decision.detail in POLICY_EVIDENCE_HOLDS
        ):
            self._retire_chat_transaction()

    def _scanner_evidence_ready(self, obs: Observation) -> bool:
        snap = obs.snapshot
        if (
            self._observer.decode_error
            or snap is None
            or obs.frames_seen_this_process <= 0
            or obs.last_frame_at is None
            or obs.native_boot != snap.boot
        ):
            return False
        age = obs.wall - obs.last_frame_at
        return -1.0 <= age < policy.STALE_FRAME_S

    def _log(self, event: str, **fields: Any) -> None:
        record = {"ts": round(self._clock(), 3), "event": event, **fields}
        self._out.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._out.flush()

    def _persist(self) -> None:
        durable = policy.durable_fields(self.state)
        if durable != self._persisted:
            write_private_json(self._state_file, durable)
            self._persisted = durable

    def _write_status(self, doc: dict[str, Any]) -> None:
        key = json.dumps(doc, sort_keys=True)
        now = self._clock()
        if key == self._status_key and now - self._status_at < HEARTBEAT_S:
            return
        write_private_json(self._status_file, {**doc, "updated_at": now, "started_at": self._started_at})
        self._status_key, self._status_at = key, now

    def _report(
        self, action: str, fault_code: str, detail: str, summary: dict[str, Any], **extra: Any
    ) -> None:
        held = action == "hold_human" or (action == "relaunching" and bool(summary.get("human_active")))
        if self._held and not held:
            self._last_hold_ended_at = self._clock()
        self._held = held
        if self._last_hold_ended_at is not None:
            extra = {**extra, "last_hold_ended_at": self._last_hold_ended_at}
        signature = (action, fault_code, detail)
        if signature != self._reported.signature:
            self._reported.signature = signature
            self._log("decision", action=action, fault_code=fault_code, detail=detail)
        self._write_status(
            {
                "action": action,
                "fault_code": fault_code,
                "detail": detail,
                "observation": summary,
                "pid": os.getpid(),
                **extra,
            }
        )

    def tick(self) -> None:
        try:
            startup = self._recovery.step()
        except OPERATIONAL_ERRORS as exc:
            self._retire_ready_evidence()
            self._log("fault", fault_code="startup_failed", error=type(exc).__name__)
            self._report("startup_failed", "startup_failed", type(exc).__name__, {})
            return
        startup_data = summarize_startup(startup)
        if self._recovery.active or startup.outcome != "ready" or not startup.native_ready:
            self._retire_ready_evidence()
            action = f"startup_{startup.outcome}"
            fault_code = ""
            detail = startup.reason
            if startup.outcome == "held" and startup.reason == "physical_presence_hold":
                action = "hold_human"
                detail = HUMAN_HOLD_DETAIL
            elif startup.outcome in {"blocked", "failed"}:
                fault_code = f"startup_{startup.outcome}"
            self._report(
                action,
                fault_code,
                detail,
                {"startup": startup_data},
            )
            return
        try:
            obs = self._observer.observe(self.state)
        except OPERATIONAL_ERRORS as exc:
            self._retire_ready_evidence()
            self._log("fault", fault_code="observe_failed", error=type(exc).__name__, detail=str(exc)[:200])
            self._report("fault", "observe_failed", type(exc).__name__, {}, startup=startup_data)
            return
        if (
            obs.process_identity is None
            or obs.process_identity != startup.process_identity
            or obs.window_xid is None
            or obs.window_xid != startup.window_xid
            or not obs.window_ok
            or not obs.window_fullscreen
            or not obs.native_ready
        ):
            self._retire_ready_evidence()
            self._report(
                "startup_reobserve",
                "startup_identity_changed",
                "startup_identity_changed",
                {"startup": startup_data},
            )
            return
        if not self._scanner_evidence_ready(obs):
            self._retire_ready_evidence()
        else:
            assert obs.process_identity is not None and obs.window_xid is not None
            evidence = (obs.process_identity, obs.window_xid, obs.session_fingerprint, obs.native_boot)
            if self._last_ready_evidence is not None and evidence != self._last_ready_evidence:
                self._retire_chat_transaction()
            self._last_ready_evidence = evidence
        self.state = policy.bind_process_identity(self.state, obs.process_identity)
        obs = replace(obs, state=self.state)
        self._persist()
        try:
            blocker = self._maintenance_blocker()
        except (OSError, RuntimeError, ValueError):
            blocker = "automation_state_invalid"
        if blocker is not None and blocker not in {
            "automation_running",
            "automation_uncertain",
            "automation_state_invalid",
        }:
            blocker = "automation_state_invalid"
        obs = replace(obs, maintenance_blocker=blocker or "")
        decision = self._decide(obs)
        prior_state = self.state
        self.state = decision.state
        self._retire_policy_hold(decision)
        if decision.action is not policy.Action.RELAUNCH:
            self._persist()
        summary = summarize(obs, self._observer.decode_error)
        performed = ""
        if decision.action not in (policy.Action.WAIT, policy.Action.HOLD_HUMAN, policy.Action.FAULT):
            try:
                performed = self._actor.execute(decision, obs)
            except MaintenanceBlocked as exc:
                self.state = prior_state
                self._retire_chat_transaction()
                self._persist()
                self._report("wait", "", str(exc), summary, startup=startup_data)
                return
            except ActionNotAdmitted as exc:
                self.state = prior_state
                if exc.reason not in {"gui_busy", "stopping"}:
                    self._retire_chat_transaction()
                self._persist()
                self._report("wait", "", exc.reason, summary, startup=startup_data)
                return
            except OPERATIONAL_ERRORS as exc:
                if decision.action is policy.Action.RELAUNCH:
                    self.state = prior_state
                    self._persist()
                self._log(
                    "fault",
                    fault_code="action_failed",
                    action=str(decision.action),
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                self._report(
                    "fault",
                    "action_failed",
                    f"{decision.action}:{type(exc).__name__}",
                    summary,
                    startup=startup_data,
                )
                return
            if decision.action is policy.Action.RELAUNCH:
                if performed != "relaunch_started":
                    self.state = prior_state
                    self._persist()
                    self._report(
                        "wait",
                        "",
                        "restart_not_dispatched",
                        summary,
                        startup=startup_data,
                    )
                    return
                self._persist()
            self._log("action", action=str(decision.action), purpose=decision.purpose, result=performed)
            if decision.arm_boot_change and performed == str(decision.action):
                self._observer.arm_boot_change()
        self._report(
            str(decision.action),
            decision.fault_code,
            decision.detail,
            summary,
            startup=startup_data,
        )

    def run(
        self, *, stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        try:
            while not stop():
                began = time.monotonic()
                self.tick()
                sleep(max(0.0, TICK_S - (time.monotonic() - began)))
        finally:
            try:
                close_actor = getattr(self._actor, "close", None)
                if close_actor is not None:
                    close_actor()
            finally:
                self._recovery.close()
