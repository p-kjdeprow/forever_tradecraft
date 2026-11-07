"""Real private-Xvfb rehearsal for the production calibration keyboard path."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import asdict
from multiprocessing import resource_tracker
from pathlib import Path

import pytest

from crowley.session import calibration as calibration_module
from crowley.session import driver as driver_module
from crowley.session import pointer_execution as pointer_execution_module
from crowley.session.calibration import InputCalibrationWorkflow, OwnedGtkCalibrationWindow
from crowley.session.driver import DesktopDriver, DriverProfile, WindowIdentity, X11Backend
from tests.x11_startup import start_private_xvfb


class _ReachedHumanWait(Exception):
    """Stop after the actual workflow enters its no-input human wait."""


_NATIVE_TOOLS = ("Xvfb", "xinput", "xprop", "xwininfo", "xauth", "dbus-daemon")
pytestmark = pytest.mark.skipif(
    any(shutil.which(name) is None for name in _NATIVE_TOOLS)
    or not Path("/usr/bin/python3.13").is_file()
    or ctypes.util.find_library("X11") is None,
    reason="the private-Xvfb native rehearsal requires GTK/X11 tools",
)


def _proc_stat_fields(pid: int) -> list[str] | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError):
        return None
    closing = stat_text.rfind(")")
    if closing < 0:
        return None
    return stat_text[closing + 2 :].split()


def _descendants(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 2:
            continue
        try:
            parents[pid] = int(fields[1])
        except ValueError:
            continue
    descendants: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        children = {pid for pid, ppid in parents.items() if ppid == parent}
        new_children = children - descendants
        descendants.update(new_children)
        pending.extend(new_children)
    return descendants


def _process_group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fields = _proc_stat_fields(pid)
        if fields is None or len(fields) < 3:
            continue
        try:
            if int(fields[2]) == pgid:
                members.add(pid)
        except ValueError:
            continue
    return members


def _close_process_streams(process: subprocess.Popen) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _terminate_process_group(process: subprocess.Popen, *, timeout: float = 2.0) -> None:
    """Reap a test-owned process and every member of its own session."""

    pgid = process.pid
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass

    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"process group {pgid} leader was not reaped") from exc

    deadline = time.monotonic() + timeout
    while True:
        members = _process_group_members(pgid)
        if not members:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"process group {pgid} was not reaped: {sorted(members)}")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            break
        time.sleep(0.01)


def _register_process_cleanup(cleanup: ExitStack, process: subprocess.Popen) -> None:
    """Register process cleanup immediately; ExitStack keeps callbacks independent."""

    cleanup.callback(_close_process_streams, process)
    cleanup.callback(_terminate_process_group, process)


def _private_bus(tmp_path: Path, cleanup: ExitStack) -> tuple[subprocess.Popen[str], str, Path]:
    socket_path = tmp_path / "session-bus.sock"
    config_path = tmp_path / "session-bus.conf"
    config_path.write_text(
        "\n".join(
            (
                "<busconfig>",
                "  <type>session</type>",
                f"  <listen>unix:path={socket_path}</listen>",
                "  <auth>EXTERNAL</auth>",
                '  <policy context="default">',
                '    <allow user="*"/>',
                '    <allow own="*"/>',
                '    <allow send_destination="*"/>',
                '    <allow receive_sender="*"/>',
                "  </policy>",
                "</busconfig>",
                "",
            )
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    process = subprocess.Popen(
        [
            "dbus-daemon",
            "--nofork",
            "--print-address=1",
            f"--config-file={config_path}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    _register_process_cleanup(cleanup, process)
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 3.0)
    if not ready:
        raise AssertionError("private no-activation D-Bus did not publish its address")
    address = process.stdout.readline().strip()
    if not address:
        stderr = process.stderr.read() if process.stderr is not None and process.poll() is not None else ""
        raise AssertionError(f"private D-Bus failed to start: {stderr.strip()}")
    assert address.startswith("unix:path=")
    assert str(tmp_path) in address
    assert "servicedir" not in config_path.read_text(encoding="utf-8")
    return process, address, config_path


def _private_xvfb(tmp_path: Path, cleanup: ExitStack) -> tuple[subprocess.Popen[bytes], str, str, int]:
    xauthority = tmp_path / "private.Xauthority"
    xauthority.touch(mode=0o600)
    xauthority.chmod(0o600)
    xvfb, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1920x1080x24",
            "-ac",
            "-extension",
            "GLX",
        ),
        popen_kwargs={
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
        },
    )
    _register_process_cleanup(cleanup, xvfb)
    x11.XCloseDisplay(xdisplay)
    return xvfb, display, str(xauthority), xvfb.pid


def _proc_start(pid: int) -> str:
    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = stat_text.rfind(")")
    return stat_text[closing + 2 :].split()[19]


def _sha256(path: str | os.PathLike[str]) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.fixture
def _private_native_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ExitStack, subprocess.Popen[str], str, str, str, Path]]:
    """Own the private servers for the whole test, including setup failures."""

    before = _descendants(os.getpid())
    tmp_path.chmod(0o700)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    pointer_rate_dir = tmp_path / "pointer-rate"
    pointer_rate_dir.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution_module, "_session_state_directory", lambda: pointer_rate_dir)
    xdg_data_home = tmp_path / "xdg-data"
    xdg_data_home.mkdir(mode=0o700)
    try:
        with ExitStack() as cleanup:
            bus, bus_address, _bus_config = _private_bus(tmp_path, cleanup)
            _xvfb, display, xauthority, _xvfb_pid = _private_xvfb(tmp_path, cleanup)
            monkeypatch.setenv("DISPLAY", display)
            monkeypatch.setenv("XAUTHORITY", xauthority)
            monkeypatch.setenv("GDK_BACKEND", "x11")
            monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus_address)
            monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path / 'no-system-bus.sock'}")
            monkeypatch.delenv("DBUS_STARTER_ADDRESS", raising=False)
            monkeypatch.delenv("DBUS_STARTER_BUS_TYPE", raising=False)
            monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
            monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
            monkeypatch.setenv("NO_AT_BRIDGE", "1")
            yield cleanup, bus, bus_address, display, xauthority, runtime_dir
    finally:
        # multiprocessing's per-interpreter resource tracker starts lazily on first use and lives until the
        # interpreter exits; it is not a rehearsal process.
        tracker_pid = getattr(resource_tracker._resource_tracker, "_pid", None)
        leaked = _descendants(os.getpid()) - before - {tracker_pid}
        # Reap anything left (for example the Guardian watchdog when driver.close() failed) before failing,
        # so a failing run never leaves processes behind.
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in leaked:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        assert not leaked, f"new child processes remained after private rehearsal: {sorted(leaked)}"


def _destroy_x11_window(x11: ctypes.CDLL, xdisplay: int, window: int) -> None:
    x11.XDestroyWindow(xdisplay, window)
    x11.XSync(xdisplay, 0)


def test_real_private_xi2_keyboard_profile_reaches_human_wait_without_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _private_native_processes: tuple[ExitStack, subprocess.Popen[str], str, str, str, Path],
) -> None:
    """Run production XTEST, xinput, parser, validator and workflow in isolation."""

    checkout = Path(__file__).resolve().parents[1]
    expected_driver = checkout / "src/crowley/session/driver.py"
    expected_calibration = checkout / "src/crowley/session/calibration.py"
    assert Path(driver_module.__file__).resolve() == expected_driver.resolve()
    assert Path(calibration_module.__file__).resolve() == expected_calibration.resolve()
    assert Path(DesktopDriver._calibration_keyboard_operations.__code__.co_filename).resolve() == (
        expected_driver.resolve()
    )

    cleanup, bus, bus_address, display, xauthority, runtime_dir = _private_native_processes

    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    xdisplay = x11.XOpenDisplay(display.encode("ascii"))
    assert xdisplay
    cleanup.callback(x11.XCloseDisplay, xdisplay)
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    root_xid = x11.XDefaultRootWindow(xdisplay)
    x11.XCreateSimpleWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    x11.XCreateSimpleWindow.restype = ctypes.c_ulong
    x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    private_game_xid = x11.XCreateSimpleWindow(xdisplay, root_xid, 0, 0, 320, 180, 0, 0, 0)
    assert private_game_xid > 0
    cleanup.callback(_destroy_x11_window, x11, xdisplay, private_game_xid)
    x11.XMapWindow(xdisplay, private_game_xid)
    x11.XSync(xdisplay, 0)
    x11.XSetInputFocus(xdisplay, private_game_xid, 2, 0)
    x11.XSync(xdisplay, 0)
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
    assert x11.XWarpPointer(xdisplay, 0, root_xid, 0, 0, 0, 0, 1809, 43) == 1
    x11.XSync(xdisplay, 0)

    driver: DesktopDriver | None = None
    backend: X11Backend | None = None
    target_refs: list[OwnedGtkCalibrationWindow] = []
    target_window_refs = []
    actual_facts: dict[str, int] = {}
    command_log: list[tuple[str, ...]] = []
    proof: dict[str, object] = {}
    observer_ref: list[object] = []
    observer_process_refs: list[subprocess.Popen[str]] = []
    observer_reader_refs: list[object] = []
    try:
        profile = DriverProfile(display=display, xauthority=xauthority)
        backend = X11Backend(profile)
        cleanup.callback(backend.close)
        monkeypatch.setattr(
            driver_module,
            "GNOME_CAPTURE_LOCK_PATH",
            tmp_path / "private-capture.lock",
        )
        driver = DesktopDriver(
            profile=profile,
            backend=backend,
            lease_path=tmp_path / "private-desktop.lock",
        )

        def known_processes() -> tuple[subprocess.Popen, ...]:
            processes: dict[int, subprocess.Popen] = {}

            def add(candidate: object) -> None:
                if isinstance(candidate, subprocess.Popen):
                    processes[candidate.pid] = candidate

            for process in observer_process_refs:
                add(process)
            for observer in observer_ref:
                add(getattr(getattr(observer, "observer", None), "process", None))
            for window in target_window_refs:
                add(getattr(window, "process", None))
            for target in target_refs:
                add(getattr(getattr(target, "window", None), "process", None))
            monitor = getattr(driver, "_monitor", None)
            add(getattr(getattr(monitor, "observer", None), "process", None))
            for process in getattr(backend, "_active_processes", ()):
                add(process)
            return tuple(processes.values())

        def close_driver_and_known_processes() -> None:
            actions: list[Callable[[], None]] = [driver.close]
            actions.extend(
                lambda process=process: _terminate_process_group(process) for process in known_processes()
            )
            errors: list[BaseException] = []
            for action in actions:
                try:
                    action()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("private rehearsal cleanup failed", errors)

        cleanup.callback(close_driver_and_known_processes)

        def private_no_wm_focus(
            window_id: str,
            *,
            generation: int | None = None,
            calibration_ticket=None,
        ) -> None:
            # Xvfb has no EWMH manager, so windowactivate is unavailable.
            # Preserve the production DesktopDriver ticket check and issue a
            # ticket-bound native windowfocus to this exact private GTK XID.
            assert generation is None and calibration_ticket is not None
            assert int(window_id, 0) == calibration_ticket.target_xid
            props = backend._run(
                ["xprop", "-id", window_id, "WM_NAME", "_NET_WM_PID"],
                calibration_ticket=calibration_ticket,
            ).stdout
            assert OwnedGtkCalibrationWindow.TITLE in props
            assert f"= {calibration_ticket.target_pid}" in props
            backend._run(
                ["xdotool", "windowfocus", "--sync", window_id],
                calibration_ticket=calibration_ticket,
            )
            focused = int(
                backend._run(
                    ["xdotool", "getwindowfocus"],
                    calibration_ticket=calibration_ticket,
                ).stdout.strip(),
                0,
            )
            assert focused == calibration_ticket.target_xid
            backend._calibration_focus_ticket = calibration_ticket

        backend.calibration_focus = private_no_wm_focus  # type: ignore[method-assign]
        original_backend_run = backend._run

        def private_no_wm_run(args: list[str], **kwargs):
            if args[:3] == ["xdotool", "windowactivate", "--sync"]:
                assert actual_facts and int(args[3], 0) == actual_facts["xid"]
                # The production calibration path requests EWMH activation.
                # With no WM in this isolated Xvfb, preserve the same ticket
                # fence and use xdotool's direct focus operation for this XID.
                args = ["xdotool", "windowfocus", "--sync", args[3]]
            return original_backend_run(args, **kwargs)

        backend._run = private_no_wm_run  # type: ignore[method-assign]

        def private_command(argv: list[str], *, display: str, xauthority: str, timeout: float = 5.0) -> str:
            command_log.append(tuple(argv))
            if argv == ["wmctrl", "-lp"]:
                # Xvfb has no window manager. Bind its compatibility row only
                # to the identity already emitted by this actual GTK child;
                # properties, tree, focus and all XI2 facts remain live.
                if not actual_facts:
                    return ""
                xid = actual_facts["xid"]
                props = subprocess.run(
                    ["xprop", "-id", hex(xid), "WM_NAME", "_NET_WM_PID"],
                    env=os.environ.copy() | {"DISPLAY": display, "XAUTHORITY": xauthority},
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=max(0.01, timeout),
                ).stdout
                if OwnedGtkCalibrationWindow.TITLE not in props or f"= {actual_facts['pid']}" not in props:
                    return ""
                return f"{hex(xid)} -1 {actual_facts['pid']} private-xvfb {OwnedGtkCalibrationWindow.TITLE}\n"
            return calibration_module._run_command(
                argv,
                display=display,
                xauthority=xauthority,
                timeout=max(0.01, timeout),
            )

        def window_factory() -> OwnedGtkCalibrationWindow:
            target = OwnedGtkCalibrationWindow(
                display=display,
                xauthority=xauthority,
                python_executable="/usr/bin/python3.13",
                command=private_command,
                layout_kind=OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD,
            )
            original_verify = target._verify

            def capture_actual_identity(pid: int, xid: int, process, **kwargs):
                actual_facts.update(pid=pid, xid=xid)
                window = original_verify(pid, xid, process, **kwargs)
                target_window_refs.append(window)
                return window

            target._verify = capture_actual_identity  # type: ignore[method-assign]
            target_refs.append(target)
            return target

        # Calibration's unrelated game identity is an isolated fixture. It is
        # never used for dispatch; the GTK target, ticket, XTEST events and
        # XI2 proof all use their real native providers.
        fake_game_identity = WindowIdentity(
            xid=int(private_game_xid),
            pid=os.getpid(),
            executable=sys.executable,
            title="private rehearsal identity fixture",
            wm_class="private-rehearsal",
            child_xid=int(private_game_xid),
            outer_geometry=(0, 0, 1920, 1080),
            child_geometry=(0, 0, 1920, 1080),
            foreground_xid=int(private_game_xid),
            process_start=_proc_start(os.getpid()),
            display=display,
            fingerprint="private-xvfb-game-identity-fixture",
            root_xid=root_xid,
            target_event_xids=frozenset({int(private_game_xid)}),
        )
        driver.resolve_identity = lambda **_kwargs: fake_game_identity  # type: ignore[method-assign]

        readiness_facts: dict[str, object] = {}
        idle_facts: dict[str, object] = {}

        def on_ready(readiness) -> None:
            readiness_facts["target_xid"] = readiness.target_xid
            readiness_facts["target_pid"] = readiness.target_pid
            readiness_facts["keyboard_profile"] = [
                [name, payload, [list(step) for step in steps]]
                for name, payload, steps in readiness.keyboard_profile
            ]

        workflow = InputCalibrationWorkflow(
            driver,
            display=display,
            window_factory=window_factory,
            on_ready=on_ready,
        )
        original_start_observer = driver.start_input_observer

        def start_and_watch_observer(*, calibration: bool, callback):
            # Xvfb emits DeviceChanged for XTEST devices on their first actual
            # use. Prime those real devices against the already-owned private
            # GTK window before observer startup, then prove every physical
            # control is up. The production synthetic sequence below still
            # uses the normal ticketed pinned emitter and untouched validator.
            assert calibration is True
            assert len(target_refs) == 1 and backend is not None
            window = target_refs[0].window
            assert window is not None
            target_props = subprocess.run(
                ["xprop", "-id", hex(window.xid), "WM_NAME", "_NET_WM_PID"],
                env=os.environ.copy() | {"DISPLAY": display, "XAUTHORITY": xauthority},
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
            assert window.title in target_props
            assert f"= {window.pid}" in target_props

            pinned_xdotool = str(backend.input_tool.executable)
            assert _sha256(pinned_xdotool) == backend.input_tool_digest
            private_env = os.environ.copy() | {"DISPLAY": display, "XAUTHORITY": xauthority}

            def private_xtest(*args: str) -> None:
                completed = subprocess.run(
                    [pinned_xdotool, *args],
                    env=private_env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                assert completed.returncode == 0

            private_xtest("windowfocus", "--sync", hex(window.xid))
            private_xtest("key", "--", "F13")
            left, top, width, height = window.geometry
            x, y = left + width // 2, top + height // 2
            prime_x, prime_y = x + 1, y + 1
            private_xtest("mousemove", "--sync", str(x + 2), str(y + 2))
            private_xtest("mousemove", "--sync", str(prime_x), str(prime_y))
            focus_after_prime = subprocess.run(
                ["xdotool", "getwindowfocus"],
                env=private_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            assert int(focus_after_prime, 0) == window.xid
            pointer_after_prime = backend.pointer_position()
            assert pointer_after_prime == (prime_x, prime_y, window.xid)
            hierarchy = driver.input_hierarchy()
            key_source, pointer_source = calibration_module.xtest_sources(hierarchy)
            for source in (key_source, pointer_source):
                state = subprocess.run(
                    ["xinput", "query-state", str(source)],
                    env=private_env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout
                physical_states = re.findall(r"\b(?:key|button)\[\d+\]=(up|down)", state)
                assert physical_states and set(physical_states) == {"up"}
            proof["private_xtest_precondition"] = {
                "target_pid": window.pid,
                "target_xid": window.xid,
                "pinned_xdotool_sha256": backend.input_tool_digest,
                "synthetic_key_source": key_source,
                "synthetic_pointer_source": pointer_source,
                "focus_xid_after_prime": int(focus_after_prime, 0),
                "pointer_after_prime": list(pointer_after_prime),
                "all_key_and_button_states_up": True,
            }

            observer = original_start_observer(calibration=calibration, callback=callback)
            observer_ref.append(observer)
            original_collect = observer.collect_until

            def collect_until(predicate, *, timeout: float):
                trace = original_collect(predicate, timeout=timeout)
                before = observer.events()
                process = observer.observer.process
                reader = observer.thread
                assert process is not None and process.poll() is None
                assert process.stdout is not None and not process.stdout.closed
                assert reader is not None and reader.is_alive()
                observer_process_refs.append(process)
                observer_reader_refs.append(reader)
                snapshot = observer.diagnostic_snapshot()
                assert snapshot["parserFailure"] is None
                assert snapshot["failureDiagnostic"] is None
                time.sleep(0.2)
                after = observer.events()
                assert after == before, "XI2 emitted another event after the final synthetic release"
                assert process.poll() is None, "XI2 observer reached EOF after the final release"
                assert process.stdout is not None and not process.stdout.closed
                assert reader.is_alive()
                idle_facts.update(
                    alive_after_idle=True,
                    reader_alive_after_idle=True,
                    pipe_open_after_idle=True,
                    event_count_before=len(before),
                    event_count_after=len(after),
                    parsed_event_count=snapshot["parsedEventCount"],
                    raw_bytes=snapshot["rawBytes"],
                    partial_raw=snapshot["partialRaw"],
                    raw_truncated=snapshot["rawTruncated"],
                    parser_failure=snapshot["parserFailure"],
                    failure_diagnostic=snapshot["failureDiagnostic"],
                )
                proof["synthetic_trace"] = trace
                proof["synthetic_event_count"] = len(after)
                proof["synthetic_events"] = [asdict(event) for event in after]
                proof["observer_pid"] = process.pid
                proof["observer_process_start"] = _proc_start(process.pid)
                proof["observer_executable"] = os.path.realpath(os.readlink(f"/proc/{process.pid}/exe"))
                proof["observer_xinput_executable_sha256"] = _sha256(shutil.which("xinput") or "xinput")
                return trace

            observer.collect_until = collect_until  # type: ignore[method-assign]
            original_wait = observer.wait_for_human

            def bounded_wait(predicate, *, timeout: float) -> None:
                assert readiness_facts, "workflow did not publish readiness before human wait"
                assert target_refs and target_refs[0].window is not None
                assert target_refs[0].window.process.poll() is None
                idle_facts["target_process_alive_at_human_wait"] = True
                before = observer.events()
                process = observer.observer.process
                reader = observer.thread
                assert process is not None and process.poll() is None
                assert reader is not None and reader.is_alive()
                try:
                    original_wait(predicate, timeout=min(timeout, 0.15))
                except driver_module.DriverError as exc:
                    assert str(exc) == "human input sample deadline expired"
                else:
                    raise AssertionError("private rehearsal unexpectedly accepted a human sample")
                after = observer.events()
                assert after == before, "an input event appeared during the human-wait boundary"
                assert process.poll() is None and reader.is_alive()
                idle_facts["human_wait_entered_without_input"] = True
                raise _ReachedHumanWait

            observer.wait_for_human = bounded_wait  # type: ignore[method-assign]
            return observer

        driver.start_input_observer = start_and_watch_observer  # type: ignore[method-assign]
        try:
            workflow.run(human_timeout=30.0)
        except _ReachedHumanWait:
            pass
        except Exception as exc:
            observer = observer_ref[0] if observer_ref else None
            events = observer.events() if observer is not None else []
            classes: dict[str, int] = {}
            for event in events:
                classes[event.event_class.value] = classes.get(event.event_class.value, 0) + 1
            print(
                "private rehearsal stopped before readiness:",
                repr(exc),
                "guardian=",
                getattr(driver.guardian, "revoked_reason", ""),
                "predicate=",
                workflow._predicate_reasons,
                "event_count=",
                len(events),
                "classes=",
                classes,
                "key_edges=",
                [(event.event_class.value, event.detail) for event in events if event.source_device == 5][:8]
                + [(event.event_class.value, event.detail) for event in events if event.source_device == 5][
                    -8:
                ],
                "raw=",
                observer.diagnostic_snapshot().get("rawBlocks", [])[-1:] if observer is not None else [],
                "partial=",
                observer.diagnostic_snapshot().get("partialRaw", "") if observer is not None else "",
            )
            raise

        assert workflow.readiness is not None
        assert workflow.ready_for_human.is_set() is False
        assert readiness_facts
        assert idle_facts["human_wait_entered_without_input"] is True
        assert idle_facts["event_count_before"] == idle_facts["event_count_after"]
        assert idle_facts["parser_failure"] is None
        assert idle_facts["failure_diagnostic"] is None
        assert len(target_refs) == 1
        target = target_refs[0]
        assert target_window_refs
        window = target_window_refs[-1]
        assert all(
            (item.pid, item.xid, item.geometry) == (window.pid, window.xid, window.geometry)
            for item in target_window_refs
        )
        process = window.process
        assert window.layout_kind == OwnedGtkCalibrationWindow.LAYOUT_ROOT_CHILD
        assert window.managed_frame_xid is None
        assert window.parent_xid == window.root_xid == root_xid
        assert window.allowed_event_target_xids == frozenset({window.xid})
        assert window.descendant_xids == frozenset({window.xid, *window.excluded_input_only_xids})
        assert window.excluded_input_only_xids
        assert window.geometry[2:] == (480, 240)
        assert workflow.readiness.target_xid == window.xid
        assert workflow.readiness.target_pid == window.pid
        assert workflow.readiness.keyboard_profile
        last_profile_row = workflow.readiness.keyboard_profile[-1]
        assert last_profile_row[0] == "type_allowlisted_command"
        assert last_profile_row[1] == sorted(DesktopDriver.COMMANDS)[-1]
        assert last_profile_row[2][-1][0] == "release"
        key_source = workflow.readiness.synthetic_key_source
        key_events = [event for event in proof["synthetic_events"] if event["source_device"] == key_source]
        expected_key_records = 4 + 2 * sum(
            len(operation.steps) for operation in workflow._keyboard_operations
        )
        assert len(key_events) == expected_key_records
        assert not any(event["event_class"] == "DeviceChanged" for event in key_events)
        delivered_key_releases = [event for event in key_events if event["event_class"] == "KeyRelease"]
        assert delivered_key_releases
        assert delivered_key_releases[-1]["detail"] == last_profile_row[2][-1][2]
        assert target.window is None
        assert process.poll() is not None
        assert driver._monitor is None
        assert not driver.guardian.active_process_groups
        assert not driver.guardian.observer_process_groups
        assert not driver._calibration_target_process_groups
        assert not driver._calibration_bootstrap_process_groups
        assert not backend.active_process_groups

        observer = observer_ref[0]
        assert len(observer_process_refs) == len(observer_reader_refs) == 1
        observer_process = observer_process_refs[0]
        observer_reader = observer_reader_refs[0]
        assert observer_process.poll() is not None
        assert not observer_reader.is_alive()
        proof.update(
            schema="private-xvfb-real-xi2-keyboard-rehearsal-v1",
            display=display,
            private_xvfb=True,
            private_xauthority_path=xauthority,
            private_no_activation_session_bus=bus_address,
            private_runtime_dir=str(runtime_dir),
            no_host_gui_or_host_input=True,
            target={
                "pid": window.pid,
                "process_start": window.process_start,
                "executable": window.executable,
                "xid": window.xid,
                "geometry": list(window.geometry),
                "layout_kind": window.layout_kind,
                "root_xid": window.root_xid,
                "parent_xid": window.parent_xid,
                "allowed_event_target_xids": sorted(window.allowed_event_target_xids),
                "excluded_input_only_xids": sorted(window.excluded_input_only_xids),
            },
            readiness=readiness_facts,
            idle_observer=idle_facts,
            prompt_shown=True,
            driver_ready_callback_invoked=bool(readiness_facts),
            human_sample_supplied=False,
            target_reaped=True,
            observer_reaped=True,
            guardian_active_process_groups=[],
            guardian_observer_process_groups=[],
            source_sha256={
                "driver.py": _sha256(Path(driver_module.__file__)),
                "calibration.py": _sha256(Path(calibration_module.__file__)),
                "input_events.py": _sha256(Path(calibration_module.__file__).with_name("input_events.py")),
            },
            source_paths={
                "driver.py": str(Path(driver_module.__file__).resolve()),
                "calibration.py": str(Path(calibration_module.__file__).resolve()),
            },
            runtime_operation_count=len(workflow._keyboard_operations),
            runtime_keyboard_physical_step_count=sum(
                len(operation.steps) for operation in workflow._keyboard_operations
            ),
            expected_keyboard_record_count=4
            + 2 * sum(len(operation.steps) for operation in workflow._keyboard_operations),
            pinned_xtest_tool_sha256=backend.input_tool_digest,
            xinput_version=subprocess.run(
                ["xinput", "--version"],
                env=os.environ.copy(),
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip(),
            xinput_sha256=_sha256(shutil.which("xinput") or "xinput"),
            command_log=[list(argv) for argv in command_log],
            strict_keyboard_validator_passed_by_workflow=True,
            strict_pointer_validator_passed_by_workflow=True,
            completion="synthetic readiness published, prompt shown, entered actual no-input wait without a human sample",
        )

        artifact_root = os.environ.get("CROWLEY_INPUT92_PROOF_DIR")
        if artifact_root:
            output_dir = Path(artifact_root)
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            output_dir.chmod(0o700)
            trace = str(proof.pop("synthetic_trace"))
            trace_path = output_dir / "xi2-synthetic-trace.txt"
            trace_path.write_text(trace, encoding="utf-8")
            trace_path.chmod(0o600)
            proof["synthetic_trace_path"] = str(trace_path)
            proof["synthetic_trace_sha256"] = _sha256(trace_path)
            proof_path = output_dir / "native-proof.json"
            proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            proof_path.chmod(0o600)
            assert stat_mode(output_dir) == 0o700
            assert stat_mode(proof_path) == 0o600
            assert stat_mode(trace_path) == 0o600
    finally:
        cleanup.close()
