"""Stable-file evidence readers for addon output and companion acknowledgements."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crowley.core import companion_state, savedvars
from crowley.core.scanformat import ScanFormatError, decode_json_payload, validate_v2_envelope
from crowley.core.watchlist import Catalog

from .journal import JournalError, stable_file

BASELINE_MEDIAN_SECONDS = 1


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


def _plan_body(plan_json: str) -> dict[str, Any]:
    try:
        value = json.loads(plan_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError("canonical plan JSON is invalid") from exc
    if not isinstance(value, dict):
        raise EvidenceError("canonical plan JSON is not an object")
    item_ids = value.get("itemIDs")
    if (
        not isinstance(item_ids, list)
        or not item_ids
        or any(
            isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0 for item_id in item_ids
        )
        or item_ids != sorted(set(item_ids))
    ):
        raise EvidenceError("canonical plan item IDs are invalid")
    return value


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
    planned: int | None = None,
    catalog: Catalog | None = None,
) -> SweepEvidence:
    """Parse one exact SavedVariables file and account for every item and row."""

    if expected_context is None or expected_plan_json is None:
        raise EvidenceError("format-2 qualification requires the pinned context and canonical plan")
    plan_body = _plan_body(expected_plan_json)
    expected_planned = len(plan_body["itemIDs"])
    if planned is None:
        planned = expected_planned
    if isinstance(planned, bool) or not isinstance(planned, int) or planned != expected_planned:
        raise EvidenceError(f"format-2 qualification plan count must be exactly {expected_planned}")
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
        raise EvidenceError(f"planned observation count does not match {expected_planned}")
    counts = {key: 0 for key in ("full", "bounded", "incomplete", "unqueried")}
    attempted = priced = rows = 0
    durations: list[int] = []
    query_times: dict[str, tuple[int, int]] = {}
    kinds: set[str] = set()
    row_kinds: dict[str, int] = {}
    categories = (
        {str(item.item_id): item.category for item in catalog.items if item.status == "active"}
        if catalog is not None
        else {}
    )
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
        category_name = categories.get(str(item_id))
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
    expected_reference_item_ids: Collection[int] | None = None,
) -> tuple[str, ...]:
    """Read the exact stable acknowledgement arrays from the Data.lua schema."""

    try:
        root = savedvars.loads(stable_file(path))
    except (OSError, UnicodeError, ValueError, JournalError) as exc:
        raise EvidenceError(f"Data.lua is malformed: {exc}") from exc
    value = root.get("CROWLEY_DATA")
    allowed = {"generatedAt", "ackedScans", "scanPlan", "referenceItemIDs"}
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
    reference_item_ids = value["referenceItemIDs"]
    configured_reference_item_ids = (
        sorted(set(expected_reference_item_ids)) if expected_reference_item_ids is not None else None
    )
    if (
        not isinstance(reference_item_ids, list)
        or any(
            isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0
            for item_id in reference_item_ids
        )
        or reference_item_ids != sorted(set(reference_item_ids))
        or (configured_reference_item_ids is not None and reference_item_ids != configured_reference_item_ids)
    ):
        raise EvidenceError("CROWLEY_DATA.referenceItemIDs is invalid")
    scan_plan = value["scanPlan"]
    if not isinstance(scan_plan, dict):
        raise EvidenceError("CROWLEY_DATA.scanPlan is invalid")
    configured_plan = any(
        item is not None for item in (expected_plan_json, expected_plan_hash, expected_client_build)
    )
    if configured_plan and not all(
        isinstance(item, str) and item
        for item in (expected_plan_json, expected_plan_hash, expected_client_build)
    ):
        raise EvidenceError("configured scan plan/build is incomplete")
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
        scan_plan["revision"] < 1
        or not isinstance(scan_plan["catalogHash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", scan_plan["catalogHash"])
        or (configured_plan and scan_plan["targetClientBuild"] != expected_client_build)
        or not isinstance(scan_plan["planHash"], str)
        or (configured_plan and scan_plan["planHash"] != expected_plan_hash)
        or not isinstance(scan_plan["planJSON"], str)
        or (configured_plan and scan_plan["planJSON"] != expected_plan_json)
        or hashlib.sha256(scan_plan["planJSON"].encode("utf-8")).hexdigest() != scan_plan["planHash"]
    ):
        raise EvidenceError("CROWLEY_DATA.scanPlan values are invalid")
    item_ids = scan_plan["itemIDs"]
    pools = scan_plan["pools"]
    if (
        not isinstance(item_ids, list)
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
    if json.dumps(plan_body, sort_keys=True, separators=(",", ":")) != scan_plan["planJSON"] or plan_body != {
        key: scan_plan[key] for key in plan_body
    }:
        raise EvidenceError("CROWLEY_DATA.scanPlan.planJSON values are invalid")
    if configured_plan:
        if not isinstance(expected_plan_json, str):
            raise EvidenceError("configured scan plan/build is incomplete")
        if json.loads(expected_plan_json) != plan_body:
            raise EvidenceError("configured scan plan does not match CROWLEY_DATA.scanPlan")
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
    list and the game's scan queue is empty after logout.
    """

    try:
        root = savedvars.loads(stable_file(saved_variables))
        db = root.get("CrowleyDB")
        if not isinstance(db, dict):
            raise EvidenceError("CrowleyDB is missing")
        if "sweeps" in db:
            raise EvidenceError("CrowleyDB contains legacy sweeps queue field")
        if "scans" not in db:
            raise EvidenceError("CrowleyDB scans queue field is missing")
        scans = db["scans"]
        if not isinstance(scans, dict):
            raise EvidenceError("CrowleyDB scans queue field is malformed")
        if scans:
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
    "EvidenceError",
    "SweepEvidence",
    "qualify_saved_scan",
    "read_acknowledged_ids",
    "read_data_acknowledged_ids",
    "read_uploads",
    "read_stable_saved_scan",
    "queues_empty",
]
