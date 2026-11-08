from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from crowley.core.house import House
from crowley.core.reference import ReferenceFormatError, validate_reference_envelope
from crowley.store.references import (
    ItemReferenceResult,
    ReferenceConflictError,
    persist_item_reference,
)
from crowley.store.references import (
    latest_item_reference as store_latest_item_reference,
)

from ..deps import Authed
from ..encoding import _strict_json_object
from ._shared import _house_id

router = APIRouter()


def _reference_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _persist_item_reference(conn: Any, raw: dict[str, Any]) -> ItemReferenceResult:
    validated = validate_reference_envelope(raw)
    digest = _reference_digest(raw)
    house_raw = validated["house"]
    house = House(house_raw["region"], house_raw["ruleset"], house_raw["faction"])
    started_at = datetime.fromtimestamp(validated["startedAt"], tz=UTC)
    finished_at = datetime.fromtimestamp(validated["finishedAt"], tz=UTC)
    return persist_item_reference(conn, raw, validated, digest, house, started_at, finished_at)


def _persist_item_reference_request(pool: Any, raw: dict[str, Any]) -> ItemReferenceResult:
    with pool.connection() as conn, conn.transaction():
        return _persist_item_reference(conn, raw)


@router.post("/v1/item-reference", dependencies=[Authed])
async def upload_item_reference(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(_persist_item_reference_request, request.app.state.pool, raw)
    except ReferenceFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ReferenceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": result.id,
        "captureId": result.id,
        "captureUid": result.capture_uid,
        "status": result.status,
        "items": result.items,
    }


@router.get("/v1/houses/{key}/item-reference", dependencies=[Authed])
def latest_item_reference(key: str, request: Request) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        rows = store_latest_item_reference(conn, house_id)
    return {"house": key, "items": rows}
