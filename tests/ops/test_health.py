"""Fixture-only verification for the unattended host operations."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.ops import health as ops
from tests.support.companion_state import write_companion_state

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
GOOD_DIGEST = "a" * 64
BACKUP_NAME = "beta-20261019T200000Z-012345abcdef"


def test_backup_id_validator_is_shared_with_market_backup() -> None:
    assert ops.BACKUP_ID_RE is ops.market_backup.BACKUP_ID_RE
    assert ops._safe_backup_name("crowley-20261107T061327Z-012345abcdef").startswith("crowley-")
    with pytest.raises(ops.OperationsError, match="invalid backup name"):
        ops._safe_backup_name("other-20261107T061327Z-012345abcdef")


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "session.toml"
    path.write_text("[profile]\n", encoding="utf-8")
    return path


def _snapshot_functions(counter: list[int] | None = None):
    def snapshot(root: Path) -> Path:
        path = root / BACKUP_NAME
        path.mkdir(mode=0o700, exist_ok=True)
        manifest_bytes = ops.market_backup.canonical_json(_health_manifest())
        (path / "manifest.json").write_bytes(manifest_bytes)
        (path / "database.dump").write_bytes(b"")
        (path / "raw.tar").write_bytes(b"")
        for item in ("manifest.json", "database.dump", "raw.tar"):
            os.chmod(path / item, 0o600)
        (path / "COMPLETE").write_bytes(
            f"{ops.market_backup.MARKER_PREFIX}{ops.market_backup.sha256_bytes(manifest_bytes)}\n".encode(
                "ascii"
            )
        )
        os.chmod(path / "COMPLETE", 0o600)
        return path

    def validate(path: Path):
        if counter is not None:
            counter[0] += 1
        return {"backup_id": path.name}, GOOD_DIGEST

    return snapshot, validate


def _health_manifest(*, created_at_utc: str = "2026-10-19T20:00:00Z") -> dict[str, object]:
    return {
        "schema": ops.market_backup.SCHEMA,
        "schema_version": 1,
        "backup_id": BACKUP_NAME,
        "created_at_utc": created_at_utc,
        "source": {
            "compose_file_sha256": "b" * 64,
            "compose_project": "fixture",
            "services": {"database": "db", "server": "server"},
            "containers": {
                "database": {"id": "db-id", "image_id": "db-image", "repo_digests": []},
                "server": {"id": "server-id", "image_id": "server-image", "repo_digests": []},
            },
            "volumes": {
                "database": {"name": "db-volume", "mountpoint": "/db"},
                "raw_archive": {"name": "raw-volume", "mountpoint": "/raw"},
            },
            "networks": [],
            "source_commit": "fixture-commit",
            "dirty_tree_sha256": "c" * 64,
            "migrations": [{"path": "src/crowley/server/migrations/001_fixture.sql", "sha256": "d" * 64}],
            "postgres_server_version": "fixture-postgres",
            "timescaledb_extension_version": "fixture-timescaledb",
        },
        "database_dump": {
            "path": "database.dump",
            "format": "postgres-custom",
            "size_bytes": 0,
            "sha256": "e" * 64,
        },
        "raw_archive": {
            "path": "raw.tar",
            "format": "tar",
            "size_bytes": 0,
            "sha256": "f" * 64,
            "members": [],
        },
    }


def _install_last_good(
    state_root: Path,
    backup_root: Path,
    *,
    completed: datetime = NOW,
    manifest: dict[str, object] | None = None,
) -> None:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = backup_root / BACKUP_NAME
    snapshot.mkdir(mode=0o700, exist_ok=True)
    manifest_value = manifest or _health_manifest(created_at_utc=completed.isoformat().replace("+00:00", "Z"))
    manifest_bytes = ops.market_backup.canonical_json(manifest_value)
    (snapshot / "manifest.json").write_bytes(manifest_bytes)
    (snapshot / "database.dump").write_bytes(b"")
    (snapshot / "raw.tar").write_bytes(b"")
    os.chmod(snapshot / "database.dump", 0o600)
    os.chmod(snapshot / "raw.tar", 0o600)
    (snapshot / "COMPLETE").write_bytes(
        f"{ops.market_backup.MARKER_PREFIX}{ops.market_backup.sha256_bytes(manifest_bytes)}\n".encode("ascii")
    )
    os.chmod(snapshot / "manifest.json", 0o600)
    os.chmod(snapshot / "COMPLETE", 0o600)
    digest = ops.market_backup.sha256_bytes(manifest_bytes)
    (state_root / "last-good-backup.json").write_text(
        json.dumps(
            {
                "schema": ops.BACKUP_RECORD_SCHEMA,
                "snapshot_name": BACKUP_NAME,
                "manifest_sha256": digest,
                "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(state_root / "last-good-backup.json", 0o600)


def _good_status(**changes: object) -> dict[str, object]:
    status: dict[str, object] = {
        "state": "WAIT_DUE",
        "mode": "repeat",
        "generation": 9,
        "nativeCalibration": True,
        "inputCalibration": True,
        "lastUid": "uid-current",
        "blocker": "",
        "actionIntent": "",
        "deadlineAt": 0,
        "dueAt": int((NOW + timedelta(seconds=120)).timestamp()),
        "calibrationOperation": {"kind": "", "state": "idle", "error": ""},
        "nativeResourceAssessment": {
            "classification": "within_targets",
            "method": "linux_proc_status_vmhwm_reset5",
            "evidenceHash": "1" * 64,
            "construction": {"growthBytes": 1024, "targetBytes": 128 * 1024 * 1024, "withinTarget": True},
            "updates": {"growthBytes": 1024, "targetBytes": 16 * 1024 * 1024, "withinTarget": True},
            "current": True,
        },
    }
    status.update(changes)
    return status


def _healthy_services() -> dict[str, dict[str, str]]:
    return {
        name: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "Result": "success",
            "ExecMainStatus": "0",
        }
        for name in ops.SERVICE_NAMES
    }


def _health(
    tmp_path: Path,
    *,
    status: dict[str, object] | None = None,
    uploads: dict[str, int] | None = None,
    services: dict[str, dict[str, str]] | None = None,
    socket_error: Exception | None = None,
    manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    state_root = tmp_path / "state"
    backup_root = tmp_path / "backups"
    _install_last_good(state_root, backup_root, manifest=manifest)
    config = _config(tmp_path)
    companion = tmp_path / "companion.json"
    write_companion_state(companion)
    config_object = SimpleNamespace(socket=tmp_path / "session.sock", companion_state=companion)

    def socket_fn(*_args, **_kwargs):
        if socket_error is not None:
            raise socket_error
        return {"ok": True, "status": status or _good_status()}

    return ops.health_operation(
        config_path=config,
        state_root=state_root,
        backup_root=backup_root,
        companion_state_path=companion,
        now=lambda: NOW,
        load_config_fn=lambda _path: config_object,
        socket_fn=socket_fn,
        service_fn=lambda: services if services is not None else _healthy_services(),
        uploads_fn=lambda _path: (
            uploads if uploads is not None else {"uid-current": int(NOW.timestamp()) - 30}
        ),
    )


def test_backup_success_revalidates_and_atomically_records_last_good(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    counter = [0]
    snapshot, validate = _snapshot_functions(counter)
    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=snapshot,
        validate_fn=validate,
        now=lambda: NOW,
    )
    assert result["outcome"] == "complete"
    assert counter == [1]
    record = json.loads((state_root / "last-good-backup.json").read_text(encoding="utf-8"))
    assert record["snapshot_name"] == BACKUP_NAME
    assert record["manifest_sha256"] == GOOD_DIGEST
    assert stat.S_IMODE((state_root / "last-good-backup.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700


def test_backup_failure_preserves_previous_last_good_and_uses_bounded_category(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    _install_last_good(state_root, backup_root)
    prior = (state_root / "last-good-backup.json").read_bytes()

    def fail(_root: Path) -> Path:
        raise RuntimeError("secret database output must not escape")

    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=fail,
        now=lambda: NOW,
    )
    assert result["exit_code"] == 2
    assert result["receipt"]["error_category"] == "operation_failed"
    assert (state_root / "last-good-backup.json").read_bytes() == prior
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("validation_failure", ["raised", "mismatched"])
def test_backup_validation_failure_preserves_previous_last_good(
    tmp_path: Path, validation_failure: str
) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    _install_last_good(state_root, backup_root)
    prior = (state_root / "last-good-backup.json").read_bytes()
    snapshot, _valid = _snapshot_functions()

    def validate(path: Path):
        if validation_failure == "raised":
            raise ops.market_backup.BackupError("invalid candidate")
        return {"backup_id": BACKUP_NAME[:-1] + "0"}, "b" * 64

    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=snapshot,
        validate_fn=validate,
        now=lambda: NOW,
    )
    assert result["outcome"] == "failed"
    assert (state_root / "last-good-backup.json").read_bytes() == prior
    if validation_failure == "raised":
        assert result["receipt"]["error_category"] == "snapshot_or_validation_failed"
    else:
        assert result["receipt"]["error_category"] == "local_io_or_validation_failed"


def test_large_schema_valid_manifest_passes_backup_admission_and_health(tmp_path: Path) -> None:
    large_manifest = _health_manifest()
    large_manifest["raw_archive"] = {
        **large_manifest["raw_archive"],
        "members": [
            {
                "path": f"objects/{index:04d}/" + "x" * 90 + ".dat",
                "size_bytes": index,
                "sha256": "f" * 64,
            }
            for index in range(2000)
        ],
    }
    manifest_bytes = ops.market_backup.canonical_json(large_manifest)
    assert len(manifest_bytes) > 256 * 1024

    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    snapshot, _unused = _snapshot_functions()

    def large_snapshot(root: Path) -> Path:
        path = snapshot(root)
        (path / "manifest.json").write_bytes(manifest_bytes)
        os.chmod(path / "manifest.json", 0o600)
        (path / "COMPLETE").write_bytes(
            f"{ops.market_backup.MARKER_PREFIX}{ops.market_backup.sha256_bytes(manifest_bytes)}\n".encode(
                "ascii"
            )
        )
        os.chmod(path / "COMPLETE", 0o600)
        return path

    def validate(path: Path):
        value = ops.market_backup.validate_manifest(
            json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        )
        return value, ops.market_backup.sha256_bytes(manifest_bytes)

    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=large_snapshot,
        validate_fn=validate,
        now=lambda: NOW,
    )
    assert result["outcome"] == "complete"
    health = _health(tmp_path, manifest=large_manifest)
    assert health["classification"] == "operational"
    assert health["issues"] == []


def test_backup_missing_config_writes_bounded_failure_receipt(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=lambda _root: pytest.fail("missing config must fail before snapshot"),
        now=lambda: NOW,
    )
    assert result["exit_code"] == 2
    receipts = list((state_root / "receipts").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["outcome"] == "failed"


def test_completed_backup_survives_receipt_directory_failure(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    state_root.mkdir()
    (state_root / "receipts").write_text("not a directory", encoding="utf-8")
    snapshot, validate = _snapshot_functions()
    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=snapshot,
        validate_fn=validate,
        now=lambda: NOW,
    )
    assert result["outcome"] == "complete"
    assert (state_root / "last-good-backup.json").exists()


def _hold_lock(path: str, ready: multiprocessing.synchronize.Event) -> None:
    with ops._process_lock(Path(path)):
        ready.set()
        time.sleep(0.8)


def test_backup_uses_real_nonblocking_process_lock(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(str(state_root / "backup.lock"), ready))
    process.start()
    assert ready.wait(2)
    snapshot, _validate = _snapshot_functions()
    result = ops.backup_operation(
        state_root=state_root,
        backup_root=backup_root,
        snapshot_fn=snapshot,
        now=lambda: NOW,
    )
    process.join(3)
    assert not process.is_alive()
    assert result["outcome"] == "busy"
    assert not (state_root / "last-good-backup.json").exists()


def test_health_operational_binds_exact_current_uid_and_writes_bounded_records(tmp_path: Path) -> None:
    result = _health(tmp_path)
    assert result["classification"] == "operational"
    assert result["operational"] is True
    assert result["issues"] == []
    health = json.loads((tmp_path / "state" / "latest-health.json").read_text(encoding="utf-8"))
    assert health["status"]["lastUid"] == "uid-current"
    assert len((tmp_path / "state" / "health-journal.jsonl").read_bytes()) < 4096


def test_health_initial_idle_calibration_is_gated_not_attention(tmp_path: Path) -> None:
    result = _health(
        tmp_path,
        status=_good_status(
            state="NATIVE_CALIBRATION",
            nativeCalibration=False,
            inputCalibration=False,
            lastUid="",
            calibrationOperation={"kind": "native", "state": "idle", "error": ""},
        ),
        uploads={},
    )
    assert result["classification"] == "gated"
    assert result["operational"] is False
    assert result["exit_code"] == 0


def test_bounded_status_requires_typed_calibration_operation_and_truncates_diagnostics() -> None:
    for operation in (None, {}, {"kind": "native", "state": "idle"}):
        with pytest.raises(ops.OperationsError, match="calibration operation"):
            ops._bounded_status(_good_status(calibrationOperation=operation))
    bounded = ops._bounded_status(
        _good_status(
            blocker="b" * 500,
            calibrationOperation={"kind": "native", "state": "error", "error": "e" * 500},
        )
    )
    assert len(bounded["blocker"]) == ops.MAX_ERROR_LENGTH
    assert len(bounded["calibrationOperation"]["error"]) == ops.MAX_ERROR_LENGTH


def test_native_resource_assessment_accepts_not_measured_and_rejects_inconsistent_facts() -> None:
    not_measured = _good_status(
        nativeResourceAssessment={
            "classification": "not_measured",
            "method": None,
            "evidenceHash": None,
            "current": False,
            "construction": None,
            "updates": None,
        }
    )
    bounded = ops._bounded_status(not_measured)
    bounded_assessment = bounded["nativeResourceAssessment"]
    assert set(bounded_assessment) == {
        "classification",
        "method",
        "evidenceHash",
        "current",
        "construction",
        "updates",
    }
    assert bounded_assessment == {
        "classification": "not_measured",
        "method": None,
        "evidenceHash": None,
        "current": False,
        "construction": None,
        "updates": None,
    }
    exceeded = _good_status(
        nativeResourceAssessment={
            "classification": "target_exceeded",
            "method": "linux_proc_status_vmhwm_reset5",
            "evidenceHash": "2" * 64,
            "construction": {
                "growthBytes": 128 * 1024 * 1024 + 1,
                "targetBytes": 128 * 1024 * 1024,
                "withinTarget": False,
            },
            "updates": {"growthBytes": 1, "targetBytes": 16 * 1024 * 1024, "withinTarget": True},
            "current": False,
        }
    )
    assert ops._bounded_status(exceeded)["nativeResourceAssessment"]["classification"] == "target_exceeded"
    with pytest.raises(ops.OperationsError, match="current"):
        ops._bounded_status(
            _good_status(
                nativeResourceAssessment={
                    "classification": "not_measured",
                    "method": None,
                    "evidenceHash": None,
                    "current": True,
                    "construction": None,
                    "updates": None,
                }
            )
        )
    contradictory = _good_status(
        nativeResourceAssessment={
            "classification": "target_exceeded",
            "method": "linux_proc_status_vmhwm_reset5",
            "evidenceHash": "3" * 64,
            "construction": {"growthBytes": 128, "targetBytes": 128, "withinTarget": False},
            "updates": {"growthBytes": 1, "targetBytes": 16, "withinTarget": True},
            "current": True,
        }
    )
    with pytest.raises(ops.OperationsError, match="not derived"):
        ops._bounded_status(contradictory)
    omitted = dict(exceeded["nativeResourceAssessment"])
    omitted.pop("updates")
    with pytest.raises(ops.OperationsError, match="incomplete"):
        ops._bounded_status(_good_status(nativeResourceAssessment=omitted))


def test_health_future_and_unrelated_upload_do_not_cover_current_uid(tmp_path: Path) -> None:
    result = _health(
        tmp_path,
        uploads={"uid-other": int(NOW.timestamp()) + 10},
    )
    assert result["classification"] == "attention"
    assert "uploads_future" in result["issues"]
    assert "current_uid_not_uploaded" in result["issues"]


@pytest.mark.parametrize("state", ["ACK_RELOAD_SENT", "ACK_RELOAD_WAIT"])
@pytest.mark.parametrize(
    ("last_uid", "uploads", "expected"),
    [
        ("", {}, "current_uid_missing"),
        ("uid-current", {"uid-other": int(NOW.timestamp()) - 1}, "current_uid_not_uploaded"),
        ("uid-current", {"uid-current": int(NOW.timestamp()) + 1}, "current_uid_not_uploaded"),
        ("uid-current", {"uid-current": int(NOW.timestamp()) - 1}, None),
    ],
)
def test_ack_states_bind_exact_current_upload_uid(
    state: str, last_uid: str, uploads: dict[str, int], expected: str | None
) -> None:
    status = ops._bounded_status(_good_status(state=state, lastUid=last_uid))
    _facts, issues = ops._upload_facts(status, Path("/private/uploads"), NOW, lambda _path: uploads)
    if expected is None:
        assert not {"current_uid_missing", "current_uid_not_uploaded"} & set(issues)
    else:
        assert expected in issues


def test_pending_queue_preserves_real_bounded_facts_and_blocks_health() -> None:
    status = ops._bounded_status(
        _good_status(
            state="SCANNING",
            deadlineAt=int((NOW + timedelta(seconds=60)).timestamp()),
            pending={"ids": ["uid-pending"], "count": 1, "cap": 6, "blocked": True, "reason": "plan_invalid"},
        )
    )
    assert status["pending"] == {
        "count": 1,
        "cap": 6,
        "blocked": True,
        "reason": "plan_invalid",
        "ids_count": 1,
    }
    classification, issues = ops._classify(status, [], [], [], NOW)
    assert classification == "attention"
    assert "pending_queue_blocked" in issues


def test_upload_errors_are_attention_even_when_gated_and_bind_exact_uid_age() -> None:
    gated = ops._bounded_status(
        _good_status(
            state="PAUSED",
            lastUid="",
            nativeCalibration=False,
            inputCalibration=False,
            nativeResourceAssessment={
                "classification": "not_measured",
                "method": None,
                "evidenceHash": None,
                "current": False,
                "construction": None,
                "updates": None,
            },
        )
    )

    def unavailable(_path: Path) -> dict[str, int]:
        raise OSError("unreadable")

    facts, issues = ops._upload_facts(gated, Path("/private/uploads"), NOW, unavailable)
    assert facts["latest_age_seconds"] is None
    assert issues == ["uploads_unavailable"]
    settled = ops._bounded_status(_good_status())
    facts, issues = ops._upload_facts(
        settled,
        Path("/private/uploads"),
        NOW,
        lambda _path: {
            "uid-current": int(NOW.timestamp()) - (ops.UPLOAD_MAX_AGE_SECONDS + 1),
            "uid-other": int(NOW.timestamp()) - 2,
        },
    )
    assert facts["last_uid_age_seconds"] == ops.UPLOAD_MAX_AGE_SECONDS + 1
    assert facts["latest_age_seconds"] == 2
    assert "current_uid_stale" in issues
    assert "uploads_stale" not in issues


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (NOW - timedelta(seconds=10), []),
        (NOW - timedelta(seconds=75 * 60 + 1), ["backup_record_stale"]),
        (NOW + timedelta(seconds=1), ["backup_record_future"]),
    ],
)
def test_backup_record_freshness_rejects_stale_and_future_timestamps(
    tmp_path: Path, completed: datetime, expected: list[str]
) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    _install_last_good(state_root, backup_root, completed=completed)
    _facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == expected


def test_backup_health_check_is_bounded_read_only_and_checks_completion_marker(
    tmp_path: Path, monkeypatch
) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    _install_last_good(state_root, backup_root)
    before_mode = stat.S_IMODE(backup_root.stat().st_mode)

    def forbidden(_path: Path):
        raise AssertionError("health must not run full archive validation")

    monkeypatch.setattr(ops.market_backup, "validate_snapshot", forbidden)
    facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == []
    assert facts["snapshot_name"] == BACKUP_NAME
    assert stat.S_IMODE(backup_root.stat().st_mode) == before_mode == 0o700
    (backup_root / BACKUP_NAME / "COMPLETE").write_bytes(b"tampered\n")
    _facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == ["backup_record_invalid"]


def test_backup_health_checks_manifest_timestamps_artifact_sizes_and_read_caps(tmp_path: Path) -> None:
    state_root, backup_root = tmp_path / "state", tmp_path / "backups"
    _install_last_good(state_root, backup_root)
    (backup_root / BACKUP_NAME / "database.dump").write_bytes(b"tampered-size")
    _facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == ["backup_record_invalid"]

    record = state_root / "last-good-backup.json"
    record.write_bytes(b"{" + b"x" * ops.MAX_BACKUP_RECORD_BYTES + b"}")
    _facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == ["backup_record_invalid"]


def test_missing_backup_root_is_not_created_by_health_validation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    (state_root / "last-good-backup.json").write_text(
        json.dumps(
            {
                "schema": ops.BACKUP_RECORD_SCHEMA,
                "snapshot_name": BACKUP_NAME,
                "manifest_sha256": GOOD_DIGEST,
                "completed_at_utc": "2026-10-19T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(state_root / "last-good-backup.json", 0o600)
    backup_root = tmp_path / "missing-backups"
    _facts, issues = ops._backup_record(backup_root, state_root, NOW)
    assert issues == ["backup_record_invalid"]
    assert not backup_root.exists()


@pytest.mark.parametrize("state", ["READY", "ARMED", "RECONCILE"])
def test_pre_first_scan_states_do_not_require_deadlines_or_uid(state: str) -> None:
    status = _good_status(state=state, lastUid="", deadlineAt=0)
    bounded = ops._bounded_status(status)
    classification, issues = ops._classify(bounded, [], [], [], NOW)
    assert classification == "operational"
    assert "current_uid_missing" not in issues
    assert "active_deadline_expired" not in issues


def test_qualification_state_requires_exact_current_uid() -> None:
    status = ops._bounded_status(_good_status(state="QUALIFY_PENDING", lastUid=""))
    _facts, issues = ops._upload_facts(status, Path("/private/uploads"), NOW, lambda _path: {})
    assert "current_uid_missing" in issues


def test_running_calibration_kind_must_match_state_but_initial_idle_is_valid() -> None:
    idle = ops._bounded_status(
        _good_status(
            state="NATIVE_CALIBRATION",
            nativeCalibration=False,
            inputCalibration=False,
            lastUid="",
            calibrationOperation={"kind": "native", "state": "idle", "error": ""},
            nativeResourceAssessment={
                "classification": "not_measured",
                "method": None,
                "evidenceHash": None,
                "current": False,
                "construction": None,
                "updates": None,
            },
        )
    )
    assert ops._classify(idle, [], [], [], NOW) == ("gated", [])
    running_wrong = ops._bounded_status(
        _good_status(
            state="NATIVE_CALIBRATION",
            nativeCalibration=False,
            inputCalibration=False,
            lastUid="",
            calibrationOperation={"kind": "input", "state": "running", "error": ""},
        )
    )
    classification, issues = ops._classify(running_wrong, [], [], [], NOW)
    assert classification == "attention"
    assert "calibration_operation_mismatch" in issues


def test_calibration_status_sequence_allows_completed_native_and_paused_input_sample() -> None:
    input_gate = ops._bounded_status(
        _good_status(
            state="INPUT_CALIBRATION",
            nativeCalibration=True,
            inputCalibration=False,
            calibrationOperation={"kind": "native", "state": "complete", "error": ""},
        )
    )
    assert ops._classify(input_gate, [], [], [], NOW) == ("gated", [])
    paused_input = ops._bounded_status(
        _good_status(
            state="PAUSED",
            nativeCalibration=True,
            inputCalibration=False,
            calibrationOperation={"kind": "input", "state": "running", "error": ""},
        )
    )
    assert ops._classify(paused_input, [], [], [], NOW) == ("gated", [])


def test_terminal_other_calibration_kind_is_history_but_active_kind_must_match() -> None:
    stale_complete = ops._bounded_status(
        _good_status(
            state="NATIVE_CALIBRATION",
            nativeCalibration=False,
            inputCalibration=False,
            lastUid="",
            calibrationOperation={"kind": "input", "state": "complete", "error": ""},
        )
    )
    classification, issues = ops._classify(stale_complete, [], [], [], NOW)
    assert classification == "gated"
    assert "calibration_operation_mismatch" not in issues
    reopen = ops._bounded_status(
        _good_status(state="REOPEN", deadlineAt=int(NOW.timestamp()) - 1, actionIntent="")
    )
    classification, issues = ops._classify(reopen, [], [], [], NOW)
    assert classification == "attention"
    assert "active_deadline_expired" in issues


def test_health_rejects_malformed_ipc_status_without_retaining_payload(tmp_path: Path) -> None:
    result = _health(tmp_path, status={"state": "READY", "secret": "must not persist"})
    assert result["classification"] == "attention"
    assert "ipc_unavailable" in result["issues"]
    assert "secret" not in json.dumps(result)


def test_health_missing_config_persists_null_status_and_independent_issues(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    result = ops.health_operation(
        config_path=tmp_path / "missing-session.toml",
        state_root=state_root,
        backup_root=tmp_path / "missing-backups",
        now=lambda: NOW,
        service_fn=_healthy_services,
    )
    assert result["classification"] == "attention"
    assert result["status"] is None
    assert "config_unavailable" in result["issues"]
    persisted = json.loads((state_root / "latest-health.json").read_text(encoding="utf-8"))
    assert persisted["status"] is None
    assert "BLOCKED" not in json.dumps(persisted)


def test_health_does_not_require_a_repository_checkout(tmp_path: Path) -> None:
    result = _health(
        tmp_path,
        status=_good_status(
            state="PAUSED",
            nativeCalibration=False,
            inputCalibration=False,
            lastUid="",
            calibrationOperation={"kind": "", "state": "idle", "error": ""},
        ),
        uploads={},
    )
    assert result["classification"] == "gated"
    assert "repo_unavailable" not in result["issues"]


@pytest.mark.parametrize(
    "status_changes,expected",
    [
        ({"state": "BLOCKED"}, "state_blocked"),
        (
            {"state": "SCANNING", "deadlineAt": int(NOW.timestamp()) - 1, "actionIntent": "scan"},
            "active_deadline_expired",
        ),
        ({"state": "WAIT_DUE", "dueAt": int(NOW.timestamp()) - 500}, "due_time_overdue"),
        ({"state": "READY", "nativeCalibration": False}, "calibration_not_current"),
    ],
)
def test_health_classifies_stalls_and_invalid_progress(
    tmp_path: Path, status_changes: dict[str, object], expected: str
) -> None:
    result = _health(tmp_path, status=_good_status(**status_changes))
    assert result["classification"] == "attention"
    assert expected in result["issues"]


def test_health_unavailable_ipc_services_and_tampered_backup_are_attention(tmp_path: Path) -> None:
    result = _health(tmp_path, socket_error=TimeoutError("private socket detail"), services={})
    assert result["classification"] == "attention"
    assert {"ipc_unavailable", "services_unavailable"} <= set(result["issues"])
    assert "private socket" not in json.dumps(result)

    state_root = tmp_path / "state"
    (state_root / "last-good-backup.json").write_text("{}", encoding="utf-8")
    os.chmod(state_root / "last-good-backup.json", 0o600)
    _facts, issues = ops._backup_record(tmp_path / "backups", state_root, NOW)
    assert "backup_record_invalid" in issues


def test_health_journal_failure_cannot_leave_fresh_green_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest_attempts = [0]
    original = ops._atomic_bytes

    def fail_latest_once(path: Path, data: bytes) -> None:
        if path.name == "latest-health.json":
            latest_attempts[0] += 1
            if latest_attempts[0] == 1:
                raise OSError("injected latest write failure")
        original(path, data)

    monkeypatch.setattr(ops, "_atomic_bytes", fail_latest_once)
    result = _health(tmp_path)
    assert result["classification"] == "attention"
    assert "health_record_write_failed" in result["issues"]
    latest = json.loads((tmp_path / "state" / "latest-health.json").read_text(encoding="utf-8"))
    assert latest["classification"] == "attention"
    assert "health_record_write_failed" in latest["issues"]


def test_service_query_is_allowlisted_and_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "\n".join(
        line
        for index, name in enumerate(ops.SERVICE_NAMES)
        for line in [
            "Result=success",
            "ExecMainStatus=0",
            f"Id={name}",
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "" if index == 0 else "",
        ]
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["timeout"] == 5.0
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(ops.subprocess, "run", run)
    result = ops._service_status()
    assert set(result) == set(ops.SERVICE_NAMES)
    assert calls and calls[0][0][:5] == ["systemctl", "--user", "show", *ops.SERVICE_NAMES]
    assert calls[0][1].get("shell") is not True


@pytest.mark.parametrize("missing", [False, True])
def test_service_query_rejects_duplicate_or_missing_unit_blocks(
    monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    block = "\n".join(
        [
            "Result=success",
            "ExecMainStatus=0",
            f"Id={ops.SERVICE_NAMES[0]}",
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
        ]
    )
    second = block.replace(ops.SERVICE_NAMES[0], ops.SERVICE_NAMES[1])
    output = block if missing else f"{block}\n\n{second}\n\n{second}"

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(ops.subprocess, "run", run)
    with pytest.raises(ops.OperationsError, match="incomplete"):
        ops._service_status()


def test_service_query_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ops.subprocess, "run", run)
    with pytest.raises(ops.OperationsError, match="unavailable"):
        ops._service_status()


def test_service_nonzero_exec_main_status_is_attention() -> None:
    services = _healthy_services()
    services[ops.SERVICE_NAMES[0]]["ExecMainStatus"] = "17"
    issues = ops._service_issues(services)
    assert f"{ops.SERVICE_NAMES[0]}:exec_main_status_17" in issues


def test_bounded_status_accepts_actual_pending_shape_only() -> None:
    with pytest.raises(ops.OperationsError, match="pending"):
        ops._bounded_status(
            _good_status(
                pending={"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": "", "uid": "bad"}
            )
        )


def test_systemd_units_are_bounded_and_minute_aligned() -> None:
    backup_service = Path("deploy/systemd/crowley-market-backup.service.in").read_text(encoding="utf-8")
    backup_timer = Path("deploy/systemd/crowley-market-backup.timer").read_text(encoding="utf-8")
    health_service = Path("deploy/systemd/crowley-health.service.in").read_text(encoding="utf-8")
    health_timer = Path("deploy/systemd/crowley-health.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in backup_service and "UMask=0077" in backup_service
    assert "ExecStart=%h/.local/bin/crowley-ops backup" in backup_service
    assert "--repo" not in backup_service
    assert "EnvironmentFile=%E/crowley/secrets.env" in backup_service
    assert "TimeoutStartSec=4500" in backup_service
    assert "OnCalendar=*-*-* *:12:00" in backup_timer and "Persistent=true" in backup_timer
    assert "TimeoutStartSec=30s" in health_service
    assert "ExecStart=%h/.local/bin/crowley-ops health" in health_service
    assert "--repo" not in health_service
    assert "EnvironmentFile=%E/crowley/secrets.env" in health_service
    assert "OnUnitActiveSec=2min" in health_timer and "Persistent=true" not in health_timer
    assert "PrivateTmp=true" not in backup_service + health_service
    assert "After=network-online.target" not in backup_service + health_service
