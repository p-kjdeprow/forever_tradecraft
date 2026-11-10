"""The rotating cold pool: plan identity, slice assignment, addon sweeps and server validation."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from crowley.core import savedvars
from crowley.core.game_target import target_scan_plan
from crowley.core.scanformat import (
    ScanFormatError,
    cold_slice_index,
    decode_json_payload,
    encode_json_payload,
    sweep_item_ids,
    validate_envelope,
)
from crowley.core.watchlist import Catalog, load_catalog
from crowley.gui.frames import PLAN_COUNT, ProtocolError, validate_snapshot
from tests.support.scan_frames import snapshot

from .wow_harness import TARGET, Auction, WowClient

COLD_SLICES = 3
CADENCE = 900


def synthetic_catalog(cold_count: int = 7) -> Catalog:
    """The shipped catalog with a few core items moved to the cold pool (no real cold entries)."""
    data = load_catalog().model_dump(mode="json")
    core = [item for item in data["items"] if item["status"] == "active" and item["pool"] == "core"]
    for item in core[50 : 50 + cold_count]:
        item["pool"] = "cold"
    return Catalog.model_validate_json(json.dumps(data))


def cold_target():
    return replace(TARGET, cold_slices=COLD_SLICES)


def cold_plan() -> dict:
    return target_scan_plan(synthetic_catalog(), cold_target())


def envelope_of(raw: dict) -> dict:
    keys = (
        "id",
        "format",
        "startedAt",
        "finishedAt",
        "house",
        "context",
        "numRows",
        "numObservations",
        "payload",
    )
    return {key: raw[key] for key in (*keys, "coldSlice") if key in raw}


# --- plan identity ---------------------------------------------------------------------------


def test_plan_without_cold_items_has_zero_slices_and_empty_cold_pool() -> None:
    plan = target_scan_plan(load_catalog(), TARGET)
    assert plan["coldSlices"] == 0
    assert plan["pools"]["cold"] == []
    assert '"coldSlices":0' in plan["planJSON"]
    assert '"pools":{"cold":[],"core":[' in plan["planJSON"]


def test_plan_with_cold_items_carries_slices_and_pool_in_the_hash() -> None:
    plan = cold_plan()
    assert plan["coldSlices"] == COLD_SLICES
    assert len(plan["pools"]["cold"]) == 7
    assert plan["itemIDs"] == sorted(
        plan["pools"]["core"] + plan["pools"]["discovery"] + plan["pools"]["cold"]
    )
    assert plan["planHash"] == hashlib.sha256(plan["planJSON"].encode()).hexdigest()
    assert json.loads(plan["planJSON"])["coldSlices"] == COLD_SLICES


def test_cold_slices_must_be_positive_exactly_when_cold_items_exist() -> None:
    with pytest.raises(ValueError, match="cold"):
        synthetic_catalog().scan_plan(cold_slices=0)
    with pytest.raises(ValueError, match="cold"):
        load_catalog().scan_plan(cold_slices=COLD_SLICES)
    with pytest.raises(ValueError, match="cold_slices"):
        load_catalog().scan_plan(cold_slices=-1)


# --- slice assignment ------------------------------------------------------------------------


def test_cold_item_i_belongs_to_slice_i_mod_n() -> None:
    plan = cold_plan()
    cold = plan["pools"]["cold"]
    hot = plan["pools"]["core"] + plan["pools"]["discovery"]
    seen: list[int] = []
    for k in range(COLD_SLICES):
        ids = sweep_item_ids(plan, k)
        assert ids == sorted(ids)
        assert set(ids) == set(hot) | {item for i, item in enumerate(cold) if i % COLD_SLICES == k}
        seen.extend(item for item in ids if item in cold)
    assert sorted(seen) == sorted(cold)


def test_slice_index_follows_start_time_and_cadence() -> None:
    assert cold_slice_index(1_760_000_000, CADENCE, COLD_SLICES) == (1_760_000_000 // CADENCE) % COLD_SLICES
    base = 900 * 1000
    assert [cold_slice_index(base + 900 * n + 17, CADENCE, 6) for n in range(7)] == [
        (1000 + n) % 6 for n in range(7)
    ]
    assert cold_slice_index(base + 899, CADENCE, 6) == cold_slice_index(base, CADENCE, 6)


def test_sweep_items_without_cold_plan_is_the_whole_plan() -> None:
    plan = target_scan_plan(load_catalog(), TARGET)
    assert sweep_item_ids(plan, None) == plan["itemIDs"]
    with pytest.raises(ScanFormatError):
        sweep_item_ids(plan, 0)


# --- addon sweeps ----------------------------------------------------------------------------


def _scan_once(client: WowClient, *, force: bool) -> None:
    before = len(list(client.lua.globals().CrowleyDB.scans.items()))
    client.set_auctions([Auction(itemID=2589, name="Linen Cloth", count=2, buyout=200)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan force" if force else "scan")
    for _ in range(700):
        client.run_timers()
        if len(list(client.lua.globals().CrowleyDB.scans.items())) > before:
            return
        client.advance_with_timers(1)
    raise AssertionError("sweep did not finish")


@pytest.fixture(scope="module")
def cold_sweeps() -> list[dict]:
    """Three consecutive sweeps of the cold plan, one per slice."""
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    scans: list[dict] = []
    for index in range(COLD_SLICES):
        _scan_once(client, force=index > 0)
        # Start the next sweep one bucket later so it takes the next slice.
        client.advance(CADENCE - int(client.lua.globals()._now) % CADENCE + 1)
        stored = savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"]
        scans = sorted(stored.values(), key=lambda scan: scan["startedAt"])
    assert len(scans) == COLD_SLICES
    return scans


def test_addon_sweeps_hot_items_plus_the_slice_for_its_start_time(cold_sweeps: list[dict]) -> None:
    plan = cold_plan()
    indexes = []
    for raw in cold_sweeps:
        expected_k = (raw["startedAt"] // CADENCE) % COLD_SLICES
        assert raw["coldSlice"] == {"index": expected_k, "count": COLD_SLICES}
        validated = validate_envelope(envelope_of(raw))
        # The explicit observation keys are the attempted set.
        assert sorted(int(key) for key in validated["decoded"]["observations"]) == sweep_item_ids(
            plan, expected_k
        )
        assert validated["decoded"]["planJSON"] == plan["planJSON"]
        assert raw["numObservations"] == len(sweep_item_ids(plan, expected_k))
        indexes.append(expected_k)
    assert sorted(indexes) == list(range(COLD_SLICES))


def test_addon_envelope_without_cold_items_has_no_cold_slice_field() -> None:
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    _scan_once(client, force=False)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    assert "coldSlice" not in raw
    validated = validate_envelope(envelope_of(raw))
    assert validated["plan"]["coldSlices"] == 0
    assert raw["numObservations"] == len(validated["plan"]["itemIDs"])


# --- server validation -----------------------------------------------------------------------


def _mutated(raw: dict, mutate) -> dict:
    envelope = envelope_of(copy.deepcopy(raw))
    decoded = decode_json_payload(envelope["payload"])
    mutate(envelope, decoded)
    envelope["payload"] = encode_json_payload(decoded)
    return envelope


def test_server_accepts_the_exact_slice_set(cold_sweeps: list[dict]) -> None:
    for raw in cold_sweeps:
        validate_envelope(envelope_of(raw))


def _cold_items(decoded: dict) -> list[str]:
    plan = json.loads(decoded["planJSON"])
    return [str(item) for item in plan["pools"]["cold"]]


def test_server_rejects_a_missing_slice_item(cold_sweeps: list[dict]) -> None:
    raw = cold_sweeps[0]

    def drop(_envelope: dict, decoded: dict) -> None:
        member = next(key for key in decoded["observations"] if key in _cold_items(decoded))
        del decoded["observations"][member]

    with pytest.raises(ScanFormatError, match="exactly one entry per swept item"):
        validate_envelope(_mutated(raw, drop))


def test_server_rejects_an_extra_cold_item_from_another_slice(cold_sweeps: list[dict]) -> None:
    raw = cold_sweeps[0]

    def add(_envelope: dict, decoded: dict) -> None:
        other = next(key for key in _cold_items(decoded) if key not in decoded["observations"])
        decoded["observations"][other] = copy.deepcopy(next(iter(decoded["observations"].values())))

    with pytest.raises(ScanFormatError, match="exactly one entry per swept item"):
        validate_envelope(_mutated(raw, add))


def test_server_rejects_a_slice_index_that_does_not_match_the_start_time(cold_sweeps: list[dict]) -> None:
    raw = cold_sweeps[0]
    wrong = (raw["coldSlice"]["index"] + 1) % COLD_SLICES

    def set_index(envelope: dict, _decoded: dict) -> None:
        envelope["coldSlice"]["index"] = wrong

    with pytest.raises(ScanFormatError, match="index does not match"):
        validate_envelope(_mutated(raw, set_index))


def test_server_rejects_wrong_slice_count_or_missing_field(cold_sweeps: list[dict]) -> None:
    raw = cold_sweeps[0]

    def set_count(envelope: dict, _decoded: dict) -> None:
        envelope["coldSlice"]["count"] = COLD_SLICES + 1

    with pytest.raises(ScanFormatError, match="count does not match"):
        validate_envelope(_mutated(raw, set_count))

    def remove(envelope: dict, _decoded: dict) -> None:
        del envelope["coldSlice"]

    with pytest.raises(ScanFormatError, match="coldSlice is required"):
        validate_envelope(_mutated(raw, remove))


def test_server_rejects_a_cold_slice_on_a_plan_without_cold_items() -> None:
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    _scan_once(client, force=False)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    envelope = envelope_of(raw)
    envelope["coldSlice"] = {"index": 0, "count": 1}
    with pytest.raises(ScanFormatError, match="only valid for a plan with cold slices"):
        validate_envelope(envelope)


def test_pre_cold_tier_envelopes_still_validate() -> None:
    """A plan written before the cold tier has no coldSlices key and no pools.cold."""
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    _scan_once(client, force=False)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))

    def legacy(_envelope: dict, decoded: dict) -> None:
        plan = json.loads(decoded["planJSON"])
        del plan["coldSlices"]
        del plan["pools"]["cold"]
        text = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        decoded["planJSON"] = text
        decoded["planHash"] = hashlib.sha256(text.encode()).hexdigest()

    validated = validate_envelope(_mutated(raw, legacy))
    assert "coldSlices" not in validated["plan"]
    assert len(validated["decoded"]["observations"]) == len(validated["plan"]["itemIDs"])


def test_plan_with_inconsistent_cold_slices_is_rejected() -> None:
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    _scan_once(client, force=False)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))

    def bad(_envelope: dict, decoded: dict) -> None:
        plan = json.loads(decoded["planJSON"])
        plan["coldSlices"] = 2
        text = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        decoded["planJSON"] = text
        decoded["planHash"] = hashlib.sha256(text.encode()).hexdigest()

    with pytest.raises(ScanFormatError, match="coldSlices must be positive exactly when"):
        validate_envelope(_mutated(raw, bad))


# --- frames decoder --------------------------------------------------------------------------


def test_scanner_planned_may_be_a_subset_of_the_plan() -> None:
    value = snapshot()
    value["scanner"] = {**value["scanner"], "planned": PLAN_COUNT - 5}
    validate_snapshot(value)


@pytest.mark.parametrize("planned", [0, PLAN_COUNT + 1])
def test_scanner_planned_outside_one_to_plan_count_is_rejected(planned: int) -> None:
    value = snapshot()
    value["scanner"] = {**value["scanner"], "planned": planned}
    with pytest.raises(ProtocolError, match="scanner.planned"):
        validate_snapshot(value)


def test_last_saved_planned_may_be_a_cold_slice_sized_set() -> None:
    value = snapshot()
    value["lastSaved"] = {
        **value["lastSaved"],
        "memoryUid": "us-normal-alliance-1794268488-Dandune-5418",
        "finishedAt": 1_000,
        "planned": PLAN_COUNT - 100,
        "attempted": PLAN_COUNT - 100,
        "failures": 0,
        "rows": 4,
    }
    validate_snapshot(value)
