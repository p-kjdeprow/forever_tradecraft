"""Strict adapters from frozen native automation snapshots to seller evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .backend import DesktopBackend, DesktopBackendError
from .seller import MarketFacts, OwnedAuction, SellerPolicyError

_MAX_SNAPSHOT_AGE_SECONDS = 60
_MAX_INSPECTION_PAGES = 4_096
_INSPECTION_PAGE_SIZE = {"inventory": 4, "owned": 4, "mail": 1}
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")


class SellerNativeError(RuntimeError):
    """A native seller observation is malformed, stale, incomplete, or mismatched."""


@dataclass(frozen=True, slots=True)
class NativeObservation[T]:
    request_id: str
    boot: str
    sequence: int
    captured_at: int
    complete: bool
    facts: tuple[T, ...]


@dataclass(frozen=True, slots=True)
class StockFact:
    item_key: str
    item_link: str
    quantity: int
    bag: int
    slot: int
    locked: bool
    bound: bool
    commodity: bool


@dataclass(frozen=True, slots=True)
class OwnedAuctionFact:
    auction_id: int
    item_key: str
    item_link: str
    quantity: int
    status: str
    buyout_known: bool
    buyout_copper: int
    cancel_allowed: bool
    cancel_cost_known: bool
    cancel_cost_copper: int


@dataclass(frozen=True, slots=True)
class ReturnFact:
    mail_fingerprint: str
    item_key: str | None
    quantity: int | None
    eligible: bool
    reason: str


class _InspectionBackend(Protocol):
    def inspect(self, kind: str, page: int) -> dict[str, object]: ...

    def observe_seller_market(
        self, item_key: str, request_id: str, managed_ids: frozenset[int] = frozenset()
    ) -> dict[str, object]: ...


class SellerNative:
    """Convert bounded addon observations into typed, provenance-bound seller facts."""

    def __init__(self, backend: DesktopBackend | _InspectionBackend) -> None:
        self.backend = backend
        self._last_inspection_id: dict[str, str] = {}

    def inventory(self) -> NativeObservation[StockFact]:
        envelope = self._collect_pages("inventory")
        facts: list[StockFact] = []
        seen: set[tuple[int, int]] = set()
        for row in envelope["rows"]:
            try:
                self._keys(
                    row,
                    {"bag", "slot", "itemKey", "itemLink", "count", "locked", "bound", "commodity"},
                    "inventory row",
                )
                bag = self._integer(row["bag"], "inventory bag", minimum=0, maximum=4)
                slot = self._integer(row["slot"], "inventory slot", minimum=1, maximum=256)
                identity = (bag, slot)
                if identity in seen:
                    raise SellerNativeError("inventory snapshot repeats a bag slot")
                seen.add(identity)
                item_key = self._item_key(row["itemKey"], "inventory itemKey")
                item_link = self._text(row["itemLink"], "inventory itemLink", maximum=512)
                quantity = self._integer(row["count"], "inventory count", minimum=1)
                locked = self._boolean(row["locked"], "inventory locked")
                bound = self._boolean(row["bound"], "inventory bound")
                commodity = self._boolean(row["commodity"], "inventory commodity")
                facts.append(StockFact(item_key, item_link, quantity, bag, slot, locked, bound, commodity))
            except (KeyError, TypeError, ValueError, SellerPolicyError) as exc:
                if isinstance(exc, SellerNativeError):
                    raise
                raise SellerNativeError(f"malformed inventory fact: {exc}") from exc
        facts.sort(key=lambda fact: (fact.bag, fact.slot))
        return self._observation(envelope, tuple(facts))

    def owned(self) -> NativeObservation[OwnedAuctionFact]:
        envelope = self._collect_pages("owned")
        facts: list[OwnedAuctionFact] = []
        seen: set[int] = set()
        for row in envelope["rows"]:
            try:
                self._keys(
                    row,
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
                    "owned row",
                )
                auction_id = self._integer(row["auctionId"], "owned auctionId", minimum=1)
                if auction_id in seen:
                    raise SellerNativeError("owned snapshot repeats an auction ID")
                seen.add(auction_id)
                item_key = self._item_key(row["itemKey"], "owned itemKey")
                item_link = self._text(row["itemLink"], "owned itemLink", maximum=512)
                quantity = self._integer(row["quantity"], "owned quantity", minimum=1)
                status = row["status"]
                if status not in {"active", "sold"}:
                    raise SellerNativeError("owned auction status is unsupported")
                buyout_known = self._boolean(row["buyoutKnown"], "owned buyoutKnown")
                buyout_copper = self._integer(row["buyoutCopper"], "owned buyoutCopper")
                if not buyout_known and buyout_copper != 0:
                    raise SellerNativeError("unknown owned buyout must be zero")
                cancel_allowed = self._boolean(row["cancelAllowed"], "owned cancelAllowed")
                cancel_cost_known = self._boolean(row["cancelCostKnown"], "owned cancelCostKnown")
                cancel_cost_copper = self._integer(row["cancelCostCopper"], "owned cancelCostCopper")
                if not cancel_cost_known and cancel_cost_copper != 0:
                    raise SellerNativeError("unknown owned cancel cost must be zero")
                facts.append(
                    OwnedAuctionFact(
                        auction_id,
                        item_key,
                        item_link,
                        quantity,
                        status,
                        buyout_known,
                        buyout_copper,
                        cancel_allowed,
                        cancel_cost_known,
                        cancel_cost_copper,
                    )
                )
            except (KeyError, TypeError, ValueError, SellerPolicyError) as exc:
                if isinstance(exc, SellerNativeError):
                    raise
                raise SellerNativeError(f"malformed owned-auction fact: {exc}") from exc
        facts.sort(key=lambda fact: fact.auction_id)
        return self._observation(envelope, tuple(facts))

    def returns(self, item_key: str) -> NativeObservation[ReturnFact]:
        self._require_item_key(item_key, commodity=True)
        envelope = self._collect_pages("mail")
        item_id = int(item_key)
        facts: list[ReturnFact] = []
        for row in envelope["rows"]:
            self._keys(
                row,
                {
                    "index",
                    "moneyCopper",
                    "codCopper",
                    "attachmentCount",
                    "attachmentItemIDs",
                    "returned",
                    "canDelete",
                    "daysLeft",
                    "isGM",
                    "attachments",
                },
                "mail row",
            )
            self._integer(row["index"], "mail index", minimum=1)
            self._integer(row["moneyCopper"], "mail moneyCopper")
            self._integer(row["codCopper"], "mail codCopper")
            attachment_count = self._integer(row["attachmentCount"], "mail attachmentCount", maximum=16)
            self._boolean(row["returned"], "mail returned")
            self._boolean(row["isGM"], "mail isGM")
            if type(row["attachments"]) is not list or len(row["attachments"]) != attachment_count:
                raise SellerNativeError("mail attachment snapshot is incomplete")
            for attachment in row["attachments"]:
                self._keys(
                    attachment,
                    {"attachment", "itemID", "itemKey", "quantity", "selectionToken"},
                    "mail attachment",
                )
                self._integer(attachment["attachment"], "mail attachment index", minimum=1, maximum=16)
                attachment_item_id = self._integer(attachment["itemID"], "mail attachment itemID", minimum=1)
                quantity = self._integer(attachment["quantity"], "mail attachment quantity", minimum=1)
                if attachment["itemKey"] is not False:
                    self._item_key(attachment["itemKey"], "mail attachment itemKey")
                    if attachment["itemKey"].split(":", 1)[0] != str(attachment_item_id):
                        raise SellerNativeError("mail attachment itemKey disagrees with its native item ID")
                token_value = attachment["selectionToken"]
                if token_value is False:
                    token = None
                else:
                    token = self._text(token_value, "mail attachment selectionToken", maximum=64)
                    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
                        raise SellerNativeError("mail attachment selection token is malformed")
                    if row["isGM"] is not False or row["codCopper"] != 0 or attachment["itemKey"] is False:
                        raise SellerNativeError("mail attachment selection token contradicts safety facts")
                if attachment_item_id != item_id:
                    continue
                observed_key = attachment["itemKey"]
                exact_key = item_key if observed_key == item_key else None
                eligible = (
                    token is not None
                    and exact_key == item_key
                    and row["isGM"] is False
                    and row["codCopper"] == 0
                )
                reason = (
                    "eligible_exact_commodity_attachment" if eligible else "return_attachment_not_selectable"
                )
                facts.append(
                    ReturnFact(
                        token or "",
                        exact_key,
                        quantity,
                        eligible,
                        reason,
                    )
                )
        return self._observation(envelope, tuple(facts))

    def market(
        self, item_key: str, request_id: str, managed_ids: frozenset[int] = frozenset()
    ) -> MarketFacts:
        self._require_item_key(item_key, commodity=True)
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise SellerNativeError("market request ID must be 32 lowercase hexadecimal characters")
        if (
            type(managed_ids) is not frozenset
            or len(managed_ids) > 1024
            or any(type(value) is not int or value < 1 for value in managed_ids)
        ):
            raise SellerNativeError("managed auction IDs must be a bounded exact set")
        owned = self.owned()
        managed_rows = tuple(
            row for row in owned.facts if row.auction_id in managed_ids and row.status == "active"
        )
        found_ids = {row.auction_id for row in managed_rows}
        if found_ids != set(managed_ids):
            raise SellerNativeError(
                "fresh owned-auction snapshot does not contain every managed active auction"
            )
        managed: list[OwnedAuction] = []
        for row in managed_rows:
            if not row.buyout_known or row.buyout_copper <= 0 or row.buyout_copper % row.quantity:
                raise SellerNativeError("managed auction has no exact unit price in owned facts")
            managed.append(
                OwnedAuction(
                    row.auction_id,
                    row.item_key,
                    row.quantity,
                    row.buyout_copper // row.quantity,
                    row.status,  # type: ignore[arg-type]
                    row.cancel_allowed,
                    row.cancel_cost_known,
                    row.cancel_cost_copper,
                )
            )
        if any(row.item_key != item_key for row in managed):
            raise SellerNativeError("managed auction has another exact item identity")

        try:
            result = self.backend.observe_seller_market(item_key, request_id, managed_ids)
        except DesktopBackendError as exc:
            raise SellerNativeError(str(exc)) from exc
        try:
            self._keys(result, {"boot", "sequence", "wallTime", "sellerMarket"}, "market envelope")
            boot = self._text(result["boot"], "market boot", maximum=128)
            sequence = self._integer(result["sequence"], "market sequence", minimum=1)
            wall_time = self._integer(result["wallTime"], "market wallTime", minimum=1)
            if boot != owned.boot or sequence <= owned.sequence:
                raise SellerNativeError("market result is not newer than same-boot ownership facts")
            native = self._keys(
                result["sellerMarket"],
                {
                    "requestId",
                    "itemKey",
                    "complete",
                    "coverage",
                    "resultCount",
                    "bestCompetitorUnitPriceCopper",
                    "bestCompetitorQuantity",
                    "bestCompetitorResultCount",
                    "ambiguousOwnerUnitPriceCopper",
                    "managedUnitPriceCopper",
                    "managedAuctionCount",
                    "managedQuantity",
                    "error",
                    "capturedAt",
                },
                "seller market result",
            )
            if native["requestId"] != request_id or native["itemKey"] != item_key:
                raise SellerNativeError("native market result does not match requested item and token")
            if native["complete"] is not True or native["coverage"] != "full" or native["error"] != "":
                raise SellerNativeError("native market result is not complete and full")
            captured_at = self._integer(native["capturedAt"], "market capturedAt", minimum=1)
            self._check_fresh(captured_at, wall_time, "market")
            result_count = self._integer(native["resultCount"], "market resultCount", maximum=100_000)
            best_price = self._optional_price(
                native["bestCompetitorUnitPriceCopper"], "best competitor price"
            )
            ambiguous = self._optional_price(native["ambiguousOwnerUnitPriceCopper"], "ambiguous owner price")
            managed_search_price = self._optional_price(
                native["managedUnitPriceCopper"], "managed search price"
            )
            managed_count = self._integer(native["managedAuctionCount"], "managed result count")
            managed_quantity = self._integer(native["managedQuantity"], "managed result quantity")
            best_quantity = self._integer(native["bestCompetitorQuantity"], "best competitor quantity")
            best_count = self._integer(native["bestCompetitorResultCount"], "best competitor row count")
            if (best_price is None) != (best_quantity == 0 and best_count == 0):
                raise SellerNativeError("best competitor aggregate fields disagree")
            if managed_count > len(managed_ids) or (managed_search_price is None) != (
                managed_count == 0 and managed_quantity == 0
            ):
                raise SellerNativeError("managed search aggregate fields disagree")
            if managed_count and managed_quantity < managed_count:
                raise SellerNativeError("managed search aggregate quantity is impossible")
            if best_count > result_count or managed_count > result_count:
                raise SellerNativeError("market aggregate exceeds the complete native result count")
            managed_price = min((row.unit_price_copper for row in managed), default=None)
            managed_quantity_exact = sum(row.remaining_quantity for row in managed)
            if managed_quantity_exact > 9_007_199_254_740_991:
                raise SellerNativeError("managed auction quantity aggregate exceeds the exact integer range")
            if (
                managed_count != len(managed)
                or managed_quantity != managed_quantity_exact
                or managed_search_price != managed_price
            ):
                raise SellerNativeError(
                    "native managed auction aggregate differs from the complete fresh owned set"
                )
            if result_count == 0:
                priority = "empty" if not managed and ambiguous is None else "unknown"
            elif ambiguous is not None and (
                (managed_price is not None and ambiguous <= managed_price)
                or (best_price is not None and ambiguous <= best_price)
            ):
                priority = "unknown"
            elif managed and managed_search_price is None:
                priority = "unknown"
            elif best_price is not None and managed_price is not None:
                if best_price == managed_price:
                    priority = "unknown"
                else:
                    priority = "competitor" if best_price < managed_price else "ours"
            elif best_price is not None:
                priority = "competitor"
            elif managed and managed_search_price == managed_price:
                priority = "ours"
            else:
                priority = "unknown"
            return MarketFacts(
                item_key,
                request_id,
                boot,
                sequence,
                captured_at,
                True,
                priority,  # type: ignore[arg-type]
                best_price,
                managed_price,
                managed_quantity_exact,
                tuple(sorted(managed_ids)),
                tuple(managed),
            )
        except (KeyError, TypeError, ValueError, SellerPolicyError) as exc:
            if isinstance(exc, SellerNativeError):
                raise
            raise SellerNativeError(f"malformed seller market fact: {exc}") from exc

    def _collect_pages(self, kind: str) -> dict[str, Any]:
        page_size = _INSPECTION_PAGE_SIZE[kind]
        pages: list[dict[str, Any]] = []
        for page in range(_MAX_INSPECTION_PAGES):
            try:
                row = self.backend.inspect(kind, page)
            except DesktopBackendError as exc:
                raise SellerNativeError(f"native {kind} inspection failed: {exc}") from exc
            if type(row) is not dict:
                raise SellerNativeError(f"native {kind} inspection returned a non-object")
            row = self._keys(
                row,
                {
                    "boot",
                    "capturedAt",
                    "kind",
                    "page",
                    "rows",
                    "hasMore",
                    "error",
                    "requestId",
                    "sequence",
                    "sourceCount",
                    "wallTime",
                },
                f"{kind} page envelope",
            )
            if row["kind"] != kind or self._integer(row["page"], f"{kind} page", maximum=4_095) != page:
                raise SellerNativeError(f"native {kind} inspection page identity changed")
            if row["error"]:
                raise SellerNativeError(f"native {kind} inspection failed: {row['error']}")
            if type(row["rows"]) is not list or type(row["hasMore"]) is not bool:
                raise SellerNativeError(f"native {kind} inspection page shape is malformed")
            self._integer(row["sequence"], f"{kind} sequence", minimum=1)
            page_capture = self._integer(row["capturedAt"], f"{kind} capturedAt", minimum=1)
            page_source_count = self._integer(row["sourceCount"], f"{kind} sourceCount", maximum=100_000)
            page_wall_time = self._integer(row["wallTime"], f"{kind} wallTime", minimum=1)
            self._check_fresh(page_capture, page_wall_time, kind)
            self._text(row["boot"], f"{kind} boot", maximum=128)
            if pages and row["sequence"] <= pages[-1]["sequence"]:
                raise SellerNativeError(f"native {kind} page sequence did not advance")
            if pages:
                for key in ("boot", "requestId", "capturedAt", "sourceCount", "wallTime"):
                    # wall time may advance while reading the same frozen snapshot.
                    if key == "wallTime":
                        continue
                    if row[key] != pages[0][key]:
                        raise SellerNativeError(f"native {kind} pages changed frozen snapshot {key}")
            if not pages:
                request_id = row["requestId"]
                if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
                    raise SellerNativeError(f"native {kind} snapshot request ID is malformed")
                previous = self._last_inspection_id.get(kind)
                if previous == request_id:
                    raise SellerNativeError(f"native {kind} page zero reused a prior snapshot generation")
                self._last_inspection_id[kind] = request_id
                expected_rows = min(page_size, page_source_count)
                if len(row["rows"]) != expected_rows:
                    raise SellerNativeError(f"native {kind} page zero does not match frozen source count")
            pages.append(row)
            if row["hasMore"] is False:
                break
        else:
            raise SellerNativeError(f"native {kind} inspection exceeds the bounded page count")
        if not pages or pages[-1]["hasMore"] is not False:
            raise SellerNativeError(f"native {kind} inspection is incomplete")
        source_count = pages[0]["sourceCount"]
        rows = [fact for row in pages for fact in row["rows"]]
        if len(rows) != source_count:
            raise SellerNativeError(f"native {kind} pages do not cover the frozen full source")
        for index, page in enumerate(pages):
            expected = min(page_size, max(0, source_count - index * page_size))
            if len(page["rows"]) != expected or page["hasMore"] != ((index + 1) * page_size < source_count):
                raise SellerNativeError(f"native {kind} page coverage contradicts frozen source count")
        return {
            "request_id": pages[0]["requestId"],
            "boot": pages[0]["boot"],
            "sequence": pages[-1]["sequence"],
            "captured_at": pages[0]["capturedAt"],
            "complete": True,
            "rows": rows,
        }

    @staticmethod
    def _observation[T](envelope: dict[str, Any], facts: tuple[T, ...]) -> NativeObservation[T]:
        request_id = SellerNative._text(envelope["request_id"], "observation request ID", maximum=32)
        boot = SellerNative._text(envelope["boot"], "observation boot", maximum=128)
        sequence = SellerNative._integer(envelope["sequence"], "observation sequence", minimum=1)
        captured_at = SellerNative._integer(envelope["captured_at"], "observation capturedAt", minimum=1)
        complete = SellerNative._boolean(envelope["complete"], "observation complete")
        return NativeObservation(
            request_id,
            boot,
            sequence,
            captured_at,
            complete,
            facts,
        )

    @classmethod
    def _require_item_key(cls, value: object, *, commodity: bool = False) -> str:
        if type(value) is not str:
            raise SellerNativeError("seller itemKey must be text")
        try:
            cls._item_key(value, "seller itemKey")
        except SellerNativeError:
            raise
        if commodity and (":" in value or not value.isdecimal() or value.startswith("0")):
            raise SellerNativeError("seller native supports exact base commodity item keys only")
        return value

    @staticmethod
    def _item_key(value: object, name: str) -> str:
        if type(value) is not str or not value.isascii() or not value:
            raise SellerNativeError(f"{name} is malformed")
        try:
            from .model import validate_item_key

            validate_item_key(value)
        except (ValueError, TypeError) as exc:
            raise SellerNativeError(f"{name} is not canonical") from exc
        return value

    @staticmethod
    def _keys(value: object, expected: set[str], name: str) -> dict[str, Any]:
        if type(value) is not dict or set(value) != expected:
            raise SellerNativeError(f"{name} must have exactly the documented fields")
        return cast(dict[str, Any], value)

    @staticmethod
    def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = 9_007_199_254_740_991) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise SellerNativeError(f"{name} is not an exact bounded integer")
        return value

    @staticmethod
    def _boolean(value: object, name: str) -> bool:
        if type(value) is not bool:
            raise SellerNativeError(f"{name} is not a boolean")
        return value

    @staticmethod
    def _text(value: object, name: str, *, maximum: int) -> str:
        if type(value) is not str or not value or not value.isascii() or len(value) > maximum:
            raise SellerNativeError(f"{name} is not bounded ASCII text")
        return value

    @classmethod
    def _optional_price(cls, value: object, name: str) -> int | None:
        if value is False:
            return None
        return cls._integer(value, name, minimum=1)

    @classmethod
    def _check_fresh(cls, captured_at: int, wall_time: int, name: str) -> None:
        if captured_at > wall_time or wall_time - captured_at > _MAX_SNAPSHOT_AGE_SECONDS:
            raise SellerNativeError(f"{name} snapshot is stale or captured after its frame")
