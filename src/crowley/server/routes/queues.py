from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from crowley.core.house import House
from crowley.core.queue import QueueFormatError, build_queue, validate_execution_record
from crowley.store.queues import (
    ExecutionConflictError,
    QueueNotFoundError,
    persist_execution,
    persist_queue,
    queue_for_execution,
)
from crowley.store.queues import (
    active_queue as store_active_queue,
)

from ..deps import Authed
from ..encoding import _strict_json_object

router = APIRouter()


def _queue_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _persist_queue(conn: Any, raw: dict[str, Any]) -> dict[str, Any]:
    source = raw.get("source", "spec")
    if source not in {"spec", "paper_pending"}:
        raise QueueFormatError("queue source is invalid")
    has_confirmation = "confirmRealTrades" in raw
    confirmation = raw.get("confirmRealTrades")
    spec = {key: value for key, value in raw.items() if key not in {"source", "confirmRealTrades"}}
    queue = build_queue(spec)
    if source == "paper_pending" and queue["armed"]:
        raise QueueFormatError("paper-derived queues must be unarmed")
    if queue["armed"]:
        if confirmation != queue["queueId"]:
            raise QueueFormatError("armed queues require confirmation of the full queueId")
    elif has_confirmation:
        raise QueueFormatError("confirmRealTrades requires an armed queue")
    house = House.parse(queue["house"])
    persist_queue(conn, queue, source, house)
    return queue


def _persist_queue_request(pool: Any, raw: dict[str, Any]) -> dict[str, Any]:
    with pool.connection() as conn, conn.transaction():
        return _persist_queue(conn, raw)


@router.post("/v1/queues", dependencies=[Authed])
async def publish_queue(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        return await run_in_threadpool(_persist_queue_request, request.app.state.pool, raw)
    except QueueFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/v1/houses/{key}/queue/active", dependencies=[Authed])
def active_queue(
    key: str, request: Request, character: str = Query(..., min_length=1)
) -> dict[str, Any] | None:
    try:
        house = House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with request.app.state.pool.connection() as conn:
        return store_active_queue(conn, house, character)


def _persist_execution(conn: Any, raw: dict[str, Any]) -> dict[str, Any]:
    queue_id = raw.get("queueId")
    if not isinstance(queue_id, str) or not queue_id:
        raise QueueFormatError("execution.queueId must be a non-empty string")
    digest = _queue_digest(raw)
    queue = queue_for_execution(conn, queue_id)
    execution = validate_execution_record(raw, queue)
    return persist_execution(conn, raw, execution, digest)


def _persist_execution_request(pool: Any, raw: dict[str, Any]) -> dict[str, Any]:
    with pool.connection() as conn, conn.transaction():
        return _persist_execution(conn, raw)


@router.post("/v1/executions", dependencies=[Authed])
async def upload_execution(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        return await run_in_threadpool(_persist_execution_request, request.app.state.pool, raw)
    except QueueNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QueueFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
