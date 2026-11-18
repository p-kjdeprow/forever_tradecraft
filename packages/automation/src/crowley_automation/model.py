"""Immutable strict JSON contract for the six bounded local operation kinds.

The public JSON uses camelCase field names. Batch identity is SHA256 of sorted,
minified UTF-8 JSON, and item identity uses crowley-common's canonical validator.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast

from crowley_common.house import House
from crowley_common.scanformat import ScanFormatError
from crowley_common.scanformat import validate_item_key as _validate_shared_item_key

# Forever short/medium/long durations; native API indices are 1/2/3.
DEFAULT_LISTING_DURATION_HOURS = 2
LISTING_DURATIONS_HOURS = (2, 8, 24)

MAX_BATCH_BYTES = 16_384
MAX_ACTIONS = 32
MAX_INTEGER = 9_007_199_254_740_991
MAX_QUANTITY = 200
MAX_ITEM_KEY_LENGTH = 128


class BatchError(ValueError):
    """The input does not satisfy the operation contract."""


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BatchError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def validate_item_key(value: object) -> str:
    if type(value) is not str or len(value) > MAX_ITEM_KEY_LENGTH:
        raise BatchError("itemKey must be a bounded canonical string")
    try:
        return _validate_shared_item_key(value)
    except ScanFormatError as exc:
        raise BatchError(f"itemKey is not canonical: {exc}") from exc


def _target_id(value: object, name: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 64 or not value.isascii():
        raise BatchError(f"{name} must be a configured-ID-shaped string")
    if (
        value[0] < "a"
        or value[0] > "z"
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in value)
    ):
        raise BatchError(f"{name} must be a configured-ID-shaped string")
    return value


@dataclass(frozen=True, slots=True)
class PostCommodity:
    item_key: str
    quantity: int
    unit_price_copper: int
    duration_hours: int
    max_deposit_copper: int

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        _integer(self.quantity, "quantity", minimum=1, maximum=MAX_QUANTITY)
        _integer(self.unit_price_copper, "unitPriceCopper", minimum=1)
        if self.unit_price_copper * self.quantity > MAX_INTEGER:
            raise BatchError("unitPriceCopper times quantity exceeds the safe-integer limit")
        _integer(self.duration_hours, "durationHours", minimum=2, maximum=24)
        if self.duration_hours not in LISTING_DURATIONS_HOURS:
            raise BatchError("durationHours must be 2, 8, or 24")
        _integer(self.max_deposit_copper, "maxDepositCopper")


@dataclass(frozen=True, slots=True)
class PostItem:
    item_key: str
    quantity: int
    unit_price_copper: int
    duration_hours: int
    max_deposit_copper: int

    def __post_init__(self) -> None:
        PostCommodity(
            self.item_key,
            self.quantity,
            self.unit_price_copper,
            self.duration_hours,
            self.max_deposit_copper,
        )


@dataclass(frozen=True, slots=True)
class CancelAuction:
    auction_id: int
    max_cancel_cost_copper: int

    def __post_init__(self) -> None:
        _integer(self.auction_id, "auctionId", minimum=1)
        _integer(self.max_cancel_cost_copper, "maxCancelCostCopper")


@dataclass(frozen=True, slots=True)
class CollectMail:
    message_limit: int

    def __post_init__(self) -> None:
        _integer(self.message_limit, "messageLimit", minimum=1, maximum=50)


@dataclass(frozen=True, slots=True)
class CollectReturn:
    item_key: str
    quantity: int
    mail_fingerprint: str

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        _integer(self.quantity, "quantity", minimum=1, maximum=MAX_INTEGER)
        if type(self.mail_fingerprint) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.mail_fingerprint):
            raise BatchError("mailFingerprint must be a full lowercase 64-hex selection token")


@dataclass(frozen=True, slots=True)
class Interact:
    target_id: str

    def __post_init__(self) -> None:
        _target_id(self.target_id, "targetId")


@dataclass(frozen=True, slots=True)
class MoveRoute:
    route_id: str

    def __post_init__(self) -> None:
        _target_id(self.route_id, "routeId")


type Action = PostCommodity | PostItem | CancelAuction | CollectMail | CollectReturn | Interact | MoveRoute
_ACTION_TYPES = (PostCommodity, PostItem, CancelAuction, CollectMail, CollectReturn, Interact, MoveRoute)


def action_object(action: Action) -> dict[str, object]:
    if isinstance(action, (PostCommodity, PostItem)):
        kind = "post_commodity" if isinstance(action, PostCommodity) else "post_item"
        return {
            "durationHours": action.duration_hours,
            "itemKey": action.item_key,
            "kind": kind,
            "maxDepositCopper": action.max_deposit_copper,
            "quantity": action.quantity,
            "unitPriceCopper": action.unit_price_copper,
        }
    if isinstance(action, CancelAuction):
        return {
            "auctionId": action.auction_id,
            "kind": "cancel_auction",
            "maxCancelCostCopper": action.max_cancel_cost_copper,
        }
    if isinstance(action, CollectMail):
        return {"kind": "collect_mail", "messageLimit": action.message_limit}
    if isinstance(action, CollectReturn):
        return {
            "kind": "collect_return",
            "itemKey": action.item_key,
            "mailFingerprint": action.mail_fingerprint,
            "quantity": action.quantity,
        }
    if isinstance(action, Interact):
        return {"kind": "interact", "targetId": action.target_id}
    if isinstance(action, MoveRoute):
        return {"kind": "move_route", "routeId": action.route_id}
    raise TypeError(f"unsupported action type: {type(action).__name__}")


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BatchError("value cannot be represented as canonical JSON") from exc


def _exact_keys(value: object, expected: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise BatchError(f"{name} must contain exactly these keys: {', '.join(sorted(expected))}")
    return value


def _parse_action(value: object) -> Action:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise BatchError("each action must be an object with a string kind")
    kind = value["kind"]
    if kind in ("post_commodity", "post_item"):
        fields = {"kind", "itemKey", "quantity", "unitPriceCopper", "durationHours", "maxDepositCopper"}
        row = _exact_keys({"durationHours": DEFAULT_LISTING_DURATION_HOURS, **value}, fields, kind)
        args = (
            row["itemKey"],
            row["quantity"],
            row["unitPriceCopper"],
            row["durationHours"],
            row["maxDepositCopper"],
        )
        cls = PostCommodity if kind == "post_commodity" else PostItem
        return cls(*args)  # type: ignore[arg-type]
    if kind == "cancel_auction":
        row = _exact_keys(value, {"kind", "auctionId", "maxCancelCostCopper"}, kind)
        return CancelAuction(row["auctionId"], row["maxCancelCostCopper"])  # type: ignore[arg-type]
    if kind == "collect_mail":
        row = _exact_keys(value, {"kind", "messageLimit"}, kind)
        return CollectMail(row["messageLimit"])  # type: ignore[arg-type]
    if kind == "collect_return":
        row = _exact_keys(value, {"kind", "itemKey", "quantity", "mailFingerprint"}, kind)
        return CollectReturn(
            cast(str, row["itemKey"]),
            cast(int, row["quantity"]),
            cast(str, row["mailFingerprint"]),
        )
    if kind == "interact":
        row = _exact_keys(value, {"kind", "targetId"}, kind)
        return Interact(row["targetId"])  # type: ignore[arg-type]
    if kind == "move_route":
        row = _exact_keys(value, {"kind", "routeId"}, kind)
        return MoveRoute(row["routeId"])  # type: ignore[arg-type]
    raise BatchError(f"unsupported action kind: {kind}")


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BatchError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    raise BatchError(f"non-integer JSON number is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class Batch:
    house: str
    character: str
    expires_at: int
    actions: tuple[Action, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BatchError("schemaVersion must be integer 1")
        if type(self.house) is not str:
            raise BatchError("house must use a canonical region-ruleset-faction identity")
        try:
            if House.parse(self.house).key != self.house:
                raise ValueError("non-canonical house key")
        except ValueError as exc:
            raise BatchError("house must use a canonical region-ruleset-faction identity") from exc
        if (
            type(self.character) is not str
            or not 1 <= len(self.character) <= 12
            or not self.character.isascii()
            or not self.character[0].isalpha()
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'-"
                for char in self.character
            )
        ):
            raise BatchError("character must be a valid bounded character name")
        _integer(self.expires_at, "expiresAt", minimum=1)
        if type(self.actions) is not tuple or not 1 <= len(self.actions) <= MAX_ACTIONS:
            raise BatchError(f"actions must be a tuple with 1..{MAX_ACTIONS} entries")
        if any(type(action) not in _ACTION_TYPES for action in self.actions):
            raise BatchError("actions contains an unsupported action")

    def object(self) -> dict[str, object]:
        return {
            "actions": [action_object(action) for action in self.actions],
            "character": self.character,
            "expiresAt": self.expires_at,
            "house": self.house,
            "schemaVersion": self.schema_version,
        }

    @property
    def canonical(self) -> str:
        return canonical_json(self.object())

    @property
    def batch_id(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, source: str | bytes) -> Batch:
        if type(source) not in (str, bytes):
            raise BatchError("batch input must be UTF-8 JSON text")
        try:
            if isinstance(source, str):
                raw = source.encode("utf-8")
            else:
                raw = source
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise BatchError("batch is not valid UTF-8 JSON") from exc
        if len(raw) > MAX_BATCH_BYTES:
            raise BatchError(f"batch exceeds {MAX_BATCH_BYTES} bytes")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_duplicate_rejecting_object,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            )
        except BatchError:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise BatchError("batch is not valid UTF-8 JSON") from exc
        root = _exact_keys(value, {"schemaVersion", "house", "character", "expiresAt", "actions"}, "batch")
        rows = root["actions"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_ACTIONS:
            raise BatchError(f"actions must be a list with 1..{MAX_ACTIONS} entries")
        return cls(
            house=root["house"],  # type: ignore[arg-type]
            character=root["character"],  # type: ignore[arg-type]
            expires_at=root["expiresAt"],  # type: ignore[arg-type]
            actions=tuple(_parse_action(action) for action in rows),
            schema_version=root["schemaVersion"],  # type: ignore[arg-type]
        )
