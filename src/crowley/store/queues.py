"""Persistence and read models for trade queues and executions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from crowley.core.house import House

from ._types import Connection, JsonObject
from .houses import ensure_house


class QueueNotFoundError(LookupError):
    """The execution refers to an unknown queue."""


class ExecutionConflictError(ValueError):
    """An execution is not an append-only extension of stored results."""


def persist_queue(conn: Connection, queue: JsonObject, source: str, house: House) -> JsonObject:
    """Supersede the active queue and publish a validated queue."""

    queue_values = cast(dict[str, Any], queue)
    generated_at = datetime.fromtimestamp(queue_values["generatedAt"], tz=UTC)
    expires_at = datetime.fromtimestamp(queue_values["expiresAt"], tz=UTC)
    house_id = ensure_house(conn, house)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trade_queues
            SET superseded_at = now()
            WHERE house_id = %s AND character = %s AND superseded_at IS NULL
            """,
            (house_id, queue_values["character"]),
        )
        cur.execute(
            """
            INSERT INTO trade_queues
              (queue_id, house_id, character, generated_at, expires_at, armed, actions, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                queue_values["queueId"],
                house_id,
                queue_values["character"],
                generated_at,
                expires_at,
                queue_values["armed"],
                Jsonb(queue_values["actions"]),
                source,
            ),
        )
    return queue


def active_queue(conn: Connection, house: House, character: str) -> JsonObject | None:
    """Return the active, unexpired queue for a house and character."""

    with conn.cursor(row_factory=dict_row) as cur:
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
    return None if row is None else _queue_payload(cast(JsonObject, dict(row)))


def _queue_payload(row: JsonObject) -> JsonObject:
    return {
        "format": 1,
        "queueId": row["queue_id"],
        "generatedAt": int(cast(datetime, row["generated_at"]).timestamp()),
        "expiresAt": int(cast(datetime, row["expires_at"]).timestamp()),
        "house": row["house"],
        "character": row["character"],
        "armed": row["armed"],
        "actions": row["actions"],
    }


def queue_for_execution(conn: Connection, queue_id: str) -> JsonObject:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            """
            SELECT q.queue_id, q.generated_at, q.expires_at, h.key AS house,
                   q.character, q.armed, q.actions
            FROM trade_queues AS q JOIN houses AS h ON h.id = q.house_id
            WHERE q.queue_id = %s
            """,
            (queue_id,),
        ).fetchone()
    if row is None:
        raise QueueNotFoundError(f"unknown queue {queue_id}")
    return _queue_payload(cast(JsonObject, dict(row)))


def _stored_result(row: tuple[object, ...]) -> JsonObject:
    at = cast(datetime, row[5])
    return {
        "seq": row[1],
        "kind": row[2],
        "status": row[3],
        "at": int(at.timestamp()),
        "detail": row[4],
    }


def persist_execution(
    conn: Connection,
    raw: JsonObject,
    execution: JsonObject,
    digest: str,
) -> JsonObject:
    """Append a validated execution record in the caller's transaction."""

    raw_values = cast(dict[str, Any], raw)
    execution_values = cast(dict[str, Any], execution)
    queue_id = raw_values["queueId"]
    queue_for_execution(conn, queue_id)
    with conn.cursor() as cur:
        stored_rows = cur.execute(
            """
            SELECT ordinal, seq, kind, status, detail, "at"
            FROM trade_execution_results
            WHERE queue_id = %s ORDER BY ordinal
            """,
            (queue_id,),
        ).fetchall()
        record_exists = (
            cur.execute("SELECT 1 FROM trade_execution_records WHERE queue_id = %s", (queue_id,)).fetchone()
            is not None
        )
        stored = [_stored_result(row) for row in stored_rows]
        incoming = execution_values["results"]
        if len(incoming) < len(stored) or incoming[: len(stored)] != stored:
            raise ExecutionConflictError("execution results are not an append-only extension")
        added = incoming[len(stored) :]
        if not record_exists:
            cur.execute(
                """
                INSERT INTO trade_execution_records (queue_id, raw, content_digest)
                VALUES (%s,%s,%s)
                """,
                (queue_id, Jsonb(execution_values), digest),
            )
        elif added:
            cur.execute(
                """
                UPDATE trade_execution_records
                SET received_at = now(), raw = %s, content_digest = %s
                WHERE queue_id = %s
                """,
                (Jsonb(execution_values), digest, queue_id),
            )
        result_values = [
            (
                queue_id,
                ordinal,
                result["seq"],
                result["kind"],
                result["status"],
                datetime.fromtimestamp(result["at"], tz=UTC),
                Jsonb(result["detail"]),
            )
            for ordinal, result in enumerate(added, start=len(stored) + 1)
        ]
        if result_values:
            cur.executemany(
                """
                INSERT INTO trade_execution_results
                    (queue_id, ordinal, seq, kind, status, "at", detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                result_values,
            )
    status = "duplicate" if record_exists and not added else "appended" if record_exists else "created"
    return {"queueId": queue_id, "status": status, "results": len(incoming)}
