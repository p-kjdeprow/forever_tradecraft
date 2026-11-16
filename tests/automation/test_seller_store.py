from __future__ import annotations

import json
from pathlib import Path

import pytest
from crowley_automation.ledger import Ledger
from crowley_automation.model import Batch, CancelAuction, CollectReturn, Interact, MoveRoute, PostCommodity
from crowley_automation.runner import (
    Admission,
    AdmissionDenied,
    AutomationRunner,
    DispatchReceipt,
    RunnerConfig,
)
from crowley_automation.seller import (
    SELLER_POLICY_EXAMPLE,
    InventoryFacts,
    MarketFacts,
    OwnedAuction,
    OwnedFacts,
    ReturnCandidate,
    ReturnObservationFacts,
    SellerDecision,
    SellerPolicy,
    StockFacts,
)
from crowley_automation.seller_store import SellerStore, SellerStoreError

NOW = 1_000
BOOT = "boot-a"
BUILD = "70938"
HOUSE = "us-normal-horde"
CHARACTER = "Tester"
ITEM = "123"


def policy(
    *,
    quantity: int = 5,
    batch_size: int = 5,
    max_total_spend: int = 500,
    max_deposit: int = 50,
    expires_at: int = 1_800_000_000,
) -> SellerPolicy:
    source = dict(SELLER_POLICY_EXAMPLE)
    item = dict(source["items"][0])  # type: ignore[index]
    item["quantity"] = quantity
    item["batchSize"] = min(batch_size, quantity)
    item["maxDepositCopper"] = max_deposit
    item["maxCancelCostCopper"] = 10
    source["items"] = [item]
    source["maxTotalSpendCopper"] = max_total_spend
    source["expiresAt"] = expires_at
    return SellerPolicy.from_json(json.dumps(source))


def inventory(
    bag: int,
    *,
    sequence: int = 1,
    request_id: str | None = None,
    observed_at: int = NOW,
    complete: bool = True,
    boot_id: str = BOOT,
) -> InventoryFacts:
    return InventoryFacts(
        ITEM,
        request_id or f"inventory:{boot_id}:{sequence}-{sequence}",
        boot_id,
        sequence,
        observed_at,
        complete,
        bag,
    )


def owned(
    auctions: tuple[OwnedAuction, ...] = (),
    *,
    sequence: int = 2,
    request_id: str | None = None,
    observed_at: int = NOW,
    complete: bool = True,
    boot_id: str = BOOT,
) -> OwnedFacts:
    return OwnedFacts(
        ITEM,
        request_id or f"owned:{boot_id}:{sequence}-{sequence}",
        boot_id,
        sequence,
        observed_at,
        complete,
        auctions,
    )


def market(
    *,
    request_id: str = "a" * 32,
    sequence: int = 3,
    observed_at: int = NOW,
    priority: str = "empty",
    competing: int | None = None,
    managed: tuple[OwnedAuction, ...] = (),
) -> MarketFacts:
    return MarketFacts(
        ITEM,
        request_id,
        BOOT,
        sequence,
        observed_at,
        True,
        priority,  # type: ignore[arg-type]
        competing,
        150 if managed else None,
        sum(row.remaining_quantity for row in managed),
        tuple(row.auction_id for row in managed),
        managed,
    )


def make_session(
    path: Path,
    *,
    selected: SellerPolicy | None = None,
) -> tuple[Ledger, SellerStore, str, SellerPolicy, StockFacts]:
    ledger = Ledger(path)
    store = SellerStore(ledger)
    selected = selected or policy()
    initial = inventory(selected.items[0].quantity)
    session_id = store.start(
        selected,
        boot_id=BOOT,
        client_build=BUILD,
        confirmed_policy_id=selected.policy_id,
        initial_inventory=(initial,),
        now=NOW,
    )
    stock = store.reconcile_stock(session_id, initial, owned(), now=NOW)
    return ledger, store, session_id, selected, stock


def first_post(
    store: SellerStore,
    session_id: str,
    selected: SellerPolicy,
    stock: StockFacts,
    *,
    now: int = NOW,
) -> tuple[MarketFacts, object, Batch]:
    facts = market(observed_at=now, sequence=3, request_id="a" * 32)
    decision = store.create_decision(session_id, facts, stock, now=now)
    batch = Batch(selected.house, selected.character, now + 30, decision.actions)
    store.prepare_child(session_id, decision.decision_id, batch, now=now)
    return facts, decision, batch


class FakeBackend:
    def __init__(
        self,
        result: dict[str, object] | None = None,
        *,
        observed_at: int = NOW + 1,
        boot_id: str = BOOT,
    ) -> None:
        self.result = result
        self.observed_at = observed_at
        self.boot_id = boot_id
        self.staged: tuple[str, int, int, str] | None = None
        self.dispatched: list[object] = []

    def stage(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        self.staged = (batch_id, ordinal, step_index, action_digest)

    def observe(self) -> Admission:
        values: dict[str, object] = {
            "session_boot_id": self.boot_id,
            "house": HOUSE,
            "character": CHARACTER,
            "observed_at": self.observed_at,
            "human_present": False,
            "focused": True,
            "gui_lock_owned": True,
            "client_ah_owned": True,
            "receipt_sequence": 1,
            "client_build": BUILD,
        }
        if self.staged is not None:
            batch_id, ordinal, step_index, digest = self.staged
            values.update(
                staged_batch_id=batch_id,
                staged_ordinal=ordinal,
                staged_step_index=step_index,
                staged_action_digest=digest,
                ready=True,
            )
        return Admission(**values)  # type: ignore[arg-type]

    def dispatch(
        self,
        action: object,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
        admission: Admission,
    ) -> DispatchReceipt:
        self.dispatched.append(action)
        return DispatchReceipt(
            batch_id,
            ordinal,
            step_index,
            action_digest,
            admission.session_boot_id,
            True,
            True,
            True,
            result=self.result,
        )


def execute(
    ledger: Ledger,
    store: SellerStore,
    session_id: str,
    decision_id: str,
    batch: Batch,
    backend: FakeBackend,
    *,
    now: int,
    route_config: bool = False,
) -> dict[str, object]:
    config = RunnerConfig(
        frozenset({"auctioneer_main", "mailbox_main"}),
        tuple(sorted({("ah_to_mail", 1), ("mail_to_ah", 1)})) if route_config else (),
    )
    runner = AutomationRunner(ledger, config=config, clock=lambda: now)
    return runner.run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision_id,
        phase="decision"
        if batch.actions[0].__class__ not in (MoveRoute, Interact, CollectReturn)
        else "decision",
        backend=backend,
    )


def canceled_recycle(
    path: Path,
    *,
    remaining_quantity: int = 4,
    selected: SellerPolicy | None = None,
) -> tuple[Ledger, SellerStore, str, SellerPolicy, SellerDecision]:
    ledger, store, session_id, selected, stock = make_session(path, selected=selected)
    _, post_decision, post_batch = first_post(store, session_id, selected, stock)
    assert (
        execute(
            ledger,
            store,
            session_id,
            post_decision.decision_id,
            post_batch,
            FakeBackend(
                {
                    "kind": "post_commodity",
                    "auctionId": 77,
                    "itemKey": ITEM,
                    "quantity": 5,
                    "unitPriceCopper": 150,
                    "depositCopper": 2,
                },
                observed_at=NOW + 1,
            ),
            now=NOW + 1,
        )["state"]
        == "succeeded"
    )
    listing = OwnedAuction(77, ITEM, 5, 150, "active", True, True, 2)
    stock_after_post = store.reconcile_stock(
        session_id,
        inventory(0, sequence=4, observed_at=NOW + 2),
        owned((listing,), sequence=5, observed_at=NOW + 2),
        now=NOW + 2,
    )
    recycle = store.create_decision(
        session_id,
        market(
            request_id="b" * 32,
            sequence=6,
            observed_at=NOW + 2,
            priority="competitor",
            competing=125,
            managed=(listing,),
        ),
        stock_after_post,
        now=NOW + 2,
    )
    cancel_batch = Batch(HOUSE, CHARACTER, NOW + 30, recycle.actions)
    store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=NOW + 2)
    cancel_status = AutomationRunner(ledger, clock=lambda: NOW + 3).run_seller_child(
        cancel_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        backend=FakeBackend(
            {
                "kind": "cancel_auction",
                "auctionId": 77,
                "itemKey": ITEM,
                "remainingQuantity": remaining_quantity,
                "cancelCostCopper": 3,
            },
            observed_at=NOW + 3,
        ),
    )
    assert cancel_status["state"] == "succeeded"
    return ledger, store, session_id, selected, recycle


def collect_return_at_mailbox(
    ledger: Ledger,
    store: SellerStore,
    session_id: str,
    selected: SellerPolicy,
    recycle: SellerDecision,
    *,
    quantity: int,
    observed_at: int = NOW + 5,
) -> str:
    store.revoke(session_id, now=observed_at - 1)
    token = "c" * 64
    request_id = "returns:boot-b:1-1"
    candidate = ReturnCandidate(
        ITEM,
        quantity,
        token,
        request_id,
        "boot-b",
        1,
        observed_at,
        True,
        True,
        "exact commodity return with one-use native selection token",
    )
    store.resume_at_mailbox(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-b", sequence=1, observed_at=observed_at),),
        mail_observations=(
            ReturnObservationFacts(
                ITEM,
                request_id,
                "boot-b",
                1,
                observed_at,
                True,
                (candidate,),
            ),
        ),
        now=observed_at,
    )
    phase = f"collect_return:{token}"
    actions = store.actions_for_phase(session_id, recycle.decision_id, phase, now=observed_at)
    assert actions == (CollectReturn(ITEM, quantity, token),)
    batch = Batch(HOUSE, CHARACTER, observed_at + 20, actions)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        batch,
        phase=phase,
        now=observed_at,
    )
    status = AutomationRunner(ledger, clock=lambda: observed_at + 1).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase=phase,
        backend=FakeBackend(
            {"kind": "collect_return", "itemKey": ITEM, "quantity": quantity, "mailFingerprint": token},
            observed_at=observed_at + 1,
            boot_id="boot-b",
        ),
    )
    assert status["state"] == "succeeded"
    return token


def test_session_restart_preserves_stock_and_child_reservation(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    facts, decision, batch = first_post(store, session_id, selected, stock)
    assert decision.kind == "post"
    assert isinstance(batch.actions[0], PostCommodity)
    assert batch.actions[0].quantity == 5

    with pytest.raises(SellerStoreError, match="exact persisted policy-derived"):
        tampered = Batch(
            selected.house,
            selected.character,
            batch.expires_at,
            (PostCommodity(ITEM, 5, batch.actions[0].unit_price_copper + 1, 24, 50),),
        )
        store.prepare_child(session_id, decision.decision_id, tampered, now=NOW)

    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity) == (0, 5)
    assert progress.reserved_spend_copper == 50
    assert store.report(session_id, now=NOW)["items"]

    restarted_ledger = Ledger(ledger.path)
    restarted = SellerStore(restarted_ledger)
    assert restarted.status(session_id, now=NOW).policy_id == selected.policy_id
    assert restarted.item_progress(session_id, ITEM) == progress
    with pytest.raises(SellerStoreError, match="another durable seller session is active"):
        restarted.start(
            selected,
            boot_id=BOOT,
            client_build=BUILD,
            confirmed_policy_id=selected.policy_id,
            initial_inventory=(facts_inventory(),),
            now=NOW + 1,
        )


def facts_inventory() -> InventoryFacts:
    return inventory(5, sequence=9, request_id="inventory:boot-a:9-9", observed_at=NOW + 1)


def test_atomic_post_receipt_creates_exact_managed_auction_and_spend(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    backend = FakeBackend(
        {
            "kind": "post_commodity",
            "auctionId": 77,
            "itemKey": ITEM,
            "quantity": 5,
            "unitPriceCopper": 150,
            "depositCopper": 2,
        }
    )
    status = execute(ledger, store, session_id, decision.decision_id, batch, backend, now=NOW + 1)

    assert status["state"] == "succeeded"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.managed_active_quantity, progress.unresolved_quantity) == (
        0,
        5,
        0,
    )
    assert progress.spent_copper == 2
    assert progress.reserved_spend_copper == 0
    assert [row.auction_id for row in store.managed_auctions(session_id, ITEM)] == [77]

    restarted = SellerStore(Ledger(ledger.path))
    assert restarted.item_progress(session_id, ITEM) == progress
    assert restarted.managed_auctions(session_id, ITEM)[0].created_batch_id == batch.batch_id


@pytest.mark.parametrize("deposit, should_apply", [(2, True), (51, False)])
def test_seller_reconciliation_receipt_applies_effect_atomically(
    tmp_path: Path, deposit: int, should_apply: bool
) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW + 1, session_boot_id=BOOT, receipt_baseline=3)
    assert ledger.recover_interrupted(now=NOW + 2) == 1
    assert ledger.status(batch.batch_id)["state"] == "uncertain"

    native_receipt = {
        "sequence": 4,
        "batchId": batch.batch_id,
        "ordinal": 0,
        "stepIndex": 0,
        "callbackDelivered": True,
        "postcondition": True,
        "actionComplete": True,
        "detail": "",
        "result": {
            "kind": "post_commodity",
            "auctionId": 77,
            "itemKey": ITEM,
            "quantity": 5,
            "unitPriceCopper": 150,
            "depositCopper": deposit,
        },
    }

    if should_apply:
        ledger.reconcile_receipt(
            batch.batch_id,
            0,
            0,
            native_receipt,
            boot_id=BOOT,
            step_limit=1,
            now=NOW + 3,
        )
        assert ledger.status(batch.batch_id)["state"] == "succeeded"
        progress = store.item_progress(session_id, ITEM)
        assert (progress.unresolved_quantity, progress.managed_active_quantity, progress.spent_copper) == (
            0,
            5,
            2,
        )
        assert [row.auction_id for row in store.managed_auctions(session_id, ITEM)] == [77]
    else:
        with pytest.raises(SellerStoreError, match="deposit"):
            ledger.reconcile_receipt(
                batch.batch_id,
                0,
                0,
                native_receipt,
                boot_id=BOOT,
                step_limit=1,
                now=NOW + 3,
            )
        assert ledger.status(batch.batch_id)["state"] == "uncertain"
        progress = store.item_progress(session_id, ITEM)
        assert (progress.unresolved_quantity, progress.managed_active_quantity, progress.spent_copper) == (
            5,
            0,
            0,
        )
        assert store.managed_auctions(session_id, ITEM) == ()


def test_budget_overrun_is_rejected_before_dispatch(tmp_path: Path) -> None:
    selected = policy(max_total_spend=20, max_deposit=50)
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3", selected=selected)
    facts = market()
    decision = store.create_decision(session_id, facts, stock, now=NOW)
    batch = Batch(selected.house, selected.character, NOW + 30, decision.actions)
    with pytest.raises(SellerStoreError, match="spending limit"):
        store.prepare_child(session_id, decision.decision_id, batch, reservation_copper=50, now=NOW)
    assert ledger.status(batch.batch_id)["state"] == "prepared"
    assert store.item_progress(session_id, ITEM).bag_quantity == 5
    assert store.item_progress(session_id, ITEM).reserved_spend_copper == 0


def test_expired_inflight_seller_child_is_recovered_without_releasing_stock_or_budget(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW + 1, session_boot_id=BOOT, receipt_baseline=3)

    status = AutomationRunner(ledger, clock=lambda: NOW + 31).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=FakeBackend(),
    )

    assert status["state"] == "uncertain"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        0,
        5,
        50,
    )
    assert ledger.has_uncertain_effect()


def test_expired_never_dispatched_seller_child_releases_its_reservation(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)

    status = AutomationRunner(ledger, clock=lambda: NOW + 31).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=FakeBackend(),
    )

    assert status["state"] == "expired"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_restart_releases_persisted_expired_child_after_lock_and_recovery(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    ledger.mark_expired(batch.batch_id, now=NOW + 31)
    backend = FakeBackend()

    status = AutomationRunner(ledger, clock=lambda: NOW + 31).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=backend,
    )

    assert status["state"] == "expired"
    assert backend.staged is None
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_releasing_expired_post_twice_cannot_refund_unrelated_unresolved_stock(tmp_path: Path) -> None:
    _, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    store.release_before_dispatch(session_id, decision.decision_id, "decision", batch, now=NOW + 31)
    assert store.item_progress(session_id, ITEM).bag_quantity == 5

    store.reconcile_stock(
        session_id,
        inventory(0, sequence=3, observed_at=NOW + 40),
        owned(sequence=4, observed_at=NOW + 40),
        now=NOW + 40,
    )
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity) == (0, 5)

    store.release_before_dispatch(session_id, decision.decision_id, "decision", batch, now=NOW + 41)
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        0,
        5,
        0,
    )


def test_resume_preserves_durable_stock_and_expired_grant_has_explicit_release(tmp_path: Path) -> None:
    ledger, store, session_id, selected, _ = make_session(tmp_path / "ledger.sqlite3")
    store.revoke(session_id, now=NOW + 1)
    resumed = store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(10, boot_id="boot-b", sequence=1, observed_at=NOW + 2),),
        owned_facts=(owned(boot_id="boot-b", sequence=2, observed_at=NOW + 2),),
        now=NOW + 2,
    )
    assert resumed == session_id
    assert store.status(session_id, now=NOW + 2).state == "active"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.admitted_quantity, progress.bag_quantity, progress.spent_copper) == (5, 5, 0)

    expired_policy = policy(expires_at=NOW + 10)
    _, expired_store, expired_session, _, _ = make_session(
        tmp_path / "expired.sqlite3", selected=expired_policy
    )
    expired_store.revoke(expired_session, now=NOW + 1)
    with pytest.raises(SellerStoreError, match="cannot be renewed"):
        expired_store.resume(
            expired_session,
            confirmed_policy_id=expired_policy.policy_id,
            boot_id=BOOT,
            client_build=BUILD,
            inventories=(inventory(5, sequence=2, observed_at=NOW + 11),),
            owned_facts=(owned(sequence=3, observed_at=NOW + 11),),
            now=NOW + 11,
        )
    released = expired_store.release_expired_stock(
        expired_session,
        confirmed_policy_id=expired_policy.policy_id,
        boot_id=BOOT,
        client_build=BUILD,
        inventories=(inventory(5, sequence=2, observed_at=NOW + 11),),
        owned_facts=(owned(sequence=3, observed_at=NOW + 11),),
        now=NOW + 11,
    )
    assert released == expired_session
    assert expired_store.status(expired_session, now=NOW + 11).state == "completed"
    assert expired_store.item_progress(expired_session, ITEM).bag_quantity == 5


def test_expired_release_accepts_fully_sold_item_beside_bag_only_stock(tmp_path: Path) -> None:
    source = dict(SELLER_POLICY_EXAMPLE)
    first = dict(source["items"][0])  # type: ignore[index]
    first.update(itemKey="123", quantity=1, batchSize=1)
    second = dict(first)
    second["itemKey"] = "456"
    source["items"] = [first, second]
    source["expiresAt"] = NOW + 10
    selected = SellerPolicy.from_json(json.dumps(source))
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = SellerStore(ledger)
    initial = tuple(
        InventoryFacts(key, f"inventory:{key}:1-1", BOOT, 1, NOW, True, 1) for key in ("123", "456")
    )
    session_id = store.start(
        selected,
        boot_id=BOOT,
        client_build=BUILD,
        confirmed_policy_id=selected.policy_id,
        initial_inventory=initial,
        now=NOW,
    )
    stocks = {
        key: store.reconcile_stock(
            session_id,
            initial[index],
            OwnedFacts(key, f"owned:{key}:2-2", BOOT, 2, NOW, True, ()),
            now=NOW,
        )
        for index, key in enumerate(("123", "456"))
    }
    market_a = MarketFacts("123", "a" * 32, BOOT, 3, NOW, True, "empty", None, None, 0, (), ())
    post_decision = store.create_decision(session_id, market_a, stocks["123"], now=NOW)
    post_action = post_decision.actions[0]
    assert isinstance(post_action, PostCommodity)
    batch = Batch(HOUSE, CHARACTER, selected.expires_at - 1, post_decision.actions)
    store.prepare_child(session_id, post_decision.decision_id, batch, now=NOW)
    status = AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=post_decision.decision_id,
        backend=FakeBackend(
            {
                "kind": "post_commodity",
                "auctionId": 501,
                "itemKey": "123",
                "quantity": 1,
                "unitPriceCopper": post_action.unit_price_copper,
                "depositCopper": 1,
            },
            observed_at=NOW + 1,
        ),
    )
    assert status["state"] == "succeeded"
    sold = OwnedAuction(501, "123", 0, post_action.unit_price_copper, "sold", False, False, 0)
    store.reconcile_stock(
        session_id,
        InventoryFacts("123", "inventory:123:4-4", BOOT, 4, NOW + 2, True, 0),
        OwnedFacts("123", "owned:123:5-5", BOOT, 5, NOW + 2, True, (sold,)),
        now=NOW + 2,
    )

    released = store.release_expired_stock(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id=BOOT,
        client_build=BUILD,
        inventories=(
            InventoryFacts("123", "inventory:123:6-6", BOOT, 6, NOW + 11, True, 0),
            InventoryFacts("456", "inventory:456:3-3", BOOT, 3, NOW + 11, True, 1),
        ),
        owned_facts=(
            OwnedFacts("123", "owned:123:7-7", BOOT, 7, NOW + 11, True, (sold,)),
            OwnedFacts("456", "owned:456:4-4", BOOT, 4, NOW + 11, True, ()),
        ),
        now=NOW + 11,
    )
    assert released == session_id
    assert store.status(session_id, now=NOW + 11).state == "completed"
    assert store.item_progress(session_id, "123").bag_quantity == 0
    assert store.item_progress(session_id, "456").bag_quantity == 1


def test_persisted_post_interval_survives_new_market_request_and_store_instance(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger, store, session_id, selected, stock = make_session(path, selected=policy(quantity=10))
    _, decision, batch = first_post(store, session_id, selected, stock)
    post_backend = FakeBackend(
        {
            "kind": "post_commodity",
            "auctionId": 88,
            "itemKey": ITEM,
            "quantity": 5,
            "unitPriceCopper": 150,
            "depositCopper": 2,
        },
        observed_at=NOW + 1,
    )
    assert (
        AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
            batch,
            seller_store=store,
            session_id=session_id,
            decision_id=decision.decision_id,
            backend=post_backend,
        )["state"]
        == "succeeded"
    )

    listing = OwnedAuction(88, ITEM, 5, 150, "active", False, False, 0)
    stock_after = store.reconcile_stock(
        session_id,
        inventory(5, sequence=4, observed_at=NOW + 2),
        owned((listing,), sequence=5, observed_at=NOW + 2),
        now=NOW + 2,
    )
    restarted_store = SellerStore(Ledger(path))
    wait = restarted_store.create_decision(
        session_id,
        market(
            request_id="b" * 32,
            sequence=6,
            observed_at=NOW + 3,
            priority="unknown",
            competing=150,
            managed=(listing,),
        ),
        stock_after,
        now=NOW + 3,
    )
    assert wait.kind == "wait"
    assert "interval" in wait.reason

    ready = restarted_store.create_decision(
        session_id,
        market(
            request_id="c" * 32,
            sequence=7,
            observed_at=NOW + 61,
            priority="unknown",
            competing=150,
            managed=(listing,),
        ),
        stock_after,
        now=NOW + 61,
    )
    assert ready.kind == "post"
    assert ready.unit_price_copper == 150


@pytest.mark.parametrize("outcome", ["succeeded", "known_failure", "unknown_failure"])
def test_post_cooldown_starts_at_actual_late_attempt(tmp_path: Path, outcome: str) -> None:
    ledger, store, session_id, selected, stock = make_session(
        tmp_path / "ledger.sqlite3", selected=policy(quantity=10)
    )
    decision = store.create_decision(
        session_id,
        market(request_id="d" * 32, observed_at=NOW),
        stock,
        now=NOW,
    )
    batch = Batch(HOUSE, CHARACTER, NOW + 60, decision.actions)
    store.prepare_child(session_id, decision.decision_id, batch, now=NOW)

    class DelayedPostBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            assert isinstance(action, PostCommodity)
            if outcome == "unknown_failure":
                raise RuntimeError("dispatch result was lost")
            if outcome == "known_failure":
                return DispatchReceipt(
                    batch_id,
                    ordinal,
                    step_index,
                    action_digest,
                    admission.session_boot_id,
                    True,
                    False,
                    False,
                )
            return DispatchReceipt(
                batch_id,
                ordinal,
                step_index,
                action_digest,
                admission.session_boot_id,
                True,
                True,
                True,
                result={
                    "kind": "post_commodity",
                    "auctionId": 91,
                    "itemKey": ITEM,
                    "quantity": action.quantity,
                    "unitPriceCopper": action.unit_price_copper,
                    "depositCopper": 2,
                },
            )

    first_status = AutomationRunner(ledger, clock=lambda: NOW + 59).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=DelayedPostBackend(observed_at=NOW + 59),
    )
    expected_state = {
        "succeeded": "succeeded",
        "known_failure": "failed",
        "unknown_failure": "uncertain",
    }[outcome]
    assert first_status["state"] == expected_state
    assert store.item_progress(session_id, ITEM).last_post_attempt_at == NOW + 59

    if outcome == "unknown_failure":
        ledger.reconcile_receipt(
            batch.batch_id,
            0,
            0,
            {
                "sequence": 2,
                "batchId": batch.batch_id,
                "ordinal": 0,
                "stepIndex": 0,
                "callbackDelivered": True,
                "postcondition": False,
                "actionComplete": False,
                "detail": "native confirmed post refusal",
            },
            boot_id=BOOT,
            step_limit=1,
            now=NOW + 61,
        )
        assert store.item_progress(session_id, ITEM).last_post_attempt_at == NOW + 61

    post_succeeded = outcome == "succeeded"
    listing = OwnedAuction(91, ITEM, 5, 150, "active", False, False, 0) if post_succeeded else None
    stock_after = store.reconcile_stock(
        session_id,
        inventory(5 if post_succeeded else 10, sequence=4, observed_at=NOW + 62),
        owned(() if listing is None else (listing,), sequence=5, observed_at=NOW + 62),
        now=NOW + 62,
    )
    next_decision = store.create_decision(
        session_id,
        market(
            request_id="e" * 32,
            sequence=6,
            observed_at=NOW + 63,
            priority="unknown",
            competing=150,
            managed=() if listing is None else (listing,),
        ),
        stock_after,
        now=NOW + 63,
    )
    assert next_decision.kind == "wait"
    assert "interval" in next_decision.reason


def test_recycle_round_cap_is_session_wide_across_items(tmp_path: Path) -> None:
    source = dict(SELLER_POLICY_EXAMPLE)
    first_item = dict(source["items"][0])  # type: ignore[index]
    first_item.update(itemKey="123", quantity=1, batchSize=1)
    second_item = dict(first_item)
    second_item["itemKey"] = "456"
    source["items"] = [first_item, second_item]
    source["maxRecycleRounds"] = 1
    selected = SellerPolicy.from_json(json.dumps(source))
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = SellerStore(ledger)
    initial = tuple(
        InventoryFacts(key, f"inventory:{key}:1-1", BOOT, 1, NOW, True, 1) for key in ("123", "456")
    )
    session_id = store.start(
        selected,
        boot_id=BOOT,
        client_build=BUILD,
        confirmed_policy_id=selected.policy_id,
        initial_inventory=initial,
        now=NOW,
    )

    for index, key in enumerate(("123", "456")):
        stock = store.reconcile_stock(
            session_id,
            initial[index],
            OwnedFacts(key, f"owned:{key}:2-2", BOOT, 2, NOW, True, ()),
            now=NOW,
        )
        market_facts = MarketFacts(key, "a" * 32, BOOT, 3, NOW, True, "empty", None, None, 0, (), ())
        post_decision = store.create_decision(session_id, market_facts, stock, now=NOW)
        post_batch = Batch(HOUSE, CHARACTER, NOW + 30, post_decision.actions)
        store.prepare_child(session_id, post_decision.decision_id, post_batch, now=NOW)
        post_backend = FakeBackend(
            {
                "kind": "post_commodity",
                "auctionId": 90 + index,
                "itemKey": key,
                "quantity": 1,
                "unitPriceCopper": 150,
                "depositCopper": 1,
            },
            observed_at=NOW + 1,
        )
        assert (
            AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
                post_batch,
                seller_store=store,
                session_id=session_id,
                decision_id=post_decision.decision_id,
                backend=post_backend,
            )["state"]
            == "succeeded"
        )

        listing = OwnedAuction(90 + index, key, 1, 150, "active", True, True, 1)
        stock = store.reconcile_stock(
            session_id,
            InventoryFacts(key, f"inventory:{key}:4-4", BOOT, 4, NOW + 2, True, 0),
            OwnedFacts(key, f"owned:{key}:5-5", BOOT, 5, NOW + 2, True, (listing,)),
            now=NOW + 2,
        )
        displaced = MarketFacts(
            key,
            ("b" if index == 0 else "c") * 32,
            BOOT,
            6,
            NOW + 2,
            True,
            "competitor",
            125,
            150,
            1,
            (listing.auction_id,),
            (listing,),
        )
        recycle = store.create_decision(session_id, displaced, stock, now=NOW + 2)
        cancel_batch = Batch(HOUSE, CHARACTER, NOW + 30, recycle.actions)
        if index == 0:
            store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=NOW + 2)
            assert store.item_progress(session_id, key).recycle_rounds == 1
        else:
            with pytest.raises(SellerStoreError, match="session recycle-round cap"):
                store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=NOW + 2)


def test_known_cancel_failure_releases_future_child_budget(tmp_path: Path) -> None:
    selected = policy(quantity=3, batch_size=1)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = SellerStore(ledger)
    initial = inventory(3)
    session_id = store.start(
        selected,
        boot_id=BOOT,
        client_build=BUILD,
        confirmed_policy_id=selected.policy_id,
        initial_inventory=(initial,),
        now=NOW,
    )
    stock = store.reconcile_stock(session_id, initial, owned(), now=NOW)
    listings: list[OwnedAuction] = []
    for index in range(3):
        now = NOW + index * 61
        facts = MarketFacts(
            ITEM,
            f"{index + 30:032x}",
            BOOT,
            30 + index,
            now,
            True,
            "empty" if not listings else "competitor",
            None if not listings else 125,
            min((row.unit_price_copper for row in listings), default=None),
            sum(row.remaining_quantity for row in listings),
            tuple(row.auction_id for row in listings),
            tuple(listings),
        )
        decision = store.create_decision(session_id, facts, stock, now=now)
        post = decision.actions[0]
        assert isinstance(post, PostCommodity)
        batch = Batch(HOUSE, CHARACTER, now + 30, decision.actions)
        store.prepare_child(session_id, decision.decision_id, batch, now=now)
        status = AutomationRunner(ledger, clock=lambda now=now: now + 1).run_seller_child(
            batch,
            seller_store=store,
            session_id=session_id,
            decision_id=decision.decision_id,
            backend=FakeBackend(
                {
                    "kind": "post_commodity",
                    "auctionId": 301 + index,
                    "itemKey": ITEM,
                    "quantity": 1,
                    "unitPriceCopper": post.unit_price_copper,
                    "depositCopper": 1,
                },
                observed_at=now + 1,
            ),
        )
        assert status["state"] == "succeeded"
        listings.append(OwnedAuction(301 + index, ITEM, 1, post.unit_price_copper, "active", True, True, 1))
        stock = store.reconcile_stock(
            session_id,
            inventory(2 - index, sequence=3 + index, observed_at=now + 2),
            owned(tuple(listings), sequence=4 + index, observed_at=now + 2),
            now=now + 2,
        )

    recycle_time = NOW + 2 * 61 + 3
    recycle_market = MarketFacts(
        ITEM,
        "f" * 32,
        BOOT,
        50,
        recycle_time,
        True,
        "competitor",
        125,
        125,
        3,
        tuple(row.auction_id for row in listings),
        tuple(listings),
    )
    recycle = store.create_decision(session_id, recycle_market, stock, now=recycle_time)
    assert recycle.kind == "recycle" and len(recycle.actions) == 3
    cancel_batch = Batch(HOUSE, CHARACTER, recycle_time + 30, recycle.actions)
    store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=recycle_time)

    class PartialCancelBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            assert isinstance(action, CancelAuction)
            self.dispatched.append(action)
            if ordinal == 0:
                return DispatchReceipt(
                    batch_id,
                    ordinal,
                    step_index,
                    action_digest,
                    admission.session_boot_id,
                    True,
                    True,
                    True,
                    result={
                        "kind": "cancel_auction",
                        "auctionId": action.auction_id,
                        "itemKey": ITEM,
                        "remainingQuantity": 1,
                        "cancelCostCopper": 1,
                    },
                )
            return DispatchReceipt(
                batch_id,
                ordinal,
                step_index,
                action_digest,
                admission.session_boot_id,
                True,
                False,
                False,
                detail="cancel callback had a verified no-effect result",
            )

    backend = PartialCancelBackend(observed_at=recycle_time + 1)
    result = AutomationRunner(ledger, clock=lambda: recycle_time + 1).run_seller_child(
        cancel_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        backend=backend,
    )
    assert result["state"] == "failed"
    assert len(backend.dispatched) == 2
    progress = store.item_progress(session_id, ITEM)
    assert (
        progress.managed_active_quantity,
        progress.returning_quantity,
        progress.reserved_spend_copper,
        progress.spent_copper,
    ) == (2, 1, 0, 4)


@pytest.mark.parametrize("resume_delay", [4, 51])
def test_store_continues_frozen_recycle_targets_after_fresh_rearm(tmp_path: Path, resume_delay: int) -> None:
    selected = policy(quantity=9, batch_size=1)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = SellerStore(ledger)
    initial = inventory(9)
    session_id = store.start(
        selected,
        boot_id=BOOT,
        client_build=BUILD,
        confirmed_policy_id=selected.policy_id,
        initial_inventory=(initial,),
        now=NOW,
    )
    stock = store.reconcile_stock(session_id, initial, owned(), now=NOW)
    listings: list[OwnedAuction] = []

    for index in range(9):
        now = NOW + index * 61
        facts = MarketFacts(
            ITEM,
            f"{index + 1:032x}",
            BOOT,
            10 + index,
            now,
            True,
            "empty" if not listings else "competitor",
            None if not listings else 125,
            None if not listings else min(row.unit_price_copper for row in listings),
            sum(row.remaining_quantity for row in listings),
            tuple(row.auction_id for row in listings),
            tuple(listings),
        )
        decision = store.create_decision(session_id, facts, stock, now=now)
        post = decision.actions[0]
        assert isinstance(post, PostCommodity)
        batch = Batch(HOUSE, CHARACTER, now + 30, decision.actions)
        store.prepare_child(session_id, decision.decision_id, batch, now=now)
        post_result = FakeBackend(
            {
                "kind": "post_commodity",
                "auctionId": 100 + index,
                "itemKey": ITEM,
                "quantity": 1,
                "unitPriceCopper": post.unit_price_copper,
                "depositCopper": 1,
            },
            observed_at=now + 1,
        )
        post_status = AutomationRunner(ledger, clock=lambda now=now: now + 1).run_seller_child(
            batch,
            seller_store=store,
            session_id=session_id,
            decision_id=decision.decision_id,
            backend=post_result,
        )
        assert post_status["state"] == "succeeded", (index, decision.object(), post_status)
        listings.append(OwnedAuction(100 + index, ITEM, 1, post.unit_price_copper, "active", True, True, 1))
        stock = store.reconcile_stock(
            session_id,
            inventory(8 - index, sequence=3 + index, observed_at=now + 2),
            owned(tuple(listings), sequence=4 + index, observed_at=now + 2),
            now=now + 2,
        )

    recycle_time = NOW + 8 * 61 + 3
    recycle = store.create_decision(
        session_id,
        MarketFacts(
            ITEM,
            "f" * 32,
            BOOT,
            40,
            recycle_time,
            True,
            "competitor",
            125,
            125,
            9,
            tuple(row.auction_id for row in listings),
            tuple(listings),
        ),
        stock,
        now=recycle_time,
    )
    assert recycle.kind == "recycle"
    first_chunk = Batch(HOUSE, CHARACTER, recycle_time + 30, recycle.actions)
    store.prepare_child(session_id, recycle.decision_id, first_chunk, now=recycle_time)

    class CancelBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            assert isinstance(action, CancelAuction)
            self.dispatched.append(action)
            return DispatchReceipt(
                batch_id,
                ordinal,
                step_index,
                action_digest,
                admission.session_boot_id,
                True,
                True,
                True,
                result={
                    "kind": "cancel_auction",
                    "auctionId": action.auction_id,
                    "itemKey": ITEM,
                    "remainingQuantity": 1,
                    "cancelCostCopper": 1,
                },
            )

    cancel_status = AutomationRunner(ledger, clock=lambda: recycle_time + 1).run_seller_child(
        first_chunk,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        backend=CancelBackend(observed_at=recycle_time + 1),
    )
    assert cancel_status["state"] == "succeeded"
    assert len(cancel_status["actions"]) == 8
    assert store.item_progress(session_id, ITEM).recycling

    phase, continuation_actions = store.continue_recycle(
        session_id,
        recycle.decision_id,
        inventory(0, sequence=20, observed_at=recycle_time + 2),
        owned((listings[-1],), sequence=21, observed_at=recycle_time + 2),
        now=recycle_time + 2,
    )
    assert phase == "continue_recycle:1"
    assert continuation_actions == (CancelAuction(108, selected.items[0].max_cancel_cost_copper),)
    continuation = Batch(HOUSE, CHARACTER, recycle_time + 50, continuation_actions)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        continuation,
        phase=phase,
        now=recycle_time + 2,
    )
    resume_time = recycle_time + resume_delay
    store.revoke(session_id, now=recycle_time + 3)
    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-b", sequence=1, observed_at=resume_time),),
        owned_facts=(owned((listings[-1],), boot_id="boot-b", sequence=2, observed_at=resume_time),),
        now=resume_time,
    )
    resumed_phase, resumed_actions = store.continue_recycle(
        session_id,
        recycle.decision_id,
        inventory(0, boot_id="boot-b", sequence=1, observed_at=resume_time),
        owned((listings[-1],), boot_id="boot-b", sequence=2, observed_at=resume_time),
        now=resume_time,
    )
    expected_phase = "continue_recycle:1" if resume_delay == 4 else "continue_recycle:2"
    assert resumed_phase == expected_phase
    assert resumed_actions == continuation_actions
    if resumed_phase == phase:
        resumed_batch = store.child_batch(session_id, recycle.decision_id, resumed_phase)
        assert resumed_batch == continuation
    else:
        resumed_batch = Batch(HOUSE, CHARACTER, resume_time + 30, resumed_actions)
        store.prepare_child(
            session_id,
            recycle.decision_id,
            resumed_batch,
            phase=resumed_phase,
            now=resume_time,
        )
    assert resumed_batch is not None
    continued = AutomationRunner(ledger, clock=lambda: resume_time + 1).run_seller_child(
        resumed_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase=resumed_phase,
        backend=CancelBackend(observed_at=resume_time + 1, boot_id="boot-b"),
    )
    assert continued["state"] == "succeeded"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.managed_active_quantity, progress.returning_quantity, progress.recycle_rounds) == (
        0,
        9,
        1,
    )


@pytest.mark.parametrize("end_session", ["revoked", "expired"])
def test_late_structured_post_receipt_reconciles_after_revoke_or_expiry(
    tmp_path: Path, end_session: str
) -> None:
    selected = policy(expires_at=NOW + 2 if end_session == "expired" else 1_800_000_000)
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3", selected=selected)
    facts = market(observed_at=NOW, request_id="e" * 32)
    decision = store.create_decision(session_id, facts, stock, now=NOW)
    batch = Batch(HOUSE, CHARACTER, min(NOW + 30, selected.expires_at), decision.actions)
    store.prepare_child(session_id, decision.decision_id, batch, now=NOW)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW + 1, session_boot_id=BOOT, receipt_baseline=3)
    assert ledger.recover_interrupted(now=NOW + 2) == 1
    if end_session == "revoked":
        store.revoke(session_id, now=NOW + 2)
    else:
        assert store.status(session_id, now=NOW + 4).state == "expired"

    ledger.reconcile_receipt(
        batch.batch_id,
        0,
        0,
        {
            "sequence": 4,
            "batchId": batch.batch_id,
            "ordinal": 0,
            "stepIndex": 0,
            "callbackDelivered": True,
            "postcondition": True,
            "actionComplete": True,
            "detail": "late exact native post receipt",
            "result": {
                "kind": "post_commodity",
                "auctionId": 77,
                "itemKey": ITEM,
                "quantity": 5,
                "unitPriceCopper": 150,
                "depositCopper": 2,
            },
        },
        boot_id=BOOT,
        step_limit=1,
        now=NOW + 4,
    )

    assert ledger.status(batch.batch_id)["state"] == "succeeded"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.unresolved_quantity, progress.managed_active_quantity, progress.spent_copper) == (
        0,
        5,
        2,
    )


def test_verified_native_precondition_refusal_restores_post_stock_and_budget(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)

    class RefusalBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            self.dispatched.append(action)
            return DispatchReceipt(
                batch_id,
                ordinal,
                step_index,
                action_digest,
                admission.session_boot_id,
                True,
                False,
                False,
                detail="native precondition refused before protected callback",
            )

    status = AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=RefusalBackend(),
    )

    assert status["state"] == "failed"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_native_return_slot_stage_refusal_survives_in_action_reason(tmp_path: Path) -> None:
    from crowley_automation.backend import DesktopBackendError

    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)

    class RefusingBackend(FakeBackend):
        def stage(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
        ) -> None:
            refusal = DesktopBackendError("native preflight refused operation: insufficient_return_bag_slots")
            refusal.code = "insufficient_return_bag_slots"
            raise refusal

    status = AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=RefusingBackend(observed_at=NOW + 1),
    )
    assert status["state"] == "failed"
    assert status["actions"][0]["reason"] == (
        "native preflight refused operation: insufficient_return_bag_slots"
    )
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_arbitrary_stage_error_does_not_masquerade_as_native_refusal(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)

    class BrokenBackend(FakeBackend):
        def stage(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
        ) -> None:
            raise RuntimeError("insufficient_return_bag_slots")

    status = AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=BrokenBackend(observed_at=NOW + 1),
    )
    assert status["actions"][0]["reason"] == "stage failed: RuntimeError"


def test_reconciled_native_precondition_refusal_restores_post_stock_and_budget(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=NOW + 1, session_boot_id=BOOT, receipt_baseline=3)
    assert ledger.recover_interrupted(now=NOW + 2) == 1

    ledger.reconcile_receipt(
        batch.batch_id,
        0,
        0,
        {
            "sequence": 4,
            "batchId": batch.batch_id,
            "ordinal": 0,
            "stepIndex": 0,
            "callbackDelivered": True,
            "postcondition": False,
            "actionComplete": False,
            "detail": "native protected callback refused before applying the post",
        },
        boot_id=BOOT,
        step_limit=1,
        now=NOW + 3,
    )

    assert ledger.status(batch.batch_id)["state"] == "failed"
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_recycle_cancels_only_persisted_exact_ids_and_selective_return_is_phase_bound(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, post_decision, post_batch = first_post(store, session_id, selected, stock)
    post_backend = FakeBackend(
        {
            "kind": "post_commodity",
            "auctionId": 77,
            "itemKey": ITEM,
            "quantity": 5,
            "unitPriceCopper": 150,
            "depositCopper": 2,
        }
    )
    assert (
        execute(
            ledger,
            store,
            session_id,
            post_decision.decision_id,
            post_batch,
            post_backend,
            now=NOW + 1,
        )["state"]
        == "succeeded"
    )

    listing = OwnedAuction(77, ITEM, 5, 150, "active", True, True, 2)
    inventory_after_post = inventory(0, sequence=4, observed_at=NOW + 2)
    owned_after_post = owned((listing,), sequence=5, observed_at=NOW + 2)
    stock_after_post = store.reconcile_stock(session_id, inventory_after_post, owned_after_post, now=NOW + 2)
    market_after_post = market(
        request_id="b" * 32,
        sequence=6,
        observed_at=NOW + 2,
        priority="competitor",
        competing=125,
        managed=(listing,),
    )
    recycle = store.create_decision(session_id, market_after_post, stock_after_post, now=NOW + 2)
    assert recycle.kind == "recycle"
    assert recycle.actions == (CancelAuction(77, selected.items[0].max_cancel_cost_copper),)

    forged = Batch(
        HOUSE,
        CHARACTER,
        NOW + 30,
        (CancelAuction(78, selected.items[0].max_cancel_cost_copper),),
    )
    with pytest.raises(SellerStoreError, match="exact persisted"):
        store.prepare_child(session_id, recycle.decision_id, forged, now=NOW + 2)

    cancel_batch = Batch(HOUSE, CHARACTER, NOW + 30, recycle.actions)
    store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=NOW + 2)
    cancel_backend = FakeBackend(
        {
            "kind": "cancel_auction",
            "auctionId": 77,
            "itemKey": ITEM,
            "remainingQuantity": 4,
            "cancelCostCopper": 3,
        }
    )
    cancel_status = AutomationRunner(ledger, clock=lambda: NOW + 3).run_seller_child(
        cancel_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        backend=cancel_backend,
    )
    assert cancel_status["state"] == "succeeded"
    progress = store.item_progress(session_id, ITEM)
    assert (
        progress.managed_active_quantity,
        progress.returning_quantity,
        progress.unresolved_quantity,
        progress.sold_quantity,
    ) == (
        0,
        4,
        1,
        0,
    )
    assert progress.recycling and progress.recycle_rounds == 1
    assert progress.spent_copper == 5
    assert not store.managed_auctions(session_id, ITEM)

    outbound = store.actions_for_phase(session_id, recycle.decision_id, "travel_to_mail", now=NOW + 3)
    assert outbound == (MoveRoute(selected.outbound_route_id), Interact(selected.mailbox_target_id))
    outbound_batch = Batch(HOUSE, CHARACTER, NOW + 200, outbound)
    store.prepare_child(session_id, recycle.decision_id, outbound_batch, phase="travel_to_mail", now=NOW + 3)
    runner_config = RunnerConfig(
        frozenset({"auctioneer_main", "mailbox_main"}),
        (("ah_to_mail", 1), ("mail_to_ah", 1)),
    )
    outbound_status = AutomationRunner(ledger, config=runner_config, clock=lambda: NOW + 4).run_seller_child(
        outbound_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase="travel_to_mail",
        backend=FakeBackend(observed_at=NOW + 4),
    )
    assert outbound_status["state"] == "succeeded"

    ambiguous = ReturnCandidate(
        ITEM,
        4,
        "d" * 64,
        "returns:boot-a:8-8",
        BOOT,
        8,
        NOW + 4,
        True,
        False,
        "native selection token unavailable",
    )
    store.record_returns(session_id, recycle.decision_id, (ambiguous,), now=NOW + 4)
    with pytest.raises(SellerStoreError, match="ambiguous"):
        store.actions_for_phase(
            session_id, recycle.decision_id, f"collect_return:{ambiguous.mail_fingerprint}", now=NOW + 4
        )

    store.revoke(session_id, now=NOW + 4)
    mail_candidate = ReturnCandidate(
        ITEM,
        4,
        "c" * 64,
        "returns:boot-b:1-1",
        "boot-b",
        1,
        NOW + 5,
        True,
        True,
        "exact commodity return with one-use native selection token",
    )
    resumed_session = store.resume_at_mailbox(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(5, boot_id="boot-b", sequence=1, observed_at=NOW + 5),),
        mail_observations=(
            ReturnObservationFacts(
                ITEM,
                "returns:boot-b:1-1",
                "boot-b",
                1,
                NOW + 5,
                True,
                (mail_candidate,),
            ),
        ),
        now=NOW + 5,
    )
    assert resumed_session == session_id
    resumed_status = store.status(session_id, now=NOW + 5)
    assert resumed_status.state == "active"
    assert resumed_status.resume_location == "mailbox"
    assert resumed_status.boot_id == "boot-b"
    assert store.child_status(session_id, recycle.decision_id, "travel_to_mail") == "succeeded"
    assert store.child_batch(session_id, recycle.decision_id, "travel_to_mail") == outbound_batch

    candidate = ReturnCandidate(
        ITEM,
        4,
        "c" * 64,
        "returns:boot-b:1-1",
        "boot-b",
        1,
        NOW + 5,
        True,
        True,
        mail_candidate.reason,
    )
    store.record_returns(session_id, recycle.decision_id, (candidate,), now=NOW + 5)
    refreshed_candidate = ReturnCandidate(
        ITEM,
        4,
        candidate.mail_fingerprint,
        "returns:boot-b:2-2",
        "boot-b",
        2,
        NOW + 8,
        True,
        True,
        "same native one-use selection token observed again",
    )
    store.record_returns(session_id, recycle.decision_id, (refreshed_candidate,), now=NOW + 8)
    collect_phase = f"collect_return:{candidate.mail_fingerprint}"
    collect = store.actions_for_phase(session_id, recycle.decision_id, collect_phase, now=NOW + 9)
    assert collect == (CollectReturn(ITEM, 4, "c" * 64),)
    with pytest.raises(SellerStoreError, match="stale"):
        store.actions_for_phase(session_id, recycle.decision_id, collect_phase, now=NOW + 65)
    collect_batch = Batch(HOUSE, CHARACTER, NOW + 30, collect)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        collect_batch,
        phase=collect_phase,
        now=NOW + 9,
    )
    collect_backend = FakeBackend(
        {
            "kind": "collect_return",
            "itemKey": ITEM,
            "quantity": 4,
            "mailFingerprint": "c" * 64,
        },
        observed_at=NOW + 10,
        boot_id="boot-b",
    )
    collect_status = AutomationRunner(ledger, config=runner_config, clock=lambda: NOW + 10).run_seller_child(
        collect_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase=collect_phase,
        backend=collect_backend,
    )
    assert collect_status["state"] == "succeeded"
    progress = store.item_progress(session_id, ITEM)
    assert (
        progress.bag_quantity,
        progress.returning_quantity,
        progress.unresolved_quantity,
        progress.recycling,
    ) == (
        4,
        0,
        1,
        True,
    )

    inbound = store.actions_for_phase(session_id, recycle.decision_id, "travel_to_ah", now=NOW + 10)
    assert inbound == (MoveRoute(selected.return_route_id), Interact(selected.ah_target_id))
    inbound_batch = Batch(HOUSE, CHARACTER, NOW + 200, inbound)
    store.prepare_child(session_id, recycle.decision_id, inbound_batch, phase="travel_to_ah", now=NOW + 10)

    class FailedRouteBackend(FakeBackend):
        def dispatch(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
            admission: Admission,
        ) -> DispatchReceipt:
            self.dispatched.append(action)
            return DispatchReceipt(
                batch_id,
                ordinal,
                step_index,
                action_digest,
                admission.session_boot_id,
                True,
                False,
                False,
                detail="route precondition failed with no movement effect",
            )

    inbound_status = AutomationRunner(ledger, config=runner_config, clock=lambda: NOW + 11).run_seller_child(
        inbound_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase="travel_to_ah",
        backend=FailedRouteBackend(observed_at=NOW + 11, boot_id="boot-b"),
    )
    assert inbound_status["state"] == "failed"
    assert store.child_status(session_id, recycle.decision_id, "travel_to_ah") == "failed"

    store.revoke(session_id, now=NOW + 12)
    mailbox_epoch = ReturnObservationFacts(
        ITEM,
        "returns:boot-c:1-1",
        "boot-c",
        1,
        NOW + 13,
        True,
        (),
    )
    store.resume_at_mailbox(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-c",
        client_build=BUILD,
        inventories=(inventory(4, boot_id="boot-c", sequence=1, observed_at=NOW + 13),),
        mail_observations=(mailbox_epoch,),
        now=NOW + 13,
    )
    recovery_phase, recovery_actions = store.recovery_to_ah(session_id, recycle.decision_id, now=NOW + 13)
    assert recovery_phase.startswith("recovery_to_ah:")
    assert recovery_actions == inbound
    recovery_batch = Batch(HOUSE, CHARACTER, NOW + 250, recovery_actions)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        recovery_batch,
        phase=recovery_phase,
        now=NOW + 13,
    )
    inbound_status = AutomationRunner(ledger, config=runner_config, clock=lambda: NOW + 14).run_seller_child(
        recovery_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase=recovery_phase,
        backend=FakeBackend(observed_at=NOW + 14, boot_id="boot-c"),
    )
    assert inbound_status["state"] == "succeeded"
    assert store.status(session_id, now=NOW + 14).resume_location == "ah"
    assert not store.item_progress(session_id, ITEM).recycling


def test_partial_route_receipt_advances_seller_child_without_replaying_prior_step(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, post_decision, post_batch = first_post(store, session_id, selected, stock)
    post_backend = FakeBackend(
        {
            "kind": "post_commodity",
            "auctionId": 77,
            "itemKey": ITEM,
            "quantity": 5,
            "unitPriceCopper": 150,
            "depositCopper": 2,
        },
        observed_at=NOW + 1,
    )
    assert (
        AutomationRunner(ledger, clock=lambda: NOW + 1).run_seller_child(
            post_batch,
            seller_store=store,
            session_id=session_id,
            decision_id=post_decision.decision_id,
            backend=post_backend,
        )["state"]
        == "succeeded"
    )

    listing = OwnedAuction(77, ITEM, 5, 150, "active", True, True, 2)
    after_post = store.reconcile_stock(
        session_id,
        inventory(0, sequence=4, observed_at=NOW + 2),
        owned((listing,), sequence=5, observed_at=NOW + 2),
        now=NOW + 2,
    )
    recycle = store.create_decision(
        session_id,
        market(
            request_id="b" * 32,
            sequence=6,
            observed_at=NOW + 2,
            priority="competitor",
            competing=125,
            managed=(listing,),
        ),
        after_post,
        now=NOW + 2,
    )
    cancel_batch = Batch(HOUSE, CHARACTER, NOW + 30, recycle.actions)
    store.prepare_child(session_id, recycle.decision_id, cancel_batch, now=NOW + 2)
    cancel_backend = FakeBackend(
        {
            "kind": "cancel_auction",
            "auctionId": 77,
            "itemKey": ITEM,
            "remainingQuantity": 5,
            "cancelCostCopper": 3,
        },
        observed_at=NOW + 3,
    )
    assert (
        AutomationRunner(ledger, clock=lambda: NOW + 3).run_seller_child(
            cancel_batch,
            seller_store=store,
            session_id=session_id,
            decision_id=recycle.decision_id,
            backend=cancel_backend,
        )["state"]
        == "succeeded"
    )

    outbound = store.actions_for_phase(session_id, recycle.decision_id, "travel_to_mail", now=NOW + 3)
    outbound_batch = Batch(HOUSE, CHARACTER, NOW + 200, outbound)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        outbound_batch,
        phase="travel_to_mail",
        now=NOW + 3,
    )
    ledger.claim_dispatch(
        outbound_batch.batch_id,
        0,
        0,
        now=NOW + 4,
        session_boot_id=BOOT,
        receipt_baseline=10,
    )
    assert ledger.recover_interrupted(now=NOW + 5) == 1
    ledger.reconcile_receipt(
        outbound_batch.batch_id,
        0,
        0,
        {
            "sequence": 11,
            "batchId": outbound_batch.batch_id,
            "ordinal": 0,
            "stepIndex": 0,
            "callbackDelivered": True,
            "postcondition": True,
            "actionComplete": False,
            "detail": "first route step completed; continue at step one",
        },
        boot_id=BOOT,
        step_limit=2,
        now=NOW + 5,
    )
    partial = ledger.status(outbound_batch.batch_id)
    assert partial["state"] == "running"
    assert partial["actions"][0]["next_step"] == 1
    with ledger._connect() as db:
        child_state = db.execute(
            "SELECT state FROM seller_children WHERE batch_id=?", (outbound_batch.batch_id,)
        ).fetchone()["state"]
    assert child_state == "running"
    assert store.item_progress(session_id, ITEM).returning_quantity == 5

    resumed_backend = FakeBackend(observed_at=NOW + 6)
    route_config = RunnerConfig(frozenset({"auctioneer_main", "mailbox_main"}), (("ah_to_mail", 2),))
    resumed = AutomationRunner(ledger, config=route_config, clock=lambda: NOW + 6).run_seller_child(
        outbound_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase="travel_to_mail",
        backend=resumed_backend,
    )
    assert resumed["state"] == "succeeded"
    assert resumed_backend.dispatched == [
        MoveRoute(selected.outbound_route_id),
        Interact(selected.mailbox_target_id),
    ]
    store.revoke(session_id, now=NOW + 7)
    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-c",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-c", sequence=1, observed_at=NOW + 7),),
        owned_facts=(owned(boot_id="boot-c", sequence=2, observed_at=NOW + 7),),
        now=NOW + 7,
    )
    recovery_phase, recovery_actions = store.recovery_to_mail(session_id, recycle.decision_id, now=NOW + 8)
    assert recovery_phase.startswith("recovery_to_mail:")
    assert recovery_actions == outbound
    assert store.child_status(session_id, recycle.decision_id, "travel_to_mail") == "succeeded"
    assert store.child_batch(session_id, recycle.decision_id, "travel_to_mail") == outbound_batch
    recovery_batch = Batch(HOUSE, CHARACTER, NOW + 250, recovery_actions)
    store.prepare_child(
        session_id,
        recycle.decision_id,
        recovery_batch,
        phase=recovery_phase,
        now=NOW + 8,
    )
    recovery_status = AutomationRunner(
        ledger,
        config=route_config,
        clock=lambda: NOW + 9,
    ).run_seller_child(
        recovery_batch,
        seller_store=store,
        session_id=session_id,
        decision_id=recycle.decision_id,
        phase=recovery_phase,
        backend=FakeBackend(observed_at=NOW + 9, boot_id="boot-c"),
    )
    assert recovery_status["state"] == "succeeded"
    assert store.status(session_id, now=NOW + 9).resume_location == "mailbox"


def test_stock_observation_freshness_allows_coherent_paged_reads_but_expires_after_one_interval(
    tmp_path: Path,
) -> None:
    _, store, session_id, _, _ = make_session(tmp_path / "ledger.sqlite3")
    fresh_inventory = inventory(5, sequence=3, observed_at=NOW + 10)
    fresh_owned = owned(sequence=4, observed_at=NOW + 15)
    stock = store.reconcile_stock(session_id, fresh_inventory, fresh_owned, now=NOW + 20)
    assert stock.bag_quantity == 5
    decision = store.create_decision(
        session_id, market(sequence=5, observed_at=NOW + 20), stock, now=NOW + 20
    )
    assert decision.kind == "post"

    stale_inventory = inventory(5, sequence=5, observed_at=NOW - 61)
    stale_owned = owned(sequence=6, observed_at=NOW + 20)
    with pytest.raises(SellerStoreError, match="stale"):
        store.reconcile_stock(session_id, stale_inventory, stale_owned, now=NOW + 20)

    with pytest.raises(SellerStoreError, match="stale"):
        store.create_decision(
            session_id,
            market(request_id="f" * 32, sequence=7, observed_at=NOW + 76),
            stock,
            now=NOW + 76,
        )


def test_stock_provenance_is_rechecked_before_stage_and_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger, store, session_id, selected, stock = make_session(path)
    facts = market(request_id="e" * 32, sequence=3, observed_at=NOW + 1)
    decision = store.create_decision(session_id, facts, stock, now=NOW + 1)
    batch = Batch(HOUSE, CHARACTER, NOW + 20, decision.actions)
    store.prepare_child(session_id, decision.decision_id, batch, now=NOW + 1)

    class ChangedStockBackend(FakeBackend):
        def stage(
            self,
            action: object,
            *,
            batch_id: str,
            ordinal: int,
            step_index: int,
            action_digest: str,
        ) -> None:
            super().stage(
                action,
                batch_id=batch_id,
                ordinal=ordinal,
                step_index=step_index,
                action_digest=action_digest,
            )
            store.reconcile_stock(
                session_id,
                inventory(5, sequence=3, observed_at=NOW + 2),
                owned(sequence=4, observed_at=NOW + 2),
                now=NOW + 2,
            )

    backend = ChangedStockBackend(observed_at=NOW + 2)
    status = AutomationRunner(ledger, clock=lambda: NOW + 2).run_seller_child(
        batch,
        seller_store=store,
        session_id=session_id,
        decision_id=decision.decision_id,
        backend=backend,
    )
    assert status["state"] == "failed"
    assert backend.dispatched == []
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.reserved_spend_copper) == (
        5,
        0,
        0,
    )


def test_revoked_session_denies_runner_before_any_staging(tmp_path: Path) -> None:
    ledger, store, session_id, selected, stock = make_session(tmp_path / "ledger.sqlite3")
    _, decision, batch = first_post(store, session_id, selected, stock)
    store.revoke(session_id, now=NOW + 1)
    backend = FakeBackend()
    runner = AutomationRunner(ledger, clock=lambda: NOW + 1)
    with pytest.raises((SellerStoreError, AdmissionDenied), match="revoked"):
        runner.run_seller_child(
            batch,
            seller_store=store,
            session_id=session_id,
            decision_id=decision.decision_id,
            backend=backend,
        )
    assert backend.staged is None


def test_route_less_recycle_completion_preserves_unresolved_partial_sale_after_ah_resume(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, recycle = canceled_recycle(tmp_path / "ledger.sqlite3")
    collect_return_at_mailbox(ledger, store, session_id, selected, recycle, quantity=4, observed_at=NOW + 5)
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.returning_quantity, progress.unresolved_quantity) == (
        4,
        0,
        1,
    )

    with pytest.raises(SellerStoreError, match="explicit fresh AH resume"):
        store.complete_recycle_at_ah(session_id, recycle.decision_id, now=NOW + 7)

    store.revoke(session_id, now=NOW + 8)
    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-c",
        client_build=BUILD,
        inventories=(inventory(4, boot_id="boot-c", sequence=1, observed_at=NOW + 9),),
        owned_facts=(owned(boot_id="boot-c", sequence=2, observed_at=NOW + 9),),
        now=NOW + 9,
    )
    store.complete_recycle_at_ah(session_id, recycle.decision_id, now=NOW + 9)

    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.recycling) == (4, 1, False)
    assert progress.recycle_rounds == 1
    assert progress.spent_copper == 5
    assert store.child_status(session_id, recycle.decision_id, "travel_to_ah") is None
    with ledger._connect() as db:
        phases = db.execute(
            "SELECT phase FROM seller_children WHERE session_id=? AND decision_id=?",
            (session_id, recycle.decision_id),
        ).fetchall()
    assert not any(
        row["phase"] == "travel_to_ah" or row["phase"].startswith("recovery_to_ah:") for row in phases
    )


def test_recycle_completion_rejects_return_stock_without_successful_collection(tmp_path: Path) -> None:
    _, store, session_id, selected, recycle = canceled_recycle(
        tmp_path / "ledger.sqlite3", remaining_quantity=5
    )
    store.revoke(session_id, now=NOW + 4)
    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-b", sequence=1, observed_at=NOW + 5),),
        owned_facts=(owned(boot_id="boot-b", sequence=2, observed_at=NOW + 5),),
        now=NOW + 5,
    )
    with pytest.raises(SellerStoreError, match="not fully reconciled"):
        store.complete_recycle_at_ah(session_id, recycle.decision_id, now=NOW + 5)
    assert store.item_progress(session_id, ITEM).returning_quantity == 5


def test_expired_release_clears_fully_reconciled_unfinished_recycle_anchor(tmp_path: Path) -> None:
    selected = policy(expires_at=NOW + 100)
    ledger, store, session_id, selected, recycle = canceled_recycle(
        tmp_path / "ledger.sqlite3", remaining_quantity=5, selected=selected
    )
    collect_return_at_mailbox(ledger, store, session_id, selected, recycle, quantity=5, observed_at=NOW + 5)
    released_at = selected.expires_at + 1
    assert (
        store.release_expired_stock(
            session_id,
            confirmed_policy_id=selected.policy_id,
            boot_id="boot-c",
            client_build=BUILD,
            inventories=(inventory(5, boot_id="boot-c", sequence=1, observed_at=released_at),),
            owned_facts=(owned(boot_id="boot-c", sequence=2, observed_at=released_at),),
            now=released_at,
        )
        == session_id
    )
    progress = store.item_progress(session_id, ITEM)
    assert (progress.bag_quantity, progress.unresolved_quantity, progress.recycling) == (5, 0, False)
    assert progress.recycle_rounds == 1
    assert progress.spent_copper == 5
    assert progress.finished


def test_expired_ah_recovery_route_is_released_and_rederived_after_fresh_ah_resume(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, recycle = canceled_recycle(
        tmp_path / "ledger.sqlite3", remaining_quantity=5
    )
    store.revoke(session_id, now=NOW + 4)
    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-b",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-b", sequence=1, observed_at=NOW + 5),),
        owned_facts=(owned(boot_id="boot-b", sequence=2, observed_at=NOW + 5),),
        now=NOW + 5,
    )
    old_phase, actions = store.recovery_to_mail(session_id, recycle.decision_id, now=NOW + 5)
    old_batch = Batch(HOUSE, CHARACTER, NOW + 6, actions)
    store.prepare_child(session_id, recycle.decision_id, old_batch, phase=old_phase, now=NOW + 5)
    store.revoke(session_id, now=NOW + 6)

    store.resume(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-c",
        client_build=BUILD,
        inventories=(inventory(0, boot_id="boot-c", sequence=1, observed_at=NOW + 10),),
        owned_facts=(owned(boot_id="boot-c", sequence=2, observed_at=NOW + 10),),
        now=NOW + 10,
    )
    new_phase, new_actions = store.recovery_to_mail(session_id, recycle.decision_id, now=NOW + 10)
    assert new_phase != old_phase
    assert new_actions == actions
    assert store.child_status(session_id, recycle.decision_id, old_phase) == "failed"
    assert ledger.status(old_batch.batch_id)["state"] == "expired"


def test_expired_mailbox_recovery_route_is_released_and_rederived_after_fresh_resume(
    tmp_path: Path,
) -> None:
    ledger, store, session_id, selected, recycle = canceled_recycle(
        tmp_path / "ledger.sqlite3", remaining_quantity=5
    )
    collect_return_at_mailbox(ledger, store, session_id, selected, recycle, quantity=5, observed_at=NOW + 5)
    old_phase, actions = store.recovery_to_ah(session_id, recycle.decision_id, now=NOW + 6)
    old_batch = Batch(HOUSE, CHARACTER, NOW + 7, actions)
    store.prepare_child(session_id, recycle.decision_id, old_batch, phase=old_phase, now=NOW + 6)
    store.revoke(session_id, now=NOW + 7)

    store.resume_at_mailbox(
        session_id,
        confirmed_policy_id=selected.policy_id,
        boot_id="boot-c",
        client_build=BUILD,
        inventories=(inventory(5, boot_id="boot-c", sequence=1, observed_at=NOW + 10),),
        mail_observations=(
            ReturnObservationFacts(
                ITEM,
                "returns:boot-c:1-1",
                "boot-c",
                1,
                NOW + 10,
                True,
                (),
            ),
        ),
        now=NOW + 10,
    )
    new_phase, new_actions = store.recovery_to_ah(session_id, recycle.decision_id, now=NOW + 10)
    assert new_phase != old_phase
    assert new_actions == actions
    assert store.child_status(session_id, recycle.decision_id, old_phase) == "failed"
    assert ledger.status(old_batch.batch_id)["state"] == "expired"
