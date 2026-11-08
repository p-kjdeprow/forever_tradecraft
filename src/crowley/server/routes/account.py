from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from crowley.core.account import AccountFormatError, validate_account_envelope
from crowley.core.house import House
from crowley.store.accounts import (
    AccountConflictError,
    AccountStateResult,
    persist_account_state,
)
from crowley.store.accounts import (
    latest_account_state as store_latest_account_state,
)

from ..deps import Authed
from ..encoding import _strict_json_object
from ._shared import _house_id

router = APIRouter()


def _account_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _persist_account_state(conn: Any, raw: dict[str, Any]) -> AccountStateResult:
    validated = validate_account_envelope(raw)
    digest = _account_digest(raw)
    house_raw = validated["house"]
    house = House(house_raw["region"], house_raw["ruleset"], house_raw["faction"])
    captured_at = datetime.fromtimestamp(validated["capturedAt"], tz=UTC)
    gold = validated["gold"] if isinstance(validated["gold"], int) else None
    return persist_account_state(
        conn,
        raw,
        validated,
        digest,
        house,
        captured_at,
        gold,
    )


def _persist_account_state_request(pool: Any, raw: dict[str, Any]) -> AccountStateResult:
    with pool.connection() as conn, conn.transaction():
        return _persist_account_state(conn, raw)


@router.post("/v1/account-state", dependencies=[Authed])
async def upload_account_state(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(_persist_account_state_request, request.app.state.pool, raw)
    except AccountFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AccountConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": result.id,
        "captureId": result.id,
        "captureUid": result.capture_uid,
        "status": result.status,
        "professions": result.professions,
        "recipes": result.recipes,
        "items": result.items,
    }


@router.get("/v1/houses/{key}/account-state/latest", dependencies=[Authed])
def latest_account_state(
    key: str, request: Request, character: str = Query(..., min_length=1)
) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        capture = store_latest_account_state(conn, house_id, character)
    if not capture:
        raise HTTPException(status_code=404, detail="no account state for character")
    return {"house": key, "character": character, "capture": capture}
