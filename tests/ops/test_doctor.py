from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua
from crowley.ops import doctor, install
from crowley.server.db import MigrationStatus


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    game_root = tmp_path / "game"
    target = replace(
        install.packaged_game_target(),
        game_root=str(game_root),
        wine_prefix=str(tmp_path / "wine-prefix"),
    )
    (tmp_path / "wine-prefix").mkdir()
    monkeypatch.setattr(install, "packaged_game_target", lambda: target)
    monkeypatch.setattr(doctor, "packaged_game_target", lambda: target)
    flavor = game_root / target.product_dir
    (flavor / "Interface" / "AddOns").mkdir(parents=True)
    account = flavor / "WTF" / "Account" / "850981908#1"
    (account / "SavedVariables").mkdir(parents=True)
    (account / "SavedVariables" / "Crowley.lua").write_text("CrowleyDB = {}\n", encoding="utf-8")
    config = tmp_path / "config" / "crowley"
    routes = config / "routes"
    routes.mkdir(parents=True)
    (routes / "stormwind.json").write_text("{}\n", encoding="utf-8")
    (config / "companion.toml").write_text(
        f'wow_dir = "{flavor}"\nserver_url = "http://crowley-server:8080"\nwtf_account = "850981908#1"\n',
        encoding="utf-8",
    )
    pin_dir = tmp_path / "pins"
    pin_dir.mkdir()
    binaries: dict[str, Path] = {}
    for name in ("identity-observer", "telemetry-region", "codex"):
        path = pin_dir / name
        path.write_text(f"#!/bin/sh\n# {name}\n", encoding="utf-8")
        path.chmod(0o700)
        binaries[name] = path
    session = Path("deploy/config/session.toml").read_text(encoding="utf-8")
    template_pins = tomllib.loads(session)["session"]
    session = session.replace(
        'wine_prefix = "/path/to/measured/wine-prefix"',
        f'wine_prefix = "{target.wine_prefix}"',
    )
    session = session.replace("/path/to/World of Warcraft/_classic_beta_", str(flavor))
    session = session.replace(
        "/path/to/pinned/x11-identity-observer",
        str(binaries["identity-observer"]),
    )
    session = session.replace(
        template_pins["identity_observer_executable_sha256"],
        hashlib.sha256(binaries["identity-observer"].read_bytes()).hexdigest(),
    )
    session = session.replace("/path/to/pinned/gnome-screenshot", str(binaries["telemetry-region"]))
    session = session.replace(
        template_pins["telemetry_region_executable_sha256"],
        hashlib.sha256(binaries["telemetry-region"].read_bytes()).hexdigest(),
    )
    session = session.replace(
        'codex_path = "~/.cache/crowley/codex/codex"', f'codex_path = "{binaries["codex"]}"'
    )
    session = session.replace(
        'routes_file = "/path/to/reviewed/routes/stormwind.json"',
        f'routes_file = "{routes / "stormwind.json"}"',
    )
    session = session.replace(
        'companion_state = "~/.local/state/crowley/companion-state.json"',
        f'companion_state = "{tmp_path / "companion-state.json"}"',
    )
    (tmp_path / "companion-state.json").write_text("{}\n", encoding="utf-8")
    (config / "session.toml").write_text(session, encoding="utf-8")
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
    entrypoints = tmp_path / "home" / ".local" / "bin"
    entrypoints.mkdir(parents=True)
    for name in ("crowley-companion", "crowley-ops", "crowley-paper-step", "crowley-session"):
        path = entrypoints / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    monkeypatch.setattr(install.subprocess, "run", lambda *_args, **_kwargs: None)
    install.install(install.InstallRequest(game_root=game_root), environment)
    return environment, game_root


def _current(_url: str) -> MigrationStatus:
    return MigrationStatus((), (), (), (), ())


def test_doctor_passes_after_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    report = doctor.doctor(environment, database_status=_current)
    assert report.ok
    assert report.problems == ()


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


def test_doctor_rejects_missing_pins_and_codex_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    (tmp_path / "config/crowley/session.toml").write_text(
        '[session]\ncli_version = "codex-cli 0.162.1"\n', encoding="utf-8"
    )
    report = doctor.doctor(environment, database_status=_current)
    joined = "\n".join(report.problems)
    assert "identity_observer_executable" in joined
    assert "telemetry_region_executable" in joined
    assert "codex_path is required" in joined


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
