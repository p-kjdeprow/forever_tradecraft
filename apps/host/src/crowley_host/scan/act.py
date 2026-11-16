"""Scanner-aware actions composed from the standalone desktop primitives."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from crowley_automation import desktop_input
from crowley_automation.gui import process

from .policy import INPUT_ACTIONS, Action, Decision, Target, allowed_commands


class MaintenanceBlocked(desktop_input.ActError):
    """An automation operation became active after policy selected maintenance."""


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
        launcher: desktop_input.LauncherRun,
        terminate: Callable[[Sequence[int]], int] = process.terminate,
        maintenance_blocker: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._keys, self._activate, self._lease = keys, activate, lease
        self._presence, self._target, self._matcher = presence, target, matcher
        self.launcher = launcher
        self._terminate = terminate
        self._maintenance_blocker = maintenance_blocker

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

    @property
    def launcher_running(self) -> bool:
        return self.launcher.running

    def poll_launcher(self) -> tuple[bool, str]:
        return self.launcher.poll()

    def execute(self, decision: Decision, window_xid: int | None) -> str:
        """Carry out a scanner policy decision after its observed preconditions."""

        action = decision.action
        if action in (Action.WAIT, Action.HOLD_HUMAN, Action.FAULT):
            return "none"
        if action is Action.RELAUNCH:
            with self._lease.hold():
                human, _reason = self._presence.human_active()
                if human:
                    return desktop_input.SKIPPED_HUMAN
                self._check_maintenance()
                try:
                    self._terminate(self._matcher.pids())
                except RuntimeError as exc:
                    raise desktop_input.ActError(
                        f"owned game process could not be terminated: {exc}"
                    ) from exc
            self.launcher.start()
            return "relaunch_started"
        if action is Action.ACTIVATE_WINDOW or action in INPUT_ACTIONS:
            with self._lease.hold():
                human, _reason = self._presence.human_active()
                if human:
                    return desktop_input.SKIPPED_HUMAN
                self._check_maintenance()
                if action is Action.ACTIVATE_WINDOW:
                    if window_xid is None:
                        raise desktop_input.ActError("no game window to activate")
                    self._activate(window_xid)
                elif action in (Action.OPEN_CHAT, Action.PRESS_RETURN):
                    self._keys.press("Return")
                elif action in (Action.PRESS_ESCAPE, Action.CLOSE_AH):
                    self._keys.press("Escape")
                elif action is Action.PRESS_INTERACT:
                    self._keys.chord(desktop_input.INTERACT_CHORD)
                elif action is Action.TYPE_COMMAND:
                    command = decision.command
                    if command not in allowed_commands(self._target):
                        raise desktop_input.ActError(f"command is not allowlisted: {command!r}")
                    self._keys.type_text(command)
                else:
                    raise desktop_input.ActError(f"unhandled input action {action}")
            return str(action)
        raise desktop_input.ActError(f"unhandled action {action}")
