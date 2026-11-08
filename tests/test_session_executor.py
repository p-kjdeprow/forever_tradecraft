import math
from dataclasses import FrozenInstanceError

import pytest

from crowley.session.executor import (
    BindingMetadata,
    ClickEvent,
    ClickEventKind,
    DemonstrationRole,
    ExecutorError,
    FreshObservation,
    KeyRole,
    MotionDemonstration,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    Rect,
    WaitPolicy,
    accepts_observation,
    aggregate_motion_profile,
    observations_settled,
    plan_camera_drag,
    plan_context_wait,
    plan_key_hold,
    plan_pointer_curve,
    plan_single_click,
)


@pytest.fixture
def binding() -> BindingMetadata:
    return BindingMetadata("window-1", "capture-8", 8, 3, 42, (42, 7, 1))


@pytest.fixture
def target(binding: BindingMetadata) -> PointerTarget:
    return PointerTarget("auctioneer_buckler", Rect(100, 100, 120, 40), binding, padding=4)


def test_pointer_curve_is_smooth_bounded_and_exact(target: PointerTarget) -> None:
    trajectory = plan_pointer_curve(
        Point(40, 120), target, Rect(0, 0, 400, 300), profile=MotionProfile(curvature=0.2)
    )

    assert trajectory.endpoint == target.safe_point
    assert trajectory.samples[0].point == Point(40, 120)
    assert trajectory.samples[-1].point == target.safe_point
    assert trajectory.samples[-1].at == trajectory.duration
    assert all(sample.at >= 0 for sample in trajectory.samples)
    client = Rect(0, 0, 400, 300)
    assert all(client.contains(sample.point) for sample in trajectory.samples)

    distances = [
        math.hypot(b.point.x - a.point.x, b.point.y - a.point.y)
        for a, b in zip(trajectory.samples, trajectory.samples[1:], strict=False)
    ]
    assert distances[0] < max(distances[1:-1])
    assert distances[-1] < max(distances[1:-1])
    with pytest.raises(ExecutorError):
        plan_pointer_curve(
            Point(40, 120), target, client, profile=MotionProfile(role=DemonstrationRole.CAMERA)
        )


def test_pointer_duration_accounts_for_distance_and_target_size(target: PointerTarget) -> None:
    far = plan_pointer_curve(Point(0, 0), target, Rect(0, 0, 500, 500))
    small = PointerTarget("small", Rect(100, 100, 20, 20), target.binding, padding=2)
    narrow = plan_pointer_curve(Point(0, 0), small, Rect(0, 0, 500, 500))
    # The fitted model measures target difficulty once.  A shorter move can
    # still take longer when its safe interior is materially narrower.
    assert 0 < far.duration < narrow.duration


def test_default_timing_is_fitts_style_and_baseline_is_plausible() -> None:
    profile = MotionProfile()
    target = PointerTarget(
        "target",
        Rect(900, 480, 80, 80),
        BindingMetadata("window", "capture", 1, 1, 1, (1,)),
        padding=20,
    )
    short = plan_pointer_curve(Point(0, 520), target, Rect(0, 0, 1000, 700), profile=profile)
    medium_target = PointerTarget(
        "medium",
        Rect(460, 300, 250, 80),
        target.binding,
        padding=20,
    )
    medium = plan_pointer_curve(Point(0, 340), medium_target, Rect(0, 0, 1000, 700), profile=profile)
    thirty_target = PointerTarget("thirty", Rect(30, 0, 80, 80), target.binding, padding=20)
    thirty = plan_pointer_curve(Point(0, 40), thirty_target, Rect(0, 0, 1000, 700), profile=profile)

    assert 0.5 < short.duration < 0.9
    assert 0.45 < medium.duration < 0.7
    assert 0.2 < thirty.duration < 0.45
    assert short.duration > medium.duration > thirty.duration


def test_emitted_paths_are_deterministic_native_pixels_and_need_an_interior_tick(
    target: PointerTarget,
) -> None:
    trajectory = plan_pointer_curve(Point(40.4, 120.4), target, Rect(0, 0, 400, 300))
    assert trajectory.samples[0].point == Point(40, 120)
    assert len(trajectory.samples) >= 4
    assert all(sample.point.x.is_integer() and sample.point.y.is_integer() for sample in trajectory.samples)
    with pytest.raises(ExecutorError):
        plan_pointer_curve(
            Point(40, 120),
            target,
            Rect(0, 0, 400, 300),
            profile=MotionProfile(min_duration=0.01, max_duration=0.01, sample_hz=10),
        )

    with pytest.raises(ExecutorError):
        PointerSample(0.1, Point(1.5, 2))


def test_profile_minimums_are_jointly_executable(target: PointerTarget) -> None:
    executable = MotionProfile(min_duration=0.02, max_duration=0.2, sample_hz=200)
    trajectory = plan_pointer_curve(Point(40, 120), target, Rect(0, 0, 400, 300), profile=executable)
    assert trajectory.duration >= 0.02
    assert len(trajectory.samples) >= 4
    with pytest.raises(ExecutorError, match="three bounded path intervals"):
        MotionProfile(min_duration=0.02, max_duration=0.2, sample_hz=100)
    with pytest.raises(ExecutorError):
        MotionProfile(min_dwell=0.019)
    with pytest.raises(ExecutorError):
        plan_key_hold(KeyRole.MOVE_FORWARD, 0.019, target.binding)


def test_event_shape_is_strict_and_click_requires_verified_client_bounds(
    binding: BindingMetadata,
    target: PointerTarget,
) -> None:
    client = Rect(0, 0, 400, 300)
    with pytest.raises(ExecutorError):
        ClickEvent("press", 0, target.safe_point, PointerButton.LEFT)  # type: ignore[arg-type]
    with pytest.raises(ExecutorError):
        ClickEvent(ClickEventKind.PRESS, 0, Point(1.5, 2), PointerButton.LEFT)
    with pytest.raises(ExecutorError):
        ClickEvent(ClickEventKind.PRESS, 0, target.safe_point, "left")  # type: ignore[arg-type]
    outside = PointerTarget("menu", Rect(390, 290, 20, 20), binding)
    with pytest.raises(ExecutorError):
        plan_single_click(outside, client)


def test_click_is_one_semantic_button_press_and_release_with_profile_dwell(target: PointerTarget) -> None:
    client = Rect(0, 0, 400, 300)
    profile = MotionProfile(min_dwell=0.1, max_dwell=0.2)
    plan = plan_single_click(target, client, dwell=0.12, profile=profile)
    assert [event.kind.value for event in plan.events] == ["press", "release"]
    assert all(event.button is PointerButton.LEFT for event in plan.events)
    assert plan.events[0].at == 0
    assert plan.events[1].at == 0.12
    assert target.safe_bounds.contains(plan.point)
    npc_plan = plan_single_click(target, client, button=PointerButton.RIGHT, profile=profile)
    assert all(event.button is PointerButton.RIGHT for event in npc_plan.events)
    assert 0.1 <= npc_plan.dwell <= 0.2
    default_plan = plan_single_click(target, client)
    assert 0.05 <= default_plan.dwell <= 0.35
    with pytest.raises(ExecutorError):
        plan_single_click(target, client, dwell=0.0, profile=profile)
    with pytest.raises(ExecutorError):
        plan_single_click(target, client, profile=MotionProfile(role=DemonstrationRole.CAMERA))


def test_rect_uses_inclusive_pixel_coordinates_and_rejects_exclusive_edge(binding: BindingMetadata) -> None:
    client = Rect(0, 0, 4, 3)
    assert client.contains(Point(3, 2))
    assert not client.contains(Point(4, 2))
    assert not client.contains(Point(3, 3))
    assert client.clip(Point(4, 3)) == Point(3, 2)
    edge_target = PointerTarget("menu", Rect(0, 0, 4, 3), binding)
    assert edge_target.safe_bounds.contains(edge_target.safe_point)
    with pytest.raises(ExecutorError):
        plan_pointer_curve(Point(4, 1), edge_target, client)


def test_key_hold_and_camera_drag_have_closed_roles_and_signed_path(binding: BindingMetadata) -> None:
    hold = plan_key_hold(KeyRole.STRAFE_LEFT, 0.25, binding)
    assert hold.role is KeyRole.STRAFE_LEFT
    assert hold.press_at == 0
    assert hold.release_at == 0.25
    with pytest.raises(ExecutorError):
        plan_key_hold("a", 0.25, binding)  # type: ignore[arg-type]

    camera = plan_camera_drag(Point(300.4, 100.4), Point(160.4, 130.4), Rect(0, 0, 400, 300), binding)
    assert camera.button is PointerButton.RIGHT
    assert camera.signed_delta == Point(-140, 30)
    assert camera.samples[0].point == Point(300, 100)
    assert camera.samples[-1].point == Point(160, 130)
    assert [event.kind.value for event in camera.events] == ["press", "release"]
    with pytest.raises(ExecutorError):
        plan_camera_drag(Point(10, 10), Point(10, 10), Rect(0, 0, 400, 300), binding)


def test_camera_timing_uses_endpoint_tolerance_across_distances(binding: BindingMetadata) -> None:
    profile = aggregate_motion_profile(
        (
            MotionDemonstration(DemonstrationRole.CAMERA, 40, 0.4, 40),
            MotionDemonstration(DemonstrationRole.CAMERA, 160, 0.6, 40),
        )
    )
    near = plan_camera_drag(
        Point(100, 100),
        Point(140, 100),
        Rect(0, 0, 500, 300),
        binding,
        profile=profile,
        target_width=40,
    )
    far = plan_camera_drag(
        Point(100, 100),
        Point(260, 100),
        Rect(0, 0, 500, 300),
        binding,
        profile=profile,
        target_width=40,
    )
    assert near.duration == pytest.approx(0.4, abs=0.01)
    assert far.duration == pytest.approx(0.6, abs=0.01)
    assert near.duration < far.duration
    with pytest.raises(ExecutorError):
        plan_camera_drag(
            Point(100, 100),
            Point(140, 100),
            Rect(0, 0, 500, 300),
            binding,
            target_width=0,
        )


def test_wait_requires_fresh_matching_generation_and_sequence(binding: BindingMetadata) -> None:
    plan = plan_context_wait(
        "auction_house", binding, started_at=10.0, policy=WaitPolicy(timeout=0.5, poll_interval=0.2)
    )
    assert plan.poll_times() == (10.0, 10.2, 10.4, 10.5)
    fresh = FreshObservation("auction_house", "window-1", 3, 9, 10.1)
    assert accepts_observation(plan, fresh, now=10.3)
    assert not accepts_observation(plan, FreshObservation("auction_house", "window-1", 3, 9, 9.9), now=10.3)
    assert not accepts_observation(plan, FreshObservation("auction_house", "window-1", 3, 9, 10.1), now=10.6)
    assert not accepts_observation(plan, FreshObservation("auction_house", "window-1", 2, 9, 10.1), now=10.3)
    assert not accepts_observation(plan, FreshObservation("auction_house", "window-1", 3, 8, 10.1), now=10.3)
    assert not accepts_observation(plan, FreshObservation("other", "window-1", 3, 9, 10.1), now=10.3)
    assert not accepts_observation(plan, FreshObservation("auction_house", "window-1", 3, 9, 8.0), now=10.3)
    settled = (
        FreshObservation("auction_house", "window-1", 3, 9, 10.1, received_at=10.15),
        FreshObservation("auction_house", "window-1", 3, 10, 10.2, received_at=10.25),
    )
    assert observations_settled(plan, settled, now=10.3)
    assert not observations_settled(plan, (settled[0], settled[0]), now=10.3)
    assert observations_settled(
        plan,
        (settled[0], FreshObservation("auction_house", "window-1", 3, 11, 10.2, received_at=10.25)),
        now=10.3,
    )
    assert not observations_settled(
        plan,
        (
            settled[0],
            FreshObservation(
                "auction_house",
                "window-1",
                3,
                11,
                10.2,
                source="other-capture",
                received_at=10.25,
            ),
        ),
        now=10.3,
    )


def test_settling_does_not_reage_an_admitted_observation_after_model_latency(
    binding: BindingMetadata,
) -> None:
    """The runtime admits each frame on receipt, then settles the latest frame."""

    plan = plan_context_wait(
        "auction_house",
        binding,
        started_at=100.0,
        policy=WaitPolicy(
            timeout=10.0,
            poll_interval=0.2,
            settle_count=2,
            max_observation_age=3.1,
        ),
    )
    first = FreshObservation(
        "auction_house",
        "window-1",
        3,
        9,
        100.1,
        received_at=100.2,
    )
    latest = FreshObservation(
        "auction_house",
        "window-1",
        3,
        10,
        103.1,
        received_at=103.2,
    )
    assert accepts_observation(plan, first, now=100.2)
    assert accepts_observation(plan, latest, now=103.2)
    # Re-aging ``first`` at 103.2 would exceed the 3.1-second freshness
    # window.  Its admission at 100.2 remains valid while the latest frame is
    # checked at the decision instant.
    assert observations_settled(plan, (first, latest), now=103.2)
    missing_receipt = FreshObservation("auction_house", "window-1", 3, 11, 103.2)
    assert not observations_settled(plan, (first, missing_receipt), now=103.2)


def test_profile_aggregation_is_bounded_numeric_and_immutable(binding: BindingMetadata) -> None:
    profile = aggregate_motion_profile(
        (
            MotionDemonstration(DemonstrationRole.POINTER, 100, 0.5, 20, 0.1, 0.1),
            MotionDemonstration(DemonstrationRole.POINTER, 200, 1.0, 20, 0.2, 0.2),
        )
    )
    assert profile.preferred_speed > 200
    assert profile.role is DemonstrationRole.POINTER
    assert profile.min_dwell == profile.max_dwell == pytest.approx(0.15)
    assert profile.curvature == pytest.approx(0.15)
    assert profile.min_dwell == pytest.approx(0.15)
    assert profile.max_dwell == pytest.approx(0.15)
    camera_profile = aggregate_motion_profile((MotionDemonstration(DemonstrationRole.CAMERA, 100, 0.5, 20),))
    assert camera_profile.role is DemonstrationRole.CAMERA
    assert aggregate_motion_profile(()) == MotionProfile()
    with pytest.raises(ExecutorError):
        aggregate_motion_profile(
            (
                MotionDemonstration(DemonstrationRole.POINTER, 100, 0.5, 20),
                MotionDemonstration(DemonstrationRole.CAMERA, 100, 0.5, 20),
            )
        )
    with pytest.raises(FrozenInstanceError):
        profile.preferred_speed = 100  # type: ignore[misc]


def test_profile_fit_replays_different_difficulties_and_has_sparse_policy() -> None:
    intercept, slope = 0.3, 0.08

    def duration(distance: float, width: float) -> float:
        return intercept + slope * math.log2(1.0 + distance / width)

    demonstrations = (
        MotionDemonstration(DemonstrationRole.POINTER, 100, duration(100, 20), 20),
        MotionDemonstration(DemonstrationRole.POINTER, 400, duration(400, 40), 40),
        MotionDemonstration(DemonstrationRole.POINTER, 900, duration(900, 40), 40),
    )
    profile = aggregate_motion_profile(demonstrations)
    assert profile.duration_intercept == pytest.approx(intercept, abs=0.01)
    assert profile.duration_slope == pytest.approx(slope, abs=0.01)

    same_difficulty = (
        MotionDemonstration(DemonstrationRole.POINTER, 100, 0.5, 20),
        MotionDemonstration(DemonstrationRole.POINTER, 200, 0.5, 40),
    )
    sparse = aggregate_motion_profile(same_difficulty)
    assert sparse.duration_intercept == MotionProfile().duration_intercept
    assert sparse.duration_slope == MotionProfile().duration_slope


def test_curve_peak_matches_learned_fraction_when_unclipped(target: PointerTarget) -> None:
    profile = MotionProfile(curvature=0.2, sample_hz=120)
    trajectory = plan_pointer_curve(Point(50, 300), target, Rect(0, 0, 500, 600), profile=profile)
    start, end = trajectory.samples[0].point, trajectory.samples[-1].point
    dx, dy = end.x - start.x, end.y - start.y
    distance = math.hypot(dx, dy)
    deviations = [
        abs((sample.point.x - start.x) * dy - (sample.point.y - start.y) * dx) / distance
        for sample in trajectory.samples
    ]
    assert max(deviations) == pytest.approx(distance * profile.curvature, abs=2.0)


def test_move_aggregation_changes_bounded_pulse_preference(binding: BindingMetadata) -> None:
    short = aggregate_motion_profile((MotionDemonstration(DemonstrationRole.MOVE, 1, 0.08, 1),))
    long = aggregate_motion_profile((MotionDemonstration(DemonstrationRole.MOVE, 1, 0.24, 1),))
    assert short.role is long.role is DemonstrationRole.MOVE
    assert short.max_duration == pytest.approx(0.08)
    assert long.max_duration == pytest.approx(0.24)
    assert short.max_duration < long.max_duration <= 0.35


def test_move_aggregation_ignores_zero_displacement_and_rejects_all_zero() -> None:
    mixed = aggregate_motion_profile(
        (
            MotionDemonstration(DemonstrationRole.MOVE, 0, 0.3, 1),
            MotionDemonstration(DemonstrationRole.MOVE, 1, 0.1, 1),
            MotionDemonstration(DemonstrationRole.MOVE, 2, 0.2, 1),
        )
    )
    assert mixed.max_duration == pytest.approx(0.15)
    with pytest.raises(ExecutorError, match="positive measured displacement"):
        aggregate_motion_profile(
            (
                MotionDemonstration(DemonstrationRole.MOVE, 0, 0.1, 1),
                MotionDemonstration(DemonstrationRole.MOVE, 0, 0.2, 1),
            )
        )


def test_aggregation_requires_an_explicitly_matching_baseline_role() -> None:
    with pytest.raises(ExecutorError, match="baseline role"):
        aggregate_motion_profile(
            (MotionDemonstration(DemonstrationRole.CAMERA, 40, 0.4, 40),),
            baseline=MotionProfile(),
        )
    assert (
        aggregate_motion_profile((MotionDemonstration(DemonstrationRole.CAMERA, 40, 0.4, 40),)).role
        is DemonstrationRole.CAMERA
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Point(float("nan"), 0),
        lambda: Rect(0, 0, 0, 20),
        lambda: Rect(0, 0, 4.5, 20),
        lambda: BindingMetadata("window", "capture", 0, 1, 1, (1,)),
        lambda: MotionProfile(sample_hz=1),
        lambda: MotionDemonstration(DemonstrationRole.MOVE, 1, 1, 0),
    ],
)
def test_malformed_nonfinite_and_unbounded_inputs_fail(factory) -> None:
    with pytest.raises(ExecutorError):
        factory()
