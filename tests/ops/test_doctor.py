from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from crowley.core.game_target import load_game_target
from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua
from crowley.ops import doctor, install
from crowley.server.db import MigrationStatus

TARGET_FIXTURE = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"
REVIEWED_CODEX_DIGEST = "628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    game_root = tmp_path / "game"
    target = load_game_target(TARGET_FIXTURE)
    (tmp_path / "wine-prefix").mkdir()
    monkeypatch.setattr(install, "packaged_game_target", lambda: target)
    monkeypatch.setattr(doctor, "packaged_game_target", lambda: target)
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
                "client_x = 0",
                "client_y = 0",
                "client_width = 1920",
                "client_height = 1080",
                "outer_width = 1928",
                "outer_height = 1092",
                "window_client_offset = [0, 0]",
                "canvas_origin = [80, 48]",
                "canvas_crop = [80, 48, 256, 256]",
                "model_crop = [80, 48, 1760, 984]",
                "",
            )
        ),
        encoding="utf-8",
    )
    (config / "companion.toml").write_text(
        "[companion]\npoll_seconds = 3.0\nrefresh_minutes = 5.0\n",
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
    for name in ("crowley-companion", "crowley-ops", "crowley-paper-step", "crowley-scan"):
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
    monkeypatch.setattr(install, "packaged_game_target", fail_duplicate_target)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_aggregates_unit_addon_secret_database_and_account_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    unit = tmp_path / "config/systemd/user/crowley-scan.service"
    unit.write_text(unit.read_text(encoding="utf-8").replace("%h/.local/bin/crowley-scan", "/checkout/scan"))
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
    assert "model_crop" in joined


def test_doctor_aggregates_invalid_host_and_companion_tunables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    (tmp_path / "config/crowley/host.toml").write_text("unknown = true\n", encoding="utf-8")
    (tmp_path / "config/crowley/companion.toml").write_text(
        "[companion]\npoll_seconds = nan\nrefresh_minutes = -1.0\n", encoding="utf-8"
    )

    report = doctor.doctor(environment, database_status=_current)

    joined = "\n".join(report.problems)
    assert "unknown" in joined
    assert "companion.toml is invalid" in joined


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


def test_doctor_accepts_companion_rendered_data_and_queue_lua(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    data_lua = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    queue_lua = data_lua.with_name("Queue.lua")
    data_lua.write_text(
        render_assignment(
            "CROWLEY_DATA",
            {"generatedAt": 1, "ackedScans": [], "scanPlan": False, "referenceItemIDs": []},
            header="companion test",
        ),
        encoding="utf-8",
    )
    queue_lua.write_text(render_queue_lua(None), encoding="utf-8")

    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_accepts_uv_tool_symlinked_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    entrypoints = tmp_path / "home" / ".local" / "bin"
    tool_bin = tmp_path / "home" / ".local" / "share" / "uv" / "tools" / "crowley" / "bin"
    tool_bin.mkdir(parents=True)
    for name in ("crowley-companion", "crowley-ops", "crowley-paper-step", "crowley-scan"):
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
    (entrypoints / "crowley-scan").chmod(0o600)
    problems = "\n".join(doctor.doctor(environment, database_status=_current).problems)
    assert f"unit entrypoint is unavailable: {entrypoints / 'crowley-companion'}" in problems
    assert f"unit entrypoint is not executable: {entrypoints / 'crowley-scan'}" in problems


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
