"""Strict scanner and game observation frame decoding plus canvas pixel transport."""

from __future__ import annotations

import binascii
import json
import re
import struct
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

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


def _base(value: dict[str, Any], kind: FrameKind, expected_fields: set[str]) -> tuple[str, int, int, int]:
    top = _keys(value, expected_fields, "snapshot")
    if _integer(top["schema"], "schema", minimum=1, maximum=1) != 1:
        raise _error("schema", "must be 1")
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
    }
    boot, sequence, wall_time, uptime_ms = _base(value, "automation", top_fields)
    context = _keys(
        value["context"],
        {"addonVersion", "interface", "clientBuild", "character", "realm", "playerFaction", "zone"},
        "context",
    )
    for field in ("addonVersion", "clientBuild", "character", "realm", "playerFaction"):
        _string(context[field], f"context.{field}", limit=128)
    _integer(context["interface"], "context.interface", minimum=1)
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
    return Snapshot(dict(value))


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
