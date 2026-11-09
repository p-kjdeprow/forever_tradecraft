"""Durable companion/Data.lua boundaries for the repeating session."""

import os
from pathlib import Path

import pytest

from crowley.core.luawriter import render_assignment
from crowley.core.scanformat import encode_json_payload
from crowley.core.watchlist import load_catalog
from crowley.session.evidence import (
    EvidenceError,
    qualify_saved_scan,
    queues_empty,
    read_data_acknowledged_ids,
    read_uploads,
)
from tests.support.companion_state import write_companion_state


def _private_write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _pinned_plan() -> dict:
    return load_catalog().scan_plan()


def test_empty_scans_are_accepted_with_distinct_ack_surfaces(tmp_path: Path) -> None:
    companion = tmp_path / "companion-state.json"
    write_companion_state(companion, uploaded={"uid-1": 1000})
    data = tmp_path / "Data.lua"
    _private_write(
        data,
        render_assignment(
            "CROWLEY_DATA",
            {
                "generatedAt": 1,
                "ackedScans": ["uid-1"],
                "scanPlan": _pinned_plan(),
                "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
            },
            header="test",
        ).encode(),
    )

    assert read_uploads(companion) == {"uid-1": 1000}
    assert read_data_acknowledged_ids(data) == ("uid-1",)

    _private_write(
        data,
        render_assignment(
            "CROWLEY_DATA",
            {
                "generatedAt": 1,
                "ackedScans": [],
                "scanPlan": _pinned_plan(),
                "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
            },
            header="test",
        ).encode(),
    )
    saved = tmp_path / "Crowley.lua"
    _private_write(
        saved,
        render_assignment("CrowleyDB", {"scans": {}}, header="test").encode(),
    )
    assert queues_empty(saved, companion, data, settled_uid="uid-1")


def test_data_rejects_removed_shopping_lists_key(tmp_path: Path) -> None:
    data = tmp_path / "Data.lua"
    value = {
        "generatedAt": 1,
        "ackedScans": [],
        "scanPlan": _pinned_plan(),
        "referenceItemIDs": sorted(item.item_id for item in load_catalog().items),
        "shoppingLists": [],
    }
    _private_write(data, render_assignment("CROWLEY_DATA", value, header="stale").encode())

    with pytest.raises(EvidenceError, match="CROWLEY_DATA is malformed"):
        read_data_acknowledged_ids(data)


def test_legacy_sweeps_queue_is_rejected(tmp_path: Path) -> None:
    saved = tmp_path / "Crowley.lua"
    _private_write(
        saved,
        render_assignment("CrowleyDB", {"scans": {}, "sweeps": {}}, header="legacy").encode(),
    )

    with pytest.raises(EvidenceError, match="legacy sweeps"):
        queues_empty(saved, tmp_path / "companion.json", tmp_path / "Data.lua")


def test_nonempty_scans_queue_is_not_settled(tmp_path: Path) -> None:
    saved = tmp_path / "Crowley.lua"
    _private_write(
        saved,
        render_assignment("CrowleyDB", {"scans": {"uid-1": {}}}, header="pending").encode(),
    )

    assert not queues_empty(saved, tmp_path / "companion.json", tmp_path / "Data.lua")


def test_companion_legacy_state_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "companion-state.json"
    _private_write(path, b'{"uploaded":{"uid-1":1000},"probes":[]}')
    try:
        read_uploads(path)
    except EvidenceError:
        pass
    else:
        raise AssertionError("legacy companion state must not stand in for the current versioned contract")


@pytest.mark.parametrize(
    ("owned_section", "owned_complete"),
    [
        (
            {
                "status": "unqueried",
                "query": {"kind": "unqueried"},
                "reasonCode": "not_started",
                "rowCount": 0,
                "rows": [],
            },
            False,
        ),
        (
            {
                "status": "full",
                "query": {"kind": "queried", "startedAt": 101, "finishedAt": 102},
                "reasonCode": "ok",
                "rowCount": 0,
                "rows": [],
            },
            True,
        ),
    ],
)
def test_qualify_saved_scan_extracts_complete_native_rows_and_owned_section(
    tmp_path: Path, owned_section: dict, owned_complete: bool
) -> None:
    plan = load_catalog().scan_plan()
    uid = "native-428"
    observations: dict[str, dict] = {}
    for ordinal, item_id in enumerate(plan["itemIDs"], 1):
        item_key = str(item_id)
        kind = "commodity" if ordinal == 1 else "item"
        row = {
            "resultID": ordinal,
            "itemID": item_id,
            "itemKey": item_key,
            "rawItemKey": {
                "itemID": item_id,
                "itemLevel": 0,
                "itemSuffix": 0,
                "battlePetSpeciesID": 0,
                "bonusIDs": [],
            },
            "link": {"kind": "unknown"}
            if kind == "commodity"
            else {"kind": "known", "value": f"item:{item_id}:0:0:0:0:0:0:0:0:0:0:0:0"},
            "quantity": 1,
            "price": {"kind": "unit" if kind == "commodity" else "auction", "copper": 100 + ordinal},
            "unitPrice": {"numerator": 100 + ordinal, "denominator": 1},
            "owners": {
                "names": [],
                "totalNumberOfOwners": 0,
                "containsOwnerItem": False,
                "containsAccountItem": False,
                "identityKind": "grouped",
            },
            "timeLeft": {"kind": "unknown"},
        }
        observations[item_key] = {
            "itemID": item_id,
            "ordinal": ordinal,
            "kind": kind,
            "query": {"kind": "queried", "startedAt": 100, "finishedAt": 101},
            "coverage": "full",
            "sourceComplete": True,
            "cutoff": {"kind": "unbounded"},
            "higherPriceWitness": {"kind": "unknown"},
            "reasonCode": "ok",
            "rowCount": 1,
            "rows": {"row:1": row},
        }
    payload = {
        "planHash": plan["planHash"],
        "planJSON": plan["planJSON"],
        "observations": observations,
        "owned": owned_section,
    }
    envelope = {
        "id": uid,
        "format": 2,
        "startedAt": 100,
        "finishedAt": 102,
        "house": {
            "region": "beta",
            "ruleset": "normal",
            "faction": "alliance",
            "source": "auto",
            "key": "beta-normal-alliance",
        },
        "context": {
            "character": "Arthur",
            "realm": "Classic Beta PvE 2",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
            "clientBuild": "70450",
            "interface": 16001,
            "addonVersion": "0.2.0",
        },
        "numRows": 428,
        "numObservations": 428,
        "payload": encode_json_payload(payload),
    }
    saved = tmp_path / "Crowley.lua"
    _private_write(
        saved,
        render_assignment("CrowleyDB", {"scans": {uid: envelope}}, "native").encode(),
    )
    parsed = qualify_saved_scan(
        saved.read_bytes(),
        uid=uid,
        house="beta-normal-alliance",
        plan_hash=plan["planHash"],
        expected_plan_json=plan["planJSON"],
        expected_context=envelope["context"],
    )
    assert parsed.observations == parsed.attempted == parsed.full == 428
    assert parsed.row_count == parsed.priced == 428
    assert set(parsed.kinds) == {"commodity", "item"}
    assert len(parsed.item_evidence) == 428
    assert parsed.owned_rows == [] and parsed.owned_priced_row_count == 0
    # An empty owned section qualifies only when the owned query completed.
    assert parsed.owned_query_complete is owned_complete
