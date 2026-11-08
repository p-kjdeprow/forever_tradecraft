"""Pure, bounded plans for the local session executor.

This module deliberately stops at planning.  A driver may consume the plans, but
the planner never opens a display, emits an input event, or calls a model.  Every
plan carries the capture and Guardian binding that the driver must re-check
before admitting it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Final


class ExecutorError(ValueError):
    """Raised when a plan or its input is outside the bounded contract."""


class PointerButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"


class KeyRole(StrEnum):
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    STRAFE_LEFT = "strafe_left"
    STRAFE_RIGHT = "strafe_right"


class DemonstrationRole(StrEnum):
    POINTER = "pointer"
    CAMERA = "camera"
    MOVE = "move"


class ClickEventKind(StrEnum):
    PRESS = "press"
    RELEASE = "release"


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ExecutorError(f"{name} must be finite")
    return float(value)


def _positive(value: float, name: str) -> float:
    value = _finite(value, name)
    if value <= 0:
        raise ExecutorError(f"{name} must be positive")
    return value


def _nonnegative(value: float, name: str) -> float:
    value = _finite(value, name)
    if value < 0:
        raise ExecutorError(f"{name} must be nonnegative")
    return value


def _bounded(value: float, name: str, lower: float, upper: float) -> float:
    value = _finite(value, name)
    if not lower <= value <= upper:
        raise ExecutorError(f"{name} must be in [{lower}, {upper}]")
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutorError(f"{name} must be non-empty text")
    return value


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ExecutorError(f"{name} must be a positive integer")
    return value


def _pixel_int(
    value: float,
    name: str,
    *,
    positive: bool = False,
    allow_negative: bool = False,
) -> float:
    """Validate a native pixel coordinate/count without silently rounding it."""
    value = _finite(value, name)
    if (
        value != math.floor(value)
        or (positive and value < 1)
        or (not positive and not allow_negative and value < 0)
    ):
        raise ExecutorError(f"{name} must be a nonnegative integer pixel value")
    return value


def _quantize_pixel(value: float) -> float:
    """Round half away from zero, matching deterministic native integer emission."""
    return float(math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5))


def _require_pixel_point(point: Point, name: str = "point") -> None:
    if not isinstance(point, Point):
        raise ExecutorError(f"{name} must be Point")
    _pixel_int(point.x, f"{name}.x", allow_negative=True)
    _pixel_int(point.y, f"{name}.y", allow_negative=True)


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        _finite(self.x, "point.x")
        _finite(self.y, "point.y")

    def to_pixel(self) -> Point:
        """Return the deterministic integer pixel used by native input emission."""
        return Point(_quantize_pixel(self.x), _quantize_pixel(self.y))


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        _pixel_int(self.x, "rect.x", allow_negative=True)
        _pixel_int(self.y, "rect.y", allow_negative=True)
        _pixel_int(self.width, "rect.width", positive=True)
        _pixel_int(self.height, "rect.height", positive=True)

    @property
    def right(self) -> float:
        """Inclusive coordinate of the final addressable client pixel."""
        return self.x + self.width - 1

    @property
    def bottom(self) -> float:
        """Inclusive coordinate of the final addressable client pixel."""
        return self.y + self.height - 1

    @property
    def right_edge(self) -> float:
        """Exclusive X edge used when converting native width to pixel bounds."""
        return self.x + self.width

    @property
    def bottom_edge(self) -> float:
        """Exclusive Y edge used when converting native height to pixel bounds."""
        return self.y + self.height

    def contains(self, point: Point) -> bool:
        return self.x <= point.x <= self.right and self.y <= point.y <= self.bottom

    def clip(self, point: Point) -> Point:
        return Point(min(max(point.x, self.x), self.right), min(max(point.y, self.y), self.bottom))

    def encloses(self, other: Rect) -> bool:
        if not isinstance(other, Rect):
            raise ExecutorError("rect enclosure requires a Rect")
        return (
            self.x <= other.x
            and self.y <= other.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


@dataclass(frozen=True, slots=True)
class BindingMetadata:
    """The minimum identity/capture binding a later driver must revalidate."""

    identity_fingerprint: str
    capture_id: str
    capture_sequence: int
    generation: int
    target_xid: int
    target_ancestry: tuple[int, ...]
    source: str = "native-visual"
    boot: str = ""

    def __post_init__(self) -> None:
        _text(self.identity_fingerprint, "identity_fingerprint")
        _text(self.capture_id, "capture_id")
        _text(self.source, "source")
        if not isinstance(self.boot, str) or len(self.boot) > 96:
            raise ExecutorError("boot must be bounded text")
        _positive_int(self.capture_sequence, "capture_sequence")
        _positive_int(self.generation, "generation")
        _positive_int(self.target_xid, "target_xid")
        if not isinstance(self.target_ancestry, tuple):
            raise ExecutorError("target_ancestry must be an immutable tuple")
        if not self.target_ancestry or self.target_xid not in self.target_ancestry:
            raise ExecutorError("target_ancestry must contain target_xid")
        if any(type(item) is not int or item <= 0 for item in self.target_ancestry):
            raise ExecutorError("target_ancestry must contain positive window ids")


@dataclass(frozen=True, slots=True)
class PointerTarget:
    control: str
    bounds: Rect
    binding: BindingMetadata
    padding: float = 1.0

    def __post_init__(self) -> None:
        _text(self.control, "control")
        if not isinstance(self.bounds, Rect):
            raise ExecutorError("bounds must be a Rect")
        if not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("binding must be BindingMetadata")
        _pixel_int(self.padding, "padding")
        if self.padding * 2 >= min(self.bounds.width, self.bounds.height):
            raise ExecutorError("padding leaves no safe interior target")

    @property
    def safe_point(self) -> Point:
        """Return a deterministic addressable pixel near the interior center."""
        return Point(
            self.bounds.x + (self.bounds.width - 1) // 2,
            self.bounds.y + (self.bounds.height - 1) // 2,
        )

    @property
    def safe_bounds(self) -> Rect:
        return Rect(
            self.bounds.x + self.padding,
            self.bounds.y + self.padding,
            self.bounds.width - 2 * self.padding,
            self.bounds.height - 2 * self.padding,
        )


@dataclass(frozen=True, slots=True)
class MotionProfile:
    """Bounded deterministic motion parameters; no raw demonstrations are retained.

    ``duration_intercept`` and ``duration_slope`` are the fitted timing model
    parameters.  The model is ``intercept + slope * log2(1 + distance /
    target_width)``.  ``preferred_speed`` remains a compatible role-specific
    displacement/pulse preference; it is not another pointer-duration term.
    """

    preferred_speed: float = 900.0
    min_duration: float = 0.08
    max_duration: float = 2.0
    sample_hz: float = 60.0
    curvature: float = 0.08
    min_dwell: float = 0.05
    max_dwell: float = 0.35
    role: DemonstrationRole = DemonstrationRole.POINTER
    duration_intercept: float = 0.25
    duration_slope: float = 0.095

    def __post_init__(self) -> None:
        if not isinstance(self.role, DemonstrationRole):
            raise ExecutorError("profile role must be a closed DemonstrationRole value")
        _bounded(self.preferred_speed, "preferred_speed", 1.0, 5000.0)
        _bounded(self.min_duration, "min_duration", _MIN_EXECUTABLE_DURATION, 10.0)
        # Keep saved profiles inside the installed xdotool/Guardian budgets.
        _bounded(self.max_duration, "max_duration", self.min_duration, 4.0)
        _bounded(self.sample_hz, "sample_hz", 10.0, 240.0)
        if self.min_duration * self.sample_hz < _MIN_PATH_INTERVALS:
            raise ExecutorError("min_duration and sample_hz must provide three bounded path intervals")
        if math.ceil(self.max_duration * self.sample_hz) + 1 > _MAX_PATH_SAMPLES:
            raise ExecutorError("duration and sample_hz exceed the bounded path sample budget")
        _bounded(self.curvature, "curvature", -0.35, 0.35)
        _bounded(self.min_dwell, "min_dwell", _MIN_EXECUTABLE_DURATION, 2.0)
        _bounded(self.max_dwell, "max_dwell", self.min_dwell, 2.0)
        _bounded(self.duration_intercept, "duration_intercept", _MIN_EXECUTABLE_DURATION, 2.0)
        _bounded(self.duration_slope, "duration_slope", 0.001, 1.0)


@dataclass(frozen=True, slots=True)
class PointerSample:
    at: float
    point: Point

    def __post_init__(self) -> None:
        _require_pixel_point(self.point, "sample.point")
        _nonnegative(self.at, "sample.at")


@dataclass(frozen=True, slots=True)
class PointerTrajectory:
    samples: tuple[PointerSample, ...]
    duration: float
    endpoint: Point
    binding: BindingMetadata
    control: str

    def __post_init__(self) -> None:
        if not isinstance(self.samples, tuple):
            raise ExecutorError("samples must be an immutable tuple")
        if not isinstance(self.endpoint, Point) or not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("trajectory endpoint and binding have invalid types")
        _text(self.control, "control")
        if len(self.samples) < 2:
            raise ExecutorError("trajectory requires a start and endpoint")
        _positive(self.duration, "trajectory.duration")
        if self.samples[-1].point != self.endpoint:
            raise ExecutorError("trajectory endpoint must be exact")
        previous = -1.0
        for sample in self.samples:
            if not isinstance(sample, PointerSample):
                raise ExecutorError("trajectory samples must be PointerSample values")
            if sample.at < previous:
                raise ExecutorError("trajectory times must be monotonic")
            previous = sample.at
        if self.samples[0].at != 0.0 or self.samples[-1].at != self.duration:
            raise ExecutorError("trajectory must start at zero and end at duration")


@dataclass(frozen=True, slots=True)
class ClickEvent:
    kind: ClickEventKind
    at: float
    point: Point
    button: PointerButton

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ClickEventKind):
            raise ExecutorError("click event kind must be ClickEventKind")
        _require_pixel_point(self.point, "click event point")
        if not isinstance(self.button, PointerButton):
            raise ExecutorError("click event button must be PointerButton")
        _nonnegative(self.at, "click event time")


@dataclass(frozen=True, slots=True)
class ClickPlan:
    point: Point
    dwell: float
    button: PointerButton
    binding: BindingMetadata
    control: str
    client_bounds: Rect

    def __post_init__(self) -> None:
        _require_pixel_point(self.point, "click point")
        if not isinstance(self.button, PointerButton):
            raise ExecutorError("click button must be PointerButton")
        if not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("binding must be BindingMetadata")
        if not isinstance(self.client_bounds, Rect) or not self.client_bounds.contains(self.point):
            raise ExecutorError("click point must be inside verified client bounds")
        _text(self.control, "control")
        _bounded(self.dwell, "dwell", _MIN_EXECUTABLE_DURATION, 2.0)

    @property
    def events(self) -> tuple[ClickEvent, ClickEvent]:
        return (
            ClickEvent(ClickEventKind.PRESS, 0.0, self.point, self.button),
            ClickEvent(ClickEventKind.RELEASE, self.dwell, self.point, self.button),
        )


@dataclass(frozen=True, slots=True)
class KeyHoldPlan:
    role: KeyRole
    duration: float
    binding: BindingMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.role, KeyRole) or not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("key role and binding have invalid types")
        _bounded(self.duration, "key hold duration", _MIN_EXECUTABLE_DURATION, 3.0)

    @property
    def press_at(self) -> float:
        return 0.0

    @property
    def release_at(self) -> float:
        return self.duration


@dataclass(frozen=True, slots=True)
class CameraDragPlan:
    button: PointerButton
    samples: tuple[PointerSample, ...]
    duration: float
    signed_delta: Point
    binding: BindingMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.button, PointerButton) or not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("camera button and binding have invalid types")
        if not isinstance(self.samples, tuple):
            raise ExecutorError("camera samples must be an immutable tuple")
        if not isinstance(self.signed_delta, Point):
            raise ExecutorError("camera signed_delta must be Point")
        if self.button is not PointerButton.RIGHT:
            raise ExecutorError("camera drag only permits the right button")
        _positive(self.duration, "camera duration")
        if len(self.samples) < 2 or self.samples[0].at != 0.0 or self.samples[-1].at != self.duration:
            raise ExecutorError("camera drag requires a complete timed path")
        previous = -1.0
        for sample in self.samples:
            if not isinstance(sample, PointerSample) or sample.at < previous:
                raise ExecutorError("camera drag times must be monotonic PointerSamples")
            previous = sample.at
        expected = Point(
            self.samples[-1].point.x - self.samples[0].point.x,
            self.samples[-1].point.y - self.samples[0].point.y,
        )
        if expected != self.signed_delta:
            raise ExecutorError("camera signed_delta must match the path endpoints")
        if self.signed_delta.x == 0 and self.signed_delta.y == 0:
            raise ExecutorError("camera drag must have non-zero signed motion")

    @property
    def events(self) -> tuple[ClickEvent, ClickEvent]:
        return (
            ClickEvent(ClickEventKind.PRESS, 0.0, self.samples[0].point, self.button),
            ClickEvent(ClickEventKind.RELEASE, self.duration, self.samples[-1].point, self.button),
        )


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    timeout: float = 2.0
    poll_interval: float = 0.1
    settle_count: int = 2
    max_observation_age: float = 5.0

    def __post_init__(self) -> None:
        _bounded(self.timeout, "wait timeout", 0.01, 30.0)
        _bounded(self.poll_interval, "poll interval", 0.01, self.timeout)
        _bounded(self.max_observation_age, "max observation age", 0.01, 30.0)
        if (
            not isinstance(self.settle_count, int)
            or isinstance(self.settle_count, bool)
            or not 1 <= self.settle_count <= 10
        ):
            raise ExecutorError("settle_count must be an integer in [1, 10]")


@dataclass(frozen=True, slots=True)
class ContextWaitPlan:
    expected_context: str
    policy: WaitPolicy
    binding: BindingMetadata
    started_at: float
    allow_source_boot_change: bool = False

    def __post_init__(self) -> None:
        _text(self.expected_context, "expected_context")
        if not isinstance(self.policy, WaitPolicy) or not isinstance(self.binding, BindingMetadata):
            raise ExecutorError("wait policy and binding have invalid types")
        _finite(self.started_at, "wait started_at")
        if not isinstance(self.allow_source_boot_change, bool):
            raise ExecutorError("allow_source_boot_change must be boolean")

    def poll_times(self) -> tuple[float, ...]:
        count = math.ceil(self.policy.timeout / self.policy.poll_interval)
        return tuple(
            self.started_at + min(i * self.policy.poll_interval, self.policy.timeout)
            for i in range(count + 1)
        )


@dataclass(frozen=True, slots=True)
class FreshObservation:
    context: str
    identity_fingerprint: str
    generation: int
    capture_sequence: int
    observed_at: float
    source: str = "native-visual"
    boot: str = ""
    capture_id: str = ""
    # ``received_at`` records when the consumer admitted this observation.
    # It is deliberately separate from the evidence timestamp: a model call
    # may complete well after the captured frame, but an already-admitted
    # observation must not be aged again while later settling evidence is
    # produced.  Settling requires this field explicitly; ``None`` is useful
    # for standalone accepts_observation checks but cannot prove admission.
    received_at: float | None = None

    def __post_init__(self) -> None:
        _text(self.context, "observation.context")
        _text(self.identity_fingerprint, "observation.identity_fingerprint")
        _positive_int(self.generation, "observation.generation")
        _positive_int(self.capture_sequence, "observation.capture_sequence")
        _finite(self.observed_at, "observation.observed_at")
        _text(self.source, "observation.source")
        if not isinstance(self.boot, str) or len(self.boot) > 96:
            raise ExecutorError("observation.boot is too long")
        if not self.capture_id:
            object.__setattr__(self, "capture_id", f"sequence:{self.capture_sequence}")
        _text(self.capture_id, "observation.capture_id")
        if self.received_at is not None:
            _finite(self.received_at, "observation.received_at")


@dataclass(frozen=True, slots=True)
class MotionDemonstration:
    """Deliberate numeric summary; raw keys, text and event traces are excluded.

    ``target_width`` is the minimum safe interior dimension used by the
    planner, rather than the outer widget width or an arbitrary visual size.
    Numeric imports remain deliberate summaries and are not represented as
    verified live-human calibration.
    """

    role: DemonstrationRole
    distance: float
    duration: float
    target_width: float
    dwell: float = 0.0
    curvature: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.role, DemonstrationRole):
            raise ExecutorError("demonstration role must be a closed DemonstrationRole value")
        _bounded(self.distance, "demonstration distance", 0.0, 10000.0)
        _bounded(self.duration, "demonstration duration", _MIN_EXECUTABLE_DURATION, 10.0)
        _bounded(self.target_width, "demonstration target_width", 0.1, 10000.0)
        _bounded(self.dwell, "demonstration dwell", 0.0, 2.0)
        _bounded(self.curvature, "demonstration curvature", -0.35, 0.35)


_MAX_PATH_SAMPLES: Final = 4096
_MIN_PATH_INTERVALS: Final = 3.0
_MIN_EXECUTABLE_DURATION: Final = 0.02
_MIN_MOVE_PULSE: Final = 0.05
_MAX_MOVE_PULSE: Final = 0.35
_MIN_DURATION_INTERCEPT: Final = _MIN_EXECUTABLE_DURATION
_MAX_DURATION_INTERCEPT: Final = 2.0
_MIN_DURATION_SLOPE: Final = 0.001
_MAX_DURATION_SLOPE: Final = 1.0

# Camera drags do not have a widget target rectangle.  This is the explicit
# native-pixel endpoint tolerance used by the default planner; callers with a
# measured viewport tolerance may supply ``target_width`` directly.
CAMERA_ENDPOINT_TOLERANCE_PIXELS: Final = 40.0


def _smoothstep(value: float) -> float:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _path_samples(
    start: Point,
    end: Point,
    bounds: Rect,
    duration: float,
    sample_hz: float,
    curvature: float,
) -> tuple[PointerSample, ...]:
    start = start.to_pixel()
    end = end.to_pixel()
    if not bounds.contains(start) or not bounds.contains(end):
        raise ExecutorError("path endpoints must be inside client bounds")
    distance = math.hypot(end.x - start.x, end.y - start.y)
    if distance <= 0:
        raise ExecutorError("path endpoints must differ")
    if duration * sample_hz < _MIN_PATH_INTERVALS:
        raise ExecutorError("duration and sample rate must provide three bounded intervals")
    midpoint = Point((start.x + end.x) / 2, (start.y + end.y) / 2)
    perpendicular = Point(-(end.y - start.y) / distance, (end.x - start.x) / distance)
    control = bounds.clip(
        Point(
            # A quadratic Bézier reaches half its control-point offset at
            # its midpoint.  Doubling the control offset therefore makes the
            # observed peak deviation equal the stored curvature fraction.
            midpoint.x + perpendicular.x * distance * curvature * 2.0,
            midpoint.y + perpendicular.y * distance * curvature * 2.0,
        )
    )
    count = min(_MAX_PATH_SAMPLES - 1, max(1, math.ceil(duration * sample_hz)))
    samples: list[PointerSample] = []
    for index in range(count + 1):
        linear = index / count
        t = 0.0 if index == 0 else duration if index == count else duration * linear
        progress = _smoothstep(linear)
        one_minus = 1.0 - progress
        point = Point(
            one_minus * one_minus * start.x
            + 2 * one_minus * progress * control.x
            + progress * progress * end.x,
            one_minus * one_minus * start.y
            + 2 * one_minus * progress * control.y
            + progress * progress * end.y,
        )
        emitted = end if index == count else start if index == 0 else point.to_pixel()
        if not bounds.contains(emitted):
            raise ExecutorError("quantized path sample escaped client bounds")
        samples.append(PointerSample(t, emitted))
    return tuple(samples)


def _duration(distance: float, target_width: float, profile: MotionProfile) -> float:
    """Return the bounded Fitts-style movement duration for one target."""
    index_of_difficulty = math.log2(1.0 + distance / max(target_width, 0.1))
    requested = profile.duration_intercept + profile.duration_slope * index_of_difficulty
    return min(profile.max_duration, max(profile.min_duration, requested))


_DEFAULT_MOTION_PROFILE: Final = MotionProfile()
_DEFAULT_CAMERA_PROFILE: Final = MotionProfile(role=DemonstrationRole.CAMERA)
_DEFAULT_WAIT_POLICY: Final = WaitPolicy()


def plan_pointer_curve(
    start: Point,
    target: PointerTarget,
    client_bounds: Rect,
    *,
    profile: MotionProfile = _DEFAULT_MOTION_PROFILE,
) -> PointerTrajectory:
    """Build a bounded, smooth pointer path to the target's safe interior."""
    if (
        not isinstance(start, Point)
        or not isinstance(target, PointerTarget)
        or not isinstance(client_bounds, Rect)
    ):
        raise ExecutorError("pointer start, target and client_bounds have invalid types")
    if not isinstance(profile, MotionProfile):
        raise ExecutorError("profile must be MotionProfile")
    if profile.role is not DemonstrationRole.POINTER:
        raise ExecutorError("pointer plan requires a pointer profile")
    start = start.to_pixel()
    endpoint = target.safe_point
    if not client_bounds.encloses(target.bounds):
        raise ExecutorError("target must be inside client bounds")
    distance = math.hypot(endpoint.x - start.x, endpoint.y - start.y)
    if distance <= 0:
        raise ExecutorError("pointer start already equals target; no movement plan")
    duration = _duration(distance, min(target.safe_bounds.width, target.safe_bounds.height), profile)
    samples = _path_samples(start, endpoint, client_bounds, duration, profile.sample_hz, profile.curvature)
    return PointerTrajectory(samples, duration, endpoint, target.binding, target.control)


def plan_single_click(
    target: PointerTarget,
    client_bounds: Rect,
    *,
    button: PointerButton = PointerButton.LEFT,
    dwell: float | None = None,
    profile: MotionProfile = _DEFAULT_MOTION_PROFILE,
) -> ClickPlan:
    """Build one semantic-button press and matching release at a safe target point."""
    if not isinstance(target, PointerTarget) or not isinstance(client_bounds, Rect):
        raise ExecutorError("click target and client_bounds have invalid types")
    if not client_bounds.encloses(target.bounds):
        raise ExecutorError("click target must be inside verified client bounds")
    if not isinstance(button, PointerButton):
        raise ExecutorError("click button must be PointerButton")
    if not isinstance(profile, MotionProfile):
        raise ExecutorError("profile must be MotionProfile")
    if profile.role is not DemonstrationRole.POINTER:
        raise ExecutorError("click plan requires a pointer profile")
    selected_dwell = (profile.min_dwell + profile.max_dwell) / 2 if dwell is None else dwell
    _bounded(selected_dwell, "dwell", profile.min_dwell, profile.max_dwell)
    return ClickPlan(
        target.safe_point,
        selected_dwell,
        button,
        target.binding,
        target.control,
        client_bounds,
    )


def plan_key_hold(role: KeyRole, duration: float, binding: BindingMetadata) -> KeyHoldPlan:
    if not isinstance(role, KeyRole):
        raise ExecutorError("key role must be a closed KeyRole value")
    return KeyHoldPlan(role, duration, binding)


def plan_camera_drag(
    start: Point,
    end: Point,
    client_bounds: Rect,
    binding: BindingMetadata,
    *,
    profile: MotionProfile = _DEFAULT_CAMERA_PROFILE,
    target_width: float = CAMERA_ENDPOINT_TOLERANCE_PIXELS,
) -> CameraDragPlan:
    if not isinstance(start, Point) or not isinstance(end, Point) or not isinstance(client_bounds, Rect):
        raise ExecutorError("camera endpoints and client_bounds have invalid types")
    if not isinstance(profile, MotionProfile):
        raise ExecutorError("profile must be MotionProfile")
    if profile.role is not DemonstrationRole.CAMERA:
        raise ExecutorError("camera plan requires a camera profile")
    _bounded(target_width, "camera target_width", 0.1, 10000.0)
    start = start.to_pixel()
    end = end.to_pixel()
    distance = math.hypot(end.x - start.x, end.y - start.y)
    if distance <= 0:
        raise ExecutorError("camera drag must have non-zero signed motion")
    duration = _duration(distance, target_width, profile)
    samples = _path_samples(start, end, client_bounds, duration, profile.sample_hz, profile.curvature)
    return CameraDragPlan(
        PointerButton.RIGHT,
        samples,
        duration,
        Point(end.x - start.x, end.y - start.y),
        binding,
    )


def plan_context_wait(
    expected_context: str,
    binding: BindingMetadata,
    *,
    started_at: float,
    policy: WaitPolicy = _DEFAULT_WAIT_POLICY,
    allow_source_boot_change: bool = False,
) -> ContextWaitPlan:
    return ContextWaitPlan(expected_context, policy, binding, started_at, allow_source_boot_change)


def accepts_observation(plan: ContextWaitPlan, observation: FreshObservation, *, now: float) -> bool:
    """Return true only for a fresh observation from the exact bound generation."""
    if not isinstance(plan, ContextWaitPlan) or not isinstance(observation, FreshObservation):
        raise ExecutorError("plan and observation have invalid types")
    now = _finite(now, "now")
    same_domain = observation.source == plan.binding.source and observation.boot == plan.binding.boot
    domain_ok = same_domain or plan.allow_source_boot_change
    sequence_ok = observation.capture_sequence > plan.binding.capture_sequence if same_domain else True
    return (
        observation.context == plan.expected_context
        and observation.identity_fingerprint == plan.binding.identity_fingerprint
        and observation.generation == plan.binding.generation
        and domain_ok
        and sequence_ok
        and observation.observed_at >= plan.started_at
        and now <= plan.started_at + plan.policy.timeout
        and 0.0 <= now - observation.observed_at <= plan.policy.max_observation_age
    )


def observations_settled(
    plan: ContextWaitPlan,
    observations: tuple[FreshObservation, ...],
    *,
    now: float,
) -> bool:
    """Require distinct, consecutive fresh observations for the settle policy."""
    if not isinstance(plan, ContextWaitPlan) or not isinstance(observations, tuple):
        raise ExecutorError("settle plan and observations have invalid types")
    if len(observations) < plan.policy.settle_count:
        return False
    recent = observations[-plan.policy.settle_count :]
    # The producer validates every observation at the point it arrives.  For
    # callers that carry that admission timestamp, retain that decision for
    # earlier members of the settle run and apply the current freshness check
    # only to the latest frame.  This prevents a slow model turn from making
    # the first already-admitted observation stale retroactively.
    if any(observation.received_at is None for observation in recent):
        return False
    assert all(observation.received_at is not None for observation in recent)
    if any(
        observation.received_at < observation.observed_at or observation.received_at > now
        for observation in recent
    ):
        return False
    for index, observation in enumerate(recent):
        assert observation.received_at is not None
        validation_time = now if index == len(recent) - 1 else observation.received_at
        if not accepts_observation(plan, observation, now=validation_time):
            return False
    settled_source, settled_boot = recent[0].source, recent[0].boot
    if any(
        observation.source != settled_source or observation.boot != settled_boot for observation in recent
    ):
        return False
    return all(
        current.capture_id != previous.capture_id
        and previous.observed_at < current.observed_at
        and current.capture_sequence > previous.capture_sequence
        and previous.received_at <= current.received_at
        for previous, current in zip(recent, recent[1:], strict=False)
    )


def aggregate_motion_profile(
    demonstrations: tuple[MotionDemonstration, ...],
    *,
    baseline: MotionProfile | None = None,
) -> MotionProfile:
    """Aggregate deliberate summaries into one bounded role profile.

    At least two materially different target difficulties are required to
    identify both timing parameters.  With fewer, the baseline timing model
    is retained explicitly; the other aggregate fields can still learn a
    role-specific dwell, curve, or MOVE pulse preference.
    """
    if not isinstance(demonstrations, tuple):
        raise ExecutorError("demonstrations must be an immutable tuple")
    if baseline is not None and not isinstance(baseline, MotionProfile):
        raise ExecutorError("baseline must be MotionProfile or None")
    if any(not isinstance(demo, MotionDemonstration) for demo in demonstrations):
        raise ExecutorError("demonstrations must contain MotionDemonstration values")
    if len({demo.role for demo in demonstrations}) > 1:
        raise ExecutorError("demonstrations must select one motion role per profile")
    if not demonstrations:
        return _DEFAULT_MOTION_PROFILE if baseline is None else baseline
    role = demonstrations[0].role
    if baseline is None:
        # The omitted baseline is intentionally role-derived, so the CLI can
        # build camera and MOVE profiles without relabelling pointer bounds.
        baseline = MotionProfile(role=role)
    elif baseline.role is not role:
        raise ExecutorError("baseline role must match demonstration role")
    timed = [demo for demo in demonstrations if demo.distance > 0.0]
    difficulties = [math.log2(1.0 + demo.distance / max(demo.target_width, 0.1)) for demo in timed]
    duration_intercept, duration_slope = _fit_duration_model(
        timed,
        difficulties,
        baseline=baseline,
    )
    # Keep this long-standing field useful to movement consumers and existing
    # callers.  It is a bounded displacement preference, not part of the
    # pointer timing equation above.
    speeds = [
        demo.distance * (1.0 + 0.12 * difficulty) / demo.duration
        for demo, difficulty in zip(timed, difficulties, strict=False)
    ]
    dwell_values = [demo.dwell for demo in demonstrations]
    curvature_values = [demo.curvature for demo in demonstrations]
    speed = median(speeds) if speeds else baseline.preferred_speed
    dwell = median(dwell_values) if dwell_values else baseline.min_dwell
    curvature = median(curvature_values) if curvature_values else baseline.curvature

    min_duration = baseline.min_duration
    max_duration = baseline.max_duration
    if role is DemonstrationRole.MOVE:
        # ``max_duration`` is the existing runtime pulse ceiling.  For MOVE
        # profiles, retain a deliberate, bounded learned ceiling so the
        # selected file changes actual pulse sizing while the daemon's hard
        # 0.35 s safety cap remains authoritative.
        if not timed:
            raise ExecutorError("MOVE demonstrations require positive measured displacement")
        pulse = median(demo.duration for demo in timed)
        max_duration = min(_MAX_MOVE_PULSE, max(_MIN_MOVE_PULSE, pulse))
        min_duration = max(_MIN_MOVE_PULSE, min(min_duration, max_duration))
        # Preserve the executable sample invariant if a caller supplied a
        # low-rate baseline.
        min_duration = max(min_duration, _MIN_PATH_INTERVALS / baseline.sample_hz)
        max_duration = max(max_duration, min_duration)

    return MotionProfile(
        preferred_speed=min(5000.0, max(1.0, speed)),
        min_duration=min_duration,
        max_duration=max_duration,
        sample_hz=baseline.sample_hz,
        curvature=min(0.35, max(-0.35, curvature)),
        min_dwell=min(2.0, max(_MIN_EXECUTABLE_DURATION, dwell)),
        max_dwell=min(2.0, max(_MIN_EXECUTABLE_DURATION, dwell)),
        role=role,
        duration_intercept=duration_intercept,
        duration_slope=duration_slope,
    )


def _fit_duration_model(
    demonstrations: list[MotionDemonstration],
    difficulties: list[float],
    *,
    baseline: MotionProfile,
) -> tuple[float, float]:
    """Fit bounded intercept/slope with a deterministic sparse-data policy.

    The median of all pairwise slopes is a robust bounded fit for the small
    deliberate samples this module accepts.  A single difficulty cannot
    identify both parameters, so both baseline timing values are retained
    explicitly in that case.
    """
    if len(demonstrations) < 2 or max(difficulties, default=0.0) - min(difficulties, default=0.0) < 1e-6:
        return baseline.duration_intercept, baseline.duration_slope

    slopes = [
        (right.duration - left.duration) / (right_difficulty - left_difficulty)
        for left_index, left in enumerate(demonstrations)
        for right, right_difficulty in zip(
            demonstrations[left_index + 1 :],
            difficulties[left_index + 1 :],
            strict=False,
        )
        for left_difficulty in (difficulties[left_index],)
        if abs(right_difficulty - left_difficulty) >= 1e-6
    ]
    if not slopes:
        return baseline.duration_intercept, baseline.duration_slope
    slope = min(
        _MAX_DURATION_SLOPE,
        max(_MIN_DURATION_SLOPE, median(slopes)),
    )
    intercept = median(
        demo.duration - slope * difficulty
        for demo, difficulty in zip(demonstrations, difficulties, strict=False)
    )
    intercept = min(_MAX_DURATION_INTERCEPT, max(_MIN_DURATION_INTERCEPT, intercept))
    return intercept, slope


__all__ = [
    "BindingMetadata",
    "CAMERA_ENDPOINT_TOLERANCE_PIXELS",
    "CameraDragPlan",
    "ClickEvent",
    "ClickEventKind",
    "ClickPlan",
    "ContextWaitPlan",
    "DemonstrationRole",
    "ExecutorError",
    "FreshObservation",
    "KeyHoldPlan",
    "KeyRole",
    "MotionDemonstration",
    "MotionProfile",
    "PointerButton",
    "PointerSample",
    "PointerTarget",
    "PointerTrajectory",
    "Point",
    "Rect",
    "WaitPolicy",
    "accepts_observation",
    "aggregate_motion_profile",
    "observations_settled",
    "plan_camera_drag",
    "plan_context_wait",
    "plan_key_hold",
    "plan_pointer_curve",
    "plan_single_click",
]
