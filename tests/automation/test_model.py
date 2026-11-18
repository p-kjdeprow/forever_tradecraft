from __future__ import annotations

import hashlib
import json

import pytest
from crowley_automation.model import (
    MAX_BATCH_BYTES,
    MAX_INTEGER,
    Batch,
    BatchError,
    CancelAuction,
    CollectMail,
    Interact,
    MoveRoute,
    PostCommodity,
    PostItem,
    validate_item_key,
)


def batch_json(action: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "house": "us-normal-horde",
            "character": "Tester",
            "expiresAt": 2_000_000_000,
            "actions": [action or {"kind": "collect_mail", "messageLimit": 4}],
        }
    )


def test_round_trip_supported_actions_and_canonical_digest() -> None:
    actions = (
        PostCommodity("123:s5:b2.2:l10", 2, 1234, 24, 400),
        PostItem("456:p7", 1, 999, 2, 12),
        CancelAuction(42, 500),
        CollectMail(3),
        Interact("auctioneer_main"),
        MoveRoute("bank_path"),
    )
    batch = Batch("us-normal-horde", "Tester", 2_000_000_000, actions)

    parsed = Batch.from_json(batch.canonical)

    assert parsed == batch
    assert parsed.batch_id == hashlib.sha256(parsed.canonical.encode()).hexdigest()
    assert len(parsed.batch_id) == 64


@pytest.mark.parametrize(
    "source",
    [
        '{"schemaVersion":1,"schemaVersion":1}',
        '{"schemaVersion":1,"house":"us-normal-horde","character":"Tester",'
        '"expiresAt":2000000000,"actions":[{"kind":"collect_mail","kind":"collect_mail",'
        '"messageLimit":1}]}',
        batch_json({"kind": "collect_mail", "messageLimit": True}),
        batch_json({"kind": "collect_mail", "messageLimit": 1.0}),
        batch_json({"kind": "collect_mail", "messageLimit": 1, "unexpected": 0}),
        batch_json({"kind": "buy_item", "itemKey": "123"}),
        batch_json({"kind": "interact", "targetId": "Auctioneer"}),
        batch_json({"kind": "post_item", "itemKey": "123", "quantity": 1}),
        '{"schemaVersion":1,"house":"us-normal-horde","character":"Tester",'
        '"expiresAt":2000000000.5,"actions":[{"kind":"collect_mail","messageLimit":1}]}',
    ],
)
def test_rejects_noncanonical_or_ambiguous_json(source: str) -> None:
    with pytest.raises(BatchError):
        Batch.from_json(source)


def test_rejects_nonfinite_numbers_and_oversized_batch() -> None:
    with pytest.raises(BatchError, match="non-integer"):
        Batch.from_json(batch_json().replace('"messageLimit": 4', '"messageLimit": NaN'))
    with pytest.raises(BatchError, match="exceeds"):
        Batch.from_json(" " * (MAX_BATCH_BYTES + 1))
    with pytest.raises(BatchError, match="UTF-8"):
        Batch.from_json(batch_json().encode("utf-16"))
    with pytest.raises(BatchError, match="UTF-8"):
        Batch.from_json(b"{\xff}")


@pytest.mark.parametrize("item_key", ["0", "001", "123:b3.2", "123:x1", "123:s0", "123:b1..2"])
def test_rejects_bad_item_identity(item_key: str) -> None:
    with pytest.raises(BatchError):
        validate_item_key(item_key)


def test_action_numeric_bounds_are_exact_and_safe() -> None:
    with pytest.raises(BatchError):
        PostCommodity("123", True, 1, 2, 0)
    with pytest.raises(BatchError, match="safe-integer"):
        PostItem("123", 2, MAX_INTEGER // 2 + 1, 2, 0)
    with pytest.raises(BatchError):
        CancelAuction(2**53, 0)
    with pytest.raises(BatchError):
        CollectMail(51)


def test_batch_order_changes_identity_and_constructor_is_immutable() -> None:
    a = CollectMail(1)
    b = CancelAuction(2, 0)
    left = Batch("us-normal-horde", "Tester", 100, (a, b))
    right = Batch("us-normal-horde", "Tester", 100, (b, a))

    assert left.batch_id != right.batch_id
    with pytest.raises(BatchError, match="tuple"):
        Batch("us-normal-horde", "Tester", 100, [a])  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["post_item", "post_commodity"])
def test_missing_duration_defaults_to_two_hours_and_canonicalizes_before_confirmation(kind):
    action = {"kind": kind, "itemKey": "123", "quantity": 2, "unitPriceCopper": 100, "maxDepositCopper": 50}
    defaulted = Batch.from_json(batch_json(action))
    explicit = Batch.from_json(batch_json({**action, "durationHours": 2}))
    assert defaulted == explicit
    assert defaulted.batch_id == explicit.batch_id
    assert defaulted.object()["actions"][0]["durationHours"] == 2
    for invalid in (None, True, 12, 48):
        with pytest.raises(BatchError):
            Batch.from_json(batch_json({**action, "durationHours": invalid}))
