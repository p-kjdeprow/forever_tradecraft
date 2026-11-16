"""Strict scanner and game observation frame decoding plus canvas pixel transport."""

from __future__ import annotations

import binascii
import json
import math
import re
import struct
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .scanformat import ScanFormatError, validate_item_key

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
PLAN_COUNT = 807

FrameKind = Literal["scanner", "automation"]


class ProtocolError(ValueError):
    """A frame or snapshot is not admissible."""


@dataclass(frozen=True, slots=True)
class ProtocolContract:
    """Resolved scan plan facts required to validate one scanner publication."""

    plan_count: int
    cadence_seconds: int
    sweep_cap_seconds: int
    initial_price_levels: int
    plan_item_ids: tuple[int, ...]
    planned_sizes: tuple[int, ...] = ()
    plan_hash: str = ""

    @property
    def sweep_sizes(self) -> tuple[int, ...]:
        return self.planned_sizes or (self.plan_count,)

    @classmethod
    def from_plan(cls, plan: Mapping[str, object], *, sweep_cap_seconds: int = 600) -> ProtocolContract:
        item_ids = plan.get("itemIDs")
        cadence = plan.get("cadenceSeconds")
        levels = plan.get("initialPriceLevels")
        build = plan.get("targetClientBuild")
        plan_hash = plan.get("planHash")
        if (
            not isinstance(item_ids, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in item_ids)
            or item_ids != sorted(set(item_ids))
            or isinstance(cadence, bool)
            or not isinstance(cadence, int)
            or isinstance(levels, bool)
            or not isinstance(levels, int)
            or not isinstance(build, str)
            or not isinstance(plan_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
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
        slices = plan.get("coldSlices")
        pools = plan.get("pools")
        if (
            isinstance(slices, bool)
            or not isinstance(slices, int)
            or slices < 0
            or not isinstance(pools, dict)
        ):
            raise ProtocolError("canonical plan cold slices are malformed")
        cold = pools.get("cold")
        if not isinstance(cold, list) or (slices > 0) != bool(cold) or slices > len(cold):
            raise ProtocolError("canonical plan cold pool is malformed")
        hot_count = len(item_ids) - len(cold)
        sizes = tuple(hot_count + len(cold[index::slices]) for index in range(slices))
        return cls(len(item_ids), cadence, sweep_cap_seconds, levels, tuple(item_ids), sizes, plan_hash)


@dataclass(frozen=True, slots=True)
class Snapshot:
    value: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.value[key]


@dataclass(frozen=True, slots=True)
class Frame:
    snapshot: Snapshot
    boot: str
    sequence: int
    wall_time: int
    uptime_ms: int
    crc32: int
    payload_length: int
    raw_bytes: bytes = b""


def _error(path: str, message: str) -> ProtocolError:
    return ProtocolError(f"{path}: {message}")


def _keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "expected object")
    if any(not isinstance(key, str) for key in value):
        raise _error(path, "object keys must be strings")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual, key=lambda item: item.encode("utf-8"))
        unknown = sorted(actual - expected, key=lambda item: item.encode("utf-8"))
        raise _error(path, f"keys differ; missing={missing}, unknown={unknown}")
    return value


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


def _string_array(value: Any, path: str, *, maximum: int | None = None) -> list[str]:
    if not isinstance(value, list) or (maximum is not None and len(value) > maximum):
        raise _error(path, "expected bounded array")
    result = [_string(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if result != sorted(set(result), key=lambda item: item.encode("utf-8")):
        raise _error(path, "must contain unique byte-sorted strings")
    return result


def _frame_header(value: Any, expected_kind: FrameKind | None) -> tuple[dict[str, Any], FrameKind]:
    if not isinstance(value, dict):
        raise _error("snapshot", "expected object")
    kind = value.get("kind")
    if kind not in {"scanner", "automation"}:
        raise _error("kind", "must be scanner or automation")
    if expected_kind is not None and kind != expected_kind:
        raise _error("kind", f"expected {expected_kind}")
    return value, kind


def _base(
    value: dict[str, Any], kind: FrameKind, expected_fields: set[str], *, schema_version: int = 1
) -> tuple[str, int, int, int]:
    top = _keys(value, expected_fields, "snapshot")
    if _integer(top["schema"], "schema", minimum=schema_version, maximum=schema_version) != schema_version:
        raise _error("schema", f"must be {schema_version}")
    if top["kind"] != kind:
        raise _error("kind", f"must be {kind}")
    boot = _string(top["boot"], "boot", limit=128)
    sequence = _integer(top["sequence"], "sequence", minimum=1)
    wall_time = _integer(top["wallTime"], "wallTime", minimum=1)
    uptime_ms = _integer(top["uptimeMs"], "uptimeMs")
    return boot, sequence, wall_time, uptime_ms


def _validate_scanner(
    value: dict[str, Any], contract: ProtocolContract | None, plan_item_ids: Collection[int] | None
) -> Snapshot:
    top_fields = {
        "schema",
        "kind",
        "boot",
        "sequence",
        "wallTime",
        "uptimeMs",
        "addonVersion",
        "house",
        "plan",
        "scanner",
        "lastSaved",
        "pending",
        "loaded",
    }
    boot, sequence, wall_time, uptime_ms = _base(value, "scanner", top_fields)
    top = value
    _string(top["addonVersion"], "addonVersion", limit=64)

    house = _keys(top["house"], {"key", "source"}, "house")
    _string(house["key"], "house.key", limit=64)
    _string(house["source"], "house.source", limit=128)

    plan = _keys(
        top["plan"], {"hash", "count", "cadenceSeconds", "sweepCapSeconds", "initialPriceLevels"}, "plan"
    )
    plan_hash = _string(plan["hash"], "plan.hash", empty=True, limit=64)
    plan_count = _integer(plan["count"], "plan.count")
    cadence = _integer(plan["cadenceSeconds"], "plan.cadenceSeconds")
    sweep_cap = _integer(plan["sweepCapSeconds"], "plan.sweepCapSeconds")
    price_levels = _integer(plan["initialPriceLevels"], "plan.initialPriceLevels")
    plan_available = bool(plan_hash)
    if plan_available:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
            or min(plan_count, cadence, sweep_cap, price_levels) <= 0
        ):
            raise _error("plan", "available plan identity is malformed")
        if contract is not None:
            if (
                plan_hash != contract.plan_hash
                or plan_count != contract.plan_count
                or cadence != contract.cadence_seconds
                or sweep_cap != contract.sweep_cap_seconds
                or price_levels != contract.initial_price_levels
            ):
                raise _error("plan", "does not match the resolved scanner target")
    elif any((plan_count, cadence, sweep_cap, price_levels)):
        raise _error("plan", "unavailable plan must use empty identity and zero values")

    scanner = _keys(
        top["scanner"],
        {"phase", "startedAt", "dueAt", "ordinal", "planned", "cursorItemID", "reason"},
        "scanner",
    )
    phase = scanner["phase"]
    if not isinstance(phase, str) or phase not in {"ready", "scheduled", "item", "owned", "blocked"}:
        raise _error("scanner.phase", "invalid phase")
    started = _integer(scanner["startedAt"], "scanner.startedAt")
    due = _integer(scanner["dueAt"], "scanner.dueAt")
    ordinal = _integer(scanner["ordinal"], "scanner.ordinal")
    planned = _integer(scanner["planned"], "scanner.planned")
    cursor_item_id = _integer(scanner["cursorItemID"], "scanner.cursorItemID")
    reason = _string(scanner["reason"], "scanner.reason", empty=True, limit=64)
    if not plan_available:
        if phase != "blocked" or reason != "plan_invalid" or planned or started or due or ordinal:
            raise _error("scanner", "missing plan must publish a blocked plan_invalid state")
    else:
        rules = contract
        valid_sizes = rules.sweep_sizes if rules else (plan_count,)
        if planned not in valid_sizes:
            raise _error("scanner.planned", f"must be one of {sorted(set(valid_sizes))}")
        if phase in {"ready", "blocked"} and (started or due or ordinal):
            raise _error("scanner", "inactive phase cannot have start, due time, or ordinal")
        if phase == "scheduled" and (started or due == 0 or due < wall_time or ordinal):
            raise _error("scanner", "scheduled phase timing is invalid")
        if phase in {"item", "owned"}:
            if started == 0 or due != started + cadence or not 1 <= ordinal <= planned:
                raise _error("scanner", "active phase timing or ordinal is invalid")
            if not started <= wall_time <= due:
                raise _error("scanner", "active phase is outside its cadence interval")
        if phase == "blocked" and reason != "queue_full":
            raise _error("scanner.reason", "blocked scan must report queue_full")
        if phase != "blocked" and reason:
            raise _error("scanner.reason", "only a blocked scan can have a reason")
    if plan_item_ids is not None:
        configured_ids = frozenset(plan_item_ids)
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in configured_ids):
            raise _error("plan.itemIDs", "configured item IDs are invalid")
        if cursor_item_id and cursor_item_id not in configured_ids:
            raise _error("scanner.cursorItemID", "cursor is outside the configured plan")

    last = _keys(
        top["lastSaved"], {"memoryUid", "finishedAt", "planned", "attempted", "failures", "rows"}, "lastSaved"
    )
    memory_uid = _string(last["memoryUid"], "lastSaved.memoryUid", empty=True, limit=192)
    finished = _integer(last["finishedAt"], "lastSaved.finishedAt")
    last_planned = _integer(last["planned"], "lastSaved.planned")
    attempted = _integer(last["attempted"], "lastSaved.attempted")
    failures = _integer(last["failures"], "lastSaved.failures")
    _integer(last["rows"], "lastSaved.rows")
    if not memory_uid:
        if finished or last_planned or attempted or failures or last["rows"]:
            raise _error("lastSaved", "empty UID requires zero counters")
    elif (
        finished == 0
        or last_planned == 0
        or attempted > last_planned
        or not last_planned - attempted <= failures <= last_planned
    ):
        raise _error("lastSaved", "counter invariant failed")

    pending = _keys(top["pending"], {"ids", "count", "cap", "blocked", "reason"}, "pending")
    pending_ids = _string_array(pending["ids"], "pending.ids", maximum=6)
    pending_count = _integer(pending["count"], "pending.count", maximum=6)
    pending_cap = _integer(pending["cap"], "pending.cap", minimum=6, maximum=6)
    pending_blocked = _boolean(pending["blocked"], "pending.blocked")
    pending_reason = _string(pending["reason"], "pending.reason", empty=True, limit=64)
    if pending_count != len(pending_ids) or pending_cap != 6:
        raise _error("pending", "count/cap invariant failed")
    if pending_blocked != (phase == "blocked") or pending_reason != reason:
        raise _error("pending", "blocked state must match scanner state")
    if reason == "queue_full" and pending_count != pending_cap:
        raise _error("pending", "queue_full requires a full pending list")

    loaded = _keys(top["loaded"], {"ackedIds", "generatedAt"}, "loaded")
    _string_array(loaded["ackedIds"], "loaded.ackedIds")
    _integer(loaded["generatedAt"], "loaded.generatedAt")
    return Snapshot(dict(top))


def _validate_automation(value: dict[str, Any]) -> Snapshot:
    top_fields = {
        "schema",
        "kind",
        "boot",
        "sequence",
        "wallTime",
        "uptimeMs",
        "context",
        "ahOpen",
        "auctioneer",
        "blockingUI",
        "chatEdit",
        "goldCopper",
        "operation",
        "inspection",
        "motion",
        "mailOpen",
        "ownership",
    }
    boot, sequence, wall_time, uptime_ms = _base(value, "automation", top_fields, schema_version=2)
    context = _keys(
        value["context"],
        {
            "addonVersion",
            "interface",
            "clientBuild",
            "character",
            "realm",
            "playerFaction",
            "zone",
            "nativeRegion",
        },
        "context",
    )
    for field in ("addonVersion", "clientBuild", "character", "realm", "playerFaction"):
        _string(context[field], f"context.{field}", limit=128)
    _integer(context["interface"], "context.interface", minimum=1)
    _integer(context["nativeRegion"], "context.nativeRegion", minimum=1, maximum=MAX_U32)
    _string(context["zone"], "context.zone", empty=True, limit=128)
    _boolean(value["ahOpen"], "ahOpen")

    auctioneer = _keys(
        value["auctioneer"],
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
    within_range = _boolean(auctioneer["withinTradeRange"], "auctioneer.withinTradeRange")
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
        raise _error("auctioneer.interactionMode", "invalid native interaction mode")
    if not target_present and (target_name or range_available or within_range):
        raise _error("auctioneer", "absent target has target or range facts")
    if target_present and not target_name:
        raise _error("auctioneer.targetName", "present target requires a name")
    if within_range and not range_available:
        raise _error("auctioneer.withinTradeRange", "cannot be true when range is unavailable")
    if not binding_available and binding_action:
        raise _error("auctioneer.ctrlF12Action", "unavailable binding must have an empty action")

    blocking = _keys(
        value["blockingUI"],
        {"staticPopupAvailable", "staticPopupShown", "gameMenuAvailable", "gameMenuShown"},
        "blockingUI",
    )
    for field in ("staticPopupAvailable", "staticPopupShown", "gameMenuAvailable", "gameMenuShown"):
        _boolean(blocking[field], f"blockingUI.{field}")
    if blocking["staticPopupShown"] and not blocking["staticPopupAvailable"]:
        raise _error("blockingUI.staticPopupShown", "cannot be true when unavailable")
    if blocking["gameMenuShown"] and not blocking["gameMenuAvailable"]:
        raise _error("blockingUI.gameMenuShown", "cannot be true when unavailable")

    chat = _keys(value["chatEdit"], {"active", "focused", "text"}, "chatEdit")
    active = _boolean(chat["active"], "chatEdit.active")
    focused = _boolean(chat["focused"], "chatEdit.focused")
    text = _string(chat["text"], "chatEdit.text", empty=True, limit=128)
    if focused and not active:
        raise _error("chatEdit.focused", "cannot be true when chat is inactive")
    if len(text.encode("utf-8")) > 128:
        raise _error("chatEdit.text", "UTF-8 value is too long")
    _integer(value["goldCopper"], "goldCopper", maximum=9_007_199_254_740_991)

    operation = _keys(
        value["operation"],
        {"batchId", "ordinal", "stepIndex", "expiresAt", "action", "ready", "error", "buttonRect", "receipt"},
        "operation",
    )
    batch_id = _string(operation["batchId"], "operation.batchId", empty=True, limit=64)
    if batch_id and not re.fullmatch(r"[0-9a-f]{64}", batch_id):
        raise _error("operation.batchId", "must be empty or a lowercase SHA256 digest")
    ordinal = _integer(operation["ordinal"], "operation.ordinal")
    step_index = _integer(operation["stepIndex"], "operation.stepIndex")
    expires_at = _integer(operation["expiresAt"], "operation.expiresAt")
    ready = _boolean(operation["ready"], "operation.ready")
    operation_error = _string(operation["error"], "operation.error", empty=True, limit=128)
    action = operation["action"]
    if action is not False:
        _validate_native_action(action)
    if not batch_id:
        if action is not False or ordinal or step_index or expires_at or ready:
            raise _error("operation", "idle action must have empty identity and no ready state")
    elif action is False or expires_at == 0:
        raise _error("operation", "staged operation requires an action and expiry")
    if ready and (not batch_id or operation_error or expires_at <= wall_time):
        raise _error("operation.ready", "ready operation requires a live staged identity")
    button = _keys(operation["buttonRect"], {"x", "y", "width", "height"}, "operation.buttonRect")
    button_x = _integer(button["x"], "operation.buttonRect.x", maximum=32_767)
    button_y = _integer(button["y"], "operation.buttonRect.y", maximum=32_767)
    button_width = _integer(button["width"], "operation.buttonRect.width", minimum=1, maximum=2_048)
    button_height = _integer(button["height"], "operation.buttonRect.height", minimum=1, maximum=2_048)
    if button_x + button_width > 32_768 or button_y + button_height > 32_768:
        raise _error("operation.buttonRect", "button rectangle exceeds the coordinate limit")
    receipt = _keys(
        operation["receipt"],
        {
            "sequence",
            "batchId",
            "ordinal",
            "stepIndex",
            "callbackDelivered",
            "postcondition",
            "actionComplete",
            "detail",
        },
        "operation.receipt",
    )
    receipt_sequence = _integer(receipt["sequence"], "operation.receipt.sequence")
    receipt_batch = _string(receipt["batchId"], "operation.receipt.batchId", empty=True, limit=64)
    receipt_ordinal = _integer(receipt["ordinal"], "operation.receipt.ordinal")
    receipt_step = _integer(receipt["stepIndex"], "operation.receipt.stepIndex")
    for field in ("callbackDelivered", "postcondition", "actionComplete"):
        if type(receipt[field]) is not bool and receipt[field] != "unknown":
            raise _error(f"operation.receipt.{field}", "must be boolean or 'unknown'")
    _string(receipt["detail"], "operation.receipt.detail", empty=True, limit=240)
    if receipt_sequence == 0:
        if (
            receipt_batch
            or receipt_ordinal
            or receipt_step
            or any(
                receipt[field] != "unknown"
                for field in ("callbackDelivered", "postcondition", "actionComplete")
            )
        ):
            raise _error("operation.receipt", "empty receipt must use unknown outcomes and empty identity")
    elif not re.fullmatch(r"[0-9a-f]{64}", receipt_batch):
        raise _error("operation.receipt.batchId", "published receipt requires a lowercase SHA256 digest")

    inspection = _keys(value["inspection"], {"kind", "page", "rows", "hasMore", "error"}, "inspection")
    inspection_kind = inspection["kind"]
    if inspection_kind not in {"none", "inventory", "owned", "mail"}:
        raise _error("inspection.kind", "invalid inspection kind")
    page = _integer(inspection["page"], "inspection.page", maximum=4_095)
    if not isinstance(inspection["rows"], list) or len(inspection["rows"]) > 4:
        raise _error("inspection.rows", "must be an array of at most four rows")
    for index, row in enumerate(inspection["rows"]):
        _validate_inspection_row(inspection_kind, row, f"inspection.rows[{index}]")
    _boolean(inspection["hasMore"], "inspection.hasMore")
    inspection_error = _string(inspection["error"], "inspection.error", empty=True, limit=128)
    if inspection_kind == "none" and (page or inspection["rows"] or inspection["hasMore"]):
        raise _error("inspection", "empty inspection must have no page or rows")
    if inspection_error and (inspection["rows"] or inspection["hasMore"]):
        raise _error("inspection", "failed inspection cannot publish rows")

    motion = _keys(
        value["motion"],
        {
            "x",
            "y",
            "instance",
            "facing",
            "moving",
            "dead",
            "inCombat",
            "mounted",
            "target",
            "bindings",
            "hover",
        },
        "motion",
    )
    for field in ("x", "y", "facing"):
        _finite_number(motion[field], f"motion.{field}")
    _integer(motion["instance"], "motion.instance")
    for field in ("moving", "dead", "inCombat", "mounted"):
        _boolean(motion[field], f"motion.{field}")
    target = _keys(
        motion["target"],
        {"present", "guid", "name", "rangeAvailable", "withinRange"},
        "motion.target",
    )
    target_present = _boolean(target["present"], "motion.target.present")
    target_guid = _string(target["guid"], "motion.target.guid", empty=True, limit=128)
    target_name = _string(target["name"], "motion.target.name", empty=True, limit=128)
    range_available = _boolean(target["rangeAvailable"], "motion.target.rangeAvailable")
    within_range = _boolean(target["withinRange"], "motion.target.withinRange")
    if not target_present and (target_guid or target_name or range_available or within_range):
        raise _error("motion.target", "absent target has target or range facts")
    if target_present and (not target_guid or not target_name):
        raise _error("motion.target", "present target requires GUID and name")
    if within_range and not range_available:
        raise _error("motion.target.withinRange", "cannot be true when range is unavailable")
    bindings = _keys(motion["bindings"], {"forward", "left", "right"}, "motion.bindings")
    for field in ("forward", "left", "right"):
        _string(bindings[field], f"motion.bindings.{field}", empty=True, limit=64)
    hover = _keys(motion["hover"], {"kind", "name", "screenX", "screenY"}, "motion.hover")
    hover_kind = hover["kind"]
    if hover_kind not in {"none", "unit", "object"}:
        raise _error("motion.hover.kind", "invalid hover kind")
    hover_name = _string(hover["name"], "motion.hover.name", empty=True, limit=128)
    hover_x = _finite_number(hover["screenX"], "motion.hover.screenX")
    hover_y = _finite_number(hover["screenY"], "motion.hover.screenY")
    if not 0 <= hover_x <= 32_767 or not 0 <= hover_y <= 32_767:
        raise _error("motion.hover", "physical cursor coordinates are outside the bound")
    if (hover_kind == "none") != (hover_name == ""):
        raise _error("motion.hover", "only a none hover has an empty name")

    _boolean(value["mailOpen"], "mailOpen")
    ownership = _keys(value["ownership"], {"mode", "ahOwner", "scannerState"}, "ownership")
    if ownership["mode"] not in {"standalone", "coordinated"}:
        raise _error("ownership.mode", "invalid ownership mode")
    if ownership["ahOwner"] not in {"none", "automation", "scanner"}:
        raise _error("ownership.ahOwner", "invalid auction-house owner")
    if ownership["scannerState"] not in {
        "not_installed",
        "available",
        "active",
        "quiescent",
        "suspended",
        "unavailable",
    }:
        raise _error("ownership.scannerState", "invalid scanner state")
    if ownership["mode"] == "standalone" and ownership["scannerState"] != "not_installed":
        raise _error("ownership", "standalone mode cannot publish scanner state")
    return Snapshot(dict(value))


def _finite_number(value: Any, path: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _error(path, "expected a finite number")
    return value


def _action_integer(value: Any, path: str, *, minimum: int = 0, maximum: int = 9_007_199_254_740_991) -> int:
    return _integer(value, path, minimum=minimum, maximum=maximum)


def _validate_native_action(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise _error("operation.action", "expected an action object")
    kind = value["kind"]
    if kind in {"post_commodity", "post_item"}:
        row = _keys(
            value,
            {"kind", "itemKey", "quantity", "unitPriceCopper", "durationHours", "maxDepositCopper"},
            "operation.action",
        )
        try:
            validate_item_key(row["itemKey"])
        except (ScanFormatError, TypeError) as exc:
            raise _error("operation.action.itemKey", "invalid canonical item key") from exc
        _action_integer(row["quantity"], "operation.action.quantity", minimum=1, maximum=200)
        price = _action_integer(row["unitPriceCopper"], "operation.action.unitPriceCopper", minimum=1)
        duration = _action_integer(
            row["durationHours"], "operation.action.durationHours", minimum=12, maximum=48
        )
        if duration not in {12, 24, 48} or price * row["quantity"] > 9_007_199_254_740_991:
            raise _error("operation.action", "posting duration or total price is outside bounds")
        _action_integer(row["maxDepositCopper"], "operation.action.maxDepositCopper")
    elif kind == "cancel_auction":
        row = _keys(value, {"kind", "auctionId", "maxCancelCostCopper"}, "operation.action")
        _action_integer(row["auctionId"], "operation.action.auctionId", minimum=1)
        _action_integer(row["maxCancelCostCopper"], "operation.action.maxCancelCostCopper")
    elif kind == "collect_mail":
        row = _keys(value, {"kind", "messageLimit"}, "operation.action")
        _action_integer(row["messageLimit"], "operation.action.messageLimit", minimum=1, maximum=50)
    elif kind in {"interact", "move_route"}:
        field = "targetId" if kind == "interact" else "routeId"
        row = _keys(value, {"kind", field}, "operation.action")
        target = _string(row[field], f"operation.action.{field}", limit=64)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", target):
            raise _error(f"operation.action.{field}", "must be a configured identifier")
    else:
        raise _error("operation.action.kind", "unsupported action kind")


def _validate_inspection_row(kind: str, value: Any, path: str) -> None:
    if kind == "inventory":
        row = _keys(
            value, {"bag", "slot", "itemKey", "itemLink", "count", "locked", "bound", "commodity"}, path
        )
        _integer(row["bag"], f"{path}.bag", maximum=4)
        _integer(row["slot"], f"{path}.slot", minimum=1, maximum=256)
        try:
            validate_item_key(row["itemKey"])
        except (ScanFormatError, TypeError) as exc:
            raise _error(f"{path}.itemKey", "invalid canonical item key") from exc
        _string(row["itemLink"], f"{path}.itemLink", limit=512)
        _integer(row["count"], f"{path}.count", minimum=1, maximum=9_007_199_254_740_991)
        for field in ("locked", "bound", "commodity"):
            _boolean(row[field], f"{path}.{field}")
    elif kind == "owned":
        row = _keys(
            value,
            {
                "auctionId",
                "itemKey",
                "itemLink",
                "quantity",
                "buyoutKnown",
                "buyoutCopper",
                "status",
                "cancelAllowed",
                "cancelCostKnown",
                "cancelCostCopper",
            },
            path,
        )
        _integer(row["auctionId"], f"{path}.auctionId", minimum=1)
        try:
            validate_item_key(row["itemKey"])
        except (ScanFormatError, TypeError) as exc:
            raise _error(f"{path}.itemKey", "invalid canonical item key") from exc
        _string(row["itemLink"], f"{path}.itemLink", limit=512)
        _integer(row["quantity"], f"{path}.quantity", minimum=1, maximum=9_007_199_254_740_991)
        buyout_known = _boolean(row["buyoutKnown"], f"{path}.buyoutKnown")
        buyout = _integer(row["buyoutCopper"], f"{path}.buyoutCopper")
        if not buyout_known and buyout != 0:
            raise _error(f"{path}.buyoutCopper", "unknown buyout must be zero")
        if row["status"] not in {"active", "sold"}:
            raise _error(f"{path}.status", "invalid owned auction status")
        _boolean(row["cancelAllowed"], f"{path}.cancelAllowed")
        cost_known = _boolean(row["cancelCostKnown"], f"{path}.cancelCostKnown")
        cost = _integer(row["cancelCostCopper"], f"{path}.cancelCostCopper")
        if not cost_known and cost != 0:
            raise _error(f"{path}.cancelCostCopper", "unknown cancellation cost must be zero")
    elif kind == "mail":
        row = _keys(
            value,
            {
                "index",
                "moneyCopper",
                "codCopper",
                "attachmentCount",
                "attachmentItemIDs",
                "returned",
                "canDelete",
                "daysLeft",
            },
            path,
        )
        _integer(row["index"], f"{path}.index", minimum=1, maximum=1_000_000)
        _integer(row["moneyCopper"], f"{path}.moneyCopper")
        _integer(row["codCopper"], f"{path}.codCopper")
        attachment_count = _integer(row["attachmentCount"], f"{path}.attachmentCount", maximum=16)
        item_ids = row["attachmentItemIDs"]
        if not isinstance(item_ids, list) or len(item_ids) != attachment_count:
            raise _error(f"{path}.attachmentItemIDs", "must exactly describe the attachment count")
        for index, item_id in enumerate(item_ids):
            _integer(item_id, f"{path}.attachmentItemIDs[{index}]", minimum=1)
        _boolean(row["returned"], f"{path}.returned")
        _boolean(row["canDelete"], f"{path}.canDelete")
        days_left = _finite_number(row["daysLeft"], f"{path}.daysLeft")
        if not 0 <= days_left <= 365:
            raise _error(f"{path}.daysLeft", "must be within the one year bound")
    else:
        raise _error(path, "rows require inventory, owned, or mail inspection")


def validate_snapshot(
    value: Any,
    *,
    expected_kind: FrameKind | None = None,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> Snapshot:
    top, kind = _frame_header(value, expected_kind)
    if kind == "scanner":
        return _validate_scanner(top, contract, plan_item_ids)
    if contract is not None or plan_item_ids is not None:
        raise ProtocolError("scanner plan validation cannot be applied to automation facts")
    return _validate_automation(top)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value & MAX_U32)


def encode_payload(
    snapshot: dict[str, Any],
    *,
    expected_kind: FrameKind | None = None,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> bytes:
    validate_snapshot(snapshot, expected_kind=expected_kind, plan_item_ids=plan_item_ids, contract=contract)
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(raw) + compressor.flush()
    if not 1 <= len(compressed) <= MAX_PAYLOAD:
        raise ProtocolError(f"compressed payload length {len(compressed)} outside 1..{MAX_PAYLOAD}")
    return compressed


def encode_frame(
    snapshot: dict[str, Any],
    *,
    expected_kind: FrameKind | None = None,
    plan_item_ids: Collection[int] | None = None,
    contract: ProtocolContract | None = None,
) -> bytes:
    validate_snapshot(snapshot, expected_kind=expected_kind, plan_item_ids=plan_item_ids, contract=contract)
    payload = encode_payload(
        snapshot, expected_kind=expected_kind, plan_item_ids=plan_item_ids, contract=contract
    )
    header = b"".join(
        (
            MAGIC,
            bytes((VERSION, ENCODING, WIDTH, HEIGHT)),
            struct.pack("<H", len(payload)),
            _u32(snapshot["sequence"]),
            _u32(snapshot["wallTime"]),
            _u32(snapshot["uptimeMs"]),
        )
    )
    checksum = _u32(binascii.crc32(header + payload) & MAX_U32)
    frame = header + checksum + payload
    if len(frame) > CAPACITY:
        raise ProtocolError("frame exceeds canvas capacity")
    return frame + bytes(CAPACITY - len(frame))


def _inflate(payload: bytes) -> bytes:
    decompressor = zlib.decompressobj(-15)
    try:
        raw = decompressor.decompress(payload, MAX_INFLATED + 1)
        if len(raw) > MAX_INFLATED:
            raise ProtocolError("inflated payload exceeds limit")
        raw += decompressor.flush(MAX_INFLATED + 1 - len(raw))
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
    expected_kind: FrameKind | None = None,
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
    if binascii.crc32(frame[:22] + payload) & MAX_U32 != expected_crc:
        raise ProtocolError("CRC mismatch")
    try:
        value = json.loads(_inflate(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid snapshot JSON: {exc}") from exc
    snapshot = validate_snapshot(
        value, expected_kind=expected_kind, plan_item_ids=plan_item_ids, contract=contract
    )
    if (
        snapshot["sequence"] != sequence
        or snapshot["wallTime"] != wall_time
        or snapshot["uptimeMs"] != uptime_ms
    ):
        raise ProtocolError("header and JSON freshness fields disagree")
    return Frame(
        snapshot, snapshot["boot"], sequence, wall_time, uptime_ms, expected_crc, payload_length, bytes(frame)
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


@dataclass(frozen=True, slots=True)
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
        expected_kind: FrameKind,
        crop: tuple[int, int, int, int],
        threshold: int = 128,
        contract: ProtocolContract | None = None,
    ):
        if expected_kind not in {"scanner", "automation"}:
            raise ProtocolError("expected kind must be scanner or automation")
        if expected_kind == "automation" and contract is not None:
            raise ProtocolError("automation decoder cannot use a scanner plan contract")
        self.expected_kind: FrameKind = expected_kind
        self.crop = crop
        self.threshold = threshold
        self.contract = contract
        self.boot: str | None = None
        self.sequence: int | None = None
        self.uptime_ms: int | None = None
        self.crc32: int | None = None
        self.window_identity: str | None = None
        self._boot_change_allowed = False

    def allow_boot_change_once(self) -> None:
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
            frame = decode_frame(unpack_cells(rows), expected_kind=self.expected_kind, contract=self.contract)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid pixel crop: {exc}") from exc
        if not window_identity:
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
            unsigned_delta(frame.uptime_ms, self.uptime_ms or 0)
        self.boot, self.sequence, self.uptime_ms, self.crc32 = (
            frame.boot,
            frame.sequence,
            frame.uptime_ms,
            frame.crc32,
        )
        return Sample("FRESH", frame, monotonic, self.crop, frame.crc32, window_identity)
