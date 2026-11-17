from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import crowley_automation.desktop_observe as desktop_observe
import crowley_automation.startup as startup_module
import pytest
from crowley_automation.config import AutomationConfig
from crowley_automation.desktop_input import GuiLease
from crowley_automation.desktop_observe import NativeWorldObservation, ScreenCanvas, WindowState, X11Window
from crowley_automation.desktop_session import DesktopSession, DesktopSessionUnavailable
from crowley_automation.gui.capture import CaptureError
from crowley_automation.gui.process import ProcessIdentity
from crowley_automation.startup import (
    StartupRecovery,
    StartupStatus,
    _BlockEpisode,
    _HoldEpisode,
    _single_instance_enabled,
)
from crowley_automation.startup_ui import StartupUIError, UIEvidence, UIKind
from PIL import Image


def _config(game_root: Path) -> AutomationConfig:
    return AutomationConfig(
        wine_prefix=str(game_root / "wine"),
        game_root=str(game_root),
        display=":0",
        window_title="World of Warcraft",
        game_executable="Wow.exe",
        game_argv0="C:\\wow.exe",
        window_class="wow.exe",
        product="wowf",
        product_dir="_forever_",
        character="Tester",
        realm="Realm",
        character_select_name="Tester",
        faction="Alliance",
        house="us-normal-alliance",
        build="1.60.1.70938",
        interface=16001,
        native_region=110,
        canvas_crop=(0, 0, 256, 256),
    )


def _recovery(game_root: Path) -> StartupRecovery:
    recovery = StartupRecovery.__new__(StartupRecovery)
    recovery.config = _config(game_root)
    return recovery


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def wall(self) -> float:
        return 10_000.0 + self.value


class _SessionProvider:
    def __init__(self, authority: Path) -> None:
        self.available = False
        self.session = DesktopSession({"DISPLAY": ":0", "XAUTHORITY": str(authority)}, "same")

    def current(self) -> DesktopSession:
        if not self.available:
            raise DesktopSessionUnavailable("desktop_session_unavailable")
        return self.session


class _Lease:
    def __init__(self) -> None:
        self.held = False
        self.fd = 55

    @contextmanager
    def hold(self) -> Iterator[None]:
        self.held = True
        try:
            yield
        finally:
            self.held = False


def _fake_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock()
    provider = _SessionProvider(tmp_path / "Xauthority")
    lease = _Lease()
    presence = [False]
    monkeypatch.setattr(startup_module, "maintenance_blocker", lambda: None)
    recovery = StartupRecovery(
        _config(tmp_path),
        stopping=lambda: False,
        session_provider=provider,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
        lease_factory=lambda _path: lease,  # type: ignore[arg-type]
    )
    recovery._presence_active = lambda _environment: (presence[0], "test")  # type: ignore[method-assign]
    recovery._battle_net_unknown_window_candidates = lambda *_args: 0  # type: ignore[method-assign]
    return recovery, clock, provider, lease, presence


def _write_build_info(root: Path, rows: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".build.info").write_text(
        "Product!STRING:0|Active!DEC:1|Version!STRING:0\n" + rows,
        encoding="utf-8",
    )


def test_disk_build_requires_one_active_matching_product_row(tmp_path: Path) -> None:
    _write_build_info(
        tmp_path,
        "wowf|1|1.60.1.70938\nwowc|1|1.60.1.71000\n",
    )
    _recovery(tmp_path)._require_disk_build()


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ("wowc|1|1.60.1.70938\n", "client_build_active_product_missing"),
        (
            "wowf|1|1.60.1.70938\nwowf|1|1.60.1.70938\n",
            "client_build_active_product_ambiguous",
        ),
        ("wowf|1|1.60.1.71000\n", "client_build_mismatch"),
        ("wowf|1\n", "client_build_info_malformed"),
    ],
)
def test_disk_build_refuses_missing_ambiguous_or_changed_rows(tmp_path: Path, rows: str, reason: str) -> None:
    _write_build_info(tmp_path, rows)
    with pytest.raises(RuntimeError, match=reason):
        _recovery(tmp_path)._require_disk_build()


def test_restart_reason_outside_frozen_allowlist_never_inspects_process(
    tmp_path: Path,
) -> None:
    recovery = StartupRecovery(
        _config(tmp_path),
        stopping=lambda: False,
        session_provider=object(),  # type: ignore[arg-type]
    )
    status = recovery.request_restart(
        ProcessIdentity(17, 99, 1000, "C:\\wow.exe", str(tmp_path / "wine")),
        "blocking_ui",
    )
    assert status.outcome == "blocked"
    assert status.reason == "restart_reason_not_authorized"
    assert not recovery.active


def test_hidden_owned_window_is_locatable_but_never_valid_geometry() -> None:
    commands: list[tuple[str, ...]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        command = tuple(argv)
        commands.append(command)
        if command[:2] == ("xdotool", "search"):
            return 0, "42\n"
        if command[:2] == ("xdotool", "getwindowname"):
            return 0, "World of Warcraft\n"
        if command[:2] == ("xdotool", "getwindowpid"):
            return 0, "17\n"
        if command == ("xdotool", "getdisplaygeometry"):
            return 0, "1920 1080\n"
        if command[:2] == ("xwininfo", "-id") and "-children" in command:
            return 0, '  0x2b "client" 1920x1080+0+0\n'
        if command[:2] == ("xwininfo", "-id"):
            return 0, "  Map State: IsUnMapped\n  Override Redirect State: no\n"
        if command[:2] == ("xdotool", "getwindowgeometry"):
            return 0, "X=0\nY=0\nWIDTH=1920\nHEIGHT=1080\n"
        if command[:2] == ("xprop", "-id"):
            return 0, "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"
        if command == ("xdotool", "getwindowfocus"):
            return 0, "0\n"
        if command[:2] == ("xprop", "-root"):
            return 0, "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"
        raise AssertionError(command)

    state = X11Window("wow.exe", "World of Warcraft", run).observe([17], include_hidden=True)
    assert state.xid == 42
    assert state.visible is False
    assert state.ok is False
    assert "window_not_viewable" in state.detail
    assert ("xdotool", "search", "--class", "^wow\\.exe$") in commands


def test_unmapped_override_redirect_duplicate_title_is_not_the_main_window() -> None:
    helper_id = int("1400016", 16)
    main_id = int("1400017", 16)

    def run(argv: list[str]) -> tuple[int, str]:
        command = tuple(argv)
        if command[:2] == ("xdotool", "search"):
            return 0, f"{helper_id}\n{main_id}\n"
        if command[:2] == ("xdotool", "getwindowname"):
            return 0, "Battle.net\n"
        if command[:2] == ("xdotool", "getwindowpid"):
            return 0, "33913\n"
        if command == ("xdotool", "getdisplaygeometry"):
            return 0, "1920 1080\n"
        if command[:2] == ("xwininfo", "-id") and "-children" in command:
            return 0, '  0x2b "client" 1920x1080+0+0\n'
        if command[:2] == ("xwininfo", "-id"):
            if command[2] == hex(helper_id):
                return 0, "  Map State: IsUnMapped\n  Override Redirect State: yes\n"
            return 0, "  Map State: IsViewable\n  Override Redirect State: no\n"
        if command[:2] == ("xdotool", "getwindowgeometry"):
            return 0, "X=0\nY=0\nWIDTH=1920\nHEIGHT=1080\n"
        if command[:2] == ("xprop", "-id"):
            return 0, "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"
        if command == ("xdotool", "getwindowfocus"):
            return 0, f"{main_id}\n"
        raise AssertionError(command)

    state = X11Window("battle.net.exe", "Battle.net", run).observe([33913], include_hidden=True)
    assert state.xid == main_id
    assert state.count == 1
    assert state.visible
    assert state.ok


def test_visible_override_redirect_duplicate_title_still_blocks() -> None:
    helper_id = int("1400016", 16)
    main_id = int("1400017", 16)

    def run(argv: list[str]) -> tuple[int, str]:
        command = tuple(argv)
        if command[:2] == ("xdotool", "search"):
            return 0, f"{helper_id}\n{main_id}\n"
        if command[:2] == ("xdotool", "getwindowname"):
            return 0, "Battle.net\n"
        if command[:2] == ("xdotool", "getwindowpid"):
            return 0, "33913\n"
        if command[:2] == ("xwininfo", "-id"):
            return 0, "  Map State: IsViewable\n  Override Redirect State: yes\n"
        if command == ("xdotool", "getdisplaygeometry"):
            return 0, "1920 1080\n"
        raise AssertionError(command)

    state = X11Window("battle.net.exe", "Battle.net", run).observe([33913], include_hidden=True)
    assert state.xid is None
    assert state.count == 2
    assert state.foreign_count == 0
    assert "window_count=2" in state.detail


def test_same_title_foreign_process_window_blocks_an_owned_candidate() -> None:
    def run(argv: list[str]) -> tuple[int, str]:
        command = tuple(argv)
        if command[:2] == ("xdotool", "search"):
            return 0, "42\n99\n"
        if command[:2] == ("xdotool", "getwindowname"):
            return 0, "World of Warcraft\n"
        if command[:2] == ("xdotool", "getwindowpid"):
            return 0, "17\n" if command[-1] == "42" else "23\n"
        if command == ("xdotool", "getdisplaygeometry"):
            return 0, "1920 1080\n"
        raise AssertionError(command)

    state = X11Window("wow.exe", "World of Warcraft", run).observe([17])
    assert state.xid is None
    assert state.count == 2
    assert state.foreign_count == 1
    assert state.ok is False


@pytest.mark.parametrize(
    "atoms",
    [
        "_NET_WM_STATE_FULLSCREEN, _NET_WM_STATE_FOCUSED",
        "_NET_WM_STATE_FOCUSED, _NET_WM_STATE_FULLSCREEN",
    ],
)
def test_fullscreen_atom_parser_accepts_comma_separated_atoms_in_either_order(atoms: str) -> None:
    window = X11Window(
        "wow.exe",
        "World of Warcraft",
        lambda _argv: (0, f"_NET_WM_STATE(ATOM) = {atoms}"),
    )
    assert window._fullscreen(42)


def test_fullscreen_atom_parser_rejects_substring_or_command_failure() -> None:
    lookalike = X11Window(
        "wow.exe",
        "World of Warcraft",
        lambda _argv: (0, "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREENISH"),
    )
    failed = X11Window("wow.exe", "World of Warcraft", lambda _argv: (1, "_NET_WM_STATE_FULLSCREEN"))
    assert not lookalike._fullscreen(42)
    assert not failed._fullscreen(42)


def test_episode_clock_waits_for_session_and_pauses_during_presence_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, lease, presence = _fake_recovery(tmp_path, monkeypatch)
    recovery._step_locked = lambda _session: recovery._wait("fake", "bounded_progress")  # type: ignore[method-assign]

    missing = recovery.step()
    assert missing.outcome == "held"
    assert missing.deadline_at is None
    assert not recovery.active

    clock.value = 700.0  # desktop unavailable longer than the complete episode budget
    provider.available = True
    first = recovery.step()
    assert first.outcome == "waiting"
    assert first.deadline_at == clock.wall() + 600.0
    assert recovery.active

    clock.value = 710.0
    presence[0] = True
    held = recovery.step()
    assert held.outcome == "held"
    assert held.reason == "physical_presence_hold"
    assert held.deadline_at is None
    assert not recovery.active

    clock.value = 5_000.0
    presence[0] = False
    resumed = recovery.step()
    assert resumed.outcome == "waiting"
    assert resumed.deadline_at == clock.wall() + 590.0
    assert recovery.active

    clock.value = 5_591.0
    expired = recovery.step()
    assert expired.outcome == "failed"
    assert expired.reason == "episode_deadline_expired"
    assert not recovery.active
    assert not lease.held


def test_ready_is_freshly_observed_on_every_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    observations = 0

    def observe(_session: DesktopSession):
        nonlocal observations
        observations += 1
        return recovery._finish("ready", "native_ready", "fresh_matching_world", native=True)

    recovery._step_locked = observe  # type: ignore[method-assign]
    assert recovery.step().outcome == "ready"
    assert recovery.step().outcome == "ready"
    assert observations == 2


def test_safe_login_refusal_reobserves_after_backoff_but_not_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    observations = 0

    def observe(_session: DesktopSession):
        nonlocal observations
        observations += 1
        if observations == 1:
            raise _BlockEpisode("battle_net_login_required")
        return recovery._finish("ready", "native_ready", "login_resolved_and_world_ready", native=True)

    recovery._step_locked = observe  # type: ignore[method-assign]
    recovery._play_attempts = 2
    recovery._play_deadline = 42.0
    episode_deadline = 600.0
    blocked = recovery.step()
    assert blocked.outcome == "blocked"
    assert blocked.retry_at == clock.wall() + 5.0
    assert recovery.step() == blocked
    assert observations == 1
    clock.value = 5.0
    resumed = recovery.step()
    assert resumed.outcome == "ready"
    assert resumed.reason == "login_resolved_and_world_ready"
    assert observations == 2
    assert recovery._play_attempts == 2
    assert recovery._play_deadline == 42.0
    assert recovery._episode_deadline == episode_deadline


def test_unknown_ui_reobservation_preserves_prior_attempts_and_original_episode_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    observations = 0

    def observe(_session: DesktopSession):
        nonlocal observations
        observations += 1
        if observations == 1:
            raise _BlockEpisode("battle_net_ui_unrecognized")
        return recovery._wait("battle_net_ready", "still_unrecognized")

    recovery._step_locked = observe  # type: ignore[method-assign]
    recovery._play_attempts = 2
    recovery._play_deadline = 38.0
    assert recovery.step().reason == "battle_net_ui_unrecognized"
    assert recovery._episode_deadline == 600.0
    clock.value = 5.0
    assert recovery.step().reason == "still_unrecognized"
    assert recovery._play_attempts == 2
    assert recovery._play_deadline == 38.0
    assert recovery._episode_deadline == 600.0
    clock.value = 601.0
    expired = recovery.step()
    assert expired.outcome == "failed"
    assert expired.reason == "episode_deadline_expired"
    assert observations == 2


def test_startup_lease_contention_returns_promptly_as_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lock_path = private_dir / "gui.lock"
    monkeypatch.setattr(startup_module, "gui_lock_path", lambda: lock_path)
    monkeypatch.setattr(startup_module, "maintenance_blocker", lambda: None)
    provider = _SessionProvider(tmp_path / "Xauthority")
    provider.available = True
    recovery = StartupRecovery(
        _config(tmp_path),
        stopping=lambda: False,
        session_provider=provider,  # type: ignore[arg-type]
    )
    recovery._presence_active = lambda _environment: (False, "test")  # type: ignore[method-assign]
    holder = GuiLease(lock_path)
    started = time.monotonic()
    with holder.hold():
        status = recovery.step()
    assert time.monotonic() - started < 0.5
    assert status.outcome == "held"
    assert status.reason == "gui_busy"
    assert not recovery.active


def test_activate_cleanup_failure_is_retained_and_does_not_unlock_gui_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._step_locked = lambda _session: recovery._wait("fake", "bounded_progress")  # type: ignore[method-assign]
    assert recovery.step().outcome == "waiting"
    identity = ProcessIdentity(17, 100, 1000, "C:\\wow.exe", str(tmp_path / "wine"))

    class FailedRunner:
        child_pid = None
        cleanup_failed = True

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(returncode=0)

        def close(self) -> None:
            self.cleanup_failed = True

    runner = FailedRunner()

    def new_control(*_args, **_kwargs):
        recovery._control = runner  # type: ignore[assignment]
        return runner

    monkeypatch.setattr(recovery, "_guard_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recovery, "_new_control", new_control)
    monkeypatch.setattr(
        recovery,
        "_input_tool",
        lambda: SimpleNamespace(proc_path="/pinned/xdotool", image=SimpleNamespace(fd=9)),
    )
    with pytest.raises(startup_module.ControlChildError, match="cleanup"):
        recovery._activate(provider.session, identity, 42, game=True)
    assert recovery._control is runner
    assert not recovery._release_episode()
    assert recovery.active
    assert lease.held


@pytest.mark.parametrize(
    "raw",
    [
        '{"Client":{"SingleInstance":"false"}}',
        '{"Elsewhere":{"SingleInstance":"true"},"Client":{"SingleInstance":"false"}}',
        '{"Client":{"SingleInstance":"true","SingleInstance":"false"}}',
        "not-json",
    ],
)
def test_only_exact_client_single_instance_string_true_is_accepted(raw: str) -> None:
    assert not _single_instance_enabled(raw)


def test_exact_client_single_instance_string_true_is_accepted() -> None:
    assert _single_instance_enabled('{"Client":{"SingleInstance":"true"}}')


def _add_fake_battle_net_proc(
    root: Path,
    pid: int,
    argv0: str,
    wine_prefix: str,
    *,
    arguments: tuple[str, ...] = (),
    comm: str = "Battle.net.exe",
) -> None:
    proc = root / str(pid)
    proc.mkdir()
    (proc / "comm").write_text(comm, encoding="utf-8")
    (proc / "cmdline").write_bytes("\0".join((argv0, *arguments)).encode() + b"\0")
    (proc / "environ").write_bytes(f"WINEPREFIX={wine_prefix}\0".encode())


def test_battle_net_identity_uses_exact_argv0_uid_prefix_and_stable_ticks_not_comm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wine_prefix = str(tmp_path / "wine")
    expected = str(startup_module._battle_net_executable(wine_prefix))
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_fake_battle_net_proc(proc_root, 101, expected, wine_prefix, comm="CrBrowserMain")
    _add_fake_battle_net_proc(proc_root, 102, "/other/Battle.net.exe", wine_prefix)
    _add_fake_battle_net_proc(proc_root, 103, expected, str(tmp_path / "other-prefix"))
    _add_fake_battle_net_proc(
        proc_root,
        104,
        expected,
        wine_prefix,
        arguments=("--type=renderer",),
        comm="CrBrowserMain",
    )
    ticks = {101: 31693, 102: 31694, 103: 31695, 104: 31697}
    current_uid = os.getuid()
    monkeypatch.setattr(startup_module, "process_start_ticks", lambda pid: ticks.get(pid))

    identities = startup_module._battle_net_identities(wine_prefix, proc_root=proc_root)
    assert identities == (ProcessIdentity(101, 31693, current_uid, expected, wine_prefix),)

    monkeypatch.setattr(startup_module.os, "getuid", lambda: current_uid + 1)
    assert startup_module._battle_net_identities(wine_prefix, proc_root=proc_root) == ()


def test_battle_net_identity_fails_closed_on_argument_bearing_same_prefix_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wine_prefix = str(tmp_path / "wine")
    expected = str(startup_module._battle_net_executable(wine_prefix))
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _add_fake_battle_net_proc(
        proc_root,
        104,
        expected,
        wine_prefix,
        arguments=("--profile",),
        comm="CrBrowserMain",
    )
    monkeypatch.setattr(startup_module, "process_start_ticks", lambda _pid: 31696)
    with pytest.raises(_BlockEpisode, match="battle_net_process_arguments_unexpected"):
        startup_module._battle_net_identities(wine_prefix, proc_root=proc_root)


def test_stop_rechecked_at_detached_battle_net_launch_boundary_prevents_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._step_locked = lambda _session: recovery._wait("fake", "bounded_progress")  # type: ignore[method-assign]
    assert recovery.step().outcome == "waiting"
    executable = tmp_path / "Battle.net.exe"
    executable.write_bytes(b"inert test file")
    monkeypatch.setattr(startup_module, "_reviewed_wine_path", lambda: "/reviewed/wine")
    monkeypatch.setattr(startup_module, "_battle_net_executable", lambda _prefix: executable)
    monkeypatch.setattr(
        startup_module, "_battle_net_config", lambda _prefix: '{"Client":{"SingleInstance":"true"}}'
    )
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: ())
    stopped = [False]
    guard_checks = 0

    def guard_launch(session: DesktopSession) -> None:
        nonlocal guard_checks
        guard_checks += 1
        recovery._guard_common(session)
        if guard_checks == 2:
            stopped[0] = True

    recovery._stopping = lambda: stopped[0]
    recovery._guard_launch = guard_launch  # type: ignore[method-assign]
    launches: list[list[str]] = []
    recovery._popen = lambda argv, **_kwargs: launches.append(list(argv))
    with pytest.raises(_HoldEpisode, match="stop_requested"):
        recovery._launch_battle_net(provider.session)
    assert not launches


@pytest.mark.parametrize(
    ("game_appears_at_check", "play_attempts", "expected_outcome", "expected_action"),
    [
        (2, 0, "held", None),
        (3, 0, "held", None),
        (None, 0, "waiting", "play"),
        (None, 3, "failed", None),
    ],
)
def test_battle_net_play_rechecks_for_a_new_owned_game_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    game_appears_at_check: int | None,
    play_attempts: int,
    expected_outcome: str,
    expected_action: str | None,
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._play_attempts = play_attempts
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    game = ProcessIdentity(31, 300, 1000, "Wow.exe", str(tmp_path / "wine"))
    main = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    game_checks = 0

    def game_identities() -> tuple[ProcessIdentity, ...]:
        nonlocal game_checks
        game_checks += 1
        if game_appears_at_check is not None and game_checks >= game_appears_at_check:
            return (game,)
        return ()

    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", game_identities)
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: main)
    monkeypatch.setattr(
        recovery,
        "_battle_net_updates_window",
        lambda *_args: WindowState(None, 0, False, False, (1920, 1080), visible=False),
    )
    monkeypatch.setattr(
        recovery,
        "_screen_observation",
        lambda identity, _session, *, game: SimpleNamespace(
            identity=identity,
            window=main,
            image=object(),
            ocr=object(),
            reader=object(),
            session_fingerprint="same",
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net",
        lambda _screen: UIEvidence(UIKind.PLAY_READY, "play_ready", (100, 100, 200, 50), "Play"),
    )
    actions: list[str] = []

    def play_click(session, current, _screen, _evidence, *, action: str) -> None:
        recovery._guard_identity(session, current, 42, game=False, require_focus=True)
        recovery._guard_identity(session, current, 42, game=False, require_focus=True)
        actions.append(action)

    monkeypatch.setattr(recovery, "_click_battle_net_control", play_click)
    status = recovery.step()
    assert status.outcome == expected_outcome, status
    assert actions == ([] if expected_action is None else [expected_action])
    if expected_outcome == "held":
        assert status.reason == "owned_game_process_appeared_before_battle_net_action"
    elif expected_outcome == "failed":
        assert status.reason == "play_attempt_limit_reached"


def test_observed_battle_net_disappearance_never_spawns_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    recovery._known_bnet_identity = identity
    launches: list[str] = []
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: ())
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_launch_battle_net", lambda _session: launches.append("launch"))

    status = recovery.step()

    assert status.outcome == "blocked"
    assert status.reason == "battle_net_disappeared_after_observation"
    assert launches == []


def test_disappeared_battle_net_waits_only_for_original_play_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    recovery._known_bnet_identity = identity
    recovery._play_deadline = 20.0
    recovery._play_attempts = 1
    launches: list[str] = []
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: ())
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_launch_battle_net", lambda _session: launches.append("launch"))

    waiting = recovery.step()
    assert waiting.outcome == "waiting"
    assert waiting.reason == "waiting_for_game_after_battle_net_exit"
    assert waiting.deadline_at == pytest.approx(10_020.0)
    assert recovery._play_deadline == 20.0
    assert recovery._play_attempts == 1
    assert launches == []

    clock.value = 20.0
    blocked = recovery.step()
    assert blocked.outcome == "blocked"
    assert blocked.reason == "battle_net_disappeared_after_observation"
    assert recovery._play_attempts == 1
    assert launches == []


@pytest.mark.parametrize("appears", ["game", "battle_net", "none"])
def test_detached_battle_net_launch_rechecks_game_and_battle_net_ownership_after_prechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, appears: str
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._step_locked = lambda _session: recovery._wait("fake", "bounded_progress")  # type: ignore[method-assign]
    assert recovery.step().outcome == "waiting"
    executable = tmp_path / "Battle.net.exe"
    executable.write_bytes(b"inert test file")
    game = ProcessIdentity(31, 300, 1000, "Wow.exe", str(tmp_path / "wine"))
    bnet = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    game_checks = 0
    bnet_checks = 0

    def game_identities() -> tuple[ProcessIdentity, ...]:
        nonlocal game_checks
        game_checks += 1
        return (game,) if appears == "game" and game_checks >= 2 else ()

    def bnet_identities(_prefix: str) -> tuple[ProcessIdentity, ...]:
        nonlocal bnet_checks
        bnet_checks += 1
        return (bnet,) if appears == "battle_net" and bnet_checks >= 3 else ()

    monkeypatch.setattr(recovery, "_game_identities", game_identities)
    monkeypatch.setattr(startup_module, "_battle_net_identities", bnet_identities)
    monkeypatch.setattr(startup_module, "_reviewed_wine_path", lambda: "/reviewed/wine")
    monkeypatch.setattr(startup_module, "_battle_net_executable", lambda _prefix: executable)
    monkeypatch.setattr(
        startup_module, "_battle_net_config", lambda _prefix: '{"Client":{"SingleInstance":"true"}}'
    )
    launches: list[list[str]] = []
    recovery._popen = lambda argv, **_kwargs: launches.append(list(argv))

    if appears == "none":
        recovery._launch_battle_net(provider.session)
        assert len(launches) == 1
    else:
        with pytest.raises(_HoldEpisode):
            recovery._launch_battle_net(provider.session)
        assert launches == []


def test_uncertain_control_cleanup_keeps_the_canonical_lease_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._step_locked = lambda _session: recovery._wait("fake", "bounded_progress")  # type: ignore[method-assign]
    assert recovery.step().outcome == "waiting"
    assert recovery.active

    class FailedCleanup:
        child_pid = None
        cleanup_failed = True

        def close(self) -> None:
            pass

    recovery._control = FailedCleanup()  # type: ignore[assignment]
    assert not recovery._release_episode()
    assert recovery.active
    assert lease.held


def test_cold_start_adopts_battle_net_then_selects_product_play_and_world_without_blind_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    game_identity = ProcessIdentity(17, 100, 1000, "C:\\wow.exe", str(tmp_path / "wine"))
    bnet_identity = ProcessIdentity(
        21,
        200,
        1000,
        str(tmp_path / "wine/drive_c/Program Files (x86)/Battle.net/Battle.net.exe"),
        str(tmp_path / "wine"),
    )
    bnet_identities: list[ProcessIdentity] = []
    game_identities: list[ProcessIdentity] = []
    bnet_window = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    game_window = WindowState(44, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080), fullscreen=True)
    app_actions: list[str] = []
    world_ready = [False]

    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: tuple(bnet_identities))
    monkeypatch.setattr(
        recovery,
        "_game_identities",
        lambda: tuple(game_identities),
    )
    monkeypatch.setattr(recovery, "_launch_battle_net", lambda _session: app_actions.append("launch_bnet"))
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: bnet_window)
    monkeypatch.setattr(
        recovery,
        "_battle_net_updates_window",
        lambda *_args: WindowState(None, 0, False, False, (1920, 1080), visible=False),
    )
    monkeypatch.setattr(recovery, "_game_window", lambda *_args: game_window)
    monkeypatch.setattr(recovery, "_guard_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        recovery,
        "_screen_observation",
        lambda identity, _session, *, game: SimpleNamespace(
            identity=identity,
            window=game_window if game else bnet_window,
            image=object(),
            ocr=object(),
            reader=object(),
            session_fingerprint="same",
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net",
        lambda _screen: UIEvidence(UIKind.PLAY_READY, "play_ready", (100, 100, 200, 50), "Play"),
    )
    monkeypatch.setattr(
        recovery,
        "_click_battle_net_control",
        lambda _session, _identity, _screen, _evidence, *, action: app_actions.append(action),
    )
    monkeypatch.setattr(
        startup_module,
        "classify_character_select_capture",
        lambda *_args, **_kwargs: UIEvidence(
            UIKind.CHARACTER_SELECT,
            "expected_selected_name_and_enabled_enter_world",
            (850, 982, 207, 34),
            "Enter World",
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_click_game_control",
        lambda *_args: app_actions.append("enter_world"),
    )

    def observe_native(_config: AutomationConfig, **_kwargs) -> NativeWorldObservation:
        return NativeWorldObservation(
            game_identity,
            game_window,
            {"boot": "boot-1", "character": "Tester", "realm": "Realm"} if world_ready[0] else None,
            clock.wall(),
            "same",
        )

    monkeypatch.setattr(startup_module, "observe_native_world", observe_native)

    assert recovery.step().action == "battle_net_launch_dispatched"
    bnet_identities.append(bnet_identity)
    clock.value += 1.1
    assert recovery.step().action == "play_click_dispatched"
    game_identities.append(game_identity)
    clock.value += 1.1
    assert recovery.step().action == "enter_world_click_dispatched"
    world_ready[0] = True
    clock.value += 1.1
    ready = recovery.step()
    assert ready.outcome == "ready"
    assert ready.native_ready
    assert app_actions == ["launch_bnet", "play", "enter_world"]


def test_existing_hidden_decorated_game_window_is_restored_and_repaired_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(17, 100, 1000, "C:\\wow.exe", str(tmp_path / "wine"))
    hidden = WindowState(
        44, 1, False, False, (1920, 1080), "window_not_viewable", visible=False, fullscreen=False
    )
    restored = WindowState(
        44, 1, False, True, (1920, 1080), "window_not_fullscreen", visible=True, fullscreen=False
    )
    healthy = WindowState(44, 1, True, True, (1920, 1080), fullscreen=True)
    states = [hidden]
    actions: list[tuple[str, int]] = []
    monkeypatch.setattr(recovery, "_game_identities", lambda: (identity,))
    monkeypatch.setattr(recovery, "_game_window", lambda *_args: states[0])
    monkeypatch.setattr(
        recovery,
        "_activate",
        lambda _session, _identity, xid, *, game: (
            actions.append(("restore", xid)),
            states.__setitem__(0, restored),
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_repair_fullscreen",
        lambda _session, _identity, xid: (actions.append(("repair", xid)), states.__setitem__(0, healthy)),
    )
    monkeypatch.setattr(
        startup_module,
        "observe_native_world",
        lambda _config, **_kwargs: NativeWorldObservation(identity, healthy, {"boot": "boot-1"}, 1.0, "same"),
    )
    assert recovery.step().action == "window_activation_dispatched"
    clock.value += 1.1
    assert recovery.step().action == "fullscreen_repair_dispatched"
    clock.value += 1.1
    assert recovery.step().outcome == "ready"
    assert actions == [("restore", 44), ("repair", 44)]


@pytest.mark.parametrize(
    ("evidence", "outcome", "reason"),
    [
        (UIEvidence(UIKind.LOGIN_REQUIRED, "login_required_visible"), "blocked", "battle_net_login_required"),
        (
            UIEvidence(UIKind.ACCOUNT_CONFLICT, "account_conflict_visible"),
            "blocked",
            "battle_net_account_conflict",
        ),
        (
            UIEvidence(UIKind.UPDATE_BUSY, "update_or_install_visible"),
            "waiting",
            "battle_net_update_or_install_busy",
        ),
        (UIEvidence(UIKind.UNKNOWN, "launcher_ui_unrecognized"), "blocked", "battle_net_ui_unrecognized"),
    ],
)
def test_unqualified_battle_net_states_never_click_play(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: UIEvidence,
    outcome: str,
    reason: str,
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    bnet_identity = ProcessIdentity(
        21,
        200,
        1000,
        str(tmp_path / "wine/drive_c/Program Files (x86)/Battle.net/Battle.net.exe"),
        str(tmp_path / "wine"),
    )
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (bnet_identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(
        recovery,
        "_battle_net_window",
        lambda *_args: WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080)),
    )
    monkeypatch.setattr(
        recovery,
        "_battle_net_updates_window",
        lambda *_args: WindowState(None, 0, False, False, (1920, 1080), visible=False),
    )
    monkeypatch.setattr(
        recovery,
        "_screen_observation",
        lambda *_args, **_kwargs: SimpleNamespace(
            identity=bnet_identity,
            window=WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080)),
        ),
    )
    monkeypatch.setattr(recovery, "_classify_battle_net", lambda _screen: evidence)
    actions: list[str] = []
    monkeypatch.setattr(
        recovery,
        "_click_battle_net_control",
        lambda *_args, **_kwargs: actions.append("click"),
    )
    status = recovery.step()
    assert status.outcome == outcome
    assert status.reason == reason
    assert not actions


def test_measured_updates_child_is_activated_closed_once_and_must_disappear_before_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(
        21,
        200,
        1000,
        str(tmp_path / "wine/drive_c/Program Files (x86)/Battle.net/Battle.net.exe"),
        str(tmp_path / "wine"),
    )
    main = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    updates = [WindowState(60, 1, False, False, (1920, 1080), outer_rect=(528, 234, 900, 600))]
    no_updates = WindowState(None, 0, False, False, (1920, 1080), visible=False)
    actions: list[str] = []
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: main)
    monkeypatch.setattr(recovery, "_battle_net_updates_window", lambda *_args: updates[0])
    monkeypatch.setattr(recovery, "_require_disk_build", lambda: None)
    monkeypatch.setattr(recovery, "_guard_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        recovery,
        "_screen_updates_observation",
        lambda *_args: SimpleNamespace(window=updates[0]),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net_updates",
        lambda _screen: UIEvidence(
            UIKind.NEWS_MODAL,
            "verified_battle_net_updates_panel",
            (1400, 241, 10, 10),
            "Close",
            "semantic-release-evidence",
        ),
    )

    def activate(*_args) -> None:
        actions.append("activate_updates")
        updates[0] = WindowState(
            60,
            1,
            False,
            True,
            (1920, 1080),
            outer_rect=(528, 234, 900, 600),
        )

    monkeypatch.setattr(recovery, "_activate_updates_window", activate)
    monkeypatch.setattr(
        recovery, "_click_battle_net_updates_close", lambda *_args: actions.append("close_updates")
    )
    monkeypatch.setattr(
        recovery,
        "_screen_observation",
        lambda identity, _session, *, game: SimpleNamespace(
            identity=identity,
            window=main,
            image=object(),
            ocr=object(),
            reader=object(),
            session_fingerprint="same",
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net",
        lambda _screen: UIEvidence(UIKind.PLAY_READY, "play_ready", (100, 100, 200, 50), "Play"),
    )
    monkeypatch.setattr(
        recovery,
        "_click_battle_net_control",
        lambda *_args, **kwargs: actions.append(kwargs["action"]),
    )

    assert recovery.step().action == "window_activation_dispatched"
    clock.value += 1.1
    assert recovery.step().action == "news_close_click_dispatched"
    clock.value += 1.1
    assert recovery.step().reason == "verifying_battle_net_updates_dismissal"
    assert actions == ["activate_updates", "close_updates"]
    updates[0] = no_updates
    clock.value += 1.1
    assert recovery.step().reason == "battle_net_updates_disappeared_reobserving_main"
    clock.value += 1.1
    assert recovery.step().action == "play_click_dispatched"
    assert actions == ["activate_updates", "close_updates", "play"]


@pytest.mark.parametrize(
    ("window", "expected_reason"),
    [
        (
            WindowState(
                None,
                1,
                False,
                False,
                (1920, 1080),
                "foreign_window_candidate",
                visible=False,
                foreign_count=1,
            ),
            "foreign_battle_net_updates_window_candidate",
        ),
        (
            WindowState(None, 2, False, False, (1920, 1080), "window_count=2", visible=False),
            "multiple_battle_net_updates_windows",
        ),
    ],
)
def test_foreign_or_duplicate_updates_children_block_without_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: WindowState,
    expected_reason: str,
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(
        recovery,
        "_battle_net_window",
        lambda *_args: WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080)),
    )
    monkeypatch.setattr(recovery, "_battle_net_updates_window", lambda *_args: window)
    actions: list[str] = []
    monkeypatch.setattr(recovery, "_activate_updates_window", lambda *_args: actions.append("activate"))
    monkeypatch.setattr(recovery, "_click_battle_net_updates_close", lambda *_args: actions.append("close"))
    status = recovery.step()
    assert status.outcome == "blocked"
    assert status.reason == expected_reason
    assert actions == []


def test_unverified_updates_child_waits_without_closing_or_reaching_main_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    main = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    child = WindowState(60, 1, False, True, (1920, 1080), outer_rect=(528, 234, 900, 600))
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: main)
    monkeypatch.setattr(recovery, "_battle_net_updates_window", lambda *_args: child)
    monkeypatch.setattr(recovery, "_screen_updates_observation", lambda *_args: object())
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net_updates",
        lambda _screen: UIEvidence(UIKind.UNKNOWN, "battle_net_updates_heading_unverified"),
    )
    actions: list[str] = []
    monkeypatch.setattr(recovery, "_click_battle_net_updates_close", lambda *_args: actions.append("close"))
    monkeypatch.setattr(
        recovery, "_screen_observation", lambda *_args, **_kwargs: actions.append("main_capture")
    )
    status = recovery.step()
    assert status.outcome == "waiting"
    assert status.phase == "news_modal"
    assert status.reason == "battle_net_updates_heading_unverified"
    assert actions == []


def test_unknown_battlenet_window_title_blocks_main_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(
        recovery,
        "_battle_net_window",
        lambda *_args: WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080)),
    )
    monkeypatch.setattr(recovery, "_battle_net_unknown_window_candidates", lambda *_args: 1)
    actions: list[str] = []
    status = recovery.step()
    assert status.outcome == "blocked"
    assert status.reason == "unrecognized_battle_net_window_candidate"
    assert actions == []


def test_replaced_updates_child_after_close_is_a_changed_postcondition_not_a_second_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    main = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    updates = [WindowState(60, 1, False, True, (1920, 1080), outer_rect=(528, 234, 900, 600))]
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: main)
    monkeypatch.setattr(recovery, "_battle_net_updates_window", lambda *_args: updates[0])
    monkeypatch.setattr(
        recovery,
        "_screen_updates_observation",
        lambda *_args: SimpleNamespace(window=updates[0]),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net_updates",
        lambda _screen: UIEvidence(
            UIKind.NEWS_MODAL,
            "verified_battle_net_updates_panel",
            (875, 7, 10, 10),
            "Close",
            "semantic-release-evidence",
        ),
    )
    closes: list[int] = []
    monkeypatch.setattr(
        recovery,
        "_click_battle_net_updates_close",
        lambda _session, _identity, screen, _evidence: closes.append(screen.window.xid),
    )
    assert recovery.step().action == "news_close_click_dispatched"
    clock.value += 1.1
    updates[0] = WindowState(61, 1, False, True, (1920, 1080), outer_rect=(528, 234, 900, 600))
    changed = recovery.step()
    assert changed.outcome == "blocked"
    assert changed.reason == "battle_net_updates_window_changed_after_close"
    assert closes == [60]


@pytest.mark.parametrize(
    ("guard_state", "expected_outcome"),
    [
        (WindowState(None, 0, False, False, (1920, 1080), visible=False), "waiting"),
        (
            WindowState(60, 1, False, True, (1920, 1080), outer_rect=(528, 234, 900, 600)),
            "held",
        ),
        (
            WindowState(None, 1, False, False, (1920, 1080), visible=False, foreign_count=1),
            "held",
        ),
        (WindowState(None, 2, False, False, (1920, 1080), visible=False), "held"),
    ],
)
def test_main_play_guard_rechecks_for_new_updates_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_state: WindowState,
    expected_outcome: str,
) -> None:
    recovery, _clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    main = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    absent = WindowState(None, 0, False, False, (1920, 1080), visible=False)
    updates_states = [absent, guard_state]
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: main)
    monkeypatch.setattr(
        recovery,
        "_battle_net_updates_window",
        lambda *_args: updates_states.pop(0) if updates_states else guard_state,
    )
    monkeypatch.setattr(
        recovery,
        "_screen_observation",
        lambda identity, _session, *, game: SimpleNamespace(
            identity=identity,
            window=main,
            image=object(),
            ocr=object(),
            reader=object(),
            session_fingerprint="same",
        ),
    )
    monkeypatch.setattr(
        recovery,
        "_classify_battle_net",
        lambda _screen: UIEvidence(UIKind.PLAY_READY, "play_ready", (100, 100, 200, 50), "Play"),
    )
    actions: list[str] = []

    def guarded_play(session, current, _screen, _evidence, *, action: str) -> None:
        recovery._guard_identity(session, current, 42, game=False, require_focus=True)
        actions.append(action)

    monkeypatch.setattr(
        recovery,
        "_click_battle_net_control",
        guarded_play,
    )

    status = recovery.step()
    assert status.outcome == expected_outcome, status
    if expected_outcome == "held":
        assert status.reason == "battle_net_updates_window_appeared_before_main_action"
        assert actions == []
    else:
        assert status.reason == "play_click_dispatched"
        assert actions == ["play"]


def test_one_stale_read_reobserves_without_resetting_actions_or_episode_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    attempts = 0
    recovery._play_attempts = 2

    def observe(_session) -> StartupStatus:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StartupUIError("screen_capture_stale")
        return recovery._finish("ready", "native_ready", "matching_native_world_ready", native=True)

    monkeypatch.setattr(recovery, "_step_locked", observe)
    stale = recovery.step()
    assert stale.outcome == "waiting"
    assert stale.reason == "screen_capture_stale_reobserve"
    original_deadline = recovery._episode_deadline
    assert recovery._play_attempts == 2
    assert recovery.active

    clock.value += 1.1
    ready = recovery.step()
    assert ready.outcome == "ready"
    assert recovery._play_attempts == 2
    assert recovery._episode_deadline == original_deadline
    assert attempts == 2


def test_repeated_stale_reads_still_expire_the_original_episode_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True

    def stale_capture(_session) -> StartupStatus:
        raise StartupUIError("screen_capture_stale")

    monkeypatch.setattr(recovery, "_step_locked", stale_capture)
    first = recovery.step()
    assert first.outcome == "waiting"
    assert first.deadline_at is not None
    assert first.deadline_at == pytest.approx(10_600.0)
    clock.value = 600.0
    expired = recovery.step()
    assert expired.outcome == "failed"
    assert expired.reason == "episode_deadline_expired"


def test_capture_that_takes_too_long_is_waited_and_reobserved_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    identity = ProcessIdentity(21, 200, 1000, "Battle.net.exe", str(tmp_path / "wine"))
    state = WindowState(42, 1, True, True, (1920, 1080), outer_rect=(0, 0, 1920, 1080))
    monkeypatch.setattr(startup_module, "_battle_net_identities", lambda _prefix: (identity,))
    monkeypatch.setattr(recovery, "_game_identities", lambda: ())
    monkeypatch.setattr(recovery, "_battle_net_window", lambda *_args: state)
    monkeypatch.setattr(
        recovery,
        "_battle_net_updates_window",
        lambda *_args: WindowState(None, 0, False, False, (1920, 1080), visible=False),
    )

    class SlowCanvas:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def capture(self, display: tuple[int, int]) -> Image.Image:
            clock.value += 6.0
            return Image.new("RGB", display, "black")

    monkeypatch.setattr(startup_module, "ScreenCanvas", SlowCanvas)
    monkeypatch.setattr(
        recovery,
        "_reader_factory",
        lambda _environment: startup_module.TesseractReader(monotonic=clock.monotonic),
    )
    status = recovery.step()
    assert status.outcome == "waiting"
    assert status.reason == "screen_capture_stale_reobserve"
    assert recovery._episode_deadline == pytest.approx(600.0)
    assert recovery.active


def test_capture_failure_releases_lease_and_reobserves_with_original_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recovery, clock, provider, lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    recovery._bnet_launched = True
    recovery._login_deadline = 180.0
    recovery._play_deadline = 20.0
    recovery._news_close_pending = True
    recovery._news_deadline = 15.0
    recovery._play_attempts = 2
    attempts = 0

    def observe(_session) -> StartupStatus:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CaptureError("diagnostic details that must not enter status")
        return recovery._finish("ready", "native_ready", "matching_native_world_ready", native=True)

    monkeypatch.setattr(recovery, "_step_locked", observe)
    with caplog.at_level("WARNING", logger=startup_module.__name__):
        unavailable = recovery.step()
    original_deadline = recovery._episode_deadline
    assert unavailable.outcome == "waiting"
    assert unavailable.reason == "screen_capture_failed_reobserve"
    assert unavailable.deadline_at == pytest.approx(10_015.0)
    assert "diagnostic details" not in unavailable.reason
    assert not recovery.active
    assert not lease.held
    assert recovery._play_attempts == 2
    assert recovery._episode_deadline == original_deadline == pytest.approx(600.0)
    assert len(caplog.records) == 1
    assert len(caplog.records[0].getMessage()) <= 200

    clock.value += 1.1
    ready = recovery.step()
    assert ready.outcome == "ready"
    assert recovery._episode_deadline == original_deadline
    assert recovery._play_attempts == 2


def test_repeated_capture_failures_expire_original_episode_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, clock, provider, _lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    monkeypatch.setattr(
        recovery,
        "_step_locked",
        lambda _session: (_ for _ in ()).throw(CaptureError("capture unavailable")),
    )

    first = recovery.step()
    assert first.outcome == "waiting"
    original_deadline = recovery._episode_deadline
    clock.value = 600.0
    expired = recovery.step()
    assert expired.outcome == "failed"
    assert expired.reason == "episode_deadline_expired"
    assert recovery._episode_deadline == original_deadline == pytest.approx(600.0)


def test_capture_failure_with_unreaped_control_retains_gui_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery, _clock, provider, lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True

    class UnreapedControl:
        child_pid = 404
        cleanup_failed = True

        def close(self) -> None:
            raise startup_module.ControlChildError("still alive")

    control = UnreapedControl()
    recovery._control = control  # type: ignore[assignment]
    monkeypatch.setattr(
        recovery,
        "_step_locked",
        lambda _session: (_ for _ in ()).throw(CaptureError("capture unavailable")),
    )

    status = recovery.step()
    assert status.outcome == "failed"
    assert status.reason == "control_cleanup_failed"
    assert recovery.active
    assert lease.held
    assert recovery._control is control


def test_battle_net_control_failure_logs_bounded_detail_without_changing_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recovery, _clock, provider, lease, _presence = _fake_recovery(tmp_path, monkeypatch)
    provider.available = True
    detail = "runner could not verify control\n" + "diagnostic " * 50
    monkeypatch.setattr(
        recovery,
        "_step_locked",
        lambda _session: (_ for _ in ()).throw(startup_module.BattleNetPlayError(detail)),
    )

    with caplog.at_level("WARNING", logger=startup_module.__name__):
        status = recovery.step()

    assert status.outcome == "failed"
    assert status.reason == "startup_control_failed"
    assert detail not in status.reason
    assert not recovery.active
    assert not lease.held
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "BattleNetPlayError: runner could not verify control diagnostic" in message
    assert "\n" not in message
    assert len(message) <= 200


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OSError("no command"), "gnome-screenshot failed (OSError)"),
        (subprocess.TimeoutExpired("gnome-screenshot", timeout=1.0), "gnome-screenshot timed out"),
    ],
)
def test_screen_capture_runner_preserves_bounded_failure_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: str,
) -> None:
    monkeypatch.setattr(
        desktop_observe.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )
    with pytest.raises(CaptureError) as error:
        ScreenCanvas(tmp_path)._capture_run(["gnome-screenshot", "-f", "capture.png"], 1.0)
    assert str(error.value) == expected


def test_screen_capture_runner_includes_bounded_stderr_on_exit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        desktop_observe.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=9, stderr="diagnostic " + "x" * 400),
    )
    with pytest.raises(CaptureError) as error:
        ScreenCanvas(tmp_path)._capture_run(["gnome-screenshot", "-f", "capture.png"], 1.0)
    assert "gnome-screenshot exited 9: diagnostic" in str(error.value)
    assert len(str(error.value)) <= 200


@pytest.mark.parametrize("source_kind", ["mapping", "callable", "inherited"])
def test_capture_child_gets_portal_switch_without_mutating_source_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    monkeypatch.setenv("HDY_DISABLE_PORTAL", "0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", str(tmp_path / "inherited-authority"))
    monkeypatch.setenv("CAPTURE_SENTINEL", "inherited-value")
    source_environment = {
        "DISPLAY": ":7",
        "XAUTHORITY": str(tmp_path / "provided-authority"),
        "HDY_DISABLE_PORTAL": "0",
        "CAPTURE_SENTINEL": "provided-value",
    }
    source_before = dict(source_environment)
    process_environment_before = dict(os.environ)
    calls: list[tuple[list[str], dict[str, object]]] = []

    if source_kind == "mapping":
        canvas = ScreenCanvas(tmp_path, source_environment)
    elif source_kind == "callable":
        canvas = ScreenCanvas(tmp_path, lambda: source_environment)
    else:
        canvas = ScreenCanvas(tmp_path)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(desktop_observe.subprocess, "run", run)
    canvas._capture_run(["gnome-screenshot", "-f", "capture.png"], 4.25)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["gnome-screenshot", "-f", "capture.png"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 4.25
    child_environment = kwargs["env"]
    assert isinstance(child_environment, dict)
    assert child_environment["HDY_DISABLE_PORTAL"] == "1"
    if source_kind == "inherited":
        assert child_environment["DISPLAY"] == ":0"
        assert child_environment["XAUTHORITY"] == str(tmp_path / "inherited-authority")
        assert child_environment["CAPTURE_SENTINEL"] == "inherited-value"
    else:
        assert child_environment["DISPLAY"] == ":7"
        assert child_environment["XAUTHORITY"] == str(tmp_path / "provided-authority")
        assert child_environment["CAPTURE_SENTINEL"] == "provided-value"
    assert source_environment == source_before
    assert os.environ["HDY_DISABLE_PORTAL"] == "0"
    assert dict(os.environ) == process_environment_before
