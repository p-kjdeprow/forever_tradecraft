from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..deps import Authed

router = APIRouter()


class AddonDataRequest(BaseModel):
    # Scan ids currently sitting in the player's SavedVariables.
    scan_ids: list[str] = []


@router.get("/v1/scan-plan", dependencies=[Authed])
def scan_plan(request: Request) -> dict[str, Any]:
    return dict(request.app.state.canonical_scan_plan)


@router.post("/v1/addon-data", dependencies=[Authed])
def addon_data(body: AddonDataRequest, request: Request) -> dict[str, Any]:
    """Everything the companion writes into Crowley_Data/Data.lua."""
    acked: list[str] = []
    if body.scan_ids:
        with request.app.state.pool.connection() as conn:
            acked = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT scan_uid FROM scans WHERE scan_uid = ANY(%s)
                    UNION
                    SELECT sweep_uid FROM market_sweeps WHERE sweep_uid = ANY(%s)
                    """,
                    (body.scan_ids, body.scan_ids),
                )
            ]
    return {
        "generatedAt": int(time.time()),
        "ackedScans": sorted(acked),
        "scanPlan": dict(request.app.state.canonical_scan_plan),
        "referenceItemIDs": list(request.app.state.reference_item_ids),
        # Filled by the strategy engine in a later phase.
        "shoppingLists": [],
    }
