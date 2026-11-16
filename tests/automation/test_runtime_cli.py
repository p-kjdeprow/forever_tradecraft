from __future__ import annotations

import json
from pathlib import Path

import pytest
from crowley_automation.cli import main
from crowley_automation.ledger import Ledger, LedgerError, UncertainEffectInterlock
from crowley_automation.model import Batch, CollectMail
from crowley_automation.runtime import _operation_ledger, default_ledger_path, maintenance_blocker

NOW = 1_000
HOUSE = "us-normal-horde"
CHARACTER = "Tester"


def make_batch() -> Batch:
    return Batch(HOUSE, CHARACTER, NOW + 200, (CollectMail(2),))


def test_default_ledger_uses_xdg_state_home() -> None:
    assert default_ledger_path(
        {"HOME": "/home/worker", "XDG_STATE_HOME": "/state", "XDG_RUNTIME_DIR": "/run/user/1"}
    ) == Path("/state/crowley/automation/ledger.sqlite3")
    assert default_ledger_path({"HOME": "/home/worker"}) == Path(
        "/home/worker/.local/state/crowley/automation/ledger.sqlite3"
    )


def test_live_operation_requires_the_shared_ledger_but_fake_io_can_isolate(tmp_path: Path) -> None:
    custom = tmp_path / "test-ledger.sqlite3"

    with pytest.raises(ValueError, match="shared default automation ledger"):
        _operation_ledger(custom, io=None)

    isolated = _operation_ledger(custom, io=object())  # type: ignore[arg-type]
    assert isolated.path == custom.absolute()
    assert custom.exists()


def test_maintenance_blocker_handles_missing_busy_uncertain_and_invalid_state(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "ledger.sqlite3"
    assert maintenance_blocker(missing) is None
    assert not missing.exists()

    path = tmp_path / "busy" / "ledger.sqlite3"
    ledger = Ledger(path)
    with ledger.runner_lock():
        assert maintenance_blocker(path) == "automation_running"

    uncertain_path = tmp_path / "uncertain" / "ledger.sqlite3"
    uncertain = Ledger(uncertain_path)
    batch = make_batch()
    uncertain.prepare(batch, now=NOW)
    uncertain.claim_dispatch(
        batch.batch_id,
        0,
        0,
        now=NOW,
        session_boot_id="boot-a",
        receipt_baseline=4,
    )
    assert maintenance_blocker(uncertain_path) == "automation_uncertain"

    invalid = tmp_path / "invalid" / "ledger.sqlite3"
    invalid.parent.mkdir(mode=0o700)
    invalid.write_bytes(b"not a sqlite database")
    assert maintenance_blocker(invalid) == "automation_state_invalid"


def test_durable_stop_survives_ledger_reopen_and_requires_explicit_clear(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    ledger.stop("operator_stop", now=NOW)

    assert Ledger(path).stop_requested() is True
    Ledger(path).clear_stop()
    assert Ledger(path).stop_requested() is False


def test_causal_reconcile_requires_exact_later_receipt_and_boot(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    batch = make_batch()
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(
        batch.batch_id,
        0,
        0,
        now=NOW,
        session_boot_id="boot-a",
        receipt_baseline=7,
    )
    ledger.recover_interrupted(now=NOW + 1)
    receipt = {
        "sequence": 8,
        "batchId": batch.batch_id,
        "ordinal": 0,
        "stepIndex": 0,
        "callbackDelivered": True,
        "postcondition": True,
        "actionComplete": True,
        "detail": "mail_state_and_settlement_changed",
    }

    with pytest.raises(LedgerError, match="boot"):
        ledger.reconcile_receipt(batch.batch_id, 0, 0, receipt, boot_id="boot-b", step_limit=2)
    assert ledger.has_uncertain_effect()

    ledger.reconcile_receipt(batch.batch_id, 0, 0, receipt, boot_id="boot-a", step_limit=2, now=NOW + 2)
    assert ledger.status(batch.batch_id)["state"] == "succeeded"
    assert not ledger.has_uncertain_effect()


def test_final_mail_page_can_reconcile_as_action_success(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW, session_boot_id="boot-a", receipt_baseline=7)
    ledger.recover_interrupted(now=NOW + 1)
    receipt = {
        "sequence": 8,
        "batchId": batch.batch_id,
        "ordinal": 0,
        "stepIndex": 0,
        "callbackDelivered": True,
        "postcondition": True,
        "actionComplete": False,
        "detail": "mail_page_postcondition",
    }

    ledger.reconcile_receipt(batch.batch_id, 0, 0, receipt, boot_id="boot-a", step_limit=1, now=NOW + 2)

    status = ledger.status(batch.batch_id)
    assert status["state"] == "succeeded"
    assert status["actions"][0]["state"] == "succeeded"
    assert status["steps"][0]["action_complete"] is False
    assert not ledger.has_uncertain_effect()


def test_final_route_step_cannot_reconcile_incomplete(tmp_path: Path) -> None:
    from crowley_automation.model import MoveRoute

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    batch = Batch(HOUSE, CHARACTER, NOW + 200, (MoveRoute("route-a"),))
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW, session_boot_id="boot-a", receipt_baseline=7)
    ledger.recover_interrupted(now=NOW + 1)
    receipt = {
        "sequence": 8,
        "batchId": batch.batch_id,
        "ordinal": 0,
        "stepIndex": 0,
        "callbackDelivered": True,
        "postcondition": True,
        "actionComplete": False,
        "detail": "route_step_postcondition",
    }

    with pytest.raises(LedgerError, match="incomplete"):
        ledger.reconcile_receipt(batch.batch_id, 0, 0, receipt, boot_id="boot-a", step_limit=1, now=NOW + 2)


def test_stop_cli_sets_and_clears_durable_flag(tmp_path: Path, capsys) -> None:
    path = tmp_path / "ledger.sqlite3"

    assert main(["stop", "--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["stopRequested"] is True
    assert main(["stop", "--clear", "--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["stopRequested"] is False


def test_stop_cannot_clear_an_uncertain_effect(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW, session_boot_id="boot-a", receipt_baseline=3)
    ledger.recover_interrupted(now=NOW + 1)
    ledger.stop(now=NOW + 2)

    with pytest.raises(UncertainEffectInterlock):
        ledger.clear_stop()
