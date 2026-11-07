from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


def _strict_json_object(body: bytes) -> dict[str, Any]:
    """Decode one upload body while rejecting duplicate keys at every depth."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise ValueError(f"invalid JSON constant {value}")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="malformed JSON upload") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="upload body must be a JSON object")
    return value
