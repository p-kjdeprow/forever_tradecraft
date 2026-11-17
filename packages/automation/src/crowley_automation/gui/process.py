"""Exact game process identity.

A process is the owned game when its ``comm`` (truncated to 15 bytes), uid,
argv0 and ``WINEPREFIX`` environment entry all match.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """An observed process instance, distinct across PID reuse and prefix changes."""

    pid: int
    start_ticks: int
    uid: int
    argv0: str
    wine_prefix: str


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

    def identities(self) -> tuple[ProcessIdentity, ...]:
        """Return every current exact match, ordered by PID."""

        identities: list[ProcessIdentity] = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            identity = _identity_for_pid(int(proc.name), self)
            if identity is not None:
                identities.append(identity)
        return tuple(sorted(identities, key=lambda identity: identity.pid))

    def still_matches(self, identity: ProcessIdentity) -> bool:
        """Re-read all identity components immediately before a process/window effect."""

        current = _identity_for_pid(identity.pid, self)
        return current == identity


def _start_ticks(proc: Path) -> int | None:
    try:
        raw = (proc / "stat").read_text()
    except OSError:
        return None
    # comm may contain spaces and ')' characters; Linux encloses it in the
    # first '(' and final ')' before the fixed-width fields.
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 1 :].split()
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def process_start_ticks(pid: int) -> int | None:
    """Return Linux `/proc` start ticks for the current process instance."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return _start_ticks(Path(f"/proc/{pid}"))


def _identity_for_pid(pid: int, matcher: OwnedGameMatcher) -> ProcessIdentity | None:
    proc = Path(f"/proc/{pid}")
    try:
        before = _start_ticks(proc)
        if before is None:
            return None
        if (proc / "comm").read_text().rstrip("\n") != matcher.comm:
            return None
        uid = proc.stat().st_uid
        if uid != matcher.uid:
            return None
        argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode("utf-8", errors="strict")
        if argv0 != matcher.argv0:
            return None
        environ = (proc / "environ").read_bytes().split(b"\0")
        prefix = f"WINEPREFIX={matcher.wine_prefix}".encode()
        if prefix not in environ:
            return None
        after = _start_ticks(proc)
    except (OSError, UnicodeDecodeError, PermissionError):
        return None
    if after != before:
        return None
    return ProcessIdentity(pid, before, uid, argv0, matcher.wine_prefix)


def owned_game_pids(matcher: OwnedGameMatcher) -> list[int]:
    return [identity.pid for identity in matcher.identities()]
