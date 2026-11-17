from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from crowley_automation import desktop_observe
from crowley_automation.gui.capture import CaptureError
from crowley_automation.gui.process import ProcessIdentity
from crowley_common.frames import PLAN_COUNT
from crowley_host.scan import observe
from crowley_host.scan.policy import LoopState, Target
from crowley_scanner.state import (
    STATE_VERSION,
    CompanionState,
    RejectionKey,
    RejectionRecord,
    dump_bytes,
)

from tests.support.scan_frames import (
    AUTOMATION_CROP,
    SCANNER_CROP,
    automation_snapshot,
    render_screen,
    snapshot,
)

TARGET = Target(
    character="Arthur",
    realm="Classic Beta PvE 2",
    faction="Alliance",
    house_key="beta-normal-alliance",
    plan_hash="a" * 64,
    build="70450",
    interface=16001,
    addon_version="0.2.0",
    scanner_addon_version="0.3.0",
    auctioneer="Auctioneer Jaxon",
    native_region=1,
)
DISPLAY = (1920, 1080)


class FakeProcess:
    def __init__(self, pids: list[int], started: float = 100.0) -> None:
        self.current, self.started = pids, started

    def pids(self) -> list[int]:
        return list(self.current)

    def identities(self) -> tuple[ProcessIdentity, ...]:
        return tuple(
            ProcessIdentity(pid, int(self.started * 100), 1000, "C:\\Wow.exe", "/prefix")
            for pid in self.current
        )

    def started_at(self, pids: list[int]) -> float:
        return self.started


class FakeWindow:
    def __init__(self, ok: bool = True, focused: bool = True, xid: int = 77) -> None:
        self.state = desktop_observe.WindowState(xid, 1, ok, focused, DISPLAY, fullscreen=True)

    def observe(self, pids: list[int]) -> desktop_observe.WindowState:
        self.pids = pids
        return self.state


class FakeCanvas:
    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.error: Exception | None = None
        self.sizes: list[tuple[int, int]] = []

    def capture(self, display: tuple[int, int]) -> Any:
        self.sizes.append(display)
        if self.error:
            raise self.error
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class FakeDisk:
    def read(self) -> observe.DiskState:
        e = frozenset[str]()
        return observe.DiskState(frozenset({"s1"}), frozenset({"u1"}), e, "", frozenset({"a1"}))


class FakePresence:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def human_active(self) -> tuple[bool, str]:
        return self.active, "fake"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def make(
    process: FakeProcess | None = None,
    window: FakeWindow | None = None,
    canvas: FakeCanvas | None = None,
    presence: FakePresence | None = None,
    clock: Clock | None = None,
) -> tuple[observe.Observer, FakeProcess, FakeWindow, FakeCanvas, Clock]:
    process, window = process or FakeProcess([10]), window or FakeWindow()
    canvas, clock = canvas or FakeCanvas(), clock or Clock()
    from crowley_common.frames import ProtocolContract

    obs = observe.Observer(
        process=process,
        window=window,
        canvas=canvas,
        disk=FakeDisk(),
        presence=presence or FakePresence(),
        target=TARGET,
        contract=ProtocolContract(PLAN_COUNT, 900, 600, 20, (), plan_hash="a" * 64),
        canvas_crop=SCANNER_CROP,
        automation_canvas_crop=AUTOMATION_CROP,
        clock=clock,
        monotonic=clock,
    )
    return obs, process, window, canvas, clock


def test_snapshot_mapping_covers_every_policy_field() -> None:
    scanner = snapshot(5)
    game = automation_snapshot(7)
    game["ahOpen"] = False
    game["auctioneer"].update(targetPresent=True, targetName="Auctioneer Jaxon", rangeAvailable=True)
    game["auctioneer"]["withinTradeRange"] = True
    scanner["scanner"].update(phase="scheduled", dueAt=1_900)
    scanner["lastSaved"]["memoryUid"] = "uid-1"
    scanner["lastSaved"].update(finishedAt=900, planned=100, attempted=100, failures=0, rows=10)
    scanner["pending"].update(ids=["uid-1"], count=1)
    scanner["loaded"]["ackedIds"] = ["a1"]
    game["chatEdit"] = {"active": True, "focused": True, "text": "/rel"}
    snap = observe.to_snapshot(scanner, game, 1234.5)
    assert (snap.boot, snap.wall_time, snap.received_at) == ("automation-boot-1", 1_000, 1234.5)
    assert (snap.character, snap.realm, snap.faction, snap.zone) == (
        "Arthur",
        "Classic Beta PvE 2",
        "Alliance",
        "Orgrimmar",
    )
    assert (snap.build, snap.interface, snap.addon_version, snap.scanner_addon_version) == (
        "70450",
        16001,
        "0.2.0",
        "0.3.0",
    )
    assert snap.native_region == 1
    assert (snap.house_key, snap.plan_hash) == ("beta-normal-alliance", "a" * 64)
    assert not snap.ah_open and snap.target_present and snap.auctioneer_name == "Auctioneer Jaxon"
    assert snap.range_available and snap.in_range and snap.binding_available
    assert (snap.binding_action, snap.interaction_mode, snap.blocking_ui) == ("", "any", "")
    assert (snap.phase, snap.due_at, snap.last_saved_uid) == ("scheduled", 1900.0, "uid-1")
    assert snap.pending_ids == {"uid-1"} and snap.pending_count == 1 and snap.pending_cap == 6
    assert not snap.pending_blocked and snap.pending_reason == ""
    assert snap.loaded_acked_ids == {"a1"}
    assert (snap.chat_active, snap.chat_focused, snap.chat_text) == (True, True, "/rel")
    assert not snap.automation_busy


def test_native_operation_and_auction_house_ownership_block_maintenance() -> None:
    scanner = snapshot(1)
    game = automation_snapshot(1)
    game["operation"]["batchId"] = "batch-1"
    assert observe.to_snapshot(scanner, game, 1000.0).automation_busy

    game = automation_snapshot(2)
    game["ownership"]["ahOwner"] = "automation"
    assert observe.to_snapshot(scanner, game, 1000.0).automation_busy

    game = automation_snapshot(3)
    game["operation"]["receipt"]["actionComplete"] = "unknown"
    assert not observe.to_snapshot(scanner, game, 1000.0).automation_busy


@pytest.mark.parametrize(
    ("blocking", "expected"),
    [
        ({}, ""),
        ({"staticPopupShown": True}, "static_popup_shown"),
        ({"gameMenuShown": True}, "game_menu_shown"),
        ({"staticPopupAvailable": False, "staticPopupShown": False}, "static_popup_unavailable"),
        ({"gameMenuAvailable": False}, "game_menu_unavailable"),
    ],
)
def test_blocking_reason(blocking: dict[str, bool], expected: str) -> None:
    base = {
        "staticPopupAvailable": True,
        "staticPopupShown": False,
        "gameMenuAvailable": True,
        "gameMenuShown": False,
    }
    assert observe.blocking_reason({**base, **blocking}) == expected


def test_observe_decodes_a_frame_and_tracks_progress() -> None:
    obs, _p, _w, canvas, clock = make()
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    first = obs.observe(LoopState())
    assert first.process_alive and first.window_ok and first.window_focused
    assert first.process_identity == ProcessIdentity(10, 10000, 1000, "C:\\Wow.exe", "/prefix")
    assert first.window_xid == 77 and first.window_fullscreen and first.native_ready
    assert first.snapshot is not None and first.snapshot.received_at == 1000.0
    assert first.frames_seen_this_process == 1 and first.last_frame_at == 1000.0
    assert first.disk_saved_ids == {"s1"} and first.uploaded_ids == {"u1"} and first.data_acked_ids == {"a1"}
    assert not first.human_active and first.target == TARGET
    assert canvas.sizes == [DISPLAY]
    # The same frame again is NO_CHANGE: the snapshot keeps its old received_at.
    clock.now = 1005.0
    again = obs.observe(LoopState())
    assert again.snapshot is not None and again.snapshot.received_at == 1000.0
    assert not again.native_ready  # the cached native publication was not retimestamped
    assert again.frames_seen_this_process == 1 and again.last_frame_at == 1000.0
    canvas.frames = [render_screen(snapshot(2), automation_snapshot(2))]
    clock.now = 1006.0
    third = obs.observe(LoopState())
    assert third.snapshot is not None and third.snapshot.received_at == 1006.0
    assert third.frames_seen_this_process == 2


def test_no_process_means_no_capture() -> None:
    obs, process, _w, canvas, _c = make(process=FakeProcess([]))
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    result = obs.observe(LoopState())
    assert not result.process_alive and not result.window_ok and result.snapshot is None
    assert canvas.sizes == []


def test_capture_and_decode_errors_leave_no_fresh_snapshot() -> None:
    obs, _p, _w, canvas, _c = make()
    canvas.error = CaptureError("no screenshot")
    result = obs.observe(LoopState())
    assert result.snapshot is None and obs.decode_error.startswith("CaptureError")
    canvas.error = None
    from PIL import Image

    canvas.frames = [Image.new("RGB", DISPLAY, (7, 7, 7))]
    result = obs.observe(LoopState())
    assert result.snapshot is None and obs.decode_error.startswith("scanner ProtocolError")


def test_native_busy_survives_a_bad_scanner_canvas_without_rewriting_frame_time() -> None:
    from PIL import ImageDraw

    def mixed(sequence: int, busy: bool, *, bad_scanner: bool):
        scanner, game = snapshot(sequence), automation_snapshot(sequence)
        if busy:
            game["ownership"]["ahOwner"] = "automation"
        image = render_screen(scanner, game)
        if bad_scanner:
            left, top, width, height = SCANNER_CROP
            ImageDraw.Draw(image).rectangle((left, top, left + width - 1, top + height - 1), fill=(7, 7, 7))
        return image

    obs, _process, _window, canvas, clock = make()
    canvas.frames = [mixed(1, False, bad_scanner=False)]
    first = obs.observe(LoopState())
    assert first.snapshot is not None and not first.native_automation_busy

    clock.now += 1
    canvas.frames = [mixed(2, True, bad_scanner=True)]
    stale_pair = obs.observe(LoopState())
    assert stale_pair.snapshot == first.snapshot
    assert stale_pair.snapshot is not None and not stale_pair.snapshot.automation_busy
    assert stale_pair.native_automation_busy
    assert stale_pair.native_ready
    assert stale_pair.last_frame_at == first.last_frame_at
    assert obs.decode_error.startswith("scanner ProtocolError")

    fresh, _process, _window, fresh_canvas, _clock = make()
    fresh_canvas.frames = [mixed(1, True, bad_scanner=True)]
    no_scanner_history = fresh.observe(LoopState())
    assert no_scanner_history.snapshot is None
    assert no_scanner_history.native_automation_busy
    assert no_scanner_history.native_ready


def test_window_not_ok_skips_capture() -> None:
    obs, _p, _w, canvas, _c = make(window=FakeWindow(ok=False))
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    result = obs.observe(LoopState())
    assert not result.window_ok and result.snapshot is None and canvas.sizes == []


def test_foreign_window_candidate_blocks_host_capture_even_if_marked_ok() -> None:
    obs, _p, window, canvas, _c = make()
    window.state = desktop_observe.WindowState(
        77,
        2,
        True,
        True,
        DISPLAY,
        "foreign_window_candidate",
        fullscreen=True,
        foreign_count=1,
    )
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]

    result = obs.observe(LoopState())

    assert not result.window_ok and not result.native_ready
    assert result.window_xid is None and result.snapshot is None
    assert canvas.sizes == []


def test_boot_change_is_rejected_until_armed() -> None:
    obs, _p, _w, canvas, _c = make()
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    obs.observe(LoopState())
    other = snapshot(2)
    other["boot"] = "scanner-boot-2"
    game = automation_snapshot(2)
    canvas.frames = [render_screen(other, game)]
    rejected = obs.observe(LoopState())
    assert "unexpected status boot change" in obs.decode_error
    assert rejected.frames_seen_this_process == 1
    obs.arm_boot_change()
    accepted = obs.observe(LoopState())
    assert obs.decode_error == "" and accepted.frames_seen_this_process == 2
    assert accepted.snapshot is not None and accepted.snapshot.scanner_addon_version == "0.3.0"


def test_decoder_epoch_resets_on_process_and_window_change() -> None:
    obs, process, window, canvas, _c = make()
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    obs.observe(LoopState())
    # A new process starts with another boot and sequence: no arming needed.
    process.current, process.started = [11], 500.0
    other = snapshot(1)
    other["boot"] = "scanner-boot-9"
    game = automation_snapshot(1)
    game["boot"] = "automation-boot-9"
    canvas.frames = [render_screen(other, game)]
    fresh = obs.observe(LoopState())
    assert obs.decode_error == ""
    assert fresh.frames_seen_this_process == 1 and fresh.process_started_at == 500.0
    assert fresh.snapshot is not None and fresh.snapshot.boot == "automation-boot-9"
    # A new window id also gets a new decoder (the old one is bound to the old identity).
    window.state = desktop_observe.WindowState(78, 1, True, True, DISPLAY, fullscreen=True)
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    obs.observe(LoopState())
    assert obs.decode_error == ""


def test_observer_does_not_choose_between_multiple_owned_processes() -> None:
    obs, _p, window, canvas, _c = make(process=FakeProcess([10, 12]))
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    result = obs.observe(LoopState())
    assert result.process_alive and result.process_identity is None
    assert not result.window_ok and result.snapshot is None
    assert not hasattr(window, "pids") and canvas.sizes == []


def test_presence_is_passed_through() -> None:
    obs, _p, _w, canvas, _c = make(presence=FakePresence(active=True))
    canvas.frames = [render_screen(snapshot(1), automation_snapshot(1))]
    result = obs.observe(LoopState())
    assert result.human_active and result.human_reason == "fake"


# --- window source ------------------------------------------------------------------------


TITLE = "World of Warcraft"
FULLSCREEN = "WINDOW=77\nX=0\nY=0\nWIDTH=1920\nHEIGHT=1080\nSCREEN=0\n"
ONE_CHILD = 'xwininfo: Window id: 0x4d\n  1 child:\n     0x4e "wow.exe": ("x" "y")  1920x1080+0+0  +0+0\n'


def x11_runner(
    ids: str = "77\n",
    geometry: str = FULLSCREEN,
    focus: str = "77\n",
    *,
    active: str = "",
    tree: str = ONE_CHILD,
    names: dict[str, str] | None = None,
    pids: dict[str, str] | None = None,
) -> tuple[observe.X11Run, list[list[str]]]:
    calls: list[list[str]] = []
    names = names or {}
    pids = pids or {}

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if argv[1] == "search":
            return (0, ids) if ids else (1, "")
        if argv[1] == "getwindowname":
            return 0, names.get(argv[2], TITLE) + "\n"
        if argv[1] == "getwindowpid":
            return 0, pids.get(argv[2], "10") + "\n"
        if argv[1] == "getdisplaygeometry":
            return 0, "1920 1080\n"
        if argv[1] == "getwindowgeometry":
            return 0, geometry
        if argv[1] == "getwindowfocus":
            return 0, focus
        if argv[0] == "xprop":
            if "_NET_WM_STATE" in argv:
                return 0, "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"
            return 0, f"_NET_ACTIVE_WINDOW(WINDOW): window id # {active}\n" if active else "no\n"
        if argv[0] == "xwininfo":
            return 0, tree
        raise AssertionError(argv)

    return run, calls


def window(run: desktop_observe.X11Run) -> desktop_observe.X11Window:
    return desktop_observe.X11Window("wow.exe", TITLE, run)


def test_window_exactly_one_owned_fullscreen_focused() -> None:
    run, calls = x11_runner()
    state = window(run).observe([10])
    assert (state.xid, state.ok, state.focused, state.display) == (77, True, True, DISPLAY)
    assert state.fullscreen
    assert calls[0] == ["xdotool", "search", "--onlyvisible", "--class", "^wow\\.exe$"]


@pytest.mark.parametrize("focus", ["77\n", "78\n"])
def test_typing_focus_check_accepts_owned_window_and_its_child_without_discovery(focus: str) -> None:
    run, calls = x11_runner(focus=focus)
    assert window(run).focus_state(77, 10) == (True, True)
    assert len(calls) == 4
    assert all(call[1] not in {"search", "getdisplaygeometry", "getwindowgeometry"} for call in calls)


def test_typing_focus_check_rejects_foreign_owner_and_focus() -> None:
    run, calls = x11_runner(pids={"77": "999"})
    assert window(run).focus_state(77, 10) == (False, False)
    assert len(calls) == 2
    run, _ = x11_runner(focus="999\n")
    assert window(run).focus_state(77, 10) == (True, False)


def test_window_match_requires_title_and_owned_pid() -> None:
    run, _ = x11_runner(ids="77\n78\n", names={"78": "Other"}, pids={"77": "10", "78": "10"})
    assert window(run).observe([10]).xid == 77  # 78 has the wrong title
    run, _ = x11_runner(pids={"77": "999"})
    state = window(run).observe([10])
    assert (
        not state.ok
        and state.xid is None
        and state.count == 1
        and state.foreign_count == 1
        and state.detail == "foreign_window_candidate"
    )
    run, _ = x11_runner(ids="77\n78\n", pids={"77": "10", "78": "11"})
    assert window(run).observe([10, 11]).count == 2  # two owned windows is not exactly one


def test_window_focus_on_child_and_active_window() -> None:
    run, _ = x11_runner(focus="78\n")  # 0x4e == 78 is the child
    assert window(run).observe([10]).focused
    run, _ = x11_runner(focus="0\n", active="0x4d")
    assert window(run).observe([10]).focused
    run, _ = x11_runner(focus="99\n")
    assert not window(run).observe([10]).focused


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [
        ({"ids": ""}, "window_count=0"),
        ({"ids": "77\n78\n"}, "window_count=2"),
        ({"geometry": "X=10\nY=0\nWIDTH=1920\nHEIGHT=1080\n"}, "geometry"),
        ({"geometry": "X=0\nY=0\nWIDTH=1900\nHEIGHT=1080\n"}, "geometry"),
        ({"tree": "  0 children:\n"}, "children=[]"),
        ({"tree": ONE_CHILD + '     0x4f "x": ()  10x10+0+0  +0+0\n'}, "children"),
        ({"tree": ONE_CHILD.replace("1920x1080", "1900x1080")}, "children"),
    ],
)
def test_window_not_ok(kwargs: dict[str, Any], detail: str) -> None:
    run, _ = x11_runner(**kwargs)
    state = window(run).observe([10])
    assert not state.ok and detail in state.detail


def test_window_requires_fullscreen_atom_for_native_readiness() -> None:
    def run(argv: list[str]) -> tuple[int, str]:
        if argv[0] == "xprop" and "_NET_WM_STATE" in argv:
            return 0, "_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT\n"
        base, _calls = x11_runner()
        return base(argv)

    state = window(run).observe([10])
    assert state.ok and not state.fullscreen


# --- disk ---------------------------------------------------------------------------------

SAVED = (
    b'CrowleyDB = {\n["scans"] = {\n["uid-1"] = {["id"] = "uid-1"},\n["uid-2"] = {["id"] = "uid-2"},\n},\n}\n'
)
DATA = b'CROWLEY_DATA = {\n["ackedScans"] = {"uid-1", "uid-3"},\n["generatedAt"] = 5,\n}\n'


def companion_bytes() -> bytes:
    key = RejectionKey("acct", "WTF/x.lua", "scan", "uid-9", "d" * 64)
    record = RejectionRecord(key, 422, "plan_mismatch", "msg", 1.0, 2.0, "c" * 64, 1)
    other = RejectionRecord(
        RejectionKey("acct", "WTF/x.lua", "addon-data", "addon-data", "e" * 64),
        400,
        "bad",
        "m",
        1.0,
        9.0,
        "c" * 64,
        1,
    )
    state = CompanionState(
        STATE_VERSION,
        {"uid-1": 10},
        (),
        {key.storage_key(): record, other.key.storage_key(): other},
    )
    return dump_bytes(state)


def test_disk_source_reads_fixture_files(tmp_path: Path) -> None:
    saved, companion, data = tmp_path / "Crowley.lua", tmp_path / "companion.json", tmp_path / "Data.lua"
    saved.write_bytes(SAVED)
    companion.write_bytes(companion_bytes())
    data.write_bytes(DATA)
    source = observe.FileDiskSource(saved, companion, data)
    state = source.read()
    assert state.saved_ids == {"uid-1", "uid-2"}
    assert state.uploaded_ids == {"uid-1"}
    assert state.rejected_ids == {"uid-9"}  # only scan records; addon-data is not a scan
    assert state.last_error == "plan_mismatch"
    assert state.acked_ids == {"uid-1", "uid-3"}


def test_disk_source_missing_saved_and_companion_are_empty_but_data_lua_is_required(tmp_path: Path) -> None:
    data = tmp_path / "Data.lua"
    data.write_bytes(b'CROWLEY_DATA = {\n["ackedScans"] = {},\n}\n')
    state = observe.FileDiskSource(tmp_path / "a", tmp_path / "b", data).read()
    assert state.saved_ids == frozenset() and state.uploaded_ids == frozenset()
    assert state.acked_ids == frozenset() and state.last_error == ""
    with pytest.raises(FileNotFoundError):
        observe.FileDiskSource(tmp_path / "a", tmp_path / "b", tmp_path / "nope").read()


def test_disk_source_rejects_malformed_files_and_caches(tmp_path: Path) -> None:
    saved, data = tmp_path / "Crowley.lua", tmp_path / "Data.lua"
    saved.write_bytes(b"CrowleyDB = {}\n")
    data.write_bytes(DATA)
    with pytest.raises(desktop_observe.ObserveError, match="scans"):
        observe.FileDiskSource(saved, tmp_path / "c", data).read()
    saved.write_bytes(SAVED)
    source = observe.FileDiskSource(saved, tmp_path / "c", data)
    first = source.read()
    saved.write_bytes(copy.copy(SAVED))  # rewritten identically, but the cache key is mtime/size
    assert source.read().saved_ids == first.saved_ids
    data.write_bytes(b'CROWLEY_DATA = {\n["ackedScans"] = {1},\n}\n')
    with pytest.raises(desktop_observe.ObserveError, match="ackedScans"):
        source.read()
