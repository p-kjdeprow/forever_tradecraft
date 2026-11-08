from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from crowley.store.sweeps import acknowledged_scan_ids

from ..deps import Authed

router = APIRouter()


class AddonDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Scan ids currently sitting in the player's SavedVariables.
    scan_ids: list[str] = Field(default_factory=list)


class AddonDataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generatedAt: int
    ackedScans: list[str]
    scanPlan: dict[str, object]
    referenceItemIDs: list[int]


@router.get("/v1/scan-plan", dependencies=[Authed])
def scan_plan(request: Request) -> dict[str, object]:
    return dict(request.app.state.canonical_scan_plan)


@router.post("/v1/addon-data", dependencies=[Authed])
def addon_data(body: AddonDataRequest, request: Request) -> AddonDataResponse:
    """Everything the companion writes into Crowley_Data/Data.lua."""
    acked: list[str] = []
    if body.scan_ids:
        with request.app.state.pool.connection() as conn:
            acked = acknowledged_scan_ids(conn, body.scan_ids)
    return AddonDataResponse(
        generatedAt=int(time.time()),
        ackedScans=sorted(acked, key=lambda item: item.encode()),
        scanPlan=dict(request.app.state.canonical_scan_plan),
        referenceItemIDs=list(request.app.state.reference_item_ids),
    )
