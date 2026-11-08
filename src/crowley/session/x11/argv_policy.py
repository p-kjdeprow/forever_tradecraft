"""Pure allowlist classification for the commands sent to the X11 boundary."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

InputSchema = Literal[
    "motion-key",
    "motion-timeline",
    "camera-drag",
    "pointer-motion",
    "single-click",
    "keyboard-steps",
]

KNOWN_SCHEMAS = frozenset(
    {
        "motion-key",
        "motion-timeline",
        "camera-drag",
        "pointer-motion",
        "single-click",
        "keyboard-steps",
    }
)


@dataclass(frozen=True, slots=True)
class Admission:
    """The complete pure result of classifying one command vector."""

    valid: bool
    mutating: bool
    keys: frozenset[str] = frozenset()
    buttons: frozenset[int] = frozenset()
    destinations: frozenset[tuple[int, int]] = frozenset()
    reason: str | None = None


def _invalid(reason: str) -> Admission:
    return Admission(valid=False, mutating=False, reason=reason)


def _xid(value: str) -> bool:
    return bool(re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value))


def _integer(value: str) -> bool:
    return bool(re.fullmatch(r"-?[0-9]+", value))


def _physical_keycode(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{3}", value)) and 8 <= int(value) <= 255


def _bounded_sleep(value: str) -> bool:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        return False
    amount = float(value)
    return math.isfinite(amount) and 0.0 < amount <= 2.0


def _pointer_segment(
    tokens: Sequence[str], *, require_motion: bool, minimum_moves: int = 1
) -> tuple[bool, frozenset[tuple[int, int]]]:
    """Validate a pointer-only timed segment and collect destinations."""

    if len(tokens) > 4096:
        return False, frozenset()
    if not tokens:
        return not require_motion, frozenset()
    index = 0
    destinations: set[tuple[int, int]] = set()
    elapsed = 0.0
    while index < len(tokens):
        if tokens[index] != "sleep" or index + 1 >= len(tokens):
            return False, frozenset()
        if not _bounded_sleep(tokens[index + 1]):
            return False, frozenset()
        elapsed += float(tokens[index + 1])
        if elapsed > 2.0:
            return False, frozenset()
        index += 2
        if index < len(tokens):
            if tokens[index] == "sleep":
                continue
            if tokens[index] != "mousemove" or index + 2 >= len(tokens):
                return False, frozenset()
            if not _integer(tokens[index + 1]) or not _integer(tokens[index + 2]):
                return False, frozenset()
            destinations.add((int(tokens[index + 1]), int(tokens[index + 2])))
            index += 3
    required_destinations = minimum_moves if require_motion else 0
    return len(destinations) >= required_destinations, frozenset(destinations)


def _keyboard_chain(tokens: Sequence[str]) -> tuple[bool, frozenset[str]]:
    """Validate explicit physical key transitions and return their key set."""

    if not tokens or len(tokens) > 4096 or len(tokens) % 2:
        return False, frozenset()
    held: list[str] = []
    keys: set[str] = set()
    for index in range(0, len(tokens), 2):
        operation, keycode = tokens[index : index + 2]
        if not _physical_keycode(keycode):
            return False, frozenset()
        if operation == "keydown":
            if keycode in held:
                return False, frozenset()
            held.append(keycode)
        elif operation == "keyup":
            if not held or held[-1] != keycode:
                return False, frozenset()
            held.pop()
        else:
            return False, frozenset()
        keys.add(keycode)
    return bool(tokens) and not held, frozenset(keys)


def _camera_chain(tokens: Sequence[str]) -> tuple[bool, frozenset[tuple[int, int]]]:
    """Validate positioning followed by one held right-button drag."""

    if not tokens or len(tokens) > 4096 or tuple(tokens[-2:]) != ("mouseup", "3"):
        return False, frozenset()
    presses = [index for index, token in enumerate(tokens) if token == "mousedown"]
    if len(presses) != 1:
        return False, frozenset()
    press = presses[0]
    if tuple(tokens[press : press + 2]) != ("mousedown", "3"):
        return False, frozenset()
    prefix = tokens[:press]
    held = tokens[press + 2 : -2]
    prefix_valid, prefix_destinations = _pointer_segment(prefix, require_motion=False)
    held_valid, held_destinations = _pointer_segment(held, require_motion=True, minimum_moves=1)
    destinations = prefix_destinations | held_destinations
    valid = prefix_valid and held_valid and len(destinations) >= 2
    return valid, destinations


def _motion_timeline(tokens: Sequence[str]) -> tuple[bool, frozenset[str]]:
    """Validate one sealed keyboard timeline and return its key set."""

    if not tokens or len(tokens) > 256:
        return False, frozenset()
    index = 0
    active: set[str] = set()
    keys: set[str] = set()
    pressed_at: dict[str, float] = {}
    last_release: dict[str, float] = {}
    elapsed = 0.0
    event_count = 0
    while index < len(tokens):
        operation = tokens[index]
        if operation in {"keydown", "keyup"}:
            if index + 1 >= len(tokens):
                return False, frozenset()
            key = tokens[index + 1]
            if key not in {"w", "q", "e", "space"}:
                return False, frozenset()
            if operation == "keydown":
                if key in active or (key in last_release and elapsed == last_release[key]):
                    return False, frozenset()
                active.add(key)
                pressed_at[key] = elapsed
            elif key not in active:
                return False, frozenset()
            else:
                hold = elapsed - pressed_at[key]
                if hold < 0.03 or hold > 0.45:
                    return False, frozenset()
                active.remove(key)
                del pressed_at[key]
                last_release[key] = elapsed
            keys.add(key)
            event_count += 1
            if event_count > 64:
                return False, frozenset()
            index += 2
            continue
        if operation == "sleep":
            if index + 1 >= len(tokens) or not _bounded_sleep(tokens[index + 1]):
                return False, frozenset()
            elapsed += float(tokens[index + 1])
            if elapsed > 6.0:
                return False, frozenset()
            index += 2
            continue
        return False, frozenset()
    return event_count > 0 and not active, frozenset(keys)


def _typed_admission(args: Sequence[str], schema: str) -> Admission:
    if schema == "motion-key":
        if len(args) != 7:
            return Admission(False, False, reason="motion-key grammar")
        valid = (
            args[2] == args[6]
            and args[1] == "keydown"
            and args[3] == "sleep"
            and args[5] == "keyup"
            and bool(re.fullmatch(r"[a-z]", args[2]))
            and _bounded_sleep(args[4])
        )
        keys = frozenset({args[2]}) if valid else frozenset()
        return Admission(valid, valid, keys=keys, reason=None if valid else "motion-key grammar")
    if schema == "motion-timeline":
        valid, keys = _motion_timeline(args[1:])
        return Admission(valid, valid, keys=keys, reason=None if valid else "motion-timeline grammar")
    if schema == "camera-drag":
        valid, destinations = _camera_chain(args[1:])
        return Admission(
            valid,
            valid,
            buttons=frozenset({3}) if valid else frozenset(),
            destinations=destinations,
            reason=None if valid else "camera-drag grammar",
        )
    if schema == "single-click":
        if len(args) != 7:
            return Admission(False, False, reason="single-click grammar")
        valid = (
            args[1] == "mousedown"
            and args[2] in {"1", "3"}
            and args[3] == "sleep"
            and _bounded_sleep(args[4])
            and args[5] == "mouseup"
            and args[6] == args[2]
        )
        return Admission(
            valid,
            valid,
            buttons=frozenset({int(args[2])}) if valid else frozenset(),
            reason=None if valid else "single-click grammar",
        )
    if schema == "keyboard-steps":
        valid, keys = _keyboard_chain(args[1:])
        return Admission(valid, valid, keys=keys, reason=None if valid else "keyboard-steps grammar")
    valid, destinations = _pointer_segment(args[1:], require_motion=True, minimum_moves=2)
    return Admission(
        valid,
        valid,
        destinations=destinations,
        reason=None if valid else "pointer-motion grammar",
    )


def classify(argv: Sequence[str], schema: str | None = None) -> Admission:
    """Classify an argv vector without executing or consulting host state."""

    args = tuple(argv)
    if not args or any(not isinstance(argument, str) for argument in args):
        return _invalid("argv is empty or contains a non-string")
    if schema is not None and schema not in KNOWN_SCHEMAS:
        return _invalid("unknown input schema")
    command_name = os.path.basename(args[0])
    if command_name == "true":
        return Admission(len(args) == 1, False, reason=None if len(args) == 1 else "true grammar")
    if command_name == "sleep":
        valid = len(args) == 2 and bool(re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", args[1]))
        return Admission(valid, False, reason=None if valid else "sleep grammar")
    if command_name == "gnome-screenshot":
        valid = len(args) == 3 and args[1] == "-f" and os.path.isabs(args[2])
        return Admission(valid, False, reason=None if valid else "gnome-screenshot grammar")
    if command_name == "xinput":
        valid = (
            args == ("xinput", "--list", "--short")
            or (
                len(args) == 4
                and args[1] == "list"
                and args[2] == "--long"
                and bool(re.fullmatch(r"[1-9][0-9]*", args[3]))
            )
            or (len(args) == 3 and args[1] == "list-props" and bool(re.fullmatch(r"[1-9][0-9]*", args[2])))
        )
        return Admission(valid, False, reason=None if valid else "xinput grammar")
    if command_name == "xmodmap":
        valid = args == ("xmodmap", "-pke")
        return Admission(valid, False, reason=None if valid else "xmodmap grammar")
    if command_name == "xwininfo":
        valid = args in {
            ("xwininfo", "-root", "-tree"),
            ("xwininfo", "-root", "-stats"),
        } or (len(args) == 4 and args[1] == "-id" and _xid(args[2]) and args[3] in {"-tree", "-geometry"})
        return Admission(valid, False, reason=None if valid else "xwininfo grammar")
    if command_name == "xprop":
        valid = args == ("xprop", "-root", "_NET_ACTIVE_WINDOW") or (
            len(args) >= 4
            and args[1] == "-id"
            and _xid(args[2])
            and all(
                property_name in {"WM_NAME", "_NET_WM_NAME", "_NET_WM_PID", "WM_CLASS", "_NET_ACTIVE_WINDOW"}
                for property_name in args[3:]
            )
        )
        return Admission(valid, False, reason=None if valid else "xprop grammar")
    if command_name != "xdotool":
        return _invalid("command is not allowlisted")

    subcommand = args[1] if len(args) > 1 else ""
    if subcommand == "search":
        valid = len(args) == 4 and args[2] == "--name" and bool(args[3])
        return Admission(valid, False, reason=None if valid else "xdotool search grammar")
    if subcommand == "getwindowgeometry":
        valid = len(args) == 4 and args[2] == "--shell" and _xid(args[3])
        return Admission(valid, False, reason=None if valid else "xdotool getwindowgeometry grammar")
    if subcommand == "getwindowfocus":
        valid = len(args) == 2
        return Admission(valid, False, reason=None if valid else "xdotool getwindowfocus grammar")
    if subcommand == "getmouselocation":
        valid = args == ("xdotool", "getmouselocation", "--shell")
        return Admission(valid, False, reason=None if valid else "xdotool getmouselocation grammar")
    if subcommand in {"windowactivate", "windowfocus", "windowraise"}:
        valid = len(args) == 4 and args[2] == "--sync" and _xid(args[3])
        return Admission(valid, valid, reason=None if valid else "xdotool window grammar")
    if schema is not None:
        return _typed_admission(args, schema)
    if subcommand in {"key", "keydown", "keyup"}:
        if len(args) not in {3, 4}:
            return Admission(False, False, reason="xdotool key grammar")
        if len(args) == 4 and args[2] != "--":
            return Admission(False, False, reason="xdotool key grammar")
        key = tuple(args)[-1]
        valid = bool(re.fullmatch(r"[A-Za-z0-9_+.-]+", key)) and "--clearmodifiers" not in args
        keys = frozenset(part for part in key.split("+") if part) if valid else frozenset()
        return Admission(valid, valid, keys=keys, reason=None if valid else "xdotool key grammar")
    # Bare mousemove, mousedown, and mouseup are intentionally refused.  They
    # cannot carry the sealed human gesture schema or a broker claim.
    return _invalid("xdotool command requires a sealed input schema")


__all__ = ["Admission", "InputSchema", "KNOWN_SCHEMAS", "classify"]
