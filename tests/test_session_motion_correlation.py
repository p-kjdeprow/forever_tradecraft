"""Guardian correlation tests for the retained native hover prefix.

The four source-4 events below and the undelimited final Motion are copied from
the retained observer diagnostic. Modeled source-7 events in the completion and
negative tests exercise the matcher only; they are not additional native
evidence from that attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session.guardian import GuardianError, InputGuardian
from crowley.session.guardian.correlation import ActionCorrelation
from crowley.session.input_events import EventClass, InputEventError, XI2Event, XI2StreamParser

_RETAINED_RAW_BLOCKS = (
    "⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]\n"
    "⎜   ↳ Virtual core XTEST pointer              \tid=4\t[slave  pointer  (2)]\n"
    "⎜   ↳ xwayland-pointer:17                     \tid=6\t[slave  pointer  (2)]\n"
    "⎜   ↳ xwayland-relative-pointer:17            \tid=7\t[slave  pointer  (2)]\n"
    "⎜   ↳ xwayland-pointer-gestures:17            \tid=8\t[slave  pointer  (2)]\n"
    "⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]\n"
    "    ↳ Virtual core XTEST keyboard             \tid=5\t[slave  keyboard (3)]\n"
    "    ↳ xwayland-keyboard:17                    \tid=9\t[slave  keyboard (3)]\n",
    "EVENT type 17 (RawMotion)\n"
    "    device: 2 (4)\n"
    "    time:   527685625\n"
    "    detail: 0\n"
    "    flags: \n"
    "    valuators:\n"
    "          0: 1029.00 (1029.00)\n"
    "          1: 340.00 (340.00)\n\n",
    "EVENT type 6 (Motion)\n"
    "    device: 4 (4)\n"
    "    time: 527685625\n"
    "    detail: 0\n"
    "    flags: \n"
    "    root: 1030.00/340.00\n"
    "    event: 1030.00/340.00\n"
    "    buttons:\n"
    "    modifiers: locked 0x10 latched 0 base 0 effective: 0x10\n"
    "    group: locked 0 latched 0 base 0 effective: 0\n"
    "    valuators:\n"
    "        0: 1029.00\n"
    "        1: 340.00\n"
    "    windows: root 0x374 event 0x374 child 0x3c00003\n",
    "EVENT type 17 (RawMotion)\n"
    "    device: 2 (4)\n"
    "    time:   527685658\n"
    "    detail: 0\n"
    "    flags: \n"
    "    valuators:\n"
    "          0: 1028.00 (1028.00)\n"
    "          1: 340.00 (340.00)\n\n",
)
_RETAINED_PARTIAL_TAIL = (
    "EVENT type 6 (Motion)\n"
    "    device: 4 (4)\n"
    "    time: 527685658\n"
    "    detail: 0\n"
    "    flags: \n"
    "    root: 1029.00/340.00\n"
    "    event: 1029.00/340.00\n"
    "    buttons:\n"
    "    modifiers: locked 0x10 latched 0 base 0 effective: 0x10\n"
    "    group: locked 0 latched 0 base 0 effective: 0\n"
    "    valuators:\n"
    "        0: 1028.00\n"
    "        1: 340.00\n"
    "    windows: root 0x374 event 0x374 child 0x3c00003\n"
)
_RETAINED_RAW_BLOCK_PINS = (
    (608, "02c9507265b87e126f0be0eb194d38f3b4974500e0982937f3c984666c77f6e6"),
    (168, "2d990544540f224841e564e348c0863102b242571fc6a30a76333be13e191d5d"),
    (364, "fd595440f00099dcf1ea266a5892cfbbb41b6e7f29a8971775d6578c91dbd548"),
    (168, "70844ecc686cfc25a035d0cdc49b4a0d325e9fe1048f8185e542f7f7b88b4c78"),
)
_RETAINED_PARTIAL_TAIL_PIN = (
    364,
    "30979cc2b004ef03af4d1156955b9e9f2c9885c2cc697e9049e0d93656ff3486",
)
_TARGET = 0x3C00003
_CERTIFIED_EVENT_XIDS = frozenset({0x300002A, _TARGET})
_MOTION_ENDPOINTS = (
    (1029, 340),
    (1028, 340),
    (1026, 340),
    (1025, 340),
    (1023, 340),
    (1021, 339),
    (1020, 339),
    (1018, 338),
    (1017, 337),
    (1016, 336),
    (1015, 336),
    (1014, 335),
    (1013, 335),
    (1012, 335),
    (1011, 335),
    (1010, 334),
    (1009, 334),
    (1007, 333),
    (1006, 333),
    (1005, 332),
    (1004, 331),
    (1003, 330),
    (1002, 330),
    (1002, 329),
    (1001, 329),
    (999, 328),
    (998, 328),
    (996, 327),
    (995, 327),
    (993, 327),
    (992, 327),
    (991, 327),
    (990, 327),
    (989, 327),
)
_EXPECTED_MOTION_TRACE = tuple((x, y, x, y) for x, y in _MOTION_ENDPOINTS)
_ECHO_SOURCES = frozenset({7})


def _parse_retained_prefix() -> list[XI2Event]:
    for block, (expected_length, expected_digest) in zip(
        _RETAINED_RAW_BLOCKS, _RETAINED_RAW_BLOCK_PINS, strict=True
    ):
        encoded = block.encode("utf-8")
        assert len(encoded) == expected_length
        assert hashlib.sha256(encoded).hexdigest() == expected_digest
    partial_tail = _RETAINED_PARTIAL_TAIL.encode("utf-8")
    assert len(partial_tail) == _RETAINED_PARTIAL_TAIL_PIN[0]
    assert hashlib.sha256(partial_tail).hexdigest() == _RETAINED_PARTIAL_TAIL_PIN[1]
    parser = XI2StreamParser()
    events: list[XI2Event] = []
    assert not _RETAINED_PARTIAL_TAIL.endswith("\n\n")
    for block in (*_RETAINED_RAW_BLOCKS, _RETAINED_PARTIAL_TAIL):
        for line in block.splitlines(keepends=True):
            event = parser.feed(line)
            if event is not None:
                events.append(event)
    assert parser.failure_diagnostic is None
    assert len(events) == 4
    return events


def _begin_canonical_hover(
    tmp_path: Path,
    name: str,
    *,
    expected_motion_trace: tuple[tuple[int, int, int, int], ...] = _EXPECTED_MOTION_TRACE,
) -> tuple[InputGuardian, int, object]:
    guardian = InputGuardian(tmp_path / f"{name}.lock", parent_pid=os.getppid())
    generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="retained-hover-identity"))
    event_classes = {"DeviceChanged", "RawMotion", "Motion", "Enter", "Leave"}
    action = guardian.begin_action(
        generation,
        target_xid=0x300002A,
        source_device=4,
        master_device=2,
        event_classes=event_classes,
        device_by_class={
            event_class: 2 if event_class in {"DeviceChanged", "RawMotion"} else 4
            for event_class in event_classes
        },
        optional_classes={"DeviceChanged", "Enter", "Leave"},
        target_ancestor=lambda xid: xid in _CERTIFIED_EVENT_XIDS,
        expected_modifiers=(0x10, 0, 0),
        expected_motion_trace=expected_motion_trace,
        initial_origin=(1030, 340, 1030, 340),
        expected_root_xid=0x374,
        expected_event_xid=0x374,
        expected_child_xids=_CERTIFIED_EVENT_XIDS,
        canonical_motion=True,
        echo_sources=_ECHO_SOURCES,
        timeout=5.0,
    )
    return guardian, generation, action


def _modeled_echo(
    primary: XI2Event,
    *,
    timestamp_ms: int,
    source_device: int = 7,
    master_device: int = 7,
    target: int = _TARGET,
    root_xid: int = 0x374,
    event_xid: int = 0x374,
    root_xy: tuple[float, float] | None = None,
    event_xy: tuple[float, float] | None = None,
    modifiers: tuple[int, int, int] | None = None,
) -> XI2Event:
    endpoint = tuple(
        float(value) for axis in (0, 1) for row_axis, value, _raw in primary.valuators if row_axis == axis
    )
    assert len(endpoint) == 2
    return XI2Event(
        6,
        EventClass.MOTION,
        master_device,
        source_device,
        timestamp_ms,
        0,
        root_xid,
        event_xid,
        target,
        endpoint if root_xy is None else root_xy,
        endpoint if event_xy is None else event_xy,
        primary.modifiers if modifiers is None else modifiers,
        modifier_effective=(
            (primary.modifier_effective if modifiers is None else 0)
            if modifiers is None or modifiers == primary.modifiers
            else modifiers[0] | modifiers[1] | modifiers[2]
        ),
    )


def _observe_retained_prefix(guardian: InputGuardian, events: list[XI2Event]) -> None:
    for event in events:
        assert guardian.observe(event), (event.event_class.value, guardian.revoked_reason)


def test_retained_native_partial_tail_advances_primary_fifo_but_never_completes_without_echoes(
    tmp_path: Path,
) -> None:
    """Replay the exact native prefix through the parser and production Guardian."""

    events = _parse_retained_prefix()
    assert [
        (event.event_class.value, event.source_device, event.master_device, event.timestamp_ms)
        for event in events
    ] == [
        ("RawMotion", 4, 2, 527685625),
        ("Motion", 4, 4, 527685625),
        ("RawMotion", 4, 2, 527685658),
        ("Motion", 4, 4, 527685658),
    ]
    assert events[3].target_xid == _TARGET
    assert events[3].valuators == ((0, 1028.0, None), (1, 340.0, None))

    guardian, _generation, action = _begin_canonical_hover(tmp_path, "retained-prefix")
    try:
        _observe_retained_prefix(guardian, events)
        assert guardian.armed
        assert action.motion_index == 2
        assert action.emitted_motion_count == 2
        assert action.echo_index == 0
        assert len(action.pending_echoes) == 2
        assert action.raw_motion_pairing.pending_timestamps == []
        assert not action.complete
    finally:
        guardian.close()


def test_retained_prefix_completes_only_after_bounded_fifo_echo_pairs(tmp_path: Path) -> None:
    """Modeled echoes test completion semantics, not the retained host trace."""

    events = _parse_retained_prefix()
    guardian, generation, action = _begin_canonical_hover(
        tmp_path,
        "modeled-completion",
        expected_motion_trace=_EXPECTED_MOTION_TRACE[:2],
    )
    try:
        _observe_retained_prefix(guardian, events)
        first_echo = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2)
        second_echo = _modeled_echo(events[3], timestamp_ms=events[3].timestamp_ms + 10)
        assert guardian.observe(first_echo)
        assert action.echo_index == 1
        assert len(action.pending_echoes) == 1
        assert action.complete
        assert guardian.observe(second_echo)
        assert action.echo_index == 2
        assert not action.pending_echoes
        assert action.complete
        guardian.finish_action(generation, action)
    finally:
        guardian.close()


@pytest.mark.parametrize(
    "variant",
    (
        "unsolicited",
        "duplicate",
        "reordered",
        "late",
        "wrong-source",
        "wrong-master",
        "wrong-target",
        "wrong-root",
        "wrong-event-window",
        "wrong-endpoint",
        "wrong-modifiers",
        "unknown-physical-source",
    ),
)
def test_retained_hover_echo_contract_rejects_unpaired_or_mismatched_motion(
    tmp_path: Path,
    variant: str,
) -> None:
    events = _parse_retained_prefix()
    guardian, _generation, action = _begin_canonical_hover(tmp_path, f"negative-{variant}")
    try:
        if variant == "unsolicited":
            event = _modeled_echo(events[1], timestamp_ms=events[1].timestamp_ms + 1)
        else:
            _observe_retained_prefix(guardian, events)
            first_echo = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2)
            second_echo = _modeled_echo(events[3], timestamp_ms=events[3].timestamp_ms + 10)
            if variant == "duplicate":
                assert guardian.observe(first_echo)
                event = first_echo
            elif variant == "reordered":
                event = second_echo
            elif variant == "late":
                event = _modeled_echo(events[1], timestamp_ms=events[1].timestamp_ms + 251)
            elif variant == "wrong-source":
                event = _modeled_echo(
                    events[1], timestamp_ms=events[3].timestamp_ms + 2, source_device=8, master_device=8
                )
            elif variant == "wrong-master":
                event = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2, master_device=2)
            elif variant == "wrong-target":
                event = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2, target=_TARGET + 1)
            elif variant == "wrong-root":
                event = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2, root_xid=0x375)
            elif variant == "wrong-event-window":
                event = _modeled_echo(events[1], timestamp_ms=events[3].timestamp_ms + 2, event_xid=0x375)
            elif variant == "wrong-endpoint":
                event = _modeled_echo(
                    events[1],
                    timestamp_ms=events[3].timestamp_ms + 2,
                    root_xy=(1030.0, 340.0),
                )
            elif variant == "wrong-modifiers":
                event = _modeled_echo(
                    events[1], timestamp_ms=events[3].timestamp_ms + 2, modifiers=(0x10, 1, 0)
                )
            else:
                event = events[3].__class__(
                    events[3].type_number,
                    events[3].event_class,
                    2,
                    9,
                    events[3].timestamp_ms + 1,
                    events[3].detail,
                    events[3].root_xid,
                    events[3].event_xid,
                    events[3].child_xid,
                    events[3].root_xy,
                    events[3].event_xy,
                    events[3].modifiers,
                    modifier_effective=events[3].modifier_effective,
                    valuators=events[3].valuators,
                )
        if variant == "reordered":
            assert guardian.observe(event)
            assert guardian.armed
            assert action.echo_index == 1
        else:
            assert not guardian.observe(event)
            assert not guardian.armed
            assert guardian.revoked_reason.startswith("unattributed XI2 event class=Motion")
    finally:
        guardian.close()


def _grace_event(
    *,
    source_device: int,
    master_device: int,
    timestamp_ms: int,
    root_xy: tuple[float, float] = (10.0, 20.0),
    event_xy: tuple[float, float] = (10.0, 20.0),
) -> SimpleNamespace:
    return SimpleNamespace(
        event_class="Motion",
        source_device=source_device,
        master_device=master_device,
        target_xid=0x700101,
        detail=0,
        timestamp_ms=timestamp_ms,
        modifiers=(0, 0, 0),
        modifier_effective=0,
        is_raw=False,
        root_xy=root_xy,
        event_xy=event_xy,
    )


def _parsed_grace_event(*, effective: str | None = "0x0") -> XI2Event:
    modifier_line = "    modifiers: locked 0x0 latched 0 base 0"
    if effective is not None:
        modifier_line += f" effective: {effective}"
    trace = f"""EVENT type 6 (Motion)
    device: 7 (7)
    time: 101
    detail: 0
    root: 10.00/20.00
    event: 10.00/20.00
{modifier_line}
    windows: root 0x1 event 0x1 child 0x700101
"""
    parser = XI2StreamParser()
    event: XI2Event | None = None
    for line in trace.splitlines(keepends=True):
        parsed = parser.feed(line)
        if parsed is not None:
            event = parsed
    if event is None:
        event = parser.finish()
    assert parser.failure_diagnostic is None
    assert event is not None
    return event


def _begin_grace_action(
    tmp_path: Path,
    *,
    timeout: float = 10.0,
    clock: Callable[[], float] | None = None,
) -> tuple[InputGuardian, int, list[float]]:
    now = [0.0]
    monotonic = (lambda: now[0]) if clock is None else clock
    guardian = InputGuardian(tmp_path / "grace.lock", parent_pid=os.getppid(), monotonic=monotonic)
    generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="grace"))
    guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=2,
        event_classes={"Motion"},
        device_by_class={"Motion": 4},
        optional_classes={"Motion"},
        expected_modifiers=(0, 0, 0),
        expected_motion_trace=((10, 20, 10, 20),),
        echo_sources={7},
        timeout=timeout,
    )
    assert guardian.observe(_grace_event(source_device=4, master_device=4, timestamp_ms=100))
    return guardian, generation, now


@pytest.mark.parametrize("variant", ("matched", "exact", "expired", "mismatched", "unrelated"))
def test_forwarded_echo_is_optional_but_bounded_after_completion(tmp_path: Path, variant: str) -> None:
    guardian, generation, now = _begin_grace_action(tmp_path)
    action = guardian.action
    assert action is not None and action.complete
    guardian.finish_action(generation, action)
    try:
        if variant == "matched":
            assert guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
            assert guardian.armed
        elif variant in {"exact", "expired"}:
            now[0] = 0.25 if variant == "exact" else 0.251
            assert not guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
            assert not guardian.armed
        elif variant == "mismatched":
            assert not guardian.observe(
                _grace_event(source_device=7, master_device=7, timestamp_ms=101, root_xy=(11.0, 20.0))
            )
            assert not guardian.armed
        else:
            assert not guardian.observe(_grace_event(source_device=8, master_device=8, timestamp_ms=101))
            assert not guardian.armed
    finally:
        guardian.close()


@pytest.mark.parametrize("variant", ("missing", "negative", "inconsistent"))
def test_parser_echo_with_invalid_delivered_effective_modifiers_revokes(tmp_path: Path, variant: str) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action = guardian.action
    assert action is not None and action.complete
    guardian.finish_action(generation, action)
    parsed = _parsed_grace_event(effective="0x1" if variant == "inconsistent" else "0x0")
    if variant == "missing":
        parsed = replace(parsed, modifier_effective=None)
    elif variant == "negative":
        parsed = replace(parsed, modifier_effective=-1)
    try:
        assert not guardian.observe(parsed)
        assert not guardian.armed
    finally:
        guardian.close()


def test_parser_rejects_missing_motion_effective_modifier_state() -> None:
    with pytest.raises(InputEventError, match="missing effective modifier state"):
        _parsed_grace_event(effective=None)


def test_late_echo_from_action_a_is_consumed_after_action_b_begins(tmp_path: Path) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action_a = guardian.action
    assert action_a is not None and action_a.complete
    guardian.finish_action(generation, action_a)
    try:
        action_b = guardian.begin_action(
            generation,
            target_xid=0x700101,
            source_device=4,
            master_device=4,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            optional_classes={"Motion"},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((30, 40, 30, 40),),
            echo_sources={7},
        )
        assert guardian.observe(
            _grace_event(
                source_device=4,
                master_device=4,
                timestamp_ms=200,
                root_xy=(30.0, 40.0),
                event_xy=(30.0, 40.0),
            )
        )
        assert action_b.echo_index == 0
        assert len(action_b.pending_echoes) == 1
        assert guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
        assert action_b.echo_index == 0
        assert len(action_b.pending_echoes) == 1
        assert guardian.observe(
            _grace_event(
                source_device=7,
                master_device=7,
                timestamp_ms=201,
                root_xy=(30.0, 40.0),
                event_xy=(30.0, 40.0),
            )
        )
        assert action_b.echo_index == 1
        assert not action_b.pending_echoes
        guardian.finish_action(generation, action_b)
    finally:
        guardian.close()


def test_echo_source_matching_neither_current_or_grace_expectation_revokes(tmp_path: Path) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action = guardian.action
    assert action is not None
    assert action.complete
    guardian.finish_action(generation, action)
    try:
        action_b = guardian.begin_action(
            generation,
            target_xid=0x700101,
            source_device=4,
            master_device=4,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            optional_classes={"Motion"},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((30, 40, 30, 40),),
            echo_sources={7},
        )
        del action_b
        assert not guardian.observe(
            _grace_event(
                source_device=7,
                master_device=7,
                timestamp_ms=101,
                root_xy=(99.0, 99.0),
                event_xy=(99.0, 99.0),
            )
        )
        assert not guardian.armed
    finally:
        guardian.close()


def test_echo_survives_finish_and_next_begin_during_observation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action_a = guardian.action
    assert action_a is not None and action_a.complete
    snapshot_barrier = threading.Barrier(2)
    continue_observation = threading.Event()
    result: list[bool] = []
    errors: list[BaseException] = []
    original_target_phase = guardian._observe_target_phase
    paused = True

    def pause_after_snapshot(observation) -> None:  # type: ignore[no-untyped-def]
        nonlocal paused
        if paused:
            paused = False
            snapshot_barrier.wait(timeout=1.0)
            assert continue_observation.wait(timeout=1.0)
        original_target_phase(observation)

    monkeypatch.setattr(guardian, "_observe_target_phase", pause_after_snapshot)

    def observe_echo() -> None:
        try:
            result.append(guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101)))
        except BaseException as exc:  # pragma: no cover - assertion below reports the exception
            errors.append(exc)

    observer = threading.Thread(target=observe_echo)
    observer.start()
    snapshot_barrier.wait(timeout=1.0)
    guardian.finish_action(generation, action_a)
    action_b = guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=4,
        event_classes={"Motion"},
        device_by_class={"Motion": 4},
        optional_classes={"Motion"},
        expected_modifiers=(0, 0, 0),
        expected_motion_trace=((30, 40, 30, 40),),
        echo_sources={7},
    )
    try:
        continue_observation.set()
        observer.join(timeout=1.0)
        assert not observer.is_alive()
        assert errors == []
        assert result == [True]
        assert guardian.armed
        assert guardian.action is action_b
        assert guardian.observe(
            _grace_event(
                source_device=4,
                master_device=4,
                timestamp_ms=200,
                root_xy=(30.0, 40.0),
                event_xy=(30.0, 40.0),
            )
        )
        assert guardian.observe(
            _grace_event(
                source_device=7,
                master_device=7,
                timestamp_ms=201,
                root_xy=(30.0, 40.0),
                event_xy=(30.0, 40.0),
            )
        )
        guardian.finish_action(generation, action_b)
    finally:
        continue_observation.set()
        observer.join(timeout=1.0)
        guardian.close()


def test_echo_survives_finish_and_next_begin_after_action_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guardian, generation, now = _begin_grace_action(tmp_path, timeout=0.25)
    action_a = guardian.action
    assert action_a is not None and action_a.complete
    snapshot_barrier = threading.Barrier(2)
    continue_observation = threading.Event()
    result: list[bool] = []
    errors: list[BaseException] = []
    original_target_phase = guardian._observe_target_phase
    paused = True

    def pause_after_snapshot(observation) -> None:  # type: ignore[no-untyped-def]
        nonlocal paused
        if paused:
            paused = False
            snapshot_barrier.wait(timeout=1.0)
            assert continue_observation.wait(timeout=1.0)
        original_target_phase(observation)

    monkeypatch.setattr(guardian, "_observe_target_phase", pause_after_snapshot)

    def observe_echo() -> None:
        try:
            result.append(guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101)))
        except BaseException as exc:  # pragma: no cover - assertion below reports the exception
            errors.append(exc)

    observer = threading.Thread(target=observe_echo)
    observer.start()
    snapshot_barrier.wait(timeout=1.0)
    now[0] = 0.30
    guardian.finish_action(generation, action_a)
    action_b = guardian.begin_action(
        generation,
        target_xid=0x700101,
        source_device=4,
        master_device=4,
        event_classes={"Motion"},
        device_by_class={"Motion": 4},
        optional_classes={"Motion"},
        expected_modifiers=(0, 0, 0),
        expected_motion_trace=((30, 40, 30, 40),),
        echo_sources={7},
    )
    try:
        continue_observation.set()
        observer.join(timeout=1.0)
        assert not observer.is_alive()
        assert errors == []
        assert result == [True]
        assert guardian.armed
        assert guardian.action is action_b
        assert action_b.echo_index == 0
        assert not action_b.pending_echoes
    finally:
        continue_observation.set()
        observer.join(timeout=1.0)
        guardian.revoke("test cleanup")
        guardian.close()


def test_grace_is_cleared_on_revoke_and_new_arm(tmp_path: Path) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action = guardian.action
    assert action is not None
    guardian.finish_action(generation, action)
    try:
        new_generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="new-generation"))
        assert new_generation != generation
        guardian.begin_action(
            new_generation,
            target_xid=0x700101,
            source_device=4,
            master_device=4,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            optional_classes={"Motion"},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((10, 20, 10, 20),),
            echo_sources={7},
        )
        assert not guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
        assert not guardian.armed
    finally:
        guardian.close()


def test_grace_is_cleared_by_revoke_before_a_later_arm(tmp_path: Path) -> None:
    guardian, generation, _now = _begin_grace_action(tmp_path)
    action = guardian.action
    assert action is not None
    guardian.finish_action(generation, action)
    guardian.revoke("test grace cleanup")
    try:
        new_generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="after-revoke"))
        guardian.begin_action(
            new_generation,
            target_xid=0x700101,
            source_device=4,
            master_device=4,
            event_classes={"Motion"},
            device_by_class={"Motion": 4},
            optional_classes={"Motion"},
            expected_modifiers=(0, 0, 0),
            expected_motion_trace=((10, 20, 10, 20),),
            echo_sources={7},
        )
        assert not guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
        assert not guardian.armed
    finally:
        guardian.close()


def test_competing_begin_actions_keep_single_action_invariant(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "competing.lock", parent_pid=os.getppid())
    generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="competing"))
    barrier = threading.Barrier(3)
    results: list[ActionCorrelation] = []
    errors: list[BaseException] = []

    def begin() -> None:
        try:
            barrier.wait(timeout=1.0)
            results.append(
                guardian.begin_action(
                    generation,
                    target_xid=0x700101,
                    source_device=4,
                    master_device=4,
                    event_classes={"Motion"},
                    device_by_class={"Motion": 4},
                    optional_classes={"Motion"},
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports the exception
            errors.append(exc)

    workers = [threading.Thread(target=begin) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=1.0)
    for worker in workers:
        worker.join(timeout=1.0)
    try:
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], GuardianError)
        assert str(errors[0]) == "another input action is already held"
    finally:
        guardian.revoke("test cleanup")
        guardian.close()


def test_echo_grace_uses_completion_time_not_action_deadline(tmp_path: Path) -> None:
    guardian, generation, now = _begin_grace_action(tmp_path, timeout=0.25)
    action = guardian.action
    assert action is not None and action.complete
    now[0] = 0.249
    guardian.finish_action(generation, action)
    try:
        now[0] = 0.30
        assert guardian.observe(_grace_event(source_device=7, master_device=7, timestamp_ms=101))
        assert guardian.armed
    finally:
        guardian.close()


def _fixture_events(name: str) -> tuple[dict[str, object], list[XI2Event]]:
    fixture = json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    parser = XI2StreamParser()
    events: list[XI2Event] = []
    observer = fixture["observer"]
    assert isinstance(observer, dict)
    blocks = observer["rawBlocks"]
    assert isinstance(blocks, list)
    partial = observer["partialRaw"]
    assert isinstance(partial, str)
    for block in [*blocks, f"{partial}\n"]:
        assert isinstance(block, str)
        for line in block.splitlines(keepends=True):
            event = parser.feed(line)
            if event is not None:
                events.append(event)
    assert parser.failure_diagnostic is None
    return fixture, events


def _replay_fixture_action(
    tmp_path: Path, name: str
) -> tuple[InputGuardian, int, object, dict[str, object], list[XI2Event]]:
    fixture, events = _fixture_events(name)
    expected = fixture["expectedAction"]
    binding = fixture["inputBinding"]
    assert isinstance(expected, dict)
    assert isinstance(binding, dict)
    motion = next(event for event in events if event.event_class is EventClass.MOTION)
    event_classes = expected["eventClasses"]
    target_event_xids = expected["targetEventXids"]
    expected_trace = expected["expectedMotionTrace"]
    echo_sources = binding["forwardedEchoSources"]
    assert isinstance(event_classes, list)
    assert isinstance(target_event_xids, list)
    assert isinstance(expected_trace, list)
    assert isinstance(echo_sources, list)
    target_xid = expected["targetXid"]
    source_device = expected["sourceDevice"]
    master_device = expected["masterDevice"]
    assert all(
        isinstance(value, int) for value in (*target_event_xids, target_xid, source_device, master_device)
    )
    guardian = InputGuardian(tmp_path / f"{name}.lock", parent_pid=os.getppid())
    generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint=name))
    action = guardian.begin_action(
        generation,
        target_xid=target_xid,
        source_device=source_device,
        master_device=master_device,
        event_classes=set(event_classes),
        device_by_class={"DeviceChanged": 2, "RawMotion": 2, "Motion": 4, "Enter": 4, "Leave": 4},
        optional_classes={"DeviceChanged", "Enter", "Leave"},
        target_ancestor=lambda xid: xid in set(target_event_xids),
        expected_modifiers=motion.modifiers,
        expected_motion_trace=tuple(tuple(row) for row in expected_trace),
        initial_origin=(*map(int, motion.root_xy), *map(int, motion.event_xy)),
        expected_root_xid=motion.root_xid,
        expected_event_xid=motion.event_xid,
        expected_child_xids=frozenset(target_event_xids),
        canonical_motion=True,
        echo_sources=frozenset(echo_sources),
        timeout=10.0,
    )
    return guardian, generation, action, fixture, events


def test_forever_hover_fixture_completes_without_relative_pointer_echo(tmp_path: Path) -> None:
    guardian, generation, action, _fixture, events = _replay_fixture_action(
        tmp_path, "forever-hover-no-echo-20261108"
    )
    try:
        for event in events:
            assert guardian.observe(event), (event.event_class.value, guardian.revoked_reason)
        assert action.complete
        assert action.emitted_motion_count == 37
        assert len(action.pending_echoes) == 37
        guardian.finish_action(generation, action)
        assert guardian.armed
    finally:
        guardian.close()


def test_beta_hover_fixture_accepts_next_primary_while_echo_is_pending(tmp_path: Path) -> None:
    guardian, _generation, action, _fixture, events = _replay_fixture_action(
        tmp_path, "beta-hover-ordering-20261023"
    )
    try:
        for event in events:
            assert guardian.observe(event), (event.event_class.value, guardian.revoked_reason)
        assert guardian.armed
        assert action.motion_index == 2
        assert len(action.pending_echoes) == 2
    finally:
        guardian.close()
