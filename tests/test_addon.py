"""Native scanner contract tests against the production Lua addon."""

import base64
import hashlib
import json
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from crowley_common.frames import decode_frame
from crowley_common.game_target import selected_target
from crowley_common.scanformat import ScanFormatError, validate_envelope
from crowley_common.watchlist import load_catalog
from crowley_scanner import savedvars
from lupa import lua51

from .wow_harness import Auction, WowClient


def non_beta_target(*, ruleset: str = "normal", realm: str = "Normal"):
    target = selected_target()
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


def decoded_payload(client: WowClient) -> dict:
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    return validate_envelope(envelope(raw))["decoded"]


def schedule_trace(client: WowClient) -> dict:
    return json.loads(
        client.lua.globals().C_EncodingUtil.SerializeJSON(client.lua.globals().CrowleyDB.scheduleTrace)
    )


def load_pure_lua_modules(*names: str) -> tuple[lua51.LuaRuntime, object]:
    """Load pure addon modules into a Lua runtime with no WoW globals."""

    lua = lua51.LuaRuntime(unpack_returned_tuples=True, encoding="latin-1")
    namespace = lua.table()
    for name in names:
        source = (
            Path(__file__).parents[1]
            / "packages"
            / "scanner"
            / "src"
            / "crowley_scanner"
            / "addon"
            / "Crowley"
            / f"{name}.lua"
        ).read_text()
        lua.execute(f"return function(...) {source}\nend")("Crowley", namespace)
    return lua, namespace


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


def test_pure_scanner_state_has_no_client_lifecycle_dependency():
    lua, namespace = load_pure_lua_modules("ScannerState")
    order = namespace.ScannerState.PlanOrder(lua.table_from([10, 20, 30]), 20)
    assert list(order.values()) == [30, 10, 20]
    rows = lua.table_from(
        [
            lua.table_from({"unitPrice": lua.table_from({"numerator": 100, "denominator": 1})}),
            lua.table_from({"unitPrice": lua.table_from({"numerator": 200, "denominator": 1})}),
            lua.table_from({"unitPrice": lua.table_from({"numerator": 300, "denominator": 1})}),
        ]
    )
    coverage = namespace.ScannerState.Coverage(rows, 2, True, None)
    assert coverage.coverage == "bounded"
    assert coverage.cutoff.numerator == 200 and coverage.higherPriceWitness.numerator == 300


def test_scanner_publishes_without_automation_addon():
    client = WowClient(load_automation=False)
    raw = run_sweep(client)
    assert raw["house"]["key"] == "beta-normal-horde"
    frame = decode_frame(client.scanner_frame, expected_kind="scanner")
    assert frame.snapshot["kind"] == "scanner"
    assert client.lua.globals().CrowleyAutomation is None


def test_automation_publishes_without_scanner_or_scan_plan():
    client = WowClient(load_scanner=False)
    client.lua.globals().AuctionHouseFrame = None
    client.login()
    snapshot = client.automation_snapshot()
    frame = decode_frame(client.automation_frame, expected_kind="automation")
    assert snapshot["kind"] == "automation"
    assert snapshot["context"]["character"] == "Tester"
    assert frame.snapshot["kind"] == "automation"
    assert client.lua.globals().CrowleyDB is None
    assert client.lua.globals().CROWLEY_DATA is None
    assert snapshot["ahOpen"] is False

    client.fire("AUCTION_HOUSE_SHOW")
    assert client.automation_snapshot()["ahOpen"] is True
    client.fire("AUCTION_HOUSE_CLOSED")
    assert client.automation_snapshot()["ahOpen"] is False


def test_automation_publishes_strict_auctioneer_binding_and_blocking_ui_facts():
    client = WowClient(load_scanner=False)
    client.login()

    absent = client.automation_snapshot()["auctioneer"]
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
    value = client.automation_snapshot()
    assert client.state.interactDistanceCalls[1] == 2
    assert value["auctioneer"] == {
        "targetPresent": True,
        "targetName": "Auctioneer Jaxon",
        "rangeAvailable": True,
        "withinTradeRange": True,
        "bindingAvailable": True,
        "ctrlF12Action": "INTERACTTARGET",
        "interactionMode": "any",
    }
    client.fire("PLAYER_ENTERING_WORLD")
    assert (
        decode_frame(client.automation_frame, expected_kind="automation").snapshot.value["auctioneer"]
        == value["auctioneer"]
    )

    for cvar, mode in (("0", "disabled"), ("1", "gamepad"), ("2", "keyboard"), ("3", "any")):
        client.set_soft_target_interact(cvar)
        assert client.automation_snapshot()["auctioneer"]["interactionMode"] == mode
    client.set_soft_target_interact("invalid")
    assert client.automation_snapshot()["auctioneer"]["interactionMode"] == "unavailable"
    client.set_soft_target_interact(None)
    assert client.automation_snapshot()["auctioneer"]["interactionMode"] == "unavailable"
    client.set_soft_target_interact("3")

    client.set_blocking_ui_state(static_popup_shown=True)
    assert client.automation_snapshot()["blockingUI"]["staticPopupShown"] is True
    client.set_blocking_ui_state(game_menu_shown=True)
    assert client.automation_snapshot()["blockingUI"]["gameMenuShown"] is True
    previous_frame = client.automation_frame
    client.set_blocking_ui_state(static_popup_available=False)
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.automation_frame == previous_frame
    client.set_blocking_ui_state(static_popup_available=True, game_menu_available=False)
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.automation_frame == previous_frame
    client.set_blocking_ui_state(game_menu_available=True)

    client.set_auctioneer_state(
        target_name="Auctioneer Jaxon",
        range_available=False,
        binding_available=False,
    )
    previous_frame = client.automation_frame
    assert client.state.interactDistanceCalls[2] == 2
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.automation_frame == previous_frame


def test_automation_chat_readback_requires_methods_and_bounds_utf8_bytes():
    client = WowClient(load_scanner=False)
    client.login()
    client.set_chat_edit(active=True, focused=True, text="/crowley status")
    edit = client.state.chatEdit
    client.fire("PLAYER_ENTERING_WORLD")
    previous_frame = client.automation_frame
    previous_chat = decode_frame(previous_frame, expected_kind="automation").snapshot.value["chatEdit"]
    assert previous_chat == {"active": True, "focused": True, "text": "/crowley status"}

    for name in ("IsShown", "HasFocus", "GetText"):
        original = getattr(edit, name)
        setattr(edit, name, None)
        client.fire("PLAYER_ENTERING_WORLD")
        assert client.automation_frame == previous_frame
        setattr(edit, name, original)

    for name, value in (
        ("IsShown", "function() return 1 end"),
        ("HasFocus", "function() return 'yes' end"),
        ("GetText", "function() return 1 end"),
    ):
        original = getattr(edit, name)
        setattr(edit, name, client.lua.eval(value))
        client.fire("PLAYER_ENTERING_WORLD")
        assert client.automation_frame == previous_frame
        setattr(edit, name, original)

    chat_util = client.lua.globals().ChatFrameUtil
    get_active_window = chat_util.GetActiveWindow
    chat_util.GetActiveWindow = None
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.automation_frame == previous_frame
    chat_util.GetActiveWindow = get_active_window

    client.set_chat_edit_bytes(active=True, focused=True, text=(b"a" * 127) + "é".encode())
    client.fire("PLAYER_ENTERING_WORLD")
    assert client.automation_frame == previous_frame
    assert (
        decode_frame(client.automation_frame, expected_kind="automation").snapshot.value["chatEdit"]
        == previous_chat
    )

    client.set_chat_edit(active=False, focused=False)
    client.fire("PLAYER_ENTERING_WORLD")
    inactive_chat = decode_frame(client.automation_frame, expected_kind="automation").snapshot.value[
        "chatEdit"
    ]
    assert inactive_chat == {"active": False, "focused": False, "text": ""}

    client.set_chat_edit_bytes(active=True, focused=True, text=(b"a" * 126) + "é".encode())
    client.fire("PLAYER_ENTERING_WORLD")
    recovered_chat = decode_frame(client.automation_frame, expected_kind="automation").snapshot.value[
        "chatEdit"
    ]
    assert recovered_chat["active"] is True
    assert recovered_chat["focused"] is True


@pytest.mark.parametrize(
    ("event", "reason"), [("AUCTION_HOUSE_CLOSED", "ah_closed"), ("PLAYER_LOGOUT", "logout")]
)
def test_scanner_pending_timer_is_cancelled_by_close_or_logout(event: str, reason: str):
    client = WowClient(load_automation=False)
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    assert client.lua.globals()._nextTimerAt() is not None

    client.fire(event)
    client.run_timers()

    trace = schedule_trace(client)
    assert any(item["type"] == "cancel" and item["reason"] == reason for item in trace["events"])
    assert any(item["type"] == "stale_token" for item in trace["events"])
    assert any(item["type"] == "ah_closed_discard" for item in trace["events"])
    assert client.state.queryCalls == 0
    assert client.ns.Scanner.InProgress() is False


def test_suspend_quiescence_waits_for_active_query_then_resumes_schedule():
    client = WowClient(load_automation=False)
    client.login()
    client.set_canonical_plan()
    client.set_auctions([Auction(itemID=2589, name="Linen Cloth", count=2, buyout=200)])
    client.set_owned_behavior(emit=False)
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    advance_to_owned_query(client)

    api = client.lua.globals().CrowleyScanner
    assert api.suspend("host-active") == (False, "scan_active")
    assert api.quiescent("host-active") is False
    assert api.resume("host-active") == (False, "scan_active")

    client.fire("OWNED_AUCTIONS_UPDATED")
    assert api.quiescent("host-active") is True
    assert api.resume("host-active") == (True, None)
    assert client.lua.globals()._nextTimerAt() is not None


def test_combined_host_holds_native_market_grant_until_real_scanner_finishes():
    catalog = load_catalog()
    item = next(item for item in catalog.items if item.status == "active")
    small_catalog = catalog.model_copy(update={"items": [item]})
    client = WowClient(combined_install=True, catalog=small_catalog)
    client.login()
    client.set_canonical_plan()
    client.set_event_behavior(delay=1)
    client.set_auctions([Auction(itemID=item.item_id, name=item.name, count=2, buyout=200)])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()
    assert client.ns.Scanner.InProgress() is True
    assert client.state.queryCalls == 1

    command = client.lua.globals().SlashCmdList.CAUTO
    batch_id = "a" * 64
    for value in (
        f"begin {batch_id} 0 0",
        "kind post_commodity",
        f"key 1 {item.item_id}",
        "arg quantity 1",
        "arg unitPriceCopper 100",
        "arg durationHours 12",
        "arg maxDepositCopper 0",
        "expires 1760000100",
        "seal",
    ):
        command(value)
    waiting = client.automation_snapshot()
    assert waiting["operation"]["batchId"] == batch_id
    assert waiting["operation"]["ready"] is False
    assert waiting["ownership"]["ahOwner"] == "scanner"
    assert waiting["ownership"]["scannerState"] == "active"
    assert client.state.ownedQueries == 0

    finish_canonical(client)
    client.fire("PLAYER_ENTERING_WORLD")
    held = client.automation_snapshot()
    assert client.ns.Scanner.InProgress() is False
    assert held["ownership"]["mode"] == "coordinated"
    assert held["ownership"]["ahOwner"] == "automation"
    assert held["ownership"]["scannerState"] == "quiescent"
    assert client.state.ownedQueries > 0

    command("clear")
    released = client.automation_snapshot()
    assert released["operation"]["batchId"] == ""
    assert released["ownership"]["ahOwner"] == "none"
    assert client.lua.globals().CrowleyScanner.quiescent("crowley-automation") is False
    trace = schedule_trace(client)
    assert any(
        event["type"] == "timer_create" and event.get("source") == "ownership_resume"
        for event in trace["events"]
    )


def test_scanner_auction_house_suspend_protocol_is_explicit_and_exclusive():
    client = WowClient(load_automation=False)
    client.login()
    client.set_canonical_plan()
    client.fire("AUCTION_HOUSE_SHOW")
    api = client.lua.globals().CrowleyScanner
    assert api.protocol == 1
    assert api.suspend("host-v1") == (True, None)
    assert api.quiescent("host-v1") is True
    assert api.suspend("other-host") == (False, "owned_by_other")
    client.slash("scan")
    assert client.state.queryCalls == 0
    assert api.resume("other-host") == (False, "not_owner")
    assert api.resume("host-v1") == (True, None)
    assert client.lua.globals()._nextTimerAt() is not None


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
    client.set_auctions(
        [Auction(2825, "Sword", 1, 500, itemLevel=40, itemSuffix=7, classID=2, auctionID=2825)]
    )
    client.set_key_warm([2825])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observation = decoded_payload(client)["observations"]["2825"]
    query = next(
        event
        for event in client.query_trace["events"]
        if event["type"] == "query_send" and event.get("operation") == "search" and event["itemID"] == 2825
    )
    row = next(iter(observation["rows"].values()))
    assert observation["coverage"] == "full"
    assert observation["rowCount"] == 1
    assert query["classID"] == 2
    assert query["equipment"] is True
    assert query["nativeMethod"] == "SendSellSearchQuery"
    assert query["nativeKey"] == {"itemID": 2825, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}
    assert row["rawItemKey"]["itemLevel"] == 40
    assert row["rawItemKey"]["itemSuffix"] == 7
    assert row["itemKey"] == "2825:s7:l40"
    equipment_methods = [entry for entry in client.query_methods if entry["itemID"] == 2825]
    assert equipment_methods[0]["itemLevel"] == 0


def test_missing_item_class_is_incomplete_and_does_not_stall_remaining_items():
    client = WowClient()
    client.login()
    client.set_canonical_plan()
    client.set_auctions(
        [
            Auction(2825, "Sword", 1, 500, itemLevel=40, classID=None, auctionID=2825),
            Auction(2555, "Linen", 27, 100, itemLevel=15, classID=9, auctionID=2555),
        ]
    )
    client.set_key_warm([2555])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    client.run_timers()

    observations = decoded_payload(client)["observations"]
    assert observations["2825"]["coverage"] == "incomplete"
    assert observations["2825"]["reasonCode"] == "metadata_unavailable"
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
        validate_envelope(envelope(raw))


def test_harness_serializes_empty_lua_tables_as_arrays():
    client = WowClient()
    value = client._to_lua({"empty": {}, "owners": {"Seller": True}})
    wire = json.loads(client.lua.globals().C_EncodingUtil.SerializeJSON(value))
    assert wire == {"empty": [], "owners": {"Seller": True}}


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


def test_addon_rejects_unknown_companion_data_key():
    client = WowClient()
    client.lua.globals().CROWLEY_DATA.shoppingLists = client._to_lua([])
    with pytest.raises(Exception, match="unknown or missing keys"):
        client.login()


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
    parsed = validate_envelope(envelope(raw))["decoded"]
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
    assert validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]["coverage"] == "full"
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
    parsed = validate_envelope(envelope(raw))
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    # window active while allowing all 480 initial queries to finish within the
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
    assert raw["numObservations"] == 480
    assert raw["numRows"] == 480
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
    client.state.fastCanonical = False
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    for _ in range(700):
        # Linen sits late enough in the plan that the real rolling send throttle would
        # time its query out; clear the throttle so this test isolates bounded coverage.
        client.lua.globals().CrowleyDB.querySendTimes = client.lua.table()
        client.state.sendLog = client.lua.table()
        client.run_timers()
        if list(client.lua.globals().CrowleyDB.scans.items()):
            break
        client.advance_with_timers(1)
    raw = next(iter(savedvars.loads(client.saved_variables())["CrowleyDB"]["scans"].values()))
    parsed = validate_envelope(envelope(raw))["decoded"]
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    observation = validate_envelope(envelope(raw))["decoded"]["observations"]["2589"]
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
    owned = validate_envelope(envelope(raw))["decoded"]["owned"]
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
    owned = validate_envelope(envelope(raw))["decoded"]["owned"]
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
    owned = validate_envelope(envelope(raw))["decoded"]["owned"]
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
    assert client.scanner_snapshot()["scanner"]["phase"] == "ready"


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


def test_faction_override_bypasses_unsupported_native_faction_and_full_sweep_is_strict():
    client = WowClient(target=non_beta_target(), current_region=1, realm="Normal", faction="Unrecognized")
    raw = run_sweep(client, settings={"faction": "alliance"})
    parsed = validate_envelope(envelope(raw))

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

    assert validate_envelope(envelope(raw))["house"]["key"] == "beta-normal-horde"


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

    assert validate_envelope(envelope(raw))["house"]["key"] == "us-normal-horde"


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

    assert validate_envelope(envelope(raw))["house"]["key"] == "beta-normal-horde"


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


def test_canonical_gate_accepts_shipped_plan_and_rejects_short_plan_before_start():
    client = WowClient()
    client.login()
    client.set_plan([2589])
    client.fire("AUCTION_HOUSE_SHOW")
    client.slash("scan")
    assert client.state.queryCalls == 0
    assert "plan_invalid" in client.chat[-1]
    client.set_canonical_plan()
    assert client.scanner_snapshot()["plan"] == {
        "hash": load_catalog().scan_plan()["planHash"],
        "count": len(load_catalog().scan_plan()["itemIDs"]),
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
    assert client.scanner_snapshot()["pending"]["reason"] == "plan_invalid"
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
    assert client.scanner_snapshot()["pending"]["reason"] == "plan_invalid"
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
    assert client.scanner_snapshot()["pending"]["reason"] == "plan_invalid"
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
    status = client.scanner_snapshot()
    assert status["scanner"]["phase"] == "blocked"
    assert status["pending"]["blocked"] is True
    assert status["pending"]["reason"] == "plan_invalid"
    published = client.scanner_snapshot()
    assert published["scanner"]["phase"] == "blocked"
    assert published["pending"]["blocked"] is True
    assert published["pending"]["reason"] == "plan_invalid"
    decoded = decode_frame(client.scanner_frame, expected_kind="scanner", plan_item_ids=source["itemIDs"])
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
    assert client.scanner_snapshot()["pending"]["reason"] == "plan_invalid"
    assert client.state.queryCalls == 0
