"""Strict scan-format validation and raw-deflated JSON payload encoding."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import zlib
from typing import Any

from .game_target import FIXED_COLLECTION_CADENCE_SECONDS

PRODUCER_FORMAT = 3
ACCEPTED_FORMATS = (2, 3)
MAX_SAFE_INTEGER = 9_007_199_254_740_991
UINT32_MODULUS = 4_294_967_296
UINT32_MAX = UINT32_MODULUS - 1
WRAPPED_TIME_LEFT_MIN = UINT32_MODULUS - 300
MAX_EXACT_TIME_LEFT_SECONDS = 172_800


class ScanFormatError(ValueError):
    """A supported scan envelope or payload is not admissible."""


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScanFormatError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def decode_strict_json(source: str | bytes | bytearray) -> dict[str, Any]:
    """Decode one outer JSON object while rejecting duplicate keys.

    FastAPI/Pydantic normally gives an endpoint a dict after the standard JSON
    decoder has already discarded duplicate keys. The upload endpoint uses this
    helper on request bytes before validation.
    """
    if not isinstance(source, (str, bytes, bytearray)):
        raise ScanFormatError("JSON request must be text or bytes")
    try:
        value = json.loads(source, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanFormatError(f"malformed JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ScanFormatError("JSON request must be an object")
    return value


# Public aliases let the server worker choose a descriptive import without
# creating a second decoder or a second wire representation.
decode_outer_json = decode_strict_json
decode_json_object = decode_strict_json
decode_json_envelope = decode_strict_json


def decode_json_payload(encoded: str) -> dict[str, Any]:
    """Decode the one format-2 raw-deflate/base64 JSON representation."""
    if not isinstance(encoded, str):
        raise ScanFormatError("payload must be an encoded string")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        raw = zlib.decompress(compressed, -zlib.MAX_WBITS)
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_pairs, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeDecodeError, zlib.error, json.JSONDecodeError) as exc:
        raise ScanFormatError(f"malformed compressed payload: {exc}") from exc
    if not isinstance(value, dict):
        raise ScanFormatError("payload must be a JSON object")
    return value


def encode_json_payload(value: dict[str, Any]) -> str:
    """Encode a canonical JSON object using raw deflate and standard base64."""
    if not isinstance(value, dict):
        raise ScanFormatError("payload must be an object")
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return base64.b64encode(compressor.compress(text.encode("utf-8")) + compressor.flush()).decode("ascii")


def _native_keyed_collection(value: Any, where: str) -> dict[str, Any]:
    """Validate a Lua keyed table after WoW's JSON encoding.

    WoW's native encoder represents an empty Lua table as ``[]`` and a
    non-empty keyed table as an object.  The internal representation remains
    a mapping so the rest of the validator can use the canonical row/name
    operations.  Empty objects and non-empty arrays are deliberately rejected
    instead of being repaired here.
    """
    if isinstance(value, list):
        if value:
            raise ScanFormatError(f"{where} must be an empty array or non-empty object")
        return {}
    if isinstance(value, dict):
        if not value:
            raise ScanFormatError(f"{where} must be an empty array or non-empty object")
        return value
    raise ScanFormatError(f"{where} must be an empty array or non-empty object")


def _keys(value: Any, expected: set[str], where: str) -> None:
    if not isinstance(value, dict):
        raise ScanFormatError(f"{where} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ScanFormatError(f"{where} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ScanFormatError(f"{where} is missing fields: {sorted(missing)}")


def _int(value: Any, where: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ScanFormatError(f"{where} must be an integer in [{minimum}, {maximum}]")
    return value


def _string(value: Any, where: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ScanFormatError(f"{where} must be a {'non-empty ' if nonempty else ''}string")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ScanFormatError(f"{where} must be boolean")
    return value


def _choice(value: Any, allowed: set[str], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ScanFormatError(f"{where} has an invalid value")
    return value


def _rational(value: Any, where: str, *, positive: bool = True) -> tuple[int, int]:
    _keys(value, {"numerator", "denominator"}, where)
    numerator = _int(value["numerator"], f"{where}.numerator", minimum=1 if positive else 0)
    denominator = _int(value["denominator"], f"{where}.denominator", minimum=1)
    if math.gcd(numerator, denominator) != 1:
        raise ScanFormatError(f"{where} is not reduced")
    return numerator, denominator


def _reduced(numerator: int, denominator: int) -> tuple[int, int]:
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _safe_product(a: int, b: int, where: str) -> int:
    if a < 0 or b < 0 or a > MAX_SAFE_INTEGER or b > MAX_SAFE_INTEGER:
        raise ScanFormatError(f"{where} is outside the safe numeric domain")
    if a and b > MAX_SAFE_INTEGER // a:
        raise ScanFormatError(f"{where} comparison product is unsafe")
    return a * b


def _compare_rational(left: tuple[int, int], right: tuple[int, int], where: str) -> int:
    # Check both products before multiplying: Lua's safe integer boundary must
    # produce an incomplete numeric-range sample rather than a rounded order.
    left_product = _safe_product(left[0], right[1], where)
    right_product = _safe_product(right[0], left[1], where)
    return (left_product > right_product) - (left_product < right_product)


def _validate_timestamp(value: Any, where: str) -> int:
    epoch = _int(value, where)
    try:
        import datetime

        datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ScanFormatError(f"{where} is not a storable timestamp") from exc
    return epoch


def _validate_plan(plan_json: Any, plan_hash: Any) -> dict[str, Any]:
    _string(plan_json, "payload.planJSON")
    _string(plan_hash, "payload.planHash")
    try:
        plan = json.loads(plan_json, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ScanFormatError(f"invalid planJSON: {exc}") from exc
    _keys(
        plan,
        {
            "schemaVersion",
            "id",
            "revision",
            "catalogHash",
            "targetClientBuild",
            "cadenceSeconds",
            "initialPriceLevels",
            "itemIDs",
            "pools",
        }
        | ({"coldSlices"} if isinstance(plan, dict) and "coldSlices" in plan else set()),
        "planJSON",
    )
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    if canonical != plan_json:
        raise ScanFormatError("planJSON is not canonical")
    if hashlib.sha256(plan_json.encode("utf-8")).hexdigest() != plan_hash:
        raise ScanFormatError("planHash does not match planJSON")
    if (
        isinstance(plan["schemaVersion"], bool)
        or not isinstance(plan["schemaVersion"], int)
        or plan["schemaVersion"] != 2
        or plan["id"] != "first-run"
    ):
        raise ScanFormatError("unsupported scan plan")
    _int(plan["revision"], "plan.revision", minimum=1)
    _string(plan["catalogHash"], "plan.catalogHash")
    if not re.fullmatch(r"[0-9a-f]{64}", plan["catalogHash"]):
        raise ScanFormatError("plan.catalogHash must be lowercase SHA-256 hex")
    _string(plan["targetClientBuild"], "plan.targetClientBuild")
    if (
        isinstance(plan["cadenceSeconds"], bool)
        or not isinstance(plan["cadenceSeconds"], int)
        or plan["cadenceSeconds"] != FIXED_COLLECTION_CADENCE_SECONDS
    ):
        raise ScanFormatError("plan cadenceSeconds must be " + str(FIXED_COLLECTION_CADENCE_SECONDS))
    initial_levels = _int(plan["initialPriceLevels"], "plan.initialPriceLevels", minimum=1)
    ids = plan["itemIDs"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(isinstance(item_id, bool) or not isinstance(item_id, int) for item_id in ids)
        or ids != sorted(set(ids))
    ):
        raise ScanFormatError("plan.itemIDs must be sorted, unique and nonempty")
    for item_id in ids:
        _int(item_id, "plan.itemIDs[]", minimum=1)
    # Plans from before the cold tier carry neither coldSlices nor pools.cold; they are N == 0.
    cold_slices = _int(plan["coldSlices"], "plan.coldSlices") if "coldSlices" in plan else 0
    pool_names = ("core", "discovery", "cold") if "coldSlices" in plan else ("core", "discovery")
    _keys(plan["pools"], set(pool_names), "plan.pools")
    pool_ids: list[int] = []
    for pool in pool_names:
        values = plan["pools"][pool]
        if (
            not isinstance(values, list)
            or any(isinstance(item_id, bool) or not isinstance(item_id, int) for item_id in values)
            or values != sorted(set(values))
        ):
            raise ScanFormatError(f"plan.pools.{pool} must be sorted and unique")
        for item_id in values:
            _int(item_id, f"plan.pools.{pool}[]", minimum=1)
        pool_ids.extend(values)
    if sorted(pool_ids) != ids:
        raise ScanFormatError("plan pools must partition itemIDs")
    if (cold_slices > 0) != bool(plan["pools"].get("cold")):
        raise ScanFormatError("plan.coldSlices must be positive exactly when pools.cold is nonempty")
    if cold_slices > len(plan["pools"].get("cold", ())):
        raise ScanFormatError("plan.coldSlices exceeds the number of cold items")
    plan["initialPriceLevels"] = initial_levels
    return plan


def _validate_raw_key(value: Any, row_item_id: int, where: str) -> dict[str, Any]:
    _keys(value, {"itemID", "itemLevel", "itemSuffix", "battlePetSpeciesID", "bonusIDs"}, where)
    for key in ("itemID", "itemLevel", "itemSuffix", "battlePetSpeciesID"):
        _int(value[key], f"{where}.{key}")
    if value["itemID"] != row_item_id:
        raise ScanFormatError(f"{where}.itemID does not match row itemID")
    bonuses = _native_keyed_collection(value["bonusIDs"], f"{where}.bonusIDs")
    value["bonusIDs"] = bonuses
    for index, bonus in bonuses.items():
        if not isinstance(index, str) or not re.fullmatch(r"\d+", index):
            raise ScanFormatError(f"{where}.bonusIDs keys must be decimal")
        _int(bonus, f"{where}.bonusIDs[{index}]", minimum=1)
    if set(bonuses) != {str(index) for index in range(len(bonuses))}:
        raise ScanFormatError(f"{where}.bonusIDs keys must be contiguous from zero")
    return value


def _canonical_item_key(item_id: int, raw: dict[str, Any]) -> str:
    parts = [str(item_id)]
    if raw["itemSuffix"]:
        parts.append(f"s{raw['itemSuffix']}")
    if raw["bonusIDs"]:
        bonuses = [raw["bonusIDs"][index] for index in sorted(raw["bonusIDs"], key=int)]
        parts.append("b" + ".".join(str(bonus) for bonus in sorted(bonuses)))
    if raw["itemLevel"]:
        parts.append(f"l{raw['itemLevel']}")
    if raw["battlePetSpeciesID"]:
        parts.append(f"p{raw['battlePetSpeciesID']}")
    return ":".join(parts)


def _validate_item_link(value: Any, item_id: int, raw: dict[str, Any], where: str) -> None:
    _string(value, where)
    match = re.search(r"item:([\-\d:]+)", value)
    if not match:
        raise ScanFormatError(f"{where} does not identify an item")
    fields = match.group(1).split(":")
    try:
        if len(fields) < 13:
            raise ValueError("item link omits identity fields")
        link_item_id = int(fields[0])
        link_suffix = int(fields[6]) if len(fields) > 6 and fields[6] else 0
        bonus_count = int(fields[12]) if len(fields) > 12 and fields[12] else 0
        if bonus_count < 0 or len(fields) < 13 + bonus_count:
            raise ValueError("bonus count exceeds link fields")
        link_bonuses = [int(entry) for entry in fields[13 : 13 + bonus_count]]
    except (TypeError, ValueError) as exc:
        raise ScanFormatError(f"{where} has malformed item identity") from exc
    if link_item_id != item_id:
        raise ScanFormatError(f"{where} item identity does not match itemID")
    if link_suffix != raw["itemSuffix"]:
        raise ScanFormatError(f"{where} suffix does not match raw item identity")
    raw_bonuses = [raw["bonusIDs"][index] for index in sorted(raw["bonusIDs"], key=int)]
    if sorted(link_bonuses) != sorted(raw_bonuses):
        raise ScanFormatError(f"{where} bonuses do not match raw item identity")


def _validate_link(value: Any, where: str, *, commodity: bool) -> None:
    if not isinstance(value, dict):
        raise ScanFormatError(f"{where} must be an object")
    kind = value.get("kind")
    if kind == "unknown":
        _keys(value, {"kind"}, where)
        if not commodity:
            raise ScanFormatError("noncommodity rows require an item link")
    elif kind == "known":
        _keys(value, {"kind", "value"}, where)
        _string(value["value"], f"{where}.value")
    else:
        raise ScanFormatError(f"{where}.kind must be known or unknown")


def _validate_owners(value: Any, kind: str, where: str) -> None:
    _keys(
        value,
        {"names", "totalNumberOfOwners", "containsOwnerItem", "containsAccountItem", "identityKind"},
        where,
    )
    names = _native_keyed_collection(value["names"], f"{where}.names")
    value["names"] = names
    if any(not isinstance(name, str) or not name or v is not True for name, v in names.items()):
        raise ScanFormatError(f"{where}.names must be a name:true object")
    owner_count = _int(value["totalNumberOfOwners"], f"{where}.totalNumberOfOwners")
    if owner_count < len(names):
        raise ScanFormatError(f"{where}.totalNumberOfOwners is below known names")
    owner_item = _bool(value["containsOwnerItem"], f"{where}.containsOwnerItem")
    account_item = _bool(value["containsAccountItem"], f"{where}.containsAccountItem")
    identity = _choice(value["identityKind"], {"individual", "grouped", "unknown"}, f"{where}.identityKind")
    if kind == "commodity" and identity != "grouped":
        raise ScanFormatError(f"{where}.commodity owners must be grouped")
    individually_identifiable = owner_count == 1 and len(names) == 1 and not owner_item and not account_item
    if identity == "individual" and not individually_identifiable:
        raise ScanFormatError(f"{where}.individual owner identity is inconsistent")
    if kind != "commodity" and identity == "grouped" and individually_identifiable:
        raise ScanFormatError(f"{where}.grouped owner identity is inconsistent")


def _validate_time_left(value: Any, where: str, wire_format: int) -> None:
    if not isinstance(value, dict):
        raise ScanFormatError(f"{where} is invalid")
    kind = _choice(value.get("kind"), {"exact", "band", "unknown"}, f"{where}.kind")
    if kind == "exact":
        _keys(value, {"kind", "seconds"}, where)
        if wire_format == 3:
            _int(
                value["seconds"],
                f"{where}.seconds",
                minimum=-300,
                maximum=MAX_EXACT_TIME_LEFT_SECONDS,
            )
        else:
            # Format 2 is historical wire data.  Preserve its original
            # non-negative safe-integer rule; normalization handles the one
            # newly understood uint32-wrapped window for storage.
            _int(value["seconds"], f"{where}.seconds")
    elif kind == "band":
        _keys(value, {"kind", "band", "minimumSeconds", "maximumSeconds"}, where)
        _int(value["band"], f"{where}.band", maximum=3)
        minimum = _int(value["minimumSeconds"], f"{where}.minimumSeconds")
        maximum = _int(value["maximumSeconds"], f"{where}.maximumSeconds")
        if minimum > maximum:
            raise ScanFormatError(f"{where} band bounds are unordered")
    else:
        _keys(value, {"kind"}, where)


def _validate_row(value: Any, planned_id: int, kind: str, where: str, wire_format: int) -> None:
    _keys(
        value,
        {
            "resultID",
            "itemID",
            "itemKey",
            "rawItemKey",
            "link",
            "quantity",
            "price",
            "unitPrice",
            "owners",
            "timeLeft",
        },
        where,
    )
    _int(value["resultID"], f"{where}.resultID", minimum=1)
    item_id = _int(value["itemID"], f"{where}.itemID", minimum=1)
    if item_id != planned_id:
        raise ScanFormatError(f"{where}.itemID is outside its observation")
    raw = _validate_raw_key(value["rawItemKey"], item_id, f"{where}.rawItemKey")
    item_key = _string(value["itemKey"], f"{where}.itemKey")
    if item_key != _canonical_item_key(item_id, raw):
        raise ScanFormatError(f"{where}.itemKey does not match raw item identity")
    _validate_link(value["link"], f"{where}.link", commodity=kind == "commodity")
    if value["link"]["kind"] == "known":
        _validate_item_link(value["link"]["value"], item_id, raw, f"{where}.link.value")
    quantity = _int(value["quantity"], f"{where}.quantity", minimum=1)
    price = value["price"]
    _keys(price, {"kind", "copper"}, f"{where}.price")
    expected_price_kind = "unit" if kind == "commodity" else "auction"
    if price["kind"] != expected_price_kind:
        raise ScanFormatError(f"{where}.price kind does not match observation")
    copper = _int(price["copper"], f"{where}.price.copper", minimum=1)
    unit = _rational(value["unitPrice"], f"{where}.unitPrice")
    if kind == "commodity" and unit != (copper, 1):
        raise ScanFormatError(f"{where}.unitPrice must equal commodity price")
    if kind == "item":
        expected = (copper, 1) if wire_format == 3 else _reduced(copper, quantity)
        if unit != expected:
            if wire_format == 3:
                raise ScanFormatError(f"{where}.unitPrice must equal item auction price")
            raise ScanFormatError(f"{where}.unitPrice must equal reduced buyout / quantity")
    _validate_owners(value["owners"], kind, f"{where}.owners")
    _validate_time_left(value["timeLeft"], f"{where}.timeLeft", wire_format)


def _validate_pairwise_prices(
    rows: list[dict[str, Any]],
    where: str,
    *,
    cutoff: tuple[int, int] | None = None,
    witness: tuple[int, int] | None = None,
) -> None:
    """Check every exact comparison needed by a successful scope.

    Sorting adjacent rows is insufficient: a non-adjacent pair can overflow
    the safe integer domain even when every adjacent comparison is safe. Do
    the complete comparison set before any ordering or level proof.
    """
    ratios = [_rational(row["unitPrice"], f"{where}.row.unitPrice") for row in rows]
    for index, left in enumerate(ratios):
        for right in ratios[index + 1 :]:
            _compare_rational(left, right, f"{where}.rowPricePair")
        if cutoff is not None:
            _compare_rational(left, cutoff, f"{where}.cutoff")
        if witness is not None:
            _compare_rational(left, witness, f"{where}.higherPriceWitness")
    if cutoff is not None and witness is not None:
        _compare_rational(cutoff, witness, f"{where}.cutoffWitness")


def _validate_observation(
    value: Any,
    item_id: int,
    sweep_start: int,
    sweep_end: int,
    initial_levels: int,
    where: str,
    wire_format: int,
) -> int:
    _keys(
        value,
        {
            "itemID",
            "ordinal",
            "kind",
            "query",
            "coverage",
            "sourceComplete",
            "cutoff",
            "higherPriceWitness",
            "reasonCode",
            "rowCount",
            "rows",
        },
        where,
    )
    if _int(value["itemID"], f"{where}.itemID", minimum=1) != item_id:
        raise ScanFormatError(f"{where}.itemID mismatch")
    _int(value["ordinal"], f"{where}.ordinal", minimum=1)
    kind = _choice(value["kind"], {"commodity", "item", "unknown"}, f"{where}.kind")
    query = value["query"]
    if not isinstance(query, dict):
        raise ScanFormatError(f"{where}.query must be an object")
    _keys(
        query,
        {"kind"} if query.get("kind") == "unqueried" else {"kind", "startedAt", "finishedAt"},
        f"{where}.query",
    )
    if query["kind"] == "unqueried":
        query_is_unqueried = True
    elif query["kind"] == "queried":
        query_is_unqueried = False
        started = _validate_timestamp(query["startedAt"], f"{where}.query.startedAt")
        finished = _validate_timestamp(query["finishedAt"], f"{where}.query.finishedAt")
        if started > finished or not sweep_start <= started <= finished <= sweep_end:
            raise ScanFormatError(f"{where}.query window is outside sweep")
    else:
        raise ScanFormatError(f"{where}.query.kind invalid")
    coverage = _choice(value["coverage"], {"full", "bounded", "incomplete", "unqueried"}, f"{where}.coverage")
    _bool(value["sourceComplete"], f"{where}.sourceComplete")
    cutoff = value["cutoff"]
    if not isinstance(cutoff, dict):
        raise ScanFormatError(f"{where}.cutoff invalid")
    cutoff_kind = _choice(cutoff.get("kind"), {"unbounded", "price", "unknown"}, f"{where}.cutoff.kind")
    cutoff_ratio: tuple[int, int] | None = None
    if cutoff_kind == "price":
        _keys(cutoff, {"kind", "numerator", "denominator"}, f"{where}.cutoff")
        cutoff_ratio = _rational(
            {"numerator": cutoff["numerator"], "denominator": cutoff["denominator"]}, f"{where}.cutoff"
        )
    else:
        _keys(cutoff, {"kind"}, f"{where}.cutoff")
    witness = value["higherPriceWitness"]
    if not isinstance(witness, dict):
        raise ScanFormatError(f"{where}.higherPriceWitness invalid")
    witness_kind = _choice(witness.get("kind"), {"known", "unknown"}, f"{where}.higherPriceWitness.kind")
    witness_ratio: tuple[int, int] | None = None
    if witness_kind == "known":
        _keys(witness, {"kind", "numerator", "denominator"}, f"{where}.higherPriceWitness")
        witness_ratio = _rational(
            {"numerator": witness["numerator"], "denominator": witness["denominator"]},
            f"{where}.higherPriceWitness",
        )
    else:
        _keys(witness, {"kind"}, f"{where}.higherPriceWitness")
    reasons = {
        "ok",
        "metadata_unavailable",
        "query_timeout",
        "page_limit",
        "sweep_timeout",
        "ah_closed",
        "logout",
        "house_changed",
        "missing_item_link",
        "identity_mismatch",
        "unexpected_order",
        "numeric_range",
        "not_started",
    }
    reason = _choice(value["reasonCode"], reasons, f"{where}.reasonCode")
    row_count = _int(value["rowCount"], f"{where}.rowCount")
    rows = _native_keyed_collection(value["rows"], f"{where}.rows")
    value["rows"] = rows
    if row_count != len(rows):
        raise ScanFormatError(f"{where}.rows/count mismatch")
    if query_is_unqueried:
        if (
            coverage != "unqueried"
            or kind != "unknown"
            or row_count != 0
            or value["sourceComplete"]
            or cutoff["kind"] != "unknown"
            or witness["kind"] != "unknown"
            or reason not in {"not_started", "logout", "ah_closed", "house_changed", "sweep_timeout"}
        ):
            raise ScanFormatError(f"{where} unqueried invariants invalid")
        return 0
    if coverage == "unqueried" or reason == "not_started":
        raise ScanFormatError(f"{where} queried scope cannot be unqueried")
    if kind == "unknown" and coverage != "incomplete":
        raise ScanFormatError(f"{where} unknown kind requires incomplete coverage")
    seen_ids: set[int] = set()
    parsed_rows: list[dict[str, Any]] = []
    for row_key, row in rows.items():
        if not isinstance(row_key, str) or not re.fullmatch(r"row:[1-9]\d*", row_key):
            raise ScanFormatError(f"{where}.rows key invalid")
        _validate_row(row, item_id, kind, f"{where}.rows.{row_key}", wire_format)
        if row["resultID"] in seen_ids:
            raise ScanFormatError(f"{where} has duplicate resultID")
        seen_ids.add(row["resultID"])
        parsed_rows.append(row)
    if set(rows) != {f"row:{index}" for index in range(1, row_count + 1)}:
        raise ScanFormatError(f"{where}.rows keys must be contiguous")
    successful = coverage in {"full", "bounded"}
    levels: list[tuple[int, int]] = []
    if successful:
        # Successful observations must prove exact ordering and depth. This
        # complete preflight is deliberately before sorting so an unsafe
        # non-adjacent cross product cannot become a Lua/Python sort failure.
        _validate_pairwise_prices(
            parsed_rows,
            where,
            cutoff=cutoff_ratio if coverage == "bounded" else None,
            witness=witness_ratio if coverage == "bounded" else None,
        )
        ordered_ratios = [
            _rational(rows[f"row:{index}"]["unitPrice"], f"{where}.row.unitPrice")
            for index in range(1, row_count + 1)
        ]
        for left, right in zip(ordered_ratios, ordered_ratios[1:], strict=False):
            if _compare_rational(left, right, f"{where}.rowOrder") > 0:
                raise ScanFormatError(f"{where}.rows must be sorted by exact unit price")
        for ratio in ordered_ratios:
            if not levels or _compare_rational(levels[-1], ratio, f"{where}.levels") != 0:
                levels.append(ratio)
        if len(levels) > initial_levels:
            raise ScanFormatError(f"{where} retains more than the plan depth")
    if coverage == "full":
        if (
            cutoff["kind"] != "unbounded"
            or witness["kind"] != "unknown"
            or not value["sourceComplete"]
            or reason != "ok"
        ):
            raise ScanFormatError(f"{where} full coverage requires unbounded source-complete evidence")
    elif coverage == "bounded":
        if (
            reason != "ok"
            or len(levels) != initial_levels
            or cutoff_ratio is None
            or witness_ratio is None
            or _compare_rational(witness_ratio, cutoff_ratio, f"{where}.cutoff") <= 0
        ):
            raise ScanFormatError(f"{where} bounded coverage requires a higher witness")
        if kind == "item" and not value["sourceComplete"]:
            raise ScanFormatError(f"{where} bounded item coverage requires sourceComplete")
        if not parsed_rows or _compare_rational(levels[-1], cutoff_ratio, f"{where}.cutoff") != 0:
            raise ScanFormatError(f"{where} cutoff must equal the retained boundary")
    elif coverage == "incomplete":
        if reason == "ok" or cutoff["kind"] != "unknown" or value["sourceComplete"]:
            raise ScanFormatError(f"{where} incomplete coverage requires an error and unknown cutoff")
    else:
        raise ScanFormatError(f"{where} invalid coverage")
    return row_count


def sweep_item_ids(plan: dict[str, Any], cold_slice: int | None) -> list[int]:
    """Items one sweep attempts, in plan order: every hot item, then cold slice ``cold_slice``.

    Cold item i of ``pools.cold`` belongs to slice ``i % coldSlices``.  Without cold items
    (``cold_slice is None``) this is the whole plan.
    """
    cold_slices = plan.get("coldSlices", 0)
    if cold_slices == 0:
        if cold_slice is not None:
            raise ScanFormatError("a plan without cold items has no cold slice")
        return list(plan["itemIDs"])
    if cold_slice is None or not 0 <= cold_slice < cold_slices:
        raise ScanFormatError("cold slice index is outside the plan's coldSlices")
    hot = set(plan["pools"]["core"]) | set(plan["pools"]["discovery"])
    chosen = {item for i, item in enumerate(plan["pools"]["cold"]) if i % cold_slices == cold_slice}
    return [item_id for item_id in plan["itemIDs"] if item_id in hot or item_id in chosen]


def _validate_cold_slice(present: bool, value: Any, plan: dict[str, Any]) -> int | None:
    """Return the sweep's cold slice index, or None for a plan without cold items.

    The addon rotates slices with a persisted counter, so the index carries no information the
    server can derive; it only has to be in range and agree with the attempted item set.
    """
    cold_slices = plan.get("coldSlices", 0)
    if cold_slices == 0:
        if present:
            raise ScanFormatError("envelope.coldSlice is only valid for a plan with cold slices")
        return None
    if not present:
        raise ScanFormatError("envelope.coldSlice is required when the plan has cold slices")
    _keys(value, {"index", "count"}, "envelope.coldSlice")
    if _int(value["count"], "envelope.coldSlice.count") != cold_slices:
        raise ScanFormatError("envelope.coldSlice.count does not match plan.coldSlices")
    index = _int(value["index"], "envelope.coldSlice.index")
    if index >= cold_slices:
        raise ScanFormatError("envelope.coldSlice.index must be below the slice count")
    return index


def _validate_owned(value: Any, sweep_start: int, sweep_end: int) -> None:
    _keys(value, {"status", "query", "reasonCode", "rowCount", "rows"}, "owned")
    status = _choice(value["status"], {"full", "incomplete", "unqueried"}, "owned.status")
    query = value["query"]
    if not isinstance(query, dict):
        raise ScanFormatError("owned.query must be an object")
    if query.get("kind") == "unqueried":
        _keys(query, {"kind"}, "owned.query")
        if status != "unqueried":
            raise ScanFormatError("owned unqueried query requires unqueried status")
    elif query.get("kind") == "queried":
        if status == "unqueried":
            raise ScanFormatError("owned unqueried status requires an unqueried query")
        _keys(query, {"kind", "startedAt", "finishedAt"}, "owned.query")
        start = _validate_timestamp(query["startedAt"], "owned.query.startedAt")
        end = _validate_timestamp(query["finishedAt"], "owned.query.finishedAt")
        if start > end or not sweep_start <= start <= end <= sweep_end:
            raise ScanFormatError("owned query outside sweep")
    else:
        raise ScanFormatError("owned.query.kind invalid")
    reason = _choice(
        value["reasonCode"],
        {"ok", "query_timeout", "ah_closed", "logout", "house_changed", "sweep_timeout", "not_started"},
        "owned.reasonCode",
    )
    if status == "unqueried" and reason != "not_started":
        raise ScanFormatError("owned unqueried status requires not_started reason")
    if status == "full" and (query["kind"] != "queried" or reason != "ok"):
        raise ScanFormatError("owned full status requires a successful queried batch")
    if status == "incomplete" and (query["kind"] != "queried" or reason == "ok"):
        raise ScanFormatError("owned incomplete status requires a failed queried batch")
    row_count = _int(value["rowCount"], "owned.rowCount")
    rows = _native_keyed_collection(value["rows"], "owned.rows")
    value["rows"] = rows
    if row_count != len(rows):
        raise ScanFormatError("owned rows/count mismatch")
    if status == "unqueried" and rows:
        raise ScanFormatError("owned unqueried batch cannot contain rows")
    seen: set[int] = set()
    for key, row in rows.items():
        if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
            raise ScanFormatError("owned row key must be decimal")
        _keys(
            row,
            {"auctionID", "itemID", "itemKey", "rawItemKey", "itemLink", "quantity", "buyout", "status"},
            f"owned.rows.{key}",
        )
        auction_id = _int(row["auctionID"], f"owned.rows.{key}.auctionID", minimum=1)
        if auction_id != int(key) or auction_id in seen:
            raise ScanFormatError("owned auction ID mismatch or duplicate")
        seen.add(auction_id)
        item_id = _int(row["itemID"], f"owned.rows.{key}.itemID", minimum=1)
        raw = _validate_raw_key(row["rawItemKey"], item_id, f"owned.rows.{key}.rawItemKey")
        if not isinstance(row["itemKey"], str) or row["itemKey"] != _canonical_item_key(item_id, raw):
            raise ScanFormatError(f"owned.rows.{key}.itemKey does not match raw item identity")
        _validate_item_link(row["itemLink"], item_id, raw, f"owned.rows.{key}.itemLink")
        _int(row["quantity"], f"owned.rows.{key}.quantity", minimum=1)
        buyout = row["buyout"]
        if not isinstance(buyout, dict):
            raise ScanFormatError("owned buyout invalid")
        buyout_kind = _choice(buyout.get("kind"), {"known", "unknown"}, f"owned.rows.{key}.buyout.kind")
        if buyout_kind == "known":
            _keys(buyout, {"kind", "copper"}, f"owned.rows.{key}.buyout")
            _int(buyout["copper"], f"owned.rows.{key}.buyout.copper", minimum=1)
        else:
            _keys(buyout, {"kind"}, f"owned.rows.{key}.buyout")
        _choice(row["status"], {"active", "sold"}, f"owned.rows.{key}.status")


def validate_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a supported envelope with its decoded payload."""
    # coldSlice is the one optional envelope key; it is validated against the plan below.
    cold_slice_present = isinstance(raw, dict) and "coldSlice" in raw
    cold_slice_raw = raw["coldSlice"] if cold_slice_present else None
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "coldSlice"}
    _keys(
        raw,
        {
            "id",
            "format",
            "startedAt",
            "finishedAt",
            "house",
            "context",
            "numRows",
            "numObservations",
            "payload",
        },
        "envelope",
    )
    _string(raw["id"], "envelope.id")
    wire_format = raw["format"]
    if wire_format not in ACCEPTED_FORMATS:
        raise ScanFormatError("formats 2 and 3 are the only accepted upload formats")
    started = _validate_timestamp(raw["startedAt"], "envelope.startedAt")
    finished = _validate_timestamp(raw["finishedAt"], "envelope.finishedAt")
    if started > finished:
        raise ScanFormatError("envelope timestamps are unordered")
    _int(raw["numRows"], "envelope.numRows")
    _int(raw["numObservations"], "envelope.numObservations")
    house = raw["house"]
    _keys(house, {"region", "ruleset", "faction", "source", "key"}, "envelope.house")
    for key in ("region", "ruleset", "faction", "source", "key"):
        _string(house[key], f"envelope.house.{key}")
    context = raw["context"]
    _keys(
        context,
        {
            "character",
            "realm",
            "playerFaction",
            "zone",
            "subzone",
            "locale",
            "clientVersion",
            "clientBuild",
            "interface",
            "addonVersion",
        },
        "envelope.context",
    )
    for key in (
        "character",
        "realm",
        "playerFaction",
        "locale",
        "clientVersion",
        "clientBuild",
        "addonVersion",
    ):
        _string(context[key], f"envelope.context.{key}")
    for key in ("zone", "subzone"):
        _string(context[key], f"envelope.context.{key}", nonempty=False)
    _int(context["interface"], "envelope.context.interface", minimum=1)
    decoded = decode_json_payload(raw["payload"])
    _keys(decoded, {"planHash", "planJSON", "observations", "owned"}, "payload")
    plan = _validate_plan(decoded["planJSON"], decoded["planHash"])
    cold_index = _validate_cold_slice(cold_slice_present, cold_slice_raw, plan)
    # Attempted items are exactly the observation keys: an attempted item with zero listings
    # has an observation (coverage full/empty), an unattempted one has no key at all.
    swept = sweep_item_ids(plan, cold_index)
    observations = decoded["observations"]
    if not isinstance(observations, dict) or set(observations) != {str(item_id) for item_id in swept}:
        raise ScanFormatError("observations must contain exactly one entry per swept item")
    ordinals: list[int] = []
    rows_total = 0
    for key, observation in observations.items():
        if not isinstance(key, str) or not key.isdecimal() or int(key) not in swept:
            raise ScanFormatError("observation key is not a swept decimal item ID")
        rows_total += _validate_observation(
            observation,
            int(key),
            started,
            finished,
            plan["initialPriceLevels"],
            f"observations.{key}",
            wire_format,
        )
        ordinals.append(observation["ordinal"])
    if sorted(ordinals) != list(range(1, len(swept) + 1)):
        raise ScanFormatError("observation ordinals must partition the swept items")
    if raw["numRows"] != rows_total or raw["numObservations"] != len(swept):
        raise ScanFormatError("envelope counts do not match payload")
    _validate_owned(decoded["owned"], started, finished)
    validated = {**raw, "decoded": decoded, "plan": plan}
    if cold_index is not None:
        validated["coldSlice"] = cold_slice_raw
    return validated


def normalize_observation(format: int, kind: str, observation: dict[str, Any]) -> dict[str, Any]:
    """Return an immutable-semantic copy suitable for storage.

    Format 3 is already corrected on the wire.  Format 2 remains accepted for
    historical uploads, but item unit prices and the observed uint32-wrapped
    exact time-left values are translated before they enter the normalized
    observation tables.  The received decoded payload is never edited.
    """
    if format not in ACCEPTED_FORMATS:
        raise ScanFormatError("cannot normalize an unsupported scan format")
    if not isinstance(kind, str) or kind not in {"commodity", "item", "unknown"}:
        raise ScanFormatError("observation kind is invalid")
    if not isinstance(observation, dict):
        raise ScanFormatError("observation must be an object")

    import copy

    normalized = copy.deepcopy(observation)
    if format == 2 and kind == "item" and normalized.get("coverage") == "bounded":
        raise ScanFormatError("format-2 item bounded coverage cannot be normalized")

    rows = normalized.get("rows", {})
    if not isinstance(rows, dict):
        raise ScanFormatError("observation rows must be an object")
    for row in rows.values():
        if not isinstance(row, dict):
            raise ScanFormatError("observation row must be an object")
        time_left = row.get("timeLeft")
        if isinstance(time_left, dict) and time_left.get("kind") == "exact":
            seconds = time_left.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, int):
                raise ScanFormatError("exact time-left seconds must be an integer")
            if format == 2:
                if WRAPPED_TIME_LEFT_MIN <= seconds <= UINT32_MAX:
                    time_left["seconds"] = seconds - UINT32_MODULUS
                elif seconds > MAX_EXACT_TIME_LEFT_SECONDS:
                    raise ScanFormatError("exact time-left seconds are outside the understood range")
            elif not -300 <= seconds <= MAX_EXACT_TIME_LEFT_SECONDS:
                raise ScanFormatError("format-3 exact time-left seconds are outside the valid range")

        if format == 2 and kind == "item":
            price = row.get("price")
            if (
                not isinstance(price, dict)
                or isinstance(price.get("copper"), bool)
                or not isinstance(price.get("copper"), int)
            ):
                raise ScanFormatError("item row price copper is invalid")
            row["unitPrice"] = {"numerator": price["copper"], "denominator": 1}
    return normalized


# Compatibility names remain aliases for historical callers and archives.
validate_v2_envelope = validate_envelope
parse_v2_envelope = validate_envelope
