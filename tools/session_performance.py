"""Bounded, offline measurements for the Crowley session decode boundary.

This command only reads explicitly named PNGs and JSON timing receipts. It
never captures a desktop, sends input, starts a session, or consults a live
service. The production decoder and pure executor planners remain the
reference implementations. Where the production decoder exposes no public
substep boundary, this module reports one combined strict-decode interval.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import platform
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar

from PIL import Image

from crowley.session.decoder import PixelDecoder
from crowley.session.executor import (
    BindingMetadata,
    KeyRole,
    Point,
    PointerTarget,
    Rect,
    plan_camera_drag,
    plan_key_hold,
    plan_pointer_curve,
    plan_single_click,
)
from crowley.session.protocol import ProtocolError
from crowley.session.source_binding import SAFETY_MODULES

MAX_SAMPLES = 1_000
MAX_INPUTS = 16
MAX_RECEIPTS = 16
MAX_PNG_BYTES = 1_048_576
MAX_RECEIPT_BYTES = 1_048_576
MAX_LIVE_RECEIPTS = 64
MAX_LIVE_RECEIPT_BYTES = 4_194_304
MAX_BINDING_BYTES = 65_536
MAX_LIVE_JSON_DEPTH = 32
EXPECTED_DIMENSIONS = (256, 256)
STAGE_NAMES = (
    "png_load",
    "pixel_extract_2x2_validation",
    "cell_unpack",
    "frame_inflate_json_schema_validation",
    "full_strict_decode",
    "pointer_planning",
    "click_planning",
    "key_planning",
    "camera_planning",
)
SOURCE_FILES = (
    "src/crowley/session/decoder.py",
    "src/crowley/session/protocol.py",
    "src/crowley/session/executor.py",
)
LIVE_SOURCE_FILES = SAFETY_MODULES
LIVE_RUN_SCHEMA = "crowley.paused-region-performance-run"
LIVE_BINDING_SCHEMA = "crowley.runtime-performance-accepted-binding"
LIVE_TARGETS = 8
LIVE_SAMPLE_DEADLINE_SECONDS = 1.0
LIVE_MIN_INTERVAL_SECONDS = 0.75
LIVE_MAX_INTERVAL_SECONDS = 1.25
LIVE_MIN_RUNS = 15
LIVE_MIN_PAIRS = 120
LIVE_REPEATABILITY_FRACTION = 0.80
LIVE_APPLICATION_CPU_THRESHOLD_SECONDS = 0.100
LIVE_APPLICATION_RATIO_THRESHOLD = 0.20
LIVE_FAILURE_PHASES = frozenset(
    {
        "status",
        "owner_ipc",
        "owner_capture",
        "png_publication",
        "png_load",
        "strict_decode",
        "deadline",
        "source_binding",
        "config_binding",
        "producer_binding",
        "model_counter",
        "cleanup",
        "process_termination",
    }
)
LIVE_FAILURE_REASONS = frozenset(
    {
        "status_failed",
        "owner_ipc_failed",
        "owner_capture_failed",
        "png_publication_failed",
        "png_load_failed",
        "strict_decode_failed",
        "sample_deadline_missed",
        "source_binding_drift",
        "config_binding_mismatch",
        "producer_binding_mismatch",
        "model_call_delta_nonzero",
        "owner_receipt_nonce_duplicate",
        "model_call_counters_missing",
        "cleanup_failed",
        "process_terminated",
        "unknown_required_stage",
    }
)
_HEX_SHA256 = frozenset("0123456789abcdef")
T = TypeVar("T")


class PerformanceInputError(ValueError):
    """An explicit offline artifact is malformed or outside its bounds."""


@dataclass(frozen=True)
class Timed:
    value: Any
    wall_seconds: float
    cpu_seconds: float


def _reject_constant(value: str) -> Any:
    del value
    raise PerformanceInputError("receipt_invalid_json_constant")


def _strict_json(raw: bytes) -> Any:
    def hook(values: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in values]
        if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
            raise PerformanceInputError("receipt_duplicate_or_non_string_key")
        return dict(values)

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=hook,
            parse_constant=_reject_constant,
        )
    except PerformanceInputError:
        raise
    except RecursionError as exc:
        raise PerformanceInputError("receipt_json_nesting_exceeded") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerformanceInputError("receipt_invalid_utf8_or_json") from exc


def _timed[T](function: Callable[[], T]) -> Timed:
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    value = function()
    wall = (time.perf_counter_ns() - wall_start) / 1_000_000_000
    cpu = (time.process_time_ns() - cpu_start) / 1_000_000_000
    return Timed(value, wall, cpu)


def _read_bounded(path: Path) -> tuple[bytes, int]:
    """Read an explicitly named file without trusting a racing stat result."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PerformanceInputError(f"unable to stat explicit input {path.name!r}") from exc
    if size <= 0 or size > MAX_PNG_BYTES:
        raise PerformanceInputError(f"PNG input size must be in 1..{MAX_PNG_BYTES} bytes")
    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size != size:
                raise PerformanceInputError("input file changed during bounded read")
            raw = handle.read(MAX_PNG_BYTES + 1)
    except PerformanceInputError:
        raise
    except OSError as exc:
        raise PerformanceInputError("unable to read explicit input") from exc
    if len(raw) > MAX_PNG_BYTES or len(raw) != size:
        raise PerformanceInputError("input file changed during bounded read")
    try:
        if path.stat().st_size != size:
            raise PerformanceInputError("input file changed during bounded read")
    except OSError as exc:
        raise PerformanceInputError("unable to stat explicit input") from exc
    return raw, size


def _load_png(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> Image.Image:
    try:
        raw, size = _read_bounded(path)
        if expected_size is not None and size != expected_size:
            raise PerformanceInputError("input bytes changed since preflight")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise PerformanceInputError("input bytes changed since preflight")
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG":
                raise PerformanceInputError("PNG container or format is invalid")
            if image.size != EXPECTED_DIMENSIONS:
                raise PerformanceInputError(f"PNG dimensions are not {EXPECTED_DIMENSIONS!r}")
            image.load()
            return image.copy()
    except PerformanceInputError:
        raise
    except (OSError, ValueError) as exc:
        raise PerformanceInputError("PNG container or decode is invalid") from exc


def _source_hashes(repo_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in SOURCE_FILES:
        path = repo_root / relative
        try:
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise PerformanceInputError("required source boundary is unavailable") from exc
    return result


def _planner_inputs() -> tuple[Point, PointerTarget, Rect, BindingMetadata]:
    binding = BindingMetadata(
        identity_fingerprint="offline-performance-client",
        capture_id="offline-performance-capture",
        capture_sequence=1,
        generation=1,
        target_xid=1,
        target_ancestry=(1,),
        source="offline-fixture",
        boot="offline-boot",
    )
    client = Rect(0, 0, 800, 600)
    target = PointerTarget("offline-control", Rect(120, 100, 80, 40), binding)
    return Point(20, 20), target, client, binding


def _planner_timings() -> dict[str, Timed]:
    start, target, client, binding = _planner_inputs()
    return {
        "pointer_planning": _timed(lambda: plan_pointer_curve(start, target, client)),
        "click_planning": _timed(lambda: plan_single_click(target, client)),
        "key_planning": _timed(lambda: plan_key_hold(KeyRole.MOVE_FORWARD, 0.1, binding)),
        "camera_planning": _timed(
            lambda: plan_camera_drag(Point(200, 200), Point(260, 200), client, binding)
        ),
    }


def _stage_summary(wall: list[float], cpu: list[float]) -> dict[str, Any]:
    if len(wall) != len(cpu):
        raise PerformanceInputError("stage wall and CPU samples have different lengths")
    if not wall:
        return {
            "samples": 0,
            "wall_p50_ms": None,
            "wall_p95_ms": None,
            "wall_p99_ms": None,
            "wall_max_ms": None,
            "cpu_p50_ms": None,
            "cpu_p95_ms": None,
            "cpu_p99_ms": None,
            "cpu_max_ms": None,
            "wall_samples_seconds": [],
            "cpu_samples_seconds": [],
        }

    def quantile(values: Sequence[float], quantile_value: float) -> float:
        index = math.ceil(quantile_value * len(values)) - 1
        return sorted(values)[index]

    return {
        "samples": len(wall),
        "wall_p50_ms": quantile(wall, 0.50) * 1000,
        "wall_p95_ms": quantile(wall, 0.95) * 1000,
        "wall_p99_ms": quantile(wall, 0.99) * 1000,
        "wall_max_ms": max(wall) * 1000,
        "cpu_p50_ms": quantile(cpu, 0.50) * 1000,
        "cpu_p95_ms": quantile(cpu, 0.95) * 1000,
        "cpu_p99_ms": quantile(cpu, 0.99) * 1000,
        "cpu_max_ms": max(cpu) * 1000,
        "wall_samples_seconds": list(wall),
        "cpu_samples_seconds": list(cpu),
    }


def _unmeasured_stage(reason: str) -> dict[str, Any]:
    """Describe a production substep that has no public timing boundary."""

    return {
        "samples": None,
        "wall_p50_ms": None,
        "wall_p95_ms": None,
        "wall_p99_ms": None,
        "wall_max_ms": None,
        "cpu_p50_ms": None,
        "cpu_p95_ms": None,
        "cpu_p99_ms": None,
        "cpu_max_ms": None,
        "wall_samples_seconds": None,
        "cpu_samples_seconds": None,
        "unmeasured_reason": reason,
    }


def _environment_metadata() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "pillow": str(getattr(Image, "__version__", "unknown")),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "wall_clock": "time.perf_counter_ns (monotonic)",
        "cpu_clock": "time.process_time_ns (process CPU)",
    }


def _measure_input(path: Path, *, samples: int, warmups: int) -> dict[str, Any]:
    raw, size = _read_bounded(path)
    digest = hashlib.sha256(raw).hexdigest()
    # Validate the explicit container and dimensions before any timed sample.
    # Pixel/CRC/protocol refusal remains a measured strict-decode failure.
    preflight = _load_png(path, expected_sha256=digest, expected_size=size)
    preflight.close()
    stage_values: dict[str, tuple[list[float], list[float]]] = {name: ([], []) for name in STAGE_NAMES}
    failures: list[dict[str, Any]] = []

    def run_once(index: int, *, retain: bool) -> None:
        image: Image.Image | None = None
        try:
            loaded = _timed(lambda: _load_png(path, expected_sha256=digest, expected_size=size))
            image = loaded.value
            if retain:
                stage_values["png_load"][0].append(loaded.wall_seconds)
                stage_values["png_load"][1].append(loaded.cpu_seconds)

            full = _timed(
                lambda: PixelDecoder().decode(
                    image,
                    monotonic=time.monotonic(),
                    window_identity="offline-performance-client",
                )
            )
            if full.value.kind != "FRESH" or full.value.frame is None:
                raise ProtocolError("fresh decoder did not return a FRESH frame")
            if retain:
                stage_values["full_strict_decode"][0].append(full.wall_seconds)
                stage_values["full_strict_decode"][1].append(full.cpu_seconds)
        except PerformanceInputError:
            raise
        except (OSError, ProtocolError, TypeError, ValueError) as exc:
            if retain:
                failures.append({"sample": index, "phase": "decode", "error_type": type(exc).__name__})
        finally:
            if image is not None:
                image.close()

        try:
            planners = _planner_timings()
            if retain:
                for name, timing in planners.items():
                    stage_values[name][0].append(timing.wall_seconds)
                    stage_values[name][1].append(timing.cpu_seconds)
        except (TypeError, ValueError) as exc:
            if retain:
                failures.append({"sample": index, "phase": "planning", "error_type": type(exc).__name__})

    for index in range(warmups):
        run_once(index, retain=False)
    for index in range(samples):
        run_once(index, retain=True)

    stages = {name: _stage_summary(wall, cpu) for name, (wall, cpu) in stage_values.items()}
    for name in (
        "pixel_extract_2x2_validation",
        "cell_unpack",
        "frame_inflate_json_schema_validation",
    ):
        stages[name] = _unmeasured_stage(
            "production decoder exposes no public boundary; included in full_strict_decode"
        )

    return {
        "input_sha256": digest,
        "input_size_bytes": size,
        "input_dimensions": list(EXPECTED_DIMENSIONS),
        "warmups": warmups,
        "samples_requested": samples,
        "failures": failures,
        "stages": stages,
    }


_RECEIPT_KNOWN_FIELDS = frozenset(
    {
        "artifacts",
        "binary",
        "boundaries",
        "candidate_status",
        "canonical",
        "claims",
        "container",
        "created_utc",
        "generated_utc",
        "harness",
        "integration_status",
        "predecessors",
        "private_build",
        "production",
        "review_required_before_live",
        "schema",
        "schema_version",
        "receipt_kind",
        "stage2_attestation",
        "scope",
        "status",
        "started_utc",
        "finished_utc",
        "source_hashes",
        "source",
        "build",
        "build_config",
        "receipt",
        "samples",
        "stages",
        "run_return",
        "failure",
        "failures",
        "capture",
        "io",
        "application_compute",
        "end_to_end_wall",
        "verification",
    }
)
_NESTED_RECEIPT_KNOWN_FIELDS = frozenset({"kind", "schema"})


def _receipt_diagnostic(path: Path) -> dict[str, Any]:
    """Read only bounded receipt metadata; never treat it as accepted evidence."""

    try:
        initial = path.stat()
    except OSError:
        return {"status": "refused", "reason_code": "receipt_unreadable"}
    if initial.st_size <= 0 or initial.st_size > MAX_RECEIPT_BYTES:
        return {"status": "refused", "reason_code": "receipt_size_out_of_bounds"}
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                initial.st_dev,
                initial.st_ino,
                initial.st_size,
            ):
                return {"status": "refused", "reason_code": "receipt_changed_during_read"}
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
            finished = os.fstat(handle.fileno())
    except OSError:
        return {"status": "refused", "reason_code": "receipt_unreadable"}
    if (finished.st_dev, finished.st_ino, finished.st_size) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
    ):
        return {"status": "refused", "reason_code": "receipt_changed_during_read"}
    if len(raw) == 0 or len(raw) > MAX_RECEIPT_BYTES or len(raw) != opened.st_size:
        return {"status": "refused", "reason_code": "receipt_changed_during_read"}
    digest = hashlib.sha256(raw).hexdigest()
    try:
        value = _strict_json(raw)
    except PerformanceInputError as exc:
        return {
            "status": "refused",
            "reason_code": str(exc),
            "receipt_sha256": digest,
            "size_bytes": len(raw),
        }
    if not isinstance(value, dict):
        return {
            "status": "refused",
            "reason_code": "receipt_root_not_object",
            "receipt_sha256": digest,
            "size_bytes": len(raw),
        }
    keys = [key for key in value if isinstance(key, str)]
    nested = value.get("receipt")
    nested_keys = [key for key in nested if isinstance(key, str)] if isinstance(nested, dict) else []
    return {
        "status": "diagnostic_only",
        "reason_code": "receipt_contract_unavailable",
        "receipt_sha256": digest,
        "size_bytes": len(raw),
        "root_type": "object",
        "known_fields": sorted(set(keys) & _RECEIPT_KNOWN_FIELDS),
        "unknown_field_count": len(set(keys) - _RECEIPT_KNOWN_FIELDS),
        "nested_receipt_known_fields": sorted(set(nested_keys) & _NESTED_RECEIPT_KNOWN_FIELDS),
        "nested_receipt_unknown_field_count": len(set(nested_keys) - _NESTED_RECEIPT_KNOWN_FIELDS),
        "accepted_flag_present": "accepted" in value,
        "source_hashes_present": isinstance(value.get("source_hashes"), dict)
        or isinstance(value.get("accepted_source_hashes"), dict),
        "producer_binding_present": value.get("producer_binding") is not None,
        "config_binding_present": value.get("config_binding") is not None,
        "run_records_present": isinstance(value.get("runs"), list),
    }


def _decision(
    measurements: list[dict[str, Any]],
    receipt_paths: Sequence[Path],
    *,
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    diagnostics = [_receipt_diagnostic(path) for path in receipt_paths]
    reasons = [
        "no_accepted_matching_region_instrumentation",
        "paired_cpu_and_end_to_end_intervals_unavailable",
        "insufficient_eight_sample_runs",
    ]
    if not receipt_paths:
        reasons.append("no_preserved_timing_receipts")
    else:
        reasons.append("receipt_contract_unavailable")
        if any(item.get("status") == "refused" for item in diagnostics):
            reasons.append("receipt_refused")
    limitations = [
        "Offline measurements never capture the desktop, send input, invoke Luna, or claim live freshness.",
        "PNG load is timed from an explicit path; the offline run does not measure native capture or IPC.",
        "Full strict decode includes all private pixel, cell, frame, JSON, and schema work.",
        (
            "The supplied receipt format is retained for bounded diagnostics only; it is not an "
            "accepted live instrumentation contract."
        ),
        (
            "Local decoder source hashes identify measured code, not an accepted region producer "
            "or runtime binding."
        ),
    ]
    receipt_counts = {
        "supplied": len(receipt_paths),
        "read": sum(item.get("receipt_sha256") is not None for item in diagnostics),
        "refused": sum(item.get("status") == "refused" for item in diagnostics),
    }
    return {
        "schema": "crowley.runtime-performance-decision",
        "schema_version": 1,
        "accepted_source_hashes": {},
        "producer_binding": None,
        "config_binding": None,
        "runs": [],
        "statistics": {
            "offline_measurements": measurements,
            "environment": _environment_metadata(),
            "cold_start_excluded": True,
            "measured_source_hashes": source_hashes,
            "receipt_counts": receipt_counts,
            "receipt_diagnostics": diagnostics,
            "quantile_definition": "sorted_samples[ceil(N*q)-1]",
            "eligibility": "insufficient_evidence",
            "eligible_run_count": 0,
            "pooled_completed_count": 0,
            "pooled_application_compute_cpu_p95_seconds": None,
            "pooled_end_to_end_wall_p95_seconds": None,
            "ratio_of_quantiles": None,
            "run_trigger_repeatability": {
                "trigger_runs": None,
                "eligible_runs": 0,
                "minimum_fraction": 0.80,
                "passed": None,
                "runs": [],
            },
            "deadline_failure_candidates": [],
        },
        "decision": "insufficient_evidence",
        "reason_codes": sorted(set(reasons)),
        "limitations": limitations,
    }


class _LiveRefusal(ValueError):
    """A safe, fixed live-input refusal code (never carries source data)."""


_LIVE_REASON_CODES = frozenset(
    {
        "binding_unreadable",
        "binding_size_out_of_bounds",
        "binding_changed_during_read",
        "binding_digest_mismatch",
        "binding_contract_invalid",
        "receipt_unreadable",
        "receipt_size_out_of_bounds",
        "receipt_changed_during_read",
        "receipt_invalid_json",
        "receipt_duplicate_or_non_string_key",
        "receipt_json_nesting_exceeded",
        "receipt_root_not_object",
        "receipt_schema_invalid",
        "unknown_required_stage",
        "missing_required_stage",
        "binding_mismatch",
        "source_binding_drift",
        "config_binding_mismatch",
        "producer_binding_mismatch",
        "run_timing_invalid",
        "attempt_timing_invalid",
        "attempt_lifecycle_invalid",
        "model_call_counters_invalid",
        "model_call_counters_missing",
        "model_call_delta_nonzero",
        "cleanup_invalid",
        "duplicate_receipt_digest",
        "duplicate_run_id",
        "insufficient_complete_runs",
        "insufficient_completed_pairs",
        "migration_trigger_not_repeatable",
        "no_migration_trigger",
    }
)
_LIVE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "run_id",
        "run_state",
        "generation",
        "targets_monotonic",
        "observer_source_hashes_before",
        "observer_source_hashes_after",
        "producer_binding",
        "config_binding",
        "attempts",
    }
)
_LIVE_ATTEMPT_KEYS = frozenset(
    {
        "index",
        "target_monotonic",
        "deadline_monotonic",
        "outer_started_monotonic",
        "state",
        "status_returned_monotonic",
        "owner_ipc_request_started_monotonic",
        "owner",
        "owner_ipc_returned_monotonic",
        "png_publication_end_monotonic",
        "png_load_started_monotonic",
        "png_load_finished_monotonic",
        "strict_decode_started_monotonic",
        "strict_decode_finished_monotonic",
        "sample_completed_monotonic",
        "application_compute_cpu_seconds",
        "application_compute_cpu_method",
        "partial_decode_wall_seconds",
        "partial_decode_cpu_seconds",
        "png_sha256",
        "generation",
        "identity",
        "failure",
    }
)
_LIVE_OWNER_KEYS = frozenset(
    {
        "owner_entered_monotonic",
        "request_started_monotonic",
        "lock_acquired_monotonic",
        "ack_sent_monotonic",
        "capture_started_monotonic",
        "capture_returned_monotonic",
        "response_ready_monotonic",
        "receipt_nonce",
        "generation",
        "source_binding",
        "producer_binding",
        "config_binding",
        "model_calls_before",
        "model_calls_after",
        "model_call_delta",
        "cleanup",
        "failure",
    }
)
_LIVE_FAILURE_KEYS = frozenset({"phase", "reason_code"})
_LIVE_COUNTER_KEYS = frozenset({"started", "succeeded", "failed"})
_LIVE_CLEANUP_KEYS = frozenset(
    {
        "capture_process_reaped",
        "attempt_retired",
        "lease_held",
        "watchdog_healthy",
        "active_process_count",
        "active_process_group_count",
        "observer_process_group_count",
    }
)
_LIVE_OWNER_CLOCKS = (
    "owner_entered_monotonic",
    "request_started_monotonic",
    "lock_acquired_monotonic",
    "ack_sent_monotonic",
    "capture_started_monotonic",
    "capture_returned_monotonic",
    "response_ready_monotonic",
)
_LIVE_CLIENT_CLOCKS = (
    "outer_started_monotonic",
    "status_returned_monotonic",
    "owner_ipc_request_started_monotonic",
    "owner_ipc_returned_monotonic",
    "png_publication_end_monotonic",
    "png_load_started_monotonic",
    "png_load_finished_monotonic",
    "strict_decode_started_monotonic",
    "strict_decode_finished_monotonic",
    "sample_completed_monotonic",
)
_LIVE_DECODE_CAUSAL_CLIENT_CLOCKS = _LIVE_CLIENT_CLOCKS[:-1]
_LIVE_REQUIRED_INTERVALS = (
    "status_wall_seconds",
    "owner_ipc_round_trip_wall_seconds",
    "owner_wall_seconds",
    "pre_capture_owner_wall_seconds",
    "capture_lock_wait_wall_seconds",
    "admitted_setup_protocol_wall_seconds",
    "external_capture_parent_validation_wall_seconds",
    "response_materialization_wall_seconds",
    "png_load_wall_seconds",
    "strict_decode_wall_seconds",
    "end_to_end_wall_seconds",
)


def _safe_sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(character in _HEX_SHA256 for character in value)
    )


def _finite_number(value: Any, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonnegative_int(value: Any, *, nullable: bool = False) -> bool:
    return nullable and value is None or isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _exact_object(value: Any, keys: frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _json_with_depth(raw: bytes) -> Any:
    """Strict JSON parser with an explicit pre-parse nesting cap."""
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _LiveRefusal("receipt_invalid_json") from exc
    depth = 0
    in_string = False
    escaped = False
    for character in source:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_LIVE_JSON_DEPTH:
                raise _LiveRefusal("receipt_json_nesting_exceeded")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise _LiveRefusal("receipt_invalid_json")
    if in_string or depth != 0:
        raise _LiveRefusal("receipt_invalid_json")

    def hook(values: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in values]
        if len(keys) != len(set(keys)):
            raise _LiveRefusal("receipt_duplicate_or_non_string_key")
        return dict(values)

    def reject_constant(_: str) -> Any:
        raise _LiveRefusal("receipt_invalid_json")

    try:
        return json.loads(source, object_pairs_hook=hook, parse_constant=reject_constant)
    except _LiveRefusal:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise _LiveRefusal("receipt_invalid_json") from exc


def _stable_bounded_read(path: Path, limit: int, prefix: str) -> tuple[bytes, str]:
    """Read one regular, single-link file and reject path or descriptor drift."""
    unreadable = f"{prefix}_unreadable"
    too_large = f"{prefix}_size_out_of_bounds"
    changed = f"{prefix}_changed_during_read"
    try:
        before_path = path.lstat()
        if not stat.S_ISREG(before_path.st_mode) or stat.S_ISLNK(before_path.st_mode):
            raise _LiveRefusal(unreadable)
        if before_path.st_nlink != 1 or before_path.st_size <= 0 or before_path.st_size > limit:
            raise _LiveRefusal(
                too_large if before_path.st_size > limit or before_path.st_size <= 0 else unreadable
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
    except _LiveRefusal:
        raise
    except OSError as exc:
        raise _LiveRefusal(unreadable) from exc
    try:
        before_fd = os.fstat(descriptor)
        if not stat.S_ISREG(before_fd.st_mode) or before_fd.st_nlink != 1:
            raise _LiveRefusal(unreadable)
        if _stat_identity(before_fd) != _stat_identity(before_path):
            raise _LiveRefusal(changed)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        if (
            _stat_identity(before_fd) != _stat_identity(after_fd)
            or _stat_identity(after_fd) != _stat_identity(after_path)
            or len(raw) != before_fd.st_size
        ):
            raise _LiveRefusal(changed)
        if len(raw) > limit:
            raise _LiveRefusal(too_large)
        return raw, hashlib.sha256(raw).hexdigest()
    except _LiveRefusal:
        raise
    except OSError as exc:
        raise _LiveRefusal(unreadable) from exc
    finally:
        os.close(descriptor)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _load_binding(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if not _safe_sha256(expected_sha256):
        raise PerformanceInputError("binding_digest_mismatch")
    try:
        raw, digest = _stable_bounded_read(path, MAX_BINDING_BYTES, "binding")
    except _LiveRefusal as exc:
        raise PerformanceInputError(str(exc)) from exc
    if not hmac.compare_digest(digest, expected_sha256):
        raise PerformanceInputError("binding_digest_mismatch")
    try:
        binding = _json_with_depth(raw)
    except _LiveRefusal as exc:
        raise PerformanceInputError(str(exc)) from exc
    if not _exact_object(
        binding,
        frozenset(
            {"schema", "schema_version", "accepted_source_hashes", "producer_binding", "config_binding"}
        ),
    ):
        raise PerformanceInputError("binding_contract_invalid")
    if (
        binding["schema"] != LIVE_BINDING_SCHEMA
        or not _nonnegative_int(binding["schema_version"])
        or binding["schema_version"] != 1
    ):
        raise PerformanceInputError("binding_contract_invalid")
    source_hashes = binding["accepted_source_hashes"]
    if (
        not isinstance(source_hashes, dict)
        or set(source_hashes) != set(LIVE_SOURCE_FILES)
        or not all(_safe_sha256(value) for value in source_hashes.values())
    ):
        raise PerformanceInputError("binding_contract_invalid")
    if not _valid_producer(binding["producer_binding"]) or not _valid_config(binding["config_binding"]):
        raise PerformanceInputError("binding_contract_invalid")
    return binding, digest


def _valid_producer(value: Any) -> bool:
    return (
        _exact_object(value, frozenset({"binary_sha256", "protocol_branch"}))
        and _safe_sha256(value["binary_sha256"])
        and value["protocol_branch"] == "ScreenshotArea"
    )


def _valid_config(value: Any) -> bool:
    return (
        _exact_object(value, frozenset({"raw_file_sha256", "semantic_sha256"}))
        and _safe_sha256(value["raw_file_sha256"])
        and _safe_sha256(value["semantic_sha256"])
    )


def _source_binding(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(LIVE_SOURCE_FILES)
        and all(_safe_sha256(item) for item in value.values())
    )


def _validate_counter_triplet(value: Any) -> bool:
    if not _exact_object(value, _LIVE_COUNTER_KEYS) or not all(
        _nonnegative_int(item, nullable=True) for item in value.values()
    ):
        return False
    started = value["started"]
    completed = value["succeeded"]
    failed = value["failed"]
    return started is None or completed is None or failed is None or completed + failed <= started


def _validate_failure(value: Any, *, required: bool) -> bool:
    if value is None:
        return not required
    return (
        _exact_object(value, _LIVE_FAILURE_KEYS)
        and isinstance(value["phase"], str)
        and isinstance(value["reason_code"], str)
        and value["phase"] in LIVE_FAILURE_PHASES
        and value["reason_code"] in LIVE_FAILURE_REASONS
    )


def _derive_interval(end: Any, start: Any) -> float | None:
    if not _finite_number(end, nullable=True) or not _finite_number(start, nullable=True):
        return None
    if end is None or start is None or end < start:
        return None
    return float(end - start)


def _validate_attempt(
    value: Any,
    index: int,
    target: float,
    deadline: float,
    generation: int,
    binding: dict[str, Any],
    previous_identity: tuple[str, str, int] | None,
    seen_nonces: set[str],
) -> tuple[dict[str, Any], bool, bool, tuple[str, str, int] | None, str | None]:
    """Return a privacy-safe attempt, pair status, and internal identity."""
    if not _exact_object(value, _LIVE_ATTEMPT_KEYS):
        unknown = isinstance(value, dict) and bool(set(value) - _LIVE_ATTEMPT_KEYS)
        raise _LiveRefusal("unknown_required_stage" if unknown else "missing_required_stage")
    if (
        value["index"] != index
        or not _nonnegative_int(value["index"])
        or not _finite_number(value["target_monotonic"])
        or value["target_monotonic"] != target
        or not _finite_number(value["deadline_monotonic"])
        or value["deadline_monotonic"] != deadline
        or value["deadline_monotonic"] != target + LIVE_SAMPLE_DEADLINE_SECONDS
        or not isinstance(value["state"], str)
        or value["state"] not in {"completed", "failed", "incomplete"}
    ):
        raise _LiveRefusal("attempt_timing_invalid")
    for key in _LIVE_CLIENT_CLOCKS:
        if not _finite_number(value[key], nullable=True) or (value[key] is not None and value[key] < 0):
            raise _LiveRefusal("attempt_timing_invalid")
    if not _nonnegative_int(value["generation"], nullable=True):
        raise _LiveRefusal("attempt_timing_invalid")
    if value["generation"] is not None and value["generation"] != generation:
        raise _LiveRefusal("attempt_timing_invalid")
    if value["png_sha256"] is not None and not _safe_sha256(value["png_sha256"]):
        raise _LiveRefusal("attempt_timing_invalid")
    for key in (
        "application_compute_cpu_seconds",
        "partial_decode_wall_seconds",
        "partial_decode_cpu_seconds",
    ):
        if not _finite_number(value[key], nullable=True) or (value[key] is not None and value[key] < 0):
            raise _LiveRefusal("attempt_timing_invalid")
    if (
        value["application_compute_cpu_method"] is not None
        and value["application_compute_cpu_method"] != "time.process_time_ns"
    ):
        raise _LiveRefusal("attempt_timing_invalid")
    if (
        value["application_compute_cpu_seconds"] is None
        and value["application_compute_cpu_method"] is not None
    ) or (
        value["application_compute_cpu_seconds"] is not None
        and (value["application_compute_cpu_method"] != "time.process_time_ns")
    ):
        raise _LiveRefusal("attempt_timing_invalid")
    if value["application_compute_cpu_seconds"] is not None and (
        value["strict_decode_started_monotonic"] is None or value["strict_decode_finished_monotonic"] is None
    ):
        raise _LiveRefusal("missing_required_stage")
    compute_available = (
        value["application_compute_cpu_seconds"] is not None
        and value["application_compute_cpu_method"] == "time.process_time_ns"
        and value["strict_decode_started_monotonic"] is not None
        and value["strict_decode_finished_monotonic"] is not None
    )
    state = value["state"]
    if not _validate_failure(value["failure"], required=state == "failed"):
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if (
        value["failure"] is not None
        and value["failure"]["reason_code"] == "strict_decode_failed"
        and value["application_compute_cpu_seconds"] is not None
    ):
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if state == "completed" and value["failure"] is not None:
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if any(
        value[key] is not None for key in ("partial_decode_wall_seconds", "partial_decode_cpu_seconds")
    ) and (
        state != "failed"
        or value["failure"]["reason_code"] != "strict_decode_failed"
        or value["application_compute_cpu_seconds"] is not None
    ):
        raise _LiveRefusal("attempt_lifecycle_invalid")

    owner = value["owner"]
    owner_clocks: dict[str, float | None] = dict.fromkeys(_LIVE_OWNER_CLOCKS)
    nonce: str | None = None
    nonce_unique = True
    model_delta: dict[str, int | None] | None = None
    cleanup_ok = False
    owner_binding_ok = False
    owner_failure: Any = None
    if owner is not None:
        if not _exact_object(owner, _LIVE_OWNER_KEYS):
            unknown = isinstance(owner, dict) and bool(set(owner) - _LIVE_OWNER_KEYS)
            raise _LiveRefusal("unknown_required_stage" if unknown else "missing_required_stage")
        for key in _LIVE_OWNER_CLOCKS:
            clock = owner[key]
            if not _finite_number(clock, nullable=True) or (clock is not None and clock < 0):
                raise _LiveRefusal("attempt_timing_invalid")
            owner_clocks[key] = float(clock) if clock is not None else None
        if not _nonnegative_int(owner["generation"], nullable=True):
            raise _LiveRefusal("attempt_timing_invalid")
        if owner["generation"] is not None and owner["generation"] != generation:
            raise _LiveRefusal("attempt_lifecycle_invalid")
        if owner["receipt_nonce"] is not None:
            nonce = owner["receipt_nonce"]
            if not isinstance(nonce, str) or not nonce:
                raise _LiveRefusal("attempt_lifecycle_invalid")
            nonce_unique = nonce not in seen_nonces
            seen_nonces.add(nonce)
        if not _validate_counter_triplet(owner["model_calls_before"]):
            raise _LiveRefusal("model_call_counters_invalid")
        if not _validate_counter_triplet(owner["model_calls_after"]):
            raise _LiveRefusal("model_call_counters_invalid")
        if not _validate_counter_triplet(owner["model_call_delta"]):
            raise _LiveRefusal("model_call_counters_invalid")
        model_delta = {}
        for key in _LIVE_COUNTER_KEYS:
            before = owner["model_calls_before"][key]
            after = owner["model_calls_after"][key]
            claimed = owner["model_call_delta"][key]
            expected = after - before if before is not None and after is not None else None
            if expected is not None and expected < 0 or claimed != expected:
                raise _LiveRefusal("model_call_counters_invalid")
            model_delta[key] = expected
        cleanup = owner["cleanup"]
        if not _exact_object(cleanup, _LIVE_CLEANUP_KEYS):
            raise _LiveRefusal("cleanup_invalid")
        bool_fields = (
            "capture_process_reaped",
            "attempt_retired",
            "lease_held",
            "watchdog_healthy",
        )
        if not all(isinstance(cleanup[key], bool) for key in bool_fields) or not all(
            _nonnegative_int(cleanup[key])
            for key in ("active_process_count", "active_process_group_count", "observer_process_group_count")
        ):
            raise _LiveRefusal("cleanup_invalid")
        cleanup_ok = (
            cleanup["capture_process_reaped"]
            and cleanup["attempt_retired"]
            and cleanup["lease_held"]
            and cleanup["watchdog_healthy"]
            and cleanup["active_process_count"] == 0
            and cleanup["active_process_group_count"] == 0
            and cleanup["observer_process_group_count"] == 0
        )
        if not _validate_failure(owner["failure"], required=False):
            raise _LiveRefusal("attempt_lifecycle_invalid")
        if state == "completed" and owner["failure"] is not None:
            raise _LiveRefusal("attempt_lifecycle_invalid")
        owner_failure = owner["failure"]
        owner_binding_ok = (
            _source_binding(owner["source_binding"])
            and owner["source_binding"] == binding["accepted_source_hashes"]
            and _valid_producer(owner["producer_binding"])
            and owner["producer_binding"] == binding["producer_binding"]
            and _valid_config(owner["config_binding"])
            and owner["config_binding"] == binding["config_binding"]
        )
    elif state == "completed":
        raise _LiveRefusal("missing_required_stage")

    client_clocks = {key: value[key] for key in _LIVE_CLIENT_CLOCKS}
    identity = value["identity"]
    identity_valid = (
        _exact_object(identity, frozenset({"fingerprint", "boot", "sequence"}))
        and isinstance(identity["fingerprint"], str)
        and bool(identity["fingerprint"])
        and isinstance(identity["boot"], str)
        and bool(identity["boot"])
        and _nonnegative_int(identity["sequence"])
    )
    if identity is not None and not identity_valid:
        raise _LiveRefusal("attempt_lifecycle_invalid")
    strict_decode_failed = (
        value["failure"] is not None and value["failure"]["reason_code"] == "strict_decode_failed"
    )
    decode_completed_claim = state == "completed" or (
        client_clocks["strict_decode_finished_monotonic"] is not None and not strict_decode_failed
    )
    if decode_completed_claim and (not compute_available or value["png_sha256"] is None):
        raise _LiveRefusal("missing_required_stage")
    decode_succeeded = decode_completed_claim and compute_available
    if decode_succeeded:
        if any(client_clocks[key] is None for key in _LIVE_DECODE_CAUSAL_CLIENT_CLOCKS):
            raise _LiveRefusal("missing_required_stage")
        if owner is None or any(owner_clocks[key] is None for key in _LIVE_OWNER_CLOCKS):
            raise _LiveRefusal("missing_required_stage")
        if owner["generation"] is None:
            raise _LiveRefusal("missing_required_stage")
        if (
            not identity_valid
            or value["generation"] != generation
            or owner is None
            or owner["receipt_nonce"] is None
            or owner["generation"] != generation
        ):
            raise _LiveRefusal("attempt_lifecycle_invalid")
        current_identity = (identity["fingerprint"], identity["boot"], identity["sequence"])
        if previous_identity is not None and (
            current_identity[:2] != previous_identity[:2] or current_identity[2] <= previous_identity[2]
        ):
            raise _LiveRefusal("attempt_lifecycle_invalid")
    else:
        current_identity = previous_identity

    sequence = [client_clocks["outer_started_monotonic"]]
    if client_clocks["outer_started_monotonic"] is None or client_clocks["outer_started_monotonic"] < target:
        raise _LiveRefusal("attempt_timing_invalid")
    sequence.extend(
        [
            client_clocks["status_returned_monotonic"],
            client_clocks["owner_ipc_request_started_monotonic"],
        ]
    )
    sequence.extend(owner_clocks[key] for key in _LIVE_OWNER_CLOCKS)
    sequence.extend(
        [
            client_clocks["owner_ipc_returned_monotonic"],
            client_clocks["png_publication_end_monotonic"],
            client_clocks["png_load_started_monotonic"],
            client_clocks["png_load_finished_monotonic"],
            client_clocks["strict_decode_started_monotonic"],
            client_clocks["strict_decode_finished_monotonic"],
            client_clocks["sample_completed_monotonic"],
        ]
    )
    reached = [item for item in sequence if item is not None]
    if any(right < left for left, right in zip(reached, reached[1:], strict=False)):
        raise _LiveRefusal("attempt_timing_invalid")
    if state == "completed" and any(item is None for item in sequence):
        raise _LiveRefusal("missing_required_stage")

    intervals = {
        "status_wall_seconds": _derive_interval(
            client_clocks["status_returned_monotonic"], client_clocks["outer_started_monotonic"]
        ),
        "owner_ipc_round_trip_wall_seconds": _derive_interval(
            client_clocks["owner_ipc_returned_monotonic"],
            client_clocks["owner_ipc_request_started_monotonic"],
        ),
        "owner_wall_seconds": _derive_interval(
            owner_clocks["response_ready_monotonic"], owner_clocks["owner_entered_monotonic"]
        ),
        "pre_capture_owner_wall_seconds": _derive_interval(
            owner_clocks["request_started_monotonic"], owner_clocks["owner_entered_monotonic"]
        ),
        "capture_lock_wait_wall_seconds": _derive_interval(
            owner_clocks["lock_acquired_monotonic"], owner_clocks["request_started_monotonic"]
        ),
        "admitted_setup_protocol_wall_seconds": _derive_interval(
            owner_clocks["ack_sent_monotonic"], owner_clocks["lock_acquired_monotonic"]
        ),
        "external_capture_parent_validation_wall_seconds": _derive_interval(
            owner_clocks["capture_returned_monotonic"], owner_clocks["capture_started_monotonic"]
        ),
        "response_materialization_wall_seconds": _derive_interval(
            client_clocks["png_publication_end_monotonic"], owner_clocks["response_ready_monotonic"]
        ),
        "png_load_wall_seconds": _derive_interval(
            client_clocks["png_load_finished_monotonic"], client_clocks["png_load_started_monotonic"]
        ),
        "strict_decode_wall_seconds": _derive_interval(
            client_clocks["strict_decode_finished_monotonic"],
            client_clocks["strict_decode_started_monotonic"],
        ),
        "end_to_end_wall_seconds": _derive_interval(
            client_clocks["strict_decode_finished_monotonic"], client_clocks["outer_started_monotonic"]
        ),
    }
    private = {
        stage: {"wall_seconds": None, "cpu_seconds": None, "reason": "no_public_boundary"}
        for stage in (
            "pixel_extract_2x2_validation",
            "cell_unpack",
            "frame_inflate_json_schema_validation",
        )
    }
    paired = (
        decode_succeeded
        and owner_binding_ok
        and intervals["end_to_end_wall_seconds"] is not None
        and intervals["strict_decode_wall_seconds"] is not None
        and model_delta is not None
        and all(delta == 0 for delta in model_delta.values())
        and nonce_unique
        and cleanup_ok
        and owner_failure is None
    )
    pair_reasons: list[str] = []
    timing_reasons: list[str] = []
    if decode_succeeded and not paired:
        if not owner_binding_ok:
            pair_reasons.append("binding_mismatch")
        if not nonce_unique:
            pair_reasons.append("owner_receipt_nonce_duplicate")
        if model_delta is None or any(delta is None for delta in model_delta.values()):
            pair_reasons.append("model_call_counters_missing")
        elif any(delta != 0 for delta in model_delta.values()):
            pair_reasons.append("model_call_delta_nonzero")
        if not cleanup_ok:
            pair_reasons.append("cleanup_invalid")
        if owner_failure is not None:
            pair_reasons.append(owner_failure["reason_code"])
        if not pair_reasons:
            pair_reasons.append("missing_required_stage")
    if (
        decode_succeeded
        and intervals["end_to_end_wall_seconds"] is not None
        and (
            intervals["end_to_end_wall_seconds"] > LIVE_SAMPLE_DEADLINE_SECONDS
            or (
                value["sample_completed_monotonic"] is not None
                and value["sample_completed_monotonic"] > deadline
            )
        )
    ):
        timing_reasons.append("sample_deadline_missed")
    timing_qualified = state == "completed" and paired and not timing_reasons
    safe_attempt = {
        "index": index,
        "target_monotonic": target,
        "deadline_monotonic": deadline,
        "generation": generation,
        "state": state,
        "boundaries": {
            **client_clocks,
            "owner": dict(owner_clocks) if owner is not None else None,
        },
        "png_sha256": value["png_sha256"],
        "failure": value["failure"],
        "owner_failure": owner_failure,
        "intervals": intervals,
        "application_compute_cpu_seconds": (
            float(value["application_compute_cpu_seconds"]) if compute_available else None
        ),
        "application_compute_cpu_available": compute_available,
        "application_compute_cpu_method": ("time.process_time_ns" if compute_available else None),
        "external_process_cpu_seconds": None,
        "external_process_cpu_reason": "external_process_cpu_unavailable",
        "private_decoder_substages": private,
        "model_call_delta": model_delta,
        "model_call_counts": (
            {
                "before": owner["model_calls_before"],
                "after": owner["model_calls_after"],
                "delta": model_delta,
            }
            if owner is not None
            else None
        ),
        "cleanup": owner["cleanup"] if owner is not None else None,
        "cleanup_ok": cleanup_ok,
        "binding_ok": owner_binding_ok,
        "receipt_nonce_unique": nonce_unique,
        "owner_reached": owner is not None and owner_clocks["owner_entered_monotonic"] is not None,
        "paired_completed": paired,
        "inter_sample_model_call_delta": None,
        "timing_qualified": timing_qualified,
        "pair_provenance_reason_codes": pair_reasons,
        "timing_qualification_reason_codes": timing_reasons,
        "qualification_reason_codes": sorted(set(pair_reasons + timing_reasons)),
        "interval_availability": {key: interval is not None for key, interval in intervals.items()},
        "partial_decode_wall_seconds": value["partial_decode_wall_seconds"],
        "partial_decode_cpu_seconds": value["partial_decode_cpu_seconds"],
    }
    reason = None
    if state == "completed" and not timing_qualified:
        reason = "sample_deadline_missed"
    elif state == "failed" and value["failure"] is not None:
        reason = value["failure"]["reason_code"]
    return safe_attempt, paired, cleanup_ok, current_identity, reason


def _validate_live_receipt(
    value: Any,
    digest: str,
    binding: dict[str, Any],
    *,
    seen_digests: set[str],
    seen_run_ids: set[str],
    seen_nonces: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    if not isinstance(value, dict):
        raise _LiveRefusal("receipt_root_not_object")
    if not _exact_object(value, _LIVE_RECEIPT_KEYS):
        unknown = bool(set(value) - _LIVE_RECEIPT_KEYS)
        raise _LiveRefusal("unknown_required_stage" if unknown else "receipt_schema_invalid")
    if (
        value["schema"] != LIVE_RUN_SCHEMA
        or not _nonnegative_int(value["schema_version"])
        or value["schema_version"] != 2
    ):
        raise _LiveRefusal("receipt_schema_invalid")
    run_id = value["run_id"]
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
        raise _LiveRefusal("receipt_schema_invalid")
    duplicate = None
    if digest in seen_digests:
        duplicate = "duplicate_receipt_digest"
    if run_id in seen_run_ids:
        duplicate = "duplicate_run_id"
    seen_digests.add(digest)
    seen_run_ids.add(run_id)
    state = value["run_state"]
    if not isinstance(state, str) or state not in {"passed", "failed", "incomplete"}:
        raise _LiveRefusal("receipt_schema_invalid")
    generation = value["generation"]
    if not _nonnegative_int(generation):
        raise _LiveRefusal("receipt_schema_invalid")
    targets = value["targets_monotonic"]
    if (
        not isinstance(targets, list)
        or len(targets) != LIVE_TARGETS
        or not all(_finite_number(item) and item >= 0 for item in targets)
    ):
        raise _LiveRefusal("receipt_schema_invalid")
    target_values = [float(item) for item in targets]
    if any(
        not math.isclose(b - a, 1.0, rel_tol=0.0, abs_tol=1e-9)
        for a, b in zip(target_values, target_values[1:], strict=False)
    ):
        raise _LiveRefusal("run_timing_invalid")
    source_hashes_before = value["observer_source_hashes_before"]
    source_hashes_after = value["observer_source_hashes_after"]
    if not _source_binding(source_hashes_before) or not _source_binding(source_hashes_after):
        raise _LiveRefusal("source_binding_drift")
    source_ok = (
        source_hashes_before == binding["accepted_source_hashes"]
        and source_hashes_after == source_hashes_before
    )
    if not _valid_producer(value["producer_binding"]) or not _valid_config(value["config_binding"]):
        raise _LiveRefusal("receipt_schema_invalid")
    producer_ok = value["producer_binding"] == binding["producer_binding"]
    config_ok = value["config_binding"] == binding["config_binding"]
    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > LIVE_TARGETS:
        raise _LiveRefusal("receipt_schema_invalid")
    safe_attempts: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    model_deltas: list[dict[str, Any]] = []
    identity: tuple[str, str, int] | None = None
    previous_model_calls_after: dict[str, int | None] | None = None
    inter_sample_model_increase = False
    for index, attempt in enumerate(attempts):
        attempt_safe, paired, _, identity, _ = _validate_attempt(
            attempt,
            index,
            target_values[index],
            target_values[index] + LIVE_SAMPLE_DEADLINE_SECONDS,
            generation,
            binding,
            identity,
            seen_nonces,
        )
        counts = attempt_safe["model_call_counts"]
        if counts is not None:
            before = counts["before"]
            after = counts["after"]
            inter_sample_delta: dict[str, int | None] | None = None
            if previous_model_calls_after is not None:
                inter_sample_delta = {}
                for key in _LIVE_COUNTER_KEYS:
                    previous = previous_model_calls_after[key]
                    current = before[key]
                    if previous is None or current is None:
                        inter_sample_delta[key] = None
                    else:
                        difference = current - previous
                        if difference < 0:
                            raise _LiveRefusal("model_call_counters_invalid")
                        inter_sample_delta[key] = difference
                if any(value is not None and value > 0 for value in inter_sample_delta.values()):
                    inter_sample_model_increase = True
            attempt_safe["inter_sample_model_call_delta"] = inter_sample_delta
            previous_model_calls_after = after
        safe_attempts.append(attempt_safe)
        if attempt_safe["model_call_delta"] is not None:
            model_deltas.append(
                {
                    "index": index,
                    "delta": attempt_safe["model_call_delta"],
                    "inter_sample_delta": attempt_safe["inter_sample_model_call_delta"],
                }
            )
        if paired:
            completed.append(
                {
                    "index": index,
                    "end_to_end_wall_seconds": attempt_safe["intervals"]["end_to_end_wall_seconds"],
                    "application_compute_cpu_seconds": attempt_safe["application_compute_cpu_seconds"],
                }
            )
        if attempt["state"] != "completed":
            if index != len(attempts) - 1:
                raise _LiveRefusal("attempt_lifecycle_invalid")
    if state == "passed" and (
        len(attempts) != LIVE_TARGETS or any(item["state"] != "completed" for item in safe_attempts)
    ):
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if state == "failed" and not any(item["state"] == "failed" for item in safe_attempts):
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if state == "incomplete" and len(attempts) == LIVE_TARGETS:
        raise _LiveRefusal("attempt_lifecycle_invalid")
    if state == "incomplete" and attempts and safe_attempts[-1]["state"] == "failed":
        raise _LiveRefusal("attempt_lifecycle_invalid")
    timing_violations = [
        {
            "kind": "sample_deadline",
            "index": attempt["index"],
            "reason_code": reason,
        }
        for attempt in safe_attempts
        for reason in attempt["timing_qualification_reason_codes"]
    ]
    for index, (left, right) in enumerate(zip(safe_attempts, safe_attempts[1:], strict=False)):
        for key, kind in (
            ("outer_started_monotonic", "actual_start_cadence"),
            ("sample_completed_monotonic", "actual_completion_cadence"),
        ):
            left_value = left["boundaries"][key]
            right_value = right["boundaries"][key]
            if left_value is None or right_value is None:
                continue
            interval = right_value - left_value
            if not LIVE_MIN_INTERVAL_SECONDS <= interval <= LIVE_MAX_INTERVAL_SECONDS:
                timing_violations.append(
                    {
                        "kind": kind,
                        "from_index": index,
                        "to_index": index + 1,
                        "interval_seconds": interval,
                    }
                )
    timing_qualified = state == "passed" and not timing_violations
    run_reasons = [duplicate] if duplicate else []
    if not source_ok:
        run_reasons.append("source_binding_drift")
    if not producer_ok:
        run_reasons.append("producer_binding_mismatch")
    if not config_ok:
        run_reasons.append("config_binding_mismatch")
    eligible = duplicate is None and source_ok and producer_ok and config_ok
    if inter_sample_model_increase:
        eligible = False
        run_reasons.append("model_call_delta_nonzero")
    for attempt in safe_attempts:
        if not attempt["binding_ok"] and (attempt["state"] == "completed" or attempt["owner_reached"]):
            eligible = False
            run_reasons.append("binding_mismatch")
        if attempt["state"] == "completed" and not attempt["paired_completed"]:
            eligible = False
            run_reasons.extend(attempt["qualification_reason_codes"])
        if attempt["state"] == "completed" and not attempt["timing_qualified"]:
            eligible = False
            run_reasons.extend(attempt["timing_qualification_reason_codes"] or ["run_timing_invalid"])
        if attempt["application_compute_cpu_available"] and not attempt["paired_completed"]:
            eligible = False
            run_reasons.extend(attempt["pair_provenance_reason_codes"] or ["attempt_lifecycle_invalid"])
        if attempt["owner_reached"] and not attempt["cleanup_ok"]:
            eligible = False
            run_reasons.append("cleanup_invalid")
        if attempt["owner_reached"] and not attempt["receipt_nonce_unique"]:
            eligible = False
            run_reasons.append("owner_receipt_nonce_duplicate")
        delta = attempt["model_call_delta"]
        if attempt["owner_reached"] and (delta is None or any(value is None for value in delta.values())):
            eligible = False
            run_reasons.append("model_call_counters_missing")
        elif attempt["owner_reached"] and any(value != 0 for value in delta.values()):
            eligible = False
            run_reasons.append("model_call_delta_nonzero")
        if attempt["failure"] is not None:
            run_reasons.append(attempt["failure"]["reason_code"])
    if timing_violations:
        run_reasons.append("run_timing_invalid")
        if state == "passed":
            eligible = False
    interval_names = set(_LIVE_REQUIRED_INTERVALS)
    availability = {
        name: sum(attempt["interval_availability"][name] for attempt in safe_attempts)
        for name in sorted(interval_names)
    }
    report_run = {
        "receipt_sha256": digest,
        "receipt_state": state,
        "eligible": eligible,
        "reason_codes": sorted(set(run_reasons)),
        "observer_source_hashes_before": source_hashes_before,
        "observer_source_hashes_after": source_hashes_after,
        "producer_binding": value["producer_binding"],
        "config_binding": value["config_binding"],
        "targets_monotonic": target_values,
        "attempts_begun": len(attempts),
        "attempts": safe_attempts,
        "timing_qualified": timing_qualified,
        "timing_qualification_violations": timing_violations,
        "completed_pairs": completed,
        "failed_phases_and_reasons": [
            {
                "index": item["index"],
                "phase": item["failure"]["phase"],
                "reason_code": item["failure"]["reason_code"],
            }
            for item in safe_attempts
            if item["failure"] is not None
        ],
        "model_call_deltas": model_deltas,
        "interval_availability": availability,
    }
    return report_run, completed, run_id


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    return sorted(values)[math.ceil(q * len(values)) - 1]


def _live_decision(
    runs: list[dict[str, Any]], accepted_binding: dict[str, Any], binding_sha256: str
) -> dict[str, Any]:
    eligible_runs = [run for run in runs if run["eligible"]]
    full_runs = [run for run in eligible_runs if len(run["completed_pairs"]) == LIVE_TARGETS]
    pairs = [sample for run in eligible_runs for sample in run["completed_pairs"]]
    walls = [sample["end_to_end_wall_seconds"] for sample in pairs]
    cpus = [sample["application_compute_cpu_seconds"] for sample in pairs]
    wall_q95 = _quantile(walls, 0.95)
    cpu_q95 = _quantile(cpus, 0.95)
    ratio = cpu_q95 / wall_q95 if cpu_q95 is not None and wall_q95 else None
    complete_run_metrics: list[dict[str, Any]] = []
    cpu_trigger_runs = 0
    ratio_trigger_runs = 0
    for run in full_runs:
        run_walls = [sample["end_to_end_wall_seconds"] for sample in run["completed_pairs"]]
        run_cpus = [sample["application_compute_cpu_seconds"] for sample in run["completed_pairs"]]
        run_wall_q95 = _quantile(run_walls, 0.95)
        run_cpu_q95 = _quantile(run_cpus, 0.95)
        run_ratio = run_cpu_q95 / run_wall_q95 if run_cpu_q95 is not None and run_wall_q95 else None
        cpu_hit = run_cpu_q95 is not None and run_cpu_q95 >= LIVE_APPLICATION_CPU_THRESHOLD_SECONDS
        ratio_hit = run_ratio is not None and run_ratio >= LIVE_APPLICATION_RATIO_THRESHOLD
        cpu_trigger_runs += cpu_hit
        ratio_trigger_runs += ratio_hit
        complete_run_metrics.append(
            {
                "receipt_sha256": run["receipt_sha256"],
                "completed_pairs": len(run["completed_pairs"]),
                "application_compute_cpu_p95_seconds": run_cpu_q95,
                "end_to_end_wall_p95_seconds": run_wall_q95,
                "ratio_of_quantiles": run_ratio,
                "cpu_threshold_met": cpu_hit,
                "ratio_threshold_met": ratio_hit,
            }
        )
    cpu_repeatable = bool(full_runs) and cpu_trigger_runs / len(full_runs) >= LIVE_REPEATABILITY_FRACTION
    ratio_repeatable = bool(full_runs) and ratio_trigger_runs / len(full_runs) >= LIVE_REPEATABILITY_FRACTION
    deadline_candidates: list[dict[str, Any]] = []
    for run in eligible_runs:
        for attempt in run["attempts"]:
            wall = attempt["intervals"]["end_to_end_wall_seconds"]
            cpu = attempt["application_compute_cpu_seconds"]
            all_stages = all(attempt["interval_availability"][name] for name in _LIVE_REQUIRED_INTERVALS)
            failed = attempt["state"] == "failed" and attempt["failure"] is not None
            if (
                failed
                and attempt["failure"]["phase"] == "deadline"
                and attempt["failure"]["reason_code"] == "sample_deadline_missed"
                and all_stages
                and wall is not None
                and cpu is not None
                and wall > LIVE_SAMPLE_DEADLINE_SECONDS
                and cpu >= LIVE_APPLICATION_CPU_THRESHOLD_SECONDS
                and wall - cpu < LIVE_SAMPLE_DEADLINE_SECONDS
            ):
                deadline_candidates.append(
                    {
                        "receipt_sha256": run["receipt_sha256"],
                        "index": attempt["index"],
                        "wall_seconds": wall,
                        "cpu_seconds": cpu,
                    }
                )
    distinct_candidate_runs = {item["receipt_sha256"] for item in deadline_candidates}
    deadline_trigger = len(deadline_candidates) >= 3 and len(distinct_candidate_runs) >= 3
    baseline_enough = len(eligible_runs) >= LIVE_MIN_RUNS and len(pairs) >= LIVE_MIN_PAIRS
    primary_enough = len(full_runs) >= LIVE_MIN_RUNS and len(pairs) >= LIVE_MIN_PAIRS
    binding_mismatch_codes = {
        "binding_mismatch",
        "source_binding_drift",
        "config_binding_mismatch",
        "producer_binding_mismatch",
    }
    mismatched_runs = [run for run in runs if binding_mismatch_codes.intersection(run["reason_codes"])]
    refused_records = [run for run in runs if run.get("receipt_refused") is True]
    if (
        not baseline_enough
        or mismatched_runs
        or refused_records
        or (not primary_enough and not deadline_trigger)
    ):
        decision = "insufficient_evidence"
        reasons = []
        if len(eligible_runs) < LIVE_MIN_RUNS or (
            baseline_enough and len(full_runs) < LIVE_MIN_RUNS and not deadline_trigger
        ):
            reasons.append("insufficient_complete_runs")
        if len(pairs) < LIVE_MIN_PAIRS:
            reasons.append("insufficient_completed_pairs")
        if refused_records:
            reasons.append("receipt_schema_invalid")
        reasons.extend(
            code
            for run in mismatched_runs
            for code in sorted(binding_mismatch_codes.intersection(run["reason_codes"]))
        )
    elif deadline_trigger or (
        primary_enough
        and (
            (cpu_q95 is not None and cpu_q95 >= LIVE_APPLICATION_CPU_THRESHOLD_SECONDS and cpu_repeatable)
            or (ratio is not None and ratio >= LIVE_APPLICATION_RATIO_THRESHOLD and ratio_repeatable)
        )
    ):
        decision = "plan_prototype"
        reasons = []
    elif not primary_enough:
        decision = "insufficient_evidence"
        reasons = ["insufficient_complete_runs"]
    else:
        decision = "defer_rewrite"
        reasons = (
            ["migration_trigger_not_repeatable"]
            if (
                (
                    cpu_q95 is not None
                    and cpu_q95 >= LIVE_APPLICATION_CPU_THRESHOLD_SECONDS
                    and not cpu_repeatable
                )
                or (ratio is not None and ratio >= LIVE_APPLICATION_RATIO_THRESHOLD and not ratio_repeatable)
            )
            else ["no_migration_trigger"]
        )
    return {
        "schema": "crowley.runtime-performance-decision",
        "schema_version": 1,
        "accepted_source_hashes": accepted_binding["accepted_source_hashes"],
        "producer_binding": accepted_binding["producer_binding"],
        "config_binding": accepted_binding["config_binding"],
        "runs": runs,
        "statistics": {
            "binding_sha256": binding_sha256,
            "quantile_definition": "sorted_samples[ceil(N*q)-1]",
            "eligible_run_count": len(eligible_runs),
            "eligible_full_eight_sample_run_count": len(full_runs),
            "binding_mismatch_run_count": len(mismatched_runs),
            "pooled_completed_count": len(pairs),
            "pooled_application_compute_cpu_p95_seconds": cpu_q95,
            "pooled_end_to_end_wall_p95_seconds": wall_q95,
            "ratio_of_quantiles": ratio,
            "run_trigger_repeatability": {
                "eligible_full_runs": len(full_runs),
                "minimum_full_runs": LIVE_MIN_RUNS,
                "minimum_fraction": LIVE_REPEATABILITY_FRACTION,
                "application_cpu_threshold_seconds": LIVE_APPLICATION_CPU_THRESHOLD_SECONDS,
                "application_ratio_threshold": LIVE_APPLICATION_RATIO_THRESHOLD,
                "cpu_trigger_runs": cpu_trigger_runs,
                "cpu_passed": cpu_repeatable if len(full_runs) >= LIVE_MIN_RUNS else None,
                "ratio_trigger_runs": ratio_trigger_runs,
                "ratio_passed": ratio_repeatable if len(full_runs) >= LIVE_MIN_RUNS else None,
                "runs": complete_run_metrics,
            },
            "deadline_failure_candidates": deadline_candidates,
            "deadline_failure_trigger": deadline_trigger,
            "failed_attempt_count": (
                None
                if any(run["failed_phases_and_reasons"] is None for run in runs)
                else sum(len(run["failed_phases_and_reasons"]) for run in runs)
            ),
        },
        "decision": decision,
        "reason_codes": reasons,
        "limitations": [
            (
                "This offline evaluator checks supplied receipts against a separately supplied "
                "parent-expected binding digest."
            ),
            (
                "The binding digest does not prove that a human reviewed the source, and receipt "
                "JSON is not independently authenticated as live capture."
            ),
            (
                "No qualifying real schema-2 inputs are currently available; fixture decisions "
                "are tests only and do not establish a live bottleneck or authorize a Rust rewrite."
            ),
            "Elapsed wall and application process CPU are separate; intervals overlap and are never summed.",
            "External process CPU is unavailable, and private decoder substages have no public boundary.",
        ],
    }


def build_live_report(
    receipts: Sequence[Path],
    *,
    accepted_binding: Path,
    binding_sha256: str,
) -> dict[str, Any]:
    if not 1 <= len(receipts) <= MAX_LIVE_RECEIPTS:
        raise PerformanceInputError(f"live receipts must contain 1..{MAX_LIVE_RECEIPTS} files")
    try:
        binding, binding_digest = _load_binding(accepted_binding, binding_sha256)
    except PerformanceInputError:
        raise
    runs: list[dict[str, Any]] = []
    seen_digests: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_nonces: set[str] = set()
    digest_indexes: dict[str, int] = {}
    run_id_indexes: dict[str, int] = {}
    for path in receipts:
        digest: str | None = None
        try:
            raw, digest = _stable_bounded_read(path, MAX_LIVE_RECEIPT_BYTES, "receipt")
            value = _json_with_depth(raw)
            run, _, run_id = _validate_live_receipt(
                value,
                digest,
                binding,
                seen_digests=seen_digests,
                seen_run_ids=seen_run_ids,
                seen_nonces=seen_nonces,
            )
            duplicate_codes: set[str] = set()
            for key, index_map, code in (
                (digest, digest_indexes, "duplicate_receipt_digest"),
                (run_id, run_id_indexes, "duplicate_run_id"),
            ):
                if key in index_map:
                    duplicate_codes.add(code)
                    previous = runs[index_map[key]]
                    previous["eligible"] = False
                    previous["reason_codes"] = sorted(set(previous["reason_codes"] + [code]))
                else:
                    index_map[key] = len(runs)
            if duplicate_codes:
                run["eligible"] = False
                run["reason_codes"] = sorted(set(run["reason_codes"] + list(duplicate_codes)))
        except _LiveRefusal as exc:
            code = str(exc) if str(exc) in _LIVE_REASON_CODES else "receipt_schema_invalid"
            runs.append(
                {
                    "receipt_sha256": digest,
                    "receipt_state": None,
                    "eligible": False,
                    "reason_codes": [code],
                    "targets_monotonic": None,
                    "receipt_refused": True,
                    "attempts_begun": None,
                    "attempts": None,
                    "completed_pairs": None,
                    "failed_phases_and_reasons": None,
                    "model_call_deltas": None,
                    "interval_availability": None,
                }
            )
        else:
            runs.append(run)
    # A malformed or refused record remains in `runs` and cannot be promoted
    # by the presence of otherwise valid records.
    report = _live_decision(runs, binding, binding_digest)
    report["reason_codes"] = sorted(
        set(report["reason_codes"]).union(
            code
            for run in runs
            for code in run["reason_codes"]
            if code in _LIVE_REASON_CODES or code in LIVE_FAILURE_REASONS
        )
    )
    return report


def build_report(
    inputs: Sequence[Path],
    receipts: Sequence[Path] = (),
    *,
    samples: int = 100,
    warmups: int = 5,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    if not 1 <= samples <= MAX_SAMPLES:
        raise PerformanceInputError(f"samples must be in 1..{MAX_SAMPLES}")
    if not 0 <= warmups <= MAX_SAMPLES:
        raise PerformanceInputError(f"warmups must be in 0..{MAX_SAMPLES}")
    if not 1 <= len(inputs) <= MAX_INPUTS:
        raise PerformanceInputError(f"inputs must contain 1..{MAX_INPUTS} explicit PNGs")
    if len(receipts) > MAX_RECEIPTS:
        raise PerformanceInputError(f"receipts must contain at most {MAX_RECEIPTS} explicit files")
    source_root = repo_root or Path(__file__).resolve().parents[1]
    source_hashes = _source_hashes(source_root)
    measurements = [_measure_input(path, samples=samples, warmups=warmups) for path in inputs]
    return _decision(measurements, receipts, source_hashes=source_hashes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", dest="inputs", action="append", type=Path)
    mode.add_argument("--live-receipt", dest="live_receipts", action="append", type=Path)
    parser.add_argument("--receipt", dest="receipts", action="append", type=Path, default=[])
    parser.add_argument("--accepted-binding", type=Path)
    parser.add_argument("--binding-sha256")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.live_receipts is not None:
            if args.receipts:
                raise PerformanceInputError("--receipt is available only with PNG replay")
            if args.accepted_binding is None or args.binding_sha256 is None:
                raise PerformanceInputError("live mode requires --accepted-binding and --binding-sha256")
            report = build_live_report(
                args.live_receipts,
                accepted_binding=args.accepted_binding,
                binding_sha256=args.binding_sha256,
            )
        else:
            if args.accepted_binding is not None or args.binding_sha256 is not None:
                raise PerformanceInputError("accepted binding options are available only in live mode")
            report = build_report(
                args.inputs,
                args.receipts,
                samples=args.samples,
                warmups=args.warmups,
            )
    except PerformanceInputError as exc:
        print(f"session_performance: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
