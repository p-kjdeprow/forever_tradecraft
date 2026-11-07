"""Strict, path-aware validation and canonical JSON primitives."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from re import Pattern
from typing import TypeGuard, cast

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_integer(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _is_bytes(value: object) -> bool:
    return isinstance(value, bytes)


def _all_strings(values: Iterable[object]) -> bool:
    return all(isinstance(value, str) for value in values)


def _is_string_mapping(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return _all_strings(mapping.keys())


def _is_json_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_json_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


class StrictValueError(ValueError):
    """A strict value failed validation at *field*."""

    def __init__(self, field: str, message: str) -> None:
        if not _is_nonempty_string(field):
            raise ValueError("strict validation field must be a non-empty string")
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _check_bound(value: int | None, *, name: str, field: str) -> None:
    if value is not None and not _is_integer(value):
        raise StrictValueError(field, f"{name} must be an integer")


def strict_int(
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    field: str,
) -> int:
    """Validate an integer, explicitly excluding booleans."""

    _check_bound(minimum, name="minimum", field=field)
    _check_bound(maximum, name="maximum", field=field)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise StrictValueError(field, "minimum must not exceed maximum")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrictValueError(field, "must be an integer")
    if minimum is not None and value < minimum:
        raise StrictValueError(field, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise StrictValueError(field, f"must be at most {maximum}")
    return value


def strict_str(
    value: object,
    *,
    max_len: int | None = None,
    pattern: str | Pattern[str] | None = None,
    field: str,
) -> str:
    """Validate a string length and an optional full-match pattern."""

    if max_len is not None:
        strict_int(max_len, minimum=0, field=f"{field}.max_len")
    if not isinstance(value, str):
        raise StrictValueError(field, "must be a string")
    if max_len is not None and len(value) > max_len:
        raise StrictValueError(field, f"must be at most {max_len} characters")
    if pattern is not None:
        try:
            matched = re.fullmatch(pattern, value)
        except (re.error, TypeError) as exc:
            raise StrictValueError(field, "pattern is invalid") from exc
        if matched is None:
            raise StrictValueError(field, "does not match the required pattern")
    return value


def strict_bool(value: object, *, field: str) -> bool:
    """Validate a JSON boolean without coercion."""

    if not isinstance(value, bool):
        raise StrictValueError(field, "must be a boolean")
    return value


def strict_finite_float(value: object, *, field: str) -> float:
    """Validate a finite JSON number and normalize it to ``float``."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrictValueError(field, "must be a finite number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise StrictValueError(field, "must be a finite number") from exc
    if not math.isfinite(normalized):
        raise StrictValueError(field, "must be a finite number")
    return normalized


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of bytes as hexadecimal text."""

    if not _is_bytes(data):
        raise TypeError("sha256_hex requires bytes")
    return hashlib.sha256(data).hexdigest()


def exact_keys(
    mapping: Mapping[str, object],
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> dict[str, object]:
    """Require exactly the declared required and optional mapping keys."""

    if not _is_string_mapping(mapping):
        raise StrictValueError("$", "must be an object")
    required_keys = frozenset(required)
    optional_keys = frozenset(optional)
    if not _all_strings(required_keys | optional_keys):
        raise StrictValueError("$", "required and optional keys must be strings")
    if not required_keys.isdisjoint(optional_keys):
        raise StrictValueError("$", "required and optional keys must be disjoint")
    actual = set(mapping)
    missing = required_keys - actual
    unknown = actual - required_keys - optional_keys
    if missing:
        names = ", ".join(sorted(missing))
        raise StrictValueError("$", f"missing keys: {names}")
    if unknown:
        names = ", ".join(sorted(unknown))
        raise StrictValueError("$", f"unknown keys: {names}")
    return dict(mapping)


def _reject_constant(value: str) -> None:
    raise StrictValueError("$", f"non-finite JSON number {value}")


def _parse_finite_float(value: str) -> float:
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise StrictValueError("$", f"non-finite JSON number {value}") from exc
    if not math.isfinite(normalized):
        raise StrictValueError("$", f"non-finite JSON number {value}")
    return normalized


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise StrictValueError("$", f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json_loads(data: bytes, max_bytes: int) -> JsonValue:
    """Decode bounded UTF-8 JSON while rejecting duplicate and non-finite values."""

    if not _is_bytes(data):
        raise StrictValueError("$", "JSON input must be bytes")
    if not _is_integer(max_bytes) or max_bytes < 0:
        raise StrictValueError("$", "max_bytes must be a nonnegative integer")
    if len(data) > max_bytes:
        raise StrictValueError("$", "JSON input exceeds its size bound")
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except StrictValueError:
        raise
    except UnicodeDecodeError as exc:
        raise StrictValueError("$", "JSON is not valid UTF-8") from exc
    except RecursionError as exc:
        raise StrictValueError("$", "JSON nesting exceeds the decoder limit") from exc
    except (json.JSONDecodeError, OverflowError, ValueError) as exc:
        raise StrictValueError("$", "JSON is malformed") from exc
    return value


def _validate_json_value(
    value: object, field: str = "$", active_containers: set[int] | None = None
) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictValueError(field, "must be a finite number")
        return value
    if active_containers is None:
        active_containers = set()
    if _is_json_list(value):
        container_id = id(value)
        if container_id in active_containers:
            raise StrictValueError(field, "contains a reference cycle")
        active_containers.add(container_id)
        try:
            return [
                _validate_json_value(item, f"{field}[{index}]", active_containers)
                for index, item in enumerate(value)
            ]
        finally:
            active_containers.remove(container_id)
    if _is_json_dict(value):
        container_id = id(value)
        if container_id in active_containers:
            raise StrictValueError(field, "contains a reference cycle")
        active_containers.add(container_id)
        try:
            normalized: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise StrictValueError(field, "object keys must be strings")
                normalized[key] = _validate_json_value(item, f"{field}.{key}", active_containers)
            return normalized
        finally:
            active_containers.remove(container_id)
    raise StrictValueError(field, "must be a JSON value")


def canonical_json_bytes(value: object) -> bytes:
    """Encode compact sorted-key canonical JSON as UTF-8 with ``ensure_ascii=False``.

    This matches the existing digest form used by ``crowley.ops.backup``
    and the scan payload encoder: sorted keys, compact separators, and literal
    UTF-8 Unicode rather than ASCII escape sequences.
    """

    try:
        normalized = _validate_json_value(value)
        text = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return text.encode("utf-8")
    except StrictValueError:
        raise
    except (TypeError, UnicodeError, ValueError) as exc:
        raise StrictValueError("$", "value is not canonical JSON") from exc


__all__ = [
    "JsonValue",
    "StrictValueError",
    "canonical_json_bytes",
    "exact_keys",
    "sha256_hex",
    "strict_bool",
    "strict_finite_float",
    "strict_int",
    "strict_json_loads",
    "strict_str",
]
