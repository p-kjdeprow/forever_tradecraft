"""Host installation path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley_common.game_target import CatalogLike, GameTarget, target_scan_plan
from crowley_common.platform.paths import XdgDirs
from crowley_common.watchlist import Catalog, load_catalog

from .host import HostConfig


class ResolutionError(ValueError):
    """Host paths or the selected target cannot be resolved safely."""


def _xdg_dirs(xdg: XdgDirs | None) -> XdgDirs:
    if xdg is not None:
        return xdg
    try:
        return XdgDirs.from_environ(os.environ)
    except ValueError as exc:
        raise ResolutionError(f"XDG directories are unavailable: {exc}") from exc


@dataclass(frozen=True, slots=True)
class OpsResolvedConfig:
    target: GameTarget
    catalog: Catalog
    canonical_plan: dict[str, object]
    route_provenance: str
    host: HostConfig
    flavor_dir: Path
    account_root: Path
    account_dir: Path
    addon_dir: Path
    data_addon_dir: Path
    saved_variables: Path
    data_lua: Path
    addons_dir: Path
    state_dir: Path
    runtime_dir: Path
    config_dir: Path


def resolve_ops(
    target: GameTarget,
    host: HostConfig,
    *,
    xdg: XdgDirs | None = None,
    catalog: Catalog | None = None,
) -> OpsResolvedConfig:
    """Resolve host installation and health paths from one target and host file."""

    dirs = _xdg_dirs(xdg)
    flavor = host.game_root_path / target.product_dir
    account_root = flavor / "WTF" / "Account"
    account_dir = account_root / host.wtf_account
    addons_dir = flavor / "Interface" / "AddOns"
    selected_catalog = load_catalog(enforce_target=False) if catalog is None else catalog
    target.validate_catalog(cast(CatalogLike, selected_catalog))
    plan = target_scan_plan(cast(CatalogLike, selected_catalog), target)
    state_dir = dirs.crowley_state / target.house / target.target_id / host.wtf_account / host.character
    return OpsResolvedConfig(
        target=target,
        catalog=selected_catalog,
        canonical_plan=plan,
        route_provenance=target.route_provenance,
        host=host,
        flavor_dir=flavor,
        account_root=account_root,
        account_dir=account_dir,
        addon_dir=addons_dir / "Crowley",
        data_addon_dir=addons_dir / "Crowley_Data",
        saved_variables=account_dir / "SavedVariables" / "Crowley.lua",
        data_lua=addons_dir / "Crowley_Data" / "Data.lua",
        addons_dir=addons_dir,
        state_dir=state_dir,
        runtime_dir=dirs.crowley_runtime,
        config_dir=dirs.crowley_config,
    )


__all__ = ["OpsResolvedConfig", "ResolutionError", "resolve_ops"]
