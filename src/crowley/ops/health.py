"""Bounded unattended Crowley backup and read-only health operations.

The commands in this module are deliberately explicit about every path.  They
are suitable for private user systemd units and retain only bounded, typed
metadata; snapshot creation and validation remain owned by ``market_backup``.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crowley.ops import backup as market_backup
from crowley.ops.backup import BACKUP_ID_RE
from crowley.platform.paths import XdgDirs, XdgPathError
from crowley.session import evidence
from crowley.session.cli import _socket_request
from crowley.session.config import ClientConfig, load_client_config

OPS_SCHEMA = "crowley.host-operations-v1"
BACKUP_RECORD_SCHEMA = "crowley.host-last-good-backup-v1"
HEALTH_SCHEMA = "crowley.host-health-v1"
RECEIPT_SCHEMA = "crowley.host-operation-receipt-v1"
MAX_JOURNAL_RECORDS = 64
MAX_ISSUES = 16
MAX_ERROR_LENGTH = 160
MAX_BACKUP_RECORD_BYTES = 16 * 1024
# Manifest members grow with the retained raw archive.  Keep a bounded parser
# limit with headroom for the current format rather than rejecting valid
# backups after a short collection interval.
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MARKER_BYTES = 128
MAX_JOURNAL_BYTES = 128 * 1024
# Hourly backup cadence plus a 15-minute completion grace period.
BACKUP_MAX_AGE_SECONDS = 75 * 60
UPLOAD_MAX_AGE_SECONDS = 30 * 60
WAIT_DUE_GRACE_SECONDS = 4 * 60
SERVICE_TIMEOUT_SECONDS = 5.0
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_COMMAND_STATES = frozenset(
    {
        "SCANNING",
        "QUIET_SAVE",
        "SAVE_RELOAD_SENT",
        "SAVE_RELOAD_WAIT",
        "UPLOAD_WAIT",
        "ACK_RELOAD_SENT",
        "ACK_RELOAD_WAIT",
        "QUALIFY_PENDING",
        "REOPEN",
        "DISK_CLEANUP_SENT",
        "DISK_CLEANUP_WAIT",
    }
)
SETTLED_STATES = frozenset(
    {
        "WAIT_DUE",
        "QUALIFY_PENDING",
        "ACK_RELOAD_SENT",
        "ACK_RELOAD_WAIT",
        "REOPEN",
        "DISK_CLEANUP_SENT",
        "DISK_CLEANUP_WAIT",
    }
)
PRE_SCAN_STATES = frozenset({"READY", "ARMED", "RECONCILE"})
DEADLINE_STATES = frozenset(
    {
        "SCANNING",
        "QUIET_SAVE",
        "SAVE_RELOAD_SENT",
        "SAVE_RELOAD_WAIT",
        "UPLOAD_WAIT",
        "ACK_RELOAD_SENT",
        "ACK_RELOAD_WAIT",
        "DISK_CLEANUP_WAIT",
    }
)
TRANSITION_DEADLINE_STATES = frozenset()
GATED_STATES = frozenset({"PAUSED", "STOPPED", "NATIVE_CALIBRATION", "INPUT_CALIBRATION"})
KNOWN_STATES = (
    PRE_SCAN_STATES
    | GATED_STATES
    | SAFE_COMMAND_STATES
    | SETTLED_STATES
    | {
        "BLOCKED",
        "LOGIN_REQUIRED",
    }
)
ALLOWED_SERVICE_FIELDS = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus")
SERVICE_NAMES = ("crowley-session.service", "crowley-companion.service")


class OperationsError(RuntimeError):
    """A fixed, safe-to-report failure category."""


class BusyError(OperationsError):
    """The operation's process-lifetime lock is already held."""


@dataclass(frozen=True, slots=True)
class OperationPaths:
    """The installed-package operational paths resolved from XDG roots."""

    config_path: Path
    state_root: Path
    backup_root: Path
    companion_state: Path


def operation_paths() -> OperationPaths:
    try:
        dirs = XdgDirs.from_environ(os.environ)
    except XdgPathError as exc:
        raise OperationsError("Crowley XDG paths are unavailable") from exc
    return OperationPaths(
        config_path=dirs.crowley_config / "session.toml",
        state_root=dirs.crowley_state / market_backup.OPERATIONS_STATE,
        backup_root=dirs.crowley_state / market_backup.BACKUP_STATE,
        companion_state=dirs.crowley_state / "companion-state.json",
    )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, where: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OperationsError(f"invalid {where}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationsError(f"invalid {where}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise OperationsError(f"invalid {where}")
    return parsed.astimezone(dt.UTC)


def _finite_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise OperationsError(f"invalid {where}")
    return float(value)


def _private_dir(path: Path) -> None:
    if not path.is_absolute():
        raise OperationsError("private path must be absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise OperationsError("private directory is unsafe")
    os.chmod(path, 0o700)


def _private_existing_dir(path: Path) -> None:
    """Validate an owned private directory without creating or changing it."""

    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != os.getuid()
        or stat.S_IMODE(path.stat().st_mode) != 0o700
    ):
        raise OperationsError("private directory is unsafe")


def _private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise OperationsError("private file is unsafe")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise OperationsError("private file mode is unsafe")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _read_bounded(path: Path, limit: int, where: str) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise OperationsError(f"{where} is unavailable") from exc
    if len(data) > limit:
        raise OperationsError(f"{where} is too large")
    return data


def _strict_json(path: Path, *, limit: int = MAX_BACKUP_RECORD_BYTES) -> Any:
    try:
        return json.loads(_read_bounded(path, limit, "private JSON record").decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OperationsError("private JSON record is malformed") from exc


def _bounded_text(value: Any, where: str, *, limit: int = MAX_ERROR_LENGTH) -> str:
    if not isinstance(value, str):
        raise OperationsError(f"invalid {where}")
    return value[:limit]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    if len(data) > 256 * 1024:
        raise OperationsError("private record is too large")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        dirfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


@contextmanager
def _process_lock(path: Path):
    _private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BusyError("operation already running") from None
            raise OperationsError("operation lock failed") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _safe_backup_name(value: Any) -> str:
    if not isinstance(value, str) or not BACKUP_ID_RE.fullmatch(value):
        raise OperationsError("invalid backup name")
    return value


def _write_receipt(state_root: Path, receipt: dict[str, Any]) -> None:
    receipts = state_root / "receipts"
    try:
        _private_dir(receipts)
        stamp = str(receipt.get("ended_at_utc") or receipt.get("started_at_utc") or "unknown").replace(
            ":", ""
        )
        name = f"{stamp}-{os.getpid()}.json"
        _atomic_bytes(receipts / name, _canonical_json(receipt))
        entries = sorted((p for p in receipts.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime_ns)
        for old in entries[:-32]:
            try:
                old.unlink()
            except OSError:
                pass
    except (OSError, OperationsError):
        # A receipt cannot change the operation's causal result.
        return


def fixed_error(exc: BaseException) -> str:
    if isinstance(exc, BusyError):
        return "busy"
    if isinstance(exc, market_backup.BackupError):
        return "snapshot_or_validation_failed"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, (OSError, OperationsError)):
        return "local_io_or_validation_failed"
    return "operation_failed"


_fixed_error = fixed_error


def _record_base(kind: str, started: dt.datetime) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "kind": kind,
        "started_at_utc": _utc_text(started),
        "ended_at_utc": "",
        "outcome": "failed",
        "error_category": "",
    }


def backup_operation(
    *,
    state_root: Path,
    backup_root: Path,
    snapshot_fn: Callable[[Path], Path] = market_backup.snapshot,
    validate_fn: Callable[[Path], tuple[dict[str, Any], str]] = market_backup.validate_snapshot,
    now: Callable[[], dt.datetime] = _utc_now,
) -> dict[str, Any]:
    """Create and revalidate one snapshot, preserving last-good on every failure."""

    _private_dir(state_root)
    _private_dir(backup_root)
    started = now()
    receipt = _record_base("backup", started)
    try:
        with _process_lock(state_root / "backup.lock"):
            snapshot_path = snapshot_fn(backup_root)
            if not isinstance(snapshot_path, Path):
                snapshot_path = Path(snapshot_path)
            try:
                snapshot_path = snapshot_path.resolve(strict=True)
            except OSError as exc:
                raise OperationsError("snapshot path is unavailable") from exc
            if snapshot_path.parent != backup_root.resolve() or not BACKUP_ID_RE.fullmatch(
                snapshot_path.name
            ):
                raise OperationsError("snapshot path is outside the backup root")
            marker_path = snapshot_path / "COMPLETE"
            if marker_path.stat().st_size > MAX_MARKER_BYTES:
                raise OperationsError("snapshot completion marker is too large")
            # Snapshot creation validates internally.  This one explicit
            # post-return validation is the last-good admission gate.
            manifest, digest = validate_fn(snapshot_path)
            backup_id = _safe_backup_name(manifest.get("backup_id"))
            if backup_id != snapshot_path.name or not SHA_RE.fullmatch(digest):
                raise OperationsError("snapshot identity is inconsistent")
            completed = now()
            record = {
                "schema": BACKUP_RECORD_SCHEMA,
                "snapshot_name": backup_id,
                "manifest_sha256": digest,
                "completed_at_utc": _utc_text(completed),
            }
            _atomic_bytes(state_root / "last-good-backup.json", _canonical_json(record))
            receipt.update(
                outcome="complete",
                error_category="",
                snapshot_name=backup_id,
                manifest_sha256=digest,
            )
    except BusyError:
        receipt.update(outcome="busy", error_category="busy")
        receipt["ended_at_utc"] = _utc_text(now())
        _write_receipt(state_root, receipt)
        return {"outcome": "busy", "exit_code": 0, "receipt": receipt}
    except BaseException as exc:
        receipt.update(outcome="failed", error_category=_fixed_error(exc))
        receipt["ended_at_utc"] = _utc_text(now())
        _write_receipt(state_root, receipt)
        return {"outcome": "failed", "exit_code": 2, "receipt": receipt}
    receipt["ended_at_utc"] = _utc_text(now())
    _write_receipt(state_root, receipt)
    return {"outcome": "complete", "exit_code": 0, "receipt": receipt}


def _bounded_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationsError("status is not an object")
    state = value.get("state")
    if not isinstance(state, str) or state not in KNOWN_STATES:
        raise OperationsError("status state is invalid")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in {"repeat", "validation", "interaction"}:
        raise OperationsError("status mode is invalid")
    generation = value.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise OperationsError("status generation is invalid")
    native = value.get("nativeCalibration")
    input_ready = value.get("inputCalibration")
    if not isinstance(native, bool) or not isinstance(input_ready, bool):
        raise OperationsError("status calibration flags are invalid")
    result: dict[str, Any] = {
        "state": state,
        "mode": mode,
        "generation": generation,
        "nativeCalibration": native,
        "inputCalibration": input_ready,
    }
    for name in ("lastUid", "actionIntent"):
        item = value.get(name, "")
        if not isinstance(item, str) or len(item) > 256:
            raise OperationsError(f"status {name} is invalid")
        result[name] = item
    result["blocker"] = _bounded_text(value.get("blocker", ""), "status blocker")
    for name in ("deadlineAt", "dueAt"):
        item = value.get(name, 0)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise OperationsError(f"status {name} is invalid")
        result[name] = float(item)
    operation = value.get("calibrationOperation")
    if not isinstance(operation, dict) or len(operation) > 16:
        raise OperationsError("calibration operation is invalid")
    if set(("kind", "state", "error")) - set(operation):
        raise OperationsError("calibration operation is incomplete")
    op: dict[str, Any] = {}
    for key in ("kind", "state", "error"):
        item = operation[key]
        if not isinstance(item, str):
            raise OperationsError("calibration operation field is invalid")
        op[key] = item if key != "error" else item[:MAX_ERROR_LENGTH]
    if "state" in op and op["state"] not in {"idle", "starting", "running", "complete", "error"}:
        raise OperationsError("calibration operation state is invalid")
    if "kind" in op and op["kind"] not in {"", "native", "input"}:
        raise OperationsError("calibration operation kind is invalid")
    result["calibrationOperation"] = op
    pending = value.get("pending", {})
    if pending is None:
        pending = {}
    if not isinstance(pending, dict) or len(pending) > 16:
        raise OperationsError("pending status is invalid")
    bounded_pending: dict[str, Any] = {}
    allowed_pending = {"ids", "count", "cap", "blocked", "reason"}
    if set(pending) - allowed_pending:
        raise OperationsError("pending status fields are invalid")
    if pending:
        pending_ids = pending.get("ids")
        count = pending.get("count")
        cap = pending.get("cap")
        blocked = pending.get("blocked")
        reason = pending.get("reason")
        if (
            not isinstance(pending_ids, list)
            or len(pending_ids) > 6
            or any(not isinstance(uid, str) or not uid or len(uid) > 256 for uid in pending_ids)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 6
            or count != len(pending_ids)
            or isinstance(cap, bool)
            or not isinstance(cap, int)
            or cap != 6
            or not isinstance(blocked, bool)
            or not isinstance(reason, str)
            or len(reason) > MAX_ERROR_LENGTH
            or (blocked and reason not in {"queue_full", "plan_invalid"})
            or (not blocked and reason)
            or (reason == "queue_full" and count != cap)
        ):
            raise OperationsError("pending status invariants are invalid")
        bounded_pending = {
            "count": count,
            "cap": cap,
            "blocked": blocked,
            "reason": reason,
            "ids_count": len(pending_ids),
        }
    result["pending"] = bounded_pending
    assessment = value.get("nativeResourceAssessment")
    if assessment is not None:
        if not isinstance(assessment, dict):
            raise OperationsError("native resource assessment is invalid")
        expected_assessment_keys = {
            "classification",
            "method",
            "evidenceHash",
            "current",
            "construction",
            "updates",
        }
        if set(assessment) != expected_assessment_keys:
            raise OperationsError("native resource assessment fields are incomplete")
        bounded: dict[str, Any] = {}
        classification = assessment.get("classification")
        if classification not in {"within_targets", "target_exceeded", "not_measured"}:
            raise OperationsError("native resource assessment classification is invalid")
        bounded["classification"] = classification
        method = assessment.get("method")
        evidence_hash = assessment.get("evidenceHash")
        if classification == "not_measured":
            if method is not None or evidence_hash is not None:
                raise OperationsError("not measured assessment must be null-bound")
            bounded["method"] = None
            bounded["evidenceHash"] = None
        else:
            if not isinstance(method, str) or not method or len(method) > 128:
                raise OperationsError("measured assessment method is invalid")
            if not isinstance(evidence_hash, str) or not SHA_RE.fullmatch(evidence_hash):
                raise OperationsError("measured assessment hash is invalid")
            bounded["method"] = method
            bounded["evidenceHash"] = evidence_hash
        current = assessment.get("current")
        if not isinstance(current, bool):
            raise OperationsError("native resource assessment current flag is invalid")
        bounded["current"] = current
        if classification == "not_measured" and current:
            raise OperationsError("not measured assessment cannot be current")
        for phase in ("construction", "updates"):
            detail = assessment.get(phase)
            if classification == "not_measured":
                if detail is not None:
                    raise OperationsError("not measured assessment has measured facts")
                bounded[phase] = None
                continue
            if not isinstance(detail, dict) or set(detail) != {"growthBytes", "targetBytes", "withinTarget"}:
                raise OperationsError("native resource assessment detail is incomplete")
            clean_detail: dict[str, Any] = {}
            for key in ("growthBytes", "targetBytes"):
                number = detail[key]
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    raise OperationsError("native resource assessment bytes are invalid")
                clean_detail[key] = number
            if clean_detail["targetBytes"] <= 0 or not isinstance(detail["withinTarget"], bool):
                raise OperationsError("native resource assessment detail is invalid")
            if detail["withinTarget"] != (clean_detail["growthBytes"] <= clean_detail["targetBytes"]):
                raise OperationsError("native resource assessment detail is not derived")
            clean_detail["withinTarget"] = detail["withinTarget"]
            bounded[phase] = clean_detail
        if classification == "not_measured":
            result["nativeResourceAssessment"] = bounded
            return result
        if classification == "within_targets" and not all(
            bounded[phase].get("withinTarget") is True for phase in ("construction", "updates")
        ):
            raise OperationsError("within target assessment facts disagree")
        if classification == "target_exceeded" and all(
            bounded[phase].get("withinTarget") is True for phase in ("construction", "updates")
        ):
            raise OperationsError("target exceeded assessment facts disagree")
        result["nativeResourceAssessment"] = bounded
    else:
        result["nativeResourceAssessment"] = None
    return result


def _service_status() -> dict[str, dict[str, str]]:
    command = [
        "systemctl",
        "--user",
        "show",
        *SERVICE_NAMES,
        "--no-pager",
        *[f"--property={field}" for field in ("Id", *ALLOWED_SERVICE_FIELDS)],
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=SERVICE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperationsError("service query unavailable") from exc
    if completed.returncode != 0:
        raise OperationsError("service query failed")
    allowed = {"Id", *ALLOWED_SERVICE_FIELDS}
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            if block:
                blocks.append(block)
                block = []
        else:
            block.append(line)
    if block:
        blocks.append(block)
    result: dict[str, dict[str, str]] = {}
    required = set(ALLOWED_SERVICE_FIELDS)
    for lines in blocks:
        fields: dict[str, str] = {}
        for line in lines:
            if "=" not in line:
                raise OperationsError("service query is malformed")
            key, value = line.split("=", 1)
            if key not in allowed or key in fields:
                raise OperationsError("service query is malformed")
            fields[key] = value[:64]
        unit = fields.pop("Id", None)
        if unit not in SERVICE_NAMES or set(fields) != required or unit in result:
            raise OperationsError("service query is incomplete")
        result[unit] = fields
    if set(result) != set(SERVICE_NAMES) or any(
        set(fields) != set(ALLOWED_SERVICE_FIELDS) for fields in result.values()
    ):
        raise OperationsError("service query is incomplete")
    return result


def _service_issues(services: dict[str, dict[str, str]]) -> list[str]:
    issues: list[str] = []
    for name in SERVICE_NAMES:
        item = services[name]
        if item["LoadState"] != "loaded":
            issues.append(f"{name}:not_loaded")
        if item["ActiveState"] != "active" or item["SubState"] not in {"running", "listening"}:
            issues.append(f"{name}:not_active")
        if item["Result"] not in {"success", "active", ""}:
            issues.append(f"{name}:result_{item['Result'][:32]}")
        try:
            exit_status = int(item["ExecMainStatus"], 10)
        except (KeyError, TypeError, ValueError):
            issues.append(f"{name}:exec_main_status_invalid")
        else:
            if exit_status != 0:
                issues.append(f"{name}:exec_main_status_{exit_status}")
    return issues


def _backup_record(backup_root: Path, state_root: Path, now: dt.datetime) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    path = state_root / "last-good-backup.json"
    if not path.exists():
        return {}, ["backup_record_missing"]
    try:
        _private_file(path)
        record = _strict_json(path, limit=MAX_BACKUP_RECORD_BYTES)
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "snapshot_name",
            "manifest_sha256",
            "completed_at_utc",
        }:
            raise OperationsError("backup record fields are invalid")
        if record["schema"] != BACKUP_RECORD_SCHEMA:
            raise OperationsError("backup record schema is invalid")
        name = _safe_backup_name(record["snapshot_name"])
        digest = record["manifest_sha256"]
        if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
            raise OperationsError("backup record digest is invalid")
        completed = _parse_utc(record["completed_at_utc"], "backup completion")
        age = (now - completed).total_seconds()
        if age < 0:
            issues.append("backup_record_future")
        if age > BACKUP_MAX_AGE_SECONDS:
            issues.append("backup_record_stale")
        _private_existing_dir(backup_root)
        snapshot = backup_root / name
        if snapshot.parent != backup_root or snapshot.is_symlink() or not snapshot.is_dir():
            raise OperationsError("backup snapshot path is unsafe")
        if stat.S_IMODE(snapshot.stat().st_mode) != 0o700 or snapshot.stat().st_uid != os.getuid():
            raise OperationsError("backup snapshot directory is unsafe")
        manifest_path = snapshot / "manifest.json"
        marker_path = snapshot / "COMPLETE"
        _private_file(manifest_path)
        _private_file(marker_path)
        manifest_bytes = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "backup manifest")
        try:
            manifest = market_backup.validate_manifest(market_backup.strict_json(manifest_bytes))
        except market_backup.BackupError as exc:
            raise OperationsError("backup manifest is invalid") from exc
        if market_backup.canonical_json(manifest) != manifest_bytes:
            raise OperationsError("backup manifest is not canonical")
        created = _parse_utc(manifest.get("created_at_utc"), "manifest creation")
        if completed < created or completed > created + dt.timedelta(seconds=BACKUP_MAX_AGE_SECONDS):
            raise OperationsError("backup completion is not bound to manifest creation")
        for key in ("database_dump", "raw_archive"):
            artifact = manifest.get(key)
            if not isinstance(artifact, dict):
                raise OperationsError("backup artifact metadata is invalid")
            relative = artifact.get("path")
            size = artifact.get("size_bytes")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise OperationsError("backup artifact metadata is invalid")
            artifact_path = snapshot / relative
            _private_file(artifact_path)
            if artifact_path.stat().st_size != size:
                raise OperationsError("backup artifact size does not match manifest")
        actual_digest = hashlib.sha256(manifest_bytes).hexdigest()
        marker = _read_bounded(marker_path, MAX_MARKER_BYTES, "backup completion marker")
        if marker != f"{market_backup.MARKER_PREFIX}{actual_digest}\n".encode("ascii"):
            raise OperationsError("backup completion marker is invalid")
        if actual_digest != digest or manifest.get("backup_id") != name:
            raise OperationsError("backup manifest digest does not match record")
        return {"snapshot_name": name, "manifest_sha256": digest, "age_seconds": max(0.0, age)}, issues
    except (OperationsError, market_backup.BackupError, OSError):
        return {}, ["backup_record_invalid"]


def _upload_facts(
    status: dict[str, Any],
    upload_path: Path,
    now: dt.datetime,
    uploads_fn: Callable[[Path], dict[str, int]],
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        uploads = uploads_fn(upload_path)
        if not isinstance(uploads, dict) or any(
            not isinstance(uid, str)
            or not uid
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
            for uid, timestamp in uploads.items()
        ):
            raise OperationsError("uploads are malformed")
    except (OperationsError, evidence.EvidenceError):
        return {"last_uid": status["lastUid"], "last_uid_age_seconds": None, "latest_age_seconds": None}, [
            "uploads_invalid"
        ]
    except Exception:
        return {"last_uid": status["lastUid"], "last_uid_age_seconds": None, "latest_age_seconds": None}, [
            "uploads_unavailable"
        ]
    future = [timestamp for timestamp in uploads.values() if timestamp > now.timestamp()]
    if future:
        issues.append("uploads_future")
    valid = {uid: timestamp for uid, timestamp in uploads.items() if timestamp <= now.timestamp()}
    latest = max(valid.values(), default=None)
    latest_age = None if latest is None else max(0.0, now.timestamp() - latest)
    last_uid = status["lastUid"]
    last_uid_age = None
    if last_uid and last_uid in valid:
        last_uid_age = max(0.0, now.timestamp() - valid[last_uid])
    result = {"last_uid": last_uid, "last_uid_age_seconds": last_uid_age, "latest_age_seconds": latest_age}
    if status["state"] in SETTLED_STATES and not last_uid:
        issues.append("current_uid_missing")
    if status["state"] in SETTLED_STATES and last_uid and last_uid not in valid:
        issues.append("current_uid_not_uploaded")
    if (
        status["state"] in SETTLED_STATES
        and last_uid_age is not None
        and last_uid_age > UPLOAD_MAX_AGE_SECONDS
    ):
        issues.append("current_uid_stale")
    if status["state"] not in GATED_STATES and status["state"] not in SAFE_COMMAND_STATES:
        if latest_age is None:
            issues.append("uploads_unknown")
        elif latest_age > UPLOAD_MAX_AGE_SECONDS:
            issues.append("uploads_stale")
    return result, issues


def _classify(
    status: dict[str, Any],
    service_issues: list[str],
    upload_issues: list[str],
    backup_issues: list[str],
    now: dt.datetime,
) -> tuple[str, list[str]]:
    issues = list(service_issues) + list(upload_issues) + list(backup_issues)
    state = status["state"]
    operation = status["calibrationOperation"]
    pending = status["pending"]
    if pending.get("blocked"):
        issues.append("pending_queue_blocked")
    if operation.get("state") == "error" or operation.get("error"):
        issues.append("calibration_operation_error")
    if state in {"BLOCKED", "LOGIN_REQUIRED"}:
        issues.append(f"state_{state.lower()}")
    deadline = status["deadlineAt"]
    if state in DEADLINE_STATES and deadline <= now.timestamp():
        issues.append("active_deadline_expired")
    elif state not in GATED_STATES and deadline > 0 and deadline <= now.timestamp():
        # Non-gated states may legitimately have no deadline, but a published
        # non-zero deadline is authoritative wherever the controller emits it.
        issues.append("active_deadline_expired")
    if state == "WAIT_DUE":
        due = status["dueAt"]
        if due <= 0:
            issues.append("due_time_missing")
        elif now.timestamp() - due > WAIT_DUE_GRACE_SECONDS:
            issues.append("due_time_overdue")
    expected_kind = {"NATIVE_CALIBRATION": "native", "INPUT_CALIBRATION": "input"}.get(state)
    operation_state = operation.get("state", "")
    operation_kind = operation.get("kind", "")
    paused_input_sample = state == "PAUSED" and operation_state == "running" and operation_kind == "input"
    if (
        operation_state in {"starting", "running"}
        and (expected_kind is None or operation_kind != expected_kind)
        and not paused_input_sample
    ):
        issues.append("calibration_operation_mismatch")
    if state not in GATED_STATES and (not status["nativeCalibration"] or not status["inputCalibration"]):
        issues.append("calibration_not_current")
    if len(issues) > MAX_ISSUES:
        issues = issues[:MAX_ISSUES]
    if state in GATED_STATES and not issues:
        return "gated", []
    if issues:
        return "attention", issues
    return "operational", []


def health_operation(
    *,
    config_path: Path | None = None,
    state_root: Path,
    backup_root: Path,
    companion_state_path: Path | None = None,
    now: Callable[[], dt.datetime] = _utc_now,
    load_config_fn: Callable[[Path], ClientConfig] = load_client_config,
    socket_fn: Callable[..., dict[str, Any]] = _socket_request,
    service_fn: Callable[[], dict[str, dict[str, str]]] = _service_status,
    uploads_fn: Callable[[Path], dict[str, int]] = evidence.read_uploads,
) -> dict[str, Any]:
    _private_dir(state_root)
    current = now()
    issues: list[str] = []
    config: ClientConfig | None = None
    status: dict[str, Any] | None = None
    services: dict[str, dict[str, str]] = {}
    if config_path is None or companion_state_path is None:
        try:
            defaults = operation_paths()
        except OperationsError:
            defaults = None
            issues.append("xdg_paths_unavailable")
        if defaults is not None:
            if config_path is None:
                config_path = defaults.config_path
            if companion_state_path is None:
                companion_state_path = defaults.companion_state
    try:
        if config_path is None:
            raise OperationsError("session configuration path is unavailable")
        resolved_config = config_path.resolve(strict=True)
        if not resolved_config.is_file():
            raise OperationsError("config path is not a file")
        config = load_config_fn(resolved_config)
    except Exception:
        issues.append("config_unavailable")
    if config is not None:
        try:
            response = socket_fn(config.socket, {"command": "status"}, timeout=SERVICE_TIMEOUT_SECONDS)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise OperationsError("IPC status refused")
            status = _bounded_status(response.get("status"))
        except Exception:
            issues.append("ipc_unavailable")
    try:
        services = service_fn()
        service_issues = _service_issues(services)
    except Exception:
        service_issues = ["services_unavailable"]
    backup_facts, backup_issues = _backup_record(backup_root, state_root, current)
    if status is None:
        upload_facts = {"last_uid": None, "last_uid_age_seconds": None, "latest_age_seconds": None}
        upload_issues: list[str] = []
        classification = "attention"
        class_issues = []
        issues.extend(service_issues)
        issues.extend(backup_issues)
    else:
        if companion_state_path is None:
            upload_facts = {"last_uid": None, "last_uid_age_seconds": None, "latest_age_seconds": None}
            upload_issues = ["companion_state_unavailable"]
        else:
            upload_facts, upload_issues = _upload_facts(
                status,
                companion_state_path,
                current,
                uploads_fn,
            )
        classification, class_issues = _classify(
            status, service_issues, upload_issues, backup_issues, current
        )
    issues.extend(class_issues)
    if issues:
        classification = "attention"
    result: dict[str, Any] = {
        "schema": HEALTH_SCHEMA,
        "generated_at_utc": _utc_text(current),
        "classification": classification,
        "operational": classification == "operational",
        "summary": classification.upper(),
        "issues": sorted(set(issues))[:MAX_ISSUES],
        "status": None
        if status is None
        else {
            key: status[key]
            for key in (
                "state",
                "mode",
                "generation",
                "nativeCalibration",
                "inputCalibration",
                "lastUid",
                "blocker",
                "actionIntent",
                "deadlineAt",
                "dueAt",
                "calibrationOperation",
                "pending",
                "nativeResourceAssessment",
            )
        },
        "services": services,
        "uploads": upload_facts,
        "backup": backup_facts,
    }
    try:
        journal = state_root / "health-journal.jsonl"
        lines: list[bytes] = []
        if journal.exists():
            _private_file(journal)
            lines = _read_bounded(journal, MAX_JOURNAL_BYTES, "health journal").splitlines()[
                -(MAX_JOURNAL_RECORDS - 1) :
            ]
        lines.append(
            _canonical_json(
                {
                    "generated_at_utc": result["generated_at_utc"],
                    "classification": classification,
                    "operational": result["operational"],
                    "issues": result["issues"],
                }
            )
        )
        journal_bytes = b"\n".join(lines) + b"\n"
        if len(journal_bytes) > MAX_JOURNAL_BYTES:
            raise OperationsError("health journal is too large")
        # Commit the journal first.  A failure before latest-health is
        # published leaves the previous latest record intact.
        _atomic_bytes(journal, journal_bytes)
        _atomic_bytes(state_root / "latest-health.json", _canonical_json(result))
    except Exception:
        result["classification"] = "attention"
        result["operational"] = False
        result["summary"] = "ATTENTION"
        result["issues"] = sorted(set([*result["issues"], "health_record_write_failed"]))[:MAX_ISSUES]
        try:
            _atomic_bytes(state_root / "latest-health.json", _canonical_json(result))
        except Exception:
            pass
    result["exit_code"] = 0 if result["classification"] == "gated" or result["operational"] else 2
    return result
