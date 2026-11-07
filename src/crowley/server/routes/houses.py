from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row

from crowley.core.volume import NORMALIZATION_POLICY, project_observation_variant, volume_report

from ..deps import Authed
from ._shared import _house_id

router = APIRouter()


def _base_item_id(item_key: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(?::.*)?", item_key)
    if not match:
        raise HTTPException(status_code=400, detail="item key must start with a positive item ID")
    return int(match.group(1))


@router.get("/v1/houses", dependencies=[Authed])
def list_houses(request: Request) -> list[dict[str, Any]]:
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT h.key, h.region, h.ruleset, h.faction,
                   count(DISTINCT s.id) AS legacy_scans,
                   count(DISTINCT ms.id) AS sweeps,
                   count(DISTINCT s.id) + count(DISTINCT ms.id) AS scans,
                   greatest(max(s.scanned_at), max(ms.finished_at)) AS last_scan_at,
                   (SELECT num_listings FROM scans
                     WHERE house_id = h.id ORDER BY scanned_at DESC LIMIT 1) AS last_scan_listings
            FROM houses h LEFT JOIN scans s ON s.house_id = h.id
              LEFT JOIN market_sweeps ms ON ms.house_id = h.id
            GROUP BY h.id ORDER BY h.key
            """
        ).fetchall()


@router.get("/v1/houses/{key}/latest", dependencies=[Authed])
def latest_prices(
    key: str,
    request: Request,
    search: str | None = None,
    limit: int = Query(default=100, le=5000),
) -> dict[str, Any]:
    """Latest market observations plus a separate legacy full-scan section."""
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
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
    if not scan and not latest_market:
        raise HTTPException(status_code=404, detail="house has no scans")
    return {
        "house": key,
        "scan": scan,
        "items": legacy_rows,
        "legacy_full_scan": {"scan": scan, "items": legacy_rows} if scan else None,
        "legacyPoints": legacy_rows,
        "sweep": latest_market,
        "observations": market_rows,
    }


@router.get("/v1/houses/{key}/items/{item_key}/history", dependencies=[Authed])
def item_history(
    key: str, item_key: str, request: Request, days: int = Query(default=7, le=365)
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
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
    market = []
    for row in market_rows:
        projected = project_observation_variant(row["observation"], item_key)
        market.append({**row, "observation": projected, "row_count": projected["rowCount"]})
    return {
        "house": key,
        "item": item,
        "legacy_full_scan": {"points": points} if points else None,
        "legacyPoints": points,
        "observations": market,
    }


@router.get("/v1/houses/{key}/items/{item_key}/volume", dependencies=[Authed])
def item_volume(
    key: str,
    item_key: str,
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.house_id, ms.started_at, ms.finished_at,
                   mo.item_key, ms.context, ms.plan_hash, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE ms.house_id = %s AND mo.item_id = %s
            ORDER BY ms.started_at, (mo.observation->>'ordinal')::int, ms.sweep_uid
            """,
            (house_id, item_id),
        ).fetchall()
        own_sales = cur.execute(
            """
            SELECT auction_id, character, first_seen_sold_at, quantity, listed_buyout,
                   item_id, item_key, raw_item_key, observation_window, inconsistent
            FROM own_sale_evidence
            WHERE house_id = %s AND item_id = %s AND first_seen_sold_at >= %s
            ORDER BY first_seen_sold_at
            """,
            (house_id, item_id, since),
        ).fetchall()
    records = []
    for row in rows:
        # Keep the complete base-item scope for grouping and persistent-ID
        # checks.  volume_report applies the exact variant filter while it
        # still sees grouped rows and variants outside the requested prefix.
        observation = row["observation"]
        records.append(
            {
                "observation": observation,
                "envelope": {
                    "house": {"key": key},
                    "itemKey": item_key,
                    "context": row["context"],
                    "planHash": row["plan_hash"],
                    "startedAt": int(row["started_at"].timestamp()),
                    "finishedAt": int(row["finished_at"].timestamp()),
                    "sweepStart": int(row["started_at"].timestamp()),
                    "ordinal": observation["ordinal"],
                    "sweep_uid": row["sweep_uid"],
                },
            }
        )
    own_sales = [row for row in own_sales if row["item_key"] == item_key]
    report = volume_report(records, hours=hours, end=int(time.time()), item_key=item_key)
    report["house"] = key
    report["itemKey"] = item_key
    report["normalizationPolicy"] = NORMALIZATION_POLICY
    report["ownConfirmed"] = {
        "units": sum(row["quantity"] for row in own_sales if not row["inconsistent"]),
        "evidence": own_sales,
        "executionTimeKnown": False,
    }
    return report
