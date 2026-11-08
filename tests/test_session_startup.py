"""Passive startup and persistent takeover evidence regressions."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest

from crowley.session.calibration import (
    GTK_LAYOUT_ROOT_CHILD,
    InputCalibrationWorkflow,
    OwnedGtkCalibrationWindow,
    xtest_sources,
)
from crowley.session.cli import SessionIPC
from crowley.session.controller import Controller, ControllerState
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import Sample
from crowley.session.driver import (
    DesktopDriver,
    DriverError,
    InputMonitor,
    RecordingBackend,
    WindowIdentity,
    X11Backend,
)
from crowley.session.guardian import InputGuardian
from crowley.session.input_events import EventClass, XI2Event, parse_xi2_records
from crowley.session.mail_loop import MailLoopBinding
from crowley.session.model import CLI_VERSION, LunaAdapter
from crowley.session.protocol import Frame, Snapshot
from crowley.session.startup import (
    InvalidationCause,
    StartupEnvironment,
    TakeoverDrillRecord,
    is_measured_human_action,
    mint_runtime_evidence,
    resolve_forwarded_echo_sources,
)
from tests.support.config import fixture_codex_path, fixture_host
from tests.x11_startup import start_private_xvfb


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="private Xvfb is unavailable")
def test_native_input_state_uses_xkb_system_abi_on_private_xvfb() -> None:
    """Read a private server's state and compare it with the declared XKB ABI."""

    server, display, library, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "640x480x24",
            "-ac",
        ),
        popen_kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE},
    )
    try:
        library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        library.XDefaultRootWindow.restype = ctypes.c_ulong
        root = int(library.XDefaultRootWindow(xdisplay))
        library.XCloseDisplay(xdisplay)

        backend = X11Backend.__new__(X11Backend)
        backend.profile = SimpleNamespace(display=display)
        observed = backend.observe_native_input_state(root)
        assert observed[1:] == ((), ())

        # Compare the production result with an independent ctypes declaration
        # matching XkbStateRec in the installed XKBstr.h, including its ushort
        # fields and natural padding.
        class XkbStateRec(ctypes.Structure):
            _fields_ = [
                ("group", ctypes.c_ubyte),
                ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort),
                ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte),
                ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte),
                ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte),
                ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte),
                ("ptr_buttons", ctypes.c_ushort),
            ]

        assert ctypes.sizeof(XkbStateRec) == 18
        direct = library.XOpenDisplay(display.encode())
        assert direct
        library.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(XkbStateRec)]
        library.XkbGetState.restype = ctypes.c_int
        try:
            state = XkbStateRec()
            assert library.XkbGetState(direct, 0x100, ctypes.byref(state)) == 0
            assert observed[0] == (state.locked_mods, state.latched_mods, state.base_mods)
        finally:
            library.XCloseDisplay(direct)
    finally:
        server.terminate()
        server.wait(timeout=5)


@pytest.mark.skipif(
    shutil.which("Xvfb") is None
    or shutil.which("xinput") is None
    or shutil.which("xdotool") is None
    or not Path("/usr/bin/python3.13").is_file(),
    reason="private native keyboard fixture is unavailable",
)
def test_stormwind_target_binding_and_interaction_chord_use_guarded_native_keyboard_path(
    tmp_path: Path,
) -> None:
    """Verify fixed route emissions through real GTK, XTEST, XI2, and Guardian admission."""

    from crowley.session import driver as driver_module

    window_host = urlparse(fixture_host(tmp_path).server_url).hostname
    assert window_host is not None
    expected_driver = (Path(__file__).resolve().parents[1] / "src/crowley/session/driver.py").resolve()
    assert Path(driver_module.__file__).resolve() == expected_driver
    assert (
        Path(driver_module.DesktopDriver._calibration_keyboard_operations.__code__.co_filename).resolve()
        == expected_driver
    )

    server, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1920x1080x24",
            "-ac",
        ),
        popen_kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE},
    )
    driver = None
    window_factory = None
    try:
        xauthority = tmp_path / "private-Xauthority"
        xauthority.touch(mode=0o600)
        xauthority.chmod(0o600)

        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
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
        try:
            root = x11.XDefaultRootWindow(xdisplay)
            assert x11.XWarpPointer(xdisplay, 0, root, 0, 0, 0, 0, 1809, 43) == 1
            x11.XSync(xdisplay, 0)
        finally:
            x11.XCloseDisplay(xdisplay)

        profile = driver_module.DriverProfile(display=display, xauthority=str(xauthority))
        backend = driver_module.X11Backend(profile)
        driver = driver_module.DesktopDriver(
            profile=profile,
            backend=backend,
            lease_path=tmp_path / "private-desktop.lock",
        )
        production_run = backend._run

        def private_xvfb_focus_adapter(argv, **kwargs):
            # Bare Xvfb has no EWMH window manager for ``windowactivate``.
            # Keep the same certified XID and Guardian command path, using
            # X11's direct focus request for this isolated fixture only.
            if argv[:3] == ["xdotool", "windowactivate", "--sync"]:
                argv = ["xdotool", "windowfocus", "--sync", argv[3]]
            return production_run(argv, **kwargs)

        backend._run = private_xvfb_focus_adapter
        driver.begin_calibration_operation()
        verifier_facts: dict[str, int] = {}
        verifier_env = os.environ.copy()
        verifier_env.update({"DISPLAY": display, "XAUTHORITY": str(xauthority)})

        def verifier_command(argv, *, display: str, xauthority: str, timeout: float = 5.0) -> str:
            del display, xauthority
            if argv == ["wmctrl", "-lp"]:
                if not verifier_facts:
                    return ""
                return (
                    f"0x{verifier_facts['xid']:x} -1 {verifier_facts['pid']} {window_host} "
                    f"{OwnedGtkCalibrationWindow.TITLE}\n"
                )
            if argv[:2] in (["xprop", "-id"], ["xwininfo", "-id"]):
                query_xid = int(argv[2], 16)
                allowed = {verifier_facts.get("xid", 0), verifier_facts.get("helper", 0)}
                if query_xid not in allowed:
                    raise AssertionError(f"private GTK verifier queried an unowned XID: {argv!r}")
            else:
                raise AssertionError(f"unexpected GTK verifier command: {argv!r}")
            result = subprocess.run(
                argv,
                env=verifier_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=max(0.001, timeout),
            )
            if argv == ["xwininfo", "-id", hex(verifier_facts["xid"]), "-tree"]:
                _root, parents, _geometries = driver_module._parse_xwininfo_tree(result.stdout)
                if len(parents) != 1:
                    raise AssertionError(f"private GTK target has an unexpected child tree: {parents!r}")
                helper = next(iter(parents))
                if "helper" not in verifier_facts:
                    verifier_facts["helper"] = helper
                elif verifier_facts["helper"] != helper:
                    raise AssertionError("private GTK helper changed during certification")
            return result.stdout

        window_factory = OwnedGtkCalibrationWindow(
            display=display,
            xauthority=str(xauthority),
            python_executable="/usr/bin/python3.13",
            layout_kind=GTK_LAYOUT_ROOT_CHILD,
            command=verifier_command,
        )
        original_verify = window_factory._verify

        def capture_identity(pid: int, xid: int, process, **kwargs):
            verifier_facts.update(pid=pid, xid=xid)
            return original_verify(pid, xid, process, **kwargs)

        window_factory._verify = capture_identity
        window = window_factory.create(
            timeout=10.0,
            startup_admission=driver.admit_calibration_target_process,
        )
        left, top, width, height = window.geometry
        assert width > 1 and height > 1
        # The synthetic proof must start from a real, certified point inside
        # the GTK client and must produce a nonzero measured displacement.
        # Isolate this setup warp to the private Xvfb display; the production
        # emitter below still performs and verifies its own guarded motion.
        setup_display = x11.XOpenDisplay(display.encode("ascii"))
        assert setup_display
        try:
            setup_root = x11.XDefaultRootWindow(setup_display)
            assert x11.XWarpPointer(setup_display, 0, setup_root, 0, 0, 0, 0, left + 8, top + 8) == 1
            x11.XSync(setup_display, 0)
        finally:
            x11.XCloseDisplay(setup_display)
        origin_x, origin_y, origin_window = backend.pointer_position()
        assert left <= origin_x < left + width and top <= origin_y < top + height, (
            origin_x,
            origin_y,
            window.geometry,
        )
        assert (origin_x, origin_y) != (left + width // 2, top + height // 2)

        # The first use of a private XTEST source causes this Xvfb to publish
        # DeviceChanged metadata. Prime only the private, certified target
        # before starting the measured observer; the operation under test
        # still runs through the unchanged production observer and validator.
        private_env = os.environ.copy() | {"DISPLAY": display, "XAUTHORITY": str(xauthority)}
        pinned_xdotool = str(backend.input_tool.executable)
        focus = subprocess.run(
            [pinned_xdotool, "windowfocus", "--sync", hex(window.xid)],
            env=private_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert focus.returncode == 0
        prime_key = subprocess.run(
            [pinned_xdotool, "key", "--", "F13"],
            env=private_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert prime_key.returncode == 0
        prime_x, prime_y = left + 9, top + 9
        prime_pointer = subprocess.run(
            [pinned_xdotool, "mousemove", "--sync", str(prime_x), str(prime_y)],
            env=private_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert prime_pointer.returncode == 0
        profile_facts = backend.xtest_input_profile(driver.input_hierarchy())
        modifiers, held_keys, held_buttons = backend.observe_native_input_state(profile_facts.root_xid)
        assert modifiers == (0, 0, 0)
        assert held_keys == ()
        assert held_buttons == ()
        runtime_keymap = backend.observe_keymap()
        runtime_levels = backend.observe_keymap_levels()
        runtime_modifier_masks = (
            backend.observe_keysym_modifier_mask("Shift_L"),
            backend.observe_keysym_modifier_mask("Control_L"),
        )
        runtime_profile = DesktopDriver._runtime_keyboard_profile(
            tuple(driver.COMMANDS), runtime_levels, runtime_keymap, modifiers, runtime_modifier_masks
        )
        runtime_rows = {(operation, payload): steps for operation, payload, steps in runtime_profile}
        assert backend.pointer_position()[:2] == (prime_x, prime_y)
        monitor = driver.start_input_observer(calibration=True)
        ticket = driver.issue_calibration_ticket(window)
        driver.focus_calibration_target(window, calibration_ticket=ticket)
        origin_x, origin_y, origin_window = backend.pointer_position()
        assert left <= origin_x < left + width and top <= origin_y < top + height
        assert ticket.contains(origin_window)

        owned = {(item.operation, item.payload): item for item in driver.calibration_keyboard_operations()}
        operations = (
            owned[("interact_target", "")],
            owned[("type_allowlisted_command", driver.TARGET_AUCTIONEER_COMMAND)],
            owned[("type_allowlisted_command", driver.CONDITIONAL_INTERACTION_BINDING_COMMAND)],
        )
        assert (
            ("press", "Shift_L"),
            ("press", "a"),
            ("release", "a"),
            ("release", "Shift_L"),
        ) in tuple(
            tuple((step.phase, step.keysym) for step in operations[1].steps[index : index + 4])
            for index in range(len(operations[1].steps) - 3)
        )
        assert any(step.keysym == "comma" for step in operations[2].steps)
        hierarchy = driver.input_hierarchy()
        keyboard_source, _pointer_source = xtest_sources(hierarchy)
        keymap = driver.calibration_keyboard_keymap()
        expected_count = 4 + 2 * sum(len(item.steps) for item in operations)

        driver.synthetic_target_keyboard_profile(window, operations=operations, calibration_ticket=ticket)
        key_events = {
            EventClass.RAW_KEY_PRESS,
            EventClass.KEY_PRESS,
            EventClass.RAW_KEY_RELEASE,
            EventClass.KEY_RELEASE,
        }
        monitor.collect_until(
            lambda events: (
                sum(
                    event.source_device == keyboard_source and event.event_class in key_events
                    for event in events
                )
                >= expected_count
            ),
            timeout=30.0,
        )
        observed_keyboard_events = monitor.events()
        source_events = [
            event for event in observed_keyboard_events if event.source_device == keyboard_source
        ]
        source_event_counts = {
            name: sum(event.event_class.value == name for event in source_events)
            for name in sorted({event.event_class.value for event in source_events})
        }
        assert len(source_events) == expected_count, (len(source_events), expected_count, source_event_counts)
        rows = InputCalibrationWorkflow._validate_keyboard_probe(
            observed_keyboard_events,
            keyboard_source,
            window,
            hierarchy,
            operations,
            keymap,
        )
        # The same real XI2 transitions must satisfy the passive-runtime
        # profile builder. This catches modifier pre/post-state mismatches
        # that the calibration-only profile could otherwise hide.
        assert all(runtime_rows[(operation, payload)] == steps for operation, payload, steps in rows)
        assert tuple((row[0], row[1]) for row in rows) == tuple(
            (item.operation, item.payload) for item in operations
        )
        assert not driver.guardian.armed
        assert driver.guardian.active_process_groups == frozenset({window.pid})
        assert not backend.active_process_groups
        assert not backend._process_calibration_tickets
        profile_facts = backend.xtest_input_profile(hierarchy)
        modifiers, held_keys, held_buttons = backend.observe_native_input_state(profile_facts.root_xid)
        assert modifiers == (0, 0, 0)
        assert held_keys == ()
        assert held_buttons == ()
    finally:
        if window_factory is not None and window_factory.window is not None:
            window_factory.close()
        if driver is not None:
            driver.close()
        server.terminate()
        server.wait(timeout=5)


def test_xi2_modifier_tuple_keeps_locked_latched_base_order() -> None:
    trace = """EVENT type 2 (KeyPress)
    device: 5 (5)
    time: 232007447
    detail: 8
    root: 100.00/100.00
    event: 100.00/100.00
    modifiers: locked 5 latched 2 base 1 effective: 7
    windows: root 0x374 event 0x374 child 0x600119
"""
    event = parse_xi2_records(trace, require_event=True)[0]
    assert event.modifiers == (5, 2, 1)


def test_explicit_codex_executable_matches_the_pinned_runtime_version(tmp_path: Path) -> None:
    configured_path = fixture_codex_path(tmp_path)
    adapter = LunaAdapter(neutral_dir=tmp_path / "neutral", codex_path=configured_path)
    assert adapter.cli_version == CLI_VERSION == "codex-cli 0.162.1"
    assert Path(adapter.codex_path) == configured_path


def test_runtime_evidence_is_process_sealed_and_environment_bound() -> None:
    class Profile:
        root_xid = 0x374

        def verify(self) -> None:
            return None

        def canonical_payload(self):
            return ("private-test-profile",)

    class Identity:
        fingerprint = "game-identity"
        process_start = "42"
        child_geometry = (1, 2, 800, 600)

    environment = StartupEnvironment(
        display=":private",
        hierarchy=(
            (2, 3, "master", "pointer:Virtual core pointer"),
            (3, 2, "master", "keyboard:Virtual core keyboard"),
            (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
            (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
            (6, 2, "slave", "pointer:xwayland-pointer:17"),
            (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
            (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
            (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
        ),
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=Profile(),
        keymap_levels=((38, ("a", "A")),),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version="input-v1",
        input_tool_sha256="a" * 64,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-evidence",
        readiness_sequence=1,
        environment=environment,
        identity=Identity(),
        keyboard_keymap=(("a", 38),),
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=(),
    )
    assert evidence.is_live
    assert evidence.environment_fingerprint == environment.fingerprint
    assert evidence.synthetic_modifiers == (0, 0, 0)
    assert evidence.forwarded_echo_sources == frozenset({7})
    assert evidence.forwarded_echo_sources != frozenset({environment.pointer_master})


def test_forwarded_echo_source_is_the_unique_retained_xwayland_relay() -> None:
    hierarchy = (
        (2, 3, "master", "pointer:Virtual core pointer"),
        (3, 2, "master", "keyboard:Virtual core keyboard"),
        (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
        (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
        (6, 2, "slave", "pointer:xwayland-pointer:17"),
        (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
        (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
        (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
    )
    assert resolve_forwarded_echo_sources(hierarchy, 2) == (7,)
    with pytest.raises(ValueError, match="absent or ambiguous"):
        resolve_forwarded_echo_sources(tuple(row for row in hierarchy if row[0] != 7), 2)
    with pytest.raises(ValueError, match="absent or ambiguous"):
        resolve_forwarded_echo_sources(
            (*hierarchy, (10, 2, "slave", "pointer:xwayland-relative-pointer:18")), 2
        )
    with pytest.raises(ValueError, match="absent or ambiguous"):
        resolve_forwarded_echo_sources(
            tuple((row[0], 99, *row[2:]) if row[0] == 7 else row for row in hierarchy),
            2,
        )


def _runtime_evidence():
    class Profile:
        root_xid = 0x374

        def verify(self) -> None:
            return None

        def canonical_payload(self):
            return ("test-profile",)

    class Identity:
        fingerprint = "game-identity"
        process_start = "42"
        child_geometry = (10, 20, 800, 600)

    environment = StartupEnvironment(
        display=":private",
        hierarchy=(
            (2, 3, "master", "pointer:Virtual core pointer"),
            (3, 2, "master", "keyboard:Virtual core keyboard"),
            (4, 2, "slave", "pointer:Virtual core XTEST pointer"),
            (5, 3, "slave", "keyboard:Virtual core XTEST keyboard"),
            (6, 2, "slave", "pointer:xwayland-pointer:17"),
            (7, 2, "slave", "pointer:xwayland-relative-pointer:17"),
            (8, 2, "slave", "pointer:xwayland-pointer-gestures:17"),
            (9, 3, "slave", "keyboard:xwayland-keyboard:17"),
        ),
        keyboard_source=5,
        keyboard_master=3,
        pointer_source=4,
        pointer_master=2,
        forwarded_echo_sources=(7,),
        xtest_profile=Profile(),
        keymap_levels=((38, ("a", "A")),),
        modifier_masks=(("Shift_L", 1), ("Control_L", 4)),
        input_tool_version="input-v1",
        input_tool_sha256="b" * 64,
    )
    evidence = mint_runtime_evidence(
        readiness_boot="boot-evidence",
        readiness_sequence=1,
        environment=environment,
        identity=Identity(),
        keyboard_keymap=(("a", 38),),
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=(),
    )
    return Identity(), environment, evidence


def test_runtime_keyboard_profile_matches_retained_xi2_modifier_transitions() -> None:
    """The runtime profile follows XI2's pre-transition modifier snapshots."""

    retained_control_trace = (
        "EVENT type 13 (RawKeyPress)\n"
        "    device: 3 (5)\n"
        "    time: 509690239\n"
        "    detail: 37\n"
        "    valuators:\n\n"
        "EVENT type 2 (KeyPress)\n"
        "    device: 5 (5)\n"
        "    time: 509690239\n"
        "    detail: 37\n"
        "    root: 1680.00/152.00\n"
        "    event: 1680.00/152.00\n"
        "    modifiers: locked 0x10 latched 0 base 0 effective: 0x10\n"
        "    windows: root 0x374 event 0x374 child 0x4600003\n\n"
        "EVENT type 14 (RawKeyRelease)\n"
        "    device: 3 (5)\n"
        "    time: 509690252\n"
        "    detail: 37\n"
        "    valuators:\n\n"
        "EVENT type 3 (KeyRelease)\n"
        "    device: 5 (5)\n"
        "    time: 509690252\n"
        "    detail: 37\n"
        "    root: 1680.00/152.00\n"
        "    event: 1680.00/152.00\n"
        "    modifiers: locked 0x10 latched 0 base 0x4 effective: 0x14\n"
        "    windows: root 0x374 event 0x374 child 0x4600003\n\n"
    )
    retained = parse_xi2_records(retained_control_trace)
    delivered = [
        event for event in retained if event.event_class in {EventClass.KEY_PRESS, EventClass.KEY_RELEASE}
    ]
    assert [event.modifiers for event in delivered] == [(0x10, 0, 0), (0x10, 0, 0x4)]
    assert [event.modifier_effective for event in delivered] == [0x10, 0x14]

    keymap = {
        "Return": 36,
        "Escape": 9,
        "Control_L": 37,
        "F12": 96,
        "Shift_L": 50,
        "a": 38,
        "b": 56,
        "c": 54,
        "d": 40,
        "e": 26,
        "f": 41,
        "g": 42,
        "h": 43,
        "i": 31,
        "j": 44,
        "k": 45,
        "l": 46,
        "m": 58,
        "n": 57,
        "o": 32,
        "p": 33,
        "q": 24,
        "r": 27,
        "s": 39,
        "t": 28,
        "u": 30,
        "v": 55,
        "w": 25,
        "x": 53,
        "y": 29,
        "z": 52,
        "comma": 59,
        "space": 65,
        "0": 19,
        "1": 10,
        "2": 11,
        "9": 18,
        "apostrophe": 48,
        "equal": 21,
        "minus": 20,
        "semicolon": 47,
        "slash": 61,
    }
    levels_by_code: dict[int, list[str]] = {}
    for symbol, code in keymap.items():
        levels_by_code.setdefault(code, []).append(symbol)
        if len(symbol) == 1 and symbol.isascii() and symbol.isalpha() and symbol.islower():
            levels_by_code[code].append(symbol.upper())
    levels = {code: tuple(sorted(set(symbols))) for code, symbols in levels_by_code.items()}
    keyboard_profile = DesktopDriver._runtime_keyboard_profile(
        tuple(DesktopDriver.COMMANDS), levels, keymap, (0x10, 0, 0), (1, 4)
    )
    profile = {(operation, payload): steps for operation, payload, steps in keyboard_profile}

    control_chord = profile[("interact_target", "")]
    assert control_chord[0][1:] == ("Control_L", 37, delivered[0].modifiers, delivered[0].modifier_effective)
    assert control_chord[3][1:] == ("Control_L", 37, delivered[1].modifiers, delivered[1].modifier_effective)
    assert control_chord[1][3:] == ((0x10, 0, 0x4), 0x14)
    assert control_chord[2][3:] == ((0x10, 0, 0x4), 0x14)

    target_steps = profile[("type_allowlisted_command", DesktopDriver.TARGET_AUCTIONEER_COMMAND)]
    shift_index = next(index for index, step in enumerate(target_steps) if step[0:2] == ("press", "Shift_L"))
    shift_mask = 1
    assert target_steps[shift_index][3:] == ((0x10, 0, 0), 0x10)
    assert target_steps[shift_index + 1][3:] == ((0x10, 0, shift_mask), 0x10 | shift_mask)
    assert target_steps[shift_index + 2][3:] == ((0x10, 0, shift_mask), 0x10 | shift_mask)
    assert target_steps[shift_index + 3][3:] == ((0x10, 0, shift_mask), 0x10 | shift_mask)

    binding_steps = profile[
        ("type_allowlisted_command", DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND)
    ]
    assert any(step[1] == "comma" for step in binding_steps)
    assert any(step[0:2] == ("press", "Shift_L") for step in binding_steps)  # parentheses


def test_takeover_hover_uses_positive_passive_frame_binding_and_guarded_motion_path(tmp_path) -> None:
    """Exercise the production hover planner and Guardian with sealed frame provenance."""

    _fixture_identity, environment, fixture_evidence = _runtime_evidence()
    identity = WindowIdentity(
        xid=0x600100,
        pid=os.getpid(),
        executable="/usr/bin/wine",
        title="World of Warcraft",
        wm_class="wow.exe",
        child_xid=0x600101,
        outer_geometry=(10, 20, 800, 600),
        child_geometry=(10, 20, 800, 600),
        foreground_xid=0x600100,
        process_start="private-hover-test",
        display=environment.display,
        fingerprint="private-hover-game-identity",
        root_xid=0x374,
        managed_frame_xid=0x600100,
        target_event_xids=frozenset({0x600101}),
    )
    evidence = mint_runtime_evidence(
        readiness_boot="private-hover-boot",
        readiness_sequence=37,
        environment=environment,
        identity=identity,
        keyboard_keymap=fixture_evidence.keyboard_keymap,
        native_modifiers=(0, 0, 0),
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=fixture_evidence.keyboard_profile,
    )
    driver = DesktopDriver(
        backend=RecordingBackend(identity=identity),
        lease_path=tmp_path / "takeover-hover.lock",
    )
    # Keep X11Backend's production type guard while making every backend
    # boundary private and deterministic. The planner, frame-bound binding,
    # Guardian correlation, validator, and receipt path below remain real.
    backend = X11Backend.__new__(X11Backend)
    backend.profile = SimpleNamespace(display=environment.display)
    backend.input_tool = SimpleNamespace(version="private-test-input-tool")
    backend.xauthority = ""
    backend.last_pointer_execution = None
    backend.release_held_input = lambda: None
    driver.backend = backend
    driver.resolve_identity = lambda *, require_foreground=True, **_kwargs: identity
    driver._is_owned_client_target = lambda xid, _identity: xid in identity.target_event_xids
    backend.pointer_position = lambda *, generation: (410, 320, identity.child_xid)
    driver._input_evidence = evidence

    class RuntimeMonitor:
        alive = True
        failure_diagnostic = None

        def __init__(self) -> None:
            self._events: list[XI2Event] = []

        def events(self) -> list[XI2Event]:
            return list(self._events)

        def push(self, event: XI2Event) -> None:
            self._events.append(event)
            driver._external_input(event)
            assert driver.guardian.armed, (
                f"Guardian revoked on {event.event_class.value}; events={len(self._events)}; "
                f"reason={driver.guardian.revoked_reason}"
            )

        def wait_for_event(self, predicate, *, timeout: float) -> None:
            del timeout
            assert predicate(self.events()), "Guardian did not finish the production hover trace"

    monitor = RuntimeMonitor()
    driver._monitor = monitor  # type: ignore[assignment]
    generation = driver.guardian.arm(evidence)

    def emit_private_trace(chain, *, generation, input_schema, expected_argv, **_kwargs) -> None:
        assert input_schema == "pointer-motion"
        assert tuple(chain) == tuple(expected_argv)
        motion_coordinates = []
        index = 0
        while index < len(chain):
            if chain[index] == "mousemove":
                motion_coordinates.append((int(chain[index + 1]), int(chain[index + 2])))
                index += 3
            elif chain[index] == "sleep":
                index += 2
            else:
                index += 1
        action = driver.guardian.action
        assert action is not None
        planned = tuple(row[:2] for row in action.expected_motion_trace)
        assert tuple(motion_coordinates) == planned
        previous = (410, 320)
        for timestamp, (root_x, root_y) in enumerate(motion_coordinates, start=1):
            valuators = ((0, float(root_x), None), (1, float(root_y), None))
            monitor.push(
                XI2Event(
                    type_number=17,
                    event_class=EventClass.RAW_MOTION,
                    master_device=environment.pointer_master,
                    source_device=environment.pointer_source,
                    timestamp_ms=timestamp,
                    detail=0,
                    root_xid=None,
                    event_xid=None,
                    child_xid=None,
                    valuators=valuators,
                )
            )
            motion_fields = {
                "timestamp_ms": timestamp,
                "detail": 0,
                "root_xid": evidence.xtest_profile.root_xid,
                "event_xid": evidence.xtest_profile.root_xid,
                "child_xid": identity.child_xid,
                "root_xy": (float(previous[0]), float(previous[1])),
                "event_xy": (float(previous[0]), float(previous[1])),
                "modifiers": (0, 0, 0),
                "modifier_effective": 0,
                "valuators": valuators,
            }
            monitor.push(
                XI2Event(
                    type_number=6,
                    event_class=EventClass.MOTION,
                    master_device=environment.pointer_source,
                    source_device=environment.pointer_source,
                    **motion_fields,
                )
            )
            echo_fields = {
                **motion_fields,
                "root_xy": (float(root_x), float(root_y)),
                "event_xy": (float(root_x), float(root_y)),
            }
            monitor.push(
                XI2Event(
                    type_number=6,
                    event_class=EventClass.MOTION,
                    master_device=environment.forwarded_echo_sources[0],
                    source_device=environment.forwarded_echo_sources[0],
                    **echo_fields,
                )
            )
            previous = (root_x, root_y)

    backend._run = emit_private_trace
    try:
        gesture = driver.run_takeover_hover(generation, evidence)
        assert gesture.click is None
        assert gesture.movement_duration + 1e-12 >= 0.8
        assert gesture.trajectory.binding.capture_sequence == evidence.readiness_sequence
        assert evidence.readiness_boot in gesture.trajectory.binding.capture_id
        assert driver.guardian.action is None
        assert driver.pointer_gesture_receipts[-1]["accepted"] is True
        assert driver.pointer_gesture_receipts[-1]["action"] == "takeover_hover"
    finally:
        driver._monitor = None
        if driver.guardian.armed:
            driver.revoke("private hover test cleanup")
        driver.guardian.close()


@pytest.mark.skipif(
    shutil.which("Xvfb") is None or shutil.which("cc") is None, reason="private X11 fixture unavailable"
)
@pytest.mark.parametrize(
    ("failure_kind", "snapshot_failure"),
    [
        ("raw_event", False),
        ("parser_error", False),
        ("raw_event", True),
        ("emitter_error", False),
    ],
)
def test_takeover_hover_failure_retains_monitor_after_guardian_kills_emitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str, snapshot_failure: bool
) -> None:
    """Retain the parsed XI2 cause after Guardian revokes and terminates dispatch."""

    from crowley.session.input_tool import PinnedInputTool
    from tests.test_session_input_tool import fixture_input_tool

    source_identity, base_environment, base_evidence = _runtime_evidence()
    del source_identity

    source = tmp_path / "blocking-input-tool.c"
    executable = tmp_path / "blocking-input-tool"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    const char *marker = getenv("CROWLEY_HOVER_EMITTER_MARKER");
    if (marker == NULL) return 2;
    int fd = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return 3;
    if (write(fd, "ready", 5) != 5) return 4;
    close(fd);
    if (getenv("CROWLEY_HOVER_EMITTER_FAILURE") != NULL) return 23;
    for (;;) pause();
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    tool: PinnedInputTool | None = None
    driver: DesktopDriver | None = None
    server, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1920x1080x24",
            "-ac",
        ),
        popen_kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True},
    )
    x11.XCloseDisplay(xdisplay)
    read_fd = -1
    writer_fd = -1
    reader = None
    injector: threading.Thread | None = None
    injector_errors: list[BaseException] = []
    monitor: InputMonitor | None = None
    try:
        xauthority = tmp_path / "private-Xauthority"
        xauthority.touch(mode=0o600)
        xauthority.chmod(0o600)
        marker = tmp_path / "emitter-started"
        monkeypatch.setenv("CROWLEY_HOVER_EMITTER_MARKER", str(marker))
        if failure_kind == "emitter_error":
            monkeypatch.setenv("CROWLEY_HOVER_EMITTER_FAILURE", "1")
        else:
            monkeypatch.delenv("CROWLEY_HOVER_EMITTER_FAILURE", raising=False)
        tool = fixture_input_tool(tmp_path / "private-xdotool", str(executable))
        from crowley.session.driver import DriverProfile

        profile = DriverProfile(display=display, xauthority=str(xauthority))
        backend = X11Backend(profile, input_tool=tool)
        driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "private-desktop.lock")

        identity = WindowIdentity(
            xid=0x600100,
            pid=os.getpid(),
            executable="/usr/bin/wine",
            title="World of Warcraft",
            wm_class="wow.exe",
            child_xid=0x600101,
            outer_geometry=(10, 20, 800, 600),
            child_geometry=(10, 20, 800, 600),
            foreground_xid=0x600100,
            process_start="private-hover-failure-test",
            display=display,
            fingerprint="private-hover-failure-identity",
            root_xid=base_evidence.xtest_profile.root_xid,
            managed_frame_xid=0x600100,
            target_event_xids=frozenset({0x600101}),
        )
        environment = replace(base_environment, display=display)
        evidence = mint_runtime_evidence(
            readiness_boot="private-hover-failure-boot",
            readiness_sequence=38,
            environment=environment,
            identity=identity,
            keyboard_keymap=base_evidence.keyboard_keymap,
            native_modifiers=base_evidence.native_modifiers,
            native_held_keycodes=(),
            native_held_buttons=(),
            keyboard_profile=base_evidence.keyboard_profile,
        )
        driver.resolve_identity = lambda *, require_foreground=True, **_kwargs: identity
        driver._is_owned_client_target = lambda xid, _identity: xid in identity.target_event_xids
        driver.input_hierarchy = lambda **_kwargs: environment.hierarchy
        driver.xtest_input_profile = lambda _hierarchy=None, **_kwargs: evidence.xtest_profile
        backend.observe_keymap_levels = lambda **_kwargs: dict(environment.keymap_levels)
        backend.observe_native_input_state = lambda _root: (evidence.native_modifiers, (), ())
        backend.pointer_position = lambda *, generation: (410, 320, identity.child_xid)

        controller = Controller(tmp_path / "session.json", driver=driver)
        paused = controller.pause("private takeover hover failure fixture")

        read_fd, writer_fd = os.pipe()
        reader = os.fdopen(read_fd, "r", encoding="ascii", buffering=1)
        read_fd = -1

        class PipeProcess:
            stdout = reader

            def __init__(self) -> None:
                self.returncode: int | None = None

            def poll(self):
                return self.returncode

            def terminate(self):
                if self.returncode is None:
                    self.returncode = 0
                    try:
                        os.close(writer_fd)
                    except OSError:
                        pass

            def wait(self, timeout=None):
                del timeout
                self.terminate()
                return self.returncode

            def kill(self):
                self.terminate()

        process = PipeProcess()

        class PipeObserver:
            def __init__(self) -> None:
                self.process = process

            def stop(self):
                process.terminate()
                return ""

        monitor = InputMonitor(display, driver._external_input, xauthority=str(xauthority))
        monitor.observer = PipeObserver()  # type: ignore[assignment]
        monitor.thread = threading.Thread(target=monitor._read, name="private-hover-xi2-reader", daemon=True)
        driver._monitor = monitor
        monitor.thread.start()
        assert monitor._started.wait(1.0)
        assert monitor.alive

        daemon = SessionDaemon.__new__(SessionDaemon)
        daemon.controller = controller
        daemon.driver = driver
        daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
        daemon.passive_startup = lambda: evidence
        daemon.status_dict = lambda: {"controllerState": controller.status().state.value}
        daemon._startup_operation = {"state": "ready"}
        daemon._takeover_drill_operation = {"state": "idle", "failure": ""}
        daemon._takeover_drill_lock = threading.Lock()
        daemon._takeover_cancel_lock = threading.Lock()
        daemon._takeover_cancel = threading.Event()
        daemon._takeover_event = None

        raw_record = (
            "EVENT type 17 (RawMotion)\ndevice: 4 (2)\ntime: 10\ndetail: 0\n\n"
            if failure_kind == "parser_error"
            else "EVENT type 13 (RawKeyPress)\ndevice: 3 (9)\ntime: 10\ndetail: 50\n\n"
        )

        def inject_unattributed_event_after_emitter_start() -> None:
            try:
                deadline = time.monotonic() + 2.0
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert marker.exists(), "guarded emitter did not reach its admitted child"
                if failure_kind == "emitter_error":
                    return
                os.write(writer_fd, raw_record.encode("ascii"))
                assert monitor is not None and monitor.thread is not None
                monitor.thread.join(timeout=2.0)
                assert not monitor.thread.is_alive(), "XI2 reader did not finish after Guardian stop"
            except BaseException as exc:
                injector_errors.append(exc)

        injector = threading.Thread(
            target=inject_unattributed_event_after_emitter_start,
            name="private-hover-failure-injector",
            daemon=True,
        )
        injector.start()
        if snapshot_failure:
            monitor.diagnostic_snapshot = lambda: (_ for _ in ()).throw(OSError("snapshot unavailable"))  # type: ignore[method-assign]
        with pytest.raises(DriverError, match="desktop command failed") as failure:
            daemon.run_takeover_drill(expected_generation=paused.generation)
        injector.join(timeout=2.0)
        assert not injector.is_alive()
        assert not injector_errors, injector_errors
        assert not driver.guardian.armed
        assert driver._monitor is None
        context = driver._last_takeover_hover_failure_context
        assert isinstance(context, dict)
        assert context["stage"] == "pointer_emission"
        expected_reason = (
            "malformed XI2 event stream"
            if failure_kind == "parser_error"
            else "unattributed XI2 event class=RawKeyPress"
        )
        if failure_kind == "emitter_error":
            assert context["guardianBeforeCleanup"]["armed"] is True
            assert context["guardianBeforeCleanup"]["revokedReason"] in {"", "startup"}
            assert context["guardianAfterCleanup"]["armed"] is False
            assert context["guardianAfterCleanup"]["revokedReason"] == (
                "takeover hover failed before its motion trace completed"
            )
        else:
            assert context["guardianBeforeCleanup"]["revokedReason"].startswith(expected_reason)
        assert isinstance(context["observerRegisteredAtFailureCapture"], bool)
        assert context["expectedAction"]["expectedMotionCount"] > 0
        assert context["expectedAction"]["argv"][0] == "xdotool"
        assert "mousemove" in context["expectedAction"]["argv"]
        if failure_kind == "emitter_error":
            assert "23" in context["exception"]
        else:
            assert "-15" in context["exception"]
        assert daemon._takeover_drill_operation["state"] == "failed"
        assert controller.takeover_drill_record is None
        public_failure = daemon._takeover_drill_operation["failure"]
        if failure_kind == "emitter_error":
            assert public_failure.startswith("takeover hover failed during pointer_emission;")
            assert "Guardian revocation: takeover hover failed before" not in public_failure
        else:
            assert public_failure.startswith(f"takeover hover Guardian revocation: {expected_reason}")
        assert "EVENT type 13" not in public_failure
        artifact_path = tmp_path / "takeover-drill-diagnostics" / "latest-hover-failure.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["persistedAtNs"] > 0
        assert len(artifact["sourceBindings"]["crowley.session.daemon"]) == 64
        assert len(artifact["sourceBindings"]["crowley.session.driver"]) == 64
        assert artifact["cleanup"]["resourcesUnresolved"] is False
        assert artifact["cleanup"]["driverMonitorRegisteredAfterCleanup"] is False
        assert artifact["guardianBeforeCleanup"] == context["guardianBeforeCleanup"]
        assert artifact["guardianAfterCleanup"] == context["guardianAfterCleanup"]
        assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(artifact_path.parent.stat().st_mode) == 0o700
        if snapshot_failure:
            assert "snapshot unavailable" in artifact["observerSnapshotError"]
            assert "snapshot unavailable" in artifact["observerSnapshotAfterCleanupError"]
            assert "observer" not in context
            assert "observer" not in artifact
        elif failure_kind == "parser_error":
            observer = artifact["observer"]
            assert observer["parsedEventCount"] == 0
            assert observer["rawBlocks"] == [raw_record]
            assert observer["partialRaw"] == ""
            assert observer["parserFailure"]["reason"] == "RawMotion: missing valuators section"
            assert observer["failureDiagnostic"]["reason"] == "RawMotion: missing valuators section"
        elif failure_kind == "emitter_error":
            observer = artifact["observer"]
            assert observer["parsedEventCount"] == 0
            assert observer["rawBlocks"] == []
            assert observer["partialRaw"] == ""
            assert observer["parserFailure"] is None
            assert observer["failureDiagnostic"] is None
            assert "23" in artifact["commandError"]["message"]
        else:
            observer = artifact["observer"]
            assert observer["parsedEventCount"] == 1
            # The parser emits once the event fields are complete, before it
            # consumes the blank record delimiter. Guardian stop therefore
            # truthfully retains this event as a partial raw tail.
            assert observer["rawBlocks"] == []
            assert observer["partialRaw"].startswith("EVENT type 13 (RawKeyPress)")
            assert "detail: 50" in observer["partialRaw"]
            assert observer["parserFailure"] is None
            assert observer["rawTruncated"] is False
        assert "desktop command failed" in str(failure.value)
    finally:
        if injector is not None and injector.is_alive():
            if writer_fd >= 0:
                try:
                    os.close(writer_fd)
                except OSError:
                    pass
            injector.join(timeout=2.0)
        if monitor is not None and driver is not None and driver._monitor is monitor:
            try:
                driver.stop_input_observer(monitor)
            except Exception:
                pass
        if reader is not None:
            reader.close()
        if driver is not None:
            driver.close()
        elif tool is not None:
            tool.close()
        if read_fd >= 0:
            os.close(read_fd)
        if server is not None:
            server.terminate()
            server.wait(timeout=5)


class _TestGuardian:
    def __init__(self):
        self.generation = 0
        self.armed = False
        self._callbacks = []
        self.held = False

    def on_revoke(self, callback):
        self._callbacks.append(callback)

    def arm(self, _evidence):
        self.generation += 1
        self.armed = True
        return self.generation

    def revoke(self, reason):
        self.generation += 1
        self.armed = False
        for callback in self._callbacks:
            callback(reason, self.generation)
        return self.generation


class _RuntimeDriver:
    def __init__(self):
        self.guardian = _TestGuardian()
        self.input_calibration_calls = 0

    def arm(self, evidence):
        return self.guardian.arm(evidence)

    def revoke(self, reason):
        return self.guardian.revoke(reason)

    def input_calibration(self, _evidence):
        self.input_calibration_calls += 1
        raise AssertionError("passive runtime resume must not run the old keyboard calibration")


def test_takeover_drill_is_durable_and_runtime_resume_skips_old_calibration(tmp_path: Path) -> None:
    _identity, environment, evidence = _runtime_evidence()
    driver = _RuntimeDriver()
    journal = tmp_path / "session.json"
    controller = Controller(journal, driver=driver)
    controller.pause("prepare explicit drill")
    record = TakeoverDrillRecord.create(environment.fingerprint, 37, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)

    started = controller.start_with_runtime_evidence(evidence)
    assert started.state == ControllerState.RECONCILE
    assert driver.guardian.armed
    assert driver.input_calibration_calls == 0
    controller.pause("test restart boundary")

    restarted_driver = _RuntimeDriver()
    restarted = Controller(journal, driver=restarted_driver)
    assert restarted.takeover_drill_record == record
    assert not restarted._native_calibrated
    assert not restarted._input_calibrated
    resumed = restarted.resume_with_runtime_evidence(
        evidence, expected_generation=restarted.status().generation
    )
    assert resumed.state == ControllerState.RECONCILE
    assert restarted_driver.guardian.armed
    assert restarted_driver.input_calibration_calls == 0


def test_takeover_drill_rejects_environment_mismatch_and_corrupt_record(tmp_path: Path) -> None:
    _identity, environment, evidence = _runtime_evidence()
    driver = _RuntimeDriver()
    journal = tmp_path / "session.json"
    controller = Controller(journal, driver=driver)
    controller.pause("prepare explicit drill")
    record = TakeoverDrillRecord.create(environment.fingerprint, 3, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)

    _other_identity, other_environment, other_evidence = _runtime_evidence()
    other_environment = StartupEnvironment(
        **{
            **other_environment.__dict__,
            "display": ":different",
        }
    )
    other_evidence = mint_runtime_evidence(
        readiness_boot=other_evidence.readiness_boot,
        readiness_sequence=other_evidence.readiness_sequence,
        environment=other_environment,
        identity=_other_identity,
        keyboard_keymap=other_evidence.keyboard_keymap,
        native_modifiers=other_evidence.native_modifiers,
        native_held_keycodes=(),
        native_held_buttons=(),
        keyboard_profile=other_evidence.keyboard_profile,
    )
    with pytest.raises(DriverError, match="matching durable takeover drill"):
        controller.start_with_runtime_evidence(other_evidence)
    assert controller.status().state == ControllerState.PAUSED
    assert not driver.guardian.armed

    raw = json.loads(journal.read_text())
    raw["takeoverDrill"]["fingerprint"] = "0" * 64
    journal.write_text(json.dumps(raw))
    refused = Controller(journal, driver=_RuntimeDriver())
    assert refused.takeover_drill_record is None
    with pytest.raises(DriverError, match="matching durable takeover drill"):
        refused.resume_with_runtime_evidence(evidence, expected_generation=refused.status().generation)


def _public_status_for_controller(controller: Controller, driver) -> dict:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.controller = controller
    daemon.driver = driver
    daemon.last_error = ""
    daemon.last_snapshot = {}
    daemon._startup_evidence = None
    daemon._startup_operation = {"state": "idle"}
    daemon._takeover_drill_operation = {"state": "idle", "failure": ""}
    daemon._calibration_operation_snapshot = lambda: {}
    daemon._native_bootstrap_ready = SimpleNamespace(is_set=lambda: False)
    daemon._native_baseline_ready = SimpleNamespace(is_set=lambda: False)
    daemon._native_bootstrap_lock = threading.RLock()
    daemon._native_baseline_observation = {}
    daemon._native_bootstrap_observation = {}
    daemon._native_handshake = {}
    daemon._mail_loop_binding = MailLoopBinding()
    daemon._mail_loop_active_binding = None
    daemon._mail_loop_control_epoch = 0
    daemon._mail_loop_last_decision = None
    daemon._mail_cycle_wait_deadlines = {}
    controller.native_resource_assessment = lambda: {}
    return daemon.status_dict()


def test_typed_telemetry_block_clears_live_readiness_but_retains_drill_in_public_status(
    tmp_path: Path,
) -> None:
    _identity, environment, evidence = _runtime_evidence()
    driver = _RuntimeDriver()
    journal = tmp_path / "session.json"
    controller = Controller(journal, driver=driver)
    controller.pause("prepare drill")
    record = TakeoverDrillRecord.create(environment.fingerprint, 21, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)
    controller._accept_runtime_evidence(evidence)

    blocked = controller.block(
        "ordinary client reload telemetry gap",
        invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
    )

    assert blocked.state is ControllerState.BLOCKED
    assert controller._runtime_startup_evidence is None
    assert controller.runtime_readiness_invalidation is InvalidationCause.TELEMETRY_INVALID
    assert controller.takeover_drill_record == record
    persisted = json.loads(journal.read_text())
    assert persisted["takeoverDrill"] == record.to_json()
    assert persisted["runtimeReadinessInvalidation"] == "telemetry_invalid"
    public = _public_status_for_controller(controller, driver)
    assert public["runtimeReadinessInvalidation"] == "telemetry_invalid"
    assert public["takeoverDrill"]["state"] == "stored_unverified"
    assert public["takeoverDrill"]["fingerprint"] == record.fingerprint


def test_untyped_block_fails_closed_as_unknown_without_deleting_drill(tmp_path: Path) -> None:
    _identity, environment, evidence = _runtime_evidence()
    driver = _RuntimeDriver()
    controller = Controller(tmp_path / "session.json", driver=driver)
    controller.pause("prepare drill")
    record = TakeoverDrillRecord.create(environment.fingerprint, 22, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)
    controller._accept_runtime_evidence(evidence)

    controller.block("reason wording does not select a cause")

    assert controller.runtime_readiness_invalidation is InvalidationCause.UNKNOWN
    assert controller._runtime_startup_evidence is None
    assert controller.takeover_drill_record == record


def test_observer_loss_revoke_propagates_typed_cause_and_pauses(tmp_path: Path) -> None:
    class Guardian:
        generation = 0
        armed = False

        def __init__(self):
            self.callbacks = []

        def on_revoke(self, callback):
            self.callbacks.append(callback)

        def revoke(self, reason):
            self.generation += 1
            self.armed = False
            for callback in tuple(self.callbacks):
                callback(reason, self.generation)
            return self.generation

    driver = DesktopDriver.__new__(DesktopDriver)
    driver._calibration_action_lock = threading.RLock()
    driver._queued = []
    driver._input_evidence = object()
    driver._active_revocation_cause = InvalidationCause.UNKNOWN
    driver.guardian = Guardian()
    controller = Controller(tmp_path / "session.json", driver=driver)
    with controller.lock:
        controller._state = ControllerState.RECONCILE
    driver.guardian.armed = True

    driver._external_input(None)

    assert controller.status().state is ControllerState.PAUSED
    assert controller.runtime_readiness_invalidation is InvalidationCause.OBSERVER_LOST
    assert driver._input_evidence is None


@pytest.mark.parametrize(
    ("changed_fact", "expected_cause"),
    [
        ("hierarchy", InvalidationCause.INPUT_ENVIRONMENT_CHANGED),
        ("identity", InvalidationCause.GAME_PROCESS_CHANGED),
    ],
)
def test_runtime_arm_rejection_blocks_with_observed_typed_cause_and_retains_drill(
    tmp_path: Path,
    changed_fact: str,
    expected_cause: InvalidationCause,
) -> None:
    identity, environment, evidence = _runtime_evidence()

    class Guardian:
        generation = 0
        armed = False

        def __init__(self):
            self.callbacks = []

        def on_revoke(self, callback):
            self.callbacks.append(callback)

        def revoke(self, reason):
            self.generation += 1
            self.armed = False
            for callback in tuple(self.callbacks):
                callback(reason, self.generation)
            return self.generation

    backend = X11Backend.__new__(X11Backend)
    backend._check_published_tool = lambda: None
    backend.observe_keymap_levels = lambda: dict(environment.keymap_levels)
    backend.observe_native_input_state = lambda _root: (evidence.native_modifiers, (), ())
    driver = DesktopDriver.__new__(DesktopDriver)
    driver.profile = SimpleNamespace(display=environment.display)
    driver.backend = backend
    driver._monitor = SimpleNamespace(alive=True)
    driver._monitor_calibration = False
    driver._calibration_action_lock = threading.RLock()
    driver._queued = []
    driver._input_evidence = None
    driver._active_revocation_cause = InvalidationCause.UNKNOWN
    driver.guardian = Guardian()
    hierarchy = evidence.xinput_hierarchy
    if changed_fact == "hierarchy":
        driver.input_hierarchy = lambda: (*hierarchy, (99, 2, "slave", "pointer:unowned"))
    else:
        driver.input_hierarchy = lambda: hierarchy
    driver.xtest_input_profile = lambda _hierarchy: evidence.xtest_profile
    driver.resolve_identity = lambda *, require_foreground: (
        identity
        if changed_fact != "identity"
        else SimpleNamespace(
            fingerprint="different-game",
            process_start=identity.process_start,
            child_geometry=identity.child_geometry,
        )
    )

    controller = Controller(tmp_path / f"{changed_fact}.json", driver=driver)
    controller.pause("prepare drill")
    record = TakeoverDrillRecord.create(environment.fingerprint, 23, observed_at=1_800_000_000)
    controller.persist_takeover_drill(record)
    controller._accept_runtime_evidence(evidence)

    blocked = controller.resume(expected_generation=controller.status().generation)

    assert blocked.state is ControllerState.BLOCKED
    assert controller.runtime_readiness_invalidation is expected_cause
    assert controller._runtime_startup_evidence is None
    assert controller.takeover_drill_record == record


@pytest.mark.parametrize(
    ("native_phase", "native_ordinal", "accepted"),
    [("normal", 0, True), ("updates", 0, False), ("normal", 1, False), ("held", 100, False)],
)
@pytest.mark.parametrize(
    "entry_state", [ControllerState.STOPPED, ControllerState.PAUSED, ControllerState.BLOCKED]
)
def test_passive_startup_requires_three_advancing_ordinary_frames_without_input(
    native_phase: str, native_ordinal: int, accepted: bool, entry_state: ControllerState
) -> None:
    from crowley.session.daemon import SessionDaemon

    identity, environment, evidence = _runtime_evidence()

    class Monitor:
        alive = True
        failure_diagnostic = None

        def __init__(self) -> None:
            self.observer = SimpleNamespace(process=SimpleNamespace(pid=4242))

    class Driver:
        def __init__(self):
            self.guardian = SimpleNamespace(armed=False, generation=7, observer_process_groups=(4242,))
            self.monitor = Monitor()
            self._monitor = None
            self.input_calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            assert require_foreground is True
            assert deadline_monotonic is None or deadline_monotonic > 0
            return identity

        def start_input_observer(self):
            self.input_calls.append("observer")
            self._monitor = self.monitor
            return self.monitor

        def stop_input_observer(self, monitor):
            assert monitor is self.monitor
            monitor.alive = False
            self._monitor = None
            self.guardian.observer_process_groups = ()

        def mint_runtime_input_evidence(self, measured_identity, *, readiness_boot, readiness_sequence):
            assert measured_identity.fingerprint == identity.fingerprint
            assert (readiness_boot, readiness_sequence) == ("boot-a", 3)
            self.input_calls.append("measure-readonly-environment")
            return mint_runtime_evidence(
                readiness_boot=readiness_boot,
                readiness_sequence=readiness_sequence,
                environment=environment,
                identity=identity,
                keyboard_keymap=evidence.keyboard_keymap,
                native_modifiers=evidence.native_modifiers,
                native_held_keycodes=evidence.native_held_keycodes,
                native_held_buttons=evidence.native_held_buttons,
                keyboard_profile=evidence.keyboard_profile,
            )

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    daemon._startup_lock = threading.Lock()
    daemon._startup_evidence = None
    daemon._startup_operation = {"state": "not_checked", "advancingFrames": 0}
    daemon.controller = SimpleNamespace(
        lock=threading.RLock(),
        status=lambda: SimpleNamespace(generation=70, state=entry_state, stop_requested=False),
    )
    daemon._native_construction_lock = threading.RLock()
    daemon._passive_region_epoch = 0
    daemon._passive_region_admission = None
    daemon._shutdown_event = threading.Event()
    frames = [
        Frame(
            Snapshot(
                {
                    "context": {"character": "Arthur", "realm": "Beta"},
                    "render": {"nativePhase": native_phase, "nativeOrdinal": native_ordinal},
                }
            ),
            "boot-a",
            index,
            100 + index,
            index,
            index,
            1,
        )
        for index in (1, 2, 3)
    ]
    samples = [Sample("NO_CHANGE", None, 0.0, (0, 0, 1, 1), 0, identity.fingerprint)] + [
        Sample("FRESH", frame, float(frame.sequence), (0, 0, 1, 1), frame.crc32, identity.fingerprint)
        for frame in frames
    ]

    def capture_status(*, deadline, _passive_region_capability):
        assert deadline == _passive_region_capability.deadline_monotonic
        assert _passive_region_capability is daemon._passive_region_admission
        assert _passive_region_capability.generation == 70
        assert _passive_region_capability.state is entry_state
        return samples.pop(0)

    daemon.capture_status = capture_status

    if accepted:
        ready = daemon.passive_startup()
        assert ready.is_live
        assert (ready.readiness_boot, ready.readiness_sequence) == ("boot-a", 3)
        assert daemon._startup_operation["advancingFrames"] == 3
        assert daemon._startup_operation["state"] == "ready"
        assert daemon._passive_region_admission is None
    else:
        with pytest.raises(DriverError, match="ordinary nativePhase=normal ordinal=0"):
            daemon.passive_startup()
        assert daemon._startup_operation["advancingFrames"] == 0
        assert daemon._startup_operation["state"] == "blocked"
        assert daemon._passive_region_admission is None
    assert daemon.driver.input_calls == (
        ["observer", "measure-readonly-environment"] if accepted else ["observer"]
    )


@pytest.mark.parametrize("failure", ["observer", "identity", "generation"])
def test_passive_startup_refuses_drift_and_reaps_private_admission(failure: str) -> None:
    from crowley.session.daemon import SessionDaemon

    identity, _environment, _evidence = _runtime_evidence()

    class Monitor:
        alive = True
        failure_diagnostic = None
        observer = SimpleNamespace(process=SimpleNamespace(pid=5151))

    monitor = Monitor()
    state = SimpleNamespace(generation=18, state=ControllerState.STOPPED, stop_requested=False)

    class Driver:
        def __init__(self) -> None:
            self.guardian = SimpleNamespace(armed=False, generation=3, observer_process_groups=(5151,))
            self._monitor = None
            self.identity_calls = 0
            self.capture_calls = 0

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            assert require_foreground is True
            assert deadline_monotonic is None or deadline_monotonic > 0
            self.identity_calls += 1
            if failure == "identity" and self.identity_calls > 1:
                return SimpleNamespace(fingerprint="changed-window", process_start="43")
            return identity

        def start_input_observer(self):
            self._monitor = monitor
            return monitor

        def stop_input_observer(self, current):
            assert current is monitor
            current.alive = False
            self._monitor = None
            self.guardian.observer_process_groups = ()

        def mint_runtime_input_evidence(self, *_args, **_kwargs):
            pytest.fail("invalidated passive startup minted runtime evidence")

    frames = [
        Frame(
            Snapshot(
                {
                    "context": {"character": "Arthur", "realm": "Beta"},
                    "render": {"nativePhase": "normal", "nativeOrdinal": 0},
                }
            ),
            "boot-a",
            sequence,
            100 + sequence,
            sequence,
            sequence,
            1,
        )
        for sequence in (1, 2, 3)
    ]
    samples = [
        Sample("FRESH", frame, float(frame.sequence), (0, 0, 1, 1), frame.crc32, identity.fingerprint)
        for frame in frames
    ]
    driver = Driver()
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = driver
    daemon.controller = SimpleNamespace(lock=threading.RLock(), status=lambda: state)
    daemon._startup_lock = threading.Lock()
    daemon._startup_evidence = None
    daemon._startup_operation = {"state": "not_checked", "advancingFrames": 0}
    daemon._native_construction_lock = threading.RLock()
    daemon._passive_region_epoch = 0
    daemon._passive_region_admission = None
    daemon._shutdown_event = threading.Event()

    def capture_status(*, deadline, _passive_region_capability):
        assert deadline == _passive_region_capability.deadline_monotonic
        driver.capture_calls += 1
        sample = samples.pop(0)
        if failure == "observer":
            monitor.alive = False
        elif failure == "generation":
            state.generation += 1
        return sample

    daemon.capture_status = capture_status
    expected_message = {
        "observer": "registered XI2 observer died during passive startup",
        "identity": "game identity changed at passive startup completion",
        "generation": "controller state changed during passive startup capture",
    }[failure]
    with pytest.raises(DriverError, match=expected_message):
        daemon.passive_startup()

    assert daemon._startup_evidence is None
    assert daemon._startup_operation["state"] == "blocked"
    assert daemon._passive_region_admission is None
    assert driver._monitor is None
    assert not driver.guardian.observer_process_groups
    assert not driver.guardian.armed


def test_runtime_start_reports_drill_needed_without_arming_or_legacy_calibration(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    driver = _RuntimeDriver()
    controller = Controller(tmp_path / "session.json", driver=driver)
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.controller = controller
    daemon.driver = driver
    daemon.passive_startup = lambda: evidence
    daemon.status_dict = lambda: {"state": controller.status().state.value}
    daemon._startup_operation = {"state": "ready"}
    daemon._takeover_drill_operation = {"state": "idle", "failure": ""}

    result = daemon.runtime_start(expected_generation=controller.status().generation)

    assert result["nextAction"] == "takeover_drill"
    assert result["takeoverDrill"]["state"] == "needed"
    assert controller.status().state is ControllerState.STOPPED
    assert not driver.guardian.armed
    assert driver.input_calibration_calls == 0


def _takeover_drill_fixture(tmp_path, evidence, human_event):
    class Guardian:
        generation = 0
        armed = False
        held = False
        active_process_groups = ()
        observer_process_groups = ()

    guardian = Guardian()
    callback = {"value": None}
    daemon_ref = {}

    class Monitor:
        alive = True
        failure_diagnostic = None

        def wait_for_human(self, predicate, *, timeout):
            del timeout
            daemon = daemon_ref["daemon"]
            assert daemon._takeover_drill_operation["state"] == "awaiting_input"
            status = daemon.status_dict()
            assert status["takeoverDrill"]["state"] == "awaiting_input"
            assert status["nextAction"] == "provide_human_input"
            current = callback["value"]
            if human_event is None:
                assert current is not None
                current(
                    XI2Event(
                        event_class=EventClass.KEY_PRESS,
                        type_number=2,
                        master_device=evidence.environment.keyboard_master,
                        source_device=evidence.environment.keyboard_source,
                        timestamp_ms=1,
                        detail=38,
                        root_xid=0x374,
                        event_xid=0x374,
                        child_xid=0x600001,
                    )
                )
                raise DriverError("human input sample deadline expired")
            assert current is not None
            forwarded_echo = XI2Event(
                event_class=EventClass.MOTION,
                type_number=6,
                master_device=evidence.environment.forwarded_echo_sources[0],
                source_device=evidence.environment.forwarded_echo_sources[0],
                timestamp_ms=9,
                detail=0,
                root_xid=0x374,
                event_xid=0x374,
                child_xid=0x600001,
                root_xy=(30.0, 40.0),
                event_xy=(30.0, 40.0),
                modifiers=(0, 0, 0),
                modifier_effective=0,
            )
            current(forwarded_echo)
            assert not predicate(())
            current(human_event)
            if not predicate(()):
                raise DriverError("human input sample deadline expired")

    monitor = Monitor()
    driver = DesktopDriver.__new__(DesktopDriver)
    backend = X11Backend.__new__(X11Backend)
    backend.observe_native_input_state = lambda _root: ((0, 0, 0), (), ())
    driver.backend = backend
    driver.guardian = guardian
    driver._monitor = monitor
    driver._takeover_input_callback = None

    def arm(_evidence):
        assert _evidence is evidence
        guardian.generation += 1
        guardian.armed = True
        return guardian.generation

    def revoke(_reason):
        guardian.generation += 1
        guardian.armed = False
        guardian.held = False
        return guardian.generation

    def stop_observer(current):
        assert current is monitor
        monitor.alive = False
        driver._monitor = None

    driver.arm = arm
    driver.revoke = revoke
    driver.run_takeover_hover = lambda generation, _evidence: (
        generation == guardian.generation and guardian.armed
    )

    def set_takeover_input_callback(value):
        callback["value"] = value

    driver.set_takeover_input_callback = set_takeover_input_callback
    driver.stop_input_observer = stop_observer

    class ControllerStub:
        def __init__(self):
            self.lock = threading.RLock()
            self.persisted = []
            self.value = SimpleNamespace(
                state=ControllerState.PAUSED,
                generation=70,
                stop_requested=False,
                action_intent="",
            )

        def status(self):
            with self.lock:
                return self.value

        def pause(self, reason="explicit pause"):
            with self.lock:
                self.value.generation += 1
                self.value.state = ControllerState.PAUSED
                driver.revoke(reason)
                return self.value

        def persist_takeover_drill(self, record):
            assert not guardian.armed and not guardian.held
            assert driver._monitor is None
            assert not monitor.alive
            assert callback["value"] is None
            assert daemon_ref["daemon"]._takeover_drill_operation["state"] == "awaiting_input"
            self.persisted.append(record)

    controller = ControllerStub()
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.controller = controller
    daemon.driver = driver
    daemon.passive_startup = lambda: evidence
    daemon.status_dict = lambda: {
        "controllerState": controller.value.state.value,
        "takeoverDrill": {"state": daemon._takeover_drill_operation["state"]},
        "nextAction": (
            "provide_human_input" if daemon._takeover_drill_operation["state"] == "awaiting_input" else "wait"
        ),
    }
    daemon._startup_operation = {"state": "ready"}
    daemon._takeover_drill_operation = {"state": "idle", "failure": ""}
    daemon._takeover_drill_lock = threading.Lock()
    daemon._takeover_cancel_lock = threading.Lock()
    daemon._takeover_cancel = threading.Event()
    daemon._takeover_event = None
    daemon_ref["daemon"] = daemon
    return daemon, driver, guardian, monitor, controller


def _hover_failure_context(raw_blocks: list[str]) -> dict[str, Any]:
    reason = "unattributed XI2 event class=RawKeyPress source=9 target=None"
    return {
        "schema": "crowley.takeover-hover-failure",
        "schemaVersion": 1,
        "stage": "pointer_emission",
        "guardianBeforeCleanup": {
            "generation": 72,
            "armed": False,
            "held": False,
            "revokedReason": reason,
        },
        "guardianAfterCleanup": {
            "generation": 72,
            "armed": False,
            "held": False,
            "revokedReason": reason,
        },
        "expectedAction": {
            "name": "takeover_hover",
            "expectedMotionCount": 31,
            "argv": ["xdotool", "mousemove", "10", "20", "sleep", "0.1"],
        },
        "observer": {
            "rawBlocks": raw_blocks,
            "partialRaw": "",
            "rawBytes": sum(len(block.encode("utf-8")) for block in raw_blocks),
            "rawBytesSeen": sum(len(block.encode("utf-8")) for block in raw_blocks),
            "rawBlocksSeen": len(raw_blocks),
            "rawCompleteBlocksSeen": len(raw_blocks),
            "rawBlocksRetained": len(raw_blocks),
            "rawTruncated": False,
            "parsedEventCount": len(raw_blocks),
            "parserFailure": None,
            "failureDiagnostic": None,
            "observerAlive": False,
        },
    }


def test_takeover_hover_failure_is_private_bounded_and_preserves_first_cause(tmp_path: Path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    raw_record = "EVENT type 13 (RawKeyPress)\ndevice: 3 (9)\ntime: 10\ndetail: 50\n\n"
    daemon, driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    driver._last_takeover_hover_failure_context = _hover_failure_context([raw_record])

    def failed_hover(_generation, _evidence):
        guardian.armed = False
        raise DriverError(
            "desktop command failed: xdotool mousemove 123 456 sleep 0.1 " + ("long-argv " * 200)
        )

    driver.run_takeover_hover = failed_hover
    with pytest.raises(DriverError) as failure:
        daemon.run_takeover_drill(expected_generation=70)

    assert str(failure.value).startswith("takeover hover Guardian revocation: unattributed XI2 event")
    assert str(failure.value).index("unattributed XI2 event") < str(failure.value).index(
        "desktop command failed"
    )
    assert controller.persisted == []
    assert daemon._takeover_drill_operation["state"] == "failed"
    artifact_path = tmp_path / "takeover-drill-diagnostics" / "latest-hover-failure.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["observer"]["rawBlocks"] == [raw_record]
    assert "-15" not in artifact["commandError"]["message"]
    assert "desktop command failed" in artifact["commandError"]["message"]
    assert artifact["cleanup"]["resourcesUnresolved"] is False
    assert artifact["cleanup"]["driverMonitorRegisteredAfterCleanup"] is False
    assert artifact["cleanup"]["guardianArmed"] is False
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact_path.parent.stat().st_mode) == 0o700


def test_takeover_hover_failure_diagnostic_write_error_does_not_mask_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    driver._last_takeover_hover_failure_context = _hover_failure_context([])

    def failed_hover(_generation, _evidence):
        guardian.armed = False
        raise DriverError("desktop command failed: private emitter terminated with signal 15")

    driver.run_takeover_hover = failed_hover

    def failed_persist(_context):
        raise OSError("private diagnostic disk write failed")

    monkeypatch.setattr(daemon, "_persist_takeover_hover_failure", failed_persist)
    with pytest.raises(DriverError, match="Guardian revocation: unattributed XI2 event") as failure:
        daemon.run_takeover_drill(expected_generation=70)

    assert "desktop command failed" in str(failure.value)
    assert "private diagnostic disk write failed" not in str(failure.value)
    assert controller.persisted == []
    assert daemon._takeover_drill_operation["state"] == "failed"
    assert not (tmp_path / "takeover-drill-diagnostics" / "latest-hover-failure.json").exists()


def test_takeover_hover_failure_artifact_discards_raw_overflow(tmp_path: Path) -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(journal=tmp_path / "session.json")
    raw_overflow = "x" * (2 * 1024 * 1024 + 1)
    artifact_path = daemon._persist_takeover_hover_failure(_hover_failure_context([raw_overflow]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact["artifactTruncated"] is True
    assert artifact["observer"]["rawBlocks"] == []
    assert artifact["observer"]["partialRaw"] == ""
    assert artifact["observer"]["rawTruncated"] is True
    assert artifact_path.stat().st_size <= 2 * 1024 * 1024
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact_path.parent.stat().st_mode) == 0o700


def test_takeover_drill_requires_non_xtest_sample_and_releases_before_persist(tmp_path) -> None:
    identity, _environment, evidence = _runtime_evidence()
    del identity
    human = XI2Event(
        event_class=EventClass.KEY_PRESS,
        type_number=2,
        master_device=9,
        source_device=9,
        timestamp_ms=10,
        detail=38,
        root_xid=0x374,
        event_xid=0x374,
        child_xid=0x600001,
    )
    daemon, _driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, human)

    result = daemon.run_takeover_drill(expected_generation=70)

    assert result["takeoverDrill"]["state"] == "passed"
    assert result["nextAction"] == "resume"
    assert "releaseLatencyMs" in result["takeoverDrill"]
    assert "detectionLatencyMs" not in result["takeoverDrill"]
    assert not guardian.armed and not guardian.held
    assert len(controller.persisted) == 1
    assert controller.persisted[0].environment_fingerprint == evidence.environment_fingerprint
    assert daemon._takeover_drill_operation["state"] == "passed"


def test_takeover_drill_stop_failure_releases_lock_but_keeps_observer_owned(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    human = XI2Event(
        event_class=EventClass.KEY_PRESS,
        type_number=2,
        master_device=9,
        source_device=9,
        timestamp_ms=10,
        detail=38,
        root_xid=0x374,
        event_xid=0x374,
        child_xid=0x600001,
    )
    daemon, driver, guardian, monitor, controller = _takeover_drill_fixture(tmp_path, evidence, human)
    guardian.observer_process_groups = (4242,)
    stop_calls = []
    arm_calls = []
    original_arm = driver.arm

    def refuse_observer_stop(current):
        assert current is monitor
        stop_calls.append(current)
        raise DriverError("XI2 observer termination could not be confirmed")

    driver.stop_input_observer = refuse_observer_stop
    driver.arm = lambda value: (arm_calls.append(value), original_arm(value))[1]

    with pytest.raises(DriverError, match="XI2 observer termination could not be confirmed"):
        daemon.run_takeover_drill(expected_generation=70)

    assert len(stop_calls) == 2  # pre-PASS stop and best-effort finalizer retry
    assert not controller.persisted
    assert not guardian.armed and not guardian.held
    assert monitor.alive and driver._monitor is monitor
    assert guardian.observer_process_groups == (4242,)
    assert daemon._takeover_event is None
    assert daemon._takeover_drill_operation["state"] == "failed"
    assert daemon._takeover_drill_operation["cleanupPending"] is True
    assert "cleanup failed" in daemon._takeover_drill_operation["failure"]
    assert daemon._takeover_drill_lock.acquire(blocking=False)
    daemon._takeover_drill_lock.release()
    arm_count_after_failure = len(arm_calls)

    # A later drill can acquire the single-flight lock but cannot arm against
    # the still-owned, unreaped observer. The owner remains able to retry reap.
    with pytest.raises(DriverError, match="previous takeover drill cleanup remains unresolved"):
        daemon.run_takeover_drill(expected_generation=70)

    assert len(arm_calls) == arm_count_after_failure
    assert not controller.persisted
    assert monitor.alive and driver._monitor is monitor
    assert guardian.observer_process_groups == (4242,)
    assert daemon._takeover_event is None
    assert daemon._takeover_drill_lock.acquire(blocking=False)
    daemon._takeover_drill_lock.release()


def test_takeover_drill_rechecks_generation_after_passive_startup_before_arm(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)
    arm_calls = []
    original_arm = driver.arm
    driver.arm = lambda value: (arm_calls.append(value), original_arm(value))[1]

    def concurrent_pause_during_passive_startup():
        controller.value.generation += 1
        return evidence

    daemon.passive_startup = concurrent_pause_during_passive_startup

    with pytest.raises(DriverError, match="generation changed before arm"):
        daemon.run_takeover_drill(expected_generation=70)

    assert not arm_calls
    assert not guardian.armed
    assert not controller.persisted
    assert daemon._takeover_drill_operation["state"] == "failed"


def test_takeover_drill_rechecks_generation_after_arm_before_hover(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)
    original_arm = driver.arm
    hover_calls = []

    def arm_then_concurrent_pause(value):
        generation = original_arm(value)
        controller.value.generation += 1
        return generation

    driver.arm = arm_then_concurrent_pause
    driver.run_takeover_hover = lambda *args: hover_calls.append(args)

    with pytest.raises(DriverError, match="generation changed before hover"):
        daemon.run_takeover_drill(expected_generation=70)

    assert not hover_calls
    assert not guardian.armed
    assert not controller.persisted
    assert daemon._takeover_drill_operation["state"] == "failed"


def test_takeover_pause_during_admitted_hover_returns_promptly_and_revokes_dispatch(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, driver, guardian, _monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)
    hover_entered = threading.Event()
    allow_hover_to_check = threading.Event()
    pause_returned = threading.Event()
    hover_dispatched = []
    outcome = []

    def admitted_hover(generation, _evidence):
        hover_entered.set()
        if not allow_hover_to_check.wait(1.0):
            raise AssertionError("test did not release the admitted hover barrier")
        if guardian.generation != generation or not guardian.armed:
            raise DriverError("takeover hover lease was revoked")
        hover_dispatched.append(generation)

    driver.run_takeover_hover = admitted_hover

    def run_drill():
        try:
            daemon.run_takeover_drill(expected_generation=70, timeout=0.1)
        except Exception as exc:
            outcome.append(exc)

    drill_thread = threading.Thread(target=run_drill)
    drill_thread.start()
    assert hover_entered.wait(1.0)

    def pause_controller():
        controller.pause("concurrent operator pause")
        pause_returned.set()

    pause_thread = threading.Thread(target=pause_controller)
    pause_thread.start()
    assert pause_returned.wait(0.25), "operator pause waited behind native hover work"
    allow_hover_to_check.set()
    drill_thread.join(timeout=2.0)
    pause_thread.join(timeout=2.0)

    assert not drill_thread.is_alive() and not pause_thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], DriverError)
    assert "revoked" in str(outcome[0])
    assert not hover_dispatched
    assert not guardian.armed and not guardian.held
    assert not controller.persisted
    assert daemon._takeover_drill_operation["state"] == "failed"


@pytest.mark.parametrize("race", ["generation", "cancel"], ids=["new-generation", "cancel"])
def test_takeover_drill_rechecks_lifecycle_before_persisting_pass(tmp_path, race) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    human = XI2Event(
        event_class=EventClass.KEY_PRESS,
        type_number=2,
        master_device=9,
        source_device=9,
        timestamp_ms=10,
        detail=38,
        root_xid=0x374,
        event_xid=0x374,
        child_xid=0x600001,
    )
    daemon, driver, guardian, monitor, controller = _takeover_drill_fixture(tmp_path, evidence, human)
    original_stop_observer = driver.stop_input_observer

    def race_after_release(current_monitor):
        original_stop_observer(current_monitor)
        if race == "generation":
            controller.value.generation += 1
        else:
            daemon.cancel_takeover_drill()

    driver.stop_input_observer = race_after_release

    expected_message = "generation changed before PASS persistence" if race == "generation" else "canceled"
    with pytest.raises(DriverError, match=expected_message):
        daemon.run_takeover_drill(expected_generation=70)

    assert not controller.persisted
    assert not guardian.armed and not guardian.held
    assert driver._monitor is None and not monitor.alive
    assert daemon._takeover_drill_operation["state"] == ("canceled" if race == "cancel" else "failed")


def test_takeover_human_sample_requires_measured_source_role_and_attachment() -> None:
    _identity, environment, _evidence = _runtime_evidence()
    valid_keyboard = XI2Event(
        2,
        EventClass.KEY_PRESS,
        9,
        9,
        10,
        38,
        0x374,
        0x374,
        0x600001,
    )
    valid_pointer = XI2Event(
        6,
        EventClass.MOTION,
        6,
        6,
        11,
        0,
        0x374,
        0x374,
        0x600001,
    )
    unknown_source = XI2Event(2, EventClass.KEY_PRESS, 99, 99, 12, 38, 0x374, 0x374, 0x600001)
    wrong_attachment_role = XI2Event(2, EventClass.KEY_PRESS, 6, 6, 13, 38, 0x374, 0x374, 0x600001)
    forwarded_echo = XI2Event(6, EventClass.MOTION, 7, 7, 14, 0, 0x374, 0x374, 0x600001)

    assert is_measured_human_action(valid_keyboard, environment)
    assert is_measured_human_action(valid_pointer, environment)
    assert not is_measured_human_action(unknown_source, environment)
    assert not is_measured_human_action(wrong_attachment_role, environment)
    assert not is_measured_human_action(forwarded_echo, environment)


@pytest.mark.parametrize(
    "event",
    [
        XI2Event(2, EventClass.KEY_PRESS, 99, 99, 12, 38, 0x374, 0x374, 0x600001),
        XI2Event(2, EventClass.KEY_PRESS, 6, 6, 13, 38, 0x374, 0x374, 0x600001),
        XI2Event(6, EventClass.MOTION, 7, 7, 14, 0, 0x374, 0x374, 0x600001),
    ],
    ids=["unknown-source", "wrong-role", "forwarded-echo"],
)
def test_unmeasured_takeover_source_never_completes_or_persists_drill(tmp_path, event) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, _driver, guardian, monitor, controller = _takeover_drill_fixture(tmp_path, evidence, event)

    with pytest.raises(DriverError, match="takeover human input deadline expired"):
        daemon.run_takeover_drill(expected_generation=70, timeout=0.01)

    assert not controller.persisted
    assert not guardian.armed and not guardian.held
    assert not monitor.alive
    assert daemon._takeover_drill_operation["state"] == "failed"


def test_takeover_drill_does_not_persist_known_xtest_input(tmp_path) -> None:
    _identity, _environment, evidence = _runtime_evidence()
    daemon, driver, guardian, monitor, controller = _takeover_drill_fixture(tmp_path, evidence, None)

    with pytest.raises(DriverError, match="takeover human input deadline expired"):
        daemon.run_takeover_drill(expected_generation=70, timeout=0.01)

    assert not controller.persisted
    assert not guardian.armed and not guardian.held
    assert driver._monitor is None
    assert not monitor.alive
    assert daemon._takeover_drill_operation["state"] == "failed"


@pytest.mark.parametrize(
    ("event_class", "master", "source", "expected"),
    [
        (EventClass.RAW_KEY_PRESS, 3, 9, True),
        (EventClass.RAW_BUTTON_PRESS, 2, 6, True),
        (EventClass.KEY_PRESS, 9, 9, True),
        (EventClass.RAW_KEY_PRESS, 8, 9, False),
        (EventClass.RAW_KEY_PRESS, 3, 5, False),  # attached XTEST keyboard
        (EventClass.KEY_PRESS, 19, 19, False),  # unmeasured source
        (EventClass.RAW_KEY_PRESS, 2, 6, False),  # pointer cannot satisfy a key sample
    ],
)
def test_takeover_human_classifier_binds_raw_and_delivered_source_roles(
    event_class, master, source, expected
) -> None:
    _identity, environment, _evidence = _runtime_evidence()
    event = XI2Event(
        event_class=event_class,
        type_number={
            EventClass.RAW_KEY_PRESS: 13,
            EventClass.RAW_BUTTON_PRESS: 15,
            EventClass.KEY_PRESS: 2,
        }[event_class],
        master_device=master,
        source_device=source,
        timestamp_ms=10,
        detail=50,
        root_xid=0x374,
        event_xid=0x374,
        child_xid=0x600001,
        root_xy=(30.0, 40.0),
        event_xy=(20.0, 20.0),
        modifiers=(0, 0, 0),
        modifier_effective=0,
    )

    assert is_measured_human_action(event, environment) is expected


def test_takeover_drill_records_raw_key_before_guardian_stops_observer(tmp_path: Path) -> None:
    """The real parser and Guardian preserve a physical raw-first takeover sample."""

    _identity, _environment, evidence = _runtime_evidence()
    raw_first_trace = (
        "EVENT type 13 (RawKeyPress)\n"
        "device: 3 (9)\n"
        "time: 10\n"
        "detail: 50\n\n"
        "EVENT type 2 (KeyPress)\n"
        "device: 9 (9)\n"
        "time: 10\n"
        "detail: 50\n"
        "windows: root 0x374 event 0x374 child 0x600001\n"
        "root: 30/40\n"
        "event: 20/20\n"
        "modifiers: locked 0 latched 0 base 0 effective: 0\n\n"
    )
    parsed_trace = parse_xi2_records(raw_first_trace)
    assert [event.event_class for event in parsed_trace] == [EventClass.RAW_KEY_PRESS, EventClass.KEY_PRESS]

    class ControllerStub:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self.persisted = []
            self.value = SimpleNamespace(
                state=ControllerState.PAUSED,
                generation=70,
                stop_requested=False,
                action_intent="",
            )

        def status(self):
            return self.value

        def persist_takeover_drill(self, record):
            assert not guardian.armed and not guardian.held
            assert driver._monitor is None
            self.persisted.append(record)

    class PipeProcess:
        def __init__(self, stream) -> None:
            self.stdout = stream
            self._returncode = None

        def poll(self):
            return self._returncode

        def terminate(self):
            self._returncode = 0

        def wait(self, timeout=None):
            del timeout
            self._returncode = 0
            return 0

        def kill(self):
            self._returncode = -9

    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r", encoding="ascii", buffering=1)
    writer_ready = threading.Event()
    ordered_callbacks: list[str] = []
    guardian = InputGuardian(tmp_path / "raw-first-guardian.lock", parent_pid=os.getpid())
    driver = DesktopDriver.__new__(DesktopDriver)
    backend = X11Backend.__new__(X11Backend)
    backend.observe_native_input_state = lambda _root: ((0, 0, 0), (), ())
    driver.backend = backend
    driver.guardian = guardian
    driver._input_evidence = evidence
    driver._calibration_action_lock = threading.RLock()
    driver._queued = []
    driver._calibration_ticket = None
    driver._calibration_due_waiters = {}
    driver._revoke_callbacks = []
    driver._held_input = False
    driver._monitor_calibration = False
    monitor = InputMonitor(evidence.display, driver._external_input, xauthority="private-test")
    process = PipeProcess(reader)
    monitor.observer.process = process  # type: ignore[assignment]
    driver._monitor = monitor
    guardian.on_revoke(driver._on_guardian_revoke)
    real_observe = guardian.observe

    def observe(event) -> bool:
        ordered_callbacks.append("guardian")
        return real_observe(event)

    guardian.observe = observe  # type: ignore[method-assign]
    real_set_callback = driver.set_takeover_input_callback

    def set_takeover_input_callback(callback) -> None:
        if callback is None:
            real_set_callback(None)
            return

        def before_guardian(event) -> None:
            ordered_callbacks.append("drill_callback")
            callback(event)

        real_set_callback(before_guardian)
        writer_ready.set()

    driver.set_takeover_input_callback = set_takeover_input_callback
    driver.arm = lambda runtime_evidence: guardian.arm(runtime_evidence)
    driver.run_takeover_hover = lambda _generation, _runtime_evidence: None
    monitor._started.set()
    monitor.thread = threading.Thread(target=monitor._read, name="test-xi2-raw-first", daemon=True)
    monitor.thread.start()

    controller = ControllerStub()
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.controller = controller
    daemon.driver = driver
    daemon.passive_startup = lambda: evidence
    daemon.status_dict = lambda: {"state": controller.value.state.value}
    daemon._startup_operation = {"state": "ready"}
    daemon._takeover_drill_operation = {"state": "idle", "failure": ""}
    daemon._takeover_drill_lock = threading.Lock()
    daemon._takeover_cancel_lock = threading.Lock()
    daemon._takeover_cancel = threading.Event()
    daemon._takeover_event = None

    def write_trace() -> None:
        assert writer_ready.wait(2.0)
        os.write(write_fd, raw_first_trace.encode("ascii"))
        os.close(write_fd)

    writer_thread = threading.Thread(target=write_trace, name="test-xi2-raw-first-writer", daemon=True)
    writer_thread.start()
    try:
        result = daemon.run_takeover_drill(expected_generation=70, timeout=1.0)
        writer_thread.join(timeout=1.0)

        assert result["takeoverDrill"]["state"] == "passed"
        assert len(controller.persisted) == 1
        assert ordered_callbacks == ["drill_callback", "guardian"]
        assert [event.event_class for event in monitor.events()] == [EventClass.RAW_KEY_PRESS]
        assert not guardian.armed and not guardian.held
    finally:
        if writer_thread.is_alive():
            try:
                os.close(write_fd)
            except OSError:
                pass
            writer_thread.join(timeout=1.0)
        try:
            reader.close()
        except OSError:
            pass
        guardian.close()


def test_driver_does_not_classify_forwarded_xtest_echo_as_human() -> None:
    _identity, _environment, evidence = _runtime_evidence()
    driver = DesktopDriver.__new__(DesktopDriver)
    driver._input_evidence = evidence
    classified = []
    observed = []
    driver._takeover_input_callback = classified.append
    driver.guardian = SimpleNamespace(armed=True, observe=observed.append)
    driver.revoke = lambda reason: pytest.fail(f"well-formed forwarded echo revoked input: {reason}")
    source = next(iter(evidence.forwarded_echo_sources))
    event = XI2Event(
        event_class=EventClass.MOTION,
        type_number=6,
        master_device=source,
        source_device=source,
        timestamp_ms=1,
        detail=0,
        root_xid=0x374,
        event_xid=0x374,
        child_xid=0x600001,
        root_xy=(30.0, 40.0),
        event_xy=(30.0, 40.0),
        modifiers=(0, 0, 0),
        modifier_effective=0,
    )

    driver._external_input(event)

    assert classified == []
    assert observed == [event]


def test_public_runtime_commands_route_through_daemon_owner(tmp_path: Path) -> None:
    driver = _RuntimeDriver()
    controller = Controller(tmp_path / "session.json", driver=driver)

    class Owner:
        calls = []

        def runtime_start(self, *, expected_generation, mode, resume):
            self.calls.append(("runtime_start", expected_generation, mode, resume))
            return {"route": "passive-startup"}

        def run_takeover_drill(self, *, expected_generation):
            self.calls.append(("takeover_drill", expected_generation))
            return {"route": "human-drill"}

        def cancel_takeover_drill(self):
            self.calls.append(("takeover_drill_cancel",))
            return {"route": "cancel"}

    owner = Owner()
    ipc = SessionIPC(controller, owner)
    started = ipc.dispatch({"command": "start", "generation": 0, "mode": "repeat"})
    resumed = ipc.dispatch({"command": "resume", "generation": 0})
    drilled = ipc.dispatch({"command": "takeover_drill", "generation": 0})
    canceled = ipc.dispatch({"command": "takeover_drill_cancel", "generation": 0})
    assert started == {"ok": True, "status": {"route": "passive-startup"}}
    assert resumed == {"ok": True, "status": {"route": "passive-startup"}}
    assert drilled == {"ok": True, "status": {"route": "human-drill"}}
    assert canceled == {"ok": True, "status": {"route": "cancel"}}
    assert owner.calls == [
        ("runtime_start", 0, "repeat", False),
        ("runtime_start", 0, "repeat", True),
        ("takeover_drill", 0),
        ("takeover_drill_cancel",),
    ]


def test_stormwind_route_verifies_target_and_binding_between_commands() -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    active_chat = {"active": True, "focused": True, "text": ""}
    inactive_chat = {"active": False, "focused": False, "text": ""}

    def snapshot(*, chat, target, action, in_range=True):
        return {
            "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
            "render": {"nativePhase": "normal", "nativeOrdinal": 0},
            "ahOpen": False,
            "blockingUI": {
                "staticPopupAvailable": True,
                "staticPopupShown": False,
                "gameMenuAvailable": True,
                "gameMenuShown": False,
            },
            "chatEdit": chat,
            "auctioneer": {
                "targetPresent": target is not None,
                "targetName": target or "",
                "rangeAvailable": target is not None,
                "withinTradeRange": bool(target is not None and in_range),
                "bindingAvailable": True,
                "ctrlF12Action": action,
                "interactionMode": "any",
            },
        }

    target_command = DesktopDriver.TARGET_AUCTIONEER_COMMAND
    binding_command = DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND
    target_chat = {"active": True, "focused": True, "text": target_command}
    binding_chat = {"active": True, "focused": True, "text": binding_command}
    values = [
        snapshot(chat=inactive_chat, target=None, action=""),
        snapshot(chat=inactive_chat, target=None, action=""),  # first fresh frame still predates chat open
        snapshot(chat=active_chat, target=None, action=""),
        snapshot(chat=target_chat, target=None, action=""),  # verified EditBox before Return
        snapshot(chat=target_chat, target=None, action=""),  # first post-Return frame remains pending
        snapshot(chat=inactive_chat, target=None, action=""),
        snapshot(chat=inactive_chat, target="Auctioneer Jaxon", action=""),
        snapshot(chat=inactive_chat, target="Auctioneer Jaxon", action=""),  # next chat-open frame is stale
        snapshot(chat=active_chat, target="Auctioneer Jaxon", action=""),
        snapshot(chat=binding_chat, target="Auctioneer Jaxon", action=""),  # pre-submit readback
        snapshot(chat=binding_chat, target="Auctioneer Jaxon", action=""),
        snapshot(chat=inactive_chat, target="Auctioneer Jaxon", action=""),
        snapshot(chat=inactive_chat, target="Auctioneer Jaxon", action="INTERACTTARGET"),
    ]

    class Driver:
        def __init__(self):
            self.calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            assert require_foreground is True
            return identity

        def run(self, name, generation, *, precondition, postcondition):
            assert name == "open_chat"
            assert generation == 7
            assert precondition(identity)
            self.calls.append(("run", name))
            assert postcondition(identity)

        def prepare_command(self, generation, command, _telemetry, **kwargs):
            assert generation == 7
            assert kwargs["precondition"](identity)
            self.calls.append(("command", command))
            assert kwargs["readback"]() == command
            assert kwargs["postcondition"](identity)

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    frames = [
        Frame(Snapshot(value), "boot", index, 1_000 + index, index, index, 1)
        for index, value in enumerate(values, 1)
    ]
    samples = [
        Sample("FRESH", frame, float(frame.sequence), (0, 0, 1, 1), frame.crc32, identity.fingerprint)
        for frame in frames
    ]
    daemon.capture_status = lambda *, deadline: samples.pop(0)
    snapshot_result, final_frame = daemon._prepare_auctioneer_open(
        generation=7,
        expected_identity=identity.fingerprint,
        dispatch_allowed=lambda: None,
    )
    assert snapshot_result["auctioneer"]["ctrlF12Action"] == "INTERACTTARGET"
    assert final_frame.sequence == 13
    assert daemon.driver.calls == [
        ("run", "open_chat"),
        ("command", DesktopDriver.TARGET_AUCTIONEER_COMMAND),
        ("run", "open_chat"),
        ("command", DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND),
    ]


def test_stormwind_route_closes_empty_focused_chat_before_interaction() -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    base = {
        "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
        "render": {"nativePhase": "normal", "nativeOrdinal": 0},
        "ahOpen": False,
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "auctioneer": {
            "targetPresent": True,
            "targetName": "Auctioneer Jaxon",
            "rangeAvailable": True,
            "withinTradeRange": True,
            "bindingAvailable": True,
            "ctrlF12Action": "INTERACTTARGET",
            "interactionMode": "keyboard",
        },
    }
    values = [
        {**base, "chatEdit": {"active": True, "focused": True, "text": ""}},
        {**base, "chatEdit": {"active": True, "focused": True, "text": ""}},
        {**base, "chatEdit": {"active": False, "focused": False, "text": ""}},
    ]

    class Driver:
        def __init__(self):
            self.calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            assert require_foreground is True
            return identity

        def run(self, name, generation, *, precondition, postcondition):
            assert name == "escape_command_buffer"
            assert generation == 7
            assert precondition(identity)
            self.calls.append(name)
            assert postcondition(identity)

        def prepare_command(self, *_args, **_kwargs):
            raise AssertionError("known target and binding need no chat command")

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    frames = [
        Frame(Snapshot(value), "boot", index, 1_000 + index, index, index, 1)
        for index, value in enumerate(values, 1)
    ]
    samples = [
        Sample("FRESH", frame, float(frame.sequence), (0, 0, 1, 1), frame.crc32, identity.fingerprint)
        for frame in frames
    ]
    daemon.capture_status = lambda *, deadline: samples.pop(0)

    snapshot_result, final_frame = daemon._prepare_auctioneer_open(
        generation=7,
        expected_identity=identity.fingerprint,
        dispatch_allowed=lambda: None,
    )

    assert snapshot_result["chatEdit"] == {"active": False, "focused": False, "text": ""}
    assert final_frame.sequence == 3
    assert daemon.driver.calls == ["escape_command_buffer"]


@pytest.mark.parametrize(
    ("native_phase", "native_ordinal"),
    [("held", 1), ("updates", 0), ("normal", 1)],
)
def test_stormwind_fresh_status_rejects_nonordinary_native_frames(
    native_phase: str, native_ordinal: int
) -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    frame = Frame(
        Snapshot({"render": {"nativePhase": native_phase, "nativeOrdinal": native_ordinal}}),
        "boot",
        1,
        1_001,
        1,
        1,
        1,
    )
    sample = Sample("FRESH", frame, 1.0, (0, 0, 1, 1), frame.crc32, identity.fingerprint)
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.capture_status = lambda *, deadline: sample
    daemon.driver = SimpleNamespace(resolve_identity=lambda **_kwargs: identity)

    with pytest.raises(DriverError, match="ordinary nativePhase=normal ordinal=0"):
        daemon._fresh_status_after(identity)


@pytest.mark.parametrize(
    ("snapshot_value", "message"),
    [
        (
            {
                "auctioneer": {
                    "targetPresent": True,
                    "targetName": "Auctioneer Jaxon",
                    "rangeAvailable": True,
                    "withinTradeRange": False,
                    "bindingAvailable": True,
                    "ctrlF12Action": "",
                    "interactionMode": "any",
                }
            },
            "outside the native trade-range predicate",
        ),
        (
            {
                "auctioneer": {
                    "targetPresent": True,
                    "targetName": "Auctioneer Jaxon",
                    "rangeAvailable": True,
                    "withinTradeRange": True,
                    "bindingAvailable": True,
                    "ctrlF12Action": "TOGGLEAUTORUN",
                    "interactionMode": "any",
                }
            },
            "will not be overwritten",
        ),
    ],
)
def test_stormwind_route_refuses_before_chat_when_range_or_binding_is_unsafe(
    snapshot_value: dict[str, Any], message: str
) -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    telemetry = {
        "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
        "render": {"nativePhase": "normal", "nativeOrdinal": 0},
        "ahOpen": False,
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "chatEdit": {"active": False, "focused": False, "text": ""},
        **snapshot_value,
    }
    frame = Frame(Snapshot(telemetry), "boot", 1, 1_000, 1, 1, 1)

    class Driver:
        calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            return identity

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def prepare_command(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    daemon.capture_status = lambda *, deadline: Sample(
        "FRESH", frame, 1.0, (0, 0, 1, 1), frame.crc32, identity.fingerprint
    )
    with pytest.raises(DriverError, match=message):
        daemon._prepare_auctioneer_open(
            generation=7,
            expected_identity=identity.fingerprint,
            dispatch_allowed=lambda: None,
        )
    assert daemon.driver.calls == []


@pytest.mark.parametrize(
    "blocking_ui",
    [
        {
            "staticPopupAvailable": True,
            "staticPopupShown": True,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        {
            "staticPopupAvailable": False,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": False,
            "gameMenuShown": False,
        },
        {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": True,
        },
    ],
    ids=["popup-visible", "popup-api-unavailable", "game-menu-api-unavailable", "game-menu-visible"],
)
def test_stormwind_route_refuses_before_any_input_with_blocking_ui(
    blocking_ui: dict[str, bool],
) -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    telemetry = {
        "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
        "render": {"nativePhase": "normal", "nativeOrdinal": 0},
        "ahOpen": False,
        "blockingUI": blocking_ui,
        "chatEdit": {"active": False, "focused": False, "text": ""},
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": True,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
    }
    frame = Frame(Snapshot(telemetry), "boot", 1, 1_000, 1, 1, 1)

    class Driver:
        calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            return identity

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def prepare_command(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    daemon.capture_status = lambda *, deadline: Sample(
        "FRESH", frame, 1.0, (0, 0, 1, 1), frame.crc32, identity.fingerprint
    )
    with pytest.raises(DriverError, match="Stormwind world scene"):
        daemon._prepare_auctioneer_open(
            generation=7,
            expected_identity=identity.fingerprint,
            dispatch_allowed=lambda: None,
        )
    assert daemon.driver.calls == []


@pytest.mark.parametrize(
    ("interaction_mode", "message"),
    [("disabled", "disabled"), ("gamepad", "disabled"), ("unavailable", "unavailable")],
)
def test_stormwind_route_refuses_before_chat_without_keyboard_interaction(
    interaction_mode: str, message: str
) -> None:
    identity = SimpleNamespace(fingerprint="game-identity")
    telemetry = {
        "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
        "render": {"nativePhase": "normal", "nativeOrdinal": 0},
        "ahOpen": False,
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "chatEdit": {"active": False, "focused": False, "text": ""},
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": True,
            "ctrlF12Action": "",
            "interactionMode": interaction_mode,
        },
    }
    frame = Frame(Snapshot(telemetry), "boot", 1, 1_000, 1, 1, 1)

    class Driver:
        calls = []

        def resolve_identity(self, *, require_foreground, deadline_monotonic=None):
            return identity

        def run(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def prepare_command(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(profile=SimpleNamespace(character="Arthur", realm="Beta"))
    daemon.driver = Driver()
    daemon.capture_status = lambda *, deadline: Sample(
        "FRESH", frame, 1.0, (0, 0, 1, 1), frame.crc32, identity.fingerprint
    )
    with pytest.raises(DriverError, match=message):
        daemon._prepare_auctioneer_open(
            generation=7,
            expected_identity=identity.fingerprint,
            dispatch_allowed=lambda: None,
        )
    assert daemon.driver.calls == []
