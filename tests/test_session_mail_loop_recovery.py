from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session import protocol
from crowley.session.controller import Controller, ControllerState, SessionMode
from crowley.session.journal import Journal, JournalError
from crowley.session.mail_loop import (
    MailLoopBinding,
    MailLoopBoundary,
    MailOperationBounds,
    admit_mail_cycle,
)
from tests.support.config import fixture_target

from .wow_harness import WowClient

TARGET = fixture_target()


class _RecoveryDriver:
    """A non-physical owner whose only observable effect is lease revocation."""

    def __init__(self, *, generation: int = 1, revoke_raises: bool = False) -> None:
        self.effects: list[tuple[str, str]] = []
        self.revoke_raises = revoke_raises
        self.guardian = SimpleNamespace(
            armed=True,
            generation=generation,
            revoke=self.revoke,
        )

    def revoke(self, reason: str) -> int:
        self.effects.append(("revoke", reason))
        if self.revoke_raises:
            raise JournalError("synthetic revoke failure")
        self.guardian.generation += 1
        return self.guardian.generation


class _BoundaryJournal(Journal):
    """Inject one real bundle write boundary, retaining all prior writes."""

    def __init__(self, path: Path, boundary: str | None) -> None:
        super().__init__(path)
        self.boundary = boundary
        self.reached = False
        self.in_bundle = False

    def append_archive_bundle(self, records, *, checkpoint=None):  # type: ignore[no-untyped-def]
        self.in_bundle = True
        try:
            return super().append_archive_bundle(records, checkpoint=checkpoint)
        finally:
            self.in_bundle = False

    def _before(self, boundary: str, candidate=None) -> None:  # type: ignore[no-untyped-def]
        if not self.in_bundle or self.boundary != boundary:
            return
        current = self.read()
        stage = current["current"].get("mailCycleStage")
        assert isinstance(stage, dict), "full outcome must be durable before bundle writes"
        assert stage["archive"]["result"] in {"complete", "blocked"}
        if boundary == "manifest":
            assert list((self.archive_path / ".pending").glob("*.json"))
        elif boundary == "artifact":
            assert list((self.archive_path / ".bundles").glob("*.json"))
        elif boundary == "checkpoint":
            artifacts = list((self.archive_path / "mail_cycle").glob("*.json"))
            assert artifacts, "archive artifact must exist before checkpoint replacement"
            assert isinstance(candidate, dict)
            assert candidate["archives"]["mail_cycle"]["count"] >= 1
        self.boundary = None
        self.reached = True
        raise JournalError(f"injected {boundary} publication boundary")

    def _write_pending_artifact(self, destination, value, directory):  # type: ignore[no-untyped-def]
        self._before("pending")
        return super()._write_pending_artifact(destination, value, directory)

    def _write_json_artifact(self, destination, value, directory):  # type: ignore[no-untyped-def]
        self._before("manifest")
        return super()._write_json_artifact(destination, value, directory)

    def _write_archive_artifact(self, kind, ordinal, record):  # type: ignore[no-untyped-def]
        self._before("artifact")
        return super()._write_archive_artifact(kind, ordinal, record)

    def write(self, value):  # type: ignore[no-untyped-def]
        self._before("checkpoint", value)
        return super().write(value)


def _queue() -> dict[str, object]:
    return {
        "format": 1,
        "queueId": f"{TARGET.house}-1760000000-recovery",
        "generatedAt": 1_759_990_000,
        "expiresAt": 1_760_000_100,
        "house": TARGET.house,
        "character": "Arthur",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 2}],
    }


def _frame(ack_uid: str) -> protocol.Frame:
    """Synthetic protocol-valid unit fixture derived from WowClient bytes."""

    client = WowClient(
        faction=TARGET.faction,
        realm=TARGET.realm,
        current_region=TARGET.native_region_id,
        current_region_name=TARGET.region,
        queue=_queue(),
    )
    client.set_player_name("Arthur")
    client.set_canonical_plan()
    client.login()
    client.set_frame_open("mailbox")
    client.publish_agent()
    value = copy.deepcopy(protocol.decode_frame(bytes(client.agent_frame)).snapshot.value)
    value.update(
        {
            "ahOpen": True,
            "wallTime": 1_760_000_890,
            "loaded": {"ackedIds": [ack_uid], "generatedAt": 1_760_000_001},
        }
    )
    value["scanner"]["phase"] = "ready"
    value["scanner"]["dueAt"] = 1_760_000_000
    return protocol.decode_frame(protocol.encode_frame(value))


def _bounds() -> MailOperationBounds:
    return MailOperationBounds(1, 1, 1, 1, 1, 1, 1)


def _boundary(
    controller: Controller, ack_uid: str, *, seen_ack_uids: frozenset[str] = frozenset()
) -> MailLoopBoundary:
    return MailLoopBoundary(
        generation=controller.status().generation,
        session_mode=SessionMode.REPEAT.value,
        repeat_phase=ControllerState.WAIT_DUE.value,
        ack_loaded_uid=ack_uid,
        status_last_uid=ack_uid,
        seen_ack_uids=seen_ack_uids,
        unresolved_action=False,
        reconciliation_pending=False,
        stop_requested=False,
        closeout_pending=False,
        last_actual_start_at=1_760_000_000,
        now=1_760_000_890,
    )


def _seed(
    tmp_path: Path,
    *,
    ack_uid: str = "scan-A",
    journal: Journal | None = None,
) -> tuple[Controller, _RecoveryDriver, protocol.Frame, object, MailLoopBinding]:
    journal = journal or Journal(tmp_path / "session.json")
    driver = _RecoveryDriver()
    controller = Controller(
        journal,
        driver=driver,
        recover_on_restart=False,
        wall_clock=lambda: 1_760_000_890,
    )
    controller._state = ControllerState.WAIT_DUE
    controller._mode = SessionMode.REPEAT
    controller._generation = 1
    controller._lease_generation = 1
    controller._started_at = 1_760_000_000
    controller._last_uid = ack_uid
    controller._evidence_meta = {"ackLoadedUID": ack_uid}
    controller._pending = False
    controller._action_intent = ""
    controller._reconciliation_required = False
    controller._restart_uncertain = False
    controller._stop_requested = False
    controller._closeout_requested = False
    frame = _frame(ack_uid)
    queue_id = frame.snapshot.value["executor"]["queueId"]
    binding = MailLoopBinding(
        True,
        controller.status().generation,
        queue_id,
        1,
        TARGET.house,
        "Arthur",
    )
    decision = admit_mail_cycle(
        frame.snapshot,
        binding,
        _boundary(controller, ack_uid),
        _bounds(),
    )
    assert decision.admitted is True
    return controller, driver, frame, decision, binding


def _begin(
    controller: Controller,
    frame: protocol.Frame,
    decision: object,
    binding: MailLoopBinding,
    ack_uid: str,
):
    status = controller.status()
    return controller.begin_mail_cycle(
        decision=decision,
        ack_loaded_uid=ack_uid,
        status_last_uid=ack_uid,
        wire_ack_uids=(ack_uid,),
        queue_id=decision.queue_id,
        seq=1,
        house=TARGET.house,
        character="Arthur",
        snapshot=frame.snapshot,
        binding=binding,
        bounds=_bounds(),
        expected_generation=status.generation,
        expected_lease_generation=status.lease_generation,
    )


def _frame_evidence(frame: protocol.Frame) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(frame.raw_bytes).hexdigest(),
        "length": len(frame.raw_bytes),
        "crc32": frame.crc32,
        "rawFrameHex": frame.raw_bytes.hex(),
    }


def _frame_crs(frame: protocol.Frame) -> dict[str, object]:
    return {
        "boot": frame.boot,
        "sequence": frame.sequence,
        "crc32": frame.crc32,
        "identity": "client",
    }


def _executor_anchor(frame: protocol.Frame, capability: object) -> dict[str, object]:
    return {
        "action": "executor_click",
        "evidenceHash": hashlib.sha256(frame.raw_bytes).hexdigest(),
        "captureId": "recovery-anchor",
        "captureSequence": frame.sequence,
        "identity": "client",
        "generation": capability.lease_generation,
        "targetXid": 257,
        "scene": "executor_mailbox",
        "control": "crowley_executor_button",
        "bounds": [240, 190, 60, 40],
    }


def _executor_receipt(
    pre_frame: protocol.Frame, post_frame: protocol.Frame, capability: object
) -> dict[str, object]:
    """Synthetic unit receipt; this is not a rendered/native physical claim."""

    return {
        "action": "executor_click",
        "generation": capability.lease_generation,
        "identity": "client",
        "admittedAnchorEvidenceHash": hashlib.sha256(pre_frame.raw_bytes).hexdigest(),
        "admittedAnchorCaptureId": "recovery-anchor",
        "admittedAnchorCaptureSequence": pre_frame.sequence,
        "anchorEvidenceHash": hashlib.sha256(post_frame.raw_bytes).hexdigest(),
        "anchorCaptureId": "recovery-refresh",
        "anchorCaptureSequence": post_frame.sequence,
        "endpoint": [1, 1],
        "rateGate": "canonical-user-session",
        "clickPrecommitMonotonicNs": 1,
        "clickDispatchStartedMonotonicNs": 2,
        "pressReleaseMonotonicNs": 3,
        "clickAckMonotonicNs": 4,
        "rateRecordMonotonicNs": 5,
        "guardianCompleted": True,
        "receiptFinalizedMonotonicNs": 6,
    }


def _terminal_frame(frame: protocol.Frame) -> protocol.Frame:
    value = copy.deepcopy(frame.snapshot.value)
    value["sequence"] = frame.sequence + 1
    value["uptimeMs"] = frame.uptime_ms + 1000
    value["executor"].update(
        {
            "currentSeq": 0,
            "currentKind": "",
            "phase": "complete",
            "inFlight": False,
            "clickReady": False,
            "workDone": 0,
            "workTotal": 0,
            "blocked": False,
            "latched": False,
            "ambiguous": False,
            "clickOrdinal": frame.snapshot.value["executor"]["clickOrdinal"] + 1,
            "lastClickSource": "button",
            "resultCount": frame.snapshot.value["executor"]["resultCount"] + 1,
            "lastResult": {"seq": 1, "kind": "mail_collect", "status": "dry_run"},
        }
    )
    return protocol.decode_frame(protocol.encode_frame(value))


def _to_awaiting_confirmation(controller: Controller, capability: object) -> None:
    for phase in ("preparing", "mailbox_open", "awaiting_confirmation"):
        controller.transition_mail_cycle(phase, capability=capability)


def _terminal_executor_outcome(
    controller: Controller, frame: protocol.Frame, capability: object
) -> dict[str, object]:
    """Commit one synthetic, protocol-valid dry-run through real producer APIs."""

    _to_awaiting_confirmation(controller, capability)
    pre_executor = copy.deepcopy(frame.snapshot.value["executor"])
    attempt = controller.begin_executor_click(
        queue_id=capability.queue_id,
        seq=capability.admitted_seq,
        generation=capability.generation,
        lease_generation=capability.lease_generation,
        cycle_id=capability.cycle_id,
        pre_crs=_frame_crs(frame),
        pre_ordinal=pre_executor["clickOrdinal"],
        arms={
            "published": bool(pre_executor["publishedArmed"]),
            "session": bool(pre_executor["sessionArmed"]),
        },
        anchor=_executor_anchor(frame, capability),
        confirmation="",
        pre_executor=pre_executor,
        pre_evidence=_frame_evidence(frame),
    )
    terminal = _terminal_frame(frame)
    archived = controller.finalize_executor_click(
        attempt_id=attempt["attemptId"],
        result="dry_run",
        post_crs=_frame_crs(terminal),
        receipt=_executor_receipt(frame, terminal, capability),
        terminal_action=True,
        post_executor=terminal.snapshot.value["executor"],
        pre_evidence=_frame_evidence(frame),
        post_evidence=_frame_evidence(terminal),
    )
    assert archived["result"] == "dry_run"
    assert archived["postExecutor"]["workDone"] == 0
    return archived


def _synthetic_ah_open_proof(controller: Controller) -> dict[str, object]:
    """Synthetic AH proof using the harness identity, not a live/native claim."""

    status = controller.status()
    return {
        "fresh": True,
        "ahOpen": True,
        "mailOpen": False,
        "identity": "client",
        "generation": status.generation,
        "leaseGeneration": status.lease_generation,
    }


def _complete_to_opening_ah(
    controller: Controller, frame: protocol.Frame, capability: object
) -> dict[str, object]:
    archived = _terminal_executor_outcome(controller, frame, capability)
    accepted = controller.accept_mail_terminal(
        capability=capability,
        outcome_id=archived["outcomeId"],
    )
    assert accepted["outcomeId"] == archived["outcomeId"]
    assert controller.mail_cycle_checkpoint()["phase"] == "to_auctioneer"
    controller.transition_mail_cycle("opening_ah", capability=capability)
    return archived


def _assert_same_outcome(staged: dict, published: dict) -> None:
    expected = staged["archive"]
    # Journal publication adds only its immutable archive envelope.  Compare
    # that envelope explicitly, then compare the complete logical payload in
    # both directions so an unknown/replaced field cannot hide in recovery.
    envelope = {"archiveOrdinal", "archivedAt", "commitSequence"}
    assert set(published) == set(expected) | envelope
    assert isinstance(published["archiveOrdinal"], int)
    assert published["archiveOrdinal"] > 0
    assert isinstance(published["archivedAt"], int)
    assert published["archivedAt"] > 0
    assert isinstance(published["commitSequence"], int)
    assert published["commitSequence"] > 0
    persisted_payload = {key: value for key, value in published.items() if key not in envelope}
    assert persisted_payload == expected


def _assert_executor_outcome(
    record: dict,
    capability: object,
    expected_outcome_id: str,
) -> None:
    """Validate immutable executor identity, raw evidence, and terminal status."""

    assert record["outcomeId"] == expected_outcome_id
    for key, value in {
        "queueId": capability.queue_id,
        "seq": capability.admitted_seq,
        "cycleId": capability.cycle_id,
        "generation": capability.generation,
        "leaseGeneration": capability.lease_generation,
    }.items():
        assert record[key] == value
    assert record["result"] == "dry_run"
    assert record["terminalAction"] is True
    pre_evidence = record["preEvidence"]
    post_evidence = record["postEvidence"]
    pre_raw = bytes.fromhex(pre_evidence["rawFrameHex"])
    post_raw = bytes.fromhex(post_evidence["rawFrameHex"])
    pre_frame = protocol.decode_frame(pre_raw)
    post_frame = protocol.decode_frame(post_raw)
    assert pre_evidence["sha256"] == hashlib.sha256(pre_raw).hexdigest()
    assert post_evidence["sha256"] == hashlib.sha256(post_raw).hexdigest()
    assert record["preCrs"] == _frame_crs(pre_frame)
    assert record["postCrs"] == _frame_crs(post_frame)
    assert record["preExecutor"] == pre_frame.snapshot.value["executor"]
    assert record["postExecutor"] == post_frame.snapshot.value["executor"]
    assert record["postExecutor"]["lastResult"] == {
        "seq": capability.admitted_seq,
        "kind": "mail_collect",
        "status": "dry_run",
    }
    assert record["postExecutor"]["clickOrdinal"] == record["preExecutor"]["clickOrdinal"] + 1
    assert record["postExecutor"]["resultCount"] == record["preExecutor"]["resultCount"] + 1


@pytest.mark.parametrize("result", ["complete", "blocked"])
@pytest.mark.parametrize("boundary", ["pending", "manifest", "artifact", "checkpoint"])
def test_mail_outcome_recovers_exactly_at_each_real_journal_boundary(
    tmp_path: Path, result: str, boundary: str
) -> None:
    journal = _BoundaryJournal(tmp_path / "session.json", None if result == "complete" else boundary)
    controller, driver, frame, decision, binding = _seed(tmp_path, journal=journal)
    capability = _begin(controller, frame, decision, binding, "scan-A")
    original_identity = {
        "cycleId": capability.cycle_id,
        "queueId": capability.queue_id,
        "admittedSeq": capability.admitted_seq,
        "generation": capability.generation,
        "leaseGeneration": capability.lease_generation,
    }
    if result == "complete":
        executor_record = _complete_to_opening_ah(controller, frame, capability)
        _assert_executor_outcome(executor_record, capability, executor_record["outcomeId"])
        journal.boundary = boundary
        with pytest.raises(JournalError, match="injected"):
            controller.complete_mail_cycle(
                capability=capability,
                ah_open_proof=_synthetic_ah_open_proof(controller),
            )
    else:
        with pytest.raises(JournalError, match="injected"):
            controller.block_mail_cycle("operator blocked outcome", capability=capability)

    assert journal.reached is True
    staged = copy.deepcopy(journal.read()["current"]["mailCycleStage"])
    assert staged["archive"]["result"] == result
    assert staged["archive"]["outcomeId"]
    for key, value in original_identity.items():
        assert staged["archive"][key] == value
    # The original owner remains alive, but a later operator hold must be
    # durable and must be carried into recovery without changing the outcome.
    controller.stop()
    expected_outcome_id = staged["archive"]["outcomeId"]
    fresh_driver = _RecoveryDriver()
    fresh = Controller(journal, driver=fresh_driver, recover_on_restart=True)
    records = fresh.journal.archives("mail_cycle")
    assert len(records) == 1
    _assert_same_outcome(staged, records[0])
    assert records[0]["outcomeId"] == expected_outcome_id
    assert fresh.journal.read()["current"]["mailCycleStage"] is None
    assert fresh.mail_cycle_capability() is None
    assert fresh.status().state is ControllerState.STOPPED
    assert fresh.status().stop_requested is True
    assert fresh.status().generation != original_identity["generation"]
    assert fresh.status().lease_generation != original_identity["leaseGeneration"]
    if result == "complete":
        recovered_executor = fresh.journal.archives("executor")
        assert len(recovered_executor) == 1
        _assert_executor_outcome(recovered_executor[0], capability, executor_record["outcomeId"])
    # A complete outcome is staged before its normal final revoke; an injected
    # publication failure therefore reaches only the explicit stop revoke.
    # A blocked outcome revokes before publication and then receives that stop
    # revoke as well.  Assert the ordered effects and resulting lease epoch,
    # rather than accepting a vacuous ``all(...)`` predicate.
    expected_effects = (
        [("revoke", "stop requested")]
        if result == "complete"
        else [("revoke", "operator blocked outcome"), ("revoke", "stop requested")]
    )
    assert driver.effects == expected_effects
    assert controller.status().lease_generation == original_identity["leaseGeneration"] + len(
        expected_effects
    )
    assert fresh_driver.effects == []


@pytest.mark.parametrize("replace_before_raise", [False, True])
def test_mail_admission_write_failure_fences_owner_and_fresh_owner(
    tmp_path: Path, replace_before_raise: bool
) -> None:
    controller, driver, frame, decision, binding = _seed(tmp_path)
    initial_lease_generation = controller.status().lease_generation
    original_write = controller.journal.write
    failed = True

    def fail_once(value):  # type: ignore[no-untyped-def]
        nonlocal failed
        if failed:
            failed = False
            if replace_before_raise:
                original_write(value)
            raise JournalError("admission checkpoint write failed")
        return original_write(value)

    controller.journal.write = fail_once  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="admission checkpoint"):
        _begin(controller, frame, decision, binding, "scan-A")
    assert controller.mail_cycle_capability() is None
    assert controller.status().state is ControllerState.BLOCKED
    assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
    assert controller.mail_cycle_ack_seen("scan-A") is True
    controller.tick(
        expected_generation=controller.status().generation,
        expected_state=ControllerState.BLOCKED,
    )
    assert controller.status().state is ControllerState.BLOCKED
    assert controller.mail_cycle_capability() is None
    expected_reason = "admission checkpoint write failed"
    assert driver.effects == [("revoke", expected_reason)]
    assert controller.status().lease_generation == initial_lease_generation + 1

    with pytest.raises(RuntimeError, match="blocked mail cycle ACK"):
        _begin(controller, frame, decision, binding, "scan-A")
    assert driver.effects == [("revoke", expected_reason)]

    fresh = Controller(controller.journal, driver=_RecoveryDriver(), recover_on_restart=True)
    assert fresh.mail_cycle_capability() is None
    assert fresh.mail_cycle_checkpoint()["phase"] == "blocked"
    assert fresh.mail_cycle_ack_seen("scan-A") is True


def test_admission_revoke_failure_still_publishes_durable_blocked_outcome(tmp_path: Path) -> None:
    controller, driver, frame, decision, binding = _seed(tmp_path)
    initial_lease_generation = controller.status().lease_generation
    driver.revoke_raises = True
    original_write = controller.journal.write
    failed = True

    def fail_once(value):  # type: ignore[no-untyped-def]
        nonlocal failed
        if failed:
            failed = False
            raise JournalError("admission checkpoint write failed")
        return original_write(value)

    controller.journal.write = fail_once  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="admission checkpoint|revoke"):
        _begin(controller, frame, decision, binding, "scan-A")
    assert len(driver.effects) == 1
    assert driver.effects[0][0] == "revoke"
    assert driver.effects[0][1] == "admission checkpoint write failed"
    assert controller.status().lease_generation == initial_lease_generation
    assert controller.mail_cycle_capability() is None
    assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
    records = controller.journal.archives("mail_cycle")
    assert len(records) == 1
    assert records[0]["result"] == "blocked"
    assert controller.journal.read()["current"]["mailCycleStage"] is None

    fresh = Controller(controller.journal, driver=_RecoveryDriver(), recover_on_restart=True)
    assert fresh.mail_cycle_ack_seen("scan-A") is True
    assert fresh.mail_cycle_capability() is None


def test_generic_phase_transition_cannot_forge_terminal_mail_proof(tmp_path: Path) -> None:
    controller, _driver, frame, decision, binding = _seed(tmp_path)
    capability = _begin(controller, frame, decision, binding, "scan-A")
    _to_awaiting_confirmation(controller, capability)
    before = controller.mail_cycle_checkpoint()
    with pytest.raises(RuntimeError):
        controller.transition_mail_cycle(
            "to_auctioneer",
            capability=capability,
            last_executor_seq=capability.admitted_seq,
        )
    assert controller.mail_cycle_checkpoint() == before
    assert controller.mail_cycle_capability() is capability


def test_terminal_acceptance_requires_the_exact_durable_outcome_id(tmp_path: Path) -> None:
    controller, _driver, frame, decision, binding = _seed(tmp_path)
    capability = _begin(controller, frame, decision, binding, "scan-A")
    archived = _terminal_executor_outcome(controller, frame, capability)
    before = copy.deepcopy(controller.mail_cycle_checkpoint())
    wrong_id = ("0" if archived["outcomeId"][0] != "0" else "1") + archived["outcomeId"][1:]
    with pytest.raises(RuntimeError, match="durably archived|identity or result"):
        controller.accept_mail_terminal(capability=capability, outcome_id=wrong_id)
    assert controller.mail_cycle_checkpoint() == before
    assert controller.mail_cycle_capability() is capability
    assert controller.mail_cycle_checkpoint()["terminalOutcomeId"] == ""

    accepted = controller.accept_mail_terminal(
        capability=capability,
        outcome_id=archived["outcomeId"],
    )
    assert accepted["outcomeId"] == archived["outcomeId"]
    assert controller.mail_cycle_checkpoint()["phase"] == "to_auctioneer"


def test_stage_write_failure_is_unknown_and_cannot_fabricate_completion(tmp_path: Path) -> None:
    controller, _driver, frame, decision, binding = _seed(tmp_path)
    capability = _begin(controller, frame, decision, binding, "scan-A")
    _complete_to_opening_ah(controller, frame, capability)
    original_save = controller._save
    reached = False

    def fail_stage(*, last_action: str = "") -> None:
        nonlocal reached
        if last_action == "mail_cycle_complete_staged":
            reached = True
            raise JournalError("staged outcome write failed")
        original_save(last_action=last_action)

    controller._save = fail_stage  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="staged outcome"):
        controller.complete_mail_cycle(
            capability=capability,
            ah_open_proof=_synthetic_ah_open_proof(controller),
        )
    assert reached is True
    assert controller.journal.read()["current"]["mailCycleStage"] is None
    assert controller.journal.archives("mail_cycle") == []

    fresh = Controller(controller.journal, driver=_RecoveryDriver(), recover_on_restart=True)
    records = fresh.journal.archives("mail_cycle")
    assert records
    assert all(record["result"] != "complete" for record in records)
    assert fresh.mail_cycle_capability() is None
    assert fresh.status().state is ControllerState.BLOCKED


def _prepare_repeat(
    controller: Controller, ack_uid: str, *, expect_admitted: bool = True
) -> tuple[protocol.Frame, object, MailLoopBinding]:
    controller._state = ControllerState.WAIT_DUE
    controller._mode = SessionMode.REPEAT
    controller._stop_requested = False
    controller._closeout_requested = False
    controller._restart_uncertain = False
    controller._reconciliation_required = False
    controller._pending = False
    controller._action_intent = ""
    controller._last_uid = ack_uid
    controller._evidence_meta = {"ackLoadedUID": ack_uid}
    controller._started_at = 1_760_000_000
    controller.driver.guardian.armed = True
    controller.driver.guardian.generation = controller.status().lease_generation
    controller._save(last_action="verified repeat precondition")
    frame = _frame(ack_uid)
    binding = MailLoopBinding(
        True,
        controller.status().generation,
        frame.snapshot.value["executor"]["queueId"],
        1,
        TARGET.house,
        "Arthur",
    )
    seen = frozenset(
        record["ackUid"]
        for record in controller.journal.archives("mail_cycle")
        if isinstance(record, dict) and isinstance(record.get("ackUid"), str)
    )
    decision = admit_mail_cycle(
        frame.snapshot,
        binding,
        _boundary(controller, ack_uid, seen_ack_uids=seen),
        _bounds(),
    )
    assert decision.admitted is expect_admitted
    if not expect_admitted:
        assert decision.reason == "ack_already_seen"
    return frame, decision, binding


def test_blocked_ack_history_survives_fresh_owners_and_refuses_old_ack(tmp_path: Path) -> None:
    controller, _driver, frame, decision, binding = _seed(tmp_path, ack_uid="scan-A")
    capability = _begin(controller, frame, decision, binding, "scan-A")
    controller.block_mail_cycle("blocked A", capability=capability)
    assert controller.mail_cycle_ack_seen("scan-A") is True

    owner_b = Controller(
        controller.journal,
        driver=_RecoveryDriver(),
        recover_on_restart=True,
        wall_clock=lambda: 1_760_000_890,
    )
    frame_b, decision_b, binding_b = _prepare_repeat(owner_b, "scan-B")
    capability_b = _begin(owner_b, frame_b, decision_b, binding_b, "scan-B")
    owner_b.block_mail_cycle("blocked B", capability=capability_b)

    owner_a_again = Controller(
        owner_b.journal,
        driver=_RecoveryDriver(),
        recover_on_restart=True,
        wall_clock=lambda: 1_760_000_890,
    )
    assert owner_a_again.mail_cycle_ack_seen("scan-A") is True
    assert owner_a_again.mail_cycle_ack_seen("scan-B") is True
    frame_a, decision_a, binding_a = _prepare_repeat(owner_a_again, "scan-A", expect_admitted=False)
    with pytest.raises(RuntimeError, match="decision is not the exact current action"):
        _begin(owner_a_again, frame_a, decision_a, binding_a, "scan-A")
    assert owner_a_again.mail_cycle_capability() is None
    assert [record["ackUid"] for record in owner_a_again.journal.archives("mail_cycle")] == [
        "scan-A",
        "scan-B",
    ]
