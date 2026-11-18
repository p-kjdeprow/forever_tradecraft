"""Small immutable keyboard plans, shared by both desktop callers and transports."""

from __future__ import annotations

import math
import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from .keyboard import KeyboardError, char_transitions, transition_argv

MAX_TIMELINE_SECONDS = 20.0
MAX_TEXT_LENGTH = 256


@dataclass(frozen=True, slots=True)
class KeyEvent:
    at: float
    phase: str
    key: str


@dataclass(frozen=True, slots=True)
class Timeline:
    events: tuple[KeyEvent, ...]
    duration: float
    seed: int

    def __post_init__(self) -> None:
        if type(self.seed) is not int or not 0 <= self.seed < 2**64:
            raise KeyboardError("timeline seed must be an unsigned 64-bit integer")
        if type(self.events) is not tuple or len(self.events) > 2048:
            raise KeyboardError("timeline events must be a bounded immutable tuple")
        if (
            type(self.duration) not in (int, float)
            or not math.isfinite(self.duration)
            or not 0 <= self.duration <= MAX_TIMELINE_SECONDS
        ):
            raise KeyboardError("timeline duration is outside its bound")
        previous = 0.0
        held: list[str] = []
        for event in self.events:
            if (
                type(event) is not KeyEvent
                or type(event.at) not in (int, float)
                or not math.isfinite(event.at)
            ):
                raise KeyboardError("timeline event is malformed")
            if not previous <= event.at <= self.duration or type(event.key) is not str or not event.key:
                raise KeyboardError("timeline event timing or key is invalid")
            if event.phase == "press" and event.key not in held:
                held.append(event.key)
            elif event.phase == "release" and held and held[-1] == event.key:
                held.pop()
            else:
                raise KeyboardError("timeline transitions are not balanced")
            previous = event.at
        if held or (not self.events and self.duration != 0):
            raise KeyboardError("timeline leaves controls held or has an empty delay")

    @property
    def timeout(self) -> float:
        """Declared gesture duration plus bounded startup/reap overhead, never an arm renewal."""
        return max(5.0, self.duration + 3.0)

    def xtest(self, keymap: Mapping[str, int]) -> tuple[list[str], frozenset[str]]:
        # Validate the complete mapping/chain before constructing any executable input.
        if not self.events:
            return ["xdotool"], frozenset()
        _, held = transition_argv(tuple((e.phase, e.key) for e in self.events), keymap)
        argv = ["xdotool"]
        previous = 0.0
        for event in self.events:
            if event.at > previous:
                argv.extend(("sleep", f"{event.at - previous:.9f}"))
            argv.extend(("keydown" if event.phase == "press" else "keyup", f"{keymap[event.key]:03d}"))
            previous = event.at
        if self.duration > previous:
            argv.extend(("sleep", f"{self.duration - previous:.9f}"))
        return argv, held


def plan_text(text: str, *, seed: int | None = None) -> Timeline:
    """Correlated fast typing, bounded dwell and boundary pauses, without invented typos."""
    if type(text) is not str or len(text) > MAX_TEXT_LENGTH:
        raise KeyboardError("typed text exceeds its bounded command length")
    transitions = tuple(char_transitions(char) for char in text)
    chosen = secrets.randbits(64) if seed is None else seed
    if type(chosen) is not int or not 0 <= chosen < 2**64:
        raise KeyboardError("timeline seed must be an unsigned 64-bit integer")
    rng = random.Random(chosen)
    cadence = rng.uniform(0.035, 0.050)
    at = 0.0
    events: list[KeyEvent] = []
    for char, steps in zip(text, transitions, strict=True):
        cadence = min(0.060, max(0.025, 0.8 * cadence + 0.2 * rng.uniform(0.025, 0.065)))
        dwell = rng.uniform(0.016, 0.028)
        for phase, key in steps:
            events.append(KeyEvent(at if phase == "press" else at + dwell, phase, key))
        at += max(cadence, dwell + 0.006)
        if char in " /;":
            at += rng.uniform(0.020, 0.065)
    return Timeline(tuple(events), at, chosen)


def validate_timed_keys(args: tuple[str, ...]) -> float:
    """Positive allowlist for the pinned tool: balanced numeric keys and bounded sleeps."""
    if not args or len(args) % 2 or len(args) > 8192:
        raise KeyboardError("timed key grammar is malformed")
    held: list[str] = []
    elapsed = 0.0
    presses = 0
    for index in range(0, len(args), 2):
        phase, value = args[index : index + 2]
        if phase == "sleep":
            try:
                delay = float(value)
            except ValueError as exc:
                raise KeyboardError("timed key delay is malformed") from exc
            if not math.isfinite(delay) or not 0 < delay <= MAX_TIMELINE_SECONDS:
                raise KeyboardError("timed key delay is outside its bound")
            elapsed += delay
        elif value.isdecimal() and len(value) in {2, 3} and 8 <= int(value) <= 255:
            if phase == "keydown" and value not in held:
                held.append(value)
                presses += 1
            elif phase == "keyup" and held and held[-1] == value:
                held.pop()
            else:
                raise KeyboardError("timed key transitions are unbalanced")
        else:
            raise KeyboardError("timed key operation is outside its allowlist")
    if held or not presses or elapsed > MAX_TIMELINE_SECONDS:
        raise KeyboardError("timed key plan is unbalanced or exceeds its duration")
    return elapsed
