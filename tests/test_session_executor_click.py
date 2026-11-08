from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.session import cli as session_cli
from crowley.session import pointer_execution, protocol
from crowley.session.cli import SessionIPC
from crowley.session.config import SessionConfig
from crowley.session.controller import Controller, ControllerState, SessionMode
from crowley.session.daemon import SessionDaemon
from crowley.session.decoder import PixelDecoder
from crowley.session.driver import DriverError, MeasuredAnchor
from crowley.session.executor import BindingMetadata, Point, PointerButton, PointerTarget, Rect
from crowley.session.input_events import EventClass
from crowley.session.journal import Journal, JournalError
from crowley.session.model import CLI_VERSION
from tests.support.config import fixture_target

from .test_session_driver_actions import (
    _driver as native_driver,
)
from .test_session_driver_actions import (
    _event,
    _EventMonitor,
    _identity,
    _motion_pair,
)
from .wow_harness import WowClient

TARGET = fixture_target()


def _queue() -> dict:
    return {
        "format": 1,
        "queueId": f"{TARGET.house}-1760000000-executor",
        "generatedAt": 1_759_990_000,
        "expiresAt": 1_760_000_100,
        "house": TARGET.house,
        "character": "Arthur",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 2}],
    }


def _pre_executor_projection(*, started: bool = False) -> dict:
    return _wire_fixture(started=started)[1]


def _wire_fixture(*, started: bool = False) -> tuple[protocol.Frame, dict]:
    frame, projection = _wire_fixture_cached(started)
    return frame, copy.deepcopy(projection)


@lru_cache(maxsize=2)
def _wire_fixture_cached(started: bool) -> tuple[protocol.Frame, dict]:
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
    frame = protocol.decode_frame(bytes(client.agent_frame))
    projection = copy.deepcopy(frame.snapshot.value["executor"])
    if started:
        projection.update(
            {
                "phase": "mail_wait",
                "inFlight": True,
                "clickReady": False,
                "clickOrdinal": 1,
                "lastClickSource": "button",
                "resultCount": 1,
                "lastResult": {"seq": 1, "kind": "mail_collect", "status": "started"},
            }
        )
    snapshot = copy.deepcopy(frame.snapshot.value)
    if started:
        # A started callback is a later CRS publication, not a second view of
        # the pre-click frame.  Keep the causal sequence witness advancing so
        # controller proof checks exercise the real unsigned-delta fence.
        snapshot["sequence"] = frame.sequence + 1
        snapshot["uptimeMs"] = frame.uptime_ms + 1000
    snapshot["executor"] = projection
    raw = protocol.encode_frame(snapshot)
    return protocol.decode_frame(raw), projection


def _pre_evidence(*, started: bool = False) -> dict:
    frame, _ = _wire_fixture(started=started)
    raw = frame.raw_bytes
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "length": len(raw),
        "crc32": frame.crc32,
        "rawFrameHex": raw.hex(),
    }


def _post_evidence() -> dict:
    frame, _ = _causal_post_frame()
    raw = frame.raw_bytes
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "length": len(raw),
        "crc32": frame.crc32,
        "rawFrameHex": raw.hex(),
    }


def _causal_post_frame() -> tuple[protocol.Frame, dict]:
    """Return a real-wire successor of the cached ready projection."""

    frame, projection = _wire_fixture()
    value = copy.deepcopy(frame.snapshot.value)
    value["sequence"] = frame.sequence + 1
    value["uptimeMs"] = frame.uptime_ms + 1000
    return protocol.decode_frame(protocol.encode_frame(value)), projection


def _post_crs() -> dict:
    frame, _ = _causal_post_frame()
    return {"boot": frame.boot, "sequence": frame.sequence, "crc32": frame.crc32, "identity": "client"}


def _started_post_frame() -> tuple[protocol.Frame, dict]:
    frame, _ = _wire_fixture()
    value = copy.deepcopy(frame.snapshot.value)
    value["sequence"] = frame.sequence + 1
    value["uptimeMs"] = frame.uptime_ms + 1000
    projection = _started_executor_projection()
    value["executor"] = projection
    return protocol.decode_frame(protocol.encode_frame(value)), projection


def _started_post_evidence() -> dict:
    frame, _ = _started_post_frame()
    raw = frame.raw_bytes
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "length": len(raw),
        "crc32": frame.crc32,
        "rawFrameHex": raw.hex(),
    }


def _started_post_crs() -> dict:
    frame, _ = _started_post_frame()
    return {"boot": frame.boot, "sequence": frame.sequence, "crc32": frame.crc32, "identity": "client"}


def _archive_pre_evidence() -> dict:
    return _pre_evidence()


def _pre_crs(*, started: bool = False) -> dict:
    frame, _ = _wire_fixture(started=started)
    return {"boot": frame.boot, "sequence": frame.sequence, "crc32": frame.crc32, "identity": "client"}


def _test_receipt() -> dict:
    return {
        "action": "executor_click",
        "generation": 1,
        "identity": "client",
        "admittedAnchorEvidenceHash": "a" * 64,
        "admittedAnchorCaptureId": "capture",
        "admittedAnchorCaptureSequence": 1,
        "anchorEvidenceHash": "b" * 64,
        "anchorCaptureId": "capture-two",
        "anchorCaptureSequence": 2,
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


def _test_anchor() -> dict:
    return {
        "action": "executor_click",
        "evidenceHash": "a" * 64,
        "captureId": "capture",
        "captureSequence": 1,
        "identity": "client",
        "generation": 1,
        "targetXid": 257,
        "scene": "executor_mailbox",
        "control": "crowley_executor_button",
        "bounds": [240, 190, 60, 40],
    }


def _started_executor_projection() -> dict:
    value = _pre_executor_projection(started=True)
    value.update(
        {
            "clickReady": False,
            "clickOrdinal": 1,
            "lastClickSource": "button",
            "resultCount": 1,
            "lastResult": {"seq": 1, "kind": "mail_collect", "status": "started"},
        }
    )
    return value


@pytest.mark.parametrize("missing", ["anchorCaptureSequence", "anchorEvidenceHash"])
def test_executor_receipt_rejects_missing_or_untyped_refreshed_witness(missing: str) -> None:
    receipt = _test_receipt()
    receipt.pop(missing)
    receipt["unexpected"] = True
    assert not SessionDaemon._executor_receipt_valid(
        receipt,
        generation=1,
        identity="client",
        anchor=_test_anchor(),
    )


def test_executor_receipt_binds_guardian_lease_epoch_not_controller_generation() -> None:
    receipt = _test_receipt()
    receipt["generation"] = 3
    assert SessionDaemon._executor_receipt_valid(
        receipt,
        generation=3,
        identity="client",
        anchor=_test_anchor(),
    )
    assert not SessionDaemon._executor_receipt_valid(
        receipt,
        generation=7,
        identity="client",
        anchor=_test_anchor(),
    )


def test_real_lua_capture_decodes_executor_and_reaches_exact_ipc_dispatch(tmp_path: Path) -> None:
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
    client.set_mail_count(2)
    client.set_frame_open("mailbox")
    client.publish_agent()
    decoder = PixelDecoder()
    sample = decoder.decode(client.render_agent_capture(), monotonic=1.0, window_identity="executor-wire")
    assert sample.kind == "FRESH" and sample.frame is not None
    executor = sample.frame.snapshot.value["executor"]
    assert executor["queueId"] == _queue()["queueId"]
    assert executor["currentKind"] == "mail_collect"
    assert executor["clickReady"] is True
    assert executor["clickOrdinal"] == 0

    calls: list[dict] = []

    class Owner:
        def execute_executor_click(self, **request):
            calls.append(request)
            return {"result": "dry_run", "attemptId": "attempt-1"}

    controller = SimpleNamespace(
        lock=threading.RLock(),
        status=lambda: SimpleNamespace(generation=7),
    )
    ipc = SessionIPC(controller, Owner())
    request = {
        "command": "executor_click",
        "generation": 7,
        "queueId": executor["queueId"],
        "seq": executor["currentSeq"],
        "confirmQueueId": "",
        "cycleId": "",
    }
    response = ipc.dispatch(request)
    assert response == {"ok": True, "result": "dry_run", "attemptId": "attempt-1"}
    assert calls == [
        {
            "queue_id": executor["queueId"],
            "seq": executor["currentSeq"],
            "confirm_queue_id": "",
            "cycle_id": "",
            "generation": 7,
        }
    ]
    assert ipc.dispatch({**request, "extra": True})["ok"] is False
    assert client.protected_calls == []


def test_executor_cli_main_emits_the_exact_ipc_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict] = []
    config = SimpleNamespace(socket=tmp_path / "session.sock", reload_deadline_seconds=45)
    monkeypatch.setattr(session_cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        session_cli,
        "_socket_request",
        lambda socket, request, timeout: (
            requests.append({"socket": socket, "request": request, "timeout": timeout})
            or {"ok": True, "result": "dry_run", "attemptId": "attempt-1"}
        ),
    )
    assert (
        session_cli.main(
            [
                "--config",
                str(tmp_path / "session.toml"),
                "executor-click",
                "--generation",
                "7",
                "--queue-id",
                "queue-id",
                "--seq",
                "3",
                "--confirm-queue-id",
                "",
                "--cycle-id",
                "",
            ]
        )
        == 0
    )
    assert len(requests) == 1
    assert requests[0]["request"] == {
        "command": "executor_click",
        "generation": 7,
        "queueId": "queue-id",
        "seq": 3,
        "confirmQueueId": "",
        "cycleId": "",
    }


def test_executor_ipc_refuses_a_competing_native_construction_owner() -> None:
    calls: list[dict] = []

    class Owner:
        def _native_construction_flush_expiry(self) -> None:
            return None

        def _native_construction_blocks_competing_entry(self) -> bool:
            return True

        def execute_executor_click(self, **request: object) -> dict:
            calls.append(request)
            raise AssertionError("competing construction owner must gate before handler")

    controller = SimpleNamespace(
        lock=threading.RLock(),
        status=lambda: SimpleNamespace(generation=7),
    )
    response = SessionIPC(controller, Owner()).dispatch(
        {
            "command": "executor_click",
            "generation": 7,
            "queueId": "queue-id",
            "seq": 3,
            "confirmQueueId": "",
            "cycleId": "",
        }
    )
    assert response == {"ok": False, "error": "native construction admission is active"}
    assert calls == []


@pytest.mark.parametrize(
    "final_mutation",
    [
        None,
        "queue",
        "arms",
        "generation",
        "capture-generation",
        "capture-pause",
        "capture-lease",
        "capture-attempt",
        "initial-blocking",
        "capture-blocking",
        "initial-chat",
        "capture-chat",
        "post-capture",
        "delayed-publication",
        "pre-finalize-crash",
        "cleanup-failure",
        "armed-two-message",
    ],
)
def test_unarmed_lua_button_click_composes_through_daemon_and_records_dry_run(
    final_mutation: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default daemon path must witness the actual unarmed Lua callback."""

    queue = _queue()
    if final_mutation == "armed-two-message":
        queue["armed"] = True
        queue["actions"] = [
            {"seq": 1, "kind": "mail_collect", "maxMessages": 2},
            {"seq": 2, "kind": "craft", "recipeID": 2963, "times": 1},
        ]
    client = WowClient(
        faction=TARGET.faction,
        realm=TARGET.realm,
        current_region=TARGET.native_region_id,
        current_region_name=TARGET.region,
        queue=queue,
    )
    client.set_player_name("Arthur")
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(2)
    client.set_frame_open("mailbox")
    if final_mutation == "initial-blocking":
        client.set_blocking_ui_state(static_popup_shown=True)
    elif final_mutation == "initial-chat":
        client.set_chat_edit(active=True, focused=True)
    client.publish_agent()
    if final_mutation == "armed-two-message":
        assert client.ns.Executor.Arm(queue["queueId"]) is True
        client.publish_agent()

    driver = native_driver(tmp_path, monkeypatch)
    profile = driver.profile
    identity = _identity()
    backend = driver.backend
    pointer = [20, 20, identity.child_xid]
    commands: list[list[str]] = []
    backend.pointer_position = lambda **_kwargs: tuple(pointer)  # type: ignore[method-assign]
    backend._focus = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    anchor_paths = [tmp_path / f"executor-anchor-{index}" for index in range(1, 5)]

    def make_anchor(path: Path, sequence: int) -> MeasuredAnchor:
        payload = f"executor-anchor-{sequence}".encode()
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        captured = time.monotonic()
        bounds = (240, 190, 60, 40)
        facts = {
            "action": "executor_click",
            "bounds": ",".join(str(value) for value in bounds),
            "capture": digest,
            "captureMonotonic": str(captured),
            "capturePath": str(path),
            "captureSha256": digest,
            "control": "crowley_executor_button",
            "generation": "1",
            "scene": "executor_mailbox",
            "target": str(identity.child_xid),
            "targetAncestry": str(identity.child_xid),
        }
        return MeasuredAnchor.from_capture(
            action="executor_click",
            x=260,
            y=210,
            identity_fingerprint=identity.fingerprint,
            target_xid=identity.child_xid,
            capture_id=digest,
            capture_sequence=sequence,
            source="native-visual",
            observed_monotonic=captured,
            capture_monotonic=captured,
            scene="executor_mailbox",
            bounds=bounds,
            target_ancestry=(identity.child_xid,),
            capture_sha256=digest,
            generation=1,
            capture_path=str(path),
            facts=tuple(facts.items()),
        )

    anchors = [make_anchor(path, index) for index, path in enumerate(anchor_paths, 1)]
    anchor_iter = iter(anchors)
    anchor_calls = 0

    def provide_anchor(_identity, _action):
        nonlocal anchor_calls
        anchor_calls += 1
        return next(anchor_iter)

    driver.measured_anchor_provider = provide_anchor  # type: ignore[method-assign]
    driver._verify_current_xtest_profile = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    monkeypatch.setattr(pointer_execution.secrets, "randbits", lambda _bits: 0x123456789)
    generation = driver.guardian.arm(driver._input_evidence)
    backend.activate_generation(generation)
    click_dispatch_count = 0

    def run_action(args, *, input_schema=None, **kwargs):
        nonlocal click_dispatch_count
        commands.append(list(args))
        if input_schema == "pointer-motion":
            for index, value in enumerate(args[:-2]):
                if value == "mousemove":
                    pointer[0] = int(args[index + 1])
                    pointer[1] = int(args[index + 2])
                    pointer[2] = identity.child_xid
        elif input_schema == "single-click":
            driver.input_broker.redeem_button_emission_claim(
                kwargs["button_emission_claim"],
                emitted_chain=tuple(args),
                button=PointerButton.LEFT if args[2] == "1" else PointerButton.RIGHT,
                action_generation=kwargs["emission_action_generation"],
                lease_generation=kwargs["emission_lease_generation"],
            )
            # Model the delivered button callback at physical dispatch; Lua
            # bytes are captured only by the subsequent daemon observations.
            if final_mutation != "delayed-publication":
                client.click_executor()
                click_dispatch_count += 1
                if final_mutation == "armed-two-message":
                    if click_dispatch_count == 1:
                        client.update_agent()
                    elif click_dispatch_count == 2:
                        client.set_mail_items([{"money": 0}, {"money": 0}])
                        client.fire("MAIL_INBOX_UPDATE")
                        client.update_agent()
            backend.last_pointer_execution = {"action": "substituted"}

    backend._run = run_action  # type: ignore[method-assign]
    pointer_plan = pointer_execution.plan_human_pointer(
        Point(pointer[0], pointer[1]),
        PointerTarget(
            "crowley_executor_button",
            Rect(*anchors[0].bounds),
            BindingMetadata(
                identity.fingerprint,
                anchors[0].capture_id,
                anchors[0].capture_sequence,
                generation,
                anchors[0].target_xid,
                anchors[0].target_ancestry,
                source=anchors[0].source,
            ),
            padding=1,
        ),
        Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
        seed=0x123456789,
        button=PointerButton.LEFT,
        profile=driver.pointer_motion_profile,
    )
    events = []
    prior = pointer_plan.trajectory.samples[0].point
    timestamp = 1
    for sample in pointer_plan.trajectory.samples[1:]:
        if sample.point != prior:
            events.extend(
                _motion_pair(
                    timestamp,
                    (int(prior.x), int(prior.y)),
                    (int(sample.point.x), int(sample.point.y)),
                )
            )
            timestamp += 1
            prior = sample.point
    endpoint = (int(pointer_plan.endpoint.x), int(pointer_plan.endpoint.y))
    events.extend(
        [
            _event(EventClass.RAW_BUTTON_PRESS, timestamp, detail=1),
            _event(EventClass.BUTTON_PRESS, timestamp + 1, detail=1, root=endpoint, event_xy=endpoint),
            _event(EventClass.RAW_BUTTON_RELEASE, timestamp + 2, detail=1),
            _event(EventClass.BUTTON_RELEASE, timestamp + 3, detail=1, root=endpoint, event_xy=endpoint),
        ]
    )
    monitor = _EventMonitor(events)
    monitor.set_callback(driver.guardian.observe)

    def wait_for_event(predicate, *, timeout):
        del timeout
        for index, event in enumerate(monitor.events):
            before_action = driver.guardian.action
            accepted = monitor.callback(event)
            if accepted is False:
                raise AssertionError(
                    {
                        "index": index,
                        "event": event.event_class.value,
                        "source": event.source_device,
                        "target": event.target_xid,
                        "detail": event.detail,
                        "timestamp": event.timestamp_ms,
                        "valuators": event.valuators,
                        "reason": driver.guardian.revoked_reason,
                        "beforeCanonical": getattr(before_action, "canonical_motion", None),
                        "beforeExpected": getattr(before_action, "expected_motion_trace", None),
                        "beforeSource": getattr(before_action, "source_device", None),
                        "beforeMaster": getattr(before_action, "master_device", None),
                        "beforeEventClasses": getattr(before_action, "event_classes", None),
                    }
                )
            if predicate(monitor.events):
                return
        action = driver.guardian.action
        raise AssertionError(
            {
                "complete": predicate(monitor.events),
                "sequenceIndex": getattr(action, "sequence_index", None),
                "motionIndex": getattr(action, "motion_index", None),
                "expectedMotion": getattr(action, "expected_motion_trace", None),
                "lastError": getattr(driver.guardian, "revoked_reason", ""),
            }
        )

    monitor.wait_for_event = wait_for_event  # type: ignore[method-assign]
    driver._monitor = monitor  # type: ignore[assignment]

    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = generation
    controller._lease_generation = generation
    codex = tmp_path / "codex"
    codex.write_text(f"#!/bin/sh\nprintf '%s\\n' {CLI_VERSION!r}\n", encoding="utf-8")
    codex.chmod(0o700)
    config = SessionConfig(
        profile=profile,
        journal=journal.path,
        socket=tmp_path / "session.sock",
        capture_dir=tmp_path / "captures",
        plan_hash="synthetic-plan-hash",
        plan_json="{}",
        codex_path=str(codex),
        model_neutral_dir=tmp_path / "model-neutral",
    )
    daemon = SessionDaemon(config, controller=controller, driver=driver)

    capture_count = 0

    def capture_client_crop(path: Path, **_kwargs: object) -> Path:
        nonlocal capture_count
        capture_count += 1
        # The first two post-release captures are deliberately stale.  The
        # real Lua callback becomes visible only on the next read, proving the
        # daemon polls its public capture path rather than clicking again.
        if final_mutation == "delayed-publication" and capture_count == 5:
            client.click_executor()
        client.publish_agent()
        client.render_agent_capture().save(path)
        return path

    daemon._capture_client_crop = capture_client_crop  # type: ignore[method-assign]
    precommit_calls: list[int] = []
    original_precommit = pointer_execution.ClickRatePermit.precommit

    def record_precommit(permit, **kwargs):
        precommit_calls.append(1)
        return original_precommit(permit, **kwargs)

    monkeypatch.setattr(pointer_execution.ClickRatePermit, "precommit", record_precommit)
    if final_mutation in {"queue", "arms", "generation"}:
        original_final_admission = daemon._executor_final_admission

        def mutate_final_admission(*args: object, **kwargs: object):
            if final_mutation == "queue":
                client.lua.globals().CROWLEY_QUEUE.queueId = "changed-before-press"
            elif final_mutation == "arms":
                client.lua.globals().CROWLEY_QUEUE.armed = True
            else:
                controller._generation = generation + 1
            return original_final_admission(*args, **kwargs)

        daemon._executor_final_admission = mutate_final_admission  # type: ignore[method-assign]
    elif final_mutation in {"capture-generation", "capture-pause", "capture-lease", "capture-attempt"}:
        original_observe_motion = daemon._observe_motion

        def mutate_during_final_capture(generation_value: int, *args: object, **kwargs: object):
            observed = original_observe_motion(generation_value, *args, **kwargs)
            if kwargs.get("previous") is not None and not kwargs.get("after_release"):
                if final_mutation == "capture-generation":
                    controller._generation = generation + 1
                elif final_mutation == "capture-pause":
                    controller._state = ControllerState.PAUSED
                elif final_mutation == "capture-lease":
                    controller._lease_generation = generation + 1
                else:
                    assert controller._executor_intent is not None
                    controller._executor_intent["attemptId"] = "changed-during-capture"
                    controller._save(last_action="test_executor_attempt_mutation")
            return observed

        daemon._observe_motion = mutate_during_final_capture  # type: ignore[method-assign]
    elif final_mutation == "capture-blocking":
        original_observe_motion = daemon._observe_motion

        def mutate_blocking_during_final_capture(generation_value: int, *args: object, **kwargs: object):
            if kwargs.get("previous") is not None and not kwargs.get("after_release"):
                client.set_blocking_ui_state(static_popup_shown=True)
            return original_observe_motion(generation_value, *args, **kwargs)

        daemon._observe_motion = mutate_blocking_during_final_capture  # type: ignore[method-assign]
    elif final_mutation == "capture-chat":
        original_observe_motion = daemon._observe_motion

        def mutate_chat_during_final_capture(generation_value: int, *args: object, **kwargs: object):
            if kwargs.get("previous") is not None and not kwargs.get("after_release"):
                client.set_chat_edit(active=True, focused=True)
            return original_observe_motion(generation_value, *args, **kwargs)

        daemon._observe_motion = mutate_chat_during_final_capture  # type: ignore[method-assign]
    elif final_mutation == "post-capture":
        original_observe_motion = daemon._observe_motion

        def fail_post_capture(generation_value: int, *args: object, **kwargs: object):
            if kwargs.get("after_release"):
                raise DriverError("executor post-click capture timed out")
            return original_observe_motion(generation_value, *args, **kwargs)

        daemon._observe_motion = fail_post_capture  # type: ignore[method-assign]
    elif final_mutation == "cleanup-failure":

        def fail_guardian_cleanup(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("guardian cleanup failed")

        driver.guardian.finish_action = fail_guardian_cleanup  # type: ignore[method-assign]
    elif final_mutation == "pre-finalize-crash":

        def crash_before_outcome_staging(*_args: object, **_kwargs: object) -> dict:
            raise JournalError("simulated crash before executor outcome staging")

        controller.finalize_executor_click = crash_before_outcome_staging  # type: ignore[method-assign]
    ipc = SessionIPC(controller, daemon)
    request = {
        "command": "executor_click",
        "generation": generation,
        "queueId": queue["queueId"],
        "seq": 1,
        "confirmQueueId": queue["queueId"] if final_mutation == "armed-two-message" else "",
        "cycleId": "",
    }
    try:
        response = ipc.dispatch(request)
    finally:
        if final_mutation != "armed-two-message":
            driver.close()

    if final_mutation == "delayed-publication":
        assert response["ok"] is True, response
        assert response["result"] == "dry_run"
        assert capture_count >= 6
        assert sum("mousedown" in command for command in commands) == 1
        assert client.protected_calls == []
        return
    if final_mutation == "armed-two-message":
        assert response["ok"] is True, response
        assert response["result"] == "started", json.dumps(
            response["archive"].get("postExecutor"), sort_keys=True
        )
        first_post = response["archive"]["postExecutor"]
        assert first_post["currentSeq"] == 1
        assert first_post["currentKind"] == "mail_collect"
        assert first_post["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "started"}
        assert first_post["resultCount"] == 1
        client.set_mail_items([{"money": 0}, {"money": 20}])
        client.fire("MAIL_INBOX_UPDATE")
        client.advance(1)
        client.update_agent()
        # A fresh owner has no durable overall marker or process-local
        # continuation capability.  Even though the real Lua frame is now an
        # in-flight mail_items frame with clickReady=true, it must refuse the
        # initial-ready gate before measuring an anchor or creating intent.
        empty_journal = Journal(tmp_path / "foreign-empty.json")
        empty_controller = Controller(empty_journal, driver=driver, recover_on_restart=False)
        empty_controller._mode = SessionMode.INTERACTION
        empty_controller._state = ControllerState.READY
        empty_controller._generation = generation
        empty_controller._lease_generation = generation
        foreign_daemon = SessionDaemon(config, controller=empty_controller, driver=driver)
        foreign_daemon._capture_client_crop = capture_client_crop  # type: ignore[method-assign]
        foreign = SessionIPC(empty_controller, foreign_daemon).dispatch(
            {
                "command": "executor_click",
                "generation": generation,
                "queueId": queue["queueId"],
                "seq": 1,
                "confirmQueueId": queue["queueId"],
                "cycleId": "",
            }
        )
        assert foreign["ok"] is False
        assert "exact ready phase" in foreign["error"]
        assert empty_controller.executor_continuation_capability() is None
        assert empty_controller.executor_click_checkpoint() is None
        assert client.snapshot_agent()["executor"]["phase"] == "mail_items"
        assert client.snapshot_agent()["executor"]["inFlight"] is True
        assert client.snapshot_agent()["executor"]["clickReady"] is True
        assert anchor_calls == 2
        second_plan = pointer_execution.plan_human_pointer(
            Point(pointer[0], pointer[1]),
            PointerTarget(
                "crowley_executor_button",
                Rect(*anchors[2].bounds),
                BindingMetadata(
                    identity.fingerprint,
                    anchors[2].capture_id,
                    anchors[2].capture_sequence,
                    generation,
                    anchors[2].target_xid,
                    anchors[2].target_ancestry,
                    source=anchors[2].source,
                ),
                padding=1,
            ),
            Rect(0, 0, identity.child_geometry[2], identity.child_geometry[3]),
            seed=0x123456789,
            button=PointerButton.LEFT,
            profile=driver.pointer_motion_profile,
        )
        second_events = []
        prior = second_plan.trajectory.samples[0].point
        timestamp = 1
        for sample in second_plan.trajectory.samples[1:]:
            if sample.point != prior:
                second_events.extend(
                    _motion_pair(
                        timestamp,
                        (int(prior.x), int(prior.y)),
                        (int(sample.point.x), int(sample.point.y)),
                    )
                )
                timestamp += 1
                prior = sample.point
        endpoint = (int(second_plan.endpoint.x), int(second_plan.endpoint.y))
        second_events.extend(
            [
                _event(EventClass.RAW_BUTTON_PRESS, timestamp, detail=1),
                _event(EventClass.BUTTON_PRESS, timestamp + 1, detail=1, root=endpoint, event_xy=endpoint),
                _event(EventClass.RAW_BUTTON_RELEASE, timestamp + 2, detail=1),
                _event(EventClass.BUTTON_RELEASE, timestamp + 3, detail=1, root=endpoint, event_xy=endpoint),
            ]
        )
        monitor.events = second_events
        continuation = ipc.dispatch(
            {
                "command": "executor_click",
                "generation": generation,
                "queueId": queue["queueId"],
                "seq": 1,
                "confirmQueueId": queue["queueId"],
                "cycleId": "",
            }
        )
        assert continuation["ok"] is True, continuation
        assert continuation["result"] == "succeeded"
        second_post = continuation["archive"]["postExecutor"]
        assert second_post["currentSeq"] == 2
        assert second_post["currentKind"] == "craft"
        assert second_post["lastResult"] == {
            "seq": 1,
            "kind": "mail_collect",
            "status": "succeeded",
        }
        assert second_post["resultCount"] == first_post["resultCount"]
        assert client.protected_calls == ["AutoLootMailItem", "AutoLootMailItem"]
        driver.close()
        return
    if final_mutation == "pre-finalize-crash":
        assert response["ok"] is False, response
        assert sum("mousedown" in command for command in commands) == 1
        current = journal.read()["current"]["executor"]
        assert current["intent"] is not None
        intent = current["intent"]
        raw = bytes.fromhex(intent["preEvidence"]["rawFrameHex"])
        frame = protocol.decode_frame(raw)
        assert {
            "boot": frame.boot,
            "sequence": frame.sequence,
            "crc32": frame.crc32,
            "identity": intent["preCrs"]["identity"],
        } == intent["preCrs"]
        assert frame.snapshot.value["executor"] == intent["preExecutor"]
        recovered = Controller(
            journal,
            driver=SimpleNamespace(guardian=SimpleNamespace(armed=False, generation=0)),
            recover_on_restart=True,
        )
        assert recovered.status().state is ControllerState.BLOCKED
        assert journal.read()["archives"]["executor"]["count"] == 0
        return
    if final_mutation is not None:
        assert response["ok"] is False, response
        if final_mutation == "post-capture":
            assert any("mousedown" in command for command in commands)
            assert client.execution(queue["queueId"])["results"][-1]["status"] == "dry_run"
            archived = journal.read()["archives"]["executor"]
            assert archived["count"] == 1, response
            artifacts = list((tmp_path / ".session.json.archives" / "executor").glob("*.json"))
            assert len(artifacts) == 1
            assert json.loads(artifacts[0].read_text())["receipt"]["action"] == "executor_click"
        elif final_mutation == "cleanup-failure":
            assert any("mousedown" in command for command in commands)
            artifact = next((tmp_path / ".session.json.archives" / "executor").glob("*.json"))
            assert json.loads(artifact.read_text())["receipt"]["guardianCompleted"] is False
        else:
            assert not any("mousedown" in command for command in commands)
            assert client.execution(queue["queueId"])["results"] == []
            if final_mutation in {
                "capture-generation",
                "capture-pause",
                "capture-lease",
                "capture-attempt",
            }:
                assert precommit_calls == []
        assert client.protected_calls == []
        return
    assert response["ok"] is True, response
    assert response["result"] == "dry_run"
    assert commands
    assert client.protected_calls == []
    assert client.execution(queue["queueId"])["results"][-1]["status"] == "dry_run"
    archive = response["archive"]
    assert archive["receipt"]["action"] == "executor_click"
    assert archive["receipt"]["clickPrecommitMonotonicNs"] > 0
    assert archive["receipt"]["rateRecordMonotonicNs"] > archive["receipt"]["clickPrecommitMonotonicNs"]
    assert archive["receipt"]["guardianCompleted"] is True
    assert archive["postCrs"]["sequence"] > archive["preCrs"]["sequence"]
    assert archive["preExecutor"]["resultCount"] == 0
    assert archive["postExecutor"]["clickOrdinal"] == archive["preExecutor"]["clickOrdinal"] + 1
    for evidence in (archive["preEvidence"], archive["postEvidence"]):
        assert evidence["length"] == 2048
        assert len(evidence["rawFrameHex"]) == 4096
        assert hashlib.sha256(bytes.fromhex(evidence["rawFrameHex"])).hexdigest() == evidence["sha256"]
    assert backend.last_pointer_execution == {"action": "substituted"}


def test_executor_intent_is_archived_once_and_restart_blocks_replay(tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    attempt = controller.begin_executor_click(
        queue_id="beta-normal-alliance-1760000000-executor",
        seq=1,
        generation=1,
        lease_generation=2,
        cycle_id="",
        pre_crs=_pre_crs(),
        pre_ordinal=0,
        arms={"published": True, "session": True},
        anchor=_test_anchor(),
        confirmation="beta-normal-alliance-1760000000-executor",
        pre_executor=_pre_executor_projection(),
        pre_evidence=_pre_evidence(),
    )
    assert journal.read()["current"]["executor"]["intent"]["attemptId"] == attempt["attemptId"]
    with pytest.raises(ValueError, match="complete post evidence"):
        controller.finalize_executor_click(
            attempt_id=attempt["attemptId"],
            result="succeeded",
            post_crs=_post_crs(),
            receipt=None,
            terminal_action=True,
            post_executor=_pre_executor_projection(),
            pre_evidence=_archive_pre_evidence(),
            post_evidence=_post_evidence(),
        )
    assert journal.read()["current"]["executor"]["intent"]["attemptId"] == attempt["attemptId"]
    archived = controller.finalize_executor_click(
        attempt_id=attempt["attemptId"],
        result="succeeded",
        post_crs=_post_crs(),
        receipt={**_test_receipt(), "generation": 2},
        terminal_action=True,
        post_executor=_pre_executor_projection(),
        pre_evidence=_archive_pre_evidence(),
        post_evidence=_post_evidence(),
    )
    assert archived["outcomeId"]
    assert journal.read()["current"]["executor"] is None
    assert journal.read()["archives"]["executor"]["count"] == 1

    # A fresh owner cannot reconstruct permission from an unfinished attempt.
    controller._state = ControllerState.READY
    controller._executor_intent = attempt
    controller._executor_overall = {
        "queueId": attempt["queueId"],
        "seq": attempt["seq"],
        "cycleId": "",
        "generation": attempt["generation"],
        "leaseGeneration": attempt["leaseGeneration"],
        "status": "in_flight",
    }
    controller._save(last_action="test_seed_executor_restart")
    restarted = Controller(journal, driver=driver, recover_on_restart=True)
    assert restarted.status().state is ControllerState.BLOCKED
    assert restarted.executor_click_checkpoint() is not None


def test_executor_intent_checkpoint_failure_fails_closed_before_input(tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2

    def fail_save(**_kwargs: object) -> None:
        raise JournalError("checkpoint write failed")

    controller._save = fail_save  # type: ignore[method-assign]
    values = {
        "queue_id": "beta-normal-alliance-1760000000-executor",
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": False, "session": False},
        "anchor": _test_anchor(),
        "confirmation": "",
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    with pytest.raises(JournalError, match="checkpoint write failed"):
        controller.begin_executor_click(**values)
    assert controller.executor_click_checkpoint() is None
    assert journal.read()["current"]["executor"] is None
    with pytest.raises(RuntimeError, match="blocked after restart"):
        controller.begin_executor_click(**values)


def test_executor_archive_write_failure_preserves_one_intent_for_retry(tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    values = {
        "queue_id": "beta-normal-alliance-1760000000-executor",
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": False, "session": False},
        "anchor": _test_anchor(),
        "confirmation": "",
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    attempt = controller.begin_executor_click(**values)
    original_append = journal.append_archive_bundle

    def fail_archive(*_args: object, **_kwargs: object) -> dict:
        raise JournalError("archive publication failed")

    journal.append_archive_bundle = fail_archive  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="archive publication failed"):
        controller.finalize_executor_click(
            attempt_id=attempt["attemptId"],
            result="dry_run",
            post_crs=_post_crs(),
            receipt={**_test_receipt(), "generation": 2},
            terminal_action=True,
            post_executor=_pre_executor_projection(),
            pre_evidence=_archive_pre_evidence(),
            post_evidence=_post_evidence(),
        )
    staged = controller.executor_click_checkpoint()
    assert staged is not None and staged["staged"] is not None
    assert journal.read()["archives"]["executor"]["count"] == 0
    journal.append_archive_bundle = original_append  # type: ignore[method-assign]
    fresh = Controller(journal, driver=driver, recover_on_restart=True)
    assert fresh.executor_click_checkpoint() is None
    assert journal.read()["archives"]["executor"]["count"] == 1


def test_executor_staging_write_failure_stays_unresolved_for_fresh_owner(tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    values = {
        "queue_id": "beta-normal-alliance-1760000000-executor",
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": False, "session": False},
        "anchor": _test_anchor(),
        "confirmation": "",
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    attempt = controller.begin_executor_click(**values)
    original_save = controller._save

    def fail_staging(*, last_action: str = "") -> None:
        if last_action == "executor_outcome_staged":
            raise JournalError("executor staging write failed")
        original_save(last_action=last_action)

    controller._save = fail_staging  # type: ignore[method-assign]
    with pytest.raises(JournalError, match="executor staging write failed"):
        controller.finalize_executor_click(
            attempt_id=attempt["attemptId"],
            result="dry_run",
            post_crs=_post_crs(),
            receipt={**_test_receipt(), "generation": 2},
            terminal_action=True,
            post_executor=_pre_executor_projection(),
            pre_evidence=_archive_pre_evidence(),
            post_evidence=_post_evidence(),
        )
    current = journal.read()
    assert current["current"]["executor"]["intent"]["attemptId"] == attempt["attemptId"]
    assert current["archives"]["executor"]["count"] == 0
    fresh = Controller(journal, driver=driver, recover_on_restart=True)
    assert fresh.status().state is ControllerState.BLOCKED
    assert journal.read()["archives"]["executor"]["count"] == 0


@pytest.mark.parametrize("boundary", ["pending", "manifest", "artifact", "checkpoint"])
def test_executor_bundle_boundaries_recover_with_a_fresh_owner(boundary: str, tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    values = {
        "queue_id": "beta-normal-alliance-1760000000-executor",
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": False, "session": False},
        "anchor": _test_anchor(),
        "confirmation": "",
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    attempt = controller.begin_executor_click(**values)
    finalize = {
        "attempt_id": attempt["attemptId"],
        "result": "dry_run",
        "post_crs": _post_crs(),
        "receipt": {**_test_receipt(), "generation": 2},
        "terminal_action": True,
        "post_executor": _pre_executor_projection(),
        "pre_evidence": _archive_pre_evidence(),
        "post_evidence": _post_evidence(),
    }
    if boundary == "pending":
        original = journal._write_pending_artifact

        def fail_pending(*_args: object, **_kwargs: object) -> None:
            raise JournalError("pending marker failed")

        journal._write_pending_artifact = fail_pending  # type: ignore[method-assign]
    elif boundary == "manifest":
        original = journal._write_json_artifact

        def fail_manifest(*_args: object, **_kwargs: object) -> None:
            raise JournalError("manifest failed")

        journal._write_json_artifact = fail_manifest  # type: ignore[method-assign]
    elif boundary == "artifact":
        original = journal._write_archive_artifact

        def fail_artifact(*_args: object, **_kwargs: object) -> None:
            raise JournalError("archive artifact failed")

        journal._write_archive_artifact = fail_artifact  # type: ignore[method-assign]
    else:
        original = journal.write
        writes = 0

        def fail_checkpoint(value: dict[str, object]) -> None:
            nonlocal writes
            writes += 1
            if writes >= 2:
                raise JournalError("checkpoint failed")
            original(value)

        journal.write = fail_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(JournalError):
            controller.finalize_executor_click(**finalize)
    finally:
        if boundary == "pending":
            journal._write_pending_artifact = original  # type: ignore[method-assign]
        elif boundary == "manifest":
            journal._write_json_artifact = original  # type: ignore[method-assign]
        elif boundary == "artifact":
            journal._write_archive_artifact = original  # type: ignore[method-assign]
        else:
            journal.write = original  # type: ignore[method-assign]

    # A later operator hold must survive outcome replay.  The staged payload,
    # not these test arguments, is the fresh owner's recovery authority.
    held = journal.read()
    held.update(
        {
            "state": "PAUSED",
            "resumeState": "PAUSED",
            "stopRequested": True,
            "restartRequiresResume": True,
            "blocker": "operator paused after staged executor outcome",
        }
    )
    journal.write(held)
    fresh = Controller(journal, driver=driver, recover_on_restart=True)
    assert fresh.status().state is ControllerState.PAUSED
    assert fresh.status().stop_requested is True
    assert fresh.executor_click_checkpoint() is None
    assert journal.read()["archives"]["executor"]["count"] == 1
    artifact = next((tmp_path / ".session.json.archives" / "executor").glob("*.json"))
    archived = json.loads(artifact.read_text())
    assert archived["outcomeId"] == hashlib.sha256(f"executor|{attempt['attemptId']}".encode()).hexdigest()
    assert archived["attemptId"] == attempt["attemptId"]
    assert archived["result"] == "dry_run"
    assert archived["receipt"] == finalize["receipt"]


def test_started_mail_action_requires_private_same_process_continuation_capability(
    tmp_path: Path,
) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    values = {
        "queue_id": "beta-normal-alliance-1760000000-executor",
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": True, "session": True},
        "anchor": _test_anchor(),
        "confirmation": "beta-normal-alliance-1760000000-executor",
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    initial = controller.begin_executor_click(**values)
    started = _started_executor_projection()
    controller.finalize_executor_click(
        attempt_id=initial["attemptId"],
        result="started",
        post_crs=_started_post_crs(),
        receipt={**_test_receipt(), "generation": 2},
        terminal_action=False,
        post_executor=started,
        pre_evidence=_archive_pre_evidence(),
        post_evidence=_started_post_evidence(),
    )
    capability = controller.executor_continuation_capability()
    assert isinstance(capability, str) and capability
    assert controller.executor_click_checkpoint()["overall"]["status"] == "in_flight"

    with pytest.raises(RuntimeError, match="another executor mail action owns"):
        controller.begin_executor_click(**values)
    continuation = controller.begin_executor_click(
        **{
            **values,
            "pre_crs": _pre_crs(started=True),
            "pre_executor": started,
            "pre_ordinal": 1,
            "pre_evidence": _pre_evidence(started=True),
            "continuation_capability": capability,
        }
    )
    assert continuation["attemptId"] != initial["attemptId"]


def test_executor_driver_requires_final_admission_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = native_driver(tmp_path, monkeypatch)
    try:
        with pytest.raises(DriverError, match="final admission callback"):
            driver.run("executor_click", 1)
    finally:
        driver.close()


def test_executor_frame_evidence_rejects_missing_crc_binding() -> None:
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
    raw = bytes(client.agent_frame)
    frame = protocol.decode_frame(raw)
    key = (frame.boot, frame.sequence, "executor-wire")
    daemon = object.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(protocol_contract=None)
    daemon._motion_evidence = {key: raw}
    daemon._motion_crs = {}
    daemon._motion_snapshots = {key: frame.snapshot.value}
    motion = SimpleNamespace(boot=frame.boot, sequence=frame.sequence, identity_fingerprint="executor-wire")
    with pytest.raises(DriverError, match="does not match its decoded tuple"):
        daemon._executor_frame_evidence(motion)


def test_armed_empty_confirmation_refuses_before_anchor_or_input() -> None:
    daemon = object.__new__(SessionDaemon)
    daemon._motion_intent_lock = threading.RLock()
    daemon._motion_deadline_local = threading.local()
    daemon._native_clock = time.monotonic
    daemon.controller = SimpleNamespace(
        lock=threading.RLock(),
        executor_continuation_capability=lambda: None,
        executor_click_checkpoint=lambda: None,
    )
    daemon._invalidate_motion_admission = lambda: None
    daemon._require_motion_interaction = lambda _generation, **_kwargs: 1
    frame = SimpleNamespace(
        boot="boot", sequence=1, identity_fingerprint="identity", chat_focused=False, mail_open=True
    )
    daemon._observe_motion = lambda *_args, **_kwargs: frame
    daemon._motion_snapshots = {
        ("boot", 1, "identity"): {
            "blockingUI": {
                "staticPopupAvailable": True,
                "staticPopupShown": False,
                "gameMenuAvailable": True,
                "gameMenuShown": False,
            }
        }
    }
    daemon._executor_snapshot_for_frame = lambda _frame: {
        "availability": "valid",
        "queueId": "queue-id",
        "currentSeq": 1,
        "currentKind": "mail_collect",
        "house": TARGET.house,
        "character": "Arthur",
        "buttonName": "CrowleyTradeExecutorButton",
        "buttonVisible": True,
        "clickReady": True,
        "phase": "ready",
        "inFlight": False,
        "blocked": False,
        "ambiguous": False,
        "publishedArmed": True,
        "sessionArmed": True,
    }
    daemon.config = SimpleNamespace(profile=SimpleNamespace(house=TARGET.house, character="Arthur"))
    anchors: list[object] = []
    daemon.driver = SimpleNamespace(measure_pointer_anchor=lambda *_args, **_kwargs: anchors.append(True))
    with pytest.raises(DriverError, match="requires exact queue confirmation"):
        daemon.execute_executor_click(
            queue_id="queue-id",
            seq=1,
            confirm_queue_id="",
            cycle_id="",
            generation=1,
        )
    assert anchors == []


def test_foreign_inflight_mail_refuses_before_anchor_or_intent() -> None:
    daemon = object.__new__(SessionDaemon)
    daemon._native_clock = time.monotonic
    daemon.controller = SimpleNamespace(
        lock=threading.RLock(),
        executor_continuation_capability=lambda: None,
        executor_click_checkpoint=lambda: {
            "intent": None,
            "overall": {
                "queueId": "foreign-queue",
                "seq": 1,
                "cycleId": "",
                "generation": 1,
                "leaseGeneration": 1,
                "status": "in_flight",
            },
            "staged": None,
        },
    )
    daemon._executor_continuation_capability = None
    daemon._require_motion_interaction = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("foreign ownership must refuse before motion admission")
    )
    daemon._observe_motion = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("foreign ownership must refuse before capture")
    )
    daemon.driver = SimpleNamespace(
        measure_pointer_anchor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreign ownership must refuse before anchor")
        )
    )
    with pytest.raises(DriverError, match="another executor mail action owns"):
        daemon.execute_executor_click(
            queue_id="local-queue",
            seq=1,
            confirm_queue_id="",
            cycle_id="",
            generation=1,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("queueId", "other-queue"),
        ("seq", 2),
        ("cycleId", "other-cycle"),
        ("generation", 2),
        ("leaseGeneration", 2),
    ],
)
def test_executor_overall_drift_refuses_before_capture_anchor_or_input(field: str, value: object) -> None:
    daemon = object.__new__(SessionDaemon)
    daemon._motion_intent_lock = threading.RLock()
    daemon._motion_deadline_local = threading.local()
    daemon._native_clock = time.monotonic
    overall = {
        "queueId": "queue-id",
        "seq": 1,
        "cycleId": "",
        "generation": 1,
        "leaseGeneration": 1,
        "status": "in_flight",
    }
    overall[field] = value
    daemon._executor_continuation_capability = "cap"
    daemon.controller = SimpleNamespace(
        lock=threading.RLock(),
        executor_continuation_capability=lambda: "cap",
        executor_click_checkpoint=lambda: {"intent": None, "overall": overall, "staged": None},
        status=lambda: SimpleNamespace(lease_generation=1),
    )
    daemon._require_motion_interaction = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("overall drift must refuse before motion admission")
    )
    daemon._observe_motion = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("overall drift must refuse before capture")
    )
    daemon.driver = SimpleNamespace(
        measure_pointer_anchor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("overall drift must refuse before anchor")
        )
    )
    with pytest.raises(DriverError, match="another executor mail action owns"):
        daemon.execute_executor_click(
            queue_id="queue-id",
            seq=1,
            confirm_queue_id="queue-id",
            cycle_id="",
            generation=1,
        )


@pytest.mark.parametrize("field", ["queueId", "seq", "cycleId", "generation", "leaseGeneration"])
def test_controller_overall_drift_rejects_matching_private_capability(field: str, tmp_path: Path) -> None:
    guardian = SimpleNamespace(armed=True, generation=2)
    driver = SimpleNamespace(guardian=guardian)
    journal = Journal(tmp_path / "session.json")
    controller = Controller(journal, driver=driver, recover_on_restart=False)
    controller._mode = SessionMode.INTERACTION
    controller._state = ControllerState.READY
    controller._generation = 1
    controller._lease_generation = 2
    values = {
        "queue_id": _queue()["queueId"],
        "seq": 1,
        "generation": 1,
        "lease_generation": 2,
        "cycle_id": "",
        "pre_crs": _pre_crs(),
        "pre_ordinal": 0,
        "arms": {"published": True, "session": True},
        "anchor": _test_anchor(),
        "confirmation": _queue()["queueId"],
        "pre_executor": _pre_executor_projection(),
        "pre_evidence": _pre_evidence(),
    }
    controller.begin_executor_click(**values)
    capability = controller.executor_continuation_capability()
    assert capability is None
    # A first action is not continuation-authorized; seed a valid overall and
    # private capability exactly as a witnessed started mail action would.
    controller._executor_intent = None
    controller._executor_capability = "cap"
    controller._executor_overall = {
        "queueId": values["queue_id"],
        "seq": values["seq"],
        "cycleId": values["cycle_id"],
        "generation": values["generation"],
        "leaseGeneration": values["lease_generation"],
        "status": "in_flight",
    }
    controller._executor_overall[field] = {
        "queueId": "other",
        "seq": 2,
        "cycleId": "other",
        "generation": 3,
        "leaseGeneration": 4,
    }[field]
    with pytest.raises(RuntimeError, match="another executor mail action owns"):
        controller.begin_executor_click(
            **values,
            continuation_capability="cap",
        )


def test_malformed_nested_executor_checkpoint_is_not_treated_as_absent(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "session.json")
    value = journal.read()
    value["current"]["executor"] = {"intent": {}, "overall": None, "staged": None}
    journal.write(value)
    with pytest.raises(JournalError, match="executor intent schema"):
        Controller(journal, driver=SimpleNamespace(guardian=SimpleNamespace()), recover_on_restart=False)


def test_real_lua_mail_frames_cover_started_continuation_and_mixed_successor() -> None:
    queue = _queue()
    queue["armed"] = True
    queue["actions"] = [
        {"seq": 1, "kind": "mail_collect", "maxMessages": 2},
        {"seq": 2, "kind": "craft", "recipeID": 2963, "times": 1},
    ]
    client = WowClient(
        faction=TARGET.faction,
        realm=TARGET.realm,
        current_region=TARGET.native_region_id,
        current_region_name=TARGET.region,
        queue=queue,
    )
    client.set_player_name("Arthur")
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(2)
    client.set_auto_postconditions(False)
    client.set_frame_open("mailbox")
    client.publish_agent()
    assert client.ns.Executor.Arm(queue["queueId"]) is True
    ready = client.snapshot_agent()["executor"]
    client.click_executor()
    client.update_agent()
    started = client.snapshot_agent()["executor"]
    assert started["resultCount"] == ready["resultCount"] + 1
    assert started["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "started"}
    assert started["phase"] == "mail_wait"
    assert SessionDaemon._executor_delivery_valid(
        ready, started, queue_id=queue["queueId"], seq=1, continuation=False
    )

    client.set_mail_items([{"money": 0}, {"money": 20}])
    client.fire("MAIL_INBOX_UPDATE")
    client.advance(1)
    client.update_agent()
    mail_items = client.snapshot_agent()["executor"]
    assert mail_items["phase"] == "mail_items" and mail_items["clickReady"] is True
    assert mail_items["resultCount"] == started["resultCount"]
    client.click_executor()
    client.set_mail_items([{"money": 0}, {"money": 0}])
    client.fire("MAIL_INBOX_UPDATE")
    client.update_agent()
    terminal = client.snapshot_agent()["executor"]
    assert terminal["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "succeeded"}
    assert terminal["currentSeq"] == 2 and terminal["currentKind"] == "craft"
    assert terminal["resultCount"] == mail_items["resultCount"]
    assert SessionDaemon._executor_delivery_valid(
        mail_items, terminal, queue_id=queue["queueId"], seq=1, continuation=True
    )
    assert client.protected_calls == ["AutoLootMailItem", "AutoLootMailItem"]
