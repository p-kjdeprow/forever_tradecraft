"""Persistence and read models for market sweeps and observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from crowley.core.house import House

from ._types import Connection, JsonObject
from .houses import ensure_house


class SweepConflictError(Exception):
    """A sweep UID is already claimed by incompatible content."""


class LegacySweepConflictError(SweepConflictError):
    """A new sweep UID collides with a retained legacy scan."""


class SweepContentConflictError(SweepConflictError):
    """A sweep UID is already claimed by a different content digest."""


@dataclass(frozen=True, slots=True)
class SweepClaim:
    """Result of checking the cross-version sweep UID namespace."""

    duplicate: bool
    sweep_id: int | None


@dataclass(frozen=True, slots=True)
class SweepWriteResult:
    """Database identifiers created for a new sweep."""

    sweep_id: int
    house_id: int


def claim_sweep(conn: Connection, envelope_id: str, digest: str) -> SweepClaim:
    """Serialize and validate a sweep UID claim without opening a transaction."""

    with conn.cursor() as cur:
        # The caller owns the surrounding transaction.  The lock is retained
        # until that transaction ends so the legacy and modern namespaces are
        # checked atomically with the eventual insert.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (envelope_id,))
        legacy = cur.execute("SELECT 1 FROM scans WHERE scan_uid=%s", (envelope_id,)).fetchone()
        if legacy is not None:
            raise LegacySweepConflictError("sweep UID collides with legacy scan")
        existing = cur.execute(
            "SELECT id, content_digest FROM market_sweeps WHERE sweep_uid=%s FOR UPDATE", (envelope_id,)
        ).fetchone()
    if existing is None:
        return SweepClaim(duplicate=False, sweep_id=None)
    if existing[1] != digest:
        raise SweepContentConflictError("sweep UID has different content")
    return SweepClaim(duplicate=True, sweep_id=cast(int, existing[0]))


def persist_sweep(
    conn: Connection,
    *,
    envelope_id: str,
    raw: JsonObject,
    decoded: JsonObject,
    plan: JsonObject,
    normalized_observations: dict[str, JsonObject],
    digest: str,
    house: House,
    house_source: str,
    started_at: datetime,
    finished_at: datetime,
    context: JsonObject,
    raw_path: str,
) -> SweepWriteResult:
    """Insert one new sweep and all of its child rows.

    This adapter deliberately uses cursor ``executemany`` for observation and
    captured-row batches.  The caller owns the transaction and archive
    lifecycle.
    """

    raw_values = cast(dict[str, Any], raw)
    decoded_values = cast(dict[str, Any], decoded)
    plan_values = cast(dict[str, Any], plan)
    context_values = cast(dict[str, Any], context)
    observations = cast(dict[str, dict[str, Any]], normalized_observations)
    house_id = ensure_house(conn, house)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_plans (plan_hash, plan_json, plan_body)
            VALUES (%s,%s,%s) ON CONFLICT (plan_hash) DO NOTHING
            """,
            (decoded_values["planHash"], decoded_values["planJSON"], json.dumps(plan_values)),
        )
        sweep_row = cur.execute(
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
                decoded_values["planHash"],
                started_at,
                finished_at,
                context_values["character"],
                context_values["clientVersion"],
                context_values["clientBuild"],
                context_values["interface"],
                house_source,
                raw_values["numRows"],
                raw_values["numObservations"],
                json.dumps(context_values),
                json.dumps(decoded_values),
                digest,
                raw_path,
            ),
        ).fetchone()
        if sweep_row is None:
            raise RuntimeError("sweep insert did not return an id")
        sweep_id = cast(int, sweep_row[0])

        observation_values = [
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
            )
            for item_key, observation in observations.items()
        ]
        cur.executemany(
            """
            INSERT INTO market_observations
              (sweep_id, house_id, item_id, item_key, ordinal, kind, query,
               coverage, source_complete, cutoff, higher_price_witness, reason_code,
               row_count, observation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            observation_values,
        )

        row_values = [
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
            )
            for observation in observations.values()
            for row_key, row in observation["rows"].items()
        ]
        if row_values:
            cur.executemany(
                """
                INSERT INTO market_captured_rows
                  (sweep_id,item_id,row_key,result_id,item_key,raw_item_key,quantity,
                   unit_numerator,unit_denominator,row_data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                row_values,
            )
    return SweepWriteResult(sweep_id=sweep_id, house_id=house_id)


def insert_owned(conn: Connection, sweep_id: int, house_id: int, character: str, owned: JsonObject) -> None:
    """Insert the owned-auction observation for a sweep."""

    owned_values = cast(dict[str, Any], owned)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO owned_observations
              (sweep_id, house_id, character, status, query, reason_code, row_count, rows)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                sweep_id,
                house_id,
                character,
                owned_values["status"],
                json.dumps(owned_values["query"]),
                owned_values["reasonCode"],
                owned_values["rowCount"],
                json.dumps(owned_values["rows"]),
            ),
        )


def insert_sold_evidence(
    conn: Connection,
    sweep_id: int,
    house_id: int,
    character: str,
    owned: JsonObject,
    owned_finished: datetime,
) -> None:
    """Merge sold-auction evidence while preserving first-observation order."""

    owned_values = cast(dict[str, Any], owned)
    if owned_values["status"] != "full" or owned_values["query"].get("kind") != "queried":
        return
    window = {
        "startedAt": owned_values["query"]["startedAt"],
        "finishedAt": owned_values["query"]["finishedAt"],
    }
    with conn.cursor() as cur:
        for row in owned_values["rows"].values():
            if row["status"] != "sold":
                continue
            buyout = row["buyout"].get("copper") if row["buyout"]["kind"] == "known" else None
            existing = cur.execute(
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
                cur.execute(
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
                cur.execute(
                    """
                    INSERT INTO own_sale_evidence
                      (house_id, character, auction_id, first_seen_sold_at, first_seen_sweep_id,
                       item_id, item_key, raw_item_key, quantity, listed_buyout, item_link,
                       observation_window)
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


def record_probe(conn: Connection, probed_at: datetime, probe: JsonObject) -> None:
    """Persist a validated client probe idempotently."""

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO client_probes (probed_at, probe) VALUES (%s, %s) ON CONFLICT (probed_at) DO NOTHING",
            (probed_at, json.dumps(probe)),
        )


def acknowledged_scan_ids(conn: Connection, scan_ids: list[str]) -> list[str]:
    """Return legacy and modern scan IDs known to the server."""

    with conn.cursor() as cur:
        rows = cur.execute(
            """
            SELECT scan_uid FROM scans WHERE scan_uid = ANY(%s)
            UNION
            SELECT sweep_uid FROM market_sweeps WHERE sweep_uid = ANY(%s)
            """,
            (scan_ids, scan_ids),
        ).fetchall()
    return [cast(str, row[0]) for row in rows]
