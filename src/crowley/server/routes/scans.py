from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from crowley.core.scanformat import ScanFormatError

from ..deps import Authed, Settings
from ..encoding import _strict_json_object
from ..ingest import SweepConflictError, ingest_market_sweep

router = APIRouter()


def _persist_market_sweep(pool: Any, raw: dict[str, Any], data_dir: Any) -> Any:
    """Run synchronous validation, DB writes and archiving off the ASGI loop."""
    with pool.connection() as conn:
        return ingest_market_sweep(conn, raw, data_dir)


@router.post("/v1/scans", dependencies=[Authed])
async def upload_scan(request: Request, settings: Settings) -> dict[str, Any]:
    """Upload the single accepted format-2 envelope."""
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(
            _persist_market_sweep, request.app.state.pool, raw, settings.data_dir
        )
    except ScanFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SweepConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.__dict__


@router.post("/v1/probes", dependencies=[Authed])
def upload_probe(probe: dict[str, Any], request: Request) -> dict[str, str]:
    with request.app.state.pool.connection() as conn:
        conn.execute(
            "INSERT INTO client_probes (probed_at, probe) VALUES (%s, %s) ON CONFLICT (probed_at) DO NOTHING",
            (datetime.fromtimestamp(probe["at"], tz=UTC), json.dumps(probe)),
        )
    return {"status": "ok"}
