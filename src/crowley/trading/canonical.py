"""Canonical JSON encoding and digests for trading-owned contracts."""

from __future__ import annotations

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    """Encode JSON with stable key ordering and separators."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_canonical_json(value: object) -> str:
    """Return the SHA-256 digest of one canonical JSON value."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = ["canonical_json_bytes", "sha256_canonical_json"]
