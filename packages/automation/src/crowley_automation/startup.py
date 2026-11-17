"""One bounded observation-driven desktop startup and recovery episode."""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crowley_common.platform.paths import XdgDirs
from PIL import Image

from crowley_automation.battle_net import (
    BattleNetPlayError,
    Box,
    click_control,
    locate_play_button,
)
from crowley_automation.config import AutomationConfig
from crowley_automation.desktop_input import ActError, GuiLease, gui_lock_path
from crowley_automation.desktop_observe import (
    NativeWorldObservation,
    ObserveError,
    ScreenCanvas,
    WindowState,
    X11Window,
    observe_native_world,
    x11_runner,
)
from crowley_automation.desktop_session import (
    DesktopSession,
    DesktopSessionProvider,
    DesktopSessionUnavailable,
)
from crowley_automation.gui.capture import CaptureError
from crowley_automation.gui.control_child import (
    ControlChildCancelled,
    ControlChildError,
    ControlChildRunner,
    PinnedControlTool,
)
from crowley_automation.gui.input_tool import PinnedInputTool
from crowley_automation.gui.process import OwnedGameMatcher, ProcessIdentity, process_start_ticks
from crowley_automation.presence import Presence
from crowley_automation.runtime import maintenance_blocker
from crowley_automation.startup_ui import (
    OCRDocument,
    StartupUIError,
    TesseractReader,
    UIEvidence,
    UIKind,
    classify_battle_net_ui,
    classify_battle_net_updates_capture,
    classify_character_select_capture,
)

StartupOutcome = Literal["ready", "waiting", "held", "blocked", "failed", "stopped"]
_EPISODE_SECONDS = 600.0
_LOGIN_SECONDS = 180.0
_WINDOW_SECONDS = 30.0
_PLAY_WAIT_SECONDS = 20.0
_WORLD_SECONDS = 300.0
_CLOSE_SECONDS = 30.0
_REPAIR_RETRY_SECONDS = 1.0
_RETRY_SECONDS = 1.0
_RESTART_REASONS = frozenset({"ah_open_failed", "no_progress", "no_frame_after_running"})
_SAFE_REOBSERVATION_REFUSALS = frozenset(
    {
        "battle_net_login_required",
        "battle_net_account_conflict",
        "battle_net_ui_unrecognized",
        "client_build_info_unavailable",
        "client_build_info_malformed",
        "client_build_active_product_missing",
        "client_build_active_product_ambiguous",
        "client_build_mismatch",
    }
)
_BATTLE_NET_WINDOWS_CLASS = "battle.net.exe"
_BATTLE_NET_WINDOW_TITLE = "Battle.net"
_BATTLE_NET_UPDATES_TITLE = "Battle.net Updates"
_PRODUCT_UI_TITLES = {"wowf": "WoW: Forever"}
_BATTLE_NET_CONFIG_LIMIT = 2 * 1024 * 1024
_BUILD_INFO_LIMIT = 1024 * 1024
_LOG = logging.getLogger(__name__)


def _nonblocking_gui_lease(path: Path) -> GuiLease:
    return GuiLease(path, timeout=0.0)


@dataclass(frozen=True, slots=True)
class StartupStatus:
    outcome: StartupOutcome
    phase: str
    reason: str
    action: str
    started_at: float
    deadline_at: float | None
    retry_at: float | None
    native_ready: bool
    process_identity: ProcessIdentity | None
    window_xid: int | None


@dataclass(frozen=True, slots=True)
class _ScreenObservation:
    identity: ProcessIdentity
    window: WindowState
    image: object
    ocr: OCRDocument
    reader: TesseractReader
    session_fingerprint: str


class _HoldEpisode(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BlockEpisode(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StartupRecovery:
    """Own one nonblocking, bounded GUI-lease episode and reobserve every transition."""

    def __init__(
        self,
        config: AutomationConfig,
        *,
        stopping: Callable[[], bool],
        session_provider: DesktopSessionProvider | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        popen: Callable[..., object] = subprocess.Popen,
        lease_factory: Callable[[Path], GuiLease] = _nonblocking_gui_lease,
        input_tool_factory: Callable[[], PinnedInputTool] = PinnedInputTool.load,
        control_tool_factory: Callable[..., PinnedControlTool] = PinnedControlTool.load,
        reader_factory: Callable[[Mapping[str, str]], TesseractReader] | None = None,
    ) -> None:
        self.config = config
        self._stopping = stopping
        self.session_provider = session_provider or DesktopSessionProvider(config.display)
        self._clock = monotonic
        self._wall = wall_clock
        self._runner = runner
        self._popen = popen
        self._lease = lease_factory(gui_lock_path())
        self._lease_context = None
        self._active = False
        self._input_tool_factory = input_tool_factory
        self._control_tool_factory = control_tool_factory
        self._reader_factory = reader_factory or (
            lambda environment: TesseractReader(environment=dict(environment))
        )
        self._tool: PinnedInputTool | None = None
        self._control: ControlChildRunner | None = None
        self._control_tools: dict[str, PinnedControlTool] = {}
        self._started_mono: float | None = None
        self._started_wall = 0.0
        self._episode_deadline = 0.0
        self._episode_remaining: float | None = None
        self._session_fingerprint: str | None = None
        self._retry_mono: float | None = None
        self._last_status: StartupStatus | None = None
        self._terminal_status: StartupStatus | None = None
        self._last_native: NativeWorldObservation | None = None
        self._last_screen: _ScreenObservation | None = None
        self._last_identity: ProcessIdentity | None = None
        self._last_window: int | None = None
        self._last_ui: UIEvidence | None = None
        self._bnet_launched = False
        self._known_bnet_identity: ProcessIdentity | None = None
        self._bnet_launch_deadline: float | None = None
        self._login_deadline: float | None = None
        self._play_attempts = 0
        self._play_deadline: float | None = None
        self._news_close_pending = False
        self._news_deadline: float | None = None
        self._news_close_window: int | None = None
        self._enter_world_dispatched = False
        self._known_game_identity: ProcessIdentity | None = None
        self._world_deadline: float | None = None
        self._game_window_deadline: float | None = None
        self._repair_deadline: float | None = None
        self._repair_retry_at = 0.0
        self._restart_identity: ProcessIdentity | None = None
        self._restart_reason: str | None = None
        self._closing_identity: ProcessIdentity | None = None
        self._close_deadline: float | None = None
        self._close_dispatched = False
        self._closed = False

    @property
    def active(self) -> bool:
        """Whether this recovery object currently holds the canonical GUI lease."""

        return self._active

    def step(self) -> StartupStatus:
        """Take one fresh bounded step; no phase wait sleeps inside the controller."""

        if self._terminal_status is not None:
            if self._terminal_status.outcome == "ready":
                self._reset_after_ready()
            elif (
                self._terminal_status.reason in _SAFE_REOBSERVATION_REFUSALS
                and self._retry_mono is not None
                and self._clock() >= self._retry_mono
            ):
                self._terminal_status = None
                self._last_status = None
                self._retry_mono = None
                self._last_screen = None
                self._last_ui = None
                self._last_native = None
            else:
                return self._terminal_status
        if self._closed:
            now = self._wall()
            return StartupStatus("stopped", "desktop", "closed", "none", now, None, None, False, None, None)
        if self._stopping():
            return self._finish("stopped", "desktop", "stop_requested")

        try:
            session = self.session_provider.current()
        except DesktopSessionUnavailable as error:
            self._last_native = None
            self._pause_episode()
            if not self._release_episode():
                return self._publish(
                    "failed", "desktop", "control_cleanup_failed", identity=None, window=None
                )
            return self._publish(
                "held", "desktop", error.reason, retry=self._clock() + 5.0, identity=None, window=None
            )
        if self._session_fingerprint is not None and session.fingerprint != self._session_fingerprint:
            self._last_native = None
            self._pause_episode()
            if not self._release_episode():
                return self._publish(
                    "failed", "desktop", "control_cleanup_failed", identity=None, window=None
                )
            return self._publish(
                "held",
                "desktop",
                "desktop_session_changed",
                retry=self._clock() + 1.0,
                identity=None,
                window=None,
            )
        blocker = maintenance_blocker()
        if blocker is not None:
            self._pause_episode()
            if not self._release_episode():
                return self._publish(
                    "failed", "ownership", "control_cleanup_failed", identity=None, window=None
                )
            return self._publish(
                "held",
                "ownership",
                "automation_maintenance_or_uncertain",
                retry=self._clock() + 5.0,
                identity=None,
                window=None,
            )
        active, _presence_reason = self._presence_active(session.environment)
        if active:
            self._pause_episode()
            if not self._release_episode():
                return self._publish(
                    "failed", "ownership", "control_cleanup_failed", identity=None, window=None
                )
            return self._publish(
                "held",
                "ownership",
                "physical_presence_hold",
                retry=self._clock() + 5.0,
                identity=None,
                window=None,
            )
        if not self._active:
            try:
                self._lease_context = self._lease.hold()
                self._lease_context.__enter__()
                self._active = True
            except ActError:
                self._lease_context = None
                self._pause_episode()
                return self._publish(
                    "held",
                    "ownership",
                    "gui_busy",
                    retry=self._clock() + 1.0,
                    identity=None,
                    window=None,
                )
        self._session_fingerprint = session.fingerprint
        self._start_or_resume_episode()
        if self._stopping():
            return self._finish("stopped", "desktop", "stop_requested")
        if self._clock() >= self._episode_deadline:
            return self._finish("failed", self._phase(), "episode_deadline_expired")

        if self._last_status is not None and self._last_status.outcome == "waiting":
            if self._retry_mono is not None and self._clock() < self._retry_mono:
                return self._last_status
        try:
            return self._step_locked(session)
        except _HoldEpisode as error:
            phase = self._phase()
            self._last_native = None
            self._pause_episode()
            if not self._release_episode():
                return self._publish(
                    "failed",
                    phase,
                    "control_cleanup_failed",
                    identity=self._last_identity,
                    window=self._last_window,
                )
            return self._publish(
                "held",
                phase,
                error.reason,
                retry=self._clock() + 1.0,
                identity=self._last_identity,
                window=self._last_window,
            )
        except _BlockEpisode as error:
            return self._finish("blocked", self._phase(), error.reason)
        except DesktopSessionUnavailable as error:
            self._last_native = None
            self._pause_episode()
            phase = self._phase()
            if phase == "game_close":
                self._restart_identity = None
                self._restart_reason = None
            if not self._release_episode():
                return self._publish("failed", phase, "control_cleanup_failed", identity=None, window=None)
            return self._publish(
                "held",
                "desktop",
                error.reason,
                retry=self._clock() + 5.0,
                identity=None,
                window=None,
            )
        except CaptureError as error:
            message = f"{type(error).__name__}: {' '.join(str(error).split())}"
            prefix = "startup screen capture failed: "
            _LOG.warning("%s%s", prefix, message[: 200 - len(prefix)])
            self._last_native = None
            phase = self._phase()
            deadlines: list[float] = [self._episode_deadline]
            if phase == "battle_net_login":
                deadlines.extend(
                    item for item in (self._login_deadline, self._play_deadline) if item is not None
                )
                if self._news_close_pending:
                    if self._news_deadline is not None:
                        deadlines.append(self._news_deadline)
            elif phase == "world_loading":
                if self._world_deadline is not None:
                    deadlines.append(self._world_deadline)
            elif phase == "game_close":
                if self._close_deadline is not None:
                    deadlines.append(self._close_deadline)
            deadline = min(deadlines)
            if not self._release_episode():
                return self._publish(
                    "failed",
                    phase,
                    "control_cleanup_failed",
                    identity=self._last_identity,
                    window=self._last_window,
                )
            return self._wait(
                phase,
                "screen_capture_failed_reobserve",
                identity=self._last_identity,
                window=self._last_window,
                deadline=deadline,
            )
        except StartupUIError as error:
            if error.args and error.args[0] == "screen_capture_stale":
                phase = self._phase()
                phase_deadline = {
                    "battle_net_login": self._login_deadline,
                    "world_loading": self._world_deadline,
                    "game_close": self._close_deadline,
                }.get(phase)
                deadlines = [self._episode_deadline]
                if phase_deadline is not None:
                    deadlines.append(phase_deadline)
                return self._wait(
                    phase,
                    "screen_capture_stale_reobserve",
                    identity=self._last_identity,
                    window=self._last_window,
                    deadline=min(deadlines),
                )
            return self._finish("failed", self._phase(), _safe_failure_reason(error))
        except (
            ObserveError,
            ControlChildCancelled,
            ControlChildError,
            BattleNetPlayError,
        ) as error:
            reason = _safe_failure_reason(error)
            if isinstance(error, BattleNetPlayError):
                message = f"{type(error).__name__}: {' '.join(str(error).split())}"
                prefix = "startup control failed: "
                _LOG.warning("%s%s", prefix, message[: 200 - len(prefix)])
            return self._finish("failed", self._phase(), reason)
        except (OSError, subprocess.SubprocessError, ValueError, ActError) as error:
            return self._finish("failed", self._phase(), _safe_failure_reason(error))

    def request_restart(self, identity: ProcessIdentity, reason: str) -> StartupStatus:
        """Request a guarded normal WM close for one proven current in-world client."""

        if reason not in _RESTART_REASONS:
            return self._finish("blocked", "game_close", "restart_reason_not_authorized")
        if not isinstance(identity, ProcessIdentity):
            return self._finish("blocked", "game_close", "restart_identity_invalid")
        if self._terminal_status is not None:
            if self._terminal_status.outcome != "ready":
                return self._terminal_status
            self._terminal_status = None
            self._last_status = None
            self._started_mono = None
            self._started_wall = 0.0
            self._last_screen = None
            self._last_identity = None
            self._last_window = None
        if self._restart_identity is not None:
            if identity != self._restart_identity or reason != self._restart_reason:
                return self._finish("blocked", "game_close", "restart_request_conflict")
            return self.step()
        self._restart_identity = identity
        self._restart_reason = reason
        return self.step()

    def close(self) -> None:
        """Idempotently cancel/reap exact controls and release the GUI lease."""

        if self._closed:
            return
        self._closed = self._release_episode()

    def _reset_after_ready(self) -> None:
        """Retire the completed episode and let the next call reobserve from scratch."""

        self._terminal_status = None
        self._last_status = None
        self._started_mono = None
        self._started_wall = 0.0
        self._episode_deadline = 0.0
        self._episode_remaining = None
        self._retry_mono = None
        self._session_fingerprint = None
        self._last_screen = None
        self._last_identity = None
        self._last_window = None
        self._last_ui = None
        self._bnet_launched = False
        self._known_bnet_identity = None
        self._bnet_launch_deadline = None
        self._login_deadline = None
        self._play_attempts = 0
        self._play_deadline = None
        self._news_close_pending = False
        self._news_deadline = None
        self._news_close_window = None
        self._enter_world_dispatched = False
        self._known_game_identity = None
        self._world_deadline = None
        self._game_window_deadline = None
        self._repair_deadline = None
        self._repair_retry_at = 0.0
        self._restart_identity = None
        self._restart_reason = None
        self._closing_identity = None
        self._close_deadline = None
        self._close_dispatched = False

    def _start_or_resume_episode(self) -> None:
        if self._started_mono is not None:
            return
        now = self._clock()
        if self._started_wall == 0.0:
            self._started_wall = self._wall()
        remaining = _EPISODE_SECONDS if self._episode_remaining is None else self._episode_remaining
        self._started_mono = now
        self._episode_deadline = now + remaining
        self._episode_remaining = None

    def _pause_episode(self) -> None:
        if self._started_mono is None:
            return
        self._episode_remaining = max(0.0, self._episode_deadline - self._clock())
        self._started_mono = None
        self._episode_deadline = 0.0

    def _step_locked(self, session: DesktopSession) -> StartupStatus:
        now = self._clock()
        identities = self._game_identities()
        if len(identities) > 1:
            raise _BlockEpisode("multiple_owned_game_processes")
        if self._closing_identity is not None:
            if identities == (self._closing_identity,):
                if self._close_deadline is not None and now >= self._close_deadline:
                    return self._finish("failed", "game_close", "normal_window_close_timed_out")
                return self._wait(
                    "game_close",
                    "waiting_for_exact_game_exit",
                    identity=self._closing_identity,
                    window=self._last_window,
                    deadline=self._close_deadline,
                    action="wm_close_dispatched" if self._close_dispatched else "none",
                )
            if identities:
                raise _BlockEpisode("game_process_replaced_during_close")
            self._closing_identity = None
            self._known_game_identity = None
            self._restart_identity = None
            self._restart_reason = None
            self._close_dispatched = False
            self._close_deadline = None

        if self._restart_identity is not None:
            return self._request_exact_close(session, identities)
        if identities:
            if self._last_native is not None and self._last_native.identity != identities[0]:
                self._last_native = None
            return self._step_game(session, identities[0])
        if self._known_game_identity is not None:
            raise _BlockEpisode("owned_game_process_disappeared_unexpectedly")
        self._last_native = None
        self._game_window_deadline = None
        self._world_deadline = None
        return self._step_battle_net(session)

    def _step_game(self, session: DesktopSession, identity: ProcessIdentity) -> StartupStatus:
        if self._known_game_identity is not None and self._known_game_identity != identity:
            raise _BlockEpisode("game_process_replaced_during_episode")
        self._known_game_identity = identity
        self._last_identity = identity
        self._last_window = None
        if self._game_window_deadline is None:
            self._game_window_deadline = self._clock() + _WINDOW_SECONDS
        state = self._game_window(identity, session)
        if state.foreign_count:
            raise _BlockEpisode("foreign_game_window_candidate")
        if state.count > 1:
            raise _BlockEpisode("multiple_owned_game_windows")
        if state.xid is None:
            if self._clock() >= self._game_window_deadline:
                return self._finish("failed", "game_window", "owned_game_window_missing")
            return self._wait(
                "game_window",
                "waiting_for_owned_game_window",
                identity=identity,
                window=None,
                deadline=self._game_window_deadline,
            )
        self._last_window = state.xid
        if state.detail == "window_process_mismatch":
            raise _BlockEpisode("game_window_process_mismatch")
        if not state.visible:
            if self._clock() >= self._game_window_deadline:
                return self._finish(
                    "failed", "game_window", "game_window_restore_timed_out", identity, state.xid
                )
            try:
                self._activate(session, identity, state.xid, game=True)
            except subprocess.TimeoutExpired:
                _LOG.warning("startup game-window restore timed out; reobserving same window")
                if self._clock() >= self._game_window_deadline:
                    return self._finish(
                        "failed", "game_window", "game_window_restore_timed_out", identity, state.xid
                    )
                return self._wait(
                    "game_window",
                    "game_window_restore_timeout_reobserve",
                    identity=identity,
                    window=state.xid,
                    deadline=self._game_window_deadline,
                )
            return self._wait(
                "game_window",
                "game_window_restore_dispatched",
                identity=identity,
                window=state.xid,
                deadline=self._game_window_deadline,
                action="window_activation_dispatched",
            )
        if not state.ok or not state.fullscreen:
            if self._repair_deadline is None:
                self._repair_deadline = self._clock() + _WINDOW_SECONDS
            if self._clock() >= self._repair_deadline:
                return self._finish(
                    "failed", "game_window", "fullscreen_repair_timed_out", identity, state.xid
                )
            if self._clock() < self._repair_retry_at:
                return self._wait(
                    "game_window",
                    "verifying_fullscreen_repair",
                    identity=identity,
                    window=state.xid,
                    deadline=self._repair_deadline,
                )
            try:
                self._repair_fullscreen(session, identity, state.xid)
            except subprocess.TimeoutExpired:
                _LOG.warning("startup fullscreen repair timed out; reobserving same window")
                self._repair_retry_at = self._clock() + _REPAIR_RETRY_SECONDS
                if self._clock() >= self._repair_deadline:
                    return self._finish(
                        "failed", "game_window", "fullscreen_repair_timed_out", identity, state.xid
                    )
                return self._wait(
                    "game_window",
                    "fullscreen_repair_timeout_reobserve",
                    identity=identity,
                    window=state.xid,
                    deadline=self._repair_deadline,
                )
            self._repair_retry_at = self._clock() + _REPAIR_RETRY_SECONDS
            return self._wait(
                "game_window",
                "fullscreen_repair_dispatched",
                identity=identity,
                window=state.xid,
                deadline=self._repair_deadline,
                action="fullscreen_repair_dispatched",
            )
        self._repair_deadline = None
        self._repair_retry_at = 0

        try:
            native = observe_native_world(
                self.config,
                session_provider=self.session_provider,
                clock=self._clock,
                wall_clock=self._wall,
            )
        except DesktopSessionUnavailable as error:
            raise _HoldEpisode(error.reason) from error
        if native.identity != identity:
            raise _HoldEpisode("game_process_identity_changed")
        self._last_native = native
        if native.snapshot is not None:
            self._world_deadline = None
            return self._finish(
                "ready",
                "native_ready",
                "matching_native_world_ready",
                native=True,
                identity=identity,
                window=native.window.xid,
            )
        if self._world_deadline is None:
            self._world_deadline = self._clock() + _WORLD_SECONDS
        if self._clock() >= self._world_deadline:
            return self._finish(
                "failed", "world_loading", "world_entry_deadline_expired", identity, state.xid
            )

        screen = self._screen_observation(identity, session, game=True)
        self._last_screen = screen
        evidence = classify_character_select_capture(
            screen.reader,
            screen.ocr,
            screen.image,  # type: ignore[arg-type]
            expected_selected_name=self.config.character_select_name,
        )
        self._last_ui = evidence
        if evidence.kind == UIKind.CHARACTER_SELECT:
            if self._enter_world_dispatched:
                return self._wait(
                    "world_loading",
                    "waiting_for_native_world_after_enter",
                    identity=identity,
                    window=state.xid,
                    deadline=self._world_deadline,
                    action="enter_world_click_dispatched",
                )
            if not state.focused:
                self._activate(session, identity, state.xid, game=True)
                return self._wait(
                    "character_select",
                    "game_window_activation_dispatched",
                    identity=identity,
                    window=state.xid,
                    deadline=self._world_deadline,
                    action="window_activation_dispatched",
                )
            self._click_game_control(session, identity, screen, evidence)
            self._enter_world_dispatched = True
            return self._wait(
                "world_loading",
                "enter_world_click_dispatched",
                identity=identity,
                window=state.xid,
                deadline=self._world_deadline,
                action="enter_world_click_dispatched",
            )
        if evidence.kind == UIKind.QUEUE:
            reason = evidence.reason
        elif evidence.kind == UIKind.DISCONNECTED:
            reason = evidence.reason
        elif evidence.kind == UIKind.WORLD_LOADING:
            reason = evidence.reason
        else:
            reason = "ui_unrecognized_no_input_authorized"
        return self._wait(
            "world_loading",
            reason,
            identity=identity,
            window=state.xid,
            deadline=self._world_deadline,
        )

    def _step_battle_net(self, session: DesktopSession) -> StartupStatus:
        self._last_identity = None
        identities = _battle_net_identities(self.config.wine_prefix)
        if len(identities) > 1:
            raise _BlockEpisode("multiple_battle_net_instances")
        if not identities:
            if self._known_bnet_identity is not None:
                if self._play_deadline is not None and self._clock() < self._play_deadline:
                    return self._wait(
                        "game_start",
                        "waiting_for_game_after_battle_net_exit",
                        window=self._last_window,
                        deadline=self._play_deadline,
                        action="play_click_dispatched",
                    )
                raise _BlockEpisode("battle_net_disappeared_after_observation")
            if self._bnet_launched:
                deadline = self._bnet_launch_deadline or self._clock() + _LOGIN_SECONDS
                if self._clock() >= deadline:
                    return self._finish("failed", "battle_net_login", "battle_net_start_timed_out")
                return self._wait(
                    "battle_net_login",
                    "waiting_for_battle_net_process",
                    window=None,
                    deadline=deadline,
                )
            self._guard_launch(session)
            if _battle_net_identities(self.config.wine_prefix):
                raise _HoldEpisode("battle_net_process_appeared_before_launch")
            self._launch_battle_net(session)
            self._bnet_launched = True
            self._bnet_launch_deadline = self._clock() + _LOGIN_SECONDS
            self._login_deadline = self._bnet_launch_deadline
            return self._wait(
                "battle_net_login",
                "battle_net_launch_dispatched",
                window=None,
                deadline=self._bnet_launch_deadline,
                action="battle_net_launch_dispatched",
            )

        identity = identities[0]
        if self._known_bnet_identity is not None and self._known_bnet_identity != identity:
            raise _BlockEpisode("battle_net_process_replaced_during_episode")
        self._known_bnet_identity = identity
        self._last_identity = None  # public status ProcessIdentity is reserved for WoW.
        if self._login_deadline is None:
            self._login_deadline = self._clock() + _LOGIN_SECONDS
        if self._clock() >= self._login_deadline:
            return self._finish("failed", "battle_net_login", "battle_net_login_deadline_expired")
        state = self._battle_net_window(identity, session)
        if state.foreign_count:
            raise _BlockEpisode("foreign_battle_net_window_candidate")
        if state.count > 1:
            raise _BlockEpisode("multiple_battle_net_windows")
        if state.xid is None:
            return self._wait(
                "battle_net_login",
                "waiting_for_battle_net_window",
                window=None,
                deadline=self._login_deadline,
            )
        self._last_window = state.xid
        if state.detail == "window_process_mismatch":
            raise _BlockEpisode("battle_net_window_process_mismatch")

        if self._battle_net_unknown_window_candidates(identity, session):
            raise _BlockEpisode("unrecognized_battle_net_window_candidate")
        updates = self._battle_net_updates_window(identity, session)
        if updates.foreign_count:
            raise _BlockEpisode("foreign_battle_net_updates_window_candidate")
        if updates.count > 1:
            raise _BlockEpisode("multiple_battle_net_updates_windows")
        if self._news_close_pending:
            if updates.xid is None:
                self._news_close_pending = False
                self._news_deadline = None
                self._news_close_window = None
                return self._wait(
                    "battle_net_ready",
                    "battle_net_updates_disappeared_reobserving_main",
                    window=state.xid,
                    deadline=self._login_deadline,
                )
            if updates.xid != self._news_close_window:
                raise _BlockEpisode("battle_net_updates_window_changed_after_close")
            if self._news_deadline is not None and self._clock() >= self._news_deadline:
                return self._finish(
                    "failed",
                    "news_modal",
                    "battle_net_updates_did_not_close",
                    window=updates.xid,
                )
            return self._wait(
                "news_modal",
                "verifying_battle_net_updates_dismissal",
                window=updates.xid,
                deadline=self._news_deadline,
                action="news_close_click_dispatched",
            )
        if updates.xid is not None:
            if updates.detail == "window_process_mismatch":
                raise _BlockEpisode("battle_net_updates_window_process_mismatch")
            if not updates.visible or not updates.focused:
                self._activate_updates_window(session, identity, updates.xid)
                return self._wait(
                    "news_modal",
                    "battle_net_updates_activation_dispatched",
                    window=updates.xid,
                    deadline=self._login_deadline,
                    action="window_activation_dispatched",
                )
            if not _is_updates_rect(updates.outer_rect):
                return self._wait(
                    "news_modal",
                    "battle_net_updates_panel_loading",
                    window=updates.xid,
                    deadline=self._login_deadline,
                )
            updates_screen = self._screen_updates_observation(session, identity, updates.xid)
            updates_evidence = self._classify_battle_net_updates(updates_screen)
            if updates_evidence.kind != UIKind.NEWS_MODAL:
                return self._wait(
                    "news_modal",
                    updates_evidence.reason,
                    window=updates.xid,
                    deadline=self._login_deadline,
                )
            self._click_battle_net_updates_close(session, identity, updates_screen, updates_evidence)
            self._news_close_pending = True
            self._news_close_window = updates.xid
            self._news_deadline = self._clock() + 10.0
            return self._wait(
                "news_modal",
                "battle_net_updates_close_click_dispatched",
                window=updates.xid,
                deadline=self._news_deadline,
                action="news_close_click_dispatched",
            )
        if not state.visible:
            self._activate(session, identity, state.xid, game=False)
            return self._wait(
                "battle_net_login",
                "battle_net_window_restore_dispatched",
                window=state.xid,
                deadline=self._login_deadline,
                action="window_activation_dispatched",
            )
        screen = self._screen_observation(identity, session, game=False)
        self._last_screen = screen
        evidence = self._classify_battle_net(screen)
        self._last_ui = evidence
        if evidence.kind == UIKind.LOGIN_REQUIRED:
            raise _BlockEpisode("battle_net_login_required")
        if evidence.kind == UIKind.ACCOUNT_CONFLICT:
            raise _BlockEpisode("battle_net_account_conflict")
        if evidence.kind == UIKind.UPDATE_BUSY:
            return self._wait(
                "battle_net_ready",
                "battle_net_update_or_install_busy",
                identity=None,
                window=state.xid,
                deadline=self._login_deadline,
            )
        if evidence.kind == UIKind.PLAY_READY:
            if self._play_deadline is not None:
                if self._clock() < self._play_deadline:
                    return self._wait(
                        "game_start",
                        "waiting_after_play_click",
                        window=state.xid,
                        deadline=self._play_deadline,
                        action="play_click_dispatched",
                    )
                self._play_deadline = None
            if self._play_attempts >= 3:
                return self._finish("failed", "game_start", "play_attempt_limit_reached", window=state.xid)
            if not state.focused:
                self._activate(session, identity, state.xid, game=False)
                return self._wait(
                    "battle_net_ready",
                    "battle_net_window_activation_dispatched",
                    window=state.xid,
                    deadline=self._login_deadline,
                    action="window_activation_dispatched",
                )
            self._click_battle_net_control(session, identity, screen, evidence, action="play")
            self._play_attempts += 1
            self._play_deadline = self._clock() + _PLAY_WAIT_SECONDS
            return self._wait(
                "game_start",
                "play_click_dispatched",
                window=state.xid,
                deadline=self._play_deadline,
                action="play_click_dispatched",
            )
        if self._play_deadline is not None:
            if self._clock() < self._play_deadline:
                return self._wait(
                    "game_start",
                    "waiting_for_game_after_play_click",
                    window=state.xid,
                    deadline=self._play_deadline,
                    action="play_click_dispatched",
                )
            raise _BlockEpisode("play_postcondition_not_ready_for_retry")
        raise _BlockEpisode("battle_net_ui_unrecognized")

    def _request_exact_close(
        self, session: DesktopSession, identities: tuple[ProcessIdentity, ...]
    ) -> StartupStatus:
        identity = self._restart_identity
        if identity is None or identities != (identity,):
            raise _HoldEpisode("restart_identity_no_longer_current")
        try:
            native = observe_native_world(
                self.config,
                session_provider=self.session_provider,
                clock=self._clock,
                wall_clock=self._wall,
            )
        except (ObserveError, DesktopSessionUnavailable):
            self._restart_identity = None
            self._restart_reason = None
            return self._finish("held", "game_close", "fresh_native_world_required_for_restart")
        previous = self._last_native
        previous_boot = (
            previous.snapshot.get("boot") if previous is not None and previous.snapshot is not None else None
        )
        current_boot = native.snapshot.get("boot") if native.snapshot is not None else None
        if (
            native.identity != identity
            or native.snapshot is None
            or native.window.xid is None
            or not previous_boot
            or current_boot != previous_boot
        ):
            self._restart_identity = None
            self._restart_reason = None
            return self._finish("held", "game_close", "fresh_native_world_required_for_restart")
        self._last_native = native
        self._last_identity = identity
        window = native.window.xid
        self._last_window = window
        self._require_disk_build()
        self._guard_identity(session, identity, window, game=True, require_focus=False)

        def close_guard() -> None:
            self._guard_identity(session, identity, window, game=True, require_focus=False)
            self._require_disk_build()

        control = self._new_control(
            session, identity, window, game=True, require_focus=False, guard=close_guard
        )
        try:
            result = control.run(
                [self._input_tool().proc_path, "windowclose", str(window)],
                timeout=5.0,
                env=dict(session.environment),
                pass_fds=(self._input_tool().image.fd,),
            )
        finally:
            self._close_control(control)
        if result.returncode != 0:
            raise _BlockEpisode("normal_window_close_failed")
        self._closing_identity = identity
        self._close_dispatched = True
        self._close_deadline = self._clock() + _CLOSE_SECONDS
        return self._wait(
            "game_close",
            "normal_window_close_dispatched",
            identity=identity,
            window=window,
            deadline=self._close_deadline,
            action="wm_close_dispatched",
        )

    def _game_identities(self) -> tuple[ProcessIdentity, ...]:
        return self._game_matcher().identities()

    def _game_matcher(self) -> OwnedGameMatcher:
        return OwnedGameMatcher.for_game(
            game_executable=self.config.game_executable,
            game_argv0=self.config.game_argv0,
            wine_prefix=self.config.wine_prefix,
        )

    def _game_window(self, identity: ProcessIdentity, session: DesktopSession) -> WindowState:
        return X11Window(
            self.config.window_class,
            self.config.window_title,
            x11_runner(lambda: dict(self.session_provider.current().environment)),
        ).observe([identity.pid], include_hidden=True)

    def _battle_net_window(self, identity: ProcessIdentity, session: DesktopSession) -> WindowState:
        return X11Window(
            _BATTLE_NET_WINDOWS_CLASS,
            _BATTLE_NET_WINDOW_TITLE,
            x11_runner(lambda: dict(self.session_provider.current().environment)),
        ).observe([identity.pid], include_hidden=True)

    def _battle_net_updates_window(self, identity: ProcessIdentity, session: DesktopSession) -> WindowState:
        return X11Window(
            _BATTLE_NET_WINDOWS_CLASS,
            _BATTLE_NET_UPDATES_TITLE,
            x11_runner(lambda: dict(self.session_provider.current().environment)),
        ).observe([identity.pid], include_hidden=True)

    def _battle_net_unknown_window_candidates(
        self, identity: ProcessIdentity, session: DesktopSession
    ) -> int:
        run = x11_runner(lambda: dict(self.session_provider.current().environment))
        code, output = run(
            ["xdotool", "search", "--onlyvisible", "--class", f"^{re.escape(_BATTLE_NET_WINDOWS_CLASS)}$"]
        )
        if code == 1:
            return 0
        if code != 0:
            raise ObserveError(f"xdotool search exited {code}")
        count = 0
        known_titles = {_BATTLE_NET_WINDOW_TITLE, _BATTLE_NET_UPDATES_TITLE}
        for candidate in output.split():
            if not candidate.isdigit():
                raise ObserveError("xdotool search returned an invalid window id")
            title_code, title = run(["xdotool", "getwindowname", candidate])
            if title_code != 0:
                raise ObserveError("could not read Battle.net window title")
            pid_code, pid = run(["xdotool", "getwindowpid", candidate])
            if pid_code != 0 or not pid.strip().isdigit():
                raise ObserveError("could not read Battle.net window owner")
            if title.strip() not in known_titles:
                count += 1
        return count

    def _guard_updates_window(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        window: int,
        *,
        require_focus: bool,
    ) -> None:
        self._guard_common(session)
        if self._game_identities():
            raise _HoldEpisode("owned_game_process_appeared_before_updates_action")
        if self._identities_for(identity, game=False) != (identity,):
            raise _HoldEpisode("battle_net_process_identity_changed")
        if self._battle_net_unknown_window_candidates(identity, session):
            raise _HoldEpisode("battle_net_window_inventory_changed")
        main = self._battle_net_window(identity, session)
        if main.count != 1 or main.xid != self._last_window:
            raise _HoldEpisode("battle_net_main_window_changed_during_updates_action")
        updates = self._battle_net_updates_window(identity, session)
        if updates.foreign_count or updates.count != 1 or updates.xid != window:
            raise _HoldEpisode("battle_net_updates_window_changed")
        if not _is_updates_rect(updates.outer_rect):
            raise _HoldEpisode("battle_net_updates_geometry_changed")
        if require_focus and (not updates.visible or not updates.focused):
            raise _HoldEpisode("battle_net_updates_focus_changed")

    def _screen_updates_observation(
        self, session: DesktopSession, identity: ProcessIdentity, window: int
    ) -> _ScreenObservation:
        self._guard_updates_window(session, identity, window, require_focus=True)
        source = self._battle_net_updates_window(identity, session)
        rect = source.outer_rect
        if (
            source.xid != window
            or rect is None
            or not _is_updates_rect(rect)
            or not source.visible
            or not source.focused
        ):
            raise _HoldEpisode("battle_net_updates_window_unavailable_for_capture")
        left, top, width, height = rect
        dirs = XdgDirs.from_environ(os.environ)

        def environment_provider() -> Mapping[str, str]:
            return dict(self.session_provider.current().environment)

        captured = self._clock()
        full_image: Image.Image = ScreenCanvas(
            dirs.crowley_runtime / "startup-capture", environment_provider
        ).capture(source.display)
        if left < 0 or top < 0 or left + width > full_image.width or top + height > full_image.height:
            raise _HoldEpisode("battle_net_updates_window_outside_display")
        image = full_image.crop((left, top, left + width, top + height))
        current_session = self.session_provider.current()
        if current_session.fingerprint != session.fingerprint:
            raise _HoldEpisode("desktop_session_changed_during_capture")
        reader = self._reader_factory(current_session.environment)
        ocr = reader.read(image, captured_monotonic=captured)
        exact = self._identities_for(identity, game=False)
        after = self._battle_net_updates_window(identity, session)
        if (
            exact != (identity,)
            or after.xid != window
            or after.count != 1
            or after.foreign_count
            or after.outer_rect != rect
            or after.display != source.display
            or not after.visible
            or not after.focused
        ):
            raise _HoldEpisode("battle_net_updates_window_changed_during_capture")
        if self.session_provider.current().fingerprint != session.fingerprint:
            raise _HoldEpisode("desktop_session_changed_during_capture")
        return _ScreenObservation(identity, after, image, ocr, reader, session.fingerprint)

    def _classify_battle_net_updates(self, screen: _ScreenObservation) -> UIEvidence:
        evidence = classify_battle_net_updates_capture(
            screen.reader,
            screen.ocr,
            screen.image,  # type: ignore[arg-type]
        )
        rect = screen.window.outer_rect
        if evidence.control is None or rect is None:
            return evidence
        x, y, _width, _height = rect
        left, top, width, height = evidence.control
        return UIEvidence(
            evidence.kind,
            evidence.reason,
            (x + left, y + top, width, height),
            evidence.control_text,
            evidence.context_fingerprint,
        )

    def _activate_updates_window(
        self, session: DesktopSession, identity: ProcessIdentity, window: int
    ) -> None:
        def guard() -> None:
            self._guard_updates_window(session, identity, window, require_focus=False)

        guard()
        runtime = self._new_control(
            session,
            identity,
            window,
            game=False,
            require_focus=False,
            guard=guard,
        )
        try:
            result = runtime.run(
                [self._input_tool().proc_path, "windowactivate", "--sync", str(window)],
                timeout=5.0,
                env=dict(session.environment),
                pass_fds=(self._input_tool().image.fd,),
            )
        finally:
            self._close_control(runtime)
        if result.returncode != 0:
            raise _BlockEpisode("battle_net_updates_activation_failed")

    def _click_battle_net_updates_close(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        screen: _ScreenObservation,
        evidence: UIEvidence,
    ) -> None:
        control_box = _evidence_box(evidence)
        window = screen.window.xid
        if control_box is None or window is None:
            raise _BlockEpisode("battle_net_updates_close_geometry_unavailable")

        def guard() -> None:
            self._guard_updates_window(session, identity, window, require_focus=True)

        guard()
        runtime = self._new_control(
            session,
            identity,
            window,
            game=False,
            require_focus=True,
            guard=guard,
        )

        def revalidate() -> Box | None:
            fresh = self._screen_updates_observation(session, identity, window)
            classified = self._classify_battle_net_updates(fresh)
            if (
                classified.kind != evidence.kind
                or classified.reason != evidence.reason
                or classified.control_text != evidence.control_text
                or classified.context_fingerprint != evidence.context_fingerprint
            ):
                return None
            return _evidence_box(classified)

        try:
            click_control(
                window,
                control_box,
                screen.window.display,
                tool=self._input_tool(),
                control_run=runtime.run,
                environment=lambda: self.session_provider.current().environment,
                guard=guard,
                revalidate=revalidate,
                action="news_close",
            )
        finally:
            self._close_control(runtime)

    def _screen_observation(
        self, identity: ProcessIdentity, session: DesktopSession, *, game: bool
    ) -> _ScreenObservation:
        window = self._last_window
        if window is None:
            raise _HoldEpisode("window_identity_unavailable")
        self._guard_identity(session, identity, window, game=game, require_focus=False)
        source = self._game_window(identity, session) if game else self._battle_net_window(identity, session)
        if source.xid is None or source.xid != self._last_window or source.count != 1 or not source.visible:
            raise _HoldEpisode("window_changed_before_capture")
        captured = self._clock()
        dirs = XdgDirs.from_environ(os.environ)

        def environment_provider() -> Mapping[str, str]:
            return dict(self.session_provider.current().environment)

        image: Image.Image = ScreenCanvas(
            dirs.crowley_runtime / "startup-capture", environment_provider
        ).capture(source.display)
        current_session = self.session_provider.current()
        if current_session.fingerprint != session.fingerprint:
            raise _HoldEpisode("desktop_session_changed_during_capture")
        reader = self._reader_factory(current_session.environment)
        ocr = reader.read(image, captured_monotonic=captured)
        exact = self._identities_for(identity, game=game)
        after = self._game_window(identity, session) if game else self._battle_net_window(identity, session)
        if (
            exact != (identity,)
            or after.xid != source.xid
            or after.count != 1
            or after.outer_rect != source.outer_rect
            or after.client_rect != source.client_rect
            or after.display != source.display
            or after.ok != source.ok
            or after.fullscreen != source.fullscreen
            or after.visible != source.visible
            or after.focused != source.focused
        ):
            raise _HoldEpisode("process_or_window_changed_during_capture")
        if self.session_provider.current().fingerprint != session.fingerprint:
            raise _HoldEpisode("desktop_session_changed_during_capture")
        self._last_window = source.xid
        return _ScreenObservation(identity, after, image, ocr, reader, session.fingerprint)

    def _classify_battle_net(self, screen: _ScreenObservation) -> UIEvidence:
        title = _PRODUCT_UI_TITLES.get(self.config.product)
        if title is None:
            return UIEvidence(UIKind.UNKNOWN, "configured_product_has_no_verified_display_title")
        evidence = classify_battle_net_ui(screen.ocr.words, screen.image, expected_product=title)  # type: ignore[arg-type]
        if evidence.kind != UIKind.UNKNOWN:
            return evidence
        window = _window_box(screen.window)
        if window is None:
            return UIEvidence(UIKind.UNKNOWN, "battle_net_window_geometry_unavailable")
        try:
            button = locate_play_button(
                screen.image,  # type: ignore[arg-type]
                window,
                words=screen.ocr.words,
                captured_monotonic=screen.ocr.captured_monotonic,
            )
        except BattleNetPlayError:
            return UIEvidence(UIKind.UNKNOWN, "play_control_not_uniquely_verified")
        return classify_battle_net_ui(
            screen.ocr.words,
            screen.image,  # type: ignore[arg-type]
            expected_product=title,
            play_button=(button.left, button.top, button.width, button.height),
        )

    def _click_battle_net_control(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        screen: _ScreenObservation,
        evidence: UIEvidence,
        *,
        action: str,
    ) -> None:
        control_box = _evidence_box(evidence)
        window = screen.window.xid
        if control_box is None or window is None:
            raise _BlockEpisode("startup_control_geometry_unavailable")
        self._guard_identity(session, identity, window, game=False, require_focus=True)
        if action == "play":
            self._require_disk_build()
        runtime = self._new_control(session, identity, window, game=False, require_focus=True)
        fresh_box: Box | None = None

        def revalidate() -> Box | None:
            nonlocal fresh_box
            fresh = self._screen_observation(identity, session, game=False)
            classified = self._classify_battle_net(fresh)
            if (
                classified.kind != evidence.kind
                or classified.reason != evidence.reason
                or classified.control_text != evidence.control_text
            ):
                return None
            fresh_box = _evidence_box(classified)
            return fresh_box

        def action_guard() -> None:
            self._guard_identity(session, identity, window, game=False, require_focus=True)
            if action == "play":
                self._require_disk_build()

        try:
            click_control(
                window,
                control_box,
                screen.window.display,
                tool=self._input_tool(),
                control_run=runtime.run,
                environment=lambda: self.session_provider.current().environment,
                guard=action_guard,
                revalidate=revalidate,
                action=action,
            )
        finally:
            self._close_control(runtime)

    def _click_game_control(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        screen: _ScreenObservation,
        evidence: UIEvidence,
    ) -> None:
        control_box = _evidence_box(evidence)
        window = screen.window.xid
        if control_box is None or window is None:
            raise _BlockEpisode("enter_world_control_geometry_unavailable")
        self._require_disk_build()
        self._guard_identity(session, identity, window, game=True, require_focus=True)
        runtime = self._new_control(session, identity, window, game=True, require_focus=True)

        def revalidate() -> Box | None:
            fresh = self._screen_observation(identity, session, game=True)
            classified = classify_character_select_capture(
                fresh.reader,
                fresh.ocr,
                fresh.image,  # type: ignore[arg-type]
                expected_selected_name=self.config.character_select_name,
            )
            if (
                classified.kind != evidence.kind
                or classified.reason != evidence.reason
                or classified.control_text != evidence.control_text
            ):
                return None
            return _evidence_box(classified)

        def action_guard() -> None:
            self._guard_identity(session, identity, window, game=True, require_focus=True)
            self._require_disk_build()

        try:
            click_control(
                window,
                control_box,
                screen.window.display,
                tool=self._input_tool(),
                control_run=runtime.run,
                environment=lambda: self.session_provider.current().environment,
                guard=action_guard,
                revalidate=revalidate,
                action="enter_world",
            )
        finally:
            self._close_control(runtime)

    def _activate(
        self, session: DesktopSession, identity: ProcessIdentity, window: int, *, game: bool
    ) -> None:
        self._guard_identity(session, identity, window, game=game, require_focus=False)
        runtime = self._new_control(session, identity, window, game=game, require_focus=False)
        try:
            result = runtime.run(
                [self._input_tool().proc_path, "windowactivate", "--sync", str(window)],
                timeout=5.0,
                env=dict(session.environment),
                pass_fds=(self._input_tool().image.fd,),
            )
        finally:
            self._close_control(runtime)
        if result.returncode != 0:
            raise _BlockEpisode("window_activation_failed")

    def _repair_fullscreen(self, session: DesktopSession, identity: ProcessIdentity, window: int) -> None:
        xprop = self._pinned_control_tool("xprop")
        wmctrl = self._pinned_control_tool("wmctrl")

        def guard() -> None:
            self._guard_identity(session, identity, window, game=True, require_focus=False)

        runtime = self._new_control(
            session,
            identity,
            window,
            game=True,
            require_focus=False,
            extra_tools={"xprop": xprop, "wmctrl": wmctrl},
            guard=guard,
        )
        commands = (
            (xprop, ["-id", str(window), "-remove", "WM_NORMAL_HINTS"]),
            (
                xprop,
                [
                    "-id",
                    str(window),
                    "-f",
                    "_MOTIF_WM_HINTS",
                    "32c",
                    "-set",
                    "_MOTIF_WM_HINTS",
                    "0x2, 0x0, 0x0, 0x0, 0x0",
                ],
            ),
            (wmctrl, ["-i", "-r", str(window), "-b", "remove,maximized_vert,maximized_horz"]),
            (wmctrl, ["-i", "-r", str(window), "-b", "add,fullscreen"]),
        )
        try:
            for tool, args in commands:
                guard()
                result = runtime.run(
                    [tool.proc_path, *args],
                    timeout=5.0,
                    env=dict(session.environment),
                    pass_fds=(tool.fd,),
                )
                if result.returncode != 0:
                    raise _BlockEpisode("window_manager_request_failed")
        finally:
            self._close_control(runtime)

    def _close_control(self, runtime: ControlChildRunner) -> None:
        """Forget a control runner only after its child and held-input cleanup succeed."""

        try:
            runtime.close()
        except ControlChildError:
            self._control = runtime
            raise
        if runtime.child_pid is not None or runtime.cleanup_failed:
            self._control = runtime
            raise ControlChildError("control helper cleanup did not complete")
        if self._control is runtime:
            self._control = None

    def _new_control(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        window: int,
        *,
        game: bool,
        require_focus: bool,
        extra_tools: Mapping[str, PinnedControlTool] | None = None,
        guard: Callable[[], None] | None = None,
    ) -> ControlChildRunner:
        tool = self._input_tool()
        runner = ControlChildRunner(
            self._lease,
            tool,
            environment=lambda: self.session_provider.current().environment,
            stopping=self._stopping,
            guard=guard
            or (
                lambda: self._guard_identity(
                    session, identity, window, game=game, require_focus=require_focus
                )
            ),
            extra_tools=extra_tools,
            window_xid=window,
        )
        self._control = runner
        return runner

    def _input_tool(self) -> PinnedInputTool:
        if self._tool is None:
            self._tool = self._input_tool_factory()
        return self._tool

    def _pinned_control_tool(self, name: str, path: str | None = None) -> PinnedControlTool:
        key = f"{name}:{path or ''}"
        tool = self._control_tools.get(key)
        if tool is None:
            if path is None:
                tool = self._control_tool_factory(name)
            else:
                tool = self._control_tool_factory(name, path)
            self._control_tools[key] = tool
        return tool

    def _require_disk_build(self) -> None:
        path = Path(self.config.game_root) / ".build.info"
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _BUILD_INFO_LIMIT:
                raise _BlockEpisode("client_build_info_malformed")
            chunks = bytearray()
            while block := os.read(descriptor, min(65536, _BUILD_INFO_LIMIT + 1 - len(chunks))):
                chunks.extend(block)
                if len(chunks) > _BUILD_INFO_LIMIT:
                    raise _BlockEpisode("client_build_info_malformed")
            raw = chunks.decode("utf-8", errors="strict")
        except _BlockEpisode:
            raise
        except (OSError, UnicodeDecodeError):
            raise _BlockEpisode("client_build_info_unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        lines = raw.splitlines()
        if len(lines) < 2:
            raise _BlockEpisode("client_build_info_malformed")
        headers = [field.split("!", 1)[0] for field in lines[0].split("|")]
        if len(headers) != len(set(headers)) or not {"Product", "Active", "Version"}.issubset(headers):
            raise _BlockEpisode("client_build_info_malformed")
        records: list[dict[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            fields = line.split("|")
            if len(fields) != len(headers):
                raise _BlockEpisode("client_build_info_malformed")
            records.append(dict(zip(headers, fields, strict=True)))
        active = [
            row for row in records if row.get("Product") == self.config.product and row.get("Active") == "1"
        ]
        if not active:
            raise _BlockEpisode("client_build_active_product_missing")
        if len(active) != 1:
            raise _BlockEpisode("client_build_active_product_ambiguous")
        if active[0].get("Version") != self.config.build:
            raise _BlockEpisode("client_build_mismatch")

    def _guard_launch(self, session: DesktopSession) -> None:
        if not self._active or not self._lease.held:
            raise _HoldEpisode("gui_lease_unavailable")
        self._guard_common(session)
        if self._game_identities():
            raise _HoldEpisode("owned_game_process_appeared_before_bnet_launch")
        if _battle_net_identities(self.config.wine_prefix):
            raise _HoldEpisode("battle_net_process_appeared_before_launch")

    def _guard_identity(
        self,
        session: DesktopSession,
        identity: ProcessIdentity,
        window: int,
        *,
        game: bool,
        require_focus: bool,
    ) -> None:
        self._guard_common(session)
        current = self._identities_for(identity, game=game)
        if current != (identity,):
            raise _HoldEpisode("process_identity_changed")
        if not game and self._game_identities():
            raise _HoldEpisode("owned_game_process_appeared_before_battle_net_action")
        if not game and self._battle_net_unknown_window_candidates(identity, session):
            raise _HoldEpisode("battle_net_window_inventory_changed")
        if not game:
            updates = self._battle_net_updates_window(identity, session)
            if updates.count or updates.foreign_count:
                raise _HoldEpisode("battle_net_updates_window_appeared_before_main_action")
        state = self._game_window(identity, session) if game else self._battle_net_window(identity, session)
        if state.count != 1 or state.xid != window:
            raise _HoldEpisode("window_identity_changed")
        if require_focus and not state.focused:
            raise _HoldEpisode("window_focus_changed")

    def _guard_common(self, session: DesktopSession) -> None:
        if self._stopping():
            raise _HoldEpisode("stop_requested")
        if not self._active or not self._lease.held:
            raise _HoldEpisode("gui_lease_unavailable")
        try:
            current = self.session_provider.current()
        except DesktopSessionUnavailable as error:
            raise _HoldEpisode(error.reason) from error
        if current.fingerprint != session.fingerprint:
            raise _HoldEpisode("desktop_session_changed")
        if maintenance_blocker() is not None:
            raise _HoldEpisode("automation_maintenance_or_uncertain")
        active, _reason = self._presence_active(current.environment)
        if active:
            raise _HoldEpisode("physical_presence_hold")

    @staticmethod
    def _presence_active(environment: Mapping[str, str]) -> tuple[bool, str]:
        def run(argv: list[str], timeout: float) -> str:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=dict(environment),
            )
            return result.stdout

        return Presence(runner=run).human_active()

    def _identities_for(self, identity: ProcessIdentity, *, game: bool) -> tuple[ProcessIdentity, ...]:
        return self._game_identities() if game else _battle_net_identities(identity.wine_prefix)

    def _launch_battle_net(self, session: DesktopSession) -> None:
        wine = _reviewed_wine_path()
        bnet = _battle_net_executable(self.config.wine_prefix)
        if not bnet.is_file():
            raise _BlockEpisode("battle_net_executable_missing")
        config = _battle_net_config(self.config.wine_prefix)
        if not _single_instance_enabled(config):
            raise _BlockEpisode("battle_net_single_instance_not_enabled")
        environment = _wine_environment(session.environment, self.config.wine_prefix)
        self._guard_launch(session)
        if _battle_net_identities(self.config.wine_prefix):
            raise _HoldEpisode("battle_net_process_appeared_before_launch")
        if not _single_instance_enabled(_battle_net_config(self.config.wine_prefix)):
            raise _BlockEpisode("battle_net_single_instance_not_enabled")
        self._guard_launch(session)
        if self._stopping():
            raise _HoldEpisode("stop_requested")
        self._popen(
            [wine, str(bnet)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )

    def _publish(
        self,
        outcome: StartupOutcome,
        phase: str,
        reason: str,
        *,
        retry: float | None = None,
        deadline: float | None = None,
        action: str = "none",
        native: bool = False,
        identity: ProcessIdentity | None,
        window: int | None,
    ) -> StartupStatus:
        self._retry_mono = retry
        started_at = self._started_wall if self._started_wall != 0.0 else self._wall()
        episode_deadline = self._episode_deadline if self._started_mono is not None else None
        status = StartupStatus(
            outcome,
            phase,
            reason,
            action,
            started_at,
            self._wall_deadline(deadline if deadline is not None else episode_deadline),
            self._wall_deadline(retry),
            native,
            identity,
            window,
        )
        self._last_status = status
        if outcome in {"ready", "blocked", "failed", "stopped"}:
            self._terminal_status = status
        return status

    def _wait(
        self,
        phase: str,
        reason: str,
        *,
        identity: ProcessIdentity | None = None,
        window: int | None = None,
        deadline: float | None = None,
        action: str = "none",
    ) -> StartupStatus:
        return self._publish(
            "waiting",
            phase,
            reason,
            retry=min(self._clock() + _RETRY_SECONDS, deadline or self._episode_deadline),
            deadline=deadline,
            action=action,
            identity=identity,
            window=window,
        )

    def _finish(
        self,
        outcome: StartupOutcome,
        phase: str,
        reason: str,
        identity: ProcessIdentity | None = None,
        window: int | None = None,
        *,
        native: bool = False,
        action: str = "none",
    ) -> StartupStatus:
        if outcome != "ready":
            self._last_native = None
        if not self._release_episode():
            outcome = "failed"
            reason = "control_cleanup_failed"
        retry = (
            self._clock() + 5.0 if outcome == "blocked" and reason in _SAFE_REOBSERVATION_REFUSALS else None
        )
        status = self._publish(
            outcome,
            phase,
            reason,
            retry=retry,
            action=action,
            native=native,
            deadline=self._episode_deadline if self._started_mono is not None else None,
            identity=identity,
            window=window,
        )
        if outcome in {"blocked", "failed", "stopped"}:
            self._terminal_status = status
        if outcome == "ready":
            self._terminal_status = status
        return status

    def _release_episode(self) -> bool:
        if self._control is not None:
            control = self._control
            try:
                control.close()
            except ControlChildError:
                return False
            if control.child_pid is not None or control.cleanup_failed:
                return False
            self._control = None
        if self._tool is not None:
            try:
                self._tool.close()
            except OSError:
                pass
            finally:
                self._tool = None
        for tool in self._control_tools.values():
            try:
                tool.close()
            except OSError:
                pass
        self._control_tools.clear()
        if self._lease_context is not None:
            try:
                self._lease_context.__exit__(None, None, None)
            except Exception:
                return False
            else:
                self._lease_context = None
        self._active = False
        self._session_fingerprint = None
        self._last_screen = None
        self._last_ui = None
        return True

    def _phase(self) -> str:
        if self._restart_identity is not None or self._closing_identity is not None:
            return "game_close"
        if self._last_identity is not None:
            return "world_loading"
        if self._bnet_launched or self._login_deadline is not None:
            return "battle_net_login"
        return "desktop"

    def _wall_deadline(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        delta = max(0.0, deadline - self._clock())
        return self._wall() + delta


def _battle_net_identities(
    wine_prefix: str, *, proc_root: Path = Path("/proc")
) -> tuple[ProcessIdentity, ...]:
    expected_unix = str(_battle_net_executable(wine_prefix))
    expected_argv0 = {
        expected_unix,
        r"C:\Program Files (x86)\Battle.net\Battle.net.exe",
    }
    found: list[ProcessIdentity] = []
    unsupported_candidate = False
    for proc in proc_root.iterdir():
        if not proc.name.isdecimal():
            continue
        pid = int(proc.name)
        before = process_start_ticks(pid)
        if before is None:
            continue
        try:
            uid = proc.stat().st_uid
            if uid != os.getuid():
                continue
            parts = (proc / "cmdline").read_bytes().split(b"\0")
            argv0 = parts[0].decode("utf-8", errors="strict")
            if argv0 not in expected_argv0:
                continue
        except (OSError, UnicodeDecodeError):
            continue
        try:
            environ = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            if process_start_ticks(pid) == before:
                unsupported_candidate = True
            continue
        if f"WINEPREFIX={wine_prefix}".encode() not in environ:
            continue
        if process_start_ticks(pid) != before:
            continue
        if any(part for part in parts[1:]):
            unsupported_candidate = True
            continue
        found.append(ProcessIdentity(pid, before, uid, argv0, wine_prefix))
    if not found and unsupported_candidate:
        raise _BlockEpisode("battle_net_process_arguments_unexpected")
    return tuple(sorted(found, key=lambda item: item.pid))


def _battle_net_executable(wine_prefix: str) -> Path:
    return Path(wine_prefix) / "drive_c" / "Program Files (x86)" / "Battle.net" / "Battle.net.exe"


def _battle_net_config(wine_prefix: str) -> str:
    user = pwd.getpwuid(os.getuid()).pw_name
    path = (
        Path(wine_prefix)
        / "drive_c"
        / "users"
        / user
        / "AppData"
        / "Roaming"
        / "Battle.net"
        / "Battle.net.config"
    )
    try:
        if path.stat().st_size > _BATTLE_NET_CONFIG_LIMIT:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _single_instance_enabled(raw: str) -> bool:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    client = value.get("Client")
    if not isinstance(client, dict):
        return False
    enabled = client.get("SingleInstance")
    return type(enabled) is str and enabled == "true"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reviewed_wine_path() -> str:
    value = os.environ.get("CROWLEY_WINE")
    if not value or not Path(value).is_absolute() or not os.access(value, os.X_OK):
        raise _BlockEpisode("reviewed_wine_executable_unavailable")
    return value


def _wine_environment(base: Mapping[str, str], wine_prefix: str) -> dict[str, str]:
    environment = dict(base)
    environment.update(
        {
            "WINEPREFIX": wine_prefix,
            "WINEARCH": "win64",
            "WINEESYNC": "1",
            "STAGING_SHARED_MEMORY": "1",
            "DXVK_CONFIG_FILE": f"{wine_prefix}/dxvk.conf",
            "DXVK_HUD": "compiler",
            "DXVK_STATE_CACHE_PATH": wine_prefix,
            "__GL_DXVK_OPTIMIZATIONS": "1",
            "__GL_SHADER_DISK_CACHE": "1",
            "__GL_SHADER_DISK_CACHE_PATH": wine_prefix,
            "WINEDEBUG": "-all",
        }
    )
    return environment


def _window_box(window: WindowState) -> Box | None:
    if window.outer_rect is None:
        return None
    x, y, width, height = window.outer_rect
    if width <= 0 or height <= 0:
        return None
    return Box(x, y, width, height)


def _is_updates_rect(rect: tuple[int, int, int, int] | None) -> bool:
    return rect is not None and rect[2:] == (900, 600)


def _evidence_box(evidence: UIEvidence) -> Box | None:
    if evidence.control is None:
        return None
    return Box(*evidence.control)


def _safe_failure_reason(error: BaseException) -> str:
    if isinstance(error, StartupUIError):
        return error.args[0] if error.args and isinstance(error.args[0], str) else "startup_ui_failed"
    if isinstance(error, ControlChildCancelled):
        return "control_child_cancelled"
    if isinstance(error, ControlChildError):
        return "control_child_failed"
    if isinstance(error, BattleNetPlayError):
        return "startup_control_failed"
    if isinstance(error, ObserveError):
        return "desktop_observation_failed"
    if isinstance(error, subprocess.TimeoutExpired):
        return "bounded_command_timed_out"
    return "startup_runtime_failed"


__all__ = ["StartupRecovery", "StartupStatus"]
