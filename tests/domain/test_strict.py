from __future__ import annotations

import math

import pytest

from crowley.domain.strict import (
    StrictValueError,
    canonical_json_bytes,
    exact_keys,
    sha256_hex,
    strict_bool,
    strict_finite_float,
    strict_int,
    strict_json_loads,
    strict_str,
)


def test_strict_int_excludes_bool_and_reports_field_path() -> None:
    assert strict_int(3, minimum=1, maximum=4, field="scan.count") == 3
    with pytest.raises(StrictValueError, match=r"scan\.count: must be an integer"):
        strict_int(True, field="scan.count")


def test_strict_int_bounds_and_string_validation() -> None:
    with pytest.raises(StrictValueError, match="at least 2"):
        strict_int(1, minimum=2, field="count")
    assert strict_str("abc-12", max_len=8, pattern=r"[a-z]+-[0-9]+", field="item.key") == "abc-12"
    with pytest.raises(StrictValueError, match="at most 3"):
        strict_str("abcd", max_len=3, field="name")
    with pytest.raises(StrictValueError, match="does not match"):
        strict_str("ABC", pattern=r"[a-z]+", field="name")


def test_strict_bool_and_finite_float_are_not_coercive() -> None:
    assert strict_bool(False, field="enabled") is False
    assert strict_finite_float(3, field="ratio") == 3.0
    assert math.isfinite(strict_finite_float(1.5, field="ratio"))
    with pytest.raises(StrictValueError, match="enabled: must be a boolean"):
        strict_bool(1, field="enabled")
    with pytest.raises(StrictValueError, match="ratio: must be a finite number"):
        strict_finite_float(float("nan"), field="ratio")
    with pytest.raises(StrictValueError, match="ratio: must be a finite number"):
        strict_finite_float(float("inf"), field="ratio")


def test_exact_keys_requires_required_and_allows_optional() -> None:
    assert exact_keys({"a": 1, "b": 2}, {"a"}, {"b"}) == {"a": 1, "b": 2}
    with pytest.raises(StrictValueError, match="missing keys: a"):
        exact_keys({}, {"a"})
    with pytest.raises(StrictValueError, match="unknown keys: b"):
        exact_keys({"a": 1, "b": 2}, {"a"})


def test_strict_json_rejects_duplicate_non_utf8_nan_and_oversize() -> None:
    assert strict_json_loads(b'{"a":1,"nested":[true]}', 100) == {"a": 1, "nested": [True]}
    with pytest.raises(StrictValueError, match="duplicate JSON key"):
        strict_json_loads(b'{"a":1,"a":2}', 100)
    with pytest.raises(StrictValueError, match="finite JSON number"):
        strict_json_loads(b'{"value":NaN}', 100)
    with pytest.raises(StrictValueError, match="UTF-8"):
        strict_json_loads(b"{\xff}", 100)
    with pytest.raises(StrictValueError, match="size bound"):
        strict_json_loads(b"{}", 1)


@pytest.mark.parametrize("payload", (b"1e999", b"-1e999"))
def test_strict_json_rejects_positive_and_negative_exponent_overflow(payload: bytes) -> None:
    with pytest.raises(StrictValueError, match="non-finite JSON number"):
        strict_json_loads(payload, 6)


def test_strict_json_normalizes_large_integer_and_deep_nesting_errors() -> None:
    with pytest.raises(StrictValueError, match="JSON is malformed"):
        strict_json_loads((b"9" * 5000), 5000)
    nested = b"[" * 10000 + b"0" + b"]" * 10000
    with pytest.raises(StrictValueError, match="JSON nesting exceeds|JSON is malformed"):
        strict_json_loads(nested, len(nested))


def test_canonical_json_matches_existing_digest_form() -> None:
    assert canonical_json_bytes({"z": "é", "a": [1, True]}) == '{"a":[1,true],"z":"é"}'.encode()
    assert sha256_hex(b"payload") == "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"


@pytest.mark.parametrize("value", ((1, 2), {1: "not-a-string-key"}, "\ud800"))
def test_canonical_json_rejects_non_json_values_and_encoding_errors(value: object) -> None:
    with pytest.raises(StrictValueError):
        canonical_json_bytes(value)


def test_canonical_json_rejects_reference_cycles() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(StrictValueError, match="reference cycle"):
        canonical_json_bytes(value)
