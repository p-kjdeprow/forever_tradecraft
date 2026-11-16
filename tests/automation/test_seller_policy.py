from __future__ import annotations

import json
from dataclasses import replace

import pytest
from crowley_automation.model import CancelAuction, PostCommodity, action_object
from crowley_automation.seller import (
    MAX_SELLER_CANCELS_PER_CHILD,
    SELLER_POLICY_EXAMPLE,
    MarketFacts,
    OwnedAuction,
    SellerPolicy,
    SellerPolicyError,
    StockFacts,
    decide,
)

NOW = 1_000
BOOT = "boot-a"
REQUEST = "a" * 32


def policy(*, quantity: int = 20, batch_size: int = 5) -> SellerPolicy:
    source = dict(SELLER_POLICY_EXAMPLE)
    item = dict(source["items"][0])  # type: ignore[index]
    item["quantity"] = quantity
    item["batchSize"] = batch_size
    source["items"] = [item]
    return SellerPolicy.from_json(json.dumps(source))


def market(
    *,
    priority: str = "empty",
    competing: int | None = None,
    managed: tuple[OwnedAuction, ...] = (),
    managed_quantity: int = 0,
    observed_at: int = NOW,
    complete: bool = True,
) -> MarketFacts:
    return MarketFacts(
        item_key="123",
        request_id=REQUEST,
        client_boot_id=BOOT,
        sequence=10,
        observed_at=observed_at,
        complete=complete,
        priority=priority,  # type: ignore[arg-type]
        best_competing_unit_price_copper=competing,
        managed_unit_price_copper=150 if managed else None,
        managed_active_quantity=managed_quantity,
        managed_auction_ids=tuple(row.auction_id for row in managed),
        managed_auctions=managed,
    )


def stock(
    *,
    admitted: int = 20,
    bag: int = 20,
    active: int = 0,
    returning: int = 0,
    unresolved: int = 0,
    sold: int = 0,
    recycling: bool = False,
) -> StockFacts:
    return StockFacts("123", admitted, bag, active, returning, unresolved, sold, recycling=recycling)


def test_policy_has_canonical_digest_and_strict_schema() -> None:
    parsed = SellerPolicy.from_json(json.dumps(SELLER_POLICY_EXAMPLE))
    assert parsed.object() == SELLER_POLICY_EXAMPLE
    assert len(parsed.policy_id) == 64
    assert parsed.canonical == json.dumps(
        parsed.object(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )

    with pytest.raises(SellerPolicyError, match="duplicate JSON key"):
        SellerPolicy.from_json('{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(SellerPolicyError, match="only exact base itemKey"):
        source = dict(SELLER_POLICY_EXAMPLE)
        item = dict(source["items"][0])  # type: ignore[index]
        item["itemKey"] = "123:b2.2"
        source["items"] = [item]
        SellerPolicy.from_json(json.dumps(source))
    with pytest.raises(SellerPolicyError, match="fixed at 60"):
        source = dict(SELLER_POLICY_EXAMPLE)
        source["intervalSeconds"] = 30
        SellerPolicy.from_json(json.dumps(source))


def test_competitor_price_is_matched_exactly_and_floor_pauses() -> None:
    selected = policy()
    item = selected.items[0]
    competing = decide(selected, item, market(priority="competitor", competing=121), stock(), NOW)
    assert competing.kind == "post"
    assert competing.quantity == 5
    assert competing.unit_price_copper == 121
    assert isinstance(competing.actions[0], PostCommodity)
    assert action_object(competing.actions[0])["unitPriceCopper"] == 121

    below_floor = decide(selected, item, market(priority="competitor", competing=99), stock(), NOW)
    assert below_floor.kind == "pause"
    assert below_floor.actions == ()
    assert "minimum" in below_floor.reason


def test_empty_market_requires_explicit_price_and_owned_priority_waits() -> None:
    selected = policy()
    item = selected.items[0]
    no_price = replace(item, start_unit_price_copper=None)
    no_start = replace(selected, items=(no_price,))
    paused = decide(no_start, no_price, market(), stock(), NOW)
    assert paused.kind == "pause"
    assert paused.actions == ()

    own = OwnedAuction(9, "123", 5, 150, "active", True, True, 2)
    wait = decide(
        selected,
        item,
        market(priority="ours", managed=(own,), managed_quantity=5),
        stock(bag=15, active=5),
        NOW,
    )
    assert wait.kind == "wait"
    assert wait.actions == ()


def test_exact_managed_recycle_and_floor_block_recycling() -> None:
    selected = policy()
    item = selected.items[0]
    managed = OwnedAuction(77, "123", 5, 150, "active", True, True, 2)
    observed = market(priority="competitor", competing=125, managed=(managed,), managed_quantity=5)
    recycle = decide(selected, item, observed, stock(bag=0, active=5, sold=15), NOW)
    assert recycle.kind == "recycle"
    assert recycle.actions == (CancelAuction(77, item.max_cancel_cost_copper),)

    below_floor = decide(
        selected,
        item,
        replace(observed, best_competing_unit_price_copper=99),
        stock(bag=0, active=5, sold=15),
        NOW,
    )
    assert below_floor.kind == "pause"
    assert below_floor.actions == ()

    unknown_tie = decide(
        selected,
        item,
        market(priority="unknown", competing=150, managed=(managed,), managed_quantity=5),
        stock(bag=0, active=5, sold=15),
        NOW,
    )
    assert unknown_tie.kind == "recycle"
    assert unknown_tie.actions == (CancelAuction(77, item.max_cancel_cost_copper),)
    assert "priority unobserved" in unknown_tie.reason


def test_unknown_priority_incomplete_and_stale_pause_but_sold_stock_finishes() -> None:
    selected = policy()
    item = selected.items[0]
    assert decide(selected, item, market(priority="unknown"), stock(), NOW).kind == "pause"
    assert decide(selected, item, market(complete=False), stock(), NOW).kind == "pause"
    assert decide(selected, item, market(observed_at=NOW - 61), stock(), NOW).kind == "pause"

    finished = decide(
        selected,
        item,
        market(priority="unknown"),
        stock(bag=0, sold=20),
        NOW,
    )
    assert finished.kind == "finish"
    assert finished.actions == ()


def test_unknown_priority_matches_once_per_minute_and_never_undercuts() -> None:
    selected = policy()
    item = selected.items[0]
    ready = stock()
    unknown_tie = market(priority="unknown", competing=150)
    matched = decide(selected, item, unknown_tie, ready, NOW)
    assert matched.kind == "post"
    assert matched.unit_price_copper == 150

    cooling_down = replace(ready, last_post_attempt_at=NOW - 59)
    wait = decide(selected, item, unknown_tie, cooling_down, NOW)
    assert wait.kind == "wait"
    assert wait.actions == ()
    assert "interval" in wait.reason

    cheaper_listing = OwnedAuction(9, "123", 5, 149, "active", True, True, 2)
    market_with_cheaper_managed = replace(
        unknown_tie,
        managed_unit_price_copper=149,
        managed_active_quantity=5,
        managed_auction_ids=(9,),
        managed_auctions=(cheaper_listing,),
    )
    strict_undercut = decide(selected, item, market_with_cheaper_managed, stock(bag=15, active=5), NOW)
    assert strict_undercut.kind == "wait"
    assert strict_undercut.actions == ()


def test_recycle_continuation_uses_exact_managed_rows_when_priority_changes() -> None:
    selected = policy()
    item = selected.items[0]
    remaining = OwnedAuction(78, "123", 5, 150, "active", True, True, 2)
    changed_priority = market(priority="ours", managed=(remaining,), managed_quantity=5)
    continuation = decide(
        selected,
        item,
        changed_priority,
        stock(bag=0, active=5, sold=15, recycling=True),
        NOW,
    )
    assert continuation.kind == "recycle"
    assert continuation.actions == (CancelAuction(78, item.max_cancel_cost_copper),)


def test_large_managed_set_is_cancelled_in_exact_bounded_chunks() -> None:
    selected = policy(quantity=40, batch_size=5)
    item = selected.items[0]
    managed = tuple(
        OwnedAuction(auction_id, "123", 1, 150, "active", True, True, 1) for auction_id in range(100, 140)
    )
    facts = market(
        priority="competitor",
        competing=125,
        managed=managed,
        managed_quantity=len(managed),
    )
    decision = decide(selected, item, facts, stock(admitted=40, bag=0, active=40), NOW)
    assert decision.kind == "recycle"
    assert len(decision.actions) == MAX_SELLER_CANCELS_PER_CHILD
    assert tuple(
        action.auction_id for action in decision.actions if isinstance(action, CancelAuction)
    ) == tuple(range(100, 100 + MAX_SELLER_CANCELS_PER_CHILD))
    assert decision.remaining_auction_ids == tuple(range(100 + MAX_SELLER_CANCELS_PER_CHILD, 140))


def test_conservation_rejects_stock_inflation() -> None:
    with pytest.raises(SellerPolicyError, match="conserve"):
        stock(admitted=20, bag=21)
