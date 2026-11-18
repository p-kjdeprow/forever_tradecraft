"""Measured route profiles and a bounded feedback controller for one waypoint."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley_common.house import House

MAX_ROUTE_BYTES = 16_384
MAX_WAYPOINTS = 64
MAX_TURN_RATE = 20.0
MAX_FORWARD_SPEED = 1_000.0
MAX_INSTANCE_ID = 2_147_483_647
MAX_HOLD_SECONDS = 0.25
MAX_WAYPOINT_SECONDS = 15.0
MAX_OBSERVATION_AGE = 2.0
MAX_HOLDS = 64
HEADING_TOLERANCE = 0.12
TURN_SIGN_TOLERANCE = 0.01
MAX_TURN_DRIFT_UNITS = 0.05
_BUILD = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_ROUTE_ID = re.compile(r"route-[0-9a-f]{58}\Z")
_ROUTE_FIELDS = frozenset(
    {
        "schema",
        "house",
        "character",
        "clientBuild",
        "instanceId",
        "leftRadiansPerSecond",
        "rightRadiansPerSecond",
        "forwardUnitsPerSecond",
        "tolerance",
        "waypoints",
    }
)


class MovementError(ValueError):
    """A route, observation, or controller argument is outside its contract."""


def _number(value: object, name: str, low: float | None = None, high: float | None = None) -> float:
    if type(value) is int:
        try:
            result = float(value)
        except OverflowError as exc:
            raise MovementError(f"{name} must be finite") from exc
    elif type(value) is float:
        result = value
    else:
        raise MovementError(f"{name} must be finite")
    if (
        not math.isfinite(result)
        or (low is not None and result < low)
        or (high is not None and result > high)
    ):
        raise MovementError(f"{name} is outside its finite bounds")
    return result


def _string(value: object, name: str, limit: int) -> str:
    if type(value) is not str or not value:
        raise MovementError(f"{name} must be a nonempty string")
    try:
        if len(value.encode("utf-8")) > limit:
            raise MovementError(f"{name} exceeds {limit} UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise MovementError(f"{name} must be valid UTF-8") from exc
    return value


def _house(value: object) -> str:
    if type(value) is not str:
        raise MovementError("house must be a canonical house key")
    try:
        if House.parse(value).key == value:
            return value
    except ValueError:
        pass
    raise MovementError("house must be a canonical house key")


def _build(value: object) -> str:
    if type(value) is not str or not _BUILD.fullmatch(value) or len(value) > 64:
        raise MovementError("clientBuild must be a nonempty numeric dotted string")
    return value


def _integer(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise MovementError(f"{name} must be an integer in [0, {maximum}]")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise MovementError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _constant(value: str) -> object:
    raise MovementError(f"nonfinite JSON number is forbidden: {value}")


def _object(value: object, keys: set[str] | frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise MovementError("route must contain exactly the schema keys")
    return value


@dataclass(frozen=True, slots=True)
class RouteProfile:
    house: str
    character: str
    client_build: str
    instance_id: int
    left_radians_per_second: float
    right_radians_per_second: float
    forward_units_per_second: float
    tolerance: float
    waypoints: tuple[tuple[float, float], ...]
    schema: int = 1

    def __post_init__(self) -> None:
        _house(self.house)
        _string(self.character, "character", 128)
        _build(self.client_build)
        _integer(self.instance_id, "instanceId", MAX_INSTANCE_ID)
        left = _number(self.left_radians_per_second, "leftRadiansPerSecond", -MAX_TURN_RATE, MAX_TURN_RATE)
        right = _number(self.right_radians_per_second, "rightRadiansPerSecond", -MAX_TURN_RATE, MAX_TURN_RATE)
        if left == 0.0 or right == 0.0 or math.copysign(1.0, left) == math.copysign(1.0, right):
            raise MovementError("measured turn rates must be nonzero and have opposite signs")
        speed = _number(self.forward_units_per_second, "forwardUnitsPerSecond", high=MAX_FORWARD_SPEED)
        if speed <= 0.0:
            raise MovementError("forwardUnitsPerSecond must be positive")
        _number(self.tolerance, "tolerance", 0.1, 5.0)
        if type(self.schema) is not int or self.schema != 1:
            raise MovementError("schema must be integer 1")
        if type(self.waypoints) is not tuple or not 1 <= len(self.waypoints) <= MAX_WAYPOINTS:
            raise MovementError(f"waypoints must contain 1..{MAX_WAYPOINTS} immutable positions")
        for index, point in enumerate(self.waypoints):
            if type(point) is not tuple or len(point) != 2:
                raise MovementError(f"waypoints[{index}] must be an immutable x,y pair")
            _number(point[0], "waypoint x")
            _number(point[1], "waypoint y")

    def object(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "house": self.house,
            "character": self.character,
            "clientBuild": self.client_build,
            "instanceId": self.instance_id,
            "leftRadiansPerSecond": self.left_radians_per_second,
            "rightRadiansPerSecond": self.right_radians_per_second,
            "forwardUnitsPerSecond": self.forward_units_per_second,
            "tolerance": self.tolerance,
            "waypoints": [[x, y] for x, y in self.waypoints],
        }

    @property
    def canonical(self) -> str:
        return json.dumps(
            self.object(), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    @property
    def route_id(self) -> str:
        return "route-" + hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()[:58]

    @classmethod
    def from_json(cls, source: str | bytes) -> RouteProfile:
        if type(source) not in (str, bytes):
            raise MovementError("route input must be UTF-8 JSON")
        if isinstance(source, str) and len(source) > MAX_ROUTE_BYTES:
            raise MovementError(f"route exceeds {MAX_ROUTE_BYTES} bytes")
        raw = source.encode("utf-8") if isinstance(source, str) else source
        if len(raw) > MAX_ROUTE_BYTES:
            raise MovementError(f"route exceeds {MAX_ROUTE_BYTES} bytes")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except MovementError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MovementError("route is not valid UTF-8 JSON") from exc
        row = _object(value, _ROUTE_FIELDS)
        waypoints = row["waypoints"]
        if type(waypoints) is not list or not 1 <= len(waypoints) <= MAX_WAYPOINTS:
            raise MovementError(f"waypoints must contain 1..{MAX_WAYPOINTS} positions")
        points = []
        for point in waypoints:
            if type(point) is not list or len(point) != 2:
                raise MovementError("each waypoint must be an x,y pair")
            points.append(cast(tuple[float, float], tuple(point)))
        return cls(
            cast(str, row["house"]),
            cast(str, row["character"]),
            cast(str, row["clientBuild"]),
            cast(int, row["instanceId"]),
            cast(float, row["leftRadiansPerSecond"]),
            cast(float, row["rightRadiansPerSecond"]),
            cast(float, row["forwardUnitsPerSecond"]),
            cast(float, row["tolerance"]),
            tuple(points),
            cast(int, row["schema"]),
        )


def load_route_profile(path: str | Path, expected_route_id: str) -> RouteProfile:
    if type(expected_route_id) is not str or not _ROUTE_ID.fullmatch(expected_route_id):
        raise MovementError("routeId is malformed")
    with Path(path).open("rb") as source:
        route = RouteProfile.from_json(source.read(MAX_ROUTE_BYTES + 1))
    if route.route_id != expected_route_id:
        raise MovementError("route file content does not match routeId")
    return route


@dataclass(frozen=True, slots=True)
class Motion:
    boot: str
    observed_at: float
    x: float
    y: float
    facing: float
    instance_id: int
    client_build: str
    house: str
    character: str
    moving: bool
    dead: bool
    combat: bool
    mounted: bool

    def __post_init__(self) -> None:
        _string(self.boot, "boot", 128)
        _number(self.observed_at, "observed_at", 0.0)
        _number(self.x, "x")
        _number(self.y, "y")
        _number(self.facing, "facing", 0.0, math.tau)
        _integer(self.instance_id, "instance_id", MAX_INSTANCE_ID)
        _build(self.client_build)
        _house(self.house)
        _string(self.character, "character", 128)
        if any(type(getattr(self, name)) is not bool for name in ("moving", "dead", "combat", "mounted")):
            raise MovementError("moving, dead, combat, and mounted must be exact booleans")


@dataclass(frozen=True, slots=True)
class HoldEvidence:
    keys: tuple[str, ...]
    seconds: float
    started_at: float
    finished_at: float


@dataclass(frozen=True, slots=True)
class WaypointResult:
    reached: bool
    action_complete: bool
    reason: str
    samples: tuple[Motion, ...]
    holds: tuple[HoldEvidence, ...]


def _angle(value: float) -> float:
    return (value + math.pi) % math.tau - math.pi


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def walk_waypoint(
    profile: RouteProfile,
    index: int,
    *,
    sample: Callable[[], Motion],
    hold: Callable[[tuple[str, ...], float], None],
    monotonic: Callable[[], float],
    stop: Callable[[], bool],
    continuous_forward: Callable[[float, float], None] | None = None,
    release_forward: Callable[[], None] | None = None,
) -> WaypointResult:
    """Walk one waypoint; optional forward continuity is bounded by fresh feedback.

    The continuous transport owns independent expiry and releases before receipt.
    Turning remains stationary and uses the existing measured turn rates.
    """
    if continuous_forward is not None and not callable(continuous_forward):
        raise MovementError("continuous_forward must be a bounded transport callback")
    if continuous_forward is not None and not callable(release_forward):
        raise MovementError("continuous movement requires an explicit release callback")
    if type(profile) is not RouteProfile or type(index) is not int or not 0 <= index < len(profile.waypoints):
        raise MovementError("profile or waypoint index is invalid")
    clock = _number(monotonic(), "monotonic clock", 0.0)
    deadline = clock + MAX_WAYPOINT_SECONDS
    samples: list[Motion] = []
    holds: list[HoldEvidence] = []
    target_x, target_y = profile.waypoints[index]
    boot: str | None = None
    observed_at: float | None = None
    no_progress = 0
    forward_active = False

    def now() -> float:
        nonlocal clock
        value = _number(monotonic(), "monotonic clock", 0.0)
        if value < clock:
            raise MovementError("monotonic clock moved backwards")
        clock = value
        return value

    def done(reason: str, reached: bool = False) -> WaypointResult:
        complete = reached and index == len(profile.waypoints) - 1
        return WaypointResult(reached, complete, reason, tuple(samples), tuple(holds))

    def cancelled() -> bool:
        value = stop()
        if type(value) is not bool:
            raise MovementError("stop callback must return a boolean")
        return value

    def read() -> tuple[Motion | None, str | None]:
        nonlocal boot, observed_at
        if cancelled():
            return None, "cancelled"
        if now() >= deadline:
            return None, "deadline"
        motion = sample()
        if type(motion) is not Motion:
            raise MovementError("sample callback must return a Motion")
        if cancelled():
            return None, "cancelled"
        timestamp = now()
        if timestamp >= deadline:
            return None, "deadline"
        if motion.observed_at > timestamp:
            return None, "future_observation"
        if timestamp - motion.observed_at > MAX_OBSERVATION_AGE or (
            observed_at is not None and motion.observed_at <= observed_at
        ):
            return None, "stale_observation"
        observed_at = motion.observed_at
        if (motion.house, motion.character) != (profile.house, profile.character):
            return None, "wrong_identity"
        if motion.client_build != profile.client_build:
            return None, "wrong_build"
        if motion.instance_id != profile.instance_id:
            return None, "wrong_instance"
        if boot is None:
            boot = motion.boot
        elif motion.boot != boot:
            return None, "boot_changed"
        if motion.dead or motion.combat or motion.mounted:
            return None, "unsafe_state"
        samples.append(motion)
        return motion, None

    current, reason = read()
    if reason is not None:
        return done(reason)
    assert current is not None

    def stopped_baseline() -> tuple[Motion | None, str | None]:
        nonlocal forward_active
        assert release_forward is not None
        release_forward()
        forward_active = False
        while True:
            baseline, failure = read()
            if failure is not None or (baseline is not None and not baseline.moving):
                return baseline, failure

    while True:
        if cancelled():
            return done("cancelled")
        dx, dy = target_x - current.x, target_y - current.y
        distance = math.hypot(dx, dy)
        if distance <= profile.tolerance:
            return done("reached", True)
        remaining = deadline - now()
        if remaining <= 0.0:
            return done("deadline")
        error = _angle(math.atan2(dy, dx) - current.facing)
        turning = abs(error) > HEADING_TOLERANCE
        if turning and forward_active:
            current, reason = stopped_baseline()
            if reason is not None:
                return done(reason)
            assert current is not None
            continue
        if turning:
            rate, key = profile.left_radians_per_second, "Left"
            if error * rate < 0.0:
                rate, key = profile.right_radians_per_second, "Right"
            seconds = min(MAX_HOLD_SECONDS, abs(error / rate), remaining)
        else:
            rate, key = profile.forward_units_per_second, "Up"
            if continuous_forward:
                # An already-held key travels during observation too. A stopped
                # baseline does not. Neither estimate can be rebased at delivery.
                base = current.observed_at if forward_active else now()
                travel_deadline = min(
                    current.observed_at + MAX_OBSERVATION_AGE,
                    base + max(0.0, distance - profile.tolerance * 0.5) / rate,
                    deadline,
                )
                seconds = travel_deadline - now()
                if seconds <= 0 and forward_active:
                    current, reason = stopped_baseline()
                    if reason is not None:
                        return done(reason)
                    assert current is not None
                    continue
            else:
                seconds = min(MAX_HOLD_SECONDS, distance / rate, remaining)
        if seconds <= 0.0:
            return done("deadline")
        if len(holds) >= MAX_HOLDS:
            return done("hold_limit")
        if cancelled():
            return done("cancelled")
        began = now()
        if began >= deadline:
            return done("deadline")
        seconds = min(seconds, deadline - began)
        if continuous_forward is not None and not turning:
            continuous_forward(travel_deadline, current.observed_at)
            forward_active = True
        else:
            hold((key,), seconds)
        ended = now()
        holds.append(HoldEvidence((key,), seconds, began, ended))
        if cancelled():
            return done("cancelled")
        if ended >= deadline:
            return done("deadline")
        following, reason = read()
        if reason is not None:
            return done(reason)
        assert following is not None
        if continuous_forward and not turning:
            # Capture latency is travel time too. The helper still caps exposure
            # independently at the admitted feedback/travel deadlines.
            seconds = min(seconds, now() - began)
        moved_x, moved_y = following.x - current.x, following.y - current.y
        displacement = math.hypot(moved_x, moved_y)
        if turning:
            expected = rate * seconds
            delta = _angle(following.facing - current.facing)
            if delta * expected < 0.0 and abs(delta) > TURN_SIGN_TOLERANCE:
                return done("motion_inconsistent")
            if abs(delta) > max(0.35, abs(expected) * 2.0 + 0.1):
                return done("implausible_motion")
            if displacement > MAX_TURN_DRIFT_UNITS:
                return done("implausible_motion")
            no_progress = no_progress + 1 if abs(delta) < abs(expected) * 0.2 else 0
        else:
            delta = _angle(following.facing - current.facing)
            expected = rate * seconds
            if abs(delta) > 0.35 or displacement > expected * 2 + profile.tolerance:
                return done("motion_inconsistent" if abs(delta) > 0.35 else "implausible_motion")
            if displacement > 1e-9:
                heading = current.facing + delta / 2
                along = _dot(moved_x, moved_y, math.cos(heading), math.sin(heading))
                lateral = abs(_dot(moved_x, moved_y, -math.sin(heading), math.cos(heading)))
                if along <= 0 or lateral > max(0.05, along * math.tan(0.5)):
                    return done("motion_inconsistent")
            next_distance = math.hypot(target_x - following.x, target_y - following.y)
            if next_distance <= profile.tolerance:
                return done("reached", True)
            if _dot(dx, dy, target_x - following.x, target_y - following.y) < 0:
                return done("overshoot")
            if next_distance > distance + max(0.05, profile.tolerance * 0.25):
                return done("divergence")
            no_progress = no_progress + 1 if distance - next_distance < expected * 0.2 else 0
        if no_progress >= 3:
            return done("no_progress")
        current = following
