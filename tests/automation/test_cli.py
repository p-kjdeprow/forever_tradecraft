from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import crowley_automation.cli as automation_cli
from crowley_automation.cli import main
from crowley_automation.ledger import Ledger

from tests.automation.native_harness import NativeHarness


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


def test_inspect_and_prepare_report_implemented_but_unqualified_runtime(tmp_path: Path, capsys) -> None:
    batch = source_file(tmp_path)
    ledger = tmp_path / "private" / "ledger.sqlite3"

    assert main(["inspect", str(batch)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["runtimeImplemented"] is True
    assert inspected["liveQualification"] == "unverified"
    assert inspected["action_count"] == 1

    assert main(["prepare", str(batch), "--ledger", str(ledger)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["state"] == "prepared"
    assert prepared["runtimeImplemented"] is True
    assert prepared["liveQualification"] == "unverified"

    assert main(["status", prepared["batch_id"], "--ledger", str(ledger)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "prepared"


def test_run_requires_explicit_batch_confirmation_and_session_arm(capsys) -> None:
    try:
        main(["run"])
    except SystemExit as exc:
        assert exc.code == 2
        error = json.loads(capsys.readouterr().err)
        assert "required" in error["error"]
    else:
        raise AssertionError("run must require confirmation and a session arm")


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


def test_observe_exposes_motion_and_operation_without_private_chat_or_mail_text(monkeypatch, capsys) -> None:
    value = NativeHarness().snapshot
    value["chatEdit"]["text"] = "private chat text"
    value["inspection"] = {
        "kind": "mail",
        "page": 0,
        "rows": [
            {
                "index": 1,
                "moneyCopper": 0,
                "codCopper": 0,
                "attachmentCount": 0,
                "attachmentItemIDs": [],
                "returned": False,
                "canDelete": True,
                "daysLeft": 30.0,
                "isGM": False,
                "attachments": [],
            }
        ],
        "hasMore": False,
        "error": "",
        "requestId": "a" * 32,
        "capturedAt": value["wallTime"],
        "sourceCount": 1,
    }

    @contextmanager
    def leased_backend():
        yield None, None, SimpleNamespace(observe_native=lambda: value), {}

    monkeypatch.setattr(automation_cli, "_leased_backend", leased_backend)
    assert main(["observe"]) == 0
    rendered = capsys.readouterr().out
    observed = json.loads(rendered)

    assert observed["motion"]["bindings"]["forward"] == "MOVEFORWARD"
    assert observed["motion"]["hover"]["kind"] == "none"
    assert observed["operation"]["receipt"]["sequence"] == 0
    assert observed["blockingUI"]["gameMenuShown"] is False
    assert observed["chatActive"] is False
    assert "private chat text" not in rendered
    assert "private mail header" not in rendered
