"""Build one `policy.Observation` per tick from real sources.

Every source sits behind a small interface so tests substitute fakes.  Nothing here
decides anything: process, window, canvas, disk and presence are read and mapped.

Decoder epochs.  A `PixelDecoder` is bound to one window identity and one boot.  A new
decoder is created whenever the window identity (game pid, process start, window id)
changes, and a boot change is accepted only when `arm_boot_change` was called after our own
`/reload` (`Decision.arm_boot_change`).  An unexpected boot change therefore stops decoding
until the process is relaunched (policy row 2), which is the designed recovery.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from crowley.core import companion_state, savedvars
from crowley.gui import capture as gui_capture
from crowley.gui.frames import PixelDecoder, ProtocolContract, ProtocolError
from crowley.gui.process import OwnedGameMatcher

from .policy import LoopState, Observation, Snapshot, Target

RECORD_KIND_SCAN = "scan"  # companion/sync.py record kind of an uploaded scan
SAMPLE_FRESH = "FRESH"
STABLE_READ_ATTEMPTS = 4
STABLE_READ_INTERVAL_S = 0.05
COMMAND_TIMEOUT_S = 5.0
DECODE_ERROR_LIMIT = 160


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
_HEX_ID = re.compile(r"^\s+(0x[0-9a-fA-F]+)\s", re.MULTILINE)


class WindowSource(Protocol):
    def observe(self) -> WindowState: ...


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
    """Exactly one visible game-class window, fullscreen at the origin, and who has focus."""

    def __init__(self, window_class: str, run: X11Run) -> None:
        self._pattern = f"^{re.escape(window_class)}$"
        self._run = run

    def _must(self, argv: list[str]) -> str:
        code, out = self._run(argv)
        if code != 0:
            raise ObserveError(f"{' '.join(argv[:2])} exited {code}")
        return out

    def observe(self) -> WindowState:
        code, out = self._run(["xdotool", "search", "--onlyvisible", "--class", self._pattern])
        if code not in (0, 1):  # 1 = no match
            raise ObserveError(f"xdotool search exited {code}")
        ids = [int(line) for line in out.split()] if code == 0 else []
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
        ok = rect == (0, 0, display[0], display[1])
        detail = "" if ok else f"geometry={rect} display={display}"
        return WindowState(xid, 1, ok, self._focused(xid), display, detail)

    def _focused(self, xid: int) -> bool:
        code, out = self._run(["xdotool", "getwindowfocus"])
        foreground = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
        if foreground == 0:
            code, out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
            match = re.search(r"#\s*(0x[0-9a-fA-F]+)", out) if code == 0 else None
            foreground = int(match.group(1), 16) if match else 0
        if foreground == xid:
            return True
        if foreground == 0:
            return False
        code, tree = self._run(["xwininfo", "-id", hex(xid), "-children"])
        children = {int(m, 16) for m in _HEX_ID.findall(tree)} if code == 0 else set()
        return foreground in children


# --- canvas -------------------------------------------------------------------------------


class CanvasSource(Protocol):
    def capture(self, display: tuple[int, int]) -> Any:
        """The canvas crop of a fresh screenshot as a PIL RGB image."""
        ...


class ScreenCanvas:
    def __init__(self, crop: tuple[int, int, int, int], scratch_dir: Path) -> None:
        self._crop = crop
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
        left, top, width, height = self._crop
        if left + width > image.width or top + height > image.height:
            raise ObserveError("canvas_crop lies outside the screenshot")
        return image.crop((left, top, left + width, top + height))


# --- snapshot mapping ---------------------------------------------------------------------


def blocking_reason(blocking: dict[str, Any]) -> str:
    """ "" when both popups are observable and hidden; else a short reason."""

    reasons = []
    if not blocking["staticPopupAvailable"]:
        reasons.append("static_popup_unavailable")
    elif blocking["staticPopupShown"]:
        reasons.append("static_popup_shown")
    if not blocking["gameMenuAvailable"]:
        reasons.append("game_menu_unavailable")
    elif blocking["gameMenuShown"]:
        reasons.append("game_menu_shown")
    return ",".join(reasons)


def to_snapshot(value: dict[str, Any], received_at: float) -> Snapshot:
    """Map a validated schema-6 snapshot dict to `policy.Snapshot` (see policy.py docstring)."""

    context, auctioneer, scanner = value["context"], value["auctioneer"], value["scanner"]
    pending, chat, render = value["pending"], value["chatEdit"], value["render"]
    return Snapshot(
        boot=value["boot"],
        native_phase=render["nativePhase"],
        native_ordinal=render["nativeOrdinal"],
        wall_time=value["wallTime"],
        received_at=received_at,
        character=context["character"],
        realm=context["realm"],
        faction=context["playerFaction"],
        zone=context["zone"],
        build=context["clientBuild"],
        interface=context["interface"],
        addon_version=context["addonVersion"],
        house_key=value["house"]["key"],
        plan_hash=value["plan"]["hash"],
        ah_open=value["ahOpen"],
        target_present=auctioneer["targetPresent"],
        auctioneer_name=auctioneer["targetName"],
        range_available=auctioneer["rangeAvailable"],
        in_range=auctioneer["withinTradeRange"],
        binding_available=auctioneer["bindingAvailable"],
        binding_action=auctioneer["ctrlF12Action"],
        interaction_mode=auctioneer["interactionMode"],
        blocking_ui=blocking_reason(value["blockingUI"]),
        phase=scanner["phase"],
        due_at=float(scanner["dueAt"]),
        last_saved_uid=value["lastSaved"]["memoryUid"],
        pending_ids=frozenset(pending["ids"]),
        pending_count=pending["count"],
        pending_cap=pending["cap"],
        pending_blocked=pending["blocked"],
        pending_reason=pending["reason"],
        loaded_acked_ids=frozenset(value["loaded"]["ackedIds"]),
        chat_active=chat["active"],
        chat_focused=chat["focused"],
        chat_text=chat["text"],
    )


# --- disk ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskState:
    saved_ids: frozenset[str]
    uploaded_ids: frozenset[str]
    rejected_ids: frozenset[str]
    last_error: str
    acked_ids: frozenset[str]


class DiskSource(Protocol):
    def read(self) -> DiskState: ...


def stable_bytes(path: Path) -> bytes:
    """Read a file only when two consecutive stat/read/stat rounds agree (writer finished)."""

    previous: tuple[int, int] | None = None
    for attempt in range(STABLE_READ_ATTEMPTS):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        marker = (after.st_mtime_ns, after.st_size)
        if marker == (before.st_mtime_ns, before.st_size) and marker == previous:
            return data
        previous = marker
        if attempt < STABLE_READ_ATTEMPTS - 1:
            time.sleep(STABLE_READ_INTERVAL_S)
    raise ObserveError(f"file did not stabilize: {path.name}")


def saved_scan_ids(root: dict[str, Any]) -> frozenset[str]:
    db = root.get("CrowleyDB")
    if not isinstance(db, dict) or not isinstance(db.get("scans"), dict):
        raise ObserveError("CrowleyDB.scans is missing from SavedVariables")
    return frozenset(str(key) for key in db["scans"])


def acked_scan_ids(root: dict[str, Any]) -> frozenset[str]:
    value = root.get("CROWLEY_DATA")
    if not isinstance(value, dict):
        raise ObserveError("CROWLEY_DATA is missing from Data.lua")
    entries = value.get("ackedScans", [])
    if isinstance(entries, dict) and not entries:
        entries = []  # Lua serializes an empty array as {}
    if not isinstance(entries, list) or any(not isinstance(item, str) or not item for item in entries):
        raise ObserveError("CROWLEY_DATA.ackedScans is invalid")
    return frozenset(entries)


class FileDiskSource:
    """Stable ids from the account SavedVariables, companion state and Data.lua.

    A SavedVariables file or companion state that does not exist yet means no scan has
    been saved or uploaded; Data.lua is installed with the addon, so its absence is an
    error.  Parsed results are cached by (mtime, size) so a 2 s tick is cheap.
    """

    def __init__(self, saved_variables: Path, companion: Path, data_lua: Path) -> None:
        self._paths = (saved_variables, companion, data_lua)
        self._cache: dict[Path, tuple[tuple[int, int], Any]] = {}

    def _cached(self, path: Path, parse: Callable[[bytes], Any]) -> Any:
        stat = path.stat()
        marker = (stat.st_mtime_ns, stat.st_size)
        hit = self._cache.get(path)
        if hit is not None and hit[0] == marker:
            return hit[1]
        value = parse(stable_bytes(path))
        self._cache[path] = (marker, value)
        return value

    def read(self) -> DiskState:
        saved_path, companion_path, data_path = self._paths
        saved = frozenset[str]()
        if saved_path.exists():
            saved = self._cached(saved_path, lambda raw: saved_scan_ids(savedvars.loads(raw)))
        uploaded = frozenset[str]()
        rejected = frozenset[str]()
        last_error = ""
        if companion_path.exists():
            state = self._cached(companion_path, companion_state.parse_bytes)
            uploaded = frozenset(state.uploaded)
            scan_rejections = [r for r in state.rejections.values() if r.key.record_kind == RECORD_KIND_SCAN]
            rejected = frozenset(r.key.record_id for r in scan_rejections)
            if scan_rejections:
                latest = max(scan_rejections, key=lambda r: r.last_seen)
                last_error = latest.error_code or f"http_{latest.status}"
        acked = self._cached(data_path, lambda raw: acked_scan_ids(savedvars.loads(raw)))
        return DiskState(saved, uploaded, rejected, last_error, acked)


# --- presence -----------------------------------------------------------------------------


class PresenceSource(Protocol):
    def human_active(self) -> tuple[bool, str]: ...


# --- observer -----------------------------------------------------------------------------


class Observer:
    def __init__(
        self,
        *,
        process: ProcessSource,
        window: WindowSource,
        canvas: CanvasSource,
        disk: DiskSource,
        presence: PresenceSource,
        target: Target,
        contract: ProtocolContract,
        canvas_crop: tuple[int, int, int, int],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process, self._window, self._canvas = process, window, canvas
        self._disk, self._presence = disk, presence
        self._target, self._contract, self._crop = target, contract, canvas_crop
        self._clock, self._monotonic = clock, monotonic
        self._decoder: PixelDecoder | None = None
        self._identity = ""
        self._process_key: tuple[tuple[int, ...], float] | None = None
        self._frames_seen = 0
        self._last_frame_at: float | None = None
        self._latest: tuple[dict[str, Any], float] | None = None
        self.decode_error = ""
        self.window: WindowState = ABSENT_WINDOW

    @property
    def window_xid(self) -> int | None:
        return self.window.xid

    def human_active(self) -> tuple[bool, str]:
        return self._presence.human_active()

    def arm_boot_change(self) -> None:
        """Our own `/reload` was submitted: accept the one boot change it causes."""

        if self._decoder is not None:
            self._decoder.allow_boot_change_once()

    def _new_decoder(self) -> PixelDecoder:
        return PixelDecoder(crop=self._crop, contract=self._contract)

    def _sample(self, window: WindowState, identity: str) -> tuple[dict[str, Any], float] | None:
        """Capture and decode once; update the frame counters.  None when no frame decoded."""

        if identity != self._identity or self._decoder is None:
            self._decoder = self._new_decoder()
            self._identity = identity
            self._latest = None
        now = self._clock()
        try:
            image = self._canvas.capture(window.display)
            sample = self._decoder.decode(image, monotonic=self._monotonic(), window_identity=identity)
        except (gui_capture.CaptureError, ObserveError, ProtocolError) as exc:
            self.decode_error = f"{type(exc).__name__}: {exc}"[:DECODE_ERROR_LIMIT]
            return None
        self.decode_error = ""
        assert sample.frame is not None
        if sample.kind == SAMPLE_FRESH or self._latest is None:
            self._frames_seen += 1
            self._last_frame_at = now
            self._latest = (dict(sample.frame.snapshot.value), now)
        return self._latest

    def probe_frame(self) -> bool:
        """One capture and decode with a throwaway decoder: is a valid frame on screen?"""

        window = self._window.observe()
        if not window.ok or window.xid is None:
            return False
        decoder = self._new_decoder()
        try:
            image = self._canvas.capture(window.display)
            decoder.decode(image, monotonic=self._monotonic(), window_identity=f"probe:{window.xid}")
        except (gui_capture.CaptureError, ObserveError, ProtocolError):
            return False
        return True

    def observe(self, state: LoopState) -> Observation:
        wall, mono = self._clock(), self._monotonic()
        pids = self._process.pids()
        alive = bool(pids)
        started = self._process.started_at(pids) if alive else 0.0
        key = (tuple(pids), started) if alive else None
        if key != self._process_key:
            self._process_key = key
            self._frames_seen = 0
            self._last_frame_at = None
            self._latest = None
        window = self._window.observe() if alive else ABSENT_WINDOW
        self.window = window
        if key is not None and window.ok and window.xid is not None:
            self._sample(window, f"{key[0][0]}:{started}:{window.xid}")
        elif not alive:
            self.decode_error = ""
        # A failed or skipped decode leaves the last frame in place; it ages into "stale".
        latest = self._latest if alive else None
        human, reason = self._presence.human_active()
        disk = self._disk.read()
        snapshot = to_snapshot(latest[0], latest[1]) if latest is not None else None
        return Observation(
            wall=wall,
            mono=mono,
            process_alive=alive,
            process_started_at=started,
            window_ok=window.ok,
            window_focused=window.focused,
            snapshot=snapshot,
            last_frame_at=self._last_frame_at,
            frames_seen_this_process=self._frames_seen,
            disk_saved_ids=disk.saved_ids,
            uploaded_ids=disk.uploaded_ids,
            permanently_rejected_ids=disk.rejected_ids,
            data_acked_ids=disk.acked_ids,
            companion_last_error=disk.last_error,
            human_active=human,
            human_reason=reason,
            target=self._target,
            state=state,
        )
