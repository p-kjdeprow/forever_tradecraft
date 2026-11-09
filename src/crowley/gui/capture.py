"""Cross-process GNOME screenshot lock, complete-PNG wait and one-shot screen capture.

The lock serializes every ``gnome-screenshot`` writer on the host.  A screenshot
file is published non-atomically by the writer, so callers must wait until the
PNG decodes completely before reading it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from crowley.platform.paths import XdgDirs

DEFAULT_CAPTURE_TIMEOUT_SECONDS = 15.0


class CaptureError(RuntimeError):
    """The screenshot lock, writer or output failed its contract."""


def capture_lock_path() -> Path:
    return XdgDirs.from_environ(os.environ).crowley_cache / "gnome-screenshot.lock"


class IncompleteCaptureOutput(CaptureError):
    """The screenshot writer has not exposed a complete PNG yet."""


class InvalidCaptureOutput(CaptureError):
    """The screenshot writer exposed a terminally invalid PNG result."""


@contextmanager
def capture_lock(
    deadline_monotonic: float, *, check: Callable[[], None] | None = None, lock_path: Path | None = None
):
    """Hold the private cross-process GNOME screenshot serialization lock."""

    lock_path = lock_path if lock_path is not None else capture_lock_path()
    parent = lock_path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_lstat = parent.lstat()
        parent_stat = parent.stat()
    except OSError as exc:
        raise CaptureError(f"GNOME capture lock directory is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(parent_lstat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise CaptureError("GNOME capture lock directory is not a private owned directory")

    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CaptureError(f"GNOME capture lock path cannot be inspected: {exc}") from exc
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
        or stat.S_IMODE(existing.st_mode) != 0o600
    ):
        raise CaptureError("GNOME capture lock path is not a private owned regular file")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CaptureError(f"GNOME capture lock cannot be opened safely: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CaptureError("GNOME capture lock is not a private owned regular file")
        while True:
            if check is not None:
                check()
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise CaptureError("GNOME capture lock deadline expired while waiting")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise CaptureError(f"GNOME capture lock failed: {exc}") from exc
                time.sleep(min(0.01, remaining))
        try:
            if check is not None:
                check()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_complete_png(path: Path, minimum_size: tuple[int, int]) -> None:
    """Require a newly written PNG whose bytes can be fully decoded."""

    try:
        from PIL import Image, UnidentifiedImageError

        try:
            with path.open("rb") as stream:
                prefix = stream.read(8)
        except FileNotFoundError as exc:
            raise IncompleteCaptureOutput("output is not present yet") from exc
        if prefix != b"\x89PNG\r\n\x1a\n":
            if len(prefix) < 8:
                raise IncompleteCaptureOutput("PNG signature is incomplete")
            raise InvalidCaptureOutput("GNOME screenshot format is not PNG")

        with Image.open(path) as image:
            if image.format != "PNG":
                raise InvalidCaptureOutput("GNOME screenshot format is not PNG")
            if image.width < minimum_size[0] or image.height < minimum_size[1]:
                raise InvalidCaptureOutput("GNOME screenshot is smaller than the verified client")
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG":
                raise InvalidCaptureOutput("GNOME screenshot format changed")
            if image.width < minimum_size[0] or image.height < minimum_size[1]:
                raise InvalidCaptureOutput("GNOME screenshot is smaller than the verified client")
            image.load()
    except (IncompleteCaptureOutput, InvalidCaptureOutput):
        raise
    except UnidentifiedImageError as exc:
        raise IncompleteCaptureOutput(f"PNG is not decodable yet: {exc}") from exc
    except CaptureError:
        raise
    except (FileNotFoundError, OSError, SyntaxError, ValueError) as exc:
        raise IncompleteCaptureOutput(f"GNOME screenshot is not a complete PNG: {exc}") from exc


def producer_failure(process: subprocess.Popen[str], context: str) -> CaptureError:
    """Preserve a terminal producer exit and its actual stderr."""

    return_code = process.poll()
    detail = ""
    try:
        _stdout, stderr = process.communicate(timeout=0)
        if stderr:
            detail = stderr.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    suffix = f"; stderr={detail!r}" if detail else ""
    return CaptureError(f"{context}: exit={return_code}{suffix}")


def wait_for_complete_png(
    path: Path,
    minimum_size: tuple[int, int],
    deadline_monotonic: float,
    *,
    check: Callable[[], None] | None = None,
    process: subprocess.Popen[str] | None = None,
    protocol_socket: socket.socket | None = None,
) -> None:
    last_error = "output is not present yet"
    while True:
        if check is not None:
            check()
        try:
            validate_complete_png(path, minimum_size)
            if check is not None:
                check()
            return
        except InvalidCaptureOutput:
            raise
        except IncompleteCaptureOutput as exc:
            last_error = str(exc)
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                if return_code != 0:
                    raise producer_failure(
                        process, f"telemetry producer exited before PNG completion: {last_error}"
                    )
                if check is not None:
                    check()
                try:
                    validate_complete_png(path, minimum_size)
                except InvalidCaptureOutput:
                    raise
                except IncompleteCaptureOutput as exc:
                    raise producer_failure(
                        process, f"telemetry producer exited before PNG completion: {exc}"
                    ) from exc
                if check is not None:
                    check()
                return
        if protocol_socket is not None:
            readable, _writable, _exceptional = select.select([protocol_socket], [], [], 0)
            if readable:
                try:
                    if not protocol_socket.recv(1, socket.MSG_PEEK):
                        if process is not None:
                            raise producer_failure(process, "telemetry producer closed before PNG completion")
                        raise CaptureError("telemetry producer protocol closed before PNG completion")
                except BlockingIOError:
                    pass
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise CaptureError(
                f"GNOME screenshot did not become a complete PNG before deadline: {last_error}"
            )
        time.sleep(min(0.01, remaining))


def capture_screen(
    path: Path,
    minimum_size: tuple[int, int],
    *,
    deadline_monotonic: float | None = None,
    check: Callable[[], None] | None = None,
    run: Callable[[list[str], float], None] | None = None,
    lock_path: Path | None = None,
) -> None:
    """Run ``gnome-screenshot -f path`` under the capture lock and wait for a complete PNG.

    ``run`` executes the argv with a timeout in seconds; the default is a plain
    subprocess that raises ``CaptureError`` on a nonzero exit or timeout.
    """

    deadline = (
        time.monotonic() + DEFAULT_CAPTURE_TIMEOUT_SECONDS
        if deadline_monotonic is None
        else deadline_monotonic
    )
    runner = _run_plain if run is None else run
    with capture_lock(deadline, check=check, lock_path=lock_path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError("capture deadline expired before screenshot")
        runner(["gnome-screenshot", "-f", str(path)], min(DEFAULT_CAPTURE_TIMEOUT_SECONDS, remaining))
        wait_for_complete_png(path, minimum_size, deadline, check=check)


def _run_plain(argv: list[str], timeout: float) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(f"{argv[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        raise CaptureError(f"{' '.join(argv)} exited {completed.returncode}: {completed.stderr.strip()}")
