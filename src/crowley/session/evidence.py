"""Stable-file evidence readers for addon output and companion acknowledgements."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crowley.core import companion_state, savedvars
from crowley.core.game_target import packaged_game_target, target_scan_plan
from crowley.core.scanformat import ScanFormatError, decode_json_payload, validate_v2_envelope
from crowley.core.watchlist import load_catalog

from .journal import JournalError, stable_file

BASELINE_UID = "beta-normal-alliance-1792040351-Arthur-6643"
BASELINE_MEDIAN_SECONDS = 1
BASELINE_RANGE_SECONDS = (0, 2)
_TARGET = packaged_game_target()
_CANONICAL_CATALOG = load_catalog()
_CANONICAL_PLAN = target_scan_plan(_CANONICAL_CATALOG, _TARGET)
_PLAN_COUNT = len(_CANONICAL_PLAN["itemIDs"])


@dataclass(frozen=True)
class SweepEvidence:
    uid: str
    house: str
    plan_hash: str
    observations: int
    attempted: int
    full: int
    bounded: int
    incomplete: int
    unqueried: int
    priced: int
    row_count: int
    kinds: tuple[str, ...]
    query_durations: tuple[int, ...]
    envelope: dict[str, Any]
    payload: dict[str, Any]
    stable_verified: bool = False
    query_times: dict[str, tuple[int, int]] = field(default_factory=dict)
    row_kinds: dict[str, int] = field(default_factory=dict)
    item_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ``owned`` is a first-class format-2 section.  Preserve its complete row
    # objects and counts alongside per-item evidence instead of reducing it to
    # one aggregate number.
    owned_rows: list[dict[str, Any]] = field(default_factory=list)
    owned_priced_row_count: int = 0

    def archive_record(self) -> dict[str, Any]:
        """Return the complete JSON-safe per-run evidence record."""

        return {
            "uid": self.uid,
            "house": self.house,
            "planHash": self.plan_hash,
            "observations": self.observations,
            "attempted": self.attempted,
            "full": self.full,
            "bounded": self.bounded,
            "incomplete": self.incomplete,
            "unqueried": self.unqueried,
            "pricedRowCount": self.priced,
            "rowCount": self.row_count,
            "kinds": list(self.kinds),
            "queryDurations": list(self.query_durations),
            "queryTimes": copy.deepcopy(self.query_times),
            "itemEvidence": copy.deepcopy(self.item_evidence),
            "ownedRows": copy.deepcopy(self.owned_rows),
            "ownedPricedRowCount": self.owned_priced_row_count,
            "envelope": copy.deepcopy(self.envelope),
        }


class EvidenceError(RuntimeError):
    """Stable evidence did not prove the expected native sweep."""


def _decode_saved_scan(scan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    required = {
        "id",
        "format",
        "startedAt",
        "finishedAt",
        "house",
        "context",
        "numRows",
        "numObservations",
        "payload",
    }
    if set(scan) != required:
        raise EvidenceError("saved scan envelope fields differ")
    try:
        raw_payload = decode_json_payload(scan["payload"])
        envelope = validate_v2_envelope(scan)
    except (ScanFormatError, ValueError, TypeError) as exc:
        raise EvidenceError(f"format-2 scan is malformed: {exc}") from exc
    return envelope, raw_payload


def qualify_saved_scan(
    data: bytes,
    *,
    uid: str,
    house: str,
    plan_hash: str,
    expected_context: dict[str, Any] | None = None,
    expected_plan_json: str | None = None,
    planned: int = _PLAN_COUNT,
) -> SweepEvidence:
    """Parse one exact SavedVariables file and account for every item and row."""

    if expected_context is None or expected_plan_json is None:
        raise EvidenceError("format-2 qualification requires the pinned context and canonical plan")
    if isinstance(planned, bool) or not isinstance(planned, int) or planned != _PLAN_COUNT:
        raise EvidenceError(f"format-2 qualification plan count must be exactly {_PLAN_COUNT}")
    try:
        root = savedvars.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError(f"SavedVariables is malformed: {exc}") from exc
    db = root.get("CrowleyDB")
    if not isinstance(db, dict) or not isinstance(db.get("scans"), dict):
        raise EvidenceError("CrowleyDB.scans is missing")
    scan = db["scans"].get(uid)
    if not isinstance(scan, dict):
        raise EvidenceError(f"stable SavedVariables does not contain UID {uid}")
    envelope, payload = _decode_saved_scan(scan)
    decoded = envelope["decoded"]
    if envelope["id"] != uid or envelope["house"]["key"] != house or decoded["planHash"] != plan_hash:
        raise EvidenceError("UID, house, or plan hash mismatch")
    if expected_plan_json is not None and decoded["planJSON"] != expected_plan_json:
        raise EvidenceError("canonical plan JSON does not match configured plan")
    if expected_context is not None:
        context = envelope["context"]
        for key, expected in expected_context.items():
            if context.get(key) != expected:
                raise EvidenceError(f"saved scan context field {key} does not match configured identity")
    observations = decoded["observations"]
    if len(observations) != planned or envelope["numObservations"] != planned:
        raise EvidenceError(f"planned observation count does not match {_PLAN_COUNT}")
    counts = {key: 0 for key in ("full", "bounded", "incomplete", "unqueried")}
    attempted = priced = rows = 0
    durations: list[int] = []
    query_times: dict[str, tuple[int, int]] = {}
    kinds: set[str] = set()
    row_kinds: dict[str, int] = {}
    catalog = {str(item.item_id): item.category for item in load_catalog().items if item.status == "active"}
    item_evidence: dict[str, dict[str, Any]] = {}
    owned = payload.get("owned")
    if not isinstance(owned, dict):
        raise EvidenceError("format-2 payload owned section is missing")
    owned_value = owned.get("rows", [])
    if isinstance(owned_value, dict):
        owned_rows = [owned_value[key] for key in sorted(owned_value, key=lambda item: str(item).encode())]
    elif isinstance(owned_value, list):
        owned_rows = list(owned_value)
    else:
        raise EvidenceError("format-2 payload owned rows are malformed")
    owned_count = owned.get("rowCount")
    if isinstance(owned_count, bool) or not isinstance(owned_count, int) or owned_count != len(owned_rows):
        raise EvidenceError("format-2 payload owned row count is inconsistent")
    owned_priced = sum(
        1
        for row in owned_rows
        if isinstance(row, dict)
        and isinstance(row.get("buyout"), dict)
        and row["buyout"].get("kind") == "known"
        and isinstance(row["buyout"].get("copper"), int)
        and not isinstance(row["buyout"].get("copper"), bool)
        and row["buyout"]["copper"] > 0
    )
    owned_by_item: dict[str, list[dict[str, Any]]] = {}
    for row in owned_rows:
        if (
            isinstance(row, dict)
            and isinstance(row.get("itemID"), int)
            and not isinstance(row["itemID"], bool)
        ):
            owned_by_item.setdefault(str(row["itemID"]), []).append(row)
    for item_id, observation in observations.items():
        coverage = observation["coverage"]
        counts[coverage] += 1
        if observation["query"]["kind"] == "queried":
            attempted += 1
            durations.append(observation["query"]["finishedAt"] - observation["query"]["startedAt"])
            query_times[str(item_id)] = (
                observation["query"]["startedAt"],
                observation["query"]["finishedAt"],
            )
        category_name = catalog.get(str(item_id))
        category = {
            "recipes": "recipe",
            "containers": "container",
            "equipment": "equipment",
        }.get(category_name, "commodity")
        item_evidence[str(item_id)] = {
            "coverage": coverage,
            "sourceComplete": bool(observation["sourceComplete"]),
            "priced": False,
            "rowCount": int(observation["rowCount"]),
            "pricedRowCount": 0,
            "ownedRows": copy.deepcopy(owned_by_item.get(str(item_id), [])),
            "ownedPricedRowCount": sum(
                1
                for row in owned_by_item.get(str(item_id), [])
                if isinstance(row.get("buyout"), dict)
                and row["buyout"].get("kind") == "known"
                and isinstance(row["buyout"].get("copper"), int)
                and not isinstance(row["buyout"].get("copper"), bool)
                and row["buyout"]["copper"] > 0
            ),
            "category": category,
            "query": observation["query"]["kind"],
        }
        if observation["query"]["kind"] == "queried":
            item_evidence[str(item_id)]["queryStartedAt"] = int(observation["query"]["startedAt"])
            item_evidence[str(item_id)]["queryFinishedAt"] = int(observation["query"]["finishedAt"])
        for row in observation["rows"].values():
            rows += 1
            priced += int(row["price"]["copper"] > 0)
            kinds.add(observation["kind"])
            row_kinds[observation["kind"]] = row_kinds.get(observation["kind"], 0) + 1
            item_evidence[str(item_id)]["priced"] = (
                item_evidence[str(item_id)]["priced"] or row["price"]["copper"] > 0
            )
            if row["price"]["copper"] > 0:
                item_evidence[str(item_id)]["pricedRowCount"] += 1
    # Owned rows are allowed to contain an item absent from the auction scan's
    # planned observations.  Retain a per-item proof row for that case rather
    # than forcing consumers to recover it from the aggregate owned section.
    for item_id, owned_item_rows in owned_by_item.items():
        if item_id in item_evidence:
            continue
        item_evidence[item_id] = {
            "coverage": "owned",
            "sourceComplete": True,
            "priced": False,
            "rowCount": 0,
            "pricedRowCount": 0,
            "ownedRows": copy.deepcopy(owned_item_rows),
            "ownedPricedRowCount": sum(
                1
                for row in owned_item_rows
                if isinstance(row.get("buyout"), dict)
                and row["buyout"].get("kind") == "known"
                and isinstance(row["buyout"].get("copper"), int)
                and not isinstance(row["buyout"]["copper"], bool)
                and row["buyout"]["copper"] > 0
            ),
            "category": "commodity",
            "query": "owned",
        }
    return SweepEvidence(
        uid=uid,
        house=house,
        plan_hash=plan_hash,
        observations=len(observations),
        attempted=attempted,
        full=counts["full"],
        bounded=counts["bounded"],
        incomplete=counts["incomplete"],
        unqueried=counts["unqueried"],
        priced=priced,
        row_count=rows,
        kinds=tuple(sorted(kinds)),
        query_durations=tuple(durations),
        envelope=envelope,
        payload=payload,
        stable_verified=True,
        query_times=query_times,
        row_kinds=row_kinds,
        item_evidence=item_evidence,
        owned_rows=owned_rows,
        owned_priced_row_count=owned_priced,
    )


def read_stable_saved_scan(path: str | Path, **kwargs: Any) -> SweepEvidence:
    try:
        return qualify_saved_scan(stable_file(path), **kwargs)
    except (OSError, JournalError) as exc:
        raise EvidenceError(str(exc)) from exc


def read_uploads(path: str | Path) -> dict[str, int]:
    """Read the companion's strict durable upload map.

    The companion state is deliberately parsed independently from addon
    ``CROWLEY_DATA``.  HTTP success is not evidence until this file has held
    the exact UID and its integer upload timestamp.
    """

    try:
        value = companion_state.parse_bytes(stable_file(path))
    except (OSError, UnicodeError, companion_state.CompanionStateError, JournalError) as exc:
        raise EvidenceError(f"acknowledgement file is malformed: {exc}") from exc
    return dict(value.uploaded)


def read_acknowledged_ids(path: str | Path) -> tuple[str, ...]:
    """Compatibility view of companion uploads, sorted as UIDs."""

    return tuple(sorted(read_uploads(path), key=lambda item: item.encode()))


def read_data_acknowledged_ids(
    path: str | Path,
    *,
    expected_plan_json: str | None = None,
    expected_plan_hash: str | None = None,
    expected_client_build: str | None = None,
) -> tuple[str, ...]:
    """Read the exact stable acknowledgement arrays from the Data.lua schema."""

    try:
        root = savedvars.loads(stable_file(path))
    except (OSError, UnicodeError, ValueError, JournalError) as exc:
        raise EvidenceError(f"Data.lua is malformed: {exc}") from exc
    value = root.get("CROWLEY_DATA")
    allowed = {"generatedAt", "ackedScans", "scanPlan", "referenceItemIDs", "shoppingLists"}
    if not isinstance(value, dict) or set(value) != allowed:
        raise EvidenceError("CROWLEY_DATA is malformed")
    generated_at = value.get("generatedAt", 0)
    if isinstance(generated_at, bool) or not isinstance(generated_at, int) or generated_at < 0:
        raise EvidenceError("CROWLEY_DATA.generatedAt is invalid")
    ids: list[str] = []
    entries = value.get("ackedScans", [])
    if isinstance(entries, dict) and not entries:
        # WoW's Lua serializer represents an empty array-like table as `{}`;
        # an empty mapping is the only accepted compatibility form.
        entries = []
    if not isinstance(entries, list) or any(not isinstance(item, str) or not item for item in entries):
        raise EvidenceError("CROWLEY_DATA.ackedScans is invalid")
    if entries != sorted(set(entries), key=lambda item: item.encode()):
        raise EvidenceError("CROWLEY_DATA.ackedScans must be unique and byte-sorted")
    ids.extend(entries)
    if len(ids) != len(set(ids)):
        raise EvidenceError("CROWLEY_DATA acknowledgement IDs must be unique")
    catalog = _CANONICAL_CATALOG
    reference_item_ids = value["referenceItemIDs"]
    expected_reference_item_ids = sorted(item.item_id for item in catalog.items)
    if (
        not isinstance(reference_item_ids, list)
        or any(
            isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0
            for item_id in reference_item_ids
        )
        or reference_item_ids != sorted(set(reference_item_ids))
        or reference_item_ids != expected_reference_item_ids
    ):
        raise EvidenceError("CROWLEY_DATA.referenceItemIDs is invalid")
    scan_plan = value["scanPlan"]
    if not isinstance(scan_plan, dict):
        raise EvidenceError("CROWLEY_DATA.scanPlan is invalid")
    pinned = _CANONICAL_PLAN
    if expected_plan_json is None:
        expected_plan_json = pinned["planJSON"]
    if expected_plan_hash is None:
        expected_plan_hash = pinned["planHash"]
    if expected_client_build is None:
        expected_client_build = pinned["targetClientBuild"]
    if (
        expected_plan_json != pinned["planJSON"]
        or expected_plan_hash != pinned["planHash"]
        or expected_client_build != pinned["targetClientBuild"]
    ):
        raise EvidenceError("configured scan plan/build is not the pinned plan")
    expected_plan_keys = {
        "schemaVersion",
        "id",
        "revision",
        "catalogHash",
        "targetClientBuild",
        "cadenceSeconds",
        "initialPriceLevels",
        "itemIDs",
        "pools",
        "planHash",
        "planJSON",
    }
    if set(scan_plan) != expected_plan_keys:
        raise EvidenceError("CROWLEY_DATA.scanPlan shape is invalid")
    integer_fields = ("schemaVersion", "revision", "cadenceSeconds", "initialPriceLevels")
    if any(isinstance(scan_plan[key], bool) or not isinstance(scan_plan[key], int) for key in integer_fields):
        raise EvidenceError("CROWLEY_DATA.scanPlan integer fields are invalid")
    if (
        scan_plan["schemaVersion"] != pinned["schemaVersion"]
        or scan_plan["id"] != pinned["id"]
        or scan_plan["revision"] < 1
        or scan_plan["cadenceSeconds"] != _TARGET.cadence_seconds
        or scan_plan["initialPriceLevels"] != _TARGET.initial_price_levels
        or not isinstance(scan_plan["catalogHash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", scan_plan["catalogHash"])
        or scan_plan["targetClientBuild"] != expected_client_build
        or not isinstance(scan_plan["planHash"], str)
        or scan_plan["planHash"] != expected_plan_hash
        or not isinstance(scan_plan["planJSON"], str)
        or scan_plan["planJSON"] != expected_plan_json
        or hashlib.sha256(scan_plan["planJSON"].encode("utf-8")).hexdigest() != scan_plan["planHash"]
    ):
        raise EvidenceError("CROWLEY_DATA.scanPlan values are invalid")
    item_ids = scan_plan["itemIDs"]
    pools = scan_plan["pools"]
    if (
        not isinstance(item_ids, list)
        or len(item_ids) != _PLAN_COUNT
        or any(
            isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0 for item_id in item_ids
        )
        or item_ids != sorted(set(item_ids))
        or not isinstance(pools, dict)
        or set(pools) != {"core", "discovery"}
    ):
        raise EvidenceError("CROWLEY_DATA.scanPlan item IDs are invalid")
    pool_ids: list[int] = []
    for pool in ("core", "discovery"):
        values = pools[pool]
        if (
            not isinstance(values, list)
            or any(
                isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0
                for item_id in values
            )
            or values != sorted(set(values))
        ):
            raise EvidenceError("CROWLEY_DATA.scanPlan pool values are invalid")
        pool_ids.extend(values)
    if sorted(pool_ids) != item_ids:
        raise EvidenceError("CROWLEY_DATA.scanPlan pools do not partition itemIDs")
    try:
        plan_body = json.loads(scan_plan["planJSON"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError("CROWLEY_DATA.scanPlan.planJSON is invalid") from exc
    if not isinstance(plan_body, dict) or set(plan_body) != {
        "schemaVersion",
        "id",
        "revision",
        "catalogHash",
        "targetClientBuild",
        "cadenceSeconds",
        "initialPriceLevels",
        "itemIDs",
        "pools",
    }:
        raise EvidenceError("CROWLEY_DATA.scanPlan.planJSON shape is invalid")
    if (
        json.dumps(plan_body, sort_keys=True, separators=(",", ":")) != scan_plan["planJSON"]
        or plan_body != {key: scan_plan[key] for key in plan_body}
        or plan_body != {key: pinned[key] for key in plan_body}
        or scan_plan != pinned
    ):
        raise EvidenceError("CROWLEY_DATA.scanPlan.planJSON values are invalid")
    shopping_lists = value["shoppingLists"]
    if isinstance(shopping_lists, dict):
        if shopping_lists:
            raise EvidenceError("CROWLEY_DATA.shoppingLists must be an array")
        shopping_lists = []
    elif not isinstance(shopping_lists, list):
        raise EvidenceError("CROWLEY_DATA.shoppingLists is invalid")
    for index, shopping_list in enumerate(shopping_lists):
        if not isinstance(shopping_list, dict) or set(shopping_list) != {"name", "house", "items"}:
            raise EvidenceError(f"CROWLEY_DATA.shoppingLists[{index}] shape is invalid")
        if (
            not isinstance(shopping_list["name"], str)
            or not shopping_list["name"]
            or not isinstance(shopping_list["house"], str)
            or not shopping_list["house"]
            or not isinstance(shopping_list["items"], list)
            or not shopping_list["items"]
        ):
            raise EvidenceError(f"CROWLEY_DATA.shoppingLists[{index}] values are invalid")
        for item_index, item in enumerate(shopping_list["items"]):
            if not isinstance(item, dict) or set(item) != {"name", "maxPrice", "quantity"}:
                raise EvidenceError(
                    f"CROWLEY_DATA.shoppingLists[{index}].items[{item_index}] shape is invalid"
                )
            if (
                not isinstance(item["name"], str)
                or not item["name"]
                or isinstance(item["maxPrice"], bool)
                or not isinstance(item["maxPrice"], int)
                or item["maxPrice"] < 0
                or isinstance(item["quantity"], bool)
                or not isinstance(item["quantity"], int)
                or item["quantity"] <= 0
            ):
                raise EvidenceError(
                    f"CROWLEY_DATA.shoppingLists[{index}].items[{item_index}] values are invalid"
                )
    return tuple(sorted(set(ids), key=lambda item: item.encode()))


def queues_empty(
    saved_variables: str | Path,
    companion_state: str | Path,
    data_lua: str | Path,
    *,
    settled_uid: str | None = None,
) -> bool:
    """Prove the durable queues are empty while retaining upload history.

    The companion's ``uploaded`` map is a 30-day history, not a queue, so a
    closeout must require the settled UID in that map rather than deleting it.
    The addon data file is subsequently refreshed with an empty ``ackedScans``
    list and the game's scan/sweep queues are empty after logout.
    """

    try:
        root = savedvars.loads(stable_file(saved_variables))
        db = root.get("CrowleyDB")
        if not isinstance(db, dict):
            raise EvidenceError("CrowleyDB is missing")
        if "scans" not in db or "sweeps" not in db:
            raise EvidenceError("CrowleyDB queue fields are missing")
        scans = db["scans"]
        sweeps = db["sweeps"]
        if not isinstance(scans, dict) or not isinstance(sweeps, dict):
            raise EvidenceError("CrowleyDB queue fields are malformed")
        if scans or sweeps:
            return False
        uploads = read_uploads(companion_state)
        data_ids = read_data_acknowledged_ids(data_lua)
    except EvidenceError:
        raise
    except (OSError, UnicodeError, ValueError, JournalError) as exc:
        raise EvidenceError(f"durable queue state is malformed: {exc}") from exc
    if settled_uid is not None and settled_uid not in uploads:
        return False
    return not data_ids


__all__ = [
    "BASELINE_MEDIAN_SECONDS",
    "BASELINE_RANGE_SECONDS",
    "BASELINE_UID",
    "EvidenceError",
    "SweepEvidence",
    "qualify_saved_scan",
    "read_acknowledged_ids",
    "read_data_acknowledged_ids",
    "read_uploads",
    "read_stable_saved_scan",
    "queues_empty",
]
