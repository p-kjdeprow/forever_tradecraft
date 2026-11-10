"""Execute one policy `Decision`: keys, window activation, relaunch.

Every input action holds the exclusive GUI lease (`$XDG_RUNTIME_DIR/crowley/gui.lock`), the
same flock the launcher takes around its own input steps, and re-checks presence under
the lease.  Keys go through the pinned XTEST tool as numeric keycode transitions.  The scan
loop never moves the pointer.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from crowley.gui import keyboard, process
from crowley.gui.input_tool import PinnedInputTool
from crowley.gui.process import OwnedGameMatcher
from crowley.platform.paths import XdgDirs

from .policy import INPUT_ACTIONS, Action, Decision, Target, allowed_commands

LAUNCHER = "crowley-world-of-warcraft"
LAUNCHER_TIMEOUT_S = 600.0
LEASE_TIMEOUT_S = 30.0
COMMAND_TIMEOUT_S = 10.0
INTERACT_CHORD: tuple[keyboard.KeyStep, ...] = (
    ("press", "Control_L"),
    ("press", "F12"),
    ("release", "F12"),
    ("release", "Control_L"),
)


class ActError(RuntimeError):
    """An action could not be carried out; the loop reports a fault and ticks on."""


# --- GUI lease ----------------------------------------------------------------------------


def gui_lock_path() -> Path:
    return XdgDirs.from_environ(os.environ).crowley_runtime / "gui.lock"


class GuiLease:
    """Exclusive flock on the GUI lock file; the directory is private (0700)."""

    def __init__(self, path: Path, *, timeout: float = LEASE_TIMEOUT_S) -> None:
        self._path = path
        self._timeout = timeout
        self.held = False

    @contextmanager
    def hold(self) -> Iterator[None]:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ActError(f"GUI lease directory is not private: {parent}")
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            deadline = time.monotonic() + self._timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise ActError(f"GUI lease failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise ActError("GUI lease is held elsewhere (launcher busy)")
                time.sleep(0.05)
            self.held = True
            try:
                yield
            finally:
                self.held = False
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --- keys ---------------------------------------------------------------------------------


class KeySender(Protocol):
    def press(self, key: str) -> None: ...

    def chord(self, steps: tuple[keyboard.KeyStep, ...]) -> None: ...

    def type_text(self, text: str) -> None: ...


_KEYCODE_LINE = re.compile(r"^\s*keycode\s+(\d+)\s*=\s*(.*?)\s*$")


def parse_keymap(xmodmap_pke: str) -> dict[str, int]:
    """Level-0 keysym -> keycode, first keycode wins (as the calibrated driver measured it)."""

    keymap: dict[str, int] = {}
    for line in xmodmap_pke.splitlines():
        match = _KEYCODE_LINE.match(line)
        if match is None:
            continue
        symbols = [s for s in match.group(2).split() if s != "NoSymbol"]
        if symbols:
            keymap.setdefault(symbols[0], int(match.group(1)))
    if not keymap:
        raise ActError("xmodmap returned an empty keymap")
    return keymap


class XtestKeys:
    """Keys through the pinned XTEST tool, run from its sealed memfd image."""

    def __init__(
        self,
        tool: PinnedInputTool,
        env: Mapping[str, str],
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._tool, self._env, self._run = tool, dict(env), run

    def _keymap(self) -> dict[str, int]:
        done = self._run(
            ["xmodmap", "-pke"],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_S,
            env=self._env,
        )
        if done.returncode != 0:
            raise ActError(f"xmodmap exited {done.returncode}")
        return parse_keymap(done.stdout)

    def _send(self, argv: list[str], held: frozenset[str]) -> None:
        command = [self._tool.proc_path, *argv[1:]]
        try:
            done = self._run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_S,
                env=self._env,
                pass_fds=(self._tool.image.fd,),
            )
            failed = done.returncode != 0
        except (OSError, subprocess.SubprocessError) as exc:
            self._release(held)
            raise ActError(f"input tool failed: {exc}") from exc
        if failed:
            self._release(held)
            raise ActError(f"input tool exited {done.returncode}")

    def _release(self, held: frozenset[str]) -> None:
        """Best effort: no key may stay down after a failed transition."""

        for code in sorted(held, reverse=True):
            try:
                self._run(
                    [self._tool.proc_path, "keyup", code],
                    capture_output=True,
                    check=False,
                    timeout=2.0,
                    env=self._env,
                    pass_fds=(self._tool.image.fd,),
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def chord(self, steps: tuple[keyboard.KeyStep, ...]) -> None:
        argv, held = keyboard.transition_argv(steps, self._keymap())
        self._send(argv, held)

    def press(self, key: str) -> None:
        argv, held = keyboard.key_argv(key, self._keymap())
        self._send(argv, held)

    def type_text(self, text: str) -> None:
        for argv, held in keyboard.type_text_argv(text, self._keymap()):
            self._send(argv, held)


# --- window activation and launcher -------------------------------------------------------


def activate_window(env: Mapping[str, str], xid: int, *, run: Callable[..., Any] = subprocess.run) -> None:
    try:
        done = run(
            ["xdotool", "windowactivate", "--sync", str(xid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_S,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActError(f"window activation failed: {exc}") from exc
    if done.returncode != 0:
        raise ActError(f"window activation exited {done.returncode}")


class LauncherRun:
    """The launcher as a child process the loop polls, so ticks (and presence) continue."""

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = LAUNCHER_TIMEOUT_S,
    ) -> None:
        self._which, self._popen, self._clock, self._timeout = which, popen, clock, timeout
        self._child: Any = None
        self._started = 0.0

    @property
    def running(self) -> bool:
        return self._child is not None

    def start(self) -> None:
        if self._child is not None:
            raise ActError("launcher is already running")
        executable = self._which(LAUNCHER)
        if executable is None:
            raise ActError(f"{LAUNCHER} is not on PATH")
        self._child = self._popen([executable], stdin=subprocess.DEVNULL, start_new_session=True)
        self._started = self._clock()

    def poll(self) -> tuple[bool, str]:
        """(finished, detail).  A launcher past its timeout is killed and reported."""

        child = self._child
        if child is None:
            return True, "not running"
        code = child.poll()
        if code is None and self._clock() - self._started >= self._timeout:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()
            child.wait()
            self._child = None
            return True, "timeout"
        if code is None:
            return False, "running"
        self._child = None
        return True, f"exit={code}"


# --- actor --------------------------------------------------------------------------------


class Presence(Protocol):
    def human_active(self) -> tuple[bool, str]: ...


SKIPPED_HUMAN = "skipped:human_active"


class Actor:
    def __init__(
        self,
        *,
        keys: KeySender,
        activate: Callable[[int], None],
        lease: GuiLease,
        presence: Presence,
        target: Target,
        matcher: OwnedGameMatcher,
        launcher: LauncherRun,
        terminate: Callable[[Sequence[int]], int] = process.terminate,
    ) -> None:
        self._keys, self._activate, self._lease = keys, activate, lease
        self._presence, self._target, self._matcher = presence, target, matcher
        self.launcher = launcher
        self._terminate = terminate

    @property
    def launcher_running(self) -> bool:
        return self.launcher.running

    def poll_launcher(self) -> tuple[bool, str]:
        return self.launcher.poll()

    def execute(self, decision: Decision, window_xid: int | None) -> str:
        """Carry out *decision*; returns what was done (or why nothing was)."""

        action = decision.action
        if action in (Action.WAIT, Action.HOLD_HUMAN, Action.FAULT):
            return "none"
        if action is Action.RELAUNCH:
            self._terminate(self._matcher.pids())
            self.launcher.start()
            return "relaunch_started"
        if action is Action.ACTIVATE_WINDOW or action in INPUT_ACTIONS:
            with self._lease.hold():
                human, _reason = self._presence.human_active()
                if human:
                    return SKIPPED_HUMAN
                return self._input(decision, window_xid)
        raise ActError(f"unhandled action {action}")

    def _input(self, decision: Decision, window_xid: int | None) -> str:
        action = decision.action
        if action is Action.ACTIVATE_WINDOW:
            if window_xid is None:
                raise ActError("no game window to activate")
            self._activate(window_xid)
        elif action in (Action.OPEN_CHAT, Action.PRESS_RETURN):
            self._keys.press("Return")
        elif action in (Action.PRESS_ESCAPE, Action.CLOSE_AH):
            self._keys.press("Escape")
        elif action is Action.PRESS_INTERACT:
            self._keys.chord(INTERACT_CHORD)
        elif action is Action.TYPE_COMMAND:
            if decision.command not in allowed_commands(self._target):
                raise ActError("attempted to type a non-allowlisted command")
            self._keys.type_text(decision.command)
        else:
            raise ActError(f"unhandled input action {action}")
        return str(action)
