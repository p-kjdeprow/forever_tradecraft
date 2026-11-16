"""One guarded desktop adapter for the schema-2 local operation contract."""

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
from .gui.click_gate import ClickRatePermit, acquire_click_rate_permit
from .gui.input_tool import PinnedInputTool
from .gui.keyboard import char_transitions, key_argv, transition_argv
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
_RECEIPT_FIELDS = ("callbackDelivered", "postcondition", "actionComplete")


class DesktopBackendError(RuntimeError):
    """Desktop state or a protected input did not satisfy the operation contract."""


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

    def _ensure_input(self) -> tuple[PinnedInputTool, XtestKeys]:
        if self._tool is None:
            self._tool = PinnedInputTool.load()
            self._keys = XtestKeys(self._tool, self._env, run=self._runner)
        assert self._keys is not None
        return self._tool, self._keys

    def capture(self, *, timeout: float = 5.0) -> CapturedFrame:
        deadline = time.monotonic() + timeout
        last_reason = "automation publication has not advanced"
        while time.monotonic() < deadline:
            began = time.monotonic()
            pids = tuple(self._process.pids())
            if not pids:
                raise DesktopBackendError("no owned game process")
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
            active, reason = self._presence.human_active()
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
        active, reason = self._presence.human_active()
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
        tool, _keys = self._ensure_input()
        try:
            done = self._runner(
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
        # Every character is its own guarded XTEST transition.
        keymap = self._keymap()
        for char in text:
            argv, held = transition_argv(char_transitions(char), keymap)
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

    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None:
        _tool, keys = self._ensure_input()
        keys.hold(key, seconds, guard)

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
        if self._tool is not None:
            self._tool.close()
            self._tool = None
            self._keys = None


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
        self.preflight_gold: int | None = None
        self.cleanup_state = "not_needed"
        self.evidence_state = "not_written"

    def set_deadline(self, expires_at: int) -> None:
        self._deadline_wall = expires_at

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
        )

    def observe_native(self) -> dict[str, Any]:
        """Return one fresh, identity-checked schema-2 observation for read-only CLI output."""
        return self._fresh().value

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
        from .model import Interact, MoveRoute

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
                    raise DesktopBackendError(f"native preflight refused operation: {operation['error']}")
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
        from .model import CancelAuction, CollectMail, Interact, MoveRoute, PostCommodity, PostItem

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

    def _send_command(self, command: str) -> CapturedFrame:
        captured = self._fresh_guard()
        chat = captured.value["chatEdit"]
        if chat["active"]:
            if not chat["focused"] or chat["text"]:
                raise DesktopBackendError("existing chat input is not empty and focused")
        else:

            def open_chat_guard() -> None:
                current = self._fresh_guard().value["chatEdit"]
                if current["active"] or current["focused"] or current["text"]:
                    raise DesktopBackendError("chat state changed before opening command input")

            self.io.press("Return", open_chat_guard)
            captured = self._wait(
                lambda value: value["chatEdit"]["active"] and value["chatEdit"]["focused"], 3.0
            )
            if captured.value["chatEdit"]["text"]:
                raise DesktopBackendError("chat opened with unexpected text")

        self.io.type_text(command, self._input_guard)
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
            current = self._fresh_guard().value["chatEdit"]
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
        from .model import CancelAuction, CollectMail, Interact, MoveRoute, PostCommodity, PostItem

        if isinstance(action, (PostCommodity, PostItem, CancelAuction, CollectMail)):
            return self._click_native(action, batch_id, ordinal, step_index, action_digest, captured)
        if isinstance(action, Interact):
            return self._interact(action, batch_id, ordinal, step_index, action_digest, captured)
        if isinstance(action, MoveRoute):
            return self._move(action, batch_id, ordinal, step_index, action_digest)
        raise DesktopBackendError("unsupported action type")

    def _click_native(
        self, action: object, batch_id: str, ordinal: int, step: int, digest: str, staged: CapturedFrame
    ) -> DispatchReceipt:
        from .model import CollectMail

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
            required_ui="mail" if isinstance(action, CollectMail) else "ah",
            requires_ah=not isinstance(action, CollectMail),
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
            first = plan.trajectory.samples[0].point
            if (x, y) != (int(first.x), int(first.y)):
                raise DesktopBackendError("pointer changed before its planned path")
            chain = build_pointer_motion_chain(plan.trajectory, (0, 0, width, height))
            self._write_evidence(
                batch_id, ordinal, step, plan.seed, (x, y), endpoint, plan.movement_duration, 0, 0
            )
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

            def hold_guard() -> None:
                captured = self._fresh_guard()
                self._check_route_bindings(
                    captured.value["motion"], route, captured.value["context"], self.config
                )

            hold_guard()
            self.io.hold(keys[0], seconds, hold_guard)

        result: WaypointResult = walk_waypoint(
            route,
            step,
            sample=sample,
            hold=hold,
            monotonic=self.io.monotonic,
            stop=self.stop_requested,
        )
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
        command = f"/cauto inspect {kind} {page}"
        captured = self._send_command(command)
        deadline = self.io.monotonic() + 8.0
        while self.io.monotonic() < deadline:
            captured = self._fresh()
            inspection = captured.value["inspection"]
            if inspection["kind"] == kind and inspection["page"] == page:
                return {
                    "kind": kind,
                    "page": page,
                    "rows": inspection["rows"],
                    "hasMore": inspection["hasMore"],
                    "error": inspection["error"],
                }
            self.io.sleep(0.05)
        raise DesktopBackendError("native inspection page was not published")

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


__all__ = ["BackendIO", "CapturedFrame", "DesktopBackend", "DesktopBackendError", "DesktopIO"]
