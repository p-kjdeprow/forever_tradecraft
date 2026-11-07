from __future__ import annotations

import pytest

from crowley.platform.clock import (
    SYSTEM_MONOTONIC_CLOCK,
    ClockDomainError,
    DeadlineClock,
    DeadlineExpiredError,
    SystemMonotonicClock,
    SystemSleeper,
)
from tests.support.clock import SimulatedClock, SimulatedSleeper


def test_values_reject_cross_domain_comparison_and_deadline_construction() -> None:
    simulated = SimulatedClock()
    system_instant = SYSTEM_MONOTONIC_CLOCK.now()
    simulated_instant = simulated.now()
    simulated_deadline = simulated.deadline_after(1.0)

    with pytest.raises(ClockDomainError, match="clock domain"):
        _ = system_instant < simulated_instant
    with pytest.raises(ClockDomainError, match="clock domain"):
        _ = system_instant == simulated_instant
    with pytest.raises(ClockDomainError, match="clock domain"):
        _ = SYSTEM_MONOTONIC_CLOCK.deadline_after(1.0) - simulated_instant
    with pytest.raises(ClockDomainError, match="clock domain"):
        DeadlineClock(SYSTEM_MONOTONIC_CLOCK, simulated_deadline)


def test_deadline_child_intersection_only_shrinks() -> None:
    clock = SimulatedClock()
    action = DeadlineClock.after(clock, 10.0)
    later = action.child(clock.deadline_after(20.0))
    earlier = action.child(clock.deadline_after(3.0))

    assert later.deadline == action.deadline
    assert earlier.deadline < action.deadline
    assert (action.deadline - earlier.deadline) == pytest.approx(7.0)


def test_simulated_remaining_and_expired_are_in_the_simulated_domain() -> None:
    clock = SimulatedClock()
    action = DeadlineClock.after(clock, 5.0)

    assert action.remaining() == pytest.approx(5.0)
    assert not action.expired()
    clock.advance(2.25)
    assert action.remaining() == pytest.approx(2.75)
    clock.advance(2.75)
    assert action.expired()
    assert action.remaining() == 0.0


def test_system_clock_is_monotonic_and_singleton() -> None:
    assert SystemMonotonicClock() is SYSTEM_MONOTONIC_CLOCK
    first = SYSTEM_MONOTONIC_CLOCK.now()
    second = SYSTEM_MONOTONIC_CLOCK.now()

    assert second >= first

    with pytest.raises(TypeError, match="cannot be subclassed"):

        class _FakeSystemClock(SystemMonotonicClock):
            pass

    for replacement in (None, object()):
        with pytest.raises(AttributeError, match="singleton state is immutable"):
            SystemMonotonicClock._instance = replacement
    assert SystemMonotonicClock() is SYSTEM_MONOTONIC_CLOCK


def test_deadline_clock_cannot_be_reassigned() -> None:
    clock = SimulatedClock()
    action = DeadlineClock.after(clock, 10.0)

    with pytest.raises(AttributeError, match="DeadlineClock is immutable"):
        action._deadline = clock.deadline_after(20.0)
    with pytest.raises(AttributeError, match="DeadlineClock is immutable"):
        action._base = SimulatedClock()


def test_simulated_clock_rejects_backward_advance() -> None:
    clock = SimulatedClock()
    initial = clock.now()

    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(-0.1)

    assert clock.now() == initial


def test_simulated_sleeper_never_advances_past_deadline() -> None:
    clock = SimulatedClock()
    sleeper = SimulatedSleeper(clock)
    deadline = clock.deadline_after(1.0)

    sleeper.sleep(5.0, deadline=deadline)

    assert clock.now() == deadline.instant
    with pytest.raises(DeadlineExpiredError, match="deadline has expired"):
        sleeper.sleep(0.1, deadline=deadline)


def test_system_sleeper_rejects_simulated_deadline() -> None:
    simulated = SimulatedClock()

    with pytest.raises(ClockDomainError, match="clock domain"):
        SystemSleeper().sleep(0.01, deadline=simulated.deadline_after(1.0))


def test_system_clock_binding_cannot_be_replaced() -> None:
    import crowley.platform.clock as clock_module

    with pytest.raises(AttributeError, match="immutable"):
        clock_module.SYSTEM_MONOTONIC_CLOCK = SystemMonotonicClock()
