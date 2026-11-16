import math

import pytest
from crowley_automation.gui.pointer import (
    BindingMetadata,
    ExecutorError,
    MotionProfile,
    Point,
    PointerButton,
    PointerExecutionError,
    PointerSample,
    PointerTarget,
    Rect,
    plan_human_pointer,
)


@pytest.fixture
def binding() -> BindingMetadata:
    return BindingMetadata("window-1", "capture-8", 8, 3, 42, (42, 7, 1))


@pytest.fixture
def target(binding: BindingMetadata) -> PointerTarget:
    return PointerTarget("auctioneer_buckler", Rect(100, 100, 120, 40), binding, padding=4)


def test_seeded_pointer_plan_is_smooth_bounded_and_exact(target: PointerTarget) -> None:
    client = Rect(0, 0, 400, 300)
    plan = plan_human_pointer(Point(40, 120), target, client, seed=1234)
    samples = plan.trajectory.samples

    assert plan.endpoint == samples[-1].point
    assert target.safe_bounds.contains(plan.endpoint)
    assert samples[0].point == Point(40, 120)
    assert samples[-1].at == plan.movement_duration
    assert len(samples) >= 4 and len(plan.waypoints) >= 1
    assert all(sample.at >= 0 for sample in samples)
    assert all(client.contains(sample.point) for sample in samples)
    assert all(sample.point.x.is_integer() and sample.point.y.is_integer() for sample in samples)
    assert all(b.at > a.at for a, b in zip(samples, samples[1:], strict=False))

    distances = [
        math.hypot(b.point.x - a.point.x, b.point.y - a.point.y)
        for a, b in zip(samples, samples[1:], strict=False)
    ]
    assert max(distances) > 0
    assert plan.movement_duration <= 2.0


def test_pointer_timing_accounts_for_target_difficulty() -> None:
    binding = BindingMetadata("window", "capture", 1, 1, 1, (1,))
    client = Rect(0, 0, 1000, 700)
    profile = MotionProfile()
    short_target = PointerTarget("short", Rect(900, 480, 80, 80), binding, padding=20)
    medium_target = PointerTarget("medium", Rect(460, 300, 250, 80), binding, padding=20)
    near_target = PointerTarget("near", Rect(30, 0, 80, 80), binding, padding=20)

    short = plan_human_pointer(Point(0, 520), short_target, client, seed=15, button=None, profile=profile)
    medium = plan_human_pointer(Point(0, 340), medium_target, client, seed=15, button=None, profile=profile)
    near = plan_human_pointer(Point(0, 40), near_target, client, seed=15, button=None, profile=profile)
    assert 0.5 < short.movement_duration < 0.9
    assert 0.45 < medium.movement_duration < 0.7
    assert 0.2 < near.movement_duration < 0.45
    assert short.movement_duration > medium.movement_duration > near.movement_duration


def test_native_pixel_quantization_and_minimum_path_intervals(target: PointerTarget) -> None:
    plan = plan_human_pointer(Point(40.4, 120.4), target, Rect(0, 0, 400, 300), seed=3)
    assert plan.trajectory.samples[0].point == Point(40, 120)
    with pytest.raises(ExecutorError):
        MotionProfile(min_duration=0.01, max_duration=0.01, sample_hz=10)
    with pytest.raises(ExecutorError):
        PointerSample(0.1, Point(1.5, 2))


def test_click_plan_uses_safe_point_semantic_button_and_bounded_dwell(target: PointerTarget) -> None:
    client = Rect(0, 0, 400, 300)
    profile = MotionProfile(min_dwell=0.1, max_dwell=0.2)
    left = plan_human_pointer(target.safe_point, target, client, seed=12, profile=profile)
    assert left.click is not None
    assert left.click.point == left.endpoint
    assert left.click.button is PointerButton.LEFT
    assert target.safe_bounds.contains(left.click.point)
    assert 0.1 <= left.click.dwell <= 0.2

    right = plan_human_pointer(
        Point(40, 120), target, client, seed=12, button=PointerButton.RIGHT, profile=profile
    )
    assert right.click is not None and right.click.button is PointerButton.RIGHT
    no_click = plan_human_pointer(Point(40, 120), target, client, seed=12, button=None)
    assert no_click.click is None
    with pytest.raises(PointerExecutionError, match="click button"):
        plan_human_pointer(Point(40, 120), target, client, seed=12, button="left")  # type: ignore[arg-type]


def test_rect_uses_inclusive_pixels_and_pointer_requires_verified_client_bounds(
    binding: BindingMetadata,
) -> None:
    client = Rect(0, 0, 4, 3)
    assert client.contains(Point(3, 2))
    assert not client.contains(Point(4, 2))
    assert not client.contains(Point(3, 3))
    assert client.clip(Point(4, 3)) == Point(3, 2)
    edge_target = PointerTarget("menu", Rect(0, 0, 4, 3), binding)
    assert edge_target.safe_bounds.contains(edge_target.safe_point)
    outside = PointerTarget("outside", Rect(390, 290, 20, 20), binding)
    with pytest.raises(PointerExecutionError, match="outside"):
        plan_human_pointer(Point(4, 1), outside, client, seed=7)
