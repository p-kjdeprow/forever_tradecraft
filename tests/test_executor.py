"""Trade executor behavior using the real addon Lua and the WoW harness."""

from __future__ import annotations

from dataclasses import replace

import pytest

from crowley.core.game_target import packaged_game_target
from crowley.gui.frames import decode_frame

from .wow_harness import Auction, WowClient

US_TARGET = replace(
    packaged_game_target(),
    region="us",
    native_region_id=1,
    ruleset="normal",
    realm="Normal",
    faction="Horde",
    house="us-normal-horde",
)


def us_client(**kwargs):
    kwargs.setdefault("target", US_TARGET)
    kwargs.setdefault("faction", "Horde")
    kwargs.setdefault("realm", US_TARGET.realm)
    kwargs.setdefault("current_region", US_TARGET.native_region_id)
    return WowClient(**kwargs)


def make_queue(actions, *, armed=False, house="us-normal-horde", character="Tester"):
    return {
        "format": 1,
        "queueId": "us-normal-horde-1760000000-abcd",
        "generatedAt": 1759990000,
        "expiresAt": 1760000100,
        "house": house,
        "character": character,
        "armed": armed,
        "actions": [{"seq": index, **action} for index, action in enumerate(actions, start=1)],
    }


def load(action, *, armed=False, **kwargs):
    client = us_client(queue=make_queue([action], armed=armed, **kwargs))
    client.login()
    return client


def open_frame(client: WowClient, frame: str) -> None:
    client.set_frame_open(frame)


def arm(client: WowClient) -> None:
    client.slash("arm us-normal-horde-1760000000-abcd")


def result(client: WowClient) -> dict:
    execution = client.execution()
    assert execution is not None
    return execution["results"][-1]


def terminal_result(client: WowClient) -> dict:
    execution = client.execution()
    assert execution is not None
    for item in reversed(execution["results"]):
        if item["status"] in {"succeeded", "failed", "refused", "skipped"}:
            return item
    raise AssertionError("no terminal result")


def bag_state(items: list[dict]) -> dict:
    return {
        0: {
            "numSlots": len(items),
            "freeSlots": sum(1 for item in items if not item),
            "slots": {index: item for index, item in enumerate(items, start=1)},
        }
    }


def test_executor_is_inert_without_a_queue():
    client = us_client()
    assert client.executor_button is None
    client.login()
    assert client.executor_button is None
    assert client.ns.Executor.Valid() is False
    assert client.snapshot_agent()["executor"] == {
        "availability": "none",
        "queueId": "",
        "house": "",
        "character": "",
        "publishedArmed": False,
        "sessionArmed": False,
        "buttonVisible": False,
        "buttonName": "",
        "currentSeq": 0,
        "currentKind": "",
        "phase": "none",
        "inFlight": False,
        "clickReady": False,
        "workDone": 0,
        "workTotal": 0,
        "blocked": False,
        "latched": False,
        "ambiguous": False,
        "clickOrdinal": 0,
        "lastClickSource": "none",
        "resultCount": 0,
        "lastResult": {"seq": 0, "kind": "", "status": "none"},
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"extra": True},
        {"format": 1, "unknown": True},
        {
            "format": 1,
            "queueId": "id",
            "generatedAt": 2,
            "expiresAt": 1,
            "house": "h",
            "character": "Tester",
            "armed": False,
            "actions": [],
        },
        {
            "format": 1,
            "queueId": "id",
            "generatedAt": 1,
            "expiresAt": 2,
            "house": "h",
            "character": "Tester",
            "armed": False,
            "actions": [{"seq": 1, "kind": "unknown"}],
        },
    ],
)
def test_invalid_queue_is_rejected_and_records_sequence_zero(bad):
    client = us_client(queue=bad)
    client.login()
    execution = client.execution(bad.get("queueId", "<invalid>"))
    assert execution["results"][0]["seq"] == 0
    assert execution["results"][0]["status"] == "refused"
    assert client.executor_button is None


def test_executor_telemetry_distinguishes_invalid_queue_and_valid_mail_progress():
    invalid = us_client(queue={"format": 1, "queueId": "x"})
    invalid.login()
    invalid_value = invalid.snapshot_agent()["executor"]
    assert invalid_value["availability"] == "invalid"
    assert invalid_value["queueId"] == ""
    assert invalid_value["resultCount"] == 1
    assert invalid_value["lastResult"] == {"seq": 0, "kind": "queue", "status": "refused"}

    client = load({"kind": "mail_collect", "maxMessages": 2})
    client.set_mail_count(2)
    open_frame(client, "mailbox")
    ready = client.snapshot_agent()["executor"]
    assert ready["availability"] == "valid"
    assert ready["currentSeq"] == 1 and ready["currentKind"] == "mail_collect"
    assert ready["phase"] == "ready"
    assert ready["buttonVisible"] is True and ready["clickReady"] is True
    assert ready["workDone"] == 0 and ready["workTotal"] == 2
    # The native visibility fact remains true even when the unreliable close
    # event is delivered; the executor must not hide the button from that event.
    client.fire("MAIL_CLOSED")
    assert client.snapshot_agent()["executor"]["buttonVisible"] is True


def test_mail_show_event_without_native_mail_frame_is_not_click_ready():
    client = load({"kind": "mail_collect", "maxMessages": 1})
    client.set_mail_count(1)
    # Deliberately publish only the event flag.  MailFrame:IsShown remains
    # false, so the event cannot authorize a button or protected call.
    client.fire("MAIL_SHOW")
    value = client.snapshot_agent()["executor"]
    assert value["buttonVisible"] is False
    assert value["clickReady"] is False
    assert client.snapshot_agent()["mailOpen"] is False
    client.click_executor(force=True)
    assert client.protected_calls == []


def test_armed_partial_mail_frames_prove_wait_progress_and_one_second_readiness():
    client = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    client.set_canonical_plan()
    client.set_mail_count(2)
    client.set_auto_postconditions(False)
    open_frame(client, "mailbox")
    arm(client)
    client.click_executor()
    assert client.protected_calls == ["AutoLootMailItem"]

    # Pause on the native postcondition: the first message is started but not
    # yet observed empty/removed.  This is a real CRS1 FrameBytes decode.
    client.update_agent()
    waiting = decode_frame(client.agent_frame).snapshot["executor"]
    assert waiting["availability"] == "valid"
    assert waiting["phase"] == "mail_wait"
    assert waiting["inFlight"] is True
    assert waiting["clickReady"] is False
    assert waiting["workDone"] == 0 and waiting["workTotal"] == 2
    assert waiting["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "started"}

    # Expose the first message's native empty postcondition, then wait the
    # actual one-second click-rate interval before checking mail_items.
    client.set_mail_items([{"money": 0}, {"money": 20}])
    client.fire("MAIL_INBOX_UPDATE")
    client.update_agent()
    too_fast = decode_frame(client.agent_frame).snapshot["executor"]
    assert too_fast["phase"] == "mail_items"
    assert too_fast["inFlight"] is True
    assert too_fast["clickReady"] is False
    assert too_fast["workDone"] == 1 and too_fast["workTotal"] == 2
    client.advance(1)
    client.update_agent()
    ready = decode_frame(client.agent_frame).snapshot["executor"]
    assert ready["phase"] == "mail_items"
    assert ready["inFlight"] is True
    assert ready["clickReady"] is True
    assert ready["workDone"] == 1 and ready["workTotal"] == 2
    assert ready["resultCount"] == 1
    assert client.protected_calls == ["AutoLootMailItem"]


def test_mail_success_advances_to_new_action_with_fresh_frame_progress():
    client = us_client(
        queue=make_queue(
            [
                {"kind": "mail_collect", "maxMessages": 1},
                {"kind": "mail_collect", "maxMessages": 2},
            ],
            armed=True,
        )
    )
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(1)
    open_frame(client, "mailbox")
    arm(client)
    client.click_executor()

    client.update_agent()
    executor = decode_frame(client.agent_frame).snapshot["executor"]
    assert executor["currentSeq"] == 2
    assert executor["currentKind"] == "mail_collect"
    assert executor["phase"] == "ready"
    assert executor["inFlight"] is False
    assert executor["workDone"] == 0
    assert executor["workTotal"] == 2
    assert executor["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "succeeded"}


def test_partial_mail_ui_error_roundtrips_progress_on_blocked_ready_state():
    client = us_client(queue=make_queue([{"kind": "mail_collect", "maxMessages": 2}], armed=True))
    client.set_canonical_plan()
    client.login()
    client.set_mail_count(2)
    client.set_auto_postconditions(False)
    open_frame(client, "mailbox")
    arm(client)
    client.click_executor()

    client.set_mail_items([{"money": 0}, {"money": 20}])
    client.fire("MAIL_INBOX_UPDATE")
    client.fire("UI_ERROR_MESSAGE", 1, "native failure")

    client.update_agent()
    executor = decode_frame(client.agent_frame).snapshot["executor"]
    assert executor["currentSeq"] == 1
    assert executor["currentKind"] == "mail_collect"
    assert executor["phase"] == "ready"
    assert executor["inFlight"] is False
    assert executor["blocked"] is True
    assert executor["workDone"] == 1 and executor["workTotal"] == 2
    assert executor["lastResult"] == {"seq": 1, "kind": "mail_collect", "status": "failed"}


@pytest.mark.parametrize("queue", [True, "malformed", 17], ids=["true", "string", "number"])
def test_truthy_non_table_queue_publishes_invalid_executor_telemetry(queue):
    client = us_client(queue=queue)
    client.login()
    value = client.snapshot_agent()["executor"]
    assert value["availability"] == "invalid"
    assert value["queueId"] == ""
    assert value["lastResult"] == {"seq": 0, "kind": "queue", "status": "refused"}
    assert value["resultCount"] == 1


def test_executor_telemetry_physical_button_ordinal_is_not_direct_api_ordinal():
    direct = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1})
    direct.set_merchant_items([{"itemID": 3371}])
    open_frame(direct, "merchant")
    direct.click_executor(force=True)
    direct_value = direct.snapshot_agent()["executor"]
    assert direct_value["lastClickSource"] == "directAPI"
    assert direct_value["clickOrdinal"] == 0

    button = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1})
    button.set_merchant_items([{"itemID": 3371}])
    open_frame(button, "merchant")
    button.click_executor()
    button_value = button.snapshot_agent()["executor"]
    assert button_value["lastClickSource"] == "button"
    assert button_value["clickOrdinal"] == 1


def test_unarmed_click_is_a_dry_run_without_protected_calls():
    client = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 5})
    client.set_merchant_items([{"itemID": 3371}])
    open_frame(client, "merchant")
    client.click_executor()
    assert client.protected_calls == []
    assert result(client)["status"] == "dry_run"


def test_buy_requires_both_arms_and_completes_on_events():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    client.fire("COMMODITY_PRICE_UPDATED", 2589, 20, 40)
    client.advance(1)
    client.click_executor()
    assert client.protected_calls[-1] == "C_AuctionHouse.ConfirmCommoditiesPurchase"
    client.fire("COMMODITY_PURCHASE_SUCCEEDED", 2589, 2)
    assert result(client)["status"] == "succeeded"
    assert result(client)["detail"]["unitPrice"] == 20


def test_high_commodity_price_cancels_and_blocks():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PRICE_UPDATED", 2589, 26, 52)
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    client.advance(1)
    client.click_executor()
    assert client.protected_calls == [
        "C_AuctionHouse.StartCommoditiesPurchase",
        "C_AuctionHouse.CancelCommoditiesPurchase",
    ]
    assert terminal_result(client)["status"] == "refused"
    assert terminal_result(client)["detail"]["reason"] == "price_above_max"
    assert result(client)["status"] == "blocked"


def test_post_and_cancel_record_native_details():
    post = load(
        {"kind": "post_commodity", "itemID": 2589, "quantity": 2, "unitPrice": 30, "durationHours": 12},
        armed=True,
    )
    post.set_bags(bag_state([{"itemID": 2589, "stackCount": 5}]))
    post.set_auctions([Auction(2589, "Linen", 1, 100, isCommodity=True)])
    open_frame(post, "ah")
    arm(post)
    post.click_executor()
    post.fire("AUCTION_HOUSE_AUCTION_CREATED")
    assert result(post)["status"] == "succeeded"
    assert result(post)["detail"]["bag"] == 0
    assert result(post)["detail"]["deposit"] == 2

    cancel = load({"kind": "cancel_auction", "auctionID": 123}, armed=True)
    open_frame(cancel, "ah")
    arm(cancel)
    cancel.click_executor()
    cancel.fire("AUCTION_CANCELED", 123)
    assert cancel.protected_calls == ["C_AuctionHouse.CancelAuction"]
    assert result(cancel)["status"] == "succeeded"


@pytest.mark.parametrize(
    ("action", "setup", "event", "event_args", "native_call"),
    [
        (
            {"kind": "post_commodity", "itemID": 2589, "quantity": 2, "unitPrice": 30, "durationHours": 12},
            lambda client: (
                client.set_bags(bag_state([{"itemID": 2589, "stackCount": 5}])),
                client.set_auctions([Auction(2589, "Linen", 1, 100, isCommodity=True)]),
            ),
            "AUCTION_HOUSE_AUCTION_CREATED",
            (),
            "C_AuctionHouse.PostCommodity",
        ),
        (
            {"kind": "cancel_auction", "auctionID": 123},
            lambda client: None,
            "AUCTION_CANCELED",
            (123,),
            "C_AuctionHouse.CancelAuction",
        ),
    ],
    ids=("post-commodity", "cancel-auction"),
)
def test_started_post_and_cancel_frames_decode_before_native_success(
    action, setup, event, event_args, native_call
):
    client = load(action, armed=True)
    client.set_canonical_plan()
    setup(client)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    assert client.protected_calls == [native_call]

    # The native call has started, but no success event has arrived.  The
    # producer's literal nil Lua phase wires as ready; the consumer accepts
    # this only for these two actual beginStarted paths and their started proof.
    client.update_agent()
    pending = decode_frame(client.agent_frame).snapshot["executor"]
    assert pending["availability"] == "valid"
    assert pending["currentSeq"] == 1
    assert pending["currentKind"] == action["kind"]
    assert pending["phase"] == "ready"
    assert pending["inFlight"] is True
    assert pending["clickReady"] is False
    assert pending["lastResult"] == {"seq": 1, "kind": action["kind"], "status": "started"}

    # A second physical click is too fast while the native call is still
    # unresolved.  It appends a same-action blocked result and latches the
    # executor; the literal ready/inFlight wire state remains valid.
    client.click_executor()
    client.update_agent()
    blocked = decode_frame(client.agent_frame).snapshot["executor"]
    assert blocked["phase"] == "ready"
    assert blocked["inFlight"] is True
    assert blocked["clickReady"] is False
    assert blocked["blocked"] is False and blocked["ambiguous"] is False
    assert blocked["latched"] is True
    assert blocked["lastResult"] == {"seq": 1, "kind": action["kind"], "status": "blocked"}

    client.fire(event, *event_args)
    client.update_agent()
    terminal = decode_frame(client.agent_frame).snapshot["executor"]
    assert terminal["availability"] == "valid"
    assert terminal["currentSeq"] == 0
    assert terminal["currentKind"] == ""
    assert terminal["phase"] == "complete"
    assert terminal["inFlight"] is False
    assert terminal["resultCount"] == 2
    assert terminal["lastResult"] == {"seq": 1, "kind": action["kind"], "status": "blocked"}
    assert client.execution()["results"][0]["status"] == "succeeded"


def test_vendor_stack_mismatch_and_missing_merchant_are_refused():
    sell = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 5}, armed=True)
    sell.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}, {"itemID": 2589, "stackCount": 4}]))
    open_frame(sell, "merchant")
    sell.click_executor()
    assert result(sell)["detail"] == "stack_mismatch"

    buy = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 5}, armed=True)
    open_frame(buy, "merchant")
    buy.click_executor()
    assert result(buy)["detail"] == "merchant_item_missing"


def test_missing_bag_stack_recipe_identity_and_frame_identity_failures():
    missing = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 5}, armed=True)
    missing.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}]))
    open_frame(missing, "merchant")
    missing.click_executor()
    assert result(missing)["detail"] == "missing_bag_stack"

    not_learned = load({"kind": "craft", "recipeID": 2963, "times": 1}, armed=True)
    not_learned.set_trade_skill(
        ready=True, recipe_ids=[2963], recipe_infos={2963: {"learned": False}}, craftable_counts={2963: 1}
    )
    open_frame(not_learned, "profession")
    not_learned.click_executor()
    assert result(not_learned)["detail"] == "recipe_not_learned"

    short = load({"kind": "craft", "recipeID": 2963, "times": 2}, armed=True)
    short.set_trade_skill(
        ready=True, recipe_ids=[2963], recipe_infos={2963: {"learned": True}}, craftable_counts={2963: 1}
    )
    open_frame(short, "profession")
    short.click_executor()
    assert result(short)["detail"] == "craftable_count_short"

    house = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True, house="eu-normal-horde")
    house.set_merchant_items([{"itemID": 3371}])
    open_frame(house, "merchant")
    house.click_executor()
    assert result(house)["detail"] == "house_mismatch"

    character = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True, character="Other")
    character.set_merchant_items([{"itemID": 3371}])
    open_frame(character, "merchant")
    character.click_executor()
    assert result(character)["detail"] == "character_mismatch"


def test_craft_and_mail_use_one_click_per_work_unit():
    craft = load({"kind": "craft", "recipeID": 2963, "times": 1}, armed=True)
    craft.set_trade_skill(
        ready=True, recipe_ids=[2963], recipe_infos={2963: {"learned": True}}, craftable_counts={2963: 1}
    )
    open_frame(craft, "profession")
    arm(craft)
    craft.click_executor()
    assert craft.protected_calls == ["C_TradeSkillUI.CraftRecipe"]
    assert result(craft)["status"] == "succeeded"

    mail = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    mail.set_mail_count(2)
    open_frame(mail, "mailbox")
    arm(mail)
    mail.click_executor()
    mail.advance(1)
    mail.click_executor(force=True)
    assert mail.protected_calls == ["AutoLootMailItem", "AutoLootMailItem"]
    assert result(mail)["status"] == "succeeded"


def test_wrong_frame_expiry_identity_and_too_fast_are_refusals():
    client = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True)
    client.set_merchant_items([{"itemID": 3371}])
    client.click_executor(force=True)
    assert result(client)["detail"] == "wrong_frame"

    expired = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True)
    expired.set_merchant_items([{"itemID": 3371}])
    open_frame(expired, "merchant")
    expired.advance(101)
    expired.click_executor()
    assert result(expired)["detail"] == "expired"

    fast = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}, armed=True)
    open_frame(fast, "ah")
    arm(fast)
    fast.click_executor()
    fast.advance(0)
    fast.click_executor(force=True)
    assert result(fast)["status"] == "blocked"
    assert result(fast)["detail"]["reason"] == "too_fast"


def test_slash_commands_and_session_arm_are_local():
    client = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True)
    client.slash("queue")
    assert "queue: us-normal-horde-1760000000-abcd" in client.chat[-1]
    client.slash("arm wrong")
    assert "queue_id_mismatch" in client.chat[-1]
    client.slash("arm us-normal-horde-1760000000-abcd")
    assert "session=true" in client.chat[-1]
    client.slash("disarm")
    assert "session=false" in client.chat[-1]
    client.slash("skip")
    assert result(client)["status"] == "skipped"


@pytest.mark.parametrize(
    "kind, setup, frame",
    [
        ("buy_commodity", lambda c: None, "ah"),
        (
            "post_commodity",
            lambda c: (
                c.set_bags(bag_state([{"itemID": 2589, "stackCount": 5}])),
                c.set_auctions([Auction(2589, "Linen", 1, 100, isCommodity=True)]),
            ),
            "ah",
        ),
        ("cancel_auction", lambda c: None, "ah"),
        ("vendor_sell", lambda c: c.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}])), "merchant"),
        ("vendor_buy", lambda c: c.set_merchant_items([{"itemID": 3371}]), "merchant"),
        (
            "craft",
            lambda c: c.set_trade_skill(
                ready=True,
                recipe_ids=[2963],
                recipe_infos={2963: {"learned": True}},
                craftable_counts={2963: 1},
            ),
            "profession",
        ),
        ("mail_collect", lambda c: c.set_mail_count(1), "mailbox"),
    ],
)
def test_every_kind_is_a_dry_run_without_both_arms(kind, setup, frame):
    actions = {
        "buy_commodity": {"kind": kind, "itemID": 2589, "quantity": 1, "maxUnitPrice": 25},
        "post_commodity": {"kind": kind, "itemID": 2589, "quantity": 1, "unitPrice": 30, "durationHours": 12},
        "cancel_auction": {"kind": kind, "auctionID": 123},
        "vendor_sell": {"kind": kind, "itemID": 2589, "quantity": 2},
        "vendor_buy": {"kind": kind, "itemID": 3371, "quantity": 5},
        "craft": {"kind": kind, "recipeID": 2963, "times": 1},
        "mail_collect": {"kind": kind, "maxMessages": 1},
    }
    client = load(actions[kind], armed=True)
    setup(client)
    open_frame(client, frame)
    client.click_executor()
    assert client.protected_calls == []
    assert result(client)["status"] == "dry_run"


def test_each_single_arm_is_still_a_dry_run():
    published_off = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=False)
    published_off.set_merchant_items([{"itemID": 3371}])
    open_frame(published_off, "merchant")
    arm(published_off)
    published_off.click_executor()
    assert published_off.protected_calls == []
    assert result(published_off)["status"] == "dry_run"

    session_off = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True)
    session_off.set_merchant_items([{"itemID": 3371}])
    open_frame(session_off, "merchant")
    session_off.click_executor()
    assert session_off.protected_calls == []
    assert result(session_off)["status"] == "dry_run"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda queue: queue["actions"][0].update(extra=True),
        lambda queue: queue["actions"][0].update(itemID="2589"),
        lambda queue: queue["actions"][0].update(seq=2),
        lambda queue: queue.update(unknown=True),
    ],
)
def test_strict_validation_rejects_bad_fields_as_the_whole_queue(mutation):
    queue = make_queue([{"kind": "vendor_buy", "itemID": 3371, "quantity": 1}])
    mutation(queue)
    client = us_client(queue=queue)
    client.login()
    execution = client.execution(queue["queueId"])
    assert len(execution["results"]) == 1
    assert execution["results"][0]["seq"] == 0
    assert execution["results"][0]["status"] == "refused"
    assert client.executor_button is None


def test_more_than_fifty_actions_is_rejected():
    queue = make_queue([{"kind": "vendor_buy", "itemID": 3371, "quantity": 1} for _ in range(51)])
    client = us_client(queue=queue)
    client.login()
    execution = client.execution(queue["queueId"])
    assert execution["results"][0]["seq"] == 0
    assert "50" in execution["results"][0]["detail"]


def test_buy_does_not_confirm_before_price_and_validates_prices():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.advance(1)
    client.click_executor()
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert result(client)["status"] == "blocked"
    assert result(client)["detail"]["reason"] == "price_pending"

    invalid = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(invalid, "ah")
    arm(invalid)
    invalid.click_executor()
    invalid.fire("COMMODITY_PRICE_UPDATED", 2589, -1, -2)
    assert invalid.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert invalid.execution()["results"][0]["status"] == "started"
    assert result(invalid)["status"] == "blocked"
    assert result(invalid)["detail"]["reason"] == "price_invalid"
    invalid.advance(1)
    invalid.click_executor()
    assert terminal_result(invalid)["status"] == "refused"


def test_buy_preflights_start_confirm_and_cancel_before_start():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}, armed=True)
    client.lua.globals().C_AuctionHouse.CancelCommoditiesPurchase = None
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    assert client.protected_calls == []
    assert result(client)["detail"] == "buy_api_unavailable"


def test_cancel_price_is_a_separate_protected_click_with_exact_arguments():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PRICE_UPDATED", 2589, 26, 52)
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    client.advance(1)
    client.click_executor()
    assert client.protected_call_records[-1] == {
        "name": "C_AuctionHouse.CancelCommoditiesPurchase",
        "argc": 1,
        "args": [2589],
    }
    assert terminal_result(client)["status"] == "refused"
    assert result(client)["status"] == "blocked"


def test_disarm_is_rechecked_before_confirmation_and_continuation_calls():
    buy = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(buy, "ah")
    arm(buy)
    buy.click_executor()
    buy.fire("COMMODITY_PRICE_UPDATED", 2589, 20, 40)
    buy.slash("disarm")
    buy.advance(1)
    buy.click_executor()
    assert buy.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert buy.execution()["results"][-1]["detail"]["reason"] == "not_armed"

    sell = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 5}, armed=True)
    sell.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}, {"itemID": 2589, "stackCount": 3}]))
    open_frame(sell, "merchant")
    arm(sell)
    sell.click_executor()
    sell.slash("disarm")
    sell.advance(1)
    sell.click_executor()
    assert sell.protected_calls == ["C_Container.UseContainerItem"]

    mail = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    mail.set_mail_count(2)
    open_frame(mail, "mailbox")
    arm(mail)
    mail.click_executor()
    mail.slash("disarm")
    mail.advance(1)
    mail.click_executor(force=True)
    assert mail.protected_calls == ["AutoLootMailItem"]


@pytest.mark.parametrize("kind", ["vendor_sell", "mail_collect"])
def test_in_flight_refusal_latches_before_the_next_work_unit(kind):
    if kind == "vendor_sell":
        client = load({"kind": kind, "itemID": 2589, "quantity": 5}, armed=True)
        client.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}, {"itemID": 2589, "stackCount": 3}]))
        frame = "merchant"
    else:
        client = load({"kind": kind, "maxMessages": 2}, armed=True)
        client.set_mail_count(2)
        frame = "mailbox"
    open_frame(client, frame)
    arm(client)
    client.click_executor()
    client.slash("disarm")
    client.advance(1)
    client.click_executor(force=True)
    assert len(client.protected_calls) == 1
    assert result(client)["status"] == "blocked"
    assert result(client)["detail"]["reason"] == "not_armed"

    arm(client)
    client.advance(1)
    client.click_executor(force=True)
    assert len(client.protected_calls) == 1
    assert client.ns.Executor.State().inFlight is True
    assert client.ns.Executor.State().latched is True


def test_too_fast_is_stopping_and_in_flight_skip_is_refused():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.click_executor(force=True)
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    client.slash("skip")
    assert "in_flight" in client.chat[-1]
    client.advance(2)
    client.click_executor(force=True)
    assert client.protected_calls == [
        "C_AuctionHouse.StartCommoditiesPurchase",
        "C_AuctionHouse.CancelCommoditiesPurchase",
    ]
    assert terminal_result(client)["status"] == "refused"
    assert result(client)["status"] == "blocked"


def test_in_flight_skip_cannot_abandon_a_started_purchase():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.advance(1)
    client.slash("skip")
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert client.execution()["results"][-1]["status"] == "started"
    assert "in_flight" in client.chat[-1]


def test_vendor_revalidates_every_slot_before_selling():
    client = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 5}, armed=True)
    client.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}, {"itemID": 2589, "stackCount": 3}]))
    open_frame(client, "merchant")
    arm(client)
    client.click_executor()
    client.set_bags(bag_state([{}, {"itemID": 999, "stackCount": 3}]))
    client.advance(1)
    client.click_executor(force=True)
    assert client.protected_calls == ["C_Container.UseContainerItem"]
    assert result(client)["status"] == "blocked"
    assert result(client)["detail"]["reason"] == "slot_changed"


def test_exact_protected_arguments_and_result_shape():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PRICE_UPDATED", 2589, 20, 40)
    client.advance(1)
    client.click_executor()
    client.fire("COMMODITY_PURCHASE_SUCCEEDED", 2589, 2)
    assert client.protected_call_records == [
        {"name": "C_AuctionHouse.StartCommoditiesPurchase", "argc": 2, "args": [2589, 2]},
        {"name": "C_AuctionHouse.ConfirmCommoditiesPurchase", "argc": 2, "args": [2589, 2]},
    ]
    latest = result(client)
    assert set(latest) == {"seq", "kind", "status", "at", "detail"}
    assert latest["detail"] == {"itemID": 2589, "quantity": 2, "unitPrice": 20, "totalPrice": 40}


def test_cancel_cost_is_required_and_event_must_match_auction_id():
    client = load({"kind": "cancel_auction", "auctionID": 123}, armed=True)
    client.set_cancel_cost(7)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    assert client.protected_call_records[-1] == {
        "name": "C_AuctionHouse.CancelAuction",
        "argc": 1,
        "args": [123],
    }
    client.fire("AUCTION_CANCELED", 456)
    assert result(client)["status"] == "started"
    client.fire("AUCTION_CANCELED", 123)
    assert result(client)["status"] == "succeeded"

    missing = load({"kind": "cancel_auction", "auctionID": 123}, armed=True)
    missing.lua.globals().C_AuctionHouse.GetCancelCost = None
    open_frame(missing, "ah")
    arm(missing)
    missing.click_executor()
    assert missing.protected_calls == []
    assert result(missing)["detail"] == "cancel_cost_unavailable"


def test_purchase_failure_event_is_recorded_only_after_confirmation():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PURCHASE_SUCCEEDED", 2589, 2)
    assert result(client)["status"] == "started"
    client.fire("COMMODITY_PRICE_UPDATED", 2589, 20, 40)
    client.advance(1)
    client.click_executor()
    client.fire("COMMODITY_PURCHASE_FAILED", 1, "declined")
    assert result(client)["status"] == "failed"
    assert result(client)["detail"] == {"event": "COMMODITY_PURCHASE_FAILED", "error": "declined"}


def test_every_protected_call_captures_its_exact_arguments():
    post = load(
        {"kind": "post_commodity", "itemID": 2589, "quantity": 2, "unitPrice": 30, "durationHours": 24},
        armed=True,
    )
    post.set_bags(bag_state([{"itemID": 2589, "stackCount": 5}]))
    post.set_auctions([Auction(2589, "Linen", 1, 100, isCommodity=True)])
    open_frame(post, "ah")
    arm(post)
    post.click_executor()
    assert post.protected_call_records[-1] == {
        "name": "C_AuctionHouse.PostCommodity",
        "argc": 4,
        "args": [{"bag": 0, "slot": 1}, 2, 2, 30],
    }

    sell = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 2}, armed=True)
    sell.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}]))
    open_frame(sell, "merchant")
    arm(sell)
    sell.click_executor()
    assert sell.protected_call_records[-1] == {
        "name": "C_Container.UseContainerItem",
        "argc": 2,
        "args": [0, 1],
    }

    buy = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 5}, armed=True)
    buy.set_merchant_items([{"itemID": 3371}, {"itemID": 999}])
    open_frame(buy, "merchant")
    arm(buy)
    buy.click_executor()
    assert buy.protected_call_records[-1] == {"name": "BuyMerchantItem", "argc": 2, "args": [1, 5]}

    craft = load({"kind": "craft", "recipeID": 2963, "times": 2}, armed=True)
    craft.set_trade_skill(
        ready=True, recipe_ids=[2963], recipe_infos={2963: {"learned": True}}, craftable_counts={2963: 2}
    )
    open_frame(craft, "profession")
    arm(craft)
    craft.click_executor()
    assert craft.protected_call_records[-1] == {
        "name": "C_TradeSkillUI.CraftRecipe",
        "argc": 2,
        "args": [2963, 2],
    }

    mail = load({"kind": "mail_collect", "maxMessages": 1}, armed=True)
    mail.set_mail_count(1)
    open_frame(mail, "mailbox")
    arm(mail)
    mail.click_executor()
    assert mail.protected_call_records[-1] == {"name": "AutoLootMailItem", "argc": 1, "args": [1]}


def test_protected_harness_stubs_do_not_discard_unexpected_arguments():
    client = us_client()
    client.lua.globals().C_AuctionHouse.StartCommoditiesPurchase(2589, 2, "unexpected")
    assert client.protected_call_records[-1] == {
        "name": "C_AuctionHouse.StartCommoditiesPurchase",
        "argc": 3,
        "args": [2589, 2, "unexpected"],
    }


@pytest.mark.parametrize("kind", ["vendor_buy", "craft", "mail_collect"])
def test_postcondition_actions_remain_in_flight_and_ui_errors_fail_them(kind):
    if kind == "vendor_buy":
        client = load({"kind": kind, "itemID": 3371, "quantity": 2}, armed=True)
        client.set_merchant_items([{"itemID": 3371}])
        frame = "merchant"
    elif kind == "craft":
        client = load({"kind": kind, "recipeID": 2963, "times": 2}, armed=True)
        client.set_trade_skill(
            ready=True, recipe_ids=[2963], recipe_infos={2963: {"learned": True}}, craftable_counts={2963: 2}
        )
        frame = "profession"
    else:
        client = load({"kind": kind, "maxMessages": 1}, armed=True)
        client.set_mail_count(1)
        frame = "mailbox"
    client.set_auto_postconditions(False)
    open_frame(client, frame)
    arm(client)
    client.click_executor()
    assert result(client)["status"] == "started"
    client.fire("UI_ERROR_MESSAGE", 1, "native failure")
    assert result(client)["status"] == "failed"
    assert result(client)["detail"]["error"] == "native failure"


def test_reload_reconciles_success_dry_run_and_ambiguous_started_history():
    original = load({"kind": "vendor_buy", "itemID": 3371, "quantity": 1}, armed=True)
    original.set_merchant_items([{"itemID": 3371}])
    open_frame(original, "merchant")
    arm(original)
    original.click_executor()
    saved = {"executions": {original.execution()["queueId"]: original.execution()}}
    reloaded = us_client(
        queue=make_queue([{"kind": "vendor_buy", "itemID": 3371, "quantity": 1}], armed=True),
        initial_savedvars=saved,
    )
    reloaded.login()
    assert reloaded.ns.Executor.Current() is None
    assert reloaded.ns.Executor.SessionArmed() is False

    queue = make_queue([{"kind": "vendor_buy", "itemID": 3371, "quantity": 1}], armed=True)
    dry_saved = {
        "executions": {
            queue["queueId"]: {
                "queueId": queue["queueId"],
                "house": queue["house"],
                "character": queue["character"],
                "results": [
                    {"seq": 1, "kind": "vendor_buy", "status": "dry_run", "at": 1760000000, "detail": {}}
                ],
            }
        }
    }
    dry = us_client(queue=queue, initial_savedvars=dry_saved)
    dry.login()
    assert dry.ns.Executor.Current()["seq"] == 1

    ambiguous = make_queue([{"kind": "vendor_buy", "itemID": 3371, "quantity": 1}], armed=True)
    ambiguous_saved = {
        "executions": {
            ambiguous["queueId"]: {
                "queueId": ambiguous["queueId"],
                "house": ambiguous["house"],
                "character": ambiguous["character"],
                "results": [
                    {"seq": 1, "kind": "vendor_buy", "status": "started", "at": 1760000000, "detail": {}}
                ],
            }
        }
    }
    blocked = us_client(queue=ambiguous, initial_savedvars=ambiguous_saved)
    blocked.login()
    blocked.set_merchant_items([{"itemID": 3371}])
    open_frame(blocked, "merchant")
    blocked.slash("arm us-normal-horde-1760000000-abcd")
    blocked.click_executor()
    assert blocked.protected_calls == []
    assert result(blocked)["detail"] == {"reason": "ambiguous_history"}


def test_reload_reconciles_ambiguous_history_across_all_sequences():
    queue = make_queue(
        [
            {"kind": "vendor_buy", "itemID": 3371, "quantity": 1},
            {"kind": "vendor_buy", "itemID": 3371, "quantity": 1},
        ],
        armed=True,
    )
    saved = {
        "executions": {
            queue["queueId"]: {
                "queueId": queue["queueId"],
                "house": queue["house"],
                "character": queue["character"],
                "results": [
                    {"seq": 1, "kind": "vendor_buy", "status": "dry_run", "at": 1760000000, "detail": {}},
                    {"seq": 2, "kind": "vendor_buy", "status": "started", "at": 1760000000, "detail": {}},
                    {
                        "seq": 2,
                        "kind": "vendor_buy",
                        "status": "blocked",
                        "at": 1760000000,
                        "detail": {"reason": "too_fast"},
                    },
                ],
            }
        }
    }
    client = us_client(queue=queue, initial_savedvars=saved)
    client.login()
    client.set_merchant_items([{"itemID": 3371}])
    open_frame(client, "merchant")
    arm(client)
    client.click_executor()
    assert client.protected_calls == []
    assert result(client)["detail"] == {"reason": "ambiguous_history"}
    assert client.ns.Executor.Current()["seq"] == 2


def test_ah_executor_fails_closed_during_scanner_activity_or_unknown_state():
    active = load({"kind": "cancel_auction", "auctionID": 123}, armed=True)
    active.ns.Scanner.InProgress = active.lua.eval("function() return true end")
    open_frame(active, "ah")
    arm(active)
    active.click_executor()
    assert active.protected_calls == []
    assert result(active)["detail"] == "scanner_active"

    unknown = load({"kind": "cancel_auction", "auctionID": 123}, armed=True)
    unknown.ns.Scanner.InProgress = None
    open_frame(unknown, "ah")
    arm(unknown)
    unknown.click_executor()
    assert unknown.protected_calls == []
    assert result(unknown)["detail"] == "scanner_state_unavailable"


def test_stale_commodity_price_event_cannot_authorize_confirmation():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PRICE_UPDATED", 9999, 20, 40)
    client.advance(1)
    client.click_executor()
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert result(client)["status"] == "blocked"
    assert result(client)["detail"]["reason"] == "price_pending"

    correct = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(correct, "ah")
    arm(correct)
    correct.click_executor()
    correct.fire("COMMODITY_PRICE_UPDATED", 2589, 20, 40)
    correct.advance(1)
    correct.click_executor()
    assert correct.protected_calls[-1] == "C_AuctionHouse.ConfirmCommoditiesPurchase"


def test_invalid_price_stays_in_flight_until_an_explicit_cancel():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 2, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.fire("COMMODITY_PRICE_UPDATED", 2589, -1, 0)
    assert client.protected_calls == ["C_AuctionHouse.StartCommoditiesPurchase"]
    assert client.execution()["results"][0]["status"] == "started"
    client.advance(1)
    client.click_executor()
    assert client.protected_calls[-1] == "C_AuctionHouse.CancelCommoditiesPurchase"
    assert terminal_result(client)["status"] == "refused"
    assert terminal_result(client)["detail"]["reason"] == "price_invalid"


def test_blocked_started_history_is_ambiguous_after_reload_and_cannot_be_skipped():
    client = load({"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}, armed=True)
    open_frame(client, "ah")
    arm(client)
    client.click_executor()
    client.advance(0)
    client.click_executor(force=True)
    saved = {"executions": {client.execution()["queueId"]: client.execution()}}

    reloaded = us_client(
        queue=make_queue(
            [{"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}], armed=True
        ),
        initial_savedvars=saved,
    )
    reloaded.login()
    open_frame(reloaded, "ah")
    reloaded.slash("arm us-normal-horde-1760000000-abcd")
    reloaded.click_executor()
    assert reloaded.protected_calls == []
    # A click on an ambiguous history is a nonterminal event, so the ambiguity survives reloads.
    assert result(reloaded)["status"] == "blocked"
    assert result(reloaded)["detail"] == {"reason": "ambiguous_history"}
    reloaded.slash("skip")
    assert "ambiguous_history" in reloaded.chat[-1]

    again = us_client(
        queue=make_queue(
            [{"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25}], armed=True
        ),
        initial_savedvars={"executions": {reloaded.execution()["queueId"]: reloaded.execution()}},
    )
    again.login()
    open_frame(again, "ah")
    again.slash("arm us-normal-horde-1760000000-abcd")
    again.click_executor()
    assert again.protected_calls == []
    assert result(again)["detail"] == {"reason": "ambiguous_history"}

    # Contract: a refused result after a started one is terminal (in-flight refusals are blocked
    # events), e.g. a purchase whose excessive price was cancelled; the queue moves on.
    cancelled = make_queue(
        [
            {"kind": "buy_commodity", "itemID": 2589, "quantity": 1, "maxUnitPrice": 25},
            {"kind": "vendor_buy", "itemID": 3371, "quantity": 5},
        ],
        armed=True,
    )
    cancelled_saved = {
        "executions": {
            cancelled["queueId"]: {
                "queueId": cancelled["queueId"],
                "house": cancelled["house"],
                "character": cancelled["character"],
                "results": [
                    {"seq": 1, "kind": "buy_commodity", "status": "started", "at": 1760000000, "detail": {}},
                    {
                        "seq": 1,
                        "kind": "buy_commodity",
                        "status": "blocked",
                        "at": 1760000000,
                        "detail": {"reason": "price_above_max"},
                    },
                    {
                        "seq": 1,
                        "kind": "buy_commodity",
                        "status": "refused",
                        "at": 1760000001,
                        "detail": {"reason": "price_above_max"},
                    },
                ],
            }
        }
    }
    resumed = us_client(queue=cancelled, initial_savedvars=cancelled_saved)
    resumed.login()
    resumed.set_merchant_items([{"itemID": 3371}])
    open_frame(resumed, "merchant")
    resumed.slash("arm us-normal-horde-1760000000-abcd")
    # Not ambiguous: the executor stops at the refused purchase (contract), nothing is in flight,
    # so an explicit skip is accepted and the next action runs.
    resumed.click_executor()
    assert resumed.protected_calls == []
    assert result(resumed)["status"] != "blocked"
    resumed.slash("skip")
    assert "ambiguous_history" not in resumed.chat[-1]
    resumed.advance(1)
    resumed.click_executor()
    assert resumed.protected_call_records == [{"name": "BuyMerchantItem", "argc": 2, "args": [1, 5]}]


def test_partial_vendor_and_mail_actions_cannot_be_skipped():
    sell = load({"kind": "vendor_sell", "itemID": 2589, "quantity": 5}, armed=True)
    sell.set_bags(bag_state([{"itemID": 2589, "stackCount": 2}, {"itemID": 2589, "stackCount": 3}]))
    open_frame(sell, "merchant")
    arm(sell)
    sell.click_executor()
    assert sell.ns.Executor.State().inFlight is True
    sell.slash("skip")
    assert sell.protected_calls == ["C_Container.UseContainerItem"]
    assert "in_flight" in sell.chat[-1]

    mail = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    mail.set_mail_count(2)
    open_frame(mail, "mailbox")
    arm(mail)
    mail.click_executor()
    assert mail.ns.Executor.State().inFlight is True
    mail.slash("skip")
    assert mail.protected_calls == ["AutoLootMailItem"]
    assert "in_flight" in mail.chat[-1]


def test_mail_empty_message_is_observed_without_advancing_index():
    client = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    client.set_mail_items([{"money": 10}, {"money": 20}])
    open_frame(client, "mailbox")
    arm(client)
    client.click_executor()
    client.advance(1)
    client.click_executor(force=True)
    assert client.protected_call_records == [
        {"name": "AutoLootMailItem", "argc": 1, "args": [1]},
        {"name": "AutoLootMailItem", "argc": 1, "args": [2]},
    ]
    assert result(client)["status"] == "succeeded"


def test_mail_removed_message_reuses_index_one():
    client = load({"kind": "mail_collect", "maxMessages": 2}, armed=True)
    client.set_mail_count(2)
    open_frame(client, "mailbox")
    arm(client)
    client.click_executor()
    client.advance(1)
    client.click_executor(force=True)
    assert client.protected_call_records == [
        {"name": "AutoLootMailItem", "argc": 1, "args": [1]},
        {"name": "AutoLootMailItem", "argc": 1, "args": [1]},
    ]
    assert result(client)["status"] == "succeeded"
