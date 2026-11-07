from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session import cli as session_cli
from crowley.session import protocol
from crowley.session.cli import SessionIPC
from crowley.session.controller import Controller, ControllerState
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import Sample
from crowley.session.journal import JournalError
from crowley.session.mail_loop import admit_mail_cycle

from .test_session_executor_click import (
    _started_executor_projection,
    _test_anchor,
    _test_receipt,
    _wire_fixture,
)
from .test_session_mail_loop import (
    _begin_seeded_cycle,
    _seed_mail_controller,
)
from .test_session_mail_loop_composition import QUEUE_ID, WALL_TIME, _make_case, _start_request

_EXECUTOR_AUDIT_KEYS = frozenset(
    {
        "attemptId",
        "queueId",
        "seq",
        "generation",
        "leaseGeneration",
        "cycleId",
        "preCrs",
        "preExecutor",
        "preClickOrdinal",
        "preArms",
        "anchor",
        "confirmation",
        "preEvidence",
        "status",
        "outcomeId",
        "result",
        "postCrs",
        "postExecutor",
        "receipt",
        "postEvidence",
        "observationError",
        "terminalAction",
    }
)
_JOURNAL_ENVELOPE_KEYS = frozenset({"archiveOrdinal", "archivedAt", "commitSequence"})


def _assert_exact_executor_audit(
    returned: object,
    persisted: object,
    *,
    expected_identity: dict[str, object],
    terminal_observation: bool = False,
) -> None:
    """Require one canonical executor record plus only Journal metadata."""

    assert isinstance(returned, dict)
    assert isinstance(persisted, dict)
    audit_keys = (
        (_EXECUTOR_AUDIT_KEYS - {"attemptId", "receipt", "status", "observationError"})
        | {"kind", "physicalAttemptId", "physicalOutcomeId"}
        if terminal_observation
        else _EXECUTOR_AUDIT_KEYS
    )
    assert set(returned) == audit_keys | _JOURNAL_ENVELOPE_KEYS
    assert set(persisted) == audit_keys | _JOURNAL_ENVELOPE_KEYS
    assert all(returned[key] == expected_identity[key] for key in expected_identity)
    for key in _JOURNAL_ENVELOPE_KEYS:
        value = returned[key]
        assert isinstance(value, int) and not isinstance(value, bool) and value > 0
    # Journal persistence is JSON, so tuple-valued receipt coordinates are
    # canonically represented as arrays after the round trip.
    canonical_returned = json.loads(json.dumps(returned, sort_keys=True))
    assert persisted == canonical_returned


def _capability_identity(capability: object) -> dict[str, object]:
    return {
        "queueId": capability.queue_id,
        "seq": capability.admitted_seq,
        "cycleId": capability.cycle_id,
        "generation": capability.generation,
        "leaseGeneration": capability.lease_generation,
    }


def _assert_evidence_matches_frame(evidence: object, frame: protocol.Frame) -> None:
    """Bind durable evidence to independently retained fixture bytes."""

    assert isinstance(evidence, dict)
    expected = _evidence(frame)
    assert evidence == expected
    raw = bytes.fromhex(evidence["rawFrameHex"])
    assert raw == frame.raw_bytes
    decoded = protocol.decode_frame(raw)
    assert decoded.raw_bytes == frame.raw_bytes
    assert decoded.boot == frame.boot
    assert decoded.sequence == frame.sequence
    assert decoded.crc32 == frame.crc32
    assert decoded.snapshot.value == frame.snapshot.value


def _retain_driver_receipts(driver: object) -> list[dict[str, object]]:
    """Observe the real driver call-local receipt without changing its path."""

    receipts: list[dict[str, object]] = []
    original_run = driver.run

    def retain(*args: object, **kwargs: object) -> dict[str, object]:
        returned = original_run(*args, **kwargs)
        assert isinstance(returned, dict)
        # X11Backend pointer admission recursively re-enters DesktopDriver.run
        # with the broker reservation. The inner invocation is the physical
        # call-local receipt that the daemon archives; the outer invocation is
        # only the one-use rate wrapper.
        if kwargs.get("_rate_reservation") is not None:
            receipts.append(copy.deepcopy(returned))
        return returned

    driver.run = retain  # type: ignore[method-assign]
    return receipts


def _assert_unique_executor_artifact(controller: Controller, record: dict[str, object]) -> Path:
    """Require one exact outcome artifact and exact canonical payload."""

    outcome_id = record.get("outcomeId")
    assert isinstance(outcome_id, str) and outcome_id
    matches: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((controller.journal.archive_path / "executor").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        if payload.get("outcomeId") == outcome_id:
            matches.append((path, payload))
    assert len(matches) == 1
    artifact, payload = matches[0]
    assert artifact.name == f"{int(record['archiveOrdinal']):020d}.json"
    canonical_record = json.loads(json.dumps(record, sort_keys=True))
    assert payload == canonical_record
    return artifact


class _ResumeProbeDriver:
    """Unarmed restart owner that records any forbidden re-arm/input attempt."""

    def __init__(self, *, generation: int) -> None:
        self.arm_calls = 0
        self.input_calls = 0
        self.revoke_calls: list[str] = []
        self.guardian = SimpleNamespace(armed=False, generation=generation, revoke=self.revoke)

    def arm(self, _evidence: object) -> int:
        self.arm_calls += 1
        self.guardian.armed = True
        self.guardian.generation += 1
        return self.guardian.generation

    def input_calibration(self, _evidence: object) -> None:
        self.input_calls += 1

    def revoke(self, reason: str) -> int:
        self.revoke_calls.append(reason)
        self.guardian.armed = False
        self.guardian.generation += 1
        return self.guardian.generation


def _ipc_start(controller: Controller, daemon: SessionDaemon, queue_id: str = QUEUE_ID) -> dict:
    return SessionIPC(controller, daemon).dispatch(
        {
            "command": "mail_loop_start",
            "generation": controller.status().generation,
            "queueId": queue_id,
            "seq": 1,
            "confirmQueueId": "",
        }
    )


def _mail_failure_frame(frame: protocol.Frame, *, status: str) -> protocol.Frame:
    value = copy.deepcopy(frame.snapshot.value)
    value["sequence"] = frame.sequence + 1
    value["uptimeMs"] = frame.uptime_ms + 1000
    prior_executor = frame.snapshot.value["executor"]
    continuation = prior_executor["lastResult"]["status"] == "started"
    value["executor"].update(
        {
            "currentSeq": 1,
            "currentKind": "mail_collect",
            "phase": "ready",
            "inFlight": False,
            "clickReady": False,
            "blocked": True,
            "latched": False,
            "ambiguous": False,
            "clickOrdinal": prior_executor["clickOrdinal"]
            if continuation
            else prior_executor["clickOrdinal"] + 1,
            "lastClickSource": "button",
            "resultCount": prior_executor["resultCount"]
            if continuation
            else prior_executor["resultCount"] + 1,
            "lastResult": {"seq": 1, "kind": "mail_collect", "status": status},
        }
    )
    return protocol.decode_frame(protocol.encode_frame(value))


def _evidence(frame: protocol.Frame) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(frame.raw_bytes).hexdigest(),
        "length": len(frame.raw_bytes),
        "crc32": frame.crc32,
        "rawFrameHex": frame.raw_bytes.hex(),
    }


def _pre_crs_from_frame(frame: protocol.Frame) -> dict[str, object]:
    return {
        "boot": frame.boot,
        "sequence": frame.sequence,
        "crc32": frame.crc32,
        "identity": "client",
    }


def _fixture_crs(frame: protocol.Frame, identity: str) -> dict[str, object]:
    return {
        "boot": frame.boot,
        "sequence": frame.sequence,
        "crc32": frame.crc32,
        "identity": identity,
    }


def _accept_public_terminal(
    controller: Controller,
    frame: protocol.Frame,
    decision: object,
    capability: object,
) -> dict[str, object]:
    """Drive a full-controller terminal proof from the captured public frame."""

    pre_value = copy.deepcopy(frame.snapshot.value)
    wire_started, _ = _wire_fixture(started=True)
    pre_value["boot"] = wire_started.boot
    pre_frame = protocol.decode_frame(protocol.encode_frame(pre_value))
    started_value = copy.deepcopy(wire_started.snapshot.value)
    started_value["boot"] = pre_frame.boot
    started_value["sequence"] = pre_frame.sequence + 1
    started_value["uptimeMs"] = pre_frame.uptime_ms + 1000
    started_value["executor"]["queueId"] = decision.queue_id
    started_value["executor"]["house"] = pre_frame.snapshot.value["executor"]["house"]
    started_value["executor"]["character"] = pre_frame.snapshot.value["executor"]["character"]
    started_frame = protocol.decode_frame(protocol.encode_frame(started_value))
    # A physical attempt is legal only once the route/mailbox confirmation
    # boundary has been durably established.  Keep the phase graph causal:
    # transition first, then persist the real executor intent/finalization.
    for phase in ("preparing", "mailbox_open", "awaiting_confirmation"):
        controller.transition_mail_cycle(phase, capability=capability)
    controller.begin_executor_click(
        queue_id=decision.queue_id,
        seq=1,
        generation=1,
        lease_generation=1,
        cycle_id=capability.cycle_id,
        pre_crs=_pre_crs_from_frame(pre_frame),
        pre_ordinal=pre_frame.snapshot.value["executor"]["clickOrdinal"],
        arms={"published": False, "session": False},
        anchor=_test_anchor(),
        confirmation="",
        pre_executor=pre_frame.snapshot.value["executor"],
        pre_evidence=_evidence(pre_frame),
    )
    started_executor = _started_executor_projection()
    started_executor["queueId"] = decision.queue_id
    started_executor["house"] = pre_frame.snapshot.value["executor"]["house"]
    started_executor["character"] = pre_frame.snapshot.value["executor"]["character"]
    controller.finalize_executor_click(
        attempt_id=controller.executor_click_checkpoint()["intent"]["attemptId"],
        result="started",
        post_crs=_pre_crs_from_frame(started_frame),
        receipt=_test_receipt(),
        terminal_action=False,
        post_executor=started_executor,
        pre_evidence=_evidence(pre_frame),
        post_evidence=_evidence(started_frame),
    )
    controller.transition_mail_cycle("executing", capability=capability, last_executor_seq=1)
    terminal = copy.deepcopy(started_frame.snapshot.value)
    terminal["sequence"] = started_frame.sequence + 1
    terminal["uptimeMs"] = started_frame.uptime_ms + 1000
    terminal["executor"].update(
        {
            "queueId": decision.queue_id,
            "currentSeq": 0,
            "currentKind": "",
            "phase": "complete",
            "inFlight": False,
            "clickReady": False,
            "workDone": 0,
            "workTotal": 0,
            "blocked": False,
            "latched": False,
            "lastResult": {"seq": 1, "kind": "mail_collect", "status": "succeeded"},
        }
    )
    terminal_frame = protocol.decode_frame(protocol.encode_frame(terminal))
    return controller.observe_mail_cycle_completion(
        capability=capability,
        post_crs=_pre_crs_from_frame(terminal_frame),
        post_executor=terminal_frame.snapshot.value["executor"],
        post_evidence=_evidence(terminal_frame),
    )


@pytest.mark.parametrize("failure", ["before", "after", "revoke"])
def test_daemon_admission_error_blocks_without_scan_fallthrough(tmp_path: Path, failure: str) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    try:
        assert _start_request(controller, daemon)["ok"] is True
        sample = daemon.capture_status()
        assert sample.kind == "FRESH" and sample.frame is not None
        native_before = len(backend.native_calls)
        action_before = len(backend.action_receipts)
        route_before = len(backend.route_timelines)
        reload_before = backend.reload_calls
        prepared_before = list(backend.prepared_commands)
        current_before = controller.journal.read()["current"]
        original_write = controller.journal.write
        failed = True

        def fail_once(value: dict) -> None:
            nonlocal failed
            if failed:
                failed = False
                if failure == "after":
                    original_write(value)
                raise JournalError("admission checkpoint write failed")
            original_write(value)

        controller.journal.write = fail_once  # type: ignore[method-assign]
        if failure == "revoke":
            original_revoke = controller.driver.revoke

            def revoke_then_fail(_reason: str) -> int:
                raise JournalError("guardian revoke failed")

            controller.driver.revoke = revoke_then_fail  # type: ignore[method-assign]

        daemon._advance(sample.frame.snapshot.value, fence=controller.observation_fence())
        assert controller.status().state is ControllerState.BLOCKED
        assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
        assert len(backend.native_calls) == native_before
        assert len(backend.action_receipts) == action_before
        assert len(backend.route_timelines) == route_before
        assert backend.reload_calls == reload_before
        assert backend.prepared_commands == prepared_before
        assert failed is False
        current_after = controller.journal.read()["current"]
        defaults = {
            "action": "",
            "actionAttempt": 0,
            "actionResult": "",
            "actionStartedAt": 0,
            "frameBoot": "",
            "frameSequence": 0,
            "frameCrc32": 0,
            "reloadBoot": "",
        }
        for key, default in defaults.items():
            assert current_after.get(key, default) == current_before.get(key, default)
        assert daemon._mail_loop_last_decision["admitted"] is False
    finally:
        if failure == "revoke":
            controller.driver.revoke = original_revoke  # type: ignore[method-assign]
        driver.close()


def test_public_mail_start_rejects_stale_generation_and_active_owner_before_capture(
    tmp_path: Path,
) -> None:
    daemon, _backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    try:
        calls = 0

        def forbidden_capture(*_args: object, **_kwargs: object):
            nonlocal calls
            calls += 1
            raise AssertionError("stale start must reject before capture")

        daemon.capture_status = forbidden_capture  # type: ignore[method-assign]
        stale = SessionIPC(controller, daemon).dispatch(
            {
                "command": "mail_loop_start",
                "generation": controller.status().generation + 1,
                "queueId": QUEUE_ID,
                "seq": 1,
                "confirmQueueId": "",
            }
        )
        assert stale == {"ok": False, "error": "stale generation"}
        assert calls == 0
    finally:
        driver.close()

    daemon, _backend, driver, controller, _clock = _make_case(
        tmp_path / "active", armed=False, max_messages=1
    )
    try:
        assert _start_request(controller, daemon)["ok"] is True
        sample = daemon.capture_status()
        daemon._mail_loop_step = lambda *_args: None  # type: ignore[method-assign]
        assert daemon._admit_mail_loop(
            sample.frame.snapshot.value, controller.status(), controller.observation_fence()
        )
        calls = 0

        def forbidden_active_capture(*_args: object, **_kwargs: object):
            nonlocal calls
            calls += 1
            raise AssertionError("active owner must reject before capture")

        daemon.capture_status = forbidden_active_capture  # type: ignore[method-assign]
        active = _ipc_start(controller, daemon)
        assert active["ok"] is False
        assert "repeat mode" in active["error"]
        assert calls == 0
    finally:
        driver.close()


def test_mail_start_stop_during_capture_invalidates_prior_commit(tmp_path: Path) -> None:
    daemon, _backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    entered = threading.Event()
    release = threading.Event()
    original_capture = daemon.capture_status
    result: dict[str, object] = {}

    def blocked_capture(*_args: object, **_kwargs: object):
        entered.set()
        assert release.wait(timeout=2)
        return original_capture()

    daemon.capture_status = blocked_capture  # type: ignore[method-assign]

    def start() -> None:
        try:
            result["value"] = daemon.mail_loop_start(
                generation=controller.status().generation,
                queue_id=QUEUE_ID,
                seq=1,
                confirm_queue_id="",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=start)
    try:
        thread.start()
        assert entered.wait(timeout=2)
        stopped = daemon.mail_loop_stop(generation=controller.status().generation)
        assert stopped["enabled"] is False
        release.set()
        thread.join(timeout=2)
        assert isinstance(result.get("error"), Exception)
        assert "reservation is stale" in str(result["error"])
        assert daemon.mail_loop_status()["enabled"] is False
        assert controller.mail_cycle_checkpoint()["phase"] == "none"
    finally:
        release.set()
        thread.join(timeout=2)
        driver.close()


def test_replacement_start_wins_capture_race_without_old_commit(tmp_path: Path) -> None:
    daemon, _backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    first = daemon.capture_status()
    assert first.frame is not None
    replacement_queue = QUEUE_ID + "-replacement"
    value = copy.deepcopy(first.frame.snapshot.value)
    value["executor"]["queueId"] = replacement_queue
    replacement = SimpleNamespace(kind="FRESH", frame=protocol.decode_frame(protocol.encode_frame(value)))
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original_capture = daemon.capture_status
    result: dict[str, object] = {}

    def raced_capture(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=2)
            return first
        if calls == 2:
            return replacement
        return original_capture()

    daemon.capture_status = raced_capture  # type: ignore[method-assign]

    def start_old() -> None:
        try:
            result["old"] = daemon.mail_loop_start(
                generation=controller.status().generation,
                queue_id=QUEUE_ID,
                seq=1,
                confirm_queue_id="",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=start_old)
    try:
        thread.start()
        assert entered.wait(timeout=2)
        new = daemon.mail_loop_start(
            generation=controller.status().generation,
            queue_id=replacement_queue,
            seq=1,
            confirm_queue_id="",
        )
        assert new["queueId"] == replacement_queue
        release.set()
        thread.join(timeout=2)
        assert "reservation is stale" in str(result["error"])
        status = daemon.mail_loop_status()
        assert status["enabled"] is True
        assert status["generation"] == controller.status().generation
        assert status["queueId"] == replacement_queue
        assert status["cycle"]["phase"] == "none"
    finally:
        release.set()
        thread.join(timeout=2)
        driver.close()


def test_completion_does_not_erase_binding_replaced_at_final_commit(tmp_path: Path) -> None:
    """A real public replacement start survives final completion cleanup."""

    daemon, _backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    try:
        assert _start_request(controller, daemon)["ok"] is True
        first = daemon.capture_status()
        assert first.frame is not None
        snapshot = protocol.validate_snapshot(first.frame.snapshot.value)
        binding_a = daemon._mail_loop_binding
        status = controller.status()
        decision = daemon._mail_boundary(first.frame.snapshot.value, status)
        policy = daemon._mail_operation_bounds()
        admitted = admit_mail_cycle(snapshot, binding_a, decision, policy)

        # Admit A through the daemon's real policy/controller path, while
        # suppressing only its next physical step so the terminal fixture can
        # establish the legal completion phase below.
        original_step = daemon._mail_loop_step
        daemon._mail_loop_step = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        assert daemon._admit_mail_loop(
            first.frame.snapshot.value, controller.status(), controller.observation_fence()
        )
        capability = controller.mail_cycle_capability()
        assert capability is not None
        _accept_public_terminal(controller, first.frame, admitted, capability)
        assert controller.mail_cycle_checkpoint()["phase"] == "to_auctioneer"
        daemon._mail_loop_step = original_step  # type: ignore[method-assign]

        replacement_queue = binding_a.queue_id + "-replacement"
        replacement_value = copy.deepcopy(first.frame.snapshot.value)
        replacement_value["executor"]["queueId"] = replacement_queue
        replacement = SimpleNamespace(
            kind="FRESH", frame=protocol.decode_frame(protocol.encode_frame(replacement_value))
        )
        daemon.capture_status = lambda **_kwargs: replacement  # type: ignore[method-assign]
        original_complete = controller.complete_mail_cycle

        def complete_then_publicly_replace(*args: object, **kwargs: object):
            result = original_complete(*args, **kwargs)
            response = daemon.mail_loop_start(
                generation=controller.status().generation,
                queue_id=replacement_queue,
                seq=1,
                confirm_queue_id="",
            )
            assert response["enabled"] is True
            assert response["queueId"] == replacement_queue
            return result

        controller.complete_mail_cycle = complete_then_publicly_replace  # type: ignore[method-assign]
        daemon.execute_route_walk = lambda *_args, **_kwargs: None
        daemon._mail_cycle_require_budget = lambda *_args, **_kwargs: None
        returned = SimpleNamespace(
            boot="boot-return", sequence=99, identity_fingerprint="client", mail_open=False
        )
        daemon._motion_snapshots = {
            (returned.boot, returned.sequence, returned.identity_fingerprint): {
                "mailOpen": False,
                "ahOpen": False,
                "blockingUI": {
                    "staticPopupAvailable": True,
                    "staticPopupShown": False,
                    "gameMenuAvailable": True,
                    "gameMenuShown": False,
                },
            }
        }
        daemon._observe_motion = lambda *_args, **_kwargs: returned
        daemon._run_action = lambda *_args, **_kwargs: {"ahOpen": True, "mailOpen": False}
        daemon.driver.resolve_identity = lambda **_kwargs: SimpleNamespace(fingerprint="client")
        controller.resume = lambda **_kwargs: controller.status()  # type: ignore[method-assign]

        daemon._mail_loop_step({}, capability)
        status = daemon.mail_loop_status()
        assert status["enabled"] is True
        assert status["queueId"] == replacement_queue
        assert status["generation"] == controller.status().generation
        assert status["cycle"]["phase"] == "complete"
        assert daemon._mail_loop_active_binding is None
    finally:
        driver.close()


@pytest.mark.parametrize("field", ["house", "character"])
def test_public_mail_start_rejects_executor_identity_mismatch_without_input(
    tmp_path: Path, field: str
) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    try:
        sample = daemon.capture_status()
        assert sample.frame is not None
        value = copy.deepcopy(sample.frame.snapshot.value)
        queue_id = value["executor"]["queueId"]
        value["executor"][field] = "foreign-identity"
        bad = SimpleNamespace(kind="FRESH", frame=protocol.decode_frame(protocol.encode_frame(value)))
        native_before = len(backend.native_calls)
        action_before = len(backend.action_receipts)
        daemon.capture_status = lambda *_args, **_kwargs: bad  # type: ignore[method-assign]
        response = _ipc_start(controller, daemon, queue_id=queue_id)
        assert response["ok"] is False
        assert "authoritative mail action" in response["error"]
        assert daemon.mail_loop_status()["enabled"] is False
        assert len(backend.native_calls) == native_before
        assert len(backend.action_receipts) == action_before
    finally:
        driver.close()


def test_public_mail_start_rejects_lone_surrogate_before_capture(tmp_path: Path) -> None:
    daemon, _backend, driver, controller, _clock = _make_case(tmp_path, armed=False, max_messages=1)
    try:
        called = False

        def forbidden_capture(*_args: object, **_kwargs: object):
            nonlocal called
            called = True
            raise AssertionError("invalid queue identity must reject before capture")

        daemon.capture_status = forbidden_capture  # type: ignore[method-assign]
        response = SessionIPC(controller, daemon).dispatch(
            {
                "command": "mail_loop_start",
                "generation": controller.status().generation,
                "queueId": "\ud800",
                "seq": 1,
                "confirmQueueId": "",
            }
        )
        assert response["ok"] is False
        assert "malformed" in response["error"] or "UTF-8" in response["error"]
        assert called is False
    finally:
        driver.close()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["mail-loop-start", "--generation", "7", "--queue-id", "queue-7", "--seq", "3"],
            {
                "command": "mail_loop_start",
                "generation": 7,
                "queueId": "queue-7",
                "seq": 3,
                "confirmQueueId": "",
            },
        ),
        (
            ["mail-loop-status"],
            {"command": "mail_loop_status"},
        ),
        (
            ["mail-loop-stop", "--generation", "7"],
            {"command": "mail_loop_stop", "generation": 7},
        ),
        (
            [
                "executor-click",
                "--generation",
                "7",
                "--queue-id",
                "queue-7",
                "--seq",
                "3",
                "--confirm-queue-id",
                "queue-7",
                "--cycle-id",
                "cycle-7",
            ],
            {
                "command": "executor_click",
                "generation": 7,
                "queueId": "queue-7",
                "seq": 3,
                "confirmQueueId": "queue-7",
                "cycleId": "cycle-7",
            },
        ),
    ],
    ids=["start", "status", "stop", "executor-click"],
)
def test_cli_serializes_exact_mail_loop_and_executor_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected: dict[str, object],
) -> None:
    requests: list[dict[str, object]] = []
    config = SimpleNamespace(socket=tmp_path / "session.sock", reload_deadline_seconds=45)
    monkeypatch.setattr(session_cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        session_cli,
        "_socket_request",
        lambda socket, request, timeout: (
            requests.append({"socket": socket, "timeout": timeout, "request": request})
            or {"ok": True, "status": {}}
        ),
    )
    assert session_cli.main(["--config", str(tmp_path / "session.toml"), *arguments]) == 0
    assert len(requests) == 1
    assert requests[0]["request"] == expected


def test_resume_refuses_staged_and_restart_mail_ownership_before_generation_or_arm(
    tmp_path: Path,
) -> None:
    controller, frame, decision = _seed_mail_controller(tmp_path / "restart")
    _begin_seeded_cycle(controller, frame, decision)
    fresh_driver = _ResumeProbeDriver(generation=controller.status().lease_generation)
    fresh = Controller(controller.journal, driver=fresh_driver, recover_on_restart=False)
    before = fresh.status()
    before_journal = copy.deepcopy(fresh.journal.read())
    before_cycle = fresh.mail_cycle_checkpoint()
    before_archives = fresh.journal.archives("mail_cycle")
    with pytest.raises(RuntimeError, match="restart/recovery fence"):
        fresh.resume(expected_generation=before.generation)
    after = fresh.status()
    assert after == before
    assert fresh.journal.read() == before_journal
    assert fresh.mail_cycle_checkpoint() == before_cycle
    assert fresh.journal.archives("mail_cycle") == before_archives
    assert fresh_driver.guardian.armed is False
    assert fresh_driver.arm_calls == 0
    assert fresh_driver.input_calls == 0

    controller, frame, decision = _seed_mail_controller(tmp_path / "staged")
    capability = _begin_seeded_cycle(controller, frame, decision)
    _accept_public_terminal(controller, frame, decision, capability)
    controller.transition_mail_cycle("opening_ah", capability=capability)
    original_append = controller.journal.append_archive_bundle

    def fail_publication(*_args: object, **_kwargs: object):
        raise JournalError("mail publication boundary failed")

    controller.journal.append_archive_bundle = fail_publication  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="publication boundary"):
        controller.complete_mail_cycle(
            capability=capability,
            ah_open_proof={
                "fresh": True,
                "ahOpen": True,
                "mailOpen": False,
                "identity": "client",
                "generation": controller.status().generation,
                "leaseGeneration": controller.status().lease_generation,
            },
        )
    controller.journal.append_archive_bundle = original_append  # type: ignore[method-assign]
    fresh_driver = _ResumeProbeDriver(generation=controller.status().lease_generation)

    def fail_fresh_recovery(*_args: object, **_kwargs: object):
        raise JournalError("fresh recovery publication boundary failed")

    # Controller construction normally replays the already-published
    # executor manifest before it can inspect the staged mail outcome.  Keep
    # this injection at the controller's own staged-recovery boundary so the
    # fresh owner can construct and durably fence the unresolved publication.
    original_replay = controller.journal.replay_archive_bundles
    controller.journal.replay_archive_bundles = lambda: 0  # type: ignore[method-assign]
    controller.journal.append_archive_bundle = fail_fresh_recovery  # type: ignore[method-assign]
    try:
        fresh = Controller(controller.journal, driver=fresh_driver, recover_on_restart=False)
    finally:
        controller.journal.replay_archive_bundles = original_replay  # type: ignore[method-assign]
        controller.journal.append_archive_bundle = original_append  # type: ignore[method-assign]
    before = fresh.status()
    before_journal = copy.deepcopy(fresh.journal.read())
    before_cycle = fresh.mail_cycle_checkpoint()
    before_archives = fresh.journal.archives("mail_cycle")
    with pytest.raises(RuntimeError, match="publication is unresolved"):
        fresh.resume(expected_generation=before.generation)
    assert fresh.status() == before
    assert fresh.journal.read() == before_journal
    assert fresh.mail_cycle_checkpoint() == before_cycle
    assert fresh.journal.archives("mail_cycle") == before_archives
    assert fresh_driver.guardian.armed is False
    assert fresh_driver.arm_calls == 0
    assert fresh_driver.input_calls == 0


def test_mail_completion_wait_deadline_is_fixed_and_reserves_remaining_units(tmp_path: Path) -> None:
    daemon, backend, driver, controller, clock = _make_case(tmp_path, armed=True, max_messages=2)
    try:
        client = backend.agent_client
        assert client is not None
        assert client.ns.Executor.Arm(QUEUE_ID) is True
        client.publish_agent()
        assert _start_request(controller, daemon)["ok"] is True
        daemon.run_once()
        capability = controller.mail_cycle_capability()
        assert capability is not None
        assert controller.mail_cycle_checkpoint()["phase"] == "awaiting_confirmation"

        gate_calls: list[tuple[str, float, str, int, int]] = []
        original_gate = daemon._mail_cycle_require_budget

        def observed_gate(current_capability, bound, effect, **kwargs):  # type: ignore[no-untyped-def]
            cycle = controller.mail_cycle_checkpoint()
            gate_calls.append(
                (
                    effect,
                    float(bound),
                    cycle["phase"],
                    int(cycle["mailUnits"]),
                    int(cycle["completedUnits"]),
                )
            )
            return original_gate(current_capability, bound, effect, **kwargs)

        daemon._mail_cycle_require_budget = observed_gate  # type: ignore[method-assign]
        physical_starts: list[float] = []
        original_run = driver.run

        def observed_run(*args: object, **kwargs: object):
            physical_starts.append(clock.monotonic())
            return original_run(*args, **kwargs)

        driver.run = observed_run  # type: ignore[method-assign]
        request = SessionIPC(controller, daemon).dispatch(
            {
                "command": "executor_click",
                "generation": controller.status().generation,
                "queueId": QUEUE_ID,
                "seq": 1,
                "confirmQueueId": QUEUE_ID,
                "cycleId": capability.cycle_id,
            }
        )
        assert request["ok"] is True and request["result"] == "started"
        assert physical_starts
        route_before = len(backend.route_timelines)
        first_attempt = physical_starts[0]
        first_deadline = daemon._mail_cycle_wait_deadlines[capability.cycle_id]
        assert first_deadline > first_attempt
        assert first_deadline <= first_attempt + capability.budget.mail_postcondition_seconds + 1e-6
        expected_remaining = (
            capability.budget.mail_click_seconds
            + capability.budget.mail_units * capability.budget.mail_postcondition_seconds
            + capability.budget.route_to_auctioneer_seconds
            + capability.budget.ah_reopen_seconds
        )
        assert expected_remaining > capability.budget.mail_postcondition_seconds
        daemon.capture_status = lambda **_kwargs: SimpleNamespace(kind="NO_CHANGE", frame=None)  # type: ignore[method-assign]
        before_poll = len(backend.action_receipts)
        clock.advance(0.5)
        daemon._observe_mail_completion(capability)
        assert any(
            effect == "mail completion observation"
            and bound == capability.budget.mail_postcondition_seconds
            and mail_units == 2
            and completed == 0
            for effect, bound, _phase, mail_units, completed in gate_calls
        )
        assert any(effect == "mail completion observation" for effect, *_rest in gate_calls)
        assert daemon._mail_cycle_wait_deadlines[capability.cycle_id] == first_deadline
        clock.advance(max(0.0, first_deadline - clock.monotonic()) + 0.01)
        status = daemon.run_once()
        cycle = controller.mail_cycle_checkpoint()
        assert cycle["phase"] == "blocked"
        assert "observation deadline expired" in status["blocker"]
        assert cycle["completedUnits"] == 0
        assert cycle["mailUnits"] == 2
        assert len(backend.action_receipts) == before_poll
        assert backend._executor_clicks == 1
        assert len(backend.route_timelines) == route_before
    finally:
        driver.close()


def test_mail_completion_budget_fences_future_work_before_observation_capture(tmp_path: Path) -> None:
    """A fitting world operation cannot consume the overall future reserve."""

    daemon, backend, driver, controller, clock = _make_case(tmp_path, armed=True, max_messages=2)
    client = backend.agent_client
    assert client is not None
    try:
        assert client.ns.Executor.Arm(QUEUE_ID) is True
        client.publish_agent()
        assert _start_request(controller, daemon)["ok"] is True
        sample = daemon.capture_status()
        assert sample.kind == "FRESH" and sample.frame is not None
        validated = protocol.validate_snapshot(sample.frame.snapshot.value)
        binding = daemon._mail_loop_binding
        status = controller.status()
        boundary = daemon._mail_boundary(sample.frame.snapshot.value, status)
        bounds = daemon._mail_operation_bounds()
        decision = admit_mail_cycle(validated, binding, boundary, bounds)
        assert decision.admitted is True
        capability = controller.begin_mail_cycle(
            decision=decision,
            ack_loaded_uid=boundary.ack_loaded_uid,
            status_last_uid=boundary.status_last_uid,
            wire_ack_uids=tuple(sample.frame.snapshot.value["loaded"]["ackedIds"]),
            queue_id=binding.queue_id,
            seq=binding.seq,
            house=binding.house,
            character=binding.character,
            snapshot=validated,
            binding=binding,
            bounds=bounds,
            expected_generation=status.generation,
            expected_lease_generation=status.lease_generation,
        )
        controller.transition_mail_cycle("preparing", capability=capability)
        controller.transition_mail_cycle("mailbox_open", capability=capability)

        # Keep this as a legal pre-effect mailbox boundary.  The injected
        # clock advances within the actual immutable capability deadline; it
        # does not alter the capability or create a synthetic deadline.
        remaining_before = capability.deadline_monotonic - clock.monotonic()
        future_reservation = (
            capability.budget.world_click_seconds
            + capability.budget.mail_units
            * (capability.budget.mail_click_seconds + capability.budget.mail_postcondition_seconds)
            + capability.budget.route_to_auctioneer_seconds
            + capability.budget.ah_reopen_seconds
        )
        assert remaining_before > future_reservation
        clock.advance(remaining_before - (capability.budget.world_click_seconds + 1.0))
        now = clock.monotonic()
        remaining_overall = capability.deadline_monotonic - now
        assert remaining_overall > capability.budget.world_click_seconds
        assert now + future_reservation > capability.deadline_monotonic

        route_before = len(backend.route_timelines)
        action_before = len(backend.action_receipts)
        native_before = len(backend.native_calls)
        reload_before = backend.reload_calls
        prepared_before = list(backend.prepared_commands)
        daemon._advance(sample.frame.snapshot.value, fence=controller.observation_fence())
        cycle = controller.mail_cycle_checkpoint()
        assert cycle["phase"] == "blocked"
        assert "cannot fit mailbox world click" in cycle["reason"]
        # The gate is before model/anchor/world-click work.  The only input
        # effect below is the normal durable Guardian revoke for blocking.
        assert len(backend.action_receipts) == action_before
        assert len(backend.route_timelines) == route_before
        assert len(backend.native_calls) == native_before
        assert backend.reload_calls == reload_before
        assert backend.prepared_commands == prepared_before
    finally:
        driver.close()


def test_mail_admission_uses_one_wall_sample_across_rollback(tmp_path: Path) -> None:
    """A wall-clock rollback cannot extend one admitted cycle's deadline."""

    daemon, _backend, driver, controller, clock = _make_case(tmp_path, armed=False, max_messages=2)
    try:
        assert _start_request(controller, daemon)["ok"] is True
        sample = daemon.capture_status()
        assert sample.frame is not None
        snapshot = protocol.validate_snapshot(sample.frame.snapshot.value)
        binding = daemon._mail_loop_binding
        status = controller.status()
        decision = daemon._mail_boundary(sample.frame.snapshot.value, status)
        policy = daemon._mail_operation_bounds()
        admitted = admit_mail_cycle(snapshot, binding, decision, policy)

        # The first value is the only coherent admission sample.  A second
        # read observes a rollback and must not be allowed to mint extra
        # monotonic authority from the larger apparent wall-time delta.
        wall_values = iter((WALL_TIME + 2.0, WALL_TIME - 900.0))
        wall_reads: list[float] = []

        def rollback_wall() -> float:
            value = float(next(wall_values))
            wall_reads.append(value)
            return value

        controller.wall_clock = rollback_wall
        monotonic_before = clock.monotonic()
        capability = controller.begin_mail_cycle(
            decision=admitted,
            ack_loaded_uid=decision.ack_loaded_uid,
            status_last_uid=decision.status_last_uid,
            wire_ack_uids=tuple(sample.frame.snapshot.value["loaded"]["ackedIds"]),
            queue_id=binding.queue_id,
            seq=binding.seq,
            house=binding.house,
            character=binding.character,
            snapshot=snapshot,
            binding=binding,
            bounds=policy,
            expected_generation=status.generation,
            expected_lease_generation=status.lease_generation,
        )

        assert wall_reads == [WALL_TIME + 2.0]
        assert capability.deadline_monotonic == pytest.approx(
            monotonic_before + float(admitted.completion_deadline_at) - (WALL_TIME + 2.0)
        )
        assert controller.mail_cycle_checkpoint()["deadlineAt"] == admitted.completion_deadline_at
    finally:
        driver.close()


@pytest.mark.parametrize("result", ["failed", "refused"])
def test_direct_owned_failure_archives_and_blocks_before_return_route(tmp_path: Path, result: str) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=True, max_messages=1)
    client = backend.agent_client
    assert client is not None
    try:
        assert client.ns.Executor.Arm(QUEUE_ID) is True
        client.publish_agent()
        assert _start_request(controller, daemon)["ok"] is True
        daemon.run_once()
        capability = controller.mail_cycle_capability()
        assert capability is not None
        assert controller.mail_cycle_checkpoint()["phase"] == "awaiting_confirmation"
        expected_identity = _capability_identity(capability)
        journal_before = controller.journal.read()
        prior_executor = controller.journal.archives("executor")
        prior_executor_ordinal = max(
            (int(record["archiveOrdinal"]) for record in prior_executor),
            default=0,
        )
        prior_commit_sequence = int(journal_before.get("commitSequence", 0))
        wall_started = int(time.time())
        if result == "failed":
            original_run = driver.run

            def fail_after_real_click(*args: object, **kwargs: object):
                receipt = original_run(*args, **kwargs)
                client.fire("UI_ERROR_MESSAGE", 1, "synthetic_failure")
                client.update_agent()
                client.publish_agent()
                return receipt

            driver.run = fail_after_real_click  # type: ignore[method-assign]
        else:
            original_emit = backend._emit_runtime_pointer_click

            def refuse_after_final_admission(*args: object, **kwargs: object):
                emitted = original_emit(*args, **kwargs)
                if backend.current["mailOpen"]:
                    client.lua.globals()._now = backend.queue["expiresAt"] + 1
                    client.publish_agent()
                return emitted

            backend._emit_runtime_pointer_click = refuse_after_final_admission  # type: ignore[method-assign]

        route_before = len(backend.route_timelines)
        action_before = len(backend.action_receipts)
        world_before = backend._world_clicks
        actual_receipts = _retain_driver_receipts(driver)
        executor_raw: list[bytes] = []
        original_frame_record = daemon._executor_frame_record

        def retain_executor_frame(frame: object) -> dict[str, object]:
            key = (frame.boot, frame.sequence, frame.identity_fingerprint)
            raw = daemon._motion_evidence[key]
            assert isinstance(raw, bytes)
            executor_raw.append(bytes(raw))
            return original_frame_record(frame)

        daemon._executor_frame_record = retain_executor_frame  # type: ignore[method-assign]
        response = SessionIPC(controller, daemon).dispatch(
            {
                "command": "executor_click",
                "generation": controller.status().generation,
                "queueId": QUEUE_ID,
                "seq": 1,
                "confirmQueueId": QUEUE_ID,
                "cycleId": capability.cycle_id,
            }
        )
        wall_finished = int(time.time())
        assert response["ok"] is True, response
        assert response["result"] == result
        archive = response["archive"]
        persisted = controller.journal.archives("executor")[-1]
        assert len(executor_raw) == 2
        pre_frame = protocol.decode_frame(executor_raw[0])
        post_frame = protocol.decode_frame(executor_raw[-1])
        pre_executor = copy.deepcopy(pre_frame.snapshot.value["executor"])
        _assert_exact_executor_audit(
            archive,
            persisted,
            expected_identity=expected_identity,
        )
        assert archive["preExecutor"] == pre_executor
        assert archive["postExecutor"] == post_frame.snapshot.value["executor"]
        _assert_evidence_matches_frame(archive["preEvidence"], pre_frame)
        _assert_evidence_matches_frame(archive["postEvidence"], post_frame)
        assert archive["preCrs"] == _fixture_crs(pre_frame, backend.identity.fingerprint)
        assert archive["postCrs"] == _fixture_crs(post_frame, backend.identity.fingerprint)
        post_executor = post_frame.snapshot.value["executor"]
        assert post_executor["clickOrdinal"] == pre_executor["clickOrdinal"] + 1
        assert post_executor["resultCount"] == pre_executor["resultCount"] + 1
        assert post_executor["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": result}
        assert archive["archiveOrdinal"] == prior_executor_ordinal + 1
        assert archive["commitSequence"] == prior_commit_sequence + 1
        assert wall_started <= archive["archivedAt"] <= wall_finished
        assert archive["result"] == result
        assert archive["terminalAction"] is True
        assert archive["postExecutor"]["lastResult"]["status"] == result
        assert archive["postExecutor"]["blocked"] is True
        assert archive["postExecutor"]["currentSeq"] == 1
        assert archive["postExecutor"]["currentKind"] == "mail_collect"
        assert len(actual_receipts) == 1
        assert json.loads(json.dumps(archive["receipt"], sort_keys=True)) == json.loads(
            json.dumps(actual_receipts[0], sort_keys=True)
        )
        _assert_unique_executor_artifact(controller, archive)
        assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
        assert controller.mail_cycle_checkpoint()["terminalOutcomeId"] == ""
        with pytest.raises(RuntimeError, match="capability"):
            controller.accept_mail_terminal(capability=capability, outcome_id=archive["outcomeId"])
        assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
        assert len(backend.route_timelines) == route_before
        assert backend._world_clicks == world_before
        assert len(backend.action_receipts) == action_before + 1
        assert backend._executor_clicks == 1
    finally:
        driver.close()


@pytest.mark.parametrize("result", ["failed", "refused"])
def test_async_owned_failure_archives_and_blocks_without_return_route(tmp_path: Path, result: str) -> None:
    daemon, backend, driver, controller, _clock = _make_case(tmp_path, armed=True, max_messages=2)
    client = backend.agent_client
    assert client is not None
    try:
        assert client.ns.Executor.Arm(QUEUE_ID) is True
        client.publish_agent()
        assert _start_request(controller, daemon)["ok"] is True
        daemon.run_once()
        capability = controller.mail_cycle_capability()
        assert capability is not None
        expected_identity = _capability_identity(capability)
        executor_raw: list[bytes] = []
        original_frame_record = daemon._executor_frame_record

        def retain_executor_frame(frame: object) -> dict[str, object]:
            key = (frame.boot, frame.sequence, frame.identity_fingerprint)
            raw = daemon._motion_evidence[key]
            assert isinstance(raw, bytes)
            executor_raw.append(bytes(raw))
            return original_frame_record(frame)

        daemon._executor_frame_record = retain_executor_frame  # type: ignore[method-assign]
        actual_receipts = _retain_driver_receipts(driver)
        started = SessionIPC(controller, daemon).dispatch(
            {
                "command": "executor_click",
                "generation": controller.status().generation,
                "queueId": QUEUE_ID,
                "seq": 1,
                "confirmQueueId": QUEUE_ID,
                "cycleId": capability.cycle_id,
            }
        )
        assert started["ok"] is True and started["result"] == "started"
        assert len(executor_raw) == 2
        pre_frame = protocol.decode_frame(executor_raw[0])
        expected_pre_executor = copy.deepcopy(pre_frame.snapshot.value["executor"])
        expected_pre_evidence = _evidence(pre_frame)
        started_frame = protocol.decode_frame(executor_raw[-1])
        daemon._executor_frame_record = original_frame_record  # type: ignore[method-assign]
        started_sample = daemon.capture_status()
        assert started_sample.kind == "FRESH" and started_sample.frame is not None
        post = _mail_failure_frame(started_frame, status=result)
        expected_post_executor = copy.deepcopy(post.snapshot.value["executor"])
        expected_post_evidence = _evidence(post)
        expected_post_crs = _fixture_crs(post, started_sample.window_identity)
        post_sample = Sample(
            kind="FRESH",
            frame=post,
            capture_monotonic=daemon._native_clock(),
            crop=started_sample.crop,
            crc32=post.crc32,
            window_identity=started_sample.window_identity,
        )
        daemon.capture_status = lambda **_kwargs: post_sample  # type: ignore[method-assign]
        daemon._motion_snapshots[(post.boot, post.sequence, post_sample.window_identity)] = copy.deepcopy(
            post.snapshot.value
        )
        daemon._motion_evidence[(post.boot, post.sequence, post_sample.window_identity)] = post.raw_bytes
        daemon._motion_evidence[(post.boot, post.sequence, "test")] = post.raw_bytes
        daemon._motion_crs[(post.boot, post.sequence, post_sample.window_identity)] = post.crc32
        daemon._motion_crs[(post.boot, post.sequence, "test")] = post.crc32
        route_before = len(backend.route_timelines)
        action_before = len(backend.action_receipts)
        physical_parent = controller.journal.archives("executor")[-1]
        assert physical_parent["result"] == "started"
        physical_parent_before = copy.deepcopy(physical_parent)
        assert len(actual_receipts) == 1
        assert physical_parent_before["receipt"] == json.loads(json.dumps(actual_receipts[0], sort_keys=True))
        journal_before = controller.journal.read()
        prior_executor_ordinal = max(
            (int(record["archiveOrdinal"]) for record in controller.journal.archives("executor")),
            default=0,
        )
        prior_commit_sequence = int(journal_before.get("commitSequence", 0))
        wall_started = int(time.time())
        observed_returns: list[dict[str, object]] = []
        original_observe = controller.observe_mail_cycle_completion

        def observe_and_capture(*args: object, **kwargs: object) -> dict[str, object]:
            returned = original_observe(*args, **kwargs)
            observed_returns.append(copy.deepcopy(returned))
            return returned

        controller.observe_mail_cycle_completion = observe_and_capture  # type: ignore[method-assign]
        daemon._observe_mail_completion(capability)
        wall_finished = int(time.time())
        assert len(observed_returns) == 1
        returned = observed_returns[0]
        terminal = controller.journal.archives("executor")[-1]
        _assert_exact_executor_audit(
            returned,
            terminal,
            expected_identity=expected_identity,
            terminal_observation=True,
        )
        parent_after = next(
            record
            for record in controller.journal.archives("executor")
            if record.get("outcomeId") == physical_parent_before["outcomeId"]
        )
        assert parent_after == physical_parent_before
        assert terminal["preCrs"] == physical_parent_before["preCrs"]
        assert terminal["preCrs"] == _fixture_crs(pre_frame, backend.identity.fingerprint)
        assert terminal["preExecutor"] == expected_pre_executor
        assert terminal["preExecutor"] == physical_parent_before["preExecutor"]
        assert terminal["preEvidence"] == expected_pre_evidence
        assert terminal["preEvidence"] == physical_parent_before["preEvidence"]
        assert physical_parent_before["postCrs"] == _fixture_crs(
            started_frame, started_sample.window_identity
        )
        _assert_evidence_matches_frame(physical_parent_before["postEvidence"], started_frame)
        assert physical_parent_before["postExecutor"] == started_frame.snapshot.value["executor"]
        _assert_evidence_matches_frame(terminal["postEvidence"], post)
        assert terminal["postEvidence"] == expected_post_evidence
        assert terminal["postExecutor"] == expected_post_executor
        assert terminal["postCrs"] == expected_post_crs
        assert terminal["postExecutor"]["clickOrdinal"] == expected_pre_executor["clickOrdinal"] + 1
        assert terminal["postExecutor"]["resultCount"] == expected_pre_executor["resultCount"] + 1
        assert terminal["postExecutor"]["lastResult"] == {
            "seq": 1,
            "kind": "mail_collect",
            "status": result,
        }
        assert terminal["archiveOrdinal"] == prior_executor_ordinal + 1
        assert terminal["commitSequence"] == prior_commit_sequence + 1
        assert wall_started <= terminal["archivedAt"] <= wall_finished
        assert "receipt" not in returned
        assert returned["physicalAttemptId"] == physical_parent["attemptId"]
        assert returned["physicalOutcomeId"] == physical_parent["outcomeId"]
        assert returned["outcomeId"] != physical_parent["outcomeId"]
        assert terminal["archiveOrdinal"] != physical_parent["archiveOrdinal"]
        assert terminal["commitSequence"] > physical_parent["commitSequence"]
        _assert_unique_executor_artifact(controller, physical_parent_before)
        _assert_unique_executor_artifact(controller, terminal)
        assert terminal["kind"] == "terminal_observation"
        assert terminal["result"] == result
        assert terminal["physicalAttemptId"] == started["archive"]["attemptId"]
        assert terminal["postExecutor"]["lastResult"]["status"] == result
        assert terminal["postExecutor"]["blocked"] is True
        assert terminal["outcomeId"]
        assert controller.mail_cycle_checkpoint()["phase"] == "blocked"
        assert controller.mail_cycle_checkpoint()["terminalOutcomeId"] == ""
        assert len(backend.route_timelines) == route_before
        assert len(backend.action_receipts) == action_before
        assert backend._executor_clicks == 1
    finally:
        driver.close()
