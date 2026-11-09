"""Transient-blocker supervisor for the session controller.

The supervisor is an IPC client only.  It polls ``status`` and, when the
controller is BLOCKED on a documented transient condition, issues the same
``resume`` an operator would (full passive readiness and reconciliation run
inside the daemon).  It never resumes PAUSED (human takeover, guardian
revocation) and never resumes a blocker that is not on the allowlist below.
It synthesizes no input of any kind.

When the controller stays BLOCKED (any blocker) or the daemon stops answering
for a long time, the supervisor can also relaunch the whole game stack through
an injected runner: stop the session daemon, terminate the owned game, run the
launcher, start the session daemon, then resume once.  PAUSED and STOPPED are
operator states and never trigger it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TextIO

# Blockers recorded by the controller on a loaded host that clear on a fresh
# passive-readiness pass.  Matching is an exact ``str.startswith``; anything
# else stays BLOCKED for an operator.  Extend only after reviewing the cause.
TRANSIENT_BLOCKER_PREFIXES: tuple[str, ...] = (
    "desktop command timed out: gnome-screenshot",
    "identity observer deadline expired",
    "identity observer timed out",
    # The same observer timeouts when they happen during automatic restart startup.
    "automatic restart startup failed: identity observer deadline expired",
    "automatic restart startup failed: identity observer timed out",
    "corrupt telemetry frame: invalid pixel crop",
    "Stormwind scene or blocking UI changed during command readback",
    # Re-opening the AH after the ack reload missed its deadline on an overloaded
    # host; a fresh RECONCILE -> REOPEN pass succeeds once load drops.
    "reopen deadline expired",
    # Reload acknowledgement missed its deadline after a successful upload on a
    # loaded host; resume goes to RECONCILE, which clears the reload intent.
    # Exact states only: "deadline expired in PAUSED/STOPPED" must stay excluded.
    "deadline expired in SAVE_RELOAD_SENT",
    "deadline expired in ACK_RELOAD_SENT",
)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_MIN_RESUME_INTERVAL_SECONDS = 45.0
DEFAULT_FAILURE_BUDGET = 5
DEFAULT_RELAUNCH_THRESHOLD_SECONDS = 600.0
RELAUNCH_MIN_INTERVAL_SECONDS = 1800.0
RELAUNCH_BUDGET = 3
PROGRESS_STATES = frozenset({"SCANNING", "WAIT_DUE"})
SESSION_UNIT = "crowley-session.service"
LAUNCHER_NAME = "crowley-world-of-warcraft"
LAUNCHER_TIMEOUT_SECONDS = 600.0


class SupervisorError(RuntimeError):
    """The daemon answered with a shape the supervisor does not understand."""


class RelaunchRunner(Protocol):
    """Process side of a relaunch; every method raises on failure."""

    def stop_session(self) -> None: ...
    def terminate_game(self) -> int: ...
    def run_launcher(self) -> None: ...
    def start_session(self) -> None: ...


class SystemRelaunchRunner:
    """systemd, /proc and the installed launcher, using the launcher's identity rule."""

    def __init__(
        self,
        *,
        game_executable: str,
        game_argv0: str,
        wine_prefix: str,
        term_wait: float = 20.0,
        kill_wait: float = 10.0,
    ) -> None:
        self._comm = game_executable[:15]  # /proc/PID/comm is truncated to 15 bytes
        self._argv0 = game_argv0
        self._prefix = wine_prefix
        self._uid = os.getuid()
        self._term_wait = term_wait
        self._kill_wait = kill_wait

    @staticmethod
    def _systemctl(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=120, check=False)

    def stop_session(self) -> None:
        result = self._systemctl("stop", SESSION_UNIT)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl stop failed: {result.stderr.decode(errors='replace').strip()}")

    def start_session(self) -> None:
        result = self._systemctl("start", SESSION_UNIT)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl start failed: {result.stderr.decode(errors='replace').strip()}")

    def _owned_game_pids(self) -> list[int]:
        pids: list[int] = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if (proc / "comm").read_text().rstrip("\n") != self._comm:
                    continue
                if proc.stat().st_uid != self._uid:
                    continue
                argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
                if argv0 != self._argv0:
                    continue
                environ = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue  # exited, or not ours to read
            if f"WINEPREFIX={self._prefix}".encode() in environ:
                pids.append(int(proc.name))
        return pids

    def _signal_all(self, sig: signal.Signals) -> list[int]:
        pids = self._owned_game_pids()
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        return pids

    def _wait_gone(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while self._owned_game_pids():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
        return True

    def terminate_game(self) -> int:
        pids = self._signal_all(signal.SIGTERM)
        if pids and not self._wait_gone(self._term_wait):
            self._signal_all(signal.SIGKILL)
            if not self._wait_gone(self._kill_wait):
                raise RuntimeError("owned game process survived SIGKILL")
        return len(pids)

    def run_launcher(self) -> None:
        launcher = shutil.which(LAUNCHER_NAME)
        if launcher is None:
            raise RuntimeError(f"{LAUNCHER_NAME} is not on PATH")
        subprocess.run([launcher], check=True, timeout=LAUNCHER_TIMEOUT_SECONDS)


# Blockers that name explicit operator reconciliation are never resumed or
# relaunched automatically.
OPERATOR_BLOCKER_MARKERS: tuple[str, ...] = ("explicit reconciliation required", "unrecoverable")


def requires_operator(blocker: object) -> bool:
    return not isinstance(blocker, str) or any(marker in blocker for marker in OPERATOR_BLOCKER_MARKERS)


def is_transient_blocker(blocker: object) -> bool:
    return isinstance(blocker, str) and blocker.startswith(TRANSIENT_BLOCKER_PREFIXES)


class Supervisor:
    def __init__(
        self,
        request: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        min_resume_interval: float = DEFAULT_MIN_RESUME_INTERVAL_SECONDS,
        failure_budget: int = DEFAULT_FAILURE_BUDGET,
        monotonic: Callable[[], float] = time.monotonic,
        relaunch: RelaunchRunner | None = None,
        relaunch_threshold: float = DEFAULT_RELAUNCH_THRESHOLD_SECONDS,
        relaunch_min_interval: float = RELAUNCH_MIN_INTERVAL_SECONDS,
        relaunch_budget: int = RELAUNCH_BUDGET,
        out: TextIO,
    ) -> None:
        if min_resume_interval <= 0 or failure_budget < 1:
            raise ValueError("min_resume_interval must be positive and failure_budget at least 1")
        if relaunch_threshold <= 0 or relaunch_min_interval <= 0 or relaunch_budget < 1:
            raise ValueError("relaunch threshold and interval must be positive and budget at least 1")
        self._request = request
        self._min_interval = min_resume_interval
        self._budget = failure_budget
        self._monotonic = monotonic
        self._out = out
        self._last_resume: float | None = None
        # Resumes issued since the controller was last seen outside BLOCKED.
        self._consecutive = 0
        self._exhausted_logged = False
        self._relaunch = relaunch
        self._relaunch_threshold = relaunch_threshold
        self._relaunch_min_interval = relaunch_min_interval
        self._relaunch_budget = relaunch_budget
        self._blocked_key: tuple[int, object] | None = None
        self._blocked_since: float | None = None
        self._last_relaunch: float | None = None
        # Relaunches since the controller last reached SCANNING or WAIT_DUE.
        self._relaunches = 0
        self._relaunch_exhausted_logged = False
        self._resume_after_relaunch = False

    def _log(self, event: str, **fields: object) -> None:
        print(json.dumps({"event": event, **fields}, sort_keys=True), file=self._out, flush=True)

    def step(self) -> None:
        """Poll once and resume or relaunch if the documented conditions all hold."""

        response = self._request({"command": "status"})
        now = self._monotonic()
        if response.get("ok") is not True:
            # An unanswered status says nothing about the durable state: the
            # daemon may be starting over a PAUSED human takeover.  Relaunch is
            # decided only from an observed BLOCKED status.
            self._log("status_failed", error=response.get("error"))
            return
        status = response.get("status")
        if not isinstance(status, dict):
            raise SupervisorError("status response has no status object")
        state, generation = status.get("state"), status.get("generation")
        if not isinstance(state, str) or isinstance(generation, bool) or not isinstance(generation, int):
            raise SupervisorError("status response lacks a string state and integer generation")
        if state in PROGRESS_STATES:
            self._relaunches = 0
            self._relaunch_exhausted_logged = False
        if state != "BLOCKED":
            # The one-shot post-relaunch resume belongs to the first status after
            # the relaunch only; PAUSED/STOPPED or progress consumes it.
            self._resume_after_relaunch = False
            self._blocked_key = self._blocked_since = None
            if self._consecutive:
                self._log("recovered", state=state, generation=generation, resumes=self._consecutive)
            self._consecutive = 0
            self._exhausted_logged = False
            return
        blocker = status.get("blocker")
        key = (generation, blocker)
        if key != self._blocked_key:
            self._blocked_key, self._blocked_since = key, now
        if self._resume_after_relaunch:
            # The relaunched session is blocked on the pre-relaunch blocker; one
            # resume with the current generation re-runs readiness and reconcile.
            self._resume_after_relaunch = False
            if not requires_operator(blocker):
                self._resume(generation, blocker, now, reason="post_relaunch")
                return
        if is_transient_blocker(blocker):
            self._resume_transient(generation, blocker, now)
        else:
            self._log("ignored_blocker", blocker=blocker, generation=generation)
        if (
            self._blocked_since is not None
            and now - self._blocked_since >= self._relaunch_threshold
            and not requires_operator(blocker)
        ):
            self._maybe_relaunch("blocked", now, blocker=blocker)

    def _resume_transient(self, generation: int, blocker: object, now: float) -> None:
        if self._consecutive >= self._budget:
            if not self._exhausted_logged:
                self._exhausted_logged = True
                self._log(
                    "budget_exhausted",
                    blocker=blocker,
                    generation=generation,
                    resumes=self._consecutive,
                    note="persistent fault; operator action required",
                )
            return
        if self._last_resume is not None and now - self._last_resume < self._min_interval:
            return
        self._consecutive += 1
        self._resume(generation, blocker, now)

    def _resume(self, generation: int, blocker: object, now: float, **extra: object) -> None:
        self._last_resume = now
        result = self._request({"command": "resume", "generation": generation})
        self._log(
            "resume",
            ok=result.get("ok") is True,
            error=result.get("error"),
            blocker=blocker,
            generation=generation,
            attempt=self._consecutive,
            budget=self._budget,
            **extra,
        )

    def _maybe_relaunch(self, reason: str, now: float, **fields: object) -> None:
        runner = self._relaunch
        if runner is None:
            return
        if self._relaunches >= self._relaunch_budget:
            if not self._relaunch_exhausted_logged:
                self._relaunch_exhausted_logged = True
                self._log(
                    "relaunch_budget_exhausted",
                    relaunches=self._relaunches,
                    note="relaunches made no scanning progress; operator action required",
                )
            return
        if self._last_relaunch is not None and now - self._last_relaunch < self._relaunch_min_interval:
            return
        self._last_relaunch = now
        self._relaunches += 1
        # Each attempt restarts both clocks, whatever its outcome.
        self._blocked_key = self._blocked_since = None
        self._log(
            "relaunch_start", reason=reason, attempt=self._relaunches, budget=self._relaunch_budget, **fields
        )
        try:
            try:
                runner.stop_session()
                self._log("relaunch_step", step="stop_session")
                killed = runner.terminate_game()
                self._log("relaunch_step", step="terminate_game", processes=killed)
                runner.run_launcher()
                self._log("relaunch_step", step="run_launcher")
            finally:
                # Never leave the daemon stopped: with it down nothing can observe
                # or retry, and systemd does not restart a stopped unit.
                runner.start_session()
                self._log("relaunch_step", step="start_session")
        except Exception as exc:  # noqa: BLE001 - any step failure ends the attempt, loudly
            self._log("relaunch_failed", error=f"{type(exc).__name__}: {exc}")
            return
        self._resume_after_relaunch = True
        self._log("relaunch_done", attempt=self._relaunches)


def run(
    supervisor: Supervisor,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
) -> None:
    count = 0
    while iterations is None or count < iterations:
        supervisor.step()
        count += 1
        sleep(poll_seconds)
