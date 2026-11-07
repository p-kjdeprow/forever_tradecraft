from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from crowley.core.house import House
from crowley.core.queue import QueueFormatError, build_queue, validate_execution_record

from ..deps import Authed
from ..encoding import _strict_json_object
from ..ingest import ensure_house

router = APIRouter()


def _queue_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _queue_epoch(value: datetime) -> int:
    return int(value.timestamp())


def _queue_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": 1,
        "queueId": row["queue_id"],
        "generatedAt": _queue_epoch(row["generated_at"]),
        "expiresAt": _queue_epoch(row["expires_at"]),
        "house": row["house"],
        "character": row["character"],
        "armed": row["armed"],
        "actions": row["actions"],
    }


def _persist_queue(pool: Any, raw: dict[str, Any]) -> dict[str, Any]:
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
    generated_at = datetime.fromtimestamp(queue["generatedAt"], tz=UTC)
    expires_at = datetime.fromtimestamp(queue["expiresAt"], tz=UTC)
    with pool.connection() as conn, conn.transaction():
        house_id = ensure_house(conn, house)
        conn.execute(
            """
            UPDATE trade_queues
            SET superseded_at = now()
            WHERE house_id = %s AND character = %s AND superseded_at IS NULL
            """,
            (house_id, queue["character"]),
        )
        conn.execute(
            """
            INSERT INTO trade_queues
              (queue_id, house_id, character, generated_at, expires_at, armed, actions, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                queue["queueId"],
                house_id,
                queue["character"],
                generated_at,
                expires_at,
                queue["armed"],
                Jsonb(queue["actions"]),
                source,
            ),
        )
    return queue


@router.post("/v1/queues", dependencies=[Authed])
async def publish_queue(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        return await run_in_threadpool(_persist_queue, request.app.state.pool, raw)
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
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT q.queue_id, q.generated_at, q.expires_at, h.key AS house,
                   q.character, q.armed, q.actions
            FROM trade_queues AS q
            JOIN houses AS h ON h.id = q.house_id
            WHERE h.key = %s AND q.character = %s
              AND q.superseded_at IS NULL AND q.expires_at > now()
            ORDER BY q.id DESC
            LIMIT 1
            """,
            (house.key, character),
        ).fetchone()
    return None if row is None else _queue_payload(row)


def _queue_for_execution(conn: Any, queue_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT q.queue_id, q.generated_at, q.expires_at, h.key AS house,
               q.character, q.armed, q.actions
        FROM trade_queues AS q JOIN houses AS h ON h.id = q.house_id
        WHERE q.queue_id = %s
        """,
        (queue_id,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return _queue_payload(row)
    return {
        "format": 1,
        "queueId": row[0],
        "generatedAt": _queue_epoch(row[1]),
        "expiresAt": _queue_epoch(row[2]),
        "house": row[3],
        "character": row[4],
        "armed": row[5],
        "actions": row[6],
    }


def _stored_result(row: Any) -> dict[str, Any]:
    at = row[5]
    return {
        "seq": row[1],
        "kind": row[2],
        "status": row[3],
        "at": _queue_epoch(at),
        "detail": row[4],
    }


def _persist_execution(pool: Any, raw: dict[str, Any]) -> dict[str, Any]:
    queue_id = raw.get("queueId")
    if not isinstance(queue_id, str) or not queue_id:
        raise QueueFormatError("execution.queueId must be a non-empty string")
    digest = _queue_digest(raw)
    with pool.connection() as conn, conn.transaction():
        queue = _queue_for_execution(conn, queue_id)
        if queue is None:
            raise KeyError(queue_id)
        execution = validate_execution_record(raw, queue)
        stored_rows = conn.execute(
            """
            SELECT ordinal, seq, kind, status, detail, "at"
            FROM trade_execution_results
            WHERE queue_id = %s ORDER BY ordinal
            """,
            (queue_id,),
        ).fetchall()
        record_exists = (
            conn.execute("SELECT 1 FROM trade_execution_records WHERE queue_id = %s", (queue_id,)).fetchone()
            is not None
        )
        stored = [_stored_result(row) for row in stored_rows]
        incoming = execution["results"]
        if len(incoming) < len(stored) or incoming[: len(stored)] != stored:
            raise ValueError("execution results are not an append-only extension")
        added = incoming[len(stored) :]
        if not record_exists:
            conn.execute(
                """
                INSERT INTO trade_execution_records (queue_id, raw, content_digest)
                VALUES (%s,%s,%s)
                """,
                (queue_id, Jsonb(execution), digest),
            )
        elif added:
            conn.execute(
                """
                UPDATE trade_execution_records
                SET received_at = now(), raw = %s, content_digest = %s
                WHERE queue_id = %s
                """,
                (Jsonb(execution), digest, queue_id),
            )
        for ordinal, result in enumerate(added, start=len(stored) + 1):
            conn.execute(
                """
                INSERT INTO trade_execution_results
                    (queue_id, ordinal, seq, kind, status, "at", detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    queue_id,
                    ordinal,
                    result["seq"],
                    result["kind"],
                    result["status"],
                    datetime.fromtimestamp(result["at"], tz=UTC),
                    Jsonb(result["detail"]),
                ),
            )
        if record_exists and not added:
            status = "duplicate"
        elif record_exists:
            status = "appended"
        else:
            status = "created"
    return {"queueId": queue_id, "status": status, "results": len(incoming)}


@router.post("/v1/executions", dependencies=[Authed])
async def upload_execution(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        return await run_in_threadpool(_persist_execution, request.app.state.pool, raw)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown queue {exc.args[0]}") from exc
    except QueueFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
