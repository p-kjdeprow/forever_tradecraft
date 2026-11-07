"""Typed monotonic time capabilities for Crowley's safety boundaries.

Safety, process supervision, IPC timeouts, and click-rate enforcement use the
protected system monotonic domain.  An action shares one :class:`DeadlineClock`
and its injected :class:`Sleeper`; simulated clocks belong only in tests.  A
clock domain is never converted or used as a fallback for another domain.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, runtime_checkable

_NANOSECONDS_PER_SECOND = 1_000_000_000


class ClockDomainError(TypeError):
    """Raised when values from different monotonic clock domains interact."""


class DeadlineExpiredError(TimeoutError):
    """Raised when a bounded sleep is requested after its deadline."""


@dataclass(frozen=True, slots=True, eq=False)
class ClockDomain:
    """Opaque identity for one source of monotonic time.

    ``eq=False`` is intentional: two domains with the same diagnostic name
    remain different domains.  Values compare domains by object identity, not
    by a label that could accidentally be shared by a fake and a real clock.
    """

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("clock domain name must be a non-empty string")


def _validate_nanoseconds(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer nanosecond value")
    if value < 0:
        raise ValueError(f"{label} cannot be negative")
    return value


def _duration_nanoseconds(seconds: float, label: str) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError(f"{label} must be a number of seconds")
    if not math.isfinite(float(seconds)):
        raise ValueError(f"{label} must be finite")
    if seconds < 0:
        raise ValueError(f"{label} cannot be negative")
    return round(float(seconds) * _NANOSECONDS_PER_SECOND)


def _signed_duration_nanoseconds(seconds: float, label: str) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError(f"{label} must be a number of seconds")
    if not math.isfinite(float(seconds)):
        raise ValueError(f"{label} must be finite")
    return round(float(seconds) * _NANOSECONDS_PER_SECOND)


def _domain_error(operation: str, expected: ClockDomain, actual: ClockDomain) -> ClockDomainError:
    return ClockDomainError(f"{operation} requires clock domain {expected.name!r}, got {actual.name!r}")


@dataclass(frozen=True, slots=True, eq=False)
class MonotonicInstant:
    """A monotonic timestamp tied to one exact :class:`ClockDomain`."""

    nanoseconds: int
    domain: ClockDomain

    def __post_init__(self) -> None:
        _validate_nanoseconds(self.nanoseconds, "monotonic instant")
        if not isinstance(self.domain, ClockDomain):
            raise TypeError("monotonic instant domain must be a ClockDomain")

    def _require_same_domain(self, other: MonotonicInstant, operation: str) -> None:
        if self.domain is not other.domain:
            raise _domain_error(operation, self.domain, other.domain)

    def _with_nanoseconds(self, nanoseconds: int) -> MonotonicInstant:
        return MonotonicInstant(nanoseconds, self.domain)

    def __add__(self, seconds: float) -> MonotonicInstant:
        delta = _signed_duration_nanoseconds(seconds, "instant offset")
        return self._with_nanoseconds(_validate_nanoseconds(self.nanoseconds + delta, "resulting instant"))

    def __sub__(self, other: MonotonicInstant | float) -> float | MonotonicInstant:
        if isinstance(other, MonotonicInstant):
            self._require_same_domain(other, "instant subtraction")
            return (self.nanoseconds - other.nanoseconds) / _NANOSECONDS_PER_SECOND
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            delta = _signed_duration_nanoseconds(other, "instant offset")
            return self._with_nanoseconds(
                _validate_nanoseconds(self.nanoseconds - delta, "resulting instant")
            )
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MonotonicInstant):
            return NotImplemented
        self._require_same_domain(other, "instant comparison")
        return self.nanoseconds == other.nanoseconds

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, MonotonicInstant):
            return NotImplemented
        self._require_same_domain(other, "instant comparison")
        return self.nanoseconds < other.nanoseconds

    def __le__(self, other: object) -> bool:
        if not isinstance(other, MonotonicInstant):
            return NotImplemented
        self._require_same_domain(other, "instant comparison")
        return self.nanoseconds <= other.nanoseconds

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, MonotonicInstant):
            return NotImplemented
        self._require_same_domain(other, "instant comparison")
        return self.nanoseconds > other.nanoseconds

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, MonotonicInstant):
            return NotImplemented
        self._require_same_domain(other, "instant comparison")
        return self.nanoseconds >= other.nanoseconds

    def __hash__(self) -> int:
        return hash((self.domain, self.nanoseconds))


@dataclass(frozen=True, slots=True, eq=False)
class Deadline:
    """An absolute monotonic deadline tied to one exact clock domain."""

    nanoseconds: int
    domain: ClockDomain

    def __post_init__(self) -> None:
        _validate_nanoseconds(self.nanoseconds, "deadline")
        if not isinstance(self.domain, ClockDomain):
            raise TypeError("deadline domain must be a ClockDomain")

    @classmethod
    def from_instant(cls, instant: MonotonicInstant) -> Deadline:
        """Create a deadline at ``instant`` without changing its domain."""

        return cls(instant.nanoseconds, instant.domain)

    @property
    def instant(self) -> MonotonicInstant:
        """Return this deadline as the corresponding typed instant."""

        return MonotonicInstant(self.nanoseconds, self.domain)

    def _require_same_domain(self, other: Deadline | MonotonicInstant, operation: str) -> None:
        if self.domain is not other.domain:
            raise _domain_error(operation, self.domain, other.domain)

    def _with_nanoseconds(self, nanoseconds: int) -> Deadline:
        return Deadline(nanoseconds, self.domain)

    def __add__(self, seconds: float) -> Deadline:
        delta = _signed_duration_nanoseconds(seconds, "deadline offset")
        return self._with_nanoseconds(_validate_nanoseconds(self.nanoseconds + delta, "resulting deadline"))

    def __sub__(self, other: Deadline | MonotonicInstant | float) -> float | Deadline:
        if isinstance(other, (Deadline, MonotonicInstant)):
            self._require_same_domain(other, "deadline subtraction")
            return (self.nanoseconds - other.nanoseconds) / _NANOSECONDS_PER_SECOND
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            delta = _signed_duration_nanoseconds(other, "deadline offset")
            return self._with_nanoseconds(
                _validate_nanoseconds(self.nanoseconds - delta, "resulting deadline")
            )
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Deadline):
            return NotImplemented
        self._require_same_domain(other, "deadline comparison")
        return self.nanoseconds == other.nanoseconds

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Deadline):
            return NotImplemented
        self._require_same_domain(other, "deadline comparison")
        return self.nanoseconds < other.nanoseconds

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Deadline):
            return NotImplemented
        self._require_same_domain(other, "deadline comparison")
        return self.nanoseconds <= other.nanoseconds

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Deadline):
            return NotImplemented
        self._require_same_domain(other, "deadline comparison")
        return self.nanoseconds > other.nanoseconds

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Deadline):
            return NotImplemented
        self._require_same_domain(other, "deadline comparison")
        return self.nanoseconds >= other.nanoseconds

    def __hash__(self) -> int:
        return hash((self.domain, self.nanoseconds))


@runtime_checkable
class MonotonicClock(Protocol):
    """The minimal clock capability needed to create typed deadlines."""

    @property
    def domain(self) -> ClockDomain: ...

    def now(self) -> MonotonicInstant: ...

    def deadline_after(self, seconds: float) -> Deadline: ...


class _SystemMonotonicClockMeta(type):
    """Keep singleton state out of the settable class namespace."""

    def __setattr__(cls, name: str, value: object) -> None:
        if name == "_instance":
            raise AttributeError("SystemMonotonicClock singleton state is immutable")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name == "_instance":
            raise AttributeError("SystemMonotonicClock singleton state cannot be deleted")
        super().__delattr__(name)


class SystemMonotonicClock(metaclass=_SystemMonotonicClockMeta):
    """The one real monotonic clock used by safety-critical production code.

    The class is final through ``__init_subclass__`` and its ``__new__`` method
    always returns the one instance.  The singleton is held in a protected
    module binding rather than writable class state.  The module-level binding
    is protected by the module type installed at the bottom of this module, so
    ordinary assignment or deletion cannot replace it at runtime.  As with
    every Python guard, deliberate mutation through a module ``__dict__`` or
    other object-model bypass is outside this guarantee.
    """

    __slots__ = ("_domain",)

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("SystemMonotonicClock cannot be subclassed")

    def __new__(cls) -> SystemMonotonicClock:
        if cls is not SystemMonotonicClock:
            raise TypeError("SystemMonotonicClock cannot be subclassed")
        global _system_monotonic_instance
        if _system_monotonic_instance is None:
            instance = super().__new__(cls)
            object.__setattr__(instance, "_domain", ClockDomain("system-monotonic"))
            _system_monotonic_instance = instance
        return _system_monotonic_instance

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("SystemMonotonicClock is immutable")

    @property
    def domain(self) -> ClockDomain:
        return self._domain

    def now(self) -> MonotonicInstant:
        return MonotonicInstant(time.monotonic_ns(), self._domain)

    def deadline_after(self, seconds: float) -> Deadline:
        return Deadline.from_instant(self.now() + seconds)

    def __copy__(self) -> SystemMonotonicClock:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> SystemMonotonicClock:
        return self


_system_monotonic_instance: SystemMonotonicClock | None = None
SYSTEM_MONOTONIC_CLOCK = SystemMonotonicClock()


class DeadlineClock:
    """Per-action view of a base clock bounded by one absolute deadline."""

    __slots__ = ("_base", "_deadline")

    def __init__(self, base: MonotonicClock, deadline: Deadline) -> None:
        if not isinstance(base, MonotonicClock):
            raise TypeError("deadline clock base must implement MonotonicClock")
        if base.domain is not deadline.domain:
            raise _domain_error("deadline clock construction", base.domain, deadline.domain)
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_deadline", deadline)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("DeadlineClock is immutable")

    @classmethod
    def after(cls, base: MonotonicClock, seconds: float) -> DeadlineClock:
        """Create an action deadline relative to the base clock's current time."""

        return cls(base, base.deadline_after(seconds))

    @property
    def base(self) -> MonotonicClock:
        return self._base

    @property
    def deadline(self) -> Deadline:
        return self._deadline

    def remaining(self) -> float:
        """Return non-negative seconds remaining in this action budget."""

        remaining = self._deadline - self._base.now()
        return max(0.0, remaining)

    def expired(self) -> bool:
        """Return whether the base clock has reached this action deadline."""

        return self._base.now() >= self._deadline.instant

    def child(self, deadline: Deadline) -> DeadlineClock:
        """Intersect this action with ``deadline``; a child cannot extend it."""

        if not isinstance(deadline, Deadline):
            raise TypeError("child deadline must be a Deadline")
        self._deadline._require_same_domain(deadline, "deadline intersection")
        selected = deadline if deadline.nanoseconds < self._deadline.nanoseconds else self._deadline
        return DeadlineClock(self._base, selected)


@runtime_checkable
class Sleeper(Protocol):
    """A sleeper whose requested duration is bounded by an absolute deadline."""

    def sleep(self, seconds: float, *, deadline: Deadline) -> None: ...


class SystemSleeper:
    """Sleep using the real system clock and never request beyond a deadline."""

    def sleep(self, seconds: float, *, deadline: Deadline) -> None:
        duration = _duration_nanoseconds(seconds, "sleep duration")
        if deadline.domain is not SYSTEM_MONOTONIC_CLOCK.domain:
            raise _domain_error("system sleep", SYSTEM_MONOTONIC_CLOCK.domain, deadline.domain)
        now = SYSTEM_MONOTONIC_CLOCK.now()
        remaining = deadline - now
        if remaining <= 0:
            raise DeadlineExpiredError("system sleep deadline has expired")
        remaining_nanoseconds = deadline.nanoseconds - now.nanoseconds
        sleep_nanoseconds = min(duration, remaining_nanoseconds)
        if sleep_nanoseconds > 0:
            time.sleep(sleep_nanoseconds / _NANOSECONDS_PER_SECOND)


__all__ = [
    "ClockDomain",
    "ClockDomainError",
    "Deadline",
    "DeadlineClock",
    "DeadlineExpiredError",
    "MonotonicClock",
    "MonotonicInstant",
    "SYSTEM_MONOTONIC_CLOCK",
    "Sleeper",
    "SystemMonotonicClock",
    "SystemSleeper",
]


class _ClockModule(ModuleType):
    """Prevent ordinary runtime replacement of the system clock binding."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"SYSTEM_MONOTONIC_CLOCK", "_system_monotonic_instance"} and name in self.__dict__:
            raise AttributeError(f"{name} is immutable")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in {"SYSTEM_MONOTONIC_CLOCK", "_system_monotonic_instance"}:
            raise AttributeError(f"{name} cannot be deleted")
        super().__delattr__(name)


_module = sys.modules[__name__]
_module.__class__ = _ClockModule
