from __future__ import annotations

import configparser
import json
import os
import pwd
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from crowley_common.game_target import load_game_target
from crowley_host.ops import install
from packaging.utils import parse_wheel_filename

TARGET_FIXTURE = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"


@pytest.fixture(autouse=True)
def _fixture_target(monkeypatch: pytest.MonkeyPatch) -> None:
    target = load_game_target(TARGET_FIXTURE)
    monkeypatch.setattr(install, "selected_target", lambda: target)


def _write_host(config_home: Path, game_root: Path, wine_prefix: Path) -> None:
    host = Path(config_home) / "crowley" / "host.toml"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(
        "\n".join(
            (
                f"wine_prefix = {json.dumps(str(wine_prefix))}",
                f"game_root = {json.dumps(str(game_root))}",
                'wtf_account = "850981908#1"',
                'character = "Arthur"',
                'display = ":crowley-test"',
                'server_url = "http://server.invalid:8080"',
                'window_title = "World of Warcraft"',
                "canvas_crop = [80, 48, 256, 256]",
                "automation_canvas_crop = [352, 48, 256, 256]",
                "",
            )
        ),
        encoding="utf-8",
    )


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
    _write_host(environment["XDG_CONFIG_HOME"], game_root, tmp_path / "wine")
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
        "crowley-host.service",
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
        if name == "crowley-host.service":
            # The runtime directory is tmpfs; the unit recreates it after a reboot.
            service = parser["Service"]
            assert service["RuntimeDirectory"] == "crowley"
            assert service["RuntimeDirectoryMode"] == "0700"
            assert service["RuntimeDirectoryPreserve"] == "yes"


def test_host_unit_is_rendered_and_not_enabled() -> None:
    units = {name: raw.decode("utf-8") for name, raw in install.render_units()}
    host = units["crowley-host.service"]
    assert "ExecStart=%h/.local/bin/crowley-host run" in host
    assert "Conflicts=" not in host
    assert not any("session" in name for name in units)
    for line in (
        "EnvironmentFile=%E/crowley/secrets.env",
        "Environment=DISPLAY=:0",
        "Environment=WAYLAND_DISPLAY=wayland-0",
        "Environment=XDG_RUNTIME_DIR=%t",
        "Environment=XDG_STATE_HOME=%S",
        "PassEnvironment=XAUTHORITY",
        "KillMode=process",
        "Restart=always",
        "RestartSec=5s",
        "StartLimitIntervalSec=0",
    ):
        assert line in host
    source = Path(install.__file__).read_text(encoding="utf-8")
    assert "enable" not in source  # the installer never enables or starts a unit


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
    assert (tmp_path / "config/crowley/host.toml").is_file()
    assert not (game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley").exists()


def test_install_requires_host_contract_and_prints_example_path(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    (tmp_path / "config/crowley/host.toml").unlink()
    with pytest.raises(install.InstallError, match="host.example.toml"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)


def test_refuses_symlinked_game_root(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    linked_root = tmp_path / "linked-game"
    linked_root.symlink_to(game_root, target_is_directory=True)
    _write_host(environment["XDG_CONFIG_HOME"], linked_root, tmp_path / "wine")
    with pytest.raises(install.InstallError, match="symlinked path component"):
        install.install(install.InstallRequest(game_root=linked_root, dry_run=True), environment)


def test_refuses_symlinked_xdg_config_root(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    real_config = tmp_path / "real-config"
    linked_config = tmp_path / "config"
    linked_config.rename(real_config)
    linked_config.symlink_to(real_config, target_is_directory=True)
    environment["XDG_CONFIG_HOME"] = str(linked_config)
    with pytest.raises(install.InstallError, match="symlinked path component"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)


def test_hostile_paths_are_escaped_in_launcher_and_component_configs(tmp_path: Path) -> None:
    environment, _game_root, _runtime = _environment(tmp_path)
    marker = tmp_path / "executed"
    hostile_root = tmp_path / ("game;$(touch " + str(marker) + ')"\\é')
    (hostile_root / "_classic_beta_" / "Interface" / "AddOns").mkdir(parents=True)
    _write_host(environment["XDG_CONFIG_HOME"], hostile_root, tmp_path / "wine")
    plan = install.build_plan(install.InstallRequest(game_root=hostile_root), environment)
    configs = {
        item.relative_name: item.content
        for item in plan.files
        if item.relative_name in {"config/scanner.toml"}
    }
    assert all(content is not None for content in configs.values())
    scanner = tomllib.loads(configs["config/scanner.toml"].decode("utf-8"))
    assert set(scanner) == {"scanner"}
    assert scanner["scanner"]["game_root"] == str(hostile_root)
    launcher = next(
        item.content for item in plan.files if item.relative_name == "launcher/crowley-world-of-warcraft"
    )
    assert launcher is not None
    assert "$(touch" in launcher.decode("utf-8")
    assert not marker.exists()


def test_launcher_uses_host_display_and_requires_xauthority(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    plan = install.build_plan(install.InstallRequest(game_root=game_root), environment)
    launcher = next(
        item.content for item in plan.files if item.relative_name == "launcher/crowley-world-of-warcraft"
    )
    assert launcher is not None
    rendered = launcher.decode("utf-8")
    assert "DISPLAY_VALUE=:crowley-test" in rendered
    assert "${DISPLAY" not in rendered
    assert "${XAUTHORITY:-" not in rendered
    assert ":-" not in rendered

    launcher_path = tmp_path / "launcher"
    launcher_path.write_bytes(launcher)
    launcher_path.chmod(0o700)
    child_environment = dict(environment)
    child_environment["CROWLEY_WINE"] = "/bin/true"
    child_environment.pop("XAUTHORITY", None)
    completed = subprocess.run(
        [str(launcher_path)],
        env=child_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "set XAUTHORITY" in completed.stderr


def test_launcher_selects_the_target_product_without_repeated_exec(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    target = load_game_target(TARGET_FIXTURE)
    plan = install.build_plan(install.InstallRequest(game_root=game_root), environment)
    launcher = next(
        item.content for item in plan.files if item.relative_name == "launcher/crowley-world-of-warcraft"
    )
    assert launcher is not None
    rendered = launcher.decode("utf-8")

    assert f"PRODUCT={target.product}" in rendered
    assert 'battle_net_exec "launch_uid $PRODUCT"' in rendered
    assert f"EXECUTABLE={target.executable}" in rendered
    exec_invocations = [
        line
        for line in rendered.splitlines()
        if line.startswith('    game_env "$WINE" "$BATTLE_NET" --exec=')
    ]
    assert len(exec_invocations) == 1
    assert exec_invocations[0].count("--exec") == 1
    assert "--class '^battle\\.net\\.exe$'" in rendered
    assert "--name" not in rendered  # xdotool shares one pattern across --class/--name
    assert "getwindowname" in rendered
    assert "-b remove,maximized_vert,maximized_horz" in rendered
    assert "-remove WM_NORMAL_HINTS" in rendered
    assert "_MOTIF_WM_HINTS '0x2, 0x0, 0x0, 0x0, 0x0'" in rendered
    assert "_NET_WM_STATE_FULLSCREEN" in rendered
    assert "xwininfo -id" in rendered
    # The only keyboard Return is the Enter World press sent to the game window.
    assert rendered.count("xdotool key") == 1
    assert "x11 xdotool key --clearmodifiers Return" in rendered
    assert rendered.index('"$CROWLEY_DESKTOP" play') < rendered.index("xdotool key --clearmodifiers Return")
    # Enter World repeats until a canvas frame decodes (no blind fixed settle).
    assert "CHARACTER_SELECT_SETTLE_SECONDS" not in rendered
    assert "ENTER_WORLD_SETTLE_SECONDS=15" in rendered
    assert "ENTER_WORLD_TIMEOUT_SECONDS=600" in rendered
    assert "ENTER_WORLD_ATTEMPTS" not in rendered
    assert 'x11 "$CROWLEY_DESKTOP" probe-frame || probe=$?' in rendered
    # Return only on exit 1; 0 breaks; anything else fails.
    assert "0) break ;;" in rendered and "1) gui_step enter_world ;;" in rendered
    assert "*) fail " in rendered
    assert rendered.index("probe-frame") < rendered.index("1) gui_step enter_world")
    # Every input step holds the shared GUI lease and passes the presence gate.
    assert '"$CROWLEY_DESKTOP" presence-ok --for-input' in rendered
    assert '9>"$GUI_LOCK"' in rendered and 'flock -w "$GUI_LEASE_WAIT_SECONDS" 9' in rendered
    assert 'GUI_LOCK="$GUI_LOCK_DIR/gui.lock"' in rendered
    assert rendered.count("gui_step x11 xdotool windowactivate --sync") == 2
    play_call = 'gui_step x11 "$CROWLEY_DESKTOP" play --window "$window"'
    assert play_call in rendered
    assert "crowley-ops" not in rendered
    assert "gui_step enter_world" in rendered
    assert rendered.index("xdotool key --clearmodifiers Return") > rendered.index(
        'xdotool windowactivate --sync "$game_window"'
    )
    assert "xdotool key --clearmodifiers Escape" not in rendered
    assert rendered.count("battle_net_exec 'focus play'") == 1
    assert '"$CROWLEY_DESKTOP" play --window "$window"' in rendered
    assert "PLAY_ATTEMPTS=3" in rendered
    assert "GAME_START_ATTEMPT_SECONDS=20" in rendered
    assert "xdotool click" not in rendered
    assert "mousemove" not in rendered


def test_launcher_rejects_missing_single_instance_before_wine(
    tmp_path: Path,
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    target = load_game_target(TARGET_FIXTURE)
    executable = game_root / target.product_dir / target.executable
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"game executable")

    user_name = pwd.getpwuid(os.getuid()).pw_name
    config = (
        tmp_path
        / "wine"
        / "drive_c"
        / "users"
        / user_name
        / "AppData"
        / "Roaming"
        / "Battle.net"
        / "Battle.net.config"
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}", encoding="utf-8")

    wine = tmp_path / "wine-recorder"
    record = tmp_path / "wine-invocations"
    wine.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CROWLEY_WINE_RECORD"\n', encoding="utf-8")
    wine.chmod(0o700)
    xauthority = tmp_path / "Xauthority"
    xauthority.write_bytes(b"xauthority")

    plan = install.build_plan(install.InstallRequest(game_root=game_root), environment)
    launcher = next(
        item.content for item in plan.files if item.relative_name == "launcher/crowley-world-of-warcraft"
    )
    assert launcher is not None
    launcher_path = tmp_path / "launcher"
    launcher_path.write_bytes(launcher)
    launcher_path.chmod(0o700)

    child_environment = dict(environment)
    child_environment.update(
        {
            "CROWLEY_WINE": str(wine),
            "CROWLEY_WINE_RECORD": str(record),
            "XAUTHORITY": str(xauthority),
        }
    )
    completed = subprocess.run(
        [str(launcher_path)],
        env=child_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert 'Client.SingleInstance must be "true"' in completed.stderr
    assert not record.exists()


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


@pytest.mark.parametrize(
    "contents",
    [b"[scanner\n", b"[scanner]\nunknown_setting = true\n"],
    ids=("malformed-toml", "unknown-setting"),
)
def test_install_refuses_invalid_existing_scanner_config_without_mutations(
    tmp_path: Path, contents: bytes
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    scanner_path = tmp_path / "config" / "crowley" / "scanner.toml"
    scanner_path.write_bytes(contents)

    with pytest.raises(install.InstallError, match="existing scanner.toml is invalid; repair or remove"):
        install.install(install.InstallRequest(game_root=game_root, dry_run=True), environment)

    assert scanner_path.read_bytes() == contents
    assert not (tmp_path / "state/crowley/install/manifest.json").exists()
    assert not (tmp_path / "config/systemd/user/crowley-host.service").exists()
    assert not (game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley").exists()


def test_combined_install_renders_the_host_bridge_and_coordinated_mode(tmp_path: Path) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    plan = install.build_plan(install.InstallRequest(game_root=game_root), environment)
    files = {item.relative_name: item.content for item in plan.files}
    assert files["addon/Crowley_Host/Crowley_Host.toc"] is not None
    assert b"ns.Coordinated = true" in files["addon/Crowley_Automation/Mode.lua"]
    standalone_mode = (
        Path(__file__).parents[2]
        / "packages"
        / "automation"
        / "src"
        / "crowley_automation"
        / "addon"
        / "Crowley_Automation"
        / "Mode.lua"
    ).read_bytes()
    assert b"ns.Coordinated = false" in standalone_mode


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
    addons = game_root / "_classic_beta_" / "Interface" / "AddOns"
    (addons / "Crowley_Data").symlink_to(legacy, target_is_directory=True)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    target = addons / "Crowley_Data"
    assert target.is_dir() and not target.is_symlink()
    assert (legacy / "Crowley_Data.toc").read_text(encoding="utf-8") == "legacy"
    assert (target / "Data.lua").read_text(encoding="utf-8") == ('CROWLEY_DATA = { ["generatedAt"] = 99 }\n')


def test_runtime_owned_lua_is_preserved_and_not_digest_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, game_root, _runtime = _environment(tmp_path)
    _no_systemd(monkeypatch)
    install.install(install.InstallRequest(game_root=game_root), environment)
    data = game_root / "_classic_beta_" / "Interface" / "AddOns" / "Crowley_Data" / "Data.lua"
    data.write_text('CROWLEY_DATA = { ["generatedAt"] = 42 }\n', encoding="utf-8")
    install.install(install.InstallRequest(game_root=game_root), environment)
    assert 'generatedAt"] = 42' in data.read_text(encoding="utf-8")
    manifest = json.loads((tmp_path / "state/crowley/install/manifest.json").read_text(encoding="utf-8"))
    assert "sha256" not in manifest["files"]["addon/Crowley_Data/Data.lua"]


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
        assert "crowley_host/ops/package_data/docker/Dockerfile" in names
        assert "crowley_host/ops/package_data/systemd/crowley-health.service.in" in names
        assert "crowley_host/ops/package_data/systemd/crowley-host.service.in" in names
        assert "crowley_host/ops/package_data/config/host.example.toml" in names
        assert not any("launcher" in name or "/addon/" in name for name in names)
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
        CROWLEY_DATABASE_URL="postgresql://crowley:host-only@db.invalid:5432/crowley",
        CROWLEY_API_TOKEN="dummy",
        CROWLEY_BIND="0.0.0.0",
        CROWLEY_IMAGE_VERSION="0.2.0",
        XDG_CONFIG_HOME=str(config_home),
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(install.compose_file_path()),
            "config",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert "build:" not in completed.stdout
    assert "image: crowley-server:0.2.0" in completed.stdout
    assert "postgresql://crowley:dummy@db:5432/crowley" in completed.stdout
    compose_text = install.compose_file_path().read_text(encoding="utf-8")
    assert "CROWLEY_HOST: 0.0.0.0" in compose_text
    assert '"${CROWLEY_BIND:?set CROWLEY_BIND in secrets.env}:8080:8080"' in compose_text
    host_endpoint = "db.invalid:5432"
    assert host_endpoint not in completed.stdout


def test_packaged_compose_keeps_container_host_and_host_bind_variable() -> None:
    compose_text = install.compose_file_path().read_text(encoding="utf-8")
    server_marker = "\n  server:\n"
    assert server_marker in compose_text
    server_text = compose_text.split(server_marker, 1)[1]
    volumes_marker = "\nvolumes:\n"
    assert volumes_marker in server_text
    server_text = server_text.split(volumes_marker, 1)[0]
    assert "CROWLEY_HOST: 0.0.0.0" in server_text
    assert '"${CROWLEY_BIND:?set CROWLEY_BIND in secrets.env}:8080:8080"' in server_text


def test_image_build_from_wheel_stages_only_packaged_inputs(tmp_path: Path) -> None:
    market_wheel = tmp_path / "crowley_market-0.2.0-py3-none-any.whl"
    common_wheel = tmp_path / "crowley_common-0.2.0-py3-none-any.whl"
    market_wheel.write_bytes(b"market wheel bytes")
    common_wheel.write_bytes(b"common wheel bytes")
    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(command: object, cwd: Path) -> None:
        assert isinstance(command, tuple)
        typed_command = tuple(str(value) for value in command)
        calls.append((typed_command, cwd))
        if typed_command[0] == "docker":
            context = Path(typed_command[-1])
            staged_wheels = tuple(path for path in context.iterdir() if path.suffix == ".whl")
            assert {path.name for path in context.iterdir()} == {
                "Dockerfile",
                market_wheel.name,
                common_wheel.name,
            }
            assert {parse_wheel_filename(path.name)[0] for path in staged_wheels} == {
                "crowley-market",
                "crowley-common",
            }
            assert {path.read_bytes() for path in staged_wheels} == {
                b"market wheel bytes",
                b"common wheel bytes",
            }
            dockerfile = (context / "Dockerfile").read_bytes()
            assert b"COPY *.whl /build/" in dockerfile
            assert b"/build/*.whl" in dockerfile

    tag = install.build_image(wheel=market_wheel, common_wheel=common_wheel, runner=runner)
    assert tag == "crowley-server:0.2.0"
    assert len(calls) == 1
    assert calls[0][0][:4] == ("docker", "build", "--tag", "crowley-server:0.2.0")


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
            (output_dir / "crowley_common-0.2.0-py3-none-any.whl").write_bytes(b"built common wheel")
            (output_dir / "crowley_market-0.2.0-py3-none-any.whl").write_bytes(b"built market wheel")
        else:
            context = Path(typed_command[-1])
            staged_wheels = tuple(path for path in context.iterdir() if path.suffix == ".whl")
            assert {path.name for path in context.iterdir()} == {
                "Dockerfile",
                "crowley_common-0.2.0-py3-none-any.whl",
                "crowley_market-0.2.0-py3-none-any.whl",
            }
            assert len(staged_wheels) == 2
            assert {path.read_bytes() for path in staged_wheels} == {
                b"built common wheel",
                b"built market wheel",
            }

    tag = install.build_image(source=source, runner=runner)
    assert tag == "crowley-server:0.2.0"
    assert [call[0][0] for call in calls] == ["uv", "docker"]
    assert calls[0][1] == source
