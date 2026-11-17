"""Parent-death-safe runner for the pinned, allowlisted XTEST control commands."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crowley_automation.desktop_input import GuiLease
from crowley_automation.gui.input_tool import PinnedInputTool
from crowley_automation.gui.pointer import PointerExecutionError, validate_pointer_motion_action
from crowley_automation.gui.process import process_start_ticks

_MAX_COMMAND_SECONDS = 30.0
_CLEANUP_SECONDS = 2.0
_REAP_SECONDS = 0.4
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_PDEATHSIG = 1
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEAL_WRITE = getattr(__import__("fcntl"), "F_SEAL_WRITE", 0x0008)
_SEAL_GROW = getattr(__import__("fcntl"), "F_SEAL_GROW", 0x0004)
_SEAL_SHRINK = getattr(__import__("fcntl"), "F_SEAL_SHRINK", 0x0002)
_SEAL_SEAL = getattr(__import__("fcntl"), "F_SEAL_SEAL", 0x0001)


class ControlChildError(RuntimeError):
    """A control helper could not be safely started, cancelled, or reaped."""


class ControlChildCancelled(ControlChildError):
    """The parent stop request cancelled one exact control helper."""


EnvironmentProvider = Mapping[str, str] | Callable[[], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class _InputImage:
    tool: PinnedInputTool

    @property
    def name(self) -> str:
        return "xdotool"

    @property
    def proc_path(self) -> str:
        return self.tool.proc_path

    @property
    def fd(self) -> int:
        return self.tool.image.fd

    @property
    def digest(self) -> str:
        return self.tool.digest


@dataclass(slots=True)
class PinnedControlTool:
    """A small allowlisted system control binary copied into a sealed image."""

    name: str
    proc_path: str
    fd: int
    digest: str

    @classmethod
    def load(cls, name: str, path: str | os.PathLike[str] | None = None) -> PinnedControlTool:
        if name not in {"xprop", "wmctrl"}:
            raise ControlChildError("system control binary is outside the allowlist")
        candidate = Path(path) if path is not None else Path("/usr/bin") / name
        source_fd = -1
        image = -1
        try:
            resolved = candidate.resolve(strict=True)
            source_fd = os.open(resolved, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(source_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or not info.st_mode & stat.S_IXUSR
                or info.st_size <= 0
                or info.st_size > _MAX_IMAGE_BYTES
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or info.st_uid not in {0, os.getuid()}
                or os.pread(source_fd, 4, 0) != b"\x7fELF"
            ):
                raise ControlChildError("system control binary is not a trusted regular executable")
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while block := os.read(source_fd, 1024 * 1024):
                digest.update(block)
                chunks.append(block)
            final_info = os.fstat(source_fd)
            if _file_identity(info) != _file_identity(final_info):
                raise ControlChildError("system control binary changed while pinning")
            image = _memfd_create(f"crowley-{name}")
            for block in chunks:
                _write_all(image, block)
            os.fchmod(image, 0o700)
            fcntl.fcntl(
                image,
                _F_ADD_SEALS,
                _SEAL_WRITE | _SEAL_GROW | _SEAL_SHRINK | _SEAL_SEAL,
            )
            pinned, image = image, -1
            return cls(name, str(resolved), pinned, digest.hexdigest())
        except (OSError, ValueError) as error:
            raise ControlChildError("system control binary could not be pinned") from error
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if image >= 0:
                os.close(image)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ControlChildError("control image write made no progress")
        view = view[written:]


def _memfd_create(name: str) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        creator = libc.memfd_create
    except AttributeError as error:
        raise ControlChildError("glibc memfd_create is unavailable") from error
    creator.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    creator.restype = ctypes.c_int
    descriptor = creator(name.encode("ascii"), 0x0001 | 0x0002)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return descriptor


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size, info.st_mtime_ns


class ControlChildRunner:
    """Synchronous control runner; it accepts only the pinned emitter's tiny grammar.

    ``run`` intentionally mirrors the small subset of ``subprocess.run`` used by
    ``XtestKeys``. Every launched helper inherits the already-held canonical GUI
    lease and dies on parent death. Real game and Battle.net processes never use
    this runner.
    """

    def __init__(
        self,
        lease: GuiLease,
        tool: PinnedInputTool,
        *,
        environment: EnvironmentProvider,
        stopping: Callable[[], bool],
        guard: Callable[[], None],
        extra_tools: Mapping[str, PinnedControlTool] | None = None,
        window_xid: int | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lease = lease
        self._tool = tool
        self._environment = environment
        self._stopping = stopping
        self._guard = guard
        self._extra_tools = dict(extra_tools or {})
        self._window_xid = window_xid
        self._popen = popen
        self._clock = monotonic
        self._sleep = sleep
        self._child: Any = None
        self._possible_down: set[str] = set()
        self._closed = False
        self.cleanup_failed = False

    @property
    def child_pid(self) -> int | None:
        return None if self._child is None else int(self._child.pid)

    @property
    def possible_down(self) -> frozenset[str]:
        return frozenset(self._possible_down)

    def run(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = False,
        timeout: float = 10.0,
        env: Mapping[str, str] | None = None,
        pass_fds: Sequence[int] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run one bounded allowlisted command through a direct PDEATHSIG child."""

        del capture_output, text, check
        if self._closed:
            raise ControlChildError("control runner is closed")
        if not self._lease.held or self._lease.fd is None:
            raise ControlChildError("control runner requires the held canonical GUI lease")
        pinned = self._resolve_tool(argv[0] if argv else "")
        if tuple(sorted(set(pass_fds))) != tuple(sorted({pinned.fd})):
            raise ControlChildError("control runner received unexpected descriptors")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout <= _MAX_COMMAND_SECONDS
        ):
            raise ControlChildError("control command timeout is outside its bound")
        args = tuple(argv[1:])
        operation = _classify(pinned.name, args, expected_window=self._window_xid)
        if operation != "release":
            if self._stopping():
                self.release_only()
                raise ControlChildCancelled("control cancelled by stop")
        try:
            result = self._execute(
                pinned,
                args,
                operation,
                float(timeout),
                cleanup=operation == "release",
                environment_overrides=env,
            )
        except ControlChildCancelled:
            self.release_only()
            raise
        except Exception:
            if operation != "release":
                self.release_only()
            raise
        if result.returncode == 0:
            self._clear_released(args)
        return subprocess.CompletedProcess(list(argv), result.returncode, result.stdout, result.stderr)

    def release_only(self) -> None:
        """Emit only recorded mouse/key releases under the still-held GUI lease."""

        if self._child is not None:
            self.cleanup_failed = True
            return
        if not self._possible_down:
            self.cleanup_failed = False
            return
        if not self._lease.held:
            self.cleanup_failed = True
            return
        controls = tuple(sorted(self._possible_down))
        args: list[str] = []
        for control in controls:
            if control == "button:1":
                args.extend(("mouseup", "1"))
            elif control.startswith("key:"):
                args.extend(("keyup", control.removeprefix("key:")))
        if not args:
            self.cleanup_failed = True
            return
        try:
            result = self._execute(
                _InputImage(self._tool), tuple(args), "release", _CLEANUP_SECONDS, cleanup=True
            )
        except (OSError, ControlChildError, subprocess.SubprocessError):
            self.cleanup_failed = True
            return
        if result.returncode == 0:
            self._possible_down.clear()
            self.cleanup_failed = False
        else:
            self.cleanup_failed = True

    def close(self) -> None:
        """Cancel/reap this runner's exact child, then make one release-only cleanup."""

        if self._closed:
            return
        if self._child is not None:
            self._cancel_and_reap()
        self.release_only()
        self._closed = not self.cleanup_failed

    def _environment_values(self, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
        values = self._environment() if callable(self._environment) else self._environment
        env = dict(values)
        if not env.get("DISPLAY") or not env.get("XAUTHORITY"):
            raise ControlChildError("current graphical session is unavailable")
        inherited_pythonpath = os.environ.get("PYTHONPATH")
        if inherited_pythonpath:
            env.setdefault("PYTHONPATH", inherited_pythonpath)
        if overrides:
            for key in ("DISPLAY", "XAUTHORITY"):
                if key in overrides and env.get(key) != overrides[key]:
                    raise ControlChildError("control command cannot change the authenticated X11 session")
            env.update(overrides)
        return env

    def _resolve_tool(self, proc_path: str) -> _InputImage | PinnedControlTool:
        image = _InputImage(self._tool)
        if proc_path == image.proc_path:
            return image
        for candidate in self._extra_tools.values():
            if proc_path == candidate.proc_path:
                return candidate
        raise ControlChildError("control executable is outside the pinned allowlist")

    def _execute(
        self,
        image: _InputImage | PinnedControlTool,
        args: tuple[str, ...],
        operation: str,
        timeout: float,
        *,
        cleanup: bool,
        environment_overrides: Mapping[str, str] | None = None,
    ):
        if self._child is not None:
            raise ControlChildError("previous control child is still tracked")
        lease_fd = self._lease.fd
        if lease_fd is None:
            raise ControlChildError("canonical GUI lease descriptor is unavailable")
        parent_start = process_start_ticks(os.getpid())
        if parent_start is None:
            raise ControlChildError("parent process identity is unavailable")
        image_fd = image.fd
        command = [
            sys.executable,
            "-m",
            "crowley_automation.gui.control_child",
            str(self._lease.path),
            str(lease_fd),
            str(image_fd),
            str(os.getpid()),
            str(parent_start),
            image.digest,
            image.name,
            operation,
            str(self._window_xid or 0),
            *args,
        ]
        environment = self._environment_values(environment_overrides)
        if not cleanup:
            if self._stopping():
                raise ControlChildCancelled("control cancelled by stop")
            self._guard()
            if self._stopping():
                raise ControlChildCancelled("control cancelled by stop")
            self._mark_possible_down(args)
        try:
            child = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                close_fds=True,
                pass_fds=(lease_fd, image_fd),
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ControlChildError("control child could not start") from error
        self._child = child
        deadline = self._clock() + timeout
        try:
            while child.poll() is None:
                if not cleanup and self._stopping():
                    self._cancel_and_reap()
                    self.release_only()
                    raise ControlChildCancelled("control child cancelled by stop")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._cancel_and_reap()
                    if not cleanup:
                        self.release_only()
                    raise subprocess.TimeoutExpired(command, timeout)
                self._sleep(min(0.05, remaining))
            try:
                stdout, stderr = child.communicate(timeout=0)
            except subprocess.TimeoutExpired:
                stdout, stderr = child.communicate(timeout=_REAP_SECONDS)
            return subprocess.CompletedProcess(command, int(child.returncode), stdout or "", stderr or "")
        finally:
            if self._child is child and child.poll() is not None:
                self._child = None

    def _cancel_and_reap(self) -> None:
        child = self._child
        if child is None:
            return
        reaped = False
        try:
            child.terminate()
            child.wait(timeout=_REAP_SECONDS)
            reaped = True
        except subprocess.TimeoutExpired:
            try:
                os.kill(int(child.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                self.cleanup_failed = True
                raise ControlChildError("exact control child could not be signalled") from error
            try:
                child.wait(timeout=_REAP_SECONDS)
                reaped = True
            except subprocess.TimeoutExpired as error:
                self.cleanup_failed = True
                raise ControlChildError("exact control child could not be reaped") from error
        except ProcessLookupError:
            try:
                child.wait(timeout=_REAP_SECONDS)
                reaped = True
            except subprocess.TimeoutExpired as error:
                self.cleanup_failed = True
                raise ControlChildError("exact control child could not be reaped") from error
        finally:
            if reaped:
                try:
                    child.communicate(timeout=_REAP_SECONDS)
                except (subprocess.TimeoutExpired, OSError):
                    self.cleanup_failed = True
                self._child = None

    def _mark_possible_down(self, args: tuple[str, ...]) -> None:
        for index, token in enumerate(args[:-1]):
            value = args[index + 1]
            if token == "mousedown" and value == "1":
                self._possible_down.add("button:1")
            elif token == "keydown" and value.isdecimal() and 8 <= int(value) <= 255:
                self._possible_down.add(f"key:{value}")

    def _clear_released(self, args: tuple[str, ...]) -> None:
        for index, token in enumerate(args[:-1]):
            value = args[index + 1]
            if token == "mouseup" and value == "1":
                self._possible_down.discard("button:1")
            elif token == "keyup" and value.isdecimal():
                self._possible_down.discard(f"key:{value}")


def _classify(
    name: str,
    args: tuple[str, ...],
    *,
    expected_window: int | None = None,
) -> str:
    if name == "xprop":
        if expected_window is None or expected_window <= 0:
            raise ControlChildError("WM hint update requires an exact observed window")
        xid = str(expected_window)
        if args == ("-id", xid, "-remove", "WM_NORMAL_HINTS"):
            return "wm_hint_remove"
        if args == (
            "-id",
            xid,
            "-f",
            "_MOTIF_WM_HINTS",
            "32c",
            "-set",
            "_MOTIF_WM_HINTS",
            "0x2, 0x0, 0x0, 0x0, 0x0",
        ):
            return "wm_hint_set"
        raise ControlChildError("xprop request is outside the fullscreen repair allowlist")
    if name == "wmctrl":
        if expected_window is None or expected_window <= 0:
            raise ControlChildError("window state update requires an exact observed window")
        xid = str(expected_window)
        if args == ("-i", "-c", xid):
            return "window_close"
        if args == ("-i", "-r", xid, "-b", "remove,maximized_vert,maximized_horz"):
            return "wm_maximize_remove"
        if args == ("-i", "-r", xid, "-b", "add,fullscreen"):
            return "wm_fullscreen_add"
        raise ControlChildError("wmctrl request is outside the fullscreen repair allowlist")
    if name != "xdotool":
        raise ControlChildError("control executable is outside the pinned allowlist")
    if args == ("getmouselocation", "--shell"):
        return "query"
    if (
        len(args) == 3
        and args[:2] == ("windowactivate", "--sync")
        and args[2].isdecimal()
        and int(args[2]) > 0
    ):
        return "activate"
    if args and len(args) % 2 == 0:
        releases_only = True
        for index in range(0, len(args), 2):
            phase, code = args[index : index + 2]
            if phase == "mouseup" and code == "1":
                continue
            if phase == "keyup" and code.isdecimal() and 8 <= int(code) <= 255:
                continue
            releases_only = False
            break
        if releases_only:
            return "release"
    if (
        args[:2] == ("mousedown", "1")
        and len(args) == 6
        and args[2] == "sleep"
        and args[4:] == ("mouseup", "1")
    ):
        _delay(args[3])
        return "click"
    if args and args[0] == "sleep":
        try:
            validate_pointer_motion_action(args)
        except PointerExecutionError as error:
            raise ControlChildError("pointer motion is outside the pinned gesture grammar") from error
        return "motion"
    if args and all(args[index] in {"keydown", "keyup"} for index in range(0, len(args), 2)):
        if len(args) % 2 or not args:
            raise ControlChildError("key transition grammar is malformed")
        held: set[str] = set()
        for index in range(0, len(args), 2):
            phase, code = args[index : index + 2]
            if not code.isdecimal() or not 8 <= int(code) <= 255:
                raise ControlChildError("key transition code is malformed")
            if phase == "keydown":
                if code in held:
                    raise ControlChildError("key transition presses an already held key")
                held.add(code)
            elif code not in held:
                raise ControlChildError("key transition releases a key that is not held")
            else:
                held.remove(code)
        if held:
            raise ControlChildError("key transition leaves a key held")
        return "key"
    raise ControlChildError("control command is outside the pinned allowlist")


def _delay(value: str) -> float:
    try:
        delay = float(value)
    except (ValueError, TypeError) as error:
        raise ControlChildError("control delay is malformed") from error
    if not 0 < delay <= 10:
        raise ControlChildError("control delay is outside its bound")
    return delay


def _main(argv: Sequence[str]) -> int:
    if len(argv) < 9:
        return 125
    (
        path_text,
        lease_text,
        image_text,
        parent_text,
        parent_ticks_text,
        digest,
        name,
        operation,
        window_text,
        *args,
    ) = argv
    try:
        lease_path = Path(path_text)
        lease_fd, image_fd, parent_pid, parent_ticks = map(
            int, (lease_text, image_text, parent_text, parent_ticks_text)
        )
        if any(value < 3 for value in (lease_fd, image_fd)) or parent_pid <= 0 or parent_ticks < 0:
            return 125
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return 125
        if name not in {"xdotool", "xprop", "wmctrl"}:
            return 125
        expected_window = int(window_text)
        if expected_window < 0:
            return 125
        child_operation = _classify(name, tuple(args), expected_window=expected_window or None)
        if child_operation != operation:
            return 125
        if operation == "release" and any(
            args[index] == "mousedown" or args[index] == "keydown" for index in range(0, len(args), 2)
        ):
            return 125
        if not _verify_lease(lease_path, lease_fd) or not _verify_image(image_fd, digest, name):
            return 125
        if not _set_parent_death_signal():
            return 125
        if os.getppid() != parent_pid or process_start_ticks(parent_pid) != parent_ticks:
            return 125
        os.set_inheritable(lease_fd, True)
        executable = f"/proc/self/fd/{image_fd}"
        os.set_inheritable(image_fd, True)
        os.execv(executable, [executable, *args])
    except (OSError, ValueError, ControlChildError):
        return 125
    return 125


def _verify_lease(path: Path, fd: int) -> bool:
    try:
        path_stat = path.lstat()
        parent_stat = path.parent.lstat()
        descriptor = os.fstat(fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(parent_stat.st_mode)
        and not stat.S_ISLNK(parent_stat.st_mode)
        and parent_stat.st_uid == os.getuid()
        and stat.S_IMODE(parent_stat.st_mode) == 0o700
        and stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_uid == os.getuid()
        and stat.S_IMODE(path_stat.st_mode) == 0o600
        and stat.S_ISREG(descriptor.st_mode)
        and descriptor.st_uid == os.getuid()
        and stat.S_IMODE(descriptor.st_mode) == 0o600
        and (path_stat.st_dev, path_stat.st_ino) == (descriptor.st_dev, descriptor.st_ino)
    )


def _verify_image(fd: int, digest: str, name: str) -> bool:
    import fcntl

    try:
        info = os.fstat(fd)
        link = os.readlink(f"/proc/self/fd/{fd}")
        data = os.pread(fd, _MAX_IMAGE_BYTES + 1, 0)
        seals = fcntl.fcntl(fd, _F_GET_SEALS)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_IMAGE_BYTES:
        return False
    if hashlib.sha256(data).hexdigest() != digest:
        return False
    required = _SEAL_WRITE | _SEAL_GROW | _SEAL_SHRINK | _SEAL_SEAL
    return link.startswith(f"/memfd:crowley-{name}") and seals & required == required


def _set_parent_death_signal() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        prctl = libc.prctl
    except AttributeError:
        return False
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))


__all__ = ["ControlChildCancelled", "ControlChildError", "ControlChildRunner"]
