"""The owned game process rule and a bounded terminate.

A process is the owned game when its ``comm`` (truncated to 15 bytes), uid,
argv0 and ``WINEPREFIX`` environment entry all match.  The launcher script
applies the same rule in shell (``owned_game_running``).
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OwnedGameMatcher:
    """Identity rule for the one game process this user owns."""

    comm: str
    argv0: str
    wine_prefix: str
    uid: int

    @classmethod
    def for_game(cls, *, game_executable: str, game_argv0: str, wine_prefix: str) -> OwnedGameMatcher:
        # /proc/PID/comm is truncated to 15 bytes.
        return cls(game_executable[:15], game_argv0, wine_prefix, os.getuid())

    def pids(self) -> list[int]:
        return owned_game_pids(self)


def owned_game_pids(matcher: OwnedGameMatcher) -> list[int]:
    pids: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "comm").read_text().rstrip("\n") != matcher.comm:
                continue
            if proc.stat().st_uid != matcher.uid:
                continue
            argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if argv0 != matcher.argv0:
                continue
            environ = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue  # exited, or not ours to read
        if f"WINEPREFIX={matcher.wine_prefix}".encode() in environ:
            pids.append(int(proc.name))
    return pids


def owned_game_running(matcher: OwnedGameMatcher) -> bool:
    return bool(owned_game_pids(matcher))


def pid_alive(pid: int) -> bool:
    """True while the pid exists and is not a zombie."""

    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat_text.rsplit(")", 1)[1].split()[0] != "Z"


def terminate(
    pids: Iterable[int],
    *,
    term_wait: float = 20.0,
    kill_wait: float = 10.0,
    alive: Callable[[int], bool] = pid_alive,
) -> int:
    """SIGTERM, wait, SIGKILL, wait; raise if any pid is still alive. Returns the pid count."""

    targets = list(pids)

    def signal_all(sig: signal.Signals) -> None:
        for pid in targets:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def wait_gone(seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while any(alive(pid) for pid in targets):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
        return True

    if targets:
        signal_all(signal.SIGTERM)
        if not wait_gone(term_wait):
            signal_all(signal.SIGKILL)
            if not wait_gone(kill_wait):
                raise RuntimeError("owned game process survived SIGKILL")
    return len(targets)
