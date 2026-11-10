"""Targeted watchlist Lua behavior using the real addon under Lua 5.1."""

import base64
import hashlib
import json
import math
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from lupa import lua51

from crowley.core import savedvars
from crowley.core.game_target import packaged_game_target
from crowley.core.scanformat import ScanFormatError, validate_v2_envelope
from crowley.core.watchlist import load_catalog
from crowley.gui.frames import PixelDecoder, decode_frame

from .wow_harness import Auction, WowClient

FIXTURES = Path(__file__).parent / "fixtures"


def non_beta_target(*, ruleset: str = "normal", realm: str = "Normal"):
    target = packaged_game_target()
    faction = "Alliance"
    return replace(
        target,
        region="us",
        native_region_id=1,
        ruleset=ruleset,
        realm=realm,
        faction=faction,
        house=f"us-{ruleset}-{faction.lower()}",
    )


def run_sweep(
    client: WowClient, item_ids: list[int] | None = None, settings: dict[str, str] | None = None
) -> dict:
    client.login()
    for field, value in (settings or {}).items():
        client.lua.globals().CrowleyDB.settings[field] = value
    # Every successful production sweep uses the packaged canonical
    # plan.  ``item_ids`` is retained only for callers from the older focused
    # tests; those tests inspect a canonical observation while the harness
    # advances the real rolling throttle window.
    client.set_canonical_plan()
    client.set_auctions([Auction(itemID=2589, name="Linen Cloth", count=2, buyout=200)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    for _ in range(700):
        client.run_timers()
        if list(client.lua.globals().CrowleyDB.scans.items()):
            break
        client.advance_with_timers(1)
    scans = savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"]
    assert len(scans) == 1
    return next(iter(scans.values()))


def envelope(raw: dict) -> dict:
    result = {
        key: raw[key]
        for key in (
            "id",
            "format",
            "startedAt",
            "finishedAt",
            "house",
            "context",
            "numRows",
            "numObservations",
        )
    }
    result["payload"] = raw["payload"]
    return result


def deferred_native_client() -> WowClient:
    """Reach the real post-reload deferred boundary through the slash command."""

    source = WowClient()
    source.login()
    source.slash("agent defer")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]
    client = WowClient(initial_savedvars=saved)
    client.login()
    return client


def pending_restore_savedvars() -> dict:
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    return savedvars.loads(source.saved_variables())["CrowleyDB"]


def queued_timer_count(client: WowClient) -> int:
    return len(list(client.lua.globals()._timers.values()))


def decoded_payload(client: WowClient) -> dict:
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    return validate_v2_envelope(envelope(raw))["decoded"]


def schedule_trace(client: WowClient) -> dict:
    return json.loads(
        client.lua.globals().C_EncodingUtil.SerializeJSON(client.lua.globals().CrowleyDB.scheduleTrace)
    )


def load_pure_lua_modules(*names: str) -> tuple[lua51.LuaRuntime, object]:
    """Load pure addon modules into a Lua runtime with no WoW globals."""

    lua = lua51.LuaRuntime(unpack_returned_tuples=True, encoding="latin-1")
    namespace = lua.table()
    namespace.RegisterModule = lambda _name, _spec: None
    for name in names:
        source = (Path(__file__).parents[1] / "addon" / "Crowley" / f"{name}.lua").read_text()
        lua.execute(f"return function(...) {source}\nend")("Crowley", namespace)
    return lua, namespace


def test_pure_addon_state_and_encoders_have_no_native_dependency():
    lua, namespace = load_pure_lua_modules(
        "GeneratedSchemas", "ScannerState", "AgentState", "ExecutorState", "ScanEncoder", "AgentEncoder"
    )
    order = namespace.ScannerState.PlanOrder(lua.table_from([10, 20, 30]), 20)
    assert list(order.values()) == [30, 10, 20]
    with pytest.raises(Exception, match="must not be empty"):
        namespace.ScannerState.PlanOrder(lua.table(), None)

    sweep = namespace.ScannerState.NewSweep(
        lua.table_from({"itemIDs": lua.table_from([10])}),
        lua.table_from({"key": "beta-normal-horde"}),
        lua.table_from({"character": "Tester"}),
        1,
        1,
    )
    reduced = namespace.ScannerState.Reduce(
        sweep,
        lua.table_from(
            {
                "type": "item_completed",
                "itemID": 10,
                "observation": lua.table_from({}),
                "cursor": 10,
                "nextOrdinal": 2,
            }
        ),
    )
    assert reduced.observations["10"] is not None
    rows = lua.table_from(
        [
            lua.table_from({"unitPrice": lua.table_from({"numerator": 100, "denominator": 1})}),
            lua.table_from({"unitPrice": lua.table_from({"numerator": 200, "denominator": 1})}),
            lua.table_from({"unitPrice": lua.table_from({"numerator": 300, "denominator": 1})}),
        ]
    )
    full = namespace.ScannerState.Coverage(rows, 3, True, None)
    assert full.coverage == "full" and full.sourceComplete is True and full.reasonCode == "ok"
    bounded = namespace.ScannerState.Coverage(rows, 2, True, None)
    assert bounded.coverage == "bounded"
    assert bounded.cutoff.numerator == 200 and bounded.higherPriceWitness.numerator == 300
    incomplete = namespace.ScannerState.Coverage(rows, 3, False, "metadata_unavailable")
    assert incomplete.coverage == "incomplete" and incomplete.sourceComplete is False
    assert incomplete.reasonCode == "metadata_unavailable"
    with pytest.raises(Exception, match="positive integer"):
        namespace.ScannerState.Coverage(rows, 0, True, None)
    with pytest.raises(Exception, match="no rational unit price"):
        namespace.ScannerState.Coverage(lua.table_from([lua.table_from({})]), 1, True, None)

    lifecycle = namespace.AgentState.New("ordinary")
    deferred = namespace.AgentState.Transition(lifecycle, "defer")
    assert deferred.lifecycle == "deferred"
    invalid, reason = namespace.AgentState.Transition(deferred, "resume")
    assert invalid is None and reason == "invalid_transition"
    with pytest.raises(Exception, match="lifecycle is invalid"):
        namespace.AgentState.New("held")
    held = namespace.AgentState.Measurement(lifecycle, 0)
    assert held.lifecycle == "ordinary" and held.nativePhase == "held" and held.nativeOrdinal == 0
    updating = namespace.AgentState.Measurement(held, 1)
    assert updating.nativePhase == "updates" and updating.nativeOrdinal == 1
    completed = namespace.AgentState.Measurement(updating, 100)
    assert completed.nativePhase == "held" and completed.measurementCompleted is True
    resumed = namespace.AgentState.Transition(completed, "resume")
    assert resumed.lifecycle == "ordinary" and resumed.nativePhase == "normal" and resumed.nativeOrdinal == 0
    with pytest.raises(Exception, match="measurement input is malformed"):
        namespace.AgentState.Measurement(deferred, 1)

    schemas = namespace.GeneratedWireSchemas
    queue = lua.table_from(
        {
            "format": 1,
            "queueId": "q",
            "generatedAt": 1,
            "expiresAt": 2,
            "house": "beta-normal-horde",
            "character": "Tester",
            "armed": False,
            "actions": lua.table_from(
                [lua.table_from({"seq": 1, "kind": "vendor_sell", "itemID": 10, "quantity": 1})]
            ),
        }
    )
    valid = namespace.ExecutorState.ValidateQueue(queue, schemas)
    assert valid is True
    queue.extra = True
    valid, reason = namespace.ExecutorState.ValidateQueue(queue, schemas)
    assert not valid and reason == "queue has unknown or missing fields"

    execution_state = lua.table_from(
        {
            "index": 1,
            "phase": "mail_wait",
            "inFlight": True,
            "result": lua.table_from({"status": "started"}),
            "blocked": True,
            "latched": True,
            "cancelReason": "blocked",
            "cancelAttempted": True,
            "mailIndex": 3,
            "mailCount": 4,
            "mailProcessed": 2,
            "beforeMailCount": 5,
            "stacks": lua.table_from([lua.table_from({"bag": 0, "slot": 1})]),
            "stackIndex": 1,
            "beforeBagCount": 2,
            "expectedBagCount": 1,
            "expectedItemID": 10,
        }
    )
    advanced = namespace.ExecutorState.Reduce(execution_state, lua.table_from({"type": "advance"}))
    assert advanced.index == 2 and advanced.inFlight is False and advanced.blocked is False
    for field in (
        "phase",
        "result",
        "cancelReason",
        "mailIndex",
        "mailCount",
        "mailProcessed",
        "beforeMailCount",
        "stacks",
        "stackIndex",
        "beforeBagCount",
        "expectedBagCount",
        "expectedItemID",
    ):
        assert advanced[field] is None
    assert advanced.latched is False and advanced.cancelAttempted is False
    with pytest.raises(Exception, match="unknown event"):
        namespace.ExecutorState.Reduce(execution_state, lua.table_from({"type": "unknown"}))

    sweep_for_encoding = lua.table_from(
        {
            "planHash": "plan",
            "planJSON": "{}",
            "observations": lua.table_from({}),
            "owned": lua.table_from({}),
            "house": lua.table_from({"key": "beta-normal-horde", "source": "override"}),
            "context": lua.table_from({"character": "Tester"}),
            "startedAt": 1,
        }
    )
    encoded_payload = namespace.ScanEncoder.Payload(sweep_for_encoding)
    assert encoded_payload.planHash == "plan" and encoded_payload.owned is not None
    encoded_envelope = namespace.ScanEncoder.Envelope(sweep_for_encoding, "scan", 2, 0, 0, "wire")
    assert encoded_envelope.format == 3 and encoded_envelope.payload == "wire"
    serialize = lua.eval("function(_) return 'json' end")
    compress = lua.eval("function(value, method) return value .. tostring(method) end")
    base64 = lua.eval("function(value) return '[' .. value .. ']' end")
    assert namespace.ScanEncoder.Encode(encoded_payload, serialize, compress, base64, 7) == "[json7]"
    ok, decoded = namespace.ScanEncoder.Decode(
        "wire", lua.eval("function(value) return value .. '-decoded' end")
    )
    assert ok is True and decoded == "wire-decoded"
    bad_sweep = lua.table_from({"planHash": "plan"})
    with pytest.raises(Exception, match="payload does not match"):
        namespace.ScanEncoder.Payload(bad_sweep)

    frame = namespace.AgentEncoder.Frame("{}", "payload", 1, 2, 3)
    assert len(frame) == 2048


def test_live_modules_dispatch_their_production_transitions_through_reducers():
    agent = WowClient()
    agent.login()
    agent.lua.globals().testNs = agent.ns
    agent.lua.execute(
        "_agent_measurement_calls = 0; "
        "local measurement = testNs.AgentState.Measurement; "
        "testNs.AgentState.Measurement = function(current, ordinal) "
        "_agent_measurement_calls = _agent_measurement_calls + 1; "
        "return measurement(current, ordinal) end"
    )
    agent.slash("agent hold")
    assert agent.lua.globals()._agent_measurement_calls == 1

    scanner = WowClient()
    scanner.login()
    scanner.set_canonical_plan()
    scanner.lua.globals().testNs = scanner.ns
    scanner.lua.execute(
        "_scanner_reducer_events = {}; local reduce = testNs.ScannerState.Reduce; "
        "testNs.ScannerState.Reduce = function(current, event) "
        "table.insert(_scanner_reducer_events, event.type); return reduce(current, event) end"
    )
    scanner.set_auctions([Auction(itemID=2589, name="Linen Cloth", count=2, buyout=200)])
    scanner.fire("AUCTION_HOUSE_SHOW")
    scanner.slash("scan")
    scanner.run_timers()
    scanner_events = list(scanner.lua.globals()._scanner_reducer_events.values())
    assert scanner_events[:4] == ["item_started", "item_completed", "item_started", "item_completed"]
    assert scanner_events[-3:] == ["owned_started", "owned_completed", "finished"]

    timeout = WowClient(suppress_owned_timeout=True)
    timeout.login()
    timeout.set_canonical_plan()
    timeout.lua.globals().testNs = timeout.ns
    timeout.lua.execute(
        "_scanner_reducer_events = {}; local reduce = testNs.ScannerState.Reduce; "
        "testNs.ScannerState.Reduce = function(current, event) "
        "table.insert(_scanner_reducer_events, event.type); return reduce(current, event) end"
    )
    ids = load_catalog().scan_plan()["itemIDs"]
    timeout.set_auctions([Auction(2589, "Linen Cloth", count=1, buyout=100)])
    timeout.set_metadata_pending(ids)
    timeout.set_owned_behavior(emit=False, delay=601)
    timeout.fire("AUCTION_HOUSE_SHOW")
    timeout.slash("scan")
    advance_to_owned_query(timeout)
    sweep_deadline = int(timeout.lua.globals().CrowleyDB.lastSweepStart) + 600
    assert int(timeout.lua.globals()._now) < sweep_deadline
    timeout.advance_with_timers(sweep_deadline - int(timeout.lua.globals()._now))
    timeout_events = list(timeout.lua.globals()._scanner_reducer_events.values())
    assert timeout_events[-3:] == ["owned_started", "owned_completed", "finished"]
    assert timeout_events.count("item_completed") > 0
    assert decoded_payload(timeout)["owned"]["reasonCode"] == "sweep_timeout"

    def new_executor(queue_id: str) -> WowClient:
        client = WowClient(
            faction="Alliance",
            realm="Classic Beta PvE 2",
            current_region=110,
            queue={
                "format": 1,
                "queueId": queue_id,
                "generatedAt": 1759990000,
                "expiresAt": 1760000100,
                "house": "beta-normal-alliance",
                "character": "Tester",
                "armed": True,
                "actions": [
                    {
                        "seq": 1,
                        "kind": "buy_commodity",
                        "itemID": 2589,
                        "quantity": 1,
                        "maxUnitPrice": 100,
                    }
                ],
            },
        )
        client.login()
        client.fire("AUCTION_HOUSE_SHOW")
        client.slash(f"arm {queue_id}")
        client.lua.globals().testNs = client.ns
        client.lua.execute(
            "_executor_reducer_events = {}; local reduce = testNs.ExecutorState.Reduce; "
            "testNs.ExecutorState.Reduce = function(current, event) "
            "table.insert(_executor_reducer_events, event.type); return reduce(current, event) end"
        )
        return client

    executor = new_executor("beta-normal-alliance-1760000000-success")
    executor.click_executor()
    assert list(executor.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
    ]
    started = executor._json_table(executor.ns.Executor.State())
    assert started["inFlight"] is True
    assert started["phase"] == "await_price"
    assert started["latched"] is False

    executor.fire("COMMODITY_PRICE_UPDATED", 2589, 50, 50)
    assert list(executor.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "phase",
    ]
    priced = executor._json_table(executor.ns.Executor.State())
    assert priced["inFlight"] is True and priced["phase"] == "ready_to_confirm"

    executor.advance(1)
    executor.click_executor()
    assert list(executor.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "phase",
        "click",
        "click_at",
        "phase",
    ]
    confirming = executor._json_table(executor.ns.Executor.State())
    assert confirming["inFlight"] is True and confirming["phase"] == "await_purchase"

    executor.fire("COMMODITY_PURCHASE_SUCCEEDED")
    assert list(executor.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "phase",
        "click",
        "click_at",
        "phase",
        "finished",
        "advance",
    ]
    succeeded = executor._json_table(executor.ns.Executor.State())
    assert succeeded["index"] == 2
    assert succeeded["inFlight"] is False
    assert succeeded["blocked"] is False
    assert succeeded["latched"] is False
    assert "phase" not in succeeded
    assert (
        executor.execution("beta-normal-alliance-1760000000-success")["results"][0]["status"] == "succeeded"
    )

    failed = new_executor("beta-normal-alliance-1760000000-failed")
    failed.click_executor()
    failed.fire("COMMODITY_PRICE_UPDATED", 2589, 50, 50)
    failed.advance(1)
    failed.click_executor()
    failed.fire("COMMODITY_PURCHASE_FAILED", 1, "purchase_failed")
    assert list(failed.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "phase",
        "click",
        "click_at",
        "phase",
        "finished",
    ]
    failure_state = failed._json_table(failed.ns.Executor.State())
    assert failure_state["index"] == 1
    assert failure_state["inFlight"] is False
    assert failure_state["blocked"] is True
    assert failure_state["latched"] is False
    assert "phase" not in failure_state
    assert failed.execution("beta-normal-alliance-1760000000-failed")["results"][0]["status"] == "failed"

    blocked = new_executor("beta-normal-alliance-1760000000-blocked")
    blocked.click_executor()
    blocked.fire("COMMODITY_PRICE_UPDATED", 2589, 200, 200)
    assert list(blocked.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "blocked",
    ]
    blocking_state = blocked._json_table(blocked.ns.Executor.State())
    assert blocking_state["inFlight"] is True
    assert blocking_state["latched"] is True
    assert blocking_state["blocked"] is False
    assert blocking_state["phase"] == "must_cancel"

    blocked.advance(1)
    blocked.click_executor()
    assert list(blocked.lua.globals()._executor_reducer_events.values()) == [
        "click",
        "click_at",
        "started",
        "phase",
        "result_detail",
        "blocked",
        "click",
        "click_at",
        "cancel_attempted",
        "finished",
    ]
    blocked_state = blocked._json_table(blocked.ns.Executor.State())
    assert blocked_state["inFlight"] is False
    assert blocked_state["latched"] is False
    assert blocked_state["blocked"] is True
    assert blocked_state["cancelAttempted"] is True
    assert blocked_state["result"]["status"] == "refused"
    assert "phase" not in blocked_state
    blocked_results = blocked.execution("beta-normal-alliance-1760000000-blocked")["results"]
    assert [result["status"] for result in blocked_results] == ["refused", "blocked"]

    reconciled = WowClient(
        faction="Alliance",
        realm="Classic Beta PvE 2",
        current_region=110,
        queue={
            "format": 1,
            "queueId": "beta-normal-alliance-1760000000-reconcile",
            "generatedAt": 1759990000,
            "expiresAt": 1760000100,
            "house": "beta-normal-alliance",
            "character": "Tester",
            "armed": False,
            "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 1}],
        },
    )
    reconciled.lua.globals().testNs = reconciled.ns
    reconciled.lua.execute(
        "_executor_reducer_events = {}; local reduce = testNs.ExecutorState.Reduce; "
        "testNs.ExecutorState.Reduce = function(current, event) "
        "table.insert(_executor_reducer_events, event.type); return reduce(current, event) end"
    )
    reconciled.login()
    assert list(reconciled.lua.globals()._executor_reducer_events.values()) == ["reconcile"]


def finish_canonical(client: WowClient) -> None:
    """Drain the canonical sweep after an adversarial event was observed."""

    client.state.fastCanonical = True
    client.run_timers()


def advance_to_owned_query(client: WowClient) -> None:
    """Reach the real owned-auction request without skipping its event window."""

    client.state.fastCanonical = False
    for _ in range(1000):
        client.run_timers()
        if client.state.ownedQueries:
            return
        deadline = client.lua.globals()._nextTimerAt()
        if deadline is None:
            break
        client.lua.globals()._now = deadline
    raise AssertionError("canonical sweep did not reach owned query")


@pytest.mark.parametrize("is_commodity", [False, True], ids=["item", "commodity"])
@pytest.mark.parametrize("depth_case", ["below", "exact", "above"])
def test_complete_source_depth_boundary_is_unbounded_without_higher_witness(is_commodity, depth_case):
    if depth_case == "below":
        prices = list(range(100, 119))
        expected_coverage = "full"
        expected_rows = len(prices)
        expected_ids = set(range(1000, 1000 + len(prices)))
    elif depth_case == "exact":
        # Two rows share the twentieth level, so the boundary itself is covered.
        prices = list(range(100, 119)) + [119, 119]
        expected_coverage = "full"
        expected_rows = len(prices)
        expected_ids = set(range(1000, 1000 + len(prices)))
    else:
        prices = list(range(100, 121))
        expected_coverage = "bounded"
        expected_rows = 20
        expected_ids = set(range(1000, 1020))

    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(
                2589,
                "Linen",
                1,
                price,
                isCommodity=is_commodity,
                auctionID=1000 + index,
            )
            for index, price in enumerate(prices)
        ]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["kind"] == ("commodity" if is_commodity else "item")
    assert observation["coverage"] == expected_coverage
    assert observation["sourceComplete"] is True
    assert observation["reasonCode"] == "ok"
    assert observation["rowCount"] == expected_rows == len(observation["rows"])
    assert {row["resultID"] for row in observation["rows"].values()} == expected_ids
    if depth_case in {"below", "exact"}:
        assert observation["cutoff"] == {"kind": "unbounded"}
        assert observation["higherPriceWitness"] == {"kind": "unknown"}
    else:
        assert observation["cutoff"] == {"kind": "price", "numerator": 119, "denominator": 1}
        assert observation["higherPriceWitness"] == {"kind": "known", "numerator": 120, "denominator": 1}


@pytest.mark.parametrize("warm", [False, True], ids=["cold-refresh", "warm-cache"])
def test_non_equipment_recipe_refreshes_native_key_and_uses_normal_search(warm):
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2555, "Linen", 27, 100, itemLevel=15, classID=9, auctionID=2555)])
    if warm:
        client.set_key_warm([2555])
        assert client.lua.globals().C_AuctionHouse.MakeItemKey(2555, 0, 0, 0).itemLevel == 15
    else:
        client.set_metadata_pending([2555])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["2555"]
    query = next(
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send" and event.get("operation") == "search" and event["itemID"] == 2555
    )
    assert observation["coverage"] == "full"
    assert observation["rowCount"] == 1
    assert query["classID"] == 9
    assert query["equipment"] is False
    assert query["nativeMethod"] == "SendSearchQuery"
    assert query["nativeKey"] == {"itemID": 2555, "itemLevel": 15, "itemSuffix": 0, "battlePetSpeciesID": 0}
    recipe_methods = [entry for entry in client.query_methods if entry["itemID"] == 2555]
    assert recipe_methods[0]["method"] == "SendSearchQuery"
    assert recipe_methods[0]["itemLevel"] == 15


def test_container_uses_native_generated_key_and_normal_search():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(118, "Bag", 1, 250, itemLevel=12, classID=1, auctionID=4000)])
    client.set_key_warm([118])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["118"]
    query = next(
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send" and event.get("operation") == "search" and event["itemID"] == 118
    )
    assert observation["coverage"] == "full"
    assert query["classID"] == 1
    assert query["equipment"] is False
    assert query["nativeMethod"] == "SendSearchQuery"
    assert query["nativeKey"]["itemLevel"] == 12


def test_equipment_uses_zeroed_sell_key_and_preserves_variant_result_key():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(873, "Sword", 1, 500, itemLevel=40, itemSuffix=7, classID=2, auctionID=873)])
    client.set_key_warm([873])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["873"]
    query = next(
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send" and event.get("operation") == "search" and event["itemID"] == 873
    )
    row = next(iter(observation["rows"].values()))
    assert observation["coverage"] == "full"
    assert observation["rowCount"] == 1
    assert query["classID"] == 2
    assert query["equipment"] is True
    assert query["nativeMethod"] == "SendSellSearchQuery"
    assert query["nativeKey"] == {"itemID": 873, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
    assert row["rawItemKey"]["itemLevel"] == 40
    assert row["rawItemKey"]["itemSuffix"] == 7
    assert row["itemKey"] == "873:s7:l40"
    equipment_methods = [entry for entry in client.query_methods if entry["itemID"] == 873]
    assert equipment_methods[0]["itemLevel"] == 0


def test_missing_item_class_is_incomplete_and_does_not_stall_remaining_items():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(873, "Sword", 1, 500, itemLevel=40, classID=None, auctionID=873),
            Auction(2555, "Linen", 27, 100, itemLevel=15, classID=9, auctionID=2555),
        ]
    )
    client.set_key_warm([2555])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observations = decoded_payload(client)["observations"]
    assert observations["873"]["coverage"] == "incomplete"
    assert observations["873"]["reasonCode"] == "metadata_unavailable"
    assert observations["2555"]["coverage"] == "full"
    assert observations["2555"]["rowCount"] == 1
    sends = [
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send" and event.get("operation") == "search"
    ]
    focused = [event for event in sends if event["itemID"] in {873, 2555}]
    assert [(event["itemID"], event["nativeMethod"]) for event in focused] == [(2555, "SendSearchQuery")]


def test_harness_nonzero_item_sell_search_has_throttle_without_result():
    client = WowClient()
    client.set_auctions([Auction(2555, "Linen", 27, 100, itemLevel=15, classID=9)])
    key = client.lua.globals().C_AuctionHouse.MakeItemKey(2555, 15, 0, 0)
    sorts = client._to_lua([{"sortOrder": 2, "reverseSort": False}])
    client.lua.globals().C_AuctionHouse.SendSellSearchQuery(key, sorts, False)
    client.run_timers()
    assert client.state.lastQuery.valid is False
    assert client.state.queryCalls == 1
    assert client.send_log == [{"at": 1760000000, "kind": "search", "itemID": 2555}]


def test_strict_validator_rejects_malformed_full_price_from_payload_roundtrip():
    client = WowClient()
    raw = run_sweep(client)
    payload = json.loads(zlib.decompress(base64.b64decode(raw["payload"]), -15))
    observation = payload["observations"]["2589"]
    observation["cutoff"] = {"kind": "price", "numerator": 200, "denominator": 1}
    raw["payload"] = base64.b64encode(
        zlib.compress(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), wbits=-15)
    ).decode()

    with pytest.raises(ScanFormatError, match="full"):
        validate_v2_envelope(envelope(raw))


def test_harness_serializes_empty_lua_tables_as_arrays():
    client = WowClient()
    value = client._to_lua({"empty": {}, "owners": {"Seller": True}})
    wire = json.loads(client.lua.globals().C_EncodingUtil.SerializeJSON(value))
    assert wire == {"empty": [], "owners": {"Seller": True}}


def test_addon_lifecycle_registry_runs_registered_modules_in_dependency_order_after_restore():
    client = WowClient()
    client.lua.globals().testNs = client.ns
    client.lua.execute(
        "_registry_order = {}; _seen_saved = nil; "
        "testNs.RegisterModule('OrderB', {requires = {}, onLoad = function() "
        "table.insert(_registry_order, 'B') end}); "
        "testNs.RegisterModule('OrderA', {requires = {'OrderB'}, onLoad = function() "
        "table.insert(_registry_order, 'A'); _seen_saved = CrowleyDB.lifecycleMarker end})"
    )
    client.inject_saved_variables({"lifecycleMarker": "restored"})
    client.login()

    assert list(client.lua.globals()._registry_order.values()) == ["B", "A"]
    assert client.lua.globals()._seen_saved == "restored"


def test_addon_lifecycle_registry_rejects_unknown_dependency_and_cycle():
    unknown = WowClient()
    unknown.lua.globals().testNs = unknown.ns
    unknown.lua.execute("testNs.RegisterModule('Broken', {requires = {'Missing'}, onLoad = function() end})")
    with pytest.raises(Exception, match="unknown module dependency.*Broken.*Missing"):
        unknown.login()

    cycle = WowClient()
    cycle.lua.globals().testNs = cycle.ns
    cycle.lua.execute(
        "testNs.RegisterModule('CycleA', {requires = {'CycleB'}, onLoad = function() end}); "
        "testNs.RegisterModule('CycleB', {requires = {'CycleA'}, onLoad = function() end})"
    )
    with pytest.raises(Exception, match="module dependency cycle.*CycleA.*CycleB.*CycleA"):
        cycle.login()


def test_addon_lifecycle_registry_rejects_duplicate_registration():
    client = WowClient()
    client.lua.globals().testNs = client.ns
    with pytest.raises(Exception, match="duplicate module registration: Core"):
        client.lua.execute("testNs.RegisterModule('Core', {requires = {}, onLoad = function() end})")


@pytest.mark.parametrize("legacy", ["saved", "saved_acks", "data", "data_empty"])
def test_addon_rejects_legacy_sweep_ack_storage(legacy):
    saved = None
    if legacy == "saved":
        saved = {"sweeps": {"old-sweep": {"id": "old-sweep"}}}
    elif legacy == "saved_acks":
        saved = {"ackedSweeps": ["old-sweep"]}
    client = WowClient(initial_savedvars=saved)
    if legacy in {"data", "data_empty"}:
        values = ["old-sweep"] if legacy == "data" else []
        client.lua.globals().CROWLEY_DATA.ackedSweeps = client._to_lua(values)
    with pytest.raises(Exception, match="legacy"):
        client.login()


@pytest.mark.parametrize(
    "expression",
    [
        "{[1] = 'b', [2] = 'a'}",
        r"{[1] = '\195\169', [2] = 'a'}",
        "{[1] = 'a', [2] = 'a'}",
        "{[1] = ''}",
        "{[1] = 'a', [2] = 1}",
        "{[1] = 'a', [3] = 'c'}",
        "{name = 'a'}",
    ],
)
def test_addon_rejects_malformed_acked_scan_arrays(expression):
    client = WowClient()
    client.lua.execute(f"CROWLEY_DATA.ackedScans = {expression}")
    with pytest.raises(Exception, match="ackedScans"):
        client.login()


def test_addon_accepts_byte_sorted_non_ascii_acked_scan_array():
    client = WowClient()
    client.lua.execute(r"CROWLEY_DATA.ackedScans = {'a', '\195\169'}")
    client.login()


def test_addon_rejects_removed_shopping_lists_data_key():
    client = WowClient()
    client.lua.globals().CROWLEY_DATA.shoppingLists = client._to_lua([])
    with pytest.raises(Exception, match="unknown or missing keys"):
        client.login()


def test_executor_registers_only_pinned_forever_events():
    queue = {
        "format": 1,
        "queueId": "beta-normal-alliance-1760000000-event-contract",
        "generatedAt": 1759990000,
        "expiresAt": 1760000100,
        "house": "beta-normal-alliance",
        "character": "Tester",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 1}],
    }
    client = WowClient(queue=queue)
    client.login()
    expected = {
        "AUCTION_HOUSE_SHOW",
        "AUCTION_HOUSE_CLOSED",
        "MERCHANT_SHOW",
        "MERCHANT_CLOSED",
        "MAIL_SHOW",
        "MAIL_CLOSED",
        "TRADE_SKILL_SHOW",
        "TRADE_SKILL_CLOSE",
        "COMMODITY_PRICE_UPDATED",
        "COMMODITY_PURCHASE_SUCCEEDED",
        "COMMODITY_PURCHASE_FAILED",
        "AUCTION_HOUSE_AUCTION_CREATED",
        "AUCTION_CANCELED",
        "UI_ERROR_MESSAGE",
        "BAG_UPDATE",
        "BAG_UPDATE_DELAYED",
        "MAIL_INBOX_UPDATE",
        "MERCHANT_UPDATE",
    }
    actual = {str(event) for event in client.ns.Executor.RegisteredEvents().values()}
    assert actual == expected
    registered = {str(event) for event, _ in client.ns.handlers.items()}
    assert expected <= registered


def test_agent_first_publication_sees_initialized_scanner_and_executor():
    queue = {
        "format": 1,
        "queueId": "beta-normal-alliance-1760000000-first-publication",
        "generatedAt": 1759990000,
        "expiresAt": 1760000100,
        "house": "beta-normal-alliance",
        "character": "Tester",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 1}],
    }
    client = WowClient(faction="Alliance", realm="Classic Beta PvE 2", current_region=110, queue=queue)
    client.set_canonical_plan()
    client.login()

    first = decode_frame(client.agent_frame).snapshot.value
    assert first["scanner"]["phase"] == "ready"
    assert first["executor"]["availability"] == "valid"
    assert first["executor"]["queueId"] == queue["queueId"]


def test_addon_decides_saved_marker_after_file_execution_and_unknown_blocks_scanner():
    client = WowClient()
    assert client.ns.Agent.Surface() is None
    fixture = savedvars.load_file(FIXTURES / "native-construction-unknown-savedvars.lua")
    client.inject_saved_variables(fixture["CrowleyDB"])
    client.login()

    decision = client._json_table(client.ns.NativeDecision())
    assert decision["kind"] == "restore_only"
    assert decision["reason"] == "malformed"
    assert client.ns.Agent.Lifecycle() == "restore_only"
    assert client.ns.Agent.Surface() is None
    assert client.lua.globals().CrowleyDB.nativeConstruction.extra == "must-not-leak"
    client.set_canonical_plan()
    assert client.ns.Scanner.Status()["phase"] == "ready"
    assert client.ns.Scanner.Status()["reason"] == ""

    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert "native construction fence active" in client.chat[-1]


def test_native_scalar_marker_is_restore_only_without_raw_value_indexing():
    client = WowClient(initial_savedvars={"nativeConstruction": True})
    assert client.ns.Agent.Surface() is None
    client.login()

    decision = client._json_table(client.ns.NativeDecision())
    assert decision["kind"] == "restore_only"
    assert decision["reason"] == "malformed"
    assert client.ns.Agent.Lifecycle() == "restore_only"
    assert client.ns.Agent.Surface() is None
    client.set_canonical_plan()
    assert client.ns.Scanner.Status()["phase"] == "ready"
    assert client.ns.Scanner.Status()["reason"] == ""
    assert client.lua.globals().CrowleyNativeConstructionRestore.IsShown(
        client.lua.globals().CrowleyNativeConstructionRestore
    )

    before = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    client.click_native("restore")
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before
    assert client.ns.Agent.RecoveryAdmitted() is False

    # Extra arguments are rejected before the command actor can admit recovery.
    client.slash("agent recover extra")
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before
    client.slash("agent recover")
    assert "agent recover accepted" in client.chat[-1]
    assert client.ns.Agent.RecoveryMode() is True
    client.click_native("restore")
    marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "restore_pending"
    assert client.reload_calls == 1


@pytest.mark.parametrize(
    "raw_marker",
    [
        False,
        17,
        "native",
        [],
        [1],
        {"schema": 1, "state": "deferred", "attempts": 0},
        {"schema": "1", "state": "deferred", "token": "boot", "attempts": 0},
        {"schema": 1, "state": "unknown", "token": "boot", "attempts": 0},
        {"schema": 1, "state": 17, "token": "boot", "attempts": 0},
        {"schema": 1, "state": {"bad": True}, "token": "boot", "attempts": 0},
        {"schema": 1, "state": "deferred", "token": 17, "attempts": 0},
        {"schema": 1, "state": "deferred", "token": {"bad": True}, "attempts": 0},
        {"schema": 1, "state": "deferred", "token": "", "attempts": 0},
        {"schema": 1, "state": "deferred", "token": "bad token", "attempts": 0},
        {"schema": 1, "state": "deferred", "token": "bad\nvalue", "attempts": 0},
        {"schema": 1, "state": "deferred", "token": "x" * 65, "attempts": 0},
        {"schema": 1, "state": "armed", "token": "boot", "attempts": 2},
        {"schema": 1, "state": "deferred", "token": "boot", "attempts": 1},
        {"schema": 1, "state": "deferred", "token": "boot", "attempts": 2},
        {"schema": 1, "state": "constructing", "token": "boot", "attempts": 0},
        {"schema": 1, "state": "constructing", "token": "boot", "attempts": 2},
        {"schema": 1, "state": "armed", "token": "boot", "attempts": 1},
        {"schema": 1, "state": "constructed", "token": "boot", "attempts": 0},
        {"schema": 1, "state": "constructed", "token": "boot", "attempts": 2},
        {"schema": 1, "state": "failed", "token": "boot", "attempts": 0},
        {"schema": 1, "state": "failed", "token": "boot", "attempts": 2},
        {"schema": 1, "state": "restore_pending", "token": "recovery:", "attempts": 0},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "a" * 23, "attempts": 0},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "a" * 25, "attempts": 0},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "g" * 24, "attempts": 0},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "a" * 24, "attempts": 1},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "a" * 24, "attempts": 2},
        {"schema": 1, "state": "deferred", "token": "boot", "attempts": "0"},
        {"schema": 1, "state": "deferred", "token": "boot", "attempts": 0, "extra": True},
        {"schema": 1, "state": "restore_pending", "token": "recovery:" + "A" * 24, "attempts": 0},
    ],
    ids=[
        "false",
        "number",
        "string",
        "empty-array",
        "array",
        "missing-token",
        "wrong-schema-type",
        "unknown-state",
        "numeric-state",
        "table-state",
        "numeric-token",
        "table-token",
        "empty-token",
        "whitespace-token",
        "control-token",
        "long-token",
        "armed-attempt-two",
        "deferred-attempt-one",
        "deferred-attempt-two",
        "constructing-attempt-zero",
        "constructing-attempt-two",
        "armed-attempt",
        "constructed-attempt",
        "constructed-attempt-two",
        "failed-attempt-zero",
        "failed-attempt-two",
        "recovery-short",
        "recovery-23-hex",
        "recovery-long",
        "recovery-nonhex",
        "recovery-attempt-one",
        "recovery-attempt-two",
        "attempt-type",
        "extra-field",
        "recovery-uppercase",
    ],
)
def test_native_malformed_marker_matrix_preserves_raw_value_and_stays_surface_free(raw_marker):
    client = WowClient(initial_savedvars={"nativeConstruction": raw_marker})
    client.login()
    assert client._json_table(client.ns.NativeDecision())["kind"] == "restore_only"
    assert client.ns.Agent.Lifecycle() == "restore_only"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == raw_marker
    client.slash("agent recover")
    assert "agent recover accepted" in client.chat[-1]
    client.click_native("restore")
    canonical = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert canonical["state"] == "restore_pending"
    assert canonical["attempts"] == 0
    assert canonical["token"].startswith("recovery:") and len(canonical["token"]) == 33
    assert client.reload_calls == 1


def test_native_unknown_preload_entrypoints_refuse_without_side_effects():
    client = WowClient()
    before_native = client._json_table(client.ns.Agent.NativeState())
    for method in (client.ns.Agent.Publish, client.ns.Agent.Snapshot):
        with pytest.raises(Exception, match="agent_unconstructed"):
            method()
    assert client._json_table(client.ns.Agent.NativeState()) == before_native
    assert client.ns.Agent.FrameBytes() is None
    assert client.lua.globals().CrowleyDB is None

    # Manual, event, timer, and fence-release entry points all remain inert
    # until ADDON_LOADED has established the native decision and SavedVariables.
    client.fire("AUCTION_HOUSE_CLOSED")
    client.fire("PLAYER_LOGOUT")
    client.ns.Scanner.Schedule("preload")
    client.ns.Scanner.SetNativeConstructionFence(False)
    client.advance_with_timers(1)
    assert client.ns.Scanner.InProgress() is False
    assert client.ns.Scanner.PreLogoutFence()[1] == "native_construction_fence"
    assert client.ns.Agent.Construct() == (False, "construct_not_deferred")
    assert client.ns.Agent.Hold() == (False, "agent_unconstructed")
    assert client.ns.Agent.Measure() == (False, "agent_unconstructed")
    assert client.ns.Agent.Resume() == (False, "agent_unconstructed")
    assert client.ns.Agent.Restore() == (False, "recovery_not_admitted")
    assert client.ns.Agent.BeginRecovery() == (False, "recovery_not_required")
    client.slash("agent recover")
    assert client.lua.globals().CrowleyDB is None


@pytest.mark.parametrize(
    ("state", "attempts", "token", "expected_lifecycle"),
    [
        ("armed", 0, "x" * 64, "deferred"),
        ("deferred", 0, "x" * 64, "deferred"),
        ("constructing", 1, "x" * 64, "constructing"),
        ("constructed", 1, "x" * 64, "restore_only"),
        ("failed", 1, "x" * 64, "failed"),
        ("armed", 0, "boot", "deferred"),
        ("deferred", 0, "boot", "deferred"),
        ("constructing", 1, "boot", "constructing"),
        ("constructed", 1, "boot", "restore_only"),
        ("failed", 1, "boot", "failed"),
        ("restore_pending", 0, "recovery:" + "a" * 24, "ordinary"),
    ],
)
def test_native_valid_marker_matrix_has_closed_lifecycle_projection(
    state, attempts, token, expected_lifecycle
):
    client = WowClient(
        initial_savedvars={
            "nativeConstruction": {"schema": 1, "state": state, "token": token, "attempts": attempts}
        }
    )
    client.login()
    expected_state = "deferred" if state == "armed" else state
    assert client._json_table(client.ns.NativeDecision())["kind"] == expected_state
    assert client.ns.Agent.Lifecycle() == expected_lifecycle
    if state == "restore_pending":
        assert client.lua.globals().CrowleyDB.nativeConstruction is None
        assert client.ns.Agent.Surface() is not None
    else:
        assert client.ns.Agent.Surface() is None


def test_native_construction_marker_matrix_defers_then_constructs_once():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    armed = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert armed["state"] == "armed" and armed["attempts"] == 0
    assert client.lua.globals().CrowleyNativeConstructionPanel.IsShown(
        client.lua.globals().CrowleyNativeConstructionPanel
    )

    # The pre-reload callback is real and inert: it cannot construct early.
    client.click_native("construct")
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "armed"
    assert client.ns.Agent.Surface() is not None

    saved = savedvars.loads(client.saved_variables())["CrowleyDB"]
    deferred = WowClient(initial_savedvars=saved)
    assert deferred.ns.Agent.Surface() is None
    deferred.login()
    assert deferred._json_table(deferred.ns.NativeDecision())["kind"] == "deferred"
    assert deferred.ns.Agent.Surface() is None
    deferred.slash("agent release")
    deferred.click_native("construct")
    assert deferred.ns.Agent.Lifecycle() == "constructed"
    assert deferred.ns.Agent.Surface() is not None
    assert deferred.lua.globals().CrowleyDB.nativeConstruction.state == "constructed"
    before_update = deferred._json_table(deferred.ns.Agent.Snapshot())["sequence"]
    deferred.update_agent(1)
    after_update = deferred._json_table(deferred.ns.Agent.Snapshot())["sequence"]
    assert after_update > before_update
    textures = deferred.texture_count
    deferred.click_native("construct")
    assert deferred.texture_count == textures
    resumed = WowClient(initial_savedvars=savedvars.loads(deferred.saved_variables())["CrowleyDB"])
    resumed.login()
    assert resumed.ns.Agent.Surface() is None
    assert resumed.ns.Agent.Lifecycle() == "restore_only"


def test_native_frames_decode_across_private_fence_and_measurement_lifecycle():
    source = WowClient()
    source.login()
    source.set_canonical_plan()
    source.slash("agent defer")
    deferred_frame = decode_frame(source.publish_agent() and source.agent_frame).snapshot.value
    assert deferred_frame["scanner"]["phase"] == "ready"
    assert deferred_frame["scanner"]["scheduleFence"] == {
        "state": "armed",
        "reason": "",
        "command": "/crowley session fence",
    }
    assert deferred_frame["pending"] == {"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": ""}

    client = WowClient(initial_savedvars=savedvars.loads(source.saved_variables())["CrowleyDB"])
    client.login()
    client.set_canonical_plan()
    client.slash("agent release")
    client.click_native("construct")
    constructed_frame = decode_frame(client.agent_frame).snapshot.value
    native_boot = constructed_frame["boot"]
    assert native_boot.startswith("native-construction:")
    assert constructed_frame["scanner"]["scheduleFence"]["state"] == "armed"
    assert constructed_frame["render"]["nativePhase"] == "normal"
    assert constructed_frame["render"]["nativeOrdinal"] == 0

    client.slash("agent hold")
    assert decode_frame(client.agent_frame).snapshot.value["render"]["nativePhase"] == "held"
    client.slash("agent measure")
    client.advance_with_timers(100)
    assert decode_frame(client.agent_frame).snapshot.value["render"]["nativeOrdinal"] == 100
    client.slash("agent resume")
    resumed_frame = decode_frame(client.agent_frame).snapshot.value
    assert resumed_frame["render"]["nativePhase"] == "normal"
    assert resumed_frame["render"]["nativeOrdinal"] == 0
    client.slash("agent finalize")
    finalized_frame = decode_frame(client.agent_frame).snapshot.value
    assert finalized_frame["boot"] == native_boot
    assert finalized_frame["scanner"]["scheduleFence"] == {
        "state": "inactive",
        "reason": "",
        "command": "/crowley session fence",
    }

    client.slash("agent recover")
    client.click_native("restore")
    restored = WowClient(initial_savedvars=savedvars.loads(client.saved_variables())["CrowleyDB"])
    restored.set_canonical_plan()
    restored.login()
    restored_frame = decode_frame(restored.agent_frame).snapshot.value
    assert restored.lua.globals().CrowleyDB.nativeConstruction is None
    assert restored.ns.Agent.Lifecycle() == "ordinary"
    assert restored_frame["boot"] != native_boot
    assert restored_frame["scanner"]["scheduleFence"]["state"] == "inactive"


def test_native_construct_publication_failure_is_transactional_and_recoverable():
    client = deferred_native_client()
    client.slash("agent release")
    client.lua.globals().C_EncodingUtil.SerializeJSON = client.lua.eval(
        "function() error('publication fixture failure') end"
    )
    client.click_native("construct")
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.ns.Agent.Lifecycle() == "failed"
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "failed"
    assert all(shown is False for shown in client.visible_native_textures)
    assert client.lua.globals().CrowleyNativeConstructionRestore.IsShown(
        client.lua.globals().CrowleyNativeConstructionRestore
    )
    assert "construct refused: publication_error" in client.chat[-1]
    assert "agent construct accepted" not in client.chat


def test_native_partial_texture_failure_hides_every_owned_texture():
    client = deferred_native_client()
    client.slash("agent release")
    client.set_texture_failure(17)
    client.click_native("construct")
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.ns.Agent.Lifecycle() == "failed"
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "failed"
    assert client.visible_native_textures
    assert all(shown is False for shown in client.visible_native_textures)


def test_native_restore_invalid_facing_at_addon_loaded_waits_for_world_entry():
    client = WowClient(initial_savedvars=pending_restore_savedvars(), facing=None)
    client.fire("ADDON_LOADED", "Crowley")
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "restore_pending"
    assert client.ns.Scanner.NativeConstructionFence() is True
    restore_button = client.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is False
    assert queued_timer_count(client) == 1

    client.set_motion(facing=1.25)
    client.set_canonical_plan()
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.lua.globals().CrowleyDB.agentRestoreFailure is None
    assert client.ns.Scanner.NativeConstructionFence() is False
    frame = client.agent_frame
    textures = client.texture_count
    client.advance_with_timers(30)
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.agent_frame == frame
    assert client.texture_count == textures
    assert queued_timer_count(client) == 0


def test_native_restore_duplicate_world_events_do_not_commit_twice():
    client = WowClient(initial_savedvars=pending_restore_savedvars(), facing=None)
    client.login()
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "restore_pending"
    assert client.ns.Scanner.NativeConstructionFence() is True
    restore_button = client.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is False

    client.set_motion(facing=1.25)
    client.set_canonical_plan()
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.lua.globals().CrowleyDB.agentRestoreFailure is None
    assert client.ns.Scanner.NativeConstructionFence() is False
    decoded = decode_frame(client.agent_frame).snapshot.value
    assert decoded["render"]["nativePhase"] == "normal"
    assert decoded["render"]["nativeOrdinal"] == 0
    frame = client.agent_frame
    textures = client.texture_count
    client.fire("PLAYER_ENTERING_WORLD")
    client.advance_with_timers(30)
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.agent_frame == frame
    assert client.texture_count == textures
    assert queued_timer_count(client) == 0


def test_native_restore_transient_facing_failure_retries_once_and_drains_stale_timeout():
    client = WowClient(initial_savedvars=pending_restore_savedvars(), facing=None)
    client.fire("ADDON_LOADED", "Crowley")
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.texture_count == 0
    assert queued_timer_count(client) == 2

    client.set_canonical_plan()
    # Keep the existing cooldown active so the release timer remains visible
    # while this test proves that no query starts during readiness polling.
    client.lua.globals().CrowleyDB.lastSweepStart = int(client.lua.globals().time())
    client.fire("AUCTION_HOUSE_SHOW")
    events = schedule_trace(client)["events"]
    assert any(
        event["type"] == "refusal"
        and event["reason"] == "native_construction_fence"
        and event["source"] == "ah_show"
        for event in events
    )
    assert client.state.queryCalls == 0

    client.set_motion(facing=1.25)
    client.advance_with_timers(0.1)
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.ns.Agent.Surface() is not None
    assert queued_timer_count(client) == 2
    events = schedule_trace(client)["events"]
    assert events[-1]["type"] == "timer_create"
    assert events[-1]["source"] == "native_construction_release"
    frame = client.agent_frame
    textures = client.texture_count
    client.fire("PLAYER_ENTERING_WORLD")
    client.advance_with_timers(30)
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.agent_frame == frame
    assert client.texture_count == textures
    assert client.state.queryCalls == 0
    client.fire("AUCTION_HOUSE_CLOSED")
    client.advance_with_timers(900)
    assert queued_timer_count(client) == 0


def test_native_restore_missing_world_entry_times_out_with_recovery_route():
    client = WowClient(initial_savedvars=pending_restore_savedvars())
    client.fire("ADDON_LOADED", "Crowley")
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert queued_timer_count(client) == 1
    client.advance_with_timers(30)
    assert client.ns.Agent.Lifecycle() == "failed"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.ns.Scanner.NativeConstructionFence() is True
    assert (
        client.lua.globals().CrowleyDB.agentRestoreFailure == "world readiness: missing PLAYER_ENTERING_WORLD"
    )
    marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "failed" and marker["attempts"] == 1
    restore_button = client.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is True
    assert queued_timer_count(client) == 0
    client.slash("agent recover")
    client.click_native("restore")
    recovered = WowClient(initial_savedvars=savedvars.loads(client.saved_variables())["CrowleyDB"])
    recovered.login()
    assert recovered.ns.Agent.Lifecycle() == "ordinary"
    assert recovered.lua.globals().CrowleyDB.nativeConstruction is None


def test_native_restore_permanent_facing_failure_times_out_with_exact_reason():
    client = WowClient(initial_savedvars=pending_restore_savedvars(), facing=None)
    client.fire("ADDON_LOADED", "Crowley")
    client.fire("PLAYER_ENTERING_WORLD")
    client.advance_with_timers(30)
    client.advance_with_timers(1)
    assert client.ns.Agent.Lifecycle() == "failed"
    assert client.ns.Agent.Surface() is None
    assert client.ns.Scanner.NativeConstructionFence() is True
    failure = str(client.lua.globals().CrowleyDB.agentRestoreFailure)
    assert failure.startswith("world readiness: ")
    assert failure.endswith("invalid facing (nil:nil)")
    assert queued_timer_count(client) == 0


def test_native_restore_deadline_refuses_before_native_probe():
    client = WowClient(initial_savedvars=pending_restore_savedvars())
    client.fire("ADDON_LOADED", "Crowley")
    calls = {"count": 0}

    def facing_probe():
        calls["count"] += 1
        return 1.25

    client.lua.globals().GetPlayerFacing = facing_probe
    client.lua.globals()._now = 1760000030
    client.fire("PLAYER_ENTERING_WORLD")
    assert calls["count"] == 0
    assert client.ns.Agent.Lifecycle() == "failed"
    assert "deadline exceeded before native probe" in str(client.lua.globals().CrowleyDB.agentRestoreFailure)
    client.run_timers()
    assert calls["count"] == 0
    assert queued_timer_count(client) == 0


def test_native_restore_accepts_unknown_ctm_and_ground_states_when_movement_is_valid():
    client = WowClient(
        initial_savedvars=pending_restore_savedvars(),
        ctm_mode="nil",
        ground_modes={
            "flying": "nil",
            "falling": "nil",
            "swimming": "nil",
            "submerged": "nil",
            "mounted": "nil",
            "taxi": "nil",
        },
    )
    client.fire("ADDON_LOADED", "Crowley")
    client.set_canonical_plan()
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.ns.Agent.Lifecycle() == "ordinary"
    frame = decode_frame(client.agent_frame).snapshot.value
    assert frame["motion"]["native"]["ctm"]["state"] == "unknown"
    assert frame["motion"]["native"]["ground"]["state"] == "unknown"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.ns.Scanner.NativeConstructionFence() is False


def test_native_restore_stale_nonce_callbacks_are_inert():
    client = WowClient(initial_savedvars=pending_restore_savedvars(), facing=None)
    client.fire("ADDON_LOADED", "Crowley")
    client.fire("PLAYER_ENTERING_WORLD")
    original = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    newer = dict(original)
    # This token replacement is an artificial boundary; real Restore and
    # BeginRecovery refuse while the readiness transaction is pending.
    newer["token"] = "recovery:" + "b" * 24
    client.lua.globals().CrowleyDB.nativeConstruction = client._to_lua(newer)
    client.set_motion(facing=1.25)
    client.advance_with_timers(0.1)
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert client.texture_count == 0
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == newer
    client.advance_with_timers(30)
    assert client.ns.Agent.Lifecycle() == "restore_pending"
    assert client.ns.Agent.Surface() is None
    assert queued_timer_count(client) == 0


def test_native_restore_load_publication_failure_keeps_failed_surfaceless():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]
    restored = WowClient(initial_savedvars=saved)
    restored.lua.globals().C_EncodingUtil.SerializeJSON = restored.lua.eval(
        "function() error('restore publication fixture failure') end"
    )
    restored.login()
    assert restored.ns.Agent.Surface() is None
    assert restored.ns.Agent.FrameBytes() is None
    assert restored.ns.Agent.Lifecycle() == "failed"
    marker = restored.lua.globals().CrowleyDB.nativeConstruction
    assert marker.state == "failed" and marker.attempts == 1
    assert restored.ns.Scanner.NativeConstructionFence() is True
    assert all(shown is False for shown in restored.visible_native_textures)
    failure = str(restored.lua.globals().CrowleyDB.agentRestoreFailure)
    assert failure.startswith("first publication: ")
    assert failure.endswith("restore publication fixture failure")
    assert any("agent restore failed: " + failure in message for message in restored.chat)
    assert restored.lua.globals().CrowleyNativeConstructionRestore.IsShown(
        restored.lua.globals().CrowleyNativeConstructionRestore
    )
    assert restored.lua.globals().CrowleyNativeConstructionRestore.IsEnabled(
        restored.lua.globals().CrowleyNativeConstructionRestore
    )


def test_native_restore_second_unarmed_publication_failure_preserves_recovery_route():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]

    restored = WowClient(initial_savedvars=saved)
    original = restored.lua.globals().C_EncodingUtil.SerializeJSON
    calls = {"count": 0}

    def fail_second(value):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("second unarmed publication fixture failure")
        return original(value)

    restored.lua.globals().C_EncodingUtil.SerializeJSON = fail_second
    restored.login()
    assert calls["count"] == 2
    assert restored.ns.Agent.Surface() is None
    assert restored.ns.Agent.FrameBytes() is None
    assert restored.ns.Agent.Lifecycle() == "failed"
    assert restored.ns.Scanner.NativeConstructionFence() is True
    failure = str(restored.lua.globals().CrowleyDB.agentRestoreFailure)
    assert failure.startswith("second publication: ")
    assert failure.endswith("second unarmed publication fixture failure")
    assert any("agent restore failed: " + failure in message for message in restored.chat)
    marker = restored._json_table(restored.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "failed" and marker["attempts"] == 1
    assert all(shown is False for shown in restored.visible_native_textures)
    assert marker["token"].startswith("recovery:") and len(marker["token"]) == 33
    restore_button = restored.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is True

    # The failed boot remains admitted only through the exact addon-owned
    # recovery actor and can complete on a fresh ordinary load.
    restored.slash("agent recover")
    restored.click_native("restore")
    recovered = WowClient(initial_savedvars=savedvars.loads(restored.saved_variables())["CrowleyDB"])
    recovered.login()
    assert recovered.ns.Agent.Lifecycle() == "ordinary"
    assert recovered.lua.globals().CrowleyDB.nativeConstruction is None
    assert recovered.ns.Scanner.NativeConstructionFence() is False


def test_native_restore_surface_allocation_failure_reports_cause_and_preserves_recovery_route():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]

    restored = WowClient(initial_savedvars=saved)
    restored.set_texture_failure(17)
    restored.login()
    assert restored.ns.Agent.Surface() is None
    assert restored.ns.Agent.FrameBytes() is None
    assert restored.ns.Agent.Lifecycle() == "failed"
    assert restored.ns.Scanner.NativeConstructionFence() is True
    marker = restored._json_table(restored.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "failed" and marker["attempts"] == 1
    assert all(shown is False for shown in restored.visible_native_textures)
    failure = str(restored.lua.globals().CrowleyDB.agentRestoreFailure)
    assert failure.startswith("createSurface: ")
    assert failure.endswith("native texture construction failure")
    assert any("agent restore failed: " + failure in message for message in restored.chat)
    restore_button = restored.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is True


def test_native_finalize_publication_failure_rolls_back_marker_and_fence():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    saved = savedvars.loads(client.saved_variables())["CrowleyDB"]
    client = WowClient(initial_savedvars=saved)
    client.login()
    client.slash("agent release")
    client.click_native("construct")
    client.set_canonical_plan()
    client.ns.ahOpen = True
    client.slash("agent hold")
    client.slash("agent measure")
    client.advance_with_timers(100)
    client.slash("agent resume")
    assert client.ns.Agent.Lifecycle() == "constructed"

    original = client.lua.globals().C_EncodingUtil.SerializeJSON
    client.lua.globals().C_EncodingUtil.SerializeJSON = client.lua.eval(
        "function() error('finalize publication fixture failure') end"
    )
    client.slash("agent finalize")
    assert "agent finalize refused: publication_error" in client.chat[-1]
    assert "agent finalize accepted" not in client.chat
    assert client.ns.Agent.Lifecycle() == "constructed"
    assert client.ns.Scanner.NativeConstructionFence() is True
    client.lua.globals().C_EncodingUtil.SerializeJSON = original
    assert not any(
        event["type"] == "timer_create" and event["source"] == "native_construction_release"
        for event in schedule_trace(client)["events"]
    )
    marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "constructed" and marker["attempts"] == 1
    restore_button = client.lua.globals().CrowleyNativeConstructionRestore
    assert restore_button.IsShown(restore_button) is True
    assert restore_button.IsEnabled(restore_button) is True
    client.slash("agent recover")
    client.click_native("restore")
    recovered = WowClient(initial_savedvars=savedvars.loads(client.saved_variables())["CrowleyDB"])
    recovered.login()
    assert recovered.ns.Agent.Lifecycle() == "ordinary"
    assert recovered.lua.globals().CrowleyDB.nativeConstruction is None


def test_native_construction_error_cleans_partial_surface_and_preserves_restore():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    saved = savedvars.loads(client.saved_variables())["CrowleyDB"]
    deferred = WowClient(initial_savedvars=saved)
    deferred.login()
    deferred.slash("agent release")
    deferred.lua.globals().UIParent.GetWidth = deferred.lua.eval(
        "function() error('geometry fixture failure') end"
    )
    deferred.click_native("construct")
    assert deferred.ns.Agent.Surface() is None
    assert deferred.ns.Agent.Lifecycle() == "failed"
    assert deferred.lua.globals().CrowleyDB.nativeConstruction.state == "failed"
    assert deferred.lua.globals().CrowleyNativeConstructionRestore.IsShown(
        deferred.lua.globals().CrowleyNativeConstructionRestore
    )


def test_native_ordinary_surface_failure_reports_reason_and_refuses_defer():
    client = WowClient(root_width=0)
    client.login()
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.Lifecycle() == "restore_only"
    assert any("agent telemetry unavailable" in message for message in client.chat)
    client.slash("agent defer")
    assert "ordinary_boot_unavailable" in client.chat[-1]


def test_native_initial_ordinary_publication_failure_retries_on_periodic_update():
    client = WowClient()
    client.set_canonical_plan()
    original = client.lua.globals().C_EncodingUtil.SerializeJSON
    calls = {"count": 0}

    def fail_once(value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("ordinary publication transient fixture failure")
        return original(value)

    client.lua.globals().C_EncodingUtil.SerializeJSON = fail_once
    client.login()
    assert calls["count"] == 1
    assert client.ns.Agent.Surface() is not None
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.ns.Agent.FrameBytes() is None
    client.update_agent(1)
    assert calls["count"] == 2
    assert decode_frame(client.agent_frame).snapshot.value["boot"] == client.ns.Agent.Boot()
    assert client.ns.Agent.Lifecycle() == "ordinary"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None


def test_native_required_marker_and_reload_apis_fail_closed():
    marker_missing = WowClient()
    marker_missing.login()
    marker_missing.ns.SetNativeMarker = None
    marker_missing.slash("agent defer")
    assert "marker_validation_unavailable" in marker_missing.chat[-1]
    assert marker_missing.lua.globals().CrowleyDB.nativeConstruction is None

    reload_missing = WowClient()
    reload_missing.login()
    reload_missing.slash("agent defer")
    reload_missing.slash("agent recover")
    reload_missing.lua.globals().ReloadUI = None
    reload_missing.click_native("restore")
    assert reload_missing.lua.globals().CrowleyDB.nativeConstruction.state == "armed"
    assert "recovery not admitted" in reload_missing.chat[-1]


def test_native_and_logout_fences_remain_independent():
    client = WowClient()
    client.login()
    client.slash("session fence")
    assert client.ns.Scanner.PreLogoutFenceStatus()["state"] == "armed"
    client.slash("agent defer")
    assert "logout_fence_active" in client.chat[-1]
    client.ns.Scanner.SetNativeConstructionFence(True)
    assert client.ns.Scanner.NativeConstructionFence() is True
    client.ns.Scanner.SetNativeConstructionFence(False)
    assert client.ns.Scanner.PreLogoutFenceStatus()["state"] == "armed"


def test_native_release_rearms_auction_house_schedule_when_house_is_open():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.ns.Scanner.SetNativeConstructionFence(True)
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.ns.Scanner.InProgress() is False
    before = schedule_trace(client)
    client.ns.Scanner.SetNativeConstructionFence(False)
    after = schedule_trace(client)
    assert len(after["events"]) > len(before["events"])
    assert after["events"][-1]["type"] == "timer_create"
    assert after["events"][-1]["source"] == "native_construction_release"


def test_native_finalize_rearms_open_house_only_after_successful_commit():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]
    client = WowClient(initial_savedvars=saved)
    client.login()
    client.set_canonical_plan()
    client.slash("agent release")
    client.click_native("construct")
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.ns.Scanner.InProgress() is False
    client.slash("agent hold")
    client.slash("agent measure")
    client.advance_with_timers(100)
    client.slash("agent resume")
    client.slash("agent finalize")
    assert client.ns.Agent.Lifecycle() == "finalized"
    events = schedule_trace(client)["events"]
    assert events[-1]["type"] == "timer_create"
    assert events[-1]["source"] == "native_construction_release"
    assert decode_frame(client.agent_frame).snapshot.value["scanner"]["scheduleFence"]["state"] == "inactive"


def test_native_restore_rearms_open_house_only_after_successful_commit():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]
    restored = WowClient(initial_savedvars=saved)
    restored.set_canonical_plan()
    # Model the house already being open when ADDON_LOADED performs the
    # restore transaction; the production commit invokes the existing setter.
    restored.ns.ahOpen = True
    restored.login()
    assert restored.ns.Agent.Lifecycle() == "ordinary"
    assert restored.lua.globals().CrowleyDB.nativeConstruction is None
    events = schedule_trace(restored)["events"]
    assert events[-1]["type"] == "timer_create"
    assert events[-1]["source"] == "native_construction_release"
    assert (
        decode_frame(restored.agent_frame).snapshot.value["scanner"]["scheduleFence"]["state"] == "inactive"
    )


def test_native_restore_success_clears_stale_failure_diagnostic():
    source = WowClient()
    source.login()
    source.slash("agent defer")
    source.slash("agent recover")
    source.click_native("restore")
    saved = savedvars.loads(source.saved_variables())["CrowleyDB"]
    saved["agentRestoreFailure"] = "stale restore failure"

    restored = WowClient(initial_savedvars=saved)
    restored.login()
    assert restored.ns.Agent.Lifecycle() == "ordinary"
    assert restored.lua.globals().CrowleyDB.nativeConstruction is None
    assert restored.lua.globals().CrowleyDB.agentRestoreFailure is None


def test_native_restore_admission_uses_fixed_command_actor():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    before = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    client.slash("agent recover extra")
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before
    client.click_native("restore")
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before
    client.slash("agent recover")
    assert "agent recover accepted" in client.chat[-1]
    client.click_native("restore")
    marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    assert marker["state"] == "restore_pending"
    assert marker["attempts"] == 0
    assert marker["token"].startswith("recovery:") and len(marker["token"]) == 33
    assert client.reload_calls == 1


def test_native_recovery_refuses_plain_ordinary_and_inflight_restore():
    ordinary = WowClient()
    ordinary.login()
    ordinary.slash("agent recover")
    assert "recovery_not_required" in ordinary.chat[-1]

    pending = WowClient()
    pending.login()
    pending.slash("agent defer")
    pending.slash("agent recover")
    pending.click_native("restore")
    pending.slash("agent recover")
    assert "recovery_pending" in pending.chat[-1]


def test_native_release_window_expires_at_ten_seconds():
    client = deferred_native_client()
    client.slash("agent release")
    client.advance(10)
    client.click_native("construct")
    assert client.ns.Agent.Lifecycle() == "deferred"
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "deferred"
    assert "construction_not_released" in client.chat[-1]


def test_native_restore_disables_controls_and_same_load_fallback_reenables_restore():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    client.click_native("restore")
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "armed"
    client.slash("agent recover")
    assert (
        client.lua.globals().CrowleyNativeConstructionConstruct.IsShown(
            client.lua.globals().CrowleyNativeConstructionConstruct
        )
        is False
    )
    client.click_native("restore")
    assert client.lua.globals().CrowleyDB.nativeConstruction.state == "restore_pending"
    assert (
        client.lua.globals().CrowleyNativeConstructionConstruct.IsEnabled(
            client.lua.globals().CrowleyNativeConstructionConstruct
        )
        is False
    )
    assert (
        client.lua.globals().CrowleyNativeConstructionRestore.IsEnabled(
            client.lua.globals().CrowleyNativeConstructionRestore
        )
        is False
    )
    client.advance_with_timers(2)
    assert client.ns.Agent.RecoveryAdmitted() is True
    assert (
        client.lua.globals().CrowleyNativeConstructionRestore.IsEnabled(
            client.lua.globals().CrowleyNativeConstructionRestore
        )
        is True
    )


def test_native_restore_timer_cannot_admit_a_newer_nonce_attempt():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    client.slash("agent recover")
    client.click_native("restore")
    first = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    # A newer persisted attempt has a distinct nonce while the older timer is
    # still queued.  The callback must reject its stale nonce and leave the
    # controls in-flight.
    newer = dict(first)
    newer["token"] = "recovery:" + "b" * 24
    client.lua.globals().CrowleyDB.nativeConstruction = client._to_lua(newer)
    client.advance_with_timers(2)
    assert client.ns.Agent.RecoveryAdmitted() is False
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == newer
    assert (
        client.lua.globals().CrowleyNativeConstructionRestore.IsEnabled(
            client.lua.globals().CrowleyNativeConstructionRestore
        )
        is False
    )


def test_native_restore_after_finalize_remains_reachable_and_hides_after_successful_reload():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    saved = savedvars.loads(client.saved_variables())["CrowleyDB"]
    client = WowClient(initial_savedvars=saved)
    client.login()
    client.set_canonical_plan()
    client.slash("agent release")
    client.click_native("construct")
    client.slash("agent hold")
    client.slash("agent measure")
    client.advance_with_timers(100)
    client.slash("agent resume")
    client.slash("agent finalize")
    assert client.ns.Agent.Lifecycle() == "finalized"
    assert client.lua.globals().CrowleyNativeConstructionRestore.IsShown(
        client.lua.globals().CrowleyNativeConstructionRestore
    )
    client.slash("agent recover")
    assert client.ns.Scanner.NativeConstructionFence() is True
    client.update_agent(1)
    recovery_frame = decode_frame(client.agent_frame).snapshot.value
    assert recovery_frame["scanner"]["scheduleFence"] == {
        "state": "armed",
        "reason": "",
        "command": "/crowley session fence",
    }
    assert (
        client.lua.globals().CrowleyNativeConstructionConstruct.IsShown(
            client.lua.globals().CrowleyNativeConstructionConstruct
        )
        is False
    )
    client.click_native("restore")
    restored = WowClient(initial_savedvars=savedvars.loads(client.saved_variables())["CrowleyDB"])
    restored.login()
    assert restored.ns.Agent.Lifecycle() == "ordinary"
    assert restored.lua.globals().CrowleyDB.nativeConstruction is None
    assert (
        restored.lua.globals().CrowleyNativeConstructionPanel.IsShown(
            restored.lua.globals().CrowleyNativeConstructionPanel
        )
        is False
    )
    restored.slash("agent defer")
    assert restored.lua.globals().CrowleyDB.nativeConstruction.state == "armed"


def test_native_panel_is_commands_owned_and_fixed_geometry():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    panel = client.lua.globals().CrowleyNativeConstructionPanel
    assert (panel.width, panel.height) == (220, 84)
    assert list(panel.point.values())[0] == "TOPRIGHT"
    assert panel.point[2].name == "UIParent"
    assert list(panel.point.values())[2:] == ["TOPRIGHT", -24, -24]
    assert client.lua.globals().CrowleyNativeConstructionConstruct.template == "UIPanelButtonTemplate"
    assert client.lua.globals().CrowleyNativeConstructionRestore.template == "UIPanelButtonTemplate"


def test_native_finalize_clears_only_native_fence_after_completed_measurement():
    client = WowClient()
    client.login()
    client.slash("agent defer")
    saved = savedvars.loads(client.saved_variables())["CrowleyDB"]
    client = WowClient(initial_savedvars=saved)
    client.login()
    client.slash("agent release")
    client.click_native("construct")
    client.slash("agent hold")
    client.slash("agent measure")
    client.advance_with_timers(100)
    client.slash("agent resume")
    before_observed = client._json_table(client.ns.Agent.Snapshot())["sequence"]
    client.slash("agent finalize")
    client.update_agent(1)
    after_observed = client._json_table(client.ns.Agent.Snapshot())["sequence"]
    assert after_observed > before_observed
    assert client.ns.Agent.Lifecycle() == "finalized"
    assert client.lua.globals().CrowleyDB.nativeConstruction is None
    assert client.ns.Scanner.NativeConstructionFence() is False


def test_native_finalize_refuses_resume_from_held_zero_without_measurement():
    client = deferred_native_client()
    client.slash("agent release")
    client.click_native("construct")
    client.slash("agent hold")
    client.slash("agent resume")
    client.slash("agent finalize")
    assert "agent finalize refused: finalize_requires_resume" in client.chat[-1]
    assert client.ns.Agent.Lifecycle() == "constructed"
    assert client.ns.Scanner.NativeConstructionFence() is True
    assert client.ns.Scanner.LogoutFenceActive() is False


def test_query_trace_records_barriers_readiness_and_unchanged_outcomes():
    client = WowClient()
    run_sweep(client)
    trace = client.query_trace
    assert trace["version"] == 1
    assert trace["dropped"] >= 0
    assert len(trace["events"]) <= trace["cap"]
    assert [event["sequence"] for event in trace["events"]] == sorted(
        event["sequence"] for event in trace["events"]
    )
    types = [event["type"] for event in trace["events"]]
    assert "dispatch" in types and "query_send" in types and "query_outcome" in types
    dispatch = next(event for event in trace["events"] if event["type"] == "dispatch")
    assert dispatch["matching"] is True
    assert dispatch["barriers"] == {"sentRequested": True, "sent": True, "response": True, "awaiting": True}
    readiness = next(
        event for event in trace["events"] if event["type"] == "readiness" and event.get("itemID") == 2589
    )
    assert readiness["readiness"] == {
        "hasSearchResults": {"status": "ok", "value": True},
        "hasFull": {"status": "ok", "value": True},
        "quantity": {"status": "ok", "value": 2},
        "count": {"status": "ok", "value": 1},
    }
    assert [
        entry["kind"]
        for entry in client.send_log
        if (entry["kind"] == "search" and entry["itemID"] == 2589) or entry["kind"] == "owned"
    ] == ["search", "owned"]
    assert any(
        event["outcome"] == "full" and event.get("itemID") == 2589
        for event in trace["events"]
        if event["type"] == "query_outcome"
    )


def test_harness_keeps_commodity_sell_search_ignored_and_search_query_live():
    client = WowClient()
    client.set_auctions([Auction(2555, "Linen", 27, 100, isCommodity=True)])
    key = client.lua.globals().C_AuctionHouse.MakeItemKey(2555, 0, 0, 0)
    sorts = client._to_lua([{"sortOrder": 1, "reverseSort": False}])
    client.lua.globals().C_AuctionHouse.SendSellSearchQuery(key, sorts, False)
    assert client.ignored_commodity_sell_queries == 1
    assert client.state.queryCalls == 0
    assert client.send_log == []
    client.lua.globals().C_AuctionHouse.SendSearchQuery(key, sorts, False, 0, 0)
    client.run_timers()
    assert client.state.queryCalls == 1
    assert client.send_log == [{"at": 1760000000, "kind": "search", "itemID": 2555}]
    assert [entry["method"] for entry in client.query_methods] == [
        "SendSellSearchQuery",
        "SendSearchQuery",
    ]


def test_mixed_commodity_and_item_queries_select_native_methods_and_validate_payload():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(2555, "Linen", 27, 100, isCommodity=True),
            Auction(2589, "Cloth", 2, 200),
        ]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    parsed = validate_v2_envelope(envelope(raw))["decoded"]
    assert all(parsed["observations"][item_id]["coverage"] == "full" for item_id in ("2555", "2589"))
    assert parsed["observations"]["2555"]["rows"]["row:1"]["itemID"] == 2555
    assert parsed["observations"]["2589"]["rows"]["row:1"]["itemID"] == 2589
    initial = [
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send"
        and event.get("operation") == "search"
        and event["itemID"] in {2555, 2589}
    ]
    assert [(event["itemID"], event["nativeMethod"]) for event in initial] == [
        (2555, "SendSearchQuery"),
        (2589, "SendSearchQuery"),
    ]
    assert [
        (entry["itemID"], entry["method"])
        for entry in client.query_methods
        if entry["itemID"] in {2555, 2589}
    ] == [
        (2555, "SendSearchQuery"),
        (2589, "SendSearchQuery"),
    ]


@pytest.mark.parametrize(
    ("commodity", "event_name"),
    [
        (False, "ITEM_SEARCH_RESULTS_ADDED"),
        (False, "ITEM_SEARCH_RESULTS_UPDATED"),
        (True, "COMMODITY_SEARCH_RESULTS_ADDED"),
        (True, "COMMODITY_SEARCH_RESULTS_UPDATED"),
    ],
)
def test_query_trace_preserves_native_added_or_updated_event_name(commodity, event_name):
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100, isCommodity=commodity)])
    event_kind = event_name.rsplit("_", 1)[-1]
    client.set_event_behavior(kind=event_kind)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    trace = client.active_query_trace or client.query_trace
    dispatch = next(
        event
        for event in trace["events"]
        if event["type"] == "dispatch" and event["matching"] and event.get("itemID") == 2589
    )
    assert dispatch["nativeEvent"] == event_name


def test_result_before_barrier_is_recorded_without_changing_send_or_timeout_outcome():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(before_barriers=True)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance(30)
    client.run_timers()
    trace = client.active_query_trace or client.query_trace
    dispatch = next(event for event in trace["events"] if event["type"] == "dispatch")
    assert dispatch["matching"] is True
    assert dispatch["barriers"] == {
        "sentRequested": True,
        "sent": False,
        "response": False,
        "awaiting": False,
    }
    assert [entry["kind"] for entry in client.send_log if entry["itemID"] == 2589] == ["search"]
    assert any(
        event.get("reason") == "query_timeout"
        for event in trace["events"]
        if event["type"] == "query_outcome"
    )


def test_foreign_result_is_traced_without_becoming_a_matching_dispatch():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(stale=True)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    foreign = [
        event
        for event in (client.active_query_trace or client.query_trace)["events"]
        if event["type"] == "dispatch" and not event["matching"]
    ]
    assert foreign and foreign[0]["resultIdentity"]["itemID"] == 1002588
    assert foreign[0]["nativeEvent"] == "ITEM_SEARCH_RESULTS_UPDATED"


def test_query_trace_cap_and_completed_lifecycle_are_bounded():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.set_event_behavior(delay=5)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.active_query_trace["version"] == 1
    for _ in range(700):
        client.run_timers()
        if client.query_trace is not None:
            break
        client.advance(1)
    trace = client.query_trace
    assert trace["cap"] == 256
    assert len(trace["events"]) == trace["cap"]
    assert trace["dropped"] > 0
    assert trace["sequence"] > trace["cap"]
    assert client.active_query_trace is None


def test_new_sweep_resets_active_trace_and_retains_previous_completed_trace():
    client = WowClient()
    run_sweep(client)
    completed = client.query_trace
    client.advance(900)
    client.fire("AUCTION_HOUSE_CLOSED")
    client.set_event_behavior(delay=5)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.active_query_trace["events"][0]["sequence"] == 1
    assert client.active_query_trace["events"][0]["generation"] != completed["events"][0]["generation"]
    assert client.query_trace == completed


@pytest.mark.parametrize("mode", ["absent", "throwing"])
def test_readiness_diagnostic_unsupported_or_error_does_not_break_scan(mode):
    client = WowClient()
    client.set_readiness_api("HasSearchResults", mode)
    raw = run_sweep(client)
    assert validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]["coverage"] == "full"
    readiness = next(event for event in client.query_trace["events"] if event["type"] == "readiness")
    assert readiness["readiness"]["hasSearchResults"]["status"] == mode.replace(
        "absent", "unsupported"
    ).replace("throwing", "error")


def test_absent_plan_refuses_actionable_collection():
    client = WowClient()
    client.login()
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert "scan refused: no scan plan loaded" in client.chat[-1]
    assert not list(client.lua.globals().CrowleyDB.scans.items())


def test_targeted_sweep_roundtrips_strict_v3_payload():
    raw = run_sweep(WowClient())
    parsed = validate_v2_envelope(envelope(raw))
    assert raw["format"] == 3
    observation = parsed["decoded"]["observations"]["2589"]
    assert observation["coverage"] == "full"
    assert observation["rows"]["row:1"]["unitPrice"] == {"numerator": 200, "denominator": 1}
    assert parsed["decoded"]["owned"]["status"] == "full"


def test_commodity_sweep_uses_unit_price_and_item_id_key_without_link():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen Cloth", 7, 25, isCommodity=True)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    assert observation["kind"] == "commodity"
    assert observation["rows"]["row:1"]["itemKey"] == "2589"
    assert observation["rows"]["row:1"]["price"] == {"kind": "unit", "copper": 25}


def test_format3_grouped_item_uses_one_auction_buyout_and_emits_format3():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen Cloth", 4, 1500, auctionID=1)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    row = observation["rows"]["row:1"]
    assert raw["format"] == 3
    assert row["quantity"] == 4
    assert row["unitPrice"] == {"numerator": 1500, "denominator": 1}


def test_format3_decodes_wrapped_commodity_time_left_and_rejects_other_large_values():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [Auction(2589, "Linen Cloth", 1, 25, isCommodity=True, timeLeftSeconds=4_294_967_293)]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    row = decoded_payload(client)["observations"]["2589"]["rows"]["row:1"]
    assert row["timeLeft"] == {"kind": "exact", "seconds": -3}

    rejected = WowClient()
    rejected.login()
    rejected.set_canonical_plan()
    rejected.set_auctions(
        [Auction(2589, "Linen Cloth", 1, 25, isCommodity=True, timeLeftSeconds=4_294_000_000)]
    )
    rejected.fire("AUCTION_HOUSE_SHOW")
    rejected.slash("scan")
    rejected.run_timers()
    observation = decoded_payload(rejected)["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "numeric_range"


def test_item_depth_selection_is_ordered_by_per_auction_copper():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(2589, "Linen Cloth", 100 if price == 21 else 1, price, auctionID=price)
            for price in range(1, 22)
        ]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "bounded"
    assert {row["resultID"] for row in observation["rows"].values()} == set(range(1, 21))
    assert observation["cutoff"] == {"kind": "price", "numerator": 20, "denominator": 1}
    assert observation["higherPriceWitness"] == {"kind": "known", "numerator": 21, "denominator": 1}


def test_representative_full_watchlist_payload_size_is_measured():
    client = WowClient()
    client.login()
    ids = load_catalog().scan_plan()["itemIDs"]
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(itemID=item_id, name=f"I{item_id}", count=1, buyout=100, auctionID=item_id)
            for item_id in ids
        ]
    )
    # A one-second response cadence keeps the real rolling 100/60s transport
    # window active while allowing all 428 initial queries to finish within the
    # 600-second sweep budget.
    client.set_event_behavior(delay=1)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    for _ in range(700):
        client.run_timers()
        if list(client.lua.globals().CrowleyDB.scans.items()):
            break
        client.advance_with_timers(1)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    payload = decoded_payload(client)
    assert raw["numObservations"] == 428
    assert raw["numRows"] == 428
    assert all(observation["coverage"] == "full" for observation in payload["observations"].values())
    # This is a measured representative fixture, not a wire-size promise.  Keep a
    # generous budget so a harmless encoder change cannot turn the test brittle.
    assert 10_000 < len(raw["payload"]) < 100_000


def test_sweep_timeout_dispatches_before_later_inserted_timer():
    client = WowClient()
    first = run_sweep(client)
    client.advance_with_timers(900)
    scans = list(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values())
    assert len(scans) == 2
    second = next(scan for scan in scans if scan["id"] != first["id"])
    assert decoded_payload(client)["observations"]["2589"]["coverage"] == "full"
    assert second["finishedAt"] >= second["startedAt"]


def test_finished_sweep_keeps_immutable_plan_provenance():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(delay=5)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    original_hash = client.lua.globals().CROWLEY_DATA.scanPlan.planHash
    client.set_plan([2592])  # deliberately invalid replacement while the canonical sweep is active
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    payload = decoded_payload(client)
    assert payload["planHash"] == original_hash
    assert json.loads(payload["planJSON"])["itemIDs"] == load_catalog().scan_plan()["itemIDs"]


def test_documented_structured_keys_and_cumulative_paging():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.pageSize = 10
    client.set_auctions(
        [Auction(2589, "Linen", 1, 100 + index, auctionID=1000 + index) for index in range(25)]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    payload = decoded_payload(client)
    observation = payload["observations"]["2589"]
    assert observation["coverage"] == "bounded"
    assert len(observation["rows"]) == 20
    focused_sends = [
        entry for entry in client.send_log if entry["itemID"] == 2589 or entry["kind"] == "owned"
    ]
    assert len(focused_sends) == 4  # search, two cumulative pages, owned query
    assert all(entry["kind"] in {"search", "item_page", "owned"} for entry in client.send_log)


def test_bounded_commodity_has_known_witness_and_strict_roundtrip():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_source_incomplete()
    client.set_auctions(
        [Auction(2589, "Linen", 1, price, isCommodity=True, auctionID=price) for price in range(1, 22)]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    parsed = validate_v2_envelope(envelope(raw))["decoded"]
    observation = parsed["observations"]["2589"]
    assert observation["coverage"] == "bounded"
    assert observation["sourceComplete"] is False
    assert observation["cutoff"] == {"kind": "price", "numerator": 20, "denominator": 1}
    assert observation["higherPriceWitness"] == {"kind": "known", "numerator": 21, "denominator": 1}
    assert len(observation["rows"]) == 20


def test_full_item_uses_per_auction_buyout_for_unit_price_and_order():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(2589, "Linen", 2, 2, auctionID=1),
            Auction(2589, "Linen", 1, 3, auctionID=2),
            Auction(2589, "Linen", 4, 4, auctionID=3),
        ]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    assert observation["coverage"] == "full"
    ordered_rows = [observation["rows"][f"row:{index}"] for index in range(1, 4)]
    assert [row["resultID"] for row in ordered_rows] == [1, 2, 3]
    assert [row["unitPrice"] for row in ordered_rows] == [
        {"numerator": 2, "denominator": 1},
        {"numerator": 3, "denominator": 1},
        {"numerator": 4, "denominator": 1},
    ]


def test_page_limit_preserves_sorted_partial_rows_beyond_depth():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.pageSize = 3
    client.set_auctions(
        [Auction(2589, "Linen", 2 if price % 2 == 0 else 1, price, auctionID=price) for price in range(1, 34)]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "page_limit"
    assert observation["rowCount"] == 30
    assert len(observation["rows"]) > 20
    first_units = [observation["rows"][f"row:{index}"]["unitPrice"] for index in range(1, 5)]
    assert first_units == [
        {"numerator": 1, "denominator": 1},
        {"numerator": 2, "denominator": 1},
        {"numerator": 3, "denominator": 1},
        {"numerator": 4, "denominator": 1},
    ]


def test_item_buyout_comparison_uses_safe_unit_copper_without_quantity_cross_products():
    max_safe = 9007199254740991
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(2589, "Linen", 1, 1, auctionID=1),
            Auction(2589, "Linen", max_safe, max_safe - 1, auctionID=2),
            Auction(2589, "Linen", 1, max_safe, auctionID=3),
        ]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    assert observation["coverage"] == "full"
    assert observation["reasonCode"] == "ok"
    assert observation["rowCount"] == 3


def test_advertised_missing_result_is_incomplete_not_empty_full_scope():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_missing_results([1])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "identity_mismatch"
    assert observation["rowCount"] == 0


def test_equal_price_ties_roundtrip_in_any_result_identity_order():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [Auction(2589, "Linen", 1, 100, isCommodity=True, auctionID=index) for index in range(1, 13)]
    )
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    observation = validate_v2_envelope(envelope(raw))["decoded"]["observations"]["2589"]
    assert observation["coverage"] == "full"
    assert observation["rowCount"] == 12


def test_market_abort_keeps_owned_collection_unqueried():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(delay=5)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.fire("PLAYER_LOGOUT")
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    owned = validate_v2_envelope(envelope(raw))["decoded"]["owned"]
    assert owned["status"] == "unqueried"
    assert owned["query"] == {"kind": "unqueried"}
    assert owned["reasonCode"] == "not_started"


def test_owned_house_change_is_incomplete_and_override_finalizes_before_apply():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.set_owned([Auction(2589, "Linen", 1, 100)], complete=True)
    client.set_owned_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    advance_to_owned_query(client)
    client.slash("set faction alliance")
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    owned = validate_v2_envelope(envelope(raw))["decoded"]["owned"]
    assert owned["status"] == "incomplete"
    assert owned["reasonCode"] == "house_changed"
    assert client.lua.globals().CrowleyDB.settings.faction == "alliance"


def test_owned_result_rechecks_captured_house_before_accepting_rows():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.set_owned([Auction(2589, "Linen", 1, 100)], complete=True)
    client.set_owned_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    advance_to_owned_query(client)
    client.set_house(faction="Alliance")
    client.advance(5)
    client.run_timers()
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    owned = validate_v2_envelope(envelope(raw))["decoded"]["owned"]
    assert owned["status"] == "incomplete"
    assert owned["reasonCode"] == "house_changed"


def test_hidden_owner_metadata_is_unknown_not_affirmative_grouping():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100, hiddenOwners=True)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    observation = decoded_payload(client)["observations"]["2589"]
    row = next(iter(observation["rows"].values()))
    assert row["owners"]["identityKind"] == "unknown"
    assert row["owners"]["names"] == {}


def test_shared_boundary_level_keeps_every_row_at_cutoff():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    auctions = [Auction(2589, "Linen", 1, price, isCommodity=True, auctionID=price) for price in range(1, 20)]
    auctions.extend(
        [
            Auction(2589, "Linen", 2, 20, isCommodity=True, auctionID=2001),
            Auction(2589, "Linen", 3, 20, isCommodity=True, auctionID=2002),
            Auction(2589, "Linen", 1, 21, isCommodity=True, auctionID=2003),
        ]
    )
    client.set_auctions(auctions)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "bounded"
    assert observation["cutoff"] == {"kind": "price", "numerator": 20, "denominator": 1}
    assert len(observation["rows"]) == 21
    assert sum(row["unitPrice"]["numerator"] == 20 for row in observation["rows"].values()) == 2


def test_external_same_key_search_is_marked_incomplete():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.external_search(2589)
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    assert decoded_payload(client)["observations"]["2589"]["reasonCode"] == "identity_mismatch"


def test_cumulative_page_individual_to_grouped_identity_is_incomplete():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.pageSize = 1
    client.set_auctions(
        [
            Auction(2589, "Linen", 1, 100, auctionID=1),
            Auction(2589, "Linen", 1, 200, auctionID=2),
        ]
    )
    client.set_event_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance(5)
    client.run_timers()
    client.state.auctions[1].containsOwnerItem = True
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "identity_mismatch"


def test_cumulative_page_grouped_to_individual_identity_is_incomplete():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.pageSize = 1
    client.set_auctions(
        [
            Auction(2589, "Linen", 1, 100, auctionID=1, containsOwnerItem=True),
            Auction(2589, "Linen", 1, 200, auctionID=2),
        ]
    )
    client.set_event_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance(5)
    client.run_timers()
    client.state.auctions[1].containsOwnerItem = False
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "identity_mismatch"


def test_cumulative_page_individual_to_hidden_owner_identity_is_incomplete():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.pageSize = 1
    client.set_auctions(
        [
            Auction(2589, "Linen", 1, 100, auctionID=1),
            Auction(2589, "Linen", 1, 200, auctionID=2),
        ]
    )
    client.set_event_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance(5)
    client.run_timers()
    hidden = client.lua.table()
    client.state.auctions[1].owners = hidden
    client.state.auctions[1].totalOwners = 0
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "incomplete"
    assert observation["reasonCode"] == "identity_mismatch"


def test_no_payload_received_event_cannot_complete_search():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(kind="RECEIVED")
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    assert not list(client.lua.globals().CrowleyDB.scans.items())
    client.advance(30)
    client.run_timers()
    finish_canonical(client)
    assert decoded_payload(client)["observations"]["2589"]["reasonCode"] == "query_timeout"


def test_stale_unrelated_result_event_does_not_complete_active_query():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(stale=True)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    finish_canonical(client)
    assert decoded_payload(client)["observations"]["2589"]["coverage"] == "full"


def test_logout_preserves_partial_and_marks_tail_reason():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(delay=5)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.fire("PLAYER_LOGOUT")
    client.run_timers()
    observations = decoded_payload(client)["observations"]
    assert observations["2589"]["reasonCode"] == "logout"
    assert observations["2592"]["reasonCode"] == "logout"
    assert observations["2592"]["coverage"] == "unqueried"
    client.advance(900)
    client.run_timers()
    assert client.publish_agent()["scanner"]["phase"] == "ready"


def test_start_cadence_and_no_overlap():
    client = WowClient()
    first = run_sweep(client)
    client.slash("scan")
    assert "due in" in client.chat[-1]
    client.state.fastCanonical = False
    remaining = client.ns.Scanner.CooldownRemaining()
    client.advance(max(0, remaining - 1))
    client.run_timers()
    assert len(list(client.lua.globals().CrowleyDB.scans.items())) == 1
    client.advance(1)
    client.fire("AUCTION_HOUSE_SHOW")
    client.state.fastCanonical = True
    client.run_timers()
    assert len(list(client.lua.globals().CrowleyDB.scans.items())) == 2
    starts = sorted(scan["startedAt"] for _, scan in client.lua.globals().CrowleyDB.scans.items())
    assert first["startedAt"] + 900 == starts[-1]


def test_timer_alone_starts_at_900_seconds_without_auction_house_reopen_or_overlap():
    client = WowClient()
    run_sweep(client)
    client.state.fastCanonical = False
    client.slash("scan")
    assert "due in" in client.chat[-1]

    remaining = client.ns.Scanner.CooldownRemaining()
    client.advance(remaining - 1)
    client.run_timers()
    assert len(list(client.lua.globals().CrowleyDB.scans.items())) == 1
    assert not client.ns.Scanner.InProgress()

    client.advance(1)
    client.run_timers()
    trace = schedule_trace(client)
    starts = [event for event in trace["events"] if event["type"] == "start_commit"]
    assert len(starts) == 2
    assert client.ns.Scanner.InProgress()
    client.slash("scan")
    assert "already running" in client.chat[-1]
    assert len([event for event in schedule_trace(client)["events"] if event["type"] == "start_commit"]) == 2


def test_reload_owner_uses_persisted_last_sweep_start_for_schedule_due():
    client = WowClient()
    client.lua.globals().CrowleyDB = client._to_lua({"lastSweepStart": 1760000000})
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.lua.globals()._nextTimerAt() == 1760000900
    timer = next(event for event in schedule_trace(client)["events"] if event["type"] == "timer_create")
    assert timer["delay"] == 900
    assert timer["due"] == 1760000900


@pytest.mark.parametrize(
    ("event", "reason"), [("AUCTION_HOUSE_CLOSED", "ah_closed"), ("PLAYER_LOGOUT", "logout")]
)
def test_schedule_callback_is_cancelled_and_stale_after_lifecycle_event(event: str, reason: str):
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    client.fire(event)
    client.run_timers()
    trace = schedule_trace(client)
    assert any(item["type"] == "cancel" and item["reason"] == reason for item in trace["events"])
    assert any(item["type"] == "stale_token" for item in trace["events"])
    assert any(item["type"] == "ah_closed_discard" for item in trace["events"])
    assert not client.ns.Scanner.InProgress()
    assert client.state.queryCalls == 0


def test_schedule_callback_is_cancelled_and_stale_after_pre_logout_fence():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.ns.Scanner.PreLogoutFence() == (True, None)
    client.run_timers()
    trace = schedule_trace(client)
    assert any(item["type"] == "cancel" and item["reason"] == "pre_logout_fence" for item in trace["events"])
    assert any(item["type"] == "stale_token" for item in trace["events"])
    assert not client.ns.Scanner.InProgress()
    assert client.state.queryCalls == 0


def test_schedule_refusals_preserve_plan_and_queue_blocks():
    client = WowClient()
    client.login()
    client.set_plan([2589])
    client.fire("AUCTION_HOUSE_SHOW")
    client.lua.globals().CrowleyDB.storageBlocked = True
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    reasons = [item["reason"] for item in schedule_trace(client)["events"] if item["type"] == "refusal"]
    assert "plan_invalid" in reasons
    assert "storage_blocked" in reasons
    assert client.lua.globals()._nextTimerAt() is None
    assert client.state.queryCalls == 0


def test_active_sweep_rejects_schedule_and_duplicate_start_without_overlap():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    before = client.state.queryCalls
    client.ns.Scanner.Schedule("active-test")
    client.slash("scan")
    trace = schedule_trace(client)
    assert any(item["type"] == "refusal" and item["reason"] == "sweep_active" for item in trace["events"])
    assert client.state.queryCalls == before
    assert client.ns.Scanner.InProgress()
    assert "already running" in client.chat[-1]


def test_schedule_trace_is_bounded_fixed_schema_and_separate_from_query_trace():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    for _ in range(40):
        client.ns.Scanner.Schedule("source-" + ("x" * 100))
    trace = schedule_trace(client)
    assert trace["version"] == 1
    assert trace["cap"] == 64
    assert len(trace["events"]) == 64
    assert trace["overflow"] > 0
    fields = {
        "sequence",
        "type",
        "wall",
        "uptime",
        "load",
        "token",
        "currentToken",
        "reason",
        "source",
        "due",
        "delay",
    }
    assert all(set(event) == fields for event in trace["events"])
    assert all(len(event["source"]) <= 32 for event in trace["events"])
    assert client.lua.globals().CrowleyDB.activeQueryTrace is None
    assert client.state.queryCalls == 0


def test_early_timer_callback_with_separate_external_clocks_rearms_at_wall_due():
    # The pre-fix diagnostic proved this callback consumed the only timer.  The
    # focused correction must retain the wall/timer split and re-arm once.
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.fastCanonical = False
    globals_ = client.lua.globals()
    globals_._wallNow = 1760000000
    globals_._uptimeNow = 500
    globals_.time = client.lua.eval("function() return _wallNow end")
    globals_.GetTime = client.lua.eval("function() return _uptimeNow end")
    globals_.CrowleyDB.lastSweepStart = 1760000000
    client.fire("AUCTION_HOUSE_SHOW")
    assert globals_._nextTimerAt() == 1760000900

    globals_._wallNow = 1760000899
    globals_._uptimeNow = 1400
    client.advance(900)
    client.run_timers()
    trace = schedule_trace(client)
    refusal = next(item for item in trace["events"] if item["type"] == "due_refusal")
    assert refusal["delay"] == 1
    assert refusal["due"] == 1760000900
    assert refusal["wall"] == 1760000899
    assert refusal["uptime"] == 1400
    assert not client.ns.Scanner.InProgress()
    assert globals_._nextTimerAt() == 1760000901

    globals_._wallNow = 1760000900
    globals_._uptimeNow = 1401
    client.advance(1)
    client.run_timers()
    trace = schedule_trace(client)
    assert len([item for item in trace["events"] if item["type"] == "due_refusal"]) == 1
    assert (
        len(
            [
                item
                for item in trace["events"]
                if item["type"] == "timer_create" and item["source"] == "timer_rearm"
            ]
        )
        == 1
    )
    assert len([item for item in trace["events"] if item["type"] == "start_commit"]) == 1
    assert client.ns.Scanner.InProgress()


def test_repeated_early_callbacks_rearm_once_each_without_duplicate_start_and_cancel():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.state.fastCanonical = False
    globals_ = client.lua.globals()
    globals_._wallNow = 1760000000
    globals_._uptimeNow = 500
    globals_.time = client.lua.eval("function() return _wallNow end")
    globals_.GetTime = client.lua.eval("function() return _uptimeNow end")
    globals_.CrowleyDB.lastSweepStart = 1760000000
    client.fire("AUCTION_HOUSE_SHOW")
    chat_before_callbacks = len(client.chat)

    globals_._wallNow = 1760000899
    globals_._uptimeNow = 1400
    client.advance(900)
    client.run_timers()
    for _ in range(2):
        globals_._uptimeNow += 1
        client.advance(1)
        client.run_timers()

    trace = schedule_trace(client)
    assert len([item for item in trace["events"] if item["type"] == "due_refusal"]) == 3
    assert (
        len(
            [
                item
                for item in trace["events"]
                if item["type"] == "timer_create" and item["source"] == "timer_rearm"
            ]
        )
        == 3
    )
    assert not [item for item in trace["events"] if item["type"] == "start_commit"]
    assert client.state.queryCalls == 0
    assert len(client.chat) == chat_before_callbacks

    client.fire("AUCTION_HOUSE_CLOSED")
    client.advance(1)
    client.run_timers()
    trace = schedule_trace(client)
    assert any(item["type"] == "cancel" and item["reason"] == "ah_closed" for item in trace["events"])
    assert any(item["type"] == "stale_token" for item in trace["events"])
    assert client.state.queryCalls == 0


def test_schedule_trace_does_not_claim_timer_creation_when_api_raises():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.lua.globals().C_Timer.After = client.lua.eval("function() error('timer API failure') end")
    with pytest.raises(Exception, match="timer API failure"):
        client.fire("AUCTION_HOUSE_SHOW")
    trace = schedule_trace(client)
    assert any(item["type"] == "request" and item["source"] == "ah_show" for item in trace["events"])
    assert not any(item["type"] == "timer_create" for item in trace["events"])


def test_ah_close_finalizes_partial_without_replaying_callback():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen Cloth", 2, 200)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.fire("AUCTION_HOUSE_CLOSED")
    client.run_timers()
    scans = list(client.lua.globals().CrowleyDB.scans.items())
    assert len(scans) == 1
    assert "sweep saved" in client.chat[-1]


def test_owned_and_query_api_are_reported_by_probe():
    client = WowClient()
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]
    names = probe["namespaces"]["C_AuctionHouse"]
    assert "SendSellSearchQuery" in names
    assert "QueryOwnedAuctions" in names


def test_probe_records_actual_movement_api_presence_and_binding_actions():
    client = WowClient()
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]
    assert probe["motion"] == {
        "apiPresence": {
            "UnitPosition": True,
            "GetPlayerFacing": True,
            "GetUnitSpeed": True,
            "UnitAffectingCombat": True,
            "UnitIsDeadOrGhost": True,
            "GetBindingKey": True,
        },
        "bindingActions": [
            "MOVEFORWARD",
            "MOVEBACKWARD",
            "STRAFELEFT",
            "STRAFERIGHT",
            "TURNLEFT",
        ],
    }


def test_probe_records_native_region_realm_and_optional_namespace_surfaces():
    client = WowClient(
        target=non_beta_target(),
        realm="Normal",
        current_region=1,
        current_region_name="",
        realm_info=True,
        game_rules=True,
    )
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["region"] == 1
    assert probe["regionName"] == ""
    assert probe["realmName"] == "Normal"
    assert probe["context"]["realm"] == "Normal"
    assert "GetRealmInfo" in probe["namespaces"]["C_RealmInfo"]
    assert "GetActiveGameMode" in probe["namespaces"]["C_GameRules"]


def test_probe_keeps_native_diagnostics_when_house_detection_raises():
    client = WowClient(target=non_beta_target(), realm="Normal", current_region=1, faction=None)
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["region"] == 1
    assert probe["regionName"] == "US"
    assert probe["realmName"] == "Normal"
    assert probe["context"] is False
    assert "native player faction" in probe["contextError"]
    assert probe["house"] is False
    assert "attributionError" in probe
    assert "unable to detect native faction" in probe["attributionError"]


def test_agent_samples_actual_motion_and_changed_values_are_fresh():
    client = WowClient()
    client.set_canonical_plan()
    client.login()
    first = client.publish_agent()
    client.set_motion(position=(101.25, 202.5, 31.75, 2), facing=2.5, speed=7.5, combat=True)
    second = client.publish_agent()
    assert first["schema"] == second["schema"] == 6
    assert first["motion"]["native"]["ctm"] == {
        "accessor": "C_CVar.GetCVarBool",
        "cvar": "autointeract",
        "state": "disabled",
        "reason": "",
    }
    assert first["motion"]["native"]["ground"]["state"] == "grounded"
    assert first["motion"]["native"]["speedTuple"] == {
        "current": 0,
        "run": 7,
        "flight": 7,
        "swim": 4.7222218513489,
        "unitStatus": "unverified",
    }
    assert first["motion"]["position"] == {"x": 100.25, "y": 200.5, "z": 30.75, "instance": 1}
    assert second["motion"]["position"] == {"x": 101.25, "y": 202.5, "z": 31.75, "instance": 2}
    assert second["motion"]["facing"] == 2.5
    assert second["motion"]["speed"] == 7.5
    assert second["motion"]["combat"] is True
    assert second["sequence"] > first["sequence"]
    decoded = decode_frame(client.agent_frame)
    assert decoded.snapshot["motion"] == second["motion"]


def test_agent_hover_reports_unit_object_and_none_with_strict_tooltip_owner():
    client = WowClient()
    client.login()

    client.set_hover_unit("Auctioneer Jaxon")
    assert client.publish_agent()["hover"] == {"kind": "unit", "name": "Auctioneer Jaxon"}

    client.set_hover_object("Mailbox")
    assert client.publish_agent()["hover"] == {"kind": "object", "name": "Mailbox"}

    client.set_hover_object("Mailbox", owner_ui_parent=False)
    assert client.publish_agent()["hover"] == {"kind": "none", "name": ""}

    client.clear_hover()
    assert client.publish_agent()["hover"] == {"kind": "none", "name": ""}


@pytest.mark.parametrize(
    ("name", "overlong"),
    [(b"x" * 49, True), ("é".encode() * 24, False), ("é".encode() * 25, True)],
    ids=["ascii-overlong", "multibyte-exactly-48-bytes", "multibyte-overlong"],
)
def test_agent_hover_enforces_48_utf8_bytes(name: bytes, overlong: bool):
    client = WowClient()
    client.login()
    client.set_hover_unit_bytes(name)
    if overlong:
        with pytest.raises(Exception, match="overlong name"):
            client.ns.Agent.Publish()
    else:
        value = client.publish_agent()
        assert value["hover"]["kind"] == "unit"
        assert value["hover"]["name"].encode("latin-1") == name


def test_agent_hover_shown_ui_parent_tooltip_with_nil_first_line_is_none():
    client = WowClient()
    client.login()
    client.set_hover_object("Mailbox")
    client.state.tooltipName = None
    assert client.publish_agent()["hover"] == {"kind": "none", "name": ""}


@pytest.mark.parametrize(
    "api",
    ["UnitExists", "UnitName", "GameTooltip", "GameTooltipTextLeft1", "UIParent", "MailFrame"],
)
def test_agent_hover_and_mail_apis_fail_loudly_when_missing(api: str):
    client = WowClient()
    client.login()
    client.lua.globals()[api] = None
    with pytest.raises(Exception, match=api):
        client.ns.Agent.Publish()


@pytest.mark.parametrize("open_state", [False, True])
def test_agent_reports_mail_frame_visibility(open_state: bool):
    client = WowClient()
    client.login()
    client.set_mail(open=open_state)
    assert client.publish_agent()["mailOpen"] is open_state


def test_schema6_executor_telemetry_survives_lua_capture_and_pixel_decoder():
    queue = {
        "format": 1,
        "queueId": "beta-normal-alliance-1760000000-telemetry",
        "generatedAt": 1759990000,
        "expiresAt": 1760000100,
        "house": "beta-normal-alliance",
        "character": "Tester",
        "armed": False,
        "actions": [{"seq": 1, "kind": "mail_collect", "maxMessages": 1}],
    }
    client = WowClient(faction="Alliance", realm="Classic Beta PvE 2", current_region=110, queue=queue)
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(1)
    client.set_frame_open("mailbox")
    client.update_agent()
    decoder = PixelDecoder()
    first = decoder.decode(client.render_agent_capture(), monotonic=1.0, window_identity="executor-wire")
    assert first.kind == "FRESH" and first.frame is not None
    assert first.frame.snapshot["schema"] == 6
    assert first.frame.snapshot["executor"]["availability"] == "valid"
    assert first.frame.snapshot["executor"]["clickReady"] is True

    client.click_executor()
    client.update_agent()
    second = decoder.decode(client.render_agent_capture(), monotonic=2.0, window_identity="executor-wire")
    assert second.kind == "FRESH" and second.frame is not None
    executor = second.frame.snapshot["executor"]
    assert executor["clickOrdinal"] == 1
    assert executor["lastClickSource"] == "button"
    assert executor["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "dry_run"}


@pytest.mark.parametrize(
    ("action", "field"),
    [("JUMP", "jump"), ("TURNRIGHT", "turnRight")],
)
def test_agent_rejects_unbound_new_motion_bindings(action: str, field: str):
    client = WowClient()
    client.login()
    client.state.bindings[action] = None
    with pytest.raises(Exception, match=field):
        client.ns.Agent.Publish()


def test_hud_visibility_hides_all_cells_stops_publication_and_bare_command_toggles():
    client = WowClient()
    client.set_canonical_plan()
    client.login()

    agent = client.ns.Agent
    surface = agent.Surface()
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False
    original_bytes = client.agent_frame
    original = decode_frame(original_bytes).snapshot
    original_boot = agent.Boot()
    original_count = int(agent.Snapshot().render.updateCount)
    old_update = surface.onUpdate
    geometry = (
        surface.scale,
        surface.width,
        surface.height,
        surface.point[1],
        surface.point[2].name,
        surface.point[3],
        surface.point[4],
        surface.point[5],
    )
    texture_count = client.texture_count

    client.slash("hud hide")
    assert client.lua.globals().CrowleyDB.settings.hudHidden is True
    assert agent.HudHidden() is True
    assert surface.IsShown(surface) is False
    assert surface.onUpdate is None
    assert client.visible_native_textures and not any(client.visible_native_textures)
    assert agent.FrameBytes() is None
    assert client._json_table(client.lua.globals().CrowleyDB.settings)["hudHidden"] is True
    assert "required for agent operation" in client.chat[-1]
    assert "does not pause the controller" in client.chat[-1]

    # Even a callback captured before SetScript(nil) cannot publish while hidden.
    old_update(surface, 1)
    assert int(agent.Snapshot().render.updateCount) == original_count
    with pytest.raises(Exception, match="agent_hud_hidden"):
        agent.Publish()

    client.slash("hud status")
    assert "telemetry HUD is hidden" in client.chat[-1]
    client.fire("AUCTION_HOUSE_SHOW")
    client.advance_with_timers(5)
    assert agent.HudHidden() is True
    assert surface.IsShown(surface) is False
    assert surface.onUpdate is None

    client.slash("hud")
    assert agent.HudHidden() is False
    assert agent.FrameBytes() is not None
    shown = decode_frame(client.agent_frame).snapshot
    assert shown["sequence"] > original["sequence"]
    assert shown["schema"] == original["schema"]
    assert agent.Boot() == original_boot
    assert surface.IsShown(surface) is True
    assert surface.onUpdate is not None
    assert all(client.visible_native_textures)
    assert client.texture_count == texture_count
    assert (
        surface.scale,
        surface.width,
        surface.height,
        surface.point[1],
        surface.point[2].name,
        surface.point[3],
        surface.point[4],
        surface.point[5],
    ) == geometry

    current_update_count = int(agent.Snapshot().render.updateCount)
    client.slash("hud show")
    assert int(agent.Snapshot().render.updateCount) == current_update_count
    assert surface.onUpdate is not None
    assert client.texture_count == texture_count
    client.update_agent(1)
    assert int(agent.Snapshot().render.updateCount) == current_update_count + 1

    client.slash("hud hide")
    client.slash("hud hide")
    assert agent.HudHidden() is True
    assert agent.FrameBytes() is None
    assert surface.onUpdate is None

    persisted = savedvars.loads(client.saved_variables())["CrowleyDB"]
    reloaded = WowClient(initial_savedvars=persisted)
    reloaded.login()
    assert reloaded.lua.globals().CrowleyDB.settings.hudHidden is True
    assert reloaded.ns.Agent.Surface() is None
    assert reloaded.ns.Agent.FrameBytes() is None


def test_persisted_hidden_hud_boot_has_no_surface_and_show_publishes_fresh_frame():
    client = WowClient(initial_savedvars={"settings": {"hudHidden": True}})
    client.login()

    assert client.lua.globals().CrowleyDB.settings.hudHidden is True
    assert client.ns.Agent.HudHidden() is True
    assert client.ns.Agent.Surface() is None
    assert client.ns.Agent.FrameBytes() is None
    assert client.texture_count == 0
    with pytest.raises(Exception, match="agent_hud_hidden"):
        client.ns.Agent.Publish()

    client.fire("AUCTION_HOUSE_SHOW")
    client.advance_with_timers(10)
    assert client.ns.Agent.Surface() is None

    client.slash("hud show")
    surface = client.ns.Agent.Surface()
    assert surface is not None and surface.IsShown(surface)
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False
    assert client.ns.Agent.HudHidden() is False
    assert client.ns.Agent.FrameBytes() is not None
    assert surface.onUpdate is not None
    assert client.texture_count == 2048


def test_hud_show_publication_failure_stays_hidden_and_hide_works_after_publish_failure():
    hidden = WowClient(initial_savedvars={"settings": {"hudHidden": True}})
    hidden.login()
    codec = hidden.lua.globals().C_EncodingUtil
    serialize = codec.SerializeJSON
    codec.SerializeJSON = hidden.lua.eval("function(_) error('forced publication failure') end")

    hidden.slash("hud show")
    assert "refused" in hidden.chat[-1]
    assert "publication_failed" in hidden.chat[-1]
    assert hidden.lua.globals().CrowleyDB.settings.hudHidden is True
    assert hidden.ns.Agent.HudHidden() is True
    assert hidden.ns.Agent.Surface() is None
    assert hidden.ns.Agent.FrameBytes() is None
    assert hidden.texture_count == 2048
    assert not any(hidden.visible_native_textures)

    codec.SerializeJSON = serialize
    hidden.slash("hud show")
    assert "telemetry HUD visible" in hidden.chat[-1]
    assert hidden.ns.Agent.FrameBytes() is not None

    visible = WowClient()
    visible.login()
    visible_codec = visible.lua.globals().C_EncodingUtil
    visible_codec.SerializeJSON = visible.lua.eval("function(_) error('forced publication failure') end")
    with pytest.raises(Exception, match="forced publication failure"):
        visible.ns.Agent.Publish()
    assert visible.ns.Agent.FrameBytes() is not None  # Previous bytes are retained until Hide.

    visible.slash("hud hide")
    assert "telemetry HUD hidden" in visible.chat[-1]
    assert visible.lua.globals().CrowleyDB.settings.hudHidden is True
    assert visible.ns.Agent.FrameBytes() is None
    assert visible.ns.Agent.Surface().onUpdate is None
    assert not any(visible.visible_native_textures)


@pytest.mark.parametrize("transaction", ["agent hold", "agent defer"])
def test_hud_visibility_mutation_refuses_while_native_lifecycle_owns_surface(transaction: str):
    client = WowClient()
    client.login()
    client.slash(transaction)
    assert "accepted" in client.chat[-1]
    before_marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    before_lifecycle = client.ns.Agent.Lifecycle()
    before_native = client._json_table(client.ns.Agent.NativeState())
    before_fence = client.ns.Scanner.NativeConstructionFence()

    client.slash("hud hide")
    assert "native_lifecycle_owns_surface" in client.chat[-1]
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False
    assert client.ns.Agent.HudHidden() is False
    assert client.ns.Agent.Surface().IsShown(client.ns.Agent.Surface()) is True
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before_marker
    assert client.ns.Agent.Lifecycle() == before_lifecycle
    assert client._json_table(client.ns.Agent.NativeState()) == before_native
    assert client.ns.Scanner.NativeConstructionFence() == before_fence
    assert client.reload_calls == 0


@pytest.mark.parametrize("persisted_hidden", [False, True], ids=["hide-existing", "hidden-boot"])
@pytest.mark.parametrize("entry", ["agent hold", "agent defer"])
def test_hidden_hud_rejects_native_entry_before_mutation_and_show_restores_entry(
    persisted_hidden: bool, entry: str
):
    saved = {"settings": {"hudHidden": True}} if persisted_hidden else None
    client = WowClient(initial_savedvars=saved)
    client.login()
    if not persisted_hidden:
        client.slash("hud hide")

    agent = client.ns.Agent
    assert agent.HudHidden() is True
    before_lifecycle = agent.Lifecycle()
    before_native = client._json_table(agent.NativeState())
    before_marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    before_fence = client.ns.Scanner.NativeConstructionFence()
    before_timers = queued_timer_count(client)
    before_reload_calls = client.reload_calls

    client.slash(entry)
    assert "agent_hud_hidden" in client.chat[-1]
    assert agent.Lifecycle() == before_lifecycle
    assert client._json_table(agent.NativeState()) == before_native
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) == before_marker
    assert client.ns.Scanner.NativeConstructionFence() == before_fence
    assert queued_timer_count(client) == before_timers
    assert client.reload_calls == before_reload_calls
    assert agent.HudHidden() is True
    assert client.lua.globals().CrowleyDB.settings.hudHidden is True
    surface = agent.Surface()
    assert surface is None or surface.IsShown(surface) is False

    client.slash("hud show")
    assert "telemetry HUD visible" in client.chat[-1]
    assert agent.HudHidden() is False
    assert agent.FrameBytes() is not None
    assert agent.Surface().IsShown(agent.Surface()) is True

    client.slash(entry)
    assert "accepted" in client.chat[-1]
    if entry == "agent hold":
        assert client._json_table(agent.NativeState()) == {"phase": "held", "ordinal": 0}
        assert client.lua.globals().CrowleyDB.nativeConstruction is None
        assert client.ns.Scanner.NativeConstructionFence() is False
    else:
        assert client.lua.globals().CrowleyDB.nativeConstruction.state == "armed"
        assert client.ns.Scanner.NativeConstructionFence() is True


def test_hidden_hud_rejects_all_public_native_lifecycle_apis_without_side_effects():
    client = WowClient(initial_savedvars={"settings": {"hudHidden": True}})
    client.login()
    agent = client.ns.Agent
    before_lifecycle = agent.Lifecycle()
    before_native = client._json_table(agent.NativeState())
    before_marker = client._json_table(client.lua.globals().CrowleyDB.nativeConstruction)
    before_fence = client.ns.Scanner.NativeConstructionFence()
    before_timers = queued_timer_count(client)

    for name in (
        "AdmitRelease",
        "BeginRecovery",
        "Construct",
        "Restore",
        "Finalize",
        "Hold",
        "Measure",
        "Resume",
    ):
        assert getattr(agent, name)() == (False, "agent_hud_hidden"), name

    assert agent.Lifecycle() == before_lifecycle == "ordinary"
    assert client._json_table(agent.NativeState()) == before_native
    assert before_marker is None
    assert client._json_table(client.lua.globals().CrowleyDB.nativeConstruction) is before_marker
    assert before_fence is False
    assert client.ns.Scanner.NativeConstructionFence() == before_fence
    assert queued_timer_count(client) == before_timers
    assert client.reload_calls == 0
    assert agent.FrameBytes() is None
    assert agent.HudHidden() is True
    assert client.lua.globals().CrowleyDB.settings.hudHidden is True

    client.slash("hud show")
    assert "telemetry HUD visible" in client.chat[-1]
    assert agent.FrameBytes() is not None


def test_hud_command_rejects_extra_or_unknown_arguments():
    client = WowClient()
    client.login()
    surface = client.ns.Agent.Surface()

    client.slash("hud hide extra")
    assert "usage: /crowley hud" in client.chat[-2]
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False
    assert surface.IsShown(surface) is True

    client.slash("hud toggle")
    assert "usage: /crowley hud" in client.chat[-2]
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False

    client.slash("hud unknown")
    assert "usage: /crowley hud" in client.chat[-2]
    assert client.lua.globals().CrowleyDB.settings.hudHidden is False


def test_agent_publishes_strict_auctioneer_and_ctrl_f12_native_facts():
    client = WowClient()
    client.set_canonical_plan()
    client.login()

    absent = client.publish_agent()["auctioneer"]
    assert absent == {
        "targetPresent": False,
        "targetName": "",
        "rangeAvailable": False,
        "withinTradeRange": False,
        "bindingAvailable": True,
        "ctrlF12Action": "",
        "interactionMode": "any",
    }

    client.set_auctioneer_state(
        target_name="Auctioneer Jaxon",
        range_available=True,
        within_trade_range=True,
        ctrl_f12_action="INTERACTTARGET",
    )
    value = client.publish_agent()
    assert client.state.interactDistanceCalls[1] == 2
    assert value["schema"] == 6
    assert value["auctioneer"] == {
        "targetPresent": True,
        "targetName": "Auctioneer Jaxon",
        "rangeAvailable": True,
        "withinTradeRange": True,
        "bindingAvailable": True,
        "ctrlF12Action": "INTERACTTARGET",
        "interactionMode": "any",
    }
    assert value["blockingUI"] == {
        "staticPopupAvailable": True,
        "staticPopupShown": False,
        "gameMenuAvailable": True,
        "gameMenuShown": False,
    }
    decoded = decode_frame(client.agent_frame).snapshot.value
    assert decoded["auctioneer"] == value["auctioneer"]
    assert decoded["blockingUI"] == value["blockingUI"]

    for cvar, mode in (("0", "disabled"), ("1", "gamepad"), ("2", "keyboard"), ("3", "any")):
        client.set_soft_target_interact(cvar)
        assert client.publish_agent()["auctioneer"]["interactionMode"] == mode
    client.set_soft_target_interact("invalid")
    assert client.publish_agent()["auctioneer"]["interactionMode"] == "unavailable"
    client.set_soft_target_interact(None)
    assert client.publish_agent()["auctioneer"]["interactionMode"] == "unavailable"
    client.set_soft_target_interact("3")

    client.set_blocking_ui_state(static_popup_shown=True)
    assert client.publish_agent()["blockingUI"]["staticPopupShown"] is True
    client.set_blocking_ui_state(game_menu_shown=True)
    assert client.publish_agent()["blockingUI"]["gameMenuShown"] is True
    client.set_blocking_ui_state(static_popup_available=False)
    unavailable_blocker = client.publish_agent()["blockingUI"]
    assert unavailable_blocker["staticPopupAvailable"] is False
    assert unavailable_blocker["staticPopupShown"] is False

    client.set_auctioneer_state(
        target_name="Auctioneer Jaxon",
        range_available=False,
        binding_available=False,
    )
    unavailable = client.publish_agent()["auctioneer"]
    assert client.state.interactDistanceCalls[2] == 2
    assert unavailable["targetPresent"] is True
    assert unavailable["rangeAvailable"] is False
    assert unavailable["withinTradeRange"] is False
    assert unavailable["bindingAvailable"] is False
    assert unavailable["ctrlF12Action"] == ""
    assert unavailable["interactionMode"] == "any"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda client: setattr(client.state, "positionX", None),
        lambda client: setattr(client.state, "facing", float("nan")),
        lambda client: setattr(client.state, "combat", None),
        lambda client: client.state.bindings.__setitem__("MOVEFORWARD", "not a binding"),
    ],
    ids=["missing-position", "nonfinite-facing", "missing-combat", "malformed-binding"],
)
def test_agent_rejects_missing_or_malformed_native_motion(mutation):
    client = WowClient()
    client.set_canonical_plan()
    client.login()
    mutation(client)
    with pytest.raises(Exception, match="movement telemetry"):
        client.ns.Agent.Publish()


def test_probe_records_absent_optional_namespaces_as_false():
    client = WowClient(game_rules=False)
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["namespaces"]["C_RealmInfo"] is False
    assert probe["namespaces"]["C_GameRules"] is False


def test_probe_records_trade_api_surface_without_calling_trade_apis():
    namespace_functions = {
        "C_Container": ["GetContainerItemInfo", "UseContainerItem"],
        "C_TradeSkillUI": ["GetRecipeInfo"],
        "C_Spell": [],
        "C_Mail": False,
        "C_MerchantFrame": ["GetBuybackItemInfo"],
        "C_Bank": False,
        "C_PlayerInfo": ["GetPlayerMythicPlusRatingSummary"],
        "C_CurrencyInfo": [],
        "C_Timer": ["After"],
        "C_SpellBook": False,
        "C_Professions": ["GetProfessionInfo"],
    }
    trade_global_names = [
        "GetMoney",
        "GetCoinTextureString",
        "GetContainerNumSlots",
        "GetContainerItemInfo",
        "GetContainerItemLink",
        "UseContainerItem",
        "PickupContainerItem",
        "GetInboxNumItems",
        "GetInboxHeaderInfo",
        "GetInboxItem",
        "GetInboxItemLink",
        "TakeInboxItem",
        "TakeInboxMoney",
        "AutoLootMailItem",
        "CheckInbox",
        "GetNumSkillLines",
        "GetSkillLineInfo",
        "GetProfessions",
        "GetProfessionInfo",
        "GetTradeSkillLine",
        "GetNumTradeSkills",
        "GetTradeSkillInfo",
        "GetTradeSkillNumReagents",
        "GetTradeSkillReagentInfo",
        "GetTradeSkillCooldown",
        "DoTradeSkill",
        "CloseTradeSkill",
        "GetSpellCooldown",
        "GetMerchantNumItems",
        "GetMerchantItemInfo",
        "GetMerchantItemID",
        "BuyMerchantItem",
        "CanMerchantRepair",
        "GetNumTrainerServices",
        "GetTrainerServiceInfo",
        "GetTrainerServiceSkillReq",
        "GetTrainerServiceCost",
        "BuyTrainerService",
        "IsUsableSpell",
        "InCombatLockdown",
    ]
    trade_globals = {
        name: name in {"GetMoney", "GetContainerItemInfo", "BuyMerchantItem"} for name in trade_global_names
    }
    frame_names = [
        "AuctionHouseFrame",
        "MerchantFrame",
        "MailFrame",
        "TradeSkillFrame",
        "ClassTrainerFrame",
        "ProfessionsFrame",
    ]
    trade_frames = {name: name in {"AuctionHouseFrame", "MailFrame"} for name in frame_names}

    client = WowClient(
        target=non_beta_target(),
        realm="Normal",
        current_region=1,
        trade_namespaces=namespace_functions,
        trade_globals=trade_globals,
        trade_frames=trade_frames,
    )
    client.login()
    calls_before_probe = client.trade_calls
    probe = client.probe()

    assert set(probe["namespaces"]) >= {
        "C_Container",
        "C_TradeSkillUI",
        "C_Spell",
        "C_Mail",
        "C_MerchantFrame",
        "C_Bank",
        "C_PlayerInfo",
        "C_CurrencyInfo",
        "C_Timer",
        "C_SpellBook",
        "C_Professions",
    }
    for name, functions in namespace_functions.items():
        assert probe["namespaces"][name] == (sorted(functions) if functions is not False else False)
    assert probe["tradeGlobals"] == trade_globals
    assert probe["tradeFrames"] == trade_frames
    assert probe["region"] == 1
    assert probe["realmName"] == "Normal"
    assert probe["motion"]["apiPresence"]["GetBindingKey"] is True
    assert client.trade_calls == calls_before_probe


def test_faction_override_bypasses_unsupported_native_faction_and_full_sweep_is_strict():
    client = WowClient(target=non_beta_target(), current_region=1, realm="Normal", faction="Unrecognized")
    raw = run_sweep(client, settings={"faction": "alliance"})
    parsed = validate_v2_envelope(envelope(raw))

    assert raw["house"]["key"] == "us-normal-alliance"
    assert raw["house"]["source"] == "override: faction"
    assert raw["context"]["playerFaction"] == "Unrecognized"
    assert parsed["decoded"]["observations"]["2589"]["coverage"] == "full"


def test_probe_captures_snapshot_error_with_nil_native_faction_and_override():
    client = WowClient(target=non_beta_target(), current_region=1, realm="Normal", faction=None)
    client.login()
    client.lua.globals().CrowleyDB.settings.faction = "alliance"
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["context"] is False
    assert "native player faction" in probe["contextError"]
    assert probe["house"]["key"] == "us-normal-alliance"
    assert probe["house"]["source"] == "override: faction"
    assert "interface 16001 build 70450" in client.chat[-1]


def test_nil_native_faction_override_refuses_scan_before_queries_or_saved_scan():
    client = WowClient(target=non_beta_target(), current_region=1, realm="Normal", faction=None)
    client.login()
    client.lua.globals().CrowleyDB.settings.faction = "alliance"
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen Cloth", 1, 100)])
    client.fire("AUCTION_HOUSE_SHOW")
    with pytest.raises(Exception, match="native player faction"):
        client.slash("scan")

    assert client.state.queryCalls == 0
    assert not list(client.lua.globals().CrowleyDB.scans.items())


def test_probe_keeps_native_facts_when_overrides_supply_unmapped_house_fields():
    client = WowClient(realm="Classic Beta PvE 2", current_region_name="")
    client.login()
    client.lua.globals().CrowleyDB.settings.region = "us"
    client.lua.globals().CrowleyDB.settings.ruleset = "normal"
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["regionName"] == ""
    assert probe["context"]["realm"] == "Classic Beta PvE 2"
    assert probe["house"]["key"] == "us-normal-horde"
    assert probe["house"]["source"] == "override: region,ruleset"


def test_beta_native_facts_are_attributed_to_the_verified_beta_house():
    client = WowClient(realm="Classic Beta PvE 2", current_region=110, current_region_name="")
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["region"] == 110
    assert probe["regionName"] == ""
    assert probe["realmName"] == "Classic Beta PvE 2"
    assert probe["house"]["key"] == "beta-normal-horde"
    assert probe["house"]["source"] == "auto"
    assert "attributionError" not in probe


def test_beta_auto_attribution_roundtrips_through_a_strict_sweep():
    raw = run_sweep(WowClient(realm="Classic Beta PvE 2", current_region=110, current_region_name=""))

    assert validate_v2_envelope(envelope(raw))["house"]["key"] == "beta-normal-horde"


def test_selected_non_beta_target_binds_native_region_and_realm():
    target = non_beta_target()
    client = WowClient(
        target=target,
        current_region=target.native_region_id,
        current_region_name="misleading",
        realm=target.realm,
    )
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"]["key"] == "us-normal-horde"
    assert probe["house"]["source"] == "auto"


@pytest.mark.parametrize(
    ("current_region", "realm", "message"),
    [(2, "PvP", "native region id"), (1, "Normal", "native realm")],
)
def test_selected_non_beta_target_rejects_native_identity_drift(current_region, realm, message):
    target = non_beta_target(ruleset="pvp", realm="PvP")
    client = WowClient(target=target, current_region=current_region, realm=realm)
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"] is False
    assert message in probe["attributionError"]


@pytest.mark.parametrize(
    ("realm", "ruleset"),
    [("Normal", "normal"), ("PvP", "pvp"), ("RP", "rp"), ("Hardcore", "hardcore")],
)
def test_standard_realm_labels_map_exactly_to_rulesets(realm, ruleset):
    client = WowClient(target=non_beta_target(ruleset=ruleset, realm=realm), current_region=1, realm=realm)
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"]["key"] == f"us-{ruleset}-horde"


def test_unknown_native_region_id_is_rejected_without_a_guess():
    client = WowClient(current_region=999, current_region_name="US", realm="Normal")
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"] is False
    assert "native region id 999 does not match target" in probe["attributionError"]


def test_unverified_beta_realm_is_rejected():
    client = WowClient(current_region=110, current_region_name="", realm="Classic Beta PvE 1")
    client.login()
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"] is False
    assert "native realm" in probe["attributionError"]


def test_region_override_bypasses_unknown_native_region_id():
    client = WowClient(current_region=999, current_region_name="", realm="Normal")
    client.login()
    client.slash("set region us")
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"]["key"] == "us-normal-horde"
    assert probe["house"]["source"] == "override: region"


def test_region_override_unknown_id_roundtrips_through_a_strict_sweep():
    raw = run_sweep(
        WowClient(current_region=999, current_region_name="", realm="Normal"),
        settings={"region": "us"},
    )

    assert validate_v2_envelope(envelope(raw))["house"]["key"] == "us-normal-horde"


def test_ruleset_override_does_not_read_an_unrecognized_native_realm():
    client = WowClient(current_region=110, current_region_name="", realm="Unverified realm")
    client.login()
    client.slash("set ruleset normal")
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["context"]["realm"] == "Unverified realm"
    assert probe["house"]["key"] == "beta-normal-horde"
    assert probe["house"]["source"] == "override: ruleset"


def test_ruleset_override_unverified_beta_realm_roundtrips_through_a_strict_sweep():
    raw = run_sweep(
        WowClient(current_region=110, current_region_name="", realm="Classic Beta PvE 1"),
        settings={"ruleset": "normal"},
    )

    assert validate_v2_envelope(envelope(raw))["house"]["key"] == "beta-normal-horde"


def test_invalid_persisted_house_setting_is_rejected_loudly():
    client = WowClient()
    client.login()
    client.lua.globals().CrowleyDB.settings.region = "mars"
    client.slash("probe")
    probe = savedvars.loads(client.saved_variables())["CrowleyDB"]["probe"]

    assert probe["house"] is False
    assert "invalid saved override region" in probe["attributionError"]


def test_sweep_timeout_keeps_active_item_partial_and_unqueried_tail():
    client = WowClient()
    client.login()
    ids = load_catalog().scan_plan()["itemIDs"]
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_metadata_pending(ids)
    client.state.metadataDelay = 601
    client.set_event_behavior(delay=601)
    client.set_owned_behavior(emit=False)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance_with_timers(600)
    payload = decoded_payload(client)
    observations = payload["observations"]
    assert any(
        observation["reasonCode"] == "sweep_timeout" and observation["query"]["kind"] == "queried"
        for observation in observations.values()
    )
    assert observations[str(ids[-1])]["coverage"] == "unqueried"
    assert observations[str(ids[-1])]["reasonCode"] == "sweep_timeout"
    assert payload["owned"]["status"] == "unqueried"
    assert payload["owned"]["query"] == {"kind": "unqueried"}
    assert payload["owned"]["reasonCode"] == "not_started"


def test_invalid_house_setting_keeps_delayed_sweep_and_settings_unchanged():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(2589, "Linen", 1, 100)])
    client.set_event_behavior(delay=5)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.ns.Scanner.InProgress()
    assert client.lua.globals().CrowleyDB.settings.faction is None
    client.slash("set faction goblins")
    assert client.ns.Scanner.InProgress()
    assert client.lua.globals().CrowleyDB.settings.faction is None
    client.advance(5)
    client.run_timers()
    finish_canonical(client)
    observation = decoded_payload(client)["observations"]["2589"]
    assert observation["coverage"] == "full"
    assert observation["reasonCode"] == "ok"


def test_owned_timeout_starts_before_throttled_send_and_finalizes():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.set_throttled()
    client.focus_item(118)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    assert not list(client.lua.globals().CrowleyDB.scans.items())
    client.advance(30)
    client.run_timers()
    assert client.ns.Scanner.InProgress()
    client.fire("PLAYER_LOGOUT")
    client.run_timers()
    assert decoded_payload(client)["observations"]["118"]["reasonCode"] == "query_timeout"


def test_rolling_message_window_never_exceeds_100_sends():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    for _ in range(1000):
        client.run_timers()
        if list(client.lua.globals().CrowleyDB.scans.items()):
            break
        client.advance(1)
    timestamps = [entry["at"] for entry in client.send_log]
    assert timestamps
    assert all(sum(now - 60 < then <= now for then in timestamps) <= 100 for now in timestamps)


def test_capped_sweeps_rotate_after_last_attempted_item():
    client = WowClient()
    client.login()
    ids = [2589, 2592, 2770]
    client.set_canonical_plan()
    client.set_auctions([Auction(itemID=item_id, name="Item", count=1, buyout=100) for item_id in ids])
    client.set_event_behavior(delay=601)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.advance(600)
    client.run_timers()
    first_id = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))["id"]
    client.advance(300)
    client.set_event_behavior(delay=601)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan force")
    client.advance(600)
    client.run_timers()
    scans = list(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values())
    assert len(scans) == 2
    second = next(scan for scan in scans if scan["id"] != first_id)
    second_payload = json.loads(zlib.decompress(base64.b64decode(second["payload"]), -15))
    assert second_payload["observations"]["159"]["query"]["kind"] == "queried"


def test_owned_partial_event_waits_for_full_completion():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions([])
    client.set_owned([Auction(2589, "Linen", 1, 100, status="sold", auctionID=77)], complete=False)
    client.focus_item(2589)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    advance_to_owned_query(client)
    assert not list(client.lua.globals().CrowleyDB.scans.items())
    client.state.ownedFull = True
    client.fire("OWNED_AUCTIONS_UPDATED")
    assert decoded_payload(client)["owned"]["status"] == "full"
    assert "77" in decoded_payload(client)["owned"]["rows"]


def test_six_pending_sweeps_pause_without_eviction_until_ack_reload():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    for index in range(6):
        client.set_auctions([Auction(2589, "Linen Cloth", 1, 100 + index)])
        client.fire("AUCTION_HOUSE_SHOW")
        client.slash("scan")
        client.run_timers()
        if index < 5:
            client.advance(900)
    assert len(list(client.lua.globals().CrowleyDB.scans.items())) == 6
    client.slash("scan force")
    assert "storage blocked" in client.chat[-1]
    ids = sorted(
        (scan_id for scan_id, _ in client.lua.globals().CrowleyDB.scans.items()),
        key=lambda scan_id: scan_id.encode(),
    )
    client.lua.globals().CROWLEY_DATA.ackedScans = client._to_lua(ids)
    client.login()
    assert not list(client.lua.globals().CrowleyDB.scans.items())


def test_agent_chat_readback_requires_all_real_editbox_methods_and_bounds_text():
    client = WowClient()
    client.login()
    client.set_chat_edit(active=True, focused=True, text="/crowley status")
    edit = client.state.chatEdit
    original = edit.IsShown
    edit.IsShown = None
    try:
        assert client.publish_agent()["chatEdit"] == {"active": False, "focused": False, "text": ""}
    finally:
        edit.IsShown = original
    client.set_chat_edit(active=True, focused=True, text="x" * 129)
    assert client.publish_agent()["chatEdit"] == {"active": False, "focused": False, "text": ""}


def test_agent_chat_readback_uses_utf8_byte_boundary_at_128_bytes():
    client = WowClient()
    client.login()
    client.set_chat_edit_bytes(active=True, focused=True, text=(b"a" * 126) + "é".encode())
    assert client.publish_agent()["chatEdit"]["active"] is True
    client.set_chat_edit_bytes(active=True, focused=True, text=(b"a" * 127) + "é".encode())
    assert client.publish_agent()["chatEdit"] == {"active": False, "focused": False, "text": ""}


def test_reserved_pre_logout_fence_refuses_active_scan_and_wins_before_due_callback():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    armed, reason = client.ns.Scanner.PreLogoutFence()
    assert armed is False and reason == "scan_active"
    assert client.publish_agent()["scanner"]["scheduleFence"]["state"] == "refused"

    idle = WowClient()
    idle.login()
    idle.set_canonical_plan()
    idle.fire("AUCTION_HOUSE_SHOW")
    armed, reason = idle.ns.Scanner.PreLogoutFence()
    assert armed is True and reason is None
    idle.run_timers()
    assert not idle.ns.Scanner.InProgress()
    idle_frame = idle.publish_agent()
    assert idle_frame["scanner"]["scheduleFence"] == {
        "state": "armed",
        "reason": "",
        "command": "/crowley session fence",
    }
    assert idle_frame["scanner"]["phase"] in {"ready", "scheduled"}
    assert idle_frame["pending"]["blocked"] is False
    assert idle_frame["pending"]["reason"] == ""
    decoded = decode_frame(idle.agent_frame, plan_item_ids=load_catalog().scan_plan()["itemIDs"])
    assert decoded.snapshot["scanner"]["scheduleFence"] == {
        "state": "armed",
        "reason": "",
        "command": "/crowley session fence",
    }


def test_agent_hold_measure_resume_has_exact_update_window_and_no_gc_mutation():
    client = WowClient()
    client.set_lua_heap(values=[42])
    client.login()
    assert client.snapshot_agent()["render"]["luaHeap"] == {
        "method": "collectgarbage_count",
        "available": True,
        "beforeKiB": 42,
        "afterKiB": 42,
        "netKiB": 0,
    }
    client.slash("agent hold")
    held = client.snapshot_agent()["render"]
    assert (held["nativePhase"], held["nativeOrdinal"]) == ("held", 0)
    before = held["updateCount"]
    client.slash("agent measure")
    assert client.snapshot_agent()["render"]["nativeOrdinal"] == 1
    for _ in range(98):
        client.advance_with_timers(1)
    assert client.snapshot_agent()["render"]["nativePhase"] == "updates"
    assert client.snapshot_agent()["render"]["nativeOrdinal"] == 99
    client.advance_with_timers(1)
    final = client.snapshot_agent()["render"]
    assert (final["nativePhase"], final["nativeOrdinal"]) == ("held", 100)
    assert final["updateCount"] == before + 100
    client.slash("agent resume")
    resumed = client.snapshot_agent()["render"]
    assert (resumed["nativePhase"], resumed["nativeOrdinal"]) == ("normal", 0)
    assert client.gc_mutations == 0


@pytest.mark.parametrize("available", [False, True])
def test_agent_lua_heap_unavailable_or_constant_is_honest(available):
    client = WowClient()
    client.set_lua_heap(available=available, values=[7])
    client.login()
    heap = client.snapshot_agent()["render"]["luaHeap"]
    if available:
        assert heap == {
            "method": "collectgarbage_count",
            "available": True,
            "beforeKiB": 7,
            "afterKiB": 7,
            "netKiB": 0,
        }
    else:
        assert heap == {
            "method": "collectgarbage_count",
            "available": False,
            "beforeKiB": 0,
            "afterKiB": 0,
            "netKiB": 0,
        }
    assert client.gc_mutations == 0


@pytest.mark.parametrize(
    "sample", [math.inf, -math.inf, math.nan], ids=["positive-inf", "negative-inf", "nan"]
)
def test_agent_lua_heap_rejects_nonfinite_samples(sample):
    client = WowClient()
    client.set_lua_heap(values=[sample])
    client.login()
    heap = client.publish_agent()["render"]["luaHeap"]
    assert heap == {
        "method": "collectgarbage_count",
        "available": False,
        "beforeKiB": 0,
        "afterKiB": 0,
        "netKiB": 0,
    }
    assert client.gc_mutations == 0


def test_fence_schema_is_always_present_and_underscore_alias_is_not_reserved():
    client = WowClient()
    client.login()
    inactive = client.snapshot_agent()["scanner"]["scheduleFence"]
    assert inactive == {"state": "inactive", "reason": "", "command": "/crowley session fence"}
    client.slash("__session_fence")
    assert client.snapshot_agent()["scanner"]["scheduleFence"]["state"] == "inactive"
    client.slash("session fence")
    assert client.snapshot_agent()["scanner"]["scheduleFence"] == {
        "state": "armed",
        "reason": "",
        "command": "/crowley session fence",
    }


def test_canonical_gate_accepts_shipped_plan_and_rejects_short_plan_before_start():
    client = WowClient()
    client.login()
    client.set_plan([2589])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.state.queryCalls == 0
    assert "plan_invalid" in client.chat[-1]
    client.set_canonical_plan()
    assert client.publish_agent()["plan"] == {
        "hash": load_catalog().scan_plan()["planHash"],
        "count": 428,
        "cadenceSeconds": 900,
        "sweepCapSeconds": 600,
        "initialPriceLevels": 20,
    }


def test_canonical_gate_rejects_changed_same_length_body_with_forged_hash():
    client = WowClient()
    client.login()
    source = load_catalog().scan_plan()
    body = json.loads(source["planJSON"])
    body["itemIDs"][0] = 119
    body["pools"]["core"][0] = 119
    forged = dict(source)
    forged["itemIDs"] = list(body["itemIDs"])
    forged["pools"] = body["pools"]
    forged["planJSON"] = json.dumps(body, sort_keys=True, separators=(",", ":"))
    # Keep the shipped hash to model a forged canonical claim; the decoded
    # body must still be checked against the source catalog identity.
    client.lua.globals().CROWLEY_DATA.scanPlan = client._to_lua(forged)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.snapshot_agent()["pending"]["reason"] == "plan_invalid"
    assert client.state.queryCalls == 0


def test_canonical_gate_rejects_top_level_body_disagreement():
    client = WowClient()
    client.login()
    source = load_catalog().scan_plan()
    mismatch = dict(source)
    mismatch["targetClientBuild"] = "1.60.1.1"
    client.lua.globals().CROWLEY_DATA.scanPlan = client._to_lua(mismatch)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.snapshot_agent()["pending"]["reason"] == "plan_invalid"
    assert client.state.queryCalls == 0


def test_canonical_gate_rejects_previous_client_build_even_with_matching_body_hash():
    client = WowClient()
    client.login()
    source = load_catalog().scan_plan()
    body = json.loads(source["planJSON"])
    body["targetClientBuild"] = "1.60.1.70374"
    plan_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    forged = dict(source)
    forged["targetClientBuild"] = "1.60.1.70374"
    forged["planJSON"] = plan_json
    forged["planHash"] = hashlib.sha256(plan_json.encode()).hexdigest()
    client.lua.globals().CROWLEY_DATA.scanPlan = client._to_lua(forged)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.snapshot_agent()["pending"]["reason"] == "plan_invalid"
    assert client.state.queryCalls == 0


@pytest.mark.parametrize(
    ("version", "build", "interface"),
    [
        ("1.60.1", "70374", 16001),
        ("1.60.1", "70425", 16001),
        ("9.9.9", "70450", 16001),
        ("1.60.1", "unknown", 16001),
        ("1.60.1", "70450", 16000),
    ],
    ids=["old-build", "previous-client-build", "unknown-version", "unknown-build", "unknown-interface"],
)
def test_canonical_gate_rejects_runtime_identity_before_schedule_or_start(
    version: str, build: str, interface: int
):
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    source = load_catalog().scan_plan()
    client.lua.globals().GetBuildInfo = client.lua.eval(
        f'function() return "{version}", "{build}", "Oct 1 2026", {interface} end'
    )

    # The plan remains the exact canonical 70450 plan; only the runtime identity
    # is invalid.  Schedule must reject it before creating a timer, and a direct
    # start must reject it before creating a sweep or sending a query.
    loaded = client.lua.globals().CROWLEY_DATA.scanPlan
    assert loaded.planHash == source["planHash"]
    assert loaded.planJSON == source["planJSON"]
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.lua.globals()._nextTimerAt() is None

    client.slash("scan")
    assert "client_invalid" in client.chat[-1]
    assert client.lua.globals()._nextTimerAt() is None
    assert client.state.queryCalls == 0
    assert client.ns.Scanner.InProgress() is False
    status = client.snapshot_agent()
    assert status["scanner"]["phase"] == "blocked"
    assert status["pending"]["blocked"] is True
    assert status["pending"]["reason"] == "plan_invalid"
    published = client.publish_agent()
    assert published["scanner"]["phase"] == "blocked"
    assert published["pending"]["blocked"] is True
    assert published["pending"]["reason"] == "plan_invalid"
    decoded = decode_frame(client.agent_frame, plan_item_ids=source["itemIDs"])
    assert decoded.snapshot["scanner"]["phase"] == "blocked"
    assert decoded.snapshot["pending"]["blocked"] is True
    assert decoded.snapshot["pending"]["reason"] == "plan_invalid"


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda body: json.dumps(body, sort_keys=True, indent=2),
        lambda body: json.dumps({key: body[key] for key in reversed(list(body))}, separators=(",", ":")),
    ],
    ids=["pretty-printed", "alternate-key-order"],
)
def test_canonical_gate_rejects_equivalent_noncanonical_plan_bytes(rewrite):
    client = WowClient()
    client.login()
    source = load_catalog().scan_plan()
    forged = dict(source)
    body = json.loads(source["planJSON"])
    forged["planJSON"] = rewrite(body)
    # The body still decodes to the shipped catalog and retains its canonical
    # hash claim; only the wire bytes differ from the shipped planJSON.
    assert json.loads(forged["planJSON"]) == body
    assert forged["planHash"] == source["planHash"]
    client.lua.globals().CROWLEY_DATA.scanPlan = client._to_lua(forged)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.snapshot_agent()["pending"]["reason"] == "plan_invalid"
    assert client.state.queryCalls == 0


@pytest.mark.parametrize(
    ("ctm_mode", "expected_reason"),
    [("true", ""), ("error", "call_error"), ("nil", "nil_result"), ("wrong_type", "wrong_type")],
)
def test_native_capability_ctm_failures_are_explicit_and_still_decode(
    ctm_mode: str, expected_reason: str
) -> None:
    client = WowClient(ctm_mode=ctm_mode)
    client.set_canonical_plan()
    client.login()
    ctm_calls_before = int(client.state.ctmCalls or 0)
    speed_calls_before = int(client.state.speedCalls or 0)
    ground_calls_before = {
        field: int(client.state.groundCalls[field] or 0)
        for field in ("flying", "falling", "swimming", "submerged", "mounted", "taxi")
    }
    value = client.publish_agent()
    assert value["schema"] == 6
    assert value["motion"]["native"]["ctm"]["state"] in {"enabled", "unknown"}
    if ctm_mode == "true":
        assert value["motion"]["native"]["ctm"] == {
            "accessor": "C_CVar.GetCVarBool",
            "cvar": "autointeract",
            "state": "enabled",
            "reason": "",
        }
    else:
        assert value["motion"]["native"]["ctm"]["state"] == "unknown"
        assert value["motion"]["native"]["ctm"]["reason"] == expected_reason
    assert value["motion"]["native"]["ground"]["state"] == "grounded"
    decoded = decode_frame(client.agent_frame)
    assert (
        decoded.snapshot["motion"]["speed"] == decoded.snapshot["motion"]["native"]["speedTuple"]["current"]
    )
    assert client.state.speedCalls - speed_calls_before == 1
    assert int(client.state.ctmCalls or 0) - ctm_calls_before == 1
    for field in ("flying", "falling", "swimming", "submerged", "mounted", "taxi"):
        assert client.state.groundCalls[field] - ground_calls_before[field] == 1


def test_native_capability_ctm_missing_is_explicit_and_still_decodes() -> None:
    client = WowClient(ctm_missing=True)
    client.set_canonical_plan()
    client.login()
    ctm_calls_before = int(client.state.ctmCalls or 0)
    value = client.publish_agent()
    assert value["motion"]["native"]["ctm"] == {
        "accessor": "C_CVar.GetCVarBool",
        "cvar": "autointeract",
        "state": "unknown",
        "reason": "missing",
    }
    assert int(client.state.ctmCalls or 0) - ctm_calls_before == 0
    assert decode_frame(client.agent_frame).snapshot["motion"]["native"]["ctm"]["state"] == "unknown"


@pytest.mark.parametrize(
    ("field", "mode", "expected_state"),
    [
        ("flying", "true", "not_grounded"),
        ("falling", "error", "unknown"),
        ("swimming", "nil", "unknown"),
        ("submerged", "wrong_type", "unknown"),
        ("mounted", "missing", "unknown"),
        ("taxi", "true", "not_grounded"),
    ],
)
def test_native_ground_predicates_preserve_explicit_state(field: str, mode: str, expected_state: str) -> None:
    modes = {name: "false" for name in ("flying", "falling", "swimming", "submerged", "mounted", "taxi")}
    modes[field] = mode
    client = WowClient(ground_modes=modes)
    client.set_canonical_plan()
    client.login()
    speed_calls_before = int(client.state.speedCalls or 0)
    value = client.publish_agent()
    ground = value["motion"]["native"]["ground"]
    assert ground["state"] == expected_state
    assert ground[field] == ("true" if mode == "true" else "unknown")
    expected_reason = {
        "true": "",
        "error": "call_error",
        "nil": "nil_result",
        "wrong_type": "wrong_type",
        "missing": "missing",
    }[mode]
    assert ground["reason"] == expected_reason
    decode_frame(client.agent_frame)
    assert client.state.speedCalls - speed_calls_before == 1


@pytest.mark.parametrize(
    ("field", "mode"),
    [
        (field, mode)
        for field in ("flying", "falling", "swimming", "submerged", "mounted", "taxi")
        for mode in ("true", "error", "nil", "wrong_type", "missing")
    ],
)
def test_native_ground_matrix_has_exact_calls_arguments_and_first_failure_reason(
    field: str, mode: str
) -> None:
    fields = ("flying", "falling", "swimming", "submerged", "mounted", "taxi")
    modes = {name: "false" for name in fields}
    modes[field] = mode
    client = WowClient(ground_modes=modes)
    client.set_canonical_plan()
    client.login()
    before_speed = int(client.state.speedCalls or 0)
    before_calls = {name: int(client.state.groundCalls[name] or 0) for name in fields}
    before_args = {name: int(client.state.groundArgCounts[name] or 0) for name in fields}

    value = client.publish_agent()
    ground = value["motion"]["native"]["ground"]
    expected_reason = {
        "true": "",
        "error": "call_error",
        "nil": "nil_result",
        "wrong_type": "wrong_type",
        "missing": "missing",
    }[mode]
    assert ground[field] == ("true" if mode == "true" else "unknown")
    if mode == "true":
        assert ground["state"] == "not_grounded"
    else:
        assert ground["state"] == "unknown"
    assert ground["reason"] == expected_reason
    assert int(client.state.speedCalls or 0) - before_speed == 1
    for name in fields:
        assert int(client.state.groundCalls[name] or 0) - before_calls[name] == (
            0 if modes[name] == "missing" else 1
        )
        expected_args = 1 if name == "taxi" and modes[name] != "missing" else 0
        assert int(client.state.groundArgCounts[name] or 0) - before_args[name] == expected_args

    # The producer's call order is flying/falling/... but the wire reason is
    # the fixed falling/flying/... priority from the reviewed truth table.
    if field == "flying":
        client.state.groundModes.falling = "error"
        client.state.groundModes.flying = "nil"
        both_failed = client.publish_agent()["motion"]["native"]["ground"]
        assert both_failed["reason"] == "call_error"
