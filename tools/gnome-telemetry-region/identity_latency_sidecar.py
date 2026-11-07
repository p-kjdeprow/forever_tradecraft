#!/usr/bin/env python3
"""Read and summarize the private native identity latency profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from crowley.session.cli import _socket_request as _bounded_socket_request
from crowley.session.config import load_config
from crowley.session.daemon import session_config_semantic_sha256
from crowley.session.source_binding import SAFETY_MODULES, build_source_binding

# Keep the private reader bound aligned with the existing 512 KiB IPC record
# bound.  The 256-event diagnostic cap must round-trip without silently
# discarding the slowest profile behind the writer-failure token.
_MAX_BYTES = 524288
_SHA256 = 64
_BARRIERS = ("daemon_pre", "backend_pre", "backend_post", "daemon_post")
_INTERVALS = ("owner_to_request_started", "capture_return_to_response_ready")
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
_OUTCOMES = frozenset({"ok", "failed", "cancelled", "deadline", "not_reached"})


class IdentityLatencyError(RuntimeError):
    """A private profile or manifest is malformed or cannot be trusted as provenance."""


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256
        and all(character in "0123456789abcdef" for character in value)
    )


def _regular_stat(path: Path, *, mode: int) -> os.stat_result:
    try:
        before = path.lstat()
    except OSError as exc:
        raise IdentityLatencyError("profile descriptor is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size < 0
        or before.st_size > _MAX_BYTES
    ):
        raise IdentityLatencyError("profile descriptor is not a bounded private regular file")
    return before


def _same_descriptor(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def read_private_file(path: Path, *, mode: int = 0o600) -> tuple[bytes, str]:
    """Read one profile with descriptor identity checks and return provenance digest."""

    before = _regular_stat(path, mode=mode)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IdentityLatencyError("profile descriptor cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not _same_descriptor(before, opened):
            raise IdentityLatencyError("profile descriptor changed before read")
        digest = hashlib.sha256()
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(16384, _MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_BYTES:
                raise IdentityLatencyError("profile exceeds its bounded size")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _same_descriptor(opened, after) or not _same_descriptor(after, path.lstat()):
            raise IdentityLatencyError("profile descriptor changed during read")
        return bytes(data), digest.hexdigest()
    except OSError as exc:
        raise IdentityLatencyError("profile descriptor read failed") from exc
    finally:
        os.close(descriptor)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityLatencyError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise IdentityLatencyError(f"{label} must be an object")
    return value


def load_expected_binding(path: Path, supplied_sha256: str) -> tuple[dict[str, Any], str]:
    """Load the parent-reviewed manifest; current source files cannot establish it."""

    if not _is_sha(supplied_sha256):
        raise IdentityLatencyError("supplied expected-binding SHA is malformed")
    data, digest = read_private_file(path)
    if digest != supplied_sha256:
        raise IdentityLatencyError("expected-binding manifest SHA does not match the supplied SHA")
    value = _json_object(data, "expected-binding manifest")
    expected = {
        "schema",
        "source_binding",
        "config_file_sha256",
        "config_semantic_sha256",
        "producer_sha256",
    }
    if set(value) != expected or value["schema"] != "identity-latency-binding-v1":
        raise IdentityLatencyError("expected-binding manifest shape is not exact")
    source_binding = value["source_binding"]
    if (
        not isinstance(source_binding, dict)
        or set(source_binding) != SAFETY_MODULES
        or not all(_is_sha(item) for item in source_binding.values())
        or not _is_sha(value["config_file_sha256"])
        or not _is_sha(value["config_semantic_sha256"])
        or not _is_sha(value["producer_sha256"])
    ):
        raise IdentityLatencyError("expected-binding manifest values are malformed")
    return value, digest


def _hash_local_descriptor(path: Path, label: str) -> str:
    """Hash one mutable local input with descriptor and final-stat checks."""

    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > 4 * 1024 * 1024
        ):
            raise IdentityLatencyError(f"{label} descriptor is not safe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise IdentityLatencyError(f"{label} descriptor is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        expected = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != expected:
            raise IdentityLatencyError(f"{label} descriptor changed before read")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        final = path.lstat()
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        final_identity = (
            final.st_dev,
            final.st_ino,
            final.st_mode,
            final.st_nlink,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        )
        if identity != after_identity or after_identity != final_identity:
            raise IdentityLatencyError(f"{label} descriptor changed during read")
        return digest.hexdigest()
    except OSError as exc:
        raise IdentityLatencyError(f"{label} descriptor cannot be read") from exc
    finally:
        os.close(descriptor)


def current_source_binding() -> dict[str, str]:
    """Return the package-derived production safety binding."""

    return dict(build_source_binding().manifest)


def _current_config_binding(path: Path) -> tuple[str, str, str]:
    raw_sha256 = _hash_local_descriptor(path, "config")
    try:
        config = load_config(path)
    except (OSError, TypeError, ValueError) as exc:
        raise IdentityLatencyError(
            "current config or producer binding cannot be loaded through the accepted loader"
        ) from exc
    post_load_raw_sha256 = _hash_local_descriptor(path, "config")
    if post_load_raw_sha256 != raw_sha256:
        raise IdentityLatencyError("current config changed while it was being loaded")
    semantic_sha256 = session_config_semantic_sha256(config)
    producer = config.profile.telemetry_region_executable
    if producer is None:
        raise IdentityLatencyError("config has no pinned telemetry producer")
    producer_sha256 = _hash_local_descriptor(producer, "configured producer")
    if producer_sha256 != config.profile.telemetry_region_executable_sha256:
        raise IdentityLatencyError("configured producer hash differs from loaded config")
    return raw_sha256, semantic_sha256, producer_sha256


def _clock_field(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _duration_record(value: Any, *, names: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "duration_ns",
        "outcome",
    }:
        raise IdentityLatencyError("latency interval shape is not exact")
    if value["name"] not in names or not all(
        _clock_field(value[key])
        for key in (
            "start_monotonic_ns",
            "end_monotonic_ns",
            "duration_ns",
        )
    ):
        raise IdentityLatencyError("latency interval values are malformed")
    if value["outcome"] not in _OUTCOMES:
        raise IdentityLatencyError("latency interval outcome is unsupported")
    start, end, duration = (
        value["start_monotonic_ns"],
        value["end_monotonic_ns"],
        value["duration_ns"],
    )
    if start is None or end is None:
        if duration is not None:
            raise IdentityLatencyError("latency interval duration requires both boundaries")
        if value["outcome"] != "not_reached":
            raise IdentityLatencyError("reached latency interval has an unknown boundary")
    elif end < start or duration != end - start:
        raise IdentityLatencyError("latency interval duration is inconsistent")


def validate_artifact(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "status",
        "source_binding_sha256",
        "producer_sha256",
        "config_file_sha256",
        "config_semantic_sha256",
        "owner_entry_monotonic_ns",
        "owner_bound_monotonic_ns",
        "owner_return_monotonic_ns",
        "intervals_non_additive",
        "intervals",
        "barriers",
        "event_count",
        "events_truncated",
        "events",
    }
    if set(value) != expected or value["schema"] != "identity-latency-v1":
        raise IdentityLatencyError("identity latency artifact shape is not exact")
    if value["status"] not in {"complete", "partial"} or value["intervals_non_additive"] is not True:
        raise IdentityLatencyError("identity latency artifact status is unsupported")
    for key in (
        "source_binding_sha256",
        "producer_sha256",
        "config_file_sha256",
        "config_semantic_sha256",
    ):
        if value[key] is not None and not _is_sha(value[key]):
            raise IdentityLatencyError("identity latency binding is malformed")
    if not isinstance(value["owner_entry_monotonic_ns"], int) or isinstance(
        value["owner_entry_monotonic_ns"], bool
    ):
        raise IdentityLatencyError("identity latency entry clock is malformed")
    for key in ("owner_bound_monotonic_ns", "owner_return_monotonic_ns"):
        if not _clock_field(value[key]):
            raise IdentityLatencyError("identity latency owner clock is malformed")
    entry = value["owner_entry_monotonic_ns"]
    bound = value["owner_bound_monotonic_ns"]
    returned = value["owner_return_monotonic_ns"]
    if (bound is not None and bound < entry) or (returned is not None and returned < entry):
        raise IdentityLatencyError("identity latency owner clocks are out of order")
    if bound is not None and returned is not None and returned < bound:
        raise IdentityLatencyError("identity latency owner return precedes directory binding")
    intervals = value["intervals"]
    if not isinstance(intervals, list) or len(intervals) != len(_INTERVALS):
        raise IdentityLatencyError("identity latency interval count is malformed")
    for item, name in zip(intervals, _INTERVALS, strict=True):
        if not isinstance(item, dict) or item.get("name") != name:
            raise IdentityLatencyError("identity latency interval order is malformed")
        _duration_record(item, names={name})
    barriers = value["barriers"]
    if not isinstance(barriers, list) or len(barriers) != len(_BARRIERS):
        raise IdentityLatencyError("identity latency barrier count is malformed")
    for item, name in zip(barriers, _BARRIERS, strict=True):
        if not isinstance(item, dict) or set(item) != {
            "barrier",
            "start_monotonic_ns",
            "end_monotonic_ns",
            "duration_ns",
            "outcome",
        }:
            raise IdentityLatencyError("identity latency barrier shape is not exact")
        if item["barrier"] != name:
            raise IdentityLatencyError("identity latency barrier order is malformed")
        _duration_record(
            {
                "name": item["barrier"],
                "start_monotonic_ns": item["start_monotonic_ns"],
                "end_monotonic_ns": item["end_monotonic_ns"],
                "duration_ns": item["duration_ns"],
                "outcome": item["outcome"],
            },
            names={name},
        )
    events = value["events"]
    if (
        not isinstance(value["event_count"], int)
        or isinstance(value["event_count"], bool)
        or not 0 <= value["event_count"] <= 256
        or not isinstance(value["events_truncated"], bool)
        or not isinstance(events, list)
        or len(events) > 256
    ):
        raise IdentityLatencyError("identity latency event count is malformed")
    if value["event_count"] != len(events):
        raise IdentityLatencyError("identity latency event count is malformed")
    if value["status"] == "complete" and value["events_truncated"]:
        raise IdentityLatencyError("identity latency truncated trace cannot be complete")
    event_keys = {
        "name",
        "barrier",
        "ordinal",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "duration_ns",
        "owner_thread_cpu_ns",
        "owner_process_cpu_ns",
        "child_cpu_ns",
        "child_cpu_reason",
        "outcome",
    }
    for event in events:
        if not isinstance(event, dict) or set(event) != event_keys:
            raise IdentityLatencyError("identity latency event shape is not exact")
        if (
            event["name"] not in _EVENT_NAMES
            or event["barrier"] not in {*_BARRIERS, "none"}
            or not isinstance(event["ordinal"], int)
            or isinstance(event["ordinal"], bool)
            or not 0 <= event["ordinal"] <= 31
            or not all(
                _clock_field(event[key])
                for key in (
                    "start_monotonic_ns",
                    "end_monotonic_ns",
                    "duration_ns",
                    "owner_thread_cpu_ns",
                    "owner_process_cpu_ns",
                )
            )
            or event["child_cpu_ns"] is not None
            or event["child_cpu_reason"] != "external_child_cpu_unmeasured"
            or event["outcome"] not in _OUTCOMES
        ):
            raise IdentityLatencyError("identity latency event values are malformed")
        _duration_record(
            {
                "name": event["name"],
                "start_monotonic_ns": event["start_monotonic_ns"],
                "end_monotonic_ns": event["end_monotonic_ns"],
                "duration_ns": event["duration_ns"],
                "outcome": event["outcome"],
            },
            names={event["name"]},
        )
    return value


def _canonical_source_binding(source_binding: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(source_binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _artifact_path(response: dict[str, Any]) -> Path | None:
    if response.get("ok") is True:
        sample = response.get("sample")
        directory = sample.get("artifact_directory") if isinstance(sample, dict) else None
        if isinstance(directory, str):
            return Path(directory) / "identity-latency.json"
    failure = response.get("failure")
    receipt = failure.get("receipt_path") if isinstance(failure, dict) else None
    if isinstance(receipt, str):
        return Path(receipt).with_name("identity-latency.json")
    return None


def _response_bindings(response: dict[str, Any]) -> dict[str, Any]:
    payload = response.get("sample") if response.get("ok") is True else response.get("failure")
    if not isinstance(payload, dict):
        return {}
    config = payload.get("config")
    producer = payload.get("producer")
    return {
        "config_file_sha256": (
            config.get("request_config_file_sha256")
            if isinstance(config, dict)
            else payload.get("config_file_sha256")
        ),
        "config_semantic_sha256": (
            config.get("request_config_semantic_sha256")
            if isinstance(config, dict)
            else payload.get("config_semantic_sha256")
        ),
        "producer_sha256": (
            producer.get("sha256") if isinstance(producer, dict) else payload.get("producer_sha256")
        ),
        "source_binding": payload.get("source_binding"),
    }


def _response_owner_return_ns(response: dict[str, Any]) -> int | None:
    payload = response.get("sample") if response.get("ok") is True else response.get("failure")
    if not isinstance(payload, dict):
        return None
    value = payload.get("owner_returned")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        return None
    return int(round(float(value) * 1_000_000_000))


def _response_owner_return_reason(response: dict[str, Any]) -> str | None:
    payload = response.get("sample") if response.get("ok") is True else response.get("failure")
    if not isinstance(payload, dict) or "owner_returned" not in payload:
        return "owner_return_clock_unavailable"
    value = payload["owner_returned"]
    if value is None:
        return "owner_return_clock_unavailable"
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        return "owner_return_clock_invalid"
    return None


def _binding_consistent(response: dict[str, Any], artifact: dict[str, Any], expected: dict[str, Any]) -> bool:
    response_binding = _response_bindings(response)
    expected_source = _canonical_source_binding(expected["source_binding"])
    artifact_source = artifact.get("source_binding_sha256")
    if artifact_source != expected_source:
        return False
    for key in ("config_file_sha256", "config_semantic_sha256", "producer_sha256"):
        if artifact.get(key) != expected[key]:
            return False
        if response_binding.get(key) is not None and response_binding[key] != expected[key]:
            return False
    source = response_binding.get("source_binding")
    return isinstance(source, dict) and source == expected["source_binding"]


def read_response_profile(
    response: dict[str, Any],
    expected_binding: dict[str, Any],
) -> dict[str, Any]:
    path = _artifact_path(response)
    record: dict[str, Any] = {
        "response_ok": response.get("ok") is True,
        "artifact_sha256": None,
        "artifact_status": None,
        "binding_consistent": False,
        "reason": "artifact_absent",
        "barriers": None,
        "intervals": None,
        "event_count": None,
        "events_truncated": None,
        "events": None,
        "owner_entry_monotonic_ns": None,
        "owner_bound_monotonic_ns": None,
        "owner_return_monotonic_ns": None,
        "owner_returned_monotonic_ns": None,
        "owner_return_to_response_received_ns": None,
        "owner_return_reason": None,
    }
    if path is None:
        return record
    try:
        data, digest = read_private_file(path)
        artifact = validate_artifact(_json_object(data, "identity latency artifact"))
        consistent = _binding_consistent(response, artifact, expected_binding)
    except IdentityLatencyError:
        record["reason"] = "artifact_invalid"
        return record
    record.update(
        {
            "artifact_sha256": digest,
            "artifact_status": artifact["status"],
            "binding_consistent": consistent,
            "reason": "ok" if consistent else "binding_inconsistent",
            "barriers": artifact["barriers"],
            "intervals": artifact["intervals"],
            "event_count": artifact["event_count"],
            "events_truncated": artifact["events_truncated"],
            "events": artifact["events"],
            "owner_entry_monotonic_ns": artifact["owner_entry_monotonic_ns"],
            "owner_bound_monotonic_ns": artifact["owner_bound_monotonic_ns"],
            "owner_return_monotonic_ns": artifact["owner_return_monotonic_ns"],
        }
    )
    return record


def build_report(
    responses: list[dict[str, Any]],
    expected_binding: dict[str, Any],
    manifest_sha256: str,
    transport: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    if not _is_sha(manifest_sha256):
        raise IdentityLatencyError("manifest provenance SHA is malformed")
    samples = [read_response_profile(response, expected_binding) for response in responses]
    if transport is not None:
        if len(transport) != len(samples):
            raise IdentityLatencyError("transport timing count does not match sample count")
        for index, (sample, timing) in enumerate(zip(samples, transport, strict=True)):
            if set(timing) != {
                "request_started_monotonic_ns",
                "response_received_monotonic_ns",
                "transport_duration_ns",
            }:
                raise IdentityLatencyError("transport timing shape is not exact")
            started = timing["request_started_monotonic_ns"]
            received = timing["response_received_monotonic_ns"]
            duration = timing["transport_duration_ns"]
            if (
                not isinstance(started, int)
                or isinstance(started, bool)
                or not isinstance(received, int)
                or isinstance(received, bool)
                or not isinstance(duration, int)
                or isinstance(duration, bool)
                or started < 0
                or received < started
                or duration != received - started
            ):
                raise IdentityLatencyError("transport timing values are malformed")
            sample.update(timing)
            owner_returned = _response_owner_return_ns(responses[index])
            if owner_returned is None:
                sample["owner_return_reason"] = _response_owner_return_reason(responses[index])
            elif not started <= owner_returned <= received:
                sample["owner_return_reason"] = "owner_return_clock_invalid"
            else:
                sample["owner_returned_monotonic_ns"] = owner_returned
                sample["owner_return_to_response_received_ns"] = (
                    timing["response_received_monotonic_ns"] - owner_returned
                )
    return {
        "schema": "identity-latency-diagnostic-v1",
        "manifest_sha256": manifest_sha256,
        "sample_count": len(samples),
        "diagnostic_only": True,
        "samples": samples,
    }


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(encoded) > 524288:
        raise IdentityLatencyError("private response exceeds its bounded size")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _socket_request(
    socket_path: Path,
    request: dict[str, Any],
    *,
    timeout: float = 5.0,
    response_deadline: float | None = None,
    failure_evidence_grace: float = 0.0,
) -> dict[str, Any]:
    """Use the session CLI's strict one-record, absolute-deadline client."""

    try:
        return _bounded_socket_request(
            socket_path,
            request,
            timeout=timeout,
            response_deadline=response_deadline,
            failure_evidence_grace=failure_evidence_grace,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise IdentityLatencyError(f"owner IPC request failed: {exc}") from exc


def _status_preflight(
    socket_path: Path,
    generation: int,
) -> dict[str, Any]:
    response = _socket_request(socket_path, {"command": "status"}, timeout=5.0)
    if response.get("ok") is not True or not isinstance(response.get("status"), dict):
        raise IdentityLatencyError("owner status preflight was not successful")
    status = response["status"]
    if (
        status.get("state") != "PAUSED"
        or status.get("generation") != generation
        or status.get("stopRequested") is not False
        or status.get("closeoutRequested") is not False
        or status.get("actionIntent") != ""
        or status.get("reconciliationRequired") is not False
        or status.get("nativeBootstrapPending") is not False
        or status.get("nativeBootstrapReady") is not False
    ):
        raise IdentityLatencyError("owner status is not the exact paused diagnostic boundary")
    operation = status.get("calibrationOperation")
    if not isinstance(operation, dict) or operation.get("state") in {"starting", "running"}:
        raise IdentityLatencyError("owner status has an active calibration operation")
    return status


def _create_fresh_raw_directory(path: Path) -> None:
    """Create one new private leaf; never reuse or follow caller state."""

    if path.exists() or path.is_symlink():
        raise IdentityLatencyError("raw evidence directory must be a fresh path")
    try:
        path.mkdir(mode=0o700)
        # mkdir(2) applies the process umask.  Normalize the newly-created
        # leaf before validating it so the private-directory contract is
        # independent of the caller's umask.
        os.chmod(path, 0o700)
        metadata = path.lstat()
    except OSError as exc:
        raise IdentityLatencyError("raw evidence directory could not be created safely") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise IdentityLatencyError("raw evidence directory is not a fresh private directory")


def _preflight_output(path: Path) -> None:
    """Prove the final report destination is fresh and writable before IPC."""

    if path.exists() or path.is_symlink():
        raise IdentityLatencyError("diagnostic report output must be a fresh path")
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = parent.lstat()
    except OSError as exc:
        raise IdentityLatencyError("diagnostic report output parent is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IdentityLatencyError("diagnostic report output parent is not a directory")
    probe: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".probe", dir=parent, delete=False
        ) as stream:
            probe = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise IdentityLatencyError("diagnostic report output is not writable") from exc
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass


def _parse_hex(value: str, label: str) -> str:
    if not _is_sha(value):
        raise argparse.ArgumentTypeError(f"{label} must be a lowercase SHA-256")
    return value


def _load_response(path: Path) -> dict[str, Any]:
    data, _digest = read_private_file(path)
    return _json_object(data, "fixture response")


def _run_fixture(args: argparse.Namespace) -> dict[str, Any]:
    expected, manifest_sha = load_expected_binding(Path(args.manifest), args.manifest_sha256)
    response = _load_response(Path(args.response))
    return build_report([response], expected, manifest_sha)


def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    expected, manifest_sha = load_expected_binding(Path(args.manifest), args.manifest_sha256)
    if (
        args.config_file_sha256 != expected["config_file_sha256"]
        or args.config_semantic_sha256 != expected["config_semantic_sha256"]
    ):
        raise IdentityLatencyError("request config binding differs from the reviewed manifest")
    config_path = getattr(args, "config_file", None)
    if config_path is None:
        raise IdentityLatencyError("live mode requires the reviewed config file path")
    current_config_sha256, current_semantic_sha256, current_producer_sha256 = _current_config_binding(
        Path(config_path)
    )
    if (
        current_config_sha256 != expected["config_file_sha256"]
        or current_semantic_sha256 != expected["config_semantic_sha256"]
        or current_producer_sha256 != expected["producer_sha256"]
    ):
        raise IdentityLatencyError("current config or producer binding differs from the reviewed manifest")
    current_source = current_source_binding()
    if current_source != expected["source_binding"]:
        raise IdentityLatencyError("current source binding differs from the reviewed manifest")
    _status_preflight(Path(args.socket), args.generation)
    responses: list[dict[str, Any]] = []
    transport: list[dict[str, int]] = []
    raw_dir = Path(args.raw_dir)
    _create_fresh_raw_directory(raw_dir)
    if getattr(args, "output", None) is not None:
        _preflight_output(Path(args.output))
    for ordinal in range(3):
        parent_started = time.monotonic()
        request = {
            "command": "paused_region_sample",
            "generation": args.generation,
            "parent_started": parent_started,
            "deadline": parent_started + 1.0,
            "config_file_sha256": args.config_file_sha256,
            "config_semantic_sha256": args.config_semantic_sha256,
        }
        request_started = time.monotonic_ns()
        try:
            response = _socket_request(
                Path(args.socket),
                request,
                response_deadline=request["deadline"],
                failure_evidence_grace=0.25,
            )
        except (IdentityLatencyError, OSError):
            response = {"ok": False, "error": "owner_ipc_unavailable"}
        response_received = time.monotonic_ns()
        responses.append(response)
        transport.append(
            {
                "request_started_monotonic_ns": request_started,
                "response_received_monotonic_ns": response_received,
                "transport_duration_ns": response_received - request_started,
            }
        )
        _write_private_json(raw_dir / f"response-{ordinal:02d}.json", response)
    return build_report(responses, expected, manifest_sha, transport)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--manifest-sha256", required=True, type=lambda value: _parse_hex(value, "manifest SHA")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--generation", type=int)
    parser.add_argument("--config-file-sha256", type=lambda value: _parse_hex(value, "config file SHA"))
    parser.add_argument(
        "--config-semantic-sha256", type=lambda value: _parse_hex(value, "config semantic SHA")
    )
    args = parser.parse_args()
    if args.mode == "fixture":
        if args.response is None or args.socket is not None or args.raw_dir is not None:
            parser.error("fixture mode requires --response and forbids live transport arguments")
        report = _run_fixture(args)
    else:
        if (
            args.socket is None
            or args.raw_dir is None
            or args.generation is None
            or args.config_file_sha256 is None
            or args.config_semantic_sha256 is None
            or args.config_file is None
            or args.response is not None
        ):
            parser.error(
                "live mode requires socket, raw-dir, generation, config file, and both config hashes"
            )
        report = _run_live(args)
    encoded = (json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        args.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
