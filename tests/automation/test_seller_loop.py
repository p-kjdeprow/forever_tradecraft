from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

import pytest
from crowley_automation.ledger import Ledger
from crowley_automation.model import (
    Action,
    CancelAuction,
    CollectMail,
    CollectReturn,
    MoveRoute,
    PostCommodity,
)
from crowley_automation.runner import Admission, AutomationRunner, DispatchReceipt, RunnerConfig
from crowley_automation.seller import (
    SELLER_POLICY_EXAMPLE,
    MarketFacts,
    OwnedAuction,
    ReturnCandidate,
    ReturnObservationFacts,
    SellerPolicy,
)
from crowley_automation.seller_loop import (
    SellerLoop,
    SellerLoopError,
    _FairItemSchedule,
    inventory_facts_for_policy,
    validate_seller_mailbox_runtime,
    validate_seller_runtime,
)
from crowley_automation.seller_native import (
    NativeObservation,
    OwnedAuctionFact,
    ReturnFact,
    StockFact,
)
from crowley_automation.seller_store import SellerStore


def test_schedule_checks_each_item_once_and_from_completion_time() -> None:
    schedule = _FairItemSchedule(("item-a", "item-b"), started_monotonic=10.0)

    assert schedule.next_due(10.0).item_key == "item-a"  # type: ignore[union-attr]
    assert schedule.observed("item-a", started_monotonic=10.0, completed_monotonic=13.0) == 0
    assert schedule.next_due(13.0).item_key == "item-b"  # type: ignore[union-attr]
    schedule.observed("item-b", started_monotonic=13.0, completed_monotonic=15.0)

    assert schedule.next_due(69.0) is None
    assert schedule.next_delay(69.0) == 4.0
    assert schedule.next_due(73.0).item_key == "item-a"  # type: ignore[union-attr]


def test_schedule_reports_overdue_checks_without_catch_up_bursts() -> None:
    schedule = _FairItemSchedule(("item-a", "item-b", "item-c"), started_monotonic=0.0)

    assert schedule.observed("item-a", started_monotonic=75.0, completed_monotonic=80.0) == 75.0
    assert schedule.next_due(80.0).item_key == "item-b"  # type: ignore[union-attr]
    schedule.observed("item-b", started_monotonic=80.0, completed_monotonic=90.0)
    assert schedule.next_due(90.0).item_key == "item-c"  # type: ignore[union-attr]
    schedule.observed("item-c", started_monotonic=90.0, completed_monotonic=95.0)

    # Every next slot starts at least sixty seconds after its own successful read.
    assert schedule.next_due(95.0) is None
    assert schedule.next_due(140.0).item_key == "item-a"  # type: ignore[union-attr]
    assert schedule.slot("item-a").last_success_monotonic == 80.0


def test_failed_observation_is_retried_after_one_interval_but_keeps_last_success() -> None:
    schedule = _FairItemSchedule(("item-a",), started_monotonic=5.0)
    schedule.observed("item-a", started_monotonic=5.0, completed_monotonic=6.0)
    schedule.failed("item-a", completed_monotonic=25.0)

    slot = schedule.slot("item-a")
    assert slot.last_success_monotonic == 6.0
    assert slot.due_monotonic == 85.0
    assert schedule.next_due(84.999) is None
    assert schedule.next_due(85.0).item_key == "item-a"  # type: ignore[union-attr]


def test_schedule_rejects_unknown_items_and_nonmonotonic_completion() -> None:
    schedule = _FairItemSchedule(("item-a",), started_monotonic=0)

    with pytest.raises(ValueError, match="not admitted"):
        schedule.failed("item-b", completed_monotonic=1.0)
    with pytest.raises(ValueError, match="precedes"):
        schedule.observed("item-a", started_monotonic=2.0, completed_monotonic=1.0)


def test_seller_runtime_requires_measured_auction_house_source() -> None:
    policy = seller_policy(("123",))
    ah = type(
        "Target",
        (),
        {
            "kind": "npc",
            "expected_ui": "ah",
            "house": policy.house,
            "character": policy.character,
            "client_build": policy.client_build,
            "instance_id": 1,
            "guid": "NPC-1",
            "name": "Auctioneer Jaxon",
        },
    )()
    mailbox = type(
        "Target",
        (),
        {
            "kind": "mailbox",
            "expected_ui": "mail",
            "house": policy.house,
            "character": policy.character,
            "client_build": policy.client_build,
        },
    )()
    route = type(
        "Route",
        (),
        {"house": policy.house, "character": policy.character, "client_build": policy.client_build},
    )()
    backend = type(
        "Backend",
        (),
        {
            "targets": {policy.ah_target_id: ah, policy.mailbox_target_id: mailbox},
            "routes": {policy.outbound_route_id: route, policy.return_route_id: route},
            "bind_seller_ah_target": lambda self, target_id: setattr(self, "bound_target", target_id),
        },
    )()
    config = type(
        "Config",
        (),
        {
            "house": policy.house,
            "character": policy.character,
            "build": f"1.60.1.{policy.client_build}",
        },
    )()
    observation = {
        "context": {"clientBuild": policy.client_build, "character": policy.character},
        "ahOpen": True,
        "motion": {
            "instance": 1,
            "target": {
                "present": True,
                "guid": "NPC-1",
                "name": "Auctioneer Jaxon",
                "rangeAvailable": True,
                "withinRange": True,
            },
        },
        "auctioneer": {
            "targetPresent": True,
            "targetName": "Auctioneer Jaxon",
            "rangeAvailable": True,
            "withinTradeRange": True,
        },
    }

    validate_seller_runtime(policy, config, observation, backend)
    assert backend.bound_target == policy.ah_target_id

    observation["motion"]["target"]["guid"] = "another-npc"  # type: ignore[index]
    with pytest.raises(SellerLoopError, match="measured NPC source"):
        validate_seller_runtime(policy, config, observation, backend)


def test_mailbox_resume_requires_measured_mail_ui_and_anchor() -> None:
    from types import SimpleNamespace

    policy = seller_policy(("123",))
    ah = SimpleNamespace(
        kind="npc",
        expected_ui="ah",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )
    mailbox = SimpleNamespace(
        kind="mailbox",
        expected_ui="mail",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
        instance_id=4,
        anchor_x=10.0,
        anchor_y=20.0,
        max_distance=3.0,
    )
    route = SimpleNamespace(
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )
    backend = SimpleNamespace(
        targets={policy.ah_target_id: ah, policy.mailbox_target_id: mailbox},
        routes={policy.outbound_route_id: route, policy.return_route_id: route},
        bind_seller_ah_target=lambda _target_id: None,
    )
    config = SimpleNamespace(
        house=policy.house,
        character=policy.character,
        build=f"1.60.1.{policy.client_build}",
    )
    observation = {
        "context": {"character": policy.character, "clientBuild": policy.client_build},
        "mailOpen": True,
        "motion": {
            "instance": 4,
            "moving": False,
            "dead": False,
            "inCombat": False,
            "x": 11.0,
            "y": 21.0,
        },
    }

    validate_seller_mailbox_runtime(policy, config, observation, backend)

    observation["motion"]["x"] = 14.0  # type: ignore[index]
    with pytest.raises(SellerLoopError, match="measured mailbox anchor"):
        validate_seller_mailbox_runtime(policy, config, observation, backend)


BOOT = "seller-test-boot"
NOW = 1_000


def seller_policy(
    item_keys: tuple[str, ...],
    *,
    quantities: dict[str, int] | None = None,
    batch_sizes: dict[str, int] | None = None,
) -> SellerPolicy:
    source = copy.deepcopy(SELLER_POLICY_EXAMPLE)
    template = dict(source["items"][0])  # type: ignore[index]
    source["expiresAt"] = SELLER_POLICY_EXAMPLE["expiresAt"]
    source["maxTotalSpendCopper"] = 10_000
    source["items"] = [
        {
            **template,
            "itemKey": item_key,
            "quantity": (quantities or {}).get(item_key, 5),
            "batchSize": (batch_sizes or {}).get(item_key, 5),
        }
        for item_key in item_keys
    ]
    return SellerPolicy.from_json(json.dumps(source))


class _Clock:
    def __init__(self) -> None:
        self.mono = 0.0

    def monotonic(self) -> float:
        return self.mono

    def wall_time(self) -> float:
        return NOW + self.mono

    def sleep(self, seconds: float) -> None:
        # Controller sleeps in one-second interruptible slices; jump each slice
        # to the next minute boundary so long seller schedules stay fast in tests.
        self.mono += seconds * 60.0


@dataclass
class _Listing:
    auction_id: int
    item_key: str
    quantity: int
    unit_price: int
    active: bool = True
    sold: bool = False


@dataclass
class _World:
    io: _Clock
    policy: SellerPolicy
    bag: dict[str, int] = field(default_factory=dict)
    listings: list[_Listing] = field(default_factory=list)
    pending_returns: dict[int, _Listing] = field(default_factory=dict)
    collected_returns: set[int] = field(default_factory=set)
    return_selection_tokens: dict[str, int] = field(default_factory=dict)
    collected_return_tokens: list[str] = field(default_factory=list)
    posts_by_item: dict[str, int] = field(default_factory=dict)
    post_prices_by_item: dict[str, list[int]] = field(default_factory=dict)
    post_times_by_item: dict[str, list[float]] = field(default_factory=dict)
    cancellations: list[int] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    next_auction_id: int = 100
    sell_after: float | None = None
    force_competitor: set[str] = field(default_factory=set)
    recycling_competitors: set[str] = field(default_factory=set)
    opening_competitors: dict[str, int] = field(default_factory=dict)
    partial_cancel_remaining_by_item: dict[str, int] = field(default_factory=dict)
    unknown_equal_price: set[str] = field(default_factory=set)
    switch_to_ours_after_cancellations: int | None = None
    delay_returns: bool = False
    delayed_return_auction_ids: set[int] = field(default_factory=set)
    no_return_bag_slots: bool = False
    stage_return_refusal: bool = False
    staged_actions: list[Action] = field(default_factory=list)
    fail_return_route_once: bool = False


class _SimulatedBackend:
    """Runner seam with causal receipts; never used by production eligibility code."""

    def __init__(self, io: _Clock, world: _World, policy: SellerPolicy) -> None:
        self.io = io
        self.world = world
        self.policy = policy
        self.stop_requested = lambda: False
        self.deadline = policy.expires_at
        self.staged: tuple[str, int, int, str] | None = None
        self.sequence = 0
        self.ah_target_id: str | None = None

    def set_deadline(self, expires_at: int) -> None:
        self.deadline = expires_at

    def bind_seller_ah_target(self, target_id: str) -> None:
        self.ah_target_id = target_id

    def observe(self) -> Admission:
        if self.stop_requested():
            from crowley_automation.backend import DesktopBackendError

            raise DesktopBackendError("simulated durable session stop")
        self.sequence += 1
        staged = self.staged
        return Admission(
            session_boot_id=BOOT,
            house=self.policy.house,
            character=self.policy.character,
            observed_at=int(self.io.wall_time()),
            human_present=False,
            focused=True,
            gui_lock_owned=True,
            client_ah_owned=True,
            staged_batch_id=None if staged is None else staged[0],
            staged_ordinal=None if staged is None else staged[1],
            staged_step_index=None if staged is None else staged[2],
            staged_action_digest=None if staged is None else staged[3],
            ready=None if staged is None else True,
            receipt_sequence=self.sequence,
            client_build=self.policy.client_build,
        )

    def stage(
        self,
        action: Action,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        self.world.staged_actions.append(action)
        if isinstance(action, CollectReturn) and self.world.stage_return_refusal:
            from crowley_automation.backend import DesktopBackendError

            raise DesktopBackendError(
                "native preflight refused operation: insufficient_return_bag_slots",
                code="insufficient_return_bag_slots",
            )
        self.staged = (batch_id, ordinal, step_index, action_digest)

    def dispatch(
        self,
        action: Action,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
        admission: Admission,
    ) -> DispatchReceipt:
        self.world.actions.append(action)
        result: dict[str, object] | None = None
        callback_delivered: bool | None = True
        postcondition: bool | None = True
        action_complete: bool | None = True
        detail = "simulated_exact_effect"
        if isinstance(action, PostCommodity):
            self.world.next_auction_id += 1
            listing = _Listing(
                self.world.next_auction_id,
                action.item_key,
                action.quantity,
                action.unit_price_copper,
            )
            self.world.listings.append(listing)
            self.world.bag[action.item_key] -= action.quantity
            self.world.posts_by_item[action.item_key] = self.world.posts_by_item.get(action.item_key, 0) + 1
            self.world.post_prices_by_item.setdefault(action.item_key, []).append(action.unit_price_copper)
            self.world.post_times_by_item.setdefault(action.item_key, []).append(self.world.io.mono)
            result = {
                "kind": "post_commodity",
                "auctionId": listing.auction_id,
                "itemKey": action.item_key,
                "quantity": action.quantity,
                "unitPriceCopper": action.unit_price_copper,
                "depositCopper": 1,
            }
        elif isinstance(action, CancelAuction):
            listing = next(row for row in self.world.listings if row.auction_id == action.auction_id)
            assert listing.active
            listing.active = False
            remaining = self.world.partial_cancel_remaining_by_item.get(listing.item_key, listing.quantity)
            self.world.pending_returns[listing.auction_id] = _Listing(
                listing.auction_id,
                listing.item_key,
                remaining,
                listing.unit_price,
                active=False,
            )
            self.world.cancellations.append(listing.auction_id)
            result = {
                "kind": "cancel_auction",
                "auctionId": listing.auction_id,
                "itemKey": listing.item_key,
                "remainingQuantity": remaining,
                "cancelCostCopper": 1,
            }
        elif isinstance(action, CollectReturn):
            auction_id = self.world.return_selection_tokens.pop(action.mail_fingerprint, None)
            assert auction_id is not None
            listing = self.world.pending_returns[auction_id]
            assert auction_id not in self.world.collected_returns
            assert action.item_key == listing.item_key and action.quantity == listing.quantity
            if self.world.no_return_bag_slots:
                callback_delivered = False
                postcondition = False
                action_complete = False
                detail = "insufficient_return_bag_slots"
            else:
                self.world.bag[action.item_key] = self.world.bag.get(action.item_key, 0) + action.quantity
                self.world.collected_returns.add(auction_id)
                self.world.collected_return_tokens.append(action.mail_fingerprint)
                result = {
                    "kind": "collect_return",
                    "mailFingerprint": action.mail_fingerprint,
                    "itemKey": action.item_key,
                    "quantity": action.quantity,
                }
        elif isinstance(action, MoveRoute) and action.route_id == self.policy.return_route_id:
            if self.world.fail_return_route_once:
                self.world.fail_return_route_once = False
                callback_delivered = False
                postcondition = False
                action_complete = False
                detail = "simulated_route_no_effect"
        return DispatchReceipt(
            batch_id,
            ordinal,
            step_index,
            action_digest,
            admission.session_boot_id,
            callback_delivered,
            postcondition,
            action_complete,
            detail,
            result,
        )


class _SimulatedNative:
    """Finite native snapshot source for controller/store/runner cycle tests."""

    def __init__(self, world: _World) -> None:
        self.world = world
        self.sequence = 10

    def _next(self) -> tuple[int, int]:
        self.sequence += 1
        return self.sequence, int(self.world.io.wall_time())

    def inventory(self) -> NativeObservation[StockFact]:
        sequence, captured = self._next()
        facts = tuple(
            StockFact(item_key, f"|Hitem:{item_key}:0|h[test]|h", quantity, 0, index + 1, False, False, True)
            for index, (item_key, quantity) in enumerate(sorted(self.world.bag.items()))
            if quantity > 0
        )
        return NativeObservation(f"inventory:{sequence}", BOOT, sequence, captured, True, facts)

    def owned(self) -> NativeObservation[OwnedAuctionFact]:
        sequence, captured = self._next()
        rows: list[OwnedAuctionFact] = []
        for listing in self.world.listings:
            if (
                listing.active
                and self.world.sell_after is not None
                and self.world.io.mono >= self.world.sell_after
            ):
                listing.active = False
                listing.sold = True
            if not listing.active and not listing.sold:
                continue
            rows.append(
                OwnedAuctionFact(
                    listing.auction_id,
                    listing.item_key,
                    f"|Hitem:{listing.item_key}:0|h[test]|h",
                    0 if listing.sold else listing.quantity,
                    "sold" if listing.sold else "active",
                    True,
                    listing.unit_price * listing.quantity,
                    True,
                    True,
                    1,
                )
            )
        facts = tuple(rows)
        return NativeObservation(f"owned:{sequence}", BOOT, sequence, captured, True, facts)

    def returns(self, item_key: str) -> NativeObservation[ReturnFact]:
        sequence, captured = self._next()
        # Each fresh inbox observation issues new opaque one-snapshot tokens. A later
        # observation invalidates every token from the previous snapshot.
        self.world.return_selection_tokens.clear()
        facts_list = []
        for auction_id, listing in sorted(self.world.pending_returns.items()):
            if listing.item_key != item_key or auction_id in self.world.collected_returns:
                continue
            if self.world.delay_returns or auction_id in self.world.delayed_return_auction_ids:
                continue
            token = hashlib.sha256(f"inbox:{sequence}:{auction_id}".encode("ascii")).hexdigest()
            self.world.return_selection_tokens[token] = auction_id
            facts_list.append(
                ReturnFact(
                    token,
                    listing.item_key,
                    listing.quantity,
                    True,
                    "simulated exact commodity and full attachment selection token",
                )
            )
        facts = tuple(facts_list)
        return NativeObservation(f"returns:{sequence}", BOOT, sequence, captured, True, facts)

    def market(self, item_key: str, request_id: str, managed_ids: frozenset[int]) -> MarketFacts:
        sequence, captured = self._next()
        listings = tuple(
            OwnedAuction(
                listing.auction_id,
                listing.item_key,
                listing.quantity,
                listing.unit_price,
                "active",
                True,
                True,
                1,
            )
            for listing in sorted(self.world.listings, key=lambda row: row.auction_id)
            if listing.active and listing.item_key == item_key and listing.auction_id in managed_ids
        )
        post_count = self.world.posts_by_item.get(item_key, 0)
        if (
            self.world.switch_to_ours_after_cancellations is not None
            and len(self.world.cancellations) >= self.world.switch_to_ours_after_cancellations
            and listings
        ):
            priority, competitor = "ours", None
        elif item_key in self.world.unknown_equal_price:
            priority = "unknown"
            competitor = self.world.opening_competitors.get(item_key) or min(
                (row.unit_price for row in listings), default=None
            )
        elif item_key in self.world.force_competitor:
            priority, competitor = "competitor", 125
        elif item_key in self.world.opening_competitors and post_count == 0:
            priority, competitor = "competitor", self.world.opening_competitors[item_key]
        elif item_key in self.world.recycling_competitors and post_count == 1:
            priority, competitor = "competitor", 125
        elif listings:
            priority, competitor = "ours", None
        else:
            priority, competitor = "empty", None
        if item_key in self.world.recycling_competitors and post_count == 1 and not listings:
            # After cancellation, the returned batch still faces the same competitor.
            priority, competitor = "competitor", 125
        return MarketFacts(
            item_key,
            request_id,
            BOOT,
            sequence,
            captured,
            True,
            priority,  # type: ignore[arg-type]
            competitor,
            min((row.unit_price_copper for row in listings), default=None),
            sum(row.remaining_quantity for row in listings),
            tuple(sorted(managed_ids)),
            listings,
        )


def _start_session(
    tmp_path,
    policy: SellerPolicy,
    world: _World,
    progress=None,
) -> tuple[SellerLoop, SellerStore]:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = SellerStore(ledger)
    native = _SimulatedNative(world)
    initial = inventory_facts_for_policy(policy, native.inventory())
    session_id = store.start(
        policy,
        boot_id=BOOT,
        client_build=policy.client_build,
        confirmed_policy_id=policy.policy_id,
        initial_inventory=initial,
        now=NOW,
    )
    backend = _SimulatedBackend(world.io, world, policy)
    runner = AutomationRunner(
        ledger,
        RunnerConfig(
            frozenset({policy.ah_target_id, policy.mailbox_target_id}),
            tuple(sorted(((policy.outbound_route_id, 1), (policy.return_route_id, 1)))),
        ),
        clock=lambda: int(world.io.wall_time()),
    )
    loop = SellerLoop(
        policy,
        session_id,
        store=store,
        ledger=ledger,
        runner=runner,
        backend=backend,  # type: ignore[arg-type]
        native=native,
        progress=progress,
    )
    return loop, store


def _resume_paused_mailbox_loop(loop: SellerLoop, store: SellerStore) -> tuple[SellerLoop, SellerStore]:
    policy = loop.policy
    native = loop.native
    inventory_observation = native.inventory()
    mail_observation = native.returns("123")
    candidates = tuple(
        ReturnCandidate(
            row.item_key,
            row.quantity,
            row.mail_fingerprint,
            mail_observation.request_id,
            mail_observation.boot,
            mail_observation.sequence,
            mail_observation.captured_at,
            mail_observation.complete,
            row.eligible,
            row.reason,
        )
        for row in mail_observation.facts
    )
    mail_facts = ReturnObservationFacts(
        "123",
        mail_observation.request_id,
        mail_observation.boot,
        mail_observation.sequence,
        mail_observation.captured_at,
        mail_observation.complete,
        candidates,
    )
    resumed_store = SellerStore(loop.ledger)
    resumed_store.resume_at_mailbox(
        loop.session_id,
        confirmed_policy_id=policy.policy_id,
        boot_id=BOOT,
        client_build=policy.client_build,
        inventories=inventory_facts_for_policy(policy, inventory_observation),
        mail_observations=(mail_facts,),
        now=int(loop.io.wall_time()),
    )
    resumed_loop = SellerLoop(
        policy,
        loop.session_id,
        store=resumed_store,
        ledger=loop.ledger,
        runner=loop.runner,
        backend=loop.backend,
        native=native,
        at_mailbox_item_key="123",
    )
    return resumed_loop, resumed_store


def _resume_paused_ah_loop(loop: SellerLoop, store: SellerStore) -> tuple[SellerLoop, SellerStore]:
    policy = loop.policy
    native = loop.native
    inventories = []
    owned_facts = []
    for item in policy.items:
        inventory, owned = loop._fresh_snapshot(item)
        inventories.append(inventory)
        owned_facts.append(owned)
    resumed_store = SellerStore(loop.ledger)
    resumed_store.resume(
        loop.session_id,
        confirmed_policy_id=policy.policy_id,
        boot_id=BOOT,
        client_build=policy.client_build,
        inventories=tuple(inventories),
        owned_facts=tuple(owned_facts),
        now=int(loop.io.wall_time()),
    )
    resumed_loop = SellerLoop(
        policy,
        loop.session_id,
        store=resumed_store,
        ledger=loop.ledger,
        runner=loop.runner,
        backend=loop.backend,
        native=native,
    )
    return resumed_loop, resumed_store


def test_one_item_post_sale_cycle_finishes_from_fresh_managed_status(tmp_path) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(io, policy, bag={"123": 5}, sell_after=60.0)
    loop, store = _start_session(tmp_path, policy, world)

    result = loop.run()

    assert result["driverState"] == "completed", result
    assert result["session"]["state"] == "completed"  # type: ignore[index]
    assert world.posts_by_item == {"123": 1}
    assert store.item_progress(result["session"]["session_id"], "123").sold_quantity == 5  # type: ignore[index]
    assert all(
        not isinstance(action, (CancelAuction, CollectReturn, CollectMail)) for action in world.actions
    )


def test_two_item_recycle_collects_only_item_a_return_and_preserves_item_b(tmp_path) -> None:
    policy = seller_policy(("456", "123"))
    io = _Clock()
    world = _World(io, policy, bag={"123": 5, "456": 5}, recycling_competitors={"123"})
    holder: dict[str, SellerStore] = {}

    def stop_after_returned_item_is_relisted(event: dict[str, object]) -> None:
        if (
            event.get("event") == "seller_decision"
            and event.get("itemKey") == "123"
            and event.get("kind") == "wait"
            and world.posts_by_item.get("123") == 2
        ):
            holder["store"].revoke(
                holder["session_id"],
                "test operator stop after complete recycle cycle",
                now=int(io.wall_time()),
            )

    loop, store = _start_session(tmp_path, policy, world, stop_after_returned_item_is_relisted)
    holder["store"] = store
    holder["session_id"] = loop.session_id

    result = loop.run()

    item_a_ids = {row.auction_id for row in world.listings if row.item_key == "123"}
    item_b_ids = {row.auction_id for row in world.listings if row.item_key == "456"}
    assert result["driverState"] == "stopped", result
    assert result["session"]["state"] == "revoked"  # type: ignore[index]
    assert len(world.cancellations) == 1
    assert world.cancellations[0] in item_a_ids
    assert world.cancellations[0] not in item_b_ids
    assert world.collected_returns == {world.cancellations[0]}
    assert len(world.collected_return_tokens) == 1
    assert len(world.collected_return_tokens[0]) == 64
    assert world.collected_return_tokens[0] not in world.return_selection_tokens
    assert world.posts_by_item["123"] == 2
    assert world.posts_by_item["456"] == 1
    assert store.item_progress(loop.session_id, "456").managed_active_quantity == 5
    assert [action.item_key for action in world.actions if isinstance(action, CollectReturn)] == ["123"]
    assert not any(isinstance(action, CollectMail) for action in world.actions)


def test_large_managed_set_is_cancelled_in_five_exact_chunks_without_touching_other_item(tmp_path) -> None:
    policy = seller_policy(("456", "123"), quantities={"456": 1, "123": 40}, batch_sizes={"456": 1, "123": 1})
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 40, "456": 1},
        force_competitor={"123"},
        switch_to_ours_after_cancellations=8,
    )
    holder: dict[str, SellerStore] = {}
    decisions: list[dict[str, object]] = []
    continuation_chunks: list[dict[str, object]] = []
    check_count = 0
    target_reached = False

    def stop_after_complete_recycle(event: dict[str, object]) -> None:
        nonlocal check_count, target_reached
        if event.get("event") == "seller_decision":
            decisions.append(event)
        if event.get("event") == "seller_recycle_continuation":
            continuation_chunks.append(event)
        if event.get("event") == "seller_item_checked":
            check_count += 1
            if (check_count > 150 or io.mono > 9_000) and not target_reached:
                holder["store"].revoke(
                    holder["session_id"],
                    "test progress bound reached before recycle completion",
                    now=int(io.wall_time()),
                )
        if (
            event.get("event") == "seller_decision"
            and event.get("itemKey") == "123"
            and event.get("kind") == "post"
            and world.posts_by_item.get("123") == 40
            and len(world.cancellations) == 40
        ):
            target_reached = True
            holder["store"].revoke(
                holder["session_id"],
                "test operator stop after forty-listing cancellation pass",
                now=int(io.wall_time()),
            )

    loop, store = _start_session(tmp_path, policy, world, stop_after_complete_recycle)
    holder["store"] = store
    holder["session_id"] = loop.session_id

    result = loop.run()

    item_a_ids = {row.auction_id for row in world.listings if row.item_key == "123"}
    item_b_ids = {row.auction_id for row in world.listings if row.item_key == "456"}
    assert result["session"]["state"] == "revoked"  # type: ignore[index]
    assert world.posts_by_item == {"456": 1, "123": 40}
    assert len(item_a_ids) == 40
    assert len(world.cancellations) == 40
    assert set(world.cancellations) == item_a_ids
    assert not (set(world.cancellations) & item_b_ids)
    assert len([action for action in world.actions if isinstance(action, CollectReturn)]) == 40
    assert len(set(world.collected_return_tokens)) == 40
    assert all(len(token) == 64 for token in world.collected_return_tokens)
    assert not any(isinstance(action, CollectMail) for action in world.actions)
    assert store.item_progress(loop.session_id, "123").recycle_rounds == 1
    assert store.item_progress(loop.session_id, "456").managed_active_quantity == 1
    assert target_reached
    assert check_count <= 150
    assert io.mono <= 9_000
    assert [len(row["auctionIds"]) for row in continuation_chunks] == [8, 8, 8, 8]
    assert [row["phase"] for row in continuation_chunks] == [
        "continue_recycle:1",
        "continue_recycle:2",
        "continue_recycle:3",
        "continue_recycle:4",
    ]
    assert sum(len(row["auctionIds"]) for row in continuation_chunks) == 32
    assert not any("continue the persisted recycle pass" in str(row.get("reason")) for row in decisions)


def test_sold_leading_batch_reuses_last_causal_price_when_market_is_empty(tmp_path) -> None:
    policy = seller_policy(("123",), quantities={"123": 20}, batch_sizes={"123": 5})
    source = policy.object()
    item = dict(source["items"][0])  # type: ignore[index]
    item["startUnitPriceCopper"] = None
    source["items"] = [item]
    policy = SellerPolicy.from_json(json.dumps(source))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 20},
        sell_after=60.0,
        opening_competitors={"123": 100},
    )
    holder: dict[str, SellerStore] = {}

    def stop_after_second_batch(event: dict[str, object]) -> None:
        if (
            event.get("event") == "seller_child_finished"
            and event.get("phase") == "decision"
            and world.posts_by_item.get("123") == 2
        ):
            holder["store"].revoke(
                holder["session_id"], "test stop after sold-stock price reuse", now=int(io.wall_time())
            )

    loop, store = _start_session(tmp_path, policy, world, stop_after_second_batch)
    holder["store"] = store
    holder["session_id"] = loop.session_id

    result = loop.run()

    assert result["session"]["state"] == "revoked"  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [100, 100]


def test_unknown_equal_price_allows_one_fresh_matched_batch_per_item_interval(tmp_path) -> None:
    policy = seller_policy(("123",), quantities={"123": 10}, batch_sizes={"123": 5})
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 10},
        opening_competitors={"123": 100},
        unknown_equal_price={"123"},
    )
    decisions: list[dict[str, object]] = []
    holder: dict[str, SellerStore] = {}

    def revoke_after_second_post(event: dict[str, object]) -> None:
        if event.get("event") == "seller_decision":
            decisions.append(event)
        if (
            event.get("event") == "seller_child_finished"
            and event.get("phase") == "decision"
            and world.posts_by_item.get("123") == 2
        ):
            holder["store"].revoke(
                holder["session_id"], "test stop after second matched-price batch", now=int(io.wall_time())
            )

    loop, store = _start_session(tmp_path, policy, world, revoke_after_second_post)
    holder["store"] = store
    holder["session_id"] = loop.session_id

    result = loop.run()

    assert result["session"]["state"] == "revoked"  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [100, 100]
    assert world.post_times_by_item["123"] == [0.0, 60.0]
    assert any(row["kind"] == "wait" and "post attempt interval" in str(row["reason"]) for row in decisions)
    assert any("matched-price" in str(row["reason"]) for row in decisions)


def test_unknown_equal_price_full_exhaustion_recycles_and_reposts_exact_return(tmp_path) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 5},
        opening_competitors={"123": 100},
        unknown_equal_price={"123"},
    )
    decisions: list[dict[str, object]] = []
    holder: dict[str, SellerStore] = {}

    def revoke_after_repost(event: dict[str, object]) -> None:
        if event.get("event") == "seller_decision":
            decisions.append(event)
        if (
            event.get("event") == "seller_child_finished"
            and event.get("phase") == "decision"
            and world.posts_by_item.get("123") == 2
        ):
            holder["store"].revoke(
                holder["session_id"], "test stop after unknown-tie recycle", now=int(io.wall_time())
            )

    loop, store = _start_session(tmp_path, policy, world, revoke_after_repost)
    holder["store"] = store
    holder["session_id"] = loop.session_id

    result = loop.run()

    posted_ids = {row.auction_id for row in world.listings if row.item_key == "123"}
    assert result["session"]["state"] == "revoked"  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [100, 100]
    assert world.post_times_by_item["123"] == [0.0, 60.0]
    assert len(world.cancellations) == 1
    assert world.cancellations[0] in posted_ids
    assert len(world.collected_return_tokens) == 1
    assert len(world.collected_return_tokens[0]) == 64
    assert store.item_progress(loop.session_id, "123").recycle_rounds == 1
    assert any(
        row["kind"] == "recycle"
        and row["reason"] == "scheduled matched-price refresh; queue priority unobserved"
        for row in decisions
    )
    assert [action.item_key for action in world.actions if isinstance(action, CollectReturn)] == ["123"]
    assert not any(isinstance(action, CollectMail) for action in world.actions)


def test_partial_mail_pause_resumes_at_measured_mailbox_without_replaying_route(tmp_path) -> None:
    policy = seller_policy(("123",), quantities={"123": 10}, batch_sizes={"123": 5})
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 10},
        opening_competitors={"123": 100},
        unknown_equal_price={"123"},
        delayed_return_auction_ids={102},
    )
    loop, store = _start_session(tmp_path, policy, world)

    paused = loop.run()

    assert paused["driverState"] == "paused", paused
    assert paused["session"]["state"] == "active"  # type: ignore[index]
    assert store.item_progress(loop.session_id, "123").returning_quantity == 5
    assert store.item_progress(loop.session_id, "123").bag_quantity == 5
    assert world.collected_returns == {101}
    assert len([action for action in world.actions if isinstance(action, CollectReturn)]) == 1
    assert (
        len(
            [
                action
                for action in world.actions
                if getattr(action, "route_id", None) == policy.outbound_route_id
            ]
        )
        == 1
    )

    world.delayed_return_auction_ids.clear()
    resumed_loop, resumed_store = _resume_paused_mailbox_loop(loop, store)
    holder = {"store": resumed_store}

    def stop_after_repost(event: dict[str, object]) -> None:
        if (
            event.get("event") == "seller_child_finished"
            and event.get("phase") == "decision"
            and world.posts_by_item.get("123") == 3
        ):
            holder["store"].revoke(
                loop.session_id,
                "test stop after delayed returned batch was relisted",
                now=int(io.wall_time()),
            )

    resumed_loop.progress = stop_after_repost
    resumed = resumed_loop.run()

    assert resumed["session"]["state"] == "revoked"  # type: ignore[index]
    assert world.collected_returns == {101, 102}
    assert len(world.collected_return_tokens) == 2
    assert world.posts_by_item["123"] == 3
    assert (
        len(
            [
                action
                for action in world.actions
                if getattr(action, "route_id", None) == policy.outbound_route_id
            ]
        )
        == 1
    )
    assert (
        len(
            [
                action
                for action in world.actions
                if getattr(action, "route_id", None) == policy.return_route_id
            ]
        )
        == 1
    )
    assert not any(isinstance(action, CollectMail) for action in world.actions)


def test_full_bag_return_refusal_pauses_without_retrying_until_explicit_resume(tmp_path) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 5},
        recycling_competitors={"123"},
        no_return_bag_slots=True,
    )
    loop, store = _start_session(tmp_path, policy, world)

    paused = loop.run()

    assert paused["driverState"] == "paused", paused
    assert "no free bag slots" in str(paused["reason"])
    assert store.item_progress(loop.session_id, "123").returning_quantity == 5
    first_collects = [action for action in world.actions if isinstance(action, CollectReturn)]
    assert len(first_collects) == 1

    world.no_return_bag_slots = False
    world.fail_return_route_once = True
    resumed_loop, resumed_store = _resume_paused_mailbox_loop(loop, store)

    route_paused = resumed_loop.run()

    assert route_paused["driverState"] == "paused", route_paused
    assert "explicitly resume" in str(route_paused["reason"])
    progress = store.item_progress(loop.session_id, "123")
    assert progress.returning_quantity == 0 and progress.bag_quantity == 5
    assert (
        store.child_status(loop.session_id, progress.recycle_anchor_decision_id, "travel_to_ah") == "failed"
    )

    resumed_loop, resumed_store = _resume_paused_mailbox_loop(loop, store)
    holder = {"store": resumed_store}

    def stop_after_repost(event: dict[str, object]) -> None:
        if (
            event.get("event") == "seller_child_finished"
            and event.get("phase") == "decision"
            and world.posts_by_item.get("123") == 2
        ):
            holder["store"].revoke(
                loop.session_id,
                "test stop after full-bag recovery",
                now=int(io.wall_time()),
            )

    resumed_loop.progress = stop_after_repost
    resumed = resumed_loop.run()

    assert resumed["session"]["state"] == "revoked"  # type: ignore[index]
    assert store.item_progress(loop.session_id, "123").returning_quantity == 0
    assert world.collected_returns == {101}
    assert len([action for action in world.actions if isinstance(action, CollectReturn)]) == 2
    collection_tokens = [
        action.mail_fingerprint for action in world.actions if isinstance(action, CollectReturn)
    ]
    assert collection_tokens[0] != collection_tokens[1]
    return_routes = [
        action
        for action in world.actions
        if isinstance(action, MoveRoute) and action.route_id == policy.return_route_id
    ]
    assert len(return_routes) == 2, resumed.get("reason")
    assert not any(isinstance(action, CollectMail) for action in world.actions)


def test_native_stage_capacity_refusal_pauses_mailbox_without_retrying(tmp_path) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 5},
        recycling_competitors={"123"},
        stage_return_refusal=True,
    )
    loop, store = _start_session(tmp_path, policy, world)

    result = loop.run()

    assert result["driverState"] == "paused", result
    assert "no free bag slots" in str(result["reason"])
    assert len([action for action in world.staged_actions if isinstance(action, CollectReturn)]) == 1
    assert not any(isinstance(action, CollectReturn) for action in world.actions)
    assert store.item_progress(loop.session_id, "123").returning_quantity == 5
    assert not loop.ledger.has_uncertain_effect()
    failed_return = next(
        action
        for batch in loop.ledger.all_status()
        for action in batch["actions"]
        if action["action"]["kind"] == "collect_return"
    )
    assert failed_return["state"] == "failed"
    assert failed_return["reason"] == ("native preflight refused operation: insufficient_return_bag_slots")


@pytest.mark.parametrize("resume_at", [60.0, 120.0])
def test_failed_return_route_recovers_from_fresh_ah_resume_and_reposts(tmp_path, resume_at: float) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 5},
        recycling_competitors={"123"},
        fail_return_route_once=True,
    )
    loop, store = _start_session(tmp_path, policy, world)

    paused = loop.run()

    progress_before = store.item_progress(loop.session_id, "123")
    anchor = progress_before.recycle_anchor_decision_id
    assert paused["driverState"] == "paused", paused
    assert anchor is not None
    assert progress_before.recycling
    assert progress_before.returning_quantity == 0 and progress_before.bag_quantity == 5
    assert store.child_status(loop.session_id, anchor, "travel_to_ah") == "failed"
    assert len([action for action in world.actions if isinstance(action, CollectReturn)]) == 1

    # The operator reopens the seller only after a fresh AH observation; the durable policy,
    # recycle round, stock and failed route record remain the same across either wait length.
    io.mono = resume_at
    resumed_loop, resumed_store = _resume_paused_ah_loop(loop, store)
    progress: list[dict[str, object]] = []

    def stop_after_fresh_post(event: dict[str, object]) -> None:
        progress.append(event)
        if event.get("event") == "seller_decision" and event.get("kind") == "wait":
            if world.posts_by_item.get("123") == 2:
                resumed_store.revoke(
                    loop.session_id,
                    "test stop after AH-resumed recycle post",
                    now=int(io.wall_time()),
                )

    resumed_loop.progress = stop_after_fresh_post
    resumed = resumed_loop.run()

    progress_after = store.item_progress(loop.session_id, "123")
    assert resumed["session"]["state"] == "revoked", resumed  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [150, 125]
    assert progress_after.recycle_rounds == progress_before.recycle_rounds == 1
    assert progress_after.spent_copper > progress_before.spent_copper
    assert progress_after.admitted_quantity == progress_before.admitted_quantity == 5
    assert store.child_status(loop.session_id, anchor, "travel_to_ah") == "failed"
    assert any(row.get("event") == "seller_recycle_recovered_at_ah" for row in progress)
    assert (
        sum(
            1
            for action in world.actions
            if isinstance(action, MoveRoute) and action.route_id == policy.return_route_id
        )
        == 1
    )


@pytest.mark.parametrize("resume_at", [60.0, 120.0])
def test_explicit_ah_resume_after_last_collect_before_route_preparation_reposts(
    tmp_path, resume_at: float
) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(io, policy, bag={"123": 5}, recycling_competitors={"123"})
    holder: dict[str, SellerStore] = {}

    def revoke_after_last_collection(event: dict[str, object]) -> None:
        if (
            event.get("event") == "seller_child_finished"
            and str(event.get("phase", "")).startswith("collect_return:")
            and event.get("state") == "succeeded"
        ):
            holder["store"].revoke(
                holder["session_id"],
                "test stop after final collection before return route preparation",
                now=int(io.wall_time()),
            )

    loop, store = _start_session(tmp_path, policy, world, revoke_after_last_collection)
    holder["store"] = store
    holder["session_id"] = loop.session_id
    interrupted = loop.run()

    progress_before = store.item_progress(loop.session_id, "123")
    anchor = progress_before.recycle_anchor_decision_id
    collect_action = next(action for action in world.actions if isinstance(action, CollectReturn))
    assert interrupted["driverState"] == "stopped", interrupted
    assert anchor is not None
    assert progress_before.recycling and progress_before.bag_quantity == 5
    assert progress_before.returning_quantity == 0 and progress_before.unresolved_quantity == 0
    assert (
        store.child_status(loop.session_id, anchor, f"collect_return:{collect_action.mail_fingerprint}")
        == "succeeded"
    )
    assert store.child_status(loop.session_id, anchor, "travel_to_ah") is None
    assert not any(
        isinstance(action, MoveRoute) and action.route_id == policy.return_route_id
        for action in world.actions
    )

    # Re-arm only after fresh AH proofs; the original collection receipt and policy remain durable.
    io.mono = resume_at
    resumed_loop, resumed_store = _resume_paused_ah_loop(loop, store)
    checks = 0
    recovered: list[dict[str, object]] = []

    def stop_after_post(event: dict[str, object]) -> None:
        nonlocal checks
        recovered.append(event)
        if event.get("event") == "seller_item_checked":
            checks += 1
            if checks > 10 or io.mono > resume_at + 600:
                resumed_store.revoke(
                    loop.session_id,
                    "test progress bound reached before resumed repost",
                    now=int(io.wall_time()),
                )
        if event.get("event") == "seller_decision" and event.get("kind") == "wait":
            if world.posts_by_item.get("123") == 2:
                resumed_store.revoke(
                    loop.session_id,
                    "test stop after post from AH resume",
                    now=int(io.wall_time()),
                )

    resumed_loop.progress = stop_after_post
    resumed = resumed_loop.run()

    progress_after = store.item_progress(loop.session_id, "123")
    assert resumed["session"]["session_id"] == loop.session_id  # type: ignore[index]
    assert resumed["session"]["policy_id"] == policy.policy_id  # type: ignore[index]
    assert resumed["session"]["state"] == "revoked", resumed  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [150, 125]
    assert progress_after.admitted_quantity == progress_before.admitted_quantity == 5
    assert progress_after.recycle_rounds == progress_before.recycle_rounds == 1
    assert progress_after.spent_copper > progress_before.spent_copper
    assert progress_after.reserved_spend_copper == 0
    assert not progress_after.recycling and progress_after.recycle_anchor_decision_id is None
    assert store.child_status(loop.session_id, anchor, "travel_to_ah") is None
    assert any(row.get("event") == "seller_recycle_recovered_at_ah" for row in recovered)
    assert checks <= 10 and io.mono <= resume_at + 600


def test_ah_resume_reposts_known_partial_return_bag_and_preserves_unresolved_unit(tmp_path) -> None:
    policy = seller_policy(("123",))
    io = _Clock()
    world = _World(
        io,
        policy,
        bag={"123": 5},
        recycling_competitors={"123"},
        partial_cancel_remaining_by_item={"123": 4},
        fail_return_route_once=True,
    )
    loop, store = _start_session(tmp_path, policy, world)

    paused = loop.run()

    progress_before = store.item_progress(loop.session_id, "123")
    anchor = progress_before.recycle_anchor_decision_id
    assert paused["driverState"] == "paused", paused
    assert anchor is not None
    assert progress_before.recycling
    assert progress_before.bag_quantity == 4
    assert progress_before.returning_quantity == 0
    assert progress_before.unresolved_quantity == 1
    assert world.pending_returns[world.cancellations[0]].quantity == 4

    io.mono = 60.0
    resumed_loop, resumed_store = _resume_paused_ah_loop(loop, store)

    def stop_after_partial_repost(event: dict[str, object]) -> None:
        if event.get("event") == "seller_decision" and event.get("kind") == "wait":
            if world.posts_by_item.get("123") == 2:
                resumed_store.revoke(
                    loop.session_id,
                    "test stop after partial returned bag repost",
                    now=int(io.wall_time()),
                )

    resumed_loop.progress = stop_after_partial_repost
    resumed = resumed_loop.run()

    progress_after = store.item_progress(loop.session_id, "123")
    assert resumed["session"]["state"] == "revoked", resumed  # type: ignore[index]
    assert world.posts_by_item["123"] == 2
    assert world.post_prices_by_item["123"] == [150, 125]
    post_actions = [action for action in world.actions if isinstance(action, PostCommodity)]
    assert [action.quantity for action in post_actions] == [5, 4]
    collect_actions = [action for action in world.actions if isinstance(action, CollectReturn)]
    assert len(collect_actions) == 1 and collect_actions[0].quantity == 4
    assert progress_after.admitted_quantity == progress_before.admitted_quantity == 5
    assert progress_after.recycle_rounds == progress_before.recycle_rounds == 1
    assert progress_after.spent_copper > progress_before.spent_copper
    assert progress_after.bag_quantity == 0
    assert progress_after.managed_active_quantity == 4
    assert progress_after.returning_quantity == 0
    assert progress_after.unresolved_quantity == 1
    assert progress_after.managed_active_quantity + progress_after.unresolved_quantity == 5
    assert not progress_after.recycling and progress_after.recycle_anchor_decision_id is None
    assert store.child_status(loop.session_id, anchor, "travel_to_ah") == "failed"
