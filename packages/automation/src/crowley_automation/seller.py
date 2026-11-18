"""Strict seller policy and pure, evidence-bound decisions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from crowley_common.house import House

from .model import (
    DEFAULT_LISTING_DURATION_HOURS,
    LISTING_DURATIONS_HOURS,
    MAX_INTEGER,
    MAX_QUANTITY,
    Action,
    BatchError,
    CancelAuction,
    PostCommodity,
    action_object,
    canonical_json,
    validate_item_key,
)

MAX_SELLER_POLICY_BYTES = 32_768
MAX_SELLER_ITEMS = 32
SELLER_INTERVAL_SECONDS = 60
SELLER_DECISION_MAX_AGE_SECONDS = 60
SELLER_PHASE_MAX_AGE_SECONDS = 300
MAX_SELLER_CANCELS_PER_CHILD = 8

SELLER_POLICY_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schemaVersion",
        "house",
        "character",
        "clientBuild",
        "expiresAt",
        "intervalSeconds",
        "items",
        "maxTotalSpendCopper",
        "maxRecycleRounds",
        "ahTargetId",
        "mailboxTargetId",
        "outboundRouteId",
        "returnRouteId",
    ],
    "properties": {
        "schemaVersion": {"const": 1},
        "house": {
            "type": "string",
            "pattern": "^(us|eu|kr|tw|cn|beta)-(normal|pvp|rp|hardcore)-(alliance|horde|neutral)$",
        },
        "character": {"type": "string", "minLength": 1, "maxLength": 12},
        "clientBuild": {"type": "string", "minLength": 1, "maxLength": 64},
        "expiresAt": {"type": "integer", "minimum": 1, "maximum": MAX_INTEGER},
        "intervalSeconds": {"const": SELLER_INTERVAL_SECONDS},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SELLER_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "itemKey",
                    "quantity",
                    "batchSize",
                    "minUnitPriceCopper",
                    "startUnitPriceCopper",
                    "maxDepositCopper",
                    "maxCancelCostCopper",
                    "kind",
                ],
                "properties": {
                    "itemKey": {"type": "string", "pattern": "^[1-9][0-9]*$"},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": MAX_INTEGER},
                    "batchSize": {"type": "integer", "minimum": 1, "maximum": MAX_QUANTITY},
                    "minUnitPriceCopper": {"type": "integer", "minimum": 1, "maximum": MAX_INTEGER},
                    "startUnitPriceCopper": {
                        "oneOf": [
                            {"type": "integer", "minimum": 1, "maximum": MAX_INTEGER},
                            {"type": "null"},
                        ]
                    },
                    "durationHours": {
                        "enum": list(LISTING_DURATIONS_HOURS),
                        "default": DEFAULT_LISTING_DURATION_HOURS,
                    },
                    "maxDepositCopper": {"type": "integer", "minimum": 0, "maximum": MAX_INTEGER},
                    "maxCancelCostCopper": {"type": "integer", "minimum": 0, "maximum": MAX_INTEGER},
                    "kind": {"const": "commodity"},
                },
            },
        },
        "maxTotalSpendCopper": {"type": "integer", "minimum": 1, "maximum": MAX_INTEGER},
        "maxRecycleRounds": {"type": "integer", "minimum": 0, "maximum": MAX_INTEGER},
        "ahTargetId": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
        "mailboxTargetId": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
        "outboundRouteId": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
        "returnRouteId": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
    },
}

SELLER_POLICY_EXAMPLE: dict[str, object] = {
    "schemaVersion": 1,
    "house": "us-normal-horde",
    "character": "Tester",
    "clientBuild": "70938",
    "expiresAt": 1_800_000_000,
    "intervalSeconds": 60,
    "items": [
        {
            "itemKey": "123",
            "quantity": 200,
            "batchSize": 20,
            "minUnitPriceCopper": 100,
            "startUnitPriceCopper": 150,
            "durationHours": DEFAULT_LISTING_DURATION_HOURS,
            "maxDepositCopper": 50,
            "maxCancelCostCopper": 10,
            "kind": "commodity",
        }
    ],
    "maxTotalSpendCopper": 500,
    "maxRecycleRounds": 3,
    "ahTargetId": "auctioneer_main",
    "mailboxTargetId": "mailbox_main",
    "outboundRouteId": "ah_to_mail",
    "returnRouteId": "mail_to_ah",
}
DecisionKind = Literal["wait", "post", "recycle", "finish", "pause"]
Priority = Literal["ours", "competitor", "empty", "unknown"]


class SellerPolicyError(ValueError):
    """Seller policy JSON or facts violate the fixed seller contract."""


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SellerPolicyError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _text(value: object, name: str, *, maximum: int = 128) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or not value.isascii():
        raise SellerPolicyError(f"{name} must be bounded non-empty ASCII text")
    return value


def _target(value: object, name: str) -> str:
    text = _text(value, name, maximum=64)
    if (
        text[0] < "a"
        or text[0] > "z"
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in text)
    ):
        raise SellerPolicyError(f"{name} must be a configured-ID-shaped string")
    return text


def _exact_keys(value: object, keys: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise SellerPolicyError(f"{name} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SellerPolicyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    raise SellerPolicyError(f"non-integer JSON number is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class SellerItemPolicy:
    item_key: str
    quantity: int
    batch_size: int
    min_unit_price_copper: int
    start_unit_price_copper: int | None
    duration_hours: int
    max_deposit_copper: int
    max_cancel_cost_copper: int
    kind: Literal["commodity"] = "commodity"

    def __post_init__(self) -> None:
        try:
            validate_item_key(self.item_key)
        except BatchError as exc:
            raise SellerPolicyError(str(exc)) from exc
        if not self.item_key.isascii() or not self.item_key.isdecimal():
            raise SellerPolicyError("only exact base itemKey commodity identities are supported")
        _integer(self.quantity, "quantity", minimum=1)
        _integer(self.batch_size, "batchSize", minimum=1, maximum=MAX_QUANTITY)
        _integer(self.min_unit_price_copper, "minUnitPriceCopper", minimum=1)
        if self.start_unit_price_copper is not None:
            _integer(self.start_unit_price_copper, "startUnitPriceCopper", minimum=1)
            if self.start_unit_price_copper < self.min_unit_price_copper:
                raise SellerPolicyError("startUnitPriceCopper must not be below minUnitPriceCopper")
        if type(self.duration_hours) is not int or self.duration_hours not in LISTING_DURATIONS_HOURS:
            raise SellerPolicyError("durationHours must be 2, 8, or 24")
        _integer(self.max_deposit_copper, "maxDepositCopper")
        _integer(self.max_cancel_cost_copper, "maxCancelCostCopper")
        if self.kind != "commodity":
            raise SellerPolicyError("only commodity item policies are supported")

    def object(self) -> dict[str, object]:
        return {
            "batchSize": self.batch_size,
            "durationHours": self.duration_hours,
            "itemKey": self.item_key,
            "kind": self.kind,
            "maxCancelCostCopper": self.max_cancel_cost_copper,
            "maxDepositCopper": self.max_deposit_copper,
            "minUnitPriceCopper": self.min_unit_price_copper,
            "quantity": self.quantity,
            "startUnitPriceCopper": self.start_unit_price_copper,
        }


@dataclass(frozen=True, slots=True)
class SellerPolicy:
    house: str
    character: str
    client_build: str
    expires_at: int
    interval_seconds: int
    items: tuple[SellerItemPolicy, ...]
    max_total_spend_copper: int
    max_recycle_rounds: int
    ah_target_id: str
    mailbox_target_id: str
    outbound_route_id: str
    return_route_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SellerPolicyError("schemaVersion must be integer 1")
        if type(self.house) is not str:
            raise SellerPolicyError("house must be a canonical house identity")
        try:
            if House.parse(self.house).key != self.house:
                raise ValueError("noncanonical house")
        except ValueError as exc:
            raise SellerPolicyError("house must be a canonical house identity") from exc
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
            raise SellerPolicyError("character must be a valid bounded character name")
        _text(self.client_build, "clientBuild", maximum=64)
        _integer(self.expires_at, "expiresAt", minimum=1)
        if type(self.interval_seconds) is not int or self.interval_seconds != SELLER_INTERVAL_SECONDS:
            raise SellerPolicyError("intervalSeconds is fixed at 60")
        if type(self.items) is not tuple or not 1 <= len(self.items) <= MAX_SELLER_ITEMS:
            raise SellerPolicyError(f"items must be a tuple with 1..{MAX_SELLER_ITEMS} entries")
        keys = [item.item_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise SellerPolicyError("itemKey values must be unique")
        if any(type(item) is not SellerItemPolicy for item in self.items):
            raise SellerPolicyError("items must contain SellerItemPolicy values")
        _integer(self.max_total_spend_copper, "maxTotalSpendCopper", minimum=1)
        _integer(self.max_recycle_rounds, "maxRecycleRounds")
        _target(self.ah_target_id, "ahTargetId")
        _target(self.mailbox_target_id, "mailboxTargetId")
        _target(self.outbound_route_id, "outboundRouteId")
        _target(self.return_route_id, "returnRouteId")

    def object(self) -> dict[str, object]:
        return {
            "ahTargetId": self.ah_target_id,
            "character": self.character,
            "clientBuild": self.client_build,
            "expiresAt": self.expires_at,
            "house": self.house,
            "intervalSeconds": self.interval_seconds,
            "items": [item.object() for item in self.items],
            "mailboxTargetId": self.mailbox_target_id,
            "maxRecycleRounds": self.max_recycle_rounds,
            "maxTotalSpendCopper": self.max_total_spend_copper,
            "outboundRouteId": self.outbound_route_id,
            "returnRouteId": self.return_route_id,
            "schemaVersion": self.schema_version,
        }

    @property
    def canonical(self) -> str:
        return canonical_json(self.object())

    @property
    def policy_id(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    def item(self, item_key: str) -> SellerItemPolicy:
        for item in self.items:
            if item.item_key == item_key:
                return item
        raise SellerPolicyError(f"item is not admitted by this policy: {item_key}")

    @classmethod
    def from_json(cls, source: str | bytes) -> SellerPolicy:
        if type(source) not in (str, bytes):
            raise SellerPolicyError("seller policy input must be UTF-8 JSON text")
        try:
            raw = source.encode("utf-8") if isinstance(source, str) else source
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise SellerPolicyError("seller policy is not valid UTF-8 JSON") from exc
        if len(raw) > MAX_SELLER_POLICY_BYTES:
            raise SellerPolicyError(f"seller policy exceeds {MAX_SELLER_POLICY_BYTES} bytes")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_duplicate_rejecting_object,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            )
        except SellerPolicyError:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise SellerPolicyError("seller policy is not valid UTF-8 JSON") from exc
        root = _exact_keys(
            value,
            {
                "schemaVersion",
                "house",
                "character",
                "clientBuild",
                "expiresAt",
                "intervalSeconds",
                "items",
                "maxTotalSpendCopper",
                "maxRecycleRounds",
                "ahTargetId",
                "mailboxTargetId",
                "outboundRouteId",
                "returnRouteId",
            },
            "seller policy",
        )
        item_rows = root["items"]
        if type(item_rows) is not list or not 1 <= len(item_rows) <= MAX_SELLER_ITEMS:
            raise SellerPolicyError(f"items must be a list with 1..{MAX_SELLER_ITEMS} entries")
        item_fields = {
            "itemKey",
            "quantity",
            "batchSize",
            "minUnitPriceCopper",
            "startUnitPriceCopper",
            "durationHours",
            "maxDepositCopper",
            "maxCancelCostCopper",
            "kind",
        }
        items: list[SellerItemPolicy] = []
        for raw_item in item_rows:
            if type(raw_item) is not dict:
                raise SellerPolicyError("seller item must be an object")
            row = _exact_keys(
                {"durationHours": DEFAULT_LISTING_DURATION_HOURS, **raw_item}, item_fields, "seller item"
            )
            if row["kind"] != "commodity":
                raise SellerPolicyError("only commodity item policies are supported")
            items.append(
                SellerItemPolicy(
                    item_key=row["itemKey"],  # type: ignore[arg-type]
                    quantity=row["quantity"],  # type: ignore[arg-type]
                    batch_size=row["batchSize"],  # type: ignore[arg-type]
                    min_unit_price_copper=row["minUnitPriceCopper"],  # type: ignore[arg-type]
                    start_unit_price_copper=row["startUnitPriceCopper"],  # type: ignore[arg-type]
                    duration_hours=row["durationHours"],  # type: ignore[arg-type]
                    max_deposit_copper=row["maxDepositCopper"],  # type: ignore[arg-type]
                    max_cancel_cost_copper=row["maxCancelCostCopper"],  # type: ignore[arg-type]
                )
            )
        return cls(
            house=root["house"],  # type: ignore[arg-type]
            character=root["character"],  # type: ignore[arg-type]
            client_build=root["clientBuild"],  # type: ignore[arg-type]
            expires_at=root["expiresAt"],  # type: ignore[arg-type]
            interval_seconds=root["intervalSeconds"],  # type: ignore[arg-type]
            items=tuple(items),
            max_total_spend_copper=root["maxTotalSpendCopper"],  # type: ignore[arg-type]
            max_recycle_rounds=root["maxRecycleRounds"],  # type: ignore[arg-type]
            ah_target_id=root["ahTargetId"],  # type: ignore[arg-type]
            mailbox_target_id=root["mailboxTargetId"],  # type: ignore[arg-type]
            outbound_route_id=root["outboundRouteId"],  # type: ignore[arg-type]
            return_route_id=root["returnRouteId"],  # type: ignore[arg-type]
            schema_version=root["schemaVersion"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MarketFacts:
    item_key: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    complete: bool
    priority: Priority
    best_competing_unit_price_copper: int | None
    managed_unit_price_copper: int | None
    managed_active_quantity: int
    managed_auction_ids: tuple[int, ...] = ()
    managed_auctions: tuple[OwnedAuction, ...] = ()

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        if type(self.request_id) is not str or re.fullmatch(r"[0-9a-f]{32}", self.request_id) is None:
            raise SellerPolicyError("requestId must be a full lowercase 128-bit request token")
        _text(self.client_boot_id, "clientBootId")
        _integer(self.sequence, "sequence", minimum=1)
        _integer(self.observed_at, "observedAt", minimum=1)
        if type(self.complete) is not bool:
            raise SellerPolicyError("market complete must be boolean")
        if self.priority not in ("ours", "competitor", "empty", "unknown"):
            raise SellerPolicyError("market priority is not a supported exact value")
        for price, name in (
            (self.best_competing_unit_price_copper, "bestCompetingUnitPriceCopper"),
            (self.managed_unit_price_copper, "managedUnitPriceCopper"),
        ):
            if price is not None:
                _integer(price, name, minimum=1)
        if self.priority == "competitor" and self.best_competing_unit_price_copper is None:
            raise SellerPolicyError("competitor priority requires an exact competing price")
        _integer(self.managed_active_quantity, "managedActiveQuantity")
        if type(self.managed_auction_ids) is not tuple:
            raise SellerPolicyError("managedAuctionIds must be a tuple")
        for auction_id in self.managed_auction_ids:
            _integer(auction_id, "auctionId", minimum=1)
        if self.managed_auction_ids != tuple(sorted(set(self.managed_auction_ids))):
            raise SellerPolicyError("managedAuctionIds must be sorted and unique")
        if type(self.managed_auctions) is not tuple or any(
            type(row) is not OwnedAuction for row in self.managed_auctions
        ):
            raise SellerPolicyError("managedAuctions must be an immutable tuple of exact owned facts")
        managed_rows = [row for row in self.managed_auctions if row.status == "active"]
        if any(row.item_key != self.item_key for row in managed_rows):
            raise SellerPolicyError("managedAuctions contain another exact item identity")
        row_ids = tuple(row.auction_id for row in managed_rows)
        if row_ids != tuple(sorted(set(row_ids))):
            raise SellerPolicyError("managedAuctions must be sorted and unique")
        if self.managed_auction_ids and self.managed_auction_ids != row_ids:
            raise SellerPolicyError("managedAuctionIds disagree with exact managed auction rows")
        if (
            managed_rows
            and sum(row.remaining_quantity for row in managed_rows) != self.managed_active_quantity
        ):
            raise SellerPolicyError("managed auction rows disagree with managed active quantity")

    def object(self) -> dict[str, object]:
        managed_ids = self.managed_auction_ids or tuple(
            row.auction_id for row in self.managed_auctions if row.status == "active"
        )
        return {
            "bestCompetingUnitPriceCopper": self.best_competing_unit_price_copper,
            "clientBootId": self.client_boot_id,
            "complete": self.complete,
            "itemKey": self.item_key,
            "managedActiveQuantity": self.managed_active_quantity,
            "managedAuctionIds": list(managed_ids),
            "managedAuctions": [row.object() for row in self.managed_auctions],
            "managedUnitPriceCopper": self.managed_unit_price_copper,
            "observedAt": self.observed_at,
            "priority": self.priority,
            "requestId": self.request_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class StockFacts:
    item_key: str
    admitted_quantity: int
    bag_quantity: int
    managed_active_quantity: int
    returning_quantity: int
    unresolved_quantity: int
    sold_quantity: int
    inventory_request_id: str | None = None
    inventory_client_boot_id: str | None = None
    inventory_sequence: int | None = None
    inventory_observed_at: int | None = None
    owned_request_id: str | None = None
    owned_client_boot_id: str | None = None
    owned_sequence: int | None = None
    owned_observed_at: int | None = None
    last_post_attempt_at: int | None = None
    last_posted_unit_price_copper: int | None = None
    recycling: bool = False

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        for value, name in (
            (self.admitted_quantity, "admittedQuantity"),
            (self.bag_quantity, "bagQuantity"),
            (self.managed_active_quantity, "managedActiveQuantity"),
            (self.returning_quantity, "returningQuantity"),
            (self.unresolved_quantity, "unresolvedQuantity"),
            (self.sold_quantity, "soldQuantity"),
        ):
            _integer(value, name)
        if (
            self.bag_quantity
            + self.managed_active_quantity
            + self.returning_quantity
            + self.unresolved_quantity
            + self.sold_quantity
            != self.admitted_quantity
        ):
            raise SellerPolicyError("stock quantities do not conserve the admitted quantity")
        inventory = (
            self.inventory_request_id,
            self.inventory_client_boot_id,
            self.inventory_sequence,
            self.inventory_observed_at,
        )
        owned = (
            self.owned_request_id,
            self.owned_client_boot_id,
            self.owned_sequence,
            self.owned_observed_at,
        )
        if any(value is not None for value in (*inventory, *owned)):
            if any(value is None for value in (*inventory, *owned)):
                raise SellerPolicyError("stock snapshot provenance must include inventory and owned evidence")
            _text(self.inventory_request_id, "stockInventoryRequestId")
            _text(self.inventory_client_boot_id, "stockInventoryBootId")
            _integer(self.inventory_sequence, "stockInventorySequence", minimum=1)
            _integer(self.inventory_observed_at, "stockInventoryObservedAt", minimum=1)
            _text(self.owned_request_id, "stockOwnedRequestId")
            _text(self.owned_client_boot_id, "stockOwnedBootId")
            _integer(self.owned_sequence, "stockOwnedSequence", minimum=1)
            _integer(self.owned_observed_at, "stockOwnedObservedAt", minimum=1)
        if self.last_post_attempt_at is not None:
            _integer(self.last_post_attempt_at, "lastPostAttemptAt", minimum=1)
        if self.last_posted_unit_price_copper is not None:
            _integer(self.last_posted_unit_price_copper, "lastPostedUnitPriceCopper", minimum=1)
        if type(self.recycling) is not bool:
            raise SellerPolicyError("recycling state must be boolean")

    def object(self) -> dict[str, object]:
        return {
            "admittedQuantity": self.admitted_quantity,
            "bagQuantity": self.bag_quantity,
            "itemKey": self.item_key,
            "managedActiveQuantity": self.managed_active_quantity,
            "inventoryClientBootId": self.inventory_client_boot_id,
            "inventoryObservedAt": self.inventory_observed_at,
            "inventoryRequestId": self.inventory_request_id,
            "inventorySequence": self.inventory_sequence,
            "lastPostAttemptAt": self.last_post_attempt_at,
            "lastPostedUnitPriceCopper": self.last_posted_unit_price_copper,
            "ownedClientBootId": self.owned_client_boot_id,
            "ownedObservedAt": self.owned_observed_at,
            "ownedRequestId": self.owned_request_id,
            "ownedSequence": self.owned_sequence,
            "returningQuantity": self.returning_quantity,
            "recycling": self.recycling,
            "soldQuantity": self.sold_quantity,
            "unresolvedQuantity": self.unresolved_quantity,
        }


@dataclass(frozen=True, slots=True)
class InventoryFacts:
    item_key: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    complete: bool
    bag_quantity: int

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        _text(self.request_id, "inventoryRequestId")
        _text(self.client_boot_id, "inventoryClientBootId")
        _integer(self.sequence, "inventorySequence", minimum=1)
        _integer(self.observed_at, "inventoryObservedAt", minimum=1)
        if type(self.complete) is not bool:
            raise SellerPolicyError("inventory complete must be boolean")
        _integer(self.bag_quantity, "bagQuantity")

    def object(self) -> dict[str, object]:
        return {
            "bagQuantity": self.bag_quantity,
            "clientBootId": self.client_boot_id,
            "complete": self.complete,
            "itemKey": self.item_key,
            "observedAt": self.observed_at,
            "requestId": self.request_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class OwnedAuction:
    auction_id: int
    item_key: str
    remaining_quantity: int
    unit_price_copper: int
    status: Literal["active", "sold"]
    cancel_allowed: bool
    cancel_cost_known: bool
    cancel_cost_copper: int

    def __post_init__(self) -> None:
        _integer(self.auction_id, "auctionId", minimum=1)
        validate_item_key(self.item_key)
        _integer(self.remaining_quantity, "remainingQuantity")
        if self.status == "active" and self.remaining_quantity < 1:
            raise SellerPolicyError("active owned auction must have positive remaining quantity")
        _integer(self.unit_price_copper, "unitPriceCopper", minimum=1)
        if self.status not in ("active", "sold"):
            raise SellerPolicyError("owned auction status must be active or sold")
        if type(self.cancel_allowed) is not bool or type(self.cancel_cost_known) is not bool:
            raise SellerPolicyError("owned auction cancellation evidence must be boolean")
        _integer(self.cancel_cost_copper, "cancelCostCopper")
        if self.cancel_cost_known is False and self.cancel_cost_copper != 0:
            raise SellerPolicyError("unknown cancellation cost must be represented as zero")

    def object(self) -> dict[str, object]:
        return {
            "auctionId": self.auction_id,
            "cancelAllowed": self.cancel_allowed,
            "cancelCostCopper": self.cancel_cost_copper,
            "cancelCostKnown": self.cancel_cost_known,
            "itemKey": self.item_key,
            "remainingQuantity": self.remaining_quantity,
            "status": self.status,
            "unitPriceCopper": self.unit_price_copper,
        }


@dataclass(frozen=True, slots=True)
class OwnedFacts:
    item_key: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    complete: bool
    auctions: tuple[OwnedAuction, ...]

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        _text(self.request_id, "ownedRequestId")
        _text(self.client_boot_id, "ownedClientBootId")
        _integer(self.sequence, "ownedSequence", minimum=1)
        _integer(self.observed_at, "ownedObservedAt", minimum=1)
        if type(self.complete) is not bool:
            raise SellerPolicyError("owned complete must be boolean")
        if type(self.auctions) is not tuple or any(type(row) is not OwnedAuction for row in self.auctions):
            raise SellerPolicyError("owned auctions must be an immutable tuple")
        ids = [row.auction_id for row in self.auctions]
        if ids != sorted(set(ids)):
            raise SellerPolicyError("owned auction IDs must be sorted and unique")
        if any(row.item_key != self.item_key for row in self.auctions):
            raise SellerPolicyError("owned auction facts contain another item identity")

    def object(self) -> dict[str, object]:
        return {
            "auctions": [row.object() for row in self.auctions],
            "clientBootId": self.client_boot_id,
            "complete": self.complete,
            "itemKey": self.item_key,
            "observedAt": self.observed_at,
            "requestId": self.request_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class ReturnCandidate:
    item_key: str | None
    quantity: int | None
    mail_fingerprint: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    complete: bool
    eligible: bool
    reason: str

    def __post_init__(self) -> None:
        if self.item_key is not None:
            validate_item_key(self.item_key)
        if self.quantity is not None:
            _integer(self.quantity, "returnQuantity", minimum=1, maximum=MAX_INTEGER)
        if self.eligible and (self.item_key is None or self.quantity is None):
            raise SellerPolicyError("eligible return candidate requires exact item identity and quantity")
        if type(self.mail_fingerprint) is not str or len(self.mail_fingerprint) > 64:
            raise SellerPolicyError("mailFingerprint must be a bounded native selection token")
        if self.eligible and re.fullmatch(r"[0-9a-f]{64}", self.mail_fingerprint) is None:
            raise SellerPolicyError("eligible return requires a full lowercase 64-hex selection token")
        if self.mail_fingerprint and re.fullmatch(r"[0-9a-f]{64}", self.mail_fingerprint) is None:
            raise SellerPolicyError("non-empty selection tokens must be lowercase 64-hex text")
        _text(self.request_id, "returnRequestId")
        _text(self.client_boot_id, "returnClientBootId")
        _integer(self.sequence, "returnSequence", minimum=1)
        _integer(self.observed_at, "returnObservedAt", minimum=1)
        if type(self.complete) is not bool:
            raise SellerPolicyError("return observation complete must be boolean")
        if type(self.eligible) is not bool or type(self.reason) is not str or len(self.reason) > 240:
            raise SellerPolicyError("return eligibility and reason are malformed")

    def object(self) -> dict[str, object]:
        return {
            "clientBootId": self.client_boot_id,
            "complete": self.complete,
            "eligible": self.eligible,
            "itemKey": self.item_key,
            "mailFingerprint": self.mail_fingerprint,
            "observedAt": self.observed_at,
            "quantity": self.quantity,
            "reason": self.reason,
            "requestId": self.request_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class ReturnObservationFacts:
    item_key: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    complete: bool
    candidates: tuple[ReturnCandidate, ...]

    def __post_init__(self) -> None:
        validate_item_key(self.item_key)
        _text(self.request_id, "returnsRequestId")
        _text(self.client_boot_id, "returnsClientBootId")
        _integer(self.sequence, "returnsSequence", minimum=1)
        _integer(self.observed_at, "returnsObservedAt", minimum=1)
        if type(self.complete) is not bool or type(self.candidates) is not tuple:
            raise SellerPolicyError("returns observation completeness and candidates are malformed")
        for candidate in self.candidates:
            if type(candidate) is not ReturnCandidate or (
                candidate.request_id,
                candidate.client_boot_id,
                candidate.sequence,
                candidate.observed_at,
                candidate.complete,
            ) != (
                self.request_id,
                self.client_boot_id,
                self.sequence,
                self.observed_at,
                self.complete,
            ):
                raise SellerPolicyError("return candidate provenance differs from its observation envelope")

    def object(self) -> dict[str, object]:
        return {
            "candidates": [candidate.object() for candidate in self.candidates],
            "clientBootId": self.client_boot_id,
            "complete": self.complete,
            "itemKey": self.item_key,
            "observedAt": self.observed_at,
            "requestId": self.request_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class ManagedAuction:
    auction_id: int
    item_key: str
    remaining_quantity: int
    unit_price_copper: int
    active: bool
    created_batch_id: str


@dataclass(frozen=True, slots=True)
class SellerItemProgress:
    session_id: str
    item_key: str
    admitted_quantity: int
    bag_quantity: int
    managed_active_quantity: int
    returning_quantity: int
    unresolved_quantity: int
    sold_quantity: int
    recycle_rounds: int
    recycling: bool
    finished: bool
    reserved_spend_copper: int
    spent_copper: int
    recycle_anchor_decision_id: str | None
    last_post_attempt_at: int | None


@dataclass(frozen=True, slots=True)
class SellerSessionStatus:
    session_id: str
    policy_id: str
    boot_id: str
    client_build: str
    expires_at: int
    state: Literal["active", "completed", "revoked", "expired", "stopped"]
    stop_reason: str | None
    created_at: int
    resume_location: Literal["ah", "mailbox"] | None = None


@dataclass(frozen=True, slots=True)
class SellerDecision:
    decision_id: str
    kind: DecisionKind
    item_key: str
    quantity: int
    unit_price_copper: int | None
    reason: str
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int
    evidence_digest: str
    actions: tuple[Action, ...]
    remaining_auction_ids: tuple[int, ...] = ()

    def object(self) -> dict[str, object]:
        return {
            "actions": [action_object(action) for action in self.actions],
            "clientBootId": self.client_boot_id,
            "decisionId": self.decision_id,
            "evidenceDigest": self.evidence_digest,
            "itemKey": self.item_key,
            "kind": self.kind,
            "observedAt": self.observed_at,
            "quantity": self.quantity,
            "reason": self.reason,
            "remainingAuctionIds": list(self.remaining_auction_ids),
            "requestId": self.request_id,
            "sequence": self.sequence,
            "unitPriceCopper": self.unit_price_copper,
        }


def decide(
    policy: SellerPolicy,
    item: SellerItemPolicy,
    market: MarketFacts,
    stock: StockFacts,
    now: int,
) -> SellerDecision:
    """Choose one bounded policy action from an exact fresh snapshot; never performs I/O."""
    _integer(now, "now", minimum=1)
    if (
        policy.item(item.item_key) != item
        or market.item_key != item.item_key
        or stock.item_key != item.item_key
    ):
        raise SellerPolicyError("decision facts do not identify the selected policy item")
    if stock.admitted_quantity != item.quantity:
        raise SellerPolicyError("observed stock does not match the policy admission cap")
    if market.managed_active_quantity != stock.managed_active_quantity:
        raise SellerPolicyError("market and stock facts disagree about managed active quantity")
    evidence = {
        "market": market.object(),
        "policyId": policy.policy_id,
        "stock": stock.object(),
    }
    evidence_digest = hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()
    kind: DecisionKind = "pause"
    quantity = 0
    price: int | None = None
    reason = "market or ownership evidence is not safe for a decision"
    actions: tuple[Action, ...] = ()
    remaining_ids: tuple[int, ...] = ()
    if (
        stock.bag_quantity == 0
        and stock.managed_active_quantity == 0
        and stock.returning_quantity == 0
        and stock.unresolved_quantity == 0
    ):
        kind, reason = "finish", "no admitted bag, managed, or returning stock remains"
    elif not market.complete:
        reason = "targeted market result is incomplete"
    elif now < market.observed_at or now - market.observed_at > SELLER_DECISION_MAX_AGE_SECONDS:
        reason = "targeted market result is stale"
    elif stock.unresolved_quantity > 0 and stock.bag_quantity == 0 and stock.managed_active_quantity == 0:
        reason = "stock conservation includes unresolved quantity"
    elif stock.returning_quantity > 0 and stock.managed_active_quantity == 0 and stock.bag_quantity == 0:
        kind, reason = "wait", "managed returns are still outstanding"
    elif stock.recycling:
        if stock.managed_active_quantity == 0:
            kind, reason = "wait", "persisted recycle pass is resolving its return route"
        elif not market.managed_auctions:
            kind, reason = "pause", "persisted recycle pass has no exact active auction identity"
        elif any(
            row.status != "active"
            or not row.cancel_allowed
            or not row.cancel_cost_known
            or row.cancel_cost_copper > item.max_cancel_cost_copper
            for row in market.managed_auctions
        ):
            kind, reason = (
                "pause",
                "persisted recycle auction cancellation permission or cost is unknown or above its cap",
            )
        else:
            kind, reason = "recycle", "continue the persisted recycle pass on exact active auctions"
            auction_ids = tuple(row.auction_id for row in market.managed_auctions)
            cancel_ids = auction_ids[:MAX_SELLER_CANCELS_PER_CHILD]
            actions = tuple(
                CancelAuction(auction_id, item.max_cancel_cost_copper) for auction_id in cancel_ids
            )
            remaining_ids = auction_ids[MAX_SELLER_CANCELS_PER_CHILD:]
            quantity = sum(
                row.remaining_quantity for row in market.managed_auctions[:MAX_SELLER_CANCELS_PER_CHILD]
            )
    elif stock.bag_quantity > 0:
        if market.priority == "ours" and stock.managed_active_quantity > 0:
            kind, reason = "wait", "managed listing retains purchase priority"
        elif market.priority == "ours":
            reason = "own priority is not backed by a managed active auction"
        elif market.priority == "empty" and stock.managed_active_quantity > 0:
            reason = "empty result conflicts with managed active auction evidence"
        elif market.priority == "unknown" and market.best_competing_unit_price_copper is None:
            reason = "competing price is unknown while queue priority is unobserved"
        elif (
            market.priority == "unknown"
            and market.managed_unit_price_copper is not None
            and market.best_competing_unit_price_copper is not None
            and market.managed_unit_price_copper < market.best_competing_unit_price_copper
        ):
            kind, reason = "wait", "managed listing is strictly cheaper; queue priority unobserved"
        else:
            if market.priority in ("competitor", "unknown"):
                price = market.best_competing_unit_price_copper
                if market.priority == "unknown" and price == market.managed_unit_price_copper:
                    reason = "scheduled matched-price refresh; queue priority unobserved"
                elif market.priority == "unknown":
                    reason = "match the exact competing price; queue priority unobserved"
                else:
                    reason = "match the exact best competing unit price"
            else:
                price = (
                    market.managed_unit_price_copper
                    or stock.last_posted_unit_price_copper
                    or item.start_unit_price_copper
                )
                reason = "retain a known valid price or use the explicit starting price"
            if price is None:
                kind, reason = "pause", "empty market has no valid managed or starting price"
            elif price < item.min_unit_price_copper:
                kind, reason = "pause", "matching price is below the approved minimum"
            else:
                kind = "post"
                quantity = min(item.batch_size, stock.bag_quantity)
                if (
                    stock.last_post_attempt_at is not None
                    and now < stock.last_post_attempt_at + SELLER_INTERVAL_SECONDS
                ):
                    kind, quantity, price, reason = (
                        "wait",
                        0,
                        None,
                        "post attempt interval has not elapsed",
                    )
                else:
                    actions = (
                        PostCommodity(
                            item.item_key,
                            quantity,
                            price,
                            item.duration_hours,
                            item.max_deposit_copper,
                        ),
                    )
    elif stock.managed_active_quantity > 0:
        if market.priority == "ours":
            kind, reason = "wait", "managed listing retains purchase priority"
        elif market.priority in ("competitor", "unknown"):
            competing_price = market.best_competing_unit_price_copper
            managed_price = (
                market.managed_unit_price_copper
                or min(row.unit_price_copper for row in market.managed_auctions)
                if market.managed_auctions
                else market.managed_unit_price_copper
            )
            if competing_price is None:
                kind, reason = "pause", "competing price is unknown while queue priority is unresolved"
            elif competing_price < item.min_unit_price_copper:
                kind, reason = "pause", "matching price is below the approved minimum"
            elif (
                market.priority == "unknown" and managed_price is not None and managed_price < competing_price
            ):
                kind, reason = "wait", "managed listing is strictly cheaper; queue priority unobserved"
            elif not market.managed_auctions:
                kind, reason = "pause", "active managed quantity has no exact auction identity"
            elif any(
                row.status != "active"
                or not row.cancel_allowed
                or not row.cancel_cost_known
                or row.cancel_cost_copper > item.max_cancel_cost_copper
                for row in market.managed_auctions
            ):
                kind, reason = (
                    "pause",
                    "managed auction cancellation permission or cost is unknown or above its cap",
                )
            else:
                kind = "recycle"
                reason = (
                    "scheduled matched-price refresh; queue priority unobserved"
                    if market.priority == "unknown"
                    else "another batch is needed and admitted bag stock is empty"
                )
                auction_ids = tuple(row.auction_id for row in market.managed_auctions)
                cancel_ids = auction_ids[:MAX_SELLER_CANCELS_PER_CHILD]
                actions = tuple(
                    CancelAuction(auction_id, item.max_cancel_cost_copper) for auction_id in cancel_ids
                )
                remaining_ids = auction_ids[MAX_SELLER_CANCELS_PER_CHILD:]
                quantity = sum(
                    row.remaining_quantity for row in market.managed_auctions[:MAX_SELLER_CANCELS_PER_CHILD]
                )
        else:
            kind, reason = "pause", "managed listing displacement is not proved"

    body: dict[str, object] = {
        "actions": [action_object(action) for action in actions],
        "evidenceDigest": evidence_digest,
        "itemKey": item.item_key,
        "kind": kind,
        "quantity": quantity,
        "reason": reason,
        "unitPriceCopper": price,
    }
    decision_id = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return SellerDecision(
        decision_id,
        kind,
        item.item_key,
        quantity,
        price,
        reason,
        market.request_id,
        market.client_boot_id,
        market.sequence,
        market.observed_at,
        evidence_digest,
        actions,
        remaining_ids,
    )
