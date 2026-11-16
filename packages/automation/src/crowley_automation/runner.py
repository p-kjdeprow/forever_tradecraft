"""A single bounded runner with explicit authorization and session arm gates.

The backend is deliberately only a protocol: this foundation has no native
adapter. Staging may send bounded preparatory GUI input, so the runner checks
session and operator gates before staging and requires exact readiness readback.
Dispatch is called only after an immutable SQLite dispatch marker has committed
and all gates have been rechecked.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .ledger import BatchStatus, Ledger, ReadyStep, UncertainEffectInterlock
from .model import (
    MAX_INTEGER,
    Action,
    Batch,
    CancelAuction,
    CollectMail,
    CollectReturn,
    Interact,
    MoveRoute,
    PostCommodity,
    PostItem,
    action_object,
    canonical_json,
)

MAX_OBSERVATION_AGE_SECONDS = 3


class AdmissionDenied(RuntimeError):
    """Current session or operator conditions do not permit a dispatch."""


def _validate_dispatch_result(value: object) -> dict[str, object]:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ValueError("structured dispatch result must be an exact object with a kind")
    kind = value["kind"]
    if kind == "post_commodity":
        fields = {"kind", "auctionId", "itemKey", "quantity", "unitPriceCopper", "depositCopper"}
        if set(value) != fields:
            raise ValueError("post_commodity result fields are not exact")
        if type(value["depositCopper"]) is not int and value["depositCopper"] is not None:
            raise ValueError("depositCopper must be an exact integer or unknown")
        PostCommodity(
            value["itemKey"],  # type: ignore[arg-type]
            value["quantity"],  # type: ignore[arg-type]
            value["unitPriceCopper"],  # type: ignore[arg-type]
            12,
            0,
        )
        if type(value["auctionId"]) is not int or value["auctionId"] < 1:
            raise ValueError("auctionId must be a positive exact integer")
        if value["depositCopper"] is not None and (
            type(value["depositCopper"]) is not int or value["depositCopper"] < 0
        ):
            raise ValueError("depositCopper must be non-negative")
    elif kind == "post_item":
        fields = {"kind", "auctionId", "itemKey", "quantity", "unitPriceCopper", "depositCopper"}
        if set(value) != fields:
            raise ValueError("post_item result fields are not exact")
        PostItem(
            value["itemKey"],  # type: ignore[arg-type]
            value["quantity"],  # type: ignore[arg-type]
            value["unitPriceCopper"],  # type: ignore[arg-type]
            12,
            0,
        )
        if type(value["auctionId"]) is not int or value["auctionId"] < 1:
            raise ValueError("auctionId must be a positive exact integer")
        if value["depositCopper"] is not None and (
            type(value["depositCopper"]) is not int or value["depositCopper"] < 0
        ):
            raise ValueError("depositCopper must be non-negative")
    elif kind == "cancel_auction":
        fields = {"kind", "auctionId", "itemKey", "remainingQuantity", "cancelCostCopper"}
        if set(value) != fields:
            raise ValueError("cancel_auction result fields are not exact")
        if (
            type(value["auctionId"]) is not int
            or value["auctionId"] < 1
            or type(value["remainingQuantity"]) is not int
            or not 0 <= value["remainingQuantity"] <= MAX_INTEGER
            or (
                value["cancelCostCopper"] is not None
                and (
                    type(value["cancelCostCopper"]) is not int
                    or not 0 <= value["cancelCostCopper"] <= MAX_INTEGER
                )
            )
        ):
            raise ValueError("cancel result quantities and costs must be exact non-negative integers")
        try:
            from .model import validate_item_key

            validate_item_key(value["itemKey"])
        except (TypeError, ValueError) as exc:
            raise ValueError("cancel result itemKey is invalid") from exc
    elif kind == "collect_return":
        fields = {"kind", "mailFingerprint", "itemKey", "quantity"}
        if set(value) != fields:
            raise ValueError("collect_return result fields are not exact")
        action = CollectReturn(
            value["itemKey"],  # type: ignore[arg-type]
            value["quantity"],  # type: ignore[arg-type]
            value["mailFingerprint"],  # type: ignore[arg-type]
        )
        if action.quantity > MAX_INTEGER:
            raise ValueError("return quantity exceeds the safe-integer limit")
    else:
        raise ValueError("unsupported structured dispatch result kind")
    return value


def dispatch_result_matches_action(result: dict[str, object] | None, action: Action) -> bool:
    if result is None:
        return False
    if isinstance(action, PostCommodity):
        return (
            result.get("kind") == "post_commodity"
            and result.get("itemKey") == action.item_key
            and result.get("quantity") == action.quantity
            and result.get("unitPriceCopper") == action.unit_price_copper
        )
    if isinstance(action, PostItem):
        return (
            result.get("kind") == "post_item"
            and result.get("itemKey") == action.item_key
            and result.get("quantity") == action.quantity
            and result.get("unitPriceCopper") == action.unit_price_copper
        )
    if isinstance(action, CancelAuction):
        return result.get("kind") == "cancel_auction" and result.get("auctionId") == action.auction_id
    if isinstance(action, CollectReturn):
        return (
            result.get("kind") == "collect_return"
            and result.get("itemKey") == action.item_key
            and result.get("quantity") == action.quantity
            and result.get("mailFingerprint") == action.mail_fingerprint
        )
    return True


@dataclass(frozen=True, slots=True)
class BatchAuthorization:
    batch_id: str
    house: str
    character: str
    expires_at: int

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in (self.batch_id, self.house, self.character)):
            raise ValueError("batch authorization identity fields must be non-empty text")
        if type(self.expires_at) is not int or not 1 <= self.expires_at <= MAX_INTEGER:
            raise ValueError("authorization expiry must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class SessionArm:
    session_boot_id: str
    house: str
    character: str
    expires_at: int

    def __post_init__(self) -> None:
        identity = (self.session_boot_id, self.house, self.character)
        if any(type(value) is not str or not value for value in identity):
            raise ValueError("session arm identity fields must be non-empty text")
        if type(self.expires_at) is not int or not 1 <= self.expires_at <= MAX_INTEGER:
            raise ValueError("session arm expiry must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class Admission:
    """Fresh session gates, plus an all-or-none staged-operation readback."""

    session_boot_id: str
    house: str
    character: str
    observed_at: int
    human_present: bool
    focused: bool
    gui_lock_owned: bool
    client_ah_owned: bool
    staged_batch_id: str | None = None
    staged_ordinal: int | None = None
    staged_step_index: int | None = None
    staged_action_digest: str | None = None
    ready: bool | None = None
    receipt_sequence: int | None = None
    client_build: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.session_boot_id) is not str
            or not self.session_boot_id
            or len(self.session_boot_id) > 128
        ):
            raise ValueError("session_boot_id must be a bounded non-empty string")
        if type(self.house) is not str or type(self.character) is not str:
            raise ValueError("admission identity must be text")
        if self.client_build is not None and (
            type(self.client_build) is not str or not self.client_build or len(self.client_build) > 64
        ):
            raise ValueError("client_build must be bounded non-empty text when present")
        if type(self.observed_at) is not int or not 0 <= self.observed_at <= MAX_INTEGER:
            raise ValueError("observed_at must be a non-negative exact integer")
        if any(
            type(value) is not bool
            for value in (self.human_present, self.focused, self.gui_lock_owned, self.client_ah_owned)
        ):
            raise ValueError("admission gates must be exact booleans")
        if self.receipt_sequence is not None and (
            type(self.receipt_sequence) is not int or not 0 <= self.receipt_sequence <= MAX_INTEGER
        ):
            raise ValueError("receipt sequence must be a non-negative exact integer")
        staged_values = (
            self.staged_batch_id,
            self.staged_ordinal,
            self.staged_step_index,
            self.staged_action_digest,
            self.ready,
        )
        if all(value is None for value in staged_values):
            return
        if (
            type(self.staged_batch_id) is not str
            or type(self.staged_ordinal) is not int
            or type(self.staged_step_index) is not int
            or type(self.staged_action_digest) is not str
            or type(self.ready) is not bool
            or self.staged_ordinal < 0
            or self.staged_step_index < 0
        ):
            raise ValueError("staging readback must provide exact identity and readiness fields")


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    batch_id: str
    ordinal: int
    step_index: int
    action_digest: str
    session_boot_id: str
    callback_delivered: bool | None
    postcondition: bool | None
    action_complete: bool | None
    detail: str = ""
    result: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (self.batch_id, self.action_digest, self.session_boot_id)
        ):
            raise ValueError("receipt identity fields must be non-empty text")
        if len(self.batch_id) != 64 or any(char not in "0123456789abcdef" for char in self.batch_id):
            raise ValueError("receipt batch_id must be a full lowercase SHA256 digest")
        if len(self.action_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.action_digest
        ):
            raise ValueError("receipt action_digest must be a full lowercase SHA256 digest")
        if len(self.session_boot_id) > 128:
            raise ValueError("receipt session_boot_id must be bounded text")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("receipt ordinal must be a non-negative exact integer")
        if type(self.step_index) is not int or self.step_index < 0:
            raise ValueError("receipt step_index must be a non-negative exact integer")
        if any(
            type(value) not in (bool, type(None))
            for value in (self.callback_delivered, self.postcondition, self.action_complete)
        ):
            raise ValueError("receipt evidence must be boolean or unknown")
        if type(self.detail) is not str or len(self.detail) > 240:
            raise ValueError("receipt detail must be bounded text")
        if self.result is not None:
            _validate_dispatch_result(self.result)

    def object(self) -> dict[str, object]:
        return {
            "action_digest": self.action_digest,
            "batch_id": self.batch_id,
            "callback_delivered": self.callback_delivered,
            "detail": self.detail,
            "ordinal": self.ordinal,
            "step_index": self.step_index,
            "postcondition": self.postcondition,
            "action_complete": self.action_complete,
            "session_boot_id": self.session_boot_id,
            "result": self.result,
        }


class Backend(Protocol):
    """One concrete adapter boundary; no adapter is shipped in this package yet."""

    def stage(
        self,
        action: Action,
        *,
        batch_id: str,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        """Prepare one bounded primitive without performing its protected action."""
        ...

    def observe(self) -> Admission:
        """Return fresh session gates and exact readiness identity after staging."""
        ...

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
        """Send one primitive after an adapter-side device-gate check at the actual press."""
        ...


class SellerAuthorizationStore(Protocol):
    def validate_child(
        self,
        session_id: str,
        decision_id: str,
        batch: Batch,
        admission: Admission | None,
        *,
        phase: str = "decision",
        now: int | None = None,
    ) -> None: ...

    def release_before_dispatch(
        self,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        *,
        now: int,
    ) -> None: ...

    def apply_terminal_step(
        self,
        db: sqlite3.Connection,
        *,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        ordinal: int,
        action: Action,
        state: Literal["succeeded", "failed", "uncertain"],
        receipt: dict[str, object] | None,
        reason: str | None,
        now: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _SellerRunContext:
    store: SellerAuthorizationStore
    session_id: str
    decision_id: str
    phase: str


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    interaction_targets: frozenset[str] = frozenset()
    route_step_limits: tuple[tuple[str, int], ...] = ()
    max_observation_age_seconds: int = MAX_OBSERVATION_AGE_SECONDS

    def __post_init__(self) -> None:
        if (
            type(self.max_observation_age_seconds) is not int
            or not 1 <= self.max_observation_age_seconds <= 10
        ):
            raise ValueError("max observation age must be 1..10 seconds")
        if type(self.interaction_targets) is not frozenset or type(self.route_step_limits) is not tuple:
            raise ValueError("configured targets and route limits must be immutable")
        for target_id in self.interaction_targets:
            Interact(target_id)
        route_ids = [route_id for route_id, _ in self.route_step_limits]
        if route_ids != sorted(set(route_ids)):
            raise ValueError("route step limits must be sorted by unique route ID")
        for route_id, limit in self.route_step_limits:
            MoveRoute(route_id)
            if type(limit) is not int or not 1 <= limit <= 64:
                raise ValueError("configured route step limits must be 1..64")


class AutomationRunner:
    def __init__(
        self,
        ledger: Ledger,
        config: RunnerConfig | None = None,
        clock=time.time,
    ) -> None:
        self.ledger = ledger
        self.config = RunnerConfig() if config is None else config
        self.clock = clock

    def dry_run(self, batch: Batch) -> dict[str, object]:
        """Describe a batch without touching the backend or durable ledger."""
        return {
            "batch_id": batch.batch_id,
            "dry_run": True,
            "actions": [action_object(action) for action in batch.actions],
        }

    def preflight(
        self,
        batch: Batch,
        *,
        authorization: BatchAuthorization,
        session_arm: SessionArm,
        confirmed_batch_id: str,
        backend: Backend,
    ) -> dict[str, object]:
        """Stage each immutable action for native readiness without dispatching it."""
        self._check_authorization(batch, authorization, session_arm, confirmed_batch_id, int(self.clock()))
        result: list[dict[str, object]] = []
        may_have_staged = False
        with self.ledger.runner_lock():
            try:
                self.ledger.recover_interrupted(now=int(self.clock()))
                if self.ledger.stop_requested():
                    raise AdmissionDenied("durable stop is active")
                if self.ledger.has_uncertain_effect():
                    raise UncertainEffectInterlock("an unresolved effect interlocks all new action batches")
                for ordinal, action in enumerate(batch.actions):
                    now = int(self.clock())
                    self._check_authorization(batch, authorization, session_arm, confirmed_batch_id, now)
                    self._check_configured_target(action)
                    deadline = min(batch.expires_at, authorization.expires_at, session_arm.expires_at)
                    set_deadline = getattr(backend, "set_deadline", None)
                    if callable(set_deadline):
                        set_deadline(deadline)
                    observed = backend.observe()
                    self._check_before_stage(batch, session_arm, observed, now)
                    digest = hashlib.sha256(canonical_json(action_object(action)).encode("utf-8")).hexdigest()
                    may_have_staged = True
                    backend.stage(
                        action, batch_id=batch.batch_id, ordinal=ordinal, step_index=0, action_digest=digest
                    )
                    ready = backend.observe()
                    self._check_before_dispatch(
                        batch, session_arm, ready, int(self.clock()), action, ordinal, 0, digest
                    )
                    result.append(
                        {
                            "ordinal": ordinal,
                            "action": action_object(action),
                            "actionDigest": digest,
                            "nativePreflight": "ready",
                            "boot": ready.session_boot_id,
                            "goldCopper": getattr(backend, "preflight_gold", None),
                        }
                    )
                return {"batch_id": batch.batch_id, "dry_run": True, "preflight": result}
            finally:
                clear_stage = getattr(backend, "clear_stage", None)
                if may_have_staged and callable(clear_stage):
                    try:
                        clear_stage()
                    except Exception:
                        pass

    def run(
        self,
        batch: Batch,
        *,
        authorization: BatchAuthorization,
        session_arm: SessionArm,
        confirmed_batch_id: str,
        backend: Backend,
    ) -> BatchStatus:
        self.ledger.prepare(batch, now=int(self.clock()))
        with self.ledger.runner_lock():
            self.ledger.recover_interrupted(now=int(self.clock()))
            return self._run_locked(
                batch,
                authorization=authorization,
                session_arm=session_arm,
                confirmed_batch_id=confirmed_batch_id,
                backend=backend,
            )

    def run_seller_child(
        self,
        batch: Batch,
        *,
        seller_store: SellerAuthorizationStore,
        session_id: str,
        decision_id: str,
        phase: str = "decision",
        backend: Backend,
    ) -> BatchStatus:
        """Run one durable policy-derived child without manufacturing manual auth or an arm."""
        with self.ledger.runner_lock():
            self.ledger.recover_interrupted(now=int(self.clock()))
            status = self.ledger.status(batch.batch_id)
            if status["state"] in ("succeeded", "failed", "uncertain"):
                return status
            if self.ledger.has_uncertain_effect():
                raise UncertainEffectInterlock("an unresolved effect interlocks all new action batches")
            if status["state"] == "expired":
                seller_store.release_before_dispatch(
                    session_id, decision_id, phase, batch, now=int(self.clock())
                )
                return status
            if self.ledger.stop_requested():
                raise AdmissionDenied("durable stop is active")
            now = int(self.clock())
            if now >= batch.expires_at:
                self.ledger.mark_expired(batch.batch_id, now=now)
                seller_store.release_before_dispatch(session_id, decision_id, phase, batch, now=now)
                return self.ledger.status(batch.batch_id)
            seller_store.validate_child(
                session_id, decision_id, batch, None, phase=phase, now=int(self.clock())
            )
            return self._run_locked(
                batch,
                authorization=None,
                session_arm=None,
                confirmed_batch_id=None,
                backend=backend,
                seller_context=_SellerRunContext(seller_store, session_id, decision_id, phase),
            )

    def _run_locked(
        self,
        batch: Batch,
        *,
        authorization: BatchAuthorization | None,
        session_arm: SessionArm | None,
        confirmed_batch_id: str | None,
        backend: Backend,
        seller_context: _SellerRunContext | None = None,
    ) -> BatchStatus:
        """Dispatch each not-yet-sent action once, stopping on the first ambiguity."""
        now = int(self.clock())
        status = self.ledger.status(batch.batch_id)
        if status["state"] in ("succeeded", "failed", "uncertain", "expired"):
            return status
        if now >= batch.expires_at:
            self.ledger.mark_expired(batch.batch_id, now=now)
            if seller_context is not None:
                seller_context.store.release_before_dispatch(
                    seller_context.session_id,
                    seller_context.decision_id,
                    seller_context.phase,
                    batch,
                    now=now,
                )
            return self.ledger.status(batch.batch_id)
        if self.ledger.has_uncertain_effect():
            raise UncertainEffectInterlock("an unresolved effect interlocks all new action batches")
        self._check_run_context(
            batch, authorization, session_arm, confirmed_batch_id, seller_context, None, now
        )

        while True:
            status = self.ledger.status(batch.batch_id)
            if status["state"] in ("succeeded", "failed", "uncertain", "expired"):
                return status
            self._check_run_context(
                batch, authorization, session_arm, confirmed_batch_id, seller_context, None, int(self.clock())
            )
            ready = self._next_ready_step(status)
            if ready is None:
                return status
            ordinal = ready["ordinal"]
            step_index = ready["step_index"]
            action = batch.actions[ordinal]
            self._check_configured_target(action)
            digest = hashlib.sha256(canonical_json(action_object(action)).encode("utf-8")).hexdigest()
            observed = backend.observe()
            pre_stage_now = int(self.clock())
            self._check_run_context(
                batch, authorization, session_arm, confirmed_batch_id, seller_context, observed, pre_stage_now
            )
            if seller_context is None:
                assert session_arm is not None
                self._check_before_stage(batch, session_arm, observed, pre_stage_now)
            else:
                self._check_seller_before_stage(batch, observed, pre_stage_now)
            set_deadline = getattr(backend, "set_deadline", None)
            if callable(set_deadline):
                set_deadline(self._execution_deadline(batch, authorization, session_arm))
            try:
                backend.stage(
                    action,
                    batch_id=batch.batch_id,
                    ordinal=ordinal,
                    step_index=step_index,
                    action_digest=digest,
                )
            except Exception as exc:
                clear_stage = getattr(backend, "clear_stage", None)
                if callable(clear_stage):
                    try:
                        clear_stage()
                    except Exception:
                        pass
                refusal_code = self._native_stage_refusal_code(exc)
                reason = (
                    f"native preflight refused operation: {refusal_code}"
                    if refusal_code is not None
                    else f"stage failed: {type(exc).__name__}"
                )
                self.ledger.fail_before_dispatch(batch.batch_id, ordinal, reason, now=int(self.clock()))
                if seller_context is not None:
                    seller_context.store.release_before_dispatch(
                        seller_context.session_id,
                        seller_context.decision_id,
                        seller_context.phase,
                        batch,
                        now=int(self.clock()),
                    )
                return self.ledger.status(batch.batch_id)
            try:
                observed = backend.observe()
                observed_now = int(self.clock())
                self._check_run_context(
                    batch,
                    authorization,
                    session_arm,
                    confirmed_batch_id,
                    seller_context,
                    observed,
                    observed_now,
                )
                if seller_context is None:
                    assert session_arm is not None
                    self._check_before_dispatch(
                        batch, session_arm, observed, observed_now, action, ordinal, step_index, digest
                    )
                else:
                    self._check_seller_before_dispatch(
                        batch, observed, observed_now, action, ordinal, step_index, digest
                    )
            except Exception:
                clear_stage = getattr(backend, "clear_stage", None)
                if callable(clear_stage):
                    try:
                        clear_stage()
                    except Exception:
                        pass
                raise
            set_deadline = getattr(backend, "set_deadline", None)
            if callable(set_deadline):
                set_deadline(self._execution_deadline(batch, authorization, session_arm))
            self.ledger.claim_dispatch(
                batch.batch_id,
                ordinal,
                step_index,
                now=observed_now,
                session_boot_id=observed.session_boot_id,
                receipt_baseline=observed.receipt_sequence,
            )
            try:
                current_admission = backend.observe()
                dispatch_now = int(self.clock())
                self._check_run_context(
                    batch,
                    authorization,
                    session_arm,
                    confirmed_batch_id,
                    seller_context,
                    current_admission,
                    dispatch_now,
                )
                if seller_context is None:
                    assert session_arm is not None
                    self._check_before_dispatch(
                        batch,
                        session_arm,
                        current_admission,
                        dispatch_now,
                        action,
                        ordinal,
                        step_index,
                        digest,
                    )
                else:
                    self._check_seller_before_dispatch(
                        batch,
                        current_admission,
                        dispatch_now,
                        action,
                        ordinal,
                        step_index,
                        digest,
                    )
            except Exception as exc:
                clear_stage = getattr(backend, "clear_stage", None)
                if callable(clear_stage):
                    try:
                        clear_stage()
                    except Exception:
                        pass
                self.ledger.finish_step(
                    batch.batch_id,
                    ordinal,
                    step_index,
                    "failed",
                    now=int(self.clock()),
                    callback_delivered=None,
                    postcondition=None,
                    action_complete=None,
                    receipt=None,
                    reason=f"pre-dispatch revalidation failed: {type(exc).__name__}",
                    transaction_hook=self._seller_hook(
                        seller_context,
                        batch,
                        ordinal,
                        action,
                        "failed",
                        None,
                        f"pre-dispatch revalidation failed: {type(exc).__name__}",
                    ),
                )
                return self.ledger.status(batch.batch_id)
            try:
                receipt = backend.dispatch(
                    action,
                    batch_id=batch.batch_id,
                    ordinal=ordinal,
                    step_index=step_index,
                    action_digest=digest,
                    admission=current_admission,
                )
            except Exception as exc:
                self.ledger.finish_step(
                    batch.batch_id,
                    ordinal,
                    step_index,
                    "uncertain",
                    now=int(self.clock()),
                    callback_delivered=None,
                    postcondition=None,
                    action_complete=None,
                    receipt=None,
                    reason=f"dispatch raised {type(exc).__name__}; effect is uncertain",
                    transaction_hook=self._seller_hook(
                        seller_context,
                        batch,
                        ordinal,
                        action,
                        "uncertain",
                        None,
                        f"dispatch raised {type(exc).__name__}; effect is uncertain",
                    ),
                )
                return self.ledger.status(batch.batch_id)
            result_state, continue_action = self._receipt_state(
                receipt,
                batch.batch_id,
                ordinal,
                step_index,
                digest,
                current_admission.session_boot_id,
                action,
                self._step_limit(action),
                require_structured_result=seller_context is not None,
            )
            if result_state is None:
                receipt_data = receipt.object() if isinstance(receipt, DispatchReceipt) else None
                callback = receipt.callback_delivered if isinstance(receipt, DispatchReceipt) else None
                postcondition = receipt.postcondition if isinstance(receipt, DispatchReceipt) else None
                complete = receipt.action_complete if isinstance(receipt, DispatchReceipt) else None
                self.ledger.finish_step(
                    batch.batch_id,
                    ordinal,
                    step_index,
                    "uncertain",
                    now=int(self.clock()),
                    callback_delivered=callback,
                    postcondition=postcondition,
                    action_complete=complete,
                    receipt=receipt_data,
                    reason="receipt is missing, contradictory, or not causally bound to this dispatch",
                    transaction_hook=self._seller_hook(
                        seller_context,
                        batch,
                        ordinal,
                        action,
                        "uncertain",
                        receipt.object() if isinstance(receipt, DispatchReceipt) else None,
                        "receipt is missing, contradictory, or not causally bound to this dispatch",
                    ),
                )
                return self.ledger.status(batch.batch_id)
            self.ledger.finish_step(
                batch.batch_id,
                ordinal,
                step_index,
                result_state,
                now=int(self.clock()),
                callback_delivered=receipt.callback_delivered,
                postcondition=receipt.postcondition,
                action_complete=receipt.action_complete,
                receipt=receipt.object(),
                continue_action=continue_action,
                reason=None if result_state == "succeeded" else "operation did not reach verified completion",
                transaction_hook=self._seller_hook(
                    seller_context,
                    batch,
                    ordinal,
                    action,
                    result_state,
                    receipt.object(),
                    None if result_state == "succeeded" else "operation did not reach verified completion",
                ),
            )
            if result_state == "succeeded" and not continue_action:
                completed = self.ledger.status(batch.batch_id)
                if completed["state"] == "succeeded":
                    finish_batch = getattr(backend, "finish_batch", None)
                    if callable(finish_batch):
                        finish_batch()
                    return completed

    @staticmethod
    def _next_ready_step(status: BatchStatus) -> ReadyStep | None:
        row = next((item for item in status["actions"] if item["state"] == "ready"), None)
        if row is None:
            return None
        return {"ordinal": row["ordinal"], "step_index": row["next_step"]}

    @staticmethod
    def _native_stage_refusal_code(exc: Exception) -> str | None:
        from .backend import DesktopBackendError

        if type(exc) is not DesktopBackendError:
            return None
        code = getattr(exc, "code", None)
        return code if code == "insufficient_return_bag_slots" else None

    def _check_authorization(
        self,
        batch: Batch,
        authorization: BatchAuthorization,
        session_arm: SessionArm,
        confirmed_batch_id: str,
        now: int,
    ) -> None:
        if (
            type(confirmed_batch_id) is not str
            or confirmed_batch_id != batch.batch_id
            or authorization.batch_id != batch.batch_id
        ):
            raise AdmissionDenied("explicit confirmation and authorization must name this exact batch ID")
        if authorization.house != batch.house or authorization.character != batch.character:
            raise AdmissionDenied("authorization identity does not match batch identity")
        if type(authorization.expires_at) is not int or authorization.expires_at <= now:
            raise AdmissionDenied("batch authorization is expired or invalid")
        if session_arm.house != batch.house or session_arm.character != batch.character:
            raise AdmissionDenied("session arm identity does not match batch identity")
        if type(session_arm.expires_at) is not int or session_arm.expires_at <= now:
            raise AdmissionDenied("session arm is expired or invalid")
        if self.ledger.stop_requested():
            raise AdmissionDenied("durable stop is active")

    def _check_run_context(
        self,
        batch: Batch,
        authorization: BatchAuthorization | None,
        session_arm: SessionArm | None,
        confirmed_batch_id: str | None,
        seller_context: _SellerRunContext | None,
        observation: Admission | None,
        now: int,
    ) -> None:
        if seller_context is not None:
            seller_context.store.validate_child(
                seller_context.session_id,
                seller_context.decision_id,
                batch,
                observation,
                phase=seller_context.phase,
                now=now,
            )
            return
        if authorization is None or session_arm is None or confirmed_batch_id is None:
            raise AdmissionDenied("manual batch authorization is missing")
        self._check_authorization(batch, authorization, session_arm, confirmed_batch_id, now)

    @staticmethod
    def _execution_deadline(
        batch: Batch,
        authorization: BatchAuthorization | None,
        session_arm: SessionArm | None,
    ) -> int:
        if authorization is None or session_arm is None:
            return batch.expires_at
        return min(batch.expires_at, authorization.expires_at, session_arm.expires_at)

    def _check_seller_before_stage(self, batch: Batch, observation: Admission, now: int) -> None:
        if now >= batch.expires_at:
            self.ledger.mark_expired(batch.batch_id, now=now)
            raise AdmissionDenied("seller child expired before staging")
        if (
            observation.observed_at > now
            or now - observation.observed_at > self.config.max_observation_age_seconds
        ):
            raise AdmissionDenied("session observation is stale")
        if observation.human_present:
            raise AdmissionDenied("human presence blocks dispatch")
        if not observation.focused:
            raise AdmissionDenied("game window is not focused")
        if not observation.gui_lock_owned:
            raise AdmissionDenied("GUI ownership lock is not held")

    def _check_seller_before_dispatch(
        self,
        batch: Batch,
        observation: Admission,
        now: int,
        action: Action,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        self._check_seller_before_stage(batch, observation, now)
        if (
            observation.staged_batch_id != batch.batch_id
            or observation.staged_ordinal != ordinal
            or observation.staged_step_index != step_index
            or observation.staged_action_digest != action_digest
            or observation.ready is not True
        ):
            raise AdmissionDenied("staged operation identity or readiness readback does not match")
        if isinstance(action, (PostCommodity, PostItem, CancelAuction)) and not observation.client_ah_owned:
            raise AdmissionDenied("client-side auction-house ownership is not held")

    def _seller_hook(
        self,
        context: _SellerRunContext | None,
        batch: Batch,
        ordinal: int,
        action: Action,
        state: Literal["succeeded", "failed", "uncertain"] | None,
        receipt: dict[str, object] | None,
        reason: str | None,
    ) -> Callable[[sqlite3.Connection], None] | None:
        if context is None or state is None:
            return None

        def apply(db: sqlite3.Connection) -> None:
            context.store.apply_terminal_step(
                db,
                session_id=context.session_id,
                decision_id=context.decision_id,
                phase=context.phase,
                batch=batch,
                ordinal=ordinal,
                action=action,
                state=state,
                receipt=receipt,
                reason=reason,
                now=int(self.clock()),
            )

        return apply

    def _check_session_and_gui(
        self,
        batch: Batch,
        arm: SessionArm,
        observation: Admission,
        now: int,
    ) -> None:
        if now >= batch.expires_at:
            self.ledger.mark_expired(batch.batch_id, now=now)
            raise AdmissionDenied("batch expired before dispatch")
        if now >= arm.expires_at:
            raise AdmissionDenied("session arm expired before dispatch")
        if (
            observation.observed_at > now
            or now - observation.observed_at > self.config.max_observation_age_seconds
        ):
            raise AdmissionDenied("session observation is stale")
        if (
            observation.session_boot_id != arm.session_boot_id
            or observation.house != batch.house
            or observation.character != batch.character
        ):
            raise AdmissionDenied("session boot or character identity changed")
        if observation.human_present:
            raise AdmissionDenied("human presence blocks dispatch")
        if not observation.focused:
            raise AdmissionDenied("game window is not focused")
        if not observation.gui_lock_owned:
            raise AdmissionDenied("GUI ownership lock is not held")

    def _check_before_stage(
        self,
        batch: Batch,
        arm: SessionArm,
        observation: Admission,
        now: int,
    ) -> None:
        """Check human/session/focus/GUI gates before stage can send any GUI input."""
        self._check_session_and_gui(batch, arm, observation, now)

    def _check_before_dispatch(
        self,
        batch: Batch,
        arm: SessionArm,
        observation: Admission,
        now: int,
        action: Action,
        ordinal: int,
        step_index: int,
        action_digest: str,
    ) -> None:
        """Recheck admission after staging and require AH ownership for market mutations."""
        self._check_session_and_gui(batch, arm, observation, now)
        if (
            observation.staged_batch_id != batch.batch_id
            or observation.staged_ordinal != ordinal
            or observation.staged_step_index != step_index
            or observation.staged_action_digest != action_digest
            or observation.ready is not True
        ):
            raise AdmissionDenied("staged operation identity or readiness readback does not match")
        if isinstance(action, (PostCommodity, PostItem, CancelAuction)) and not observation.client_ah_owned:
            raise AdmissionDenied("client-side auction-house ownership is not held")

    def _check_configured_target(self, action: Action) -> None:
        if isinstance(action, Interact) and action.target_id not in self.config.interaction_targets:
            raise AdmissionDenied("interaction target is not explicitly configured")
        if isinstance(action, MoveRoute) and action.route_id not in dict(self.config.route_step_limits):
            raise AdmissionDenied("route ID is not explicitly configured")

    def _step_limit(self, action: Action) -> int:
        if isinstance(action, CollectMail):
            return action.message_limit
        if isinstance(action, MoveRoute):
            return dict(self.config.route_step_limits)[action.route_id]
        return 1

    @staticmethod
    def _receipt_state(
        receipt: object,
        batch_id: str,
        ordinal: int,
        step_index: int,
        digest: str,
        session_boot_id: str,
        action: Action,
        step_limit: int,
        *,
        require_structured_result: bool = False,
    ) -> tuple[Literal["succeeded", "failed"] | None, bool]:
        if not isinstance(receipt, DispatchReceipt):
            return None, False
        if (
            receipt.batch_id != batch_id
            or receipt.ordinal != ordinal
            or receipt.step_index != step_index
            or receipt.action_digest != digest
            or receipt.session_boot_id != session_boot_id
        ):
            return None, False
        if receipt.result is not None and not dispatch_result_matches_action(receipt.result, action):
            return None, False
        if receipt.callback_delivered is True and receipt.postcondition is True:
            if (
                require_structured_result
                and isinstance(action, (PostCommodity, CancelAuction, CollectReturn))
                and not dispatch_result_matches_action(receipt.result, action)
            ):
                return None, False
            if receipt.action_complete is True:
                return "succeeded", False
            if receipt.action_complete is False and isinstance(action, (CollectMail, MoveRoute)):
                if step_index + 1 < step_limit:
                    return "succeeded", True
                if isinstance(action, CollectMail):
                    return "succeeded", False
                return "failed", False
            return None, False
        if receipt.callback_delivered is True and receipt.postcondition is False:
            return "failed", False
        if receipt.callback_delivered is False and receipt.postcondition is False:
            return "failed", False
        return None, False
