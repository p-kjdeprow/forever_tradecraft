from __future__ import annotations

from copy import deepcopy

import pytest
from crowley_common.frames import ProtocolError, decode_frame, encode_frame

from tests.automation.native_harness import NativeHarness


def test_automation_frame_is_v2_and_uses_v1_transport_header() -> None:
    frame = decode_frame(NativeHarness().frame_bytes, expected_kind="automation")
    assert frame.raw_bytes[:4] == b"CRS1"
    assert frame.raw_bytes[4:6] == bytes((1, 1))
    assert frame.snapshot["schema"] == 2


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
            }
        ],
    }
    encode_frame(snapshot, expected_kind="automation")

    snapshot["inspection"]["rows"][0]["attachmentCount"] = 17
    snapshot["inspection"]["rows"][0]["attachmentItemIDs"].append(17)
    with pytest.raises(ProtocolError, match="attachmentCount"):
        encode_frame(snapshot, expected_kind="automation")


def test_mail_inspection_rejects_internal_header_fields() -> None:
    snapshot = deepcopy(decode_frame(NativeHarness().frame_bytes).snapshot.value)
    snapshot["inspection"] = {
        "kind": "mail",
        "page": 0,
        "hasMore": False,
        "error": "",
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
                "sender": "private sender",
            }
        ],
    }
    with pytest.raises(ProtocolError, match="keys differ"):
        encode_frame(snapshot, expected_kind="automation")
