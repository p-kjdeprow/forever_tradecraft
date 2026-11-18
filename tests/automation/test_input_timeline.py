"""Offline timing and transport tests; no desktop, physical reader or virtual device."""

from __future__ import annotations

import math

import pytest
from crowley_automation.gui.control_child import ControlChildError, _classify
from crowley_automation.gui.keyboard import KeyboardError
from crowley_automation.gui.timeline import KeyEvent, Timeline, plan_text, validate_timed_keys
from crowley_automation.gui.uinput_worker import parse_events


def test_seeded_cadence_is_bounded_correlated_and_mapping_is_validated_upfront():
    plan = plan_text("/Ad a", seed=42)
    assert plan == plan_text("/Ad a", seed=42)
    assert plan != plan_text("/Ad a", seed=43)
    args, held = plan.xtest({"slash": 61, "a": 38, "d": 40, "space": 65, "Shift_L": 50})
    assert held == {"061", "038", "040", "065", "050"}
    assert _classify("xdotool", tuple(args[1:])) == "key"
    assert validate_timed_keys(tuple(args[1:])) == pytest.approx(plan.duration, abs=1e-8)
    at = 0.0
    transitions = []
    for i in range(1, len(args), 2):
        if args[i] == "sleep":
            at += float(args[i + 1])
        else:
            transitions.append((at, args[i], args[i + 1]))
    assert transitions[0][1:] == ("keydown", "061")
    # Shift nests around A; its release follows the letter release.
    shifted = [(phase, code) for _, phase, code in transitions][2:6]
    assert shifted == [("keydown", "050"), ("keydown", "038"), ("keyup", "038"), ("keyup", "050")]
    for i, event in enumerate(plan.events):
        if event.phase == "press" and event.key != "Shift_L":
            release = next(e for e in plan.events[i + 1 :] if e.phase == "release" and e.key == event.key)
            assert 0.016 <= release.at - event.at <= 0.0280001
    with pytest.raises(KeyboardError, match="measured keycode"):
        plan.xtest({"slash": 61})


def test_command_budget_does_not_scale_capture_or_process_count():
    # Actual process/capture counts are covered at both callers. This checks the
    # declared cadence does not turn the scanner's 123-character binding into a
    # long command that requires renewed authorization.
    durations = [plan_text("/" + "a" * 122, seed=i).duration for i in range(50)]
    assert min(durations) > 3
    assert max(durations) < 8
    assert all(plan_text("/" + "a" * 122, seed=i).timeout < 11 for i in range(50))
    assert plan_text("", seed=1).events == ()


@pytest.mark.parametrize("text", ["a\n", "a\t", "a😀", "a" * 257])
def test_invalid_full_text_is_rejected_before_any_emission(text):
    with pytest.raises(KeyboardError):
        plan_text(text)


@pytest.mark.parametrize(
    "events,duration",
    [
        ((KeyEvent(0, "press", "a"),), 0.1),
        ((KeyEvent(0.2, "press", "a"), KeyEvent(0.1, "release", "a")), 0.3),
        ((KeyEvent(0, "release", "a"),), 0.1),
        ((KeyEvent(0, "press", "a"), KeyEvent(0.1, "release", "b")), 0.2),
        ((), math.nan),
        ((), True),
        ((), 21),
    ],
)
def test_timeline_rejects_unbalanced_or_unbounded_input(events, duration):
    with pytest.raises(KeyboardError):
        Timeline(events, duration, 1)


@pytest.mark.parametrize(
    "args",
    [
        ("keydown", "038", "sleep", "nan", "keyup", "038"),
        ("keydown", "038", "sleep", "21", "keyup", "038"),
        ("keydown", "038", "keydown", "038", "keyup", "038"),
        ("keydown", "038", "keyup", "039"),
        ("keydown", "038", "type", "hello", "keyup", "038"),
    ],
)
def test_control_parser_rejects_invalid_timed_jobs(args):
    with pytest.raises(ControlChildError):
        _classify("xdotool", args)


def test_uinput_uses_explicit_mapping_and_validates_before_first_event():
    plan = plan_text("a", seed=1)
    args, _ = plan.xtest({"a": 38})
    events, duration = parse_events(tuple(args[1:]), {"38": 30})
    assert events[0] == (0, "keydown", 30)
    assert events[1][1:] == ("keyup", 30)
    assert duration == pytest.approx(plan.duration, abs=1e-8)
    with pytest.raises(RuntimeError, match="qualified evdev mapping"):
        parse_events(tuple(args[1:]), {})
