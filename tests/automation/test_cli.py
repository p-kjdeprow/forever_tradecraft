from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from crowley_automation.cli import main
from crowley_automation.ledger import Ledger


def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "house": "us-normal-horde",
                "character": "Tester",
                "expiresAt": 2_000_000_000,
                "actions": [{"kind": "collect_mail", "messageLimit": 2}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_inspect_and_prepare_are_explicitly_nonexecuting(tmp_path: Path, capsys) -> None:
    batch = source_file(tmp_path)
    ledger = tmp_path / "private" / "ledger.sqlite3"

    assert main(["inspect", str(batch)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["execution_available"] is False
    assert inspected["action_count"] == 1

    assert main(["prepare", str(batch), "--ledger", str(ledger)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["state"] == "prepared"
    assert prepared["execution_available"] is False

    assert main(["status", prepared["batch_id"], "--ledger", str(ledger)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "prepared"


def test_cli_has_no_claiming_run_command() -> None:
    try:
        main(["run"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("foundation CLI must not expose a fake run command")


def test_cli_reports_corrupt_sqlite_as_json_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "private" / "ledger.sqlite3"
    Ledger(path)
    path.write_bytes(b"not a sqlite database")

    assert main(["status", "--ledger", str(path)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert "database" in error["error"].lower()


def test_cli_reports_locked_sqlite_as_json_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "private" / "ledger.sqlite3"
    Ledger(path)
    connection = sqlite3.connect(path, timeout=0)
    connection.execute("BEGIN EXCLUSIVE")
    try:
        assert main(["status", "--ledger", str(path)]) == 2
        error = json.loads(capsys.readouterr().err)
        assert "locked" in error["error"].lower()
    finally:
        connection.rollback()
        connection.close()
