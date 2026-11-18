"""One guarded desktop adapter for the schema-3 local operation contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from crowley_common.frames import PixelDecoder, ProtocolError, Sample
from crowley_common.platform.paths import XdgDirs
from crowley_common.platform.privfs import atomic_write_private, ensure_private_dir

from .config import AutomationConfig
from .desktop_input import INTERACT_CHORD, GuiLease, XtestKeys, parse_keymap
from .desktop_observe import (
    ProcProcessSource,
    ScreenCanvas,
    WindowState,
    X11Window,
    crop_canvas,
    x11_environment,
    x11_runner,
)
from .desktop_session import DesktopSessionProvider
from .gui.click_gate import ClickRatePermit, acquire_click_rate_permit
from .gui.control_child import ControlChildRunner
from .gui.input_tool import PinnedInputTool
from .gui.keyboard import key_argv, transition_argv
from .gui.pointer import (
    BindingMetadata,
    Point,
    PointerButton,
    PointerTarget,
    Rect,
    build_pointer_motion_chain,
    plan_human_pointer,
)
from .gui.process import OwnedGameMatcher
from .gui.timeline import plan_text
from .gui.uinput import UinputSession, calibration
from .movement import Motion, RouteProfile, WaypointResult, walk_waypoint
from .presence import Presence
from .runner import Admission, DispatchReceipt
from .targets import TargetProfile

MAX_FRAME_AGE_SECONDS = 3.0
FRAME_REUSE_SECONDS = 2.0
COMMAND_TIMEOUT_SECONDS = 5.0
STAGE_TIMEOUT_SECONDS = 90.0
RECEIPT_TIMEOUT_SECONDS = 45.0
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SELLER_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_SELLER_MANAGED_AUCTION_IDS = 1024
_RECEIPT_FIELDS = ("callbackDelivered", "postcondition", "actionComplete")


class DesktopBackendError(RuntimeError):
    """Desktop state or a protected input did not satisfy the operation contract."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    sample: Sample
    window: WindowState
    pids: tuple[int, ...]
    human_active: bool
    human_reason: str

    @property
    def value(self) -> dict[str, Any]:
        if self.sample.frame is None:
            raise DesktopBackendError("automation frame is unavailable")
        return self.sample.frame.snapshot.value


class BackendIO(Protocol):
    """Small concrete IO seam used by contract tests."""

    def capture(self, *, timeout: float = 5.0) -> CapturedFrame: ...
    def recheck(self, captured: CapturedFrame) -> CapturedFrame: ...
    def press(self, key: str, guard: Callable[[], None]) -> None: ...
    def chord(self, steps: tuple[tuple[str, str], ...], guard: Callable[[], None]) -> None: ...
    def type_text(self, text: str, guard: Callable[[], None]) -> None: ...
    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None: ...
    def pointer_position(self) -> tuple[int, int, int]: ...
    def emit(self, args: Sequence[str], timeout: float) -> str: ...
    def window_contains(self, child: int, parent: int) -> bool: ...
    def permit(
        self, deadline: float, guard: Callable[[], None]
    ) -> AbstractContextManager[ClickRatePermit]: ...
    def monotonic(self) -> float: ...
    def wall_time(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class DesktopIO:
    """Linux process/window/capture and pinned XTEST implementations."""

    def __init__(
        self,
        config: AutomationConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        presence: Presence | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._env = x11_environment(config.display)
        self._process = ProcProcessSource(
            OwnedGameMatcher.for_game(
                game_executable=config.game_executable,
                game_argv0=config.game_argv0,
                wine_prefix=config.wine_prefix,
            )
        )
        self._x11 = x11_runner(self._env)
        self._window_source = X11Window(config.window_class, config.window_title, self._x11)
        self._dirs = XdgDirs.from_environ(os.environ)
        self._canvas = ScreenCanvas(self._dirs.crowley_runtime / "automation-capture")
        self._decoder = PixelDecoder(expected_kind="automation", crop=config.canvas_crop)
        self._presence = presence or Presence()
        self._tool: PinnedInputTool | None = None
        self._keys: XtestKeys | None = None
        self._control: ControlChildRunner | None = None
        self._devices: UinputSession | None = None
        self._calibration: dict[str, Any] | None = None
        self._lease: GuiLease | None = None
        self._during: Callable[[], None] = lambda: None
        self._preparing: Callable[[], None] = lambda: None
        self._stopping: Callable[[], bool] = lambda: False
        self._deadline: Callable[[], float] = lambda: 0.0
        self._session = DesktopSessionProvider(config.display)
        self._session_fingerprint: str | None = None
        self._process_identities: tuple[Any, ...] | None = None
        self._display_size: tuple[int, int] | None = None

    def bind_input(
        self,
        lease: GuiLease,
        *,
        during: Callable[[], None],
        stopping: Callable[[], bool],
        deadline: Callable[[], float],
        preparing: Callable[[], None],
    ) -> None:
        self._lease, self._during, self._stopping, self._deadline = lease, during, stopping, deadline
        self._preparing = preparing
        lease.protect_release(self._input_released)

    def _input_released(self) -> bool:
        control = self._control
        control_ready = control is None or (
            control.child_pid is None and not control.possible_down and not control.cleanup_failed
        )
        return control_ready and (self._devices is None or self._devices.released)

    def _device_session(self) -> UinputSession:
        if self._devices is None:
            if self._lease is None or self.config.input_calibration is None:
                raise DesktopBackendError("selected device transport is not bound and calibrated")
            measured = calibration(self.config.input_calibration, environment=self._env)
            if not measured["idle_coverage"] and 0 < self._deadline() - time.time() <= 300:
                raise DesktopBackendError(
                    "cold uinput lacks qualified idle history for this short arm; "
                    "qualify keyboard/mouse idle coverage or prepare a foreground seller session"
                )
            if self._display_size is not None and self._display_size != tuple(measured["display_size"]):
                raise DesktopBackendError("calibrated display dimensions changed")
            self._calibration = measured
            self._devices = UinputSession(
                self._lease,
                measured,
                environment=self._env,
                guard=self._during,
                deadline=self._deadline,
                preparing=self._preparing,
            )
        return self._devices

    def _human_active(self) -> tuple[bool, str]:
        if self.config.input_backend == "uinput":
            if self._calibration is not None and self._display_size != tuple(
                self._calibration["display_size"]
            ):
                raise DesktopBackendError("calibrated display dimensions changed")
            return self._device_session().human_active()
        return self._presence.human_active()

    def check_emission(self, captured: CapturedFrame) -> None:
        """No capture and no reuse of stale native evidence as admission."""
        session = self._session.current()
        if self._session_fingerprint is None or session.fingerprint != self._session_fingerprint:
            raise DesktopBackendError("graphical session changed during input")
        pids = tuple(self._process.pids())
        if pids != captured.pids or captured.window.xid is None or len(pids) != 1:
            raise DesktopBackendError("owned process changed during input")
        if self._process.identities() != self._process_identities:
            raise DesktopBackendError("owned process instance changed during input")
        owned, focused = self._window_source.focus_state(captured.window.xid, pids[0])
        if not owned or not focused:
            raise DesktopBackendError("owned game focus changed during input")
        # The helper monitors physical devices independently; nested status requests
        # during an executing job would corrupt the request/response stream.
        if self.config.input_backend == "xtest" and self._presence.human_active()[0]:
            raise DesktopBackendError("human presence blocks input")

    def _ensure_input(self) -> tuple[PinnedInputTool, XtestKeys]:
        if self._tool is None:
            self._tool = PinnedInputTool.load()
            if self._lease is None:
                raise DesktopBackendError("input requires its bound GUI lease")
            self._control = ControlChildRunner(
                self._lease,
                self._tool,
                environment=self._env,
                stopping=self._stopping,
                guard=self._during,
                during=self._during,
            )
            self._keys = XtestKeys(self._tool, self._env, run=self._control.run, query_run=self._runner)
        assert self._keys is not None
        return self._tool, self._keys

    def capture(self, *, timeout: float = 5.0) -> CapturedFrame:
        if self.config.input_backend == "uinput":
            # Device preparation can establish initial idle coverage. Do it
            # before starting any native-frame freshness clock.
            self._device_session()
        session = self._session.current()
        if dict(session.environment).get("XAUTHORITY") != self._env.get("XAUTHORITY"):
            raise DesktopBackendError("authenticated graphical session changed")
        if self._session_fingerprint is not None and self._session_fingerprint != session.fingerprint:
            raise DesktopBackendError("graphical session identity changed")
        self._session_fingerprint = session.fingerprint
        deadline = time.monotonic() + timeout
        last_reason = "automation publication has not advanced"
        while time.monotonic() < deadline:
            began = time.monotonic()
            pids = tuple(self._process.pids())
            if not pids:
                raise DesktopBackendError("no owned game process")
            identities = self._process.identities()
            if self._process_identities is not None and identities != self._process_identities:
                raise DesktopBackendError("owned game process instance changed")
            self._process_identities = identities
            window = self._window_source.observe(list(pids))
            if not window.ok or window.xid is None:
                raise DesktopBackendError(f"game window is not valid: {window.detail}")
            identity = f"{','.join(map(str, pids))}:{window.xid}"
            image = self._canvas.capture(window.display)
            canvas = crop_canvas(image, window.display, self.config.canvas_crop, "automation canvas")
            try:
                sample = self._decoder.decode(canvas, monotonic=began, window_identity=identity)
            except ProtocolError as exc:
                raise DesktopBackendError(f"automation frame failed validation: {exc}") from exc
            current_pids = tuple(self._process.pids())
            current_window = self._window_source.observe(list(current_pids)) if current_pids else None
            if (
                current_pids != pids
                or current_window is None
                or not current_window.ok
                or current_window.xid != window.xid
                or time.monotonic() - began > MAX_FRAME_AGE_SECONDS
            ):
                raise DesktopBackendError("process, window, or capture age changed during observation")
            self._display_size = window.display
            active, reason = self._human_active()
            if sample.kind == "FRESH" and sample.frame is not None:
                self._validate_identity(sample.frame.snapshot.value)
                return CapturedFrame(sample, current_window, pids, active, reason)
            last_reason = "automation frame is cached or unchanged"
            time.sleep(min(0.08, max(0.0, deadline - time.monotonic())))
        raise DesktopBackendError(last_reason)

    def recheck(self, captured: CapturedFrame) -> CapturedFrame:
        """Recheck live process/window/presence while retaining the original frame timestamp."""
        pids = tuple(self._process.pids())
        if not pids:
            raise DesktopBackendError("owned game process disappeared")
        window = self._window_source.observe(list(pids))
        identity = f"{','.join(map(str, pids))}:{window.xid}"
        if (
            pids != captured.pids
            or not window.ok
            or window.xid != captured.window.xid
            or identity != captured.sample.window_identity
            or time.monotonic() - captured.sample.capture_monotonic > MAX_FRAME_AGE_SECONDS
        ):
            raise DesktopBackendError("process, window, or original capture changed during input")
        active, reason = self._human_active()
        return replace(captured, window=window, human_active=active, human_reason=reason)

    def _validate_identity(self, snapshot: dict[str, Any]) -> None:
        context = snapshot["context"]
        expected = {
            "character": self.config.character,
            "realm": self.config.realm,
            "playerFaction": self.config.faction,
            "clientBuild": self.config.build.rsplit(".", 1)[1],
            "interface": self.config.interface,
            "nativeRegion": self.config.native_region,
        }
        if any(context[key] != value for key, value in expected.items()):
            raise DesktopBackendError("automation frame belongs to another configured identity")

    def _keymap(self) -> dict[str, int]:
        try:
            done = self._runner(
                ["xmodmap", "-pke"],
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=self._env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DesktopBackendError("measured keymap is unavailable") from exc
        if done.returncode != 0:
            raise DesktopBackendError("measured keymap query failed")
        if self._calibration is not None and (
            hashlib.sha256(done.stdout.encode()).hexdigest() != self._calibration["x_keymap_sha256"]
        ):
            raise DesktopBackendError("qualified keyboard layout changed")
        return parse_keymap(done.stdout)

    def _send_keys(self, argv: list[str], held: frozenset[str], guard: Callable[[], None]) -> None:
        guard()
        try:
            self._emit(argv[1:], COMMAND_TIMEOUT_SECONDS)
        except BaseException:
            for code in sorted(held, reverse=True):
                try:
                    self._emit(["keyup", code], 2.0)
                except Exception:
                    pass
            raise

    def press(self, key: str, guard: Callable[[], None]) -> None:
        argv, held = key_argv(key, self._keymap())
        self._send_keys(argv, held, guard)

    def chord(self, steps: tuple[tuple[str, str], ...], guard: Callable[[], None]) -> None:
        argv, held = transition_argv(steps, self._keymap())
        self._send_keys(argv, held, guard)

    def _emit(self, args: Sequence[str], timeout: float) -> str:
        if self.config.input_backend == "uinput" and tuple(args) != ("getmouselocation", "--shell"):
            devices = self._device_session()
            if args and args[0] in {"sleep", "mousedown"}:
                if (
                    calibration(self.config.input_calibration or "", environment=self._env)
                    != self._calibration
                ):
                    raise DesktopBackendError("qualified pointer settings or calibration changed")
            devices.execute(args, timeout=timeout)
            return ""
        tool, _keys = self._ensure_input()
        assert self._control is not None
        try:
            done = self._control.run(
                [tool.proc_path, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=self._env,
                pass_fds=(tool.image.fd,),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DesktopBackendError(f"pinned input tool failed: {type(exc).__name__}") from exc
        if done.returncode != 0:
            raise DesktopBackendError(f"pinned input tool exited {done.returncode}")
        return done.stdout

    def type_text(self, text: str, guard: Callable[[], None]) -> None:
        plan = plan_text(text)
        if not plan.events:
            return
        argv, _held = plan.xtest(self._keymap())
        guard()
        try:
            self._emit(argv[1:], plan.timeout)
        except BaseException:
            if self._control is not None:
                self._control.release_only()
            if self._devices is not None:
                self._devices.close()
            raise

    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None:
        if self.config.input_backend == "uinput":
            if key not in {"Up", "Left", "Right"} or not 0 < seconds <= 0.25:
                raise DesktopBackendError("bounded movement hold is invalid")
            argv, _held = key_argv(key, self._keymap())
            guard()
            self._emit([*argv[1:3], "sleep", str(seconds), *argv[3:]], seconds + 3)
            return
        _tool, keys = self._ensure_input()
        keys.hold(key, seconds, guard)

    def forward(self, travel_deadline: float, observed_at: float) -> None:
        code = self._keymap().get("Up")
        if code is None:
            raise DesktopBackendError("forward key has no measured mapping")
        devices = self._device_session()
        # X and evdev codes are separate; only the explicit qualified mapping is used.
        measured = self._calibration
        assert measured is not None
        evdev_code = measured["x_to_evdev"].get(str(code))
        if evdev_code is None:
            raise DesktopBackendError("forward key has no qualified evdev mapping")
        devices.forward(evdev_code, feedback_deadline=observed_at + 2.0, travel_deadline=travel_deadline)

    def release_movement(self) -> None:
        if self._devices is not None:
            self._devices.release_all()

    def pointer_position(self) -> tuple[int, int, int]:
        output = self._emit(["getmouselocation", "--shell"], COMMAND_TIMEOUT_SECONDS)
        values: dict[str, int] = {}
        for line in output.splitlines():
            key, separator, value = line.strip().partition("=")
            if key in {"X", "Y", "SCREEN", "WINDOW"} and separator and key not in values:
                try:
                    values[key] = int(value, 0) if key == "WINDOW" else int(value)
                except ValueError as exc:
                    raise DesktopBackendError("pointer position is malformed") from exc
        if set(values) != {"X", "Y", "SCREEN", "WINDOW"} or values["SCREEN"] != 0:
            raise DesktopBackendError("pointer position is incomplete")
        return values["X"], values["Y"], values["WINDOW"]

    def emit(self, args: Sequence[str], timeout: float) -> str:
        return self._emit(args, timeout)

    def window_contains(self, child: int, parent: int) -> bool:
        current = child
        for _ in range(16):
            if current == parent:
                return True
            code, output = self._x11(["xwininfo", "-id", hex(current), "-tree"])
            if code != 0:
                return False
            parent_match = re.search(r"Parent window id: (0x[0-9a-fA-F]+)", output)
            root_match = re.search(r"Root window id: (0x[0-9a-fA-F]+)", output)
            if parent_match is None or root_match is None:
                return False
            next_id = int(parent_match.group(1), 16)
            if next_id == int(root_match.group(1), 16):
                return False
            current = next_id
        return False

    def permit(self, deadline: float, guard: Callable[[], None]) -> AbstractContextManager[ClickRatePermit]:
        return acquire_click_rate_permit(deadline_monotonic=deadline, cancellation_check=guard)

    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        if self._devices is not None:
            self._devices.close()
        if self._control is not None:
            self._control.close()
        if not self._input_released():
            raise DesktopBackendError("input cleanup is incomplete; GUI lease remains held")
        if self._tool is not None:
            self._tool.close()
            self._tool = None
            self._keys = None
        if self._lease is not None:
            self._lease.release_retained()


class DesktopBackend:
    """The concrete runner adapter. All input stays behind current-call gates."""

    def __init__(
        self,
        config: AutomationConfig,
        targets: Mapping[str, TargetProfile],
        routes: Mapping[str, RouteProfile],
        lease: GuiLease,
        *,
        io: BackendIO | None = None,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self.config, self.targets, self.routes, self.lease = config, dict(targets), dict(routes), lease
        self.io: BackendIO = DesktopIO(config) if io is None else io
        self.stop_requested = stop_requested
        self._boot: str | None = None
        self._expected: tuple[str, int, int, str] | None = None
        self._staged_receipt_sequence = 0
        self._deadline_wall = 0
        self._snapshot: CapturedFrame | None = None
        self._seller_ah_target_id: str | None = None
        self._inspection_snapshots: dict[str, tuple[str, int, int, str]] = {}
        self.preflight_gold: int | None = None
        self.cleanup_state = "not_needed"
        self.evidence_state = "not_written"
        if isinstance(self.io, DesktopIO):
            self.io.bind_input(
                lease,
                during=self._emission_guard,
                stopping=lambda: self.stop_requested(),
                deadline=lambda: float(self._deadline_wall),
                preparing=self._check_controls,
            )

    def _emission_guard(self) -> None:
        self._check_controls()
        if self._snapshot is None or not isinstance(self.io, DesktopIO):
            raise DesktopBackendError("input has no admitted desktop snapshot")
        self.io.check_emission(self._snapshot)
        self._check_controls()

    def set_deadline(self, expires_at: int) -> None:
        self._deadline_wall = expires_at

    def bind_seller_ah_target(self, target_id: str) -> None:
        """Bind seller queries and AH clicks to one configured, reviewed AH NPC."""
        profile = self.targets.get(target_id)
        if (
            profile is None
            or profile.kind != "npc"
            or profile.expected_ui != "ah"
            or (profile.house, profile.character) != (self.config.house, self.config.character)
        ):
            raise DesktopBackendError("seller Auction House target is not a matching configured NPC profile")
        self._seller_ah_target_id = target_id

    def _check_seller_ah_source(self, value: dict[str, Any]) -> None:
        target_id = self._seller_ah_target_id
        profile = None if target_id is None else self.targets.get(target_id)
        if profile is None:
            raise DesktopBackendError("seller Auction House source profile has not been bound")
        target = value["motion"]["target"]
        auctioneer = value["auctioneer"]
        if (
            value["ahOpen"] is not True
            or value["motion"]["instance"] != profile.instance_id
            or not target["present"]
            or target["guid"] != profile.guid
            or target["name"] != profile.name
            or not target["rangeAvailable"]
            or not target["withinRange"]
            or not auctioneer["targetPresent"]
            or auctioneer["targetName"] != profile.name
            or not auctioneer["rangeAvailable"]
            or not auctioneer["withinTradeRange"]
        ):
            raise DesktopBackendError("open Auction House is not bound to the measured seller NPC source")

    def _fresh(self, *, timeout: float = 5.0) -> CapturedFrame:
        captured = self.io.capture(timeout=timeout)
        if captured.sample.kind != "FRESH" or captured.sample.frame is None:
            raise DesktopBackendError("current call did not decode a fresh automation frame")
        age = self.io.monotonic() - captured.sample.capture_monotonic
        if age < 0 or age > MAX_FRAME_AGE_SECONDS:
            raise DesktopBackendError("automation capture is stale")
        value = captured.value
        if self._boot is None:
            self._boot = value["boot"]
        elif value["boot"] != self._boot:
            raise DesktopBackendError("automation boot changed during the operation")
        self._snapshot = captured
        return captured

    def _guard(self) -> CapturedFrame:
        self._check_controls()
        current = self._snapshot
        age = None if current is None else self.io.monotonic() - current.sample.capture_monotonic
        if current is not None and age is not None and 0 <= age <= FRAME_REUSE_SECONDS:
            captured = self.io.recheck(current)
            self._snapshot = captured
        else:
            captured = self._fresh(timeout=COMMAND_TIMEOUT_SECONDS)
        self._check_frame_gate(captured)
        return captured

    def _fresh_guard(self) -> CapturedFrame:
        self._check_controls()
        captured = self._fresh(timeout=COMMAND_TIMEOUT_SECONDS)
        self._check_frame_gate(captured)
        return captured

    def _check_controls(self) -> None:
        if self.stop_requested():
            raise DesktopBackendError("durable stop is active")
        if not self.lease.held:
            raise DesktopBackendError("shared GUI lease is not held")
        if self.lease.retained:
            raise DesktopBackendError("input cleanup is incomplete; GUI lease remains held")
        if self._deadline_wall and self.io.wall_time() >= self._deadline_wall:
            raise DesktopBackendError("batch, authorization, or session arm expired")

    def _check_frame_gate(self, captured: CapturedFrame) -> None:
        # This runs after capture/recheck so a blocking observation cannot outlive stop or expiry.
        self._check_controls()
        if captured.human_active:
            raise DesktopBackendError("human presence blocks input")
        if not captured.window.focused or not captured.window.ok:
            raise DesktopBackendError("owned game window is not focused")

    def _input_guard(self) -> None:
        self._guard()

    def observe(self) -> Admission:
        captured = self._fresh()
        value = captured.value
        self.preflight_gold = value["goldCopper"]
        operation = value["operation"]
        action_value = operation["action"]
        digest = None
        if action_value is not False and operation["batchId"]:
            digest = _action_digest(action_value)
        if self._expected is not None:
            expected_batch, expected_ordinal, expected_step, expected_digest = self._expected
            if (
                operation["batchId"] != expected_batch
                or operation["ordinal"] != expected_ordinal
                or operation["stepIndex"] != expected_step
                or digest != expected_digest
            ):
                raise DesktopBackendError("native staged action readback changed")
        return Admission(
            session_boot_id=value["boot"],
            house=self.config.house,
            character=value["context"]["character"],
            observed_at=value["wallTime"],
            human_present=captured.human_active,
            focused=captured.window.focused,
            gui_lock_owned=self.lease.held,
            client_ah_owned=value["ownership"]["ahOwner"] == "automation",
            staged_batch_id=operation["batchId"] or None,
            staged_ordinal=operation["ordinal"] if operation["batchId"] else None,
            staged_step_index=operation["stepIndex"] if operation["batchId"] else None,
            staged_action_digest=digest,
            ready=operation["ready"] if operation["batchId"] else None,
            receipt_sequence=operation["receipt"]["sequence"],
            client_build=value["context"]["clientBuild"],
        )

    def observe_native(self) -> dict[str, Any]:
        """Return one fresh, identity-checked automation observation for read-only CLI output."""
        return self._fresh().value

    def observe_seller_market(
        self, item_key: str, request_id: str, managed_ids: frozenset[int] = frozenset()
    ) -> dict[str, object]:
        """Request one targeted commodity query and return only its matching complete result."""
        from .model import validate_item_key

        try:
            validate_item_key(item_key)
        except ValueError as exc:
            raise DesktopBackendError("seller market item key is not canonical") from exc
        if ":" in item_key:
            raise DesktopBackendError("targeted seller search supports canonical commodity identities only")
        if type(request_id) is not str or _SELLER_REQUEST_ID.fullmatch(request_id) is None:
            raise DesktopBackendError("seller market request ID must be 32 lowercase hexadecimal characters")
        if (
            type(managed_ids) is not frozenset
            or len(managed_ids) > _MAX_SELLER_MANAGED_AUCTION_IDS
            or any(type(auction_id) is not int or auction_id < 1 for auction_id in managed_ids)
        ):
            raise DesktopBackendError("seller managed auction IDs exceed the exact bounded query contract")

        before = self._fresh_guard()
        if not before.value["ahOpen"]:
            raise DesktopBackendError("targeted seller market query requires the auction house to be open")
        self._check_seller_ah_source(before.value)
        boot = before.value["boot"]
        baseline_sequence = before.sample.frame.sequence if before.sample.frame is not None else 0
        item_id = item_key
        self._send_command(f"/cauto market {request_id} {item_id}", require_seller_ah=True)
        for command in _seller_market_id_commands(request_id, managed_ids):
            self._send_command(command, require_seller_ah=True)
        self._send_command(f"/cauto market_start {request_id} {len(managed_ids)}", require_seller_ah=True)

        def completed(value: dict[str, Any]) -> bool:
            result = value["sellerMarket"]
            return result["requestId"] == request_id and (result["complete"] is True or bool(result["error"]))

        captured = self._wait(completed, 20.0)
        value = captured.value
        result = value["sellerMarket"]
        frame = captured.sample.frame
        if (
            frame is None
            or frame.sequence <= baseline_sequence
            or value["boot"] != boot
            or result["requestId"] != request_id
            or result["itemKey"] != item_key
        ):
            raise DesktopBackendError(
                "seller market result does not match the current request and client boot"
            )
        if result["coverage"] not in {"none", "full", "incomplete", "truncated"}:
            raise DesktopBackendError("seller market result has unknown native coverage")
        captured_at = result["capturedAt"]
        if (
            type(captured_at) is not int
            or captured_at > value["wallTime"]
            or value["wallTime"] - captured_at > int(MAX_FRAME_AGE_SECONDS)
        ):
            raise DesktopBackendError("seller market result is stale or has invalid capture time")
        if result["error"]:
            if result["complete"] is not False or result["coverage"] != "none":
                raise DesktopBackendError("seller market error result has contradictory coverage")
        elif result["complete"] is not True or result["coverage"] != "full":
            raise DesktopBackendError("seller market query did not establish complete full coverage")
        managed_count = result["managedAuctionCount"]
        managed_quantity = result["managedQuantity"]
        managed_price = result["managedUnitPriceCopper"]
        if (
            type(managed_count) is not int
            or not 0 <= managed_count <= len(managed_ids)
            or type(managed_quantity) is not int
            or managed_quantity < 0
            or (managed_price is not False and (type(managed_price) is not int or managed_price < 1))
        ):
            raise DesktopBackendError("seller market result has malformed managed-listing aggregates")
        return {
            "boot": value["boot"],
            "sequence": frame.sequence,
            "wallTime": value["wallTime"],
            "sellerMarket": result,
        }

    def stage(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        if not _HEX_DIGEST.fullmatch(batch_id) or not _HEX_DIGEST.fullmatch(action_digest):
            raise DesktopBackendError("batch or action digest is malformed")
        self._expected = (batch_id, ordinal, step_index, action_digest)
        deadline = self.io.monotonic() + STAGE_TIMEOUT_SECONDS
        now = int(self.io.wall_time())
        expires = min(self._deadline_wall or now + 120, now + 120)
        if expires <= now:
            raise DesktopBackendError("authorization expires before native staging")
        prior_operation = self._fresh().value["operation"]
        receipt = prior_operation["receipt"]
        if receipt["sequence"] and any(receipt[key] == "unknown" for key in _RECEIPT_FIELDS):
            raise DesktopBackendError("an unresolved native receipt interlocks staging")
        if prior_operation["batchId"] and prior_operation["batchId"] != batch_id:
            self._send_command("/cauto clear")
        for command in self._stage_commands(action, batch_id, ordinal, step_index, expires):
            self._send_command(command)
        from .model import CollectReturn, Interact, MoveRoute

        while self.io.monotonic() < deadline:
            captured = self._fresh(timeout=COMMAND_TIMEOUT_SECONDS)
            operation = captured.value["operation"]
            if (
                operation["batchId"] == batch_id
                and operation["ordinal"] == ordinal
                and operation["stepIndex"] == step_index
            ):
                actual = operation["action"]
                if actual is False or _action_digest(actual) != action_digest:
                    raise DesktopBackendError("native staged arguments differ from immutable batch intent")
                if operation["error"]:
                    error = operation["error"]
                    if isinstance(action, CollectReturn) and error == "insufficient_return_bag_slots":
                        raise DesktopBackendError(
                            f"native preflight refused operation: {error}",
                            code="insufficient_return_bag_slots",
                        )
                    raise DesktopBackendError(f"native preflight refused operation: {error}")
                if operation["ready"]:
                    if isinstance(action, Interact):
                        profile = self.targets.get(action.target_id)
                        if profile is None or (
                            profile.house,
                            profile.character,
                            profile.client_build,
                            profile.instance_id,
                        ) != (
                            self.config.house,
                            self.config.character,
                            captured.value["context"]["clientBuild"],
                            captured.value["motion"]["instance"],
                        ):
                            raise DesktopBackendError(
                                "interaction profile identity does not match current player"
                            )
                    if isinstance(action, MoveRoute):
                        route = self.routes.get(action.route_id)
                        if route is None:
                            raise DesktopBackendError("route profile is not configured")
                        self._check_route_bindings(
                            captured.value["motion"], route, captured.value["context"], self.config
                        )
                    self._staged_receipt_sequence = operation["receipt"]["sequence"]
                    return
            self.io.sleep(0.05)
        raise DesktopBackendError("native stage did not publish an exact ready action")

    @staticmethod
    def _stage_commands(
        action: object, batch_id: str, ordinal: int, step: int, expires: int
    ) -> tuple[str, ...]:
        from .model import (
            CancelAuction,
            CollectMail,
            CollectReturn,
            Interact,
            MoveRoute,
            PostCommodity,
            PostItem,
        )

        rows = [f"/cauto begin {batch_id} {ordinal} {step}"]
        if isinstance(action, (PostCommodity, PostItem)):
            kind = "post_commodity" if isinstance(action, PostCommodity) else "post_item"
            rows += [f"/cauto kind {kind}"]
            fields = (
                ("quantity", action.quantity),
                ("unitPriceCopper", action.unit_price_copper),
                ("durationHours", action.duration_hours),
                ("maxDepositCopper", action.max_deposit_copper),
            )
            rows.extend(f"/cauto arg {name} {value}" for name, value in fields)
            encoded = action.item_key
            for index in range(0, len(encoded), 60):
                rows.append(f"/cauto key {index // 60 + 1} {encoded[index : index + 60]}")
        elif isinstance(action, CancelAuction):
            rows.extend(
                (
                    "/cauto kind cancel_auction",
                    f"/cauto arg auctionId {action.auction_id}",
                    f"/cauto arg maxCancelCostCopper {action.max_cancel_cost_copper}",
                )
            )
        elif isinstance(action, CollectReturn):
            rows += [
                "/cauto kind collect_return",
                f"/cauto arg quantity {action.quantity}",
                f"/cauto arg mailFingerprint {action.mail_fingerprint}",
            ]
            for index in range(0, len(action.item_key), 60):
                rows.append(f"/cauto key {index // 60 + 1} {action.item_key[index : index + 60]}")
        elif isinstance(action, CollectMail):
            rows.extend(("/cauto kind collect_mail", f"/cauto arg messageLimit {action.message_limit}"))
        elif isinstance(action, Interact):
            rows.extend(("/cauto kind interact", f"/cauto arg targetId {action.target_id}"))
        elif isinstance(action, MoveRoute):
            rows.extend(("/cauto kind move_route", f"/cauto arg routeId {action.route_id}"))
        else:
            raise DesktopBackendError("unsupported action type")
        rows.extend((f"/cauto expires {expires}", "/cauto seal"))
        for row in rows:
            if len(row[1:].encode("ascii")) + 7 > 128:
                raise DesktopBackendError("native command exceeds its 128-byte grammar bound")
        return tuple(rows)

    def _send_command(self, command: str, *, require_seller_ah: bool = False) -> CapturedFrame:
        def checked_fresh() -> CapturedFrame:
            current = self._fresh_guard()
            if require_seller_ah:
                self._check_seller_ah_source(current.value)
            return current

        def command_input_guard() -> None:
            current = self._guard()
            if require_seller_ah:
                self._check_seller_ah_source(current.value)

        captured = checked_fresh()
        chat = captured.value["chatEdit"]
        if chat["active"]:
            if not chat["focused"] or chat["text"]:
                raise DesktopBackendError("existing chat input is not empty and focused")
        else:

            def open_chat_guard() -> None:
                guarded = checked_fresh()
                current = guarded.value["chatEdit"]
                if current["active"] or current["focused"] or current["text"]:
                    raise DesktopBackendError("chat state changed before opening command input")

            self.io.press("Return", open_chat_guard)
            captured = self._wait(
                lambda value: value["chatEdit"]["active"] and value["chatEdit"]["focused"], 3.0
            )
            if captured.value["chatEdit"]["text"]:
                raise DesktopBackendError("chat opened with unexpected text")

        self.io.type_text(command, command_input_guard)
        typed = self._wait(
            lambda value: (
                value["chatEdit"]["active"]
                and value["chatEdit"]["focused"]
                and value["chatEdit"]["text"] == command
            ),
            3.0,
        )
        if typed.value["chatEdit"]["text"] != command:
            raise DesktopBackendError("exact command text was not published")

        def submit_guard() -> None:
            guarded = checked_fresh()
            current = guarded.value["chatEdit"]
            if not current["active"] or not current["focused"] or current["text"] != command:
                raise DesktopBackendError("exact focused chat command changed before Return")

        self.io.press("Return", submit_guard)
        return self._wait(lambda value: value["chatEdit"]["active"] is False, 5.0)

    def _wait(self, predicate: Callable[[dict[str, Any]], bool], timeout: float) -> CapturedFrame:
        deadline = self.io.monotonic() + timeout
        while self.io.monotonic() < deadline:
            captured = self._fresh_guard()
            if predicate(captured.value):
                return captured
            self.io.sleep(0.05)
        raise DesktopBackendError("fresh native publication did not reach the requested state")

    def dispatch(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
        admission: Admission,
    ) -> DispatchReceipt:
        captured = self._guard()
        operation = captured.value["operation"]
        if (
            operation["batchId"] != batch_id
            or operation["ordinal"] != ordinal
            or operation["stepIndex"] != step_index
            or operation["ready"] is not True
            or operation["action"] is False
            or _action_digest(operation["action"]) != action_digest
            or admission.session_boot_id != captured.value["boot"]
        ):
            raise DesktopBackendError("last-moment native staging or session identity changed")
        from .model import (
            CancelAuction,
            CollectMail,
            CollectReturn,
            Interact,
            MoveRoute,
            PostCommodity,
            PostItem,
        )

        if isinstance(action, (PostCommodity, PostItem, CancelAuction, CollectMail, CollectReturn)):
            return self._click_native(action, batch_id, ordinal, step_index, action_digest, captured)
        if isinstance(action, Interact):
            return self._interact(action, batch_id, ordinal, step_index, action_digest, captured)
        if isinstance(action, MoveRoute):
            return self._move(action, batch_id, ordinal, step_index, action_digest)
        raise DesktopBackendError("unsupported action type")

    def _click_native(
        self, action: object, batch_id: str, ordinal: int, step: int, digest: str, staged: CapturedFrame
    ) -> DispatchReceipt:
        from .model import CollectMail, CollectReturn

        value = staged.value
        rect = value["operation"]["buttonRect"]
        bounds = Rect(rect["x"], rect["y"], rect["width"], rect["height"])
        return self._click(
            bounds,
            staged,
            batch_id=batch_id,
            ordinal=ordinal,
            step=step,
            digest=digest,
            required_ui="mail" if isinstance(action, (CollectMail, CollectReturn)) else "ah",
            requires_ah=not isinstance(action, (CollectMail, CollectReturn)),
        )

    def _click(
        self,
        bounds: Rect,
        staged: CapturedFrame,
        *,
        batch_id: str,
        ordinal: int,
        step: int,
        digest: str,
        mailbox: TargetProfile | None = None,
        required_ui: str | None = None,
        requires_ah: bool = False,
    ) -> DispatchReceipt:
        if staged.window.xid is None:
            raise DesktopBackendError("owned game window id is unavailable")
        frame = staged.sample.frame
        if frame is None:
            raise DesktopBackendError("staged action has no validated native frame")
        width, height = staged.window.display
        screen = Rect(0, 0, width, height)
        binding = BindingMetadata(
            f"{self.config.character}:{self.config.realm}:{self.config.build}",
            f"automation-{frame.sequence}",
            max(1, frame.sequence),
            1,
            staged.window.xid,
            (staged.window.xid,),
            source="native-operation",
            boot=frame.boot,
        )
        target = PointerTarget("automation-action", bounds, binding, padding=1)
        deadline = self._click_deadline()
        precommit_ns = press_ns = 0
        with self.io.permit(deadline, self._input_guard) as permit:
            self._guard()
            x, y, _start_window = self.io.pointer_position()
            plan = plan_human_pointer(Point(x, y), target, screen, button=PointerButton.LEFT)
            if plan.click is None:
                raise DesktopBackendError("pointer plan has no left click")
            endpoint = (int(plan.endpoint.x), int(plan.endpoint.y))
            first = plan.endpoint if plan.trajectory is None else plan.trajectory.samples[0].point
            if (x, y) != (int(first.x), int(first.y)):
                raise DesktopBackendError("pointer changed before its planned path")
            chain = (
                None
                if plan.trajectory is None
                else build_pointer_motion_chain(plan.trajectory, (0, 0, width, height))
            )
            self._write_evidence(
                batch_id, ordinal, step, plan.seed, (x, y), endpoint, plan.movement_duration, 0, 0
            )
            if chain is not None:
                self.io.emit(chain[1:], plan.movement_duration + COMMAND_TIMEOUT_SECONDS)
            self._guard()
            end_x, end_y, end_window = self.io.pointer_position()
            if (end_x, end_y) != endpoint or not bounds.contains(Point(end_x, end_y)):
                raise DesktopBackendError("pointer endpoint is not inside the staged target")
            if not self.io.window_contains(end_window, staged.window.xid):
                raise DesktopBackendError("pointer endpoint is outside the owned game window")
            current = self._fresh()
            if mailbox is not None:
                self._check_mailbox_hover(current, mailbox, endpoint)
            self._check_staged_click(
                current, digest, batch_id, ordinal, step, staged, required_ui, requires_ah
            )
            if mailbox is not None and current.value["mailOpen"]:
                raise DesktopBackendError(
                    "mailbox interaction requires the mail UI to be closed before input"
                )
            guarded = self._guard()
            position = self.io.pointer_position()
            if position[:2] != endpoint:
                raise DesktopBackendError("pointer moved away from its verified endpoint before press")
            if not self.io.window_contains(position[2], staged.window.xid):
                raise DesktopBackendError("pointer left the owned game window before press")
            if mailbox is not None:
                self._check_mailbox_hover(guarded, mailbox, endpoint)
                if guarded.value["mailOpen"]:
                    raise DesktopBackendError(
                        "mailbox interaction requires the mail UI to be closed before input"
                    )
            self._check_staged_click(
                guarded, digest, batch_id, ordinal, step, staged, required_ui, requires_ah
            )
            precommit_ns = permit.precommit(deadline_monotonic=deadline)
            press_attempted = False
            pressed = False
            try:
                final_gate = self._fresh_guard()
                position = self.io.pointer_position()
                if position[:2] != endpoint or not self.io.window_contains(position[2], staged.window.xid):
                    raise DesktopBackendError("pointer endpoint or window changed after click precommit")
                if mailbox is not None:
                    self._check_mailbox_hover(final_gate, mailbox, endpoint)
                    if final_gate.value["mailOpen"]:
                        raise DesktopBackendError(
                            "mailbox interaction requires the mail UI to be closed before input"
                        )
                self._check_staged_click(
                    final_gate, digest, batch_id, ordinal, step, staged, required_ui, requires_ah
                )
                final_gate = self._fresh_guard()
                self._check_staged_click(
                    final_gate, digest, batch_id, ordinal, step, staged, required_ui, requires_ah
                )
                if mailbox is not None:
                    self._check_mailbox_hover(final_gate, mailbox, endpoint)
                    if final_gate.value["mailOpen"]:
                        raise DesktopBackendError(
                            "mailbox interaction requires the mail UI to be closed before input"
                        )
                position = self.io.pointer_position()
                if position[:2] != endpoint or not self.io.window_contains(position[2], staged.window.xid):
                    raise DesktopBackendError("pointer endpoint or window changed immediately before press")
                self._check_controls()
                press_attempted = True
                self.io.emit(
                    ["mousedown", "1", "sleep", f"{plan.click.dwell:.9f}", "mouseup", "1"],
                    plan.click.dwell + COMMAND_TIMEOUT_SECONDS,
                )
                pressed = True
                press_ns = permit.record_press()
            finally:
                if press_attempted and not pressed:
                    try:
                        self.io.emit(["mouseup", "1"], COMMAND_TIMEOUT_SECONDS)
                    except Exception:
                        pass
        try:
            self._write_evidence(
                batch_id,
                ordinal,
                step,
                plan.seed,
                (x, y),
                endpoint,
                plan.movement_duration,
                precommit_ns,
                press_ns,
            )
            self.evidence_state = "written"
        except Exception:
            self.evidence_state = "gesture_evidence_write_failed"
        if mailbox is not None:
            final = self._wait(lambda frame: frame["mailOpen"], RECEIPT_TIMEOUT_SECONDS)
            result = self._local_receipt(
                batch_id, ordinal, step, digest, final, True, True, True, "mail_ui_open"
            )
        else:
            result = self._native_receipt(batch_id, ordinal, step, digest, self._staged_receipt_sequence)
        if mailbox is not None:
            try:
                self._write_receipt_evidence(batch_id, ordinal, step, result.object())
            except Exception:
                self.evidence_state = "receipt_evidence_write_failed"
        return result

    def _check_staged_click(
        self,
        captured: CapturedFrame,
        digest: str,
        batch: str,
        ordinal: int,
        step: int,
        staged: CapturedFrame,
        required_ui: str | None,
        requires_ah: bool,
    ) -> None:
        value = captured.value
        operation = value["operation"]
        if (
            operation["batchId"] != batch
            or operation["ordinal"] != ordinal
            or operation["stepIndex"] != step
            or operation["ready"] is not True
            or operation["expiresAt"] <= self.io.wall_time()
            or operation["action"] is False
            or _action_digest(operation["action"]) != digest
            or (
                required_ui in {"ah", "mail"}
                and operation["buttonRect"] != staged.value["operation"]["buttonRect"]
            )
            or (required_ui == "ah" and not value["ahOpen"])
            or (required_ui == "mail" and not value["mailOpen"])
            or (requires_ah and value["ownership"]["ahOwner"] != "automation")
        ):
            raise DesktopBackendError("staged action, UI, ownership, or target changed before press")
        if required_ui == "ah" and self._seller_ah_target_id is not None:
            self._check_seller_ah_source(value)

    def _check_mailbox_hover(
        self, captured: CapturedFrame, profile: TargetProfile, endpoint: tuple[int, int]
    ) -> None:
        motion = captured.value["motion"]
        hover = motion["hover"]
        if (
            hover["kind"] != "object"
            or hover["name"] != profile.name
            or (hover["screenX"], hover["screenY"]) != endpoint
        ):
            raise DesktopBackendError("fresh native hover evidence does not identify the measured mailbox")
        if (profile.house, profile.character, profile.client_build, profile.instance_id) != (
            self.config.house,
            self.config.character,
            captured.value["context"]["clientBuild"],
            motion["instance"],
        ):
            raise DesktopBackendError("mailbox profile identity changed")
        assert (
            profile.anchor_x is not None and profile.anchor_y is not None and profile.max_distance is not None
        )
        if (
            (motion["x"] - profile.anchor_x) ** 2 + (motion["y"] - profile.anchor_y) ** 2
        ) ** 0.5 > profile.max_distance:
            raise DesktopBackendError("player is outside the measured mailbox anchor radius")

    def _native_receipt(
        self, batch: str, ordinal: int, step: int, digest: str, baseline: int
    ) -> DispatchReceipt:
        deadline = self.io.monotonic() + RECEIPT_TIMEOUT_SECONDS
        latest: CapturedFrame | None = None
        while self.io.monotonic() < deadline:
            latest = self._fresh(timeout=COMMAND_TIMEOUT_SECONDS)
            receipt = latest.value["operation"]["receipt"]
            if (
                receipt["sequence"] > baseline
                and receipt["batchId"] == batch
                and receipt["ordinal"] == ordinal
                and receipt["stepIndex"] == step
                and all(receipt[key] != "unknown" for key in _RECEIPT_FIELDS)
            ):
                try:
                    self._write_receipt_evidence(
                        batch,
                        ordinal,
                        step,
                        {"boot": latest.value["boot"], **latest.value["operation"]["receipt"]},
                    )
                    self.evidence_state = "written"
                except Exception:
                    self.evidence_state = "receipt_evidence_write_failed"
                result = DispatchReceipt(
                    batch,
                    ordinal,
                    step,
                    digest,
                    latest.value["boot"],
                    receipt["callbackDelivered"],
                    receipt["postcondition"],
                    receipt["actionComplete"],
                    receipt["detail"],
                    _structured_receipt_result(receipt.get("result", False)),
                )
                self._expected = None
                return result
            self.io.sleep(0.05)
        if latest is not None:
            try:
                self._write_receipt_evidence(
                    batch,
                    ordinal,
                    step,
                    {"boot": latest.value["boot"], **latest.value["operation"]["receipt"]},
                )
                self.evidence_state = "written"
            except Exception:
                self.evidence_state = "receipt_evidence_write_failed"
        # A press happened, but no causal native receipt was observed. Never report success.
        return DispatchReceipt(
            batch, ordinal, step, digest, cast(str, self._boot), None, None, None, "receipt_timeout_unknown"
        )

    def _interact(
        self, action: object, batch: str, ordinal: int, step: int, digest: str, staged: CapturedFrame
    ) -> DispatchReceipt:
        profile = self.targets.get(action.target_id)  # type: ignore[attr-defined]
        if profile is None:
            raise DesktopBackendError("interaction target profile is not configured")
        if (profile.house, profile.character, profile.client_build) != (
            self.config.house,
            self.config.character,
            self.config.build.rsplit(".", 1)[1],
        ):
            raise DesktopBackendError("interaction target profile identity does not match config")
        if profile.kind == "mailbox":
            assert profile.rect is not None
            return self._click(
                Rect(*profile.rect),
                staged,
                batch_id=batch,
                ordinal=ordinal,
                step=step,
                digest=digest,
                mailbox=profile,
            )
        if profile.expected_ui != "ah":
            raise DesktopBackendError("NPC target profile must expect auction-house UI")
        self._send_command(f"/targetexact {profile.name}")
        expected = "ahOpen" if profile.expected_ui == "ah" else "mailOpen"
        self._wait(
            lambda value: (
                value["motion"]["target"]["present"]
                and value["motion"]["target"]["guid"] == profile.guid
                and value["motion"]["target"]["name"] == profile.name
                and value["motion"]["target"]["rangeAvailable"]
                and value["motion"]["target"]["withinRange"]
            ),
            8.0,
        )

        def interact_guard() -> None:
            current = self._fresh_guard().value
            target = current["motion"]["target"]
            facts = current["auctioneer"]
            if current[expected]:
                raise DesktopBackendError("interaction UI was already open before the interaction input")
            if (
                (profile.house, profile.character, profile.client_build, profile.instance_id)
                != (
                    self.config.house,
                    self.config.character,
                    current["context"]["clientBuild"],
                    current["motion"]["instance"],
                )
                or not target["present"]
                or target["guid"] != profile.guid
                or target["name"] != profile.name
                or not target["rangeAvailable"]
                or not target["withinRange"]
                or facts["targetName"] != profile.name
                or not facts["targetPresent"]
                or not facts["rangeAvailable"]
                or not facts["withinTradeRange"]
                or not facts["bindingAvailable"]
                or facts["ctrlF12Action"] != "INTERACTTARGET"
                or facts["interactionMode"] not in {"keyboard", "any"}
            ):
                raise DesktopBackendError("native target, range, identity, or INTERACTTARGET binding changed")

        interact_guard()
        self.io.chord(INTERACT_CHORD, interact_guard)
        final = self._wait(lambda value: value[expected], 10.0)
        return self._local_receipt(
            batch, ordinal, step, digest, final, True, True, True, "interaction_ui_open"
        )

    def _move(self, action: object, batch: str, ordinal: int, step: int, digest: str) -> DispatchReceipt:
        route = self.routes.get(action.route_id)  # type: ignore[attr-defined]
        if route is None:
            raise DesktopBackendError("route profile is not configured")

        continuous_io = (
            self.io if isinstance(self.io, DesktopIO) and self.config.input_backend == "uinput" else None
        )

        def sample() -> Motion:
            captured = self._fresh_guard()
            value = captured.value
            motion, context = value["motion"], value["context"]
            self._check_route_bindings(motion, route, context, self.config)
            return Motion(
                value["boot"],
                captured.sample.capture_monotonic,
                motion["x"],
                motion["y"],
                motion["facing"],
                motion["instance"],
                context["clientBuild"],
                self.config.house,
                context["character"],
                motion["moving"],
                motion["dead"],
                motion["inCombat"],
                motion["mounted"],
            )

        def hold(keys: tuple[str, ...], seconds: float) -> None:
            if len(keys) != 1 or keys[0] not in {"Up", "Left", "Right"}:
                raise DesktopBackendError("movement controller selected an unsupported key")
            if continuous_io is not None:
                continuous_io.release_movement()

            def hold_guard() -> None:
                captured = self._fresh_guard()
                self._check_route_bindings(
                    captured.value["motion"], route, captured.value["context"], self.config
                )

            hold_guard()
            self.io.hold(keys[0], seconds, hold_guard)

        def forward(travel_deadline: float, observed_at: float) -> None:
            assert continuous_io is not None
            continuous_io.forward(travel_deadline, observed_at)
            self.io.sleep(max(0.0, min(0.1, travel_deadline - self.io.monotonic())))

        try:
            result: WaypointResult = walk_waypoint(
                route,
                step,
                sample=sample,
                hold=hold,
                monotonic=self.io.monotonic,
                stop=self.stop_requested,
                continuous_forward=forward if continuous_io is not None else None,
                release_forward=continuous_io.release_movement if continuous_io is not None else None,
            )
        finally:
            if continuous_io is not None:
                continuous_io.release_movement()
        final = self._fresh_guard()
        final_motion, final_context = final.value["motion"], final.value["context"]
        self._check_route_bindings(final_motion, route, final_context, self.config)
        target_x, target_y = route.waypoints[step]
        still_reached = (
            result.reached
            and ((final_motion["x"] - target_x) ** 2 + (final_motion["y"] - target_y) ** 2) ** 0.5
            <= route.tolerance
        )
        detail = result.reason if still_reached or not result.reached else "final_position_changed"
        return self._local_receipt(
            batch,
            ordinal,
            step,
            digest,
            final,
            True,
            still_reached,
            still_reached and step == len(route.waypoints) - 1,
            detail,
        )

    @staticmethod
    def _check_route_bindings(
        motion: dict[str, Any], route: RouteProfile, context: dict[str, Any], config: AutomationConfig
    ) -> None:
        bindings = motion["bindings"]
        if (bindings["forward"], bindings["left"], bindings["right"]) != (
            "MOVEFORWARD",
            "TURNLEFT",
            "TURNRIGHT",
        ):
            raise DesktopBackendError("native arrow bindings do not match movement controls")
        if (
            route.house != config.house
            or route.character != config.character
            or route.client_build != context["clientBuild"]
            or motion["instance"] != route.instance_id
            or motion["dead"]
            or motion["inCombat"]
            or motion["mounted"]
        ):
            raise DesktopBackendError("movement identity or safety state changed")

    def _local_receipt(
        self,
        batch: str,
        ordinal: int,
        step: int,
        digest: str,
        captured: CapturedFrame,
        callback: bool,
        postcondition: bool,
        complete: bool,
        detail: str,
    ) -> DispatchReceipt:
        if type(callback) is bool and type(postcondition) is bool and type(complete) is bool:
            self._expected = None
        return DispatchReceipt(
            batch, ordinal, step, digest, captured.value["boot"], callback, postcondition, complete, detail
        )

    def _click_deadline(self) -> float:
        if not self._deadline_wall:
            return self.io.monotonic() + RECEIPT_TIMEOUT_SECONDS
        return self.io.monotonic() + max(0.001, self._deadline_wall - self.io.wall_time())

    def _write_evidence(
        self,
        batch: str,
        ordinal: int,
        step: int,
        seed: int,
        start: tuple[int, int],
        end: tuple[int, int],
        duration: float,
        precommit_ns: int,
        press_ns: int,
    ) -> None:
        dirs = XdgDirs.from_environ(os.environ)
        directory = dirs.crowley_state / "automation" / "evidence"
        ensure_private_dir(directory)
        path = directory / f"{batch}-{ordinal}-{step}.json"
        data = json.dumps(
            {
                "schema": "crowley.automation-gesture.v1",
                "batchId": batch,
                "ordinal": ordinal,
                "stepIndex": step,
                "seed": seed,
                "start": list(start),
                "endpoint": list(end),
                "duration": round(duration, 6),
                "precommitMonotonicNs": precommit_ns,
                "pressMonotonicNs": press_ns,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        atomic_write_private(path, data.encode("utf-8"))

    def _write_receipt_evidence(self, batch: str, ordinal: int, step: int, receipt: dict[str, Any]) -> None:
        dirs = XdgDirs.from_environ(os.environ)
        directory = dirs.crowley_state / "automation" / "evidence"
        ensure_private_dir(directory)
        path = directory / f"{batch}-{ordinal}-{step}-receipt.json"
        atomic_write_private(
            path,
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def inspect(self, kind: str, page: int) -> dict[str, Any]:
        if kind not in {"inventory", "owned", "mail"} or type(page) is not int or not 0 <= page <= 4095:
            raise DesktopBackendError("inspection kind or page is invalid")
        before = self._fresh_guard()
        if not self._deadline_wall:
            # Read-only preparation (including initial history) precedes this
            # bounded command deadline. Existing authorizations never renew.
            self.set_deadline(int(self.io.wall_time()) + 120)
        before_frame = before.sample.frame
        if before_frame is None:
            raise DesktopBackendError("native inspection baseline has no frame sequence")
        baseline_sequence = before_frame.sequence
        baseline_request_id = before.value["inspection"]["requestId"]
        command = f"/cauto inspect {kind} {page}"
        captured = self._send_command(command)
        deadline = self.io.monotonic() + 8.0
        while self.io.monotonic() < deadline:
            captured = self._fresh()
            inspection = captured.value["inspection"]
            frame = captured.sample.frame
            if (
                frame is not None
                and frame.sequence > baseline_sequence
                and inspection["kind"] == kind
                and inspection["page"] == page
                and (page != 0 or inspection["requestId"] != baseline_request_id)
            ):
                snapshot = (
                    inspection["requestId"],
                    inspection["capturedAt"],
                    inspection["sourceCount"],
                    captured.value["boot"],
                )
                prior_snapshot = self._inspection_snapshots.get(kind)
                if page > 0 and prior_snapshot is not None and snapshot != prior_snapshot:
                    raise DesktopBackendError("native inspection page changed its frozen snapshot identity")
                if page == 0:
                    self._inspection_snapshots[kind] = snapshot
                return {
                    "boot": captured.value["boot"],
                    "capturedAt": inspection["capturedAt"],
                    "kind": kind,
                    "page": page,
                    "rows": inspection["rows"],
                    "hasMore": inspection["hasMore"],
                    "error": inspection["error"],
                    "requestId": inspection["requestId"],
                    "sequence": frame.sequence,
                    "sourceCount": inspection["sourceCount"],
                    "wallTime": captured.value["wallTime"],
                }
            self.io.sleep(0.05)
        raise DesktopBackendError("a fresh native inspection page was not published for this request")

    def clear_stage(self) -> None:
        try:
            current = self._fresh().value
            receipt = current["operation"]["receipt"]
            if receipt["sequence"] and any(receipt[key] == "unknown" for key in _RECEIPT_FIELDS):
                self.cleanup_state = "refused_unresolved_native_effect"
                return
            cleared = self._send_command("/cauto clear").value["operation"]
            if cleared["batchId"] or cleared["action"] is not False:
                self.cleanup_state = "clear_not_confirmed"
                return
            self._expected = None
            self.cleanup_state = "cleared"
        except Exception:
            self.cleanup_state = "clear_not_confirmed"
            raise

    def finish_batch(self) -> None:
        """Explicitly release a known-complete native stage while lease is held."""
        if self.stop_requested():
            self.cleanup_state = "skipped_durable_stop"
            return
        try:
            current = self._fresh().value
            receipt = current["operation"]["receipt"]
            if receipt["sequence"] and any(receipt[key] == "unknown" for key in _RECEIPT_FIELDS):
                self.cleanup_state = "refused_unresolved_native_effect"
                return
            if current["ownership"]["ahOwner"] == "automation" or current["operation"]["batchId"]:
                cleared = self._send_command("/cauto clear").value["operation"]
                if cleared["batchId"] or cleared["action"] is not False:
                    self.cleanup_state = "release_not_confirmed"
                    return
                self._expected = None
                self.cleanup_state = "released"
            else:
                self.cleanup_state = "already_released"
        except Exception:
            # A known effect remains known; failed cleanup is reported separately.
            self.cleanup_state = "release_not_confirmed"

    def close(self) -> None:
        self.io.close()


def _action_digest(value: object) -> str:
    from .model import canonical_json

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _structured_receipt_result(value: object) -> dict[str, object] | None:
    """Translate the native false sentinel to Python's explicit unknown value."""
    if value is False:
        return None
    if type(value) is not dict:
        raise DesktopBackendError("native structured receipt result is malformed")
    result = dict(value)
    for field in ("depositCopper", "cancelCostCopper"):
        if result.get(field, None) is False:
            result[field] = None
        elif field in result and result[field] is not None and type(result[field]) is not int:
            raise DesktopBackendError(f"native receipt {field} is not an exact integer or unknown")
    return result


def _seller_market_id_commands(request_id: str, managed_ids: frozenset[int]) -> tuple[str, ...]:
    """Chunk every managed ID by the native command's actual encoded-byte limit."""
    if type(request_id) is not str or _SELLER_REQUEST_ID.fullmatch(request_id) is None:
        raise DesktopBackendError("seller market request ID is malformed")
    if type(managed_ids) is not frozenset or len(managed_ids) > _MAX_SELLER_MANAGED_AUCTION_IDS:
        raise DesktopBackendError("seller managed auction IDs exceed the bounded query contract")
    if any(type(auction_id) is not int or auction_id < 1 for auction_id in managed_ids):
        raise DesktopBackendError("seller managed auction IDs must be positive exact integers")
    chunks: list[str] = []
    current: list[int] = []
    chunk_index = 1
    for auction_id in sorted(managed_ids):
        candidate = [*current, auction_id]
        command = f"/cauto market_ids {request_id} {chunk_index} {','.join(map(str, candidate))}"
        if len(command[1:].encode("ascii")) + 7 > 128:
            if not current:
                raise DesktopBackendError("one seller managed auction ID exceeds the native command bound")
            chunks.append(f"/cauto market_ids {request_id} {chunk_index} {','.join(map(str, current))}")
            chunk_index += 1
            current = [auction_id]
            command = f"/cauto market_ids {request_id} {chunk_index} {auction_id}"
            if len(command[1:].encode("ascii")) + 7 > 128:
                raise DesktopBackendError("one seller managed auction ID exceeds the native command bound")
        else:
            current = candidate
    if current:
        chunks.append(f"/cauto market_ids {request_id} {chunk_index} {','.join(map(str, current))}")
    return tuple(chunks)


__all__ = ["BackendIO", "CapturedFrame", "DesktopBackend", "DesktopBackendError", "DesktopIO"]
