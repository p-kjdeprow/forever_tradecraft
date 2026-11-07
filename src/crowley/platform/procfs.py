"""Linux procfs process identity and guarded process-group signaling."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class ProcfsError(RuntimeError):
    """The process identity contract could not be read or verified."""


class ProcessIdentityMismatchError(ProcfsError):
    """The PID no longer identifies the expected process."""


_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class ProcessIdentity:
    """PID identity bound to Linux start ticks and the current boot."""

    pid: int
    start_ticks: int
    boot_id: str


def _validate_pid(pid: int) -> None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ProcfsError("process pid must be a positive integer")


def _boot_id() -> str:
    try:
        value = _BOOT_ID_PATH.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError) as exc:
        raise ProcfsError("kernel boot identity is unavailable") from exc
    if _BOOT_ID_PATTERN.fullmatch(value) is None:
        raise ProcfsError("kernel boot identity is malformed")
    return value


def _read_stat(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text(encoding="ascii")


def read_identity(pid: int) -> ProcessIdentity:
    """Read one process identity, parsing ``comm`` at the final closing parenthesis."""

    _validate_pid(pid)
    try:
        text = _read_stat(pid)
    except (OSError, UnicodeError) as exc:
        raise ProcfsError(f"process {pid} identity is unavailable") from exc
    closing = text.rfind(")")
    if closing <= 0:
        raise ProcfsError(f"process {pid} stat record is malformed")
    fields = text[closing + 1 :].split()
    if len(fields) < 20:
        raise ProcfsError(f"process {pid} stat record is incomplete")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ProcfsError(f"process {pid} start time is malformed") from exc
    if start_ticks < 0:
        raise ProcfsError(f"process {pid} start time is malformed")
    return ProcessIdentity(pid=pid, start_ticks=start_ticks, boot_id=_boot_id())


def still_same(identity: ProcessIdentity) -> bool:
    """Return whether the PID still names the same boot-bound process."""

    try:
        current = read_identity(identity.pid)
    except ProcfsError:
        return False
    return current == identity


def signal_group(identity: ProcessIdentity, sig: int) -> None:
    """Re-read *identity* immediately before signaling its process group."""

    if isinstance(sig, bool) or not isinstance(sig, int):
        raise ProcfsError("signal must be an integer")
    try:
        current = read_identity(identity.pid)
    except ProcfsError as exc:
        raise ProcessIdentityMismatchError("process identity could not be verified before signaling") from exc
    if current != identity:
        raise ProcessIdentityMismatchError("process identity changed before signaling")
    try:
        os.killpg(identity.pid, sig)
    except OSError as exc:
        raise ProcfsError(f"process group {identity.pid} could not be signaled") from exc


__all__ = [
    "ProcessIdentity",
    "ProcessIdentityMismatchError",
    "ProcfsError",
    "read_identity",
    "signal_group",
    "still_same",
]
