"""Validated human-pointer geometry, seeded gesture planning and native motion chain.

The durable click-rate gate lives in ``crowley_automation.gui.click_gate``.
"""

from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ExecutorError(ValueError):
    """Raised when a plan or its input is outside the bounded contract."""


class PointerButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"


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
    """Bounded deterministic motion timing; no raw demonstrations are retained.

    ``duration_intercept`` and ``duration_slope`` are the fitted timing model
    parameters.  The model is ``intercept + slope * log2(1 + distance /
    target_width)``.
    """

    min_duration: float = 0.08
    max_duration: float = 2.0
    sample_hz: float = 60.0
    min_dwell: float = 0.05
    max_dwell: float = 0.35
    duration_intercept: float = 0.25
    duration_slope: float = 0.095

    def __post_init__(self) -> None:
        _bounded(self.min_duration, "min_duration", _MIN_EXECUTABLE_DURATION, 10.0)
        # Keep saved profiles inside the installed xdotool/Guardian budgets.
        _bounded(self.max_duration, "max_duration", self.min_duration, 4.0)
        _bounded(self.sample_hz, "sample_hz", 10.0, 240.0)
        if self.min_duration * self.sample_hz < _MIN_PATH_INTERVALS:
            raise ExecutorError("min_duration and sample_hz must provide three bounded path intervals")
        if math.ceil(self.max_duration * self.sample_hz) + 1 > _MAX_PATH_SAMPLES:
            raise ExecutorError("duration and sample_hz exceed the bounded path sample budget")
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


_MAX_PATH_SAMPLES: Final = 4096


_MIN_PATH_INTERVALS: Final = 3.0


_MIN_EXECUTABLE_DURATION: Final = 0.02


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


_MAX_GESTURE_DURATION = 2.0


_MAX_GESTURE_ARGV_ITEMS = 1024


_MAX_GENERIC_MOTION_DURATION = 10.0


_MAX_GENERIC_MOTION_SAMPLES = 4096


_GESTURE_STARTUP_RESERVE_SECONDS = 1.0


_MIN_SEGMENT_DURATION = 0.08


_DEFAULT_POINTER_PROFILE = MotionProfile()


class PointerExecutionError(RuntimeError):
    """A pointer gesture or shared click interval could not be admitted."""


@dataclass(frozen=True, slots=True)
class HumanPointerPlan:
    seed: int
    trajectory: PointerTrajectory | None
    waypoints: tuple[Point, ...]
    click: ClickPlan | None
    stationary: Point | None = None

    @property
    def endpoint(self) -> Point:
        if self.trajectory is None:
            if self.stationary is None:
                raise PointerExecutionError("stationary plan has no endpoint")
            return self.stationary
        return self.trajectory.endpoint

    @property
    def movement_duration(self) -> float:
        return 0.0 if self.trajectory is None else self.trajectory.duration

    @property
    def dwell(self) -> float:
        return 0.0 if self.click is None else self.click.dwell


def _random_waypoints(
    start: Point,
    endpoint: Point,
    client_bounds: Rect,
    rng: random.Random,
) -> tuple[Point, ...]:
    dx = endpoint.x - start.x
    dy = endpoint.y - start.y
    distance = math.hypot(dx, dy)
    candidates: list[Point] = []
    if distance > 0:
        count = rng.choice((1, 2))
        perpendicular = (-dy / distance, dx / distance)
        for index in range(count):
            progress = rng.uniform(0.28, 0.46) if index == 0 else rng.uniform(0.56, 0.76)
            offset_limit = min(18.0, max(2.0, distance * 0.045))
            offset = rng.uniform(-offset_limit, offset_limit)
            raw = Point(
                start.x + dx * progress + perpendicular[0] * offset,
                start.y + dy * progress + perpendicular[1] * offset,
            )
            point = client_bounds.clip(raw).to_pixel()
            if point not in (start, endpoint) and point not in candidates:
                candidates.append(point)
    if not candidates:
        raise PointerExecutionError("client has no safe pixel for a bounded pointer submovement")
    return tuple(candidates)


def _segment_durations(
    points: tuple[Point, ...],
    total_duration: float,
) -> tuple[float, ...]:
    distances = [math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(points, points[1:], strict=False)]
    if any(distance <= 0 for distance in distances):
        raise PointerExecutionError("pointer submovement contains a zero-length segment")
    count = len(distances)
    selected_total = max(total_duration, count * _MIN_SEGMENT_DURATION)
    if selected_total > _MAX_GESTURE_DURATION:
        raise PointerExecutionError("bounded pointer submovements exceed their total duration")
    distance_total = sum(distances)
    remainder = selected_total - count * _MIN_SEGMENT_DURATION
    return tuple(_MIN_SEGMENT_DURATION + remainder * distance / distance_total for distance in distances)


def plan_human_pointer(
    start: Point,
    target: PointerTarget,
    client_bounds: Rect,
    *,
    seed: int | None = None,
    button: PointerButton | None = PointerButton.LEFT,
    profile: MotionProfile = _DEFAULT_POINTER_PROFILE,
) -> HumanPointerPlan:
    """Plan a seeded, curved, multi-segment path and optional bounded click."""

    if not isinstance(start, Point) or not isinstance(target, PointerTarget):
        raise PointerExecutionError("pointer start and target are malformed")
    if not isinstance(client_bounds, Rect) or not client_bounds.encloses(target.bounds):
        raise PointerExecutionError("target is outside the verified client")
    if button is not None and not isinstance(button, PointerButton):
        raise PointerExecutionError("click button is not a closed PointerButton")
    if not isinstance(profile, MotionProfile):
        raise PointerExecutionError("human pointer gesture requires a MotionProfile")
    selected_seed = secrets.randbits(64) if seed is None else seed
    if (
        isinstance(selected_seed, bool)
        or not isinstance(selected_seed, int)
        or not 0 <= selected_seed < 2**64
    ):
        raise PointerExecutionError("gesture seed must be an unsigned 64-bit integer")
    rng = random.Random(selected_seed)
    safe = target.safe_bounds
    start = start.to_pixel()
    if safe.contains(start):
        click_plan = (
            None
            if button is None
            else ClickPlan(
                start,
                rng.uniform(profile.min_dwell, profile.max_dwell),
                button,
                target.binding,
                target.control,
                client_bounds,
            )
        )
        return HumanPointerPlan(selected_seed, None, (), click_plan, start)
    endpoint = Point(
        rng.randint(int(safe.x), int(safe.right)),
        rng.randint(int(safe.y), int(safe.bottom)),
    )
    start = start.to_pixel()
    waypoints = _random_waypoints(start, endpoint, client_bounds, rng)
    points = (start, *waypoints, endpoint)
    # One or two seeded waypoints provide submovements for actual travel.
    if len(points) < 3:
        raise PointerExecutionError("pointer gesture lacks a bounded submovement")
    distance = math.hypot(endpoint.x - start.x, endpoint.y - start.y)
    target_width = max(1.0, min(safe.width, safe.height))
    base = _duration(distance, target_width, profile)
    varied_total = min(
        _MAX_GESTURE_DURATION,
        max(profile.min_duration, base * rng.uniform(0.88, 1.12)),
    )
    durations = _segment_durations(points, varied_total)
    all_samples: list[PointerSample] = []
    elapsed = 0.0
    for segment_index, (segment_start, segment_end, duration) in enumerate(
        zip(points, points[1:], durations, strict=False)
    ):
        # A one-pixel target can still have an exact frozen endpoint. Curvature
        # is bounded and every sample remains within the verified client.
        curvature = rng.uniform(-0.16, 0.16)
        segment = _path_samples(
            segment_start,
            segment_end,
            client_bounds,
            duration,
            profile.sample_hz,
            curvature,
        )
        if segment_index == 0:
            all_samples.extend(segment)
        else:
            all_samples.extend(PointerSample(elapsed + sample.at, sample.point) for sample in segment[1:])
        elapsed += duration
    if not all_samples or all_samples[-1].point != endpoint:
        raise PointerExecutionError("gesture samples do not end at the frozen endpoint")
    trajectory = PointerTrajectory(
        tuple(all_samples),
        elapsed,
        endpoint,
        target.binding,
        target.control,
    )
    click_plan = None
    if button is not None:
        dwell = rng.uniform(profile.min_dwell, profile.max_dwell)
        click_plan = ClickPlan(endpoint, dwell, button, target.binding, target.control, client_bounds)
    return HumanPointerPlan(selected_seed, trajectory, tuple(waypoints), click_plan)


def build_pointer_motion_chain(
    trajectory: PointerTrajectory,
    client_geometry: tuple[int, int, int, int],
) -> list[str]:
    """Build the shared positive xdotool grammar for a timed pointer path."""

    if not isinstance(trajectory, PointerTrajectory):
        raise PointerExecutionError("pointer trajectory is malformed")
    if (
        not isinstance(client_geometry, tuple)
        or len(client_geometry) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in client_geometry)
        or client_geometry[2] <= 0
        or client_geometry[3] <= 0
    ):
        raise PointerExecutionError("pointer client geometry is malformed")
    if (
        trajectory.duration > _MAX_GENERIC_MOTION_DURATION
        or len(trajectory.samples) > _MAX_GENERIC_MOTION_SAMPLES
    ):
        raise PointerExecutionError("pointer path exceeds the bounded motion grammar")
    left, top, width, height = client_geometry
    bounds = Rect(0, 0, width, height)
    if any(not bounds.contains(sample.point) for sample in trajectory.samples):
        raise PointerExecutionError("pointer trajectory escaped the verified client")
    chain = ["xdotool"]
    previous_at = trajectory.samples[0].at
    previous_point = trajectory.samples[0].point
    for sample in trajectory.samples[1:]:
        delay = sample.at - previous_at
        if not math.isfinite(delay) or delay <= 0 or delay > _MAX_GESTURE_DURATION:
            raise PointerExecutionError("pointer trajectory timing is not strictly bounded")
        chain.extend(("sleep", f"{delay:.9f}"))
        if sample.point != previous_point:
            chain.extend(
                (
                    "mousemove",
                    str(left + int(sample.point.x)),
                    str(top + int(sample.point.y)),
                )
            )
        previous_at = sample.at
        previous_point = sample.point
    return chain


def native_pointer_motion_action(
    trajectory: PointerTrajectory,
    client_geometry: tuple[int, int, int, int],
) -> tuple[str, ...]:
    """Return the validated pinned-tool argv without the xdotool executable."""

    action = tuple(build_pointer_motion_chain(trajectory, client_geometry)[1:])
    validate_pointer_motion_action(action)
    return action


def validate_pointer_motion_action(action: tuple[str, ...]) -> float:
    """Validate native motion argv and return its planned elapsed duration."""

    if not isinstance(action, tuple) or not action or len(action) > _MAX_GESTURE_ARGV_ITEMS:
        raise PointerExecutionError("native pointer motion argv is malformed")
    index = 0
    elapsed = 0.0
    moves = 0
    while index < len(action):
        if action[index] != "sleep" or index + 1 >= len(action):
            raise PointerExecutionError("native pointer motion argv has an unknown operation")
        try:
            delay = float(action[index + 1])
        except (TypeError, ValueError) as exc:
            raise PointerExecutionError("native pointer motion delay is malformed") from exc
        if not math.isfinite(delay) or not 0 < delay <= _MAX_GESTURE_DURATION:
            raise PointerExecutionError("native pointer motion delay is outside its bound")
        elapsed += delay
        index += 2
        if index < len(action) and action[index] == "mousemove":
            if index + 2 >= len(action):
                raise PointerExecutionError("native pointer move is truncated")
            coords = action[index + 1 : index + 3]
            if any(not isinstance(value, str) or not value.isdecimal() for value in coords):
                raise PointerExecutionError("native pointer coordinates are malformed")
            moves += 1
            index += 3
    if moves < 2 or elapsed <= 0 or elapsed > _MAX_GESTURE_DURATION + 1e-6:
        raise PointerExecutionError("native pointer motion is not a bounded multi-segment gesture")
    return elapsed


def pointer_action_timeout(action: tuple[str, ...]) -> float:
    """Return a bounded timeout derived from the validated gesture duration."""

    duration = validate_pointer_motion_action(action)
    return duration + _GESTURE_STARTUP_RESERVE_SECONDS
