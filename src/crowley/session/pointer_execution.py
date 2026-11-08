"""Shared seeded pointer gestures and crash-safe automated-click spacing."""

from __future__ import annotations

import fcntl
import json
import math
import os
import random
import secrets
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .executor import (
    ClickPlan,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    PointerTrajectory,
    Rect,
    _duration,
    _path_samples,
)

CLICK_INTERVAL_NS = 1_000_000_000
_GO_LATENCY_RESERVE_NS = 5_000_000_000
_MAX_GATE_WAIT_SECONDS = 8.0
_MAX_STATE_FUTURE_NS = 10_000_000_000
_MAX_GESTURE_DURATION = 2.0
_MAX_GESTURE_ARGV_ITEMS = 1024
_MAX_GENERIC_MOTION_DURATION = 10.0
_MAX_GENERIC_MOTION_SAMPLES = 4096
_GESTURE_STARTUP_RESERVE_SECONDS = 1.0
_MIN_SEGMENT_DURATION = 0.08
_PERMIT_ISSUER = object()
_DEFAULT_POINTER_PROFILE = MotionProfile()


class PointerExecutionError(RuntimeError):
    """A pointer gesture or shared click interval could not be admitted."""


@dataclass(frozen=True, slots=True)
class HumanPointerPlan:
    seed: int
    trajectory: PointerTrajectory
    waypoints: tuple[Point, ...]
    click: ClickPlan | None

    @property
    def endpoint(self) -> Point:
        return self.trajectory.endpoint

    @property
    def movement_duration(self) -> float:
        return self.trajectory.duration

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
    else:
        # Already-at-target still receives a real, bounded out-and-back
        # submovement. The detour stays in the verified client, while the
        # frozen final endpoint remains exactly the selected target pixel.
        offsets = [(x, y) for x in (-12, -7, 7, 12) for y in (-8, -4, 4, 8)]
        rng.shuffle(offsets)
        for ox, oy in offsets:
            point = client_bounds.clip(Point(start.x + ox, start.y + oy)).to_pixel()
            if point != start and point not in candidates:
                candidates.append(point)
                if len(candidates) == 2:
                    break
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
    if not isinstance(profile, MotionProfile) or profile.role.value != "pointer":
        raise PointerExecutionError("human pointer gesture requires a pointer motion profile")
    selected_seed = secrets.randbits(64) if seed is None else seed
    if (
        isinstance(selected_seed, bool)
        or not isinstance(selected_seed, int)
        or not 0 <= selected_seed < 2**64
    ):
        raise PointerExecutionError("gesture seed must be an unsigned 64-bit integer")
    rng = random.Random(selected_seed)
    safe = target.safe_bounds
    endpoint = Point(
        rng.randint(int(safe.x), int(safe.right)),
        rng.randint(int(safe.y), int(safe.bottom)),
    )
    start = start.to_pixel()
    waypoints = _random_waypoints(start, endpoint, client_bounds, rng)
    points = (start, *waypoints, endpoint)
    # The one-at-target case gets two detours. For an ordinary path, one or
    # two seeded waypoints provide genuine submovements without a raw warp.
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


def _boot_identity() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise PointerExecutionError("kernel boot identity is unavailable") from exc
    if len(value) != 36 or any(ch not in "0123456789abcdef-" for ch in value.lower()):
        raise PointerExecutionError("kernel boot identity is malformed")
    return value.lower()


def _session_state_directory() -> Path:
    uid = os.getuid()
    runtime = Path("/run/user") / str(uid)
    try:
        runtime_stat = runtime.lstat()
    except OSError as exc:
        raise PointerExecutionError("canonical user runtime directory is unavailable") from exc
    if (
        stat.S_ISLNK(runtime_stat.st_mode)
        or not stat.S_ISDIR(runtime_stat.st_mode)
        or runtime_stat.st_uid != uid
        or runtime_stat.st_mode & 0o077
    ):
        raise PointerExecutionError("canonical user runtime directory is unsafe")
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        try:
            configured_path = Path(configured).resolve(strict=True)
        except OSError as exc:
            raise PointerExecutionError("configured runtime identity cannot be verified") from exc
        if configured_path != runtime:
            raise PointerExecutionError("configured runtime differs from the canonical user session")
    state_dir = runtime / "crowley"
    try:
        state_dir.mkdir(mode=0o700, exist_ok=True)
        info = state_dir.lstat()
    except OSError as exc:
        raise PointerExecutionError("canonical pointer timing directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & 0o077
    ):
        raise PointerExecutionError("canonical pointer timing directory is unsafe")
    return state_dir


def _secure_lock_fd(directory: Path) -> int:
    path = directory / "pointer-click-rate.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
    except OSError as exc:
        raise PointerExecutionError("pointer timing lock cannot be opened") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PointerExecutionError("pointer timing lock has unsafe ownership or mode")
    return fd


def _read_state(directory: Path) -> dict[str, object] | None:
    path = directory / "pointer-click-rate.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PointerExecutionError("pointer timing state cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PointerExecutionError("pointer timing state has unsafe ownership or mode")
        chunks = bytearray()
        while len(chunks) <= 4096:
            data = os.read(fd, 4097 - len(chunks))
            if not data:
                break
            chunks.extend(data)
        if len(chunks) > 4096:
            raise PointerExecutionError("pointer timing state exceeds its bound")
    finally:
        os.close(fd)
    try:
        state = json.loads(chunks.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PointerExecutionError("pointer timing state is malformed") from exc
    expected = {"version", "bootId", "nextAllowedMonotonicNs", "lastPressMonotonicNs"}
    if not isinstance(state, dict) or set(state) != expected or state.get("version") != 1:
        raise PointerExecutionError("pointer timing state schema is unknown")
    if not isinstance(state.get("bootId"), str):
        raise PointerExecutionError("pointer timing boot identity is malformed")
    next_allowed = state.get("nextAllowedMonotonicNs")
    last_press = state.get("lastPressMonotonicNs")
    if isinstance(next_allowed, bool) or not isinstance(next_allowed, int) or next_allowed < 0:
        raise PointerExecutionError("pointer timing deadline is malformed")
    if last_press is not None and (
        isinstance(last_press, bool) or not isinstance(last_press, int) or last_press < 0
    ):
        raise PointerExecutionError("pointer timing press record is malformed")
    return state


def _write_state(directory: Path, state: dict[str, object]) -> None:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")
    fd = -1
    temporary: str | None = None
    dir_fd = -1
    try:
        fd, temporary = tempfile.mkstemp(prefix=".pointer-click-rate.", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "pointer-click-rate.json")
        temporary = None
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(dir_fd)
    except OSError as exc:
        raise PointerExecutionError("durable pointer timing state publication failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if dir_fd >= 0:
            os.close(dir_fd)


class ClickRatePermit:
    """Exclusive timing permit held from pre-capture wait through release."""

    def __init__(
        self,
        fd: int,
        directory: Path,
        boot_id: str,
        *,
        acquired_monotonic: float,
        _issuer: object,
    ):
        if _issuer is not _PERMIT_ISSUER:
            raise PointerExecutionError("click timing permits are issued only by the canonical gate")
        if not math.isfinite(acquired_monotonic):
            raise PointerExecutionError("click timing permit acquisition time is malformed")
        self._fd = fd
        self._directory = directory
        self._boot_id = boot_id
        self._acquired_monotonic = float(acquired_monotonic)
        self._precommitted = False
        self._deadline_blocked = False
        self._recorded = False
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed and self._fd >= 0

    @property
    def acquired_monotonic(self) -> float:
        """Monotonic time at which this permit was issued by the rate gate."""

        return self._acquired_monotonic

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        if self._closed or self._precommitted:
            raise PointerExecutionError("click timing permit is closed or already committed")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise PointerExecutionError("click timing deadline is malformed")
        if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
            raise PointerExecutionError("click timing deadline expired before durable precommit")
        now = time.monotonic_ns()
        state = {
            "version": 1,
            "bootId": self._boot_id,
            "nextAllowedMonotonicNs": now + _GO_LATENCY_RESERVE_NS + CLICK_INTERVAL_NS,
            "lastPressMonotonicNs": None,
        }
        prior = _read_state(self._directory)
        if prior is not None and prior["bootId"] == self._boot_id:
            last_press = prior["lastPressMonotonicNs"]
            state["lastPressMonotonicNs"] = last_press
        _write_state(self._directory, state)
        self._precommitted = True
        if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
            # Keep the conservative durable reservation.  The caller must
            # refuse the physical press, but may not roll timing state back.
            self._deadline_blocked = True
            raise PointerExecutionError("click timing deadline expired after durable precommit")
        return now

    def record_press(self) -> int:
        if self._closed or not self._precommitted or self._recorded or self._deadline_blocked:
            raise PointerExecutionError("click timing press cannot be recorded in this permit state")
        actual = time.monotonic_ns()
        state = {
            "version": 1,
            "bootId": self._boot_id,
            "nextAllowedMonotonicNs": actual + CLICK_INTERVAL_NS,
            "lastPressMonotonicNs": actual,
        }
        _write_state(self._directory, state)
        self._recorded = True
        return actual

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> ClickRatePermit:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def acquire_click_rate_permit(
    *,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> ClickRatePermit:
    """Wait for and hold the canonical session click gate, failing closed."""

    directory = _session_state_directory()
    boot_id = _boot_identity()
    fd = _secure_lock_fd(directory)
    wait_deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
    if deadline_monotonic is not None:
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            os.close(fd)
            raise PointerExecutionError("click rate deadline is malformed")
        wait_deadline = min(wait_deadline, float(deadline_monotonic))
    try:
        while True:
            if cancellation_check is not None:
                cancellation_check()
            if time.monotonic() >= wait_deadline:
                raise PointerExecutionError("click rate gate wait exceeded its bounded deadline")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.025, max(0.0, wait_deadline - time.monotonic())))
        while True:
            if cancellation_check is not None:
                cancellation_check()
            now = time.monotonic_ns()
            state = _read_state(directory)
            if state is None or state["bootId"] != boot_id:
                return ClickRatePermit(
                    fd,
                    directory,
                    boot_id,
                    acquired_monotonic=time.monotonic(),
                    _issuer=_PERMIT_ISSUER,
                )
            next_allowed = state["nextAllowedMonotonicNs"]
            assert isinstance(next_allowed, int)
            if next_allowed - now > _MAX_STATE_FUTURE_NS:
                raise PointerExecutionError("pointer timing deadline is implausibly far in the future")
            if now >= next_allowed:
                return ClickRatePermit(
                    fd,
                    directory,
                    boot_id,
                    acquired_monotonic=time.monotonic(),
                    _issuer=_PERMIT_ISSUER,
                )
            if time.monotonic() >= wait_deadline:
                raise PointerExecutionError("click rate spacing wait exceeded its bounded deadline")
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                raise PointerExecutionError("click spacing wait cannot fit before its deadline")
            time.sleep(min(0.025, max(0.0, (next_allowed - now) / 1_000_000_000)))
    except BaseException:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        raise
