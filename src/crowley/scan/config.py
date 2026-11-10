"""Scan-loop configuration: the selected target plus the host contract, nothing else."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from crowley.config import host_config_path, load_host_config
from crowley.config.resolved import resolve_companion
from crowley.config.tunables import CompanionTunables
from crowley.core.game_target import selected_target
from crowley.gui.frames import ProtocolContract
from crowley.gui.process import OwnedGameMatcher

from .policy import Target

# The auctioneer the AH opener targets (addon-side name, Stormwind City).  Recorded in the
# assumptions table in docs/ARCHITECTURE.md.
AUCTIONEER_NAME = "Auctioneer Jaxon"
_TOC_VERSION = re.compile(r"^## Version:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


class ScanConfigError(ValueError):
    """The scan loop's configuration cannot be resolved."""


@dataclass(frozen=True)
class ScanConfig:
    target: Target
    matcher: OwnedGameMatcher
    window_class: str
    window_title: str
    display: str
    canvas_crop: tuple[int, int, int, int]
    saved_variables: Path
    companion_state: Path
    data_lua: Path
    contract: ProtocolContract


def addon_version(toc: Path) -> str:
    """Version of the installed Crowley addon, the value the addon reports as ns.VERSION."""

    try:
        text = toc.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScanConfigError(f"addon TOC is unreadable: {toc}: {exc}") from exc
    match = _TOC_VERSION.search(text)
    if match is None:
        raise ScanConfigError(f"addon TOC has no '## Version:' line: {toc}")
    return match.group(1)


def load_scan_config() -> ScanConfig:
    game = selected_target()
    host = load_host_config(host_config_path())
    # The companion resolution derives exactly the game paths and plan we need; the
    # tunables are unused by the scan loop, so any valid pair will do.
    resolved = resolve_companion(game, host, CompanionTunables(poll_seconds=1.0, refresh_minutes=1.0))
    plan_hash = resolved.canonical_plan.get("planHash")
    if not isinstance(plan_hash, str) or not plan_hash:
        raise ScanConfigError("selected target canonical plan has no planHash")
    version = addon_version(resolved.addons_dir / "Crowley" / "Crowley.toc")
    target = Target(
        character=host.character,
        realm=game.realm,
        faction=game.faction,
        house_key=game.house,
        plan_hash=plan_hash,
        build=game.native_build,
        interface=game.interface,
        addon_version=version,
        auctioneer=AUCTIONEER_NAME,
    )
    matcher = OwnedGameMatcher.for_game(
        game_executable=game.executable,
        game_argv0=game.windows_argv0,
        wine_prefix=str(host.wine_prefix_path),
    )
    return ScanConfig(
        target=target,
        matcher=matcher,
        window_class=game.window_class,
        window_title=host.window_title,
        display=host.display,
        canvas_crop=host.canvas_crop,
        saved_variables=resolved.saved_variables,
        companion_state=resolved.state_file,
        data_lua=resolved.data_lua,
        contract=ProtocolContract.from_plan(
            resolved.canonical_plan, sweep_cap_seconds=game.sweep_cap_seconds
        ),
    )
