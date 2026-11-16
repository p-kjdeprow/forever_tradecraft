from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from crowley_common.game_target import load_game_target
from crowley_common.luawriter import render_assignment
from crowley_common.watchlist import load_catalog
from crowley_host.config import resolved as host_resolved
from crowley_host.ops import doctor, install
from crowley_market.server.db import MigrationStatus

TARGET_FIXTURE = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"
REVIEWED_CODEX_DIGEST = "628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    game_root = tmp_path / "game"
    target = load_game_target(TARGET_FIXTURE)
    (tmp_path / "wine-prefix").mkdir()
    monkeypatch.setattr(install, "selected_target", lambda: target)
    monkeypatch.setattr(doctor, "selected_target", lambda: target)
    monkeypatch.setattr(doctor, "_check_gnome_animations", lambda _problems: None)
    monkeypatch.setattr(doctor, "_check_gnome_idle_delay", lambda _problems: None)
    flavor = game_root / target.product_dir
    (flavor / "Interface" / "AddOns").mkdir(parents=True)
    account = flavor / "WTF" / "Account" / "850981908#1"
    (account / "SavedVariables").mkdir(parents=True)
    (account / "SavedVariables" / "Crowley.lua").write_text("CrowleyDB = {}\n", encoding="utf-8")
    config = tmp_path / "config" / "crowley"
    config.mkdir(parents=True)
    (config / "host.toml").write_text(
        "\n".join(
            (
                f'wine_prefix = "{tmp_path / "wine-prefix"}"',
                f'game_root = "{game_root}"',
                'wtf_account = "850981908#1"',
                'character = "Arthur"',
                'display = ":0"',
                'server_url = "http://crowley-server:8080"',
                'window_title = "World of Warcraft"',
                "canvas_crop = [80, 48, 256, 256]",
                "automation_canvas_crop = [352, 48, 256, 256]",
                "",
            )
        ),
        encoding="utf-8",
    )
    secrets = config / "secrets.env"
    secrets.write_text(
        "CROWLEY_DATABASE_URL=postgresql://test\nCROWLEY_API_TOKEN=test\nCROWLEY_WINE=/opt/wine/bin/wine\n",
        encoding="utf-8",
    )
    secrets.chmod(0o600)
    environment = dict(
        os.environ,
        HOME=str(tmp_path / "home"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_RUNTIME_DIR=str(runtime),
        CROWLEY_API_TOKEN="test",
    )
    monkeypatch.setattr(doctor.input_tool, "verify_artifact", lambda *_args, **_kwargs: None)
    entrypoints = tmp_path / "home" / ".local" / "bin"
    entrypoints.mkdir(parents=True)
    for name in ("crowley-companion", "crowley-ops", "crowley-host"):
        path = entrypoints / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    install.install(install.InstallRequest(game_root=game_root), environment)
    return environment, game_root


def _current(_url: str) -> MigrationStatus:
    return MigrationStatus((), (), (), (), ())


def test_doctor_passes_after_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok
    assert report.problems == ()


def test_rerun_install_repins_configs_and_preserves_scanner_intervals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, old_game_root = _setup(tmp_path, monkeypatch)
    config_dir = Path(environment["XDG_CONFIG_HOME"]) / "crowley"
    scanner_path = config_dir / "scanner.toml"
    scanner_path.write_text(
        scanner_path.read_text(encoding="utf-8")
        .replace("poll_seconds = 3.0", "poll_seconds = 4.5")
        .replace("refresh_minutes = 5.0", "refresh_minutes = 6.5"),
        encoding="utf-8",
    )

    old_target = doctor.selected_target()
    new_target = replace(old_target, full_build="7.0.0.79999")
    new_catalog = load_catalog(enforce_target=False).model_copy(
        update={"client_build": new_target.full_build}
    )
    monkeypatch.setattr(host_resolved, "load_catalog", lambda **_kwargs: new_catalog)
    monkeypatch.setattr(doctor, "selected_target", lambda: new_target)
    monkeypatch.setattr(install, "selected_target", lambda: new_target)
    new_game_root = tmp_path / "replacement-game"
    flavor = new_game_root / new_target.product_dir
    (flavor / "Interface" / "AddOns").mkdir(parents=True)
    saved = flavor / "WTF" / "Account" / "850981908#1" / "SavedVariables"
    saved.mkdir(parents=True)
    (saved / "Crowley.lua").write_text("CrowleyDB = {}\n", encoding="utf-8")
    new_wine = tmp_path / "replacement-wine"
    new_wine.mkdir()
    host_path = config_dir / "host.toml"
    host_text = host_path.read_text(encoding="utf-8")
    host_text = host_text.replace(str(old_game_root), str(new_game_root))
    host_text = host_text.replace(str(tmp_path / "wine-prefix"), str(new_wine))
    host_text = host_text.replace('character = "Arthur"', 'character = "Merlin"')
    host_text = host_text.replace('display = ":0"', 'display = ":3"')
    host_path.write_text(host_text, encoding="utf-8")

    install.install(install.InstallRequest(game_root=new_game_root), environment)
    scanner = tomllib.loads(scanner_path.read_text(encoding="utf-8"))["scanner"]
    automation = tomllib.loads((config_dir / "automation.toml").read_text(encoding="utf-8"))["automation"]
    assert (
        scanner["game_root"],
        scanner["character"],
        scanner["poll_seconds"],
        scanner["refresh_minutes"],
    ) == (
        str(new_game_root),
        "Merlin",
        4.5,
        6.5,
    )
    assert (
        automation["game_root"],
        automation["wine_prefix"],
        automation["character"],
        automation["display"],
        automation["build"],
    ) == (str(new_game_root), str(new_wine), "Merlin", ":3", new_target.full_build)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_reports_an_unpublished_or_invalid_input_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise doctor.input_tool.InputToolError("private XTEST tool is not published")

    monkeypatch.setattr(doctor.input_tool, "verify_artifact", refuse)
    report = doctor.doctor(environment, database_status=_current)
    assert "pinned input tool is invalid: private XTEST tool is not published" in report.problems


def test_doctor_reuses_validated_host_for_install_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)

    def fail_duplicate_load(_path: Path) -> object:
        raise AssertionError("doctor asked the installer to reload host.toml")

    def fail_duplicate_target() -> object:
        raise AssertionError("doctor asked the installer to reload target.json")

    monkeypatch.setattr(install, "load_host_config", fail_duplicate_load)
    monkeypatch.setattr(install, "selected_target", fail_duplicate_target)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_aggregates_unit_addon_secret_database_and_account_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    unit = tmp_path / "config/systemd/user/crowley-host.service"
    unit.write_text(unit.read_text(encoding="utf-8").replace("%h/.local/bin/crowley-host", "/checkout/host"))
    addon = game_root / "_classic_beta_/Interface/AddOns/Crowley/Crowley.toc"
    addon.write_text("## Interface: 99999\n", encoding="utf-8")
    secrets = tmp_path / "config/crowley/secrets.env"
    secrets.chmod(0o644)
    (game_root / "_classic_beta_/WTF/Account/account-two").mkdir()
    report = doctor.doctor(
        environment,
        database_status=lambda _url: MigrationStatus(("001_init.sql",), ("002_market.sql",), (), (), ()),
    )
    joined = "\n".join(report.problems)
    assert not report.ok
    assert "checkout path" in joined
    assert "digest mismatch" in joined
    assert "0600" in joined
    assert "migrations are not current" in joined


def test_doctor_accepts_a_selected_account_with_unrelated_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    (game_root / "_classic_beta_" / "WTF" / "Account" / "account-two").mkdir()
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_reports_all_missing_host_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    (tmp_path / "config/crowley/host.toml").write_text("unknown = true\n", encoding="utf-8")
    report = doctor.doctor(environment, database_status=_current)
    joined = "\n".join(report.problems)
    assert "unknown" in joined
    assert "wine_prefix" in joined
    assert "game_root" in joined
    assert "automation_canvas_crop" in joined


def test_doctor_aggregates_invalid_host_and_scanner_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    (tmp_path / "config/crowley/host.toml").write_text("unknown = true\n", encoding="utf-8")
    (tmp_path / "config/crowley/scanner.toml").write_text(
        "[scanner]\npoll_seconds = nan\nrefresh_minutes = -1.0\n", encoding="utf-8"
    )

    report = doctor.doctor(environment, database_status=_current)

    joined = "\n".join(report.problems)
    assert "unknown" in joined
    assert "scanner.toml is invalid" in joined


def test_doctor_requires_the_selected_saved_variables_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    saved_variables = (
        game_root / "_classic_beta_" / "WTF" / "Account" / "850981908#1" / "SavedVariables" / "Crowley.lua"
    )
    saved_variables.unlink()
    report = doctor.doctor(environment, database_status=_current)
    assert any("SavedVariables/Crowley.lua" in problem for problem in report.problems)


def test_doctor_accepts_companion_rendered_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    data_lua = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    data_lua.write_text(
        render_assignment(
            "CROWLEY_DATA",
            {"generatedAt": 1, "ackedScans": [], "scanPlan": False},
            header="companion test",
        ),
        encoding="utf-8",
    )

    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_accepts_uv_tool_symlinked_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    entrypoints = tmp_path / "home" / ".local" / "bin"
    tool_bin = tmp_path / "home" / ".local" / "share" / "uv" / "tools" / "crowley" / "bin"
    tool_bin.mkdir(parents=True)
    for name in ("crowley-companion", "crowley-ops", "crowley-host"):
        target = tool_bin / name
        (entrypoints / name).rename(target)
        (entrypoints / name).symlink_to(target)
    report = doctor.doctor(environment, database_status=_current)
    assert report.problems == ()


def test_doctor_rejects_dangling_or_non_executable_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    entrypoints = tmp_path / "home" / ".local" / "bin"
    (entrypoints / "crowley-companion").unlink()
    (entrypoints / "crowley-companion").symlink_to(tmp_path / "missing")
    (entrypoints / "crowley-host").chmod(0o600)
    problems = "\n".join(doctor.doctor(environment, database_status=_current).problems)
    assert f"unit entrypoint is unavailable: {entrypoints / 'crowley-companion'}" in problems
    assert f"unit entrypoint is not executable: {entrypoints / 'crowley-host'}" in problems


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    (
        ("false\n", 0, None),
        ("true\n", 0, "GNOME animations must be disabled"),
        ("", 1, "gsettings failed"),
    ),
)
def test_doctor_checks_gnome_animations(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )
    problems: list[str] = []
    doctor._check_gnome_animations(problems)
    if expected is None:
        assert problems == []
    else:
        assert expected in "\n".join(problems)


def test_doctor_reports_unavailable_gsettings(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gsettings")

    monkeypatch.setattr(doctor.subprocess, "run", unavailable)
    problems: list[str] = []
    doctor._check_gnome_animations(problems)
    assert "gsettings is unavailable" in "\n".join(problems)


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    (
        ("uint32 0\n", 0, None),
        ("uint32 300\n", 0, "idle-delay must be 0"),
        ("", 1, "gsettings failed"),
    ),
)
def test_doctor_checks_gnome_idle_delay(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: str | None
) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    problems: list[str] = []
    doctor._check_gnome_idle_delay(problems)
    assert seen == [["gsettings", "get", "org.gnome.desktop.session", "idle-delay"]]
    if expected is None:
        assert problems == []
    else:
        assert expected in "\n".join(problems)


def test_doctor_reports_unavailable_gsettings_for_idle_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gsettings")

    monkeypatch.setattr(doctor.subprocess, "run", unavailable)
    problems: list[str] = []
    doctor._check_gnome_idle_delay(problems)
    assert "gsettings is unavailable" in "\n".join(problems)
