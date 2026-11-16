from __future__ import annotations

from typing import Any

import pytest
from crowley_common.frames import decode_frame

from tests.automation.native_harness import NativeHarness, item_link

BATCH = "a" * 64


def command(harness: NativeHarness, value: str) -> None:
    assert harness.slash(value), value


def stage(
    harness: NativeHarness,
    kind: str,
    *,
    fields: dict[str, int] | None = None,
    item_key: str | None = None,
    batch: str = BATCH,
    ordinal: int = 0,
    step: int = 0,
) -> None:
    command(harness, f"begin {batch} {ordinal} {step}")
    command(harness, f"kind {kind}")
    if item_key is not None:
        for index, start in enumerate(range(0, len(item_key), 60), 1):
            command(harness, f"key {index} {item_key[start : start + 60]}")
    for name, value in (fields or {}).items():
        command(harness, f"arg {name} {value}")
    command(harness, f"expires {int(harness.state.now) + 90}")
    command(harness, "seal")


def admit_market_stage(harness: NativeHarness, kind: str, **kwargs: Any) -> None:
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(harness, kind, **kwargs)
    assert harness.state.queryOwnedCalls == 1
    assert not harness.snapshot["operation"]["ready"]
    harness.fire("OWNED_AUCTIONS_UPDATED")


def test_v2_idle_frame_publishes_required_native_observations() -> None:
    harness = NativeHarness()
    value = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value

    assert value["schema"] == 2
    assert value["context"]["nativeRegion"] == 110
    assert value["goldCopper"] == 10_000_000
    assert value["motion"]["bindings"] == {
        "forward": "MOVEFORWARD",
        "left": "TURNLEFT",
        "right": "TURNRIGHT",
    }
    assert value["motion"]["hover"] == {
        "kind": "none",
        "name": "",
        "screenX": 960,
        "screenY": 540,
    }
    assert value["operation"]["action"] is False
    assert value["ownership"] == {"mode": "standalone", "ahOwner": "none", "scannerState": "not_installed"}


@pytest.mark.parametrize(
    ("mode_source", "omit_mode"),
    [(None, True), ("local _, ns = ...\nns.Coordinated = 'standalone'", False)],
)
def test_startup_rejects_missing_or_malformed_explicit_mode(mode_source: str | None, omit_mode: bool) -> None:
    harness = NativeHarness(mode_source=mode_source, omit_mode=omit_mode, defer_start=True)
    with pytest.raises(Exception, match="automation mode must be an explicit boolean"):
        harness.fire("ADDON_LOADED", "Crowley_Automation")
    assert harness.native_calls == []


def test_partial_stage_keeps_idle_wire_projection_until_seal() -> None:
    harness = NativeHarness()
    command(harness, f"begin {BATCH} 0 0")
    assert harness.snapshot["operation"]["action"] is False
    assert harness.snapshot["operation"]["batchId"] == ""
    command(harness, "kind move_route")
    assert harness.snapshot["operation"]["action"] is False
    command(harness, "arg routeId bank_path")
    assert harness.snapshot["operation"]["action"] is False
    command(harness, f"expires {int(harness.state.now) + 90}")
    idle = harness.snapshot["operation"]
    assert idle["action"] is False
    assert idle["batchId"] == "" and idle["expiresAt"] == 0 and not idle["ready"]
    command(harness, "seal")
    sealed = harness.snapshot["operation"]
    assert sealed["action"] == {"kind": "move_route", "routeId": "bank_path"}
    assert sealed["batchId"] == BATCH and sealed["ready"]


def test_sealed_local_stages_can_continue_only_within_the_same_batch() -> None:
    harness = NativeHarness()
    stage(harness, "move_route", fields={"routeId": "bank_path"}, ordinal=0, step=4)
    first = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value["operation"]
    assert first["action"] == {"kind": "move_route", "routeId": "bank_path"}
    assert (first["batchId"], first["ordinal"], first["stepIndex"]) == (BATCH, 0, 4)

    stage(harness, "interact", fields={"targetId": "auctioneer"}, ordinal=1, step=5)
    second = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value["operation"]
    assert second["action"] == {"kind": "interact", "targetId": "auctioneer"}
    assert (second["batchId"], second["ordinal"], second["stepIndex"]) == (BATCH, 1, 5)
    assert harness.native_calls == []

    other_batch = "b" * 64
    assert not harness.slash(f"begin {other_batch} 2 6")
    rejected = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value["operation"]
    assert rejected["action"] is False
    assert harness.native_calls == []


def test_post_commodity_uses_exact_native_arguments_and_one_button_click() -> None:
    harness = NativeHarness()
    harness.add_item(count=10, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 5, "unitPriceCopper": 100, "durationHours": 24, "maxDepositCopper": 1000},
    )
    assert harness.snapshot["operation"]["ready"]
    assert harness.state.queryOwnedCalls == 1
    assert harness._from_lua(harness.state.queryOwnedArgs) == [[]]

    harness.hardware_click()
    harness.hardware_click()
    assert len(harness.native_calls) == 1
    call = harness.native_calls[0]
    assert call["kind"] == "post_commodity"
    assert len(call["args"]) == 4
    assert call["args"][0] == {"bag": 0, "slot": 1}
    assert call["args"][1:] == [2, 5, 100]
    assert harness.snapshot["operation"]["receipt"]["postcondition"] == "unknown"

    harness.add_owned(auction_id=99, quantity=5, buyout=500)
    harness.fire("AUCTION_HOUSE_AUCTION_CREATED", 99)
    harness.fire("OWNED_AUCTIONS_UPDATED")
    operation = harness.snapshot["operation"]
    assert operation["receipt"]["postcondition"] is True
    assert operation["receipt"]["actionComplete"] is True
    assert operation["receipt"]["sequence"] == 1
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"


def test_native_button_layering_and_hidden_button_readiness() -> None:
    harness = NativeHarness()
    harness.add_item(count=10, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    button = harness.lua.globals().CrowleyAutomationOperationButton
    assert button.strata == "TOOLTIP"
    assert button.frameLevel == 1001
    assert harness.snapshot["operation"]["ready"]
    button.Hide(button)
    harness.fire("PLAYER_ENTERING_WORLD")
    operation = harness.snapshot["operation"]
    assert not operation["ready"]
    assert operation["error"] == "operation_button_unavailable"
    harness.hardware_click()
    assert harness.native_calls == []

    local = NativeHarness()
    stage(local, "move_route", fields={"routeId": "bank_path"})
    local_button = local.lua.globals().CrowleyAutomationOperationButton
    local_button.Hide(local_button)
    local.fire("PLAYER_ENTERING_WORLD")
    assert local.snapshot["operation"]["ready"]
    assert local.native_calls == []


@pytest.mark.parametrize(("duration", "index"), [(12, 1), (24, 2), (48, 3)])
def test_post_item_uses_equal_total_bid_and_buyout_and_exact_arity(duration: int, index: int) -> None:
    harness = NativeHarness()
    harness.add_item(count=4, commodity=False)
    admit_market_stage(
        harness,
        "post_item",
        item_key="123",
        fields={"quantity": 3, "unitPriceCopper": 71, "durationHours": duration, "maxDepositCopper": 1000},
    )
    harness.hardware_click()
    call = harness.native_calls[0]
    assert call["kind"] == "post_item"
    assert len(call["args"]) == 5
    assert call["args"][1:] == [index, 3, 213, 213]


def test_variant_matching_uses_bonus_ids_from_link_and_allows_duplicates() -> None:
    harness = NativeHarness()
    key = {"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
    harness.add_item(item_key=key, link=item_link(123, bonuses=(8, 3, 8)), count=4, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123:b3.8.8",
        fields={"quantity": 2, "unitPriceCopper": 9, "durationHours": 12, "maxDepositCopper": 1000},
    )
    assert harness.snapshot["operation"]["ready"]
    harness.hardware_click()
    assert len(harness.native_calls) == 1


def _key_with_length(length: int) -> str:
    body_length = length - len("123:b")
    if body_length % 2:
        bonuses = ["1"] * ((body_length + 1) // 2)
    else:
        count = body_length // 2
        bonuses = ["1"] * (count - 1) + ["11"]
    value = "123:b" + ".".join(bonuses)
    assert len(value) == length
    return value


@pytest.mark.parametrize("key_length", [64, 65, 128])
def test_item_key_chunk_pipeline_accepts_canonical_keys_through_128_bytes(key_length: int) -> None:
    harness = NativeHarness()
    harness.fire("AUCTION_HOUSE_SHOW")
    item_key = _key_with_length(key_length)
    stage(
        harness,
        "post_commodity",
        item_key=item_key,
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    assert harness.snapshot["operation"]["action"]["itemKey"] == item_key
    assert harness.native_calls == []


def test_item_key_chunk_pipeline_rejects_129_bytes() -> None:
    harness = NativeHarness()
    item_key = _key_with_length(129)
    command(harness, f"begin {BATCH} 0 0")
    command(harness, "kind post_commodity")
    chunks = [item_key[index : index + 60] for index in range(0, len(item_key), 60)]
    command(harness, f"key 1 {chunks[0]}")
    command(harness, f"key 2 {chunks[1]}")
    assert not harness.slash(f"key 3 {chunks[2]}")
    assert harness.snapshot["operation"]["action"] is False
    assert harness.native_calls == []


def test_malformed_truncated_and_bad_suffix_links_fail_identity_and_post_preflight() -> None:
    harness = NativeHarness()
    native_key = harness._to_lua({"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    canonical = harness.ns.AutomationIdentity.Canonical(native_key, item_link(123))
    canonical_bonus = harness.ns.AutomationIdentity.Canonical(native_key, item_link(123, bonuses=(3, 8, 8)))
    assert canonical == "123"
    assert canonical_bonus == "123:b3.8.8"

    def full_link(fields: list[str]) -> str:
        return "|cffffffff|Hitem:" + ":".join(fields) + "|h[Item]|h|r"

    empty_metadata = full_link(["123", "", "", "", "", "", "0", "", "", "", "", "", "0", "55"])
    empty_zero_identity = full_link(["123"] + [""] * 12 + ["55"])
    empty_metadata_bonus = full_link(["123", "", "", "", "", "", "0", "", "", "", "", "", "1", "8", "91"])
    assert harness.ns.AutomationIdentity.Canonical(native_key, empty_metadata) == "123"
    assert harness.ns.AutomationIdentity.Canonical(native_key, empty_zero_identity) == "123"
    assert harness.ns.AutomationIdentity.Canonical(native_key, empty_metadata_bonus) == "123:b8"
    truncated = "|cffffffff|Hitem:123|h[Item]|h|r"
    bad_suffix = "|cffffffff|Hitem:123:0:0:0:0:0:x:0:0:0:0:0:0|h[Item]|h|r"
    bad_count = full_link(["123", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "x"])
    missing_bonus = full_link(["123", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "1"])
    incomplete = item_link(123)[:-2]
    assert harness.ns.AutomationIdentity.Canonical(native_key, truncated) is None
    assert harness.ns.AutomationIdentity.Canonical(native_key, bad_suffix) is None
    assert harness.ns.AutomationIdentity.Canonical(native_key, bad_count) is None
    assert harness.ns.AutomationIdentity.Canonical(native_key, missing_bonus) is None
    assert harness.ns.AutomationIdentity.Canonical(native_key, incomplete) is None

    for link in (truncated, bad_suffix, incomplete):
        harness = NativeHarness()
        harness.add_item(link=link, commodity=True)
        harness.fire("AUCTION_HOUSE_SHOW")
        stage(
            harness,
            "post_commodity",
            item_key="123",
            fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
        )
        harness.fire("OWNED_AUCTIONS_UPDATED")
        assert not harness.snapshot["operation"]["ready"]
        assert "cannot be identified exactly" in harness.snapshot["operation"]["error"]
        harness.hardware_click()
        assert harness.native_calls == []


def test_market_preflight_fails_before_button_ready_and_rechecks_stock_at_click() -> None:
    harness = NativeHarness()
    harness.add_item(count=2, commodity=True, available=2)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 3, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    operation = harness.snapshot["operation"]
    assert not operation["ready"]
    assert operation["error"] == "insufficient_commodity_stock"
    assert harness.native_calls == []

    harness = NativeHarness()
    harness.add_item(count=5, commodity=True, available=5)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 3, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.state.bags["0:1"].count = 1
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is False
    assert harness.snapshot["ownership"]["ahOwner"] == "none"


def test_owned_results_query_requires_exclusive_quiescent_ownership_and_event() -> None:
    harness = NativeHarness(coordinated=True, scanner_available=True, scan_active=True)
    harness.add_item(count=5, commodity=True)
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    assert harness.state.queryOwnedCalls == 0
    assert harness.snapshot["ownership"]["ahOwner"] == "scanner"

    harness.lua.globals()["_scan_active"] = False
    harness.advance(1.0)
    assert harness.state.queryOwnedCalls == 1
    assert not harness.snapshot["operation"]["ready"]
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert harness.snapshot["operation"]["ready"]
    assert harness.native_calls == []


def test_owned_query_rejection_or_incomplete_event_never_enables_action() -> None:
    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    harness.state.queryOwnedResult = False
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    operation = harness.snapshot["operation"]
    assert not operation["ready"]
    assert operation["error"] == "owned_auction_query_rejected"
    assert harness.native_calls == []

    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    harness.state.ownedFull = False
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert not harness.snapshot["operation"]["ready"]
    assert harness.snapshot["operation"]["error"] == "owned_auction_results_incomplete"
    assert harness.state.queryOwnedCalls == 1
    assert harness.native_calls == []


def test_owned_query_rejects_unexpected_native_return_values() -> None:
    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    harness.state.queryOwnedUnexpected = True
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    assert harness.snapshot["operation"]["error"] == "owned_auction_query_rejected"
    assert harness.state.queryOwnedCalls == 1
    assert harness.native_calls == []


def test_completed_market_grant_is_bound_to_batch_until_clear_or_expiry() -> None:
    harness = NativeHarness(coordinated=True, scanner_available=True)
    harness.add_item(count=10, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.hardware_click()
    harness.add_owned(auction_id=81, quantity=1, buyout=10)
    harness.fire("AUCTION_HOUSE_AUCTION_CREATED", 81)
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    assert harness.state.queryOwnedCalls == 1

    assert not harness.slash(f"begin {'b' * 64} 1 1")
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    native_call_count = len(harness.native_calls)
    stage(harness, "move_route", fields={"routeId": "bank_path"}, ordinal=1, step=1)
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    stage(harness, "interact", fields={"targetId": "auctioneer"}, ordinal=2, step=2)
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    assert harness.snapshot["ownership"]["scannerState"] == "quiescent"
    assert len(harness.native_calls) == native_call_count
    assert harness.slash("clear")
    assert harness.snapshot["ownership"]["ahOwner"] == "none"


def test_cancel_cost_preflight_and_native_cancel_arity() -> None:
    harness = NativeHarness()
    harness.add_owned(auction_id=55, can_cancel=True, cancel_cost=25)
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(harness, "cancel_auction", fields={"auctionId": 55, "maxCancelCostCopper": 25})
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert harness.snapshot["operation"]["ready"]
    harness.hardware_click()
    assert len(harness.native_calls) == 1
    assert harness.native_calls[0]["kind"] == "cancel_auction"
    assert harness.native_calls[0]["args"] == [55]
    harness.fire("AUCTION_CANCELED", 55)
    harness.state.owned[1] = None
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is True
    assert harness.snapshot["operation"]["receipt"]["actionComplete"] is True


def test_deposit_and_cancel_cost_limits_are_rechecked_at_dispatch() -> None:
    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 100},
    )
    harness.state.deposit = 101
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is False

    harness = NativeHarness()
    harness.add_owned(auction_id=56, can_cancel=True, cancel_cost=25)
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(harness, "cancel_auction", fields={"auctionId": 56, "maxCancelCostCopper": 25})
    harness.fire("OWNED_AUCTIONS_UPDATED")
    assert harness.snapshot["operation"]["ready"]
    harness.state.cancelCosts[56] = 26
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is False


def test_owned_inspection_queries_under_grant_and_releases_after_exact_event() -> None:
    harness = NativeHarness(coordinated=True, scanner_available=True)
    harness.add_owned(auction_id=77, quantity=2, buyout=222)
    harness.fire("AUCTION_HOUSE_SHOW")
    assert harness.slash("inspect owned 0")
    assert harness.snapshot["inspection"]["error"] == "owned_query_pending"
    assert harness.state.queryOwnedCalls == 1
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    harness.fire("AUCTION_HOUSE_SHOW")
    harness.fire("OWNED_AUCTIONS_UPDATED")
    inspection = harness.snapshot["inspection"]
    assert inspection["kind"] == "owned"
    assert inspection["rows"][0]["auctionId"] == 77
    assert harness.snapshot["ownership"]["ahOwner"] == "none"


def test_mail_header_and_attachment_reads_are_exact_and_wire_is_sanitized() -> None:
    harness = NativeHarness()
    harness.add_mail(money=300, attachments=[(123, 2)])
    harness.fire("MAIL_SHOW")
    assert harness.slash("inspect mail 0")
    row = harness.snapshot["inspection"]["rows"][0]
    assert set(row) == {
        "index",
        "moneyCopper",
        "codCopper",
        "attachmentCount",
        "attachmentItemIDs",
        "returned",
        "canDelete",
        "daysLeft",
    }
    assert row["attachmentItemIDs"] == [123]
    assert row["daysLeft"] == 30
    assert "sender" not in row and "subject" not in row

    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert harness.snapshot["operation"]["ready"]
    harness.hardware_click()
    assert len(harness.native_calls) == 1
    assert harness.native_calls[0]["kind"] == "collect_mail"
    assert harness.native_calls[0]["args"] == [1]
    row = harness.state.inbox[1]
    row.money = 0
    row.itemCount = 0
    row.attachments[1] = None
    harness.add_item(
        slot=1, count=2, item_key={"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
    )
    harness.state.money = harness.state.money + 300
    harness.fire("MAIL_INBOX_UPDATE")
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is True
    assert harness.snapshot["operation"]["receipt"]["actionComplete"] is False


def test_mail_settlement_uses_raw_bag_counts_without_auction_house_apis() -> None:
    harness = NativeHarness()
    harness.add_item(
        slot=1,
        item_key={"itemID": 999, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0},
        count=4,
        bound=True,
        sell_valid=False,
    )
    harness.add_mail(attachments=[(123, 2)])
    auction = harness.lua.globals().C_AuctionHouse
    auction.GetItemKeyFromItem = None
    auction.GetItemCommodityStatus = None
    auction.IsSellItemValid = None
    harness.lua.globals().C_Item.IsBound = None
    harness.fire("MAIL_SHOW")
    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert harness.snapshot["operation"]["ready"]
    harness.hardware_click()
    assert harness.native_calls == [{"kind": "collect_mail", "args": [1]}]
    assert harness.state.locationCalls == 0
    assert harness.state.sellValidCalls == 0

    row = harness.state.inbox[1]
    row.itemCount = 0
    row.attachments[1] = None
    harness.add_item(
        slot=2,
        item_key={"itemID": 123, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0},
        count=2,
        bound=True,
        sell_valid=False,
    )
    harness.fire("MAIL_INBOX_UPDATE")
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is True
    assert harness.state.locationCalls == 0
    assert harness.state.sellValidCalls == 0


def test_mail_attachment_slot_holes_are_scanned_without_publishing_private_header_values() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(101, 1), (202, 3)])
    row = harness.state.inbox[1]
    row.attachments[3] = row.attachments[2]
    row.attachments[2] = None
    harness.fire("MAIL_SHOW")
    assert harness.slash("inspect mail 0")
    assert harness.snapshot["inspection"]["rows"][0]["attachmentItemIDs"] == [101, 202]


def test_mail_header_with_unexpected_native_return_arity_fails_closed() -> None:
    harness = NativeHarness()
    harness.add_mail(money=20)
    harness.state.mailHeaderExtra = True
    harness.fire("MAIL_SHOW")
    assert not harness.slash("inspect mail 0")
    assert harness.snapshot["inspection"]["error"] == "mail header arity malformed"


def test_mail_capacity_preflight_is_conservative_and_rechecked_at_click() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(101, 1), (202, 1)])
    harness.state.freeBagSlotsByBag[0] = 1
    harness.state.freeBagSlotsByBag[1] = 20
    harness.state.bagFamilies[1] = 8
    harness.fire("MAIL_SHOW")
    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert not harness.snapshot["operation"]["ready"]
    assert harness.snapshot["operation"]["error"] == "insufficient_mail_bag_slots"
    bag_queries = harness._from_lua(harness.state.freeBagSlotArgs)
    assert len(bag_queries) >= 5
    assert bag_queries[:5] == [[0], [1], [2], [3], [4]]
    assert harness.native_calls == []

    harness = NativeHarness()
    harness.add_mail(attachments=[(101, 1), (202, 1)])
    harness.fire("MAIL_SHOW")
    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert harness.snapshot["operation"]["ready"]
    harness.state.freeBagSlotsByBag[0] = 1
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["receipt"]["postcondition"] is False

    harness = NativeHarness()
    harness.add_mail(money=300)
    harness.state.freeBagSlotsByBag[0] = 0
    harness.fire("MAIL_SHOW")
    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert harness.snapshot["operation"]["ready"]
    assert harness.state.freeBagSlotCalls == 0


def test_mail_capacity_requires_exact_native_free_slot_returns() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(101, 1)])
    harness.state.freeBagSlotExtra = True
    harness.fire("MAIL_SHOW")
    stage(harness, "collect_mail", fields={"messageLimit": 5})
    assert not harness.snapshot["operation"]["ready"]
    assert harness.snapshot["operation"]["error"] == "free bag slot facts malformed"
    assert harness.native_calls == []


def test_mail_cod_and_gm_messages_never_arm_or_dispatch() -> None:
    for options, expected in [
        ({"cod": 1, "money": 30}, "no_eligible_non_cod_mail"),
        ({"money": 30, "is_gm": True}, "no_eligible_non_cod_mail"),
    ]:
        harness = NativeHarness()
        harness.add_mail(**options)
        harness.fire("MAIL_SHOW")
        stage(harness, "collect_mail", fields={"messageLimit": 5})
        assert not harness.snapshot["operation"]["ready"]
        assert harness.snapshot["operation"]["error"] == expected
        harness.hardware_click()
        assert harness.native_calls == []


def test_active_or_closed_operation_cannot_be_replayed_or_rearmed() -> None:
    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.fire("AUCTION_HOUSE_CLOSED")
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["action"] is False
    assert harness.snapshot["ownership"]["ahOwner"] == "none"


def test_abandoned_unsealed_stage_expires_without_enabling_action() -> None:
    harness = NativeHarness()
    assert harness.slash(f"begin {BATCH} 0 0")
    assert harness.slash("kind post_commodity")
    harness.advance(121)
    assert harness.snapshot["operation"]["action"] is False
    assert harness.snapshot["operation"]["error"] == "partial_stage_expired"
    assert harness.native_calls == []


def test_missing_coordinator_in_combined_mode_fails_closed() -> None:
    harness = NativeHarness(coordinated=True)
    harness.add_item(count=5, commodity=True)
    harness.fire("AUCTION_HOUSE_SHOW")
    stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    assert not harness.snapshot["operation"]["ready"]
    assert harness.snapshot["operation"]["error"] == "coordinator_unavailable"
    assert harness.state.queryOwnedCalls == 0
    assert harness.native_calls == []


@pytest.mark.parametrize(
    "commands",
    [
        ["arg quantity 1.0"],
        ["arg unknown 1"],
        ["arg quantity 1", "arg quantity 2"],
        ["key 2 123"],
        ["key 1 123", "key 1 456"],
        ["key 1 " + "x" * 61],
    ],
)
def test_strict_staging_rejects_malformed_fields_and_chunk_sequences(commands: list[str]) -> None:
    harness = NativeHarness()
    assert harness.slash(f"begin {BATCH} 0 0")
    assert harness.slash("kind post_commodity")
    for value in commands:
        result = harness.slash(value)
        if value == commands[-1]:
            assert not result
        if not result:
            break
    assert harness.native_calls == []
    assert harness.snapshot["operation"]["action"] is False


def test_expired_seal_never_arms_or_dispatches() -> None:
    harness = NativeHarness()
    harness.add_item(count=5, commodity=True)
    harness.fire("AUCTION_HOUSE_SHOW")
    command(harness, f"begin {BATCH} 0 0")
    command(harness, "kind post_commodity")
    command(harness, "key 1 123")
    command(harness, "arg quantity 1")
    command(harness, "arg unitPriceCopper 10")
    command(harness, "arg durationHours 24")
    command(harness, "arg maxDepositCopper 1000")
    command(harness, f"expires {int(harness.state.now) + 1}")
    harness.advance(2)
    assert not harness.slash("seal")
    assert harness.snapshot["operation"]["action"] is False
    assert harness.state.queryOwnedCalls == 0
    assert harness.native_calls == []


def test_unrelated_native_callbacks_leave_market_receipt_unknown_and_grant_held() -> None:
    harness = NativeHarness(coordinated=True, scanner_available=True)
    harness.add_item(count=5, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.hardware_click()
    harness.fire("AUCTION_HOUSE_AUCTION_CREATED", 444)
    harness.fire("AUCTION_HOUSE_CLOSED")
    operation = harness.snapshot["operation"]
    assert operation["receipt"]["postcondition"] == "unknown"
    assert operation["receipt"]["actionComplete"] == "unknown"
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    assert not harness.slash("clear")
    assert not harness.slash(f"begin {BATCH} 1 1")
    harness.hardware_click()
    assert len(harness.native_calls) == 1


def test_native_call_exception_stays_unknown_and_cannot_be_replayed() -> None:
    harness = NativeHarness(coordinated=True, scanner_available=True)
    harness.add_item(count=5, commodity=True)
    admit_market_stage(
        harness,
        "post_commodity",
        item_key="123",
        fields={"quantity": 1, "unitPriceCopper": 10, "durationHours": 24, "maxDepositCopper": 1000},
    )
    harness.state.nativeError = True
    harness.hardware_click()
    harness.hardware_click()
    operation = harness.snapshot["operation"]
    assert len(harness.native_calls) == 1
    assert operation["receipt"]["postcondition"] == "unknown"
    assert operation["receipt"]["actionComplete"] == "unknown"
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    assert not harness.slash("clear")


def test_persisted_pending_market_marker_resuspends_and_never_rearms() -> None:
    harness = NativeHarness(
        coordinated=True,
        scanner_available=True,
        saved_pending={"batchId": BATCH, "ordinal": 0, "stepIndex": 0, "kind": "post_commodity"},
    )
    assert harness.snapshot["ownership"]["ahOwner"] == "automation"
    assert harness.snapshot["ownership"]["scannerState"] == "quiescent"
    frame = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value
    assert frame["operation"]["action"] is False
    restored_pending = harness.saved_db.pending
    assert not harness.slash(f"begin {BATCH} 1 1")
    harness.hardware_click()
    assert harness.native_calls == []
    assert harness.lua.eval("rawequal")(harness.lua.globals().CrowleyAutomationDB, harness.saved_db)
    assert harness.lua.eval("rawequal")(harness.saved_db.pending, restored_pending)
    assert harness.orphan_db.pending is None
    assert harness.saved_db.pending.kind == "post_commodity"
    scanner = harness.lua.globals().CrowleyScanner
    assert scanner.quiescent("crowley-automation") is True


def test_compressed_overflow_keeps_last_frame_and_later_publish_recovers() -> None:
    harness = NativeHarness()
    previous = harness.frame_bytes
    codec = harness.lua.globals().C_EncodingUtil
    compress = codec.CompressString
    codec.CompressString = lambda *_: "x" * 3000
    harness.fire("PLAYER_ENTERING_WORLD")
    assert harness.frame_bytes == previous

    codec.CompressString = compress
    harness.fire("PLAYER_ENTERING_WORLD")
    current = harness.frame_bytes
    assert current != previous
    assert decode_frame(current, expected_kind="automation").snapshot["sequence"] > 1


def test_motion_cursor_is_physical_and_independent_of_ui_scale() -> None:
    harness = NativeHarness(ui_scale=2.0)
    harness.state.cursorX = 300
    harness.state.cursorY = 200
    harness.fire("PLAYER_ENTERING_WORLD")
    motion = decode_frame(harness.frame_bytes, expected_kind="automation").snapshot["motion"]
    assert motion["hover"]["screenX"] == 300
    assert motion["hover"]["screenY"] == 880
    assert decode_frame(harness.frame_bytes, expected_kind="automation").snapshot["operation"][
        "buttonRect"
    ] == {
        "x": 740,
        "y": 800,
        "width": 440,
        "height": 88,
    }
