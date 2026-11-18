from __future__ import annotations

import hashlib
import json
import math

import pytest
from crowley_automation.movement import (
    MAX_HOLD_SECONDS,
    MAX_ROUTE_BYTES,
    Motion,
    MovementError,
    RouteProfile,
    load_route_profile,
    walk_waypoint,
)


def profile(*, waypoints: tuple[tuple[float, float], ...] = ((1.0, 0.0),)) -> RouteProfile:
    return RouteProfile(
        house="us-normal-horde",
        character="Tester",
        client_build="1.60.1.70938",
        instance_id=17,
        left_radians_per_second=1.0,
        right_radians_per_second=-1.0,
        forward_units_per_second=1.0,
        tolerance=0.1,
        waypoints=waypoints,
    )


class SimulatedMotion:
    def __init__(
        self,
        route: RouteProfile,
        *,
        x: float = 0.0,
        y: float = 0.0,
        facing: float = 0.0,
        actual_forward_speed: float = 1.0,
    ) -> None:
        self.route = route
        self.x, self.y, self.facing = x, y, facing
        self.actual_forward_speed = actual_forward_speed
        self.now = 0.0
        self.boot = "boot-a"
        self.house = route.house
        self.character = route.character
        self.client_build = route.client_build
        self.instance_id = route.instance_id
        self.keys: list[tuple[str, ...]] = []
        self.durations: list[float] = []
        self.move = True

    def sample(self) -> Motion:
        return Motion(
            boot=self.boot,
            observed_at=self.now,
            x=self.x,
            y=self.y,
            facing=self.facing % math.tau,
            instance_id=self.instance_id,
            client_build=self.client_build,
            house=self.house,
            character=self.character,
            moving=False,
            dead=False,
            combat=False,
            mounted=False,
        )

    def hold(self, keys: tuple[str, ...], seconds: float) -> None:
        self.keys.append(keys)
        self.durations.append(seconds)
        self.now += seconds
        if not self.move:
            return
        if keys == ("Left",):
            self.facing = (self.facing + self.route.left_radians_per_second * seconds) % math.tau
        elif keys == ("Right",):
            self.facing = (self.facing + self.route.right_radians_per_second * seconds) % math.tau
        elif keys == ("Up",):
            self.x += math.cos(self.facing) * self.actual_forward_speed * seconds
            self.y += math.sin(self.facing) * self.actual_forward_speed * seconds
        else:
            raise AssertionError(f"unexpected movement keys: {keys}")


def run(route: RouteProfile, world: SimulatedMotion, *, index: int = 0, stop=lambda: False):
    return walk_waypoint(
        route,
        index,
        sample=world.sample,
        hold=world.hold,
        monotonic=lambda: world.now,
        stop=stop,
    )


def test_measured_profile_round_trip_hash_and_content_addressed_lookup(tmp_path) -> None:
    route = profile(waypoints=((-12.5, 8), (3.25, 4.5)))
    encoded = route.canonical
    parsed = RouteProfile.from_json(encoded)

    assert parsed == route
    assert len(parsed.route_id) == 64
    assert parsed.route_id == "route-" + hashlib.sha256(encoded.encode()).hexdigest()[:58]
    assert RouteProfile.from_json(json.dumps(route.object())).route_id == route.route_id

    route_file = tmp_path / "profile.json"
    route_file.write_text(encoded, encoding="utf-8")
    assert load_route_profile(route_file, route.route_id) == route
    with pytest.raises(MovementError, match="does not match"):
        load_route_profile(route_file, profile().route_id)


@pytest.mark.parametrize(
    "source",
    [
        '{"schema":1,"schema":1}',
        (
            '{"schema":1,"house":"us-normal-horde","character":"Tester",'
            '"clientBuild":"1.60.1.70938","instanceId":17,"leftRadiansPerSecond":1,'
            '"rightRadiansPerSecond":-1,"forwardUnitsPerSecond":1,"tolerance":0.1,'
            '"waypoints":[[0,0]],"extra":0}'
        ),
        (
            '{"schema":1,"house":"us-normal-horde","character":"Tester",'
            '"clientBuild":"1.60.1.70938","instanceId":17,"leftRadiansPerSecond":1,'
            '"rightRadiansPerSecond":-1,"forwardUnitsPerSecond":1,"tolerance":0.1,'
            '"waypoints":[[true,0]]}'
        ),
        (
            '{"schema":1,"house":"us-normal-horde","character":"Tester",'
            '"clientBuild":"1.60.1.70938","instanceId":true,"leftRadiansPerSecond":1,'
            '"rightRadiansPerSecond":-1,"forwardUnitsPerSecond":1,"tolerance":0.1,'
            '"waypoints":[[0,0]]}'
        ),
        (
            '{"schema":1,"house":"us-normal-horde","character":"Tester",'
            '"clientBuild":"1.60.1.70938","instanceId":17,"leftRadiansPerSecond":1,'
            '"rightRadiansPerSecond":-1,"forwardUnitsPerSecond":1,"tolerance":0.1,'
            '"waypoints":[[0,0]],"x":NaN}'
        ),
    ],
)
def test_route_json_rejects_ambiguous_or_wrongly_typed_profiles(source: str) -> None:
    with pytest.raises(MovementError):
        RouteProfile.from_json(source)


def test_route_json_enforces_bytes_rate_signs_and_waypoint_bounds() -> None:
    with pytest.raises(MovementError, match="exceeds"):
        RouteProfile.from_json(" " * (MAX_ROUTE_BYTES + 1))
    with pytest.raises(MovementError, match="opposite signs"):
        RouteProfile("us-normal-horde", "Tester", "1.60.1.70938", 1, 1.0, 2.0, 1.0, 1.0, ((0.0, 0.0),))
    with pytest.raises(MovementError, match="bounds"):
        RouteProfile("us-normal-horde", "Tester", "1.60.1.70938", 1, -21.0, 1.0, 1.0, 1.0, ((0.0, 0.0),))
    with pytest.raises(MovementError, match="waypoints"):
        RouteProfile("us-normal-horde", "Tester", "1.60.1.70938", 1, 1.0, -1.0, 1.0, 1.0, ())
    with pytest.raises(MovementError, match="clientBuild"):
        RouteProfile("us-normal-horde", "Tester", "build-70938", 1, 1.0, -1.0, 1.0, 1.0, ((0.0, 0.0),))


@pytest.mark.parametrize(
    ("facing", "target", "expected_key"),
    [
        (math.tau - 0.2, (1.0, 0.2), "Left"),
        (0.4, (1.0, 0.0), "Right"),
    ],
)
def test_turn_sign_uses_measured_rates_across_angle_wrap(facing, target, expected_key) -> None:
    route = profile(waypoints=(target,))
    world = SimulatedMotion(route, facing=facing)

    result = run(route, world)

    assert result.reached and result.action_complete and result.reason == "reached"
    assert (expected_key,) in world.keys
    other = "Right" if expected_key == "Left" else "Left"
    assert (other,) not in world.keys
    assert all(len(keys) == 1 and keys[0] in {"Up", "Left", "Right"} for keys in world.keys)
    assert all(0 < seconds <= MAX_HOLD_SECONDS for seconds in world.durations)


def test_waypoint_success_is_one_primitive_and_does_not_walk_the_next_target() -> None:
    route = profile(waypoints=((1.0, 0.0), (100.0, 0.0)))
    world = SimulatedMotion(route)

    result = run(route, world, index=0)

    assert result.reached and not result.action_complete
    assert math.isclose(world.x, 1.0)
    assert len(result.samples) == len(result.holds) + 1
    assert len(result.holds) == 4
    final = run(route, SimulatedMotion(route, x=100.0), index=1)
    assert final.reached and final.action_complete


def test_wrong_direction_turn_fails_on_first_hold() -> None:
    route = profile(waypoints=((0.0, 10.0),))
    world = SimulatedMotion(route)
    normal_hold = world.hold

    def reverse_turn(keys: tuple[str, ...], seconds: float) -> None:
        before = world.facing
        normal_hold(keys, seconds)
        if keys == ("Left",):
            world.facing = before - 0.1

    world.hold = reverse_turn
    result = run(route, world)

    assert result.reason == "motion_inconsistent"
    assert not result.reached and not result.action_complete
    assert world.keys == [("Left",)]


def test_translation_during_turn_cannot_reach_or_issue_another_hold() -> None:
    route = profile(waypoints=((0.0, 0.2),))
    world = SimulatedMotion(route)
    normal_hold = world.hold

    def translate_while_turning(keys: tuple[str, ...], seconds: float) -> None:
        normal_hold(keys, seconds)
        if keys in (("Left",), ("Right",)):
            world.y = 0.15

    world.hold = translate_while_turning
    result = run(route, world)

    assert result.reason == "implausible_motion"
    assert not result.reached and not result.action_complete
    assert world.keys == [("Left",)]


def test_cancellation_during_sample_prevents_arrival_success() -> None:
    route = profile()
    world = SimulatedMotion(route)
    cancelled = False

    def arrive_and_cancel() -> Motion:
        nonlocal cancelled
        world.x = 1.0
        cancelled = True
        return world.sample()

    result = walk_waypoint(
        route,
        0,
        sample=arrive_and_cancel,
        hold=world.hold,
        monotonic=lambda: world.now,
        stop=lambda: cancelled,
    )

    assert result.reason == "cancelled"
    assert not result.reached and not result.action_complete
    assert result.holds == ()


def test_wrong_identity_stale_observation_and_boot_change_fail_closed() -> None:
    route = profile(waypoints=((10.0, 0.0),))
    wrong = SimulatedMotion(route)
    wrong.character = "Other"
    assert run(route, wrong).reason == "wrong_identity"
    assert wrong.keys == []

    stale = SimulatedMotion(route)
    stale.now = 10.0
    stale.sample = lambda: Motion(
        "boot-a",
        stale.now - 3.0,
        stale.x,
        stale.y,
        stale.facing % math.tau,
        17,
        route.client_build,
        route.house,
        route.character,
        False,
        False,
        False,
        False,
    )
    assert run(route, stale).reason == "stale_observation"
    assert stale.keys == []

    reboot = SimulatedMotion(route)
    original_hold = reboot.hold

    def reboot_after_first_hold(keys: tuple[str, ...], seconds: float) -> None:
        original_hold(keys, seconds)
        reboot.boot = "boot-b"

    reboot.hold = reboot_after_first_hold
    result = run(route, reboot)
    assert result.reason == "boot_changed"
    assert not result.reached
    assert not result.action_complete


def test_nonprogress_cancellation_and_deadline_are_bounded() -> None:
    route = profile(waypoints=((100.0, 0.0),))
    stuck = SimulatedMotion(route)
    stuck.move = False
    assert run(route, stuck).reason == "no_progress"
    assert len(stuck.keys) == 3

    cancelled = SimulatedMotion(route)
    result = run(route, cancelled, stop=lambda: cancelled.now >= 0.25)
    assert result.reason == "cancelled"
    assert len(result.holds) == 1

    far = SimulatedMotion(route)
    result = run(route, far)
    assert result.reason == "deadline"
    assert len(result.holds) == 60
    assert all(seconds <= MAX_HOLD_SECONDS for seconds in far.durations)
    assert far.now <= 15.0


def test_motion_rejects_bool_numbers_bad_facing_and_non_boolean_flags() -> None:
    args = ("boot", 0.0, 0.0, 0.0, 0.0, 1, "1.60.1.70938", "us-normal-horde", "Tester")
    with pytest.raises(MovementError):
        Motion(*args[:3], True, *args[4:], False, False, False, False)
    with pytest.raises(MovementError, match="facing"):
        Motion(
            "boot",
            0.0,
            0.0,
            0.0,
            math.tau + 1,
            1,
            "1.60.1.70938",
            "us-normal-horde",
            "Tester",
            False,
            False,
            False,
            False,
        )
    with pytest.raises(MovementError, match="boolean"):
        Motion(
            "boot", 0.0, 0.0, 0.0, 0.0, 1, "1.60.1.70938", "us-normal-horde", "Tester", 1, False, False, False
        )


def test_continuous_forward_preserves_key_through_feedback_but_releases_at_waypoint():
    from types import SimpleNamespace

    from crowley_automation.gui.uinput_worker import DeviceState

    route = profile(waypoints=((1.0, 0.0), (2.0, 0.0)))
    world = SimulatedMotion(route)
    events = []
    device = SimpleNamespace(write=lambda *args: events.append(args), syn=lambda: None)
    monitor = SimpleNamespace(check=lambda: None, active=lambda: False)
    state = DeviceState(device, monitor, clock=lambda: world.now, wall=lambda: 1000)
    budgets = []

    def hold(keys, budget):
        assert keys == ("Up",)
        budgets.append(budget)
        state.authorize(2000)
        state.feedback = world.now + 2
        state.travel = world.now + budget
        state.heartbeat = world.now
        state.key(103, True)
        # Feedback cadence is independent of the hold's travel/freshness budget.
        duration = min(0.1, budget)
        world.now += duration
        world.x += duration

    for index in range(2):
        try:
            result = walk_waypoint(
                route,
                index,
                sample=world.sample,
                hold=hold,
                monotonic=lambda: world.now,
                stop=lambda: False,
                continuous_forward=lambda deadline, _observed: hold(("Up",), deadline - world.now),
                release_forward=state.release,
            )
            assert result.reached
        finally:
            state.release()
        assert not state.held
    assert max(budgets) > MAX_HOLD_SECONDS
    assert events == [(1, 103, 1), (1, 103, 0), (1, 103, 1), (1, 103, 0)]


def test_continuous_approach_rebaselines_after_stopping_before_stationary_turn():
    from dataclasses import replace

    route = profile(waypoints=((1.0, 0.12),))
    world = SimulatedMotion(route)
    active = [False]
    stopped_samples, turns = [], []

    def advance(seconds):
        if active[0]:
            world.x += math.cos(world.facing) * seconds
            world.y += math.sin(world.facing) * seconds
        world.now += seconds

    def sample():
        # Capture reports its starting position, with real travel during return latency.
        value = replace(world.sample(), moving=active[0])
        advance(0.12)
        if not active[0]:
            stopped_samples.append((value.x, value.y))
        return value

    def forward(deadline, _observed):
        assert deadline > world.now
        active[0] = True
        advance(min(0.1, deadline - world.now))

    def release():
        if active[0]:
            advance(0.03)  # release transport latency while the old key is still held
        active[0] = False

    def hold(keys, seconds):
        assert not active[0]
        assert stopped_samples[-1] == pytest.approx((world.x, world.y))
        turns.append(keys)
        world.hold(keys, seconds)

    try:
        result = walk_waypoint(
            route,
            0,
            sample=sample,
            hold=hold,
            monotonic=lambda: world.now,
            stop=lambda: False,
            continuous_forward=forward,
            release_forward=release,
        )
        assert result.reached, result.reason
        assert turns and all(keys in {("Left",), ("Right",)} for keys in turns)
        assert len(stopped_samples) > 1
    finally:
        release()
