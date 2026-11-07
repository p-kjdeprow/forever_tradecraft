from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.utils import parse_wheel_filename

from crowley.ops import install


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    game_root = tmp_path / "game"
    (game_root / "_classic_beta_" / "Interface" / "AddOns").mkdir(parents=True)
    environment = dict(
        os.environ,
        HOME=str(tmp_path / "home"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_RUNTIME_DIR=str(runtime),
    )
    return environment, game_root, runtime


def _no_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.subprocess, "run", lambda *_args, **_kwargs: None)


def test_rendered_units_parse_and_have_no_checkout_paths() -> None:
    units = dict(install.render_units())
    assert set(units) == {
        "crowley-companion.service",
        "crowley-health.service",
        "crowley-health.timer",
        "crowley-market-backup.service",
        "crowley-market-backup.timer",
        "crowley-paper-trading.service",
        "crowley-paper-trading.timer",
        "crowley-session.service",
    }
    for name, raw in units.items():
        text = raw.decode("utf-8")
        parser = configparser.RawConfigParser(strict=False)
        parser.read_string(text)
        assert "/home/" not in text
        assert "/run/user/" not in text
        assert "repos/" not in text
        assert "WorkingDirectory=" not in text
        if name.endswith(".service"):
            assert "EnvironmentFile=%E/crowley/secrets.env" in text


def test_dry_run_writes_nothing_and_does_not_reload_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    called = False

    def fail(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(install.subprocess, "run", fail)
    result = install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)
    assert result.dry_run
    assert result.changed
    assert not called
    assert not (tmp_path / "config").exists()
    assert not (game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley").exists()


def test_refuses_symlinked_game_root(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    linked_root = tmp_path / "linked-game"
    linked_root.symlink_to(game_root, target_is_directory=True)
    with pytest.raises(install.InstallError, match="symlinked path component"):
        install.install(install.InstallRequest(game_root=linked_root, dry_run=True), environment)


def test_refuses_symlinked_xdg_config_root(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    real_config = tmp_path / "real-config"
    real_config.mkdir()
    linked_config = tmp_path / "config"
    linked_config.symlink_to(real_config, target_is_directory=True)
    environment["XDG_CONFIG_HOME"] = str(linked_config)
    with pytest.raises(install.InstallError, match="symlinked path component"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)


def test_hostile_paths_are_escaped_in_launcher_and_toml(tmp_path: Path) -> None:
    environment, _game_root, _runtime = _environment(tmp_path)
    marker = tmp_path / "executed"
    hostile_root = tmp_path / ("game;$(touch " + str(marker) + ')"\\é')
    (hostile_root / "_classic_beta_" / "Interface" / "AddOns").mkdir(parents=True)
    plan = install.build_plan(install.InstallRequest(game_root=hostile_root), environment)
    configs = {
        item.relative_name: item.content
        for item in plan.files
        if item.relative_name in {"config/companion.toml", "config/session.toml"}
    }
    assert all(content is not None for content in configs.values())
    companion = tomllib.loads(configs["config/companion.toml"].decode("utf-8"))
    session = tomllib.loads(configs["config/session.toml"].decode("utf-8"))
    assert companion["wow_dir"] == str(hostile_root / "_classic_beta_")
    assert session["profile"]["wine_prefix"]
    assert session["session"]["saved_variables"] == str(
        hostile_root / "_classic_beta_" / "WTF" / "Account" / "850981908#1" / "SavedVariables" / "Crowley.lua"
    )
    assert session["session"]["data_lua"] == str(
        hostile_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    )
    launcher = next(
        item.content for item in plan.files if item.relative_name == "launcher/crowley-world-of-warcraft"
    )
    assert launcher is not None
    assert "$(touch" in launcher.decode("utf-8")
    assert not marker.exists()


def test_install_copies_addon_units_and_manifest_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    request = install.InstallRequest(game_root=game_root)
    first = install.install(request, environment)
    second = install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)
    assert first.changed
    assert not second.changed
    toc = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley" / "Crowley.toc"
    assert "## Interface: 16001" in toc.read_text(encoding="utf-8")
    manifest_path = tmp_path / "state/crowley/install/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "crowley-install-v2"
    assert manifest["addon_mode"] == "packaged"
    assert len(manifest["files"]) == len(install.build_plan(request, environment).files)
    assert manifest["roots"]["game_addons"] == str(
        (game_root / "_classic_beta_" / "Interface" / "AddOns").resolve()
    )
    assert "sha256" not in manifest["files"]["addon/Crowley_Data/Data.lua"]
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "config/crowley/systemd/user/crowley-health.timer").is_symlink()


def test_install_refuses_a_foreign_file(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    foreign = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley"
    foreign.mkdir()
    (foreign / "Crowley.toc").write_text("foreign", encoding="utf-8")
    with pytest.raises(install.InstallError, match="foreign file"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)


def test_development_addon_is_a_symlink_only_with_explicit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    checkout = tmp_path / "checkout"
    (checkout / "addon" / "Crowley").mkdir(parents=True)
    (checkout / "addon" / "Crowley" / "Crowley.toc").write_text("## Interface: 16001\n", encoding="utf-8")
    _no_systemd(monkeypatch)
    install.install(
        install.InstallRequest(game_root=game_root, dev_addon_from=checkout),
        environment,
    )
    target = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley"
    assert target.is_symlink()
    assert target.resolve() == (checkout / "addon" / "Crowley").resolve()
    assert (target.parent / "Crowley_Data" / "Data.lua").is_file()


def test_packaged_and_development_addon_transitions_replace_the_owned_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    checkout = tmp_path / "checkout"
    (checkout / "addon" / "Crowley").mkdir(parents=True)
    (checkout / "addon" / "Crowley" / "Crowley.toc").write_text("## Interface: 16001\n", encoding="utf-8")
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    target = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley"
    assert target.is_dir() and not target.is_symlink()
    install.install(install.InstallRequest(game_root=game_root, dev_addon_from=checkout), environment)
    assert target.is_symlink()
    install.install(install.InstallRequest(game_root=game_root), environment)
    assert target.is_dir() and not target.is_symlink()
    assert "## Interface: 16001" in (target / "Crowley.toc").read_text(encoding="utf-8")


def test_legacy_addon_symlink_is_replaced_without_writing_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    legacy = tmp_path / "legacy-addon"
    (legacy / "Crowley").mkdir(parents=True)
    (legacy / "Crowley" / "Crowley.toc").write_text("legacy", encoding="utf-8")
    target = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley"
    target.symlink_to(legacy / "Crowley", target_is_directory=True)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    assert target.is_dir() and not target.is_symlink()
    assert (legacy / "Crowley" / "Crowley.toc").read_text(encoding="utf-8") == "legacy"


def test_legacy_data_symlink_is_replaced_without_writing_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    (legacy / "Crowley_Data.toc").write_text("legacy", encoding="utf-8")
    (legacy / "Data.lua").write_text('CROWLEY_DATA = { ["generatedAt"] = 99 }\n', encoding="utf-8")
    (legacy / "Queue.lua").write_text('CROWLEY_QUEUE = { ["scan"] = true }\n', encoding="utf-8")
    addons = game_root / "_classic_beta_" / "Interface" / "AddOns"
    (addons / "Crowley_Data").symlink_to(legacy, target_is_directory=True)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    target = addons / "Crowley_Data"
    assert target.is_dir() and not target.is_symlink()
    assert (legacy / "Crowley_Data.toc").read_text(encoding="utf-8") == "legacy"
    assert (target / "Data.lua").read_text(encoding="utf-8") == ('CROWLEY_DATA = { ["generatedAt"] = 99 }\n')
    assert (target / "Queue.lua").read_text(encoding="utf-8") == ('CROWLEY_QUEUE = { ["scan"] = true }\n')


def test_runtime_owned_lua_is_preserved_and_not_digest_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    data = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    queue = data.with_name("Queue.lua")
    data.write_text('CROWLEY_DATA = { ["generatedAt"] = 42 }\n', encoding="utf-8")
    queue.write_text("CROWLEY_QUEUE = false\n", encoding="utf-8")
    install.install(install.InstallRequest(game_root=game_root), environment)
    assert 'generatedAt"] = 42' in data.read_text(encoding="utf-8")
    manifest = json.loads((tmp_path / "state/crowley/install/manifest.json").read_text(encoding="utf-8"))
    assert "sha256" not in manifest["files"]["addon/Crowley_Data/Data.lua"]
    assert "sha256" not in manifest["files"]["addon/Crowley_Data/Queue.lua"]


def test_runtime_owned_lua_directory_is_foreign_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    data = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    data.unlink()
    data.mkdir()
    with pytest.raises(install.InstallError, match="foreign file"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)


def test_daemon_reload_failure_restores_the_previous_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    toc = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley" / "Crowley.toc"
    before = toc.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "systemctl")

    monkeypatch.setattr(install.subprocess, "run", fail)
    with pytest.raises(install.InstallError, match="rolled back"):
        install.install(install.InstallRequest(game_root=game_root), environment)
    assert toc.read_bytes() == before
    assert (
        json.loads((tmp_path / "state/crowley/install/manifest.json").read_text(encoding="utf-8"))["schema"]
        == "crowley-install-v2"
    )


def test_commit_failure_restores_all_destination_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    data_dir = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data"
    before = (data_dir / "Crowley_Data.toc").read_bytes()
    original_replace = install.os.replace
    failed = False

    def fail_once(source: object, destination: object) -> None:
        nonlocal failed
        if not failed and Path(destination) == data_dir:
            failed = True
            raise OSError("injected commit failure")
        original_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", fail_once)
    with pytest.raises(install.InstallError, match="publish install destination"):
        install.install(install.InstallRequest(game_root=game_root), environment)
    assert (data_dir / "Crowley_Data.toc").read_bytes() == before
    assert (tmp_path / "state/crowley/install/manifest.json").is_file()


def test_manifest_publication_failure_restores_previous_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    manifest = tmp_path / "state/crowley/install/manifest.json"
    before = manifest.read_bytes()
    original_replace = install.os.replace
    failed = False

    def fail_manifest_once(source: object, destination: object) -> None:
        nonlocal failed
        if not failed and Path(destination) == manifest:
            failed = True
            raise OSError("injected manifest publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", fail_manifest_once)
    with pytest.raises(install.InstallError, match="publish install destination"):
        install.install(install.InstallRequest(game_root=game_root), environment)
    assert manifest.read_bytes() == before


def test_failed_rollback_preserves_rollback_material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    data_dir = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data"
    original_replace = install.os.replace
    state = {"commit": False, "restore": False}

    def fail_commit_and_restore(source: object, destination: object) -> None:
        if Path(destination) == data_dir and not state["commit"]:
            state["commit"] = True
            raise OSError("injected commit failure")
        if (
            Path(destination) == data_dir
            and state["commit"]
            and Path(source).parent.name.startswith(".crowley-rollback-")
        ):
            state["restore"] = True
            raise OSError("injected rollback failure")
        original_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", fail_commit_and_restore)
    with pytest.raises(install.InstallError, match="rollback material retained"):
        install.install(install.InstallRequest(game_root=game_root), environment)
    assert state == {"commit": True, "restore": True}
    assert tuple(data_dir.parent.glob(".crowley-rollback-*"))


def test_clean_wheel_contains_package_resources_without_checkout_trees(tmp_path: Path) -> None:
    output = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert output.returncode == 0, output.stderr
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        assert "crowley/ops/package_data/addon/Crowley/Crowley.toc" in names
        assert "crowley/ops/package_data/docker/Dockerfile" in names
        assert "crowley/ops/package_data/systemd/crowley-health.service.in" in names
        assert "crowley/ops/resources/docker-compose.yml" in names
        assert not any(name.startswith("addon/") or name.startswith("deploy/") for name in names)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_compose_config_accepts_dummy_required_environment(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    secrets_dir = config_home / "crowley"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.env").write_text("", encoding="utf-8")
    env_file = tmp_path / "compose.env"
    env_file.write_text("", encoding="utf-8")
    environment = dict(
        os.environ,
        CROWLEY_DB_PASSWORD="dummy",
        CROWLEY_DATABASE_URL="postgresql://crowley:host-only@" + "127.0." + "0.1" + ":5432/crowley",
        CROWLEY_API_TOKEN="dummy",
        CROWLEY_BIND="127.0." + "0.1",
        CROWLEY_IMAGE_VERSION="0.1.0",
        XDG_CONFIG_HOME=str(config_home),
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "src/crowley/ops/resources/docker-compose.yml",
            "config",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert "build:" not in completed.stdout
    assert "image: crowley-server:0.1.0" in completed.stdout
    assert "postgresql://crowley:dummy@db:5432/crowley" in completed.stdout
    host_endpoint = "127.0." + "0.1" + ":5432"
    assert host_endpoint not in completed.stdout


def test_image_build_from_wheel_stages_only_packaged_inputs(tmp_path: Path) -> None:
    wheel = tmp_path / "crowley-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(command: object, cwd: Path) -> None:
        assert isinstance(command, tuple)
        typed_command = tuple(str(value) for value in command)
        calls.append((typed_command, cwd))
        if typed_command[0] == "docker":
            context = Path(typed_command[-1])
            staged_wheels = tuple(path for path in context.iterdir() if path.suffix == ".whl")
            assert {path.name for path in context.iterdir()} == {"Dockerfile", wheel.name}
            assert len(staged_wheels) == 1
            assert parse_wheel_filename(staged_wheels[0].name)[0] == "crowley"
            assert parse_wheel_filename(staged_wheels[0].name)[1].public == "0.1.0"
            assert staged_wheels[0].read_bytes() == b"wheel bytes"
            dockerfile = (context / "Dockerfile").read_bytes()
            assert b"COPY *.whl /build/" in dockerfile
            assert b"/build/*.whl" in dockerfile

    tag = install.build_image(wheel=wheel, runner=runner)
    assert tag == "crowley-server:0.1.0"
    assert len(calls) == 1
    assert calls[0][0][:4] == ("docker", "build", "--tag", "crowley-server:0.1.0")


def test_image_build_from_source_stages_uv_built_wheel(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(command: object, cwd: Path) -> None:
        assert isinstance(command, tuple)
        typed_command = tuple(str(value) for value in command)
        calls.append((typed_command, cwd))
        if typed_command[0] == "uv":
            output_dir = Path(typed_command[typed_command.index("--out-dir") + 1])
            (output_dir / "crowley-0.1.0-py3-none-any.whl").write_bytes(b"built wheel")
        else:
            context = Path(typed_command[-1])
            staged_wheels = tuple(path for path in context.iterdir() if path.suffix == ".whl")
            assert {path.name for path in context.iterdir()} == {
                "Dockerfile",
                "crowley-0.1.0-py3-none-any.whl",
            }
            assert len(staged_wheels) == 1
            assert parse_wheel_filename(staged_wheels[0].name)[0] == "crowley"
            assert (staged_wheels[0]).read_bytes() == b"built wheel"

    tag = install.build_image(source=source, runner=runner)
    assert tag == "crowley-server:0.1.0"
    assert [call[0][0] for call in calls] == ["uv", "docker"]
    assert calls[0][1] == source
