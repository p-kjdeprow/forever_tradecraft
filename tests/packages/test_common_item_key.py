from __future__ import annotations

import pytest
from crowley_common.scanformat import ScanFormatError, validate_item_key


@pytest.mark.parametrize(
    "value",
    (
        "1",
        "1:s2",
        "1:b2.2.9",
        "1:s2:b2.2.9:l100:p7",
        "9007199254740991:s9007199254740991",
    ),
)
def test_validate_item_key_accepts_canonical_identity(value: str) -> None:
    assert validate_item_key(value) == value


@pytest.mark.parametrize(
    "value",
    (
        None,
        True,
        "0",
        "01",
        "1:s0",
        "1:b0",
        "1:b3.2",
        "1:l2:s1",
        "1:s1:s2",
        "1:b2..3",
        "1:p9007199254740992",
    ),
)
def test_validate_item_key_rejects_noncanonical_identity(value: object) -> None:
    with pytest.raises(ScanFormatError):
        validate_item_key(value)
