"""Shared target, host, tunables, and package-resource fixtures."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from crowley.config import (
    CompanionTunables,
    HostConfig,
    SessionTunables,
    resolve_session,
)
from crowley.core.game_target import GameTarget, load_game_target
from crowley.platform.paths import XdgDirs
from crowley.session.config import SessionConfig
from crowley.session.driver import DriverProfile
from crowley.session.protocol import ProtocolContract

FIXTURE_TARGET_PATH = Path(__file__).parents[1] / "fixtures" / "targets" / "verified-beta.json"


def fixture_target() -> GameTarget:
    """Return the explicit test target instead of the packaged selection."""

    return load_game_target(FIXTURE_TARGET_PATH)


def fixture_host(root: Path) -> HostConfig:
    """Return a complete host contract rooted under the test directory."""

    return HostConfig.model_validate(
        {
            "wine_prefix": str(root / "wine"),
            "game_root": str(root / "game"),
            "wtf_account": "account#1",
            "character": "Arthur",
            "display": ":0",
            "server_url": "http://server.example.invalid:8080",
            "window_title": "World of Warcraft",
            "client_x": 0,
            "client_y": 0,
            "client_width": 1920,
            "client_height": 1080,
            "outer_width": 1928,
            "outer_height": 1092,
            "window_client_offset": [0, 0],
            "canvas_origin": [80, 48],
            "canvas_crop": [80, 48, 256, 256],
            "model_crop": [80, 48, 1760, 984],
        }
    )


def fixture_session_tunables() -> SessionTunables:
    """Return the complete tracked session tunables fixture."""

    return SessionTunables(
        quiet_margin_seconds=15,
        upload_deadline_seconds=60,
        reload_deadline_seconds=45,
        ack_deadline_seconds=60,
    )


def fixture_companion_tunables() -> CompanionTunables:
    """Return the complete tracked companion tunables fixture."""

    return CompanionTunables(poll_seconds=3.0, refresh_minutes=5.0)


def fixture_xdg(root: Path) -> XdgDirs:
    """Return private XDG directories rooted under the test directory."""

    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    (runtime / "crowley").mkdir(mode=0o700, exist_ok=True)
    return XdgDirs.from_environ(
        {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(runtime),
        }
    )


def fixture_codex_path(root: Path) -> Path:
    """Create an explicit pinned Codex fixture executable."""

    path = root / "codex"
    path.write_text("#!/bin/sh\nprintf 'codex-cli 0.162.1\\n'\n", encoding="ascii")
    path.chmod(0o700)
    return path


def fixture_session_config(
    root: Path,
    *,
    target: GameTarget | None = None,
    host: HostConfig | None = None,
    tunables: SessionTunables | None = None,
    profile: DriverProfile | None = None,
    journal: Path | None = None,
    socket: Path | None = None,
    capture_dir: Path | None = None,
    companion_state: Path | None = None,
    saved_variables: Path | None = None,
    data_lua: Path | None = None,
    routes_file: Path | None = None,
    codex_path: Path | None = None,
) -> SessionConfig:
    """Build a resolver-backed daemon config with an optional test route override.

    Production resolution always supplies the packaged target route.  Mail-loop
    composition tests may pass their generated TEST route set explicitly.
    """

    selected = fixture_target() if target is None else target
    machine = fixture_host(root) if host is None else host
    values = fixture_session_tunables() if tunables is None else tunables
    resolved = resolve_session(selected, machine, values, xdg=fixture_xdg(root))
    plan = resolved.canonical_plan
    plan_hash = plan.get("planHash")
    plan_json = plan.get("planJSON")
    if not isinstance(plan_hash, str) or not isinstance(plan_json, str):
        raise AssertionError("fixture target canonical plan is incomplete")
    protocol_contract = ProtocolContract.from_plan(plan, sweep_cap_seconds=resolved.sweep_cap_seconds)
    fixture_profile = profile or DriverProfile(
        display=machine.display,
        wine_prefix=machine.wine_prefix,
        client_x=machine.client_x,
        client_y=machine.client_y,
        client_width=machine.client_width,
        client_height=machine.client_height,
        outer_width=machine.outer_width,
        outer_height=machine.outer_height,
        window_client_offset=machine.window_client_offset,
        canvas_origin=machine.canvas_origin,
        canvas_crop=machine.canvas_crop,
        model_crop=machine.model_crop,
        window_title=machine.window_title,
        window_class=selected.window_class,
        executable=selected.executable,
        character=machine.character,
        realm=selected.realm,
        house=selected.house,
        plan_count=len(plan["itemIDs"]),
        protocol_contract=protocol_contract,
    )
    return SessionConfig(
        profile=fixture_profile,
        journal=resolved.journal if journal is None else journal,
        socket=resolved.socket if socket is None else socket,
        codex_path=str(fixture_codex_path(root) if codex_path is None else codex_path),
        model_neutral_dir=resolved.model_neutral_dir,
        cadence_seconds=resolved.cadence_seconds,
        sweep_cap_seconds=resolved.sweep_cap_seconds,
        quiet_margin_seconds=values.quiet_margin_seconds,
        reload_deadline_seconds=values.reload_deadline_seconds,
        ack_deadline_seconds=values.ack_deadline_seconds,
        upload_deadline_seconds=values.upload_deadline_seconds,
        capture_dir=resolved.capture_dir if capture_dir is None else capture_dir,
        companion_state=resolved.companion_state if companion_state is None else companion_state,
        saved_variables=resolved.saved_variables if saved_variables is None else saved_variables,
        data_lua=resolved.data_lua if data_lua is None else data_lua,
        plan_hash=plan_hash,
        plan_json=plan_json,
        client_build=resolved.client_build,
        interface=resolved.interface,
        player_faction=resolved.player_faction,
        locale=resolved.locale,
        client_version=resolved.client_version,
        routes_file=resolved.route_file if routes_file is None else routes_file,
        target=resolved.target,
        catalog=resolved.catalog,
        canonical_plan=resolved.canonical_plan,
        route_provenance=resolved.route_provenance,
        account_dir=resolved.account_dir,
        protocol_contract=protocol_contract,
    )


def fixture_route_path() -> Path:
    """Return the selected fixture route from packaged data."""

    resource = resources.files("crowley.data").joinpath(
        "routes", fixture_target().target_id, "stormwind.json"
    )
    if not isinstance(resource, Path):
        raise AssertionError("fixture route must be available as a filesystem path")
    return resource


def write_host_toml(path: Path, host: HostConfig) -> None:
    """Write a complete host contract for a session loader test."""

    lines = (
        f"wine_prefix = {json.dumps(host.wine_prefix)}",
        f"game_root = {json.dumps(host.game_root)}",
        f"wtf_account = {json.dumps(host.wtf_account)}",
        f"character = {json.dumps(host.character)}",
        f"display = {json.dumps(host.display)}",
        f"server_url = {json.dumps(host.server_url)}",
        f"window_title = {json.dumps(host.window_title)}",
        f"client_x = {host.client_x}",
        f"client_y = {host.client_y}",
        f"client_width = {host.client_width}",
        f"client_height = {host.client_height}",
        f"outer_width = {host.outer_width}",
        f"outer_height = {host.outer_height}",
        f"window_client_offset = {list(host.window_client_offset)}",
        f"canvas_origin = {list(host.canvas_origin)}",
        f"canvas_crop = {list(host.canvas_crop)}",
        f"model_crop = {list(host.model_crop)}",
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_session_toml(path: Path, tunables: SessionTunables | None = None) -> None:
    """Write only the strict session tunables table."""

    value = fixture_session_tunables() if tunables is None else tunables
    path.write_text(
        "\n".join(
            (
                "[session]",
                f"quiet_margin_seconds = {value.quiet_margin_seconds}",
                f"upload_deadline_seconds = {value.upload_deadline_seconds}",
                f"reload_deadline_seconds = {value.reload_deadline_seconds}",
                f"ack_deadline_seconds = {value.ack_deadline_seconds}",
                "",
            )
        ),
        encoding="utf-8",
    )


def write_companion_toml(path: Path, tunables: CompanionTunables | None = None) -> None:
    """Write only the strict companion tunables table."""

    value = fixture_companion_tunables() if tunables is None else tunables
    path.write_text(
        "\n".join(
            (
                "[companion]",
                f"poll_seconds = {value.poll_seconds}",
                f"refresh_minutes = {value.refresh_minutes}",
                "",
            )
        ),
        encoding="utf-8",
    )


__all__ = [
    "FIXTURE_TARGET_PATH",
    "fixture_companion_tunables",
    "fixture_codex_path",
    "fixture_host",
    "fixture_route_path",
    "fixture_session_tunables",
    "fixture_session_config",
    "fixture_target",
    "fixture_xdg",
    "write_companion_toml",
    "write_host_toml",
    "write_session_toml",
]
