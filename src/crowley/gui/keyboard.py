"""Keysym mapping and exact XTEST press/release transitions for typed text and keys.

The pinned input tool is driven with numeric keycode tokens (``keydown 038``,
``keyup 038``) taken from a measured keymap.  This module builds those argv
lists; running them is the caller's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

type KeyStep = tuple[str, str]


class KeyboardError(RuntimeError):
    """A key, character or transition chain is outside the allowlisted contract."""


def keysym_for_char(char: str) -> str | None:
    """Return the allowlisted keysym chord (``+``-joined) for one character, or None."""

    if char.isascii() and char.isalpha() and char.isupper():
        return f"Shift_L+{char.lower()}"
    if char.isascii() and char.isalnum():
        return char
    return {
        "/": "slash",
        " ": "space",
        "-": "minus",
        "=": "equal",
        ";": "semicolon",
        "'": "apostrophe",
        ",": "comma",
        "(": "Shift_L+9",
        ")": "Shift_L+0",
    }.get(char)


def transition_argv(
    steps: tuple[KeyStep, ...], keymap: Mapping[str, int]
) -> tuple[list[str], frozenset[str]]:
    """Build exact keycode transitions and their bounded held-key set.

    Numeric keycode tokens make xdotool send the measured physical key
    directly. Passing modifier keysyms through xdotool's keysym path also
    causes it to synthesize that modifier from the key's own modifier map,
    producing an unmatched raw release on this server.
    """

    if not steps or not isinstance(keymap, Mapping):
        raise KeyboardError("keyboard transition chain is empty")
    argv = ["xdotool"]
    held: list[str] = []
    for phase, keysym in steps:
        if not isinstance(keysym, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", keysym):
            raise KeyboardError("keyboard transition keysym is malformed")
        keycode = keymap.get(keysym)
        if isinstance(keycode, bool) or not isinstance(keycode, int) or not 8 <= keycode <= 255:
            raise KeyboardError(f"keyboard transition keysym {keysym!r} has no measured keycode")
        # xdo first tries XStringToKeysym and only then parses a numeric
        # keycode. Three digits keep keycodes 8 and 9 distinct from the
        # printable keysyms "8" and "9".
        physical = f"{keycode:03d}"
        if phase == "press":
            if physical in held:
                raise KeyboardError("keyboard transition presses a key that is already held")
            held.append(physical)
            argv.extend(("keydown", physical))
        elif phase == "release":
            if not held or physical != held[-1]:
                raise KeyboardError("keyboard transition releases a key that is not held")
            held.pop()
            argv.extend(("keyup", physical))
        else:
            raise KeyboardError("keyboard transition phase is malformed")
    if held:
        raise KeyboardError("keyboard transition chain leaves a key held")
    return argv, frozenset(f"{keymap[keysym]:03d}" for _phase, keysym in steps)


def key_argv(key: str, keymap: Mapping[str, int]) -> tuple[list[str], frozenset[str]]:
    """Return the argv and held-key set that press and release one key."""

    return transition_argv((("press", key), ("release", key)), keymap)


def char_transitions(char: str) -> tuple[KeyStep, ...]:
    """Return the press/release chain for one typed character (modifiers nest around the key)."""

    mapped = keysym_for_char(char)
    if mapped is None:
        raise KeyboardError(f"character {char!r} has no allowlisted XTEST keysym")
    components = tuple(part for part in mapped.split("+") if part)
    return tuple(
        [("press", component) for component in components]
        + [("release", component) for component in reversed(components)]
    )


def type_text_argv(text: str, keymap: Mapping[str, int]) -> list[tuple[list[str], frozenset[str]]]:
    """Return one ``(argv, held_keys)`` action per character of ``text``.

    ``xdotool type`` emits one opaque command with no way to correlate each
    character's raw/delivered press and release, so each character is its own
    XTEST key action.  Characters are validated lazily by the caller iterating
    ``char_transitions`` when execution order matters; this helper validates
    the whole text up front.
    """

    return [transition_argv(char_transitions(char), keymap) for char in text]
