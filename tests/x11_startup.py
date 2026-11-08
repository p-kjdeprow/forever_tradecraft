"""Owned Xvfb startup synchronization for concurrent native test workers."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import os
import select
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_PRIVATE_XVFB_START_LOCK = Path(tempfile.gettempdir()) / "crowley-private-xvfb-startup.lock"


def _stderr_text(server: subprocess.Popen[Any]) -> str:
    if server.stderr is None or server.poll() is None:
        return ""
    output = server.stderr.read()
    return output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)


def _stop_failed_server(server: subprocess.Popen[Any]) -> None:
    try:
        server.terminate()
        server.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        server.kill()
        server.wait(timeout=3)


@contextmanager
def private_xvfb_startup_lock() -> Iterator[None]:
    """Serialize displayfd allocation across all native test workers."""

    with _PRIVATE_XVFB_START_LOCK.open("a+", encoding="ascii") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def start_private_xvfb(
    command_builder: Callable[[int], Sequence[str]],
    *,
    popen_kwargs: Mapping[str, Any],
) -> tuple[subprocess.Popen[Any], str, ctypes.CDLL, object]:
    """Start one exact Xvfb command and prove its owned display is accepting."""

    with private_xvfb_startup_lock():
        read_fd, write_fd = os.pipe()
        server: subprocess.Popen[Any] | None = None
        try:
            command = list(command_builder(write_fd))
            server = subprocess.Popen(
                command,
                pass_fds=(write_fd,),
                **dict(popen_kwargs),
            )
            os.close(write_fd)
            write_fd = -1
            ready, _, _ = select.select([read_fd], [], [], 5.0)
            if not ready:
                detail = _stderr_text(server)
                raise AssertionError(f"private Xvfb did not publish a display number: {detail.strip()}")
            display_number = os.read(read_fd, 32).decode("ascii").strip()
            if not display_number.isdigit():
                detail = _stderr_text(server)
                raise AssertionError(f"private Xvfb published malformed display: {display_number!r} {detail}")
            display = f":{display_number}"
            library = ctypes.util.find_library("X11")
            if library is None:
                raise AssertionError("libX11 is unavailable for private Xvfb readiness")
            x11 = ctypes.CDLL(library)
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int
            deadline = time.monotonic() + 5.0
            xdisplay = None
            while xdisplay is None and time.monotonic() < deadline:
                if server.poll() is not None:
                    detail = _stderr_text(server)
                    raise AssertionError(f"private Xvfb exited before accepting {display}: {detail.strip()}")
                xdisplay = x11.XOpenDisplay(display.encode("ascii"))
                if xdisplay is None:
                    time.sleep(0.01)
            if xdisplay is None:
                detail = _stderr_text(server)
                raise AssertionError(f"private Xvfb did not accept {display}: {detail.strip()}")
            return server, display, x11, xdisplay
        except BaseException:
            if server is not None:
                _stop_failed_server(server)
            raise
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)
