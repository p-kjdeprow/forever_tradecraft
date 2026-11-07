from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from crowley.session.driver import X11Backend
from crowley.session.executor import PointerButton
from crowley.session.input_broker import ButtonEmissionClaim, InputBroker, InputBrokerError
from crowley.session.pointer_execution import PointerExecutionError


@dataclass
class _FakePermit:
    clock: list[float]
    events: list[str]
    gate: _FakeGate
    is_open: bool = True

    @property
    def acquired_monotonic(self) -> float:
        return self.clock[0]

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        if deadline_monotonic is not None and self.clock[0] >= deadline_monotonic:
            raise PointerExecutionError("deadline expired")
        self.events.append("precommit")
        return len(self.events)

    def record_press(self) -> int:
        self.events.append("record_press")
        self.gate.next_allowed = self.clock[0] + 1.0
        return len(self.events)

    def close(self) -> None:
        self.is_open = False


@dataclass
class _FakeGate:
    clock: list[float]
    events: list[str] = field(default_factory=list)
    next_allowed: float = 0.0
    reservations: int = 0

    def acquire(self, **_kwargs: object) -> _FakePermit:
        if self.clock[0] < self.next_allowed:
            raise PointerExecutionError("click spacing wait cannot fit before its deadline")
        self.reservations += 1
        return _FakePermit(self.clock, self.events, self)


def _broker() -> tuple[InputBroker, _FakeGate, list[float]]:
    clock = [0.0]
    gate = _FakeGate(clock)
    return InputBroker.for_tests(clock=lambda: clock[0], permit_factory=gate.acquire), gate, clock


def _mint(
    broker: InputBroker,
    *,
    button: PointerButton = PointerButton.LEFT,
    action_generation: int = 4,
    lease_generation: int = 9,
    chain: tuple[str, ...] = ("xdotool", "mousedown", "1", "mouseup", "1"),
    deadline: float = 10.0,
    semantic_authorization: str = "test-click",
):
    if button is PointerButton.RIGHT and chain == ("xdotool", "mousedown", "1", "mouseup", "1"):
        chain = ("xdotool", "mousedown", "3", "mouseup", "3")
    reservation = broker.reserve_button_rate(deadline_monotonic=deadline)
    gesture = broker.mint_admitted_gesture(
        action_generation=action_generation,
        lease_generation=lease_generation,
        button=button,
        emitted_chain=chain,
        destination_anchor="anchor-1",
        target_identity="client-1",
        guardian_admission_id="guardian-1",
        semantic_authorization=semantic_authorization,
        deadline_monotonic=deadline,
        reservation=reservation,
    )
    return gesture, reservation, chain


def _emit(
    broker: InputBroker,
    gesture,
    chain: tuple[str, ...],
    *,
    action: int = 4,
    lease: int = 9,
    semantic_authorization: str = "test-click",
):
    return broker.emit_button_gesture(
        gesture,
        emitted_chain=chain,
        action_generation=action,
        lease_generation=lease,
        destination_anchor="anchor-1",
        target_identity="client-1",
        guardian_admission_id="guardian-1",
        semantic_authorization=semantic_authorization,
        emit=lambda claim: broker.redeem_button_emission_claim(
            claim,
            emitted_chain=chain,
            button=gesture.button,
            action_generation=action,
            lease_generation=lease,
        ),
    )


def test_admitted_gesture_is_immutable_and_one_use() -> None:
    broker, _gate, _clock = _broker()
    gesture, reservation, chain = _mint(broker)
    try:
        with pytest.raises(AttributeError):
            gesture.action_generation = 8  # type: ignore[misc]
        _emit(broker, gesture, chain)
        with pytest.raises(InputBrokerError, match="already been consumed"):
            _emit(broker, gesture, chain)
    finally:
        broker.release_reservation(reservation)


def test_admitted_gesture_cannot_cross_broker_provenance() -> None:
    owner, _gate, _clock = _broker()
    foreign, _foreign_gate, _foreign_clock = _broker()
    gesture, reservation, chain = _mint(owner)
    try:
        with pytest.raises(InputBrokerError, match="not minted by this broker"):
            foreign.emit_button_gesture(
                gesture,
                emitted_chain=chain,
                action_generation=4,
                lease_generation=9,
                destination_anchor="anchor-1",
                target_identity="client-1",
                guardian_admission_id="guardian-1",
                semantic_authorization="test-click",
                emit=lambda _claim: pytest.fail("foreign gesture reached emission"),
            )
    finally:
        owner.release_reservation(reservation)


def test_emission_claim_cannot_cross_broker_provenance() -> None:
    owner, _owner_gate, _owner_clock = _broker()
    foreign, _foreign_gate, _foreign_clock = _broker()
    gesture, reservation, chain = _mint(owner)
    try:

        def emit(claim: ButtonEmissionClaim) -> None:
            with pytest.raises(InputBrokerError, match="foreign or already redeemed"):
                foreign.redeem_button_emission_claim(
                    claim,
                    emitted_chain=chain,
                    button=gesture.button,
                    action_generation=4,
                    lease_generation=9,
                )
            owner.redeem_button_emission_claim(
                claim,
                emitted_chain=chain,
                button=gesture.button,
                action_generation=4,
                lease_generation=9,
            )

        owner.emit_button_gesture(
            gesture,
            emitted_chain=chain,
            action_generation=4,
            lease_generation=9,
            destination_anchor="anchor-1",
            target_identity="client-1",
            guardian_admission_id="guardian-1",
            semantic_authorization="test-click",
            emit=emit,
        )
    finally:
        owner.release_reservation(reservation)


def test_gesture_digest_mismatch_is_refused_without_consuming_the_admission() -> None:
    broker, _gate, _clock = _broker()
    gesture, reservation, chain = _mint(broker)
    try:
        with pytest.raises(InputBrokerError, match="digest"):
            _emit(broker, gesture, (*chain, "unexpected"))
        _emit(broker, gesture, chain)
    finally:
        broker.release_reservation(reservation)


def test_button_binding_refuses_left_admission_for_right_button_chain() -> None:
    broker, gate, _clock = _broker()
    reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
    try:
        with pytest.raises(InputBrokerError, match="button does not match"):
            broker.mint_admitted_gesture(
                action_generation=4,
                lease_generation=9,
                button=PointerButton.LEFT,
                emitted_chain=("xdotool", "mousedown", "3", "mouseup", "3"),
                destination_anchor="anchor-1",
                target_identity="client-1",
                guardian_admission_id="guardian-1",
                semantic_authorization="test-click",
                deadline_monotonic=10.0,
                reservation=reservation,
            )
        assert gate.events == []
    finally:
        broker.release_reservation(reservation)


def test_expired_deadline_is_refused_before_precommit() -> None:
    broker, gate, clock = _broker()
    gesture, reservation, chain = _mint(broker, deadline=1.0)
    try:
        clock[0] = 1.0
        with pytest.raises(InputBrokerError, match="deadline expired"):
            _emit(broker, gesture, chain)
        assert gate.events == []
    finally:
        broker.release_reservation(reservation)


@pytest.mark.parametrize("field", ["action", "lease"])
def test_stale_generation_is_refused(field: str) -> None:
    broker, gate, _clock = _broker()
    gesture, reservation, chain = _mint(broker)
    try:
        kwargs = {"action": 4, "lease": 9}
        kwargs[field] += 1
        with pytest.raises(InputBrokerError, match="generation is stale"):
            _emit(broker, gesture, chain, action=kwargs["action"], lease=kwargs["lease"])
        assert gate.events == []
    finally:
        broker.release_reservation(reservation)


@pytest.mark.parametrize(
    ("first_button", "second_button"),
    [
        (PointerButton.LEFT, PointerButton.RIGHT),
        (PointerButton.RIGHT, PointerButton.LEFT),
    ],
)
def test_all_button_downs_share_the_one_second_gate(
    first_button: PointerButton, second_button: PointerButton
) -> None:
    broker, gate, clock = _broker()
    first, first_reservation, first_chain = _mint(broker, button=first_button)
    try:
        _emit(broker, first, first_chain)
        clock[0] = 0.5
        with pytest.raises(InputBrokerError, match="click spacing"):
            broker.reserve_button_rate(deadline_monotonic=10.0)
        clock[0] = 1.0
        second_reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
        try:
            second_button_number = "3" if second_button is PointerButton.RIGHT else "1"
            second_chain = ("xdotool", "mousedown", second_button_number, "mouseup", second_button_number)
            second = broker.mint_admitted_gesture(
                action_generation=4,
                lease_generation=9,
                button=second_button,
                emitted_chain=second_chain,
                destination_anchor="anchor-2",
                target_identity="client-1",
                guardian_admission_id="guardian-2",
                semantic_authorization="camera-drag"
                if second_button is PointerButton.RIGHT
                else "test-click",
                deadline_monotonic=10.0,
                reservation=second_reservation,
            )
            broker.emit_button_gesture(
                second,
                emitted_chain=second_chain,
                action_generation=4,
                lease_generation=9,
                destination_anchor="anchor-2",
                target_identity="client-1",
                guardian_admission_id="guardian-2",
                semantic_authorization="camera-drag"
                if second_button is PointerButton.RIGHT
                else "test-click",
                emit=lambda claim: broker.redeem_button_emission_claim(
                    claim,
                    emitted_chain=second_chain,
                    button=second_button,
                    action_generation=4,
                    lease_generation=9,
                ),
            )
        finally:
            broker.release_reservation(second_reservation)
        assert gate.reservations == 2
    finally:
        broker.release_reservation(first_reservation)


@pytest.mark.parametrize(
    ("first_chain", "first_button", "second_chain", "second_button"),
    [
        (
            ("xdotool", "mousedown", "1", "sleep", "0.100000", "mouseup", "1"),
            PointerButton.LEFT,
            ("xdotool", "mousedown", "3", "sleep", "0.100000", "mouseup", "3"),
            PointerButton.RIGHT,
        ),
        (
            ("xdotool", "mousedown", "3", "sleep", "0.100000", "mouseup", "3"),
            PointerButton.RIGHT,
            ("xdotool", "mousedown", "1", "sleep", "0.100000", "mouseup", "1"),
            PointerButton.LEFT,
        ),
    ],
)
def test_production_click_and_camera_chains_share_the_rate_gate(
    first_chain: tuple[str, ...],
    first_button: PointerButton,
    second_chain: tuple[str, ...],
    second_button: PointerButton,
) -> None:
    broker, gate, clock = _broker()
    first, first_reservation, _ = _mint(broker, button=first_button, chain=first_chain)
    try:
        _emit(broker, first, first_chain)
        clock[0] = 0.999
        with pytest.raises(InputBrokerError, match="click spacing"):
            broker.reserve_button_rate(deadline_monotonic=10.0)
        clock[0] = 1.0
        second_reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
        try:
            second = broker.mint_admitted_gesture(
                action_generation=4,
                lease_generation=9,
                button=second_button,
                emitted_chain=second_chain,
                destination_anchor="production-anchor-2",
                target_identity="client-1",
                guardian_admission_id="production-guardian-2",
                semantic_authorization=(
                    "camera_drag" if second_button is PointerButton.RIGHT else "world_click"
                ),
                deadline_monotonic=10.0,
                reservation=second_reservation,
            )
            broker.emit_button_gesture(
                second,
                emitted_chain=second_chain,
                action_generation=4,
                lease_generation=9,
                destination_anchor="production-anchor-2",
                target_identity="client-1",
                guardian_admission_id="production-guardian-2",
                semantic_authorization=(
                    "camera_drag" if second_button is PointerButton.RIGHT else "world_click"
                ),
                emit=lambda claim: broker.redeem_button_emission_claim(
                    claim,
                    emitted_chain=second_chain,
                    button=second_button,
                    action_generation=4,
                    lease_generation=9,
                ),
            )
        finally:
            broker.release_reservation(second_reservation)
        assert gate.reservations == 2
    finally:
        broker.release_reservation(first_reservation)


def test_calibration_click_and_runtime_click_share_the_rate_gate() -> None:
    broker, gate, clock = _broker()
    calibration_chain = ("xdotool", "mousedown", "1", "sleep", "0.100000", "mouseup", "1")
    runtime_chain = ("xdotool", "mousedown", "1", "sleep", "0.100000", "mouseup", "1")
    calibration, calibration_reservation, _ = _mint(
        broker,
        chain=calibration_chain,
        semantic_authorization="input_calibration",
    )
    try:
        _emit(
            broker,
            calibration,
            calibration_chain,
            semantic_authorization="input_calibration",
        )
        clock[0] = 0.999
        with pytest.raises(InputBrokerError, match="click spacing"):
            broker.reserve_button_rate(deadline_monotonic=10.0)
        clock[0] = 1.0
        runtime_reservation = broker.reserve_button_rate(deadline_monotonic=10.0)
        try:
            runtime = broker.mint_admitted_gesture(
                action_generation=4,
                lease_generation=9,
                button=PointerButton.LEFT,
                emitted_chain=runtime_chain,
                destination_anchor="runtime-anchor",
                target_identity="client-1",
                guardian_admission_id="runtime-guardian",
                semantic_authorization="world_click",
                deadline_monotonic=10.0,
                reservation=runtime_reservation,
            )
            broker.emit_button_gesture(
                runtime,
                emitted_chain=runtime_chain,
                action_generation=4,
                lease_generation=9,
                destination_anchor="runtime-anchor",
                target_identity="client-1",
                guardian_admission_id="runtime-guardian",
                semantic_authorization="world_click",
                emit=lambda claim: broker.redeem_button_emission_claim(
                    claim,
                    emitted_chain=runtime_chain,
                    button=PointerButton.LEFT,
                    action_generation=4,
                    lease_generation=9,
                ),
            )
        finally:
            broker.release_reservation(runtime_reservation)
        assert gate.reservations == 2
    finally:
        broker.release_reservation(calibration_reservation)


def test_release_cleanup_does_not_need_a_gesture_or_rate_reservation() -> None:
    broker, gate, _clock = _broker()
    released: list[str] = []
    broker.release_held_inputs(lambda: released.append("button-up/key-up"))
    assert released == ["button-up/key-up"]
    assert gate.reservations == 0


def test_x11_release_held_input_is_permit_free() -> None:
    backend = object.__new__(X11Backend)
    backend._process_lock = threading.RLock()
    backend._active_processes = set()
    backend._process_generations = {}
    backend._canceled_generations = set()
    backend._cancel_active = threading.Event()
    backend._held_keys = {"A"}
    backend._held_buttons = {3}
    released: list[list[str]] = []
    backend._release_command = lambda args: released.append(args) or True
    backend.input_broker = object()

    backend.release_held_input()

    assert released == [["xdotool", "keyup", "A"], ["xdotool", "mouseup", "3"]]
    assert backend._held_keys == set()
    assert backend._held_buttons == set()


def test_precommit_emit_record_order_is_owned_by_the_broker() -> None:
    broker, gate, _clock = _broker()
    gesture, reservation, chain = _mint(broker)
    try:

        def emit(claim: ButtonEmissionClaim) -> None:
            gate.events.append("emit")
            broker.redeem_button_emission_claim(
                claim,
                emitted_chain=chain,
                button=gesture.button,
                action_generation=4,
                lease_generation=9,
            )

        broker.emit_button_gesture(
            gesture,
            emitted_chain=chain,
            action_generation=4,
            lease_generation=9,
            destination_anchor="anchor-1",
            target_identity="client-1",
            guardian_admission_id="guardian-1",
            semantic_authorization="test-click",
            emit=emit,
        )
        assert gate.events == ["precommit", "emit", "record_press"]
    finally:
        broker.release_reservation(reservation)
