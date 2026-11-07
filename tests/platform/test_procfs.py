from __future__ import annotations

import signal

import pytest

from crowley.platform import procfs
from crowley.platform.procfs import (
    ProcessIdentity,
    ProcessIdentityMismatchError,
    ProcfsError,
    read_identity,
    signal_group,
    still_same,
)


def _stat_record(comm: str, start_ticks: int) -> str:
    tail = " ".join(["S", *[str(value) for value in range(1, 19)], str(start_ticks)])
    return f"123 ({comm}){tail}"


def test_read_identity_splits_stat_at_the_last_parenthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procfs, "_read_stat", lambda _pid: _stat_record("odd ) ( command", 987654))
    monkeypatch.setattr(procfs, "_boot_id", lambda: "01234567-89ab-cdef-0123-456789abcdef")

    identity = read_identity(123)

    assert identity == ProcessIdentity(123, 987654, "01234567-89ab-cdef-0123-456789abcdef")


def test_read_identity_rejects_malformed_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procfs, "_read_stat", lambda _pid: "123 (command) S 1")

    with pytest.raises(ProcfsError, match="incomplete"):
        read_identity(123)


def test_still_same_returns_false_for_reused_or_missing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = ProcessIdentity(123, 10, "boot")
    monkeypatch.setattr(procfs, "read_identity", lambda _pid: ProcessIdentity(123, 11, "boot"))

    assert not still_same(identity)

    def missing(_pid: int) -> ProcessIdentity:
        raise ProcfsError("gone")

    monkeypatch.setattr(procfs, "read_identity", missing)
    assert not still_same(identity)


def test_signal_group_reverifies_identity_before_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = ProcessIdentity(123, 10, "boot")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(procfs, "read_identity", lambda _pid: identity)
    monkeypatch.setattr(procfs.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    signal_group(identity, signal.SIGTERM)

    assert calls == [(123, signal.SIGTERM)]


def test_signal_group_refuses_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = ProcessIdentity(123, 10, "boot")
    monkeypatch.setattr(procfs, "read_identity", lambda _pid: ProcessIdentity(123, 11, "boot"))
    monkeypatch.setattr(procfs.os, "killpg", lambda _pid, _sig: pytest.fail("killpg must not run"))

    with pytest.raises(ProcessIdentityMismatchError, match="changed"):
        signal_group(identity, signal.SIGKILL)
