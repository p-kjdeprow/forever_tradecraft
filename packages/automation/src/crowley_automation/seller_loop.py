"""Foreground seller controller and its serial monotonic item scheduler."""

from __future__ import annotations

import math
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from crowley_automation.backend import DesktopBackend, DesktopBackendError
from crowley_automation.ledger import Ledger, UncertainEffectInterlock
from crowley_automation.model import Action, Batch, CancelAuction, PostCommodity
from crowley_automation.runtime import arm_seller_backend, run_seller_child
from crowley_automation.seller import (
    SELLER_DECISION_MAX_AGE_SECONDS,
    SELLER_PHASE_MAX_AGE_SECONDS,
    InventoryFacts,
    MarketFacts,
    OwnedAuction,
    OwnedFacts,
    ReturnCandidate,
    SellerDecision,
    SellerItemPolicy,
    SellerPolicy,
    SellerPolicyError,
    SellerSessionStatus,
    StockFacts,
)
from crowley_automation.seller_store import SellerStore, SellerStoreError

SELLER_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ObservationSlot:
    item_key: str
    due_monotonic: float
    last_success_monotonic: float | None


class _FairItemSchedule:
    """Keep item checks serial, fair and at least one interval apart per item."""

    def __init__(self, item_keys: tuple[str, ...], started_monotonic: float) -> None:
        if not item_keys or len(set(item_keys)) != len(item_keys):
            raise ValueError("seller schedule requires unique item keys")
        if any(type(key) is not str or not key for key in item_keys):
            raise ValueError("seller schedule item keys must be non-empty text")
        if type(started_monotonic) not in (int, float) or not math.isfinite(started_monotonic):
            raise ValueError("seller schedule start time must be finite monotonic seconds")
        self._order = item_keys
        self._rank = {item_key: rank for rank, item_key in enumerate(item_keys)}
        self._due = dict.fromkeys(item_keys, float(started_monotonic))
        self._last_success: dict[str, float | None] = dict.fromkeys(item_keys)

    def next_due(self, now_monotonic: float) -> ObservationSlot | None:
        due_items = (item_key for item_key in self._order if self._due[item_key] <= now_monotonic)
        item_key = min(due_items, key=lambda key: (self._due[key], self._rank[key]), default=None)
        if item_key is None:
            return None
        return ObservationSlot(item_key, self._due[item_key], self._last_success[item_key])

    def next_delay(self, now_monotonic: float) -> float:
        return max(0.0, min(self._due.values()) - now_monotonic)

    def observed(
        self,
        item_key: str,
        *,
        started_monotonic: float,
        completed_monotonic: float,
        observation_monotonic: float | None = None,
    ) -> float:
        self._check_item(item_key)
        observed_at = completed_monotonic if observation_monotonic is None else observation_monotonic
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in (started_monotonic, completed_monotonic, observed_at)
        ):
            raise ValueError("monotonic observation times must be finite numbers")
        if observed_at < started_monotonic:
            raise ValueError("observation precedes the item check start")
        if completed_monotonic < observed_at:
            raise ValueError("item completion precedes the successful observation")
        lateness = max(0.0, started_monotonic - self._due[item_key])
        self._last_success[item_key] = observed_at
        # Schedule from completion so a slow query never causes catch-up bursts.
        self._due[item_key] = completed_monotonic + SELLER_INTERVAL_SECONDS
        return lateness

    def failed(self, item_key: str, *, completed_monotonic: float) -> None:
        self._check_item(item_key)
        if type(completed_monotonic) not in (int, float) or not math.isfinite(completed_monotonic):
            raise ValueError("monotonic observation time must be finite")
        # Preserve the last successful observation while avoiding a hot retry loop.
        self._due[item_key] = completed_monotonic + SELLER_INTERVAL_SECONDS

    def checked(self, item_key: str, *, started_monotonic: float, completed_monotonic: float) -> float:
        """Defer a completed progress check while preserving its last market observation."""
        self._check_item(item_key)
        if (
            type(started_monotonic) not in (int, float)
            or type(completed_monotonic) not in (int, float)
            or not math.isfinite(started_monotonic)
            or not math.isfinite(completed_monotonic)
            or completed_monotonic < started_monotonic
        ):
            raise ValueError("item check completion is outside its monotonic interval")
        lateness = max(0.0, started_monotonic - self._due[item_key])
        self._due[item_key] = completed_monotonic + SELLER_INTERVAL_SECONDS
        return lateness

    def slot(self, item_key: str) -> ObservationSlot:
        self._check_item(item_key)
        return ObservationSlot(item_key, self._due[item_key], self._last_success[item_key])

    def _check_item(self, item_key: str) -> None:
        if item_key not in self._due:
            raise ValueError("item is not admitted to this seller schedule")

    def complete(self, item_key: str) -> None:
        self._check_item(item_key)
        self._due[item_key] = math.inf


class SellerLoopError(ValueError):
    """The approved seller policy does not match its current runtime facts."""


def inventory_facts_for_policy(policy: SellerPolicy, observation: Any) -> tuple[InventoryFacts, ...]:
    """Aggregate exact eligible bag rows into the policy's bounded admitted identities."""
    rows: list[InventoryFacts] = []
    for item in policy.items:
        quantity = sum(
            fact.quantity
            for fact in observation.facts
            if fact.item_key == item.item_key and fact.commodity and not fact.locked and not fact.bound
        )
        rows.append(
            InventoryFacts(
                item.item_key,
                observation.request_id,
                observation.boot,
                observation.sequence,
                observation.captured_at,
                observation.complete,
                quantity,
            )
        )
    return tuple(rows)


def _validate_seller_profiles(
    policy: SellerPolicy, config: Any, observation: dict[str, Any], backend: Any
) -> tuple[Any, Any]:
    context = observation["context"]
    build = context["clientBuild"]
    if (
        policy.house != config.house
        or policy.character != config.character
        or policy.character != context["character"]
        or policy.client_build != build
        or policy.client_build != config.build.rsplit(".", 1)[1]
    ):
        raise SellerLoopError("seller policy house, character or client build differs from the live client")
    ah = backend.targets.get(policy.ah_target_id)
    mailbox = backend.targets.get(policy.mailbox_target_id)
    outbound = backend.routes.get(policy.outbound_route_id)
    returning = backend.routes.get(policy.return_route_id)
    if ah is None or ah.kind != "npc" or ah.expected_ui != "ah":
        raise SellerLoopError("seller policy Auction House target has no matching reviewed profile")
    if mailbox is None or mailbox.kind != "mailbox" or mailbox.expected_ui != "mail":
        raise SellerLoopError("seller policy mailbox target has no matching reviewed profile")
    for profile in (ah, mailbox, outbound, returning):
        if profile is None or (
            profile.house,
            profile.character,
            profile.client_build,
        ) != (policy.house, policy.character, policy.client_build):
            raise SellerLoopError("seller route or target profile identity differs from policy")
    bind_target = getattr(backend, "bind_seller_ah_target", None)
    if not callable(bind_target):
        raise SellerLoopError("desktop backend cannot bind the reviewed seller Auction House source")
    bind_target(policy.ah_target_id)
    return ah, mailbox


def validate_seller_runtime(
    policy: SellerPolicy, config: Any, observation: dict[str, Any], backend: Any
) -> None:
    """Reject identity/profile drift before a session is authorized or previewed at the AH."""
    ah, _mailbox = _validate_seller_profiles(policy, config, observation, backend)
    motion = observation["motion"]
    target = motion["target"]
    auctioneer = observation["auctioneer"]
    if (
        observation["ahOpen"] is not True
        or motion["instance"] != ah.instance_id
        or target["present"] is not True
        or target["guid"] != ah.guid
        or target["name"] != ah.name
        or target["rangeAvailable"] is not True
        or target["withinRange"] is not True
        or auctioneer["targetPresent"] is not True
        or auctioneer["targetName"] != ah.name
        or auctioneer["rangeAvailable"] is not True
        or auctioneer["withinTradeRange"] is not True
    ):
        raise SellerLoopError("open Auction House is not proved at the policy's measured NPC source")


def validate_seller_mailbox_runtime(
    policy: SellerPolicy, config: Any, observation: dict[str, Any], backend: Any
) -> None:
    """Prove the live mailbox UI and measured anchor for an explicitly resumed recycle pass."""
    _ah, mailbox = _validate_seller_profiles(policy, config, observation, backend)
    motion = observation["motion"]
    anchor_x, anchor_y, max_distance = mailbox.anchor_x, mailbox.anchor_y, mailbox.max_distance
    if (
        observation["mailOpen"] is not True
        or motion["instance"] != mailbox.instance_id
        or motion["moving"] is not False
        or motion["dead"] is not False
        or motion["inCombat"] is not False
        or type(motion["x"]) not in (int, float)
        or type(motion["y"]) not in (int, float)
        or any(type(value) not in (int, float) for value in (anchor_x, anchor_y, max_distance))
        or math.hypot(motion["x"] - anchor_x, motion["y"] - anchor_y) > max_distance
    ):
        raise SellerLoopError("mailbox resume is not proved at the policy's measured mailbox anchor")


class SellerLoop:
    """Run one durable, serial seller session in the foreground."""

    def __init__(
        self,
        policy: SellerPolicy,
        session_id: str,
        *,
        store: SellerStore,
        ledger: Ledger,
        runner: Any,
        backend: DesktopBackend,
        native: Any,
        progress: Callable[[dict[str, object]], None] | None = None,
        at_mailbox_item_key: str | None = None,
    ) -> None:
        self.policy = policy
        self.session_id = session_id
        self.store = store
        self.ledger = ledger
        self.runner = runner
        self.backend = backend
        self.native = native
        self.io = backend.io
        self.progress = progress or (lambda _event: None)
        self._at_mailbox = at_mailbox_item_key is not None
        self._mailbox_item_key = at_mailbox_item_key
        self._pause_reason: str | None = None
        initial_status = store.status(session_id, now=int(self.io.wall_time()))
        self._resume_epoch_usable = initial_status.resume_location in {"ah", "mailbox"}
        self._last_decisions: dict[str, dict[str, object]] = {}
        self._last_child_state: dict[str, str] = {}
        self._last_observation_monotonic: dict[str, float] = {}
        if at_mailbox_item_key is not None:
            if at_mailbox_item_key not in {item.item_key for item in policy.items}:
                raise SellerLoopError("mailbox resume names an item outside the immutable seller policy")
            progress_row = store.item_progress(session_id, at_mailbox_item_key)
            if (
                not progress_row.recycling
                or progress_row.managed_active_quantity
                or not (progress_row.returning_quantity or progress_row.bag_quantity)
                or initial_status.resume_location != "mailbox"
            ):
                raise SellerLoopError("mailbox resume is outside a pending exact recycle return phase")
        arm_seller_backend(
            backend,
            ledger=ledger,
            seller_store=store,
            session_id=session_id,
            policy_expires_at=policy.expires_at,
            ah_target_id=policy.ah_target_id,
        )

    def run(self) -> dict[str, object]:
        schedule = _FairItemSchedule(tuple(item.item_key for item in self.policy.items), self.io.monotonic())
        while True:
            status = self.store.status(self.session_id, now=int(self.io.wall_time()))
            if status.state != "active":
                return self._result(status)
            if self.ledger.stop_requested():
                self.store.revoke(self.session_id, "durable automation stop", now=int(self.io.wall_time()))
                return self._result(self.store.status(self.session_id, now=int(self.io.wall_time())))
            if self.ledger.has_uncertain_effect():
                return self._result(status, reason="global uncertain-effect interlock is active")

            now = self.io.monotonic()
            if self._at_mailbox and self._mailbox_item_key is not None:
                mailbox_slot = schedule.slot(self._mailbox_item_key)
                slot = mailbox_slot if mailbox_slot.due_monotonic <= now else None
            else:
                slot = schedule.next_due(now)
            if slot is None:
                delay = (
                    max(0.0, schedule.slot(self._mailbox_item_key).due_monotonic - now)
                    if self._at_mailbox and self._mailbox_item_key is not None
                    else schedule.next_delay(now)
                )
                self.io.sleep(min(delay, 1.0))
                continue

            started = self.io.monotonic()
            prior_observation = self._last_observation_monotonic.get(slot.item_key)
            try:
                outcome = self._check_item(self.policy.item(slot.item_key))
            except DesktopBackendError as exc:
                self.store.revoke(
                    self.session_id, "desktop session gate failed", now=int(self.io.wall_time())
                )
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason=f"desktop gate stopped the foreground loop: {str(exc)[:160]}",
                )
            except UncertainEffectInterlock:
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason="global uncertain-effect interlock is active",
                )
            except sqlite3.Error as exc:
                self.store.revoke(self.session_id, "seller ledger read failed", now=int(self.io.wall_time()))
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason=(
                        f"seller ledger stopped the foreground loop: {type(exc).__name__}: {str(exc)[:120]}"
                    ),
                )
            except SellerStoreError as exc:
                self.store.revoke(
                    self.session_id, "seller state validation failed", now=int(self.io.wall_time())
                )
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason=f"seller state stopped the foreground loop: {str(exc)[:160]}",
                )
            except SellerPolicyError as exc:
                self.store.revoke(
                    self.session_id, "seller fact validation failed", now=int(self.io.wall_time())
                )
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason=f"seller fact stopped the foreground loop: {str(exc)[:160]}",
                )
            except Exception as exc:
                from .seller_native import SellerNativeError

                if not isinstance(exc, SellerNativeError):
                    raise
                completed = self.io.monotonic()
                schedule.failed(slot.item_key, completed_monotonic=completed)
                self.progress(
                    {
                        "event": "seller_observation_deferred",
                        "itemKey": slot.item_key,
                        "reason": str(exc)[:160],
                    }
                )
                continue

            completed = self.io.monotonic()
            if outcome == "finished":
                schedule.complete(slot.item_key)
            else:
                observation = self._last_observation_monotonic.get(slot.item_key)
                if observation is None or observation == prior_observation:
                    lateness = schedule.checked(
                        slot.item_key,
                        started_monotonic=started,
                        completed_monotonic=completed,
                    )
                else:
                    lateness = schedule.observed(
                        slot.item_key,
                        started_monotonic=started,
                        completed_monotonic=completed,
                        observation_monotonic=observation,
                    )
                self.progress(
                    {
                        "event": "seller_item_checked",
                        "itemKey": slot.item_key,
                        "latenessSeconds": round(lateness, 3),
                    }
                )
            if outcome == "paused":
                return self._result(
                    self.store.status(self.session_id, now=int(self.io.wall_time())),
                    reason=self._pause_reason
                    or "recycling requires return evidence that native facts cannot currently establish",
                )

    def _check_item(self, item: SellerItemPolicy) -> str:
        progress = self.store.item_progress(self.session_id, item.item_key)
        if progress.finished:
            return "finished"
        if progress.recycling:
            if progress.managed_active_quantity:
                anchor = progress.recycle_anchor_decision_id
                if anchor is None:
                    raise SellerStoreError("recycle progress has no exact decision anchor")
                inventory, owned = self._fresh_snapshot(item)
                phase, actions = self.store.continue_recycle(
                    self.session_id,
                    anchor,
                    inventory,
                    owned,
                    now=int(self.io.wall_time()),
                )
                self.progress(
                    {
                        "event": "seller_recycle_continuation",
                        "itemKey": item.item_key,
                        "decisionId": anchor,
                        "phase": phase,
                        "auctionIds": [action.auction_id for action in actions],
                    }
                )
                result = self._run_phase(
                    anchor,
                    phase,
                    actions_override=actions,
                    evidence_expires_at=min(inventory.observed_at + 60, owned.observed_at + 60),
                )
                if result is None or result.get("state") != "succeeded":
                    return "checked"
                progress = self.store.item_progress(self.session_id, item.item_key)
                if not progress.recycling:
                    refreshed = self._observe_decision(item)
                    return "finished" if refreshed.kind == "finish" else "checked"
            outcome = self._advance_recycle(item, progress.recycle_anchor_decision_id)
            if outcome != "returned_to_ah":
                return outcome
            # The return route suspends market monitoring. Refresh before using any returned stock.
        decision = self._observe_decision(item)
        if decision.kind == "finish":
            return "finished"
        if decision.kind not in {"post", "recycle"}:
            return "checked"
        if decision.kind == "recycle" and any(
            other.item_key != item.item_key
            and self.store.item_progress(self.session_id, other.item_key).recycling
            for other in self.policy.items
        ):
            self.progress(
                {
                    "event": "seller_recycle_deferred",
                    "itemKey": item.item_key,
                    "decisionId": decision.decision_id,
                    "reason": "another item has the sole active recycle pass",
                }
            )
            return "checked"
        result = self._run_phase(decision.decision_id, "decision", decision=decision)
        if result is None or result.get("state") != "succeeded":
            return "checked"
        if decision.kind == "post":
            # Confirm the managed listing promptly; this read cannot authorize another child.
            self._observe_decision(item)
            return "checked"
        current = self.store.item_progress(self.session_id, item.item_key)
        outcome = self._advance_recycle(item, current.recycle_anchor_decision_id)
        if outcome == "returned_to_ah":
            refreshed = self._observe_decision(item)
            if refreshed.kind == "post":
                result = self._run_phase(refreshed.decision_id, "decision", decision=refreshed)
                if result is not None and result.get("state") == "succeeded":
                    self._observe_decision(item)
            elif refreshed.kind == "finish":
                return "finished"
            return "checked"
        return outcome

    def _observe_decision(self, item: SellerItemPolicy) -> SellerDecision:
        stock, managed_owned = self._fresh_stock(item)
        managed_ids = frozenset(row.auction_id for row in managed_owned)
        if len(managed_ids) > 1024:
            from .seller_native import SellerNativeError

            raise SellerNativeError("managed auction count exceeds the bounded targeted-query contract")
        self.backend.set_deadline(self.policy.expires_at)
        market = self.native.market(item.item_key, secrets.token_hex(16), managed_ids)
        self._last_observation_monotonic[item.item_key] = self.io.monotonic()
        if type(market) is not MarketFacts:
            raise SellerPolicyError("native market adapter did not return exact MarketFacts")
        if market.managed_auction_ids != tuple(sorted(managed_ids)):
            raise SellerPolicyError("native market result does not bind every persisted managed auction ID")
        decision = self.store.create_decision(
            self.session_id,
            market,
            stock,
            now=int(self.io.wall_time()),
        )
        self._last_decisions[item.item_key] = decision.object()
        self.progress(
            {
                "event": "seller_decision",
                "itemKey": item.item_key,
                "kind": decision.kind,
                "reason": decision.reason,
                "quantity": decision.quantity,
                "unitPriceCopper": decision.unit_price_copper,
                "requestId": decision.request_id,
                "clientBootId": decision.client_boot_id,
                "sequence": decision.sequence,
                "evidenceDigest": decision.evidence_digest,
            }
        )
        return decision

    def _fresh_snapshot(self, item: SellerItemPolicy) -> tuple[InventoryFacts, OwnedFacts]:
        self.backend.set_deadline(self.policy.expires_at)
        inventory_observation = self.native.inventory()
        self.backend.set_deadline(self.policy.expires_at)
        owned_observation = self.native.owned()
        now = int(self.io.wall_time())
        if inventory_observation.boot != owned_observation.boot:
            raise SellerPolicyError("inventory and owned observations came from different client boots")
        if inventory_observation.boot != self.store.status(self.session_id, now=now).boot_id:
            raise SellerPolicyError("native observation boot differs from the authorized seller session")

        inventory = next(
            row
            for row in inventory_facts_for_policy(self.policy, inventory_observation)
            if row.item_key == item.item_key
        )

        stored_auctions = {
            row.auction_id: row for row in self.store.managed_auctions(self.session_id, item.item_key)
        }
        stored_ids = set(stored_auctions)
        auctions: list[OwnedAuction] = []
        for row in owned_observation.facts:
            if row.item_key != item.item_key:
                continue
            if row.status not in {"active", "sold"}:
                if row.auction_id in stored_ids:
                    raise SellerPolicyError("managed auction ownership facts are incomplete")
                continue
            if row.status == "sold":
                stored = stored_auctions.get(row.auction_id)
                if stored is None:
                    # Manual historical rows are never adopted into seller stock.
                    continue
                unit_price = stored.unit_price_copper
            else:
                if not row.buyout_known or type(row.buyout_copper) is not int or row.buyout_copper < 1:
                    if row.auction_id in stored_ids:
                        raise SellerPolicyError("managed auction unit price is unknown")
                    continue
                if row.quantity < 1 or row.buyout_copper % row.quantity:
                    if row.auction_id in stored_ids:
                        raise SellerPolicyError("managed auction total buyout has no exact unit price")
                    continue
                unit_price = row.buyout_copper // row.quantity
            auctions.append(
                OwnedAuction(
                    row.auction_id,
                    row.item_key,
                    row.quantity,
                    unit_price,
                    row.status,
                    row.cancel_allowed,
                    row.cancel_cost_known,
                    row.cancel_cost_copper if row.cancel_cost_known else 0,
                )
            )
        auctions.sort(key=lambda row: row.auction_id)
        owned = OwnedFacts(
            item.item_key,
            owned_observation.request_id,
            owned_observation.boot,
            owned_observation.sequence,
            owned_observation.captured_at,
            owned_observation.complete,
            tuple(auctions),
        )
        return inventory, owned

    def _fresh_stock(self, item: SellerItemPolicy) -> tuple[StockFacts, tuple[OwnedAuction, ...]]:
        inventory, owned = self._fresh_snapshot(item)
        now = int(self.io.wall_time())
        stock = self.store.reconcile_stock(self.session_id, inventory, owned, now=now)
        active_ids = {row.auction_id for row in self.store.managed_auctions(self.session_id, item.item_key)}
        managed_owned = tuple(
            row for row in owned.auctions if row.auction_id in active_ids and row.status == "active"
        )
        return stock, managed_owned

    def _run_phase(
        self,
        decision_id: str,
        phase: str,
        *,
        decision: SellerDecision | None = None,
        evidence_expires_at: int | None = None,
        actions_override: tuple[Action, ...] | None = None,
    ) -> dict[str, object] | None:
        now = int(self.io.wall_time())
        actions = self.store.actions_for_phase(
            self.session_id,
            decision_id,
            phase,
            now=now,
        )
        if actions_override is not None and actions != actions_override:
            raise SellerStoreError("seller store changed the exact frozen recycle continuation actions")
        if not actions:
            return None
        if phase == "decision" and (decision is None or actions != decision.actions):
            raise SellerStoreError("seller store returned child actions outside the persisted decision")
        if phase.startswith("continue_recycle:") and actions_override is None:
            raise SellerStoreError("recycle continuation lacks fresh persisted continuation actions")
        if phase in {"travel_to_mail", "travel_to_ah"} or phase.startswith(
            ("recovery_to_mail:", "recovery_to_ah:")
        ):
            expires_at = min(self.policy.expires_at, now + SELLER_PHASE_MAX_AGE_SECONDS)
        elif phase.startswith("collect_return:"):
            expires_at = min(
                self.policy.expires_at,
                now + SELLER_DECISION_MAX_AGE_SECONDS if evidence_expires_at is None else evidence_expires_at,
            )
        elif phase.startswith("continue_recycle:"):
            if evidence_expires_at is None:
                raise SellerStoreError("recycle continuation requires fresh inventory and owned deadlines")
            expires_at = min(
                self.policy.expires_at, now + SELLER_DECISION_MAX_AGE_SECONDS, evidence_expires_at
            )
        else:
            if decision is None:
                raise SellerStoreError("decision child lacks its exact persisted decision facts")
            expires_at = min(self.policy.expires_at, decision.observed_at + SELLER_DECISION_MAX_AGE_SECONDS)
        existing_batch = self.store.child_batch(self.session_id, decision_id, phase)
        if existing_batch is not None:
            if existing_batch.actions != actions:
                raise SellerStoreError("persisted seller child actions differ from the exact current phase")
            batch = existing_batch
        else:
            batch = self._unused_batch(expires_at, actions, now=now)
        reservation = sum(self._reservation(action) for action in actions)
        self.store.prepare_child(
            self.session_id,
            decision_id,
            batch,
            phase=phase,
            reservation_copper=reservation,
            now=now,
        )
        self.backend.set_deadline(expires_at)
        result = run_seller_child(
            batch,
            runner=self.runner,
            seller_store=self.store,
            session_id=self.session_id,
            decision_id=decision_id,
            phase=phase,
            backend=self.backend,
        )
        child_state = str(result.get("state", "unknown"))
        self._last_child_state[decision_id] = child_state
        for item_key, decision_data in self._last_decisions.items():
            if decision_data.get("decisionId") == decision_id:
                self._last_child_state[item_key] = child_state
        self.progress(
            {
                "event": "seller_child_finished",
                "batchId": result.get("batch_id"),
                "decisionId": decision_id,
                "phase": phase,
                "state": child_state,
            }
        )
        return result

    def _unused_batch(self, expires_at: int, actions: tuple[Action, ...], *, now: int) -> Batch:
        """Choose an unused canonical ledger ID without extending any evidence deadline."""
        candidate_expiry = expires_at
        while candidate_expiry > now:
            candidate = Batch(self.policy.house, self.policy.character, candidate_expiry, actions)
            try:
                self.ledger.status(candidate.batch_id)
            except KeyError:
                return candidate
            candidate_expiry -= 1
        raise SellerStoreError("seller child has no unused batch identity within its evidence deadline")

    def _advance_recycle(self, item: SellerItemPolicy, anchor: str | None) -> str:
        if anchor is None:
            raise SellerStoreError("recycle progress has no exact decision anchor")
        progress = self.store.item_progress(self.session_id, item.item_key)
        if progress.managed_active_quantity:
            return "checked"
        if progress.unresolved_quantity and not progress.returning_quantity and not progress.bag_quantity:
            return "paused"
        if progress.returning_quantity == 0:
            if self._at_mailbox:
                prior_route = self.store.child_status(self.session_id, anchor, "travel_to_ah")
                resume_location = self.store.status(
                    self.session_id, now=int(self.io.wall_time())
                ).resume_location
                if prior_route in {"succeeded", "failed", "expired"} and resume_location == "mailbox":
                    if not self._resume_epoch_usable:
                        self._pause_reason = (
                            "Auction House route needs a new explicit mailbox resume after route failure"
                        )
                        return "paused"
                    recovery_phase, actions = self.store.recovery_to_ah(
                        self.session_id,
                        anchor,
                        now=int(self.io.wall_time()),
                    )
                    route = self._run_phase(anchor, recovery_phase, actions_override=actions)
                elif prior_route in {"succeeded", "failed", "expired"}:
                    self._pause_reason = (
                        "Auction House route needs explicit resume from the measured mailbox location"
                    )
                    return "paused"
                else:
                    route = self._run_phase(anchor, "travel_to_ah")
                if route is None or route.get("state") != "succeeded":
                    self._resume_epoch_usable = False
                    self._pause_reason = (
                        "Auction House route did not complete; verify the measured location "
                        "and explicitly resume"
                    )
                    return "paused"
                self._at_mailbox = False
                self._mailbox_item_key = None
                return "returned_to_ah"
            status = self.store.status(self.session_id, now=int(self.io.wall_time()))
            if status.resume_location == "ah" and self._resume_epoch_usable:
                self.store.complete_recycle_at_ah(
                    self.session_id,
                    anchor,
                    now=int(self.io.wall_time()),
                )
                self.progress(
                    {
                        "event": "seller_recycle_recovered_at_ah",
                        "itemKey": item.item_key,
                        "decisionId": anchor,
                        "reason": "explicit fresh Auction House resume reconciled the completed return pass",
                    }
                )
                return "returned_to_ah"
            return "checked"

        if not self._at_mailbox:
            prior_route = self.store.child_status(self.session_id, anchor, "travel_to_mail")
            resume_location = self.store.status(self.session_id, now=int(self.io.wall_time())).resume_location
            if prior_route in {"succeeded", "failed", "expired"} and resume_location == "ah":
                if not self._resume_epoch_usable:
                    self._pause_reason = (
                        "mailbox route needs a new explicit Auction House resume after route failure"
                    )
                    return "paused"
                recovery_phase, actions = self.store.recovery_to_mail(
                    self.session_id,
                    anchor,
                    now=int(self.io.wall_time()),
                )
                travel = self._run_phase(
                    anchor,
                    recovery_phase,
                    actions_override=actions,
                )
            elif prior_route in {"succeeded", "failed", "expired"}:
                self._pause_reason = (
                    "mailbox route needs explicit resume from the measured Auction House location"
                )
                return "paused"
            else:
                travel = self._run_phase(anchor, "travel_to_mail")
            if travel is None or travel.get("state") != "succeeded":
                self._resume_epoch_usable = False
                self._pause_reason = (
                    "mailbox route did not complete; verify the measured location and explicitly resume"
                )
                return "paused"
            self._at_mailbox = True
            self._mailbox_item_key = item.item_key

        self.backend.set_deadline(self.policy.expires_at)
        return_observation = self.native.returns(item.item_key)
        outstanding = self.store.item_progress(self.session_id, item.item_key).returning_quantity
        candidates = tuple(
            ReturnCandidate(
                row.item_key,
                row.quantity,
                row.mail_fingerprint,
                return_observation.request_id,
                return_observation.boot,
                return_observation.sequence,
                return_observation.captured_at,
                return_observation.complete,
                row.eligible
                and row.item_key == item.item_key
                and row.quantity is not None
                and row.quantity <= outstanding,
                (
                    row.reason
                    if not row.eligible
                    else "return attachment exceeds outstanding admitted stock"
                    if row.quantity is None or row.quantity > outstanding
                    else "return attachment does not match the exact policy commodity"
                    if row.item_key != item.item_key
                    else row.reason
                ),
            )
            for row in return_observation.facts
        )
        self.store.record_returns(self.session_id, anchor, candidates, now=int(self.io.wall_time()))
        eligible = sorted(
            (candidate for candidate in candidates if candidate.eligible),
            key=lambda candidate: candidate.mail_fingerprint,
        )
        for candidate in eligible:
            result = self._run_phase(
                anchor,
                f"collect_return:{candidate.mail_fingerprint}",
                evidence_expires_at=candidate.observed_at + SELLER_DECISION_MAX_AGE_SECONDS,
            )
            if result is None or result.get("state") != "succeeded":
                detail = self._receipt_detail(result)
                if detail == "insufficient_return_bag_slots" and result is not None:
                    if result.get("state") == "uncertain" or self.ledger.has_uncertain_effect():
                        return "checked"
                    self._pause_reason = (
                        "return collection paused: no free bag slots for the exact returned attachment; "
                        "free space, re-observe the mailbox, then resume with the same policy"
                    )
                    self.progress(
                        {
                            "event": "seller_return_paused",
                            "itemKey": item.item_key,
                            "mailFingerprint": candidate.mail_fingerprint,
                            "reason": self._pause_reason,
                        }
                    )
                    return "paused"
                return "checked"
            # The inbox can reindex after each attachment; always obtain new facts.
            return self._advance_recycle(item, anchor)

        progress = self.store.item_progress(self.session_id, item.item_key)
        if progress.returning_quantity:
            self._pause_reason = (
                "recycling paused: no eligible exact commodity return attachment is currently available"
            )
            return "paused"
        route = self._run_phase(anchor, "travel_to_ah")
        if route is None or route.get("state") != "succeeded":
            return "checked"
        self._at_mailbox = False
        self._mailbox_item_key = None
        return "returned_to_ah"

    @staticmethod
    def _receipt_detail(result: dict[str, object] | None) -> str | None:
        if result is None:
            return None
        steps = result.get("steps")
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict):
                continue
            receipt = step.get("receipt")
            if isinstance(receipt, dict) and type(receipt.get("detail")) is str:
                return receipt["detail"]
        actions = result.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict):
                    continue
                if action.get("reason") == (
                    "native preflight refused operation: insufficient_return_bag_slots"
                ):
                    return "insufficient_return_bag_slots"
        return None

    @staticmethod
    def _reservation(action: Action) -> int:
        if isinstance(action, PostCommodity):
            return action.max_deposit_copper
        if isinstance(action, CancelAuction):
            return action.max_cancel_cost_copper
        return 0

    def _result(self, status: SellerSessionStatus, *, reason: str | None = None) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for item in self.policy.items:
            progress = asdict(self.store.item_progress(self.session_id, item.item_key))
            decision = self._last_decisions.get(item.item_key)
            progress["lastSuccessfulObservation"] = (
                {key: decision.get(key) for key in ("observedAt", "requestId", "sequence", "evidenceDigest")}
                if decision is not None
                else None
            )
            progress["lastDecision"] = decision
            progress["lastChildState"] = self._last_child_state.get(item.item_key)
            items.append(progress)
        result: dict[str, object] = {
            "controllerStopped": True,
            "driverState": (
                "completed"
                if status.state == "completed"
                else "paused"
                if status.state == "active" and reason is not None
                else "stopped"
            ),
            "items": items,
            "session": asdict(status),
            "liveQualification": "unverified",
        }
        if reason is not None:
            result["reason"] = reason
        return result
