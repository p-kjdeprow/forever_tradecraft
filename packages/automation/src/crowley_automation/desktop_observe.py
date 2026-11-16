"Owned process, window and screenshot observation primitives."

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from crowley_automation.gui import capture as gui_capture
from crowley_automation.gui.process import OwnedGameMatcher

COMMAND_TIMEOUT_S = 5.0


class ObserveError(RuntimeError):
    """A source could not be read; the tick reports a fault and sends no input."""


# --- process ------------------------------------------------------------------------------


class ProcessSource(Protocol):
    def pids(self) -> list[int]: ...

    def started_at(self, pids: list[int]) -> float:
        """Wall-clock start time of the oldest of *pids*."""
        ...


def _boot_time() -> float:
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise ObserveError("/proc/stat has no btime")


def process_start_time(pid: int) -> float:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError as exc:
        raise ObserveError(f"process {pid} vanished: {exc}") from exc
    fields = stat_text.rsplit(")", 1)[1].split()
    return _boot_time() + int(fields[19]) / os.sysconf("SC_CLK_TCK")


class ProcProcessSource:
    def __init__(self, matcher: OwnedGameMatcher) -> None:
        self._matcher = matcher

    def pids(self) -> list[int]:
        return sorted(self._matcher.pids())

    def started_at(self, pids: list[int]) -> float:
        return min(process_start_time(pid) for pid in pids)


# --- window -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowState:
    xid: int | None  # the one game window, if exactly one exists
    count: int
    ok: bool  # exactly one visible window at the display origin and size
    focused: bool
    display: tuple[int, int]
    detail: str = ""


ABSENT_WINDOW = WindowState(None, 0, False, False, (0, 0), "no process")


type X11Run = Callable[[list[str]], tuple[int, str]]
_CHILD = re.compile(r"^\s+(0x[0-9a-fA-F]+)\s.*?\s(\d+)x(\d+)[+-]\d+[+-]\d+\s", re.MULTILINE)


class WindowSource(Protocol):
    def observe(self, pids: list[int]) -> WindowState:
        """The one visible game window owned by *pids*."""
        ...


def x11_runner(env: dict[str, str]) -> X11Run:
    def run(argv: list[str]) -> tuple[int, str]:
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT_S, env=env
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObserveError(f"{argv[0]} failed: {exc}") from exc
        return done.returncode, done.stdout

    return run


def x11_environment(display: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    xauthority = source.get("XAUTHORITY")
    if not xauthority:
        raise ObserveError("XAUTHORITY is not set")
    return {**source, "DISPLAY": display, "XAUTHORITY": xauthority}


class X11Window:
    """Exactly one visible game window (class, title and _NET_WM_PID of an owned process),
    at the display origin and size with exactly one child at display size; and who has focus."""

    def __init__(self, window_class: str, window_title: str, run: X11Run) -> None:
        self._pattern = f"^{re.escape(window_class)}$"
        self._title = window_title
        self._run = run

    def _must(self, argv: list[str]) -> str:
        code, out = self._run(argv)
        if code != 0:
            raise ObserveError(f"{' '.join(argv[:2])} exited {code}")
        return out

    def _owned(self, xid: int, pids: list[int]) -> bool:
        code, title = self._run(["xdotool", "getwindowname", str(xid)])
        if code != 0 or title.strip() != self._title:
            return False
        code, pid = self._run(["xdotool", "getwindowpid", str(xid)])
        return code == 0 and pid.strip().isdigit() and int(pid) in pids

    def _children(self, xid: int) -> list[tuple[int, tuple[int, int]]]:
        tree = self._must(["xwininfo", "-id", hex(xid), "-children"])
        return [(int(m[0], 16), (int(m[1]), int(m[2]))) for m in _CHILD.findall(tree)]

    def observe(self, pids: list[int]) -> WindowState:
        code, out = self._run(["xdotool", "search", "--onlyvisible", "--class", self._pattern])
        if code not in (0, 1):  # 1 = no match
            raise ObserveError(f"xdotool search exited {code}")
        candidates = [int(line) for line in out.split()] if code == 0 else []
        ids = [xid for xid in candidates if self._owned(xid, pids)]
        size = self._must(["xdotool", "getdisplaygeometry"]).split()
        display = (int(size[0]), int(size[1]))
        if len(ids) != 1:
            return WindowState(None, len(ids), False, False, display, f"window_count={len(ids)}")
        xid = ids[0]
        geometry: dict[str, int] = {}
        for line in self._must(["xdotool", "getwindowgeometry", "--shell", str(xid)]).splitlines():
            key, _, value = line.partition("=")
            if key in ("X", "Y", "WIDTH", "HEIGHT"):
                geometry[key] = int(value)
        rect = (geometry.get("X"), geometry.get("Y"), geometry.get("WIDTH"), geometry.get("HEIGHT"))
        children = self._children(xid)
        problems = []
        if rect != (0, 0, display[0], display[1]):
            problems.append(f"geometry={rect} display={display}")
        if len(children) != 1 or children[0][1] != display:
            problems.append(f"children={[size for _id, size in children]}")
        focused = self._focused(xid, [child for child, _size in children])
        return WindowState(xid, 1, not problems, focused, display, ",".join(problems))

    def _focused(self, xid: int, children: list[int]) -> bool:
        code, out = self._run(["xdotool", "getwindowfocus"])
        foreground = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
        if foreground == 0:
            code, out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
            match = re.search(r"#\s*(0x[0-9a-fA-F]+)", out) if code == 0 else None
            foreground = int(match.group(1), 16) if match else 0
        return foreground != 0 and (foreground == xid or foreground in children)


# --- canvas -------------------------------------------------------------------------------


class CanvasSource(Protocol):
    def capture(self, display: tuple[int, int]) -> Any:
        """A fresh full screenshot as a PIL RGB image."""
        ...


class ScreenCanvas:
    def __init__(self, scratch_dir: Path) -> None:
        self._scratch = scratch_dir

    def capture(self, display: tuple[int, int]) -> Any:
        from PIL import Image

        self._scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="scan-", dir=self._scratch) as scratch:
            path = Path(scratch) / "screen.png"
            gui_capture.capture_screen(path, display)
            with Image.open(path) as opened:
                image = opened.convert("RGB")
        if image.size != display:
            raise ObserveError(f"screenshot is {image.size}, expected the display size {display}")
        return image


def crop_canvas(image: Any, display: tuple[int, int], crop: tuple[int, int, int, int], name: str) -> Any:
    """Extract one fixed status canvas from a full screenshot."""
    if image.size != display:
        raise ObserveError(f"screenshot is {image.size}, expected the display size {display}")
    left, top, width, height = crop
    if (
        left < 0
        or top < 0
        or width != 256
        or height != 256
        or left + width > image.width
        or top + height > image.height
    ):
        raise ObserveError(f"{name} crop lies outside the screenshot")
    return image.crop((left, top, left + width, top + height))


# --- snapshot mapping ---------------------------------------------------------------------
