"""Simulation-only monotonic clock and bounded sleeper."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from crowley.platform.clock import (
    ClockDomain,
    ClockDomainError,
    Deadline,
    DeadlineClock,
    DeadlineExpiredError,
    MonotonicInstant,
)


@dataclass(slots=True)
class SimulatedClock:
    """A private monotonic domain for deterministic tests and simulations."""

    _nanoseconds: int = 0
    _domain: ClockDomain = field(default_factory=lambda: ClockDomain("simulation"), init=False)

    def __post_init__(self) -> None:
        if isinstance(self._nanoseconds, bool) or not isinstance(self._nanoseconds, int):
            raise TypeError("simulated clock nanoseconds must be an integer")
        if self._nanoseconds < 0:
            raise ValueError("simulated clock nanoseconds cannot be negative")

    @property
    def domain(self) -> ClockDomain:
        return self._domain

    def now(self) -> MonotonicInstant:
        return MonotonicInstant(self._nanoseconds, self._domain)

    def deadline_after(self, seconds: float) -> Deadline:
        return Deadline.from_instant(self.now() + seconds)

    def advance(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("simulated clock advance must be a number of seconds")
        if not math.isfinite(float(seconds)) or seconds < 0:
            raise ValueError("simulated clock advance must be finite and non-negative")
        self._nanoseconds = (self.now() + seconds).nanoseconds


class SimulatedSleeper:
    """Advance a simulation by no more than its typed deadline."""

    def __init__(self, clock: SimulatedClock) -> None:
        self._clock = clock

    def sleep(self, seconds: float, *, deadline: Deadline) -> None:
        if deadline.domain is not self._clock.domain:
            raise ClockDomainError("simulated sleep requires the simulated clock domain")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("simulated sleep duration must be a number of seconds")
        if not math.isfinite(float(seconds)) or seconds < 0:
            raise ValueError("simulated sleep duration must be finite and non-negative")
        bounded = DeadlineClock(self._clock, deadline)
        if bounded.expired():
            raise DeadlineExpiredError("simulated sleep deadline has expired")
        self._clock.advance(min(seconds, bounded.remaining()))
