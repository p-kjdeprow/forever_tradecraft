"""Crash-safe, process-shared spacing of automated clicks (at least 1.0 s apart)."""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from crowley.gui.pointer import PointerExecutionError

CLICK_INTERVAL_NS = 1_000_000_000


_GO_LATENCY_RESERVE_NS = 5_000_000_000


_MAX_GATE_WAIT_SECONDS = 8.0


_MAX_STATE_FUTURE_NS = 10_000_000_000


_PERMIT_ISSUER = object()


def _boot_identity() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise PointerExecutionError("kernel boot identity is unavailable") from exc
    if len(value) != 36 or any(ch not in "0123456789abcdef-" for ch in value.lower()):
        raise PointerExecutionError("kernel boot identity is malformed")
    return value.lower()


def _session_state_directory() -> Path:
    uid = os.getuid()
    runtime = Path("/run/user") / str(uid)
    try:
        runtime_stat = runtime.lstat()
    except OSError as exc:
        raise PointerExecutionError("canonical user runtime directory is unavailable") from exc
    if (
        stat.S_ISLNK(runtime_stat.st_mode)
        or not stat.S_ISDIR(runtime_stat.st_mode)
        or runtime_stat.st_uid != uid
        or runtime_stat.st_mode & 0o077
    ):
        raise PointerExecutionError("canonical user runtime directory is unsafe")
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        try:
            configured_path = Path(configured).resolve(strict=True)
        except OSError as exc:
            raise PointerExecutionError("configured runtime identity cannot be verified") from exc
        if configured_path != runtime:
            raise PointerExecutionError("configured runtime differs from the canonical user session")
    state_dir = runtime / "crowley"
    try:
        state_dir.mkdir(mode=0o700, exist_ok=True)
        info = state_dir.lstat()
    except OSError as exc:
        raise PointerExecutionError("canonical pointer timing directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & 0o077
    ):
        raise PointerExecutionError("canonical pointer timing directory is unsafe")
    return state_dir


def _secure_lock_fd(directory: Path) -> int:
    path = directory / "pointer-click-rate.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
    except OSError as exc:
        raise PointerExecutionError("pointer timing lock cannot be opened") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PointerExecutionError("pointer timing lock has unsafe ownership or mode")
    return fd


def _read_state(directory: Path) -> dict[str, object] | None:
    path = directory / "pointer-click-rate.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PointerExecutionError("pointer timing state cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PointerExecutionError("pointer timing state has unsafe ownership or mode")
        chunks = bytearray()
        while len(chunks) <= 4096:
            data = os.read(fd, 4097 - len(chunks))
            if not data:
                break
            chunks.extend(data)
        if len(chunks) > 4096:
            raise PointerExecutionError("pointer timing state exceeds its bound")
    finally:
        os.close(fd)
    try:
        state = json.loads(chunks.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PointerExecutionError("pointer timing state is malformed") from exc
    expected = {"version", "bootId", "nextAllowedMonotonicNs", "lastPressMonotonicNs"}
    if not isinstance(state, dict) or set(state) != expected or state.get("version") != 1:
        raise PointerExecutionError("pointer timing state schema is unknown")
    if not isinstance(state.get("bootId"), str):
        raise PointerExecutionError("pointer timing boot identity is malformed")
    next_allowed = state.get("nextAllowedMonotonicNs")
    last_press = state.get("lastPressMonotonicNs")
    if isinstance(next_allowed, bool) or not isinstance(next_allowed, int) or next_allowed < 0:
        raise PointerExecutionError("pointer timing deadline is malformed")
    if last_press is not None and (
        isinstance(last_press, bool) or not isinstance(last_press, int) or last_press < 0
    ):
        raise PointerExecutionError("pointer timing press record is malformed")
    return state


def _write_state(directory: Path, state: dict[str, object]) -> None:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("ascii")
    fd = -1
    temporary: str | None = None
    dir_fd = -1
    try:
        fd, temporary = tempfile.mkstemp(prefix=".pointer-click-rate.", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "pointer-click-rate.json")
        temporary = None
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(dir_fd)
    except OSError as exc:
        raise PointerExecutionError("durable pointer timing state publication failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if dir_fd >= 0:
            os.close(dir_fd)


class ClickRatePermit:
    """Exclusive timing permit held from pre-capture wait through release."""

    def __init__(
        self,
        fd: int,
        directory: Path,
        boot_id: str,
        *,
        acquired_monotonic: float,
        _issuer: object,
    ):
        if _issuer is not _PERMIT_ISSUER:
            raise PointerExecutionError("click timing permits are issued only by the canonical gate")
        if not math.isfinite(acquired_monotonic):
            raise PointerExecutionError("click timing permit acquisition time is malformed")
        self._fd = fd
        self._directory = directory
        self._boot_id = boot_id
        self._acquired_monotonic = float(acquired_monotonic)
        self._precommitted = False
        self._deadline_blocked = False
        self._recorded = False
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed and self._fd >= 0

    @property
    def acquired_monotonic(self) -> float:
        """Monotonic time at which this permit was issued by the rate gate."""

        return self._acquired_monotonic

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        if self._closed or self._precommitted:
            raise PointerExecutionError("click timing permit is closed or already committed")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise PointerExecutionError("click timing deadline is malformed")
        if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
            raise PointerExecutionError("click timing deadline expired before durable precommit")
        now = time.monotonic_ns()
        state = {
            "version": 1,
            "bootId": self._boot_id,
            "nextAllowedMonotonicNs": now + _GO_LATENCY_RESERVE_NS + CLICK_INTERVAL_NS,
            "lastPressMonotonicNs": None,
        }
        prior = _read_state(self._directory)
        if prior is not None and prior["bootId"] == self._boot_id:
            last_press = prior["lastPressMonotonicNs"]
            state["lastPressMonotonicNs"] = last_press
        _write_state(self._directory, state)
        self._precommitted = True
        if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
            # Keep the conservative durable reservation.  The caller must
            # refuse the physical press, but may not roll timing state back.
            self._deadline_blocked = True
            raise PointerExecutionError("click timing deadline expired after durable precommit")
        return now

    def record_press(self) -> int:
        if self._closed or not self._precommitted or self._recorded or self._deadline_blocked:
            raise PointerExecutionError("click timing press cannot be recorded in this permit state")
        actual = time.monotonic_ns()
        state = {
            "version": 1,
            "bootId": self._boot_id,
            "nextAllowedMonotonicNs": actual + CLICK_INTERVAL_NS,
            "lastPressMonotonicNs": actual,
        }
        _write_state(self._directory, state)
        self._recorded = True
        return actual

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> ClickRatePermit:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def acquire_click_rate_permit(
    *,
    deadline_monotonic: float | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> ClickRatePermit:
    """Wait for and hold the canonical session click gate, failing closed."""

    directory = _session_state_directory()
    boot_id = _boot_identity()
    fd = _secure_lock_fd(directory)
    wait_deadline = time.monotonic() + _MAX_GATE_WAIT_SECONDS
    if deadline_monotonic is not None:
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            os.close(fd)
            raise PointerExecutionError("click rate deadline is malformed")
        wait_deadline = min(wait_deadline, float(deadline_monotonic))
    try:
        while True:
            if cancellation_check is not None:
                cancellation_check()
            if time.monotonic() >= wait_deadline:
                raise PointerExecutionError("click rate gate wait exceeded its bounded deadline")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.025, max(0.0, wait_deadline - time.monotonic())))
        while True:
            if cancellation_check is not None:
                cancellation_check()
            now = time.monotonic_ns()
            state = _read_state(directory)
            if state is None or state["bootId"] != boot_id:
                return ClickRatePermit(
                    fd,
                    directory,
                    boot_id,
                    acquired_monotonic=time.monotonic(),
                    _issuer=_PERMIT_ISSUER,
                )
            next_allowed = state["nextAllowedMonotonicNs"]
            assert isinstance(next_allowed, int)
            if next_allowed - now > _MAX_STATE_FUTURE_NS:
                raise PointerExecutionError("pointer timing deadline is implausibly far in the future")
            if now >= next_allowed:
                return ClickRatePermit(
                    fd,
                    directory,
                    boot_id,
                    acquired_monotonic=time.monotonic(),
                    _issuer=_PERMIT_ISSUER,
                )
            if time.monotonic() >= wait_deadline:
                raise PointerExecutionError("click rate spacing wait exceeded its bounded deadline")
            if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
                raise PointerExecutionError("click spacing wait cannot fit before its deadline")
            time.sleep(min(0.025, max(0.0, (next_allowed - now) / 1_000_000_000)))
    except BaseException:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        raise
