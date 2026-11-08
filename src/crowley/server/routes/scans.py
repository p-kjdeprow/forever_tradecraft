from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt
from starlette.concurrency import run_in_threadpool

from crowley.core.scanformat import ScanFormatError
from crowley.store.sweeps import record_probe

from ..deps import Authed, Settings
from ..encoding import _strict_json_object
from ..ingest import SweepConflictError, _persist_market_sweep

router = APIRouter()


class ProbeRequest(BaseModel):
    """Validated client probe envelope; probe-specific fields remain extensible."""

    model_config = ConfigDict(extra="allow")

    at: StrictInt


@router.post("/v1/scans", dependencies=[Authed])
async def upload_scan(request: Request, settings: Settings) -> dict[str, Any]:
    """Upload the single accepted format-2 envelope."""
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(
            _persist_market_sweep_request, request.app.state.pool, raw, settings.data_dir
        )
    except ScanFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SweepConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.__dict__


def _persist_market_sweep_request(pool: Any, raw: dict[str, Any], data_dir: Any) -> Any:
    """Run validation, archiving, and the store UoW off the ASGI loop."""

    with pool.connection() as conn, conn.transaction():
        return _persist_market_sweep(conn, raw, data_dir)


@router.post("/v1/probes", dependencies=[Authed])
def upload_probe(probe: ProbeRequest, request: Request) -> dict[str, str]:
    try:
        probed_at = datetime.fromtimestamp(probe.at, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="probe.at is not storable") from exc
    with request.app.state.pool.connection() as conn, conn.transaction():
        record_probe(conn, probed_at, probe.model_dump(mode="json"))
    return {"status": "ok"}
