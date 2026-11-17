from __future__ import annotations

import io
import json
import re
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crowley_common.frames import PLAN_COUNT
from crowley_common.game_target import load_game_target
from crowley_host.scan import cli, config, loop
from crowley_host.scan.policy import Action, Decision

from tests.ops.test_install import _write_host
from tests.support.config import fixture_automation

TARGET_FIXTURE = Path(__file__).parent / "fixtures" / "targets" / "verified-beta.json"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    for name, value in {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(runtime),
        "XAUTHORITY": str(tmp_path / "xauth"),
    }.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def test_status_prints_the_status_file_or_fails(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 1
    assert "has not run" in capsys.readouterr().err
    loop.write_private_json(loop.status_path(), {"action": "wait", "fault_code": ""})
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "wait", "fault_code": ""}


def test_paths_come_from_xdg(env: Path) -> None:
    assert loop.state_path() == env / "state" / "crowley" / "scan-loop.json"
    assert loop.status_path() == env / "runtime" / "crowley" / "scan-status.json"
    loop.write_private_json(loop.status_path(), {"a": 1})
    assert stat.S_IMODE(loop.status_path().parent.stat().st_mode) == 0o700


def test_commands_are_registered_and_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["bogus"])
    assert exc.value.code == 2


def test_run_wires_the_loop_and_restores_state(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class FakeTool:
        closed = False

        def close(self) -> None:
            FakeTool.closed = True

    class FakeLoop:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

        def run(self, *, stop: Any) -> None:
            created["ran"] = True

    loop.write_private_json(loop.state_path(), {"bad": 1})
    monkeypatch.setattr(cli, "PinnedInputTool", type("T", (), {"load": staticmethod(lambda: FakeTool())}))
    monkeypatch.setattr(cli, "load_scan_config", lambda: loaded_config(env))
    monkeypatch.setattr(cli, "ScanLoop", FakeLoop)
    with pytest.raises(loop.StateError, match="corrupt"):
        cli.main(["run"])
    assert FakeTool.closed  # the pinned tool is released even when startup fails
    loop.state_path().unlink()
    FakeTool.closed = False
    assert cli.main(["run"]) == 0
    assert created["ran"] and FakeTool.closed
    assert created["state_file"] == loop.state_path() and created["status_file"] == loop.status_path()
    assert created["actor"] is not None and created["observer"] is not None
    assert created["recovery"].session_provider is created["actor"]._session_provider
    assert created["observer"]._session_provider is created["actor"]._session_provider
    assert created["observer"]._session_environment is created["actor"]._session_environment


def test_run_types_one_guarded_command_child_with_continuous_monitoring(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crowley_automation import startup as automation_startup
    from crowley_automation.desktop_observe import WindowState
    from crowley_automation.gui.process import ProcessIdentity

    from tests.test_scan_act import XMODMAP, ready_automation, ready_observation

    events: list[str] = []
    observation = ready_observation(chat_active=True, chat_focused=True)
    identity = observation.process_identity
    assert identity is not None
    native_value = ready_automation(chat_active=True, chat_focused=True)
    window = WindowState(77, 1, True, True, (1920, 1080), fullscreen=True)

    class Session:
        fingerprint = "session-1"
        environment = {"DISPLAY": ":0", "XAUTHORITY": str(env / "xauth")}

    class SessionProvider:
        def current(self) -> Session:
            return Session()

    session_provider = SessionProvider()

    class Presence:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def human_active(self) -> tuple[bool, str]:
            return False, "idle"

    class Matcher:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return (identity,)

        def still_matches(self, expected: ProcessIdentity) -> bool:
            return expected == identity

    class WindowSource:
        def observe(self, _pids: list[int]) -> WindowState:
            return window

        def focus_state(self, xid: int, pid: int) -> tuple[bool, bool]:
            return xid == window.xid and pid == identity.pid, window.focused

    class Observer:
        window_source = WindowSource()
        window_xid = 77
        decode_error = ""

        def observe(self, state: Any) -> Any:
            events.append("paired_capture")
            return replace(observation, state=state)

    observer = Observer()

    class Tool:
        proc_path = "/proc/self/fd/9"
        image = SimpleNamespace(fd=9)

        def close(self) -> None:
            pass

    class Recovery:
        def __init__(self, _config: Any, *, session_provider: Any, **_kwargs: Any) -> None:
            self.session_provider = session_provider

        def close(self) -> None:
            pass

    class Control:
        possible_down = False
        child_pid = None
        cleanup_failed = False

        def __init__(
            self, _lease: Any, _tool: Any, *, environment: Any, guard: Any, during: Any, **_kwargs: Any
        ) -> None:
            self.environment, self.guard, self.during = environment, guard, during

        def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert self.environment()["DISPLAY"] == ":0"
            events.append("runner_environment_resolved")
            self.guard()
            key_chars = {
                "061": "/",
                "027": "r",
                "026": "e",
                "046": "l",
                "032": "o",
                "038": "a",
                "040": "d",
            }
            for index in range(1, len(argv), 2):
                if argv[index] == "keydown" and argv[index + 1] in key_chars:
                    self.during()
                    native_value["chatEdit"]["text"] += key_chars[argv[index + 1]]
            events.append("emission")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def close(self) -> None:
            pass

    actor_type = cli.Actor

    class CountingActor(actor_type):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(clock=lambda: observation.wall, **kwargs)

        def before_emit(self) -> None:
            events.append("actor_guard")
            super().before_emit()

    class MatcherConfig:
        def __new__(cls) -> Any:
            return Matcher()

    class FakeLoop:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, *, stop: Any) -> None:
            del stop
            actor = self.kwargs["actor"]
            actor._keys._query_run = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, XMODMAP, "")
            actor.execute(Decision(Action.TYPE_COMMAND, observation.state, command="/reload"), observation)

    def native_observe(*_args: Any, **_kwargs: Any) -> Any:
        events.append("native_capture")
        return SimpleNamespace(
            identity=identity,
            window=window,
            snapshot=native_value,
            session_fingerprint="session-1",
        )

    loaded = replace(loaded_config(env), matcher=MatcherConfig())
    monkeypatch.setattr(cli, "load_scan_config", lambda: loaded)
    monkeypatch.setattr(cli, "DesktopSessionProvider", lambda _display: session_provider)
    monkeypatch.setattr(cli, "Presence", Presence)
    monkeypatch.setattr(cli, "build_observer", lambda *_args, **_kwargs: observer)
    monkeypatch.setattr(cli, "PinnedInputTool", type("ToolLoader", (), {"load": staticmethod(Tool)}))
    monkeypatch.setattr(cli, "ControlChildRunner", Control)
    monkeypatch.setattr(cli, "Actor", CountingActor)
    monkeypatch.setattr(cli, "ScanLoop", FakeLoop)
    monkeypatch.setattr(cli, "maintenance_blocker", lambda: None)
    monkeypatch.setattr(cli.desktop_observe, "observe_native_world", native_observe)
    monkeypatch.setattr(automation_startup, "StartupRecovery", Recovery)

    assert cli.main(["run"]) == 0

    command_length = len("/reload")
    guard_positions = [index for index, event in enumerate(events) if event == "actor_guard"]
    assert len(guard_positions) == command_length + 1
    assert events.count("runner_environment_resolved") == 1
    assert events.count("native_capture") == 1
    assert events.count("paired_capture") == 1
    assert native_value["chatEdit"]["text"] == "/reload"
    assert events[-1] == "emission"


def loaded_config(root: Path) -> config.ScanConfig:
    from crowley_automation.gui.process import OwnedGameMatcher
    from crowley_common.frames import ProtocolContract

    from tests.test_scan_observe import TARGET

    return config.ScanConfig(
        target=TARGET,
        automation=fixture_automation(root),
        matcher=OwnedGameMatcher("Wow.exe", "C:\\wow.exe", str(root / "wine"), 1),
        window_class="wow.exe",
        window_title="World of Warcraft",
        display=":0",
        canvas_crop=(80, 48, 256, 256),
        automation_canvas_crop=(352, 48, 256, 256),
        saved_variables=root / "Crowley.lua",
        companion_state=root / "companion.json",
        data_lua=root / "Data.lua",
        contract=ProtocolContract(PLAN_COUNT, 900, 600, 20, ()),
    )


def test_load_scan_config_pins_come_from_target_host_and_installed_addon(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = load_game_target(TARGET_FIXTURE)
    monkeypatch.setattr(config, "selected_target", lambda: game)
    game_root = env / "game"
    toc = game_root / game.product_dir / "Interface" / "AddOns" / "Crowley" / "Crowley.toc"
    toc.parent.mkdir(parents=True)
    toc.write_text("## Interface: 16001\n## Version: 0.3.0\n", encoding="utf-8")
    automation_toc = toc.parent.parent / "Crowley_Automation" / "Crowley_Automation.toc"
    automation_toc.parent.mkdir(parents=True)
    automation_toc.write_text("## Interface: 16001\n## Version: 0.2.0\n", encoding="utf-8")
    _write_host(env / "config", game_root, env / "wine")
    from crowley_host.config import load_host_config
    from crowley_host.ops.install import _render_config

    host = load_host_config(env / "config" / "crowley" / "host.toml")
    automation_path = env / "config" / "crowley" / "automation.toml"
    automation_path.write_bytes(_render_config("automation.toml", host, game))
    scanner_config = env / "config" / "crowley" / "scanner.toml"
    scanner_config.write_text(
        "[scanner]\n"
        f'game_root = "{game_root}"\n'
        'wtf_account = "850981908#1"\n'
        'character = "Arthur"\n'
        'server_url = "http://server.invalid:8080"\n',
        encoding="utf-8",
    )
    loaded = config.load_scan_config()
    target = loaded.target
    assert (target.character, target.realm, target.faction) == ("Arthur", game.realm, game.faction)
    assert (target.house_key, target.build, target.interface) == (
        game.house,
        game.native_build,
        game.interface,
    )
    assert target.addon_version == "0.2.0" and target.scanner_addon_version == "0.3.0"
    assert target.auctioneer == "Auctioneer Jaxon"
    assert len(target.plan_hash) == 64
    assert (
        loaded.window_title == "World of Warcraft"
        and loaded.window_class == game.window_class
        and loaded.display == ":crowley-test"
    )
    assert loaded.canvas_crop == (80, 48, 256, 256)
    assert loaded.automation_canvas_crop == (352, 48, 256, 256)
    assert loaded.matcher.comm == game.executable[:15] and loaded.matcher.argv0 == game.windows_argv0
    assert loaded.matcher.wine_prefix == str(env / "wine")
    assert loaded.saved_variables.name == "Crowley.lua" and loaded.data_lua.name == "Data.lua"
    assert loaded.contract.plan_count > 0


def test_addon_version_requires_a_version_line(tmp_path: Path) -> None:
    toc = tmp_path / "Crowley.toc"
    toc.write_text("## Title: Crowley\n", encoding="utf-8")
    with pytest.raises(config.ScanConfigError, match="Version"):
        config.addon_version(toc)
    with pytest.raises(config.ScanConfigError, match="unreadable"):
        config.addon_version(tmp_path / "missing.toc")


def test_crowley_toc_version_equals_the_addon_ns_version() -> None:
    root = (
        Path(__file__).parents[1] / "packages" / "scanner" / "src" / "crowley_scanner" / "addon" / "Crowley"
    )
    toc = config.addon_version(root / "Crowley.toc")
    match = re.search(r'^ns\.VERSION = "([^"]+)"', (root / "Core.lua").read_text(encoding="utf-8"), re.M)
    assert match is not None and match.group(1) == toc
    automation = (
        Path(__file__).parents[1]
        / "packages"
        / "automation"
        / "src"
        / "crowley_automation"
        / "addon"
        / "Crowley_Automation"
        / "Crowley_Automation.toc"
    )
    automation_version = re.search(
        r'^local VERSION = "([^"]+)"',
        (automation.parent / "Automation.lua").read_text(encoding="utf-8"),
        re.M,
    )
    assert automation_version is not None
    assert config.addon_version(automation) == automation_version.group(1)


def test_help_is_quiet() -> None:
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        import contextlib

        with contextlib.redirect_stdout(out):
            cli.main(["--help"])
    assert exc.value.code == 0 and "run" in out.getvalue() and "status" in out.getvalue()
