"""Action and event-correlation state for the input guardian.

The correlation record is mutated only by the guardian's ordered observation
phases and by the final commit phase under the guardian lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..input_events import RawMotionPairing

CORRELATION_WINDOW_MS = 250


@dataclass
class ActionCorrelation:
    generation: int
    target_xid: int
    source_device: int
    master_device: int
    event_classes: frozenset[str]
    detail: int | None
    started_monotonic: float
    deadline_monotonic: float
    device_by_class: tuple[tuple[str, int], ...] = ()
    target_ancestor: Callable[[int], bool] | None = None
    required_sequence: tuple[str, ...] = ()
    optional_classes: frozenset[str] = frozenset()
    sequence_index: int = 0
    observed_detail: int | None = None
    observed_details: dict[int, int] = field(default_factory=dict)
    observed_modifiers: dict[int, tuple[int, int, int] | None] = field(default_factory=dict)
    expected_modifiers: tuple[int, int, int] | None = None
    expected_details: tuple[int, ...] = ()
    expected_modifier_sequence: tuple[tuple[int, int, int], ...] = ()
    require_motion_before_button: bool = True
    expected_coordinates: tuple[float, float] | None = None
    expected_event_coordinates: tuple[float, float] | None = None
    expected_press_coordinates: tuple[float, float] | None = None
    expected_press_event_coordinates: tuple[float, float] | None = None
    expected_release_coordinates: tuple[float, float] | None = None
    expected_release_event_coordinates: tuple[float, float] | None = None
    expected_motion_trace: tuple[tuple[int, int, int, int], ...] = ()
    expected_positioning_trace: tuple[tuple[int, int, int, int], ...] = ()
    initial_origin: tuple[int, int, int, int] | None = None
    expected_root_xid: int | None = None
    expected_event_xid: int | None = None
    expected_child_xids: frozenset[int] = frozenset()
    canonical_motion: bool = False
    positioning_index: int = 0
    motion_index: int = 0
    # Planned holds are an allowlist for the exact native command that will
    # be admitted later.  They are deliberately separate from the physical
    # sets below: planning an input never authorizes a release for an input
    # that was not emitted.
    planned_held_keys: frozenset[str] = frozenset()
    planned_held_buttons: frozenset[int] = frozenset()
    require_motion_before_release: bool = False
    release_started: bool = False
    # Keyboard probe rows describe one raw/delivered pair for each physical
    # transition (two events); pointer rows describe press/release pairs (four
    # events).  Keep the historical pointer default while allowing a sealed
    # keyboard profile to pin modifiers for every transition.
    modifier_group_size: int = 4
    echo_proof: tuple[tuple[Any, ...], ...] = ()
    echo_index: int = 0
    require_delivered_modifiers: bool = True
    echo_sources: frozenset[int] = frozenset()
    # Canonical planned source-4 motions may each produce one separately
    # forwarded source-7 record. Keep exact outstanding pairs until the relay
    # is observed; canonical queue length is bounded by the finite motion plan,
    # and each pair is accepted only inside the correlation window.
    pending_echoes: list[
        tuple[
            int,
            int | None,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[int, int, int] | None,
        ]
    ] = field(default_factory=list)
    # RawMotion is diagnostic ancestry only. Each observed raw record must
    # pair one-to-one with a following primary delivered Motion carrying the
    # exact same XI2 server timestamp before the action can finish.
    raw_motion_pairing: RawMotionPairing = field(default_factory=RawMotionPairing)
    emitted_motion_count: int = 0
    current_motion_timestamp: int | None = None
    current_motion_target: int | None = None
    current_motion_root_xy: tuple[float, float] | None = None
    current_motion_event_xy: tuple[float, float] | None = None
    current_motion_modifiers: tuple[int, int, int] | None = None
    last_accepted_endpoint: tuple[int, int, int, int] | None = None

    def expected_device(self, event_class: str) -> int:
        for name, device in self.device_by_class:
            if name == event_class:
                return device
        return self.master_device

    @property
    def complete(self) -> bool:
        return (
            self.sequence_index >= len(self.required_sequence)
            and self.positioning_index >= len(self.expected_positioning_trace)
            and self.motion_index >= len(self.expected_motion_trace)
            and not self.raw_motion_pairing.pending
        )


@dataclass(frozen=True, slots=True)
class EchoGraceExpectation:
    """One exact forwarded echo that remains admissible after completion."""

    generation: int
    deadline_monotonic: float
    timestamp: int
    target_xid: int | None
    root_xy: tuple[float, float] | None
    event_xy: tuple[float, float] | None
    modifiers: tuple[int, int, int] | None
    echo_sources: frozenset[int]
    canonical_motion: bool
    expected_root_xid: int | None
    expected_event_xid: int | None
    require_delivered_modifiers: bool


@dataclass(slots=True)
class _Observation:
    """Mutable state passed through the ordered observation phases."""

    event: Any
    action: ActionCorrelation | None
    prior_timestamp: int | None
    generation: int
    event_class: Any
    target: Any
    target_ok: bool = False
    echo_match: bool = False
    motion_match_index: int | None = None
    positioning_match_index: int | None = None
    echo_pending_index: int | None = None
    grace_expectation: EchoGraceExpectation | None = None
    grace_index: int | None = None
    grace_expectations: tuple[EchoGraceExpectation, ...] = ()
    matched_echo_expectation: EchoGraceExpectation | None = None
    action_pending_echoes: tuple[
        tuple[
            int,
            int | None,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[int, int, int] | None,
        ],
        ...,
    ] = ()
    canonical_motion_checked: bool = False
    canonical_raw_queued: bool = False
    canonical_endpoint: tuple[int, int] | None = None
    sequence_ok: bool = True
    now: float = 0.0
    timestamp: Any = None
    pending_raw_primary: bool = False
    timestamp_ok: bool = False
    allowed: bool = False
