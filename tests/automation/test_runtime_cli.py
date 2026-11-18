from __future__ import annotations

import json
from pathlib import Path

import pytest
from crowley_automation.cli import main
from crowley_automation.ledger import Ledger, LedgerError, UncertainEffectInterlock
from crowley_automation.model import Batch, CollectMail
from crowley_automation.runtime import (
    _operation_ledger,
    arm_seller_backend,
    default_ledger_path,
    maintenance_blocker,
)

NOW = 1_000
HOUSE = "us-normal-horde"
CHARACTER = "Tester"


def make_batch() -> Batch:
    return Batch(HOUSE, CHARACTER, NOW + 200, (CollectMail(2),))


def test_default_ledger_uses_xdg_state_home(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    state = tmp_path / "synthetic-state"
    runtime = tmp_path / "synthetic-runtime"
    assert (
        default_ledger_path(
            {"HOME": str(home), "XDG_STATE_HOME": str(state), "XDG_RUNTIME_DIR": str(runtime)}
        )
        == state / "crowley/automation/ledger.sqlite3"
    )
    assert default_ledger_path({"HOME": str(home)}) == home / ".local/state/crowley/automation/ledger.sqlite3"


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


def test_seller_backend_guard_checks_durable_revoke_and_policy_expiry(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    session = {"state": "active"}

    class Backend:
        deadline = 0
        stop_requested = None

        def set_deadline(self, expires_at: int) -> None:
            self.deadline = expires_at

        def bind_seller_ah_target(self, target_id: str) -> None:
            self.target_id = target_id

    class Store:
        def status(self, session_id: str):
            assert session_id == "seller-session"
            return type("Status", (), session)()

    backend = Backend()
    arm_seller_backend(
        backend,  # type: ignore[arg-type]
        ledger=ledger,
        seller_store=Store(),
        session_id="seller-session",
        policy_expires_at=NOW + 100,
        ah_target_id="capital_auctioneer",
    )

    assert backend.deadline == NOW + 100
    assert backend.target_id == "capital_auctioneer"
    assert backend.stop_requested is not None and backend.stop_requested() is False
    session["state"] = "completed"
    assert backend.stop_requested is not None and backend.stop_requested() is False
    session["state"] = "revoked"
    assert backend.stop_requested is not None and backend.stop_requested() is True


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
        "result": False,
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
        "result": False,
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
        "result": False,
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


def test_seller_runtime_closes_backend_before_unlock_even_on_error(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from crowley_automation import runtime
    from crowley_automation.desktop_input import GuiLease

    lease = GuiLease(tmp_path / "private" / "gui.lock")
    closed = []
    backend = SimpleNamespace(close=lambda: closed.append(lease.held))
    monkeypatch.setattr(runtime, "_operation_ledger", lambda *_args: object())
    monkeypatch.setattr(runtime, "_runtime_components", lambda **_kwargs: (object(), backend))
    with pytest.raises(RuntimeError, match="cancelled"):
        with runtime.seller_runtime(gui_lease=lease, config=object(), io=object()):
            assert lease.held
            raise RuntimeError("cancelled")
    assert closed == [True]
    assert not lease.held


@pytest.mark.parametrize("operation", ["run_batch", "dry_run_batch"])
def test_runtime_seeds_short_deadline_before_first_observation(tmp_path, monkeypatch, operation):
    from types import SimpleNamespace

    from crowley_automation import runtime
    from crowley_automation.desktop_input import GuiLease
    from crowley_automation.runner import BatchAuthorization, SessionArm

    batch = make_batch()
    authorization = BatchAuthorization(batch.batch_id, HOUSE, CHARACTER, NOW + 50)
    arm = SessionArm("boot", HOUSE, CHARACTER, NOW + 40)
    lease = GuiLease(tmp_path / "private" / "gui.lock")
    deadlines, closed = [], []
    backend = SimpleNamespace(
        set_deadline=deadlines.append, cleanup_state="done", close=lambda: closed.append(lease.held)
    )

    def execute(*_args, **_kwargs):
        # This seam represents the runner's first backend.observe call.
        assert deadlines == [NOW + 40]
        assert lease.held
        return {}

    runner = SimpleNamespace(run=execute, preflight=execute)
    monkeypatch.setattr(runtime, "_operation_ledger", lambda *_args: object())
    monkeypatch.setattr(runtime, "_runtime", lambda *_args, **_kwargs: (runner, backend))
    getattr(runtime, operation)(
        batch, authorization, arm, batch.batch_id, gui_lease=lease, config=object(), io=object()
    )
    assert closed == [True]
