"""Scanner-aware disk and dual-publication observer for the host loop."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from crowley_automation.desktop_observe import (
    ABSENT_WINDOW,
    FUTURE_FRAME_TOLERANCE_SECONDS,
    MAX_FRAME_AGE_SECONDS,
    CanvasSource,
    ObserveError,
    WindowSource,
    WindowState,
    crop_canvas,
)
from crowley_automation.desktop_session import (
    DesktopSession,
    DesktopSessionProvider,
    DesktopSessionUnavailable,
)
from crowley_automation.gui import capture as gui_capture
from crowley_automation.gui.process import ProcessIdentity
from crowley_common.frames import FrameKind, PixelDecoder, ProtocolContract, ProtocolError
from crowley_scanner import savedvars
from crowley_scanner import state as companion_state

from .policy import LoopState, Observation, Snapshot, Target

RECORD_KIND_SCAN = "scan"
SAMPLE_FRESH = "FRESH"
STABLE_READ_ATTEMPTS = 4
STABLE_READ_INTERVAL_S = 0.05
DECODE_ERROR_LIMIT = 160


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


def automation_busy(value: dict[str, Any]) -> bool:
    operation = value["operation"]
    ownership = value["ownership"]
    stage = operation.get("stage", "")
    return (
        bool(operation["batchId"])
        or stage not in ("", False, None)
        or operation["action"] is not False
        or ownership["ahOwner"] == "automation"
    )


def to_snapshot(
    scanner_value: dict[str, Any], automation_value: dict[str, Any], received_at: float
) -> Snapshot:
    """Join validated scanner and automation facts at the observation boundary."""

    context = automation_value["context"]
    auctioneer = automation_value["auctioneer"]
    scanner = scanner_value["scanner"]
    pending, chat = scanner_value["pending"], automation_value["chatEdit"]
    return Snapshot(
        boot=automation_value["boot"],
        wall_time=automation_value["wallTime"],
        received_at=received_at,
        character=context["character"],
        realm=context["realm"],
        faction=context["playerFaction"],
        zone=context["zone"],
        build=context["clientBuild"],
        interface=context["interface"],
        native_region=context["nativeRegion"],
        addon_version=context["addonVersion"],
        scanner_addon_version=scanner_value["addonVersion"],
        house_key=scanner_value["house"]["key"],
        plan_hash=scanner_value["plan"]["hash"],
        ah_open=automation_value["ahOpen"],
        target_present=auctioneer["targetPresent"],
        auctioneer_name=auctioneer["targetName"],
        range_available=auctioneer["rangeAvailable"],
        in_range=auctioneer["withinTradeRange"],
        binding_available=auctioneer["bindingAvailable"],
        binding_action=auctioneer["ctrlF12Action"],
        interaction_mode=auctioneer["interactionMode"],
        blocking_ui=blocking_reason(automation_value["blockingUI"]),
        phase=scanner["phase"],
        due_at=float(scanner["dueAt"]),
        last_saved_uid=scanner_value["lastSaved"]["memoryUid"],
        pending_ids=frozenset(pending["ids"]),
        pending_count=pending["count"],
        pending_cap=pending["cap"],
        pending_blocked=pending["blocked"],
        pending_reason=pending["reason"],
        loaded_acked_ids=frozenset(scanner_value["loaded"]["ackedIds"]),
        chat_active=chat["active"],
        chat_focused=chat["focused"],
        chat_text=chat["text"],
        automation_busy=automation_busy(automation_value),
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


class OwnedProcesses(Protocol):
    def identities(self) -> tuple[ProcessIdentity, ...]: ...

    def started_at(self, pids: list[int]) -> float: ...


class SessionEnvironment(Protocol):
    def refresh(self, session: DesktopSession) -> None: ...

    def __call__(self) -> Mapping[str, str]: ...


# --- observer -----------------------------------------------------------------------------


class Observer:
    def __init__(
        self,
        *,
        process: OwnedProcesses,
        window: WindowSource,
        canvas: CanvasSource,
        disk: DiskSource,
        presence: PresenceSource,
        target: Target,
        contract: ProtocolContract,
        canvas_crop: tuple[int, int, int, int],
        automation_canvas_crop: tuple[int, int, int, int],
        session_provider: DesktopSessionProvider | None = None,
        session_environment: SessionEnvironment | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process, self._window, self._canvas = process, window, canvas
        self._session_provider = session_provider
        self._session_environment = session_environment
        self._disk, self._presence = disk, presence
        self._target, self._contract = target, contract
        self._scanner_crop, self._automation_crop = canvas_crop, automation_canvas_crop
        self._clock, self._monotonic = clock, monotonic
        self._scanner_decoder: PixelDecoder | None = None
        self._automation_decoder: PixelDecoder | None = None
        self._identity = ""
        self._process_key: tuple[tuple[ProcessIdentity, ...], float] | None = None
        self._session_fingerprint = ""
        self._frames_seen = 0
        self._last_frame_at: float | None = None
        self._latest_scanner: tuple[dict[str, Any], float] | None = None
        self._latest_automation: tuple[dict[str, Any], float] | None = None
        self._latest: tuple[Snapshot, float] | None = None
        self._latest_pair: tuple[tuple[str, int], tuple[str, int]] | None = None
        self.decode_error = ""
        self.window: WindowState = ABSENT_WINDOW

    @property
    def window_source(self) -> WindowSource:
        """Window query adapter shared with the guarded action path."""

        return self._window

    @property
    def window_xid(self) -> int | None:
        return None if self.window.foreign_count else self.window.xid

    def human_active(self) -> tuple[bool, str]:
        return self._presence.human_active()

    def arm_boot_change(self) -> None:
        """Our own `/reload` was submitted: accept the one boot change it causes."""

        if self._scanner_decoder is not None:
            self._scanner_decoder.allow_boot_change_once()
        if self._automation_decoder is not None:
            self._automation_decoder.allow_boot_change_once()

    def _new_decoder(self, kind: FrameKind, crop: tuple[int, int, int, int]) -> PixelDecoder:
        return PixelDecoder(
            expected_kind=kind,
            crop=crop,
            contract=self._contract if kind == "scanner" else None,
        )

    def _retire_evidence(self) -> None:
        self._scanner_decoder = None
        self._automation_decoder = None
        self._identity = ""
        self._latest_scanner = None
        self._latest_automation = None
        self._latest = None
        self._latest_pair = None
        self._last_frame_at = None
        self._frames_seen = 0
        self.decode_error = ""

    def _native_ready(self, value: dict[str, Any] | None, received_at: float | None, now: float) -> bool:
        age = None if received_at is None else now - received_at
        if (
            value is None
            or age is None
            or age > MAX_FRAME_AGE_SECONDS
            or age < -FUTURE_FRAME_TOLERANCE_SECONDS
        ):
            return False
        context = value["context"]
        expected = {
            "character": self._target.character,
            "realm": self._target.realm,
            "playerFaction": self._target.faction,
            "clientBuild": self._target.build,
            "interface": self._target.interface,
            "addonVersion": self._target.addon_version,
            "nativeRegion": self._target.native_region,
        }
        return all(context[key] == expected_value for key, expected_value in expected.items())

    def _sample(self, window: WindowState, identity: str) -> tuple[Snapshot, float] | None:
        """Capture and decode once; update the frame counters.  None when no frame decoded."""

        if identity != self._identity or self._scanner_decoder is None or self._automation_decoder is None:
            self._scanner_decoder = self._new_decoder("scanner", self._scanner_crop)
            self._automation_decoder = self._new_decoder("automation", self._automation_crop)
            self._identity = identity
            self._latest = None
            self._latest_scanner = None
            self._latest_automation = None
            self._latest_pair = None
        now = self._clock()
        try:
            image = self._canvas.capture(window.display)
            scanner_image = crop_canvas(image, window.display, self._scanner_crop, "scanner canvas")
            automation_image = crop_canvas(image, window.display, self._automation_crop, "automation canvas")
        except (gui_capture.CaptureError, ObserveError) as exc:
            self.decode_error = f"{type(exc).__name__}: {exc}"[:DECODE_ERROR_LIMIT]
            return None
        errors = []
        try:
            scanner_sample = self._scanner_decoder.decode(
                scanner_image, monotonic=self._monotonic(), window_identity=identity
            )
            if scanner_sample.frame is not None and scanner_sample.kind == SAMPLE_FRESH:
                self._latest_scanner = (dict(scanner_sample.frame.snapshot.value), now)
        except ProtocolError as exc:
            errors.append(f"scanner {type(exc).__name__}: {exc}")
        try:
            automation_sample = self._automation_decoder.decode(
                automation_image, monotonic=self._monotonic(), window_identity=identity
            )
            if automation_sample.frame is not None and automation_sample.kind == SAMPLE_FRESH:
                self._latest_automation = (dict(automation_sample.frame.snapshot.value), now)
        except ProtocolError as exc:
            errors.append(f"automation {type(exc).__name__}: {exc}")
        self.decode_error = "; ".join(errors)[:DECODE_ERROR_LIMIT]
        if errors:
            return self._latest
        if self._latest_scanner is None or self._latest_automation is None:
            return None
        scanner_value, scanner_at = self._latest_scanner
        automation_value, automation_at = self._latest_automation
        pair = (
            (scanner_value["boot"], scanner_value["sequence"]),
            (automation_value["boot"], automation_value["sequence"]),
        )
        if pair != self._latest_pair:
            received_at = min(scanner_at, automation_at)
            snap = to_snapshot(scanner_value, automation_value, received_at)
            self._frames_seen += 1
            self._last_frame_at = received_at
            self._latest = (snap, received_at)
            self._latest_pair = pair
        return self._latest

    def observe(self, state: LoopState) -> Observation:
        wall, mono = self._clock(), self._monotonic()
        session_fingerprint = ""
        session_unavailable = False
        if self._session_provider is not None:
            try:
                session = self._session_provider.current()
            except DesktopSessionUnavailable:
                self._retire_evidence()
                session_unavailable = True
            else:
                session_fingerprint = session.fingerprint
                if self._session_environment is not None:
                    self._session_environment.refresh(session)
                if session_fingerprint != self._session_fingerprint:
                    self._retire_evidence()
                self._session_fingerprint = session_fingerprint
        identities = self._process.identities()
        pids = [identity.pid for identity in identities]
        alive = bool(identities)
        started = self._process.started_at(pids) if len(identities) == 1 else 0.0
        identity = identities[0] if len(identities) == 1 else None
        key = (identities, started) if identity is not None else None
        if key != self._process_key:
            self._process_key = key
            self._retire_evidence()
        window = (
            self._window.observe(pids) if identity is not None and not session_unavailable else ABSENT_WINDOW
        )
        self.window = window
        owned_window_ok = window.ok and window.foreign_count == 0
        if key is not None and identity is not None and owned_window_ok and window.xid is not None:
            self._sample(window, f"{identity.pid}:{identity.start_ticks}:{window.xid}")
        elif not alive:
            self.decode_error = ""
        # A failed or skipped decode leaves the last frame in place; it ages into "stale".
        latest = self._latest if alive else None
        latest_automation = self._latest_automation if alive and not session_unavailable else None
        human, reason = self._presence.human_active()
        disk = self._disk.read()
        snapshot = latest[0] if latest is not None else None
        automation_value = latest_automation[0] if latest_automation is not None else None
        automation_at = latest_automation[1] if latest_automation is not None else None
        if self._session_provider is not None and session_fingerprint:
            try:
                current_session = self._session_provider.current()
                if current_session.fingerprint != session_fingerprint:
                    if self._session_environment is not None:
                        self._session_environment.refresh(current_session)
                    self._retire_evidence()
                    self.window = ABSENT_WINDOW
                    window = ABSENT_WINDOW
                    snapshot = None
                    automation_value = None
                    automation_at = None
                    latest_automation = None
                    session_fingerprint = ""
            except DesktopSessionUnavailable:
                self._retire_evidence()
                self.window = ABSENT_WINDOW
                window = ABSENT_WINDOW
                snapshot = None
                automation_value = None
                automation_at = None
                latest_automation = None
                session_fingerprint = ""
        return Observation(
            wall=wall,
            mono=mono,
            process_alive=alive,
            process_started_at=started,
            window_ok=owned_window_ok and window.fullscreen,
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
            native_automation_busy=(
                automation_busy(latest_automation[0]) if latest_automation is not None else False
            ),
            process_identity=identity,
            window_xid=window.xid if owned_window_ok else None,
            window_fullscreen=window.fullscreen,
            session_fingerprint=session_fingerprint,
            native_ready=self._native_ready(automation_value, automation_at, wall)
            and identity is not None
            and owned_window_ok
            and window.fullscreen,
            native_boot=automation_value["boot"] if automation_value is not None else "",
            state=state,
        )
