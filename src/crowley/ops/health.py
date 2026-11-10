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

from crowley.core import companion_state
from crowley.ops import backup as market_backup
from crowley.ops.backup import BACKUP_ID_RE
from crowley.platform.paths import XdgDirs, XdgPathError
from crowley.scan import loop as scan_loop

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
# Three 15-minute cadences.
UPLOAD_MAX_AGE_SECONDS = 45 * 60
# After a human hold ends the loop needs time to resume and complete a sweep.
HOLD_GRACE_SECONDS = 25 * 60
SERVICE_TIMEOUT_SECONDS = 5.0
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# The scan loop rewrites its status at least every 60 s (scan.loop.HEARTBEAT_S).
STATUS_MAX_AGE_SECONDS = 5 * 60
ALLOWED_SERVICE_FIELDS = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus")
SERVICE_NAMES = ("crowley-scan.service", "crowley-companion.service")


class OperationsError(RuntimeError):
    """A fixed, safe-to-report failure category."""


class BusyError(OperationsError):
    """The operation's process-lifetime lock is already held."""


@dataclass(frozen=True, slots=True)
class OperationPaths:
    """The installed-package operational paths resolved from XDG roots."""

    state_root: Path
    backup_root: Path


def _companion_state_path() -> Path:
    """The live companion state file, resolved exactly as the companion resolves it."""

    from crowley.companion.config import state_path

    try:
        return state_path()
    except Exception as exc:
        raise OperationsError("companion state path cannot be resolved") from exc


def operation_paths() -> OperationPaths:
    try:
        dirs = XdgDirs.from_environ(os.environ)
    except XdgPathError as exc:
        raise OperationsError("Crowley XDG paths are unavailable") from exc
    return OperationPaths(
        state_root=dirs.crowley_state / market_backup.OPERATIONS_STATE,
        backup_root=dirs.crowley_state / market_backup.BACKUP_STATE,
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
    """Validate the scan loop's ``scan-status.json`` and keep only bounded facts."""

    if not isinstance(value, dict):
        raise OperationsError("status is not an object")
    result: dict[str, Any] = {}
    for name in ("action", "fault_code", "detail"):
        item = value.get(name)
        if not isinstance(item, str):
            raise OperationsError(f"status {name} is invalid")
        result[name] = item[:MAX_ERROR_LENGTH]
    if not result["action"]:
        raise OperationsError("status action is invalid")
    for name in ("updated_at", "started_at"):
        item = value.get(name)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item <= 0
        ):
            raise OperationsError(f"status {name} is invalid")
        result[name] = float(item)
    ended = value.get("last_hold_ended_at")
    if ended is not None:
        if isinstance(ended, bool) or not isinstance(ended, (int, float)) or not math.isfinite(ended):
            raise OperationsError("status last_hold_ended_at is invalid")
        result["last_hold_ended_at"] = float(ended)
    return result


def _read_scan_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_bounded(path, MAX_BACKUP_RECORD_BYTES, "scan status"))
    except (OSError, ValueError, OperationsError) as exc:
        raise OperationsError("scan status is unreadable") from exc
    return _bounded_status(value)


def _read_uploads(path: Path) -> dict[str, int]:
    """The companion's durable upload map: uid to upload timestamp."""

    try:
        data = _read_bounded(path, MAX_MANIFEST_BYTES, "companion state")
        return dict(companion_state.parse_bytes(data).uploaded)
    except (OSError, OperationsError, companion_state.CompanionStateError) as exc:
        raise OperationsError("companion state is unreadable") from exc


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
    upload_path: Path,
    now: dt.datetime,
    uploads_fn: Callable[[Path], dict[str, int]],
) -> tuple[dict[str, Any], list[str]]:
    """Data freshness: the newest scan the server accepted, from the companion's durable state."""

    try:
        uploads = uploads_fn(upload_path)
    except Exception:
        return {"latest_age_seconds": None}, ["uploads_unavailable"]
    issues: list[str] = []
    stamps = list(uploads.values())
    if any(isinstance(t, bool) or not isinstance(t, int) or t <= 0 for t in stamps):
        return {"latest_age_seconds": None}, ["uploads_invalid"]
    if any(t > now.timestamp() for t in stamps):
        issues.append("uploads_future")
    valid = [t for t in stamps if t <= now.timestamp()]
    if not valid:
        return {"latest_age_seconds": None}, [*issues, "uploads_unknown"]
    age = max(0.0, now.timestamp() - max(valid))
    if age > UPLOAD_MAX_AGE_SECONDS:
        issues.append("uploads_stale")
    return {"latest_age_seconds": age}, issues


def _status_issues(status: dict[str, Any], now: dt.datetime) -> list[str]:
    issues: list[str] = []
    if now.timestamp() - status["updated_at"] > STATUS_MAX_AGE_SECONDS:
        issues.append("scan_status_stale")
    if status["fault_code"]:
        issues.append(f"scan_fault_{status['fault_code'] or 'unknown'}"[:64])
    return issues


def health_operation(
    *,
    state_root: Path,
    backup_root: Path,
    companion_state_path: Path | None = None,
    scan_status_path: Path | None = None,
    now: Callable[[], dt.datetime] = _utc_now,
    service_fn: Callable[[], dict[str, dict[str, str]]] = _service_status,
    uploads_fn: Callable[[Path], dict[str, int]] = _read_uploads,
) -> dict[str, Any]:
    _private_dir(state_root)
    current = now()
    issues: list[str] = []
    status: dict[str, Any] | None = None
    services: dict[str, dict[str, str]] = {}
    if companion_state_path is None:
        companion_state_path = _companion_state_path()
    try:
        status = _read_scan_status(scan_status_path or scan_loop.status_path())
    except Exception:
        issues.append("scan_status_unavailable")
    try:
        services = service_fn()
        service_issues = _service_issues(services)
    except Exception:
        service_issues = ["services_unavailable"]
    backup_facts, backup_issues = _backup_record(backup_root, state_root, current)
    holding = status is not None and status["action"] == "hold_human"
    upload_facts, upload_issues = _upload_facts(companion_state_path, current, uploads_fn)
    recently_held = (
        status is not None
        and "last_hold_ended_at" in status
        and 0 <= current.timestamp() - status["last_hold_ended_at"] < HOLD_GRACE_SECONDS
    )
    if holding or recently_held:
        # A human is at the machine, or just left: no new data is expected yet.
        upload_issues = [i for i in upload_issues if i != "uploads_stale"]
    issues.extend(service_issues)
    issues.extend(upload_issues)
    issues.extend(backup_issues)
    if status is not None:
        issues.extend(_status_issues(status, current))
    issues = sorted(set(issues))[:MAX_ISSUES]
    if issues:
        classification = "attention"
    elif holding:
        classification = "gated"
    else:
        classification = "operational"
    result: dict[str, Any] = {
        "schema": HEALTH_SCHEMA,
        "generated_at_utc": _utc_text(current),
        "classification": classification,
        "operational": classification == "operational",
        "summary": classification.upper(),
        "issues": issues,
        "status": status,
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
