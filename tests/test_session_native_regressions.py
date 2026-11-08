"""Focused native boundary regressions.

These fixtures exercise the owned calibration and X11 boundary with fake
processes, clocks, and observers.  They do not claim that a real game client
or a human input sample has passed calibration.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import crowley.session.daemon as daemon_module
from crowley.session.calibration import (
    NATIVE_CONSTRUCTION_LIMIT_BYTES,
    CalibrationError,
    InputCalibrationEvidence,
    InputCalibrationWorkflow,
    KeyboardProbeOperation,
    KeyboardProbeStep,
    LinuxProcStatusVmHwmReset5,
    NativeCalibrationEvidence,
    NativeCalibrationRunner,
    NativeMemoryArchiveReceipt,
    NativeMemoryAssessmentArchiveReceipt,
    NativeMemoryMeasurement,
    NativePreconstructionEvidence,
    NativeRenderObservation,
    NativeRestorationReceipt,
    NativeResumeBoundary,
    OwnedGtkCalibrationWindow,
    OwnedWindow,
    XTestInputProfile,
    xtest_sources,
)
from crowley.session.cli import SessionIPC
from crowley.session.controller import Controller, ControllerState
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import PixelDecoder
from crowley.session.driver import (
    _EXEC_ADMISSION_WRAPPER,
    DesktopDriver,
    DriverError,
    DriverProfile,
    InputMonitor,
    MeasuredAnchor,
    RecordingBackend,
    SyntheticPointerProbe,
    WindowIdentity,
    X11Backend,
)
from crowley.session.guardian import GuardianError, InputGuardian
from crowley.session.input_events import EventClass, XI2Event, parse_xi2_records
from crowley.session.journal import DEFAULT_STATE, Journal, JournalError
from crowley.session.protocol import HEADER_SIZE, ProtocolError, encode_frame, pack_cells
from tests.support.config import fixture_codex_path, fixture_session_config
from tests.support.ewmh_window_manager import (
    start_ewmh_window_manager,
    stop_ewmh_window_manager,
)
from tests.support.private_lease import private_lease_path
from tests.test_session_input_tool import fixture_input_tool
from tests.x11_startup import start_private_xvfb

TARGET = 0x600119
FOREIGN = 0x1A00011
TARGET_DESCENDANT = 0x60011A
TARGET_FRAME = 0x60011B
NATIVE_ABSENCE_EVIDENCE = Path(__file__).parent / "fixtures" / "evidence" / "native_absence"


def _xtest_profile(
    hierarchy: tuple[tuple[int, int, str, str], ...],
    *,
    source: int = 4,
    parent: int = 2,
    root: int = 0x374,
) -> XTestInputProfile:
    return XTestInputProfile(
        source,
        parent,
        next((row[3] for row in hierarchy if row[0] == source), "XTEST pointer"),
        3,
        ((0, "Rel X", "relative", -1.0, -1.0, 0), (1, "Rel Y", "relative", -1.0, -1.0, 0)),
        1,
        1,
        (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        root,
        (0, 0, 1920, 1080),
        hierarchy,
    )


def _mutter_gtk_fixture_command(
    *,
    pid: int,
    target: int,
    frame: int,
    helper: int,
    child: int,
    frame_pid: int = 5717,
    frame_for: int | None = None,
    advertised_helper: int | None = None,
    extra_frame_child: int | None = None,
    helper_stats: str | None = None,
    frame_props: str | None = None,
    helper_props: str | None = None,
):
    """Build the captured Mutter certificate command surface for GTK tests."""

    frame_for = target if frame_for is None else frame_for
    advertised_helper = helper if advertised_helper is None else advertised_helper

    def stats(x: int, y: int, width: int, height: int) -> str:
        return f"Absolute upper-left X: {x}\nAbsolute upper-left Y: {y}\nWidth: {width}\nHeight: {height}\n"

    def command(args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return f"{hex(target)}  -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        if args == ["xprop", "-id", hex(target)]:
            return (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                f"_NET_WM_PID(CARDINAL) = {pid}\n"
                "_NET_FRAME_EXTENTS(CARDINAL) = 5, 31, 5, 38\n"
            )
        if args == ["xprop", "-id", hex(frame)]:
            return frame_props or (
                f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(frame_for)}\n"
                f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(advertised_helper)}\n"
                f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
                'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
            )
        if args == ["xprop", "-id", hex(helper)]:
            return helper_props or "_NET_WM_USER_TIME(CARDINAL) = 276992109\n"
        if args[0] == "xprop":
            raise AssertionError(args)
        if args == ["xwininfo", "-root", "-tree"]:
            extra = (
                # Deliberate mutation: retain native child syntax while adding
                # one unadvertised direct sibling to the captured tree.
                f"    {hex(extra_frame_child)} (has no name): () 1x1+1+1  +693+363\n"
                if extra_frame_child is not None
                else ""
            )
            return (
                "xwininfo: Window id: 0x374 (the root window)\n"
                f'  {hex(frame)} "CROWLEY INPUT CALIBRATION ONLY": '
                f'("mutter-x11-frames" "mutter-x11-frames") 602x401+692+362  +692+362\n'
                f'    {hex(target)} "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
                "480x240+61+94  +753+456\n"
                f"      {hex(child)} (has no name): () 1x1+-1+-1  +752+455\n"
                f"    {hex(helper)} (has no name): () 1x1+-1+-1  +691+361\n" + extra
            )
        if args == ["xwininfo", "-id", hex(frame), "-stats"]:
            return stats(692, 362, 602, 401) + "Depth: 32\nBorder width: 0\nClass: InputOutput\n"
        if args == ["xwininfo", "-id", hex(target), "-stats"]:
            return (
                stats(753, 456, 480, 240)
                + "Depth: 24\nBorder width: 0\nClass: InputOutput\n"
                + "Map State: IsViewable\n"
            )
        if args == ["xwininfo", "-id", hex(helper), "-stats"]:
            return helper_stats or (
                stats(691, 361, 1, 1) + "Relative upper-left X: -1\nRelative upper-left Y: -1\n"
                "Depth: 0\nBorder width: 0\nClass: InputOnly\n"
            )
        raise AssertionError(args)

    return command


def _root_child_gtk_fixture_command(*, pid: int, target: int, helper: int):
    """Build actual owner-verifier responses for the observed root-child layout."""

    state: dict[str, object] = {
        "user_time_window": f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(helper)}\n",
        "children": (helper,),
        "map_state": "IsViewable",
        "identity_companions": True,
    }
    commands: list[tuple[str, ...]] = []

    def command(args: list[str], **_kwargs: object) -> str:
        commands.append(tuple(args))
        if args == ["wmctrl", "-lp"]:
            return f"{hex(target)} -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        if args == ["xprop", "-id", hex(target)]:
            identity_properties = (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n_NET_WM_PID(CARDINAL) = {pid}\n'
                if state["identity_companions"]
                else ""
            )
            return (
                identity_properties + "_MOTIF_WM_HINTS(_MOTIF_WM_HINTS) = 0x2, 0, 0, 0, 0\n"
                "_NET_FRAME_EXTENTS:  not found.\n"
                + str(state["user_time_window"] or "_NET_WM_USER_TIME_WINDOW:  not found.\n")
            )
        if args == ["xwininfo", "-id", hex(target), "-stats"]:
            map_state = state["map_state"]
            return (
                f"xwininfo: Window id: {hex(target)}\n"
                "Absolute upper-left X: 1440\nAbsolute upper-left Y: 32\n"
                "Width: 480\nHeight: 240\nDepth: 24\nBorder width: 0\n"
                "Class: InputOutput\n" + (f"Map State: {map_state}\n" if map_state is not None else "")
            )
        if args == ["xwininfo", "-id", hex(target), "-tree"]:
            children = tuple(state["children"])
            rows = "".join(
                f"     {hex(child)} (has no name): ()  1x1+-1+-1  +1439+31\n" for child in children
            )
            count = len(children)
            return (
                f'xwininfo: Window id: {hex(target)} "CROWLEY INPUT CALIBRATION ONLY"\n\n'
                "  Root window id: 0x374 (the root window) (has no name)\n"
                "  Parent window id: 0x374 (the root window) (has no name)\n"
                f"     {count} children:\n{rows}"
            )
        if len(args) == 4 and args[:2] == ["xwininfo", "-id"] and args[3] == "-stats":
            child = int(args[2], 16)
            return (
                f"xwininfo: Window id: {hex(child)}\n"
                "Absolute upper-left X: 1439\nAbsolute upper-left Y: 31\n"
                "Relative upper-left X: -1\nRelative upper-left Y: -1\n"
                "Width: 1\nHeight: 1\nDepth: 0\nBorder width: 0\nClass: InputOnly\n"
            )
        raise AssertionError(args)

    return state, commands, command


def _wait_for(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


# Captured-format xinput test-xi2 blocks.  This deliberately preserves raw
# and delivered master/source pairs, releases, transitions, and the forwarded
# source-7 motion to an unrelated window.
FULL_XI2_TRACE = """EVENT type 13 (RawKeyPress)
    device: 3 (5)
    time: 232007447
    detail: 8
    valuators:
EVENT type 2 (KeyPress)
    device: 5 (5)
    time: 232007447
    detail: 8
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 14 (RawKeyRelease)
    device: 3 (5)
    time: 232007462
    detail: 8
    valuators:
EVENT type 3 (KeyRelease)
    device: 5 (5)
    time: 232007462
    detail: 8
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 1 (DeviceChanged)
    device: 2 (4)
    time: 232007470
    reason: slave_switch
EVENT type 7 (Enter)
    device: 4 (4)
    time: 232007471
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 8 (Leave)
    device: 4 (4)
    time: 232007472
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 15 (RawButtonPress)
    device: 2 (4)
    time: 232007476
    detail: 1
    valuators:
EVENT type 4 (ButtonPress)
    device: 4 (4)
    time: 232007476
    detail: 1
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 16 (RawButtonRelease)
    device: 2 (4)
    time: 232007490
    detail: 1
    valuators:
EVENT type 5 (ButtonRelease)
    device: 4 (4)
    time: 232007490
    detail: 1
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x600119
EVENT type 6 (Motion)
    device: 7 (7)
    time: 232007505
    detail: 0
    root: 987.00/595.00
    event: 987.00/595.00
    modifiers: locked 0 latched 0 base 0 effective: 0
    windows: root 0x374 event 0x374 child 0x1a00011
"""


def _evidence(
    tmp_path: Path,
    *,
    hierarchy: tuple[tuple[int, int, str, str], ...] | None = None,
    xauthority: str | None = None,
    driver_version: str = "native-x11-3",
    game_identity_fingerprint: str = "owned-game-identity",
    game_geometry: tuple[int, int, int, int] = (0, 0, 1920, 1080),
):
    records = list(parse_xi2_records(FULL_XI2_TRACE, require_event=True))
    # The forwarded motion is retained as an owned-target observation here;
    # the separate full-trace test keeps the foreign-target negative proof.
    records[-1] = replace(
        records[-1],
        child_xid=TARGET,
        event_xid=0x374,
        root_xy=(988.0, 596.0),
        event_xy=(988.0, 596.0),
    )
    records.insert(
        -1,
        replace(
            _event(EventClass.RAW_MOTION, master=2, source=4, timestamp=232007500, detail=0, child=None),
            valuators=((0, 988.0, 988.0), (1, 596.0, 596.0)),
        ),
    )
    records.insert(
        -1,
        replace(
            _event(EventClass.MOTION, master=4, source=4, timestamp=232007500, detail=0),
            valuators=((0, 988.0, 988.0), (1, 596.0, 596.0)),
        ),
    )
    key_details = {
        "Return": 36,
        "slash": 61,
        "r": 27,
        "e": 25,
        "l": 46,
        "o": 43,
        "a": 38,
        "d": 40,
    }
    profile_operations = (
        KeyboardProbeOperation(
            "open_chat",
            "",
            (KeyboardProbeStep("press", "Return"), KeyboardProbeStep("release", "Return")),
        ),
        KeyboardProbeOperation(
            "type_allowlisted_command",
            "/reload",
            tuple(
                step
                for char in "/reload"
                for step in (
                    KeyboardProbeStep("press", {"/": "slash"}.get(char, char)),
                    KeyboardProbeStep("release", {"/": "slash"}.get(char, char)),
                )
            ),
        ),
    )
    profile_keysyms = tuple(sorted(key_details.items()))
    profile_steps = [(operation, step) for operation in profile_operations for step in operation.steps]
    profile_start = 232007540
    for index, (_operation, step) in enumerate(profile_steps):
        detail = key_details[step.keysym]
        event_class = EventClass.RAW_KEY_PRESS if step.phase == "press" else EventClass.RAW_KEY_RELEASE
        delivered_class = EventClass.KEY_PRESS if step.phase == "press" else EventClass.KEY_RELEASE
        timestamp = profile_start + index * 2
        records.extend(
            (
                _event(
                    event_class,
                    master=3,
                    source=5,
                    timestamp=timestamp,
                    detail=detail,
                    child=None,
                ),
                _event(
                    delivered_class,
                    master=5,
                    source=5,
                    timestamp=timestamp + 1,
                    detail=detail,
                ),
            )
        )
    records.extend(
        [
            _event(EventClass.RAW_KEY_PRESS, master=3, source=9, timestamp=232007518, detail=8, child=None),
            _event(EventClass.KEY_PRESS, master=9, source=9, timestamp=232007520, detail=8),
            _event(EventClass.RAW_KEY_RELEASE, master=3, source=9, timestamp=232007522, detail=8, child=None),
            _event(EventClass.KEY_RELEASE, master=9, source=9, timestamp=232007524, detail=8),
            _event(
                EventClass.RAW_BUTTON_PRESS,
                master=2,
                source=7,
                timestamp=232007526,
                detail=1,
                child=None,
            ),
            _event(EventClass.BUTTON_PRESS, master=7, source=7, timestamp=232007528, detail=1),
            _event(
                EventClass.RAW_BUTTON_RELEASE,
                master=2,
                source=7,
                timestamp=232007530,
                detail=1,
                child=None,
            ),
            _event(EventClass.BUTTON_RELEASE, master=7, source=7, timestamp=232007532, detail=1),
        ]
    )
    measured_hierarchy = hierarchy or (
        (2, 0, "master", "pointer:Virtual core pointer"),
        (3, 0, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer"),
        (9, 3, "slave", "keyboard:xwayland-keyboard"),
    )
    measured_profile = _xtest_profile(measured_hierarchy)
    measured_window = OwnedWindow(
        os.getpid(),
        TARGET,
        "CROWLEY INPUT CALIBRATION ONLY",
        (800, 400, 480, 240),
        object(),  # type: ignore[arg-type]
        frozenset({TARGET}),
        "/usr/bin/python3",
        "owned-target-start",
        TARGET + 1,
        (0, 0, 500, 260),
        frozenset({TARGET, TARGET + 1}),
    )
    measured_transitions = InputCalibrationWorkflow._canonical_motion_transitions(
        records,
        measured_window,
        measured_profile,
    )
    return InputCalibrationEvidence._issue(
        fingerprint="owned-native-test-fingerprint",
        display=":test",
        target_pid=os.getpid(),
        target_xid=TARGET,
        target_child_xid=TARGET,
        synthetic_key_source=5,
        synthetic_pointer_source=4,
        human_key_source=9,
        human_pointer_source=7,
        xinput_hierarchy=measured_hierarchy,
        xtest_profile=measured_profile,
        synthetic_motion_transitions=measured_transitions,
        synthetic_motion_query=(987, 595, TARGET, 988, 596, 988, 596, TARGET),
        synthetic_motion_seed=0,
        synthetic_motion_plan=((0.0, 987, 595), (0.5, 988, 596)),
        synthetic_events=tuple(
            (record.event_class.value, record.source_device, record.detail)
            for record in records
            if record.source_device in {4, 5}
        ),
        human_events=tuple(
            (record.event_class.value, record.source_device, record.detail)
            for record in records
            if record.source_device in {7, 9}
            and record.event_class
            in {
                EventClass.KEY_PRESS,
                EventClass.KEY_RELEASE,
                EventClass.RAW_KEY_PRESS,
                EventClass.RAW_KEY_RELEASE,
                EventClass.BUTTON_PRESS,
                EventClass.BUTTON_RELEASE,
                EventClass.RAW_BUTTON_PRESS,
                EventClass.RAW_BUTTON_RELEASE,
            }
        ),
        forwarded_events=(("Motion", 7, TARGET),),
        driver_version=driver_version,
        xauthority=xauthority or str(tmp_path / "Xauthority"),
        event_proof=tuple(
            (
                record.event_class.value,
                record.master_device,
                record.source_device,
                record.detail,
                record.timestamp_ms,
                record.root_xid,
                record.event_xid,
                record.child_xid,
                record.root_xy,
                record.event_xy,
                record.modifiers,
                record.modifier_effective,
            )
            for record in records
        ),
        game_identity_fingerprint=game_identity_fingerprint,
        game_geometry=game_geometry,
        target_executable="/usr/bin/python3",
        target_process_start="owned-target-start",
        target_geometry=(800, 400, 480, 240),
        managed_frame_xid=TARGET + 1,
        managed_frame_geometry=(790, 390, 500, 260),
        target_event_xids=(TARGET, TARGET + 1),
        synthetic_modifiers=(0, 0, 0),
        synthetic_echo_proof=(
            (
                "MotionEcho",
                232007505,
                7,
                7,
                TARGET,
                (988.0, 596.0),
                (988.0, 596.0),
                232007500,
                4,
                TARGET,
            ),
        ),
        keyboard_keymap=profile_keysyms,
        keyboard_profile=tuple(
            (
                operation.operation,
                operation.payload,
                tuple(
                    (
                        step.phase,
                        step.keysym,
                        key_details[step.keysym],
                        (0, 0, 0),
                        0,
                    )
                    for step in operation.steps
                ),
            )
            for operation in profile_operations
        ),
    )


def _event(
    event_class: EventClass,
    *,
    master: int,
    source: int,
    timestamp: int,
    detail: int | None,
    child: int | None = TARGET,
    root_xy: tuple[float, float] | None = None,
    event_xy: tuple[float, float] | None = None,
    root_xid: int = 0x374,
    event_xid: int = 0x374,
    valuators: tuple[tuple[int, float, float | None], ...] = (),
) -> XI2Event:
    type_numbers = {
        EventClass.RAW_KEY_PRESS: 13,
        EventClass.KEY_PRESS: 2,
        EventClass.RAW_KEY_RELEASE: 14,
        EventClass.KEY_RELEASE: 3,
        EventClass.RAW_BUTTON_PRESS: 15,
        EventClass.BUTTON_PRESS: 4,
        EventClass.RAW_BUTTON_RELEASE: 16,
        EventClass.RAW_MOTION: 17,
        EventClass.BUTTON_RELEASE: 5,
        EventClass.MOTION: 6,
        EventClass.DEVICE_CHANGED: 1,
        EventClass.ENTER: 7,
        EventClass.LEAVE: 8,
    }
    return XI2Event(
        type_numbers[event_class],
        event_class,
        master,
        source,
        timestamp,
        detail,
        root_xid if child is not None else None,
        event_xid if child is not None else None,
        child,
        root_xy=root_xy if root_xy is not None else ((987.0, 595.0) if child is not None else None),
        event_xy=event_xy if event_xy is not None else ((987.0, 595.0) if child is not None else None),
        modifiers=(0, 0, 0) if child is not None else None,
        modifier_effective=0 if child is not None else None,
        valuators=valuators,
    )


def _render_events(events: list[XI2Event], *, motion_source: int | None = None) -> str:
    blocks: list[str] = []
    for event in events:
        lines = [f"EVENT type {event.type_number} ({event.event_class.value})"]
        lines.append(f"    device: {event.master_device} ({event.source_device})")
        lines.append(f"    time: {event.timestamp_ms}")
        if event.detail is not None:
            lines.append(f"    detail: {event.detail}")
        if event.event_class is EventClass.RAW_MOTION or (
            event.event_class is EventClass.MOTION
            and event.source_device == (4 if motion_source is None else motion_source)
        ):
            lines.append("    valuators:")
            for axis, value, raw_value in event.valuators:
                suffix = f" ({raw_value:.2f})" if raw_value is not None else ""
                lines.append(f"         {axis}: {value:.2f}{suffix}")
        if event.child_xid is not None:
            root_xy = event.root_xy or (987.0, 595.0)
            event_xy = event.event_xy or root_xy
            root_xid = event.root_xid if event.root_xid is not None else 0x374
            event_xid = event.event_xid if event.event_xid is not None else root_xid
            lines.extend(
                (
                    f"    root: {root_xy[0]:.2f}/{root_xy[1]:.2f}",
                    f"    event: {event_xy[0]:.2f}/{event_xy[1]:.2f}",
                    f"    windows: root {hex(root_xid)} event {hex(event_xid)} child {hex(event.child_xid)}",
                )
            )
        elif event.event_class is not EventClass.RAW_MOTION:
            lines.append("    valuators:")
            for axis, value, raw_value in event.valuators:
                suffix = f" ({raw_value:.2f})" if raw_value is not None else ""
                lines.append(f"         {axis}: {value:.2f}{suffix}")
        if event.modifiers is not None:
            locked, latched, base = event.modifiers
            effective = event.modifier_effective
            if effective is None:
                raise AssertionError("delivered fixture event lacks effective modifier state")
            lines.append(
                f"    modifiers: locked {locked} latched {latched} base {base} effective: {effective}"
            )
        blocks.append("\n".join(lines))
    return "\n".join(blocks) + "\n"


def test_complete_xi2_fixture_preserves_pairs_releases_transitions_and_foreign_motion() -> None:
    records = parse_xi2_records(FULL_XI2_TRACE, require_event=True)
    assert len(records) == 12
    assert [
        (record.event_class, record.master_device, record.source_device, record.detail, record.target_xid)
        for record in records
    ] == [
        (EventClass.RAW_KEY_PRESS, 3, 5, 8, None),
        (EventClass.KEY_PRESS, 5, 5, 8, TARGET),
        (EventClass.RAW_KEY_RELEASE, 3, 5, 8, None),
        (EventClass.KEY_RELEASE, 5, 5, 8, TARGET),
        (EventClass.DEVICE_CHANGED, 2, 4, None, None),
        (EventClass.ENTER, 4, 4, None, TARGET),
        (EventClass.LEAVE, 4, 4, None, TARGET),
        (EventClass.RAW_BUTTON_PRESS, 2, 4, 1, None),
        (EventClass.BUTTON_PRESS, 4, 4, 1, TARGET),
        (EventClass.RAW_BUTTON_RELEASE, 2, 4, 1, None),
        (EventClass.BUTTON_RELEASE, 4, 4, 1, TARGET),
        (EventClass.MOTION, 7, 7, 0, FOREIGN),
    ]
    assert [(record.master_device, record.source_device) for record in records[:4]] == [
        (3, 5),
        (5, 5),
        (3, 5),
        (5, 5),
    ]
    assert records[1].modifiers == (0, 0, 0)
    assert records[8].modifiers == (0, 0, 0)


def test_guardian_requires_complete_correlations_and_rejects_foreign_motion(tmp_path: Path) -> None:
    records = parse_xi2_records(FULL_XI2_TRACE, require_event=True)
    guardian = InputGuardian(tmp_path / "guardian.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(_evidence(tmp_path))
        guardian.begin_action(
            generation,
            target_xid=TARGET,
            source_device=5,
            master_device=3,
            event_classes={
                EventClass.RAW_KEY_PRESS.value,
                EventClass.KEY_PRESS.value,
                EventClass.RAW_KEY_RELEASE.value,
                EventClass.KEY_RELEASE.value,
            },
            device_by_class={
                EventClass.RAW_KEY_PRESS.value: 3,
                EventClass.KEY_PRESS.value: 5,
                EventClass.RAW_KEY_RELEASE.value: 3,
                EventClass.KEY_RELEASE.value: 5,
            },
            detail=8,
        )
        for record in records[:4]:
            assert guardian.observe(record)
        guardian.finish_action(generation)

        guardian.begin_action(
            generation,
            target_xid=TARGET,
            source_device=4,
            master_device=2,
            event_classes={
                EventClass.DEVICE_CHANGED.value,
                EventClass.ENTER.value,
                EventClass.LEAVE.value,
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
                EventClass.MOTION.value,
            },
            device_by_class={
                EventClass.DEVICE_CHANGED.value: 2,
                EventClass.ENTER.value: 4,
                EventClass.LEAVE.value: 4,
                EventClass.RAW_BUTTON_PRESS.value: 2,
                EventClass.BUTTON_PRESS.value: 4,
                EventClass.RAW_BUTTON_RELEASE.value: 2,
                EventClass.BUTTON_RELEASE.value: 4,
                EventClass.MOTION.value: 4,
            },
            target_ancestor=lambda xid: xid == TARGET,
            detail=1,
        )
        for record in records[4:11]:
            assert guardian.observe(record), record
        assert not guardian.observe(records[11])
        assert not guardian.armed
        assert guardian.action is None
    finally:
        guardian.close()


def test_guardian_rejects_unpaired_forwarded_motion_on_owned_target(tmp_path: Path) -> None:
    """A current source-7 motion needs the current source-4 echo, not old coordinates."""

    records = parse_xi2_records(FULL_XI2_TRACE, require_event=True)
    evidence = _evidence(tmp_path)
    guardian = InputGuardian(tmp_path / "unpaired-motion.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(evidence)
        guardian.begin_action(
            generation,
            target_xid=TARGET,
            source_device=4,
            master_device=4,
            event_classes={EventClass.MOTION.value},
            target_ancestor=lambda xid: xid == TARGET,
            expected_modifiers=(0, 0, 0),
            expected_coordinates=(988.0, 596.0),
            echo_proof=evidence.synthetic_echo_proof,
        )
        stale_owned_motion = replace(
            records[-1],
            master_device=7,
            source_device=7,
            child_xid=TARGET,
            event_xid=TARGET,
            timestamp_ms=5_000,
        )
        assert not guardian.observe(stale_owned_motion)
        assert not guardian.armed
    finally:
        guardian.close()


def test_synthetic_pointer_probe_is_typed_and_rejects_malformed_motion() -> None:
    probe = SyntheticPointerProbe(
        origin=(987, 595, TARGET),
        requested_endpoint=(1002, 610),
        final_observed=(1002, 610, TARGET),
        emitted_transitions=((992, 600), (1002, 610)),
        seed=17,
        plan=((0.0, 987, 595), (0.1, 992, 600), (0.2, 1002, 610)),
    )
    assert probe.origin == (987, 595, TARGET)
    assert probe.requested_endpoint == probe.final_observed[:2]
    with pytest.raises(DriverError, match="motion plan"):
        SyntheticPointerProbe(
            origin=probe.origin,
            requested_endpoint=probe.requested_endpoint,
            final_observed=probe.final_observed,
            emitted_transitions=probe.emitted_transitions,
            seed=probe.seed,
            plan=((0.0, 987, 595), (0.0, 992, 600)),
        )


def test_root_child_ticket_pins_layout_and_rejects_root_helper_and_sibling_targets(tmp_path: Path) -> None:
    from crowley.session.calibration import GTK_LAYOUT_ROOT_CHILD

    target_xid = 0x600119
    helper_xid = 0x60011A
    sibling_xid = 0x60011B
    root_xid = 0x374
    target = OwnedWindow(
        os.getpid(),
        target_xid,
        OwnedGtkCalibrationWindow.TITLE,
        (1440, 32, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({target_xid, helper_xid}),
        "/usr/bin/python3",
        "root-child-target-start",
        None,
        (),
        frozenset({target_xid}),
        GTK_LAYOUT_ROOT_CHILD,
        root_xid,
        root_xid,
        frozenset({helper_xid}),
    )
    driver = DesktopDriver(
        profile=DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority")),
        backend=RecordingBackend(),
        lease_path=tmp_path / "root-child-ticket.lock",
    )
    try:
        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        assert ticket.target_xids == {target_xid}
        assert ticket.target_descendant_xids == {target_xid, helper_xid}
        assert ticket.target_excluded_input_only_xids == {helper_xid}
        assert ticket.target_root_xid == ticket.target_parent_xid == root_xid

        forged_targets = (
            replace(target, certified_event_target_xids=frozenset({target_xid, root_xid})),
            replace(target, certified_event_target_xids=frozenset({target_xid, helper_xid})),
            replace(
                target,
                descendant_xids=frozenset({target_xid, helper_xid, sibling_xid}),
                certified_event_target_xids=frozenset({target_xid, sibling_xid}),
            ),
            replace(
                target,
                xid=helper_xid,
                descendant_xids=frozenset({target_xid, helper_xid}),
                excluded_input_only_xids=frozenset({helper_xid}),
                certified_event_target_xids=frozenset({helper_xid}),
            ),
            replace(target, layout_kind="mutter-managed-frame-v1", managed_frame_xid=target_xid),
        )
        for forged in forged_targets:
            with pytest.raises(DriverError, match="certificate|changed after ticket"):
                driver._verify_calibration_target(ticket, forged)

        with pytest.raises(DriverError, match="changed after ticket"):
            driver._verify_calibration_target(replace(ticket, target_root_xid=root_xid + 1), target)
        with pytest.raises(DriverError, match="changed after ticket"):
            driver._verify_calibration_target(
                replace(ticket, target_layout_kind="mutter-managed-frame-v1"), target
            )
    finally:
        driver.close()


def test_calibration_gate_health_is_rechecked_after_registration_ack(tmp_path: Path) -> None:
    """A watchdog dying after group ACK cannot release the GTK helper gate."""

    profile = DriverProfile(display=":gtk-watchdog-race", xauthority=str(tmp_path / "Xauthority"))
    driver = DesktopDriver(
        profile=profile,
        backend=RecordingBackend(),
        lease_path=tmp_path / "gtk-watchdog-race.lock",
    )
    release_calls: list[bool] = []
    original_register = driver.guardian.register_calibration_bootstrap_process

    def register_then_reap_watchdog(pgid: int, epoch: int) -> None:
        original_register(pgid, epoch)
        watchdog = driver.guardian._watchdog_process
        assert watchdog is not None
        watchdog.terminate()
        watchdog.join(timeout=2)
        assert not watchdog.is_alive()

    driver.guardian.register_calibration_bootstrap_process = register_then_reap_watchdog
    try:
        driver.begin_calibration_operation()
        with pytest.raises(DriverError, match="startup admission failed"):
            driver.admit_calibration_target_process(500_100, lambda: release_calls.append(True))
        assert not release_calls
        assert not driver.guardian.active_process_groups
    finally:
        driver.close()


def test_calibration_admission_records_unregister_failure(tmp_path: Path) -> None:
    driver = DesktopDriver(
        profile=DriverProfile(display=":gtk-unregister-note", xauthority=str(tmp_path / "Xauthority")),
        backend=RecordingBackend(),
        lease_path=tmp_path / "gtk-unregister-note.lock",
    )
    original_unregister = driver.guardian.unregister_process_group

    def failing_unregister(_pgid: int) -> None:
        raise RuntimeError("synthetic unregister failure")

    driver.guardian.unregister_process_group = failing_unregister
    try:
        driver.begin_calibration_operation()
        with pytest.raises(DriverError, match="startup admission failed") as failure:
            driver.admit_calibration_target_process(
                500_200, lambda: (_ for _ in ()).throw(RuntimeError("gate"))
            )
        assert any("unregister failed" in note for note in failure.value.__notes__)
    finally:
        driver.guardian.unregister_process_group = original_unregister
        original_unregister(500_200)
        driver.close()


def test_failed_gtk_create_unregisters_bootstrap_for_same_driver_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A post-admission verification failure must leave calibration retryable."""

    import crowley.session.calibration as calibration_module

    profile = DriverProfile(display=":gtk-retry", xauthority=str(tmp_path / "Xauthority"))
    driver = DesktopDriver(
        profile=profile,
        backend=RecordingBackend(),
        lease_path=tmp_path / "gtk-retry.lock",
    )
    processes: list[object] = []
    lifecycle: list[str] = []
    verify_calls = 0

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self._gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append("terminate")
            if self._gate_reader >= 0:
                os.close(self._gate_reader)
                self._gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            lifecycle.append("kill")
            if self._gate_reader >= 0:
                os.close(self._gate_reader)
                self._gate_reader = -1
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            lifecycle.append("wait")
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(_args: list[str], **_kwargs: object) -> FakeProcess:
        pid = 500_000 + len(processes)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = _args.index(_EXEC_ADMISSION_WRAPPER)
        gate_reader = os.dup(int(_args[wrapper_index + 1]))
        process = FakeProcess(pid, os.fdopen(read_fd, "r", encoding="utf-8"), gate_reader)
        processes.append(process)
        return process

    def verify(
        _target: OwnedGtkCalibrationWindow,
        pid: int,
        xid: int,
        process: object,
        **_kwargs: object,
    ) -> object:
        del pid, xid
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            raise CalibrationError("synthetic GTK verification failure")
        return SimpleNamespace(pid=process.pid, xid=TARGET, process=process)

    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    original_unregister = driver.guardian.unregister_process_group

    def record_unregister(pgid: int) -> None:
        lifecycle.append("unregister")
        original_unregister(pgid)

    driver.guardian.unregister_process_group = record_unregister
    try:
        driver.begin_calibration_operation()
        failed = OwnedGtkCalibrationWindow(
            display=profile.display,
            xauthority=profile.xauthority,
            python_executable=sys.executable,
        )
        with pytest.raises(CalibrationError, match="synthetic GTK verification failure"):
            failed.create(startup_admission=driver.admit_calibration_target_process)
        assert lifecycle[:3] == ["terminate", "wait", "unregister"]
        assert not driver.guardian.active_process_groups
        assert not driver._calibration_bootstrap_process_groups

        # This is the liveness contract: a transient target verification
        # failure must not require closing the driver before retrying.
        driver.begin_calibration_operation()
        retry = OwnedGtkCalibrationWindow(
            display=profile.display,
            xauthority=profile.xauthority,
            python_executable=sys.executable,
        )
        window = retry.create(startup_admission=driver.admit_calibration_target_process)
        assert window.pid == processes[-1].pid
        assert driver.guardian.active_process_groups == frozenset({processes[-1].pid})
    finally:
        for process in processes:
            process.terminate()  # type: ignore[attr-defined]
            process.stdout.close()  # type: ignore[attr-defined]
        driver.close()


def test_gtk_create_waits_for_reparented_readiness_without_respawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identity publication is not readiness, and one child owns the retry loop."""

    import crowley.session.calibration as calibration_module

    processes: list[object] = []
    verify_deadlines: list[float] = []
    releases: list[bool] = []

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pid = 601_000 + len(processes)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        gate_reader = os.dup(int(argv[wrapper_index + 1]))
        process = FakeProcess(pid, os.fdopen(read_fd, "r", encoding="utf-8"), gate_reader)
        processes.append(process)
        return process

    def verify(
        _target: OwnedGtkCalibrationWindow,
        pid: int,
        xid: int,
        process: object,
        **kwargs: object,
    ) -> object:
        assert kwargs["startup_readiness"] is True
        deadline = kwargs["deadline"]
        assert isinstance(deadline, float)
        verify_deadlines.append(deadline)
        if len(verify_deadlines) == 1:
            raise calibration_module._GtkStartupPending("Mutter has not reparented the client")
        return SimpleNamespace(pid=pid, xid=xid, process=process)

    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-readiness",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )

    def admit(_pid: int, release: Callable[[], None]) -> None:
        releases.append(True)
        release()

    try:
        window = target.create(timeout=0.2, startup_admission=admit)
        assert window.pid == 601_000
        assert len(processes) == 1
        assert len(verify_deadlines) >= 2
        assert len(set(verify_deadlines)) == 1
        assert releases == [True]
    finally:
        if target.window is not None:
            target.close()
        for process in processes:
            process.stdout.close()  # type: ignore[attr-defined]


def test_gtk_create_real_verifier_replays_startup_states_and_command_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One admitted child crosses real pending states before one ready certificate."""

    import crowley.session.calibration as calibration_module

    pid = 604_000
    frame = 0x600192
    helper = 0x600193
    child = 0x600121
    frame_pid = 5717
    clock = [100.0]
    attempts = 0
    command_observations: list[tuple[float, float]] = []
    processes: list[object] = []
    admissions: list[int] = []
    registration_cleanups: list[int] = []

    class FakeProcess:
        def __init__(self, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        process = FakeProcess(os.fdopen(read_fd, "r", encoding="utf-8"), os.dup(int(argv[wrapper_index + 1])))
        processes.append(process)
        return process

    def stats(x: int, y: int, width: int, height: int, *, map_state: str | None = None) -> str:
        result = (
            f"Absolute upper-left X: {x}\nAbsolute upper-left Y: {y}\n"
            f"Width: {width}\nHeight: {height}\nDepth: 24\nBorder width: 0\nClass: InputOutput\n"
        )
        if map_state is not None:
            result += f"Map State: {map_state}\n"
        return result

    ready_command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=frame,
        helper=helper,
        child=child,
        frame_pid=frame_pid,
    )

    def command(args: list[str], **kwargs: object) -> str:
        nonlocal attempts
        command_observations.append((clock[0], float(kwargs["timeout"])))
        clock[0] += 0.005
        if args[:2] == ["wmctrl", "-lp"]:
            attempts += 1
            return f"{hex(TARGET)} -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        phase = attempts - 1
        if phase == 0 and args == ["xprop", "-id", hex(TARGET)]:
            return f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n_NET_WM_PID(CARDINAL) = {pid}\n'
        if phase < 4 and args == ["xprop", "-id", hex(TARGET)]:
            return (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                f"_NET_WM_PID(CARDINAL) = {pid}\n"
                "_NET_FRAME_EXTENTS(CARDINAL) = 5, 31, 5, 38\n"
            )
        if phase < 4 and args == ["xwininfo", "-id", hex(TARGET), "-stats"]:
            return stats(
                753,
                456,
                480,
                240,
                map_state="IsUnMapped" if phase < 2 else "IsViewable",
            )
        if phase == 2 and args == ["xwininfo", "-root", "-tree"]:
            return (
                "xwininfo: Window id: 0x374 (the root window)\n"
                f'  {hex(TARGET)} "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
                "480x240+753+456  +753+456\n"
                '  0x700001 "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
                "1x1+1+1  +1+1\n"
            )
        if phase == 3 and args == ["xwininfo", "-root", "-tree"]:
            return (
                "xwininfo: Window id: 0x374 (the root window)\n"
                f'  {hex(frame)} "CROWLEY INPUT CALIBRATION ONLY": '
                f'("mutter-x11-frames" "mutter-x11-frames") 602x401+692+362  +692+362\n'
                f'    {hex(TARGET)} "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
                "480x240+61+94  +753+456\n"
                "    0x700001 (has no name): () 1x1+-1+-1  +752+455\n"
            )
        if phase == 3 and args == ["xprop", "-id", hex(frame)]:
            return (
                f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
                f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
                'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
            )
        return ready_command(args, **kwargs)

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        calibration_module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration)
    )
    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-real-verifier",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
        command=command,
    )

    def admit(_pid: int, release: Callable[[], None]) -> Callable[[], None]:
        admissions.append(1)
        release()

        def cleanup() -> None:
            registration_cleanups.append(1)

        return cleanup

    initial_clock = clock[0]
    window = target.create(timeout=1.0, startup_admission=admit)
    try:
        assert attempts == 5
        assert len(processes) == 1
        assert len(command_observations) > 8
        assert admissions == [1]
        assert registration_cleanups == []
        assert all(
            timeout == pytest.approx(initial_clock + 1.0 - observed_clock)
            for observed_clock, timeout in command_observations
        )
        assert all(
            left[1] > right[1] > 0
            for left, right in zip(command_observations, command_observations[1:], strict=False)
        )
        assert window.allowed_event_target_xids == frozenset({frame, TARGET, child})
    finally:
        target.close()
        processes[0].stdout.close()  # type: ignore[attr-defined]


@pytest.mark.skipif(
    shutil.which("Xvfb") is None
    or shutil.which("xdotool") is None
    or shutil.which("xprop") is None
    or shutil.which("xwininfo") is None
    or shutil.which("python3.13") is None,
    reason="private Xvfb/GTK fixture is unavailable",
)
@pytest.mark.parametrize(
    ("mouse_position_enabled", "expected_inside"),
    [(True, True), (False, False)],
    ids=("settled-mouse-position-contains-origin", "ignored-mouse-position-is-refused"),
)
def test_gtk_create_preserves_top_edge_pointer_through_guarded_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mouse_position_enabled: bool,
    expected_inside: bool,
) -> None:
    """Measure real GTK placement around an unchanged private-Xvfb pointer."""

    import crowley.session.calibration as calibration_module

    xvfb, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1920x1080x24",
            "-ac",
        ),
        popen_kwargs={
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
        },
    )
    target: OwnedGtkCalibrationWindow | None = None
    driver: DesktopDriver | None = None
    try:
        env = os.environ.copy()
        env["DISPLAY"] = display
        env["GDK_BACKEND"] = "x11"
        xauthority = str(tmp_path / "private-xauthority")
        Path(xauthority).touch(mode=0o600)
        if not mouse_position_enabled:
            script = calibration_module._GTK_SCRIPT
            placement = "window.set_position(Gtk.WindowPosition.MOUSE)\n"
            assert placement in script
            monkeypatch.setattr(calibration_module, "_GTK_SCRIPT", script.replace(placement, ""))

        # Xvfb's xdotool mousemove path is inert without an XTEST pointer
        # device. Set only the initial private test coordinate with XWarpPointer,
        # then observe it unchanged across real GTK mapping and guarded focus.
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        xroot = x11.XDefaultRootWindow(xdisplay)
        x11.XWarpPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
        ]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        assert x11.XWarpPointer(xdisplay, 0, xroot, 0, 0, 0, 0, 1809, 43) == 1
        x11.XSync(xdisplay, 0)

        def pointer_position() -> tuple[int, int, int]:
            output = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
            values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            return int(values["X"]), int(values["Y"]), int(values["WINDOW"], 0)

        before_create = pointer_position()
        assert before_create[:2] == (1809, 43)

        facts: dict[str, int] = {}
        commands: list[tuple[str, ...]] = []
        from crowley.session.driver import _parse_xwininfo_tree

        def command(argv: list[str], *, display: str, xauthority: str, timeout: float = 5.0) -> str:
            commands.append(tuple(argv))
            if argv == ["wmctrl", "-lp"]:
                if not facts:
                    return ""
                return f"0x{facts['xid']:x} -1 {facts['pid']} {socket.gethostname()} {OwnedGtkCalibrationWindow.TITLE}\n"
            if argv[:2] == ["xprop", "-id"]:
                if int(argv[2], 16) != facts["xid"]:
                    raise AssertionError(f"unowned xprop target: {argv!r}")
            elif argv[:2] == ["xwininfo", "-id"]:
                query_xid = int(argv[2], 16)
                if query_xid not in {facts["xid"], facts.get("helper", 0)}:
                    raise AssertionError(f"unowned xwininfo target: {argv!r}")
            else:
                raise AssertionError(f"unexpected GTK verifier command: {argv!r}")
            if argv[0] == "xwininfo" and len(argv) == 4 and argv[3] == "-tree":
                if argv[2] != hex(facts["xid"]):
                    raise AssertionError("root enumeration is forbidden in the root-child contract test")
            result = subprocess.run(
                argv,
                env=env | {"DISPLAY": display, "XAUTHORITY": xauthority},
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.001, timeout),
            )
            if result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode, argv, output=result.stdout, stderr=result.stderr
                )
            if argv == ["xwininfo", "-id", hex(facts["xid"]), "-tree"]:
                _tree_root, parents, _geometry = _parse_xwininfo_tree(result.stdout)
                if len(parents) != 1:
                    raise AssertionError(f"unexpected GTK child tree: {parents!r}")
                helper = next(iter(parents))
                if "helper" not in facts:
                    facts["helper"] = helper
                elif helper != facts["helper"]:
                    raise AssertionError(f"GTK child tree changed: {parents!r}")
            return result.stdout

        target = OwnedGtkCalibrationWindow(
            display=display,
            xauthority=xauthority,
            python_executable="/usr/bin/python3.13",
            command=command,
            layout_kind=OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD,
        )
        original_verify = target._verify

        def capture_identity(pid: int, xid: int, process, **kwargs):
            facts.update(pid=pid, xid=xid)
            return original_verify(pid, xid, process, **kwargs)

        target._verify = capture_identity  # type: ignore[method-assign]
        backend = RecordingBackend()

        def guarded_private_focus(xid: int) -> None:
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", hex(xid)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            focused = subprocess.run(
                ["xdotool", "getwindowfocus"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            backend.events.append({"name": "private_guarded_focus", "xid": xid, "focused": focused})

        backend.calibration_focus = guarded_private_focus
        driver = DesktopDriver(
            profile=DriverProfile(display=display, xauthority=xauthority),
            backend=backend,
            lease_path=tmp_path / "private-desktop.lock",
        )
        driver.begin_calibration_operation()
        window = target.create(
            timeout=3.0,
            startup_admission=driver.admit_calibration_target_process,
        )
        process = window.process
        after_map = pointer_position()
        ticket = driver.issue_calibration_ticket(window)
        driver.focus_calibration_target(window, calibration_ticket=ticket)
        after_focus = pointer_position()
        left, top, width, height = window.geometry
        inside = left <= after_focus[0] < left + width and top <= after_focus[1] < top + height
        assert window.title == OwnedGtkCalibrationWindow.TITLE
        assert window.geometry[2:] == (480, 240)
        assert window.layout_kind == OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD
        assert window.managed_frame_xid is None
        assert window.root_xid > 0 and window.parent_xid == window.root_xid
        assert window.descendant_xids == {window.xid, facts["helper"]}
        assert window.allowed_event_target_xids == {window.xid}
        assert window.excluded_input_only_xids == {facts["helper"]}
        assert window.contains_xid(window.xid)
        assert not window.contains_xid(window.root_xid)
        assert ("xwininfo", "-root", "-tree") not in commands
        assert before_create[:2] == after_map[:2] == after_focus[:2] == (1809, 43)
        assert inside is expected_inside, (after_focus, window.geometry)
        assert backend.events == [
            {"name": "private_guarded_focus", "xid": window.xid, "focused": str(window.xid)}
        ]
        target.close()
        assert process.poll() is not None
        assert target.window is None
    finally:
        if target is not None and target.window is not None:
            target.close()
        if driver is not None:
            driver.cancel_calibration_actions()
            driver.close()
        if xdisplay:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(xdisplay)
        xvfb.terminate()
        xvfb.wait(timeout=3)


@pytest.mark.skipif(
    shutil.which("Xvfb") is None
    or shutil.which("xdotool") is None
    or shutil.which("wmctrl") is None
    or shutil.which("xprop") is None
    or shutil.which("xwininfo") is None
    or shutil.which("xinput") is None
    or shutil.which("python3.13") is None
    or shutil.which("cc") is None,
    reason="private Xvfb/GTK calibration fixture is unavailable",
)
def test_private_xvfb_calibration_workflow_captures_every_typed_motion_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the owned workflow around a private GTK target and retain its full probe trace."""

    from crowley.session import pointer_execution as pointer_execution_module

    xvfb, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1920x1080x24",
            "-ac",
        ),
        popen_kwargs={
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
        },
    )
    target: OwnedGtkCalibrationWindow | None = None
    driver: DesktopDriver | None = None
    wm_process: subprocess.Popen[bytes] | None = None
    try:
        python_executable = shutil.which("python3.13")
        xdotool_executable = shutil.which("xdotool")
        if python_executable is None or xdotool_executable is None:
            pytest.skip("private calibration executables are unavailable")
        xauthority = tmp_path / "private-calibration-Xauthority"
        xauthority.touch(mode=0o600)
        env = os.environ.copy()
        env.update({"DISPLAY": display, "GDK_BACKEND": "x11", "XAUTHORITY": str(xauthority)})
        wm_process = start_ewmh_window_manager(
            display=display,
            xauthority=str(xauthority),
            workdir=tmp_path / "ewmh-window-manager",
        )
        subprocess.run(
            ["xdotool", "mousemove", "100", "100"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        rate_directory = tmp_path / "pointer-click-rate"
        rate_directory.mkdir(mode=0o700)
        monkeypatch.setattr(pointer_execution_module, "_session_state_directory", lambda: rate_directory)
        profile = DriverProfile(display=display, xauthority=str(xauthority))
        backend = X11Backend(
            profile, input_tool=fixture_input_tool(tmp_path / "input-tool", xdotool_executable)
        )
        driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "desktop.lock")
        backend.input_broker = driver.input_broker

        actual_hierarchy = driver.input_hierarchy()
        used_ids = {row[0] for row in actual_hierarchy}
        human_pointer = max(used_ids) + 1
        human_key = human_pointer + 1
        pointer_master = next(
            row[1] for row in actual_hierarchy if "pointer" in row[3].casefold() and row[2] == "master"
        )
        keyboard_master = next(
            row[1] for row in actual_hierarchy if "keyboard" in row[3].casefold() and row[2] == "master"
        )
        hierarchy = (
            *actual_hierarchy,
            (human_pointer, pointer_master, "slave", "pointer:private human pointer"),
            (human_key, keyboard_master, "slave", "keyboard:private human keyboard"),
        )
        synthetic_key_source, synthetic_pointer_source = xtest_sources(hierarchy)
        monkeypatch.setattr(driver, "input_hierarchy", lambda **_kwargs: hierarchy)
        game_identity = WindowIdentity(
            0x700001,
            os.getpid(),
            python_executable,
            "World of Warcraft",
            "python3.13",
            0x700002,
            (0, 0, 1920, 1080),
            (0, 0, 1920, 1080),
            0x700002,
            "private-game-start",
            display,
            "private-game-identity",
            root_xid=0x1,
            target_event_xids=frozenset({0x700001, 0x700002}),
        )
        monkeypatch.setattr(driver, "resolve_identity", lambda **_kwargs: game_identity)
        operations = (
            KeyboardProbeOperation(
                "open_chat",
                "",
                (KeyboardProbeStep("press", "Return"), KeyboardProbeStep("release", "Return")),
            ),
        )
        live_keymap = backend.observe_keymap()
        return_detail = live_keymap.get("Return")
        if not isinstance(return_detail, int) or return_detail <= 0:
            pytest.skip("private Xvfb has no measurable Return key")
        monkeypatch.setattr(driver, "calibration_keyboard_operations", lambda: operations)
        monkeypatch.setattr(driver, "calibration_keyboard_keymap", lambda: {"Return": return_detail})

        class Observer:
            def __init__(self, callback: Callable[[XI2Event | None], None]) -> None:
                self.callback = callback
                self.events_seen: list[XI2Event] = []
                self.synthetic_trace = ""
                self.human_trace = ""

            def _publish(self, event: XI2Event) -> None:
                self.events_seen.append(event)
                self.callback(event)

            def collect_until(self, predicate, *, timeout: float) -> str:
                del timeout
                synthetic = parse_xi2_records(self.synthetic_trace, require_event=True)
                for event in synthetic:
                    self._publish(event)
                if not predicate(self.events_seen):
                    raise AssertionError("private calibration observer did not accept the typed probe trace")
                return self.synthetic_trace

            def wait_for_human(self, predicate, *, timeout: float) -> None:
                del timeout
                human = parse_xi2_records(self.human_trace, require_event=True)
                for event in human:
                    self._publish(event)
                if not predicate(self.events_seen):
                    raise AssertionError("private calibration observer did not accept the deliberate trace")

            def events(self) -> list[XI2Event]:
                return list(self.events_seen)

            def stop(self) -> None:
                return None

        observer_box: list[Observer] = []
        probe_box: list[SyntheticPointerProbe] = []

        def start_observer(*, calibration: bool, callback: Callable[[XI2Event | None], None]):
            if not calibration:
                raise AssertionError("private calibration fixture requested a runtime observer")
            observer = Observer(callback)
            observer_box.append(observer)
            return observer

        monkeypatch.setattr(driver, "start_input_observer", start_observer)
        monkeypatch.setattr(
            driver,
            "stop_input_observer",
            lambda _observer, *, already_stopped=False: None,
        )

        facts: dict[str, int] = {}

        def command(argv: list[str], *, display: str, xauthority: str, timeout: float = 5.0) -> str:
            if argv == ["wmctrl", "-lp"]:
                if not facts:
                    return ""
                return f"{hex(facts['xid'])} -1 {facts['pid']} {socket.gethostname()} {OwnedGtkCalibrationWindow.TITLE}\n"
            result = subprocess.run(
                argv,
                env=env | {"DISPLAY": display, "XAUTHORITY": xauthority},
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.001, timeout),
            )
            if result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode, argv, output=result.stdout, stderr=result.stderr
                )
            return result.stdout

        def event(
            event_class: EventClass,
            *,
            master: int,
            source: int,
            timestamp: int,
            detail: int | None,
            child: int | None,
            root_xy: tuple[float, float] | None = None,
            event_xy: tuple[float, float] | None = None,
            valuators: tuple[tuple[int, float, float | None], ...] = (),
            root_xid: int | None = 0x1,
            event_xid: int | None = 0x1,
        ) -> XI2Event:
            raw = event_class in {
                EventClass.RAW_KEY_PRESS,
                EventClass.RAW_KEY_RELEASE,
                EventClass.RAW_BUTTON_PRESS,
                EventClass.RAW_BUTTON_RELEASE,
                EventClass.RAW_MOTION,
            }
            return XI2Event(
                {
                    EventClass.RAW_KEY_PRESS: 13,
                    EventClass.KEY_PRESS: 2,
                    EventClass.RAW_KEY_RELEASE: 14,
                    EventClass.KEY_RELEASE: 3,
                    EventClass.RAW_BUTTON_PRESS: 15,
                    EventClass.BUTTON_PRESS: 4,
                    EventClass.RAW_BUTTON_RELEASE: 16,
                    EventClass.BUTTON_RELEASE: 5,
                    EventClass.RAW_MOTION: 17,
                    EventClass.MOTION: 6,
                }[event_class],
                event_class,
                master,
                source,
                timestamp,
                detail,
                None if raw else root_xid,
                None if raw else event_xid,
                None if raw else child,
                None if raw else root_xy,
                None if raw else event_xy,
                None if raw else (0, 0, 0),
                valuators=valuators,
                modifier_effective=None if raw else 0,
            )

        def install_probe_trace(probe: SyntheticPointerProbe, window: OwnedWindow) -> None:
            observer = observer_box[0]
            profile_capture = driver.xtest_input_profile(hierarchy)
            if profile_capture.source_device != synthetic_pointer_source:
                raise AssertionError("XTEST profile pointer source differs from the measured hierarchy")
            keyboard_source = synthetic_key_source
            keyboard_parent = next(row[1] for row in hierarchy if row[0] == keyboard_source)
            records: list[XI2Event] = []
            timestamp = 100
            for key_event, _phase in (
                (EventClass.RAW_KEY_PRESS, "press"),
                (EventClass.KEY_PRESS, "press"),
                (EventClass.RAW_KEY_RELEASE, "release"),
                (EventClass.KEY_RELEASE, "release"),
            ):
                records.append(
                    event(
                        key_event,
                        master=keyboard_parent if key_event.name.startswith("RAW") else keyboard_source,
                        source=keyboard_source,
                        timestamp=timestamp,
                        detail=8 if key_event.name.startswith("RAW") else 8,
                        child=window.xid,
                    )
                )
                timestamp += 1
            for phase in ("press", "release"):
                raw_class = EventClass.RAW_KEY_PRESS if phase == "press" else EventClass.RAW_KEY_RELEASE
                delivered_class = EventClass.KEY_PRESS if phase == "press" else EventClass.KEY_RELEASE
                records.extend(
                    (
                        event(
                            raw_class,
                            master=keyboard_parent,
                            source=keyboard_source,
                            timestamp=timestamp,
                            detail=return_detail,
                            child=window.xid,
                        ),
                        event(
                            delivered_class,
                            master=keyboard_source,
                            source=keyboard_source,
                            timestamp=timestamp + 1,
                            detail=return_detail,
                            child=window.xid,
                            root_xy=probe.origin[:2],
                            event_xy=probe.origin[:2],
                        ),
                    )
                )
                timestamp += 2
            previous = probe.origin[:2]
            for endpoint in probe.emitted_transitions:
                endpoint_float = (float(endpoint[0]), float(endpoint[1]))
                records.extend(
                    (
                        event(
                            EventClass.RAW_MOTION,
                            master=profile_capture.parent_device,
                            source=profile_capture.source_device,
                            timestamp=timestamp,
                            detail=0,
                            child=window.xid,
                            valuators=(
                                (0, endpoint_float[0], endpoint_float[0]),
                                (1, endpoint_float[1], endpoint_float[1]),
                            ),
                        ),
                        event(
                            EventClass.MOTION,
                            master=profile_capture.source_device,
                            source=profile_capture.source_device,
                            timestamp=timestamp,
                            detail=0,
                            child=window.xid,
                            root_xy=previous,
                            event_xy=previous,
                            valuators=(
                                (0, endpoint_float[0], endpoint_float[0]),
                                (1, endpoint_float[1], endpoint_float[1]),
                            ),
                            root_xid=profile_capture.root_xid,
                            event_xid=profile_capture.root_xid,
                        ),
                    )
                )
                previous = endpoint_float
                timestamp += 1
            records.extend(
                (
                    event(
                        EventClass.RAW_BUTTON_PRESS,
                        master=profile_capture.parent_device,
                        source=profile_capture.source_device,
                        timestamp=timestamp,
                        detail=1,
                        child=window.xid,
                    ),
                    event(
                        EventClass.BUTTON_PRESS,
                        master=profile_capture.source_device,
                        source=profile_capture.source_device,
                        timestamp=timestamp + 1,
                        detail=1,
                        child=window.xid,
                        root_xy=previous,
                        event_xy=previous,
                        root_xid=profile_capture.root_xid,
                        event_xid=profile_capture.root_xid,
                    ),
                    event(
                        EventClass.RAW_BUTTON_RELEASE,
                        master=profile_capture.parent_device,
                        source=profile_capture.source_device,
                        timestamp=timestamp + 2,
                        detail=1,
                        child=window.xid,
                    ),
                    event(
                        EventClass.BUTTON_RELEASE,
                        master=profile_capture.source_device,
                        source=profile_capture.source_device,
                        timestamp=timestamp + 3,
                        detail=1,
                        child=window.xid,
                        root_xy=previous,
                        event_xy=previous,
                        root_xid=profile_capture.root_xid,
                        event_xid=profile_capture.root_xid,
                    ),
                )
            )
            human_timestamp = timestamp + 10
            observer.synthetic_trace = _render_events(records, motion_source=synthetic_pointer_source)
            human_records = [
                event(
                    EventClass.RAW_KEY_PRESS,
                    master=keyboard_master,
                    source=human_key,
                    timestamp=human_timestamp,
                    detail=38,
                    child=window.xid,
                ),
                event(
                    EventClass.KEY_PRESS,
                    master=human_key,
                    source=human_key,
                    timestamp=human_timestamp + 1,
                    detail=38,
                    child=window.xid,
                    root_xy=previous,
                    event_xy=previous,
                ),
                event(
                    EventClass.RAW_KEY_RELEASE,
                    master=keyboard_master,
                    source=human_key,
                    timestamp=human_timestamp + 2,
                    detail=38,
                    child=window.xid,
                ),
                event(
                    EventClass.KEY_RELEASE,
                    master=human_key,
                    source=human_key,
                    timestamp=human_timestamp + 3,
                    detail=38,
                    child=window.xid,
                    root_xy=previous,
                    event_xy=previous,
                ),
                event(
                    EventClass.MOTION,
                    master=human_pointer,
                    source=human_pointer,
                    timestamp=human_timestamp + 4,
                    detail=0,
                    child=window.xid,
                    root_xy=previous,
                    event_xy=previous,
                ),
                event(
                    EventClass.RAW_BUTTON_PRESS,
                    master=pointer_master,
                    source=human_pointer,
                    timestamp=human_timestamp + 5,
                    detail=1,
                    child=window.xid,
                ),
                event(
                    EventClass.BUTTON_PRESS,
                    master=human_pointer,
                    source=human_pointer,
                    timestamp=human_timestamp + 6,
                    detail=1,
                    child=window.xid,
                    root_xy=previous,
                    event_xy=previous,
                ),
                event(
                    EventClass.RAW_BUTTON_RELEASE,
                    master=pointer_master,
                    source=human_pointer,
                    timestamp=human_timestamp + 7,
                    detail=1,
                    child=window.xid,
                ),
                event(
                    EventClass.BUTTON_RELEASE,
                    master=human_pointer,
                    source=human_pointer,
                    timestamp=human_timestamp + 8,
                    detail=1,
                    child=window.xid,
                    root_xy=previous,
                    event_xy=previous,
                ),
            ]
            observer.human_trace = _render_events(human_records, motion_source=human_pointer)

        original_synthetic = driver.synthetic_target_keyboard_profile

        def synthetic_target_keyboard_profile(*args: object, **kwargs: object) -> SyntheticPointerProbe:
            probe = original_synthetic(*args, **kwargs)
            probe_box.append(probe)
            install_probe_trace(probe, args[0])  # type: ignore[arg-type]
            return probe

        monkeypatch.setattr(driver, "synthetic_target_keyboard_profile", synthetic_target_keyboard_profile)
        target = OwnedGtkCalibrationWindow(
            display=display,
            xauthority=str(xauthority),
            python_executable=python_executable,
            command=command,
            layout_kind=OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD,
        )
        original_verify = target._verify

        x11.XWarpPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
        ]
        x11.XWarpPointer.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XFlush.restype = ctypes.c_int

        def place_pointer_in_target(window: OwnedWindow) -> None:
            _left, _top, width, height = window.geometry
            x11.XWarpPointer(
                xdisplay,
                0,
                ctypes.c_ulong(window.xid),
                0,
                0,
                0,
                0,
                width // 2,
                height // 2,
            )
            x11.XFlush(xdisplay)
            deadline = time.monotonic() + 2.0
            last_observation: tuple[int, int, int] | None = None
            last_error: DriverError | None = None
            while time.monotonic() < deadline:
                try:
                    observation = backend.pointer_position()
                except DriverError as exc:
                    last_error = exc
                else:
                    last_observation = observation
                    if window.contains_xid(observation[2]):
                        return
                time.sleep(0.01)
            if last_error is not None:
                raise AssertionError(
                    f"private Xvfb pointer did not enter target; last query failed: {last_error}"
                ) from last_error
            raise AssertionError(
                f"private Xvfb pointer did not enter target; last observation: {last_observation!r}"
            )

        def capture_identity(pid: int, xid: int, process, **kwargs):
            facts.update(pid=pid, xid=xid)
            window = original_verify(pid, xid, process, **kwargs)
            place_pointer_in_target(window)
            return window

        target._verify = capture_identity  # type: ignore[method-assign]
        workflow = InputCalibrationWorkflow(
            driver,
            display=display,
            window_factory=lambda: target,
        )
        evidence = workflow.run(human_timeout=1)
        assert len(probe_box) == 1
        probe = probe_box[0]
        assert len(probe.emitted_transitions) >= 2
        assert evidence.synthetic_motion_seed == probe.seed
        assert evidence.synthetic_motion_plan == probe.plan
        assert tuple(transition[10] for transition in evidence.synthetic_motion_transitions) == (
            probe.emitted_transitions
        )
        assert len(
            [
                row
                for row in evidence.event_proof
                if row[0] == "RawMotion" and row[2] == evidence.synthetic_pointer_source
            ]
        ) == len(probe.emitted_transitions)
    finally:
        if target is not None and target.window is not None:
            target.close()
        if driver is not None:
            driver.close()
        if xdisplay:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(xdisplay)
        if wm_process is not None:
            stop_ewmh_window_manager(wm_process)
        xvfb.terminate()
        xvfb.wait(timeout=3)


@pytest.mark.parametrize(
    "mutation",
    ("observed", "wrong-parent", "extra-client-descendant", "second-input-only-helper"),
)
def test_root_child_verifier_replays_input87_tree_and_rejects_unobserved_subtrees(
    mutation: str,
) -> None:
    """Replay the retained live client tree without admitting root or siblings."""

    target_xid = 0x4800003
    helper_xid = 0x4800004
    root_xid = 0x374
    extra_xid = 0x4800005
    parent_xid = 0x123 if mutation == "wrong-parent" else root_xid
    child_rows = [f"     {hex(helper_xid)} (has no name): ()  1x1+-1+-1  +1439+31"]
    if mutation == "extra-client-descendant":
        child_rows.append(f'     {hex(extra_xid)} "extra": ("fixture" "fixture")  10x10+0+0  +1440+32')
    elif mutation == "second-input-only-helper":
        child_rows.append(f"     {hex(extra_xid)} (has no name): ()  1x1+-1+-1  +1439+31")
    tree = (
        f'xwininfo: Window id: {hex(target_xid)} "CROWLEY INPUT CALIBRATION ONLY"\n\n'
        f"  Root window id: {hex(root_xid)} (the root window) (has no name)\n"
        f"  Parent window id: {hex(parent_xid)} (the root window) (has no name)\n"
        f"     {len(child_rows)} children:\n" + "\n".join(child_rows) + "\n"
    )
    helper_stats = (
        f"xwininfo: Window id: {hex(helper_xid)}\n\n"
        "  Absolute upper-left X:  1439\n"
        "  Absolute upper-left Y:  31\n"
        "  Relative upper-left X:  -1\n"
        "  Relative upper-left Y:  -1\n"
        "  Width: 1\n"
        "  Height: 1\n"
        "  Depth: 0\n"
        "  Border width: 0\n"
        "  Class: InputOnly\n"
    )
    extra_stats = (
        f"xwininfo: Window id: {hex(extra_xid)}\n\n"
        "  Absolute upper-left X:  1440\n"
        "  Absolute upper-left Y:  32\n"
        "  Relative upper-left X:  0\n"
        "  Relative upper-left Y:  0\n"
        "  Width: 10\n"
        "  Height: 10\n"
        "  Depth: 24\n"
        "  Border width: 0\n"
        "  Class: InputOutput\n"
    )
    commands: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> str:
        commands.append(tuple(argv))
        if argv == ["xwininfo", "-id", hex(target_xid), "-tree"]:
            return tree
        if argv == ["xwininfo", "-id", hex(helper_xid), "-stats"]:
            return helper_stats
        if argv == ["xwininfo", "-id", hex(extra_xid), "-stats"]:
            return (
                extra_stats
                if mutation == "extra-client-descendant"
                else helper_stats.replace(hex(helper_xid), hex(extra_xid))
            )

        raise AssertionError(f"unexpected root-child query: {argv!r}")

    target = OwnedGtkCalibrationWindow(
        display=":input87-observer",
        xauthority="/tmp/input87-observer.Xauthority",
        layout_kind=OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD,
        command=lambda argv, **_kwargs: command(argv),
    )
    if mutation == "observed":
        window = target._verify_root_child_layout(
            pid=569822,
            xid=target_xid,
            process=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
            executable="/usr/bin/python3",
            process_start="50072661",
            geometry=(1440, 32, 480, 240),
            advertised_helper_xid=helper_xid,
            startup_readiness=False,
            run_command=command,
        )
        assert window.root_xid == window.parent_xid == root_xid
        assert window.descendant_xids == {target_xid, helper_xid}
        assert window.allowed_event_target_xids == {target_xid}
        assert window.excluded_input_only_xids == {helper_xid}
        assert root_xid not in window.allowed_event_target_xids
        assert all(row[:3] != ("xwininfo", "-root", "-tree") for row in commands)
    else:
        with pytest.raises(
            CalibrationError,
            match="directly parented|unexpected client descendant|certificate|metadata helper",
        ):
            target._verify_root_child_layout(
                pid=569822,
                xid=target_xid,
                process=SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
                executable="/usr/bin/python3",
                process_start="50072661",
                geometry=(1440, 32, 480, 240),
                advertised_helper_xid=helper_xid,
                startup_readiness=False,
                run_command=command,
            )


def _verify_root_child_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int,
    target_xid: int = TARGET,
    helper_xid: int = TARGET_DESCENDANT,
) -> tuple[OwnedGtkCalibrationWindow, dict[str, object], list[tuple[str, ...]]]:
    import crowley.session.calibration as calibration_module

    state, commands, command = _root_child_gtk_fixture_command(pid=pid, target=target_xid, helper=helper_xid)

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid != pid:
            raise AssertionError(process_pid)
        return "/usr/bin/python3", "fixture-start", "python3"

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(
        display=":root-child-verifier",
        xauthority="private.Xauthority",
        layout_kind=OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD,
        command=command,
    )
    return target, state, commands


def test_root_child_owner_binds_advertised_user_time_helper_to_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = os.getpid()
    target, _state, commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    window = target._verify(
        pid,
        TARGET,
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        startup_readiness=True,
    )
    assert window.excluded_input_only_xids == frozenset({TARGET_DESCENDANT})
    assert window.allowed_event_target_xids == frozenset({TARGET})
    assert window.descendant_xids == frozenset({TARGET, TARGET_DESCENDANT})
    assert ("xwininfo", "-id", hex(TARGET_DESCENDANT), "-stats") in commands


def test_root_child_missing_advertised_helper_waits_for_publication_then_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    target, state, commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    state["user_time_window"] = None
    process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(calibration_module._GtkStartupPending, match="helper identity"):
        target._verify(pid, TARGET, process, startup_readiness=True)  # type: ignore[arg-type]
    assert not any(args[-1:] == ("-tree",) for args in commands)

    state["user_time_window"] = f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(TARGET_DESCENDANT)}\n"
    window = target._verify(pid, TARGET, process, startup_readiness=True)  # type: ignore[arg-type]
    assert window.excluded_input_only_xids == frozenset({TARGET_DESCENDANT})


def test_root_child_absent_helper_is_hard_failure_after_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = os.getpid()
    target, state, _commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    state["user_time_window"] = None
    with pytest.raises(CalibrationError, match="helper identity"):
        target._verify(
            pid,
            TARGET,
            SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
            startup_readiness=False,
        )


@pytest.mark.parametrize(
    "publication",
    (
        "_NET_WM_USER_TIME_WINDOW(CARDINAL) = 123\n",
        "_NET_WM_USER_TIME_WINDOW(WINDOW): window id # 0x60011a\n"
        "_NET_WM_USER_TIME_WINDOW(WINDOW): window id # 0x60011a\n",
    ),
    ids=("wrong-property-type", "duplicate-property"),
)
def test_root_child_owner_rejects_malformed_or_duplicate_helper_publication(
    monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    pid = os.getpid()
    target, state, commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    state["user_time_window"] = publication
    state["identity_companions"] = False
    with pytest.raises(CalibrationError, match="property|duplicated") as failure:
        target._verify(
            pid,
            TARGET,
            SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
            startup_readiness=True,
        )
    import crowley.session.calibration as calibration_module

    assert not isinstance(failure.value, calibration_module._GtkStartupPending)
    assert not any(args[:2] == ("xwininfo", "-id") for args in commands)


@pytest.mark.parametrize(
    ("published_xid", "expected_query_count"),
    ((TARGET, 0), (0x374, 1), (0x60011B, 1)),
    ids=("client-self", "root", "wrong-helper"),
)
def test_root_child_owner_rejects_unbound_helper_xid_before_certificate(
    monkeypatch: pytest.MonkeyPatch, published_xid: int, expected_query_count: int
) -> None:
    pid = os.getpid()
    target, state, commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    state["user_time_window"] = f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(published_xid)}\n"
    state["map_state"] = None
    with pytest.raises(CalibrationError, match="helper|identity|advertised") as failure:
        target._verify(
            pid,
            TARGET,
            SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
            startup_readiness=True,
        )
    import crowley.session.calibration as calibration_module

    assert not isinstance(failure.value, calibration_module._GtkStartupPending)
    tree_queries = sum(args[-1:] == ("-tree",) for args in commands)
    assert tree_queries == expected_query_count
    assert ("xwininfo", "-id", hex(TARGET_DESCENDANT), "-stats") not in commands


def test_root_child_missing_helper_subtree_is_pending_only_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    target, state, _commands = _verify_root_child_fixture(monkeypatch, pid=pid)
    state["children"] = ()
    process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(calibration_module._GtkStartupPending, match="subtree"):
        target._verify(pid, TARGET, process, startup_readiness=True)  # type: ignore[arg-type]
    with pytest.raises(CalibrationError, match="tree|helper") as failure:
        target._verify(pid, TARGET, process, startup_readiness=False)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_create_readiness_deadline_is_shrinking_and_reaps_pending_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A never-ready client times out once and cannot leak or respawn a child."""

    import crowley.session.calibration as calibration_module

    processes: list[object] = []
    verify_deadlines: list[float] = []
    lifecycle: list[str] = []
    registration_cleanup_calls: list[int] = []

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append("terminate")
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            lifecycle.append("kill")
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            lifecycle.append("wait")
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pid = 602_000 + len(processes)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        gate_reader = os.dup(int(argv[wrapper_index + 1]))
        process = FakeProcess(pid, os.fdopen(read_fd, "r", encoding="utf-8"), gate_reader)
        processes.append(process)
        return process

    def verify(
        _target: OwnedGtkCalibrationWindow,
        _pid: int,
        _xid: int,
        _process: object,
        **kwargs: object,
    ) -> object:
        deadline = kwargs["deadline"]
        assert isinstance(deadline, float)
        verify_deadlines.append(deadline)
        raise calibration_module._GtkStartupPending("GTK frame remains pending")

    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-readiness-timeout",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )

    def admit(_pid: int, release: Callable[[], None]) -> Callable[[], None]:
        release()

        def cleanup() -> None:
            registration_cleanup_calls.append(1)

        return cleanup

    with pytest.raises(CalibrationError, match="startup deadline"):
        target.create(timeout=0.025, startup_admission=admit)
    assert len(processes) == 1
    assert verify_deadlines
    assert len(set(verify_deadlines)) == 1
    assert lifecycle[:2] == ["terminate", "wait"]
    assert registration_cleanup_calls == [1]
    assert processes[0].poll() is not None
    processes[0].stdout.close()  # type: ignore[attr-defined]


def test_gtk_create_deadline_includes_startup_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Admission consumes the one create budget before any certificate query."""

    import crowley.session.calibration as calibration_module

    clock = [100.0]
    verify_calls = 0
    processes: list[object] = []

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pid = 603_500
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        process = FakeProcess(
            pid,
            os.fdopen(read_fd, "r", encoding="utf-8"),
            os.dup(int(argv[wrapper_index + 1])),
        )
        processes.append(process)
        return process

    def verify(*_args: object, **_kwargs: object) -> object:
        nonlocal verify_calls
        verify_calls += 1
        return SimpleNamespace(pid=603_500, xid=TARGET, process=processes[0])

    monkeypatch.setattr(calibration_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-admission-deadline",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )

    def admit(_pid: int, release: Callable[[], None]) -> None:
        clock[0] += 0.2
        release()

    with pytest.raises(CalibrationError, match="startup deadline"):
        target.create(timeout=0.1, startup_admission=admit)
    assert verify_calls == 0
    assert processes[0].poll() is not None
    processes[0].stdout.close()  # type: ignore[attr-defined]


def test_gtk_create_late_final_certificate_reaps_and_unregisters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A certificate that arrives after the absolute deadline is not retained."""

    import crowley.session.calibration as calibration_module

    clock = [300.0]
    processes: list[object] = []
    cleanup_calls: list[int] = []

    class FakeProcess:
        def __init__(self, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = 603_600
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": 603_600, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        process = FakeProcess(os.fdopen(read_fd, "r", encoding="utf-8"), os.dup(int(argv[wrapper_index + 1])))
        processes.append(process)
        return process

    def verify(*_args: object, **_kwargs: object) -> object:
        clock[0] += 0.2
        return SimpleNamespace(pid=603_600, xid=TARGET, process=processes[0])

    monkeypatch.setattr(calibration_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-late-certificate",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )

    def admit(_pid: int, release: Callable[[], None]) -> Callable[[], None]:
        release()

        def cleanup() -> None:
            cleanup_calls.append(1)

        return cleanup

    with pytest.raises(CalibrationError, match="startup deadline"):
        target.create(timeout=0.1, startup_admission=admit)
    assert processes[0].poll() is not None
    assert cleanup_calls == [1]
    processes[0].stdout.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("writer_open", "message"),
    [(True, "identity was incomplete"), (False, "identity channel closed before identity")],
)
def test_gtk_create_partial_identity_does_not_call_blocking_readline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, writer_open: bool, message: str
) -> None:
    """A partial identity is bounded whether its writer remains open or closes."""

    import crowley.session.calibration as calibration_module

    clock = [200.0]
    select_calls = 0
    processes: list[object] = []
    lifecycle: list[str] = []
    registration_cleanup_calls: list[int] = []

    class PartialStdout:
        def __init__(self, read_fd: int) -> None:
            self.read_fd = read_fd

        def fileno(self) -> int:
            return self.read_fd

        def readline(self) -> str:
            raise AssertionError("blocking readline called for partial identity")

        def close(self) -> None:
            os.close(self.read_fd)

    class FakeProcess:
        def __init__(self, pid: int, stdout: PartialStdout, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self.gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append("terminate")
            if self.gate_reader >= 0:
                os.close(self.gate_reader)
                self.gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            lifecycle.append("wait")
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pid = 603_501
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"pid": 603501')
        if not writer_open:
            os.close(write_fd)
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        process = FakeProcess(
            pid,
            PartialStdout(read_fd),
            os.dup(int(argv[wrapper_index + 1])),
        )
        processes.append(process)
        if writer_open:
            # Keep the writer open: this variant is specifically a partial live line.
            process.writer = write_fd  # type: ignore[attr-defined]
        return process

    def fake_select(_read: object, _write: object, _error: object, _timeout: float):
        nonlocal select_calls
        select_calls += 1
        if select_calls == 1:
            return (_read, [], [])
        if not writer_open:
            return (_read, [], [])
        clock[0] += 0.2
        return ([], [], [])

    monkeypatch.setattr(calibration_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(calibration_module.select, "select", fake_select)
    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-partial-identity",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )

    def admit(_pid: int, release: Callable[[], None]) -> Callable[[], None]:
        release()

        def cleanup() -> None:
            registration_cleanup_calls.append(1)

        return cleanup

    try:
        with pytest.raises(CalibrationError, match=rf"^GTK calibration {re.escape(message)}$"):
            target.create(timeout=0.1, startup_admission=admit)
        assert lifecycle == ["terminate", "wait"]
        assert registration_cleanup_calls == [1]
        assert processes[0].poll() is not None
    finally:
        if processes:
            if writer_open:
                os.close(processes[0].writer)  # type: ignore[attr-defined]
            processes[0].stdout.close()  # type: ignore[attr-defined]


def test_gtk_startup_rejects_wrong_present_identity_with_missing_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong present WM_NAME is hard failure even when WM_PID is absent."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xprop", "-id", hex(TARGET)]:
            return 'WM_NAME(STRING) = "WRONG TITLE"\n'
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="property|properties") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_duplicate_present_property_with_missing_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate present WM_NAME is hard failure even when WM_PID is absent."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xprop", "-id", hex(TARGET)]:
            return (
                'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
            )
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="property|properties") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_wrong_present_wmctrl_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-XID row with a different PID cannot become a pending absence."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return f"{hex(TARGET)} -1 {pid + 1} host CROWLEY INPUT CALIBRATION ONLY\n"
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="title/PID") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_second_same_pid_title_wmctrl_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second same-owner title cannot be discarded as an unrelated XID."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return (
                f"{hex(TARGET)} -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
                f"0x600194 -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
            )
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="title/PID") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_alternate_only_same_pid_title_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alternate-only same-owner row is contradictory, not initial absence."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return f"0x600194 -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="title/PID") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_unavailable_published_frame_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published frame PID with unavailable /proc identity is never pending."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            raise CalibrationError("identity unavailable")
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="frame process identity is unavailable") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_unavailable_frame_pid_with_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published frame PID is hard-invalid even while another property is absent."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    frame = 0x600192
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=frame,
        helper=0x600193,
        child=0x600121,
        frame_props=(
            f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
            f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
            'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
        ),
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            raise CalibrationError("identity unavailable")
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=base)
    with pytest.raises(CalibrationError, match="frame process identity is unavailable") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_unavailable_client_identity_with_missing_frame_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published client identity is hard-invalid before frame readiness can pend."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
        frame_props=(
            f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
            f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
            'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
        ),
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            raise CalibrationError("identity unavailable")
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="owned GTK process identity is unavailable") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_malformed_present_extents_with_missing_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed extents are hard failure even while map state is absent."""

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xprop", "-id", hex(TARGET)]:
            return (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                f"_NET_WM_PID(CARDINAL) = {pid}\n"
                "_NET_FRAME_EXTENTS(CARDINAL) = 5, malformed, 5, 38\n"
            )
        if args == ["xwininfo", "-id", hex(TARGET), "-stats"]:
            return "Absolute upper-left X: 753\nAbsolute upper-left Y: 456\nWidth: 480\nHeight: 240\n"
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    import crowley.session.calibration as calibration_module

    with pytest.raises(CalibrationError, match="frame extents") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_extra_child_before_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected frame child is contradictory even if the advertised helper is absent."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
        advertised_helper=0x600194,
        extra_frame_child=FOREIGN,
    )
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="children") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_multiple_frame_children_before_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two non-client frame children are contradictory before helper publication."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
        extra_frame_child=FOREIGN,
        frame_props=(
            f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
            f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
            'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
        ),
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="children") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_startup_rejects_known_unexpected_child_before_missing_frame_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published helper plus one wrong child is hard even with another property pending."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame = 0x600192
    helper = 0x600193
    frame_pid = 5717
    frame_props = (
        f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
        f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(helper)}\n"
        f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
        # Deliberately omit WM_CLASS while the published helper and wrong child
        # make the tree contradiction independently conclusive.
    )
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=frame,
        helper=helper,
        child=0x600121,
        extra_frame_child=FOREIGN,
        frame_props=frame_props,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xwininfo", "-root", "-tree"]:
            tree = base(args, **kwargs)
            return tree.replace(
                f"    {hex(helper)} (has no name): () 1x1+-1+-1  +691+361\n",
                "",
            )
        return base(args, **kwargs)

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="children") as failure:
        target._verify(pid, TARGET, object(), startup_readiness=True)  # type: ignore[arg-type]
    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_post_ready_validation_does_not_skip_map_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-ready certificate checks always require a complete map state."""

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xwininfo", "-id", hex(TARGET), "-stats"]:
            return "Absolute upper-left X: 753\nAbsolute upper-left Y: 456\nWidth: 480\nHeight: 240\n"
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="map state"):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_gtk_post_ready_unmapped_state_fails_hard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned certificate cannot receive startup grace for an unmapped client."""

    pid = os.getpid()
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def command(args: list[str], **kwargs: object) -> str:
        if args == ["xwininfo", "-id", hex(TARGET), "-stats"]:
            return (
                "Absolute upper-left X: 753\nAbsolute upper-left Y: 456\n"
                "Width: 480\nHeight: 240\nMap State: IsUnMapped\n"
            )
        return base(args, **kwargs)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="IsUnMapped") as failure:
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]
    import crowley.session.calibration as calibration_module

    assert not isinstance(failure.value, calibration_module._GtkStartupPending)


def test_gtk_failure_reaps_before_surface_registration_cleanup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cleanup exception cannot skip termination and process reap."""

    import crowley.session.calibration as calibration_module

    lifecycle: list[str] = []
    processes: list[object] = []

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self._gate_reader = gate_reader

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append("terminate")
            if self._gate_reader >= 0:
                os.close(self._gate_reader)
                self._gate_reader = -1
            self.returncode = -15

        def kill(self) -> None:
            lifecycle.append("kill")
            if self._gate_reader >= 0:
                os.close(self._gate_reader)
                self._gate_reader = -1
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            lifecycle.append("wait")
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(_args: list[str], **_kwargs: object) -> FakeProcess:
        pid = 501_000
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        os.close(write_fd)
        wrapper_index = _args.index(_EXEC_ADMISSION_WRAPPER)
        gate_reader = os.dup(int(_args[wrapper_index + 1]))
        process = FakeProcess(pid, os.fdopen(read_fd, "r", encoding="utf-8"), gate_reader)
        processes.append(process)
        return process

    def verify(
        _target: OwnedGtkCalibrationWindow,
        _pid: int,
        _xid: int,
        _process: object,
        **_kwargs: object,
    ) -> object:
        raise CalibrationError("synthetic GTK verification failure")

    def admit(_pid: int, release: Callable[[], None]) -> Callable[[], None]:
        release()

        def cleanup() -> None:
            raise RuntimeError("synthetic registration cleanup failure")

        return cleanup

    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    target = OwnedGtkCalibrationWindow(
        display=":gtk-cleanup-error",
        xauthority=str(tmp_path / "Xauthority"),
        python_executable=sys.executable,
    )
    try:
        with pytest.raises(CalibrationError, match="clean up GTK target") as failure:
            target.create(startup_admission=admit)
        assert isinstance(failure.value.__cause__, RuntimeError)
        assert lifecycle[:2] == ["terminate", "wait"]
    finally:
        for process in processes:
            process.terminate()  # type: ignore[attr-defined]
            process.stdout.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize("identity_failure", ("timeout", "nonjson", "incomplete", "pid-mismatch"))
def test_gtk_identity_failure_reaps_unregisters_and_allows_same_driver_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, identity_failure: str
) -> None:
    """Startup identity failures leave the Guardian retryable in one epoch fence."""

    import crowley.session.calibration as calibration_module

    profile = DriverProfile(
        display=f":gtk-identity-{identity_failure}", xauthority=str(tmp_path / "Xauthority")
    )
    driver = DesktopDriver(
        profile=profile,
        backend=RecordingBackend(),
        lease_path=tmp_path / f"gtk-identity-{identity_failure}.lock",
    )
    processes: list[object] = []
    lifecycle: list[str] = []

    class FakeProcess:
        def __init__(self, pid: int, stdout: io.TextIOBase, gate_reader: int, writer: int) -> None:
            self.pid = pid
            self.stdout = stdout
            self.stdin = io.StringIO()
            self.returncode: int | None = None
            self._gate_reader = gate_reader
            self._writer = writer

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append("terminate")
            for fd_name in ("_gate_reader", "_writer"):
                fd = getattr(self, fd_name)
                if fd >= 0:
                    os.close(fd)
                    setattr(self, fd_name, -1)
            self.returncode = -15

        def kill(self) -> None:
            lifecycle.append("kill")
            self.terminate()
            self.returncode = -9

        def wait(self, timeout: float = 2.0) -> int:
            del timeout
            lifecycle.append("wait")
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(argv: list[str], **_kwargs: object) -> FakeProcess:
        pid = 510_000 + len(processes)
        read_fd, write_fd = os.pipe()
        if not processes:
            if identity_failure == "nonjson":
                os.write(write_fd, b"not-json\n")
            elif identity_failure == "incomplete":
                os.write(write_fd, b"{}\n")
            elif identity_failure == "pid-mismatch":
                os.write(write_fd, (json.dumps({"pid": pid + 1, "xid": TARGET}) + "\n").encode())
        else:
            os.write(write_fd, (json.dumps({"pid": pid, "xid": TARGET}) + "\n").encode())
        if identity_failure != "timeout" or processes:
            os.close(write_fd)
            writer = -1
        else:
            writer = write_fd
        wrapper_index = argv.index(_EXEC_ADMISSION_WRAPPER)
        gate_reader = os.dup(int(argv[wrapper_index + 1]))
        process = FakeProcess(pid, os.fdopen(read_fd, "r", encoding="utf-8"), gate_reader, writer)
        processes.append(process)
        return process

    def verify(
        _target: OwnedGtkCalibrationWindow,
        pid: int,
        xid: int,
        process: object,
        **_kwargs: object,
    ) -> object:
        return SimpleNamespace(pid=pid, xid=xid, process=process)

    monkeypatch.setattr(calibration_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    original_unregister = driver.guardian.unregister_process_group

    def record_unregister(pgid: int) -> None:
        lifecycle.append("unregister")
        original_unregister(pgid)

    driver.guardian.unregister_process_group = record_unregister
    try:
        driver.begin_calibration_operation()
        target = OwnedGtkCalibrationWindow(
            display=profile.display,
            xauthority=profile.xauthority,
            python_executable=sys.executable,
        )
        with pytest.raises(CalibrationError):
            target.create(timeout=0.02, startup_admission=driver.admit_calibration_target_process)
        assert lifecycle[:3] == ["terminate", "wait", "unregister"]
        assert not driver.guardian.active_process_groups
        assert not driver._calibration_bootstrap_process_groups

        driver.begin_calibration_operation()
        retry = OwnedGtkCalibrationWindow(
            display=profile.display,
            xauthority=profile.xauthority,
            python_executable=sys.executable,
        )
        window = retry.create(startup_admission=driver.admit_calibration_target_process)
        assert window.pid == processes[-1].pid
        assert driver.guardian.active_process_groups == frozenset({window.pid})
    finally:
        for process in processes:
            process.terminate()  # type: ignore[attr-defined]
            process.stdout.close()  # type: ignore[attr-defined]
        driver.close()


def test_owned_gtk_target_rejects_duplicate_identity_and_missing_ancestry() -> None:
    frame = 0x600130
    pid = os.getpid()

    def command(args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return f"{hex(TARGET)}  -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        if args == ["xprop", "-id", hex(TARGET)]:
            return (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                f"_NET_WM_PID(CARDINAL) = {pid}\n"
                "_NET_FRAME_EXTENTS(CARDINAL) = 5, 31, 5, 38\n"
            )
        if args[0] == "xwininfo" and "-tree" in args:
            return f"xwininfo: Window id: 0x374 root\n  {hex(frame)} (foreign)\n"
        if args[0] == "xwininfo":
            return (
                "Absolute upper-left X: 11\nAbsolute upper-left Y: 22\n"
                "Width: 480\nHeight: 240\nMap State: IsViewable\n"
            )
        if args[:2] == ["xdotool", "windowactivate"]:
            return ""
        if args[:2] == ["xdotool", "getwindowfocus"]:
            return "0x600119\n"
        raise AssertionError(args)

    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="ancestry"):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_owned_gtk_tree_accepts_target_and_advertised_user_time_helper_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_token = 0x374
    frame = 0x600192
    helper = 0x600193
    child = 0x600121
    frame_pid = 5717
    pid = os.getpid()

    import crowley.session.calibration as calibration_module

    def stats(x: int, y: int, width: int, height: int) -> str:
        return f"Absolute upper-left X: {x}\nAbsolute upper-left Y: {y}\nWidth: {width}\nHeight: {height}\n"

    def command(args: list[str], **_kwargs: object) -> str:
        if args[:2] == ["wmctrl", "-lp"]:
            return f"{hex(TARGET)}  -1 {pid} host CROWLEY INPUT CALIBRATION ONLY\n"
        if args == ["xprop", "-id", hex(TARGET)]:
            return (
                f'WM_NAME(STRING) = "CROWLEY INPUT CALIBRATION ONLY"\n'
                f"_NET_WM_PID(CARDINAL) = {pid}\n"
                "_NET_FRAME_EXTENTS(CARDINAL) = 5, 31, 5, 38\n"
            )
        if args == ["xprop", "-id", hex(frame)]:
            return (
                f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
                f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(helper)}\n"
                f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
                'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
            )
        if args == ["xprop", "-id", hex(helper)]:
            return "_NET_WM_USER_TIME(CARDINAL) = 276992109\n"
        if args[0] == "xprop":
            raise AssertionError(args)
        if args[0] == "xwininfo" and "-tree" in args:
            return (
                f"xwininfo: Window id: {hex(root_token)} (the root window)\n"
                f'  {hex(frame)} "CROWLEY INPUT CALIBRATION ONLY": '
                f'("mutter-x11-frames" "mutter-x11-frames") 602x401+692+362  +692+362\n'
                f'    {hex(TARGET)} "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
                "480x240+61+94  +753+456\n"
                f"      {hex(child)} (has no name): () 1x1+-1+-1  +752+455\n"
                f"    {hex(helper)} (has no name): () 1x1+-1+-1  +691+361\n"
            )
        if args == ["xwininfo", "-id", hex(frame), "-stats"]:
            return stats(692, 362, 602, 401) + "Depth: 32\nBorder width: 0\nClass: InputOutput\n"
        if args == ["xwininfo", "-id", hex(TARGET), "-stats"]:
            return (
                stats(753, 456, 480, 240)
                + "Depth: 24\nBorder width: 0\nClass: InputOutput\nMap State: IsViewable\n"
            )
        if args == ["xwininfo", "-id", hex(helper), "-stats"]:
            return (
                stats(691, 361, 1, 1) + "Relative upper-left X: -1\nRelative upper-left Y: -1\n"
                "Depth: 0\nBorder width: 0\nClass: InputOnly\n"
            )
        if args[:2] == ["xdotool", "windowactivate"]:
            return ""
        if args[:2] == ["xdotool", "getwindowfocus"]:
            return hex(TARGET) + "\n"
        raise AssertionError(args)

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    window = target._verify(pid, TARGET, object())  # type: ignore[arg-type]
    assert window.allowed_event_target_xids == frozenset({frame, TARGET, child})
    assert window.contains_xid(TARGET)
    assert window.contains_xid(child)
    assert not window.contains_xid(helper)
    assert not window.contains_xid(root_token)
    assert not window.contains_xid(0x600130)


@pytest.mark.parametrize(
    ("variant", "error"),
    [
        ("mismatched-frame-for", "frame identity or advertised helper"),
        ("mismatched-helper", "frame identity or advertised helper|children do not match"),
        ("missing-frame-for", "frame properties|X11 property"),
        ("duplicate-frame-for", "frame properties|X11 property"),
        ("conflicting-frame-for-type", "frame properties|X11 property"),
        ("conflicting-helper-type", "frame properties|X11 property"),
        ("zero-user-time", "user-time property is invalid"),
        ("negative-user-time", "user-time property is incomplete"),
    ],
)
def test_owned_gtk_certificate_rejects_malformed_frame_and_helper_properties(
    variant: str,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame = 0x600192
    helper = 0x600193
    frame_pid = 5717
    valid_frame_props = (
        f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
        f"_NET_WM_USER_TIME_WINDOW(WINDOW): window id # {hex(helper)}\n"
        f"_NET_WM_PID(CARDINAL) = {frame_pid}\n"
        'WM_CLASS(STRING) = "mutter-x11-frames", "mutter-x11-frames"\n'
    )
    frame_props = valid_frame_props
    helper_props: str | None = None
    if variant == "mismatched-frame-for":
        frame_props = valid_frame_props.replace(hex(TARGET), "0x600194", 1)
    elif variant == "mismatched-helper":
        frame_props = valid_frame_props.replace(hex(helper), hex(TARGET), 1)
    elif variant == "missing-frame-for":
        frame_props = valid_frame_props.replace(f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n", "")
    elif variant == "duplicate-frame-for":
        frame_props = valid_frame_props + f"_MUTTER_FRAME_FOR(WINDOW): window id # {hex(TARGET)}\n"
    elif variant == "conflicting-frame-for-type":
        frame_props = valid_frame_props + "_MUTTER_FRAME_FOR(CARDINAL) = 1\n"
    elif variant == "conflicting-helper-type":
        frame_props = valid_frame_props + "_NET_WM_USER_TIME_WINDOW(CARDINAL) = 1\n"
    elif variant == "zero-user-time":
        helper_props = "_NET_WM_USER_TIME(CARDINAL) = 0\n"
    elif variant == "negative-user-time":
        helper_props = "_NET_WM_USER_TIME(CARDINAL) = -1\n"
    else:
        raise AssertionError(variant)
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=frame,
        helper=helper,
        child=0x600121,
        frame_props=frame_props,
        helper_props=helper_props,
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match=error):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_owned_gtk_certificate_rejects_unadvertised_frame_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
        extra_frame_child=FOREIGN,
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="children do not match"):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_owned_gtk_certificate_rejects_wrong_helper_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    wrong_helper_stats = (
        "Absolute upper-left X: 691\nAbsolute upper-left Y: 361\nWidth: 2\nHeight: 1\n"
        "Relative upper-left X: -1\nRelative upper-left Y: -1\n"
        "Depth: 0\nBorder width: 0\nClass: InputOnly\n"
    )
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
        helper_stats=wrong_helper_stats,
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="helper geometry or class"):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_owned_gtk_certificate_rejects_wrong_mutter_process_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame_pid = 5717
    command = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=0x600192,
        helper=0x600193,
        child=0x600121,
    )

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == frame_pid:
            return "/usr/bin/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(CalibrationError, match="not the observed Mutter frame"):
        target._verify(pid, TARGET, object())  # type: ignore[arg-type]


def test_owned_gtk_startup_direct_root_sibling_waits_for_fresh_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct-root client and tiny same-title sibling never form a frame."""

    import crowley.session.calibration as calibration_module

    pid = os.getpid()
    frame = 0x600192
    helper = 0x600193
    child = 0x600121
    base = _mutter_gtk_fixture_command(
        pid=pid,
        target=TARGET,
        frame=frame,
        helper=helper,
        child=child,
    )
    startup_tree = (
        "xwininfo: Window id: 0x374 (the root window)\n"
        f'  {hex(TARGET)} "CROWLEY INPUT CALIBRATION ONLY": ("-c" "-c") '
        "480x240+753+456  +753+456\n"
        f'  {hex(frame)} "CROWLEY INPUT CALIBRATION ONLY": '
        '("mutter-x11-frames" "mutter-x11-frames") 1x1+1+1  +1+1\n'
    )
    tree_calls = 0

    def command(args: list[str], **kwargs: object) -> str:
        nonlocal tree_calls
        if args == ["xwininfo", "-root", "-tree"]:
            tree_calls += 1
            if tree_calls == 1:
                return startup_tree
        return base(args, **kwargs)

    def process_identity(process_pid: int) -> tuple[str, str, str]:
        if process_pid == pid:
            return "/usr/bin/python3", "client-start", "python3"
        if process_pid == 5717:
            return "/usr/libexec/mutter-x11-frames", "frame-start", "mutter-x11-fram"
        raise AssertionError(process_pid)

    monkeypatch.setattr(calibration_module, "_process_identity", process_identity)
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority", command=command)
    with pytest.raises(calibration_module._GtkStartupPending, match="reparented"):
        target._verify(pid, TARGET, object(), timeout=1.0, startup_readiness=True)  # type: ignore[arg-type]
    window = target._verify(pid, TARGET, object(), timeout=1.0, startup_readiness=True)  # type: ignore[arg-type]
    assert window.managed_frame_xid == frame
    assert tree_calls == 2


def test_owned_gtk_prompt_recertifies_ready_certificate_without_startup_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt acknowledgement cannot preserve a certificate after structural drift."""

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"PROMPT_ACK\n")
    os.close(write_fd)
    stdout = os.fdopen(read_fd, "r", encoding="utf-8")

    class FakeProcess:
        pid = 603_001
        stdin = io.StringIO()

        def poll(self) -> None:
            return None

    process = FakeProcess()
    original = OwnedWindow(
        process.pid,
        TARGET,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        process,  # type: ignore[arg-type]
        frozenset({TARGET}),
        "/usr/bin/python3",
        "start",
        0x600192,
        (0, 0, 500, 260),
        frozenset({TARGET, 0x600192}),
    )
    drifted = replace(original, geometry=(1, 0, 480, 240))
    target = OwnedGtkCalibrationWindow(display=":test", xauthority="Xauthority")
    target.window = replace(original, process=process)  # type: ignore[arg-type]
    process.stdout = stdout  # type: ignore[attr-defined]
    calls: list[bool] = []

    def verify(
        _target: OwnedGtkCalibrationWindow,
        _pid: int,
        _xid: int,
        _process: object,
        **kwargs: object,
    ) -> OwnedWindow:
        calls.append(bool(kwargs.get("startup_readiness", False)))
        return drifted

    monkeypatch.setattr(OwnedGtkCalibrationWindow, "_verify", verify)
    try:
        with pytest.raises(CalibrationError, match="certificate changed"):
            target.show_human_prompt()
        assert calls == [False]
    finally:
        stdout.close()


class _WorkflowObserver:
    def __init__(self, callback, records: list[XI2Event], human: list[XI2Event], *, outside: bool):
        self.callback = callback
        self.records = records
        self.human = human
        self.outside = outside
        self._event_list: list[XI2Event] = []

    def _add(self, event: XI2Event) -> None:
        self._event_list.append(event)
        self.callback(event)

    def collect_until(self, predicate, *, timeout: float) -> str:
        for event in self.records:
            self._add(event)
        assert predicate(self._event_list)
        return _render_events(self.records)

    def wait_for_human(self, predicate, *, timeout: float) -> None:
        if self.outside:
            self._add(parse_xi2_records(FULL_XI2_TRACE, require_event=True)[-1])
        for event in self.human:
            self._add(event)

    def events(self) -> list[XI2Event]:
        return list(self._event_list)

    def stop(self) -> str:
        return _render_events(self._event_list)


class _WorkflowTarget:
    def __init__(self) -> None:
        self.window = OwnedWindow(
            os.getpid(),
            TARGET,
            OwnedGtkCalibrationWindow.TITLE,
            (800, 400, 480, 240),
            SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
            frozenset({TARGET}),
            "/usr/bin/python3",
            "owned-target-start",
            TARGET + 1,
            (800, 400, 500, 260),
            frozenset({TARGET, TARGET + 1}),
        )
        self.prompted = False

    def create(self) -> OwnedWindow:
        return self.window

    def close(self) -> None:
        return None

    def show_human_prompt(self) -> None:
        self.prompted = True


class _WorkflowDriver:
    profile = SimpleNamespace(display=":test", xauthority="Xauthority")
    driver_version = "native-x11-3"
    xauthority = "Xauthority"

    def __init__(self, *, outside: bool):
        records = parse_xi2_records(FULL_XI2_TRACE, require_event=True)
        # Mark this deterministic observer fixture as the native X11 path so
        # the workflow seals and replays its canonical raw/primary transition.
        self.backend = object.__new__(X11Backend)
        self.backend.pointer_position = lambda **_kwargs: (987, 595, TARGET)  # type: ignore[method-assign]
        self.observer: _WorkflowObserver | None = None
        self.revocations: list[str] = []
        self.queued_calibration_count = 0
        self.calibration_executions = 0
        self.outside = outside
        self._records = records[:11]
        # The synthetic phase owns a source-4 delivered motion.  The human
        # suffix below supplies the independent source-7 takeover motion;
        # only the former may participate in an echo proof.
        self._records.insert(
            7,
            replace(
                _event(
                    EventClass.RAW_MOTION,
                    master=2,
                    source=4,
                    timestamp=232007474,
                    detail=0,
                    child=None,
                ),
                valuators=((0, 988.0, 988.0), (1, 596.0, 596.0)),
            ),
        )
        self._records.insert(
            8,
            replace(
                _event(EventClass.MOTION, master=4, source=4, timestamp=232007474, detail=0),
                valuators=((0, 988.0, 988.0), (1, 596.0, 596.0)),
            ),
        )
        self._records.insert(
            9,
            replace(
                _event(EventClass.RAW_MOTION, master=2, source=4, timestamp=232007475, detail=0, child=None),
                valuators=((0, 989.0, 989.0), (1, 597.0, 597.0)),
            ),
        )
        self._records.insert(
            10,
            replace(
                _event(
                    EventClass.MOTION,
                    master=4,
                    source=4,
                    timestamp=232007475,
                    detail=0,
                    root_xy=(988.0, 596.0),
                    event_xy=(988.0, 596.0),
                ),
                valuators=((0, 989.0, 989.0), (1, 597.0, 597.0)),
                root_xy=(988.0, 596.0),
                event_xy=(988.0, 596.0),
            ),
        )
        self._keyboard_keymap = {"Return": 36}
        self._keyboard_operations = (
            KeyboardProbeOperation(
                "open_chat",
                "",
                (KeyboardProbeStep("press", "Return"), KeyboardProbeStep("release", "Return")),
            ),
        )
        for index, step in enumerate(self._keyboard_operations[0].steps):
            detail = self._keyboard_keymap[step.keysym]
            timestamp = 232007540 + index * 2
            event_class = EventClass.RAW_KEY_PRESS if step.phase == "press" else EventClass.RAW_KEY_RELEASE
            delivered_class = EventClass.KEY_PRESS if step.phase == "press" else EventClass.KEY_RELEASE
            self._records.extend(
                (
                    _event(
                        event_class,
                        master=3,
                        source=5,
                        timestamp=timestamp,
                        detail=detail,
                        child=None,
                    ),
                    _event(
                        delivered_class,
                        master=5,
                        source=5,
                        timestamp=timestamp + 1,
                        detail=detail,
                    ),
                )
            )
        self._human = [
            replace(
                _event(
                    EventClass.MOTION,
                    master=7,
                    source=7,
                    timestamp=232007505,
                    detail=0,
                ),
                root_xy=(989.0, 597.0),
                event_xy=(989.0, 597.0),
            ),
            _event(
                EventClass.RAW_KEY_PRESS,
                master=3,
                source=9,
                timestamp=232007518,
                detail=8,
                child=None,
            ),
            _event(
                EventClass.KEY_PRESS,
                master=9,
                source=9,
                timestamp=232007520,
                detail=8,
            ),
            _event(
                EventClass.RAW_KEY_RELEASE,
                master=3,
                source=9,
                timestamp=232007522,
                detail=8,
                child=None,
            ),
            _event(
                EventClass.KEY_RELEASE,
                master=9,
                source=9,
                timestamp=232007522,
                detail=8,
            ),
            _event(
                EventClass.RAW_BUTTON_PRESS,
                master=2,
                source=7,
                timestamp=232007523,
                detail=1,
                child=None,
            ),
            _event(
                EventClass.BUTTON_PRESS,
                master=7,
                source=7,
                timestamp=232007524,
                detail=1,
            ),
            _event(
                EventClass.RAW_BUTTON_RELEASE,
                master=2,
                source=7,
                timestamp=232007525,
                detail=1,
                child=None,
            ),
            _event(
                EventClass.BUTTON_RELEASE,
                master=7,
                source=7,
                timestamp=232007525,
                detail=1,
            ),
        ]
        self.calibration_due_count = 0
        self.calibration_rejected_count = 0
        self.calibration_due_deadline = 0.0
        self.calibration_epoch = 0
        self.calibration_ticket = None
        self.ready = None
        self.queue_seen_before_ready = False
        self.game_identity = WindowIdentity(
            0x700001,
            os.getpid(),
            "WowB.exe",
            "World of Warcraft",
            "wowb.exe,wowb.exe",
            0x700002,
            (0, 0, 1928, 1092),
            (0, 0, 1920, 1080),
            0x700002,
            "game-start",
            ":test",
            "owned-game-identity",
            root_xid=0x374,
            managed_frame_xid=0x700001,
            target_event_xids=frozenset({0x700001, 0x700002}),
        )

    def input_hierarchy(self):
        return (
            (2, 0, "master", "pointer:Virtual core pointer"),
            (3, 0, "master", "keyboard:Virtual core keyboard"),
            (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
            (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
            (7, 2, "slave", "pointer:xwayland-relative-pointer"),
            (9, 3, "slave", "keyboard:xwayland-keyboard"),
        )

    def xtest_input_profile(self, hierarchy, **_kwargs):
        return _xtest_profile(hierarchy)

    def begin_calibration_operation(self) -> int:
        self.calibration_epoch += 1
        self.queued_calibration_count = 0
        self.calibration_due_count = 0
        self.calibration_rejected_count = 0
        return self.calibration_epoch

    def resolve_identity(self, *, require_foreground: bool = False):
        del require_foreground
        return self.game_identity

    def notify_input_calibration_ready(self, readiness) -> None:
        self.ready = readiness

    def issue_calibration_ticket(self, window):
        assert window.contains_xid(TARGET)
        self.calibration_ticket = SimpleNamespace(epoch=self.calibration_epoch)
        return self.calibration_ticket

    def clear_input_calibration_ready(self) -> None:
        self.ready = None

    def calibration_keyboard_operations(self):
        return self._keyboard_operations

    def calibration_keyboard_keymap(self):
        return self._keyboard_keymap

    def stop_input_observer(self, observer, *, already_stopped: bool = False) -> None:
        del observer, already_stopped

    def start_input_observer(self, *, calibration: bool, callback):
        assert calibration
        self.observer = _WorkflowObserver(callback, self._records, self._human, outside=self.outside)
        return self.observer

    def focus_calibration_target(self, window: OwnedWindow, *, calibration_ticket) -> None:
        assert window.contains_xid(TARGET)
        assert calibration_ticket is not None

    def synthetic_target_keyboard_profile(
        self, window: OwnedWindow, *, operations, calibration_ticket
    ) -> SyntheticPointerProbe:
        assert window.contains_xid(TARGET)
        assert tuple(operations) == self._keyboard_operations
        assert calibration_ticket is not None
        return SyntheticPointerProbe(
            origin=(987, 595, TARGET),
            requested_endpoint=(989, 597),
            final_observed=(989, 597, TARGET),
            emitted_transitions=((988, 596), (989, 597)),
            seed=17,
            plan=((0.0, 987, 595), (0.1, 988, 596), (0.2, 989, 597)),
        )

    def queue_delayed_calibration_action(
        self,
        window: OwnedWindow,
        *,
        delay: float,
        execution_gate: threading.Event | None = None,
        calibration_ticket,
    ) -> None:
        assert delay > 0.0
        assert calibration_ticket is self.calibration_ticket
        self.queue_seen_before_ready = self.ready is None
        self.queued_calibration_count += 1
        self.calibration_due_deadline = delay
        return SimpleNamespace(epoch=self.calibration_epoch)

    def cancel_calibration_actions(self) -> None:
        self.queued_calibration_count = 0

    def revoke(self, reason: str) -> int:
        self.revocations.append(reason)
        self.queued_calibration_count = 0
        self.calibration_due_count = 1
        self.calibration_rejected_count = 1
        return len(self.revocations)

    def retain_input_evidence(self, evidence) -> None:
        evidence.verify_seal()


def test_complete_owned_workflow_preserves_exact_human_and_synthetic_pairs() -> None:
    driver = _WorkflowDriver(outside=False)
    evidence = InputCalibrationWorkflow(driver, display=":test", window_factory=_WorkflowTarget).run(
        human_timeout=1
    )
    assert evidence.target_xid == TARGET
    assert evidence.synthetic_key_source == 5
    assert evidence.synthetic_pointer_source == 4
    assert evidence.human_key_source == 9
    assert evidence.human_pointer_source == 7
    records = driver._records
    assert evidence.synthetic_events == tuple(
        (record.event_class.value, record.source_device, record.detail) for record in records
    )
    assert evidence.human_events == tuple(
        (event.event_class.value, event.source_device, event.detail)
        for event in driver._human
        if event.is_action_event
    )
    expected_classes = [event.event_class.value for event in driver._records]
    expected_classes.extend(event.event_class.value for event in driver._human)
    assert [proof[0] for proof in evidence.event_proof] == expected_classes
    assert [proof[1:4] for proof in evidence.event_proof[-len(driver._human) :]] == [
        (event.master_device, event.source_device, event.detail) for event in driver._human
    ]
    assert not driver.queued_calibration_count
    assert driver.queue_seen_before_ready


@pytest.mark.parametrize(
    "mutation",
    ("unpaired", "wrong-origin", "wrong-final", "outside-intermediate"),
)
def test_native_workflow_motion_sequence_rejects_transition_tampering(mutation: str) -> None:
    """The workflow's captured multi-segment XTEST path remains fully sealed."""

    workflow_driver = _WorkflowDriver(outside=False)
    target = _WorkflowTarget().window
    profile = workflow_driver.xtest_input_profile(workflow_driver.input_hierarchy())
    probe = SyntheticPointerProbe(
        origin=(987, 595, TARGET),
        requested_endpoint=(989, 597),
        final_observed=(989, 597, TARGET),
        emitted_transitions=((988, 596), (989, 597)),
        seed=17,
        plan=((0.0, 987, 595), (0.1, 988, 596), (0.2, 989, 597)),
    )
    records = list(workflow_driver._records)
    raw_motion = [
        index
        for index, event in enumerate(records)
        if event.event_class is EventClass.RAW_MOTION and event.source_device == profile.source_device
    ]
    primary_motion = [
        index
        for index, event in enumerate(records)
        if event.event_class is EventClass.MOTION and event.source_device == profile.source_device
    ]
    assert len(raw_motion) == len(primary_motion) == len(probe.emitted_transitions)
    if mutation == "unpaired":
        records.pop(primary_motion[0])
    elif mutation == "wrong-origin":
        records[primary_motion[0]] = replace(
            records[primary_motion[0]],
            root_xy=(900.0, 500.0),
            event_xy=(900.0, 500.0),
        )
    elif mutation == "wrong-final":
        records[raw_motion[-1]] = replace(
            records[raw_motion[-1]],
            valuators=((0, 990.0, 990.0), (1, 598.0, 598.0)),
        )
        records[primary_motion[-1]] = replace(
            records[primary_motion[-1]],
            valuators=((0, 990.0, 990.0), (1, 598.0, 598.0)),
        )
    else:
        outside_endpoint = (1300.0, 700.0)
        records[raw_motion[0]] = replace(
            records[raw_motion[0]],
            valuators=(
                (0, outside_endpoint[0], outside_endpoint[0]),
                (1, outside_endpoint[1], outside_endpoint[1]),
            ),
        )
        records[primary_motion[0]] = replace(
            records[primary_motion[0]],
            root_xy=outside_endpoint,
            event_xy=outside_endpoint,
            valuators=(
                (0, outside_endpoint[0], outside_endpoint[0]),
                (1, outside_endpoint[1], outside_endpoint[1]),
            ),
        )
        records[primary_motion[1]] = replace(
            records[primary_motion[1]],
            root_xy=outside_endpoint,
            event_xy=outside_endpoint,
        )
        probe = replace(
            probe,
            emitted_transitions=((1300, 700), (989, 597)),
            plan=((0.0, 987, 595), (0.1, 1300, 700), (0.2, 989, 597)),
        )
    with pytest.raises(CalibrationError):
        InputCalibrationWorkflow._canonical_motion_transitions(
            records,
            target,
            profile,
            synthetic_motion_probe=probe,
        )


def test_workflow_refuses_pointer_above_measured_client_after_guarded_focus() -> None:
    """A certified frame hit in its title area is refused before probe input."""

    driver = _WorkflowDriver(outside=False)
    order: list[str] = []
    pointer = (1809, 43, TARGET_FRAME)
    driver.focus_calibration_target = lambda *_args, **_kwargs: order.append("focus")
    driver.backend.pointer_position = lambda **_kwargs: order.append("pointer_query") or pointer
    synthetic_calls: list[str] = []

    def unexpected_synthetic(*_args, **_kwargs) -> None:
        synthetic_calls.append("called")

    driver.synthetic_target_keyboard_profile = unexpected_synthetic

    class OffscreenMeasuredTarget(_WorkflowTarget):
        def __init__(self) -> None:
            super().__init__()
            self.window = replace(
                self.window,
                geometry=(1440, 71, 480, 240),
                managed_frame_geometry=(1379, -23, 602, 401),
                certified_event_target_xids=frozenset({TARGET, TARGET_DESCENDANT, TARGET_FRAME}),
            )
            self.closed = False

        def close(self) -> None:
            self.closed = True

    target = OffscreenMeasuredTarget()
    workflow = InputCalibrationWorkflow(driver, display=":test", window_factory=lambda: target)
    with pytest.raises(CalibrationError, match="does not contain the current pointer"):
        workflow.run(human_timeout=1)

    assert order == ["focus", "pointer_query"]
    assert synthetic_calls == []
    assert target.closed
    assert workflow.failure_context is not None
    placement = workflow.failure_context["targetPlacement"]
    assert placement == {
        "producer": "InputCalibrationWorkflow.target_pointer_preflight",
        "targetXid": TARGET,
        "targetGeometry": [1440, 71, 480, 240],
        "origin": {"x": 1809, "y": 43, "windowXid": TARGET_FRAME},
        "predicates": {"certifiedHitWindow": True, "insideTargetGeometry": False},
    }
    assert workflow.failure_context["readinessPublished"] is False
    assert workflow.failure_context["stage"] == "target_pointer_preflight"


def test_first_human_event_outside_owned_target_revokes_and_suppresses_due_action() -> None:
    driver = _WorkflowDriver(outside=True)
    with pytest.raises(CalibrationError, match="outside owned GTK ancestry"):
        InputCalibrationWorkflow(driver, display=":test", window_factory=_WorkflowTarget).run(human_timeout=1)
    assert driver.revocations == ["first human input during calibration"]
    assert driver.queued_calibration_count == 0
    assert driver.calibration_executions == 0


def test_arm_replaces_stopped_calibration_monitor_with_runtime_observer(tmp_path: Path) -> None:
    hierarchy = _evidence(tmp_path).xinput_hierarchy
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._cancel_active = threading.Event()
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "monitor.lock")
    identity = WindowIdentity(
        1,
        os.getpid(),
        "WowB.exe",
        "World of Warcraft",
        "wowb.exe,wowb.exe",
        2,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        1,
        "start",
        ":test",
        "game-id",
        root_xid=0x374,
        managed_frame_xid=1,
        target_event_xids=frozenset({1, 2}),
    )
    backend.resolve_identity = lambda **_kwargs: identity
    driver.input_hierarchy = lambda **_kwargs: hierarchy  # type: ignore[method-assign]
    stopped: list[bool] = []
    runtime = SimpleNamespace(stop=lambda: stopped.append(True))
    calibration = SimpleNamespace(stop=lambda: stopped.append(True))
    driver._monitor = calibration
    driver._monitor_calibration = True
    started: list[bool] = []

    arm_evidence = _evidence(
        tmp_path,
        hierarchy=hierarchy,
        driver_version=driver.driver_version,
        game_identity_fingerprint="game-id",
        game_geometry=identity.child_geometry,
    )
    backend.xtest_input_profile = lambda _hierarchy, **_kwargs: arm_evidence.xtest_profile  # type: ignore[method-assign]

    def start(*, calibration: bool = False, callback=None):
        started.append(calibration)
        driver._monitor = runtime
        driver._monitor_calibration = calibration
        return runtime

    driver.start_input_observer = start  # type: ignore[method-assign]
    try:
        driver.arm(arm_evidence)
        assert stopped == [True]
        assert started == [False]
        assert driver._monitor is runtime
        assert not driver._monitor_calibration
    finally:
        driver.close()


def test_begin_action_generation_check_and_install_are_atomic(tmp_path: Path) -> None:
    guardian = InputGuardian(tmp_path / "atomic.lock", parent_pid=os.getppid())
    clock_entered = threading.Event()
    continue_clock = threading.Event()
    result: dict[str, object] = {}
    try:
        now = [0.0]

        def gated_clock() -> float:
            if not clock_entered.is_set():
                clock_entered.set()
                assert continue_clock.wait(2)
            return now[0]

        guardian._clock = gated_clock  # type: ignore[method-assign]
        generation = guardian.arm(_evidence(tmp_path))

        def begin() -> None:
            try:
                result["action"] = guardian.begin_action(
                    generation,
                    target_xid=TARGET,
                    source_device=5,
                    master_device=3,
                    event_classes={EventClass.KEY_PRESS.value},
                )
            except Exception as exc:  # implementation should fence this race
                result["error"] = exc

        thread = threading.Thread(target=begin)
        thread.start()
        assert clock_entered.wait(2)
        revoker = threading.Thread(target=lambda: guardian.revoke("concurrent human input"))
        revoker.start()
        continue_clock.set()
        revoker.join(2)
        thread.join(2)
        assert not thread.is_alive()
        assert not revoker.is_alive()
        assert guardian.action is None
        assert "error" in result or guardian.generation != generation
    finally:
        guardian.close()


def test_active_x11_process_group_cancellation_releases_tracked_keys_and_buttons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._active_processes = set()
    backend._active_process_groups = set()
    backend._process_generations = {}
    backend._process_calibration_tickets = {}
    backend._process_lock = threading.RLock()
    backend._active_generation = 1
    backend._canceled_generations = set()
    backend._admission = lambda _generation, callback: callback()
    backend._register_process_group = lambda _generation, _pid: None
    backend._unregister_process_group = lambda _pid: None
    backend._publish_holds = None

    class CancellationFlag:
        def __init__(self) -> None:
            self.event = threading.Event()

        def set(self) -> None:
            self.event.set()

        def is_set(self) -> bool:
            return self.event.is_set()

        def clear(self) -> None:
            # Keep the cancellation visible until the blocked caller has
            # observed it; the real implementation must provide that fence.
            return None

    backend._cancel_active = CancellationFlag()
    backend._held_keys = {"F13"}
    backend._held_buttons = set()
    terminated: list[int] = []
    releases: list[list[str]] = []

    class BlockingProcess:
        pid = 4242

        def __init__(self, gate_reader: int) -> None:
            self.returncode: int | None = None
            self.ready = threading.Event()
            self.gate_reader = gate_reader

        def communicate(self, *, timeout: float | None = None):
            if timeout is None:
                return "", ""
            assert timeout <= 0.2
            self.ready.wait(timeout)
            raise subprocess.TimeoutExpired("xdotool", timeout)

        def poll(self):
            return self.returncode

        def wait(self, timeout: float | None = None):
            self.returncode = -15
            os.close(self.gate_reader)
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process: BlockingProcess | None = None

    def fake_popen(args, **kwargs):
        del kwargs
        nonlocal process
        wrapper_index = args.index(_EXEC_ADMISSION_WRAPPER)
        assert args[wrapper_index - 1] == "-c"
        gate_fd_index = wrapper_index + 1
        process = BlockingProcess(os.dup(int(args[gate_fd_index])))
        return process

    monkeypatch.setattr("crowley.session.driver.subprocess.Popen", fake_popen)
    backend._terminate_process = lambda value: (terminated.append(value.pid), value.wait())
    backend._release_command = lambda args: releases.append(args)
    result: dict[str, object] = {}

    def run() -> None:
        try:
            backend._run(
                ["xdotool", "keydown", "F13"],
                timeout=5,
                generation=1,
                held_keys={"F13"},
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: bool(backend._active_processes))
    backend.release_held_input()
    thread.join(2)
    assert not thread.is_alive()
    assert terminated and set(terminated) == {4242}
    assert ["xdotool", "keyup", "F13"] in releases
    assert ["xdotool", "mouseup", "1"] not in releases
    assert isinstance(result.get("error"), DriverError)


def test_xi2_monitor_has_one_stdout_reader() -> None:
    class Process:
        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.communicate_calls = 0

        def terminate(self) -> None:
            return None

        def communicate(self, *args, **kwargs):
            self.communicate_calls += 1
            return "", ""

    process = Process()
    monitor = InputMonitor(":test", lambda _event: None, xauthority="Xauthority")
    monitor.observer.start = lambda: setattr(monitor.observer, "process", process)  # type: ignore[method-assign]
    monitor.start()
    monitor.stop()
    assert process.communicate_calls == 0


def test_xi2_reader_delivers_final_release_from_open_idle_pipe() -> None:
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", buffering=1)
    seen: list[XI2Event] = []

    class Process:
        def __init__(self) -> None:
            self.stdout = stdout
            self.poll_result: int | None = None

        def poll(self) -> int | None:
            return self.poll_result

        def terminate(self) -> None:
            self.poll_result = 0

        def wait(self, timeout: float | None = None) -> int:
            self.poll_result = 0
            return 0

        def kill(self) -> None:
            self.poll_result = -9

    process = Process()
    monitor = InputMonitor(":test", lambda event: seen.append(event) if event is not None else None)
    monitor.observer.start = lambda: setattr(monitor.observer, "process", process)  # type: ignore[method-assign]
    release_events = [
        _event(EventClass.KEY_RELEASE, master=5, source=5, timestamp=232008000, detail=38),
        _event(EventClass.BUTTON_RELEASE, master=4, source=4, timestamp=232008001, detail=1),
    ]
    try:
        monitor.start()
        os.write(write_fd, (_render_events(release_events) + "\n").encode("utf-8"))
        monitor.wait_for_event(lambda events: len(events) == 2, timeout=2)
        assert [event.event_class for event in seen] == [
            EventClass.KEY_RELEASE,
            EventClass.BUTTON_RELEASE,
        ]
        assert process.poll() is None
    finally:
        os.close(write_fd)
        monitor.stop()
        stdout.close()


def test_real_input_monitor_emits_idle_raw_motion_and_guardian_revokes_without_next_event(
    tmp_path: Path,
) -> None:
    """A blank-delimited raw record reaches the real reader before pipe EOF."""

    # Source-derived formatter expectation; this is not a retained live
    # capture and includes the verified XTEST RawMotion detail contract.
    raw_motion = (
        "EVENT type 17 (RawMotion)\n"
        "    device: 2 (4)\n"
        "    time: 232008100\n"
        "    detail: 0\n"
        "    flags: \n"
        "    valuators:\n"
        "         0: 873.00 (873.00)\n"
        "         1: 576.00 (576.00)\n"
        "\n"
    )

    class Evidence:
        is_live = True
        fingerprint = "idle-raw-motion"

    guardian = InputGuardian(tmp_path / "idle-raw-motion.lock", parent_pid=os.getppid())
    guardian.arm(Evidence())
    observed: list[bool | None] = []
    monitor = InputMonitor(
        ":test", lambda event: observed.append(None if event is None else guardian.observe(event))
    )

    def start_observer() -> None:
        monitor.observer.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                f"import sys,time;sys.stdout.write({raw_motion!r});sys.stdout.flush();time.sleep(5)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    monitor.observer.start = start_observer  # type: ignore[method-assign]
    try:
        monitor.start()
        monitor.wait_for_event(lambda events: len(events) == 1, timeout=2)
        assert _wait_for(lambda: not guardian.armed)
        assert observed == [False]
        assert monitor.observer.process is not None
        assert monitor.observer.process.poll() is None
    finally:
        monitor.stop()
        guardian.close()


def test_real_input_monitor_reports_malformed_raw_block_at_idle_delimiter(
    tmp_path: Path,
) -> None:
    raw_motion = (
        "EVENT type 17 (RawMotion)\n"
        "    device: 2 (4)\n"
        "    time: 232008101\n"
        "    detail: 1\n"
        "    flags: \n"
        "    valuators:\n"
        "         0: 873.00 (873.00)\n"
        "         1: 576.00 (576.00)\n"
        "\n"
    )

    class Evidence:
        is_live = True
        fingerprint = "idle-malformed-raw-motion"

    guardian = InputGuardian(tmp_path / "idle-malformed-raw-motion.lock", parent_pid=os.getppid())
    guardian.arm(Evidence())
    observed: list[bool | None] = []

    def observe(event: XI2Event | None) -> None:
        if event is None:
            guardian.revoke("malformed raw observer block")
            observed.append(None)
        else:
            observed.append(guardian.observe(event))

    monitor = InputMonitor(":test", observe)

    def start_observer() -> None:
        monitor.observer.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                f"import sys,time;sys.stdout.write({raw_motion!r});sys.stdout.flush();time.sleep(5)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    monitor.observer.start = start_observer  # type: ignore[method-assign]
    try:
        monitor.start()
        assert _wait_for(lambda: observed == [None])
        assert not guardian.armed
        diagnostic = monitor.failure_diagnostic
        assert diagnostic is not None
        assert diagnostic["reason"] == "RawMotion: detail must be 0"
        assert diagnostic["text"] == raw_motion
    finally:
        monitor.stop()
        guardian.close()


def test_xi2_observer_clean_eof_revokes_runtime_callback() -> None:
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", buffering=1)
    callbacks: list[XI2Event | None] = []

    class Process:
        def __init__(self) -> None:
            self.stdout = stdout
            self.poll_result: int | None = None

        def poll(self) -> int | None:
            return self.poll_result

        def terminate(self) -> None:
            self.poll_result = 0
            self.stdout.close()

        def wait(self, timeout: float | None = None) -> int:
            self.poll_result = 0
            return 0

        def kill(self) -> None:
            self.poll_result = -9

    process = Process()
    monitor = InputMonitor(":test", callbacks.append)
    monitor.observer.start = lambda: setattr(monitor.observer, "process", process)  # type: ignore[method-assign]
    try:
        monitor.start()
        os.close(write_fd)
        assert _wait_for(lambda: callbacks == [None])
    finally:
        monitor.stop()
        stdout.close()


def test_real_input_monitor_notifies_after_guardian_callback_under_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real reader/subprocess must wake completion after callback state changes."""

    events = [
        _event(EventClass.RAW_KEY_PRESS, master=3, source=5, timestamp=1, detail=8, child=None),
        _event(EventClass.KEY_PRESS, master=5, source=5, timestamp=2, detail=8),
        _event(EventClass.RAW_KEY_RELEASE, master=3, source=5, timestamp=3, detail=8, child=None),
        _event(EventClass.KEY_RELEASE, master=5, source=5, timestamp=4, detail=8),
    ]
    prefix = _render_events(events[:3])
    final = _render_events(events[3:])
    guardian = InputGuardian(tmp_path / "monitor-race.lock", parent_pid=os.getppid())
    generation = guardian.arm(SimpleNamespace(is_live=True, fingerprint="monitor-race"))
    action = guardian.begin_action(
        generation,
        target_xid=TARGET,
        source_device=5,
        master_device=5,
        event_classes={event.event_class.value for event in events},
        required_sequence=(
            EventClass.RAW_KEY_PRESS.value,
            EventClass.KEY_PRESS.value,
            EventClass.RAW_KEY_RELEASE.value,
            EventClass.KEY_RELEASE.value,
        ),
        device_by_class={
            EventClass.RAW_KEY_PRESS.value: 3,
            EventClass.KEY_PRESS.value: 5,
            EventClass.RAW_KEY_RELEASE.value: 3,
            EventClass.KEY_RELEASE.value: 5,
        },
        expected_details=(8, 8, 8, 8),
        expected_modifiers=(0, 0, 0),
        timeout=3.0,
    )
    monitor = InputMonitor(":test", guardian.observe)

    def start_observer() -> None:
        monitor.observer.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                (
                    f"import sys;sys.stdout.write({prefix!r});sys.stdout.flush();"
                    f"sys.stdin.readline();sys.stdout.write({final!r});sys.stdout.flush();"
                    "sys.stdin.readline()"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr(monitor.observer, "start", start_observer)
    lock_entered = threading.Event()
    release_lock = threading.Event()

    def contend_guardian_lock() -> None:
        with guardian._lock:
            lock_entered.set()
            assert release_lock.wait(3)

    waiter_error: list[BaseException] = []
    waiter_done = threading.Event()

    def wait_for_completion() -> None:
        try:
            monitor.wait_for_event(lambda _events: action.complete, timeout=2.0)
        except BaseException as exc:
            waiter_error.append(exc)
        finally:
            waiter_done.set()

    monitor.start()
    contender: threading.Thread | None = None
    waiter: threading.Thread | None = None
    completed_action: subprocess.Popen[str] | None = None
    try:
        assert _wait_for(lambda: len(monitor.events()) == 3)
        contender = threading.Thread(target=contend_guardian_lock, daemon=True)
        contender.start()
        assert lock_entered.wait(2)
        waiter = threading.Thread(target=wait_for_completion, daemon=True)
        waiter.start()
        process = monitor.observer.process
        assert process is not None and process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        assert _wait_for(lambda: len(monitor.events()) == len(events))
        # Complete a separate harmless action subprocess while the final
        # observer callback is blocked on Guardian._lock.  The action has
        # exited before the callback is released and the completed action is
        # finally retired below.
        completed_action = subprocess.Popen([sys.executable, "-I", "-S", "-c", "import sys;sys.exit(0)"])
        assert completed_action.wait(timeout=2) == 0
        release_lock.set()
        assert waiter_done.wait(2)
        assert not waiter_error
        assert action.complete
        # Stop the real reader subprocess before releasing the completed
        # action.  InputMonitor marks an unsolicited EOF as observer death;
        # the normal stop path closes that process without revoking the
        # already-complete action.
        monitor.stop()
        assert process.poll() is not None
    finally:
        release_lock.set()
        if waiter is not None:
            waiter.join(timeout=2)
        if contender is not None:
            contender.join(timeout=2)
        monitor.stop()
        if action.complete:
            guardian.finish_action(generation, action)
        else:
            guardian.revoke("monitor regression cleanup")
        guardian.close()


def test_reload_stream_correlates_each_character_without_guardian_revoke(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", buffering=1)
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._active_processes = set()
    backend._process_lock = threading.RLock()
    backend._cancel_active = threading.Event()
    backend._held_keys = set()
    backend._held_buttons = set()
    backend._measured_anchors = {}
    identity = WindowIdentity(
        0x700100,
        os.getpid(),
        "WowB.exe",
        "World of Warcraft",
        "wowb.exe,wowb.exe",
        TARGET,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        TARGET,
        "start",
        ":test",
        "game-fingerprint",
        root_xid=0x374,
        managed_frame_xid=0x700100,
        target_event_xids=frozenset({0x700100, TARGET}),
    )
    backend._identity_for_test = identity

    class Process:
        def __init__(self) -> None:
            self.stdout = stdout
            self.poll_result: int | None = None

        def poll(self) -> int | None:
            return self.poll_result

        def terminate(self) -> None:
            self.poll_result = 0

        def wait(self, timeout: float | None = None) -> int:
            self.poll_result = 0
            return 0

        def kill(self) -> None:
            self.poll_result = -9

    process = Process()
    monitor = InputMonitor(
        ":test", lambda event: driver._external_input(event), xauthority=profile.xauthority
    )
    monitor.observer.start = lambda: setattr(monitor.observer, "process", process)  # type: ignore[method-assign]
    details = [61, 27, 25, 46, 43, 38, 40]
    commands: list[list[str]] = []
    emitted = 0

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal emitted
        commands.append(args)
        if args == ["xmodmap", "-pke"]:
            names = ["slash", "r", "e", "l", "o", "a", "d", "Return"]
            key_details = [*details, 36]
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="\n".join(
                    f"keycode {detail} = {name}" for detail, name in zip(key_details, names, strict=True)
                ),
                stderr="",
            )
        index = emitted
        expected_command = [
            "xdotool",
            "keydown",
            f"{details[index]:03d}",
            "keyup",
            f"{details[index]:03d}",
        ]
        if args != expected_command:
            raise AssertionError(args)
        emitted += 1
        detail = details[index]
        timestamp = 232009000 + index * 10
        records = [
            _event(
                EventClass.RAW_KEY_PRESS, master=3, source=5, timestamp=timestamp, detail=detail, child=None
            ),
            replace(
                _event(EventClass.KEY_PRESS, master=5, source=5, timestamp=timestamp, detail=detail),
                modifiers=(0, 0, 0),
            ),
            _event(
                EventClass.RAW_KEY_RELEASE,
                master=3,
                source=5,
                timestamp=timestamp + 1,
                detail=detail,
                child=None,
            ),
            replace(
                _event(EventClass.KEY_RELEASE, master=5, source=5, timestamp=timestamp + 1, detail=detail),
                modifiers=(0, 0, 0),
            ),
        ]
        suffix = "\n" if emitted == len(details) else ""
        os.write(write_fd, (_render_events(records) + suffix).encode("utf-8"))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    backend._run = fake_run
    backend.action = lambda _name, **kwargs: backend._type(
        kwargs["command"], keymap=kwargs["_keyboard_keymap"]
    )
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "reload.lock")
    driver._identity = lambda **_kwargs: backend._identity_for_test  # type: ignore[method-assign]
    evidence = _evidence(
        tmp_path,
        xauthority=profile.xauthority,
        driver_version=driver.driver_version,
    )
    driver.input_calibration(evidence)
    generation = driver.guardian.arm(evidence)
    driver._monitor = monitor
    driver._monitor_calibration = False
    try:
        monitor.start()
        driver.run(
            "type_allowlisted_command",
            generation,
            command="/reload",
            precondition=lambda _identity: True,
        )
        assert [command for command in commands if command and command[0] == "xdotool"] == [
            ["xdotool", "keydown", f"{detail:03d}", "keyup", f"{detail:03d}"] for detail in details
        ]
        assert emitted == len(details)
        assert driver.guardian.revoked_reason == ""
        assert driver.guardian.armed
        assert driver.guardian.action is None
    finally:
        os.close(write_fd)
        driver.stop_input_observer(monitor)
        stdout.close()
        driver.close()


def test_input_evidence_survives_revoke_for_explicit_resume_but_stays_unarmed(tmp_path: Path) -> None:
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    driver = DesktopDriver(
        profile=profile,
        backend=RecordingBackend(),
        lease_path=tmp_path / "retained.lock",
    )
    evidence = _evidence(tmp_path)
    try:
        driver.retain_input_evidence(evidence)
        generation = driver.guardian.arm(evidence)
        driver.revoke("first human input")
        assert driver.retained_input_evidence is evidence
        assert not driver.guardian.armed
        assert driver.guardian.generation == generation + 1
    finally:
        driver.close()


def test_owner_watchdog_does_not_use_unsupported_keyup_all(tmp_path: Path) -> None:
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "watchdog.lock")
    try:
        assert "--all" not in driver.guardian._watchdog_command
    finally:
        driver.close()


def _status_image(frame: bytes) -> Image.Image:
    image = Image.new("RGB", (256, 256), (0, 0, 0))
    for row, cells in enumerate(pack_cells(frame)):
        for column, bit in enumerate(cells):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    image.putpixel((column * 2 + dx, row * 2 + dy), color)
    return image


def test_native_calibration_proves_all_four_physical_pixels_per_cell(tmp_path: Path) -> None:
    from .test_session_runtime import _snapshot

    frame = encode_frame(_snapshot(1000, sequence=1))
    image = _status_image(frame)
    bits = pack_cells(frame)
    one_cell = next(
        (row, column) for row, cells in enumerate(bits) for column, bit in enumerate(cells) if bit
    )
    row, column = one_cell
    image.putpixel((column * 2, row * 2), (0, 0, 0))
    path = tmp_path / "crop.png"
    image.save(path)
    with pytest.raises(CalibrationError, match="physical|cell"):
        NativeCalibrationRunner._validate_cells(path)


def test_pixel_decoder_rejects_crc_change_for_same_boot_sequence() -> None:
    from .test_session_runtime import _snapshot

    first = _status_image(encode_frame(_snapshot(1000, sequence=1)))
    second = _status_image(encode_frame(_snapshot(1001, sequence=1)))
    decoder = PixelDecoder()
    decoder.decode(first, monotonic=1.0, window_identity="identity")
    with pytest.raises(ProtocolError, match="CRC|CRC|duplicate"):
        decoder.decode(second, monotonic=1.1, window_identity="identity")


def _native_driver(tmp_path: Path) -> tuple[DesktopDriver, WindowIdentity, subprocess.Popen[str]]:
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"), wine_prefix="prefix")
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    child = subprocess.Popen(["/bin/sleep", "120"])
    stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
    process_start = stat[stat.rfind(")") + 2 :].split()[19]
    process_executable = os.path.realpath(os.readlink(f"/proc/{child.pid}/exe"))
    identity = WindowIdentity(
        1,
        child.pid,
        process_executable,
        "World of Warcraft",
        "wowb.exe,wowb.exe",
        2,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        1,
        process_start,
        ":test",
        "a" * 64,
        root_xid=0x374,
        managed_frame_xid=1,
        target_event_xids=frozenset({1, 2}),
    )
    backend.resolve_identity = lambda **_kwargs: identity
    driver = DesktopDriver(
        profile=profile, backend=backend, lease_path=private_lease_path(tmp_path, "native.lock")
    )
    native_clock = [time.monotonic()]
    driver._native_test_clock = native_clock  # type: ignore[attr-defined]
    driver.input_hierarchy = lambda **_kwargs: _evidence(tmp_path).xinput_hierarchy  # type: ignore[method-assign]
    driver.new_native_decoder = lambda: object()  # type: ignore[method-assign]
    driver.native_memory_meter = LinuxProcStatusVmHwmReset5(
        child.pid,
        process_start=process_start,
        process_executable=process_executable,
        identity_fingerprint=identity.fingerprint,
        monotonic=lambda: native_clock[0],
    )

    preconstruction_path = tmp_path / "preconstruction.png"
    preconstruction_path.write_bytes(b"absence-capture")
    preconstruction_sha256 = hashlib.sha256(preconstruction_path.read_bytes()).hexdigest()

    def preconstruction(observed, *, baseline_reader, baseline_ready):
        baseline = baseline_reader(observed.pid)
        baseline_ready(observed.pid, baseline)
        return NativePreconstructionEvidence._issue(
            identity_fingerprint=observed.fingerprint,
            process_start=observed.process_start,
            absent=True,
            baseline_id=preconstruction_sha256,
            observed_monotonic=driver._native_test_clock[0],  # type: ignore[attr-defined]
            bootstrap_release_monotonic=driver._native_test_clock[0],  # type: ignore[attr-defined]
            display=observed.display,
            process_executable=observed.executable,
            window_xid=observed.xid,
            child_xid=observed.child_xid,
            geometry=observed.child_geometry,
            xauthority=profile.xauthority,
            wine_prefix=profile.wine_prefix,
            baseline_hwm_bytes=baseline,
            capture_path=str(preconstruction_path),
            capture_sha256=preconstruction_sha256,
        )

    driver.native_preconstruction_provider = preconstruction
    driver.notify_native_baseline_ready = lambda _identity, _value: None  # type: ignore[method-assign]
    driver.notify_native_memory_reset_ready = (  # type: ignore[method-assign]
        lambda _identity, record: NativeMemoryArchiveReceipt.for_record(
            record, archive_id=f"native-regression-{record.phase}-{record.ordinal}"
        )
    )
    driver.notify_native_hold_required = lambda _identity: None  # type: ignore[method-assign]
    driver.notify_native_memory_ready = lambda _identity, _pending: None  # type: ignore[method-assign]
    driver.notify_native_resume_required = lambda _identity, _boundary: None  # type: ignore[method-assign]
    driver.notify_native_resume_ready = lambda _identity, _evidence, _resumed: None  # type: ignore[method-assign]
    driver.notify_native_measurement_failed = (  # type: ignore[method-assign]
        lambda _identity, reason: NativeRestorationReceipt(
            "restoration_required", False, reason, durable=True
        )
    )
    driver.release_native_capture = lambda path: path.unlink(missing_ok=True)  # type: ignore[method-assign]
    return driver, identity, child


def _metrics(update_count: int, phase: str = "held", ordinal: int = 100) -> dict[str, object]:
    return {
        "constructionSeconds": 0.1,
        "updateCount": update_count,
        "updateP99Ms": 10,
        "textureCount": 16_384,
        "effectiveScale": 1,
        "cellSize": (2, 2),
        "luaHeap": {
            "method": "collectgarbage_count",
            "available": True,
            "beforeKiB": 1,
            "afterKiB": 2,
            "netKiB": 1,
        },
        "nativePhase": phase,
        "nativeOrdinal": ordinal,
    }


def _native_observation(
    tmp_path: Path,
    identity: WindowIdentity,
    phase: str,
    ordinal: int,
    *,
    sequence: int,
    capture_monotonic: float,
):
    path = tmp_path / f"{phase}-{ordinal}.png"
    Image.new("RGB", (256, 256), (0, 0, 0)).save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if phase == "construct":
        update_count = 1
        telemetry_phase = "normal"
    elif phase == "held" and ordinal == 0:
        update_count = 3
        telemetry_phase = "held"
    elif phase == "updates":
        update_count = 3 + ordinal
        telemetry_phase = "updates"
    elif phase == "held" and ordinal == 100:
        update_count = 103
        telemetry_phase = "held"
    elif phase == "resume":
        update_count = 104
        telemetry_phase = "normal"
    else:
        raise AssertionError((phase, ordinal))
    return NativeRenderObservation(
        _metrics(
            update_count,
            telemetry_phase,
            ordinal,
        ),
        identity.fingerprint,
        boot="boot-a",
        sequence=sequence,
        crc32=sequence,
        capture_id=digest,
        capture_path=str(path),
        capture_sha256=digest,
        capture_monotonic=capture_monotonic,
        native_phase=telemetry_phase,
        native_ordinal=ordinal,
    )


def _construction_daemon(
    tmp_path: Path, *, state: ControllerState = ControllerState.NATIVE_CALIBRATION
) -> tuple[SessionDaemon, DesktopDriver, WindowIdentity, subprocess.Popen[str]]:
    driver, identity, child = _native_driver(tmp_path)
    meter = driver.native_memory_meter
    if meter is None:
        raise AssertionError("construction daemon fixture has no pre-bound native memory meter")
    meter._monotonic = driver._clock
    journal_path = tmp_path / "construction-journal.json"
    value = copy.deepcopy(DEFAULT_STATE)
    value.update(
        {
            "state": state.value,
            "generation": 7,
            "current": {"frameBoot": "boot-a", "frameSequence": 1, "frameCrc32": 1},
        }
    )
    journal = Journal(journal_path)
    journal.write(value)
    config = replace(
        fixture_session_config(
            tmp_path,
            profile=driver.profile,
            journal=journal_path,
            socket=tmp_path / "construction.sock",
            codex_path=fixture_codex_path(tmp_path),
        ),
        plan_hash="plan",
        plan_json="{}",
    )
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    return SessionDaemon(config, controller=controller, driver=driver), driver, identity, child


def _construction_frame(identity: WindowIdentity, *, boot: str, sequence: int) -> dict[str, object]:
    return {
        "surface": "present",
        "boot": boot,
        "sequence": sequence,
        "crc32": sequence,
        "snapshot": {
            "render": {"nativePhase": "normal", "nativeOrdinal": 0},
            "scanner": {"scheduleFence": {"state": "inactive"}},
        },
        "identityFingerprint": identity.fingerprint,
        "captureIdentity": identity.fingerprint,
        "captureMonotonic": 1.0,
    }


def test_native_absence_classifier_rejects_encoded_but_malformed_surface(tmp_path: Path) -> None:
    from .test_session_runtime import _snapshot

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    ordinary = tmp_path / "ordinary.png"
    encoded = tmp_path / "encoded.png"
    damaged = tmp_path / "damaged.png"
    Image.new("RGB", (256, 256), (127, 127, 127)).save(ordinary)
    grid = Image.new("RGB", (256, 256), (0, 0, 0))
    for row in range(128):
        for column in range(128):
            if (row + column) % 2:
                for dy in (0, 1):
                    for dx in (0, 1):
                        grid.putpixel((column * 2 + dx, row * 2 + dy), (255, 255, 255))
    grid.save(encoded)
    # A real encoded payload with one physical pixel damaged remains
    # encoded-looking and must not be relabeled as ordinary absence.
    damaged_image = _status_image(encode_frame(_snapshot(1000, sequence=1)))
    damaged_image.putpixel((20, 20), (127, 127, 127))
    damaged_image.save(damaged)
    try:
        assert daemon._strict_native_absence_capture(ordinary) is True
        assert daemon._strict_native_absence_capture(encoded) is False
        assert daemon._strict_native_absence_capture(damaged) is False
        expected_size = (driver.profile.canvas_crop[2], driver.profile.canvas_crop[3])
        assert daemon_module._strict_native_absence_image(ordinary, expected_size=expected_size) is True
        assert daemon_module._strict_native_absence_image(encoded, expected_size=expected_size) is False
        assert daemon_module._strict_native_absence_image(damaged, expected_size=expected_size) is False
        assert daemon._strict_native_absence_capture(ordinary) == daemon_module._strict_native_absence_image(
            ordinary, expected_size=expected_size
        )
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_absence_classifier_rejects_payload_occlusion_over_ten_percent(
    tmp_path: Path,
) -> None:
    from .test_session_runtime import _snapshot

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        image = _status_image(encode_frame(_snapshot(1000, sequence=1)))
        # Occlude a 100x100 physical-pixel payload region (2,500 of 16,384
        # logical cells) while preserving the CRS1 header.  The strict
        # classifier must retain the encoded-surface verdict without relying
        # on a damaged-cell percentage heuristic.
        for y in range(64, 164):
            for x in range(64, 164):
                image.putpixel((x, y), (127, 127, 127))
        path = tmp_path / "payload-occluded.png"
        image.save(path)
        assert daemon._strict_native_absence_capture(path) is False
        malformed_mode = tmp_path / "malformed-mode.png"
        image.convert("P").save(malformed_mode)
        assert daemon._strict_native_absence_capture(malformed_mode) is False
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_absence_classifier_rejects_combined_header_payload_and_loading_surfaces(
    tmp_path: Path,
) -> None:
    from .test_session_runtime import _snapshot

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        encoded = _status_image(encode_frame(_snapshot(1000, sequence=1)))
        # Erase the first protocol byte and a large payload block together;
        # the surviving CRS1 magic bytes still identify this as a damaged
        # encoded surface without a global intact-cell threshold.
        for y in range(0, 2):
            for x in range(0, 16):
                encoded.putpixel((x, y), (99, 121, 143))
        for y in range(96, 256):
            for x in range(96, 256):
                encoded.putpixel((x, y), (99, 121, 143))
        combined = tmp_path / "combined-header-payload.png"
        encoded.save(combined)
        assert daemon._strict_native_absence_capture(combined) is False

        # Erase all four CRS1 magic bytes as well as the payload.  The
        # surviving fixed version/encoding/geometry bytes still identify the
        # protocol canvas; a damaged encoded fragment must never establish
        # ordinary absence merely because its magic was occluded.
        entire_magic = _status_image(encode_frame(_snapshot(1000, sequence=1)))
        for y in range(0, 2):
            for x in range(0, 64):
                entire_magic.putpixel((x, y), (99, 121, 143))
        for y in range(96, 256):
            for x in range(96, 256):
                entire_magic.putpixel((x, y), (99, 121, 143))
        entire_magic_path = tmp_path / "entire-magic-header-payload.png"
        entire_magic.save(entire_magic_path)
        assert daemon._strict_native_absence_capture(entire_magic_path) is False

        # The generated regression must also survive erasing a whole physical
        # row and the complete 26-byte protocol header; neither case relies on
        # the retained private parent evidence.
        whole_row = _status_image(encode_frame(_snapshot(1000, sequence=1)))
        for y in range(0, 2):
            for x in range(0, 256):
                whole_row.putpixel((x, y), (99, 121, 143))
        for y in range(96, 256):
            for x in range(96, 256):
                whole_row.putpixel((x, y), (99, 121, 143))
        whole_row_path = tmp_path / "whole-row-header-payload.png"
        whole_row.save(whole_row_path)
        assert daemon._strict_native_absence_capture(whole_row_path) is False

        full_header = _status_image(encode_frame(_snapshot(1000, sequence=1)))
        for y in range(0, 4):
            end_x = 256 if y < 2 else (HEADER_SIZE * 8 - 128) * 2
            for x in range(0, end_x):
                full_header.putpixel((x, y), (99, 121, 143))
        for y in range(96, 256):
            for x in range(96, 256):
                full_header.putpixel((x, y), (99, 121, 143))
        full_header_path = tmp_path / "full-header-payload.png"
        full_header.save(full_header_path)
        assert daemon._strict_native_absence_capture(full_header_path) is False

        black = tmp_path / "black-loading.png"
        white = tmp_path / "white-loading.png"
        Image.new("RGB", (256, 256), (0, 0, 0)).save(black)
        Image.new("RGB", (256, 256), (255, 255, 255)).save(white)
        assert daemon._strict_native_absence_capture(black) is False
        assert daemon._strict_native_absence_capture(white) is False

        ordinary = Image.new("RGB", (256, 256))
        for y in range(256):
            for x in range(256):
                ordinary.putpixel((x, y), ((x * 3) % 251, (y * 5) % 241, (x + y) % 239))
        ordinary_path = tmp_path / "ordinary-colorful.png"
        ordinary.save(ordinary_path)
        assert daemon._strict_native_absence_capture(ordinary_path) is True
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_absence_fragments_reject_retained_negatives_and_accept_game_regions() -> None:
    """Check the fragment rule against retained decoder evidence and crops."""

    evidence_dir = NATIVE_ABSENCE_EVIDENCE
    assert evidence_dir.is_dir()
    negative_names = (
        "black-loading.png",
        "encoded-header-and-payload-occlusion.png",
        "encoded-entire-magic-and-payload-occlusion.png",
        "encoded-whole-first-row-and-payload-occlusion.png",
    )
    for name in negative_names:
        path = evidence_dir / name
        assert path.is_file()
        assert daemon_module._strict_native_absence_image(path, expected_size=(256, 256)) is False
        decoder = PixelDecoder(crop=(80, 48, 256, 256))
        with Image.open(path) as image:
            image.load()
            with pytest.raises(ProtocolError):
                decoder.decode(image, monotonic=1.0, window_identity=f"retained-{name}")

    for index in range(1, 5):
        path = evidence_dir / f"ordinary-game-region-{index}.png"
        assert path.is_file()
        assert daemon_module._strict_native_absence_image(path, expected_size=(256, 256)) is True


def test_native_capture_decoder_rejects_damaged_encoded_png_as_absence(
    tmp_path: Path,
) -> None:
    from .test_session_runtime import _snapshot

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    damaged = tmp_path / "damaged-decoder.png"
    image = _status_image(encode_frame(_snapshot(1000, sequence=1)))
    image.putpixel((20, 20), (127, 127, 127))
    image.save(damaged)
    daemon.driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
    daemon._capture_native_region_for_identity = lambda *_args, **_kwargs: damaged  # type: ignore[method-assign]
    try:
        with pytest.raises(DriverError, match="malformed encoded surface"):
            daemon._native_construction_capture(
                deadline=daemon._native_clock() + 5.0,
                expected_generation=7,
                expected_state=ControllerState.NATIVE_CALIBRATION,
            )
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_post_reset_absence_keeps_capture_start_time_after_processing(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    ordinary = tmp_path / "post-reset-ordinary.png"
    Image.new("RGB", (256, 256), (127, 127, 127)).save(ordinary)
    clock = [100.0]
    daemon._native_clock = lambda: clock[0]  # type: ignore[method-assign]
    daemon.driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]

    def delayed_capture(*_args: object, **_kwargs: object) -> Path:
        clock[0] = 106.0
        return ordinary

    daemon._capture_native_region_for_identity = delayed_capture  # type: ignore[method-assign]
    try:
        retained = daemon._retain_native_absence_capture(identity)
        assert retained["capture"] == 100.0
    finally:
        daemon._cleanup_native_owned_captures()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize(
    ("begin_surface", "begin_boot", "fence_state", "expected_kind"),
    [
        ("absent", "", "inactive", "restore"),
        ("present", "boot-a", "armed", "restore"),
        ("present", "native-construction:boot-a", "inactive", "restore"),
        ("present", "boot-a", "inactive", "ordinary_reload"),
        ("present", "ordinary-later", "inactive", "ordinary_reload"),
    ],
)
def test_native_recovery_begin_classifies_all_reload_variants_via_ipc(
    tmp_path: Path,
    begin_surface: str,
    begin_boot: str,
    fence_state: str,
    expected_kind: str,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        if begin_surface == "absent":
            begin = {
                "surface": "absent",
                "identityFingerprint": identity.fingerprint,
                "captureIdentity": identity.fingerprint,
                "captureMonotonic": 1.0,
            }
        else:
            begin = _construction_frame(identity, boot=begin_boot, sequence=2)
            begin["snapshot"]["scanner"]["scheduleFence"]["state"] = fence_state  # type: ignore[index]
        daemon._native_construction_observation_provider = lambda: begin
        ipc = SessionIPC(daemon.controller, owner=daemon)
        reply = ipc.dispatch(
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
        )
        assert reply["ok"] is True, reply
        assert reply["handshake"]["recoveryKind"] == expected_kind
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_begin_refuses_competing_capture_before_second_observation(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    first_result: list[dict[str, object]] = []
    first_errors: list[BaseException] = []
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )

        def blocked_observation() -> dict[str, object]:
            calls[0] += 1
            entered.set()
            if not release.wait(3.0):
                raise DriverError("recovery observation fixture did not release")
            return {
                "surface": "absent",
                "identityFingerprint": identity.fingerprint,
                "captureIdentity": identity.fingerprint,
                "captureMonotonic": 1.0,
            }

        daemon._native_construction_observation_provider = blocked_observation

        def begin_first() -> None:
            try:
                first_result.append(
                    daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                first_errors.append(exc)

        worker = threading.Thread(target=begin_first, name="native-recovery-first")
        worker.start()
        assert entered.wait(2.0)
        second = SessionIPC(daemon.controller, owner=daemon).dispatch(
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
        )
        assert second["ok"] is False
        assert "already in flight" in str(second["error"])
        assert calls[0] == 1
        release.set()
        worker.join(timeout=4.0)
        assert not worker.is_alive()
        assert not first_errors, first_errors
        assert first_result and first_result[0]["handshake"]["state"] == "recovery_required"
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_restart_refuses_live_reservation(tmp_path: Path) -> None:
    """A restart-style recovery cannot bypass a live public preflight owner."""

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    observation_calls = [0]

    def counted_preflight_observation() -> dict[str, object]:
        observation_calls[0] += 1
        return _construction_frame(identity, boot="boot-a", sequence=2)

    try:
        daemon._native_construction_observation_provider = counted_preflight_observation
        preflight = daemon.native_construction_control_preflight(expected_generation=7)
        assert observation_calls == [1]
        reservation_before_restart = copy.deepcopy(daemon._native_construction_reservation)
        assert reservation_before_restart is not None
        reservation_id = preflight["reservation"]["reservationId"]
        with pytest.raises(DriverError, match="recovery restart cannot overlap a live reservation"):
            daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        assert observation_calls == [1]
        assert daemon._native_construction_reservation == reservation_before_restart
        assert daemon._native_construction_reservation["reservationId"] == reservation_id
        assert daemon._native_construction_inflight is None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_context_rejects_existing_owner_without_guardian_generation(tmp_path: Path) -> None:
    """Only a truly ownerless legacy start may sample the current Guardian generation."""

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        with daemon._native_construction_lock:
            context = daemon._native_construction_start_context_locked(generation=7, deferred=False)
            assert context["guardianGeneration"] == driver.guardian.generation
            daemon._native_construction_run_context = None
            daemon._native_construction_reservation = {"reservationId": "malformed-owner"}
            with pytest.raises(DriverError, match="missing Guardian generation"):
                daemon._native_construction_start_context_locked(generation=7, deferred=True)
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_snapshot_rejects_live_guardian_generation_drift(tmp_path: Path) -> None:
    """Snapshot validation compares the immutable owner generation to live Guardian state."""

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        driver.guardian.generation += 1
        with pytest.raises(DriverError, match="Guardian binding changed"):
            daemon._native_construction_validate_snapshot(reservation, expected_generation=7)
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_cancel_revokes_inflight_capture_epoch(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    entered = threading.Event()
    release = threading.Event()
    begin_errors: list[BaseException] = []
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_control_preflight(expected_generation=7)
        reservation_id = prepared["reservation"]["reservationId"]

        def blocked_observation() -> dict[str, object]:
            entered.set()
            if not release.wait(3.0):
                raise DriverError("inflight recovery fixture did not release")
            return {
                "surface": "absent",
                "identityFingerprint": identity.fingerprint,
                "captureIdentity": identity.fingerprint,
                "captureMonotonic": 1.0,
            }

        daemon._native_construction_observation_provider = blocked_observation

        def begin() -> None:
            try:
                daemon.native_construction_recovery_begin(
                    expected_generation=7, reservation_id=reservation_id
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                begin_errors.append(exc)

        worker = threading.Thread(target=begin, name="native-recovery-cancel-inflight")
        worker.start()
        assert entered.wait(2.0)
        cancelled = daemon.native_construction_recovery_cancel(
            expected_generation=7, reservation_id=reservation_id
        )
        assert cancelled["handshake"]["state"] == "cancelled"
        release.set()
        worker.join(timeout=4.0)
        assert not worker.is_alive()
        assert begin_errors and isinstance(begin_errors[0], DriverError)
        assert "revoked" in str(begin_errors[0])
        assert daemon._native_construction_inflight is None
        assert daemon._native_construction_ticket is None
        assert daemon.controller.native_construction_checkpoint() is not None
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_complete_cancel_barrier_preserves_checkpoint(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        begin = {
            "surface": "absent",
            "identityFingerprint": identity.fingerprint,
            "captureIdentity": identity.fingerprint,
            "captureMonotonic": 1.0,
        }
        daemon._native_construction_observation_provider = lambda: begin
        started = daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        reservation_id = started["reservation"]["reservationId"]
        valid = _construction_frame(identity, boot="ordinary-new", sequence=3)
        valid["captureMonotonic"] = 2.0
        daemon._native_construction_observation_provider = lambda: valid
        second_validation = threading.Event()
        release_completion = threading.Event()
        validation_count = [0]
        original_validate = daemon._native_construction_validate_current_locked

        def gated_validate(*args: object, **kwargs: object) -> WindowIdentity:
            result = original_validate(*args, **kwargs)
            validation_count[0] += 1
            if validation_count[0] == 2:
                second_validation.set()
                if not release_completion.wait(3.0):
                    raise DriverError("recovery completion fixture did not release")
            return result

        daemon._native_construction_validate_current_locked = gated_validate  # type: ignore[method-assign]
        completion_errors: list[BaseException] = []

        def complete() -> None:
            try:
                daemon.native_construction_recovery_complete(
                    expected_generation=7, reservation_id=reservation_id
                )
            except BaseException as exc:
                completion_errors.append(exc)

        completion_thread = threading.Thread(target=complete, name="native-recovery-complete")
        completion_thread.start()
        assert second_validation.wait(2.0)
        cancelled = daemon.native_construction_recovery_cancel(
            expected_generation=7, reservation_id=reservation_id
        )
        assert cancelled["handshake"]["state"] == "cancelled"
        assert daemon.controller.native_construction_checkpoint() is not None
        release_completion.set()
        completion_thread.join(timeout=4.0)
        assert not completion_thread.is_alive()
        assert completion_errors and isinstance(completion_errors[0], DriverError)
        assert "revoked" in str(completion_errors[0])
        assert daemon.controller.native_construction_checkpoint() is not None
    finally:
        release_completion.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_complete_expired_ticket_returns_typed_ipc_error(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        daemon._native_construction_observation_provider = lambda: {
            "surface": "absent",
            "identityFingerprint": identity.fingerprint,
            "captureIdentity": identity.fingerprint,
            "captureMonotonic": 1.0,
        }
        started = daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        reservation_id = started["reservation"]["reservationId"]
        daemon._native_construction_reservation["expiresMonotonic"] = daemon._native_clock() - 1.0
        daemon._native_construction_operation_deadline = daemon._native_clock() - 1.0
        reply = SessionIPC(daemon.controller, owner=daemon).dispatch(
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": reservation_id,
            }
        )
        assert reply["ok"] is False
        assert "expired" in str(reply["error"])
        assert "AttributeError" not in str(reply["error"])
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_durable_commit_failure_keeps_checkpoint_for_fresh_owner(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        begin = _construction_frame(identity, boot="boot-a", sequence=2)
        begin["snapshot"]["scanner"]["scheduleFence"]["state"] = "inactive"  # type: ignore[index]
        valid = _construction_frame(identity, boot="ordinary-new", sequence=3)
        valid["captureMonotonic"] = 2.0
        daemon._native_construction_observation_provider = lambda: begin
        started = daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        reservation_id = started["reservation"]["reservationId"]
        daemon._native_construction_observation_provider = lambda: valid
        original_write = daemon.controller.journal.write
        write_calls = [0]
        original_history = copy.deepcopy(daemon.controller._history)

        def fail_commit(*args: object, **kwargs: object) -> object:
            write_calls[0] += 1
            raise OSError("durable recovery fixture failure")

        daemon.controller.journal.write = fail_commit  # type: ignore[method-assign]
        with pytest.raises(OSError, match="durable recovery fixture failure"):
            daemon.native_construction_recovery_complete(expected_generation=7, reservation_id=reservation_id)
        daemon.controller.journal.write = original_write  # type: ignore[method-assign]
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["phase"] == "recovery_required"
        assert write_calls == [1]
        assert daemon.controller._history == original_history
        fresh_dir = tmp_path / "fresh-owner"
        fresh_dir.mkdir()
        fresh_driver, _fresh_identity, fresh_child = _native_driver(fresh_dir)
        try:
            fresh = Controller(daemon.controller.journal, driver=fresh_driver, recover_on_restart=False)
            assert fresh.native_construction_checkpoint() == checkpoint
            assert fresh.native_construction_checkpoint_valid() is True
        finally:
            fresh_driver.close()
            fresh_child.terminate()
            fresh_child.wait(timeout=3)
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize("failed_write", [1, 2], ids=["archive-write", "checkpoint-write"])
def test_native_calibrated_rolls_back_on_transient_durable_write(tmp_path: Path, failed_write: int) -> None:
    """Archive/checkpoint failures cannot leave a false native gate in memory."""

    from crowley.session.journal import JournalError
    from tests.test_session import native_calibration

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    fresh_driver: DesktopDriver | None = None
    fresh_child: subprocess.Popen[str] | None = None
    try:
        controller = daemon.controller
        evidence = native_calibration(process_start="native-r9-transient-write")
        prior_history = copy.deepcopy(controller._calibration_history)
        prior_evidence = copy.deepcopy(controller._calibration_evidence)
        prior_reason = controller._calibration_invalidated_reason
        prior_controller_history = copy.deepcopy(controller._history)
        prior_state = controller.status().state
        original_write = controller.journal.write
        write_calls = [0]

        def fail_one_write(value: dict[str, object]) -> None:
            write_calls[0] += 1
            if write_calls[0] == failed_write:
                raise JournalError("transient native finalize write failure")
            original_write(value)

        controller.journal.write = fail_one_write  # type: ignore[method-assign]
        with pytest.raises(JournalError, match="transient native finalize write failure"):
            controller.native_calibrated(
                evidence,
                expected_generation=7,
                expected_state=ControllerState.NATIVE_CALIBRATION,
            )
        controller.journal.write = original_write  # type: ignore[method-assign]
        assert write_calls == [failed_write]
        assert controller._native_calibrated is False
        assert controller._calibration_evidence == prior_evidence
        assert controller._calibration_history == prior_history
        assert controller._calibration_invalidated_reason == prior_reason
        assert controller._history == prior_controller_history
        assert controller.status().state is prior_state is ControllerState.NATIVE_CALIBRATION
        persisted = controller.journal.read()
        assert persisted["nativeCalibration"] is False
        assert persisted["state"] == ControllerState.NATIVE_CALIBRATION.value
        fresh_dir = tmp_path / f"fresh-owner-{failed_write}"
        fresh_dir.mkdir()
        fresh_driver, _fresh_identity, fresh_child = _native_driver(fresh_dir)
        fresh = Controller(controller.journal, driver=fresh_driver, recover_on_restart=False)
        assert fresh.status().state is ControllerState.NATIVE_CALIBRATION
        assert fresh._native_calibrated is False
    finally:
        if fresh_driver is not None:
            fresh_driver.close()
        if fresh_child is not None:
            fresh_child.terminate()
            fresh_child.wait(timeout=3)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize("failed_write", [1, 2], ids=["finalize-archive", "finalize-state"])
def test_native_finalize_owner_error_handler_preserves_recovery_after_one_shot_write_failure(
    tmp_path: Path, failed_write: int
) -> None:
    """The real native owner converts a finalize write fault into recovery evidence."""

    from crowley.core.watchlist import load_catalog
    from crowley.session.journal import JournalError

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    worker = None
    try:
        daemon.config = replace(daemon.config, plan_hash=load_catalog().scan_plan()["planHash"])
        _configure_deadline_png_runner(daemon, driver, identity, tmp_path)
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        running = daemon.native_construction_recheck(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        assert running["handshake"]["state"] == "running"

        controller = daemon.controller
        original_write = controller.journal.write
        finalize_started = threading.Event()
        write_calls = [0]
        original_native_calibrated = controller.native_calibrated

        def observed_native_calibrated(*args: object, **kwargs: object) -> object:
            finalize_started.set()
            return original_native_calibrated(*args, **kwargs)

        controller.native_calibrated = observed_native_calibrated  # type: ignore[method-assign]

        def fail_one_write(value: dict[str, object]) -> None:
            if finalize_started.is_set():
                write_calls[0] += 1
            if finalize_started.is_set() and write_calls[0] == failed_write:
                raise JournalError("one-shot native finalize write failure")
            original_write(value)

        controller.journal.write = fail_one_write  # type: ignore[method-assign]
        daemon.start_calibration("native", expected_generation=7)
        worker = daemon._calibration_thread
        assert worker is not None
        assert _wait_for(
            lambda: daemon._native_construction_handshake.get("state") == "resume_ready",
            # The production gate retains its 30-second per-frame bound. This
            # outer test wait includes 103 real image encode/decode calls and
            # needs CPU headroom when twelve native workers are active.
            timeout=60.0,
        ), (
            daemon._native_construction_handshake.get("state"),
            daemon._calibration_operation.get("error"),
            daemon._native_handshake.get("failure"),
        )
        daemon.native_construction_finalize_admit(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        worker.join(timeout=90.0)
        assert not worker.is_alive()
        operation = daemon.calibration_status()["calibrationOperation"]
        assert operation["state"] == "error"
        assert "one-shot native finalize write failure" in operation["error"]
        assert write_calls[0] >= failed_write + 1

        checkpoint = controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["phase"] == "recovery_required"
        persisted = controller.journal.read()
        assert persisted["nativeCalibration"] is False
        assert persisted["state"] == ControllerState.NATIVE_CALIBRATION.value
        assert persisted["current"].get("state") != ControllerState.INPUT_CALIBRATION.value
        assert daemon._native_construction_handshake["state"] == "error"
        assert daemon._native_construction_reservation is None

        controller.journal.write = original_write  # type: ignore[method-assign]
        fresh_dir = tmp_path / "fresh-finalize-owner"
        fresh_dir.mkdir()
        fresh_driver, _fresh_identity, fresh_child = _native_driver(fresh_dir)
        try:
            fresh = Controller(controller.journal, driver=fresh_driver, recover_on_restart=False)
            assert fresh.native_construction_checkpoint() == checkpoint
            assert fresh.native_construction_checkpoint_valid() is True
            assert fresh.status().state is ControllerState.NATIVE_CALIBRATION
        finally:
            fresh_driver.close()
            fresh_child.terminate()
            fresh_child.wait(timeout=3)
    finally:
        if worker is not None:
            worker.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_real_predecessors_reject_before_admission(tmp_path: Path) -> None:
    """Exercise predecessor failures through a real daemon and SessionIPC owner."""

    def run_case(
        name: str,
        *,
        state: ControllerState,
        setup: Callable[[SessionDaemon, WindowIdentity], None] | None,
        request: dict[str, object],
        expected: str,
    ) -> None:
        case_dir = tmp_path / name
        case_dir.mkdir()
        daemon, driver, identity, child = _construction_daemon(case_dir, state=state)
        try:
            if setup is not None:
                setup(daemon, identity)
            reply = SessionIPC(daemon.controller, owner=daemon).dispatch(request)
            assert reply == {"ok": False, "error": expected}, reply
        finally:
            driver.close()
            child.terminate()
            child.wait(timeout=3)

    run_case(
        "bootstrap-before-prepare",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=None,
        request={
            "command": "native_construction_bootstrap",
            "generation": 7,
            "reservationId": "missing",
            "expectedConstructorBoot": "native-construction:boot-a",
        },
        expected="native construction reservation is unknown",
    )
    run_case(
        "recheck-before-bootstrap",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=None,
        request={"command": "native_construction_recheck", "generation": 7, "reservationId": "missing"},
        expected="native construction reservation is unknown",
    )
    run_case(
        "finalize-before-resume-ready",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=None,
        request={
            "command": "native_construction_finalize_admit",
            "generation": 7,
            "reservationId": "missing",
        },
        expected="native construction reservation is unknown",
    )
    run_case(
        "recovery-complete-without-ticket",
        state=ControllerState.PAUSED,
        setup=None,
        request={
            "command": "native_construction_recovery_complete",
            "generation": 7,
            "reservationId": "missing",
        },
        expected="native recovery ticket is unknown",
    )
    run_case(
        "recovery-cancel-without-ticket",
        state=ControllerState.PAUSED,
        setup=None,
        request={
            "command": "native_construction_recovery_cancel",
            "generation": 7,
            "reservationId": "missing",
        },
        expected="native construction reservation is unknown",
    )
    run_case(
        "prepare-existing-checkpoint",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=lambda daemon, _identity: daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        ),
        request={"command": "native_construction_prepare", "generation": 7},
        expected="native construction checkpoint already exists",
    )
    run_case(
        "control-preflight-wrong-state",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=None,
        request={"command": "native_construction_control_preflight", "generation": 7},
        expected="native construction prepare requires PAUSED",
    )
    run_case(
        "recovery-begin-wrong-state",
        state=ControllerState.NATIVE_CALIBRATION,
        setup=None,
        request={
            "command": "native_construction_recovery_begin",
            "generation": 7,
            "reservationId": "",
        },
        expected="native construction recovery requires the current PAUSED generation",
    )

    def checkpoint_setup(daemon: SessionDaemon, _identity: WindowIdentity) -> None:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        daemon._native_construction_observation_provider = lambda: {
            "surface": "absent",
            "identityFingerprint": daemon.driver.resolve_identity().fingerprint,
            "captureIdentity": daemon.driver.resolve_identity().fingerprint,
            "captureMonotonic": 1.0,
        }

    def cancel_ticket(daemon: SessionDaemon, _identity: WindowIdentity) -> None:
        started = daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        daemon.native_construction_recovery_cancel(
            expected_generation=7,
            reservation_id=started["reservation"]["reservationId"],
        )

    run_case(
        "complete-after-cancel",
        state=ControllerState.PAUSED,
        setup=lambda daemon, identity: (checkpoint_setup(daemon, identity), cancel_ticket(daemon, identity)),
        request={
            "command": "native_construction_recovery_complete",
            "generation": 7,
            "reservationId": "cancelled",
        },
        expected="native recovery ticket is unknown",
    )

    def completed_ticket(daemon: SessionDaemon, identity: WindowIdentity) -> None:
        checkpoint_setup(daemon, identity)
        started = daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        daemon._native_construction_observation_provider = lambda: (
            _construction_frame(identity, boot="ordinary-new", sequence=3) | {"captureMonotonic": 2.0}
        )
        daemon.native_construction_recovery_complete(
            expected_generation=7,
            reservation_id=started["reservation"]["reservationId"],
        )

    run_case(
        "cancel-after-complete",
        state=ControllerState.PAUSED,
        setup=completed_ticket,
        request={
            "command": "native_construction_recovery_cancel",
            "generation": 7,
            "reservationId": "completed",
        },
        expected="native construction reservation is unknown",
    )

    def preflight_handoff(daemon: SessionDaemon, identity: WindowIdentity) -> None:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_control_preflight(expected_generation=7)
        daemon._native_construction_observation_provider = lambda: {
            "surface": "absent",
            "identityFingerprint": identity.fingerprint,
            "captureIdentity": identity.fingerprint,
            "captureMonotonic": 1.0,
        }
        daemon._wrong_handoff_id = prepared["reservation"]["reservationId"]

    run_case(
        "recovery-handoff-wrong-id",
        state=ControllerState.PAUSED,
        setup=preflight_handoff,
        request={
            "command": "native_construction_recovery_begin",
            "generation": 7,
            "reservationId": "measure-reservation-that-is-not-live",
        },
        expected="preflight reservation is unavailable for recovery handoff",
    )


def test_native_construction_public_predecessors_reach_live_owner_guards(tmp_path: Path) -> None:
    """Use live reservation/ticket IDs so named predecessor guards execute."""

    def checkpoint(daemon: SessionDaemon) -> None:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )

    daemon, driver, identity, child = _construction_daemon(tmp_path / "bootstrap-replay")
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        ipc = SessionIPC(daemon.controller, owner=daemon)
        prepared = ipc.dispatch({"command": "native_construction_prepare", "generation": 7})
        reservation = prepared["reservation"]["reservationId"]
        expected_boot = prepared["reservation"]["expectedConstructorBoot"]
        bootstrapped = ipc.dispatch(
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": reservation,
                "expectedConstructorBoot": expected_boot,
            }
        )
        assert bootstrapped["ok"] is True
        replay = ipc.dispatch(
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": reservation,
                "expectedConstructorBoot": expected_boot,
            }
        )
        assert replay == {"ok": False, "error": "native bootstrap was replayed"}
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)

    daemon, driver, identity, child = _construction_daemon(tmp_path / "stale-echo")
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        ipc = SessionIPC(daemon.controller, owner=daemon)
        prepared = ipc.dispatch({"command": "native_construction_prepare", "generation": 7})
        stale = ipc.dispatch(
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": prepared["reservation"]["reservationId"],
                "expectedConstructorBoot": "native-construction:wrong-boot",
            }
        )
        assert stale == {"ok": False, "error": "native construction boot echo is stale"}
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)

    daemon, driver, identity, child = _construction_daemon(tmp_path / "finalize-order")
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        ipc = SessionIPC(daemon.controller, owner=daemon)
        prepared = ipc.dispatch({"command": "native_construction_prepare", "generation": 7})
        out_of_order = ipc.dispatch(
            {
                "command": "native_construction_finalize_admit",
                "generation": 7,
                "reservationId": prepared["reservation"]["reservationId"],
            }
        )
        assert out_of_order == {
            "ok": False,
            "error": "native construction finalize admission is out of order",
        }
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)

    daemon, driver, identity, child = _construction_daemon(tmp_path / "callback-order")
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        ipc = SessionIPC(daemon.controller, owner=daemon)
        prepared = ipc.dispatch({"command": "native_construction_prepare", "generation": 7})
        reservation = prepared["reservation"]
        bootstrapped = ipc.dispatch(
            {
                "command": "native_construction_bootstrap",
                "generation": 7,
                "reservationId": reservation["reservationId"],
                "expectedConstructorBoot": reservation["expectedConstructorBoot"],
            }
        )
        assert bootstrapped["ok"] is True
        with pytest.raises(DriverError, match="deferred hold-required callback arrived out of order"):
            daemon.notify_native_hold_required(identity)
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)

    daemon, driver, identity, child = _construction_daemon(
        tmp_path / "recovery-overlap", state=ControllerState.PAUSED
    )
    entered = threading.Event()
    release = threading.Event()
    try:
        checkpoint(daemon)

        def blocked_observation() -> dict[str, object]:
            entered.set()
            assert release.wait(3.0)
            return {
                "surface": "absent",
                "identityFingerprint": identity.fingerprint,
                "captureIdentity": identity.fingerprint,
                "captureMonotonic": 1.0,
            }

        daemon._native_construction_observation_provider = blocked_observation
        ipc = SessionIPC(daemon.controller, owner=daemon)
        first_response: list[dict[str, object]] = []

        def begin() -> None:
            first_response.append(
                ipc.dispatch(
                    {
                        "command": "native_construction_recovery_begin",
                        "generation": 7,
                        "reservationId": "",
                    }
                )
            )

        thread = threading.Thread(target=begin, name="native-recovery-overlap")
        thread.start()
        assert entered.wait(3.0)
        overlap = ipc.dispatch(
            {
                "command": "native_construction_recovery_begin",
                "generation": 7,
                "reservationId": "",
            }
        )
        assert overlap == {
            "ok": False,
            "error": "native construction recovery admission is already in flight",
        }
        release.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert first_response and first_response[0]["ok"] is True
        ticket = first_response[0]["reservation"]["reservationId"]
        cancelled = ipc.dispatch(
            {
                "command": "native_construction_recovery_cancel",
                "generation": 7,
                "reservationId": ticket,
            }
        )
        assert cancelled["ok"] is True
        after_cancel = ipc.dispatch(
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": ticket,
            }
        )
        assert after_cancel == {"ok": False, "error": "native recovery ticket is unknown"}
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_completion_rejects_old_prefixed_and_fenced_frames_via_ipc(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        begin = _construction_frame(identity, boot="boot-a", sequence=2)
        begin["snapshot"]["scanner"]["scheduleFence"]["state"] = "inactive"  # type: ignore[index]
        daemon._native_construction_observation_provider = lambda: begin
        ipc = SessionIPC(daemon.controller, owner=daemon)
        started = ipc.dispatch(
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
        )
        assert started["ok"] is True
        reservation_id = started["reservation"]["reservationId"]

        expected_old_frame = _construction_frame(identity, boot="native-construction:boot-a", sequence=3)
        expected_old_frame["captureMonotonic"] = 2.0
        old_frame = _construction_frame(identity, boot="boot-a", sequence=3)
        old_frame["captureMonotonic"] = 2.25
        absent_frame = {
            "surface": "absent",
            "identityFingerprint": identity.fingerprint,
            "captureIdentity": identity.fingerprint,
            "captureMonotonic": 2.5,
        }
        loading_frame = _construction_frame(identity, boot="boot-a", sequence=3)
        loading_frame["snapshot"]["scanner"]["scheduleFence"]["state"] = "armed"  # type: ignore[index]
        loading_frame["captureMonotonic"] = 2.75
        valid = _construction_frame(identity, boot="ordinary-new", sequence=4)
        valid["captureMonotonic"] = 3.0
        observations = iter((expected_old_frame, old_frame, absent_frame, loading_frame, valid))
        daemon._native_construction_observation_provider = lambda: next(observations)
        recovered = ipc.dispatch(
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": reservation_id,
            }
        )
        assert recovered["ok"] is True, recovered
        assert recovered["handshake"]["state"] == "recovered"
        assert [entry["state"] for entry in recovered["stateHistory"]] == [
            "recovery_required",
            "recovered",
        ]
        assert all(set(entry) == {"state", "nativeClock"} for entry in recovered["stateHistory"])
        assert daemon.controller.native_construction_checkpoint() is None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_recovery_completion_rejects_construction_prefixed_boot_immediately(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        begin = _construction_frame(identity, boot="boot-a", sequence=2)
        begin["snapshot"]["scanner"]["scheduleFence"]["state"] = "inactive"  # type: ignore[index]
        daemon._native_construction_observation_provider = lambda: begin
        ipc = SessionIPC(daemon.controller, owner=daemon)
        started = ipc.dispatch(
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
        )
        assert started["ok"] is True
        prefixed = _construction_frame(identity, boot="native-construction:other", sequence=3)
        prefixed["captureMonotonic"] = 2.0
        daemon._native_construction_observation_provider = lambda: prefixed
        started_at = time.monotonic()
        reply = ipc.dispatch(
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": started["reservation"]["reservationId"],
            }
        )
        assert time.monotonic() - started_at < 2.0
        assert reply["ok"] is False
        assert "ordinary boot" in reply["error"]
        assert daemon.controller.native_construction_checkpoint() is not None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_callbacks_admit_finalize_and_expiry_fail_closed(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        assert prepared["handshake"]["operationBoundSeconds"] == 3130.0
        reservation = prepared["reservation"]
        assert set(reservation) == {
            "reservationId",
            "reservationKind",
            "reservationState",
            "controllerGeneration",
            "desktopLeaseHeld",
            "guardianGeneration",
            "guardianArmed",
            "ownerPid",
            "ownerProcessStart",
            "profileFingerprint",
            "identityFingerprint",
            "B0",
            "expectedConstructorBoot",
            "expiresMonotonic",
        }
        reservation_id = reservation["reservationId"]
        expected_boot = reservation["expectedConstructorBoot"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation_id,
            expected_constructor_boot=expected_boot,
        )
        assert (
            daemon.native_construction_recheck(expected_generation=7, reservation_id=reservation_id)[
                "handshake"
            ]["state"]
            == "running"
        )
        with pytest.raises(DriverError, match="resume_ready"):
            daemon.native_construction_finalize_admit(expected_generation=7, reservation_id=reservation_id)

        record_payload = {
            "phase": "construction",
            "ordinal": 0,
            "pid": identity.pid,
            "process_start": identity.process_start,
            "process_executable": driver.native_memory_meter.process_executable,
            "identity_fingerprint": identity.fingerprint,
        }
        record = SimpleNamespace(
            **record_payload,
            to_dict=lambda: record_payload,
            verify=lambda: None,
        )
        daemon.notify_native_memory_reset_ready(identity, record)
        absence_path = tmp_path / "absence.png"
        Image.new("RGB", (256, 256), (127, 127, 127)).save(absence_path)
        driver.capture_client_crop_for_identity = lambda _identity: absence_path  # type: ignore[method-assign]
        driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
        absence_digest = hashlib.sha256(absence_path.read_bytes()).hexdigest()
        daemon._native_bootstrap_observation = {
            "identity": identity.fingerprint,
            "capture": daemon._native_clock(),
            "captureDigest": absence_digest,
            "capturePath": str(absence_path),
        }
        daemon._native_bootstrap_ready.set()
        daemon._native_preconstruction_provider(
            identity,
            baseline_reader=lambda _pid: 10,
            baseline_ready=lambda _pid, value: daemon.notify_native_baseline_ready(identity, value),
        )
        daemon.release_native_capture(daemon._native_construction_handshake["absenceCapturePath"])
        daemon.notify_native_hold_required(identity)
        record_payload = {**record_payload, "phase": "updates"}
        daemon.notify_native_memory_reset_ready(
            identity,
            SimpleNamespace(
                **record_payload,
                to_dict=lambda: record_payload,
                verify=lambda: None,
            ),
        )
        pending = SimpleNamespace(
            pid=identity.pid,
            process_start=identity.process_start,
            process_executable=driver.native_memory_meter.process_executable,
            identity_fingerprint=identity.fingerprint,
            phase="updates",
            ordinal=0,
        )
        daemon.notify_native_memory_ready(identity, pending)
        boundary = NativeResumeBoundary._issue(
            identity_fingerprint=identity.fingerprint,
            process_start=identity.process_start,
            process_executable=identity.executable,
            boot=expected_boot,
            sequence=10,
            crc32=10,
            update_count=100,
            capture_monotonic=2.0,
            capture_sha256="b" * 64,
            render=_metrics(100),
        )
        daemon.notify_native_resume_required(identity, boundary)
        daemon.notify_native_resume_ready(
            identity,
            SimpleNamespace(
                native_identity_fingerprint=identity.fingerprint,
                process_pid=identity.pid,
                resume_update_count=101,
            ),
            SimpleNamespace(
                native_phase="normal",
                native_ordinal=0,
                metrics=_metrics(101, phase="normal", ordinal=0),
                identity_fingerprint=identity.fingerprint,
                boot=expected_boot,
                sequence=11,
                capture_monotonic=3.0,
            ),
        )
        finalized = daemon.native_construction_finalize_admit(
            expected_generation=7, reservation_id=reservation_id
        )
        assert finalized["handshake"]["state"] == "finalize_required"
        daemon._native_construction_reservation["expiresMonotonic"] = daemon._native_clock() - 1.0
        with pytest.raises(DriverError, match="unknown|expired"):
            daemon.native_construction_recheck(expected_generation=7, reservation_id=reservation_id)
        assert daemon.controller.native_construction_checkpoint()["phase"] == "recovery_required"
        daemon.controller.pause("native construction recovery")
        recovery_generation = daemon.controller.status().generation
        daemon._native_construction_observation_provider = lambda: {
            "surface": "absent",
            "identityFingerprint": identity.fingerprint,
            "captureIdentity": identity.fingerprint,
            "captureMonotonic": daemon._native_clock(),
        }
        recovery = daemon.native_construction_recovery_begin(
            expected_generation=recovery_generation, reservation_id=""
        )
        recovery_id = recovery["reservation"]["reservationId"]
        daemon._native_construction_observation_provider = lambda: (
            _construction_frame(identity, boot="ordinary-new", sequence=5)
            | {"captureMonotonic": daemon._native_clock() + 1.0}
        )
        completed = daemon.native_construction_recovery_complete(
            expected_generation=recovery_generation, reservation_id=recovery_id
        )
        assert completed["handshake"]["state"] == "recovered"
        assert daemon.controller.native_construction_checkpoint() is None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_operation_bound_expires_at_finite_boundary(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    clock = [100.0]
    daemon._native_clock = lambda: clock[0]  # type: ignore[method-assign]
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        operation_deadline = daemon._native_construction_operation_deadline
        assert operation_deadline == 100.0 + 3130.0
        clock[0] = float(operation_deadline)
        with pytest.raises(DriverError, match="unknown|expired"):
            daemon.native_construction_recheck(
                expected_generation=7, reservation_id=reservation["reservationId"]
            )
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["phase"] == "recovery_required"
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def _prepare_deferred_resume_required(
    daemon: SessionDaemon,
    driver: DesktopDriver,
    identity: WindowIdentity,
    tmp_path: Path,
) -> tuple[str, str, NativeResumeBoundary]:
    """Drive a real deferred owner through the public callback boundaries."""

    daemon._native_construction_observation_provider = lambda: _construction_frame(
        identity, boot="boot-a", sequence=2
    )
    prepared = daemon.native_construction_prepare(expected_generation=7)
    reservation = prepared["reservation"]
    daemon.native_construction_bootstrap(
        expected_generation=7,
        reservation_id=reservation["reservationId"],
        expected_constructor_boot=reservation["expectedConstructorBoot"],
    )
    record_payload: dict[str, object] = {
        "phase": "construction",
        "ordinal": 0,
        "pid": identity.pid,
        "process_start": identity.process_start,
        "process_executable": driver.native_memory_meter.process_executable,
        "identity_fingerprint": identity.fingerprint,
    }
    daemon.notify_native_memory_reset_ready(
        identity,
        SimpleNamespace(
            **record_payload,
            to_dict=lambda: record_payload,
            verify=lambda: None,
        ),
    )
    absence_path = tmp_path / "resume-absence.png"
    Image.new("RGB", (256, 256), (127, 127, 127)).save(absence_path)
    driver.capture_client_crop_for_identity = lambda _identity: absence_path  # type: ignore[method-assign]
    driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
    absence_digest = hashlib.sha256(absence_path.read_bytes()).hexdigest()
    daemon._native_bootstrap_observation = {
        "identity": identity.fingerprint,
        "capture": daemon._native_clock(),
        "captureDigest": absence_digest,
        "capturePath": str(absence_path),
    }
    daemon._native_bootstrap_ready.set()
    daemon._native_preconstruction_provider(
        identity,
        baseline_reader=lambda _pid: 10,
        baseline_ready=lambda _pid, value: daemon.notify_native_baseline_ready(identity, value),
    )
    daemon.release_native_capture(daemon._native_construction_handshake["absenceCapturePath"])
    daemon.notify_native_hold_required(identity)
    update_payload = {**record_payload, "phase": "updates"}
    daemon.notify_native_memory_reset_ready(
        identity,
        SimpleNamespace(
            **update_payload,
            to_dict=lambda: update_payload,
            verify=lambda: None,
        ),
    )
    pending = SimpleNamespace(
        pid=identity.pid,
        process_start=identity.process_start,
        process_executable=driver.native_memory_meter.process_executable,
        identity_fingerprint=identity.fingerprint,
        phase="updates",
        ordinal=0,
    )
    daemon.notify_native_memory_ready(identity, pending)
    boundary = NativeResumeBoundary._issue(
        identity_fingerprint=identity.fingerprint,
        process_start=identity.process_start,
        process_executable=identity.executable,
        boot=reservation["expectedConstructorBoot"],
        sequence=10,
        crc32=10,
        update_count=100,
        capture_monotonic=2.0,
        capture_sha256="b" * 64,
        render=_metrics(100),
    )
    daemon.notify_native_resume_required(identity, boundary)
    return reservation["reservationId"], reservation["expectedConstructorBoot"], boundary


@pytest.mark.parametrize(
    "malformed_kind",
    ["missing_capture", "missing_sequence", "none_sequence", "nonfinite_sequence"],
)
def test_native_resume_ready_malformed_facts_refuse_before_publication(
    tmp_path: Path, malformed_kind: str
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        reservation_id, expected_boot, _boundary = _prepare_deferred_resume_required(
            daemon, driver, identity, tmp_path
        )
        evidence = SimpleNamespace(
            native_identity_fingerprint=identity.fingerprint,
            process_pid=identity.pid,
            resume_update_count=101,
        )
        resumed_values: dict[str, object] = {
            "native_phase": "normal",
            "native_ordinal": 0,
            "metrics": _metrics(101, phase="normal", ordinal=0),
            "identity_fingerprint": identity.fingerprint,
            "boot": expected_boot,
            "sequence": 11,
            "capture_monotonic": 3.0,
        }
        if malformed_kind == "missing_capture":
            del resumed_values["capture_monotonic"]
        elif malformed_kind == "missing_sequence":
            del resumed_values["sequence"]
        elif malformed_kind == "none_sequence":
            resumed_values["sequence"] = None
        else:
            resumed_values["sequence"] = float("nan")
        with pytest.raises(DriverError, match="native resume-ready observation facts are malformed"):
            daemon.notify_native_resume_ready(
                identity,
                evidence,
                SimpleNamespace(**resumed_values),
            )
        with daemon._native_construction_lock:
            assert daemon._native_construction_handshake["state"] == "resume_required"
            assert daemon._native_construction_handshake.get("resumeReady") is False
            assert daemon._native_construction_reservation is not None
            assert daemon._native_construction_reservation["reservationId"] == reservation_id
        assert daemon.controller.status().state is ControllerState.NATIVE_CALIBRATION
        assert daemon.controller.native_construction_checkpoint() is not None
        assert daemon.controller.native_construction_checkpoint()["phase"] == "prepared"

        receipt = daemon.notify_native_measurement_failed(identity, f"malformed {malformed_kind}")
        assert receipt.reason == f"malformed {malformed_kind}"
        assert daemon.controller.status().state is ControllerState.NATIVE_CALIBRATION
        assert daemon.controller.native_construction_checkpoint()["phase"] == "recovery_required"
        with daemon._native_construction_lock:
            assert daemon._native_construction_handshake["state"] == "restoration_required"
            assert daemon._native_construction_reservation is None
            assert daemon._native_construction_run_context["closed"] is True
            assert all(
                entry["state"] != "resume_ready" for entry in daemon._native_construction_state_history
            )
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_deferred_callbacks_reject_duplicate_resume_boundaries(tmp_path: Path) -> None:
    """Deferred callbacks keep their published ordering after hold-required."""

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        reservation_id, expected_boot, boundary = _prepare_deferred_resume_required(
            daemon, driver, identity, tmp_path
        )
        with pytest.raises(DriverError, match="deferred resume-required callback arrived out of order"):
            daemon.notify_native_resume_required(identity, boundary)

        evidence = SimpleNamespace(
            native_identity_fingerprint=identity.fingerprint,
            process_pid=identity.pid,
            resume_update_count=101,
        )
        resumed = SimpleNamespace(
            native_phase="normal",
            native_ordinal=0,
            metrics=_metrics(101, phase="normal", ordinal=0),
            identity_fingerprint=identity.fingerprint,
            boot=expected_boot,
            sequence=11,
            capture_monotonic=3.0,
        )
        daemon.notify_native_resume_ready(identity, evidence, resumed)
        with pytest.raises(DriverError, match="deferred resume-ready callback arrived out of order"):
            daemon.notify_native_resume_ready(identity, evidence, resumed)
        with daemon._native_construction_lock:
            assert daemon._native_construction_handshake["state"] == "resume_ready"
            assert daemon._native_construction_handshake["resumeReady"] is True
            assert daemon._native_construction_reservation["reservationId"] == reservation_id
        receipt = daemon.notify_native_measurement_failed(identity, "duplicate callback cleanup")
        assert receipt.reason == "duplicate callback cleanup"
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_expiry_flushes_before_direct_bootstrap_admission(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-expiring", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        daemon._native_construction_reservation["expiresMonotonic"] = daemon._native_clock() - 1.0
        with pytest.raises(DriverError, match="expired"):
            daemon.native_construction_bootstrap(
                expected_generation=7,
                reservation_id=prepared["reservation"]["reservationId"],
                expected_constructor_boot=prepared["reservation"]["expectedConstructorBoot"],
            )
        assert daemon._native_construction_expired is False
        assert daemon._native_construction_reservation is None
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["phase"] == "recovery_required"
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_terminal_receipts_are_bounded_and_immutable(tmp_path: Path) -> None:
    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        with daemon._native_construction_lock:
            for index in range(daemon._NATIVE_CONSTRUCTION_TERMINAL_LIMIT + 4):
                daemon._native_construction_state_history = [{"state": "error", "nativeClock": float(index)}]
                daemon._native_construction_record_terminal_locked(
                    {
                        "reservationId": f"terminal-{index}",
                        "reservationKind": "recovery",
                        "reservationState": "recovery",
                    },
                    state="error",
                    failure="bounded fixture",
                )
            assert len(daemon._native_construction_terminals) == daemon._NATIVE_CONSTRUCTION_TERMINAL_LIMIT
            assert "terminal-0" not in daemon._native_construction_terminals
            assert "terminal-4" in daemon._native_construction_terminals
            first = daemon._native_construction_terminal_reply("terminal-4")
            assert first is not None
            first["reservation"]["reservationState"] = "mutated"
            assert (
                daemon._native_construction_terminals["terminal-4"]["reservation"]["reservationState"]
                == "error"
            )
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_reservation_blocks_competing_input_race(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        daemon.native_construction_prepare(expected_generation=7)
        assert daemon._native_construction_blocks_competing_entry() is True
        with pytest.raises(DriverError, match="construction admission"):
            daemon.start_calibration("input", expected_generation=7)
        response = SessionIPC(daemon.controller, daemon).dispatch(
            {"command": "interaction_start", "generation": 7}
        )
        assert response == {"ok": False, "error": "native construction admission is active"}
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_construction_control_preflight_is_recovery_only(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        preflight = daemon.native_construction_control_preflight(expected_generation=7)
        reservation = preflight["reservation"]
        assert reservation["reservationKind"] == "preflight"
        with pytest.raises(DriverError, match="control preflight"):
            daemon.native_construction_bootstrap(
                expected_generation=7,
                reservation_id=reservation["reservationId"],
                expected_constructor_boot=reservation["expectedConstructorBoot"],
            )
        with pytest.raises(DriverError, match="unknown"):
            daemon.native_construction_recovery_cancel(
                expected_generation=7, reservation_id=reservation["reservationId"]
            )
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["B0"] == "boot-a"
        assert checkpoint["phase"] == "recovery_required"
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_paused_preflight_and_recovery_use_configured_png_capture(
    tmp_path: Path,
) -> None:
    """Exercise the public PAUSED IPC path through the configured producer gate."""

    from .test_session_runtime import _snapshot

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    try:
        producer = tmp_path / "configured-region-producer"
        producer.write_bytes(b"configured producer fixture")
        configured_profile = replace(
            driver.profile,
            telemetry_region_executable=producer,
            telemetry_region_executable_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
        )
        driver.profile = configured_profile
        daemon.config = replace(daemon.config, profile=configured_profile)
        driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
        calls = [0]
        expected_identity = identity

        class AdmittedProcess:
            def __init__(self) -> None:
                self.process = SimpleNamespace(pid=os.getpid())

            def release_admission_gate(self) -> None:
                return None

        def capture(
            path: Path,
            *,
            identity: WindowIdentity,
            rectangle: tuple[int, int, int, int],
            observer_admission: Callable[[Callable[[], object]], object] | None = None,
            observer_reaped: Callable[[int], None] | None = None,
            **_kwargs: object,
        ) -> WindowIdentity:
            assert identity.fingerprint == expected_identity.fingerprint
            assert rectangle == (
                expected_identity.child_geometry[0] + configured_profile.canvas_crop[0],
                expected_identity.child_geometry[1] + configured_profile.canvas_crop[1],
                256,
                256,
            )
            assert observer_admission is not None
            assert observer_reaped is not None
            admitted = observer_admission(lambda: AdmittedProcess())
            observer_reaped(admitted.process.pid)  # type: ignore[union-attr]
            calls[0] += 1
            if calls[0] == 1:
                boot = "boot-a"
            elif calls[0] == 2:
                boot = "native-construction:boot-a"
            else:
                boot = "ordinary-new"
            snapshot = _snapshot(1000 + calls[0], boot=boot, sequence=calls[0])
            if calls[0] == 2:
                snapshot["render"]["nativePhase"] = "held"
                snapshot["render"]["nativeOrdinal"] = 100
            frame = encode_frame(snapshot)
            _status_image(frame).save(path)
            return identity

        driver.backend.capture_native_region = capture  # type: ignore[method-assign]
        ipc = SessionIPC(daemon.controller, daemon)
        prepared = ipc.dispatch({"command": "native_construction_control_preflight", "generation": 7})
        assert prepared["ok"] is True, prepared
        reservation_id = prepared["reservation"]["reservationId"]
        started = ipc.dispatch(
            {
                "command": "native_construction_recovery_begin",
                "generation": 7,
                "reservationId": reservation_id,
            }
        )
        assert started["ok"] is True
        recovered = ipc.dispatch(
            {
                "command": "native_construction_recovery_complete",
                "generation": 7,
                "reservationId": started["reservation"]["reservationId"],
            }
        )
        assert recovered["ok"] is True
        assert recovered["handshake"]["state"] == "recovered"
        assert calls[0] == 3
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_paused_preflight_without_producer_refuses_before_stock_capture(
    tmp_path: Path,
) -> None:
    daemon, driver, _identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    stock_calls: list[object] = []
    process_calls: list[object] = []
    try:
        driver.capture_client_crop_for_identity = (  # type: ignore[method-assign]
            lambda *args, **kwargs: stock_calls.append((args, kwargs))
        )
        driver.backend.capture_native_region = (  # type: ignore[method-assign]
            lambda *args, **kwargs: process_calls.append((args, kwargs))
        )
        reply = SessionIPC(daemon.controller, daemon).dispatch(
            {"command": "native_construction_control_preflight", "generation": 7}
        )
        assert reply["ok"] is False
        assert "configured telemetry producer" in reply["error"]
        assert stock_calls == []
        assert process_calls == []
        assert daemon._native_construction_reservation is None
        assert daemon.controller.native_construction_checkpoint() is None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_paused_recovery_begin_without_producer_refuses_before_capture(
    tmp_path: Path,
) -> None:
    daemon, driver, _identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    stock_calls: list[object] = []
    process_calls: list[object] = []
    try:
        unconfigured_profile = replace(
            driver.profile,
            telemetry_region_executable=None,
            telemetry_region_executable_sha256=None,
        )
        driver.profile = unconfigured_profile
        daemon.config = replace(daemon.config, profile=unconfigured_profile)
        daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        daemon._native_construction_observation_provider = None
        driver.capture_client_crop_for_identity = (  # type: ignore[method-assign]
            lambda *args, **kwargs: stock_calls.append((args, kwargs))
        )
        driver.backend.capture_native_region = (  # type: ignore[method-assign]
            lambda *args, **kwargs: process_calls.append((args, kwargs))
        )
        reply = SessionIPC(daemon.controller, daemon).dispatch(
            {"command": "native_construction_recovery_begin", "generation": 7, "reservationId": ""}
        )
        assert reply["ok"] is False
        assert "configured telemetry producer" in reply["error"]
        assert stock_calls == []
        assert process_calls == []
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None and checkpoint["phase"] == "prepared"
        assert daemon._native_construction_reservation is None
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def _r9_configure_png_capture(
    daemon: SessionDaemon,
    driver: DesktopDriver,
    identity: WindowIdentity,
    tmp_path: Path,
    *,
    admission_wait: threading.Event | None = None,
) -> list[object]:
    """Bind a configured PNG path while retaining the real Guardian gate."""

    from .test_session_runtime import _snapshot

    tmp_path.mkdir(parents=True, exist_ok=True)
    producer = tmp_path / "r9-configured-region-producer"
    producer.write_bytes(b"configured producer fixture")
    configured_profile = replace(
        driver.profile,
        telemetry_region_executable=producer,
        telemetry_region_executable_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
    )
    driver.profile = configured_profile
    daemon.config = replace(daemon.config, profile=configured_profile)
    process_calls: list[object] = []
    expected_identity = identity

    class AdmittedProcess:
        def __init__(self) -> None:
            self.process = SimpleNamespace(pid=os.getpid())

        def release_admission_gate(self) -> None:
            return None

    def capture(
        path: Path,
        *,
        identity: WindowIdentity,
        observer_admission: Callable[[Callable[[], object]], object] | None = None,
        observer_reaped: Callable[[int], None] | None = None,
        **_kwargs: object,
    ) -> WindowIdentity:
        assert identity.fingerprint == expected_identity.fingerprint
        assert observer_admission is not None
        assert observer_reaped is not None

        def start_process() -> object:
            process_calls.append(object())
            return AdmittedProcess()

        if admission_wait is not None:
            assert admission_wait.wait(3.0)
        admitted = observer_admission(start_process)
        observer_reaped(admitted.process.pid)  # type: ignore[union-attr]
        _status_image(encode_frame(_snapshot(1001, boot="boot-a", sequence=1))).save(path)
        return identity

    driver.backend.capture_native_region = capture  # type: ignore[method-assign]
    return process_calls


@pytest.mark.parametrize("revocation_wins", [False, True])
def test_native_capability_admission_and_owner_clear_linearize(tmp_path: Path, revocation_wins: bool) -> None:
    """A public owner and real Guardian gate have one capability transaction."""

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    release_match = threading.Event()
    first_match = threading.Event()
    second_match = threading.Event()
    owner_cleared = threading.Event()
    capture_done = threading.Event()
    process_calls: list[object] = []
    capture_errors: list[BaseException] = []
    revoke_errors: list[BaseException] = []
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        process_calls = _r9_configure_png_capture(
            daemon,
            driver,
            identity,
            tmp_path,
            admission_wait=owner_cleared if revocation_wins else None,
        )
        prepared = daemon.native_construction_control_preflight(expected_generation=7)
        reservation = prepared["reservation"]
        reservation_id = reservation["reservationId"]
        with daemon._native_construction_lock:
            capability = daemon._native_construction_capability_locked()
        assert capability is not None
        original_matches = daemon._native_construction_capability_matches
        match_count = [0]

        def gated_matches(candidate: dict[str, object]) -> bool:
            result = original_matches(candidate)
            match_count[0] += 1
            if match_count[0] == 1:
                first_match.set()
                if revocation_wins and not release_match.wait(3.0):
                    raise DriverError("capability first-match barrier did not release")
            elif match_count[0] == 2:
                second_match.set()
                if not revocation_wins and not release_match.wait(3.0):
                    raise DriverError("capability admission barrier did not release")
            return result

        daemon._native_construction_capability_matches = gated_matches  # type: ignore[method-assign]
        initial_epoch = int(capability["epoch"])

        def monitor_owner_clear() -> None:
            while not owner_cleared.is_set():
                if daemon._native_construction_lock.acquire(blocking=False):
                    try:
                        if (
                            daemon._native_construction_reservation is None
                            and daemon._native_construction_epoch != initial_epoch
                        ):
                            owner_cleared.set()
                    finally:
                        daemon._native_construction_lock.release()
                if not owner_cleared.is_set():
                    time.sleep(0.001)

        monitor = threading.Thread(target=monitor_owner_clear, name="r9-owner-clear-monitor")
        monitor.start()

        def capture() -> None:
            try:
                daemon._capture_native_region_for_identity(
                    identity,
                    expected_generation=7,
                    expected_state=ControllerState.PAUSED,
                    deadline=time.monotonic() + 5.0,
                    native_capability=capability,
                )
            except BaseException as exc:
                capture_errors.append(exc)
            finally:
                capture_done.set()

        capture_thread = threading.Thread(target=capture, name="r9-capability-capture")
        capture_thread.start()
        if revocation_wins:
            assert first_match.wait(2.0)
        else:
            assert second_match.wait(2.0)

        def replay_bootstrap() -> None:
            try:
                daemon.native_construction_bootstrap(
                    expected_generation=7,
                    reservation_id=reservation_id,
                    expected_constructor_boot=reservation["expectedConstructorBoot"],
                )
            except BaseException as exc:
                revoke_errors.append(exc)

        revoke_thread = threading.Thread(target=replay_bootstrap, name="r9-capability-replay")
        revoke_thread.start()
        time.sleep(0.05)
        assert revoke_thread.is_alive()
        assert process_calls == []
        if revocation_wins:
            release_match.set()
            assert owner_cleared.wait(2.0)
            assert capture_done.wait(3.0)
            assert process_calls == []
        else:
            assert not owner_cleared.wait(0.1)
            release_match.set()
            assert capture_done.wait(3.0)
            assert len(process_calls) == 1
            assert owner_cleared.wait(2.0)
        capture_thread.join(timeout=3.0)
        revoke_thread.join(timeout=3.0)
        monitor.join(timeout=3.0)
        assert not capture_thread.is_alive()
        assert not revoke_thread.is_alive()
        assert not monitor.is_alive()
        assert revoke_errors and isinstance(revoke_errors[0], DriverError)
        assert "control preflight" in str(revoke_errors[0])
        if revocation_wins:
            assert capture_errors and isinstance(capture_errors[0], DriverError)
            assert "capability" in str(capture_errors[0])
        else:
            assert capture_errors == []
        assert owner_cleared.is_set()
    finally:
        release_match.set()
        if daemon._native_construction_reservation is not None:
            try:
                daemon.native_construction_recovery_cancel(
                    expected_generation=7, reservation_id=reservation_id
                )
            except (DriverError, UnboundLocalError):
                pass
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_capability_matches_exact_owner_facts_and_inflight_none_id(tmp_path: Path) -> None:
    """Capability matching rejects stale/replaced owners and accepts exact inflight None."""

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    second_daemon: SessionDaemon | None = None
    second_driver: DesktopDriver | None = None
    second_child: subprocess.Popen[str] | None = None
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        process_calls = _r9_configure_png_capture(daemon, driver, identity, tmp_path)
        prepared = daemon.native_construction_control_preflight(expected_generation=7)
        installed = prepared["reservation"]
        negative_capabilities: list[dict[str, object]] = []
        with daemon._native_construction_lock:
            capability = daemon._native_construction_capability_locked()
            assert capability is not None
            assert daemon._native_construction_capability_matches(capability)
            negative_capabilities = [
                {**capability, "kind": "measure"},
                {**capability, "reservationId": None},
                {**capability, "epoch": int(capability["epoch"]) + 1},
                {
                    **capability,
                    "guardianGeneration": int(capability["guardianGeneration"]) + 1,
                },
            ]
            assert all(
                not daemon._native_construction_capability_matches(candidate)
                for candidate in negative_capabilities
            )
        for candidate in negative_capabilities:
            with pytest.raises(DriverError, match="live recovery capability"):
                daemon._capture_native_region_for_identity(
                    identity,
                    expected_generation=7,
                    expected_state=ControllerState.PAUSED,
                    deadline=time.monotonic() + 3.0,
                    native_capability=candidate,
                )
            assert process_calls == []
        with pytest.raises(DriverError, match="control preflight"):
            daemon.native_construction_bootstrap(
                expected_generation=7,
                reservation_id=installed["reservationId"],
                expected_constructor_boot=installed["expectedConstructorBoot"],
            )

        second_daemon, second_driver, second_identity, second_child = _construction_daemon(
            tmp_path / "replaced-owner", state=ControllerState.PAUSED
        )
        second_daemon.controller.set_native_construction_checkpoint(
            {
                "schema": 1,
                "B0": "boot-a",
                "expectedConstructorBoot": "native-construction:boot-a",
                "ownerGeneration": 7,
                "kind": "measure",
                "phase": "prepared",
            },
            expected_generation=7,
            expected_state=ControllerState.PAUSED,
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked_observation() -> dict[str, object]:
            entered.set()
            assert release.wait(3.0)
            return {
                "surface": "absent",
                "identityFingerprint": second_identity.fingerprint,
                "captureIdentity": second_identity.fingerprint,
                "captureMonotonic": 1.0,
            }

        second_daemon._native_construction_observation_provider = blocked_observation
        begin_result: list[object] = []

        def begin_first() -> None:
            try:
                begin_result.append(
                    second_daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
                )
            except BaseException as exc:
                begin_result.append(exc)

        first_thread = threading.Thread(target=begin_first, name="r9-inflight-capability")
        first_thread.start()
        assert entered.wait(2.0)
        with second_daemon._native_construction_lock:
            inflight = second_daemon._native_construction_capability_locked()
            assert inflight is not None and inflight["reservationId"] is None
            assert inflight["guardianGeneration"] == second_driver.guardian.generation
            assert second_daemon._native_construction_capability_matches(inflight)
        release.set()
        first_thread.join(timeout=3.0)
        assert not first_thread.is_alive()
        assert begin_result and isinstance(begin_result[0], dict)
        old_epoch = int(inflight["epoch"])
        old_id = inflight["reservationId"]
        first_reservation_id = begin_result[0]["reservation"]["reservationId"]
        second_daemon.native_construction_recovery_cancel(
            expected_generation=7, reservation_id=first_reservation_id
        )
        second_daemon._native_construction_observation_provider = lambda: {
            "surface": "absent",
            "identityFingerprint": second_identity.fingerprint,
            "captureIdentity": second_identity.fingerprint,
            "captureMonotonic": 2.0,
        }
        second = second_daemon.native_construction_recovery_begin(expected_generation=7, reservation_id="")
        assert second["reservation"]["reservationId"] != old_id
        with second_daemon._native_construction_lock:
            assert not second_daemon._native_construction_capability_matches(inflight)
            current = second_daemon._native_construction_capability_locked()
            assert current is not None
            assert int(current["epoch"]) != old_epoch
            assert second_daemon._native_construction_capability_matches(current)
        replaced_process_calls = _r9_configure_png_capture(
            second_daemon, second_driver, second_identity, tmp_path / "replaced-owner-capture"
        )
        with pytest.raises(DriverError, match="live recovery capability"):
            second_daemon._capture_native_region_for_identity(
                second_identity,
                expected_generation=7,
                expected_state=ControllerState.PAUSED,
                deadline=time.monotonic() + 3.0,
                native_capability=inflight,
            )
        assert replaced_process_calls == []
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)
        if second_driver is not None:
            second_driver.close()
        if second_child is not None:
            second_child.terminate()
            second_child.wait(timeout=3)


@pytest.mark.parametrize("revoke_first", [True, False])
def test_native_guardian_generation_fence_covers_real_child_admission(
    tmp_path: Path, revoke_first: bool
) -> None:
    """Exercise outer capability refusal and cleanup for an admitted child.

    The compiled producer cases establish the Guardian-generation fence; this
    supplementary child fixture covers the outer capability refusal path and
    confirms that an admitted child is cleaned up in either race order.
    """

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    producer = Path("/bin/true")
    configured_profile = replace(
        driver.profile,
        telemetry_region_executable=producer,
        telemetry_region_executable_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
    )
    driver.profile = configured_profile
    daemon.config = replace(daemon.config, profile=configured_profile)
    # _native_driver intentionally constructs an isolated backend without a
    # live display.  Populate the real release bookkeeping used when the
    # Guardian revokes an admitted observer.
    driver.backend._active_processes = set()
    driver.backend._active_process_groups = set()
    driver.backend._process_lock = threading.RLock()
    driver.backend._capture_revocation_epoch = 0
    driver.backend._cancel_active = threading.Event()
    driver.backend._process_generations = {}
    driver.backend._held_keys = set()
    driver.backend._held_buttons = set()
    started: list[subprocess.Popen[str]] = []
    admitted = threading.Event()
    release_capture = threading.Event()
    capture_errors: list[BaseException] = []
    try:
        with daemon._native_construction_lock:
            daemon._native_construction_epoch += 1
            epoch = daemon._native_construction_epoch
            daemon._native_construction_inflight = {
                "epoch": epoch,
                "generation": 7,
                "guardianGeneration": driver.guardian.generation,
                "kind": "preflight",
            }
            capability = daemon._native_construction_capability_locked()
        assert capability is not None
        backend_epoch = getattr(driver.backend, "_capture_revocation_epoch", 0)

        def capture(
            _identity: WindowIdentity,
            *,
            destination: Path | None = None,
            observer_admission: Callable[[Callable[[], object]], object] | None = None,
            observer_reaped: Callable[[int], None] | None = None,
            **_kwargs: object,
        ) -> Path:
            assert observer_admission is not None and observer_reaped is not None

            def start_process() -> object:
                process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
                started.append(process)
                return SimpleNamespace(process=process, release_admission_gate=lambda: None)

            admitted_process = observer_admission(start_process)
            admitted.set()
            assert release_capture.wait(3.0)
            process = getattr(admitted_process, "process", None)
            if process is None:
                process = getattr(getattr(admitted_process, "observer", None), "process", None)
            assert process is not None
            process.terminate()
            process.wait(timeout=3.0)
            observer_reaped(process.pid)
            output = destination or (tmp_path / "real-child-capture.png")
            output.write_bytes(b"capture")
            return output

        driver.capture_native_region_for_identity = capture  # type: ignore[method-assign]

        if revoke_first:
            # Hold the controller mutex so capture blocks at its outer
            # controller admission. Revoke there so the outer PAUSED capability
            # check refuses it before any observer admission.
            controller_ready = threading.Event()

            def blocked_capture() -> None:
                try:
                    daemon._capture_native_region_for_identity(
                        identity,
                        expected_generation=7,
                        expected_state=ControllerState.PAUSED,
                        deadline=time.monotonic() + 5.0,
                        native_capability=capability,
                    )
                except BaseException as exc:
                    capture_errors.append(exc)
                finally:
                    controller_ready.set()

            with daemon.controller.lock:
                thread = threading.Thread(target=blocked_capture, name="guardian-generation-revoke-first")
                thread.start()
                time.sleep(0.05)
                revoker = threading.Thread(
                    target=lambda: driver.guardian.revoke("native generation race"),
                    name="guardian-generation-revoker",
                )
                revoker.start()
                assert _wait_for(lambda: driver.guardian.generation != capability["guardianGeneration"])
            revoker.join(timeout=3.0)
            thread.join(timeout=3.0)
            assert not thread.is_alive() and not revoker.is_alive()
            assert capture_errors and "capability" in str(capture_errors[0]).lower()
            assert started == []
        else:
            thread = threading.Thread(
                target=lambda: daemon._capture_native_region_for_identity(
                    identity,
                    expected_generation=7,
                    expected_state=ControllerState.PAUSED,
                    deadline=time.monotonic() + 5.0,
                    native_capability=capability,
                ),
                name="guardian-generation-admission-first",
            )
            thread.start()
            assert admitted.wait(3.0)
            assert len(started) == 1 and started[0].poll() is None
            driver.guardian.revoke("native generation after admission")
            release_capture.set()
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert driver.guardian.observer_process_groups == frozenset()
            assert driver.guardian.active_process_groups == frozenset()
            assert all(process.poll() is not None for process in started)
            with pytest.raises(DriverError, match="capability"):
                daemon._capture_native_region_for_identity(
                    identity,
                    expected_generation=7,
                    expected_state=ControllerState.PAUSED,
                    deadline=time.monotonic() + 5.0,
                    native_capability=capability,
                )
            assert len(started) == 1
        if revoke_first:
            assert getattr(driver.backend, "_capture_revocation_epoch", 0) == backend_epoch
        else:
            assert getattr(driver.backend, "_capture_revocation_epoch", 0) > backend_epoch
    finally:
        release_capture.set()
        for process in started:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_paused_preflight_cancel_before_producer_admission_dispatches_no_process(
    tmp_path: Path,
) -> None:
    """A revoked recovery admission cannot cross the producer gate."""

    from .test_session_runtime import _snapshot

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    release = threading.Event()
    try:
        producer = tmp_path / "configured-region-producer"
        producer.write_bytes(b"configured producer fixture")
        configured_profile = replace(
            driver.profile,
            telemetry_region_executable=producer,
            telemetry_region_executable_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
        )
        driver.profile = configured_profile
        daemon.config = replace(daemon.config, profile=configured_profile)
        driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
        entered = threading.Event()
        process_calls: list[object] = []
        capture_errors: list[BaseException] = []
        setup_complete = False

        class AdmittedProcess:
            def __init__(self) -> None:
                self.process = SimpleNamespace(pid=os.getpid())

            def release_admission_gate(self) -> None:
                return None

        def capture(
            path: Path,
            *,
            identity: WindowIdentity,
            observer_reaped: Callable[[int], None] | None = None,
            observer_admission: Callable[[Callable[[], object]], object] | None = None,
            **_kwargs: object,
        ) -> WindowIdentity:
            assert observer_admission is not None

            def start_process() -> object:
                process_calls.append(object())
                return AdmittedProcess()

            nonlocal setup_complete
            if not setup_complete:
                admitted = observer_admission(start_process)
                if observer_reaped is not None:
                    observer_reaped(admitted.process.pid)  # type: ignore[union-attr]
                _status_image(encode_frame(_snapshot(1001, boot="boot-a", sequence=1))).save(path)
                setup_complete = True
                return identity

            entered.set()
            assert release.wait(3.0)
            try:
                observer_admission(start_process)
            except BaseException as exc:
                capture_errors.append(exc)
                raise
            raise AssertionError("revoked producer admission unexpectedly started")

        driver.backend.capture_native_region = capture  # type: ignore[method-assign]
        ipc = SessionIPC(daemon.controller, daemon)
        prepared = ipc.dispatch({"command": "native_construction_control_preflight", "generation": 7})
        assert prepared["ok"] is True, prepared
        reservation_id = prepared["reservation"]["reservationId"]
        process_calls.clear()
        recovery_response: list[dict[str, object]] = []

        def recovery_begin() -> None:
            recovery_response.append(
                ipc.dispatch(
                    {
                        "command": "native_construction_recovery_begin",
                        "generation": 7,
                        "reservationId": reservation_id,
                    }
                )
            )

        worker = threading.Thread(target=recovery_begin, name="native-recovery-cancel")
        worker.start()
        assert entered.wait(3.0)
        cancelled = ipc.dispatch(
            {
                "command": "native_construction_recovery_cancel",
                "generation": 7,
                "reservationId": reservation_id,
            }
        )
        assert cancelled["ok"] is True, cancelled
        assert cancelled["handshake"]["state"] == "cancelled"
        release.set()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
        assert recovery_response and recovery_response[0]["ok"] is False
        assert capture_errors and isinstance(capture_errors[0], DriverError)
        assert process_calls == []
        assert daemon._native_construction_reservation is None
        checkpoint = daemon.controller.native_construction_checkpoint()
        assert checkpoint is not None
        assert checkpoint["phase"] == "recovery_required"
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_poll_validation_releases_construction_lock_before_pause_callback(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=prepared["reservation"]["reservationId"],
            expected_constructor_boot=prepared["reservation"]["expectedConstructorBoot"],
        )
        original_validate = daemon._native_construction_validate_current_locked

        def gated_validate(*args: object, **kwargs: object) -> WindowIdentity:
            entered.set()
            if not release.wait(3.0):
                raise DriverError("poll validation fixture did not release")
            return original_validate(*args, **kwargs)

        daemon._native_construction_validate_current_locked = gated_validate  # type: ignore[method-assign]
        poll_errors: list[BaseException] = []

        def poll() -> None:
            try:
                daemon._check_native_poll_identity(identity, 7)
            except BaseException as exc:
                poll_errors.append(exc)

        poll_thread = threading.Thread(target=poll, name="native-poll-validation")
        poll_thread.start()
        assert entered.wait(2.0)
        pause_result: list[object] = []

        def pause() -> None:
            pause_result.append(daemon.controller.pause("poll validation race"))

        pause_thread = threading.Thread(target=pause, name="native-poll-pause")
        pause_thread.start()
        pause_thread.join(timeout=2.0)
        assert not pause_thread.is_alive(), "pause callback waited on construction validation"
        release.set()
        poll_thread.join(timeout=3.0)
        assert not poll_thread.is_alive()
        assert pause_result
        assert poll_errors and isinstance(poll_errors[0], DriverError)
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_deferred_start_seals_owner_before_pause_and_never_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    seen_contexts: list[dict[str, object] | None] = []
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )

        def blocked_runner(
            *,
            expected_generation: int | None = None,
            admitted_context: dict[str, object] | None = None,
        ) -> dict[str, object]:
            del expected_generation
            seen_contexts.append(copy.deepcopy(admitted_context))
            entered.set()
            release.wait(3.0)
            raise DriverError("deferred worker was revoked")

        daemon.run_native_calibration = blocked_runner  # type: ignore[method-assign]
        daemon.start_calibration("native", expected_generation=7)
        assert entered.wait(2.0)
        paused = daemon.controller.pause("deferred start pause")
        assert paused.state is ControllerState.PAUSED
        release.set()
        thread = daemon._calibration_thread
        assert thread is not None
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert seen_contexts and seen_contexts[0] is not None
        assert seen_contexts[0]["deferred"] is True
        assert daemon._native_construction_reservation is None
        assert daemon._calibration_operation["state"] == "error"
    finally:
        release.set()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_worker_prepare_pause_three_way_lock_barrier(tmp_path: Path) -> None:
    """Exercise the real worker/status, prepare, and pause lock cycle.

    The worker is allowed to finish its runner while the calibration-state
    mutex is held.  At the same time prepare owns the construction mutex and
    waits for that state mutex, while pause owns the controller mutex and is
    synchronously waiting for the construction revocation callback.  The
    worker's status snapshot must be taken before it enters the state mutex;
    otherwise this bounded barrier reproduces the historical inversion.
    """

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    runner_entered = threading.Event()
    allow_runner_return = threading.Event()
    worker_status_entered = threading.Event()
    prepare_done = threading.Event()
    pause_done = threading.Event()
    prepare_errors: list[BaseException] = []
    pause_errors: list[BaseException] = []

    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )

        def blocked_runner(**_kwargs: object) -> dict[str, object]:
            runner_entered.set()
            if not allow_runner_return.wait(3.0):
                raise DriverError("worker barrier did not release")
            return {"calibration": "native"}

        daemon.run_native_calibration = blocked_runner  # type: ignore[method-assign]
        daemon.start_calibration("native", expected_generation=7)
        assert runner_entered.wait(2.0)

        original_status = daemon.status_dict

        def observed_status() -> dict[str, object]:
            worker_status_entered.set()
            return original_status()

        daemon.status_dict = observed_status  # type: ignore[method-assign]

        def prepare() -> None:
            try:
                daemon.native_construction_prepare(expected_generation=7)
            except BaseException as exc:
                prepare_errors.append(exc)
            finally:
                prepare_done.set()

        def pause() -> None:
            try:
                daemon.controller.pause("three-way native barrier")
            except BaseException as exc:
                pause_errors.append(exc)
            finally:
                pause_done.set()

        # Holding the state mutex makes prepare stop at its real
        # construction->calibration edge.  It also keeps the worker's status
        # snapshot at the exact point where the old worker acquired the state
        # lock before asking for controller status.
        with daemon._calibration_state_lock:
            prepare_thread = threading.Thread(target=prepare, name="native-three-way-prepare")
            prepare_thread.start()
            assert _wait_for(lambda: _native_inflight_exists(daemon), timeout=2.0), (
                "prepare never reached the construction admission interval"
            )

            pause_thread = threading.Thread(target=pause, name="native-three-way-pause")
            pause_thread.start()
            assert _wait_for(
                lambda: _controller_pause_reached(daemon),
                timeout=2.0,
            ), "pause never reached its controller-owned state"
            # The callback is synchronous under controller.pause; because
            # prepare owns construction, pause remains alive at this point.
            assert pause_thread.is_alive(), "pause callback did not contend for construction"

            allow_runner_return.set()
            assert worker_status_entered.wait(2.0), (
                "worker did not publish status before taking the calibration-state lock"
            )

        allow_runner_return.set()
        prepare_thread.join(timeout=3.0)
        pause_thread.join(timeout=3.0)
        assert not prepare_thread.is_alive(), "prepare remained blocked after lock release"
        assert not pause_thread.is_alive(), "pause callback remained blocked after lock release"
        assert prepare_done.is_set() and pause_done.is_set()
        assert len(prepare_errors) == 1
        assert isinstance(prepare_errors[0], DriverError)
        assert str(prepare_errors[0]) == "native calibration is already running"
        assert not pause_errors, pause_errors
        worker_thread = daemon._calibration_thread
        if worker_thread is not None:
            worker_thread.join(timeout=3.0)
        assert worker_thread is None or not worker_thread.is_alive()
    finally:
        allow_runner_return.set()
        thread = daemon._calibration_thread
        if thread is not None:
            thread.join(timeout=3.0)
            assert not thread.is_alive()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def _native_inflight_exists(daemon: SessionDaemon) -> bool:
    if not daemon._native_construction_lock.acquire(blocking=False):
        # A non-acquirable construction lock is itself evidence that prepare
        # crossed the construction->calibration edge and is waiting on the
        # state mutex held by this fixture.
        return True
    try:
        return isinstance(daemon._native_construction_inflight, dict)
    finally:
        daemon._native_construction_lock.release()


def _controller_pause_reached(daemon: SessionDaemon) -> bool:
    if not daemon.controller.lock.acquire(blocking=False):
        # Controller.pause owns this mutex while its Guardian revocation
        # callback waits for the construction lock.
        return True
    try:
        return daemon.controller.status().state is ControllerState.PAUSED
    finally:
        daemon.controller.lock.release()


@pytest.mark.parametrize("old_outcome", ["complete", "error"])
def test_completed_worker_cannot_publish_into_next_operation_record(tmp_path: Path, old_outcome: str) -> None:
    """A late result/error/finally from one worker cannot touch its successor."""

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    old_entered = threading.Event()
    old_finished = threading.Event()
    old_release = threading.Event()
    old_post_finish_release = threading.Event()
    new_entered = threading.Event()
    new_release = threading.Event()
    try:

        def old_runner(
            *, expected_generation: int | None = None, admitted_context: dict[str, object] | None = None
        ) -> dict[str, object]:
            daemon._begin_calibration("native", expected_generation, admitted_context=admitted_context)
            old_entered.set()
            if not old_release.wait(3.0):
                raise DriverError("old worker fixture did not release")
            daemon._finish_calibration(
                state=old_outcome,
                error="old worker error" if old_outcome == "error" else "",
            )
            old_finished.set()
            if not old_post_finish_release.wait(3.0):
                raise DriverError("old worker post-finish fixture did not release")
            if old_outcome == "error":
                raise DriverError("late old worker failure")
            return {"owner": "old"}

        def new_runner(
            *,
            expected_generation: int | None = None,
            admitted_context: dict[str, object] | None = None,
        ) -> dict[str, object]:
            daemon._begin_calibration("native", expected_generation, admitted_context=admitted_context)
            new_entered.set()
            if not new_release.wait(3.0):
                raise DriverError("new worker fixture did not release")
            daemon._finish_calibration(state="complete")
            return {"owner": "new"}

        native_calls = [0]

        def native_runner(**kwargs: object) -> dict[str, object]:
            native_calls[0] += 1
            if native_calls[0] == 1:
                return old_runner(**kwargs)  # type: ignore[arg-type]
            return new_runner(**kwargs)  # type: ignore[arg-type]

        daemon.run_native_calibration = native_runner  # type: ignore[method-assign]
        daemon.start_calibration("native", expected_generation=7)
        assert old_entered.wait(2.0)
        old_thread = daemon._calibration_thread
        assert old_thread is not None
        old_release.set()
        assert old_finished.wait(2.0)

        daemon.start_calibration("native", expected_generation=7)
        assert new_entered.wait(2.0)
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["kind"] == "native"
            assert current["state"] == "running"
            assert current["result"] is None
            assert current["error"] == ""

        # Let the old worker publish its result or error and run finally while
        # the new operation record is current.  Identity checks must keep all
        # three writes on the old record.
        old_post_finish_release.set()
        old_thread.join(timeout=3.0)
        assert not old_thread.is_alive()
        # The old worker has now published its result/error and run its
        # identity-guarded finally.  The successor record must still be
        # untouched before its own worker is released.
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["kind"] == "native"
            assert current["state"] == "running"
            assert current["result"] is None
            assert current["error"] == ""
        new_release.set()
        new_thread = daemon._calibration_thread
        assert new_thread is not None
        new_thread.join(timeout=3.0)
        assert not new_thread.is_alive()
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["kind"] == "native"
            assert current["state"] == "complete"
            assert current["error"] == ""
            assert current["result"] is not None
            assert current["result"]["calibrationOperation"]["kind"] == "native"  # type: ignore[index]
    finally:
        old_release.set()
        old_post_finish_release.set()
        new_release.set()
        thread = daemon._calibration_thread
        if thread is not None:
            thread.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_completed_worker_cannot_flip_next_starting_operation_record(
    tmp_path: Path,
) -> None:
    """A stale worker's finally cannot turn a newly published starting record into error."""

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    old_release = threading.Event()
    old_finished = threading.Event()
    old_post_finish_release = threading.Event()
    new_worker_entered = threading.Event()
    new_begin_release = threading.Event()
    new_entered = threading.Event()
    new_release = threading.Event()
    try:

        def old_runner(
            *, expected_generation: int | None = None, admitted_context: dict[str, object] | None = None
        ) -> dict[str, object]:
            daemon._begin_calibration("native", expected_generation, admitted_context=admitted_context)
            if not old_release.wait(3.0):
                raise DriverError("old worker fixture did not release")
            daemon._finish_calibration(state="complete")
            old_finished.set()
            if not old_post_finish_release.wait(3.0):
                raise DriverError("old worker post-finish fixture did not release")
            raise DriverError("late old worker failure")

        def new_runner(
            *, expected_generation: int | None = None, admitted_context: dict[str, object] | None = None
        ) -> dict[str, object]:
            new_worker_entered.set()
            if not new_begin_release.wait(3.0):
                raise DriverError("new worker start fixture did not release")
            daemon._begin_calibration("native", expected_generation, admitted_context=admitted_context)
            new_entered.set()
            if not new_release.wait(3.0):
                raise DriverError("new worker fixture did not release")
            daemon._finish_calibration(state="complete")
            return {"owner": "new"}

        native_calls = [0]

        def native_runner(**kwargs: object) -> dict[str, object]:
            native_calls[0] += 1
            if native_calls[0] == 1:
                return old_runner(**kwargs)  # type: ignore[arg-type]
            return new_runner(**kwargs)  # type: ignore[arg-type]

        daemon.run_native_calibration = native_runner  # type: ignore[method-assign]
        daemon.start_calibration("native", expected_generation=7)
        old_thread = daemon._calibration_thread
        assert old_thread is not None
        old_release.set()
        assert old_finished.wait(2.0)

        daemon.start_calibration("native", expected_generation=7)
        assert new_worker_entered.wait(2.0)
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["state"] == "starting"
            assert current["kind"] == "native"
            assert current["result"] is None
            assert current["error"] == ""

        old_post_finish_release.set()
        old_thread.join(timeout=3.0)
        assert not old_thread.is_alive()
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["state"] == "starting"
            assert current["result"] is None
            assert current["error"] == ""

        new_begin_release.set()
        assert new_entered.wait(2.0)
        new_release.set()
        new_thread = daemon._calibration_thread
        assert new_thread is not None
        new_thread.join(timeout=3.0)
        assert not new_thread.is_alive()
        with daemon._calibration_state_lock:
            current = daemon._calibration_operation
            assert current["state"] == "complete"
            assert current["error"] == ""
            assert current["result"] is not None
    finally:
        old_release.set()
        old_post_finish_release.set()
        new_begin_release.set()
        new_release.set()
        thread = daemon._calibration_thread
        if thread is not None:
            thread.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_input_start_owns_record_and_publishes_worker_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public input start reaches its own running record before a controlled sample error."""

    from crowley.session.calibration import InputCalibrationWorkflow

    daemon, driver, _identity, child = _construction_daemon(tmp_path, state=ControllerState.INPUT_CALIBRATION)
    sample_entered = threading.Event()
    release_sample = threading.Event()

    def reject_sample(self: object) -> object:
        del self
        sample_entered.set()
        if not release_sample.wait(10.0):
            raise DriverError("controlled input sample fixture did not release")
        raise DriverError("controlled input sample failure")

    monkeypatch.setattr(InputCalibrationWorkflow, "run", reject_sample)
    monkeypatch.setattr(daemon, "_validate_runtime_paths", lambda: None)
    try:
        started = daemon.start_calibration("input", expected_generation=7)
        assert started["calibrationOperation"]["state"] in {"starting", "running"}, started[
            "calibrationOperation"
        ]  # type: ignore[index]
        assert sample_entered.wait(2.0)
        with daemon._calibration_state_lock:
            assert daemon._calibration_operation["state"] == "running"
            assert daemon._calibration_operation["kind"] == "input"
            assert daemon._calibration_operation["result"] is None
            assert daemon._calibration_operation["error"] == ""
        release_sample.set()
        worker = daemon._calibration_thread
        assert worker is not None
        worker.join(timeout=5.0)
        assert not worker.is_alive()
        with daemon._calibration_state_lock:
            current = copy.deepcopy(daemon._calibration_operation)
        assert current["kind"] == "input"
        assert current["state"] == "error"
        assert current["error"] == "controlled input sample failure"
        assert current["result"] is None
    finally:
        release_sample.set()
        worker = daemon._calibration_thread
        if worker is not None:
            worker.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_input_start_rejects_cross_kind_worker_while_starting(tmp_path: Path) -> None:
    """A real starting input record blocks a competing native worker."""

    daemon, driver, _identity, child = _construction_daemon(tmp_path, state=ControllerState.INPUT_CALIBRATION)
    release_input = threading.Event()
    input_entered = threading.Event()

    def held_input(**_kwargs: object) -> dict[str, object]:
        input_entered.set()
        if not release_input.wait(3.0):
            raise DriverError("held input fixture did not release")
        return {"calibration": "input"}

    daemon.run_input_calibration = held_input  # type: ignore[method-assign]
    try:
        daemon.start_calibration("input", expected_generation=7)
        assert input_entered.wait(2.0)
        with daemon._calibration_state_lock:
            assert daemon._calibration_operation["state"] == "starting"
            assert daemon._calibration_operation["kind"] == "input"
        with pytest.raises(DriverError, match="already starting"):
            daemon._begin_calibration("native", 7)
        release_input.set()
        worker = daemon._calibration_thread
        assert worker is not None
        worker.join(timeout=3.0)
        assert not worker.is_alive()
        with daemon._calibration_state_lock:
            assert daemon._calibration_operation["state"] == "error"
    finally:
        release_input.set()
        worker = daemon._calibration_thread
        if worker is not None:
            worker.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_public_legacy_bootstrap_rejects_live_deferred_owner(tmp_path: Path) -> None:
    daemon, driver, identity, child = _construction_daemon(tmp_path)
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=2
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        with pytest.raises(DriverError, match="blocked by another construction admission"):
            daemon.native_bootstrap(expected_generation=7)
        assert daemon._native_construction_reservation is not None
        assert daemon._native_construction_reservation["reservationId"] == reservation["reservationId"]
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize(
    ("cancel_after_first_final_capture", "binding_drift", "recheck_race"),
    [
        (False, None, False),
        (True, None, False),
        (False, "identity", False),
        (False, "guardian", False),
        (False, None, True),
    ],
)
def test_native_construction_composes_real_png_runner_and_finalize(
    tmp_path: Path,
    cancel_after_first_final_capture: bool,
    binding_drift: str | None,
    recheck_race: bool,
) -> None:
    """Exercise the deferred gate through the production runner callbacks."""

    from crowley.core.watchlist import load_catalog

    from .test_session_runtime import _snapshot

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    daemon.config = replace(daemon.config, plan_hash=load_catalog().scan_plan()["planHash"])
    clock = [time.monotonic()]
    capture_index = 0
    sequence = 0
    worker_result: dict[str, object] = {}
    rebind_entered = threading.Event()
    release_rebind = threading.Event()
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    cancel_attempted = threading.Event()
    cancel_flush_done = threading.Event()
    release_cancel_flush = threading.Event()
    cancel_controller_probe = threading.Event()
    cancel_controller_probe_values: list[bool] = []
    final_controller_entered = threading.Event()
    cancel_response: list[dict[str, object]] = []
    cancel_done = threading.Event()
    cancel_thread: threading.Thread | None = None
    controller_lock_facade: ForwardingControllerLock | None = None
    rebind_calls: list[object] = []
    observe_calls: list[object] = []
    native_calibrated_calls: list[object] = []
    active_identity = [identity]
    final_capture_returned = threading.Event()
    release_final_capture = threading.Event()
    recheck_validation_entered = threading.Event()
    release_recheck_validation = threading.Event()
    recheck_result: dict[str, object] = {}
    recheck_error: list[BaseException] = []
    actor: object | None = None
    actor_listener: socket.socket | None = None
    actor_server: threading.Thread | None = None
    actor_stop = threading.Event()
    actor_errors: list[BaseException] = []
    actor_request_count = 0
    actor_request_lock = threading.Lock()
    recheck_actor_thread_ids: set[int] = set()

    def now() -> float:
        return clock[0]

    def write_strict_capture(
        current_identity: WindowIdentity,
        decoder: PixelDecoder,
        *,
        deadline: float | None = None,
        **_kwargs: object,
    ) -> tuple[object, str, Path]:
        del deadline
        nonlocal capture_index, sequence
        if current_identity.fingerprint != identity.fingerprint:
            raise AssertionError("capture identity changed")
        capture_index += 1
        sequence += 1
        if capture_index == 1:
            boot = "boot-a"
            phase, ordinal, updates = "normal", 0, 1
            clock[0] += 0.1
        elif capture_index == 2:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "normal", 0, 1
            clock[0] += 0.1
        elif capture_index == 3:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "held", 0, 3
            # A legitimate operator wait remains inside the held provider's
            # existing 30-second deadline and pushes the complete envelope
            # beyond the rejected 127-second class-based bound.
            clock[0] += 15.0
        elif 4 <= capture_index <= 102:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "updates", capture_index - 3, 3 + capture_index - 3
            clock[0] += 1.0
        elif capture_index == 103:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "held", 100, 103
            clock[0] += 1.0
        elif capture_index == 104:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "normal", 0, 104
            clock[0] += 15.0
        else:
            boot = "native-construction:boot-a"
            phase, ordinal, updates = "normal", 0, 104
            clock[0] += 0.1
        snapshot = _snapshot(1000 + sequence, boot=boot, sequence=sequence)
        snapshot["uptimeMs"] = sequence * 1000
        render = _metrics(updates, phase=phase, ordinal=ordinal)
        render["cellSize"] = [2, 2]
        snapshot["render"] = render
        path = tmp_path / f"strict-{capture_index:03d}.png"
        _status_image(encode_frame(snapshot)).save(path)
        with Image.open(path) as image:
            image.load()
            sample = decoder.decode(image, monotonic=clock[0], window_identity=current_identity.fingerprint)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if binding_drift is not None and capture_index == 105:
            final_capture_returned.set()
            if not release_final_capture.wait(5.0):
                raise DriverError("final binding drift barrier did not release")
        return sample, digest, path

    def write_absence_capture(current_identity: WindowIdentity, **_kwargs: object) -> Path:
        if current_identity.fingerprint != identity.fingerprint:
            raise AssertionError("absence capture identity changed")
        clock[0] = max(clock[0], time.monotonic() + 1.0)
        path = tmp_path / "ordinary-absence.png"
        Image.new("RGB", (256, 256), (127, 127, 127)).save(path)
        return path

    def write_region_capture(current_identity: WindowIdentity, **_kwargs: object) -> Path:
        state = daemon._native_construction_handshake.get("state")
        if state in {None, "", "finalize_required"}:
            _sample, _digest, path = write_strict_capture(
                current_identity,
                PixelDecoder(crop=driver.profile.canvas_crop),
            )
            return path
        return write_absence_capture(current_identity, **_kwargs)

    if binding_drift is not None:
        driver.resolve_identity = lambda **_kwargs: active_identity[0]  # type: ignore[method-assign]
    driver._clock = now  # type: ignore[method-assign]
    driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]
    daemon._native_clock = now  # type: ignore[method-assign]
    meter = driver.native_memory_meter
    if meter is None:
        raise AssertionError("native construction fixture has no pre-bound native memory meter")
    meter._monotonic = now

    def write_native_frame(*args: object, **kwargs: object) -> tuple[object, str, Path, None]:
        sample, digest, path = write_strict_capture(*args, **kwargs)
        return sample, digest, path, None

    daemon._capture_native_frame_with_capture = write_native_frame  # type: ignore[method-assign]
    daemon._capture_native_region_for_identity = write_region_capture  # type: ignore[method-assign]
    driver.capture_client_crop_for_identity = write_absence_capture  # type: ignore[method-assign]
    original_rebind = daemon.controller.rebind_native_construction_frame
    original_observe = daemon.controller.observe_frame
    original_native_calibrated = daemon.controller.native_calibrated
    original_transaction = daemon._native_construction_finalize_transaction
    original_flush_expiry = daemon._native_construction_flush_expiry
    original_validate_snapshot = daemon._native_construction_validate_snapshot

    def gated_validate_snapshot(*args: object, **kwargs: object) -> object:
        result = original_validate_snapshot(*args, **kwargs)
        if recheck_race and threading.get_ident() in recheck_actor_thread_ids:
            recheck_validation_entered.set()
            if not release_recheck_validation.wait(10.0):
                raise AssertionError("recheck validation barrier did not release")
        return result

    def gated_rebind(**kwargs: object) -> object:
        rebind_calls.append(kwargs)
        if cancel_after_first_final_capture:
            return original_rebind(**kwargs)
        if kwargs.get("reason") == "native_construction_finalize":
            rebind_entered.set()
            if not release_rebind.wait(3.0):
                raise DriverError("finalize rebind barrier did not release")
        return original_rebind(**kwargs)

    def counted_observe(**kwargs: object) -> object:
        observe_calls.append(kwargs)
        return original_observe(**kwargs)

    def counted_native_calibrated(*args: object, **kwargs: object) -> object:
        native_calibrated_calls.append((args, kwargs))
        return original_native_calibrated(*args, **kwargs)

    def gated_transaction(**kwargs: object) -> object:
        transaction_entered.set()
        if not release_transaction.wait(5.0):
            raise DriverError("finalize transaction barrier did not release")
        return original_transaction(**kwargs)

    def gated_flush_expiry() -> None:
        original_flush_expiry()
        if threading.current_thread().name == "native-finalize-cancel":
            cancel_flush_done.set()
            if not release_cancel_flush.wait(5.0):
                raise DriverError("cancel flush barrier did not release")

    class ForwardingControllerLock:
        """Forward the real controller mutex while observing cancel contention."""

        def __init__(self, real_lock: threading.RLock) -> None:
            self.real_lock = real_lock

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if blocking and threading.current_thread().name == "native-finalize-cancel":
                if not final_controller_entered.wait(5.0):
                    raise DriverError("final transaction did not acquire controller lock")
                probed = self.real_lock.acquire(blocking=False)
                cancel_controller_probe_values.append(probed)
                cancel_controller_probe.set()
                if probed:
                    self.real_lock.release()
            if timeout == -1:
                acquired = self.real_lock.acquire(blocking)
            else:
                acquired = self.real_lock.acquire(blocking, timeout)
            if acquired and threading.current_thread().name == "native-construction-composed":
                final_controller_entered.set()
            return acquired

        def release(self) -> None:
            self.real_lock.release()

        def __enter__(self) -> ForwardingControllerLock:
            self.acquire()
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            self.release()

        def __getattr__(self, name: str) -> object:
            return getattr(self.real_lock, name)

    daemon.controller.rebind_native_construction_frame = gated_rebind  # type: ignore[method-assign]
    daemon.controller.observe_frame = counted_observe  # type: ignore[method-assign]
    daemon.controller.native_calibrated = counted_native_calibrated  # type: ignore[method-assign]
    daemon._native_construction_finalize_transaction = gated_transaction  # type: ignore[method-assign]
    daemon._native_construction_flush_expiry = gated_flush_expiry  # type: ignore[method-assign]
    daemon._native_construction_validate_snapshot = gated_validate_snapshot  # type: ignore[method-assign]
    try:
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        assert [entry["state"] for entry in prepared["stateHistory"]] == ["construction_prepared"]
        assert all(set(entry) == {"state", "nativeClock"} for entry in prepared["stateHistory"])
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        running = daemon.native_construction_recheck(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        assert [entry["state"] for entry in running["stateHistory"]] == [
            "construction_prepared",
            "running",
        ]
        operation_started = clock[0]

        def run() -> None:
            try:
                worker_result["value"] = daemon.run_native_calibration(expected_generation=7)
            except BaseException as exc:  # pragma: no cover - asserted below
                worker_result["error"] = exc

        worker = threading.Thread(target=run, name="native-construction-composed")
        worker.start()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            with daemon._native_construction_lock:
                state = daemon._native_construction_handshake.get("state")
            if state == "resume_ready":
                break
            if not worker.is_alive():
                break
            time.sleep(0.01)
        if "error" in worker_result:
            raise worker_result["error"]  # type: ignore[misc]
        assert worker.is_alive(), worker_result.get("error")

        if recheck_race:
            # Keep one real CancellationActor alive across both rechecks.  Its
            # first response seeds the immutable binding in the actor child;
            # the second request is the one that is paused between daemon
            # validation and its second construction-lock read.
            from tools import native_calibration_operator as operator

            actor_socket = tmp_path / "finalized-actor.sock"
            actor_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            actor_listener.bind(str(actor_socket))
            actor_socket.chmod(0o600)
            actor_listener.listen(4)
            actor_listener.settimeout(0.2)

            def serve_actor_rechecks() -> None:
                nonlocal actor_request_count
                try:
                    while not actor_stop.is_set():
                        try:
                            connection, _ = actor_listener.accept()
                        except TimeoutError:
                            continue
                        except OSError:
                            if actor_stop.is_set():
                                return
                            raise
                        with connection:
                            raw = bytearray()
                            while not raw.endswith(b"\n"):
                                chunk = connection.recv(4096)
                                if not chunk:
                                    break
                                raw.extend(chunk)
                            operation = json.loads(bytes(raw).decode("utf-8"))
                            assert operation["command"] == "native_construction_recheck"
                            with actor_request_lock:
                                actor_request_count += 1
                                if actor_request_count == 2:
                                    recheck_actor_thread_ids.add(threading.get_ident())
                            response = SessionIPC(daemon.controller, daemon).dispatch(
                                {
                                    "command": "native_construction_recheck",
                                    "generation": operation["generation"],
                                    "reservationId": operation["reservationId"],
                                }
                            )
                            connection.sendall(
                                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                            )
                except BaseException as exc:  # pragma: no cover - asserted below
                    actor_errors.append(exc)

            actor_server = threading.Thread(target=serve_actor_rechecks, name="native-finalized-actor")
            actor_server.start()
            actor = operator.CancellationActor(
                actor_socket,
                generation=7,
                reservation_id=reservation["reservationId"],
                deadline=time.monotonic() + 60.0,
                owner_pid=os.getpid(),
                owner_start=operator._proc_start(os.getpid()),
            )
            actor.start()
            initial_actor_reply = actor.call(
                "recheck",
                generation=7,
                reservation_id=reservation["reservationId"],
                deadline=actor.deadline,
            )
            assert initial_actor_reply["handshake"]["state"] == "resume_ready"
            assert initial_actor_reply["reservation"]["reservationState"] == "consumed"

            def run_recheck() -> None:
                try:
                    assert actor is not None
                    recheck_result["value"] = actor.call(
                        "recheck",
                        reservation_id=reservation["reservationId"],
                        generation=7,
                        deadline=actor.deadline,
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    recheck_error.append(exc)

            recheck_thread = threading.Thread(target=run_recheck, name="native-recheck-race")
            recheck_thread.start()
            assert recheck_validation_entered.wait(5.0)

        finalized = daemon.native_construction_finalize_admit(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        assert finalized["handshake"]["state"] == "finalize_required"
        fast_finalize = daemon.native_construction_finalize_admit(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        assert fast_finalize["handshake"]["state"] == "finalize_required"
        if not cancel_after_first_final_capture and binding_drift is None and not recheck_race:
            controller_lock_facade = ForwardingControllerLock(daemon.controller._mutex)
            daemon.controller._mutex = controller_lock_facade  # type: ignore[assignment]

        if recheck_race:
            assert transaction_entered.wait(5.0)
            release_transaction.set()
            release_rebind.set()
            terminal_deadline = time.monotonic() + 60.0
            while time.monotonic() < terminal_deadline:
                with daemon._native_construction_lock:
                    terminal_snapshot = copy.deepcopy(
                        daemon._native_construction_terminals.get(reservation["reservationId"])
                    )
                if terminal_snapshot is not None:
                    break
                assert worker.is_alive(), worker_result.get("error")
                time.sleep(0.01)
            assert terminal_snapshot is not None
            assert daemon.controller.status().state is ControllerState.INPUT_CALIBRATION, json.dumps(
                terminal_snapshot, sort_keys=True
            )
            journal_native = daemon.controller.journal.read().get("calibrationEvidence", {}).get("native")
            assert isinstance(journal_native, dict)
            assert daemon.controller._native_calibrated is True
            assert isinstance(journal_native.get("capture_path"), str)

            release_recheck_validation.set()
            recheck_thread.join(timeout=10.0)
            assert not recheck_thread.is_alive()
            assert not recheck_error, recheck_error
            race_reply = recheck_result.get("value")
            assert isinstance(race_reply, dict)
            assert race_reply["handshake"]["state"] == "finalized"
            assert race_reply["reservation"]["reservationState"] == "finalized"
            assert actor_request_count == 2
            assert actor is not None
            actor.close()
            actor_stop.set()
            assert actor_listener is not None
            actor_listener.close()
            assert actor_server is not None
            actor_server.join(timeout=5.0)
            assert not actor_errors, actor_errors
            race_reply["reservation"]["reservationState"] = "mutated"
            assert terminal_snapshot["reservation"]["reservationState"] == "finalized"
            ipc_reply = SessionIPC(daemon.controller, daemon).dispatch(
                {
                    "command": "native_construction_recheck",
                    "generation": 7,
                    "reservationId": reservation["reservationId"],
                }
            )
            assert ipc_reply["ok"] is True
            assert ipc_reply["handshake"]["state"] == "finalized"
            assert ipc_reply["reservation"]["reservationState"] == "finalized"
            with pytest.raises(DriverError, match="terminal generation is stale"):
                daemon.native_construction_recheck(
                    expected_generation=8,
                    reservation_id=reservation["reservationId"],
                )
            with pytest.raises(DriverError, match="reservation is unknown"):
                daemon.native_construction_recheck(
                    expected_generation=7,
                    reservation_id=f"{reservation['reservationId']}-different",
                )
        elif binding_drift is not None:
            assert final_capture_returned.wait(5.0)
            if binding_drift == "identity":
                active_identity[0] = replace(identity, fingerprint="native-r9-final-drift")
            else:
                driver.guardian.generation += 1
            release_final_capture.set()
            worker.join(timeout=60.0)
            assert not worker.is_alive()
            assert isinstance(worker_result.get("error"), DriverError)
            assert "binding changed" in str(worker_result["error"])
            assert not transaction_entered.is_set()
            assert rebind_calls == []
            assert observe_calls == []
            assert native_calibrated_calls == []
            return
        elif cancel_after_first_final_capture:
            assert transaction_entered.wait(5.0)

            def cancel_after_transaction_entry() -> None:
                cancel_attempted.set()
                try:
                    cancel_response.append(
                        SessionIPC(daemon.controller, daemon).dispatch(
                            {
                                "command": "native_construction_recovery_cancel",
                                "generation": 7,
                                "reservationId": reservation["reservationId"],
                            }
                        )
                    )
                finally:
                    cancel_done.set()

            cancel_thread = threading.Thread(
                target=cancel_after_transaction_entry, name="native-finalize-cancel"
            )
            cancel_thread.start()
            assert cancel_attempted.wait(2.0)
            assert cancel_flush_done.wait(3.0)
            release_cancel_flush.set()
            assert cancel_done.wait(3.0)
            release_transaction.set()
        else:
            assert transaction_entered.wait(5.0)

            def cancel_during_final_transaction() -> None:
                cancel_attempted.set()
                cancel_response.append(
                    SessionIPC(daemon.controller, daemon).dispatch(
                        {
                            "command": "native_construction_recovery_cancel",
                            "generation": 7,
                            "reservationId": reservation["reservationId"],
                        }
                    )
                )
                cancel_done.set()

            cancel_thread = threading.Thread(
                target=cancel_during_final_transaction, name="native-finalize-cancel"
            )
            cancel_thread.start()
            assert cancel_attempted.wait(2.0)
            assert cancel_flush_done.wait(3.0)
            release_transaction.set()
            assert final_controller_entered.wait(3.0)
            assert rebind_entered.wait(5.0)
            release_cancel_flush.set()
            assert cancel_controller_probe.wait(3.0)
            assert cancel_controller_probe_values == [False]
            assert not cancel_done.is_set()
            release_rebind.set()

        worker.join(timeout=60.0)
        assert not worker.is_alive()
        if recheck_race:
            assert "error" not in worker_result, worker_result.get("error")
            result = worker_result["value"]
            assert isinstance(result, dict)
            assert result["calibration"] == "native"
        else:
            assert cancel_thread is not None
            cancel_thread.join(timeout=5.0)
            assert not cancel_thread.is_alive()
        if cancel_after_first_final_capture:
            assert len(cancel_response) == 1
            assert cancel_response[0]["ok"] is True
            assert cancel_response[0]["handshake"]["state"] == "cancelled"
            assert cancel_response[0]["reservation"]["reservationState"] == "cancelled"
            assert "error" in worker_result
            assert "owner was revoked before commit" in str(worker_result["error"])
            assert rebind_calls == []
            assert observe_calls == []
            assert native_calibrated_calls == []
            assert daemon.controller.status().state is ControllerState.NATIVE_CALIBRATION
            assert daemon.controller.native_construction_checkpoint()["phase"] == "recovery_required"
            assert capture_index == 105
            terminal = daemon.native_construction_recheck(
                expected_generation=7, reservation_id=reservation["reservationId"]
            )
            assert terminal["handshake"]["state"] == "cancelled"
            assert terminal["reservation"]["reservationState"] == "cancelled"
        elif not recheck_race:
            assert cancel_response == [{"ok": False, "error": "native construction reservation is unknown"}]
            assert "error" not in worker_result, worker_result.get("error")
            result = worker_result["value"]
            assert isinstance(result, dict)
            assert result["calibration"] == "native"
            assert daemon._native_construction_handshake["state"] == "finalized"
            assert daemon.controller.native_construction_checkpoint() is not None
            terminal = daemon.native_construction_recheck(
                expected_generation=7, reservation_id=reservation["reservationId"]
            )
            assert terminal["handshake"]["state"] == "finalized"
            assert terminal["reservation"]["reservationState"] == "finalized"
            expected_history = [
                "construction_prepared",
                "running",
                "construction_reset_archived",
                "baseline_bound",
                "construction_released",
                "hold_required",
                "updates_reset_archived",
                "measure_ready",
                "resume_required",
                "resume_ready",
                "finalize_required",
                "finalized",
            ]
            history = terminal["stateHistory"]
            assert [entry["state"] for entry in history] == expected_history
            assert all(set(entry) == {"state", "nativeClock"} for entry in history)
            clocks = [entry["nativeClock"] for entry in history]
            assert all(isinstance(clock_value, float) for clock_value in clocks)
            assert clocks == sorted(clocks)
            polled = daemon.native_construction_recheck(
                expected_generation=7, reservation_id=reservation["reservationId"]
            )
            assert polled["stateHistory"] == history
        else:
            assert daemon.controller.status().state is ControllerState.INPUT_CALIBRATION
        if not cancel_after_first_final_capture:
            assert capture_index == 105
        assert clock[0] - operation_started > 127.0
    finally:
        release_rebind.set()
        release_transaction.set()
        release_cancel_flush.set()
        release_final_capture.set()
        release_recheck_validation.set()
        recheck_thread = locals().get("recheck_thread")
        if isinstance(recheck_thread, threading.Thread):
            recheck_thread.join(timeout=10.0)
        actor_stop.set()
        if actor_listener is not None:
            try:
                actor_listener.close()
            except OSError:
                pass
        if actor is not None:
            try:
                actor.close()
            except Exception:
                actor.abandon()
        if actor_server is not None:
            actor_server.join(timeout=5.0)
        if controller_lock_facade is not None:
            daemon.controller._mutex = controller_lock_facade.real_lock  # type: ignore[assignment]
        daemon._native_construction_flush_expiry = original_flush_expiry  # type: ignore[method-assign]
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def _configure_deadline_png_runner(
    daemon: SessionDaemon,
    driver: DesktopDriver,
    identity: WindowIdentity,
    tmp_path: Path,
) -> list[float]:
    """Install the same real PNG runner boundary used by the composed test."""

    from .test_session_runtime import _snapshot

    clock = [time.monotonic()]
    capture_index = [0]
    sequence = [0]

    def now() -> float:
        return clock[0]

    def write_strict_capture(
        current_identity: WindowIdentity,
        decoder: PixelDecoder,
        *,
        deadline: float | None = None,
        **_kwargs: object,
    ) -> tuple[object, str, Path]:
        del deadline
        if current_identity.fingerprint != identity.fingerprint:
            raise AssertionError("capture identity changed")
        capture_index[0] += 1
        sequence[0] += 1
        index = capture_index[0]
        if index == 1:
            boot, phase, ordinal, updates, advance = "boot-a", "normal", 0, 1, 0.1
        elif index == 2:
            boot, phase, ordinal, updates, advance = "native-construction:boot-a", "normal", 0, 1, 0.1
        elif index == 3:
            boot, phase, ordinal, updates, advance = "native-construction:boot-a", "held", 0, 3, 15.0
        elif 4 <= index <= 102:
            boot, phase, ordinal, updates, advance = (
                "native-construction:boot-a",
                "updates",
                index - 3,
                3 + index - 3,
                1.0,
            )
        elif index == 103:
            boot, phase, ordinal, updates, advance = "native-construction:boot-a", "held", 100, 103, 1.0
        elif index == 104:
            boot, phase, ordinal, updates, advance = "native-construction:boot-a", "normal", 0, 104, 15.0
        else:
            boot, phase, ordinal, updates, advance = "native-construction:boot-a", "normal", 0, 104, 0.1
        clock[0] += advance
        snapshot = _snapshot(1000 + sequence[0], boot=boot, sequence=sequence[0])
        snapshot["uptimeMs"] = sequence[0] * 1000
        render = _metrics(updates, phase=phase, ordinal=ordinal)
        render["cellSize"] = [2, 2]
        snapshot["render"] = render
        path = tmp_path / f"deadline-{index:03d}.png"
        _status_image(encode_frame(snapshot)).save(path)
        with Image.open(path) as image:
            image.load()
            sample = decoder.decode(
                image,
                monotonic=clock[0],
                window_identity=current_identity.fingerprint,
            )
        return sample, hashlib.sha256(path.read_bytes()).hexdigest(), path

    def write_absence_capture(current_identity: WindowIdentity, **_kwargs: object) -> Path:
        if current_identity.fingerprint != identity.fingerprint:
            raise AssertionError("absence capture identity changed")
        path = tmp_path / "deadline-absence.png"
        Image.new("RGB", (256, 256), (127, 127, 127)).save(path)
        return path

    def write_region_capture(current_identity: WindowIdentity, **_kwargs: object) -> Path:
        state = daemon._native_construction_handshake.get("state")
        if state in {None, "", "finalize_required"}:
            _sample, _digest, path = write_strict_capture(
                current_identity,
                PixelDecoder(crop=driver.profile.canvas_crop),
            )
            return path
        return write_absence_capture(current_identity)

    driver._clock = now  # type: ignore[method-assign]
    daemon._native_clock = now  # type: ignore[method-assign]
    meter = driver.native_memory_meter
    if meter is None:
        raise AssertionError("deadline runner fixture has no pre-bound native memory meter")
    meter._monotonic = now
    driver.new_native_decoder = lambda: PixelDecoder(crop=driver.profile.canvas_crop)  # type: ignore[method-assign]

    def write_native_frame(*args: object, **kwargs: object) -> tuple[object, str, Path, None]:
        sample, digest, path = write_strict_capture(*args, **kwargs)
        return sample, digest, path, None

    daemon._capture_native_frame_with_capture = write_native_frame  # type: ignore[method-assign]
    daemon._capture_native_region_for_identity = write_region_capture  # type: ignore[method-assign]
    driver.capture_client_crop_for_identity = write_absence_capture  # type: ignore[method-assign]
    return clock


@pytest.mark.parametrize(
    ("drift_kind", "expected_error"),
    [
        ("identity", "deferred finalize capture identity binding changed"),
        ("guardian", "deferred finalize Guardian binding changed"),
    ],
)
def test_native_finalize_transaction_rechecks_final_binding_before_mutation(
    tmp_path: Path, drift_kind: str, expected_error: str
) -> None:
    """Final commit refuses a changed identity or Guardian generation atomically."""

    from tests.test_session import native_calibration

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    rebind_calls: list[object] = []
    observe_calls: list[object] = []
    native_calibrated_calls: list[object] = []
    try:
        daemon._native_construction_observation_provider = lambda: _construction_frame(
            identity, boot="boot-a", sequence=1
        )
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        with daemon._native_construction_lock:
            run_context = copy.deepcopy(daemon._native_construction_run_context)
            reservation_snapshot = copy.deepcopy(daemon._native_construction_reservation)
            assert run_context is not None
            assert reservation_snapshot is not None
            daemon._native_construction_handshake["state"] = "finalize_required"
        original_rebind = daemon.controller.rebind_native_construction_frame
        original_observe = daemon.controller.observe_frame
        original_native_calibrated = daemon.controller.native_calibrated

        def counted_rebind(**kwargs: object) -> object:
            rebind_calls.append(kwargs)
            return original_rebind(**kwargs)

        def counted_observe(**kwargs: object) -> object:
            observe_calls.append(kwargs)
            return original_observe(**kwargs)

        def counted_native_calibrated(*args: object, **kwargs: object) -> object:
            native_calibrated_calls.append((args, kwargs))
            return original_native_calibrated(*args, **kwargs)

        daemon.controller.rebind_native_construction_frame = counted_rebind  # type: ignore[method-assign]
        daemon.controller.observe_frame = counted_observe  # type: ignore[method-assign]
        daemon.controller.native_calibrated = counted_native_calibrated  # type: ignore[method-assign]
        if drift_kind == "guardian":
            driver.guardian.generation += 1
            final_identity_fingerprint = identity.fingerprint
            final_capture_identity = identity.fingerprint
        else:
            final_identity_fingerprint = "drifted-final-identity"
            final_capture_identity = identity.fingerprint
        with pytest.raises(DriverError, match=re.escape(expected_error)):
            daemon._native_construction_finalize_transaction(
                run_context=run_context,
                reservation_snapshot=reservation_snapshot,
                expected_generation=7,
                finalize_deadline=daemon._native_clock() + 30.0,
                hook_deadline=None,
                final_boot=reservation["expectedConstructorBoot"],
                final_sequence=2,
                final_crc32=2,
                final_identity_fingerprint=final_identity_fingerprint,
                final_capture_identity=final_capture_identity,
                evidence=native_calibration(process_start="native-r9-binding-drift"),
            )
        assert rebind_calls == []
        assert observe_calls == []
        assert native_calibrated_calls == []
    finally:
        with daemon._native_construction_lock:
            active = daemon._native_construction_reservation
        if active is not None:
            try:
                daemon.native_construction_recovery_cancel(
                    expected_generation=7,
                    reservation_id=active["reservationId"],
                )
            except DriverError:
                pass
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize("deadline_kind", ["final", "hook", "operation"])
def test_native_finalize_transaction_wait_expiry_refuses_before_mutation(
    tmp_path: Path, deadline_kind: str
) -> None:
    """A live PNG owner cannot commit after a bound expires on lock wait."""

    from crowley.core.watchlist import load_catalog

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    daemon.config = replace(daemon.config, plan_hash=load_catalog().scan_plan()["planHash"])
    clock = _configure_deadline_png_runner(daemon, driver, identity, tmp_path)
    worker_result: dict[str, object] = {}
    finalize_event_entered = threading.Event()
    release_finalize_event = threading.Event()
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    original_transaction_entered = threading.Event()
    controller_held = threading.Event()
    rebind_calls: list[object] = []
    observe_calls: list[object] = []
    native_calibrated_calls: list[object] = []
    original_event = daemon._native_construction_finalize_event
    original_transaction = daemon._native_construction_finalize_transaction
    transaction_calls = [0]
    original_transaction_calls = [0]

    class DelayedFinalizeEvent:
        def set(self) -> None:
            original_event.set()

        def clear(self) -> None:
            original_event.clear()

        def wait(self, timeout: float | None = None) -> bool:
            finalize_event_entered.set()
            if not release_finalize_event.wait(timeout):
                return False
            return original_event.wait(timeout)

    daemon._native_construction_finalize_event = DelayedFinalizeEvent()  # type: ignore[assignment]

    def gated_transaction(*args: object, **kwargs: object) -> object:
        transaction_calls[0] += 1
        transaction_entered.set()
        if not release_transaction.wait(3.0):
            raise DriverError("transaction deadline barrier did not release")
        original_transaction_calls[0] += 1
        original_transaction_entered.set()
        return original_transaction(*args, **kwargs)

    daemon._native_construction_finalize_transaction = gated_transaction  # type: ignore[method-assign]
    original_rebind = daemon.controller.rebind_native_construction_frame
    original_observe = daemon.controller.observe_frame
    original_native_calibrated = daemon.controller.native_calibrated

    def counted_rebind(**kwargs: object) -> object:
        rebind_calls.append(kwargs)
        return original_rebind(**kwargs)

    def counted_observe(**kwargs: object) -> object:
        observe_calls.append(kwargs)
        return original_observe(**kwargs)

    def counted_native_calibrated(*args: object, **kwargs: object) -> object:
        native_calibrated_calls.append((args, kwargs))
        return original_native_calibrated(*args, **kwargs)

    daemon.controller.rebind_native_construction_frame = counted_rebind  # type: ignore[method-assign]
    daemon.controller.observe_frame = counted_observe  # type: ignore[method-assign]
    daemon.controller.native_calibrated = counted_native_calibrated  # type: ignore[method-assign]
    try:
        prepared = daemon.native_construction_prepare(expected_generation=7)
        reservation = prepared["reservation"]
        daemon.native_construction_bootstrap(
            expected_generation=7,
            reservation_id=reservation["reservationId"],
            expected_constructor_boot=reservation["expectedConstructorBoot"],
        )
        if deadline_kind == "hook":
            original_resume_ready = daemon.notify_native_resume_ready

            def short_hook_resume_ready(*args: object, **kwargs: object) -> object:
                result = original_resume_ready(*args, **kwargs)
                with daemon._native_construction_lock:
                    daemon._native_construction_handshake["finalizeHookDeadline"] = (
                        daemon._native_clock() + 1.0
                    )
                return result

            driver.notify_native_resume_ready = short_hook_resume_ready  # type: ignore[method-assign]
        run_thread = threading.Thread(
            target=lambda: _store_exception(
                worker_result,
                lambda: daemon.run_native_calibration(expected_generation=7),
            ),
            name=f"native-deadline-{deadline_kind}",
        )
        run_thread.start()
        assert _wait_for(
            lambda: daemon._native_construction_handshake.get("state") == "resume_ready",
            timeout=60.0,
        )

        # Hook is intentionally the tight bound for the hook case.  The
        # delayed event gives this test a real post-admission handoff point so
        # the worker cannot race the deadline fixture's mutation.
        with daemon._native_construction_lock:
            now = clock[0]
            if deadline_kind == "hook":
                # The callback wrapper above installs this bound before the
                # worker snapshots its local finalize-hook deadline.
                pass
            elif deadline_kind == "operation":
                daemon._native_construction_operation_deadline = now + 1.0
            else:
                daemon._native_construction_handshake["finalizeHookDeadline"] = now + 30.0
        admitted = daemon.native_construction_finalize_admit(
            expected_generation=7, reservation_id=reservation["reservationId"]
        )
        assert admitted["handshake"]["state"] == "finalize_required"
        assert finalize_event_entered.wait(3.0)
        with daemon._native_construction_lock:
            now = clock[0]
            # Keep the other applicable bounds valid while the named bound
            # expires after the outer revalidation has succeeded.
            daemon._native_construction_handshake["finalizeDeadline"] = now + 10.0
            if deadline_kind == "final":
                deadline = now + 1.0
                daemon._native_construction_handshake["finalizeDeadline"] = deadline
            elif deadline_kind == "hook":
                deadline = float(daemon._native_construction_handshake["finalizeHookDeadline"])
            else:
                deadline = float(daemon._native_construction_operation_deadline)
        release_finalize_event.set()
        # This is the actual transaction entry, after all outer capture and
        # revalidation checks.  Hold the controller lock while the named
        # deadline expires, then release the wrapper into the production
        # transaction so its inner guard, rather than an earlier check,
        # refuses the commit.
        assert transaction_entered.wait(10.0)
        with daemon.controller.lock:
            controller_held.set()
            clock[0] = deadline + 1.0
            release_transaction.set()
            time.sleep(0.1)
            assert rebind_calls == []
            assert observe_calls == []
            assert native_calibrated_calls == []
        run_thread.join(timeout=10.0)
        assert not run_thread.is_alive()
        assert transaction_calls == [1]
        assert original_transaction_calls == [1]
        assert original_transaction_entered.is_set()
        assert isinstance(worker_result.get("error"), DriverError)
        error = str(worker_result["error"])
        expected_error = {
            "final": "deferred finalize frame deadline expired",
            "hook": "deferred finalize hook deadline expired",
            "operation": "deferred finalize admission deadline expired",
        }[deadline_kind]
        assert expected_error in error
        assert daemon._native_construction_handshake["state"] == "error"
        assert daemon.controller.native_construction_checkpoint()["phase"] == "recovery_required"
    finally:
        release_finalize_event.set()
        release_transaction.set()
        thread = daemon._calibration_thread
        if thread is not None:
            thread.join(timeout=10.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def _store_exception(result: dict[str, object], call: Callable[[], object]) -> None:
    try:
        result["value"] = call()
    except BaseException as exc:  # pragma: no cover - asserted by caller
        result["error"] = exc


def _owned_child_identity(child: subprocess.Popen[str]) -> tuple[str, str]:
    stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
    return (
        stat[stat.rfind(")") + 2 :].split()[19],
        os.path.realpath(os.readlink(f"/proc/{child.pid}/exe")),
    )


def test_reset5_archives_historical_hwm_before_each_owned_window(tmp_path: Path) -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys\nbuf=None\nfor line in sys.stdin:\n"
            " if line == 'alloc\\n':\n  buf=bytearray(32*1024*1024)\n"
            "  for i in range(0,len(buf),4096): buf[i]=1\n  print('allocated', flush=True)\n"
            " elif line == 'free\\n':\n  buf=None\n  print('freed', flush=True)\n"
            " elif line == 'stop\\n': break\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        process_start, executable = _owned_child_identity(child)
        meter = LinuxProcStatusVmHwmReset5(
            child.pid,
            process_start=process_start,
            process_executable=executable,
            identity_fingerprint="owned-child",
        )
        archived = []
        pending = meter.begin_window(
            "construction",
            0,
            limit_bytes=128 * 1024 * 1024,
            before_reset=lambda record: (
                archived.append(record),
                NativeMemoryArchiveReceipt.for_record(record, archive_id=f"archive-{len(archived)}"),
            )[1],
        )
        assert archived[0].method == "linux_proc_status_vmhwm_reset5"
        assert archived[0].reset_value == 5
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write("alloc\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "allocated"
        child.stdin.write("free\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "freed"
        measurement = meter.finish_window(
            pending,
            workload_start_monotonic=pending.reset_completed_monotonic,
            workload_end_monotonic=time.monotonic(),
            workload_start_ordinal=0,
            workload_end_ordinal=0,
        )
        assert measurement.growth_bytes >= 8 * 1024 * 1024
        assert measurement.growth_bytes == max(
            0, measurement.final_hwm_bytes - measurement.post_reset_hwm_bytes
        )
        second = meter.begin_window(
            "updates",
            0,
            limit_bytes=16 * 1024 * 1024,
            before_reset=lambda record: (
                archived.append(record),
                NativeMemoryArchiveReceipt.for_record(record, archive_id=f"archive-{len(archived)}"),
            )[1],
        )
        assert len(archived) == 2
        assert second.historical_hwm_bytes >= second.post_reset_hwm_bytes
    finally:
        if child.stdin is not None:
            child.stdin.write("stop\n")
            child.stdin.flush()
        child.terminate()
        child.wait(timeout=3)
        if meter is not None:
            meter.close()


def _native_failure_daemon(tmp_path: Path):
    driver, _identity, child = _native_driver(tmp_path)
    assert driver.native_memory_meter is not None
    driver.native_memory_meter.close()
    driver.native_memory_meter = None
    controller = Controller(
        Journal(tmp_path / "failure-journal.json"),
        driver=driver,
        recover_on_restart=False,
    )
    with controller.lock:
        controller._state = ControllerState.NATIVE_CALIBRATION
    daemon = object.__new__(SessionDaemon)
    daemon.driver = driver
    daemon.controller = controller
    daemon._native_clock = lambda: driver._native_test_clock[0]  # type: ignore[attr-defined]
    daemon._native_memory_archive_diagnostic = None
    daemon._native_handshake = {"state": "running", "failure": ""}
    return daemon, driver, controller, child


def _complete_over_limit_window(meter: LinuxProcStatusVmHwmReset5) -> NativeMemoryMeasurement:
    uid = os.getuid()
    statuses = iter(
        [
            (1_000, 900, uid),
            (1_000, 900, uid),
            (1_000, 900, uid),
            (1_000, 900, uid),
            (1_000, 900, uid),
            (1_000 + 200 * 1024 * 1024, 900, uid),
            (1_000, 900, uid),
        ]
    )
    fallback = (1_000, 900, uid)

    def read_status():
        return next(statuses, fallback)

    meter._status = read_status  # type: ignore[method-assign]
    pending = meter.begin_window(
        "construction",
        0,
        limit_bytes=128 * 1024 * 1024,
        before_reset=lambda record: NativeMemoryArchiveReceipt.for_record(record, archive_id="reset"),
    )
    return meter.finish_window(
        pending,
        workload_start_monotonic=pending.reset_completed_monotonic,
        workload_end_monotonic=pending.reset_completed_monotonic,
        workload_start_ordinal=0,
        workload_end_ordinal=0,
    )


def test_reset5_over_limit_without_assessment_callback_fails_closed() -> None:
    child = subprocess.Popen(["/bin/sleep", "20"])
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        process_start, executable = _owned_child_identity(child)
        meter = LinuxProcStatusVmHwmReset5(
            child.pid,
            process_start=process_start,
            process_executable=executable,
            identity_fingerprint="missing-assessment-callback",
        )
        with pytest.raises(CalibrationError, match="callback is not configured"):
            _complete_over_limit_window(meter)
        assert meter._pending_window is None
    finally:
        if meter is not None:
            meter.close()
        child.terminate()
        child.wait(timeout=3)


def test_assessment_archive_receipt_rejects_forged_domain_and_identity() -> None:
    child = subprocess.Popen(["/bin/sleep", "20"])
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        process_start, executable = _owned_child_identity(child)
        meter = LinuxProcStatusVmHwmReset5(
            child.pid,
            process_start=process_start,
            process_executable=executable,
            identity_fingerprint="receipt-domain",
        )

        def valid_receipt(measurement: NativeMemoryMeasurement) -> NativeMemoryAssessmentArchiveReceipt:
            payload = {
                "kind": "native_memory_assessments",
                "schema": 1,
                "classification": "target_exceeded",
                "measurement": measurement.to_dict(),
            }
            outcome_id = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            measurement_digest = hashlib.sha256(
                json.dumps(measurement.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return NativeMemoryAssessmentArchiveReceipt(outcome_id, measurement_digest, 1, True)

        meter._over_limit_archive = valid_receipt
        measurement = _complete_over_limit_window(meter)
        payload = {
            "kind": "native_memory_assessments",
            "schema": 1,
            "classification": "target_exceeded",
            "measurement": measurement.to_dict(),
            "outcomeId": valid_receipt(measurement).outcome_id,
            "phase": measurement.phase,
            "ordinal": measurement.ordinal,
            "archiveOrdinal": 1,
            "archivedAt": 123,
        }
        assert NativeMemoryAssessmentArchiveReceipt.from_archive(measurement, payload).durable is True
        for key, forged in (
            ("kind", "native_memory_failures"),
            ("classification", "within_targets"),
            ("outcomeId", "0" * 64),
            ("archiveOrdinal", 0),
            ("measurement", {**measurement.to_dict(), "final_hwm_bytes": 1}),
        ):
            forged_record = copy.deepcopy(payload)
            forged_record[key] = forged
            with pytest.raises(CalibrationError):
                NativeMemoryAssessmentArchiveReceipt.from_archive(measurement, forged_record)
    finally:
        if meter is not None:
            meter.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize("drift", ["state", "meter", "identity"])
def test_daemon_assessment_archive_rejects_live_binding_drift(tmp_path: Path, drift: str) -> None:
    daemon, driver, controller, child = _native_failure_daemon(tmp_path)
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        meter = daemon._ensure_native_memory_meter()
        assert meter is not None
        original_archive = daemon._archive_native_memory_assessment
        original_resolve = driver.resolve_identity

        def drifted_archive(measurement: NativeMemoryMeasurement):
            if drift == "state":
                controller._state = ControllerState.PAUSED
            elif drift == "meter":
                driver.native_memory_meter = None
            else:
                driver.resolve_identity = lambda: replace(  # type: ignore[method-assign]
                    original_resolve(), fingerprint="assessment-identity-drift"
                )
            return original_archive(meter, measurement)

        meter._over_limit_archive = drifted_archive  # type: ignore[method-assign]
        with pytest.raises(CalibrationError, match="archive failed"):
            _complete_over_limit_window(meter)
        assert controller.journal.archives("native_memory_assessments") == []
    finally:
        if meter is not None:
            meter.close()
        driver.native_memory_meter = None
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_daemon_archives_exact_over_target_assessment_idempotently_and_privately(
    tmp_path: Path,
) -> None:
    daemon, driver, controller, child = _native_failure_daemon(tmp_path)
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        meter = daemon._ensure_native_memory_meter()
        assert meter is not None
        original_append = controller.journal.append_archive
        replayed: list[dict[str, object]] = []

        def append_and_replay(kind: str, record: dict[str, object]) -> dict[str, object]:
            first = original_append(kind, record)
            second = original_append(kind, record)
            assert first == second
            replayed.extend([first, second])
            return second

        controller.journal.append_archive = append_and_replay  # type: ignore[method-assign]
        measurement = _complete_over_limit_window(meter)
        measurement.verify()
        assert measurement.growth_bytes > measurement.limit_bytes
        assert len(replayed) == 2
        assert replayed[0]["archiveOrdinal"] == replayed[1]["archiveOrdinal"] == 1
        records = controller.journal.archives("native_memory_assessments")
        assert len(records) == 1
        artifact = next((controller.journal.archive_path / "native_memory_assessments").glob("*.json"))
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        record = records[0]
        assert record["schema"] == 1
        assert record["kind"] == "native_memory_assessments"
        assert record["classification"] == "target_exceeded"
        assert record["archiveOrdinal"] == 1
        assert record["measurement"]["final_hwm_bytes"] == 1_000 + 200 * 1024 * 1024
        assert record["measurement"]["growth_bytes"] == 200 * 1024 * 1024
        expected_outcome = hashlib.sha256(
            json.dumps(
                {
                    "kind": "native_memory_assessments",
                    "schema": 1,
                    "classification": "target_exceeded",
                    "measurement": record["measurement"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert record["outcomeId"] == expected_outcome
        assert set(record) == {
            "schema",
            "outcomeId",
            "phase",
            "ordinal",
            "kind",
            "classification",
            "measurement",
            "archiveOrdinal",
            "archivedAt",
        }
        assert daemon._native_memory_archive_diagnostic is None
        assert daemon._native_handshake == {"state": "running", "failure": ""}
        durable = controller.journal.read()
        assert durable["nativeCalibration"] is False
        assert durable["calibrationEvidence"] == {}
        assert durable["qualification"]["totalAccepted"] == 0
        assert controller.status().state == ControllerState.NATIVE_CALIBRATION
        assert driver.native_memory_meter is meter
    finally:
        if meter is not None:
            meter.close()
        driver.native_memory_meter = None
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_real_runner_carries_updates_overage_through_resume_and_finalization(tmp_path: Path) -> None:
    """An over-target updates window is assessed, then the real runner still seals 100/normal/0."""

    from .test_session_native import _NativeRunnerHarness

    harness = _NativeRunnerHarness(tmp_path)
    journal_path = tmp_path / "runner-assessment-journal.json"
    journal = Journal(journal_path)
    durable = copy.deepcopy(DEFAULT_STATE)
    durable.update({"state": ControllerState.NATIVE_CALIBRATION.value, "generation": 7})
    journal.write(durable)
    controller = Controller(journal, driver=harness.driver, recover_on_restart=False)
    daemon = object.__new__(SessionDaemon)
    daemon.driver = harness.driver
    daemon.controller = controller
    daemon._native_memory_archive_diagnostic = None
    daemon._native_handshake = {"state": "running", "failure": ""}
    meter = harness.meter
    original_status = meter._status
    original_finish = meter.finish_window
    over_limit = [False]

    def status_with_updates_overage() -> tuple[int, int, int]:
        hwm, rss, uid = original_status()
        if over_limit[0]:
            return (
                hwm + meter._pending_window.limit_bytes + 1
                if meter._pending_window
                else hwm + (16 * 1024 * 1024) + 1,
                rss,
                uid,
            )
        return hwm, rss, uid

    def finish_window_with_assessment(*args: object, **kwargs: object) -> NativeMemoryMeasurement:
        pending = args[0]
        over_limit[0] = pending.phase == "updates"
        try:
            return original_finish(*args, **kwargs)
        finally:
            over_limit[0] = False

    meter._status = status_with_updates_overage  # type: ignore[method-assign]
    meter.finish_window = finish_window_with_assessment  # type: ignore[method-assign]
    meter._over_limit_archive = lambda measurement: daemon._archive_native_memory_assessment(  # type: ignore[method-assign]
        meter, measurement
    )
    try:
        evidence = harness.run()
        evidence.verify_seal()
        assert evidence.memory_measurements[1].growth_bytes > evidence.memory_measurements[1].limit_bytes
        assert evidence.memory_measurements[1].phase == "updates"
        assessments = controller.journal.archives("native_memory_assessments")
        assert len(assessments) == 1
        assert assessments[0]["classification"] == "target_exceeded"
        assert assessments[0]["measurement"] == evidence.memory_measurements[1].to_dict()
        assert evidence.render["nativeOrdinal"] == 100
        assert evidence.resume_render["nativeOrdinal"] == 0
        assert evidence.resume_update_count == evidence.render["updateCount"] + 1
        assert meter._pending_window is None
        status = controller.native_calibrated(
            evidence,
            expected_generation=7,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        )
        assert status.state is ControllerState.INPUT_CALIBRATION
        assessment = controller.native_resource_assessment()
        assert assessment["classification"] == "target_exceeded"
        assert assessment["current"] is True
        assert assessment["updates"]["withinTarget"] is False
        final_path = Path(evidence.capture_path)
        assert final_path.is_file()
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == evidence.capture_sha256
        assert all(not path.exists() for path in harness.released)
    finally:
        harness.close()


@pytest.mark.parametrize("archive_fault", ["append", "receipt"])
def test_daemon_archive_diagnostic_stays_private_and_hard_fails_assessment(
    tmp_path: Path, archive_fault: str
) -> None:
    daemon, driver, controller, child = _native_failure_daemon(tmp_path)
    meter: LinuxProcStatusVmHwmReset5 | None = None
    secret = "PRIVATE_ARCHIVE_FAILURE_SENTINEL"
    append_calls = 0
    try:
        meter = daemon._ensure_native_memory_meter()
        assert meter is not None
        if archive_fault == "append":

            def fail_append(_kind: str, _record: dict[str, object]) -> dict[str, object]:
                nonlocal append_calls
                append_calls += 1
                raise JournalError(secret)

            controller.journal.append_archive = fail_append  # type: ignore[method-assign]
        else:

            def invalid_receipt(*_args: object) -> NativeMemoryAssessmentArchiveReceipt:
                return NativeMemoryAssessmentArchiveReceipt("0" * 64, "0" * 64, 0, True)

            daemon._archive_native_memory_assessment = invalid_receipt  # type: ignore[method-assign]

        with pytest.raises(CalibrationError, match="archive failed") as failure:
            _complete_over_limit_window(meter)
        if archive_fault == "append":
            assert isinstance(failure.value.__cause__, JournalError)
        else:
            assert str(failure.value.__cause__) == "native resource assessment receipt is invalid"
        assert daemon._native_memory_archive_diagnostic == "native_memory_assessment_archive_failed"
        if archive_fault == "append":
            assert append_calls == 1
        assert secret not in repr(daemon._native_handshake)
        assert secret not in repr(controller.status())
        assert secret not in repr(controller.journal.read())
        assert controller.journal.archives("native_memory_assessments") == []
        assert controller._native_calibrated is False
        assert controller.status().state == ControllerState.NATIVE_CALIBRATION
    finally:
        if meter is not None:
            meter.close()
        driver.native_memory_meter = None
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize(
    "growth_bytes",
    [NATIVE_CONSTRUCTION_LIMIT_BYTES, NATIVE_CONSTRUCTION_LIMIT_BYTES - 1],
    ids=["at-limit", "below-limit"],
)
def test_daemon_under_limit_growth_returns_exact_measurement_without_archive(
    tmp_path: Path, growth_bytes: int
) -> None:
    daemon, driver, controller, child = _native_failure_daemon(tmp_path)
    meter: LinuxProcStatusVmHwmReset5 | None = None
    try:
        meter = daemon._ensure_native_memory_meter()
        assert meter is not None
        assert driver.native_memory_meter is meter
        assert meter._over_limit_archive is not None
        wired_callback = meter._over_limit_archive
        callback_calls: list[NativeMemoryMeasurement] = []

        def count_callback(
            measurement: NativeMemoryMeasurement,
        ) -> NativeMemoryAssessmentArchiveReceipt:
            callback_calls.append(measurement)
            return wired_callback(measurement)

        meter._over_limit_archive = count_callback
        uid = os.getuid()
        final_hwm = 1_000 + growth_bytes
        status = (1_000, 900, uid)
        statuses = iter([status, status, status, status, status, (final_hwm, 900, uid)])
        meter._status = lambda: next(statuses)  # type: ignore[method-assign]
        pending = meter.begin_window(
            "construction",
            0,
            limit_bytes=NATIVE_CONSTRUCTION_LIMIT_BYTES,
            before_reset=lambda record: NativeMemoryArchiveReceipt.for_record(
                record, archive_id=f"under-limit-{growth_bytes}"
            ),
        )

        result = meter.finish_window(
            pending,
            workload_start_monotonic=pending.reset_completed_monotonic,
            workload_end_monotonic=pending.reset_completed_monotonic,
            workload_start_ordinal=0,
            workload_end_ordinal=0,
        )
        expected = NativeMemoryMeasurement(
            method=pending.method,
            phase=pending.phase,
            ordinal=pending.ordinal,
            pid=pending.pid,
            process_start=pending.process_start,
            process_executable=pending.process_executable,
            identity_fingerprint=pending.identity_fingerprint,
            uid=pending.uid,
            kernel_release=pending.kernel_release,
            historical_hwm_bytes=pending.historical_hwm_bytes,
            rss_before_reset_bytes=pending.rss_before_reset_bytes,
            reset_completed_monotonic=pending.reset_completed_monotonic,
            post_reset_hwm_bytes=pending.post_reset_hwm_bytes,
            rss_after_reset_bytes=pending.rss_after_reset_bytes,
            workload_start_monotonic=pending.reset_completed_monotonic,
            workload_end_monotonic=pending.reset_completed_monotonic,
            workload_start_ordinal=0,
            workload_end_ordinal=0,
            final_hwm_bytes=final_hwm,
            final_rss_bytes=900,
            growth_bytes=growth_bytes,
            limit_bytes=NATIVE_CONSTRUCTION_LIMIT_BYTES,
        )

        result.verify()
        expected.verify()
        assert result == expected
        assert callback_calls == []
        assert controller.journal.archives("native_memory_failures") == []
        assert controller.journal.archives("native_memory_assessments") == []
        assert daemon._native_memory_archive_diagnostic is None
        assert daemon._native_handshake == {"state": "running", "failure": ""}
        durable = controller.journal.read()
        assert durable["nativeCalibration"] is False
        assert durable["calibrationEvidence"] == {}
        assert durable["qualification"]["totalAccepted"] == 0
        assert controller.status().state == ControllerState.NATIVE_CALIBRATION
        assert driver.native_memory_meter is meter
    finally:
        if meter is not None:
            meter.close()
        driver.native_memory_meter = None
        driver.close()
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize(
    ("construction_extra", "updates_extra", "expected_classification"),
    [
        (0, 0, "within_targets"),
        (1, 0, "target_exceeded"),
        (0, 1, "target_exceeded"),
        (1, 1, "target_exceeded"),
    ],
    ids=["both-at-target", "construction-plus-one", "updates-plus-one", "both-plus-one"],
)
def test_controller_seals_resource_assessment_from_both_reset5_windows(
    tmp_path: Path,
    construction_extra: int,
    updates_extra: int,
    expected_classification: str,
) -> None:
    """The controller derives the durable result from both exact reset5 windows."""

    from tests.test_session import native_calibration

    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        base = native_calibration(process_start="resource-assessment")
        construction, updates = base.memory_measurements
        measurements = (
            replace(
                construction,
                growth_bytes=construction.limit_bytes + construction_extra,
                final_hwm_bytes=construction.post_reset_hwm_bytes
                + construction.limit_bytes
                + construction_extra,
            ),
            replace(
                updates,
                growth_bytes=updates.limit_bytes + updates_extra,
                final_hwm_bytes=updates.post_reset_hwm_bytes + updates.limit_bytes + updates_extra,
            ),
        )
        evidence = replace(base, memory_measurements=measurements, fingerprint="", evidence_hash="")
        evidence = replace(evidence, fingerprint=evidence.content_fingerprint()).with_hash()
        status = daemon.controller.native_calibrated(
            evidence,
            expected_generation=7,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        )
        assert status.state is ControllerState.INPUT_CALIBRATION
        assert not daemon.driver.guardian.armed
        assessment = daemon.controller.native_resource_assessment()
        assert assessment["classification"] == expected_classification
        assert assessment["current"] is True
        assert assessment["evidenceHash"] == evidence.evidence_hash
        assert assessment["construction"]["growthBytes"] == measurements[0].growth_bytes
        assert assessment["updates"]["growthBytes"] == measurements[1].growth_bytes
        assert assessment["construction"]["withinTarget"] is (construction_extra == 0)
        assert assessment["updates"]["withinTarget"] is (updates_extra == 0)

        native_record = daemon.controller._calibration_evidence["native"]
        stored = native_record["resourceAssessment"]
        original_target = stored["construction"]["targetBytes"]
        stored["construction"]["targetBytes"] = original_target + 1
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        stored["construction"]["targetBytes"] = original_target
        original_growth = stored["updates"]["growthBytes"]
        stored["updates"]["growthBytes"] = original_growth + 1
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        stored["updates"]["growthBytes"] = original_growth
        original_classification = stored["classification"]
        stored["classification"] = (
            "within_targets" if original_classification == "target_exceeded" else "target_exceeded"
        )
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        stored["classification"] = original_classification
        stored["current"] = True
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        stored.pop("current")

        # Changing a measurement and its visible phase facts together must not
        # bypass the evidence fingerprint/hash binding.
        raw_updates = native_record["memory_measurements"][1]
        original_updates = copy.deepcopy(raw_updates)
        raw_updates["growth_bytes"] += 1
        raw_updates["final_hwm_bytes"] += 1
        stored["updates"] = {
            "growthBytes": raw_updates["growth_bytes"],
            "targetBytes": raw_updates["limit_bytes"],
            "withinTarget": raw_updates["growth_bytes"] <= raw_updates["limit_bytes"],
        }
        stored["classification"] = (
            "target_exceeded"
            if not stored["updates"]["withinTarget"] or not stored["construction"]["withinTarget"]
            else "within_targets"
        )
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        native_record["memory_measurements"][1] = original_updates
        stored["updates"] = {
            "growthBytes": original_updates["growth_bytes"],
            "targetBytes": original_updates["limit_bytes"],
            "withinTarget": original_updates["growth_bytes"] <= original_updates["limit_bytes"],
        }
        stored["classification"] = original_classification
        original_assessment = native_record.pop("resourceAssessment")
        assert daemon.controller.native_resource_assessment()["classification"] == "not_measured"
        native_record["resourceAssessment"] = original_assessment

        daemon.controller._invalidate_live_calibration("resource assessment regression")
        historical = daemon.controller.native_resource_assessment()
        assert historical["classification"] == expected_classification
        assert historical["current"] is False
        restarted = Controller(daemon.controller.journal, driver=driver, recover_on_restart=True)
        restarted_assessment = restarted.native_resource_assessment()
        assert restarted_assessment["classification"] == expected_classification
        assert restarted_assessment["current"] is False
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_resource_assessment_is_not_current_during_replacement_gate(tmp_path: Path) -> None:
    daemon, driver, _identity, child = _construction_daemon(tmp_path)
    try:
        from tests.test_session import native_calibration

        first = native_calibration(process_start="replacement-a")
        daemon.controller.native_calibrated(
            first,
            expected_generation=7,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        )
        assert daemon.controller.native_resource_assessment()["current"] is True
        daemon.controller.pause("prepare native replacement")
        started = daemon.controller.start()
        assert started.state is ControllerState.NATIVE_CALIBRATION
        pending = daemon.controller.native_resource_assessment()
        assert pending["current"] is False
        assert pending["evidenceHash"] == first.evidence_hash

        second = native_calibration(process_start="replacement-b")
        daemon.controller.native_calibrated(
            second,
            expected_generation=started.generation,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        )
        committed = daemon.controller.native_resource_assessment()
        assert committed["current"] is True
        assert committed["evidenceHash"] == second.evidence_hash
        assert committed["evidenceHash"] != first.evidence_hash
    finally:
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_native_runner_samples_hwm_before_constructor_and_each_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    driver, identity, child = _native_driver(tmp_path)
    clock = driver._native_test_clock  # type: ignore[attr-defined]
    order: list[tuple[str, str | int]] = []
    sequence = [0]
    decoded: dict[Path, int] = {}
    metrics_by_path: dict[Path, dict[str, object]] = {}

    def provider(observed: WindowIdentity, phase: str, ordinal: int) -> NativeRenderObservation:
        order.append(("provider", phase))
        assert observed.fingerprint == identity.fingerprint
        if phase == "construct":
            clock[0] += 0.1
        elif phase == "held" and ordinal == 0:
            clock[0] += 0.1
        elif phase == "updates" and ordinal == 1:
            clock[0] += 0.1
        elif phase == "updates":
            clock[0] += 1.0
        elif phase == "held" and ordinal == 100:
            clock[0] += 1.0
        elif phase == "resume":
            clock[0] += 0.1
        observation = _native_observation(
            tmp_path,
            identity,
            phase,
            ordinal,
            sequence=len(metrics_by_path) + 1,
            capture_monotonic=clock[0],
        )
        assert observation.capture_path is not None
        metrics_by_path[Path(observation.capture_path)] = observation.metrics
        return observation

    def decode(_path: Path, *, identity: WindowIdentity, decoder: object):
        if _path not in decoded:
            sequence[0] += 1
            decoded[_path] = sequence[0]
        current = decoded[_path]
        return SimpleNamespace(
            frame=SimpleNamespace(
                boot="boot-a",
                sequence=current,
                uptime_ms=current * 1000,
                crc32=current,
                snapshot=SimpleNamespace(value={"render": metrics_by_path[_path]}),
            )
        )

    driver.native_measurement_provider = provider
    driver.decode_native_capture = decode  # type: ignore[method-assign]
    resume_order: list[str] = []

    def resume_required(_identity: WindowIdentity, boundary: object) -> None:
        assert isinstance(boundary, NativeResumeBoundary)
        assert boundary.render["nativePhase"] == "held"
        assert boundary.render["nativeOrdinal"] == 100
        order.append(("callback", "resume-required"))
        resume_order.append("resume-required")

    driver.notify_native_resume_required = resume_required  # type: ignore[method-assign]
    driver.notify_native_resume_ready = (  # type: ignore[method-assign]
        lambda _identity, _evidence, _resumed: (
            order.append(("callback", "resume-ready")),
            resume_order.append("resume-ready"),
        )[-1]
    )
    try:
        evidence = NativeCalibrationRunner(driver, monotonic=lambda: clock[0]).run()
        assert order[0] == ("provider", "construct")
        assert sum(kind == "provider" and phase == "updates" for kind, phase in order) == 99
        assert evidence.construction_update_count == 1
        assert evidence.held_update_count == 3
        assert evidence.render["updateCount"] == 103
        assert evidence.resume_update_count == 104
        assert order[-3:] == [
            ("callback", "resume-required"),
            ("provider", "resume"),
            ("callback", "resume-ready"),
        ]
        assert resume_order == ["resume-required", "resume-ready"]
    finally:
        driver.close()
        driver.native_memory_meter.close()  # type: ignore[union-attr]
        child.terminate()
        child.wait(timeout=2)


def test_native_runner_rejects_boot_churn_across_101_bound_frames(tmp_path: Path) -> None:
    driver, identity, child = _native_driver(tmp_path)
    clock = driver._native_test_clock  # type: ignore[attr-defined]
    sequence = [0]
    decoded: dict[Path, int] = {}
    metrics_by_path: dict[Path, dict[str, object]] = {}

    def decode(_path: Path, *, identity: WindowIdentity, decoder: object):
        if _path not in decoded:
            sequence[0] += 1
            decoded[_path] = sequence[0]
        current = decoded[_path]
        boot = "boot-a" if current <= 50 else "boot-b"
        return SimpleNamespace(
            frame=SimpleNamespace(
                boot=boot,
                sequence=current,
                uptime_ms=current * 1000,
                crc32=current,
                snapshot=SimpleNamespace(value={"render": metrics_by_path[_path]}),
            )
        )

    driver.decode_native_capture = decode  # type: ignore[method-assign]

    def provider(observed: WindowIdentity, phase: str, ordinal: int) -> NativeRenderObservation:
        if phase == "construct":
            clock[0] += 0.1
        elif phase == "held" and ordinal == 0:
            clock[0] += 0.1
        elif phase == "updates" and ordinal == 1:
            clock[0] += 0.1
        elif phase == "updates":
            clock[0] += 1.0
        elif phase == "held" and ordinal == 100:
            clock[0] += 1.0
        elif phase == "resume":
            clock[0] += 0.1
        observation = _native_observation(
            tmp_path,
            identity,
            phase,
            ordinal,
            sequence=len(metrics_by_path) + 1,
            capture_monotonic=clock[0],
        )
        assert observation.capture_path is not None
        metrics_by_path[Path(observation.capture_path)] = observation.metrics
        return observation

    driver.native_measurement_provider = provider
    try:
        with pytest.raises(CalibrationError, match="boot|identity"):
            NativeCalibrationRunner(driver, monotonic=lambda: clock[0]).run()
    finally:
        driver.close()
        driver.native_memory_meter.close()  # type: ignore[union-attr]
        child.terminate()
        child.wait(timeout=2)


def test_measured_anchor_installation_rejects_manual_provenance_and_stale_identity(tmp_path: Path) -> None:
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._measured_anchors = {}
    identity = WindowIdentity(
        1,
        os.getpid(),
        "WowB.exe",
        "World of Warcraft",
        "wowb.exe,wowb.exe",
        2,
        (0, 0, 1928, 1092),
        (0, 0, 1920, 1080),
        1,
        "start",
        ":test",
        "identity-a",
        root_xid=0x374,
        managed_frame_xid=1,
        target_event_xids=frozenset({1, 2}),
    )
    backend.resolve_identity = lambda **_kwargs: identity
    backend.is_descendant = lambda xid, ancestor: xid == ancestor
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "anchor.lock")
    observed = time.monotonic()
    anchors = tuple(
        MeasuredAnchor(
            action,
            10,
            10,
            identity.fingerprint,
            identity.child_xid,
            f"capture-{index}",
            index + 1,
            "manual-anchor",
            observed,
        )
        for index, action in enumerate(sorted(driver.POINTER_ACTIONS))
    )
    try:
        with pytest.raises(DriverError, match="measure_pointer_anchor"):
            driver.install_measured_anchors(anchors)
        capture = tmp_path / "anchor.png"
        Image.new("RGB", (32, 32), (0, 0, 0)).save(capture)
        positive_driver = DesktopDriver(
            profile=profile,
            backend=RecordingBackend(),
            lease_path=tmp_path / "anchor-positive.lock",
        )
        positive_driver._identity = lambda **_kwargs: identity  # type: ignore[method-assign]
        generation = positive_driver.guardian.arm(_evidence(tmp_path))
        valid_list: list[MeasuredAnchor] = []
        for index, action in enumerate(sorted(positive_driver.POINTER_ACTIONS)):
            anchor_capture = tmp_path / f"anchor-{index}.png"
            Image.new("RGB", (32, 32), (0, 0, 0)).save(anchor_capture)
            capture_digest = hashlib.sha256(anchor_capture.read_bytes()).hexdigest()
            valid_list.append(
                positive_driver.build_measured_anchor(
                    action=action,
                    x=10,
                    y=10,
                    capture_path=anchor_capture,
                    capture_id=capture_digest,
                    capture_sequence=index + 1,
                    capture_monotonic=observed + index * 0.01,
                    scene={
                        "dismiss_disconnect": "disconnect_dialog",
                        "reconnect_game": "game_client",
                        "select_arthur": "character_select",
                        "enter_world": "character_select",
                        "open_ah": "world",
                        "logout": "world",
                    }[action],
                    bounds=(0, 0, 1920, 1080),
                    target_xid=identity.child_xid,
                    target_ancestry=(identity.child_xid,),
                    generation=generation,
                )
            )
        valid = tuple(valid_list)
        positive_driver.install_measured_anchors(valid)
        unproved = tuple(replace(anchor, facts=()) for anchor in valid)
        with pytest.raises(DriverError, match="facts|proof|control|visual"):
            positive_driver.install_measured_anchors(unproved)
        stale_identity = WindowIdentity(
            identity.xid,
            identity.pid,
            identity.executable,
            identity.title,
            identity.wm_class,
            identity.child_xid,
            identity.outer_geometry,
            identity.child_geometry,
            identity.foreground_xid,
            identity.process_start,
            identity.display,
            "identity-b",
            root_xid=identity.root_xid,
            managed_frame_xid=identity.managed_frame_xid,
            target_event_xids=identity.target_event_xids,
        )
        positive_driver._identity = lambda **_kwargs: stale_identity  # type: ignore[method-assign]
        with pytest.raises(DriverError, match="stale|identity|calibration"):
            positive_driver.install_measured_anchors(valid)
        positive_driver.close()
    finally:
        driver.close()


def test_generation_revoke_after_begin_action_blocks_backend_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A revoke between admission and the backend call must be a hard fence."""

    backend = RecordingBackend()
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "dispatch.lock")
    evidence = _evidence(tmp_path)
    driver.input_calibration(evidence)
    generation = driver.guardian.arm(evidence)
    original_begin = driver.guardian.begin_action

    def revoke_after_admission(*args: object, **kwargs: object):
        action = original_begin(*args, **kwargs)
        driver.revoke("test revoke at dispatch barrier")
        return action

    monkeypatch.setattr(driver.guardian, "begin_action", revoke_after_admission)
    try:
        with pytest.raises(DriverError, match="revoked|stale|cancel"):
            driver.run(
                "open_chat",
                generation,
                precondition=lambda _identity: True,
                postcondition=lambda _identity: True,
            )
        assert backend.events == []
        assert not driver.guardian.armed
    finally:
        driver.close()


def test_spawn_registration_revoke_cannot_let_a_gated_child_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancellation during Popen must cover the registration gap as well."""

    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._active_processes = set()
    backend._active_process_groups = set()
    backend._process_generations = {}
    backend._process_calibration_tickets = {}
    backend._process_lock = threading.RLock()
    backend._cancel_active = threading.Event()
    backend._active_generation = 2
    backend._canceled_generations = set()
    backend._admission = lambda _generation, callback: callback()
    backend._register_process_group = lambda _generation, _pid: None
    backend._unregister_process_group = lambda _pid: None
    backend._held_keys = set()
    backend._held_buttons = set()
    backend._release_command = lambda _args: True
    backend._publish_holds = None
    terminated: list[int] = []

    class GatedChild:
        pid = 7401

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self, *, timeout: float | None = None):
            # If the cancellation fence is lost, _run would incorrectly
            # accept this child as a successful desktop action.
            if timeout is not None and self.returncode is None:
                time.sleep(min(timeout, 0.01))
            return "", ""

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = -15
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    child = GatedChild()

    def terminate(process: GatedChild) -> None:
        terminated.append(process.pid)
        process.terminate()

    backend._terminate_process = terminate

    def spawn(*_args: object, **_kwargs: object) -> GatedChild:
        # Revoke before _run receives the Popen return value.  This is the
        # precise Popen-to-active-process registration window under review.
        backend.release_held_input()
        return child

    monkeypatch.setattr("crowley.session.driver.subprocess.Popen", spawn)
    try:
        with pytest.raises(DriverError, match="cancel"):
            backend._run(
                ["xdotool", "keydown", "F13"],
                timeout=0.2,
                generation=1,
                held_keys={"F13"},
            )
        assert child.returncode == -9
        assert terminated == []
        assert not backend._active_processes
    finally:
        backend._cancel_active.set()


def test_real_executed_wrapper_success_and_process_group_cancellation(tmp_path: Path) -> None:
    """Exercise the production wrapper with harmless processes, including revoke."""

    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    backend = object.__new__(X11Backend)
    backend.profile = profile
    backend.xauthority = profile.xauthority
    backend.input_tool = fixture_input_tool(tmp_path / "tool")
    backend._active_processes = set()
    backend._active_process_groups = set()
    backend._process_generations = {}
    backend._process_calibration_tickets = {}
    backend._process_lock = threading.RLock()
    backend._cancel_active = threading.Event()
    backend._canceled_generations = set()
    backend._active_generation = None
    backend._held_keys = set()
    backend._held_buttons = set()
    backend._release_command = lambda _args: True
    backend._publish_holds = None

    success = backend._run(["/usr/bin/true"], timeout=2)
    assert success.returncode == 0
    assert backend.active_process_groups == frozenset()

    result: dict[str, BaseException] = {}

    def run_sleep() -> None:
        try:
            backend._run(["/usr/bin/sleep", "30"], timeout=10)
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=run_sleep)
    worker.start()
    assert _wait_for(lambda: bool(backend.active_process_groups))
    backend.release_held_input()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert isinstance(result.get("error"), DriverError)
    assert backend._cancel_active.is_set()
    assert backend.active_process_groups == frozenset()


def test_sealed_input_and_native_facts_cannot_be_replaced_after_issue(tmp_path: Path) -> None:
    input_evidence = _evidence(tmp_path)
    input_evidence.verify_seal()
    with pytest.raises(CalibrationError, match="seal|fingerprint|tamper|evidence"):
        replace(input_evidence, synthetic_key_source=999).verify_seal()

    capture = tmp_path / "capture.png"
    capture.write_bytes(b"native-capture")
    capture_sha256 = hashlib.sha256(capture.read_bytes()).hexdigest()
    native = NativeCalibrationEvidence._issue(
        fingerprint="a" * 64,
        process_pid=os.getpid(),
        process_executable="/usr/bin/python3",
        process_start="owned-process-start",
        window_xid=1,
        child_xid=2,
        geometry=(0, 0, 1920, 1080),
        xinput_hierarchy=input_evidence.xinput_hierarchy,
        crop=(80, 48, 256, 256),
        boot="boot-a",
        sequence=101,
        crc32=0xABCD,
        render=_metrics(102),
        construction_update_count=1,
        held_update_count=2,
        resume_update_count=103,
        memory_measurements=(
            NativeMemoryMeasurement(
                method="linux_proc_status_vmhwm_reset5",
                phase="construction",
                ordinal=0,
                pid=os.getpid(),
                process_start="owned-process-start",
                process_executable="/usr/bin/python3",
                identity_fingerprint="owned-native-identity",
                uid=os.getuid(),
                kernel_release="test-kernel",
                historical_hwm_bytes=100,
                rss_before_reset_bytes=100,
                reset_completed_monotonic=1.0,
                post_reset_hwm_bytes=100,
                rss_after_reset_bytes=100,
                workload_start_monotonic=1.1,
                workload_end_monotonic=1.2,
                workload_start_ordinal=0,
                workload_end_ordinal=0,
                final_hwm_bytes=100,
                final_rss_bytes=100,
                growth_bytes=0,
                limit_bytes=128 * 1024 * 1024,
            ),
            NativeMemoryMeasurement(
                method="linux_proc_status_vmhwm_reset5",
                phase="updates",
                ordinal=0,
                pid=os.getpid(),
                process_start="owned-process-start",
                process_executable="/usr/bin/python3",
                identity_fingerprint="owned-native-identity",
                uid=os.getuid(),
                kernel_release="test-kernel",
                historical_hwm_bytes=100,
                rss_before_reset_bytes=100,
                reset_completed_monotonic=2.0,
                post_reset_hwm_bytes=100,
                rss_after_reset_bytes=100,
                workload_start_monotonic=2.1,
                workload_end_monotonic=2.2,
                workload_start_ordinal=1,
                workload_end_ordinal=100,
                final_hwm_bytes=100,
                final_rss_bytes=100,
                growth_bytes=0,
                limit_bytes=16 * 1024 * 1024,
            ),
        ),
        native_identity_fingerprint="owned-native-identity",
        resume_sequence=102,
        resume_crc32=0xABCE,
        resume_render=_metrics(103, phase="normal", ordinal=0),
        resume_capture_monotonic=2.0,
        capture_path=str(capture),
        xauthority=str(tmp_path / "Xauthority"),
        wine_prefix="/owned/wine",
        capture_sha256=capture_sha256,
        capture_monotonic=1.0,
        preconstruction_fingerprint="owned-preconstruction",
    )
    native.verify_seal()
    with pytest.raises(CalibrationError, match="seal|fingerprint|tamper|evidence"):
        replace(native, boot="boot-replaced").verify_seal()


def test_guardian_rejects_nonzero_delivered_modifier_state(tmp_path: Path) -> None:
    records = parse_xi2_records(FULL_XI2_TRACE, require_event=True)
    guardian = InputGuardian(tmp_path / "modifier.lock", parent_pid=os.getppid())
    try:
        generation = guardian.arm(_evidence(tmp_path))
        guardian.begin_action(
            generation,
            target_xid=TARGET,
            source_device=5,
            master_device=3,
            event_classes={
                EventClass.RAW_KEY_PRESS.value,
                EventClass.KEY_PRESS.value,
                EventClass.RAW_KEY_RELEASE.value,
                EventClass.KEY_RELEASE.value,
            },
            device_by_class={
                EventClass.RAW_KEY_PRESS.value: 3,
                EventClass.KEY_PRESS.value: 5,
                EventClass.RAW_KEY_RELEASE.value: 3,
                EventClass.KEY_RELEASE.value: 5,
            },
            detail=8,
        )
        assert guardian.observe(records[0])
        nonzero_modifier = replace(records[1], modifiers=(1, 0, 0))
        assert not guardian.observe(nonzero_modifier)
        assert not guardian.armed
    finally:
        guardian.close()


def test_guardian_close_reports_failed_release_and_keeps_fallback(tmp_path: Path) -> None:
    failed = [True]

    def release() -> None:
        if failed[0]:
            raise DriverError("native release failed")

    guardian = InputGuardian(
        tmp_path / "close-failure.lock",
        parent_pid=os.getppid(),
        release_input=release,
        watch_interval=0.01,
    )
    try:
        generation = guardian.arm(_evidence(tmp_path))
        guardian.begin_action(
            generation,
            target_xid=TARGET,
            source_device=5,
            master_device=3,
            event_classes={EventClass.KEY_PRESS.value},
            held_keys={"F13"},
        )
        guardian.publish_current_holds(generation, {"F13"}, set())
        with pytest.raises((GuardianError, DriverError), match="release|close|held"):
            guardian.close()
        assert guardian.held
        assert guardian.lease.held
    finally:
        failed[0] = False
        guardian.close()


def test_delayed_calibration_ticket_due_after_revoke_never_spawns_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timers: list[object] = []

    class DeferredTimer:
        def __init__(self, _delay: float, callback, args: tuple[object, ...]):
            self.callback = callback
            self.args = args
            self.daemon = False

        def start(self) -> None:
            timers.append(self)

    monkeypatch.setattr("crowley.session.driver.threading.Timer", DeferredTimer)
    backend = RecordingBackend()
    profile = DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority"))
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "due.lock")
    target = OwnedWindow(
        os.getpid(),
        TARGET,
        OwnedGtkCalibrationWindow.TITLE,
        (0, 0, 480, 240),
        SimpleNamespace(poll=lambda: None),  # type: ignore[arg-type]
        frozenset({TARGET}),
        "/usr/bin/python3",
        "owned-target-start",
        TARGET + 1,
        (0, 0, 500, 260),
        frozenset({TARGET, TARGET + 1}),
    )
    try:
        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=0.25, calibration_ticket=ticket)
        assert len(timers) == 1
        driver.revoke("human response after timeout plus margin")
        timers[0].callback(*timers[0].args)  # type: ignore[attr-defined]
        assert backend.events == []
        assert driver.calibration_executions == 0
        assert driver.calibration_due_count == 1
    finally:
        driver.close()


def test_valid_calibration_ticket_executes_before_revoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    timers: list[object] = []

    class DeferredTimer:
        def __init__(self, _delay: float, callback, args: tuple[object, ...]):
            self.callback = callback
            self.args = args
            self.daemon = False

        def start(self) -> None:
            timers.append(self)

    monkeypatch.setattr("crowley.session.driver.threading.Timer", DeferredTimer)
    backend = RecordingBackend()
    driver = DesktopDriver(
        profile=DriverProfile(display=":test", xauthority=str(tmp_path / "Xauthority")),
        backend=backend,
        lease_path=tmp_path / "due-valid.lock",
    )
    target = _WorkflowTarget().window
    try:
        driver.begin_calibration_operation()
        ticket = driver.issue_calibration_ticket(target)
        driver.queue_delayed_calibration_action(target, delay=120.5, calibration_ticket=ticket)
        assert len(timers) == 1
        timers[0].callback(*timers[0].args)  # type: ignore[attr-defined]
        assert backend.events == [{"name": "calibration_focus", "xid": TARGET}]
        assert driver.calibration_executions == 1
        assert driver.calibration_rejected_count == 0
    finally:
        driver.close()


def test_synthetic_readiness_waits_for_open_stream_releases(tmp_path: Path) -> None:
    """A press pair cannot publish readiness while releases remain buffered."""

    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", buffering=1)
    partial = parse_xi2_records(FULL_XI2_TRACE, require_event=True)[:10]
    release = parse_xi2_records(FULL_XI2_TRACE, require_event=True)[10]
    # A complete subsequent Motion block flushes the release while the
    # observer's stdout remains open and idle.  DeviceChanged has no
    # rendered reason field in this synthetic pipe fixture, so use the
    # captured Motion record as the legal trailing block.
    trailing = replace(
        parse_xi2_records(FULL_XI2_TRACE, require_event=True)[-1],
        child_xid=TARGET,
        event_xid=TARGET,
    )
    process_events: list[XI2Event | None] = []

    class Process:
        poll_result: int | None = None

        def __init__(self) -> None:
            self.stdout = stdout

        def poll(self) -> int | None:
            return self.poll_result

        def terminate(self) -> None:
            self.poll_result = 0
            self.stdout.close()

        def wait(self, timeout: float | None = None) -> int:
            self.poll_result = 0
            return 0

        def kill(self) -> None:
            self.poll_result = -9

    process = Process()
    monitor = InputMonitor(":test", process_events.append, xauthority="Xauthority")
    monitor.observer.start = lambda: setattr(monitor.observer, "process", process)  # type: ignore[method-assign]
    window = _WorkflowTarget().window
    hierarchy = _WorkflowDriver(outside=False).input_hierarchy()
    probe_operations = (
        KeyboardProbeOperation(
            "open_chat",
            "",
            (KeyboardProbeStep("press", "Return"), KeyboardProbeStep("release", "Return")),
        ),
    )
    probe_keymap = (("Return", 36),)
    try:
        monitor.start()
        os.write(write_fd, _render_events(partial).encode())
        monitor.wait_for_event(
            lambda events: any(event.event_class is EventClass.BUTTON_PRESS for event in events),
            timeout=2,
        )
        assert not InputCalibrationWorkflow._has_synthetic(
            monitor.events(),
            window,
            5,
            4,
            hierarchy,
            keyboard_operations=probe_operations,
            keyboard_keymap=probe_keymap,
        )
        # The pipe stays open and idle after the final block; a new EVENT block
        # is supplied only to flush the complete ButtonRelease record.
        profile_events = [
            _event(
                EventClass.RAW_KEY_PRESS,
                master=3,
                source=5,
                timestamp=232008010,
                detail=36,
                child=None,
            ),
            _event(EventClass.KEY_PRESS, master=5, source=5, timestamp=232008011, detail=36),
            _event(
                EventClass.RAW_KEY_RELEASE,
                master=3,
                source=5,
                timestamp=232008012,
                detail=36,
                child=None,
            ),
            _event(EventClass.KEY_RELEASE, master=5, source=5, timestamp=232008013, detail=36),
        ]
        os.write(write_fd, _render_events([release, trailing, *profile_events]).encode())
        monitor.wait_for_event(
            lambda events: InputCalibrationWorkflow._has_synthetic(
                events,
                window,
                5,
                4,
                hierarchy,
                keyboard_operations=probe_operations,
                keyboard_keymap=probe_keymap,
            ),
            timeout=2,
        )
        assert InputCalibrationWorkflow._has_synthetic(
            monitor.events(),
            window,
            5,
            4,
            hierarchy,
            keyboard_operations=probe_operations,
            keyboard_keymap=probe_keymap,
        )
        assert process.poll() is None
    finally:
        os.close(write_fd)
        monitor.stop()
        stdout.close()


def test_calibration_human_response_budget_reaches_two_minutes_with_fake_clock() -> None:
    """The harmless due ticket cannot turn the 120-second human window into 250ms."""

    clock = [0.0]
    waits: list[float] = []
    driver = _WorkflowDriver(outside=False)
    original_start = driver.start_input_observer

    def start_observer(*, calibration: bool, callback):
        observer = original_start(calibration=calibration, callback=callback)

        def wait_for_human(predicate, *, timeout: float) -> None:
            waits.append(timeout)
            remaining = max(0.0, 119.0 - clock[0])
            clock[0] += min(timeout, remaining)
            if clock[0] >= 119.0 and not predicate(observer.events()):
                for event in observer.human:
                    observer._add(event)

        observer.wait_for_human = wait_for_human  # type: ignore[method-assign]
        return observer

    driver.start_input_observer = start_observer  # type: ignore[method-assign]
    workflow = InputCalibrationWorkflow(
        driver,
        display=":test",
        window_factory=_WorkflowTarget,
        monotonic=lambda: clock[0],
    )
    evidence = workflow.run(human_timeout=120)
    assert evidence.human_key_source == 9
    assert clock[0] >= 119.0
    assert waits
