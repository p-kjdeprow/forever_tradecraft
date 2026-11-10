"""Shared target, host, tunables, and package-resource fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from crowley.config import CompanionTunables, HostConfig
from crowley.core.game_target import GameTarget, load_game_target
from crowley.platform.paths import XdgDirs

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
    "fixture_host",
    "fixture_target",
    "fixture_xdg",
    "write_companion_toml",
    "write_host_toml",
]
