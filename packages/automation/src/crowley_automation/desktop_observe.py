"Owned process, window and screenshot observation primitives."

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from crowley_common.frames import MAGIC, PixelDecoder, ProtocolError

from crowley_automation.config import AutomationConfig
from crowley_automation.gui import capture as gui_capture
from crowley_automation.gui.process import OwnedGameMatcher, ProcessIdentity

from .desktop_session import DesktopSessionProvider

COMMAND_TIMEOUT_S = 5.0
MAX_FRAME_AGE_SECONDS = 3.0
FUTURE_FRAME_TOLERANCE_SECONDS = 1.0


class ObserveError(RuntimeError):
    """A source could not be read; the tick reports a fault and sends no input."""


@dataclass(frozen=True, slots=True)
class NativeWorldObservation:
    """One fresh automation publication bound to the exact current process/window."""

    identity: ProcessIdentity
    window: WindowState
    snapshot: dict[str, Any] | None
    observed_wall: float
    session_fingerprint: str


def has_frame_signature(canvas: object) -> bool:
    """Check the magic in the first 32 physical status cells."""

    try:
        width, height = canvas.size  # type: ignore[attr-defined]
        if (width, height) != (256, 256):
            raise ObserveError("automation canvas crop is not 256x256")
        for byte_index, value in enumerate(MAGIC):
            for bit_index in range(8):
                cell = byte_index * 8 + bit_index
                left, top = (cell % 128) * 2, (cell // 128) * 2
                for dy in range(2):
                    for dx in range(2):
                        red, green, blue, *_ = canvas.getpixel((left + dx, top + dy))  # type: ignore[attr-defined]
                        luminance = (299 * red + 587 * green + 114 * blue) // 1000
                        expected = 255 if (value >> bit_index) & 1 else 0
                        if luminance != expected:
                            return False
        return True
    except ObserveError:
        raise
    except (AttributeError, IndexError, TypeError, ValueError, OSError) as error:
        raise ObserveError("could not inspect automation canvas header") from error


def observe_native_world(
    config: AutomationConfig,
    *,
    session_provider: DesktopSessionProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> NativeWorldObservation:
    """Take one fresh, independently decoded automation-world observation."""

    if session_provider is None:
        try:
            environment = x11_environment(config.display)
            session_fingerprint = "inherited-session"
        except ObserveError as error:
            raise ObserveError("desktop session is unavailable") from error

        def env_provider() -> Mapping[str, str]:
            return environment

    else:
        session = session_provider.current()
        environment = dict(session.environment)
        session_fingerprint = session.fingerprint

        def env_provider() -> Mapping[str, str]:
            return dict(session_provider.current().environment)

    matcher = OwnedGameMatcher.for_game(
        game_executable=config.game_executable,
        game_argv0=config.game_argv0,
        wine_prefix=config.wine_prefix,
    )
    identities = matcher.identities()
    if len(identities) != 1:
        raise ObserveError("owned game process is missing or ambiguous")
    identity = identities[0]
    source = X11Window(config.window_class, config.window_title, x11_runner(env_provider))
    window = source.observe([identity.pid])
    if not window.ok or not window.fullscreen or window.xid is None:
        raise ObserveError("game window is not verified fullscreen")
    began = clock()
    snapshot: dict[str, Any] | None = None
    capture_started_wall = 0.0

    def verify_observation_owner() -> WindowState:
        current_identities = matcher.identities()
        current_window = source.observe([identity.pid]) if current_identities == (identity,) else None
        if (
            current_identities != (identity,)
            or current_window is None
            or not current_window.ok
            or not current_window.fullscreen
            or current_window.xid != window.xid
            or clock() - began > MAX_FRAME_AGE_SECONDS
        ):
            raise ObserveError("process, window, or original capture changed during observation")
        if session_provider is not None and session_provider.current().fingerprint != session_fingerprint:
            raise ObserveError("desktop session changed during observation")
        return current_window

    for capture_attempt in range(2):
        capture_started_monotonic = clock()
        capture_started_wall = wall_clock()
        try:
            from crowley_common.platform.paths import XdgDirs

            dirs = XdgDirs.from_environ(os.environ)
            image = ScreenCanvas(dirs.crowley_runtime / "automation-capture", env_provider).capture(
                window.display
            )
        except (OSError, ValueError) as error:
            raise ObserveError("desktop capture failed") from error
        capture_finished_wall = wall_clock()
        canvas = crop_canvas(image, window.display, config.canvas_crop, "automation canvas")
        if has_frame_signature(canvas):
            decoder = PixelDecoder(expected_kind="automation", crop=config.canvas_crop)
            try:
                sample = decoder.decode(
                    canvas,
                    monotonic=capture_started_monotonic,
                    window_identity=f"{identity.pid}:{identity.start_ticks}:{window.xid}",
                )
            except ProtocolError as error:
                raise ObserveError("automation canvas frame is corrupt") from error
            if sample.frame is not None:
                candidate = sample.frame.snapshot.value
                context = candidate["context"]
                expected = {
                    "character": config.character,
                    "realm": config.realm,
                    "playerFaction": config.faction,
                    "clientBuild": config.build.rsplit(".", 1)[1],
                    "interface": config.interface,
                    "nativeRegion": config.native_region,
                }
                if any(context[key] != value for key, value in expected.items()):
                    raise ObserveError("automation publication identity does not match configuration")
                wall_time = candidate["wallTime"]
                too_old = wall_time < capture_finished_wall - MAX_FRAME_AGE_SECONDS
                too_future = wall_time > capture_finished_wall + FUTURE_FRAME_TOLERANCE_SECONDS
                if too_old or too_future:
                    if too_old and capture_attempt == 0:
                        verify_observation_owner()
                        continue
                    age = max(-999.999, min(999.999, capture_finished_wall - wall_time))
                    capture_elapsed = max(0.0, min(999.999, capture_finished_wall - capture_started_wall))
                    raise ObserveError(
                        "automation publication is stale or future-dated "
                        f"(age={age:.3f}s, capture={capture_elapsed:.3f}s)"
                    )
                snapshot = candidate
        break

    current_window = verify_observation_owner()
    return NativeWorldObservation(
        identity, current_window, snapshot, capture_started_wall, session_fingerprint
    )


# --- process ------------------------------------------------------------------------------


class ProcessSource(Protocol):
    def pids(self) -> list[int]: ...

    def identities(self) -> tuple[ProcessIdentity, ...]: ...

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

    def identities(self) -> tuple[ProcessIdentity, ...]:
        return self._matcher.identities()

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
    outer_rect: tuple[int, int, int, int] | None = None
    client_rect: tuple[int, int, int, int] | None = None
    fullscreen: bool = False
    visible: bool = True
    foreign_count: int = 0


ABSENT_WINDOW = WindowState(None, 0, False, False, (0, 0), "no process", visible=False)


type X11Run = Callable[[list[str]], tuple[int, str]]
type EnvironmentProvider = Mapping[str, str] | Callable[[], Mapping[str, str]]
_CHILD = re.compile(r"^\s+(0x[0-9a-fA-F]+)\s.*?\s(\d+)x(\d+)([+-]\d+)([+-]\d+)\s", re.MULTILINE)


class WindowSource(Protocol):
    def focus_state(self, xid: int, pid: int) -> tuple[bool, bool]:
        """Verify a previously admitted window's owner and current keyboard focus."""
        ...

    def observe(self, pids: list[int], *, include_hidden: bool = False) -> WindowState:
        """The one visible game window owned by *pids*."""
        ...


def x11_runner(env: EnvironmentProvider) -> X11Run:
    def run(argv: list[str]) -> tuple[int, str]:
        current_env = dict(env() if callable(env) else env)
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_S,
                env=current_env,
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
        title_matches, owned = self._candidate_identity(xid, pids)
        return title_matches and owned

    def focus_state(self, xid: int, pid: int) -> tuple[bool, bool]:
        """Check keyboard delivery without repeating display/geometry discovery."""

        if not self._owned(xid, [pid]):
            return False, False
        children = self._children(xid)
        return True, self._focused(xid, [child for child, _rect in children])

    def _candidate_identity(self, xid: int, pids: list[int]) -> tuple[bool, bool]:
        code, title = self._run(["xdotool", "getwindowname", str(xid)])
        if code != 0 or title.strip() != self._title:
            return False, False
        code, pid = self._run(["xdotool", "getwindowpid", str(xid)])
        return True, code == 0 and pid.strip().isdigit() and int(pid) in pids

    def _children(self, xid: int) -> list[tuple[int, tuple[int, int, int, int]]]:
        tree = self._must(["xwininfo", "-id", hex(xid), "-children"])
        return [
            (
                int(match[0], 16),
                (int(match[3]), int(match[4]), int(match[1]), int(match[2])),
            )
            for match in _CHILD.findall(tree)
        ]

    def observe(self, pids: list[int], *, include_hidden: bool = False) -> WindowState:
        search = ["xdotool", "search"]
        if not include_hidden:
            search.append("--onlyvisible")
        search.extend(("--class", self._pattern))
        code, out = self._run(search)
        if code not in (0, 1):  # 1 = no match
            raise ObserveError(f"xdotool search exited {code}")
        candidates = [int(line) for line in out.split()] if code == 0 else []
        ids: list[int] = []
        foreign = 0
        for candidate in candidates:
            title_matches, owned = self._candidate_identity(candidate, pids)
            if title_matches:
                if owned:
                    if include_hidden and self._is_unmapped_override_redirect(candidate):
                        continue
                    ids.append(candidate)
                else:
                    foreign += 1
        size = self._must(["xdotool", "getdisplaygeometry"]).split()
        display = (int(size[0]), int(size[1]))
        total = len(ids) + foreign
        if total != 1 or foreign:
            detail = "foreign_window_candidate" if foreign else f"window_count={total}"
            return WindowState(
                None,
                total,
                False,
                False,
                display,
                detail,
                visible=False,
                foreign_count=foreign,
            )
        xid = ids[0]
        visible = True if not include_hidden else self._viewable(xid)
        geometry: dict[str, int] = {}
        for line in self._must(["xdotool", "getwindowgeometry", "--shell", str(xid)]).splitlines():
            key, _, value = line.partition("=")
            if key in ("X", "Y", "WIDTH", "HEIGHT"):
                geometry[key] = int(value)
        rect = (
            (geometry["X"], geometry["Y"], geometry["WIDTH"], geometry["HEIGHT"])
            if {"X", "Y", "WIDTH", "HEIGHT"}.issubset(geometry)
            else None
        )
        children = self._children(xid)
        fullscreen = self._fullscreen(xid)
        problems = []
        if rect != (0, 0, display[0], display[1]):
            problems.append(f"geometry={rect} display={display}")
        if not visible:
            problems.append("window_not_viewable")
        expected_client = (0, 0, display[0], display[1])
        if len(children) != 1 or children[0][1] != expected_client:
            problems.append(f"children={[rect for _id, rect in children]}")
        focused = self._focused(xid, [child for child, _rect in children])
        return WindowState(
            xid,
            1,
            not problems,
            focused,
            display,
            ",".join(problems),
            rect,
            children[0][1] if len(children) == 1 else None,
            fullscreen,
            visible,
        )

    def _viewable(self, xid: int) -> bool:
        tree = self._must(["xwininfo", "-id", hex(xid)])
        match = re.search(r"^\s*Map State:\s*(\S+)\s*$", tree, re.MULTILINE)
        return match is not None and match.group(1) == "IsViewable"

    def _is_unmapped_override_redirect(self, xid: int) -> bool:
        tree = self._must(["xwininfo", "-id", hex(xid)])
        map_states = re.findall(r"^\s*Map State:\s*(\S+)\s*$", tree, re.MULTILINE)
        override_states = re.findall(r"^\s*Override Redirect State:\s*(\S+)\s*$", tree, re.MULTILINE)
        return map_states == ["IsUnMapped"] and override_states == ["yes"]

    def _fullscreen(self, xid: int) -> bool:
        code, output = self._run(["xprop", "-id", str(xid), "_NET_WM_STATE"])
        atoms = {atom for atom in re.split(r"[,\s]+", output) if atom}
        return code == 0 and "_NET_WM_STATE_FULLSCREEN" in atoms

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
    def __init__(self, scratch_dir: Path, environment: EnvironmentProvider | None = None) -> None:
        self._scratch = scratch_dir
        self._environment = environment

    def _capture_run(self, argv: list[str], timeout: float) -> None:
        if self._environment is None:
            source_environment = os.environ
        elif callable(self._environment):
            source_environment = self._environment()
        else:
            source_environment = self._environment
        capture_environment = dict(source_environment)
        # Keep libhandy's theme-settings lookup out of the screenshot child.
        capture_environment["HDY_DISABLE_PORTAL"] = "1"
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=capture_environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise gui_capture.CaptureError(f"{argv[0]} timed out") from exc
        except OSError as exc:
            raise gui_capture.CaptureError(f"{argv[0]} failed ({type(exc).__name__})") from exc
        if done.returncode != 0:
            detail = " ".join(done.stderr.split())[:160]
            suffix = f": {detail}" if detail else ""
            raise gui_capture.CaptureError(f"{argv[0]} exited {done.returncode}{suffix}")

    def capture(self, display: tuple[int, int]) -> Any:
        from PIL import Image

        self._scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="scan-", dir=self._scratch) as scratch:
            path = Path(scratch) / "screen.png"
            gui_capture.capture_screen(path, display, run=self._capture_run)
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
