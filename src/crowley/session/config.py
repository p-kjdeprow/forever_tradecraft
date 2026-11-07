"""TOML configuration for the measured local profile."""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crowley.core.game_target import GameTarget, packaged_game_target, target_scan_plan
from crowley.core.watchlist import load_catalog

from .driver import DriverProfile

_TARGET = packaged_game_target()
_CANONICAL_CLIENT_VERSION = _TARGET.client_version
_CANONICAL_CLIENT_BUILD = _TARGET.native_build
_CANONICAL_INTERFACE = _TARGET.interface
_CANONICAL_FACTION = _TARGET.faction
_CANONICAL_LOCALE = _TARGET.locale
DEFAULT_RELOAD_DEADLINE_SECONDS = 45
MAX_RELOAD_DEADLINE_SECONDS = 600


def _private_group_error(gid: int) -> str | None:
    """Return why *gid* is not the current user's private primary group."""

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


def _validate_route_metadata(metadata: object, *, parent: bool) -> None:
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
    """Open a configured routes file and validate the opened objects.

    The parent directory is opened first and the file is then opened relative
    to that descriptor.  Both checks therefore apply to the objects actually
    used by the caller; the caller must read and close the returned descriptor.
    """

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        parent_stat = os.fstat(parent_fd)
        _validate_route_metadata(parent_stat, parent=True)
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        metadata = os.fstat(descriptor)
        _validate_route_metadata(metadata, parent=False)
        return descriptor
    except Exception:
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
    cadence_seconds: int = _TARGET.cadence_seconds
    sweep_cap_seconds: int = _TARGET.sweep_cap_seconds
    quiet_margin_seconds: int = 15
    reload_deadline_seconds: int = DEFAULT_RELOAD_DEADLINE_SECONDS
    ack_deadline_seconds: int = 60
    upload_deadline_seconds: int = 60
    model: str = "gpt-6-luna"
    cli_version: str = "codex-cli 0.162.1"
    codex_path: str = (
        "/home/dev/.cache/crowley/codex/0.162.1/node_modules/@openai/codex-linux-x64/"
        "vendor/x86_64-unknown-linux-musl/bin/codex"
    )
    capture_dir: Path = Path("/run/user/1000/crowley/captures")
    companion_state: Path | None = None
    saved_variables: Path | None = None
    data_lua: Path | None = None
    plan_hash: str = ""
    plan_json: str = ""
    client_build: str = _CANONICAL_CLIENT_BUILD
    interface: int = _CANONICAL_INTERFACE
    player_faction: str = _CANONICAL_FACTION
    locale: str = _CANONICAL_LOCALE
    client_version: str = _CANONICAL_CLIENT_VERSION
    addon_version: str = "0.2.0"
    pointer_profile: Path | None = None
    camera_profile: Path | None = None
    move_profile: Path | None = None
    telemetry_region_executable: Path | None = None
    telemetry_region_executable_sha256: str | None = None
    identity_observer_executable: Path | None = None
    identity_observer_executable_sha256: str | None = None
    routes_file: Path | None = None


def _load_raw_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("session configuration must be a TOML table")
    return raw


def _validate_socket_path(value: object) -> Path:
    if not isinstance(value, (str, Path)) or not value:
        raise ValueError("session.socket must be a non-empty string")
    try:
        socket_path = Path(value).expanduser()
    except (RuntimeError, ValueError) as exc:
        raise ValueError("session.socket must be a valid path") from exc
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
        pass
    except OSError as exc:
        raise ValueError(f"session.socket cannot be inspected: {socket_path}") from exc
    else:
        if stat.S_ISLNK(socket_metadata.st_mode):
            raise ValueError("session.socket must not be a symlink")
    return socket_path


def _client_config_from_raw(raw: dict[str, Any]) -> ClientConfig:
    session = raw.get("session")
    if not isinstance(session, dict):
        raise ValueError("session table is required")
    if "socket" not in session:
        raise ValueError("session.socket is required")
    return ClientConfig(socket=_validate_socket_path(session["socket"]))


def load_client_config(path: str | Path, *, socket_override: str | Path | None = None) -> ClientConfig:
    """Load only the socket configuration needed by an IPC client."""

    if socket_override is not None:
        return ClientConfig(socket=_validate_socket_path(socket_override))
    raw = _load_raw_config(Path(path))
    return _client_config_from_raw(raw)


def load_config(
    path: str | Path,
    *,
    expected_target: GameTarget | None = None,
    socket_override: str | Path | None = None,
) -> SessionConfig:
    target = Path(path)
    raw = _load_raw_config(target)
    launch_target = expected_target or _TARGET
    canonical_plan = target_scan_plan(load_catalog(), launch_target)
    canonical_client_version = launch_target.client_version
    canonical_client_build = launch_target.native_build
    canonical_interface = launch_target.interface
    canonical_faction = launch_target.faction
    canonical_locale = launch_target.locale
    canonical_wow_root = Path(launch_target.game_root) / launch_target.product_dir
    profile = raw.get("profile", {})
    session = raw.get("session", {})
    if not isinstance(profile, dict) or not isinstance(session, dict):
        raise ValueError("profile and session must be TOML tables")
    if "socket" not in session:
        raise ValueError("session.socket is required")
    client_config = (
        ClientConfig(socket=_validate_socket_path(socket_override))
        if socket_override is not None
        else _client_config_from_raw(raw)
    )

    def required_profile_string(name: str, expected: str) -> str:
        value = profile.get(name)
        if not isinstance(value, str) or value != expected:
            raise ValueError(f"profile.{name} must match the packaged launch target")
        return value

    configured_wine_prefix = required_profile_string("wine_prefix", launch_target.wine_prefix)
    configured_window_class = required_profile_string("window_class", launch_target.window_class)
    configured_executable = required_profile_string("executable", launch_target.executable)
    configured_realm = required_profile_string("realm", launch_target.realm)
    configured_house = required_profile_string("house", launch_target.house)

    def tuple4(name: str, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        value = profile.get(name, default)
        if not isinstance(value, list) or len(value) != 4 or any(not isinstance(item, int) for item in value):
            raise ValueError(f"{name} must contain four integer coordinates")
        return tuple(value)  # type: ignore[return-value]

    origin = profile.get("canvas_origin", [80, 48])
    if not isinstance(origin, list) or len(origin) != 2 or any(not isinstance(item, int) for item in origin):
        raise ValueError("canvas_origin must contain two integer coordinates")
    window_offset = profile.get("window_client_offset", [0, 0])
    if (
        not isinstance(window_offset, list)
        or len(window_offset) != 2
        or any(not isinstance(item, int) for item in window_offset)
    ):
        raise ValueError("window_client_offset must contain two integer coordinates")
    if "action_anchors" in profile or "anchor_fingerprints" in profile:
        raise ValueError(
            "manual action anchors are not accepted; controls must be measured from a fresh live scene"
        )
    # Coordinates and copied identity fingerprints are deliberately not a
    # configuration surface.  The native visual producer measures one control
    # from one fresh scene and the driver seals it to that capture.
    anchors: dict[str, tuple[int, int]] = {}
    anchor_fingerprints: dict[str, str] = {}
    telemetry_raw = session.get("telemetry_region_executable")
    telemetry_hash = session.get("telemetry_region_executable_sha256")
    if telemetry_raw is None and telemetry_hash is None:
        telemetry_path: Path | None = None
        telemetry_digest: str | None = None
    else:
        if not isinstance(telemetry_raw, str) or not Path(telemetry_raw).is_absolute():
            raise ValueError("telemetry_region_executable must be an absolute path")
        if not isinstance(telemetry_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", telemetry_hash):
            raise ValueError("telemetry_region_executable_sha256 must be a lowercase SHA-256")
        telemetry_path = Path(telemetry_raw)
        if not telemetry_path.is_file() or not os.access(telemetry_path, os.X_OK):
            raise ValueError("telemetry region executable is unavailable")
        if hashlib.sha256(telemetry_path.read_bytes()).hexdigest() != telemetry_hash:
            raise ValueError("telemetry region executable hash does not match")
        telemetry_digest = telemetry_hash
    observer_raw = session.get("identity_observer_executable")
    observer_hash = session.get("identity_observer_executable_sha256")
    if observer_raw is None and observer_hash is None:
        observer_path: Path | None = None
        observer_digest: str | None = None
    else:
        if not isinstance(observer_raw, str) or not Path(observer_raw).is_absolute():
            raise ValueError("identity_observer_executable must be an absolute path")
        if not isinstance(observer_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", observer_hash):
            raise ValueError("identity_observer_executable_sha256 must be a lowercase SHA-256")
        observer_path = Path(observer_raw)
        if not observer_path.is_file() or not os.access(observer_path, os.X_OK):
            raise ValueError("identity observer executable is unavailable")
        if hashlib.sha256(observer_path.read_bytes()).hexdigest() != observer_hash:
            raise ValueError("identity observer executable hash does not match")
        observer_digest = observer_hash
    driver_profile = DriverProfile(
        display=str(profile.get("display", ":0")),
        xauthority=str(profile.get("xauthority", "")),
        wine_prefix=configured_wine_prefix,
        client_width=int(profile.get("client_width", 1920)),
        client_height=int(profile.get("client_height", 1080)),
        outer_width=int(profile.get("outer_width", 1928)),
        outer_height=int(profile.get("outer_height", 1092)),
        window_client_offset=tuple(window_offset),
        action_anchors=anchors,
        canvas_origin=tuple(origin),
        canvas_crop=tuple4("canvas_crop", (80, 48, 256, 256)),
        model_crop=tuple4("model_crop", (80, 48, 1760, 984)),
        window_title=str(profile.get("window_title", "World of Warcraft")),
        window_class=configured_window_class,
        executable=configured_executable,
        character=str(profile.get("character", "Arthur")),
        realm=configured_realm,
        house=configured_house,
        plan_count=int(profile.get("plan_count", len(canonical_plan["itemIDs"]))),
        anchor_fingerprints=anchor_fingerprints,
        telemetry_region_executable=telemetry_path,
        telemetry_region_executable_sha256=telemetry_digest,
        identity_observer_executable=observer_path,
        identity_observer_executable_sha256=observer_digest,
    )
    if not driver_profile.window_title or not driver_profile.window_class:
        raise ValueError("window title and class are required for one-client identity")
    if (
        driver_profile.plan_count,
        session.get("cadence_seconds", launch_target.cadence_seconds),
        session.get("sweep_cap_seconds", launch_target.sweep_cap_seconds),
    ) != (len(canonical_plan["itemIDs"]), launch_target.cadence_seconds, launch_target.sweep_cap_seconds):
        raise ValueError(
            f"configured plan must be {len(canonical_plan['itemIDs'])} items with "
            f"{launch_target.cadence_seconds}/{launch_target.sweep_cap_seconds} cadence and cap"
        )
    if (
        str(profile.get("client_build", canonical_client_build)) != canonical_client_build
        or int(profile.get("interface", canonical_interface)) != canonical_interface
    ):
        raise ValueError(
            f"configured client must match target build {launch_target.full_build}/"
            f"interface {canonical_interface}"
        )
    if (
        str(profile.get("player_faction", canonical_faction)) != canonical_faction
        or str(profile.get("locale", canonical_locale)) != canonical_locale
        or str(profile.get("client_version", canonical_client_version)) != canonical_client_version
        or str(profile.get("addon_version", "0.2.0")) != "0.2.0"
    ):
        raise ValueError("configured client context does not match the pinned Forever profile")
    configured_plan_hash = profile.get("plan_hash")
    configured_plan_json = profile.get("plan_json")
    if not isinstance(configured_plan_hash, str) or not isinstance(configured_plan_json, str):
        raise ValueError("plan_hash and plan_json must be strings")
    if configured_plan_hash and not re.fullmatch(r"[0-9a-f]{64}", configured_plan_hash):
        raise ValueError("plan_hash must be a lowercase SHA-256 value")
    if bool(configured_plan_hash) != bool(configured_plan_json):
        raise ValueError("plan_hash and plan_json must be configured together")
    if not configured_plan_hash:
        raise ValueError("the canonical plan hash and plan JSON are required before startup")
    if configured_plan_json and (
        hashlib.sha256(configured_plan_json.encode()).hexdigest() != configured_plan_hash
    ):
        raise ValueError("plan_hash does not match canonical plan_json")
    if (
        configured_plan_hash != canonical_plan["planHash"]
        or configured_plan_json != canonical_plan["planJSON"]
    ):
        raise ValueError("configured plan must exactly match the packaged target canonical plan")
    left, top, width, height = driver_profile.model_crop
    if (
        driver_profile.model_crop == driver_profile.canvas_crop
        or left < 0
        or top < 0
        or width < 640
        or height < 480
        or left + width > driver_profile.client_width
        or top + height > driver_profile.client_height
    ):
        raise ValueError("model_crop must be a separately verified game-client crop")
    quiet_margin = int(session.get("quiet_margin_seconds", 15))
    reload_deadline = int(session.get("reload_deadline_seconds", DEFAULT_RELOAD_DEADLINE_SECONDS))
    ack_deadline = int(session.get("ack_deadline_seconds", 60))
    upload_deadline = int(session.get("upload_deadline_seconds", 60))
    if (
        not 0 < quiet_margin < 900
        or not 1 <= reload_deadline <= MAX_RELOAD_DEADLINE_SECONDS
        or not 1 <= ack_deadline <= MAX_RELOAD_DEADLINE_SECONDS
        or not 1 <= upload_deadline <= 60
    ):
        raise ValueError("session deadlines are outside the pinned bounds")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    journal_path = Path(session.get("journal", state_home / "crowley" / "session.json")).expanduser()
    socket_path = client_config.socket
    saved_path = Path(session["saved_variables"]).expanduser() if session.get("saved_variables") else None
    companion_path = Path(session["companion_state"]).expanduser() if session.get("companion_state") else None
    data_path = Path(session["data_lua"]).expanduser() if session.get("data_lua") else None
    for label, configured_path in (("SavedVariables", saved_path), ("Data.lua", data_path)):
        if configured_path is None or not configured_path.is_absolute():
            continue
        if not configured_path.is_relative_to(canonical_wow_root):
            raise ValueError(f"{label} path must be below the packaged target flavor root")
    routes_path: Path | None = None
    if "routes_file" in session:
        configured_routes = session["routes_file"]
        if not isinstance(configured_routes, str) or not configured_routes:
            raise ValueError("routes_file must be an absolute path")
        routes_path = Path(configured_routes).expanduser()
        if not routes_path.is_absolute():
            raise ValueError("routes_file must be an absolute path")
        try:
            descriptor = open_routes_descriptor(routes_path)
            os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise ValueError(f"routes_file is unavailable: {routes_path}") from exc
    motion_profiles: dict[str, Path | None] = {}
    for role in ("pointer", "camera", "move"):
        configured = session.get(f"{role}_profile")
        if configured in (None, "baseline"):
            motion_profiles[role] = None
        elif isinstance(configured, str) and Path(configured).is_absolute():
            motion_profiles[role] = Path(configured)
        else:
            raise ValueError(f"{role}_profile must be 'baseline' or an absolute profile path")
    for label, required_path in (
        ("SavedVariables", saved_path),
        ("companion state", companion_path),
        ("Data.lua", data_path),
    ):
        if required_path is None:
            raise ValueError(f"{label} path is required before startup")
        if not required_path.is_absolute():
            raise ValueError(f"{label} path must be absolute")
        if not required_path.is_file():
            raise ValueError(f"{label} file does not exist: {required_path}")
    codex_path = str(session.get("codex_path", "/home/dev/.local/share/pnpm/nodejs/22.23.1/bin/codex"))
    if not Path(codex_path).is_absolute():
        raise ValueError("codex_path must be an absolute executable path")
    if (
        str(session.get("model", "gpt-6-luna")) != "gpt-6-luna"
        or str(session.get("cli_version", "codex-cli 0.162.1")) != "codex-cli 0.162.1"
    ):
        raise ValueError("model and Codex CLI version must match the pinned startup contract")
    return SessionConfig(
        profile=driver_profile,
        # The journal contains restart intent, intervention history, and
        # accepted evidence.  Keep it in the persistent user state directory;
        # only the socket/capture paths belong under the runtime directory.
        journal=journal_path,
        socket=socket_path,
        cadence_seconds=launch_target.cadence_seconds,
        sweep_cap_seconds=launch_target.sweep_cap_seconds,
        quiet_margin_seconds=quiet_margin,
        reload_deadline_seconds=reload_deadline,
        ack_deadline_seconds=ack_deadline,
        upload_deadline_seconds=upload_deadline,
        model=str(session.get("model", "gpt-6-luna")),
        cli_version=str(session.get("cli_version", "codex-cli 0.162.1")),
        codex_path=codex_path,
        capture_dir=Path(session.get("capture_dir", "/run/user/1000/crowley/captures")).expanduser(),
        companion_state=companion_path,
        saved_variables=saved_path,
        data_lua=data_path,
        plan_hash=str(profile.get("plan_hash", "")),
        plan_json=str(profile.get("plan_json", "")),
        client_build=str(profile.get("client_build", canonical_client_build)),
        interface=int(profile.get("interface", canonical_interface)),
        player_faction=str(profile.get("player_faction", canonical_faction)),
        locale=str(profile.get("locale", canonical_locale)),
        client_version=str(profile.get("client_version", canonical_client_version)),
        addon_version=str(profile.get("addon_version", "0.2.0")),
        pointer_profile=motion_profiles["pointer"],
        camera_profile=motion_profiles["camera"],
        move_profile=motion_profiles["move"],
        telemetry_region_executable=telemetry_path,
        telemetry_region_executable_sha256=telemetry_digest,
        identity_observer_executable=observer_path,
        identity_observer_executable_sha256=observer_digest,
        routes_file=routes_path,
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
