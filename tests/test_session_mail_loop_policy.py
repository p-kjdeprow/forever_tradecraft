"""Independent pure-policy checks for the future ACK-bounded mail loop."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from crowley.session.mail_loop import (
    MailLoopBinding,
    MailLoopBoundary,
    MailLoopPolicyError,
    MailOperationBounds,
    admit_mail_cycle,
)
from crowley.session.protocol import decode_frame, encode_frame

from .wow_harness import WowClient

QUEUE_ID = "beta-normal-horde-Tester-1760000000-abcd"
HOUSE = "beta-normal-horde"
CHARACTER = "Tester"
FRAME_WALL = 1_760_000_000
FRAME_DUE = 1_760_000_800
ACTUAL_START = 1_759_999_900


def _client_snapshot(*, successor: bool = False):
    queue = {
        "format": 1,
        "queueId": QUEUE_ID,
        "generatedAt": 1_759_990_000,
        "expiresAt": 1_760_000_600,
        "house": HOUSE,
        "character": CHARACTER,
        "armed": False,
        "actions": [
            {"seq": 1, "kind": "mail_collect", "maxMessages": 2},
            *([{"seq": 2, "kind": "mail_collect", "maxMessages": 99}] if successor else []),
        ],
    }
    client = WowClient(
        queue=queue,
        initial_savedvars={"lastSweepStart": ACTUAL_START, "scans": {}},
    )
    client.set_canonical_plan()
    client.login()
    # The real addon reads this acknowledgement from the loaded Data.lua
    # projection; the policy boundary supplies only the controller's status UID.
    client.lua.globals().CROWLEY_DATA.ackedScans = client._to_lua(["scan-1"])
    client.lua.globals().CROWLEY_DATA.generatedAt = FRAME_WALL
    client.set_mail_count(2)
    client.set_frame_open("ah")
    client.update_agent()
    return client, decode_frame(client.agent_frame).snapshot


def _binding(**changes) -> MailLoopBinding:
    value = MailLoopBinding(
        enabled=True,
        generation=7,
        queue_id=QUEUE_ID,
        seq=1,
        house=HOUSE,
        character=CHARACTER,
    )
    return replace(value, **changes)


def _boundary(snapshot=None, **changes) -> MailLoopBoundary:
    actual_start = ACTUAL_START
    now = FRAME_WALL
    if snapshot is not None:
        now = snapshot.value["wallTime"]
    value = MailLoopBoundary(
        generation=7,
        session_mode="repeat",
        repeat_phase="WAIT_DUE",
        ack_loaded_uid="scan-1",
        status_last_uid="scan-1",
        seen_ack_uids=frozenset(),
        unresolved_action=False,
        reconciliation_pending=False,
        stop_requested=False,
        closeout_pending=False,
        last_actual_start_at=actual_start,
        now=now,
    )
    return replace(value, **changes)


def _roundtrip(value):
    return decode_frame(encode_frame(value)).snapshot


def _bounds(**changes) -> MailOperationBounds:
    value = MailOperationBounds(
        route_to_mailbox_seconds=2.0,
        world_click_seconds=1.0,
        mail_click_seconds=1.0,
        mail_postcondition_seconds=2.0,
        route_to_auctioneer_seconds=3.0,
        ah_reopen_seconds=2.0,
        safety_margin_seconds=4.0,
    )
    return replace(value, **changes)


def test_actual_lua_snapshot_admits_exact_current_mail_without_button_requirement():
    client, snapshot = _client_snapshot()
    executor = snapshot.value["executor"]
    assert snapshot.value["schema"] == 6
    assert snapshot.value["ahOpen"] is True
    assert snapshot.value["loaded"]["ackedIds"] == ["scan-1"]
    assert snapshot.value["loaded"]["generatedAt"] == FRAME_WALL
    assert snapshot.value["scanner"]["phase"] == "scheduled"
    assert snapshot.value["scanner"]["dueAt"] == FRAME_DUE
    assert executor["currentSeq"] == 1
    assert executor["currentKind"] == "mail_collect"
    assert executor["phase"] == "ready"
    assert executor["workDone"] == 0 and executor["workTotal"] == 2
    assert executor["clickReady"] is False

    decision = admit_mail_cycle(snapshot, _binding(), _boundary(snapshot), _bounds())

    assert decision.admitted is True
    assert decision.deferred is False
    assert decision.reason == "admitted"
    assert decision.queue_id == QUEUE_ID
    assert decision.admitted_seq == 1
    assert decision.ack_uid == "scan-1"
    assert decision.mail_units == 2
    assert decision.effective_due_at == FRAME_DUE
    assert decision.budget is not None
    assert decision.budget.total_seconds == 18.0
    assert decision.latest_start_at == FRAME_DUE - 18.0
    assert decision.completion_deadline_at == FRAME_DUE - 4.0
    assert client.protected_calls == []


def test_codec_valid_ready_scanner_snapshot_is_admitted():
    _, snapshot = _client_snapshot()
    snapshot.value["scanner"].update(
        phase="ready",
        activeSweepId="",
        startedAt=0,
        dueAt=0,
        ordinal=0,
    )
    snapshot = _roundtrip(snapshot.value)
    assert snapshot.value["scanner"]["phase"] == "ready"
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(snapshot), _bounds())
    assert decision.admitted is True
    assert decision.reason == "admitted"


def test_disabled_binding_is_dry_run_by_default():
    _, snapshot = _client_snapshot()
    decision = admit_mail_cycle(snapshot, MailLoopBinding(), _boundary(), _bounds())
    assert decision.admitted is False
    assert decision.deferred is True
    assert decision.reason == "disabled"
    assert decision.admitted_seq == 0


def test_disabled_semantics_do_not_bypass_malformed_inputs():
    _, snapshot = _client_snapshot()
    with pytest.raises(MailLoopPolicyError, match="disabled binding"):
        admit_mail_cycle(
            snapshot,
            MailLoopBinding(queue_id="stale"),
            _boundary(),
            _bounds(),
        )
    for malformed in (
        MailLoopBinding(generation=False),
        MailLoopBinding(seq=0.0),
        MailLoopBinding(queue_id=None),
    ):
        with pytest.raises(MailLoopPolicyError, match="binding"):
            admit_mail_cycle(snapshot, malformed, _boundary(), _bounds())
    with pytest.raises(MailLoopPolicyError, match="bounds"):
        admit_mail_cycle(snapshot, MailLoopBinding(), _boundary(), _bounds(route_to_mailbox_seconds=0))
    with pytest.raises(MailLoopPolicyError, match="immutable frozenset"):
        admit_mail_cycle(snapshot, MailLoopBinding(), _boundary(seen_ack_uids=[]), _bounds())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"repeat_phase": "REOPEN"}, "not_wait_due"),
        ({"session_mode": "interaction"}, "session_not_repeat"),
        ({"status_last_uid": "scan-2"}, "ack_status_mismatch"),
        ({"status_last_uid": ""}, "ack_missing"),
        ({"seen_ack_uids": frozenset({"scan-1"})}, "ack_already_seen"),
        ({"unresolved_action": True}, "unresolved_boundary"),
    ],
    ids=["repeat-phase", "session-mode", "ack-wire", "ack-missing", "ack-once", "unresolved"],
)
def test_boundary_and_ack_gates_defer(change, reason):
    _, snapshot = _client_snapshot()
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(**change), _bounds())
    assert decision.deferred is True
    assert decision.reason == reason


@pytest.mark.parametrize(
    "flag",
    ["unresolved_action", "reconciliation_pending", "stop_requested", "closeout_pending"],
)
def test_every_unresolved_boundary_flag_defers(flag):
    _, snapshot = _client_snapshot()
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(**{flag: True}), _bounds())
    assert decision.deferred is True
    assert decision.reason == "unresolved_boundary"


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 8},
        {"queue_id": "other-queue"},
        {"seq": 2},
        {"house": "other-house"},
        {"character": "Other"},
    ],
    ids=["generation", "queue", "sequence", "house", "character"],
)
def test_process_binding_and_identity_are_exact(change):
    _, snapshot = _client_snapshot()
    decision = admit_mail_cycle(snapshot, _binding(**change), _boundary(), _bounds())
    assert decision.deferred is True
    expected = "generation_mismatch" if "generation" in change else "executor_not_initial_mail"
    assert decision.reason == expected


@pytest.mark.parametrize("field", ["pending", "scanner"])
def test_frame_pending_and_scanner_are_authoritative(field):
    _, snapshot = _client_snapshot()
    if field == "pending":
        snapshot.value["pending"]["ids"] = ["pending-1"]
        snapshot.value["pending"]["count"] = 1
        expected = "pending_work"
    else:
        snapshot.value["scanner"].update(
            phase="item",
            activeSweepId="",
            startedAt=FRAME_WALL - 100,
            dueAt=FRAME_WALL + 800,
            ordinal=1,
        )
        expected = "scanner_not_ready"
    snapshot = _roundtrip(snapshot.value)
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(), _bounds())
    assert decision.deferred is True
    assert decision.reason == expected


@pytest.mark.parametrize("field", ["house", "character"])
def test_frame_context_identity_must_match_executor_and_binding(field):
    _, snapshot = _client_snapshot()
    if field == "house":
        snapshot.value["house"]["key"] = "beta-normal-alliance"
    else:
        snapshot.value["context"]["character"] = "Other"
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(), _bounds())
    assert decision.deferred is True
    assert decision.reason == "executor_not_initial_mail"


def test_missing_start_and_clock_before_frame_defer():
    _, snapshot = _client_snapshot()
    missing = admit_mail_cycle(snapshot, _binding(), _boundary(last_actual_start_at=None), _bounds())
    assert missing.deferred is True and missing.reason == "start_missing"
    old_clock = admit_mail_cycle(snapshot, _binding(), _boundary(now=FRAME_WALL - 1), _bounds())
    assert old_clock.deferred is True and old_clock.reason == "clock_before_frame"


def test_snapshot_type_and_boundary_collections_are_strict():
    _, snapshot = _client_snapshot()
    with pytest.raises(MailLoopPolicyError, match="Snapshot type"):
        admit_mail_cycle(SimpleNamespace(value=snapshot.value), _binding(), _boundary(), _bounds())
    for bad in (["scan-1"], ("scan-1",), "scan-1"):
        with pytest.raises(MailLoopPolicyError, match="immutable frozenset"):
            admit_mail_cycle(snapshot, _binding(), _boundary(seen_ack_uids=bad), _bounds())


def test_controller_ack_proof_is_distinct_from_raw_loaded_ack():
    _, snapshot = _client_snapshot()
    # ack_loaded_uid is controller evidence and status_last_uid is the
    # controller status; neither substitutes for frame loaded.ackedIds.
    missing = admit_mail_cycle(snapshot, _binding(), _boundary(ack_loaded_uid=""), _bounds())
    assert missing.deferred is True and missing.reason == "ack_missing"
    mismatched = admit_mail_cycle(snapshot, _binding(), _boundary(ack_loaded_uid="scan-2"), _bounds())
    assert mismatched.deferred is True and mismatched.reason == "ack_status_mismatch"
    snapshot.value["loaded"]["ackedIds"] = []
    wire_mismatch = admit_mail_cycle(snapshot, _binding(), _boundary(), _bounds())
    assert wire_mismatch.deferred is True and wire_mismatch.reason == "ack_wire_mismatch"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("session_mode", 1),
        ("repeat_phase", None),
        ("ack_loaded_uid", 1),
        ("status_last_uid", 1),
        ("last_actual_start_at", "not-a-time"),
        ("now", "not-a-time"),
    ],
)
def test_boundary_scalar_types_are_not_coerced(field, bad):
    _, snapshot = _client_snapshot()
    with pytest.raises(MailLoopPolicyError, match="boundary"):
        admit_mail_cycle(snapshot, _binding(), _boundary(**{field: bad}), _bounds())


def test_larger_successor_is_not_selected_or_budgeted():
    _, snapshot = _client_snapshot(successor=True)
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(snapshot), _bounds())
    assert decision.admitted is True
    assert decision.admitted_seq == 1
    assert decision.mail_units == 2


def test_due_boundary_uses_max_addon_and_actual_start_and_is_not_extendable():
    _, snapshot = _client_snapshot()
    bounds = _bounds(
        route_to_mailbox_seconds=1.0,
        world_click_seconds=1.0,
        mail_click_seconds=1.0,
        mail_postcondition_seconds=1.0,
        route_to_auctioneer_seconds=1.0,
        ah_reopen_seconds=1.0,
        safety_margin_seconds=1.0,
    )
    snapshot.value["scanner"]["dueAt"] = FRAME_DUE + 100
    at_deadline = admit_mail_cycle(snapshot, _binding(), _boundary(now=FRAME_DUE + 91.0), bounds)
    assert at_deadline.admitted is True
    assert at_deadline.effective_due_at == FRAME_DUE + 100
    assert at_deadline.latest_start_at == FRAME_DUE + 91
    assert at_deadline.completion_deadline_at == FRAME_DUE + 99
    assert at_deadline.budget is not None and at_deadline.budget.operational_seconds == 8.0
    late = admit_mail_cycle(snapshot, _binding(), _boundary(now=FRAME_DUE + 91.01), bounds)
    assert late.deferred is True
    assert late.reason == "insufficient_due_budget"
    assert late.effective_due_at == FRAME_DUE + 100
    assert late.latest_start_at == FRAME_DUE + 91

    snapshot.value["scanner"]["dueAt"] = FRAME_DUE - 100
    anchored = admit_mail_cycle(snapshot, _binding(), _boundary(now=FRAME_DUE - 18), bounds)
    assert anchored.admitted is True
    assert anchored.effective_due_at == FRAME_DUE
    assert anchored.latest_start_at == FRAME_DUE - 9


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
def test_operation_bounds_require_finite_positive_seconds(bad):
    _, snapshot = _client_snapshot()
    with pytest.raises(MailLoopPolicyError, match="bounds"):
        admit_mail_cycle(
            snapshot,
            _binding(),
            _boundary(),
            _bounds(route_to_mailbox_seconds=bad),
        )


def test_huge_duration_overflow_is_a_policy_error():
    _, snapshot = _client_snapshot()
    with pytest.raises(MailLoopPolicyError, match="bounds"):
        admit_mail_cycle(snapshot, _binding(), _boundary(), _bounds(route_to_mailbox_seconds=10**1000))


@pytest.mark.parametrize("field, bad", [("workTotal", True), ("workTotal", 1.5), ("workDone", 0.5)])
def test_fractional_or_boolean_work_progress_defers(field, bad):
    _, snapshot = _client_snapshot()
    snapshot.value["executor"][field] = bad
    decision = admit_mail_cycle(snapshot, _binding(), _boundary(), _bounds())
    assert decision.deferred is True
    assert decision.reason in {"executor_progress_invalid", "executor_work_total_invalid"}


def test_active_mail_and_successor_progress_are_not_silently_admitted():
    _, snapshot = _client_snapshot()
    value = dict(snapshot.value)
    value["executor"] = dict(value["executor"])
    value["executor"].update(phase="mail_wait", inFlight=False)
    # A validated Snapshot is required by the API; this policy mutation is
    # deliberately not a runtime composition claim and must not be admitted.
    decision = admit_mail_cycle(type(snapshot)(value), _binding(), _boundary(), _bounds())
    assert decision.deferred is True
    assert decision.reason == "executor_not_initial_mail"
