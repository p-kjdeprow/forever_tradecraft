"""Transient-blocker supervisor for the session controller.

The supervisor is an IPC client only.  It polls ``status`` and, when the
controller is BLOCKED on a documented transient condition, issues the same
``resume`` an operator would (full passive readiness and reconciliation run
inside the daemon).  It never resumes PAUSED (human takeover, guardian
revocation) and never resumes a blocker that is not on the allowlist below.
It synthesizes no input of any kind.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TextIO

# Blockers recorded by the controller on a loaded host that clear on a fresh
# passive-readiness pass.  Matching is an exact ``str.startswith``; anything
# else stays BLOCKED for an operator.  Extend only after reviewing the cause.
TRANSIENT_BLOCKER_PREFIXES: tuple[str, ...] = (
    "desktop command timed out: gnome-screenshot",
    "identity observer deadline expired",
    "identity observer timed out",
    "corrupt telemetry frame: invalid pixel crop",
    "Stormwind scene or blocking UI changed during command readback",
)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_MIN_RESUME_INTERVAL_SECONDS = 45.0
DEFAULT_FAILURE_BUDGET = 5


class SupervisorError(RuntimeError):
    """The daemon answered with a shape the supervisor does not understand."""


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
        out: TextIO,
    ) -> None:
        if min_resume_interval <= 0 or failure_budget < 1:
            raise ValueError("min_resume_interval must be positive and failure_budget at least 1")
        self._request = request
        self._min_interval = min_resume_interval
        self._budget = failure_budget
        self._monotonic = monotonic
        self._out = out
        self._last_resume: float | None = None
        # Resumes issued since the controller was last seen outside BLOCKED.
        self._consecutive = 0
        self._exhausted_logged = False

    def _log(self, event: str, **fields: object) -> None:
        print(json.dumps({"event": event, **fields}, sort_keys=True), file=self._out, flush=True)

    def step(self) -> None:
        """Poll once and resume if the documented conditions all hold."""

        response = self._request({"command": "status"})
        if response.get("ok") is not True:
            self._log("status_failed", error=response.get("error"))
            return
        status = response.get("status")
        if not isinstance(status, dict):
            raise SupervisorError("status response has no status object")
        state, generation = status.get("state"), status.get("generation")
        if not isinstance(state, str) or isinstance(generation, bool) or not isinstance(generation, int):
            raise SupervisorError("status response lacks a string state and integer generation")
        if state != "BLOCKED":
            if self._consecutive:
                self._log("recovered", state=state, generation=generation, resumes=self._consecutive)
            self._consecutive = 0
            self._exhausted_logged = False
            return
        blocker = status.get("blocker")
        if not is_transient_blocker(blocker):
            self._log("ignored_blocker", blocker=blocker, generation=generation)
            return
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
        now = self._monotonic()
        if self._last_resume is not None and now - self._last_resume < self._min_interval:
            return
        self._last_resume = now
        self._consecutive += 1
        result = self._request({"command": "resume", "generation": generation})
        self._log(
            "resume",
            ok=result.get("ok") is True,
            error=result.get("error"),
            blocker=blocker,
            generation=generation,
            attempt=self._consecutive,
            budget=self._budget,
        )


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
