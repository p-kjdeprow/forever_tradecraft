"""Focused tests for the packaged Crowley operations CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crowley.ops import backup, cli, health


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    return (
        tmp_path / "repo",
        tmp_path / "session.toml",
        tmp_path / "state",
        tmp_path / "backups",
        tmp_path / "snapshot",
    )


def test_backup_forwards_exact_arguments_and_returns_operation_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, config, state_root, backup_root, _snapshot = _paths(tmp_path)
    calls: list[tuple[Path, Path, Path, Path]] = []

    def run(*, repo: Path, config: Path, state_root: Path, backup_root: Path) -> dict[str, object]:
        calls.append((repo, config, state_root, backup_root))
        return {"outcome": "complete", "exit_code": 7}

    monkeypatch.setattr(health, "backup_operation", run)

    assert (
        cli.main(
            [
                "backup",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--state-root",
                str(state_root),
                "--backup-root",
                str(backup_root),
            ]
        )
        == 7
    )
    assert calls == [(repo, config, state_root, backup_root)]
    assert json.loads(capsys.readouterr().out) == {
        "schema": health.OPS_SCHEMA,
        "operation": "backup",
        "outcome": "complete",
        "exit_code": 7,
    }


def test_health_forwards_exact_arguments_and_returns_operation_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, config, state_root, backup_root, _snapshot = _paths(tmp_path)
    calls: list[tuple[Path, Path, Path, Path]] = []

    def run(*, repo: Path, config_path: Path, state_root: Path, backup_root: Path) -> dict[str, object]:
        calls.append((repo, config_path, state_root, backup_root))
        return {"classification": "gated", "exit_code": 3}

    monkeypatch.setattr(health, "health_operation", run)

    assert (
        cli.main(
            [
                "health",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--state-root",
                str(state_root),
                "--backup-root",
                str(backup_root),
            ]
        )
        == 3
    )
    assert calls == [(repo, config, state_root, backup_root)]
    assert json.loads(capsys.readouterr().out) == {
        "schema": health.OPS_SCHEMA,
        "operation": "health",
        "outcome": "gated",
        "exit_code": 3,
    }


def test_snapshot_forwards_exact_arguments_and_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _config, _state_root, root, _snapshot = _paths(tmp_path)
    calls: list[tuple[Path, Path]] = []

    def run(received_repo: Path, received_root: Path) -> Path:
        calls.append((received_repo, received_root))
        return root / "crowley-20261107T061327Z-012345abcdef"

    monkeypatch.setattr(backup, "snapshot", run)

    assert cli.main(["snapshot", "--repo", str(repo), "--root", str(root)]) == 0
    assert calls == [(repo, root)]
    assert capsys.readouterr().out == "snapshot complete: crowley-20261107T061327Z-012345abcdef\n"


def test_drill_forwards_exact_arguments_and_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _config, _state_root, root, snapshot = _paths(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"result":"restore_verified"}', encoding="utf-8")
    calls: list[tuple[Path, Path, Path]] = []

    def run(received_repo: Path, received_snapshot: Path, received_root: Path) -> Path:
        calls.append((received_repo, received_snapshot, received_root))
        return receipt_path

    monkeypatch.setattr(backup, "drill", run)

    assert cli.main(["drill", str(snapshot), "--repo", str(repo), "--root", str(root)]) == 0
    assert calls == [(repo, snapshot, root)]
    assert capsys.readouterr().out == "restore drill restore_verified: receipt.json\n"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [("snapshot", []), ("drill", ["snapshot-dir"])],
)
def test_snapshot_and_drill_require_repo(
    command: str, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([command, *arguments])

    assert raised.value.code == 2
    assert "--repo" in capsys.readouterr().err


def test_expected_snapshot_failure_is_reported_without_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_repo: Path, _root: Path) -> Path:
        raise backup.BackupError("known backup failure")

    monkeypatch.setattr(backup, "snapshot", fail)

    assert cli.main(["snapshot", "--repo", str(tmp_path), "--root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "known backup failure\n"


def test_unexpected_snapshot_failure_is_bounded_and_does_not_leak_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_repo: Path, _root: Path) -> Path:
        raise RuntimeError("secret subprocess output")

    monkeypatch.setattr(backup, "snapshot", fail)

    assert cli.main(["snapshot", "--repo", str(tmp_path), "--root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret subprocess output" not in captured.err
    assert captured.err == (
        "bounded backup operation failed; inspect private receipt or incomplete snapshot\n"
    )


def test_expected_health_failure_is_serialized_without_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_paths: Path) -> dict[str, object]:
        raise health.OperationsError("secret validation detail")

    monkeypatch.setattr(health, "health_operation", fail)

    assert (
        cli.main(
            [
                "health",
                "--repo",
                str(tmp_path),
                "--config",
                str(tmp_path / "config"),
                "--state-root",
                str(tmp_path / "state"),
                "--backup-root",
                str(tmp_path / "backups"),
            ]
        )
        == 2
    )
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
