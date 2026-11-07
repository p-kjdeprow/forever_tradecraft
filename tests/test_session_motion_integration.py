from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import crowley.session.daemon as daemon_module
import crowley.session.driver as driver_module
from crowley.session import Controller, ControllerState, DesktopDriver, SessionConfig, SessionMode
from crowley.session.calibration import InputCalibrationEvidence
from crowley.session.cli import SessionIPC
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import PixelDecoder, Sample
from crowley.session.driver import (
    DriverError,
    DriverProfile,
    WindowIdentity,
    X11Backend,
)
from crowley.session.executor import KeyRole, PointerButton
from crowley.session.input_broker import ButtonEmissionClaim, InputBroker
from crowley.session.input_events import EventClass, XI2Event
from crowley.session.journal import DEFAULT_STATE, Journal
from crowley.session.model import LunaAdapter, ModelRouteObservation
from crowley.session.motion import (
    MAX_FRAME_AGE,
    MOTION_POST_RELEASE_RESERVE_SECONDS,
    CameraMapping,
    MotionFrame,
    MovementBasis,
    NativeMotionProvider,
)
from crowley.session.pointer_execution import PointerExecutionError
from crowley.session.protocol import Frame, Snapshot

from .test_session_input_tool import fixture_input_tool
from .test_session_native_regressions import _evidence
from .wow_harness import WowClient


class _Identity:
    fingerprint = "client-motion"


class _FixtureRateGate:
    """Test-only one-second broker gate with the production permit surface."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self, *, deadline_monotonic: float | None, cancellation_check) -> _FixtureRatePermit:
        while True:
            if cancellation_check is not None:
                cancellation_check()
            now = time.monotonic()
            if deadline_monotonic is not None and now >= deadline_monotonic:
                raise PointerExecutionError("fixture click rate deadline expired")
            with self._lock:
                if now >= self._next_allowed:
                    return _FixtureRatePermit(self, now)
                wait = self._next_allowed - now
            time.sleep(min(0.01, wait))


class _FixtureRatePermit:
    def __init__(self, gate: _FixtureRateGate, acquired: float) -> None:
        self._gate = gate
        self._acquired = acquired
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed

    @property
    def acquired_monotonic(self) -> float:
        return self._acquired

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        if self._closed:
            raise PointerExecutionError("fixture click permit is closed")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise PointerExecutionError("fixture click deadline expired before precommit")
        return time.monotonic_ns()

    def record_press(self) -> int:
        if self._closed:
            raise PointerExecutionError("fixture click permit is closed")
        with self._gate._lock:
            self._gate._next_allowed = time.monotonic() + 1.0
        return time.monotonic_ns()

    def close(self) -> None:
        self._closed = True


def _executor_none() -> dict:
    return {
        "availability": "none",
        "queueId": "",
        "house": "",
        "character": "",
        "publishedArmed": False,
        "sessionArmed": False,
        "buttonVisible": False,
        "buttonName": "",
        "currentSeq": 0,
        "currentKind": "",
        "phase": "none",
        "inFlight": False,
        "clickReady": False,
        "workDone": 0,
        "workTotal": 0,
        "blocked": False,
        "latched": False,
        "ambiguous": False,
        "clickOrdinal": 0,
        "lastClickSource": "none",
        "resultCount": 0,
        "lastResult": {"seq": 0, "kind": "", "status": "none"},
    }


class _MotionMonitor:
    """A live XI2 boundary whose records are fed by the fake process edge."""

    alive = True
    driver: DesktopDriver | None = None

    def wait_for_event(self, predicate: object, *, timeout: float) -> None:
        del timeout
        if not callable(predicate) or not predicate(()):
            action = getattr(getattr(self.driver, "guardian", None), "action", None)
            reason = getattr(getattr(self.driver, "guardian", None), "revoked_reason", "")
            raise AssertionError(f"production Guardian action did not complete: {action!r} {reason!r}")

    def stop(self) -> str:
        self.alive = False
        return ""


def _fixture_input_tool(root: Path):
    """Build a sealed no-input ELF that stays alive until its owner kills it.

    The production command still supplies the complete typed xdotool argv;
    this image intentionally ignores it.  Keeping the child alive lets the
    installed admission/Guardian path observe a real registered process
    group while the external XI2 edge publishes the simulated events.
    """

    source = root.parent / "motion-input-tool.c"
    executable = root.parent / "motion-input-tool"
    source.write_text(
        "#include <unistd.h>\nint main(void) { for (;;) pause(); }\n",
        encoding="ascii",
    )
    subprocess.run(
        ["cc", "-O2", str(source), "-o", str(executable)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return fixture_input_tool(root, executable_source=str(executable))


def _fixture_codex(root: Path) -> Path:
    """Provide an explicit version-only model boundary for daemon fixtures."""

    executable = root / "codex-version"
    executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.162.1\\n'\n", encoding="ascii")
    executable.chmod(0o755)
    return executable


class _IPCBackend(X11Backend):
    """X11 backend with only external desktop/XI2 boundaries replaced."""

    def __init__(self, root: Path, identity: WindowIdentity) -> None:
        # Integration tests must carry their own sealed image.  Passing this
        # explicit fixture keeps construction independent from the host's
        # PRIVATE_TOOL_ROOT publication (the hermetic runner deliberately
        # points that variable at a missing directory).
        input_tool = _fixture_input_tool(root / "input-tool")
        super().__init__(
            DriverProfile(
                display=":test",
                xauthority=str(root / "Xauthority"),
                wine_prefix=str(root / "wine"),
                client_width=1920,
                client_height=1080,
                outer_width=1920,
                outer_height=1080,
                canvas_crop=(0, 0, 256, 256),
                model_crop=(0, 0, 640, 480),
            ),
            input_tool=input_tool,
        )
        self.identity = identity
        self.driver: DesktopDriver | None = None
        self.evidence: InputCalibrationEvidence | None = None
        self.argv: list[tuple[str, ...]] = []
        self._timestamp = 100
        self.pointer = (50, 50)
        self.motion_position = (10.0, 20.0)
        self.motion_facing = 0.0
        # The external process edge publishes deliberately asymmetric role
        # speeds.  Calibration must measure each binding through the real
        # DesktopDriver/Guardian path instead of inferring them from forward.
        self.motion_role_speeds = {"w": 7.0, "s": 6.0, "a": 5.0, "d": 4.0}
        self.mirror_strafe = False
        self.motion_speed = 0.0
        self.motion_speed_after_input: float | None = None
        self.motion_input_count = 0
        self.physical_input_count = 0
        self.motion_speed_after_input_number: int | None = None
        self.motion_chat_focused = False
        self.camera_yaw_per_pixel = 0.1
        self.camera_response_scale = 1.0
        self.cancelled = threading.Event()
        self.process_started = threading.Event()
        self.process_pid: int | None = None
        self.hold_process = False
        self.block_process = threading.Event()
        self.capture_polls = 0
        self.no_change_polls = 0
        self.capture_publications = 0
        self.publication_times: list[float] = []
        self.capture_delay_once = 0.0

    def resolve_identity(
        self,
        *,
        require_foreground: bool = True,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        del require_foreground, deadline_monotonic
        return self.identity

    def verify_identity(
        self, identity: WindowIdentity, *, deadline_monotonic: float | None = None
    ) -> WindowIdentity:
        del deadline_monotonic
        if identity.fingerprint != self.identity.fingerprint:
            raise DriverError("fixture identity changed")
        return self.identity

    def input_hierarchy(self, *, deadline_monotonic: float | None = None):
        del deadline_monotonic
        assert self.evidence is not None
        return self.evidence.xinput_hierarchy

    def observe_keymap(self, *, deadline_monotonic: float | None = None) -> dict[str, int]:
        del deadline_monotonic
        assert self.evidence is not None
        return dict(self.evidence.keyboard_keymap)

    def observe_keymap_levels(self, *, deadline_monotonic: float | None = None):
        del deadline_monotonic
        assert self.evidence is not None
        return {detail: (keysym,) for keysym, detail in self.evidence.keyboard_keymap}

    def pointer_position(
        self, *, generation: int | None = None, deadline_monotonic: float | None = None
    ) -> tuple[int, int, int]:
        del generation, deadline_monotonic
        return self.pointer[0], self.pointer[1], self.identity.child_xid

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        del deadline_monotonic
        if identity is None or identity.fingerprint != self.identity.fingerprint:
            raise DriverError("fixture capture identity is stale")
        if self.capture_delay_once:
            delay = self.capture_delay_once
            self.capture_delay_once = 0.0
            time.sleep(delay)
        Image.new("RGB", (1920, 1080), (22, 22, 22)).save(path, compress_level=0)
        return self.identity

    def _event(
        self,
        event_class: str,
        *,
        source: int,
        master: int,
        detail: int | None,
        raw: bool,
        root_xy: tuple[float, float] | None = None,
        event_xy: tuple[float, float] | None = None,
        valuators: tuple[tuple[int, float, float | None], ...] = (),
        timestamp_ms: int | None = None,
    ) -> SimpleNamespace | XI2Event:
        if timestamp_ms is None:
            self._timestamp += 1
        else:
            self._timestamp = timestamp_ms
        if event_class == "Motion":
            return XI2Event(
                6,
                EventClass.MOTION,
                master,
                source,
                self._timestamp,
                detail,
                None if raw else 0x374,
                None if raw else 0x374,
                None if raw else self.identity.child_xid,
                root_xy,
                event_xy,
                None if raw else (0, 0, 0),
                (),
                None if raw else 0,
                valuators,
            )
        return SimpleNamespace(
            event_class=event_class,
            source_device=source,
            master_device=master,
            target_xid=None if raw else self.identity.child_xid,
            child_xid=None if raw else self.identity.child_xid,
            detail=detail,
            timestamp_ms=self._timestamp,
            modifiers=None if raw else (0, 0, 0),
            modifier_effective=None if raw else 0,
            is_raw=raw,
            root_xy=root_xy,
            event_xy=event_xy,
            root_xid=None if raw else 0x374,
            event_xid=None if raw else 0x374,
            valuators=valuators,
        )

    def _observe(self, event: SimpleNamespace) -> None:
        assert self.driver is not None
        if not self.driver.guardian.observe(event):
            raise AssertionError(
                f"fixture XI2 event was rejected: {event.event_class} {self.driver.guardian.revoked_reason}"
            )

    def _release_command(self, args: list[str]) -> bool:
        """Mock only the emergency external release subprocess boundary."""

        self.argv.append(tuple(args))
        self.cancelled.set()
        return True

    def _run(
        self,
        args: list[str],
        *,
        generation: int | None = None,
        calibration_ticket: object | None = None,
        held_keys=(),
        held_buttons=(),
        input_schema: str | None = None,
        expected_argv: tuple[str, ...] | None = None,
        deadline_monotonic: float | None = None,
        timeout: float = 5.0,
        button_emission_claim: ButtonEmissionClaim | None = None,
        emission_action_generation: int | None = None,
        emission_lease_generation: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run production X11Backend admission with a harmless process edge.

        The real backend validates the typed argv, enters Guardian admission,
        registers the process group, publishes holds, and waits for completion.
        Only the external child and XI2 event delivery are simulated here.
        """

        if input_schema not in {"motion-key", "camera-drag"}:
            return self._run_fixture_external(
                args,
                generation=generation,
                calibration_ticket=calibration_ticket,
                held_keys=held_keys,
                held_buttons=held_buttons,
                input_schema=input_schema,
                expected_argv=expected_argv,
                deadline_monotonic=deadline_monotonic,
                timeout=timeout,
                button_emission_claim=button_emission_claim,
                emission_action_generation=emission_action_generation,
                emission_lease_generation=emission_lease_generation,
            )

        real_popen = driver_module.subprocess.Popen
        backend = self

        class FixtureProcess:
            def __init__(self, command, **kwargs):
                # Keep the production admission wrapper and all of its
                # process ownership arguments.  In particular, the wrapper
                # receives the inherited gate fd, starts a new process group,
                # and is registered by X11Backend before the gate opens.
                self._child = real_popen(command, **kwargs)
                self.pid = self._child.pid
                self.returncode: int | None = None
                self.stdout = None
                self.stderr = None
                self._done = threading.Event()
                self._error = ""
                self._cancelled = False

                def complete() -> None:
                    callback_succeeded = False
                    try:
                        if self._cancelled:
                            return
                        # Popen returns before X11Backend has registered this
                        # process group and before the admission gate opens.
                        # Wait for both production boundaries to complete; the
                        # sealed helper's /proc executable proves the child is
                        # the pinned image rather than the Python wrapper.
                        ready_by = time.monotonic() + 2.0
                        while time.monotonic() < ready_by:
                            with backend._process_lock:
                                registered = self.pid in backend._active_process_groups
                            try:
                                executable = os.readlink(f"/proc/{self.pid}/exe")
                            except OSError:
                                executable = ""
                            if registered and "memfd:crowley-xdotool" in executable:
                                break
                            if self._child.poll() is not None:
                                break
                            time.sleep(0.001)
                        backend.process_pid = self.pid
                        backend._run_fixture_external(
                            args,
                            generation=generation,
                            calibration_ticket=calibration_ticket,
                            held_keys=held_keys,
                            held_buttons=held_buttons,
                            input_schema=input_schema,
                            expected_argv=expected_argv,
                            deadline_monotonic=deadline_monotonic,
                            timeout=timeout,
                            button_emission_claim=button_emission_claim,
                            emission_action_generation=emission_action_generation,
                            emission_lease_generation=emission_lease_generation,
                        )
                        callback_succeeded = True
                    except Exception as exc:  # production _run reports child failure
                        self._error = str(exc)
                    finally:
                        # The event edge has completed (or failed), so close
                        # the harmless no-input executable.  During a held
                        # motion this runs only after Guardian revocation,
                        # while the real child remains live and registered.
                        if self._child.poll() is None:
                            self._child.terminate()
                        self._child.wait()
                        self.returncode = 0 if callback_succeeded else 1
                        self._done.set()

                self._thread = threading.Thread(target=complete, name="fixture-input-child", daemon=True)
                self._thread.start()

            def poll(self):
                return self.returncode

            def communicate(self, timeout=None):
                if not self._done.wait(timeout):
                    raise subprocess.TimeoutExpired("fixture-input-child", timeout)
                stdout, stderr = self._child.communicate()
                return stdout or "", self._error or stderr or ""

            def wait(self, timeout=None):
                if not self._done.wait(timeout):
                    raise subprocess.TimeoutExpired("fixture-input-child", timeout)
                return self.returncode

            def terminate(self):
                self._cancelled = True
                backend.cancelled.set()
                if self._child.poll() is None:
                    self._child.terminate()

            def kill(self):
                self._cancelled = True
                backend.cancelled.set()
                if self._child.poll() is None:
                    self._child.kill()

        driver_module.subprocess.Popen = FixtureProcess
        try:
            return super()._run(
                args,
                generation=generation,
                calibration_ticket=calibration_ticket,
                held_keys=held_keys,
                held_buttons=held_buttons,
                input_schema=input_schema,
                expected_argv=expected_argv,
                deadline_monotonic=deadline_monotonic,
                timeout=timeout,
                button_emission_claim=button_emission_claim,
                emission_action_generation=emission_action_generation,
                emission_lease_generation=emission_lease_generation,
            )
        finally:
            driver_module.subprocess.Popen = real_popen

    def _run_fixture_external(
        self,
        args: list[str],
        *,
        generation: int | None = None,
        calibration_ticket: object | None = None,
        held_keys=(),
        held_buttons=(),
        input_schema: str | None = None,
        expected_argv: tuple[str, ...] | None = None,
        deadline_monotonic: float | None = None,
        timeout: float = 5.0,
        button_emission_claim: ButtonEmissionClaim | None = None,
        emission_action_generation: int | None = None,
        emission_lease_generation: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del calibration_ticket, held_keys, held_buttons, timeout
        if expected_argv is not None and tuple(args) != expected_argv:
            raise AssertionError("production driver did not use its sealed argv")
        if input_schema == "single-click":
            if not isinstance(button_emission_claim, ButtonEmissionClaim):
                raise AssertionError("fixture received no broker emission claim")
            button = PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT
            self.input_broker.redeem_button_emission_claim(
                button_emission_claim,
                emitted_chain=tuple(args),
                button=button,
                action_generation=emission_action_generation,
                lease_generation=emission_lease_generation,
            )
        self.argv.append(tuple(args))
        if args == ["xinput", "--list", "--short"]:
            return subprocess.CompletedProcess(
                args,
                0,
                "\n".join(
                    (
                        "Virtual core pointer\tid=2 [master pointer (0)]",
                        "Virtual core keyboard\tid=3 [master keyboard (0)]",
                        "Virtual core XTEST pointer\tid=4 [slave pointer (2)]",
                        "Virtual core XTEST keyboard\tid=5 [slave keyboard (3)]",
                        "xwayland-relative-pointer\tid=7 [slave pointer (2)]",
                        "xwayland-keyboard\tid=9 [slave keyboard (3)]",
                    )
                )
                + "\n",
                "",
            )
        if args == ["xinput", "list", "--long", "4"]:
            return subprocess.CompletedProcess(
                args,
                0,
                """Virtual core XTEST pointer               id=4 [slave  pointer  (2)]
\tReporting 3 classes:
\t\tClass originated from: 4. Type: XIButtonClass
\t\tButtons supported: 10
\t\tButton labels: \"Button Left\" \"Button Middle\" \"Button Right\"
\t\tButton state:
\t\tClass originated from: 4. Type: XIValuatorClass
\t\tDetail for Valuator 0:
\t\t  Label: Rel X
\t\t  Range: -1.000000 - -1.000000
\t\t  Resolution: 0 units/m
\t\t  Mode: relative
\t\tClass originated from: 4. Type: XIValuatorClass
\t\tDetail for Valuator 1:
\t\t  Label: Rel Y
\t\t  Range: -1.000000 - -1.000000
\t\t  Resolution: 0 units/m
\t\t  Mode: relative
""",
                "",
            )
        if args == ["xinput", "list-props", "4"]:
            return subprocess.CompletedProcess(
                args,
                0,
                """Device 'Virtual core XTEST pointer':
\tDevice Enabled (122):\t1
\tCoordinate Transformation Matrix (124):\t1.000000, 0.000000, 0.000000, 0.000000, 1.000000, 0.000000, 0.000000, 0.000000, 1.000000
\tXTEST Device (233):\t1
""",
                "",
            )
        if args == ["xwininfo", "-root", "-stats"]:
            return subprocess.CompletedProcess(
                args,
                0,
                """xwininfo: Window id: 0x374 (the root window) (has no name)

  Absolute upper-left X:  0
  Absolute upper-left Y:  0
  Width: 1920
  Height: 1080
""",
                "",
            )
        if generation is None:
            return subprocess.CompletedProcess(args, 0, "", "")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("fixture process deadline expired")
        if input_schema == "motion-key" and self.hold_process:
            self.process_started.set()
            while self.driver is not None and self.driver.guardian.armed:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise DriverError("fixture process deadline expired")
                time.sleep(0.001)
            self.cancelled.set()
            raise DriverError("fixture process cancelled")
        if self.block_process.wait(0.001):
            self.cancelled.set()
            raise DriverError("fixture process cancelled")
        assert self.driver is not None and self.evidence is not None
        keymap = dict(self.evidence.keyboard_keymap)
        if input_schema == "motion-key":
            keysym = args[2]
            detail = keymap[keysym]
            for event_class, raw in (
                ("RawKeyPress", True),
                ("KeyPress", False),
                ("RawKeyRelease", True),
                ("KeyRelease", False),
            ):
                self._observe(
                    self._event(
                        event_class,
                        source=5,
                        master=3 if raw else 5,
                        detail=detail,
                        raw=raw,
                    )
                )
            # The fixture's process boundary publishes a measured post-key
            # displacement through the subsequent external capture.  The
            # role-specific speed and current native facing are part of this
            # external process simulation; the production binding and event
            # ownership remain real.
            duration = float(args[args.index("sleep") + 1])
            speed = self.motion_role_speeds[keysym]
            if keysym == "w":
                heading = self.motion_facing
            elif keysym == "s":
                heading = self.motion_facing + math.pi
            elif keysym == "a":
                heading = self.motion_facing + (-math.pi / 2 if self.mirror_strafe else math.pi / 2)
            else:
                heading = self.motion_facing - math.pi / 2
            distance = speed * duration
            self.motion_position = (
                self.motion_position[0] + distance * math.cos(heading),
                self.motion_position[1] + distance * math.sin(heading),
            )
            self.motion_input_count += 1
            self.physical_input_count += 1
            if self.motion_speed_after_input is not None and (
                self.motion_speed_after_input_number is None
                or self.motion_input_count == self.motion_speed_after_input_number
            ):
                self.motion_speed = self.motion_speed_after_input
        elif input_schema == "camera-drag":
            # Count drags separately from movement-role probes. The latter is
            # calibrated-role evidence; the former drives native
            # publication-barrier injection across every input type.
            self.physical_input_count += 1
            tokens = args[1:]
            press = tokens.index("mousedown")
            release = len(tokens) - 2

            def moves(start: int, end: int) -> None:
                try:
                    prior = self._last_move(tokens, start)
                except AssertionError:
                    prior = (float(self.pointer[0]), float(self.pointer[1]))
                index = start
                while index < end:
                    if tokens[index] == "mousemove":
                        x, y = float(tokens[index + 1]), float(tokens[index + 2])
                        origin = prior
                        self._observe(
                            self._event(
                                "RawMotion",
                                source=4,
                                master=2,
                                detail=0,
                                raw=True,
                                valuators=((0, x, x), (1, y, y)),
                            )
                        )
                        self._observe(
                            self._event(
                                "Motion",
                                source=4,
                                master=4,
                                detail=0,
                                raw=False,
                                root_xy=origin,
                                event_xy=origin,
                                valuators=((0, x, x), (1, y, y)),
                                timestamp_ms=self._timestamp,
                            )
                        )
                        # The calibrated proof requires the forwarded XI2
                        # echo for every current synthetic pointer sample.
                        self._observe(
                            self._event(
                                "Motion",
                                source=7,
                                master=7,
                                detail=0,
                                raw=False,
                                root_xy=(x, y),
                                event_xy=(x, y),
                            )
                        )
                        prior = (x, y)
                        index += 3
                    else:
                        index += 1

            # The production camera chain positions first, then emits the
            # held drag, and closes with the exact button pair.
            moves(0, press)
            if press + 2 >= len(tokens) or tokens[press : press + 2] != ["mousedown", "3"]:
                raise AssertionError("camera chain has no right-button admission")
            start_xy = self._last_move(tokens, press)
            end_xy = self._last_move(tokens, release)
            self.pointer = (int(end_xy[0]), int(end_xy[1]))
            for event_class, raw in (("RawButtonPress", True), ("ButtonPress", False)):
                self._observe(
                    self._event(
                        event_class,
                        source=4,
                        master=2 if raw else 4,
                        detail=3,
                        raw=raw,
                        root_xy=None if raw else start_xy,
                        event_xy=None if raw else start_xy,
                    )
                )
            moves(press + 2, release)
            start_x = start_xy[0]
            end_x = end_xy[0]
            self.motion_facing = (
                self.motion_facing
                + (end_x - start_x) * self.camera_yaw_per_pixel * self.camera_response_scale
            ) % (2.0 * math.pi)
            for event_class, raw in (("RawButtonRelease", True), ("ButtonRelease", False)):
                self._observe(
                    self._event(
                        event_class,
                        source=4,
                        master=2 if raw else 4,
                        detail=3,
                        raw=raw,
                        root_xy=None if raw else end_xy,
                        event_xy=None if raw else end_xy,
                    )
                )
        return subprocess.CompletedProcess(args, 0, "", "")

    @staticmethod
    def _last_move(tokens: list[str], end: int) -> tuple[float, float]:
        for index in range(end - 3, -1, -1):
            if tokens[index] == "mousemove":
                return float(tokens[index + 1]), float(tokens[index + 2])
        raise AssertionError("camera chain has no measured pointer point")


def _ipc_identity() -> WindowIdentity:
    return WindowIdentity(
        0x600118,
        os.getpid(),
        "/usr/bin/python3",
        "World of Warcraft",
        "wowb.exe",
        0x600119,
        (0, 0, 1920, 1080),
        (0, 0, 1920, 1080),
        0x600119,
        "ipc-process-start",
        ":ipc-test",
        "ipc-game-identity",
        0x600118,
        0x60011A,
        frozenset({0x600119, 0x60011A}),
    )


def _motion_sample(
    identity: WindowIdentity,
    sequence: int,
    *,
    facing: float = 0.0,
    position: tuple[float, float] = (10.0, 20.0),
    boot: str = "ipc-boot",
    capture_monotonic: float | None = None,
    speed: float = 0.0,
    chat_focused: bool = False,
) -> Sample:
    snapshot = {
        "schema": 6,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": _executor_none(),
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": False,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
        "chatEdit": {"active": chat_focused, "focused": chat_focused},
        "motion": {
            "position": {"x": position[0], "y": position[1], "z": 3.0, "instance": 4},
            "facing": facing,
            "speed": speed,
            "combat": False,
            "deadOrGhost": False,
            "bindings": {
                "moveForward": "W",
                "moveBackward": "S",
                "strafeLeft": "A",
                "strafeRight": "D",
                "turnLeft": "Q",
                "jump": "SPACE",
                "turnRight": "E",
            },
            "native": {
                "ctm": {
                    "accessor": "C_CVar.GetCVarBool",
                    "cvar": "autointeract",
                    "state": "disabled",
                    "reason": "",
                },
                "ground": {
                    "state": "grounded",
                    "falling": "false",
                    "flying": "false",
                    "swimming": "false",
                    "submerged": "false",
                    "mounted": "false",
                    "taxi": "false",
                    "reason": "",
                },
                "speedTuple": {
                    "current": speed,
                    "run": 0,
                    "flight": 0,
                    "swim": 0,
                    "unitStatus": "unverified",
                },
            },
        },
        "context": {"zone": "Ironforge", "subzone": "The Commons"},
    }
    return Sample(
        "FRESH",
        SimpleNamespace(
            boot=boot,
            sequence=sequence,
            snapshot=SimpleNamespace(value=snapshot),
        ),
        time.monotonic() if capture_monotonic is None else capture_monotonic,
        (0, 0, 256, 256),
        sequence,
        identity.fingerprint,
    )


def _ipc_motion_fixture(
    tmp_path: Path,
    *,
    camera: bool = False,
    preinstall_basis: bool = True,
    preinstall_camera_mapping: bool = True,
):
    identity = _ipc_identity()
    backend = _IPCBackend(tmp_path, identity)
    evidence = _evidence(
        tmp_path,
        xauthority=backend.xauthority,
        game_identity_fingerprint=identity.fingerprint,
        game_geometry=identity.child_geometry,
    )
    # The shared native regression fixture predates the movement binding
    # contract and seals ``e`` at the keycode used by the integration trace.
    # This fixture owns the complete external keymap/event edge, so issue a
    # local sealed copy whose level-zero symbol is the actual lower-case
    # movement binding ``w``.  Re-sealing the evidence keeps the production
    # driver's identity and keyboard proof checks meaningful.
    local_keymap = {("w" if keysym == "e" else keysym): detail for keysym, detail in evidence.keyboard_keymap}
    # The imported probe only needed the calibration command alphabet.  This
    # fixture owns the external keymap edge for all four native movement
    # roles, so add the lower-case S level-zero entry used by backward input.
    local_keymap.setdefault("s", 39)
    keyboard_keymap = tuple(sorted(local_keymap.items()))
    keyboard_profile = tuple(
        (
            operation,
            payload,
            tuple(
                (
                    phase,
                    "w" if keysym == "e" else keysym,
                    detail,
                    modifiers,
                    effective,
                )
                for phase, keysym, detail, modifiers, effective in steps
            ),
        )
        for operation, payload, steps in evidence.keyboard_profile
    )
    evidence = replace(evidence, keyboard_keymap=keyboard_keymap, keyboard_profile=keyboard_profile)
    driver = DesktopDriver(
        profile=backend.profile,
        backend=backend,
        lease_path=tmp_path / "desktop-lease.lock",
    )
    driver.input_broker = InputBroker.for_tests(
        clock=time.monotonic,
        permit_factory=_FixtureRateGate().acquire,
    )
    backend.input_broker = driver.input_broker
    evidence = replace(evidence, driver_version=driver.driver_version)
    evidence = replace(evidence, fingerprint=evidence.content_fingerprint()).with_hash()
    backend.evidence = evidence
    backend.driver = driver
    monitor = _MotionMonitor()
    monitor.driver = driver
    driver._monitor = monitor  # type: ignore[attr-defined]

    journal = Journal(tmp_path / "session.json")
    journal.write(DEFAULT_STATE)
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    config = SessionConfig(
        profile=backend.profile,
        journal=tmp_path / "session.json",
        socket=tmp_path / "session.sock",
        capture_dir=tmp_path / "captures",
        plan_hash="ipc-test-plan",
        plan_json="{}",
        codex_path=str(_fixture_codex(tmp_path)),
    )
    daemon = SessionDaemon(config, controller=controller, driver=driver)

    phase = {
        "camera": camera,
        "sequence": 0,
        "polls": 0,
        "next_publication": None,
    }

    def capture(*, deadline: float | None = None) -> Sample:
        del deadline
        phase["polls"] += 1
        backend.capture_polls += 1
        if backend.capture_delay_once:
            delay = backend.capture_delay_once
            backend.capture_delay_once = 0.0
            time.sleep(delay)
        # Capture polls are frequent, while the addon publishes at one Hz.
        # The schedule is real monotonic time: every intervening poll must
        # observe NO_CHANGE, so operation deadlines and Guardian clocks retain
        # their production meaning in the installed path.
        now = time.monotonic()
        if phase["next_publication"] is None:
            phase["next_publication"] = now
        if now < phase["next_publication"]:
            backend.no_change_polls += 1
            return Sample("NO_CHANGE", None, now, (0, 0, 256, 256), None, identity.fingerprint)
        phase["sequence"] += 1
        backend.capture_publications += 1
        backend.publication_times.append(now)
        phase["next_publication"] = now + 1.0
        return _motion_sample(
            identity,
            phase["sequence"],
            facing=backend.motion_facing,
            position=backend.motion_position,
            boot=getattr(backend, "motion_boot", "ipc-boot"),
            speed=backend.motion_speed,
            chat_focused=backend.motion_chat_focused,
            capture_monotonic=now,
        )

    provider = daemon.motion_provider
    provider._capture = lambda: capture()  # type: ignore[attr-defined]
    provider._capture_with_deadline = capture  # type: ignore[attr-defined]
    provider._identity = lambda *, deadline: driver.resolve_identity(  # type: ignore[attr-defined]
        deadline_monotonic=deadline
    )

    class Model:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            assert image
            assert kwargs["target"] == "game-client"
            assert float(kwargs["corridor_distance"]) > 0.0
            return ModelRouteObservation("world", "ipc-route", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            assert image
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = Model()

    controller._native_calibrated = True
    started = controller.start(SessionMode.INTERACTION)
    generation = started.generation
    assert started.state is ControllerState.INPUT_CALIBRATION
    controller.input_calibrated(
        evidence=evidence,
        expected_generation=generation,
        expected_state=ControllerState.INPUT_CALIBRATION,
    )
    reconciled = controller.reconcile_interaction(
        identity_ok=True,
        world_ready=True,
        expected_generation=generation,
    )
    assert reconciled.state is ControllerState.READY
    ipc = SessionIPC(controller, daemon)
    started_response = ipc.dispatch({"command": "interaction_start", "generation": generation})
    assert started_response["ok"] is True
    assert started_response["status"]["mode"] == SessionMode.INTERACTION.value

    if preinstall_basis:
        backend.motion_role_speeds = {"w": 4.0, "s": 4.0, "a": 4.0, "d": 4.0}
        daemon.movement_basis = MovementBasis(
            identity_fingerprint=identity.fingerprint,
            boot="ipc-boot",
            generation=generation,
            capture_id="movement:ipc",
            before_sequence=1,
            after_sequence=2,
            forward_heading_offset=0.0,
            forward_displacement=0.4,
            pulse_seconds=0.1,
            strafe_heading_offset=math.pi / 2,
            strafe_displacement=0.4,
            facing_sign=1,
            handedness=1,
        )
    else:
        daemon.movement_basis = None
    if preinstall_camera_mapping:
        daemon.camera_mapping = CameraMapping(
            identity_fingerprint=identity.fingerprint,
            generation=generation,
            capture_id="camera:ipc",
            yaw_per_pixel=0.1,
            boot="ipc-boot",
        )
    return daemon, controller, driver, backend, ipc, generation


def _actual_native_ipc_fixture(
    tmp_path: Path,
    *,
    ctm_mode: str = "false",
    ctm_missing: bool = False,
    ground_modes: dict[str, str] | None = None,
):
    """Use the real Lua Agent -> pixel decoder -> daemon provider chain."""

    daemon, controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    client = WowClient(
        ctm_mode=ctm_mode,
        ctm_missing=ctm_missing,
        ground_modes=ground_modes
        or {
            "flying": "false",
            "falling": "false",
            "swimming": "false",
            "submerged": "false",
            "mounted": "false",
            "taxi": "false",
        },
    )
    client.set_canonical_plan()
    client.login()
    client.set_motion(
        position=(backend.motion_position[0], backend.motion_position[1], 3.0, 4),
        facing=backend.motion_facing,
    )
    client.publish_agent()
    native_boot = client.snapshot_agent()["boot"]
    if daemon.movement_basis is not None:
        daemon.movement_basis = replace(daemon.movement_basis, boot=native_boot)
    if daemon.camera_mapping is not None:
        daemon.camera_mapping = replace(daemon.camera_mapping, boot=native_boot)
    decoder = PixelDecoder(crop=(0, 0, 256, 256))
    state = {
        "last_input_count": 0,
        "post_publications": 0,
        "post_failure": None,
        "post_ground_failure": None,
    }

    def capture(*, deadline: float | None = None) -> Sample:
        del deadline
        if backend.physical_input_count > state["last_input_count"]:
            state["last_input_count"] = backend.physical_input_count
            state["post_publications"] = 0
        if state["post_failure"] is not None and backend.physical_input_count:
            state["post_publications"] += 1
            phase = state["post_failure"]
            if phase == "barrier" and state["post_publications"] == 1:
                client.state.ctmMode = "true"
            elif phase == "barrier" and state["post_publications"] == 2:
                client.state.ctmMode = "false"
            elif phase == "final" and state["post_publications"] == 2:
                client.state.ctmMode = "true"
            elif phase == "exception" and state["post_publications"] == 1:
                raise DriverError("synthetic native successor capture failure")
        if state["post_ground_failure"] is not None and backend.physical_input_count:
            state["post_publications"] += 1
            if state["post_publications"] == 1:
                client.state.groundModes[state["post_ground_failure"]] = "true"
            elif state["post_publications"] == 2:
                client.state.groundModes[state["post_ground_failure"]] = "false"
        client.set_motion(
            position=(backend.motion_position[0], backend.motion_position[1], 3.0, 4),
            facing=backend.motion_facing,
            speed=backend.motion_speed,
        )
        client.publish_agent()
        now = time.monotonic()
        return decoder.decode(
            client.render_agent_capture(), monotonic=now, window_identity=backend.identity.fingerprint
        )

    provider = daemon.motion_provider
    provider._capture = lambda: capture()  # type: ignore[attr-defined]
    provider._capture_with_deadline = capture  # type: ignore[attr-defined]
    provider._identity = lambda *, deadline: driver.resolve_identity(  # type: ignore[attr-defined]
        deadline_monotonic=deadline
    )
    daemon._test_native_client = client
    daemon._test_native_capture_state = state
    return daemon, controller, driver, backend, ipc, generation


def test_actual_native_pipeline_admits_successful_public_motion_key(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is True, response
        assert backend.motion_input_count == 1
        assert any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("ctm_mode", "ctm_missing", "expected_reason"),
    (
        ("true", False, "enabled"),
        ("error", False, "call_error"),
        ("nil", False, "nil_result"),
        ("wrong_type", False, "wrong_type"),
        ("false", True, "missing"),
    ),
)
def test_actual_native_pipeline_rejects_every_ctm_capability_class(
    tmp_path: Path,
    ctm_mode: str,
    ctm_missing: bool,
    expected_reason: str,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(
        tmp_path, ctm_mode=ctm_mode, ctm_missing=ctm_missing
    )
    try:
        observed = daemon.motion_provider.observe(generation, deadline=time.monotonic() + 5.0)
        assert observed.native.ctm.reason == ("" if expected_reason == "enabled" else expected_reason)
        assert observed.native.ctm.state == ("enabled" if expected_reason == "enabled" else "unknown")
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert backend.motion_input_count == 0
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("field", "mode"),
    [
        (field, mode)
        for field in ("flying", "falling", "swimming", "submerged", "mounted", "taxi")
        for mode in ("true", "error", "nil", "wrong_type", "missing")
    ],
)
def test_actual_native_pipeline_rejects_every_ground_capability_class(
    tmp_path: Path, field: str, mode: str
) -> None:
    fields = ("flying", "falling", "swimming", "submerged", "mounted", "taxi")
    ground_modes = {name: "false" for name in fields}
    ground_modes[field] = mode
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(
        tmp_path, ground_modes=ground_modes
    )
    try:
        observed = daemon.motion_provider.observe(generation, deadline=time.monotonic() + 5.0)
        expected_reason = {
            "true": "",
            "error": "call_error",
            "nil": "nil_result",
            "wrong_type": "wrong_type",
            "missing": "missing",
        }[mode]
        assert observed.native.ground.reason == ("" if mode == "true" else expected_reason)
        assert getattr(observed.native.ground, field) == ("true" if mode == "true" else "unknown")
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert backend.motion_input_count == 0
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    (
        (
            {"command": "motion_key", "role": KeyRole.MOVE_FORWARD.value, "duration": 0.01},
            "executable minimum",
        ),
        (
            {"command": "motion_camera_calibrate", "deltaPixels": 16},
            "sub-pi envelope",
        ),
        (
            {"command": "motion_movement_calibrate", "pulseSeconds": 0.001},
            "outside bounds",
        ),
        (
            {"command": "motion_waypoint", "x": "bad", "y": 20.0},
            "finite numbers",
        ),
    ),
)
def test_actual_native_preobservation_rejections_retain_exact_proofs(
    tmp_path: Path, payload: dict[str, object], error_fragment: str
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    camera_mapping = daemon.camera_mapping
    movement_basis = daemon.movement_basis
    assert camera_mapping is not None
    assert movement_basis is not None
    try:
        response = ipc.dispatch(dict(payload, generation=generation))
        assert response["ok"] is False
        assert error_fragment in response["error"]
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is movement_basis
        assert backend.motion_input_count == 0
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("command", "payload"),
    (
        ("motion_key", {"command": "motion_key", "role": KeyRole.MOVE_FORWARD.value, "duration": 0.08}),
        ("motion_camera", {"command": "motion_camera", "deltaPixels": 4}),
        (
            "motion_camera_calibrate",
            {"command": "motion_camera_calibrate", "deltaPixels": 4},
        ),
        (
            "motion_movement_calibrate",
            {
                "command": "motion_movement_calibrate",
                "pulseSeconds": 0.03,
            },
        ),
        (
            "motion_waypoint",
            {"command": "motion_waypoint", "x": 10.4, "y": 20.0},
        ),
    ),
)
def test_actual_native_gate_initial_refuses_every_public_motion_entrypoint(
    tmp_path: Path,
    command: str,
    payload: dict[str, object],
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    try:
        daemon._test_native_client.state.ctmMode = "true"
        payload = dict(payload, generation=generation)
        response = ipc.dispatch(payload)
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_actual_native_gate_precommit_failure_wins_over_pose_drift_and_clears_proofs(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)

    class NativeFailureModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del image, kwargs
            daemon._test_native_client.state.ctmMode = "true"
            backend.motion_position = (backend.motion_position[0] + 1.0, backend.motion_position[1])
            return ModelRouteObservation("world", "precommit-native-failure", True, True)

    daemon.model = NativeFailureModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("command", "payload", "preinstall_basis", "preinstall_camera_mapping"),
    (
        (
            "motion_key",
            {"command": "motion_key", "role": KeyRole.MOVE_FORWARD.value, "duration": 0.08},
            True,
            True,
        ),
        ("motion_camera", {"command": "motion_camera", "deltaPixels": 4}, True, True),
        (
            "motion_camera_calibrate",
            {"command": "motion_camera_calibrate", "deltaPixels": 4},
            True,
            False,
        ),
        (
            "motion_movement_calibrate",
            {"command": "motion_movement_calibrate", "pulseSeconds": 0.03},
            False,
            True,
        ),
        ("motion_waypoint", {"command": "motion_waypoint", "x": 10.4, "y": 20.0}, True, True),
    ),
)
def test_actual_native_gate_precommit_matrix_clears_before_every_public_input(
    tmp_path: Path,
    command: str,
    payload: dict[str, object],
    preinstall_basis: bool,
    preinstall_camera_mapping: bool,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    if not preinstall_basis:
        daemon.movement_basis = None
    if not preinstall_camera_mapping:
        daemon.camera_mapping = None

    class NativeFailureModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def _fail(self) -> None:
            daemon._test_native_client.state.ctmMode = "true"
            backend.motion_position = (backend.motion_position[0] + 1.0, backend.motion_position[1])

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del image, kwargs
            self._fail()
            return ModelRouteObservation("world", "precommit-native-matrix", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del image, kwargs
            self._fail()
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = NativeFailureModel()
    try:
        response = ipc.dispatch(dict(payload, generation=generation))
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_actual_native_gate_clock_setup_failure_clears_and_releases_lock(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    calls = 0

    def raising_clock() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("clock setup regression")
        return time.monotonic()

    daemon._native_clock = raising_clock
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "clock setup regression" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
        # A different thread must be able to acquire the actual RLock.  A
        # same-thread reacquire would be permitted even if the lock leaked.
        daemon._native_clock = time.monotonic
        acquired = threading.Event()

        def reacquire_from_other_thread() -> None:
            lock = daemon._motion_intent_lock
            if lock.acquire(timeout=1.0):
                acquired.set()
                lock.release()

        lock_thread = threading.Thread(target=reacquire_from_other_thread)
        lock_thread.start()
        lock_thread.join(timeout=2.0)
        assert not lock_thread.is_alive()
        assert acquired.is_set()
    finally:
        daemon._native_clock = time.monotonic
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize("failure", ("lease", "generation", "deadline"))
def test_actual_native_gate_mid_operation_failure_clears_retained_proofs(
    tmp_path: Path, failure: str
) -> None:
    daemon, controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)

    class LeaseRevokingModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del kwargs
            assert image
            if failure == "lease":
                driver.guardian.revoke("mid-operation successor regression")
            elif failure == "generation":
                controller.stop()
            else:
                daemon._motion_deadline_local.deadline = time.monotonic() - 1.0
            return ModelRouteObservation("world", "lease-revoked-route", True, True)

    daemon.model = LeaseRevokingModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert (
            "lease" in response["error"]
            or "interaction" in response["error"]
            or "deadline" in response["error"]
        )
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def _assert_actual_allowed_successor_cannot_revive_motion_proofs(
    daemon: SessionDaemon,
    client: WowClient,
    backend: _IPCBackend,
    ipc: SessionIPC,
    generation: int,
) -> None:
    """Publish and observe a real allowed frame, then prove no input revives proofs."""

    daemon._test_native_capture_state["post_failure"] = None
    daemon._test_native_capture_state["post_ground_failure"] = None
    client.state.ctmMode = "false"
    for field in ("flying", "falling", "swimming", "submerged", "mounted", "taxi"):
        client.state.groundModes[field] = "false"
    client.publish_agent()
    successor = daemon.motion_provider.observe(generation, deadline=time.monotonic() + 5.0)
    successor.require_native_gate()
    before_argv = len(backend.argv)
    response = ipc.dispatch(
        {
            "command": "motion_key",
            "generation": generation,
            "role": KeyRole.MOVE_FORWARD.value,
            "duration": 0.08,
        }
    )
    assert response["ok"] is False
    assert "movement basis" in response["error"] or "camera mapping" in response["error"]
    assert len(backend.argv) == before_argv
    assert daemon.camera_mapping is None
    assert daemon.movement_basis is None


@pytest.mark.parametrize("post_failure", ("barrier", "final", "exception"))
def test_actual_native_gate_postrelease_failure_clears_proofs_after_observed_release(
    tmp_path: Path,
    post_failure: str,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    daemon._test_native_capture_state["post_failure"] = post_failure
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "capability" in response["error"] or "successor" in response["error"]
        assert backend.motion_input_count == 1
        assert any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        _assert_actual_allowed_successor_cannot_revive_motion_proofs(
            daemon, daemon._test_native_client, backend, ipc, generation
        )
    finally:
        daemon.request_shutdown()
        driver.close()


def test_actual_native_ground_barrier_failure_cannot_be_hidden_by_allowed_successor(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    daemon._test_native_capture_state["post_ground_failure"] = "falling"
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "capability" in response["error"]
        assert backend.motion_input_count == 1
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        _assert_actual_allowed_successor_cannot_revive_motion_proofs(
            daemon, daemon._test_native_client, backend, ipc, generation
        )
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    ("command", "payload", "preinstall_basis", "preinstall_camera_mapping"),
    (
        (
            "motion_key",
            {"command": "motion_key", "role": KeyRole.MOVE_FORWARD.value, "duration": 0.08},
            True,
            True,
        ),
        ("motion_camera", {"command": "motion_camera", "deltaPixels": 4}, True, True),
        (
            "motion_camera_calibrate",
            {"command": "motion_camera_calibrate", "deltaPixels": 4},
            True,
            False,
        ),
        (
            "motion_movement_calibrate",
            {"command": "motion_movement_calibrate", "pulseSeconds": 0.03},
            False,
            True,
        ),
        ("motion_waypoint", {"command": "motion_waypoint", "x": 10.4, "y": 20.0}, True, True),
    ),
)
def test_actual_native_gate_postrelease_matrix_observes_release_before_failure(
    tmp_path: Path,
    command: str,
    payload: dict[str, object],
    preinstall_basis: bool,
    preinstall_camera_mapping: bool,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _actual_native_ipc_fixture(tmp_path)
    if not preinstall_basis:
        daemon.movement_basis = None
    if not preinstall_camera_mapping:
        daemon.camera_mapping = None
    daemon._test_native_capture_state["post_failure"] = "barrier"
    try:
        response = ipc.dispatch(dict(payload, generation=generation))
        assert response["ok"] is False
        assert "capability" in response["error"] or "successor" in response["error"]
        assert backend.physical_input_count >= 1
        assert any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        _assert_actual_allowed_successor_cannot_revive_motion_proofs(
            daemon, daemon._test_native_client, backend, ipc, generation
        )
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_motion_key_reaches_real_driver_lease_and_guardian(tmp_path: Path) -> None:
    daemon, controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is True, response
        assert backend.argv
        assert backend.argv[-1][0:3] == ("xdotool", "keydown", "w")
        assert backend.argv[-1][-2:] == ("keyup", "w")
        assert driver.guardian.armed
        assert controller.status().lease_generation == driver.guardian.generation
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_chat_focus_on_confirmed_dispatch_frame_before_input(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)

    class FocusedConfirmationModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del kwargs
            assert image
            # The route screenshot is unfocused. The external native
            # publication changes focus before the final same-pose confirm.
            backend.motion_chat_focused = True
            return ModelRouteObservation("world", "focused-confirmation", True, True)

    daemon.model = FocusedConfirmationModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "chat" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_exhausted_feedback_reserve_rejects_before_input(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    camera_mapping = daemon.camera_mapping
    movement_basis = daemon.movement_basis
    assert camera_mapping is not None
    assert movement_basis is not None
    try:
        # Reuse the production deadline local to model an operation that has
        # only the post-release publication reserve left.  The route/model
        # path must reject before DesktopDriver/Guardian can inject a key.
        daemon._motion_deadline_local.deadline = time.monotonic() + MOTION_POST_RELEASE_RESERVE_SECONDS + 0.01
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert response["error"] == "motion pulse cannot fit the bounded intent deadline"
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is movement_basis
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        del daemon._motion_deadline_local.deadline
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_reserve_refusal_retains_exact_proofs(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)
    backend.pointer = (50, 50)
    camera_mapping = daemon.camera_mapping
    movement_basis = daemon.movement_basis
    assert camera_mapping is not None
    assert movement_basis is not None
    try:
        daemon._motion_deadline_local.deadline = time.monotonic() + MOTION_POST_RELEASE_RESERVE_SECONDS + 0.6
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 4})
        assert response["ok"] is False
        assert response["error"] == "camera drag cannot fit the bounded intent deadline"
        assert backend.capture_publications == 2
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is movement_basis
        assert not any("mousedown" in argv for argv in backend.argv)
    finally:
        del daemon._motion_deadline_local.deadline
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_waypoint_reserve_refusal_retains_exact_proofs(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    camera_mapping = daemon.camera_mapping
    movement_basis = daemon.movement_basis
    assert camera_mapping is not None
    assert movement_basis is not None
    try:
        daemon._motion_deadline_local.deadline = time.monotonic() + MOTION_POST_RELEASE_RESERVE_SECONDS + 0.6
        start_x, start_y = backend.motion_position
        response = ipc.dispatch(
            {
                "command": "motion_waypoint",
                "generation": generation,
                "x": start_x + 0.5,
                "y": start_y,
            }
        )
        assert response["ok"] is False
        assert response["error"] == "motion pulse cannot fit the bounded intent deadline"
        assert backend.capture_publications == 2
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is movement_basis
        assert not any(argv[:2] == ("xdotool", "keydown") or "mousedown" in argv for argv in backend.argv)
    finally:
        del daemon._motion_deadline_local.deadline
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rechecks_original_route_age_after_pose_confirmation(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)

    class DelayedCaptureModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del kwargs
            assert image
            # Keep the original route screenshot while an external model
            # delay lets its freshness window expire.  The confirmation
            # capture itself remains immediate, so this exercises the
            # final original-route age check rather than capture-start
            # staleness in the confirmation provider.
            time.sleep(MAX_FRAME_AGE + 0.25)
            return ModelRouteObservation("world", "delayed-route", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = DelayedCaptureModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        # The original route proof is rejected once the delayed model turn
        # finishes; no physical input is admitted from stale evidence.
        assert "corridor was not positively proved" in response["error"]
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_stormwind_reopen_uses_fresh_native_telemetry_without_model_observation() -> None:
    """The ordinary Auction House route proves its outcome from native telemetry."""

    identity = SimpleNamespace(fingerprint="runtime-client")
    status = SimpleNamespace(
        state=ControllerState.READY,
        stop_requested=False,
        mode=SessionMode.REPEAT,
        generation=7,
        action_intent="",
        lease_generation=11,
    )

    class _Controller:
        lock = threading.RLock()

        def status(self):
            return status

        def set_action_intent(self, name: str, *, deadline_seconds: int) -> None:
            del deadline_seconds
            status.action_intent = name

        def action_result(self, result: str) -> None:
            del result

        def clear_action_intent(self, result: str) -> None:
            del result
            status.action_intent = ""

    class _Guardian:
        armed = True
        generation = 11

        def check(self, generation: int) -> None:
            assert generation == self.generation

    class _Driver:
        backend = object.__new__(X11Backend)
        guardian = _Guardian()

        def resolve_identity(self, *, require_foreground=False, deadline_monotonic=None):
            del deadline_monotonic
            if require_foreground is not False:
                assert require_foreground is True
            return identity

        def verify_named_action(self, *_args, **_kwargs):
            raise AssertionError("ordinary Auction House reopening must not query named-state vision")

        def observe_named_state(self, *_args, **_kwargs):
            raise AssertionError("ordinary Auction House reopening must not invoke Luna")

        def run(self, name: str, generation: int, **kwargs: object) -> None:
            assert name == "open_ah"
            assert generation == self.guardian.generation
            assert kwargs["precondition"](identity)
            assert kwargs["postcondition"](identity)

    def snapshot(*, ah_open: bool) -> dict[str, object]:
        return {
            "context": {"character": "Arthur", "realm": "Beta", "zone": "Stormwind City"},
            "render": {"nativePhase": "normal", "nativeOrdinal": 0},
            "ahOpen": ah_open,
            "blockingUI": {
                "staticPopupAvailable": True,
                "staticPopupShown": False,
                "gameMenuAvailable": True,
                "gameMenuShown": False,
            },
            "chatEdit": {"active": False, "focused": False, "text": ""},
            "auctioneer": {
                "targetPresent": True,
                "targetName": "Auctioneer Jaxon",
                "rangeAvailable": True,
                "withinTradeRange": True,
                "bindingAvailable": True,
                "ctrlF12Action": "INTERACTTARGET",
                "interactionMode": "any",
            },
        }

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.controller = _Controller()
    daemon.driver = _Driver()
    daemon.config = SimpleNamespace(
        profile=SimpleNamespace(character="Arthur", realm="Beta"), reload_deadline_seconds=20
    )
    daemon._native_clock = time.monotonic
    statuses = [snapshot(ah_open=False), snapshot(ah_open=False), snapshot(ah_open=True)]
    frames = [
        Frame(Snapshot(value), "boot", sequence, 1000 + sequence, sequence, sequence, 1)
        for sequence, value in enumerate(statuses, 1)
    ]
    samples = [
        Sample("FRESH", frame, float(frame.sequence), (0, 0, 1, 1), frame.crc32, identity.fingerprint)
        for frame in frames
    ]
    daemon.capture_status = lambda *, deadline: samples.pop(0)

    daemon._run_action("open_ah", {"ahOpen": False})
    assert not samples
    assert status.action_intent == ""


def test_session_ipc_movement_calibrate_uses_installed_feedback_and_all_bindings(tmp_path: Path) -> None:
    """The IPC request reaches controller lease, real driver, Guardian, and feedback."""

    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path, preinstall_basis=False
    )
    camera_mapping = daemon.camera_mapping
    assert camera_mapping is not None
    try:
        started_at = time.monotonic()
        response = ipc.dispatch(
            {
                "command": "motion_movement_calibrate",
                "generation": generation,
                "pulseSeconds": 0.03,
            }
        )
        assert response["ok"] is True, response
        elapsed = time.monotonic() - started_at
        movement = response["movement"]
        assert movement["forwardDisplacement"] == pytest.approx(0.21)
        assert movement["backwardDisplacement"] == pytest.approx(0.18)
        assert movement["strafeDisplacement"] == pytest.approx(0.15)
        assert movement["strafeRightDisplacement"] == pytest.approx(0.12)
        assert movement["handedness"] in {-1, 1}
        assert backend.capture_publications > 0
        assert backend.no_change_polls >= backend.capture_publications * 2
        assert backend.capture_polls == backend.capture_publications + backend.no_change_polls
        assert elapsed >= 4.0
        assert all(
            later - earlier >= 0.8
            for earlier, later in zip(backend.publication_times, backend.publication_times[1:], strict=False)
        )
        keydowns = [argv[2] for argv in backend.argv if argv[:2] == ("xdotool", "keydown")]
        assert keydowns == ["w", "w", "a", "s", "d"]
        assert backend.motion_role_speeds == {"w": 7.0, "s": 6.0, "a": 5.0, "d": 4.0}
        assert backend.process_started.is_set() is False
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_movement_calibrate_refuses_mirrored_strafe_before_basis_seal(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path, preinstall_basis=False
    )
    backend.mirror_strafe = True
    try:
        response = ipc.dispatch(
            {
                "command": "motion_movement_calibrate",
                "generation": generation,
                "pulseSeconds": 0.03,
            }
        )
        assert response["ok"] is False
        assert "handed" in response["error"]
        assert daemon.movement_basis is None
        keydowns = [argv[2] for argv in backend.argv if argv[:2] == ("xdotool", "keydown")]
        assert keydowns == ["w", "w", "a"]
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_speed_change_on_confirmed_calibration_probe(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path, preinstall_basis=False
    )
    calls = 0

    class MovingConfirmationModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            nonlocal calls
            del kwargs
            assert image
            calls += 1
            # The external capture edge reports movement before the probe's
            # final same-pose dispatch confirmation.
            backend.motion_speed = 1.0
            return ModelRouteObservation("world", f"moving-{calls}", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = MovingConfirmationModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_movement_calibrate",
                "generation": generation,
                "pulseSeconds": 0.03,
            }
        )
        assert response["ok"] is False
        assert "stationary" in response["error"]
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_moving_post_release_calibration_feedback_before_next_role(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path, preinstall_basis=False
    )
    backend.motion_speed_after_input = 1.0
    backend.motion_speed_after_input_number = 4
    try:
        response = ipc.dispatch(
            {
                "command": "motion_movement_calibrate",
                "generation": generation,
                "pulseSeconds": 0.03,
            }
        )
        assert response["ok"] is False
        assert "settled" in response["error"]
        keydowns = [argv[2] for argv in backend.argv if argv[:2] == ("xdotool", "keydown")]
        assert keydowns == ["w", "w", "a", "s"]
        assert daemon.movement_basis is None
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_moving_post_release_pulse_feedback(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    backend.motion_speed_after_input = 1.0
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "settled" in response["error"]
        assert any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_waypoint_uses_the_calibrated_basis_through_real_input(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path, preinstall_basis=False
    )
    camera_mapping = daemon.camera_mapping
    assert camera_mapping is not None
    try:
        too_large = ipc.dispatch(
            {
                "command": "motion_camera_calibrate",
                "generation": generation,
                "deltaPixels": 16,
            }
        )
        assert too_large["ok"] is False
        assert "sub-pi envelope" in str(too_large["error"])
        assert not any("mousedown" in argv for argv in backend.argv)
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is None
        calibrated = ipc.dispatch(
            {
                "command": "motion_movement_calibrate",
                "generation": generation,
                "pulseSeconds": 0.03,
            }
        )
        assert calibrated["ok"] is True, calibrated
        start_x, start_y = backend.motion_position
        response = ipc.dispatch(
            {
                "command": "motion_waypoint",
                "generation": generation,
                "x": start_x + 0.5,
                "y": start_y,
            }
        )
        assert response["ok"] is True, response
        assert response["motion"]["x"] >= start_x + 0.25
        keydowns = [argv[2] for argv in backend.argv if argv[:2] == ("xdotool", "keydown")]
        assert keydowns[:5] == ["w", "w", "a", "s", "d"]
        assert keydowns[-1] == "w"
        assert any("mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_native_reload_after_route_before_physical_input(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)

    class ReloadingModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            assert image
            # The external model call is allowed to expose a real state
            # transition, but the motion chain must refuse input when the
            # successor no longer matches the route frame.
            backend.motion_boot = "ipc-boot-reloaded"
            backend.motion_position = (backend.motion_position[0] + 0.5, backend.motion_position[1])
            return ModelRouteObservation("world", "reload-route", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = ReloadingModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert response["ok"] is False
        assert "boot changed" in str(response["error"]) or "motion changed" in str(response["error"])
        assert not any(argv[:2] == ("xdotool", "keydown") for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_reaches_real_drag_argv_and_guardian(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)
    backend.pointer = (50, 50)
    try:
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 20})
        assert response["ok"] is True, response
        assert backend.argv
        chain = backend.argv[-1]
        assert chain[0] == "xdotool"
        assert "mousedown" in chain
        press = chain.index("mousedown")
        assert chain[press + 1] == "3"
        assert chain[-2:] == ("mouseup", "3")
        assert driver.guardian.armed
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_calibration_uses_confirmed_dispatch_pose_and_reports_identity(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path,
        camera=True,
        preinstall_basis=False,
        preinstall_camera_mapping=False,
    )
    try:
        calibrated = ipc.dispatch(
            {
                "command": "motion_camera_calibrate",
                "generation": generation,
                "deltaPixels": 8,
            }
        )
        assert calibrated["ok"] is True, calibrated
        camera = calibrated["camera"]
        assert camera["generation"] == generation
        assert camera["boot"] == "ipc-boot"
        assert camera["yawPerPixel"] == pytest.approx(0.1)
        assert camera["captureId"].startswith("camera:ipc-boot:")

        # The mapping is now installed through the real IPC/controller/lease
        # chain. A zero-response drag must fail against the confirmed pose,
        # rather than treating pre-dispatch evidence drift as progress.
        backend.camera_response_scale = 0.0
        no_response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 4})
        assert no_response["ok"] is False
        assert "signed progress" in str(no_response["error"])
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_calibration_rejects_pre_dispatch_drift_with_no_response(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(
        tmp_path,
        camera=True,
        preinstall_basis=False,
        preinstall_camera_mapping=False,
    )

    class DriftingCalibrationModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del kwargs
            assert image
            backend.motion_facing = 0.019
            backend.camera_response_scale = 0.0
            return ModelRouteObservation("world", "drifting-calibration", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = DriftingCalibrationModel()
    try:
        response = ipc.dispatch(
            {
                "command": "motion_camera_calibrate",
                "generation": generation,
                "deltaPixels": 8,
            }
        )
        assert response["ok"] is False
        assert "yaw" in response["error"]
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_runtime_rejects_pre_dispatch_drift_with_no_response(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)

    class DriftingRuntimeModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            backend.motion_facing = 0.019
            backend.camera_response_scale = 0.0
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = DriftingRuntimeModel()
    try:
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 8})
        assert response["ok"] is False
        assert "signed progress" in response["error"] or "calibrated error" in response["error"]
    finally:
        daemon.request_shutdown()
        driver.close()


@pytest.mark.parametrize(
    "response_scale",
    (0.0, -1.0),
    ids=("no-progress", "wrong-progress"),
)
def test_session_ipc_camera_rejects_observed_bad_progress(tmp_path: Path, response_scale: float) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)
    backend.pointer = (50, 50)
    try:
        backend.camera_response_scale = response_scale
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 16})
        assert response["ok"] is False
        assert "signed progress" in response["error"]
        assert daemon.camera_mapping is None
        assert daemon.movement_basis is None
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_rejects_infeasible_angle_before_observation_and_retains_proofs(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)
    backend.pointer = (50, 50)
    camera_mapping = daemon.camera_mapping
    movement_basis = daemon.movement_basis
    assert camera_mapping is not None
    assert movement_basis is not None
    try:
        before = len([argv for argv in backend.argv if "mousedown" in argv])
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 40})
        assert response["ok"] is False
        assert "angular motion" in response["error"]
        after = len([argv for argv in backend.argv if "mousedown" in argv])
        assert after == before
        assert daemon.camera_mapping is camera_mapping
        assert daemon.movement_basis is movement_basis
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_camera_rejects_pose_change_before_drag(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path, camera=True)

    class MovingModel:
        def cancel(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def observe_route(self, image: bytes, **kwargs: object) -> ModelRouteObservation:
            del kwargs
            assert image
            backend.motion_position = (backend.motion_position[0] + 0.5, backend.motion_position[1])
            return ModelRouteObservation("world", "pose-change-route", True, True)

        def observe_viewport(self, image: bytes, **kwargs: object) -> SimpleNamespace:
            del kwargs
            assert image
            backend.motion_position = (backend.motion_position[0] + 0.5, backend.motion_position[1])
            return SimpleNamespace(scene="world", point=(100, 100), ui_clear=True, units_clear=True)

    daemon.model = MovingModel()
    try:
        response = ipc.dispatch({"command": "motion_camera", "generation": generation, "deltaPixels": 20})
        assert response["ok"] is False
        assert "motion changed" in str(response["error"])
        assert not any("mousedown" in argv for argv in backend.argv)
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_stale_controller_and_stale_lease_epochs(tmp_path: Path) -> None:
    daemon, controller, driver, _backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    try:
        stale_controller = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation + 1,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert stale_controller == {
            "ok": False,
            "error": "motion intent requires the current interaction generation",
        }

        current_lease = driver.guardian.generation
        controller._planned_revoke = True
        try:
            driver.revoke("test lease takeover")
        finally:
            controller._planned_revoke = False
        assert driver.guardian.generation != current_lease
        stale_lease = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )
        assert stale_lease["ok"] is False
        assert "current armed input lease" in stale_lease["error"]
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_motion_cancellation_revokes_inflight_physical_process(tmp_path: Path) -> None:
    daemon, controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    backend.hold_process = True
    result: dict[str, dict[str, object]] = {}

    def dispatch() -> None:
        result["response"] = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )

    thread = threading.Thread(target=dispatch)
    thread.start()
    try:
        assert backend.process_started.wait(2.0)
        assert backend.process_pid is not None
        os.kill(backend.process_pid, 0)
        with backend._process_lock:
            assert backend.process_pid in backend._active_process_groups
        controller._planned_revoke = True
        try:
            driver.revoke("test cancellation")
        finally:
            controller._planned_revoke = False
        thread.join(2.0)
        assert not thread.is_alive()
        assert backend.cancelled.is_set()
        assert result["response"]["ok"] is False
        assert not driver.guardian.armed
        deadline = time.monotonic() + 1.0
        while backend.active_process_groups and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not backend.active_process_groups
        assert not driver.guardian.active_process_groups
    finally:
        if thread.is_alive():
            driver.revoke("test cancellation cleanup")
            thread.join(2.0)
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_rejects_second_physical_intent_without_waiting(tmp_path: Path) -> None:
    daemon, controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    backend.hold_process = True
    first_result: dict[str, dict[str, object]] = {}
    second_result: dict[str, dict[str, object]] = {}

    def dispatch_first() -> None:
        first_result["response"] = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )

    def dispatch_second() -> None:
        second_result["response"] = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )

    first = threading.Thread(target=dispatch_first)
    second = threading.Thread(target=dispatch_second)
    first.start()
    try:
        assert backend.process_started.wait(2.0)
        second.start()
        second.join(1.0)
        assert not second.is_alive(), "a second physical intent was queued behind the first"
        assert second_result["response"]["ok"] is False
        assert "in flight" in str(second_result["response"]["error"])
        keydown = [argv for argv in backend.argv if argv[:2] == ("xdotool", "keydown")]
        assert len(keydown) == 1
    finally:
        controller._planned_revoke = True
        try:
            driver.revoke("test concurrent intent cleanup")
        finally:
            controller._planned_revoke = False
        first.join(2.0)
        second.join(2.0)
        daemon.request_shutdown()
        driver.close()
    assert not first.is_alive()
    assert first_result["response"]["ok"] is False


def test_session_ipc_engine_and_interaction_start_share_capture_ownership_fence(
    tmp_path: Path,
) -> None:
    daemon, _controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    try:
        # Hold the same non-waiting fence used by the recurring engine while
        # the two real entrypoints race.  Neither caller may begin capture or
        # controller/lease mutation behind the active owner.
        fence = daemon._claim_motion_fence()
        assert fence is not None
        engine_result: dict[str, dict[str, object]] = {}

        def engine() -> None:
            engine_result["status"] = daemon.run_once()

        engine_thread = threading.Thread(target=engine)
        engine_thread.start()
        engine_thread.join(1.0)
        assert not engine_thread.is_alive()
        assert backend.capture_polls == 0

        start_result: dict[str, dict[str, object]] = {}

        def interaction_start() -> None:
            start_result["response"] = ipc.dispatch(
                {"command": "interaction_start", "generation": generation}
            )

        start_thread = threading.Thread(target=interaction_start)
        start_thread.start()
        start_thread.join(1.0)
        assert not start_thread.is_alive()
        start = start_result["response"]
        assert start["ok"] is False
        assert "in flight" in str(start["error"])
        fence.release()
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_idle_interaction_ready_engine_skips_capture(tmp_path: Path) -> None:
    daemon, _controller, driver, backend, _ipc, _generation = _ipc_motion_fixture(tmp_path)
    try:
        result = daemon.run_once()
        assert result["mode"] == "interaction"
        assert backend.capture_polls == 0
    finally:
        daemon.request_shutdown()
        driver.close()


def test_session_ipc_interaction_stop_revokes_inflight_physical_process(tmp_path: Path) -> None:
    daemon, controller, driver, backend, ipc, generation = _ipc_motion_fixture(tmp_path)
    backend.hold_process = True
    result: dict[str, dict[str, object]] = {}

    def dispatch() -> None:
        result["response"] = ipc.dispatch(
            {
                "command": "motion_key",
                "generation": generation,
                "role": KeyRole.MOVE_FORWARD.value,
                "duration": 0.08,
            }
        )

    thread = threading.Thread(target=dispatch)
    stop_result: dict[str, dict[str, object]] = {}

    def stop() -> None:
        stop_result["response"] = ipc.dispatch({"command": "interaction_stop", "generation": generation})

    stop_thread = threading.Thread(target=stop)
    thread.start()
    try:
        assert backend.process_started.wait(2.0)
        stop_thread.start()
        stop_thread.join(1.0)
        assert not stop_thread.is_alive(), "interaction stop waited behind an active physical intent"
        stopped = stop_result["response"]
        assert stopped["ok"] is True, stopped
        assert stopped["status"]["mode"] == SessionMode.REPEAT.value
        thread.join(2.0)
        assert not thread.is_alive()
        assert backend.cancelled.is_set()
        assert result["response"]["ok"] is False
        assert not driver.guardian.armed
    finally:
        if thread.is_alive():
            controller._planned_revoke = True
            try:
                driver.revoke("test interaction stop cleanup")
            finally:
                controller._planned_revoke = False
            thread.join(2.0)
        stop_thread.join(2.0)
        daemon.request_shutdown()
        driver.close()


def _frame(*, facing: float = 0.0) -> MotionFrame:
    snapshot = {
        "schema": 6,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": _executor_none(),
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": False,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
        "chatEdit": {"active": False, "focused": False},
        "motion": {
            "position": {"x": 10.0, "y": 20.0, "z": 3.0, "instance": 4},
            "facing": facing,
            "speed": 0.0,
            "combat": False,
            "deadOrGhost": False,
            "bindings": {
                "moveForward": "W",
                "moveBackward": "S",
                "strafeLeft": "A",
                "strafeRight": "D",
                "turnLeft": "Q",
                "jump": "SPACE",
                "turnRight": "E",
            },
            "native": {
                "ctm": {
                    "accessor": "C_CVar.GetCVarBool",
                    "cvar": "autointeract",
                    "state": "disabled",
                    "reason": "",
                },
                "ground": {
                    "state": "grounded",
                    "falling": "false",
                    "flying": "false",
                    "swimming": "false",
                    "submerged": "false",
                    "mounted": "false",
                    "taxi": "false",
                    "reason": "",
                },
                "speedTuple": {
                    "current": 0,
                    "run": 0,
                    "flight": 0,
                    "swim": 0,
                    "unitStatus": "unverified",
                },
            },
        },
        "context": {"zone": "Ironforge", "subzone": "The Commons"},
    }
    sample = SimpleNamespace(
        kind="FRESH",
        capture_monotonic=10.0,
        frame=SimpleNamespace(
            boot="boot-motion",
            sequence=7,
            snapshot=SimpleNamespace(value=snapshot),
        ),
    )
    return MotionFrame.from_sample(sample, _Identity())


def test_route_observer_binds_egocentric_heading_to_nonzero_native_facing() -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon._native_clock = lambda: 10.2
    deadline_local = threading.local()
    deadline_local.deadline = 30.0
    daemon._motion_deadline_local = deadline_local
    daemon._require_interaction = lambda _generation, **_kwargs: 1
    daemon.movement_basis = MovementBasis(
        identity_fingerprint="client-motion",
        boot="boot-motion",
        generation=1,
        capture_id="movement:boot-motion:1:2",
        before_sequence=1,
        after_sequence=2,
        forward_heading_offset=0.8,
        forward_displacement=0.1,
        pulse_seconds=0.1,
        strafe_heading_offset=math.pi / 2,
        strafe_displacement=0.1,
        facing_sign=-1,
        handedness=1,
    )
    daemon.config = SimpleNamespace(profile=SimpleNamespace(model_crop=(0, 0, 640, 480)))
    daemon.driver = SimpleNamespace(
        resolve_identity=lambda require_foreground=True, deadline_monotonic=None: _Identity(),
    )
    capture_deadlines: list[float] = []

    def capture_model(*, deadline: float) -> bytes:
        capture_deadlines.append(deadline)
        return b"fresh-crop"

    daemon._capture_model = capture_model
    seen: dict[str, float] = {}

    def observe_route(image: bytes, **kwargs: object) -> ModelRouteObservation:
        assert image == b"fresh-crop"
        seen.update({key: float(kwargs[key]) for key in ("corridor_heading", "corridor_distance")})
        return ModelRouteObservation("world", "corridor-7", True, True)

    daemon.model = SimpleNamespace(observe_route=observe_route)
    frame = _frame(facing=1.2)

    evidence = daemon._observe_route(1, frame.x + 1.0, frame.y, frame=frame)

    assert seen["corridor_heading"] == pytest.approx(0.4)
    assert seen["corridor_distance"] == 1.0
    assert capture_deadlines == [30.0]
    assert evidence.corridor_heading == pytest.approx(0.4)
    assert evidence.boot == frame.boot
    assert evidence.sequence == frame.sequence


def test_route_parser_accepts_honest_negative_proof() -> None:
    usage = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }
    answer = '{"scene":"world","routeId":"","targetVisible":false,"groundClear":false}'
    stream = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "route-negative"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "answer-1", "type": "agent_message", "text": answer},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": usage}),
        )
    )
    parsed = LunaAdapter._parse_route_observation(stream)
    assert not parsed.target_visible
    assert not parsed.ground_clear


def test_route_prompt_defines_native_character_frame_and_refusal_contract(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }
    answer = '{"scene":"world","routeId":"clear","targetVisible":true,"groundClear":true}'
    stream = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "route-prompt"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "answer-1", "type": "agent_message", "text": answer},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": usage}),
        )
    )
    adapter = LunaAdapter.__new__(LunaAdapter)
    adapter.neutral_dir = tmp_path
    prompts: list[str] = []

    def run_cli(*args: object, **kwargs: object) -> str:
        del args
        prompts.append(str(kwargs["prompt"]))
        return stream

    adapter._run_cli = run_cli
    result = adapter.observe_route(
        b"verified-crop",
        verified_crop=(0, 0, 640, 480),
        origin_x=10.0,
        origin_y=20.0,
        corridor_heading=math.pi / 2,
        corridor_distance=1.0,
    )
    assert result.target_visible
    prompt = prompts[0]
    assert "character-forward" in prompt
    assert "0 radians" in prompt
    assert "+pi/2" in prompt
    assert "measured world units" in prompt
    assert "camera" in prompt
    assert "empty routeId" in prompt
    assert "direction" in prompt


def test_model_crop_forwards_absolute_deadline_to_production_capture(tmp_path) -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(capture_dir=tmp_path)
    daemon.capture_index = 0
    daemon._native_clock = lambda: 10.0
    seen: list[float | None] = []

    def capture(path, *, model: bool, deadline_monotonic: float | None = None):
        assert model
        seen.append(deadline_monotonic)
        path.write_bytes(b"crop")
        return path

    daemon.driver = SimpleNamespace(capture_client_crop=capture)
    assert daemon._capture_model(deadline=11.0) == b"crop"
    assert seen == [11.0]


def _pointer_anchor_deadline_fixture(monkeypatch: pytest.MonkeyPatch):
    now = [100.0]
    real_time = time
    real_monotonic = real_time.monotonic

    class DaemonTimeProxy:
        def monotonic(self) -> float:
            return now[0]

        def __getattr__(self, name: str) -> object:
            return getattr(real_time, name)

    monkeypatch.setattr(daemon_module, "time", DaemonTimeProxy())
    assert real_time.monotonic is real_monotonic

    identity = SimpleNamespace(
        fingerprint="client-motion",
        child_xid=22,
        xid=11,
        child_geometry=(0, 0, 640, 480),
    )
    status = SimpleNamespace(generation=3, state=ControllerState.READY)
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(
        profile=SimpleNamespace(
            character="Arthur",
            realm="Classic Beta PvE 2",
            model_crop=(0, 0, 640, 480),
        )
    )
    daemon.controller = SimpleNamespace(lock=threading.RLock(), status=lambda: status)
    daemon._configured_anchor_provider = None
    daemon._named_capture_sequence = 0
    daemon._record_visual_proof = lambda *args, **kwargs: None

    class TimedCapture:
        def read_bytes(self) -> bytes:
            now[0] = 105.0
            return b"retained-capture"

        def __str__(self) -> str:
            return "retained-capture.png"

    capture = TimedCapture()
    daemon._capture_model_artifact = lambda *args, **kwargs: (capture, "d" * 64, 100.0)

    def resolve_identity(*, deadline_monotonic: float | None = None) -> SimpleNamespace:
        assert deadline_monotonic == pytest.approx(105.0)
        return identity

    def build_measured_anchor(**kwargs: object) -> SimpleNamespace:
        assert kwargs["deadline_monotonic"] == pytest.approx(105.0)
        return SimpleNamespace(
            capture_sequence=1,
            bounds=(40, 30, 80, 60),
            target_xid=22,
            target_ancestry=(11, 22),
            evidence_hash="anchor-proof",
        )

    daemon.driver = SimpleNamespace(
        ANCHOR_CONTROLS={"executor_click": "crowley_executor_button"},
        anchor_observation_deadline=None,
        guardian=SimpleNamespace(armed=True, generation=7),
        backend=object(),
        resolve_identity=resolve_identity,
        build_measured_anchor=build_measured_anchor,
    )
    return daemon, identity, now


def test_pointer_anchor_read_slack_expiry_does_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, identity, _now = _pointer_anchor_deadline_fixture(monkeypatch)
    model_calls: list[float] = []

    def observe_model(*args: object, timeout: float, **kwargs: object) -> object:
        del args, kwargs
        model_calls.append(timeout)
        return SimpleNamespace(
            state="executor_mailbox",
            facts=daemon._named_facts(
                "executor_mailbox", character="Arthur", realm="Classic Beta PvE 2", idle="true"
            ),
            anchor=(40, 30, 80, 60),
        )

    daemon._observe_model = observe_model
    with pytest.raises(DriverError, match="bounded deadline"):
        daemon._measure_pointer_anchor(identity, "executor_click")
    assert model_calls == []


def test_pointer_anchor_model_receives_post_read_remaining_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, identity, now = _pointer_anchor_deadline_fixture(monkeypatch)
    model_timeouts: list[float] = []

    class PartialCapture:
        def read_bytes(self) -> bytes:
            now[0] = 101.5
            return b"retained-capture"

        def __str__(self) -> str:
            return "retained-capture.png"

    capture = PartialCapture()
    daemon._capture_model_artifact = lambda *args, **kwargs: (capture, "d" * 64, 100.0)

    def observe_model(image: bytes, *, timeout: float, **kwargs: object) -> object:
        assert image == b"retained-capture"
        model_timeouts.append(timeout)
        now[0] = 102.0
        return SimpleNamespace(
            state="executor_mailbox",
            facts=daemon._named_facts(
                "executor_mailbox", character="Arthur", realm="Classic Beta PvE 2", idle="true"
            ),
            anchor=(40, 30, 80, 60),
        )

    daemon._observe_model = observe_model
    daemon._measure_pointer_anchor(identity, "executor_click")
    assert model_timeouts == [pytest.approx(3.5)]


def test_native_provider_forwards_one_absolute_deadline_to_production_capture() -> None:
    motion = _frame()
    sample = Sample(
        "FRESH",
        SimpleNamespace(
            boot=motion.boot,
            sequence=motion.sequence,
            snapshot=SimpleNamespace(
                value={
                    "schema": 6,
                    "hover": {"kind": "none", "name": ""},
                    "mailOpen": False,
                    "executor": _executor_none(),
                    "blockingUI": {
                        "staticPopupAvailable": True,
                        "staticPopupShown": False,
                        "gameMenuAvailable": True,
                        "gameMenuShown": False,
                    },
                    "auctioneer": {
                        "targetPresent": False,
                        "targetName": "",
                        "rangeAvailable": False,
                        "withinTradeRange": False,
                        "bindingAvailable": False,
                        "ctrlF12Action": "",
                        "interactionMode": "any",
                    },
                    "chatEdit": {"active": False, "focused": False},
                    "motion": {
                        "position": {
                            "x": motion.x,
                            "y": motion.y,
                            "z": motion.z,
                            "instance": motion.instance,
                        },
                        "facing": motion.facing,
                        "speed": motion.speed,
                        "combat": motion.combat,
                        "deadOrGhost": motion.dead_or_ghost,
                        "bindings": dict(motion.bindings),
                        "native": {
                            "ctm": {
                                "accessor": "C_CVar.GetCVarBool",
                                "cvar": "autointeract",
                                "state": "disabled",
                                "reason": "",
                            },
                            "ground": {
                                "state": "grounded",
                                "falling": "false",
                                "flying": "false",
                                "swimming": "false",
                                "submerged": "false",
                                "mounted": "false",
                                "taxi": "false",
                                "reason": "",
                            },
                            "speedTuple": {
                                "current": motion.speed,
                                "run": 0,
                                "flight": 0,
                                "swim": 0,
                                "unitStatus": "unverified",
                            },
                        },
                    },
                    "context": {"zone": "Ironforge", "subzone": "The Commons"},
                }
            ),
        ),
        motion.capture_monotonic,
        (0, 0, 640, 480),
        1,
        "client-motion",
    )
    seen: list[float | None] = []

    def capture(*, deadline: float | None = None) -> Sample:
        seen.append(deadline)
        return sample

    provider = NativeMotionProvider(
        lambda: sample,
        lambda **_kwargs: _Identity(),
        capture_with_deadline=capture,
        clock=lambda: 10.1,
    )
    frame = provider.observe(1, deadline=11.0)
    assert frame.sequence == motion.sequence
    assert seen == [11.0]
