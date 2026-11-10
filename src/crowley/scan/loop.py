"""The scan loop: observe, decide, act, persist, report.  One tick every 2 s."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from crowley.gui import capture as gui_capture
from crowley.gui.input_tool import InputToolError
from crowley.gui.keyboard import KeyboardError
from crowley.platform.paths import XdgDirs

from . import policy
from .act import ActError
from .observe import ObserveError
from .policy import Decision, LoopState, Observation

TICK_S = 2.0
HEARTBEAT_S = 60.0
STATE_FILE = "scan-loop.json"
STATUS_FILE = "scan-status.json"

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
    def execute(self, decision: Decision, window_xid: int | None) -> str: ...

    @property
    def launcher_running(self) -> bool: ...

    def poll_launcher(self) -> tuple[bool, str]: ...


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
        }
    return summary


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
        out: TextIO | None = None,
        clock: Callable[[], float] = time.time,
        decide: Callable[[Observation], Decision] = policy.decide,
        started_at: float | None = None,
    ) -> None:
        self._observer, self._actor = observer, actor
        self.state = state
        self._state_file, self._status_file = state_file, status_file
        self._out = out if out is not None else sys.stdout
        self._clock = clock
        self._decide = decide
        self._persisted = policy.durable_fields(state)
        self._reported = _Reported()
        self._status_key = ""
        self._status_at = 0.0
        self._started_at = clock() if started_at is None else started_at

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
        if self._actor.launcher_running:
            finished, detail = self._actor.poll_launcher()
            if finished:
                self._log("launcher_finished", detail=detail)
            human, reason = self._observer.human_active()
            self._report(
                "relaunching", "", detail, {"human_active": human, "human_reason": reason}, launcher=detail
            )
            return
        try:
            obs = self._observer.observe(self.state)
        except OPERATIONAL_ERRORS as exc:
            self._log("fault", fault_code="observe_failed", error=type(exc).__name__, detail=str(exc)[:200])
            self._report("fault", "observe_failed", type(exc).__name__, {})
            return
        decision = self._decide(obs)
        self.state = decision.state
        self._persist()
        summary = summarize(obs, self._observer.decode_error)
        performed = ""
        if decision.action not in (policy.Action.WAIT, policy.Action.HOLD_HUMAN, policy.Action.FAULT):
            try:
                performed = self._actor.execute(decision, self._observer.window_xid)
            except OPERATIONAL_ERRORS as exc:
                self._log(
                    "fault",
                    fault_code="action_failed",
                    action=str(decision.action),
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                self._report("fault", "action_failed", f"{decision.action}:{type(exc).__name__}", summary)
                return
            self._log("action", action=str(decision.action), purpose=decision.purpose, result=performed)
            if decision.arm_boot_change and performed == str(decision.action):
                self._observer.arm_boot_change()
        self._report(str(decision.action), decision.fault_code, decision.detail, summary)

    def run(
        self, *, stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        while not stop():
            began = time.monotonic()
            self.tick()
            sleep(max(0.0, TICK_S - (time.monotonic() - began)))
