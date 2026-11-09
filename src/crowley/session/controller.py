"""Deterministic repeating-session state machine."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import protocol
from .calibration import CalibrationError, NativeCalibrationEvidence, NativeMemoryMeasurement
from .driver import DesktopDriver, DriverError
from .evidence import BASELINE_MEDIAN_SECONDS, SweepEvidence
from .journal import Journal, JournalError
from .mail_loop import MailBudget, MailLoopBinding, MailLoopBoundary, MailOperationBounds, admit_mail_cycle
from .startup import InvalidationCause, RuntimeInputEvidence, TakeoverDrillRecord


class _ReadOnlyDriver:
    """No desktop lease or input side effect for offline ``status``."""

    class _Guardian:
        generation = 0

        def revoke(self, reason: str) -> int:
            return 0

    guardian = _Guardian()

    def revoke(self, reason: str) -> None:
        return None


class ControllerState(StrEnum):
    STOPPED = "STOPPED"
    NATIVE_CALIBRATION = "NATIVE_CALIBRATION"
    INPUT_CALIBRATION = "INPUT_CALIBRATION"
    ARMED = "ARMED"
    RECONCILE = "RECONCILE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    READY = "READY"
    WAIT_DUE = "WAIT_DUE"
    SCANNING = "SCANNING"
    QUIET_SAVE = "QUIET_SAVE"
    SAVE_RELOAD_SENT = "SAVE_RELOAD_SENT"
    SAVE_RELOAD_WAIT = "SAVE_RELOAD_WAIT"
    UPLOAD_WAIT = "UPLOAD_WAIT"
    ACK_RELOAD_SENT = "ACK_RELOAD_SENT"
    ACK_RELOAD_WAIT = "ACK_RELOAD_WAIT"
    QUALIFY_PENDING = "QUALIFY_PENDING"
    REOPEN = "REOPEN"
    DISK_CLEANUP_SENT = "DISK_CLEANUP_SENT"
    DISK_CLEANUP_WAIT = "DISK_CLEANUP_WAIT"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"


class SessionMode(StrEnum):
    REPEAT = "repeat"
    VALIDATION = "validation"
    INTERACTION = "interaction"


class ActionKind(StrEnum):
    """Canonical external effect identities persisted in the journal."""

    SCAN = "scan"
    SAVE_RELOAD = "save_reload"
    UPLOAD_WAIT = "upload_wait"
    ACK_RELOAD = "ack_reload"
    LOGOUT = "logout"
    OPEN_AH = "open_ah"
    DISMISS_DISCONNECT = "dismiss_disconnect"
    RECONNECT_GAME = "reconnect_game"
    SELECT_ARTHUR = "select_arthur"
    ENTER_WORLD = "enter_world"
    PRE_LOGOUT_FENCE = "pre_logout_fence"
    STOP = "stop"


@dataclass(frozen=True)
class ActionIntent:
    """The one identity shared by preparation, dispatch and observation."""

    kind: ActionKind
    attempt: int
    deadline_at: int


_ACTION_ALIASES = {
    "/crowley scan": ActionKind.SCAN,
    "/reload": ActionKind.SAVE_RELOAD,
    "/logout": ActionKind.LOGOUT,
}

_SCANNER_PHASE_UNSET = object()
_EXECUTOR_TERMINAL_RESULTS = frozenset({"dry_run", "succeeded", "failed", "refused", "skipped"})

_PASSIVE_BOOT_ADOPTION_BLOCKED_STATES = frozenset(
    {
        ControllerState.SCANNING,
        ControllerState.NATIVE_CALIBRATION,
        ControllerState.SAVE_RELOAD_SENT,
        ControllerState.SAVE_RELOAD_WAIT,
        ControllerState.ACK_RELOAD_SENT,
        ControllerState.ACK_RELOAD_WAIT,
        ControllerState.QUALIFY_PENDING,
    }
)
_PASSIVE_BOOT_ADOPTION_BLOCKED_INTENTS = frozenset(
    {
        ActionKind.SCAN.value,
        ActionKind.SAVE_RELOAD.value,
        ActionKind.ACK_RELOAD.value,
        ActionKind.ENTER_WORLD.value,
        ActionKind.LOGOUT.value,
    }
)


def _expected_executor_result_count(pre_executor: dict[str, Any], pre_count: int) -> int:
    """Return the producer's causal resultCount for one admitted button.

    Crowley's Lua producer appends one result record for the overall action.
    A later mail_items/mail_wait continuation advances clickOrdinal but keeps
    that same resultCount; requiring +1 for every mailbox message invents a
    producer fact and rejects the real multi-message path.
    """

    if pre_executor.get("phase") in {"mail_items", "mail_wait"}:
        return pre_count
    return pre_count + 1


_EXECUTOR_INTENT_KEYS = frozenset(
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
    }
)
_EXECUTOR_OVERALL_KEYS = frozenset({"queueId", "seq", "cycleId", "generation", "leaseGeneration", "status"})
_EXECUTOR_STAGED_KEYS = frozenset({"archive", "checkpoint"})
_EXECUTOR_ANCHOR_KEYS = frozenset(
    {
        "action",
        "evidenceHash",
        "captureId",
        "captureSequence",
        "identity",
        "generation",
        "targetXid",
        "scene",
        "control",
        "bounds",
    }
)
_EXECUTOR_PROJECTION_KEYS = frozenset(
    {
        "availability",
        "queueId",
        "house",
        "character",
        "publishedArmed",
        "sessionArmed",
        "buttonVisible",
        "buttonName",
        "currentSeq",
        "currentKind",
        "phase",
        "inFlight",
        "clickReady",
        "workDone",
        "workTotal",
        "blocked",
        "latched",
        "ambiguous",
        "clickOrdinal",
        "lastClickSource",
        "resultCount",
        "lastResult",
    }
)

_MAIL_CYCLE_PHASES = frozenset(
    {
        "none",
        "preparing",
        "to_mailbox",
        "mailbox_open",
        "executing",
        "awaiting_confirmation",
        "to_auctioneer",
        "opening_ah",
        "complete",
        "blocked",
    }
)
_MAIL_CYCLE_TRANSITIONS = {
    "to_mailbox": frozenset({"preparing"}),
    "preparing": frozenset({"mailbox_open"}),
    "mailbox_open": frozenset({"awaiting_confirmation"}),
    # Return-route authority is minted only by accept_mail_terminal after a
    # durable causal executor outcome has been validated.  A generic phase
    # transition must never be sufficient to bypass that proof gate.
    "awaiting_confirmation": frozenset({"executing"}),
    # A callback can publish the next mail-item boundary synchronously with
    # the started result.  That settles the owned wait without a second
    # physical click; the next click remains separately confirmation-gated.
    "executing": frozenset({"executing", "awaiting_confirmation"}),
    "to_auctioneer": frozenset({"opening_ah"}),
    "opening_ah": frozenset(),
}
_MAIL_CYCLE_KEYS = frozenset(
    {
        "phase",
        "cycleId",
        "ackUid",
        "queueId",
        "generation",
        "leaseGeneration",
        "admittedSeq",
        "mailUnits",
        "budget",
        "completedUnits",
        "terminalOutcomeId",
        "lastExecutorSeq",
        "admittedEffectiveDueAt",
        "deadlineAt",
        "reason",
    }
)
_MAIL_BUDGET_KEYS = frozenset(
    {
        "mailUnits",
        "routeToMailboxSeconds",
        "worldClickSeconds",
        "mailClickSeconds",
        "mailPostconditionSeconds",
        "routeToAuctioneerSeconds",
        "ahReopenSeconds",
        "safetyMarginSeconds",
        "operationalSeconds",
        "totalSeconds",
    }
)
_MAIL_CYCLE_ARCHIVE_KEYS = frozenset(
    {
        "outcomeId",
        "cycleId",
        "ackUid",
        "queueId",
        "generation",
        "leaseGeneration",
        "admittedSeq",
        "mailUnits",
        "budget",
        "completedUnits",
        "terminalOutcomeId",
        "lastExecutorSeq",
        "effectiveDueAt",
        "deadlineAt",
        "result",
        "reason",
    }
)


def _mail_budget_record(value: MailBudget) -> dict[str, Any]:
    if type(value) is not MailBudget:
        raise JournalError("mail cycle budget is not an immutable MailBudget")
    return {
        "mailUnits": value.mail_units,
        "routeToMailboxSeconds": value.route_to_mailbox_seconds,
        "worldClickSeconds": value.world_click_seconds,
        "mailClickSeconds": value.mail_click_seconds,
        "mailPostconditionSeconds": value.mail_postcondition_seconds,
        "routeToAuctioneerSeconds": value.route_to_auctioneer_seconds,
        "ahReopenSeconds": value.ah_reopen_seconds,
        "safetyMarginSeconds": value.safety_margin_seconds,
        "operationalSeconds": value.operational_seconds,
        "totalSeconds": value.total_seconds,
    }


def _validate_mail_budget(value: Any, *, expected_units: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _MAIL_BUDGET_KEYS:
        raise JournalError("journal mail cycle budget schema is malformed")
    units = value["mailUnits"]
    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        raise JournalError("journal mail cycle budget units are malformed")
    if expected_units is not None and units != expected_units:
        raise JournalError("journal mail cycle budget units disagree with the cycle")
    numeric = tuple(key for key in _MAIL_BUDGET_KEYS if key != "mailUnits")
    for key in numeric:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise JournalError(f"journal mail cycle budget {key} is malformed")
        if key.endswith("Seconds") and float(item) <= 0:
            raise JournalError(f"journal mail cycle budget {key} must be positive")
    operational = (
        float(value["routeToMailboxSeconds"])
        + float(value["worldClickSeconds"])
        + units * (float(value["mailClickSeconds"]) + float(value["mailPostconditionSeconds"]))
        + float(value["routeToAuctioneerSeconds"])
        + float(value["ahReopenSeconds"])
    )
    total = operational + float(value["safetyMarginSeconds"])
    if not math.isclose(operational, float(value["operationalSeconds"]), rel_tol=0.0, abs_tol=1e-9):
        raise JournalError("journal mail cycle budget operational total disagrees")
    if not math.isclose(total, float(value["totalSeconds"]), rel_tol=0.0, abs_tol=1e-9):
        raise JournalError("journal mail cycle budget total disagrees")
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class MailCycleCapability:
    """Opaque same-process authority for one admitted mailbox cycle."""

    cycle_id: str
    queue_id: str
    admitted_seq: int
    mail_units: int
    budget: MailBudget
    generation: int
    lease_generation: int
    deadline_monotonic: float


def _empty_mail_cycle() -> dict[str, Any]:
    return {
        "phase": "none",
        "cycleId": "",
        "ackUid": "",
        "queueId": "",
        "generation": 0,
        "leaseGeneration": 0,
        "admittedSeq": 0,
        "mailUnits": 0,
        "budget": None,
        "completedUnits": 0,
        "terminalOutcomeId": "",
        "lastExecutorSeq": 0,
        "admittedEffectiveDueAt": 0.0,
        "deadlineAt": 0.0,
        "reason": "",
    }


def _validate_mail_cycle(value: Any, *, allow_none: bool = False) -> dict[str, Any] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or set(value) != _MAIL_CYCLE_KEYS:
        raise JournalError("journal mail cycle schema is malformed")
    if not isinstance(value["phase"], str) or value["phase"] not in _MAIL_CYCLE_PHASES:
        raise JournalError("journal mail cycle phase is malformed")
    for key in ("cycleId", "ackUid", "queueId", "reason"):
        if not isinstance(value[key], str):
            raise JournalError(f"journal mail cycle {key} is malformed")
    for key in (
        "generation",
        "leaseGeneration",
        "admittedSeq",
        "mailUnits",
        "completedUnits",
        "lastExecutorSeq",
    ):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
            raise JournalError(f"journal mail cycle {key} is malformed")
    if not isinstance(value["terminalOutcomeId"], str):
        raise JournalError("journal mail cycle terminal outcome identity is malformed")
    for key in ("admittedEffectiveDueAt", "deadlineAt"):
        if (
            isinstance(value[key], bool)
            or not isinstance(value[key], (int, float))
            or not math.isfinite(float(value[key]))
            or float(value[key]) < 0
        ):
            raise JournalError(f"journal mail cycle {key} is malformed")
    if value["phase"] == "none":
        if value["budget"] is not None:
            raise JournalError("empty journal mail cycle must not retain a budget")
    else:
        _validate_mail_budget(value["budget"], expected_units=value["mailUnits"])
        if value["completedUnits"] > value["mailUnits"]:
            raise JournalError("journal mail cycle completed units exceed its admitted bound")
    if value["phase"] == "none":
        if value != _empty_mail_cycle():
            raise JournalError("empty journal mail cycle must use canonical defaults")
    else:
        if (
            not value["cycleId"]
            or not value["ackUid"]
            or not value["queueId"]
            or value["generation"] <= 0
            or value["leaseGeneration"] <= 0
            or value["admittedSeq"] <= 0
            or value["mailUnits"] <= 0
            or value["lastExecutorSeq"] < 0
            or value["admittedEffectiveDueAt"] <= 0
            or value["deadlineAt"] <= 0
        ):
            raise JournalError("nonempty journal mail cycle identity is malformed")
    return copy.deepcopy(value)


def _validate_mail_cycle_stage(value: Any, *, allow_none: bool = True) -> dict[str, Any] | None:
    """Validate the bounded immutable mail outcome recovery marker."""

    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or set(value) != {"archive", "checkpoint"}:
        raise JournalError("journal mail cycle stage schema is malformed")
    archive = value["archive"]
    checkpoint = value["checkpoint"]
    if (
        not isinstance(archive, dict)
        or set(archive) != _MAIL_CYCLE_ARCHIVE_KEYS
        or not isinstance(archive.get("outcomeId"), str)
        or not archive.get("outcomeId")
        or not isinstance(archive.get("cycleId"), str)
        or not archive.get("cycleId")
        or not isinstance(archive.get("ackUid"), str)
        or not archive.get("ackUid")
        or not isinstance(archive.get("queueId"), str)
        or not archive.get("queueId")
        or isinstance(archive.get("generation"), bool)
        or not isinstance(archive.get("generation"), int)
        or archive.get("generation") <= 0
        or isinstance(archive.get("leaseGeneration"), bool)
        or not isinstance(archive.get("leaseGeneration"), int)
        or archive.get("leaseGeneration") <= 0
        or isinstance(archive.get("admittedSeq"), bool)
        or not isinstance(archive.get("admittedSeq"), int)
        or archive.get("admittedSeq") <= 0
        or isinstance(archive.get("mailUnits"), bool)
        or not isinstance(archive.get("mailUnits"), int)
        or archive.get("mailUnits") <= 0
        or isinstance(archive.get("completedUnits"), bool)
        or not isinstance(archive.get("completedUnits"), int)
        or archive.get("completedUnits") < 0
        or archive.get("result") not in {"complete", "blocked"}
        or not isinstance(archive.get("reason"), str)
        or not isinstance(checkpoint, dict)
        or checkpoint.get("schema") != 1
        or not isinstance(checkpoint.get("current"), dict)
        or not isinstance(checkpoint["current"].get("mailCycle"), dict)
    ):
        raise JournalError("journal mail cycle stage contents are malformed")
    cycle = _validate_mail_cycle(checkpoint["current"]["mailCycle"])
    assert cycle is not None
    _validate_mail_budget(archive["budget"], expected_units=archive["mailUnits"])
    if archive["completedUnits"] > archive["mailUnits"]:
        raise JournalError("journal mail cycle stage completed units exceed its bound")
    for archive_key, cycle_key in (
        ("cycleId", "cycleId"),
        ("ackUid", "ackUid"),
        ("queueId", "queueId"),
        ("generation", "generation"),
        ("leaseGeneration", "leaseGeneration"),
        ("admittedSeq", "admittedSeq"),
        ("mailUnits", "mailUnits"),
        ("budget", "budget"),
        ("completedUnits", "completedUnits"),
        ("terminalOutcomeId", "terminalOutcomeId"),
        ("lastExecutorSeq", "lastExecutorSeq"),
        ("effectiveDueAt", "admittedEffectiveDueAt"),
        ("deadlineAt", "deadlineAt"),
    ):
        if archive[archive_key] != cycle[cycle_key]:
            raise JournalError("journal mail cycle stage disagrees with its checkpoint")
    expected_phase = "complete" if archive["result"] == "complete" else "blocked"
    if cycle["phase"] != expected_phase:
        raise JournalError("journal mail cycle stage result disagrees with its phase")
    expected_outcome = hashlib.sha256(
        f"mail-cycle|{archive['cycleId']}|{archive['ackUid']}|"
        f"{archive['admittedSeq']}|{archive['result']}".encode()
    ).hexdigest()
    if archive["outcomeId"] != expected_outcome:
        raise JournalError("journal mail cycle stage outcome identity is unstable")
    return copy.deepcopy(value)


def _validate_executor_checkpoint(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"intent", "overall", "staged"}:
        raise JournalError("journal executor checkpoint schema is malformed")
    intent = value["intent"]
    overall = value["overall"]
    staged = value["staged"]
    if intent is None and overall is None and staged is None:
        raise JournalError("journal executor checkpoint cannot be empty")
    if intent is not None:
        if not isinstance(intent, dict) or set(intent) != _EXECUTOR_INTENT_KEYS:
            raise JournalError("journal executor intent schema is malformed")
        if (
            not isinstance(intent["attemptId"], str)
            or not intent["attemptId"]
            or not isinstance(intent["queueId"], str)
            or not intent["queueId"]
            or isinstance(intent["seq"], bool)
            or not isinstance(intent["seq"], int)
            or intent["seq"] <= 0
            or isinstance(intent["generation"], bool)
            or not isinstance(intent["generation"], int)
            or intent["generation"] <= 0
            or isinstance(intent["leaseGeneration"], bool)
            or not isinstance(intent["leaseGeneration"], int)
            or intent["leaseGeneration"] <= 0
            or not isinstance(intent["cycleId"], str)
            or not isinstance(intent["confirmation"], str)
            or intent["status"] != "pending"
        ):
            raise JournalError("journal executor intent identity is malformed")
        pre_crs = intent["preCrs"]
        if (
            not isinstance(pre_crs, dict)
            or set(pre_crs) != {"boot", "sequence", "crc32", "identity"}
            or not isinstance(pre_crs["boot"], str)
            or isinstance(pre_crs["sequence"], bool)
            or not isinstance(pre_crs["sequence"], int)
            or pre_crs["sequence"] <= 0
            or isinstance(pre_crs["crc32"], bool)
            or not isinstance(pre_crs["crc32"], int)
            or not isinstance(pre_crs["identity"], str)
        ):
            raise JournalError("journal executor preCrs is malformed")
        projection = intent["preExecutor"]
        if not isinstance(projection, dict) or set(projection) != _EXECUTOR_PROJECTION_KEYS:
            raise JournalError("journal executor preExecutor is malformed")
        last = projection["lastResult"]
        if (
            not isinstance(last, dict)
            or set(last) != {"seq", "kind", "status"}
            or isinstance(last["seq"], bool)
            or not isinstance(last["seq"], int)
            or not isinstance(last["kind"], str)
            or not isinstance(last["status"], str)
        ):
            raise JournalError("journal executor preExecutor lastResult is malformed")
        if isinstance(intent["preClickOrdinal"], bool) or not isinstance(intent["preClickOrdinal"], int):
            raise JournalError("journal executor preClickOrdinal is malformed")
        arms = intent["preArms"]
        if (
            not isinstance(arms, dict)
            or set(arms) != {"published", "session"}
            or any(not isinstance(item, bool) for item in arms.values())
        ):
            raise JournalError("journal executor preArms is malformed")
        evidence = intent["preEvidence"]
        if not _valid_executor_evidence(
            evidence,
            require_raw=True,
            expected_crs=pre_crs,
            expected_executor=projection,
        ):
            raise JournalError("journal executor preEvidence is malformed")
        if not isinstance(intent["anchor"], dict) or set(intent["anchor"]) != _EXECUTOR_ANCHOR_KEYS:
            raise JournalError("journal executor anchor is malformed")
    if overall is not None:
        if not isinstance(overall, dict) or set(overall) != _EXECUTOR_OVERALL_KEYS:
            raise JournalError("journal executor overall schema is malformed")
        if (
            not isinstance(overall["queueId"], str)
            or not overall["queueId"]
            or isinstance(overall["seq"], bool)
            or not isinstance(overall["seq"], int)
            or overall["seq"] <= 0
            or not isinstance(overall["cycleId"], str)
            or isinstance(overall["generation"], bool)
            or not isinstance(overall["generation"], int)
            or overall["generation"] <= 0
            or isinstance(overall["leaseGeneration"], bool)
            or not isinstance(overall["leaseGeneration"], int)
            or overall["leaseGeneration"] <= 0
            or overall["status"] != "in_flight"
        ):
            raise JournalError("journal executor overall identity is malformed")
    if staged is not None:
        if not isinstance(staged, dict) or set(staged) != _EXECUTOR_STAGED_KEYS:
            raise JournalError("journal executor staged outcome schema is malformed")
        archive = staged["archive"]
        checkpoint = staged["checkpoint"]
        terminal_observation = isinstance(archive, dict) and archive.get("kind") == "terminal_observation"
        if (
            not isinstance(archive, dict)
            or not isinstance(archive.get("outcomeId"), str)
            or not archive.get("outcomeId")
            or not isinstance(archive.get("result"), str)
            or archive.get("result")
            not in {"dry_run", "started", "blocked", "succeeded", "failed", "refused", "skipped"}
            or (
                not terminal_observation
                and (not isinstance(archive.get("attemptId"), str) or not archive.get("attemptId"))
            )
            or (
                terminal_observation
                and (
                    not isinstance(archive.get("physicalAttemptId"), str)
                    or not archive.get("physicalAttemptId")
                    or not isinstance(archive.get("physicalOutcomeId"), str)
                    or not archive.get("physicalOutcomeId")
                )
            )
        ):
            raise JournalError("journal executor staged archive is malformed")
        if not isinstance(checkpoint, dict) or checkpoint.get("schema") != 1:
            raise JournalError("journal executor staged checkpoint is malformed")
        current = checkpoint.get("current")
        if not isinstance(current, dict) or "executor" not in current:
            raise JournalError("journal executor staged checkpoint current state is malformed")
        nested = current.get("executor")
        if nested is not None:
            _validate_executor_checkpoint(nested)
            if nested.get("staged") is not None:
                raise JournalError("journal executor staged checkpoint is recursively staged")


def _valid_executor_evidence(
    value: Any,
    *,
    require_raw: bool,
    expected_crs: dict[str, Any] | None = None,
    expected_executor: dict[str, Any] | None = None,
    contract: protocol.ProtocolContract | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    base = {"sha256", "length", "crc32"}
    allowed = base | {"rawFrameHex"}
    if require_raw and set(value) != allowed:
        return False
    if not require_raw and set(value) not in (base, allowed):
        return False
    raw_hex = value.get("rawFrameHex")
    decoded = None
    if "rawFrameHex" in value:
        if not isinstance(raw_hex, str) or len(raw_hex) != 4096:
            return False
        try:
            raw = bytes.fromhex(raw_hex)
            decoded = protocol.decode_frame(raw, contract=contract)
        except (ValueError, protocol.ProtocolError):
            return False
        if (
            hashlib.sha256(raw).hexdigest() != value.get("sha256")
            or len(raw) != protocol.CAPACITY
            or value.get("length") != protocol.CAPACITY
            or value.get("crc32") != decoded.crc32
        ):
            return False
        if expected_crs is not None and (
            decoded.boot != expected_crs.get("boot")
            or decoded.sequence != expected_crs.get("sequence")
            or decoded.crc32 != expected_crs.get("crc32")
        ):
            return False
        if expected_executor is not None and decoded.snapshot.value.get("executor") != expected_executor:
            return False
    valid = (
        isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
        and all(char in "0123456789abcdef" for char in value["sha256"].lower())
        and isinstance(value.get("length"), int)
        and not isinstance(value["length"], bool)
        and value["length"] == 2048
        and isinstance(value.get("crc32"), int)
        and not isinstance(value["crc32"], bool)
    )
    if not valid:
        return False
    return decoded is not None if expected_crs is not None or expected_executor is not None else True


def _valid_executor_causal_transition(
    pre_crs: Any,
    pre_executor: Any,
    post_crs: Any,
    post_executor: Any,
    *,
    expected_queue: str | None = None,
) -> bool:
    """Validate the immutable causal identity of one executor transition."""

    crs_values = (pre_crs, post_crs)
    for value in crs_values:
        if not isinstance(value, dict) or set(value) != {"boot", "sequence", "crc32", "identity"}:
            return False
        if (
            not isinstance(value["boot"], str)
            or not value["boot"]
            or isinstance(value["sequence"], bool)
            or not isinstance(value["sequence"], int)
            or value["sequence"] <= 0
            or isinstance(value["crc32"], bool)
            or not isinstance(value["crc32"], int)
            or value["crc32"] < 0
            or not isinstance(value["identity"], str)
            or not value["identity"]
        ):
            return False
    if post_crs["boot"] != pre_crs["boot"] or post_crs["identity"] != pre_crs["identity"]:
        return False
    try:
        if protocol.unsigned_delta(post_crs["sequence"], pre_crs["sequence"]) <= 0:
            return False
    except (TypeError, ValueError, protocol.ProtocolError):
        return False
    for projection in (pre_executor, post_executor):
        if not isinstance(projection, dict) or set(projection) != _EXECUTOR_PROJECTION_KEYS:
            return False
        for key in ("queueId", "house", "character"):
            if not isinstance(projection[key], str) or not projection[key]:
                return False
    if (
        pre_executor["queueId"] != post_executor["queueId"]
        or pre_executor["house"] != post_executor["house"]
        or pre_executor["character"] != post_executor["character"]
        or (expected_queue is not None and pre_executor["queueId"] != expected_queue)
    ):
        return False
    return True


def _valid_executor_receipt(value: Any, *, intent: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "action",
        "generation",
        "identity",
        "admittedAnchorEvidenceHash",
        "admittedAnchorCaptureId",
        "admittedAnchorCaptureSequence",
        "anchorEvidenceHash",
        "anchorCaptureId",
        "anchorCaptureSequence",
        "endpoint",
        "rateGate",
        "clickPrecommitMonotonicNs",
        "clickDispatchStartedMonotonicNs",
        "pressReleaseMonotonicNs",
        "clickAckMonotonicNs",
        "rateRecordMonotonicNs",
        "guardianCompleted",
        "receiptFinalizedMonotonicNs",
    }
    if not required <= set(value) or value["action"] != "executor_click":
        return False
    endpoint = value["endpoint"]
    times = [
        value[key]
        for key in (
            "clickPrecommitMonotonicNs",
            "clickDispatchStartedMonotonicNs",
            "pressReleaseMonotonicNs",
            "clickAckMonotonicNs",
            "rateRecordMonotonicNs",
            "receiptFinalizedMonotonicNs",
        )
    ]
    return (
        value["generation"] == intent["leaseGeneration"]
        and value["identity"] == intent["preCrs"]["identity"]
        and isinstance(value.get("admittedAnchorEvidenceHash"), str)
        and len(value["admittedAnchorEvidenceHash"]) == 64
        and all(char in "0123456789abcdef" for char in value["admittedAnchorEvidenceHash"].lower())
        and isinstance(value.get("admittedAnchorCaptureId"), str)
        and bool(value["admittedAnchorCaptureId"])
        and isinstance(value.get("admittedAnchorCaptureSequence"), int)
        and not isinstance(value["admittedAnchorCaptureSequence"], bool)
        and value["admittedAnchorEvidenceHash"] == intent["anchor"]["evidenceHash"]
        and value["admittedAnchorCaptureId"] == intent["anchor"]["captureId"]
        and value["admittedAnchorCaptureSequence"] == intent["anchor"]["captureSequence"]
        and isinstance(value.get("anchorEvidenceHash"), str)
        and len(value["anchorEvidenceHash"]) == 64
        and all(char in "0123456789abcdef" for char in value["anchorEvidenceHash"].lower())
        and isinstance(value.get("anchorCaptureId"), str)
        and bool(value["anchorCaptureId"])
        and value["anchorEvidenceHash"] != intent["anchor"]["evidenceHash"]
        and value["anchorCaptureId"] != intent["anchor"]["captureId"]
        and isinstance(value["anchorCaptureSequence"], int)
        and not isinstance(value["anchorCaptureSequence"], bool)
        and value["anchorCaptureSequence"] > intent["anchor"]["captureSequence"]
        and value["rateGate"] == "canonical-user-session"
        and value["guardianCompleted"] is True
        and isinstance(endpoint, (list, tuple))
        and len(endpoint) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in endpoint)
        and all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in times)
        and times == sorted(times)
    )


_NATIVE_CONSTRUCTION_CHECKPOINT_KEYS = frozenset(
    {"schema", "B0", "expectedConstructorBoot", "ownerGeneration", "kind", "phase"}
)
_NATIVE_CONSTRUCTION_KINDS = frozenset({"measure", "preflight"})
_NATIVE_CONSTRUCTION_PHASES = frozenset({"prepared", "recovery_required"})


def _valid_native_construction_checkpoint(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _NATIVE_CONSTRUCTION_CHECKPOINT_KEYS:
        return False
    schema = value.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != 1:
        return False
    b0 = value.get("B0")
    expected = value.get("expectedConstructorBoot")
    try:
        b0_size = len(b0.encode("utf-8")) if isinstance(b0, str) else -1
        expected_size = len(expected.encode("utf-8")) if isinstance(expected, str) else -1
    except UnicodeError:
        return False
    if (
        not isinstance(b0, str)
        or not b0
        or b0.startswith("native-construction:")
        or b0_size > 64
        or not isinstance(expected, str)
        or expected != f"native-construction:{b0}"
        or expected_size > 96
    ):
        return False
    generation = value.get("ownerGeneration")
    kind = value.get("kind")
    phase = value.get("phase")
    return (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
        and isinstance(kind, str)
        and kind in _NATIVE_CONSTRUCTION_KINDS
        and isinstance(phase, str)
        and phase in _NATIVE_CONSTRUCTION_PHASES
    )


def _action_kind(action: str, *, acknowledgement: bool = False) -> ActionKind:
    if acknowledgement and action in {"/reload", "save_reload"}:
        return ActionKind.ACK_RELOAD
    try:
        return ActionKind(action)
    except ValueError:
        try:
            return _ACTION_ALIASES[action]
        except KeyError as exc:
            raise ValueError(f"unknown action intent {action!r}") from exc


@dataclass(frozen=True)
class ControllerStatus:
    state: ControllerState
    mode: SessionMode
    generation: int
    lease_generation: int
    stop_requested: bool
    closeout_requested: bool
    streak: int
    blocker: str
    last_uid: str
    started_at: int = 0
    expected_boot: str = ""
    action_intent: str = ""
    deadline_at: int = 0


@dataclass(frozen=True)
class ObservationFence:
    """Admission token captured before daemon I/O.

    A daemon worker must carry this token through file reads, decoding, and
    model work.  The controller admits the result only when both the lease
    generation and the expected state still match atomically under its lock.
    """

    generation: int
    state: ControllerState
    action_attempt: int = 0


class Controller:
    """The controller owns timing and durable intent, never scan serialization."""

    def __init__(
        self,
        journal: Journal | str | Path,
        *,
        driver: DesktopDriver | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], int] = lambda: int(time.time()),
        cadence_seconds: int = 900,
        sweep_cap_seconds: int = 600,
        plan_count: int = 428,
        protocol_contract: protocol.ProtocolContract | None = None,
        reload_deadline_seconds: int = 45,
        upload_deadline_seconds: int = 60,
        ack_deadline_seconds: int = 60,
        recover_on_restart: bool = True,
    ):
        if (
            isinstance(cadence_seconds, bool)
            or not isinstance(cadence_seconds, int)
            or cadence_seconds != 900
        ):
            raise ValueError("cadence_seconds must be 900")
        if isinstance(plan_count, bool) or not isinstance(plan_count, int) or plan_count <= 0:
            raise ValueError("plan_count must be a positive integer")
        self.journal = journal if isinstance(journal, Journal) else Journal(journal)
        self.driver = driver or (DesktopDriver() if recover_on_restart else _ReadOnlyDriver())
        self.clock = clock
        self.wall_clock = wall_clock
        self.cadence_seconds = cadence_seconds
        self.sweep_cap_seconds = sweep_cap_seconds
        self.reload_deadline_seconds = reload_deadline_seconds
        self.upload_deadline_seconds = upload_deadline_seconds
        self.ack_deadline_seconds = ack_deadline_seconds
        self.plan_count = plan_count
        self.protocol_contract = protocol_contract
        # A fresh owner must finish any manifest that survived a publication
        # or checkpoint failure before interpreting the live checkpoint.
        self.journal.replay_archive_bundles()
        journal_was_present = self.journal.path.exists()
        value = self.journal.read()
        native_key_present = "nativeConstruction" in value
        stored_native_construction = value.get("nativeConstruction")
        if not native_key_present:
            self._native_construction: dict[str, Any] | None = None
            self._native_construction_valid = True
            self._native_construction_raw: Any = None
        elif stored_native_construction is None:
            # A present JSON null is evidence of a malformed checkpoint, not
            # the same thing as an omitted checkpoint.  Preserve it verbatim
            # so a restart cannot turn an invalid durable state into absence.
            self._native_construction = None
            self._native_construction_valid = False
            self._native_construction_raw = None
        elif _valid_native_construction_checkpoint(stored_native_construction):
            self._native_construction = copy.deepcopy(stored_native_construction)
            self._native_construction_valid = True
            self._native_construction_raw = None
        else:
            # Preserve malformed durable state verbatim.  A fresh owner must
            # fail closed and recovery may inspect it, but ordinary checkpoint
            # writes must never silently repair or erase the evidence.
            self._native_construction = None
            self._native_construction_valid = False
            self._native_construction_raw = copy.deepcopy(stored_native_construction)
        self._state = ControllerState(value.get("state", "STOPPED"))
        self._mode = SessionMode(value.get("mode", "repeat"))
        self._generation = int(value.get("generation", 0))
        self._lease_generation = int(value.get("leaseGeneration", 0))
        self._stop_requested = bool(value.get("stopRequested", False))
        self._closeout_requested = bool(value.get("closeoutRequested", False))
        self._blocker = str(value.get("blocker", ""))
        self._native_calibrated = bool(value.get("nativeCalibration", False))
        self._input_calibrated = bool(value.get("inputCalibration", False))
        stored_takeover_drill = value.get("takeoverDrill")
        self._takeover_drill_record = TakeoverDrillRecord.parse(stored_takeover_drill)
        self._takeover_drill_valid = self._takeover_drill_record is not None
        self._takeover_drill_raw: Any = (
            None if self._takeover_drill_valid else copy.deepcopy(stored_takeover_drill)
        )
        stored_calibration = value.get("calibrationEvidence", {})
        self._calibration_evidence: dict[str, Any] = (
            stored_calibration if isinstance(stored_calibration, dict) else {}
        )
        stored_calibration_history = value.get("calibrationHistory", [])
        self._calibration_history: list[dict[str, Any]] = (
            [item for item in stored_calibration_history if isinstance(item, dict)][-20:]
            if isinstance(stored_calibration_history, list)
            else []
        )
        self._calibration_invalidated_reason = str(value.get("calibrationInvalidated", ""))
        stored_invalidation = value.get("runtimeReadinessInvalidation")
        try:
            self._runtime_readiness_invalidation = (
                InvalidationCause(stored_invalidation) if stored_invalidation else None
            )
        except ValueError:
            self._runtime_readiness_invalidation = InvalidationCause.UNKNOWN
        stored_history = value.get("history", [])
        self._history: list[dict[str, Any]] = (
            [item for item in stored_history if isinstance(item, dict)][-200:]
            if isinstance(stored_history, list)
            else []
        )
        self._restart_uncertain = bool(value.get("restartUncertain", False))
        self._reconciliation_required = bool(value.get("reconciliationRequired", False))
        # A live calibration record is intentionally process-local.  It binds
        # the armed lease to the XInput hierarchy and target identity; after a
        # restart the record cannot be reconstructed from a journal boolean.
        self._input_evidence: Any | None = None
        self._runtime_startup_evidence: RuntimeInputEvidence | None = None
        qualification = value.get("qualification", {})
        self._streak = int(qualification.get("streak", 0))
        self._accepted = list(qualification.get("accepted", []))
        stored_total_accepted = qualification.get("totalAccepted", len(self._accepted))
        self._total_accepted = (
            int(stored_total_accepted)
            if isinstance(stored_total_accepted, int)
            and not isinstance(stored_total_accepted, bool)
            and stored_total_accepted >= len(self._accepted)
            else len(self._accepted)
        )
        seed = qualification.get("cadenceSeedStart")
        self._cadence_seed_start = int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else None
        current = value.get("current", {})
        current_uid = str(current.get("uid", ""))
        self._pending = bool(current.get("pending", False))
        self._last_uid = current_uid or (self._accepted[-1].get("uid", "") if self._accepted else "")
        self._started_at = int(current.get("startedAt", 0))
        self._expected_boot = str(current.get("expectedBoot", ""))
        raw_action = str(current.get("action", ""))
        # The daemon and controller now share canonical IDs.  A literal slash
        # command can only be observed in an interrupted pre-round journal;
        # normalize it at the boundary before any recovery decision.
        try:
            self._action_intent = _action_kind(raw_action).value if raw_action else ""
        except ValueError:
            self._action_intent = raw_action
        self._action_attempt = int(current.get("actionAttempt", 0))
        self._action_result = str(current.get("actionResult", ""))
        self._action_started_at = int(current.get("actionStartedAt", 0))
        self._frame_boot = str(current.get("frameBoot", ""))
        self._frame_sequence = int(current.get("frameSequence", 0))
        self._frame_crc32 = int(current.get("frameCrc32", 0))
        self._restart_reference_boot = str(current.get("restartReferenceBoot", ""))
        self._restart_observed_boot = str(current.get("restartObservedBoot", ""))
        self._reload_boot = str(current.get("reloadBoot", ""))
        self._deadline_at = int(current.get("deadlineAt", 0))
        self._evidence_meta: dict[str, Any] = current.get("evidence", {})
        stored_mail_cycle = current.get("mailCycle", _empty_mail_cycle())
        self._mail_cycle = _validate_mail_cycle(stored_mail_cycle)
        if self._mail_cycle is None:
            self._mail_cycle = _empty_mail_cycle()
        stored_mail_cycle_stage = current.get("mailCycleStage")
        self._mail_cycle_stage = _validate_mail_cycle_stage(stored_mail_cycle_stage)
        self._mail_cycle_staged_archive: dict[str, Any] | None = (
            copy.deepcopy(self._mail_cycle_stage["archive"]) if self._mail_cycle_stage is not None else None
        )
        self._mail_cycle_staged_checkpoint: dict[str, Any] | None = (
            copy.deepcopy(self._mail_cycle_stage["checkpoint"])
            if self._mail_cycle_stage is not None
            else None
        )
        self._mail_cycle_capability: MailCycleCapability | None = None
        # Terminal authority is intentionally process-local.  Durable archive
        # bytes remain audit/recovery evidence and cannot mint a new return
        # permission after restart.
        self._mail_cycle_terminal_authority: str | None = None
        self._mail_cycle_restart_blocked = bool(
            self._mail_cycle_staged_archive
            or self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
        )
        stored_executor = current.get("executor")
        if stored_executor is not None:
            _validate_executor_checkpoint(stored_executor)
        self._executor_intent: dict[str, Any] | None = (
            copy.deepcopy(stored_executor.get("intent"))
            if isinstance(stored_executor, dict) and isinstance(stored_executor.get("intent"), dict)
            else None
        )
        self._executor_overall: dict[str, Any] | None = (
            copy.deepcopy(stored_executor.get("overall"))
            if isinstance(stored_executor, dict) and isinstance(stored_executor.get("overall"), dict)
            else None
        )
        self._executor_owner_token = uuid.uuid4().hex
        self._executor_capability: str | None = None
        stored_staged = stored_executor.get("staged") if isinstance(stored_executor, dict) else None
        self._executor_staged_archive: dict[str, Any] | None = (
            copy.deepcopy(stored_staged["archive"]) if isinstance(stored_staged, dict) else None
        )
        self._executor_staged_checkpoint: dict[str, Any] | None = (
            copy.deepcopy(stored_staged["checkpoint"]) if isinstance(stored_staged, dict) else None
        )
        self._executor_restart_blocked = bool(
            self._executor_intent or self._executor_overall or self._executor_staged_archive
        )
        stored_qualification_pending = value.get("qualificationPending")
        self._qualification_pending: dict[str, Any] | None = (
            copy.deepcopy(stored_qualification_pending)
            if isinstance(stored_qualification_pending, dict)
            else None
        )
        recovery_attempts = value.get("recoveryAttempts", 0)
        recovery_actions = value.get("recoveryActions", [])
        self._recovery_attempts = int(recovery_attempts) if isinstance(recovery_attempts, int) else 0
        self._recovery_actions = (
            {item for item in recovery_actions if isinstance(item, str)}
            if isinstance(recovery_actions, list)
            else set()
        )
        self._deadline = (
            self.clock() + max(0, self._deadline_at - self.wall_clock()) if self._deadline_at else 0.0
        )
        self._fresh_owner = False
        # A process restart may convert an active repeat checkpoint into a
        # durable pause.  The default owner may then recollect passive
        # readiness and use the ordinary resume path, but only for that
        # restart-created pause.  A checkpoint that was already PAUSED or
        # STOPPED remains an explicit human hold.
        self._restart_startup_resume_pending = False
        self._mutex = threading.RLock()
        self._guardian_callback_active = False
        self._planned_revoke = False
        guardian_callback = getattr(getattr(self.driver, "guardian", None), "on_revoke", None)
        if callable(guardian_callback):
            guardian_callback(lambda reason, _generation: self._on_guardian_revocation(reason))
        self._resume_state = ControllerState(value.get("resumeState", self._state.value))
        calibration_was_invalidated = False
        if recover_on_restart and (
            self._native_calibrated
            or self._input_calibrated
            or self._calibration_evidence
            or self._calibration_history
        ):
            # Calibration booleans and serialized proof are audit evidence only.
            # A new controller process cannot reconstruct the private seal or
            # bind it to the current game PID/boot/display.  Same-process
            # pause/resume retains ``_input_evidence`` below; construction of a
            # new Controller always requires both live gates again.
            self._native_calibrated = False
            self._input_calibrated = False
            self._calibration_invalidated_reason = "process restart invalidated live calibration evidence"
            calibration_was_invalidated = True
        restart_epoch_advanced = False
        if recover_on_restart and journal_was_present:
            # A fresh controller process owns a new IPC epoch.  Persisted
            # generations are audit data only: stale daemon work from the
            # previous process must fail admission even when the durable
            # state is already PAUSED, BLOCKED, or STOPPED.
            self._generation += 1
            restart_epoch_advanced = True
            self._fresh_owner = True
        restartable_states = {
            ControllerState.STOPPED,
            ControllerState.PAUSED,
            ControllerState.BLOCKED,
            ControllerState.QUALIFY_PENDING,
        }
        if recover_on_restart and self._state not in restartable_states:
            self._resume_state = self._state
            self._restart_uncertain = bool(
                self._action_intent and self._action_result not in {"", "complete", "abandoned_after_restart"}
            )
            self._input_evidence = None
            self._state = ControllerState.PAUSED
            self._restart_startup_resume_pending = bool(
                self._mode == SessionMode.REPEAT and not self._stop_requested
            )
            self._blocker = (
                "process restart with uncertain action; fresh observation required"
                if self._restart_uncertain
                else "process restart; explicit resume required"
            )
            self._save(last_action="restart_pause")
        elif calibration_was_invalidated:
            # Preserve the durable PAUSED/BLOCKED/STOPPED checkpoint while
            # recording that its old calibration booleans are no longer live.
            self._save(last_action="restart_invalidate_calibration")
        elif restart_epoch_advanced:
            self._save(last_action="restart_epoch")
        if recover_on_restart and self._executor_restart_blocked and self._executor_staged_archive is None:
            # A fresh owner may never reinterpret a persisted physical click
            # or an in-flight mail action as permission to dispatch again.
            self._state = ControllerState.BLOCKED
            self._resume_state = ControllerState.BLOCKED
            self._blocker = "executor click/action survived restart; explicit reconciliation required"
            self._save(last_action="executor_restart_blocked")
        if recover_on_restart and self._mail_cycle["phase"] not in {"none", "complete", "blocked"}:
            self._block_mail_cycle_locked("mail cycle survived restart; explicit reconciliation required")
            self._state = ControllerState.BLOCKED
            self._resume_state = ControllerState.BLOCKED
            self._restart_uncertain = True
            self._reconciliation_required = True
            self._blocker = "mail cycle survived restart; explicit reconciliation required"
            self._save(last_action="mail_cycle_restart_blocked")
            try:
                self._stage_mail_cycle_outcome_locked(
                    result="blocked",
                    reason="mail cycle survived restart; explicit reconciliation required",
                )
                self._publish_staged_mail_cycle_outcome_locked()
            except Exception:
                # The current blocked marker remains durable even when the
                # archive boundary needs a later fresh-owner repair.
                self._mail_cycle_restart_blocked = True
        if self._executor_staged_archive is not None:
            self._recover_staged_executor_outcome(recover_on_restart=recover_on_restart)
        if self._mail_cycle_staged_archive is not None:
            self._recover_staged_mail_cycle_outcome(recover_on_restart=recover_on_restart)
        # Do not infer a missing reference from the current frame during
        # restart.  The dispatch/expect_boot boundary is the only honest
        # source of B0; if it was absent, a save/ack transition cannot later
        # claim a changed boot.

    def claim_restart_startup_resume(self) -> int | None:
        """Consume the one automatic resume allowed for a restart-created hold."""

        with self._mutex:
            if not self._restart_startup_resume_pending:
                return None
            self._restart_startup_resume_pending = False
            if (
                self._state != ControllerState.PAUSED
                or self._mode != SessionMode.REPEAT
                or self._stop_requested
            ):
                return None
            return self._generation

    def observation_fence(self) -> ObservationFence:
        """Capture the generation/state admission boundary for external I/O."""

        with self._mutex:
            return ObservationFence(self._generation, self._state, self._action_attempt)

    @staticmethod
    def _state_matches(current: ControllerState, expected: object | None) -> bool:
        if expected is None:
            return True
        if isinstance(expected, ControllerState):
            return current == expected
        if isinstance(expected, str):
            return current.value == expected
        if isinstance(expected, (set, frozenset, tuple, list)):
            return any(Controller._state_matches(current, item) for item in expected)
        return False

    def _admitted(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> bool:
        """Check a post-I/O result while ``self._mutex`` is held."""

        if fence is not None:
            if expected_generation is not None and expected_generation != fence.generation:
                return False
            if expected_state is not None and not self._state_matches(fence.state, expected_state):
                return False
            expected_generation = fence.generation
            expected_state = fence.state
            if fence.action_attempt != self._action_attempt:
                return False
        if expected_generation is not None and self._generation != expected_generation:
            return False
        return self._state_matches(self._state, expected_state)

    def admit_observation(
        self,
        fence: ObservationFence,
        *,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
    ) -> bool:
        """Public typed admission check for daemon workers.

        This method is deliberately small: callers should perform their file
        or screenshot I/O first, then call the transition method with the same
        fence.  A false result means the observation is stale and must be
        discarded without changing durable state.
        """

        with self._mutex:
            return self._admitted(
                fence=fence,
                expected_state=expected_state,
            )

    def _archive(self, kind: str, record: dict[str, Any]) -> None:
        # Keep archives separate from the bounded live checkpoint.  Archive
        # failures are real durability failures and must surface to the caller.
        self.journal.append_archive(kind, record)

    def _begin_intent(self, action: str, *, deadline_seconds: int | None = None) -> None:
        """Start a unique action attempt and clear the prior result/deadline."""

        if not action:
            raise ValueError("action intent must be nonempty")
        action = _action_kind(action).value
        self._action_attempt += 1
        self._action_intent = action
        self._action_result = "pending"
        self._restart_reference_boot = ""
        self._restart_observed_boot = ""
        self._action_started_at = self.wall_clock()
        self._deadline = 0.0
        self._deadline_at = 0
        if deadline_seconds is not None:
            self._deadline = self.clock() + max(0, deadline_seconds)
            self._deadline_at = self.wall_clock() + max(0, deadline_seconds)

    def _clear_intent(self, result: str = "complete") -> None:
        self._action_result = result
        self._action_intent = ""
        self._action_started_at = 0
        self._deadline = 0.0
        self._deadline_at = 0
        self._restart_reference_boot = ""
        self._restart_observed_boot = ""

    @classmethod
    def _bounded_evidence(cls, evidence: dict[str, Any]) -> dict[str, Any]:
        """Keep live checkpoints small while archives retain full item proof."""

        bounded: dict[str, Any] = {}
        for key, value in evidence.items():
            if key in {"itemEvidence", "ownedRows", "fullEvidence", "priorEvidence"}:
                continue
            bounded[key] = copy.deepcopy(value)
        for key in ("itemEvidence", "ownedRows"):
            value = evidence.get(key)
            if value is not None:
                bounded[f"{key}Count"] = len(value) if isinstance(value, (dict, list)) else 0
        full = evidence.get("fullEvidence")
        if isinstance(full, dict):
            bounded["fullEvidence"] = {
                key: full.get(key) for key in ("uid", "planHash", "startedAt", "finishedAt") if key in full
            }
        prior = evidence.get("priorEvidence")
        if isinstance(prior, dict):
            bounded["priorEvidence"] = cls._bounded_evidence(prior)
        return bounded

    def _dispatch_intent(self, action: str, *, deadline_seconds: int) -> None:
        """Record an external dispatch without silently creating a second attempt."""

        action = _action_kind(action).value
        if self._action_intent != action:
            self._begin_intent(action, deadline_seconds=deadline_seconds)
        elif not self._deadline_at:
            self._deadline = self.clock() + max(0, deadline_seconds)
            self._deadline_at = self.wall_clock() + max(0, deadline_seconds)
        if action in {"save_reload", "ack_reload", "enter_world"} and self._frame_boot:
            self._restart_reference_boot = self._frame_boot
        self._action_result = "dispatched"

    def _outcome_checkpoint(
        self,
        *,
        include_executor_stage: bool = True,
        include_mail_cycle_stage: bool = True,
    ) -> dict[str, Any]:
        """Return the bounded controller state committed with an outcome."""

        return {
            "schema": 1,
            "state": self._state.value,
            "mode": self._mode.value,
            "generation": self._generation,
            "leaseGeneration": self._lease_generation,
            "stopRequested": self._stop_requested,
            "closeoutRequested": self._closeout_requested,
            "restartRequiresResume": self._state
            in {ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED},
            "blocker": self._blocker,
            "restartUncertain": self._restart_uncertain,
            "reconciliationRequired": self._reconciliation_required,
            # Qualification is deliberately staged as a full record before
            # its archive transaction.  This is the bounded controller's one
            # durable exception: a fresh owner can reconstruct the public
            # qualification call after the first pending-marker failure.
            "qualificationPending": copy.deepcopy(self._qualification_pending),
            "qualification": {
                "streak": self._streak,
                "totalAccepted": self._total_accepted,
                "cadenceSeedStart": self._cadence_seed_start,
                "accepted": [
                    {
                        **{key: copy.deepcopy(value) for key, value in accepted.items() if key != "evidence"},
                        **(
                            {"evidence": self._bounded_evidence(accepted["evidence"])}
                            if isinstance(accepted.get("evidence"), dict)
                            else {}
                        ),
                    }
                    for accepted in self._accepted
                ],
            },
            "resumeState": self._resume_state.value,
            "current": {
                "uid": self._last_uid if self._pending else "",
                "pending": self._pending,
                "startedAt": self._started_at,
                "expectedBoot": self._expected_boot,
                "action": self._action_intent,
                "actionAttempt": self._action_attempt,
                "actionResult": self._action_result,
                "actionStartedAt": self._action_started_at,
                "frameBoot": self._frame_boot,
                "frameSequence": self._frame_sequence,
                "frameCrc32": self._frame_crc32,
                "restartReferenceBoot": self._restart_reference_boot,
                "restartObservedBoot": self._restart_observed_boot,
                "reloadBoot": self._reload_boot,
                "deadlineAt": self._deadline_at,
                "evidence": self._bounded_evidence(self._evidence_meta),
                "executor": self._executor_checkpoint(include_staged=include_executor_stage),
                "mailCycle": copy.deepcopy(self._mail_cycle),
                "mailCycleStage": (
                    {
                        "archive": copy.deepcopy(self._mail_cycle_staged_archive),
                        "checkpoint": copy.deepcopy(self._mail_cycle_staged_checkpoint),
                    }
                    if include_mail_cycle_stage and self._mail_cycle_staged_archive is not None
                    else None
                ),
            },
        }

    def _executor_checkpoint(self, *, include_staged: bool = True) -> dict[str, Any] | None:
        """Return the bounded durable click/overall-action checkpoint."""

        if (
            self._executor_intent is None
            and self._executor_overall is None
            and self._executor_staged_archive is None
        ):
            return None
        checkpoint = {
            "intent": copy.deepcopy(self._executor_intent),
            "overall": copy.deepcopy(self._executor_overall),
            "staged": None,
        }
        if include_staged and self._executor_staged_archive is not None:
            if not isinstance(self._executor_staged_checkpoint, dict):
                raise JournalError("journal executor staged checkpoint is unavailable")
            checkpoint["staged"] = {
                "archive": copy.deepcopy(self._executor_staged_archive),
                "checkpoint": copy.deepcopy(self._executor_staged_checkpoint),
            }
        _validate_executor_checkpoint(checkpoint)
        return checkpoint

    @staticmethod
    def _preserve_operator_hold(current: dict[str, Any], checkpoint: dict[str, Any]) -> None:
        """Keep a later operator pause/stop while replaying an older outcome."""

        state = current.get("state")
        if not current.get("stopRequested") and state not in {
            ControllerState.PAUSED.value,
            ControllerState.STOPPED.value,
            ControllerState.BLOCKED.value,
        }:
            return
        for key in (
            "state",
            "resumeState",
            "stopRequested",
            "closeoutRequested",
            "restartRequiresResume",
            "blocker",
            "generation",
            "leaseGeneration",
        ):
            if key in current:
                checkpoint[key] = copy.deepcopy(current[key])

    def _recover_staged_executor_outcome(self, *, recover_on_restart: bool) -> None:
        """Publish a durable staged outcome without caller-supplied evidence."""

        with self._mutex:
            archive = copy.deepcopy(self._executor_staged_archive)
            checkpoint = copy.deepcopy(self._executor_staged_checkpoint)
            if not isinstance(archive, dict) or not isinstance(checkpoint, dict):
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._blocker = "executor outcome staging is incomplete; reconciliation required"
                self._executor_restart_blocked = True
                return
            current = self.journal.read()
            self._preserve_operator_hold(current, checkpoint)
            try:
                self.journal.append_archive_bundle({"executor": archive}, checkpoint=checkpoint)
            except Exception as exc:
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._blocker = f"executor outcome recovery is unresolved: {exc}"
                self._executor_restart_blocked = True
                try:
                    self._save(last_action="executor_outcome_recovery_blocked")
                except Exception:
                    pass
                return
            self._executor_staged_archive = None
            self._executor_staged_checkpoint = None
            self._executor_restart_blocked = bool(self._executor_overall)
            if self._executor_restart_blocked and recover_on_restart:
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._blocker = "executor click/action survived restart; explicit reconciliation required"
                self._save(last_action="executor_restart_blocked")

    def _recover_staged_mail_cycle_outcome(self, *, recover_on_restart: bool) -> None:
        """Replay a complete immutable mail outcome without the old owner."""

        with self._mutex:
            archive = copy.deepcopy(self._mail_cycle_staged_archive)
            checkpoint = copy.deepcopy(self._mail_cycle_staged_checkpoint)
            if not isinstance(archive, dict) or not isinstance(checkpoint, dict):
                self._mail_cycle_restart_blocked = True
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._blocker = "mail cycle outcome staging is incomplete; reconciliation required"
                return
            current = self.journal.read()
            self._preserve_operator_hold(current, checkpoint)
            try:
                self.journal.append_archive_bundle({"mail_cycle": archive}, checkpoint=checkpoint)
            except Exception as exc:
                self._mail_cycle_restart_blocked = True
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._blocker = f"mail cycle outcome recovery is unresolved: {exc}"
                try:
                    self._save(last_action="mail_cycle_outcome_recovery_blocked")
                except Exception:
                    pass
                return
            self._mail_cycle_staged_archive = None
            self._mail_cycle_staged_checkpoint = None
            self._mail_cycle_stage = None
            self._mail_cycle_restart_blocked = False
            self._save(last_action="mail_cycle_outcome_recovered")

    def _mail_cycle_outcome_record_locked(self, *, result: str, reason: str) -> dict[str, Any]:
        if result not in {"complete", "blocked"} or not isinstance(reason, str):
            raise JournalError("mail cycle outcome result is malformed")
        outcome_id = hashlib.sha256(
            f"mail-cycle|{self._mail_cycle['cycleId']}|{self._mail_cycle['ackUid']}|"
            f"{self._mail_cycle['admittedSeq']}|{result}".encode()
        ).hexdigest()
        return {
            "outcomeId": outcome_id,
            "cycleId": self._mail_cycle["cycleId"],
            "ackUid": self._mail_cycle["ackUid"],
            "queueId": self._mail_cycle["queueId"],
            "generation": self._mail_cycle["generation"],
            "leaseGeneration": self._mail_cycle["leaseGeneration"],
            "admittedSeq": self._mail_cycle["admittedSeq"],
            "mailUnits": self._mail_cycle["mailUnits"],
            "budget": copy.deepcopy(self._mail_cycle["budget"]),
            "completedUnits": self._mail_cycle["completedUnits"],
            "terminalOutcomeId": self._mail_cycle["terminalOutcomeId"],
            "lastExecutorSeq": self._mail_cycle["lastExecutorSeq"],
            "effectiveDueAt": self._mail_cycle["admittedEffectiveDueAt"],
            "deadlineAt": self._mail_cycle["deadlineAt"],
            "result": result,
            "reason": reason,
        }

    def _stage_mail_cycle_outcome_locked(
        self,
        *,
        result: str,
        reason: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        """Persist the full logical outcome before publishing archive views."""

        record = self._mail_cycle_outcome_record_locked(result=result, reason=reason)
        checkpoint = copy.deepcopy(checkpoint or self._outcome_checkpoint(include_mail_cycle_stage=False))
        current = checkpoint.setdefault("current", {})
        current["mailCycleStage"] = None
        self._mail_cycle_staged_archive = copy.deepcopy(record)
        self._mail_cycle_staged_checkpoint = copy.deepcopy(checkpoint)
        self._mail_cycle_stage = {
            "archive": copy.deepcopy(record),
            "checkpoint": copy.deepcopy(checkpoint),
        }
        _validate_mail_cycle_stage(self._mail_cycle_stage)
        self._save(last_action=f"mail_cycle_{result}_staged")

    def _publish_staged_mail_cycle_outcome_locked(self) -> dict[str, Any]:
        archive = copy.deepcopy(self._mail_cycle_staged_archive)
        checkpoint = copy.deepcopy(self._mail_cycle_staged_checkpoint)
        if not isinstance(archive, dict) or not isinstance(checkpoint, dict):
            raise JournalError("mail cycle outcome staging is unavailable")
        published = self.journal.append_archive_bundle({"mail_cycle": archive}, checkpoint=checkpoint)
        self._mail_cycle_staged_archive = None
        self._mail_cycle_staged_checkpoint = None
        self._mail_cycle_stage = None
        # A blocked publication is durable audit evidence but remains a
        # same-owner restart/resume fence.  Complete outcomes may proceed to
        # ordinary reconciliation after their archive commits.
        self._mail_cycle_restart_blocked = archive.get("result") == "blocked"
        try:
            self._save(last_action="mail_cycle_outcome_published")
        except Exception:
            # The bundle checkpoint is already authoritative; a later owner
            # can replay it without the dead process's in-memory fields.
            pass
        return copy.deepcopy(published["mail_cycle"])

    def _calibration_gate_state(self) -> ControllerState:
        runtime_evidence = self._runtime_startup_evidence
        if runtime_evidence is not None and runtime_evidence.is_live:
            return ControllerState.ARMED
        if not self._native_calibrated:
            return ControllerState.NATIVE_CALIBRATION
        if not self._input_calibrated or self._input_evidence is None:
            return ControllerState.INPUT_CALIBRATION
        return ControllerState.ARMED

    def _archive_outcome(self, *, result: str, reason: str, kind: str = "rejected") -> None:
        """Archive a complete run outcome and its current acknowledgement witnesses."""
        if self._evidence_meta.get("outcomeArchived"):
            self._qualification_pending = None
            return
        evidence = copy.deepcopy(self._evidence_meta)
        uid = str(evidence.get("uid") or self._last_uid)
        outcome_id = hashlib.sha256(
            f"{uid}|{evidence.get('startedAt', 0)}|{result}|{reason}".encode()
        ).hexdigest()
        run = {
            "outcomeId": outcome_id,
            "uid": uid,
            "startedAt": evidence.get("startedAt", 0),
            "finishedAt": evidence.get("finishedAt", 0),
            "rowCount": evidence.get("rowCount", 0),
            "pricedRowCount": evidence.get("priced", evidence.get("pricedRowCount", 0)),
            "itemEvidence": copy.deepcopy(evidence.get("itemEvidence", {})),
            "ownedRows": copy.deepcopy(evidence.get("ownedRows", [])),
            "ownedPricedRowCount": evidence.get("ownedPricedRowCount", 0),
            "qualification": result,
            "streak": self._streak,
            "reason": reason,
            "evidence": evidence,
            "fullEvidence": copy.deepcopy(evidence.get("fullEvidence", {})),
            "uploadTimestamp": evidence.get("uploadTimestamp", 0),
            "uploadLatencySeconds": evidence.get("uploadLatencySeconds", 0),
            "ackPublicationAt": evidence.get("ackPublicationAt", 0),
            "ackPublicationLatencySeconds": evidence.get("ackPublicationLatencySeconds", 0),
            "ackLoadedAt": evidence.get("ackLoadedAt", 0),
            "loadedAt": evidence.get("ackLoadedAt", 0),
            "loadedBoot": evidence.get("ackLoadedBoot", ""),
            "loadedSequence": evidence.get("ackLoadedSequence", 0),
            "ackLatencySeconds": evidence.get("ackLatencySeconds", 0),
            "ackLoadedLatencySeconds": evidence.get("ackLoadedLatencySeconds", 0),
            "companionUploadedUID": evidence.get("atomicCompanionUploadedUid", ""),
            "dataAckUID": evidence.get("dataAckUid", ""),
            "loadedUID": evidence.get("ackLoadedUID", ""),
            "evidenceUID": evidence.get("qualifiedEvidenceUID", ""),
            "generation": self._generation,
        }
        cadence = {
            "outcomeId": outcome_id,
            "uid": uid,
            "result": result,
            "reason": reason,
            "acceptedBefore": evidence.get("acceptedBefore", self._streak),
            "streak": self._streak,
            "startedAt": evidence.get("startedAt", 0),
            "finishedAt": evidence.get("finishedAt", 0),
            "evidence": evidence,
            "generation": self._generation,
        }
        acknowledgement = {
            "outcomeId": outcome_id,
            "uid": uid,
            "result": result,
            "reason": reason,
            "uploadTimestamp": evidence.get("uploadTimestamp", 0),
            "uploadLatencySeconds": evidence.get("uploadLatencySeconds", 0),
            "ackPublicationAt": evidence.get("ackPublicationAt", 0),
            "ackPublicationLatencySeconds": evidence.get("ackPublicationLatencySeconds", 0),
            "ackLoadedAt": evidence.get("ackLoadedAt", 0),
            "loadedAt": evidence.get("ackLoadedAt", 0),
            "loadedBoot": evidence.get("ackLoadedBoot", ""),
            "loadedSequence": evidence.get("ackLoadedSequence", 0),
            "ackLatencySeconds": evidence.get("ackLatencySeconds", 0),
            "ackLoadedLatencySeconds": evidence.get("ackLoadedLatencySeconds", 0),
            "companionUploadedUID": evidence.get("atomicCompanionUploadedUid", ""),
            "dataAckUID": evidence.get("dataAckUid", ""),
            "loadedUID": evidence.get("ackLoadedUID", ""),
            "evidenceUID": evidence.get("qualifiedEvidenceUID", ""),
            "evidence": evidence,
            "generation": self._generation,
        }
        evidence_record = {
            "outcomeId": outcome_id,
            "uid": uid,
            "kind": kind,
            "result": result,
            "reason": reason,
            "generation": self._generation,
            "fullEvidence": copy.deepcopy(evidence.get("fullEvidence", {})),
            "evidence": evidence,
        }
        checkpoint = self._outcome_checkpoint()
        # The manifest checkpoint must carry the same terminal marker as the
        # archive.  If publication succeeds and the following live save
        # fails, a fresh owner must replay the committed outcome without
        # attempting a second logical result.
        checkpoint["current"]["evidence"]["outcomeArchived"] = True
        # The durable outcome is now self-contained; do not leave a full
        # retry payload in the committed checkpoint.
        checkpoint["qualificationPending"] = None
        self.journal.append_archive_bundle(
            {
                "runs": run,
                "evidence": evidence_record,
                "cadence": cadence,
                "acknowledgements": acknowledgement,
            },
            checkpoint=checkpoint,
        )
        self._evidence_meta["outcomeArchived"] = True
        self._qualification_pending = None

    def _set_stable_scan_evidence(self, evidence: SweepEvidence) -> None:
        """Install the complete stable scan record for one UID."""

        envelope_id = evidence.envelope.get("id", evidence.uid)
        envelope_house = evidence.envelope.get("house", {})
        if envelope_id != evidence.uid or (
            isinstance(envelope_house, dict)
            and envelope_house.get("key")
            and envelope_house.get("key") != evidence.house
        ):
            raise DriverError("stable scan proof identity mismatch")
        self._evidence_meta = {
            "uid": evidence.uid,
            "startedAt": int(evidence.envelope["startedAt"]),
            "finishedAt": int(evidence.envelope["finishedAt"]),
            "planHash": evidence.plan_hash,
            "observations": evidence.observations,
            "attempted": evidence.attempted,
            "full": evidence.full,
            "bounded": evidence.bounded,
            "incomplete": evidence.incomplete,
            "unqueried": evidence.unqueried,
            "priced": evidence.priced,
            "rowCount": evidence.row_count,
            "kinds": list(evidence.kinds),
            "queryTimes": copy.deepcopy(evidence.query_times),
            "itemEvidence": copy.deepcopy(evidence.item_evidence),
            "ownedRows": copy.deepcopy(evidence.owned_rows),
            "ownedPricedRowCount": evidence.owned_priced_row_count,
            "fullEvidence": self._full_evidence_record(evidence),
            "qualification": "pending",
            "stableSavedUid": evidence.uid,
            "stableHouse": evidence.house,
            "stablePlanHash": evidence.plan_hash,
            "stableContext": copy.deepcopy(evidence.envelope.get("context", {})),
        }

    @staticmethod
    def _full_evidence_record(evidence: SweepEvidence) -> dict[str, Any]:
        """Serialize every field needed to reconstruct public qualification."""

        record = evidence.archive_record()
        record.update(
            {
                "payload": copy.deepcopy(evidence.payload),
                "stableVerified": evidence.stable_verified,
                "rowKinds": copy.deepcopy(evidence.row_kinds),
            }
        )
        return record

    @staticmethod
    def _evidence_from_record(record: dict[str, Any]) -> SweepEvidence:
        """Reconstruct a complete public ``SweepEvidence`` from a checkpoint."""

        if not isinstance(record, dict) or record.get("stableVerified") is not True:
            raise DriverError("qualification checkpoint lacks stable evidence")
        required = {
            "uid",
            "house",
            "planHash",
            "observations",
            "attempted",
            "full",
            "bounded",
            "incomplete",
            "unqueried",
            "pricedRowCount",
            "rowCount",
            "kinds",
            "queryDurations",
            "queryTimes",
            "itemEvidence",
            "ownedRows",
            "ownedPricedRowCount",
            "envelope",
            "payload",
        }
        if not required.issubset(record):
            raise DriverError("qualification checkpoint is incomplete")
        try:
            query_times = {
                str(key): (int(value[0]), int(value[1]))
                for key, value in record["queryTimes"].items()
                if isinstance(value, (list, tuple)) and len(value) == 2
            }
            if len(query_times) != len(record["queryTimes"]):
                raise ValueError("query timing evidence is malformed")
            return SweepEvidence(
                uid=str(record["uid"]),
                house=str(record["house"]),
                plan_hash=str(record["planHash"]),
                observations=int(record["observations"]),
                attempted=int(record["attempted"]),
                full=int(record["full"]),
                bounded=int(record["bounded"]),
                incomplete=int(record["incomplete"]),
                unqueried=int(record["unqueried"]),
                priced=int(record["pricedRowCount"]),
                row_count=int(record["rowCount"]),
                kinds=tuple(str(item) for item in record["kinds"]),
                query_durations=tuple(int(item) for item in record["queryDurations"]),
                envelope=copy.deepcopy(record["envelope"]),
                payload=copy.deepcopy(record["payload"]),
                stable_verified=True,
                query_times=query_times,
                row_kinds={str(key): int(value) for key, value in record.get("rowKinds", {}).items()},
                item_evidence=copy.deepcopy(record["itemEvidence"]),
                owned_rows=copy.deepcopy(record["ownedRows"]),
                owned_priced_row_count=int(record["ownedPricedRowCount"]),
                # Records written before the completion witness existed do
                # not prove the owned query; they cannot qualify.
                owned_query_complete=record.get("ownedQueryComplete") is True,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DriverError(f"qualification checkpoint is malformed: {exc}") from exc

    def _stage_qualification_pending(self, evidence: SweepEvidence, *, started_at: int) -> None:
        """Durably stage full evidence before mutating the outcome transaction."""

        self._qualification_pending = {
            "startedAt": int(started_at),
            "evidence": self._full_evidence_record(evidence),
            "metadata": copy.deepcopy(self._evidence_meta),
        }
        self._state = ControllerState.QUALIFY_PENDING
        self._pending = True
        self._last_uid = evidence.uid
        self._save(last_action="qualification_pending")

    def _qualification_state_snapshot(self) -> dict[str, Any]:
        """Capture the exact staged state before an outcome is mutated."""

        fields = (
            "_state",
            "_resume_state",
            "_pending",
            "_last_uid",
            "_blocker",
            "_evidence_meta",
            "_qualification_pending",
            "_accepted",
            "_total_accepted",
            "_streak",
            "_cadence_seed_start",
            "_deadline",
            "_deadline_at",
            "_closeout_requested",
            "_stop_requested",
            "_action_intent",
            "_action_attempt",
            "_action_result",
            "_action_started_at",
            "_expected_boot",
            "_frame_boot",
            "_frame_sequence",
            "_frame_crc32",
            "_restart_reference_boot",
            "_restart_observed_boot",
            "_reload_boot",
            "_started_at",
            "_restart_uncertain",
            "_reconciliation_required",
            "_lease_generation",
            "_native_calibrated",
            "_input_calibrated",
            "_calibration_evidence",
            "_calibration_history",
            "_calibration_invalidated_reason",
            "_runtime_readiness_invalidation",
        )
        return {field: copy.deepcopy(getattr(self, field)) for field in fields}

    def _restore_qualification_state(self, snapshot: dict[str, Any]) -> None:
        """Restore staged state after an outcome publication failure."""

        for field, value in snapshot.items():
            setattr(self, field, copy.deepcopy(value))

    def _archive_staged_outcome(
        self, snapshot: dict[str, Any], *, result: str, reason: str, kind: str
    ) -> None:
        """Publish an outcome without losing its durable staged retry state."""

        try:
            self._archive_outcome(result=result, reason=reason, kind=kind)
        except JournalError:
            self._restore_qualification_state(snapshot)
            raise

    def _park_recovered_terminal(self, status: ControllerStatus) -> ControllerStatus:
        """Require explicit resume and fresh gates after disk-only completion."""

        if not self._fresh_owner or status.state not in {
            ControllerState.REOPEN,
            ControllerState.DISK_CLEANUP_SENT,
        }:
            return status
        self._resume_state = status.state
        self._state = ControllerState.PAUSED
        self._reconciliation_required = True
        self._restart_uncertain = False
        self._blocker = "recovered outcome requires explicit resume and fresh calibration"
        self._fresh_owner = False
        self._save(last_action="recovered_outcome_requires_resume")
        return self.status()

    def _reject_unrecoverable_cycle(self, reason: str) -> ControllerStatus:
        """Persist a truthful rejected outcome when an in-flight cycle is disproved."""

        if not reason:
            raise ValueError("rejection reason must be nonempty")
        prior_state = self._qualification_state_snapshot()
        if not self._evidence_meta:
            self._evidence_meta = {
                "uid": self._last_uid,
                "startedAt": self._started_at,
                "finishedAt": 0,
                "qualification": "rejected",
            }
        else:
            self._evidence_meta["qualification"] = "rejected"
        self._evidence_meta["rejectionReason"] = reason
        self._pending = False
        self._restart_uncertain = False
        self._reconciliation_required = False
        self._blocker = reason
        self._state = ControllerState.BLOCKED
        self._clear_intent("rejected_after_restart")
        self._archive_staged_outcome(
            prior_state,
            result="rejected",
            reason=reason,
            kind="restart_rejection",
        )
        self._save(last_action="restart_rejected")
        return self.status()

    def _reject_empty_scan_attempt(self, reason: str) -> ControllerStatus:
        """Archive a dispatched scan that proved no new data was produced."""

        if not reason:
            raise ValueError("rejection reason must be nonempty")
        prior_state = self._qualification_state_snapshot()
        started_at = self._started_at
        self._evidence_meta = {
            "uid": "",
            "startedAt": started_at,
            "finishedAt": 0,
            "qualification": "rejected",
            "rejectionReason": reason,
            "attemptedAction": ActionKind.SCAN.value,
            "attemptObservation": "ready_empty_stable_queues",
        }
        self._pending = False
        self._restart_uncertain = False
        self._reconciliation_required = False
        self._blocker = reason
        self._state = ControllerState.BLOCKED
        self._resume_state = ControllerState.WAIT_DUE
        self._started_at = 0
        self._last_uid = ""
        self._clear_intent("rejected_ready_empty_scan")
        self._archive_staged_outcome(prior_state, result="rejected", reason=reason, kind="scan_noop")
        self._save(last_action="scan_noop_rejected")
        return self.status()

    def _save(self, *, last_action: str = "", history_wall: float | None = None) -> None:
        with self._mutex:
            _validate_mail_cycle(self._mail_cycle)
            if self._mail_cycle_staged_archive is None:
                if self._mail_cycle_staged_checkpoint is not None:
                    raise JournalError("journal mail cycle stage checkpoint is unavailable")
            else:
                if not isinstance(self._mail_cycle_staged_checkpoint, dict):
                    raise JournalError("journal mail cycle stage checkpoint is unavailable")
                _validate_mail_cycle_stage(
                    {
                        "archive": self._mail_cycle_staged_archive,
                        "checkpoint": self._mail_cycle_staged_checkpoint,
                    }
                )
            if last_action and last_action not in {"frame_observed", "frame_before_reload_dispatch"}:
                self._history.append(
                    {
                        "at": self.wall_clock() if history_wall is None else history_wall,
                        "action": last_action,
                        "state": self._state.value,
                        "generation": self._generation,
                        "uid": self._last_uid,
                    }
                )
                del self._history[:-200]
            current = self.journal.read()
            current.update(
                {
                    "state": self._state.value,
                    "mode": self._mode.value,
                    "generation": self._generation,
                    "leaseGeneration": self._lease_generation,
                    "stopRequested": self._stop_requested,
                    "closeoutRequested": self._closeout_requested,
                    "restartRequiresResume": self._state
                    in {ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED},
                    "blocker": self._blocker,
                    "nativeCalibration": self._native_calibrated,
                    "inputCalibration": self._input_calibrated,
                    "calibrationEvidence": copy.deepcopy(self._calibration_evidence),
                    "calibrationHistory": copy.deepcopy(self._calibration_history),
                    "calibrationInvalidated": self._calibration_invalidated_reason,
                    "runtimeReadinessInvalidation": (
                        self._runtime_readiness_invalidation.value
                        if self._runtime_readiness_invalidation is not None
                        else None
                    ),
                    "takeoverDrill": (
                        self._takeover_drill_record.to_json()
                        if self._takeover_drill_valid and self._takeover_drill_record is not None
                        else copy.deepcopy(self._takeover_drill_raw)
                    ),
                    "restartUncertain": self._restart_uncertain,
                    "reconciliationRequired": self._reconciliation_required,
                    "qualificationPending": copy.deepcopy(self._qualification_pending),
                    "recoveryAttempts": self._recovery_attempts,
                    "recoveryActions": sorted(self._recovery_actions),
                    "history": copy.deepcopy(self._history),
                    "lastAction": last_action,
                    "qualification": {
                        "streak": self._streak,
                        "totalAccepted": self._total_accepted,
                        "cadenceSeedStart": self._cadence_seed_start,
                        "accepted": [
                            {
                                **{
                                    key: copy.deepcopy(value)
                                    for key, value in accepted.items()
                                    if key != "evidence"
                                },
                                **(
                                    {"evidence": self._bounded_evidence(accepted["evidence"])}
                                    if isinstance(accepted.get("evidence"), dict)
                                    else {}
                                ),
                            }
                            for accepted in self._accepted
                        ],
                        "lastAcceptedUid": self._accepted[-1].get("uid", "") if self._accepted else "",
                        "lastAcceptedStart": self._accepted[-1].get("startedAt", 0) if self._accepted else 0,
                        "lastAcceptedFinish": self._accepted[-1].get("finishedAt", 0)
                        if self._accepted
                        else 0,
                        "lastReason": self._blocker,
                        "format2Evidence": self._bounded_evidence(self._evidence_meta),
                        "history": copy.deepcopy(self._history),
                    },
                    "resumeState": self._resume_state.value,
                    "current": {
                        "uid": self._last_uid if self._pending else "",
                        "pending": self._pending,
                        "startedAt": self._started_at,
                        "expectedBoot": self._expected_boot,
                        "action": self._action_intent,
                        "actionAttempt": self._action_attempt,
                        "actionResult": self._action_result,
                        "actionStartedAt": self._action_started_at,
                        "frameBoot": self._frame_boot,
                        "frameSequence": self._frame_sequence,
                        "frameCrc32": self._frame_crc32,
                        "restartReferenceBoot": self._restart_reference_boot,
                        "restartObservedBoot": self._restart_observed_boot,
                        "reloadBoot": self._reload_boot,
                        "deadlineAt": self._deadline_at,
                        "evidence": self._bounded_evidence(self._evidence_meta),
                        "executor": self._executor_checkpoint(),
                        "mailCycle": copy.deepcopy(self._mail_cycle),
                        "mailCycleStage": (
                            {
                                "archive": copy.deepcopy(self._mail_cycle_staged_archive),
                                "checkpoint": copy.deepcopy(self._mail_cycle_staged_checkpoint),
                            }
                            if self._mail_cycle_staged_archive is not None
                            else None
                        ),
                    },
                }
            )
            if self._native_construction_valid:
                if self._native_construction is None:
                    current.pop("nativeConstruction", None)
                else:
                    current["nativeConstruction"] = copy.deepcopy(self._native_construction)
            else:
                # Keep malformed durable state visible for recovery/audit.  It
                # is never interpreted as an admission checkpoint.
                current["nativeConstruction"] = copy.deepcopy(self._native_construction_raw)
            self.journal.write(current)

    def _invalidate_live_calibration(self, reason: str) -> None:
        """Drop reusable gate flags after a client/display/observer change."""

        self._native_calibrated = False
        self._input_calibrated = False
        self._input_evidence = None
        self._runtime_startup_evidence = None
        self._calibration_invalidated_reason = reason

    def invalidate_runtime_readiness(self, cause: InvalidationCause) -> None:
        """Drop only process-local readiness; the durable drill is environment checked."""

        if not isinstance(cause, InvalidationCause):
            cause = InvalidationCause.UNKNOWN
        self._runtime_startup_evidence = None
        self._runtime_readiness_invalidation = cause
        self._invalidate_live_calibration(f"runtime readiness invalidated: {cause.value}")

    @property
    def runtime_readiness_invalidation(self) -> InvalidationCause | None:
        with self._mutex:
            return self._runtime_readiness_invalidation

    @property
    def takeover_drill_record(self) -> TakeoverDrillRecord | None:
        with self._mutex:
            return self._takeover_drill_record if self._takeover_drill_valid else None

    def persist_takeover_drill(self, record: TakeoverDrillRecord) -> None:
        """Persist one completed, environment-bound takeover drill."""

        parsed = TakeoverDrillRecord.parse(record.to_json())
        if parsed is None:
            raise ValueError("takeover drill record is malformed")
        with self._mutex:
            if self._state != ControllerState.PAUSED or self.driver.guardian.armed:
                raise RuntimeError("takeover drill evidence is retained only after the lease is paused")
            self._takeover_drill_record = parsed
            self._takeover_drill_valid = True
            self._takeover_drill_raw = None
            self._save(last_action="takeover_drill_passed")

    def _validate_runtime_evidence(self, evidence: RuntimeInputEvidence) -> None:
        try:
            evidence.verify_seal()
        except (TypeError, ValueError) as exc:
            raise DriverError("runtime startup evidence is not live in this process") from exc
        if not evidence.is_live:
            raise DriverError("runtime startup evidence is not live in this process")
        record = self._takeover_drill_record if self._takeover_drill_valid else None
        if record is None:
            # A stored drill for the current contract suffices; environment
            # drift (reboot, device reconnect) does not require the operator.
            raise DriverError("a durable takeover drill is required before runtime input")

    def _accept_runtime_evidence(self, evidence: RuntimeInputEvidence) -> None:
        self._validate_runtime_evidence(evidence)
        self._runtime_startup_evidence = evidence
        self._native_calibrated = True
        self._input_calibrated = True
        self._input_evidence = evidence
        self._calibration_invalidated_reason = ""
        self._runtime_readiness_invalidation = None

    def _adopt_passive_boot_if_allowed(self, evidence: RuntimeInputEvidence) -> bool:
        """Bind a fresh passive boot without crossing an in-flight effect fence."""

        if (
            self._state in _PASSIVE_BOOT_ADOPTION_BLOCKED_STATES
            or self._resume_state in _PASSIVE_BOOT_ADOPTION_BLOCKED_STATES
        ):
            return False
        if not self._native_construction_valid or self._native_construction is not None:
            return False
        if self._action_intent in _PASSIVE_BOOT_ADOPTION_BLOCKED_INTENTS and self._action_result not in {
            "",
            "complete",
            "abandoned_after_restart",
        }:
            return False
        new_boot = evidence.readiness_boot
        old_boot = self._frame_boot
        if old_boot == new_boot:
            return True
        prior_frame = (self._frame_boot, self._frame_sequence, self._frame_crc32)
        prior_history = copy.deepcopy(self._history)
        self._frame_boot = new_boot
        self._frame_sequence = 0
        self._frame_crc32 = 0
        try:
            if old_boot:
                outcome_id = hashlib.sha256(
                    f"passive-boot-adoption|{old_boot}|{new_boot}|"
                    f"{self._action_attempt}|{self._action_intent}".encode()
                ).hexdigest()
                record = {
                    "outcomeId": outcome_id,
                    "kind": "external_boot_change_adopted",
                    "oldBoot": old_boot,
                    "newBoot": new_boot,
                    "generation": self._generation,
                    "state": self._resume_state.value,
                    "intent": self._action_intent,
                }
                history = [
                    *self._history,
                    {
                        "at": self.wall_clock(),
                        "action": "external_boot_change_adopted",
                        "state": self._state.value,
                        "generation": self._generation,
                        "uid": self._last_uid,
                    },
                ][-200:]
                checkpoint = self._outcome_checkpoint()
                checkpoint["history"] = history
                checkpoint["lastAction"] = "external_boot_change_adopted"
                self.journal.append_archive_bundle({"interventions": record}, checkpoint=checkpoint)
                self._history = history
            else:
                self._save(last_action="passive_boot_bound")
        except Exception:
            self._frame_boot, self._frame_sequence, self._frame_crc32 = prior_frame
            self._history = prior_history
            raise
        return True

    def resume_with_runtime_evidence(
        self,
        evidence: RuntimeInputEvidence,
        *,
        expected_generation: int | None = None,
    ) -> ControllerStatus:
        """Bind fresh passive evidence, then use ordinary resume/reconcile fencing."""

        with self._mutex:
            if self._mail_cycle_staged_archive is not None:
                raise RuntimeError("mail cycle outcome publication is unresolved")
            if self._mail_cycle_restart_blocked:
                raise RuntimeError("mail cycle restart/recovery fence is unresolved")
            if expected_generation is not None and expected_generation != self._generation:
                raise RuntimeError("stale generation")
            if self._state not in {ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED}:
                raise RuntimeError(f"cannot resume from {self._state}")
            self._validate_runtime_evidence(evidence)
            self._adopt_passive_boot_if_allowed(evidence)
            self._accept_runtime_evidence(evidence)
        return self.resume(expected_generation=expected_generation)

    def status(self) -> ControllerStatus:
        with self._mutex:
            return ControllerStatus(
                self._state,
                self._mode,
                self._generation,
                self._lease_generation,
                self._stop_requested,
                self._closeout_requested,
                self._streak,
                self._blocker,
                self._last_uid,
                self._started_at,
                self._expected_boot,
                self._action_intent,
                self._deadline_at,
            )

    def mail_cycle_checkpoint(self) -> dict[str, Any]:
        """Return the bounded durable cycle evidence without its capability."""

        with self._mutex:
            return copy.deepcopy(self._mail_cycle)

    def mail_cycle_capability(self) -> MailCycleCapability | None:
        """Return the opaque live capability for the current process only."""

        with self._mutex:
            return self._mail_cycle_capability

    def observe_mail_cycle_completion(
        self,
        *,
        capability: MailCycleCapability,
        post_crs: dict[str, Any],
        post_executor: dict[str, Any],
        post_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Durably consume a terminal addon result without another pointer effect."""

        with self._mutex:
            if not self._mail_cycle_capability_matches_locked(capability):
                raise RuntimeError("mail cycle capability is stale or expired")
            if self._mail_cycle["phase"] != "executing":
                raise RuntimeError("mail cycle is not awaiting an executor completion")
            overall = self._executor_overall
            if not isinstance(overall, dict) or overall.get("cycleId") != capability.cycle_id:
                raise RuntimeError("executor completion is not owned by this mail cycle")
            if not isinstance(post_crs, dict) or not isinstance(post_executor, dict):
                raise ValueError("mail completion lacks exact post-frame evidence")
            last = post_executor.get("lastResult")
            if (
                not isinstance(last, dict)
                or last.get("seq") != capability.admitted_seq
                or last.get("kind") != "mail_collect"
                or last.get("status") not in _EXECUTOR_TERMINAL_RESULTS
            ):
                raise ValueError("mail completion is not an exact terminal result")
            if not isinstance(post_evidence, dict) or not post_evidence.get("rawFrameHex"):
                raise ValueError("mail completion lacks durable CRS bytes")
            prior = next(
                (
                    record
                    for record in reversed(self.journal.archives("executor"))
                    if isinstance(record, dict)
                    and record.get("cycleId") == capability.cycle_id
                    and record.get("seq") == capability.admitted_seq
                    and record.get("result") == "started"
                ),
                None,
            )
            if not isinstance(prior, dict):
                raise RuntimeError("mail completion has no prior physical attempt archive")
            if not _valid_executor_receipt(prior.get("receipt"), intent=prior):
                raise ValueError("mail completion prior physical receipt is malformed")
            pre_executor = prior.get("preExecutor")
            pre_crs = prior.get("preCrs")
            started_executor = prior.get("postExecutor")
            started_crs = prior.get("postCrs")
            if (
                not isinstance(pre_executor, dict)
                or not isinstance(pre_crs, dict)
                or not isinstance(started_executor, dict)
                or not isinstance(started_crs, dict)
            ):
                raise RuntimeError("mail completion prior physical archive is incomplete")
            if not _valid_executor_causal_transition(
                pre_crs,
                pre_executor,
                post_crs,
                post_executor,
                expected_queue=capability.queue_id,
            ):
                raise ValueError("mail completion frame is not causally bound to its pre-frame")
            if (
                post_crs.get("boot") != started_crs.get("boot")
                or post_crs.get("identity") != started_crs.get("identity")
                or post_crs.get("boot") != pre_crs.get("boot")
                or post_crs.get("identity") != pre_crs.get("identity")
            ):
                raise ValueError("mail completion changed client boot or identity")
            try:
                if protocol.unsigned_delta(post_crs["sequence"], started_crs["sequence"]) <= 0:
                    raise ValueError("mail completion frame is not a fresh successor")
            except (KeyError, TypeError, ValueError, protocol.ProtocolError) as exc:
                raise ValueError("mail completion frame sequence is not a fresh successor") from exc
            pre_ordinal = pre_executor.get("clickOrdinal")
            started_ordinal = started_executor.get("clickOrdinal")
            post_ordinal = post_executor.get("clickOrdinal")
            pre_count = pre_executor.get("resultCount")
            started_count = started_executor.get("resultCount")
            post_count = post_executor.get("resultCount")
            started_last = started_executor.get("lastResult")
            if (
                isinstance(pre_ordinal, bool)
                or not isinstance(pre_ordinal, int)
                or isinstance(started_ordinal, bool)
                or not isinstance(started_ordinal, int)
                or isinstance(post_ordinal, bool)
                or not isinstance(post_ordinal, int)
                or post_ordinal != pre_ordinal + 1
                or started_ordinal != post_ordinal
                or isinstance(pre_count, bool)
                or not isinstance(pre_count, int)
                or isinstance(started_count, bool)
                or not isinstance(started_count, int)
                or isinstance(post_count, bool)
                or not isinstance(post_count, int)
                or started_count != _expected_executor_result_count(pre_executor, pre_count)
                or post_count != started_count
                or not isinstance(started_last, dict)
                or started_last.get("status") != "started"
                or started_last.get("seq") != capability.admitted_seq
                or started_last.get("kind") != "mail_collect"
                or started_executor.get("inFlight") is not True
                or started_executor.get("phase") not in {"mail_wait", "mail_items"}
                or post_executor.get("queueId") != overall.get("queueId")
                or post_executor.get("lastClickSource") != "button"
            ):
                raise ValueError("mail completion lacks causal ordinal/result counters")
            if not _valid_executor_evidence(
                post_evidence,
                require_raw=True,
                expected_crs=post_crs,
                expected_executor=post_executor,
                contract=self.protocol_contract,
            ):
                raise ValueError("mail completion post evidence is malformed")
            current_seq = post_executor.get("currentSeq")
            current_kind = post_executor.get("currentKind")
            result = str(last["status"])
            if current_seq == 0 and current_kind == "":
                successor_valid = post_executor.get("phase") == "complete"
            elif current_seq == capability.admitted_seq and current_kind == "mail_collect":
                successor_valid = post_executor.get("blocked") is True and result in {"failed", "refused"}
            elif current_seq == capability.admitted_seq + 1 and current_kind in {
                "buy_commodity",
                "post_commodity",
                "cancel_auction",
                "vendor_sell",
                "vendor_buy",
                "craft",
                "mail_collect",
            }:
                successor_valid = result in {"dry_run", "succeeded", "skipped"}
            else:
                successor_valid = False
            if not successor_valid:
                raise ValueError("mail completion successor is not valid")
            outcome_id = hashlib.sha256(
                f"mail-observation|{capability.cycle_id}|{capability.admitted_seq}|{post_crs.get('boot')}|{post_crs.get('sequence')}".encode()
            ).hexdigest()
            # A terminal observation is a distinct asynchronous archive, not
            # a second physical receipt.  Copy only the causal identity and
            # frame evidence; the durable parent remains the sole source of
            # Guardian/pointer proof.
            archive = {
                key: copy.deepcopy(prior[key])
                for key in (
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
                )
            }
            archive.update(
                {
                    "outcomeId": outcome_id,
                    "kind": "terminal_observation",
                    "result": result,
                    "postCrs": copy.deepcopy(post_crs),
                    "postExecutor": copy.deepcopy(post_executor),
                    "postEvidence": copy.deepcopy(post_evidence),
                    "physicalAttemptId": prior.get("attemptId", ""),
                    "physicalOutcomeId": prior.get("outcomeId", ""),
                    "terminalAction": True,
                }
            )
            prior_executor_capability = self._executor_capability
            self._executor_overall = None
            self._executor_capability = None
            self._mail_cycle["lastExecutorSeq"] = capability.admitted_seq
            checkpoint = self._outcome_checkpoint()
            try:
                published = self.journal.append_archive_bundle({"executor": archive}, checkpoint=checkpoint)
            except Exception:
                self._executor_overall = copy.deepcopy(overall)
                self._executor_capability = prior_executor_capability
                self._mail_cycle["phase"] = "executing"
                raise
            if result in {"failed", "refused"}:
                # The exact terminal executor observation is archived first;
                # the mail-cycle block then fences the ACK and revokes input.
                self.block_mail_cycle(
                    f"mail executor result was {result}; no route or retry is permitted",
                    capability=capability,
                )
                return copy.deepcopy(published["executor"])
            return self.accept_mail_terminal(capability=capability, outcome_id=outcome_id)

    def observe_executor_completion(
        self,
        *,
        continuation_capability: str,
        post_crs: dict[str, Any],
        post_executor: dict[str, Any],
        post_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Archive a same-process terminal mail result without another click.

        This is the standalone interaction-mode counterpart to
        ``observe_mail_cycle_completion``.  The private continuation token is
        process-local; durable bytes can prove what happened but cannot mint a
        new permission after restart.
        """

        with self._mutex:
            if (
                not isinstance(continuation_capability, str)
                or not continuation_capability
                or continuation_capability != self._executor_capability
            ):
                raise RuntimeError("executor continuation capability is stale or unowned")
            overall = self._executor_overall
            if (
                not isinstance(overall, dict)
                or overall.get("cycleId") != ""
                or overall.get("status") != "in_flight"
            ):
                raise RuntimeError("standalone executor completion is not owned")
            if not isinstance(post_crs, dict) or not isinstance(post_executor, dict):
                raise ValueError("executor completion lacks exact post-frame evidence")
            last = post_executor.get("lastResult")
            if (
                not isinstance(last, dict)
                or last.get("seq") != overall.get("seq")
                or last.get("kind") != "mail_collect"
                or last.get("status") not in _EXECUTOR_TERMINAL_RESULTS
            ):
                raise ValueError("executor completion is not an exact terminal result")
            if not isinstance(post_evidence, dict) or not post_evidence.get("rawFrameHex"):
                raise ValueError("executor completion lacks durable CRS bytes")
            prior = next(
                (
                    record
                    for record in reversed(self.journal.archives("executor"))
                    if isinstance(record, dict)
                    and record.get("cycleId") == ""
                    and record.get("seq") == overall.get("seq")
                    and record.get("result") == "started"
                ),
                None,
            )
            if not isinstance(prior, dict):
                raise RuntimeError("executor completion has no prior physical attempt archive")
            if not _valid_executor_receipt(prior.get("receipt"), intent=prior):
                raise ValueError("executor completion prior physical receipt is malformed")
            pre_executor = prior.get("preExecutor")
            started_executor = prior.get("postExecutor")
            started_crs = prior.get("postCrs")
            if (
                not isinstance(pre_executor, dict)
                or not isinstance(started_executor, dict)
                or not isinstance(started_crs, dict)
            ):
                raise RuntimeError("executor completion prior physical archive is incomplete")
            if not _valid_executor_causal_transition(
                prior.get("preCrs"),
                pre_executor,
                post_crs,
                post_executor,
                expected_queue=overall.get("queueId"),
            ):
                raise ValueError("executor completion frame is not causally bound to its pre-frame")
            if post_crs.get("boot") != started_crs.get("boot") or post_crs.get("identity") != started_crs.get(
                "identity"
            ):
                raise ValueError("executor completion changed client boot or identity")
            try:
                if protocol.unsigned_delta(post_crs["sequence"], started_crs["sequence"]) <= 0:
                    raise ValueError("executor completion frame is not a fresh successor")
            except (KeyError, TypeError, ValueError, protocol.ProtocolError) as exc:
                raise ValueError("executor completion frame sequence is not a fresh successor") from exc
            pre_ordinal = pre_executor.get("clickOrdinal")
            started_ordinal = started_executor.get("clickOrdinal")
            post_ordinal = post_executor.get("clickOrdinal")
            pre_count = pre_executor.get("resultCount")
            started_count = started_executor.get("resultCount")
            post_count = post_executor.get("resultCount")
            started_last = started_executor.get("lastResult")
            if (
                isinstance(pre_ordinal, bool)
                or not isinstance(pre_ordinal, int)
                or isinstance(started_ordinal, bool)
                or not isinstance(started_ordinal, int)
                or isinstance(post_ordinal, bool)
                or not isinstance(post_ordinal, int)
                or started_ordinal != pre_ordinal + 1
                or post_ordinal != started_ordinal
                or isinstance(pre_count, bool)
                or not isinstance(pre_count, int)
                or isinstance(started_count, bool)
                or not isinstance(started_count, int)
                or isinstance(post_count, bool)
                or not isinstance(post_count, int)
                or started_count != _expected_executor_result_count(pre_executor, pre_count)
                or post_count != started_count
                or not isinstance(started_last, dict)
                or started_last.get("seq") != overall.get("seq")
                or started_last.get("kind") != "mail_collect"
                or started_last.get("status") != "started"
                or started_executor.get("inFlight") is not True
                or started_executor.get("phase") not in {"mail_wait", "mail_items"}
                or post_executor.get("queueId") != overall.get("queueId")
                or post_executor.get("clickOrdinal") != started_ordinal
                or post_executor.get("resultCount") != started_count
                or post_executor.get("lastClickSource") != "button"
            ):
                raise ValueError("executor completion lacks causal ordinal/result counters")
            current_seq = post_executor.get("currentSeq")
            current_kind = post_executor.get("currentKind")
            status = str(last["status"])
            if current_seq == 0 and current_kind == "":
                successor_valid = post_executor.get("phase") == "complete"
            elif current_seq == overall.get("seq") and current_kind == "mail_collect":
                successor_valid = post_executor.get("blocked") is True and status in {
                    "failed",
                    "refused",
                }
            elif current_seq == overall.get("seq") + 1 and current_kind in {
                "buy_commodity",
                "post_commodity",
                "cancel_auction",
                "vendor_sell",
                "vendor_buy",
                "craft",
                "mail_collect",
            }:
                successor_valid = status in {"dry_run", "succeeded", "skipped"}
            else:
                successor_valid = False
            if not successor_valid:
                raise ValueError("executor completion successor is not valid")
            if not _valid_executor_evidence(
                post_evidence,
                require_raw=True,
                expected_crs=post_crs,
                expected_executor=post_executor,
                contract=self.protocol_contract,
            ):
                raise ValueError("executor completion post evidence is malformed")
            outcome_id = hashlib.sha256(
                f"executor-observation|{overall['queueId']}|{overall['seq']}|"
                f"{post_crs.get('boot')}|{post_crs.get('sequence')}".encode()
            ).hexdigest()
            # Keep the async observation separate from the physical attempt;
            # it must not inherit the parent's receipt or archive envelope.
            archive = {
                key: copy.deepcopy(prior[key])
                for key in (
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
                )
            }
            archive.update(
                {
                    "outcomeId": outcome_id,
                    "kind": "terminal_observation",
                    "result": status,
                    "postCrs": copy.deepcopy(post_crs),
                    "postExecutor": copy.deepcopy(post_executor),
                    "postEvidence": copy.deepcopy(post_evidence),
                    "physicalAttemptId": prior.get("attemptId", ""),
                    "physicalOutcomeId": prior.get("outcomeId", ""),
                    "terminalAction": True,
                }
            )
            prior_overall = copy.deepcopy(self._executor_overall)
            prior_capability = self._executor_capability
            self._executor_overall = None
            self._executor_capability = None
            checkpoint = self._outcome_checkpoint(include_executor_stage=False)
            self._executor_staged_archive = copy.deepcopy(archive)
            self._executor_staged_checkpoint = copy.deepcopy(checkpoint)
            stage_persisted = False
            try:
                self._save(last_action="executor_observation_staged")
                stage_persisted = True
                published = self.journal.append_archive_bundle({"executor": archive}, checkpoint=checkpoint)
            except Exception:
                if stage_persisted:
                    self._executor_restart_blocked = True
                else:
                    self._executor_staged_archive = None
                    self._executor_staged_checkpoint = None
                    self._executor_overall = prior_overall
                    self._executor_capability = prior_capability
                    self._executor_restart_blocked = True
                raise
            self._executor_staged_archive = None
            self._executor_staged_checkpoint = None
            self._executor_restart_blocked = False
            if status in {"failed", "refused"}:
                self.block(f"standalone mail executor result was {status}; no retry is permitted")
            return copy.deepcopy(published["executor"])

    def _block_mail_cycle_locked(self, reason: str) -> None:
        """Close a live mailbox capability without performing a second revoke."""

        if self._mail_cycle["phase"] in {"none", "complete", "blocked"}:
            return
        self._mail_cycle["phase"] = "blocked"
        self._mail_cycle["reason"] = reason
        self._mail_cycle_capability = None
        self._mail_cycle_restart_blocked = True
        self._deadline = 0.0
        self._deadline_at = 0

    def _publish_blocked_mail_cycle_locked(self, reason: str) -> None:
        """Durably publish a blocked cycle, including control-error paths."""

        if self._mail_cycle["phase"] != "blocked" or not self._mail_cycle.get("ackUid"):
            return
        if self._mail_cycle_staged_archive is None and not self.mail_cycle_ack_seen(
            self._mail_cycle["ackUid"]
        ):
            self._stage_mail_cycle_outcome_locked(result="blocked", reason=reason)
        if self._mail_cycle_staged_archive is not None:
            self._publish_staged_mail_cycle_outcome_locked()

    def _record_mail_cycle_control_failure_locked(self, reason: str) -> None:
        """Fence and archive a cycle when a control revoke itself fails."""

        self._state = ControllerState.BLOCKED
        self._resume_state = ControllerState.BLOCKED
        self._restart_uncertain = True
        self._reconciliation_required = True
        self._blocker = reason
        guardian = getattr(self.driver, "guardian", None)
        if guardian is not None and isinstance(getattr(guardian, "generation", None), int):
            self._lease_generation = guardian.generation
        self._publish_blocked_mail_cycle_locked(reason)

    def _fence_mail_cycle_admission_failure_locked(self, reason: str) -> None:
        """Turn an uncertain first checkpoint into a durable blocked owner."""

        if self._mail_cycle["phase"] in {"none", "complete", "blocked"}:
            return
        self._mail_cycle["phase"] = "blocked"
        self._mail_cycle["reason"] = reason
        self._mail_cycle_capability = None
        self._mail_cycle_restart_blocked = True
        self._state = ControllerState.BLOCKED
        self._resume_state = ControllerState.BLOCKED
        self._mode = SessionMode.INTERACTION
        self._restart_uncertain = True
        self._reconciliation_required = True
        self._deadline = 0.0
        self._deadline_at = 0
        self._blocker = reason
        self._planned_revoke = True
        revoke_error: Exception | None = None
        try:
            self._lease_generation = self.driver.revoke(reason)
        except Exception as exc:
            revoke_error = exc
            guardian = getattr(self.driver, "guardian", None)
            if guardian is not None and isinstance(getattr(guardian, "generation", None), int):
                self._lease_generation = guardian.generation
        finally:
            self._planned_revoke = False
        publish_error: Exception | None = None
        try:
            # Even a failed Guardian release must leave a durable blocked ACK;
            # otherwise the next tick could interpret the failed admission as
            # an ordinary defer and retry an uncertain cycle.
            self._publish_blocked_mail_cycle_locked(reason)
        except Exception as exc:
            publish_error = exc
            self._mail_cycle_restart_blocked = True
        if revoke_error is not None:
            if publish_error is not None:
                raise RuntimeError(
                    "mail cycle admission revoke failed "
                    f"({revoke_error}); blocked publication failed ({publish_error})"
                ) from revoke_error
            raise revoke_error
        if publish_error is not None:
            raise publish_error

    def _mail_cycle_capability_matches_locked(self, capability: MailCycleCapability) -> bool:
        return (
            isinstance(capability, MailCycleCapability)
            and capability is self._mail_cycle_capability
            and self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
            and capability.cycle_id == self._mail_cycle["cycleId"]
            and capability.queue_id == self._mail_cycle["queueId"]
            and capability.admitted_seq == self._mail_cycle["admittedSeq"]
            and capability.generation == self._generation
            and capability.lease_generation == self._lease_generation
            and self.clock() < capability.deadline_monotonic
        )

    def begin_mail_cycle(
        self,
        *,
        decision: Any,
        ack_loaded_uid: str,
        status_last_uid: str,
        wire_ack_uids: list[str] | tuple[str, ...],
        queue_id: str,
        seq: int,
        house: str,
        character: str,
        snapshot: protocol.Snapshot,
        binding: MailLoopBinding,
        bounds: MailOperationBounds,
        expected_generation: int | None = None,
        expected_lease_generation: int | None = None,
    ) -> MailCycleCapability:
        """Atomically admit one pure-policy mail decision into interaction mode."""

        with self._mutex:
            if self._mail_cycle_staged_archive is not None:
                raise RuntimeError("mail cycle outcome publication is unresolved")
            if expected_generation is not None and expected_generation != self._generation:
                raise RuntimeError("stale mail cycle generation")
            if self._mail_cycle["phase"] not in {"none", "complete", "blocked"}:
                raise RuntimeError("another mail cycle owns interaction admission")
            if self._mail_cycle["phase"] == "blocked" and self._mail_cycle.get("ackUid") == ack_loaded_uid:
                raise RuntimeError("the blocked mail cycle ACK cannot be retried")
            if self._mail_cycle["phase"] == "blocked" and self._mail_cycle.get("ackUid"):
                if not self.mail_cycle_ack_seen(self._mail_cycle["ackUid"]):
                    raise RuntimeError("blocked mail cycle publication is unresolved")
            if self._state is not ControllerState.WAIT_DUE or self._mode is not SessionMode.REPEAT:
                raise RuntimeError("mail cycle requires repeat WAIT_DUE")
            if self._stop_requested or self._closeout_requested or self._restart_uncertain:
                raise RuntimeError("mail cycle has an unresolved controller boundary")
            if self._reconciliation_required or self._pending or self._action_intent:
                raise RuntimeError("mail cycle has unresolved controller work")
            if self._executor_intent is not None or self._executor_overall is not None:
                raise RuntimeError("mail cycle has unresolved executor work")
            lease_generation = self._lease_generation
            if expected_lease_generation is not None and expected_lease_generation != lease_generation:
                raise RuntimeError("stale mail cycle lease generation")
            guardian = getattr(self.driver, "guardian", None)
            if (
                guardian is None
                or not getattr(guardian, "armed", False)
                or getattr(guardian, "generation", 0) != lease_generation
            ):
                raise RuntimeError("mail cycle requires the current armed lease")
            if (
                not getattr(decision, "admitted", False)
                or getattr(decision, "queue_id", None) != queue_id
                or getattr(decision, "admitted_seq", None) != seq
                or not isinstance(getattr(decision, "ack_uid", None), str)
                or not getattr(decision, "ack_uid", "")
            ):
                raise RuntimeError("mail cycle decision is not the exact current action")
            current_ack = self._evidence_meta.get("ackLoadedUID", "")
            if current_ack != ack_loaded_uid or self._last_uid != status_last_uid:
                raise RuntimeError("mail cycle ACK is not the controller-owned current evidence")
            if type(snapshot) is not protocol.Snapshot:
                raise RuntimeError("mail cycle requires a validated protocol Snapshot")
            if type(binding) is not MailLoopBinding or type(bounds) is not MailOperationBounds:
                raise RuntimeError("mail cycle admission policy inputs are malformed")
            if (
                not isinstance(ack_loaded_uid, str)
                or not isinstance(status_last_uid, str)
                or not ack_loaded_uid
                or ack_loaded_uid != status_last_uid
                or ack_loaded_uid != decision.ack_uid
                or not isinstance(wire_ack_uids, (list, tuple))
                or tuple(wire_ack_uids) != tuple(snapshot.value.get("loaded", {}).get("ackedIds", ()))
                or ack_loaded_uid not in snapshot.value.get("loaded", {}).get("ackedIds", ())
            ):
                raise RuntimeError("mail cycle ACK evidence is not exact")
            # Capture one coherent clock pair before rebuilding the live
            # policy boundary. Re-reading wall time after policy admission
            # could turn a rollback into extra operation authority.
            admission_wall = float(self.wall_clock())
            admission_monotonic = float(self.clock())
            if not math.isfinite(admission_wall) or not math.isfinite(admission_monotonic):
                raise RuntimeError("mail cycle admission clocks are not finite")
            live_seen = frozenset(
                record.get("ackUid")
                for record in self.journal.archives("mail_cycle")
                if isinstance(record, dict) and isinstance(record.get("ackUid"), str) and record.get("ackUid")
            )
            live_boundary = MailLoopBoundary(
                generation=self._generation,
                session_mode=self._mode.value,
                repeat_phase=self._state.value,
                ack_loaded_uid=str(current_ack),
                status_last_uid=self._last_uid,
                seen_ack_uids=live_seen,
                unresolved_action=bool(
                    self._action_intent or self._executor_intent or self._executor_overall
                ),
                reconciliation_pending=self._reconciliation_required,
                stop_requested=self._stop_requested,
                closeout_pending=self._closeout_requested,
                last_actual_start_at=(float(self._started_at) if self._started_at > 0 else None),
                now=admission_wall,
            )
            live_decision = admit_mail_cycle(snapshot, binding, live_boundary, bounds)
            if not live_decision.admitted or live_decision != decision:
                raise RuntimeError("mail cycle policy decision changed before atomic admission")
            if self.mail_cycle_ack_seen(ack_loaded_uid):
                raise RuntimeError("mail cycle ACK has already reached a terminal outcome")
            if (
                not isinstance(queue_id, str)
                or not queue_id
                or not isinstance(house, str)
                or not house
                or not isinstance(character, str)
                or not character
                or isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq <= 0
            ):
                raise RuntimeError("mail cycle queue identity is malformed")
            effective_due = getattr(decision, "effective_due_at", None)
            deadline_wall = getattr(decision, "completion_deadline_at", None)
            if (
                isinstance(effective_due, bool)
                or not isinstance(effective_due, (int, float))
                or not math.isfinite(float(effective_due))
                or float(effective_due) <= 0
                or isinstance(deadline_wall, bool)
                or not isinstance(deadline_wall, (int, float))
                or not math.isfinite(float(deadline_wall))
                or float(deadline_wall) <= admission_wall
            ):
                raise RuntimeError("mail cycle deadline is not finite and positive")
            remaining = float(deadline_wall) - admission_wall
            budget = getattr(decision, "budget", None)
            if type(budget) is not MailBudget or not isinstance(
                getattr(budget, "total_seconds", None), (int, float)
            ):
                raise RuntimeError("mail cycle decision has no finite operation budget")
            budget_record = _mail_budget_record(budget)
            _validate_mail_budget(budget_record, expected_units=int(decision.mail_units))
            if admission_wall + float(budget.total_seconds) > float(effective_due):
                raise RuntimeError("mail cycle operation budget no longer fits its due boundary")
            deadline_monotonic = admission_monotonic + remaining
            if not math.isfinite(deadline_monotonic) or deadline_monotonic <= admission_monotonic:
                raise RuntimeError("mail cycle deadline has expired")
            cycle_id = uuid.uuid4().hex
            self._mail_cycle = {
                "phase": "to_mailbox",
                "cycleId": cycle_id,
                "ackUid": ack_loaded_uid,
                "queueId": queue_id,
                "generation": self._generation,
                "leaseGeneration": lease_generation,
                "admittedSeq": seq,
                "mailUnits": int(decision.mail_units),
                "budget": budget_record,
                "completedUnits": 0,
                "terminalOutcomeId": "",
                "lastExecutorSeq": 0,
                "admittedEffectiveDueAt": float(effective_due),
                "deadlineAt": float(deadline_wall),
                "reason": "",
            }
            _validate_mail_cycle(self._mail_cycle)
            self._mail_cycle_capability = MailCycleCapability(
                cycle_id=cycle_id,
                queue_id=queue_id,
                admitted_seq=seq,
                mail_units=int(decision.mail_units),
                budget=budget,
                generation=self._generation,
                lease_generation=lease_generation,
                deadline_monotonic=deadline_monotonic,
            )
            self._mail_cycle_terminal_authority = None
            self._mode = SessionMode.INTERACTION
            self._state = ControllerState.READY
            self._resume_state = ControllerState.WAIT_DUE
            self._deadline = deadline_monotonic
            self._deadline_at = int(float(deadline_wall))
            self._blocker = ""
            try:
                self._save(last_action="mail_cycle_begin", history_wall=admission_wall)
            except Exception as exc:
                # The write may have replaced the journal before surfacing an
                # error.  Never restore WAIT_DUE or let the next tick scan:
                # fence this exact candidate, revoke the owner, and publish a
                # stable blocked ACK.  A fresh owner therefore sees either
                # the blocked checkpoint or the still-live nonterminal marker,
                # both of which refuse automatic admission.
                try:
                    self._fence_mail_cycle_admission_failure_locked(str(exc))
                except Exception as fence_exc:
                    # The fence routine has already attempted revoke and the
                    # blocked staging/publication path before surfacing its
                    # control error.  Preserve that concrete exception type
                    # (not a generic wrapper) so callers can distinguish the
                    # Guardian failure while the durable blocked ACK remains
                    # the recovery authority.
                    raise fence_exc from exc
                raise
            return self._mail_cycle_capability

    def transition_mail_cycle(
        self,
        phase: str,
        *,
        capability: MailCycleCapability,
        last_executor_seq: int | None = None,
    ) -> dict[str, Any]:
        with self._mutex:
            if phase not in _MAIL_CYCLE_PHASES - {"none", "complete", "blocked"}:
                raise ValueError("mail cycle phase is not a live transition")
            if not self._mail_cycle_capability_matches_locked(capability):
                raise RuntimeError("mail cycle capability is stale or expired")
            current_phase = self._mail_cycle["phase"]
            if phase not in _MAIL_CYCLE_TRANSITIONS.get(current_phase, frozenset()):
                raise RuntimeError(f"mail cycle phase cannot transition {current_phase}->{phase}")
            if last_executor_seq is not None:
                if isinstance(last_executor_seq, bool) or not isinstance(last_executor_seq, int):
                    raise ValueError("mail cycle executor sequence is malformed")
                if last_executor_seq != self._mail_cycle["admittedSeq"]:
                    raise ValueError("mail cycle executor sequence is not the admitted sequence")
                self._mail_cycle["lastExecutorSeq"] = last_executor_seq
            self._mail_cycle["phase"] = phase
            _validate_mail_cycle(self._mail_cycle)
            try:
                self._save(last_action=f"mail_cycle_{phase}")
            except Exception:
                self._mail_cycle["phase"] = current_phase
                self._mail_cycle_capability = None
                self._mail_cycle_restart_blocked = True
                raise
            return copy.deepcopy(self._mail_cycle)

    def _validate_mail_terminal_archive_locked(
        self, capability: MailCycleCapability, outcome_id: str
    ) -> dict[str, Any]:
        """Validate one already durable causal executor terminal outcome.

        This is deliberately archive-backed: a caller cannot manufacture
        return authority by passing a receipt or projection to the controller.
        The raw post frame remains the source of the terminal sequence/kind,
        successor, click ordinal, and result-count witnesses.
        """

        if not isinstance(outcome_id, str) or not outcome_id:
            raise ValueError("mail terminal outcome identity is malformed")
        record = next(
            (
                item
                for item in reversed(self.journal.archives("executor"))
                if isinstance(item, dict) and item.get("outcomeId") == outcome_id
            ),
            None,
        )
        if not isinstance(record, dict):
            raise RuntimeError("mail terminal outcome is not durably archived")
        if (
            record.get("cycleId") != capability.cycle_id
            or record.get("queueId") != capability.queue_id
            or record.get("seq") != capability.admitted_seq
            or record.get("generation") != capability.generation
            or record.get("leaseGeneration") != capability.lease_generation
            or record.get("result") not in {"dry_run", "succeeded"}
            or record.get("terminalAction") is not True
        ):
            raise RuntimeError("mail terminal outcome identity or result is not exact")
        post_crs = record.get("postCrs")
        post_executor = record.get("postExecutor")
        post_evidence = record.get("postEvidence")
        pre_crs = record.get("preCrs")
        pre_executor = record.get("preExecutor")
        pre_evidence = record.get("preEvidence")
        if (
            not isinstance(post_crs, dict)
            or not isinstance(post_executor, dict)
            or not isinstance(post_evidence, dict)
            or not isinstance(pre_crs, dict)
            or not isinstance(pre_executor, dict)
            or not isinstance(pre_evidence, dict)
        ):
            raise RuntimeError("mail terminal outcome lacks complete pre/post evidence")
        if not _valid_executor_evidence(
            pre_evidence,
            require_raw=True,
            expected_crs=pre_crs,
            expected_executor=pre_executor,
            contract=self.protocol_contract,
        ):
            raise RuntimeError("mail terminal outcome pre evidence is malformed")
        if record.get("kind") == "terminal_observation":
            physical_outcome_id = record.get("physicalOutcomeId")
            physical_attempt_id = record.get("physicalAttemptId")
            parent = next(
                (
                    item
                    for item in reversed(self.journal.archives("executor"))
                    if isinstance(item, dict) and item.get("outcomeId") == physical_outcome_id
                ),
                None,
            )
            if (
                not isinstance(physical_outcome_id, str)
                or not physical_outcome_id
                or not isinstance(physical_attempt_id, str)
                or not physical_attempt_id
                or not isinstance(parent, dict)
                or parent.get("result") != "started"
                or parent.get("attemptId") != physical_attempt_id
                or parent.get("cycleId") != record.get("cycleId")
                or parent.get("queueId") != record.get("queueId")
                or parent.get("seq") != record.get("seq")
                or parent.get("generation") != record.get("generation")
                or parent.get("leaseGeneration") != record.get("leaseGeneration")
                or any(record.get(key) != parent.get(key) for key in ("preCrs", "preExecutor", "preEvidence"))
                or "receipt" in record
                or not _valid_executor_receipt(parent.get("receipt"), intent=parent)
            ):
                raise RuntimeError("mail terminal outcome physical proof is not exact")
        elif not _valid_executor_receipt(record.get("receipt"), intent=record):
            raise RuntimeError("mail terminal outcome receipt is malformed")
        if not _valid_executor_causal_transition(
            pre_crs,
            pre_executor,
            post_crs,
            post_executor,
            expected_queue=capability.queue_id,
        ):
            raise RuntimeError("mail terminal outcome is not causally bound to its pre-frame")
        last = post_executor.get("lastResult")
        if (
            not isinstance(last, dict)
            or last.get("seq") != capability.admitted_seq
            or last.get("kind") != "mail_collect"
            or last.get("status") != record.get("result")
        ):
            raise RuntimeError("mail terminal outcome is not the admitted terminal action")
        pre_ordinal = pre_executor.get("clickOrdinal")
        post_ordinal = post_executor.get("clickOrdinal")
        pre_count = pre_executor.get("resultCount")
        post_count = post_executor.get("resultCount")
        if (
            isinstance(pre_ordinal, bool)
            or not isinstance(pre_ordinal, int)
            or isinstance(post_ordinal, bool)
            or not isinstance(post_ordinal, int)
            or post_ordinal != pre_ordinal + 1
            or isinstance(pre_count, bool)
            or not isinstance(pre_count, int)
            or isinstance(post_count, bool)
            or not isinstance(post_count, int)
            or post_count != _expected_executor_result_count(pre_executor, pre_count)
            or post_executor.get("lastClickSource") != "button"
        ):
            raise RuntimeError("mail terminal outcome lacks causal click counters")
        current_seq = post_executor.get("currentSeq")
        current_kind = post_executor.get("currentKind")
        if current_seq == 0 and current_kind == "":
            successor_valid = post_executor.get("phase") == "complete"
        elif current_seq == capability.admitted_seq + 1 and current_kind in {
            "buy_commodity",
            "post_commodity",
            "cancel_auction",
            "vendor_sell",
            "vendor_buy",
            "craft",
            "mail_collect",
        }:
            successor_valid = post_executor.get("phase") == "ready" and post_executor.get("inFlight") is False
        else:
            successor_valid = False
        if not successor_valid:
            raise RuntimeError("mail terminal outcome successor is not valid")
        if not _valid_executor_evidence(
            post_evidence,
            require_raw=True,
            expected_crs=post_crs,
            expected_executor=post_executor,
            contract=self.protocol_contract,
        ):
            raise RuntimeError("mail terminal outcome raw evidence is malformed")
        return copy.deepcopy(record)

    def accept_mail_terminal(self, *, capability: MailCycleCapability, outcome_id: str) -> dict[str, Any]:
        """Mint return-route authority from one durable terminal executor proof."""

        with self._mutex:
            if not self._mail_cycle_capability_matches_locked(capability):
                raise RuntimeError("mail cycle capability is stale or expired")
            if self._mail_cycle["phase"] not in {"awaiting_confirmation", "executing"}:
                raise RuntimeError("mail cycle is not awaiting a terminal executor outcome")
            archive = self._validate_mail_terminal_archive_locked(capability, outcome_id)
            prior_cycle = copy.deepcopy(self._mail_cycle)
            prior_authority = self._mail_cycle_terminal_authority
            self._mail_cycle["phase"] = "to_auctioneer"
            self._mail_cycle["terminalOutcomeId"] = outcome_id
            self._mail_cycle["lastExecutorSeq"] = capability.admitted_seq
            self._mail_cycle_terminal_authority = outcome_id
            try:
                _validate_mail_cycle(self._mail_cycle)
                self._save(last_action="mail_cycle_terminal_accepted")
            except Exception:
                self._mail_cycle = prior_cycle
                self._mail_cycle_terminal_authority = prior_authority
                raise
            return archive

    def record_mail_cycle_progress(
        self,
        *,
        capability: MailCycleCapability,
        executor: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist only causal progress from the owned executor frame."""

        with self._mutex:
            if not self._mail_cycle_capability_matches_locked(capability):
                raise RuntimeError("mail cycle capability is stale or expired")
            if not isinstance(executor, dict):
                raise ValueError("mail cycle progress frame is malformed")
            queue_id = executor.get("queueId")
            work_done = executor.get("workDone")
            work_total = executor.get("workTotal")
            if (
                isinstance(work_done, bool)
                or not isinstance(work_done, int)
                or isinstance(work_total, bool)
                or not isinstance(work_total, int)
                or work_done < 0
            ):
                raise ValueError("mail cycle progress bounds are malformed")
            last = executor.get("lastResult")
            status = last.get("status") if isinstance(last, dict) else ""
            seq = last.get("seq") if isinstance(last, dict) else None
            kind = last.get("kind") if isinstance(last, dict) else None
            if (
                not isinstance(last, dict)
                or seq != capability.admitted_seq
                or kind != "mail_collect"
                or status not in {"started", *_EXECUTOR_TERMINAL_RESULTS}
            ):
                raise ValueError("mail cycle progress result is not the admitted action")
            terminal = (
                status in _EXECUTOR_TERMINAL_RESULTS
                and seq == capability.admitted_seq
                and kind == "mail_collect"
            )
            if (
                (queue_id != capability.queue_id and not terminal)
                or (work_total <= 0 and not terminal)
                or work_done > work_total
                or (work_total > capability.mail_units and not terminal)
            ):
                raise ValueError("mail cycle progress bounds are malformed")
            if terminal:
                # ``mailUnits`` is the admitted maxMessages upper bound, not
                # an observed inbox count.  A dry-run can collect zero and a
                # real inbox can contain fewer messages; retain only the
                # causal progress published by the addon frame.
                completed = work_done
            else:
                completed = work_done
            if completed < self._mail_cycle["completedUnits"]:
                # A successful terminal callback advances the addon action
                # before publishing its successor.  That transition clears
                # the native mail counters, so a terminal frame may truthfully
                # report workDone=0 after earlier mail_items progress.  Keep
                # the already durable observed count; never infer additional
                # units from the reset.  Failed/refused/started frames and
                # malformed successors remain strict regressions.
                current_seq = executor.get("currentSeq")
                current_kind = executor.get("currentKind")
                successor_reset = (
                    terminal
                    and status in {"dry_run", "succeeded"}
                    and work_done == 0
                    and executor.get("inFlight") is False
                    and (
                        (current_seq == 0 and current_kind == "" and executor.get("phase") == "complete")
                        or (
                            current_seq == capability.admitted_seq + 1
                            and current_kind
                            in {
                                "buy_commodity",
                                "post_commodity",
                                "cancel_auction",
                                "vendor_sell",
                                "vendor_buy",
                                "craft",
                                "mail_collect",
                            }
                            and executor.get("phase") == "ready"
                        )
                    )
                )
                if not successor_reset:
                    raise ValueError("mail cycle progress regressed")
                completed = self._mail_cycle["completedUnits"]
            self._mail_cycle["completedUnits"] = min(capability.mail_units, completed)
            _validate_mail_cycle(self._mail_cycle)
            self._save(last_action="mail_cycle_progress")
            return copy.deepcopy(self._mail_cycle)

    def block_mail_cycle(
        self, reason: str, *, capability: MailCycleCapability | None = None
    ) -> ControllerStatus:
        with self._mutex:
            if not isinstance(reason, str) or not reason:
                raise ValueError("mail cycle block reason is required")
            if capability is not None and (
                not isinstance(capability, MailCycleCapability)
                or capability.cycle_id != self._mail_cycle.get("cycleId")
                or capability.generation != self._generation
                or capability.lease_generation != self._lease_generation
            ):
                raise RuntimeError("mail cycle capability is stale")
            if self._mail_cycle["phase"] in {"none", "complete", "blocked"}:
                return self.status()
            self._mail_cycle["phase"] = "blocked"
            self._mail_cycle["reason"] = reason
            self._mail_cycle_capability = None
            self._mail_cycle_terminal_authority = None
            self._mail_cycle_restart_blocked = True
            self._planned_revoke = True
            revoke_error: Exception | None = None
            try:
                self._lease_generation = self.driver.revoke(reason)
            except Exception as exc:
                # Guardian release/revoke failures are themselves an
                # uncertain boundary.  Preserve the blocked ACK before
                # re-raising so a later owner cannot infer permission from a
                # missing archive.
                revoke_error = exc
                guardian = getattr(self.driver, "guardian", None)
                if guardian is not None and isinstance(getattr(guardian, "generation", None), int):
                    self._lease_generation = guardian.generation
            finally:
                self._planned_revoke = False
            self._state = ControllerState.BLOCKED
            self._resume_state = ControllerState.BLOCKED
            self._mode = SessionMode.INTERACTION
            self._reconciliation_required = True
            self._restart_uncertain = True
            self._deadline = 0.0
            self._deadline_at = 0
            self._blocker = reason
            self._publish_blocked_mail_cycle_locked(reason)
            if revoke_error is not None:
                raise revoke_error
            return self.status()

    def complete_mail_cycle(
        self,
        *,
        capability: MailCycleCapability,
        ah_open_proof: dict[str, Any],
    ) -> dict[str, Any]:
        with self._mutex:
            if not self._mail_cycle_capability_matches_locked(capability):
                raise RuntimeError("mail cycle capability is stale or expired")
            if self._mail_cycle["phase"] != "opening_ah":
                raise RuntimeError("mail cycle is not ready for completion")
            terminal_outcome_id = self._mail_cycle.get("terminalOutcomeId")
            if (
                not isinstance(terminal_outcome_id, str)
                or not terminal_outcome_id
                or terminal_outcome_id != self._mail_cycle_terminal_authority
                or self._mail_cycle.get("lastExecutorSeq") != capability.admitted_seq
            ):
                raise RuntimeError("mail cycle completion lacks accepted terminal executor proof")
            if (
                not isinstance(ah_open_proof, dict)
                or set(ah_open_proof)
                != {"fresh", "ahOpen", "mailOpen", "identity", "generation", "leaseGeneration"}
                or ah_open_proof.get("fresh") is not True
                or ah_open_proof.get("ahOpen") is not True
                or ah_open_proof.get("mailOpen") is not False
                or not isinstance(ah_open_proof.get("identity"), str)
                or not ah_open_proof.get("identity")
                or ah_open_proof.get("generation") != self._generation
                or ah_open_proof.get("leaseGeneration") != self._lease_generation
            ):
                raise RuntimeError("mail cycle completion lacks a fresh AH-open/mail-closed proof")
            self._mail_cycle["phase"] = "complete"
            self._mail_cycle_capability = None
            self._mail_cycle_terminal_authority = None
            self._mail_cycle_restart_blocked = True
            self._generation += 1
            self._mode = SessionMode.REPEAT
            self._state = ControllerState.PAUSED
            self._resume_state = ControllerState.WAIT_DUE
            self._reconciliation_required = True
            self._restart_uncertain = False
            self._blocker = "mail cycle complete; explicit resume required"
            self._deadline = 0.0
            self._deadline_at = 0
            checkpoint = self._outcome_checkpoint()
            # Stage the complete immutable logical payload before the archive
            # bundle.  A publication failure therefore never rewrites success
            # as a blocked record and a fresh owner can replay this exact data.
            self._stage_mail_cycle_outcome_locked(result="complete", reason="", checkpoint=checkpoint)
            try:
                published = self._publish_staged_mail_cycle_outcome_locked()
            except Exception:
                self._mail_cycle_restart_blocked = True
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._reconciliation_required = True
                self._restart_uncertain = True
                self._blocker = "mail cycle completion publication failed"
                raise
            self._planned_revoke = True
            try:
                self._lease_generation = self.driver.revoke("mail cycle complete")
            except Exception as exc:
                # The complete logical outcome is already durable.  A failed
                # lease release therefore becomes a blocked reconciliation
                # boundary, never permission to route or click again.
                self._mail_cycle_restart_blocked = True
                self._state = ControllerState.BLOCKED
                self._resume_state = ControllerState.BLOCKED
                self._reconciliation_required = True
                self._restart_uncertain = True
                self._blocker = f"mail cycle completion revoke failed: {exc}"
                guardian = getattr(self.driver, "guardian", None)
                if guardian is not None and isinstance(getattr(guardian, "generation", None), int):
                    self._lease_generation = guardian.generation
                try:
                    self._save(last_action="mail_cycle_complete_revoke_failed")
                except Exception:
                    pass
                raise
            finally:
                self._planned_revoke = False
            self._mail_cycle_restart_blocked = False
            self._save(last_action="mail_cycle_complete")
            return copy.deepcopy(published)

    def mail_cycle_ack_seen(self, ack_uid: str) -> bool:
        if not isinstance(ack_uid, str) or not ack_uid:
            raise ValueError("mail cycle ACK UID is required")
        records = self.journal.archives("mail_cycle")
        return any(
            isinstance(record, dict)
            and record.get("ackUid") == ack_uid
            and record.get("result") in {"complete", "blocked"}
            for record in records
        )

    @property
    def executor_action_active(self) -> bool:
        with self._mutex:
            return self._executor_overall is not None

    def executor_click_checkpoint(self) -> dict[str, Any] | None:
        """Return a detached executor checkpoint for daemon admission/tests."""

        with self._mutex:
            return copy.deepcopy(self._executor_checkpoint())

    def executor_continuation_capability(self) -> str | None:
        """Return the private same-process capability for a started mail action."""

        with self._mutex:
            return self._executor_capability

    def begin_executor_click(
        self,
        *,
        queue_id: str,
        seq: int,
        generation: int,
        lease_generation: int,
        cycle_id: str,
        pre_crs: dict[str, Any],
        pre_ordinal: int,
        arms: dict[str, bool],
        anchor: dict[str, Any],
        confirmation: str,
        pre_executor: dict[str, Any],
        pre_evidence: dict[str, Any],
        continuation_capability: str | None = None,
    ) -> dict[str, Any]:
        """Persist one physical executor attempt before any pointer motion."""

        if not isinstance(queue_id, str) or not queue_id:
            raise ValueError("executor queueId is required")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise ValueError("executor seq must be positive")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("executor generation must be positive")
        if (
            isinstance(lease_generation, bool)
            or not isinstance(lease_generation, int)
            or lease_generation <= 0
        ):
            raise ValueError("executor lease generation must be positive")
        if not isinstance(cycle_id, str):
            raise ValueError("executor cycleId must be a string")
        if not isinstance(confirmation, str):
            raise ValueError("executor confirmation must be a string")
        if not isinstance(pre_crs, dict) or set(pre_crs) != {"boot", "sequence", "crc32", "identity"}:
            raise ValueError("executor pre-click CRS is malformed")
        if isinstance(pre_ordinal, bool) or not isinstance(pre_ordinal, int) or pre_ordinal < 0:
            raise ValueError("executor pre-click ordinal is malformed")
        if (
            not isinstance(arms, dict)
            or set(arms) != {"published", "session"}
            or any(not isinstance(value, bool) for value in arms.values())
        ):
            raise ValueError("executor arm observations are malformed")
        if not isinstance(anchor, dict) or not anchor:
            raise ValueError("executor anchor proof is required")
        if not isinstance(pre_executor, dict) or set(pre_executor) != _EXECUTOR_PROJECTION_KEYS:
            raise ValueError("executor pre-state projection is malformed")
        if not _valid_executor_evidence(
            pre_evidence,
            require_raw=True,
            expected_crs=pre_crs,
            expected_executor=pre_executor,
            contract=self.protocol_contract,
        ):
            raise ValueError("executor pre-frame evidence is malformed")
        if continuation_capability is not None and not isinstance(continuation_capability, str):
            raise ValueError("executor continuation capability is malformed")
        with self._mutex:
            if self._executor_restart_blocked:
                raise RuntimeError("executor action is blocked after restart")
            if self._executor_intent is not None:
                raise RuntimeError("an executor click intent is already in flight")
            if self._executor_overall is not None:
                expected_overall = {
                    "queueId": queue_id,
                    "seq": seq,
                    "cycleId": cycle_id,
                    "generation": generation,
                    "leaseGeneration": lease_generation,
                }
                if (
                    continuation_capability is None
                    or continuation_capability != self._executor_capability
                    or any(
                        self._executor_overall.get(key) != expected
                        for key, expected in expected_overall.items()
                    )
                ):
                    raise RuntimeError("another executor mail action owns interaction admission")
            elif continuation_capability is not None:
                raise RuntimeError("executor continuation capability is not active")
            if self._generation != generation:
                raise RuntimeError("stale executor generation")
            if self._mode is not SessionMode.INTERACTION or self._state is not ControllerState.READY:
                raise RuntimeError("executor click requires READY interaction mode")
            if self._lease_generation != lease_generation:
                raise RuntimeError("stale executor lease generation")
            guardian = self.driver.guardian
            if (
                not getattr(guardian, "armed", False)
                or getattr(guardian, "generation", 0) != lease_generation
            ):
                raise RuntimeError("executor click requires the current armed lease")
            payload = {
                "queueId": queue_id,
                "seq": seq,
                "generation": generation,
                "leaseGeneration": lease_generation,
                "cycleId": cycle_id,
                "preCrs": copy.deepcopy(pre_crs),
                "preExecutor": copy.deepcopy(pre_executor),
                "preClickOrdinal": pre_ordinal,
                "preArms": copy.deepcopy(arms),
                "anchor": copy.deepcopy(anchor),
                "confirmation": confirmation,
                "preEvidence": copy.deepcopy(pre_evidence),
                "status": "pending",
            }
            attempt_id = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            payload["attemptId"] = attempt_id
            prior_intent = self._executor_intent
            prior_overall = self._executor_overall
            prior_history = copy.deepcopy(self._history)
            self._executor_intent = payload
            if self._executor_overall is None:
                self._executor_overall = {
                    "queueId": queue_id,
                    "seq": seq,
                    "cycleId": cycle_id,
                    "generation": generation,
                    "leaseGeneration": lease_generation,
                    "status": "in_flight",
                }
            try:
                self._save(last_action="executor_intent")
            except Exception:
                # A failed pre-dispatch checkpoint is an uncertain durability
                # boundary.  Do not let the in-memory owner continue as if
                # its intent were safely persisted; a fresh owner must be the
                # only path allowed to reconcile a possibly replaced journal.
                self._executor_intent = prior_intent
                self._executor_overall = prior_overall
                self._history = prior_history
                self._executor_restart_blocked = True
                raise
            return copy.deepcopy(payload)

    def finalize_executor_click(
        self,
        *,
        attempt_id: str,
        result: str,
        post_crs: dict[str, Any] | None,
        receipt: dict[str, Any] | None,
        terminal_action: bool,
        post_executor: dict[str, Any] | None = None,
        pre_evidence: dict[str, Any] | None = None,
        post_evidence: dict[str, Any] | None = None,
        observation_error: str = "",
    ) -> dict[str, Any]:
        """Archive one physical witness atomically; never dispatches itself."""

        if result not in {"dry_run", "started", "blocked", "succeeded", "failed", "refused", "skipped"}:
            raise ValueError("executor result is not supported")
        with self._mutex:
            intent = self._executor_intent
            if not isinstance(intent, dict) or intent.get("attemptId") != attempt_id:
                raise RuntimeError("executor click intent is missing or stale")
            if result not in {"blocked", "failed", "refused", "skipped"} and (
                not isinstance(post_crs, dict)
                or not isinstance(post_executor, dict)
                or not isinstance(receipt, dict)
                or not isinstance(post_evidence, dict)
            ):
                raise ValueError("executor delivered result requires complete post evidence and receipt")
            if pre_evidence is None:
                pre_evidence = copy.deepcopy(intent.get("preEvidence"))
            if not _valid_executor_evidence(
                pre_evidence,
                require_raw=result not in {"blocked"},
                expected_crs=intent.get("preCrs"),
                expected_executor=intent.get("preExecutor"),
                contract=self.protocol_contract,
            ):
                raise ValueError("executor outcome requires complete pre evidence")
            if result not in {
                "blocked",
                "failed",
                "refused",
                "skipped",
            } and not _valid_executor_causal_transition(
                intent.get("preCrs"),
                intent.get("preExecutor"),
                post_crs,
                post_executor,
                expected_queue=intent.get("queueId"),
            ):
                raise ValueError("executor post outcome is not causally bound to its pre-frame")
            if result not in {"blocked", "failed", "refused", "skipped"} and not _valid_executor_evidence(
                post_evidence,
                require_raw=True,
                expected_crs=post_crs,
                expected_executor=post_executor,
                contract=self.protocol_contract,
            ):
                raise ValueError("executor post evidence is malformed")
            if result not in {"blocked", "failed", "refused", "skipped"} and not _valid_executor_receipt(
                receipt, intent=intent
            ):
                raise ValueError("executor physical receipt is malformed")
            if self._executor_staged_archive is not None:
                staged = copy.deepcopy(self._executor_staged_archive)
                staged_checkpoint = copy.deepcopy(self._executor_staged_checkpoint)
                if staged.get("attemptId") != attempt_id:
                    raise RuntimeError("executor outcome has a different staged attempt")
                if not isinstance(staged_checkpoint, dict):
                    raise RuntimeError("executor staged checkpoint is unavailable")
                self._preserve_operator_hold(self.journal.read(), staged_checkpoint)
                prior_intent = self._executor_intent
                prior_overall = self._executor_overall
                prior_capability = self._executor_capability
                try:
                    published = self.journal.append_archive_bundle(
                        {"executor": staged}, checkpoint=staged_checkpoint
                    )
                except Exception:
                    self._executor_intent = prior_intent
                    self._executor_overall = prior_overall
                    self._executor_capability = prior_capability
                    raise
                self._executor_staged_archive = None
                self._executor_staged_checkpoint = None
                self._executor_restart_blocked = False
                return copy.deepcopy(published["executor"])
            outcome_id = hashlib.sha256(f"executor|{attempt_id}".encode()).hexdigest()
            archive = copy.deepcopy(intent)
            archive.update(
                {
                    "outcomeId": outcome_id,
                    "result": result,
                    "postCrs": copy.deepcopy(post_crs),
                    "postExecutor": copy.deepcopy(post_executor),
                    "receipt": copy.deepcopy(receipt),
                    "preEvidence": copy.deepcopy(pre_evidence),
                    "postEvidence": copy.deepcopy(post_evidence),
                    "observationError": observation_error,
                    "terminalAction": bool(terminal_action),
                }
            )
            prior_intent = self._executor_intent
            prior_overall = self._executor_overall
            prior_capability = self._executor_capability
            if terminal_action:
                self._executor_intent = None
                self._executor_overall = None
                self._executor_capability = None
            else:
                self._executor_intent = None
                if self._executor_overall is None:
                    self._executor_overall = {
                        "queueId": intent["queueId"],
                        "seq": intent["seq"],
                        "cycleId": intent.get("cycleId", ""),
                        "generation": intent["generation"],
                        "leaseGeneration": intent["leaseGeneration"],
                        "status": "in_flight",
                    }
                if result == "started":
                    if self._executor_capability is None:
                        self._executor_capability = secrets.token_urlsafe(32)
                else:
                    self._executor_capability = None
            checkpoint = self._outcome_checkpoint(include_executor_stage=False)
            self._executor_staged_archive = copy.deepcopy(archive)
            self._executor_staged_checkpoint = copy.deepcopy(checkpoint)
            stage_persisted = False
            try:
                # Stage the complete immutable outcome and its resulting
                # controller checkpoint before any archive boundary.  If this
                # write fails, the original intent remains durable and a fresh
                # owner blocks; it must not fabricate a known success.
                self._save(last_action="executor_outcome_staged")
                stage_persisted = True
                published = self.journal.append_archive_bundle({"executor": archive}, checkpoint=checkpoint)
            except Exception:
                if stage_persisted:
                    # A durable stage is now the only retry authority.  Keep
                    # it in memory for this owner and let a fresh owner
                    # recover the same bytes from the journal checkpoint.
                    self._executor_restart_blocked = True
                else:
                    self._executor_staged_archive = None
                    self._executor_staged_checkpoint = None
                    self._executor_intent = prior_intent
                    self._executor_overall = prior_overall
                    self._executor_capability = prior_capability
                    self._executor_restart_blocked = True
                raise
            self._executor_staged_archive = None
            self._executor_staged_checkpoint = None
            self._executor_restart_blocked = False
            return copy.deepcopy(published["executor"])

    def set_action_intent(
        self,
        action: str,
        *,
        deadline_seconds: int | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Persist an action fence before any desktop dispatch."""

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            canonical = _action_kind(action).value
            # Preparation and dispatch are one effect boundary.  If the
            # caller has already prepared this exact intent, retain its
            # attempt and deadline instead of minting a second reload.
            if self._action_intent != canonical:
                self._begin_intent(canonical, deadline_seconds=deadline_seconds)
            elif deadline_seconds is not None and not self._deadline_at:
                self._deadline = self.clock() + max(0, deadline_seconds)
                self._deadline_at = self.wall_clock() + max(0, deadline_seconds)
            if canonical in {"save_reload", "ack_reload", "enter_world"} and self._frame_boot:
                self._restart_reference_boot = self._frame_boot
            self._save(last_action=f"intent:{action}")
            return self.status()

    def action_result(
        self,
        result: str,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        if not result:
            raise ValueError("action result must be nonempty")
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            self._action_result = result
            self._save(last_action=f"action:{result}")
            return self.status()

    def clear_action_intent(
        self,
        result: str = "complete",
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            self._clear_intent(result)
            self._save(last_action=f"action:{result}")
            return self.status()

    def record_recovery_attempt(
        self,
        action: str,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        if not action:
            raise ValueError("recovery action must be nonempty")
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            self._recovery_attempts += 1
            self._recovery_actions.add(action)
            self._archive(
                "interventions",
                {
                    "kind": "recovery",
                    "action": action,
                    "generation": self._generation,
                    "state": self._state.value,
                },
            )
            self._save(last_action=f"recovery:{action}")
            return self.status()

    def clear_recovery_attempts(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            self._recovery_attempts = 0
            self._recovery_actions.clear()
            self._save(last_action="recovery_clear")
            return self.status()

    def record_model_proof(
        self,
        proof: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Persist measured model capture/timing/acceptance proof only."""

        if not isinstance(proof, dict):
            raise ValueError("model proof must be an object")
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            clean = self._proof_record(proof)
            self._evidence_meta["modelProof"] = copy.deepcopy(clean)
            self._archive("evidence", {"kind": "model_proof", "proof": clean, "generation": self._generation})
            self._save(last_action="model_proof")
            return self.status()

    def record_anchor_proof(
        self,
        proof: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Persist the identity bound anchor capture and acceptance proof."""

        if not isinstance(proof, dict):
            raise ValueError("anchor proof must be an object")
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            clean = self._proof_record(proof)
            self._evidence_meta["anchorProof"] = copy.deepcopy(clean)
            self._archive(
                "evidence", {"kind": "anchor_proof", "proof": clean, "generation": self._generation}
            )
            self._save(last_action="anchor_proof")
            return self.status()

    archive_model_proof = record_model_proof
    archive_anchor_proof = record_anchor_proof

    @staticmethod
    def _proof_record(value: dict[str, Any]) -> dict[str, Any]:
        """Copy JSON proof while excluding private seals and chat contents."""

        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    str(key): clean(child)
                    for key, child in item.items()
                    if str(key) not in {"_seal", "chatText", "chattext", "chatEdit", "text"}
                }
            if isinstance(item, (list, tuple)):
                return [clean(child) for child in item]
            if isinstance(item, (str, int, float, bool)) or item is None:
                return item
            return str(item)

        return clean(value)

    @property
    def lock(self) -> threading.RLock:
        return self._mutex

    def native_construction_checkpoint(self) -> dict[str, Any] | None:
        """Return the durable native-construction checkpoint, if valid.

        A malformed persisted record is intentionally represented as no valid
        checkpoint.  ``native_construction_checkpoint_valid`` lets the daemon
        distinguish that fail-closed state without exposing the raw record.
        """

        with self._mutex:
            return copy.deepcopy(self._native_construction) if self._native_construction_valid else None

    def native_construction_checkpoint_valid(self) -> bool:
        with self._mutex:
            return self._native_construction_valid

    def set_native_construction_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
    ) -> ControllerStatus:
        if not _valid_native_construction_checkpoint(checkpoint):
            raise ValueError("native construction checkpoint is malformed")
        if expected_generation is not None and checkpoint["ownerGeneration"] != expected_generation:
            raise ValueError("native construction checkpoint generation is stale")
        with self._mutex:
            if not self._admitted(expected_generation=expected_generation, expected_state=expected_state):
                return self.status()
            self._native_construction = copy.deepcopy(checkpoint)
            self._native_construction_valid = True
            self._native_construction_raw = None
            self._save(last_action="native_construction_checkpoint")
            return self.status()

    def mark_native_construction_recovery_required(self) -> ControllerStatus:
        with self._mutex:
            if not self._native_construction_valid or self._native_construction is None:
                raise RuntimeError("native construction checkpoint is unavailable")
            checkpoint = copy.deepcopy(self._native_construction)
            checkpoint["phase"] = "recovery_required"
            self._native_construction = checkpoint
            self._save(last_action="native_construction_recovery_required")
            return self.status()

    def rebind_native_construction_frame(
        self,
        *,
        boot: str,
        sequence: int,
        crc32: int,
        reason: str,
        expected_generation: int,
        expected_state: ControllerState | str,
    ) -> ControllerStatus:
        """Persist one closed native finalize/recovery frame identity.

        This is deliberately narrower than a generic boot setter.  The daemon
        must pass the accepted frame through ``observe_frame`` immediately
        after this durable rebind, so ordinary frame churn remains fenced.
        """

        if reason not in {"native_construction_finalize", "native_construction_recovery"}:
            raise ValueError("native construction rebind reason is not allowlisted")
        expected = ControllerState(expected_state)
        required_state = (
            ControllerState.NATIVE_CALIBRATION
            if reason == "native_construction_finalize"
            else ControllerState.PAUSED
        )
        if expected is not required_state:
            raise ValueError("native construction rebind state is not allowlisted")
        if (
            not isinstance(boot, str)
            or not boot
            or len(boot.encode("utf-8")) > 96
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or isinstance(crc32, bool)
            or not isinstance(crc32, int)
            or sequence > 0xFFFFFFFF
            or crc32 < 0
            or crc32 > 0xFFFFFFFF
        ):
            raise ValueError("native construction rebind frame is malformed")
        with self._mutex:
            if not self._native_construction_valid or self._native_construction is None:
                raise RuntimeError("native construction checkpoint is unavailable")
            if not self._admitted(expected_generation=expected_generation, expected_state=expected):
                return self.status()
            self._frame_boot = boot
            self._frame_sequence = sequence
            self._frame_crc32 = crc32
            self._save(last_action=f"{reason}_rebind")
            return self.status()

    def commit_native_construction_recovery(
        self,
        *,
        boot: str,
        sequence: int,
        crc32: int,
        expected_generation: int,
        expected_state: ControllerState | str = ControllerState.PAUSED,
    ) -> ControllerStatus:
        """Atomically publish a recovery frame and consume its checkpoint.

        Recovery completion must not report success after a frame rebind has
        persisted while checkpoint deletion failed.  Both mutations therefore
        share one journal write and are rolled back in memory if that write
        fails.  The daemon performs only a read-only post-proof after this
        closed commit.
        """

        expected = ControllerState(expected_state)
        if expected is not ControllerState.PAUSED:
            raise ValueError("native recovery commit requires PAUSED state")
        if (
            not isinstance(boot, str)
            or not boot
            or len(boot.encode("utf-8")) > 96
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence > 0xFFFFFFFF
            or isinstance(crc32, bool)
            or not isinstance(crc32, int)
            or crc32 < 0
            or crc32 > 0xFFFFFFFF
        ):
            raise ValueError("native recovery frame is malformed")
        with self._mutex:
            if not self._native_construction_valid or self._native_construction is None:
                raise RuntimeError("native construction checkpoint is unavailable")
            if self._native_construction.get("phase") != "recovery_required":
                raise RuntimeError("native recovery checkpoint is not required")
            if not self._admitted(expected_generation=expected_generation, expected_state=expected):
                return self.status()
            old_frame = (self._frame_boot, self._frame_sequence, self._frame_crc32)
            old_checkpoint = copy.deepcopy(self._native_construction)
            old_history = copy.deepcopy(self._history)
            self._frame_boot, self._frame_sequence, self._frame_crc32 = boot, sequence, crc32
            self._native_construction = None
            self._native_construction_valid = True
            self._native_construction_raw = None
            try:
                self._save(last_action="native_construction_recovery_commit")
            except Exception:
                self._frame_boot, self._frame_sequence, self._frame_crc32 = old_frame
                self._native_construction = old_checkpoint
                self._native_construction_valid = True
                self._native_construction_raw = None
                self._history = old_history
                raise
            return self.status()

    def validate_native_construction_recovery_commit(
        self,
        *,
        boot: str,
        sequence: int,
        crc32: int,
        expected_generation: int,
        expected_state: ControllerState | str = ControllerState.PAUSED,
    ) -> ControllerStatus:
        """Read-only post-proof for a closed native recovery commit.

        The durable frame/checkpoint mutation is already closed by
        ``commit_native_construction_recovery``.  This method intentionally
        performs no journal write, so a post-commit verification failure
        cannot strand a consumed checkpoint before terminal publication.
        """

        expected = ControllerState(expected_state)
        if expected is not ControllerState.PAUSED:
            raise ValueError("native recovery proof requires PAUSED state")
        with self._mutex:
            if not self._admitted(expected_generation=expected_generation, expected_state=expected):
                return self.status()
            status = self.status()
            if status.state is not expected or status.generation != expected_generation:
                return status
            if (self._frame_boot, self._frame_sequence, self._frame_crc32) != (
                boot,
                sequence,
                crc32,
            ):
                raise RuntimeError("native recovery durable frame proof changed")
            if self._native_construction is not None:
                raise RuntimeError("native recovery checkpoint remained after closed commit")
            return status

    def tick(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state in {ControllerState.PAUSED, ControllerState.STOPPED}:
                # A pause or stop is a durable hold (human takeover, guardian
                # revocation, operator).  An action deadline left over from the
                # interrupted step must not rewrite that hold into a generic
                # BLOCKED state and lose its reason.
                return self.status()
            if (self._deadline and self.clock() >= self._deadline) or (
                self._deadline_at and self.wall_clock() >= self._deadline_at
            ):
                return self.block(f"deadline expired in {self._state.value}")
            return self.status()

    def observe_frame(
        self,
        *,
        boot: str,
        sequence: int,
        crc32: int,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Persist and validate one fresh decoded frame identity.

        A reload is the only operation allowed to change the boot identity.  A
        second boot during one reload wait is boot churn and blocks the session.
        """

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if not boot or sequence < 0 or crc32 < 0:
                return self.block(
                    "malformed frame evidence",
                    invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                )
            restart_boot_transition = (
                self._restart_uncertain
                and self._action_intent in {"save_reload", "ack_reload", "enter_world"}
                and bool(self._restart_reference_boot)
                and self._frame_boot == self._restart_reference_boot
                and boot != self._restart_reference_boot
            )
            if (
                self._frame_boot
                and self._state
                not in {
                    ControllerState.SAVE_RELOAD_SENT,
                    ControllerState.ACK_RELOAD_SENT,
                    ControllerState.SAVE_RELOAD_WAIT,
                    ControllerState.ACK_RELOAD_WAIT,
                    ControllerState.QUALIFY_PENDING,
                }
                and boot != self._frame_boot
                and not restart_boot_transition
            ):
                return self.block(
                    "unexpected client boot change",
                    invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                )
            if self._state in {ControllerState.SAVE_RELOAD_SENT, ControllerState.ACK_RELOAD_SENT}:
                # The sent state is entered before its reload command is
                # dispatched.  A frame from the preceding boot cannot satisfy
                # the wait; expect_boot is the durable command boundary.
                if not self._expected_boot:
                    self._frame_boot = boot
                    self._frame_sequence = sequence
                    self._frame_crc32 = crc32
                    self._save(last_action="frame_before_reload_dispatch")
                    return self.status()
                if self._expected_boot and boot == self._expected_boot:
                    return self.status()
                if self._reload_boot and boot != self._reload_boot:
                    return self.block(
                        "client boot churn during reload",
                        invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                    )
                self._reload_boot = boot
                self._expected_boot = ""
                self._state = (
                    ControllerState.ACK_RELOAD_WAIT
                    if self._state == ControllerState.ACK_RELOAD_SENT
                    else ControllerState.SAVE_RELOAD_WAIT
                )
            elif self._state in {ControllerState.SAVE_RELOAD_WAIT, ControllerState.ACK_RELOAD_WAIT}:
                if self._reload_boot and boot != self._reload_boot:
                    return self.block(
                        "client boot changed after reload",
                        invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                    )
            elif self._frame_boot and boot != self._frame_boot and not restart_boot_transition:
                return self.block(
                    "client boot changed outside reload",
                    invalidation_cause=InvalidationCause.TELEMETRY_INVALID,
                )
            if self._action_intent in {"save_reload", "ack_reload", "enter_world"}:
                if self._restart_reference_boot and boot != self._restart_reference_boot:
                    self._restart_observed_boot = self._restart_observed_boot or boot
                elif (
                    self._action_intent == "enter_world"
                    and not self._restart_reference_boot
                    and not self._frame_boot
                ):
                    # Character select has no Agent boot.  The first real
                    # world boot is the only honest post-effect observation;
                    # it becomes the persisted identity that rejects churn.
                    self._restart_observed_boot = self._restart_observed_boot or boot
            self._frame_boot = boot
            self._frame_sequence = sequence
            self._frame_crc32 = crc32
            self._save(last_action="frame_observed")
            return self.status()

    def start(
        self,
        mode: SessionMode | str = SessionMode.REPEAT,
        *,
        runtime_evidence: RuntimeInputEvidence | None = None,
        expected_generation: int | None = None,
    ) -> ControllerStatus:
        interaction_generation: int | None = None
        runtime_generation: int | None = None
        with self._mutex:
            if expected_generation is not None and expected_generation != self._generation:
                raise RuntimeError("stale generation")
            if self._state not in {ControllerState.STOPPED, ControllerState.PAUSED}:
                raise RuntimeError(f"cannot start from {self._state}")
            if self._pending:
                raise RuntimeError("pending scan work requires explicit resume")
            if runtime_evidence is not None:
                self._validate_runtime_evidence(runtime_evidence)
                if (
                    self._reconciliation_required
                    or self._restart_uncertain
                    or (
                        self._action_intent
                        and self._action_result not in {"", "complete", "abandoned_after_restart"}
                    )
                ):
                    raise RuntimeError(
                        "runtime start cannot replace unresolved durable action evidence; use resume"
                    )
                if not self._adopt_passive_boot_if_allowed(runtime_evidence):
                    raise RuntimeError(
                        "runtime start cannot adopt passive boot while durable work is unresolved; use resume"
                    )
            requested_mode = SessionMode(mode)
            if requested_mode == SessionMode.INTERACTION:
                # Interaction startup uses the same durable calibration gates as
                # repeat mode.  A stopped/paused owner has no live lease, so it
                # must never jump directly to READY or mutate the mode before a
                # gate can be represented in the journal.
                self._mode = requested_mode
                self._stop_requested = False
                self._closeout_requested = False
                self._blocker = ""
                self._generation += 1
                interaction_generation = self._generation
                if runtime_evidence is not None:
                    self._accept_runtime_evidence(runtime_evidence)
                gate = self._calibration_gate_state()
                if gate != ControllerState.ARMED or self._reconciliation_required:
                    self._state = gate if gate != ControllerState.ARMED else ControllerState.RECONCILE
                    self._resume_state = self._state
                    self._save(last_action="interaction_start_gate")
                    return self.status()
                self._state = ControllerState.PAUSED
                self._resume_state = ControllerState.WAIT_DUE
                self._save(last_action="interaction_start_rearm")
            else:
                self._mode = requested_mode
                if self._mode == SessionMode.VALIDATION:
                    # A validation invocation is a new qualification run.  Its
                    # first accepted sweep seeds cadence; prior accepted evidence
                    # must never satisfy the three-sweep requirement.
                    self._accepted = []
                    self._total_accepted = 0
                    self._streak = 0
                    self._cadence_seed_start = None
                    self._evidence_meta = {}
                self._stop_requested = False
                self._closeout_requested = False
                self._blocker = ""
                self._generation += 1
                self._state = ControllerState.NATIVE_CALIBRATION
                self._resume_state = self._state
                self._started_at = 0
                self._expected_boot = ""
                self._action_intent = ""
                self._action_attempt = 0
                self._action_result = ""
                self._action_started_at = 0
                self._frame_boot = runtime_evidence.readiness_boot if runtime_evidence is not None else ""
                self._frame_sequence = 0
                self._frame_crc32 = 0
                self._restart_reference_boot = ""
                self._restart_observed_boot = ""
                self._reload_boot = ""
                self._deadline_at = 0
                self._evidence_meta = {}
                self._cadence_seed_start = None
                self._restart_uncertain = False
                self._reconciliation_required = False
                self._recovery_attempts = 0
                self._recovery_actions.clear()
                if runtime_evidence is None:
                    self._state = ControllerState.NATIVE_CALIBRATION
                    self._resume_state = self._state
                    self._runtime_startup_evidence = None
                else:
                    self._accept_runtime_evidence(runtime_evidence)
                    self._state = ControllerState.PAUSED
                    self._resume_state = ControllerState.READY
                    runtime_generation = self._generation
                self._save(last_action="start")
                if runtime_generation is None:
                    return self.status()
        if runtime_generation is not None:
            return self.resume(expected_generation=runtime_generation)
        assert interaction_generation is not None
        return self.resume(expected_generation=interaction_generation)

    def start_with_runtime_evidence(
        self,
        evidence: RuntimeInputEvidence,
        mode: SessionMode | str = SessionMode.REPEAT,
        *,
        expected_generation: int | None = None,
    ) -> ControllerStatus:
        """Start through passive readiness and a matching durable drill."""

        return self.start(mode, runtime_evidence=evidence, expected_generation=expected_generation)

    def enter_interaction(self, *, expected_generation: int | None = None) -> ControllerStatus:
        with self._mutex:
            if expected_generation is not None and expected_generation != self._generation:
                raise RuntimeError("stale generation")
            if self._state not in {ControllerState.READY, ControllerState.WAIT_DUE}:
                raise RuntimeError(f"cannot enter interaction from {self._state}")
            if self._mail_cycle["phase"] not in {"none", "complete", "blocked"}:
                raise RuntimeError("mail cycle already owns interaction admission")
            if not self._native_calibrated or not self._input_calibrated or self._input_evidence is None:
                raise RuntimeError("interaction mode requires completed native and input calibration")
            guardian = getattr(self.driver, "guardian", None)
            if guardian is None or not guardian.armed or guardian.generation != self._lease_generation:
                raise RuntimeError("interaction mode requires the current armed input lease")
            self._mode = SessionMode.INTERACTION
            self._state = ControllerState.READY
            self._action_intent = ""
            self._action_result = ""
            self._deadline_at = 0
            self._save(last_action="interaction_enter")
            return self.status()

    def reconcile_interaction(
        self,
        *,
        identity_ok: bool,
        world_ready: bool,
        expected_generation: int | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Move a gated interaction owner to READY without touching scan work."""

        with self._mutex:
            if not self._admitted(expected_generation=expected_generation, fence=fence):
                return self.status()
            if self._mode != SessionMode.INTERACTION:
                raise RuntimeError("controller is not in interaction mode")
            if self._mail_cycle["phase"] not in {"none", "complete", "blocked"}:
                raise RuntimeError("mail cycle owns interaction admission")
            if self._state not in {
                ControllerState.ARMED,
                ControllerState.RECONCILE,
                ControllerState.READY,
                ControllerState.WAIT_DUE,
            }:
                return self.status()
            if not identity_ok:
                return self.block("interaction identity mismatch")
            if not world_ready:
                self._state = ControllerState.LOGIN_REQUIRED
                self._save(last_action="interaction_requires_world")
                return self.status()
            if not self._native_calibrated or not self._input_calibrated or self._input_evidence is None:
                self._state = self._calibration_gate_state()
                self._save(last_action="interaction_requires_calibration")
                return self.status()
            guardian = getattr(self.driver, "guardian", None)
            if guardian is None or not guardian.armed or guardian.generation != self._lease_generation:
                # Keep RECONCILE durable until the normal resume path has
                # re-armed the exact current lease.  Never claim READY here.
                self._state = ControllerState.RECONCILE
                self._save(last_action="interaction_requires_lease")
                return self.status()
            self._state = ControllerState.READY
            self._reconciliation_required = False
            self._blocker = ""
            self._save(last_action="interaction_reconcile")
            return self.status()

    def exit_interaction(self, *, expected_generation: int | None = None) -> ControllerStatus:
        with self._mutex:
            if expected_generation is not None and expected_generation != self._generation:
                raise RuntimeError("stale generation")
            if self._mode != SessionMode.INTERACTION:
                raise RuntimeError("controller is not in interaction mode")
            # A public interaction stop is also a cycle cancellation boundary;
            # never leave a live mailbox capability behind a revoked lease.
            mail_cycle_was_live = self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
            self._block_mail_cycle_locked("interaction mode exited")
            self._planned_revoke = True
            try:
                self._lease_generation = self.driver.revoke("interaction mode exited")
            except Exception as exc:
                self._record_mail_cycle_control_failure_locked(
                    f"interaction exit guardian revoke failed: {exc}"
                )
                raise
            finally:
                self._planned_revoke = False
            self._mode = SessionMode.REPEAT
            self._generation += 1
            self._state = ControllerState.PAUSED
            self._resume_state = ControllerState.PAUSED
            if mail_cycle_was_live:
                self._publish_blocked_mail_cycle_locked("interaction mode exited")
            self._save(last_action="interaction_exit")
            return self.status()

    def native_calibrated(
        self,
        evidence: Any,
        *,
        preconstruction_proof: dict[str, Any] | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.NATIVE_CALIBRATION:
                return self.block(f"native calibration is not expected in {self._state.value}")
        try:
            verify = getattr(evidence, "verify_seal", None)
            if not callable(verify):
                raise DriverError("native calibration evidence is not sealed")
            verify()
            resource_assessment = self._resource_assessment_record(evidence)
        except (DriverError, RuntimeError) as exc:
            with self._mutex:
                if not self._admitted(
                    expected_generation=expected_generation, expected_state=expected_state, fence=fence
                ):
                    return self.status()
                return self.block(str(exc))
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            prior_native_calibrated = self._native_calibrated
            prior_calibration_evidence = copy.deepcopy(self._calibration_evidence)
            prior_calibration_history = copy.deepcopy(self._calibration_history)
            prior_calibration_invalidated_reason = self._calibration_invalidated_reason
            prior_history = copy.deepcopy(self._history)
            prior_state = self._state
            self._native_calibrated = True
            record = self._evidence_record(evidence)
            record["resourceAssessment"] = copy.deepcopy(resource_assessment)
            if preconstruction_proof is not None:
                record["preconstruction"] = self._proof_record(preconstruction_proof)
            self._calibration_evidence["native"] = record
            self._calibration_history.append(
                {"gate": "native", "at": self.wall_clock(), "evidence": copy.deepcopy(record)}
            )
            del self._calibration_history[:-20]
            self._calibration_invalidated_reason = ""
            self._state = ControllerState.INPUT_CALIBRATION
            try:
                self._archive("evidence", {"kind": "native_calibration", "evidence": copy.deepcopy(record)})
                self._save(last_action="native_calibrated")
            except Exception:
                self._native_calibrated = prior_native_calibrated
                self._calibration_evidence = prior_calibration_evidence
                self._calibration_history = prior_calibration_history
                self._calibration_invalidated_reason = prior_calibration_invalidated_reason
                self._history = prior_history
                self._state = prior_state
                raise
            return self.status()

    @staticmethod
    def _resource_assessment_record(evidence: Any) -> dict[str, Any]:
        derive = getattr(evidence, "resource_assessment", None)
        if not callable(derive):
            raise DriverError("native calibration evidence has no resource assessment")
        assessment = derive()
        if not isinstance(assessment, dict):
            raise DriverError("native resource assessment is malformed")
        expected = {"classification", "method", "evidenceHash", "construction", "updates"}
        if set(assessment) != expected:
            raise DriverError("native resource assessment fields are incomplete")
        if assessment.get("classification") not in {"within_targets", "target_exceeded"}:
            raise DriverError("native resource assessment classification is invalid")
        if (
            not isinstance(assessment.get("method"), str)
            or not assessment["method"]
            or not isinstance(assessment.get("evidenceHash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", assessment["evidenceHash"])
        ):
            raise DriverError("native resource assessment identity is invalid")
        expected_hash = getattr(evidence, "evidence_hash", None)
        if assessment["evidenceHash"] != expected_hash:
            raise DriverError("native resource assessment is not bound to evidence")
        exceeded = False
        for phase in ("construction", "updates"):
            facts = assessment.get(phase)
            if not isinstance(facts, dict) or set(facts) != {"growthBytes", "targetBytes", "withinTarget"}:
                raise DriverError("native resource assessment phase facts are incomplete")
            growth = facts.get("growthBytes")
            target = facts.get("targetBytes")
            within = facts.get("withinTarget")
            if (
                isinstance(growth, bool)
                or not isinstance(growth, int)
                or growth < 0
                or isinstance(target, bool)
                or not isinstance(target, int)
                or target <= 0
                or not isinstance(within, bool)
                or within != (growth <= target)
            ):
                raise DriverError("native resource assessment phase facts are invalid")
            exceeded = exceeded or not within
        if assessment["classification"] != ("target_exceeded" if exceeded else "within_targets"):
            raise DriverError("native resource assessment classification is not derived")
        return copy.deepcopy(assessment)

    @staticmethod
    def _not_measured_resource_assessment() -> dict[str, Any]:
        return {
            "classification": "not_measured",
            "method": None,
            "evidenceHash": None,
            "current": False,
            "construction": None,
            "updates": None,
        }

    def native_resource_assessment(self) -> dict[str, Any]:
        """Return only the current, validated resource assessment state."""

        with self._mutex:
            record = self._calibration_evidence.get("native")
            if not isinstance(record, dict):
                return self._not_measured_resource_assessment()
            assessment = record.get("resourceAssessment")
            if not isinstance(assessment, dict):
                return self._not_measured_resource_assessment()
            expected = {"classification", "method", "evidenceHash", "construction", "updates"}
            if set(assessment) != expected:
                return self._not_measured_resource_assessment()
            if assessment.get("classification") not in {"within_targets", "target_exceeded"}:
                return self._not_measured_resource_assessment()
            raw_measurements = record.get("memory_measurements")
            if not isinstance(raw_measurements, list) or len(raw_measurements) != 2:
                return self._not_measured_resource_assessment()
            measurements: dict[str, NativeMemoryMeasurement] = {}
            for phase, raw in zip(("construction", "updates"), raw_measurements, strict=True):
                if not isinstance(raw, dict):
                    return self._not_measured_resource_assessment()
                try:
                    measurement = NativeMemoryMeasurement(**raw)
                    measurement.verify()
                except (CalibrationError, TypeError, ValueError):
                    return self._not_measured_resource_assessment()
                if measurement.to_dict() != raw or measurement.phase != phase:
                    return self._not_measured_resource_assessment()
                measurements[phase] = measurement
            if measurements["construction"].method != measurements["updates"].method:
                return self._not_measured_resource_assessment()
            stored_evidence = self._stored_native_evidence(record, measurements)
            if stored_evidence is None:
                return self._not_measured_resource_assessment()
            derived = stored_evidence.resource_assessment()
            if assessment != derived:
                return self._not_measured_resource_assessment()
            phase_values = {phase: derived[phase] for phase in ("construction", "updates")}
            result = {
                "classification": derived["classification"],
                "method": derived["method"],
                "evidenceHash": derived["evidenceHash"],
                "current": bool(
                    self._native_calibrated
                    and not self._calibration_invalidated_reason
                    and not (
                        self._state is ControllerState.NATIVE_CALIBRATION
                        or (
                            self._state is ControllerState.BLOCKED
                            and self._resume_state is ControllerState.NATIVE_CALIBRATION
                        )
                    )
                ),
                "construction": phase_values["construction"],
                "updates": phase_values["updates"],
            }
            return result

    @staticmethod
    def _stored_native_evidence(
        record: dict[str, Any], measurements: dict[str, NativeMemoryMeasurement]
    ) -> NativeCalibrationEvidence | None:
        """Reconstruct and rehash the full canonical native evidence record."""

        canonical_fields = (
            "fingerprint",
            "process_pid",
            "process_executable",
            "process_start",
            "window_xid",
            "child_xid",
            "geometry",
            "xinput_hierarchy",
            "crop",
            "boot",
            "sequence",
            "crc32",
            "render",
            "construction_update_count",
            "held_update_count",
            "resume_update_count",
            "native_identity_fingerprint",
            "resume_sequence",
            "resume_crc32",
            "resume_render",
            "resume_capture_monotonic",
            "capture_path",
            "capture_sha256",
            "capture_monotonic",
            "xauthority",
            "wine_prefix",
            "preconstruction_fingerprint",
            "provenance",
            "is_live",
            "evidence_hash",
        )
        if any(field not in record for field in canonical_fields):
            return None
        try:
            evidence = NativeCalibrationEvidence(
                fingerprint=record["fingerprint"],
                process_pid=record["process_pid"],
                process_executable=record["process_executable"],
                process_start=record["process_start"],
                window_xid=record["window_xid"],
                child_xid=record["child_xid"],
                geometry=record["geometry"],
                xinput_hierarchy=record["xinput_hierarchy"],
                crop=record["crop"],
                boot=record["boot"],
                sequence=record["sequence"],
                crc32=record["crc32"],
                render=record["render"],
                construction_update_count=record["construction_update_count"],
                held_update_count=record["held_update_count"],
                resume_update_count=record["resume_update_count"],
                memory_measurements=(measurements["construction"], measurements["updates"]),
                native_identity_fingerprint=record["native_identity_fingerprint"],
                resume_sequence=record["resume_sequence"],
                resume_crc32=record["resume_crc32"],
                resume_render=record["resume_render"],
                resume_capture_monotonic=record["resume_capture_monotonic"],
                capture_path=record["capture_path"],
                capture_sha256=record["capture_sha256"],
                capture_monotonic=record["capture_monotonic"],
                xauthority=record["xauthority"],
                wine_prefix=record["wine_prefix"],
                preconstruction_fingerprint=record["preconstruction_fingerprint"],
                provenance=record["provenance"],
                is_live=record["is_live"],
                evidence_hash=record["evidence_hash"],
            )
        except (KeyError, TypeError, ValueError):
            return None
        if evidence.fingerprint != evidence.content_fingerprint():
            return None
        if evidence.evidence_hash != evidence.with_hash().evidence_hash:
            return None
        return evidence

    def input_calibrated(
        self,
        *,
        evidence: Any,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                raise DriverError("input calibration generation/state was revoked")
            if self._state != ControllerState.INPUT_CALIBRATION:
                return self.block(f"input calibration is not expected in {self._state.value}")
            try:
                # Keep the admission lock across both lease-producing calls.
                # A pause/stop cannot revoke between the post-calibration
                # check and arm, then have stale work recreate the lease.
                self.driver.input_calibration(evidence)
                if not self._admitted(
                    expected_generation=expected_generation, expected_state=expected_state, fence=fence
                ):
                    raise DriverError("input calibration generation was revoked")
                self.driver.arm(evidence)
            except (DriverError, RuntimeError) as exc:
                if not self._admitted(
                    expected_generation=expected_generation, expected_state=expected_state, fence=fence
                ):
                    return self.status()
                return self.block(str(exc))
            if (
                not self._admitted(
                    expected_generation=expected_generation, expected_state=expected_state, fence=fence
                )
            ) or self._state == ControllerState.PAUSED:
                self._planned_revoke = True
                try:
                    self._lease_generation = self.driver.guardian.revoke(
                        "input calibration generation was revoked"
                    )
                finally:
                    self._planned_revoke = False
                return self.status()
            self._input_calibrated = True
            self._input_evidence = evidence
            record = self._evidence_record(evidence)
            self._calibration_evidence["input"] = record
            self._calibration_history.append(
                {"gate": "input", "at": self.wall_clock(), "evidence": copy.deepcopy(record)}
            )
            del self._calibration_history[:-20]
            self._calibration_invalidated_reason = ""
            self._state = ControllerState.ARMED
            self._lease_generation = self.driver.guardian.generation
            self._archive("evidence", {"kind": "input_calibration", "evidence": copy.deepcopy(record)})
            self._save(last_action="input_calibrated")
            return self.status()

    def retain_input_calibration_while_paused(
        self,
        evidence: Any,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            return self._retain_input_calibration_while_paused(evidence)

    def _retain_input_calibration_while_paused(self, evidence: Any) -> ControllerStatus:
        """Keep a proven takeover result without rearming revoked input."""

        if self._state != ControllerState.PAUSED:
            return self.block("input calibration was revoked outside a paused state")
        try:
            verify = getattr(evidence, "verify_seal", None)
            if not callable(verify):
                raise DriverError("input calibration evidence is not sealed")
            verify()
        except (DriverError, RuntimeError) as exc:
            return self.block(str(exc))
        self._input_calibrated = True
        self._input_evidence = evidence
        retain = getattr(self.driver, "retain_input_evidence", None)
        if callable(retain):
            try:
                retain(evidence)
            except (DriverError, RuntimeError) as exc:
                return self.block(str(exc))
        record = self._evidence_record(evidence)
        self._calibration_evidence["input"] = record
        self._calibration_history.append(
            {"gate": "input", "at": self.wall_clock(), "evidence": copy.deepcopy(record)}
        )
        del self._calibration_history[:-20]
        self._archive("evidence", {"kind": "input_calibration_retained", "evidence": copy.deepcopy(record)})
        self._save(last_action="input_calibration_retained_paused")
        return self.status()

    @staticmethod
    def _evidence_record(evidence: Any) -> dict[str, Any]:
        """Persist identity and measured proof without serializing a live object."""

        names = (
            "fingerprint",
            "process_pid",
            "process_executable",
            "window_xid",
            "child_xid",
            "target_pid",
            "target_xid",
            "target_child_xid",
            "display",
            "geometry",
            "crop",
            "boot",
            "sequence",
            "crc32",
            "render",
            "construction_update_count",
            "held_update_count",
            "resume_update_count",
            "memory_measurements",
            "native_identity_fingerprint",
            "resume_sequence",
            "resume_crc32",
            "resume_render",
            "resume_capture_monotonic",
            "capture_path",
            "capture_sha256",
            "capture_monotonic",
            "process_start",
            "preconstruction_fingerprint",
            "evidence_hash",
            "xauthority",
            "wine_prefix",
            "rss_before_bytes",
            "rss_after_construction_bytes",
            "rss_after_updates_bytes",
            "synthetic_key_source",
            "synthetic_pointer_source",
            "human_key_source",
            "human_pointer_source",
            "xinput_hierarchy",
            "synthetic_events",
            "human_events",
            "forwarded_events",
            "event_proof",
            "synthetic_modifiers",
            "synthetic_echo_proof",
            "keyboard_keymap",
            "keyboard_profile",
            "target_executable",
            "target_process_start",
            "target_geometry",
            "target_event_xids",
            "managed_frame_xid",
            "managed_frame_geometry",
            "game_identity_fingerprint",
            "game_geometry",
            "driver_version",
            "provenance",
            "is_live",
            # Optional proof emitted by measured anchor/model providers.  The
            # controller stores the proof metadata, never private model text.
            "anchor_capture_path",
            "anchor_capture_sha256",
            "anchor_capture_monotonic",
            "anchor_identity_fingerprint",
            "anchor_bounds",
            "anchor_proof",
            "model_capture_path",
            "model_capture_sha256",
            "model_capture_monotonic",
            "model_timing",
            "model_proof",
            "accepted_proof",
        )

        def primitive(value: Any) -> Any:
            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                return primitive(to_dict())
            if isinstance(value, tuple):
                return [primitive(item) for item in value]
            if isinstance(value, list):
                return [primitive(item) for item in value]
            if isinstance(value, dict):
                return {
                    str(key): primitive(item)
                    for key, item in value.items()
                    if str(key) not in {"_seal", "chatText", "chattext", "chatEdit"}
                }
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            return str(value)

        return {name: primitive(getattr(evidence, name)) for name in names if hasattr(evidence, name)}

    def reconcile(
        self,
        *,
        identity_ok: bool,
        world_ready: bool = True,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        arm_request: tuple[int, ControllerState, RuntimeInputEvidence] | None = None
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if not identity_ok:
                return self.block("identity mismatch")
            if self._restart_uncertain:
                # Fresh gates do not settle a client-side effect.  Keep the
                # observation-only boundary until the exact action witness is
                # supplied; restoring the saved phase here could replay it.
                self._state = ControllerState.RECONCILE
                self._reconciliation_required = True
                self._save(last_action="reconcile_uncertainty_pending")
                return self.status()
            if self._resume_state == ControllerState.QUALIFY_PENDING and isinstance(
                self._qualification_pending, dict
            ):
                # A staged qualification is already a complete read-only
                # recovery checkpoint.  Explicit pause/resume may re-enter
                # this boundary, but fresh input gates must never demote it
                # to WAIT_DUE or clear its retry proof.
                self._state = ControllerState.QUALIFY_PENDING
                self._reconciliation_required = False
                self._blocker = ""
                self._save(last_action="reconcile_qualification_pending")
                return self.status()
            if not world_ready:
                self._state = ControllerState.LOGIN_REQUIRED
            else:
                if not self._native_calibrated or not self._input_calibrated or self._input_evidence is None:
                    self._state = self._calibration_gate_state()
                    self._save(last_action="reconcile_requires_fresh_calibration")
                    return self.status()
                self._reconciliation_required = False
                resumable = {
                    ControllerState.SCANNING,
                    ControllerState.QUIET_SAVE,
                    ControllerState.SAVE_RELOAD_SENT,
                    ControllerState.SAVE_RELOAD_WAIT,
                    ControllerState.UPLOAD_WAIT,
                    ControllerState.ACK_RELOAD_SENT,
                    ControllerState.ACK_RELOAD_WAIT,
                    ControllerState.REOPEN,
                    ControllerState.DISK_CLEANUP_SENT,
                    ControllerState.DISK_CLEANUP_WAIT,
                }
                if self._resume_state in resumable and (self._started_at or self._last_uid):
                    self._state = self._resume_state
                else:
                    self._state = ControllerState.WAIT_DUE
                    self._clear_intent("reconcile")
                if self._state != ControllerState.WAIT_DUE and not self._deadline:
                    if self._state == ControllerState.SCANNING:
                        self._deadline = self.clock() + self.sweep_cap_seconds
                        self._deadline_at = self.wall_clock() + self.sweep_cap_seconds
                    elif self._state in {
                        ControllerState.QUIET_SAVE,
                        ControllerState.SAVE_RELOAD_SENT,
                        ControllerState.SAVE_RELOAD_WAIT,
                        ControllerState.REOPEN,
                    }:
                        self._deadline = self.clock() + self.reload_deadline_seconds
                        self._deadline_at = self.wall_clock() + self.reload_deadline_seconds
                    elif self._state in {
                        ControllerState.UPLOAD_WAIT,
                        ControllerState.ACK_RELOAD_SENT,
                        ControllerState.ACK_RELOAD_WAIT,
                        ControllerState.DISK_CLEANUP_SENT,
                        ControllerState.DISK_CLEANUP_WAIT,
                    }:
                        self._deadline = self.clock() + self.ack_deadline_seconds
                        self._deadline_at = self.wall_clock() + self.ack_deadline_seconds
            self._save(last_action="reconcile")
            result = self.status()
            if (
                world_ready
                and not self._restart_uncertain
                and not self._reconciliation_required
                and isinstance(self._runtime_startup_evidence, RuntimeInputEvidence)
                and result.state
                in {
                    ControllerState.WAIT_DUE,
                    ControllerState.SCANNING,
                    ControllerState.QUIET_SAVE,
                    ControllerState.SAVE_RELOAD_SENT,
                    ControllerState.SAVE_RELOAD_WAIT,
                    ControllerState.UPLOAD_WAIT,
                    ControllerState.ACK_RELOAD_SENT,
                    ControllerState.ACK_RELOAD_WAIT,
                    ControllerState.REOPEN,
                    ControllerState.DISK_CLEANUP_SENT,
                    ControllerState.DISK_CLEANUP_WAIT,
                }
            ):
                guardian = getattr(self.driver, "guardian", None)
                if guardian is None:
                    return self.block("runtime reconciliation has no Guardian")
                if guardian.armed:
                    if guardian.generation != self._lease_generation:
                        return self.block("runtime Guardian lease changed during reconciliation")
                    return result
                arm_request = (self._generation, result.state, self._runtime_startup_evidence)
            else:
                return result

        # Passive readiness remains observation-only while an old effect is
        # unresolved. Once the read-only reconciler has selected an action-
        # capable state, re-arm outside the controller lock and bind that
        # lease to the exact generation/state that admitted the transition.
        assert arm_request is not None
        arm_generation, arm_state, evidence = arm_request
        try:
            self.driver.arm(evidence)
        except (DriverError, RuntimeError) as exc:
            with self._mutex:
                if self._generation != arm_generation or self._state != arm_state:
                    return self.status()
            invalidation_cause = getattr(exc, "invalidation_cause", InvalidationCause.UNKNOWN)
            if not isinstance(invalidation_cause, InvalidationCause):
                invalidation_cause = InvalidationCause.UNKNOWN
            return self.block(
                f"runtime reconciliation arm failed: {exc}",
                invalidation_cause=invalidation_cause,
                expected_generation=arm_generation,
                expected_state=arm_state,
            )
        with self._mutex:
            if (
                self._generation != arm_generation
                or self._state != arm_state
                or self._stop_requested
                or self._restart_uncertain
                or self._reconciliation_required
            ):
                self._planned_revoke = True
                try:
                    self._lease_generation = self.driver.guardian.revoke(
                        "runtime reconciliation generation was revoked"
                    )
                finally:
                    self._planned_revoke = False
                return self.status()
            self._lease_generation = self.driver.guardian.generation
            self._save(last_action="runtime_reconcile_rearmed")
            return self.status()

    def resume(self, *, expected_generation: int | None = None) -> ControllerStatus:
        # IPC callers perform the generation admission while holding the
        # controller lock, then call this method without that lock because
        # re-arming the native observer may touch X11 and can block.  Repeat
        # the check at the method boundary so releasing the lock does not
        # create a stale-resume window.
        with self._mutex:
            if expected_generation is not None and self._generation != expected_generation:
                raise RuntimeError("stale generation")
            if self._mail_cycle_staged_archive is not None:
                raise RuntimeError("mail cycle outcome publication is unresolved")
            if self._mail_cycle_restart_blocked:
                raise RuntimeError("mail cycle restart/recovery fence is unresolved")
            if self._state not in {ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED}:
                raise RuntimeError(f"cannot resume from {self._state}")
            if self._resume_state == ControllerState.QUALIFY_PENDING and isinstance(
                self._qualification_pending, dict
            ):
                # A staged outcome is read-only recovery work, but returning
                # to it still requires the normal live calibration/re-arm
                # path before any retry can expose REOPEN or cleanup input.
                self._reconciliation_required = False
            # A restart never replays an uncertain command.  Resume admits a
            # new generation and enters reconciliation; fresh telemetry/files
            # decide whether the old effect completed or should be abandoned.
            if self._restart_uncertain:
                self._blocker = ""
                self._generation += 1
                self._reconciliation_required = True
                self._state = ControllerState.RECONCILE
                self._archive(
                    "interventions",
                    {
                        "kind": "restart_reconcile",
                        "generation": self._generation,
                        "intent": self._action_intent,
                        "attempt": self._action_attempt,
                    },
                )
                self._save(last_action="resume_restart_reconcile")
                return self.status()
            # A stopped owner may be resumed only to inspect/reconcile an
            # already-dispatched effect.  Clearing stop here would authorize a
            # new slash command or scan before that effect is known to settle.
            if self._stop_requested and self._state == ControllerState.STOPPED:
                self._generation += 1
                self._reconciliation_required = bool(self._action_intent and self._action_intent != "stop")
                self._state = (
                    ControllerState.RECONCILE if self._reconciliation_required else ControllerState.STOPPED
                )
                self._save(last_action="resume_stopped_reconcile")
                return self.status()
            self._stop_requested = False
            self._blocker = ""
            self._generation += 1
            resume_generation = self._generation
            resume_origin = self._state
            if self._reconciliation_required:
                if not self._native_calibrated:
                    self._state = ControllerState.NATIVE_CALIBRATION
                elif not self._input_calibrated or self._input_evidence is None:
                    self._state = ControllerState.INPUT_CALIBRATION
                else:
                    self._state = ControllerState.RECONCILE
                self._save(last_action="resume_requires_reconciliation")
                return self.status()
            if not self._native_calibrated:
                self._state = ControllerState.NATIVE_CALIBRATION
                self._save(last_action="resume_requires_native_calibration")
                return self.status()
            elif not self._input_calibrated:
                self._state = ControllerState.INPUT_CALIBRATION
                self._save(last_action="resume_requires_input_calibration")
                return self.status()
            elif self._input_evidence is None:
                # The durable flag records that the gate passed in an earlier
                # process, but its live identity evidence must be collected
                # again before this process can send input.
                self._state = ControllerState.INPUT_CALIBRATION
                self._save(last_action="resume_requires_input_calibration")
                return self.status()
            else:
                evidence = self._input_evidence

        try:
            if isinstance(evidence, RuntimeInputEvidence):
                self.driver.arm(evidence)
            else:
                self.driver.input_calibration(evidence)
                self.driver.arm(evidence)
        except (DriverError, RuntimeError) as exc:
            with self._mutex:
                if self._generation != resume_generation or self._state != resume_origin:
                    return self.status()
            invalidation_cause = getattr(exc, "invalidation_cause", InvalidationCause.UNKNOWN)
            if not isinstance(invalidation_cause, InvalidationCause):
                invalidation_cause = InvalidationCause.UNKNOWN
            return self.block(
                f"resume calibration evidence rejected: {exc}",
                invalidation_cause=invalidation_cause,
                expected_generation=resume_generation,
            )
        with self._mutex:
            if self._generation != resume_generation or self._state != resume_origin:
                # A pause can arrive while the native observer is being
                # re-armed.  Do not let this stale resume overwrite the
                # durable pause; revoke the just-created lease and leave
                # reconciliation for the explicit resume that follows.
                self._planned_revoke = True
                try:
                    self._lease_generation = self.driver.guardian.revoke("resume generation was revoked")
                finally:
                    self._planned_revoke = False
                return self.status()
            self._lease_generation = self.driver.guardian.generation
            self._state = ControllerState.RECONCILE
            if self._resume_state == ControllerState.SCANNING:
                self._deadline = self.clock() + self.sweep_cap_seconds
                self._deadline_at = self.wall_clock() + self.sweep_cap_seconds
            elif self._resume_state in {
                ControllerState.QUIET_SAVE,
                ControllerState.SAVE_RELOAD_SENT,
                ControllerState.SAVE_RELOAD_WAIT,
            }:
                self._deadline = self.clock() + self.reload_deadline_seconds
                self._deadline_at = self.wall_clock() + self.reload_deadline_seconds
            elif self._resume_state in {
                ControllerState.UPLOAD_WAIT,
                ControllerState.ACK_RELOAD_SENT,
                ControllerState.ACK_RELOAD_WAIT,
            }:
                self._deadline = self.clock() + self.ack_deadline_seconds
                self._deadline_at = self.wall_clock() + self.ack_deadline_seconds
            self._save(last_action="resume")
            return self.status()

    def pause(self, reason: str = "explicit pause") -> ControllerStatus:
        with self._mutex:
            mail_cycle_was_live = self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
            self._block_mail_cycle_locked(reason)
            if not (
                self._state in {ControllerState.PAUSED, ControllerState.BLOCKED}
                and self._resume_state
                not in {ControllerState.PAUSED, ControllerState.BLOCKED, ControllerState.STOPPED}
            ):
                self._resume_state = self._state
            self._generation += 1
            self._planned_revoke = True
            try:
                self._lease_generation = self.driver.guardian.revoke(reason)
            except Exception as exc:
                if mail_cycle_was_live:
                    self._record_mail_cycle_control_failure_locked(f"mail cycle pause revoke failed: {exc}")
                raise
            finally:
                self._planned_revoke = False
            if self._action_intent and self._action_result not in {
                "",
                "complete",
                "abandoned_after_restart",
            }:
                self._restart_uncertain = True
                self._reconciliation_required = True
            self._state = ControllerState.PAUSED
            self._blocker = reason
            if mail_cycle_was_live:
                self._publish_blocked_mail_cycle_locked(reason)
            self._save(last_action="pause")
            self._archive(
                "interventions",
                {
                    "kind": "pause",
                    "reason": reason,
                    "generation": self._generation,
                    "state": self._resume_state.value,
                },
            )
            return self.status()

    def reconcile_restart(
        self,
        *,
        observed_boot: str = "",
        observed_uid: str = "",
        uploaded_ids: set[str] | frozenset[str] = frozenset(),
        data_ack_ids: set[str] | frozenset[str] = frozenset(),
        loaded_ids: set[str] | frozenset[str] = frozenset(),
        pending_ids: set[str] | frozenset[str] = frozenset(),
        scanner_phase: str | None | object = _SCANNER_PHASE_UNSET,
        scanner_idle: bool = False,
        stable_scan_ids: set[str] | frozenset[str] | None = None,
        stable_sweep_ids: set[str] | frozenset[str] | None = None,
        loaded_uid: str = "",
        stable_scan_uid: str = "",
        stable_scan_evidence: SweepEvidence | None = None,
        stable_scan_verified: bool = False,
        stable_files_complete: bool = False,
        observed_scene: str = "",
        stable_upload_timestamp: int = 0,
        stable_data_ack_uid: str = "",
        stable_companion_uploaded_uid: str = "",
        loaded_at: int = 0,
        loaded_boot: str = "",
        loaded_sequence: int = 0,
        observed_modal: str = "",
        observed_character: str = "",
        observed_ah_open: bool | None = None,
        selected_arthur: bool | None = None,
        fence_armed: bool | None = None,
        action_postcondition: bool | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Resolve one persisted external intent from an actual new observation.

        This method is read-only with respect to the game.  Every action has a
        named postcondition; absence of an unrelated UID never resolves a
        save/upload/acknowledgement effect.  A recovered acknowledgement is
        qualified in this method from the complete stable scan object so a
        daemon cannot accidentally move it through ``REOPEN`` without a run
        outcome.
        """

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if not self._restart_uncertain:
                self._reconciliation_required = False
                self._save(last_action="restart_reconcile_noop")
                return self.status()
            if (
                scanner_phase is not _SCANNER_PHASE_UNSET
                and scanner_phase is not None
                and not isinstance(scanner_phase, str)
            ):
                return self.block("malformed scanner phase")
            if not isinstance(observed_scene, str) or not isinstance(observed_modal, str):
                return self.block("malformed restart scene evidence")
            for proof_ids in (uploaded_ids, data_ack_ids, loaded_ids, pending_ids):
                if not isinstance(proof_ids, (set, frozenset)) or any(
                    not isinstance(item, str) or not item for item in proof_ids
                ):
                    return self.block("malformed restart proof UID set")
            for proof_uid in (
                observed_boot,
                observed_uid,
                loaded_uid,
                stable_scan_uid,
                stable_data_ack_uid,
                stable_companion_uploaded_uid,
                loaded_boot,
            ):
                if not isinstance(proof_uid, str):
                    return self.block("malformed restart proof identity")
            for proof_time in (stable_upload_timestamp, loaded_at, loaded_sequence):
                if isinstance(proof_time, bool) or not isinstance(proof_time, int):
                    return self.block("malformed restart proof timestamp")
            uid = self._last_uid
            intent = self._action_intent
            queue_intents = {
                "scan",
                "save_reload",
                "upload_wait",
                "ack_reload",
                "logout",
                "pre_logout_fence",
            }
            if intent in queue_intents and (
                not isinstance(stable_scan_ids, (set, frozenset))
                or not isinstance(stable_sweep_ids, (set, frozenset))
            ):
                return self.block("malformed stable queue evidence")
            stable_scan_ids = (
                stable_scan_ids if isinstance(stable_scan_ids, (set, frozenset)) else frozenset()
            )
            stable_sweep_ids = (
                stable_sweep_ids if isinstance(stable_sweep_ids, (set, frozenset)) else frozenset()
            )
            if any(not isinstance(item, str) or not item for item in (*stable_scan_ids, *stable_sweep_ids)):
                return self.block("malformed stable queue UID")
            phase = scanner_phase if scanner_phase is not _SCANNER_PHASE_UNSET else None
            scene = observed_scene
            stable_scan_ids = frozenset(stable_scan_ids)
            stable_sweep_ids = frozenset(stable_sweep_ids)
            pre_effect_boot = (
                self._restart_reference_boot
                if self._restart_uncertain and intent in {"save_reload", "ack_reload", "enter_world"}
                else self._frame_boot
            )
            if pre_effect_boot:
                boot_changed = bool(observed_boot and observed_boot != pre_effect_boot)
            elif intent == "enter_world":
                # Character select has no Agent boot.  A first real world
                # boot is valid with an empty B0; once recorded, only that
                # same boot may resolve after a restart.
                boot_changed = bool(observed_boot) and (
                    not self._restart_observed_boot or observed_boot == self._restart_observed_boot
                )
            else:
                boot_changed = False
            ready_stable_facts = (
                isinstance(phase, str)
                and phase in {"ready", "scheduled"}
                and scene in {"world", "auction_house", "character_select"}
                and scanner_idle is True
                and stable_files_complete is True
            )
            complete_facts = ready_stable_facts and stable_scan_verified is True and bool(observed_boot)
            scan_completed_uid = (
                stable_scan_evidence.uid if isinstance(stable_scan_evidence, SweepEvidence) else ""
            )
            scan_completed = bool(
                intent == "scan"
                and complete_facts
                and scan_completed_uid
                and scan_completed_uid != uid
                and observed_uid == scan_completed_uid
                and stable_scan_uid == scan_completed_uid
                and scan_completed_uid in stable_scan_ids
                and int(stable_scan_evidence.envelope.get("startedAt", -1)) == self._started_at
            )
            stable_scan_matches = bool(
                uid
                and uid in stable_scan_ids
                and stable_scan_uid == uid
                and isinstance(stable_scan_evidence, SweepEvidence)
                and stable_scan_evidence.uid == uid
            )
            finish_at = int(self._evidence_meta.get("finishedAt", 0) or 0)
            upload_time_valid = bool(
                finish_at > 0
                and isinstance(stable_upload_timestamp, int)
                and not isinstance(stable_upload_timestamp, bool)
                and finish_at <= stable_upload_timestamp <= finish_at + self.upload_deadline_seconds
            )
            companion_uid = stable_companion_uploaded_uid
            proven_upload = bool(
                complete_facts
                and stable_scan_matches
                and uid in uploaded_ids
                and uid in data_ack_ids
                and upload_time_valid
                and stable_data_ack_uid == uid
                and companion_uid == uid
            )
            loaded_at_value = loaded_at
            loaded_boot_value = loaded_boot
            loaded_sequence_value = loaded_sequence
            proven_loaded = bool(
                complete_facts
                and uid
                and loaded_uid == uid
                and uid in loaded_ids
                and uid not in pending_ids
                and loaded_at_value > 0
                and loaded_boot_value
                and loaded_sequence_value > 0
                and loaded_boot_value == observed_boot
                and boot_changed
            )
            proven_saved = bool(
                complete_facts
                and intent == "save_reload"
                and uid
                and observed_uid == uid
                and boot_changed
                and stable_scan_matches
            )
            outcome = "restart_observation_unresolved"

            # Logout is a terminal read-only observation.  It intentionally
            # does not require an Agent boot, scanner phase, or calibration.
            disk_empty = (
                stable_files_complete is True
                and not stable_scan_ids
                and not stable_sweep_ids
                and not pending_ids
            )
            if intent == "logout" and scene == "character_select" and disk_empty:
                self._restart_uncertain = False
                self._reconciliation_required = False
                self._state = ControllerState.STOPPED
                self._stop_requested = True
                self._closeout_requested = False
                self._pending = False
                self._clear_intent("completed_at_character_select")
                outcome = "logout_completed_character_select"
            elif intent == "scan" and scan_completed:
                # A scan completed and was durably saved before the owner
                # stopped.  Continue at the save boundary; no scan is replayed.
                self._last_uid = scan_completed_uid
                self._set_stable_scan_evidence(stable_scan_evidence)
                self._restart_uncertain = False
                self._reconciliation_required = True
                self._resume_state = ControllerState.QUIET_SAVE
                self._pending = True
                self._state = self._calibration_gate_state()
                self._begin_intent("save_reload", deadline_seconds=self.reload_deadline_seconds)
                outcome = "scan_completed_saved"
            elif intent == "save_reload" and proven_saved:
                if isinstance(stable_scan_evidence, SweepEvidence):
                    self._set_stable_scan_evidence(stable_scan_evidence)
                self._restart_uncertain = False
                self._reconciliation_required = True
                self._resume_state = ControllerState.UPLOAD_WAIT
                self._pending = True
                self._state = self._calibration_gate_state()
                self._begin_intent("upload_wait", deadline_seconds=self.upload_deadline_seconds)
                outcome = "saved_uid_stable"
            elif intent == "upload_wait" and proven_upload:
                if isinstance(stable_scan_evidence, SweepEvidence):
                    self._set_stable_scan_evidence(stable_scan_evidence)
                self._evidence_meta["atomicCompanionUploadedUid"] = companion_uid
                self._evidence_meta["companionUploadedUID"] = companion_uid
                self._evidence_meta["dataAckUid"] = stable_data_ack_uid
                self._evidence_meta["dataAckUID"] = stable_data_ack_uid
                self._evidence_meta["uploadTimestamp"] = stable_upload_timestamp
                self._restart_uncertain = False
                self._reconciliation_required = True
                self._resume_state = ControllerState.ACK_RELOAD_SENT
                self._pending = True
                self._state = self._calibration_gate_state()
                self._begin_intent("ack_reload", deadline_seconds=self.ack_deadline_seconds)
                outcome = "upload_observed"
            elif intent == "ack_reload" and proven_loaded and stable_scan_matches and proven_upload:
                # Rebuild the complete proof and qualify it now.  Keeping a
                # temporary QUALIFY_PENDING state makes the public boundary
                # explicit while _qualify_unlocked commits exactly one result.
                prior_witnesses = copy.deepcopy(self._evidence_meta)
                self._set_stable_scan_evidence(stable_scan_evidence)
                for key in (
                    "ackPublicationAt",
                    "ackPublicationBoot",
                    "ackPublicationSequence",
                    "ackPublicationLatencySeconds",
                ):
                    if key in prior_witnesses:
                        self._evidence_meta[key] = prior_witnesses[key]
                publication = int(self._evidence_meta.get("ackPublicationAt", 0) or 0)
                upload = int(stable_upload_timestamp)
                if publication <= 0 or upload > publication or loaded_at_value < publication:
                    return self._reject_unrecoverable_cycle("acknowledgement timing witness is incomplete")
                self._evidence_meta.update(
                    {
                        "atomicCompanionUploadedUid": companion_uid,
                        "companionUploadedUID": companion_uid,
                        "dataAckUid": stable_data_ack_uid,
                        "dataAckUID": stable_data_ack_uid,
                        "uploadTimestamp": upload,
                        "ackLoadedUID": loaded_uid,
                        "loadedUID": loaded_uid,
                        "ackLoadedAt": loaded_at_value,
                        "ackLoadedBoot": loaded_boot_value,
                        "ackLoadedSequence": loaded_sequence_value,
                        "qualifiedEvidenceUID": "",
                    }
                )
                self._last_uid = uid
                self._pending = True
                self._restart_uncertain = False
                self._reconciliation_required = False
                self._clear_intent("reconciled_ack_loaded")
                self._state = ControllerState.QUALIFY_PENDING
                started_at = self._started_at or int(stable_scan_evidence.envelope["startedAt"])
                self._started_at = int(started_at)
                result = self._qualify_unlocked(stable_scan_evidence, started_at=started_at)
                result = self._park_recovered_terminal(result)
                self._archive(
                    "interventions",
                    {
                        "kind": "restart_resolution",
                        "outcome": "acknowledgement_loaded_and_qualified",
                        "generation": self._generation,
                        "intent": intent,
                        "attempt": self._action_attempt,
                        "uid": uid,
                        "loadedAt": loaded_at_value,
                        "loadedBoot": loaded_boot_value,
                        "loadedSequence": loaded_sequence_value,
                    },
                )
                self._save(last_action="restart_resolved:acknowledgement_loaded_and_qualified")
                return result
            elif intent == "pre_logout_fence" and complete_facts and fence_armed is True:
                self._resume_state = ControllerState.DISK_CLEANUP_SENT
                self._pending = False
                self._restart_uncertain = False
                self._reconciliation_required = True
                self._state = self._calibration_gate_state()
                self._clear_intent("fence_observation_expired")
                outcome = "pre_logout_fence_requires_fresh_owner"
            elif intent in {
                "dismiss_disconnect",
                "reconnect_game",
                "select_arthur",
                "enter_world",
                "open_ah",
            }:
                boot_required = intent == "enter_world"
                named_ok = (not boot_required or (bool(observed_boot) and boot_changed)) and (
                    (
                        intent == "dismiss_disconnect"
                        and scene == "game_client"
                        and observed_modal == "none"
                        and action_postcondition is True
                    )
                    or (
                        intent == "reconnect_game"
                        and scene == "character_select"
                        and action_postcondition is True
                    )
                    or (
                        intent == "select_arthur"
                        and action_postcondition is True
                        and (
                            scene == "arthur_selected"
                            or (scene == "character_select" and selected_arthur is True)
                        )
                    )
                    or (intent == "enter_world" and scene == "world" and action_postcondition is True)
                    or (
                        intent == "open_ah"
                        and scene == "auction_house"
                        and observed_ah_open is True
                        and action_postcondition is True
                    )
                )
                # Opening the auction house is idempotent: an observed closed AH
                # (in world or back at character select) proves there is no
                # effect left to account for, and repeating the open cannot
                # duplicate an external market effect.  The normal cycle then
                # recovers the scene and reopens it.
                open_ah_not_effective = (
                    intent == "open_ah"
                    and observed_ah_open is not True
                    and (scene == "character_select" or (scene == "world" and observed_ah_open is False))
                )
                if named_ok:
                    self._restart_uncertain = False
                    self._reconciliation_required = True
                    self._pending = False
                    self._resume_state = (
                        ControllerState.READY if intent == "enter_world" else ControllerState.WAIT_DUE
                    )
                    self._state = self._calibration_gate_state()
                    self._clear_intent("postcondition_observed")
                    outcome = f"{intent}_postcondition_observed"
                elif open_ah_not_effective:
                    self._restart_uncertain = False
                    self._reconciliation_required = True
                    self._pending = False
                    self._resume_state = ControllerState.WAIT_DUE
                    self._state = self._calibration_gate_state()
                    self._clear_intent("open_ah_not_effective")
                    outcome = f"open_ah_not_effective_in_{scene}"
            elif (
                not uid
                and complete_facts
                and scanner_idle is True
                and (
                    (intent == "save_reload" and self._action_result == "preparing")
                    # The cycle UID is persisted only while pending; after a
                    # restart an upload wait without it has no scan to match.
                    or intent == "upload_wait"
                )
            ):
                # A save/reload that never crossed its dispatch boundary and
                # carries no cycle UID has no scan to account for; the stable
                # files already hold whatever the companion uploads.  Clear it
                # so the next cycle starts on cadence instead of waiting
                # forever for proof of a save that was never requested.
                self._restart_uncertain = False
                self._reconciliation_required = True
                self._pending = False
                self._resume_state = ControllerState.WAIT_DUE
                self._state = self._calibration_gate_state()
                self._clear_intent(f"{intent}_without_cycle_uid")
                outcome = f"{intent}_without_cycle_uid_cleared"
            elif intent == "stop" and self._stop_requested:
                self._restart_uncertain = False
                self._reconciliation_required = False
                self._state = ControllerState.STOPPED
                self._clear_intent("stop_observed")
                outcome = "stop_observed"
            elif (
                intent in {"save_reload", "upload_wait", "ack_reload"}
                and stable_files_complete is True
                and not stable_scan_uid
                and not stable_scan_ids
                and not stable_sweep_ids
            ):
                return self._reject_unrecoverable_cycle(
                    f"unrecoverable {intent} cycle: exact UID is absent from stable scan files"
                )
            elif (
                intent == "scan"
                and self._action_result == "dispatched"
                and ready_stable_facts
                and not stable_scan_ids
                and not stable_sweep_ids
            ):
                return self._reject_empty_scan_attempt(
                    "dispatched scan reached ready with stable empty queues"
                )
            elif intent == "scan" and ready_stable_facts and not stable_scan_ids and not stable_sweep_ids:
                # A ready/empty observation before the external scan dispatch
                # is not evidence that the attempted scan produced no data.
                # Keep the intent unresolved while the owner reacquires its
                # live calibration gates.
                self._state = self._calibration_gate_state()
                self._reconciliation_required = True
            else:
                # Keep the original intent and all pending evidence.  The
                # next owner may still prove it; clearing it would authorize a
                # duplicate external effect.
                self._reconciliation_required = True

            self._archive(
                "interventions",
                {
                    "kind": "restart_resolution",
                    "outcome": outcome,
                    "generation": self._generation,
                    "intent": intent,
                    "attempt": self._action_attempt,
                    "uid": uid,
                    "observedBoot": observed_boot,
                    "observedUid": observed_uid,
                    "scannerPhase": phase,
                    "scannerIdle": scanner_idle,
                    "scene": scene,
                    "stableSavedUid": stable_scan_uid,
                    "stableUploadTimestamp": stable_upload_timestamp,
                    "stableDataAckUid": stable_data_ack_uid,
                    "stableCompanionUploadedUid": companion_uid,
                    "stableFormat2Verified": stable_scan_verified,
                    "stableFilesComplete": stable_files_complete,
                    "stableScanIds": sorted(stable_scan_ids),
                    "stableSweepIds": sorted(stable_sweep_ids),
                },
            )
            self._save(last_action=f"restart_resolved:{outcome}")
            return self.status()

    # Descriptive alias used by the daemon worker; both names retain the same
    # atomic admission and no-replay semantics.
    resolve_restart_intent = reconcile_restart

    def _on_guardian_revocation(self, reason: str) -> None:
        """Convert an independent guardian takeover into a durable pause."""
        with self._mutex:
            if (
                self._planned_revoke
                or self._guardian_callback_active
                or self._state
                in {
                    ControllerState.PAUSED,
                    ControllerState.STOPPED,
                    ControllerState.BLOCKED,
                }
            ):
                return
            # The human-readable watchdog reason is audit context, not a type
            # system. An unplanned Guardian revocation always drops the live
            # lease evidence. The durable environment-bound takeover drill is
            # retained and is compared with newly measured startup facts.
            cause = getattr(self.driver, "_active_revocation_cause", InvalidationCause.UNKNOWN)
            if not isinstance(cause, InvalidationCause):
                cause = InvalidationCause.UNKNOWN
            self.invalidate_runtime_readiness(cause)
            self._guardian_callback_active = True
            try:
                self.pause(f"input guardian: {reason}")
            finally:
                self._guardian_callback_active = False

    def stop(self) -> ControllerStatus:
        with self._mutex:
            mail_cycle_was_live = self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
            self._block_mail_cycle_locked("stop requested")
            self._generation += 1
            self._stop_requested = True
            self._planned_revoke = True
            try:
                self._lease_generation = self.driver.guardian.revoke("stop requested")
            except Exception as exc:
                if mail_cycle_was_live:
                    self._record_mail_cycle_control_failure_locked(f"mail cycle stop revoke failed: {exc}")
                raise
            finally:
                self._planned_revoke = False
            prior_intent = self._action_intent
            prior_result = self._action_result
            # The durable intent is written before the physical command.  A
            # stop racing that boundary cannot know whether the worker had
            # already crossed it, so every unresolved result remains an
            # intent-specific reconciliation obligation on the next owner.
            external_effect_pending = bool(
                prior_intent and prior_result not in {"", "complete", "abandoned_after_restart"}
            )
            if external_effect_pending:
                # Stop is orthogonal to an effect that crossed the dispatch
                # boundary.  Preserve its phase and exact intent so the next
                # owner can observe the result without replaying it.
                self._restart_uncertain = True
                self._reconciliation_required = True
            elif self._state in {
                ControllerState.STOPPED,
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
                ControllerState.WAIT_DUE,
                ControllerState.READY,
                ControllerState.QUIET_SAVE,
                ControllerState.REOPEN,
                ControllerState.DISK_CLEANUP_SENT,
                ControllerState.DISK_CLEANUP_WAIT,
            }:
                self._state = ControllerState.STOPPED
            if not prior_intent or prior_result in {"", "complete", "abandoned_after_restart"}:
                self._begin_intent("stop")
            if mail_cycle_was_live:
                self._publish_blocked_mail_cycle_locked("stop requested")
            self._save(last_action="stop")
            self._archive(
                "interventions",
                {"kind": "stop", "generation": self._generation, "state": self._state.value},
            )
            return self.status()

    def closeout(self) -> ControllerStatus:
        with self._mutex:
            if self._mail_cycle["phase"] not in {"none", "complete", "blocked"} or (
                self._mail_cycle_staged_archive is not None
            ):
                raise RuntimeError("closeout is refused while a mail cycle owns interaction")
            self._generation += 1
            self._closeout_requested = True
            if self._state in {ControllerState.WAIT_DUE, ControllerState.READY} or (
                self._state == ControllerState.STOPPED and not self._stop_requested and not self._pending
            ):
                self._state = ControllerState.DISK_CLEANUP_SENT
                self._begin_intent("logout", deadline_seconds=self.ack_deadline_seconds)
            self._archive(
                "interventions",
                {"kind": "closeout_requested", "generation": self._generation, "state": self._state.value},
            )
            self._save(last_action="closeout")
            return self.status()

    def begin_scan(
        self,
        *,
        started_at: int | None = None,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state not in {
                ControllerState.WAIT_DUE,
                ControllerState.READY,
            }:
                raise RuntimeError(f"cannot start scan from {self._state}")
            if self._stop_requested or self._closeout_requested:
                self._state = ControllerState.STOPPED
            else:
                self._state = ControllerState.SCANNING
                self._pending = True
                self._started_at = int(started_at if started_at is not None else self.wall_clock())
                # The daemon persists the scan intent before typing and marks
                # the same effect dispatched before it enters SCANNING.  Keep
                # that attempt and result when this public transition follows
                # the dispatch; creating a second attempt would turn a crash
                # into an unresolved scan and lose the truthful empty-result
                # rejection boundary.
                preserve_scan_intent = self._action_intent == "scan" and self._action_result in {
                    "pending",
                    "preparing",
                    "dispatched",
                    "chat_open",
                }
                if not preserve_scan_intent:
                    self._begin_intent("scan", deadline_seconds=self.sweep_cap_seconds)
            self._save(
                last_action=f"scan_start:{started_at if started_at is not None else self.wall_clock()}"
            )
            return self.status()

    def adopt_scan(
        self,
        *,
        started_at: int,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Adopt a scan that the addon started before a closeout was observed.

        ``closeoutRequested`` prevents the controller from initiating a new
        scan, but it must not make it discard an already-running addon scan.
        This transition records that observational handoff and lets the normal
        save/upload/ack path prove the resulting UID before final logout.
        Clearing ``_last_uid`` is intentional: an automatic restart may reuse
        the previous completed UID until its new SavedVariables envelope
        appears, and that old UID must never satisfy the new scan boundary.
        """

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.DISK_CLEANUP_SENT:
                raise RuntimeError(f"cannot adopt scan from {self._state}")
            if self._stop_requested or not self._closeout_requested:
                raise RuntimeError("scan adoption requires an active closeout without stop")
            if isinstance(started_at, bool) or not isinstance(started_at, int) or started_at <= 0:
                raise ValueError("adopted scan start must be a positive integer")
            self._state = ControllerState.SCANNING
            self._pending = True
            self._last_uid = ""
            self._started_at = started_at
            self._begin_intent("scan", deadline_seconds=self.sweep_cap_seconds)
            self._save(last_action=f"scan_adopt:{started_at}")
            return self.status()

    def saved(
        self,
        uid: str,
        *,
        stable: bool = False,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.SCANNING:
                raise RuntimeError(f"cannot observe saved UID from {self._state}")
            if not uid:
                return self.block("empty saved UID")
            self._last_uid = uid
            self._pending = True
            # A daemon may persist the reload intent before the stable UID
            # observation returns.  That observation belongs to the same
            # effect boundary and must not mint a second attempt or erase a
            # dispatched result/deadline.
            preserve_save_intent = self._action_intent == "save_reload" and (
                self._action_result in {"pending", "preparing", "dispatched", "chat_open"}
            )
            if not preserve_save_intent:
                self._begin_intent("save_reload", deadline_seconds=self.reload_deadline_seconds)
            if self._stop_requested:
                self._state = ControllerState.STOPPED
                self._save(last_action="saved_after_stop")
                return self.status()
            self._state = ControllerState.SAVE_RELOAD_SENT if stable else ControllerState.QUIET_SAVE
            self._save(last_action="saved")
            return self.status()

    def reload_sent(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state not in {ControllerState.QUIET_SAVE, ControllerState.SAVE_RELOAD_SENT}:
                raise RuntimeError(f"cannot send save reload from {self._state}")
            if self._stop_requested:
                self._state = ControllerState.STOPPED
                self._save(last_action="reload_suppressed_after_stop")
                return self.status()
            self._state = ControllerState.SAVE_RELOAD_SENT
            self._dispatch_intent("save_reload", deadline_seconds=self.reload_deadline_seconds)
            self._save(last_action="save_reload_sent")
            return self.status()

    def reload_observed(
        self,
        *,
        acknowledgement: bool = False,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        expected = ControllerState.ACK_RELOAD_SENT if acknowledgement else ControllerState.SAVE_RELOAD_SENT
        target = ControllerState.ACK_RELOAD_WAIT if acknowledgement else ControllerState.SAVE_RELOAD_WAIT
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != expected:
                raise RuntimeError(f"cannot observe reload from {self._state}")
            expected_intent = "ack_reload" if acknowledgement else "save_reload"
            if self._action_intent != expected_intent:
                raise RuntimeError("reload observation does not match the dispatched intent")
            self._reload_boot = self._frame_boot
            self._state = target
            self._expected_boot = ""
            self._save(last_action="reload_observed")
            return self.status()

    def stable_saved(
        self,
        evidence: SweepEvidence,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.SAVE_RELOAD_WAIT:
                raise RuntimeError(f"cannot accept stable SavedVariables from {self._state}")
            if evidence.uid != self._last_uid:
                return self.block("stable SavedVariables UID mismatch")
            if not evidence.stable_verified:
                return self.block("SavedVariables evidence was not stably verified")
            try:
                self._set_stable_scan_evidence(evidence)
            except (DriverError, KeyError, TypeError, ValueError) as exc:
                return self.block(str(exc))
            if self._stop_requested:
                # Stop revokes input and forbids a new upload/ack action.  Keep the
                # exact saved UID, evidence, and save-reload intent durable so an
                # explicit later resume can reconcile it without replaying the
                # reload.  stopRequested is orthogonal to the in-flight effect.
                self._state = ControllerState.STOPPED
                self._save(last_action="stable_saved_after_stop")
                return self.status()
            self._state = ControllerState.UPLOAD_WAIT
            self._begin_intent("upload_wait", deadline_seconds=self.upload_deadline_seconds)
            self._save(last_action="stable_saved")
            return self.status()

    def reject_late_upload(
        self,
        uid: str,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Close a cycle whose companion upload missed its finish deadline.

        The scan is already uploaded and stored, so there is nothing to retry;
        the cycle simply cannot qualify.  Archive it as rejected and continue
        on cadence instead of re-blocking on the same late upload forever.
        """

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.UPLOAD_WAIT:
                raise RuntimeError(f"cannot reject a late upload from {self._state}")
            if uid != self._last_uid:
                return self.block("upload UID mismatch")
            reason = "upload outside finish deadline"
            rejection_state = self._qualification_state_snapshot()
            self._evidence_meta["qualification"] = "rejected"
            self._evidence_meta["rejectionReason"] = reason
            self._pending = False
            self._archive(
                "evidence",
                {
                    "kind": "upload",
                    "uid": uid,
                    "accepted": False,
                    "generation": self._generation,
                    "evidence": copy.deepcopy(self._evidence_meta),
                },
            )
            self._archive_staged_outcome(rejection_state, result="rejected", reason=reason, kind="upload")
            self._clear_intent("late_upload_rejected")
            self._blocker = ""
            self._state = ControllerState.WAIT_DUE
            self._save(last_action="late_upload_rejected")
            return self.status()

    def uploaded(
        self,
        uid: str,
        *,
        accepted: bool,
        upload_time: int = 0,
        data_ack_uid: str = "",
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.UPLOAD_WAIT:
                raise RuntimeError(f"cannot accept upload result from {self._state}")
            if uid != self._last_uid:
                return self.block("upload UID mismatch")
            if not accepted:
                rejection_state = self._qualification_state_snapshot()
                self._evidence_meta["qualification"] = "rejected"
                self._evidence_meta["rejectionReason"] = "upload rejected"
                self._pending = False
                self._archive(
                    "evidence",
                    {
                        "kind": "upload",
                        "uid": uid,
                        "accepted": False,
                        "generation": self._generation,
                        "evidence": copy.deepcopy(self._evidence_meta),
                    },
                )
                self._archive_staged_outcome(
                    rejection_state,
                    result="rejected",
                    reason="upload rejected",
                    kind="upload",
                )
                return self.block("upload rejected")
            finish = int(self._evidence_meta.get("finishedAt", 0))
            if (
                upload_time <= 0
                or data_ack_uid != uid
                or self._evidence_meta.get("stableSavedUid") != uid
                or finish <= 0
                or upload_time < finish
                or upload_time - finish > self.upload_deadline_seconds
            ):
                return self.block("upload lacks stable companion and Data.lua acknowledgement")
            self._evidence_meta["atomicCompanionUploadedUid"] = uid
            self._evidence_meta["companionUploadedUID"] = uid
            self._evidence_meta["uploadTimestamp"] = int(upload_time)
            self._evidence_meta["dataAckUid"] = data_ack_uid
            self._evidence_meta["dataAckUID"] = data_ack_uid
            if self._evidence_meta.get("finishedAt"):
                self._evidence_meta["uploadLatencySeconds"] = max(
                    0, int(upload_time) - int(self._evidence_meta["finishedAt"])
                )
            if self._stop_requested:
                self._state = ControllerState.STOPPED
                self._deadline = 0.0
                self._deadline_at = 0
                self._save(last_action="uploaded_after_stop")
                return self.status()
            self._state = ControllerState.ACK_RELOAD_SENT
            # This is a new effect boundary.  Never carry the dispatched
            # upload result/attempt into the acknowledgement reload intent.
            self._begin_intent("ack_reload", deadline_seconds=self.ack_deadline_seconds)
            self._save(last_action="uploaded")
            return self.status()

    def record_ack_publication(
        self,
        uid: str,
        *,
        companion_uid: str,
        data_uid: str,
        publication_time: int | None = None,
        boot: str = "",
        sequence: int = 0,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Persist the two stable pre-reload acknowledgement witnesses."""

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.ACK_RELOAD_SENT:
                raise RuntimeError(f"cannot record acknowledgement publication from {self._state}")
            if uid != self._last_uid or companion_uid != uid or data_uid != uid:
                return self.block("acknowledgement publication UID mismatch")
            if (
                self._evidence_meta.get("uid") != uid
                or self._evidence_meta.get("stableSavedUid") != uid
                or (self._evidence_meta.get("atomicCompanionUploadedUid") or "") not in {"", uid}
                or self._evidence_meta.get("dataAckUid", "") not in {"", uid}
            ):
                return self.block("acknowledgement publication is not bound to stable evidence")
            publication = int(publication_time or 0)
            upload = int(self._evidence_meta.get("uploadTimestamp", 0))
            if upload <= 0 or publication < upload:
                return self.block("acknowledgement publication is not after the exact upload")
            self._evidence_meta["atomicCompanionUploadedUid"] = companion_uid
            self._evidence_meta["companionUploadedUID"] = companion_uid
            self._evidence_meta["dataAckUid"] = data_uid
            self._evidence_meta["dataAckUID"] = data_uid
            if publication_time is not None:
                self._evidence_meta["ackPublicationAt"] = publication
                if self._evidence_meta.get("uploadTimestamp"):
                    self._evidence_meta["ackPublicationLatencySeconds"] = max(
                        0,
                        int(publication_time) - int(self._evidence_meta["uploadTimestamp"]),
                    )
            if boot:
                self._evidence_meta["ackPublicationBoot"] = boot
            if sequence:
                self._evidence_meta["ackPublicationSequence"] = int(sequence)
            self._save(last_action="ack_publication_observed")
            return self.status()

    def ack_reload_complete(
        self,
        *,
        pending_ids: set[str],
        loaded_uid: str = "",
        loaded_at: int | None = None,
        boot: str = "",
        sequence: int = 0,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.ACK_RELOAD_WAIT:
                raise RuntimeError(f"cannot complete acknowledgement reload from {self._state}")
            if loaded_uid != self._last_uid:
                return self.block("acknowledgement loaded UID is not the exact pending UID")
            if self._last_uid in pending_ids:
                self._save(last_action="ack_reload_waiting_for_disk_cleanup")
                return self.status()
            publication = int(self._evidence_meta.get("ackPublicationAt", 0))
            upload = int(self._evidence_meta.get("uploadTimestamp", 0))
            loaded_at_value = int(loaded_at or 0)
            loaded_boot = str(boot or self._frame_boot)
            loaded_sequence = int(sequence or self._frame_sequence)
            if (
                upload <= 0
                or publication < upload
                or loaded_at is None
                or loaded_at_value <= 0
                or loaded_at_value < publication
                or not loaded_boot
                or loaded_sequence <= 0
                or (self._frame_boot and loaded_boot != self._frame_boot)
                or (self._frame_sequence and loaded_sequence != self._frame_sequence)
            ):
                return self.block("acknowledgement load lacks exact publication witness")
            self._evidence_meta["ackLoadedUID"] = loaded_uid
            self._evidence_meta["loadedUID"] = loaded_uid
            self._evidence_meta["ackLoadedAt"] = loaded_at_value
            self._evidence_meta["ackLoadedBoot"] = loaded_boot
            self._evidence_meta["ackLoadedSequence"] = loaded_sequence
            if self._evidence_meta.get("finishedAt"):
                self._evidence_meta["ackLatencySeconds"] = max(
                    0, loaded_at_value - int(self._evidence_meta["finishedAt"])
                )
            if self._evidence_meta.get("ackPublicationAt"):
                self._evidence_meta["ackLoadedLatencySeconds"] = max(
                    0, loaded_at_value - int(self._evidence_meta["ackPublicationAt"])
                )
            if self._stop_requested:
                self._state = ControllerState.STOPPED
            elif self._closeout_requested:
                self._state = ControllerState.DISK_CLEANUP_SENT
            else:
                self._state = ControllerState.REOPEN
            self._clear_intent("complete")
            self._save(last_action="ack_reload_complete")
            return self.status()

    def reopen_complete(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.REOPEN:
                raise RuntimeError(f"cannot complete reopen from {self._state}")
            self._state = (
                ControllerState.DISK_CLEANUP_SENT if self._closeout_requested else ControllerState.WAIT_DUE
            )
            self._clear_intent("complete")
            self._save(last_action="reopen_complete")
            return self.status()

    def expect_boot(
        self,
        boot: str,
        *,
        acknowledgement: bool = False,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state not in {ControllerState.SAVE_RELOAD_SENT, ControllerState.ACK_RELOAD_SENT}:
                raise RuntimeError(f"cannot wait for reload from {self._state}")
            if not boot:
                raise RuntimeError("reload wait requires a boot identity")
            # This is the durable pre-effect observation.  It is recorded
            # before the first post-reload frame can replace ``frameBoot`` and
            # is retained through pause/restart recovery.
            self._restart_reference_boot = boot
            self._restart_observed_boot = ""
            self._expected_boot = boot
            # A second reload starts a new one-boot window; the previous reload's
            # boot is historical evidence and must not be mistaken for churn.
            self._reload_boot = ""
            expected_intent = "ack_reload" if acknowledgement else "save_reload"
            if self._action_intent != expected_intent:
                raise RuntimeError("reload wait does not match the dispatched intent")
            self._save(last_action="expect_boot")
            return self.status()

    def ack_reload_command_sent(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.ACK_RELOAD_SENT:
                raise RuntimeError(f"cannot mark ack reload command from {self._state}")
            self._dispatch_intent("ack_reload", deadline_seconds=self.ack_deadline_seconds)
            self._save(last_action="ack_reload_command_sent")
            return self.status()

    def qualify(
        self,
        evidence: SweepEvidence,
        *,
        started_at: int,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state not in {
                ControllerState.REOPEN,
                ControllerState.DISK_CLEANUP_SENT,
                ControllerState.QUALIFY_PENDING,
            }:
                raise RuntimeError(f"cannot qualify evidence from {self._state}")
            return self._qualify_unlocked(evidence, started_at=started_at)

    def retry_pending_qualification(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        """Retry staged qualification after a first archive-boundary failure.

        The complete evidence and witness metadata come from the durable
        qualification checkpoint.  A daemon must not retain or resupply an
        in-memory ``SweepEvidence`` object after owner destruction.
        """

        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state not in {
                ControllerState.QUALIFY_PENDING,
                ControllerState.PAUSED,
                ControllerState.BLOCKED,
            }:
                raise RuntimeError(f"cannot retry qualification from {self._state}")
            if self._state in {ControllerState.PAUSED, ControllerState.BLOCKED} and (
                self._resume_state == ControllerState.QUALIFY_PENDING
            ):
                raise RuntimeError("qualification retry requires explicit resume and live input gates")
            pending = self._qualification_pending
            if not isinstance(pending, dict):
                return self._reject_unrecoverable_cycle(
                    "qualification checkpoint is missing complete evidence"
                )
            metadata = pending.get("metadata")
            evidence_record = pending.get("evidence")
            started_at = pending.get("startedAt")
            if (
                not isinstance(metadata, dict)
                or isinstance(started_at, bool)
                or not isinstance(started_at, int)
                or started_at <= 0
                or not isinstance(evidence_record, dict)
            ):
                return self._reject_unrecoverable_cycle("qualification checkpoint metadata is incomplete")
            try:
                evidence = self._evidence_from_record(evidence_record)
            except DriverError as exc:
                return self._reject_unrecoverable_cycle(str(exc))
            if metadata.get("uid") != evidence.uid or metadata.get("stableSavedUid") != evidence.uid:
                return self._reject_unrecoverable_cycle(
                    "qualification checkpoint UID is not bound to stable evidence"
                )
            self._evidence_meta = copy.deepcopy(metadata)
            self._last_uid = evidence.uid
            self._pending = True
            result = self._qualify_unlocked(evidence, started_at=started_at, stage=False)
            return self._park_recovered_terminal(result)

    def _qualify_unlocked(
        self, evidence: SweepEvidence, *, started_at: int, stage: bool = True
    ) -> ControllerStatus:
        if self._evidence_meta.get("outcomeArchived"):
            return self.status()
        prior_state = self._qualification_state_snapshot()
        if stage:
            try:
                self._stage_qualification_pending(evidence, started_at=started_at)
            except JournalError:
                self._restore_qualification_state(prior_state)
                raise
        staged_state = self._qualification_state_snapshot()
        item_valid, observed_kinds = self._validate_item_evidence(evidence)
        exact_stable = (
            self._evidence_meta.get("uid") == evidence.uid == self._last_uid
            and self._evidence_meta.get("stableSavedUid") == evidence.uid
            and self._evidence_meta.get("stablePlanHash") == evidence.plan_hash
            and self._evidence_meta.get("stableHouse") == evidence.house
            and self._evidence_meta.get("fullEvidence", {}).get("uid") == evidence.uid
        )
        valid = (
            evidence.stable_verified
            and exact_stable
            and int(evidence.envelope.get("startedAt", -1)) == int(started_at)
            and evidence.observations == self.plan_count
            and evidence.attempted == self.plan_count
            and evidence.full + evidence.bounded == self.plan_count
            and evidence.incomplete == 0
            and evidence.unqueried == 0
            and evidence.priced > 0
            and item_valid
            and {"commodity", "recipe", "container", "equipment"}.issubset(observed_kinds)
            and int(self._evidence_meta.get("uploadTimestamp", 0)) > 0
            and int(self._evidence_meta.get("ackPublicationAt", 0))
            >= int(self._evidence_meta.get("uploadTimestamp", 0))
            and int(self._evidence_meta.get("ackLoadedAt", 0))
            >= int(self._evidence_meta.get("ackPublicationAt", 0))
            and self._evidence_meta.get("atomicCompanionUploadedUid") == evidence.uid
            and self._evidence_meta.get("dataAckUid") == evidence.uid
            and self._evidence_meta.get("ackLoadedUID") == evidence.uid
            and bool(self._evidence_meta.get("ackLoadedBoot", ""))
            and int(self._evidence_meta.get("ackLoadedSequence", 0)) > 0
            and (
                not evidence.query_durations
                or sorted(evidence.query_durations)[len(evidence.query_durations) // 2]
                <= BASELINE_MEDIAN_SECONDS
            )
        )
        if not valid:
            prior_streak = self._streak
            prior_seed = self._cadence_seed_start
            prior_evidence = copy.deepcopy(self._evidence_meta)
            # A rejected candidate must retain the exact witnesses that were
            # already proven for the in-flight outcome.  Keep them at the
            # top level for the bounded checkpoint and under priorEvidence
            # for audit consumers; replacing the evidence with the candidate
            # must never erase the upload/publication/load timing chain.
            preserved_witnesses = {
                key: copy.deepcopy(prior_evidence[key])
                for key in (
                    "stableSavedUid",
                    "stableHouse",
                    "stablePlanHash",
                    "stableContext",
                    "atomicCompanionUploadedUid",
                    "uploadTimestamp",
                    "uploadLatencySeconds",
                    "dataAckUid",
                    "ackPublicationAt",
                    "ackPublicationLatencySeconds",
                    "ackPublicationBoot",
                    "ackPublicationSequence",
                    "ackLoadedAt",
                    "ackLatencySeconds",
                    "ackLoadedLatencySeconds",
                    "ackLoadedBoot",
                    "ackLoadedSequence",
                )
                if key in prior_evidence
            }
            self._evidence_meta = {
                "uid": evidence.uid,
                "startedAt": int(evidence.envelope.get("startedAt", 0)),
                "finishedAt": int(evidence.envelope.get("finishedAt", 0)),
                "planHash": evidence.plan_hash,
                "observations": evidence.observations,
                "attempted": evidence.attempted,
                "full": evidence.full,
                "bounded": evidence.bounded,
                "incomplete": evidence.incomplete,
                "unqueried": evidence.unqueried,
                "priced": evidence.priced,
                "rowCount": evidence.row_count,
                "kinds": list(evidence.kinds),
                "queryTimes": copy.deepcopy(evidence.query_times),
                "itemEvidence": copy.deepcopy(evidence.item_evidence),
                "ownedRows": copy.deepcopy(evidence.owned_rows),
                "ownedPricedRowCount": evidence.owned_priced_row_count,
                "fullEvidence": self._full_evidence_record(evidence),
                "qualification": "rejected",
                "rejectionReason": "qualifying sweep evidence failed",
                "acceptedBefore": prior_streak,
                "priorCadenceSeedStart": prior_seed,
                "priorEvidence": prior_evidence,
                **preserved_witnesses,
            }
            self._pending = False
            self._blocker = "qualifying sweep evidence failed"
            self._last_uid = evidence.uid
            self._state = (
                ControllerState.STOPPED
                if self._stop_requested
                else ControllerState.DISK_CLEANUP_SENT
                if self._closeout_requested
                else ControllerState.REOPEN
            )
            if self._state == ControllerState.REOPEN:
                self._deadline = self.clock() + self.reload_deadline_seconds
                self._deadline_at = self.wall_clock() + self.reload_deadline_seconds
            self._streak = 0
            self._cadence_seed_start = None
            self._archive_staged_outcome(
                staged_state,
                result="rejected",
                reason=self._blocker,
                kind="qualification",
            )
            self._save(last_action="qualification_reset")
            return self.status()
        if self._streak and self._cadence_seed_start is not None:
            expected = self._cadence_seed_start + self._streak * self.cadence_seconds
            if abs(started_at - expected) > 5:
                accepted_before = self._streak
                prior_seed = self._cadence_seed_start
                prior_evidence = copy.deepcopy(self._evidence_meta)
                self._blocker = "cadence interval failed"
                self._evidence_meta["qualification"] = "rejected"
                self._evidence_meta["rejectionReason"] = self._blocker
                self._pending = False
                self._state = (
                    ControllerState.STOPPED
                    if self._stop_requested
                    else ControllerState.DISK_CLEANUP_SENT
                    if self._closeout_requested
                    else ControllerState.REOPEN
                )
                if self._state == ControllerState.REOPEN:
                    self._deadline = self.clock() + self.reload_deadline_seconds
                    self._deadline_at = self.wall_clock() + self.reload_deadline_seconds
                self._evidence_meta["cadenceExpectedStartedAt"] = int(expected)
                self._evidence_meta["cadenceDeltaSeconds"] = int(started_at - expected)
                self._evidence_meta["acceptedBefore"] = accepted_before
                self._evidence_meta["priorCadenceSeedStart"] = prior_seed
                self._evidence_meta["priorEvidence"] = prior_evidence
                self._streak = 0
                self._cadence_seed_start = None
                self._archive_staged_outcome(
                    staged_state,
                    result="rejected",
                    reason=self._blocker,
                    kind="cadence",
                )
                self._save(last_action="cadence_reset")
                return self.status()
        if self._streak == 0:
            self._cadence_seed_start = int(started_at)
        self._accepted.append(
            {
                "uid": evidence.uid,
                "startedAt": started_at,
                "finishedAt": int(evidence.envelope.get("finishedAt", 0)),
                "observations": evidence.observations,
                "attempted": evidence.attempted,
                "full": evidence.full,
                "bounded": evidence.bounded,
                "incomplete": evidence.incomplete,
                "unqueried": evidence.unqueried,
                "priced": evidence.priced,
                "rows": evidence.row_count,
                "kinds": list(evidence.kinds),
                "rowKinds": copy.deepcopy(evidence.row_kinds),
                "queryTimes": copy.deepcopy(evidence.query_times),
                "itemEvidence": copy.deepcopy(evidence.item_evidence),
                "ownedRows": copy.deepcopy(evidence.owned_rows),
                "ownedPricedRowCount": evidence.owned_priced_row_count,
                "fullEvidence": self._full_evidence_record(evidence),
                "evidence": copy.deepcopy(self._evidence_meta),
            }
        )
        self._total_accepted += 1
        del self._accepted[:-20]
        self._streak += 1
        self._last_uid = evidence.uid
        self._pending = False
        self._blocker = ""
        self._evidence_meta["qualification"] = "accepted"
        self._evidence_meta["qualifiedEvidenceUID"] = evidence.uid
        self._evidence_meta["evidenceUID"] = evidence.uid
        if self._accepted:
            self._accepted[-1]["evidence"] = copy.deepcopy(self._evidence_meta)
        self._evidence_meta["acceptedBefore"] = self._streak - 1
        self._deadline = 0.0
        self._deadline_at = 0
        if self._stop_requested:
            self._state = ControllerState.STOPPED
        elif (self._mode == SessionMode.VALIDATION and self._streak >= 3) or self._closeout_requested:
            self._closeout_requested = True
            self._state = ControllerState.DISK_CLEANUP_SENT
        else:
            self._state = ControllerState.REOPEN
            self._deadline = self.clock() + self.reload_deadline_seconds
            self._deadline_at = self.wall_clock() + self.reload_deadline_seconds
        self._archive_staged_outcome(
            staged_state,
            result="accepted",
            reason="qualified",
            kind="accepted",
        )
        self._save(last_action="qualify")
        return self.status()

    def _validate_item_evidence(self, evidence: SweepEvidence) -> tuple[bool, set[str]]:
        """Check the complete target-plan accounting proof at qualification."""

        entries = evidence.item_evidence
        if not isinstance(entries, dict) or len(entries) != self.plan_count:
            return False, set()
        categories = {"commodity", "recipe", "container", "equipment"}
        observed: set[str] = set()
        rows_total = priced_total = owned_total = owned_priced_total = 0
        for item_id, item in entries.items():
            if not isinstance(item_id, str) or not isinstance(item, dict):
                return False, observed
            category = item.get("category")
            if category not in categories or item.get("coverage") not in {"full", "bounded"}:
                return False, observed
            observed.add(category)
            row_count = item.get("rowCount")
            priced_count = item.get("pricedRowCount")
            owned_rows = item.get("ownedRows")
            owned_priced = item.get("ownedPricedRowCount")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (row_count, priced_count, owned_priced)
            ) or not isinstance(owned_rows, list):
                return False, observed
            if priced_count > row_count or owned_priced > len(owned_rows):
                return False, observed
            valid_owned_priced = 0
            for row in owned_rows:
                if not isinstance(row, dict) or str(row.get("itemID", item_id)) != item_id:
                    return False, observed
                buyout = row.get("buyout")
                if isinstance(buyout, dict) and buyout.get("kind") == "known":
                    copper = buyout.get("copper")
                    if isinstance(copper, int) and not isinstance(copper, bool) and copper > 0:
                        valid_owned_priced += 1
            if valid_owned_priced != owned_priced:
                return False, observed
            rows_total += row_count
            priced_total += priced_count
            owned_total += len(owned_rows)
            owned_priced_total += owned_priced
        # An account may have no auctions listed; the owned query must have
        # completed, but owning a priced listing is not a sweep requirement.
        if not evidence.owned_query_complete:
            return False, observed
        if owned_total != len(evidence.owned_rows) or owned_priced_total != evidence.owned_priced_row_count:
            return False, observed
        if rows_total != evidence.row_count or priced_total != evidence.priced:
            return False, observed
        return True, observed

    def cleanup_complete(
        self,
        *,
        logout_observed: bool,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.DISK_CLEANUP_WAIT:
                raise RuntimeError(f"cannot complete cleanup from {self._state}")
            if not logout_observed:
                return self.block("closeout cleanup did not observe /logout")
            self._state = ControllerState.STOPPED
            self._pending = False
            self._closeout_requested = False
            self._stop_requested = True
            self._clear_intent("complete")
            self._archive(
                "interventions",
                {"kind": "closeout_complete", "generation": self._generation, "uid": self._last_uid},
            )
            self._save(last_action="cleanup_complete")
            return self.status()

    def logout_sent(
        self,
        *,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if self._state != ControllerState.DISK_CLEANUP_SENT:
                raise RuntimeError(f"cannot send logout from {self._state}")
            self._state = ControllerState.DISK_CLEANUP_WAIT
            self._dispatch_intent("logout", deadline_seconds=self.ack_deadline_seconds)
            self._save(last_action="logout_sent")
            return self.status()

    def block(
        self,
        reason: str,
        *,
        invalidation_cause: InvalidationCause = InvalidationCause.UNKNOWN,
        expected_generation: int | None = None,
        expected_state: ControllerState | str | set[ControllerState | str] | None = None,
        fence: ObservationFence | None = None,
    ) -> ControllerStatus:
        with self._mutex:
            if not self._admitted(
                expected_generation=expected_generation, expected_state=expected_state, fence=fence
            ):
                return self.status()
            if not isinstance(invalidation_cause, InvalidationCause):
                invalidation_cause = InvalidationCause.UNKNOWN
            mail_cycle_was_live = self._mail_cycle["phase"] not in {"none", "complete", "blocked"}
            self._block_mail_cycle_locked(reason)
            if isinstance(self._qualification_pending, dict) and not self._evidence_meta.get(
                "outcomeArchived"
            ):
                # An outcome publication failure leaves a complete retry proof
                # on disk.  A normal daemon error handler must not replace it
                # with mutated counters or a terminal REOPEN checkpoint.
                self.invalidate_runtime_readiness(invalidation_cause)
                self._planned_revoke = True
                try:
                    self._lease_generation = self.driver.revoke(reason)
                except Exception as exc:
                    if mail_cycle_was_live:
                        self._record_mail_cycle_control_failure_locked(
                            f"mail cycle qualification block revoke failed: {exc}"
                        )
                    raise
                finally:
                    self._planned_revoke = False
                self._resume_state = ControllerState.QUALIFY_PENDING
                self._state = ControllerState.QUALIFY_PENDING
                self._pending = True
                self._restart_uncertain = False
                self._reconciliation_required = False
                self._deadline = 0.0
                self._deadline_at = 0
                self._blocker = reason
                if mail_cycle_was_live:
                    self._publish_blocked_mail_cycle_locked(reason)
                self._archive(
                    "interventions",
                    {
                        "kind": "blocked_with_qualification_pending",
                        "reason": reason,
                        "generation": self._generation,
                        "state": ControllerState.QUALIFY_PENDING.value,
                    },
                )
                self._save(last_action="blocked_with_qualification_pending")
                return self.status()
            prior_state = self._state
            rejection_state = self._qualification_state_snapshot()
            if (
                self._evidence_meta.get("uid")
                and self._evidence_meta.get("qualification", "pending") != "accepted"
                and prior_state
                in {
                    ControllerState.SAVE_RELOAD_WAIT,
                    ControllerState.UPLOAD_WAIT,
                    ControllerState.ACK_RELOAD_SENT,
                    ControllerState.ACK_RELOAD_WAIT,
                    ControllerState.REOPEN,
                }
            ):
                self._evidence_meta["qualification"] = "rejected"
                self._evidence_meta["rejectionReason"] = reason
                self._pending = False
                self._archive_staged_outcome(
                    rejection_state,
                    result="rejected",
                    reason=reason,
                    kind="blocked",
                )
            self._resume_state = self._state
            self.invalidate_runtime_readiness(invalidation_cause)
            self._planned_revoke = True
            try:
                self.driver.revoke(reason)
            except Exception as exc:
                if mail_cycle_was_live:
                    self._record_mail_cycle_control_failure_locked(f"mail cycle block revoke failed: {exc}")
                raise
            finally:
                self._planned_revoke = False
            self._lease_generation = self.driver.guardian.generation
            self._deadline = 0.0
            self._deadline_at = 0
            self._blocker = reason
            self._reconciliation_required = True
            reload_dispatched = self._action_intent in {
                "save_reload",
                "ack_reload",
                "enter_world",
            } and self._action_result not in {"", "complete", "abandoned_after_restart"}
            if reload_dispatched:
                # A dispatched reload or world entry may have changed the
                # client boot after this block.  Resume must reconcile it the
                # same way a process restart does (restart-boot transition
                # admitted against the recorded reference boot), never as an
                # unexplained boot change.
                self._restart_uncertain = True
            self._state = ControllerState.BLOCKED
            self._streak = 0
            self._cadence_seed_start = None
            if mail_cycle_was_live:
                self._publish_blocked_mail_cycle_locked(reason)
            self._archive(
                "interventions",
                {
                    "kind": "blocked",
                    "reason": reason,
                    "generation": self._generation,
                    "state": self._resume_state.value,
                },
            )
            self._save(last_action="blocked")
            return self.status()


__all__ = [
    "ActionIntent",
    "ActionKind",
    "Controller",
    "ControllerState",
    "ControllerStatus",
    "ObservationFence",
    "SessionMode",
]
