from __future__ import annotations

import io
import json
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from crowley.core.game_target import load_game_target
from crowley.gui.frames import PLAN_COUNT
from crowley.scan import cli, config, loop, observe, presence
from tests.ops.test_install import _write_host

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


def test_presence_ok_exit_code_follows_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "presence_ok", lambda: 0)
    assert cli.main(["presence-ok"]) == 0
    monkeypatch.setattr(cli, "presence_ok", lambda: 1)
    assert cli.main(["presence-ok"]) == 1
    runner = lambda argv, timeout: "(uint64 999999,)"  # noqa: E731
    assert presence.presence_ok(presence=presence.Presence(runner=runner)) == 0


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


def loaded_config(root: Path) -> config.ScanConfig:
    from crowley.gui.frames import ProtocolContract
    from crowley.gui.process import OwnedGameMatcher
    from tests.test_scan_observe import TARGET

    return config.ScanConfig(
        target=TARGET,
        matcher=OwnedGameMatcher("Wow.exe", "C:\\wow.exe", str(root / "wine"), 1),
        window_class="wow.exe",
        window_title="World of Warcraft",
        display=":0",
        canvas_crop=(80, 48, 256, 256),
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
    toc.write_text("## Interface: 16001\n## Version: 0.2.0\n", encoding="utf-8")
    _write_host(env / "config", game_root, env / "wine")
    loaded = config.load_scan_config()
    target = loaded.target
    assert (target.character, target.realm, target.faction) == ("Arthur", game.realm, game.faction)
    assert (target.house_key, target.build, target.interface) == (
        game.house,
        game.native_build,
        game.interface,
    )
    assert target.addon_version == "0.2.0" and target.auctioneer == "Auctioneer Jaxon"
    assert len(target.plan_hash) == 64
    assert (
        loaded.window_title == "World of Warcraft"
        and loaded.window_class == game.window_class
        and loaded.display == ":crowley-test"
    )
    assert loaded.canvas_crop == (80, 48, 256, 256)
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


def test_probe_frame_exit_codes(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_scan_config", lambda: loaded_config(env))
    for result, code in ((True, 0), (False, 1)):
        monkeypatch.setattr(cli.Observer, "probe_frame", lambda self, r=result: r)
        assert cli.main(["probe-frame"]) == code

    def broken(self: Any) -> bool:
        raise observe.ObserveError("xdotool exploded")

    monkeypatch.setattr(cli.Observer, "probe_frame", broken)
    assert cli.main(["probe-frame"]) == 3
    assert "xdotool exploded" in capsys.readouterr().err
    monkeypatch.setattr(cli.Observer, "probe_frame", lambda self: True)

    def bad_config() -> config.ScanConfig:
        raise config.ScanConfigError("no host.toml")

    monkeypatch.setattr(cli, "load_scan_config", bad_config)
    assert cli.main(["probe-frame"]) == 3  # config failures are errors, never "no frame"
    with pytest.raises(SystemExit) as exc:
        cli.main(["probe-frame", "--bogus"])
    assert exc.value.code == 2


def test_crowley_toc_version_equals_the_addon_ns_version() -> None:
    root = Path(__file__).parents[1] / "addon" / "Crowley"
    toc = config.addon_version(root / "Crowley.toc")
    match = re.search(r'^ns\.VERSION = "([^"]+)"', (root / "Core.lua").read_text(encoding="utf-8"), re.M)
    assert match is not None and match.group(1) == toc


def test_help_is_quiet() -> None:
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        import contextlib

        with contextlib.redirect_stdout(out):
            cli.main(["--help"])
    assert exc.value.code == 0 and "presence-ok" in out.getvalue()
