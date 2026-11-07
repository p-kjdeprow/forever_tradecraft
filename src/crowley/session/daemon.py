"""Background capture, evidence, and bounded action loop for the user service."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, is_dataclass, replace
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any

from crowley.core import savedvars
from crowley.core.game_target import packaged_game_target, target_scan_plan
from crowley.core.watchlist import load_catalog

from . import protocol
from .calibration import (
    LinuxProcStatusVmHwmReset5,
    NativeCalibrationRunner,
    NativeMemoryArchiveReceipt,
    NativeMemoryAssessmentArchiveReceipt,
    NativeMemoryMeasurement,
    NativeMemoryMeasurementArchiveReceipt,
    NativePreconstructionEvidence,
    NativeRenderObservation,
    NativeRestorationReceipt,
)
from .config import SessionConfig, open_routes_descriptor
from .controller import Controller, ControllerState
from .decoder import PixelDecoder, Sample
from .driver import (
    _DEFAULT_CAPTURE_TIMEOUT_SECONDS,
    DesktopDriver,
    DriverError,
    MeasuredAnchor,
    NamedStateObservation,
    WindowIdentity,
    X11Backend,
    _identity_latency_barrier,
    _identity_latency_instant,
    _identity_latency_mark,
    _identity_latency_span,
    _proc_start,
    _reset_identity_latency_sink,
    _set_identity_latency_sink,
)
from .evidence import (
    EvidenceError,
    queues_empty,
    read_data_acknowledged_ids,
    read_stable_saved_scan,
    read_uploads,
)
from .executor import (
    BindingMetadata,
    ExecutorError,
    FreshObservation,
    KeyRole,
    Point,
    Rect,
    WaitPolicy,
    accepts_observation,
    observations_settled,
    plan_camera_drag,
    plan_context_wait,
)
from .guardian import GuardianError
from .input_broker import InputBrokerError
from .journal import Journal, JournalError, stable_file
from .mail_loop import (
    MailBudget,
    MailLoopBinding,
    MailLoopBoundary,
    MailOperationBounds,
    admit_mail_cycle,
)
from .model import LunaAdapter, ModelError
from .motion import (
    DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS,
    MAX_BASIS_DISPLACEMENT,
    MAX_CAMERA_CALIBRATION_PIXELS,
    MAX_CAMERA_CALIBRATION_YAW,
    MAX_CAMERA_INPUT_YAW,
    MAX_CAMERA_RESPONSE_RELATIVE_ERROR,
    MAX_CAMERA_YAW_PER_PIXEL,
    MAX_CONFIRMATION_FACING_DRIFT,
    MAX_CONFIRMATION_POSITION_DRIFT,
    MAX_FRAME_AGE,
    MAX_PULSE_SECONDS,
    MAX_WAYPOINT_CAMERA_TURNS,
    MAX_WAYPOINT_DISTANCE,
    MAX_WAYPOINT_PULSES,
    MAX_WAYPOINT_SECONDS,
    MIN_BASIS_DISPLACEMENT,
    MIN_CAMERA_CALIBRATION_PIXELS,
    MIN_CAMERA_RESPONSE_ABSOLUTE_ERROR,
    MIN_MOVEMENT_CALIBRATION_PULSE_SECONDS,
    MOTION_CALIBRATION_OPERATION_SECONDS,
    MOTION_CAMERA_OPERATION_SECONDS,
    MOTION_KEY_OPERATION_SECONDS,
    MOTION_POST_RELEASE_RESERVE_SECONDS,
    TRAVEL_UNCERTAINTY_FRACTION,
    WAYPOINT_TRANSLATION_HEADING_TOLERANCE,
    CameraMapping,
    MotionError,
    MotionFrame,
    MovementBasis,
    NativeMotionProvider,
    RouteEvidence,
    ViewportEvidence,
    normalize_angle,
    waypoint_heading_error,
)
from .preferences import load_motion_profile
from .protocol import ProtocolError, unsigned_delta
from .route_walk import KINEMATICS_PROVENANCE, Pose, RouteFile, check_fix, plan_segment, walk_budget
from .startup import (
    PASSIVE_STARTUP_TIMEOUT_SECONDS,
    TAKEOVER_DRILL_HUMAN_TIMEOUT_SECONDS,
    TAKEOVER_DRILL_RELEASE_TIMEOUT_SECONDS,
    InvalidationCause,
    RuntimeInputEvidence,
    TakeoverDrillRecord,
    is_measured_human_action,
)

_LOGGER = logging.getLogger(__name__)
_TARGET = packaged_game_target()
_CANONICAL_PLAN = target_scan_plan(load_catalog(), _TARGET)


def _canonical_semantic_value(value: Any) -> Any:
    """Convert dataclass configuration values to stable JSON primitives."""

    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, dict):
        return {str(key): _canonical_semantic_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_semantic_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported session config value: {type(value).__name__}")


def _strict_native_absence_image(path: Path, *, expected_size: tuple[int, int]) -> bool:
    """Return true only for an ordinary crop with no encoded Agent grid."""

    from PIL import Image

    try:
        with Image.open(path) as image:
            image.load()
            if image.size != expected_size or image.mode not in {"RGB", "RGBA", "L"}:
                return False
            # The wire format starts with the protocol magic.  Recover each
            # bit from the physical-cell majority, so a damaged or occluded
            # payload cannot be relabelled as an ordinary absence crop while
            # a genuine ordinary screenshot remains outside the protocol
            # shape.  A fully binary grid remains an encoded-looking malformed
            # surface even when its header was destroyed.
            bits: list[int | None] = []
            flat_bits: list[int | None] = []
            flat_cells = 0
            black_cells = 0
            white_cells = 0
            for y in range(128):
                for x in range(128):
                    raw_pixels = [image.getpixel((x * 2 + dx, y * 2 + dy)) for dy in (0, 1) for dx in (0, 1)]
                    pixels = [
                        value if isinstance(value, tuple) else (value, value, value) for value in raw_pixels
                    ]
                    colors = [pixel[:3] for pixel in pixels]
                    black = sum(color == (0, 0, 0) for color in colors)
                    white = sum(color == (255, 255, 255) for color in colors)
                    if black == 4:
                        flat_cells += 1
                        black_cells += 1
                        bits.append(0)
                        flat_bits.append(0)
                    elif white == 4:
                        flat_cells += 1
                        white_cells += 1
                        bits.append(1)
                        flat_bits.append(1)
                    elif black >= 3 and black > white:
                        bits.append(0)
                        flat_bits.append(None)
                    elif white >= 3 and white > black:
                        bits.append(1)
                        flat_bits.append(None)
                    else:
                        bits.append(None)
                        flat_bits.append(None)

            def recovered_byte(offset: int) -> int | None:
                """Recover one protocol byte when all eight cells survived."""

                group = bits[offset * 8 : (offset + 1) * 8]
                if len(group) != 8 or any(bit is None for bit in group):
                    return None
                return sum(int(bit) << index for index, bit in enumerate(group))

            header_bits = bits[:32]
            # Treat damaged cells as erasures, but require one complete
            # protocol-magic byte to survive.  CRS1's four fixed bytes are a
            # structural proof tied to the wire format, rather than a global
            # intact-cell percentage: the parent counterexample erases the
            # first byte while leaving the remaining three available.
            encoded_header = False
            for offset, expected in enumerate(b"CRS1"):
                group = header_bits[offset * 8 : (offset + 1) * 8]
                if any(bit is None for bit in group):
                    continue
                value = sum(int(bit) << index for index, bit in enumerate(group))
                if value == expected:
                    encoded_header = True
                    break
            # The fixed geometry/version bytes immediately follow the magic.
            # They remain useful evidence when an occlusion erases every magic
            # byte: unlike an arbitrary binary-cell percentage, this checks
            # the exact protocol shape and its 256-cell physical canvas.
            encoded_geometry = (
                recovered_byte(4) == 1
                and recovered_byte(5) == 1
                and recovered_byte(6) == 128
                and recovered_byte(7) == 128
            )
            # Each protocol byte occupies one aligned group of eight logical
            # cells in row-major wire order.  A complete group of exact flat
            # 2x2 black/white cells containing both values is an encoded
            # fragment even when all fixed header bytes were occluded.  This
            # is a geometry-derived local proof, not a global intact-cell
            # percentage or a guessed threshold.
            encoded_fragment = any(
                all(bit is not None for bit in group) and len({int(bit) for bit in group}) == 2
                for start in range(0, len(flat_bits), 8)
                if len(group := flat_bits[start : start + 8]) == 8
            )
            encoded_grid = flat_cells == 128 * 128 and black_cells > 0 and white_cells > 0
            loading_surface = flat_cells == 128 * 128 and (black_cells == 0 or white_cells == 0)
            return not (
                encoded_header or encoded_geometry or encoded_fragment or encoded_grid or loading_surface
            )
    except (OSError, ValueError, TypeError, IndexError):
        return False


def _canonical_dataclass_json(value: Any) -> bytes:
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("session config binding requires a dataclass instance")
    normalized = _canonical_semantic_value(asdict(value))
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def session_config_semantic_sha256(config: SessionConfig) -> str:
    """Hash the loaded semantic config independently of TOML formatting."""

    return hashlib.sha256(_canonical_dataclass_json(config)).hexdigest()


_RUNTIME_SOURCE_FILES = {
    "daemon.py": Path("src/crowley/session/daemon.py"),
    "driver.py": Path("src/crowley/session/driver.py"),
    "cli.py": Path("src/crowley/session/cli.py"),
    "decoder.py": Path("src/crowley/session/decoder.py"),
    "protocol.py": Path("src/crowley/session/protocol.py"),
    "offline_harness.py": Path("tools/gnome-telemetry-region/offline_harness.py"),
    "session_performance.py": Path("tools/session_performance.py"),
}
_RUNTIME_SOURCE_MAX_BYTES = 4 * 1024 * 1024
_DIAGNOSTIC_ERROR_MAX_CHARS = 512


def _bounded_diagnostic_error(value: Any) -> str:
    text = " ".join(str(value).split())
    if len(text) > _DIAGNOSTIC_ERROR_MAX_CHARS:
        return text[: _DIAGNOSTIC_ERROR_MAX_CHARS - 3] + "..."
    return text


def _runtime_source_root() -> Path:
    """Derive the trusted project root from the installed daemon source."""

    source = Path(__file__).absolute()
    if source.is_symlink():
        raise DriverError("runtime source descriptor is a symlink")
    return source.resolve().parents[3]


def _runtime_source_hashes(*, include_calibration: bool = False, only: str | None = None) -> dict[str, str]:
    """Read the bounded runtime descriptors, refusing drift."""

    root = _runtime_source_root()
    source_files = dict(_RUNTIME_SOURCE_FILES)
    if include_calibration:
        source_files["calibration.py"] = Path("src/crowley/session/calibration.py")
    if only is not None:
        if only not in source_files:
            raise DriverError(f"runtime source descriptor {only} is unknown")
        source_files = {only: source_files[only]}
    result: dict[str, str] = {}
    for logical_name, relative in source_files.items():
        path = root / relative
        try:
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DriverError(f"runtime source {logical_name} is not a regular file")
            if before.st_size > _RUNTIME_SOURCE_MAX_BYTES:
                raise DriverError(f"runtime source {logical_name} exceeds its bounded size")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_nlink,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ):
                    raise DriverError(f"runtime source {logical_name} changed during inspection")
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, min(1024 * 1024, _RUNTIME_SOURCE_MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _RUNTIME_SOURCE_MAX_BYTES:
                        raise DriverError(f"runtime source {logical_name} exceeds its bounded size")
                    digest.update(chunk)
                if total != opened.st_size:
                    raise DriverError(f"runtime source {logical_name} exceeds its bounded size")
                after = os.fstat(descriptor)
                if (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_nlink,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ):
                    raise DriverError(f"runtime source {logical_name} changed during inspection")
                after_path = path.lstat()
                if (
                    stat.S_ISLNK(after_path.st_mode)
                    or not stat.S_ISREG(after_path.st_mode)
                    or (
                        after_path.st_dev,
                        after_path.st_ino,
                        after_path.st_mode,
                        after_path.st_nlink,
                        after_path.st_size,
                        after_path.st_mtime_ns,
                        after_path.st_ctime_ns,
                    )
                    != (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_nlink,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                ):
                    raise DriverError(f"runtime source {logical_name} changed during inspection")
            finally:
                os.close(descriptor)
        except FileNotFoundError as exc:
            raise DriverError(f"runtime source {logical_name} is unavailable") from exc
        except OSError as exc:
            raise DriverError(f"runtime source {logical_name} cannot be inspected") from exc
        result[logical_name] = digest.hexdigest()
    if set(result) != set(source_files):
        raise DriverError("runtime source descriptor map is incomplete")
    return result


class _IdentityLatencyCollector:
    """Bounded owner-thread timing evidence for one paused native sample."""

    _EVENT_NAMES = frozenset(
        {
            "sample_enter",
            "source_binding_before",
            "process_verify",
            "proc_match",
            "identity_x11_query",
            "identity_return",
            "seal_open",
            "seal_hash",
            "lock_acquired",
            "protocol_wait",
            "protocol_record",
            "capture_started",
            "capture_returned",
            "png_publication",
            "identity_after_backend",
            "identity_after_daemon",
            "config_check",
            "source_binding_after",
            "cleanup",
            "response_ready",
            "artifact_write",
        }
    )
    _BARRIERS = ("daemon_pre", "backend_pre", "backend_post", "daemon_post")
    _OUTCOMES = frozenset({"ok", "failed", "cancelled", "deadline", "not_reached"})

    def __init__(self, *, config_file_sha256: str, config_semantic_sha256: str) -> None:
        self._owner_thread = threading.get_ident()
        self._entry = self._clocks()
        self._bound: Path | None = None
        self._owner_return: tuple[int, int, int] | None = None
        self._marks: dict[str, int] = {}
        self._events: list[dict[str, Any]] = []
        self._ordinals: dict[tuple[str, str], int] = {}
        self._barrier_stack: list[str] = []
        self._barriers = {
            name: {"start": None, "end": None, "duration": None, "outcome": "not_reached"}
            for name in self._BARRIERS
        }
        self._bindings: dict[str, str | None] = {
            "source_binding_sha256": None,
            "producer_sha256": None,
            "config_file_sha256": config_file_sha256,
            "config_semantic_sha256": config_semantic_sha256,
        }
        self._events_truncated = False

    @staticmethod
    def _clocks() -> tuple[int, int, int]:
        return time.monotonic_ns(), time.thread_time_ns(), time.process_time_ns()

    @property
    def current_barrier(self) -> str:
        return self._barrier_stack[-1] if self._barrier_stack else "none"

    def bind(self, request_dir: Path) -> None:
        if self._bound is not None or threading.get_ident() != self._owner_thread:
            raise DriverError("identity latency collector was bound more than once")
        self._bound = request_dir
        self._marks["collector_bound"] = self._clocks()[0]

    def set_bindings(self, source_binding: dict[str, str], producer_sha256: str) -> None:
        canonical = json.dumps(source_binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._bindings["source_binding_sha256"] = hashlib.sha256(canonical).hexdigest()
        self._bindings["producer_sha256"] = producer_sha256

    def set_producer(self, producer_sha256: str) -> None:
        self._bindings["producer_sha256"] = producer_sha256

    def mark(self, name: str) -> None:
        if threading.get_ident() == self._owner_thread:
            self._marks[name] = self._clocks()[0]

    def _outcome(self, error: BaseException | None) -> str:
        if error is None:
            return "ok"
        text = str(error).lower()
        if "cancel" in text or "revok" in text:
            return "cancelled"
        if "deadline" in text or "timeout" in text:
            return "deadline"
        return "failed"

    def _append_event(
        self,
        name: str,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        error: BaseException | None,
    ) -> None:
        if name not in self._EVENT_NAMES or threading.get_ident() != self._owner_thread:
            return
        barrier = self.current_barrier
        key = (name, barrier)
        ordinal = self._ordinals.get(key, 0)
        self._ordinals[key] = ordinal + 1
        if len(self._events) >= 256 or ordinal > 31:
            self._events_truncated = True
            return
        self._events.append(
            {
                "name": name,
                "barrier": barrier,
                "ordinal": ordinal,
                "start_monotonic_ns": start[0],
                "end_monotonic_ns": end[0],
                "duration_ns": max(0, end[0] - start[0]),
                "owner_thread_cpu_ns": max(0, end[1] - start[1]),
                "owner_process_cpu_ns": max(0, end[2] - start[2]),
                "child_cpu_ns": None,
                "child_cpu_reason": "external_child_cpu_unmeasured",
                "outcome": self._outcome(error),
            }
        )

    def instant(self, name: str) -> None:
        now = self._clocks()
        self._append_event(name, now, now, None)

    @contextmanager
    def span(self, name: str):
        start = self._clocks()
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._append_event(name, start, self._clocks(), error)

    @contextmanager
    def barrier(self, name: str):
        if name not in self._BARRIERS:
            raise DriverError("identity latency barrier is not recognized")
        if threading.get_ident() != self._owner_thread:
            raise DriverError("identity latency barrier crossed its owner thread")
        start = self._clocks()
        record = self._barriers[name]
        record["start"] = start[0]
        self._barrier_stack.append(name)
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            end = self._clocks()
            self._barrier_stack.pop()
            record["end"] = end[0]
            record["duration"] = max(0, end[0] - start[0])
            record["outcome"] = self._outcome(error)

    def finish(self) -> None:
        if threading.get_ident() == self._owner_thread:
            self._owner_return = self._clocks()

    def _interval(self, name: str, start_name: str, end_name: str) -> dict[str, Any]:
        start = self._entry[0] if start_name == "entry" else self._marks.get(start_name)
        end = self._marks.get(end_name)
        return {
            "name": name,
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "duration_ns": max(0, end - start) if start is not None and end is not None else None,
            "outcome": "ok" if start is not None and end is not None else "not_reached",
        }

    def artifact(self) -> dict[str, Any]:
        status = (
            "complete"
            if not self._events_truncated and all(item["outcome"] == "ok" for item in self._barriers.values())
            else "partial"
        )
        return {
            "schema": "identity-latency-v1",
            "status": status,
            **self._bindings,
            "owner_entry_monotonic_ns": self._entry[0],
            "owner_bound_monotonic_ns": self._bound and self._marks.get("collector_bound"),
            "owner_return_monotonic_ns": self._owner_return[0] if self._owner_return else None,
            "intervals_non_additive": True,
            "intervals": [
                self._interval("owner_to_request_started", "entry", "request_started"),
                self._interval("capture_return_to_response_ready", "capture_returned", "response_ready"),
            ],
            "barriers": [
                {
                    "barrier": name,
                    "start_monotonic_ns": record["start"],
                    "end_monotonic_ns": record["end"],
                    "duration_ns": record["duration"],
                    "outcome": record["outcome"],
                }
                for name, record in self._barriers.items()
            ],
            "event_count": len(self._events),
            "events_truncated": self._events_truncated,
            "events": list(self._events),
        }

    def write(self, path: Path) -> None:
        encoded = (
            json.dumps(self.artifact(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        if len(encoded) > 524288:
            raise DriverError("identity latency profile exceeds its size bound")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def write_failure_token(self, directory: Path) -> None:
        path = directory / "identity-latency-write-failed"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, b"identity_latency_write_failed\n")
        finally:
            os.close(descriptor)


def _driver_profile_matches_config(driver: Any, config: SessionConfig) -> bool:
    try:
        driver_profile = asdict(driver.profile)
        configured_profile = asdict(config.profile)
        return json.dumps(
            _canonical_semantic_value(driver_profile), sort_keys=True, separators=(",", ":"), allow_nan=False
        ) == json.dumps(
            _canonical_semantic_value(configured_profile),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


class _NativeObservationDiagnostic:
    """Keep bounded private timing evidence for one native calibration run.

    This collector is deliberately diagnostic-only.  It never participates in
    admission, publication selection, identity validation, or cleanup policy;
    persistence is best effort so a filesystem problem cannot replace the
    causal calibration error.
    """

    _SCHEMA = "native-observation-diagnostic-v1"
    _MAX_ATTEMPTS = 16
    _MAX_RUN_SPANS = 1024
    _MAX_JSON_BYTES = 1_048_576
    _SPAN_NAMES = (
        "identity_poll",
        "alignment_capability",
        "alignment_target",
        "ack_admission",
        "identity_backend_pre",
        "identity_backend_post",
        "identity_post_capture",
        "capture",
        "provider_hash",
        "provider_decode",
        "runner_hash",
        "runner_decode",
        "copy",
        "cells",
        "validation",
        "cleanup",
    )

    def __init__(self, daemon: SessionDaemon) -> None:
        self.daemon = daemon
        self._lock = threading.RLock()
        self._run_id: str | None = None
        self._run_started_ns: int | None = None
        self._run_generation: int | None = None
        self._source_binding: dict[str, str] | None = None
        self._source_binding_sha256: str | None = None
        self._source_binding_current: dict[str, str] | None = None
        self._source_binding_current_sha256: str | None = None
        self._calibration_source_sha256_at_start: str | None = None
        self._calibration_source_sha256_current: str | None = None
        self._source_drift = False
        self._source_binding_error: str | None = None
        self._source_binding_start_error: str | None = None
        self._calibration_source_error: str | None = None
        self._binary_sha256: str | None = None
        self._config_semantic_sha256: str | None = None
        self._attempts: list[dict[str, Any]] = []
        self._run_spans: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._current_path: Path | None = None
        self._terminal = False
        self._failure_persisted = False
        self._failure_path: Path | None = None
        self._offending_path: Path | None = None
        self._persistence_error: str | None = None
        self._failure_payload: dict[str, Any] | None = None
        # Tests and private evidence may subscribe to completed attempts.  The
        # callback is deliberately outside the admission path: it receives a
        # copy after release/retirement and cannot affect qualification.
        self._attempt_observer: Callable[[dict[str, Any]], None] | None = None

    @property
    def failure_path(self) -> Path | None:
        with self._lock:
            return self._failure_path

    @property
    def offending_path(self) -> Path | None:
        with self._lock:
            return self._offending_path

    def start_run(self) -> None:
        """Explicitly start one trace and compute immutable pins once."""

        with self._lock:
            if self._run_id is not None and not self._terminal:
                return
            self._run_id = uuid.uuid4().hex
            self._run_started_ns = time.monotonic_ns()
            self._attempts = []
            self._run_spans = []
            self._current = None
            self._current_path = None
            self._terminal = False
            self._failure_persisted = False
            self._failure_path = None
            self._offending_path = None
            self._persistence_error = None
            self._failure_payload = None
            source_binding = getattr(self.daemon, "_runtime_source_binding", None)
            self._source_binding = (
                {str(key): str(value) for key, value in source_binding.items()}
                if isinstance(source_binding, dict)
                else None
            )
            if self._source_binding is not None:
                canonical = json.dumps(
                    self._source_binding, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                self._source_binding_sha256 = hashlib.sha256(canonical).hexdigest()
            else:
                self._source_binding_sha256 = None
            calibration_at_start = getattr(self.daemon, "_runtime_calibration_source_sha256", None)
            self._calibration_source_sha256_at_start = (
                calibration_at_start if isinstance(calibration_at_start, str) else None
            )
            self._source_binding_current = None
            self._source_binding_current_sha256 = None
            self._calibration_source_sha256_current = None
            self._source_binding_start_error = getattr(self.daemon, "_runtime_source_binding_error", None)
            self._calibration_source_error = getattr(self.daemon, "_runtime_calibration_source_error", None)
            self._source_binding_error = self._source_binding_start_error
            try:
                current = _runtime_source_hashes(include_calibration=True)
                calibration_current = current.pop("calibration.py", None)
                self._source_binding_current = current
                self._calibration_source_sha256_current = calibration_current
                if current:
                    canonical_current = json.dumps(
                        current, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode("utf-8")
                    self._source_binding_current_sha256 = hashlib.sha256(canonical_current).hexdigest()
            except Exception as exc:
                self._source_binding_error = _bounded_diagnostic_error(exc)
            self._source_drift = (
                self._source_binding_error is not None
                or self._source_binding is None
                or self._source_binding_current is None
                or self._source_binding != self._source_binding_current
                or self._calibration_source_sha256_at_start is None
                or self._calibration_source_sha256_current is None
                or self._calibration_source_sha256_at_start != self._calibration_source_sha256_current
                or self._calibration_source_error is not None
            )
            profile = getattr(getattr(self.daemon, "driver", None), "profile", None)
            binary_sha256 = getattr(profile, "telemetry_region_executable_sha256", None)
            self._binary_sha256 = binary_sha256 if isinstance(binary_sha256, str) else None
            config_sha256 = getattr(self.daemon, "_loaded_config_semantic_sha256", None)
            self._config_semantic_sha256 = config_sha256 if isinstance(config_sha256, str) else None
            try:
                status = self.daemon.controller.status()
                generation = getattr(status, "generation", None)
                self._run_generation = generation if isinstance(generation, int) else None
            except Exception:
                self._run_generation = None

    def ensure_started(self) -> None:
        with self._lock:
            started = self._run_id is not None
        if not started:
            self.start_run()

    @staticmethod
    def _empty_spans() -> dict[str, dict[str, Any]]:
        return {
            name: {
                "start_monotonic_ns": None,
                "end_monotonic_ns": None,
                "duration_ns": None,
                "outcome": "not_reached",
                "span_token": None,
                "thread_id": None,
                "observer_calls": [],
            }
            for name in _NativeObservationDiagnostic._SPAN_NAMES
        }

    def begin_attempt(self, stage: str, ordinal: int) -> None:
        self.ensure_started()
        with self._lock:
            attempt: dict[str, Any] = {
                "requested_stage": stage,
                "requested_ordinal": ordinal,
                "started_monotonic_ns": time.monotonic_ns(),
                "thread_id": threading.get_ident(),
                "finished_monotonic_ns": None,
                "actual": {
                    "kind": None,
                    "phase": None,
                    "ordinal": None,
                    "boot": None,
                    "sequence": None,
                    "update_count": None,
                    "publisher_uptime_ms": None,
                    "capture_monotonic": None,
                    "capture_sha256": None,
                    "lua_heap": None,
                    "texture_count": None,
                    "update_p99_ms": None,
                },
                "backend_receipt": None,
                "observer_calls": [],
                "spans": self._empty_spans(),
                "status": "in_progress",
                "error": None,
                "release_outcome": "not_reached",
            }
            self._attempts.append(attempt)
            if len(self._attempts) > self._MAX_ATTEMPTS:
                del self._attempts[: len(self._attempts) - self._MAX_ATTEMPTS]
            self._current = attempt
            self._current_path = None

    def record_observer_call(
        self,
        *,
        nonce: str,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        thread_id: int,
        boundary: str | None = None,
        span_token: str | None = None,
        caller: str | None = None,
        timings_ns: dict[str, int] | None = None,
        observer_phases_ns: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Bind one successful Rust call to its live attempt/span/thread."""

        with self._lock:
            target_attempt: dict[str, Any] | None = None
            target_span: dict[str, Any] | None = None
            if isinstance(span_token, str) and span_token:
                for candidate in reversed(self._attempts):
                    spans = candidate.get("spans", {})
                    for candidate_span in spans.values():
                        if (
                            isinstance(candidate_span, dict)
                            and candidate_span.get("span_token") == span_token
                        ):
                            target_attempt = candidate
                            target_span = candidate_span
                            break
                    if target_span is not None:
                        break
            if target_attempt is None:
                target_attempt = self._current
            if target_attempt is None:
                return
            calls = target_attempt.setdefault("observer_calls", [])
            record = {
                "nonce": nonce,
                "start_monotonic_ns": start_monotonic_ns,
                "end_monotonic_ns": end_monotonic_ns,
                "thread_id": thread_id,
                "boundary": boundary or "unattributed",
                "span_token": span_token,
                "caller": caller,
                "timings_ns": copy.deepcopy(timings_ns) if isinstance(timings_ns, dict) else None,
                "observer_phases_ns": (
                    copy.deepcopy(observer_phases_ns) if isinstance(observer_phases_ns, dict) else None
                ),
            }
            if len(calls) < 8:
                calls.append(record)
            if target_span is not None and len(target_span["observer_calls"]) < 2:
                target_span["observer_calls"].append(copy.deepcopy(record))

    @contextmanager
    def span(
        self,
        name: str,
        *,
        requested_stage: str | None = None,
        requested_ordinal: int | None = None,
        attach_to_attempt: bool = True,
    ):
        """Record a finite span, attributing run-level work to its request."""

        with self._lock:
            attempt = self._current if attach_to_attempt else None
            span = attempt.get("spans", {}).get(name) if attempt is not None else None
            if span is not None and span["start_monotonic_ns"] is None:
                span["start_monotonic_ns"] = time.monotonic_ns()
                span["span_token"] = uuid.uuid4().hex
                span["thread_id"] = threading.get_ident()
            run_span: dict[str, Any] | None = None
            if span is None and attempt is None:
                run_span = {
                    "name": name,
                    "requested_stage": requested_stage,
                    "requested_ordinal": requested_ordinal,
                    "start_monotonic_ns": time.monotonic_ns(),
                    "end_monotonic_ns": None,
                    "duration_ns": None,
                    "outcome": "in_progress",
                }
                self._run_spans.append(run_span)
                if len(self._run_spans) > self._MAX_RUN_SPANS:
                    del self._run_spans[: len(self._run_spans) - self._MAX_RUN_SPANS]
            span_token = (
                span.get("span_token")
                if isinstance(span, dict)
                else run_span.get("span_token")
                if isinstance(run_span, dict)
                else None
            )
        sink_token = None
        if name == "identity_post_capture" and isinstance(span_token, str):
            sink_token = _set_identity_latency_sink(
                _NativeIdentityLatencySink(
                    self,
                    boundary=name,
                    span_token=span_token,
                    caller="NativeCalibrationRunner.capture_observation",
                )
            )
        error: BaseException | None = None
        try:
            yield span_token
        except BaseException as exc:
            error = exc
            raise
        finally:
            if sink_token is not None:
                _reset_identity_latency_sink(sink_token)
            with self._lock:
                if span is not None and span["end_monotonic_ns"] is None:
                    end = time.monotonic_ns()
                    span["end_monotonic_ns"] = end
                    start = span["start_monotonic_ns"]
                    span["duration_ns"] = max(0, end - start) if start is not None else None
                    span["outcome"] = "failed" if error is not None else "ok"
                if run_span is not None and run_span["end_monotonic_ns"] is None:
                    end = time.monotonic_ns()
                    run_span["end_monotonic_ns"] = end
                    start = run_span["start_monotonic_ns"]
                    run_span["duration_ns"] = max(0, end - start)
                    run_span["outcome"] = "failed" if error is not None else "ok"

    def record_backend_receipt(self, receipt: Any) -> None:
        with self._lock:
            if self._current is None:
                return
            if receipt is None:
                self._current["backend_receipt"] = {"status": "not_reached"}
                return
            try:
                record = asdict(receipt) if is_dataclass(receipt) else dict(receipt)
                self._current["backend_receipt"] = copy.deepcopy(record)
            except Exception:
                self._current["backend_receipt"] = {"status": "unavailable"}

    def record_candidate(self, sample: Any, digest: str | None, path: Any = None) -> None:
        with self._lock:
            if self._current is None:
                return
            try:
                frame = getattr(sample, "frame", None)
                snapshot = getattr(frame, "snapshot", None)
                value = getattr(snapshot, "value", {})
                render = value.get("render", {}) if isinstance(value, dict) else {}
                if not isinstance(render, dict):
                    render = {}
                # Agent.Publish reports the heap sample from the previous completed
                # publication. Preserve that observed lag; this is diagnostic context,
                # not a peak or an attribution claim.
                lua_heap_value = render.get("luaHeap")
                lua_heap: dict[str, Any] | None = None
                if isinstance(lua_heap_value, dict):
                    expected_lua_heap_keys = {
                        "method",
                        "available",
                        "beforeKiB",
                        "afterKiB",
                        "netKiB",
                    }
                    before_kib = lua_heap_value.get("beforeKiB")
                    after_kib = lua_heap_value.get("afterKiB")
                    net_kib = lua_heap_value.get("netKiB")
                    numeric_metrics = (before_kib, after_kib, net_kib)
                    numeric = all(
                        isinstance(item, (int, float))
                        and not isinstance(item, bool)
                        and math.isfinite(float(item))
                        for item in numeric_metrics
                    )
                    available = lua_heap_value.get("available")
                    unavailable_sentinels = all(item == 0 for item in numeric_metrics)
                    if (
                        set(lua_heap_value) == expected_lua_heap_keys
                        and lua_heap_value.get("method") == "collectgarbage_count"
                        and isinstance(available, bool)
                        and numeric
                        and (available or unavailable_sentinels)
                    ):
                        lua_heap = {
                            "method": lua_heap_value["method"],
                            "available": available,
                            "before_kib": before_kib,
                            "after_kib": after_kib,
                            "net_kib": net_kib,
                        }
                texture_count = render.get("textureCount")
                if isinstance(texture_count, bool) or not isinstance(texture_count, int):
                    texture_count = None
                update_p99_ms = render.get("updateP99Ms")
                if (
                    isinstance(update_p99_ms, bool)
                    or not isinstance(update_p99_ms, (int, float))
                    or not math.isfinite(float(update_p99_ms))
                ):
                    update_p99_ms = None
                self._current["actual"] = {
                    "kind": getattr(sample, "kind", None),
                    "phase": render.get("nativePhase"),
                    "ordinal": render.get("nativeOrdinal"),
                    "boot": getattr(frame, "boot", None),
                    "sequence": getattr(frame, "sequence", None),
                    "update_count": render.get("updateCount"),
                    "publisher_uptime_ms": getattr(frame, "uptime_ms", None),
                    "capture_monotonic": getattr(sample, "capture_monotonic", None),
                    "capture_sha256": digest,
                    "lua_heap": lua_heap,
                    "texture_count": texture_count,
                    "update_p99_ms": update_p99_ms,
                }
                self._current_path = Path(path) if path is not None else None
            except Exception:
                self._current["actual"]["capture_sha256"] = digest
                self._current_path = None

    def finish_attempt(self, *, status: str, error: str | None = None) -> None:
        observed: dict[str, Any] | None = None
        observer: Callable[[dict[str, Any]], None] | None = None
        with self._lock:
            if self._current is None:
                return
            self._current["status"] = status
            self._current["error"] = _bounded_diagnostic_error(error) if error is not None else None
            self._current["finished_monotonic_ns"] = time.monotonic_ns()
            if status != "accepted":
                observed = copy.deepcopy(self._current)
                observer = self._attempt_observer
                self._current = None
                self._current_path = None
        if observed is not None and observer is not None:
            try:
                observer(observed)
            except Exception:
                # Diagnostic observers are best effort and never alter the
                # causal native result or cleanup path.
                pass

    def set_status(self, *, status: str, error: str | None = None) -> None:
        with self._lock:
            if self._current is not None:
                self._current["status"] = status
                self._current["error"] = _bounded_diagnostic_error(error) if error is not None else None

    def mark_release(self, path: Any, *, error: str | None = None) -> None:
        refresh = False
        observed: dict[str, Any] | None = None
        observer: Callable[[dict[str, Any]], None] | None = None
        with self._lock:
            if self._current is None:
                return
            try:
                candidate = Path(path)
            except TypeError:
                return
            if self._current_path is None or candidate != self._current_path:
                return
            if error:
                self._current["release_outcome"] = "failed"
            elif candidate.exists():
                self._current["release_outcome"] = "retained"
            else:
                self._current["release_outcome"] = "released"
            self._current["finished_monotonic_ns"] = time.monotonic_ns()
            observed = copy.deepcopy(self._current)
            observer = self._attempt_observer
            self._current = None
            self._current_path = None
            refresh = self._failure_path is not None and self._failure_payload is not None
        if refresh:
            self._refresh_failure_payload()
        if observed is not None and observer is not None:
            try:
                observer(observed)
            except Exception:
                pass

    def _failure_directory(self) -> Path:
        journal = Path(self.daemon.config.journal).expanduser()
        run_id = self._run_id or uuid.uuid4().hex
        root = journal.parent / "native-observation-diagnostics"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        return root / run_id

    def _copy_offending_capture(self, path: Any) -> tuple[str | None, str | None]:
        try:
            candidate = Path(path)
            stat_result = candidate.lstat()
            if (
                not stat.S_ISREG(stat_result.st_mode)
                or stat_result.st_nlink != 1
                or stat_result.st_size > 131072
            ):
                return None, "offending capture is not a bounded regular file"
            directory = self._failure_directory()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            target = directory / "offending-capture.png"
            shutil.copyfile(candidate, target)
            os.chmod(target, 0o600)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self._offending_path = target
            return digest, None
        except Exception as exc:
            return None, f"offending capture copy failed: {type(exc).__name__}: {exc}"

    def _write_payload(self, payload: dict[str, Any]) -> Path:
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        if len(encoded) > self._MAX_JSON_BYTES:
            raise DriverError("native observation diagnostic exceeds its size bound")
        directory = self._failure_directory()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "failure.json"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".failure-", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            return path
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _refresh_failure_payload(self) -> None:
        with self._lock:
            payload = copy.deepcopy(self._failure_payload)
            path = self._failure_path
            if payload is not None:
                payload["attempts"] = copy.deepcopy(self._attempts)
        if payload is None or path is None:
            return
        try:
            refreshed = self._write_payload(payload)
            with self._lock:
                self._failure_path = refreshed
        except Exception as exc:
            self._record_persistence_failure(exc, remove_offending=False)

    def _record_persistence_failure(self, exc: BaseException, *, remove_offending: bool) -> None:
        error = type(exc).__name__
        errno_value = getattr(exc, "errno", None)
        if isinstance(errno_value, int) and not isinstance(errno_value, bool):
            error = f"{error} errno={errno_value}"
        orphan = None
        with self._lock:
            self._persistence_error = error
            if remove_offending:
                orphan = self._offending_path
                self._offending_path = None
        if orphan is not None:
            try:
                orphan.unlink()
            except OSError:
                pass
        _LOGGER.warning("native observation diagnostic persistence failed: %s", error)

    def fail(
        self,
        reason: str,
        *,
        stage: str | None = None,
        ordinal: int | None = None,
        offending_path: Any = None,
        mismatch: bool = False,
    ) -> None:
        """Persist one private failure artifact without changing the causal error."""

        self.ensure_started()
        with self._lock:
            if self._failure_persisted:
                return
            self._failure_persisted = True
            self._terminal = True
            run_id = self._run_id
            attempts = copy.deepcopy(self._attempts)
            source_binding = copy.deepcopy(self._source_binding)
            source_binding_sha256 = self._source_binding_sha256
            binary_sha256 = self._binary_sha256
            config_sha256 = self._config_semantic_sha256
            generation = self._run_generation
        offending_digest = None
        artifact_error = None
        if mismatch and offending_path is not None:
            offending_digest, artifact_error = self._copy_offending_capture(offending_path)
        payload = {
            "trace_schema": self._SCHEMA,
            "run_id": run_id,
            "generation": generation,
            "run_started_monotonic_ns": self._run_started_ns,
            "source_binding": source_binding,
            "source_binding_sha256": source_binding_sha256,
            "source_binding_current": copy.deepcopy(self._source_binding_current),
            "source_binding_current_sha256": self._source_binding_current_sha256,
            "calibration_source_sha256_at_start": self._calibration_source_sha256_at_start,
            "calibration_source_sha256_current": self._calibration_source_sha256_current,
            "source_drift": self._source_drift,
            "source_binding_error": self._source_binding_error,
            "source_binding_start_error": self._source_binding_start_error,
            "calibration_source_error": self._calibration_source_error,
            "protocol_sha256": source_binding.get("protocol.py") if source_binding else None,
            "binary_sha256": binary_sha256,
            "config_semantic_sha256": config_sha256,
            "attempts": attempts,
            "run_spans": copy.deepcopy(self._run_spans),
            "failure": {
                "reason": _bounded_diagnostic_error(reason),
                "stage": stage,
                "ordinal": ordinal,
                "offending_capture_sha256": offending_digest,
                "offending_capture_name": "offending-capture.png" if offending_digest else None,
                "offending_capture_error": (
                    _bounded_diagnostic_error(artifact_error) if artifact_error is not None else None
                ),
            },
        }
        with self._lock:
            self._failure_payload = copy.deepcopy(payload)
        try:
            path = self._write_payload(payload)
            with self._lock:
                self._failure_path = path
        except Exception as exc:
            self._record_persistence_failure(exc, remove_offending=True)

    def finish_success(self) -> None:
        with self._lock:
            self._terminal = True
            if self._current is not None:
                self._current["finished_monotonic_ns"] = time.monotonic_ns()


def _native_diagnostic_span(diagnostic: Any, name: str, *, attach_to_attempt: bool = True):
    """Use one diagnostic span path while keeping ordinary calls unchanged."""

    if isinstance(diagnostic, _NativeObservationDiagnostic):
        return diagnostic.span(name, attach_to_attempt=attach_to_attempt)
    return nullcontext()


class _NativeIdentityLatencySink:
    """Bridge the driver's existing identity barriers into one native attempt."""

    def __init__(
        self,
        diagnostic: _NativeObservationDiagnostic,
        *,
        boundary: str | None = None,
        span_token: str | None = None,
        caller: str | None = None,
    ) -> None:
        self.diagnostic = diagnostic
        self._active_boundary = boundary
        self._active_span_token = span_token
        self._active_caller = caller

    def span(self, _name: str):
        return nullcontext()

    def barrier(self, name: str):
        if name == "backend_pre":
            mapped = "identity_backend_pre"
            caller = "X11Backend.production.resolve_identity.backend_pre"
        elif name == "backend_post":
            mapped = "identity_backend_post"
            caller = "X11Backend.production.resolve_identity.backend_post"
        elif name == "post_capture":
            mapped = "identity_post_capture"
            caller = "NativeCalibrationRunner.capture_observation"
        else:
            return nullcontext()
        return self._named_span(mapped, caller=caller)

    @contextmanager
    def _named_span(self, name: str, *, caller: str | None = None):
        with self.diagnostic.span(name) as span_token:
            previous_boundary = self._active_boundary
            previous_token = self._active_span_token
            previous_caller = self._active_caller
            self._active_boundary = name
            self._active_span_token = span_token
            self._active_caller = caller
            try:
                yield
            finally:
                self._active_boundary = previous_boundary
                self._active_span_token = previous_token
                self._active_caller = previous_caller

    @contextmanager
    def named_span(self, name: str):
        with self._named_span(name, caller="NativeCalibrationRunner.capture_observation"):
            yield

    def instant(self, _name: str) -> None:
        return None

    def mark(self, _name: str) -> None:
        return None

    def observer_call(
        self,
        *,
        nonce: str,
        start_monotonic_ns: int,
        end_monotonic_ns: int,
        timings_ns: dict[str, int] | None = None,
        observer_phases_ns: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.diagnostic.record_observer_call(
            nonce=nonce,
            start_monotonic_ns=start_monotonic_ns,
            end_monotonic_ns=end_monotonic_ns,
            thread_id=threading.get_ident(),
            boundary=self._active_boundary,
            span_token=self._active_span_token,
            caller=self._active_caller,
            timings_ns=timings_ns,
            observer_phases_ns=observer_phases_ns,
        )


class _NativeCaptureRetained(Exception):
    """Internal marker for an unlink helper that left its exact path behind."""


@dataclass(frozen=True, slots=True)
class _NativeUpdateCapability:
    """Private per-update admission proof for the native identity fast path."""

    generation: int
    guardian_generation: int
    epoch: int
    reservation_id: str
    run_id: str
    identity_fingerprint: str
    expected_state: ControllerState
    profile_fingerprint: str
    owner_pid: int
    owner_process_start: str
    desktop_lease_held: bool
    guardian_armed: bool
    backend_id: int
    b0: str
    expected_constructor_boot: str
    expires_monotonic: float


@dataclass(frozen=True, slots=True)
class _PassiveRegionCaptureCapability:
    """One passive-startup operation's read-only region-capture authority."""

    epoch: int
    generation: int
    state: ControllerState
    identity_fingerprint: str
    process_start: str
    guardian_generation: int
    observer_pid: int
    deadline_monotonic: float
    observer: Any


def _serialized_motion(method):
    """Serialize all interaction producers around one physical intent.

    IPC handlers already serialize their own requests, but the engine, a
    direct caller, and a concurrent socket can still reach the daemon at the
    same time.  The lock is deliberately acquired without waiting: a second
    intent is rejected while the first one still owns capture/model/native
    feedback and the input lease.
    """

    @wraps(method)
    def guarded(self, *args, **kwargs):
        lock = getattr(self, "_motion_intent_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._motion_intent_lock = lock
        if not lock.acquire(blocking=False):
            raise DriverError("another motion intent is already in flight")
        local = getattr(self, "_motion_deadline_local", None)
        outer = local is None or not hasattr(local, "deadline")
        authority_local = getattr(self, "_mail_cycle_context", None)
        if authority_local is None:
            authority_local = threading.local()
            self._mail_cycle_context = authority_local
        authority_set = False
        requested_cycle = kwargs.get("cycle_id")
        if (
            method.__name__ == "execute_executor_click"
            and isinstance(requested_cycle, str)
            and requested_cycle
        ):
            authority_local.cycle_id = requested_cycle
            authority_set = True
        try:
            if outer:
                local = local or threading.local()
                operation_seconds = {
                    "execute_motion_key": MOTION_KEY_OPERATION_SECONDS,
                    "execute_camera_delta": MOTION_CAMERA_OPERATION_SECONDS,
                    "calibrate_camera_mapping": MOTION_CAMERA_OPERATION_SECONDS,
                    "calibrate_movement_basis": MOTION_CALIBRATION_OPERATION_SECONDS,
                    "execute_waypoint": MAX_WAYPOINT_SECONDS,
                    "execute_world_click": 60.0,
                    "execute_executor_click": 60.0,
                }.get(method.__name__)
                if method.__name__ == "execute_route_walk":
                    route_name = kwargs.get("route_name", args[0] if args else None)
                    route_file = getattr(self, "route_file", None)
                    route = route_file.routes.get(route_name) if isinstance(route_file, RouteFile) else None
                    operation_seconds = min(walk_budget(route)[1], 60) if route is not None else 60.0
                if operation_seconds is None:
                    raise DriverError(f"motion operation {method.__name__!r} has no bounded deadline")
                try:
                    requested_deadline = kwargs.get("deadline_monotonic")
                    if requested_deadline is not None and (
                        isinstance(requested_deadline, bool)
                        or not isinstance(requested_deadline, (int, float))
                        or not math.isfinite(float(requested_deadline))
                    ):
                        raise DriverError("motion operation deadline is malformed")
                    local.deadline = self._native_clock() + operation_seconds
                    if requested_deadline is not None:
                        local.deadline = min(local.deadline, float(requested_deadline))
                    cycle_capability = getattr(
                        getattr(self, "controller", None), "mail_cycle_capability", lambda: None
                    )()
                    if cycle_capability is not None:
                        local.deadline = min(local.deadline, cycle_capability.deadline_monotonic)
                except Exception:
                    self._invalidate_motion_admission()
                    raise
                self._motion_deadline_local = local
            return method(self, *args, **kwargs)
        finally:
            if outer:
                try:
                    del local.deadline
                except AttributeError:
                    pass
            if authority_set:
                try:
                    del authority_local.cycle_id
                except AttributeError:
                    pass
            lock.release()

    return guarded


class SessionDaemon:
    """One process owns one controller, input lease, capture loop, and IPC lock."""

    _NO_ROUTE_WALK_STATUS = MappingProxyType(
        {
            "state": "idle",
            "route": "",
            "generation": 0,
            "segments": (),
            "error": "",
        }
    )

    # Native operator actions are deliberately outside the daemon.  These
    # bounds only determine how long the producer waits for the next actual
    # Agent publication after a readiness signal becomes visible.
    _NATIVE_FRAME_TIMEOUT_SECONDS = 30.0
    # The native provider's longer publication wait must not remove the
    # driver's established fifteen-second producer bound.  One immutable
    # deadline below is passed through capture and pre-ACK admission.
    _NATIVE_PRODUCER_TIMEOUT_SECONDS = _DEFAULT_CAPTURE_TIMEOUT_SECONDS
    _NATIVE_POLL_INTERVAL_SECONDS = 0.05
    # One construction attempt makes 103 provider calls: construct/0, held/0,
    # updates/1..99, held/100, and resume/0.  Add the existing ten-second
    # entry and thirty-second finalize bounds.  This is an admission bound
    # only; each provider retains its independent 30-second deadline, and the
    # seven-second constructor reserve remains inside its provider deadline.
    _NATIVE_CONSTRUCTION_OPERATION_SECONDS = 10.0 + (103 * _NATIVE_FRAME_TIMEOUT_SECONDS) + 30.0
    _NATIVE_CONSTRUCTION_HISTORY_LIMIT = 16
    _NATIVE_CONSTRUCTION_TERMINAL_LIMIT = 32
    _MIN_CAMERA_DRAG_PIXELS = MIN_CAMERA_CALIBRATION_PIXELS

    @staticmethod
    def _load_routes(config: SessionConfig) -> RouteFile | None:
        path = getattr(config, "routes_file", None)
        if path is None:
            return None
        descriptor: int | None = None
        try:
            descriptor = open_routes_descriptor(path)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                route_file = RouteFile.from_json(handle.read())
                target = packaged_game_target()
                if target.full_build != "1.60.1.70450" or target.route_provenance != KINEMATICS_PROVENANCE:
                    raise DriverError(
                        "routes_file is historical beta kinematics and cannot be used for the selected target"
                    )
                if route_file.kinematics.provenance != target.route_provenance:
                    raise DriverError(
                        "routes_file kinematics provenance is not the verified beta measurement"
                    )
                return route_file
        except DriverError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise DriverError(f"routes_file is invalid: {path}: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _load_motion_profiles(config: SessionConfig) -> dict[str, Any]:
        profiles = {
            "pointer": (config.pointer_profile, "pointer"),
            "camera": (config.camera_profile, "camera"),
            "move": (config.move_profile, "move"),
        }
        loaded: dict[str, Any] = {}
        from .executor import DemonstrationRole, MotionProfile

        baselines = {
            "pointer": MotionProfile(role=DemonstrationRole.POINTER),
            "camera": MotionProfile(role=DemonstrationRole.CAMERA),
            "move": MotionProfile(role=DemonstrationRole.MOVE),
        }
        for role, (path, _label) in profiles.items():
            if path is None:
                loaded[role] = baselines[role]
                continue
            profile = load_motion_profile(path)
            if profile.role.value != role:
                raise DriverError(f"selected {role} motion profile has role {profile.role.value!r}")
            loaded[role] = profile
        return loaded

    def __init__(
        self,
        config: SessionConfig,
        *,
        controller: Controller | None = None,
        driver: DesktopDriver | None = None,
    ):
        # Keep the loaded config immutable from runtime driver evidence. The
        # driver retains the supplied profile object and records measured
        # anchors there after real actions.
        self.config = copy.deepcopy(config)
        self.route_file = self._load_routes(self.config)
        self._loaded_config_semantic_sha256 = session_config_semantic_sha256(self.config)
        self._model_counter_lock = threading.RLock()
        self._model_call_counters = {"started": 0, "succeeded": 0, "failed": 0}
        self._startup_lock = threading.Lock()
        self._startup_operation: dict[str, Any] = {
            "state": "not_checked",
            "checkedAt": 0,
            "advancingFrames": 0,
            "failure": "",
        }
        self._startup_evidence: RuntimeInputEvidence | None = None
        self._takeover_drill_lock = threading.Lock()
        # Serialize drill admission/commit against the public cancel command.
        # The cancellation path releases this lock before revoking Guardian so
        # its callback can safely take the controller lock.
        self._takeover_cancel_lock = threading.Lock()
        self._takeover_cancel = threading.Event()
        self._takeover_event: tuple[Any, int] | None = None
        self._takeover_drill_operation: dict[str, Any] = {"state": "idle", "failure": ""}
        # Source evidence is optional for ordinary daemon operation. A source
        # installation that cannot be inspected remains usable, but its
        # paused diagnostic is refused when it reaches the qualification gate.
        self._runtime_source_binding_error: str | None = None
        self._runtime_calibration_source_error: str | None = None
        try:
            self._runtime_source_binding = _runtime_source_hashes()
        except DriverError as exc:
            self._runtime_source_binding = None
            self._runtime_source_binding_error = _bounded_diagnostic_error(exc)
        try:
            calibration_binding = _runtime_source_hashes(include_calibration=True, only="calibration.py")
            self._runtime_calibration_source_sha256 = calibration_binding.get("calibration.py")
        except DriverError as exc:
            self._runtime_calibration_source_sha256 = None
            self._runtime_calibration_source_error = _bounded_diagnostic_error(exc)
        self._native_observation_diagnostic = _NativeObservationDiagnostic(self)
        self._motion_profiles = self._load_motion_profiles(self.config)
        self.capture_index = 0
        self._named_capture_sequence = 0
        self._last_addon_sequence = 0
        self._last_addon_boot = ""
        self._motion_crs: dict[tuple[str, int, str], int] = {}
        self._native_measurement_state: dict[str, Any] | None = None
        # NativeCalibrationRunner reuses the provider's exact crop artifact so
        # it can decode and hash the same bytes.  Keep those private paths
        # until the runner has consumed the whole 101-frame operation; then
        # discard every intermediate while retaining the final sealed proof.
        self._native_owned_capture_paths: set[Any] = set()
        self._native_bootstrap_ready = threading.Event()
        self._native_baseline_ready = threading.Event()
        self._native_bootstrap_lock = threading.RLock()
        self._native_bootstrap_observation: dict[str, Any] | None = None
        self._native_baseline_observation: dict[str, Any] | None = None
        self._native_bootstrap_proof: dict[str, Any] | None = None
        self._native_handshake: dict[str, Any] = {
            "state": "idle",
            "holdRequired": False,
            "measureReady": False,
            "resumeRequired": False,
            "resumeReady": False,
            "failure": "",
            "restored": None,
            "archiveReceipts": [],
        }
        # Deferred construction admission is private process-local state.  It
        # is deliberately separate from the legacy operator-visible handshake
        # returned by status_dict so B0, reservation IDs, and identity fields
        # cannot leak through public status.
        self._native_construction_lock = threading.RLock()
        # Passive startup has a separate, read-only capture authority.  It is
        # never exposed through status or IPC and is valid only while the
        # exact startup operation owns its registered XI2 observer.
        self._passive_region_epoch = 0
        self._passive_region_admission: _PassiveRegionCaptureCapability | None = None
        self._native_construction_reservation: dict[str, Any] | None = None
        self._native_construction_ticket: dict[str, Any] | None = None
        self._native_construction_handshake: dict[str, Any] = {}
        self._native_construction_state_history: list[dict[str, Any]] = []
        self._native_construction_operation_deadline: float | None = None
        self._native_construction_finalize_event = threading.Event()
        self._native_construction_epoch = 0
        self._native_construction_inflight: dict[str, Any] | None = None
        # Every native runner, including the legacy path, gets an immutable
        # callback owner.  Callbacks never infer legacy/deferred mode from the
        # mutable reservation after a worker has started.
        self._native_construction_run_context: dict[str, Any] | None = None
        self._native_construction_sealed_run_id: str | None = None
        self._native_construction_terminals: dict[str, dict[str, Any]] = {}
        self._native_construction_expired = False
        # A deferred update anchor is private run-local state.  It is written
        # only after the daemon provider accepts a fresh update and consumed by
        # the next call from the exact runner that owns that update.  Keeping
        # the runner token and anchor together prevents a stale previous run
        # or an arbitrary provider caller from borrowing the timing proof.
        self._native_update_alignment_runner: dict[str, Any] | None = None
        self._native_update_alignment_anchor: dict[str, Any] | None = None
        # A bounded private diagnostic for a failed rejected-measurement
        # audit append.  It is intentionally absent from IPC and status.
        self._native_memory_archive_diagnostic: str | None = None
        self._shutdown_event = threading.Event()
        self._motion_intent_lock = threading.RLock()
        self._executor_continuation_capability: str | None = None
        self._executor_continuation_wait_deadline: float | None = None
        self._motion_deadline_local = threading.local()
        self._mail_cycle_context = threading.local()
        self._named_observation_deadline: float | None = None
        self._calibration_lock = threading.Lock()
        self._calibration_state_lock = threading.RLock()
        self._calibration_thread: threading.Thread | None = None
        self._calibration_operation: dict[str, Any] = {
            "state": "idle",
            "kind": "",
            "generation": 0,
            "startedAt": 0,
            "finishedAt": 0,
            "error": "",
            "readiness": None,
            "result": None,
        }
        self.driver = driver or DesktopDriver(
            copy.deepcopy(self.config.profile),
            pointer_motion_profile=self._motion_profiles["pointer"],
            camera_motion_profile=self._motion_profiles["camera"],
            move_motion_profile=self._motion_profiles["move"],
            named_state_observer=self._observe_named_state,
            native_measurement_provider=self._native_measurement_provider,
            measured_anchor_provider=self._measure_pointer_anchor,
        )
        # Keep bounded native polling and the runner on the driver's injected
        # monotonic source.  This is also the source used by the guardian and
        # makes headless operator fixtures deterministic without changing the
        # process-wide clock used by sockets, watchdogs, or pytest threads.
        configured_native_clock = getattr(self.driver, "_clock", None)
        self._native_clock = configured_native_clock if callable(configured_native_clock) else time.monotonic
        configured_anchor_provider = getattr(self.driver, "measured_anchor_provider", None)
        self._configured_anchor_provider = (
            configured_anchor_provider
            if callable(configured_anchor_provider)
            and getattr(configured_anchor_provider, "__self__", None) is not self
            else None
        )
        if isinstance(self.driver.backend, X11Backend):
            # The installed daemon owns the live providers.  A caller cannot
            # replace them with copied metrics or identity-only callbacks.  A
            # pre-bound native anchor producer is retained because the native
            # worker owns that concrete visual measurement boundary.
            self.driver._named_state_observer = self._observe_named_state
            self.driver.native_measurement_provider = self._native_measurement_provider
            if self._configured_anchor_provider is None:
                self.driver.measured_anchor_provider = self._measure_pointer_anchor
            self.driver.native_preconstruction_provider = self._native_preconstruction_provider
            # The native runner consumes provider artifacts and publishes the
            # HWM boundary through these lead-owned hooks.  Registering them
            # here prevents a caller from substituting an identity-only or
            # copied-metrics callback on the live driver.
            self.driver.notify_native_baseline_ready = self.notify_native_baseline_ready
            self.driver.notify_native_memory_reset_ready = self.notify_native_memory_reset_ready
            self.driver.notify_native_hold_required = self.notify_native_hold_required
            self.driver.notify_native_memory_ready = self.notify_native_memory_ready
            self.driver.notify_native_resume_ready = self.notify_native_resume_ready
            self.driver.notify_native_resume_required = self.notify_native_resume_required
            self.driver.notify_native_measurement_failed = self.notify_native_measurement_failed
            self.driver.release_native_capture = self.release_native_capture
        # Calibration may consult this private predicate before its redundant
        # update identity poll.  The daemon-owned provider recomputes the
        # capability at entry, so replacing or wrapping the provider can only
        # fall back to the full identity path.
        self.driver._native_identity_budget_eligibility = self._native_update_identity_budget_capability
        self.driver._native_identity_budget_capability_valid = (
            self._native_update_identity_budget_capability_valid
        )
        if isinstance(self.driver.backend, X11Backend) and not (
            self.config.plan_hash and self.config.plan_json
        ):
            # The driver acquires the desktop lease and starts its independent
            # guardian at construction.  Do not leave either owner alive when
            # rejecting an incomplete live profile before the controller exists.
            try:
                close = getattr(self.driver, "close", None)
                if callable(close):
                    close()
            finally:
                raise DriverError("live session requires an externally pinned canonical plan hash and JSON")
        self.controller = controller or Controller(
            Journal(self.config.journal),
            driver=self.driver,
            cadence_seconds=self.config.cadence_seconds,
            sweep_cap_seconds=self.config.sweep_cap_seconds,
            reload_deadline_seconds=self.config.reload_deadline_seconds,
            upload_deadline_seconds=self.config.upload_deadline_seconds,
            ack_deadline_seconds=self.config.ack_deadline_seconds,
        )
        # Mail-loop opt-in is deliberately process-local.  It is never loaded
        # from the journal, so a fresh owner starts disabled and cannot replay
        # an old queue authorization.
        self._mail_loop_binding = MailLoopBinding()
        self._mail_loop_active_binding: MailLoopBinding | None = None
        self._mail_loop_control_epoch = 0
        self._mail_loop_last_decision: dict[str, Any] | None = None
        # One fixed read-only wait bound per started mail attempt. This is
        # process-local: a fresh owner never retries the physical action, and
        # a missing bound after restart fails closed immediately rather than
        # reconstructing or renewing a wait.
        self._mail_cycle_wait_deadlines: dict[str, float] = {}
        self.motion_provider = NativeMotionProvider(
            self.capture_status,
            lambda *, deadline: self._resolve_identity(
                require_foreground=True,
                deadline=deadline,
            ),
            capture_with_deadline=self.capture_status,
            clock=self._native_clock,
        )
        self.camera_mapping: CameraMapping | None = None
        self.movement_basis: MovementBasis | None = None
        self.decoder = PixelDecoder(crop=self.config.profile.canvas_crop)
        self.last_error = ""
        self.reopen_action_sent = False
        self.current_evidence = None
        # Closeout may race the addon's automatic start.  Keep one fresh
        # scanner observation so a transient item/owned frame is not mistaken
        # for a new run; the pinned scanner intentionally leaves
        # ``activeSweepId`` empty until finish.
        self._closeout_scan_candidate: tuple[int, str] | None = None
        self._closeout_fence_observation: tuple[str, int] | None = None
        self.model = LunaAdapter(
            model=self.config.model,
            cli_version=self.config.cli_version,
            codex_path=self.config.codex_path,
        )
        journal_value = self.controller.journal.read()
        stored_recovery_actions = journal_value.get("recoveryActions", [])
        self.recovery_attempts = int(journal_value.get("recoveryAttempts", 0))
        self.recovery_actions: set[str] = (
            {item for item in stored_recovery_actions if isinstance(item, str)}
            if isinstance(stored_recovery_actions, list)
            else set()
        )
        self.last_snapshot: dict[str, Any] = journal_value.get("telemetry", {})
        self._last_action_generation = 0
        self._last_motion_snapshot: dict[str, Any] = {}
        self._motion_snapshots: dict[tuple[str, int, str], dict[str, Any]] = {}
        self._motion_evidence: dict[tuple[str, int, str], bytes] = {}
        self._route_walk_lock = threading.RLock()
        self._route_walk_status: dict[str, Any] = {
            "state": "idle",
            "route": "",
            "generation": 0,
            "segments": [],
            "error": "",
        }

        # Revocation can be raised by the independent XI2 guardian while a
        # bounded visual turn is running.  Cancel the complete model process
        # group so no late decision can reach the executor.
        on_revoke = getattr(self.driver, "on_revoke", None)
        if callable(on_revoke):
            on_revoke(lambda _reason, _generation: self.model.cancel())
            on_revoke(self._on_native_construction_revocation)

    def _on_native_construction_revocation(self, reason: str, _generation: int) -> None:
        with self._native_construction_lock:
            active = (
                self._native_construction_reservation is not None
                or self._native_construction_ticket is not None
                or self._native_construction_inflight is not None
            )
            if active:
                self._native_construction_epoch += 1
                context = self._native_construction_run_context
                reservation_snapshot = copy.deepcopy(self._native_construction_reservation)
                if reservation_snapshot is not None:
                    self._native_construction_publish_state_locked("recovery_required", failure=str(reason))
                    self._native_construction_record_terminal_locked(
                        reservation_snapshot,
                        state="recovery_required",
                        failure=str(reason),
                    )
                if context is not None:
                    closed = copy.deepcopy(context)
                    closed["closed"] = True
                    closed["stateHistory"] = copy.deepcopy(self._native_construction_state_history)
                    self._native_construction_run_context = closed
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_inflight = None
                self._native_construction_operation_deadline = None
                self._native_construction_finalize_event.clear()
        # Controller.pause holds its mutex while Guardian invokes this
        # callback.  Never acquire the construction lock while making this
        # durable transition; doing so deadlocks against prepare/recheck.
        if active:
            try:
                self.controller.mark_native_construction_recovery_required()
            except RuntimeError:
                pass

    def _native_construction_revoke_owner(self, reservation_id: str | None, reason: str) -> None:
        """Invalidate exactly one local owner and retain durable B0."""

        changed = False
        with self._native_construction_lock:
            reservation = self._native_construction_reservation
            if reservation_id is None or (
                reservation is not None and reservation.get("reservationId") == reservation_id
            ):
                changed = reservation is not None or self._native_construction_inflight is not None
                self._native_construction_epoch += 1
                context = self._native_construction_run_context
                reservation_snapshot = copy.deepcopy(reservation)
                if reservation_snapshot is not None:
                    self._native_construction_publish_state_locked("recovery_required", failure=str(reason))
                    self._native_construction_record_terminal_locked(
                        reservation_snapshot,
                        state="recovery_required",
                        failure=str(reason),
                    )
                if context is not None and (
                    reservation_id is None or context.get("reservationId") == reservation_id
                ):
                    closed = copy.deepcopy(context)
                    closed["closed"] = True
                    closed["stateHistory"] = copy.deepcopy(self._native_construction_state_history)
                    self._native_construction_run_context = closed
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_inflight = None
                self._native_construction_operation_deadline = None
                self._native_construction_finalize_event.clear()
        if changed:
            try:
                self.controller.mark_native_construction_recovery_required()
            except RuntimeError:
                pass

    def _native_construction_flush_expiry(self) -> None:
        with self._native_construction_lock:
            # Expiry checks are part of every competing admission boundary,
            # rather than only the IPC method that happens to consume the
            # reservation.  The durable Guardian/controller callbacks remain
            # outside this lock.
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            self._native_construction_expired = False
        if expired:
            try:
                self.driver.guardian.cancel_calibration_operation()
            except (AttributeError, GuardianError):
                pass
            try:
                self.controller.mark_native_construction_recovery_required()
            except RuntimeError:
                pass

    def _native_construction_blocks_competing_entry(self, *, native_calibration: bool = False) -> bool:
        with self._native_construction_lock:
            reservation = self._native_construction_reservation
            inflight = self._native_construction_inflight
            if reservation is None and self._native_construction_ticket is None and inflight is None:
                return False
            if native_calibration and reservation is not None:
                return (
                    reservation.get("reservationKind") == "preflight"
                    or reservation.get("reservationState") != "consumed"
                )
            return True

    def _native_construction_callback_context_locked(self) -> dict[str, Any]:
        """Return the immutable owner for a runner callback.

        A callback arriving after revoke/failure must fail its original run. It
        must never fall through to the legacy handshake or clear a later
        reservation. The caller holds ``_native_construction_lock``.
        """

        context = self._native_construction_run_context
        if context is None or context.get("closed"):
            raise DriverError("native callback has no active run owner")
        reservation = self._native_construction_reservation
        if context.get("deferred"):
            if (
                reservation is None
                or reservation.get("reservationId") != context.get("reservationId")
                or context.get("epoch") != self._native_construction_epoch
            ):
                raise DriverError("native deferred callback owner was revoked")
        return copy.deepcopy(context)

    def _native_construction_capability_locked(self) -> dict[str, Any] | None:
        """Return the exact owner capability for a producer admission gate."""

        owner = self._native_construction_inflight or self._native_construction_reservation
        if not isinstance(owner, dict):
            return None
        return {
            "epoch": self._native_construction_epoch,
            "generation": owner.get("generation", owner.get("controllerGeneration")),
            "guardianGeneration": owner.get("guardianGeneration"),
            "reservationId": owner.get("reservationId"),
            "kind": owner.get("kind", owner.get("reservationKind")),
        }

    def _native_construction_capability_matches(self, capability: dict[str, Any]) -> bool:
        """Check an immutable capability while holding construction lock.

        Every caller must hold ``_native_construction_lock``.  The producer
        admission gate keeps that lock through Guardian admission so owner
        revocation cannot clear the exact capability between this comparison
        and process registration.
        """

        if not isinstance(capability, dict):
            return False
        if capability.get("epoch") != self._native_construction_epoch:
            return False
        owner = self._native_construction_inflight or self._native_construction_reservation
        if not isinstance(owner, dict):
            return False
        expected_generation = capability.get("generation")
        current_generation = owner.get("generation", owner.get("controllerGeneration"))
        if expected_generation != current_generation:
            return False
        if capability.get("guardianGeneration") != owner.get("guardianGeneration"):
            return False
        expected_kind = capability.get("kind")
        current_kind = owner.get("kind", owner.get("reservationKind"))
        if expected_kind != current_kind:
            return False
        expected_id = capability.get("reservationId")
        current_id = owner.get("reservationId")
        return expected_id == current_id

    @staticmethod
    def _native_exact_provider(provider: Any, daemon: SessionDaemon) -> bool:
        """Require the installed daemon provider, rather than a wrapper."""

        return getattr(provider, "__self__", None) is daemon and getattr(
            provider, "__func__", None
        ) is getattr(getattr(daemon, "_native_measurement_provider", None), "__func__", None)

    def _native_update_capability_matches_locked(
        self,
        capability: _NativeUpdateCapability,
        identity: WindowIdentity | None = None,
    ) -> bool:
        """Validate the private update proof while holding construction lock."""

        if not isinstance(capability, _NativeUpdateCapability):
            return False
        # B0 and its constructor echo are a typed construction binding, not
        # caller-controlled token fields.  The durable owner context below
        # retains the values admitted at deferred-run start, so a witness
        # cannot be reminted after the live reservation is changed.
        if not isinstance(capability.b0, str) or not isinstance(capability.expected_constructor_boot, str):
            return False
        try:
            b0_size = len(capability.b0.encode("utf-8"))
            expected_boot_size = len(capability.expected_constructor_boot.encode("utf-8"))
        except UnicodeError:
            return False
        if (
            not capability.b0
            or capability.b0.startswith("native-construction:")
            or b0_size > 64
            or capability.expected_constructor_boot != f"native-construction:{capability.b0}"
            or expected_boot_size > 96
        ):
            return False
        if capability.expected_state is not ControllerState.NATIVE_CALIBRATION:
            return False
        if capability.epoch != self._native_construction_epoch:
            return False
        if identity is not None and identity.fingerprint != capability.identity_fingerprint:
            return False
        owner = self._native_construction_reservation
        context = self._native_construction_run_context
        if (
            not isinstance(owner, dict)
            or not isinstance(context, dict)
            or context.get("closed")
            or not context.get("deferred")
            or owner.get("reservationState") != "consumed"
            or context.get("runId") != capability.run_id
            or self._native_construction_sealed_run_id != capability.run_id
            or context.get("epoch") != capability.epoch
            or context.get("reservationId") != capability.reservation_id
            or owner.get("reservationId") != capability.reservation_id
            or owner.get("controllerGeneration") != capability.generation
            or context.get("generation") != capability.generation
            or owner.get("guardianGeneration") != capability.guardian_generation
            or context.get("constructionB0") != capability.b0
            or context.get("expectedConstructorBoot") != capability.expected_constructor_boot
            or owner.get("identityFingerprint") != capability.identity_fingerprint
            or owner.get("ownerPid") != capability.owner_pid
            or owner.get("ownerProcessStart") != capability.owner_process_start
            or owner.get("desktopLeaseHeld") is not capability.desktop_lease_held
            or owner.get("guardianArmed") is not capability.guardian_armed
            or owner.get("profileFingerprint") != capability.profile_fingerprint
            or owner.get("B0") != capability.b0
            or owner.get("expectedConstructorBoot") != capability.expected_constructor_boot
            or owner.get("expiresMonotonic") != capability.expires_monotonic
        ):
            return False
        if not isinstance(self.driver.backend, X11Backend) or not self._native_region_enabled():
            return False
        # Preserve the original construction snapshot checks as live semantic
        # predicates.  Comparing a token with its copied fields alone would
        # allow a caller to mint a self-consistent but no-longer-owned token.
        if capability.owner_pid != os.getpid():
            return False
        current_process_start = _proc_start(os.getpid())
        if (
            not isinstance(current_process_start, str)
            or current_process_start != capability.owner_process_start
        ):
            return False
        if capability.desktop_lease_held is not True or capability.guardian_armed is not False:
            return False
        if id(self.driver.backend) != capability.backend_id:
            return False
        if (
            self._native_construction_profile_fingerprint(self.driver.profile)
            != capability.profile_fingerprint
        ):
            return False
        guardian = getattr(self.driver, "guardian", None)
        lease = getattr(guardian, "lease", None)
        guardian_lock = getattr(guardian, "_lock", None)
        if guardian is None or guardian_lock is None or lease is None:
            return False
        with guardian_lock:
            if (
                guardian.generation != capability.guardian_generation
                or bool(guardian.armed) != capability.guardian_armed
                or bool(lease.held) != capability.desktop_lease_held
            ):
                return False
        operation_deadline = self._native_construction_operation_deadline
        return (
            isinstance(operation_deadline, (int, float))
            and math.isfinite(float(operation_deadline))
            and self._native_clock() < float(operation_deadline)
            and self._native_clock() < capability.expires_monotonic
        )

    def _native_update_identity_budget_capability_valid(
        self,
        capability: Any,
        provider: Any,
        identity: WindowIdentity,
        stage: str,
        ordinal: int,
    ) -> bool:
        """Verify the exact daemon witness before calibration skips a poll."""

        if (
            stage != "updates"
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 1 <= ordinal <= 99
            or not isinstance(identity, WindowIdentity)
            or not self._native_exact_provider(provider, self)
        ):
            return False
        with self._native_construction_lock:
            return self._native_update_capability_matches_locked(capability, identity)

    def _native_update_identity_budget_capability(
        self,
        provider: Any,
        identity: WindowIdentity,
        stage: str,
        ordinal: int,
    ) -> _NativeUpdateCapability | None:
        """Return a daemon-owned proof only for the exact deferred update path."""

        if (
            stage != "updates"
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 1 <= ordinal <= 99
            or not self._native_exact_provider(provider, self)
            or not isinstance(identity, WindowIdentity)
        ):
            return None
        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.NATIVE_CALIBRATION:
                return None
            generation = status.generation
        if not isinstance(self.driver.backend, X11Backend) or not self._native_region_enabled():
            return None
        with self._native_construction_lock:
            context = self._native_construction_run_context
            reservation = self._native_construction_reservation
            if (
                not isinstance(context, dict)
                or context.get("closed")
                or not context.get("deferred")
                or not isinstance(reservation, dict)
                or reservation.get("reservationState") != "consumed"
            ):
                return None
            guardian = getattr(self.driver, "guardian", None)
            lease = getattr(guardian, "lease", None) if guardian is not None else None
            if guardian is None or lease is None:
                return None
            capability = _NativeUpdateCapability(
                generation=generation,
                guardian_generation=reservation.get("guardianGeneration"),
                epoch=self._native_construction_epoch,
                reservation_id=reservation.get("reservationId"),
                run_id=context.get("runId"),
                identity_fingerprint=identity.fingerprint,
                expected_state=ControllerState.NATIVE_CALIBRATION,
                profile_fingerprint=reservation.get("profileFingerprint"),
                owner_pid=reservation.get("ownerPid"),
                owner_process_start=reservation.get("ownerProcessStart"),
                desktop_lease_held=reservation.get("desktopLeaseHeld"),
                guardian_armed=reservation.get("guardianArmed"),
                backend_id=id(self.driver.backend),
                b0=reservation.get("B0"),
                expected_constructor_boot=reservation.get("expectedConstructorBoot"),
                expires_monotonic=reservation.get("expiresMonotonic"),
            )
            if (
                not isinstance(capability.guardian_generation, int)
                or isinstance(capability.guardian_generation, bool)
                or not isinstance(capability.epoch, int)
                or not isinstance(capability.reservation_id, str)
                or not isinstance(capability.run_id, str)
                or not isinstance(capability.profile_fingerprint, str)
                or not isinstance(capability.owner_pid, int)
                or not isinstance(capability.owner_process_start, str)
                or not isinstance(capability.desktop_lease_held, bool)
                or not isinstance(capability.guardian_armed, bool)
                or not isinstance(capability.b0, str)
                or not isinstance(capability.expected_constructor_boot, str)
                or not isinstance(capability.expires_monotonic, (int, float))
                or isinstance(capability.expires_monotonic, bool)
                or not math.isfinite(float(capability.expires_monotonic))
                or not self._native_update_capability_matches_locked(capability, identity)
            ):
                return None
            return capability

    def _native_construction_record_terminal_locked(
        self,
        reservation: dict[str, Any] | None,
        *,
        state: str,
        failure: str = "",
        handshake: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Retain one immutable, bounded terminal receipt before owner clear."""

        if not isinstance(reservation, dict):
            return None
        reservation_id = reservation.get("reservationId")
        if not isinstance(reservation_id, str) or not reservation_id:
            return None
        existing = self._native_construction_terminals.get(reservation_id)
        if existing is not None:
            return copy.deepcopy(existing)
        terminal_reservation = copy.deepcopy(reservation)
        terminal_reservation["reservationState"] = state
        terminal_handshake = copy.deepcopy(handshake or self._native_construction_handshake)
        terminal_handshake["state"] = state
        if failure:
            terminal_handshake["failure"] = str(failure)
        terminal = {
            "reservation": terminal_reservation,
            "handshake": terminal_handshake,
            "stateHistory": copy.deepcopy(self._native_construction_state_history),
        }
        self._native_construction_terminals[reservation_id] = terminal
        while len(self._native_construction_terminals) > self._NATIVE_CONSTRUCTION_TERMINAL_LIMIT:
            self._native_construction_terminals.pop(next(iter(self._native_construction_terminals)))
        return copy.deepcopy(terminal)

    def _native_construction_publish_state_locked(self, state: str, **updates: Any) -> None:
        """Publish one private construction state and retain its bounded history.

        The history is deliberately separate from the handshake payload: it is
        returned only by construction IPC and terminal rechecks, while public
        status remains byte-compatible.  Repeated polling or field updates in
        the same state never append another event.
        """

        if not isinstance(state, str) or not state:
            raise DriverError("native construction state is malformed")
        current = self._native_construction_handshake.get("state")
        history = self._native_construction_state_history
        if current != state:
            now = float(self._native_clock())
            if not math.isfinite(now):
                raise DriverError("native construction state clock is malformed")
            if history and now < float(history[-1]["nativeClock"]):
                raise DriverError("native construction state clock moved backwards")
            if len(history) >= self._NATIVE_CONSTRUCTION_HISTORY_LIMIT:
                raise DriverError("native construction state history is full")
            history = [*history, {"state": state, "nativeClock": now}]
            self._native_construction_state_history = history
        self._native_construction_handshake.update(updates)
        self._native_construction_handshake["state"] = state
        context = self._native_construction_run_context
        if context is not None and not context.get("closed"):
            updated_context = copy.deepcopy(context)
            updated_context["stateHistory"] = copy.deepcopy(history)
            self._native_construction_run_context = updated_context

    def _native_construction_reset_history_locked(self) -> None:
        self._native_construction_state_history = []

    def _native_construction_start_context_locked(
        self, *, generation: int, deferred: bool, reservation_id: str | None = None
    ) -> dict[str, Any]:
        """Seal one callback context while the admission lock is held."""

        self._native_construction_epoch += 1
        owner = self._native_construction_inflight or self._native_construction_reservation
        if owner is None:
            # A legacy ownerless start creates its binding here while the
            # construction lock is held.  Once an owner exists, however, a
            # missing original generation is malformed and must fail closed;
            # sampling the current Guardian generation would let stale state
            # borrow a newer owner.
            guardian_generation = self._native_construction_guardian_generation()
        elif not isinstance(owner, dict) or "guardianGeneration" not in owner:
            raise DriverError("native construction owner is missing Guardian generation")
        else:
            guardian_generation = owner["guardianGeneration"]
        context = {
            "runId": secrets.token_hex(16),
            "epoch": self._native_construction_epoch,
            "generation": generation,
            "guardianGeneration": guardian_generation,
            # Preserve the immutable construction binding beside the mutable
            # reservation.  Update witnesses must not be reminted from a
            # self-consistent reservation after B0 has changed.
            "constructionB0": owner.get("B0") if isinstance(owner, dict) else None,
            "expectedConstructorBoot": (
                owner.get("expectedConstructorBoot") if isinstance(owner, dict) else None
            ),
            "deferred": deferred,
            "reservationId": reservation_id,
            "closed": False,
            "stateHistory": copy.deepcopy(self._native_construction_state_history),
        }
        self._native_construction_sealed_run_id = context["runId"]
        self._native_construction_run_context = context
        return copy.deepcopy(context)

    def _native_construction_close_context_locked(self, context: dict[str, Any]) -> None:
        current = self._native_construction_run_context
        if current is not None and current.get("runId") == context.get("runId"):
            self._native_update_alignment_runner = None
            self._native_update_alignment_anchor = None
            closed = copy.deepcopy(current)
            closed["closed"] = True
            closed["stateHistory"] = copy.deepcopy(self._native_construction_state_history)
            self._native_construction_run_context = closed

    def _native_update_alignment_enter(self, provider: Any, identity: WindowIdentity) -> object | None:
        """Bind alignment to one live runner invocation of the installed provider."""

        if not self._native_exact_provider(provider, self) or not isinstance(identity, WindowIdentity):
            return None
        token = object()
        with self._native_construction_lock:
            context = self._native_construction_run_context
            self._native_update_alignment_runner = {
                "token": token,
                "provider": provider,
                "identity": identity.fingerprint,
                "runId": context.get("runId") if isinstance(context, dict) else None,
                "generation": context.get("generation") if isinstance(context, dict) else None,
                "epoch": self._native_construction_epoch,
            }
            self._native_update_alignment_anchor = None
        return token

    def _native_update_alignment_exit(self, token: object | None) -> None:
        """Clear provisional alignment state at every runner terminal."""

        with self._native_construction_lock:
            runner = self._native_update_alignment_runner
            if runner is None or runner.get("token") is token:
                self._native_update_alignment_runner = None
                self._native_update_alignment_anchor = None

    def _native_update_alignment_wait(
        self,
        *,
        identity: WindowIdentity,
        generation: int,
        capability: _NativeUpdateCapability,
        ordinal: int,
        deadline: float,
    ) -> float:
        """Wait for the immutable publisher-derived target before ACK admission.

        The method deliberately performs no producer work and holds no authority
        lock while sleeping.  The caller invokes it from the private driver
        pre-ACK hook, after the exact producer has passed owner admission.  The
        returned target is retained for diagnostic accounting only.
        """

        with self._native_construction_lock:
            runner = self._native_update_alignment_runner
            anchor = self._native_update_alignment_anchor
            if (
                not isinstance(runner, dict)
                or not isinstance(anchor, dict)
                or runner.get("identity") != identity.fingerprint
                or runner.get("generation") != generation
                or runner.get("epoch") != capability.epoch
                or anchor.get("nextOrdinal") != ordinal
                or anchor.get("runId") != capability.run_id
                or anchor.get("generation") != capability.generation
                or anchor.get("identity") != identity.fingerprint
            ):
                raise DriverError("native update alignment anchor is missing or stale")
            origin_capture_started = anchor.get("originCaptureStarted")
            origin_uptime_ms = anchor.get("originUptimeMs")
            previous_uptime_ms = anchor.get("lastUptimeMs")
            if (
                isinstance(origin_capture_started, bool)
                or not isinstance(origin_capture_started, (int, float))
                or not math.isfinite(float(origin_capture_started))
                or isinstance(origin_uptime_ms, bool)
                or not isinstance(origin_uptime_ms, int)
                or isinstance(previous_uptime_ms, bool)
                or not isinstance(previous_uptime_ms, int)
                or not 0 <= origin_uptime_ms <= 0xFFFFFFFF
                or not 0 <= previous_uptime_ms <= 0xFFFFFFFF
            ):
                self._native_update_alignment_anchor = None
                raise DriverError("native update alignment origin is malformed")
            try:
                publisher_elapsed_ms = unsigned_delta(previous_uptime_ms, origin_uptime_ms)
            except ProtocolError as exc:
                self._native_update_alignment_anchor = None
                raise DriverError("native update alignment publisher origin is ambiguous") from exc
            diagnostic = getattr(self, "_native_observation_diagnostic", None)
            with _native_diagnostic_span(diagnostic, "alignment_target"):
                target = self._native_update_alignment_target(
                    float(origin_capture_started), publisher_elapsed_ms
                )
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(float(deadline))
                or not math.isfinite(target)
                or target < float(origin_capture_started)
            ):
                self._native_update_alignment_anchor = None
                raise DriverError("native update alignment target is malformed")
            earliest_deadline = float(deadline)
            for candidate in (
                capability.expires_monotonic,
                self._native_construction_operation_deadline,
                (self._native_construction_reservation or {}).get("expiresMonotonic"),
            ):
                if candidate is None:
                    continue
                if (
                    isinstance(candidate, bool)
                    or not isinstance(candidate, (int, float))
                    or not math.isfinite(float(candidate))
                ):
                    self._native_update_alignment_anchor = None
                    raise DriverError("native update alignment deadline is malformed")
                earliest_deadline = min(earliest_deadline, float(candidate))
            now = self._native_clock()
            # A target that reaches or exceeds any authority deadline cannot
            # leave room for the final identity/Guardian checks and one-shot
            # ACK.  Refuse before the first sleep rather than discovering this
            # only after spending the remaining capture budget.
            if now >= earliest_deadline or target >= earliest_deadline:
                self._native_update_alignment_anchor = None
                raise DriverError("native update alignment target cannot fit its deadline")
            anchor["waitingOrdinal"] = ordinal
            anchor["targetMonotonic"] = target

        while self._native_clock() < target:
            now = self._native_clock()
            if now >= earliest_deadline:
                with self._native_construction_lock:
                    self._native_update_alignment_anchor = None
                raise DriverError("native update alignment exceeded its earliest deadline")
            remaining = target - now
            if remaining > 0:
                # Keep replay/test clock injection local to this alignment
                # wait.  Patching ``daemon.time.sleep`` would mutate the
                # process-wide stdlib module and can change Guardian
                # lifecycle timing in unrelated threads.
                sleep = getattr(self, "_native_alignment_sleep", time.sleep)
                sleep(min(0.05, remaining))
            self._check_native_poll_identity(
                identity,
                generation,
                native_capability=capability,
            )
            if self._native_clock() >= earliest_deadline:
                with self._native_construction_lock:
                    self._native_update_alignment_anchor = None
                raise DriverError("native update alignment exceeded its earliest deadline")
        self._check_native_poll_identity(identity, generation, native_capability=capability)
        return target

    def _native_update_alignment_target(
        self, origin_capture_started: float, publisher_elapsed_ms: float
    ) -> float:
        """Compute the publisher-derived target without changing its origin."""

        return float(origin_capture_started) + float(publisher_elapsed_ms) / 1000.0 + 1.050

    def _native_update_alignment_pre_ack(
        self,
        *,
        identity: WindowIdentity,
        generation: int,
        capability: _NativeUpdateCapability,
        ordinal: int,
        producer_pid: int,
        dispatch_ack_once: Callable[[], None],
        deadline: float,
        admitted_pid: int | None,
    ) -> None:
        """Align one exact admitted producer, then dispatch its ACK once."""

        if not isinstance(producer_pid, int) or isinstance(producer_pid, bool) or producer_pid <= 0:
            raise DriverError("native pre-ACK producer PID is malformed")
        if admitted_pid != producer_pid:
            raise DriverError("native pre-ACK producer PID is not the admitted process")
        self._native_update_alignment_wait(
            identity=identity,
            generation=generation,
            capability=capability,
            ordinal=ordinal,
            deadline=deadline,
        )
        guardian = self.driver.guardian
        with self.controller.lock:
            status = self.controller.status()
            if (
                status.state is not ControllerState.NATIVE_CALIBRATION
                or status.generation != generation
                or status.stop_requested
                or self._shutdown_event.is_set()
            ):
                raise DriverError("native pre-ACK controller admission was revoked")
            with self._native_construction_lock:
                if not self._native_update_capability_matches_locked(capability, identity):
                    raise DriverError("native pre-ACK identity capability was revoked")
                anchor = self._native_update_alignment_anchor
                if (
                    not isinstance(anchor, dict)
                    or anchor.get("nextOrdinal") != ordinal
                    or anchor.get("runId") != capability.run_id
                    or anchor.get("waitingOrdinal") != ordinal
                ):
                    raise DriverError("native pre-ACK alignment anchor was replaced")
                diagnostic = getattr(self, "_native_observation_diagnostic", None)
                with _native_diagnostic_span(diagnostic, "ack_admission"):
                    with guardian._lock:
                        guardian.admission_gate_ready()
                        operation_deadline = self._native_construction_operation_deadline
                        reservation = self._native_construction_reservation
                        reservation_deadline = (
                            reservation.get("expiresMonotonic") if isinstance(reservation, dict) else None
                        )
                        if (
                            self._shutdown_event.is_set()
                            or self._native_clock() >= float(deadline)
                            or self._native_clock() >= capability.expires_monotonic
                            or (
                                isinstance(operation_deadline, (int, float))
                                and self._native_clock() >= float(operation_deadline)
                            )
                            or (
                                isinstance(reservation_deadline, (int, float))
                                and self._native_clock() >= float(reservation_deadline)
                            )
                        ):
                            raise DriverError("native pre-ACK deadline or shutdown was revoked")
                        if (
                            guardian.generation != capability.guardian_generation
                            or guardian.armed
                            or guardian.held
                            or not guardian.lease.held
                            or guardian.active_process_groups
                            or producer_pid not in guardian.observer_process_groups
                            or getattr(guardian, "_release_failed", False)
                        ):
                            raise DriverError("native pre-ACK Guardian admission is not ready")
                        dispatch_ack_once()

    def _native_update_alignment_record(
        self,
        *,
        provider: Any,
        identity: WindowIdentity,
        capability: _NativeUpdateCapability | None,
        ordinal: int,
        capture_monotonic: float,
        publisher_uptime_ms: int,
        receipt: Any,
        capture_digest: str,
        previous_uptime_ms: int | None,
    ) -> None:
        """Publish a provisional receipt-backed publisher origin."""

        if capability is None or ordinal < 1 or ordinal >= 99:
            return
        if not isinstance(capture_monotonic, (int, float)) or not math.isfinite(float(capture_monotonic)):
            raise DriverError("native update capture timestamp cannot anchor alignment")
        if (
            isinstance(publisher_uptime_ms, bool)
            or not isinstance(publisher_uptime_ms, int)
            or not 0 <= publisher_uptime_ms <= 0xFFFFFFFF
            or not isinstance(capture_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture_digest)
            or receipt is None
            or getattr(receipt, "output_sha256", None) != capture_digest
            or getattr(receipt, "attempt_retired", None) is not True
            or not isinstance(getattr(receipt, "capture_started", None), (int, float))
            or isinstance(getattr(receipt, "capture_started", None), bool)
            or not math.isfinite(float(receipt.capture_started))
            or float(receipt.capture_started) > self._native_clock()
        ):
            raise DriverError("native update producer receipt cannot anchor alignment")
        expected_rectangle = (
            identity.child_geometry[0] + self.driver.profile.canvas_crop[0],
            identity.child_geometry[1] + self.driver.profile.canvas_crop[1],
            self.driver.profile.canvas_crop[2],
            self.driver.profile.canvas_crop[3],
        )
        if (
            tuple(getattr(receipt, "rectangle", ())) != expected_rectangle
            or getattr(receipt, "executable_sha256", None)
            != self.driver.profile.telemetry_region_executable_sha256
            or not isinstance(getattr(receipt, "nonce", None), str)
            or not receipt.nonce
            or not isinstance(getattr(receipt, "owner_pid", None), int)
            or receipt.owner_pid <= 0
            or not isinstance(getattr(receipt, "owner_process_start", None), str)
            or not receipt.owner_process_start
        ):
            raise DriverError("native update producer receipt is not bound to this capture")
        if previous_uptime_ms is not None and (
            isinstance(previous_uptime_ms, bool)
            or not isinstance(previous_uptime_ms, int)
            or not 0 <= previous_uptime_ms <= 0xFFFFFFFF
        ):
            raise DriverError("native update previous publisher uptime is malformed")
        with self._native_construction_lock:
            runner = self._native_update_alignment_runner
            if (
                not isinstance(runner, dict)
                or not self._native_exact_provider(runner.get("provider"), self)
                or not self._native_exact_provider(provider, self)
                or getattr(runner.get("provider"), "__func__", None)
                is not getattr(provider, "__func__", None)
                or runner.get("identity") != identity.fingerprint
                or runner.get("runId") != capability.run_id
                or runner.get("epoch") != capability.epoch
            ):
                return
            runner_anchor = self._native_update_alignment_anchor
            if ordinal > 1 and (
                not isinstance(runner_anchor, dict)
                or runner_anchor.get("nextOrdinal") != ordinal
                or runner_anchor.get("lastUptimeMs") != previous_uptime_ms
            ):
                raise DriverError("native update publisher origin is stale")
            if ordinal == 1:
                origin_capture_started = float(receipt.capture_started)
                origin_uptime_ms = publisher_uptime_ms
            else:
                if not isinstance(runner_anchor, dict):
                    raise DriverError("native update publisher origin is missing")
                origin_capture_started = runner_anchor.get("originCaptureStarted")
                origin_uptime_ms = runner_anchor.get("originUptimeMs")
            self._native_update_alignment_anchor = {
                "runId": capability.run_id,
                "generation": capability.generation,
                "epoch": capability.epoch,
                "identity": identity.fingerprint,
                "fromOrdinal": ordinal,
                "nextOrdinal": ordinal + 1,
                "originCaptureStarted": origin_capture_started,
                "originUptimeMs": origin_uptime_ms,
                "lastCaptureStarted": float(receipt.capture_started),
                "lastUptimeMs": publisher_uptime_ms,
                "lastReceiptNonce": receipt.nonce,
                "lastCaptureDigest": capture_digest,
                "waitingOrdinal": None,
            }

    def _begin_calibration(
        self,
        kind: str,
        generation: int | None,
        *,
        admitted_context: dict[str, Any] | None = None,
    ) -> None:
        if kind not in {"native", "input"}:
            raise DriverError("unknown calibration operation")
        self._native_construction_flush_expiry()
        with self._native_construction_lock:
            reservation = self._native_construction_reservation
            if kind == "native":
                if self._native_construction_inflight is not None:
                    raise DriverError("another native construction admission is already active")
                if reservation is not None and reservation.get("reservationState") != "consumed":
                    raise DriverError("native construction admission is already active")
            if not self._calibration_lock.acquire(blocking=False):
                raise DriverError("a calibration operation is already running")
            try:
                with self._calibration_state_lock:
                    current = self._calibration_operation.get("state")
                    if current == "running":
                        raise DriverError("a calibration operation is already running")
                    if current == "starting" and self._calibration_operation.get("kind") != kind:
                        raise DriverError("a calibration operation is already starting")
                    deferred = False
                    if kind == "native" and admitted_context is not None:
                        current_context = self._native_construction_run_context
                        if (
                            current_context is None
                            or current_context.get("closed")
                            or current_context.get("runId") != admitted_context.get("runId")
                            or current_context.get("epoch") != admitted_context.get("epoch")
                        ):
                            raise DriverError("native calibration admission owner was revoked")
                        deferred = bool(admitted_context.get("deferred"))
                        if deferred:
                            if reservation is None or reservation.get(
                                "reservationId"
                            ) != admitted_context.get("reservationId"):
                                raise DriverError("native deferred calibration admission was revoked")
                    else:
                        deferred = (
                            kind == "native"
                            and reservation is not None
                            and reservation.get("reservationState") == "consumed"
                        )
                        self._native_construction_start_context_locked(
                            generation=generation if generation is not None else -1,
                            deferred=deferred,
                            reservation_id=reservation.get("reservationId") if deferred else None,
                        )
                    updated_operation = {
                        "state": "running",
                        "kind": kind,
                        "generation": generation if generation is not None else -1,
                        "startedAt": int(time.time()),
                        "finishedAt": 0,
                        "error": "",
                        "readiness": None,
                        "result": None,
                    }
                    if current == "starting" and self._calibration_operation.get("kind") == kind:
                        self._calibration_operation.update(updated_operation)
                    else:
                        self._calibration_operation = updated_operation
            except Exception:
                self._calibration_lock.release()
                raise

    def start_calibration(self, kind: str, *, expected_generation: int | None = None) -> dict[str, Any]:
        """Start one supervised long-running gate and return immediately.

        Calibration can legitimately wait for a human sample or 101 fresh
        frames.  Socket clients therefore receive an observable operation
        record instead of holding a request open until the gate finishes.
        """

        if kind not in {"native", "input"}:
            raise DriverError("unknown calibration operation")
        # The construction lock and calibration-state lock form one admission
        # point. Prepare/recovery re-check the same state while holding the
        # construction lock, so a competing thread cannot slip between the
        # old check and publication of ``starting``.
        self._native_construction_flush_expiry()
        admitted_context: dict[str, Any] | None = None
        with self._native_construction_lock:
            if self._native_construction_blocks_competing_entry(native_calibration=kind == "native"):
                raise DriverError("another native construction admission is already active")
            with self._calibration_state_lock:
                current = self._calibration_operation.get("state")
                if current in {"starting", "running"}:
                    raise DriverError("a calibration operation is already running")
                self._calibration_operation = {
                    "state": "starting",
                    "kind": kind,
                    "generation": expected_generation if expected_generation is not None else -1,
                    "startedAt": int(time.time()),
                    "finishedAt": 0,
                    "error": "",
                    "readiness": None,
                    "result": None,
                }
                if kind == "native":
                    reservation = self._native_construction_reservation
                    deferred = reservation is not None and reservation.get("reservationState") == "consumed"
                    admitted_context = self._native_construction_start_context_locked(
                        generation=expected_generation if expected_generation is not None else -1,
                        deferred=deferred,
                        reservation_id=reservation.get("reservationId") if deferred else None,
                    )
                operation_record = self._calibration_operation

        def worker() -> None:
            try:
                if kind == "native":
                    self.run_native_calibration(
                        expected_generation=expected_generation,
                        admitted_context=admitted_context,
                    )
                else:
                    self.run_input_calibration(expected_generation=expected_generation)
                result_snapshot = self.status_dict()
                with self._calibration_state_lock:
                    # Pollers need the final complete/error state, but the
                    # status snapshot takes the controller lock.  Compute it
                    # before taking the calibration-state lock to preserve the
                    # controller -> construction -> calibration lock order.
                    if self._calibration_operation is operation_record:
                        operation_record["result"] = result_snapshot
            except Exception as exc:
                with self._calibration_state_lock:
                    if self._calibration_operation is operation_record:
                        operation_record["error"] = str(exc)
            finally:
                with self._calibration_state_lock:
                    if (
                        self._calibration_operation is operation_record
                        and operation_record.get("state") == "starting"
                    ):
                        operation_record["state"] = "error"
                        operation_record["finishedAt"] = int(time.time())

        thread = threading.Thread(target=worker, name=f"crowley-{kind}-calibration", daemon=True)
        self._calibration_thread = thread
        thread.start()
        return self.status_dict()

    def calibration_status(self) -> dict[str, Any]:
        return self.status_dict()

    def _finish_calibration(self, *, state: str, error: str = "") -> None:
        with self._calibration_state_lock:
            self._calibration_operation["state"] = state
            self._calibration_operation["finishedAt"] = int(time.time())
            self._calibration_operation["error"] = error
        self._calibration_lock.release()

    def _set_calibration_readiness(self, readiness: Any) -> None:
        with self._calibration_state_lock:
            self._calibration_operation["readiness"] = self._sanitize_readiness(readiness)

    def _persist_takeover_hover_failure(self, context: dict[str, Any]) -> Path:
        """Persist one bounded private hover failure without affecting its error path."""

        artifact = copy.deepcopy(context)
        try:
            artifact["sourceBindings"] = _runtime_source_hashes()
        except Exception as exc:
            artifact["sourceBindingsError"] = _bounded_diagnostic_error(exc)
        artifact["persistedAtNs"] = time.time_ns()
        artifact["artifact"] = "latest-takeover-hover-failure"
        encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            observer = artifact.get("observer")
            if isinstance(observer, dict):
                observer["rawBlocks"] = []
                observer["partialRaw"] = ""
                observer["rawTruncated"] = True
            artifact["artifactTruncated"] = True
            encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        if len(encoded) > 2 * 1024 * 1024:
            raise DriverError("takeover hover diagnostic exceeds its size bound")
        journal = Path(self.config.journal).expanduser()
        directory = journal.parent / "takeover-drill-diagnostics"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        destination = directory / "latest-hover-failure.json"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".hover-failure-", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            return destination
        finally:
            if descriptor != -1:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _takeover_hover_failure_summary(context: dict[str, Any], error: Exception) -> str:
        # The driver's failure handler may revoke an otherwise-armed lease as
        # cleanup. Only the snapshot captured before that cleanup can explain
        # why the hover failed; the post-cleanup snapshot remains private
        # diagnostic context and must not be promoted to the first cause.
        guardian = context.get("guardianBeforeCleanup")
        reason = guardian.get("revokedReason") if isinstance(guardian, dict) else None
        if isinstance(reason, str) and reason and reason != "startup":
            first_cause = f"takeover hover Guardian revocation: {reason}"
        else:
            observer = context.get("observer")
            failure = observer.get("failureDiagnostic") if isinstance(observer, dict) else None
            observer_reason = failure.get("reason") if isinstance(failure, dict) else None
            if isinstance(observer_reason, str) and observer_reason:
                first_cause = f"takeover hover observer failure: {observer_reason}"
            else:
                stage = context.get("stage")
                first_cause = f"takeover hover failed during {stage or 'native dispatch'}"
        return _bounded_diagnostic_error(f"{first_cause}; {type(error).__name__}: {error}")

    def _persist_input_calibration_failure(self, context: dict[str, Any] | None) -> None:
        """Best-effort private persistence for a pre-human input failure.

        This path is deliberately outside the controller journal/public status:
        raw XI2 text is diagnostic evidence, never qualification evidence.  A
        write, replace, or cleanup failure must leave the workflow's original
        exception and normal resource cleanup authoritative.
        """

        if not isinstance(context, dict):
            return
        if context.get("phase") != "pre_human" or context.get("readinessPublished"):
            return
        try:
            source_bindings = _runtime_source_hashes(include_calibration=True)
        except Exception as exc:
            source_bindings = {"error": _bounded_diagnostic_error(exc)}
        artifact = copy.deepcopy(context)
        artifact["sourceBindings"] = source_bindings
        artifact["createdAt"] = int(time.time())
        artifact["replayable"] = bool(artifact.get("replayable")) and not bool(source_bindings.get("error"))
        artifact["artifact"] = "latest-pre-human-input-failure"
        try:
            encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
            if len(encoded) > 2 * 1024 * 1024:
                observer = artifact.get("observer")
                if isinstance(observer, dict):
                    observer["rawBlocks"] = []
                    observer["partialRaw"] = ""
                    observer["rawTruncated"] = True
                artifact["replayable"] = False
                artifact["artifactTruncated"] = True
                encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                    "utf-8"
                )
            if len(encoded) > 2 * 1024 * 1024:
                raise OSError("input calibration diagnostic exceeds its size bound")
            journal = Path(self.config.journal).expanduser()
            directory = journal.parent / "input-calibration-diagnostics"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            destination = directory / "latest-pre-human-failure.json"
            temporary = directory / ".latest-pre-human-failure.json.tmp"
            temporary_owned = False
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            temporary_owned = True
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("input calibration diagnostic write made no progress")
                    offset += written
                os.fsync(fd)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            try:
                os.replace(temporary, destination)
            except Exception:
                # The O_EXCL create makes this path ours.  Remove only that
                # owned slot after an unsuccessful replacement; an existing
                # valid latest artifact must remain untouched.  If removal
                # itself fails, the fixed slot blocks future writers rather
                # than allowing unbounded diagnostic files to accumulate.
                raise
            else:
                temporary_owned = False
            os.chmod(destination, 0o600)
        except Exception as exc:
            if "temporary_owned" in locals() and temporary_owned:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
            _LOGGER.warning(
                "input calibration diagnostic persistence failed: %s",
                _bounded_diagnostic_error(exc),
            )

    @staticmethod
    def _sanitize_readiness(readiness: Any) -> dict[str, Any]:
        names = (
            "target_pid",
            "target_xid",
            "geometry",
            "synthetic_key_source",
            "synthetic_pointer_source",
            "xinput_hierarchy",
            "observed_monotonic",
        )
        result: dict[str, Any] = {}
        for name in names:
            value = getattr(readiness, name, None)
            if isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, list):
                value = [list(item) if isinstance(item, tuple) else item for item in value]
            if isinstance(value, (str, int, float, bool)) or value is None or isinstance(value, list):
                result[name] = value
        result["ready"] = True
        return result

    # ------------------------------------------------------------------
    # Deferred native-construction admission

    @staticmethod
    def _native_construction_profile_fingerprint(profile: Any) -> str:
        try:
            payload = json.dumps(
                _canonical_semantic_value(asdict(profile)),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise DriverError("native construction profile cannot be canonicalized") from exc
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _native_construction_positive_generation(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _native_construction_reservation_id(value: Any, *, allow_empty: bool = False) -> bool:
        return (
            isinstance(value, str)
            and (allow_empty or bool(value))
            and len(value) <= 128
            and "\x00" not in value
        )

    def _native_construction_guardian_generation(self) -> int:
        """Capture the current Guardian generation as an owner fact."""

        guardian = getattr(self.driver, "guardian", None)
        guardian_lock = getattr(guardian, "_lock", None)
        if guardian is None or guardian_lock is None:
            raise DriverError("native construction Guardian binding is unavailable")
        with guardian_lock:
            generation = getattr(guardian, "generation", None)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise DriverError("native construction Guardian generation is malformed")
        return generation

    def _native_construction_binding(
        self,
        *,
        reservation_id: str,
        reservation_kind: str,
        reservation_state: str,
        controller_generation: int,
        expected_boot: str,
        identity: WindowIdentity,
        expires_monotonic: float,
        guardian_generation: int | None = None,
        guardian_armed: bool | None = None,
        desktop_lease_held: bool | None = None,
    ) -> dict[str, Any]:
        guardian = getattr(self.driver, "guardian", None)
        lease = getattr(guardian, "lease", None)
        owner_start = _proc_start(os.getpid())
        if not owner_start:
            raise DriverError("native construction owner process start is unavailable")
        if not isinstance(expected_boot, str) or not expected_boot:
            raise DriverError("native construction boot is malformed")
        expected_constructor_boot = f"native-construction:{expected_boot}"
        if len(expected_constructor_boot.encode("utf-8")) > 96:
            raise DriverError("native construction boot is too long")
        if guardian_armed is None or desktop_lease_held is None:
            guardian_lock = getattr(guardian, "_lock", None)
            if guardian is None or guardian_lock is None:
                raise DriverError("native construction Guardian binding is unavailable")
            # The owner generation is supplied by the owner-creation
            # transaction.  Only the auxiliary armed/lease facts are sampled
            # here; a missing or stale generation is never replaced with a
            # later current value.
            with guardian_lock:
                if guardian_armed is None:
                    guardian_armed = bool(getattr(guardian, "armed", False))
                if desktop_lease_held is None:
                    desktop_lease_held = bool(getattr(lease, "held", False))
        if (
            not isinstance(guardian_generation, int)
            or isinstance(guardian_generation, bool)
            or guardian_generation < 0
        ):
            raise DriverError("native construction Guardian generation is malformed")
        return {
            "reservationId": reservation_id,
            "reservationKind": reservation_kind,
            "reservationState": reservation_state,
            "controllerGeneration": controller_generation,
            "desktopLeaseHeld": bool(desktop_lease_held),
            "guardianGeneration": guardian_generation,
            "guardianArmed": bool(guardian_armed),
            "ownerPid": os.getpid(),
            "ownerProcessStart": owner_start,
            "profileFingerprint": self._native_construction_profile_fingerprint(self.driver.profile),
            "identityFingerprint": identity.fingerprint,
            "B0": expected_boot,
            "expectedConstructorBoot": expected_constructor_boot,
            "expiresMonotonic": float(expires_monotonic),
        }

    @staticmethod
    def _native_construction_public_handshake(handshake: dict[str, Any]) -> dict[str, Any]:
        """Return only non-secret state for private IPC callers.

        The reservation record is returned separately.  This function is used
        only by the private construction IPC replies and never by status_dict.
        """

        return {
            key: copy.deepcopy(value)
            for key, value in handshake.items()
            if key
            not in {
                "B0",
                "expectedConstructorBoot",
                "reservationId",
                "ownerProcessStart",
                "profileFingerprint",
                "identityFingerprint",
            }
        }

    def _native_construction_reply(self, *, include_reservation: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "handshake": self._native_construction_public_handshake(self._native_construction_handshake),
            "nativeClock": self._native_clock(),
            "stateHistory": copy.deepcopy(self._native_construction_state_history),
        }
        if include_reservation and self._native_construction_reservation is not None:
            result["reservation"] = copy.deepcopy(self._native_construction_reservation)
        return result

    def _native_construction_terminal_reply(self, reservation_id: str) -> dict[str, Any] | None:
        with self._native_construction_lock:
            return self._native_construction_terminal_reply_locked(reservation_id)

    def _native_construction_terminal_reply_locked(
        self, reservation_id: str, *, expected_generation: int | None = None
    ) -> dict[str, Any] | None:
        """Copy one retained terminal while the construction lock is held.

        Rechecks can validate an active reservation without the construction
        lock.  Finalization may therefore move that reservation to the
        bounded terminal ledger before the recheck's second lock.  This
        helper is deliberately read-only and uses the same canonical payload
        as the first-lock terminal path.
        """

        terminal = self._native_construction_terminals.get(reservation_id)
        if terminal is None:
            return None
        if (
            expected_generation is not None
            and terminal["reservation"].get("controllerGeneration") != expected_generation
        ):
            raise DriverError("native construction terminal generation is stale")
        return {
            "reservation": copy.deepcopy(terminal["reservation"]),
            "handshake": self._native_construction_public_handshake(copy.deepcopy(terminal["handshake"])),
            "nativeClock": self._native_clock(),
            "stateHistory": copy.deepcopy(terminal["stateHistory"]),
        }

    def _native_construction_expire_locked(self) -> None:
        reservation = self._native_construction_reservation
        if reservation is None:
            return
        expires = reservation.get("expiresMonotonic")
        if isinstance(expires, (int, float)) and self._native_clock() < float(expires):
            operation_deadline = self._native_construction_operation_deadline
            if operation_deadline is None or self._native_clock() < operation_deadline:
                return
        # Expiry is fail-closed.  A durable checkpoint remains for recovery.
        reservation_snapshot = copy.deepcopy(reservation)
        self._native_construction_publish_state_locked(
            "error", failure="native construction reservation expired"
        )
        self._native_construction_record_terminal_locked(
            reservation_snapshot,
            state="error",
            failure="native construction reservation expired",
        )
        self._native_construction_reservation = None
        self._native_construction_ticket = None
        self._native_construction_operation_deadline = None
        self._native_construction_finalize_event.clear()
        self._native_construction_expired = True
        self._native_construction_epoch += 1
        context = self._native_construction_run_context
        if context is not None:
            closed = copy.deepcopy(context)
            closed["closed"] = True
            closed["stateHistory"] = copy.deepcopy(self._native_construction_state_history)
            self._native_construction_run_context = closed

    def _native_construction_capture(
        self,
        *,
        deadline: float,
        expected_generation: int | None = None,
        expected_state: ControllerState | None = None,
        native_capability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture one fresh strict frame through the production provider.

        Offline tests may supply a capture fixture through the same method
        boundary by setting ``_native_construction_observation_provider``;
        no private handshake state is advanced by that fixture.
        """

        provider = getattr(self, "_native_construction_observation_provider", None)
        if callable(provider):
            value = provider()
            if not isinstance(value, dict):
                raise DriverError("native construction observation is malformed")
            return copy.deepcopy(value)
        if not isinstance(self.driver.backend, X11Backend):
            raise DriverError("native construction requires the configured X11 backend")
        identity = self.driver.resolve_identity()
        decoder = self.driver.new_native_decoder()
        capture_started = self._native_clock()
        capture_path = None
        try:
            capture_path = self._capture_native_region_for_identity(
                identity,
                deadline=deadline,
                expected_generation=expected_generation,
                expected_state=expected_state,
                native_capability=native_capability,
            )
            capture_id = hashlib.sha256(capture_path.read_bytes()).hexdigest()
            try:
                sample = self.driver.decode_native_capture(capture_path, identity=identity, decoder=decoder)
            except ProtocolError as exc:
                if not self._strict_native_absence_capture(capture_path):
                    raise DriverError(
                        "native construction capture contains a malformed encoded surface"
                    ) from exc
                return {
                    "surface": "absent",
                    "boot": "",
                    "sequence": 0,
                    "crc32": 0,
                    "snapshot": {},
                    "identityFingerprint": identity.fingerprint,
                    "captureIdentity": identity.fingerprint,
                    "captureMonotonic": capture_started,
                    "captureSha256": capture_id,
                }
            if sample.frame is None or sample.kind != "FRESH":
                raise DriverError("native construction capture is not fresh")
            frame = sample.frame
            return {
                "surface": "present",
                "boot": frame.boot,
                "sequence": frame.sequence,
                "crc32": frame.crc32,
                "snapshot": frame.snapshot.value,
                "identityFingerprint": identity.fingerprint,
                "captureIdentity": identity.fingerprint,
                "captureMonotonic": sample.capture_monotonic,
                "captureSha256": capture_id,
            }
        finally:
            if capture_path is not None:
                self.release_native_capture(capture_path)

    def _strict_native_absence_capture(self, path: Path) -> bool:
        """Return true only for an ordinary crop with no encoded Agent grid."""
        expected_size = (
            self.config.profile.canvas_crop[2],
            self.config.profile.canvas_crop[3],
        )
        return _strict_native_absence_image(path, expected_size=expected_size)

    def _retain_native_absence_capture(
        self, identity: WindowIdentity, *, deadline: float | None = None
    ) -> dict[str, Any]:
        """Capture and retain one independently sealed ordinary absence crop."""

        capture_started = self._native_clock()
        path = self._capture_native_region_for_identity(
            identity,
            deadline=deadline,
            expected_generation=self.controller.status().generation,
            expected_state=ControllerState.NATIVE_CALIBRATION,
        )
        try:
            decoder = self.driver.new_native_decoder()
            try:
                sample = self.driver.decode_native_capture(path, identity=identity, decoder=decoder)
            except ProtocolError as exc:
                if not self._strict_native_absence_capture(path):
                    raise DriverError("post-reset capture contains a malformed encoded surface") from exc
                sample = None
            if sample is not None:
                raise DriverError("native surface was constructed before post-reset absence proof")
            evidence_dir = self.config.journal.expanduser().parent / "native-evidence"
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(evidence_dir, 0o700)
            retained = evidence_dir / f"postconstruction-{time.monotonic_ns()}.png"
            shutil.copyfile(path, retained)
            os.chmod(retained, 0o600)
            digest = hashlib.sha256(retained.read_bytes()).hexdigest()
            with self._native_bootstrap_lock:
                self._native_owned_capture_paths.add(retained)
            return {
                "identity": identity.fingerprint,
                "captureDigest": digest,
                "capturePath": str(retained),
                "surface": "absent",
                # Keep the producer-boundary timestamp. Copy/decode/hash work
                # after the capture must age the proof rather than refresh it.
                "capture": capture_started,
                "provenance": "owned-live",
            }
        finally:
            self._remove_native_capture(path)

    @staticmethod
    def _native_construction_decoded_frame_facts(
        observation: dict[str, Any],
    ) -> tuple[str, int, int, dict[str, Any]]:
        if observation.get("surface", "present") != "present":
            raise DriverError("native construction capture has no Agent frame")
        boot = observation.get("boot")
        sequence = observation.get("sequence")
        crc32 = observation.get("crc32")
        snapshot = observation.get("snapshot")
        if (
            not isinstance(boot, str)
            or not boot
            or len(boot.encode("utf-8")) > 96
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or isinstance(crc32, bool)
            or not isinstance(crc32, int)
            or crc32 < 0
            or not isinstance(snapshot, dict)
        ):
            raise DriverError("native construction frame identity is malformed")
        render = snapshot.get("render")
        if not isinstance(render, dict):
            raise DriverError("native construction frame render facts are malformed")
        phase = render.get("nativePhase")
        ordinal = render.get("nativeOrdinal")
        if (
            phase not in {"normal", "held", "updates"}
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal > 100
        ):
            raise DriverError("native construction frame phase facts are malformed")
        return boot, sequence, crc32, snapshot

    @staticmethod
    def _native_construction_frame_facts(observation: dict[str, Any]) -> tuple[str, int, int, dict[str, Any]]:
        boot, sequence, crc32, snapshot = SessionDaemon._native_construction_decoded_frame_facts(observation)
        render = snapshot["render"]
        if render.get("nativePhase") != "normal" or render.get("nativeOrdinal") != 0:
            raise DriverError("native construction requires a strict normal/0 frame")
        return boot, sequence, crc32, snapshot

    @staticmethod
    def _native_construction_observation_identity(
        observation: dict[str, Any], identity: WindowIdentity
    ) -> None:
        for key in ("identityFingerprint", "captureIdentity"):
            if observation.get(key) != identity.fingerprint:
                raise DriverError(f"native construction capture {key} is stale")

    def _native_construction_validate_snapshot(
        self, reservation: dict[str, Any], *, expected_generation: int
    ) -> WindowIdentity:
        if expected_generation != self.controller.status().generation:
            raise DriverError("native construction generation was revoked")
        if reservation.get("controllerGeneration") != expected_generation:
            raise DriverError("native construction reservation generation is stale")
        lease = getattr(getattr(self.driver, "guardian", None), "lease", None)
        if not bool(getattr(lease, "held", False)):
            raise DriverError("native construction desktop lease is not held")
        original_guardian_generation = reservation.get("guardianGeneration")
        if (
            not isinstance(original_guardian_generation, int)
            or isinstance(original_guardian_generation, bool)
            or original_guardian_generation < 0
        ):
            raise DriverError("native construction reservation Guardian generation is malformed")
        # The immutable generation captured at owner creation is passed into
        # the binding snapshot above.  Separately read the live Guardian
        # generation under its real lock so a revoked owner cannot validate
        # merely by comparing the reservation back to itself.
        current_guardian_generation = self._native_construction_guardian_generation()
        if current_guardian_generation != original_guardian_generation:
            raise DriverError("native construction Guardian binding changed (generation)")
        identity = self.driver.resolve_identity()
        guardian = self.driver.guardian
        current = self._native_construction_binding(
            reservation_id=reservation["reservationId"],
            reservation_kind=reservation["reservationKind"],
            reservation_state=reservation["reservationState"],
            controller_generation=expected_generation,
            expected_boot=reservation["B0"],
            identity=identity,
            expires_monotonic=reservation["expiresMonotonic"],
            guardian_generation=reservation.get("guardianGeneration"),
        )
        for key in (
            "desktopLeaseHeld",
            "guardianGeneration",
            "guardianArmed",
            "ownerPid",
            "ownerProcessStart",
            "profileFingerprint",
            "identityFingerprint",
            "B0",
            "expectedConstructorBoot",
        ):
            if current[key] != reservation[key]:
                raise DriverError(f"native construction reservation binding changed: {key}")
        if self._native_clock() >= float(reservation["expiresMonotonic"]):
            raise DriverError("native construction reservation expired")
        if bool(getattr(guardian, "armed", False)):
            raise DriverError("native construction requires an unarmed Guardian")
        return identity

    # Kept as a compatibility seam for existing barrier fixtures.  Despite
    # the historical name this method must be called only after the caller
    # has released the construction lock.
    def _native_construction_validate_current_locked(
        self, reservation: dict[str, Any], *, expected_generation: int
    ) -> WindowIdentity:
        return self._native_construction_validate_snapshot(
            reservation, expected_generation=expected_generation
        )

    def _native_construction_revalidate_owner(
        self,
        reservation_snapshot: dict[str, Any],
        ticket_snapshot: dict[str, Any],
        *,
        expected_generation: int,
    ) -> tuple[WindowIdentity, dict[str, Any]]:
        """Revalidate a recovery owner without holding both lock domains."""

        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.PAUSED or status.generation != expected_generation:
                raise DriverError("native recovery completion requires the current PAUSED generation")
        with self._native_construction_lock:
            current = self._native_construction_reservation
            current_ticket = self._native_construction_ticket
            if (
                current is None
                or current_ticket is None
                or current.get("reservationId") != reservation_snapshot.get("reservationId")
                or current_ticket.get("reservationId") != ticket_snapshot.get("reservationId")
                or current_ticket.get("epoch") != ticket_snapshot.get("epoch")
                or current_ticket.get("guardianGeneration") != ticket_snapshot.get("guardianGeneration")
            ):
                raise DriverError("native recovery owner was revoked during polling")
            current_snapshot = copy.deepcopy(current)
            capability = self._native_construction_capability_locked()
        identity = self._native_construction_validate_current_locked(
            current_snapshot, expected_generation=expected_generation
        )
        with self._native_construction_lock:
            current = self._native_construction_reservation
            current_ticket = self._native_construction_ticket
            if (
                current is None
                or current_ticket is None
                or current.get("reservationId") != reservation_snapshot.get("reservationId")
                or current_ticket.get("reservationId") != ticket_snapshot.get("reservationId")
                or current_ticket.get("epoch") != ticket_snapshot.get("epoch")
                or current_ticket.get("guardianGeneration") != ticket_snapshot.get("guardianGeneration")
            ):
                raise DriverError("native recovery owner was revoked during validation")
        return identity, capability or {}

    def _native_construction_revalidate_run_owner(
        self, run_context: dict[str, Any], *, expected_generation: int
    ) -> dict[str, Any]:
        """Revalidate a deferred calibration owner at each finalize wait edge."""

        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.NATIVE_CALIBRATION:
                raise DriverError("deferred native finalize generation was revoked")
            if status.generation != expected_generation:
                raise DriverError("deferred native finalize generation was revoked")
        with self._native_construction_lock:
            current_context = self._native_construction_run_context
            reservation = self._native_construction_reservation
            if (
                current_context is None
                or current_context.get("closed")
                or current_context.get("runId") != run_context.get("runId")
                or current_context.get("epoch") != run_context.get("epoch")
                or reservation is None
                or reservation.get("reservationId") != run_context.get("reservationId")
            ):
                raise DriverError("deferred native finalize owner was revoked")
            snapshot = copy.deepcopy(reservation)
        self._native_construction_validate_snapshot(snapshot, expected_generation=expected_generation)
        with self._native_construction_lock:
            current_context = self._native_construction_run_context
            reservation = self._native_construction_reservation
            if (
                current_context is None
                or current_context.get("closed")
                or current_context.get("runId") != run_context.get("runId")
                or current_context.get("epoch") != run_context.get("epoch")
                or reservation is None
                or reservation.get("reservationId") != run_context.get("reservationId")
            ):
                raise DriverError("deferred native finalize owner changed during validation")
            return copy.deepcopy(reservation)

    def _native_construction_revalidate_finalize_owner(
        self,
        run_context: dict[str, Any],
        *,
        expected_generation: int,
        finalize_deadline: float,
        hook_deadline: float | None,
    ) -> dict[str, Any]:
        """Revalidate the deferred owner at every final-capture boundary."""

        # Cancellation takes controller first, then construction.  Keep this
        # order at every edge so a same-generation cancel cannot clear the
        # owner after this check and before the next capture/sleep boundary.
        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.NATIVE_CALIBRATION:
                raise DriverError("deferred native finalize generation was revoked")
            if status.generation != expected_generation:
                raise DriverError("deferred native finalize generation was revoked")
            now = self._native_clock()
            if now >= finalize_deadline:
                raise DriverError("deferred finalize frame deadline expired")
            if hook_deadline is not None and now >= hook_deadline:
                raise DriverError("deferred finalize hook deadline expired")
            with self._native_construction_lock:
                current_context = self._native_construction_run_context
                reservation = self._native_construction_reservation
                if (
                    current_context is None
                    or current_context.get("closed")
                    or current_context.get("runId") != run_context.get("runId")
                    or current_context.get("epoch") != run_context.get("epoch")
                    or reservation is None
                    or reservation.get("reservationId") != run_context.get("reservationId")
                    or reservation.get("reservationState") != "consumed"
                ):
                    raise DriverError("deferred native finalize owner was revoked")
                if self._native_construction_epoch != run_context.get("epoch"):
                    raise DriverError("deferred native finalize owner epoch changed")
                operation_deadline = self._native_construction_operation_deadline
                if operation_deadline is not None and now >= operation_deadline:
                    raise DriverError("deferred finalize admission deadline expired")
                if now >= float(reservation["expiresMonotonic"]):
                    raise DriverError("deferred native finalize reservation expired")
                return copy.deepcopy(reservation)

    def _native_construction_finalize_transaction(
        self,
        *,
        run_context: dict[str, Any],
        reservation_snapshot: dict[str, Any],
        expected_generation: int,
        finalize_deadline: float,
        hook_deadline: float | None,
        final_boot: str,
        final_sequence: int,
        final_crc32: int,
        final_identity_fingerprint: str,
        final_capture_identity: str,
        evidence: Any,
    ) -> Any:
        """Commit the final frame and deferred gate under one owner fence."""

        # The external frame capture is complete before entering this closed
        # transaction.  Keep controller-first ownership through every durable
        # controller mutation and the private terminal/owner clear.
        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.NATIVE_CALIBRATION:
                raise DriverError("deferred finalize generation/state was revoked")
            if status.generation != expected_generation:
                raise DriverError("deferred finalize generation/state was revoked")
            with self._native_construction_lock:
                context = self._native_construction_run_context
                reservation = self._native_construction_reservation
                now = self._native_clock()
                if (
                    context is None
                    or context.get("closed")
                    or context.get("runId") != run_context.get("runId")
                    or context.get("epoch") != run_context.get("epoch")
                    or reservation is None
                    or reservation.get("reservationId") != run_context.get("reservationId")
                    or reservation.get("reservationState") != "consumed"
                    or self._native_construction_epoch != run_context.get("epoch")
                ):
                    raise DriverError("deferred finalize owner was revoked before commit")
                if (
                    reservation.get("identityFingerprint") != reservation_snapshot.get("identityFingerprint")
                    or final_identity_fingerprint != final_capture_identity
                    or final_identity_fingerprint != reservation.get("identityFingerprint")
                    or final_capture_identity != reservation.get("identityFingerprint")
                ):
                    raise DriverError("deferred finalize capture identity binding changed")
                guardian = self.driver.guardian
                if getattr(guardian, "generation", None) != reservation.get("guardianGeneration") or bool(
                    getattr(guardian, "armed", False)
                ):
                    raise DriverError("deferred finalize Guardian binding changed")
                if now >= finalize_deadline:
                    raise DriverError("deferred finalize frame deadline expired")
                if hook_deadline is not None and now >= hook_deadline:
                    raise DriverError("deferred finalize hook deadline expired")
                operation_deadline = self._native_construction_operation_deadline
                if operation_deadline is None or now >= operation_deadline:
                    raise DriverError("deferred finalize admission deadline expired")
                if now >= float(reservation["expiresMonotonic"]):
                    raise DriverError("deferred finalize reservation expired")
                if final_boot != reservation.get("expectedConstructorBoot"):
                    raise DriverError("deferred finalize frame boot is stale")
                terminal_reservation = copy.deepcopy(reservation)
                rebind_status = self.controller.rebind_native_construction_frame(
                    boot=final_boot,
                    sequence=final_sequence,
                    crc32=final_crc32,
                    reason="native_construction_finalize",
                    expected_generation=expected_generation,
                    expected_state=ControllerState.NATIVE_CALIBRATION,
                )
                if (
                    rebind_status.generation != expected_generation
                    or rebind_status.state is not ControllerState.NATIVE_CALIBRATION
                ):
                    raise DriverError("deferred finalize frame rebind was not admitted")
                observe_status = self.controller.observe_frame(
                    boot=final_boot,
                    sequence=final_sequence,
                    crc32=final_crc32,
                    expected_generation=expected_generation,
                    expected_state=ControllerState.NATIVE_CALIBRATION,
                )
                if (
                    observe_status.generation != expected_generation
                    or observe_status.state is not ControllerState.NATIVE_CALIBRATION
                ):
                    raise DriverError("deferred finalize frame observation was not admitted")
                status = self.controller.native_calibrated(
                    evidence,
                    preconstruction_proof=self._native_bootstrap_proof,
                    expected_generation=expected_generation,
                    expected_state=ControllerState.NATIVE_CALIBRATION,
                )
                if (
                    status.generation != expected_generation
                    or status.state is not ControllerState.INPUT_CALIBRATION
                ):
                    raise DriverError("native calibration admission was not finalized")
                self._native_construction_publish_state_locked(
                    "finalized", controllerGeneration=status.generation
                )
                terminal = self._native_construction_record_terminal_locked(
                    terminal_reservation,
                    state="finalized",
                    handshake=copy.deepcopy(self._native_construction_handshake),
                )
                if terminal is None:
                    raise DriverError("deferred finalize terminal receipt could not be retained")
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_operation_deadline = None
                self._native_construction_finalize_event.clear()
                self._native_construction_close_context_locked(run_context)
                return status

    def _native_construction_prepare(self, *, expected_generation: int, kind: str) -> dict[str, Any]:
        if kind not in {"measure", "preflight"}:
            raise DriverError("unknown native construction reservation kind")
        self._native_construction_flush_expiry()
        with self._native_bootstrap_lock:
            if (
                self._native_bootstrap_observation is not None
                or self._native_bootstrap_ready.is_set()
                or self._native_baseline_ready.is_set()
            ):
                raise DriverError("native bootstrap evidence is already pending")
        required = ControllerState.NATIVE_CALIBRATION if kind == "measure" else ControllerState.PAUSED
        # Snapshot controller admission before the construction lock.  The
        # pause/revoke path takes the controller lock first and invokes the
        # native callback synchronously, so the reverse order is forbidden.
        with self.controller.lock:
            status = self.controller.status()
            if status.state is not required or status.generation != expected_generation:
                raise DriverError(f"native construction prepare requires {required.value}")
            if status.mode.value == "interaction":
                raise DriverError("native construction is unavailable in interaction mode")
            if not self.controller.native_construction_checkpoint_valid():
                raise DriverError("malformed native construction checkpoint requires recovery")
            if self.controller.native_construction_checkpoint() is not None:
                raise DriverError("native construction checkpoint already exists")
        guardian = getattr(self.driver, "guardian", None)
        lease = getattr(guardian, "lease", None) if guardian is not None else None
        if (
            guardian is None
            or not bool(getattr(lease, "held", False))
            or guardian.armed
            or getattr(guardian, "_calibration_ticket", None) is not None
        ):
            raise DriverError("native construction requires an idle unarmed owner")
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            if (
                self._native_construction_reservation is not None
                or self._native_construction_ticket is not None
                or self._native_construction_inflight is not None
            ):
                raise DriverError("a native construction reservation is already active")
            with self._calibration_state_lock:
                operation = self._calibration_operation.get("state")
                if operation in {"starting", "running"} or (
                    self._calibration_operation.get("generation") == expected_generation
                    and operation != "idle"
                ):
                    raise DriverError("native calibration is already running")
            self._native_construction_epoch += 1
            epoch = self._native_construction_epoch
            guardian_generation = self._native_construction_guardian_generation()
            self._native_construction_inflight = {
                "epoch": epoch,
                "generation": expected_generation,
                "guardianGeneration": guardian_generation,
                "kind": kind,
            }
            capability = self._native_construction_capability_locked()
        deadline = self._native_clock() + min(float(self.config.reload_deadline_seconds), 600.0)
        try:
            observation = self._native_construction_capture(
                deadline=deadline,
                expected_generation=expected_generation,
                expected_state=required,
                native_capability=capability,
            )
            identity = self.driver.resolve_identity()
            self._native_construction_observation_identity(observation, identity)
            boot, _sequence, _crc32, snapshot = self._native_construction_frame_facts(observation)
            if boot.startswith("native-construction:") or len(boot.encode("utf-8")) > 64:
                raise DriverError("native construction B0 boot identity is malformed")
            scanner = snapshot.get("scanner")
            fence = scanner.get("scheduleFence") if isinstance(scanner, dict) else None
            if not isinstance(fence, dict) or fence.get("state") != "inactive":
                raise DriverError("native construction requires an unarmed projected schedule fence")
            with self.controller.lock:
                status = self.controller.status()
                if status.generation != expected_generation or status.state is not required:
                    raise DriverError("native construction prepare generation/state changed")
            with self._native_construction_lock:
                if (
                    not self._native_construction_inflight
                    or self._native_construction_inflight.get("epoch") != epoch
                ):
                    raise DriverError("native construction prepare was revoked")
            reservation_id = secrets.token_hex(16)
            record = self._native_construction_binding(
                reservation_id=reservation_id,
                reservation_kind=kind,
                reservation_state="prepared",
                controller_generation=expected_generation,
                expected_boot=boot,
                identity=identity,
                expires_monotonic=deadline,
                guardian_generation=guardian_generation,
            )
            checkpoint = {
                "schema": 1,
                "B0": boot,
                "expectedConstructorBoot": f"native-construction:{boot}",
                "ownerGeneration": expected_generation,
                "kind": kind,
                "phase": "prepared",
            }
            checkpoint_status = self.controller.set_native_construction_checkpoint(
                checkpoint,
                expected_generation=expected_generation,
                expected_state=required,
            )
            if checkpoint_status.generation != expected_generation or checkpoint_status.state is not required:
                raise DriverError("native construction checkpoint admission was revoked")
            with self._native_construction_lock:
                if (
                    not self._native_construction_inflight
                    or self._native_construction_inflight.get("epoch") != epoch
                ):
                    # A revoke raced the durable write.  Leave B0 for
                    # recovery, but never hand the stale local owner back.
                    raise DriverError("native construction prepare was revoked")
                self._native_construction_inflight = None
                self._native_construction_reservation = record
                self._native_construction_handshake = {
                    "reservationKind": kind,
                    "controllerGeneration": expected_generation,
                    "operationBoundSeconds": self._NATIVE_CONSTRUCTION_OPERATION_SECONDS,
                }
                self._native_construction_reset_history_locked()
                self._native_construction_publish_state_locked("construction_prepared")
                return self._native_construction_reply()
        except Exception as exc:
            with self._native_construction_lock:
                if (
                    self._native_construction_inflight
                    and self._native_construction_inflight.get("epoch") == epoch
                ):
                    self._native_construction_inflight = None
            raise exc

    def native_construction_prepare(self, *, expected_generation: int) -> dict[str, Any]:
        return self._native_construction_prepare(expected_generation=expected_generation, kind="measure")

    def native_construction_control_preflight(self, *, expected_generation: int) -> dict[str, Any]:
        return self._native_construction_prepare(expected_generation=expected_generation, kind="preflight")

    def native_construction_bootstrap(
        self, *, expected_generation: int, reservation_id: str, expected_constructor_boot: str
    ) -> dict[str, Any]:
        reason: str | None = None
        expired = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            if expired:
                reason = "native construction reservation expired"
            elif reservation is None or reservation.get("reservationId") != reservation_id:
                raise DriverError("native construction reservation is unknown")
            elif reservation.get("reservationKind") != "measure":
                reason = "control preflight cannot bootstrap"
            elif reservation.get("reservationState") != "prepared":
                reason = "native bootstrap was replayed"
            elif self._native_construction_handshake.get("state") != "construction_prepared":
                reason = "native bootstrap predecessor is invalid"
            elif expected_constructor_boot != reservation.get("expectedConstructorBoot"):
                reason = "native construction boot echo is stale"
            if reason:
                pass
            else:
                epoch = self._native_construction_epoch
                snapshot = copy.deepcopy(reservation)
        if expired:
            self._native_construction_flush_expiry()
            raise DriverError(reason or "native construction reservation expired")
        if reason:
            self._native_construction_revoke_owner(reservation_id, reason)
            raise DriverError(reason)
        try:
            self._native_construction_validate_snapshot(snapshot, expected_generation=expected_generation)
        except Exception as exc:
            self._native_construction_revoke_owner(reservation_id, str(exc))
            raise
        revoke_reason: str | None = None
        expired = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            if expired:
                revoke_reason = "native construction reservation expired"
            elif reservation is None or reservation.get("reservationId") != reservation_id:
                raise DriverError("native construction reservation is unknown")
            elif (
                self._native_construction_epoch != epoch or reservation.get("reservationState") != "prepared"
            ):
                revoke_reason = "native construction bootstrap was revoked"
            else:
                reservation["reservationState"] = "consumed"
                self._native_construction_operation_deadline = self._native_clock() + (
                    self._NATIVE_CONSTRUCTION_OPERATION_SECONDS
                )
                reservation["expiresMonotonic"] = self._native_construction_operation_deadline
                self._native_construction_start_context_locked(
                    generation=expected_generation,
                    deferred=True,
                    reservation_id=reservation_id,
                )
                self._native_construction_finalize_event.clear()
                self._native_construction_handshake = {
                    "controllerGeneration": expected_generation,
                    "operationDeadline": self._native_construction_operation_deadline,
                    "operationBoundSeconds": self._NATIVE_CONSTRUCTION_OPERATION_SECONDS,
                }
                self._native_construction_publish_state_locked("running")
                return self._native_construction_reply()
        if expired:
            self._native_construction_flush_expiry()
        self._native_construction_revoke_owner(
            reservation_id, revoke_reason or "native bootstrap was revoked"
        )
        raise DriverError(revoke_reason or "native bootstrap was revoked")

    def native_construction_recheck(self, *, expected_generation: int, reservation_id: str) -> dict[str, Any]:
        failure: str | None = None
        result: dict[str, Any] | None = None
        expired = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            if expired:
                failure = "native construction reservation expired"
            elif reservation is None or reservation.get("reservationId") != reservation_id:
                result = self._native_construction_terminal_reply_locked(
                    reservation_id, expected_generation=expected_generation
                )
                if result is None:
                    failure = "native construction reservation is unknown"
            else:
                snapshot = copy.deepcopy(reservation)
        if failure:
            self._native_construction_flush_expiry()
            raise DriverError(failure)
        if result is not None:
            return result
        try:
            self._native_construction_validate_snapshot(snapshot, expected_generation=expected_generation)
        except Exception as exc:
            self._native_construction_revoke_owner(reservation_id, str(exc))
            raise
        with self._native_construction_lock:
            reservation = self._native_construction_reservation
            if reservation is None or reservation.get("reservationId") != reservation_id:
                result = self._native_construction_terminal_reply_locked(
                    reservation_id, expected_generation=expected_generation
                )
                if result is None:
                    raise DriverError("native construction reservation is unknown")
                return result
            result = self._native_construction_reply()
            result["handshake"] = {
                **result["handshake"],
                "state": self._native_construction_handshake.get("state", ""),
            }
            return result

    def native_construction_finalize_admit(
        self, *, expected_generation: int, reservation_id: str
    ) -> dict[str, Any]:
        snapshot: dict[str, Any]
        expired = False
        already_admitted = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            if expired:
                pass
            elif reservation is None or reservation.get("reservationId") != reservation_id:
                raise DriverError("native construction reservation is unknown")
            elif reservation.get("reservationState") != "consumed":
                raise DriverError("native construction finalize admission is out of order")
            elif self._native_construction_handshake.get("state") not in {
                "resume_ready",
                "finalize_required",
            }:
                raise DriverError("native construction finalize requires resume_ready")
            else:
                snapshot = copy.deepcopy(reservation)
                already_admitted = self._native_construction_handshake.get("state") == "finalize_required"
        if expired:
            self._native_construction_flush_expiry()
            raise DriverError("native construction reservation expired")
        try:
            self._native_construction_validate_snapshot(snapshot, expected_generation=expected_generation)
        except Exception as exc:
            self._native_construction_revoke_owner(reservation_id, str(exc))
            raise
        with self._native_construction_lock:
            reservation = self._native_construction_reservation
            if reservation is None or reservation.get("reservationId") != reservation_id:
                raise DriverError("native construction reservation was revoked")
            if already_admitted:
                return self._native_construction_reply()
            hook_deadline = float(
                self._native_construction_handshake.get("finalizeHookDeadline", self._native_clock())
            )
            if self._native_clock() >= hook_deadline:
                raise DriverError("native construction finalize hook deadline expired")
            deadline = min(
                float(self._native_construction_operation_deadline or float("inf")),
                hook_deadline,
                self._native_clock() + 10.0,
            )
            self._native_construction_publish_state_locked("finalize_required", finalizeDeadline=deadline)
            self._native_construction_finalize_event.set()
            return self._native_construction_reply()

    def native_construction_recovery_begin(
        self, *, expected_generation: int, reservation_id: str
    ) -> dict[str, Any]:
        """Capture and seal one generation-bound PAUSED recovery ticket."""

        self._native_construction_flush_expiry()
        with self.controller.lock:
            status = self.controller.status()
            if status.state is not ControllerState.PAUSED or status.generation != expected_generation:
                raise DriverError("native construction recovery requires the current PAUSED generation")
            if status.mode.value == "interaction":
                raise DriverError("native construction recovery is unavailable in interaction mode")
            if not self.controller.native_construction_checkpoint_valid():
                raise DriverError("malformed native construction checkpoint requires recovery")
            checkpoint = copy.deepcopy(self.controller.native_construction_checkpoint())
            if checkpoint is None:
                raise DriverError("native construction recovery checkpoint is missing")
        with self._calibration_state_lock:
            if self._calibration_operation.get("state") in {"starting", "running"}:
                raise DriverError("native construction recovery overlaps a live calibration worker")
        with self._native_bootstrap_lock:
            if self._native_bootstrap_observation is not None or self._native_bootstrap_ready.is_set():
                raise DriverError("native construction recovery has a pending legacy bootstrap")
        guardian = getattr(self.driver, "guardian", None)
        lease = getattr(guardian, "lease", None) if guardian is not None else None
        if (
            guardian is None
            or not bool(getattr(lease, "held", False))
            or guardian.armed
            or getattr(guardian, "_calibration_ticket", None) is not None
        ):
            raise DriverError("native construction recovery requires an unarmed owner")

        self._native_construction_flush_expiry()
        expired = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            if expired:
                handoff = None
            elif self._native_construction_inflight is not None:
                raise DriverError("native construction recovery admission is already in flight")
            elif reservation_id:
                if reservation is None or reservation.get("reservationId") != reservation_id:
                    raise DriverError("preflight reservation is unavailable for recovery handoff")
                if reservation.get("reservationKind") != "preflight":
                    raise DriverError("only a control preflight reservation can hand off to recovery")
                handoff = copy.deepcopy(reservation)
            else:
                if reservation is not None or self._native_construction_ticket is not None:
                    raise DriverError("recovery restart cannot overlap a live reservation")
                handoff = None
            if not expired:
                self._native_construction_epoch += 1
                epoch = self._native_construction_epoch
                guardian_generation = self._native_construction_guardian_generation()
                self._native_construction_inflight = {
                    "epoch": epoch,
                    "generation": expected_generation,
                    "guardianGeneration": guardian_generation,
                    "kind": "recovery",
                    "reservationId": reservation_id or None,
                }
                capability = self._native_construction_capability_locked()

        if expired:
            self._native_construction_flush_expiry()
            raise DriverError("native construction reservation expired")

        deadline = self._native_clock() + min(float(self.config.reload_deadline_seconds), 600.0)
        ticket_id: str | None = None
        try:
            if handoff is not None:
                self._native_construction_validate_snapshot(handoff, expected_generation=expected_generation)
            observation = self._native_construction_capture(
                deadline=deadline,
                expected_generation=expected_generation,
                expected_state=ControllerState.PAUSED,
                native_capability=capability,
            )
            identity = self.driver.resolve_identity()
            self._native_construction_observation_identity(observation, identity)
            surface = observation.get("surface", "present")
            if surface not in {"present", "absent"}:
                raise DriverError("native recovery begin surface is malformed")
            begin_boot = str(observation.get("boot", "")) if surface == "present" else ""
            recovery_kind = "restore"
            if surface == "present":
                decoded_boot, _sequence, _crc32, decoded_snapshot = (
                    self._native_construction_decoded_frame_facts(observation)
                )
                if decoded_boot == checkpoint["expectedConstructorBoot"]:
                    # Restore can begin while the addon is still in its
                    # held/100 (or held/update) construction phase.  The
                    # constructor boot identity is the strict recovery
                    # discriminator; normal/0 is reserved for B0/ordinary
                    # reload admission.
                    boot = decoded_boot
                    snapshot = decoded_snapshot
                    recovery_kind = "restore"
                else:
                    boot, _sequence, _crc32, snapshot = self._native_construction_frame_facts(observation)
                scanner = snapshot.get("scanner")
                fence = scanner.get("scheduleFence") if isinstance(scanner, dict) else None
                fence_state = fence.get("state") if isinstance(fence, dict) else None
                if boot == checkpoint["expectedConstructorBoot"]:
                    recovery_kind = "restore"
                elif boot == checkpoint["B0"]:
                    if fence_state == "armed":
                        recovery_kind = "restore"
                    elif fence_state == "inactive":
                        recovery_kind = "ordinary_reload"
                    else:
                        raise DriverError("ordinary B0 recovery requires an unarmed schedule fence")
                elif boot.startswith("native-construction:"):
                    raise DriverError("native recovery begin observed an unrelated construction boot")
                elif fence_state == "inactive":
                    recovery_kind = "ordinary_reload"
                else:
                    raise DriverError("ordinary recovery requires an unarmed schedule fence")

            ticket_id = secrets.token_hex(16)
            with self._native_construction_lock:
                inflight = self._native_construction_inflight
                if (
                    not isinstance(inflight, dict)
                    or inflight.get("epoch") != epoch
                    or inflight.get("generation") != expected_generation
                ):
                    raise DriverError("native recovery begin was revoked")
                current_reservation = handoff
                if current_reservation is None:
                    current_reservation = self._native_construction_binding(
                        reservation_id=ticket_id,
                        reservation_kind="recovery",
                        reservation_state="recovery",
                        controller_generation=expected_generation,
                        expected_boot=checkpoint["B0"],
                        identity=identity,
                        expires_monotonic=deadline,
                        guardian_generation=guardian_generation,
                    )
                else:
                    current_reservation = copy.deepcopy(current_reservation)
                    current_reservation.update(
                        reservationId=ticket_id,
                        reservationKind="recovery",
                        reservationState="recovery",
                        controllerGeneration=expected_generation,
                        expiresMonotonic=deadline,
                    )
                current_reservation["reservationId"] = ticket_id
                self._native_construction_reservation = current_reservation
                self._native_construction_ticket = {
                    "reservationId": ticket_id,
                    "beginBoot": begin_boot,
                    "B0": checkpoint["B0"],
                    "beginCaptureIdentity": observation.get(
                        "captureIdentity", observation.get("identityFingerprint", "")
                    ),
                    "beginCaptureMonotonic": observation.get("captureMonotonic", self._native_clock()),
                    "controllerGeneration": expected_generation,
                    "guardianGeneration": current_reservation.get("guardianGeneration"),
                    "recoveryKind": recovery_kind,
                    "epoch": epoch,
                }
                self._native_construction_inflight = None
                self._native_construction_handshake = {
                    "controllerGeneration": expected_generation,
                    "recoveryKind": recovery_kind,
                }
                self._native_construction_reset_history_locked()
                self._native_construction_publish_state_locked("recovery_required")
            # The durable transition is outside the construction lock. A pause
            # callback may safely revoke the provisional owner while this runs.
            recovery_status = self.controller.mark_native_construction_recovery_required()
            with self.controller.lock:
                current = self.controller.status()
                if (
                    recovery_status.generation != expected_generation
                    or recovery_status.state is not ControllerState.PAUSED
                    or current.generation != expected_generation
                    or current.state is not ControllerState.PAUSED
                ):
                    raise DriverError("native recovery generation changed before ticket publication")
            with self._native_construction_lock:
                if (
                    self._native_construction_ticket is None
                    or self._native_construction_ticket.get("reservationId") != ticket_id
                ):
                    raise DriverError("native recovery ticket was revoked")
                return self._native_construction_reply()
        except Exception as exc:
            with self._native_construction_lock:
                inflight = self._native_construction_inflight
                if isinstance(inflight, dict) and inflight.get("epoch") == epoch:
                    self._native_construction_inflight = None
                reservation = self._native_construction_reservation
                if (
                    ticket_id is not None
                    and reservation is not None
                    and reservation.get("reservationId") == ticket_id
                ):
                    self._native_construction_publish_state_locked("error", failure=str(exc))
                    self._native_construction_record_terminal_locked(
                        copy.deepcopy(reservation), state="error", failure=str(exc)
                    )
                    self._native_construction_reservation = None
                    self._native_construction_ticket = None
            raise exc

    def native_construction_recovery_complete(
        self, *, expected_generation: int, reservation_id: str
    ) -> dict[str, Any]:
        expired = False
        with self._native_construction_lock:
            self._native_construction_expire_locked()
            expired = self._native_construction_expired
            reservation = self._native_construction_reservation
            ticket = self._native_construction_ticket
            if expired:
                pass
            elif (
                reservation is None
                or ticket is None
                or reservation.get("reservationId") != reservation_id
                or ticket.get("reservationId") != reservation_id
            ):
                raise DriverError("native recovery ticket is unknown")
            elif expected_generation != ticket.get("controllerGeneration"):
                raise DriverError("native recovery generation is stale")
            else:
                reservation_snapshot = copy.deepcopy(reservation)
                ticket_snapshot = copy.deepcopy(ticket)
                epoch = ticket_snapshot.get("epoch")
        if expired:
            self._native_construction_flush_expiry()
            raise DriverError("native construction reservation expired")
        completion_deadline = min(
            float(reservation_snapshot["expiresMonotonic"]), self._native_clock() + 45.0
        )
        observation: dict[str, Any] | None = None
        identity: WindowIdentity | None = None
        boot = ""
        sequence = crc32 = 0
        last_error: DriverError | None = None
        while self._native_clock() < completion_deadline:
            identity, native_capability = self._native_construction_revalidate_owner(
                reservation_snapshot,
                ticket_snapshot,
                expected_generation=expected_generation,
            )
            try:
                candidate = self._native_construction_capture(
                    deadline=completion_deadline,
                    expected_generation=expected_generation,
                    expected_state=ControllerState.PAUSED,
                    native_capability=native_capability,
                )
            except DriverError as exc:
                # Loading/absent frames and decoder failures are not recovery
                # evidence. Revalidate the owner, then keep polling within the
                # existing reload/operation deadline.
                self._native_construction_revalidate_owner(
                    reservation_snapshot,
                    ticket_snapshot,
                    expected_generation=expected_generation,
                )
                last_error = exc
                candidate = None
            if candidate is not None:
                identity_after, _ = self._native_construction_revalidate_owner(
                    reservation_snapshot,
                    ticket_snapshot,
                    expected_generation=expected_generation,
                )
                self._native_construction_observation_identity(candidate, identity_after)
                if candidate.get("surface") != "present":
                    last_error = DriverError("native recovery completion is waiting for an ordinary frame")
                elif (
                    isinstance(candidate.get("boot"), str)
                    and candidate["boot"].startswith("native-construction:")
                    and candidate["boot"] != reservation_snapshot.get("expectedConstructorBoot")
                ):
                    # A construction-prefixed boot belongs to another
                    # lifecycle and is an owner/boot violation, not a
                    # transient reload frame. Refuse it immediately.
                    raise DriverError("native recovery completion requires an ordinary boot")
                else:
                    try:
                        candidate_boot, candidate_sequence, candidate_crc32, candidate_snapshot = (
                            self._native_construction_frame_facts(candidate)
                        )
                        if candidate_boot in {
                            ticket_snapshot.get("beginBoot"),
                            ticket_snapshot.get("B0"),
                            reservation_snapshot.get("expectedConstructorBoot"),
                        }:
                            raise DriverError(
                                "native recovery completion did not observe a new ordinary boot"
                            )
                        capture_monotonic = candidate.get("captureMonotonic", -1)
                        begin_monotonic = ticket_snapshot.get("beginCaptureMonotonic", -1)
                        if not isinstance(capture_monotonic, (int, float)) or not isinstance(
                            begin_monotonic, (int, float)
                        ):
                            raise DriverError("native recovery completion capture boundary is malformed")
                        if float(capture_monotonic) <= float(begin_monotonic):
                            raise DriverError(
                                "native recovery completion capture is not newer than recovery begin"
                            )
                        scanner = candidate_snapshot.get("scanner")
                        fence = scanner.get("scheduleFence") if isinstance(scanner, dict) else None
                        if not isinstance(fence, dict) or fence.get("state") != "inactive":
                            raise DriverError("native recovery completion requires an unarmed schedule fence")
                        observation = candidate
                        identity = identity_after
                        boot, sequence, crc32, _candidate_snapshot = (
                            candidate_boot,
                            candidate_sequence,
                            candidate_crc32,
                            candidate_snapshot,
                        )
                        break
                    except DriverError as exc:
                        last_error = exc
            remaining = completion_deadline - self._native_clock()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
            self._native_construction_revalidate_owner(
                reservation_snapshot,
                ticket_snapshot,
                expected_generation=expected_generation,
            )
        if observation is None or identity is None:
            raise last_error or DriverError("native recovery completion did not observe an ordinary boot")

        # Controller.pause invokes the revocation callback while holding the
        # controller mutex.  Take controller first, then construction, and
        # keep both through owner revalidation and the closed journal commit;
        # recovery-cancel therefore cannot clear the owner between the check
        # and checkpoint removal.
        with self.controller.lock:
            current_status = self.controller.status()
            if (
                current_status.state is not ControllerState.PAUSED
                or current_status.generation != expected_generation
            ):
                raise DriverError("native recovery generation changed before commit")
            with self._native_construction_lock:
                current = self._native_construction_reservation
                current_ticket = self._native_construction_ticket
                if (
                    current is None
                    or current_ticket is None
                    or current.get("reservationId") != reservation_id
                    or current_ticket.get("reservationId") != reservation_id
                    or current_ticket.get("epoch") != epoch
                ):
                    raise DriverError("native recovery owner was revoked before commit")
                committed = self.controller.commit_native_construction_recovery(
                    boot=boot,
                    sequence=sequence,
                    crc32=crc32,
                    expected_generation=expected_generation,
                    expected_state=ControllerState.PAUSED,
                )
                observed = self.controller.validate_native_construction_recovery_commit(
                    boot=boot,
                    sequence=sequence,
                    crc32=crc32,
                    expected_generation=expected_generation,
                    expected_state=ControllerState.PAUSED,
                )
                if (
                    committed.generation != expected_generation
                    or committed.state is not ControllerState.PAUSED
                ):
                    raise DriverError("native recovery commit was not admitted")
                if observed.generation != expected_generation or observed.state is not ControllerState.PAUSED:
                    raise DriverError("native recovery observation was not admitted")
                self._native_construction_publish_state_locked("recovered")
                terminal_reservation = copy.deepcopy(current)
                terminal_handshake = {
                    "state": "recovered",
                    "recoveryKind": current_ticket.get("recoveryKind", "restore"),
                }
                captured_terminal = self._native_construction_record_terminal_locked(
                    terminal_reservation,
                    state="recovered",
                    handshake=terminal_handshake,
                )
                if captured_terminal is None:
                    raise DriverError("native recovery terminal receipt could not be retained")
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_operation_deadline = None
                self._native_construction_handshake = {"state": "recovered"}
        try:
            self.driver.guardian.cancel_calibration_operation()
        except (AttributeError, GuardianError):
            pass
        return {
            "status": self.status_dict(),
            "reservation": copy.deepcopy(captured_terminal["reservation"]),
            "handshake": self._native_construction_public_handshake(
                copy.deepcopy(captured_terminal["handshake"])
            ),
            "nativeClock": self._native_clock(),
            "stateHistory": copy.deepcopy(captured_terminal["stateHistory"]),
        }

    def native_construction_recovery_cancel(
        self, *, expected_generation: int, reservation_id: str
    ) -> dict[str, Any]:
        terminal_reservation: dict[str, Any]
        self._native_construction_flush_expiry()
        with self.controller.lock:
            if self.controller.status().generation != expected_generation:
                raise DriverError("native recovery generation is stale")
            with self._native_construction_lock:
                reservation = self._native_construction_reservation
                inflight = self._native_construction_inflight
                if reservation is not None and reservation.get("reservationId") == reservation_id:
                    terminal_reservation = copy.deepcopy(reservation)
                elif (
                    isinstance(inflight, dict)
                    and inflight.get("generation") == expected_generation
                    and inflight.get("reservationId") == (reservation_id or None)
                ):
                    # Recovery capture is an admission interval of its own.
                    # Cancel it by revoking the exact epoch before capture can
                    # hand back a ticket; no provisional reservation may be
                    # synthesized after this point.
                    terminal_reservation = {
                        "reservationId": reservation_id,
                        "reservationKind": "recovery",
                        "reservationState": "inflight",
                        "controllerGeneration": expected_generation,
                        "epoch": inflight.get("epoch"),
                    }
                else:
                    raise DriverError("native construction reservation is unknown")
                self._native_construction_epoch += 1
                self._native_construction_publish_state_locked("cancelled")
                terminal = self._native_construction_record_terminal_locked(
                    terminal_reservation, state="cancelled"
                )
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_inflight = None
                self._native_construction_operation_deadline = None
                captured_terminal = terminal or {
                    "reservation": terminal_reservation,
                    "handshake": {"state": "cancelled"},
                    "stateHistory": copy.deepcopy(self._native_construction_state_history),
                }
                context = self._native_construction_run_context
                if context is not None and not context.get("closed"):
                    closed = copy.deepcopy(context)
                    closed["closed"] = True
                    closed["stateHistory"] = copy.deepcopy(self._native_construction_state_history)
                    self._native_construction_run_context = closed
        try:
            self.driver.guardian.cancel_calibration_operation()
        except (AttributeError, GuardianError):
            pass
        try:
            self.controller.mark_native_construction_recovery_required()
        except RuntimeError:
            pass
        return {
            "status": self.status_dict(),
            "reservation": copy.deepcopy(captured_terminal["reservation"]),
            "handshake": self._native_construction_public_handshake(
                copy.deepcopy(captured_terminal["handshake"])
            ),
            "nativeClock": self._native_clock(),
            "stateHistory": copy.deepcopy(captured_terminal["stateHistory"]),
        }

    def _native_bootstrap_capture(
        self,
        *,
        expected_generation: int | None = None,
        construction_capability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture the external absence boundary for the native calibration.

        This command only records the real, unconstructed client surface.  The
        runner samples the process HWM and publishes the externally visible
        ready boundary later, from ``_native_preconstruction_provider``.  That
        ordering keeps a caller from loading the addon before the baseline is
        actually bound to the absence proof.
        """

        with self.controller.lock:
            status = self.controller.status()
            if status.state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native bootstrap requires NATIVE_CALIBRATION")
            if expected_generation is not None and status.generation != expected_generation:
                raise DriverError("native bootstrap generation was revoked")
        with self._native_construction_lock:
            context = self._native_construction_run_context
            if construction_capability is None:
                if (
                    self._native_construction_reservation is not None
                    or self._native_construction_inflight is not None
                    or (context is not None and context.get("deferred") and not context.get("closed"))
                ):
                    raise DriverError("native bootstrap is blocked by another construction admission")
            elif (
                context is None
                or context.get("closed")
                or not context.get("deferred")
                or not self._native_construction_capability_matches(construction_capability)
            ):
                raise DriverError("native bootstrap construction capability was revoked")
        if not isinstance(self.driver.backend, X11Backend):
            raise DriverError("native bootstrap requires the configured X11 backend")
        with self._native_bootstrap_lock:
            if self._native_bootstrap_observation is not None:
                raise DriverError("native bootstrap is already pending consumption")
            identity = self.driver.resolve_identity()
            decoder = self.driver.new_native_decoder()
            if self._native_region_enabled():
                path = self._capture_native_region_for_identity(identity)
            else:
                path = self.driver.capture_client_crop_for_identity(identity)
            # Bind the absence proof to the instant the owned capture was
            # returned.  Retaining/copying the artifact and waiting for the
            # runner must never restamp the evidence as a newer observation.
            capture_observed = self._native_clock()
            retained: Path | None = None
            try:
                try:
                    sample = self.driver.decode_native_capture(path, identity=identity, decoder=decoder)
                except ProtocolError:
                    # Before Agent construction the calibrated crop is ordinary
                    # client pixels and cannot decode as CRS1.  Keep the actual
                    # absence crop until the runner binds the HWM baseline.
                    if not self._strict_native_absence_capture(path):
                        raise DriverError(
                            "preconstruction capture contains a malformed or stale status surface"
                        ) from None
                    sample = None
                if sample is not None:
                    raise DriverError("native surface is already constructed before bootstrap")
                evidence_dir = self.config.journal.expanduser().parent / "native-evidence"
                evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(evidence_dir, 0o700)
                retained = evidence_dir / f"preconstruction-{time.monotonic_ns()}.png"
                shutil.copyfile(path, retained)
                os.chmod(retained, 0o600)
                capture_digest = hashlib.sha256(retained.read_bytes()).hexdigest()
                self._native_owned_capture_paths.add(retained)
            except Exception:
                if retained is not None:
                    retained.unlink(missing_ok=True)
                raise
            finally:
                self._remove_native_capture(path)
            self._native_bootstrap_observation = {
                "identity": identity.fingerprint,
                "processStart": identity.process_start,
                "processExecutable": identity.executable,
                "display": identity.display,
                "windowXid": identity.xid,
                "childXid": identity.child_xid,
                "geometry": tuple(identity.child_geometry),
                "xauthority": self.driver.xauthority,
                "winePrefix": self.config.profile.wine_prefix,
                "captureDigest": capture_digest,
                "capturePath": str(retained),
                "surface": "absent",
                "capture": capture_observed,
                "provenance": "owned-live",
            }
            self._native_bootstrap_proof = None
            self._native_baseline_ready.clear()
            self._native_baseline_observation = None
        self._native_bootstrap_ready.set()
        return self.status_dict()

    def native_bootstrap(self, *, expected_generation: int | None = None) -> dict[str, Any]:
        """Capture the legacy bootstrap boundary without a construction owner."""

        return self._native_bootstrap_capture(expected_generation=expected_generation)

    def notify_native_baseline_ready(self, identity: WindowIdentity, baseline_hwm: int) -> None:
        """Publish the ready boundary after the runner sampled the HWM."""

        if not isinstance(baseline_hwm, int) or isinstance(baseline_hwm, bool) or baseline_hwm < 0:
            raise DriverError("native baseline HWM is not a valid nonnegative integer")
        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native calibration generation was revoked")
        with self._native_bootstrap_lock:
            observation = self._native_bootstrap_observation
            if not observation or observation.get("identity") != identity.fingerprint:
                raise DriverError("native baseline-ready identity is stale")
            if observation.get("baselineHwm") is not None:
                raise DriverError("native baseline-ready boundary was already published")
            with self._native_construction_lock:
                callback_context = self._native_construction_callback_context_locked()
                deferred = bool(callback_context.get("deferred"))
                if (
                    deferred
                    and self._native_construction_handshake.get("state") != "construction_reset_archived"
                ):
                    raise DriverError("deferred baseline-bound callback arrived out of order")
            observation["baselineHwm"] = baseline_hwm
            self._native_baseline_observation = dict(observation)
            with self._native_construction_lock:
                if deferred:
                    if self._native_construction_handshake.get("state") != "construction_reset_archived":
                        raise DriverError("deferred baseline-bound callback arrived out of order")
                    self._native_construction_publish_state_locked(
                        "baseline_bound",
                        failure="",
                        restored=None,
                        baselineHwm=baseline_hwm,
                        absenceCaptureIdentity=observation.get("identity"),
                        absenceCaptureSha256=observation.get("captureDigest"),
                        absenceCapturePath=observation.get("capturePath"),
                        absenceCaptureMonotonic=observation.get("capture"),
                        absenceCaptureDeleted=False,
                    )
                else:
                    self._native_baseline_ready.set()
                    self._native_handshake.update(
                        {"state": "bootstrap_ready", "failure": "", "restored": None}
                    )

    def notify_native_memory_ready(self, identity: WindowIdentity, pending: Any) -> None:
        """Bind the reset5 update window to the same live client identity."""

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native memory-ready generation was revoked")
        memory_meter = getattr(self.driver, "native_memory_meter", None)
        memory_executable = getattr(memory_meter, "process_executable", None)
        if not isinstance(memory_executable, str) or not memory_executable:
            raise DriverError("native reset5 memory executable is not available")
        if (
            getattr(pending, "pid", None) != identity.pid
            or getattr(pending, "process_start", None) != identity.process_start
            or getattr(pending, "process_executable", None) != memory_executable
            or getattr(pending, "identity_fingerprint", None) != identity.fingerprint
        ):
            raise DriverError("native memory-ready token is not bound to the client")
        if getattr(pending, "phase", None) != "updates" or getattr(pending, "ordinal", None) != 0:
            raise DriverError("native memory-ready token is not the held zero update boundary")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            if callback_context.get("deferred"):
                if self._native_construction_handshake.get("state") != "updates_reset_archived":
                    raise DriverError("deferred measure-ready callback arrived out of order")
                self._native_construction_publish_state_locked(
                    "measure_ready", measureReady=True, failure="", restored=None
                )
            else:
                self._native_handshake.update(
                    {"state": "measure_ready", "measureReady": True, "failure": "", "restored": None}
                )

    def notify_native_memory_reset_ready(
        self, identity: WindowIdentity, record: Any
    ) -> NativeMemoryArchiveReceipt:
        """Bind the construction reset boundary before addon construction."""

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native construction reset generation was revoked")
        memory_meter = getattr(self.driver, "native_memory_meter", None)
        memory_executable = getattr(memory_meter, "process_executable", None)
        if not isinstance(memory_executable, str) or not memory_executable:
            raise DriverError("native reset5 memory executable is not available")
        if (
            getattr(record, "pid", None) != identity.pid
            or getattr(record, "process_start", None) != identity.process_start
            or getattr(record, "process_executable", None) != memory_executable
            or getattr(record, "identity_fingerprint", None) != identity.fingerprint
            or getattr(record, "phase", None) not in {"construction", "updates"}
            or getattr(record, "ordinal", None) != 0
        ):
            raise DriverError("native memory reset token is not bound to the client")
        # This callback is the reset5 meter's before-reset boundary.  The
        # historical accounting is journaled and fsynced before returning the
        # receipt; the meter will refuse to write clear_refs without it.
        to_dict = getattr(record, "to_dict", None)
        verify = getattr(record, "verify", None)
        if not callable(to_dict) or not callable(verify):
            raise DriverError("native reset callback received no complete reset record")
        verify()
        payload = to_dict()
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            if callback_context.get("deferred"):
                phase = payload.get("phase")
                expected = (
                    "running" if phase == "construction" else "hold_required" if phase == "updates" else ""
                )
                if not expected or self._native_construction_handshake.get("state") != expected:
                    raise DriverError("deferred memory-reset callback arrived out of order")
        archive_id = hashlib.sha256(
            json.dumps(
                {"identity": identity.fingerprint, "record": payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.controller.journal.append_archive(
            "native_memory",
            {
                "archiveId": archive_id,
                "record": payload,
                "identity": identity.fingerprint,
            },
        )
        receipt = NativeMemoryArchiveReceipt.for_record(record, archive_id=archive_id)
        with self._native_bootstrap_lock:
            receipts = self._native_handshake.setdefault("archiveReceipts", [])
            receipts.append(
                {
                    "archiveId": receipt.archive_id,
                    "recordSha256": receipt.record_sha256,
                    "phase": payload.get("phase", ""),
                    "ordinal": payload.get("ordinal", 0),
                }
            )
            self._native_handshake["archiveReceipts"] = receipts[-4:]
            with self._native_construction_lock:
                callback_context = self._native_construction_callback_context_locked()
                if callback_context.get("deferred"):
                    phase = payload.get("phase")
                    self._native_construction_publish_state_locked(f"{phase}_reset_archived")
                else:
                    self._native_handshake["state"] = f"{payload.get('phase', 'native')}_reset_archived"
        return receipt

    def notify_native_hold_required(self, identity: WindowIdentity) -> None:
        """Confirm the native runner crossed its held zero boundary."""

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native hold boundary generation was revoked")
        if identity.fingerprint != self.driver.resolve_identity().fingerprint:
            raise DriverError("native hold boundary identity changed")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            if callback_context.get("deferred"):
                if self._native_construction_handshake.get("state") != "construction_released":
                    raise DriverError("deferred hold-required callback arrived out of order")
                self._native_construction_publish_state_locked(
                    "hold_required", holdRequired=True, failure="", restored=None
                )
            else:
                self._native_handshake.update(
                    {"state": "hold_required", "holdRequired": True, "failure": "", "restored": None}
                )

    def notify_native_resume_required(self, identity: WindowIdentity, boundary: Any) -> None:
        """Publish the validated held/100 boundary before requesting resume."""

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native resume-required generation was revoked")
        verify = getattr(boundary, "verify", None)
        if not callable(verify):
            raise DriverError("native resume-required boundary is not sealed")
        try:
            verify()
        except Exception as exc:
            raise DriverError("native resume-required boundary is invalid") from exc
        if (
            getattr(boundary, "identity_fingerprint", None) != identity.fingerprint
            or getattr(boundary, "process_start", None) != identity.process_start
            or getattr(boundary, "process_executable", None) != identity.executable
            or getattr(boundary, "phase", None) != "held"
            or getattr(boundary, "ordinal", None) != 100
            or not getattr(boundary, "boot", "")
            or getattr(boundary, "update_count", -1) < 0
        ):
            raise DriverError("native resume-required boundary identity or phase is stale")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            target = (
                self._native_construction_handshake
                if callback_context.get("deferred")
                else self._native_handshake
            )
            if callback_context.get("deferred") and target.get("state") != "measure_ready":
                raise DriverError("deferred resume-required callback arrived out of order")
            resume_fields = {
                "resumeRequired": True,
                "resumeReady": False,
                "resumeBoundary": {
                    "identity": identity.fingerprint,
                    "boot": boundary.boot,
                    "sequence": boundary.sequence,
                    "updateCount": boundary.update_count,
                    "captureMonotonic": boundary.capture_monotonic,
                },
            }
            if callback_context.get("deferred"):
                self._native_construction_publish_state_locked("resume_required", **resume_fields)
            else:
                target.update({"state": "resume_required", **resume_fields})

    def notify_native_resume_ready(
        self, identity: WindowIdentity, evidence: Any, resumed: Any | None = None
    ) -> None:
        """Accept the completed two-window native proof at the live boundary."""

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native resume-ready generation was revoked")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            deferred_target = (
                self._native_construction_handshake
                if callback_context.get("deferred")
                else self._native_handshake
            )
            if not deferred_target.get("resumeRequired"):
                raise DriverError("native resume-ready arrived before resume-required")
        if getattr(evidence, "native_identity_fingerprint", None) != identity.fingerprint:
            raise DriverError("native resume-ready evidence identity is stale")
        if getattr(evidence, "process_pid", None) != identity.pid:
            raise DriverError("native resume-ready evidence PID changed")
        if resumed is None or getattr(resumed, "native_phase", None) != "normal":
            raise DriverError("native resume-ready observation is missing normal phase")
        if getattr(resumed, "native_ordinal", None) != 0:
            raise DriverError("native resume-ready observation is not ordinal zero")
        resumed_metrics = getattr(resumed, "metrics", {})
        boundary = deferred_target.get("resumeBoundary", {})
        resumed_sequence = getattr(resumed, "sequence", None)
        resumed_update_count = (
            resumed_metrics.get("updateCount") if isinstance(resumed_metrics, dict) else None
        )
        boundary_sequence = boundary.get("sequence") if isinstance(boundary, dict) else None
        boundary_update_count = boundary.get("updateCount") if isinstance(boundary, dict) else None
        if (
            isinstance(resumed_sequence, bool)
            or not isinstance(resumed_sequence, int)
            or resumed_sequence < 0
            or isinstance(resumed_update_count, bool)
            or not isinstance(resumed_update_count, int)
            or resumed_update_count < 0
            or isinstance(boundary_sequence, bool)
            or not isinstance(boundary_sequence, int)
            or boundary_sequence < 0
            or isinstance(boundary_update_count, bool)
            or not isinstance(boundary_update_count, int)
            or boundary_update_count < 0
        ):
            raise DriverError("native resume-ready observation facts are malformed")
        if (
            not isinstance(resumed_metrics, dict)
            or resumed_metrics.get("nativePhase") != "normal"
            or resumed_metrics.get("nativeOrdinal") != 0
            or resumed_update_count <= boundary_update_count
            or getattr(resumed, "identity_fingerprint", None) != identity.fingerprint
            or getattr(resumed, "boot", None) != boundary.get("boot")
            or resumed_sequence <= boundary_sequence
        ):
            raise DriverError("native resume-ready observation is not a fresh same-boot proof")
        evidence_resume_count = getattr(evidence, "resume_update_count", None)
        if evidence_resume_count != resumed_update_count:
            raise DriverError("native resume-ready evidence count does not match the observation")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            target = (
                self._native_construction_handshake
                if callback_context.get("deferred")
                else self._native_handshake
            )
            if callback_context.get("deferred") and target.get("state") != "resume_required":
                raise DriverError("deferred resume-ready callback arrived out of order")
            resume_updates: dict[str, Any] = {
                "resumeReady": True,
                "failure": "",
                "restored": True,
            }
            if callback_context.get("deferred"):
                resume_capture = getattr(resumed, "capture_monotonic", None)
                resume_sequence = resumed_sequence
                if (
                    isinstance(resume_capture, bool)
                    or not isinstance(resume_capture, (int, float))
                    or not math.isfinite(float(resume_capture))
                    or isinstance(resume_sequence, bool)
                    or not isinstance(resume_sequence, int)
                    or resume_sequence < 0
                ):
                    raise DriverError("native resume-ready observation facts are malformed")
                boundary_capture = boundary.get("captureMonotonic")
                if (
                    isinstance(boundary_capture, bool)
                    or not isinstance(boundary_capture, (int, float))
                    or not math.isfinite(float(boundary_capture))
                    or isinstance(boundary_sequence, bool)
                    or not isinstance(boundary_sequence, int)
                    or resume_sequence <= boundary_sequence
                    or float(resume_capture) <= float(boundary_capture)
                ):
                    raise DriverError("native resume-ready observation is not newer than its boundary")
                resume_updates.update(
                    {
                        "resumeSequence": resume_sequence,
                        "resumeCaptureMonotonic": float(resume_capture),
                        "finalizeHookDeadline": self._native_clock() + 30.0,
                    }
                )
            if callback_context.get("deferred"):
                self._native_construction_publish_state_locked("resume_ready", **resume_updates)
            else:
                target.update({"state": "resume_ready", **resume_updates})

    def notify_native_measurement_failed(
        self, identity: WindowIdentity, reason: str
    ) -> NativeRestorationReceipt:
        message = str(reason) or "native measurement failed"
        diagnostic = getattr(self, "_native_observation_diagnostic", None)
        if isinstance(diagnostic, _NativeObservationDiagnostic):
            diagnostic.fail(message)
        if identity.fingerprint != self.driver.resolve_identity().fingerprint:
            raise DriverError("native failure identity changed")
        self.controller.journal.append_archive(
            "interventions",
            {
                "kind": "native_restoration_required",
                "identity": identity.fingerprint,
                "reason": message,
                "durable": True,
            },
        )
        receipt = NativeRestorationReceipt("restoration_required", False, message, durable=True)
        receipt.verify()
        deferred = False
        run_context: dict[str, Any] | None = None
        with self._native_construction_lock:
            run_context = self._native_construction_callback_context_locked()
            deferred = bool(run_context.get("deferred"))
            target = self._native_construction_handshake if deferred else self._native_handshake
            if deferred:
                self._native_construction_publish_state_locked(
                    "restoration_required",
                    failure=message,
                    restored=False,
                    holdRequired=False,
                    measureReady=False,
                    resumeRequired=False,
                    resumeReady=False,
                )
                reservation = copy.deepcopy(self._native_construction_reservation)
                self._native_construction_record_terminal_locked(
                    reservation,
                    state="restoration_required",
                    failure=message,
                )
                self._native_construction_reservation = None
                self._native_construction_ticket = None
                self._native_construction_operation_deadline = None
                self._native_construction_finalize_event.clear()
                self._native_construction_close_context_locked(run_context)
            else:
                target.update(
                    {
                        "state": "restoration_required",
                        "failure": message,
                        "restored": False,
                        "holdRequired": False,
                        "measureReady": False,
                        "resumeRequired": False,
                        "resumeReady": False,
                    }
                )
        if deferred:
            try:
                self.controller.mark_native_construction_recovery_required()
            except RuntimeError:
                pass
        return receipt

    def _ensure_native_memory_meter(self) -> LinuxProcStatusVmHwmReset5 | None:
        """Open the default reset5 meter only for a fresh verified client."""

        if self.driver.native_memory_meter is not None:
            return None
        if not isinstance(self.driver.backend, X11Backend):
            raise DriverError("native reset5 memory meter requires the configured X11 backend")
        identity = self.driver.resolve_identity()
        try:
            linux_executable = os.path.realpath(os.readlink(f"/proc/{identity.pid}/exe"))
        except OSError as exc:
            raise DriverError("native process Linux executable is unavailable") from exc
        if not linux_executable or not Path(linux_executable).is_absolute():
            raise DriverError("native process Linux executable is incomplete")

        meter: LinuxProcStatusVmHwmReset5 | None = None

        def archive_over_limit(measurement: NativeMemoryMeasurement) -> NativeMemoryAssessmentArchiveReceipt:
            try:
                if meter is None:
                    raise DriverError("native resource-assessment meter is not owned")
                receipt = self._archive_native_memory_assessment(meter, measurement)
                if not isinstance(receipt, NativeMemoryAssessmentArchiveReceipt):
                    raise DriverError("native resource assessment archive returned no typed receipt")
                receipt.verify(measurement)
                return receipt
            except Exception:
                # Keep only a fixed private token.  The meter re-raises this
                # hard failure instead of allowing an unassessed overage.
                self._native_memory_archive_diagnostic = "native_memory_assessment_archive_failed"
                raise

        meter = LinuxProcStatusVmHwmReset5(
            identity.pid,
            process_start=identity.process_start,
            process_executable=linux_executable,
            identity_fingerprint=identity.fingerprint,
            expected_uid=os.getuid(),
            monotonic=self._native_clock,
            over_limit_archive=archive_over_limit,
        )
        self.driver.native_memory_meter = meter
        return meter

    def _archive_native_memory_failure(
        self,
        meter: LinuxProcStatusVmHwmReset5,
        measurement: NativeMemoryMeasurement,
        failure: str,
    ) -> NativeMemoryMeasurementArchiveReceipt:
        """Durably retain one complete over-limit window under live ownership."""

        expected_failure = f"native {measurement.phase} memory growth exceeds its limit"
        measurement.verify()
        if failure != expected_failure or measurement.growth_bytes <= measurement.limit_bytes:
            raise DriverError("native failed-memory outcome is not an over-limit rejection")
        if (
            measurement.method != meter.METHOD
            or measurement.pid != meter.pid
            or measurement.process_start != meter.process_start
            or measurement.process_executable != meter.process_executable
            or measurement.identity_fingerprint != meter.identity_fingerprint
            or measurement.uid != meter.expected_uid
            or measurement.kernel_release != meter.kernel_release
            or not meter.completion_matches(measurement)
        ):
            raise DriverError("native failed-memory measurement is not bound to its completing meter")

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native failed-memory archive requires NATIVE_CALIBRATION")
            if self.driver.native_memory_meter is not meter:
                raise DriverError("native failed-memory meter ownership changed")
            identity = self.driver.resolve_identity()
            if (
                identity.fingerprint != measurement.identity_fingerprint
                or identity.pid != measurement.pid
                or identity.process_start != measurement.process_start
            ):
                raise DriverError("native failed-memory client identity changed")
            meter._verify_identity()

            kind = "native_memory_failures"
            measurement_payload = measurement.to_dict()
            outcome_payload = {
                "kind": kind,
                "measurement": measurement_payload,
                "failure": failure,
            }
            outcome_id = hashlib.sha256(
                json.dumps(outcome_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            payload = {
                "schema": 1,
                "outcomeId": outcome_id,
                "phase": measurement.phase,
                "ordinal": measurement.ordinal,
                "decision": "rejected_over_limit",
                "qualifies": False,
                "failure": failure,
                "measurement": measurement_payload,
            }
            archived = self.controller.journal.append_archive(kind, payload)
            if (
                not isinstance(archived, dict)
                or set(archived) != set(payload) | {"archiveOrdinal", "archivedAt"}
                or any(archived.get(key) != value for key, value in payload.items())
                or isinstance(archived.get("archivedAt"), bool)
                or not isinstance(archived.get("archivedAt"), int)
            ):
                raise DriverError("native failed-memory archive result is invalid")
            receipt = NativeMemoryMeasurementArchiveReceipt.from_archive(measurement, archived)
            receipt.verify(measurement)
            return receipt

    def _archive_native_memory_assessment(
        self,
        meter: LinuxProcStatusVmHwmReset5,
        measurement: NativeMemoryMeasurement,
    ) -> NativeMemoryAssessmentArchiveReceipt:
        """Durably record one exact over-target result before native completion."""

        measurement.verify()
        if measurement.growth_bytes <= measurement.limit_bytes:
            raise DriverError("native resource assessment is not target-exceeded")
        if (
            measurement.method != meter.METHOD
            or measurement.pid != meter.pid
            or measurement.process_start != meter.process_start
            or measurement.process_executable != meter.process_executable
            or measurement.identity_fingerprint != meter.identity_fingerprint
            or measurement.uid != meter.expected_uid
            or measurement.kernel_release != meter.kernel_release
            or not meter.completion_matches(measurement)
        ):
            raise DriverError("native resource assessment is not bound to its completing meter")

        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native resource assessment requires NATIVE_CALIBRATION")
            if self.driver.native_memory_meter is not meter:
                raise DriverError("native resource assessment meter ownership changed")
            identity = self.driver.resolve_identity()
            if (
                identity.fingerprint != measurement.identity_fingerprint
                or identity.pid != measurement.pid
                or identity.process_start != measurement.process_start
            ):
                raise DriverError("native resource assessment client identity changed")
            meter._verify_identity()

            kind = "native_memory_assessments"
            measurement_payload = measurement.to_dict()
            outcome_payload = {
                "kind": kind,
                "schema": 1,
                "classification": "target_exceeded",
                "measurement": measurement_payload,
            }
            outcome_id = hashlib.sha256(
                json.dumps(outcome_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            payload = {
                **outcome_payload,
                "outcomeId": outcome_id,
                "phase": measurement.phase,
                "ordinal": measurement.ordinal,
            }
            archived = self.controller.journal.append_archive(kind, payload)
            if (
                not isinstance(archived, dict)
                or set(archived) != set(payload) | {"archiveOrdinal", "archivedAt"}
                or any(archived.get(key) != value for key, value in payload.items())
                or isinstance(archived.get("archivedAt"), bool)
                or not isinstance(archived.get("archivedAt"), int)
            ):
                raise DriverError("native resource assessment archive result is invalid")
            receipt = NativeMemoryAssessmentArchiveReceipt.from_archive(measurement, archived)
            receipt.verify(measurement)
            return receipt

    def _native_preconstruction_provider(
        self,
        identity: WindowIdentity,
        *,
        baseline_reader: Any,
        baseline_ready: Any,
    ) -> NativePreconstructionEvidence:
        if not callable(baseline_reader) or not callable(baseline_ready):
            raise DriverError("native preconstruction baseline callbacks are required")
        if self._shutdown_event.is_set():
            raise DriverError("native calibration was canceled during shutdown")
        with self.controller.lock:
            if self.controller.status().state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native calibration generation was revoked")
        with self._native_construction_lock:
            callback_context = self._native_construction_callback_context_locked()
            deferred = bool(callback_context.get("deferred"))
            construction_capability = self._native_construction_capability_locked() if deferred else None
            if deferred and self._native_construction_handshake.get("state") not in {
                "running",
                "construction_reset_archived",
            }:
                raise DriverError("deferred preconstruction entry arrived out of order")
        if deferred and not self._native_bootstrap_ready.is_set():
            # The deferred bootstrap consumes the reservation before reload;
            # this capture is the sealed post-reset absence boundary.
            with self.controller.lock:
                generation = self.controller.status().generation
            self._native_bootstrap_capture(
                expected_generation=generation,
                construction_capability=construction_capability,
            )
        if not self._native_bootstrap_ready.is_set():
            raise DriverError("external preconstruction bootstrap handshake is not ready")
        with self._native_bootstrap_lock:
            observation = self._native_bootstrap_observation
            if not observation or observation.get("identity") != identity.fingerprint:
                raise DriverError("preconstruction bootstrap identity is stale")
            if self._native_clock() - float(observation.get("capture", 0)) > 10.0:
                raise DriverError("preconstruction bootstrap observation is stale")
            capture_path = Path(str(observation.get("capturePath", "")))
            capture_sha256 = str(observation.get("captureDigest", ""))
            if not capture_path.is_absolute() or not capture_path.is_file():
                raise DriverError("preconstruction absence capture is unavailable")
            if hashlib.sha256(capture_path.read_bytes()).hexdigest() != capture_sha256:
                raise DriverError("preconstruction absence capture changed")
            # baseline_reader performs reset5.  Preserve the entry absence
            # evidence, then take an independently captured post-reset crop.
            baseline_hwm = baseline_reader(identity.pid)
            if not isinstance(baseline_hwm, int) or isinstance(baseline_hwm, bool) or baseline_hwm < 0:
                raise DriverError("native baseline reader returned an invalid HWM")
            entry_observation = dict(observation)
            post_observation = self._retain_native_absence_capture(identity)
            if post_observation.get("identity") != identity.fingerprint:
                raise DriverError("post-reset absence identity is stale")
            observation = post_observation
            capture_path = Path(str(observation.get("capturePath", capture_path)))
            capture_sha256 = str(observation.get("captureDigest", capture_sha256))
            if (
                not capture_path.is_file()
                or hashlib.sha256(capture_path.read_bytes()).hexdigest() != capture_sha256
            ):
                raise DriverError("post-reset absence capture changed")
            self._native_bootstrap_observation = observation
            baseline_ready(identity.pid, baseline_hwm)
            observation["baselineHwm"] = baseline_hwm
            if observation.get("baselineHwm") != baseline_hwm:
                raise DriverError("native baseline-ready callback did not publish the measured HWM")
            # Construction timing begins only after baseline-ready crosses the
            # reset receipt boundary.
            bootstrap_release = self._native_clock()
            evidence = NativePreconstructionEvidence._issue(
                identity_fingerprint=identity.fingerprint,
                process_start=identity.process_start,
                absent=True,
                baseline_id=capture_sha256,
                observed_monotonic=float(observation["capture"]),
                bootstrap_release_monotonic=bootstrap_release,
                display=identity.display,
                process_executable=identity.executable,
                window_xid=identity.xid,
                child_xid=identity.child_xid,
                geometry=identity.child_geometry,
                xauthority=self.driver.xauthority,
                wine_prefix=self.config.profile.wine_prefix,
                baseline_hwm_bytes=baseline_hwm,
                capture_path=str(capture_path),
                capture_sha256=capture_sha256,
            )
            self._native_bootstrap_proof = {
                "identityFingerprint": evidence.identity_fingerprint,
                "processStart": evidence.process_start,
                "processExecutable": evidence.process_executable,
                "display": evidence.display,
                "absent": evidence.absent,
                "baselineId": evidence.baseline_id,
                "baselineHwmBytes": evidence.baseline_hwm_bytes,
                "windowXid": evidence.window_xid,
                "childXid": evidence.child_xid,
                "geometry": list(evidence.geometry),
                "xauthority": evidence.xauthority,
                "winePrefix": evidence.wine_prefix,
                "observedMonotonic": evidence.observed_monotonic,
                "bootstrapReleaseMonotonic": evidence.bootstrap_release_monotonic,
                "capturePath": evidence.capture_path,
                "captureSha256": evidence.capture_sha256,
                "provenance": evidence.provenance,
                "fingerprint": evidence.fingerprint,
                "evidenceHash": evidence.evidence_hash,
            }
            if deferred:
                with self._native_construction_lock:
                    self._native_construction_callback_context_locked()
                    if self._native_construction_handshake.get("state") != "baseline_bound":
                        raise DriverError("deferred baseline evidence was not bound")
                    self._native_construction_handshake.update(
                        {
                            "entryAbsenceCaptureIdentity": entry_observation.get("identity"),
                            "entryAbsenceCaptureSha256": entry_observation.get("captureDigest"),
                            "entryAbsenceCapturePath": entry_observation.get("capturePath"),
                            "entryAbsenceCaptureMonotonic": entry_observation.get("capture"),
                            "releaseMonotonic": bootstrap_release,
                            "nativeBootstrapReady": False,
                            "absenceCaptureDeleted": False,
                        }
                    )
                    self._native_baseline_ready.set()
            self._native_bootstrap_observation = None
            self._native_bootstrap_ready.clear()
            return evidence

    def request_shutdown(self) -> None:
        """Stop supervision and revoke any desktop work before IPC exits."""

        self._shutdown_event.set()
        failures: list[BaseException] = []
        try:
            self.model.shutdown()
        except BaseException as exc:
            failures.append(exc)
        try:
            close = getattr(self.driver, "close", None)
            if callable(close):
                close()
            else:
                self.driver.revoke("session supervisor stopped")
        except BaseException as exc:
            failures.append(exc)
        thread = self._calibration_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                failures.append(RuntimeError("calibration worker did not terminate during shutdown"))
        keep_capture = None
        try:
            stored = self.controller.journal.read().get("calibrationEvidence", {})
            native_record = stored.get("native", {}) if isinstance(stored, dict) else {}
            if isinstance(native_record, dict):
                keep_capture = native_record.get("capture_path")
        except Exception:
            failures.append(RuntimeError("could not inspect durable native evidence during shutdown"))
        self._cleanup_native_owned_captures(keep=keep_capture)
        if failures:
            raise RuntimeError("session owner shutdown did not complete") from failures[0]

    @staticmethod
    def _remove_native_capture(path: Any) -> None:
        """Remove the crop and source emitted by one private native capture."""

        if not hasattr(path, "unlink"):
            return
        try:
            path.unlink()
        except OSError:
            pass
        parent = getattr(path, "parent", None)
        if parent is None:
            return
        # ``capture_client_crop_for_identity`` emits these two files in a
        # temporary directory.  Removing only the crop leaves desktop.png
        # behind and leaks one directory per native-gate frame.
        for name in ("desktop.png", "desktop.crop.png"):
            sibling = parent / name
            if sibling == path:
                continue
            try:
                sibling.unlink()
            except OSError:
                pass
        try:
            parent.rmdir()
        except OSError:
            pass

    def release_native_capture(self, path: Any) -> None:
        """Release one provider artifact after the native runner consumed it."""

        try:
            candidate = Path(os.fspath(path))
        except TypeError as exc:
            raise DriverError("native capture release requires a filesystem path") from exc
        with self._native_bootstrap_lock:
            self._native_owned_capture_paths.discard(candidate)
        diagnostic = getattr(self, "_native_observation_diagnostic", None)
        attach_to_attempt = (
            isinstance(diagnostic, _NativeObservationDiagnostic) and diagnostic._current_path == candidate
        )
        try:
            with _native_diagnostic_span(diagnostic, "cleanup", attach_to_attempt=attach_to_attempt):
                self._remove_native_capture(candidate)
                if isinstance(diagnostic, _NativeObservationDiagnostic) and candidate.exists():
                    raise _NativeCaptureRetained()
        except _NativeCaptureRetained:
            pass
        except Exception as exc:
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.mark_release(candidate, error=str(exc))
            raise
        if isinstance(diagnostic, _NativeObservationDiagnostic):
            diagnostic.mark_release(candidate)
        publish_release = False
        release_payload: dict[str, Any] | None = None
        with self._native_construction_lock:
            if (
                self._native_construction_reservation is not None
                and self._native_construction_reservation.get("reservationState") == "consumed"
                and self._native_construction_handshake.get("absenceCapturePath") == str(candidate)
            ):
                self._native_construction_handshake["absenceCaptureDeleted"] = not candidate.exists()
                publish_release = not candidate.exists()
                release_payload = {
                    "reservationId": self._native_construction_reservation.get("reservationId"),
                    "capturePath": str(candidate),
                    "captureSha256": self._native_construction_handshake.get("absenceCaptureSha256", ""),
                    "captureIdentity": self._native_construction_handshake.get("absenceCaptureIdentity", ""),
                    "captureMonotonic": self._native_construction_handshake.get("absenceCaptureMonotonic", 0),
                    "deleted": not candidate.exists(),
                }
        if publish_release and release_payload is not None:
            receipt = self.controller.journal.append_archive("native_construction", release_payload)
            if not isinstance(receipt, dict) or receipt.get("capturePath") != str(candidate):
                raise DriverError("native construction absence release archive is invalid")
            with self._native_construction_lock:
                reservation = self._native_construction_reservation
                if (
                    reservation is not None
                    and reservation.get("reservationState") == "consumed"
                    and self._native_construction_handshake.get("absenceCapturePath") == str(candidate)
                    and self._native_construction_handshake.get("absenceCaptureDeleted") is True
                ):
                    self._native_construction_publish_state_locked(
                        "construction_released", nativeBootstrapReady=True, releaseArchived=True
                    )
                    self._native_baseline_ready.set()

    def _cleanup_native_owned_captures(self, *, keep: Any | None = None) -> None:
        """Release provider-owned artifacts after the native runner finishes."""

        paths = set(self._native_owned_capture_paths)
        self._native_owned_capture_paths.clear()
        keep_path = os.fspath(keep) if keep is not None else None
        for path in paths:
            if keep_path is not None and os.fspath(path) == keep_path:
                self._native_owned_capture_paths.add(path)
                continue
            self._remove_native_capture(path)

    def _capture_native_frame_with_capture(
        self,
        identity: WindowIdentity,
        decoder: PixelDecoder,
        *,
        deadline: float | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | None = None,
        native_capability: _NativeUpdateCapability | None = None,
        native_update_ordinal: int | None = None,
    ) -> tuple[Sample, str, Any, Any | None]:
        capture_started = self._native_clock()
        diagnostic = getattr(self, "_native_observation_diagnostic", None)
        backend = getattr(self.driver, "backend", None)
        before_receipt = getattr(backend, "last_region_receipt", None)
        receipt_holder: list[Any] = []
        sink = (
            _NativeIdentityLatencySink(diagnostic)
            if isinstance(diagnostic, _NativeObservationDiagnostic)
            else None
        )
        sink_token = _set_identity_latency_sink(sink) if sink is not None else None
        try:
            with _native_diagnostic_span(diagnostic, "capture"):
                path = self._capture_native_region_for_identity(
                    identity,
                    deadline=deadline,
                    expected_generation=expected_generation,
                    expected_state=expected_state,
                    native_capability=native_capability,
                    native_update_ordinal=native_update_ordinal,
                    **({"_receipt_sink": receipt_holder.append} if native_capability is not None else {}),
                )
        finally:
            if sink_token is not None:
                _reset_identity_latency_sink(sink_token)
        retained = None
        try:
            if deadline is not None:
                self._remaining_motion_budget(deadline)
            with _native_diagnostic_span(diagnostic, "provider_hash"):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if native_capability is not None:
                # Eligible updates consume only the finalized receipt handed
                # off by this exact capture.  A missing/multiple handoff is a
                # refusal; the mutable compatibility field cannot stand in
                # for a per-attempt proof.
                after_receipt = receipt_holder[-1] if len(receipt_holder) == 1 else None
            else:
                after_receipt = getattr(backend, "last_region_receipt", None)
            receipt_valid = (
                after_receipt is not None
                and after_receipt is not before_receipt
                and getattr(after_receipt, "output_sha256", None) == digest
            )
            receipt_capture_started = getattr(after_receipt, "capture_started", None)
            if native_capability is not None and (
                len(receipt_holder) != 1
                or not receipt_valid
                or isinstance(receipt_capture_started, bool)
                or not isinstance(receipt_capture_started, (int, float))
                or not math.isfinite(float(receipt_capture_started))
                or float(receipt_capture_started) < capture_started
                or float(receipt_capture_started) > self._native_clock()
            ):
                raise DriverError("native eligible capture did not publish a fresh bound receipt")
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.record_backend_receipt(after_receipt if receipt_valid else None)
            with _native_diagnostic_span(diagnostic, "provider_decode"):
                sample = self.driver.decode_native_capture(path, identity=identity, decoder=decoder)
            # Decoder timestamps are publication/return times.  Freshness for
            # native consumers must instead describe when this capture was
            # admitted, before the potentially slow crop/decode path began.
            sample = replace(sample, capture_monotonic=capture_started)
            if deadline is not None:
                self._remaining_motion_budget(deadline)
            # NativeCalibrationEvidence retains the final decoded crop for
            # post-review verification.  The backend's default temporary
            # capture directory is unsuitable for that durable proof, so
            # copy each provider artifact into a private persistent evidence
            # directory before returning it to the native runner.  The runner
            # then decodes and seals this exact retained path/hash.
            with _native_diagnostic_span(diagnostic, "copy"):
                evidence_dir = self.config.journal.expanduser().parent / "native-evidence"
                evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(evidence_dir, 0o700)
                retained = evidence_dir / f"capture-{time.monotonic_ns()}-{self.capture_index:08d}.png"
                shutil.copyfile(path, retained)
                os.chmod(retained, 0o600)
            self._remove_native_capture(path)
            self._native_owned_capture_paths.add(retained)
            return sample, digest, retained, after_receipt if receipt_valid else None
        except Exception:
            self._remove_native_capture(path)
            if retained is not None:
                try:
                    retained.unlink()
                except OSError:
                    pass
            raise

    def _capture_native_frame_with_digest(
        self,
        identity: WindowIdentity,
        decoder: PixelDecoder,
        *,
        deadline: float | None = None,
    ) -> tuple[Sample, str]:
        sample, digest, path, _receipt = self._capture_native_frame_with_capture(
            identity, decoder, deadline=deadline
        )
        try:
            return sample, digest
        finally:
            self.release_native_capture(path)

    def _capture_native_frame(
        self,
        identity: WindowIdentity,
        decoder: PixelDecoder,
        *,
        deadline: float | None = None,
    ) -> Sample:
        sample, _digest = self._capture_native_frame_with_digest(identity, decoder, deadline=deadline)
        return sample

    def _native_measurement_deadline(
        self,
        attempt_started: float,
        capability: _NativeUpdateCapability | None,
        *,
        frame_deadline: float | None = None,
        include_producer_limit: bool = True,
    ) -> float:
        """Resolve one bounded deadline for a provider attempt.

        The Agent publication wait is intentionally longer than the driver's
        producer bound.  Capture, producer IPC, and pre-ACK admission must all
        consume this same absolute deadline, with the live operation/owner
        limits able to shorten it but never extend the fifteen-second driver
        limit.
        """

        if (
            isinstance(attempt_started, bool)
            or not isinstance(attempt_started, (int, float))
            or not math.isfinite(float(attempt_started))
        ):
            raise DriverError("native measurement attempt clock is malformed")
        deadline = (
            float(attempt_started) + self._NATIVE_FRAME_TIMEOUT_SECONDS
            if frame_deadline is None
            else float(frame_deadline)
        )
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise DriverError("native measurement frame deadline is malformed")
        if include_producer_limit:
            deadline = min(deadline, float(attempt_started) + self._NATIVE_PRODUCER_TIMEOUT_SECONDS)
        with self._native_construction_lock:
            candidates = (
                self._native_construction_operation_deadline,
                (self._native_construction_reservation or {}).get("expiresMonotonic"),
                capability.expires_monotonic if capability is not None else None,
            )
            for candidate in candidates:
                if candidate is None:
                    continue
                if (
                    isinstance(candidate, bool)
                    or not isinstance(candidate, (int, float))
                    or not math.isfinite(float(candidate))
                ):
                    raise DriverError("native measurement deadline is malformed")
                deadline = min(deadline, float(candidate))
        return deadline

    def _native_provider_sleep(self, seconds: float) -> None:
        """Sleep through the daemon-local provider seam, never global time."""

        hook = getattr(self, "_native_provider_sleep_hook", None)
        (hook if callable(hook) else time.sleep)(seconds)

    def _native_measurement_provider(
        self, identity: WindowIdentity, stage: str, ordinal: int
    ) -> NativeRenderObservation:
        """Wait for one actual, identity-bound Agent publication.

        Readiness callbacks are signals only.  The authorized operator drives
        ``/crowley agent hold|measure|resume`` in the game, and this producer
        polls the real status surface until that action publishes the requested
        fresh frame.  Once update 1 arrives, every later ordinal is exact.
        """

        diagnostic = getattr(self, "_native_observation_diagnostic", None)
        if isinstance(diagnostic, _NativeObservationDiagnostic):
            diagnostic.ensure_started()

        if stage not in {"construct", "held", "updates", "resume"}:
            raise DriverError(f"unknown native measurement stage {stage!r}")
        expected_phase, expected_ordinal = {
            "construct": ("normal", 0),
            "held": ("held", ordinal),
            "updates": ("updates", ordinal),
            "resume": ("normal", 0),
        }[stage]
        if stage == "updates" and not 1 <= ordinal <= 99:
            raise DriverError("native update ordinal must be in 1..99")
        if stage == "held" and ordinal not in {0, 100}:
            raise DriverError("native held ordinal must be zero or one hundred")
        if stage in {"construct", "resume"} and ordinal != 0:
            raise DriverError(f"native {stage} ordinal must be zero")
        if self._shutdown_event.is_set():
            raise DriverError("native calibration was canceled during shutdown")
        with self.controller.lock:
            live_status = self.controller.status()
            if live_status.state != ControllerState.NATIVE_CALIBRATION:
                raise DriverError("native calibration generation was revoked")
            generation = live_status.generation
        identity_budget_capability: _NativeUpdateCapability | None = None
        if stage == "updates":
            eligibility = getattr(self.driver, "_native_identity_budget_eligibility", None)
            if callable(eligibility):
                diagnostic = getattr(self, "_native_observation_diagnostic", None)
                with _native_diagnostic_span(diagnostic, "alignment_capability"):
                    identity_budget_capability = eligibility(
                        self.driver.native_measurement_provider,
                        identity,
                        stage,
                        ordinal,
                    )
        attempt_started = self._native_clock()
        frame_deadline = self._native_measurement_deadline(
            attempt_started,
            identity_budget_capability,
            include_producer_limit=False,
        )
        with self._native_construction_lock:
            context = self._native_construction_run_context
            if context is not None and context.get("closed"):
                raise DriverError("native construction capture owner was revoked")
            deferred = stage == "construct" and bool(context and context.get("deferred"))
            if deferred:
                if self._native_construction_handshake.get("state") != "construction_released":
                    raise DriverError("deferred construction provider entered before release")
                release_monotonic = self._native_construction_handshake.get("releaseMonotonic")
                operation_deadline = self._native_construction_operation_deadline
                click_deadline = frame_deadline - 7.0
                if (
                    not isinstance(release_monotonic, (int, float))
                    or not math.isfinite(float(release_monotonic))
                    or click_deadline <= float(release_monotonic)
                    or operation_deadline is None
                    or not math.isfinite(float(operation_deadline))
                    or click_deadline >= float(operation_deadline)
                ):
                    raise DriverError("deferred constructor click deadline is invalid")
                self._native_construction_handshake.update(
                    {
                        "providerDeadline": frame_deadline,
                        "constructorClickDeadline": click_deadline,
                    }
                )
        state = self._native_measurement_state
        previous_publisher_uptime_ms: int | None = None
        if stage == "construct":
            decoder = self.driver.new_native_decoder()
            prior_sequence: int | None = None
            expected_boot: str | None = None
        else:
            if (
                state is None
                or state.get("identity") != identity.fingerprint
                or state.get("generation") != generation
            ):
                raise DriverError(f"native {stage} has no matching construction identity")
            decoder = state["decoder"]
            prior_sequence = int(state["sequence"])
            expected_boot = str(state["boot"])
            previous_publisher_uptime_ms = state.get("publisherUptimeMs")

        accepted: tuple[Sample, str, Any, Any | None] | None = None
        while self._native_clock() < frame_deadline:
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.begin_attempt(stage, ordinal)
                try:
                    self._check_native_poll_identity(
                        identity,
                        generation,
                        native_capability=identity_budget_capability,
                    )
                except Exception as exc:
                    diagnostic.finish_attempt(status="error", error=str(exc))
                    raise
            else:
                self._check_native_poll_identity(
                    identity,
                    generation,
                    native_capability=identity_budget_capability,
                )
            try:
                capture_deadline = self._native_measurement_deadline(
                    self._native_clock(),
                    identity_budget_capability,
                    frame_deadline=frame_deadline,
                )
                capture_context = (
                    _native_diagnostic_span(diagnostic, "poll_backend_glue")
                    if stage == "updates"
                    else nullcontext()
                )
                with capture_context:
                    candidate, candidate_id, candidate_path, candidate_receipt = (
                        self._capture_native_frame_with_capture(
                            identity,
                            decoder,
                            deadline=capture_deadline,
                            expected_generation=(
                                generation if identity_budget_capability is not None else None
                            ),
                            expected_state=(
                                ControllerState.NATIVE_CALIBRATION
                                if identity_budget_capability is not None
                                else None
                            ),
                            native_capability=identity_budget_capability,
                            native_update_ordinal=ordinal if stage == "updates" else None,
                        )
                    )
            except ProtocolError:
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="protocol_rejected")
                # Before Agent construction the crop is ordinary game pixels.
                # Keep polling until the operator has loaded the addon, then
                # fail closed at the bounded deadline if no valid frame arrives.
                if stage == "construct":
                    self._native_provider_sleep(self._NATIVE_POLL_INTERVAL_SECONDS)
                    continue
                raise
            except DriverError as exc:
                now = self._native_clock()
                if now >= frame_deadline:
                    if isinstance(diagnostic, _NativeObservationDiagnostic):
                        diagnostic.finish_attempt(status="no_change", error=str(exc))
                    break
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="error", error=str(exc))
                raise
            except Exception as exc:
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="error", error=str(exc))
                raise
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.record_candidate(candidate, candidate_id, candidate_path)
            if candidate.kind != "FRESH" or candidate.frame is None:
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.set_status(status="no_change")
                self.release_native_capture(candidate_path)
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="no_change")
                self._native_provider_sleep(self._NATIVE_POLL_INTERVAL_SECONDS)
                continue
            frame = candidate.frame
            render = frame.snapshot.value.get("render")
            if not isinstance(render, dict):
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.fail(
                        "native frame has no render metrics",
                        stage=stage,
                        ordinal=ordinal,
                    )
                    diagnostic.set_status(status="error", error="native frame has no render metrics")
                self.release_native_capture(candidate_path)
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="error", error="native frame has no render metrics")
                raise DriverError("native frame has no render metrics")
            actual_phase = render.get("nativePhase")
            actual_ordinal = render.get("nativeOrdinal")
            if expected_boot is not None and frame.boot != expected_boot:
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.fail(
                        f"native {stage} frame boot changed during operator wait",
                        stage=stage,
                        ordinal=ordinal,
                    )
                    diagnostic.set_status(status="error", error="boot changed")
                self.release_native_capture(candidate_path)
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="error", error="boot changed")
                raise DriverError(f"native {stage} frame boot changed during operator wait")
            if prior_sequence is not None and frame.sequence <= prior_sequence:
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.set_status(status="no_change")
                self.release_native_capture(candidate_path)
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="no_change")
                self._native_provider_sleep(self._NATIVE_POLL_INTERVAL_SECONDS)
                continue

            if (actual_phase, actual_ordinal) == (expected_phase, expected_ordinal):
                if stage == "construct":
                    # A character-select/disconnected frame must never become
                    # the construction proof, even if it carries Agent data.
                    self._validate_identity(frame.snapshot.value, state=ControllerState.NATIVE_CALIBRATION)
                accepted = (candidate, candidate_id, candidate_path, candidate_receipt)
                if isinstance(diagnostic, _NativeObservationDiagnostic):
                    diagnostic.finish_attempt(status="accepted")
                break

            # Hold-required may see normal/0 while the operator reacts;
            # measure-ready may still see held/0 until Measure is issued; and
            # resume-required may see held/100 until Resume is issued.
            waitable = (
                (stage == "held" and ordinal == 0 and (actual_phase, actual_ordinal) == ("normal", 0))
                or (stage == "updates" and ordinal == 1 and (actual_phase, actual_ordinal) == ("held", 0))
                or (stage == "resume" and (actual_phase, actual_ordinal) == ("held", 100))
            )
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                message = f"native {stage}/{ordinal} returned {actual_phase!r}/{actual_ordinal!r}"
                if not waitable:
                    diagnostic.fail(
                        message,
                        stage=stage,
                        ordinal=ordinal,
                        offending_path=candidate_path,
                        mismatch=True,
                    )
                    diagnostic.set_status(status="mismatch", error=message)
                else:
                    diagnostic.set_status(status="waitable")
            self.release_native_capture(candidate_path)
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.finish_attempt(status="mismatch" if not waitable else "waitable", error=None)
            if waitable:
                self._native_provider_sleep(self._NATIVE_POLL_INTERVAL_SECONDS)
                continue
            raise DriverError(f"native {stage}/{ordinal} returned {actual_phase!r}/{actual_ordinal!r}")

        if accepted is None:
            raise DriverError(f"native {stage}/{ordinal} did not produce the requested fresh frame")
        sample, capture_id, capture_path, capture_receipt = accepted
        assert sample.frame is not None
        if sample.capture_monotonic < 0:
            self.release_native_capture(capture_path)
            raise DriverError(f"native {stage} capture timestamp is invalid")
        render = sample.frame.snapshot.value.get("render")
        assert isinstance(render, dict)
        if stage == "construct":
            self._native_measurement_state = {
                "identity": identity.fingerprint,
                "decoder": decoder,
                "boot": sample.frame.boot,
                "sequence": sample.frame.sequence,
                "captureMonotonic": sample.capture_monotonic,
                "generation": generation,
                "publisherUptimeMs": sample.frame.uptime_ms,
            }
        else:
            assert state is not None
            previous_publisher_uptime_ms = state.get("publisherUptimeMs")
            state["sequence"] = sample.frame.sequence
            state["captureMonotonic"] = sample.capture_monotonic
            state["publisherUptimeMs"] = sample.frame.uptime_ms
        if stage == "updates":
            self._native_update_alignment_record(
                provider=self.driver.native_measurement_provider,
                identity=identity,
                capability=identity_budget_capability,
                ordinal=ordinal,
                capture_monotonic=sample.capture_monotonic,
                publisher_uptime_ms=sample.frame.uptime_ms,
                previous_uptime_ms=previous_publisher_uptime_ms,
                receipt=capture_receipt,
                capture_digest=capture_id,
            )
        return NativeRenderObservation(
            metrics=dict(render),
            identity_fingerprint=identity.fingerprint,
            boot=sample.frame.boot,
            sequence=sample.frame.sequence,
            crc32=sample.frame.crc32,
            capture_id=capture_id,
            capture_path=str(capture_path),
            capture_sha256=capture_id,
            capture_monotonic=sample.capture_monotonic,
            native_phase=render.get("nativePhase"),
            native_ordinal=render.get("nativeOrdinal"),
        )

    def _check_native_poll_identity(
        self,
        identity: WindowIdentity,
        generation: int,
        *,
        native_capability: _NativeUpdateCapability | None = None,
    ) -> None:
        if self._shutdown_event.is_set():
            raise DriverError("native calibration was canceled during operator wait")
        reservation_snapshot: dict[str, Any] | None = None
        context_snapshot: dict[str, Any] | None = None
        with self._native_construction_lock:
            if native_capability is not None:
                diagnostic = getattr(self, "_native_observation_diagnostic", None)
                with _native_diagnostic_span(diagnostic, "alignment_capability"):
                    capability_matches = self._native_update_capability_matches_locked(
                        native_capability, identity
                    )
                if not capability_matches:
                    raise DriverError("native update identity capability was revoked")
            else:
                context = self._native_construction_run_context
                reservation = self._native_construction_reservation
                operation_deadline = self._native_construction_operation_deadline
                if (
                    context is not None
                    and context.get("deferred")
                    and reservation is not None
                    and reservation.get("reservationId") == context.get("reservationId")
                    and operation_deadline is not None
                    and self._native_clock() >= operation_deadline
                ):
                    raise DriverError("deferred native construction operation deadline expired")
                if context is not None and context.get("deferred"):
                    self._native_construction_callback_context_locked()
                    if reservation is None:
                        raise DriverError("native construction reservation is unavailable")
                    reservation_snapshot = copy.deepcopy(reservation)
                    context_snapshot = copy.deepcopy(context)
        if native_capability is None and reservation_snapshot is not None:
            self._native_construction_validate_current_locked(
                reservation_snapshot, expected_generation=generation
            )
            with self._native_construction_lock:
                current_context = self._native_construction_run_context
                current_reservation = self._native_construction_reservation
                if (
                    current_context is None
                    or context_snapshot is None
                    or current_context.get("runId") != context_snapshot.get("runId")
                    or current_context.get("epoch") != context_snapshot.get("epoch")
                    or current_reservation is None
                    or current_reservation.get("reservationId") != reservation_snapshot.get("reservationId")
                ):
                    raise DriverError("native construction owner changed during poll validation")
        if native_capability is None:
            diagnostic = getattr(self, "_native_observation_diagnostic", None)
            with _native_diagnostic_span(diagnostic, "identity_poll"):
                current_identity = self.driver.resolve_identity()
            if current_identity.fingerprint != identity.fingerprint:
                raise DriverError("native client identity changed during operator wait")
        with self.controller.lock:
            status = self.controller.status()
            if status.state != ControllerState.NATIVE_CALIBRATION or status.generation != generation:
                raise DriverError("native calibration generation was revoked")

    def _measure_pointer_anchor(self, identity: WindowIdentity, action: str) -> MeasuredAnchor:
        """Measure one named control from one fresh owned-client visual.

        The native driver owns capture admission and dispatch.  This method is
        the semantic producer: Luna reports facts and bounds from the captured
        client crop, and this method binds those values to the verified client
        identity before handing them back to the native driver.  A configured
        external provider remains usable for a separately owned native test
        boundary, but no backend method or profile coordinate is consulted.
        """

        controls = getattr(self.driver, "ANCHOR_CONTROLS", {})
        expected_control = controls.get(action)
        if not isinstance(expected_control, str) or not expected_control:
            raise DriverError(f"no named visual control contract exists for {action!r}")

        # A native worker may provide the capture/sealing half of this
        # contract.  It must already return a complete evidence record; this
        # path deliberately does not ask a backend for coordinates.
        provider = self._configured_anchor_provider
        if callable(provider):
            try:
                anchor = provider(identity, action)
            except Exception as exc:
                raise DriverError(f"native control-anchor measurement failed for {action}: {exc}") from exc
            if not isinstance(anchor, MeasuredAnchor):
                raise DriverError("native anchor producer returned unbound coordinate evidence")
            if (
                anchor.action != action
                or anchor.identity_fingerprint != identity.fingerprint
                or anchor.generation != self.driver.guardian.generation
                or dict(anchor.facts).get("control") != expected_control
                or dict(anchor.facts).get("capture") != anchor.capture_id
                or dict(anchor.facts).get("target") != str(anchor.target_xid)
            ):
                raise DriverError("native control-anchor identity, action, or control does not match")
            return anchor

        expected_state = {
            "dismiss_disconnect": "disconnect_dialog",
            "reconnect_game": "game_client",
            "select_arthur": "character_select",
            "enter_world": "arthur_selected",
            "open_ah": "world",
            "logout": "world",
            "executor_click": "executor_mailbox",
        }.get(action)
        if expected_state is None:
            raise DriverError(f"action {action!r} has no visual anchor scene")

        with self.controller.lock:
            status = self.controller.status()
            generation = status.generation
            state_before = status.state
        guardian_generation = self.driver.guardian.generation
        if not self.driver.guardian.armed or guardian_generation <= 0:
            raise DriverError("visual anchor requires the current armed input lease")

        anchor_deadline = time.monotonic() + 5.0
        driver_anchor_deadline = self.driver.anchor_observation_deadline
        if driver_anchor_deadline is not None:
            if (
                isinstance(driver_anchor_deadline, bool)
                or not isinstance(driver_anchor_deadline, (int, float))
                or not math.isfinite(float(driver_anchor_deadline))
            ):
                raise DriverError("driver anchor observation deadline is malformed")
            anchor_deadline = min(anchor_deadline, float(driver_anchor_deadline))
        if time.monotonic() >= anchor_deadline:
            raise DriverError("visual control anchor exceeded its bounded deadline")
        before = self.driver.resolve_identity(deadline_monotonic=anchor_deadline)
        if before.fingerprint != identity.fingerprint:
            raise DriverError("anchor capture started with a stale client identity")

        capture_path: Path | None = None
        model_started = time.monotonic()
        captured_at = model_started
        proof: dict[str, Any] = {
            "action": action,
            "control": expected_control,
            "scene": expected_state,
            "generation": generation,
            "leaseGeneration": guardian_generation,
            "identity": identity.fingerprint,
            "accepted": False,
            "captureMonotonic": captured_at,
        }
        try:
            capture_path, digest, captured_at = self._capture_model_artifact(
                identity, generation, deadline=anchor_deadline
            )
            proof.update(
                {
                    "capturePath": str(capture_path),
                    "captureSha256": digest,
                    "captureId": digest,
                    "captureSequence": self._named_capture_sequence,
                    "captureMonotonic": captured_at,
                }
            )
            image = capture_path.read_bytes()
            anchor_remaining = anchor_deadline - time.monotonic()
            if anchor_remaining <= 0:
                raise DriverError("visual control anchor exceeded its bounded deadline")
            observation = self._observe_model(
                image,
                timeout=anchor_remaining,
                requested_state=expected_state,
                requested_control=expected_control,
            )
            model_finished = time.monotonic()
            proof["modelTiming"] = {
                "startedMonotonic": model_started,
                "finishedMonotonic": model_finished,
                "durationSeconds": model_finished - model_started,
            }
            after = self.driver.resolve_identity(deadline_monotonic=anchor_deadline)
            if after.fingerprint != identity.fingerprint:
                raise DriverError("client identity changed during visual anchor inference")
            if model_finished > anchor_deadline or model_finished - captured_at > 5.0:
                raise DriverError("visual control anchor inference exceeded the freshness deadline")
            if observation.state != expected_state:
                raise DriverError(
                    f"visual anchor reported {observation.state!r}, expected {expected_state!r}"
                )
            facts = dict(observation.facts)
            expected = self._named_facts(
                expected_state,
                character=self.config.profile.character,
                realm=self.config.profile.realm,
                idle="true",
            )
            expected["control"] = expected_control
            if facts != expected:
                raise DriverError(f"visual control facts did not match {action}: {facts!r}")
            bounds = observation.anchor
            if bounds is None:
                raise DriverError("visual control observation did not include measured bounds")
            x, y, width, height = bounds
            crop_x, crop_y, crop_width, crop_height = self.config.profile.model_crop
            if (
                width <= 0
                or height <= 0
                or x < 0
                or y < 0
                or x + width > crop_width
                or y + height > crop_height
            ):
                raise DriverError("visual control bounds are outside the verified model crop")
            client_bounds = (crop_x + x, crop_y + y, width, height)
            anchor_x = client_bounds[0] + width // 2
            anchor_y = client_bounds[1] + height // 2
            if not (
                0 <= anchor_x < identity.child_geometry[2] and 0 <= anchor_y < identity.child_geometry[3]
            ):
                raise DriverError("visual control anchor is outside the verified client child")
            self._named_capture_sequence += 1
            capture_id = digest
            facts["capture"] = capture_id
            facts["target"] = str(identity.child_xid)
            facts["captureMonotonic"] = str(captured_at)
            facts["modelDurationSeconds"] = str(model_finished - model_started)
            target_ancestry = (
                (identity.xid, identity.child_xid)
                if isinstance(self.driver.backend, X11Backend)
                else (identity.child_xid,)
            )
            anchor = self.driver.build_measured_anchor(
                action=action,
                x=anchor_x,
                y=anchor_y,
                capture_path=capture_path,
                capture_id=capture_id,
                capture_sequence=self._named_capture_sequence,
                capture_monotonic=captured_at,
                scene=expected_state,
                bounds=client_bounds,
                target_xid=identity.child_xid,
                target_ancestry=target_ancestry,
                generation=guardian_generation,
                source="native-visual",
                facts=tuple(sorted(facts.items())),
                deadline_monotonic=anchor_deadline,
            )
            proof.update(
                {
                    "accepted": True,
                    "captureSequence": anchor.capture_sequence,
                    "bounds": list(anchor.bounds),
                    "targetXid": anchor.target_xid,
                    "targetAncestry": list(anchor.target_ancestry),
                    "evidenceHash": anchor.evidence_hash,
                }
            )
            self._record_visual_proof(proof, generation=generation, state=state_before)
            return anchor
        except Exception:
            proof.setdefault(
                "modelTiming",
                {
                    "startedMonotonic": model_started,
                    "finishedMonotonic": time.monotonic(),
                },
            )
            self._record_visual_proof(proof, generation=generation, state=state_before)
            raise

    def _capture_model_artifact(
        self,
        identity: WindowIdentity,
        generation: int,
        *,
        deadline: float | None = None,
    ) -> tuple[Path, str, float]:
        """Retain one lossless model crop before a visual anchor is sealed."""

        if deadline is not None and time.monotonic() >= deadline:
            raise DriverError("visual control anchor exceeded its bounded deadline")
        self.config.capture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.config.capture_dir, 0o700)
        self.capture_index += 1
        source = self.config.capture_dir / f"anchor-{generation}-{self.capture_index:08d}.png"
        cropped: Path | None = None
        try:
            cropped = self._capture_client_crop(source, model=True, deadline=deadline)
            captured_at = time.monotonic()
            if deadline is not None and captured_at >= deadline:
                raise DriverError("visual control capture exceeded its bounded deadline")
            current = self._resolve_identity(deadline=deadline)
            if current.fingerprint != identity.fingerprint:
                raise DriverError("model capture identity changed before inference")
            evidence_dir = self.config.journal.expanduser().parent / "model-evidence"
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(evidence_dir, 0o700)
            retained = evidence_dir / f"anchor-{generation}-{self.capture_index:08d}.png"
            shutil.copyfile(cropped, retained)
            os.chmod(retained, 0o600)
            digest = hashlib.sha256(retained.read_bytes()).hexdigest()
            if deadline is not None and time.monotonic() >= deadline:
                raise DriverError("visual control evidence exceeded its bounded deadline")
            return retained, digest, captured_at
        except Exception:
            if cropped is not None:
                cropped.unlink(missing_ok=True)
            source.unlink(missing_ok=True)
            raise
        finally:
            if cropped is not None:
                cropped.unlink(missing_ok=True)
            source.unlink(missing_ok=True)

    def _record_visual_proof(self, proof: dict[str, Any], *, generation: int, state: ControllerState) -> None:
        """Archive model and anchor acceptance without hiding the action error."""

        try:
            with self.controller.lock:
                current = self.controller.status()
            if current.generation != generation or current.state != state:
                return
            self.controller.record_model_proof(
                proof,
                expected_generation=generation,
                expected_state=state,
            )
            self.controller.record_anchor_proof(
                proof,
                expected_generation=generation,
                expected_state=state,
            )
        except (AttributeError, DriverError, OSError, RuntimeError, ValueError):
            # Proof archival must never turn a successfully sealed anchor into
            # an unbound coordinate; the native driver remains the admission
            # authority.  The immutable capture is retained for later review.
            return

    @staticmethod
    def _named_facts(state: str, *, character: str, realm: str, idle: str) -> dict[str, str]:
        facts = {
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
                "character": character,
                "realm": realm,
                "modal": "character_select",
                "code": "",
                "control": "character_list",
                "idle": idle,
            },
            "arthur_selected": {
                "scene": "character_select",
                "character": character,
                "realm": realm,
                "modal": "character_select",
                "code": "",
                "control": "arthur_row",
                "idle": idle,
            },
            "world": {
                "scene": "world",
                "character": character,
                "realm": realm,
                "modal": "none",
                "code": "",
                "control": "world",
                "idle": idle,
            },
            "auction_house_open": {
                "scene": "world",
                "character": character,
                "realm": realm,
                "modal": "auction_house",
                "code": "",
                "control": "auction_house",
                "idle": idle,
            },
            "executor_mailbox": {
                "scene": "world",
                "character": character,
                "realm": realm,
                "modal": "mailbox",
                "code": "",
                "control": "crowley_executor_button",
                "idle": idle,
            },
        }
        try:
            return facts[state]
        except KeyError as exc:
            raise DriverError(f"unknown named observation state {state!r}") from exc

    def _check_named_facts(self, state: str, facts: dict[str, str]) -> None:
        expected = self._named_facts(
            state,
            character=self.config.profile.character,
            realm=self.config.profile.realm,
            idle="true",
        )
        if facts != expected:
            raise DriverError(f"named visual facts did not match {state}: {facts!r}")

    @staticmethod
    def _scanner_idle_ready(snapshot: dict[str, Any], expected_uid: str = "") -> bool:
        """Prove the scanner is in an explicit idle phase for closeout."""

        pending = snapshot.get("pending", {})
        pending_ids = pending.get("ids", []) if isinstance(pending, dict) else []
        if pending_ids:
            return False
        scanner = snapshot.get("scanner", {})
        if not isinstance(scanner, dict) or scanner.get("activeSweepId"):
            return False
        if scanner.get("phase") not in {"ready", "scheduled"}:
            return False
        if expected_uid:
            saved = snapshot.get("lastSaved", {})
            if not isinstance(saved, dict) or saved.get("memoryUid") != expected_uid:
                return False
        return True

    @staticmethod
    def _logout_scanner_idle(snapshot: dict[str, Any], expected_uid: str = "") -> bool:
        """Prove that a final logout cannot race a newly started sweep.

        The closeout caller supplies a fresh post-ack frame and the caller's
        pre-logout schedule fence rejects any item/owned frame before this
        predicate is reached.  Only the addon's explicit idle phases are a
        valid logout witness.  ``activeSweepId`` is intentionally checked
        independently because the scanner can keep it empty during a live
        item phase.
        """

        if not SessionDaemon._scanner_idle_ready(snapshot, expected_uid):
            return False
        scanner = snapshot.get("scanner", {})
        if not isinstance(scanner, dict):
            return False
        fence = scanner.get("scheduleFence")
        return fence == {
            "state": "armed",
            "reason": "",
            "command": "/crowley session fence",
        }

    def _observe_addon_state(self, identity: WindowIdentity, state: str) -> NamedStateObservation:
        decoder = self.driver.new_native_decoder()
        baseline = self._last_addon_sequence
        named_deadline = getattr(self, "_named_observation_deadline", None)
        deadline = min(
            time.monotonic() + 8.0,
            named_deadline if isinstance(named_deadline, (int, float)) else float("inf"),
        )
        while time.monotonic() < deadline:
            sample = self._capture_native_frame(identity, decoder, deadline=deadline)
            if sample.kind != "FRESH" or sample.frame is None:
                time.sleep(0.05)
                continue
            frame = sample.frame
            # Sequence numbers are only ordered within one addon boot.  A
            # world transition legitimately publishes a fresh boot starting
            # at sequence one; comparing that counter with the prior menu
            # boot would wait forever for an impossible value.
            if frame.boot == self._last_addon_boot and frame.sequence <= baseline:
                time.sleep(0.05)
                continue
            snapshot = frame.snapshot.value
            context = snapshot.get("context", {})
            if (
                context.get("character") != self.config.profile.character
                or context.get("realm") != self.config.profile.realm
            ):
                raise DriverError("fresh addon observation has the wrong character or realm")
            ah_open = snapshot.get("ahOpen") is True
            if state == "auction_house_open" and not ah_open:
                time.sleep(0.05)
                continue
            if state == "world" and ah_open:
                time.sleep(0.05)
                continue
            # Telemetry proves the exact configured identity and AH boolean;
            # the visual observer independently proves the visible scene,
            # modal, control, and idle facts.  Neither source is allowed to
            # manufacture the other's fields.
            try:
                visual_image = self._capture_model(deadline=deadline)
                visual_remaining = deadline - time.monotonic()
                if visual_remaining <= 0:
                    continue
            except DriverError:
                break
            visual = self._observe_model(
                visual_image,
                timeout=min(5.0, visual_remaining),
            )
            if time.monotonic() > deadline:
                raise DriverError("visual addon-state inference exceeded the freshness deadline")
            after_model = self._resolve_identity(deadline=deadline)
            if after_model.fingerprint != identity.fingerprint:
                raise DriverError("client identity changed during addon-state inference")
            expected_visual_state = "auction_house_open" if state == "auction_house_open" else "world"
            if visual.state != expected_visual_state:
                time.sleep(0.05)
                continue
            facts = dict(visual.facts)
            self._check_named_facts(state, facts)
            # The model may spend seconds on one crop.  Read telemetry again
            # after inference and bind the returned observation to that fresh
            # boot/sequence instead of resetting the timestamp to ``now``.
            after_sample = self._capture_native_frame(identity, decoder, deadline=deadline)
            if (
                after_sample.kind != "FRESH"
                or after_sample.frame is None
                or after_sample.frame.boot != frame.boot
                or after_sample.frame.sequence <= frame.sequence
                or after_sample.capture_monotonic <= sample.capture_monotonic
            ):
                continue
            self._last_addon_sequence = after_sample.frame.sequence
            self._last_addon_boot = after_sample.frame.boot
            # Keep the named-observation sequence in one monotonic namespace
            # across addon-backed and model-backed scenes.  A later visual
            # postcondition must not appear older merely because it uses the
            # model capture path.
            self._named_capture_sequence = max(self._named_capture_sequence, after_sample.frame.sequence)
            return NamedStateObservation.from_facts(
                state=state,
                identity_fingerprint=identity.fingerprint,
                target_xid=identity.child_xid,
                capture_id=(
                    f"addon+visual:{after_sample.frame.boot}:{after_sample.frame.sequence}:"
                    f"{after_sample.frame.crc32:08x}:"
                    f"{hashlib.sha256(visual_image).hexdigest()}"
                ),
                capture_sequence=after_sample.frame.sequence,
                facts=facts,
                source="native-addon",
                observed_monotonic=after_sample.capture_monotonic,
                boot=after_sample.frame.boot,
            )
        raise DriverError(f"fresh addon observation for {state} did not arrive")

    def _observe_named_state(self, identity: WindowIdentity, state: str) -> NamedStateObservation:
        if state in {"world", "auction_house_open"}:
            return self._observe_addon_state(identity, state)
        named_deadline = getattr(self, "_named_observation_deadline", None)
        deadline = min(
            time.monotonic() + 5.0,
            named_deadline if isinstance(named_deadline, (int, float)) else float("inf"),
        )
        capture_started = time.monotonic()
        image = self._capture_model(deadline=deadline)
        # Keep freshness anchored to the start of the native crop capture;
        # using the return time would make a slow capture appear newer than
        # the evidence admitted at its beginning.  Deadline accounting still
        # uses the actual return clock below.
        captured_at = capture_started
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DriverError("visual named-state capture exceeded its bounded deadline")
        observation = self._observe_model(
            image,
            timeout=remaining,
        )
        if time.monotonic() > deadline:
            raise DriverError("visual named-state inference exceeded the freshness deadline")
        current = self._resolve_identity(deadline=deadline)
        if current.fingerprint != identity.fingerprint:
            raise DriverError("client identity changed during named-state inference")
        if observation.state != state:
            raise DriverError(f"fresh visual observation reported {observation.state!r}, expected {state!r}")
        self._check_named_facts(state, observation.facts)
        self._named_capture_sequence += 1
        return NamedStateObservation.from_facts(
            state=state,
            identity_fingerprint=identity.fingerprint,
            target_xid=identity.child_xid,
            capture_id=(
                f"visual:{self.capture_index}:{self._named_capture_sequence}:"
                f"{hashlib.sha256(image).hexdigest()}"
            ),
            capture_sequence=self._named_capture_sequence,
            facts=observation.facts,
            source="native-visual",
            observed_monotonic=captured_at,
            boot="",
        )

    def _resolve_identity(
        self,
        *,
        require_foreground: bool = True,
        deadline: float | None = None,
    ) -> WindowIdentity:
        """Resolve the live client identity under the caller's deadline.

        The production driver exposes the deadline on identity resolution so
        its X11 queries cannot outlive a motion or transition intent.  Calls
        outside a bounded operation retain the driver's ordinary startup
        behavior by omitting the optional capability.
        """

        if deadline is None:
            return self.driver.resolve_identity(require_foreground=require_foreground)
        return self.driver.resolve_identity(
            require_foreground=require_foreground,
            deadline_monotonic=deadline,
        )

    def _capture_client_crop(
        self,
        path: Path,
        *,
        model: bool = False,
        deadline: float | None = None,
    ) -> Path:
        """Capture one client crop while preserving an absolute deadline."""

        if deadline is None:
            return self.driver.capture_client_crop(path, model=model)
        return self.driver.capture_client_crop(
            path,
            model=model,
            deadline_monotonic=deadline,
        )

    def _capture_client_crop_for_identity(
        self,
        identity: WindowIdentity,
        *,
        deadline: float | None = None,
    ) -> Path:
        """Capture an identity-bound crop with the same bounded capability."""

        if deadline is None:
            return self.driver.capture_client_crop_for_identity(identity)
        return self.driver.capture_client_crop_for_identity(
            identity,
            deadline_monotonic=deadline,
        )

    def _native_region_enabled(self) -> bool:
        profile = getattr(getattr(self, "config", None), "profile", None)
        if profile is None:
            return False
        executable = getattr(profile, "telemetry_region_executable", None)
        digest = getattr(profile, "telemetry_region_executable_sha256", None)
        return executable is not None and digest is not None

    def _passive_region_capability_matches_locked(
        self,
        capability: _PassiveRegionCaptureCapability | None,
        identity: WindowIdentity,
        *,
        generation: int,
        state: ControllerState,
        deadline: float | None,
    ) -> bool:
        """Check one live passive operation while holding construction lock."""

        if capability is None or capability is not self._passive_region_admission:
            return False
        if capability.epoch != self._passive_region_epoch:
            return False
        if capability.generation != generation or capability.state is not state:
            return False
        if (
            capability.identity_fingerprint != identity.fingerprint
            or capability.process_start != identity.process_start
            or capability.deadline_monotonic != deadline
            or time.monotonic() >= capability.deadline_monotonic
        ):
            return False
        shutdown = getattr(self, "_shutdown_event", None)
        if shutdown is not None and shutdown.is_set():
            return False
        guardian = self.driver.guardian
        if guardian.generation != capability.guardian_generation:
            return False
        monitor = capability.observer
        if (
            getattr(self.driver, "_monitor", None) is not monitor
            or not getattr(monitor, "alive", False)
            or getattr(monitor, "failure_diagnostic", None) is not None
        ):
            return False
        process = getattr(getattr(monitor, "observer", None), "process", None)
        if getattr(process, "pid", None) != capability.observer_pid:
            return False
        if capability.observer_pid not in getattr(guardian, "observer_process_groups", ()):
            return False
        return True

    def _capture_native_region_for_identity(
        self,
        identity: WindowIdentity,
        *,
        destination: Path | None = None,
        deadline: float | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | None = None,
        native_capability: dict[str, Any] | _NativeUpdateCapability | None = None,
        passive_capability: _PassiveRegionCaptureCapability | None = None,
        native_update_ordinal: int | None = None,
        _receipt_sink: Callable[[Any], None] | None = None,
    ) -> Path:
        with self.controller.lock:
            admitted_status = self.controller.status()
            required_state = expected_state or admitted_status.state
            if passive_capability is not None and native_capability is not None:
                raise DriverError("passive region admission cannot borrow a native recovery capability")
            if expected_generation is not None and admitted_status.generation != expected_generation:
                raise DriverError("controller generation does not admit a native region capture")
            if expected_state is not None and admitted_status.state is not expected_state:
                raise DriverError("controller state does not admit a native region capture")
            if (
                required_state in {ControllerState.BLOCKED, ControllerState.STOPPED}
                and passive_capability is None
            ):
                raise DriverError("controller state does not admit a native region capture")
            if admitted_status.stop_requested:
                raise DriverError("controller stop request does not admit a native region capture")
            admitted_generation = admitted_status.generation
            admitted_state = required_state

        if not self._native_region_enabled():
            if admitted_state is ControllerState.PAUSED:
                raise DriverError("PAUSED native construction requires a configured telemetry producer")
            # Preserve the legacy NATIVE_CALIBRATION stock-capture path after
            # controller admission has already fenced generation/state.
            return self._capture_client_crop_for_identity(identity, deadline=deadline)
        if not isinstance(self.driver.backend, X11Backend):
            raise DriverError("native region capture requires the configured X11 backend")

        if passive_capability is not None:
            if admitted_state not in {
                ControllerState.STOPPED,
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
            }:
                raise DriverError(
                    "passive region admission is limited to stopped, paused, or blocked startup"
                )
            with self._native_construction_lock:
                if not self._passive_region_capability_matches_locked(
                    passive_capability,
                    identity,
                    generation=admitted_generation,
                    state=admitted_state,
                    deadline=deadline,
                ):
                    raise DriverError("passive native region capture capability was revoked")
        elif admitted_state is ControllerState.PAUSED:
            # A generic status/model capture in PAUSED is closed.  Only the
            # exact generation-bound provisional/recovery owner may use the
            # configured telemetry-region producer here.
            with self._native_construction_lock:
                if native_capability is None or not self._native_construction_capability_matches(
                    native_capability
                ):
                    raise DriverError("PAUSED native region capture requires a live recovery capability")
        elif isinstance(native_capability, _NativeUpdateCapability):
            # Update optimization carries a stronger private proof than the
            # legacy recovery dict.  Check it before producer selection and
            # again at the observer linearization point below.
            with self._native_construction_lock:
                if not self._native_update_capability_matches_locked(native_capability, identity):
                    raise DriverError("native update identity capability was revoked")
        elif native_capability is not None:
            # Deferred finalization also carries an exact owner capability.
            # Recheck it before selecting the producer and again at the
            # Guardian execution gate so a same-generation cancellation
            # cannot start a later capture after revocation.
            with self._native_construction_lock:
                if not self._native_construction_capability_matches(native_capability):
                    raise DriverError("native region capture capability was revoked")

        guardian = self.driver.guardian
        admitted_process_pid: int | None = None

        def admit_observer(start: Callable[[], Any]) -> Any:
            nonlocal admitted_process_pid
            # This is the linearization point: the controller state and its
            # generation remain stable until Guardian registers the gated
            # process and releases its nonblocking exec gate.
            with self.controller.lock:
                current = self.controller.status()
                if (
                    current.generation != admitted_generation
                    or current.state is not admitted_state
                    or current.stop_requested
                ):
                    raise DriverError("controller generation or state changed before observer admission")
                # Controller -> construction -> Guardian is the sole
                # capability transaction.  Revocation/expiry mutates the
                # owner under construction lock, so keep that lock through
                # the actual Guardian process admission.
                with self._native_construction_lock:
                    if passive_capability is not None:
                        if not self._passive_region_capability_matches_locked(
                            passive_capability,
                            identity,
                            generation=admitted_generation,
                            state=admitted_state,
                            deadline=deadline,
                        ):
                            raise DriverError("passive native region capture capability was revoked")
                    elif isinstance(native_capability, _NativeUpdateCapability):
                        if not self._native_update_capability_matches_locked(native_capability, identity):
                            raise DriverError("native update identity capability was revoked")
                    elif native_capability is not None and not self._native_construction_capability_matches(
                        native_capability
                    ):
                        raise DriverError("native region capture capability was revoked")

                    def start_with_guardian_generation() -> Any:
                        # observer_dispatch_admission invokes this callback
                        # while holding the real Guardian lock.  This is the
                        # final pre-Popen generation fence; an earlier sample
                        # would leave a revoke-first race at the callback.
                        expected_guardian_generation = (
                            passive_capability.guardian_generation
                            if passive_capability is not None
                            else native_capability.guardian_generation
                            if isinstance(native_capability, _NativeUpdateCapability)
                            else native_capability.get("guardianGeneration")
                            if native_capability is not None
                            else None
                        )
                        if (
                            native_capability is not None or passive_capability is not None
                        ) and guardian.generation != expected_guardian_generation:
                            raise DriverError("native region capture Guardian generation is stale")
                        return start()

                    admitted = guardian.observer_dispatch_admission(start_with_guardian_generation)
                    admitted_process = getattr(getattr(admitted, "observer", admitted), "process", None)
                    admitted_process_pid = getattr(admitted_process, "pid", None)
                    return admitted

        def observer_reaped(process_group: int) -> None:
            guardian.unregister_observer_process(process_group)

        capture_kwargs: dict[str, Any] = {
            "destination": destination,
            "observer_admission": admit_observer,
            "observer_reaped": observer_reaped,
        }
        if isinstance(native_capability, _NativeUpdateCapability) and (
            isinstance(native_update_ordinal, int)
            and not isinstance(native_update_ordinal, bool)
            and 2 <= native_update_ordinal <= 99
        ):

            def pre_ack_admission(producer_pid: int, dispatch_ack_once: Callable[[], None]) -> None:
                if deadline is None:
                    raise DriverError("native pre-ACK deadline is missing")
                self._native_update_alignment_pre_ack(
                    identity=identity,
                    generation=admitted_generation,
                    capability=native_capability,
                    ordinal=native_update_ordinal,
                    producer_pid=producer_pid,
                    dispatch_ack_once=dispatch_ack_once,
                    deadline=deadline,
                    admitted_pid=admitted_process_pid,
                )

            capture_kwargs["_pre_ack_admission"] = pre_ack_admission
        if deadline is not None:
            capture_kwargs["deadline_monotonic"] = deadline
        if _receipt_sink is not None and native_capability is not None:
            capture_kwargs["_receipt_sink"] = _receipt_sink
        return self.driver.capture_native_region_for_identity(identity, **capture_kwargs)

    def _read_command_buffer(
        self,
        *,
        previous: Any | None = None,
        frame_sink: Callable[[Any], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> str:
        # The addon publishes the native EditBox projection on its one-Hz
        # telemetry tick.  A command may be typed immediately after the last
        # frame, so consume only a fresh post-type frame within a bounded
        # interval; a duplicate pre-type frame can never prove the buffer.
        now = time.monotonic()
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(float(deadline_monotonic))
            ):
                raise DriverError("command readback deadline is malformed")
            deadline = min(now + 3.0, float(deadline_monotonic))
        else:
            deadline = now + 3.0
        if deadline <= now:
            raise DriverError("command readback deadline expired")
        while time.monotonic() < deadline:
            sample = self.capture_status(deadline=deadline)
            if sample.kind == "FRESH" and sample.frame is not None:
                frame = sample.frame
                if previous is not None:
                    if frame.boot != previous.boot:
                        raise DriverError("client boot changed during command EditBox readback")
                    try:
                        delta = unsigned_delta(frame.sequence, previous.sequence)
                    except ProtocolError as exc:
                        raise DriverError("command EditBox readback sequence is malformed") from exc
                    if delta == 0:
                        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                        continue
                snapshot = frame.snapshot.value
                render = snapshot.get("render")
                if (
                    not isinstance(render, dict)
                    or render.get("nativePhase") != "normal"
                    or isinstance(render.get("nativeOrdinal"), bool)
                    or not isinstance(render.get("nativeOrdinal"), int)
                    or render.get("nativeOrdinal") != 0
                ):
                    raise DriverError("command EditBox readback requires an ordinary native frame")
                value = snapshot.get("chatEdit")
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise DriverError("command readback omitted the active EditBox text")
                if frame_sink is not None:
                    frame_sink(frame)
                return value["text"]
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise DriverError("command readback did not contain a fresh frame")

    def _command(self, snapshot: dict[str, Any], command: str) -> bool:
        edit = snapshot.get("chatEdit", {})
        with self.controller.lock:
            controller_status = self.controller.status()
            if controller_status.state in {
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
                ControllerState.STOPPED,
            }:
                return False
            if controller_status.stop_requested:
                return False
            if controller_status.mode.value == "interaction":
                raise DriverError("engine action is revoked by interaction mode")
            if command == "/crowley scan" and controller_status.closeout_requested:
                return False
            action = self._command_action_kind(command, controller_status.state)
            deadline = self._action_deadline(action)
            self.controller.set_action_intent(action, deadline_seconds=deadline)
            # Preparation is itself a durable uncertainty boundary. If the
            # owner pauses or crashes while opening the chat/readback path,
            # reconciliation retains the exact intent and never replays it.
            self.controller.action_result("preparing")
            controller_generation = controller_status.generation
        generation = self.driver.guardian.generation
        self._last_action_generation = generation
        expected_identity = self.driver.resolve_identity().fingerprint

        def dispatch_allowed() -> None:
            with self.controller.lock:
                current = self.controller.status()
                if current.generation != controller_generation or current.state in {
                    ControllerState.PAUSED,
                    ControllerState.BLOCKED,
                    ControllerState.STOPPED,
                }:
                    raise DriverError("command dispatch was revoked")
                if current.mode.value == "interaction":
                    raise DriverError("command dispatch was revoked by interaction mode")
                if current.action_intent != action:
                    raise DriverError("command dispatch intent was replaced")

        def identity_guard(identity: Any) -> bool:
            if identity.fingerprint != expected_identity:
                return False
            if command == "/crowley scan" and snapshot.get("ahOpen") is not True:
                return False
            if command == "/logout":
                context = snapshot.get("context", {})
                if (
                    context.get("character") != self.config.profile.character
                    or context.get("realm") != self.config.profile.realm
                    or snapshot.get("pending", {}).get("ids", [])
                ):
                    return False
                if not self._logout_scanner_idle(snapshot, controller_status.last_uid):
                    return False
            if command == "/crowley session fence" and not self._scanner_idle_ready(
                snapshot, controller_status.last_uid
            ):
                return False
            return True

        if edit.get("active") and edit.get("focused") and edit.get("text") == "":
            if command == "/logout" and isinstance(self.driver.backend, X11Backend):
                # Pair the durable logout command with a named world
                # observation.  The character-select postcondition is checked
                # after the asynchronous scene transition in cleanup wait.
                self.driver.verify_named_action("logout", "before")
            dispatch_allowed()
            self.driver.prepare_command(
                generation,
                command,
                snapshot,
                readback=self._read_command_buffer,
                precondition=identity_guard,
                postcondition=identity_guard,
            )
            with self.controller.lock:
                if self.controller.status().generation != controller_generation:
                    raise DriverError("command dispatch was revoked")
                self.controller.action_result("dispatched")
            return True
        if not edit.get("active"):
            dispatch_allowed()
            self.driver.run(
                "open_chat",
                generation,
                precondition=identity_guard,
                postcondition=identity_guard,
            )
            with self.controller.lock:
                if self.controller.status().generation == controller_generation:
                    self.controller.action_result("chat_open")
            return False
        self.driver.revoke("unexpected active chat buffer")
        raise DriverError("unexpected active chat buffer; input is revoked")

    def capture_status(
        self,
        *,
        deadline: float | None = None,
        _passive_region_capability: _PassiveRegionCaptureCapability | None = None,
    ) -> Sample:
        self.config.capture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.capture_index += 1
        source = self.config.capture_dir / f"status-{self.capture_index:08d}.png"
        if deadline is None:
            deadline_local = getattr(self, "_motion_deadline_local", None)
            deadline = getattr(deadline_local, "deadline", None) if deadline_local is not None else None
        expected = self._resolve_identity(deadline=deadline)
        if self._native_region_enabled() and _passive_region_capability is not None:
            crop = self._capture_native_region_for_identity(
                expected,
                destination=source,
                deadline=deadline,
                expected_generation=_passive_region_capability.generation,
                expected_state=_passive_region_capability.state,
                passive_capability=_passive_region_capability,
            )
        elif self._native_region_enabled():
            crop = self._capture_native_region_for_identity(expected, destination=source, deadline=deadline)
        else:
            crop = self._capture_client_crop(source, deadline=deadline)
        try:
            from PIL import Image

            identity = self._resolve_identity(deadline=deadline)
            if deadline is not None:
                self._remaining_motion_budget(deadline)
            if identity.fingerprint != expected.fingerprint:
                raise DriverError("status capture identity changed after crop")
            with Image.open(crop) as image:
                sample = self.decoder.decode(
                    image, monotonic=time.monotonic(), window_identity=identity.fingerprint
                )
                if deadline is not None:
                    self._remaining_motion_budget(deadline)
                if sample.kind == "FRESH" and sample.frame is not None:
                    self._last_addon_sequence = sample.frame.sequence
                    self._last_addon_boot = sample.frame.boot
                    self._motion_crs[(sample.frame.boot, sample.frame.sequence, identity.fingerprint)] = int(
                        sample.frame.crc32
                    )
                    while len(self._motion_crs) > 256:
                        self._motion_crs.pop(next(iter(self._motion_crs)))
                    snapshot = copy.deepcopy(sample.frame.snapshot.value)
                    self._last_motion_snapshot = snapshot
                    key = (sample.frame.boot, sample.frame.sequence, sample.window_identity)
                    snapshots = getattr(self, "_motion_snapshots", None)
                    if snapshots is None:
                        snapshots = self._motion_snapshots = {}
                    snapshots[key] = snapshot
                    raw_bytes = getattr(sample.frame, "raw_bytes", b"")
                    if isinstance(raw_bytes, bytes) and len(raw_bytes) == protocol.CAPACITY:
                        self._motion_evidence[key] = bytes(raw_bytes)
                    while len(snapshots) > 256:
                        snapshots.pop(next(iter(snapshots)))
                    while len(self._motion_evidence) > 256:
                        self._motion_evidence.pop(next(iter(self._motion_evidence)))
                return sample
        finally:
            for target in (source, crop):
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass

    def _motion_frame_from_sample(self, sample: Sample, *, deadline: float | None = None) -> MotionFrame:
        """Bind a public decoded sample to the existing validated motion path."""

        if sample.kind != "FRESH" or sample.frame is None:
            raise DriverError("mail completion requires a fresh decoded CRS frame")
        identity = self._resolve_identity(require_foreground=True, deadline=deadline)
        if sample.window_identity != identity.fingerprint:
            raise DriverError("mail completion frame identity changed after capture")
        try:
            frame = MotionFrame.from_sample(
                sample,
                identity,
                capture_started_monotonic=sample.capture_monotonic,
            )
            self._require_native_gate(frame)
            frame.require_safe_successor(None, now=self._native_clock())
            return frame
        except MotionError as exc:
            self._invalidate_motion_admission()
            raise DriverError(str(exc)) from exc

    def passive_startup(self) -> RuntimeInputEvidence:
        """Collect fresh read-only readiness without using the input lease."""

        if not self._startup_lock.acquire(blocking=False):
            raise DriverError("passive startup check is already running")
        monitor = None
        passive_capability = None
        try:
            self._startup_operation = {
                "state": "checking",
                "checkedAt": int(time.time()),
                "advancingFrames": 0,
                "failure": "",
            }
            with self.controller.lock:
                entry_status = self.controller.status()
                if entry_status.state not in {
                    ControllerState.STOPPED,
                    ControllerState.PAUSED,
                    ControllerState.BLOCKED,
                }:
                    raise DriverError("passive startup is limited to stopped, paused, or blocked states")
                if entry_status.stop_requested:
                    raise DriverError("passive startup is cancelled by the controller stop request")
                entry_generation = entry_status.generation
                entry_state = entry_status.state
            identity = self.driver.resolve_identity(require_foreground=True)
            monitor = self.driver.start_input_observer()
            if not monitor.alive or monitor.failure_diagnostic is not None:
                raise DriverError("registered XI2 observer is not healthy")
            deadline = time.monotonic() + PASSIVE_STARTUP_TIMEOUT_SECONDS
            observer_process = getattr(getattr(monitor, "observer", None), "process", None)
            observer_pid = getattr(observer_process, "pid", None)
            if isinstance(observer_pid, bool) or not isinstance(observer_pid, int) or observer_pid <= 0:
                raise DriverError("passive startup observer has no registered process identity")
            with self.controller.lock:
                current_status = self.controller.status()
                if (
                    current_status.generation != entry_generation
                    or current_status.state is not entry_state
                    or current_status.stop_requested
                ):
                    raise DriverError("controller state changed before passive startup capture admission")
                with self._native_construction_lock:
                    self._passive_region_epoch += 1
                    passive_capability = _PassiveRegionCaptureCapability(
                        epoch=self._passive_region_epoch,
                        generation=entry_generation,
                        state=entry_state,
                        identity_fingerprint=identity.fingerprint,
                        process_start=identity.process_start,
                        guardian_generation=self.driver.guardian.generation,
                        observer_pid=observer_pid,
                        deadline_monotonic=deadline,
                        observer=monitor,
                    )
                    self._passive_region_admission = passive_capability
                    if not self._passive_region_capability_matches_locked(
                        passive_capability,
                        identity,
                        generation=entry_generation,
                        state=entry_state,
                        deadline=deadline,
                    ):
                        self._passive_region_admission = None
                        self._passive_region_epoch += 1
                        passive_capability = None
                        raise DriverError("passive startup observer ownership is not current")
            last_boot = ""
            last_sequence: int | None = None
            advancing = 0
            while advancing < 3:
                if time.monotonic() >= deadline:
                    raise DriverError("passive startup did not observe three advancing CRS1 frames")
                if self._shutdown_event.is_set():
                    raise DriverError("passive startup was cancelled by daemon shutdown")
                if not monitor.alive or monitor.failure_diagnostic is not None:
                    raise DriverError("registered XI2 observer died during passive startup")
                with self.controller.lock:
                    current_status = self.controller.status()
                    if (
                        current_status.generation != entry_generation
                        or current_status.state is not entry_state
                        or current_status.stop_requested
                    ):
                        raise DriverError("controller state changed during passive startup")
                sample = self.capture_status(
                    deadline=deadline,
                    _passive_region_capability=passive_capability,
                )
                with self.controller.lock:
                    current_status = self.controller.status()
                    if (
                        current_status.generation != entry_generation
                        or current_status.state is not entry_state
                        or current_status.stop_requested
                    ):
                        raise DriverError("controller state changed during passive startup capture")
                if sample.window_identity != identity.fingerprint:
                    raise DriverError("status telemetry identity changed during passive startup")
                if sample.kind == "NO_CHANGE":
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                    continue
                if sample.kind != "FRESH" or sample.frame is None:
                    raise DriverError("passive startup status capture was not a valid advancing CRS1 frame")
                frame = sample.frame
                context = frame.snapshot.value.get("context")
                if (
                    not isinstance(context, dict)
                    or context.get("character") != self.config.profile.character
                    or context.get("realm") != self.config.profile.realm
                ):
                    raise DriverError("passive startup did not observe the configured character in world")
                render = frame.snapshot.value.get("render")
                native_ordinal = render.get("nativeOrdinal") if isinstance(render, dict) else None
                if (
                    not isinstance(render, dict)
                    or render.get("nativePhase") != "normal"
                    or isinstance(native_ordinal, bool)
                    or not isinstance(native_ordinal, int)
                    or native_ordinal != 0
                ):
                    raise DriverError("passive startup frame is not ordinary nativePhase=normal ordinal=0")
                if last_boot and frame.boot != last_boot:
                    raise DriverError("client boot changed during passive startup")
                if last_sequence is not None:
                    try:
                        delta = unsigned_delta(frame.sequence, last_sequence)
                    except ProtocolError as exc:
                        raise DriverError("passive startup sequence order is malformed") from exc
                    if delta == 0:
                        raise DriverError("passive startup decoder marked a duplicate sequence as fresh")
                last_boot = frame.boot
                last_sequence = frame.sequence
                advancing += 1
                self._startup_operation["advancingFrames"] = advancing
            current = self._resolve_identity(require_foreground=True, deadline=deadline)
            with self.controller.lock:
                current_status = self.controller.status()
                if (
                    current_status.generation != entry_generation
                    or current_status.state is not entry_state
                    or current_status.stop_requested
                ):
                    raise DriverError("controller state changed at passive startup completion")
                with self._native_construction_lock:
                    if not self._passive_region_capability_matches_locked(
                        passive_capability,
                        identity,
                        generation=entry_generation,
                        state=entry_state,
                        deadline=deadline,
                    ):
                        raise DriverError("passive startup observer ownership changed at completion")
            if current.fingerprint != identity.fingerprint or current.process_start != identity.process_start:
                raise DriverError("game identity changed at passive startup completion")
            # Bind the fresh input proof to the final ordinary advancing
            # publication that completed readiness.  A seal minted before
            # these checks would not carry any real capture provenance.
            evidence = self.driver.mint_runtime_input_evidence(
                identity,
                readiness_boot=last_boot,
                readiness_sequence=last_sequence if last_sequence is not None else 0,
            )
            if time.monotonic() >= deadline:
                raise DriverError("passive startup deadline expired during read-only environment measurement")
            current = self._resolve_identity(require_foreground=True, deadline=deadline)
            if current.fingerprint != identity.fingerprint or current.process_start != identity.process_start:
                raise DriverError("game identity changed during passive startup environment measurement")
            with self.controller.lock:
                current_status = self.controller.status()
                if (
                    current_status.generation != entry_generation
                    or current_status.state is not entry_state
                    or current_status.stop_requested
                    or self._shutdown_event.is_set()
                ):
                    raise DriverError(
                        "controller state changed during passive startup environment measurement"
                    )
                with self._native_construction_lock:
                    if not self._passive_region_capability_matches_locked(
                        passive_capability,
                        identity,
                        generation=entry_generation,
                        state=entry_state,
                        deadline=deadline,
                    ):
                        raise DriverError(
                            "passive startup observer ownership changed during environment measurement"
                        )
            self._startup_evidence = evidence
            self._startup_operation = {
                "state": "ready",
                "checkedAt": int(time.time()),
                "advancingFrames": advancing,
                "environmentFingerprint": evidence.environment_fingerprint,
                "failure": "",
            }
            return evidence
        except Exception as exc:
            self._startup_evidence = None
            self._revoke_passive_region_capability(passive_capability)
            if monitor is not None and not self.driver.guardian.armed:
                stop_observer = getattr(self.driver, "stop_input_observer", None)
                if callable(stop_observer):
                    try:
                        stop_observer(monitor)
                    except Exception as cleanup_error:
                        _LOGGER.error(
                            "passive startup observer cleanup failed: %s",
                            _bounded_diagnostic_error(cleanup_error),
                        )
            self._startup_operation = {
                "state": "blocked",
                "checkedAt": int(time.time()),
                "advancingFrames": int(self._startup_operation.get("advancingFrames", 0)),
                "failure": _bounded_diagnostic_error(exc),
            }
            raise
        finally:
            self._revoke_passive_region_capability(passive_capability)
            self._startup_lock.release()

    def _revoke_passive_region_capability(self, capability: _PassiveRegionCaptureCapability | None) -> None:
        if capability is None:
            return
        with self._native_construction_lock:
            if self._passive_region_admission is capability:
                self._passive_region_admission = None
                self._passive_region_epoch += 1

    def runtime_start(
        self, *, expected_generation: int, mode: str = "repeat", resume: bool = False
    ) -> dict[str, Any]:
        """Use fresh passive readiness and only a matching persistent drill."""

        with self.controller.lock:
            status = self.controller.status()
            if status.generation != expected_generation:
                raise DriverError("stale generation")
            if status.state not in {ControllerState.STOPPED, ControllerState.PAUSED, ControllerState.BLOCKED}:
                raise DriverError(
                    f"runtime {'resume' if resume else 'start'} is not allowed in {status.state.value}"
                )
        # Validation mode is the explicit, opt-in native calibration run.
        # It remains separate from ordinary repeat startup, which uses only
        # passive readiness and a matching takeover drill.
        if not resume and mode == "validation":
            result = self.controller.start(mode, expected_generation=expected_generation)
            return self.status_dict() | {
                "controllerState": result.state.value,
                "nextAction": "native_calibration",
            }
        evidence = self.passive_startup()
        record = self.controller.takeover_drill_record
        if record is None or not record.matches(evidence.environment):
            self._startup_operation = {**self._startup_operation, "state": "takeover_needed"}
            self._takeover_drill_operation = {"state": "needed", "failure": ""}
            return self.status_dict() | {
                "startup": dict(self._startup_operation),
                "takeoverDrill": {
                    "state": "needed",
                    "environmentMatches": False,
                },
                "nextAction": "takeover_drill",
            }
        if resume:
            result = self.controller.resume_with_runtime_evidence(
                evidence, expected_generation=expected_generation
            )
        else:
            result = self.controller.start_with_runtime_evidence(
                evidence, mode, expected_generation=expected_generation
            )
        return self.status_dict() | {
            "startup": dict(self._startup_operation),
            "takeoverDrill": {"state": "passed", "environmentMatches": True},
            "nextAction": "reconcile",
            "controllerState": result.state.value,
        }

    def cancel_takeover_drill(self) -> dict[str, Any]:
        with self._takeover_cancel_lock:
            active = self._takeover_drill_operation.get("state") in {"running", "awaiting_input"}
            if active:
                self._takeover_cancel.set()
                self._takeover_drill_operation = {"state": "canceling", "failure": ""}
        if active and self.driver.guardian.armed:
            self.driver.revoke("takeover drill canceled")
        return self.status_dict()

    def run_takeover_drill(
        self,
        *,
        expected_generation: int,
        timeout: float = TAKEOVER_DRILL_HUMAN_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Run the explicit clickless hover, observe genuine input, then persist."""

        if not self._takeover_drill_lock.acquire(blocking=False):
            raise DriverError("takeover drill is already running")
        monitor = None
        armed_generation: int | None = None
        drill_started_ns = 0
        drill_started_wall_ns = time.time_ns()
        completed_hover_ns = 0
        awaiting_input_ns = 0
        runtime_evidence: RuntimeInputEvidence | None = None
        hover_failure_context: dict[str, Any] | None = None
        cleanup_pending = bool(self._takeover_drill_operation.get("cleanupPending", False))
        with self._takeover_cancel_lock:
            self._takeover_cancel.clear()
            self._takeover_event = None
            self._takeover_drill_operation = {
                "state": "running",
                "failure": "",
                "cleanupPending": cleanup_pending,
            }
        drill_generation: int | None = None

        def require_drill_generation(stage: str) -> None:
            status = self.controller.status()
            if drill_generation is None or status.generation != drill_generation:
                raise DriverError(f"takeover drill controller generation changed before {stage}")
            if status.state != ControllerState.PAUSED:
                raise DriverError(f"takeover drill controller left PAUSED before {stage}")
            if status.stop_requested:
                raise DriverError(f"takeover drill stop was requested before {stage}")
            if status.action_intent and status.action_intent not in {"", "stop"}:
                raise DriverError(f"takeover drill found an unresolved action before {stage}")
            if self._takeover_cancel.is_set():
                raise DriverError(f"takeover drill was canceled before {stage}")

        try:
            if cleanup_pending:
                pending_monitor = getattr(self.driver, "_monitor", None)
                if pending_monitor is not None:
                    try:
                        self.driver.stop_input_observer(pending_monitor)
                    except Exception as exc:
                        raise DriverError(
                            "previous takeover drill cleanup remains unresolved: "
                            + _bounded_diagnostic_error(exc)
                        ) from exc
                if self.driver.guardian.armed:
                    self.driver.revoke("retry unresolved takeover drill cleanup")
                if (
                    getattr(self.driver, "_monitor", None) is not None
                    or self.driver.guardian.armed
                    or self.driver.guardian.held
                    or self.driver.guardian.active_process_groups
                    or self.driver.guardian.observer_process_groups
                ):
                    raise DriverError("previous takeover drill cleanup remains unresolved")
                self._takeover_drill_operation["cleanupPending"] = False
            with self.controller.lock:
                status = self.controller.status()
                if status.generation != expected_generation:
                    raise DriverError("stale generation")
                if status.state not in {ControllerState.STOPPED, ControllerState.PAUSED}:
                    raise DriverError("takeover drill requires a stopped or paused controller")
                if status.stop_requested:
                    raise DriverError("takeover drill is refused while stop is requested")
                if status.action_intent and status.action_intent not in {"", "stop"}:
                    raise DriverError("takeover drill is refused while an external action is unresolved")
            if status.state == ControllerState.STOPPED:
                status = self.controller.pause("takeover drill preparation")
            drill_generation = status.generation
            evidence = self.passive_startup()
            runtime_evidence = evidence
            if not isinstance(self.driver, DesktopDriver) or not isinstance(self.driver.backend, X11Backend):
                raise DriverError("takeover drill requires the owned native X11 driver")
            monitor = self.driver._monitor
            if monitor is None or not monitor.alive:
                raise DriverError("takeover drill requires the registered passive XI2 observer")
            with self.controller.lock:
                require_drill_generation("arm")
                with self._takeover_cancel_lock:
                    require_drill_generation("arm")
            # Native startup/arm can block on owned observers and X11 calls.
            # Reserve the exact generation above, but keep pause/stop RPCs
            # responsive while the driver performs that bounded work.
            armed_generation = self.driver.arm(evidence)
            drill_started_ns = time.monotonic_ns()
            with self.controller.lock:
                require_drill_generation("hover")
                with self._takeover_cancel_lock:
                    require_drill_generation("hover")
            # The hover receives the exact arm generation. A pause racing this
            # call revokes that generation; the production hover executor
            # checks Guardian admission before every emitted pointer action.
            try:
                self.driver.run_takeover_hover(armed_generation, evidence)
            except Exception as exc:
                retained = getattr(self.driver, "_last_takeover_hover_failure_context", None)
                hover_failure_context = copy.deepcopy(retained) if isinstance(retained, dict) else {}
                hover_failure_context["drill"] = {
                    "controllerGeneration": drill_generation,
                    "armedGeneration": armed_generation,
                    "startedWallTimeNs": drill_started_wall_ns,
                    "startedMonotonicNs": drill_started_ns,
                    "readinessBoot": evidence.readiness_boot,
                    "readinessSequence": evidence.readiness_sequence,
                    "environmentFingerprint": evidence.environment_fingerprint,
                }
                hover_failure_context["commandError"] = {
                    "type": type(exc).__name__,
                    "message": " ".join(str(exc).split())[:8192],
                }
                raise DriverError(self._takeover_hover_failure_summary(hover_failure_context, exc)) from exc
            completed_hover_ns = time.monotonic_ns()
            if not self.driver.guardian.armed or self.driver.guardian.generation != armed_generation:
                raise DriverError("takeover hover was revoked before the human observation window")

            def observe_human(event: Any) -> None:
                if self._takeover_event is None and is_measured_human_action(event, evidence.environment):
                    self._takeover_event = (event, time.monotonic_ns())

            with self.controller.lock:
                require_drill_generation("human observation")
                with self._takeover_cancel_lock:
                    require_drill_generation("human observation")
                    self.driver.set_takeover_input_callback(observe_human)
                    awaiting_input_ns = time.monotonic_ns()
                    self._takeover_drill_operation = {"state": "awaiting_input", "failure": ""}
            deadline = time.monotonic() + timeout
            while self._takeover_event is None:
                if self._takeover_cancel.is_set():
                    raise DriverError("takeover drill was canceled")
                if not self.driver.guardian.armed:
                    raise DriverError("takeover lease was revoked without a genuine human input sample")
                if not monitor.alive or monitor.failure_diagnostic is not None:
                    raise DriverError("takeover observer was lost before a human sample")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DriverError("takeover human input deadline expired")
                try:
                    monitor.wait_for_human(
                        lambda _events: self._takeover_event is not None, timeout=min(0.25, remaining)
                    )
                except DriverError as exc:
                    if str(exc) != "human input sample deadline expired":
                        raise
            event, observed_ns = self._takeover_event
            if not is_measured_human_action(event, evidence.environment):
                raise DriverError(
                    "takeover sample source is not a measured human device in the input hierarchy"
                )
            with self.controller.lock:
                require_drill_generation("takeover release")
            if self.driver.guardian.armed:
                self.driver.revoke("genuine non-XTEST takeover observed")
            if self.driver.guardian.armed or self.driver.guardian.held:
                raise DriverError("Guardian did not revoke and release the takeover lease")
            self.driver.set_takeover_input_callback(None)
            self.driver.stop_input_observer(monitor)
            if self.driver.guardian.active_process_groups or self.driver.guardian.observer_process_groups:
                raise DriverError("takeover drill left an owned process group unreaped")
            release_deadline = time.monotonic() + TAKEOVER_DRILL_RELEASE_TIMEOUT_SECONDS
            while time.monotonic() < release_deadline:
                native_state = self.driver.backend.observe_native_input_state(evidence.xtest_profile.root_xid)
                if not native_state[1] and not native_state[2]:
                    break
                time.sleep(0.05)
            else:
                raise DriverError("native key or button remained held after takeover release")
            release_completed_ns = time.monotonic_ns()
            release_latency_ms = max(
                0,
                min(120_000, int((release_completed_ns - observed_ns) / 1_000_000)),
            )
            human_wait_ms = max(
                0,
                min(120_000, int((observed_ns - awaiting_input_ns) / 1_000_000)),
            )
            record = TakeoverDrillRecord.create(evidence.environment_fingerprint, release_latency_ms)
            with self.controller.lock:
                require_drill_generation("PASS persistence")
                with self._takeover_cancel_lock:
                    require_drill_generation("PASS persistence")
                    self.controller.persist_takeover_drill(record)
                    self._startup_operation = {
                        **self._startup_operation,
                        "state": "ready",
                        "takeoverDrill": "passed",
                    }
                    self._takeover_drill_operation = {"state": "passed", "failure": ""}
            return self.status_dict() | {
                "startup": dict(self._startup_operation),
                "takeoverDrill": {
                    "state": "passed",
                    "environmentMatches": True,
                    "releaseLatencyMs": release_latency_ms,
                    "humanWaitMs": human_wait_ms,
                    "hoverDurationMs": max(0, int((completed_hover_ns - drill_started_ns) / 1_000_000)),
                },
                "nextAction": "resume",
            }
        except Exception as exc:
            with self._takeover_cancel_lock:
                self._takeover_drill_operation = {
                    "state": "canceled" if self._takeover_cancel.is_set() else "failed",
                    "failure": _bounded_diagnostic_error(exc),
                    "cleanupPending": bool(self._takeover_drill_operation.get("cleanupPending", False)),
                }
            raise
        finally:
            pending_exception = sys.exc_info()[1]
            cleanup_errors: list[str] = []
            operation_passed = self._takeover_drill_operation.get("state") == "passed"

            def record_cleanup_error(stage: str, error: Exception) -> None:
                cleanup_errors.append(f"{stage}: {_bounded_diagnostic_error(error)}")

            try:
                # The success path already cleared the callback, revoked the
                # lease, and stopped/reaped its observer before persisting PASS.
                # Avoid introducing a post-persistence cleanup failure there.
                if not operation_passed:
                    try:
                        self.driver.set_takeover_input_callback(None)
                    except Exception as exc:
                        record_cleanup_error("callback cleanup", exc)
                    try:
                        if self.driver.guardian.armed:
                            self.driver.revoke("takeover drill cleanup")
                    except Exception as exc:
                        record_cleanup_error("lease cleanup", exc)
                    current_monitor = getattr(self.driver, "_monitor", None)
                    if monitor is not None and current_monitor is monitor:
                        try:
                            self.driver.stop_input_observer(monitor)
                        except Exception as exc:
                            record_cleanup_error("observer cleanup", exc)

                current_monitor = getattr(self.driver, "_monitor", None)
                try:
                    owned_observers = self.driver.guardian.observer_process_groups
                    active_groups = self.driver.guardian.active_process_groups
                    lease_unresolved = self.driver.guardian.armed or self.driver.guardian.held
                except Exception as exc:
                    record_cleanup_error("ownership check", exc)
                    owned_observers = (True,)
                    active_groups = (True,)
                    lease_unresolved = True
                resources_unresolved = bool(
                    current_monitor is not None or owned_observers or active_groups or lease_unresolved
                )
                if resources_unresolved and not cleanup_errors:
                    record_cleanup_error(
                        "ownership check", DriverError("owned observer or input resources remain")
                    )

                if hover_failure_context is not None:
                    cleanup_diagnostic: dict[str, Any] = {
                        "errors": list(cleanup_errors),
                        "resourcesUnresolved": resources_unresolved,
                        "driverMonitorRegisteredAfterCleanup": current_monitor is not None,
                    }
                    try:
                        cleanup_diagnostic.update(
                            {
                                "guardianArmed": bool(self.driver.guardian.armed),
                                "guardianHeld": bool(self.driver.guardian.held),
                                "observerProcessGroups": sorted(self.driver.guardian.observer_process_groups),
                                "activeProcessGroups": sorted(self.driver.guardian.active_process_groups),
                            }
                        )
                    except Exception as diagnostic_error:
                        cleanup_diagnostic["ownershipSnapshotError"] = _bounded_diagnostic_error(
                            diagnostic_error
                        )
                    hover_failure_context["cleanup"] = cleanup_diagnostic
                    snapshot = getattr(monitor, "diagnostic_snapshot", None)
                    if callable(snapshot):
                        try:
                            hover_failure_context["observer"] = snapshot()
                            hover_failure_context["observerSnapshotTiming"] = "after_drill_cleanup"
                        except Exception as diagnostic_error:
                            hover_failure_context["observerSnapshotAfterCleanupError"] = (
                                _bounded_diagnostic_error(diagnostic_error)
                            )
                    if runtime_evidence is not None:
                        hover_failure_context["drill"]["readinessEnvironmentFingerprint"] = (
                            runtime_evidence.environment_fingerprint
                        )
                    try:
                        self._persist_takeover_hover_failure(hover_failure_context)
                    except Exception as diagnostic_error:
                        _LOGGER.warning(
                            "takeover hover failure diagnostic persistence failed: %s",
                            _bounded_diagnostic_error(diagnostic_error),
                        )

                with self._takeover_cancel_lock:
                    state = self._takeover_drill_operation.get("state", "failed")
                    failure = str(self._takeover_drill_operation.get("failure", ""))
                    if cleanup_errors:
                        cleanup_summary = "; ".join(cleanup_errors)
                        failure = (
                            f"{failure}; cleanup failed: {cleanup_summary}"
                            if failure
                            else f"cleanup failed: {cleanup_summary}"
                        )
                        if state == "passed":
                            state = "failed"
                        self._takeover_drill_operation = {
                            **self._takeover_drill_operation,
                            "state": state,
                            "failure": _bounded_diagnostic_error(DriverError(failure)),
                        }
                    elif state == "running":
                        self._takeover_drill_operation = {
                            **self._takeover_drill_operation,
                            "state": "failed",
                            "failure": "drill exited before evidence",
                        }
                    if resources_unresolved:
                        self._takeover_drill_operation["cleanupPending"] = True
                    elif not cleanup_errors:
                        self._takeover_drill_operation["cleanupPending"] = False
                    self._takeover_event = None

                if cleanup_errors and pending_exception is None:
                    raise DriverError("takeover drill cleanup failed: " + "; ".join(cleanup_errors))
            finally:
                # No exception from callback, revoke, observer stop, or status
                # bookkeeping may strand the single-flight drill lock/event.
                self._takeover_event = None
                self._takeover_drill_lock.release()

    def _capture_model(self, *, deadline: float | None = None) -> bytes:
        if deadline is not None:
            self._remaining_motion_budget(deadline)
        self.config.capture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.capture_index += 1
        path = self.config.capture_dir / f"recovery-{self.capture_index:08d}.png"
        cropped = self._capture_client_crop(path, model=True, deadline=deadline)
        try:
            value = cropped.read_bytes()
            if deadline is not None:
                self._remaining_motion_budget(deadline)
            return value
        finally:
            for target in (path, cropped):
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass

    def _model_call_snapshot(self) -> dict[str, int]:
        lock = getattr(self, "_model_counter_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._model_counter_lock = lock
        with lock:
            counters = getattr(self, "_model_call_counters", None)
            if counters is None:
                counters = {"started": 0, "succeeded": 0, "failed": 0}
                self._model_call_counters = counters
            return {key: int(counters[key]) for key in ("started", "succeeded", "failed")}

    def _call_model_adapter(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Count the real adapter call without holding the counter lock over it."""

        adapter = self.model
        method = getattr(adapter, method_name, None)
        if not callable(method):
            raise ModelError(f"model adapter has no {method_name} entrypoint")
        lock = getattr(self, "_model_counter_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._model_counter_lock = lock
        with lock:
            counters = getattr(self, "_model_call_counters", None)
            if counters is None:
                counters = {"started": 0, "succeeded": 0, "failed": 0}
                self._model_call_counters = counters
            counters["started"] += 1
        try:
            result = method(*args, **kwargs)
        except BaseException:
            with lock:
                counters["failed"] += 1
            raise
        else:
            with lock:
                counters["succeeded"] += 1
            return result

    def _runtime_source_binding_for_diagnostic(self) -> dict[str, str]:
        initial = getattr(self, "_runtime_source_binding", None)
        if not isinstance(initial, dict) or set(initial) != set(_RUNTIME_SOURCE_FILES):
            raise DriverError("paused region diagnostic source binding is unavailable")
        current = _runtime_source_hashes()
        if current != initial:
            raise DriverError("paused region diagnostic source binding drifted")
        return dict(current)

    def _observe_model(
        self,
        image: bytes,
        *,
        timeout: float,
        requested_state: str | None = None,
        requested_control: str | None = None,
    ) -> Any:
        """Run one visual model turn under a finite freshness budget."""

        kwargs: dict[str, Any] = {
            "target": "game-client",
            "verified_crop": self.config.profile.model_crop,
        }
        if requested_state is not None:
            kwargs["requested_state"] = requested_state
        if requested_control is not None:
            kwargs["requested_control"] = requested_control
        # The timeout is part of the visual-adapter contract.  Every provider,
        # including deterministic lifecycle fixtures, must receive the same
        # freshness bound; a type-based compatibility branch would silently
        # remove the deadline from an alternate production provider.
        kwargs["timeout"] = timeout
        return self._call_model_adapter("observe", image, **kwargs)

    def _require_interaction(self, generation: int, *, allow_executor: bool = False) -> int:
        deadline_local = getattr(self, "_motion_deadline_local", None)
        if deadline_local is not None and hasattr(deadline_local, "deadline"):
            if self._native_clock() >= deadline_local.deadline:
                raise DriverError("motion intent exceeded its bounded deadline")
        with self.controller.lock:
            status = self.controller.status()
            if status.generation != generation or status.mode.value != "interaction":
                raise DriverError("motion intent requires the current interaction generation")
            if status.state != ControllerState.READY:
                raise DriverError("motion intent requires READY controller state")
            lease = self.driver.guardian
            if status.lease_generation <= 0 or not lease.armed or lease.generation != status.lease_generation:
                raise DriverError("motion intent requires the current armed input lease")
            if status.action_intent:
                raise DriverError("another interaction intent is already in flight")
            cycle = self.controller.mail_cycle_checkpoint()
            if cycle.get("phase") not in {"none", "complete", "blocked"}:
                authority = getattr(getattr(self, "_mail_cycle_context", None), "cycle_id", "")
                if authority != cycle.get("cycleId"):
                    raise DriverError("mail cycle owns interaction admission")
            if not allow_executor and getattr(self.controller, "executor_action_active", False):
                raise DriverError("executor mail action owns the interaction fence")
            return status.lease_generation

    def _require_motion_interaction(self, generation: int, *, allow_executor: bool = False) -> int:
        """Acquire motion interaction authority and invalidate on setup failure."""

        try:
            return self._require_interaction(generation, allow_executor=allow_executor)
        except Exception:
            self._invalidate_motion_admission()
            raise

    def _motion_deadline(self, *, limit: float | None = None) -> float:
        """Return the active intent deadline for a native/model helper.

        Serialized motion owns one absolute deadline across every capture,
        model call, physical action, and feedback wait.  A direct helper call
        outside an intent still receives a short bounded fallback.
        """

        local = getattr(self, "_motion_deadline_local", None)
        absolute = getattr(local, "deadline", None) if local is not None else None
        try:
            now = self._native_clock()
        except Exception:
            # Once an intent has entered the motion admission boundary, a
            # clock failure makes its existing visual proofs unusable.  The
            # serialized wrapper deliberately leaves ordinary method errors
            # alone; this helper is an explicit deadline boundary.
            self._invalidate_motion_admission()
            raise
        if absolute is not None and limit is None:
            deadline = float(absolute)
        else:
            helper_limit = 5.0 if limit is None else float(limit)
            deadline = now + helper_limit if absolute is None else min(absolute, now + helper_limit)
        if deadline <= now:
            raise DriverError("motion intent exceeded its bounded deadline")
        return deadline

    def _remaining_motion_budget(self, deadline: float) -> float:
        try:
            remaining = float(deadline) - self._native_clock()
        except Exception:
            raise
        if not math.isfinite(remaining) or remaining <= 0:
            raise DriverError("motion intent exceeded its bounded deadline")
        return remaining

    def _world_forward_heading(self, frame: MotionFrame) -> float:
        basis = getattr(self, "movement_basis", None)
        if basis is None or basis.facing_sign not in {-1, 1}:
            raise DriverError("movement coordinate basis has not been calibrated")
        return normalize_angle(basis.facing_sign * frame.facing + basis.forward_heading_offset)

    def _world_left_heading(self, frame: MotionFrame) -> float:
        basis = getattr(self, "movement_basis", None)
        if basis is None or basis.handedness not in {-1, 1} or basis.strafe_heading_offset is None:
            raise DriverError("movement left basis has not been calibrated")
        return normalize_angle(
            self._world_forward_heading(frame) + basis.handedness * math.pi / 2 + basis.strafe_heading_offset
        )

    @staticmethod
    def _require_supported_route_handedness(basis: MovementBasis | int) -> None:
        """Require the route model's documented character-left convention.

        Route prompts describe ``+pi/2`` as character-left.  A reflected
        native coordinate basis would require a different prompt contract;
        accepting it here would silently send the model a corridor with the
        opposite meaning.  Refuse before sealing or consuming such a basis.
        """

        handedness = basis.handedness if isinstance(basis, MovementBasis) else basis
        if handedness != 1:
            raise DriverError("movement route requires a positively handed coordinate basis")

    @staticmethod
    def _travel_envelope(planned_distance: float) -> float:
        """Bound planned movement plus proportional calibration uncertainty."""

        if not math.isfinite(planned_distance) or planned_distance <= 0.0:
            raise DriverError("planned movement distance is malformed")
        uncertainty = max(MIN_BASIS_DISPLACEMENT, planned_distance * TRAVEL_UNCERTAINTY_FRACTION)
        envelope = planned_distance + uncertainty
        if not math.isfinite(envelope):
            raise DriverError("planned movement envelope is malformed")
        return envelope

    def _measured_motion_speed(self, role: KeyRole) -> float:
        basis = getattr(self, "movement_basis", None)
        if basis is None:
            raise DriverError("motion pulse requires measured movement basis")
        if role in {KeyRole.MOVE_FORWARD, KeyRole.MOVE_BACKWARD}:
            if role is KeyRole.MOVE_FORWARD:
                displacement = basis.forward_displacement
                pulse = basis.pulse_seconds
            else:
                displacement = basis.backward_displacement
                pulse = basis.backward_pulse_seconds
                if displacement is None or pulse is None:
                    raise DriverError("movement basis has no measured backward speed")
        elif role in {KeyRole.STRAFE_LEFT, KeyRole.STRAFE_RIGHT}:
            if role is KeyRole.STRAFE_LEFT:
                displacement = basis.strafe_displacement
                pulse = basis.strafe_pulse_seconds
            else:
                displacement = basis.strafe_right_displacement
                pulse = basis.strafe_right_pulse_seconds
            if pulse is None:
                raise DriverError("movement basis has no measured strafe role speed")
        else:
            raise DriverError("motion pulse role is outside the measured movement basis")
        speed = displacement / pulse
        if not math.isfinite(speed) or speed <= 0.0:
            raise DriverError("movement basis has no measured role speed")
        return speed

    def _confirm_motion_pose(self, generation: int, frame: MotionFrame) -> MotionFrame:
        """Confirm model evidence survived on a fresh same-pose native frame."""

        confirmed = self._observe_motion(generation, previous=frame)
        if (
            confirmed.identity_fingerprint != frame.identity_fingerprint
            or confirmed.boot != frame.boot
            or confirmed.instance != frame.instance
            or confirmed.zone != frame.zone
            or abs(confirmed.z - frame.z) > 0.25
            or math.hypot(confirmed.x - frame.x, confirmed.y - frame.y) > MAX_CONFIRMATION_POSITION_DRIFT
            or abs(normalize_angle(confirmed.facing - frame.facing)) > MAX_CONFIRMATION_FACING_DRIFT
        ):
            self._invalidate_motion_admission()
            raise DriverError("native motion changed during visual evidence")
        if confirmed.chat_focused:
            self._invalidate_motion_admission()
            raise DriverError("native motion confirmation is refused while the chat edit box is focused")
        return confirmed

    def _invalidate_motion_admission(self) -> None:
        """Drop all retained motion proofs after a native gate failure."""

        self.camera_mapping = None
        self.movement_basis = None

    def _require_native_gate(self, frame: MotionFrame, *, matching: MotionFrame | None = None) -> None:
        try:
            frame.require_native_gate(matching=matching)
        except MotionError as exc:
            self._invalidate_motion_admission()
            raise DriverError(str(exc)) from exc

    @staticmethod
    def _route_matches_corridor(
        route: RouteEvidence,
        frame: MotionFrame,
        generation: int,
        corridor_heading: float,
        corridor_distance: float,
    ) -> bool:
        return (
            route.identity_fingerprint == frame.identity_fingerprint
            and route.generation == generation
            and route.scene == "world"
            and route.boot == frame.boot
            and route.sequence == frame.sequence
            and route.instance == frame.instance
            and route.zone == frame.zone
            and route.capture_monotonic >= frame.capture_monotonic
            and route.corridor_heading is not None
            and abs(normalize_angle(route.corridor_heading - corridor_heading)) <= 1e-6
            and route.corridor_distance is not None
            and abs(route.corridor_distance - corridor_distance) <= 1e-6
        )

    def _route_matches_frame(
        self,
        route: RouteEvidence,
        frame: MotionFrame,
        generation: int,
        target_x: float,
        target_y: float,
        forward_heading: float | None = None,
    ) -> bool:
        """Check the complete frame/corridor binding before a physical pulse."""

        if forward_heading is None:
            forward_heading = self._world_forward_heading(frame)
        expected_heading = normalize_angle(
            math.atan2(target_y - frame.y, target_x - frame.x) - forward_heading
        )
        expected_distance = math.hypot(target_x - frame.x, target_y - frame.y)
        return (
            route.identity_fingerprint == frame.identity_fingerprint
            and route.generation == generation
            and route.scene == "world"
            and route.boot == frame.boot
            and route.sequence == frame.sequence
            and route.instance == frame.instance
            and route.zone == frame.zone
            and route.capture_monotonic >= frame.capture_monotonic
            and route.corridor_heading is not None
            and abs(normalize_angle(route.corridor_heading - expected_heading)) <= 1e-6
            and route.corridor_distance is not None
            and abs(route.corridor_distance - expected_distance) <= 1e-6
        )

    def _observe_motion(
        self,
        generation: int,
        previous: MotionFrame | None = None,
        *,
        not_before: float | None = None,
        after_release: bool = False,
        deadline_monotonic: float | None = None,
        allow_executor: bool = False,
    ) -> MotionFrame:
        try:
            self._require_motion_interaction(generation, allow_executor=allow_executor)
            deadline = self._motion_deadline()
            if deadline_monotonic is not None:
                if (
                    isinstance(deadline_monotonic, bool)
                    or not isinstance(deadline_monotonic, (int, float))
                    or not math.isfinite(float(deadline_monotonic))
                ):
                    raise DriverError("motion observation deadline is malformed")
                deadline = min(deadline, float(deadline_monotonic))
                if deadline <= self._native_clock():
                    raise DriverError("motion observation deadline expired")
            if after_release:
                if previous is None:
                    raise DriverError("post-release motion observation requires its pre-action frame")
                frame = self.motion_provider.observe_after_release(
                    generation, previous=previous, deadline=deadline
                )
            else:
                frame = self.motion_provider.observe(
                    generation, previous=previous, deadline=deadline, not_before=not_before
                )
            # Every frame returned by the daemon's motion observation boundary
            # is an admission candidate.  Gate it before pose, speed, chat,
            # route, model, or driver checks so a disallowed successor cannot
            # be hidden by an earlier validation failure.
            self._require_native_gate(frame, matching=previous)
            return frame
        except MotionError as exc:
            self._invalidate_motion_admission()
            raise DriverError(str(exc)) from exc
        except Exception:
            # A required successor that cannot be obtained leaves the prior
            # camera and movement proofs unbound to the next native frame.
            self._invalidate_motion_admission()
            raise

    def _observe_route(
        self,
        generation: int,
        target_x: float | None = None,
        target_y: float | None = None,
        *,
        frame: MotionFrame | None = None,
        corridor_heading: float | None = None,
        corridor_distance: float | None = None,
    ) -> RouteEvidence:
        self._require_motion_interaction(generation)
        if frame is None:
            frame = self._observe_motion(generation)
        if frame.chat_focused:
            self._invalidate_motion_admission()
            raise DriverError("route evidence is refused while the chat edit box is focused")
        has_target = target_x is not None or target_y is not None
        if has_target and (target_x is None or target_y is None):
            raise DriverError("route evidence requires both target coordinates")
        if not has_target and (
            corridor_heading is None
            or corridor_distance is None
            or not math.isfinite(float(corridor_heading))
            or not math.isfinite(float(corridor_distance))
            or float(corridor_distance) <= 0.0
        ):
            raise DriverError("route evidence requires a positive egocentric corridor")
        if has_target:
            assert target_x is not None and target_y is not None
            if corridor_heading is None:
                try:
                    corridor_heading = normalize_angle(
                        math.atan2(target_y - frame.y, target_x - frame.x)
                        - self._world_forward_heading(frame)
                    )
                except (TypeError, ValueError):
                    raise DriverError("route evidence target coordinates are malformed") from None
            if corridor_distance is None:
                corridor_distance = math.hypot(target_x - frame.x, target_y - frame.y)
        assert corridor_heading is not None and corridor_distance is not None
        if not math.isfinite(float(corridor_heading)) or not math.isfinite(float(corridor_distance)):
            raise DriverError("route evidence corridor is malformed")
        corridor_heading = normalize_angle(float(corridor_heading))
        corridor_distance = float(corridor_distance)
        if corridor_distance <= 0.0:
            raise DriverError("route evidence corridor distance must be positive")
        deadline = self._motion_deadline()
        identity = self._resolve_identity(require_foreground=True, deadline=deadline)
        if frame.identity_fingerprint != identity.fingerprint:
            raise DriverError("route frame identity is stale")
        captured_at = self._native_clock()
        image = self._capture_model(deadline=deadline)
        remaining = self._remaining_motion_budget(deadline) - MOTION_POST_RELEASE_RESERVE_SECONDS
        if remaining <= 0:
            raise DriverError("route evidence left no post-release feedback reserve")
        observation = self._call_model_adapter(
            "observe_route",
            image,
            target="game-client",
            verified_crop=self.config.profile.model_crop,
            target_x=target_x,
            target_y=target_y,
            origin_x=frame.x,
            origin_y=frame.y,
            corridor_heading=corridor_heading,
            corridor_distance=corridor_distance,
            timeout=remaining,
        )
        if self._native_clock() > deadline - MOTION_POST_RELEASE_RESERVE_SECONDS:
            raise DriverError("route visual evidence exceeded its freshness budget")
        if (
            self._resolve_identity(require_foreground=True, deadline=deadline).fingerprint
            != identity.fingerprint
        ):
            raise DriverError("client identity changed during route evidence")
        digest = hashlib.sha256(image).hexdigest()
        return RouteEvidence(
            identity_fingerprint=identity.fingerprint,
            capture_monotonic=captured_at,
            scene=observation.scene,
            route_id=observation.route_id,
            target_visible=observation.target_visible,
            ground_clear=observation.ground_clear,
            generation=generation,
            boot=frame.boot,
            sequence=frame.sequence,
            instance=frame.instance,
            zone=frame.zone,
            screenshot_digest=digest,
            corridor_heading=corridor_heading,
            corridor_distance=corridor_distance,
        )

    def _observe_viewport(self, generation: int, frame: MotionFrame) -> ViewportEvidence:
        """Bind a fresh model-measured clear world pixel to one native frame."""

        self._require_motion_interaction(generation)
        if frame.chat_focused:
            self._invalidate_motion_admission()
            raise DriverError("camera viewport proof is refused while chat is focused")
        deadline = self._motion_deadline()
        identity = self._resolve_identity(require_foreground=True, deadline=deadline)
        if frame.identity_fingerprint != identity.fingerprint:
            raise DriverError("viewport frame identity is stale")
        captured_at = self._native_clock()
        image = self._capture_model(deadline=deadline)
        remaining = self._remaining_motion_budget(deadline) - MOTION_POST_RELEASE_RESERVE_SECONDS
        if remaining <= 0:
            raise DriverError("viewport evidence left no post-release feedback reserve")
        observe_viewport = getattr(self.model, "observe_viewport", None)
        if not callable(observe_viewport):
            raise DriverError("model has no strict world viewport observation provider")
        observation = self._call_model_adapter(
            "observe_viewport",
            image,
            target="game-client",
            verified_crop=self.config.profile.model_crop,
            timeout=remaining,
        )
        if self._native_clock() > deadline - MOTION_POST_RELEASE_RESERVE_SECONDS:
            raise DriverError("viewport visual evidence exceeded its freshness budget")
        if (
            self._resolve_identity(require_foreground=True, deadline=deadline).fingerprint
            != identity.fingerprint
        ):
            raise DriverError("client identity changed during viewport evidence")
        if getattr(observation, "scene", None) != "world":
            raise DriverError("viewport evidence is not a world scene")
        ui_clear = getattr(observation, "ui_clear", None)
        units_clear = getattr(observation, "units_clear", None)
        if not isinstance(ui_clear, bool) or not isinstance(units_clear, bool):
            raise DriverError("viewport model clearance flags are malformed")
        if not ui_clear or not units_clear:
            raise DriverError("viewport evidence does not prove a clear world point")
        raw_point = getattr(observation, "point", None)
        if (
            not isinstance(raw_point, tuple)
            or len(raw_point) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_point)
        ):
            raise DriverError("viewport model point is malformed")
        crop_x, crop_y, crop_width, crop_height = self.config.profile.model_crop
        if not (0 <= raw_point[0] < crop_width and 0 <= raw_point[1] < crop_height):
            raise DriverError("viewport model point is outside the verified model crop")
        point = (crop_x + raw_point[0], crop_y + raw_point[1])
        client_width, client_height = identity.child_geometry[2:]
        if not (0 <= point[0] < client_width and 0 <= point[1] < client_height):
            raise DriverError("viewport model point is outside the verified client")
        return ViewportEvidence(
            identity_fingerprint=identity.fingerprint,
            capture_monotonic=captured_at,
            scene=observation.scene,
            point=point,
            ui_clear=ui_clear,
            units_clear=units_clear,
            generation=generation,
            boot=frame.boot,
            sequence=frame.sequence,
            instance=frame.instance,
            zone=frame.zone,
            screenshot_digest=hashlib.sha256(image).hexdigest(),
        )

    def _camera_drag_plan(
        self,
        frame: MotionFrame,
        delta_pixels: int,
        identity: WindowIdentity,
        generation: int,
        viewport: ViewportEvidence,
        *,
        session_generation: int,
        evidence_frame: MotionFrame | None = None,
    ) -> Any:
        if isinstance(delta_pixels, bool) or not isinstance(delta_pixels, int):
            raise DriverError("camera delta must be an integer")
        width, height = identity.child_geometry[2:]
        if not isinstance(viewport, ViewportEvidence):
            raise DriverError("camera drag requires fresh measured viewport evidence")
        proof_frame = frame if evidence_frame is None else evidence_frame
        if (
            viewport.identity_fingerprint != proof_frame.identity_fingerprint
            or viewport.identity_fingerprint != identity.fingerprint
            or viewport.generation != session_generation
            or viewport.boot != proof_frame.boot
            or viewport.sequence != proof_frame.sequence
            or viewport.instance != proof_frame.instance
            or viewport.zone != proof_frame.zone
            or viewport.capture_monotonic < proof_frame.capture_monotonic
            or not viewport.ui_clear
            or not viewport.units_clear
        ):
            raise DriverError("camera viewport evidence is stale or unbound")
        if self._native_clock() - viewport.capture_monotonic > MAX_FRAME_AGE:
            raise DriverError("camera viewport evidence is stale at dispatch")
        start = Point(*viewport.point)
        safe_margin = 4
        if not (
            safe_margin <= start.x < width - safe_margin and safe_margin <= start.y < height - safe_margin
        ):
            raise DriverError("measured world viewport point is outside the safe client interior")
        end = Point(start.x + delta_pixels, start.y)
        client = Rect(0, 0, width, height)
        if not (safe_margin <= end.x < width - safe_margin and safe_margin <= end.y < height - safe_margin):
            raise DriverError("camera drag endpoint is outside the safe client interior")
        binding = BindingMetadata(
            identity.fingerprint,
            f"motion:{frame.boot}:{frame.sequence}",
            frame.sequence,
            generation,
            identity.child_xid,
            (identity.xid, identity.child_xid),
            source="native-addon",
            boot=frame.boot,
        )
        return plan_camera_drag(
            start,
            end,
            client,
            binding,
            profile=self._motion_profiles["camera"],
        )

    @_serialized_motion
    def calibrate_camera_mapping(self, delta_pixels: int, *, generation: int) -> CameraMapping:
        """Measure signed camera response with one bounded right drag."""

        if (
            isinstance(delta_pixels, bool)
            or not isinstance(delta_pixels, int)
            or not MIN_CAMERA_CALIBRATION_PIXELS <= abs(delta_pixels) <= MAX_CAMERA_CALIBRATION_PIXELS
            or MAX_CAMERA_YAW_PER_PIXEL * abs(delta_pixels) > MAX_CAMERA_CALIBRATION_YAW
        ):
            raise DriverError("camera calibration delta must be 4..8 pixels within the sub-pi envelope")
        lease_generation = self._require_motion_interaction(generation)
        operation_local = getattr(self, "_motion_deadline_local", None)
        operation_deadline = getattr(operation_local, "deadline", None)
        before = self._observe_motion(generation)
        self._require_native_gate(before)
        viewport_route = self._observe_route(
            generation,
            frame=before,
            corridor_heading=0.0,
            corridor_distance=0.5,
        )
        if not viewport_route.target_visible or not viewport_route.ground_clear:
            raise DriverError("camera calibration lacks a clear measured world viewport")
        if not self._route_matches_corridor(viewport_route, before, generation, 0.0, 0.5):
            raise DriverError("camera calibration corridor is not bound to the native frame")
        identity = self._resolve_identity(
            require_foreground=True,
            deadline=operation_deadline,
        )
        viewport = self._observe_viewport(generation, before)
        confirmed = self._confirm_motion_pose(generation, before)
        self._require_native_gate(confirmed, matching=before)
        plan = self._camera_drag_plan(
            confirmed,
            delta_pixels,
            identity,
            lease_generation,
            viewport,
            session_generation=generation,
            evidence_frame=before,
        )
        if (
            not self._route_matches_corridor(viewport_route, before, generation, 0.0, 0.5)
            or self._native_clock() - viewport_route.capture_monotonic > MAX_FRAME_AGE
            or self._native_clock() - before.capture_monotonic > MAX_FRAME_AGE
        ):
            raise DriverError("camera calibration route evidence expired before physical commit")
        if operation_deadline is not None and (
            self._native_clock() + plan.duration + MOTION_POST_RELEASE_RESERVE_SECONDS >= operation_deadline
        ):
            raise DriverError("camera calibration cannot fit the bounded intent deadline")
        if self._native_clock() - before.capture_monotonic > MAX_FRAME_AGE:
            raise DriverError("camera calibration frame is stale at dispatch")
        lease_generation = self._require_motion_interaction(generation)
        self.driver.run_camera_drag(
            plan,
            generation=lease_generation,
            action_generation=generation,
            deadline_monotonic=operation_deadline,
            live_admission_check=lambda: self._require_live_button_action_authority(
                action_generation=generation,
                lease_generation=lease_generation,
                failure_message="camera calibration action authority changed before physical drag",
            ),
        )
        after = self._observe_motion(generation, previous=confirmed, after_release=True)
        self._require_native_gate(after, matching=confirmed)
        if (
            after.identity_fingerprint != confirmed.identity_fingerprint
            or after.boot != confirmed.boot
            or after.sequence <= confirmed.sequence
            or after.capture_monotonic <= confirmed.capture_monotonic
            or after.instance != confirmed.instance
            or after.zone != confirmed.zone
            or abs(after.z - confirmed.z) > 0.25
            or math.hypot(after.x - confirmed.x, after.y - confirmed.y) > 0.01
        ):
            self._invalidate_motion_admission()
            raise DriverError("camera calibration changed scene or vertical position")
        # The physical drag was admitted from ``confirmed``.  Measure its
        # response from that same dispatch baseline so pre-input facing drift
        # cannot manufacture calibration progress.
        yaw_delta = (after.facing - confirmed.facing + math.pi) % (2.0 * math.pi) - math.pi
        if abs(yaw_delta) < 0.001:
            raise DriverError("camera calibration produced no measured yaw change")
        if abs(yaw_delta) > MAX_CAMERA_CALIBRATION_YAW:
            raise DriverError("camera calibration response exceeds the bounded angular motion")
        yaw_per_pixel = yaw_delta / delta_pixels
        if not math.isfinite(yaw_per_pixel) or not 0.0001 <= abs(yaw_per_pixel) <= MAX_CAMERA_YAW_PER_PIXEL:
            raise DriverError("camera calibration response exceeds the measured yaw bound")
        mapping = CameraMapping(
            identity_fingerprint=identity.fingerprint,
            generation=generation,
            capture_id=f"camera:{before.boot}:{before.sequence}:{after.sequence}",
            yaw_per_pixel=yaw_per_pixel,
            boot=before.boot,
        )
        self.camera_mapping = mapping
        return mapping

    @_serialized_motion
    def calibrate_movement_basis(
        self,
        pulse_seconds: float = DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS,
        *,
        generation: int,
    ) -> MovementBasis:
        """Measure every translation role and fit a circular world basis.

        Forward is repeated at two headings to establish both the facing sign
        and the circular offset.  Backward and both strafe roles are then
        measured independently; their speeds are never inferred from the
        forward probe.  The measured displacement cap and the cumulative
        two-unit cap apply to every probe.
        """

        move_profile = self._motion_profiles["move"]
        profile_limit = move_profile.max_duration
        # Calibration is the one technical probe path allowed below the
        # human motion profile minimum.  The probe still uses the driver's
        # hard pulse ceiling; runtime motion admission remains profile-bound.
        if (
            isinstance(pulse_seconds, bool)
            or not isinstance(pulse_seconds, (int, float))
            or not math.isfinite(float(pulse_seconds))
            or not MIN_MOVEMENT_CALIBRATION_PULSE_SECONDS
            <= float(pulse_seconds)
            <= min(MAX_PULSE_SECONDS, profile_limit)
        ):
            raise DriverError("movement basis calibration pulse is outside bounds")
        camera_mapping = getattr(self, "camera_mapping", None)
        if camera_mapping is None or camera_mapping.generation != generation:
            raise DriverError("movement basis calibration requires a current camera mapping")
        self._require_motion_interaction(generation)
        operation_local = getattr(self, "_motion_deadline_local", None)
        operation_deadline = getattr(operation_local, "deadline", None)
        seed = self._observe_motion(generation)
        before = self._observe_motion(generation, previous=seed)
        self._require_native_gate(before, matching=seed)
        if before.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("movement basis calibration requires a stationary frame")
        identity = self._resolve_identity(require_foreground=True, deadline=operation_deadline)
        if before.identity_fingerprint != identity.fingerprint:
            self._invalidate_motion_admission()
            raise DriverError("movement basis calibration identity is stale")
        if camera_mapping.boot and camera_mapping.boot != before.boot:
            self._invalidate_motion_admission()
            raise DriverError("movement basis camera mapping boot is stale")
        cumulative_probe_distance = 0.0

        def admit_probe(frame: MotionFrame, headings: tuple[float, ...]) -> RouteEvidence:
            """Prove each possible egocentric corridor before an unknown role."""

            # Reserve the complete unknown-role envelope before every probe.
            # Charging only the current measured distance would admit a pulse
            # that can still consume one full unit and discover the two-unit
            # violation only after physical input.
            if cumulative_probe_distance + MAX_BASIS_DISPLACEMENT > MAX_WAYPOINT_DISTANCE:
                raise DriverError("movement basis probe would exceed the cumulative local distance bound")
            route: RouteEvidence | None = None
            for heading in headings:
                # Each heading is proved against the same one-unit maximum
                # displacement before the role is admitted.
                route = prove_corridor(frame, heading, MAX_BASIS_DISPLACEMENT)
            assert route is not None
            return route

        def prove_corridor(frame: MotionFrame, heading: float, distance: float) -> RouteEvidence:
            route = self._observe_route(
                generation,
                frame=frame,
                corridor_heading=heading,
                corridor_distance=distance,
            )
            if not route.target_visible or not route.ground_clear:
                raise DriverError("movement calibration corridor was not positively proved")
            if not self._route_matches_corridor(route, frame, generation, heading, distance):
                raise DriverError("movement calibration corridor is not bound to the native frame")
            if self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE:
                raise DriverError("movement calibration frame is stale at dispatch")
            return route

        def probe(
            frame: MotionFrame, role: KeyRole, route: RouteEvidence, corridor_heading: float
        ) -> tuple[MotionFrame, MotionFrame, float, float, float]:
            nonlocal cumulative_probe_distance
            lease_generation = self._require_motion_interaction(generation)
            if operation_deadline is not None and (
                self._native_clock() + float(pulse_seconds) + MOTION_POST_RELEASE_RESERVE_SECONDS
                >= operation_deadline
            ):
                raise DriverError("movement calibration cannot fit the bounded intent deadline")
            # Route/model work may have taken long enough for the client to
            # reload or move.  Confirm the same native pose immediately before
            # admitting this pulse; this successor is the explicit chain from
            # the egocentric corridor proof to the physical input.
            confirmed = self._confirm_motion_pose(generation, frame)
            self._require_native_gate(confirmed, matching=frame)
            if confirmed.speed > 0.25:
                self._invalidate_motion_admission()
                raise DriverError("movement basis calibration requires a stationary frame")
            if (
                not self._route_matches_corridor(
                    route,
                    frame,
                    generation,
                    corridor_heading,
                    MAX_BASIS_DISPLACEMENT,
                )
                or self._native_clock() - route.capture_monotonic > MAX_FRAME_AGE
                or self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE
            ):
                raise DriverError("movement calibration route evidence expired before physical commit")
            if self._native_clock() - confirmed.capture_monotonic > MAX_FRAME_AGE:
                raise DriverError("movement calibration frame is stale at dispatch")
            if operation_deadline is not None and (
                self._native_clock() + float(pulse_seconds) + MOTION_POST_RELEASE_RESERVE_SECONDS
                >= operation_deadline
            ):
                raise DriverError("movement calibration cannot fit the bounded intent deadline")
            lease_generation = self._require_motion_interaction(generation)
            self.driver.run_motion_key_hold(
                role,
                float(pulse_seconds),
                confirmed,
                generation=lease_generation,
                deadline_monotonic=operation_deadline,
            )
            after = self._observe_motion(generation, previous=confirmed, after_release=True)
            self._require_native_gate(after, matching=confirmed)
            if (
                after.identity_fingerprint != identity.fingerprint
                or after.boot != confirmed.boot
                or after.sequence <= confirmed.sequence
                or after.capture_monotonic <= confirmed.capture_monotonic
                or after.instance != confirmed.instance
                or after.zone != confirmed.zone
                or abs(after.z - confirmed.z) > 0.25
            ):
                self._invalidate_motion_admission()
                raise DriverError("movement basis calibration changed identity, scene, or vertical position")
            if after.speed > 0.25:
                self._invalidate_motion_admission()
                raise DriverError("movement basis calibration requires settled post-release feedback")
            dx, dy = after.x - confirmed.x, after.y - confirmed.y
            displacement = math.hypot(dx, dy)
            if not MIN_BASIS_DISPLACEMENT <= displacement <= MAX_BASIS_DISPLACEMENT:
                raise DriverError("movement basis pulse produced an invalid displacement")
            cumulative_probe_distance += displacement
            if cumulative_probe_distance > MAX_WAYPOINT_DISTANCE:
                raise DriverError("movement basis calibration exceeded its cumulative local bound")
            return confirmed, after, dx, dy, displacement

        # The first probe is a directly-ahead egocentric corridor.  It does
        # not invent a world endpoint from the still-unknown coordinate basis.
        first_route = admit_probe(before, (0.0,))
        first_confirmed, first_after, first_dx, first_dy, first_displacement = probe(
            before, KeyRole.MOVE_FORWARD, first_route, 0.0
        )

        # A controlled camera turn supplies a second nonzero native heading.
        # Camera calibration is required because the facing delta must itself
        # be measured, and its viewport proof remains an independent gate.
        turn_pixels = max(
            self._MIN_CAMERA_DRAG_PIXELS,
            min(120, int(round(0.5 / abs(camera_mapping.yaw_per_pixel)))),
        )
        second_before = self.execute_camera_delta(
            turn_pixels,
            generation=generation,
            previous=first_after,
        )
        if (
            second_before.identity_fingerprint != identity.fingerprint
            or second_before.boot != before.boot
            or second_before.instance != before.instance
            or second_before.zone != before.zone
            or abs(second_before.z - before.z) > 0.25
        ):
            self._invalidate_motion_admission()
            raise DriverError("movement basis heading turn changed identity, scene, or vertical position")
        if second_before.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("movement basis calibration requires a stationary frame")
        second_route = admit_probe(second_before, (0.0,))
        second_confirmed, second_after, second_dx, second_dy, second_displacement = probe(
            second_before, KeyRole.MOVE_FORWARD, second_route, 0.0
        )
        native_heading_delta = normalize_angle(second_confirmed.facing - first_confirmed.facing)
        if abs(native_heading_delta) < 0.1:
            raise DriverError("movement basis heading turn was not measurable")
        first_world_heading = math.atan2(first_dy, first_dx)
        second_world_heading = math.atan2(second_dy, second_dx)
        world_heading_delta = normalize_angle(second_world_heading - first_world_heading)
        if abs(world_heading_delta) < 0.05:
            raise DriverError("movement basis forward headings did not change")
        facing_sign = 1 if world_heading_delta * native_heading_delta > 0 else -1
        derivative = abs(world_heading_delta / native_heading_delta)
        if not 0.5 <= derivative <= 1.5:
            raise DriverError("movement basis facing-to-world derivative is outside bounds")
        beta_first = normalize_angle(first_world_heading - facing_sign * first_confirmed.facing)
        beta_second = normalize_angle(second_world_heading - facing_sign * second_confirmed.facing)
        beta = normalize_angle(
            math.atan2(
                math.sin(beta_first) + math.sin(beta_second),
                math.cos(beta_first) + math.cos(beta_second),
            )
        )
        if abs(normalize_angle(beta_second - beta_first)) > 0.35:
            raise DriverError("movement basis facing offset was inconsistent across headings")
        forward_min = min(first_displacement, second_displacement)
        forward_max = max(first_displacement, second_displacement)
        if forward_min < MIN_BASIS_DISPLACEMENT or forward_max / forward_min > 1.5:
            raise DriverError("movement basis repeated forward probes were inconsistent")

        # All remaining translation roles are measured independently.  Their
        # distinct corridors make a wrong binding or asymmetric speed an
        # explicit calibration failure rather than an inferred value.
        strafe_before = self._observe_motion(generation, previous=second_after)
        if strafe_before.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("movement basis translation calibration requires a stationary frame")
        left_route = admit_probe(strafe_before, (math.pi / 2,))
        strafe_confirmed, strafe_after, strafe_dx, strafe_dy, strafe_displacement = probe(
            strafe_before, KeyRole.STRAFE_LEFT, left_route, math.pi / 2
        )

        # The forward/left cross product is enough to establish the route
        # handedness.  Refuse a reflected basis immediately, before spending
        # physical probes on backward or right strafe and before sealing any
        # movement basis.
        cross = second_dx * strafe_dy - second_dy * strafe_dx
        if abs(cross) < MIN_BASIS_DISPLACEMENT * MIN_BASIS_DISPLACEMENT:
            raise DriverError("movement basis probes are collinear")
        handedness = 1 if cross > 0 else -1
        self._require_supported_route_handedness(handedness)

        backward_before = self._observe_motion(generation, previous=strafe_after)
        if backward_before.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("movement basis translation calibration requires a stationary frame")
        backward_route = admit_probe(backward_before, (math.pi,))
        backward_confirmed, backward_after, backward_dx, backward_dy, backward_displacement = probe(
            backward_before, KeyRole.MOVE_BACKWARD, backward_route, math.pi
        )

        right_before = self._observe_motion(generation, previous=backward_after)
        if right_before.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("movement basis translation calibration requires a stationary frame")
        right_route = admit_probe(right_before, (-math.pi / 2,))
        right_confirmed, right_after, right_dx, right_dy, right_displacement = probe(
            right_before, KeyRole.STRAFE_RIGHT, right_route, -math.pi / 2
        )

        left_world_heading = math.atan2(strafe_dy, strafe_dx)
        left_fitted_forward = normalize_angle(facing_sign * strafe_confirmed.facing + beta)
        left_expected_heading = normalize_angle(left_fitted_forward + handedness * math.pi / 2)
        left_component = strafe_dx * math.cos(left_expected_heading) + strafe_dy * math.sin(
            left_expected_heading
        )
        left_lateral = strafe_dx * math.cos(left_expected_heading + math.pi / 2) + strafe_dy * math.sin(
            left_expected_heading + math.pi / 2
        )
        if left_component <= strafe_displacement * 0.2 or abs(left_lateral) > strafe_displacement * 0.75:
            raise DriverError("movement strafe-left probe moved in the wrong direction")
        left_offset = normalize_angle(
            left_world_heading - (facing_sign * strafe_confirmed.facing + beta + handedness * math.pi / 2)
        )
        # The role names are part of the measured contract.  Check the
        # independently observed vectors against the fitted forward axis so a
        # wrong or mirrored binding is rejected before sealing the basis.
        backward_fitted_forward = normalize_angle(facing_sign * backward_confirmed.facing + beta)
        backward_expected_heading = normalize_angle(backward_fitted_forward + math.pi)
        backward_component = backward_dx * math.cos(backward_expected_heading) + backward_dy * math.sin(
            backward_expected_heading
        )
        backward_lateral = backward_dx * math.cos(
            backward_expected_heading + math.pi / 2
        ) + backward_dy * math.sin(backward_expected_heading + math.pi / 2)
        if (
            backward_component <= backward_displacement * 0.2
            or abs(backward_lateral) > backward_displacement * 0.75
        ):
            raise DriverError("movement backward probe moved in the wrong direction")
        right_fitted_forward = normalize_angle(facing_sign * right_confirmed.facing + beta)
        right_expected_heading = normalize_angle(right_fitted_forward - handedness * math.pi / 2)
        right_component = right_dx * math.cos(right_expected_heading) + right_dy * math.sin(
            right_expected_heading
        )
        right_lateral = right_dx * math.cos(right_expected_heading + math.pi / 2) + right_dy * math.sin(
            right_expected_heading + math.pi / 2
        )
        if right_component <= right_displacement * 0.2 or abs(right_lateral) > right_displacement * 0.75:
            raise DriverError("movement strafe-right probe moved in the wrong direction")
        backward_world_heading = math.atan2(backward_dy, backward_dx)
        right_world_heading = math.atan2(right_dy, right_dx)
        basis = MovementBasis(
            identity_fingerprint=second_after.identity_fingerprint,
            boot=second_after.boot,
            generation=generation,
            capture_id=f"movement:{before.boot}:{before.sequence}:{right_after.sequence}",
            before_sequence=before.sequence,
            after_sequence=right_after.sequence,
            forward_heading_offset=beta,
            # A repeated forward probe may have a slightly different speed;
            # retain the conservative measured upper bound so runtime travel
            # envelopes cannot understate the faster heading.
            forward_displacement=forward_max,
            pulse_seconds=float(pulse_seconds),
            facing_sign=facing_sign,
            strafe_heading_offset=left_offset,
            strafe_displacement=strafe_displacement,
            strafe_pulse_seconds=float(pulse_seconds),
            handedness=handedness,
            backward_displacement=backward_displacement,
            backward_pulse_seconds=float(pulse_seconds),
            strafe_right_displacement=right_displacement,
            strafe_right_pulse_seconds=float(pulse_seconds),
            backward_heading_offset=normalize_angle(
                backward_world_heading - facing_sign * backward_confirmed.facing
            ),
            strafe_right_heading_offset=normalize_angle(
                right_world_heading - facing_sign * right_confirmed.facing
            ),
        )
        self.movement_basis = basis
        return basis

    def _set_route_walk_status(self, **updates: Any) -> None:
        with self._route_walk_lock:
            self._route_walk_status.update(copy.deepcopy(updates))

    @staticmethod
    def _motion_result(frame: MotionFrame) -> dict[str, Any]:
        return {
            "boot": frame.boot,
            "sequence": frame.sequence,
            "identity": frame.identity_fingerprint,
            "zone": frame.zone,
            "instance": frame.instance,
            "x": frame.x,
            "y": frame.y,
            "z": frame.z,
            "facing": frame.facing,
            "speed": frame.speed,
            "hover": {"kind": frame.hover_kind, "name": frame.hover_name},
            "mailOpen": frame.mail_open,
        }

    def _observe_settled_motion(
        self,
        generation: int,
        *,
        previous: MotionFrame | None = None,
        released_at: float | None = None,
    ) -> MotionFrame:
        """Require the measured post-release two-frame settled-fix rule."""

        if released_at is not None and (
            isinstance(released_at, bool)
            or not isinstance(released_at, (int, float))
            or not math.isfinite(float(released_at))
        ):
            raise DriverError("motion release timestamp is malformed")
        settle_origin = self._native_clock() if released_at is None else float(released_at)
        not_before = settle_origin + 1.2
        while self._native_clock() < not_before:
            self._require_motion_interaction(generation)
            self._remaining_motion_budget(self._motion_deadline())
            time.sleep(min(0.05, max(0.001, not_before - self._native_clock())))
        first = self._observe_motion(generation, previous=previous, not_before=not_before)
        while True:
            second = self._observe_motion(generation, previous=first)
            if (
                first.speed == 0.0
                and second.speed == 0.0
                and first.x == second.x
                and first.y == second.y
                and first.facing == second.facing
            ):
                return second
            first = second

    def _route_frame_guard(self, frame: MotionFrame, route: Any) -> None:
        if frame.instance != route.instance:
            self._invalidate_motion_admission()
            raise DriverError("route walk instance changed")
        if frame.combat:
            raise DriverError("route walk is refused during combat")
        if frame.dead_or_ghost:
            raise DriverError("route walk is refused while dead or a ghost")
        if frame.chat_focused:
            raise DriverError("route walk is refused while the chat edit box is focused")
        self._require_native_gate(frame)

    @_serialized_motion
    def execute_route_walk(
        self, route_name: str, *, generation: int, deadline_monotonic: float | None = None
    ) -> MotionFrame:
        if self.route_file is None:
            raise DriverError("route walking is not configured")
        if not isinstance(route_name, str) or not route_name:
            raise DriverError("route name is malformed")
        try:
            route = self.route_file.routes[route_name]
        except KeyError as exc:
            raise DriverError(f"unknown route: {route_name}") from exc
        max_segments, max_seconds = walk_budget(route)
        self._set_route_walk_status(
            state="running",
            route=route_name,
            generation=generation,
            maxSegments=max_segments,
            maxSeconds=max_seconds,
            segments=[],
            error="",
        )
        previous_segment = None
        try:
            lease_generation = self._require_motion_interaction(generation)
            actual = self._observe_settled_motion(generation)
            self._route_frame_guard(actual, route)
            if math.dist((actual.x, actual.y), (route.start.x, route.start.y)) > 1.5:
                raise DriverError("route walk start pose is outside the route start tolerance")
            for index in range(max_segments):
                self._require_motion_interaction(generation)
                self._route_frame_guard(actual, route)
                if previous_segment is not None:
                    verdict = check_fix(
                        route, previous_segment.expected, Pose(actual.x, actual.y, actual.facing)
                    )
                    if not verdict.ok:
                        raise DriverError(
                            "route walk fix failed: "
                            f"position={verdict.position_error:.6f}, "
                            f"facing={verdict.facing_error:.6f}, "
                            f"corridor={verdict.corridor_distance:.6f}"
                        )
                seed = secrets.randbits(63)
                try:
                    segment = plan_segment(
                        route,
                        Pose(actual.x, actual.y, actual.facing),
                        self.route_file.style,
                        self.route_file.kinematics,
                        seed,
                    )
                except (TypeError, ValueError) as exc:
                    raise DriverError(f"route segment planning failed: {exc}") from exc
                record = {
                    "index": index,
                    "segmentIndex": index,
                    "seed": seed,
                    "targetIndex": segment.target_index,
                    "final": segment.final,
                    "events": [[event.t, event.key, event.down] for event in segment.events],
                    "expected": segment.expected.to_dict(),
                }
                with self._route_walk_lock:
                    self._route_walk_status["segments"].append(record)
                deadline = getattr(getattr(self, "_motion_deadline_local", None), "deadline", None)
                lease_generation = self._require_motion_interaction(generation)
                self.driver.run_key_timeline(
                    segment.events,
                    actual,
                    generation=lease_generation,
                    deadline_monotonic=deadline,
                )
                released_at = self._native_clock()
                settled = self._observe_settled_motion(
                    generation,
                    previous=actual,
                    released_at=released_at,
                )
                self._route_frame_guard(settled, route)
                verdict = check_fix(route, segment.expected, Pose(settled.x, settled.y, settled.facing))
                if not verdict.ok:
                    raise DriverError(
                        "route walk fix failed: "
                        f"position={verdict.position_error:.6f}, "
                        f"facing={verdict.facing_error:.6f}, "
                        f"corridor={verdict.corridor_distance:.6f}"
                    )
                actual = settled
                previous_segment = segment
                if segment.final:
                    self._set_route_walk_status(
                        state="complete",
                        final=self._motion_result(actual),
                        error="",
                    )
                    return actual
            raise DriverError("route walk exceeded its segment budget")
        except Exception as exc:
            self._set_route_walk_status(state="error", error=str(exc))
            raise

    @staticmethod
    def _world_probe_points(hint: Point, radius: float, bounds: Rect) -> tuple[Point, ...]:
        clipped = bounds.clip(hint).to_pixel()
        points = [clipped]
        seen = {(int(clipped.x), int(clipped.y))}
        x = y = 0
        leg = 1
        directions = ((1, 0), (0, 1), (-1, 0), (0, -1))
        while len(points) < 16 and leg * 60 <= radius + 60:
            for direction_index, (dx, dy) in enumerate(directions):
                distance = leg if direction_index < 2 else leg + 1
                for _ in range(distance):
                    x += dx
                    y += dy
                    if math.hypot(x * 60, y * 60) > radius + 1e-9:
                        continue
                    candidate = bounds.clip(Point(hint.x + x * 60, hint.y + y * 60)).to_pixel()
                    key = (int(candidate.x), int(candidate.y))
                    if key not in seen:
                        seen.add(key)
                        points.append(candidate)
                        if len(points) == 16:
                            return tuple(points)
            leg += 2
        return tuple(points)

    def _world_expected_state(self, frame: MotionFrame, expect: str) -> bool:
        if expect != "mailOpen":
            raise DriverError("world click expected state is not allowlisted")
        key = (frame.boot, frame.sequence, frame.identity_fingerprint)
        snapshots = getattr(self, "_motion_snapshots", {})
        snapshot = snapshots.get(key) if isinstance(snapshots, dict) else None
        value = snapshot.get("mailOpen") if isinstance(snapshot, dict) else None
        # Small isolated fakes may provide the already-bound field directly;
        # never consult the mutable last-capture snapshot for a result.
        if value is None:
            value = getattr(frame, "mail_open", None)
        if not isinstance(value, bool):
            raise DriverError(f"fresh motion frame omitted {expect} state")
        return value

    def _executor_snapshot_for_frame(self, frame: MotionFrame) -> dict[str, Any]:
        """Return the executor object from the exact decoded motion frame."""

        key = (frame.boot, frame.sequence, frame.identity_fingerprint)
        snapshots = getattr(self, "_motion_snapshots", {})
        snapshot = snapshots.get(key) if isinstance(snapshots, dict) else None
        if not isinstance(snapshot, dict):
            raise DriverError("executor admission frame has no decoded snapshot")
        executor = snapshot.get("executor")
        if not isinstance(executor, dict):
            raise DriverError("executor admission frame omitted executor telemetry")
        return copy.deepcopy(executor)

    def _executor_scene_guard(self, frame: MotionFrame) -> dict[str, Any]:
        """Require the mailbox scene and exact chat/blocking-UI facts."""

        if frame.chat_focused:
            raise DriverError("executor click is refused while chat input is focused")
        if not frame.mail_open:
            raise DriverError("executor click requires the mailbox scene")
        key = (frame.boot, frame.sequence, frame.identity_fingerprint)
        snapshot = getattr(self, "_motion_snapshots", {}).get(key)
        if not isinstance(snapshot, dict) or not self._blocking_ui_is_clear(snapshot):
            raise DriverError("executor click is refused while blocking UI is shown")
        return snapshot

    def _executor_crs(self, frame: MotionFrame) -> dict[str, Any]:
        evidence = self._executor_frame_evidence(frame)
        return {
            "boot": frame.boot,
            "sequence": frame.sequence,
            "crc32": evidence["crc32"],
            "identity": frame.identity_fingerprint,
        }

    def _executor_frame_evidence(self, frame: MotionFrame) -> dict[str, Any]:
        """Return exact private CRS bytes for one decoded motion frame."""

        key = (frame.boot, frame.sequence, frame.identity_fingerprint)
        raw = getattr(self, "_motion_evidence", {}).get(key)
        if not isinstance(raw, bytes) or len(raw) != protocol.CAPACITY:
            raise DriverError("executor frame has no exact CRS byte evidence")
        try:
            decoded = protocol.decode_frame(raw)
        except protocol.ProtocolError as exc:
            raise DriverError("executor frame CRS evidence is not re-decodable") from exc
        mapped_crc = getattr(self, "_motion_crs", {}).get(key)
        if (
            decoded.boot != frame.boot
            or decoded.sequence != frame.sequence
            or isinstance(mapped_crc, bool)
            or not isinstance(mapped_crc, int)
            or mapped_crc != decoded.crc32
        ):
            raise DriverError("executor frame CRS evidence does not match its decoded tuple")
        snapshot = getattr(self, "_motion_snapshots", {}).get(key)
        if not isinstance(snapshot, dict) or snapshot != decoded.snapshot.value:
            raise DriverError("executor frame CRS evidence snapshot does not match capture")
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "length": len(raw),
            "crc32": decoded.crc32,
            "rawFrameHex": raw.hex(),
        }

    def _executor_frame_record(self, frame: MotionFrame) -> dict[str, Any]:
        return {
            "crs": self._executor_crs(frame),
            "executor": self._executor_snapshot_for_frame(frame),
            "evidence": self._executor_frame_evidence(frame),
        }

    @staticmethod
    def _executor_receipt_valid(
        receipt: dict[str, Any] | None,
        *,
        generation: int,
        identity: str,
        anchor: dict[str, Any],
    ) -> bool:
        if not isinstance(receipt, dict):
            return False
        required = {
            "action",
            "generation",
            "identity",
            "admittedAnchorEvidenceHash",
            "admittedAnchorCaptureId",
            "admittedAnchorCaptureSequence",
            "anchorEvidenceHash",
            "anchorCaptureId",
            "anchorCaptureSequence",
            "endpoint",
            "rateGate",
            "clickPrecommitMonotonicNs",
            "clickDispatchStartedMonotonicNs",
            "pressReleaseMonotonicNs",
            "clickAckMonotonicNs",
            "rateRecordMonotonicNs",
            "guardianCompleted",
            "receiptFinalizedMonotonicNs",
        }
        if not required <= set(receipt):
            return False
        endpoint = receipt.get("endpoint")
        times = [
            receipt.get("clickPrecommitMonotonicNs"),
            receipt.get("clickDispatchStartedMonotonicNs"),
            receipt.get("pressReleaseMonotonicNs"),
            receipt.get("clickAckMonotonicNs"),
            receipt.get("rateRecordMonotonicNs"),
            receipt.get("receiptFinalizedMonotonicNs"),
        ]
        return (
            receipt.get("action") == "executor_click"
            and receipt.get("generation") == generation
            and receipt.get("identity") == identity
            and isinstance(receipt.get("admittedAnchorEvidenceHash"), str)
            and len(receipt["admittedAnchorEvidenceHash"]) == 64
            and all(char in "0123456789abcdef" for char in receipt["admittedAnchorEvidenceHash"].lower())
            and isinstance(receipt.get("admittedAnchorCaptureId"), str)
            and bool(receipt["admittedAnchorCaptureId"])
            and isinstance(receipt.get("admittedAnchorCaptureSequence"), int)
            and not isinstance(receipt["admittedAnchorCaptureSequence"], bool)
            and receipt.get("admittedAnchorEvidenceHash") == anchor.get("evidenceHash")
            and receipt.get("admittedAnchorCaptureId") == anchor.get("captureId")
            and receipt.get("admittedAnchorCaptureSequence") == anchor.get("captureSequence")
            and isinstance(receipt.get("anchorEvidenceHash"), str)
            and len(receipt["anchorEvidenceHash"]) == 64
            and all(char in "0123456789abcdef" for char in receipt["anchorEvidenceHash"].lower())
            and isinstance(receipt.get("anchorCaptureId"), str)
            and bool(receipt["anchorCaptureId"])
            and receipt.get("anchorEvidenceHash") != anchor.get("evidenceHash")
            and receipt.get("anchorCaptureId") != anchor.get("captureId")
            and isinstance(receipt.get("anchorCaptureSequence"), int)
            and not isinstance(receipt.get("anchorCaptureSequence"), bool)
            and receipt.get("anchorCaptureSequence") > anchor.get("captureSequence", 0)
            and receipt.get("rateGate") == "canonical-user-session"
            and receipt.get("guardianCompleted") is True
            and isinstance(endpoint, (list, tuple))
            and len(endpoint) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in endpoint)
            and all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in times)
            and times == sorted(times)
        )

    @staticmethod
    def _executor_anchor_record(anchor: MeasuredAnchor) -> dict[str, Any]:
        return {
            "action": anchor.action,
            "evidenceHash": anchor.evidence_hash,
            "captureId": anchor.capture_id,
            "captureSequence": anchor.capture_sequence,
            "identity": anchor.identity_fingerprint,
            "generation": anchor.generation,
            "targetXid": anchor.target_xid,
            "scene": anchor.scene,
            "control": dict(anchor.facts).get("control", ""),
            "bounds": list(anchor.bounds),
        }

    def _validate_executor_frame(
        self,
        executor: dict[str, Any],
        *,
        queue_id: str,
        seq: int,
        require_ready: bool,
        require_arms: bool = False,
        continuation: bool = False,
    ) -> None:
        if executor.get("availability") != "valid":
            raise DriverError("executor queue is not valid")
        if executor.get("queueId") != queue_id:
            raise DriverError("executor queue identity does not match the requested queue")
        if executor.get("currentSeq") != seq or executor.get("currentKind") != "mail_collect":
            raise DriverError("executor current action does not match the requested mail sequence")
        if executor.get("house") != self.config.profile.house:
            raise DriverError("executor queue belongs to another house")
        if executor.get("character") != self.config.profile.character:
            raise DriverError("executor queue belongs to another character")
        if executor.get("buttonName") != "CrowleyTradeExecutorButton" or not executor.get("buttonVisible"):
            raise DriverError("executor button is not visibly available")
        if executor.get("blocked") or executor.get("ambiguous"):
            raise DriverError("executor action is blocked or ambiguous")
        if continuation:
            if executor.get("phase") != "mail_items" or executor.get("inFlight") is not True:
                raise DriverError("mail continuation requires the exact in-flight mail_items phase")
        elif executor.get("phase") != "ready" or executor.get("inFlight") is not False:
            raise DriverError("executor initial click requires the exact ready phase")
        if require_ready and not executor.get("clickReady"):
            raise DriverError("executor button is not click-ready")
        if require_arms and not (executor.get("publishedArmed") and executor.get("sessionArmed")):
            raise DriverError("executor protected click requires both observed arm bits")

    @staticmethod
    def _executor_pre_state(executor: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(executor.get(key))
            for key in (
                "queueId",
                "currentSeq",
                "currentKind",
                "phase",
                "workDone",
                "workTotal",
                "resultCount",
                "lastResult",
                "clickOrdinal",
                "lastClickSource",
                "publishedArmed",
                "sessionArmed",
                "blocked",
                "ambiguous",
            )
        }

    @staticmethod
    def _executor_delivery_valid(
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        queue_id: str,
        seq: int,
        continuation: bool,
    ) -> bool:
        """Require one exact initial or mail-items click transition."""

        if (
            after.get("queueId") != queue_id
            or after.get("clickOrdinal") != before.get("clickOrdinal", 0) + 1
            or after.get("lastClickSource") != "button"
        ):
            return False
        before_count = before.get("resultCount")
        after_count = after.get("resultCount")
        if (
            isinstance(before_count, bool)
            or not isinstance(before_count, int)
            or isinstance(after_count, bool)
            or not isinstance(after_count, int)
            or after_count != before_count + (0 if continuation else 1)
        ):
            return False
        last = after.get("lastResult")
        if not isinstance(last, dict) or last.get("seq") != seq or last.get("kind") != "mail_collect":
            return False
        status = last.get("status")
        current_seq = after.get("currentSeq")
        current_kind = after.get("currentKind")
        if status == "started":
            return (
                current_seq == seq
                and current_kind == "mail_collect"
                and after.get("phase") in {"mail_wait", "mail_items"}
                and after.get("inFlight") is True
            )
        if status not in {"dry_run", "succeeded", "failed", "refused", "skipped", "blocked"}:
            return False
        if current_seq == 0 and current_kind == "":
            return after.get("phase") == "complete"
        if current_seq == seq and current_kind == "mail_collect":
            return status in {"failed", "refused", "blocked"} and after.get("blocked") is True
        if (
            current_seq == seq + 1
            and isinstance(current_kind, str)
            and current_kind
            in {
                "buy_commodity",
                "post_commodity",
                "cancel_auction",
                "vendor_sell",
                "vendor_buy",
                "craft",
                "mail_collect",
            }
        ):
            return status in {"dry_run", "succeeded", "skipped"}
        return False

    def _require_live_button_action_authority(
        self,
        *,
        action_generation: int,
        lease_generation: int,
        failure_message: str,
    ) -> None:
        """Re-read controller and Guardian authority at a button-down boundary."""

        with self.controller.lock:
            status = self.controller.status()
        if (
            status.generation != action_generation
            or status.lease_generation != lease_generation
            or status.stop_requested
        ):
            raise DriverError(failure_message)
        try:
            self.driver.guardian.check(lease_generation)
        except Exception as exc:
            raise DriverError(failure_message) from exc

    def _executor_final_admission(
        self,
        *,
        generation: int,
        attempt: dict[str, Any],
        pre_frame: MotionFrame,
        pre_executor: dict[str, Any],
        require_arms: bool,
        continuation: bool,
    ) -> MotionFrame:
        """Re-read semantic state at the one-use pre-press boundary."""

        with self.controller.lock:
            checkpoint = self.controller.executor_click_checkpoint()
        if checkpoint is None or checkpoint.get("intent", {}).get("attemptId") != attempt.get("attemptId"):
            raise DriverError("executor final admission lost its durable attempt")
        self._require_live_button_action_authority(
            action_generation=generation,
            lease_generation=attempt["leaseGeneration"],
            failure_message="executor Guardian or controller authority changed before blocking capture",
        )
        current = self._observe_motion(
            generation,
            previous=pre_frame,
            allow_executor=True,
        )
        self._executor_scene_guard(current)
        with self.controller.lock:
            status_after_capture = self.controller.status()
            checkpoint_after_capture = self.controller.executor_click_checkpoint()
        guardian = self.driver.guardian
        if (
            status_after_capture.mode.value != "interaction"
            or status_after_capture.state is not ControllerState.READY
            or status_after_capture.stop_requested
            or checkpoint_after_capture is None
            or checkpoint_after_capture.get("intent", {}).get("attemptId") != attempt.get("attemptId")
            or not getattr(guardian, "armed", False)
            or getattr(guardian, "generation", 0) != attempt["leaseGeneration"]
        ):
            raise DriverError("executor final admission changed after blocking capture")
        self._require_live_button_action_authority(
            action_generation=generation,
            lease_generation=attempt["leaseGeneration"],
            failure_message="executor Guardian or controller authority changed after blocking capture",
        )
        current_executor = self._executor_snapshot_for_frame(current)
        self._validate_executor_frame(
            current_executor,
            queue_id=attempt["queueId"],
            seq=attempt["seq"],
            require_ready=True,
            require_arms=require_arms,
            continuation=continuation,
        )
        if self._executor_pre_state(current_executor) != self._executor_pre_state(pre_executor):
            raise DriverError("executor semantic state changed before physical press")
        if (
            current.boot != pre_frame.boot
            or current.identity_fingerprint != pre_frame.identity_fingerprint
            or current_executor.get("clickOrdinal") != attempt["preClickOrdinal"]
        ):
            raise DriverError("executor final admission CRS or click ordinal changed")
        return current

    @_serialized_motion
    def execute_executor_click(
        self,
        *,
        queue_id: str,
        seq: int,
        confirm_queue_id: str,
        cycle_id: str = "",
        generation: int,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Click one rendered mail executor button through the guarded driver."""

        try:
            queue_size = len(queue_id.encode("utf-8")) if isinstance(queue_id, str) else -1
        except UnicodeError as exc:
            raise DriverError("executor queueId is not valid UTF-8") from exc
        if not isinstance(queue_id, str) or not queue_id or queue_size > 192:
            raise DriverError("executor queueId is malformed")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise DriverError("executor seq must be positive")
        try:
            confirm_size = len(confirm_queue_id.encode("utf-8")) if isinstance(confirm_queue_id, str) else -1
        except UnicodeError as exc:
            raise DriverError("executor confirmQueueId is not valid UTF-8") from exc
        if not isinstance(confirm_queue_id, str) or confirm_size > 192:
            raise DriverError("executor confirmQueueId is malformed")
        if not isinstance(cycle_id, str) or len(cycle_id.encode("utf-8")) > 128:
            raise DriverError("executor cycleId is malformed")
        mail_capability = None
        if cycle_id:
            mail_capability = self.controller.mail_cycle_capability()
            if mail_capability is None or mail_capability.cycle_id != cycle_id:
                raise DriverError("mail cycle capability is not owned by this daemon")
            phase = self.controller.mail_cycle_checkpoint().get("phase")
            if phase not in {"awaiting_confirmation", "executing"}:
                raise DriverError("mail cycle is not awaiting an exact executor request")
        continuation_capability = getattr(self, "_executor_continuation_capability", None)
        with self.controller.lock:
            controller_capability = self.controller.executor_continuation_capability()
            checkpoint = self.controller.executor_click_checkpoint()
        if continuation_capability != controller_capability:
            raise DriverError("executor continuation capability is not owned by this daemon")
        if checkpoint is not None:
            checkpoint_intent = checkpoint.get("intent")
            checkpoint_overall = checkpoint.get("overall")
            if checkpoint_intent is not None:
                raise DriverError("executor click intent is already in flight")
            if checkpoint_overall is not None:
                if continuation_capability is None or checkpoint_overall.get("queueId") != queue_id:
                    raise DriverError("another executor mail action owns interaction admission")
                status = self.controller.status()
                expected_overall = {
                    "queueId": queue_id,
                    "seq": seq,
                    "cycleId": cycle_id,
                    "generation": generation,
                    "leaseGeneration": status.lease_generation,
                }
                if any(checkpoint_overall.get(key) != expected for key, expected in expected_overall.items()):
                    raise DriverError("another executor mail action owns interaction admission")
        cycle_phase = (
            self.controller.mail_cycle_checkpoint().get("phase") if mail_capability is not None else None
        )
        # A started callback can already publish the next mail_items boundary.
        # That boundary retains the private overall owner for controller
        # continuity.  The next physical click remains a continuation of
        # that exact owner, but still requires a fresh exact confirmation;
        # it is never inferred from the prior request.
        continuation = continuation_capability is not None
        if (
            mail_capability is not None
            and continuation
            and cycle_phase not in {"executing", "awaiting_confirmation"}
        ):
            raise DriverError("mail continuation is not in an active mail phase")
        mail_operation_deadline: float | None = None
        if mail_capability is not None:
            mail_operation_deadline = min(
                mail_capability.deadline_monotonic,
                self._mail_cycle_clock()
                + mail_capability.budget.mail_click_seconds
                + mail_capability.budget.mail_postcondition_seconds,
            )
            if deadline_monotonic is not None:
                if (
                    isinstance(deadline_monotonic, bool)
                    or not isinstance(deadline_monotonic, (int, float))
                    or not math.isfinite(float(deadline_monotonic))
                ):
                    raise DriverError("executor click deadline is malformed")
                mail_operation_deadline = min(mail_operation_deadline, float(deadline_monotonic))
            self._mail_cycle_require_budget(
                mail_capability,
                mail_capability.budget.mail_click_seconds + mail_capability.budget.mail_postcondition_seconds,
                "mail executor click",
                operation_deadline=mail_operation_deadline,
            )
        lease_generation = self._require_motion_interaction(
            generation, allow_executor=continuation or mail_capability is not None
        )
        initial = self._observe_motion(generation, allow_executor=continuation)
        self._executor_scene_guard(initial)
        initial_executor = self._executor_snapshot_for_frame(initial)
        self._validate_executor_frame(
            initial_executor,
            queue_id=queue_id,
            seq=seq,
            require_ready=True,
            continuation=continuation,
        )
        if confirm_queue_id and confirm_queue_id != queue_id:
            raise DriverError("executor confirmQueueId must exactly equal queueId")
        initially_armed = bool(
            initial_executor.get("publishedArmed") and initial_executor.get("sessionArmed")
        )
        if continuation and (not initially_armed or confirm_queue_id != queue_id):
            raise DriverError("mail continuation requires both arms and exact queue confirmation")
        if not continuation and initially_armed and not confirm_queue_id:
            raise DriverError("armed executor click requires exact queue confirmation")
        protected = continuation or (initially_armed and bool(confirm_queue_id))
        # An initially unarmed ready action still takes the real measured
        # button path.  The addon callback records dry_run and advances the
        # queue without invoking a protected API; the daemon must witness that
        # causal transition instead of fabricating a result without input.
        self._require_executor_frame_for_dry_run(initial_executor)

        pre_record = self._executor_frame_record(initial)
        deadline = self._motion_deadline(limit=None)
        if mail_operation_deadline is not None:
            deadline = min(deadline, mail_operation_deadline)
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(float(deadline_monotonic))
            ):
                raise DriverError("executor click deadline is malformed")
            deadline = min(deadline, float(deadline_monotonic))
        self._remaining_motion_budget(deadline)
        if mail_capability is not None:
            self._mail_cycle_require_budget(
                mail_capability,
                mail_capability.budget.mail_click_seconds + mail_capability.budget.mail_postcondition_seconds,
                "mail executor click",
                operation_deadline=deadline,
            )
        anchor = self.driver.measure_pointer_anchor(
            "executor_click",
            generation=lease_generation,
            deadline_monotonic=deadline,
        )
        attempt = self.controller.begin_executor_click(
            queue_id=queue_id,
            seq=seq,
            generation=generation,
            lease_generation=lease_generation,
            cycle_id=cycle_id,
            pre_crs=self._executor_crs(initial),
            pre_ordinal=int(initial_executor["clickOrdinal"]),
            arms={
                "published": bool(initial_executor["publishedArmed"]),
                "session": bool(initial_executor["sessionArmed"]),
            },
            anchor=self._executor_anchor_record(anchor),
            confirmation=confirm_queue_id,
            pre_executor=pre_record["executor"],
            pre_evidence=pre_record["evidence"],
            continuation_capability=continuation_capability,
        )
        pre_evidence = pre_record["evidence"]
        receipt: dict[str, Any] | None = None
        post: MotionFrame | None = None
        post_record: dict[str, Any] | None = None
        observation_error = ""
        finalization_attempted = False
        self._remaining_motion_budget(deadline)
        if mail_capability is not None:
            # Guardian/native admission may tighten the operation
            # intersection after the public mail-budget check.  Revalidate
            # the same fixed deadline before any physical dispatch; never
            # silently replace it with a fresh postcondition window.
            self._mail_cycle_require_budget(
                mail_capability,
                mail_capability.budget.mail_click_seconds + mail_capability.budget.mail_postcondition_seconds,
                "mail executor click",
                operation_deadline=deadline,
            )
        # Preserve the original operation intersection for the subsequent
        # read-only mail wait. It is sampled before physical dispatch so a
        # slow click/feedback path cannot mint a fresh postcondition window.
        mail_wait_started = self._mail_cycle_clock() if mail_capability is not None else None
        standalone_wait_started = self._native_clock() if mail_capability is None else None
        try:
            receipt = self.driver.run(
                "executor_click",
                lease_generation,
                action_generation=generation,
                deadline_monotonic=deadline,
                precondition=lambda identity: identity.fingerprint == initial.identity_fingerprint,
                postcondition=lambda identity: identity.fingerprint == initial.identity_fingerprint,
                _premeasured_anchor=anchor,
                _final_admission=lambda: self._executor_final_admission(
                    generation=generation,
                    attempt=attempt,
                    pre_frame=initial,
                    pre_executor=initial_executor,
                    require_arms=protected,
                    continuation=continuation,
                ),
            )
            if not self._executor_receipt_valid(
                receipt,
                generation=lease_generation,
                identity=initial.identity_fingerprint,
                anchor=attempt["anchor"],
            ):
                raise DriverError("executor physical receipt is missing or malformed")
            # A physical release can precede the addon's next publication by
            # one or more native frames.  Poll only the read-only capture path
            # under this operation's absolute deadline; never resend input.
            prior_post = initial
            delivered = False
            while True:
                try:
                    post = self._observe_motion(
                        generation,
                        previous=prior_post,
                        after_release=True,
                        deadline_monotonic=deadline,
                        allow_executor=True,
                    )
                    self._executor_scene_guard(post)
                    post_record = self._executor_frame_record(post)
                    post_executor = post_record["executor"]
                    delivered = (
                        post.boot == initial.boot
                        and post.identity_fingerprint == initial.identity_fingerprint
                        and self._executor_delivery_valid(
                            initial_executor,
                            post_executor,
                            queue_id=queue_id,
                            seq=seq,
                            continuation=continuation,
                        )
                    )
                    if delivered:
                        break
                    pre_ordinal = initial_executor.get("clickOrdinal")
                    current_ordinal = post_executor.get("clickOrdinal")
                    pre_count = initial_executor.get("resultCount")
                    current_count = post_executor.get("resultCount")
                    # Only an unchanged, exact pre-click projection is a
                    # retryable stale publication.  Boot/identity, queue,
                    # sequence, ordinal, source, or result-count changes that
                    # do not form the exact causal transition are
                    # contradictions and fail closed immediately.
                    retryable_stale = (
                        post.boot == initial.boot
                        and post.identity_fingerprint == initial.identity_fingerprint
                        and post_executor.get("queueId") == queue_id
                        and self._executor_pre_state(post_executor)
                        == self._executor_pre_state(initial_executor)
                        and isinstance(pre_ordinal, int)
                        and not isinstance(pre_ordinal, bool)
                        and current_ordinal == pre_ordinal
                        and isinstance(pre_count, int)
                        and not isinstance(pre_count, bool)
                        and current_count == pre_count
                        and post_executor.get("lastClickSource") in {"none", "button"}
                    )
                    if not retryable_stale:
                        observation_error = "executor post-click publication was contradictory"
                        break
                    if self._native_clock() >= deadline:
                        observation_error = "executor post-click publication timed out"
                        break
                    prior_post = post
                except Exception as exc:
                    observation_error = str(exc)
                    break
            if post is None or post_record is None:
                raise DriverError(f"executor post-click capture failed: {observation_error}")
            post_executor = post_record["executor"]
            if not delivered:
                result = "blocked"
                terminal_action = False
            else:
                last = post_executor.get("lastResult", {})
                result = str(last.get("status", "started")) if isinstance(last, dict) else "started"
                if result not in {
                    "dry_run",
                    "started",
                    "blocked",
                    "succeeded",
                    "failed",
                    "refused",
                    "skipped",
                }:
                    result = "blocked"
                terminal_action = result in {"dry_run", "succeeded", "failed", "refused", "skipped"}
            # A known physical receipt is retained even when the post-frame is
            # uncertain; only a witnessed terminal transition clears intent.
            finalization_attempted = True
            archived = self.controller.finalize_executor_click(
                attempt_id=attempt["attemptId"],
                result=result,
                post_crs=post_record["crs"],
                post_executor=post_executor,
                receipt=receipt,
                pre_evidence=pre_evidence,
                post_evidence=post_record["evidence"],
                terminal_action=terminal_action,
            )
            if result == "started":
                started_phase = post_executor.get("phase")
                if mail_capability is None:
                    self._executor_continuation_capability = (
                        self.controller.executor_continuation_capability()
                    )
                    if standalone_wait_started is None:
                        raise DriverError("standalone executor wait start is missing")
                    self._executor_continuation_wait_deadline = min(
                        deadline,
                        standalone_wait_started + self._NATIVE_FRAME_TIMEOUT_SECONDS,
                    )
                else:
                    # Controller mints one private continuation capability for
                    # every started callback, including cycle-owned mail
                    # actions.  Retain the exact token here so a later public
                    # request can continue the same owner; never synthesize a
                    # token or treat the prior request as confirmation.
                    self._executor_continuation_capability = (
                        self.controller.executor_continuation_capability()
                    )
                    if self._executor_continuation_capability is None:
                        raise DriverError("mail executor continuation capability is missing")
                if mail_capability is not None and started_phase == "mail_wait":
                    self._mail_cycle_wait_deadlines[mail_capability.cycle_id] = min(
                        deadline,
                        mail_wait_started + mail_capability.budget.mail_postcondition_seconds,
                    )
                elif mail_capability is not None and started_phase == "mail_items":
                    # A postcondition can run synchronously with the protected
                    # callback.  It is a fresh boundary for the next message,
                    # not an in-flight wait for the click just archived.
                    self._mail_cycle_wait_deadlines.pop(mail_capability.cycle_id, None)
            elif terminal_action or result == "blocked":
                self._executor_continuation_capability = None
                self._executor_continuation_wait_deadline = None
                if mail_capability is not None:
                    self._mail_cycle_wait_deadlines.pop(mail_capability.cycle_id, None)
            if mail_capability is not None:
                self.controller.record_mail_cycle_progress(
                    capability=mail_capability,
                    executor=post_executor,
                )
                if result == "started":
                    started_phase = post_executor.get("phase")
                    if started_phase == "mail_wait":
                        self.controller.transition_mail_cycle(
                            "executing", capability=mail_capability, last_executor_seq=seq
                        )
                    elif started_phase == "mail_items":
                        # A first callback may already be in the
                        # confirmation phase (the usual initial click path)
                        # or may be observed from an executing wait.  Only
                        # the latter needs a phase transition; both retain
                        # the exact admitted sequence and fresh-click gate.
                        if self.controller.mail_cycle_checkpoint().get("phase") != ("awaiting_confirmation"):
                            self.controller.transition_mail_cycle(
                                "awaiting_confirmation",
                                capability=mail_capability,
                                last_executor_seq=seq,
                            )
                    else:
                        self.controller.block_mail_cycle(
                            "started mail executor frame lacks mail_wait/mail_items ownership",
                            capability=mail_capability,
                        )
                        raise DriverError("started mail executor frame has an invalid phase")
                elif result in {"blocked", "failed", "refused"}:
                    self.controller.block_mail_cycle(
                        f"executor click was {result}; no route or retry is permitted",
                        capability=mail_capability,
                    )
                elif terminal_action:
                    if result not in {"dry_run", "succeeded"}:
                        self.controller.block_mail_cycle(
                            f"executor click was {result}; no route or retry is permitted",
                            capability=mail_capability,
                        )
                    else:
                        self.controller.accept_mail_terminal(
                            capability=mail_capability,
                            outcome_id=str(archived["outcomeId"]),
                        )
                        self._mail_cycle_wait_deadlines.pop(mail_capability.cycle_id, None)
            return {"result": result, "attemptId": attempt["attemptId"], "archive": archived}
        except Exception as exc:
            # Distinguish physical/observation failure from publication
            # failure.  Once finalize is attempted, never republish a new
            # blocked payload under the same outcome ID.
            if finalization_attempted or isinstance(exc, JournalError):
                if mail_capability is not None:
                    try:
                        self.controller.block_mail_cycle(
                            "executor outcome publication failed; no route or retry is permitted",
                            capability=mail_capability,
                        )
                    except Exception as block_exc:
                        raise DriverError(
                            f"executor outcome failure could not be durably fenced: {block_exc}"
                        ) from exc
                raise DriverError(
                    f"executor outcome publication failed; no retry was attempted: {exc}"
                ) from exc
            try:
                finalization_attempted = True
                archived = self.controller.finalize_executor_click(
                    attempt_id=attempt["attemptId"],
                    result="blocked",
                    post_crs=None,
                    receipt=receipt,
                    pre_evidence=pre_evidence,
                    post_executor=None,
                    post_evidence=None,
                    observation_error=str(exc),
                    terminal_action=False,
                )
            except Exception as archive_exc:
                raise DriverError(
                    "executor click failed and its exact blocked outcome could not be archived; "
                    f"no retry: {archive_exc}"
                ) from exc
            if mail_capability is not None:
                try:
                    self.controller.block_mail_cycle(
                        "executor click outcome was lost or uncertain; no retry is permitted",
                        capability=mail_capability,
                    )
                except Exception as block_exc:
                    raise DriverError(
                        f"executor click uncertainty could not be durably fenced: {block_exc}"
                    ) from exc
            raise DriverError(f"executor click was blocked after durable admission: {exc}") from exc

    @staticmethod
    def _require_executor_frame_for_dry_run(executor: dict[str, Any]) -> None:
        if executor.get("clickOrdinal") is None or executor.get("lastClickSource") not in {"none", "button"}:
            raise DriverError("executor dry-run frame has malformed click witness")

    @_serialized_motion
    def execute_world_click(
        self,
        target_name: str,
        hint_x: float,
        hint_y: float,
        radius: float,
        *,
        expect: str,
        generation: int,
        deadline_monotonic: float | None = None,
    ) -> MotionFrame:
        try:
            target_size = len(target_name.encode("utf-8")) if isinstance(target_name, str) else -1
        except UnicodeError as exc:
            raise DriverError("world click target name is not valid UTF-8") from exc
        if not isinstance(target_name, str) or not target_name or target_size > 48:
            raise DriverError("world click target name is malformed")
        if expect != "mailOpen":
            raise DriverError("world click expected state is not allowlisted")
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in (hint_x, hint_y, radius)
            )
            or not 0.0 <= float(radius) <= 300.0
        ):
            raise DriverError("world click probe geometry is malformed")
        deadline = getattr(getattr(self, "_motion_deadline_local", None), "deadline", None)
        initial = self._observe_motion(generation)
        if initial.chat_focused:
            raise DriverError("world click is refused while the chat edit box is focused")
        snapshots = getattr(self, "_motion_snapshots", {})
        initial_snapshot = (
            snapshots.get((initial.boot, initial.sequence, initial.identity_fingerprint))
            if isinstance(snapshots, dict)
            else None
        )
        if not isinstance(initial_snapshot, dict) or not self._blocking_ui_is_clear(initial_snapshot):
            raise DriverError("world click is refused while blocking UI is shown")
        self._require_native_gate(initial)
        identity = self._resolve_identity(require_foreground=True, deadline=deadline)
        if identity.fingerprint != initial.identity_fingerprint:
            raise DriverError("world click client identity changed before probing")
        bounds = Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3])
        probes = self._world_probe_points(Point(float(hint_x), float(hint_y)), float(radius), bounds)
        current = initial
        for probe in probes:
            lease_generation = self._require_motion_interaction(generation)
            gesture = self.driver.run_pointer_motion(
                probe,
                generation=lease_generation,
                deadline_monotonic=deadline,
            )
            gesture_end = self._native_clock()
            hover = self._observe_motion(
                generation,
                previous=current,
                not_before=gesture_end,
            )
            current = hover
            hover_capture = getattr(hover, "capture_monotonic", None)
            if (
                isinstance(hover_capture, bool)
                or not isinstance(hover_capture, (int, float))
                or hover_capture <= gesture_end
            ):
                continue
            if hover.hover_kind not in {"unit", "object"} or hover.hover_name != target_name:
                continue

            def check_motion_authority() -> None:
                self._require_motion_interaction(generation)

            try:
                rate_reservation = self.driver.input_broker.reserve_button_rate(
                    deadline_monotonic=deadline,
                    cancellation_check=check_motion_authority,
                )
            except (InputBrokerError, DriverError, GuardianError) as exc:
                raise DriverError(f"world click rate admission failed: {exc}") from exc
            try:
                lease_generation = self._require_motion_interaction(generation)
                proof = self._observe_motion(
                    generation,
                    previous=current,
                    not_before=gesture_end,
                )
                current = proof
                proof_age = self._native_clock() - proof.capture_monotonic
                if (
                    proof.identity_fingerprint != identity.fingerprint
                    or proof_age < 0.0
                    or proof_age > MAX_FRAME_AGE
                    or proof.capture_monotonic <= gesture_end
                    or proof.chat_focused
                    or proof.hover_kind not in {"unit", "object"}
                    or proof.hover_name != target_name
                ):
                    raise DriverError("world click hover admission proof is stale or mismatched")
                # Blocking UI can appear while probing or waiting for the permit, so the frame that
                # admits the click must itself show it clear.
                proof_snapshot = snapshots.get((proof.boot, proof.sequence, proof.identity_fingerprint))
                if not isinstance(proof_snapshot, dict) or not self._blocking_ui_is_clear(proof_snapshot):
                    raise DriverError("world click is refused while blocking UI is shown")

                def check_world_click_authority(
                    captured_lease_generation: int = lease_generation,
                ) -> None:
                    self._require_live_button_action_authority(
                        action_generation=generation,
                        lease_generation=captured_lease_generation,
                        failure_message="world click action authority changed before physical click",
                    )

                self.driver.run_hover_click(
                    probe,
                    proof,
                    gesture=gesture,
                    gesture_ended_monotonic=gesture_end,
                    generation=lease_generation,
                    action_generation=generation,
                    lease_generation=lease_generation,
                    deadline_monotonic=deadline,
                    rate_reservation=rate_reservation,
                    live_admission_check=check_world_click_authority,
                    target_name=target_name,
                )
            finally:
                self.driver.input_broker.release_reservation(rate_reservation)
            expect_deadline = self._native_clock() + 3.0
            if deadline is not None:
                expect_deadline = min(expect_deadline, deadline)
            after_click_boundary = True
            while True:
                final = self._observe_motion(
                    generation,
                    previous=current,
                    after_release=after_click_boundary,
                    deadline_monotonic=expect_deadline,
                )
                current = final
                if self._world_expected_state(final, expect):
                    return final
                if self._native_clock() >= expect_deadline:
                    break
                after_click_boundary = False
            raise DriverError(f"world click did not produce {expect}")
        raise DriverError(f"world click target was not found: {target_name}")

    @_serialized_motion
    def execute_motion_key(self, role: KeyRole, duration: float, *, generation: int) -> MotionFrame:
        duration = self._validate_motion_key_request(role, duration)
        lease_generation = self._require_motion_interaction(generation)
        frame = self._observe_motion(generation)
        self._require_native_gate(frame)
        return self._execute_motion_key_with_frame(role, duration, generation, frame, lease_generation)

    def _validate_motion_key_request(self, role: KeyRole, duration: float) -> float:
        """Validate request shape before acquiring native observation authority."""

        if not isinstance(role, KeyRole):
            raise DriverError("motion pulse role is malformed")
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise DriverError("motion pulse duration is malformed") from exc
        minimum = max(0.05, self._motion_profiles["move"].min_duration)
        maximum = min(MAX_PULSE_SECONDS, self._motion_profiles["move"].max_duration)
        if not math.isfinite(duration) or duration < minimum or duration > maximum:
            raise DriverError("motion pulse is below the executable minimum")
        return duration

    def _motion_target(
        self,
        role: KeyRole,
        duration: float,
        frame: MotionFrame,
    ) -> tuple[float, float, float, float]:
        """Return direction, endpoint and uncertainty envelope for one pulse."""

        measured_speed = self._measured_motion_speed(role)
        planned_distance = measured_speed * duration
        travel_envelope = self._travel_envelope(planned_distance)
        if travel_envelope > MAX_WAYPOINT_DISTANCE:
            raise DriverError("motion pulse exceeds the bounded travel envelope")
        world_forward = self._world_forward_heading(frame)
        if role is KeyRole.MOVE_FORWARD:
            direction = world_forward
        elif role is KeyRole.MOVE_BACKWARD:
            if self.movement_basis is None or self.movement_basis.backward_heading_offset is None:
                raise DriverError("movement basis has no measured backward direction")
            direction = normalize_angle(
                self.movement_basis.facing_sign * frame.facing + self.movement_basis.backward_heading_offset
            )
        elif role is KeyRole.STRAFE_LEFT:
            direction = self._world_left_heading(frame)
        elif role is KeyRole.STRAFE_RIGHT:
            if self.movement_basis is None or self.movement_basis.strafe_right_heading_offset is None:
                raise DriverError("movement basis has no measured strafe-right direction")
            direction = normalize_angle(
                self.movement_basis.facing_sign * frame.facing
                + self.movement_basis.strafe_right_heading_offset
            )
        else:
            raise DriverError("motion pulse role is outside the measured movement basis")
        return (
            direction,
            frame.x + travel_envelope * math.cos(direction),
            frame.y + travel_envelope * math.sin(direction),
            travel_envelope,
        )

    def _plan_waypoint_pulse(
        self,
        distance: float,
        measured_speed: float,
        minimum_pulse: float,
        profile: Any,
        cumulative_travel: float,
        arrival_tolerance: float,
        heading: float = 0.0,
    ) -> float:
        """Return a pulse whose whole response envelope can settle the target.

        This conservative planner admits a single pulse aimed at the target.
        A target outside the calibrated response-uncertainty envelope has no
        guaranteed endpoint, so it is refused before route/camera admission.
        Inside the named translation heading allowance, admission also proves
        the complete two-dimensional miss at both response-envelope extremes.
        The cumulative two-unit envelope is checked before the caller can
        request a route or dispatch input.
        """

        minimum_distance = measured_speed * minimum_pulse
        guaranteed_distance = arrival_tolerance / TRAVEL_UNCERTAINTY_FRACTION
        if distance > guaranteed_distance + 1e-9:
            raise DriverError("waypoint exceeds the calibrated response-uncertainty envelope")
        if distance < minimum_distance:
            minimum_error = self._waypoint_endpoint_error(
                distance,
                minimum_distance,
                heading,
            )
            if minimum_error > arrival_tolerance + 1e-9:
                raise DriverError("waypoint minimum pulse cannot guarantee the target endpoint")
            pulse = minimum_pulse
        else:
            pulse = distance / measured_speed
        pulse = min(MAX_PULSE_SECONDS, profile.max_duration, pulse)
        pulse = max(minimum_pulse, pulse)
        planned_distance = measured_speed * pulse
        pulse_envelope = self._travel_envelope(planned_distance)
        remaining_distance = MAX_WAYPOINT_DISTANCE - cumulative_travel
        if pulse_envelope > remaining_distance + 1e-9:
            raise DriverError("waypoint pulse envelope exceeds the bounded local distance")
        endpoint_error = self._waypoint_endpoint_error(
            distance,
            planned_distance,
            heading,
        )
        if endpoint_error > arrival_tolerance + 1e-9:
            raise DriverError("waypoint pulse cannot guarantee the target endpoint")
        return pulse

    @staticmethod
    def _waypoint_endpoint_error(target_distance: float, planned_distance: float, heading: float) -> float:
        """Return the worst two-dimensional endpoint miss for one pulse."""

        if not all(math.isfinite(value) for value in (target_distance, planned_distance, heading)):
            raise DriverError("waypoint endpoint geometry is malformed")
        if target_distance <= 0.0 or planned_distance <= 0.0:
            raise DriverError("waypoint endpoint distance is malformed")
        uncertainty = max(MIN_BASIS_DISPLACEMENT, planned_distance * TRAVEL_UNCERTAINTY_FRACTION)
        heading_magnitude = min(math.pi, abs(heading) + MAX_CONFIRMATION_FACING_DRIFT)
        endpoint_miss = max(
            math.hypot(
                actual_distance * math.cos(heading_magnitude) - target_distance,
                actual_distance * math.sin(heading_magnitude),
            )
            for actual_distance in (planned_distance - uncertainty, planned_distance + uncertainty)
        )
        # Confirmation position drift is a full two-dimensional ball.  Its
        # worst vector can point along the endpoint-miss vector, so adding its
        # radius after the speed/facing envelope is the conservative exact
        # support function; shifting only the scalar target distance would
        # underbound diagonal and perpendicular confirmation movement.
        return endpoint_miss + MAX_CONFIRMATION_POSITION_DRIFT

    def _execute_motion_key_with_frame(
        self,
        role: KeyRole,
        duration: float,
        generation: int,
        frame: MotionFrame,
        lease_generation: int | None = None,
        *,
        route: RouteEvidence | None = None,
    ) -> MotionFrame:
        """Dispatch a pulse using the frame whose corridor was actually proved."""

        if lease_generation is None:
            lease_generation = self._require_motion_interaction(generation)
        self._require_native_gate(frame)
        if frame.chat_focused:
            self._invalidate_motion_admission()
            raise DriverError("motion pulse is refused while the chat edit box is focused")
        if self.movement_basis is None:
            raise DriverError("motion pulse requires measured movement basis")
        if (
            self.movement_basis.identity_fingerprint != frame.identity_fingerprint
            or self.movement_basis.boot != frame.boot
            or self.movement_basis.generation != generation
        ):
            raise DriverError("movement basis is stale for the native frame")
        if not isinstance(role, KeyRole):
            raise DriverError("motion pulse role is malformed")
        try:
            duration = float(duration)
        except (TypeError, ValueError) as exc:
            raise DriverError("motion pulse duration is malformed") from exc
        minimum = max(0.05, self._motion_profiles["move"].min_duration)
        if (
            not math.isfinite(duration)
            or duration < minimum
            or duration > min(MAX_PULSE_SECONDS, self._motion_profiles["move"].max_duration)
        ):
            raise DriverError("motion pulse is below the executable minimum")
        direction, target_x, target_y, travel_envelope = self._motion_target(role, duration, frame)
        operation_local = getattr(self, "_motion_deadline_local", None)
        operation_deadline = getattr(operation_local, "deadline", None)
        if operation_deadline is not None and (
            self._native_clock() + duration + MOTION_POST_RELEASE_RESERVE_SECONDS >= operation_deadline
        ):
            raise DriverError("motion pulse cannot fit the bounded intent deadline")
        if route is None:
            route = self._observe_route(
                generation,
                target_x,
                target_y,
                frame=frame,
            )
        if (
            not route.target_visible
            or not route.ground_clear
            or not self._route_matches_frame(
                route,
                frame,
                generation,
                target_x,
                target_y,
            )
            or self._native_clock() - route.capture_monotonic > MAX_FRAME_AGE
            or self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE
        ):
            raise DriverError("motion pulse corridor was not positively proved")
        route_frame = frame
        frame = self._confirm_motion_pose(generation, frame)
        self._require_native_gate(frame, matching=route_frame)
        if (
            not self._route_matches_frame(route, route_frame, generation, target_x, target_y)
            or self._native_clock() - route.capture_monotonic > MAX_FRAME_AGE
            or self._native_clock() - route_frame.capture_monotonic > MAX_FRAME_AGE
        ):
            raise DriverError("motion pulse route evidence expired before physical commit")
        if self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE:
            raise DriverError("motion pulse frame is stale at dispatch")
        if operation_deadline is not None and (
            self._native_clock() + duration + MOTION_POST_RELEASE_RESERVE_SECONDS >= operation_deadline
        ):
            raise DriverError("motion pulse cannot fit the bounded intent deadline")
        # Recheck the controller and lease immediately before the physical
        # commit.  Interaction stop may revoke while route/model confirmation
        # is in flight, so the earlier admission cannot authorize this call.
        lease_generation = self._require_motion_interaction(generation)
        self.driver.run_motion_key_hold(
            role,
            duration,
            frame,
            generation=lease_generation,
            deadline_monotonic=operation_deadline,
        )
        after = self._observe_motion(generation, previous=frame, after_release=True)
        self._require_native_gate(after, matching=frame)
        if after.speed > 0.25:
            self._invalidate_motion_admission()
            raise DriverError("motion pulse requires settled post-release feedback")
        if (
            after.identity_fingerprint != frame.identity_fingerprint
            or after.boot != frame.boot
            or after.sequence <= frame.sequence
            or after.capture_monotonic <= frame.capture_monotonic
            or abs(after.z - frame.z) > 0.25
            or after.instance != frame.instance
            or after.zone != frame.zone
        ):
            self._invalidate_motion_admission()
            raise DriverError("motion pulse changed scene or vertical position")
        moved_x = after.x - frame.x
        moved_y = after.y - frame.y
        moved = math.hypot(moved_x, moved_y)
        if moved <= MIN_BASIS_DISPLACEMENT:
            raise DriverError("motion pulse made no measurable progress")
        if moved > travel_envelope or moved > MAX_WAYPOINT_DISTANCE:
            raise DriverError("motion pulse exceeded its proved travel envelope")
        direction_component = moved_x * math.cos(direction) + moved_y * math.sin(direction)
        if direction_component <= max(MIN_BASIS_DISPLACEMENT, moved * 0.2):
            raise DriverError("motion pulse moved in the wrong direction")
        return after

    @_serialized_motion
    def execute_camera_delta(
        self,
        delta_pixels: int,
        *,
        generation: int,
        previous: MotionFrame | None = None,
    ) -> MotionFrame:
        if (
            isinstance(delta_pixels, bool)
            or not isinstance(delta_pixels, int)
            or not 1 <= abs(delta_pixels) <= 240
        ):
            raise DriverError("camera delta is outside the bounded range")
        if self.camera_mapping is None:
            raise DriverError("camera mapping has not passed controlled feedback calibration")
        raw_expected_yaw = self.camera_mapping.yaw_per_pixel * delta_pixels
        # Reject the requested angular motion before taking any viewport or
        # physical-input path.  Checking after the drag could leave a real
        # button sequence behind for a request we already know is invalid.
        if abs(raw_expected_yaw) > MAX_CAMERA_INPUT_YAW:
            raise DriverError("camera request exceeds the bounded angular motion")
        lease_generation = self._require_motion_interaction(generation)
        frame = self._observe_motion(generation, previous=previous)
        self._require_native_gate(frame, matching=previous)
        operation_local = getattr(self, "_motion_deadline_local", None)
        operation_deadline = getattr(operation_local, "deadline", None)
        identity = self._resolve_identity(
            require_foreground=True,
            deadline=operation_deadline,
        )
        if self.camera_mapping.identity_fingerprint != identity.fingerprint:
            raise DriverError("camera mapping identity is stale")
        if self.camera_mapping.generation != generation:
            raise DriverError("camera mapping generation is stale")
        if self.camera_mapping.boot and self.camera_mapping.boot != frame.boot:
            raise DriverError("camera mapping addon boot is stale")
        viewport = self._observe_viewport(generation, frame)
        confirmed = self._confirm_motion_pose(generation, frame)
        self._require_native_gate(confirmed, matching=frame)
        plan = self._camera_drag_plan(
            confirmed,
            delta_pixels,
            identity,
            lease_generation,
            viewport,
            session_generation=generation,
            evidence_frame=frame,
        )
        if operation_deadline is not None and (
            self._native_clock() + plan.duration + MOTION_POST_RELEASE_RESERVE_SECONDS >= operation_deadline
        ):
            raise DriverError("camera drag cannot fit the bounded intent deadline")
        if self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE:
            raise DriverError("camera drag frame is stale at dispatch")
        if operation_deadline is not None and (
            self._native_clock() + plan.duration + MOTION_POST_RELEASE_RESERVE_SECONDS >= operation_deadline
        ):
            raise DriverError("camera drag cannot fit the bounded intent deadline")
        lease_generation = self._require_motion_interaction(generation)
        self.driver.run_camera_drag(
            plan,
            generation=lease_generation,
            action_generation=generation,
            deadline_monotonic=operation_deadline,
            live_admission_check=lambda: self._require_live_button_action_authority(
                action_generation=generation,
                lease_generation=lease_generation,
                failure_message="camera action authority changed before physical drag",
            ),
        )
        after = self._observe_motion(generation, previous=confirmed, after_release=True)
        self._require_native_gate(after, matching=confirmed)
        if (
            after.identity_fingerprint != confirmed.identity_fingerprint
            or after.boot != confirmed.boot
            or after.sequence <= confirmed.sequence
            or after.capture_monotonic <= confirmed.capture_monotonic
            or after.instance != confirmed.instance
            or after.zone != confirmed.zone
            or abs(after.z - confirmed.z) > 0.25
            or math.hypot(after.x - confirmed.x, after.y - confirmed.y) > 0.01
        ):
            self._invalidate_motion_admission()
            raise DriverError("camera correction changed scene or vertical position")
        # Compare the response with the confirmed pose that was used for the
        # admitted drag.  Evidence-frame drift is not physical progress.
        observed_yaw = (after.facing - confirmed.facing + math.pi) % (2.0 * math.pi) - math.pi
        expected_yaw = raw_expected_yaw
        if observed_yaw * expected_yaw <= 0.0:
            self._invalidate_motion_admission()
            raise DriverError("camera response made no signed progress")
        meaningful_progress = max(0.001, abs(expected_yaw) * 0.25)
        if abs(observed_yaw) < meaningful_progress:
            self._invalidate_motion_admission()
            raise DriverError("camera response made insufficient signed progress")
        actual_cap = min(MAX_CAMERA_INPUT_YAW, MAX_CAMERA_YAW_PER_PIXEL * abs(delta_pixels))
        if abs(observed_yaw) > actual_cap:
            self._invalidate_motion_admission()
            raise DriverError("camera response exceeded the bounded angular motion")
        response_tolerance = max(
            MIN_CAMERA_RESPONSE_ABSOLUTE_ERROR,
            abs(expected_yaw) * MAX_CAMERA_RESPONSE_RELATIVE_ERROR,
        )
        if abs(observed_yaw - expected_yaw) > response_tolerance:
            self._invalidate_motion_admission()
            raise DriverError("camera response exceeded its calibrated error bound")
        return after

    @_serialized_motion
    def execute_waypoint(self, target_x: float, target_y: float, *, generation: int) -> MotionFrame:
        self._validate_waypoint_request(target_x, target_y)
        self._require_motion_interaction(generation)
        if self.camera_mapping is None:
            raise DriverError("waypoint requires a calibrated camera mapping")
        if self.movement_basis is None:
            raise DriverError("waypoint requires a measured movement basis")
        if self.camera_mapping.generation != generation:
            raise DriverError("camera mapping generation is stale")
        if self.movement_basis.generation != generation:
            raise DriverError("movement basis generation is stale")
        self._require_supported_route_handedness(self.movement_basis)
        started = self._native_clock()
        frame = self._observe_motion(generation)
        self._require_native_gate(frame)
        if self.camera_mapping.identity_fingerprint != frame.identity_fingerprint:
            raise DriverError("camera mapping identity is stale")
        translation_stalls = 0
        camera_turn_attempts = 0
        pulses = 0
        cumulative_travel = 0.0
        profile = self._motion_profiles["move"]
        measured_speed = self._measured_motion_speed(KeyRole.MOVE_FORWARD)
        if measured_speed <= 0.0 or not math.isfinite(measured_speed):
            raise DriverError("movement basis has no measured speed")
        minimum_pulse = max(0.05, profile.min_duration)
        min_pulse_displacement = max(
            MIN_BASIS_DISPLACEMENT,
            measured_speed * minimum_pulse,
        )
        if min_pulse_displacement > MAX_WAYPOINT_DISTANCE:
            raise DriverError("movement basis minimum pulse exceeds the local distance bound")
        arrival_tolerance = max(0.1, min_pulse_displacement * 0.5)
        while self._native_clock() - started < MAX_WAYPOINT_SECONDS and pulses < MAX_WAYPOINT_PULSES:
            self._require_motion_interaction(generation)
            heading, distance = waypoint_heading_error(frame, target_x, target_y, self.movement_basis)
            if distance <= arrival_tolerance:
                return frame

            # Preflight the target before either a camera correction or a
            # translation route is admitted.  A camera turn cannot make an
            # otherwise unguaranteed translation endpoint safe.
            admission_heading = math.copysign(
                min(abs(heading), WAYPOINT_TRANSLATION_HEADING_TOLERANCE),
                heading,
            )
            pulse = self._plan_waypoint_pulse(
                distance,
                measured_speed,
                minimum_pulse,
                profile,
                cumulative_travel,
                arrival_tolerance,
                admission_heading,
            )
            if abs(heading) > WAYPOINT_TRANSLATION_HEADING_TOLERANCE:
                pixels = int(round(heading / self.camera_mapping.yaw_per_pixel))
                pixels = max(-120, min(120, pixels))
                if pixels == 0:
                    raise DriverError("camera mapping cannot express the required heading")
                if camera_turn_attempts >= MAX_WAYPOINT_CAMERA_TURNS:
                    raise DriverError("waypoint exceeded its camera correction attempt budget")
                frame = self.execute_camera_delta(pixels, generation=generation, previous=frame)
                camera_turn_attempts += 1
            else:
                _, route_target_x, route_target_y, _ = self._motion_target(
                    KeyRole.MOVE_FORWARD,
                    pulse,
                    frame,
                )
                route = self._observe_route(
                    generation,
                    route_target_x,
                    route_target_y,
                    frame=frame,
                )
                if not route.target_visible or not route.ground_clear:
                    raise DriverError("route was not positively proved for translation")
                if (
                    not self._route_matches_frame(
                        route,
                        frame,
                        generation,
                        route_target_x,
                        route_target_y,
                    )
                    or self._native_clock() - route.capture_monotonic > MAX_FRAME_AGE
                    or self._native_clock() - frame.capture_monotonic > MAX_FRAME_AGE
                ):
                    raise DriverError("translation route evidence is stale")
                before_move = frame
                lease_generation = self._require_motion_interaction(generation)
                frame = self._execute_motion_key_with_frame(
                    KeyRole.MOVE_FORWARD,
                    pulse,
                    generation,
                    before_move,
                    lease_generation,
                    route=route,
                )
                moved_x = frame.x - before_move.x
                moved_y = frame.y - before_move.y
                moved = math.hypot(moved_x, moved_y)
                cumulative_travel += moved
                if cumulative_travel > MAX_WAYPOINT_DISTANCE:
                    raise DriverError("waypoint cumulative travel exceeded its local bound")
                desired_heading = self._world_forward_heading(before_move)
                forward_component = moved_x * math.cos(desired_heading) + moved_y * math.sin(desired_heading)
                if moved <= MIN_BASIS_DISPLACEMENT or forward_component <= max(
                    MIN_BASIS_DISPLACEMENT, moved * 0.2
                ):
                    raise DriverError("waypoint moved in the wrong direction or made no progress")
                if abs(frame.z - before_move.z) > 0.25:
                    raise DriverError("waypoint encountered abnormal vertical movement")
                # Compare this pulse's endpoint with the distance that was
                # proved immediately before its dispatch. This counts the
                # first ineffective pulse and gives exactly three attempts
                # before the translation-stall budget is exhausted.
                post_pulse_distance = math.hypot(frame.x - target_x, frame.y - target_y)
                if post_pulse_distance >= distance - 0.005:
                    translation_stalls += 1
                    if translation_stalls >= 3:
                        raise DriverError("waypoint made no measurable progress")
                else:
                    translation_stalls = 0
            pulses += 1
        raise DriverError("waypoint did not settle before its bounded deadline")

    @staticmethod
    def _validate_waypoint_request(target_x: float, target_y: float) -> None:
        """Reject malformed target coordinates before native observation."""

        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in (target_x, target_y)
        ):
            raise DriverError("waypoint target coordinates are malformed")

    def _recover_login(self) -> None:
        if self.recovery_attempts >= 4:
            raise ModelError("bounded login recovery attempt budget exhausted")
        decision = self._call_model_adapter(
            "decide",
            self._capture_model(),
            target="game-client",
            verified_crop=self.config.profile.model_crop,
        )
        if decision.action in self.recovery_actions:
            raise ModelError(f"login recovery action {decision.action} exceeded its one-attempt budget")
        # Consume and persist the named-action retry budget before dispatch.
        # A crash or guardian revoke after intent persistence must never replay
        # the same recovery click on the next process.
        with self.controller.lock:
            self.controller.record_recovery_attempt(decision.action)
        self.recovery_attempts = self.controller.journal.read().get(
            "recoveryAttempts", self.recovery_attempts
        )
        self.recovery_actions = set(
            self.controller.journal.read().get("recoveryActions", self.recovery_actions)
        )
        self._run_action(decision.action, self.last_snapshot, target="game-client")

    def _claim_motion_fence(self) -> threading.RLock | None:
        """Claim the one capture/decoder/physical-action ownership fence.

        The recurring engine must yield immediately when an interaction intent
        already owns the fence.  Interaction stop does not call this helper;
        it revokes the guardian directly so cancellation cannot wait behind a
        capture, model call, or held native process.
        """

        lock = getattr(self, "_motion_intent_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._motion_intent_lock = lock
        if not lock.acquire(blocking=False):
            return None
        return lock

    def capture_paused_region_sample(
        self,
        *,
        expected_generation: int,
        parent_started: float,
        deadline: float,
        config_file_sha256: str,
        config_semantic_sha256: str,
    ) -> dict[str, Any]:
        """Capture one sample and always restore this thread's timing sink."""

        outer_token = _set_identity_latency_sink(None)
        try:
            return self._capture_paused_region_sample_impl(
                expected_generation=expected_generation,
                parent_started=parent_started,
                deadline=deadline,
                config_file_sha256=config_file_sha256,
                config_semantic_sha256=config_semantic_sha256,
            )
        finally:
            _reset_identity_latency_sink(outer_token)

    def _capture_paused_region_sample_impl(
        self,
        *,
        expected_generation: int,
        parent_started: float,
        deadline: float,
        config_file_sha256: str,
        config_semantic_sha256: str,
    ) -> dict[str, Any]:
        """Capture one bounded, read-only region sample through this owner."""

        latency_collector = _IdentityLatencyCollector(
            config_file_sha256=config_file_sha256,
            config_semantic_sha256=config_semantic_sha256,
        )
        latency_token = _set_identity_latency_sink(latency_collector)
        latency_collector.instant("sample_enter")
        owner_entered = time.monotonic()
        request_dir: Path | None = None
        output: Path | None = None
        phase = "request_validation"
        published = False
        sample: dict[str, Any] | None = None
        failure: BaseException | None = None
        interrupted: BaseException | None = None
        failure_record: dict[str, Any] = {}
        capture_returned: float | None = None
        motion_fence = None
        source_binding: dict[str, str] | None = None
        model_calls_before: dict[str, int] | None = None
        model_calls_after: dict[str, int] | None = None
        model_call_delta: dict[str, int] | None = None
        response_ready: float | None = None
        driver = self.driver
        backend = driver.backend
        guardian = driver.guardian
        owner_config_semantic_sha256 = self._loaded_config_semantic_sha256
        if isinstance(getattr(backend, "last_region_attempt", None), dict):
            backend.last_region_attempt = {
                "stage": "not_started",
                "request_started": None,
                "lock_acquired": None,
                "ack_sent": None,
                "capture_started": None,
                "capture_returned": None,
                "attempt_retired": None,
            }
        if isinstance(self._runtime_source_binding, dict):
            source_binding = dict(self._runtime_source_binding)
        model_calls_before = self._model_call_snapshot()
        try:
            if (
                isinstance(expected_generation, bool)
                or not isinstance(expected_generation, int)
                or expected_generation <= 0
            ):
                raise DriverError("paused region generation must be a positive integer")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in (parent_started, deadline)
            ):
                raise DriverError("paused region timestamps must be finite monotonic numbers")
            parent_started = float(parent_started)
            deadline = float(deadline)
            for label, digest in (
                ("config file hash", config_file_sha256),
                ("semantic config hash", config_semantic_sha256),
            ):
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise DriverError(f"paused region {label} is malformed")
            if not _driver_profile_matches_config(driver, self.config):
                phase = "config_binding"
                raise DriverError("owning driver profile does not match the daemon config")
            if config_semantic_sha256 != owner_config_semantic_sha256:
                phase = "config_binding"
                raise DriverError("paused region semantic config hash does not match the owner config")
            now = time.monotonic()
            if parent_started > now or now - parent_started > 1.0:
                raise DriverError("paused region parent start is future or expired")
            if deadline <= now or deadline > parent_started + 1.0:
                raise DriverError("paused region absolute deadline is expired or exceeds one second")

            phase = "owner_preconditions"
            motion_fence = self._claim_motion_fence()
            if motion_fence is None:
                raise DriverError("another motion or capture intent is already in flight")
            with self.controller.lock:
                self._check_paused_region_admission(expected_generation)

            if not isinstance(backend, X11Backend):
                raise DriverError("paused region sample requires the configured production X11 backend")
            if backend._cancel_active.is_set():
                raise DriverError(
                    "paused region sample refuses a capture-revoked backend; start a fresh owner"
                )
            if not guardian.lease.held:
                raise DriverError("owning daemon does not hold the canonical desktop lease")
            if guardian.armed or guardian.held:
                raise DriverError("paused region sample requires an unarmed, unheld Guardian")
            if guardian.active_process_groups or guardian.observer_process_groups:
                raise DriverError("paused region sample refuses another owned process group")
            if backend._active_processes or backend.active_process_groups:
                raise DriverError("paused region sample refuses another backend process")
            if self._native_bootstrap_ready.is_set():
                raise DriverError("paused region sample refuses pending native bootstrap evidence")
            calibration = self._calibration_operation_snapshot()
            if calibration.get("state") in {"starting", "running"}:
                raise DriverError("paused region sample refuses an active calibration operation")
            calibration_thread = self._calibration_thread
            if calibration_thread is not None and calibration_thread.is_alive():
                raise DriverError("paused region sample refuses a live calibration worker")
            if guardian._watchdog_failed:
                raise DriverError("paused region sample refuses an unhealthy input watchdog")
            guardian.admission_gate_ready()
            profile = driver.profile
            executable = profile.telemetry_region_executable
            executable_hash = profile.telemetry_region_executable_sha256
            if executable is None or executable_hash is None:
                raise DriverError("paused region sample requires a configured telemetry producer")
            profile.validate()

            root = Path(self.config.capture_dir)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            request_dir = Path(tempfile.mkdtemp(prefix="paused-region-", dir=root))
            os.chmod(request_dir, 0o700)
            latency_collector.bind(request_dir)
            latency_collector.set_producer(executable_hash)
            output = request_dir / "capture.png"
            # A request rejected before the native producer starts must not
            # inherit the previous request's protocol-stage timestamps.
            backend.last_region_attempt = {
                "stage": "not_started",
                "request_started": None,
                "lock_acquired": None,
                "ack_sent": None,
                "capture_started": None,
                "capture_returned": None,
                "attempt_retired": None,
            }
            phase = "source_binding"
            with _identity_latency_span("source_binding_before"):
                source_binding = self._runtime_source_binding_for_diagnostic()
            latency_collector.set_bindings(source_binding, executable_hash)
            _identity_latency_mark("source_binding_before")

            phase = "identity_before_capture"
            if time.monotonic() >= deadline:
                raise DriverError("paused region deadline expired before identity resolution")
            with _identity_latency_barrier("daemon_pre"):
                identity = driver.resolve_identity(deadline_monotonic=deadline)
            identity_record = {
                "pid": identity.pid,
                "process_start": identity.process_start,
                "xid": identity.xid,
                "child_xid": identity.child_xid,
                "executable": identity.executable,
                "display": identity.display,
                "fingerprint": identity.fingerprint,
                "child_geometry": list(identity.child_geometry),
            }
            before_receipt = backend.last_region_receipt

            def admit_observer(start):
                self._check_paused_region_config_binding(
                    driver, owner_config_semantic_sha256, config_semantic_sha256
                )
                with self.controller.lock:
                    self._check_paused_region_admission(expected_generation)
                    return guardian.observer_dispatch_admission(start)

            def unregister_observer(pgid: int) -> None:
                guardian.unregister_observer_process(pgid)

            phase = "region_capture"
            driver.capture_native_region_for_identity(
                identity,
                destination=output,
                deadline_monotonic=deadline,
                observer_admission=admit_observer,
                observer_reaped=unregister_observer,
            )
            capture_returned = time.monotonic()
            published = output.is_file() and not output.is_symlink()
            if not published:
                phase = "png_publication"
                raise DriverError("paused region producer returned without its PNG publication")
            output_bytes = output.read_bytes()
            if not output_bytes or len(output_bytes) > 131072:
                phase = "png_publication"
                raise DriverError("paused region PNG exceeds its bounded output size")
            receipt = backend.last_region_receipt
            if receipt is None or receipt is before_receipt:
                raise DriverError("paused region capture has no new actual producer receipt")
            actual_receipt = asdict(receipt)
            actual_receipt["rectangle"] = list(receipt.rectangle)
            if (
                receipt.executable_sha256 != executable_hash
                or receipt.output_sha256 != hashlib.sha256(output_bytes).hexdigest()
                or receipt.output_bytes != len(output_bytes)
                or receipt.attempt_retired is not True
            ):
                phase = "producer_binding"
                raise DriverError("paused region producer receipt does not bind its published PNG")
            with _identity_latency_span("cleanup"):
                cleanup = self._paused_region_cleanup_snapshot(output, backend, guardian)
            source = {
                "api": "org.gnome.Shell.Screenshot.ScreenshotArea",
                "branch": receipt.owner_branch,
                "rectangle": list(receipt.rectangle),
            }
            config_binding = {
                "request_config_file_sha256": config_file_sha256,
                "request_config_semantic_sha256": config_semantic_sha256,
                "owner_config_semantic_sha256": owner_config_semantic_sha256,
                "display": profile.display,
                "canvas_crop": list(profile.canvas_crop),
            }
            producer_binding = {"path": str(executable), "sha256": executable_hash}
            sample = {
                "generation": expected_generation,
                "parent_started": parent_started,
                "deadline": deadline,
                "owner_entered": owner_entered,
                "capture_returned": capture_returned,
                "identity": identity_record,
                "identity_after": None,
                "source": source,
                "config": config_binding,
                "producer": producer_binding,
                "receipt": actual_receipt,
                "png_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "png_bytes": len(output_bytes),
                "png_base64": base64.b64encode(output_bytes).decode("ascii"),
                "artifact_directory": str(request_dir),
                "artifact_png": str(output),
                "cleanup": cleanup,
                "source_binding": source_binding,
                "model_calls_before": model_calls_before,
                "model_calls_after": None,
                "model_call_delta": None,
                "response_ready": None,
            }
            if time.monotonic() >= deadline:
                raise DriverError("paused region deadline expired before response serialization")
            phase = "owner_post_capture_check"
            with _identity_latency_barrier("daemon_post"):
                identity_after = driver.resolve_identity(deadline_monotonic=deadline)
            _identity_latency_instant("identity_after_daemon")
            if identity_after.fingerprint != identity.fingerprint:
                raise DriverError("paused region client identity changed during capture")
            try:
                with _identity_latency_span("config_check"):
                    self._check_paused_region_config_binding(
                        driver, owner_config_semantic_sha256, config_semantic_sha256
                    )
            except Exception:
                phase = "config_binding"
                raise
            with self.controller.lock:
                self._check_paused_region_admission(expected_generation)
            if guardian.armed or not guardian.lease.held:
                raise DriverError("paused region capture changed the owning lease or armed state")
            guardian.admission_gate_ready()
            with _identity_latency_span("cleanup"):
                cleanup = self._paused_region_cleanup_snapshot(output, backend, guardian)
            if cleanup["empty"] is not True:
                phase = "cleanup"
                raise DriverError("paused region capture did not retire its owner process and attempts")
            sample["identity_after"] = identity_after.fingerprint
            sample["cleanup"] = cleanup
            phase = "owner_receipt_persist"
        except BaseException as exc:
            failure = exc
            interrupted = exc if not isinstance(exc, Exception) else None
        finally:
            if request_dir is not None:
                backend = self.driver.backend
                guardian = self.driver.guardian
                with _identity_latency_span("cleanup"):
                    cleanup = self._paused_region_cleanup_snapshot(output, backend, guardian)
                if failure is None and source_binding is not None:
                    try:
                        with _identity_latency_span("source_binding_after"):
                            source_after = self._runtime_source_binding_for_diagnostic()
                        if source_after != source_binding:
                            raise DriverError("paused region diagnostic source binding drifted")
                    except BaseException as source_error:
                        failure = source_error
                        phase = "source_binding"
                        if interrupted is None and not isinstance(source_error, Exception):
                            interrupted = source_error
                # The after snapshot closes the full owner interval, including
                # the final source check.  A concurrent model call during that
                # last verification must remain attributable to this sample.
                model_calls_after = self._model_call_snapshot()
                if model_calls_before is not None:
                    model_call_delta = {
                        key: model_calls_after[key] - model_calls_before[key]
                        for key in ("started", "succeeded", "failed")
                    }
                response_ready = time.monotonic()
                latency_collector.instant("response_ready")
                latency_collector.mark("response_ready")
                owner_returned = time.monotonic()
                producer_attempt = dict(backend.last_region_attempt)
                if producer_attempt.get("stage") == "not_started" and isinstance(cleanup.get("empty"), bool):
                    # A pre-admission refusal has no producer receipt to retire.
                    # Its terminal cleanup ledger is the only observation that
                    # can truthfully establish that no attempt remains.
                    producer_attempt["attempt_retired"] = cleanup["empty"]
                if sample is not None:
                    sample["owner_returned"] = owner_returned
                    sample["cleanup"] = cleanup
                    sample["model_calls_after"] = model_calls_after
                    sample["model_call_delta"] = model_call_delta
                    sample["response_ready"] = response_ready
                failure_record = {
                    "phase": phase,
                    "error": f"{type(failure).__name__}: {failure}" if failure else "",
                    "owner_entered": owner_entered,
                    "owner_returned": owner_returned,
                    "artifact_directory": str(request_dir),
                    "receipt_path": str(request_dir / "receipt.json"),
                    "published_png": str(output)
                    if published and output is not None and output.is_file()
                    else None,
                    "producer_attempt": producer_attempt,
                    "cleanup": cleanup,
                    "generation": expected_generation,
                    "config_file_sha256": config_file_sha256,
                    "config_semantic_sha256": config_semantic_sha256,
                    "owner_config_semantic_sha256": owner_config_semantic_sha256,
                    "lease_held": guardian.lease.held,
                    "armed": guardian.armed,
                    "source_binding": source_binding,
                    "model_calls_before": model_calls_before,
                    "model_calls_after": model_calls_after,
                    "model_call_delta": model_call_delta,
                }
                if failure is not None:
                    failure_record["sample"] = sample
                    failure_record["response_ready"] = response_ready
                    try:
                        self._persist_paused_region_receipt(
                            request_dir / "receipt.json", {"ok": False, "failure": failure_record}
                        )
                    except Exception as persist_error:
                        failure_record["receipt_persist_error"] = str(persist_error)
            else:
                cleanup = None
                try:
                    cleanup = self._paused_region_cleanup_snapshot(output, backend, guardian)
                except Exception:
                    cleanup = {
                        "backend_processes": None,
                        "backend_groups": None,
                        "guardian_observer_groups": None,
                        "guardian_active_groups": None,
                        "attempt_directories": None,
                        "lease_held": None,
                        "watchdog_healthy": None,
                        "armed": None,
                        "empty": False,
                    }
                model_calls_after = self._model_call_snapshot()
                model_call_delta = {
                    key: model_calls_after[key] - model_calls_before[key]
                    for key in ("started", "succeeded", "failed")
                }
                response_ready = time.monotonic()
                owner_returned = time.monotonic()
                if failure is not None:
                    producer_attempt = dict(getattr(backend, "last_region_attempt", {}))
                    if producer_attempt.get("stage") == "not_started" and isinstance(
                        cleanup.get("empty"), bool
                    ):
                        producer_attempt["attempt_retired"] = cleanup["empty"]
                    failure_record = {
                        "phase": phase,
                        "error": f"{type(failure).__name__}: {failure}",
                        "owner_entered": owner_entered,
                        "owner_returned": owner_returned,
                        "artifact_directory": None,
                        "receipt_path": None,
                        "published_png": None,
                        "producer_attempt": producer_attempt,
                        "cleanup": cleanup,
                        "generation": expected_generation,
                        "config_file_sha256": config_file_sha256,
                        "config_semantic_sha256": config_semantic_sha256,
                        "owner_config_semantic_sha256": owner_config_semantic_sha256,
                        "lease_held": guardian.lease.held,
                        "armed": guardian.armed,
                        "source_binding": source_binding,
                        "model_calls_before": model_calls_before,
                        "model_calls_after": model_calls_after,
                        "model_call_delta": model_call_delta,
                        "response_ready": response_ready,
                    }
            if motion_fence is not None:
                motion_fence.release()
        if failure is None:
            assert sample is not None and request_dir is not None
            try:
                success_receipt = self._paused_region_success_receipt(sample)
                self._persist_paused_region_receipt(request_dir / "receipt.json", success_receipt)
            except Exception as persist_error:
                failure = persist_error
                failure_record["phase"] = "owner_receipt_persist"
                failure_record["error"] = f"{type(persist_error).__name__}: {persist_error}"
                failure_record["sample"] = sample
                sample["response_ready"] = response_ready
                failure_record["response_ready"] = response_ready
                try:
                    self._persist_paused_region_receipt(
                        request_dir / "receipt.json", {"ok": False, "failure": failure_record}
                    )
                except Exception:
                    pass
        # Diagnostic publication is strictly after the causal receipt and
        # cleanup.  It may fail, but it must never mask that receipt or leave
        # the owner thread's ContextVar installed for its next request.
        try:
            try:
                latency_collector.finish()
            except BaseException:
                pass
            if request_dir is not None:
                try:
                    latency_collector.write(request_dir / "identity-latency.json")
                except BaseException:
                    try:
                        latency_collector.write_failure_token(request_dir)
                    except BaseException:
                        pass
        finally:
            _reset_identity_latency_sink(latency_token)
        if interrupted is not None:
            raise interrupted
        if failure is not None:
            result: dict[str, Any] = {"ok": False, "error": f"{type(failure).__name__}: {failure}"}
            if sample is not None:
                result["sample"] = sample
            if failure_record:
                result["failure"] = {
                    key: (
                        {name: item for name, item in value.items() if name != "png_base64"}
                        if key == "sample" and isinstance(value, dict)
                        else value
                    )
                    for key, value in failure_record.items()
                }
            return result
        assert sample is not None
        return {"ok": True, "sample": sample}

    def _check_paused_region_config_binding(
        self, driver: Any, initial_sha256: str, requested_sha256: str
    ) -> None:
        current_sha256 = session_config_semantic_sha256(self.config)
        if (
            current_sha256 != self._loaded_config_semantic_sha256
            or current_sha256 != initial_sha256
            or current_sha256 != requested_sha256
        ):
            raise DriverError("paused region semantic config changed during capture")
        if not _driver_profile_matches_config(driver, self.config):
            raise DriverError("owning driver profile no longer matches the daemon config")

    def _check_paused_region_admission(self, expected_generation: int) -> None:
        status = self.controller.status()
        if status.state != ControllerState.PAUSED or status.generation != expected_generation:
            raise DriverError("paused region owner state or generation changed")
        if status.stop_requested or status.closeout_requested or status.action_intent:
            raise DriverError("paused region owner has an active lifecycle or action intent")
        if status.mode.value == "interaction":
            raise DriverError("paused region owner refuses interaction mode")
        if self._native_bootstrap_ready.is_set():
            raise DriverError("paused region owner has pending native bootstrap evidence")
        if self._native_construction_blocks_competing_entry():
            raise DriverError("paused region owner has an active native construction admission")
        if self._native_baseline_ready.is_set():
            raise DriverError("paused region owner has active native baseline readiness")
        operation = self._calibration_operation_snapshot()
        if operation.get("state") in {"starting", "running"}:
            raise DriverError("paused region owner has an active calibration operation")

    @staticmethod
    def _paused_region_cleanup_snapshot(output: Path | None, backend: Any, guardian: Any) -> dict[str, Any]:
        attempts = []
        if output is not None:
            attempts = sorted(
                {
                    str(path)
                    for pattern in (
                        f".{output.name}.telemetry-*",
                        f"..{output.name}.telemetry-*.retired-*",
                    )
                    for path in output.parent.glob(pattern)
                }
            )
        active = len(backend._active_processes)
        groups = sorted(backend.active_process_groups)
        observer_groups = sorted(guardian.observer_process_groups)
        guardian_groups = sorted(guardian.active_process_groups)
        lease_held = guardian.lease.held
        watchdog_healthy = not guardian._watchdog_failed
        return {
            "backend_processes": active,
            "backend_groups": groups,
            "guardian_observer_groups": observer_groups,
            "guardian_active_groups": guardian_groups,
            "attempt_directories": attempts,
            "lease_held": lease_held,
            "watchdog_healthy": watchdog_healthy,
            "armed": guardian.armed,
            "empty": active == 0
            and groups == []
            and not observer_groups
            and not guardian_groups
            and not attempts,
        }

    @staticmethod
    def _paused_region_success_receipt(sample: dict[str, Any]) -> dict[str, Any]:
        expected_fields = {
            "generation",
            "parent_started",
            "deadline",
            "owner_entered",
            "capture_returned",
            "identity",
            "identity_after",
            "source",
            "config",
            "producer",
            "receipt",
            "png_sha256",
            "png_bytes",
            "png_base64",
            "artifact_directory",
            "artifact_png",
            "cleanup",
            "owner_returned",
            "source_binding",
            "model_calls_before",
            "model_calls_after",
            "model_call_delta",
            "response_ready",
        }
        if set(sample) != expected_fields:
            raise DriverError("paused region success sample has an incomplete final schema")
        timestamps = (
            sample["owner_entered"],
            sample["capture_returned"],
            sample["response_ready"],
            sample["owner_returned"],
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in timestamps
        ) or not (
            sample["owner_entered"]
            <= sample["capture_returned"]
            <= sample["response_ready"]
            <= sample["owner_returned"]
        ):
            raise DriverError("paused region success timestamps are incomplete or out of order")
        receipt = {"ok": True, **sample}
        encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > 196608:
            raise DriverError("paused region final receipt exceeds its durable size bound")
        if len(json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("utf-8")) > 480000:
            raise DriverError("paused region response metadata exceeds its bound")
        return receipt

    @staticmethod
    def _persist_paused_region_receipt(path: Path, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > 196608:
            raise DriverError("paused region receipt exceeds its size bound")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _reconcile_completed_logout_without_agent(self, *, generation: int) -> bool:
        """Resolve a completed logout from read-only character-select evidence.

        Logout flushes SavedVariables and removes the Agent surface, so the
        postcondition cannot include a fabricated boot or telemetry frame.  A
        fresh named character-select observation plus stable empty files is the
        complete proof; otherwise recovery must remain pending and may not
        issue a relogin action.
        """

        with self.controller.lock:
            status = self.controller.status()
            if status.state != ControllerState.RECONCILE or status.generation != generation:
                return False
            if self.controller.journal.read().get("restartUncertain") is not True:
                return False
            if status.action_intent != "logout":
                return False
            fence = self.controller.observation_fence()
        character_select = getattr(self.driver, "character_select_observed", None)
        try:
            observed_character_select = callable(character_select) and character_select()
        except (DriverError, EvidenceError, OSError, ValueError):
            return False
        if not observed_character_select:
            return False
        if not all((self.config.saved_variables, self.config.companion_state, self.config.data_lua)):
            raise EvidenceError("completed logout reconciliation paths are not configured")
        assert self.config.saved_variables is not None
        assert self.config.companion_state is not None
        assert self.config.data_lua is not None
        try:
            stable_empty = queues_empty(
                self.config.saved_variables,
                self.config.companion_state,
                self.config.data_lua,
                settled_uid=status.last_uid or None,
            )
        except (EvidenceError, OSError, ValueError):
            return False
        if not stable_empty:
            return False
        with self.controller.lock:
            current = self.controller.status()
            if current.generation != generation or current.state != ControllerState.RECONCILE:
                return False
            self.controller.resolve_restart_intent(
                observed_boot="",
                observed_uid="",
                uploaded_ids=set(),
                data_ack_ids=set(),
                loaded_ids=set(),
                pending_ids=set(),
                scanner_phase=None,
                scanner_idle=False,
                stable_scan_ids=frozenset(),
                stable_sweep_ids=frozenset(),
                stable_format2_verified=False,
                stable_files_complete=True,
                observed_scene="character_select",
                expected_generation=current.generation,
                expected_state=ControllerState.RECONCILE,
                fence=fence,
            )
        return self.controller.status().state == ControllerState.STOPPED

    def _reconcile_menu_intent_without_agent(self, *, generation: int) -> bool:
        """Consume a persisted menu intent from one fresh visual observation.

        Disconnect and character-select screens do not publish the Agent
        surface.  A missing frame therefore cannot authorize login recovery or
        replay the uncertain click.  The driver's named observer captures the
        current client and binds the scene facts to the verified window
        identity; the controller then clears only the already-persisted intent.
        A failed or mismatched observation deliberately leaves RECONCILE in
        place for the next owner poll.
        """

        menu_states = {
            "dismiss_disconnect": "game_client",
            "reconnect_game": "character_select",
            "select_arthur": "arthur_selected",
        }
        with self.controller.lock:
            status = self.controller.status()
            if status.state != ControllerState.RECONCILE or status.generation != generation:
                return False
            if self.controller.journal.read().get("restartUncertain") is not True:
                return False
            expected_state = menu_states.get(status.action_intent)
            if expected_state is None:
                return False
            fence = self.controller.observation_fence()

        observe = getattr(self.driver, "observe_named_state", None)
        if not callable(observe):
            return True
        try:
            observation = observe(expected_state)
        except (DriverError, OSError, ValueError):
            # A stale scene or a transient capture is still an unresolved
            # external effect.  Never fall through to _recover_login.
            return True
        if not isinstance(observation, NamedStateObservation):
            return True
        facts = dict(observation.facts)
        if observation.state != expected_state:
            return True
        if observation.identity_fingerprint != self.driver.resolve_identity().fingerprint:
            return True
        scene = "character_select" if expected_state == "arthur_selected" else expected_state
        observed_modal = facts.get("modal", "")
        observed_character = facts.get("character", "")
        selected_arthur = expected_state == "arthur_selected"
        # The named visual producer is the positive postcondition.  It is
        # capture-bound and identity-bound; a menu scene has no Agent boot, so
        # this consumer must never copy a stale telemetry boot or synthesize a
        # new one.  The controller's menu-intent branch accepts this explicit
        # no-Agent observation boundary.
        with self.controller.lock:
            current = self.controller.status()
            if current.generation != generation or current.state != ControllerState.RECONCILE:
                return True
            self.controller.resolve_restart_intent(
                observed_boot="",
                observed_uid="",
                uploaded_ids=set(),
                data_ack_ids=set(),
                loaded_ids=set(),
                pending_ids=set(),
                scanner_phase=None,
                scanner_idle=False,
                stable_scan_ids=frozenset(),
                stable_sweep_ids=frozenset(),
                stable_format2_verified=False,
                stable_files_complete=False,
                observed_scene=scene,
                observed_modal=observed_modal,
                observed_character=observed_character,
                selected_arthur=selected_arthur,
                action_postcondition=True,
                expected_generation=current.generation,
                expected_state=ControllerState.RECONCILE,
                fence=fence,
            )
        return True

    @staticmethod
    def _command_action_kind(command: str, state: ControllerState) -> str:
        if command == "/reload":
            if state in {ControllerState.ACK_RELOAD_SENT, ControllerState.ACK_RELOAD_WAIT}:
                return "ack_reload"
            return "save_reload"
        mapping = {
            "/crowley scan": "scan",
            "/crowley session fence": "pre_logout_fence",
            "/logout": "logout",
        }
        try:
            return mapping[command]
        except KeyError as exc:
            raise DriverError(f"unsupported command {command!r}") from exc

    def _action_deadline(self, action: str) -> int:
        if action == "scan":
            return self.config.sweep_cap_seconds
        if action == "save_reload":
            return self.config.reload_deadline_seconds
        if action in {"ack_reload", "logout", "pre_logout_fence"}:
            return self.config.ack_deadline_seconds
        return self.config.reload_deadline_seconds

    def _fresh_status_after(
        self,
        identity: WindowIdentity,
        *,
        previous: Any | None = None,
        timeout: float = 8.0,
        cancellation_check: Callable[[], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[dict[str, Any], Any, float]:
        """Read one advancing ordinary CRS1 frame bound to the same game."""

        deadline_clock = (
            self._mail_cycle_clock
            if deadline_monotonic is not None and self.controller.mail_cycle_capability() is not None
            else time.monotonic
        )
        now = deadline_clock()
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(float(deadline_monotonic))
            ):
                raise DriverError("fresh status deadline is malformed")
            deadline = min(now + timeout, float(deadline_monotonic))
        else:
            deadline = now + timeout
        if deadline <= now:
            raise DriverError("fresh status deadline expired")
        while deadline_clock() < deadline:
            if cancellation_check is not None:
                cancellation_check()
            sample = self.capture_status(deadline=deadline)
            if cancellation_check is not None:
                cancellation_check()
            if sample.window_identity != identity.fingerprint:
                raise DriverError("fresh command telemetry belongs to another game identity")
            if sample.kind == "NO_CHANGE":
                time.sleep(min(0.05, max(0.0, deadline - deadline_clock())))
                continue
            if sample.kind != "FRESH" or sample.frame is None:
                raise DriverError("command effect lacks a valid advancing CRS1 frame")
            frame = sample.frame
            snapshot = frame.snapshot.value
            render = snapshot.get("render") if isinstance(snapshot, dict) else None
            native_ordinal = render.get("nativeOrdinal") if isinstance(render, dict) else None
            if (
                not isinstance(render, dict)
                or render.get("nativePhase") != "normal"
                or isinstance(native_ordinal, bool)
                or not isinstance(native_ordinal, int)
                or native_ordinal != 0
            ):
                raise DriverError("command effect requires an ordinary nativePhase=normal ordinal=0 frame")
            if previous is not None:
                if frame.boot != previous.boot:
                    raise DriverError("client boot changed during the bounded auctioneer command route")
                try:
                    delta = unsigned_delta(frame.sequence, previous.sequence)
                except ProtocolError as exc:
                    raise DriverError("command telemetry sequence is malformed") from exc
                if delta == 0:
                    time.sleep(min(0.05, max(0.0, deadline - deadline_clock())))
                    continue
            current_identity = self.driver.resolve_identity(
                require_foreground=True, deadline_monotonic=deadline
            )
            if current_identity.fingerprint != identity.fingerprint:
                raise DriverError("game identity changed while reading command telemetry")
            return snapshot, frame, sample.capture_monotonic
        raise DriverError("fresh command telemetry did not arrive before its deadline")

    @staticmethod
    def _blocking_ui_is_clear(snapshot: dict[str, Any]) -> bool:
        value = snapshot.get("blockingUI")
        return (
            isinstance(value, dict)
            and value.get("staticPopupAvailable") is True
            and value.get("staticPopupShown") is False
            and value.get("gameMenuAvailable") is True
            and value.get("gameMenuShown") is False
        )

    @staticmethod
    def _require_keyboard_interaction_mode(auctioneer: dict[str, Any]) -> None:
        mode = auctioneer.get("interactionMode")
        if mode == "unavailable":
            raise DriverError("native keyboard interaction mode is unavailable")
        if mode not in {"keyboard", "any"}:
            raise DriverError(f"native keyboard interaction is disabled (mode={mode!r})")

    @staticmethod
    def _require_auctioneer_telemetry(snapshot: dict[str, Any], *, binding: str | None = None) -> None:
        auctioneer = snapshot.get("auctioneer")
        if not isinstance(auctioneer, dict):
            raise DriverError("native auctioneer telemetry is missing")
        SessionDaemon._require_keyboard_interaction_mode(auctioneer)
        if auctioneer.get("targetPresent") is not True or auctioneer.get("targetName") != "Auctioneer Jaxon":
            raise DriverError("exact target Auctioneer Jaxon is not present")
        if auctioneer.get("rangeAvailable") is not True or auctioneer.get("withinTradeRange") is not True:
            raise DriverError("Auctioneer Jaxon is outside the native trade-range predicate")
        if auctioneer.get("bindingAvailable") is not True:
            raise DriverError("native CTRL-F12 binding telemetry is unavailable")
        action = auctioneer.get("ctrlF12Action")
        if binding is not None and action != binding:
            raise DriverError(f"CTRL-F12 binding did not become {binding}")
        if binding is None and action not in {"", "INTERACTTARGET"}:
            raise DriverError("CTRL-F12 is bound to another action and will not be overwritten")

    def _prepare_auctioneer_open(
        self,
        *,
        generation: int,
        expected_identity: str,
        dispatch_allowed: Callable[[], None],
        deadline_monotonic: float | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Prepare only the exact Stormwind target and session-only binding."""

        # Mail-cycle deadlines are admitted in the controller/driver clock
        # domain.  The daemon's native polling clock may intentionally be
        # different in deterministic fixtures, so all AH preparation loops
        # must compare and pass one clock-domain deadline end to end.
        deadline_clock = (
            self._mail_cycle_clock
            if deadline_monotonic is not None and self.controller.mail_cycle_capability() is not None
            else time.monotonic
        )

        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (int, float))
                or not math.isfinite(float(deadline_monotonic))
            ):
                raise DriverError("auctioneer preparation deadline is malformed")
            deadline_monotonic = float(deadline_monotonic)
            if deadline_monotonic <= deadline_clock():
                raise DriverError("auctioneer preparation deadline expired")

        def bounded_deadline(timeout: float) -> float:
            # The admitted AH intersection is the bound for every preparation
            # substep; do not reset a shorter independent timeout after model,
            # route, or command work has already consumed budget.
            deadline = (
                float(deadline_monotonic) if deadline_monotonic is not None else deadline_clock() + timeout
            )
            if deadline <= deadline_clock():
                raise DriverError("auctioneer preparation deadline expired")
            return deadline

        identity = self.driver.resolve_identity(
            require_foreground=True, deadline_monotonic=deadline_monotonic
        )
        if identity.fingerprint != expected_identity:
            raise DriverError("auctioneer command route game identity changed")
        snapshot, frame, _captured = self._fresh_status_after(identity, deadline_monotonic=deadline_monotonic)

        def effect_frame_after(
            previous: Any,
            command: str,
            *,
            target_ready: bool,
            binding_ready: bool,
        ) -> tuple[dict[str, Any], Any]:
            deadline = bounded_deadline(8.0)
            while deadline_clock() < deadline:
                dispatch_allowed()
                fresh, next_frame, _captured = self._fresh_status_after(
                    identity,
                    previous=previous,
                    timeout=max(0.001, deadline - deadline_clock()),
                    cancellation_check=dispatch_allowed,
                    deadline_monotonic=deadline_monotonic,
                )
                if not world_closed(fresh):
                    raise DriverError("Stormwind scene or blocking UI changed during command readback")
                chat = fresh.get("chatEdit")
                if not isinstance(chat, dict):
                    raise DriverError("command effect omitted native EditBox state")
                if chat.get("active") is True:
                    if chat.get("focused") is not True or chat.get("text") not in {"", command}:
                        raise DriverError("command submission left an unexpected active EditBox buffer")
                elif chat != {"active": False, "focused": False, "text": ""}:
                    raise DriverError("command submission left an invalid inactive EditBox state")
                if chat.get("active") is not True:
                    auctioneer = fresh.get("auctioneer")
                    if not isinstance(auctioneer, dict):
                        raise DriverError("command effect omitted native Auctioneer state")
                    action = auctioneer.get("ctrlF12Action")
                    if action not in {"", "INTERACTTARGET"}:
                        raise DriverError("CTRL-F12 changed to an unrelated action during command readback")
                    if target_ready and auctioneer.get("targetPresent") is True:
                        self._require_auctioneer_telemetry(fresh)
                    if binding_ready and (
                        auctioneer.get("targetPresent") is not True
                        or auctioneer.get("targetName") != "Auctioneer Jaxon"
                        or auctioneer.get("rangeAvailable") is not True
                        or auctioneer.get("withinTradeRange") is not True
                    ):
                        raise DriverError("binding command lost the verified in-range Auctioneer target")
                    if binding_ready and auctioneer.get("ctrlF12Action") == "INTERACTTARGET":
                        self._require_auctioneer_telemetry(fresh, binding="INTERACTTARGET")
                    if (
                        not target_ready
                        or (
                            auctioneer.get("targetPresent") is True
                            and auctioneer.get("targetName") == "Auctioneer Jaxon"
                            and auctioneer.get("rangeAvailable") is True
                            and auctioneer.get("withinTradeRange") is True
                        )
                    ) and (not binding_ready or auctioneer.get("ctrlF12Action") == "INTERACTTARGET"):
                        return fresh, next_frame
                previous = next_frame
            raise DriverError("native command effect did not arrive before its bounded readback deadline")

        def world_closed(value: dict[str, Any]) -> bool:
            context = value.get("context", {})
            return (
                isinstance(context, dict)
                and context.get("character") == self.config.profile.character
                and context.get("realm") == self.config.profile.realm
                and context.get("zone") == "Stormwind City"
                and value.get("ahOpen") is False
                and self._blocking_ui_is_clear(value)
            )

        def wait_for_chat_open(previous: Any) -> tuple[dict[str, Any], Any]:
            deadline = bounded_deadline(8.0)
            while deadline_clock() < deadline:
                current, next_frame, _captured = self._fresh_status_after(
                    identity,
                    previous=previous,
                    timeout=max(0.001, deadline - deadline_clock()),
                    cancellation_check=dispatch_allowed,
                    deadline_monotonic=deadline_monotonic,
                )
                if not world_closed(current):
                    raise DriverError("Stormwind scene or blocking UI changed while opening chat")
                edit = current.get("chatEdit")
                if not isinstance(edit, dict):
                    raise DriverError("chat-open readback omitted native EditBox state")
                if edit == {"active": True, "focused": True, "text": ""}:
                    return current, next_frame
                if edit.get("active") is True:
                    if edit.get("text") != "" or not isinstance(edit.get("focused"), bool):
                        raise DriverError("opening chat produced a nonempty or malformed EditBox")
                    previous = next_frame
                    continue
                if edit != {"active": False, "focused": False, "text": ""}:
                    raise DriverError("opening chat produced an invalid EditBox state")
                previous = next_frame
            raise DriverError("empty focused chat EditBox did not appear before its bounded deadline")

        def close_empty_chat() -> None:
            nonlocal snapshot, frame
            edit = snapshot.get("chatEdit")
            if edit == {"active": False, "focused": False, "text": ""}:
                return
            if edit != {"active": True, "focused": True, "text": ""}:
                raise DriverError("Auction House interaction requires an empty focused or closed chat buffer")
            self.driver.run(
                "escape_command_buffer",
                generation,
                precondition=identity_guard,
                postcondition=identity_guard,
                **({"deadline_monotonic": deadline_monotonic} if deadline_monotonic is not None else {}),
            )
            deadline = bounded_deadline(8.0)
            previous = frame
            while deadline_clock() < deadline:
                dispatch_allowed()
                current, next_frame, _captured = self._fresh_status_after(
                    identity,
                    previous=previous,
                    timeout=max(0.001, deadline - deadline_clock()),
                    cancellation_check=dispatch_allowed,
                    deadline_monotonic=deadline_monotonic,
                )
                if not world_closed(current):
                    raise DriverError("Stormwind scene or blocking UI changed while closing chat")
                current_edit = current.get("chatEdit")
                if current_edit == {"active": False, "focused": False, "text": ""}:
                    snapshot, frame = current, next_frame
                    return
                if current_edit != {"active": True, "focused": True, "text": ""}:
                    raise DriverError("Escape left an unexpected active chat buffer")
                previous = next_frame
            raise DriverError("empty chat EditBox did not close before its bounded deadline")

        def identity_guard(current: Any) -> bool:
            dispatch_allowed()
            return current.fingerprint == expected_identity and world_closed(snapshot)

        def ensure_chat() -> None:
            nonlocal snapshot, frame
            edit = snapshot.get("chatEdit", {})
            if edit == {"active": True, "focused": True, "text": ""}:
                return
            if isinstance(edit, dict) and edit.get("active") is True:
                raise DriverError("active chat buffer is not an empty focused command line")
            if not world_closed(snapshot):
                raise DriverError("Stormwind world scene is not verified before opening chat")
            self.driver.run(
                "open_chat",
                generation,
                precondition=identity_guard,
                postcondition=identity_guard,
                **({"deadline_monotonic": deadline_monotonic} if deadline_monotonic is not None else {}),
            )
            snapshot, frame = wait_for_chat_open(frame)

        def send(command: str) -> tuple[dict[str, Any], Any]:
            ensure_chat()
            if not world_closed(snapshot):
                raise DriverError("Stormwind world scene or closed-AH fact changed before command")
            if snapshot.get("chatEdit") != {"active": True, "focused": True, "text": ""}:
                raise DriverError("command route requires a fresh empty focused chat buffer")
            dispatch_allowed()
            readback_frames: list[Any] = []

            def readback() -> str:
                dispatch_allowed()
                captured_frames: list[Any] = []
                observed = self._read_command_buffer(
                    previous=frame,
                    frame_sink=captured_frames.append,
                    deadline_monotonic=deadline_monotonic,
                )
                dispatch_allowed()
                if len(captured_frames) != 1:
                    raise DriverError("typed command lacks one captured pre-submit EditBox frame")
                readback_frames.extend(captured_frames)
                before_submit = captured_frames[0].snapshot.value
                if not world_closed(before_submit):
                    raise DriverError("Stormwind scene or blocking UI changed before command submission")
                chat = before_submit.get("chatEdit")
                if (
                    not isinstance(chat, dict)
                    or chat.get("active") is not True
                    or chat.get("focused") is not True
                ):
                    raise DriverError("pre-submit EditBox is not the owned focused command buffer")
                return observed

            self.driver.prepare_command(
                generation,
                command,
                snapshot,
                readback=readback,
                precondition=identity_guard,
                postcondition=identity_guard,
                **({"deadline_monotonic": deadline_monotonic} if deadline_monotonic is not None else {}),
            )
            if len(readback_frames) != 1:
                raise DriverError("typed command lacks one captured pre-submit EditBox frame")
            return effect_frame_after(
                readback_frames[0],
                command,
                target_ready=command == DesktopDriver.TARGET_AUCTIONEER_COMMAND,
                binding_ready=command == DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND,
            )

        auctioneer = snapshot.get("auctioneer")
        if not isinstance(auctioneer, dict):
            raise DriverError("native auctioneer telemetry is malformed")
        self._require_keyboard_interaction_mode(auctioneer)
        if auctioneer.get("bindingAvailable") is not True:
            raise DriverError("native CTRL-F12 binding telemetry is unavailable")
        if auctioneer.get("ctrlF12Action") not in {"", "INTERACTTARGET"}:
            raise DriverError("CTRL-F12 is bound to another action and will not be overwritten")
        target_exact = (
            auctioneer.get("targetPresent") is True and auctioneer.get("targetName") == "Auctioneer Jaxon"
        )
        if target_exact:
            self._require_auctioneer_telemetry(snapshot)
        else:
            if auctioneer.get("targetPresent") is False and auctioneer.get("targetName") != "":
                raise DriverError("absent native target has a nonempty target name")
            snapshot, frame = send(DesktopDriver.TARGET_AUCTIONEER_COMMAND)
            if not world_closed(snapshot):
                raise DriverError("target command did not preserve the verified closed Stormwind world")
            self._require_auctioneer_telemetry(snapshot)
        if snapshot["auctioneer"]["ctrlF12Action"] == "":
            snapshot, frame = send(DesktopDriver.CONDITIONAL_INTERACTION_BINDING_COMMAND)
            if not world_closed(snapshot):
                raise DriverError("binding command did not preserve the verified closed Stormwind world")
            self._require_auctioneer_telemetry(snapshot, binding="INTERACTTARGET")
        else:
            self._require_auctioneer_telemetry(snapshot, binding="INTERACTTARGET")
        close_empty_chat()
        return snapshot, frame

    def _run_action(self, name: str, snapshot: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        allow_mail_cycle = bool(kwargs.pop("allow_mail_cycle", False))
        requested_deadline = kwargs.pop("deadline_monotonic", None)
        mail_cycle_deadline: float | None = None
        with self.controller.lock:
            status = self.controller.status()
            if status.state in {
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
                ControllerState.STOPPED,
            }:
                raise DriverError("action dispatch is not allowed in the current state")
            if status.stop_requested:
                raise DriverError("action dispatch is not allowed after stop")
            if status.mode.value == "interaction" and not allow_mail_cycle:
                raise DriverError("engine action is not allowed in interaction mode")
            if allow_mail_cycle:
                cycle = self.controller.mail_cycle_checkpoint()
                authority = getattr(getattr(self, "_mail_cycle_context", None), "cycle_id", "")
                capability = self.controller.mail_cycle_capability()
                if (
                    cycle.get("phase") not in {"to_auctioneer", "opening_ah"}
                    or authority != cycle.get("cycleId")
                    or capability is None
                    or capability.cycle_id != authority
                ):
                    raise DriverError("mail cycle does not own Auction House opening")
                mail_cycle_deadline = float(capability.deadline_monotonic)
                if not math.isfinite(mail_cycle_deadline) or mail_cycle_deadline <= self._mail_cycle_clock():
                    raise DriverError("mail cycle completion deadline expired")
                if requested_deadline is not None:
                    if (
                        isinstance(requested_deadline, bool)
                        or not isinstance(requested_deadline, (int, float))
                        or not math.isfinite(float(requested_deadline))
                    ):
                        raise DriverError("mail cycle operation deadline is malformed")
                    mail_cycle_deadline = min(mail_cycle_deadline, float(requested_deadline))
            self.controller.set_action_intent(name, deadline_seconds=self._action_deadline(name))
            self.controller.action_result("preparing")
            controller_generation = status.generation
        generation = self.driver.guardian.generation
        self._last_action_generation = generation
        expected_identity = self.driver.resolve_identity(
            require_foreground=True,
            deadline_monotonic=mail_cycle_deadline,
        ).fingerprint

        def dispatch_allowed() -> None:
            with self.controller.lock:
                current = self.controller.status()
                if current.generation != controller_generation or current.state in {
                    ControllerState.PAUSED,
                    ControllerState.BLOCKED,
                    ControllerState.STOPPED,
                }:
                    raise DriverError("action dispatch was revoked")
                if (
                    current.mode.value == "interaction" and not allow_mail_cycle
                ) or current.action_intent != name:
                    raise DriverError("action dispatch was revoked by interaction mode")
                if (
                    current.lease_generation != self.driver.guardian.generation
                    or not self.driver.guardian.armed
                ):
                    raise DriverError("action input lease is stale or revoked")

        def guard(identity: Any) -> bool:
            if identity.fingerprint != expected_identity:
                return False
            if name == "open_ah" and snapshot.get("ahOpen") is True:
                return False
            if name == "logout":
                context = snapshot.get("context", {})
                pending = snapshot.get("pending", {}).get("ids", [])
                if context.get("character") != self.config.profile.character:
                    return False
                if context.get("realm") != self.config.profile.realm:
                    return False
                if pending:
                    return False
                if not self._logout_scanner_idle(snapshot, self.controller.status().last_uid):
                    return False
            return True

        native_auctioneer = isinstance(self.driver.backend, X11Backend) and name == "open_ah"
        native_named = isinstance(self.driver.backend, X11Backend) and name in {
            "dismiss_disconnect",
            "reconnect_game",
            "select_arthur",
            "enter_world",
        }
        if native_named:
            dispatch_allowed()
            self.driver.verify_named_action(name, "before")
        before_observation = self.driver.last_named_observation if native_named else None
        dispatch_allowed()
        action_precondition = guard
        action_postcondition = guard
        action_snapshot: dict[str, Any] = {"value": snapshot, "frame": None}
        if native_auctioneer:
            prepared, prepared_frame = self._prepare_auctioneer_open(
                generation=generation,
                expected_identity=expected_identity,
                dispatch_allowed=dispatch_allowed,
                deadline_monotonic=mail_cycle_deadline,
            )
            action_snapshot["value"] = prepared
            action_snapshot["frame"] = prepared_frame

            def auctioneer_snapshot_matches(current: dict[str, Any], *, expected_open: bool) -> bool:
                if current.get("ahOpen") is not expected_open:
                    return False
                if not self._blocking_ui_is_clear(current):
                    return False
                context = current.get("context")
                if (
                    not isinstance(context, dict)
                    or context.get("character") != self.config.profile.character
                    or context.get("realm") != self.config.profile.realm
                    or context.get("zone") != "Stormwind City"
                ):
                    return False
                if current.get("chatEdit") != {"active": False, "focused": False, "text": ""}:
                    return False
                self._require_auctioneer_telemetry(current, binding="INTERACTTARGET")
                return True

            def auctioneer_precondition(identity: Any) -> bool:
                dispatch_allowed()
                current, frame, _captured = self._fresh_status_after(
                    identity,
                    previous=action_snapshot["frame"],
                    timeout=5.0,
                    cancellation_check=dispatch_allowed,
                    deadline_monotonic=mail_cycle_deadline,
                )
                self._require_auctioneer_telemetry(current, binding="INTERACTTARGET")
                if identity.fingerprint != expected_identity:
                    raise DriverError("game identity changed before Auction House interaction")
                if not auctioneer_snapshot_matches(current, expected_open=False):
                    raise DriverError(
                        "unsafe Stormwind scene or blocking UI before Auction House interaction"
                    )
                action_snapshot.update(value=current, frame=frame)
                return True

            def auctioneer_postcondition(identity: Any) -> bool:
                postcondition_clock = (
                    self._mail_cycle_clock if mail_cycle_deadline is not None else time.monotonic
                )
                deadline = (
                    float(mail_cycle_deadline)
                    if mail_cycle_deadline is not None
                    else postcondition_clock() + 8.0
                )
                if deadline <= postcondition_clock():
                    raise DriverError("Auction House readback deadline expired")
                while postcondition_clock() < deadline:
                    dispatch_allowed()
                    current, frame, _captured = self._fresh_status_after(
                        identity,
                        previous=action_snapshot["frame"],
                        timeout=max(0.001, deadline - postcondition_clock()),
                        cancellation_check=dispatch_allowed,
                        deadline_monotonic=mail_cycle_deadline,
                    )
                    if identity.fingerprint != expected_identity:
                        raise DriverError("game identity changed during Auction House readback")
                    context = current.get("context")
                    if (
                        not isinstance(context, dict)
                        or context.get("character") != self.config.profile.character
                        or context.get("realm") != self.config.profile.realm
                        or context.get("zone") != "Stormwind City"
                        or not self._blocking_ui_is_clear(current)
                    ):
                        raise DriverError("unsafe scene or blocking UI changed during Auction House readback")
                    chat = current.get("chatEdit")
                    if chat == {"active": True, "focused": True, "text": ""}:
                        action_snapshot.update(value=current, frame=frame)
                        continue
                    if chat != {"active": False, "focused": False, "text": ""}:
                        raise DriverError("Auction House readback found an unexpected active chat buffer")
                    self._require_auctioneer_telemetry(current, binding="INTERACTTARGET")
                    if current.get("ahOpen") is True:
                        action_snapshot.update(value=current, frame=frame)
                        return True
                    if current.get("ahOpen") is not False:
                        raise DriverError("Auction House state is malformed during bounded readback")
                    action_snapshot.update(value=current, frame=frame)
                raise DriverError("Auction House did not open before its bounded readback deadline")

            action_precondition = auctioneer_precondition
            action_postcondition = auctioneer_postcondition
        if mail_cycle_deadline is not None:
            kwargs["deadline_monotonic"] = mail_cycle_deadline
        self.driver.run(
            name,
            generation,
            precondition=action_precondition,
            postcondition=action_postcondition,
            **kwargs,
        )
        if native_auctioneer:
            completed = action_snapshot.get("value")
            if not isinstance(completed, dict) or not auctioneer_snapshot_matches(
                completed, expected_open=True
            ):
                raise DriverError("native Auction House reopen lacks fresh exact postcondition telemetry")
        if native_named:
            if before_observation is None:
                raise DriverError(f"{name} has no sealed pre-action observation")
            transition_started_at = self._native_clock()
            identity = self.driver.resolve_identity(
                require_foreground=True,
                deadline_monotonic=mail_cycle_deadline,
            )
            ancestry = tuple(dict.fromkeys((identity.child_xid, identity.xid)))
            try:
                wait_plan = plan_context_wait(
                    self.driver._NAMED_ACTION_STATES[name][1],
                    BindingMetadata(
                        identity_fingerprint=before_observation.identity_fingerprint,
                        capture_id=before_observation.capture_id,
                        capture_sequence=before_observation.capture_sequence,
                        generation=generation,
                        target_xid=identity.child_xid,
                        target_ancestry=ancestry,
                        source=before_observation.source,
                        boot=before_observation.boot,
                    ),
                    started_at=transition_started_at,
                    policy=WaitPolicy(
                        timeout=20.0,
                        poll_interval=0.25,
                        settle_count=2,
                        max_observation_age=5.0,
                    ),
                    allow_source_boot_change=name == "enter_world",
                )
            except ExecutorError as exc:
                raise DriverError(f"{name} transition wait plan is invalid: {exc}") from exc
            observations: list[FreshObservation] = []
            last_error: DriverError | None = None
            settled_observation: NamedStateObservation | None = None
            wait_deadline = wait_plan.started_at + wait_plan.policy.timeout
            for poll_at in wait_plan.poll_times():
                now = self._native_clock()
                if now >= wait_deadline:
                    break
                sleep_for = min(max(0.0, poll_at - now), wait_deadline - now)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                if self._native_clock() >= wait_deadline:
                    break
                # Admission and revocation failures are terminal.  Only the
                # observation itself may be retried while the absolute
                # deadline remains.
                try:
                    dispatch_allowed()
                    self.driver.guardian.check(generation)
                except GuardianError as exc:
                    raise DriverError(str(exc)) from exc
                if self._native_clock() >= wait_deadline:
                    break
                try:
                    # The driver owns the capture/model implementation, but
                    # this wait still has one absolute transition deadline.
                    # Publish it as a scoped capability so every native and
                    # visual producer in ``observe_named_state`` can consume
                    # the same remaining budget instead of starting a fresh
                    # timeout for each poll.
                    self._named_observation_deadline = wait_deadline
                    try:
                        observed = self.driver.observe_named_state(wait_plan.expected_context)
                    finally:
                        self._named_observation_deadline = None
                    fresh = FreshObservation(
                        context=observed.state,
                        identity_fingerprint=observed.identity_fingerprint,
                        generation=generation,
                        capture_sequence=observed.capture_sequence,
                        observed_at=observed.observed_monotonic,
                        source=observed.source,
                        boot=observed.boot,
                        capture_id=observed.capture_id,
                        received_at=self._native_clock(),
                    )
                    current_now = self._native_clock()
                    if accepts_observation(wait_plan, fresh, now=current_now):
                        observations.append(fresh)
                        if observations_settled(
                            wait_plan,
                            tuple(observations),
                            now=current_now,
                        ):
                            settled_observation = observed
                            break
                    else:
                        # A rejected capture is a broken consecutive settle
                        # run; it must never silently preserve prior evidence.
                        observations.clear()
                except (DriverError, ExecutorError, ModelError) as exc:
                    last_error = exc if isinstance(exc, DriverError) else DriverError(str(exc))
                    observations.clear()
                    if self._native_clock() >= wait_deadline:
                        break
                    continue
            if settled_observation is None:
                raise last_error or DriverError(f"{name} named postcondition did not settle before deadline")
            try:
                self.driver.verify_named_action(name, "after", observation=settled_observation)
            except DriverError:
                raise
            finally:
                settled_observation = None
        with self.controller.lock:
            if self.controller.status().generation == controller_generation:
                self.controller.clear_action_intent("complete")
        return copy.deepcopy(action_snapshot.get("value", {}))

    def run_native_calibration(
        self,
        *,
        expected_generation: int | None = None,
        admitted_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Own the live render gate; callers cannot supply copied measurements."""
        self._begin_calibration("native", expected_generation, admitted_context=admitted_context)
        self._native_measurement_state = None
        diagnostic = getattr(self, "_native_observation_diagnostic", None)
        if isinstance(diagnostic, _NativeObservationDiagnostic):
            diagnostic.start_run()
        run_context: dict[str, Any] | None = None
        deferred = False
        succeeded = False
        owned_meter: LinuxProcStatusVmHwmReset5 | None = None
        # The native runner owns the measurement provider and captures each
        # construction/update frame itself.  Passing a metrics dict copied from
        # one ordinary telemetry frame would turn the gate into an assertion.
        try:
            with self._native_construction_lock:
                run_context = self._native_construction_callback_context_locked()
                deferred = bool(run_context.get("deferred"))
                if deferred and self._native_construction_handshake.get("state") != "running":
                    raise DriverError("deferred bootstrap was not consumed")
            self._native_handshake = {
                "state": "running",
                "holdRequired": False,
                "measureReady": False,
                "resumeRequired": False,
                "resumeReady": False,
                "failure": "",
                "restored": None,
                "archiveReceipts": [],
            }
            with self.controller.lock:
                status_before = self.controller.status()
                if status_before.state != ControllerState.NATIVE_CALIBRATION:
                    raise DriverError("native calibration requires an unarmed native-calibration state")
                if expected_generation is not None and status_before.generation != expected_generation:
                    raise DriverError("native calibration generation was revoked")
            owned_meter = self._ensure_native_memory_meter()
            # A new native gate supersedes any previous final capture.  The
            # runner retains only its final artifact for sealed evidence.
            pending_path = None
            with self._native_bootstrap_lock:
                if self._native_bootstrap_observation is not None:
                    pending_path = self._native_bootstrap_observation.get("capturePath")
            self._cleanup_native_owned_captures(keep=pending_path)
            evidence = NativeCalibrationRunner(
                self.driver,
                monotonic=self._native_clock,
                diagnostic=diagnostic,
            ).run()
            if deferred:
                self._native_construction_revalidate_run_owner(
                    run_context, expected_generation=expected_generation or run_context["generation"]
                )
                with self._native_construction_lock:
                    current_state = self._native_construction_handshake.get("state")
                    if current_state not in {"resume_ready", "finalize_required"}:
                        raise DriverError("deferred native run did not reach resume_ready")
                    operation_deadline = self._native_construction_operation_deadline
                    hook_deadline = self._native_construction_handshake.get("finalizeHookDeadline")
                while not self._native_construction_finalize_event.wait(0.05):
                    self._native_construction_revalidate_run_owner(
                        run_context, expected_generation=expected_generation or run_context["generation"]
                    )
                    if hook_deadline is not None and self._native_clock() >= float(hook_deadline):
                        raise DriverError("deferred finalize hook deadline expired")
                    if operation_deadline is not None and self._native_clock() >= operation_deadline:
                        raise DriverError("deferred finalize admission deadline expired")
                    if self._shutdown_event.is_set():
                        raise DriverError("deferred finalize was canceled during shutdown")
                self._native_construction_revalidate_run_owner(
                    run_context, expected_generation=expected_generation or run_context["generation"]
                )
                with self._native_construction_lock:
                    if self._native_construction_handshake.get("state") != "finalize_required":
                        raise DriverError("deferred finalize admission is malformed")
                    final_deadline = float(
                        self._native_construction_handshake.get("finalizeDeadline", self._native_clock())
                    )
                    resume_sequence = int(self._native_construction_handshake.get("resumeSequence", -1))
                    resume_capture = float(
                        self._native_construction_handshake.get("resumeCaptureMonotonic", -1)
                    )
                    admission_capture = self._native_clock()
                final_generation = expected_generation or int(run_context["generation"])
                final_observation = None
                final_boot = ""
                final_sequence = final_crc32 = 0
                final_reservation_snapshot: dict[str, Any] | None = None
                final_identity_fingerprint = ""
                final_capture_identity = ""
                last_finalize_error: Exception | None = None
                while self._native_clock() < final_deadline:
                    self._native_construction_revalidate_finalize_owner(
                        run_context,
                        expected_generation=final_generation,
                        finalize_deadline=final_deadline,
                        hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                    )
                    with self._native_construction_lock:
                        final_capture_capability = self._native_construction_capability_locked()
                    if final_capture_capability is None:
                        raise DriverError("deferred native finalize owner was revoked before capture")
                    try:
                        candidate = self._native_construction_capture(
                            deadline=final_deadline,
                            expected_generation=final_generation,
                            expected_state=ControllerState.NATIVE_CALIBRATION,
                            native_capability=final_capture_capability,
                        )
                        candidate_boot, candidate_sequence, candidate_crc32, candidate_snapshot = (
                            self._native_construction_frame_facts(candidate)
                        )
                        scanner = candidate_snapshot.get("scanner")
                        fence = scanner.get("scheduleFence") if isinstance(scanner, dict) else None
                        if (
                            candidate_boot.startswith("native-construction:")
                            and candidate_sequence > resume_sequence
                            and float(candidate.get("captureMonotonic", -1)) > resume_capture
                            and float(candidate.get("captureMonotonic", -1)) >= admission_capture
                            and isinstance(fence, dict)
                            and fence.get("state") == "inactive"
                        ):
                            self._native_construction_revalidate_finalize_owner(
                                run_context,
                                expected_generation=final_generation,
                                finalize_deadline=final_deadline,
                                hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                            )
                            final_observation = candidate
                            final_boot, final_sequence, final_crc32, _final_snapshot = (
                                candidate_boot,
                                candidate_sequence,
                                candidate_crc32,
                                candidate_snapshot,
                            )
                            break
                        last_finalize_error = DriverError("finalize observation is still pre-effect")
                    except (DriverError, ProtocolError) as exc:
                        # A capture/decoder rejection may be retried, but an
                        # owner revocation must escape immediately rather than
                        # being converted into another sleep/capture attempt.
                        self._native_construction_revalidate_finalize_owner(
                            run_context,
                            expected_generation=final_generation,
                            finalize_deadline=final_deadline,
                            hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                        )
                        last_finalize_error = exc
                    if self._native_clock() >= final_deadline:
                        break
                    time.sleep(0.05)
                    self._native_construction_revalidate_finalize_owner(
                        run_context,
                        expected_generation=final_generation,
                        finalize_deadline=final_deadline,
                        hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                    )
                if final_observation is None:
                    raise last_finalize_error or DriverError("deferred finalize frame did not settle")
                with self._native_construction_lock:
                    final_reservation_snapshot = copy.deepcopy(self._native_construction_reservation)
                if final_reservation_snapshot is None:
                    raise DriverError("deferred finalize owner was revoked before binding validation")
                final_identity = self._native_construction_validate_snapshot(
                    final_reservation_snapshot,
                    expected_generation=final_generation,
                )
                self._native_construction_observation_identity(final_observation, final_identity)
                final_identity_fingerprint = final_observation["identityFingerprint"]
                final_capture_identity = final_observation["captureIdentity"]
                if (
                    final_identity_fingerprint != final_capture_identity
                    or final_identity_fingerprint != final_reservation_snapshot.get("identityFingerprint")
                ):
                    raise DriverError("deferred finalize capture identity binding changed")
                self._native_construction_revalidate_finalize_owner(
                    run_context,
                    expected_generation=final_generation,
                    finalize_deadline=final_deadline,
                    hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                )
                status = self._native_construction_finalize_transaction(
                    run_context=run_context,
                    reservation_snapshot=final_reservation_snapshot,
                    expected_generation=final_generation,
                    finalize_deadline=final_deadline,
                    hook_deadline=float(hook_deadline) if hook_deadline is not None else None,
                    final_boot=final_boot,
                    final_sequence=final_sequence,
                    final_crc32=final_crc32,
                    final_identity_fingerprint=final_identity_fingerprint,
                    final_capture_identity=final_capture_identity,
                    evidence=evidence,
                )
            if not deferred:
                with self.controller.lock:
                    if (
                        expected_generation is not None
                        and self.controller.status().generation != expected_generation
                    ):
                        raise DriverError("native calibration generation was revoked")
                    status = self.controller.native_calibrated(
                        evidence,
                        preconstruction_proof=self._native_bootstrap_proof,
                        expected_generation=expected_generation,
                        expected_state=ControllerState.NATIVE_CALIBRATION,
                    )
            if (
                expected_generation is not None
                and status.generation != expected_generation
                or status.state is not ControllerState.INPUT_CALIBRATION
                or status.state is ControllerState.BLOCKED
            ):
                raise DriverError("native calibration admission was not finalized")
            # Deferred construction is finalized inside the controller-first
            # transaction above; no mutable owner is reread or cleared here.
            self._cleanup_native_owned_captures(keep=getattr(evidence, "capture_path", None))
            # The bootstrap token is one-use evidence.  The controller has
            # already copied it into its durable calibration record; keeping
            # the daemon-side object would let a later gate accidentally
            # reuse an observation from the previous client construction.
            self._native_bootstrap_proof = None
            self._native_baseline_ready.clear()
            self._native_baseline_observation = None
            succeeded = True
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.finish_success()
            result = self.status_dict() | {"calibration": "native", "state": status.state.value}
        except Exception as exc:
            if isinstance(diagnostic, _NativeObservationDiagnostic):
                diagnostic.fail(str(exc))
            # A failed native run cannot reuse the preconstruction capture:
            # the operator may have loaded/reloaded the client while the
            # provider was running.  Require a new external bootstrap token.
            self._native_bootstrap_proof = None
            self._native_measurement_state = None
            self._native_baseline_ready.clear()
            self._native_baseline_observation = None
            with self._native_bootstrap_lock:
                self._native_bootstrap_observation = None
                self._native_bootstrap_ready.clear()
            self._cleanup_native_owned_captures()
            if deferred:
                with self._native_construction_lock:
                    current = self._native_construction_run_context
                    if (
                        current is not None
                        and not current.get("closed")
                        and current.get("runId") == run_context.get("runId")
                    ):
                        terminal_reservation = copy.deepcopy(self._native_construction_reservation)
                        self._native_construction_publish_state_locked(
                            "error", failure=str(exc), restored=False
                        )
                        self._native_construction_record_terminal_locked(
                            terminal_reservation,
                            state="error",
                            failure=str(exc),
                        )
                        self._native_construction_reservation = None
                        self._native_construction_ticket = None
                        self._native_construction_operation_deadline = None
                        self._native_construction_finalize_event.clear()
                        self._native_construction_close_context_locked(run_context)
                try:
                    self.controller.mark_native_construction_recovery_required()
                except RuntimeError:
                    pass
            if self._native_handshake.get("state") not in {"restoration_required", "resume_ready"}:
                self._native_handshake.update(
                    {
                        "state": "error",
                        "failure": str(exc),
                        "restored": False,
                        "holdRequired": False,
                        "measureReady": False,
                        "resumeRequired": False,
                        "resumeReady": False,
                    }
                )
            self._finish_calibration(state="error", error=str(exc))
            raise
        finally:
            if owned_meter is not None:
                try:
                    owned_meter.close()
                finally:
                    if self.driver.native_memory_meter is owned_meter:
                        self.driver.native_memory_meter = None
        self._native_measurement_state = None
        self._finish_calibration(state="complete" if succeeded else "error")
        return result

    def run_input_calibration(self, *, expected_generation: int | None = None) -> dict[str, Any]:
        """Own the closed input calibration and deliberate takeover boundary."""
        from .calibration import InputCalibrationWorkflow

        self._begin_calibration("input", expected_generation)
        workflow: InputCalibrationWorkflow | None = None
        try:
            self._validate_runtime_paths()
            with self.controller.lock:
                status_before = self.controller.status()
                if status_before.state != ControllerState.INPUT_CALIBRATION:
                    raise DriverError("input calibration requires the native gate to be complete")
                if expected_generation is not None and status_before.generation != expected_generation:
                    raise DriverError("input calibration generation was revoked")
            # Pointer anchors are measured lazily by the native executor from
            # the fresh named scene immediately before each action.  The
            # input gate proves only the XTEST/guardian boundary; it must not
            # claim future disconnect, character-select, or AH controls are
            # simultaneously visible in the current world scene.
            workflow = InputCalibrationWorkflow(
                self.driver,
                display=self.config.profile.display,
                on_ready=self._set_calibration_readiness,
            )
            evidence = workflow.run()
            with self.controller.lock:
                current = self.controller.status()
            if expected_generation is not None and current.generation != expected_generation:
                # The deliberate human sample is itself expected to revoke
                # the guardian.  Preserve the sealed evidence in the durable
                # paused process while requiring an explicit resume to arm it.
                if current.state == ControllerState.PAUSED:
                    status = self.controller.retain_input_calibration_while_paused(evidence)
                    result = self.status_dict() | {"calibration": "input", "state": status.state.value}
                    self._finish_calibration(state="complete")
                    return result
                raise DriverError("input calibration generation was revoked")
            status = self.controller.input_calibrated(
                evidence=evidence,
                expected_generation=expected_generation,
            )
            result = self.status_dict() | {"calibration": "input", "state": status.state.value}
            self._finish_calibration(state="complete")
            return result
        except Exception as exc:
            if workflow is not None:
                try:
                    context = workflow.failure_context_for(exc)
                    self._persist_input_calibration_failure(context)
                except Exception as diagnostic_error:
                    _LOGGER.warning(
                        "input calibration diagnostic assembly failed: %s",
                        _bounded_diagnostic_error(diagnostic_error),
                    )
            self._finish_calibration(state="error", error=str(exc))
            raise

    def _validate_runtime_paths(self) -> None:
        """Validate configured durable locations before arming desktop input."""

        paths = {
            "SavedVariables": self.config.saved_variables,
            "companion state": self.config.companion_state,
            "Data.lua": self.config.data_lua,
        }
        for label, path in paths.items():
            if path is None:
                raise DriverError(f"{label} path is not configured")
            parent = path.expanduser().parent
            if not parent.exists() or not parent.is_dir():
                raise DriverError(f"{label} parent directory does not exist: {parent}")
            if path.exists() and path.is_dir():
                raise DriverError(f"{label} path is a directory: {path}")

    def _reconcile_durable_files(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Read every durable queue before releasing a blocked controller.

        Resume from a rejected acknowledgement is an observation boundary,
        not permission to retry the old action.  Stable reads prove that the
        three writers agree on a complete schema; the next state still checks
        the exact UID and deadlines before dispatching anything.
        """

        if not all((self.config.saved_variables, self.config.companion_state, self.config.data_lua)):
            raise EvidenceError("reconciliation paths are not configured")
        assert self.config.saved_variables is not None
        assert self.config.companion_state is not None
        assert self.config.data_lua is not None
        try:
            root = savedvars.loads(stable_file(self.config.saved_variables))
        except (OSError, UnicodeError, ValueError, TypeError, JournalError) as exc:
            raise EvidenceError(f"SavedVariables reconciliation failed: {exc}") from exc
        db = root.get("CrowleyDB")
        if not isinstance(db, dict):
            raise EvidenceError("SavedVariables reconciliation has no CrowleyDB")
        if "scans" not in db or "sweeps" not in db:
            raise EvidenceError("SavedVariables reconciliation is missing explicit queue fields")
        for name in ("scans", "sweeps"):
            if not isinstance(db[name], dict):
                raise EvidenceError(f"SavedVariables reconciliation has malformed {name}")
        stable_scan_ids = frozenset(uid for uid in db["scans"] if isinstance(uid, str) and uid)
        stable_sweep_ids = frozenset(uid for uid in db["sweeps"] if isinstance(uid, str) and uid)
        if len(stable_scan_ids) != len(db["scans"]) or len(stable_sweep_ids) != len(db["sweeps"]):
            raise EvidenceError("SavedVariables reconciliation queue IDs are malformed")
        scan_kwargs = self._saved_scan_kwargs(snapshot)
        stable_evidence = None
        for uid in sorted(stable_scan_ids, key=lambda item: item.encode()):
            parsed = read_stable_saved_scan(
                self.config.saved_variables,
                uid=uid,
                planned=self.config.profile.plan_count,
                **scan_kwargs,
            )
            if uid == str(snapshot.get("lastSaved", {}).get("memoryUid", "")):
                stable_evidence = parsed
        uploads = read_uploads(self.config.companion_state)
        data_ids = read_data_acknowledged_ids(
            self.config.data_lua,
            expected_plan_json=self.config.plan_json,
            expected_plan_hash=self.config.plan_hash,
            expected_client_build=_CANONICAL_PLAN["targetClientBuild"],
        )
        scanner = snapshot.get("scanner")
        pending = snapshot.get("pending")
        if not isinstance(scanner, dict) or not isinstance(pending, dict):
            raise EvidenceError("restart reconciliation telemetry queues are malformed")
        phase = scanner.get("phase")
        active_sweep = scanner.get("activeSweepId")
        pending_ids = pending.get("ids")
        pending_count = pending.get("count")
        if (
            not isinstance(phase, str)
            or not isinstance(active_sweep, str)
            or not isinstance(pending_ids, list)
            or any(not isinstance(uid, str) or not uid for uid in pending_ids)
            or isinstance(pending_count, bool)
            or not isinstance(pending_count, int)
            or pending_count != len(pending_ids)
        ):
            raise EvidenceError("restart reconciliation telemetry queue facts are malformed")
        stable_uid = ""
        last_saved = snapshot.get("lastSaved")
        observed_uid = last_saved.get("memoryUid", "") if isinstance(last_saved, dict) else ""
        if isinstance(observed_uid, str) and observed_uid in stable_scan_ids:
            stable_uid = observed_uid
        return {
            "scanner_phase": phase,
            # Both explicit idle phases are valid.  ``item``/``owned`` and an
            # empty activeSweepId are not a completed scan witness.  A pending
            # UID is a separate acknowledgement proof and may remain after a
            # saved sweep has completed.
            "scanner_idle": phase in {"ready", "scheduled"} and not active_sweep,
            "stable_scan_ids": stable_scan_ids,
            "stable_sweep_ids": stable_sweep_ids,
            "stable_format2_uid": stable_uid,
            "stable_format2_evidence": stable_evidence,
            "stable_saved_uid": stable_uid,
            "stable_format2_verified": bool(stable_evidence is not None and stable_evidence.stable_verified),
            "stable_files_complete": True,
            "uploaded_ids": frozenset(uploads),
            "upload_timestamps": dict(uploads),
            "data_ack_ids": frozenset(data_ids),
        }

    def run_once(self) -> dict[str, Any]:
        lock = self._claim_motion_fence()
        if lock is None:
            return self.status_dict()
        try:
            # Controller construction turns an interrupted active repeat
            # checkpoint into PAUSED and marks that restart-created pause
            # once.  Reuse the public runtime resume path here so a matching
            # durable takeover drill gets fresh passive checks before ordinary
            # reconciliation.  Persisted user PAUSED/STOPPED holds never set
            # this process-local marker and remain untouched.
            startup_generation = self.controller.claim_restart_startup_resume()
            if startup_generation is not None:
                try:
                    self.runtime_start(expected_generation=startup_generation, resume=True)
                except (DriverError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                    self.last_error = str(exc)
                    invalidation_cause = getattr(exc, "invalidation_cause", InvalidationCause.UNKNOWN)
                    if not isinstance(invalidation_cause, InvalidationCause):
                        invalidation_cause = InvalidationCause.UNKNOWN
                    # The one restart-created automatic attempt is fail-closed.
                    # Persist its failure so the owner does not remain an
                    # indefinitely paused repeat session with no retry path;
                    # an explicit public start/resume can perform fresh checks.
                    self.controller.block(
                        f"automatic restart startup failed: {exc}",
                        invalidation_cause=invalidation_cause,
                        expected_generation=startup_generation,
                        expected_state=ControllerState.PAUSED,
                    )
                else:
                    self.last_error = ""
                return self.status_dict()
            return self._run_once_owned()
        finally:
            lock.release()

    def _run_once_owned(self) -> dict[str, Any]:
        with self.controller.lock:
            # REOPEN is resolved from the fresh frame below.  Its action may
            # itself cause the fake or real client to advance to the next
            # cadence boundary before the next capture (the client can finish
            # the transition between two frames).  Let that one post-action
            # frame prove AH-open; _advance enforces the deadline while the
            # frame still reports it closed.
            current = self.controller.status()
            fence = self.controller.observation_fence()
            status = current if current.state == ControllerState.REOPEN else self.controller.tick(fence=fence)
            generation = status.generation
            # A transition may have completed between a previous owner tick
            # and this capture.  Re-read mode under the same fence before any
            # producer work, so an engine observation cannot be admitted as an
            # interaction observation or vice versa.
            if status.mode.value == "interaction" and status.state not in {
                ControllerState.NATIVE_CALIBRATION,
                ControllerState.INPUT_CALIBRATION,
                ControllerState.READY,
                ControllerState.RECONCILE,
                ControllerState.LOGIN_REQUIRED,
            }:
                return self.status_dict()
        if status.mode.value == "interaction" and status.state in {
            ControllerState.NATIVE_CALIBRATION,
            ControllerState.INPUT_CALIBRATION,
        }:
            # Interaction startup is a durable gate, not an immediate READY
            # shortcut.  Run each gate through the existing supervised
            # calibration worker and let the next owner tick reconcile the
            # resulting ARMED/RECONCILE state.  A running operation is left
            # alone; no second calibration or input lease may be queued.
            with self._calibration_state_lock:
                operation_state = self._calibration_operation.get("state")
            if operation_state not in {"starting", "running"}:
                try:
                    self.start_calibration(
                        "native" if status.state == ControllerState.NATIVE_CALIBRATION else "input",
                        expected_generation=generation,
                    )
                except DriverError as exc:
                    if "already running" not in str(exc):
                        self.last_error = str(exc)
            return self.status_dict()
        if status.state in {
            ControllerState.STOPPED,
            ControllerState.PAUSED,
            ControllerState.BLOCKED,
            ControllerState.NATIVE_CALIBRATION,
            ControllerState.INPUT_CALIBRATION,
        }:
            return self.status_dict()
        # READY interaction is intentionally quiescent while an operator is
        # idle.  A live, process-local mail-cycle capability (or a started
        # standalone executor continuation) is the narrow exception: its
        # owner must return through the normal fresh capture/_advance path so
        # route, completion, and AH proof gates can make progress.  Do not
        # treat a stale checkpoint or a generic executor-active marker as an
        # authority to capture or emit input.
        mail_cycle = self.controller.mail_cycle_checkpoint()
        mail_cycle_live = self.controller.mail_cycle_capability() is not None and mail_cycle.get(
            "phase"
        ) not in {"none", "complete", "blocked"}
        standalone_continuation = self._executor_continuation_capability is not None
        if (
            status.mode.value == "interaction"
            and status.state == ControllerState.READY
            and not status.action_intent
            and not mail_cycle_live
            and not standalone_continuation
        ):
            return self.status_dict()
        if status.state == ControllerState.QUALIFY_PENDING:
            # Qualification staged its complete evidence and publication
            # witnesses before the archive boundary.  A fresh owner retries
            # from that durable checkpoint without recapturing the game or
            # retaining an old in-memory SweepEvidence object.
            try:
                self.controller.retry_pending_qualification(
                    expected_generation=status.generation,
                    expected_state=ControllerState.QUALIFY_PENDING,
                    fence=fence,
                )
            except JournalError as exc:
                # The controller preserves the exact QUALIFY_PENDING
                # checkpoint when disk-only publication cannot write.  Do not
                # call block(), whose journal save would overwrite that retry
                # boundary; the owner can restart and retry from disk.
                self.last_error = str(exc)
                return self.status_dict()
            except (DriverError, EvidenceError, ModelError, OSError, ValueError) as exc:
                self.last_error = str(exc)
                self._block_if_current(self.last_error, generation)
            return self.status_dict()
        capture_deadline: float | None = None
        capture_clock: Callable[[], float] | None = None
        capture_owner = ""
        with self.controller.lock:
            live_cycle = self.controller.mail_cycle_checkpoint()
            live_capability = self.controller.mail_cycle_capability()
            if live_capability is not None and live_cycle.get("phase") not in {"none", "complete", "blocked"}:
                capture_owner = "mail cycle"
                capture_deadline = live_capability.deadline_monotonic
                capture_clock = self._mail_cycle_clock
                if live_cycle.get("phase") == "executing":
                    wait_deadline = self._mail_cycle_wait_deadlines.get(live_capability.cycle_id)
                    # A started callback may already carry the fresh
                    # ``mail_items`` boundary.  Only a validated current
                    # ``mail_wait`` owns the process-local wait deadline;
                    # when that record is absent, capture under the fixed
                    # cycle bound so _mail_loop_step can distinguish the
                    # settled boundary from an actually unresolved wait.
                    if wait_deadline is not None:
                        capture_deadline = min(capture_deadline, wait_deadline)
            elif self._executor_continuation_capability is not None:
                capture_owner = "standalone executor"
                capture_deadline = self._executor_continuation_wait_deadline
                capture_clock = self._native_clock
                if capture_deadline is None:
                    self.controller.block(
                        "standalone executor wait deadline is missing; no retry is permitted"
                    )
                    self.last_error = "standalone executor wait deadline is missing"
                    return self.status_dict()
        if capture_deadline is not None and capture_clock is not None and capture_clock() >= capture_deadline:
            reason = f"{capture_owner} observation deadline expired; no retry is permitted"
            if capture_owner == "mail cycle" and live_capability is not None:
                self.controller.block_mail_cycle(reason, capability=live_capability)
            else:
                self.controller.block(reason)
            self.last_error = reason
            return self.status_dict()
        try:
            sample = (
                self.capture_status(deadline=capture_deadline)
                if capture_deadline is not None
                else self.capture_status()
            )
        except (DriverError, EvidenceError, OSError, ValueError) as exc:
            # Reloads can remove the telemetry canvas while the client loads. The
            # durable expected boot/deadline handles this bounded absence.
            menu_or_logout_reconcile = (
                status.state == ControllerState.RECONCILE
                and self.controller.journal.read().get("restartUncertain") is True
                and status.action_intent
                in {
                    "logout",
                    "dismiss_disconnect",
                    "reconnect_game",
                    "select_arthur",
                    "enter_world",
                }
            )
            if isinstance(exc, ProtocolError) and not menu_or_logout_reconcile:
                self.last_error = str(exc)
                self._block_if_current(
                    f"corrupt telemetry frame: {exc}",
                    generation,
                    invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                )
                return self.status_dict()
            if status.state in {ControllerState.SAVE_RELOAD_SENT, ControllerState.ACK_RELOAD_SENT}:
                return self.status_dict()
            if status.state == ControllerState.DISK_CLEANUP_WAIT:
                try:
                    self._advance({})
                except (DriverError, EvidenceError, OSError, ValueError) as cleanup_error:
                    self.last_error = str(cleanup_error)
                    self._block_if_current(self.last_error, generation)
                return self.status_dict()
            if status.state in {
                ControllerState.ARMED,
                ControllerState.RECONCILE,
                ControllerState.LOGIN_REQUIRED,
            }:
                try:
                    uncertain_reconcile = (
                        status.state == ControllerState.RECONCILE
                        and self.controller.journal.read().get("restartUncertain") is True
                    )
                    menu_reconciled = uncertain_reconcile and self._reconcile_menu_intent_without_agent(
                        generation=generation
                    )
                    if menu_reconciled:
                        return self.status_dict()
                    completed_logout = uncertain_reconcile and self._reconcile_completed_logout_without_agent(
                        generation=generation
                    )
                    if completed_logout:
                        return self.status_dict()
                    if uncertain_reconcile:
                        # RECONCILE owns an uncertain external effect.  A
                        # missing canvas or filesystem read is an unresolved
                        # observation, never permission to issue login input.
                        # Enter-world can legitimately have no Agent while the
                        # scene loads; the durable deadline will bound this
                        # wait.  A malformed frame still fails closed below.
                        if isinstance(exc, OSError):
                            return self.status_dict()
                        if status.action_intent in {
                            "logout",
                            "dismiss_disconnect",
                            "reconnect_game",
                            "select_arthur",
                        }:
                            return self.status_dict()
                        if status.action_intent == "enter_world" and isinstance(exc, ProtocolError):
                            return self.status_dict()
                        if isinstance(exc, ProtocolError):
                            self.last_error = str(exc)
                            self._block_if_current(
                                f"corrupt telemetry frame: {exc}",
                                generation,
                                invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                            )
                            return self.status_dict()
                        return self.status_dict()
                    if status.state != ControllerState.LOGIN_REQUIRED:
                        with self.controller.lock:
                            current_status = self.controller.status()
                            if current_status.generation != generation:
                                return self.status_dict()
                            self.controller.reconcile(
                                identity_ok=True,
                                world_ready=False,
                                expected_generation=current_status.generation,
                                expected_state=current_status.state,
                                fence=self.controller.observation_fence(),
                            )
                    self._recover_login()
                except (DriverError, EvidenceError, ModelError, OSError, ValueError) as recovery_error:
                    self.last_error = str(recovery_error)
                    self._block_if_current(self.last_error, generation)
                return self.status_dict()
            self.last_error = str(exc)
            self._block_if_current(self.last_error, generation)
            return self.status_dict()
        if capture_deadline is not None and capture_clock is not None and capture_clock() >= capture_deadline:
            reason = f"{capture_owner} observation deadline expired; no retry is permitted"
            if capture_owner == "mail cycle" and live_capability is not None:
                self.controller.block_mail_cycle(reason, capability=live_capability)
            else:
                self.controller.block(reason)
            self.last_error = reason
            return self.status_dict()
        if sample.kind == "NO_CHANGE":
            if (
                capture_owner == "mail cycle"
                and live_capability is not None
                and live_cycle.get("phase") == "executing"
                and self._mail_cycle_wait_deadlines.get(live_capability.cycle_id) is None
            ):
                reason = "mail completion wait evidence is missing; no retry is permitted"
                self.controller.block_mail_cycle(reason, capability=live_capability)
                self.last_error = reason
            return self.status_dict()
        if sample.capture_monotonic < 0:
            self.last_error = "stale capture sample"
            self._block_if_current(
                self.last_error,
                generation,
                invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
            )
            return self.status_dict()
        snapshot = sample.frame.snapshot.value if sample.frame else {}
        self.last_snapshot = snapshot
        persisted = dict(snapshot)
        if isinstance(persisted.get("chatEdit"), dict):
            persisted["chatEdit"] = dict(persisted["chatEdit"])
            persisted["chatEdit"]["text"] = ""

        def persist_telemetry(value: dict[str, Any]) -> None:
            # Journal updates are optimistic as well as serialized.  If IPC
            # pause/stop advanced the durable generation while a capture was
            # being decoded, this callback must leave the operator's state
            # untouched rather than writing an old telemetry snapshot over it.
            current_status = self.controller.status()
            if (
                current_status.generation != generation
                or value.get("generation") != generation
                or value.get("state")
                in {
                    ControllerState.PAUSED.value,
                    ControllerState.BLOCKED.value,
                    ControllerState.STOPPED.value,
                }
            ):
                # A journal implementation may have read its value before an
                # operator pause while waiting in a writer hook.  Refresh the
                # entire object so the eventual replace preserves that pause.
                if current_status.generation != generation:
                    fresh = self.controller.journal.read()
                    value.clear()
                    value.update(fresh)
                return
            value["telemetry"] = persisted

        with self.controller.lock:
            if not self.controller.admit_observation(fence):
                return self.status_dict()
            self.controller.journal.update(persist_telemetry)
        try:
            with self.controller.lock:
                if not self.controller.admit_observation(fence):
                    return self.status_dict()
                current = self.controller.observe_frame(
                    boot=sample.frame.boot,
                    sequence=sample.frame.sequence,
                    crc32=sample.frame.crc32,
                    fence=fence,
                )
                if current.state == ControllerState.BLOCKED:
                    return self.status_dict()
            if current.state not in {
                ControllerState.DISK_CLEANUP_SENT,
                ControllerState.DISK_CLEANUP_WAIT,
            }:
                self._validate_identity(snapshot, state=current.state)
            with self.controller.lock:
                if self.controller.status().generation != generation:
                    return self.status_dict()
                next_fence = self.controller.observation_fence()
            self._advance(snapshot, fence=next_fence)
        except (DriverError, EvidenceError, ModelError, OSError, ValueError) as exc:
            if (
                isinstance(exc, OSError)
                and current.state == ControllerState.RECONCILE
                and self.controller.journal.read().get("restartUncertain") is True
            ):
                # A stable-file or capture read that temporarily disappears
                # cannot authorize a new action while an external effect is
                # still uncertain.  Keep the durable reconcile intent intact.
                return self.status_dict()
            self.last_error = str(exc)
            self._block_if_current(self.last_error, generation)
        else:
            self.last_error = ""
        return self.status_dict()

    def _block_if_current(
        self,
        reason: str,
        generation: int,
        *,
        invalidation_cause: InvalidationCause = InvalidationCause.UNKNOWN,
    ) -> None:
        """Block only the live observation epoch that raised ``reason``.

        Capture, model, and durable-file I/O happen outside the controller
        mutex. An operator pause or stop may therefore win while that work is
        in flight; this helper refuses to overwrite that newer state.
        """

        with self.controller.lock:
            current = self.controller.status()
            if current.generation != generation or current.state in {
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
                ControllerState.STOPPED,
            }:
                return
            self.controller.block(
                reason,
                invalidation_cause=invalidation_cause,
                expected_generation=current.generation,
                expected_state=current.state,
                fence=self.controller.observation_fence(),
            )

    def mail_loop_start(
        self,
        *,
        generation: int,
        queue_id: str,
        seq: int,
        confirm_queue_id: str,
    ) -> dict[str, Any]:
        """Enable one process-local queue binding; this does not arm or click."""

        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise DriverError("mail-loop generation is malformed")
        try:
            queue_size = len(queue_id.encode("utf-8")) if isinstance(queue_id, str) else -1
        except UnicodeError as exc:
            raise DriverError("mail-loop queueId is not valid UTF-8") from exc
        if not isinstance(queue_id, str) or not queue_id or queue_size > 192:
            raise DriverError("mail-loop queueId is malformed")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise DriverError("mail-loop seq is malformed")
        if confirm_queue_id != "":
            raise DriverError("mail-loop start confirmation must be empty")
        with self.controller.lock:
            status = self.controller.status()
            if status.generation != generation:
                raise DriverError("stale generation")
            if status.mode.value != "repeat":
                raise DriverError("mail-loop start requires repeat mode")
            if self.controller.mail_cycle_checkpoint().get("phase") not in {"none", "complete", "blocked"}:
                raise DriverError("mail-loop already owns a live cycle")
            self._mail_loop_control_epoch += 1
            reservation_epoch = self._mail_loop_control_epoch

        # The capture is intentionally outside the controller lock.  The
        # reservation above linearizes it against stop/replacement start; the
        # commit below rejects any frame captured by a superseded owner.
        sample = self.capture_status()
        if sample.kind != "FRESH" or sample.frame is None:
            raise DriverError("mail-loop start requires a fresh authoritative executor frame")
        value = sample.frame.snapshot.value
        validated = protocol.validate_snapshot(value)
        executor = validated.value.get("executor")
        context = validated.value.get("context")
        house = validated.value.get("house")
        if (
            not isinstance(executor, dict)
            or executor.get("availability") != "valid"
            or executor.get("queueId") != queue_id
            or executor.get("currentSeq") != seq
            or executor.get("currentKind") != "mail_collect"
            or executor.get("phase") != "ready"
            or executor.get("inFlight") is not False
            or executor.get("workDone") != 0
            or isinstance(executor.get("workTotal"), bool)
            or not isinstance(executor.get("workTotal"), int)
            or executor.get("workTotal") <= 0
            or executor.get("blocked") is not False
            or executor.get("latched") is not False
            or executor.get("ambiguous") is not False
            or not isinstance(context, dict)
            or context.get("character") != self.config.profile.character
            or context.get("realm") != self.config.profile.realm
            or not isinstance(house, dict)
            or house.get("key") != self.config.profile.house
            or executor.get("house") != self.config.profile.house
            or executor.get("character") != self.config.profile.character
        ):
            raise DriverError("mail-loop start queue is not the current authoritative mail action")
        with self.controller.lock:
            status = self.controller.status()
            if (
                reservation_epoch != self._mail_loop_control_epoch
                or status.generation != generation
                or status.mode.value != "repeat"
                or self.controller.mail_cycle_checkpoint().get("phase") not in {"none", "complete", "blocked"}
            ):
                raise DriverError("mail-loop start reservation is stale")
            self._mail_loop_binding = MailLoopBinding(
                enabled=True,
                generation=generation,
                queue_id=queue_id,
                seq=seq,
                house=self.config.profile.house,
                character=self.config.profile.character,
            )
            self._mail_loop_active_binding = None
        return self.mail_loop_status()

    def mail_loop_stop(self, *, generation: int) -> dict[str, Any]:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise DriverError("mail-loop generation is malformed")
        with self.controller.lock:
            if self.controller.status().generation != generation:
                raise DriverError("stale generation")
            self._mail_loop_control_epoch += 1
            cycle = self.controller.mail_cycle_checkpoint()
            try:
                if cycle.get("phase") not in {"none", "complete", "blocked"}:
                    self.controller.block_mail_cycle("mail-loop stopped")
            finally:
                # Disabling the process-local opt-in is independent of the
                # archive/revoke result; a control error must not leave the
                # binding able to admit another cycle in this owner.
                self._mail_loop_binding = MailLoopBinding()
                self._mail_loop_active_binding = None
                self._mail_cycle_wait_deadlines.clear()
        return self.mail_loop_status()

    def mail_loop_status(self) -> dict[str, Any]:
        with self.controller.lock:
            binding = self._mail_loop_binding
            cycle = self.controller.mail_cycle_checkpoint()
            # A status query is also used by narrow recovery/control probes
            # that construct the daemon around an already-owned controller.
            # Keep the public shape identical when no admission decision has
            # yet been recorded in that process.
            last_decision = copy.deepcopy(getattr(self, "_mail_loop_last_decision", {}))
        return {
            "enabled": binding.enabled,
            "generation": binding.generation,
            "queueId": binding.queue_id,
            "seq": binding.seq,
            "house": binding.house,
            "character": binding.character,
            "cycle": cycle,
            "lastDecision": last_decision,
        }

    def _mail_operation_bounds(self) -> MailOperationBounds:
        routes = self.route_file.routes if isinstance(self.route_file, RouteFile) else {}
        to_mailbox = routes.get("auctioneer_to_mailbox")
        to_auctioneer = routes.get("mailbox_to_auctioneer")
        if to_mailbox is None or to_auctioneer is None:
            raise DriverError("mail-loop requires both measured mailbox route files")
        return MailOperationBounds(
            route_to_mailbox_seconds=float(walk_budget(to_mailbox)[1]),
            world_click_seconds=60.0,
            mail_click_seconds=60.0,
            mail_postcondition_seconds=float(self._NATIVE_FRAME_TIMEOUT_SECONDS),
            route_to_auctioneer_seconds=float(walk_budget(to_auctioneer)[1]),
            ah_reopen_seconds=float(self.config.reload_deadline_seconds),
            safety_margin_seconds=float(MOTION_POST_RELEASE_RESERVE_SECONDS),
        )

    def _mail_boundary(self, snapshot: dict[str, Any], status: Any) -> MailLoopBoundary:
        journal = self.controller.journal.read()
        evidence = journal.get("current", {}).get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        seen = frozenset(
            str(record.get("ackUid"))
            for record in self.controller.journal.archives("mail_cycle")
            if isinstance(record, dict) and isinstance(record.get("ackUid"), str) and record.get("ackUid")
        )
        return MailLoopBoundary(
            generation=status.generation,
            session_mode=status.mode.value,
            repeat_phase=ControllerState.WAIT_DUE.value,
            ack_loaded_uid=str(evidence.get("ackLoadedUID", "")),
            status_last_uid=status.last_uid,
            seen_ack_uids=seen,
            unresolved_action=bool(status.action_intent),
            reconciliation_pending=bool(self.controller.journal.read().get("reconciliationRequired", False)),
            stop_requested=status.stop_requested,
            closeout_pending=status.closeout_requested,
            last_actual_start_at=float(status.started_at) if status.started_at > 0 else None,
            now=float(snapshot.get("wallTime", 0)),
        )

    def _measure_mailbox_anchor(
        self,
        generation: int,
        *,
        operation_bound: float | None = None,
        operation_deadline: float | None = None,
    ) -> tuple[float, float]:
        """Return the measured model-crop center in client-relative pixels."""

        if operation_bound is not None and operation_deadline is not None:
            raise DriverError("mailbox model observation supplied two operation bounds")
        if operation_deadline is not None:
            if (
                isinstance(operation_deadline, bool)
                or not isinstance(operation_deadline, (int, float))
                or not math.isfinite(float(operation_deadline))
            ):
                raise DriverError("mailbox model operation deadline is malformed")
            deadline = min(self._motion_deadline(), float(operation_deadline))
            self._remaining_motion_budget(deadline)
        else:
            deadline = self._motion_deadline(limit=operation_bound)
        before = self.driver.resolve_identity(require_foreground=True, deadline_monotonic=deadline)
        with self.controller.lock:
            status = self.controller.status()
            if status.generation != generation or status.mode.value != "interaction":
                raise DriverError("mailbox model observation has stale controller ownership")
            lease_generation = status.lease_generation
        if not self.driver.guardian.armed or self.driver.guardian.generation != lease_generation:
            raise DriverError("mailbox model observation lacks the current Guardian lease")
        retained_path, image_digest, captured_at = self._capture_model_artifact(
            before, generation, deadline=deadline
        )
        image = retained_path.read_bytes()
        if hashlib.sha256(image).hexdigest() != image_digest:
            raise DriverError("retained mailbox model crop hash changed before inference")
        remaining = self._remaining_motion_budget(deadline) - MOTION_POST_RELEASE_RESERVE_SECONDS
        if remaining <= 0:
            raise DriverError("mailbox model observation has no feedback reserve")
        observation = self._call_model_adapter(
            "observe",
            image,
            target="game-client",
            verified_crop=self.config.profile.model_crop,
            requested_state="mailbox_world",
            requested_control="mailbox_object",
            timeout=remaining,
        )
        if getattr(observation, "state", None) != "mailbox_world":
            raise DriverError("mailbox model observation did not prove mailbox_world")
        facts = dict(getattr(observation, "facts", {}))
        if (
            facts.get("control") != "mailbox_object"
            or facts.get("scene") != "world"
            or facts.get("character") != self.config.profile.character
            or facts.get("realm") != self.config.profile.realm
            or facts.get("modal") != ""
            or facts.get("code") != ""
            or facts.get("idle") != "true"
        ):
            raise DriverError("mailbox model observation lacks the named mailbox control")
        if self._native_clock() - captured_at > 5.0:
            raise DriverError("mailbox model observation is stale")
        bounds = getattr(observation, "anchor", None)
        crop_x, crop_y, crop_w, crop_h = self.config.profile.model_crop
        if (
            not isinstance(bounds, tuple)
            or len(bounds) != 4
            or any(isinstance(item, bool) or not isinstance(item, int) for item in bounds)
            or bounds[2] <= 0
            or bounds[3] <= 0
            or bounds[0] < 0
            or bounds[1] < 0
            or bounds[0] + bounds[2] > crop_w
            or bounds[1] + bounds[3] > crop_h
        ):
            raise DriverError("mailbox model object bounds are outside the retained crop")
        hint = (float(crop_x + bounds[0] + bounds[2] // 2), float(crop_y + bounds[1] + bounds[3] // 2))
        identity = self.driver.resolve_identity(require_foreground=True, deadline_monotonic=deadline)
        if identity.fingerprint != before.fingerprint or self.driver.guardian.generation != lease_generation:
            raise DriverError("mailbox model observation changed identity or Guardian lease")
        if not (0 <= hint[0] < identity.child_geometry[2] and 0 <= hint[1] < identity.child_geometry[3]):
            raise DriverError("mailbox model object center is outside the verified client")
        self._mailbox_model_proof = {
            "path": str(retained_path),
            "sha256": image_digest,
            "capturedAt": captured_at,
            "identity": before.fingerprint,
            "generation": generation,
            "leaseGeneration": lease_generation,
            "bounds": list(bounds),
            "hint": [hint[0], hint[1]],
        }
        self._record_visual_proof(
            {"kind": "mailbox_anchor", **self._mailbox_model_proof},
            generation=generation,
            state=ControllerState.READY,
        )
        return hint

    def _admit_mail_loop(self, snapshot: dict[str, Any], status: Any, fence: Any) -> bool:
        binding = self._mail_loop_binding
        if not binding.enabled or status.state is not ControllerState.WAIT_DUE:
            return False
        capability = None
        try:
            validated = protocol.validate_snapshot(snapshot)
            boundary = self._mail_boundary(snapshot, status)
            bounds = self._mail_operation_bounds()
            decision = admit_mail_cycle(validated, binding, boundary, bounds)
            self._mail_loop_last_decision = {
                "admitted": decision.admitted,
                "deferred": decision.deferred,
                "reason": decision.reason,
                "queueId": decision.queue_id,
                "seq": decision.admitted_seq,
                "ackUid": decision.ack_uid,
                "mailUnits": decision.mail_units,
                "effectiveDueAt": decision.effective_due_at,
                "completionDeadlineAt": decision.completion_deadline_at,
            }
            if not decision.admitted:
                return False
            with self.controller.lock:
                if binding is not self._mail_loop_binding or not self._mail_loop_binding.enabled:
                    raise RuntimeError("mail-loop binding was replaced before admission")
                capability = self.controller.begin_mail_cycle(
                    decision=decision,
                    ack_loaded_uid=boundary.ack_loaded_uid,
                    status_last_uid=boundary.status_last_uid,
                    wire_ack_uids=tuple(snapshot.get("loaded", {}).get("ackedIds", ())),
                    queue_id=binding.queue_id,
                    seq=binding.seq,
                    house=binding.house,
                    character=binding.character,
                    snapshot=validated,
                    binding=binding,
                    bounds=bounds,
                    expected_generation=status.generation,
                    expected_lease_generation=status.lease_generation,
                )
                self._mail_loop_active_binding = binding
            self._mail_cycle_context.cycle_id = capability.cycle_id
            try:
                self._mail_loop_step(snapshot, capability)
            finally:
                try:
                    del self._mail_cycle_context.cycle_id
                except AttributeError:
                    pass
            return True
        except (DriverError, RuntimeError, ValueError, KeyError, TypeError, protocol.ProtocolError) as exc:
            if capability is not None:
                try:
                    self.controller.block_mail_cycle(str(exc), capability=capability)
                except Exception:
                    pass
            self._mail_loop_last_decision = {"admitted": False, "deferred": True, "reason": str(exc)}
            return False

    def _mail_loop_step(self, snapshot: dict[str, Any], capability: Any) -> None:
        cycle = self.controller.mail_cycle_checkpoint()
        phase = cycle.get("phase")
        # A confirmation wait is still a live bounded owner.  Once its fixed
        # deadline has elapsed, fence the cycle immediately; do not leave a
        # stale request live until another click or route happens to observe
        # the expiry.
        if phase not in {"none", "complete", "blocked"}:
            if self._mail_cycle_clock() >= capability.deadline_monotonic:
                self.controller.block_mail_cycle(
                    "mail cycle completion deadline expired; no retry is permitted",
                    capability=capability,
                )
                raise DriverError("mail cycle completion deadline expired")
        if phase in {"preparing", "opening_ah"}:
            raise DriverError(f"mail cycle has an uncertain {phase} physical boundary")
        budget = capability.budget
        if phase == "to_mailbox":
            route_deadline = min(
                capability.deadline_monotonic,
                self._mail_cycle_clock() + budget.route_to_mailbox_seconds,
            )
            self._mail_cycle_require_budget(
                capability,
                budget.route_to_mailbox_seconds,
                "auctioneer-to-mailbox route",
                operation_deadline=route_deadline,
            )
            self.controller.transition_mail_cycle("preparing", capability=capability)
            self.execute_route_walk(
                "auctioneer_to_mailbox",
                generation=capability.generation,
                deadline_monotonic=route_deadline,
            )
            self.controller.transition_mail_cycle("mailbox_open", capability=capability)
            phase = "mailbox_open"
        if phase == "mailbox_open":
            # Model capture, hover/click, and post-click MAIL_SHOW feedback
            # share one fixed world-operation deadline.  Never reset a fresh
            # bound after the model has consumed part of it.
            world_deadline = min(
                capability.deadline_monotonic,
                self._mail_cycle_clock() + budget.world_click_seconds,
            )
            self._mail_cycle_require_budget(
                capability,
                budget.world_click_seconds,
                "mailbox world click",
                operation_deadline=world_deadline,
            )
            hint_x, hint_y = self._measure_mailbox_anchor(
                capability.generation,
                operation_deadline=world_deadline,
            )
            self._mail_cycle_require_budget(
                capability,
                budget.world_click_seconds,
                "mailbox world click",
                operation_deadline=world_deadline,
            )
            self.execute_world_click(
                "Mailbox",
                hint_x,
                hint_y,
                0.0,
                expect="mailOpen",
                generation=capability.generation,
                deadline_monotonic=world_deadline,
            )
            self.controller.transition_mail_cycle("awaiting_confirmation", capability=capability)
            # The default path is an actual unarmed dry-run: it traverses the
            # measured button/Guardian/native callback path once, but cannot
            # invoke a protected API.  Armed operation still waits for a
            # separate exact executor-click request.
            fresh = self._observe_motion(
                capability.generation,
                allow_executor=True,
                deadline_monotonic=world_deadline,
            )
            self._executor_scene_guard(fresh)
            executor = self._executor_snapshot_for_frame(fresh)
            if executor.get("publishedArmed") is False and executor.get("sessionArmed") is False:
                click_deadline = min(
                    capability.deadline_monotonic,
                    self._mail_cycle_clock() + budget.mail_click_seconds + budget.mail_postcondition_seconds,
                )
                self._mail_cycle_require_budget(
                    capability,
                    budget.mail_click_seconds + budget.mail_postcondition_seconds,
                    "mail executor click",
                    operation_deadline=click_deadline,
                )
                self.execute_executor_click(
                    queue_id=capability.queue_id,
                    seq=capability.admitted_seq,
                    confirm_queue_id="",
                    cycle_id=capability.cycle_id,
                    generation=capability.generation,
                    deadline_monotonic=click_deadline,
                )
        elif phase == "executing":
            executor = snapshot.get("executor")
            if not isinstance(executor, dict):
                raise DriverError("mail completion snapshot lacks executor projection")
            observed_phase = executor.get("phase")
            if observed_phase == "mail_items":
                # The addon can publish the first successful mail-item
                # boundary in the same callback as the started result.  It
                # settles the previous wait; no second click is implicit.
                self.controller.record_mail_cycle_progress(
                    capability=capability,
                    executor=executor,
                )
                self._mail_cycle_wait_deadlines.pop(capability.cycle_id, None)
                self.controller.transition_mail_cycle(
                    "awaiting_confirmation",
                    capability=capability,
                    last_executor_seq=capability.admitted_seq,
                )
            else:
                # Missing wait evidence is fail-closed only for an actual
                # mail_wait boundary.  _observe_mail_completion enforces the
                # fixed per-attempt deadline and never synthesizes one.
                self._observe_mail_completion(capability)
        elif phase == "to_auctioneer":
            route_deadline = min(
                capability.deadline_monotonic,
                self._mail_cycle_clock() + budget.route_to_auctioneer_seconds,
            )
            self._mail_cycle_require_budget(
                capability,
                budget.route_to_auctioneer_seconds,
                "mailbox-to-auctioneer route",
                operation_deadline=route_deadline,
            )
            self.execute_route_walk(
                "mailbox_to_auctioneer",
                generation=capability.generation,
                deadline_monotonic=route_deadline,
            )
            frame = self._observe_motion(
                capability.generation,
                allow_executor=True,
                deadline_monotonic=route_deadline,
            )
            if frame.mail_open:
                raise DriverError("mailbox remained open after return route")
            returned_snapshot = self._motion_snapshots.get(
                (frame.boot, frame.sequence, frame.identity_fingerprint)
            )
            if (
                not isinstance(returned_snapshot, dict)
                or returned_snapshot.get("mailOpen") is not False
                or returned_snapshot.get("ahOpen") is not False
                or not self._blocking_ui_is_clear(returned_snapshot)
            ):
                raise DriverError("return route lacks a fresh closed-mailbox world proof")
            self.controller.transition_mail_cycle("opening_ah", capability=capability)
            ah_deadline = min(
                capability.deadline_monotonic,
                self._mail_cycle_clock() + budget.ah_reopen_seconds,
            )
            self._mail_cycle_require_budget(
                capability,
                budget.ah_reopen_seconds,
                "Auction House reopen",
                operation_deadline=ah_deadline,
            )
            ah_snapshot = self._run_action(
                "open_ah",
                returned_snapshot,
                allow_mail_cycle=True,
                deadline_monotonic=ah_deadline,
            )
            proof = {
                "fresh": True,
                "ahOpen": ah_snapshot.get("ahOpen"),
                "mailOpen": ah_snapshot.get("mailOpen"),
                "identity": self.driver.resolve_identity(
                    require_foreground=True,
                    deadline_monotonic=ah_deadline,
                ).fingerprint,
                "generation": capability.generation,
                "leaseGeneration": capability.lease_generation,
            }
            # MailFrame:IsShown is the authoritative close fact.  A stale
            # MAIL_CLOSED event cannot satisfy this terminal boundary.
            if proof["mailOpen"]:
                raise DriverError("mail cycle completion observed mailbox still open")
            self.controller.complete_mail_cycle(capability=capability, ah_open_proof=proof)
            # The explicit binding covers only this admitted sequence.  A
            # successor requires a fresh public start request.
            with self.controller.lock:
                # Do not erase a replacement start that won the control
                # epoch race while completion was doing its final native work.
                if self._mail_loop_binding is self._mail_loop_active_binding:
                    self._mail_loop_binding = MailLoopBinding()
                    self._mail_loop_active_binding = None
                completed_generation = self.controller.status().generation
            # Re-enter through the ordinary re-arm/reconcile gate.  The
            # terminal archive never fabricates WAIT_DUE or reuses this
            # cycle's lease; a fresh Guardian owner must prove the next frame.
            try:
                self.controller.resume(expected_generation=completed_generation)
            except Exception as exc:
                # The terminal archive is already durable.  A failed
                # re-arm/reconcile is therefore a blocked recovery boundary,
                # never permission to route or click again.
                self.controller.block(f"mail cycle resume failed: {exc}")
                raise

    def _mail_cycle_clock(self) -> float:
        """Return the monotonic domain used to admit this cycle.

        Controller admission samples its injected clock, which is also the
        DesktopDriver deadline domain.  The daemon may use a separate native
        polling clock for settle observations (notably in deterministic
        fixtures), so mail operation deadlines must not be rebuilt from that
        unrelated source.
        """

        clock = getattr(self.controller, "clock", None)
        if callable(clock):
            value = float(clock())
        else:
            value = float(self._native_clock())
        if not math.isfinite(value):
            raise DriverError("mail cycle clock is not finite")
        return value

    def _mail_cycle_dispatch_allowed(self, capability: Any) -> None:
        if self.controller.mail_cycle_capability() is not capability:
            raise DriverError("mail cycle capability changed")
        with self.controller.lock:
            status = self.controller.status()
            if status.generation != capability.generation or status.state is not ControllerState.READY:
                raise DriverError("mail cycle controller generation changed")
            if (
                not self.driver.guardian.armed
                or self.driver.guardian.generation != capability.lease_generation
            ):
                raise DriverError("mail cycle Guardian lease changed")
        if self._mail_cycle_clock() >= capability.deadline_monotonic:
            raise DriverError("mail cycle completion deadline expired")

    def _mail_cycle_require_budget(
        self,
        capability: Any,
        seconds: float,
        effect: str,
        *,
        operation_deadline: float | None = None,
    ) -> None:
        """Refuse an effect unless the complete remaining workload still fits.

        ``operation_deadline`` is the already-created absolute bound for the
        active model/route/click/wait pipeline. Its remainder replaces the
        full phase estimate; future physical effects retain their complete
        immutable worst-case reservations.
        """

        self._mail_cycle_dispatch_allowed(capability)
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or float(seconds) <= 0
        ):
            raise DriverError(f"{effect} bound is malformed")
        now = self._mail_cycle_clock()
        if operation_deadline is not None:
            if (
                isinstance(operation_deadline, bool)
                or not isinstance(operation_deadline, (int, float))
                or not math.isfinite(float(operation_deadline))
            ):
                raise DriverError(f"{effect} deadline is malformed")
            operation_deadline = float(operation_deadline)
            if operation_deadline <= now:
                raise DriverError(f"{effect} deadline expired")
        cycle = self.controller.mail_cycle_checkpoint()
        phase = cycle.get("phase")
        budget = capability.budget
        if type(budget) is not MailBudget:
            raise DriverError("mail cycle admitted budget is not immutable")
        units = budget.mail_units
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise DriverError("mail cycle admitted mail-unit bound is malformed")
        completed = cycle.get("completedUnits")
        if isinstance(completed, bool) or not isinstance(completed, int) or not 0 <= completed <= units:
            raise DriverError("mail cycle causal progress is malformed")
        terminal_settled = isinstance(cycle.get("terminalOutcomeId"), str) and bool(
            cycle.get("terminalOutcomeId")
        )
        remaining_units = 0 if terminal_settled else units - completed
        world_click = budget.world_click_seconds
        mail_click = budget.mail_click_seconds
        postcondition = budget.mail_postcondition_seconds
        route_to_auctioneer = budget.route_to_auctioneer_seconds
        ah_reopen = budget.ah_reopen_seconds
        future_message_work = remaining_units * (mail_click + postcondition)
        if phase == "executing":
            wait_deadline = self._mail_cycle_wait_deadlines.get(capability.cycle_id)
            if wait_deadline is None:
                raise DriverError("mail completion wait deadline is missing")
            if operation_deadline is None:
                operation_deadline = wait_deadline
            else:
                operation_deadline = min(operation_deadline, wait_deadline)
            # The current message has crossed its click boundary. Its fixed
            # postcondition remainder is active; only later messages retain a
            # full click+postcondition reservation.
            future_units = max(0, remaining_units - 1)
            future_message_work = future_units * (mail_click + postcondition)
        active_remainder = float(seconds)
        if operation_deadline is not None:
            active_remainder = max(0.0, float(operation_deadline) - now)
        remaining = {
            "to_mailbox": active_remainder
            + world_click
            + future_message_work
            + route_to_auctioneer
            + ah_reopen,
            "preparing": active_remainder
            + world_click
            + future_message_work
            + route_to_auctioneer
            + ah_reopen,
            "mailbox_open": active_remainder + future_message_work + route_to_auctioneer + ah_reopen,
            "awaiting_confirmation": active_remainder
            + max(0, remaining_units - 1) * (mail_click + postcondition)
            + route_to_auctioneer
            + ah_reopen,
            "executing": active_remainder + future_message_work + route_to_auctioneer + ah_reopen,
            "to_auctioneer": active_remainder + ah_reopen,
            "opening_ah": active_remainder,
        }.get(phase)
        if remaining is None or not math.isfinite(float(remaining)):
            raise DriverError(f"mail cycle phase {phase!r} has no bounded remaining workload")
        # ``now`` is the one coherent sample used for both the active
        # operation remainder and the final whole-cycle fit.  A second clock
        # read here could reject an exact-fit operation merely because the
        # helper itself elapsed between its two checks.
        if now + float(remaining) > capability.deadline_monotonic:
            raise DriverError(f"mail cycle deadline cannot fit {effect} and its remaining workload")

    def _observe_mail_completion(self, capability: Any) -> None:
        deadline = self._mail_cycle_wait_deadlines.get(capability.cycle_id)
        if deadline is None:
            # A missing process-local physical-attempt record is an
            # unresolved ownership boundary. Do not reconstruct a wait from
            # the broader cycle deadline or authorize another observation.
            self.controller.block_mail_cycle(
                "mail completion wait deadline is missing; no retry is permitted",
                capability=capability,
            )
            raise DriverError("mail completion wait deadline is missing")
        self._mail_cycle_require_budget(
            capability,
            capability.budget.mail_postcondition_seconds,
            "mail completion observation",
            operation_deadline=deadline,
        )
        if self._mail_cycle_clock() >= deadline:
            self.controller.block_mail_cycle(
                "mail completion observation deadline expired; no retry is permitted",
                capability=capability,
            )
            raise DriverError("mail completion observation deadline expired")
        sample = self.capture_status(deadline=deadline)
        # The capture itself is part of the fixed wait operation. A frame that
        # arrives at/after its bound cannot authorize another poll or preserve
        # the cycle as live, even if the overall cycle deadline remains ahead.
        if self._mail_cycle_clock() >= deadline:
            self.controller.block_mail_cycle(
                "mail completion observation deadline expired; no retry is permitted",
                capability=capability,
            )
            raise DriverError("mail completion observation deadline expired")
        self._mail_cycle_require_budget(
            capability,
            capability.budget.mail_postcondition_seconds,
            "mail completion observation",
            operation_deadline=deadline,
        )
        if sample.kind != "FRESH" or sample.frame is None:
            return
        frame = self._motion_frame_from_sample(sample, deadline=deadline)
        self._executor_scene_guard(frame)
        executor = self._executor_snapshot_for_frame(frame)
        last = executor.get("lastResult")
        if not isinstance(last, dict) or last.get("seq") != capability.admitted_seq:
            return
        if last.get("status") not in {"dry_run", "succeeded", "failed", "refused", "skipped"}:
            return
        record = self._executor_frame_record(frame)
        self.controller.record_mail_cycle_progress(
            capability=capability,
            executor=executor,
        )
        daemon_continuation = self._executor_continuation_capability
        self.controller.observe_mail_cycle_completion(
            capability=capability,
            post_crs=record["crs"],
            post_executor=executor,
            post_evidence=record["evidence"],
        )
        self._mail_cycle_wait_deadlines.pop(capability.cycle_id, None)
        # The controller clears its private continuation only after the exact
        # terminal observation has been durably consumed (or durably blocked).
        # Clear this daemon's copy only if it is still the same owner; a newer
        # owner must never be erased by a late completion callback.
        if (
            daemon_continuation is not None
            and self._executor_continuation_capability == daemon_continuation
            and self.controller.executor_continuation_capability() is None
        ):
            self._executor_continuation_capability = None
            self._executor_continuation_wait_deadline = None

    def _observe_standalone_executor_completion(self) -> bool:
        """Observe one started standalone mail action without pressing again."""

        continuation = self._executor_continuation_capability
        if continuation is None:
            return False
        checkpoint = self.controller.executor_click_checkpoint()
        overall = checkpoint.get("overall") if isinstance(checkpoint, dict) else None
        if not isinstance(overall, dict):
            raise DriverError("executor continuation checkpoint is unavailable")
        deadline = self._executor_continuation_wait_deadline
        if deadline is None:
            self.controller.block("standalone executor wait deadline is missing; no retry is permitted")
            raise DriverError("standalone executor wait deadline is missing")
        if self._native_clock() >= deadline:
            self.controller.block("standalone executor completion deadline expired; no retry is permitted")
            raise DriverError("standalone executor completion deadline expired")
        sample = self.capture_status(deadline=deadline)
        if self._native_clock() >= deadline:
            self.controller.block("standalone executor completion deadline expired; no retry is permitted")
            raise DriverError("standalone executor completion deadline expired")
        if sample.kind != "FRESH" or sample.frame is None:
            return False
        frame = self._motion_frame_from_sample(sample, deadline=deadline)
        self._executor_scene_guard(frame)
        executor = self._executor_snapshot_for_frame(frame)
        last = executor.get("lastResult")
        if (
            not isinstance(last, dict)
            or last.get("seq") != overall.get("seq")
            or last.get("kind") != "mail_collect"
            or last.get("status") not in {"dry_run", "succeeded", "failed", "refused", "skipped"}
        ):
            return False
        record = self._executor_frame_record(frame)
        self.controller.observe_executor_completion(
            continuation_capability=continuation,
            post_crs=record["crs"],
            post_executor=executor,
            post_evidence=record["evidence"],
        )
        self._executor_continuation_capability = None
        self._executor_continuation_wait_deadline = None
        return True

    def _validate_identity(self, snapshot: dict[str, Any], *, state: ControllerState) -> None:
        context = snapshot.get("context", {})
        house = snapshot.get("house", {})
        plan = snapshot.get("plan", {})
        expected = self.config.profile
        expected_context = {
            "character": expected.character,
            "realm": expected.realm,
            "playerFaction": self.config.player_faction,
            "locale": self.config.locale,
            "clientVersion": self.config.client_version,
            "clientBuild": self.config.client_build,
            "interface": self.config.interface,
            "addonVersion": self.config.addon_version,
        }
        mismatched = [key for key, value in expected_context.items() if context.get(key) != value]
        if mismatched:
            raise EvidenceError(f"configured client context does not match telemetry: {mismatched}")
        if house.get("key") != expected.house or plan.get("count") != expected.plan_count:
            raise EvidenceError("configured house or plan does not match telemetry")
        if self.config.plan_hash and plan.get("hash") != self.config.plan_hash:
            raise EvidenceError("telemetry plan hash is not externally pinned")
        cursor = snapshot.get("scanner", {}).get("cursorItemID", 0)
        configured_ids = frozenset(_CANONICAL_PLAN["itemIDs"])
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or cursor < 0
            or (cursor and cursor not in configured_ids)
        ):
            raise EvidenceError("telemetry cursor is outside the configured scan plan")

    def _saved_scan_kwargs(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return the immutable identity contract for format-2 evidence."""

        plan_hash = self.config.plan_hash or str(snapshot.get("plan", {}).get("hash", ""))
        if not plan_hash:
            raise EvidenceError("saved scan evidence has no externally pinned plan hash")
        return {
            "house": self.config.profile.house,
            "plan_hash": plan_hash,
            "expected_plan_json": self.config.plan_json or None,
            "expected_context": {
                "character": self.config.profile.character,
                "realm": self.config.profile.realm,
                "playerFaction": self.config.player_faction,
                "locale": self.config.locale,
                "clientVersion": self.config.client_version,
                "clientBuild": self.config.client_build,
                "interface": self.config.interface,
                "addonVersion": self.config.addon_version,
            },
        }

    def _advance(self, snapshot: dict[str, Any], *, fence: Any | None = None) -> None:
        with self.controller.lock:
            status = self.controller.status()
            fence = fence or self.controller.observation_fence()
        if status.mode.value == "interaction":
            cycle = self.controller.mail_cycle_checkpoint()
            capability = self.controller.mail_cycle_capability()
            if cycle.get("phase") not in {"none", "complete", "blocked"}:
                if capability is None:
                    return self.status_dict()
                self._mail_cycle_context.cycle_id = capability.cycle_id
                try:
                    self._mail_loop_step(snapshot, capability)
                except (
                    DriverError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    protocol.ProtocolError,
                ) as exc:
                    try:
                        self.controller.block_mail_cycle(str(exc), capability=capability)
                    except Exception:
                        pass
                    self.last_error = str(exc)
                finally:
                    try:
                        del self._mail_cycle_context.cycle_id
                    except AttributeError:
                        pass
                return
            if self._executor_continuation_capability is not None:
                # A started standalone mail action owns the interaction lease
                # while its addon result settles. Observe only; never route,
                # reconcile, or emit a second click from this wait boundary.
                self._observe_standalone_executor_completion()
                return
            if self.controller.executor_action_active:
                # A blocked/uncertain executor outcome retains the durable
                # interaction fence. A fresh owner must reconcile it; this
                # process never turns it into a retry or ordinary resume.
                return self.status_dict()
            context = snapshot.get("context", {})
            world_ready = (
                isinstance(context, dict)
                and context.get("character") == self.config.profile.character
                and context.get("realm") == self.config.profile.realm
            )
            self.controller.reconcile_interaction(
                identity_ok=True,
                world_ready=world_ready,
                expected_generation=status.generation,
                fence=fence,
            )
            return
        if status.state == ControllerState.DISK_CLEANUP_WAIT:
            if not all((self.config.saved_variables, self.config.companion_state, self.config.data_lua)):
                raise EvidenceError("closeout paths are not configured")
            character_select = getattr(self.driver, "character_select_observed", None)
            if not callable(character_select) or not character_select():
                return
            if queues_empty(
                self.config.saved_variables,
                self.config.companion_state,
                self.config.data_lua,
                settled_uid=(
                    status.last_uid
                    if self.controller.journal.read()
                    .get("current", {})
                    .get("evidence", {})
                    .get("qualification")
                    != "rejected"
                    and status.last_uid
                    else None
                ),
            ):
                self.controller.cleanup_complete(
                    logout_observed=True,
                    expected_generation=status.generation,
                    expected_state=ControllerState.DISK_CLEANUP_WAIT,
                    fence=fence,
                )
            return
        context = snapshot.get("context", {})
        character_ready = (
            context.get("character") == self.config.profile.character
            and context.get("realm") == self.config.profile.realm
        )
        if status.state == ControllerState.RECONCILE and self.controller.journal.read().get(
            "reconciliationRequired"
        ):
            stable_facts = self._reconcile_durable_files(snapshot)
            if stable_facts.get("stable_format2_evidence") is not None:
                self.current_evidence = stable_facts["stable_format2_evidence"]
            uploads = stable_facts["uploaded_ids"]
            data_ids = stable_facts["data_ack_ids"]
            loaded_ids = set(snapshot.get("loaded", {}).get("ackedIds", []))
            pending_ids = set(snapshot.get("pending", {}).get("ids", []))
            loaded = snapshot.get("loaded", {})
            if not isinstance(loaded, dict):
                raise EvidenceError("restart reconciliation loaded telemetry is malformed")
            loaded_uid = status.last_uid if status.last_uid in loaded_ids else ""
            loaded_at = int(snapshot.get("wallTime", 0) or 0) if loaded_uid else 0
            loaded_boot = str(snapshot.get("boot", "")) if loaded_uid else ""
            loaded_sequence = int(snapshot.get("sequence", 0) or 0) if loaded_uid else 0
            last_saved = snapshot.get("lastSaved", {})
            observed_uid = str(last_saved.get("memoryUid", "")) if isinstance(last_saved, dict) else ""
            context = snapshot.get("context", {})
            # Missing scene facts are unresolved.  Treating an incomplete
            # capture as world would permit a login/input recovery action on a
            # stale or character-select frame.
            scene = ""
            if isinstance(context, dict):
                if not context.get("character"):
                    scene = "character_select"
                elif snapshot.get("ahOpen") is True:
                    scene = "auction_house"
                elif snapshot.get("ahOpen") is False and context.get("realm"):
                    scene = "world"
            scanner = snapshot.get("scanner", {})
            fence_armed = None
            if isinstance(scanner, dict):
                schedule_fence = scanner.get("scheduleFence")
                if isinstance(schedule_fence, dict):
                    fence_armed = schedule_fence.get("state") == "armed"
            observed_character = context.get("character", "") if isinstance(context, dict) else ""
            observed_modal = snapshot.get("modal", "")
            if not isinstance(observed_modal, str):
                observed_modal = ""
            selected_arthur = (
                observed_character == self.config.profile.character if isinstance(context, dict) else None
            )
            stable_upload_timestamp = stable_facts["upload_timestamps"].get(status.last_uid, 0)
            stable_companion_uploaded_uid = status.last_uid if status.last_uid in uploads else ""
            stable_data_ack_uid = status.last_uid if status.last_uid in data_ids else ""
            with self.controller.lock:
                current = self.controller.resolve_restart_intent(
                    observed_boot=str(snapshot.get("boot", "")),
                    observed_uid=observed_uid,
                    uploaded_ids=set(uploads),
                    data_ack_ids=set(data_ids),
                    loaded_ids=loaded_ids,
                    pending_ids=pending_ids,
                    scanner_phase=stable_facts["scanner_phase"],
                    scanner_idle=stable_facts["scanner_idle"],
                    stable_scan_ids=stable_facts["stable_scan_ids"],
                    stable_sweep_ids=stable_facts["stable_sweep_ids"],
                    stable_format2_uid=stable_facts["stable_format2_uid"],
                    stable_format2_evidence=stable_facts["stable_format2_evidence"],
                    stable_saved_uid=stable_facts["stable_saved_uid"],
                    stable_upload_timestamp=stable_upload_timestamp,
                    stable_data_ack_uid=stable_data_ack_uid,
                    stable_companion_uploaded_uid=stable_companion_uploaded_uid,
                    loaded_uid=loaded_uid,
                    loaded_at=loaded_at,
                    loaded_boot=loaded_boot,
                    loaded_sequence=loaded_sequence,
                    stable_format2_verified=stable_facts["stable_format2_verified"],
                    stable_files_complete=stable_facts["stable_files_complete"],
                    observed_scene=scene,
                    observed_modal=observed_modal,
                    observed_character=observed_character,
                    observed_ah_open=snapshot.get("ahOpen")
                    if isinstance(snapshot.get("ahOpen"), bool)
                    else None,
                    selected_arthur=selected_arthur,
                    fence_armed=fence_armed,
                    action_postcondition=(
                        True
                        if (status.action_intent == "enter_world" and scene == "world")
                        else (snapshot.get("ahOpen") is True if status.action_intent == "open_ah" else None)
                    ),
                    expected_generation=status.generation,
                    expected_state=ControllerState.RECONCILE,
                    fence=fence,
                )
                if current.state != ControllerState.RECONCILE:
                    return
        if status.state in {ControllerState.ARMED, ControllerState.RECONCILE}:
            if status.state == ControllerState.RECONCILE and self.controller.journal.read().get(
                "restartUncertain", False
            ):
                # Read-only restart reconciliation owns this frame.  Never
                # relog or issue a named action while its effect remains
                # unproved, including a character-select observation.
                return self.status_dict()
            self.controller.reconcile(
                identity_ok=True,
                world_ready=character_ready,
                expected_generation=status.generation,
                expected_state=status.state,
                fence=fence,
            )
            if not character_ready:
                self._recover_login()
            return
        if status.state == ControllerState.LOGIN_REQUIRED:
            if character_ready:
                self.controller.reconcile(
                    identity_ok=True,
                    world_ready=True,
                    expected_generation=status.generation,
                    expected_state=status.state,
                    fence=fence,
                )
                with self.controller.lock:
                    self.controller.clear_recovery_attempts()
                self.recovery_attempts = 0
                self.recovery_actions.clear()
            else:
                self._recover_login()
            return
        scanner = snapshot["scanner"]
        phase = scanner["phase"]
        if status.state in {ControllerState.READY, ControllerState.WAIT_DUE}:
            if status.closeout_requested:
                # A closeout retained across a stop/restart must not turn an
                # idle reconciliation frame into a newly initiated scan.
                self.controller.closeout()
                return
            if status.state is ControllerState.WAIT_DUE:
                admitted_or_fenced = self._admit_mail_loop(snapshot, status, fence)
                if admitted_or_fenced or self.controller.status().state not in {
                    ControllerState.READY,
                    ControllerState.WAIT_DUE,
                }:
                    return
            if (
                phase in {"item", "owned"}
                and int(scanner.get("startedAt", 0) or 0) > 0
                and int(scanner.get("ordinal", 0) or 0) > 0
            ):
                self.controller.begin_scan(
                    started_at=int(scanner["startedAt"]),
                    expected_generation=status.generation,
                    expected_state=status.state,
                    fence=fence,
                )
                return
            last_actual_start = status.started_at
            addon_due_at = int(scanner.get("dueAt", 0) or 0)
            anchored_due_at = last_actual_start + self.config.cadence_seconds if last_actual_start > 0 else 0
            effective_due_at = max(addon_due_at, anchored_due_at)
            if phase == "ready" and snapshot["wallTime"] >= effective_due_at:
                if not snapshot.get("ahOpen"):
                    self._run_action("open_ah", snapshot)
                    return
                if self._command(snapshot, "/crowley scan"):
                    self.controller.begin_scan(
                        started_at=snapshot["wallTime"],
                        expected_generation=status.generation,
                        expected_state=status.state,
                        fence=fence,
                    )
            return
        saved = snapshot.get("lastSaved", {})
        uid = str(saved.get("memoryUid", ""))
        finished_at = int(saved.get("finishedAt", 0))
        if (
            status.state == ControllerState.SCANNING
            and uid
            and uid != status.last_uid
            and finished_at >= status.started_at
        ):
            self.controller.saved(
                uid,
                expected_generation=status.generation,
                expected_state=ControllerState.SCANNING,
                fence=fence,
            )
            return
        if status.state == ControllerState.QUIET_SAVE:
            due_at = int(scanner.get("dueAt", 0))
            if due_at and int(snapshot["wallTime"]) + self.config.quiet_margin_seconds >= due_at:
                raise EvidenceError("save/reload has no quiet margin before the next due start")
            if self._command(snapshot, "/reload"):
                self.decoder.allow_boot_change_once()
                self.controller.reload_sent(
                    expected_generation=status.generation,
                    expected_state=ControllerState.QUIET_SAVE,
                )
                self.controller.expect_boot(snapshot["boot"])
            return
        if status.state == ControllerState.SAVE_RELOAD_WAIT and self.config.saved_variables:
            evidence = read_stable_saved_scan(
                self.config.saved_variables,
                uid=status.last_uid,
                **self._saved_scan_kwargs(snapshot),
            )
            self.current_evidence = evidence
            self.controller.stable_saved(
                evidence,
                expected_generation=status.generation,
                expected_state=ControllerState.SAVE_RELOAD_WAIT,
                fence=fence,
            )
            return
        if status.state == ControllerState.UPLOAD_WAIT and self.config.companion_state:
            evidence = self.current_evidence
            meta = (
                evidence.envelope
                if evidence is not None
                else self.controller.journal.read().get("current", {}).get("evidence", {})
            )
            if evidence is None and self.config.saved_variables and meta.get("planHash"):
                evidence = read_stable_saved_scan(
                    self.config.saved_variables,
                    uid=status.last_uid,
                    **self._saved_scan_kwargs(snapshot),
                )
                self.current_evidence = evidence
                meta = evidence.envelope
            if not meta:
                raise EvidenceError("upload wait has no durable sweep evidence")
            upload_time = read_uploads(self.config.companion_state).get(status.last_uid)
            data_uid = ""
            if self.config.data_lua:
                data_ids = read_data_acknowledged_ids(
                    self.config.data_lua,
                    expected_plan_json=self.config.plan_json,
                    expected_plan_hash=self.config.plan_hash,
                    expected_client_build=_CANONICAL_PLAN["targetClientBuild"],
                )
                if status.last_uid in data_ids:
                    data_uid = status.last_uid
                else:
                    return
            finish = int(meta["finishedAt"])
            if upload_time is None:
                return
            if upload_time < finish or upload_time - finish > self.config.upload_deadline_seconds:
                raise EvidenceError("upload was outside the 60-second finish deadline")
            self.controller.uploaded(
                status.last_uid,
                accepted=True,
                upload_time=upload_time,
                data_ack_uid=data_uid,
                expected_generation=status.generation,
                expected_state=ControllerState.UPLOAD_WAIT,
                fence=fence,
            )
            return
        if status.state == ControllerState.ACK_RELOAD_SENT:
            if status.action_intent == "ack_reload":
                # The addon acknowledgement and companion upload must both be
                # stable before the reload command is dispatched.  This
                # closes the Data.lua publication race and records the exact
                # witnesses in the journal before any input.
                if self.config.data_lua:
                    if status.last_uid not in read_data_acknowledged_ids(
                        self.config.data_lua,
                        expected_plan_json=self.config.plan_json,
                        expected_plan_hash=self.config.plan_hash,
                        expected_client_build=_CANONICAL_PLAN["targetClientBuild"],
                    ):
                        return
                companion_uid = status.last_uid
                if self.config.companion_state:
                    if status.last_uid not in read_uploads(self.config.companion_state):
                        return
                with self.controller.lock:
                    self.controller.record_ack_publication(
                        status.last_uid,
                        companion_uid=companion_uid,
                        data_uid=status.last_uid,
                        publication_time=int(snapshot.get("wallTime", 0)),
                        boot=str(snapshot.get("boot", "")),
                        sequence=int(snapshot.get("sequence", 0)),
                        expected_generation=status.generation,
                        expected_state=ControllerState.ACK_RELOAD_SENT,
                        fence=fence,
                    )
                if self._command(snapshot, "/reload"):
                    self.decoder.allow_boot_change_once()
                    self.controller.ack_reload_command_sent(
                        expected_generation=status.generation,
                        expected_state=ControllerState.ACK_RELOAD_SENT,
                    )
                    self.controller.expect_boot(snapshot["boot"], acknowledgement=True)
            return
        if status.state == ControllerState.ACK_RELOAD_WAIT:
            loaded = set(snapshot.get("loaded", {}).get("ackedIds", []))
            pending = set(snapshot.get("pending", {}).get("ids", []))
            # Data.lua is a pre-reload publication witness.  It is allowed to
            # be refreshed or emptied by the acknowledgement reload, so the
            # post-reload proof uses fresh addon loaded/pending telemetry plus
            # the durable stage values recorded before dispatch.
            if status.last_uid in loaded and status.last_uid not in pending:
                # Reconstruct the complete stable format-2 object before
                # clearing ACK_RELOAD_WAIT.  A loaded UID without its exact
                # evidence is unresolved; it must never be parked for a
                # future sweep or move the controller to REOPEN first.
                evidence = self.current_evidence
                if evidence is not None and evidence.uid != status.last_uid:
                    evidence = None
                if evidence is None:
                    if not self.config.saved_variables:
                        raise EvidenceError("acknowledgement has no configured stable evidence path")
                    evidence = read_stable_saved_scan(
                        self.config.saved_variables,
                        uid=status.last_uid,
                        **self._saved_scan_kwargs(snapshot),
                    )
                if evidence.uid != status.last_uid or not evidence.stable_verified:
                    raise EvidenceError("acknowledgement lacks complete stable format-2 evidence")
                self.current_evidence = evidence
                completed = self.controller.ack_reload_complete(
                    pending_ids=pending,
                    loaded_uid=status.last_uid,
                    loaded_at=int(snapshot.get("wallTime", 0)),
                    boot=str(snapshot.get("boot", "")),
                    sequence=int(snapshot.get("sequence", 0)),
                    expected_generation=status.generation,
                    expected_state=ControllerState.ACK_RELOAD_WAIT,
                    fence=fence,
                )
                # Completing the reload intent moves the controller to
                # REOPEN (or closeout cleanup), so the ACK_RELOAD_WAIT fence
                # is stale by design. Qualify under a fresh fence for the
                # state produced by ack_reload_complete.
                with self.controller.lock:
                    qualification_fence = self.controller.observation_fence()
                self.controller.qualify(
                    evidence,
                    started_at=status.started_at,
                    expected_generation=completed.generation,
                    expected_state=completed.state,
                    fence=qualification_fence,
                )
                self.current_evidence = None
            else:
                self.last_error = f"ack telemetry loaded={loaded!r} pending={pending!r}"
            return
        if status.state == ControllerState.REOPEN:
            if (
                status.deadline_at
                and int(snapshot.get("wallTime", 0)) >= status.deadline_at
                and not snapshot.get("ahOpen")
            ):
                self.controller.block("reopen deadline expired")
                return
            if snapshot.get("ahOpen"):
                self.controller.reopen_complete(
                    expected_generation=status.generation,
                    expected_state=ControllerState.REOPEN,
                    fence=fence,
                )
                self.reopen_action_sent = False
            elif not self.reopen_action_sent:
                self._run_action("open_ah", snapshot)
                self.reopen_action_sent = True
            return
        if status.state == ControllerState.DISK_CLEANUP_SENT:
            # Closeout is a final client boundary.  Do not issue logout from
            # the frame that merely caused entry into cleanup: one more fresh
            # frame must prove the scanner and pending queue are still idle
            # after the acknowledgement reload.
            closeout_sample = self.capture_status()
            if closeout_sample.kind != "FRESH" or closeout_sample.frame is None:
                return
            snapshot = closeout_sample.frame.snapshot.value
            scanner_state = snapshot.get("scanner", {})
            loaded_ids = set(snapshot.get("loaded", {}).get("ackedIds", []))
            pending_ids = set(snapshot.get("pending", {}).get("ids", []))
            phase = scanner_state.get("phase")
            active_uid = str(scanner_state.get("activeSweepId") or "")
            ordinal = int(scanner_state.get("ordinal", 0) or 0)
            started_at = int(scanner_state.get("startedAt", 0) or 0)
            scan_active = bool(active_uid) or (phase in {"item", "owned"} and started_at > 0 and ordinal > 0)
            last_saved = snapshot.get("lastSaved", {})
            saved_uid = str(last_saved.get("memoryUid") or "") if isinstance(last_saved, dict) else ""
            saved_finished = int(last_saved.get("finishedAt", 0)) if isinstance(last_saved, dict) else 0
            # An addon auto-start can race the closeout request.  Adopt that
            # already-running scan before considering logout.  Only a frame
            # that proves the exact prior UID finished after this scan began
            # can be treated as the one normal item->owned->loaded lag.
            completed_lag = (
                scan_active
                and not active_uid
                and bool(status.last_uid)
                and saved_uid == status.last_uid
                and saved_finished >= started_at > 0
                and status.last_uid in loaded_ids
                and not pending_ids
            )
            if scan_active and not completed_lag:
                candidate = (started_at, active_uid)
                if active_uid or self._closeout_scan_candidate == candidate:
                    with self.controller.lock:
                        self.controller.adopt_scan(
                            started_at=started_at or int(snapshot.get("wallTime", 0)),
                            expected_generation=status.generation,
                            expected_state=ControllerState.DISK_CLEANUP_SENT,
                            fence=fence,
                        )
                    self._closeout_scan_candidate = None
                else:
                    self._closeout_scan_candidate = candidate
                return
            self._closeout_scan_candidate = None
            if not snapshot.get("pending", {}).get("ids"):
                if not all((self.config.saved_variables, self.config.companion_state, self.config.data_lua)):
                    raise EvidenceError("closeout paths are not configured")
                evidence_status = (
                    self.controller.journal.read()
                    .get("current", {})
                    .get("evidence", {})
                    .get("qualification", "")
                )
                uploads = read_uploads(self.config.companion_state)
                if status.last_uid and evidence_status != "rejected" and status.last_uid not in uploads:
                    return
                # Arm the addon's atomic pre-logout fence first.  The command
                # itself is only preparation; logout requires a newer frame
                # from the same boot proving that the fence was accepted.
                if not self._scanner_idle_ready(snapshot, status.last_uid):
                    return
                fence_observation = self._closeout_fence_observation
                if (
                    fence_observation is None
                    or fence_observation[0] != str(snapshot.get("boot", ""))
                    or int(snapshot.get("sequence", 0)) <= fence_observation[1]
                ):
                    # The addon's armed bit is not process-local proof that
                    # this owner sent the fence.  On restart always dispatch a
                    # fresh exact fence command, then require a newer frame
                    # from the same boot before logout.
                    if self._command(snapshot, "/crowley session fence"):
                        self._closeout_fence_observation = (
                            str(snapshot.get("boot", "")),
                            int(snapshot.get("sequence", 0)),
                        )
                    return
                if not self._logout_scanner_idle(snapshot, status.last_uid):
                    return
                self._closeout_fence_observation = None
                # The final logout is the client's durable SavedVariables
                # flush.  The precondition is exact pending telemetry plus a
                # settled upload; the postcondition below reads all three
                # stable files without expecting another Agent frame.
                if self._command(snapshot, "/logout"):
                    if self._last_action_generation != self.driver.guardian.generation:
                        return
                    with self.controller.lock:
                        latest = self.controller.status()
                        if (
                            latest.generation != status.generation
                            or latest.state != ControllerState.DISK_CLEANUP_SENT
                        ):
                            return
                        logout_fence = self.controller.observation_fence()
                        self.controller.logout_sent(
                            expected_generation=latest.generation,
                            expected_state=ControllerState.DISK_CLEANUP_SENT,
                            fence=logout_fence,
                        )
            return

    def status_dict(self) -> dict[str, Any]:
        value = self.controller.status()
        durable = self.controller.journal.read()
        drill = self.controller.takeover_drill_record
        startup_evidence = self._startup_evidence
        drill_matches = bool(
            drill is not None and startup_evidence is not None and drill.matches(startup_evidence.environment)
        )
        operation_state = self._takeover_drill_operation.get("state", "idle")
        drill_state = (
            operation_state
            if operation_state in {"running", "awaiting_input", "canceling", "canceled", "failed"}
            else "needed"
            if drill is None
            else "passed"
            if drill_matches
            else "stored_unverified"
        )
        if drill_state in {"running", "canceling"}:
            next_action = "wait"
        elif drill_state == "awaiting_input":
            next_action = "provide_human_input"
        elif drill is None or (startup_evidence is not None and not drill_matches):
            next_action = "takeover_drill"
        elif self.driver.guardian.armed:
            next_action = "automatic"
        else:
            next_action = "start_or_resume"
        route_walk = dict(getattr(self, "_route_walk_status", self._NO_ROUTE_WALK_STATUS))
        if isinstance(route_walk.get("segments"), tuple):
            route_walk["segments"] = list(route_walk["segments"])
        return {
            "state": value.state.value,
            "mode": value.mode.value,
            "generation": value.generation,
            "leaseGeneration": value.lease_generation,
            "stopRequested": value.stop_requested,
            "closeoutRequested": value.closeout_requested,
            "streak": value.streak,
            "blocker": value.blocker or self.last_error,
            "lastUid": value.last_uid,
            "startedAt": value.started_at,
            "expectedBoot": value.expected_boot,
            "actionIntent": value.action_intent,
            "deadlineAt": value.deadline_at,
            "executor": self.controller.executor_click_checkpoint(),
            "mailLoop": self.mail_loop_status(),
            "reconciliationRequired": bool(durable.get("reconciliationRequired", False)),
            "boot": self.last_snapshot.get("boot", ""),
            "sequence": self.last_snapshot.get("sequence", 0),
            "phase": self.last_snapshot.get("scanner", {}).get("phase", ""),
            "dueAt": self.last_snapshot.get("scanner", {}).get("dueAt", 0),
            "lastSaved": self.last_snapshot.get("lastSaved", {}),
            "pending": self.last_snapshot.get("pending", {}),
            "loaded": self.last_snapshot.get("loaded", {}),
            "nativeCalibration": bool(durable.get("nativeCalibration", False)),
            "inputCalibration": bool(durable.get("inputCalibration", False)),
            "calibrationOperation": self._calibration_operation_snapshot(),
            "calibrationEvidence": {
                name: record.get("fingerprint", "")
                for name, record in durable.get("calibrationEvidence", {}).items()
                if isinstance(record, dict)
            },
            "calibrationHistory": durable.get("calibrationHistory", []),
            "calibrationInvalidated": durable.get("calibrationInvalidated", ""),
            "runtimeReadinessInvalidation": (
                self.controller.runtime_readiness_invalidation.value
                if self.controller.runtime_readiness_invalidation is not None
                else None
            ),
            "startup": dict(self._startup_operation),
            "takeoverDrill": {
                "state": drill_state,
                "environmentMatches": drill_matches if startup_evidence is not None else None,
                "fingerprint": drill.fingerprint if drill is not None else "",
                "failure": self._takeover_drill_operation.get("failure", ""),
            },
            "nextAction": next_action,
            "nativeResourceAssessment": self.controller.native_resource_assessment(),
            "nativeBootstrapPending": self._native_bootstrap_ready.is_set(),
            "nativeBootstrapReady": self._native_baseline_ready.is_set(),
            "nativeBootstrap": self._native_baseline_observation or self._native_bootstrap_observation,
            # These fields are the operator-visible native8 handshake.  They
            # are facts emitted by callbacks from the live runner, not claims
            # inferred from a calibration boolean.
            "nativeHandshake": dict(self._native_handshake),
            "routeWalk": copy.deepcopy(route_walk),
        }

    def _calibration_operation_snapshot(self) -> dict[str, Any]:
        with self._calibration_state_lock:
            value = dict(self._calibration_operation)
            if isinstance(value.get("readiness"), dict):
                value["readiness"] = dict(value["readiness"])
            return value

    def run_forever(self, *, interval: float = 1.0) -> None:
        while not self._shutdown_event.is_set():
            self.run_once()
            self._shutdown_event.wait(interval)


__all__ = ["SessionDaemon"]
