"""Scanner-aware actions admitted against one fresh host observation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from crowley_automation import desktop_input
from crowley_automation.desktop_observe import (
    NativeWorldObservation,
    ObserveError,
    WindowSource,
)
from crowley_automation.desktop_session import DesktopSessionProvider, DesktopSessionUnavailable
from crowley_automation.gui import process
from crowley_automation.gui.control_child import (
    ControlChildCancelled,
    ControlChildError,
    ControlChildRunner,
)
from crowley_automation.gui.process import ProcessIdentity

from .observe import automation_busy, blocking_reason
from .policy import (
    INPUT_ACTIONS,
    RESTART_REASONS,
    STALE_FRAME_S,
    Action,
    Decision,
    LoopState,
    Observation,
    Target,
    allowed_commands,
)

_LOG = logging.getLogger(__name__)


class MaintenanceBlocked(desktop_input.ActError):
    """An automation operation became active after policy selected maintenance."""


class ActionNotAdmitted(desktop_input.ActError):
    """A fresh boundary check rejected a stale or changed observation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Recovery(Protocol):
    def request_restart(self, identity: ProcessIdentity, reason: str) -> Any: ...


class SessionEnvironment(Protocol):
    def refresh(self, session: Any) -> None: ...

    def __call__(self) -> Mapping[str, str]: ...


class ScanAdmissionSource(Protocol):
    @property
    def decode_error(self) -> str: ...

    def observe(self, state: LoopState) -> Observation: ...


class Actor:
    def __init__(
        self,
        *,
        keys: desktop_input.KeySender,
        activate: Callable[[int], None],
        lease: desktop_input.GuiLease,
        presence: desktop_input.Presence,
        target: Target,
        matcher: process.OwnedGameMatcher,
        window: WindowSource,
        session_provider: DesktopSessionProvider,
        session_environment: SessionEnvironment,
        scan_observer: ScanAdmissionSource,
        native_observe: Callable[[], NativeWorldObservation],
        recovery: Recovery,
        stopping: Callable[[], bool] = lambda: False,
        control_runner: ControlChildRunner | None = None,
        maintenance_blocker: Callable[[], str | None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keys, self._activate, self._lease = keys, activate, lease
        self._presence, self._target, self._matcher = presence, target, matcher
        self._window = window
        self._session_provider = session_provider
        self._session_environment = session_environment
        self._scan_observer = scan_observer
        self._native_observe = native_observe
        self._recovery = recovery
        self._stopping = stopping
        self._control_runner = control_runner
        self._maintenance_blocker = maintenance_blocker
        self._clock = clock
        self._observation: Observation | None = None
        self._decision: Decision | None = None
        self._lease_context: Any | None = None
        self._cleanup_latched = False
        self._closing = False
        self._closed = False

    def _control_cleanup_pending(self) -> bool:
        runner = self._control_runner
        return runner is not None and (
            runner.child_pid is not None or bool(runner.possible_down) or runner.cleanup_failed
        )

    def _enter_lease_context(self) -> None:
        if self._lease_context is not None:
            raise ActionNotAdmitted("control_cleanup_failed")
        context = self._lease.hold()
        context.__enter__()
        self._lease_context = context

    def _release_lease_context(self) -> None:
        context = self._lease_context
        if context is None:
            return
        try:
            context.__exit__(None, None, None)
        except Exception:
            self._cleanup_latched = True
            raise
        else:
            self._lease_context = None

    @contextmanager
    def _action_lease(self):
        if self._closed or self._closing:
            raise ActionNotAdmitted("actor_closed")
        if self._cleanup_latched or self._control_cleanup_pending():
            self._cleanup_latched = True
            raise ActionNotAdmitted("control_cleanup_failed")
        self._enter_lease_context()
        try:
            yield
        finally:
            if self._control_cleanup_pending():
                self._cleanup_latched = True
                raise ControlChildError("control helper cleanup did not complete")
            else:
                self._release_lease_context()

    def _check_stopping(self) -> None:
        if self._stopping():
            raise ActionNotAdmitted("stopping")

    def _check_maintenance(self) -> None:
        try:
            reason = self._maintenance_blocker()
        except (OSError, RuntimeError, ValueError):
            reason = "automation_state_invalid"
        if reason is not None:
            if reason not in {
                "automation_running",
                "automation_uncertain",
                "automation_state_invalid",
            }:
                reason = "automation_state_invalid"
            raise MaintenanceBlocked(reason)

    def _check_capture_age(self, observation: Observation) -> None:
        snap = observation.snapshot
        if (
            snap is None
            or observation.native_boot != snap.boot
            or observation.frames_seen_this_process <= 0
            or observation.last_frame_at is None
        ):
            raise ActionNotAdmitted("native_observation_changed")
        now = self._clock()
        if (
            now - snap.received_at < -1.0
            or now - snap.received_at >= STALE_FRAME_S
            or now - observation.last_frame_at < -1.0
            or now - observation.last_frame_at >= STALE_FRAME_S
        ):
            raise ActionNotAdmitted("scanner_observation_stale")

    def _check_native(self, observation: Observation, decision: Decision, *, emission: bool = False) -> None:
        snap = observation.snapshot
        if not emission:
            self._check_capture_age(observation)
        if not observation.native_ready or not observation.window_fullscreen:
            raise ActionNotAdmitted("native_world_not_ready")
        if observation.native_automation_busy or snap is None or snap.automation_busy:
            raise ActionNotAdmitted("automation_native_busy")
        try:
            current = self._native_observe()
        except (ObserveError, DesktopSessionUnavailable) as exc:
            message = f"native observation failed: {type(exc).__name__}: {' '.join(str(exc).split())}"
            _LOG.warning("%s", message[:200])
            raise ActionNotAdmitted("native_observation_changed") from None
        identity = observation.process_identity
        if (
            identity is None
            or current.identity != identity
            or current.window.xid != observation.window_xid
            or not current.window.ok
            or current.window.foreign_count > 0
            or not current.window.fullscreen
            or current.session_fingerprint != observation.session_fingerprint
        ):
            raise ActionNotAdmitted("native_observation_changed")
        value = current.snapshot
        if value is None or value["boot"] != observation.native_boot or value["boot"] != snap.boot:
            raise ActionNotAdmitted("native_world_changed")
        context = value["context"]
        expected_native = {
            "character": observation.target.character,
            "realm": observation.target.realm,
            "playerFaction": observation.target.faction,
            "clientBuild": observation.target.build,
            "interface": observation.target.interface,
            "nativeRegion": observation.target.native_region,
            "addonVersion": observation.target.addon_version,
        }
        if any(context[key] != expected for key, expected in expected_native.items()):
            raise ActionNotAdmitted("native_identity_changed")
        if automation_busy(value):
            raise ActionNotAdmitted("automation_native_busy")
        if blocking_reason(value["blockingUI"]) != snap.blocking_ui:
            raise ActionNotAdmitted("blocking_ui_changed")
        fresh_context = value["context"]
        fresh_auctioneer = value["auctioneer"]
        if (
            value["ahOpen"] != snap.ah_open
            or fresh_context["zone"] != snap.zone
            or fresh_auctioneer["targetPresent"] != snap.target_present
            or fresh_auctioneer["targetName"] != snap.auctioneer_name
            or fresh_auctioneer["rangeAvailable"] != snap.range_available
            or fresh_auctioneer["withinTradeRange"] != snap.in_range
            or fresh_auctioneer["bindingAvailable"] != snap.binding_available
            or fresh_auctioneer["ctrlF12Action"] != snap.binding_action
            or fresh_auctioneer["interactionMode"] != snap.interaction_mode
        ):
            raise ActionNotAdmitted("native_action_state_changed")
        chat = value["chatEdit"]
        if decision.action is Action.TYPE_COMMAND and decision.command not in allowed_commands(
            observation.target
        ):
            raise desktop_input.ActError("command is not allowlisted")
        if decision.action is Action.TYPE_COMMAND and emission:
            text = chat["text"]
            if (
                not chat["active"]
                or not chat["focused"]
                or not isinstance(text, str)
                or not decision.command.startswith(text)
            ):
                raise ActionNotAdmitted("chat_prefix_changed")
        elif (
            chat["active"] != snap.chat_active
            or chat["focused"] != snap.chat_focused
            or chat["text"] != snap.chat_text
        ):
            raise ActionNotAdmitted("chat_state_changed")
        if decision.action is Action.TYPE_COMMAND and not emission:
            if (
                not chat["active"]
                or not chat["focused"]
                or chat["text"] != ""
                or decision.command not in allowed_commands(observation.target)
            ):
                raise ActionNotAdmitted("chat_not_ready_for_command")
        if decision.action is Action.PRESS_RETURN:
            if (
                not chat["active"]
                or not chat["focused"]
                or chat["text"] != decision.command
                or decision.command not in allowed_commands(observation.target)
            ):
                raise ActionNotAdmitted("chat_submit_state_changed")
        if decision.action is Action.PRESS_INTERACT and (
            snap.ah_open
            or not snap.target_present
            or snap.auctioneer_name != observation.target.auctioneer
            or not snap.range_available
            or not snap.in_range
            or not snap.binding_available
            or snap.binding_action != "INTERACTTARGET"
            or snap.interaction_mode not in ("keyboard", "any")
        ):
            raise ActionNotAdmitted("auctioneer_interaction_state_changed")
        expected = observation.target
        if (
            snap.character != expected.character
            or snap.realm != expected.realm
            or snap.faction != expected.faction
            or snap.house_key != expected.house_key
            or snap.plan_hash != expected.plan_hash
            or snap.build != expected.build
            or snap.interface != expected.interface
            or snap.native_region != expected.native_region
            or snap.addon_version != expected.addon_version
            or snap.scanner_addon_version != expected.scanner_addon_version
        ):
            raise ActionNotAdmitted("native_identity_changed")
        if snap.blocking_ui and not (
            decision.action is Action.PRESS_ESCAPE
            and decision.detail == "close_game_menu"
            and snap.blocking_ui == "game_menu_shown"
        ):
            raise ActionNotAdmitted("blocking_ui")

    def _check_dynamic(self, observation: Observation, decision: Decision, *, typing: bool = False) -> None:
        self._check_stopping()
        try:
            session = self._session_provider.current()
        except DesktopSessionUnavailable as exc:
            raise ActionNotAdmitted(exc.reason) from None
        if not observation.session_fingerprint or session.fingerprint != observation.session_fingerprint:
            raise ActionNotAdmitted("desktop_session_changed")
        self._session_environment.refresh(session)
        self._check_maintenance()
        human, _reason = self._presence.human_active()
        if human:
            raise ActionNotAdmitted("human_active")
        identity = observation.process_identity
        if identity is None or (
            not self._matcher.still_matches(identity) if typing else self._matcher.identities() != (identity,)
        ):
            raise ActionNotAdmitted("process_identity_changed")
        if typing:
            if observation.window_xid is None:
                raise ActionNotAdmitted("game_window_changed")
            owned, focused = self._window.focus_state(observation.window_xid, identity.pid)
            if not owned:
                raise ActionNotAdmitted("game_window_changed")
            if not focused:
                raise ActionNotAdmitted("window_focus_changed")
            return
        window = self._window.observe([identity.pid])
        if (
            not window.ok
            or window.foreign_count > 0
            or not window.fullscreen
            or window.xid is None
            or window.xid != observation.window_xid
        ):
            raise ActionNotAdmitted("game_window_changed")
        if decision.action is Action.ACTIVATE_WINDOW:
            if window.focused:
                raise ActionNotAdmitted("window_already_focused")
        elif decision.action in INPUT_ACTIONS and not window.focused:
            raise ActionNotAdmitted("window_focus_changed")

    def _check_scan_readiness(
        self, observation: Observation, decision: Decision, *, emission: bool = False
    ) -> None:
        """Require a fresh paired scanner publication at every ordinary input boundary."""

        try:
            current = self._scan_observer.observe(observation.state)
        except (ObserveError, DesktopSessionUnavailable):
            raise ActionNotAdmitted("scanner_observation_changed") from None
        if current.human_active:
            raise ActionNotAdmitted("human_active")
        if decision.action is Action.ACTIVATE_WINDOW:
            if current.window_focused:
                raise ActionNotAdmitted("window_already_focused")
        elif decision.action in INPUT_ACTIONS and not current.window_focused:
            raise ActionNotAdmitted("window_focus_changed")
        if current.native_automation_busy or (
            current.snapshot is not None and current.snapshot.automation_busy
        ):
            raise ActionNotAdmitted("automation_native_busy")
        if self._scan_observer.decode_error:
            raise ActionNotAdmitted("scanner_observation_changed")
        if (
            not current.native_ready
            or not current.window_ok
            or not current.window_fullscreen
            or current.snapshot is None
            or current.process_identity != observation.process_identity
            or current.window_xid != observation.window_xid
            or current.session_fingerprint != observation.session_fingerprint
            or current.native_boot != observation.native_boot
            or current.snapshot.boot != observation.native_boot
        ):
            raise ActionNotAdmitted("scanner_observation_changed")
        try:
            self._check_capture_age(current)
        except ActionNotAdmitted:
            raise ActionNotAdmitted("scanner_observation_stale") from None
        snap, expected = current.snapshot, observation.target
        if (
            snap.character != expected.character
            or snap.realm != expected.realm
            or snap.faction != expected.faction
            or snap.build != expected.build
            or snap.interface != expected.interface
            or snap.native_region != expected.native_region
            or snap.addon_version != expected.addon_version
            or snap.scanner_addon_version != expected.scanner_addon_version
            or snap.house_key != expected.house_key
            or snap.plan_hash != expected.plan_hash
        ):
            raise ActionNotAdmitted("scanner_identity_changed")
        original = observation.snapshot
        if original is None:
            raise ActionNotAdmitted("scanner_observation_changed")
        if (
            snap.phase != original.phase
            or snap.due_at != original.due_at
            or snap.last_saved_uid != original.last_saved_uid
            or snap.pending_ids != original.pending_ids
            or snap.pending_count != original.pending_count
            or snap.pending_cap != original.pending_cap
            or snap.pending_blocked != original.pending_blocked
            or snap.pending_reason != original.pending_reason
            or snap.loaded_acked_ids != original.loaded_acked_ids
        ):
            raise ActionNotAdmitted("scanner_state_changed")
        if (
            snap.ah_open != original.ah_open
            or snap.zone != original.zone
            or snap.target_present != original.target_present
            or snap.auctioneer_name != original.auctioneer_name
            or snap.range_available != original.range_available
            or snap.in_range != original.in_range
            or snap.binding_available != original.binding_available
            or snap.binding_action != original.binding_action
            or snap.interaction_mode != original.interaction_mode
            or snap.blocking_ui != original.blocking_ui
        ):
            raise ActionNotAdmitted("native_action_state_changed")
        if decision.action is Action.TYPE_COMMAND:
            if decision.command not in allowed_commands(expected):
                raise desktop_input.ActError("command is not allowlisted")
            chat_is_valid = (
                snap.chat_active
                and snap.chat_focused
                and (decision.command.startswith(snap.chat_text) if emission else snap.chat_text == "")
            )
            if not chat_is_valid:
                raise ActionNotAdmitted("chat_prefix_changed")
        elif decision.action is Action.PRESS_RETURN:
            if (
                not snap.chat_active
                or not snap.chat_focused
                or snap.chat_text != decision.command
                or decision.command not in allowed_commands(expected)
            ):
                raise ActionNotAdmitted("chat_submit_state_changed")
        elif (
            snap.chat_active != original.chat_active
            or snap.chat_focused != original.chat_focused
            or snap.chat_text != original.chat_text
        ):
            raise ActionNotAdmitted("chat_state_changed")

    def before_emit(self) -> None:
        """ControlChildRunner/XtestKeys guard for every individual pinned emission."""

        observation, decision = self._observation, self._decision
        if observation is None or decision is None:
            raise ActionNotAdmitted("no_admitted_action")
        self._check_dynamic(observation, decision, typing=decision.action is Action.TYPE_COMMAND)
        if decision.action is Action.TYPE_COMMAND:
            # Text entry cannot submit a command. Check the world at admission and
            # require fresh, exact full-text readback before the separate Return
            # action; screen capture between characters stalls normal typing.
            return
        self._check_native(observation, decision, emission=True)
        self._check_scan_readiness(observation, decision, emission=True)

    def during_emit(self) -> None:
        """Keep text entry interruptible without observing the screen mid-command."""

        if self._decision is not None and self._decision.action is Action.TYPE_COMMAND:
            self.before_emit()

    def execute(self, decision: Decision, observation: Observation) -> str:
        """Carry out one policy action after its original observation is revalidated."""

        action = decision.action
        if action in (Action.WAIT, Action.HOLD_HUMAN, Action.FAULT):
            return "none"
        if self._closed or self._closing:
            raise ActionNotAdmitted("actor_closed")
        if self._cleanup_latched or self._control_cleanup_pending():
            self._cleanup_latched = True
            raise ActionNotAdmitted("control_cleanup_failed")
        if action is Action.RELAUNCH:
            identity = observation.process_identity
            if identity is None or decision.detail not in RESTART_REASONS:
                raise ActionNotAdmitted("restart_not_admitted")
            self._check_stopping()
            status = self._recovery.request_restart(identity, decision.detail)
            if status.action != "wm_close_dispatched":
                raise ActionNotAdmitted(status.reason or status.outcome)
            return "relaunch_started"
        if action is Action.ACTIVATE_WINDOW or action in INPUT_ACTIONS:
            self._observation, self._decision = observation, decision
            try:
                with self._action_lease():
                    self._check_stopping()
                    self._check_native(observation, decision)
                    self._check_dynamic(observation, decision)
                    self._check_scan_readiness(observation, decision)
                    if action is Action.ACTIVATE_WINDOW:
                        if observation.window_xid is None:
                            raise ActionNotAdmitted("game_window_missing")
                        self._activate(observation.window_xid)
                    elif action in (Action.OPEN_CHAT, Action.PRESS_RETURN):
                        self._keys.press("Return")
                    elif action in (Action.PRESS_ESCAPE, Action.CLOSE_AH):
                        self._keys.press("Escape")
                    elif action is Action.PRESS_INTERACT:
                        self._keys.chord(desktop_input.INTERACT_CHORD)
                    elif action is Action.TYPE_COMMAND:
                        if decision.command not in allowed_commands(self._target):
                            raise desktop_input.ActError(f"command is not allowlisted: {decision.command!r}")
                        self._keys.type_text(decision.command)
                    else:
                        raise desktop_input.ActError(f"unhandled input action {action}")
            except desktop_input.ActError as exc:
                if "held elsewhere" in str(exc) or "launcher busy" in str(exc):
                    raise ActionNotAdmitted("gui_busy") from None
                raise
            except ControlChildCancelled as exc:
                if self._control_cleanup_pending():
                    self._cleanup_latched = True
                    raise ControlChildError("control child cleanup did not complete") from exc
                raise ActionNotAdmitted("stopping") from None
            except ControlChildError as exc:
                if self._control_cleanup_pending():
                    self._cleanup_latched = True
                    raise
                raise desktop_input.ActError("pinned control helper failed") from exc
            finally:
                self._observation, self._decision = None, None
            return str(action)
        raise desktop_input.ActError(f"unhandled action {action}")

    def close(self) -> None:
        """Reap and close the action control runner, releasing only known-down keys."""

        if self._closed:
            return
        self._closing = True
        runner = self._control_runner
        if runner is None:
            self._release_lease_context()
            self._closed = True
            return
        try:
            if self._lease_context is None and self._control_cleanup_pending():
                self._enter_lease_context()
            runner.close()
            if self._control_cleanup_pending():
                raise ControlChildError("control helper cleanup did not complete")
            self._release_lease_context()
        except Exception:
            self._cleanup_latched = True
            raise
        else:
            self._cleanup_latched = False
            self._closed = True
