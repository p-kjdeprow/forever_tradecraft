"""Shared target, host, tunables, and package-resource fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from crowley_common.game_target import GameTarget, load_game_target
from crowley_common.platform.paths import XdgDirs
from crowley_host.config import HostConfig

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
            "canvas_crop": [80, 48, 256, 256],
            "automation_canvas_crop": [352, 48, 256, 256],
        }
    )


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
        f"canvas_crop = {list(host.canvas_crop)}",
        f"automation_canvas_crop = {list(host.automation_canvas_crop)}",
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "FIXTURE_TARGET_PATH",
    "fixture_host",
    "fixture_target",
    "fixture_xdg",
    "write_host_toml",
]
