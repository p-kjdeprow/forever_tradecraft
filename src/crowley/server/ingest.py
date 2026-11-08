"""Validate and persist market sweeps atomically."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from crowley.core.house import House
from crowley.core.scanformat import ScanFormatError, normalize_observation, validate_envelope
from crowley.store.houses import ensure_house as _store_ensure_house
from crowley.store.sweeps import (
    SweepConflictError as _SweepConflictError,
)
from crowley.store.sweeps import (
    claim_sweep,
    insert_owned,
    insert_sold_evidence,
    persist_sweep,
)

SweepConflictError = _SweepConflictError


@dataclass
class IngestResult:
    scan_uid: str
    house: str
    status: str  # created | duplicate
    listings: int
    items: int
    format: int = 3


def _ts(epoch: int) -> datetime:
    # Validate this before touching the archive.  ``fromtimestamp`` raises a
    # platform-specific exception for values outside the PostgreSQL timestamp
    # range, and those must be an ordinary 422 rather than a half-archived
    # upload.
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise ScanFormatError("timestamp must be an integer epoch")
    try:
        value = datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ScanFormatError("timestamp is not storable") from exc
    if value.year < 1 or value.year > 9999:
        raise ScanFormatError("timestamp is not storable")
    return value


def ensure_house(conn: psycopg.Connection, house: House) -> int:
    """Compatibility wrapper for callers migrating to the houses adapter."""

    return _store_ensure_house(conn, house)


def _logical_digest(envelope: dict[str, Any], decoded: dict[str, Any]) -> str:
    logical = {key: value for key, value in envelope.items() if key != "payload"}
    logical["payload"] = decoded
    text = json.dumps(logical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _archive_envelope(data_dir: Path, envelope: dict[str, Any], digest: str) -> Path:
    path = data_dir / "raw" / "format2" / digest[:2] / f"{digest}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb") as zipped:
                zipped.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def _validate_storable_query_times(decoded: dict[str, Any]) -> None:
    """Check every persisted query epoch before an archive can be created."""
    for observation in decoded["observations"].values():
        query = observation["query"]
        if query["kind"] == "queried":
            _ts(query["startedAt"])
            _ts(query["finishedAt"])
    query = decoded["owned"]["query"]
    if query["kind"] == "queried":
        _ts(query["startedAt"])
        _ts(query["finishedAt"])


def _insert_owned(
    conn: psycopg.Connection, sweep_id: int, house_id: int, character: str, owned: dict[str, Any]
) -> None:
    """Compatibility seam delegating owned-observation SQL to the adapter."""

    insert_owned(conn, sweep_id, house_id, character, owned)


def _insert_sold_evidence(
    conn: psycopg.Connection,
    sweep_id: int,
    house_id: int,
    character: str,
    owned: dict[str, Any],
) -> None:
    if owned["status"] != "full" or owned["query"].get("kind") != "queried":
        return
    owned_finished = _ts(owned["query"]["finishedAt"])
    insert_sold_evidence(conn, sweep_id, house_id, character, owned, owned_finished)


def _persist_market_sweep(conn: psycopg.Connection, raw: dict[str, Any], data_dir: Path) -> IngestResult:
    """Validate and persist one accepted-format sweep in the caller's transaction."""
    validated = validate_envelope(raw)
    decoded = validated["decoded"]
    plan = validated["plan"]
    normalized_observations = {
        item_key: normalize_observation(raw["format"], observation["kind"], observation)
        for item_key, observation in decoded["observations"].items()
    }
    digest = _logical_digest(raw, decoded)
    envelope_id = raw["id"]
    house_raw = raw["house"]
    try:
        house = House(house_raw["region"], house_raw["ruleset"], house_raw["faction"])
    except ValueError as exc:
        raise ScanFormatError(str(exc)) from exc
    if house.key != house_raw["key"]:
        raise ScanFormatError("house key does not match its fields")
    context = raw["context"]
    # Check both envelope timestamps before the first filesystem operation.
    started_at = _ts(raw["startedAt"])
    finished_at = _ts(raw["finishedAt"])
    _validate_storable_query_times(decoded)
    claim = claim_sweep(conn, envelope_id, digest)
    if claim.duplicate:
        # A previous transaction may have committed the DB row before a
        # filesystem restore. Recreate only this content-addressed path.
        _archive_envelope(data_dir, raw, digest)
        return IngestResult(
            envelope_id, house.key, "duplicate", raw["numRows"], raw["numObservations"], raw["format"]
        )

    # The archive is created only after validation and UID conflict checks.
    # It is still inside the caller's DB transaction, so an insert failure
    # rolls the database back; the immutable archive is then unreferenced.
    path = _archive_envelope(data_dir, raw, digest)
    write = persist_sweep(
        conn,
        envelope_id=envelope_id,
        raw=raw,
        decoded=decoded,
        plan=plan,
        normalized_observations=normalized_observations,
        digest=digest,
        house=house,
        house_source=house_raw["source"],
        started_at=started_at,
        finished_at=finished_at,
        context=context,
        raw_path=str(path),
    )
    _insert_owned(conn, write.sweep_id, write.house_id, context["character"], decoded["owned"])
    _insert_sold_evidence(conn, write.sweep_id, write.house_id, context["character"], decoded["owned"])
    return IngestResult(
        envelope_id, house.key, "created", raw["numRows"], raw["numObservations"], raw["format"]
    )


def ingest_market_sweep(conn: psycopg.Connection, raw: dict[str, Any], data_dir: Path) -> IngestResult:
    """Validate and persist a sweep in the caller-owned transaction."""

    return _persist_market_sweep(conn, raw, data_dir)
