from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from crowley.core.volume import (
    MAX_INTERVAL_SECONDS,
    NORMALIZATION_POLICY,
    project_observation_variant,
    volume_report,
)
from crowley.store.houses import list_houses as store_list_houses
from crowley.store.sweeps import item_history_rows, volume_rows
from crowley.store.sweeps import latest_prices as store_latest_prices

from ._shared import _house_id

router = APIRouter()


def _base_item_id(item_key: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(?::.*)?", item_key)
    if not match:
        raise HTTPException(status_code=400, detail="item key must start with a positive item ID")
    return int(match.group(1))


@router.get("/v1/houses")
def list_houses(request: Request) -> list[dict[str, Any]]:
    with request.app.state.pool.connection() as conn:
        return store_list_houses(conn)


@router.get("/v1/houses/{key}/latest")
def latest_prices(
    key: str,
    request: Request,
    search: str | None = None,
    limit: int = Query(default=100, le=5000),
) -> dict[str, Any]:
    """Latest market observations plus a separate legacy full-scan section."""
    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        scan, legacy_rows, market_rows, latest_market = store_latest_prices(conn, house_id, search, limit)
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


@router.get("/v1/houses/{key}/items/{item_key}/history")
def item_history(
    key: str, item_key: str, request: Request, days: int = Query(default=7, le=365)
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        item, points, market_rows = item_history_rows(conn, house_id, item_key, item_id, since)
    market = []
    for row in market_rows:
        projected = project_observation_variant(cast(dict[str, Any], row["observation"]), item_key)
        market.append({**row, "observation": projected, "row_count": projected["rowCount"]})
    return {
        "house": key,
        "item": item,
        "legacy_full_scan": {"points": points} if points else None,
        "legacyPoints": points,
        "observations": market,
    }


@router.get("/v1/houses/{key}/items/{item_key}/volume")
def item_volume(
    key: str,
    item_key: str,
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    read_since = since - timedelta(seconds=MAX_INTERVAL_SECONDS)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        rows, own_sales = volume_rows(conn, house_id, str(item_id), item_key, read_since, since)
    records = []
    for row in rows:
        # Keep the complete base-item scope for grouping and persistent-ID
        # checks.  volume_report applies the exact variant filter while it
        # still sees grouped rows and variants outside the requested prefix.
        observation = row["observation"]
        records.append(
            {
                "observation": cast(dict[str, Any], observation),
                "envelope": {
                    "house": {"key": key},
                    "itemKey": item_key,
                    "context": cast(dict[str, Any], row["context"]),
                    "planHash": row["plan_hash"],
                    "startedAt": int(cast(datetime, row["started_at"]).timestamp()),
                    "finishedAt": int(cast(datetime, row["finished_at"]).timestamp()),
                    "sweepStart": int(cast(datetime, row["started_at"]).timestamp()),
                    "ordinal": cast(dict[str, Any], observation)["ordinal"],
                    "sweep_uid": row["sweep_uid"],
                },
            }
        )
    report = volume_report(records, hours=hours, end=int(time.time()), item_key=item_key)
    report["house"] = key
    report["itemKey"] = item_key
    report["normalizationPolicy"] = NORMALIZATION_POLICY
    report["ownConfirmed"] = {
        "units": sum(cast(int, row["quantity"]) for row in own_sales if not cast(bool, row["inconsistent"])),
        "evidence": own_sales,
        "executionTimeKnown": False,
    }
    return report
