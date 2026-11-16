from __future__ import annotations

from copy import deepcopy

import pytest
from crowley_common.frames import ProtocolError, decode_frame, encode_frame

from tests.automation.native_harness import NativeHarness


def test_automation_frame_is_v3_and_uses_v1_transport_header() -> None:
    frame = decode_frame(NativeHarness().frame_bytes, expected_kind="automation")
    assert frame.raw_bytes[:4] == b"CRS1"
    assert frame.raw_bytes[4:6] == bytes((1, 1))
    assert frame.snapshot["schema"] == 3


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda value: value["context"].pop("nativeRegion"), "context"),
        (lambda value: value["context"].update(nativeRegion=0), "context.nativeRegion"),
        (lambda value: value["context"].update(nativeRegion=4_294_967_296), "context.nativeRegion"),
        (lambda value: value.update(goldCopper=True), "goldCopper"),
        (lambda value: value["motion"]["bindings"].pop("left"), "motion.bindings"),
        (lambda value: value["motion"]["hover"].update(screenY=-1), "motion.hover"),
        (lambda value: value["operation"].update(ready=1), "operation.ready"),
    ],
)
def test_automation_parser_rejects_missing_or_malformed_native_facts(mutate, path: str) -> None:
    snapshot = deepcopy(decode_frame(NativeHarness().frame_bytes).snapshot.value)
    mutate(snapshot)
    with pytest.raises(ProtocolError, match=path.replace(".", r"\.")):
        encode_frame(snapshot, expected_kind="automation")


def test_mail_inspection_wire_has_exact_public_fields_and_native_sixteen_slot_bound() -> None:
    snapshot = deepcopy(decode_frame(NativeHarness().frame_bytes).snapshot.value)
    attachment_ids = list(range(1, 17))
    snapshot["inspection"] = {
        "kind": "mail",
        "page": 0,
        "hasMore": False,
        "error": "",
        "requestId": "a" * 32,
        "capturedAt": snapshot["wallTime"],
        "sourceCount": 1,
        "rows": [
            {
                "index": 1,
                "moneyCopper": 0,
                "codCopper": 0,
                "attachmentCount": 16,
                "attachmentItemIDs": attachment_ids,
                "returned": False,
                "canDelete": True,
                "daysLeft": 30.0,
                "isGM": False,
                "attachments": [
                    {
                        "attachment": index,
                        "itemID": index,
                        "itemKey": str(index),
                        "quantity": index,
                        "selectionToken": f"{index:064x}",
                    }
                    for index in attachment_ids
                ],
            }
        ],
    }
    encode_frame(snapshot, expected_kind="automation")

    snapshot["inspection"]["rows"][0]["attachmentCount"] = 17
    snapshot["inspection"]["rows"][0]["attachmentItemIDs"].append(17)
    with pytest.raises(ProtocolError, match="attachmentCount"):
        encode_frame(snapshot, expected_kind="automation")


def test_mail_selection_token_is_bounded_opaque_and_requires_safe_attachment_facts() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(123, 2)], returned=False)
    harness.fire("MAIL_SHOW")
    assert harness.slash("inspect mail 0")
    snapshot = deepcopy(decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value)
    attachment = snapshot["inspection"]["rows"][0]["attachments"][0]
    token = attachment["selectionToken"]
    assert isinstance(token, str) and len(token) == 64
    assert "sender" not in snapshot["inspection"]["rows"][0]
    assert "subject" not in snapshot["inspection"]["rows"][0]

    attachment["selectionToken"] = "not-a-token"
    with pytest.raises(ProtocolError, match="selectionToken"):
        encode_frame(snapshot, expected_kind="automation")

    snapshot = deepcopy(decode_frame(harness.frame_bytes, expected_kind="automation").snapshot.value)
    snapshot["inspection"]["rows"][0]["codCopper"] = 1
    with pytest.raises(ProtocolError, match="selectionToken"):
        encode_frame(snapshot, expected_kind="automation")


def test_mail_inspection_rejects_internal_header_fields() -> None:
    snapshot = deepcopy(decode_frame(NativeHarness().frame_bytes).snapshot.value)
    snapshot["inspection"] = {
        "kind": "mail",
        "page": 0,
        "hasMore": False,
        "error": "",
        "requestId": "a" * 32,
        "capturedAt": snapshot["wallTime"],
        "sourceCount": 1,
        "rows": [
            {
                "index": 1,
                "moneyCopper": 0,
                "codCopper": 0,
                "attachmentCount": 0,
                "attachmentItemIDs": [],
                "returned": False,
                "canDelete": True,
                "daysLeft": 30.0,
                "isGM": False,
                "attachments": [],
                "sender": "private sender",
            }
        ],
    }
    with pytest.raises(ProtocolError, match="keys differ"):
        encode_frame(snapshot, expected_kind="automation")


def test_mail_page_size_and_every_native_quantity_use_safe_integer_bounds() -> None:
    snapshot = deepcopy(decode_frame(NativeHarness().frame_bytes).snapshot.value)
    row = {
        "index": 1,
        "moneyCopper": 0,
        "codCopper": 0,
        "attachmentCount": 0,
        "attachmentItemIDs": [],
        "returned": False,
        "canDelete": True,
        "daysLeft": 30.0,
        "isGM": False,
        "attachments": [],
    }
    snapshot["inspection"] = {
        "kind": "mail",
        "page": 0,
        "hasMore": True,
        "error": "",
        "requestId": "b" * 32,
        "capturedAt": snapshot["wallTime"],
        "sourceCount": 5,
        "rows": [row],
    }
    encode_frame(snapshot, expected_kind="automation")
    snapshot["inspection"]["rows"].append(dict(row, index=2))
    with pytest.raises(ProtocolError, match="inspection.rows"):
        encode_frame(snapshot, expected_kind="automation")

    harness = NativeHarness()
    harness.add_mail(attachments=[(123, 1)])
    harness.fire("MAIL_SHOW")
    assert harness.slash("inspect mail 0")
    snapshot = deepcopy(decode_frame(harness.frame_bytes).snapshot.value)
    snapshot["inspection"]["rows"][0]["moneyCopper"] = 9_007_199_254_740_992
    with pytest.raises(ProtocolError, match="moneyCopper"):
        encode_frame(snapshot, expected_kind="automation")

    snapshot = deepcopy(decode_frame(harness.frame_bytes).snapshot.value)
    snapshot["inspection"]["rows"][0]["attachments"][0]["quantity"] = 9_007_199_254_740_992
    with pytest.raises(ProtocolError, match="quantity"):
        encode_frame(snapshot, expected_kind="automation")

    snapshot = deepcopy(decode_frame(harness.frame_bytes).snapshot.value)
    snapshot["sellerMarket"] = {
        "requestId": "c" * 32,
        "itemKey": "123",
        "complete": True,
        "coverage": "full",
        "resultCount": 1,
        "bestCompetitorUnitPriceCopper": 9_007_199_254_740_992,
        "bestCompetitorQuantity": 1,
        "bestCompetitorResultCount": 1,
        "ambiguousOwnerUnitPriceCopper": False,
        "managedUnitPriceCopper": False,
        "managedAuctionCount": 0,
        "managedQuantity": 0,
        "error": "",
        "capturedAt": snapshot["wallTime"],
    }
    with pytest.raises(ProtocolError, match="bestCompetitorUnitPriceCopper"):
        encode_frame(snapshot, expected_kind="automation")
