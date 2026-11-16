from __future__ import annotations

import pytest
from crowley_automation.seller_native import SellerNative, SellerNativeError
from crowley_common.frames import decode_frame

from tests.automation.native_harness import NativeHarness


class FakeBackend:
    def __init__(self) -> None:
        self.pages: dict[str, list[list[dict[str, object]]]] = {}
        self.generations: dict[str, str] = {}
        self.calls: list[tuple[str, int]] = []
        self.change_generation_at: int | None = None
        self.wall_times: list[int] = []
        self.market: dict[str, object] | None = None

    def set_pages(self, kind: str, pages: list[list[dict[str, object]]], generation: str) -> None:
        self.pages[kind] = pages
        self.generations[kind] = generation

    def inspect(self, kind: str, page: int) -> dict[str, object]:
        self.calls.append((kind, page))
        current = self.generations[kind]
        if self.change_generation_at == page:
            current = "f" * 32 if current != "f" * 32 else "e" * 32
        pages = self.pages[kind]
        rows = pages[page] if page < len(pages) else []
        source_count = sum(len(value) for value in pages)
        page_size = 1 if kind == "mail" else 4
        wall_time = self.wall_times[page] if page < len(self.wall_times) else 1_000 + page
        return {
            "boot": "boot-native",
            "capturedAt": 1_000,
            "kind": kind,
            "page": page,
            "rows": rows,
            "hasMore": (page + 1) * page_size < source_count,
            "error": "",
            "requestId": current,
            "sequence": page + 10,
            "sourceCount": source_count,
            "wallTime": wall_time,
        }

    def observe_seller_market(
        self, item_key: str, request_id: str, managed_ids: frozenset[int] = frozenset()
    ) -> dict[str, object]:
        assert self.market is not None
        return self.market


def stock_row(slot: int, *, quantity: int = 2, item_key: str = "123") -> dict[str, object]:
    return {
        "bag": 0,
        "slot": slot,
        "itemKey": item_key,
        "itemLink": "|cffffffff|Hitem:123:0:0:0:0:0:0:0:0:0:0:0:0|h[Item]|h|r",
        "count": quantity,
        "locked": False,
        "bound": False,
        "commodity": True,
    }


def owned_row(
    auction_id: int = 7,
    *,
    quantity: int = 2,
    buyout: int = 200,
    item_key: str = "123",
) -> dict[str, object]:
    return {
        "auctionId": auction_id,
        "itemKey": item_key,
        "itemLink": "|cffffffff|Hitem:123:0:0:0:0:0:0:0:0:0:0:0:0|h[Item]|h|r",
        "quantity": quantity,
        "buyoutKnown": True,
        "buyoutCopper": buyout,
        "status": "active",
        "cancelAllowed": True,
        "cancelCostKnown": True,
        "cancelCostCopper": 10,
    }


def mail_row(item_key: object = "123") -> dict[str, object]:
    return {
        "index": 1,
        "moneyCopper": 0,
        "codCopper": 0,
        "attachmentCount": 1,
        "attachmentItemIDs": [123],
        "returned": False,
        "canDelete": True,
        "daysLeft": 20.0,
        "isGM": False,
        "attachments": [
            {
                "attachment": 1,
                "itemID": 123,
                "itemKey": item_key,
                "quantity": 2,
                "selectionToken": "a" * 64 if item_key == "123" else False,
            }
        ],
    }


def market_wire(
    request_id: str = "a" * 32,
    *,
    best_price: int | bool = 100,
    best_quantity: int = 3,
    best_rows: int = 1,
    managed_price: int | bool = 100,
    managed_count: int = 1,
    managed_quantity: int = 2,
    ambiguous_price: int | bool = False,
    result_count: int = 2,
) -> dict[str, object]:
    return {
        "boot": "boot-native",
        "sequence": 30,
        "wallTime": 1_000,
        "sellerMarket": {
            "requestId": request_id,
            "itemKey": "123",
            "complete": True,
            "coverage": "full",
            "resultCount": result_count,
            "bestCompetitorUnitPriceCopper": best_price,
            "bestCompetitorQuantity": best_quantity if best_price is not False else 0,
            "bestCompetitorResultCount": best_rows if best_price is not False else 0,
            "ambiguousOwnerUnitPriceCopper": ambiguous_price,
            "managedUnitPriceCopper": managed_price,
            "managedAuctionCount": managed_count,
            "managedQuantity": managed_quantity,
            "error": "",
            "capturedAt": 1_000,
        },
    }


def test_inventory_pages_retain_one_frozen_generation_past_three_seconds() -> None:
    backend = FakeBackend()
    backend.set_pages("inventory", [[stock_row(index) for index in range(1, 5)], [stock_row(5)]], "1" * 32)
    backend.wall_times = [1_000, 1_007]
    observation = SellerNative(backend).inventory()
    assert observation.complete is True
    assert observation.request_id == "1" * 32
    assert observation.captured_at == 1_000
    assert observation.sequence == 11
    assert len(observation.facts) == 5
    assert [fact.slot for fact in observation.facts] == [1, 2, 3, 4, 5]


def test_inventory_pages_older_than_one_interval_are_refused() -> None:
    backend = FakeBackend()
    backend.set_pages("inventory", [[stock_row(1)]], "1" * 32)
    backend.wall_times = [1_061]
    with pytest.raises(SellerNativeError, match="stale or captured"):
        SellerNative(backend).inventory()


def test_inventory_rejects_mixed_snapshot_generation_and_duplicate_locations() -> None:
    backend = FakeBackend()
    backend.set_pages("inventory", [[stock_row(index) for index in range(1, 5)], [stock_row(5)]], "2" * 32)
    backend.change_generation_at = 1
    with pytest.raises(SellerNativeError, match="frozen snapshot requestId"):
        SellerNative(backend).inventory()

    duplicate = FakeBackend()
    duplicate.set_pages("inventory", [[stock_row(1), stock_row(1)]], "3" * 32)
    with pytest.raises(SellerNativeError, match="repeats a bag slot"):
        SellerNative(duplicate).inventory()


def test_malformed_native_inventory_facts_are_not_treated_as_empty_stock() -> None:
    backend = FakeBackend()
    row = stock_row(1)
    row["count"] = True
    backend.set_pages("inventory", [[row]], "4" * 32)
    with pytest.raises(SellerNativeError, match="exact bounded integer"):
        SellerNative(backend).inventory()


def test_return_candidates_use_native_one_use_token_and_exact_attachment_identity() -> None:
    backend = FakeBackend()
    backend.set_pages("mail", [[mail_row()]], "5" * 32)
    candidate = SellerNative(backend).returns("123").facts[0]
    assert candidate.item_key == "123"
    assert candidate.quantity == 2
    assert candidate.mail_fingerprint == "a" * 64
    assert candidate.eligible is True
    assert candidate.reason == "eligible_exact_commodity_attachment"

    variant_backend = FakeBackend()
    variant_backend.set_pages("mail", [[mail_row(False)]], "6" * 32)
    variant = SellerNative(variant_backend).returns("123").facts[0]
    assert variant.item_key is None
    assert variant.eligible is False
    assert variant.mail_fingerprint == ""


class HarnessBackend:
    def __init__(self, harness: NativeHarness) -> None:
        self.harness = harness

    def inspect(self, kind: str, page: int) -> dict[str, object]:
        assert self.harness.slash(f"inspect {kind} {page}")
        frame = decode_frame(self.harness.frame_bytes, expected_kind="automation").snapshot.value
        inspection = frame["inspection"]
        return {
            "boot": frame["boot"],
            "capturedAt": inspection["capturedAt"],
            "kind": inspection["kind"],
            "page": inspection["page"],
            "rows": inspection["rows"],
            "hasMore": inspection["hasMore"],
            "error": inspection["error"],
            "requestId": inspection["requestId"],
            "sequence": frame["sequence"],
            "sourceCount": inspection["sourceCount"],
            "wallTime": frame["wallTime"],
        }


def test_real_lua_frame_common_decoder_and_adapter_publish_eligible_token_only_for_native_commodity() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(123, 2)], returned=False, link_level=70)
    harness.fire("MAIL_SHOW")
    observation = SellerNative(HarnessBackend(harness)).returns("123")
    candidate = observation.facts[0]
    assert observation.complete is True
    assert candidate.item_key == "123" and candidate.quantity == 2
    assert harness.snapshot["inspection"]["rows"][0]["returned"] is False
    assert candidate.eligible is True and len(candidate.mail_fingerprint) == 64
    assert harness.native_calls == []

    noncommodity = NativeHarness()
    noncommodity.state.marketIsCommodity = False
    noncommodity.add_mail(attachments=[(123, 2)], returned=False)
    noncommodity.fire("MAIL_SHOW")
    fact = SellerNative(HarnessBackend(noncommodity)).returns("123").facts[0]
    assert fact.item_key == "123" and fact.quantity == 2
    assert fact.eligible is False and fact.mail_fingerprint == ""

    large = NativeHarness()
    large.add_mail(attachments=[(123, 250)], returned=False)
    large.fire("MAIL_SHOW")
    whole_attachment = SellerNative(HarnessBackend(large)).returns("123").facts[0]
    assert whole_attachment.quantity == 250
    assert whole_attachment.eligible is True


def test_market_equal_price_is_unknown_even_with_exact_managed_and_competitor_rows() -> None:
    backend = FakeBackend()
    backend.set_pages("owned", [[owned_row()]], "7" * 32)
    backend.market = market_wire()
    market = SellerNative(backend).market("123", "a" * 32, frozenset({7}))
    assert market.priority == "unknown"
    assert market.best_competing_unit_price_copper == 100
    assert market.managed_unit_price_copper == 100
    assert market.managed_active_quantity == 2
    assert market.managed_auction_ids == (7,)
    assert market.managed_auctions[0].auction_id == 7


def test_market_requires_full_exact_managed_owned_and_search_aggregate() -> None:
    missing = FakeBackend()
    missing.set_pages("owned", [[owned_row()]], "8" * 32)
    missing.market = market_wire()
    with pytest.raises(SellerNativeError, match="does not contain every managed"):
        SellerNative(missing).market("123", "a" * 32, frozenset({8}))

    partial = FakeBackend()
    partial.set_pages("owned", [[owned_row()]], "9" * 32)
    partial.market = market_wire(managed_count=0, managed_quantity=0, managed_price=False)
    with pytest.raises(SellerNativeError, match="aggregate differs"):
        SellerNative(partial).market("123", "a" * 32, frozenset({7}))


def test_market_malformed_token_and_stale_or_mismatched_result_fail_closed() -> None:
    backend = FakeBackend()
    backend.set_pages("owned", [[]], "b" * 32)
    backend.market = market_wire(request_id="c" * 32)
    with pytest.raises(SellerNativeError, match="32 lowercase"):
        SellerNative(backend).market("123", "bad", frozenset())
    with pytest.raises(SellerNativeError, match="does not match requested"):
        SellerNative(backend).market("123", "a" * 32, frozenset())

    stale = FakeBackend()
    stale.set_pages("owned", [[]], "d" * 32)
    stale.market = market_wire(
        result_count=0,
        best_price=False,
        best_quantity=0,
        best_rows=0,
        managed_price=False,
        managed_count=0,
        managed_quantity=0,
    )
    stale.market["wallTime"] = 1_100
    stale.market["sellerMarket"]["capturedAt"] = 1_000  # type: ignore[index]
    with pytest.raises(SellerNativeError, match="stale or captured"):
        SellerNative(stale).market("123", "a" * 32, frozenset())
