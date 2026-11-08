from __future__ import annotations

import hashlib
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from crowley.config import PinnedObserver
from crowley.core.game_target import load_game_target
from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua
from crowley.ops import doctor, install
from crowley.server.db import MigrationStatus

TARGET_FIXTURE = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    game_root = tmp_path / "game"
    target = load_game_target(TARGET_FIXTURE)
    (tmp_path / "wine-prefix").mkdir()
    monkeypatch.setattr(install, "packaged_game_target", lambda: target)
    monkeypatch.setattr(doctor, "packaged_game_target", lambda: target)
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
    session_template = Path("deploy/config/session.toml").read_bytes()
    session_values = tomllib.loads(session_template.decode("utf-8"))
    assert set(session_values) == {"session"}
    (config / "session.toml").write_bytes(session_template)
    secrets = config / "secrets.env"
    secrets.write_text(
        "CROWLEY_DATABASE_URL=postgresql://test\nCROWLEY_API_TOKEN=test\n",
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
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.162.1\\n'\n", encoding="ascii")
    codex.chmod(0o700)
    environment["CROWLEY_CODEX_PATH"] = str(codex)
    observer = tmp_path / "observer"
    observer.write_bytes(b"reviewed observer fixture")
    observer.chmod(0o700)
    observer_digest = hashlib.sha256(observer.read_bytes()).hexdigest()
    monkeypatch.setattr(
        doctor,
        "resolve_pinned_observer",
        lambda _xdg: PinnedObserver(observer, observer_digest, "1"),
    )
    entrypoints = tmp_path / "home" / ".local" / "bin"
    entrypoints.mkdir(parents=True)
    for name in ("crowley-companion", "crowley-ops", "crowley-paper-step", "crowley-session"):
        path = entrypoints / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="codex-cli 0.162.1\n", stderr=""
        ),
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


def test_doctor_reuses_validated_host_for_install_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)

    def fail_duplicate_load(_path: Path) -> object:
        raise AssertionError("doctor asked the installer to reload host.toml")

    monkeypatch.setattr(install, "load_host_config", fail_duplicate_load)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok, report.problems


def test_doctor_aggregates_unit_addon_secret_database_and_account_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root = _setup(tmp_path, monkeypatch)
    unit = tmp_path / "config/systemd/user/crowley-session.service"
    unit.write_text(
        unit.read_text(encoding="utf-8").replace("%h/.local/bin/crowley-session", "/checkout/session")
    )
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
