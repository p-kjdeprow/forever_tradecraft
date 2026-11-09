"""Start a game from Battle.net by clicking its Play button like a human.

After a fresh Battle.net start the embedded Chromium view has no keyboard
focus, ``focus play`` does not move it, and Return does nothing; Tab reaches the
logo menu (which contains Log Out), so blind keyboard navigation is unsafe.
The Play button is therefore located on a screenshot and clicked with the
shared seeded, curved, Fitts-timed pointer planner, the pinned XTEST emitter
and the canonical click-rate gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from crowley.platform.paths import XdgDirs
from crowley.session.executor import BindingMetadata, Point, PointerButton, PointerTarget, Rect
from crowley.session.input_tool import PinnedInputTool
from crowley.session.pointer_execution import (
    acquire_click_rate_permit,
    build_pointer_motion_chain,
    plan_human_pointer,
)

WINDOW_CLASS_PATTERN = r"^battle\.net\.exe$"
WINDOW_NAME = "Battle.net"
# Play button fill spans roughly RGB (20,140,255) to (0,116,224); the range is
# widened to also cover the lighter hover blue left by a pointer resting on it.
_BLUE_MAX_RED = 70
_BLUE_MIN_GREEN = 100
_BLUE_MAX_GREEN = 190
_BLUE_MIN_BLUE = 200
# Smaller blue regions (text antialiasing, icons) are not button candidates.
_MIN_COMPONENT_AREA = 200
# Play is about 230x55 at 1920x1080; the adjacent gear button is about 40 wide.
_MIN_WIDTH, _MAX_WIDTH = 150, 350
_MIN_HEIGHT, _MAX_HEIGHT = 35, 80
_MIN_FILL_RATIO = 0.6
_INNER_FRACTION = 0.6
# The label must read "Play": four glyphs (P, l, a, y), roughly centred.
_GLYPH_MIN_CHANNEL = 170
_GLYPH_MIN_AREA = 6
_PLAY_GLYPH_COUNT = 4
_LABEL_MIN_WIDTH_FRACTION, _LABEL_MAX_WIDTH_FRACTION = 0.10, 0.50
_LABEL_MAX_CENTER_OFFSET_FRACTION = 0.15
_COMMAND_TIMEOUT_SECONDS = 20.0


class BattleNetPlayError(RuntimeError):
    """The Play button could not be located or clicked."""


@dataclass(frozen=True, slots=True)
class Box:
    left: int
    top: int
    width: int
    height: int


def _blue_mask(crop: Image.Image) -> bytearray:
    data = crop.convert("RGB").tobytes()
    mask = bytearray(crop.width * crop.height)
    for index in range(len(mask)):
        red, green, blue = data[index * 3 : index * 3 + 3]
        if red <= _BLUE_MAX_RED and _BLUE_MIN_GREEN <= green <= _BLUE_MAX_GREEN and blue >= _BLUE_MIN_BLUE:
            mask[index] = 1
    return mask


def _components(mask: bytearray, width: int, height: int, *, diagonal: bool = False) -> list[tuple[int, Box]]:
    """Return (area, bounding box) of each 4-connected blue region."""

    found: list[tuple[int, Box]] = []
    for origin in range(len(mask)):
        if mask[origin] != 1:
            continue
        mask[origin] = 2
        queue = deque([origin])
        area = 0
        min_x = max_x = origin % width
        min_y = max_y = origin // width
        while queue:
            index = queue.pop()
            x, y = index % width, index // width
            area += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            neighbors = [
                (index - 1, x > 0),
                (index + 1, x < width - 1),
                (index - width, y > 0),
                (index + width, y < height - 1),
            ]
            if diagonal:
                neighbors += [
                    (index - width - 1, x > 0 and y > 0),
                    (index - width + 1, x < width - 1 and y > 0),
                    (index + width - 1, x > 0 and y < height - 1),
                    (index + width + 1, x < width - 1 and y < height - 1),
                ]
            for neighbor, valid in neighbors:
                if valid and mask[neighbor] == 1:
                    mask[neighbor] = 2
                    queue.append(neighbor)
        found.append((area, Box(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)))
    return found


def _verify_play_label(image: Image.Image, box: Box) -> None:
    """Require the button text to be the four-glyph, centred word "Play"."""

    crop = image.crop((box.left, box.top, box.left + box.width, box.top + box.height)).convert("RGB")
    data = crop.tobytes()
    mask = bytearray(box.width * box.height)
    for index in range(len(mask)):
        if min(data[index * 3 : index * 3 + 3]) >= _GLYPH_MIN_CHANNEL:
            mask[index] = 1
    glyphs = [
        glyph
        for area, glyph in _components(mask, box.width, box.height, diagonal=True)
        if area >= _GLYPH_MIN_AREA
    ]
    if len(glyphs) != _PLAY_GLYPH_COUNT:
        raise BattleNetPlayError(f"the blue button label has {len(glyphs)} glyphs, expected the 4 of 'Play'")
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_width = run_right - run_left
    if not _LABEL_MIN_WIDTH_FRACTION * box.width <= run_width <= _LABEL_MAX_WIDTH_FRACTION * box.width:
        raise BattleNetPlayError(f"the blue button label width {run_width} is implausible for 'Play'")
    if abs((run_left + run_right) / 2 - box.width / 2) > _LABEL_MAX_CENTER_OFFSET_FRACTION * box.width:
        raise BattleNetPlayError("the blue button label is not centred, so it is not 'Play'")


def locate_play_button(image: Image.Image, window: Box) -> Box:
    """Return the Play button box in image coordinates, or fail loudly.

    The search covers the bottom-left quarter of the window.  Exactly one blue
    region of plausible size must exist (the gear button beside Play is a
    separate, much narrower region).
    """

    left, top = max(window.left, 0), max(window.top + window.height // 2, 0)
    right = min(window.left + window.width // 2, image.width)
    bottom = min(window.top + window.height, image.height)
    if right <= left or bottom <= top:
        raise BattleNetPlayError("the Battle.net window's bottom-left quarter is outside the screenshot")
    crop = image.crop((left, top, right, bottom))
    regions = [
        (area, box)
        for area, box in _components(_blue_mask(crop), crop.width, crop.height)
        if area >= _MIN_COMPONENT_AREA
    ]
    if not regions:
        raise BattleNetPlayError("no blue Play button region found in the Battle.net window")
    plausible = [
        box
        for area, box in regions
        if _MIN_WIDTH <= box.width <= _MAX_WIDTH
        and _MIN_HEIGHT <= box.height <= _MAX_HEIGHT
        and area >= _MIN_FILL_RATIO * box.width * box.height
    ]
    if not plausible:
        sizes = ", ".join(f"{box.width}x{box.height}" for _, box in regions)
        raise BattleNetPlayError(f"blue regions have an implausible Play button size: {sizes}")
    if len(plausible) > 1:
        raise BattleNetPlayError(f"ambiguous Play button: {len(plausible)} plausible blue regions")
    box = plausible[0]
    located = Box(left + box.left, top + box.top, box.width, box.height)
    _verify_play_label(image, located)
    return located


def inner_target(box: Box) -> Box:
    """Return the inner 60% of a button box, centred."""

    width = max(1, int(box.width * _INNER_FRACTION))
    height = max(1, int(box.height * _INNER_FRACTION))
    return Box(box.left + (box.width - width) // 2, box.top + (box.height - height) // 2, width, height)


def _run(args: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=_COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BattleNetPlayError(f"{args[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        raise BattleNetPlayError(
            f"{' '.join(args)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def find_battle_net_window() -> int:
    """Find the one visible battle.net.exe-class window named exactly Battle.net."""

    try:
        found = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", WINDOW_CLASS_PATTERN],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BattleNetPlayError(f"xdotool search failed: {exc}") from exc
    matches = [
        int(candidate)
        for candidate in found.split()
        if _run(["xdotool", "getwindowname", candidate]).rstrip("\n") == WINDOW_NAME
    ]
    if len(matches) != 1:
        raise BattleNetPlayError(f"expected exactly one visible Battle.net window, found {len(matches)}")
    return matches[0]


def _shell_ints(text: str, names: Sequence[str]) -> dict[str, int]:
    values = dict(re.findall(r"^([A-Z]+)=(-?\d+)$", text, flags=re.MULTILINE))
    missing = [name for name in names if name not in values]
    if missing:
        raise BattleNetPlayError(f"xdotool output lacks {', '.join(missing)}")
    return {name: int(values[name]) for name in names}


def window_geometry(window: int) -> Box:
    values = _shell_ints(
        _run(["xdotool", "getwindowgeometry", "--shell", str(window)]), ("X", "Y", "WIDTH", "HEIGHT")
    )
    return Box(values["X"], values["Y"], values["WIDTH"], values["HEIGHT"])


def _display_size() -> tuple[int, int]:
    parts = _run(["xdotool", "getdisplaygeometry"]).split()
    if len(parts) != 2 or not all(part.isdecimal() for part in parts):
        raise BattleNetPlayError("xdotool getdisplaygeometry output is malformed")
    return int(parts[0]), int(parts[1])


def _tool_run(tool: PinnedInputTool, args: Sequence[str], timeout: float) -> str:
    """Run the pinned XTEST emitter (the driver's input path) with one argv."""

    completed = subprocess.run(
        [tool.proc_path, *args],
        capture_output=True,
        text=True,
        check=False,
        pass_fds=(tool.image.fd,),
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise BattleNetPlayError(f"pointer emitter exited {completed.returncode}: {completed.stderr.strip()}")
    return completed.stdout


def _pointer_position(tool: PinnedInputTool) -> tuple[int, int, int]:
    """Return (x, y, window) from the pinned tool's getmouselocation, like the driver."""

    values: dict[str, int] = {}
    for line in _tool_run(tool, ["getmouselocation", "--shell"], _COMMAND_TIMEOUT_SECONDS).splitlines():
        key, separator, value = line.strip().partition("=")
        if key not in {"X", "Y", "SCREEN", "WINDOW"} or not separator or key in values:
            raise BattleNetPlayError("pointer position output is malformed")
        try:
            values[key] = int(value, 0) if key == "WINDOW" else int(value)
        except ValueError as exc:
            raise BattleNetPlayError("pointer position output is malformed") from exc
    if set(values) != {"X", "Y", "SCREEN", "WINDOW"} or values["SCREEN"] != 0 or values["WINDOW"] <= 0:
        raise BattleNetPlayError("pointer position output is incomplete")
    return values["X"], values["Y"], values["WINDOW"]


def _is_window_or_descendant(xid: int, ancestor: int) -> bool:
    """Walk X parents upward from xid; true when ancestor is reached before the root."""

    current = xid
    for _ in range(16):
        if current == ancestor:
            return True
        tree = _run(["xwininfo", "-id", hex(current), "-tree"])
        parent = re.search(r"Parent window id: (0x[0-9a-fA-F]+)", tree)
        root = re.search(r"Root window id: (0x[0-9a-fA-F]+)", tree)
        if parent is None or root is None or int(parent.group(1), 16) == int(root.group(1), 16):
            return False
        current = int(parent.group(1), 16)
    return False


def _capture(directory: Path, display: tuple[int, int]) -> Image.Image:
    """Capture under the driver's cross-process screenshot lock; wait for a complete PNG."""

    from crowley.session.driver import _capture_lock, _wait_for_complete_png

    path = directory / "battle-net.png"
    deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
    with _capture_lock(deadline):
        _run(["gnome-screenshot", "-f", str(path)])
        _wait_for_complete_png(path, display, deadline)
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    if image.size != display:
        raise BattleNetPlayError(f"screenshot is {image.size}, expected the display size {display}")
    path.unlink()
    return image


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(evidence, sort_keys=True))


def click_play(window: int | None = None) -> Path:
    """Locate Play on a fresh capture and click it; return the evidence file.

    Mirrors the session driver's pointer protocol: plan from the pointer read
    inside the held click permit, emit the motion chain alone, verify the
    pointer endpoint and target window, precommit, then press and release as a
    separate emission, then record the press.
    """

    if window is None:
        window = find_battle_net_window()
    for name in ("DISPLAY", "XAUTHORITY"):
        if not os.environ.get(name):
            raise BattleNetPlayError(f"{name} must be set for the graphical session")
    dirs = XdgDirs.from_environ(os.environ)
    dirs.crowley_runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence_dir = dirs.crowley_state / "executor-evidence"
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    geometry = window_geometry(window)
    display = _display_size()
    with tempfile.TemporaryDirectory(prefix="battle-net-play-", dir=dirs.crowley_runtime) as scratch:
        image = _capture(Path(scratch), display)
    button = locate_play_button(image, geometry)
    inner = inner_target(button)

    # Plan in screen coordinates so a pointer outside the window still starts
    # on the planned path instead of being warped onto it.
    screen = Rect(0, 0, display[0], display[1])
    capture_id = f"battle-net-play-{time.monotonic_ns()}"
    binding = BindingMetadata("battle-net-play", capture_id, 1, 1, window, (window,))
    target = PointerTarget(
        "battle-net-play", Rect(inner.left, inner.top, inner.width, inner.height), binding, padding=1
    )
    evidence: dict[str, object] = {
        "schema": "crowley.battle-net-play.v1",
        "window": window,
        "windowGeometry": [geometry.left, geometry.top, geometry.width, geometry.height],
        "button": [button.left, button.top, button.width, button.height],
        "clickTarget": [inner.left, inner.top, inner.width, inner.height],
        "phase": "located",
    }
    evidence_path = evidence_dir / f"{capture_id}.json"
    _write_evidence(evidence_path, evidence)

    tool = PinnedInputTool.load()
    try:
        with acquire_click_rate_permit() as permit:
            start_x, start_y, _start_window = _pointer_position(tool)
            plan = plan_human_pointer(Point(start_x, start_y), target, screen, button=PointerButton.LEFT)
            if plan.click is None:
                raise BattleNetPlayError("the pointer plan carries no click")
            chain = build_pointer_motion_chain(plan.trajectory, (0, 0, display[0], display[1]))
            first = plan.trajectory.samples[0].point
            endpoint = (int(plan.endpoint.x), int(plan.endpoint.y))
            evidence.update(
                {
                    "phase": "planned",
                    "seed": plan.seed,
                    "start": [start_x, start_y],
                    "endpoint": list(endpoint),
                    "waypoints": [[point.x, point.y] for point in plan.waypoints],
                    "movementDuration": plan.movement_duration,
                    "dwell": plan.click.dwell,
                    "samples": [[s.at, s.point.x, s.point.y] for s in plan.trajectory.samples],
                    "motionArgv": chain[1:],
                }
            )
            _write_evidence(evidence_path, evidence)
            # The motion chain never warps to the first sample, so the pointer
            # must still be exactly there.
            if _pointer_position(tool)[:2] != (int(first.x), int(first.y)):
                raise BattleNetPlayError("pointer start changed before the planned motion")
            _tool_run(tool, chain[1:], plan.movement_duration + _COMMAND_TIMEOUT_SECONDS)
            end_x, end_y, end_window = _pointer_position(tool)
            if (end_x, end_y) != endpoint:
                raise BattleNetPlayError("pointer is not at the planned endpoint after motion")
            if not _is_window_or_descendant(end_window, window):
                raise BattleNetPlayError("pointer is not over the Battle.net window")
            click_args = ["mousedown", "1", "sleep", f"{plan.click.dwell:.9f}", "mouseup", "1"]
            evidence["precommitMonotonicNs"] = permit.precommit()
            evidence["phase"] = "precommitted"
            try:
                _tool_run(tool, click_args, plan.click.dwell + _COMMAND_TIMEOUT_SECONDS)
                evidence["pressMonotonicNs"] = permit.record_press()
                evidence["phase"] = "pressed"
            except BaseException:
                try:
                    _tool_run(tool, ["mouseup", "1"], _COMMAND_TIMEOUT_SECONDS)
                except (BattleNetPlayError, OSError, subprocess.TimeoutExpired):
                    pass
                raise
    finally:
        tool.close()
        _write_evidence(evidence_path, evidence)
    return evidence_path
