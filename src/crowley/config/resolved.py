"""Service-specific configuration resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley.core.game_target import CatalogLike, GameTarget, target_scan_plan
from crowley.core.watchlist import Catalog, load_catalog
from crowley.platform.paths import XdgDirs

from .host import HostConfig
from .tunables import CompanionTunables, OpsTunables


class ResolutionError(ValueError):
    """Raised when derived configuration cannot be resolved safely."""


def _xdg_dirs(xdg: XdgDirs | None) -> XdgDirs:
    if xdg is not None:
        return xdg
    try:
        return XdgDirs.from_environ(os.environ)
    except ValueError as exc:
        raise ResolutionError(f"XDG directories are unavailable: {exc}") from exc


def _state_dir(target: GameTarget, host: HostConfig, xdg: XdgDirs) -> Path:
    return xdg.crowley_state / target.house / target.target_id / host.wtf_account / host.character


def _flavor_dir(target: GameTarget, host: HostConfig) -> Path:
    return host.game_root_path / target.product_dir


def _composition(target: GameTarget, catalog: Catalog | None) -> tuple[Catalog, dict[str, object]]:
    selected_catalog = load_catalog(enforce_target=False) if catalog is None else catalog
    target.validate_catalog(cast(CatalogLike, selected_catalog))
    plan = target_scan_plan(cast(CatalogLike, selected_catalog), target)
    return selected_catalog, plan


@dataclass(frozen=True, slots=True)
class CompanionResolvedConfig:
    target: GameTarget
    catalog: Catalog
    canonical_plan: dict[str, object]
    route_provenance: str
    host: HostConfig
    tunables: CompanionTunables
    flavor_dir: Path
    account_dir: Path
    saved_variables: Path
    data_lua: Path
    queue_lua: Path
    state_file: Path
    state_dir: Path
    server_url: str
    addons_dir: Path


@dataclass(frozen=True, slots=True)
class OpsResolvedConfig:
    target: GameTarget
    catalog: Catalog
    canonical_plan: dict[str, object]
    route_provenance: str
    host: HostConfig
    tunables: OpsTunables
    flavor_dir: Path
    account_root: Path
    account_dir: Path
    addon_dir: Path
    data_addon_dir: Path
    saved_variables: Path
    data_lua: Path
    queue_lua: Path
    addons_dir: Path
    state_dir: Path
    runtime_dir: Path
    config_dir: Path


def resolve_companion(
    target: GameTarget,
    host: HostConfig,
    tunables: CompanionTunables,
    *,
    xdg: XdgDirs | None = None,
    catalog: Catalog | None = None,
) -> CompanionResolvedConfig:
    """Derive companion game, SavedVariables, addon, and state paths."""

    dirs = _xdg_dirs(xdg)
    flavor = _flavor_dir(target, host)
    state = _state_dir(target, host, dirs)
    addon = flavor / "Interface" / "AddOns" / "Crowley_Data"
    account_dir = flavor / "WTF" / "Account" / host.wtf_account
    selected_catalog, plan = _composition(target, catalog)
    return CompanionResolvedConfig(
        target=target,
        catalog=selected_catalog,
        canonical_plan=plan,
        route_provenance=target.route_provenance,
        host=host,
        tunables=tunables,
        flavor_dir=flavor,
        account_dir=account_dir,
        saved_variables=account_dir / "SavedVariables" / "Crowley.lua",
        data_lua=addon / "Data.lua",
        queue_lua=addon / "Queue.lua",
        state_file=state / "companion.json",
        state_dir=state,
        server_url=host.server_url,
        addons_dir=flavor / "Interface" / "AddOns",
    )


def resolve_ops(
    target: GameTarget,
    host: HostConfig,
    tunables: OpsTunables,
    *,
    xdg: XdgDirs | None = None,
    catalog: Catalog | None = None,
) -> OpsResolvedConfig:
    """Derive operational installation and runtime roots."""

    dirs = _xdg_dirs(xdg)
    flavor = _flavor_dir(target, host)
    account_root = flavor / "WTF" / "Account"
    account_dir = account_root / host.wtf_account
    addons_dir = flavor / "Interface" / "AddOns"
    addon_dir = addons_dir / "Crowley"
    data_addon_dir = addons_dir / "Crowley_Data"
    selected_catalog, plan = _composition(target, catalog)
    return OpsResolvedConfig(
        target=target,
        catalog=selected_catalog,
        canonical_plan=plan,
        route_provenance=target.route_provenance,
        host=host,
        tunables=tunables,
        flavor_dir=flavor,
        account_root=account_root,
        account_dir=account_dir,
        addon_dir=addon_dir,
        data_addon_dir=data_addon_dir,
        saved_variables=account_dir / "SavedVariables" / "Crowley.lua",
        data_lua=data_addon_dir / "Data.lua",
        queue_lua=data_addon_dir / "Queue.lua",
        addons_dir=addons_dir,
        state_dir=_state_dir(target, host, dirs),
        runtime_dir=dirs.crowley_runtime,
        config_dir=dirs.crowley_config,
    )


__all__ = [
    "CompanionResolvedConfig",
    "OpsResolvedConfig",
    "ResolutionError",
    "resolve_companion",
    "resolve_ops",
]
