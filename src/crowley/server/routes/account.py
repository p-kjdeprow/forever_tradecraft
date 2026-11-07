from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from crowley.core.account import AccountFormatError, validate_account_envelope
from crowley.core.house import House

from ..deps import Authed
from ..encoding import _strict_json_object
from ..ingest import ensure_house
from ._shared import _house_id

router = APIRouter()


class AccountConflictError(Exception):
    """The account capture UID is already claimed by different content."""


@dataclass
class AccountStateResult:
    id: int
    capture_uid: str
    status: str
    professions: int
    recipes: int
    items: int


def _account_digest(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _account_map(value: Any) -> dict[str, Any]:
    return {} if value == [] else value


def _persist_account_state(pool: Any, raw: dict[str, Any]) -> AccountStateResult:
    validated = validate_account_envelope(raw)
    digest = _account_digest(raw)
    house_raw = validated["house"]
    house = House(house_raw["region"], house_raw["ruleset"], house_raw["faction"])
    captured_at = datetime.fromtimestamp(validated["capturedAt"], tz=UTC)
    gold = validated["gold"] if isinstance(validated["gold"], int) else None
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (validated["id"],))
        existing = conn.execute(
            "SELECT id, content_digest FROM account_captures WHERE capture_uid=%s FOR UPDATE",
            (validated["id"],),
        ).fetchone()
        if existing:
            if existing[1] != digest:
                raise AccountConflictError("account capture UID has different content")
            return AccountStateResult(existing[0], validated["id"], "duplicate", 0, 0, 0)

        house_id = ensure_house(conn, house)
        capture_id = conn.execute(
            """
            INSERT INTO account_captures
              (capture_uid, house_id, character, captured_at, gold, raw, content_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                validated["id"],
                house_id,
                validated["context"]["character"],
                captured_at,
                gold,
                json.dumps(raw),
                digest,
            ),
        ).fetchone()[0]

        item_count = 0
        bags = _account_map(validated["bags"])
        if isinstance(bags, dict):
            for container, bag in bags.items():
                if not isinstance(bag, dict):
                    continue
                slots = _account_map(bag.get("slots", {}))
                if not isinstance(slots, dict):
                    continue
                for slot, item in slots.items():
                    if not isinstance(item, dict):
                        continue
                    conn.execute(
                        """
                        INSERT INTO account_items
                          (capture_id, location, container, slot, item_id, count, bound)
                        VALUES (%s,'bag',%s,%s,%s,%s,%s)
                        """,
                        (
                            capture_id,
                            container,
                            int(slot),
                            item["itemID"],
                            item["count"],
                            item["bound"],
                        ),
                    )
                    item_count += 1

        mail = validated["mail"]
        if isinstance(mail, dict) and mail.get("kind") == "observed":
            for message_index, message in enumerate(mail["messages"], start=1):
                for attachment_index, item in enumerate(message["items"], start=1):
                    conn.execute(
                        """
                        INSERT INTO account_items
                          (capture_id, location, container, slot, item_id, count, bound)
                        VALUES (%s,'mail',%s,%s,%s,%s,NULL)
                        """,
                        (
                            capture_id,
                            str(message_index),
                            attachment_index,
                            item["itemID"],
                            item["count"],
                        ),
                    )
                    item_count += 1

        profession_count = 0
        professions = validated["professions"]
        if isinstance(professions, list):
            for profession in professions:
                conn.execute(
                    """
                    INSERT INTO account_professions
                      (capture_id, skill_line_id, name, level, max_level)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        capture_id,
                        profession["skillLineID"],
                        profession["name"],
                        profession["level"],
                        profession["maxLevel"],
                    ),
                )
                profession_count += 1

        recipe_count = 0
        recipes = _account_map(validated["recipes"])
        if isinstance(recipes, dict):
            for skill_line, record in recipes.items():
                if not isinstance(record, dict):
                    continue
                for spell_id in record["learned"]:
                    conn.execute(
                        "INSERT INTO account_recipes (capture_id, skill_line_id, spell_id) VALUES (%s,%s,%s)",
                        (capture_id, int(skill_line), spell_id),
                    )
                    recipe_count += 1

        cooldowns = _account_map(validated["cooldowns"])
        if isinstance(cooldowns, dict):
            for spell_id, cooldown in cooldowns.items():
                if not isinstance(cooldown, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO account_cooldowns
                      (capture_id, spell_id, start_seconds, duration_seconds)
                    VALUES (%s,%s,%s,%s)
                    """,
                    (capture_id, int(spell_id), cooldown["start"], cooldown["duration"]),
                )
    return AccountStateResult(
        capture_id,
        validated["id"],
        "created",
        profession_count,
        recipe_count,
        item_count,
    )


@router.post("/v1/account-state", dependencies=[Authed])
async def upload_account_state(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        result = await run_in_threadpool(_persist_account_state, request.app.state.pool, raw)
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
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        capture = cur.execute(
            """
            SELECT capture_id, house_id, capture_uid, character, captured_at, gold,
                   raw, content_digest, received_at
            FROM latest_account_state
            WHERE house_id = %s AND character = %s
            """,
            (house_id, character),
        ).fetchone()
    if not capture:
        raise HTTPException(status_code=404, detail="no account state for character")
    return {"house": key, "character": character, "capture": capture}
