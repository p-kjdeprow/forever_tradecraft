"""Human-presence detector for the scan loop, built on Mutter's IdleMonitor.

``org.gnome.Mutter.IdleMonitor.GetIdletime`` returns the milliseconds since the last
physical input anywhere in the GNOME session.  It is used instead of an XI2 observer
because:

* it ignores XTEST: our own synthetic key presses and pointer motion do not reset it,
  so no self-gesture bookkeeping or device-topology learning is needed;
* it covers native Wayland apps as well as Xwayland clients;
* it was verified live on 2026-11-09: ``xdotool key`` and ``xdotool mousemove`` left the
  counter running (it kept counting from about 5,150,000 ms).

Recorded in the assumptions table in ``docs/ARCHITECTURE.md``.

The one fail-safe: any error, timeout or unparseable reply counts as a human being active.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

PRESENCE_WINDOW_S = 300.0
QUERY_TIMEOUT_S = 2.0

_GDBUS = [
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.gnome.Mutter.IdleMonitor",
    "--object-path",
    "/org/gnome/Mutter/IdleMonitor/Core",
    "--method",
    "org.gnome.Mutter.IdleMonitor.GetIdletime",
]
_REPLY = re.compile(r"^\(uint64 (\d+),\)$")

Runner = Callable[[list[str], float], str]


def _run_gdbus(argv: list[str], timeout: float) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    return result.stdout


class Presence:
    """Answer whether a human may be active, from the session idle time."""

    def __init__(self, *, runner: Runner = _run_gdbus) -> None:
        self._runner = runner

    def idle_ms(self) -> int:
        """Milliseconds since the last physical input; raises on any failure."""

        reply = self._runner(_GDBUS, QUERY_TIMEOUT_S).strip()
        match = _REPLY.match(reply)
        if match is None:
            raise ValueError(f"unparseable GetIdletime reply: {reply!r}")
        return int(match.group(1))

    def human_active(self, window_s: float = PRESENCE_WINDOW_S) -> tuple[bool, str]:
        try:
            idle = self.idle_ms()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return True, f"idle time unavailable, assuming human active: {exc}"
        if idle < window_s * 1000:
            return True, f"physical input {idle / 1000:.1f}s ago"
        return False, f"idle for {idle / 1000:.0f}s"


def presence_ok(window_s: float = PRESENCE_WINDOW_S, *, presence: Presence | None = None) -> int:
    """Return 0 if no human activity within *window_s*, else 1."""

    active, _reason = (presence or Presence()).human_active(window_s)
    return 1 if active else 0
