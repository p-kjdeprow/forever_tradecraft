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
    decode_json_payload,
    encode_json_payload,
    sweep_item_ids,
    validate_envelope,
)
from crowley.core.watchlist import Catalog, load_catalog
from crowley.gui.frames import PLAN_COUNT, ProtocolContract, ProtocolError, validate_snapshot
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


def test_cold_slices_cannot_exceed_the_number_of_cold_items() -> None:
    with pytest.raises(ValueError, match="empty slices"):
        synthetic_catalog().scan_plan(cold_slices=8)


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


def _stored_scans(client: WowClient) -> list[dict]:
    stored = savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"]
    return sorted(stored.values(), key=lambda scan: scan["startedAt"])


def _db(client: WowClient):
    return client.lua.globals().CrowleyDB


@pytest.fixture(scope="module")
def cold_sweeps() -> list[dict]:
    """Four consecutive sweeps of the cold plan: slices 0, 1, 2, then 0 again (wrap)."""
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    assert _db(client).coldSliceNext is None
    expected_next = [1, 2, 0, 1]
    for index in range(COLD_SLICES + 1):
        _scan_once(client, force=index > 0)
        assert _db(client).coldSliceNext == expected_next[index]
        assert _db(client).coldSliceCount == COLD_SLICES
    scans = _stored_scans(client)
    assert len(scans) == COLD_SLICES + 1
    return scans


def test_addon_rotates_slices_with_a_persisted_counter(cold_sweeps: list[dict]) -> None:
    plan = cold_plan()
    assert [raw["coldSlice"] for raw in cold_sweeps] == [
        {"index": index % COLD_SLICES, "count": COLD_SLICES} for index in range(COLD_SLICES + 1)
    ]
    for raw in cold_sweeps:
        k = raw["coldSlice"]["index"]
        validated = validate_envelope(envelope_of(raw))
        # The explicit observation keys are the attempted set.
        assert sorted(int(key) for key in validated["decoded"]["observations"]) == sweep_item_ids(plan, k)
        assert validated["decoded"]["planJSON"] == plan["planJSON"]
        assert raw["numObservations"] == len(sweep_item_ids(plan, k))


def _timed_out_sweep(client: WowClient, *, close: bool = False) -> dict:
    """A sweep that ends with an unqueried tail: by sweep timeout, or by closing the auction house."""
    before = len(_stored_scans(client))
    client.set_auctions([Auction(itemID=2589, name="Linen Cloth", count=1, buyout=100)])
    client.set_event_behavior(delay=601)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan force")
    if close:
        client.fire("AUCTION_HOUSE_CLOSED")
    else:
        client.advance(600)
        client.run_timers()
    scans = _stored_scans(client)
    assert len(scans) == before + 1
    return scans[-1]


@pytest.mark.parametrize("close", [False, True])
def test_aborted_or_timed_out_cold_sweep_validates_and_advances_the_counter(close: bool) -> None:
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    raw = _timed_out_sweep(client, close=close)
    validated = validate_envelope(envelope_of(raw))
    observations = validated["decoded"]["observations"]
    assert any(obs["query"]["kind"] == "unqueried" for obs in observations.values())
    assert sorted(int(key) for key in observations) == sweep_item_ids(cold_plan(), 0)
    assert _db(client).coldSliceNext == 1


def test_cursor_on_a_cold_item_from_another_slice_resumes_in_the_swept_set() -> None:
    plan = cold_plan()
    swept = sweep_item_ids(plan, 0)
    foreign = next(item for item in plan["pools"]["cold"] if item not in swept)
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    _db(client).cursorItemID = foreign
    _db(client).cursorPlanHash = plan["planHash"]
    raw = _timed_out_sweep(client)
    observations = validate_envelope(envelope_of(raw))["decoded"]["observations"]
    assert sorted(int(key) for key in observations) == swept
    # Resume after the last swept item at or below the cursor.
    before = [item for item in swept if item <= foreign]
    first = next(key for key, obs in observations.items() if obs["ordinal"] == 1)
    assert int(first) == swept[(swept.index(before[-1]) + 1) % len(swept)]


@pytest.mark.parametrize(
    ("stored_next", "stored_count", "reason"),
    [(1, 5, "count_changed"), (7, COLD_SLICES, "out_of_range"), (-1, COLD_SLICES, "out_of_range")],
)
def test_counter_resets_loudly_when_count_changed_or_value_out_of_range(
    stored_next: int, stored_count: int, reason: str
) -> None:
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    _db(client).coldSliceNext = stored_next
    _db(client).coldSliceCount = stored_count
    raw = _timed_out_sweep(client)
    assert raw["coldSlice"] == {"index": 0, "count": COLD_SLICES}
    assert _db(client).coldSliceReset.reason == reason
    assert _db(client).coldSliceNext == 1
    assert _db(client).coldSliceCount == COLD_SLICES
    events = [event.type for event in _db(client).lastQueryTrace.events.values()]
    assert "cold_slice_reset" in events


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


def test_server_rejects_an_index_that_disagrees_with_the_attempted_set(cold_sweeps: list[dict]) -> None:
    raw = cold_sweeps[0]

    def set_index(envelope: dict, _decoded: dict) -> None:
        envelope["coldSlice"]["index"] = 1

    with pytest.raises(ScanFormatError, match="exactly one entry per swept item"):
        validate_envelope(_mutated(raw, set_index))


@pytest.mark.parametrize("index", [COLD_SLICES, COLD_SLICES + 4])
def test_server_rejects_an_index_at_or_above_the_slice_count(cold_sweeps: list[dict], index: int) -> None:
    def set_index(envelope: dict, _decoded: dict) -> None:
        envelope["coldSlice"]["index"] = index

    with pytest.raises(ScanFormatError, match="below the slice count"):
        validate_envelope(_mutated(cold_sweeps[0], set_index))


def test_server_rejects_an_explicit_null_cold_slice(cold_sweeps: list[dict]) -> None:
    def null(envelope: dict, _decoded: dict) -> None:
        envelope["coldSlice"] = None

    with pytest.raises(ScanFormatError, match="coldSlice must be an object"):
        validate_envelope(_mutated(cold_sweeps[0], null))
    plain = envelope_of(cold_sweeps[0])
    plain["coldSlice"] = None
    with pytest.raises(ScanFormatError):
        validate_envelope(plain)


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


def _contract(plan: dict) -> ProtocolContract:
    return ProtocolContract.from_plan(plan)


def test_scanner_planned_must_equal_hot_plus_one_slice() -> None:
    plan = cold_plan()
    contract = _contract(plan)
    hot = len(plan["pools"]["core"]) + len(plan["pools"]["discovery"])
    sizes = sorted({hot + len(plan["pools"]["cold"][j::COLD_SLICES]) for j in range(COLD_SLICES)})
    assert sizes == sorted(contract.sweep_sizes) or set(sizes) == set(contract.sweep_sizes)
    for planned in sizes:
        value = snapshot()
        value["plan"] = {**value["plan"], "count": contract.plan_count}
        value["scanner"] = {**value["scanner"], "planned": planned}
        validate_snapshot(value, contract=replace_contract(contract))
    for planned in (contract.plan_count, hot, hot + 1 if hot + 1 not in sizes else 0, sizes[0] - 1):
        value = snapshot()
        value["plan"] = {**value["plan"], "count": contract.plan_count}
        value["scanner"] = {**value["scanner"], "planned": planned}
        with pytest.raises(ProtocolError, match="scanner.planned"):
            validate_snapshot(value, contract=replace_contract(contract))


def replace_contract(contract: ProtocolContract) -> ProtocolContract:
    """The real contract with the default fixture's cadence/cap/levels (the snapshot fixture's values)."""
    return ProtocolContract(contract.plan_count, 900, 600, 20, contract.plan_item_ids, contract.planned_sizes)


def test_scanner_planned_is_exactly_plan_count_without_cold_items() -> None:
    value = snapshot()
    value["scanner"] = {**value["scanner"], "planned": PLAN_COUNT - 5}
    with pytest.raises(ProtocolError, match="scanner.planned"):
        validate_snapshot(value)
    validate_snapshot(snapshot())


def test_last_saved_planned_keeps_the_previous_plan_size_relaxation() -> None:
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


def test_idle_scanner_status_reports_the_next_sweep_set_size() -> None:
    plan = cold_plan()
    client = WowClient(target=cold_target(), catalog=synthetic_catalog())
    client.login()
    client.set_canonical_plan()
    assert client.ns.Scanner.Status().planned == len(sweep_item_ids(plan, 0))
    _db(client).coldSliceNext = 1
    _db(client).coldSliceCount = COLD_SLICES
    assert client.ns.Scanner.Status().planned == len(sweep_item_ids(plan, 1))
