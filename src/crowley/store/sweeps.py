"""Persistence and read models for market sweeps and observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from psycopg.rows import dict_row

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


VOLUME_QUERY = """
    SELECT ms.sweep_uid, ms.house_id, ms.started_at, ms.finished_at,
           mo.item_key, ms.context, ms.plan_hash, mo.ordinal, mo.observation
    FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
    WHERE ms.house_id = %s
      AND mo.item_key = %s
      AND ms.finished_at >= %s
    ORDER BY ms.started_at, mo.ordinal, ms.sweep_uid
"""

LATEST_VOLUME_QUERY = """
    SELECT ms.sweep_uid, ms.house_id, ms.started_at, ms.finished_at,
           mo.item_key, ms.context, ms.plan_hash, mo.ordinal, mo.observation
    FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
    WHERE ms.house_id = %s AND mo.item_key = %s
    ORDER BY COALESCE(
                 CASE WHEN mo.observation #>> '{query,kind}' = 'queried'
                      THEN to_timestamp((mo.observation #>> '{query,finishedAt}')::double precision)
                 END,
                 ms.finished_at
             ) DESC,
             ms.started_at DESC,
             mo.ordinal DESC,
             ms.sweep_uid DESC
    LIMIT 1
"""


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


def latest_prices(
    conn: Connection, house_id: int, search: str | None, limit: int
) -> tuple[JsonObject | None, list[JsonObject], list[JsonObject], JsonObject | None]:
    """Return the legacy and modern latest-price read models."""

    with conn.cursor(row_factory=dict_row) as cur:
        scan = cur.execute(
            "SELECT id, scan_uid, scanned_at, num_listings FROM scans"
            " WHERE house_id = %s ORDER BY scanned_at DESC LIMIT 1",
            (house_id,),
        ).fetchone()
        legacy_rows = cur.execute(
            """
            SELECT st.item_key, st.item_id, i.name, i.quality, st.num_auctions, st.total_quantity,
                   st.min_unit, st.p25_unit, st.median_unit, st.market_value
            FROM item_stats st JOIN items i USING (item_key)
            WHERE st.scan_id = %s AND st.scanned_at = %s
              AND (%s::text IS NULL OR i.name ILIKE '%%' || %s || '%%')
            ORDER BY st.total_quantity DESC
            LIMIT %s
            """,
            (scan["id"], scan["scanned_at"], search, search, limit)
            if scan
            else (None, None, search, search, limit),
        ).fetchall()
        market_rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.started_at, ms.finished_at, mo.ordinal, mo.item_id,
                   mo.item_key, mo.kind, mo.coverage, mo.source_complete, mo.cutoff,
                   mo.higher_price_witness, mo.reason_code, mo.row_count, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE ms.house_id = %s
              AND ms.id = (SELECT id FROM market_sweeps WHERE house_id = %s
                           ORDER BY finished_at DESC LIMIT 1)
              AND (%s::text IS NULL OR mo.item_key ILIKE '%%' || %s || '%%')
            ORDER BY ms.finished_at DESC, mo.ordinal
            LIMIT %s
            """,
            (house_id, house_id, search, search, limit),
        ).fetchall()
        latest_market = cur.execute(
            """
            SELECT sweep_uid, started_at, finished_at, plan_hash,
                   num_observations, num_rows
            FROM market_sweeps
            WHERE house_id = %s
            ORDER BY finished_at DESC LIMIT 1
            """,
            (house_id,),
        ).fetchone()
    return (
        None if scan is None else cast(JsonObject, dict(scan)),
        [cast(JsonObject, dict(row)) for row in legacy_rows],
        [cast(JsonObject, dict(row)) for row in market_rows],
        None if latest_market is None else cast(JsonObject, dict(latest_market)),
    )


def item_history_rows(
    conn: Connection, house_id: int, item_key: str, item_id: int, since: datetime
) -> tuple[JsonObject | None, list[JsonObject], list[JsonObject]]:
    """Return item metadata, legacy history, and bounded observation history."""

    with conn.cursor(row_factory=dict_row) as cur:
        item = cur.execute(
            "SELECT item_key, item_id, name, quality, level FROM items WHERE item_key = %s",
            (item_key,),
        ).fetchone()
        points = cur.execute(
            """
            SELECT scanned_at, num_auctions, total_quantity, min_unit, p25_unit, median_unit,
                   market_value
            FROM item_stats
            WHERE house_id = %s AND item_key = %s AND scanned_at >= %s
            ORDER BY scanned_at
            """,
            (house_id, item_key, since),
        ).fetchall()
        market_rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.started_at, ms.finished_at, mo.ordinal, mo.item_id,
                   mo.item_key, mo.kind, mo.coverage, mo.source_complete, mo.cutoff,
                   mo.reason_code, mo.row_count, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE mo.house_id = %s AND mo.item_id = %s AND ms.finished_at >= %s
            ORDER BY ms.started_at, mo.ordinal, ms.sweep_uid
            """,
            (house_id, item_id, since),
        ).fetchall()
    return (
        None if item is None else cast(JsonObject, dict(item)),
        [cast(JsonObject, dict(row)) for row in points],
        [cast(JsonObject, dict(row)) for row in market_rows],
    )


def volume_rows(
    conn: Connection,
    house_id: int,
    base_item_key: str,
    item_key: str,
    observation_since: datetime,
    sales_since: datetime,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Read bounded interval rows plus one row for the latest read model.

    ``volume_report`` computes interval evidence from the bounded window but
    deliberately reports freshness from the latest observation regardless of
    the requested interval. The second query is one indexed latest-row lookup,
    not an unbounded history scan.
    """

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            VOLUME_QUERY,
            (house_id, base_item_key, observation_since),
        ).fetchall()
        latest = cur.execute(
            LATEST_VOLUME_QUERY,
            (house_id, base_item_key),
        ).fetchone()
        own_sales = cur.execute(
            """
            SELECT auction_id, character, first_seen_sold_at, quantity, listed_buyout,
                   item_id, item_key, raw_item_key, observation_window, inconsistent
            FROM own_sale_evidence
            WHERE house_id = %s AND item_key = %s AND first_seen_sold_at >= %s
            ORDER BY first_seen_sold_at
            """,
            (house_id, item_key, sales_since),
        ).fetchall()
    bounded_rows = [cast(JsonObject, dict(row)) for row in rows]
    if latest is not None:
        latest_row = cast(JsonObject, dict(latest))
        if not any(
            row["sweep_uid"] == latest_row["sweep_uid"] and row["ordinal"] == latest_row["ordinal"]
            for row in bounded_rows
        ):
            bounded_rows.append(latest_row)
    return (
        bounded_rows,
        [cast(JsonObject, dict(row)) for row in own_sales],
    )
