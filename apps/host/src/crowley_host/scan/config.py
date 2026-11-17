"""Configuration composition for the scanner-aware host loop."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley_automation.config import AutomationConfig
from crowley_automation.config import load_config as load_automation_config
from crowley_automation.gui.process import OwnedGameMatcher
from crowley_common.frames import ProtocolContract
from crowley_common.game_target import CatalogLike, selected_target, target_scan_plan
from crowley_common.platform.paths import XdgDirs
from crowley_common.watchlist import load_catalog
from crowley_scanner.companion.config import ScannerConfig, load_scanner_config

from crowley_host.config import host_config_path, load_host_config

from .policy import Target

AUCTIONEER_NAME = "Auctioneer Jaxon"
_TOC_VERSION = re.compile(r"^## Version:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


class ScanConfigError(ValueError):
    """The host scan loop configuration cannot be resolved safely."""


@dataclass(frozen=True)
class ScanConfig:
    target: Target
    automation: AutomationConfig
    matcher: OwnedGameMatcher
    window_class: str
    window_title: str
    display: str
    canvas_crop: tuple[int, int, int, int]
    automation_canvas_crop: tuple[int, int, int, int]
    saved_variables: Path
    companion_state: Path
    data_lua: Path
    contract: ProtocolContract


def addon_version(toc: Path) -> str:
    """Version of the installed addon named by its TOC."""

    try:
        text = toc.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScanConfigError(f"addon TOC is unreadable: {toc}: {exc}") from exc
    match = _TOC_VERSION.search(text)
    if match is None:
        raise ScanConfigError(f"addon TOC has no '## Version:' line: {toc}")
    return match.group(1)


def load_scan_config(
    *,
    host_path: str | Path | None = None,
    scanner_path: str | Path | None = None,
    automation_path: str | Path | None = None,
) -> ScanConfig:
    """Join host display settings and standalone scanner settings at the host boundary."""

    game = selected_target()
    host = load_host_config(host_config_path() if host_path is None else host_path)
    scanner: ScannerConfig = load_scanner_config(scanner_path)
    automation = load_automation_config(automation_path)
    catalog = load_catalog()
    game.validate_catalog(cast(CatalogLike, catalog))
    plan = target_scan_plan(cast(CatalogLike, catalog), game)
    plan_hash = plan.get("planHash")
    if not isinstance(plan_hash, str) or not plan_hash:
        raise ScanConfigError("selected target canonical plan has no planHash")

    expected_automation = {
        "wine_prefix": host.wine_prefix,
        "game_root": host.game_root,
        "display": host.display,
        "window_title": host.window_title,
        "game_executable": game.executable,
        "game_argv0": game.windows_argv0,
        "window_class": game.window_class,
        "product": game.product,
        "product_dir": game.product_dir,
        "character": host.character,
        "realm": game.realm,
        "character_select_name": host.character_select_name,
        "faction": game.faction,
        "house": game.house,
        "build": game.full_build,
        "interface": game.interface,
        "native_region": game.native_region_id,
        "canvas_crop": host.automation_canvas_crop,
    }
    if any(getattr(automation, key) != value for key, value in expected_automation.items()):
        raise ScanConfigError("automation.toml is stale relative to host.toml and selected target")

    flavor = Path(scanner.game_root) / game.product_dir
    addons_dir = flavor / "Interface" / "AddOns"
    scanner_version = addon_version(addons_dir / "Crowley" / "Crowley.toc")
    automation_version = addon_version(addons_dir / "Crowley_Automation" / "Crowley_Automation.toc")
    target = Target(
        character=scanner.character,
        realm=game.realm,
        faction=game.faction,
        house_key=game.house,
        plan_hash=plan_hash,
        build=game.native_build,
        interface=game.interface,
        addon_version=automation_version,
        scanner_addon_version=scanner_version,
        auctioneer=AUCTIONEER_NAME,
        native_region=game.native_region_id,
    )
    matcher = OwnedGameMatcher.for_game(
        game_executable=game.executable,
        game_argv0=game.windows_argv0,
        wine_prefix=str(host.wine_prefix_path),
    )
    account = flavor / "WTF" / "Account" / scanner.wtf_account
    state_dir = (
        XdgDirs.from_environ(os.environ).crowley_state
        / game.house
        / game.target_id
        / scanner.wtf_account
        / scanner.character
    )
    return ScanConfig(
        target=target,
        automation=automation,
        matcher=matcher,
        window_class=game.window_class,
        window_title=host.window_title,
        display=host.display,
        canvas_crop=host.canvas_crop,
        automation_canvas_crop=host.automation_canvas_crop,
        saved_variables=account / "SavedVariables" / "Crowley.lua",
        companion_state=state_dir / "companion.json",
        data_lua=addons_dir / "Crowley_Data" / "Data.lua",
        contract=ProtocolContract.from_plan(plan, sweep_cap_seconds=game.sweep_cap_seconds),
    )
