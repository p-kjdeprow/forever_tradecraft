"""Admission and rate-gated emission of automated button gestures."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .executor import PointerButton
from .pointer_execution import PointerExecutionError, acquire_click_rate_permit


class InputBrokerError(RuntimeError):
    """A button gesture could not be admitted or emitted."""


@runtime_checkable
class RatePermit(Protocol):
    @property
    def is_open(self) -> bool: ...

    @property
    def acquired_monotonic(self) -> float: ...

    def precommit(self, *, deadline_monotonic: float | None = None) -> int: ...

    def record_press(self) -> int: ...

    def close(self) -> None: ...


class RatePermitFactory(Protocol):
    def __call__(
        self,
        *,
        deadline_monotonic: float | None,
        cancellation_check: Callable[[], None] | None,
    ) -> RatePermit: ...


_DEFAULT_GESTURE_DEADLINE_SECONDS = 10.0


class ButtonEmissionClaim:
    """Opaque one-use proof issued only after a gesture rate precommit."""

    __slots__ = (
        "_nonce",
        "_gesture_digest",
        "_button",
        "_action_generation",
        "_lease_generation",
    )

    def __init__(
        self,
        *,
        nonce: object,
        gesture_digest: str,
        button: PointerButton,
        action_generation: int,
        lease_generation: int,
    ) -> None:
        self._nonce = nonce
        self._gesture_digest = gesture_digest
        self._button = button
        self._action_generation = action_generation
        self._lease_generation = lease_generation

    @property
    def gesture_digest(self) -> str:
        return self._gesture_digest

    @property
    def button(self) -> PointerButton:
        return self._button

    @property
    def action_generation(self) -> int:
        return self._action_generation

    @property
    def lease_generation(self) -> int:
        return self._lease_generation


def gesture_digest(emitted_chain: Sequence[str]) -> str:
    """Hash the exact argv tokens emitted for one motion-plus-press chain."""

    if not isinstance(emitted_chain, (tuple, list)) or not emitted_chain:
        raise InputBrokerError("emitted button gesture chain is malformed")
    if any(not isinstance(token, str) or not token for token in emitted_chain):
        raise InputBrokerError("emitted button gesture chain contains a malformed token")
    payload = "\0".join(emitted_chain).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_button_chain(button: PointerButton, emitted_chain: Sequence[str]) -> None:
    """Require the exact xdotool chain to carry the admitted button role."""

    down_indices = [index for index, token in enumerate(emitted_chain) if token == "mousedown"]
    up_indices = [index for index, token in enumerate(emitted_chain) if token == "mouseup"]
    if len(down_indices) != 1 or len(up_indices) != 1:
        raise InputBrokerError("emitted button gesture chain has an incomplete button phase")
    down_index = down_indices[0]
    up_index = up_indices[0]
    if (
        down_index + 1 >= len(emitted_chain)
        or up_index + 1 >= len(emitted_chain)
        or down_index >= up_index
        or emitted_chain[down_index + 1] != emitted_chain[up_index + 1]
    ):
        raise InputBrokerError("emitted button gesture chain has mismatched button phases")
    chain_button = {
        "1": PointerButton.LEFT,
        "3": PointerButton.RIGHT,
    }.get(emitted_chain[down_index + 1])
    if chain_button is None:
        raise InputBrokerError("emitted button gesture chain uses an unsupported button")
    if chain_button is not button:
        raise InputBrokerError("admitted button gesture button does not match emitted chain")


@dataclass(slots=True)
class _Consumption:
    lock: threading.Lock = field(default_factory=threading.Lock)
    consumed: bool = False
    precommit_monotonic_ns: int | None = None


class RateReservation:
    """A broker-owned permit held until its admitted gesture is emitted."""

    __slots__ = ("_permit", "_closed")

    def __init__(self, permit: RatePermit) -> None:
        self._permit = permit
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed and self._permit.is_open

    @property
    def acquired_monotonic(self) -> float:
        return self._permit.acquired_monotonic

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._permit.close()


@dataclass(frozen=True, slots=True)
class AdmittedButtonGesture:
    """One immutable, one-use authorization for one automated button-down."""

    action_generation: int
    lease_generation: int
    button: PointerButton
    gesture_digest: str
    destination_anchor: str
    target_identity: str
    guardian_admission_id: str
    semantic_authorization: str
    deadline_monotonic: float
    _reservation: RateReservation = field(repr=False, compare=False)
    _consumption: _Consumption = field(repr=False, compare=False, default_factory=_Consumption)

    def __post_init__(self) -> None:
        if isinstance(self.action_generation, bool) or not isinstance(self.action_generation, int):
            raise InputBrokerError("admitted gesture action generation is malformed")
        if isinstance(self.lease_generation, bool) or not isinstance(self.lease_generation, int):
            raise InputBrokerError("admitted gesture lease generation is malformed")
        if not isinstance(self.button, PointerButton) or self.button not in (
            PointerButton.LEFT,
            PointerButton.RIGHT,
        ):
            raise InputBrokerError("admitted gesture button is not supported")
        if not isinstance(self.gesture_digest, str) or not self.gesture_digest.isascii():
            raise InputBrokerError("admitted gesture digest is malformed")
        if len(self.gesture_digest) != hashlib.sha256().digest_size * 2:
            raise InputBrokerError("admitted gesture digest is malformed")
        try:
            int(self.gesture_digest, 16)
        except ValueError as exc:
            raise InputBrokerError("admitted gesture digest is malformed") from exc
        for value, label in (
            (self.destination_anchor, "destination anchor"),
            (self.target_identity, "target identity"),
            (self.guardian_admission_id, "Guardian admission identifier"),
            (self.semantic_authorization, "semantic authorization"),
        ):
            if not isinstance(value, str) or not value:
                raise InputBrokerError(f"admitted gesture {label} is malformed")
        if isinstance(self.deadline_monotonic, bool) or not isinstance(self.deadline_monotonic, float):
            raise InputBrokerError("admitted gesture deadline is malformed")
        if not math.isfinite(self.deadline_monotonic):
            raise InputBrokerError("admitted gesture deadline is malformed")
        if not isinstance(self._reservation, RateReservation) or not self._reservation.is_open:
            raise InputBrokerError("admitted gesture has no open rate reservation")

    def consume(self) -> None:
        """Mark this admission as used exactly once."""

        with self._consumption.lock:
            if self._consumption.consumed:
                raise InputBrokerError("admitted button gesture has already been consumed")
            self._consumption.consumed = True

    @property
    def precommit_monotonic_ns(self) -> int:
        if self._consumption.precommit_monotonic_ns is None:
            raise InputBrokerError("admitted button gesture has not reached durable precommit")
        return self._consumption.precommit_monotonic_ns


class InputBroker:
    """The sole production owner of the durable automated-button rate gate."""

    def __init__(self) -> None:
        self._clock: Callable[[], float] = time.monotonic
        self._permit_factory: RatePermitFactory = acquire_click_rate_permit
        self._admitted_gestures: dict[int, AdmittedButtonGesture] = {}
        self._emission_claims: dict[int, ButtonEmissionClaim] = {}

    @classmethod
    def for_tests(
        cls,
        *,
        clock: Callable[[], float],
        permit_factory: RatePermitFactory,
    ) -> InputBroker:
        """Build a broker with explicit test-only clock and gate seams."""

        if not callable(clock) or not callable(permit_factory):
            raise InputBrokerError("test input broker requires callable clock and permit factory")
        broker = cls.__new__(cls)
        broker._clock = clock
        broker._permit_factory = permit_factory
        broker._admitted_gestures = {}
        broker._emission_claims = {}
        return broker

    def reserve_button_rate(
        self,
        *,
        deadline_monotonic: float | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> RateReservation:
        deadline = self._validate_deadline(deadline_monotonic)
        try:
            permit = self._permit_factory(
                deadline_monotonic=deadline,
                cancellation_check=cancellation_check,
            )
        except PointerExecutionError as exc:
            raise InputBrokerError(str(exc)) from exc
        if not isinstance(permit, RatePermit):
            raise InputBrokerError("input broker received a malformed rate permit")
        if not permit.is_open:
            permit.close()
            raise InputBrokerError("input broker received a closed rate permit")
        return RateReservation(permit)

    def adopt_permit_for_tests(self, permit: RatePermit) -> RateReservation:
        """Adapt a directly supplied legacy permit for pre-existing tests."""

        if not isinstance(permit, RatePermit) or not permit.is_open:
            raise InputBrokerError("input broker requires an open canonical rate permit")
        return RateReservation(permit)

    def release_reservation(self, reservation: RateReservation) -> None:
        if not isinstance(reservation, RateReservation):
            raise InputBrokerError("input broker rate reservation is malformed")
        reservation.close()

    def mint_admitted_gesture(
        self,
        *,
        action_generation: int,
        lease_generation: int,
        button: PointerButton,
        emitted_chain: Sequence[str],
        destination_anchor: str,
        target_identity: str,
        guardian_admission_id: str,
        semantic_authorization: str,
        deadline_monotonic: float | None = None,
        reservation: RateReservation | None = None,
    ) -> AdmittedButtonGesture:
        deadline = self._gesture_deadline(deadline_monotonic)
        owned_reservation = reservation
        created_reservation = owned_reservation is None
        if owned_reservation is None:
            owned_reservation = self.reserve_button_rate(deadline_monotonic=deadline_monotonic)
        try:
            if not isinstance(owned_reservation, RateReservation) or not owned_reservation.is_open:
                raise InputBrokerError("input broker rate reservation is closed")
            if self._clock() >= deadline:
                raise InputBrokerError("admitted button gesture deadline expired before minting")
            _validate_button_chain(button, emitted_chain)
            digest = gesture_digest(emitted_chain)
            gesture = AdmittedButtonGesture(
                action_generation=action_generation,
                lease_generation=lease_generation,
                button=button,
                gesture_digest=digest,
                destination_anchor=destination_anchor,
                target_identity=target_identity,
                guardian_admission_id=guardian_admission_id,
                semantic_authorization=semantic_authorization,
                deadline_monotonic=deadline,
                _reservation=owned_reservation,
            )
            self._admitted_gestures[id(gesture)] = gesture
            return gesture
        except Exception:
            if created_reservation and isinstance(owned_reservation, RateReservation):
                owned_reservation.close()
            raise

    def emit_button_gesture(
        self,
        gesture: AdmittedButtonGesture,
        *,
        emitted_chain: Sequence[str],
        action_generation: int,
        lease_generation: int,
        destination_anchor: str,
        target_identity: str,
        guardian_admission_id: str,
        semantic_authorization: str,
        emit: Callable[[ButtonEmissionClaim], None],
    ) -> int:
        """Precommit, emit, then record one admitted button gesture."""

        if not isinstance(gesture, AdmittedButtonGesture):
            raise InputBrokerError("input broker received a malformed admitted gesture")
        if self._admitted_gestures.get(id(gesture)) is not gesture:
            raise InputBrokerError("admitted button gesture was not minted by this broker")
        if gesture.action_generation != action_generation or gesture.lease_generation != lease_generation:
            raise InputBrokerError("admitted button gesture generation is stale")
        if (
            gesture.destination_anchor != destination_anchor
            or gesture.target_identity != target_identity
            or gesture.guardian_admission_id != guardian_admission_id
            or gesture.semantic_authorization != semantic_authorization
        ):
            raise InputBrokerError("admitted button gesture binding is stale")
        _validate_button_chain(gesture.button, emitted_chain)
        if gesture_digest(emitted_chain) != gesture.gesture_digest:
            raise InputBrokerError("admitted button gesture digest does not match emitted chain")
        if not callable(emit):
            raise InputBrokerError("input broker emitter is not callable")
        if self._clock() >= gesture.deadline_monotonic:
            raise InputBrokerError("admitted button gesture deadline expired before consumption")
        gesture.consume()
        try:
            if math.isfinite(gesture.deadline_monotonic):
                precommit = gesture._reservation._permit.precommit(
                    deadline_monotonic=gesture.deadline_monotonic
                )
            else:
                precommit = gesture._reservation._permit.precommit()
            gesture._consumption.precommit_monotonic_ns = precommit
            if self._clock() >= gesture.deadline_monotonic:
                raise InputBrokerError("admitted button gesture deadline expired after precommit")
            claim = ButtonEmissionClaim(
                nonce=object(),
                gesture_digest=gesture.gesture_digest,
                button=gesture.button,
                action_generation=gesture.action_generation,
                lease_generation=gesture.lease_generation,
            )
            self._emission_claims[id(claim)] = claim
            try:
                emit(claim)
                if self._emission_claims.get(id(claim)) is claim:
                    raise InputBrokerError("button emission callback did not redeem its broker claim")
                return gesture._reservation._permit.record_press()
            finally:
                self._emission_claims.pop(id(claim), None)
        except PointerExecutionError as exc:
            raise InputBrokerError(str(exc)) from exc

    def redeem_button_emission_claim(
        self,
        claim: ButtonEmissionClaim,
        *,
        emitted_chain: Sequence[str],
        button: PointerButton,
        action_generation: int,
        lease_generation: int,
    ) -> None:
        """Redeem one broker claim against the exact native button argv."""

        if not isinstance(claim, ButtonEmissionClaim):
            raise InputBrokerError("button emission claim is malformed")
        owned_claim = self._emission_claims.pop(id(claim), None)
        if owned_claim is not claim:
            raise InputBrokerError("button emission claim is foreign or already redeemed")
        if (
            claim.button is not button
            or claim.action_generation != action_generation
            or claim.lease_generation != lease_generation
        ):
            raise InputBrokerError("button emission claim binding is stale")
        _validate_button_chain(button, emitted_chain)
        if gesture_digest(emitted_chain) != claim.gesture_digest:
            raise InputBrokerError("button emission claim digest does not match argv")

    def release_held_inputs(self, release: Callable[[], None]) -> None:
        """Run emergency release-only cleanup without a button admission."""

        if not callable(release):
            raise InputBrokerError("input broker release callback is not callable")
        release()

    @staticmethod
    def _validate_deadline(deadline_monotonic: float | None) -> float | None:
        if deadline_monotonic is None:
            return None
        if isinstance(deadline_monotonic, bool) or not isinstance(deadline_monotonic, (int, float)):
            raise InputBrokerError("input broker deadline is malformed")
        deadline = float(deadline_monotonic)
        if not math.isfinite(deadline):
            raise InputBrokerError("input broker deadline is malformed")
        return deadline

    def _gesture_deadline(self, deadline_monotonic: float | None) -> float:
        deadline = self._validate_deadline(deadline_monotonic)
        return self._clock() + _DEFAULT_GESTURE_DEADLINE_SECONDS if deadline is None else deadline
