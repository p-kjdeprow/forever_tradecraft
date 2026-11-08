"""Persistence and read models for item-reference captures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg.rows import dict_row

from crowley.core.house import House

from ._types import Connection, JsonObject
from .houses import ensure_house


class ReferenceConflictError(Exception):
    """A reference capture UID is already claimed by different content."""


@dataclass(frozen=True, slots=True)
class ItemReferenceResult:
    id: int
    capture_uid: str
    status: str
    items: int


def persist_item_reference(
    conn: Connection,
    raw: JsonObject,
    validated: JsonObject,
    digest: str,
    house: House,
    started_at: datetime,
    finished_at: datetime,
) -> ItemReferenceResult:
    """Persist one validated reference capture in the caller's transaction."""

    raw_values = raw
    validated_values = validated
    capture_uid = cast(str, validated_values["id"])
    context = cast(dict[str, object], validated_values["context"])
    items = cast(dict[str, object], validated_values["items"])
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (capture_uid,))
        existing = cur.execute(
            "SELECT id, content_digest FROM item_reference_captures WHERE capture_uid=%s FOR UPDATE",
            (capture_uid,),
        ).fetchone()
        if existing:
            if existing[1] != digest:
                raise ReferenceConflictError("reference capture UID has different content")
            return ItemReferenceResult(cast(int, existing[0]), capture_uid, "duplicate", len(items))

        house_id = ensure_house(conn, house)
        capture_row = cur.execute(
            """
            INSERT INTO item_reference_captures
              (capture_uid, house_id, client_build, started_at, finished_at, ah_open,
               context, raw, content_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                capture_uid,
                house_id,
                context["clientBuild"],
                started_at,
                finished_at,
                validated_values["ahOpen"],
                json.dumps(context),
                json.dumps(raw_values),
                digest,
            ),
        ).fetchone()
        if capture_row is None:
            raise RuntimeError("item reference capture insert did not return an id")
        capture_id = cast(int, capture_row[0])

        reference_values: list[tuple[object, ...]] = []
        deposit_values: list[tuple[object, ...]] = []
        for item_id_text, record_value in items.items():
            item_id = int(item_id_text)
            record = cast(dict[str, object], record_value)
            info_value = record.get("info")
            info = cast(dict[str, object], info_value) if isinstance(info_value, dict) else None
            deposit_value = record.get("deposit")
            deposit = cast(dict[str, object], deposit_value) if isinstance(deposit_value, dict) else None
            reference_values.append(
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
                )
            )
            if deposit and deposit["kind"] == "commodity":
                values = cast(list[dict[str, object]], deposit["values"])
                deposit_values.extend(
                    (capture_id, item_id, value["duration"], value["quantity"], value["copper"])
                    for value in values
                )

        cur.executemany(
            """
            INSERT INTO item_reference
              (capture_id, item_id, status, quality, item_level, min_level, class_id,
               subclass_id, max_stack, sell_price, bind_type, expansion_id,
               is_crafting_reagent, deposit_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            reference_values,
        )
        if deposit_values:
            cur.executemany(
                """
                INSERT INTO item_deposits
                  (capture_id, item_id, duration, quantity, deposit_copper)
                VALUES (%s,%s,%s,%s,%s)
                """,
                deposit_values,
            )
    return ItemReferenceResult(capture_id, capture_uid, "created", len(items))


def latest_item_reference(conn: Connection, house_id: int) -> list[JsonObject]:
    """Return the latest successful reference row for each item."""

    with conn.cursor(row_factory=dict_row) as cur:
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
    return [cast(JsonObject, dict(row)) for row in rows]
