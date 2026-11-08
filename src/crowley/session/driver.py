"""Verified X11 desktop boundary for the Crowley session controller."""

from __future__ import annotations

import contextvars
import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import random
import re
import secrets
import select
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any, Protocol, cast

from crowley.core.game_target import packaged_game_target, target_scan_plan
from crowley.core.watchlist import load_catalog
from crowley.platform.paths import XdgDirs

from .executor import (
    BindingMetadata,
    CameraDragPlan,
    ClickPlan,
    DemonstrationRole,
    ExecutorError,
    KeyRole,
    MotionProfile,
    Point,
    PointerButton,
    PointerTarget,
    PointerTrajectory,
    Rect,
    plan_pointer_curve,
)
from .guardian import GuardianError, InputGuardian
from .input_broker import (
    ButtonEmissionClaim,
    InputBroker,
    InputBrokerError,
    RatePermit,
    RateReservation,
)
from .input_events import (
    EventClass,
    InputEventError,
    XI2Event,
    XI2StreamParser,
    parse_xi2_records,
    source_ids,
)
from .input_tool import InputToolError, PinnedInputTool, verify_artifact
from .pointer_execution import (
    HumanPointerPlan,
    PointerExecutionError,
    plan_human_pointer,
)
from .pointer_execution import (
    build_pointer_motion_chain as _shared_pointer_motion_chain,
)
from .process_admission import ADMISSION_WRAPPER, AdmissionGate
from .startup import (
    TAKEOVER_DRILL_HOVER_COMMAND_TIMEOUT_SECONDS,
    TAKEOVER_DRILL_HOVER_OBSERVATION_TIMEOUT_SECONDS,
    InvalidationCause,
    RuntimeInputEvidence,
    StartupEnvironment,
    is_measured_human_action,
    mint_runtime_evidence,
    resolve_forwarded_echo_sources,
)

_TARGET = packaged_game_target()
_PLAN_COUNT = len(target_scan_plan(load_catalog(), _TARGET)["itemIDs"])

_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
_DEFAULT_CAPTURE_TIMEOUT_SECONDS = 15.0
_IDENTITY_OBSERVER_REAP_GRACE_SECONDS = 0.5
_INPUT_DIAGNOSTIC_MAX_BLOCKS = 1024
_INPUT_DIAGNOSTIC_MAX_RAW_BYTES = 1024 * 1024


def _create_memfd(name: str, flags: int) -> int:
    creator = getattr(os, "memfd_create", None)
    if callable(creator):
        return creator(name, flags)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    libc.memfd_create.restype = ctypes.c_int
    fd = libc.memfd_create(name.encode("ascii"), flags)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


_IDENTITY_LATENCY_SINK: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "identity_latency_sink", default=None
)


def _set_identity_latency_sink(sink: Any) -> contextvars.Token[Any | None]:
    return _IDENTITY_LATENCY_SINK.set(sink)


def _reset_identity_latency_sink(token: contextvars.Token[Any | None]) -> None:
    _IDENTITY_LATENCY_SINK.reset(token)


def _identity_latency_span(name: str):
    sink = _IDENTITY_LATENCY_SINK.get()
    if sink is None:
        return nullcontext()
    return sink.span(name)


def _identity_latency_barrier(name: str):
    sink = _IDENTITY_LATENCY_SINK.get()
    if sink is None:
        return nullcontext()
    return sink.barrier(name)


def _identity_latency_instant(name: str) -> None:
    sink = _IDENTITY_LATENCY_SINK.get()
    if sink is not None:
        sink.instant(name)


def _identity_latency_mark(name: str) -> None:
    sink = _IDENTITY_LATENCY_SINK.get()
    if sink is not None:
        sink.mark(name)


def _identity_latency_decorator(name: str):
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with _identity_latency_span(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


class DriverError(RuntimeError):
    """A precondition, identity, lease, or postcondition failed."""


class RuntimeReadinessError(DriverError):
    """A failed runtime-arm precondition with its observed invalidation cause."""

    def __init__(self, message: str, cause: InvalidationCause):
        super().__init__(message)
        self.invalidation_cause = cause


@dataclass(frozen=True)
class TelemetryRegionReceipt:
    """Immutable parent-side proof for one typed ScreenshotArea capture."""

    nonce: str
    rectangle: tuple[int, int, int, int]
    executable_sha256: str
    owner_pid: int
    owner_process_start: str
    owner_ppid: int
    owner_pgid: int
    owner_exec_fd: int
    owner_branch: str
    request_started: float
    lock_acquired: float
    ack_sent: float
    capture_started: float
    capture_returned: float
    output_path_digest: str
    output_sha256: str
    output_bytes: int
    capture_epoch: int
    attempt_retired: bool


@_identity_latency_decorator("seal_open")
def _open_sealed_executable(path: Path, expected_sha256: str) -> tuple[int, str]:
    """Copy a verified executable into an immutable memfd before admission."""

    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise DriverError("telemetry executable path/hash is malformed")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(path, flags)
        source_stat = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_uid != os.getuid()
            or not (source_stat.st_mode & stat.S_IXUSR)
            or (source_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        ):
            raise DriverError("telemetry executable is not a private owned executable")
        data = bytearray()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
    except OSError as exc:
        raise DriverError(f"telemetry executable cannot be opened safely: {path}") from exc
    finally:
        try:
            os.close(source_fd)
        except (NameError, OSError):
            pass
    # ``seal_open`` is the complete open/read interval. Keep hashing as a real
    # nested span instead of reporting a zero-duration marker after the work.
    with _identity_latency_span("seal_hash"):
        digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise DriverError("telemetry executable hash does not match the pinned capability")
    try:
        if hasattr(os, "memfd_create"):
            image_fd = os.memfd_create("crowley-telemetry-region", os.MFD_ALLOW_SEALING)
        else:
            # This project Python omits os.memfd_create, while glibc still
            # exposes the same Linux primitive. Never fall back to a pathname.
            libc = ctypes.CDLL(None, use_errno=True)
            libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            libc.memfd_create.restype = ctypes.c_int
            image_fd = libc.memfd_create(b"crowley-telemetry-region", 0x0002)
            if image_fd < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        view = memoryview(data)
        while view:
            written = os.write(image_fd, view)
            if written <= 0:
                raise OSError("sealed executable short write")
            view = view[written:]
        os.lseek(image_fd, 0, os.SEEK_SET)
        seals = _F_SEAL_WRITE | _F_SEAL_GROW | _F_SEAL_SHRINK | _F_SEAL_SEAL
        fcntl.fcntl(image_fd, _F_ADD_SEALS, seals)
    except (AttributeError, OSError) as exc:
        try:
            os.close(image_fd)
        except (NameError, OSError):
            pass
        raise DriverError("telemetry executable could not be sealed in memory") from exc
    return image_fd, digest


class _IncompleteCaptureOutput(DriverError):
    """The screenshot writer has not exposed a complete PNG yet."""


class _InvalidCaptureOutput(DriverError):
    """The screenshot writer exposed a terminally invalid PNG result."""


DRIVER_VERSION = "native-x11-3"
GNOME_CAPTURE_LOCK_PATH: Path | None = None


def _capture_lock_path() -> Path:
    if GNOME_CAPTURE_LOCK_PATH is not None:
        return GNOME_CAPTURE_LOCK_PATH
    return XdgDirs.from_environ(os.environ).crowley_cache / "gnome-screenshot.lock"


def _capture_deadline(deadline_monotonic: float | None) -> float:
    """Return the bounded deadline shared by lock, screenshot, and validation."""

    now = time.monotonic()
    if deadline_monotonic is None:
        return now + _DEFAULT_CAPTURE_TIMEOUT_SECONDS
    if (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(float(deadline_monotonic))
    ):
        raise DriverError("capture deadline is malformed")
    deadline = float(deadline_monotonic)
    if deadline <= now:
        raise DriverError("capture deadline expired before screenshot")
    return deadline


@contextmanager
def _capture_lock(deadline_monotonic: float, *, check: Callable[[], None] | None = None):
    """Hold the private cross-process GNOME screenshot serialization lock."""

    import fcntl

    lock_path = _capture_lock_path()
    parent = lock_path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_lstat = parent.lstat()
        parent_stat = parent.stat()
    except OSError as exc:
        raise DriverError(f"GNOME capture lock directory is unavailable: {exc}") from exc
    if (
        stat.S_ISLNK(parent_lstat.st_mode)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise DriverError("GNOME capture lock directory is not a private owned directory")

    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise DriverError(f"GNOME capture lock path cannot be inspected: {exc}") from exc
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
        or stat.S_IMODE(existing.st_mode) != 0o600
    ):
        raise DriverError("GNOME capture lock path is not a private owned regular file")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise DriverError(f"GNOME capture lock cannot be opened safely: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DriverError("GNOME capture lock is not a private owned regular file")
        while True:
            if check is not None:
                check()
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise DriverError("GNOME capture lock deadline expired while waiting")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise DriverError(f"GNOME capture lock failed: {exc}") from exc
                time.sleep(min(0.01, remaining))
        try:
            if check is not None:
                check()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _validate_complete_png(path: Path, minimum_size: tuple[int, int]) -> None:
    """Require a newly written PNG whose bytes can be fully decoded."""

    try:
        from PIL import Image, UnidentifiedImageError

        try:
            with path.open("rb") as stream:
                prefix = stream.read(8)
        except FileNotFoundError as exc:
            raise _IncompleteCaptureOutput("output is not present yet") from exc
        if prefix != b"\x89PNG\r\n\x1a\n":
            if len(prefix) < 8:
                raise _IncompleteCaptureOutput("PNG signature is incomplete")
            raise _InvalidCaptureOutput("GNOME screenshot format is not PNG")

        with Image.open(path) as image:
            if image.format != "PNG":
                raise _InvalidCaptureOutput("GNOME screenshot format is not PNG")
            if image.width < minimum_size[0] or image.height < minimum_size[1]:
                raise _InvalidCaptureOutput("GNOME screenshot is smaller than the verified client")
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG":
                raise _InvalidCaptureOutput("GNOME screenshot format changed")
            if image.width < minimum_size[0] or image.height < minimum_size[1]:
                raise _InvalidCaptureOutput("GNOME screenshot is smaller than the verified client")
            image.load()
    except (_IncompleteCaptureOutput, _InvalidCaptureOutput):
        raise
    except UnidentifiedImageError as exc:
        raise _IncompleteCaptureOutput(f"PNG is not decodable yet: {exc}") from exc
    except DriverError:
        raise
    except (FileNotFoundError, OSError, SyntaxError, ValueError) as exc:
        raise _IncompleteCaptureOutput(f"GNOME screenshot is not a complete PNG: {exc}") from exc


def _validate_exact_png(path: Path, size: tuple[int, int]) -> None:
    """Require the telemetry producer's exact lossless PNG dimensions."""

    _validate_complete_png(path, size)
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            if image.format != "PNG" or image.size != size:
                raise _InvalidCaptureOutput("telemetry screenshot is not exactly 256x256 PNG")
    except FileNotFoundError as exc:
        raise _IncompleteCaptureOutput("telemetry output is not present") from exc
    except OSError as exc:
        raise _IncompleteCaptureOutput("telemetry output cannot be read completely") from exc


def _proc_identity(pid: int) -> tuple[str, int, int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = text.rsplit(")", 1)[1].split()
        return tail[19], int(tail[1]), int(tail[2])
    except (OSError, IndexError, ValueError) as exc:
        raise DriverError(f"cannot inspect admitted telemetry process {pid}") from exc


def _proc_start(pid: int) -> str:
    return _proc_identity(pid)[0]


def _protocol_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _protocol_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _observer_protocol_uint(value: Any) -> bool:
    """Validate one bounded observer timing/version integer."""

    return _protocol_int(value) and value >= 0


def _protocol_float(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _sha256_proc_object(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DriverError(f"cannot hash admitted telemetry object {path}") from exc
    return digest.hexdigest()


@_identity_latency_decorator("process_verify")
def _verify_admitted_process(
    process: subprocess.Popen[str], image_fd: int, expected_digest: str, expected_ppid: int
) -> tuple[str, int, int]:
    """Verify live kernel identity and the exact sealed image before ACK."""

    if process.poll() is not None:
        raise DriverError("admitted telemetry process exited before ACK")
    try:
        os.kill(process.pid, 0)
        parent_image = os.fstat(image_fd)
        child_fd_path = Path(f"/proc/{process.pid}/fd/{image_fd}")
        child_exe_path = Path(f"/proc/{process.pid}/exe")
        child_fd_stat = child_fd_path.stat()
        child_exe_stat = child_exe_path.stat()
    except OSError as exc:
        raise DriverError("admitted telemetry process is not alive for kernel proof") from exc
    identity = _proc_identity(process.pid)
    start, ppid, pgid = identity
    if ppid != expected_ppid or pgid != process.pid:
        raise DriverError("admitted telemetry process parent or group changed")
    try:
        if os.getpgid(process.pid) != process.pid:
            raise DriverError("admitted telemetry process is outside its private process group")
        if not (Path(f"/proc/{pgid}/task") / str(process.pid)).is_dir():
            raise DriverError("admitted telemetry process is not a member of its process group")
    except OSError as exc:
        raise DriverError("cannot inspect admitted telemetry process group") from exc
    object_identity = (parent_image.st_dev, parent_image.st_ino)
    for observed in (child_fd_stat, child_exe_stat):
        if not stat.S_ISREG(observed.st_mode) or (observed.st_dev, observed.st_ino) != object_identity:
            raise DriverError("admitted telemetry executable is not the retained sealed image")
    if (
        _sha256_proc_object(child_fd_path) != expected_digest
        or _sha256_proc_object(child_exe_path) != expected_digest
    ):
        raise DriverError("admitted telemetry executable object hash does not match the sealed image")
    return identity


def _json_unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate telemetry protocol key")
        result[key] = value
    return result


def _protocol_rectangle(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    return tuple(value)  # type: ignore[return-value]


@_identity_latency_decorator("protocol_record")
def _json_record(
    sock: socket.socket,
    deadline: float,
    *,
    check: Callable[[], None] | None = None,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    """Read one strict newline-delimited child protocol record."""

    chunks = bytearray()
    while b"\n" not in chunks:
        if check is not None:
            check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("telemetry owner protocol deadline expired")
        with _identity_latency_span("protocol_wait"):
            readable, _writable, _exceptional = select.select([sock], [], [], min(0.1, remaining))
        if not readable:
            if process is not None and process.poll() is not None:
                raise _producer_failure(process, "telemetry producer exited before protocol completion")
            continue
        # Keep record boundaries lossless when a fast producer writes the next
        # record before the parent has parsed this one.
        if check is not None:
            check()
        piece = sock.recv(1)
        if not piece:
            if process is not None:
                raise _producer_failure(process, "telemetry producer protocol closed")
            raise DriverError("telemetry owner protocol closed before completion")
        chunks.extend(piece)
        if len(chunks) > 16384:
            raise DriverError("telemetry owner protocol record is too large")
    if check is not None:
        check()
    line, _separator, _rest = bytes(chunks).partition(b"\n")
    try:
        record = json.loads(line.decode("utf-8"), object_pairs_hook=_json_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DriverError("telemetry owner protocol record is malformed") from exc
    if not isinstance(record, dict):
        raise DriverError("telemetry owner protocol record is not an object")
    return record


def _producer_failure(process: subprocess.Popen[str], context: str) -> DriverError:
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
    return DriverError(f"{context}: exit={return_code}{suffix}")


@_identity_latency_decorator("png_publication")
def _wait_for_complete_png(
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
            _validate_complete_png(path, minimum_size)
            if check is not None:
                check()
            return
        except _InvalidCaptureOutput:
            raise
        except _IncompleteCaptureOutput as exc:
            last_error = str(exc)
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                if return_code != 0:
                    raise _producer_failure(
                        process, f"telemetry producer exited before PNG completion: {last_error}"
                    )
                if check is not None:
                    check()
                try:
                    _validate_complete_png(path, minimum_size)
                except _InvalidCaptureOutput:
                    raise
                except _IncompleteCaptureOutput as exc:
                    raise _producer_failure(
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
                            raise _producer_failure(
                                process, "telemetry producer closed before PNG completion"
                            )
                        raise DriverError("telemetry producer protocol closed before PNG completion")
                except BlockingIOError:
                    pass
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise DriverError(f"GNOME screenshot did not become a complete PNG before deadline: {last_error}")
        time.sleep(min(0.01, remaining))


def _validate_x11_display_screen(display: str) -> None:
    """Require the pinned XTEST emitter's supported screen-zero contract."""

    if not isinstance(display, str) or not display:
        raise DriverError("X11 display is malformed")
    match = re.search(r"\.(\d+)$", display)
    if match is not None and int(match.group(1)) != 0:
        raise DriverError("X11 display screen must be 0 for the pinned XTEST emitter")


# Compatibility name retained for tests that inject a deliberately stalled or
# wrong wrapper. Production always points this name at the positive gate.
_EXEC_ADMISSION_WRAPPER = ADMISSION_WRAPPER
_WRAPPED_COMMANDS = frozenset(
    {
        "gnome-screenshot",
        "sleep",
        "true",
        "xinput",
        "xmodmap",
        "xprop",
        "xwininfo",
        "xdotool",
    }
)


def _resolve_wrapped_executable(command: str) -> str:
    if os.path.basename(command) not in _WRAPPED_COMMANDS:
        raise DriverError(f"desktop command is not allowlisted: {command}")
    executable = shutil.which(command) if not os.path.isabs(command) else command
    if not executable or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        raise DriverError(f"desktop command executable is unavailable: {command}")
    canonical = os.path.realpath(executable)
    known = {os.path.realpath(path) for name in _WRAPPED_COMMANDS if (path := shutil.which(name)) is not None}
    if canonical not in known:
        raise DriverError(f"desktop command executable is not an installed allowlisted binary: {command}")
    return canonical


def build_pointer_motion_chain(
    trajectory: PointerTrajectory,
    client_geometry: tuple[int, int, int, int],
) -> list[str]:
    """Build one closed xdotool motion chain from a validated trajectory.

    Every sample interval remains in the argv, including intervals whose
    quantized point repeats.  This preserves the planned duration and makes a
    trailing repeated endpoint an explicit sleep instead of silently shortening
    the gesture.
    """

    try:
        chain = _shared_pointer_motion_chain(trajectory, client_geometry)
    except PointerExecutionError as exc:
        raise DriverError(str(exc)) from exc
    if chain.count("mousemove") == 0:
        raise DriverError("pointer trajectory does not contain a complete emitted path")
    return chain


def build_camera_drag_chain(
    plan: CameraDragPlan,
    client_geometry: tuple[int, int, int, int],
    positioning: PointerTrajectory | None = None,
) -> list[str]:
    """Build one positioning-plus-right-button finite drag chain."""

    if not isinstance(plan, CameraDragPlan):
        raise DriverError("camera drag plan is malformed")
    if plan.button is not PointerButton.RIGHT:
        raise DriverError("camera drag must use the sealed right-button role")
    if len(plan.samples) < 2:
        raise DriverError("camera drag requires a press and held motion endpoint")
    motion = PointerTrajectory(plan.samples, plan.duration, plan.samples[-1].point, plan.binding, "camera")
    if positioning is not None:
        if not isinstance(positioning, PointerTrajectory):
            raise DriverError("camera positioning trajectory is malformed")
        if positioning.binding != plan.binding:
            raise DriverError("camera positioning binding does not match drag binding")
        if positioning.endpoint != plan.samples[0].point:
            raise DriverError("camera positioning endpoint does not match drag start")
        positioning_chain = build_pointer_motion_chain(positioning, client_geometry)
    else:
        positioning_chain = ["xdotool"]
    chain = build_pointer_motion_chain(motion, client_geometry)
    if chain.count("mousemove") == 0:
        raise DriverError("camera drag has no emitted motion")
    # The pointer builder emits the first xdotool token and all timed motion.
    # Insert the held-button phase before the first interval and close it after
    # the final interval, preserving every planned sleep.
    return [
        "xdotool",
        *positioning_chain[1:],
        "mousedown",
        "3",
        *chain[1:],
        "mouseup",
        "3",
    ]


@dataclass(frozen=True)
class CalibrationTicket:
    """One-use admission proof for the harmless owned GTK calibration target."""

    epoch: int
    target_pid: int
    target_xid: int
    target_xids: frozenset[int]
    title: str
    geometry: tuple[int, int, int, int]
    managed_frame_xid: int = 0
    managed_frame_geometry: tuple[int, int, int, int] = ()
    target_executable: str = ""
    target_process_start: str = ""
    target_layout_kind: str = "mutter-managed-frame-v1"
    target_root_xid: int = 0
    target_parent_xid: int = 0
    target_descendant_xids: frozenset[int] = frozenset()
    target_excluded_input_only_xids: frozenset[int] = frozenset()

    def contains(self, xid: int) -> bool:
        return xid in self.target_xids


@dataclass(frozen=True, slots=True)
class SyntheticPointerProbe:
    """Typed result of one owned calibration pointer gesture."""

    origin: tuple[int, int, int]
    requested_endpoint: tuple[int, int]
    final_observed: tuple[int, int, int]
    emitted_transitions: tuple[tuple[int, int], ...]
    seed: int
    plan: tuple[tuple[float, int, int], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.origin, tuple)
            or len(self.origin) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.origin)
            or self.origin[2] <= 0
        ):
            raise DriverError("synthetic pointer probe origin is malformed")
        if (
            not isinstance(self.requested_endpoint, tuple)
            or len(self.requested_endpoint) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.requested_endpoint)
        ):
            raise DriverError("synthetic pointer probe requested endpoint is malformed")
        if (
            not isinstance(self.final_observed, tuple)
            or len(self.final_observed) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.final_observed)
            or self.final_observed[2] <= 0
        ):
            raise DriverError("synthetic pointer probe final observation is malformed")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**64
            or not self.emitted_transitions
            or any(
                not isinstance(point, tuple)
                or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in point)
                for point in self.emitted_transitions
            )
            or not self.plan
            or any(
                not isinstance(sample, tuple)
                or len(sample) != 3
                or isinstance(sample[0], bool)
                or not isinstance(sample[0], float)
                or not math.isfinite(sample[0])
                or sample[0] < 0
                or any(isinstance(value, bool) or not isinstance(value, int) for value in sample[1:])
                for sample in self.plan
            )
        ):
            raise DriverError("synthetic pointer probe motion plan is malformed")
        previous_time = -1.0
        for sample in self.plan:
            if sample[0] <= previous_time:
                raise DriverError("synthetic pointer probe motion plan is not strictly timed")
            previous_time = sample[0]
        if self.plan[0][1:] != self.origin[:2] or self.plan[-1][1:] != self.requested_endpoint:
            raise DriverError("synthetic pointer probe motion plan is not sealed to its endpoints")
        planned_transitions = tuple(
            sample[1:]
            for previous, sample in zip(self.plan, self.plan[1:], strict=False)
            if previous[1:] != sample[1:]
        )
        if planned_transitions != self.emitted_transitions:
            raise DriverError("synthetic pointer probe transitions do not match its plan")


@dataclass(frozen=True)
class WindowIdentity:
    xid: int
    pid: int
    executable: str
    title: str
    wm_class: str
    child_xid: int
    outer_geometry: tuple[int, int, int, int]
    child_geometry: tuple[int, int, int, int]
    foreground_xid: int
    process_start: str
    display: str
    fingerprint: str
    root_xid: int = 0
    managed_frame_xid: int = 0
    target_event_xids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Anchor:
    x: int
    y: int
    identity_fingerprint: str


@dataclass(frozen=True)
class NamedStateObservation:
    """One bounded visual/state observation tied to a fresh client capture.

    Login and closeout actions are modal operations.  A window identity only
    proves which process owns the pixels; it cannot prove that a disconnect
    dialog, character row, or world scene is currently visible.  The model or
    native observer therefore supplies this explicit observation, including a
    capture token/sequence and named facts.  :meth:`DesktopDriver
    .verify_named_action` validates the identity, target ancestry, freshness,
    and action-specific facts before an input action is allowed.
    """

    state: str
    identity_fingerprint: str
    target_xid: int
    capture_id: str
    capture_sequence: int
    facts: tuple[tuple[str, str], ...]
    source: str
    observed_monotonic: float
    boot: str = ""

    @classmethod
    def from_facts(
        cls,
        *,
        state: str,
        identity_fingerprint: str,
        target_xid: int,
        capture_id: str,
        capture_sequence: int,
        facts: dict[str, str],
        source: str,
        observed_monotonic: float,
        boot: str = "",
    ) -> NamedStateObservation:
        """Build a canonical observation without accepting unnamed booleans."""

        return cls(
            state=state,
            identity_fingerprint=identity_fingerprint,
            target_xid=target_xid,
            capture_id=capture_id,
            capture_sequence=capture_sequence,
            facts=tuple(sorted((str(key), str(value)) for key, value in facts.items())),
            source=source,
            observed_monotonic=observed_monotonic,
            boot=boot,
        )


@dataclass(frozen=True)
class MeasuredAnchor:
    """One visually measured control anchor tied to one client capture."""

    action: str
    x: int
    y: int
    identity_fingerprint: str
    target_xid: int
    capture_id: str
    capture_sequence: int
    source: str
    observed_monotonic: float
    capture_monotonic: float = -1.0
    facts: tuple[tuple[str, str], ...] = ()
    generation: int | None = None
    capture_path: str = ""
    capture_sha256: str = ""
    scene: str = ""
    bounds: tuple[int, int, int, int] = ()
    target_ancestry: tuple[int, ...] = ()
    evidence_hash: str = ""

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "x": self.x,
            "y": self.y,
            "identity": self.identity_fingerprint,
            "target": self.target_xid,
            "capture": self.capture_id,
            "sequence": self.capture_sequence,
            "source": self.source,
            "observed": self.observed_monotonic,
            "captureMonotonic": self.capture_monotonic,
            "facts": self.facts,
            "generation": self.generation,
            "capturePath": self.capture_path,
            "captureSha256": self.capture_sha256,
            "scene": self.scene,
            "bounds": self.bounds,
            "targetAncestry": self.target_ancestry,
        }

    def with_hash(self) -> MeasuredAnchor:
        digest = hashlib.sha256(
            json.dumps(self._canonical_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return replace(self, evidence_hash=digest)

    def verify_seal(self) -> None:
        if not self.evidence_hash:
            raise DriverError("measured anchor has no canonical evidence seal")
        if not self.capture_path or not Path(self.capture_path).is_absolute():
            raise DriverError("measured anchor capture path is not an absolute retained file")
        if (
            not self.capture_id
            or self.capture_sequence <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", self.capture_sha256)
            or isinstance(self.capture_monotonic, bool)
            or not isinstance(self.capture_monotonic, (int, float))
            or not math.isfinite(float(self.capture_monotonic))
            or self.capture_monotonic < 0
        ):
            raise DriverError("measured anchor capture binding is incomplete")
        try:
            digest = hashlib.sha256(Path(self.capture_path).read_bytes()).hexdigest()
            if digest != self.capture_sha256 or self.capture_id != digest:
                raise DriverError("measured anchor retained capture changed")
        except OSError as exc:
            raise DriverError("measured anchor retained capture is unavailable") from exc
        if self.evidence_hash != self.with_hash().evidence_hash:
            raise DriverError("measured anchor evidence seal does not match its facts")

    @classmethod
    def from_capture(
        cls,
        *,
        action: str,
        x: int,
        y: int,
        identity_fingerprint: str,
        target_xid: int,
        capture_id: str,
        capture_sequence: int,
        source: str,
        observed_monotonic: float,
        capture_monotonic: float,
        scene: str,
        bounds: tuple[int, int, int, int],
        target_ancestry: tuple[int, ...],
        capture_sha256: str,
        generation: int,
        capture_path: str = "",
        facts: tuple[tuple[str, str], ...] = (),
    ) -> MeasuredAnchor:
        """Build the sealed record returned by the native visual producer."""

        return cls(
            action=action,
            x=x,
            y=y,
            identity_fingerprint=identity_fingerprint,
            target_xid=target_xid,
            capture_id=capture_id,
            capture_sequence=capture_sequence,
            source=source,
            observed_monotonic=observed_monotonic,
            capture_monotonic=capture_monotonic,
            facts=tuple(facts),
            generation=generation,
            capture_path=capture_path,
            capture_sha256=capture_sha256,
            scene=scene,
            bounds=bounds,
            target_ancestry=tuple(target_ancestry),
        ).with_hash()


@dataclass(frozen=True)
class DriverProfile:
    display: str = ":0"
    xauthority: str = ""
    wine_prefix: str = ""
    client_x: int = 0
    client_y: int = 0
    client_width: int = 1920
    client_height: int = 1080
    outer_width: int = 1928
    outer_height: int = 1092
    window_client_offset: tuple[int, int] = (0, 0)
    canvas_origin: tuple[int, int] = (80, 48)
    canvas_crop: tuple[int, int, int, int] = (80, 48, 256, 256)
    model_crop: tuple[int, int, int, int] = (80, 48, 1760, 984)
    window_class: str = _TARGET.window_class
    executable: str = _TARGET.executable
    action_anchors: dict[str, tuple[int, int]] = field(default_factory=dict)
    anchor_fingerprints: dict[str, str] = field(default_factory=dict)
    window_title: str = "World of Warcraft"
    character: str = "Arthur"
    realm: str = _TARGET.realm
    house: str = _TARGET.house
    plan_count: int = _PLAN_COUNT
    telemetry_region_executable: Path | None = None
    telemetry_region_executable_sha256: str | None = None
    identity_observer_executable: Path | None = None
    identity_observer_executable_sha256: str | None = None

    def validate(self) -> None:
        geometry_values = (
            self.client_x,
            self.client_y,
            self.client_width,
            self.client_height,
            self.outer_width,
            self.outer_height,
            *self.window_client_offset,
            *self.canvas_origin,
            *self.canvas_crop,
            *self.model_crop,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in geometry_values):
            raise DriverError("profile geometry contains a non-integer coordinate")
        if self.client_width <= 0 or self.client_height <= 0:
            raise DriverError("client geometry must be positive")
        left, top, width, height = self.canvas_crop
        if left < 0 or top < 0 or width <= 0 or height <= 0:
            raise DriverError("status crop is malformed")
        if left + width > self.client_width or top + height > self.client_height:
            raise DriverError("status crop exceeds client geometry")
        for action, point in self.action_anchors.items():
            if action not in DesktopDriver.ACTIONS:
                raise DriverError(f"unknown action anchor {action}")
            if len(point) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) for v in point):
                raise DriverError(f"anchor {action} is not an integer pair")
            if action not in self.anchor_fingerprints:
                raise DriverError(f"anchor {action} has no measured identity evidence")
        if (self.telemetry_region_executable is None) != (self.telemetry_region_executable_sha256 is None):
            raise DriverError("telemetry region executable and SHA-256 must be configured together")
        if self.telemetry_region_executable is not None:
            if not self.telemetry_region_executable.is_absolute():
                raise DriverError("telemetry region executable must be absolute")
            if not re.fullmatch(r"[0-9a-f]{64}", self.telemetry_region_executable_sha256 or ""):
                raise DriverError("telemetry region executable SHA-256 is malformed")
            try:
                metadata = self.telemetry_region_executable.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or not (metadata.st_mode & stat.S_IXUSR)
                    or (metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
                ):
                    raise DriverError("telemetry region executable is not private and executable")
                digest = hashlib.sha256(self.telemetry_region_executable.read_bytes()).hexdigest()
            except OSError as exc:
                raise DriverError("telemetry region executable is unavailable") from exc
            if digest != self.telemetry_region_executable_sha256:
                raise DriverError("telemetry region executable hash does not match the profile")
        if (self.identity_observer_executable is None) != (self.identity_observer_executable_sha256 is None):
            raise DriverError("identity observer executable and SHA-256 must be configured together")
        if self.identity_observer_executable is not None:
            if not self.identity_observer_executable.is_absolute():
                raise DriverError("identity observer executable must be absolute")
            if not re.fullmatch(r"[0-9a-f]{64}", self.identity_observer_executable_sha256 or ""):
                raise DriverError("identity observer executable SHA-256 is malformed")
            try:
                metadata = self.identity_observer_executable.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or not (metadata.st_mode & stat.S_IXUSR)
                    or (metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
                ):
                    raise DriverError("identity observer executable is not private and executable")
                digest = hashlib.sha256(self.identity_observer_executable.read_bytes()).hexdigest()
            except OSError as exc:
                raise DriverError("identity observer executable is unavailable") from exc
            if digest != self.identity_observer_executable_sha256:
                raise DriverError("identity observer executable hash does not match the profile")


class Backend(Protocol):
    def action(self, name: str, **kwargs: Any) -> dict[str, Any] | None: ...

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity | None: ...


@dataclass
class RecordingBackend:
    """Deterministic fake backend; it does not claim native identity."""

    events: list[dict[str, Any]] = field(default_factory=list)
    captures: list[Path] = field(default_factory=list)
    command_buffer: str = ""
    identity: WindowIdentity | None = None
    action_preconditions: dict[str, bool] = field(default_factory=dict)
    action_postconditions: dict[str, bool] = field(default_factory=dict)

    def action(self, name: str, **kwargs: Any) -> dict[str, Any] | None:
        self.events.append({"name": name, **kwargs})
        if name == "type_allowlisted_command":
            self.command_buffer = str(kwargs.get("command", ""))
        elif name in {"submit_command", "escape_command_buffer"}:
            self.command_buffer = ""
        return None

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity | None:
        del deadline_monotonic
        self.captures.append(path)
        return identity or self.identity

    def calibration_focus(self, xid: int) -> None:
        self.events.append({"name": "calibration_focus", "xid": xid})

    def verify_action_precondition(self, name: str, identity: WindowIdentity) -> bool:
        return self.action_preconditions.get(name, True)

    def verify_action_postcondition(self, name: str, identity: WindowIdentity) -> bool:
        return self.action_postconditions.get(name, True)


class PinnedX11IdentityObserver:
    """Execute one sealed, pinned observer invocation per identity boundary."""

    _MAX_OUTPUT = 64 * 1024
    _MAX_BINARY = 32 * 1024 * 1024

    def __init__(self, path: Path, digest: str):
        self.path = path
        self.digest = digest
        self._lifecycle_lock = threading.RLock()
        self._close_event = threading.Event()
        self._closed = False
        self._active_processes: set[subprocess.Popen[bytes]] = set()
        self._fd = self._seal(path, digest)

    @staticmethod
    def _seal(path: Path, digest: str) -> int:
        source_fd: int | None = None
        memfd = -1
        try:
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(path, flags)
            info = os.fstat(source_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or not info.st_mode & 0o111
            ):
                raise DriverError("identity observer is not a private executable regular file")
            if info.st_size <= 0 or info.st_size > PinnedX11IdentityObserver._MAX_BINARY:
                raise DriverError("identity observer binary is outside the bounded size")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(source_fd, 1024 * 1024):
                total += len(chunk)
                if total > PinnedX11IdentityObserver._MAX_BINARY:
                    raise DriverError("identity observer binary is oversized")
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(source_fd)
            if len(data) != info.st_size or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise DriverError("identity observer changed while being read")
            if hashlib.sha256(data).hexdigest() != digest:
                raise DriverError("identity observer digest does not match profile")
            memfd_flags = getattr(os, "MFD_CLOEXEC", 0x0001) | getattr(os, "MFD_ALLOW_SEALING", 0x0002)
            memfd = _create_memfd("crowley-x11-identity-observer", memfd_flags)
            written = 0
            while written < len(data):
                written += os.write(memfd, data[written:])
            os.fchmod(memfd, 0o700)
            seals = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
            try:
                fcntl.fcntl(memfd, _F_ADD_SEALS, seals)
            except OSError as exc:
                os.close(memfd)
                memfd = -1
                raise DriverError("identity observer memfd could not be sealed") from exc
            return memfd
        except OSError as exc:
            if memfd >= 0:
                os.close(memfd)
            raise DriverError("identity observer could not be opened") from exc
        except BaseException:
            if memfd >= 0:
                os.close(memfd)
            raise
        finally:
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except OSError:
                    pass

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._close_event.set()
            active = tuple(self._active_processes)
            fd = self._fd
            self._fd = -1
        cleanup_error: DriverError | None = None
        for process in active:
            try:
                self._kill_and_reap(process)
            except DriverError as exc:
                cleanup_error = cleanup_error or exc
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                cleanup_error = cleanup_error or DriverError("identity observer FD could not be closed")
                cleanup_error.__cause__ = exc
        if cleanup_error is not None:
            raise cleanup_error

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    @classmethod
    def _kill_and_reap(cls, process: subprocess.Popen[bytes]) -> None:
        """Synchronously reap an owned observer after a bounded group kill."""

        if process.poll() is None:
            cls._kill_group(process)
        try:
            process.wait(timeout=_IDENTITY_OBSERVER_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            cls._kill_group(process)
            try:
                process.wait(timeout=_IDENTITY_OBSERVER_REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired as reap_exc:
                raise DriverError("identity observer could not be reaped") from reap_exc
            if process.poll() is None:
                raise DriverError("identity observer could not be reaped") from exc

    @classmethod
    def _drain_process(
        cls,
        process: subprocess.Popen[bytes],
        *,
        deadline: float,
        cancel_event: threading.Event | None,
        close_event: threading.Event | None = None,
    ) -> tuple[bytes, bytes]:
        """Drain both pipes without allowing a child-held pipe to escape the deadline."""

        if process.stdout is None or process.stderr is None:
            raise DriverError("identity observer pipes are unavailable")
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        streams = {"stdout": process.stdout, "stderr": process.stderr}
        killed = False
        try:
            for name, stream in streams.items():
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while True:
                if (cancel_event is not None and cancel_event.is_set()) or (
                    close_event is not None and close_event.is_set()
                ):
                    cls._kill_and_reap(process)
                    killed = True
                    raise DriverError("identity observer cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cls._kill_and_reap(process)
                    killed = True
                    raise DriverError("identity observer timed out")
                return_code = process.poll()
                if return_code is not None and not killed:
                    # The executable may have descendants holding the pipes.
                    # Close the private process group before waiting for EOF.
                    cls._kill_group(process)
                    killed = True
                if not selector.get_map():
                    if return_code is not None:
                        break
                    # Both pipes can close while a descendant-held process
                    # group remains alive. Wait in bounded increments instead
                    # of spinning until the shared deadline.
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                    continue
                events = selector.select(min(0.05, remaining))
                for key, _ in events:
                    name = key.data
                    try:
                        piece = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not piece:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    buffer = buffers[name]
                    if len(buffer) + len(piece) > cls._MAX_OUTPUT:
                        cls._kill_and_reap(process)
                        killed = True
                        raise DriverError(f"identity observer {name} is oversized")
                    buffer.extend(piece)
            if process.poll() is None:
                cls._kill_and_reap(process)
                killed = True
            return bytes(buffers["stdout"]), bytes(buffers["stderr"])
        finally:
            selector.close()
            for stream in streams.values():
                try:
                    stream.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    cls._kill_and_reap(process)
                except DriverError:
                    # Preserve the original timeout/cancellation/protocol
                    # error while still making a best effort to reap here.
                    pass

    @classmethod
    def _write_request(
        cls,
        process: subprocess.Popen[bytes],
        request: bytes,
        *,
        deadline: float,
        cancel_event: threading.Event | None,
        close_event: threading.Event | None = None,
    ) -> None:
        if process.stdin is None:
            raise DriverError("identity observer stdin is unavailable")
        stream = process.stdin
        fd = stream.fileno()
        os.set_blocking(fd, False)
        pending = memoryview(request + b"\n")
        while pending:
            if (cancel_event is not None and cancel_event.is_set()) or (
                close_event is not None and close_event.is_set()
            ):
                raise DriverError("identity observer cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverError("identity observer timed out")
            _, writable, _ = select.select([], [fd], [], min(0.05, remaining))
            if not writable:
                continue
            try:
                written = os.write(fd, pending)
            except BlockingIOError:
                continue
            if written <= 0:
                raise DriverError("identity observer stdin closed")
            pending = pending[written:]

    def _decode_response(
        self,
        stdout: bytes,
        stderr: bytes,
        *,
        process: subprocess.Popen[bytes],
        nonce: str,
    ) -> dict[str, Any]:
        if process is None or process.returncode != 0:
            detail = stderr[:256].decode("utf-8", "replace").strip()
            if not detail and len(stdout) <= self._MAX_OUTPUT:
                try:
                    failure = json.loads(stdout.decode("utf-8"), object_pairs_hook=_json_unique_pairs)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    failure = None
                if isinstance(failure, dict) and set(failure) == {"protocolVersion", "error"}:
                    if failure.get("protocolVersion") != 1 or not isinstance(failure.get("error"), str):
                        raise DriverError("identity observer error record is malformed")
                    detail = failure["error"][:256]
            raise DriverError(f"identity observer failed: {detail or 'nonzero exit'}")
        if len(stdout) > self._MAX_OUTPUT:
            raise DriverError("identity observer output is oversized")
        try:
            result = json.loads(stdout.decode("utf-8"), object_pairs_hook=_json_unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise DriverError("identity observer output is malformed") from exc
        required = {
            "protocolVersion",
            "matchingProcessCount",
            "candidateWindowCount",
            "pid",
            "start",
            "exe",
            "statusName",
            "loader",
            "winePrefix",
            "xid",
            "title",
            "class",
            "outer",
            "child",
            "outerRoot",
            "outerParent",
            "outerGeometry",
            "childGeometry",
            "outerChildren",
            "focus",
            "active",
            "display",
            "nonce",
            "binaryDigest",
            "timingsNs",
        }
        if not isinstance(result, dict) or set(result) != required:
            raise DriverError("identity observer output keys are malformed")
        if result.get("protocolVersion") != 1:
            raise DriverError("identity observer protocol version is unsupported")
        timings = result.get("timingsNs")
        if (
            not isinstance(timings, dict)
            or set(timings) != {"proc", "x11", "total"}
            or any(not _observer_protocol_uint(timings.get(key)) for key in ("proc", "x11", "total"))
            or timings["total"] < timings["proc"]
            or timings["total"] < timings["x11"]
        ):
            raise DriverError("identity observer timings are malformed")
        string_keys = {
            "start",
            "exe",
            "statusName",
            "loader",
            "winePrefix",
            "title",
            "class",
            "display",
            "nonce",
            "binaryDigest",
        }
        integer_keys = {
            "matchingProcessCount",
            "candidateWindowCount",
            "pid",
            "xid",
            "child",
            "outerRoot",
            "outerParent",
            "focus",
            "active",
            "outer",
        }
        if any(not isinstance(result[key], str) for key in string_keys):
            raise DriverError("identity observer string facts are malformed")
        if any(isinstance(result[key], bool) or not isinstance(result[key], int) for key in integer_keys):
            raise DriverError("identity observer integer facts are malformed")
        for key in ("outerGeometry", "childGeometry"):
            value = result[key]
            if (
                not isinstance(value, list)
                or len(value) != 4
                or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
            ):
                raise DriverError("identity observer geometry facts are malformed")
        children = result["outerChildren"]
        if not isinstance(children, list) or any(
            isinstance(item, bool) or not isinstance(item, int) for item in children
        ):
            raise DriverError("identity observer child facts are malformed")
        if result["nonce"] != nonce or result["binaryDigest"] != self.digest:
            raise DriverError("identity observer response binding changed")
        return result

    def invoke(
        self,
        profile: DriverProfile,
        *,
        xauthority: str,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            if self._closed or self._fd < 0:
                raise DriverError("identity observer is closed")
            observer_fd = self._fd
        deadline = (
            deadline_monotonic
            if deadline_monotonic is not None
            else time.monotonic() + _DEFAULT_CAPTURE_TIMEOUT_SECONDS
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("identity observer deadline expired")
        nonce = uuid.uuid4().hex
        request = json.dumps(
            {
                "protocolVersion": 1,
                "display": profile.display,
                "xauthority": xauthority,
                "title": profile.window_title,
                "class": profile.window_class,
                "executable": profile.executable,
                "winePrefix": os.path.realpath(os.path.expanduser(profile.wine_prefix)),
                "loader": "wine-preloader",
                "outerWidth": profile.outer_width,
                "outerHeight": profile.outer_height,
                "clientWidth": profile.client_width,
                "clientHeight": profile.client_height,
                "nonce": nonce,
                "binaryDigest": self.digest,
            },
            separators=(",", ":"),
        ).encode()
        fd_path = f"/proc/self/fd/{observer_fd}"
        process: subprocess.Popen[bytes] | None = None
        invocation_started_ns = time.monotonic_ns()
        spawn_finished_ns: int | None = None
        write_started_ns: int | None = None
        write_finished_ns: int | None = None
        drain_started_ns: int | None = None
        drain_finished_ns: int | None = None
        decode_started_ns: int | None = None
        decode_finished_ns: int | None = None
        pending_error: BaseException | None = None
        try:
            if (cancel_event is not None and cancel_event.is_set()) or self._close_event.is_set():
                raise DriverError("identity observer cancelled")
            with self._lifecycle_lock:
                if self._closed or self._fd != observer_fd:
                    raise DriverError("identity observer is closed")
                process = subprocess.Popen(
                    [fd_path],
                    executable=fd_path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd="/",
                    env={"DISPLAY": profile.display, "XAUTHORITY": xauthority, "LC_ALL": "C"},
                    pass_fds=(observer_fd,),
                    start_new_session=True,
                )
                self._active_processes.add(process)
                spawn_finished_ns = time.monotonic_ns()
            write_started_ns = time.monotonic_ns()
            self._write_request(
                process,
                request,
                deadline=deadline,
                cancel_event=cancel_event,
                close_event=self._close_event,
            )
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError as exc:
                    raise DriverError("identity observer stdin could not be closed") from exc
                finally:
                    process.stdin = None
            write_finished_ns = time.monotonic_ns()
            drain_started_ns = time.monotonic_ns()
            stdout, stderr = self._drain_process(
                process,
                deadline=deadline,
                cancel_event=cancel_event,
                close_event=self._close_event,
            )
            drain_finished_ns = time.monotonic_ns()
            decode_started_ns = time.monotonic_ns()
            result = self._decode_response(stdout, stderr, process=process, nonce=nonce)
            decode_finished_ns = time.monotonic_ns()
            sink = _IDENTITY_LATENCY_SINK.get()
            observer_call = getattr(sink, "observer_call", None)
            if callable(observer_call):
                total_finished_ns = time.monotonic_ns()
                phase_bounds = {
                    "spawn": (invocation_started_ns, spawn_finished_ns),
                    "write": (write_started_ns, write_finished_ns),
                    "drain": (drain_started_ns, drain_finished_ns),
                    "decode": (decode_started_ns, decode_finished_ns),
                    "total": (invocation_started_ns, total_finished_ns),
                }
                observer_phases_ns = {
                    name: {
                        "start": int(start),
                        "end": int(end),
                        "duration": int(end - start),
                    }
                    for name, (start, end) in phase_bounds.items()
                    if isinstance(start, int) and isinstance(end, int) and end >= start
                }
                observer_call(
                    nonce=nonce,
                    start_monotonic_ns=invocation_started_ns,
                    end_monotonic_ns=total_finished_ns,
                    timings_ns=result["timingsNs"],
                    observer_phases_ns=observer_phases_ns,
                )
            return result
        except BaseException as exc:
            pending_error = exc
            if isinstance(exc, DriverError):
                raise
            if isinstance(exc, (BrokenPipeError, OSError, subprocess.SubprocessError)):
                raise DriverError("identity observer invocation failed") from exc
            raise
        finally:
            cleanup_error: DriverError | None = None
            if process is not None:
                try:
                    if process.poll() is None:
                        self._kill_and_reap(process)
                except DriverError as exc:
                    cleanup_error = exc
                for stream_name in ("stdin", "stdout", "stderr"):
                    stream = getattr(process, stream_name, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError as exc:
                            cleanup_error = cleanup_error or DriverError(
                                "identity observer pipe could not be closed"
                            )
                            if cleanup_error.__cause__ is None:
                                cleanup_error.__cause__ = exc
                with self._lifecycle_lock:
                    self._active_processes.discard(process)
            if cleanup_error is not None:
                if pending_error is not None:
                    pending_error.add_note(str(cleanup_error))
                else:
                    raise cleanup_error


def _parse_shell_geometry(text: str) -> tuple[int, int, int, int]:
    values: dict[str, int] = {}
    for key, raw in re.findall(r"^(X|Y|WIDTH|HEIGHT)=(.*)$", text, re.MULTILINE):
        try:
            values[key] = int(raw)
        except ValueError as exc:
            raise DriverError("window geometry is not numeric") from exc
    if set(values) != {"X", "Y", "WIDTH", "HEIGHT"}:
        raise DriverError("window geometry is incomplete")
    return values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]


def _parse_xwininfo_tree(text: str) -> tuple[int, dict[int, int], dict[int, tuple[int, int, int, int]]]:
    """Parse the structural XID tree emitted by ``xwininfo -tree``.

    The queried window is identified by the single native ``xwininfo`` header;
    only geometrical window rows are structural descendants.  Hexadecimal
    values elsewhere in diagnostic text are not ancestry.  The indentation of
    each structural row is the only relationship we accept, so a root or a
    sibling mentioned in a title/property line cannot accidentally satisfy a
    client-child check.
    """

    headers = re.findall(
        r"^xwininfo:\s+Window id:\s+(0x[0-9a-fA-F]+)\b",
        text,
        re.MULTILINE,
    )
    if len(headers) != 1:
        raise DriverError("X11 window tree must contain one queried-window header")
    root = int(headers[0], 16)
    rows: list[tuple[int, int, tuple[int, int, int, int] | None]] = []
    geometry = re.compile(r"\b(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")
    for line in text.splitlines():
        match = re.match(r"^(?P<indent>\s*)(?P<xid>0x[0-9a-fA-F]+)\b", line)
        if not match:
            continue
        # xwininfo emits either a quoted WM name and class tuple, or the
        # explicit ``(has no name): ()`` form, before each row's geometry.
        # Requiring that complete marker keeps a hexadecimal token in a
        # title/property diagnostic from becoming an invented descendant.
        row_prefix = re.match(
            r'\s+(?:(?:"(?:[^"\\]|\\.)*")\s*:\s+\([^)]*\)|\([^)]*\)\s*:\s+\([^)]*\))',
            line[match.end() :],
        )
        if row_prefix is None:
            continue
        size = geometry.search(line)
        if size is None:
            continue
        rows.append(
            (
                len(match.group("indent").expandtabs(8)),
                int(match.group("xid"), 16),
                (
                    int(size.group(3)),
                    int(size.group(4)),
                    int(size.group(1)),
                    int(size.group(2)),
                ),
            )
        )
    if not rows:
        raise DriverError("X11 window tree contains no structural window rows")
    if any(xid == root for _indent, xid, _geometry in rows):
        raise DriverError("X11 window tree repeats its queried root as a child")
    if len({xid for _indent, xid, _geometry in rows}) != len(rows):
        raise DriverError("X11 window tree contains a duplicate structural window")

    first_indent = rows[0][0]
    if any(indent < first_indent for indent, _xid, _geometry in rows):
        raise DriverError("X11 window tree has invalid structural indentation")
    parents: dict[int, int] = {}
    geometries: dict[int, tuple[int, int, int, int]] = {}
    stack: list[tuple[int, int]] = [(first_indent - 1, root)]
    for indent, xid, parsed_geometry in rows:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise DriverError("X11 window tree has invalid structural ancestry")
        parents[xid] = stack[-1][1]
        stack.append((indent, xid))
        geometries[xid] = parsed_geometry
    return root, parents, geometries


class X11Backend:
    """Measured Xwayland backend using one verified-focus XTEST path."""

    def __init__(self, profile: DriverProfile, *, input_tool: PinnedInputTool | None = None):
        profile.validate()
        _validate_x11_display_screen(profile.display)
        self.profile = profile
        self.input_broker = InputBroker()
        try:
            self.input_tool = input_tool or PinnedInputTool.load()
        except InputToolError as exc:
            raise DriverError(f"private XTEST input tool is not admissible: {exc}") from exc
        self._tool_drift_callback: Callable[[str], None] | None = None
        self._tool_drifted = False
        inherited_xauthority = profile.xauthority or os.environ.get("XAUTHORITY", "")
        self.xauthority = (
            os.path.realpath(os.path.expanduser(inherited_xauthority)) if inherited_xauthority else ""
        )
        self.window_id: str | None = None
        self.last_identity: WindowIdentity | None = None
        self.last_pointer_execution: dict[str, Any] | None = None
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._active_process_groups: set[int] = set()
        self._process_lock = threading.RLock()
        self._capture_revocation_epoch = 0
        self._cancel_active = threading.Event()
        self._active_generation: int | None = None
        self._canceled_generations: set[int] = set()
        self._process_generations: dict[int, int | None] = {}
        self._process_calibration_tickets: dict[int, CalibrationTicket] = {}
        self._held_keys: set[str] = set()
        self._held_buttons: set[int] = set()
        self._anchor_lock = threading.RLock()
        self._measured_anchors: dict[str, MeasuredAnchor] = {}
        self._consumed_anchor_seals: set[str] = set()
        self._consumed_anchor_sequences: dict[str, int] = {}
        self._admission: Callable[[int, Callable[[], Any]], Any] | None = None
        self._calibration_admission: Callable[[CalibrationTicket, Callable[[], Any]], Any] | None = None
        self._register_process_group: Callable[[int | None, int], None] | None = None
        self._register_calibration_process_group: Callable[[CalibrationTicket, int], None] | None = None
        self._unregister_process_group: Callable[[int], None] | None = None
        self._calibration_ticket_current: Callable[[CalibrationTicket], bool] | None = None
        self._publish_holds: Callable[[Any, set[str], set[int]], None] | None = None
        self._admission_health: Callable[[], None] | None = None
        self._calibration_focus_ticket: CalibrationTicket | None = None
        self.last_region_receipt: TelemetryRegionReceipt | None = None
        self.last_region_attempt: dict[str, float | str | None] = {}
        self.resolver_kind = "reference-test"
        self._identity_observer: PinnedX11IdentityObserver | None = None
        self._production_observer_required = False

    @classmethod
    def production(cls, profile: DriverProfile, *, input_tool: PinnedInputTool | None = None) -> X11Backend:
        """Construct the shipped X11 backend only with the reviewed observer."""

        profile.validate()
        _validate_x11_display_screen(profile.display)
        if (
            profile.identity_observer_executable is None
            or profile.identity_observer_executable_sha256 is None
        ):
            raise DriverError("pinned Rust identity observer is required for production X11")
        backend = cls(profile, input_tool=input_tool)
        try:
            backend._identity_observer = PinnedX11IdentityObserver(
                profile.identity_observer_executable, profile.identity_observer_executable_sha256
            )
            backend.resolver_kind = "pinned-rust-v1"
            backend._production_observer_required = True
        except BaseException:
            try:
                backend.input_tool.close()
            finally:
                backend.close()
            raise
        return backend

    def close(self) -> None:
        observer = getattr(self, "_identity_observer", None)
        if observer is not None:
            observer.close()
            self._identity_observer = None

    def set_admission(self, admission: Callable[[int, Callable[[], Any]], Any] | None) -> None:
        """Install the guardian's shared generation admission fence."""

        self._admission = admission

    def set_calibration_admission(
        self, admission: Callable[[CalibrationTicket, Callable[[], Any]], Any] | None
    ) -> None:
        """Install the owned-GTK-only admission fence used before game arming."""

        self._calibration_admission = admission

    def set_calibration_ticket_validator(self, validator: Callable[[CalibrationTicket], bool] | None) -> None:
        self._calibration_ticket_current = validator

    def set_hold_publisher(self, publisher: Callable[[Any, set[str], set[int]], None] | None) -> None:
        """Publish physical holds only while a capability is being admitted."""

        self._publish_holds = publisher

    def set_admission_health(self, health: Callable[[], None] | None) -> None:
        """Recheck the watchdog immediately before committing the gate token."""

        self._admission_health = health

    def set_tool_drift_callback(self, callback: Callable[[str], None] | None) -> None:
        self._tool_drift_callback = callback

    @property
    def input_tool_digest(self) -> str:
        return self.input_tool.digest

    @property
    def input_tool_version(self) -> str:
        return self.input_tool.version

    def _check_published_tool(self, *, emergency_release: bool = False) -> None:
        # Emergency release deliberately uses the already pinned old image;
        # it must remain possible after the disk artifact is replaced/deleted.
        if emergency_release:
            return
        try:
            published = verify_artifact(self.input_tool.identity.root)
        except InputToolError as exc:
            reason = f"private XTEST input tool publication drifted: {exc}"
            self._tool_drifted = True
            callback = self._tool_drift_callback
            if callback is not None:
                callback(reason)
            raise DriverError(reason) from exc
        if published.executable_sha256 != self.input_tool.digest:
            reason = "private XTEST input tool executable digest changed after pinning"
            self._tool_drifted = True
            callback = self._tool_drift_callback
            if callback is not None:
                callback(reason)
            raise DriverError(reason)

    def set_process_group_callbacks(
        self,
        register: Callable[[int | None, int], None] | None,
        unregister: Callable[[int], None] | None,
        register_calibration: Callable[[CalibrationTicket, int], None] | None = None,
    ) -> None:
        self._register_process_group = register
        self._unregister_process_group = unregister
        self._register_calibration_process_group = register_calibration

    def activate_generation(self, generation: int) -> None:
        # A release leaves cancellation asserted.  Only the explicit arm
        # boundary may clear it, and DesktopDriver calls this after the
        # guardian has accepted the new epoch.
        self._active_generation = generation
        self._cancel_active.clear()

    def _is_cancelled(
        self, generation: int | None, calibration_ticket: CalibrationTicket | None = None
    ) -> bool:
        if calibration_ticket is not None:
            validator = getattr(self, "_calibration_ticket_current", None)
            return callable(validator) and not validator(calibration_ticket)
        if generation is not None:
            return generation in getattr(self, "_canceled_generations", set()) or (
                self._cancel_active.is_set() and getattr(self, "_active_generation", None) != generation
            )
        return self._cancel_active.is_set()

    @_identity_latency_decorator("identity_x11_query")
    def _run(
        self,
        args: list[str],
        *,
        timeout: float = 5.0,
        generation: int | None = None,
        calibration_ticket: CalibrationTicket | None = None,
        held_keys: set[str] | frozenset[str] = frozenset(),
        held_buttons: set[int] | frozenset[int] = frozenset(),
        input_schema: str | None = None,
        expected_argv: tuple[str, ...] | None = None,
        deadline_monotonic: float | None = None,
        emergency_release: bool = False,
        admitted_button_gesture: object | None = None,
        button_emission_claim: ButtonEmissionClaim | None = None,
        emission_action_generation: int | None = None,
        emission_lease_generation: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if admitted_button_gesture is not None:
            raise DriverError("button input requires a broker emission claim")
        if deadline_monotonic is None:
            deadline_monotonic = getattr(self, "_active_action_deadline", None)
        environment = os.environ.copy()
        environment["DISPLAY"] = self.profile.display
        if not self.xauthority:
            raise DriverError("Xauthority path must be explicitly configured or inherited")
        environment["XAUTHORITY"] = self.xauthority
        if self._is_cancelled(generation, calibration_ticket):
            raise DriverError("desktop action was cancelled")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise DriverError("desktop action deadline is malformed")
        if not args:
            raise DriverError("desktop command is not allowlisted: <empty>")
        command_name = os.path.basename(args[0])
        pinned_tool = getattr(self, "input_tool", None)
        if command_name == "xdotool" and pinned_tool is not None:
            self._check_published_tool(emergency_release=emergency_release)
        if generation is not None and calibration_ticket is not None:
            raise DriverError("desktop action cannot carry game and calibration authority together")
        if expected_argv is not None and tuple(args) != tuple(expected_argv):
            raise DriverError("desktop command does not match its sealed typed input plan")
        known_input_schemas = {
            "motion-key",
            "motion-timeline",
            "camera-drag",
            "pointer-motion",
            "single-click",
            "keyboard-steps",
        }
        if input_schema is not None and input_schema not in known_input_schemas:
            raise DriverError("unknown input schema")
        if input_schema == "motion-timeline" and expected_argv is None:
            raise DriverError("motion timeline requires a sealed expected argv")

        def xid(value: str) -> bool:
            return bool(re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value))

        def integer(value: str) -> bool:
            return bool(re.fullmatch(r"-?[0-9]+", value))

        def physical_keycode(value: str) -> bool:
            return bool(re.fullmatch(r"[0-9]{3}", value)) and 8 <= int(value) <= 255

        def bounded_sleep(value: str) -> bool:
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
                return False
            amount = float(value)
            return math.isfinite(amount) and 0.0 < amount <= 2.0

        def pointer_segment(
            tokens: list[str], *, require_motion: bool, minimum_moves: int = 1
        ) -> tuple[bool, frozenset[tuple[int, int]]]:
            """Validate a pointer-only timed segment with no button/key state."""

            if len(tokens) > 4096:
                return False, frozenset()
            if not tokens:
                return not require_motion, frozenset()
            index = 0
            destinations: set[tuple[int, int]] = set()
            elapsed = 0.0
            while index < len(tokens):
                if tokens[index] != "sleep" or index + 1 >= len(tokens):
                    return False, frozenset()
                if not bounded_sleep(tokens[index + 1]):
                    return False, frozenset()
                elapsed += float(tokens[index + 1])
                if elapsed > 2.0:
                    return False, frozenset()
                index += 2
                if index < len(tokens):
                    if tokens[index] == "sleep":
                        continue
                    if tokens[index] != "mousemove" or index + 2 >= len(tokens):
                        return False, frozenset()
                    if not integer(tokens[index + 1]) or not integer(tokens[index + 2]):
                        return False, frozenset()
                    destinations.add((int(tokens[index + 1]), int(tokens[index + 2])))
                    index += 3
            required_destinations = minimum_moves if require_motion else 0
            return len(destinations) >= required_destinations, frozenset(destinations)

        def keyboard_chain(tokens: list[str]) -> tuple[bool, bool]:
            """Validate one bounded sequence of explicit physical key transitions."""

            if not tokens or len(tokens) > 4096 or len(tokens) % 2:
                return False, False
            held: list[str] = []
            operations = 0
            for index in range(0, len(tokens), 2):
                operation, keysym = tokens[index : index + 2]
                if not physical_keycode(keysym):
                    return False, False
                if operation == "keydown":
                    if keysym in held:
                        return False, False
                    held.append(keysym)
                elif operation == "keyup":
                    if not held or held[-1] != keysym:
                        return False, False
                    held.pop()
                else:
                    return False, False
                operations += 1
            return operations > 0 and not held, operations > 0

        def camera_chain(tokens: list[str]) -> tuple[bool, bool]:
            """Validate optional pointer positioning plus one held right drag."""

            if not tokens or len(tokens) > 4096 or tokens[-2:] != ["mouseup", "3"]:
                return False, False
            presses = [index for index, token in enumerate(tokens) if token == "mousedown"]
            if len(presses) != 1:
                return False, False
            press = presses[0]
            if tokens[press : press + 2] != ["mousedown", "3"]:
                return False, False
            prefix = tokens[:press]
            held = tokens[press + 2 : -2]
            prefix_valid, prefix_destinations = pointer_segment(prefix, require_motion=False)
            held_valid, held_destinations = pointer_segment(held, require_motion=True, minimum_moves=1)
            destinations = prefix_destinations | held_destinations
            valid = prefix_valid and held_valid and len(destinations) >= 2
            return valid, valid

        def motion_timeline(tokens: list[str]) -> tuple[bool, bool, frozenset[str]]:
            """Validate one sealed keyboard timeline and return its key set."""

            if not tokens or len(tokens) > 256:
                return False, False, frozenset()
            index = 0
            active: set[str] = set()
            keys: set[str] = set()
            pressed_at: dict[str, float] = {}
            last_release: dict[str, float] = {}
            elapsed = 0.0
            event_count = 0
            while index < len(tokens):
                operation = tokens[index]
                if operation in {"keydown", "keyup"}:
                    if index + 1 >= len(tokens):
                        return False, False, frozenset()
                    key = tokens[index + 1]
                    if key not in {"w", "q", "e", "space"}:
                        return False, False, frozenset()
                    if operation == "keydown":
                        if key in active or key in last_release and elapsed == last_release[key]:
                            return False, False, frozenset()
                        active.add(key)
                        pressed_at[key] = elapsed
                    elif key not in active:
                        return False, False, frozenset()
                    else:
                        hold = elapsed - pressed_at[key]
                        if hold < 0.03 or hold > 0.45:
                            return False, False, frozenset()
                        active.remove(key)
                        del pressed_at[key]
                        last_release[key] = elapsed
                    keys.add(key)
                    event_count += 1
                    if event_count > 64:
                        return False, False, frozenset()
                    index += 2
                    continue
                if operation == "sleep":
                    if index + 1 >= len(tokens) or not bounded_sleep(tokens[index + 1]):
                        return False, False, frozenset()
                    elapsed += float(tokens[index + 1])
                    if elapsed > 6.0:
                        return False, False, frozenset()
                    index += 2
                    continue
                return False, False, frozenset()
            return event_count > 0 and not active, event_count > 0 and not active, frozenset(keys)

        # Every desktop subprocess is admitted by a positive argv schema.
        # This keeps newly added X11 utilities and shell-like xdotool modes
        # closed by default; a denylist cannot safely describe the mutation
        # surface of tools such as ``xprop`` or shell-like xdotool modes.
        mutating = False
        potential_mutation = (
            command_name == "xdotool"
            and len(args) > 1
            and args[1]
            in {
                "windowactivate",
                "windowfocus",
                "windowraise",
                "key",
                "keydown",
                "keyup",
                "type",
                "mousemove",
                "mousedown",
                "mouseup",
            }
        )
        if potential_mutation and generation is None and calibration_ticket is None:
            raise DriverError("input-producing desktop command requires an active capability")
        if command_name == "true":
            valid = len(args) == 1
        elif command_name == "sleep":
            valid = len(args) == 2 and bool(re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", args[1]))
        elif command_name == "gnome-screenshot":
            valid = len(args) == 3 and args[1] == "-f" and os.path.isabs(args[2])
        elif command_name == "xinput":
            valid = (
                args == ["xinput", "--list", "--short"]
                or (
                    len(args) == 4
                    and args[1] == "list"
                    and args[2] == "--long"
                    and bool(re.fullmatch(r"[1-9][0-9]*", args[3]))
                )
                or (
                    len(args) == 3 and args[1] == "list-props" and bool(re.fullmatch(r"[1-9][0-9]*", args[2]))
                )
            )
        elif command_name == "xmodmap":
            valid = args == ["xmodmap", "-pke"]
        elif command_name == "xwininfo":
            valid = tuple(args) in {
                ("xwininfo", "-root", "-tree"),
                ("xwininfo", "-root", "-stats"),
            } or (len(args) == 4 and args[1] == "-id" and xid(args[2]) and args[3] in {"-tree", "-geometry"})
        elif command_name == "xprop":
            valid = args == ["xprop", "-root", "_NET_ACTIVE_WINDOW"] or (
                len(args) >= 4
                and args[1] == "-id"
                and xid(args[2])
                and all(
                    property_name
                    in {"WM_NAME", "_NET_WM_NAME", "_NET_WM_PID", "WM_CLASS", "_NET_ACTIVE_WINDOW"}
                    for property_name in args[3:]
                )
            )
        elif command_name == "xdotool":
            subcommand = args[1] if len(args) > 1 else ""
            if subcommand == "search":
                valid = len(args) == 4 and args[2] == "--name" and bool(args[3])
            elif subcommand == "getwindowgeometry":
                valid = len(args) == 4 and args[2] == "--shell" and xid(args[3])
            elif subcommand == "getwindowfocus":
                valid = len(args) == 2
            elif subcommand == "getmouselocation":
                valid = args == ["xdotool", "getmouselocation", "--shell"]
            elif subcommand in {"windowactivate", "windowfocus", "windowraise"}:
                valid = len(args) == 4 and args[2] == "--sync" and xid(args[3])
                mutating = valid
            elif input_schema in known_input_schemas:
                if input_schema == "motion-key":
                    valid = (
                        len(args) == 7
                        and args[2] == args[6]
                        and args[1] == "keydown"
                        and args[3] == "sleep"
                        and args[5] == "keyup"
                        and bool(re.fullmatch(r"[a-z]", args[2]))
                        and bounded_sleep(args[4])
                    )
                    mutating = valid
                elif input_schema == "motion-timeline":
                    valid, mutating, _timeline_keys = motion_timeline(args[1:])
                elif input_schema == "camera-drag":
                    valid, mutating = camera_chain(args[1:])
                elif input_schema == "single-click":
                    valid = (
                        len(args) == 7
                        and args[1] == "mousedown"
                        and args[2] in {"1", "3"}
                        and args[3] == "sleep"
                        and bounded_sleep(args[4])
                        and args[5] == "mouseup"
                        and args[6] == args[2]
                    )
                    mutating = valid
                elif input_schema == "keyboard-steps":
                    valid, mutating = keyboard_chain(args[1:])
                else:
                    valid, _destinations = pointer_segment(args[1:], require_motion=True, minimum_moves=2)
                    mutating = valid
            elif subcommand in {"key", "keydown", "keyup"}:
                valid = (
                    len(args) in {3, 4}
                    and (len(args) == 3 or args[2] == "--")
                    and bool(re.fullmatch(r"[A-Za-z0-9_+.-]+", args[-1]))
                    and "--clearmodifiers" not in args
                )
                mutating = valid
            elif subcommand == "mousemove":
                valid = False
            else:
                valid = False
        else:
            valid = False
        if not valid:
            raise DriverError(f"desktop command argv is not allowlisted: {' '.join(args)}")
        if input_schema in known_input_schemas:
            if any(not isinstance(key, str) or not key for key in held_keys):
                raise DriverError("typed input argv has malformed held key metadata")
            if any(
                isinstance(button, bool) or not isinstance(button, int) or button <= 0
                for button in held_buttons
            ):
                raise DriverError("typed input argv has malformed held button metadata")
            expected_keys: frozenset[str]
            expected_buttons: frozenset[int]
            if input_schema == "motion-key":
                expected_keys = frozenset({args[2]})
                expected_buttons = frozenset()
            elif input_schema == "motion-timeline":
                _timeline_valid, _timeline_mutating, expected_keys = motion_timeline(args[1:])
                expected_buttons = frozenset()
            elif input_schema == "single-click":
                expected_keys = frozenset()
                expected_buttons = frozenset({int(args[2])})
            elif input_schema == "camera-drag":
                expected_keys = frozenset()
                expected_buttons = frozenset({3})
            elif input_schema == "keyboard-steps":
                expected_keys = frozenset(
                    args[index + 1] for index in range(1, len(args), 2) if args[index] == "keydown"
                )
                expected_buttons = frozenset()
            else:
                expected_keys = frozenset()
                expected_buttons = frozenset()
            if frozenset(held_keys) != expected_keys or frozenset(held_buttons) != expected_buttons:
                raise DriverError("typed input argv holds do not match its sealed input plan")
        elif command_name == "xdotool" and subcommand in {
            "key",
            "keydown",
            "keyup",
            "mousedown",
            "mouseup",
        }:
            # The ordinary named-key and calibration paths have no explicit
            # input schema, so derive their physical ownership directly from
            # the sealed argv as well.  This prevents a caller from asking
            # the watchdog to release a different key/button than the child
            # process was admitted to emit.
            if subcommand == "key":
                expected_keys = frozenset(part for part in args[-1].split("+") if part)
                expected_buttons = frozenset()
            elif subcommand in {"keydown", "keyup"}:
                expected_keys = frozenset({args[-1]})
                expected_buttons = frozenset()
            else:
                if admitted_button_gesture is None and subcommand in {"mousedown", "mouseup"}:
                    raise DriverError("bare button transition requires a broker-admitted gesture")
                expected_keys = frozenset()
                expected_buttons = frozenset({int(args[-1])})
            if frozenset(held_keys) != expected_keys or frozenset(held_buttons) != expected_buttons:
                raise DriverError("desktop argv holds do not match its sealed input plan")
        if mutating and generation is None and calibration_ticket is None:
            raise DriverError("input-producing desktop command requires an active capability")
        if input_schema in {"single-click", "camera-drag"}:
            if not isinstance(button_emission_claim, ButtonEmissionClaim):
                raise DriverError("button input requires a broker emission claim")
            if (
                isinstance(emission_action_generation, bool)
                or not isinstance(emission_action_generation, int)
                or isinstance(emission_lease_generation, bool)
                or not isinstance(emission_lease_generation, int)
            ):
                raise DriverError("button emission claim generations are malformed")
        elif button_emission_claim is not None:
            raise DriverError("broker emission claim is bound to a button schema")
        elif emission_action_generation is not None or emission_lease_generation is not None:
            raise DriverError("button emission claim generations require a button schema")
        if mutating and calibration_ticket is not None:
            if command_name == "xdotool" and subcommand in {"windowactivate", "windowfocus", "windowraise"}:
                if len(args) != 4 or int(args[3], 0) != calibration_ticket.target_xid:
                    raise DriverError("calibration mutation target is outside the certified GTK target")
            elif self._calibration_focus_ticket is not calibration_ticket:
                raise DriverError("calibration mutation requires a certified focused GTK target")
        if generation is not None and not callable(getattr(self, "_admission", None)):
            raise DriverError("game input admission fence is not configured")
        if calibration_ticket is not None and not callable(getattr(self, "_calibration_admission", None)):
            raise DriverError("calibration input admission fence is not configured")
        if calibration_ticket is not None and not callable(
            getattr(self, "_calibration_ticket_current", None)
        ):
            raise DriverError("calibration ticket validator is not configured")
        if input_schema in {"single-click", "camera-drag"}:
            native_button = PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT
            try:
                self.input_broker.redeem_button_emission_claim(
                    cast(ButtonEmissionClaim, button_emission_claim),
                    emitted_chain=tuple(args),
                    button=native_button,
                    action_generation=cast(int, emission_action_generation),
                    lease_generation=cast(int, emission_lease_generation),
                )
            except InputBrokerError as exc:
                raise DriverError(str(exc)) from exc
        if command_name == "xdotool":
            if pinned_tool is None:
                raise DriverError("private XTEST input tool is not configured")
            # The helper inherits the sealed memfd and execs this proc fd;
            # reopening the published pathname would defeat the pin.
            executable = self.input_tool.proc_path
        else:
            executable = _resolve_wrapped_executable(args[0])
        process: subprocess.Popen[str] | None = None
        completed_successfully = False
        gate_read, gate_write = os.pipe()
        admission_gate = AdmissionGate(gate_write)
        admission_gate_released = False
        published_holds = False
        withdrawn_holds = False
        withdrawal_error: Exception | None = None
        authority = calibration_ticket or generation

        def clear_invocation_holds() -> None:
            """Forget this invocation's local holds without waiting on I/O."""

            with self._process_lock:
                self._held_keys.difference_update(held_keys)
                self._held_buttons.difference_update(held_buttons)

        def withdraw_published_holds() -> None:
            """Withdraw holds published while the positive gate is still closed."""

            nonlocal withdrawal_error, withdrawn_holds
            if not published_holds or withdrawn_holds:
                return
            # Clear before calling the publisher: a concurrent revoke must not
            # observe a phantom local hold while a watchdog callback blocks or
            # raises during the empty publication.
            clear_invocation_holds()
            try:
                if self._publish_holds is not None:
                    self._publish_holds(authority, set(), set())
            except Exception as exc:
                withdrawal_error = exc
                raise
            withdrawn_holds = True
            withdrawal_error = None

        def spawn() -> subprocess.Popen[str]:
            nonlocal process, gate_read
            # Resolve the executable before handing it to the helper.  The
            # wrapper itself never invokes a shell and only receives this
            # already allowlisted argv vector.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _EXEC_ADMISSION_WRAPPER,
                    str(gate_read),
                    executable,
                    *args[1:],
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
                pass_fds=(gate_read, self.input_tool.image.fd)
                if command_name == "xdotool" and pinned_tool is not None
                else (gate_read,),
            )
            # Registration is part of the same admission critical section as
            # Popen.  A revoke can therefore never miss a newly-created
            # process group.
            with self._process_lock:
                if not hasattr(self, "_process_generations"):
                    self._process_generations = {}
                if not hasattr(self, "_active_process_groups"):
                    self._active_process_groups = set()
                self._active_processes.add(process)
                self._active_process_groups.add(process.pid)
                if generation is not None:
                    self._process_generations[process.pid] = generation
                elif calibration_ticket is not None:
                    self._process_calibration_tickets[process.pid] = calibration_ticket
            try:
                register_group = getattr(self, "_register_process_group", None)
                register_calibration_group = getattr(self, "_register_calibration_process_group", None)
                if generation is not None:
                    if register_group is None:
                        raise DriverError("game process-group admission is not configured")
                    register_group(generation, process.pid)
                elif calibration_ticket is not None:
                    if register_calibration_group is None:
                        raise DriverError("calibration process-group admission is not configured")
                    register_calibration_group(calibration_ticket, process.pid)
            except Exception:
                with self._process_lock:
                    self._active_processes.discard(process)
                    getattr(self, "_active_process_groups", set()).discard(process.pid)
                    getattr(self, "_process_generations", {}).pop(process.pid, None)
                    getattr(self, "_process_calibration_tickets", {}).pop(process.pid, None)
                # The admission fence may still be held by the guardian.  A
                # registration failure only needs an immediate kill here;
                # the normal finally/reaper path waits after that lock is
                # released so revocation never waits on child teardown.
                try:
                    process.kill()
                except OSError:
                    pass
                raise
            try:
                os.close(gate_read)
            except OSError:
                pass
            gate_read = -1
            return process

        def spawn_and_release_gate() -> subprocess.Popen[str]:
            """Spawn/register the wrapper and open it only inside admission."""

            nonlocal admission_gate_released, gate_write, published_holds
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                raise DriverError("desktop action deadline expired before emission")
            admitted = spawn()
            if self._is_cancelled(generation, calibration_ticket):
                try:
                    admitted.kill()
                except OSError:
                    pass
                raise DriverError("desktop action was cancelled")
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                try:
                    admitted.kill()
                except OSError:
                    pass
                raise DriverError("desktop action deadline expired before emission")
            if self._publish_holds is not None and (generation is not None or calibration_ticket is not None):
                # The wrapper is still blocked on its positive admission gate.  Publish the
                # exact items immediately before releasing that gate, after
                # Popen and PGID registration, so a rejected spawn cannot
                # cause a keyup/mouseup for input that was never emitted.
                self._publish_holds(calibration_ticket or generation, set(held_keys), set(held_buttons))
                with self._process_lock:
                    self._held_keys = set(held_keys)
                    self._held_buttons = set(held_buttons)
                published_holds = True
            admission_health = getattr(self, "_admission_health", None)
            if admission_health is not None:
                admission_health()
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                clear_invocation_holds()
                terminated = self._terminate_process(admitted)
                try:
                    withdraw_published_holds()
                except Exception as exc:
                    raise DriverError("desktop action holds could not be withdrawn after deadline") from exc
                if not terminated:
                    raise DriverError("desktop action wrapper could not be reaped after deadline")
                raise DriverError("desktop action deadline expired before emission")
            try:
                admission_gate.release()
            except OSError as exc:
                try:
                    admitted.kill()
                except OSError:
                    pass
                raise DriverError("desktop action gate could not be released") from exc
            gate_write = -1
            admission_gate_released = True
            return admitted

        try:
            admission = (
                getattr(self, "_calibration_admission", None)
                if calibration_ticket is not None
                else getattr(self, "_admission", None)
            )
            admission_token: Any = calibration_ticket if calibration_ticket is not None else generation
            if admission_token is not None and admission is None:
                raise DriverError("desktop action admission fence is not configured")
            if admission_token is not None and admission is not None:
                admission(admission_token, spawn_and_release_gate)
            else:
                spawn_and_release_gate()
            if process is None:
                raise DriverError("desktop command was not admitted")
            # The wrapper has already exec'd. Its positive token was written
            # only after Popen, process-group tracking, and the generation or
            # ticket admission fence completed. Waiting for the target's
            # result stays outside that fence.
            now = time.monotonic()
            deadline = now + timeout
            if deadline_monotonic is not None:
                deadline = min(deadline, float(deadline_monotonic))
            while True:
                if self._is_cancelled(generation, calibration_ticket):
                    terminated = self._terminate_process(process)
                    if not terminated:
                        raise DriverError("desktop action termination could not be confirmed")
                    raise DriverError("desktop action was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminated = self._terminate_process(process)
                    if not terminated:
                        raise DriverError("desktop command termination could not be confirmed")
                    raise DriverError(f"desktop command timed out: {' '.join(args)}")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise DriverError(
                    f"desktop command failed: {' '.join(args)}: {stderr.strip() or process.returncode}"
                )
            completed_successfully = True
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except DriverError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise DriverError(f"desktop command failed: {' '.join(args)}: {exc}") from exc
        finally:
            if published_holds and not admission_gate_released:
                # Admission has not crossed GO.  Clear local state before
                # closing pipes, terminating, polling, reaping, or invoking
                # the external withdrawal publisher, so a concurrent revoke
                # cannot observe a phantom hold during bounded cleanup.
                clear_invocation_holds()
            if gate_write >= 0:
                try:
                    admission_gate.close()
                except OSError:
                    pass
            if gate_read >= 0:
                try:
                    os.close(gate_read)
                except OSError:
                    pass
            if process is not None:
                if process.poll() is None:
                    self._terminate_process(process)
                # The wrapper's admission gate is still closed until the
                # positive release succeeds.  Withdraw this invocation's
                # published holds before branching on whether termination
                # has been confirmed; a child that is slow to reap must not
                # leave local state that a later cleanup interprets as a
                # physical hold.
                if published_holds and not admission_gate_released:
                    try:
                        withdraw_published_holds()
                    except Exception:
                        pass
                if process.poll() is not None:
                    # A timeout or cancellation may terminate the wrapper
                    # before the normal communicate() path.  Drain and close
                    # its pipes before dropping the process reference so a
                    # bounded failure cannot leak descriptors into later
                    # actions.
                    try:
                        process.communicate()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    if self._publish_holds is not None and not withdrawn_holds:
                        try:
                            # A cleanly completed XTEST command owns no
                            # remaining physical item.  A failed/cancelled
                            # command may have been killed between key/button
                            # press and release, so retain the exact current
                            # set for the explicit release authority.
                            self._publish_holds(
                                authority,
                                set() if completed_successfully else set(self._held_keys),
                                set() if completed_successfully else set(self._held_buttons),
                            )
                            if not admission_gate_released and published_holds:
                                withdrawn_holds = True
                                withdrawal_error = None
                        except Exception as exc:
                            # After GO, this is the independent actual-input
                            # release path; its existing best-effort behavior
                            # must not be reclassified as a pre-GO withdrawal
                            # failure.  Only a still-gated published hold is
                            # strict and surfaced below.
                            if not admission_gate_released and published_holds:
                                withdrawal_error = exc
                    self._forget_process(process)
                else:
                    if self._publish_holds is not None and not withdrawn_holds and published_holds:
                        # The child is still pending reaping.  Retry the
                        # still-gated empty publication before handing it to
                        # the reaper so a transient publisher failure is
                        # surfaced without leaving the lease advertised.
                        try:
                            self._publish_holds(authority, set(), set())
                            withdrawn_holds = True
                            withdrawal_error = None
                        except Exception as exc:
                            withdrawal_error = exc
                    self._reap_process_later(
                        process,
                        authority,
                        clear_holds=completed_successfully,
                    )
            if completed_successfully and (generation is not None or calibration_ticket is not None):
                clear_invocation_holds()
            if withdrawal_error is not None:
                raise DriverError("desktop action holds could not be withdrawn") from withdrawal_error

    @property
    def active_process_groups(self) -> frozenset[int]:
        with self._process_lock:
            return frozenset(getattr(self, "_active_process_groups", set()))

    def _forget_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes.discard(process)
            getattr(self, "_active_process_groups", set()).discard(process.pid)
            getattr(self, "_process_generations", {}).pop(process.pid, None)
            getattr(self, "_process_calibration_tickets", {}).pop(process.pid, None)
        unregister_group = getattr(self, "_unregister_process_group", None)
        if unregister_group is not None:
            try:
                unregister_group(process.pid)
            except Exception:
                pass

    def _reap_process_later(
        self,
        process: subprocess.Popen[str],
        authority: Any,
        *,
        clear_holds: bool = False,
        on_reaped: Callable[[int], None] | None = None,
    ) -> None:
        def reap() -> None:
            try:
                process.communicate()
            except (OSError, ValueError):
                try:
                    process.wait()
                except (OSError, subprocess.SubprocessError):
                    return
            if process.poll() is None:
                return
            if clear_holds and self._publish_holds is not None:
                try:
                    self._publish_holds(authority, set(), set())
                except Exception:
                    pass
            self._forget_process(process)
            if on_reaped is not None:
                try:
                    on_reaped(process.pid)
                except Exception:
                    pass

        threading.Thread(target=reap, name="crowley-process-reaper", daemon=True).start()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> bool:
        if process.poll() is not None:
            return True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                return False
        return process.poll() is not None

    def _xprop(
        self,
        xid: str,
        *properties: str,
        deadline_monotonic: float | None = None,
    ) -> str:
        return self._run(["xprop", "-id", xid, *properties], deadline_monotonic=deadline_monotonic).stdout

    def _release_command(self, args: list[str]) -> bool:
        environment = os.environ.copy()
        environment["DISPLAY"] = self.profile.display
        if not self.xauthority:
            return False
        environment["XAUTHORITY"] = self.xauthority
        command = list(args)
        pass_fds: tuple[int, ...] = ()
        if command and os.path.basename(command[0]) == "xdotool":
            self._check_published_tool(emergency_release=True)
            command[0] = self.input_tool.proc_path
            pass_fds = (self.input_tool.image.fd,)
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                env=environment,
                pass_fds=pass_fds,
            )
            return completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def observe_keymap(self, *, deadline_monotonic: float | None = None) -> dict[str, int]:
        """Read the current X server keymap through the owned backend."""

        mapping: dict[str, int] = {}
        for keycode, symbols in self.observe_keymap_levels(deadline_monotonic=deadline_monotonic).items():
            # Calibration's generic vocabulary is level-zero only.  Shifted
            # symbols stay in the level table and are never flattened into a
            # runtime movement binding.
            mapping.setdefault(symbols[0], keycode)
        if not mapping:
            raise DriverError("owned X11 backend returned an empty keymap")
        return mapping

    def observe_keymap_levels(self, *, deadline_monotonic: float | None = None) -> dict[int, tuple[str, ...]]:
        """Return each keycode's ordered XKB level-0/shift symbols.

        Runtime movement may emit only a level-0 keysym.  The old flattened
        map cannot distinguish ``w`` from a shifted ``W`` that shares the same
        keycode, so callers that emit physical movement keys must use this
        sealed level ordering as well as the keycode.
        """

        output = self._run(["xmodmap", "-pke"], timeout=5, deadline_monotonic=deadline_monotonic).stdout
        levels: dict[int, tuple[str, ...]] = {}
        for line in output.splitlines():
            match = re.match(r"^\s*keycode\s+(\d+)\s*=\s*(.*?)\s*$", line)
            if not match:
                continue
            keycode = int(match.group(1))
            symbols = tuple(keysym for keysym in match.group(2).split() if keysym and keysym != "NoSymbol")
            if not symbols or keycode in levels:
                if keycode in levels and levels[keycode] != symbols:
                    raise DriverError("owned X11 keymap repeats a keycode with conflicting levels")
                continue
            levels[keycode] = symbols
        if not levels:
            raise DriverError("owned X11 backend returned no keymap levels")
        return levels

    def observe_keysym_modifier_mask(self, keysym: str) -> int:
        """Read the server's native modifier mask for one physical keysym."""

        if not isinstance(keysym, str) or not keysym or not keysym.isascii():
            raise DriverError("native modifier lookup requires an ASCII keysym")
        library = ctypes.CDLL("libX11.so.6")
        library.XOpenDisplay.argtypes = [ctypes.c_char_p]
        library.XOpenDisplay.restype = ctypes.c_void_p
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        library.XCloseDisplay.restype = ctypes.c_int
        library.XStringToKeysym.argtypes = [ctypes.c_char_p]
        library.XStringToKeysym.restype = ctypes.c_ulong
        library.XkbKeysymToModifiers.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        library.XkbKeysymToModifiers.restype = ctypes.c_uint
        display = library.XOpenDisplay(self.profile.display.encode())
        if not display:
            raise DriverError("native X11 modifier lookup could not open the configured display")
        try:
            symbol = library.XStringToKeysym(keysym.encode("ascii"))
            if symbol == 0:
                raise DriverError(f"native X11 modifier lookup has no keysym for {keysym!r}")
            mask = int(library.XkbKeysymToModifiers(display, symbol))
            if mask <= 0 or mask & (mask - 1):
                raise DriverError(f"native X11 modifier mask for {keysym!r} is absent or ambiguous")
            return mask
        finally:
            library.XCloseDisplay(display)

    def observe_native_input_state(
        self, root_xid: int
    ) -> tuple[tuple[int, int, int], tuple[int, ...], tuple[int, ...]]:
        """Read live XKB modifiers, key state and root pointer buttons without input."""

        if isinstance(root_xid, bool) or not isinstance(root_xid, int) or root_xid <= 0:
            raise DriverError("native input state requires a measured root window")

        class XkbState(ctypes.Structure):
            _fields_ = [
                ("group", ctypes.c_ubyte),
                ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort),
                ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte),
                ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte),
                ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte),
                ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte),
                ("pointer_buttons", ctypes.c_ushort),
            ]

        display_name = self.profile.display.encode()
        library = ctypes.CDLL("libX11.so.6")
        library.XOpenDisplay.argtypes = [ctypes.c_char_p]
        library.XOpenDisplay.restype = ctypes.c_void_p
        library.XCloseDisplay.argtypes = [ctypes.c_void_p]
        library.XCloseDisplay.restype = ctypes.c_int
        library.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char)]
        library.XQueryKeymap.restype = ctypes.c_int
        library.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        library.XQueryPointer.restype = ctypes.c_int
        library.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(XkbState)]
        library.XkbGetState.restype = ctypes.c_int
        display = library.XOpenDisplay(display_name)
        if not display:
            raise DriverError("native X11 state query could not open the configured display")
        try:
            keymap = ctypes.create_string_buffer(32)
            if not library.XQueryKeymap(display, keymap):
                raise DriverError("native X11 key state query failed")
            held_keys = tuple(
                keycode for keycode in range(8, 256) if keymap.raw[keycode >> 3] & (1 << (keycode & 7))
            )
            root = ctypes.c_ulong(root_xid)
            root_return = ctypes.c_ulong()
            child_return = ctypes.c_ulong()
            root_x = ctypes.c_int()
            root_y = ctypes.c_int()
            win_x = ctypes.c_int()
            win_y = ctypes.c_int()
            mask = ctypes.c_uint()
            if not library.XQueryPointer(
                display,
                root,
                ctypes.byref(root_return),
                ctypes.byref(child_return),
                ctypes.byref(root_x),
                ctypes.byref(root_y),
                ctypes.byref(win_x),
                ctypes.byref(win_y),
                ctypes.byref(mask),
            ):
                raise DriverError("native X11 pointer state query failed")
            pointer_bits = (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12)
            held_buttons = tuple(button for button in range(1, 6) if mask.value & (1 << (7 + button)))
            if mask.value & pointer_bits:
                # XQueryPointer's mask is an independent button-state witness;
                # keep its interpretation explicit and refuse unknown bits.
                if not held_buttons:
                    raise DriverError("native X11 pointer button state is ambiguous")
            state = XkbState()
            if library.XkbGetState(display, 0x100, ctypes.byref(state)) != 0:
                raise DriverError("native XKB modifier state query failed")
            modifiers = (int(state.locked_mods), int(state.latched_mods), int(state.base_mods))
            return modifiers, held_keys, held_buttons
        finally:
            library.XCloseDisplay(display)

    def synthetic_keyboard_profile(
        self,
        operations: tuple[Any, ...],
        *,
        keymap: Mapping[str, int],
        calibration_ticket: CalibrationTicket,
    ) -> None:
        """Emit the finite keyboard profile through the owned XTEST path.

        The calibration workflow supplies operations whose steps describe the
        physical transitions that the runtime will later correlate. Each
        bounded pair or chord is emitted as explicit XTEST keydown/keyup
        commands, preserving the same transitions observed by XI2. Every
        subprocess carries the exact GTK ticket and therefore remains behind
        the common calibration admission fence.
        """

        if not isinstance(calibration_ticket, CalibrationTicket):
            raise DriverError("keyboard calibration requires an exact calibration ticket")
        if not operations:
            raise DriverError("keyboard calibration profile is empty")
        if not isinstance(keymap, Mapping) or not keymap:
            raise DriverError("keyboard calibration has no measured keymap")
        from .calibration import KeyboardProbeOperation, KeyboardProbeStep

        for operation in operations:
            if not isinstance(operation, KeyboardProbeOperation) or not operation.steps:
                raise DriverError("keyboard calibration operation is malformed")
            if any(not isinstance(step, KeyboardProbeStep) for step in operation.steps):
                raise DriverError("keyboard calibration step is malformed")
            steps = operation.steps
            index = 0
            while index < len(steps):
                step = steps[index]
                # The normal runtime emissions are complete key pairs.  This
                # keeps ASCII text bounded one character at a time and makes
                # the profile rows directly reusable for dispatch.
                if (
                    index + 1 < len(steps)
                    and step.phase == "press"
                    and steps[index + 1].phase == "release"
                    and steps[index + 1].keysym == step.keysym
                ):
                    transition_steps = (("press", step.keysym), ("release", step.keysym))
                    index += 2
                else:
                    # A chord is one contiguous press run followed by its
                    # reverse-order release run. Consume only that physical
                    # unit so later ordinary key pairs in the same command
                    # remain separate bounded emissions.
                    press_end = index
                    pressed: list[str] = []
                    while press_end < len(steps) and steps[press_end].phase == "press":
                        keysym = steps[press_end].keysym
                        if not keysym:
                            raise DriverError("keyboard calibration chord is malformed")
                        pressed.append(keysym)
                        press_end += 1
                    release_end = press_end + len(pressed)
                    released_steps = steps[press_end:release_end]
                    if (
                        not pressed
                        or len(released_steps) != len(pressed)
                        or any(item.phase != "release" or not item.keysym for item in released_steps)
                    ):
                        raise DriverError("keyboard calibration chord is malformed")
                    released = [item.keysym for item in released_steps]
                    if released != list(reversed(pressed)):
                        raise DriverError("keyboard calibration chord transition order is malformed")
                    transition_steps = tuple(
                        [("press", keysym) for keysym in pressed]
                        + [("release", item.keysym) for item in released_steps]
                    )
                    index = release_end
                argv, held = self._keyboard_transition_argv(transition_steps, keymap)
                self._run(
                    argv,
                    calibration_ticket=calibration_ticket,
                    held_keys=set(held),
                    input_schema="keyboard-steps",
                    expected_argv=tuple(argv),
                )

    def release_held_input(self) -> None:
        """Cancel active command groups, then release every tracked key/button."""

        if not hasattr(self, "_cancel_active"):
            self._cancel_active = threading.Event()
        if not hasattr(self, "_canceled_generations"):
            self._canceled_generations = set()
        with self._process_lock:
            if not hasattr(self, "_capture_revocation_epoch"):
                self._capture_revocation_epoch = 0
            self._capture_revocation_epoch += 1
            self._cancel_active.set()
            processes = tuple(self._active_processes)
            self._canceled_generations.update(
                generation
                for generation in getattr(self, "_process_generations", {}).values()
                if generation is not None
            )
        failed = False
        for process in processes:
            self._terminate_process(process)
            if process.poll() is None:
                failed = True
        keys = tuple(self._held_keys)
        buttons = tuple(self._held_buttons)
        for key in keys:
            result = self._release_command(["xdotool", "keyup", key])
            if result is not False:
                self._held_keys.discard(key)
            else:
                failed = True
        for button in buttons:
            result = self._release_command(["xdotool", "mouseup", str(button)])
            if result is not False:
                self._held_buttons.discard(button)
            else:
                failed = True
        if failed:
            raise DriverError("one or more native process groups or held inputs could not be released")

    def install_measured_anchor(self, anchor: MeasuredAnchor) -> None:
        anchor.verify_seal()
        with self._anchor_lock:
            self._measured_anchors[anchor.action] = anchor
            getattr(self, "_consumed_anchor_seals", set()).discard(anchor.evidence_hash)

    def consume_measured_anchor(self, action: str, anchor: MeasuredAnchor) -> None:
        with self._anchor_lock:
            current = self._measured_anchors.get(action)
            if current is not anchor or current.evidence_hash != anchor.evidence_hash:
                raise DriverError("measured anchor is no longer current")
            if anchor.evidence_hash in getattr(self, "_consumed_anchor_seals", set()):
                raise DriverError("measured anchor has already been consumed")
            try:
                digest = hashlib.sha256(Path(anchor.capture_path).read_bytes()).hexdigest()
            except OSError as exc:
                raise DriverError("native anchor retained capture is unavailable") from exc
            if digest != anchor.capture_sha256:
                raise DriverError("native anchor retained capture changed")
            if (
                isinstance(anchor.capture_monotonic, bool)
                or not isinstance(anchor.capture_monotonic, (int, float))
                or anchor.capture_monotonic < 0
                or not anchor.identity_fingerprint
            ):
                raise DriverError("native anchor capture timestamp is incomplete")
            prior_sequence = self._consumed_anchor_sequences.get(action)
            if prior_sequence is not None and anchor.capture_sequence <= prior_sequence:
                raise DriverError("native anchor capture sequence is not newer than the consumed proof")
            getattr(self, "_consumed_anchor_seals", set()).add(anchor.evidence_hash)
            self._consumed_anchor_sequences[action] = anchor.capture_sequence

    @staticmethod
    def _pid(text: str) -> int:
        match = re.search(r"_NET_WM_PID\([^)]*\)\s*=\s*(\d+)", text)
        if not match:
            raise DriverError("game window has no _NET_WM_PID")
        return int(match.group(1))

    @staticmethod
    def _wm_name(text: str) -> str:
        match = re.search(r"(?:_NET_WM_NAME|WM_NAME)\([^)]*\)\s*=\s*(?:UTF8_STRING\s+)?\"(.*?)\"", text)
        if not match:
            raise DriverError("game window has no WM_NAME")
        return match.group(1)

    @staticmethod
    def _wm_class(text: str) -> str:
        match = re.search(r"WM_CLASS\([^)]*\)\s*=\s*\"(.*?)\",\s*\"(.*?)\"", text)
        if not match:
            raise DriverError("game window has no WM_CLASS")
        return f"{match.group(1)},{match.group(2)}"

    def _process_executable(self, pid: int) -> tuple[str, str]:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            start = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
            status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
            linux_executable = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
        except (OSError, IndexError) as exc:
            raise DriverError(f"cannot inspect game process {pid}") from exc
        expected = self.profile.executable
        candidates = [re.split(r"[/\\]", value)[-1] for value in cmdline if value]
        matches = [value for value in candidates if value == expected]
        if len(matches) != 1:
            raise DriverError("game PID does not expose the configured Windows executable")
        process_name = next(
            (line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("Name:")),
            "",
        )
        if not process_name or process_name != expected:
            raise DriverError("game PID has an unexpected Linux process identity")
        loader = os.path.basename(linux_executable)
        if loader != "wine-preloader":
            raise DriverError("game PID is not running under the configured Wine loader")
        if self.profile.wine_prefix:
            environment = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
            values = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for item in environment.split("\0")
                if "=" in item
            }
            configured = os.path.realpath(os.path.expanduser(self.profile.wine_prefix))
            observed = os.path.realpath(os.path.expanduser(values.get("WINEPREFIX", "")))
            if not observed or observed != configured:
                raise DriverError("game PID is outside the configured Wine prefix")
        return matches[0], start

    @_identity_latency_decorator("proc_match")
    def _matching_game_pids(self) -> set[int]:
        matches: set[int] = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                self._process_executable(pid)
            except (DriverError, OSError, ValueError):
                continue
            matches.add(pid)
        return matches

    def _child_geometry(
        self,
        xid: str,
        outer: tuple[int, int, int, int],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, tuple[int, int, int, int]]:
        tree = self._run(["xwininfo", "-id", xid, "-tree"], deadline_monotonic=deadline_monotonic).stdout
        root, parents, geometries = _parse_xwininfo_tree(tree)
        outer_xid = int(xid, 0)
        if root != outer_xid:
            raise DriverError("X11 window tree root does not match the verified outer window")
        candidates = [
            (child, *geometries[child])
            for child, parent in parents.items()
            if parent == outer_xid
            and child in geometries
            and geometries[child][2:] == (self.profile.client_width, self.profile.client_height)
        ]
        if not candidates:
            raise DriverError("configured game child geometry is missing")
        if len(candidates) != 1:
            raise DriverError("multiple client children match configured geometry")
        child, rel_x, rel_y, width, height = candidates[0]
        return child, (outer[0] + rel_x, outer[1] + rel_y, width, height)

    def _verified_window_relationship(
        self, outer_xid: int, child_xid: int, *, deadline_monotonic: float | None = None
    ) -> tuple[int, frozenset[int]]:
        """Pin the measured root -> fullscreen outer -> client relationship.

        The game certificate is deliberately narrower than a generic window
        ancestry predicate.  A root, WM helper, or sibling must never become
        an event target merely because it happens to be an ancestor in a later
        tree.  The current native layout is a direct root child with exactly
        one direct client child; a reparenting or extra helper fails closed.
        """

        tree = self._run(["xwininfo", "-root", "-tree"], deadline_monotonic=deadline_monotonic).stdout
        root, parents, _geometries = _parse_xwininfo_tree(tree)
        if parents.get(outer_xid) != root:
            raise DriverError("game outer window is not the verified direct root child")
        if parents.get(child_xid) != outer_xid:
            raise DriverError("game client is not the verified direct outer child")
        direct_children = {xid for xid, parent in parents.items() if parent == outer_xid}
        if direct_children != {child_xid}:
            raise DriverError("game outer has an unexpected helper or sibling child")
        return root, frozenset({outer_xid, child_xid})

    @_identity_latency_decorator("identity_return")
    def resolve_identity(
        self,
        *,
        require_foreground: bool = True,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        observer = self._identity_observer
        if observer is not None:
            return self._resolve_identity_pinned(
                observer,
                require_foreground=require_foreground,
                deadline_monotonic=deadline_monotonic,
            )
        if self._production_observer_required:
            raise DriverError("production X11 backend is closed")
        if not self.xauthority:
            raise DriverError("Xauthority path must be explicitly configured or inherited")
        if not self.profile.wine_prefix:
            raise DriverError("Wine prefix must be explicitly configured")
        matching_pids = self._matching_game_pids()
        if len(matching_pids) != 1:
            raise DriverError(f"expected one verified game process, found {len(matching_pids)}")
        result = self._run(
            ["xdotool", "search", "--name", self.profile.window_title],
            deadline_monotonic=deadline_monotonic,
        )
        candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        identities: list[WindowIdentity] = []
        identity_errors: list[str] = []
        for xid in candidates:
            try:
                outer = _parse_shell_geometry(
                    self._run(
                        ["xdotool", "getwindowgeometry", "--shell", xid],
                        deadline_monotonic=deadline_monotonic,
                    ).stdout
                )
                props = self._xprop(
                    xid,
                    "WM_NAME",
                    "_NET_WM_PID",
                    "WM_CLASS",
                    deadline_monotonic=deadline_monotonic,
                )
                title = self._wm_name(props)
                wm_class = self._wm_class(props)
                if title != self.profile.window_title:
                    continue
                if any(value != self.profile.window_class for value in wm_class.split(",")):
                    continue
                pid = self._pid(props)
                if pid not in matching_pids:
                    continue
                executable, start = self._process_executable(pid)
                child_xid, child_geometry = self._child_geometry(
                    xid, outer, deadline_monotonic=deadline_monotonic
                )
                root_xid, target_event_xids = self._verified_window_relationship(
                    int(xid, 0), child_xid, deadline_monotonic=deadline_monotonic
                )
                focus = int(
                    self._run(
                        ["xdotool", "getwindowfocus"], deadline_monotonic=deadline_monotonic
                    ).stdout.strip(),
                    0,
                )
                root_props = self._run(
                    ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                    deadline_monotonic=deadline_monotonic,
                ).stdout
                active_match = re.search(r"=\s*(0x[0-9a-fA-F]+)", root_props)
                active = int(active_match.group(1), 16) if active_match else 0
                payload = {
                    "xid": int(xid, 0),
                    "pid": pid,
                    "exe": executable,
                    "start": start,
                    "child": child_xid,
                    "outer": outer,
                    "childGeometry": child_geometry,
                    "root": root_xid,
                    "targetEventXids": sorted(target_event_xids),
                    "display": self.profile.display,
                    "xauthority": self.xauthority,
                    "winePrefix": os.path.realpath(os.path.expanduser(self.profile.wine_prefix)),
                    "wineLoader": "wine-preloader",
                    "driver": self.input_tool_version,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                identities.append(
                    WindowIdentity(
                        int(xid, 0),
                        pid,
                        executable,
                        title,
                        wm_class,
                        child_xid,
                        outer,
                        child_geometry,
                        focus if focus else active,
                        start,
                        self.profile.display,
                        fingerprint,
                        root_xid,
                        int(xid, 0),
                        target_event_xids,
                    )
                )
            except DriverError as exc:
                identity_errors.append(str(exc))
                continue
        if len(identities) != 1:
            if len(candidates) == 1 and identity_errors:
                raise DriverError(identity_errors[0])
            raise DriverError(f"expected one verified game client, found {len(identities)}")
        identity = identities[0]
        if identity.outer_geometry[2:] != (self.profile.outer_width, self.profile.outer_height):
            raise DriverError("game outer geometry changed")
        if identity.child_geometry[2:] != (self.profile.client_width, self.profile.client_height):
            raise DriverError("game client geometry changed")
        if require_foreground and identity.foreground_xid not in {identity.xid, identity.child_xid}:
            raise DriverError("game client is not foreground")
        if identity.outer_geometry[:2] != (0, 0) or identity.child_geometry[:2] != (0, 0):
            raise DriverError("game outer/child origin changed from the calibrated origin")
        self.window_id = hex(identity.xid)
        self.last_identity = identity
        return identity

    def _resolve_identity_pinned(
        self,
        observer: PinnedX11IdentityObserver,
        *,
        require_foreground: bool,
        deadline_monotonic: float | None,
    ) -> WindowIdentity:
        if not self.xauthority:
            raise DriverError("Xauthority path must be explicitly configured or inherited")
        if not self.profile.wine_prefix:
            raise DriverError("Wine prefix must be explicitly configured")
        facts = observer.invoke(
            self.profile,
            xauthority=self.xauthority,
            deadline_monotonic=deadline_monotonic,
            cancel_event=self._cancel_active,
        )
        if (
            facts["matchingProcessCount"] != 1
            or facts["candidateWindowCount"] != 1
            or facts["exe"] != self.profile.executable
            or facts["statusName"] != self.profile.executable
            or facts["loader"] != "wine-preloader"
            or facts["winePrefix"] != os.path.realpath(os.path.expanduser(self.profile.wine_prefix))
            or facts["display"] != self.profile.display
            or facts["title"] != self.profile.window_title
            or not facts["class"].split(",")
            or any(value != self.profile.window_class for value in facts["class"].split(","))
        ):
            raise DriverError("pinned identity observer facts do not match the configured profile")
        outer = tuple(facts["outerGeometry"])
        child_geometry = tuple(facts["childGeometry"])
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool) for value in outer + child_geometry
            )
            or len(outer) != 4
            or len(child_geometry) != 4
            or facts["outer"] != facts["xid"]
            or facts["child"] == facts["xid"]
            or tuple(facts["outerChildren"]) != (facts["child"],)
            or facts["outerRoot"] == 0
            or facts["outerParent"] != facts["outerRoot"]
        ):
            raise DriverError("pinned identity observer geometry/tree facts are malformed")
        payload = {
            "xid": facts["xid"],
            "pid": facts["pid"],
            "exe": facts["exe"],
            "start": facts["start"],
            "child": facts["child"],
            "outer": outer,
            "childGeometry": child_geometry,
            "root": facts["outerRoot"],
            "targetEventXids": sorted({facts["xid"], facts["child"]}),
            "display": self.profile.display,
            "xauthority": self.xauthority,
            "winePrefix": os.path.realpath(os.path.expanduser(self.profile.wine_prefix)),
            "wineLoader": "wine-preloader",
            "driver": self.input_tool_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        identity = WindowIdentity(
            int(facts["xid"]),
            int(facts["pid"]),
            facts["exe"],
            facts["title"],
            facts["class"],
            int(facts["child"]),
            outer,
            child_geometry,
            int(facts["focus"] or facts["active"]),
            facts["start"],
            self.profile.display,
            fingerprint,
            int(facts["outerRoot"]),
            int(facts["xid"]),
            frozenset({int(facts["xid"]), int(facts["child"])}),
        )
        if identity.outer_geometry[2:] != (self.profile.outer_width, self.profile.outer_height):
            raise DriverError("game outer geometry changed")
        if identity.child_geometry[2:] != (self.profile.client_width, self.profile.client_height):
            raise DriverError("game client geometry changed")
        if require_foreground and identity.foreground_xid not in {identity.xid, identity.child_xid}:
            raise DriverError("game client is not foreground")
        if identity.outer_geometry[:2] != (0, 0) or identity.child_geometry[:2] != (0, 0):
            raise DriverError("game outer/child origin changed from the calibrated origin")
        self.window_id = hex(identity.xid)
        self.last_identity = identity
        return identity

    def _find_window(self) -> str:
        return hex(self.resolve_identity().xid)

    def verify_identity(self, identity: WindowIdentity) -> WindowIdentity:
        fresh = self.resolve_identity()
        if fresh.fingerprint != identity.fingerprint:
            raise DriverError("verified client identity is stale")
        return fresh

    def _verify_window(self, window: str) -> WindowIdentity:
        identity = self.resolve_identity()
        if int(window, 0) != identity.xid:
            raise DriverError("window identity changed")
        return identity

    def _focus(
        self,
        identity: WindowIdentity,
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        self._run(
            ["xdotool", "windowactivate", "--sync", hex(identity.xid)],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        )
        fresh = self.resolve_identity(deadline_monotonic=deadline_monotonic)
        if fresh.fingerprint != identity.fingerprint or fresh.foreground_xid not in {
            fresh.xid,
            fresh.child_xid,
        }:
            raise DriverError("verified game window did not retain focus")

    @staticmethod
    def _keysym_for_char(char: str) -> str | None:
        if char.isascii() and char.isalpha() and char.isupper():
            return f"Shift_L+{char.lower()}"
        if char.isascii() and char.isalnum():
            return char
        return {
            "/": "slash",
            " ": "space",
            "-": "minus",
            "=": "equal",
            ";": "semicolon",
            "'": "apostrophe",
            ",": "comma",
            "(": "Shift_L+9",
            ")": "Shift_L+0",
        }.get(char)

    @staticmethod
    def _keyboard_transition_argv(
        steps: tuple[tuple[str, str], ...], keymap: Mapping[str, int]
    ) -> tuple[list[str], frozenset[str]]:
        """Build exact keycode transitions and their bounded held-key set.

        Numeric keycode tokens make xdotool send the measured physical key
        directly. Passing modifier keysyms through xdotool's keysym path also
        causes it to synthesize that modifier from the key's own modifier map,
        producing an unmatched raw release on this server.
        """

        if not steps or not isinstance(keymap, Mapping):
            raise DriverError("keyboard transition chain is empty")
        argv = ["xdotool"]
        held: list[str] = []
        for phase, keysym in steps:
            if not isinstance(keysym, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", keysym):
                raise DriverError("keyboard transition keysym is malformed")
            keycode = keymap.get(keysym)
            if isinstance(keycode, bool) or not isinstance(keycode, int) or not 8 <= keycode <= 255:
                raise DriverError(f"keyboard transition keysym {keysym!r} has no measured keycode")
            # xdo first tries XStringToKeysym and only then parses a numeric
            # keycode. Three digits keep keycodes 8 and 9 distinct from the
            # printable keysyms "8" and "9".
            physical = f"{keycode:03d}"
            if phase == "press":
                if physical in held:
                    raise DriverError("keyboard transition presses a key that is already held")
                held.append(physical)
                argv.extend(("keydown", physical))
            elif phase == "release":
                if not held or physical != held[-1]:
                    raise DriverError("keyboard transition releases a key that is not held")
                held.pop()
                argv.extend(("keyup", physical))
            else:
                raise DriverError("keyboard transition phase is malformed")
        if held:
            raise DriverError("keyboard transition chain leaves a key held")
        return argv, frozenset(f"{keymap[keysym]:03d}" for _phase, keysym in steps)

    def _key(
        self,
        key: str,
        *,
        generation: int | None = None,
        keymap: Mapping[str, int],
        deadline_monotonic: float | None = None,
    ) -> None:
        argv, held = self._keyboard_transition_argv((("press", key), ("release", key)), keymap)
        self._run(
            argv,
            generation=generation,
            held_keys=set(held),
            input_schema="keyboard-steps",
            expected_argv=tuple(argv),
            deadline_monotonic=deadline_monotonic,
        )
        self._held_keys.difference_update(held)

    def _type(
        self,
        text: str,
        *,
        generation: int | None = None,
        keymap: Mapping[str, int],
        deadline_monotonic: float | None = None,
    ) -> None:
        # ``xdotool type`` emits one opaque command with no way to correlate
        # each character's raw/delivered press and release.  Emit the bounded
        # allowlisted text as one XTEST key action per character so the
        # guardian can require the exact multi-character stream and modifier
        # state while retaining every currently owned key for cancellation.
        for char in text:
            mapped = self._keysym_for_char(char)
            if mapped is None:
                raise DriverError(f"character {char!r} has no allowlisted XTEST keysym")
            components = tuple(part for part in mapped.split("+") if part)
            transitions = tuple(
                [("press", component) for component in components]
                + [("release", component) for component in reversed(components)]
            )
            argv, held = self._keyboard_transition_argv(transitions, keymap)
            self._run(
                argv,
                generation=generation,
                held_keys=set(held),
                input_schema="keyboard-steps",
                expected_argv=tuple(argv),
                deadline_monotonic=deadline_monotonic,
            )

    def pointer_position(
        self, *, generation: int | None = None, deadline_monotonic: float | None = None
    ) -> tuple[int, int, int]:
        """Read one allowlisted native pointer position and target window."""

        result = self._run(
            ["xdotool", "getmouselocation", "--shell"],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        )
        values: dict[str, int] = {}
        allowed = {"X", "Y", "SCREEN", "WINDOW"}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            key, separator, value = line.partition("=")
            valid_value = (
                bool(re.fullmatch(r"-?[0-9]+", value))
                if key != "WINDOW"
                else bool(re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value))
            )
            if not separator or key not in allowed or key in values or not valid_value:
                raise DriverError("native pointer position output is malformed")
            values[key] = int(value, 0) if key == "WINDOW" else int(value)
        if (
            not {"X", "Y", "SCREEN", "WINDOW"}.issubset(values)
            or values["SCREEN"] != 0
            or values["WINDOW"] <= 0
        ):
            raise DriverError("native pointer position output is incomplete")
        return values["X"], values["Y"], values["WINDOW"]

    def is_descendant(self, xid: int, ancestor: int) -> bool:
        """Prove an XI2 target is in the verified client's current subtree."""

        if xid == ancestor:
            return True
        tree = self._run(["xwininfo", "-id", hex(ancestor), "-tree"]).stdout
        root, parents, _geometries = _parse_xwininfo_tree(tree)
        if root != ancestor:
            return False
        current = xid
        seen: set[int] = set()
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
            if current == ancestor:
                return True
        return False

    def _is_owned_client_target(self, xid: int, identity: WindowIdentity) -> bool:
        """Use the sealed native target set for X11 reports."""

        # A certificate without an exact event-target set is incomplete.  A
        # generic ancestry fallback would admit the root, WM helpers, or a
        # foreign sibling after a reparenting transition.
        return bool(identity.target_event_xids) and xid in identity.target_event_xids

    def action(self, name: str, *, generation: int | None = None, **kwargs: Any) -> dict[str, Any] | None:
        if generation is None:
            raise DriverError("input action requires a current game generation")
        execution: dict[str, Any] | None = None
        if DesktopDriver._is_pointer_action(name):
            self.last_pointer_execution = None
        anchor_record = self._measured_anchors.get(name)
        admitted_anchor = kwargs.pop("_admitted_anchor", None)
        pointer_plan = kwargs.pop("_pointer_plan", None)
        click_plan = kwargs.pop("_click_plan", None)
        gesture_plan = kwargs.pop("_gesture_plan", None)
        rate_reservation = kwargs.pop("_rate_reservation", None)
        click_rate_permit = kwargs.pop("_click_rate_permit", None)
        guardian_admission_id = kwargs.pop("_guardian_admission_id", None)
        action_generation = kwargs.pop("_action_generation", generation)
        if isinstance(action_generation, bool) or not isinstance(action_generation, int):
            raise DriverError("native pointer action generation is malformed")
        refresh_anchor = kwargs.pop("_refresh_anchor", None)
        final_admission = kwargs.pop("_final_admission", None)
        deadline_monotonic = kwargs.get("deadline_monotonic", getattr(self, "_active_action_deadline", None))
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(float(deadline_monotonic))
            ):
                raise DriverError("native action deadline is malformed")
            deadline_monotonic = float(deadline_monotonic)
            if time.monotonic() >= deadline_monotonic:
                raise DriverError("native action deadline expired before admission")
        if name == "executor_click" and not callable(final_admission):
            raise DriverError("executor click requires a final admission callback")
        keyboard_keymap = kwargs.pop("_keyboard_keymap", None)
        if DesktopDriver._is_pointer_action(name):
            if (
                not isinstance(gesture_plan, HumanPointerPlan)
                or pointer_plan is None
                or click_plan is None
                or gesture_plan.trajectory is not pointer_plan
                or gesture_plan.click is not click_plan
            ):
                raise DriverError("native pointer action requires a validated planned gesture")
            if rate_reservation is None and click_rate_permit is None:
                raise DriverError("native pointer action has no broker rate reservation")
            if rate_reservation is not None and click_rate_permit is not None:
                raise DriverError("native pointer action received both broker reservation and legacy permit")
            if rate_reservation is not None and not isinstance(rate_reservation, RateReservation):
                raise DriverError("native pointer action has a malformed broker rate reservation")
            if rate_reservation is None:
                if not isinstance(click_rate_permit, RatePermit):
                    raise DriverError("native pointer action has a malformed legacy rate permit")
                try:
                    rate_reservation = self.input_broker.adopt_permit_for_tests(click_rate_permit)
                except InputBrokerError as exc:
                    raise DriverError(str(exc)) from exc
            if anchor_record is None:
                raise DriverError(f"action anchor for {name} has not passed live calibration")
            if (
                anchor_record.evidence_hash in getattr(self, "_consumed_anchor_seals", set())
                and admitted_anchor is not anchor_record
            ):
                raise DriverError(f"action anchor for {name} has already been consumed")
            if (
                generation is not None
                and anchor_record.generation is not None
                and anchor_record.generation != generation
            ):
                raise DriverError(f"action anchor for {name} belongs to a stale generation")
        identity = self.resolve_identity(require_foreground=False, deadline_monotonic=deadline_monotonic)
        if DesktopDriver._is_pointer_action(name):
            if anchor_record is None or anchor_record.identity_fingerprint != identity.fingerprint:
                raise DriverError(f"action anchor for {name} has not passed live calibration")
            facts = dict(anchor_record.facts)
            if (
                facts.get("control") != DesktopDriver.ANCHOR_CONTROLS[name]
                or facts.get("capture") != anchor_record.capture_id
                or facts.get("target") != str(anchor_record.target_xid)
            ):
                raise DriverError(f"action anchor for {name} is stale or unbound")
            anchor = (anchor_record.x, anchor_record.y)
        else:
            anchor = None
        self._focus(identity, generation=generation, deadline_monotonic=deadline_monotonic)
        if DesktopDriver._is_pointer_action(name):
            assert anchor is not None
            if pointer_plan is not None or click_plan is not None:
                if pointer_plan is not None and not isinstance(pointer_plan, PointerTrajectory):
                    raise DriverError("pointer trajectory plan is malformed")
                if not isinstance(click_plan, ClickPlan):
                    raise DriverError("pointer click plan is malformed")
                binding = click_plan.binding
                if pointer_plan is not None and (
                    pointer_plan.binding != binding
                    or pointer_plan.control != click_plan.control
                    or pointer_plan.endpoint != click_plan.point
                ):
                    raise DriverError("pointer trajectory does not cross-match the click plan")
                if (
                    isinstance(gesture_plan.seed, bool)
                    or not isinstance(gesture_plan.seed, int)
                    or not 0 <= gesture_plan.seed < 2**64
                ):
                    raise DriverError("pointer gesture seed is malformed")
                if binding.generation != generation or binding.identity_fingerprint != identity.fingerprint:
                    raise DriverError("pointer plan binding is stale")
                button = {1: PointerButton.LEFT, 3: PointerButton.RIGHT}.get(
                    DesktopDriver.POINTER_BUTTONS.get(name, 0)
                )
                if button is None or click_plan.button is not button:
                    raise DriverError("pointer plan semantic button is not bound to the named action")
                if (
                    anchor_record is None
                    or admitted_anchor is not anchor_record
                    or binding.capture_id != anchor_record.capture_id
                    or binding.capture_sequence != anchor_record.capture_sequence
                    or binding.target_xid != anchor_record.target_xid
                    or binding.target_ancestry != anchor_record.target_ancestry
                ):
                    raise DriverError("pointer plan anchor binding is stale")
                path_digest = hashlib.sha256(
                    repr(
                        tuple(
                            (sample.at, sample.point.x, sample.point.y)
                            for sample in gesture_plan.trajectory.samples
                        )
                    ).encode("ascii")
                ).hexdigest()
                execution = {
                    "action": name,
                    "generation": generation,
                    "identity": identity.fingerprint,
                    "admittedAnchorEvidenceHash": anchor_record.evidence_hash,
                    "admittedAnchorCaptureId": anchor_record.capture_id,
                    "admittedAnchorCaptureSequence": anchor_record.capture_sequence,
                    "anchorEvidenceHash": anchor_record.evidence_hash,
                    "anchorCaptureId": anchor_record.capture_id,
                    "anchorCaptureSequence": anchor_record.capture_sequence,
                    "seed": gesture_plan.seed,
                    "frozenEndpoint": (int(gesture_plan.endpoint.x), int(gesture_plan.endpoint.y)),
                    "endpoint": (int(gesture_plan.endpoint.x), int(gesture_plan.endpoint.y)),
                    "movementDuration": gesture_plan.movement_duration,
                    "dwell": gesture_plan.dwell,
                    "submovementCount": len(gesture_plan.waypoints),
                    "pathSha256": path_digest,
                    "rateGate": "canonical-user-session",
                    "phase": "planned",
                }
                # Preserve the legacy diagnostic snapshot, while returning
                # the executor receipt from this invocation's local object.
                self.last_pointer_execution = execution
                left, top, width, height = identity.child_geometry
                expected_start = click_plan.point if pointer_plan is None else pointer_plan.samples[0].point
                actual_x, actual_y, actual_window = self.pointer_position(
                    generation=generation, deadline_monotonic=deadline_monotonic
                )
                if not self._is_owned_client_target(actual_window, identity):
                    raise DriverError("native pointer is outside the verified client")
                if (actual_x, actual_y) != (left + int(expected_start.x), top + int(expected_start.y)):
                    raise DriverError("native pointer start changed before the admitted gesture")
                if pointer_plan is not None:
                    chain = build_pointer_motion_chain(pointer_plan, identity.child_geometry)
                    execution["movementDispatchStartedMonotonicNs"] = time.monotonic_ns()
                    execution["phase"] = "movement_dispatched"
                    self._run(
                        chain,
                        generation=generation,
                        input_schema="pointer-motion",
                        expected_argv=tuple(chain),
                        deadline_monotonic=deadline_monotonic,
                    )
                    execution["movementAckMonotonicNs"] = time.monotonic_ns()
                    execution["phase"] = "movement_acknowledged"
                # Recheck foreground identity and consume a fresh anchor after
                # motion, immediately before the pointer/window commit query.
                # Profile reads remain outside this protected click boundary.
                current = self.resolve_identity(
                    require_foreground=True, deadline_monotonic=deadline_monotonic
                )
                if (
                    current.fingerprint != binding.identity_fingerprint
                    or current.child_xid != identity.child_xid
                ):
                    raise DriverError("pointer identity changed before click commitment")
                if pointer_plan is not None:
                    if not callable(refresh_anchor):
                        raise DriverError("pointer motion has no fresh anchor re-observation")
                    refreshed_anchor = refresh_anchor(current, anchor_record)
                    if not isinstance(refreshed_anchor, MeasuredAnchor):
                        raise DriverError("fresh pointer anchor provider returned unbound evidence")
                    if (
                        refreshed_anchor.action != name
                        or refreshed_anchor.identity_fingerprint != binding.identity_fingerprint
                        or refreshed_anchor.generation != binding.generation
                        or refreshed_anchor.target_xid != anchor_record.target_xid
                        or refreshed_anchor.target_ancestry != anchor_record.target_ancestry
                        or dict(refreshed_anchor.facts).get("control") != click_plan.control
                        or refreshed_anchor.capture_sequence <= anchor_record.capture_sequence
                        or refreshed_anchor.capture_monotonic <= anchor_record.capture_monotonic
                        or refreshed_anchor.evidence_hash == anchor_record.evidence_hash
                        or not DesktopDriver._fresh_anchor_contains_endpoint(
                            refreshed_anchor,
                            click_plan.point,
                            identity.child_geometry,
                            anchor_record.bounds,
                        )
                    ):
                        raise DriverError("fresh pointer anchor changed control, bounds, or identity")
                    # The visual proof consumed for dispatch is the refreshed
                    # observation, not the pre-motion anchor.  Retain both:
                    # the admitted anchor binds the plan, while the receipt
                    # binds the actual physical press to the fresh witness.
                    execution["anchorEvidenceHash"] = refreshed_anchor.evidence_hash
                    execution["anchorCaptureId"] = refreshed_anchor.capture_id
                    execution["anchorCaptureSequence"] = refreshed_anchor.capture_sequence
                current_anchor = self._measured_anchors.get(name)
                if (
                    current_anchor is not anchor_record
                    or current_anchor.evidence_hash not in self._consumed_anchor_seals
                ):
                    raise DriverError("pointer anchor changed before click commitment")
                pre_click_x, pre_click_y, pre_click_window = self.pointer_position(
                    generation=generation, deadline_monotonic=deadline_monotonic
                )
                if not self._is_owned_client_target(pre_click_window, current):
                    raise DriverError("native pointer left the verified client before click")
                expected_point = (
                    current.child_geometry[0] + int(click_plan.point.x),
                    current.child_geometry[1] + int(click_plan.point.y),
                )
                if (pre_click_x, pre_click_y) != expected_point:
                    raise DriverError("native pointer endpoint changed before click commitment")
                click_button = click_plan.button
                native_button = {PointerButton.LEFT: 1, PointerButton.RIGHT: 3}[click_button]
                click_argv = [
                    "xdotool",
                    "mousedown",
                    str(native_button),
                    "sleep",
                    f"{click_plan.dwell:.6f}",
                    "mouseup",
                    str(native_button),
                ]
                if final_admission is not None:
                    if not callable(final_admission):
                        raise DriverError("native pointer final admission is not callable")
                    # The daemon's one-use semantic admission is the final
                    # boundary before rate precommit and the physical press.
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        raise DriverError("native pointer deadline expired before final admission")
                    final_admission()
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise DriverError("native pointer deadline expired before click rate precommit")
                if not isinstance(rate_reservation, RateReservation):
                    raise DriverError("native pointer action lost its broker rate reservation")
                emitted_chain = tuple(click_argv)
                admission_id = (
                    guardian_admission_id
                    if isinstance(guardian_admission_id, str) and guardian_admission_id
                    else f"{name}:{action_generation}:{generation}:{anchor_record.evidence_hash}"
                )
                try:
                    admitted_gesture = self.input_broker.mint_admitted_gesture(
                        action_generation=action_generation,
                        lease_generation=generation,
                        button=click_button,
                        emitted_chain=emitted_chain,
                        destination_anchor=anchor_record.evidence_hash,
                        target_identity=identity.fingerprint,
                        guardian_admission_id=admission_id,
                        semantic_authorization=name,
                        deadline_monotonic=deadline_monotonic,
                        reservation=rate_reservation,
                    )
                except InputBrokerError as exc:
                    raise DriverError(str(exc)) from exc

                def emit_click(claim: ButtonEmissionClaim) -> None:
                    execution["clickDispatchStartedMonotonicNs"] = time.monotonic_ns()
                    execution["phase"] = "click_dispatched"
                    self._run(
                        click_argv,
                        generation=generation,
                        held_buttons={native_button},
                        input_schema="single-click",
                        expected_argv=tuple(click_argv),
                        deadline_monotonic=deadline_monotonic,
                        button_emission_claim=claim,
                        emission_action_generation=action_generation,
                        emission_lease_generation=generation,
                    )
                    execution["pressReleaseMonotonicNs"] = time.monotonic_ns()
                    execution["clickAckMonotonicNs"] = time.monotonic_ns()

                try:
                    recorded_press = self.input_broker.emit_button_gesture(
                        admitted_gesture,
                        emitted_chain=emitted_chain,
                        action_generation=action_generation,
                        lease_generation=generation,
                        destination_anchor=anchor_record.evidence_hash,
                        target_identity=identity.fingerprint,
                        guardian_admission_id=admission_id,
                        semantic_authorization=name,
                        emit=emit_click,
                    )
                except InputBrokerError as exc:
                    raise DriverError(str(exc)) from exc
                execution["clickPrecommitMonotonicNs"] = admitted_gesture.precommit_monotonic_ns
                execution["rateRecordMonotonicNs"] = recorded_press
                execution["phase"] = "click_released_and_rate_recorded"
                if click_plan is not None:
                    final_x, final_y, final_window = self.pointer_position(
                        generation=generation, deadline_monotonic=deadline_monotonic
                    )
                    if not self._is_owned_client_target(final_window, current):
                        raise DriverError("native pointer postcondition left the verified client")
                    left, top, _width, _height = current.child_geometry
                    expected_point = (
                        left + int(click_plan.point.x),
                        top + int(click_plan.point.y),
                    )
                    if (final_x, final_y) != expected_point:
                        raise DriverError("native pointer postcondition endpoint changed")
            else:
                raise DriverError("native pointer action cannot use the direct-click fallback")
        elif name == "focus_game":
            return
        elif name == "open_chat":
            self._key(
                "Return",
                generation=generation,
                keymap=keyboard_keymap,
                deadline_monotonic=deadline_monotonic,
            )
        elif name == "submit_command":
            self._key(
                "Return",
                generation=generation,
                keymap=keyboard_keymap,
                deadline_monotonic=deadline_monotonic,
            )
        elif name == "escape_command_buffer":
            self._key(
                "Escape",
                generation=generation,
                keymap=keyboard_keymap,
                deadline_monotonic=deadline_monotonic,
            )
        elif name == "open_ah":
            argv, held = self._keyboard_transition_argv(
                (
                    ("press", "Control_L"),
                    ("press", "F12"),
                    ("release", "F12"),
                    ("release", "Control_L"),
                ),
                keyboard_keymap,
            )
            self._run(
                argv,
                generation=generation,
                held_keys=set(held),
                input_schema="keyboard-steps",
                expected_argv=tuple(argv),
                deadline_monotonic=deadline_monotonic,
            )
        elif name == "type_allowlisted_command":
            command = kwargs.get("command")
            if command not in DesktopDriver.COMMANDS:
                raise DriverError("attempted to type a non-allowlisted command")
            self._type(
                command,
                generation=generation,
                keymap=keyboard_keymap,
                deadline_monotonic=deadline_monotonic,
            )
        else:
            raise DriverError(f"unsupported X11 action {name}")
        if name == "executor_click":
            return copy.deepcopy(execution)
        return None

    def _capture_epoch_snapshot(self) -> int:
        with self._process_lock:
            return self._capture_revocation_epoch

    def _check_capture_epoch_locked(self, epoch: int) -> None:
        if epoch != self._capture_revocation_epoch or self._cancel_active.is_set():
            raise DriverError("desktop capture was revoked")

    def _check_capture_epoch(self, epoch: int) -> None:
        with self._process_lock:
            self._check_capture_epoch_locked(epoch)

    def capture(
        self,
        path: Path,
        *,
        identity: WindowIdentity | None = None,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        capture_epoch = self._capture_epoch_snapshot()
        deadline = _capture_deadline(deadline_monotonic)
        self._check_capture_epoch(capture_epoch)
        before = self.resolve_identity(deadline_monotonic=deadline)
        self._check_capture_epoch(capture_epoch)
        if identity is not None and before.fingerprint != identity.fingerprint:
            raise DriverError("capture identity changed before screenshot")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        attempt_dir: Path | None = None
        try:
            with _capture_lock(deadline, check=lambda: self._check_capture_epoch(capture_epoch)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("capture deadline expired before screenshot")
                attempt_dir = Path(tempfile.mkdtemp(prefix=f".{path.name}.capture-", dir=path.parent))
                stage = attempt_dir / "capture.png"
                self._check_capture_epoch(capture_epoch)
                self._run(
                    ["gnome-screenshot", "-f", str(stage)],
                    timeout=min(_DEFAULT_CAPTURE_TIMEOUT_SECONDS, remaining),
                    deadline_monotonic=deadline,
                )
                _wait_for_complete_png(
                    stage,
                    (self.profile.client_width, self.profile.client_height),
                    deadline,
                    check=lambda: self._check_capture_epoch(capture_epoch),
                )
                self._check_capture_epoch(capture_epoch)
                after = self.resolve_identity(deadline_monotonic=deadline)
                self._check_capture_epoch(capture_epoch)
                if after.fingerprint != before.fingerprint:
                    raise DriverError("capture identity changed during screenshot")
                if time.monotonic() >= deadline:
                    raise DriverError("capture deadline expired before screenshot publication")
                with self._process_lock:
                    self._check_capture_epoch_locked(capture_epoch)
                    if time.monotonic() >= deadline:
                        raise DriverError("capture deadline expired before screenshot publication")
                    os.chmod(stage, 0o600)
                    os.replace(stage, path)
                    self.last_identity = after
                    return after
        finally:
            if attempt_dir is not None:
                retired_dir = attempt_dir.with_name(f".{attempt_dir.name}.retired-{uuid.uuid4().hex}")
                try:
                    os.replace(attempt_dir, retired_dir)
                except FileNotFoundError:
                    pass
                except OSError:
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                else:
                    shutil.rmtree(retired_dir, ignore_errors=True)

    def capture_native_region(
        self,
        path: Path,
        *,
        identity: WindowIdentity,
        rectangle: tuple[int, int, int, int],
        deadline_monotonic: float | None = None,
        observer_admission: Callable[[Callable[[], Any]], Any] | None = None,
        observer_reaped: Callable[[int], None] | None = None,
        _pre_ack_admission: Callable[[int, Callable[[], None]], None] | None = None,
        _receipt_sink: Callable[[TelemetryRegionReceipt], None] | None = None,
        _allow_unobserved_fixture: bool = False,
    ) -> WindowIdentity:
        """Capture one exact profile-derived region through the private producer."""

        self.last_region_attempt = {
            "stage": "not_started",
            "request_started": None,
            "lock_acquired": None,
            "ack_sent": None,
            "capture_started": None,
            "capture_returned": None,
            "attempt_retired": None,
        }
        executable = self.profile.telemetry_region_executable
        executable_sha256 = self.profile.telemetry_region_executable_sha256
        if executable is None or executable_sha256 is None:
            raise DriverError("telemetry region capability is not configured")
        if len(rectangle) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in rectangle
        ):
            raise DriverError("telemetry region rectangle is not four integers")
        x, y, width, height = rectangle
        if width != 256 or height != 256:
            raise DriverError("telemetry region rectangle must be exactly 256x256")
        left, top, client_width, client_height = identity.child_geometry
        crop_left, crop_top, crop_width, crop_height = self.profile.canvas_crop
        expected = (left + crop_left, top + crop_top, crop_width, crop_height)
        if rectangle != expected:
            raise DriverError("telemetry region rectangle is not derived from the fresh profile identity")
        if (
            crop_left < 0
            or crop_top < 0
            or crop_width != 256
            or crop_height != 256
            or crop_left + crop_width > client_width
            or crop_top + crop_height > client_height
            or x < left
            or y < top
            or x + width > left + client_width
            or y + height > top + client_height
        ):
            raise DriverError("telemetry region rectangle is outside the verified client geometry")
        if (observer_admission is None) != (observer_reaped is None):
            raise DriverError("telemetry region observer admission and reap callbacks must be paired")
        if observer_admission is None and _allow_unobserved_fixture is not True:
            raise DriverError("telemetry region capture requires Guardian observer admission")
        if _pre_ack_admission is not None and not callable(_pre_ack_admission):
            raise DriverError("telemetry pre-ACK admission hook is not callable")
        deadline = _capture_deadline(deadline_monotonic)
        capture_epoch = self._capture_epoch_snapshot()
        self._check_capture_epoch(capture_epoch)
        with _identity_latency_barrier("backend_pre"):
            before = self.resolve_identity(deadline_monotonic=deadline)
        if before.fingerprint != identity.fingerprint:
            raise DriverError("telemetry region identity changed before capture")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        attempt_dir: Path | None = None
        image_fd: int | None = None
        parent_sock: socket.socket | None = None
        child_sock: socket.socket | None = None
        process: subprocess.Popen[str] | None = None
        owned_process: subprocess.Popen[str] | None = None
        observer_registered = False
        gate_capability: AdmissionGate | None = None
        gate_read = -1
        gate_write = -1
        owner_record: dict[str, Any] | None = None
        nonce = uuid.uuid4().hex
        output_path_digest = ""
        request_started = time.monotonic()
        _identity_latency_mark("request_started")
        # A validation or admission failure retains the entry timestamp while
        # every later boundary remains explicitly unreached.
        lock_acquired: float | None = None
        ack_sent: float | None = None
        capture_started: float | None = None
        capture_returned: float | None = None
        attempt_retired: bool | None = None
        finalized_receipt: TelemetryRegionReceipt | None = None
        protocol_stage = "capture_lock_wait"

        def update_protocol_stage(stage_name: str) -> None:
            nonlocal protocol_stage
            protocol_stage = stage_name
            self.last_region_attempt = {
                "stage": protocol_stage,
                "request_started": request_started,
                "lock_acquired": lock_acquired,
                "ack_sent": ack_sent,
                "capture_started": capture_started,
                "capture_returned": capture_returned,
                "attempt_retired": attempt_retired,
            }

        update_protocol_stage(protocol_stage)
        try:
            with _capture_lock(deadline, check=lambda: self._check_capture_epoch(capture_epoch)):
                lock_acquired = time.monotonic()
                _identity_latency_instant("lock_acquired")
                update_protocol_stage("attempt_setup")
                self._check_capture_epoch(capture_epoch)
                attempt_dir = Path(tempfile.mkdtemp(prefix=f".{path.name}.telemetry-", dir=path.parent))
                os.chmod(attempt_dir, 0o700)
                stage = attempt_dir / "capture.png"
                output_path_digest = hashlib.sha256(str(stage).encode("utf-8")).hexdigest()
                image_fd, verified_digest = _open_sealed_executable(executable, executable_sha256)
                parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                parent_sock.setblocking(True)
                child_fd = child_sock.fileno()
                gate_read, gate_write = os.pipe()
                target = f"/proc/self/fd/{image_fd}"
                args = [
                    target,
                    "--telemetry-region",
                    "--telemetry-x",
                    str(x),
                    "--telemetry-y",
                    str(y),
                    "--telemetry-width",
                    str(width),
                    "--telemetry-height",
                    str(height),
                    "--telemetry-owner-fd",
                    str(child_fd),
                    "--telemetry-exec-fd",
                    str(image_fd),
                    "--telemetry-nonce",
                    nonce,
                    "--file",
                    str(stage),
                ]
                environment = os.environ.copy()
                environment["DISPLAY"] = self.profile.display
                if not self.xauthority:
                    raise DriverError("Xauthority path must be explicitly configured or inherited")
                environment["XAUTHORITY"] = self.xauthority

                # Hold the same revocation fence across the final epoch check,
                # spawn, process registration, and positive admission gate.
                # A release may therefore either precede this entire sequence
                # or observe the registered child and revoke it before any
                # protocol ACK can be sent.
                class GatedRegionProcess:
                    def __init__(self, child: subprocess.Popen[str], gate: AdmissionGate):
                        self.process = child
                        self._gate = gate

                    def release_admission_gate(self) -> None:
                        self._gate.release()

                def spawn_gated() -> GatedRegionProcess:
                    nonlocal gate_capability, gate_read, gate_write, owned_process, process
                    admission_health = self._admission_health
                    if admission_health is not None:
                        admission_health()
                    with self._process_lock:
                        self._check_capture_epoch_locked(capture_epoch)
                        if time.monotonic() >= deadline:
                            raise DriverError("telemetry region capture deadline expired before admission")
                        child = subprocess.Popen(
                            [
                                sys.executable,
                                "-I",
                                "-S",
                                "-c",
                                _EXEC_ADMISSION_WRAPPER,
                                str(gate_read),
                                target,
                                *args[1:],
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            env=environment,
                            start_new_session=True,
                            pass_fds=(gate_read, child_fd, image_fd),
                        )
                        process = child
                        owned_process = child
                        self._active_processes.add(child)
                        self._active_process_groups.add(child.pid)
                        self._check_capture_epoch_locked(capture_epoch)
                        gate_capability = AdmissionGate(gate_write)
                        admitted = GatedRegionProcess(child, gate_capability)
                        gate_write = -1
                        os.close(gate_read)
                        gate_read = -1
                        return admitted

                update_protocol_stage("observer_admission")
                if observer_admission is None:
                    admitted_process = spawn_gated()
                    admitted_process.release_admission_gate()
                else:
                    admitted_process = observer_admission(spawn_gated)
                    observer_registered = True
                process = getattr(admitted_process, "process", None)
                if not isinstance(process, subprocess.Popen):
                    raise DriverError("observer admission did not return its gated telemetry process")
                update_protocol_stage("owner_ready")
                child_sock.close()
                child_sock = None
                owner_record = _json_record(
                    parent_sock,
                    deadline,
                    check=lambda: self._check_capture_epoch(capture_epoch),
                    process=process,
                )
                required_owner = {
                    "type",
                    "protocol",
                    "nonce",
                    "pid",
                    "process_start",
                    "ppid",
                    "pgid",
                    "exec_fd",
                    "executable_sha256",
                    "primary_owner",
                    "remote",
                    "branch",
                    "rectangle",
                    "output_path_digest",
                }
                if set(owner_record) != required_owner or owner_record.get("type") != "owner_ready":
                    raise DriverError("telemetry owner proof has an unexpected schema")
                live_identity = _proc_identity(process.pid)
                if (
                    not _protocol_int(owner_record["protocol"])
                    or owner_record["protocol"] != 1
                    or not _protocol_string(owner_record["nonce"])
                    or owner_record["nonce"] != nonce
                    or not _protocol_int(owner_record["pid"])
                    or owner_record["pid"] != process.pid
                    or not _protocol_string(owner_record["process_start"])
                    or owner_record["process_start"] != live_identity[0]
                    or not _protocol_int(owner_record["ppid"])
                    or owner_record["ppid"] != os.getpid()
                    or not _protocol_int(owner_record["pgid"])
                    or owner_record["pgid"] != process.pid
                    or not _protocol_int(owner_record["exec_fd"])
                    or owner_record["exec_fd"] != image_fd
                    or not _protocol_string(owner_record["executable_sha256"])
                    or owner_record["executable_sha256"] != verified_digest
                    or owner_record["primary_owner"] is not True
                    or owner_record["remote"] is not False
                    or owner_record["branch"] != "ScreenshotArea"
                    or _protocol_rectangle(owner_record["rectangle"]) != rectangle
                    or not _protocol_string(owner_record["output_path_digest"])
                    or owner_record["output_path_digest"] != output_path_digest
                ):
                    raise DriverError("telemetry owner proof does not match the admitted process")
                _verify_admitted_process(process, image_fd, verified_digest, os.getpid())
                ack = json.dumps({"type": "ACK", "protocol": 1, "nonce": nonce}, separators=(",", ":"))
                ack_dispatched = False

                def dispatch_ack_once() -> None:
                    nonlocal ack_sent, ack_dispatched
                    with self._process_lock:
                        if ack_dispatched:
                            raise DriverError("telemetry producer ACK was dispatched twice")
                        self._check_capture_epoch_locked(capture_epoch)
                        if process is None or process.poll() is not None or time.monotonic() >= deadline:
                            raise DriverError("telemetry capture was revoked before owner ACK")
                        _verify_admitted_process(process, image_fd, verified_digest, os.getpid())
                        ack_sent = time.monotonic()
                        update_protocol_stage("capture_started")
                        # Burn the one-shot before touching the socket.  A
                        # partial send or a caller that catches the write
                        # error must never be allowed to retry an ACK for the
                        # same admitted process/nonce.
                        ack_dispatched = True
                        parent_sock.sendall((ack + "\n").encode())

                if _pre_ack_admission is None:
                    dispatch_ack_once()
                else:
                    _pre_ack_admission(process.pid, dispatch_ack_once)
                    if not ack_dispatched:
                        raise DriverError("telemetry pre-ACK admission did not dispatch the producer ACK")
                started = _json_record(
                    parent_sock,
                    deadline,
                    check=lambda: self._check_capture_epoch(capture_epoch),
                    process=process,
                )
                if set(started) != {
                    "type",
                    "protocol",
                    "nonce",
                    "branch",
                    "rectangle",
                    "output_path_digest",
                    "capture_started",
                } or (
                    started.get("type") != "capture_started"
                    or not _protocol_int(started.get("protocol"))
                    or started.get("protocol") != 1
                    or not _protocol_string(started.get("nonce"))
                    or started.get("nonce") != nonce
                    or not _protocol_string(started.get("branch"))
                    or started.get("branch") != "ScreenshotArea"
                    or _protocol_rectangle(started.get("rectangle")) != rectangle
                    or not _protocol_string(started.get("output_path_digest"))
                    or started.get("output_path_digest") != output_path_digest
                    or not _protocol_float(started.get("capture_started"))
                    or float(started["capture_started"]) < ack_sent
                    or float(started["capture_started"]) > time.monotonic()
                ):
                    raise DriverError("telemetry capture-start proof is malformed")
                capture_started = float(started["capture_started"])
                _identity_latency_instant("capture_started")
                update_protocol_stage("capture_complete")
                _wait_for_complete_png(
                    stage,
                    (256, 256),
                    deadline,
                    check=lambda: self._check_capture_epoch(capture_epoch),
                    process=process,
                    protocol_socket=parent_sock,
                )
                completed = _json_record(
                    parent_sock,
                    deadline,
                    check=lambda: self._check_capture_epoch(capture_epoch),
                    process=process,
                )
                if set(completed) != {
                    "type",
                    "protocol",
                    "nonce",
                    "branch",
                    "rectangle",
                    "output_path_digest",
                    "output_sha256",
                    "output_bytes",
                } or (
                    completed.get("type") != "capture_complete"
                    or not _protocol_int(completed.get("protocol"))
                    or completed.get("protocol") != 1
                    or not _protocol_string(completed.get("nonce"))
                    or completed.get("nonce") != nonce
                    or not _protocol_string(completed.get("branch"))
                    or completed.get("branch") != "ScreenshotArea"
                    or _protocol_rectangle(completed.get("rectangle")) != rectangle
                    or not _protocol_string(completed.get("output_path_digest"))
                    or completed.get("output_path_digest") != output_path_digest
                    or not _protocol_string(completed.get("output_sha256"))
                    or not _protocol_int(completed.get("output_bytes"))
                    or completed.get("output_bytes", 0) < 1
                ):
                    raise DriverError("telemetry capture-complete proof is malformed")
                output_bytes = stage.read_bytes()
                output_digest = hashlib.sha256(output_bytes).hexdigest()
                if completed.get("output_sha256") != output_digest or completed.get("output_bytes") != len(
                    output_bytes
                ):
                    raise DriverError("telemetry output digest proof does not match the complete PNG")
                _validate_exact_png(stage, (256, 256))
                self._check_capture_epoch(capture_epoch)
                try:
                    return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise DriverError("telemetry producer did not exit after capture completion") from exc
                if return_code != 0:
                    raise _producer_failure(process, "telemetry producer failed after capture completion")
                capture_returned = time.monotonic()
                _identity_latency_instant("capture_returned")
                _identity_latency_mark("capture_returned")
                update_protocol_stage("identity_postcheck")
                with _identity_latency_barrier("backend_post"):
                    after = self.resolve_identity(deadline_monotonic=deadline)
                _identity_latency_instant("identity_after_backend")
                self._check_capture_epoch(capture_epoch)
                if after.fingerprint != before.fingerprint:
                    raise DriverError("telemetry region identity changed during capture")
                with self._process_lock:
                    self._check_capture_epoch_locked(capture_epoch)
                    if time.monotonic() >= deadline:
                        raise DriverError("telemetry region capture deadline expired")
                    os.chmod(stage, 0o600)
                    os.replace(stage, path)
                    self.last_identity = after
                    finalized_receipt = TelemetryRegionReceipt(
                        nonce=nonce,
                        rectangle=rectangle,
                        executable_sha256=verified_digest,
                        owner_pid=int(owner_record["pid"]),
                        owner_process_start=str(owner_record["process_start"]),
                        owner_ppid=int(owner_record["ppid"]),
                        owner_pgid=int(owner_record["pgid"]),
                        owner_exec_fd=int(owner_record["exec_fd"]),
                        owner_branch="ScreenshotArea",
                        request_started=request_started,
                        lock_acquired=lock_acquired,
                        ack_sent=ack_sent,
                        capture_started=capture_started,
                        capture_returned=capture_returned,
                        output_path_digest=output_path_digest,
                        output_sha256=output_digest,
                        output_bytes=len(output_bytes),
                        capture_epoch=capture_epoch,
                        attempt_retired=False,
                    )
                    self.last_region_receipt = finalized_receipt
                    update_protocol_stage("published")
                    return after
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise DriverError(f"telemetry region process failed: {exc}") from exc
        finally:
            if gate_capability is not None:
                gate_capability.close()
            if gate_write >= 0:
                try:
                    os.close(gate_write)
                except OSError:
                    pass
            if gate_read >= 0:
                try:
                    os.close(gate_read)
                except OSError:
                    pass
            if child_sock is not None:
                child_sock.close()
            if parent_sock is not None:
                parent_sock.close()
            if process is not None:
                if process.poll() is None:
                    if not self._terminate_process(process):
                        self._reap_process_later(
                            process,
                            capture_epoch,
                            on_reaped=observer_reaped if observer_registered else None,
                        )
                        process = None
                if process is not None:
                    try:
                        process.communicate(timeout=0.5)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    self._forget_process(process)
                    if observer_registered and observer_reaped is not None and process.poll() is not None:
                        try:
                            observer_reaped(process.pid)
                        except Exception:
                            pass
            if image_fd is not None:
                try:
                    os.close(image_fd)
                except OSError:
                    pass
            directory_retired = attempt_dir is None
            if attempt_dir is not None:
                retired_dir = attempt_dir.with_name(f".{attempt_dir.name}.retired-{uuid.uuid4().hex}")
                try:
                    os.replace(attempt_dir, retired_dir)
                except FileNotFoundError:
                    pass
                except OSError:
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                else:
                    shutil.rmtree(retired_dir, ignore_errors=True)
                directory_retired = not attempt_dir.exists() and not retired_dir.exists()
            if owned_process is None:
                process_retired = True
            else:
                with self._process_lock:
                    process_retired = (
                        owned_process.poll() is not None
                        and owned_process not in self._active_processes
                        and owned_process.pid not in self._active_process_groups
                    )
            attempt_retired = directory_retired and process_retired
            if finalized_receipt is not None:
                finalized_receipt = replace(finalized_receipt, attempt_retired=attempt_retired)
                if self.last_region_receipt is not None and self.last_region_receipt.nonce == nonce:
                    self.last_region_receipt = finalized_receipt
            _identity_latency_instant("cleanup")
            # This snapshot is also the durable failure evidence.  It is
            # written after process reap and attempt retirement, so a late
            # failure cannot erase an earlier boundary or claim provisional
            # cleanup as final.
            self.last_region_attempt = {
                "stage": protocol_stage,
                "request_started": request_started,
                "lock_acquired": lock_acquired,
                "ack_sent": ack_sent,
                "capture_started": capture_started,
                "capture_returned": capture_returned,
                "attempt_retired": attempt_retired,
            }
            if _receipt_sink is not None and finalized_receipt is not None:
                _receipt_sink(finalized_receipt)

    def calibration_focus(
        self,
        window_id: str,
        *,
        generation: int | None = None,
        calibration_ticket: CalibrationTicket | None = None,
    ) -> None:
        """Harmless delayed action used only by the owned GTK target test."""

        if calibration_ticket is None or generation is not None:
            raise DriverError("calibration focus requires an exact calibration ticket")
        props = self._run(["xprop", "-id", window_id, "WM_NAME", "_NET_WM_PID"]).stdout
        if calibration_ticket.title not in props or not re.search(
            rf"_NET_WM_PID\([^)]*\).*\b{calibration_ticket.target_pid}\b", props
        ):
            raise DriverError("owned GTK calibration target identity changed")
        self._run(
            ["xdotool", "windowactivate", "--sync", window_id],
            calibration_ticket=calibration_ticket,
        )
        focused = int(
            self._run(["xdotool", "getwindowfocus"]).stdout.strip(),
            0,
        )
        if focused != int(window_id, 0):
            raise DriverError("owned calibration target did not retain focus")
        self._calibration_focus_ticket = calibration_ticket

    def xtest_input_profile(
        self,
        hierarchy: tuple[tuple[int, int, str, str], ...],
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> Any:
        """Read and seal the exact attached XTEST pointer profile."""

        from .calibration import _parse_xinput_profile, _parse_xwininfo_root_stats, xtest_sources

        _keyboard_source, source = xtest_sources(hierarchy)
        parent = next((row[1] for row in hierarchy if row[0] == source), 0)
        if parent <= 0:
            raise DriverError("XTEST pointer source has no measured parent")
        long_text = self._run(
            ["xinput", "list", "--long", str(source)],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        ).stdout
        properties_text = self._run(
            ["xinput", "list-props", str(source)],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        ).stdout
        root_text = self._run(
            ["xwininfo", "-root", "-stats"],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        ).stdout
        try:
            root_xid, root_geometry = _parse_xwininfo_root_stats(root_text)
            return _parse_xinput_profile(
                long_text,
                properties_text,
                hierarchy,
                source_device=source,
                parent_device=parent,
                root_xid=root_xid,
                root_geometry=root_geometry,
            )
        except Exception as exc:
            raise DriverError(f"XTEST input profile is not admissible: {exc}") from exc


class XI2Observer:
    """Read-only XI2 observer using the same strict parser as runtime."""

    def __init__(self, display: str = ":0", xauthority: str = ""):
        self.display = display
        self.xauthority = xauthority
        self.process: subprocess.Popen[str] | None = None
        self._admission_gate_write = -1
        self._admission_gate: AdmissionGate | None = None
        self._expected_executable = ""
        self._expected_process_start = ""
        self._exec_verified = False

    @staticmethod
    def _process_start(pid: int) -> str:
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing = text.rfind(")")
            fields = text[closing + 2 :].split()
            if closing < 0 or len(fields) <= 19:
                raise ValueError("malformed process stat")
            return fields[19]
        except (OSError, UnicodeError, ValueError) as exc:
            raise DriverError("observer process start identity is unavailable") from exc

    def start(self) -> None:
        if self.process is not None:
            raise DriverError("XI2 observer is already running")
        environment = os.environ.copy()
        environment["DISPLAY"] = self.display
        if not self.xauthority:
            raise DriverError("Xauthority path must be explicitly configured")
        environment["XAUTHORITY"] = self.xauthority
        executable = _resolve_wrapped_executable("xinput")
        gate_read, gate_write = os.pipe()
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _EXEC_ADMISSION_WRAPPER,
                    str(gate_read),
                    os.path.realpath(executable),
                    "test-xi2",
                    "--root",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
                bufsize=1,
                pass_fds=(gate_read,),
            )
            self._expected_executable = executable
            self._expected_process_start = self._process_start(self.process.pid)
            self._exec_verified = False
            os.close(gate_read)
            gate_read = -1
            self._admission_gate_write = gate_write
            self._admission_gate = AdmissionGate(gate_write)
        except (OSError, DriverError, ValueError) as exc:
            try:
                os.close(gate_read)
            except OSError:
                pass
            try:
                os.close(gate_write)
            except OSError:
                pass
            process = self.process
            reaped = process is None
            if process is not None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.SubprocessError, TypeError):
                    pass
                reaped = getattr(process, "poll", lambda: 0)() is not None
            self.process = None if reaped else process
            self._admission_gate_write = -1
            self._admission_gate = None
            self._expected_executable = ""
            self._expected_process_start = ""
            raise DriverError(f"cannot start XI2 observer: {exc}") from exc

    def release_admission_gate(self) -> None:
        gate = self._admission_gate_write
        self._admission_gate_write = -1
        admission_gate = self._admission_gate
        self._admission_gate = None
        if gate < 0 or admission_gate is None:
            return
        try:
            admission_gate.release()
        except OSError as exc:
            raise DriverError("XI2 observer admission gate failed") from exc

    def abort_admission_gate(self) -> None:
        gate = self._admission_gate
        self._admission_gate = None
        self._admission_gate_write = -1
        if gate is not None:
            gate.close()

    def verify_exec_identity(self) -> None:
        process = self.process
        if process is None or not self._expected_executable or not self._expected_process_start:
            raise DriverError("XI2 observer has no admitted executable identity")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise DriverError("XI2 observer exited before the resolved xinput executable ran")
            try:
                executable = os.path.realpath(os.readlink(f"/proc/{process.pid}/exe"))
                start = self._process_start(process.pid)
                pgid = os.getpgid(process.pid)
            except (OSError, DriverError) as exc:
                raise DriverError("XI2 observer executable identity could not be rechecked") from exc
            if executable == self._expected_executable and start == self._expected_process_start:
                if pgid != process.pid:
                    raise DriverError("XI2 observer process group is not its admitted PID")
                self._exec_verified = True
                return
            time.sleep(0.01)
        raise DriverError("XI2 observer wrapper did not exec the resolved xinput executable")

    def stop(self) -> str:
        if self.process is None:
            raise DriverError("XI2 observer is not running")
        process = self.process
        # Stop is an abort boundary. An observer that was started but never
        # registered must never be admitted merely because it is being closed.
        self.abort_admission_gate()
        process.terminate()
        wait = getattr(process, "wait", None)
        if not callable(wait):
            self.process = None
            return ""
        try:
            wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                raise DriverError("XI2 observer termination could not be confirmed") from exc
        if getattr(process, "poll", lambda: 0)() is None:
            raise DriverError("XI2 observer termination could not be confirmed")
        self.process = None
        self._exec_verified = False
        self._expected_executable = ""
        self._expected_process_start = ""
        # The monitor's reader thread is the sole owner of stdout.  Returning
        # communicate() output here would race that reader and lose or
        # duplicate the final complete XI2 block.
        return ""

    @staticmethod
    def records(output: str) -> list[XI2Event]:
        try:
            return parse_xi2_records(output, require_event=True)
        except InputEventError as exc:
            raise DriverError(str(exc)) from exc

    @staticmethod
    def sources(output: str) -> set[int]:
        return source_ids(XI2Observer.records(output))

    @staticmethod
    def events(output: str) -> list[tuple[int, str, str]]:
        return [
            (event.source_device, event.event_class.value, hex(event.target_xid or 0))
            for event in XI2Observer.records(output)
        ]


class InputMonitor:
    """Incremental runtime event monitor; every parsed event reaches callback."""

    def __init__(
        self,
        display: str,
        callback: Callable[[XI2Event | None], None],
        *,
        xauthority: str = "",
    ):
        self.display = display
        self.callback = callback
        self.observer = XI2Observer(display, xauthority)
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._events: list[XI2Event] = []
        self._text: list[str] = []
        self._parser: XI2StreamParser | None = None
        self._failure_diagnostic: dict[str, str] | None = None
        self._condition = threading.Condition()
        self._started = threading.Event()

    def start(self) -> None:
        self.observer.start()
        self._stop.clear()
        self._started.clear()
        self.thread = threading.Thread(target=self._read, name="crowley-xi2-guardian", daemon=True)
        self.thread.start()

    def release_admission_gate(self) -> None:
        self.observer.release_admission_gate()

    def wait_ready(self, timeout: float = 1.0) -> None:
        """Wait for the reader/exec handshake outside the guardian fence."""

        self.observer.verify_exec_identity()
        if not self._started.wait(timeout):
            self.stop()
            raise DriverError("XI2 observer reader did not start")
        process = self.observer.process
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            self.stop()
            raise DriverError("XI2 observer exited during startup")

    @property
    def alive(self) -> bool:
        process = self.observer.process
        if self.thread is None or not self.thread.is_alive():
            return False
        poll = getattr(process, "poll", None)
        return not callable(poll) or poll() is None

    @property
    def failure_diagnostic(self) -> dict[str, str] | None:
        """Return the complete observer text and exact parser failure, if any."""

        with self._condition:
            return dict(self._failure_diagnostic) if self._failure_diagnostic is not None else None

    @staticmethod
    def _diagnostic_text(value: str, *, limit: int = _INPUT_DIAGNOSTIC_MAX_RAW_BYTES) -> str:
        """Bound diagnostic text without changing the validation stream."""

        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= limit:
            return value
        return encoded[:limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _diagnostic_blocks(raw: str) -> tuple[list[str], str, int, int, bool]:
        """Split retained XI2 output into bounded complete blocks and a tail.

        The parser remains the authority for runtime validation.  This helper
        only snapshots the already-read stream after a failure, so a
        truncated diagnostic can never make an event acceptable.
        """

        blocks: list[str] = []
        current: list[str] = []
        total_bytes = len(raw.encode("utf-8", errors="replace"))
        blocks_seen = 0
        retained_bytes = 0
        truncated = False

        def append_block(value: str) -> None:
            nonlocal blocks_seen, retained_bytes, truncated
            if not value:
                return
            blocks_seen += 1
            encoded_size = len(value.encode("utf-8", errors="replace"))
            if (
                len(blocks) >= _INPUT_DIAGNOSTIC_MAX_BLOCKS
                or retained_bytes + encoded_size > _INPUT_DIAGNOSTIC_MAX_RAW_BYTES
            ):
                truncated = True
                return
            blocks.append(value)
            retained_bytes += encoded_size

        for line in raw.splitlines(keepends=True):
            # xinput emits blank-delimited records.  A new EVENT header also
            # closes a malformed/partial preceding record for diagnostics.
            if (
                line.startswith("EVENT type ")
                and current
                and not any(item.strip() == "" for item in current[-1:])
            ):
                append_block("".join(current))
                current = []
            current.append(line)
            if line.strip() == "":
                append_block("".join(current))
                current = []
        partial = "".join(current)
        if partial:
            partial_bytes = len(partial.encode("utf-8", errors="replace"))
            available = max(0, _INPUT_DIAGNOSTIC_MAX_RAW_BYTES - retained_bytes)
            if partial_bytes > available:
                partial = InputMonitor._diagnostic_text(partial, limit=available)
                truncated = True
        return blocks, partial, total_bytes, blocks_seen, truncated

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return a bounded failure-time snapshot of the actual observer stream.

        This is intentionally called by the failure path after observer
        shutdown.  It does not create a second reader, query X11, or alter the
        event list used by calibration validation.
        """

        with self._condition:
            raw = "".join(self._text)
            blocks, partial, total_bytes, blocks_seen, truncated = self._diagnostic_blocks(raw)
            failure = dict(self._failure_diagnostic) if self._failure_diagnostic is not None else None
            parser = self._parser
            parser_failure = getattr(parser, "failure_diagnostic", None) if parser is not None else None
            if isinstance(parser_failure, dict):
                parser_failure = {
                    "reason": self._diagnostic_text(str(parser_failure.get("reason", "")), limit=4096)
                }
            if failure is not None:
                failure["reason"] = self._diagnostic_text(str(failure.get("reason", "")), limit=4096)
                # The actual text is represented once by rawBlocks/partialRaw;
                # retaining it again here would defeat the aggregate bound.
                failure.pop("text", None)
            retained_bytes = len("".join(blocks).encode("utf-8", errors="replace")) + len(
                partial.encode("utf-8", errors="replace")
            )
            return {
                "rawBlocks": blocks,
                "partialRaw": self._diagnostic_text(partial),
                "rawBytes": retained_bytes,
                "rawBytesSeen": total_bytes,
                "rawBlocksSeen": blocks_seen + (1 if partial else 0),
                "rawCompleteBlocksSeen": blocks_seen,
                "rawBlocksRetained": len(blocks),
                "rawTruncated": bool(truncated),
                "parsedEventCount": len(self._events),
                "parserFailure": parser_failure,
                "failureDiagnostic": failure,
                "observerAlive": self.alive,
            }

    def _read(self) -> None:
        process = self.observer.process
        if process is None or process.stdout is None:
            return
        parser = XI2StreamParser()
        self._parser = parser
        self._started.set()
        while not self._stop.is_set():
            line = process.stdout.readline()
            if not line:
                unexpected = not self._stop.is_set()
                try:
                    event = parser.finish()
                except InputEventError as exc:
                    with self._condition:
                        parser_diagnostic = parser.failure_diagnostic
                        self._failure_diagnostic = {
                            "text": "".join(self._text),
                            "reason": (
                                parser_diagnostic["reason"] if parser_diagnostic is not None else str(exc)
                            ),
                        }
                    if unexpected:
                        self.callback(None)
                else:
                    if event is not None:
                        self._record_event(event)
                    if unexpected:
                        # EOF without an explicit stop is an observer death,
                        # even when its final block was valid.  Runtime code
                        # must revoke rather than continue with stale proof.
                        self.callback(None)
                return
            self._text.append(line)
            try:
                event = parser.feed(line)
            except InputEventError as exc:
                with self._condition:
                    parser_diagnostic = parser.failure_diagnostic
                    self._failure_diagnostic = {
                        "text": "".join(self._text),
                        "reason": (
                            parser_diagnostic["reason"] if parser_diagnostic is not None else str(exc)
                        ),
                    }
                self.callback(None)
                return
            if event is not None:
                self._record_event(event)

    def _record_event(self, event: XI2Event) -> None:
        """Publish an event only after its callback has updated completion state."""

        with self._condition:
            self._events.append(event)
        try:
            self.callback(event)
        finally:
            with self._condition:
                self._condition.notify_all()

    def events(self) -> list[XI2Event]:
        with self._condition:
            return list(self._events)

    def collect_until(self, predicate: Callable[[list[XI2Event]], bool], *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate(list(self._events)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("XI2 calibration event deadline expired")
                self._condition.wait(remaining)
            return "".join(self._text)

    def wait_for_human(self, predicate: Callable[[list[XI2Event]], bool], *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate(list(self._events)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("human input sample deadline expired")
                self._condition.wait(remaining)

    def wait_for_event(self, predicate: Callable[[list[XI2Event]], bool], *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not predicate(list(self._events)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("expected XTEST event was not observed")
                self._condition.wait(remaining)

    def stop(self) -> str:
        self._stop.set()
        trailing = ""
        if self.observer.process is not None:
            try:
                trailing = self.observer.stop()
            except DriverError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        if trailing:
            self._text.append(trailing)
            parser = self._parser or XI2StreamParser()
            try:
                for line in trailing.splitlines(keepends=True):
                    event = parser.feed(line)
                    if event is not None:
                        self._record_event(event)
                event = parser.finish()
                if event is not None:
                    self._record_event(event)
            except InputEventError as exc:
                with self._condition:
                    parser_diagnostic = parser.failure_diagnostic
                    self._failure_diagnostic = {
                        "text": "".join(self._text),
                        "reason": (
                            parser_diagnostic["reason"] if parser_diagnostic is not None else str(exc)
                        ),
                    }
                self.callback(None)
        return "".join(self._text)


class DesktopDriver:
    """Allowlisted actions bound to one verified client and one lease."""

    ACTIONS = {
        "focus_game",
        "open_chat",
        "type_allowlisted_command",
        "submit_command",
        "escape_command_buffer",
        "dismiss_disconnect",
        "reconnect_game",
        "select_arthur",
        "enter_world",
        "open_ah",
        "logout",
        "executor_click",
    }
    EXECUTOR_POINTER_ACTIONS = {"executor_click"}
    POINTER_ACTIONS = {
        "dismiss_disconnect",
        "reconnect_game",
        "select_arthur",
        "enter_world",
        "logout",
    }
    POINTER_BUTTONS = {
        "dismiss_disconnect": 1,
        "reconnect_game": 1,
        "select_arthur": 1,
        "enter_world": 1,
        "logout": 1,
        "executor_click": 1,
    }
    # Native visual anchors are bound to the named control proved by the
    # provider's fresh capture.  A generic world/config coordinate cannot
    # satisfy a modal or character-select action.
    ANCHOR_CONTROLS = {
        "dismiss_disconnect": "dismiss_disconnect",
        "reconnect_game": "reconnect_game",
        "select_arthur": "arthur_row",
        "enter_world": "enter_world",
        "logout": "world",
        "executor_click": "crowley_executor_button",
    }
    ANCHOR_SCENES = {
        "dismiss_disconnect": "disconnect_dialog",
        "reconnect_game": "game_client",
        "select_arthur": "character_select",
        "enter_world": "character_select",
        "open_ah": "world",
        "logout": "world",
        "executor_click": "executor_mailbox",
    }

    @classmethod
    def _is_pointer_action(cls, name: str) -> bool:
        return name in cls.POINTER_ACTIONS or name in cls.EXECUTOR_POINTER_ACTIONS

    TARGET_AUCTIONEER_COMMAND = "/targetexact Auctioneer Jaxon"
    CONDITIONAL_INTERACTION_BINDING_COMMAND = (
        "/run local a=GetBindingAction('CTRL-F12'); "
        "if a=='' or a=='INTERACTTARGET' then SetBinding('CTRL-F12','INTERACTTARGET') end"
    )
    COMMANDS = {
        "/crowley status",
        "/crowley scan",
        "/crowley session fence",
        "/reload",
        "/logout",
        TARGET_AUCTIONEER_COMMAND,
        CONDITIONAL_INTERACTION_BINDING_COMMAND,
    }

    def __init__(
        self,
        profile: DriverProfile | None = None,
        backend: Backend | None = None,
        *,
        lease_path: str | os.PathLike[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        character_select_observer: Callable[[WindowIdentity, str], NamedStateObservation] | None = None,
        named_state_observer: Callable[[WindowIdentity, str], NamedStateObservation] | None = None,
        native_measurement_provider: Callable[[WindowIdentity, str, int], Any] | None = None,
        native_memory_meter: Any | None = None,
        measured_anchor_provider: Callable[[WindowIdentity, str], MeasuredAnchor] | None = None,
        pointer_motion_profile: MotionProfile | None = None,
        camera_motion_profile: MotionProfile | None = None,
        move_motion_profile: MotionProfile | None = None,
    ):
        self.profile = profile or DriverProfile()
        self._clock = monotonic
        self._anchor_observation_deadline_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
            f"desktop_driver_anchor_observation_deadline_{id(self)}", default=None
        )
        self.pointer_motion_profile = pointer_motion_profile or MotionProfile()
        self.camera_motion_profile = camera_motion_profile or MotionProfile(role=DemonstrationRole.CAMERA)
        self.move_motion_profile = move_motion_profile or MotionProfile(role=DemonstrationRole.MOVE)
        if self.pointer_motion_profile.role is not DemonstrationRole.POINTER:
            raise DriverError("pointer motion profile has the wrong role")
        if self.camera_motion_profile.role is not DemonstrationRole.CAMERA:
            raise DriverError("camera motion profile has the wrong role")
        if self.move_motion_profile.role is not DemonstrationRole.MOVE:
            raise DriverError("move motion profile has the wrong role")
        self.backend = backend or X11Backend.production(self.profile)
        self.input_broker = InputBroker()
        if isinstance(self.backend, X11Backend):
            self.backend.input_broker = self.input_broker
        if isinstance(self.backend, X11Backend):
            try:
                self.profile.validate()
                _validate_x11_display_screen(self.profile.display)
            except Exception:
                self.backend.input_tool.close()
                self.backend.close()
                raise
            if self.backend.profile.display != self.profile.display:
                self.backend.input_tool.close()
                self.backend.close()
                raise DriverError("injected X11 backend display does not match the selected profile")
            expected_xauthority = self.profile.xauthority or os.environ.get("XAUTHORITY", "")
            expected_xauthority = (
                os.path.realpath(os.path.expanduser(expected_xauthority)) if expected_xauthority else ""
            )
            if self.backend.xauthority != expected_xauthority:
                self.backend.input_tool.close()
                self.backend.close()
                raise DriverError("injected X11 backend Xauthority does not match the selected profile")
        self._held_input = False
        watchdog_command: tuple[str, ...] = ()
        watchdog_env: dict[str, str] = {}
        watchdog_release_spec = None
        try:
            if lease_path is None:
                configured_lease = os.environ.get("CROWLEY_DESKTOP_LEASE")
                if configured_lease is not None:
                    if not configured_lease:
                        raise DriverError("CROWLEY_DESKTOP_LEASE must not be empty")
                    lease_path = Path(configured_lease)
                else:
                    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
                    if not runtime_dir:
                        raise DriverError("desktop lease requires XDG_RUNTIME_DIR or CROWLEY_DESKTOP_LEASE")
                    lease_path = Path(runtime_dir) / "crowley" / "desktop.lock"
            self.lease_path = Path(lease_path)
            if isinstance(self.backend, X11Backend):
                if not hasattr(self.backend, "input_tool"):
                    try:
                        self.backend.input_tool = PinnedInputTool.load()
                    except InputToolError as exc:
                        raise DriverError(f"private XTEST input tool is not admissible: {exc}") from exc
                # The independent watchdog receives the exact held item set and
                # a duplicate of the already pinned image.  A global
                # ``keyup --all`` fallback would release unrelated desktop input.
                watchdog_release_spec = self.backend.input_tool.duplicate_release_spec()
                watchdog_env = {
                    "DISPLAY": self.profile.display,
                    "XAUTHORITY": self.backend.xauthority,
                }
            self.guardian = InputGuardian(
                self.lease_path,
                release_input=self._release_physical_input,
                monotonic=monotonic,
                watchdog_command=watchdog_command,
                watchdog_env=watchdog_env,
                watchdog_release_spec=watchdog_release_spec,
            )
        except Exception:
            if isinstance(self.backend, X11Backend):
                self.backend.input_tool.close()
                self.backend.close()
            raise
        self.guardian.on_revoke(self._on_guardian_revoke)
        if isinstance(self.backend, X11Backend):
            self.backend.set_tool_drift_callback(self._on_input_tool_drift)
            self.backend.set_admission(self.guardian.dispatch_admission)
            self.backend.set_calibration_admission(self.guardian.calibration_dispatch_admission)
            self.backend.set_calibration_ticket_validator(self.guardian.calibration_ticket_current)
            self.backend.set_hold_publisher(self._publish_holds)
            self.backend.set_admission_health(self.guardian.admission_gate_ready)
            self.backend.set_process_group_callbacks(
                lambda generation, pgid: self.guardian.register_process_group(pgid, generation),
                self.guardian.unregister_process_group,
                lambda ticket, pgid: self.guardian.register_calibration_process_group(pgid, ticket),
            )
        self._queued: list[tuple[int, str, dict[str, Any]]] = []
        self._calibration_action_lock = threading.RLock()
        self._monitor: InputMonitor | None = None
        self._monitor_calibration = False
        self._takeover_input_callback: Callable[[XI2Event], None] | None = None
        self._calibration_timers: dict[int, threading.Timer] = {}
        self._calibration_due_waiters: dict[int, threading.Event] = {}
        self._calibration_epoch = 0
        self._calibration_ticket: CalibrationTicket | None = None
        self._calibration_executions = 0
        self._calibration_due_count = 0
        self._calibration_rejected_count = 0
        self._calibration_due_deadline: float | None = None
        self._calibration_target_process_groups: set[int] = set()
        self._calibration_bootstrap_process_groups: set[int] = set()
        self._revoke_callbacks: list[Callable[[str, int], None]] = []
        self._input_evidence: Any | None = None
        self._retained_input_evidence: Any | None = None
        self._input_calibration_readiness: Any | None = None
        self._active_revocation_cause = InvalidationCause.UNKNOWN
        self.driver_version = (
            f"{DRIVER_VERSION};{getattr(self.backend, 'input_tool_version', self.backend.input_tool.version)}"
            if isinstance(self.backend, X11Backend)
            else DRIVER_VERSION
        )
        self._last_identity: WindowIdentity | None = None
        self._character_select_observer = character_select_observer
        self._named_state_observer = named_state_observer or character_select_observer
        self._last_named_observation: NamedStateObservation | None = None
        self._named_capture_sequence = 0
        self._named_action_before: dict[str, NamedStateObservation] = {}
        self.native_measurement_provider = native_measurement_provider
        self.native_memory_meter = native_memory_meter
        self.notify_native_memory_reset_ready: Callable[[WindowIdentity, Any], Any] | None = None
        self.notify_native_hold_required: Callable[[WindowIdentity], None] | None = None
        self.notify_native_memory_ready: Callable[[WindowIdentity, Any], None] | None = None
        # The pre-resume hook receives a sealed NativeResumeBoundary, before
        # the provider is asked to publish normal/0.  The final resume-ready
        # hook receives the completed NativeCalibrationEvidence and its fresh
        # observation after that publication is decoded.
        self.notify_native_resume_required: Callable[[WindowIdentity, Any], None] | None = None
        self.notify_native_resume_ready: Callable[[WindowIdentity, Any, Any], None] | None = None
        self.notify_native_measurement_failed: Callable[[WindowIdentity, str], Any] | None = None
        # Lead-owned hook invoked immediately before the native reset5 memory
        # baseline. It establishes an idle renderer/bootstrap boundary but
        # must not construct addon textures or provide metrics.
        self.native_preconstruction_provider: Callable[..., Any] | None = None
        self.measured_anchor_provider = measured_anchor_provider
        self._measured_anchors: dict[str, MeasuredAnchor] = {}
        self._measured_anchor_generations: dict[str, int] = {}
        self._consumed_anchor_seals: set[str] = set()
        self._pointer_gesture_receipts: list[dict[str, Any]] = []
        self._last_takeover_hover_failure_context: dict[str, Any] | None = None

    @property
    def queued_calibration_count(self) -> int:
        return len(self._queued)

    @property
    def pointer_gesture_receipts(self) -> tuple[dict[str, Any], ...]:
        """Return copies of the bounded runtime gesture evidence history."""

        return tuple(json.loads(json.dumps(row)) for row in self._pointer_gesture_receipts)

    def _record_pointer_gesture_receipt(
        self,
        *,
        action: str,
        generation: int,
        gesture: HumanPointerPlan,
        accepted: bool,
        execution: dict[str, Any] | None = None,
    ) -> None:
        if execution is None:
            execution = self.backend.last_pointer_execution if isinstance(self.backend, X11Backend) else None
        self._pointer_gesture_receipts.append(
            {
                "action": action,
                "generation": generation,
                "seed": gesture.seed,
                "frozenEndpoint": (int(gesture.endpoint.x), int(gesture.endpoint.y)),
                "movementDuration": gesture.movement_duration,
                "dwell": gesture.dwell,
                "submovementCount": len(gesture.waypoints),
                "pathSha256": hashlib.sha256(
                    repr(
                        tuple(
                            (sample.at, sample.point.x, sample.point.y)
                            for sample in gesture.trajectory.samples
                        )
                    ).encode("ascii")
                ).hexdigest(),
                "accepted": accepted,
                "execution": copy.deepcopy(execution) if execution is not None else None,
            }
        )
        if len(self._pointer_gesture_receipts) > 32:
            del self._pointer_gesture_receipts[:-32]

    @property
    def xauthority(self) -> str:
        if isinstance(self.backend, X11Backend):
            return self.backend.xauthority
        return self.profile.xauthority or os.environ.get("XAUTHORITY", "")

    def character_select_observed(self) -> bool:
        """Return a verified, named character-select scene observation.

        A plain callback returning ``True`` is intentionally not accepted.  The
        callback must return :class:`NamedStateObservation`, which is tied to a
        fresh capture and includes the configured character/realm facts.
        """

        self.verify_named_action("logout", "after")
        return True

    @property
    def last_named_observation(self) -> NamedStateObservation | None:
        return self._last_named_observation

    _NAMED_ACTION_STATES: dict[str, tuple[str, str]] = {
        "dismiss_disconnect": ("disconnect_dialog", "game_client"),
        "reconnect_game": ("game_client", "character_select"),
        "select_arthur": ("character_select", "arthur_selected"),
        "enter_world": ("arthur_selected", "world"),
        "open_ah": ("world", "auction_house_open"),
        "logout": ("world", "character_select"),
    }
    _NAMED_OBSERVATION_SOURCES = frozenset({"native-visual", "native-addon"})

    def _named_state_facts(self, state: str) -> dict[str, str]:
        expected = {
            "disconnect_dialog": {
                "scene": "disconnect_dialog",
                "character": "",
                "realm": "",
                "modal": "WOW51900319",
                "code": "WOW51900319",
                "control": "dismiss_disconnect",
                "idle": "false",
            },
            "game_client": {
                "scene": "game_client",
                "character": "",
                "realm": "",
                "modal": "none",
                "code": "",
                "control": "client",
                "idle": "false",
            },
            "character_select": {
                "scene": "character_select",
                "character": self.profile.character,
                "realm": self.profile.realm,
                "modal": "character_select",
                "code": "",
                "control": "character_list",
                "idle": "true",
            },
            "arthur_selected": {
                "scene": "character_select",
                "character": self.profile.character,
                "realm": self.profile.realm,
                "modal": "character_select",
                "code": "",
                "control": "arthur_row",
                "idle": "true",
            },
            "world": {
                "scene": "world",
                "character": self.profile.character,
                "realm": self.profile.realm,
                "modal": "none",
                "code": "",
                "control": "world",
                "idle": "true",
            },
            "auction_house_open": {
                "scene": "world",
                "character": self.profile.character,
                "realm": self.profile.realm,
                "modal": "auction_house",
                "code": "",
                "control": "auction_house",
                "idle": "true",
            },
        }
        try:
            return expected[state]
        except KeyError as exc:
            raise DriverError(f"unknown named game state {state!r}") from exc

    def _validate_named_observation(
        self,
        identity: WindowIdentity,
        expected_state: str,
        observation: NamedStateObservation,
    ) -> None:
        if observation.state != expected_state:
            raise DriverError(f"named state mismatch: expected {expected_state}, got {observation.state}")
        if observation.identity_fingerprint != identity.fingerprint:
            raise DriverError("named state observation identity is stale")
        if observation.target_xid <= 0:
            raise DriverError("named state observation has no target XID")
        if isinstance(self.backend, X11Backend):
            if not self._is_owned_client_target(observation.target_xid, identity):
                raise DriverError("named state observation target is outside the verified client")
        elif observation.target_xid != identity.child_xid:
            raise DriverError("fake named state observation target does not match the client child")
        if not observation.capture_id or observation.capture_sequence <= 0:
            raise DriverError("named state observation has no fresh capture identity")
        if observation.source not in self._NAMED_OBSERVATION_SOURCES:
            raise DriverError("named state observation source is not native evidence")
        now = self._clock()
        if (
            isinstance(observation.observed_monotonic, bool)
            or not isinstance(observation.observed_monotonic, (int, float))
            or not math.isfinite(float(observation.observed_monotonic))
        ):
            raise DriverError("named state observation timestamp is not numeric")
        age = now - float(observation.observed_monotonic)
        if age < -0.25 or age > 5.0:
            raise DriverError("named state observation is stale")
        facts = dict(observation.facts)
        if len(facts) != len(observation.facts):
            raise DriverError("named state observation contains duplicate facts")
        if isinstance(self.backend, X11Backend):
            required = self._named_state_facts(expected_state)
        else:
            # Deterministic non-X11 backends predate the native visual
            # producer and intentionally expose only the stable scene facts.
            # They still must provide a named observation object with a fresh
            # capture; identity-only booleans are rejected above.
            required = {
                "disconnect_dialog": {"scene": "disconnect_dialog"},
                "game_client": {"scene": "game_client"},
                "character_select": {"scene": "character_select", "realm": self.profile.realm},
                "arthur_selected": {
                    "scene": "character_select",
                    "character": self.profile.character,
                    "realm": self.profile.realm,
                },
                "world": {
                    "scene": "world",
                    "character": self.profile.character,
                    "realm": self.profile.realm,
                },
                "auction_house_open": {
                    "scene": "world",
                    "character": self.profile.character,
                    "realm": self.profile.realm,
                    "auction_house": "open",
                },
            }[expected_state]
        if any(facts.get(key) != value for key, value in required.items()):
            raise DriverError("named state observation lacks the required visual/state facts")

    def observe_named_state(self, state: str) -> NamedStateObservation:
        """Collect and verify one named scene from the configured native observer."""

        self._named_state_facts(state)
        identity = self.resolve_identity()
        observer = self._named_state_observer
        if observer is None:
            observer = getattr(self.backend, "observe_named_state", None)
        if not callable(observer):
            raise DriverError("native named-state observer is not configured")
        try:
            observation = observer(identity, state)
        except Exception as exc:
            raise DriverError(f"native named-state observation failed: {exc}") from exc
        if not isinstance(observation, NamedStateObservation):
            raise DriverError("native named-state observer returned identity-only or boolean evidence")
        self._validate_named_observation(identity, state, observation)
        self._last_named_observation = observation
        return observation

    def verify_named_action(
        self,
        name: str,
        stage: str,
        snapshot_or_capture: NamedStateObservation | None = None,
        *,
        observation: NamedStateObservation | None = None,
    ) -> bool:
        """Verify an action's modal pre/post state against fresh native evidence.

        ``stage`` is ``"before"`` or ``"after"``.  If no observation is
        supplied, the configured observer is called with the required state.
        Supplied observations still undergo every identity, ancestry, freshness,
        and named-fact check; a snapshot containing only an equal identity is
        rejected.
        """

        try:
            before, after = self._NAMED_ACTION_STATES[name]
        except KeyError as exc:
            raise DriverError(f"action {name!r} has no named-state contract") from exc
        if stage not in {"before", "after"}:
            raise DriverError("named action stage must be before or after")
        expected_state = before if stage == "before" else after
        if snapshot_or_capture is not None and observation is not None:
            raise DriverError("named action received two observations")
        selected_observation = snapshot_or_capture or observation or self.observe_named_state(expected_state)
        if not isinstance(selected_observation, NamedStateObservation):
            raise DriverError("named action requires explicit native state evidence")
        identity = self.resolve_identity()
        self._validate_named_observation(identity, expected_state, selected_observation)
        if stage == "after":
            before_observation = self._named_action_before.pop(name, None)
            if before_observation is not None and (
                selected_observation.capture_sequence <= before_observation.capture_sequence
                or selected_observation.observed_monotonic <= before_observation.observed_monotonic
            ):
                raise DriverError("named action postcondition reused the pre-action capture")
        else:
            self._named_action_before[name] = selected_observation
        self._last_named_observation = selected_observation
        return True

    def on_revoke(self, callback: Callable[[str, int], None]) -> None:
        self._revoke_callbacks.append(callback)

    def _release_physical_input(self) -> None:
        release = getattr(self.backend, "release_held_input", None)
        if release is not None:
            release()
        self._held_input = False

    def _on_input_tool_drift(self, reason: str) -> None:
        """Invalidate all retained native evidence when the emitter changes."""

        self._input_evidence = None
        self._retained_input_evidence = None
        self._input_calibration_readiness = None
        if self.guardian.armed:
            self.revoke(reason, invalidation_cause=InvalidationCause.INPUT_ENVIRONMENT_CHANGED)

    def _publish_holds(
        self,
        authority: int | CalibrationTicket,
        keys: set[str],
        buttons: set[int],
    ) -> None:
        if isinstance(authority, CalibrationTicket):
            self.guardian.publish_calibration_holds(authority, keys, buttons)
        else:
            self.guardian.publish_current_holds(authority, keys, buttons)

    def _on_guardian_revoke(self, reason: str, generation: int) -> None:
        if isinstance(self.backend, X11Backend):
            self.backend._calibration_focus_ticket = None
        with self._calibration_action_lock:
            # The timer and revocation share this lock so a due callback cannot
            # pass its generation check after a human event has cleared it.
            self._queued.clear()
            # Human input closes the calibration-only epoch as well as the
            # game epoch.  A ticket from before takeover can never revive on a
            # later arm or retry.
            self._calibration_ticket = None
            waiters = tuple(self._calibration_due_waiters.values())
            self._calibration_due_waiters.clear()
        for waiter in waiters:
            waiter.set()
        # Let an already scheduled calibration timer reach its bounded due
        # point and record that it was suppressed by the generation fence.
        # Cancelling it here would leave a fast human sample unable to prove
        # that the queued harmless action was actually prevented.
        self._held_input = False
        for callback in tuple(self._revoke_callbacks):
            callback(reason, generation)
        if self._monitor is not None and not self._monitor_calibration:
            # Keep the observer registry in lockstep with the actual helper;
            # stopping the subprocess without unregistering its PGID would
            # leave the owner-death watchdog believing a dead observer is
            # still live.
            self.stop_input_observer()

    def begin_calibration_operation(self) -> int:
        """Start a fresh calibration epoch and return its admission epoch."""

        with self._calibration_action_lock:
            try:
                epoch = self.guardian.begin_calibration_operation()
            except GuardianError as exc:
                raise DriverError(str(exc)) from exc
            timers = tuple(self._calibration_timers.values())
            self._calibration_timers.clear()
            waiters = tuple(self._calibration_due_waiters.values())
            self._calibration_due_waiters.clear()
            self._queued.clear()
            self._calibration_due_count = 0
            self._calibration_executions = 0
            self._calibration_rejected_count = 0
            self._calibration_due_deadline = None
            self._calibration_epoch = epoch
            self._calibration_ticket = None
            if isinstance(self.backend, X11Backend):
                self.backend._calibration_focus_ticket = None
        for waiter in waiters:
            waiter.set()
        for timer in timers:
            timer.cancel()
        for pid in tuple(self._calibration_target_process_groups):
            if not Path(f"/proc/{pid}").exists():
                self.guardian.unregister_process_group(pid)
                self._calibration_target_process_groups.discard(pid)
        for pid in tuple(self._calibration_bootstrap_process_groups):
            if not Path(f"/proc/{pid}").exists():
                self.guardian.unregister_process_group(pid)
                self._calibration_bootstrap_process_groups.discard(pid)
        return epoch

    def _calibration_dispatch_admission(self, ticket: CalibrationTicket, dispatch: Callable[[], Any]) -> Any:
        """Admit only the currently owned GTK target before human takeover."""

        if not isinstance(ticket, CalibrationTicket):
            raise GuardianError("calibration admission requires a calibration ticket")
        return self.guardian.calibration_dispatch_admission(ticket, dispatch)

    def admit_calibration_target_process(
        self, pgid: int, release_gate: Callable[[], None]
    ) -> Callable[[], None]:
        """Register and positively admit a gated GTK process.

        The epoch, watchdog health check, registration, and gate release share
        the Guardian lock.  A failed target verification can then call the
        returned capability to remove this exact bootstrap group before the
        next calibration epoch is requested.
        """

        try:
            with self._calibration_action_lock:
                expected_epoch = self._calibration_epoch
                with self.guardian._lock:
                    epoch = self.guardian._calibration_epoch
                    if expected_epoch <= 0 or epoch <= 0:
                        raise DriverError("calibration target startup has no active epoch")
                    if epoch != expected_epoch:
                        raise DriverError("calibration target startup epoch is stale")
                    if self.guardian._watchdog_failed:
                        raise DriverError("calibration target startup watchdog is unhealthy")
                    self.guardian.admission_gate_ready()
                    self.guardian.register_calibration_bootstrap_process(pgid, expected_epoch)
                    if self.guardian._calibration_epoch != expected_epoch:
                        raise DriverError("calibration target startup epoch changed during admission")
                    self.guardian.admission_gate_ready()
                    release_gate()
        except Exception as exc:
            try:
                self.guardian.unregister_process_group(pgid)
            except Exception as cleanup_exc:
                exc.add_note(f"calibration target startup unregister failed: {cleanup_exc}")
            if isinstance(exc, DriverError):
                raise
            admission_error = DriverError("calibration target startup admission failed")
            for note in getattr(exc, "__notes__", ()):
                admission_error.add_note(note)
            raise admission_error from exc
        self._calibration_bootstrap_process_groups.add(pgid)

        released = False

        def unregister() -> None:
            nonlocal released
            if released:
                return
            released = True
            try:
                self.guardian.unregister_process_group(pgid)
            finally:
                self._calibration_bootstrap_process_groups.discard(pgid)

        return unregister

    @staticmethod
    def _calibration_target_certificate(
        target: Any,
    ) -> tuple[
        frozenset[int],
        str,
        int,
        int,
        int,
        tuple[int, ...],
        frozenset[int],
        frozenset[int],
    ]:
        """Return the exact owned GTK layout and its event-capable XID set."""

        from .calibration import GTK_LAYOUT_MUTTER_FRAME, GTK_LAYOUT_ROOT_CHILD

        allowed = frozenset(target.allowed_event_target_xids)
        descendants = frozenset(target.descendant_xids)
        target_xid = target.xid
        frame_xid = target.managed_frame_xid
        frame_geometry = tuple(target.managed_frame_geometry)
        layout_kind = getattr(target, "layout_kind", GTK_LAYOUT_MUTTER_FRAME)
        root_xid = getattr(target, "root_xid", 0)
        parent_xid = getattr(target, "parent_xid", 0)
        excluded = frozenset(getattr(target, "excluded_input_only_xids", ()))
        target_executable = getattr(target, "executable", "")
        target_process_start = getattr(target, "process_start", "")
        if (
            layout_kind not in {GTK_LAYOUT_MUTTER_FRAME, GTK_LAYOUT_ROOT_CHILD}
            or not allowed
            or not descendants
            or any(isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0 for xid in allowed)
            or any(isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0 for xid in descendants)
            or any(isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0 for xid in excluded)
            or isinstance(target_xid, bool)
            or not isinstance(target_xid, int)
            or target_xid <= 0
            or target_xid not in descendants
            or isinstance(root_xid, bool)
            or not isinstance(root_xid, int)
            or root_xid < 0
            or isinstance(parent_xid, bool)
            or not isinstance(parent_xid, int)
            or parent_xid < 0
            or root_xid in allowed
            or root_xid in excluded
            or allowed & excluded
            or not isinstance(target_executable, str)
            or not target_executable
            or not os.path.isabs(target_executable)
            or not isinstance(target_process_start, str)
            or not target_process_start
        ):
            raise DriverError("owned GTK target event-target certificate is incomplete or altered")
        if layout_kind == GTK_LAYOUT_ROOT_CHILD:
            if (
                root_xid <= 0
                or parent_xid != root_xid
                or frame_xid not in (None, 0)
                or frame_geometry
                or target_xid in excluded
                or len(excluded) != 1
                or not excluded.issubset(descendants)
                or root_xid in descendants
                or descendants != {target_xid, *excluded}
                or allowed != frozenset({target_xid})
            ):
                raise DriverError("owned GTK root-child certificate is incomplete or altered")
        else:
            if (
                isinstance(frame_xid, bool)
                or not isinstance(frame_xid, int)
                or frame_xid <= 0
                or frame_xid in descendants
                or allowed != frozenset({frame_xid, *descendants})
                or len(frame_geometry) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in frame_geometry)
                or frame_geometry[2] <= 0
                or frame_geometry[3] <= 0
                or (root_xid > 0 and parent_xid != frame_xid)
                or (root_xid > 0 and root_xid in descendants)
            ):
                raise DriverError("owned GTK managed-frame certificate is incomplete or altered")
        target_geometry = tuple(target.geometry)
        if (
            len(target_geometry) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in target_geometry)
            or target_geometry[2] <= 0
            or target_geometry[3] <= 0
            or (
                layout_kind == GTK_LAYOUT_MUTTER_FRAME
                and not (
                    frame_geometry[0] <= target_geometry[0]
                    and frame_geometry[1] <= target_geometry[1]
                    and frame_geometry[0] + frame_geometry[2] >= target_geometry[0] + target_geometry[2]
                    and frame_geometry[1] + frame_geometry[3] >= target_geometry[1] + target_geometry[3]
                )
            )
        ):
            raise DriverError("owned GTK target geometry certificate is invalid")
        return (
            allowed,
            layout_kind,
            root_xid,
            parent_xid,
            int(frame_xid or 0),
            frame_geometry,
            descendants,
            excluded,
        )

    @classmethod
    def _verify_calibration_target(cls, ticket: CalibrationTicket, target: Any) -> None:
        (
            allowed,
            layout_kind,
            root_xid,
            parent_xid,
            frame_xid,
            frame_geometry,
            descendants,
            excluded,
        ) = cls._calibration_target_certificate(target)
        if (
            int(getattr(target, "pid", 0)) != ticket.target_pid
            or int(getattr(target, "xid", 0)) != ticket.target_xid
            or getattr(target, "title", "") != ticket.title
            or tuple(getattr(target, "geometry", ())) != ticket.geometry
            or getattr(target, "executable", "") != ticket.target_executable
            or getattr(target, "process_start", "") != ticket.target_process_start
            or allowed != ticket.target_xids
            or frame_xid != ticket.managed_frame_xid
            or frame_geometry != ticket.managed_frame_geometry
            or layout_kind != ticket.target_layout_kind
            or root_xid != ticket.target_root_xid
            or parent_xid != ticket.target_parent_xid
            or descendants != ticket.target_descendant_xids
            or excluded != ticket.target_excluded_input_only_xids
        ):
            raise DriverError("owned GTK calibration target changed after ticket issue")

    def cancel_calibration_actions(self) -> None:
        """Discard timers/queue after a calibration result is retained."""

        with self._calibration_action_lock:
            # Invalidate the guardian ticket while this bookkeeping lock is
            # held.  Releasing the local lock first would let a concurrent
            # retry issue a replacement ticket before cancellation observes
            # the old identity, leaving that stale epoch live.
            ticket = self._calibration_ticket
            self.guardian.cancel_calibration_operation(ticket)
            timers = tuple(self._calibration_timers.values())
            self._calibration_timers.clear()
            self._queued.clear()
            self._calibration_ticket = None
            self._calibration_due_deadline = None
            waiters = tuple(self._calibration_due_waiters.values())
            self._calibration_due_waiters.clear()
        for waiter in waiters:
            waiter.set()
        for timer in timers:
            timer.cancel()
        # Owned GTK helpers are registered when their ticket is issued.  Do
        # not drop that ownership until close() has confirmed process reap;
        # once /proc is gone, unregister the exact group exactly once.
        for pid in tuple(self._calibration_target_process_groups):
            if Path(f"/proc/{pid}").exists():
                continue
            self.guardian.unregister_process_group(pid)
            self._calibration_target_process_groups.discard(pid)
        for pid in tuple(self._calibration_bootstrap_process_groups):
            if not Path(f"/proc/{pid}").exists():
                self.guardian.unregister_process_group(pid)
                self._calibration_bootstrap_process_groups.discard(pid)

    def _issue_calibration_ticket(self, target: Any) -> CalibrationTicket:
        """Create or reuse the exact guardian-owned GTK capability."""

        from .calibration import OwnedGtkCalibrationWindow, OwnedWindow

        if not isinstance(target, OwnedWindow) or target.title != OwnedGtkCalibrationWindow.TITLE:
            raise DriverError("calibration target is not the owned GTK window")
        (
            target_xids,
            layout_kind,
            root_xid,
            parent_xid,
            managed_frame_xid,
            managed_frame_geometry,
            descendants,
            excluded,
        ) = self._calibration_target_certificate(target)
        with self._calibration_action_lock:
            expected_epoch = self._calibration_epoch
            with self.guardian._lock:
                guardian_epoch = self.guardian._calibration_epoch
                if expected_epoch <= 0 or guardian_epoch <= 0:
                    raise DriverError("calibration operation epoch is not active")
                if guardian_epoch != expected_epoch:
                    raise DriverError("calibration operation epoch is stale")
                current = self._calibration_ticket
                if current is not None and not (
                    not self.guardian.armed
                    and self.guardian._calibration_ticket is current
                    and getattr(current, "epoch", 0) == guardian_epoch
                ):
                    # The guardian is authoritative for epoch lifetime.  A
                    # local mirror can lag a revoke/begin transition, so it
                    # must never be reused merely because the GTK object
                    # still compares equal.
                    self._calibration_ticket = None
                    current = None
            if current is not None:
                self._verify_calibration_target(current, target)
                return current
            epoch = expected_epoch
            ticket = CalibrationTicket(
                epoch,
                int(target.pid),
                int(target.xid),
                target_xids,
                target.title,
                tuple(target.geometry),
                managed_frame_xid,
                managed_frame_geometry,
                str(target.executable),
                str(target.process_start),
                layout_kind,
                root_xid,
                parent_xid,
                descendants,
                excluded,
            )
            try:
                self.guardian.issue_calibration_ticket(ticket)
            except GuardianError as exc:
                raise DriverError(str(exc)) from exc
            if isinstance(self.backend, X11Backend) and isinstance(target.process, subprocess.Popen):
                try:
                    if os.getpgid(ticket.target_pid) != ticket.target_pid:
                        raise DriverError("owned GTK target is not an independently owned process group")
                except OSError as exc:
                    raise DriverError("owned GTK target process group is unavailable") from exc
                try:
                    if ticket.target_pid in self._calibration_bootstrap_process_groups:
                        self.guardian.adopt_calibration_bootstrap_process(ticket.target_pid, ticket)
                        self._calibration_bootstrap_process_groups.discard(ticket.target_pid)
                    else:
                        self.guardian.register_calibration_process_group(ticket.target_pid, ticket)
                except GuardianError as exc:
                    raise DriverError(str(exc)) from exc
                self._calibration_target_process_groups.add(ticket.target_pid)
            self._calibration_ticket = ticket
            return ticket

    def issue_calibration_ticket(self, target: Any) -> CalibrationTicket:
        """Publish the calibration capability before target preparation.

        The ticket is deliberately separate from delayed scheduling: callers
        must hold this exact capability while focus/synthetic proof runs, then
        pass it back when they schedule the bounded delayed action.
        """

        return self._issue_calibration_ticket(target)

    def arm(self, evidence: Any | None = None) -> int:
        # Calibration owns a temporary observer.  Explicit resume replaces it
        # with a live runtime observer so events cannot pass through a stopped
        # monitor after the generation fence is re-armed.
        if self._monitor is not None and self._monitor_calibration:
            calibration_monitor = self._monitor
            self.stop_input_observer(calibration_monitor)
        try:
            evidence.verify_seal()
        except Exception as exc:
            raise DriverError("input arm requires owned live runtime evidence") from exc
        if isinstance(self.backend, X11Backend):
            self.backend._check_published_tool()
        if isinstance(evidence, RuntimeInputEvidence):
            if not evidence.is_live:
                raise RuntimeReadinessError(
                    "runtime startup evidence is stale",
                    InvalidationCause.UNKNOWN,
                )
            if evidence.display != self.profile.display:
                raise RuntimeReadinessError(
                    "runtime startup evidence is bound to another display",
                    InvalidationCause.INPUT_ENVIRONMENT_CHANGED,
                )
            if not isinstance(self.backend, X11Backend):
                raise RuntimeReadinessError(
                    "runtime startup evidence requires the owned X11 backend",
                    InvalidationCause.INPUT_ENVIRONMENT_CHANGED,
                )
            current_hierarchy = self.input_hierarchy()
            if current_hierarchy != evidence.xinput_hierarchy:
                raise RuntimeReadinessError(
                    "runtime XInput hierarchy changed after passive startup",
                    InvalidationCause.INPUT_ENVIRONMENT_CHANGED,
                )
            current_profile = self.xtest_input_profile(current_hierarchy)
            if current_profile.canonical_payload() != evidence.xtest_profile.canonical_payload():
                raise RuntimeReadinessError(
                    "runtime XTEST profile changed after passive startup",
                    InvalidationCause.INPUT_ENVIRONMENT_CHANGED,
                )
            levels = tuple(
                sorted(
                    (
                        keycode,
                        tuple(symbols),
                    )
                    for keycode, symbols in self.backend.observe_keymap_levels().items()
                )
            )
            if levels != evidence.environment.keymap_levels:
                raise RuntimeReadinessError(
                    "runtime keyboard keymap changed after passive startup",
                    InvalidationCause.INPUT_ENVIRONMENT_CHANGED,
                )
            native_state = self.backend.observe_native_input_state(current_profile.root_xid)
            if native_state[1] or native_state[2]:
                raise RuntimeReadinessError(
                    "native input became held before runtime arm",
                    InvalidationCause.UNKNOWN,
                )
            if native_state[0] != evidence.native_modifiers:
                raise RuntimeReadinessError(
                    "native modifier state changed before runtime arm",
                    InvalidationCause.UNKNOWN,
                )
            try:
                current_identity = self.resolve_identity(require_foreground=True)
            except DriverError as exc:
                raise RuntimeReadinessError(
                    "game identity could not be reverified before runtime arm",
                    InvalidationCause.GAME_PROCESS_CHANGED,
                ) from exc
            if (
                current_identity.fingerprint != evidence.game_identity_fingerprint
                or current_identity.process_start != evidence.game_process_start
                or tuple(current_identity.child_geometry) != evidence.game_geometry
            ):
                raise RuntimeReadinessError(
                    "game identity or geometry changed after passive startup",
                    InvalidationCause.GAME_PROCESS_CHANGED,
                )
            if self._monitor is None or not self._monitor.alive:
                raise RuntimeReadinessError(
                    "runtime XI2 observer is not alive at arm boundary",
                    InvalidationCause.OBSERVER_LOST,
                )
        else:
            if getattr(evidence, "display", self.profile.display) != self.profile.display:
                raise DriverError("input calibration display does not match the configured display")
            if getattr(evidence, "driver_version", self.driver_version) != self.driver_version:
                raise DriverError("input calibration was produced by a different driver version")
            evidence_xauthority = getattr(evidence, "xauthority", "")
            if evidence_xauthority and evidence_xauthority != self.xauthority:
                raise DriverError("input calibration Xauthority changed")
            if isinstance(self.backend, X11Backend):
                current_hierarchy = self.input_hierarchy()
                if current_hierarchy != tuple(getattr(evidence, "xinput_hierarchy", ())):
                    raise DriverError("input calibration XInput hierarchy changed")
                current_profile = self.xtest_input_profile(current_hierarchy)
                sealed_profile = getattr(evidence, "xtest_profile", None)
                if (
                    sealed_profile is None
                    or current_profile.canonical_payload() != sealed_profile.canonical_payload()
                ):
                    raise DriverError("input calibration XTEST profile changed")
                evidence_identity = getattr(evidence, "game_identity_fingerprint", "")
                if not evidence_identity:
                    raise DriverError("input calibration has no verified game identity")
                current_identity = self.resolve_identity(require_foreground=False)
                if current_identity.fingerprint != evidence_identity:
                    raise DriverError("input calibration game identity changed")
                if tuple(getattr(evidence, "game_geometry", ())) != current_identity.child_geometry:
                    raise DriverError("input calibration game geometry changed")
        self._input_evidence = evidence
        started_runtime_monitor: InputMonitor | None = None
        try:
            if isinstance(self.backend, X11Backend) and self._monitor is not None:
                monitor_alive = getattr(self._monitor, "alive", True)
                if callable(monitor_alive):
                    monitor_alive = monitor_alive()
                if monitor_alive is False:
                    dead_monitor = self._monitor
                    self.stop_input_observer(dead_monitor, already_stopped=True)
            if isinstance(self.backend, X11Backend) and self._monitor is None:
                try:
                    started_runtime_monitor = self.start_input_observer()
                except DriverError as exc:
                    raise RuntimeReadinessError(
                        "runtime XI2 observer could not be started",
                        InvalidationCause.OBSERVER_LOST,
                    ) from exc
                monitor_alive = getattr(started_runtime_monitor, "alive", True)
                if callable(monitor_alive):
                    monitor_alive = monitor_alive()
                if monitor_alive is False:
                    raise RuntimeReadinessError(
                        "runtime XI2 observer is not alive at arm boundary",
                        InvalidationCause.OBSERVER_LOST,
                    )
            # The observer must have a live stdout reader before the lease is
            # armed.  A monitor dying between arm and startup would otherwise
            # leave a generation that can never observe revocation.
            generation = self.guardian.arm(evidence)
            if isinstance(self.backend, X11Backend):
                self.guardian.dispatch_admission(
                    generation,
                    lambda: self.backend.activate_generation(generation),
                )
                live_after_arm = getattr(self._monitor, "alive", True)
                if callable(live_after_arm):
                    live_after_arm = live_after_arm()
                if live_after_arm is False:
                    self.revoke(
                        "runtime XI2 observer died at arm boundary",
                        invalidation_cause=InvalidationCause.OBSERVER_LOST,
                    )
                    raise RuntimeReadinessError(
                        "runtime XI2 observer died at arm boundary",
                        InvalidationCause.OBSERVER_LOST,
                    )
        except Exception as exc:
            self._input_evidence = None
            if self.guardian.armed:
                cause = getattr(exc, "invalidation_cause", InvalidationCause.UNKNOWN)
                if not isinstance(cause, InvalidationCause):
                    cause = InvalidationCause.UNKNOWN
                self.revoke(
                    "runtime input arm failed",
                    invalidation_cause=cause,
                )
            elif started_runtime_monitor is not None:
                self.stop_input_observer(started_runtime_monitor)
            raise
        return generation

    def stop_input_observer(
        self, monitor: InputMonitor | None = None, *, already_stopped: bool = False
    ) -> None:
        """Stop and forget a calibration/runtime monitor exactly once."""

        if self._monitor is None or (monitor is not None and monitor is not self._monitor):
            return
        current = self._monitor
        process = getattr(getattr(current, "observer", None), "process", None)
        pid = getattr(process, "pid", None)
        if not already_stopped:
            current.stop()
        if process is not None and getattr(process, "poll", lambda: 0)() is None:
            raise DriverError("XI2 observer termination could not be confirmed")
        self._monitor = None
        self._monitor_calibration = False
        # Keep the PGID in the guardian/watchdog registry until the helper's
        # stop path has returned.  A failed termination must remain owned for
        # owner-death cleanup rather than becoming an untracked child.
        if isinstance(pid, int) and pid > 0:
            self.guardian.unregister_observer_process(pid)

    def start_input_observer(
        self,
        *,
        calibration: bool = False,
        callback: Callable[[XI2Event | None], None] | None = None,
    ) -> InputMonitor:
        if self._monitor is not None:
            return self._monitor
        observer_callback = callback or ((lambda _event: None) if calibration else self._external_input)
        xauthority = self.xauthority
        monitor = InputMonitor(self.profile.display, observer_callback, xauthority=xauthority)
        try:
            self.guardian.observer_dispatch_admission(
                lambda: monitor.start() or monitor,
                calibration=calibration,
            )
            monitor.wait_ready()
        except Exception:
            process = getattr(getattr(monitor, "observer", None), "process", None)
            pid = getattr(process, "pid", None)
            try:
                monitor.stop()
            except Exception:
                # Keep a failed-to-reap observer reachable by the driver so a
                # later close/revoke can retry it.  Its PGID remains in the
                # watchdog registry until termination is confirmed.
                self._monitor = monitor
                self._monitor_calibration = calibration
                raise
            if process is not None and getattr(process, "poll", lambda: 0)() is None:
                self._monitor = monitor
                self._monitor_calibration = calibration
            elif isinstance(pid, int) and pid > 0:
                self.guardian.unregister_observer_process(pid)
            raise
        self._monitor = monitor
        self._monitor_calibration = calibration
        return monitor

    def input_hierarchy(
        self, *, generation: int | None = None, deadline_monotonic: float | None = None
    ) -> tuple[tuple[int, int, str, str], ...]:
        """Read the current XInput hierarchy used by calibration fingerprints."""

        from .calibration import xinput_hierarchy

        if not isinstance(self.backend, X11Backend):
            return ((0, 0, "test", "test"),)
        output = self.backend._run(
            ["xinput", "--list", "--short"],
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        ).stdout
        try:
            return xinput_hierarchy(output)
        except Exception as exc:
            raise DriverError(str(exc)) from exc

    @staticmethod
    def _runtime_keyboard_profile(
        commands: tuple[str, ...],
        levels: dict[int, tuple[str, ...]],
        keymap: dict[str, int],
        modifiers: tuple[int, int, int],
        modifier_masks: tuple[int, int],
    ) -> tuple[tuple[str, str, tuple[tuple[Any, ...], ...]], ...]:
        reverse: dict[str, tuple[int, str]] = {}
        for keycode, symbols in levels.items():
            for symbol in symbols:
                reverse.setdefault(symbol, (keycode, symbols[0]))

        def detail(symbol: str, fallback: str | None = None) -> int:
            row = reverse.get(symbol) or (reverse.get(fallback) if fallback else None)
            if row is None:
                raise DriverError(f"runtime keymap has no physical key for {symbol!r}")
            return row[0]

        lock, latched, base = modifiers

        def state(base_value: int) -> tuple[tuple[int, int, int], int]:
            triple = (lock, latched, base_value)
            return triple, lock | latched | base_value

        def press_release(symbol: str, keycode: int, current: int) -> list[tuple[Any, ...]]:
            triple, effective = state(current)
            return [
                ("press", symbol, keycode, triple, effective),
                ("release", symbol, keycode, triple, effective),
            ]

        def character_steps(char: str) -> list[tuple[Any, ...]]:
            mapped = X11Backend._keysym_for_char(char)
            if mapped is None:
                raise DriverError(f"runtime command character {char!r} has no XTEST mapping")
            pieces = mapped.split("+")
            if len(pieces) == 1:
                keycode = detail(pieces[0], pieces[0].lower() if pieces[0].isupper() else None)
                return press_release(pieces[0], keycode, base)
            if len(pieces) != 2 or pieces[0] != "Shift_L":
                raise DriverError(f"runtime key mapping {mapped!r} is not a supported modifier form")
            key_symbol = pieces[1]
            shift_code = detail("Shift_L")
            key_code = detail(key_symbol, key_symbol.lower())
            shift_mask = modifier_masks[0]
            shifted, effective = state(base | shift_mask)
            unshifted, unshifted_effective = state(base)
            return [
                # XI2 delivered modifier state describes the state before the
                # modifier key transition: Shift_L press is still unshifted,
                # while its release is still shifted. The following character
                # transitions see the held modifier in both directions.
                ("press", "Shift_L", shift_code, unshifted, unshifted_effective),
                ("press", key_symbol, key_code, shifted, effective),
                ("release", key_symbol, key_code, shifted, effective),
                ("release", "Shift_L", shift_code, shifted, effective),
            ]

        rows: list[tuple[str, str, tuple[tuple[Any, ...], ...]]] = []
        for operation, payload, keys in (
            ("open_chat", "", ("Return",)),
            ("submit_command", "", ("Return",)),
            ("escape_command_buffer", "", ("Escape",)),
        ):
            steps: list[tuple[Any, ...]] = []
            for key in keys:
                steps.extend(press_release(key, detail(key), base))
            rows.append((operation, payload, tuple(steps)))
        for command in sorted(commands):
            steps = []
            for char in command:
                steps.extend(character_steps(char))
            rows.append(("type_allowlisted_command", command, tuple(steps)))
        control_code = detail("Control_L")
        f12_code = detail("F12")
        control_mask = modifier_masks[1]
        controlled, controlled_effective = state(base | control_mask)
        unmodified, unmodified_effective = state(base)
        rows.append(
            (
                "interact_target",
                "",
                (
                    ("press", "Control_L", control_code, unmodified, unmodified_effective),
                    ("press", "F12", f12_code, controlled, controlled_effective),
                    ("release", "F12", f12_code, controlled, controlled_effective),
                    ("release", "Control_L", control_code, controlled, controlled_effective),
                ),
            )
        )
        return tuple(rows)

    def mint_runtime_input_evidence(
        self, identity: WindowIdentity, *, readiness_boot: str, readiness_sequence: int
    ) -> RuntimeInputEvidence:
        if not isinstance(self.backend, X11Backend):
            raise DriverError("passive runtime startup requires the owned X11 backend")
        hierarchy = self.input_hierarchy()
        from .calibration import xtest_sources

        try:
            keyboard_source, pointer_source = xtest_sources(hierarchy)
        except Exception as exc:
            raise DriverError(f"passive startup could not resolve unique XTEST devices: {exc}") from exc
        keyboard_master = next((row[1] for row in hierarchy if row[0] == keyboard_source), 0)
        pointer_master = next((row[1] for row in hierarchy if row[0] == pointer_source), 0)
        if keyboard_master <= 0 or pointer_master <= 0:
            raise DriverError("passive startup XTEST device hierarchy has no measured masters")
        try:
            forwarded_echo_sources = resolve_forwarded_echo_sources(hierarchy, pointer_master)
        except ValueError as exc:
            raise DriverError(
                f"passive startup cannot identify the forwarded XI2 pointer source: {exc}"
            ) from exc
        profile = self.xtest_input_profile(hierarchy)
        if profile is None:
            raise DriverError("passive startup has no measured XTEST pointer profile")
        profile.verify()
        levels = self.backend.observe_keymap_levels()
        modifier_masks = (
            self.backend.observe_keysym_modifier_mask("Shift_L"),
            self.backend.observe_keysym_modifier_mask("Control_L"),
        )
        keymap: dict[str, int] = {}
        for keycode, symbols in levels.items():
            keymap.setdefault(symbols[0], keycode)
        state, held_keys, held_buttons = self.backend.observe_native_input_state(profile.root_xid)
        if held_keys or held_buttons:
            raise DriverError("passive startup found pre-existing held native input")
        from .input_tool import verify_artifact

        tool = verify_artifact(self.backend.input_tool.identity.root)
        environment = StartupEnvironment(
            display=self.profile.display,
            hierarchy=hierarchy,
            keyboard_source=keyboard_source,
            keyboard_master=keyboard_master,
            pointer_source=pointer_source,
            pointer_master=pointer_master,
            forwarded_echo_sources=forwarded_echo_sources,
            xtest_profile=profile,
            keymap_levels=tuple(sorted((code, tuple(symbols)) for code, symbols in levels.items())),
            modifier_masks=(
                ("Shift_L", modifier_masks[0]),
                ("Control_L", modifier_masks[1]),
            ),
            input_tool_version=tool.version,
            input_tool_sha256=tool.executable_sha256,
        )
        commands = tuple(self.COMMANDS)
        keyboard_profile = self._runtime_keyboard_profile(commands, levels, keymap, state, modifier_masks)
        return mint_runtime_evidence(
            readiness_boot=readiness_boot,
            readiness_sequence=readiness_sequence,
            environment=environment,
            identity=identity,
            keyboard_keymap=tuple(sorted(keymap.items())),
            native_modifiers=state,
            native_held_keycodes=held_keys,
            native_held_buttons=held_buttons,
            keyboard_profile=keyboard_profile,
        )

    def xtest_input_profile(
        self,
        hierarchy: tuple[tuple[int, int, str, str], ...],
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> Any:
        if not isinstance(self.backend, X11Backend):
            raise DriverError("XTEST input profile requires the owned X11 backend")
        return self.backend.xtest_input_profile(
            hierarchy,
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        )

    def _verify_current_xtest_profile(
        self,
        evidence: Any,
        hierarchy: tuple[tuple[int, int, str, str], ...] | None = None,
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        if not isinstance(self.backend, X11Backend):
            return
        sealed = getattr(evidence, "xtest_profile", None)
        if sealed is None:
            raise DriverError("native XTEST action has no sealed input profile")
        try:
            sealed.verify()
        except Exception as exc:
            raise DriverError("native XTEST action has an invalid sealed input profile") from exc
        query_kwargs = {
            key: value
            for key, value in (
                ("generation", generation),
                ("deadline_monotonic", deadline_monotonic),
            )
            if value is not None
        }
        if hierarchy is None:
            hierarchy = self.input_hierarchy(**query_kwargs)
        current = self.xtest_input_profile(hierarchy, **query_kwargs)
        if current is None:
            raise DriverError("native XTEST action has no freshly observed input profile")
        try:
            current.verify()
        except Exception as exc:
            raise DriverError("fresh XTEST input profile is invalid") from exc
        if current.canonical_payload() != sealed.canonical_payload():
            raise DriverError("XTEST input profile changed before native pointer dispatch")

    def _external_input(self, event: XI2Event | None) -> None:
        if event is None:
            self.revoke(
                "malformed XI2 event stream",
                invalidation_cause=InvalidationCause.OBSERVER_LOST,
            )
            return
        evidence = self._input_evidence
        callback = self._takeover_input_callback
        if (
            callback is not None
            and isinstance(evidence, RuntimeInputEvidence)
            and is_measured_human_action(event, evidence.environment)
        ):
            # Raw physical key/button records precede their delivered XI2
            # counterpart. Record the first hierarchy-verified physical
            # event before Guardian's normal immediate revoke closes the
            # observer; unknown and synthetic sources still fail closed.
            callback(event)
        try:
            self.guardian.observe(event)
        except Exception as exc:
            self.revoke(f"XI2 guardian failure: {exc}")

    def set_takeover_input_callback(self, callback: Callable[[XI2Event], None] | None) -> None:
        """Observe one actual non-XTEST XI2 input record during the explicit drill."""

        self._takeover_input_callback = callback

    def revoke(
        self,
        reason: str,
        *,
        invalidation_cause: InvalidationCause = InvalidationCause.UNKNOWN,
    ) -> int:
        with self._calibration_action_lock:
            self._queued.clear()
        previous_cause = getattr(self, "_active_revocation_cause", InvalidationCause.UNKNOWN)
        self._active_revocation_cause = (
            invalidation_cause
            if isinstance(invalidation_cause, InvalidationCause)
            else InvalidationCause.UNKNOWN
        )
        try:
            generation = self.guardian.revoke(reason)
        finally:
            self._active_revocation_cause = previous_cause
        self._input_evidence = None
        return generation

    def retain_input_evidence(self, evidence: Any) -> None:
        """Keep completed calibration proof while the controller remains paused.

        Retention never arms the lease.  Resume must explicitly pass this record
        back to :meth:`arm`, after identity and hierarchy checks run again.
        """

        try:
            evidence.verify_seal()
        except Exception as exc:
            raise DriverError("cannot retain unverified input calibration evidence") from exc
        self._retained_input_evidence = evidence

    @property
    def retained_input_evidence(self) -> Any | None:
        return self._retained_input_evidence

    def discard_retained_input_evidence(self) -> None:
        self._retained_input_evidence = None

    @property
    def input_calibration_readiness(self) -> Any | None:
        """Return owned GTK/observer proof while the human sample is pending."""

        return self._input_calibration_readiness

    def notify_input_calibration_ready(self, readiness: Any) -> None:
        if not getattr(readiness, "target_pid", 0) or not getattr(readiness, "target_xid", 0):
            raise DriverError("calibration readiness has no owned target identity")
        self._input_calibration_readiness = readiness

    def clear_input_calibration_ready(self) -> None:
        self._input_calibration_readiness = None

    def queue(self, name: str, generation: int, **kwargs: Any) -> None:
        if name not in self.ACTIONS:
            raise DriverError(f"action {name!r} is not allowlisted")
        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        self._queued.append((generation, name, kwargs))

    def install_measured_anchors(self, anchors: tuple[MeasuredAnchor, ...] | list[MeasuredAnchor]) -> None:
        """Validate a complete batch only for isolated fixture tooling.

        Live X11 dispatch measures and admits exactly one scene-local control
        immediately before use; the old all-scenes install path is rejected.
        """

        if isinstance(self.backend, X11Backend):
            raise DriverError("live X11 dispatch requires measure_pointer_anchor(action)")

        required = set(self.POINTER_ACTIONS)
        observed = {anchor.action for anchor in anchors}
        if observed != required or len(anchors) != len(required):
            raise DriverError("measured anchor set must cover every pointer action exactly once")
        identity = self.resolve_identity()
        now = self._clock()
        for anchor in anchors:
            self._validate_measured_anchor(anchor, identity)
            if anchor.source not in {"native-visual", "native-addon"}:
                raise DriverError("anchor is not native visual evidence")
            facts = dict(anchor.facts)
            required_control = self.ANCHOR_CONTROLS.get(anchor.action)
            required_scene = self.ANCHOR_SCENES[anchor.action]
            if (
                facts.get("control") != required_control
                or facts.get("scene") != required_scene
                or facts.get("capture") != anchor.capture_id
                or facts.get("target") != str(anchor.target_xid)
            ):
                raise DriverError("anchor lacks bound control/scene/capture proof")
            if anchor.identity_fingerprint != identity.fingerprint:
                raise DriverError("anchor identity is stale")
            if anchor.target_xid <= 0 or anchor.capture_sequence <= 0 or not anchor.capture_id:
                raise DriverError("anchor capture identity is incomplete")
            if now - anchor.capture_monotonic < -0.25 or now - anchor.capture_monotonic > 5.0:
                raise DriverError("anchor evidence is stale")
            if (
                isinstance(anchor.x, bool)
                or isinstance(anchor.y, bool)
                or not isinstance(anchor.x, int)
                or not isinstance(anchor.y, int)
            ):
                raise DriverError("anchor coordinates are not measured integers")
            if isinstance(self.backend, X11Backend):
                if not self._is_owned_client_target(anchor.target_xid, identity):
                    raise DriverError("anchor target is outside the verified client")
            elif anchor.target_xid != identity.child_xid:
                raise DriverError("anchor target does not match the client child")
            if not (
                0 <= anchor.x < identity.child_geometry[2] and 0 <= anchor.y < identity.child_geometry[3]
            ):
                raise DriverError("anchor is outside the verified client child")
        if isinstance(self.backend, X11Backend):
            for anchor in anchors:
                self.backend.install_measured_anchor(anchor)

    def measure_pointer_anchor(
        self,
        action: str,
        *,
        generation: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> MeasuredAnchor:
        """Measure one control from a fresh owned-client scene capture."""

        if not self._is_pointer_action(action):
            raise DriverError(f"{action} is not a measured pointer action")
        provider = self.measured_anchor_provider
        if provider is None:
            raise DriverError("native control-specific anchor provider is not configured")
        if generation is None:
            raise DriverError("native control anchor admission requires a current generation")
        deadline = self._effective_anchor_observation_deadline(deadline_monotonic)
        self._check_anchor_observation_deadline(
            deadline, "native control anchor deadline expired before identity"
        )
        identity = self.resolve_identity(deadline_monotonic=deadline)
        self._check_anchor_observation_deadline(
            deadline, "native control anchor deadline expired before observation"
        )
        try:
            with self._anchor_observation_scope(deadline):
                self._check_anchor_observation_deadline(
                    deadline, "native control anchor deadline expired before observation"
                )
                anchor = provider(identity, action)
                self._check_anchor_observation_deadline(
                    deadline, "native control anchor deadline expired after observation"
                )
        except Exception as exc:
            raise DriverError(f"native anchor measurement failed for {action}: {exc}") from exc
        if not isinstance(anchor, MeasuredAnchor):
            raise DriverError("native anchor provider returned unbound coordinate evidence")
        if anchor.generation != generation:
            raise DriverError("native anchor belongs to a different generation")
        self._validate_measured_anchor(anchor, identity)
        if anchor.evidence_hash in self._consumed_anchor_seals:
            raise DriverError("native control anchor has already been consumed")
        if isinstance(self.backend, X11Backend):
            try:
                self.guardian.check(generation)
            except GuardianError as exc:
                raise DriverError(str(exc)) from exc
        if isinstance(self.backend, X11Backend):
            self.backend.install_measured_anchor(anchor)
        self._measured_anchors[action] = anchor
        if generation is not None:
            self._measured_anchor_generations[action] = generation
        return anchor

    def _refresh_pointer_anchor(
        self,
        action: str,
        generation: int,
        identity: WindowIdentity,
        previous: MeasuredAnchor,
        *,
        deadline_monotonic: float | None = None,
    ) -> MeasuredAnchor:
        """Re-observe the same control while its Guardian action is active."""

        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        if deadline_monotonic is None:
            deadline_monotonic = getattr(self.backend, "_active_action_deadline", None)
        deadline = self._effective_anchor_observation_deadline(deadline_monotonic)
        self._check_anchor_observation_deadline(
            deadline, "fresh pointer anchor deadline expired before identity"
        )
        current = self.resolve_identity(require_foreground=True, deadline_monotonic=deadline)
        self._check_anchor_observation_deadline(
            deadline, "fresh pointer anchor deadline expired before observation"
        )
        if current.fingerprint != identity.fingerprint:
            raise DriverError("pointer identity changed before fresh control observation")
        provider = self.measured_anchor_provider
        if not callable(provider):
            raise DriverError("fresh pointer control-anchor provider is not configured")
        try:
            with self._anchor_observation_scope(deadline):
                self._check_anchor_observation_deadline(
                    deadline, "fresh pointer control-anchor observation deadline expired"
                )
                anchor = provider(current, action)
                self._check_anchor_observation_deadline(
                    deadline, "fresh pointer control-anchor observation deadline expired"
                )
        except Exception as exc:
            raise DriverError(f"fresh pointer control-anchor observation failed: {exc}") from exc
        if not isinstance(anchor, MeasuredAnchor):
            raise DriverError("fresh pointer anchor provider returned unbound evidence")
        if anchor.generation != generation:
            raise DriverError("fresh pointer anchor belongs to a stale generation")
        self._validate_measured_anchor(anchor, current)
        if anchor.capture_sequence <= previous.capture_sequence:
            raise DriverError("fresh pointer anchor did not advance its capture sequence")
        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        return anchor

    @staticmethod
    def _fresh_anchor_contains_endpoint(
        refreshed: MeasuredAnchor,
        endpoint: Point,
        client_geometry: tuple[int, int, int, int],
        previous_bounds: tuple[int, int, int, int],
    ) -> bool:
        """Accept model jitter while rejecting a real target displacement."""

        try:
            client = Rect(0, 0, client_geometry[2], client_geometry[3])
            fresh_bounds = Rect(*refreshed.bounds)
            previous = Rect(*previous_bounds)
        except (TypeError, ValueError, ExecutorError):
            return False
        if not client.encloses(fresh_bounds):
            return False
        padding = 1 if min(fresh_bounds.width, fresh_bounds.height) >= 3 else 0
        safe = Rect(
            fresh_bounds.x + padding,
            fresh_bounds.y + padding,
            fresh_bounds.width - 2 * padding,
            fresh_bounds.height - 2 * padding,
        )
        if not safe.contains(endpoint):
            return False
        size = max(1.0, min(previous.width, previous.height, fresh_bounds.width, fresh_bounds.height))
        limit = max(2.0, size * 0.35)
        return (
            abs(previous.x - fresh_bounds.x) <= limit
            and abs(previous.y - fresh_bounds.y) <= limit
            and abs(previous.width - fresh_bounds.width) <= limit
            and abs(previous.height - fresh_bounds.height) <= limit
        )

    def build_measured_anchor(
        self,
        *,
        action: str,
        x: int,
        y: int,
        capture_path: Path,
        capture_id: str,
        capture_sequence: int,
        capture_monotonic: float,
        scene: str,
        bounds: tuple[int, int, int, int],
        target_xid: int,
        target_ancestry: tuple[int, ...],
        generation: int,
        source: str = "native-visual",
        facts: tuple[tuple[str, str], ...] = (),
        deadline_monotonic: float | None = None,
    ) -> MeasuredAnchor:
        """Create an anchor sealed to one retained native capture.

        The Luna visual producer supplies the measured point and scene facts;
        this method supplies the actual file hash and canonical admission seal
        so a coordinate cannot be detached from the capture that produced it.
        """

        deadline = self._effective_anchor_observation_deadline(deadline_monotonic)
        self._check_anchor_observation_deadline(deadline, "native anchor deadline expired before identity")
        if deadline is None:
            identity = self.resolve_identity(require_foreground=False)
        else:
            identity = self.resolve_identity(require_foreground=False, deadline_monotonic=deadline)
        self._check_anchor_observation_deadline(deadline, "native anchor deadline expired after identity")
        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        if (
            isinstance(capture_monotonic, bool)
            or not isinstance(capture_monotonic, (int, float))
            or not math.isfinite(float(capture_monotonic))
            or capture_monotonic < 0
        ):
            raise DriverError("native anchor original capture time is malformed")
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired before capture path"
        )
        capture_path = capture_path.expanduser().resolve()
        self._check_anchor_observation_deadline(deadline, "native anchor deadline expired after capture path")
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired before capture existence"
        )
        if not capture_path.is_file():
            raise DriverError("native anchor capture does not exist")
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired after capture existence"
        )
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired before capture hash"
        )
        digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        self._check_anchor_observation_deadline(deadline, "native anchor deadline expired after capture hash")
        normalized_facts = dict(facts)
        normalized_facts.update(
            {
                "action": action,
                "bounds": ",".join(str(value) for value in bounds),
                "capture": capture_id,
                "capturePath": str(capture_path),
                "captureSha256": digest,
                "captureMonotonic": str(capture_monotonic),
                "control": self.ANCHOR_CONTROLS.get(action, ""),
                "generation": str(generation),
                "scene": scene,
                "target": str(target_xid),
                "targetAncestry": ",".join(str(value) for value in target_ancestry),
            }
        )
        anchor = MeasuredAnchor.from_capture(
            action=action,
            x=x,
            y=y,
            identity_fingerprint=identity.fingerprint,
            target_xid=target_xid,
            capture_id=capture_id,
            capture_sequence=capture_sequence,
            source=source,
            observed_monotonic=capture_monotonic,
            capture_monotonic=capture_monotonic,
            scene=scene,
            bounds=bounds,
            target_ancestry=target_ancestry,
            capture_path=str(capture_path),
            capture_sha256=digest,
            generation=generation,
            facts=tuple(sorted(normalized_facts.items())),
        )
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired before anchor validation"
        )
        self._validate_measured_anchor(anchor, identity)
        self._check_anchor_observation_deadline(
            deadline, "native anchor deadline expired after anchor validation"
        )
        return anchor

    def _validate_measured_anchor(self, anchor: MeasuredAnchor, identity: WindowIdentity) -> None:
        anchor.verify_seal()
        if not self._is_pointer_action(anchor.action):
            raise DriverError("anchor action is not allowlisted")
        if anchor.source not in {"native-visual", "native-addon"}:
            raise DriverError("anchor is not native visual evidence")
        facts = dict(anchor.facts)
        if len(facts) != len(anchor.facts):
            raise DriverError("anchor facts contain duplicate keys")
        expected_scene = self.ANCHOR_SCENES[anchor.action]
        if (
            facts.get("control") != self.ANCHOR_CONTROLS[anchor.action]
            or facts.get("capture") != anchor.capture_id
            or facts.get("target") != str(anchor.target_xid)
            or facts.get("scene") != expected_scene
            or anchor.scene != expected_scene
        ):
            raise DriverError("anchor lacks bound control/scene/capture proof")
        if anchor.identity_fingerprint != identity.fingerprint:
            raise DriverError("anchor identity is stale")
        if (
            anchor.target_xid <= 0
            or anchor.capture_sequence <= 0
            or not anchor.capture_id
            or not anchor.capture_path
            or not Path(anchor.capture_path).is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", anchor.capture_sha256)
            or anchor.generation is None
            or not anchor.target_ancestry
            or anchor.target_xid not in anchor.target_ancestry
            or identity.child_xid not in anchor.target_ancestry
            or len(anchor.bounds) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in anchor.bounds)
            or anchor.bounds[2] <= 0
            or anchor.bounds[3] <= 0
            or not facts.get("bounds")
            or facts.get("bounds") != ",".join(str(value) for value in anchor.bounds)
            or facts.get("targetAncestry") != ",".join(str(value) for value in anchor.target_ancestry)
            or facts.get("captureSha256") != anchor.capture_sha256
            or facts.get("captureMonotonic") != str(anchor.capture_monotonic)
            or facts.get("capturePath") != anchor.capture_path
            or facts.get("generation") != str(anchor.generation)
            or facts.get("action") != anchor.action
        ):
            raise DriverError("anchor capture identity is incomplete")
        if (
            isinstance(anchor.observed_monotonic, bool)
            or not isinstance(anchor.observed_monotonic, (int, float))
            or not math.isfinite(float(anchor.observed_monotonic))
            or isinstance(anchor.capture_monotonic, bool)
            or not isinstance(anchor.capture_monotonic, (int, float))
            or not math.isfinite(float(anchor.capture_monotonic))
            or anchor.capture_monotonic < 0
        ):
            raise DriverError("anchor observation timestamp is malformed")
        now = self._clock()
        if now - anchor.capture_monotonic < -0.25 or now - anchor.capture_monotonic > 5.0:
            raise DriverError("anchor evidence is stale")
        if (
            isinstance(anchor.x, bool)
            or isinstance(anchor.y, bool)
            or not isinstance(anchor.x, int)
            or not isinstance(anchor.y, int)
        ):
            raise DriverError("anchor coordinates are not measured integers")
        if isinstance(self.backend, X11Backend):
            if not self._is_owned_client_target(anchor.target_xid, identity):
                raise DriverError("anchor target is outside the verified client")
            if any(not self._is_owned_client_target(xid, identity) for xid in anchor.target_ancestry):
                raise DriverError("anchor target ancestry is outside the verified client")
        elif anchor.target_xid != identity.child_xid:
            raise DriverError("anchor target does not match the client child")
        left, top, width, height = anchor.bounds
        if not (
            0 <= left < identity.child_geometry[2]
            and 0 <= top < identity.child_geometry[3]
            and left + width <= identity.child_geometry[2]
            and top + height <= identity.child_geometry[3]
            and left <= anchor.x < left + width
            and top <= anchor.y < top + height
        ):
            raise DriverError("anchor is outside the verified client child")
        try:
            actual_hash = hashlib.sha256(Path(anchor.capture_path).read_bytes()).hexdigest()
        except OSError as exc:
            raise DriverError("native anchor retained capture is unavailable") from exc
        if actual_hash != anchor.capture_sha256:
            raise DriverError("native anchor retained capture changed")

    def measure_pointer_anchors(self) -> tuple[MeasuredAnchor, ...]:
        """Reject the obsolete all-scenes anchor batch API.

        Controls are mutually exclusive across scenes.  Callers must invoke
        :meth:`measure_pointer_anchor` immediately before the named action.
        """

        raise DriverError("scene-local controls require measure_pointer_anchor(action)")

    def queue_delayed_calibration_action(
        self,
        target: Any,
        *,
        delay: float,
        calibration_ticket: CalibrationTicket | None = None,
    ) -> CalibrationTicket:
        from .calibration import OwnedGtkCalibrationWindow, OwnedWindow

        if (
            not isinstance(target, OwnedWindow)
            or target.title != OwnedGtkCalibrationWindow.TITLE
            or target.xid <= 0
            or delay <= 0
        ):
            raise DriverError("calibration delayed action target/deadline is malformed")
        if not isinstance(calibration_ticket, CalibrationTicket):
            raise DriverError("delayed calibration action requires an exact calibration ticket")
        ticket = calibration_ticket
        self._verify_calibration_target(ticket, target)
        if not self.guardian.calibration_ticket_current(ticket):
            raise DriverError("calibration ticket is stale or revoked")
        with self._calibration_action_lock:
            self._queued.append((ticket.epoch, "calibration_focus", {"target": target, "ticket": ticket}))
        timer = threading.Timer(delay, self._run_delayed_calibration_action, args=(ticket, target))
        timer.daemon = True
        self._calibration_timers[id(timer)] = timer
        self._calibration_due_deadline = self._clock() + delay
        with self._calibration_action_lock:
            self._calibration_due_waiters[id(timer)] = threading.Event()
        if not self.guardian.calibration_ticket_current(ticket):
            with self._calibration_action_lock:
                self._queued = [entry for entry in self._queued if entry[2].get("ticket") is not ticket]
                self._calibration_due_waiters.pop(id(timer), None)
            raise DriverError("calibration ticket was revoked before timer start")
        timer.start()
        return ticket

    def focus_calibration_target(self, target: Any, *, calibration_ticket: CalibrationTicket) -> None:
        """Focus the owned GTK target under its exact pre-arm capability."""

        if not isinstance(calibration_ticket, CalibrationTicket):
            raise DriverError("calibration focus requires an exact calibration ticket")
        self._verify_calibration_target(calibration_ticket, target)
        if not self.guardian.calibration_ticket_current(calibration_ticket):
            raise DriverError("calibration ticket is stale or revoked")
        if isinstance(self.backend, X11Backend):
            self.backend.calibration_focus(
                hex(calibration_ticket.target_xid), calibration_ticket=calibration_ticket
            )
            return
        callback = getattr(self.backend, "calibration_focus", None)
        if not callable(callback):
            raise DriverError("fake calibration backend has no owned focus action")
        try:
            self.guardian.calibration_dispatch_admission(
                calibration_ticket, lambda: callback(calibration_ticket.target_xid)
            )
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc

    def _run_delayed_calibration_action(
        self,
        ticket: CalibrationTicket,
        target: Any,
    ) -> None:
        current = threading.current_thread()
        self._calibration_timers.pop(id(current), None)
        with self._calibration_action_lock:
            self._calibration_due_count += 1
            waiter = self._calibration_due_waiters.pop(id(current), None)
            entry_present = any(
                entry[0] == ticket.epoch
                and entry[1] == "calibration_focus"
                and entry[2].get("ticket") == ticket
                for entry in self._queued
            )
            admissible = (
                self._calibration_ticket is ticket
                and entry_present
                and self.guardian.calibration_ticket_current(ticket)
            )
            if not admissible:
                self._calibration_rejected_count += 1
                if waiter is not None:
                    waiter.set()
                return
        if waiter is not None:
            waiter.set()
        # The generation check above is deliberately outside the blocking
        # focus operation.  X11Backend's generation admission repeats it at
        # the actual Popen/registration boundary, so revoke never waits on a
        # calibration lock or permits a stale process to start.
        poll = getattr(target.process, "poll", None)
        if callable(poll) and poll() is not None:
            with self._calibration_action_lock:
                self._queued = [entry for entry in self._queued if entry[0] != ticket.epoch]
            return
        try:
            self._verify_calibration_target(ticket, target)
            if isinstance(self.backend, X11Backend):
                self.backend.calibration_focus(hex(target.xid), calibration_ticket=ticket)
            else:
                callback = getattr(self.backend, "calibration_focus", None)
                if not callable(callback):
                    raise DriverError("fake calibration backend has no owned focus action")
                admission = self._calibration_dispatch_admission
                if callable(admission):
                    admission(ticket, lambda: callback(target.xid))
                else:
                    callback(target.xid)
            with self._calibration_action_lock:
                if self._calibration_ticket is not ticket or not self.guardian.calibration_ticket_current(
                    ticket
                ):
                    self._calibration_rejected_count += 1
                    return
                self._calibration_executions += 1
                self._queued = [entry for entry in self._queued if entry[0] != ticket.epoch]
        except GuardianError:
            with self._calibration_action_lock:
                self._calibration_rejected_count += 1
        except DriverError as exc:
            with self._calibration_action_lock:
                self._calibration_rejected_count += 1
            if self._calibration_ticket == ticket:
                self.revoke(f"owned calibration action failed: {exc}")
        except Exception:
            self.revoke("owned calibration action failed")

    @property
    def calibration_executions(self) -> int:
        return self._calibration_executions

    @property
    def calibration_due_count(self) -> int:
        return self._calibration_due_count

    @property
    def calibration_rejected_count(self) -> int:
        return self._calibration_rejected_count

    @property
    def calibration_due_deadline(self) -> float | None:
        return self._calibration_due_deadline

    @staticmethod
    def _validate_deadline(deadline_monotonic: float | None) -> float | None:
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise DriverError("operation deadline is malformed")
        return None if deadline_monotonic is None else float(deadline_monotonic)

    @property
    def anchor_observation_deadline(self) -> float | None:
        """Return the inherited bound for the active measured-anchor provider."""

        return self._anchor_observation_deadline_var.get()

    def _effective_anchor_observation_deadline(self, deadline_monotonic: float | None) -> float | None:
        requested = self._validate_deadline(deadline_monotonic)
        active = self.anchor_observation_deadline
        if active is None:
            return requested
        if requested is None:
            return active
        return min(active, requested)

    @contextmanager
    def _anchor_observation_scope(self, deadline_monotonic: float | None):
        deadline = self._effective_anchor_observation_deadline(deadline_monotonic)
        token = self._anchor_observation_deadline_var.set(deadline)
        try:
            yield deadline
        finally:
            self._anchor_observation_deadline_var.reset(token)

    def _check_anchor_observation_deadline(self, deadline_monotonic: float | None, message: str) -> None:
        if deadline_monotonic is not None and self._clock() >= deadline_monotonic:
            raise DriverError(message)

    @staticmethod
    def _check_deadline(deadline_monotonic: float | None, message: str) -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError(message)

    def _identity(
        self,
        *,
        require_foreground: bool = True,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        if isinstance(self.backend, X11Backend):
            if deadline_monotonic is None:
                identity = self.backend.resolve_identity(require_foreground=require_foreground)
            else:
                identity = self.backend.resolve_identity(
                    require_foreground=require_foreground,
                    deadline_monotonic=deadline_monotonic,
                )
        else:
            identity = getattr(self.backend, "identity", None)
            if identity is None:
                identity = WindowIdentity(
                    1,
                    os.getpid(),
                    "test",
                    self.profile.window_title,
                    self.profile.window_class,
                    2,
                    (0, 0, self.profile.outer_width, self.profile.outer_height),
                    (0, 0, self.profile.client_width, self.profile.client_height),
                    1,
                    "test",
                    self.profile.display,
                    "test-identity",
                )
        self._last_identity = identity
        return identity

    def resolve_identity(
        self,
        *,
        require_foreground: bool = True,
        deadline_monotonic: float | None = None,
    ) -> WindowIdentity:
        """Return fresh identity evidence for the configured client."""

        return self._identity(
            require_foreground=require_foreground,
            deadline_monotonic=deadline_monotonic,
        )

    def verify_identity(self, identity: WindowIdentity) -> WindowIdentity:
        if isinstance(self.backend, X11Backend):
            return self.backend.verify_identity(identity)
        fresh = self._identity()
        if fresh.fingerprint != identity.fingerprint:
            raise DriverError("verified client identity is stale")
        return fresh

    def _is_owned_client_target(self, xid: int, identity: WindowIdentity) -> bool:
        """Accept a measured client root or a structurally owned descendant."""

        if not isinstance(self.backend, X11Backend):
            return xid == identity.child_xid
        return self.backend._is_owned_client_target(xid, identity)

    @staticmethod
    def _proof_events(evidence: Any) -> list[tuple[Any, ...]]:
        records = getattr(evidence, "event_proof", ())
        if not records:
            raise DriverError("input calibration has no complete event proof")
        return [tuple(record) for record in records]

    @staticmethod
    def _keyboard_profile(evidence: Any) -> dict[tuple[str, str], tuple[tuple[Any, ...], ...]]:
        """Return the immutable, sealed operation facts from input calibration."""

        raw_profile = getattr(evidence, "keyboard_profile", ())
        if not isinstance(raw_profile, (tuple, list)) or not raw_profile:
            raise DriverError("input calibration has no sealed keyboard operation profile")
        profile: dict[tuple[str, str], tuple[tuple[Any, ...], ...]] = {}
        for row in raw_profile:
            if not isinstance(row, (tuple, list)) or len(row) != 3:
                raise DriverError("input calibration keyboard operation profile is malformed")
            operation, payload, raw_steps = row
            if (
                not isinstance(operation, str)
                or not operation
                or not isinstance(payload, str)
                or not isinstance(raw_steps, (tuple, list))
                or not raw_steps
                or (operation, payload) in profile
            ):
                raise DriverError("input calibration keyboard operation profile is malformed")
            steps: list[tuple[Any, ...]] = []
            for raw_step in raw_steps:
                if (
                    not isinstance(raw_step, (tuple, list))
                    or len(raw_step) != 5
                    or raw_step[0] not in {"press", "release"}
                    or not isinstance(raw_step[1], str)
                    or not raw_step[1]
                    or isinstance(raw_step[2], bool)
                    or not isinstance(raw_step[2], int)
                    or raw_step[2] <= 0
                    or not isinstance(raw_step[3], (tuple, list))
                    or len(raw_step[3]) != 3
                    or any(
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                        for value in raw_step[3]
                    )
                    or isinstance(raw_step[4], bool)
                    or not isinstance(raw_step[4], int)
                    or raw_step[4] < 0
                    or raw_step[4] != raw_step[3][0] | raw_step[3][1] | raw_step[3][2]
                ):
                    raise DriverError("input calibration keyboard transition is malformed")
                steps.append((raw_step[0], raw_step[1], raw_step[2], tuple(raw_step[3]), raw_step[4]))
            profile[(operation, payload)] = tuple(steps)
        return profile

    def _sealed_keyboard_keymap(
        self, evidence: Any, *, deadline_monotonic: float | None = None
    ) -> dict[str, int]:
        raw_keymap = getattr(evidence, "keyboard_keymap", ())
        if not isinstance(raw_keymap, (tuple, list)) or not raw_keymap:
            raise DriverError("input calibration has no sealed keyboard keymap")
        sealed: dict[str, int] = {}
        for row in raw_keymap:
            if (
                not isinstance(row, (tuple, list))
                or len(row) != 2
                or not isinstance(row[0], str)
                or not row[0]
                or isinstance(row[1], bool)
                or not isinstance(row[1], int)
                or row[1] <= 0
                or row[0] in sealed
            ):
                raise DriverError("input calibration keyboard keymap is malformed")
            sealed[row[0]] = row[1]
        if tuple(sorted(sealed.items())) != tuple(raw_keymap):
            raise DriverError("input calibration keyboard keymap is not canonical")
        if isinstance(self.backend, X11Backend):
            observed = (
                self.backend.observe_keymap()
                if deadline_monotonic is None
                else self.backend.observe_keymap(deadline_monotonic=deadline_monotonic)
            )
            for keysym, detail in sealed.items():
                base = keysym.rsplit("+", 1)[-1]
                if observed.get(keysym, observed.get(base)) != detail:
                    raise DriverError("owned backend keyboard keymap changed after calibration")
        return sealed

    def _keyboard_operation_facts(
        self,
        operation: str,
        payload: str,
        evidence: Any,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[tuple[Any, ...], ...]:
        profile = self._keyboard_profile(evidence)
        self._sealed_keyboard_keymap(evidence, deadline_monotonic=deadline_monotonic)
        try:
            return profile[(operation, payload)]
        except KeyError as exc:
            raise DriverError(
                f"input calibration has no sealed keyboard operation {operation!r}/{payload!r}"
            ) from exc

    @staticmethod
    def _keyboard_sequence(
        steps: tuple[tuple[Any, ...], ...],
    ) -> tuple[tuple[str, ...], tuple[int, ...], tuple[tuple[int, int, int], ...]]:
        event_classes: list[str] = []
        details: list[int] = []
        modifiers: list[tuple[int, int, int]] = []
        for phase, _keysym, detail, modifier_state, _effective in steps:
            if phase == "press":
                event_classes.extend((EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value))
            else:
                event_classes.extend((EventClass.RAW_KEY_RELEASE.value, EventClass.KEY_RELEASE.value))
            details.extend((detail, detail))
            modifiers.append(modifier_state)
        # Guardian correlates one raw/delivered press or release pair at a
        # time.  Details therefore have two entries for each physical step.
        return tuple(event_classes), tuple(details), tuple(modifiers)

    def _calibration_keyboard_operations(self) -> tuple[Any, ...]:
        from .calibration import KeyboardProbeOperation, KeyboardProbeStep

        def pair(keysym: str) -> tuple[Any, Any]:
            return (
                KeyboardProbeStep("press", keysym),
                KeyboardProbeStep("release", keysym),
            )

        operations: list[Any] = [
            KeyboardProbeOperation("open_chat", "", pair("Return")),
            KeyboardProbeOperation("submit_command", "", pair("Return")),
            KeyboardProbeOperation("escape_command_buffer", "", pair("Escape")),
            KeyboardProbeOperation(
                "interact_target",
                "",
                (
                    KeyboardProbeStep("press", "Control_L"),
                    KeyboardProbeStep("press", "F12"),
                    KeyboardProbeStep("release", "F12"),
                    KeyboardProbeStep("release", "Control_L"),
                ),
            ),
        ]
        for command in sorted(self.COMMANDS):
            steps: list[Any] = []
            for char in command:
                keysym = X11Backend._keysym_for_char(char)
                if keysym is None:
                    raise DriverError(f"character {char!r} has no allowlisted XTEST keysym")
                components = tuple(part for part in keysym.split("+") if part)
                if len(components) == 1:
                    steps.extend(pair(components[0]))
                else:
                    # xdotool emits modifier key transitions as well as the
                    # character transition. Keep those physical transitions
                    # explicit so XI2 rehearsal and runtime Guardian expect
                    # the same ordered stream for uppercase and shifted punctuation.
                    steps.extend(
                        (
                            KeyboardProbeStep("press", components[0]),
                            KeyboardProbeStep("press", components[1]),
                            KeyboardProbeStep("release", components[1]),
                            KeyboardProbeStep("release", components[0]),
                        )
                    )
            operations.append(KeyboardProbeOperation("type_allowlisted_command", command, tuple(steps)))
        return tuple(operations)

    def calibration_keyboard_operations(self) -> tuple[Any, ...]:
        """Return the finite, runtime-owned keyboard emission vocabulary."""

        return self._calibration_keyboard_operations()

    def calibration_keyboard_keymap(self) -> dict[str, int]:
        """Measure the actual keymap used by the owned XTEST backend."""

        if isinstance(self.backend, X11Backend):
            return self.backend.observe_keymap()
        provider = getattr(self.backend, "observe_keymap", None)
        if not callable(provider):
            raise DriverError("owned keyboard backend has no keymap observation provider")
        mapping = provider()
        if not isinstance(mapping, dict) or not mapping:
            raise DriverError("owned keyboard backend returned no keymap")
        return dict(mapping)

    def _expected_modifiers(self, evidence: Any) -> tuple[int, int, int]:
        if isinstance(evidence, RuntimeInputEvidence):
            evidence.verify_seal()
            return evidence.native_modifiers
        sealed = getattr(evidence, "synthetic_modifiers", ())
        if (
            not isinstance(sealed, (tuple, list))
            or len(sealed) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in sealed)
        ):
            raise DriverError("input calibration lacks sealed synthetic modifier state")
        expected = tuple(sealed)
        synthetic_sources = {
            getattr(evidence, "synthetic_key_source", 0),
            getattr(evidence, "synthetic_pointer_source", 0),
        }
        delivered_records = [
            record
            for record in self._proof_events(evidence)
            if len(record) >= 12
            and record[0]
            in {
                EventClass.KEY_PRESS.value,
                EventClass.KEY_RELEASE.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.BUTTON_RELEASE.value,
            }
            and len(record) >= 3
            and record[2] in synthetic_sources
        ]
        key_delivered = [
            (record[10], record[11])
            for record in delivered_records
            if record[2] == getattr(evidence, "synthetic_key_source", 0)
        ]
        # The first F13 key pair and the complete pointer click are the stable
        # baseline facts.  Later keyboard profile rows intentionally carry
        # class-specific modifier states (for example Ctrl+A), so they must be
        # checked against their sealed rows rather than this baseline tuple.
        baseline = key_delivered[:2] + [
            (record[10], record[11])
            for record in delivered_records
            if record[2] == getattr(evidence, "synthetic_pointer_source", 0)
        ]
        if not baseline or any(modifiers is None or effective is None for modifiers, effective in baseline):
            raise DriverError("input calibration lacks delivered modifier records")
        if any(
            not isinstance(modifiers, (tuple, list))
            or len(modifiers) != 3
            or any(isinstance(component, bool) or not isinstance(component, int) for component in modifiers)
            or tuple(modifiers) != expected
            or isinstance(effective, bool)
            or not isinstance(effective, int)
            or effective != modifiers[0] | modifiers[1] | modifiers[2]
            for modifiers, effective in baseline
        ):
            raise DriverError("input calibration modifier record is malformed")
        return expected  # type: ignore[return-value]

    def _expected_key_details(self, text: str, evidence: Any) -> tuple[int, ...]:
        _event_classes, details, _modifiers = self._keyboard_sequence(
            self._keyboard_operation_facts("type_allowlisted_command", text, evidence)
        )
        return details

    def _expected_named_key_details(self, keysym: str, evidence: Any) -> tuple[int, ...]:
        operation = {
            "Return": "open_chat",
            "Escape": "escape_command_buffer",
        }.get(keysym)
        if operation is None:
            raise DriverError(f"named keysym {keysym!r} is not an owned runtime operation")
        _event_classes, details, _modifiers = self._keyboard_sequence(
            self._keyboard_operation_facts(operation, "", evidence)
        )
        return details

    def _expected_key_operation(
        self,
        name: str,
        kwargs: dict[str, Any],
        evidence: Any,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[tuple[str, ...], tuple[int, ...], tuple[tuple[int, int, int], ...]]:
        if name == "type_allowlisted_command":
            operation, payload = name, kwargs.get("command")
        elif name == "open_chat":
            operation, payload = "open_chat", ""
        elif name == "submit_command":
            operation, payload = "submit_command", ""
        elif name == "escape_command_buffer":
            operation, payload = "escape_command_buffer", ""
        elif name == "open_ah":
            operation, payload = "interact_target", ""
        else:
            return (), (), ()
        if not isinstance(payload, str):
            raise DriverError("keyboard action payload is malformed")
        return self._keyboard_sequence(
            self._keyboard_operation_facts(
                operation,
                payload,
                evidence,
                deadline_monotonic=deadline_monotonic,
            )
        )

    def _motion_input_context(
        self,
        generation: int,
        *,
        pointer: bool,
        deadline_monotonic: float | None = None,
    ) -> tuple[WindowIdentity, Any, int, int, tuple[int, int, int], dict[str, int]]:
        """Return the current identity and sealed native input contract."""

        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        if not isinstance(self.backend, X11Backend):
            raise DriverError("bounded native motion requires the X11 backend")
        evidence = self._input_evidence
        if evidence is None:
            raise DriverError("motion requires owned input calibration evidence")
        source = getattr(evidence, "synthetic_pointer_source" if pointer else "synthetic_key_source", 0)
        if isinstance(source, bool) or not isinstance(source, int) or source <= 0:
            raise DriverError("motion input source is not calibrated")
        master = next(
            (
                parent
                for device, parent, _role, _name in getattr(evidence, "xinput_hierarchy", ())
                if device == source
            ),
            0,
        )
        if isinstance(master, bool) or not isinstance(master, int) or master <= 0:
            raise DriverError("motion input master is not calibrated")
        identity = self.resolve_identity(
            require_foreground=True,
            deadline_monotonic=deadline_monotonic,
        )
        keymap = (
            {} if pointer else self._sealed_keyboard_keymap(evidence, deadline_monotonic=deadline_monotonic)
        )
        return identity, evidence, source, master, self._expected_modifiers(evidence), keymap

    def run_motion_key_hold(
        self,
        role: KeyRole,
        duration: float,
        frame: Any,
        *,
        generation: int,
        deadline_monotonic: float | None = None,
    ) -> None:
        """Dispatch one measured-binding key pulse under one Guardian action."""

        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise DriverError("motion key deadline is malformed")
        if not isinstance(role, KeyRole):
            raise DriverError("motion key role is not a closed role")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
        ):
            raise DriverError("motion key duration is malformed")
        duration = float(duration)
        if not 0.01 <= duration <= 0.35:
            raise DriverError("motion key duration exceeds the bounded pulse limit")
        identity, evidence, source, master, modifiers, keymap = self._motion_input_context(
            generation, pointer=False, deadline_monotonic=deadline_monotonic
        )
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("motion key deadline expired before admission")
        native_fields = {
            KeyRole.MOVE_FORWARD: "moveForward",
            KeyRole.MOVE_BACKWARD: "moveBackward",
            KeyRole.STRAFE_LEFT: "strafeLeft",
            KeyRole.STRAFE_RIGHT: "strafeRight",
        }
        field = native_fields.get(role)
        if field is None:
            raise DriverError("motion key role is not supported by the native schema")
        binding = frame.binding(field)
        if not isinstance(binding, str) or len(binding) != 1 or binding.upper() not in {"W", "S", "A", "D"}:
            raise DriverError("native motion binding is not an approved unmodified movement key")
        keysym = binding.lower()
        keycode = keymap.get(keysym)
        if isinstance(keycode, bool) or not isinstance(keycode, int) or keycode <= 0:
            raise DriverError("native motion binding is absent from the calibrated keymap")
        if isinstance(self.backend, X11Backend):
            levels = (
                self.backend.observe_keymap_levels()
                if deadline_monotonic is None
                else self.backend.observe_keymap_levels(deadline_monotonic=deadline_monotonic)
            ).get(keycode)
            if not levels or levels[0] != keysym:
                raise DriverError("native motion binding is not the sealed level-zero keysym")
        event_classes = {
            EventClass.RAW_KEY_PRESS.value,
            EventClass.KEY_PRESS.value,
            EventClass.RAW_KEY_RELEASE.value,
            EventClass.KEY_RELEASE.value,
        }
        action = None
        try:
            action_timeout = 10.0
            if deadline_monotonic is not None:
                action_timeout = max(0.001, float(deadline_monotonic) - time.monotonic())
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                device_by_class={
                    event_class: master if event_class.startswith("Raw") else source
                    for event_class in event_classes
                },
                target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
                required_sequence=(
                    EventClass.RAW_KEY_PRESS.value,
                    EventClass.KEY_PRESS.value,
                    EventClass.RAW_KEY_RELEASE.value,
                    EventClass.KEY_RELEASE.value,
                ),
                expected_details=(keycode,) * 4,
                expected_modifiers=modifiers,
                held_keys={keysym},
                modifier_group_size=2,
                timeout=action_timeout,
            )
            self.guardian.dispatch_admission(generation, lambda: None)
            self._held_input = True
            argv = ["xdotool", "keydown", keysym, "sleep", f"{duration:.6f}", "keyup", keysym]
            self.backend._run(
                argv,
                generation=generation,
                held_keys={keysym},
                input_schema="motion-key",
                expected_argv=tuple(argv),
                deadline_monotonic=deadline_monotonic,
            )
            self.guardian.check(generation)
            if self._monitor is None:
                raise DriverError("motion key completion requires the live XI2 observer")
            wait_timeout = 2.0
            if deadline_monotonic is not None:
                wait_timeout = min(wait_timeout, max(0.0, deadline_monotonic - time.monotonic()))
            self._monitor.wait_for_event(lambda _events: action.complete, timeout=wait_timeout)
            if not action.complete:
                raise DriverError("motion key event trace did not complete")
            self.guardian.finish_action(generation, action)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"motion key hold failed: {exc}")
            raise
        finally:
            self._held_input = False

    def run_key_timeline(
        self,
        segment_events: Any,
        frame: Any,
        *,
        generation: int,
        deadline_monotonic: float | None = None,
    ) -> None:
        """Dispatch one closed-loop route segment as one sealed key trace."""

        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise DriverError("motion timeline deadline is malformed")
        if frame is None or not callable(getattr(frame, "binding", None)):
            raise DriverError("motion timeline frame is malformed")
        try:
            from .route_walk import MAX_KEY_HOLD_SECONDS, KeyEvent

            events = tuple(segment_events)
        except (TypeError, ValueError) as exc:
            raise DriverError("motion timeline events are malformed") from exc
        if len(events) > 64:
            raise DriverError("motion timeline contains too many events")
        logical_keys = frozenset({"W", "Left", "Right", "space"})
        active: set[str] = set()
        pressed_at: dict[str, float] = {}
        last_release: dict[str, float] = {}
        normalized: list[KeyEvent] = []
        previous_time = 0.0
        for raw in events:
            if not isinstance(raw, KeyEvent):
                try:
                    event = KeyEvent(raw[0], raw[1], raw[2])
                except (IndexError, TypeError, ValueError) as exc:
                    raise DriverError("motion timeline event is malformed") from exc
            else:
                event = raw
            if (
                isinstance(event.t, bool)
                or not isinstance(event.t, (int, float))
                or not math.isfinite(float(event.t))
                or float(event.t) < 0.0
                or float(event.t) < previous_time
            ):
                raise DriverError("motion timeline timestamps are not ordered and bounded")
            if event.key not in logical_keys or type(event.down) is not bool:
                raise DriverError("motion timeline key event is outside the sealed key set")
            at = float(event.t)
            if event.down:
                if event.key in active:
                    raise DriverError(f"motion timeline presses {event.key} while held")
                if event.key in last_release and at == last_release[event.key]:
                    raise DriverError(f"motion timeline re-presses {event.key} at the same time")
                active.add(event.key)
                pressed_at[event.key] = at
            else:
                if event.key not in active:
                    raise DriverError(f"motion timeline releases {event.key} without a press")
                hold = at - pressed_at[event.key]
                if hold < 0.03 - 1e-9 or hold > MAX_KEY_HOLD_SECONDS + 1e-9:
                    raise DriverError(f"motion timeline {event.key} hold is outside the human bound")
                active.remove(event.key)
                del pressed_at[event.key]
                last_release[event.key] = at
            normalized.append(KeyEvent(at, event.key, event.down))
            previous_time = at
        if active:
            raise DriverError("motion timeline leaves keys held")
        duration = normalized[-1].t if normalized else 0.0
        if duration > 6.0:
            raise DriverError("motion timeline exceeds the six-second bound")
        if not normalized:
            return

        identity, evidence, source, master, modifiers, keymap = self._motion_input_context(
            generation, pointer=False, deadline_monotonic=deadline_monotonic
        )
        if getattr(frame, "identity_fingerprint", None) != identity.fingerprint:
            raise DriverError("motion timeline frame identity is stale")
        binding_fields = {
            "W": "moveForward",
            "Left": "turnLeft",
            "Right": "turnRight",
            "space": "jump",
        }
        keysyms: dict[str, str] = {}
        keycodes: dict[str, int] = {}
        for logical, binding_field in binding_fields.items():
            binding = frame.binding(binding_field)
            if logical == "space":
                if binding != "SPACE":
                    raise DriverError("native jump binding is not the observed SPACE keysym")
                keysym = "space"
            else:
                if not isinstance(binding, str) or len(binding) != 1 or not re.fullmatch(r"[A-Z]", binding):
                    raise DriverError("native route binding is not a single unmodified level-zero keysym")
                keysym = binding.lower()
            keycode = keymap.get(keysym)
            if isinstance(keycode, bool) or not isinstance(keycode, int) or keycode <= 0:
                raise DriverError("native route binding is absent from the calibrated keymap")
            if isinstance(self.backend, X11Backend):
                levels = (
                    self.backend.observe_keymap_levels()
                    if deadline_monotonic is None
                    else self.backend.observe_keymap_levels(deadline_monotonic=deadline_monotonic)
                ).get(keycode)
                if not levels or levels[0] != keysym:
                    raise DriverError("native route binding is not the sealed level-zero keysym")
            keysyms[logical] = keysym
            keycodes[logical] = keycode
        plan_keys = {keysyms[event.key] for event in normalized}
        argv = ["xdotool"]
        current = 0.0
        required_sequence: list[str] = []
        expected_details: list[int] = []
        expected_modifiers: list[tuple[int, int, int]] = []
        for event in normalized:
            delay = event.t - current
            if delay > 0.0:
                remaining = delay
                while remaining > 0.0:
                    piece = min(2.0, remaining)
                    argv.extend(("sleep", f"{piece:.9f}"))
                    remaining -= piece
            keysym = keysyms[event.key]
            if event.down:
                argv.extend(("keydown", keysym))
                required_sequence.extend((EventClass.RAW_KEY_PRESS.value, EventClass.KEY_PRESS.value))
            else:
                argv.extend(("keyup", keysym))
                required_sequence.extend((EventClass.RAW_KEY_RELEASE.value, EventClass.KEY_RELEASE.value))
            expected_details.extend((keycodes[event.key], keycodes[event.key]))
            expected_modifiers.append(tuple(modifiers))
            current = event.t

        event_classes = {
            EventClass.RAW_KEY_PRESS.value,
            EventClass.KEY_PRESS.value,
            EventClass.RAW_KEY_RELEASE.value,
            EventClass.KEY_RELEASE.value,
        }
        action = None
        try:
            action_timeout = 10.0
            if deadline_monotonic is not None:
                action_timeout = max(0.001, float(deadline_monotonic) - time.monotonic())
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                device_by_class={
                    event_class: master if event_class.startswith("Raw") else source
                    for event_class in event_classes
                },
                target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
                required_sequence=tuple(required_sequence),
                expected_details=tuple(expected_details),
                expected_modifier_sequence=tuple(expected_modifiers),
                expected_modifiers=modifiers,
                held_keys=plan_keys,
                modifier_group_size=2,
                timeout=action_timeout,
            )
            self.guardian.dispatch_admission(generation, lambda: None)
            self._held_input = True
            self.backend._run(
                argv,
                generation=generation,
                held_keys=plan_keys,
                input_schema="motion-timeline",
                expected_argv=tuple(argv),
                deadline_monotonic=deadline_monotonic,
            )
            self.guardian.check(generation)
            if self._monitor is None:
                raise DriverError("motion timeline completion requires the live XI2 observer")
            wait_timeout = 10.0
            if deadline_monotonic is not None:
                wait_timeout = min(wait_timeout, max(0.0, deadline_monotonic - time.monotonic()))
            self._monitor.wait_for_event(lambda _events: action.complete, timeout=wait_timeout)
            if not action.complete:
                raise DriverError("motion timeline event trace did not complete")
            self.guardian.finish_action(generation, action)
        except GuardianError as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"motion timeline Guardian failure: {exc}")
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"motion timeline failed: {exc}")
            raise
        finally:
            self._held_input = False

    def run_pointer_motion(
        self,
        point: Point,
        *,
        generation: int,
        deadline_monotonic: float | None = None,
    ) -> HumanPointerPlan:
        """Move to one verified client pixel with a Guardian-owned gesture."""

        if not isinstance(point, Point):
            raise DriverError("world pointer probe is malformed")
        identity, evidence, source, master, modifiers, _keymap = self._motion_input_context(
            generation, pointer=True, deadline_monotonic=deadline_monotonic
        )
        client_bounds = Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3])
        target_point = point.to_pixel()
        if not client_bounds.contains(target_point):
            raise DriverError("world pointer probe is outside the verified client")
        root_x, root_y, pointer_window = self.backend.pointer_position(
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        )
        if not self._is_owned_client_target(pointer_window, identity):
            raise DriverError("native pointer start is outside the verified client")
        start = Point(root_x - identity.child_geometry[0], root_y - identity.child_geometry[1]).to_pixel()
        binding = BindingMetadata(
            identity.fingerprint,
            f"world-pointer:{generation}:{identity.fingerprint}",
            max(1, self._named_capture_sequence + 1),
            generation,
            identity.child_xid,
            tuple(identity.target_event_xids) or (identity.child_xid,),
            source="native-telemetry",
        )
        try:
            gesture = plan_human_pointer(
                start,
                PointerTarget(
                    "world-hover",
                    Rect(int(target_point.x), int(target_point.y), 1, 1),
                    binding,
                    padding=0,
                ),
                client_bounds,
                seed=secrets.randbits(64),
                button=None,
                profile=self.pointer_motion_profile,
            )
            chain = build_pointer_motion_chain(gesture.trajectory, identity.child_geometry)
        except (ExecutorError, PointerExecutionError) as exc:
            raise DriverError(f"world pointer gesture is invalid: {exc}") from exc
        trace: list[tuple[int, int, int, int]] = []
        prior = gesture.trajectory.samples[0].point
        left, top, _width, _height = identity.child_geometry
        for sample in gesture.trajectory.samples[1:]:
            if sample.point == prior:
                continue
            root = (left + int(sample.point.x), top + int(sample.point.y))
            trace.append(
                (*root, *root)
                if evidence.xtest_profile is not None
                else (*root, int(sample.point.x), int(sample.point.y))
            )
            prior = sample.point
        canonical_motion = isinstance(self.backend, X11Backend) and evidence.xtest_profile is not None
        event_classes = {
            EventClass.RAW_MOTION.value,
            EventClass.MOTION.value,
            EventClass.DEVICE_CHANGED.value,
            EventClass.ENTER.value,
            EventClass.LEAVE.value,
        }
        raw_echo = getattr(evidence, "synthetic_echo_proof", ())
        if not isinstance(raw_echo, (tuple, list)):
            raise DriverError("input calibration controlled echo proof is malformed")
        echo_proof = tuple(tuple(record) for record in raw_echo)
        raw_sources = getattr(evidence, "forwarded_echo_sources", ())
        if not isinstance(raw_sources, (tuple, list, set, frozenset)):
            raise DriverError("input calibration forwarded echo sources are malformed")
        if any(
            isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0
            for source_id in raw_sources
        ):
            raise DriverError("input calibration forwarded echo sources are malformed")
        echo_sources = frozenset(raw_sources)
        action = None
        try:
            action_timeout = 10.0
            if deadline_monotonic is not None:
                action_timeout = max(0.001, float(deadline_monotonic) - time.monotonic())
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                device_by_class={
                    event_class: master
                    if event_class.startswith("Raw") or event_class == EventClass.DEVICE_CHANGED.value
                    else source
                    for event_class in event_classes
                },
                target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
                optional_classes={
                    EventClass.DEVICE_CHANGED.value,
                    EventClass.ENTER.value,
                    EventClass.LEAVE.value,
                },
                expected_modifiers=modifiers,
                expected_motion_trace=tuple(trace),
                initial_origin=(root_x, root_y, root_x, root_y) if canonical_motion else None,
                expected_root_xid=getattr(evidence.xtest_profile, "root_xid", None)
                if canonical_motion
                else None,
                expected_event_xid=getattr(evidence.xtest_profile, "root_xid", None)
                if canonical_motion
                else None,
                expected_child_xids=(
                    frozenset(identity.target_event_xids) if canonical_motion else frozenset()
                ),
                canonical_motion=canonical_motion,
                echo_proof=echo_proof,
                echo_sources=echo_sources,
                timeout=action_timeout,
            )
            self.guardian.dispatch_admission(generation, lambda: None)
            self._held_input = True
            self.backend._run(
                chain,
                generation=generation,
                input_schema="pointer-motion",
                expected_argv=tuple(chain),
                deadline_monotonic=deadline_monotonic,
            )
            self.guardian.check(generation)
            if self._monitor is None:
                raise DriverError("world pointer completion requires the live XI2 observer")
            wait_timeout = 3.0
            if deadline_monotonic is not None:
                wait_timeout = min(wait_timeout, max(0.0, deadline_monotonic - time.monotonic()))
            self._monitor.wait_for_event(lambda _events: action.complete, timeout=wait_timeout)
            if not action.complete:
                raise DriverError("world pointer event trace did not complete")
            self.guardian.finish_action(generation, action)
            final_x, final_y, final_window = self.backend.pointer_position(
                generation=generation,
                deadline_monotonic=deadline_monotonic,
            )
            if not self._is_owned_client_target(final_window, identity) or (
                final_x,
                final_y,
            ) != (left + int(gesture.endpoint.x), top + int(gesture.endpoint.y)):
                raise DriverError("native pointer probe endpoint changed")
            return gesture
        except GuardianError as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"world pointer Guardian failure: {exc}")
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"world pointer motion failed: {exc}")
            raise
        finally:
            self._held_input = False

    def run_hover_click(
        self,
        point: Point,
        hover_frame: Any,
        *,
        gesture: HumanPointerPlan,
        gesture_ended_monotonic: float,
        generation: int,
        deadline_monotonic: float | None = None,
        action_generation: int | None = None,
        lease_generation: int | None = None,
        rate_reservation: RateReservation | None = None,
        click_rate_permit: RatePermit | None = None,
        live_admission_check: Callable[[], None] | None = None,
        target_name: str,
    ) -> None:
        """Click one pointer endpoint only after a matching fresh hover frame."""

        if not isinstance(point, Point) or not isinstance(gesture, HumanPointerPlan):
            raise DriverError("world click gesture is malformed")
        if (
            isinstance(gesture_ended_monotonic, bool)
            or not isinstance(gesture_ended_monotonic, (int, float))
            or not math.isfinite(float(gesture_ended_monotonic))
        ):
            raise DriverError("world click gesture timestamp is malformed")
        if rate_reservation is not None and click_rate_permit is not None:
            raise DriverError("world click received both broker reservation and legacy permit")
        if rate_reservation is None:
            if not isinstance(click_rate_permit, RatePermit) or not click_rate_permit.is_open:
                raise DriverError("world click requires a held post-hover rate permit")
            try:
                rate_reservation = self.input_broker.adopt_permit_for_tests(click_rate_permit)
            except InputBrokerError as exc:
                raise DriverError(str(exc)) from exc
        if not isinstance(rate_reservation, RateReservation) or not rate_reservation.is_open:
            raise DriverError("world click requires an open broker rate reservation")
        selected_action_generation = generation if action_generation is None else action_generation
        if isinstance(selected_action_generation, bool) or not isinstance(selected_action_generation, int):
            raise DriverError("world click action generation is malformed")
        selected_lease_generation = generation if lease_generation is None else lease_generation
        if isinstance(selected_lease_generation, bool) or not isinstance(selected_lease_generation, int):
            raise DriverError("world click lease generation is malformed")
        if not isinstance(target_name, str) or not target_name or len(target_name.encode("utf-8")) > 48:
            raise DriverError("world click target name is malformed")
        from .motion import MAX_FRAME_AGE

        identity, evidence, source, master, modifiers, _keymap = self._motion_input_context(
            generation, pointer=True, deadline_monotonic=deadline_monotonic
        )
        if getattr(hover_frame, "identity_fingerprint", None) != identity.fingerprint:
            raise DriverError("world click hover identity is stale")
        capture_monotonic = getattr(hover_frame, "capture_monotonic", None)
        frame_age = (
            time.monotonic() - float(capture_monotonic)
            if isinstance(capture_monotonic, (int, float))
            else math.inf
        )
        if (
            isinstance(capture_monotonic, bool)
            or not isinstance(capture_monotonic, (int, float))
            or capture_monotonic <= float(gesture_ended_monotonic)
            or capture_monotonic <= rate_reservation.acquired_monotonic
            or frame_age < 0.0
            or frame_age > MAX_FRAME_AGE
        ):
            raise DriverError("world click hover frame is stale or predates the completed gesture")
        hover_kind = getattr(hover_frame, "hover_kind", None)
        hover_name = getattr(hover_frame, "hover_name", None)
        if hover_kind not in {"unit", "object"} or not isinstance(hover_name, str):
            raise DriverError("world click hover frame has no clickable target")
        if hover_name != target_name:
            raise DriverError("world click hover target name changed")
        if getattr(hover_frame, "chat_focused", True):
            raise DriverError("world click hover frame has focused chat")
        client_bounds = Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3])
        endpoint = point.to_pixel()
        if not client_bounds.contains(endpoint):
            raise DriverError("world click endpoint is outside the verified client")
        binding = gesture.trajectory.binding
        if (
            binding.identity_fingerprint != identity.fingerprint
            or binding.generation != generation
            or gesture.trajectory.endpoint != endpoint
        ):
            raise DriverError("world click gesture binding or endpoint is stale")
        left, top, _width, _height = identity.child_geometry
        actual_x, actual_y, actual_window = self.backend.pointer_position(
            generation=generation,
            deadline_monotonic=deadline_monotonic,
        )
        expected = (left + int(endpoint.x), top + int(endpoint.y))
        if not self._is_owned_client_target(actual_window, identity) or (actual_x, actual_y) != expected:
            raise DriverError("native pointer moved after the admitted hover frame")
        seed = gesture.seed ^ 0x9E3779B97F4A7C15
        dwell = random.Random(seed).uniform(
            self.pointer_motion_profile.min_dwell,
            self.pointer_motion_profile.max_dwell,
        )
        click_argv = ["xdotool", "mousedown", "1", "sleep", f"{dwell:.6f}", "mouseup", "1"]
        canonical_motion = isinstance(self.backend, X11Backend) and evidence.xtest_profile is not None
        event_classes = {
            EventClass.RAW_BUTTON_PRESS.value,
            EventClass.BUTTON_PRESS.value,
            EventClass.RAW_BUTTON_RELEASE.value,
            EventClass.BUTTON_RELEASE.value,
        }
        action = None
        try:
            action_timeout = 10.0
            if deadline_monotonic is not None:
                action_timeout = max(0.001, float(deadline_monotonic) - time.monotonic())
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                detail=1,
                device_by_class={
                    event_class: master if event_class.startswith("Raw") else source
                    for event_class in event_classes
                },
                target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
                required_sequence=(
                    EventClass.RAW_BUTTON_PRESS.value,
                    EventClass.BUTTON_PRESS.value,
                    EventClass.RAW_BUTTON_RELEASE.value,
                    EventClass.BUTTON_RELEASE.value,
                ),
                expected_details=(1, 1, 1, 1),
                expected_modifiers=modifiers,
                expected_coordinates=(float(actual_x), float(actual_y)),
                expected_event_coordinates=(
                    (float(actual_x), float(actual_y))
                    if canonical_motion
                    else (float(endpoint.x), float(endpoint.y))
                ),
                initial_origin=(actual_x, actual_y, actual_x, actual_y) if canonical_motion else None,
                expected_root_xid=getattr(evidence.xtest_profile, "root_xid", None)
                if canonical_motion
                else None,
                expected_event_xid=getattr(evidence.xtest_profile, "root_xid", None)
                if canonical_motion
                else None,
                expected_child_xids=(
                    frozenset(identity.target_event_xids) if canonical_motion else frozenset()
                ),
                canonical_motion=canonical_motion,
                held_buttons={1},
                timeout=action_timeout,
            )
            self.guardian.dispatch_admission(generation, lambda: None)
            self._held_input = True
            try:
                if live_admission_check is not None:
                    live_admission_check()
                self.last_pointer_execution = {
                    "action": "world-click",
                    "generation": generation,
                    "seed": seed,
                    "frozenEndpoint": (int(endpoint.x), int(endpoint.y)),
                    "phase": "click_planned",
                    "rateGate": "canonical-user-session",
                }
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise DriverError("world click deadline expired before click rate precommit")
                admitted_gesture = self.input_broker.mint_admitted_gesture(
                    action_generation=selected_action_generation,
                    lease_generation=selected_lease_generation,
                    button=PointerButton.LEFT,
                    emitted_chain=click_argv,
                    destination_anchor=f"{endpoint.x}:{endpoint.y}",
                    target_identity=f"{identity.fingerprint}:{target_name}",
                    guardian_admission_id=f"world-click:{selected_action_generation}:{id(action)}",
                    semantic_authorization="world_click",
                    deadline_monotonic=deadline_monotonic,
                    reservation=rate_reservation,
                )
                if live_admission_check is not None:
                    live_admission_check()
                x11_backend = cast(X11Backend, self.backend)
                recorded_press = self.input_broker.emit_button_gesture(
                    admitted_gesture,
                    emitted_chain=click_argv,
                    action_generation=selected_action_generation,
                    lease_generation=selected_lease_generation,
                    destination_anchor=f"{endpoint.x}:{endpoint.y}",
                    target_identity=f"{identity.fingerprint}:{target_name}",
                    guardian_admission_id=f"world-click:{selected_action_generation}:{id(action)}",
                    semantic_authorization="world_click",
                    emit=lambda claim: x11_backend._run(
                        click_argv,
                        generation=generation,
                        held_buttons={1},
                        input_schema="single-click",
                        expected_argv=tuple(click_argv),
                        deadline_monotonic=deadline_monotonic,
                        button_emission_claim=claim,
                        emission_action_generation=selected_action_generation,
                        emission_lease_generation=selected_lease_generation,
                    ),
                )
                self.last_pointer_execution["clickPrecommitMonotonicNs"] = (
                    admitted_gesture.precommit_monotonic_ns
                )
            except InputBrokerError as exc:
                raise DriverError(str(exc)) from exc
            # The broker records immediately after the native wrapper returns;
            # later Guardian observation cannot erase the durable reservation.
            self.last_pointer_execution["rateRecordMonotonicNs"] = recorded_press
            self.guardian.check(generation)
            if self._monitor is None:
                raise DriverError("world click completion requires the live XI2 observer")
            wait_timeout = 3.0
            if deadline_monotonic is not None:
                wait_timeout = min(wait_timeout, max(0.0, deadline_monotonic - time.monotonic()))
            self._monitor.wait_for_event(lambda _events: action.complete, timeout=wait_timeout)
            if not action.complete:
                raise DriverError("world click event trace did not complete")
            self.guardian.finish_action(generation, action)
            final_x, final_y, final_window = self.backend.pointer_position(
                generation=generation,
                deadline_monotonic=deadline_monotonic,
            )
            if not self._is_owned_client_target(final_window, identity) or (final_x, final_y) != expected:
                raise DriverError("native pointer postcondition endpoint changed")
        except GuardianError as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"world click Guardian failure: {exc}")
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"world click failed: {exc}")
            raise
        finally:
            self._held_input = False

    def run_camera_drag(
        self,
        plan: CameraDragPlan,
        *,
        generation: int,
        action_generation: int | None = None,
        deadline_monotonic: float | None = None,
        live_admission_check: Callable[[], None] | None = None,
    ) -> None:
        """Dispatch one bounded right-button drag while the button is held."""

        if not isinstance(plan, CameraDragPlan):
            raise DriverError("camera drag plan is malformed")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise DriverError("camera drag deadline is malformed")
        selected_action_generation = generation if action_generation is None else action_generation
        if isinstance(selected_action_generation, bool) or not isinstance(selected_action_generation, int):
            raise DriverError("camera drag action generation is malformed")
        identity, evidence, source, master, modifiers, _keymap = self._motion_input_context(
            generation, pointer=True, deadline_monotonic=deadline_monotonic
        )
        if isinstance(self.backend, X11Backend):
            self._verify_current_xtest_profile(
                evidence,
                generation=generation,
                deadline_monotonic=deadline_monotonic,
            )
        if plan.binding.identity_fingerprint != identity.fingerprint or plan.binding.generation != generation:
            raise DriverError("camera drag binding is stale")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DriverError("camera drag deadline expired before admission")
        root_x, root_y, window = self.backend.pointer_position(
            generation=generation, deadline_monotonic=deadline_monotonic
        )
        if not self._is_owned_client_target(window, identity):
            raise DriverError("camera drag pointer is outside the verified client")
        left, top, width, height = identity.child_geometry
        start = plan.samples[0].point
        current = Point(root_x - left, root_y - top).to_pixel()
        client = Rect(0, 0, width, height)
        if not client.contains(current):
            raise DriverError("camera drag pointer is outside the verified client pixels")
        positioning: PointerTrajectory | None = None
        if current != start:
            try:
                positioning = plan_pointer_curve(
                    current,
                    PointerTarget(
                        "camera-viewport",
                        Rect(int(start.x), int(start.y), 1, 1),
                        plan.binding,
                        padding=0,
                    ),
                    client,
                    profile=self.pointer_motion_profile,
                )
            except ExecutorError as exc:
                raise DriverError(f"camera positioning plan is invalid: {exc}") from exc
        positioning_trace: list[tuple[int, int, int, int]] = []
        canonical_motion = (
            isinstance(self.backend, X11Backend) and getattr(evidence, "xtest_profile", None) is not None
        )
        if positioning is not None:
            prior = positioning.samples[0].point
            for sample in positioning.samples[1:]:
                if sample.point != prior:
                    root_point = (left + int(sample.point.x), top + int(sample.point.y))
                    positioning_trace.append(
                        (*root_point, *root_point)
                        if canonical_motion
                        else (*root_point, int(sample.point.x), int(sample.point.y))
                    )
                    prior = sample.point
        trace: list[tuple[int, int, int, int]] = []
        prior = start
        for sample in plan.samples[1:]:
            if sample.point == prior:
                continue
            root_point = (left + int(sample.point.x), top + int(sample.point.y))
            trace.append(
                (*root_point, *root_point)
                if canonical_motion
                else (*root_point, int(sample.point.x), int(sample.point.y))
            )
            prior = sample.point
        if canonical_motion and not trace:
            raise DriverError("camera drag has no native motion trace")
        echo = getattr(evidence, "synthetic_echo_proof", ())
        action = None
        rate_reservation: RateReservation | None = None

        def require_live_admission() -> None:
            self._require_camera_admission_current(generation)
            if live_admission_check is not None:
                live_admission_check()

        try:
            action_timeout = 10.0
            if deadline_monotonic is not None:
                action_timeout = max(0.001, float(deadline_monotonic) - time.monotonic())
            event_classes = {
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
                EventClass.DEVICE_CHANGED.value,
                EventClass.RAW_MOTION.value,
                EventClass.MOTION.value,
                EventClass.ENTER.value,
                EventClass.LEAVE.value,
            }
            certified_event_targets = frozenset(identity.target_event_xids)
            if canonical_motion and not certified_event_targets:
                raise DriverError("native camera action has no certified event-target set")
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                detail=3,
                device_by_class={
                    event_class: master
                    if event_class.startswith("Raw") or event_class == EventClass.DEVICE_CHANGED.value
                    else source
                    for event_class in event_classes
                },
                target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
                required_sequence=(
                    EventClass.RAW_BUTTON_PRESS.value,
                    EventClass.BUTTON_PRESS.value,
                    EventClass.RAW_BUTTON_RELEASE.value,
                    EventClass.BUTTON_RELEASE.value,
                ),
                optional_classes={
                    EventClass.DEVICE_CHANGED.value,
                    EventClass.RAW_MOTION.value,
                    EventClass.MOTION.value,
                    EventClass.ENTER.value,
                    EventClass.LEAVE.value,
                },
                expected_modifiers=modifiers,
                expected_details=(3, 3, 3, 3),
                expected_motion_trace=tuple(trace),
                expected_positioning_trace=tuple(positioning_trace),
                expected_press_coordinates=(float(left + int(start.x)), float(top + int(start.y))),
                expected_press_event_coordinates=(
                    (float(left + int(start.x)), float(top + int(start.y)))
                    if canonical_motion
                    else (float(start.x), float(start.y))
                ),
                expected_release_coordinates=(
                    float(left + int(plan.samples[-1].point.x)),
                    float(top + int(plan.samples[-1].point.y)),
                ),
                expected_release_event_coordinates=(
                    (
                        float(left + int(plan.samples[-1].point.x)),
                        float(top + int(plan.samples[-1].point.y)),
                    )
                    if canonical_motion
                    else (
                        float(plan.samples[-1].point.x),
                        float(plan.samples[-1].point.y),
                    )
                ),
                initial_origin=(root_x, root_y, root_x, root_y) if canonical_motion else None,
                expected_root_xid=(
                    getattr(evidence.xtest_profile, "root_xid", None) if canonical_motion else None
                ),
                expected_event_xid=(
                    getattr(evidence.xtest_profile, "root_xid", None) if canonical_motion else None
                ),
                expected_child_xids=(certified_event_targets if canonical_motion else frozenset()),
                canonical_motion=canonical_motion,
                require_motion_before_button=positioning is not None,
                require_motion_before_release=True,
                echo_proof=tuple(tuple(row) for row in echo),
                held_buttons={3},
                timeout=action_timeout,
            )
            self.guardian.dispatch_admission(generation, lambda: None)
            self._held_input = True
            try:
                rate_reservation = self.input_broker.reserve_button_rate(
                    deadline_monotonic=deadline_monotonic,
                    cancellation_check=require_live_admission,
                )
            except InputBrokerError as exc:
                raise DriverError(f"camera drag rate admission failed: {exc}") from exc
            chain = build_camera_drag_chain(plan, identity.child_geometry, positioning)
            admitted_gesture = self.input_broker.mint_admitted_gesture(
                action_generation=selected_action_generation,
                lease_generation=generation,
                button=PointerButton.RIGHT,
                emitted_chain=chain,
                destination_anchor=f"{identity.fingerprint}:{start.x}:{start.y}",
                target_identity=identity.fingerprint,
                guardian_admission_id=f"camera-drag:{selected_action_generation}:{id(action)}",
                semantic_authorization="camera_drag",
                deadline_monotonic=deadline_monotonic,
                reservation=rate_reservation,
            )
            require_live_admission()
            x11_backend = cast(X11Backend, self.backend)
            self.input_broker.emit_button_gesture(
                admitted_gesture,
                emitted_chain=chain,
                action_generation=selected_action_generation,
                lease_generation=generation,
                destination_anchor=f"{identity.fingerprint}:{start.x}:{start.y}",
                target_identity=identity.fingerprint,
                guardian_admission_id=f"camera-drag:{selected_action_generation}:{id(action)}",
                semantic_authorization="camera_drag",
                emit=lambda claim: x11_backend._run(
                    chain,
                    timeout=TAKEOVER_DRILL_HOVER_COMMAND_TIMEOUT_SECONDS,
                    generation=generation,
                    held_buttons={3},
                    input_schema="camera-drag",
                    expected_argv=tuple(chain),
                    deadline_monotonic=deadline_monotonic,
                    button_emission_claim=claim,
                    emission_action_generation=selected_action_generation,
                    emission_lease_generation=generation,
                ),
            )
            self.guardian.check(generation)
            if self._monitor is None:
                raise DriverError("camera drag completion requires the live XI2 observer")
            wait_timeout = 3.0
            if deadline_monotonic is not None:
                wait_timeout = min(wait_timeout, max(0.0, deadline_monotonic - time.monotonic()))
            self._monitor.wait_for_event(lambda _events: action.complete, timeout=wait_timeout)
            if not action.complete:
                raise DriverError("camera drag event trace did not complete")
            if canonical_motion:
                final_x, final_y, final_window = self.backend.pointer_position(
                    generation=generation,
                    deadline_monotonic=deadline_monotonic,
                )
                if not self._is_owned_client_target(final_window, identity):
                    raise DriverError("camera drag postcondition left the verified client")
                left, top, _width, _height = identity.child_geometry
                endpoint = plan.samples[-1].point
                if (final_x, final_y) != (left + int(endpoint.x), top + int(endpoint.y)):
                    raise DriverError("camera drag postcondition endpoint changed")
            self.guardian.finish_action(generation, action)
        except (GuardianError, InputBrokerError) as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"camera drag admission failed: {exc}")
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"camera drag failed: {exc}")
            raise
        finally:
            if rate_reservation is not None:
                self.input_broker.release_reservation(rate_reservation)
            self._held_input = False

    def _require_camera_admission_current(self, generation: int) -> None:
        if not self.guardian.armed or self.guardian.generation != generation:
            raise GuardianError("camera drag admission was cancelled")

    def run(
        self,
        name: str,
        generation: int,
        *,
        precondition: Callable[[WindowIdentity], bool] | None = None,
        postcondition: Callable[[WindowIdentity], bool] | None = None,
        deadline_monotonic: float | None = None,
        action_generation: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        if deadline_monotonic is not None and self._clock() >= deadline_monotonic:
            raise DriverError("desktop action deadline expired before admission")
        if name not in self.ACTIONS:
            raise DriverError(f"action {name!r} is not allowlisted")
        selected_action_generation = generation if action_generation is None else action_generation
        if isinstance(selected_action_generation, bool) or not isinstance(selected_action_generation, int):
            raise DriverError("desktop action generation is malformed")
        premeasured_anchor = kwargs.pop("_premeasured_anchor", None)
        if name == "executor_click" and not callable(kwargs.get("_final_admission")):
            raise DriverError("executor click requires a final admission callback")
        try:
            self.guardian.check(generation)
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        if self._is_pointer_action(name) and isinstance(self.backend, X11Backend):
            supplied_reservation = kwargs.get("_rate_reservation")
            if supplied_reservation is None:
                supplied_legacy_permit = kwargs.get("_click_rate_permit")
                if supplied_legacy_permit is not None:
                    try:
                        reservation = self.input_broker.adopt_permit_for_tests(supplied_legacy_permit)
                    except InputBrokerError as exc:
                        raise DriverError(str(exc)) from exc
                    reservation_owned = False
                else:
                    try:
                        reservation = self.input_broker.reserve_button_rate(
                            deadline_monotonic=deadline_monotonic,
                            cancellation_check=lambda: self.guardian.check(generation),
                        )
                    except (InputBrokerError, GuardianError) as exc:
                        raise DriverError(f"pointer click admission failed: {exc}") from exc
                    reservation_owned = True
                try:
                    forwarded = {
                        key: value
                        for key, value in kwargs.items()
                        if key not in {"_rate_reservation", "_click_rate_permit"}
                    }
                    if premeasured_anchor is not None:
                        forwarded["_premeasured_anchor"] = premeasured_anchor
                    result = self.run(
                        name,
                        generation,
                        precondition=precondition,
                        postcondition=postcondition,
                        action_generation=selected_action_generation,
                        _rate_reservation=reservation,
                        deadline_monotonic=deadline_monotonic,
                        **forwarded,
                    )
                finally:
                    if reservation_owned:
                        self.input_broker.release_reservation(reservation)
                return result
            if not isinstance(supplied_reservation, RateReservation) or not supplied_reservation.is_open:
                raise DriverError("pointer action has a malformed or closed broker rate reservation")
        if name == "type_allowlisted_command" and kwargs.get("command") not in self.COMMANDS:
            raise DriverError("command is not allowlisted")
        identity = (
            self._identity(require_foreground=False, deadline_monotonic=deadline_monotonic)
            if isinstance(self.backend, X11Backend)
            else self._identity()
        )
        if precondition is None and isinstance(self.backend, X11Backend):
            raise DriverError(f"{name} requires actual telemetry/visual precondition evidence")
        self._check_deadline(deadline_monotonic, f"{name} deadline expired before precondition")
        if precondition is not None and not precondition(identity):
            raise DriverError(f"{name} precondition failed")
        self._check_deadline(deadline_monotonic, f"{name} deadline expired after precondition")
        if name == "focus_game":
            # Focus recovery emits no key/button proof, but its activation
            # command can block.  Register an empty owned action so revoke()
            # still cancels the active process even when no physical item is
            # held and the generation fence remains atomic.
            action = None
            try:
                focus_kwargs: dict[str, Any] = {
                    "target_xid": identity.child_xid,
                    "source_device": 0,
                    "master_device": 0,
                    "event_classes": set(),
                    "target_ancestor": (
                        (lambda xid: self._is_owned_client_target(xid, identity))
                        if isinstance(self.backend, X11Backend)
                        else None
                    ),
                    "required_sequence": (),
                }
                if isinstance(self.backend, X11Backend) and self._input_evidence is not None:
                    focus_source = getattr(self._input_evidence, "synthetic_pointer_source", 0)
                    focus_master = next(
                        (
                            parent
                            for device, parent, _role, _name in getattr(
                                self._input_evidence, "xinput_hierarchy", ()
                            )
                            if device == focus_source
                        ),
                        0,
                    )
                    if focus_source <= 0 or focus_master <= 0:
                        raise DriverError("focus recovery has no measured pointer source")
                    transitions = {
                        EventClass.DEVICE_CHANGED.value,
                        EventClass.MOTION.value,
                        EventClass.ENTER.value,
                        EventClass.LEAVE.value,
                    }
                    focus_kwargs.update(
                        source_device=focus_source,
                        master_device=focus_master,
                        event_classes=transitions,
                        device_by_class={
                            EventClass.DEVICE_CHANGED.value: focus_master,
                            EventClass.MOTION.value: focus_source,
                            EventClass.ENTER.value: focus_source,
                            EventClass.LEAVE.value: focus_source,
                        },
                        optional_classes=transitions,
                    )
                action = self.guardian.begin_action(
                    generation,
                    **focus_kwargs,
                )
                if isinstance(self.backend, X11Backend):
                    self.guardian.dispatch_admission(generation, lambda: None)
                    self.backend.action(
                        name,
                        generation=generation,
                        deadline_monotonic=deadline_monotonic,
                        **kwargs,
                    )
                else:
                    self.guardian.dispatch_admission(generation, lambda: self.backend.action(name, **kwargs))
                fresh = self._identity(deadline_monotonic=deadline_monotonic)
                if fresh.fingerprint != identity.fingerprint:
                    raise DriverError("game client identity changed during focus recovery")
                self._check_deadline(
                    deadline_monotonic,
                    "focus recovery deadline expired before postcondition",
                )
                if postcondition is not None and not postcondition(fresh):
                    raise DriverError("focus recovery postcondition failed")
                self._check_deadline(
                    deadline_monotonic,
                    "focus recovery deadline expired after postcondition",
                )
            except GuardianError as exc:
                raise DriverError(str(exc)) from exc
            except Exception as exc:
                if self.guardian.armed and self.guardian.generation == generation:
                    self.revoke(f"{name} failed: {exc}")
                raise
            finally:
                if action is not None and self.guardian.armed and self.guardian.generation == generation:
                    self.guardian.finish_action(generation, action)
            return
        pointer_action = self._is_pointer_action(name)
        backend_action = getattr(self.backend.action, "__func__", None)
        native_pointer_dispatch = isinstance(self.backend, X11Backend) and backend_action is X11Backend.action
        evidence = self._input_evidence
        if evidence is None:
            raise DriverError("input action requires owned calibration evidence")
        if pointer_action and isinstance(self.backend, X11Backend):
            # Controls are mutually exclusive across scenes.  Measure exactly
            # the named control immediately before dispatch, then bind that
            # capture to this generation; there is no all-scenes anchor batch.
            self._verify_current_xtest_profile(
                evidence,
                generation=generation,
                deadline_monotonic=deadline_monotonic,
            )
            if premeasured_anchor is None:
                if deadline_monotonic is None:
                    self.measure_pointer_anchor(name, generation=generation)
                else:
                    self.measure_pointer_anchor(
                        name,
                        generation=generation,
                        deadline_monotonic=deadline_monotonic,
                    )
            else:
                if not isinstance(premeasured_anchor, MeasuredAnchor):
                    raise DriverError("premeasured pointer anchor is not sealed evidence")
                if premeasured_anchor.action != name or premeasured_anchor.generation != generation:
                    raise DriverError("premeasured pointer anchor is stale")
                self._validate_measured_anchor(premeasured_anchor, identity)
                self._measured_anchors[name] = premeasured_anchor
                self._measured_anchor_generations[name] = generation
        source = (
            getattr(evidence, "synthetic_pointer_source", 0)
            if pointer_action
            else getattr(evidence, "synthetic_key_source", 0)
        )
        if not isinstance(source, int) or source <= 0:
            raise DriverError("input calibration has no measured source device")
        master: int | None = None
        hierarchy = getattr(evidence, "xinput_hierarchy", ())
        for device_id, parent_id, _role, _name in hierarchy:
            if device_id == source:
                master = parent_id
                break
        if master is None or master <= 0:
            raise DriverError("input calibration has no measured master device")
        event_classes = (
            {
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
                EventClass.DEVICE_CHANGED.value,
                EventClass.RAW_MOTION.value,
                EventClass.MOTION.value,
                EventClass.ENTER.value,
                EventClass.LEAVE.value,
            }
            if pointer_action
            else {
                EventClass.RAW_KEY_PRESS.value,
                EventClass.KEY_PRESS.value,
                EventClass.RAW_KEY_RELEASE.value,
                EventClass.KEY_RELEASE.value,
            }
        )
        required_sequence: tuple[str, ...]
        expected_details: tuple[int, ...]
        expected_modifier_sequence: tuple[tuple[int, int, int], ...] = ()
        modifier_group_size = 4
        if pointer_action:
            required_sequence = (
                EventClass.RAW_BUTTON_PRESS.value,
                EventClass.BUTTON_PRESS.value,
                EventClass.RAW_BUTTON_RELEASE.value,
                EventClass.BUTTON_RELEASE.value,
            )
            expected_details = (self.POINTER_BUTTONS[name],) * 4
        else:
            key_sequence, expected_details, expected_modifier_sequence = self._expected_key_operation(
                name,
                kwargs,
                evidence,
                deadline_monotonic=deadline_monotonic,
            )
            if not key_sequence:
                raise DriverError(f"action {name!r} has no sealed keyboard operation")
            required_sequence = key_sequence
            modifier_group_size = 2
        optional_classes = (
            {
                EventClass.DEVICE_CHANGED.value,
                EventClass.RAW_MOTION.value,
                EventClass.MOTION.value,
                EventClass.ENTER.value,
                EventClass.LEAVE.value,
            }
            if pointer_action
            else set()
        )
        held_keys: set[str] = set()
        held_buttons: set[int] = set()
        # The watchdog receives actual current holds from each bounded backend
        # command.  Future characters in a planned text string never enter
        # the release set before they are emitted.
        # The synthetic probe seals the host's actual locked/latched/base
        # state.  Runtime dispatch must carry that measured tuple forward; it
        # never starts from a guessed zero state.
        expected_modifiers = self._expected_modifiers(evidence)
        if pointer_action:
            native_button = self.POINTER_BUTTONS.get(name)
            if native_button is None:
                raise DriverError(f"pointer button for {name} has not passed live calibration")
            held_buttons.add(native_button)
        else:
            if name == "type_allowlisted_command":
                operation, payload = name, kwargs.get("command")
            elif name in {"open_chat", "submit_command", "escape_command_buffer"}:
                operation, payload = name, ""
            elif name == "open_ah":
                operation, payload = "interact_target", ""
            else:
                operation, payload = "", ""
            if operation:
                for step in self._keyboard_operation_facts(
                    operation,
                    payload,
                    evidence,
                    deadline_monotonic=deadline_monotonic,
                ):
                    keysym = step[1]
                    if not isinstance(keysym, str) or not keysym:
                        raise DriverError("sealed keyboard operation has malformed keysym")
                    held_keys.update(part for part in keysym.split("+") if part)
        echo_proof: tuple[tuple[Any, ...], ...] = ()
        echo_sources: frozenset[int] = frozenset()
        if pointer_action:
            raw_echo = getattr(evidence, "synthetic_echo_proof", ())
            if not isinstance(raw_echo, (tuple, list)):
                raise DriverError("input calibration controlled echo proof is malformed")
            echo_proof = tuple(tuple(record) for record in raw_echo)
            raw_sources = getattr(evidence, "forwarded_echo_sources", ())
            if not isinstance(raw_sources, (tuple, list, set, frozenset)):
                raise DriverError("input calibration forwarded echo sources are malformed")
            if any(
                isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0
                for source_id in raw_sources
            ):
                raise DriverError("input calibration forwarded echo sources are malformed")
            echo_sources = frozenset(raw_sources)
        anchor = self._measured_anchors.get(name) if pointer_action else None
        pointer_plan: PointerTrajectory | None = None
        click_plan = None
        gesture_plan: HumanPointerPlan | None = None
        expected_motion_trace: tuple[tuple[int, int, int, int], ...] = ()
        expected_coordinates = None
        expected_event_coordinates = None
        initial_origin: tuple[int, int, int, int] | None = None
        canonical_motion = bool(
            pointer_action
            and native_pointer_dispatch
            and getattr(evidence, "xtest_profile", None) is not None
        )
        if pointer_action and isinstance(self.backend, X11Backend) and isinstance(anchor, MeasuredAnchor):
            if anchor is None:
                raise DriverError(f"action anchor for {name} is missing at plan construction")
            try:
                current = self.resolve_identity(
                    require_foreground=True,
                    deadline_monotonic=deadline_monotonic,
                )
                if current.fingerprint != identity.fingerprint:
                    raise DriverError("native pointer plan identity changed before admission")
                client_bounds = Rect(0, 0, current.child_geometry[2], current.child_geometry[3])
                target_bounds = Rect(*anchor.bounds)
                binding = BindingMetadata(
                    identity_fingerprint=anchor.identity_fingerprint,
                    capture_id=anchor.capture_id,
                    capture_sequence=anchor.capture_sequence,
                    generation=generation,
                    target_xid=anchor.target_xid,
                    target_ancestry=anchor.target_ancestry,
                    source=anchor.source,
                )
                native_button = self.POINTER_BUTTONS.get(name)
                button = {1: PointerButton.LEFT, 3: PointerButton.RIGHT}.get(native_button or 0)
                if button is None:
                    raise DriverError(f"pointer button for {name} has not passed live calibration")
                root_x, root_y, pointer_window = self.backend.pointer_position(
                    generation=generation,
                    deadline_monotonic=deadline_monotonic,
                )
                if not self._is_owned_client_target(pointer_window, current):
                    raise DriverError("native pointer start is outside the verified client")
                start = Point(
                    root_x - current.child_geometry[0],
                    root_y - current.child_geometry[1],
                ).to_pixel()
                if not client_bounds.contains(start):
                    raise DriverError("native pointer start is outside verified client pixels")
                initial_origin = (root_x, root_y, root_x, root_y)
                padding = 1 if min(target_bounds.width, target_bounds.height) >= 3 else 0
                target = PointerTarget(self.ANCHOR_CONTROLS[name], target_bounds, binding, padding=padding)
                gesture_plan = plan_human_pointer(
                    start,
                    target,
                    client_bounds,
                    button=button,
                    profile=self.pointer_motion_profile,
                )
                click_plan = gesture_plan.click
                if click_plan is None:
                    raise DriverError("human pointer planner omitted its click plan")
                pointer_plan = gesture_plan.trajectory
                trace: list[tuple[int, int, int, int]] = []
                prior = pointer_plan.samples[0].point
                for sample in pointer_plan.samples[1:]:
                    if sample.point == prior:
                        continue
                    root = (
                        current.child_geometry[0] + int(sample.point.x),
                        current.child_geometry[1] + int(sample.point.y),
                    )
                    trace.append(
                        (*root, *root)
                        if canonical_motion
                        else (*root, int(sample.point.x), int(sample.point.y))
                    )
                    prior = sample.point
                expected_motion_trace = tuple(trace)
                expected_coordinates = (
                    float(current.child_geometry[0] + click_plan.point.x),
                    float(current.child_geometry[1] + click_plan.point.y),
                )
                expected_event_coordinates = (
                    (
                        float(current.child_geometry[0] + click_plan.point.x),
                        float(current.child_geometry[1] + click_plan.point.y),
                    )
                    if canonical_motion
                    else (float(click_plan.point.x), float(click_plan.point.y))
                )
            except (ExecutorError, PointerExecutionError) as exc:
                raise DriverError(f"native pointer plan is invalid: {exc}") from exc
        elif anchor is not None:
            expected_coordinates = (
                float(identity.child_geometry[0] + anchor.x),
                float(identity.child_geometry[1] + anchor.y),
            )
            expected_event_coordinates = (float(anchor.x), float(anchor.y))
        action = None
        admitted_anchor: MeasuredAnchor | None = None
        pointer_attempted = False
        pointer_succeeded = False
        pointer_receipt: dict[str, Any] | None = None
        cleanup_return_receipt: dict[str, Any] | None = None
        try:
            device_by_class: dict[str, int] = {}
            for event_class in event_classes:
                if event_class.startswith("Raw") or event_class in {
                    EventClass.DEVICE_CHANGED.value,
                }:
                    device_by_class[event_class] = master
                else:
                    device_by_class[event_class] = source
            target_ancestor = (
                (lambda xid: self._is_owned_client_target(xid, identity))
                if isinstance(self.backend, X11Backend)
                else None
            )
            certified_event_targets = frozenset(identity.target_event_xids)
            if canonical_motion and not certified_event_targets:
                raise DriverError("native pointer action has no certified event-target set")
            action = self.guardian.begin_action(
                generation,
                target_xid=identity.child_xid,
                source_device=source,
                master_device=master,
                event_classes=event_classes,
                detail=self.POINTER_BUTTONS[name] if pointer_action else None,
                device_by_class=device_by_class,
                target_ancestor=target_ancestor,
                required_sequence=required_sequence,
                optional_classes=optional_classes,
                held_keys=held_keys,
                held_buttons=held_buttons,
                expected_modifiers=expected_modifiers,
                echo_proof=echo_proof,
                expected_details=expected_details,
                expected_modifier_sequence=expected_modifier_sequence,
                expected_coordinates=expected_coordinates,
                expected_event_coordinates=expected_event_coordinates,
                expected_motion_trace=expected_motion_trace,
                echo_sources=echo_sources,
                initial_origin=initial_origin,
                expected_root_xid=(
                    getattr(getattr(evidence, "xtest_profile", None), "root_xid", None)
                    if canonical_motion
                    else None
                ),
                expected_event_xid=(
                    getattr(getattr(evidence, "xtest_profile", None), "root_xid", None)
                    if canonical_motion
                    else None
                ),
                expected_child_xids=(certified_event_targets if canonical_motion else frozenset()),
                canonical_motion=canonical_motion,
                expected_press_coordinates=expected_coordinates if canonical_motion else None,
                expected_press_event_coordinates=expected_event_coordinates if canonical_motion else None,
                expected_release_coordinates=expected_coordinates if canonical_motion else None,
                expected_release_event_coordinates=expected_event_coordinates if canonical_motion else None,
                modifier_group_size=modifier_group_size,
                require_delivered_modifiers=True,
            )
            if pointer_action and isinstance(self.backend, X11Backend):
                if anchor is None:
                    raise DriverError(f"action anchor for {name} is missing at admission")
                admitted_anchor = anchor

            def admit_anchor() -> None:
                if anchor is None:
                    return
                try:
                    current_identity = self.resolve_identity(
                        require_foreground=False,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if current_identity.fingerprint != anchor.identity_fingerprint:
                        raise DriverError("native anchor identity changed before one-use admission")
                    age = self._clock() - anchor.capture_monotonic
                    if not math.isfinite(float(age)) or age < -0.25 or age > 5.0:
                        raise DriverError("native anchor capture expired before one-use admission")
                    if isinstance(self.backend, X11Backend):
                        self.backend.consume_measured_anchor(name, anchor)
                except Exception:
                    # Admission itself is one-use.  Even a stale generation
                    # attempt burns the visual proof and cannot be retried by
                    # a later epoch.
                    self._consumed_anchor_seals.add(anchor.evidence_hash)
                    if isinstance(self.backend, X11Backend):
                        self.backend._consumed_anchor_seals.add(anchor.evidence_hash)
                    raise
                self._consumed_anchor_seals.add(anchor.evidence_hash)

            if isinstance(self.backend, X11Backend):
                try:
                    self.guardian.dispatch_admission(generation, admit_anchor)
                except Exception:
                    if anchor is not None:
                        self._consumed_anchor_seals.add(anchor.evidence_hash)
                        self.backend._consumed_anchor_seals.add(anchor.evidence_hash)
                    raise
                self._held_input = True
                if pointer_action:
                    if gesture_plan is None:
                        raise DriverError("native pointer action has no complete seeded gesture plan")
                    kwargs = {
                        **kwargs,
                        "_admitted_anchor": admitted_anchor,
                        "_pointer_plan": pointer_plan,
                        "_click_plan": click_plan,
                        "_gesture_plan": gesture_plan,
                        "_guardian_admission_id": (
                            f"{name}:{selected_action_generation}:{generation}:{id(self.guardian.action)}"
                        ),
                        "_action_generation": selected_action_generation,
                    }
                    if pointer_plan is not None:
                        if deadline_monotonic is None:
                            kwargs["_refresh_anchor"] = lambda current, previous: (
                                self._refresh_pointer_anchor(name, generation, current, previous)
                            )
                        else:
                            kwargs["_refresh_anchor"] = lambda current, previous: (
                                self._refresh_pointer_anchor(
                                    name,
                                    generation,
                                    current,
                                    previous,
                                    deadline_monotonic=deadline_monotonic,
                                )
                            )
                else:
                    # The emitter receives only the keymap sealed by current
                    # runtime evidence. It converts the planned keysyms to
                    # exact physical keycodes before its process is admitted.
                    kwargs = {
                        **kwargs,
                        "_keyboard_keymap": self._sealed_keyboard_keymap(
                            evidence,
                            deadline_monotonic=deadline_monotonic,
                        ),
                    }
                pointer_attempted = pointer_action and gesture_plan is not None
                if deadline_monotonic is not None:
                    kwargs = {**kwargs, "deadline_monotonic": deadline_monotonic}
                backend_deadline_previous = getattr(self.backend, "_active_action_deadline", None)
                if deadline_monotonic is not None:
                    self.backend._active_action_deadline = deadline_monotonic  # type: ignore[attr-defined]
                try:
                    backend_result = self.backend.action(name, generation=generation, **kwargs)
                finally:
                    if deadline_monotonic is not None:
                        if backend_deadline_previous is None:
                            try:
                                delattr(self.backend, "_active_action_deadline")
                            except AttributeError:
                                pass
                        else:
                            self.backend._active_action_deadline = backend_deadline_previous  # type: ignore[attr-defined]
                if name == "executor_click" and isinstance(backend_result, dict):
                    pointer_receipt = copy.deepcopy(backend_result)
            else:

                def dispatch_fake() -> None:
                    nonlocal pointer_receipt
                    admit_anchor()
                    self._held_input = True
                    final_admission = kwargs.get("_final_admission")
                    if final_admission is not None:
                        if not callable(final_admission):
                            raise DriverError("pointer final admission is not callable")
                        final_admission()
                    backend_kwargs = {
                        key: value for key, value in kwargs.items() if key != "_final_admission"
                    }
                    if deadline_monotonic is not None:
                        backend_kwargs["deadline_monotonic"] = deadline_monotonic
                    backend_deadline_previous = getattr(self.backend, "_active_action_deadline", None)
                    if deadline_monotonic is not None:
                        self.backend._active_action_deadline = deadline_monotonic  # type: ignore[attr-defined]
                    try:
                        self.backend.action(name, **backend_kwargs)
                    finally:
                        if deadline_monotonic is not None:
                            if backend_deadline_previous is None:
                                try:
                                    delattr(self.backend, "_active_action_deadline")
                                except AttributeError:
                                    pass
                            else:
                                self.backend._active_action_deadline = backend_deadline_previous  # type: ignore[attr-defined]
                    if name == "executor_click":
                        pointer_receipt = {
                            "action": name,
                            "generation": generation,
                            "anchorEvidenceHash": anchor.evidence_hash if anchor is not None else "",
                            "anchorCaptureId": anchor.capture_id if anchor is not None else "",
                            "endpoint": (
                                int(gesture_plan.endpoint.x),
                                int(gesture_plan.endpoint.y),
                            )
                            if gesture_plan is not None
                            else (
                                int(anchor.x),
                                int(anchor.y),
                            )
                            if anchor is not None
                            else None,
                            "rateGate": "canonical-user-session",
                            "pressRelease": "recording-backend",
                            "guardianCompleted": True,
                            "accepted": True,
                        }

                try:
                    self.guardian.dispatch_admission(generation, dispatch_fake)
                except Exception:
                    if anchor is not None:
                        self._consumed_anchor_seals.add(anchor.evidence_hash)
                    raise
            if isinstance(self.backend, X11Backend) and self._monitor is not None:
                monitor_timeout = 2.0
                if deadline_monotonic is not None:
                    monitor_timeout = min(
                        monitor_timeout,
                        max(0.0, deadline_monotonic - time.monotonic()),
                    )
                self._monitor.wait_for_event(
                    lambda _events: bool(self.guardian.action is not None and self.guardian.action.complete),
                    timeout=monitor_timeout,
                )
            fresh = self._identity(deadline_monotonic=deadline_monotonic)
            if fresh.fingerprint != identity.fingerprint:
                raise DriverError("game client identity changed during action")
            if (
                postcondition is None
                and isinstance(self.backend, X11Backend)
                and name != "type_allowlisted_command"
            ):
                raise DriverError(f"{name} requires actual postcondition evidence")
            self._check_deadline(deadline_monotonic, f"{name} deadline expired before postcondition")
            if postcondition is not None and not postcondition(fresh):
                raise DriverError(f"{name} postcondition failed")
            self._check_deadline(deadline_monotonic, f"{name} deadline expired after postcondition")
            pointer_succeeded = pointer_action
        except GuardianError as exc:
            raise DriverError(str(exc)) from exc
        except Exception as exc:
            if self.guardian.armed and self.guardian.generation == generation:
                self.revoke(f"{name} failed: {exc}")
            raise
        finally:
            finish_succeeded = False
            finish_error: Exception | None = None
            try:
                if action is not None and self.guardian.armed and self.guardian.generation == generation:
                    try:
                        self.guardian.finish_action(generation, action)
                        finish_succeeded = True
                        self._held_input = False
                    except Exception as exc:
                        finish_error = exc
            finally:
                if pointer_attempted and gesture_plan is not None:
                    # Executor receipts come only from this backend call's
                    # sealed return value.  Mutable backend history may have
                    # been replaced by a later action and is never consulted
                    # for the per-call result.
                    execution = copy.deepcopy(pointer_receipt)
                    if execution is None and name != "executor_click":
                        execution = (
                            copy.deepcopy(self.backend.last_pointer_execution)
                            if isinstance(self.backend, X11Backend)
                            else None
                        )
                    if execution is not None:
                        execution["guardianCompleted"] = finish_succeeded
                        execution["receiptFinalizedMonotonicNs"] = time.monotonic_ns()
                        if name == "executor_click":
                            pointer_receipt = copy.deepcopy(execution)
                    pointer_succeeded = pointer_succeeded and finish_succeeded
                    self._record_pointer_gesture_receipt(
                        action=name,
                        generation=generation,
                        gesture=gesture_plan,
                        accepted=pointer_succeeded,
                        execution=execution,
                    )
            if finish_error is not None and name != "executor_click":
                raise finish_error
            if finish_error is not None and name == "executor_click" and sys.exc_info()[0] is None:
                # Keep the sealed call-local receipt available to the daemon;
                # guardianCompleted=False makes it ineligible for success and
                # forces the daemon to archive a blocked known receipt.
                cleanup_return_receipt = copy.deepcopy(pointer_receipt)
        if cleanup_return_receipt is not None:
            return cleanup_return_receipt
        if name == "executor_click":
            return copy.deepcopy(pointer_receipt) if pointer_receipt is not None else None

    def flush_queued(self) -> None:
        queued, self._queued = self._queued, []
        for generation, name, kwargs in queued:
            self.run(name, generation, **kwargs)

    def run_takeover_hover(self, generation: int, evidence: RuntimeInputEvidence) -> HumanPointerPlan:
        """Emit one guarded clickless curved hover for the takeover drill."""

        self._last_takeover_hover_failure_context = None
        started_wall_ns = time.time_ns()
        started_monotonic_ns = time.monotonic_ns()
        stage = "validate_evidence"
        if not isinstance(self.backend, X11Backend) or not isinstance(evidence, RuntimeInputEvidence):
            raise DriverError("takeover hover requires live native runtime evidence")
        evidence.verify_seal()
        if self._input_evidence is not evidence:
            raise DriverError("takeover hover evidence is not the current armed runtime proof")
        self.guardian.check(generation)
        identity = self.resolve_identity(require_foreground=True)
        if (
            identity.fingerprint != evidence.game_identity_fingerprint
            or identity.process_start != evidence.game_process_start
            or tuple(identity.child_geometry) != evidence.game_geometry
        ):
            raise DriverError("takeover hover game identity changed after passive startup")
        xtest_profile = evidence.xtest_profile
        xtest_profile.verify()
        source = evidence.synthetic_pointer_source
        master = evidence.synthetic_pointer_master
        if source <= 0 or master <= 0 or not identity.target_event_xids:
            raise DriverError("takeover hover has no certified pointer source or target set")
        left, top, width, height = identity.child_geometry
        root_x, root_y, pointer_window = self.backend.pointer_position(generation=generation)
        if not self._is_owned_client_target(pointer_window, identity):
            raise DriverError("takeover hover pointer is outside the verified game client")
        start = Point(root_x - left, root_y - top).to_pixel()
        client_bounds = Rect(0, 0, width, height)
        if not client_bounds.contains(start):
            raise DriverError("takeover hover pointer is outside verified client pixels")
        target_width = min(96, width)
        target_height = min(80, height)
        target_left = min(max(0, int(start.x) - target_width // 2), width - target_width)
        target_top = min(max(0, int(start.y) - target_height // 2), height - target_height)
        binding = BindingMetadata(
            identity_fingerprint=identity.fingerprint,
            capture_id=(
                f"takeover:{evidence.readiness_boot}:{evidence.readiness_sequence}:"
                f"{evidence.environment_fingerprint}:{generation}"
            ),
            capture_sequence=evidence.readiness_sequence,
            generation=generation,
            target_xid=identity.child_xid,
            target_ancestry=tuple(identity.target_event_xids),
            source="passive-startup-client-interior",
        )
        target = PointerTarget(
            "takeover_hover",
            Rect(target_left, target_top, target_width, target_height),
            binding,
        )
        motion_profile = replace(
            self.pointer_motion_profile,
            min_duration=max(0.8, self.pointer_motion_profile.min_duration),
            max_duration=max(0.8, self.pointer_motion_profile.max_duration),
        )
        try:
            stage = "plan_motion"
            gesture = plan_human_pointer(start, target, client_bounds, button=None, profile=motion_profile)
            trajectory = gesture.trajectory
            if gesture.click is not None or trajectory.endpoint == start:
                raise DriverError("takeover hover plan is not a distinct clickless path")
            trace: list[tuple[int, int, int, int]] = []
            prior = trajectory.samples[0].point
            for sample in trajectory.samples[1:]:
                if sample.point == prior:
                    continue
                point = (left + int(sample.point.x), top + int(sample.point.y))
                trace.append((*point, *point))
                prior = sample.point
            expected_trace = tuple(trace)
            if not expected_trace:
                raise DriverError("takeover hover plan emitted no pointer movement")
            chain = build_pointer_motion_chain(trajectory, identity.child_geometry)
        except (ExecutorError, PointerExecutionError) as exc:
            raise DriverError(f"takeover hover plan is invalid: {exc}") from exc

        event_classes = {
            EventClass.DEVICE_CHANGED.value,
            EventClass.RAW_MOTION.value,
            EventClass.MOTION.value,
            EventClass.ENTER.value,
            EventClass.LEAVE.value,
        }
        optional_classes = {
            EventClass.DEVICE_CHANGED.value,
            EventClass.ENTER.value,
            EventClass.LEAVE.value,
        }
        device_by_class = {
            event_class: master
            if event_class in {EventClass.DEVICE_CHANGED.value, EventClass.RAW_MOTION.value}
            else source
            for event_class in event_classes
        }
        expected_origin = (root_x, root_y, root_x, root_y)
        monitor = self._monitor
        action = self.guardian.begin_action(
            generation,
            target_xid=identity.child_xid,
            source_device=source,
            master_device=master,
            event_classes=event_classes,
            device_by_class=device_by_class,
            target_ancestor=lambda xid: self._is_owned_client_target(xid, identity),
            required_sequence=(),
            optional_classes=optional_classes,
            expected_modifiers=self._expected_modifiers(evidence),
            expected_motion_trace=expected_trace,
            initial_origin=expected_origin,
            expected_root_xid=xtest_profile.root_xid,
            expected_event_xid=xtest_profile.root_xid,
            expected_child_xids=frozenset(identity.target_event_xids),
            canonical_motion=True,
            echo_sources=evidence.forwarded_echo_sources,
        )
        try:

            def admit_hover() -> None:
                self.guardian.check(generation)
                current = self.resolve_identity(require_foreground=True)
                if current.fingerprint != identity.fingerprint:
                    raise DriverError("takeover hover identity changed before dispatch")
                current_pointer = self.backend.pointer_position(generation=generation)
                if current_pointer != (root_x, root_y, pointer_window):
                    raise DriverError("takeover hover pointer moved before owned dispatch")

            stage = "dispatch_admission"
            self.guardian.dispatch_admission(generation, admit_hover)
            stage = "pointer_emission"
            self.backend._run(
                chain,
                generation=generation,
                input_schema="pointer-motion",
                expected_argv=tuple(chain),
            )
            if monitor is None or not monitor.alive:
                raise DriverError("takeover hover lost the registered XI2 observer")
            stage = "wait_for_motion_trace"
            monitor.wait_for_event(
                lambda _events: self.guardian.action is not None and self.guardian.action.complete,
                timeout=max(TAKEOVER_DRILL_HOVER_OBSERVATION_TIMEOUT_SECONDS, trajectory.duration + 1.0),
            )
            if self.guardian.action is not action or not action.complete:
                raise DriverError("takeover hover lacks a complete raw/delivered motion trace")
            stage = "verify_post_dispatch_identity"
            fresh = self.resolve_identity(require_foreground=True)
            if fresh.fingerprint != identity.fingerprint:
                raise DriverError("takeover hover game identity changed during dispatch")
            self.guardian.finish_action(generation, action)
            self._record_pointer_gesture_receipt(
                action="takeover_hover", generation=generation, gesture=gesture, accepted=True
            )
            return gesture
        except Exception as exc:

            def guardian_snapshot() -> dict[str, Any]:
                try:
                    return {
                        "generation": self.guardian.generation,
                        "armed": self.guardian.armed,
                        "held": self.guardian.held,
                        "revokedReason": self.guardian.revoked_reason,
                    }
                except Exception as snapshot_exc:
                    return {
                        "snapshotError": (
                            f"{type(snapshot_exc).__name__}: {' '.join(str(snapshot_exc).split())[:2048]}"
                        )
                    }

            guardian_before_cleanup = guardian_snapshot()
            context: dict[str, Any] = {
                "schema": "crowley.takeover-hover-failure",
                "schemaVersion": 1,
                "startedWallTimeNs": started_wall_ns,
                "failedWallTimeNs": time.time_ns(),
                "startedMonotonicNs": started_monotonic_ns,
                "failedMonotonicNs": time.monotonic_ns(),
                "stage": stage,
                "exceptionType": type(exc).__name__,
                "exception": " ".join(str(exc).split())[:8192],
                "guardianBeforeCleanup": guardian_before_cleanup,
                "identity": {
                    "fingerprint": identity.fingerprint,
                    "processStart": identity.process_start,
                    "clientXid": identity.child_xid,
                    "geometry": list(identity.child_geometry),
                    "targetEventXids": sorted(identity.target_event_xids),
                },
                "inputBinding": {
                    "runtimeGeneration": generation,
                    "environmentFingerprint": evidence.environment_fingerprint,
                    "syntheticPointerMaster": master,
                    "syntheticPointerSource": source,
                    "forwardedEchoSources": sorted(evidence.forwarded_echo_sources),
                },
                "expectedAction": {
                    "name": "takeover_hover",
                    "targetXid": action.target_xid,
                    "targetEventXids": sorted(action.expected_child_xids),
                    "sourceDevice": action.source_device,
                    "masterDevice": action.master_device,
                    "eventClasses": sorted(action.event_classes),
                    "expectedMotionTrace": [list(row) for row in action.expected_motion_trace],
                    "expectedMotionCount": len(action.expected_motion_trace),
                    "motionIndex": action.motion_index,
                    "sequenceIndex": action.sequence_index,
                    "pendingEchoCount": len(action.pending_echoes),
                    "rawMotionPending": bool(action.raw_motion_pairing.pending),
                    "complete": action.complete,
                    "argv": list(chain),
                    "gesture": {
                        "seed": gesture.seed,
                        "endpoint": [int(gesture.endpoint.x), int(gesture.endpoint.y)],
                        "movementDuration": gesture.movement_duration,
                        "dwell": gesture.dwell,
                        "submovementCount": len(gesture.waypoints),
                    },
                },
            }
            process = getattr(getattr(monitor, "observer", None), "process", None)
            observer_pid = getattr(process, "pid", None)
            if isinstance(observer_pid, int) and not isinstance(observer_pid, bool) and observer_pid > 0:
                context["observerProcessId"] = observer_pid
            cleanup_error: str | None = None
            if self.guardian.armed and self.guardian.generation == generation:
                try:
                    self.revoke("takeover hover failed before its motion trace completed")
                except Exception as cleanup_exc:
                    cleanup_error = (
                        f"{type(cleanup_exc).__name__}: {' '.join(str(cleanup_exc).split())[:2048]}"
                    )
            context["guardianAfterCleanup"] = guardian_snapshot()
            # The native process can report signal termination while the
            # observer's revocation callback is still finishing on its reader
            # thread. Record what was true at this failure snapshot; the
            # daemon adds a separate post-cleanup ownership result.
            context["observerRegisteredAtFailureCapture"] = self._monitor is monitor
            if monitor is not None:
                try:
                    context["observerAliveAtFailureCapture"] = bool(monitor.alive)
                except Exception as snapshot_exc:
                    context["observerAliveSnapshotError"] = (
                        f"{type(snapshot_exc).__name__}: {' '.join(str(snapshot_exc).split())[:2048]}"
                    )
            if cleanup_error is not None:
                context["driverCleanupError"] = cleanup_error
            snapshot = getattr(monitor, "diagnostic_snapshot", None)
            if callable(snapshot):
                try:
                    context["observer"] = snapshot()
                except Exception as snapshot_exc:
                    context["observerSnapshotError"] = (
                        f"{type(snapshot_exc).__name__}: {' '.join(str(snapshot_exc).split())[:2048]}"
                    )
            self._last_takeover_hover_failure_context = context
            raise

    def prepare_command(
        self,
        generation: int,
        command: str,
        telemetry: dict[str, Any],
        *,
        readback: Callable[[], str] | None = None,
        precondition: Callable[[WindowIdentity], bool] | None = None,
        postcondition: Callable[[WindowIdentity], bool] | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        if command not in self.COMMANDS:
            raise DriverError("command is not allowlisted")
        edit = telemetry.get("chatEdit")
        if not isinstance(edit, dict) or edit != {"active": True, "focused": True, "text": ""}:
            raise DriverError("chat edit box is not a fresh focused empty buffer")
        if readback is None:
            raise DriverError("typed command has no fresh native EditBox readback")
        self.run(
            "type_allowlisted_command",
            generation,
            command=command,
            precondition=precondition,
            deadline_monotonic=deadline_monotonic,
        )
        self._check_deadline(deadline_monotonic, "command deadline expired before EditBox readback")
        observed = readback()
        self._check_deadline(deadline_monotonic, "command deadline expired after EditBox readback")
        if observed != command:
            # Even cleanup of an unexpected EditBox value follows the same
            # measured XTEST path and receives a fresh readback pre/postcondition.
            try:
                self.run(
                    "escape_command_buffer",
                    generation,
                    precondition=lambda identity: readback() == observed,
                    postcondition=lambda identity: readback() == "",
                    deadline_monotonic=deadline_monotonic,
                )
            finally:
                self.revoke("active EditBox readback mismatch")
            raise DriverError("active EditBox text did not match requested command")
        self.run(
            "submit_command",
            generation,
            command=command,
            precondition=precondition,
            postcondition=postcondition,
            deadline_monotonic=deadline_monotonic,
        )

    def capture_client_crop(
        self,
        path: str | os.PathLike[str],
        *,
        model: bool = False,
        deadline_monotonic: float | None = None,
    ) -> Path:
        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        target = Path(path)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = self._identity(deadline_monotonic=deadline_monotonic)
        if isinstance(self.backend, X11Backend):
            if deadline_monotonic is None:
                captured = self.backend.capture(target, identity=identity)
            else:
                captured = self.backend.capture(
                    target, identity=identity, deadline_monotonic=deadline_monotonic
                )
        else:
            captured = self.backend.capture(target, identity=identity)
        if captured is None or captured.fingerprint != identity.fingerprint:
            raise DriverError("capture identity does not match the verified client")
        return self._crop_capture_file(target, identity, model=model)

    def _crop_capture_file(self, target: Path, identity: WindowIdentity, *, model: bool) -> Path:
        try:
            from PIL import Image

            with Image.open(target) as image:
                image.load()
                left, top, _, _ = identity.child_geometry
                if image.size == (self.profile.client_width, self.profile.client_height):
                    if (left, top) != (0, 0):
                        raise DriverError("client-sized capture cannot prove a nonzero child origin")
                    base_x, base_y = 0, 0
                else:
                    base_x, base_y = left, top
                crop = self.profile.model_crop if model else self.profile.canvas_crop
                x, y, width, height = crop
                if (
                    x < 0
                    or y < 0
                    or x + width > self.profile.client_width
                    or y + height > self.profile.client_height
                ):
                    raise DriverError("capture crop is outside verified client area")
                if (
                    base_x + self.profile.client_width > image.width
                    or base_y + self.profile.client_height > image.height
                ):
                    raise DriverError("desktop capture does not contain the verified game child")
                output = target.with_suffix(".crop.png")
                image.crop((base_x + x, base_y + y, base_x + x + width, base_y + y + height)).save(
                    output, format="PNG"
                )
            os.chmod(output, 0o600)
            return output
        except ImportError as exc:
            raise DriverError("Pillow is required for lossless capture decoding") from exc

    def capture_client_crop_for_identity(
        self,
        identity: WindowIdentity,
        *,
        destination: Path | None = None,
        deadline_monotonic: float | None = None,
    ) -> Path:
        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        target = destination or (Path(tempfile.mkdtemp(prefix="crowley-capture-")) / "desktop.png")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if isinstance(self.backend, X11Backend):
            if deadline_monotonic is None:
                captured = self.backend.capture(target, identity=identity)
            else:
                captured = self.backend.capture(
                    target, identity=identity, deadline_monotonic=deadline_monotonic
                )
        else:
            captured = self.backend.capture(target, identity=identity)
        if captured is None or captured.fingerprint != identity.fingerprint:
            raise DriverError("screenshot is not bound to the verified client identity")
        return self._crop_capture_file(target, identity, model=False)

    def capture_native_region_for_identity(
        self,
        identity: WindowIdentity,
        *,
        destination: Path | None = None,
        deadline_monotonic: float | None = None,
        observer_admission: Callable[[Callable[[], Any]], Any] | None = None,
        observer_reaped: Callable[[int], None] | None = None,
        _pre_ack_admission: Callable[[int, Callable[[], None]], None] | None = None,
        _receipt_sink: Callable[[TelemetryRegionReceipt], None] | None = None,
        _allow_unobserved_fixture: bool = False,
    ) -> Path:
        """Capture the exact native status rectangle without selecting model crops."""

        deadline_monotonic = self._validate_deadline(deadline_monotonic)
        crop_left, crop_top, crop_width, crop_height = self.profile.canvas_crop
        rectangle = (
            identity.child_geometry[0] + crop_left,
            identity.child_geometry[1] + crop_top,
            crop_width,
            crop_height,
        )
        target = destination or (Path(tempfile.mkdtemp(prefix="crowley-region-")) / "status.png")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not isinstance(self.backend, X11Backend):
            if _allow_unobserved_fixture is not True:
                raise DriverError("native region capture requires the configured X11 backend")
            region_capture = getattr(self.backend, "capture_native_region", None)
            if not callable(region_capture):
                raise DriverError("native region capture requires the configured X11 producer")
            fixture_kwargs: dict[str, Any] = {"identity": identity, "rectangle": rectangle}
            if _pre_ack_admission is not None:
                fixture_kwargs["_pre_ack_admission"] = _pre_ack_admission
            captured = region_capture(target, **fixture_kwargs)
        else:
            capture_kwargs: dict[str, Any] = {
                "identity": identity,
                "rectangle": rectangle,
                "deadline_monotonic": deadline_monotonic,
                "observer_admission": observer_admission,
                "observer_reaped": observer_reaped,
                "_allow_unobserved_fixture": _allow_unobserved_fixture,
            }
            if _pre_ack_admission is not None:
                capture_kwargs["_pre_ack_admission"] = _pre_ack_admission
            if _receipt_sink is not None:
                capture_kwargs["_receipt_sink"] = _receipt_sink
            captured = self.backend.capture_native_region(
                target,
                **capture_kwargs,
            )
        if captured is None or captured.fingerprint != identity.fingerprint:
            raise DriverError("native region capture identity does not match the verified client")
        return target

    def new_native_decoder(self) -> Any:
        try:
            from .decoder import PixelDecoder

            return PixelDecoder(crop=self.profile.canvas_crop)
        except ImportError as exc:
            raise DriverError("native decoder dependencies are unavailable") from exc

    def decode_native_capture(
        self, path: Path, *, identity: WindowIdentity, decoder: Any | None = None
    ) -> Any:
        try:
            from PIL import Image

            from .decoder import PixelDecoder

            with Image.open(path) as image:
                image.load()
                if image.size != (self.profile.canvas_crop[2], self.profile.canvas_crop[3]):
                    raise DriverError("native capture is not the calibrated status crop")
                active_decoder = decoder or PixelDecoder(crop=self.profile.canvas_crop)
                return active_decoder.decode(
                    image, monotonic=self._clock(), window_identity=identity.fingerprint
                )
        except ImportError as exc:
            raise DriverError("Pillow is required for native calibration evidence") from exc

    def native_calibration(self, measurements: Any | None = None) -> Any:
        from .calibration import NativeCalibrationRunner

        if measurements is not None:
            raise DriverError("native calibration metrics must come from the live measurement provider")
        try:
            return NativeCalibrationRunner(self).run()
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(str(exc)) from exc

    def input_calibration(self, evidence: Any | None = None, **legacy: Any) -> str:
        if legacy:
            raise DriverError("source sets and caller booleans cannot arm input")
        try:
            evidence.verify_seal()
        except Exception as exc:
            raise DriverError("input calibration must come from the owned live workflow") from exc
        self._input_evidence = evidence
        return evidence.fingerprint

    def calibrate_input_events(self, output: str, **legacy: Any) -> Any:
        if legacy:
            raise DriverError("caller-supplied source/correlation flags are not calibration evidence")
        try:
            parse_xi2_records(output, require_event=True)
        except InputEventError as exc:
            raise DriverError(str(exc)) from exc
        raise DriverError("captured trace alone cannot arm; run owned input calibration")

    def synthetic_target_keyboard_profile(
        self,
        target: Any,
        *,
        operations: tuple[Any, ...],
        calibration_ticket: CalibrationTicket | None = None,
    ) -> SyntheticPointerProbe:
        """Capture the runtime keyboard profile after one human-planned click."""

        from .calibration import KeyboardProbeOperation, OwnedGtkCalibrationWindow, OwnedWindow

        if not isinstance(self.backend, X11Backend):
            raise DriverError("owned GTK keyboard calibration requires the X11 backend")
        if not isinstance(target, OwnedWindow) or target.title != OwnedGtkCalibrationWindow.TITLE:
            raise DriverError("keyboard calibration target is not the owned GTK window")
        if not isinstance(calibration_ticket, CalibrationTicket):
            raise DriverError("keyboard calibration requires an exact calibration ticket")
        if not operations or any(not isinstance(item, KeyboardProbeOperation) for item in operations):
            raise DriverError("keyboard calibration operations are malformed")
        owned_operations = {
            (item.operation, item.payload): item.steps for item in self.calibration_keyboard_operations()
        }
        if any(
            (item.operation, item.payload) not in owned_operations
            or item.steps != owned_operations[(item.operation, item.payload)]
            for item in operations
        ):
            raise DriverError("keyboard calibration operations are not the owned runtime profile")
        self._verify_calibration_target(calibration_ticket, target)
        if not self.guardian.calibration_ticket_current(calibration_ticket):
            raise DriverError("calibration ticket is stale or revoked")
        props = self.backend._xprop(hex(target.xid), "WM_NAME", "_NET_WM_PID")
        if target.title not in props or not re.search(rf"_NET_WM_PID\([^)]*\).*\b{target.pid}\b", props):
            raise DriverError("owned GTK calibration XID/PID changed")
        keymap = self.calibration_keyboard_keymap()
        left, top, width, height = target.geometry
        origin_x, origin_y, origin_window = self.backend.pointer_position()
        if not calibration_ticket.contains(origin_window):
            raise DriverError("calibration pointer origin is outside the owned target")
        if not left <= origin_x < left + width or not top <= origin_y < top + height:
            raise DriverError("calibration pointer origin is outside the owned target")
        binding = BindingMetadata(
            identity_fingerprint=f"calibration:{calibration_ticket.target_pid}:{calibration_ticket.target_xid}",
            capture_id=f"calibration:{calibration_ticket.epoch}",
            capture_sequence=calibration_ticket.epoch,
            generation=calibration_ticket.epoch,
            target_xid=target.xid,
            target_ancestry=tuple(sorted(calibration_ticket.target_xids)),
            source="owned-gtk-calibration",
        )
        client_bounds = Rect(0, 0, width, height)
        pointer_target = PointerTarget("input_calibration", client_bounds, binding)
        try:
            seed = secrets.randbits(64)
            gesture = plan_human_pointer(
                Point(origin_x - left, origin_y - top),
                pointer_target,
                client_bounds,
                seed=seed,
                button=PointerButton.LEFT,
                profile=self.pointer_motion_profile,
            )
            motion_chain = build_pointer_motion_chain(gesture.trajectory, target.geometry)
        except (ExecutorError, PointerExecutionError) as exc:
            raise DriverError(f"calibration pointer plan is invalid: {exc}") from exc
        if gesture.click is None:
            raise DriverError("calibration pointer plan has no press/release phase")
        if gesture.endpoint == gesture.trajectory.samples[0].point:
            raise DriverError("calibration pointer plan has zero endpoint displacement")
        emitted_transitions: list[tuple[int, int]] = []
        prior = gesture.trajectory.samples[0].point
        for sample in gesture.trajectory.samples[1:]:
            if sample.point == prior:
                continue
            emitted_transitions.append((left + int(sample.point.x), top + int(sample.point.y)))
            prior = sample.point
        if not emitted_transitions:
            raise DriverError("calibration pointer plan emitted no motion transitions")
        plan = tuple(
            (sample.at, left + int(sample.point.x), top + int(sample.point.y))
            for sample in gesture.trajectory.samples
        )
        self.backend._run(
            ["xdotool", "key", "F13"],
            calibration_ticket=calibration_ticket,
            held_keys={"F13"},
        )
        self.backend._run(
            motion_chain,
            calibration_ticket=calibration_ticket,
            input_schema="pointer-motion",
            expected_argv=tuple(motion_chain),
        )
        post_x, post_y, post_window = self.backend.pointer_position()
        requested_endpoint = (
            left + int(gesture.endpoint.x),
            top + int(gesture.endpoint.y),
        )
        if (
            not calibration_ticket.contains(post_window)
            or not left <= post_x < left + width
            or not top <= post_y < top + height
            or (post_x, post_y) != requested_endpoint
        ):
            raise DriverError("calibration pointer postcondition changed")
        click_argv = [
            "xdotool",
            "mousedown",
            "1",
            "sleep",
            f"{gesture.click.dwell:.6f}",
            "mouseup",
            "1",
        ]
        destination_anchor = f"{target.xid}:{requested_endpoint[0]}:{requested_endpoint[1]}"
        guardian_admission_id = f"calibration:{calibration_ticket.epoch}:{target.xid}"
        try:
            reservation = self.input_broker.reserve_button_rate()
            try:
                admitted_gesture = self.input_broker.mint_admitted_gesture(
                    action_generation=calibration_ticket.epoch,
                    lease_generation=calibration_ticket.epoch,
                    button=PointerButton.LEFT,
                    emitted_chain=tuple(click_argv),
                    destination_anchor=destination_anchor,
                    target_identity=f"{target.pid}:{target.xid}",
                    guardian_admission_id=guardian_admission_id,
                    semantic_authorization="input_calibration",
                    reservation=reservation,
                )
                self.input_broker.emit_button_gesture(
                    admitted_gesture,
                    emitted_chain=tuple(click_argv),
                    action_generation=calibration_ticket.epoch,
                    lease_generation=calibration_ticket.epoch,
                    destination_anchor=destination_anchor,
                    target_identity=f"{target.pid}:{target.xid}",
                    guardian_admission_id=guardian_admission_id,
                    semantic_authorization="input_calibration",
                    emit=lambda claim: self.backend._run(
                        click_argv,
                        calibration_ticket=calibration_ticket,
                        held_buttons={1},
                        input_schema="single-click",
                        expected_argv=tuple(click_argv),
                        button_emission_claim=claim,
                        emission_action_generation=calibration_ticket.epoch,
                        emission_lease_generation=calibration_ticket.epoch,
                    ),
                )
            finally:
                self.input_broker.release_reservation(reservation)
        except InputBrokerError as exc:
            raise DriverError(f"calibration button admission failed: {exc}") from exc
        probe = SyntheticPointerProbe(
            origin=(origin_x, origin_y, origin_window),
            requested_endpoint=requested_endpoint,
            final_observed=(post_x, post_y, post_window),
            emitted_transitions=tuple(emitted_transitions),
            seed=seed,
            plan=plan,
        )
        self.backend.synthetic_keyboard_profile(
            operations,
            keymap=keymap,
            calibration_ticket=calibration_ticket,
        )
        return probe

    def close(self) -> None:
        self.revoke("driver closed")
        self.clear_input_calibration_ready()
        if self._monitor is not None:
            self.stop_input_observer()
        for pid in tuple(self._calibration_target_process_groups):
            if Path(f"/proc/{pid}").exists():
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
                deadline = time.monotonic() + 0.5
                while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if Path(f"/proc/{pid}").exists():
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
            if not Path(f"/proc/{pid}").exists():
                self.guardian.unregister_process_group(pid)
                self._calibration_target_process_groups.discard(pid)
        for pid in tuple(self._calibration_bootstrap_process_groups):
            if Path(f"/proc/{pid}").exists():
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
                deadline = time.monotonic() + 0.5
                while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if Path(f"/proc/{pid}").exists():
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
            if not Path(f"/proc/{pid}").exists():
                self.guardian.unregister_process_group(pid)
                self._calibration_bootstrap_process_groups.discard(pid)
        # Preserve the pinned image when guardian cleanup fails.  A caller can
        # retry physical release through the same immutable capability; closing
        # it here would make the retry path silently unavailable.
        self.guardian.close()
        if isinstance(self.backend, X11Backend):
            self.backend.input_tool.close()
            self.backend.close()


__all__ = [
    "Anchor",
    "Backend",
    "CalibrationTicket",
    "DesktopDriver",
    "DriverError",
    "DriverProfile",
    "InputGuardian",
    "InputMonitor",
    "NamedStateObservation",
    "MeasuredAnchor",
    "RecordingBackend",
    "SyntheticPointerProbe",
    "WindowIdentity",
    "XI2Observer",
    "X11Backend",
    "build_camera_drag_chain",
    "build_pointer_motion_chain",
]
