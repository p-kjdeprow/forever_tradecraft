from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from crowley.core.house import House
from crowley.core.reference import ReferenceFormatError, validate_reference_envelope

from ..deps import Authed
from ..encoding import _strict_json_object
from ..ingest import ensure_house
from ._shared import _house_id

router = APIRouter()


class ReferenceConflictError(Exception):
    """The capture UID is already claimed by different content."""


@dataclass
class ItemReferenceResult:
    id: int
    capture_uid: str
    status: str
    items: int


def _reference_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _persist_item_reference(pool: Any, raw: dict[str, Any]) -> ItemReferenceResult:
    validated = validate_reference_envelope(raw)
    digest = _reference_digest(raw)
    house_raw = validated["house"]
    house = House(house_raw["region"], house_raw["ruleset"], house_raw["faction"])
    started_at = datetime.fromtimestamp(validated["startedAt"], tz=UTC)
    finished_at = datetime.fromtimestamp(validated["finishedAt"], tz=UTC)
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (validated["id"],))
        existing = conn.execute(
            "SELECT id, content_digest FROM item_reference_captures WHERE capture_uid=%s FOR UPDATE",
            (validated["id"],),
        ).fetchone()
        if existing:
            if existing[1] != digest:
                raise ReferenceConflictError("reference capture UID has different content")
            return ItemReferenceResult(existing[0], validated["id"], "duplicate", len(validated["items"]))

        house_id = ensure_house(conn, house)
        capture_id = conn.execute(
            """
            INSERT INTO item_reference_captures
              (capture_uid, house_id, client_build, started_at, finished_at, ah_open,
               context, raw, content_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                validated["id"],
                house_id,
                validated["context"]["clientBuild"],
                started_at,
                finished_at,
                validated["ahOpen"],
                json.dumps(validated["context"]),
                json.dumps(raw),
                digest,
            ),
        ).fetchone()[0]
        for item_id_text, record in validated["items"].items():
            item_id = int(item_id_text)
            info = record.get("info")
            deposit = record.get("deposit")
            conn.execute(
                """
                INSERT INTO item_reference
                  (capture_id, item_id, status, quality, item_level, min_level, class_id,
                   subclass_id, max_stack, sell_price, bind_type, expansion_id,
                   is_crafting_reagent, deposit_kind)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    capture_id,
                    item_id,
                    record["status"],
                    info["quality"] if info else None,
                    info["itemLevel"] if info else None,
                    info["minLevel"] if info else None,
                    info["classID"] if info else None,
                    info["subclassID"] if info else None,
                    info["maxStack"] if info else None,
                    info["sellPrice"] if info else None,
                    info["bindType"] if info else None,
                    info["expansionID"] if info else None,
                    info["isCraftingReagent"] if info else None,
                    deposit["kind"] if deposit else None,
                ),
            )
            if deposit and deposit["kind"] == "commodity":
                for value in deposit["values"]:
                    conn.execute(
                        """
                        INSERT INTO item_deposits
                          (capture_id, item_id, duration, quantity, deposit_copper)
                        VALUES (%s,%s,%s,%s,%s)
                        """,
                        (capture_id, item_id, value["duration"], value["quantity"], value["copper"]),
                    )
    return ItemReferenceResult(capture_id, validated["id"], "created", len(validated["items"]))


@router.post("/v1/item-reference", dependencies=[Authed])
async def upload_item_reference(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(_persist_item_reference, request.app.state.pool, raw)
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
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        rows = cur.execute(
            """
            SELECT capture_id, house_id, capture_uid, client_build, started_at, finished_at,
                   ah_open, item_id, status, quality, item_level, min_level, class_id,
                   subclass_id, max_stack, sell_price, bind_type, expansion_id,
                   is_crafting_reagent, deposit_kind
            FROM latest_item_reference
            WHERE house_id = %s
            ORDER BY item_id
            """,
            (house_id,),
        ).fetchall()
    return {"house": key, "items": rows}
