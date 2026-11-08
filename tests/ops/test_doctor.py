from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from crowley.config import PinnedBinaries, PinnedObserver
from crowley.core.game_target import load_game_target
from crowley.core.luawriter import render_assignment
from crowley.core.queue import render_queue_lua
from crowley.native import pins
from crowley.ops import doctor, install
from crowley.server.db import MigrationStatus

TARGET_FIXTURE = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"
REVIEWED_CODEX_DIGEST = "628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1"


def test_production_codex_pin_matches_reviewed_install_receipt() -> None:
    assert pins.CODEX_ARTIFACT_SHA256 == REVIEWED_CODEX_DIGEST


def test_pinned_binary_digest_accepts_a_large_sparse_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large-codex"
    path.write_bytes(b"fixture")
    path.chmod(0o700)
    with path.open("r+b") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="codex-cli test\n", stderr=""
        ),
    )
    problems: list[str] = []
    doctor._check_binary(
        path,
        label="large pinned Codex executable",
        expected_digest=digest,
        expected_version="codex-cli test",
        problems=problems,
    )
    assert problems == []


def test_real_reviewed_codex_binary_hashes_when_installed() -> None:
    receipt_path = Path.home() / ".cache" / "crowley" / "codex" / "0.162.1" / "INSTALL-RECEIPT.json"
    if not receipt_path.is_file():
        pytest.skip(f"reviewed Codex install receipt is absent: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    native_binary = receipt["native_binary"]
    path = Path(native_binary["path"])
    if not path.is_file():
        pytest.skip(f"reviewed Codex executable is absent: {path}")
    problems: list[str] = []
    doctor._check_binary(
        path,
        label="reviewed pinned Codex executable",
        expected_digest=native_binary["sha256"],
        expected_version=receipt["verification"]["cli_version_output"],
        problems=problems,
    )
    assert problems == []


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
    codex_digest = hashlib.sha256(codex.read_bytes()).hexdigest()
    environment["CROWLEY_CODEX_PATH"] = str(codex)
    observer = tmp_path / "observer"
    observer.write_bytes(b"reviewed observer fixture")
    observer.chmod(0o700)
    observer_digest = hashlib.sha256(observer.read_bytes()).hexdigest()
    observer_manifest = observer.with_suffix(".manifest.json")
    observer_manifest.write_text(
        json.dumps(
            {
                "schema": pins.OBSERVER_MANIFEST_SCHEMA,
                "target": pins.TARGET_TRIPLE,
                "binary": pins.OBSERVER_ARTIFACT_NAME,
                "binary_sha256": observer_digest,
                "source_sha256": pins.OBSERVER_SOURCE_HASHES,
                "workspace_sha256": pins.OBSERVER_WORKSPACE_SHA256,
                "static_library_resolution": {
                    "mechanism": "pkg-config",
                    "package": "x11",
                    "mode": "static",
                    "libraries": [f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES],
                },
                "static_inputs": {f"lib{name}.a": "a" * 64 for name in pins.OBSERVER_STATIC_LIBRARY_NAMES},
                "toolchain": {
                    "rustc": pins.EXPECTED_RUSTC,
                    "cargo": pins.EXPECTED_CARGO,
                    "rustfmt": pins.EXPECTED_RUSTFMT,
                },
                "linker": {
                    "driver": {"name": "cc", "sha256": "b" * 64, "version": "cc test"},
                    "ld": {"name": "ld", "sha256": "c" * 64, "version": "ld test"},
                },
            }
        ),
        encoding="utf-8",
    )
    observer_manifest.chmod(0o600)
    monkeypatch.setattr(
        doctor,
        "resolve_pinned_observer",
        lambda _xdg: PinnedObserver(
            observer,
            observer_digest,
            str(pins.OBSERVER_PROTOCOL_VERSION),
            pins.OBSERVER_PROVENANCE,
            observer_manifest,
        ),
    )
    monkeypatch.setattr(
        doctor,
        "load_pinned_binaries",
        lambda _environment: PinnedBinaries(
            codex,
            "gpt-6-luna",
            "codex-cli 0.162.1",
            codex_digest,
        ),
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


def test_doctor_rejects_a_codex_digest_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    codex = Path(environment["CROWLEY_CODEX_PATH"])
    codex.write_text(codex.read_text(encoding="ascii") + "# changed\n", encoding="ascii")
    report = doctor.doctor(environment, database_status=_current)
    assert "pinned Codex executable digest mismatch" in "\n".join(report.problems)


@pytest.mark.parametrize(
    ("version", "provenance", "message"),
    [
        ("2", "crowley.x11-identity-observer.v1", "version does not match"),
        ("1", "wrong-provenance", "provenance does not match"),
    ],
)
def test_doctor_rejects_observer_version_and_provenance_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    provenance: str,
    message: str,
) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    observer = tmp_path / "observer"
    observer_digest = hashlib.sha256(observer.read_bytes()).hexdigest()
    monkeypatch.setattr(
        doctor,
        "resolve_pinned_observer",
        lambda _xdg: PinnedObserver(
            observer,
            observer_digest,
            version,
            provenance,
            observer.with_suffix(".manifest.json"),
        ),
    )
    report = doctor.doctor(environment, database_status=_current)
    assert message in "\n".join(report.problems)


def test_doctor_rejects_malformed_observer_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _game_root = _setup(tmp_path, monkeypatch)
    manifest = tmp_path / "observer.manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    del value["toolchain"]
    manifest.write_text(json.dumps(value), encoding="utf-8")
    report = doctor.doctor(environment, database_status=_current)
    assert "provenance manifest keys are invalid" in "\n".join(report.problems)


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
