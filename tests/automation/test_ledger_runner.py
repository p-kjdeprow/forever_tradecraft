from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from crowley_automation.ledger import (
    IdempotencyConflict,
    Ledger,
    LedgerError,
    UncertainEffectInterlock,
)
from crowley_automation.model import (
    Batch,
    CollectMail,
    MoveRoute,
    PostCommodity,
    PostItem,
    action_object,
    canonical_json,
)
from crowley_automation.runner import (
    Admission,
    AdmissionDenied,
    AutomationRunner,
    BatchAuthorization,
    DispatchReceipt,
    RunnerConfig,
    SessionArm,
)
from crowley_common.platform.privfs import PrivatePathError

NOW = 1_000
HOUSE = "us-normal-horde"
CHARACTER = "Tester"
BOOT_ID = "boot-a"


def make_batch(action: object | None = None, *, expires_at: int = NOW + 200) -> Batch:
    selected = action or PostCommodity("123:b2.2", 2, 500, 2, 200)
    return Batch(HOUSE, CHARACTER, expires_at, (selected,))  # type: ignore[arg-type]


def make_auth(batch: Batch) -> tuple[BatchAuthorization, SessionArm]:
    return (
        BatchAuthorization(batch.batch_id, batch.house, batch.character, NOW + 100),
        SessionArm(BOOT_ID, batch.house, batch.character, NOW + 100),
    )


def make_admission(**changes: object) -> Admission:
    value = Admission(BOOT_ID, HOUSE, CHARACTER, NOW, False, True, True, True)
    return replace(value, **changes)


class FakeBackend:
    def __init__(
        self,
        *,
        admission: Admission | None = None,
        admissions: list[Admission] | None = None,
        responses: list[tuple[bool | None, bool | None, bool | None]] | None = None,
    ):
        self.admission = admission or make_admission()
        self.admissions = list(admissions or [])
        self.staged_identity: tuple[str, int, int, str] | None = None
        self.responses = list(responses or [(True, True, True)])
        self.staged: list[tuple[int, int]] = []
        self.dispatched: list[tuple[int, int]] = []
        self.fail_dispatch = False

    def stage(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        self.staged.append((ordinal, step_index))
        self.staged_identity = (batch_id, ordinal, step_index, action_digest)

    def observe(self) -> Admission:
        if self.admissions:
            admission = self.admissions.pop(0)
        else:
            admission = self.admission
        if self.staged_identity is not None and admission.ready is None:
            batch_id, ordinal, step_index, action_digest = self.staged_identity
            return replace(
                admission,
                staged_batch_id=batch_id,
                staged_ordinal=ordinal,
                staged_step_index=step_index,
                staged_action_digest=action_digest,
                ready=True,
            )
        return admission

    def dispatch(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
        admission: Admission,
    ) -> DispatchReceipt:
        self.dispatched.append((ordinal, step_index))
        if self.fail_dispatch:
            raise RuntimeError("simulated lost native acknowledgement")
        callback, postcondition, complete = self.responses.pop(0)
        return DispatchReceipt(
            batch_id,
            ordinal,
            step_index,
            action_digest,
            admission.session_boot_id,
            callback,
            postcondition,
            complete,
        )


class MutableClock:
    def __init__(self, value: int = NOW):
        self.value = value

    def __call__(self) -> int:
        return self.value


def runner_for(path: Path, *, config: RunnerConfig | None = None) -> tuple[Ledger, AutomationRunner]:
    ledger = Ledger(path)
    return ledger, AutomationRunner(ledger, config=config, clock=lambda: NOW)


def run_batch(
    runner: AutomationRunner,
    batch: Batch,
    backend: FakeBackend,
    *,
    authorization: BatchAuthorization | None = None,
    arm: SessionArm | None = None,
) -> dict[str, object]:
    default_auth, default_arm = make_auth(batch)
    return runner.run(
        batch,
        authorization=authorization or default_auth,
        session_arm=arm or default_arm,
        confirmed_batch_id=batch.batch_id,
        backend=backend,
    )


def test_duplicate_prepare_is_idempotent_and_spec_conflict_is_rejected(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite3")
    batch = make_batch()

    first = ledger.prepare(batch, now=NOW)
    second = ledger.prepare(batch, now=NOW + 1)

    assert first["state"] == second["state"] == "prepared"
    assert len(ledger.all_status()) == 1
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE batches SET canonical_json='{}' WHERE batch_id=?", (batch.batch_id,))
    with pytest.raises(IdempotencyConflict):
        ledger.prepare(batch, now=NOW + 2)


def test_dispatch_claim_fences_competing_runner_and_restart_marks_uncertain(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "state" / "ledger.sqlite3")
    batch = make_batch()
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW)

    with pytest.raises(LedgerError, match="not ready"):
        ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW)
    with ledger.runner_lock():
        assert ledger.recover_if_idle(now=NOW) is None

    restarted = Ledger(ledger.path)
    auth, arm = make_auth(batch)
    backend = FakeBackend()
    status = AutomationRunner(restarted, clock=lambda: NOW).run(
        batch,
        authorization=auth,
        session_arm=arm,
        confirmed_batch_id=batch.batch_id,
        backend=backend,
    )

    assert status["state"] == "uncertain"
    assert status["steps"][0]["state"] == "uncertain"
    assert backend.dispatched == []


def test_private_ledger_file_and_directory_permissions(tmp_path: Path) -> None:
    path = tmp_path / "secure" / "ledger.sqlite3"
    Ledger(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

    insecure = tmp_path / "shared"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(PrivatePathError):
        Ledger(insecure / "ledger.sqlite3")

    link = tmp_path / "link.sqlite3"
    link.symlink_to(path)
    with pytest.raises((PermissionError, OSError)):
        Ledger(link)

    os.chmod(path, 0o644)
    with pytest.raises(PermissionError, match="mode-0600"):
        Ledger(path)


def test_ledger_connection_context_closes_after_transaction(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")

    with ledger._connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize(
    "admission, message, stage_count",
    [
        (make_admission(observed_at=NOW - 10), "stale", 0),
        (make_admission(human_present=True), "human", 0),
        (make_admission(focused=False), "focused", 0),
        (make_admission(gui_lock_owned=False), "GUI", 0),
        (make_admission(client_ah_owned=False), "auction-house", 1),
        (make_admission(session_boot_id="boot-b"), "boot", 0),
        (make_admission(character="Other"), "identity", 0),
    ],
)
def test_admission_denials_leave_dispatch_unclaimed(
    tmp_path: Path,
    admission: Admission,
    message: str,
    stage_count: int,
) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend(admission=admission)

    with pytest.raises(AdmissionDenied, match=message):
        run_batch(runner, batch, backend)

    status = ledger.status(batch.batch_id)
    assert status["state"] == "prepared"
    assert status["actions"][0]["state"] == "ready"
    assert backend.staged == ([(0, 0)] if stage_count else [])
    assert backend.dispatched == []


def test_preflight_recovers_crash_marker_under_lock_before_backend_input(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    ledger.prepare(batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW, session_boot_id=BOOT_ID, receipt_baseline=4)

    class NoInputBackend:
        def observe(self) -> Admission:
            raise AssertionError("preflight observed the game before recovering uncertainty")

        def stage(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("preflight staged before recovering uncertainty")

        def clear_stage(self) -> None:
            raise AssertionError("preflight cleanup sent input before checking uncertainty")

    auth, arm = make_auth(batch)
    runner = AutomationRunner(ledger, clock=lambda: NOW)
    with pytest.raises(UncertainEffectInterlock):
        runner.preflight(
            batch,
            authorization=auth,
            session_arm=arm,
            confirmed_batch_id=batch.batch_id,
            backend=NoInputBackend(),  # type: ignore[arg-type]
        )

    assert ledger.status(batch.batch_id)["state"] == "uncertain"
    assert ledger.has_uncertain_effect()


@pytest.mark.parametrize("field", ["batch", "ordinal", "step", "digest", "ready"])
def test_staged_readback_mismatch_blocks_dispatch_before_claim(tmp_path: Path, field: str) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    expected_digest = hashlib.sha256(
        canonical_json(action_object(batch.actions[0])).encode("utf-8")
    ).hexdigest()
    readback: dict[str, object] = {
        "staged_batch_id": batch.batch_id,
        "staged_ordinal": 0,
        "staged_step_index": 0,
        "staged_action_digest": expected_digest,
        "ready": True,
    }
    if field == "batch":
        readback["staged_batch_id"] = "0" * 64
    elif field == "ordinal":
        readback["staged_ordinal"] = 1
    elif field == "step":
        readback["staged_step_index"] = 1
    elif field == "digest":
        readback["staged_action_digest"] = "0" * 64
    else:
        readback["ready"] = False
    backend = FakeBackend(
        admissions=[
            make_admission(),
            make_admission(**readback),  # type: ignore[arg-type]
        ]
    )

    with pytest.raises(AdmissionDenied, match="staged operation identity or readiness"):
        run_batch(runner, batch, backend)

    status = ledger.status(batch.batch_id)
    assert status["state"] == "prepared"
    assert status["steps"][0]["state"] == "ready"
    assert backend.staged == [(0, 0)]
    assert backend.dispatched == []


@pytest.mark.parametrize(
    "changed_admission, message",
    [
        (make_admission(human_present=True), "human"),
        (make_admission(focused=False), "focused"),
        (make_admission(gui_lock_owned=False), "GUI"),
        (make_admission(session_boot_id="boot-b"), "boot"),
    ],
)
def test_changed_admission_after_staging_blocks_dispatch(
    tmp_path: Path,
    changed_admission: Admission,
    message: str,
) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend(admissions=[make_admission(), changed_admission])

    with pytest.raises(AdmissionDenied, match=message):
        run_batch(runner, batch, backend)

    status = ledger.status(batch.batch_id)
    assert status["state"] == "prepared"
    assert status["actions"][0]["state"] == "ready"
    assert backend.staged == [(0, 0)]
    assert backend.dispatched == []


def test_stage_may_acquire_ah_ownership_before_dispatch(tmp_path: Path) -> None:
    _, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend(
        admissions=[make_admission(client_ah_owned=False), make_admission(client_ah_owned=True)]
    )

    status = run_batch(runner, batch, backend)

    assert status["state"] == "succeeded"
    assert backend.staged == [(0, 0)]
    assert backend.dispatched == [(0, 0)]


def test_post_claim_gate_change_is_durably_stopped_before_dispatch(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend(admissions=[make_admission(), make_admission(), make_admission(human_present=True)])

    status = run_batch(runner, batch, backend)
    status = Ledger(ledger.path).status(batch.batch_id)

    assert status["state"] == "failed"
    assert status["steps"][0]["state"] == "failed"
    assert status["steps"][0]["dispatched_at"] == NOW
    assert "pre-dispatch revalidation failed" in status["steps"][0]["reason"]
    assert backend.dispatched == []


def test_staged_readiness_is_rechecked_after_claim(tmp_path: Path) -> None:
    batch = make_batch()
    digest = hashlib.sha256(canonical_json(action_object(batch.actions[0])).encode("utf-8")).hexdigest()
    stale_readback = make_admission(
        staged_batch_id=batch.batch_id,
        staged_ordinal=0,
        staged_step_index=0,
        staged_action_digest=digest,
        ready=False,
    )
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    backend = FakeBackend(admissions=[make_admission(), make_admission(), stale_readback])

    status = run_batch(runner, batch, backend)

    durable_status = Ledger(ledger.path).status(batch.batch_id)
    assert status["state"] == durable_status["state"] == "failed"
    assert durable_status["steps"][0]["state"] == "failed"
    assert backend.dispatched == []


@pytest.mark.parametrize("expired_field", ["batch", "authorization", "session_arm"])
def test_post_claim_rechecks_each_expiration(tmp_path: Path, expired_field: str) -> None:
    expires_at = NOW + 2 if expired_field == "batch" else NOW + 100
    batch = make_batch(expires_at=expires_at)
    authorization, session_arm = make_auth(batch)
    if expired_field == "authorization":
        authorization = replace(authorization, expires_at=NOW + 2)
    elif expired_field == "session_arm":
        session_arm = replace(session_arm, expires_at=NOW + 2)
    clock = MutableClock()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    runner = AutomationRunner(ledger, clock=clock)
    backend = FakeBackend()
    claim_dispatch = ledger.claim_dispatch

    def delayed_claim(*args: object, **kwargs: object) -> None:
        claim_dispatch(*args, **kwargs)  # type: ignore[arg-type]
        clock.value = NOW + 3

    ledger.claim_dispatch = delayed_claim  # type: ignore[method-assign]
    status = runner.run(
        batch,
        authorization=authorization,
        session_arm=session_arm,
        confirmed_batch_id=batch.batch_id,
        backend=backend,
    )

    assert status["state"] == "failed"
    assert "pre-dispatch revalidation failed" in status["steps"][0]["reason"]
    assert backend.dispatched == []


def test_post_claim_rechecks_observation_freshness_after_commit_delay(tmp_path: Path) -> None:
    batch = make_batch()
    authorization, session_arm = make_auth(batch)
    clock = MutableClock()
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    runner = AutomationRunner(ledger, clock=clock)
    backend = FakeBackend()
    claim_dispatch = ledger.claim_dispatch

    def delayed_claim(*args: object, **kwargs: object) -> None:
        claim_dispatch(*args, **kwargs)  # type: ignore[arg-type]
        clock.value = NOW + 4

    ledger.claim_dispatch = delayed_claim  # type: ignore[method-assign]
    status = runner.run(
        batch,
        authorization=authorization,
        session_arm=session_arm,
        confirmed_batch_id=batch.batch_id,
        backend=backend,
    )

    assert status["state"] == "failed"
    assert "pre-dispatch revalidation failed" in status["steps"][0]["reason"]
    assert backend.dispatched == []


def test_uncertain_effect_interlocks_other_batches_across_runner_instances(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first_ledger = Ledger(path)
    uncertain_batch = make_batch()
    first_ledger.prepare(uncertain_batch, now=NOW)
    first_ledger.claim_dispatch(uncertain_batch.batch_id, 0, 0, now=NOW)
    assert first_ledger.recover_interrupted(now=NOW) == 1

    new_batch = make_batch(CollectMail(1))
    assert first_ledger.prepare(new_batch, now=NOW)["state"] == "prepared"
    second_ledger = Ledger(path)
    backend = FakeBackend()
    new_runner = AutomationRunner(second_ledger, clock=lambda: NOW)

    with pytest.raises(UncertainEffectInterlock, match="unresolved effect"):
        run_batch(new_runner, new_batch, backend)

    assert second_ledger.status(new_batch.batch_id)["state"] == "prepared"
    assert backend.staged == backend.dispatched == []


def test_wrong_confirmation_or_authorization_never_stages(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend()
    wrong = BatchAuthorization("0" * 64, HOUSE, CHARACTER, NOW + 100)

    with pytest.raises(AdmissionDenied, match="exact batch ID"):
        run_batch(runner, batch, backend, authorization=wrong)

    assert ledger.status(batch.batch_id)["state"] == "prepared"
    assert backend.staged == []
    assert backend.dispatched == []


def test_dry_run_does_not_call_backend_or_mutate_ledger(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend()

    result = runner.dry_run(batch)

    assert result["dry_run"] is True
    assert result["batch_id"] == batch.batch_id
    assert ledger.all_status() == []
    assert backend.staged == backend.dispatched == []


def test_success_requires_callback_and_verified_postcondition(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend()

    status = run_batch(runner, batch, backend)

    assert status["state"] == "succeeded"
    assert status["steps"][0]["callback_delivered"] is True
    assert status["steps"][0]["postcondition"] is True
    assert backend.dispatched == [(0, 0)]
    assert run_batch(runner, batch, backend)["state"] == "succeeded"
    assert backend.dispatched == [(0, 0)]


def test_manual_post_item_accepts_its_structured_native_result(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch(PostItem("456:p7", 1, 999, 2, 12))

    class PostItemBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            receipt = super().dispatch(
                action,
                batch_id=batch_id,
                ordinal=ordinal,
                step_index=step_index,
                action_digest=action_digest,
                admission=admission,
            )
            return replace(
                receipt,
                result={
                    "kind": "post_item",
                    "auctionId": 77,
                    "itemKey": "456:p7",
                    "quantity": 1,
                    "unitPriceCopper": 999,
                    "depositCopper": 2,
                },
            )

    status = run_batch(runner, batch, PostItemBackend())

    assert status["state"] == "succeeded"
    assert ledger.status(batch.batch_id)["actions"][0]["state"] == "succeeded"


@pytest.mark.parametrize(
    "response, expected",
    [
        ((True, None, None), "uncertain"),
        ((False, True, True), "uncertain"),
        ((True, False, True), "failed"),
    ],
)
def test_ambiguous_or_failed_receipts_are_terminal_and_never_replayed(
    tmp_path: Path,
    response: tuple[bool | None, bool | None, bool | None],
    expected: str,
) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend(responses=[response])

    status = run_batch(runner, batch, backend)
    repeated = run_batch(runner, batch, FakeBackend())

    assert status["state"] == expected
    assert repeated["state"] == expected
    assert len(backend.dispatched) == 1


def test_dispatch_exception_is_uncertain_not_a_retry(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch()
    backend = FakeBackend()
    backend.fail_dispatch = True

    status = run_batch(runner, batch, backend)

    assert status["state"] == "uncertain"
    assert status["steps"][0]["callback_delivered"] is None
    assert run_batch(runner, batch, FakeBackend())["state"] == "uncertain"
    assert len(backend.dispatched) == 1


def test_collect_mail_uses_persisted_one_message_steps(tmp_path: Path) -> None:
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch(CollectMail(3))
    backend = FakeBackend(responses=[(True, True, False), (True, True, True)])

    status = run_batch(runner, batch, backend)

    assert status["state"] == "succeeded"
    assert backend.staged == [(0, 0), (0, 1)]
    assert backend.dispatched == [(0, 0), (0, 1)]
    assert [step["state"] for step in status["steps"]] == ["succeeded", "succeeded"]
    assert status["actions"][0]["next_step"] == 1


def test_route_walk_uses_configured_bounded_steps_and_fresh_admission(tmp_path: Path) -> None:
    config = RunnerConfig(route_step_limits=(("bank_path", 2),))
    ledger, runner = runner_for(tmp_path / "ledger.sqlite3", config=config)
    batch = make_batch(MoveRoute("bank_path"))
    backend = FakeBackend(responses=[(True, True, False), (True, True, True)])

    status = run_batch(runner, batch, backend)

    assert status["state"] == "succeeded"
    assert backend.staged == [(0, 0), (0, 1)]
    assert backend.dispatched == [(0, 0), (0, 1)]


def test_route_that_exhausts_its_configured_budget_without_completion_fails(tmp_path: Path) -> None:
    config = RunnerConfig(route_step_limits=(("bank_path", 1),))
    _, runner = runner_for(tmp_path / "ledger.sqlite3", config=config)
    batch = make_batch(MoveRoute("bank_path"))
    backend = FakeBackend(responses=[(True, True, False)])

    status = run_batch(runner, batch, backend)

    assert status["state"] == "failed"
    assert backend.dispatched == [(0, 0)]


def test_unknown_route_is_denied_without_backend_stage(tmp_path: Path) -> None:
    _, runner = runner_for(tmp_path / "ledger.sqlite3")
    batch = make_batch(MoveRoute("bank_path"))
    backend = FakeBackend()

    with pytest.raises(AdmissionDenied, match="not explicitly configured"):
        run_batch(runner, batch, backend)

    assert backend.staged == backend.dispatched == []
