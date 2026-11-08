"""Read-only installation and runtime checks for ``crowley-ops doctor``."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import psycopg

from crowley.config import (
    BinaryConfigError,
    CompanionResolvedConfig,
    CompanionTunables,
    HostConfig,
    HostConfigError,
    TunablesError,
    host_config_path,
    load_companion_tunables,
    load_host_config,
    load_pinned_binaries,
    load_session_tunables,
    resolve_companion,
    resolve_pinned_observer,
)
from crowley.core import savedvars
from crowley.core.game_target import GameTarget, packaged_game_target
from crowley.platform import privfs
from crowley.platform.paths import XdgDirs
from crowley.server.db import MigrationStatus, create_pool, migration_status

from . import install

MAX_CONFIG_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """All problems found by one doctor run."""

    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


DatabaseCheck = Callable[[str], MigrationStatus]


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME")
    if not isinstance(value, str) or not value:
        raise ValueError("HOME must be set")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError("HOME must be an absolute non-root path without traversal")
    return path


def _entrypoint_path(home: Path, value: str) -> Path:
    prefix = "%h/.local/bin/"
    if not value.startswith(prefix) or "/" in value.removeprefix(prefix):
        raise ValueError(f"unit entrypoint is not an installed user binary: {value}")
    return home / ".local" / "bin" / value.removeprefix(prefix)


def _check_executable(path: Path, problems: list[str], label: str) -> None:
    try:
        info = path.lstat()
    except OSError:
        problems.append(f"{label} is unavailable: {path}")
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        problems.append(f"{label} is not a regular file: {path}")
    elif not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        problems.append(f"{label} is not executable: {path}")


def _check_units(xdg: XdgDirs, environ: Mapping[str, str], problems: list[str]) -> None:
    expected = {name for name, _ in install.render_units()}
    unit_dir = xdg.config_home / "systemd" / "user"
    home = _home(environ)
    entrypoints = {
        "crowley-companion.service": "%h/.local/bin/crowley-companion",
        "crowley-health.service": "%h/.local/bin/crowley-ops",
        "crowley-market-backup.service": "%h/.local/bin/crowley-ops",
        "crowley-paper-trading.service": "%h/.local/bin/crowley-paper-step",
        "crowley-session.service": "%h/.local/bin/crowley-session",
    }
    for name in sorted(expected):
        path = unit_dir / name
        if not path.is_file() or path.is_symlink():
            problems.append(f"unit not installed: {path}")
            continue
        try:
            text = privfs.read_bounded(path, 128 * 1024).decode("utf-8")
        except (OSError, UnicodeError, privfs.PrivateFileError):
            problems.append(f"unit is unreadable: {path}")
            continue
        if any(fragment in text for fragment in ("/home/", "/run/user/", "repos/", "/checkout/", ".venv")):
            problems.append(f"unit contains a checkout path: {path}")
        if name.endswith(".timer"):
            continue
        executable = entrypoints.get(name)
        if executable is None or f"ExecStart={executable}" not in text:
            problems.append(f"unit does not use installed entrypoint: {path}")
        else:
            _check_executable(_entrypoint_path(home, executable), problems, "unit entrypoint")
        if "EnvironmentFile=%E/crowley/secrets.env" not in text:
            problems.append(f"unit does not load the secrets file: {path}")


def _manifest(path: Path, problems: list[str]) -> dict[str, object]:
    try:
        return install.load_manifest(path)
    except install.InstallError as exc:
        problems.append(f"install manifest is invalid: {exc}")
        return {}


def _manifest_entry(value: object, name: str) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    entry = value.get(name)
    return entry if isinstance(entry, dict) else None


def _manifest_roots(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    roots: dict[str, str] = {}
    for key, root in value.items():
        if not isinstance(key, str) or not isinstance(root, str):
            return None
        roots[key] = root
    return roots


def _file_digest(path: Path) -> str:
    return hashlib.sha256(privfs.read_bounded(path, install.MAX_RESOURCE_BYTES)).hexdigest()


def _check_runtime_lua(path: Path, global_name: str, problems: list[str]) -> None:
    try:
        value = savedvars.load_file(path)
    except (OSError, UnicodeError, savedvars.SavedVariablesError) as exc:
        problems.append(f"runtime-owned Lua is not parseable: {path} ({exc})")
        return
    if global_name not in value:
        problems.append(f"runtime-owned Lua is missing {global_name}: {path}")


def _check_manifest_files(
    plan: install.InstallPlan,
    manifest: Mapping[str, object],
    problems: list[str],
) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        problems.append("install manifest files are invalid")
        return
    development = manifest.get("addon_mode") == "development"
    for item in plan.files:
        if development and item.relative_name.startswith("addon/Crowley/"):
            continue
        entry = _manifest_entry(files, item.relative_name)
        if entry is None:
            problems.append(f"install manifest entry missing: {item.relative_name}")
            continue
        if entry.get("root") != item.root_key:
            problems.append(f"install manifest root mismatch: {item.relative_name}")
            continue
        actual = item.destination
        if item.mutable:
            if entry.get("mutable") is not True:
                problems.append(f"runtime-owned manifest entry is not mutable: {item.relative_name}")
            if not actual.is_file() or actual.is_symlink():
                problems.append(f"runtime-owned file missing or not regular: {actual}")
            continue
        if item.symlink_target is not None:
            target = entry.get("target")
            if not actual.is_symlink() or not isinstance(target, str) or os.readlink(actual) != target:
                problems.append(f"development addon symlink changed: {actual}")
            continue
        if entry.get("kind") != "file" or not actual.is_file() or actual.is_symlink():
            problems.append(f"installed file missing or not regular: {actual}")
            continue
        digest = entry.get("sha256")
        if not isinstance(digest, str):
            problems.append(f"install manifest digest missing: {item.relative_name}")
            continue
        try:
            actual_digest = _file_digest(actual)
        except (OSError, privfs.PrivateFileError):
            problems.append(f"installed file cannot be read: {actual}")
            continue
        if actual_digest != digest:
            problems.append(f"installed file digest mismatch: {actual}")
        if digest != item.digest:
            problems.append(f"installed packaged file is stale: {item.relative_name}")


def _check_addon(
    plan: install.InstallPlan,
    target: GameTarget,
    problems: list[str],
) -> None:
    manifest = _manifest(plan.manifest_path, problems)
    if not manifest:
        problems.append(f"addon manifest missing: {plan.manifest_path}")
        return
    manifest_roots = _manifest_roots(manifest.get("roots"))
    if manifest_roots is None or manifest_roots != dict(plan.roots):
        problems.append("install manifest destination roots do not match the selected installation")
        return
    if manifest.get("target_id") != target.target_id or manifest.get("interface") != target.interface:
        problems.append("addon manifest target identity does not match packaged target")
    if manifest.get("product_dir") != target.product_dir:
        problems.append("addon manifest productDir does not match packaged target")
    _check_manifest_files(plan, manifest, problems)

    crowley = plan.flavor_dir / "Interface" / "AddOns" / "Crowley"
    addon_mode = manifest.get("addon_mode")
    if addon_mode not in {"packaged", "development"}:
        problems.append("addon manifest mode is invalid")
    if addon_mode == "development":
        entry = _manifest_entry(manifest.get("files"), "addon/Crowley")
        target_text = entry.get("target") if entry is not None else None
        if (
            entry is None
            or entry.get("kind") != "symlink"
            or entry.get("root") != "addon_crowley"
            or not crowley.is_symlink()
            or not isinstance(target_text, str)
            or os.readlink(crowley) != target_text
        ):
            problems.append(f"development addon symlink missing or changed: {crowley}")
    elif crowley.is_symlink() or not crowley.is_dir():
        problems.append(f"packaged addon directory missing or is a symlink: {crowley}")

    data_dir = plan.flavor_dir / "Interface" / "AddOns" / "Crowley_Data"
    _check_runtime_lua(data_dir / "Data.lua", "CROWLEY_DATA", problems)
    _check_runtime_lua(data_dir / "Queue.lua", "CROWLEY_QUEUE", problems)
    toc = crowley / "Crowley.toc"
    try:
        toc_text = privfs.read_bounded(toc, 128 * 1024).decode("utf-8")
    except (OSError, UnicodeError, privfs.PrivateFileError):
        problems.append(f"Crowley TOC is unreadable: {toc}")
    else:
        interface = next(
            (
                line.split(":", 1)[1].strip()
                for line in toc_text.splitlines()
                if line.startswith("## Interface:") and ":" in line
            ),
            None,
        )
        if interface != str(target.interface):
            problems.append(f"Crowley TOC interface {interface!r} does not equal {target.interface}")


def _check_launcher(plan: install.InstallPlan, problems: list[str]) -> None:
    path = plan.launcher_path
    try:
        info = path.lstat()
    except OSError:
        problems.append(f"launcher is unavailable: {path}")
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        problems.append(f"launcher is not a regular file: {path}")
    elif stat.S_IMODE(info.st_mode) != 0o700:
        problems.append(f"launcher must have mode 0700: {path}")


def _check_host(xdg: XdgDirs, problems: list[str]) -> HostConfig | None:
    path = host_config_path(
        {
            "XDG_CONFIG_HOME": str(xdg.config_home),
            "HOME": str(xdg.config_home.parent),
        }
    )
    try:
        return load_host_config(path)
    except HostConfigError as exc:
        problems.append(str(exc))
        return None


def _check_tunables(xdg: XdgDirs, problems: list[str]) -> None:
    path = xdg.crowley_config / "session.toml"
    try:
        load_session_tunables(path)
    except (OSError, TunablesError, ValueError) as exc:
        problems.append(f"session.toml is invalid: {exc}")


def _check_binary(
    path: Path,
    *,
    label: str,
    expected_digest: str | None,
    expected_version: str | None,
    problems: list[str],
) -> None:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        problems.append(f"{label} path is not absolute: {path}")
        return
    try:
        metadata = path.lstat()
    except OSError:
        problems.append(f"{label} is unavailable: {path}")
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        problems.append(f"{label} is not a regular file: {path}")
        return
    if metadata.st_uid != os.getuid():
        problems.append(f"{label} is not owned by the current user: {path}")
    if not metadata.st_mode & stat.S_IXUSR or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        problems.append(f"{label} permissions are unsafe: {path}")
    try:
        digest = _file_digest(path)
    except (OSError, privfs.PrivateFileError):
        problems.append(f"{label} cannot be read: {path}")
    else:
        if expected_digest is not None and digest != expected_digest:
            problems.append(f"{label} digest mismatch: {path}")
    if expected_version is None:
        return
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"{label} version could not be checked: {path} ({exc})")
        return
    version = result.stdout.strip()
    if result.returncode != 0 or version != expected_version:
        problems.append(f"{label} version does not match the pin: {path}")


def _check_pinned_binaries(xdg: XdgDirs, environ: Mapping[str, str], problems: list[str]) -> None:
    try:
        binaries = load_pinned_binaries(environ)
    except BinaryConfigError as exc:
        problems.append(str(exc))
    else:
        _check_binary(
            binaries.codex_path,
            label="pinned Codex executable",
            expected_digest=None,
            expected_version=binaries.cli_version,
            problems=problems,
        )
    observer = resolve_pinned_observer(xdg)
    _check_binary(
        observer.path,
        label="pinned identity observer",
        expected_digest=observer.sha256,
        expected_version=None,
        problems=problems,
    )


def _secret_values(path: Path) -> dict[str, str]:
    text = privfs.read_bounded(path, MAX_CONFIG_BYTES).decode("utf-8")
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"secrets.env line {number} is not KEY=value")
        key, value = line.split("=", 1)
        if not key or key in values:
            raise ValueError(f"secrets.env line {number} has an invalid key")
        values[key] = value
    return values


def _check_secrets(xdg: XdgDirs, problems: list[str]) -> dict[str, str]:
    path = xdg.crowley_config / "secrets.env"
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("secrets file must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
            problems.append(f"secrets file must be user-owned mode 0600: {path}")
        return _secret_values(path)
    except (OSError, UnicodeError, ValueError, privfs.PrivateFileError) as exc:
        problems.append(f"secrets file unavailable: {path} ({exc})")
        return {}


def _database_status(database_url: str) -> MigrationStatus:
    pool = create_pool(database_url)
    try:
        with pool.connection() as connection:
            return migration_status(connection)
    finally:
        pool.close()


def _check_database(values: Mapping[str, str], problems: list[str], status_fn: DatabaseCheck) -> None:
    url = values.get("CROWLEY_DATABASE_URL")
    if not url:
        problems.append("CROWLEY_DATABASE_URL is missing from secrets.env")
        return
    try:
        status = status_fn(url)
    except (OSError, psycopg.Error, RuntimeError, ValueError) as exc:
        problems.append(f"database is unreachable: {type(exc).__name__}")
        return
    if not status.is_current:
        details = [
            *status.pending,
            *status.unknown_applied,
            *status.checksum_mismatch,
            *status.checksum_missing,
        ]
        problems.append(f"database migrations are not current: {', '.join(details)}")


def _check_account(resolved: CompanionResolvedConfig | None, problems: list[str]) -> None:
    if resolved is None:
        return
    try:
        saved_variables = resolved.saved_variables
        account_dir = saved_variables.parent.parent
        if not account_dir.is_dir() or account_dir.is_symlink():
            raise ValueError(f"selected WTF account does not exist: {account_dir}")
        resolved_account = account_dir.resolve(strict=True)
        account_root = resolved.flavor_dir / "WTF" / "Account"
        try:
            resolved_account.relative_to(account_root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError("configured WTF account directory escapes WTF/Account") from exc
        if not saved_variables.is_file() or saved_variables.is_symlink():
            raise ValueError(f"selected account SavedVariables/Crowley.lua does not exist: {saved_variables}")
        try:
            saved_variables.resolve(strict=True).relative_to(resolved_account)
        except ValueError as exc:
            raise ValueError("selected SavedVariables/Crowley.lua escapes the account") from exc
    except (OSError, UnicodeError, ValueError, privfs.PrivateFileError) as exc:
        problems.append(f"host account selection invalid: {exc}")


def doctor(
    environ: Mapping[str, str] | None = None,
    *,
    database_status: DatabaseCheck = _database_status,
) -> DoctorReport:
    """Run every read-only install/runtime check and aggregate all failures."""

    environment = os.environ if environ is None else environ
    problems: list[str] = []
    try:
        xdg = XdgDirs.from_environ(environment)
        _home(environment)
    except ValueError as exc:
        return DoctorReport((f"XDG paths invalid: {exc}",))
    target = packaged_game_target()
    host = _check_host(xdg, problems)
    _check_tunables(xdg, problems)
    _check_pinned_binaries(xdg, environment, problems)
    companion: CompanionResolvedConfig | None = None
    companion_path = xdg.crowley_config / "companion.toml"
    companion_tunables: CompanionTunables | None = None
    try:
        companion_tunables = load_companion_tunables(companion_path)
    except (OSError, TunablesError, ValueError) as exc:
        problems.append(f"companion.toml is invalid: {exc}")
    if host is not None and companion_tunables is not None:
        try:
            companion = resolve_companion(target, host, companion_tunables, xdg=xdg)
        except ValueError as exc:
            problems.append(f"companion configuration is invalid: {exc}")
    _check_units(xdg, environment, problems)
    if host is not None:
        try:
            plan = install.build_plan(install.InstallRequest(), environment, host=host)
        except install.InstallError as exc:
            problems.append(f"addon target unavailable: {exc}")
        else:
            _check_addon(plan, target, problems)
            _check_launcher(plan, problems)
    values = _check_secrets(xdg, problems)
    _check_database(values, problems, database_status)
    _check_account(companion, problems)
    return DoctorReport(tuple(dict.fromkeys(problems)))


__all__ = ["DatabaseCheck", "DoctorReport", "doctor"]
