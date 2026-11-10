import math

import pytest

from crowley.gui.pointer import (
    BindingMetadata,
    ClickEvent,
    ClickEventKind,
    DemonstrationRole,
    ExecutorError,
    MotionProfile,
    Point,
    PointerButton,
    PointerSample,
    PointerTarget,
    Rect,
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
