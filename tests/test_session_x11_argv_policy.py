"""Table coverage for the pure X11 argv admission grammar."""

from __future__ import annotations

from pathlib import Path

import pytest

from crowley.session.x11.argv_policy import classify


@pytest.mark.parametrize(
    ("argv", "schema", "keys", "buttons"),
    [
        (["true"], None, (), ()),
        (["sleep", "0.1"], None, (), ()),
        (["gnome-screenshot", "-f", "<capture-path>"], None, (), ()),
        (["xinput", "--list", "--short"], None, (), ()),
        (["xinput", "list", "--long", "4"], None, (), ()),
        (["xinput", "list-props", "4"], None, (), ()),
        (["xmodmap", "-pke"], None, (), ()),
        (["xwininfo", "-root", "-tree"], None, (), ()),
        (["xwininfo", "-root", "-stats"], None, (), ()),
        (["xwininfo", "-id", "0x10", "-geometry"], None, (), ()),
        (["xwininfo", "-id", "0x10", "-tree"], None, (), ()),
        (["xprop", "-root", "_NET_ACTIVE_WINDOW"], None, (), ()),
        (["xprop", "-id", "0x10", "WM_NAME", "WM_CLASS"], None, (), ()),
        (["xdotool", "search", "--name", "World of Warcraft"], None, (), ()),
        (["xdotool", "getwindowgeometry", "--shell", "0x10"], None, (), ()),
        (["xdotool", "getwindowfocus"], None, (), ()),
        (["xdotool", "getmouselocation", "--shell"], None, (), ()),
        (["xdotool", "windowactivate", "--sync", "0x10"], None, (), ()),
        (["xdotool", "windowfocus", "--sync", "0x10"], None, (), ()),
        (["xdotool", "windowraise", "--sync", "0x10"], None, (), ()),
        (["xdotool", "key", "--", "F12"], None, ("F12",), ()),
        (["xdotool", "keydown", "040"], None, ("040",), ()),
        (["xdotool", "keyup", "040"], None, ("040",), ()),
        (
            ["xdotool", "keydown", "w", "sleep", "0.050", "keyup", "w"],
            "motion-key",
            ("w",),
            (),
        ),
        (
            ["xdotool", "sleep", "0.050", "keydown", "w", "sleep", "0.050", "keyup", "w"],
            "motion-timeline",
            ("w",),
            (),
        ),
        (
            ["xdotool", "sleep", "0.010", "mousemove", "10", "10", "sleep", "0.010", "mousemove", "20", "20"],
            "pointer-motion",
            (),
            (),
        ),
        (
            ["xdotool", "mousedown", "1", "sleep", "0.050", "mouseup", "1"],
            "single-click",
            (),
            (1,),
        ),
        (
            [
                "xdotool",
                "sleep",
                "0.010",
                "mousemove",
                "10",
                "10",
                "mousedown",
                "3",
                "sleep",
                "0.010",
                "mousemove",
                "20",
                "20",
                "mouseup",
                "3",
            ],
            "camera-drag",
            (),
            (3,),
        ),
        (
            ["xdotool", "keydown", "040", "keyup", "040"],
            "keyboard-steps",
            ("040",),
            (),
        ),
    ],
)
def test_admitted_forms_return_complete_metadata(
    argv: list[str],
    schema: str | None,
    keys: tuple[str, ...],
    buttons: tuple[int, ...],
    tmp_path: Path,
) -> None:
    if argv[0] == "gnome-screenshot":
        argv[2] = str(tmp_path / "capture.png")
    admission = classify(argv, schema)
    assert admission.valid
    assert admission.mutating is (
        schema is not None
        or argv[1] in {"windowactivate", "windowfocus", "windowraise", "key", "keydown", "keyup"}
        if argv[0] == "xdotool"
        else False
    )
    assert admission.keys == frozenset(keys)
    assert admission.buttons == frozenset(buttons)


@pytest.mark.parametrize(
    ("argv", "schema"),
    [
        ([], None),
        (["unknown"], None),
        (["true", "extra"], None),
        (["sleep", "-1"], None),
        (["xinput", "list", "--long", "0"], None),
        (["xwininfo", "-id", "bad", "-tree"], None),
        (["xprop", "-root", "WM_NAME"], None),
        (["xmodmap", "-pk"], None),
        (["xdotool", "mousemove", "10", "10"], None),
        (["xdotool", "mousedown", "1"], None),
        (["xdotool", "mouseup", "1"], None),
        (["xdotool", "type", "unsafe"], None),
        (["xdotool", "unknown"], None),
        (["xdotool", "keydown", "040", "keyup", "040"], "motion-key"),
        (["xdotool", "keydown", "w", "sleep", "0.010", "keyup", "w"], "motion-timeline"),
        (["xdotool", "sleep", "0.010", "mousemove", "10", "10"], "pointer-motion"),
        (["xdotool", "mousedown", "1", "sleep", "0.050", "mouseup", "3"], "single-click"),
        (
            ["xdotool", "mousedown", "3", "sleep", "0.010", "mousemove", "20", "20", "mouseup", "3"],
            "camera-drag",
        ),
        (["xdotool", "keydown", "040", "keydown", "041"], "keyboard-steps"),
        (["xdotool", "getwindowfocus"], "removed-raw-input-schema"),
    ],
)
def test_refused_forms_are_not_admitted(argv: list[str], schema: str | None) -> None:
    assert not classify(argv, schema).valid
