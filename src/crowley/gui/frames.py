"""Lossless framed status protocol used by the Crowley addon agent, and the canvas pixel decoder.

The game surface is deliberately small and strict.  This module owns the byte-level
contract; controller code consumes only :class:`Snapshot` instances returned by
``decode_frame``.
"""

from __future__ import annotations

import binascii
import json
import math
import re
import struct
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

MAGIC = b"CRS1"
VERSION = 1
ENCODING = 1
WIDTH = HEIGHT = 128
HEADER_SIZE = 26
CAPACITY = 2048
MAX_PAYLOAD = 2000
MAX_INFLATED = 16_384
CELL_PHYSICAL = 2
MAX_U32 = 0xFFFFFFFF
HALF_U32 = 0x80000000
POSITION_LIMIT = 1_000_000.0
FACING_LIMIT = 2.0 * math.pi
SPEED_LIMIT = 1_000.0


class ProtocolError(ValueError):
    """A frame or snapshot is not admissible."""


@dataclass(frozen=True, slots=True)
class ProtocolContract:
    """Resolved target plan facts required to validate one CRS publication."""

    plan_count: int
    cadence_seconds: int
    sweep_cap_seconds: int
    initial_price_levels: int
    plan_item_ids: tuple[int, ...]

    @classmethod
    def from_plan(cls, plan: Mapping[str, object], *, sweep_cap_seconds: int = 600) -> ProtocolContract:
        item_ids = plan.get("itemIDs")
        cadence = plan.get("cadenceSeconds")
        levels = plan.get("initialPriceLevels")
        build = plan.get("targetClientBuild")
        if (
            not isinstance(item_ids, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in item_ids)
            or item_ids != sorted(set(item_ids))
            or not isinstance(cadence, int)
            or isinstance(cadence, bool)
            or not isinstance(levels, int)
            or isinstance(levels, bool)
            or not isinstance(build, str)
        ):
            raise ProtocolError("canonical plan is malformed")
        if not item_ids:
            raise ProtocolError("canonical plan must contain item IDs")
        if (
            isinstance(sweep_cap_seconds, bool)
            or not isinstance(sweep_cap_seconds, int)
            or sweep_cap_seconds <= 0
        ):
            raise ProtocolError("canonical plan sweep cap is malformed")
        return cls(len(item_ids), cadence, sweep_cap_seconds, levels, tuple(item_ids))


DEFAULT_PROTOCOL_CONTRACT = ProtocolContract(481, 900, 600, 20, tuple())
PLAN_COUNT = DEFAULT_PROTOCOL_CONTRACT.plan_count
PLAN_CADENCE = DEFAULT_PROTOCOL_CONTRACT.cadence_seconds
PLAN_CAP = DEFAULT_PROTOCOL_CONTRACT.sweep_cap_seconds
PLAN_LEVELS = DEFAULT_PROTOCOL_CONTRACT.initial_price_levels


@dataclass(frozen=True)
class Snapshot:
    value: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.value[key]


@dataclass(frozen=True)
class Frame:
    snapshot: Snapshot
    boot: str
    sequence: int
    wall_time: int
    uptime_ms: int
    crc32: int
    payload_length: int
    # Exact validated 2048-byte CRS bytes.  This is private provenance only;
    # it is never serialized into the wire snapshot or exposed as telemetry.
    raw_bytes: bytes = b""


def _error(path: str, message: str) -> ProtocolError:
    return ProtocolError(f"{path}: {message}")


def _integer(value: Any, path: str, *, minimum: int = 0, maximum: int = MAX_U32) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _error(path, f"expected integer in [{minimum}, {maximum}]")
    return value


def _string(value: Any, path: str, *, empty: bool = False, limit: int = 256) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise _error(path, "expected string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _error(path, "value is not valid UTF-8") from exc
    if len(encoded) > limit:
        raise _error(path, "UTF-8 value is too long")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "expected boolean")
    return value


def _number(value: Any, path: str, *, minimum: float = 0.0, maximum: float = 1_000_000.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, "expected finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise _error(path, "expected finite number") from exc
    if result != result or result in (float("inf"), float("-inf")) or not minimum <= result <= maximum:
        raise _error(path, "number is outside the allowed range")
    return result


def _finite_number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, "expected finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise _error(path, "expected finite number") from exc
    if (
        result != result
        or result in (float("inf"), float("-inf"))
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        raise _error(path, "expected finite number in the allowed range")
    return result


def _binding(value: Any, path: str) -> str:
    binding = _string(value, path, limit=32)
    if not re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", binding):
        raise _error(path, "must use the observed native binding syntax")
    return binding


_NATIVE_FAILURES = {"call_error", "missing", "wrong_type", "nil_result"}
_GROUND_VALUES = {"false", "true", "unknown"}


def _validate_native(value: Any, *, speed: Any) -> None:
    native = _keys(value, {"ctm", "ground", "speedTuple"}, "motion.native")
    ctm = _keys(native["ctm"], {"accessor", "cvar", "state", "reason"}, "motion.native.ctm")
    if ctm["accessor"] != "C_CVar.GetCVarBool":
        raise _error("motion.native.ctm.accessor", "must be C_CVar.GetCVarBool")
    if ctm["cvar"] != "autointeract":
        raise _error("motion.native.ctm.cvar", "must be autointeract")
    ctm_state = ctm["state"]
    if not isinstance(ctm_state, str) or ctm_state not in {"disabled", "enabled", "unknown"}:
        raise _error("motion.native.ctm.state", "invalid state")
    ctm_reason = ctm["reason"]
    if not isinstance(ctm_reason, str) or ctm_reason not in {"", *_NATIVE_FAILURES}:
        raise _error("motion.native.ctm.reason", "invalid failure code")
    if ctm_state == "unknown" and not ctm_reason:
        raise _error("motion.native.ctm", "unknown state requires a failure code")
    if ctm_state != "unknown" and ctm_reason:
        raise _error("motion.native.ctm", "known state requires an empty reason")

    ground = _keys(
        native["ground"],
        {"state", "falling", "flying", "swimming", "submerged", "mounted", "taxi", "reason"},
        "motion.native.ground",
    )
    ground_state = ground["state"]
    if not isinstance(ground_state, str) or ground_state not in {"grounded", "not_grounded", "unknown"}:
        raise _error("motion.native.ground.state", "invalid state")
    unknown_fields = []
    any_true = False
    for name in ("falling", "flying", "swimming", "submerged", "mounted", "taxi"):
        predicate = ground[name]
        if not isinstance(predicate, str) or predicate not in _GROUND_VALUES:
            raise _error(f"motion.native.ground.{name}", "invalid predicate state")
        unknown_fields.append(name) if predicate == "unknown" else None
        any_true = any_true or predicate == "true"
    ground_reason = ground["reason"]
    if not isinstance(ground_reason, str) or ground_reason not in {"", *_NATIVE_FAILURES}:
        raise _error("motion.native.ground.reason", "invalid failure code")
    if unknown_fields:
        if ground_state != "unknown" or not ground_reason:
            raise _error(
                "motion.native.ground", "unknown predicate requires unknown state and a failure code"
            )
    else:
        if ground_reason:
            raise _error("motion.native.ground", "known predicates require an empty reason")
        expected_state = "not_grounded" if any_true else "grounded"
        if ground_state != expected_state:
            raise _error("motion.native.ground", "state contradicts predicate values")

    speed_tuple = _keys(
        native["speedTuple"],
        {"current", "run", "flight", "swim", "unitStatus"},
        "motion.native.speedTuple",
    )
    for name in ("current", "run", "flight", "swim"):
        _finite_number(
            speed_tuple[name],
            f"motion.native.speedTuple.{name}",
            minimum=0.0,
            maximum=SPEED_LIMIT,
        )
    if speed_tuple["unitStatus"] != "unverified":
        raise _error("motion.native.speedTuple.unitStatus", "must be unverified")
    if speed_tuple["current"] != speed:
        raise _error("motion.native.speedTuple.current", "must equal motion.speed")


def _validate_motion(value: Any) -> None:
    motion = _keys(
        value,
        {"position", "facing", "speed", "combat", "deadOrGhost", "bindings", "native"},
        "motion",
    )
    position = _keys(motion["position"], {"x", "y", "z", "instance"}, "motion.position")
    for axis in ("x", "y", "z"):
        _finite_number(
            position[axis],
            f"motion.position.{axis}",
            minimum=-POSITION_LIMIT,
            maximum=POSITION_LIMIT,
        )
    _integer(position["instance"], "motion.position.instance", maximum=MAX_U32)
    _finite_number(motion["facing"], "motion.facing", minimum=0.0, maximum=FACING_LIMIT)
    _finite_number(motion["speed"], "motion.speed", minimum=0.0, maximum=SPEED_LIMIT)
    _boolean(motion["combat"], "motion.combat")
    _boolean(motion["deadOrGhost"], "motion.deadOrGhost")
    bindings = _keys(
        motion["bindings"],
        {"moveForward", "moveBackward", "strafeLeft", "strafeRight", "turnLeft", "jump", "turnRight"},
        "motion.bindings",
    )
    for name in ("moveForward", "moveBackward", "strafeLeft", "strafeRight", "turnLeft", "jump", "turnRight"):
        _binding(bindings[name], f"motion.bindings.{name}")
    _validate_native(motion["native"], speed=motion["speed"])


def _keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "expected object")
    # JSON objects normally have string keys, but callers can hand this
    # validator a decoded Python object directly.  Do the type check before
    # producing diagnostics so malformed values always use ProtocolError
    # instead of leaking a heterogeneous-key TypeError from sorted().
    if any(not isinstance(key, str) for key in value):
        raise _error(path, "object keys must be strings")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual, key=lambda item: item.encode("utf-8"))
        unknown = sorted(actual - expected, key=lambda item: item.encode("utf-8"))
        raise _error(path, f"keys differ; missing={missing}, unknown={unknown}")
    return value


def _uid(value: Any, path: str, *, empty: bool = False) -> str:
    return _string(value, path, empty=empty, limit=192)


_EXECUTOR_KINDS = {
    "buy_commodity",
    "post_commodity",
    "cancel_auction",
    "vendor_sell",
    "vendor_buy",
    "craft",
    "mail_collect",
}
_EXECUTOR_PHASES = {
    "none",
    "ready",
    "complete",
    "await_price",
    "ready_to_confirm",
    "must_cancel",
    "await_purchase",
    "sell_stacks",
    "sell_wait",
    "await_vendor_buy",
    "await_craft",
    "mail_items",
    "mail_wait",
}
_EXECUTOR_RESULTS = {"none", "dry_run", "started", "blocked", "succeeded", "failed", "refused", "skipped"}
_EXECUTOR_SOURCES = {"none", "button", "directAPI"}
_EXECUTOR_CLICK_PHASES = {"ready", "ready_to_confirm", "must_cancel", "sell_stacks", "mail_items"}


def _validate_executor(value: Any) -> None:
    executor = _keys(
        value,
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
        },
        "executor",
    )
    availability = executor["availability"]
    if not isinstance(availability, str) or availability not in {"none", "invalid", "valid"}:
        raise _error("executor.availability", "must be none, invalid, or valid")
    queue_id = _uid(executor["queueId"], "executor.queueId", empty=True)
    _string(executor["house"], "executor.house", empty=True)
    _string(executor["character"], "executor.character", empty=True)
    _boolean(executor["publishedArmed"], "executor.publishedArmed")
    _boolean(executor["sessionArmed"], "executor.sessionArmed")
    button_visible = _boolean(executor["buttonVisible"], "executor.buttonVisible")
    button_name = _string(executor["buttonName"], "executor.buttonName", empty=True, limit=64)
    current_seq = _integer(executor["currentSeq"], "executor.currentSeq")
    current_kind = executor["currentKind"]
    if not isinstance(current_kind, str) or current_kind not in {"", *_EXECUTOR_KINDS}:
        raise _error("executor.currentKind", "invalid action kind")
    phase = executor["phase"]
    if not isinstance(phase, str) or phase not in _EXECUTOR_PHASES:
        raise _error("executor.phase", "invalid executor phase")
    for field in ("inFlight", "clickReady", "blocked", "latched", "ambiguous"):
        _boolean(executor[field], f"executor.{field}")
    work_done = _integer(executor["workDone"], "executor.workDone")
    work_total = _integer(executor["workTotal"], "executor.workTotal")
    click_ordinal = _integer(executor["clickOrdinal"], "executor.clickOrdinal")
    source = executor["lastClickSource"]
    if not isinstance(source, str) or source not in _EXECUTOR_SOURCES:
        raise _error("executor.lastClickSource", "invalid click source")
    result_count = _integer(executor["resultCount"], "executor.resultCount")
    last_result = _keys(executor["lastResult"], {"seq", "kind", "status"}, "executor.lastResult")
    result_seq = _integer(last_result["seq"], "executor.lastResult.seq")
    result_kind = last_result["kind"]
    if not isinstance(result_kind, str) or result_kind not in {"", "queue", *_EXECUTOR_KINDS}:
        raise _error("executor.lastResult.kind", "invalid result kind")
    result_status = last_result["status"]
    if not isinstance(result_status, str) or result_status not in _EXECUTOR_RESULTS:
        raise _error("executor.lastResult.status", "invalid result status")

    if button_name not in {"", "CrowleyTradeExecutorButton"}:
        raise _error("executor.buttonName", "must identify the Crowley executor button")
    if button_visible and button_name != "CrowleyTradeExecutorButton":
        raise _error("executor.buttonName", "visible executor button requires its native name")
    if source == "button" and click_ordinal == 0:
        raise _error("executor.lastClickSource", "button source requires a positive ordinal")
    if source == "none" and click_ordinal != 0:
        raise _error("executor.lastClickSource", "none source requires ordinal zero")
    if work_done > work_total:
        raise _error("executor.workDone", "cannot exceed workTotal")
    if executor["ambiguous"] and not executor["blocked"]:
        raise _error("executor.ambiguous", "ambiguous executor history must be blocked")

    if availability == "none":
        expected = {
            "queueId": "",
            "house": "",
            "character": "",
            "publishedArmed": False,
            "sessionArmed": False,
            "buttonVisible": False,
            "buttonName": "",
            "currentSeq": 0,
            "currentKind": "",
            "phase": "none",
            "inFlight": False,
            "clickReady": False,
            "workDone": 0,
            "workTotal": 0,
            "blocked": False,
            "latched": False,
            "ambiguous": False,
            "clickOrdinal": 0,
            "lastClickSource": "none",
            "resultCount": 0,
            "lastResult": {"seq": 0, "kind": "", "status": "none"},
        }
        scalar_match = all(executor[key] == expected[key] for key in expected if key != "lastResult")
        if not scalar_match or last_result != expected["lastResult"]:
            raise _error("executor", "none availability has non-empty state")
        return

    if availability == "invalid":
        if any(
            (executor[key] != expected)
            for key, expected in {
                "queueId": "",
                "house": "",
                "character": "",
                "publishedArmed": False,
                "sessionArmed": False,
                "buttonVisible": False,
                "buttonName": "",
                "currentSeq": 0,
                "currentKind": "",
                "phase": "none",
                "inFlight": False,
                "clickReady": False,
                "workDone": 0,
                "workTotal": 0,
                "blocked": False,
                "latched": False,
                "ambiguous": False,
                "clickOrdinal": 0,
                "lastClickSource": "none",
            }.items()
        ):
            raise _error("executor", "invalid availability has non-empty queue state")
        if result_count < 1 or last_result != {"seq": 0, "kind": "queue", "status": "refused"}:
            raise _error("executor", "invalid availability requires a queue refusal witness")
        return

    if not queue_id or not executor["house"] or not executor["character"]:
        raise _error("executor", "valid availability requires queue identity")
    if current_seq == 0:
        if current_kind != "" or phase != "complete" or executor["inFlight"] or executor["clickReady"]:
            raise _error("executor", "completed queue has an active action")
    else:
        if current_kind not in _EXECUTOR_KINDS or phase in {"none", "complete"}:
            raise _error("executor", "active queue action has invalid kind or phase")
        phase_kind = {
            "await_price": "buy_commodity",
            "ready_to_confirm": "buy_commodity",
            "must_cancel": "buy_commodity",
            "await_purchase": "buy_commodity",
            "sell_stacks": "vendor_sell",
            "sell_wait": "vendor_sell",
            "await_vendor_buy": "vendor_buy",
            "await_craft": "craft",
            "mail_items": "mail_collect",
            "mail_wait": "mail_collect",
        }.get(phase)
        if phase_kind is not None and current_kind != phase_kind:
            raise _error("executor", "phase is incompatible with current action kind")
    if phase == "ready" and executor["inFlight"]:
        if not (
            current_kind in {"post_commodity", "cancel_auction"}
            and not executor["clickReady"]
            and not executor["blocked"]
            and not executor["ambiguous"]
            and result_count >= 1
            and last_result == {"seq": current_seq, "kind": current_kind, "status": "started"}
            and not executor["latched"]
        ) and not (
            current_kind in {"post_commodity", "cancel_auction"}
            and not executor["clickReady"]
            and not executor["blocked"]
            and not executor["ambiguous"]
            and result_count >= 1
            and last_result == {"seq": current_seq, "kind": current_kind, "status": "blocked"}
            and executor["latched"]
        ):
            raise _error("executor.phase", "ready action cannot be in flight")
    if phase == "complete" and (executor["inFlight"] or executor["blocked"] or executor["latched"]):
        raise _error("executor.phase", "complete action cannot retain active flags")
    if executor["clickReady"]:
        if not button_visible or current_seq == 0 or phase not in _EXECUTOR_CLICK_PHASES:
            raise _error("executor.clickReady", "ready click requires a visible actionable button")
        if phase == "ready" and executor["inFlight"]:
            raise _error("executor.clickReady", "ready click cannot be in flight")
        if executor["blocked"] or executor["ambiguous"]:
            raise _error("executor.clickReady", "blocked or ambiguous action cannot be click-ready")
        if executor["latched"] and not (
            current_kind == "buy_commodity" and phase == "must_cancel" and executor["inFlight"]
        ):
            raise _error(
                "executor.clickReady", "latched click-ready action is not an allowed commodity cancellation"
            )
    if current_kind == "mail_collect":
        if current_seq:
            if work_total < 1 or work_done >= work_total:
                raise _error("executor.workDone", "active mail action must retain work below workTotal")
            if phase in {"mail_items", "mail_wait"} and not executor["inFlight"]:
                raise _error("executor.phase", "mail postcondition phase must be in flight")
            if phase == "ready" and work_done > 0:
                if (
                    not executor["blocked"]
                    or executor["ambiguous"]
                    or executor["latched"]
                    or executor["clickReady"]
                    or last_result
                    not in (
                        {"seq": current_seq, "kind": current_kind, "status": "failed"},
                        {"seq": current_seq, "kind": current_kind, "status": "blocked"},
                    )
                ):
                    raise _error(
                        "executor.workDone", "partial ready mail action requires its terminal refusal witness"
                    )
    elif work_done != 0 or work_total != 0:
        raise _error("executor.work", "non-mail action cannot publish mail progress")
    if result_count == 0:
        if last_result != {"seq": 0, "kind": "", "status": "none"}:
            raise _error("executor.lastResult", "empty result history requires the none sentinel")
    elif result_status == "none" or result_kind in {"", "queue"} or result_seq == 0:
        raise _error("executor.lastResult", "valid result history requires an action result")


def _uid_array(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise _error(path, "expected array")
    result = [_uid(item, f"{path}[]") for item in value]
    if len(result) != len(set(result)) or result != sorted(result, key=lambda item: item.encode()):
        raise _error(path, "UIDs must be unique and byte-sorted")
    return result


def validate_snapshot(
    value: Any,
    *,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> Snapshot:
    """Validate the complete snapshot and its cross-field invariants."""

    rules = DEFAULT_PROTOCOL_CONTRACT if contract is None else contract

    top = _keys(
        value,
        {
            "schema",
            "boot",
            "sequence",
            "wallTime",
            "uptimeMs",
            "context",
            "house",
            "plan",
            "ahOpen",
            "hover",
            "mailOpen",
            "executor",
            "auctioneer",
            "blockingUI",
            "scanner",
            "lastSaved",
            "pending",
            "loaded",
            "chatEdit",
            "motion",
            "render",
        },
        "snapshot",
    )
    if _integer(top["schema"], "schema", minimum=6, maximum=6) != 6:
        raise _error("schema", "must be 6")
    _string(top["boot"], "boot", limit=96)
    # The addon increments before its first publication; zero is reserved for
    # an absent/uninitialized frame and cannot establish freshness.
    _integer(top["sequence"], "sequence", minimum=1)
    wall_time = _integer(top["wallTime"], "wallTime")
    _integer(top["uptimeMs"], "uptimeMs")

    render = _keys(
        top["render"],
        {
            "constructionSeconds",
            "updateCount",
            "updateP99Ms",
            "textureCount",
            "effectiveScale",
            "cellSize",
            "luaHeap",
            "nativePhase",
            "nativeOrdinal",
        },
        "render",
    )
    _number(render["constructionSeconds"], "render.constructionSeconds", maximum=60)
    _integer(render["updateCount"], "render.updateCount", maximum=10_000_000)
    _number(render["updateP99Ms"], "render.updateP99Ms", maximum=60_000)
    # Texture count is a bounded native diagnostic.  The pixel, timing, and
    # memory gates establish the actual render contract; this field must stay
    # finite and positive without freezing the implementation to the retired
    # one-texture-per-bit layout.
    _integer(render["textureCount"], "render.textureCount", minimum=1, maximum=16_384)
    _number(render["effectiveScale"], "render.effectiveScale", minimum=0.0001, maximum=100)
    heap = _keys(
        render["luaHeap"],
        {"method", "available", "beforeKiB", "afterKiB", "netKiB"},
        "render.luaHeap",
    )
    if heap["method"] != "collectgarbage_count":
        raise _error("render.luaHeap.method", "must be collectgarbage_count")
    _boolean(heap["available"], "render.luaHeap.available")
    before_heap = _finite_number(heap["beforeKiB"], "render.luaHeap.beforeKiB", minimum=0)
    after_heap = _finite_number(heap["afterKiB"], "render.luaHeap.afterKiB", minimum=0)
    net_heap = _finite_number(heap["netKiB"], "render.luaHeap.netKiB")
    if not heap["available"]:
        if before_heap != 0 or after_heap != 0 or net_heap != 0:
            raise _error("render.luaHeap", "unavailable measurement must use zero values")
    elif net_heap != after_heap - before_heap:
        raise _error("render.luaHeap.netKiB", "must equal afterKiB-beforeKiB")
    phase = render["nativePhase"]
    # Check the wire type before membership: malformed JSON lists/dicts must
    # become the protocol error exposed to callers, never an unhashable-value
    # TypeError from the set lookup.
    if not isinstance(phase, str) or phase not in {"normal", "held", "updates"}:
        raise _error("render.nativePhase", "must be normal, held, or updates")
    ordinal = _integer(render["nativeOrdinal"], "render.nativeOrdinal", maximum=100)
    if phase == "normal" and ordinal != 0:
        raise _error("render.nativeOrdinal", "normal phase requires ordinal zero")
    if phase == "held" and ordinal not in {0, 100}:
        raise _error("render.nativeOrdinal", "held phase requires ordinal zero or one hundred")
    if phase == "updates" and not 1 <= ordinal <= 99:
        raise _error("render.nativeOrdinal", "updates phase requires ordinal 1..99")
    cell_size = render["cellSize"]
    if (
        not isinstance(cell_size, list)
        or len(cell_size) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value != 2 for value in cell_size)
    ):
        raise _error("render.cellSize", "must be the pinned [2, 2] physical grid")

    context = _keys(
        top["context"],
        {
            "addonVersion",
            "interface",
            "clientBuild",
            "character",
            "realm",
            "playerFaction",
            "zone",
            "subzone",
            "locale",
            "clientVersion",
        },
        "context",
    )
    for key in (
        "addonVersion",
        "clientBuild",
        "character",
        "realm",
        "playerFaction",
        "locale",
        "clientVersion",
    ):
        _string(context[key], f"context.{key}", empty=key in {"character", "realm"})
    for key in ("zone", "subzone"):
        _string(context[key], f"context.{key}", empty=True)
    _integer(context["interface"], "context.interface", minimum=1)

    house = _keys(top["house"], {"key", "source"}, "house")
    _string(house["key"], "house.key")
    _string(house["source"], "house.source")

    plan = _keys(
        top["plan"], {"hash", "count", "cadenceSeconds", "sweepCapSeconds", "initialPriceLevels"}, "plan"
    )
    _string(plan["hash"], "plan.hash")
    if not re.fullmatch(r"[0-9a-f]{64}", plan["hash"]):
        raise _error("plan.hash", "must be lowercase SHA-256 hex")
    if _integer(plan["count"], "plan.count", minimum=1) != rules.plan_count:
        raise _error("plan.count", f"must be {rules.plan_count}")
    if (
        _integer(
            plan["cadenceSeconds"],
            "plan.cadenceSeconds",
            minimum=rules.cadence_seconds,
            maximum=rules.cadence_seconds,
        )
        != rules.cadence_seconds
        or _integer(
            plan["sweepCapSeconds"],
            "plan.sweepCapSeconds",
            minimum=rules.sweep_cap_seconds,
            maximum=rules.sweep_cap_seconds,
        )
        != rules.sweep_cap_seconds
        or _integer(
            plan["initialPriceLevels"],
            "plan.initialPriceLevels",
            minimum=rules.initial_price_levels,
            maximum=rules.initial_price_levels,
        )
        != rules.initial_price_levels
    ):
        raise _error(
            "plan",
            "cadence, cap, and retained levels do not match the resolved target",
        )

    _boolean(top["ahOpen"], "ahOpen")
    hover = _keys(top["hover"], {"kind", "name"}, "hover")
    if not isinstance(hover["kind"], str) or hover["kind"] not in {"none", "unit", "object"}:
        raise _error("hover.kind", "must be none, unit, or object")
    hover_name = _string(hover["name"], "hover.name", empty=True, limit=48)
    if (hover["kind"] == "none") != (hover_name == ""):
        raise _error("hover", "name must be empty exactly when kind is none")
    _boolean(top["mailOpen"], "mailOpen")
    _validate_executor(top["executor"])
    auctioneer = _keys(
        top["auctioneer"],
        {
            "targetPresent",
            "targetName",
            "rangeAvailable",
            "withinTradeRange",
            "bindingAvailable",
            "ctrlF12Action",
            "interactionMode",
        },
        "auctioneer",
    )
    target_present = _boolean(auctioneer["targetPresent"], "auctioneer.targetPresent")
    target_name = _string(auctioneer["targetName"], "auctioneer.targetName", empty=True, limit=128)
    range_available = _boolean(auctioneer["rangeAvailable"], "auctioneer.rangeAvailable")
    within_trade_range = _boolean(auctioneer["withinTradeRange"], "auctioneer.withinTradeRange")
    binding_available = _boolean(auctioneer["bindingAvailable"], "auctioneer.bindingAvailable")
    binding_action = _string(auctioneer["ctrlF12Action"], "auctioneer.ctrlF12Action", empty=True, limit=128)
    interaction_mode = auctioneer["interactionMode"]
    if not isinstance(interaction_mode, str) or interaction_mode not in {
        "disabled",
        "gamepad",
        "keyboard",
        "any",
        "unavailable",
    }:
        raise _error("auctioneer.interactionMode", "invalid native SoftTargetInteract mode")
    if not target_present and (target_name or range_available or within_trade_range):
        raise _error("auctioneer", "absent target must have empty name and unavailable range")
    if target_present and not target_name:
        raise _error("auctioneer.targetName", "present target requires a native name")
    if within_trade_range and not range_available:
        raise _error("auctioneer.withinTradeRange", "cannot be true when range is unavailable")
    if not binding_available and binding_action:
        raise _error("auctioneer.ctrlF12Action", "unavailable binding must have an empty action")
    blocking_ui = _keys(
        top["blockingUI"],
        {
            "staticPopupAvailable",
            "staticPopupShown",
            "gameMenuAvailable",
            "gameMenuShown",
        },
        "blockingUI",
    )
    for name in ("staticPopupAvailable", "staticPopupShown", "gameMenuAvailable", "gameMenuShown"):
        _boolean(blocking_ui[name], f"blockingUI.{name}")
    if blocking_ui["staticPopupShown"] and not blocking_ui["staticPopupAvailable"]:
        raise _error("blockingUI.staticPopupShown", "cannot be true when unavailable")
    if blocking_ui["gameMenuShown"] and not blocking_ui["gameMenuAvailable"]:
        raise _error("blockingUI.gameMenuShown", "cannot be true when unavailable")
    scanner = _keys(
        top["scanner"],
        {
            "phase",
            "activeSweepId",
            "startedAt",
            "dueAt",
            "ordinal",
            "planned",
            "cursorItemID",
            "scheduleFence",
        },
        "scanner",
    )
    phase = scanner["phase"]
    if not isinstance(phase, str) or phase not in {"ready", "scheduled", "item", "owned", "blocked"}:
        raise _error("scanner.phase", "invalid phase")
    active_id = _uid(scanner["activeSweepId"], "scanner.activeSweepId", empty=True)
    started = _integer(scanner["startedAt"], "scanner.startedAt")
    due = _integer(scanner["dueAt"], "scanner.dueAt")
    ordinal = _integer(scanner["ordinal"], "scanner.ordinal")
    planned = _integer(scanner["planned"], "scanner.planned", minimum=0)
    cursor_item_id = _integer(scanner["cursorItemID"], "scanner.cursorItemID")
    schedule_fence = _keys(
        scanner["scheduleFence"],
        {"state", "reason", "command"},
        "scanner.scheduleFence",
    )
    fence_state = schedule_fence["state"]
    if not isinstance(fence_state, str) or fence_state not in {"inactive", "armed", "refused"}:
        raise _error("scanner.scheduleFence.state", "must be inactive, armed, or refused")
    if schedule_fence["command"] != "/crowley session fence":
        raise _error("scanner.scheduleFence.command", "must be /crowley session fence")
    if fence_state == "refused":
        if schedule_fence["reason"] != "scan_active":
            raise _error("scanner.scheduleFence.reason", "refused fence must report scan_active")
    elif schedule_fence["reason"] != "":
        raise _error("scanner.scheduleFence.reason", "non-refused fence must have an empty reason")
    _string(schedule_fence["reason"], "scanner.scheduleFence.reason", empty=True, limit=64)
    if plan_item_ids is not None:
        try:
            configured_ids = frozenset(plan_item_ids)
        except TypeError as exc:
            raise _error("plan.itemIDs", "configured item IDs are not iterable") from exc
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in configured_ids):
            raise _error("plan.itemIDs", "configured item IDs are invalid")
        if cursor_item_id and cursor_item_id not in configured_ids:
            raise _error("scanner.cursorItemID", "cursor is outside the configured plan")
    if planned != rules.plan_count:
        raise _error("scanner.planned", f"must be {rules.plan_count}")
    if phase in {"ready", "scheduled", "blocked"} and (active_id or started):
        raise _error("scanner", "inactive phase cannot have active ID/start")
    if phase == "scheduled" and due == 0:
        raise _error("scanner.dueAt", "scheduled phase needs a due time")
    if phase == "scheduled" and due < wall_time:
        raise _error("scanner.dueAt", "scheduled due time is stale")
    if phase in {"item", "owned"}:
        # The existing scanner creates its UID only at Finish.  The empty string is
        # an explicit wire sentinel; Lua nil must never be used for this field.
        if active_id or started == 0 or due != started + rules.cadence_seconds or not 1 <= ordinal <= planned:
            raise _error("scanner", "active phase timing/ordinal invariant failed")
        if not started <= wall_time <= due:
            raise _error("scanner", "active phase is outside its cadence interval")
    if phase == "blocked" and ordinal != 0:
        raise _error("scanner.ordinal", "blocked phase must have ordinal zero")

    last = _keys(
        top["lastSaved"],
        {"memoryUid", "finishedAt", "planned", "attempted", "failures", "rows", "persistence"},
        "lastSaved",
    )
    memory_uid = _uid(last["memoryUid"], "lastSaved.memoryUid", empty=True)
    finished = _integer(last["finishedAt"], "lastSaved.finishedAt")
    last_planned = _integer(last["planned"], "lastSaved.planned")
    attempted = _integer(last["attempted"], "lastSaved.attempted")
    failures = _integer(last["failures"], "lastSaved.failures")
    _integer(last["rows"], "lastSaved.rows")
    if last["persistence"] != "unknown":
        raise _error("lastSaved.persistence", "must be unknown")
    if not memory_uid:
        if finished or any(last[key] for key in ("planned", "attempted", "failures", "rows")):
            raise _error("lastSaved", "empty UID requires zero counters")
    elif (
        # The last saved sweep may predate a scan-plan change (live 2026-11-09: a 428-item
        # sweep saved before plan revision 3 made every frame invalid, so the loop could
        # never run the 481-item sweep that would replace it).  Its counters must be
        # consistent with its own plan size, not with the current plan's.
        finished == 0
        or last_planned <= 0
        or attempted > last_planned
        or failures < last_planned - attempted
        or failures > last_planned
    ):
        raise _error("lastSaved", "counter invariant failed")
    if failures > attempted + (last_planned - attempted):
        raise _error("lastSaved.failures", "outside planned tail bound")

    pending = _keys(top["pending"], {"ids", "count", "cap", "blocked", "reason"}, "pending")
    pending_ids = _uid_array(pending["ids"], "pending.ids")
    pending_count = _integer(pending["count"], "pending.count", maximum=6)
    pending_cap = _integer(pending["cap"], "pending.cap", minimum=6, maximum=6)
    if pending_count != len(pending_ids) or pending_cap != 6 or pending_count > 6:
        raise _error("pending", "count/cap invariant failed")
    blocked = _boolean(pending["blocked"], "pending.blocked")
    reason = _string(pending["reason"], "pending.reason", empty=True)
    if blocked and reason not in {"queue_full", "plan_invalid"}:
        raise _error("pending.reason", "blocked queue reason is not recognized")
    if not blocked and reason:
        raise _error("pending", "blocked/reason invariant failed")
    if reason == "queue_full" and pending["count"] != pending["cap"]:
        raise _error("pending.reason", "queue_full requires a full queue")
    if phase == "blocked" and not blocked:
        raise _error("scanner.phase", "blocked phase requires pending.blocked")
    if blocked and phase != "blocked":
        raise _error("pending", "blocked queue requires blocked scanner phase")

    loaded = _keys(top["loaded"], {"ackedIds", "generatedAt"}, "loaded")
    _uid_array(loaded["ackedIds"], "loaded.ackedIds")
    _integer(loaded["generatedAt"], "loaded.generatedAt")
    chat = _keys(top["chatEdit"], {"active", "focused", "text"}, "chatEdit")
    _boolean(chat["active"], "chatEdit.active")
    _boolean(chat["focused"], "chatEdit.focused")
    _string(chat["text"], "chatEdit.text", empty=True, limit=128)
    _validate_motion(top["motion"])
    return Snapshot(dict(top))


def _u16(value: int) -> bytes:
    return struct.pack("<H", value)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value & MAX_U32)


def encode_payload(
    snapshot: dict[str, Any],
    *,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> bytes:
    validate_snapshot(snapshot, plan_item_ids=plan_item_ids, contract=contract)
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(raw) + compressor.flush()
    if not 1 <= len(compressed) <= MAX_PAYLOAD:
        raise ProtocolError(f"compressed payload length {len(compressed)} outside 1..{MAX_PAYLOAD}")
    return compressed


def encode_frame(
    snapshot: dict[str, Any],
    *,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> bytes:
    """Encode a validated snapshot into the exact 2048-byte frame."""

    validate_snapshot(snapshot, plan_item_ids=plan_item_ids, contract=contract)
    payload = encode_payload(snapshot, plan_item_ids=plan_item_ids, contract=contract)
    header = b"".join(
        (
            MAGIC,
            bytes((VERSION, ENCODING, WIDTH, HEIGHT)),
            _u16(len(payload)),
            _u32(snapshot["sequence"]),
            _u32(snapshot["wallTime"]),
            _u32(snapshot["uptimeMs"]),
        )
    )
    checksum = binascii.crc32(header + payload) & MAX_U32
    frame = header + _u32(checksum) + payload
    if len(frame) > CAPACITY:
        raise ProtocolError("frame exceeds canvas capacity")
    return frame + bytes(CAPACITY - len(frame))


def _inflate(payload: bytes) -> bytes:
    decompressor = zlib.decompressobj(-15)
    try:
        raw = decompressor.decompress(payload, MAX_INFLATED + 1)
        if len(raw) > MAX_INFLATED:
            raise ProtocolError("inflated payload exceeds limit")
        remaining = MAX_INFLATED + 1 - len(raw)
        raw += decompressor.flush(remaining)
    except ProtocolError:
        raise
    except (zlib.error, ValueError) as exc:
        raise ProtocolError(f"invalid raw deflate: {exc}") from exc
    if (
        len(raw) > MAX_INFLATED
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ProtocolError("inflated payload exceeds limit or is truncated")
    return raw


def decode_frame(
    frame: bytes,
    *,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> Frame:
    if not isinstance(frame, (bytes, bytearray)) or len(frame) != CAPACITY:
        raise ProtocolError(f"frame must be exactly {CAPACITY} bytes")
    if frame[:4] != MAGIC:
        raise ProtocolError("bad magic")
    if frame[4] != VERSION or frame[5] != ENCODING or frame[6] != WIDTH or frame[7] != HEIGHT:
        raise ProtocolError("unsupported frame header")
    payload_length = struct.unpack_from("<H", frame, 8)[0]
    if not 1 <= payload_length <= MAX_PAYLOAD:
        raise ProtocolError("payload length outside 1..2000")
    sequence, wall_time, uptime_ms, expected_crc = struct.unpack_from("<IIII", frame, 10)
    payload = frame[HEADER_SIZE : HEADER_SIZE + payload_length]
    if any(frame[HEADER_SIZE + payload_length :]):
        raise ProtocolError("nonzero padding")
    actual_crc = binascii.crc32(frame[:22] + payload) & MAX_U32
    if actual_crc != expected_crc:
        raise ProtocolError("CRC mismatch")
    try:
        value = json.loads(_inflate(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid snapshot JSON: {exc}") from exc
    snapshot = validate_snapshot(value, plan_item_ids=plan_item_ids, contract=contract)
    if (
        snapshot["sequence"] != sequence
        or snapshot["wallTime"] != wall_time
        or snapshot["uptimeMs"] != uptime_ms
    ):
        raise ProtocolError("header and JSON freshness fields disagree")
    return Frame(
        snapshot,
        snapshot["boot"],
        sequence,
        wall_time,
        uptime_ms,
        expected_crc,
        payload_length,
        bytes(frame),
    )


def pack_cells(frame: bytes) -> list[list[int]]:
    """Return 128 rows of 128 black/white cell bits, LSB-first."""

    if len(frame) != CAPACITY:
        raise ProtocolError("frame must be encoded before cell packing")
    rows: list[list[int]] = []
    for row in range(HEIGHT):
        bits: list[int] = []
        for byte in frame[row * 16 : (row + 1) * 16]:
            bits.extend((byte >> bit) & 1 for bit in range(8))
        rows.append(bits)
    return rows


def unpack_cells(rows: list[list[int]]) -> bytes:
    if len(rows) != HEIGHT or any(len(row) != WIDTH for row in rows):
        raise ProtocolError("cell grid must be 128x128")
    output = bytearray(CAPACITY)
    for cell, value in enumerate(bit for row in rows for bit in row):
        if isinstance(value, bool) or value not in (0, 1):
            raise ProtocolError("cell must be black or white")
        output[cell // 8] |= value << (cell % 8)
    return bytes(output)


def unsigned_delta(new: int, old: int) -> int:
    """Wrap-safe uint32 delta, rejecting the ambiguous half-range."""

    delta = (new - old) & MAX_U32
    if delta >= HALF_U32:
        raise ProtocolError("ambiguous or backwards uint32 freshness value")
    return delta


@dataclass(frozen=True)
class Sample:
    kind: str
    frame: Frame | None
    capture_monotonic: float
    crop: tuple[int, int, int, int]
    crc32: int | None
    window_identity: str


class PixelDecoder:
    def __init__(
        self,
        *,
        crop: tuple[int, int, int, int] = (80, 48, 256, 256),
        threshold: int = 128,
        contract: ProtocolContract | None = None,
    ):
        self.crop = crop
        self.threshold = threshold
        self.contract = contract
        self.boot: str | None = None
        self.sequence: int | None = None
        self.uptime_ms: int | None = None
        self.crc32: int | None = None
        self.window_identity: str | None = None
        self._boot_change_allowed = False

    def reset_for_passive_startup(self) -> None:
        """Start an explicitly owned telemetry epoch for passive startup."""

        self.boot = None
        self.sequence = None
        self.uptime_ms = None
        self.crc32 = None
        self._boot_change_allowed = False

    def allow_boot_change_once(self) -> None:
        """Arm the one fresh boot transition caused by a verified reload."""

        self._boot_change_allowed = True

    def decode(self, pixels: Any, *, monotonic: float, window_identity: str = "") -> Sample:
        try:
            width, height = pixels.size
            if (width, height) != (256, 256):
                raise ProtocolError("calibrated status crop must be 256x256")
            rows: list[list[int]] = []
            for y in range(128):
                row: list[int] = []
                for x in range(128):
                    cell: list[int] = []
                    for dy in range(2):
                        for dx in range(2):
                            red, green, blue, *_ = pixels.getpixel((x * 2 + dx, y * 2 + dy))
                            luminance = (299 * red + 587 * green + 114 * blue) // 1000
                            if luminance not in (0, 255):
                                raise ProtocolError("status cell pixel is neither black nor white")
                            cell.append(int(luminance >= self.threshold))
                    if len(set(cell)) != 1:
                        raise ProtocolError("status cell physical pixels disagree")
                    row.append(cell[0])
                rows.append(row)
            frame = decode_frame(unpack_cells(rows), contract=self.contract)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid pixel crop: {exc}") from exc
        if window_identity == "":
            raise ProtocolError("window identity is required")
        if self.window_identity is not None and window_identity != self.window_identity:
            raise ProtocolError("status window identity changed")
        self.window_identity = window_identity
        if self.boot is not None and frame.boot != self.boot:
            if not self._boot_change_allowed:
                raise ProtocolError("unexpected status boot change")
            self._boot_change_allowed = False
            self.sequence = None
            self.uptime_ms = None
            self.crc32 = None
        if self.boot == frame.boot and self.sequence == frame.sequence and self.uptime_ms == frame.uptime_ms:
            if self.crc32 != frame.crc32:
                raise ProtocolError("duplicate frame tuple has a different CRC")
            return Sample("NO_CHANGE", frame, monotonic, self.crop, frame.crc32, window_identity)
        if self.boot == frame.boot and self.sequence is not None:
            if frame.sequence <= self.sequence:
                raise ProtocolError("status sequence is stale")
            if self.uptime_ms is not None:
                unsigned_delta(frame.uptime_ms, self.uptime_ms)
        self.boot, self.sequence, self.uptime_ms, self.crc32 = (
            frame.boot,
            frame.sequence,
            frame.uptime_ms,
            frame.crc32,
        )
        return Sample("FRESH", frame, monotonic, self.crop, frame.crc32, window_identity)
