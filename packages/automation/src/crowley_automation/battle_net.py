"""Battle.net UI recognition and guarded startup-control helpers.

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
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from crowley_common.platform.paths import XdgDirs
from PIL import Image

from crowley_automation.gui.click_gate import acquire_click_rate_permit
from crowley_automation.gui.input_tool import PinnedInputTool
from crowley_automation.gui.pointer import (
    BindingMetadata,
    Point,
    PointerButton,
    PointerTarget,
    Rect,
    build_pointer_motion_chain,
    plan_human_pointer,
)
from crowley_automation.startup_ui import (
    MIN_CONTROL_CONFIDENCE,
    StartupUIError,
    TesseractReader,
    TextWord,
    line_texts,
)

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
# The label must be a single exact Tesseract word on one line, roughly centred.
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


def locate_play_button(
    image: Image.Image,
    window: Box,
    *,
    words: Sequence[TextWord] | None = None,
    captured_monotonic: float | None = None,
) -> Box:
    """Return the Play button box in image coordinates, or fail loudly.

    The search covers the bottom-left quarter of the window. Exactly one blue
    region of plausible size must contain the exact OCR line ``Play``. Four
    connected glyphs alone are not accepted as evidence.
    """

    if words is None:
        try:
            words = TesseractReader().read(image, captured_monotonic=captured_monotonic).words
        except StartupUIError as error:
            raise BattleNetPlayError("Tesseract could not verify the Battle.net button label") from error

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
    # Other blue controls (a "Play Now" banner button, dialog buttons) can share the
    # quarter; only a region whose label reads "Play" counts.  Live 2026-11-09: two
    # plausible regions made a relaunch fail and cost a 15 min backoff.
    labelled: list[Box] = []
    lines = line_texts(words, minimum_confidence=MIN_CONTROL_CONFIDENCE)
    for box in plausible:
        located = Box(left + box.left, top + box.top, box.width, box.height)
        label_words = [
            word
            for word in words
            if word.confidence >= MIN_CONTROL_CONFIDENCE
            and word.line in lines
            and lines[word.line].casefold() == "play"
            and located.left <= word.left
            and word.right <= located.left + located.width
            and located.top <= word.top
            and word.bottom <= located.top + located.height
        ]
        if len(label_words) == 1 and _centered_play_label(label_words[0], located.width, located.left):
            labelled.append(located)
            continue
        button_crop = image.crop(
            (located.left, located.top, located.left + located.width, located.top + located.height)
        ).resize((located.width * 3, located.height * 3), Image.Resampling.LANCZOS)
        try:
            local_words = TesseractReader().read(button_crop, captured_monotonic=captured_monotonic).words
        except StartupUIError:
            continue
        local_lines = line_texts(local_words, minimum_confidence=MIN_CONTROL_CONFIDENCE)
        exact = [
            word
            for word in local_words
            if word.confidence >= MIN_CONTROL_CONFIDENCE
            and word.line in local_lines
            and local_lines[word.line].casefold() == "play"
        ]
        if len(exact) == 1 and _centered_play_label(exact[0], button_crop.width, 0):
            labelled.append(located)
    if not labelled:
        raise BattleNetPlayError("no blue region contains the exact centered Play label")
    if len(labelled) > 1:
        raise BattleNetPlayError(f"ambiguous Play button: {len(labelled)} blue regions labelled Play")
    return labelled[0]


def _centered_play_label(word: TextWord, width: int, offset: int) -> bool:
    if word.text.casefold() != "play":
        return False
    if not _LABEL_MIN_WIDTH_FRACTION * width <= word.width <= _LABEL_MAX_WIDTH_FRACTION * width:
        return False
    return abs(word.center[0] - (width / 2 + offset)) <= _LABEL_MAX_CENTER_OFFSET_FRACTION * width


def inner_target(box: Box) -> Box:
    """Return the inner 60% of a button box, centred."""

    width = max(1, int(box.width * _INNER_FRACTION))
    height = max(1, int(box.height * _INNER_FRACTION))
    return Box(box.left + (box.width - width) // 2, box.top + (box.height - height) // 2, width, height)


def _run(args: Sequence[str], *, env: Mapping[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            env=None if env is None else dict(env),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BattleNetPlayError(f"{args[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        raise BattleNetPlayError(
            f"{' '.join(args)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _shell_ints(text: str, names: Sequence[str]) -> dict[str, int]:
    values = dict(re.findall(r"^([A-Z]+)=(-?\d+)$", text, flags=re.MULTILINE))
    missing = [name for name in names if name not in values]
    if missing:
        raise BattleNetPlayError(f"xdotool output lacks {', '.join(missing)}")
    return {name: int(values[name]) for name in names}


def window_geometry(window: int, *, env: Mapping[str, str] | None = None) -> Box:
    values = _shell_ints(
        _run(["xdotool", "getwindowgeometry", "--shell", str(window)], env=env),
        ("X", "Y", "WIDTH", "HEIGHT"),
    )
    return Box(values["X"], values["Y"], values["WIDTH"], values["HEIGHT"])


def _is_window_or_descendant(xid: int, ancestor: int, *, env: Mapping[str, str] | None = None) -> bool:
    """Walk X parents upward from xid; true when ancestor is reached before the root."""

    current = xid
    for _ in range(16):
        if current == ancestor:
            return True
        tree = _run(["xwininfo", "-id", hex(current), "-tree"], env=env)
        parent = re.search(r"Parent window id: (0x[0-9a-fA-F]+)", tree)
        root = re.search(r"Root window id: (0x[0-9a-fA-F]+)", tree)
        if parent is None or root is None or int(parent.group(1), 16) == int(root.group(1), 16):
            return False
        current = int(parent.group(1), 16)
    return False


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(evidence, sort_keys=True))


def click_control(
    window: int,
    control: Box,
    display: tuple[int, int],
    *,
    tool: PinnedInputTool,
    control_run: Callable[..., subprocess.CompletedProcess[str]],
    environment: Callable[[], Mapping[str, str]],
    guard: Callable[[], None],
    revalidate: Callable[[], Box | None],
    action: str,
) -> Path:
    """Human-click one freshly classified control through the parent-death runner."""

    if action not in {"play", "news_close", "enter_world"}:
        raise BattleNetPlayError("startup click action is not allowlisted")
    current_env = dict(environment())
    geometry = window_geometry(window, env=current_env)
    if geometry.width <= 0 or geometry.height <= 0 or display[0] <= 0 or display[1] <= 0:
        raise BattleNetPlayError("startup window geometry is invalid")
    if control.width <= 0 or control.height <= 0:
        raise BattleNetPlayError("startup control geometry is invalid")

    center = inner_target(control)
    screen = Rect(0, 0, display[0], display[1])
    capture_id = f"startup-{action}-{time.monotonic_ns()}"
    binding = BindingMetadata(f"startup-{action}", capture_id, 1, 1, window, (window,))
    target = PointerTarget(
        f"startup-{action}",
        Rect(center.left, center.top, center.width, center.height),
        binding,
        padding=1,
    )
    dirs = XdgDirs.from_environ(os.environ)
    dirs.crowley_runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence_dir = dirs.crowley_state / "executor-evidence"
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence: dict[str, object] = {
        "schema": "crowley.startup-click.v1",
        "action": action,
        "window": window,
        "windowGeometry": [geometry.left, geometry.top, geometry.width, geometry.height],
        "control": [control.left, control.top, control.width, control.height],
        "clickTarget": [center.left, center.top, center.width, center.height],
        "phase": "located",
    }
    evidence_path = evidence_dir / f"{capture_id}.json"
    _write_evidence(evidence_path, evidence)

    def emit(args: Sequence[str], timeout: float) -> str:
        done = control_run(
            [tool.proc_path, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=current_env,
            pass_fds=(tool.image.fd,),
        )
        if done.returncode != 0:
            raise BattleNetPlayError("pinned XTEST control command failed")
        return done.stdout

    with acquire_click_rate_permit(cancellation_check=guard) as permit:
        location = _shell_ints(
            emit(["getmouselocation", "--shell"], _COMMAND_TIMEOUT_SECONDS), ("X", "Y", "SCREEN", "WINDOW")
        )
        if location["SCREEN"] != 0 or location["WINDOW"] <= 0:
            raise BattleNetPlayError("pointer position is not attached to the active X screen")
        plan = plan_human_pointer(
            Point(location["X"], location["Y"]), target, screen, button=PointerButton.LEFT
        )
        if plan.click is None:
            raise BattleNetPlayError("startup pointer plan has no click")
        chain = build_pointer_motion_chain(plan.trajectory, (0, 0, display[0], display[1]))
        first = plan.trajectory.samples[0].point
        endpoint = (int(plan.endpoint.x), int(plan.endpoint.y))
        evidence.update(
            {
                "phase": "planned",
                "seed": plan.seed,
                "start": [location["X"], location["Y"]],
                "endpoint": list(endpoint),
                "waypoints": [[point.x, point.y] for point in plan.waypoints],
                "movementDuration": plan.movement_duration,
                "dwell": plan.click.dwell,
                "samples": [
                    [sample.at, sample.point.x, sample.point.y] for sample in plan.trajectory.samples
                ],
                "motionArgv": chain[1:],
            }
        )
        _write_evidence(evidence_path, evidence)
        if _pointer_position_with_runner(tool, control_run, current_env)[:2] != (int(first.x), int(first.y)):
            raise BattleNetPlayError("pointer start changed before startup motion")
        guard()
        emit(chain[1:], plan.movement_duration + _COMMAND_TIMEOUT_SECONDS)
        end_x, end_y, end_window = _pointer_position_with_runner(tool, control_run, current_env)
        if (end_x, end_y) != endpoint or not _is_window_or_descendant(end_window, window, env=current_env):
            raise BattleNetPlayError("pointer did not reach the verified startup control")
        if window_geometry(window, env=current_env) != geometry:
            raise BattleNetPlayError("startup window changed after pointer motion")
        fresh_control = revalidate()
        if fresh_control is None or not _safe_control_revalidation(control, fresh_control, endpoint):
            raise BattleNetPlayError("startup control changed after pointer motion")
        guard()
        evidence["precommitMonotonicNs"] = permit.precommit()
        evidence["phase"] = "precommitted"
        try:
            guard()
            emit(
                ["mousedown", "1", "sleep", f"{plan.click.dwell:.9f}", "mouseup", "1"],
                plan.click.dwell + _COMMAND_TIMEOUT_SECONDS,
            )
            evidence["pressMonotonicNs"] = permit.record_press()
            evidence["phase"] = "pressed"
        except BaseException:
            cleanup = getattr(getattr(control_run, "__self__", None), "release_only", None)
            if callable(cleanup):
                cleanup()
            else:
                try:
                    emit(["mouseup", "1"], _COMMAND_TIMEOUT_SECONDS)
                except (BattleNetPlayError, OSError, subprocess.TimeoutExpired):
                    pass
            raise
    _write_evidence(evidence_path, evidence)
    return evidence_path


def _safe_control_revalidation(original: Box, fresh: Box, endpoint: tuple[int, int]) -> bool:
    """Allow only tiny hover OCR shifts while keeping the planned endpoint safe."""

    if min(original.width, original.height, fresh.width, fresh.height) <= 0:
        return False
    original_right = original.left + original.width
    original_bottom = original.top + original.height
    fresh_right = fresh.left + fresh.width
    fresh_bottom = fresh.top + fresh.height
    if (
        max(
            abs(original.left - fresh.left),
            abs(original.top - fresh.top),
            abs(original_right - fresh_right),
            abs(original_bottom - fresh_bottom),
        )
        > 8
    ):
        return False
    intersection_width = max(0, min(original_right, fresh_right) - max(original.left, fresh.left))
    intersection_height = max(0, min(original_bottom, fresh_bottom) - max(original.top, fresh.top))
    smaller_area = min(original.width * original.height, fresh.width * fresh.height)
    if intersection_width * intersection_height < smaller_area * 0.8:
        return False
    safe = inner_target(fresh)
    return (
        safe.left <= endpoint[0] < safe.left + safe.width and safe.top <= endpoint[1] < safe.top + safe.height
    )


def _pointer_position_with_runner(
    tool: PinnedInputTool,
    control_run: Callable[..., subprocess.CompletedProcess[str]],
    environment: Mapping[str, str],
) -> tuple[int, int, int]:
    done = control_run(
        [tool.proc_path, "getmouselocation", "--shell"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        env=dict(environment),
        pass_fds=(tool.image.fd,),
    )
    if done.returncode != 0:
        raise BattleNetPlayError("pinned pointer query failed")
    values = _shell_ints(done.stdout, ("X", "Y", "SCREEN", "WINDOW"))
    if values["SCREEN"] != 0 or values["WINDOW"] <= 0:
        raise BattleNetPlayError("pointer query is not attached to the active X screen")
    return values["X"], values["Y"], values["WINDOW"]
