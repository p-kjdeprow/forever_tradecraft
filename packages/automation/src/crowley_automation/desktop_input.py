"Desktop GUI lease, keyboard input and guarded window activation."

from __future__ import annotations

import errno
import fcntl
import math
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from crowley_common.platform.paths import XdgDirs

from crowley_automation.gui import keyboard
from crowley_automation.gui.input_tool import PinnedInputTool

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
        self._inherited_fd: int | None = None
        self._fd: int | None = None
        self.held = False

    @property
    def fd(self) -> int | None:
        """The held descriptor, for a deliberate child-process handoff."""

        return self._fd

    @property
    def path(self) -> Path:
        """Canonical lock-file path whose descriptor this lease owns."""

        return self._path

    @classmethod
    def from_inherited_fd(cls, path: Path, fd: int) -> GuiLease:
        """Adopt the launcher's real lock descriptor without releasing its lock."""

        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
            raise ActError("inherited GUI lease descriptor is invalid")
        lease = cls(path)
        lease._inherited_fd = fd
        return lease

    def _verify_inherited_fd(self, fd: int) -> None:
        parent = self._path.parent
        try:
            parent_info = parent.lstat()
            path_info = self._path.lstat()
            fd_info = os.fstat(fd)
        except OSError as exc:
            raise ActError(f"inherited GUI lease cannot be inspected: {exc}") from exc
        if (
            stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
            or stat.S_IMODE(parent_info.st_mode) != 0o700
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_uid != os.getuid()
            or stat.S_IMODE(path_info.st_mode) != 0o600
            or not stat.S_ISREG(fd_info.st_mode)
            or fd_info.st_uid != os.getuid()
            or stat.S_IMODE(fd_info.st_mode) != 0o600
            or (fd_info.st_dev, fd_info.st_ino) != (path_info.st_dev, path_info.st_ino)
        ):
            raise ActError("inherited GUI lease is not the private canonical lock file")

    @contextmanager
    def hold(self) -> Iterator[None]:
        if self._inherited_fd is not None:
            fd = self._inherited_fd
            self._verify_inherited_fd(fd)
            try:
                # With an inherited descriptor this is the same open file
                # description as the launcher's lock.  LOCK_NB also makes an
                # unlocked descriptor safe: this process then owns the lock.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ActError("inherited GUI lease is held by another process") from exc
            self.held = True
            self._fd = fd
            try:
                yield
            finally:
                # Closing this descriptor in the child leaves the parent's
                # inherited open file description and lock alive.
                self.held = False
                self._fd = None
            return
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
            self._fd = fd
            try:
                yield
            finally:
                self.held = False
                self._fd = None
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
        env: Mapping[str, str] | Callable[[], Mapping[str, str]],
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        query_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        before_emit: Callable[[], None] | None = None,
    ) -> None:
        self._tool, self._env, self._run = tool, env, run
        self._query_run = query_run
        self._before_emit = before_emit

    def _environment(self) -> dict[str, str]:
        values = self._env() if callable(self._env) else self._env
        return dict(values)

    def _keymap(self) -> dict[str, int]:
        done = self._query_run(
            ["xmodmap", "-pke"],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_S,
            env=self._environment(),
        )
        if done.returncode != 0:
            raise ActError(f"xmodmap exited {done.returncode}")
        return parse_keymap(done.stdout)

    def _send(self, argv: list[str], held: frozenset[str]) -> None:
        command = [self._tool.proc_path, *argv[1:]]
        try:
            if self._before_emit is not None and argv[1] != "keyup":
                self._before_emit()
            done = self._run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_S,
                env=self._environment(),
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
                    env=self._environment(),
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
        actions = keyboard.type_text_argv(text, self._keymap())
        if not actions:
            return
        argv = ["xdotool"]
        held: set[str] = set()
        for command, controls in actions:
            argv.extend(command[1:])
            held.update(controls)
        self._send(argv, frozenset(held))

    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None:
        """Hold one measured movement key briefly, after a fresh caller guard."""

        if key not in {"Up", "Left", "Right"}:
            raise ActError("bounded movement key must be Up, Left, or Right")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ActError("movement hold duration must be numeric")
        duration = float(seconds)
        if not math.isfinite(duration) or not 0 < duration <= 0.25:
            raise ActError("movement hold duration must be finite and in (0, 0.25]")
        keymap = self._keymap()
        code = keymap.get(key)
        if isinstance(code, bool) or not isinstance(code, int) or not 8 <= code <= 255:
            raise ActError(f"movement key {key!r} has no measured keycode")
        guard()
        physical = f"{code:03d}"
        self._send(
            ["xdotool", "keydown", physical, "sleep", f"{duration:.9f}", "keyup", physical],
            frozenset({physical}),
        )


# --- window activation --------------------------------------------------------------------


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


# --- actor --------------------------------------------------------------------------------


class Presence(Protocol):
    def human_active(self) -> tuple[bool, str]: ...


SKIPPED_HUMAN = "skipped:human_active"
