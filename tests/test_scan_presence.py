from __future__ import annotations

import subprocess

import pytest

from crowley.scan import presence as p


def fixed(reply: str) -> p.Runner:
    def run(argv: list[str], timeout: float) -> str:
        assert argv[0] == "gdbus" and "GetIdletime" in argv[-1]
        assert timeout == p.QUERY_TIMEOUT_S
        return reply

    return run


def raising(exc: Exception) -> p.Runner:
    def run(argv: list[str], timeout: float) -> str:
        raise exc

    return run


def test_idle_long_is_not_active() -> None:
    active, reason = p.Presence(runner=fixed("(uint64 5150000,)\n")).human_active()
    assert not active and "5150" in reason


def test_idle_short_is_active() -> None:
    active, reason = p.Presence(runner=fixed("(uint64 1200,)\n")).human_active()
    assert active and "1.2s" in reason


def test_boundary() -> None:
    assert p.Presence(runner=fixed("(uint64 299999,)")).human_active(300.0)[0]
    assert not p.Presence(runner=fixed("(uint64 300000,)")).human_active(300.0)[0]


@pytest.mark.parametrize(
    "runner",
    [
        raising(subprocess.CalledProcessError(1, "gdbus", stderr="boom")),
        raising(subprocess.TimeoutExpired("gdbus", 2.0)),
        raising(FileNotFoundError("gdbus")),
        fixed("garbage"),
        fixed("(uint64 -5,)"),
        fixed(""),
    ],
)
def test_errors_count_as_active(runner: p.Runner) -> None:
    active, reason = p.Presence(runner=runner).human_active()
    assert active and "assuming human active" in reason


def test_presence_ok() -> None:
    assert p.presence_ok(300.0, presence=p.Presence(runner=fixed("(uint64 400000,)"))) == 0
    assert p.presence_ok(300.0, presence=p.Presence(runner=fixed("(uint64 10,)"))) == 1
    assert p.presence_ok(300.0, presence=p.Presence(runner=fixed("nope"))) == 1
