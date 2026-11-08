"""Persistence and read models for account-state captures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg.rows import dict_row

from crowley.core.house import House

from ._types import Connection, JsonObject
from .houses import ensure_house


class AccountConflictError(Exception):
    """An account capture UID is already claimed by different content."""


@dataclass(frozen=True, slots=True)
class AccountStateResult:
    id: int
    capture_uid: str
    status: str
    professions: int
    recipes: int
    items: int


def persist_account_state(
    conn: Connection,
    raw: JsonObject,
    validated: JsonObject,
    digest: str,
    house: House,
    captured_at: datetime,
    gold: int | None,
) -> AccountStateResult:
    """Persist one validated account capture in the caller's transaction."""

    raw_values = raw
    validated_values = validated
    capture_uid = cast(str, validated_values["id"])
    context = cast(dict[str, object], validated_values["context"])
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (capture_uid,))
        existing = cur.execute(
            "SELECT id, content_digest FROM account_captures WHERE capture_uid=%s FOR UPDATE",
            (capture_uid,),
        ).fetchone()
        if existing:
            if existing[1] != digest:
                raise AccountConflictError("account capture UID has different content")
            return AccountStateResult(cast(int, existing[0]), capture_uid, "duplicate", 0, 0, 0)

        house_id = ensure_house(conn, house)
        capture_row = cur.execute(
            """
            INSERT INTO account_captures
              (capture_uid, house_id, character, captured_at, gold, raw, content_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                capture_uid,
                house_id,
                context["character"],
                captured_at,
                gold,
                json.dumps(raw_values),
                digest,
            ),
        ).fetchone()
        if capture_row is None:
            raise RuntimeError("account capture insert did not return an id")
        capture_id = cast(int, capture_row[0])

        bag_values: list[tuple[object, ...]] = []
        bags_value = _account_map(validated_values["bags"])
        if isinstance(bags_value, dict):
            bags = cast(dict[str, object], bags_value)
            for container, bag_value in bags.items():
                if not isinstance(bag_value, dict):
                    continue
                bag = cast(dict[str, object], bag_value)
                slots_value = _account_map(bag.get("slots", {}))
                if not isinstance(slots_value, dict):
                    continue
                slots = cast(dict[str, object], slots_value)
                for slot, item_value in slots.items():
                    if not isinstance(item_value, dict):
                        continue
                    item = cast(dict[str, object], item_value)
                    bag_values.append(
                        (
                            capture_id,
                            "bag",
                            container,
                            int(slot),
                            item["itemID"],
                            item["count"],
                            item["bound"],
                        )
                    )

        mail_values: list[tuple[object, ...]] = []
        mail_value = validated_values["mail"]
        if isinstance(mail_value, dict):
            mail = cast(dict[str, object], mail_value)
        else:
            mail = {}
        if mail.get("kind") == "observed":
            messages = cast(list[dict[str, object]], mail["messages"])
            for message_index, message in enumerate(messages, start=1):
                items = cast(list[dict[str, object]], message["items"])
                for attachment_index, item in enumerate(items, start=1):
                    mail_values.append(
                        (
                            capture_id,
                            "mail",
                            str(message_index),
                            attachment_index,
                            item["itemID"],
                            item["count"],
                            None,
                        )
                    )

        cur.executemany(
            """
            INSERT INTO account_items
              (capture_id, location, container, slot, item_id, count, bound)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            bag_values + mail_values,
        )

        professions = cast(list[dict[str, object]], validated_values["professions"])
        profession_values: list[tuple[object, ...]] = [
            (
                capture_id,
                profession["skillLineID"],
                profession["name"],
                profession["level"],
                profession["maxLevel"],
            )
            for profession in professions
        ]
        cur.executemany(
            """
            INSERT INTO account_professions
              (capture_id, skill_line_id, name, level, max_level)
            VALUES (%s,%s,%s,%s,%s)
            """,
            profession_values,
        )

        recipe_values: list[tuple[object, ...]] = []
        recipes_value = _account_map(validated_values["recipes"])
        if isinstance(recipes_value, dict):
            recipes = cast(dict[str, object], recipes_value)
            recipe_values = [
                (capture_id, int(skill_line), spell_id)
                for skill_line, record_value in recipes.items()
                if isinstance(record_value, dict)
                for spell_id in cast(list[object], cast(dict[str, object], record_value)["learned"])
            ]
        cur.executemany(
            "INSERT INTO account_recipes (capture_id, skill_line_id, spell_id) VALUES (%s,%s,%s)",
            recipe_values,
        )

        cooldown_values: list[tuple[object, ...]] = []
        cooldowns_value = _account_map(validated_values["cooldowns"])
        if isinstance(cooldowns_value, dict):
            cooldowns = cast(dict[str, object], cooldowns_value)
            cooldown_values = [
                (
                    capture_id,
                    int(spell_id),
                    cast(dict[str, object], cooldown_value)["start"],
                    cast(dict[str, object], cooldown_value)["duration"],
                )
                for spell_id, cooldown_value in cooldowns.items()
                if isinstance(cooldown_value, dict)
            ]
        cur.executemany(
            """
            INSERT INTO account_cooldowns
              (capture_id, spell_id, start_seconds, duration_seconds)
            VALUES (%s,%s,%s,%s)
            """,
            cooldown_values,
        )
    return AccountStateResult(
        capture_id,
        capture_uid,
        "created",
        len(profession_values),
        len(recipe_values),
        len(bag_values) + len(mail_values),
    )


def _account_map(value: object) -> object:
    return {} if value == [] else value


def latest_account_state(conn: Connection, house_id: int, character: str) -> JsonObject | None:
    """Return the latest capture for one house and character."""

    with conn.cursor(row_factory=dict_row) as cur:
        capture = cur.execute(
            """
            SELECT capture_id, house_id, capture_uid, character, captured_at, gold,
                   raw, content_digest, received_at
            FROM latest_account_state
            WHERE house_id = %s AND character = %s
            """,
            (house_id, character),
        ).fetchone()
    return None if capture is None else cast(JsonObject, dict(capture))
