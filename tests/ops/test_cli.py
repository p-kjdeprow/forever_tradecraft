"""Focused tests for the installed Crowley operations CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crowley.ops import backup, cli, health


def _xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> health.OperationPaths:
    config_home = tmp_path / "config"
    state_home = tmp_path / "state"
    runtime = tmp_path / "runtime"
    config_home.mkdir()
    state_home.mkdir()
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return health.operation_paths()


def test_backup_forwards_xdg_paths_and_returns_operation_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _xdg(tmp_path, monkeypatch)
    calls: list[tuple[Path, Path]] = []

    def run(*, state_root: Path, backup_root: Path) -> dict[str, object]:
        calls.append((state_root, backup_root))
        return {"outcome": "complete", "exit_code": 7}

    monkeypatch.setattr(health, "backup_operation", run)

    assert cli.main(["backup"]) == 7
    assert calls == [(paths.state_root, paths.backup_root)]
    assert json.loads(capsys.readouterr().out) == {
        "schema": health.OPS_SCHEMA,
        "operation": "backup",
        "outcome": "complete",
        "exit_code": 7,
    }


def test_health_forwards_xdg_paths_and_returns_operation_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _xdg(tmp_path, monkeypatch)
    calls: list[tuple[Path, Path]] = []

    def run(
        *,
        state_root: Path,
        backup_root: Path,
    ) -> dict[str, object]:
        calls.append((state_root, backup_root))
        return {"classification": "gated", "exit_code": 3}

    monkeypatch.setattr(health, "health_operation", run)

    assert cli.main(["health"]) == 3
    assert calls == [(paths.state_root, paths.backup_root)]
    assert json.loads(capsys.readouterr().out) == {
        "schema": health.OPS_SCHEMA,
        "operation": "health",
        "outcome": "gated",
        "exit_code": 3,
    }


def test_snapshot_uses_xdg_backup_root_without_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _xdg(tmp_path, monkeypatch)
    calls: list[Path] = []

    def run(received_root: Path) -> Path:
        calls.append(received_root)
        return received_root / "crowley-20261107T061327Z-012345abcdef"

    monkeypatch.setattr(backup, "snapshot", run)

    assert cli.main(["snapshot"]) == 0
    assert calls == [paths.backup_root]
    assert capsys.readouterr().out == "snapshot complete: crowley-20261107T061327Z-012345abcdef\n"


def test_drill_uses_xdg_backup_root_without_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _xdg(tmp_path, monkeypatch)
    snapshot = tmp_path / "snapshot"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"result":"restore_verified"}', encoding="utf-8")
    calls: list[tuple[Path, Path]] = []

    def run(received_snapshot: Path, received_root: Path) -> Path:
        calls.append((received_snapshot, received_root))
        return receipt_path

    monkeypatch.setattr(backup, "drill", run)

    assert cli.main(["drill", str(snapshot)]) == 0
    assert calls == [(snapshot, paths.backup_root)]
    assert capsys.readouterr().out == "restore drill restore_verified: receipt.json\n"


def test_checkout_only_repo_option_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["snapshot", "--repo", "."])

    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_compose_file_prints_the_packaged_resource(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["compose-file"]) == 0
    assert Path(capsys.readouterr().out.strip()).name == "docker-compose.yml"


def test_expected_snapshot_failure_is_reported_without_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(tmp_path, monkeypatch)

    def fail(_root: Path) -> Path:
        raise backup.BackupError("known backup failure")

    monkeypatch.setattr(backup, "snapshot", fail)

    assert cli.main(["snapshot"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "known backup failure\n"


def test_unexpected_snapshot_failure_is_bounded_and_does_not_leak_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(tmp_path, monkeypatch)

    def fail(_root: Path) -> Path:
        raise RuntimeError("secret subprocess output")

    monkeypatch.setattr(backup, "snapshot", fail)

    assert cli.main(["snapshot"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret subprocess output" not in captured.err
    assert captured.err == (
        "bounded backup operation failed; inspect private receipt or incomplete snapshot\n"
    )


def test_expected_health_failure_is_serialized_without_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(tmp_path, monkeypatch)

    def fail(**_paths: Path) -> dict[str, object]:
        raise health.OperationsError("secret validation detail")

    monkeypatch.setattr(health, "health_operation", fail)

    assert cli.main(["health"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error_category": "local_io_or_validation_failed",
        "exit_code": 2,
        "operation": "health",
        "outcome": "failed",
        "schema": health.OPS_SCHEMA,
    }
    assert "secret validation detail" not in captured.out
