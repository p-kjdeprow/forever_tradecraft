from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse

from crowley.core.account import AccountFormatError, validate_account_envelope
from crowley.core.game_target import packaged_game_target, target_scan_plan
from crowley.core.house import House
from crowley.core.queue import (
    QueueFormatError,
    build_queue,
    validate_execution_record,
)
from crowley.core.reference import ReferenceFormatError, validate_reference_envelope
from crowley.core.scanformat import ScanFormatError
from crowley.core.volume import NORMALIZATION_POLICY, project_observation_variant, volume_report
from crowley.core.watchlist import load_catalog

from .config import ServerSettings, get_settings
from .db import create_pool, migrate
from .ingest import SweepConflictError, ensure_house, ingest_market_sweep

log = logging.getLogger(__name__)
_TARGET = packaged_game_target()
_CANONICAL_CATALOG = load_catalog()
_CANONICAL_SCAN_PLAN = target_scan_plan(_CANONICAL_CATALOG, _TARGET)
_REFERENCE_ITEM_IDS = tuple(sorted(item.item_id for item in _CANONICAL_CATALOG.items))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    load_catalog()  # Fail at startup if the shipped plan is invalid.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pool = create_pool(settings.database_url)
    with pool.connection() as conn:
        applied = migrate(conn)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Crowley", version="0.1.0", lifespan=lifespan)

_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")

Settings = Annotated[ServerSettings, Depends(get_settings)]


def require_token(settings: Settings, authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.api_token.get_secret_value()}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


Authed = Depends(require_token)


def _strict_json_object(body: bytes) -> dict[str, Any]:
    """Decode one upload body while rejecting duplicate keys at every depth."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise ValueError(f"invalid JSON constant {value}")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="malformed JSON upload") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="upload body must be a JSON object")
    return value


def _base_item_id(item_key: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(?::.*)?", item_key)
    if not match:
        raise HTTPException(status_code=400, detail="item key must start with a positive item ID")
    return int(match.group(1))


def _persist_market_sweep(pool: Any, raw: dict[str, Any], data_dir: Any) -> Any:
    """Run synchronous validation, DB writes and archiving off the ASGI loop."""
    with pool.connection() as conn:
        return ingest_market_sweep(conn, raw, data_dir)


def _house_id(conn, key: str) -> int:
    try:
        house = House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = conn.execute("SELECT id FROM houses WHERE key = %s", (house.key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"no data for house {house.key}")
    return row[0]


@app.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    with request.app.state.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/v1/scans", dependencies=[Authed])
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


@app.post("/v1/probes", dependencies=[Authed])
def upload_probe(probe: dict[str, Any], request: Request) -> dict[str, str]:
    with request.app.state.pool.connection() as conn:
        conn.execute(
            "INSERT INTO client_probes (probed_at, probe) VALUES (%s, %s) ON CONFLICT (probed_at) DO NOTHING",
            (datetime.fromtimestamp(probe["at"], tz=UTC), json.dumps(probe)),
        )
    return {"status": "ok"}


@app.get("/v1/houses", dependencies=[Authed])
def list_houses(request: Request) -> list[dict[str, Any]]:
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT h.key, h.region, h.ruleset, h.faction,
                   count(DISTINCT s.id) AS legacy_scans,
                   count(DISTINCT ms.id) AS sweeps,
                   count(DISTINCT s.id) + count(DISTINCT ms.id) AS scans,
                   greatest(max(s.scanned_at), max(ms.finished_at)) AS last_scan_at,
                   (SELECT num_listings FROM scans
                     WHERE house_id = h.id ORDER BY scanned_at DESC LIMIT 1) AS last_scan_listings
            FROM houses h LEFT JOIN scans s ON s.house_id = h.id
              LEFT JOIN market_sweeps ms ON ms.house_id = h.id
            GROUP BY h.id ORDER BY h.key
            """
        ).fetchall()


@app.get("/v1/houses/{key}/latest", dependencies=[Authed])
def latest_prices(
    key: str,
    request: Request,
    search: str | None = None,
    limit: int = Query(default=100, le=5000),
) -> dict[str, Any]:
    """Latest market observations plus a separate legacy full-scan section."""
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        scan = cur.execute(
            "SELECT id, scan_uid, scanned_at, num_listings FROM scans"
            " WHERE house_id = %s ORDER BY scanned_at DESC LIMIT 1",
            (house_id,),
        ).fetchone()
        legacy_rows = cur.execute(
            """
            SELECT st.item_key, st.item_id, i.name, i.quality, st.num_auctions, st.total_quantity,
                   st.min_unit, st.p25_unit, st.median_unit, st.market_value
            FROM item_stats st JOIN items i USING (item_key)
            WHERE st.scan_id = %s AND st.scanned_at = %s
              AND (%s::text IS NULL OR i.name ILIKE '%%' || %s || '%%')
            ORDER BY st.total_quantity DESC
            LIMIT %s
            """,
            (scan["id"], scan["scanned_at"], search, search, limit)
            if scan
            else (None, None, search, search, limit),
        ).fetchall()
        market_rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.started_at, ms.finished_at, mo.ordinal, mo.item_id,
                   mo.item_key, mo.kind, mo.coverage, mo.source_complete, mo.cutoff,
                   mo.higher_price_witness, mo.reason_code, mo.row_count, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE ms.house_id = %s
              AND ms.id = (SELECT id FROM market_sweeps WHERE house_id = %s
                           ORDER BY finished_at DESC LIMIT 1)
              AND (%s::text IS NULL OR mo.item_key ILIKE '%%' || %s || '%%')
            ORDER BY ms.finished_at DESC, mo.ordinal
            LIMIT %s
            """,
            (house_id, house_id, search, search, limit),
        ).fetchall()
        latest_market = cur.execute(
            """
            SELECT sweep_uid, started_at, finished_at, plan_hash,
                   num_observations, num_rows
            FROM market_sweeps
            WHERE house_id = %s
            ORDER BY finished_at DESC LIMIT 1
            """,
            (house_id,),
        ).fetchone()
    if not scan and not latest_market:
        raise HTTPException(status_code=404, detail="house has no scans")
    return {
        "house": key,
        "scan": scan,
        "items": legacy_rows,
        "legacy_full_scan": {"scan": scan, "items": legacy_rows} if scan else None,
        "legacyPoints": legacy_rows,
        "sweep": latest_market,
        "observations": market_rows,
    }


@app.get("/v1/houses/{key}/items/{item_key}/history", dependencies=[Authed])
def item_history(
    key: str, item_key: str, request: Request, days: int = Query(default=7, le=365)
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        item = cur.execute(
            "SELECT item_key, item_id, name, quality, level FROM items WHERE item_key = %s",
            (item_key,),
        ).fetchone()
        points = cur.execute(
            """
            SELECT scanned_at, num_auctions, total_quantity, min_unit, p25_unit, median_unit,
                   market_value
            FROM item_stats
            WHERE house_id = %s AND item_key = %s AND scanned_at >= %s
            ORDER BY scanned_at
            """,
            (house_id, item_key, since),
        ).fetchall()
        market_rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.started_at, ms.finished_at, mo.ordinal, mo.item_id,
                   mo.item_key, mo.kind, mo.coverage, mo.source_complete, mo.cutoff,
                   mo.reason_code, mo.row_count, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE mo.house_id = %s AND mo.item_id = %s AND ms.finished_at >= %s
            ORDER BY ms.started_at, mo.ordinal, ms.sweep_uid
            """,
            (house_id, item_id, since),
        ).fetchall()
    market = []
    for row in market_rows:
        projected = project_observation_variant(row["observation"], item_key)
        market.append({**row, "observation": projected, "row_count": projected["rowCount"]})
    return {
        "house": key,
        "item": item,
        "legacy_full_scan": {"points": points} if points else None,
        "legacyPoints": points,
        "observations": market,
    }


@app.get("/v1/houses/{key}/items/{item_key}/volume", dependencies=[Authed])
def item_volume(
    key: str,
    item_key: str,
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    item_id = _base_item_id(item_key)
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        house_id = _house_id(conn, key)
        rows = cur.execute(
            """
            SELECT ms.sweep_uid, ms.house_id, ms.started_at, ms.finished_at,
                   mo.item_key, ms.context, ms.plan_hash, mo.observation
            FROM market_observations mo JOIN market_sweeps ms ON ms.id = mo.sweep_id
            WHERE ms.house_id = %s AND mo.item_id = %s
            ORDER BY ms.started_at, (mo.observation->>'ordinal')::int, ms.sweep_uid
            """,
            (house_id, item_id),
        ).fetchall()
        own_sales = cur.execute(
            """
            SELECT auction_id, character, first_seen_sold_at, quantity, listed_buyout,
                   item_id, item_key, raw_item_key, observation_window, inconsistent
            FROM own_sale_evidence
            WHERE house_id = %s AND item_id = %s AND first_seen_sold_at >= %s
            ORDER BY first_seen_sold_at
            """,
            (house_id, item_id, since),
        ).fetchall()
    records = []
    for row in rows:
        # Keep the complete base-item scope for grouping and persistent-ID
        # checks.  volume_report applies the exact variant filter while it
        # still sees grouped rows and variants outside the requested prefix.
        observation = row["observation"]
        records.append(
            {
                "observation": observation,
                "envelope": {
                    "house": {"key": key},
                    "itemKey": item_key,
                    "context": row["context"],
                    "planHash": row["plan_hash"],
                    "startedAt": int(row["started_at"].timestamp()),
                    "finishedAt": int(row["finished_at"].timestamp()),
                    "sweepStart": int(row["started_at"].timestamp()),
                    "ordinal": observation["ordinal"],
                    "sweep_uid": row["sweep_uid"],
                },
            }
        )
    own_sales = [row for row in own_sales if row["item_key"] == item_key]
    report = volume_report(records, hours=hours, end=int(time.time()), item_key=item_key)
    report["house"] = key
    report["itemKey"] = item_key
    report["normalizationPolicy"] = NORMALIZATION_POLICY
    report["ownConfirmed"] = {
        "units": sum(row["quantity"] for row in own_sales if not row["inconsistent"]),
        "evidence": own_sales,
        "executionTimeKnown": False,
    }
    return report


class AddonDataRequest(BaseModel):
    # Scan ids currently sitting in the player's SavedVariables.
    scan_ids: list[str] = []


class ReferenceConflictError(Exception):
    """The capture UID is already claimed by different content."""


class AccountConflictError(Exception):
    """The account capture UID is already claimed by different content."""


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


@app.post("/v1/item-reference", dependencies=[Authed])
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


@app.get("/v1/houses/{key}/item-reference", dependencies=[Authed])
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


@app.post("/v1/account-state", dependencies=[Authed])
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


@app.get("/v1/houses/{key}/account-state/latest", dependencies=[Authed])
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


@app.get("/v1/scan-plan", dependencies=[Authed])
def scan_plan() -> dict[str, Any]:
    return dict(_CANONICAL_SCAN_PLAN)


@app.post("/v1/addon-data", dependencies=[Authed])
def addon_data(body: AddonDataRequest, request: Request) -> dict[str, Any]:
    """Everything the companion writes into Crowley_Data/Data.lua."""
    acked: list[str] = []
    if body.scan_ids:
        with request.app.state.pool.connection() as conn:
            acked = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT scan_uid FROM scans WHERE scan_uid = ANY(%s)
                    UNION
                    SELECT sweep_uid FROM market_sweeps WHERE sweep_uid = ANY(%s)
                    """,
                    (body.scan_ids, body.scan_ids),
                )
            ]
    return {
        "generatedAt": int(time.time()),
        "ackedScans": sorted(acked, key=lambda item: item.encode()),
        "scanPlan": dict(_CANONICAL_SCAN_PLAN),
        "referenceItemIDs": list(_REFERENCE_ITEM_IDS),
        # Filled by the strategy engine in a later phase.
        "shoppingLists": [],
    }


# -- trade queues ---------------------------------------------------------------------


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


@app.post("/v1/queues", dependencies=[Authed])
async def publish_queue(request: Request) -> dict[str, Any]:
    raw = _strict_json_object(await request.body())
    try:
        return await run_in_threadpool(_persist_queue, request.app.state.pool, raw)
    except QueueFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/houses/{key}/queue/active", dependencies=[Authed])
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


@app.post("/v1/executions", dependencies=[Authed])
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


def _paper_run_query() -> str:
    return """
        SELECT pr.id, pr.name, h.key AS house, pr.client_build, pr.start_t,
               pr.capital_copper, pr.strategies, pr.weights, pr.demand_share,
               pr.created_at, latest.step_t AS latest_step_t,
               latest.equity AS latest_equity, latest.realized AS latest_realized,
               pending.step_t AS pending_step_t
        FROM paper_runs AS pr
        JOIN houses AS h ON h.id = pr.house_id
        LEFT JOIN LATERAL (
            SELECT step_t, equity, realized
            FROM paper_steps
            WHERE run_id = pr.id
            ORDER BY step_t DESC
            LIMIT 1
        ) AS latest ON TRUE
        LEFT JOIN paper_pending AS pending ON pending.run_id = pr.id
    """


_PAPER_COPPER_MAPS = frozenset(("equity", "realized", "cash"))
_PAPER_COPPER_FIELDS = frozenset(("max_unit_price", "unit_price"))


def _paper_copper_string(value: Any) -> str:
    if isinstance(value, bool):
        raise TypeError("paper copper values cannot be boolean")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported paper copper value {value!r}")


def _paper_json(value: Any, *, key: str | None = None) -> Any:
    if key in _PAPER_COPPER_MAPS:
        if not isinstance(value, dict):
            raise TypeError(f"paper {key} must be an object")
        return {name: _paper_copper_string(amount) for name, amount in value.items()}
    if key == "capital_copper" or key in _PAPER_COPPER_FIELDS or (key and key.endswith("_copper")):
        return _paper_copper_string(value)
    if isinstance(value, dict):
        return {name: _paper_json(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [_paper_json(item) for item in value]
    return value


def _paper_run_payload(row: dict[str, Any]) -> dict[str, Any]:
    latest_equity = row["latest_equity"]
    latest_realized = row["latest_realized"]
    return {
        "name": row["name"],
        "house": row["house"],
        "client_build": row["client_build"],
        "start_t": row["start_t"],
        "capital_copper": _paper_copper_string(row["capital_copper"]),
        "strategies": row["strategies"],
        "weights": row["weights"],
        "demand_share": row["demand_share"],
        "created_at": row["created_at"],
        "latest_step_t": row["latest_step_t"],
        "latest_total_equity": None
        if latest_equity is None
        else _paper_copper_string(latest_equity.get("total")),
        "latest_total_realized": None
        if latest_realized is None
        else _paper_copper_string(latest_realized.get("total")),
        "pending_step_t": row["pending_step_t"],
    }


@app.get("/v1/paper/runs", dependencies=[Authed])
def list_paper_runs(request: Request) -> list[dict[str, Any]]:
    with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_paper_run_query() + " ORDER BY pr.name").fetchall()
    return [_paper_run_payload(row) for row in rows]


@app.get("/v1/paper/runs/{name}", dependencies=[Authed])
def paper_run(name: str, request: Request) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET TRANSACTION READ ONLY")
            with conn.cursor(row_factory=dict_row) as cur:
                run = cur.execute(_paper_run_query() + " WHERE pr.name = %s", (name,)).fetchone()
                if run is None:
                    raise HTTPException(status_code=404, detail="unknown paper run")

                curve_rows = cur.execute(
                    """
                    SELECT step_t, equity, realized, cash
                    FROM paper_steps
                    WHERE run_id = %s
                    ORDER BY step_t
                    """,
                    (run["id"],),
                ).fetchall()
                intent_rows = cur.execute(
                    """
                    SELECT step_t AS t, strategy, ordinal, intent, outcome, detail
                    FROM paper_intents
                    WHERE run_id = %s
                    ORDER BY step_t DESC, strategy, ordinal DESC
                    LIMIT 200
                    """,
                    (run["id"],),
                ).fetchall()
                pending = cur.execute(
                    """
                    SELECT step_t AS t, sweep_key, intents
                    FROM paper_pending
                    WHERE run_id = %s
                    """,
                    (run["id"],),
                ).fetchone()

    payload = _paper_run_payload(run)
    payload.update(
        {
            "curve": [
                {
                    "t": row["step_t"],
                    "equity": _paper_json(row["equity"], key="equity"),
                    "realized": _paper_json(row["realized"], key="realized"),
                    "cash": _paper_json(row["cash"], key="cash"),
                }
                for row in curve_rows
            ],
            "recent_intents": [
                {
                    "t": row["t"],
                    "strategy": row["strategy"],
                    "ordinal": row["ordinal"],
                    "intent": _paper_json(row["intent"]),
                    "outcome": row["outcome"],
                    "detail": _paper_json(row["detail"]),
                }
                for row in intent_rows
            ],
            "pending": None if pending is None else _paper_json(dict(pending)),
        }
    )
    return payload


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(
        _DASHBOARD_HTML,
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
