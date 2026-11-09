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

from crowley.gui.pointer import (
    _DEFAULT_MOTION_PROFILE,
    _MIN_EXECUTABLE_DURATION,
    _MIN_PATH_INTERVALS,
    BindingMetadata,
    ClickEvent,
    ClickEventKind,
    ClickPlan,
    DemonstrationRole,
    ExecutorError,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    PointerTrajectory,
    Rect,
    _bounded,
    _duration,
    _finite,
    _path_samples,
    _positive,
    _positive_int,
    _text,
    plan_pointer_curve,
    plan_single_click,
)


class KeyRole(StrEnum):
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    STRAFE_LEFT = "strafe_left"
    STRAFE_RIGHT = "strafe_right"


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


_DEFAULT_CAMERA_PROFILE: Final = MotionProfile(role=DemonstrationRole.CAMERA)
_DEFAULT_WAIT_POLICY: Final = WaitPolicy()


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
