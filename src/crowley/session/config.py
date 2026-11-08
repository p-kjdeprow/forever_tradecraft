"""Session configuration assembled from the selected target and host contract."""

from __future__ import annotations

import grp
import os
import pwd
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley.config import (
    CODEX_CLI_VERSION,
    CODEX_MODEL,
    BinaryConfigError,
    SessionResolvedConfig,
    host_config_path,
    load_host_config,
    load_pinned_binaries,
    load_session_tunables,
    resolve_session,
)
from crowley.core.game_target import GameTarget, selected_target
from crowley.core.watchlist import Catalog
from crowley.platform.paths import XdgDirs

from .driver import DriverProfile
from .protocol import ProtocolContract

DEFAULT_RELOAD_DEADLINE_SECONDS = 45
MAX_RELOAD_DEADLINE_SECONDS = 600


def _private_group_error(gid: int) -> str | None:
    """Return why a group is not suitable for a private session directory."""

    current_gid = os.getgid()
    if gid != current_gid:
        return f"gid {gid} differs from the current user's gid {current_gid}"
    try:
        user_name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError) as exc:
        return f"current user cannot be resolved for gid {gid}: {exc}"
    try:
        group = grp.getgrgid(gid)
    except KeyError:
        return f"gid {gid} has no group entry"
    if group.gr_name != user_name:
        return f"group name {group.gr_name!r} does not match user {user_name!r}"
    if group.gr_mem not in ([], [user_name]):
        return f"group {group.gr_name!r} has members other than {user_name!r}"
    try:
        current_uid = os.getuid()
        other_primary_users = [
            entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid and entry.pw_uid != current_uid
        ]
    except (KeyError, OSError) as exc:
        return f"primary-group accounts cannot be resolved for gid {gid}: {exc}"
    if other_primary_users:
        return f"gid {gid} is also a primary group for {other_primary_users[0]!r}"
    return None


def _validate_route_metadata(metadata: os.stat_result, *, parent: bool) -> None:
    if parent:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("routes_file parent directory must be a directory")
        label = "routes_file parent directory"
    else:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("routes_file must be a user-owned regular file")
        label = "routes_file"
    if metadata.st_mode & stat.S_IWOTH:
        raise ValueError(f"{label} must not be world writable")
    if metadata.st_mode & stat.S_IWGRP:
        reason = _private_group_error(metadata.st_gid)
        if reason is not None:
            raise ValueError(f"{label} group write requires a private group: {reason}")


def open_routes_descriptor(path: Path) -> int:
    """Open a packaged route while checking the descriptor actually consumed."""

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        _validate_route_metadata(os.fstat(parent_fd), parent=True)
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        _validate_route_metadata(os.fstat(descriptor), parent=False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True)
class ClientConfig:
    """Strict configuration required by a session IPC client."""

    socket: Path


@dataclass(frozen=True)
class SessionConfig:
    profile: DriverProfile
    journal: Path
    socket: Path
    codex_path: str
    model_neutral_dir: Path
    cadence_seconds: int = 900
    sweep_cap_seconds: int = 600
    quiet_margin_seconds: int = 15
    reload_deadline_seconds: int = DEFAULT_RELOAD_DEADLINE_SECONDS
    ack_deadline_seconds: int = 60
    upload_deadline_seconds: int = 60
    model: str = CODEX_MODEL
    cli_version: str = CODEX_CLI_VERSION
    capture_dir: Path = Path()
    companion_state: Path | None = None
    saved_variables: Path | None = None
    data_lua: Path | None = None
    plan_hash: str = ""
    plan_json: str = ""
    client_build: str = ""
    interface: int = 0
    player_faction: str = ""
    locale: str = ""
    client_version: str = ""
    addon_version: str = "0.2.0"
    pointer_profile: Path | None = None
    camera_profile: Path | None = None
    move_profile: Path | None = None
    telemetry_region_executable: Path | None = None
    telemetry_region_executable_sha256: str | None = None
    identity_observer_executable: Path | None = None
    identity_observer_executable_sha256: str | None = None
    routes_file: Path | None = None
    target: GameTarget | None = None
    catalog: Catalog | None = None
    canonical_plan: dict[str, object] | None = None
    route_provenance: str | None = None
    account_dir: Path | None = None
    protocol_contract: ProtocolContract | None = None


def _validate_socket_path(value: object) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("session.socket must be a non-empty path")
    socket_path = Path(value).expanduser()
    if not socket_path.is_absolute():
        raise ValueError("session.socket must be an absolute path")
    try:
        parent_metadata = os.lstat(socket_path.parent)
    except OSError as exc:
        raise ValueError(f"session.socket parent directory is unavailable: {socket_path.parent}") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("session.socket parent must be a directory")
    if parent_metadata.st_uid != os.getuid():
        raise ValueError("session.socket parent directory must be owned by the current user")
    if parent_metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("session.socket parent directory must be private")
    try:
        socket_metadata = os.lstat(socket_path)
    except FileNotFoundError:
        return socket_path
    except OSError as exc:
        raise ValueError(f"session.socket cannot be inspected: {socket_path}") from exc
    if stat.S_ISLNK(socket_metadata.st_mode):
        raise ValueError("session.socket must not be a symlink")
    return socket_path


def load_client_config(path: str | Path, *, socket_override: str | Path | None = None) -> ClientConfig:
    """Load only the runtime socket needed by an IPC client."""

    del path
    if socket_override is not None:
        return ClientConfig(socket=_validate_socket_path(socket_override))
    try:
        xdg = XdgDirs.from_environ(os.environ)
    except ValueError as exc:
        raise ValueError(f"session socket cannot be resolved from XDG_RUNTIME_DIR: {exc}") from exc
    return ClientConfig(socket=_validate_socket_path(xdg.crowley_runtime / "session.sock"))


def _driver_profile(resolved: SessionResolvedConfig, plan_count: int) -> DriverProfile:
    target = resolved.target
    host = resolved.host
    return DriverProfile(
        display=host.display,
        wine_prefix=host.wine_prefix,
        client_x=host.client_x,
        client_y=host.client_y,
        client_width=host.client_width,
        client_height=host.client_height,
        outer_width=host.outer_width,
        outer_height=host.outer_height,
        window_client_offset=cast(tuple[int, int], host.window_client_offset),
        canvas_origin=cast(tuple[int, int], host.canvas_origin),
        canvas_crop=cast(tuple[int, int, int, int], host.canvas_crop),
        model_crop=cast(tuple[int, int, int, int], host.model_crop),
        window_title=host.window_title,
        window_class=target.window_class,
        executable=target.executable,
        character=host.character,
        realm=target.realm,
        house=target.house,
        plan_count=plan_count,
        identity_observer_executable=resolved.identity_observer_executable,
        identity_observer_executable_sha256=resolved.identity_observer_executable_sha256,
        protocol_contract=ProtocolContract.from_plan(
            resolved.canonical_plan,
            sweep_cap_seconds=resolved.sweep_cap_seconds,
        ),
    )


def load_config(
    path: str | Path,
    *,
    expected_target: GameTarget | None = None,
    socket_override: str | Path | None = None,
) -> SessionConfig:
    """Load tunables and assemble the daemon-facing resolved session config."""

    session_path = Path(path)
    target = selected_target() if expected_target is None else expected_target
    host = load_host_config(host_config_path())
    tunables = load_session_tunables(session_path)
    resolved = resolve_session(target, host, tunables)
    plan = resolved.canonical_plan
    plan_hash = plan.get("planHash")
    plan_json = plan.get("planJSON")
    if not isinstance(plan_hash, str) or not isinstance(plan_json, str):
        raise ValueError("selected target canonical plan is incomplete")
    plan_items = plan.get("itemIDs")
    if not isinstance(plan_items, list):
        raise ValueError("selected target canonical plan has no item IDs")
    try:
        binaries = load_pinned_binaries()
    except BinaryConfigError as exc:
        raise ValueError(str(exc)) from exc
    if socket_override is not None:
        socket = _validate_socket_path(socket_override)
    else:
        socket = _validate_socket_path(resolved.socket)
    profile = _driver_profile(resolved, len(plan_items))
    return SessionConfig(
        profile=profile,
        journal=resolved.journal,
        socket=socket,
        cadence_seconds=resolved.cadence_seconds,
        sweep_cap_seconds=resolved.sweep_cap_seconds,
        quiet_margin_seconds=tunables.quiet_margin_seconds,
        reload_deadline_seconds=tunables.reload_deadline_seconds,
        ack_deadline_seconds=tunables.ack_deadline_seconds,
        upload_deadline_seconds=tunables.upload_deadline_seconds,
        model=binaries.model,
        cli_version=binaries.cli_version,
        codex_path=str(binaries.codex_path),
        model_neutral_dir=resolved.model_neutral_dir,
        capture_dir=resolved.capture_dir,
        companion_state=resolved.companion_state,
        saved_variables=resolved.saved_variables,
        data_lua=resolved.data_lua,
        plan_hash=plan_hash,
        plan_json=plan_json,
        client_build=resolved.client_build,
        interface=resolved.interface,
        player_faction=resolved.player_faction,
        locale=resolved.locale,
        client_version=resolved.client_version,
        routes_file=resolved.route_file,
        target=resolved.target,
        catalog=resolved.catalog,
        canonical_plan=resolved.canonical_plan,
        route_provenance=resolved.route_provenance,
        account_dir=resolved.account_dir,
        protocol_contract=ProtocolContract.from_plan(
            resolved.canonical_plan,
            sweep_cap_seconds=resolved.sweep_cap_seconds,
        ),
    )


__all__ = [
    "ClientConfig",
    "DEFAULT_RELOAD_DEADLINE_SECONDS",
    "MAX_RELOAD_DEADLINE_SECONDS",
    "SessionConfig",
    "load_client_config",
    "load_config",
    "open_routes_descriptor",
]
