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


class SweepConflictError(Exception):
    """The UID is already claimed by a different logical sweep."""


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
    row = conn.execute(
        "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key RETURNING id",
        (house.key, house.region, house.ruleset, house.faction),
    ).fetchone()
    if row is None:
        raise RuntimeError("house upsert did not return an id")
    return row[0]


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
    conn.execute(
        """
        INSERT INTO owned_observations
          (sweep_id, house_id, character, status, query, reason_code, row_count, rows)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            sweep_id,
            house_id,
            character,
            owned["status"],
            json.dumps(owned["query"]),
            owned["reasonCode"],
            owned["rowCount"],
            json.dumps(owned["rows"]),
        ),
    )


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
    # The query interval is the evidence window.  The sweep interval may be
    # much wider because owned auctions are collected after market items.
    window = {
        "startedAt": owned["query"]["startedAt"],
        "finishedAt": owned["query"]["finishedAt"],
    }
    for row in owned["rows"].values():
        if row["status"] != "sold":
            continue
        buyout = row["buyout"].get("copper") if row["buyout"]["kind"] == "known" else None
        existing = conn.execute(
            """
            SELECT quantity, listed_buyout, item_id, item_key, raw_item_key, item_link,
                   first_seen_sold_at
            FROM own_sale_evidence
            WHERE house_id=%s AND character=%s AND auction_id=%s FOR UPDATE
            """,
            (house_id, character, row["auctionID"]),
        ).fetchone()
        inconsistent = bool(
            existing
            and (
                existing[0] != row["quantity"]
                or existing[1] != buyout
                or existing[2] != row["itemID"]
                or existing[3] != row["itemKey"]
                or existing[4] != row["rawItemKey"]
                or existing[5] != row["itemLink"]
            )
        )
        if existing:
            conn.execute(
                """
                UPDATE own_sale_evidence
                SET inconsistent = inconsistent OR %s,
                    first_seen_sweep_id = CASE
                        WHEN first_seen_sold_at > %s THEN %s ELSE first_seen_sweep_id END,
                    observation_window = CASE
                        WHEN first_seen_sold_at > %s THEN %s ELSE observation_window END,
                    first_seen_sold_at = CASE
                        WHEN first_seen_sold_at > %s THEN %s ELSE first_seen_sold_at END
                WHERE house_id=%s AND character=%s AND auction_id=%s
                """,
                (
                    inconsistent,
                    owned_finished,
                    sweep_id,
                    owned_finished,
                    json.dumps(window),
                    owned_finished,
                    owned_finished,
                    house_id,
                    character,
                    row["auctionID"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO own_sale_evidence
                  (house_id, character, auction_id, first_seen_sold_at, first_seen_sweep_id,
                   item_id, item_key, raw_item_key, quantity, listed_buyout, item_link, observation_window)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    house_id,
                    character,
                    row["auctionID"],
                    owned_finished,
                    sweep_id,
                    row["itemID"],
                    row["itemKey"],
                    json.dumps(row["rawItemKey"]),
                    row["quantity"],
                    buyout,
                    row["itemLink"],
                    json.dumps(window),
                ),
            )


def ingest_market_sweep(conn: psycopg.Connection, raw: dict[str, Any], data_dir: Path) -> IngestResult:
    """Validate and atomically persist one accepted-format sweep."""
    try:
        validated = validate_envelope(raw)
    except ScanFormatError:
        raise
    except Exception as exc:
        # A malformed nested JSON value must never become a 500 (and must not
        # reach archive or database code).  The wire validator intentionally
        # reports its own domain errors; this catches type errors from every
        # other malformed shape.
        raise ScanFormatError("malformed format-2-or-3 envelope") from exc
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
    with conn.transaction():
        # The advisory lock serializes the cross-table legacy/v2 UID claim.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (envelope_id,))
        legacy = conn.execute("SELECT 1 FROM scans WHERE scan_uid=%s", (envelope_id,)).fetchone()
        existing = conn.execute(
            "SELECT content_digest, house_id FROM market_sweeps WHERE sweep_uid=%s FOR UPDATE", (envelope_id,)
        ).fetchone()
        if legacy:
            raise SweepConflictError("sweep UID collides with legacy scan")
        if existing:
            if existing[0] != digest:
                raise SweepConflictError("sweep UID has different content")
            # A previous transaction may have committed the DB row before a
            # filesystem restore.  Recreate only this content-addressed path;
            # never overwrite a different archive.
            _archive_envelope(data_dir, raw, digest)
            return IngestResult(
                envelope_id, house.key, "duplicate", raw["numRows"], raw["numObservations"], raw["format"]
            )
        # The archive is created only after validation and UID conflict checks.
        # It is still inside the DB transaction, so an insert failure rolls the
        # database back; the immutable archive is then simply unreferenced.
        path = _archive_envelope(data_dir, raw, digest)
        house_id = ensure_house(conn, house)
        conn.execute(
            """
            INSERT INTO market_plans (plan_hash, plan_json, plan_body)
            VALUES (%s,%s,%s) ON CONFLICT (plan_hash) DO NOTHING
            """,
            (decoded["planHash"], decoded["planJSON"], json.dumps(plan)),
        )
        sweep_row = conn.execute(
            """
            INSERT INTO market_sweeps
              (sweep_uid, house_id, plan_hash, started_at, finished_at, character,
               client_version, client_build, interface, house_source, num_rows,
               num_observations, context, payload, content_digest, raw_path)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                envelope_id,
                house_id,
                decoded["planHash"],
                started_at,
                finished_at,
                context["character"],
                context["clientVersion"],
                context["clientBuild"],
                context["interface"],
                house_raw["source"],
                raw["numRows"],
                raw["numObservations"],
                json.dumps(context),
                json.dumps(decoded),
                digest,
                str(path),
            ),
        ).fetchone()
        if sweep_row is None:
            raise RuntimeError("sweep insert did not return an id")
        sweep_id = sweep_row[0]
        for item_key, observation in normalized_observations.items():
            conn.execute(
                """
                INSERT INTO market_observations
                  (sweep_id, house_id, item_id, item_key, ordinal, kind, query,
                   coverage, source_complete, cutoff, higher_price_witness, reason_code,
                   row_count, observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    sweep_id,
                    house_id,
                    observation["itemID"],
                    item_key,
                    observation["ordinal"],
                    observation["kind"],
                    json.dumps(observation["query"]),
                    observation["coverage"],
                    observation["sourceComplete"],
                    json.dumps(observation["cutoff"]),
                    json.dumps(observation["higherPriceWitness"]),
                    observation["reasonCode"],
                    observation["rowCount"],
                    json.dumps(observation),
                ),
            )
            for row_key, row in observation["rows"].items():
                conn.execute(
                    """
                    INSERT INTO market_captured_rows
                      (sweep_id,item_id,row_key,result_id,item_key,raw_item_key,quantity,
                       unit_numerator,unit_denominator,row_data)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        sweep_id,
                        observation["itemID"],
                        row_key,
                        row["resultID"],
                        row["itemKey"],
                        json.dumps(row["rawItemKey"]),
                        row["quantity"],
                        row["unitPrice"]["numerator"],
                        row["unitPrice"]["denominator"],
                        json.dumps(row),
                    ),
                )
        _insert_owned(conn, sweep_id, house_id, context["character"], decoded["owned"])
        _insert_sold_evidence(conn, sweep_id, house_id, context["character"], decoded["owned"])
    return IngestResult(
        envelope_id, house.key, "created", raw["numRows"], raw["numObservations"], raw["format"]
    )
