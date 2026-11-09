"""Service-specific configuration resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from crowley import data as crowley_data
from crowley.core.game_target import CatalogLike, GameTarget, target_scan_plan
from crowley.core.watchlist import Catalog, load_catalog
from crowley.platform.paths import XdgDirs

from .binaries import resolve_pinned_observer
from .host import HostConfig
from .tunables import CompanionTunables, OpsTunables, SessionTunables


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


def _route_file(target: GameTarget, route_name: str) -> Path | None:
    """Return the target's packaged route, or None when no route was measured.

    A target owns a route only through ``routes/<targetId>/``.  A target without
    that directory has no mailbox route, and route consumers stay unavailable;
    a route directory missing the named route is a packaging error.
    """

    if not route_name or Path(route_name).name != route_name or Path(route_name).suffix != ".json":
        raise ResolutionError("route name must be one JSON file name")
    directory = resources.files(crowley_data).joinpath("routes", target.target_id)
    if not isinstance(directory, Path):
        raise ResolutionError("packaged route resources must be available as filesystem files")
    if not directory.exists():
        return None
    resource = directory / route_name
    if not resource.is_file():
        raise ResolutionError(f"packaged route does not exist: {target.target_id}/{route_name}")
    return resource


def _composition(target: GameTarget, catalog: Catalog | None) -> tuple[Catalog, dict[str, object]]:
    selected_catalog = load_catalog(enforce_target=False) if catalog is None else catalog
    target.validate_catalog(cast(CatalogLike, selected_catalog))
    plan = target_scan_plan(cast(CatalogLike, selected_catalog), target)
    return selected_catalog, plan


@dataclass(frozen=True, slots=True)
class SessionResolvedConfig:
    target: GameTarget
    catalog: Catalog
    canonical_plan: dict[str, object]
    route_provenance: str
    host: HostConfig
    tunables: SessionTunables
    flavor_dir: Path
    account_dir: Path
    journal: Path
    socket: Path
    capture_dir: Path
    saved_variables: Path
    companion_state: Path
    data_lua: Path
    queue_lua: Path
    route_file: Path | None
    state_dir: Path
    runtime_dir: Path
    model_neutral_dir: Path
    client_build: str
    interface: int
    player_faction: str
    locale: str
    client_version: str
    cadence_seconds: int
    sweep_cap_seconds: int
    identity_observer_executable: Path
    identity_observer_executable_sha256: str
    identity_observer_version: str


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


def resolve_session(
    target: GameTarget,
    host: HostConfig,
    tunables: SessionTunables,
    *,
    xdg: XdgDirs | None = None,
    catalog: Catalog | None = None,
) -> SessionResolvedConfig:
    """Derive every session path from target, host, and session tunables."""

    dirs = _xdg_dirs(xdg)
    flavor = _flavor_dir(target, host)
    state = _state_dir(target, host, dirs)
    runtime = dirs.crowley_runtime
    account = host.wtf_account
    addon = flavor / "Interface" / "AddOns" / "Crowley_Data"
    account_dir = flavor / "WTF" / "Account" / account
    selected_catalog, plan = _composition(target, catalog)
    observer = resolve_pinned_observer(dirs)
    saved = account_dir / "SavedVariables" / "Crowley.lua"
    return SessionResolvedConfig(
        target=target,
        catalog=selected_catalog,
        canonical_plan=plan,
        route_provenance=target.route_provenance,
        host=host,
        tunables=tunables,
        flavor_dir=flavor,
        account_dir=account_dir,
        journal=state / "journal.json",
        socket=runtime / "session.sock",
        capture_dir=runtime / "captures",
        saved_variables=saved,
        companion_state=state / "companion.json",
        data_lua=addon / "Data.lua",
        queue_lua=addon / "Queue.lua",
        route_file=_route_file(target, "stormwind.json"),
        state_dir=state,
        runtime_dir=runtime,
        model_neutral_dir=runtime / "model-neutral",
        client_build=target.native_build,
        interface=target.interface,
        player_faction=target.faction,
        locale=target.locale,
        client_version=target.client_version,
        cadence_seconds=target.cadence_seconds,
        sweep_cap_seconds=target.sweep_cap_seconds,
        identity_observer_executable=observer.path,
        identity_observer_executable_sha256=observer.sha256,
        identity_observer_version=observer.version,
    )


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
    "SessionResolvedConfig",
    "resolve_companion",
    "resolve_ops",
    "resolve_session",
]
