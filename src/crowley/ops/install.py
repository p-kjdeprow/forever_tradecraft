"""Install Crowley runtime units, configuration, launcher, and game assets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from crowley.config import (
    HostConfig,
    HostConfigError,
    OpsResolvedConfig,
    OpsTunables,
    host_config_path,
    load_host_config,
    resolve_ops,
)
from crowley.core.game_target import GameTarget, packaged_game_target
from crowley.platform import privfs
from crowley.platform.paths import XdgDirs

PACKAGE_DATA = "package_data"
MANIFEST_SCHEMA = "crowley-install-v2"
MAX_RESOURCE_BYTES = 32 * 1024 * 1024
_MUTABLE_ADDON_FILES = frozenset({"addon/Crowley_Data/Data.lua", "addon/Crowley_Data/Queue.lua"})
_CONFIG_FILES = ("companion.toml", "session.toml")
_LAUNCHER_NAME = "crowley-world-of-warcraft"
_DOCKER_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


class InstallError(RuntimeError):
    """Raised when an install cannot be completed without guessing."""


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Inputs for one deterministic install operation."""

    game_root: Path | None = None
    dev_addon_from: Path | None = None
    dry_run: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One regular file or explicitly requested development symlink."""

    destination: Path
    relative_name: str
    content: bytes | None = None
    symlink_target: Path | None = None
    root_key: str = ""
    mutable: bool = False
    mode: int = 0o644

    def __post_init__(self) -> None:
        if (self.content is None) == (self.symlink_target is None):
            raise InstallError("planned file must have exactly one content kind")
        if not self.root_key:
            raise InstallError(f"planned file has no destination root: {self.relative_name}")
        if self.mutable and self.symlink_target is not None:
            raise InstallError("development symlinks cannot be mutable")

    @property
    def digest(self) -> str:
        if self.mutable:
            raise InstallError(f"runtime-owned file has no immutable digest: {self.relative_name}")
        if self.content is not None:
            return hashlib.sha256(self.content).hexdigest()
        if self.symlink_target is None:
            raise InstallError(f"planned file has no content: {self.relative_name}")
        return hashlib.sha256(os.fsencode(str(self.symlink_target))).hexdigest()


@dataclass(frozen=True, slots=True)
class DestinationSet:
    """A destination committed as one ownership unit."""

    root_key: str
    destination: Path
    files: tuple[PlannedFile, ...]
    kind: str
    symlink_target: Path | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"file-set", "tree", "symlink"}:
            raise InstallError(f"unsupported destination set kind: {self.kind}")
        if self.kind == "symlink" and self.symlink_target is None:
            raise InstallError(f"symlink destination set has no target: {self.destination}")
        if self.kind != "symlink" and self.symlink_target is not None:
            raise InstallError(f"non-symlink destination set has a target: {self.destination}")


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Fully resolved destinations and bytes, before any mutation."""

    files: tuple[PlannedFile, ...]
    destination_sets: tuple[DestinationSet, ...]
    unit_dir: Path
    flavor_dir: Path
    launcher_path: Path
    manifest_path: Path
    roots: Mapping[str, str]
    target: GameTarget
    addon_mode: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Summary of a completed or dry-run install."""

    changed: tuple[Path, ...]
    unchanged: tuple[Path, ...]
    dry_run: bool


CommandRunner = Callable[[Sequence[str], Path], None]


@dataclass(frozen=True, slots=True)
class _StagedSet:
    destination_set: DestinationSet
    stage_path: Path


@dataclass(frozen=True, slots=True)
class _RollbackRecord:
    destination: Path
    backup: Path | None
    backup_dir: Path


def _resource_root() -> Traversable:
    return resources.files("crowley.ops").joinpath(PACKAGE_DATA)


def compose_file_path() -> Path:
    """Return the filesystem path of the package-owned Compose file."""

    resource = resources.files("crowley.ops.resources").joinpath("docker-compose.yml")
    if not isinstance(resource, Path):
        raise InstallError("packaged Compose file is not available as a filesystem path")
    try:
        path = resource.resolve(strict=True)
    except OSError as exc:
        raise InstallError("packaged Compose file is unavailable") from exc
    if not path.is_file():
        raise InstallError(f"packaged Compose resource is not a regular file: {path}")
    return path


def _package_version() -> str:
    try:
        value = importlib.metadata.version("crowley")
    except importlib.metadata.PackageNotFoundError as exc:
        raise InstallError("Crowley package metadata is unavailable") from exc
    if not _DOCKER_TAG_RE.fullmatch(value):
        raise InstallError(f"Crowley package version is not a valid Docker tag: {value!r}")
    return value


def _run_command(command: Sequence[str], cwd: Path) -> None:
    try:
        subprocess.run(list(command), cwd=cwd, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"image build command failed: {command[0]}") from exc


def _image_file(path: Path, label: str) -> Path:
    candidate = path.expanduser()
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise InstallError(f"{label} does not exist: {candidate}") from exc
    except OSError as exc:
        raise InstallError(f"cannot inspect {label}: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InstallError(f"{label} is not a regular file: {candidate}")
    return candidate.resolve(strict=True)


def _image_source(path: Path) -> Path:
    candidate = path.expanduser()
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise InstallError(f"image source checkout does not exist: {candidate}") from exc
    except OSError as exc:
        raise InstallError(f"cannot inspect image source checkout: {candidate}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"image source checkout is not a real directory: {candidate}")
    return candidate.resolve(strict=True)


def _built_wheel(output_dir: Path) -> Path:
    try:
        wheels = tuple(
            path for path in sorted(output_dir.iterdir(), key=lambda item: item.name) if path.suffix == ".whl"
        )
    except OSError as exc:
        raise InstallError(f"cannot inspect uv build output: {output_dir}") from exc
    if len(wheels) != 1 or not wheels[0].is_file() or wheels[0].is_symlink():
        raise InstallError(f"uv build did not produce exactly one regular wheel in {output_dir}")
    return wheels[0]


def build_image(
    *,
    wheel: Path | None = None,
    source: Path | None = None,
    runner: CommandRunner | None = None,
) -> str:
    """Build the versioned server image from an explicit wheel or checkout."""

    if (wheel is None) == (source is None):
        raise InstallError("image build requires exactly one of --wheel or --source")
    command_runner = _run_command if runner is None else runner
    tag = f"crowley-server:{_package_version()}"
    with tempfile.TemporaryDirectory(prefix="crowley-image-") as temporary:
        temporary_root = Path(temporary)
        context = temporary_root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_bytes(_resource_bytes("docker/Dockerfile"))
        if wheel is not None:
            wheel_path = _image_file(wheel, "wheel")
        else:
            if source is None:
                raise InstallError("image build source is missing")
            source_path = _image_source(source)
            output_dir = temporary_root / "wheel"
            output_dir.mkdir()
            command_runner(
                ("uv", "build", "--wheel", "--out-dir", str(output_dir)),
                source_path,
            )
            wheel_path = _built_wheel(output_dir)
        shutil.copy2(wheel_path, context / wheel_path.name)
        command_runner(
            ("docker", "build", "--tag", tag, "--file", str(dockerfile), str(context)),
            context,
        )
    return tag


def _walk_files(root: Traversable, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        name = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            yield from _walk_files(child, name)
        else:
            data = child.read_bytes()
            if len(data) > MAX_RESOURCE_BYTES:
                raise InstallError(f"packaged resource is too large: {name}")
            yield name, data


def _resource_bytes(name: str) -> bytes:
    resource = _resource_root().joinpath(name)
    try:
        data = resource.read_bytes()
    except OSError as exc:
        raise InstallError(f"packaged resource is unavailable: {name}") from exc
    if len(data) > MAX_RESOURCE_BYTES:
        raise InstallError(f"packaged resource is too large: {name}")
    return data


def packaged_addon_files() -> tuple[tuple[str, bytes], ...]:
    """Return every packaged addon file relative to its addon root."""

    files = tuple(_walk_files(_resource_root().joinpath("addon")))
    if not files:
        raise InstallError("packaged addon resources are empty")
    return files


def render_units() -> tuple[tuple[str, bytes], ...]:
    """Render unit templates to their installed filenames.

    Unit templates contain only systemd specifiers.  The installer therefore
    performs no host-specific substitution; ``%h``, ``%E``, ``%S`` and ``%t``
    are resolved by the user's systemd instance.
    """

    rendered: list[tuple[str, bytes]] = []
    for name, data in _walk_files(_resource_root().joinpath("systemd")):
        if name.endswith(".service.in"):
            installed_name = name.removesuffix(".in")
        elif name.endswith(".timer"):
            installed_name = name
        else:
            raise InstallError(f"unsupported systemd resource: {name}")
        rendered.append((installed_name, data))
    if not rendered:
        raise InstallError("packaged systemd resources are empty")
    return tuple(rendered)


def _resource_template(name: str, replacements: Mapping[bytes, bytes]) -> bytes:
    rendered = _resource_bytes(name)
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    if b"{{" in rendered or b"}}" in rendered:
        raise InstallError(f"packaged template contains an unresolved placeholder: {name}")
    return rendered


def render_launcher(target: GameTarget, game_root: Path, wine_prefix: Path, display: str) -> bytes:
    """Render the reviewed launcher from target, host display, and game paths."""

    resolved_game_root = _absolute_path(game_root, "launcher game root")
    expected_argv0 = rf"C:\Program Files (x86)\World of Warcraft\{target.product_dir}\{target.executable}"
    resolved_wine_prefix = _absolute_path(wine_prefix, "launcher Wine prefix")
    replacements = {
        b"{{GAME_ROOT}}": shlex.quote(str(resolved_game_root)).encode("utf-8"),
        b"{{PRODUCT_DIR}}": shlex.quote(target.product_dir).encode("utf-8"),
        b"{{EXECUTABLE}}": shlex.quote(target.executable).encode("utf-8"),
        b"{{PRODUCT}}": shlex.quote(target.product).encode("utf-8"),
        b"{{WINDOW_CLASS}}": shlex.quote(target.window_class).encode("utf-8"),
        b"{{WINE_PREFIX}}": shlex.quote(str(resolved_wine_prefix)).encode("utf-8"),
        b"{{EXPECTED_ARGV0}}": shlex.quote(expected_argv0).encode("utf-8"),
        b"{{DISPLAY}}": shlex.quote(display).encode("utf-8"),
    }
    return _resource_template("launcher/crowley-world-of-warcraft.sh.in", replacements)


def _reject_symlink_ancestors(value: Path, label: str) -> None:
    current = Path(value.anchor)
    for part in value.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise InstallError(f"cannot inspect {label}: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InstallError(f"{label} contains a symlinked path component: {current}")


def _absolute_path(value: Path, label: str) -> Path:
    if not value.is_absolute() or value == Path("/") or ".." in value.parts:
        raise InstallError(f"{label} must be an absolute non-root path without traversal")
    _reject_symlink_ancestors(value, label)
    return value


def _host_for_install(xdg: XdgDirs) -> HostConfig:
    config_path = host_config_path(
        {
            "XDG_CONFIG_HOME": str(xdg.config_home),
            "HOME": str(xdg.config_home.parent),
        }
    )
    try:
        return load_host_config(config_path)
    except HostConfigError as exc:
        raise InstallError(
            f"host.toml is required at {config_path}; copy deploy/templates/host.example.toml there: {exc}"
        ) from exc


def resolve_game_root(requested: Path | None, host: HostConfig) -> Path:
    """Resolve the host-owned absolute game root."""

    configured = _absolute_path(host.game_root_path, "host.game_root")
    if requested is not None:
        explicit = _absolute_path(requested.expanduser(), "--game-root")
        if explicit != configured:
            raise InstallError("--game-root must match host.toml game_root")
    return configured


def _safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise InstallError(f"resource path escapes its package: {path}")
    return candidate


def _canonical(path: Path, label: str) -> str:
    _reject_symlink_ancestors(path, label)
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise InstallError(f"cannot canonicalize {label}: {path}") from exc
    if not resolved.is_absolute() or resolved == Path("/"):
        raise InstallError(f"{label} must resolve to an absolute non-root path: {path}")
    return str(resolved)


def _manifest_bytes(plan: InstallPlan) -> bytes:
    entries: dict[str, dict[str, object]] = {}
    for item in plan.files:
        entry: dict[str, object] = {
            "kind": "file" if item.content is not None else "symlink",
            "root": item.root_key,
        }
        if item.mutable:
            entry["mutable"] = True
        else:
            entry["sha256"] = item.digest
        if item.content is not None and not item.mutable:
            entry["size"] = len(item.content)
        elif item.symlink_target is not None:
            entry["target"] = str(item.symlink_target)
        entries[item.relative_name] = entry
    payload = {
        "schema": MANIFEST_SCHEMA,
        "target_id": plan.target.target_id,
        "product_dir": plan.target.product_dir,
        "interface": plan.target.interface,
        "addon_mode": plan.addon_mode,
        "roots": dict(sorted(plan.roots.items())),
        "files": entries,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_manifest(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        value = json.loads(privfs.read_bounded(path, 8 * 1024 * 1024).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, privfs.PrivateFileError) as exc:
        raise InstallError(f"install manifest is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise InstallError(f"install manifest has unsupported schema: {path}")
    roots = value.get("roots")
    files = value.get("files")
    if not isinstance(roots, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in roots.items()
    ):
        raise InstallError("install manifest roots must be a string object")
    if not isinstance(files, dict):
        raise InstallError("install manifest files must be an object")
    return value


def _manifest_entries(manifest: Mapping[str, object]) -> Mapping[str, object]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {}
    return files


def _manifest_roots_match(manifest: Mapping[str, object], plan: InstallPlan) -> bool:
    roots = manifest.get("roots")
    return isinstance(roots, dict) and dict(roots) == dict(plan.roots)


def _destination_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _lstat_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InstallError(f"cannot inspect install destination: {path}") from exc
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _ensure_directory(path: Path, label: str) -> None:
    _reject_symlink_ancestors(path, label)
    if _destination_exists(path):
        try:
            info = path.lstat()
        except OSError as exc:
            raise InstallError(f"cannot inspect {label}: {path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise InstallError(f"{label} is not a real directory: {path}")
        return
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        raise InstallError(f"cannot create {label}: {path}") from exc


def _require_directory(path: Path, label: str) -> None:
    _reject_symlink_ancestors(path, label)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise InstallError(f"{label} is not a directory: {path}") from exc
    except OSError as exc:
        raise InstallError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallError(f"{label} is not a real directory: {path}")


def _tree_paths(root: Path) -> Iterator[tuple[str, Path]]:
    if not _destination_exists(root) or root.is_symlink():
        return
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        relative = child.name
        yield relative, child
        if child.is_dir() and not child.is_symlink():
            for nested, path in _tree_paths(child):
                yield f"{relative}/{nested}", path


def _legacy_data_tree_is_owned(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    expected = {"Crowley_Data.toc", "Data.lua", "Queue.lua"}
    entries = tuple(_tree_paths(path))
    if {relative for relative, _ in entries} != expected:
        return False
    return all(candidate.is_file() and not candidate.is_symlink() for _, candidate in entries)


def _managed_names(manifest: Mapping[str, object], root_key: str) -> set[str]:
    names: set[str] = set()
    for name, value in _manifest_entries(manifest).items():
        if not isinstance(name, str) or not isinstance(value, dict) or value.get("root") != root_key:
            continue
        names.add(name)
    return names


def _check_tree_foreign(destination_set: DestinationSet, previous: Mapping[str, object], force: bool) -> None:
    destination = destination_set.destination
    if not _destination_exists(destination) or destination.is_symlink():
        return
    if not destination.is_dir():
        raise InstallError(f"refusing to overwrite foreign file: {destination}")
    managed = _managed_names(previous, destination_set.root_key)
    prefixes = {
        "addon_crowley": "addon/Crowley/",
        "addon_data": "addon/Crowley_Data/",
    }
    prefix = prefixes.get(destination_set.root_key, "")
    allowed = {name.removeprefix(prefix) for name in managed if name.startswith(prefix)}
    mutable = {name.removeprefix(prefix) for name in _MUTABLE_ADDON_FILES if name.startswith(prefix)}
    for relative, path in _tree_paths(destination):
        if relative in mutable and path.is_dir() and not path.is_symlink():
            if not force:
                raise InstallError(f"refusing to overwrite foreign file: {destination / relative}")
            continue
        if relative in allowed and path.is_dir() and not path.is_symlink():
            if not force:
                raise InstallError(f"refusing to overwrite foreign file: {destination / relative}")
            continue
        if path.is_dir() and not path.is_symlink():
            if any(name.startswith(f"{relative}/") for name in allowed | mutable):
                continue
            if not force:
                raise InstallError(f"refusing to overwrite foreign file: {destination / relative}")
            continue
        if relative not in allowed and relative not in mutable and not force:
            raise InstallError(f"refusing to overwrite foreign file: {destination / relative}")


def _check_foreign(plan: InstallPlan, previous: Mapping[str, object], force: bool) -> None:
    roots_match = _manifest_roots_match(previous, plan)
    for destination_set in plan.destination_sets:
        destination = destination_set.destination
        if destination_set.kind in {"tree", "symlink"}:
            if not _destination_exists(destination):
                continue
            if destination.is_symlink():
                if destination_set.root_key in {"addon_crowley", "addon_data"} or force:
                    continue
                raise InstallError(f"refusing to overwrite foreign file: {destination}")
            if roots_match and _managed_names(previous, destination_set.root_key):
                _check_tree_foreign(destination_set, previous, force)
                continue
            if (
                destination_set.kind == "tree"
                and destination_set.root_key == "addon_data"
                and _legacy_data_tree_is_owned(destination)
            ):
                continue
            if not force:
                raise InstallError(f"refusing to overwrite foreign file: {destination}")
            continue

        for item in destination_set.files:
            if not _destination_exists(item.destination):
                continue
            if item.mutable:
                if item.destination.is_dir() and not item.destination.is_symlink() and not force:
                    raise InstallError(f"refusing to overwrite foreign file: {item.destination}")
                continue
            if item.destination.is_dir() and not item.destination.is_symlink():
                if not force:
                    raise InstallError(f"refusing to overwrite foreign file: {item.destination}")
                continue
            if roots_match and item.relative_name in _manifest_entries(previous):
                continue
            if not force:
                raise InstallError(f"refusing to overwrite foreign file: {item.destination}")


def _render_config(name: str) -> bytes:
    """Return the tracked tunables template without rewriting its contents."""

    return _resource_bytes(f"config/{name}")


def _plan_file(
    destination: Path,
    relative_name: str,
    content: bytes,
    root_key: str,
    *,
    mutable: bool = False,
    mode: int = 0o644,
) -> PlannedFile:
    return PlannedFile(
        destination=destination,
        relative_name=relative_name,
        content=content,
        root_key=root_key,
        mutable=mutable,
        mode=mode,
    )


def _build_plan(
    request: InstallRequest,
    xdg: XdgDirs,
    target: GameTarget,
    home: Path,
    *,
    host: HostConfig,
    resolved: OpsResolvedConfig,
) -> InstallPlan:
    game_root = resolve_game_root(request.game_root, host)
    _require_directory(game_root, "game root")
    flavor_dir = resolved.flavor_dir
    _require_directory(flavor_dir, "target product directory")
    addons_dir = resolved.addons_dir
    _require_directory(addons_dir, "addon directory")

    unit_dir = xdg.config_home / "systemd" / "user"
    config_dir = xdg.crowley_config
    launcher_dir = home / ".local" / "bin"
    manifest_path = xdg.crowley_state / "install" / "manifest.json"
    roots = {
        "systemd_user": _canonical(unit_dir, "systemd user directory"),
        "config": _canonical(config_dir, "Crowley config directory"),
        "game_addons": _canonical(addons_dir, "game addon directory"),
        "launcher": _canonical(launcher_dir, "launcher directory"),
        "manifest": _canonical(manifest_path, "install manifest"),
    }

    unit_files = tuple(
        _plan_file(unit_dir / name, f"systemd/user/{name}", data, "systemd_user")
        for name, data in render_units()
    )
    config_files: list[PlannedFile] = [
        _plan_file(
            config_dir / name,
            f"config/{name}",
            _render_config(name),
            "config",
            mutable=True,
            mode=0o600,
        )
        for name in _CONFIG_FILES
    ]
    config_files.append(
        _plan_file(
            config_dir / "secrets.env",
            "config/secrets.env",
            _resource_bytes("secrets.env.example"),
            "config",
            mutable=True,
            mode=0o600,
        )
    )

    addon_files = tuple(packaged_addon_files())
    crowley_files: list[PlannedFile] = []
    data_files: list[PlannedFile] = []
    for name, data in addon_files:
        relative = _safe_relative(name)
        destination = addons_dir / relative
        root_key = "addon_crowley" if relative.parts[0] == "Crowley" else "addon_data"
        planned = _plan_file(
            destination,
            f"addon/{name}",
            data,
            root_key,
            mutable=f"addon/{name}" in _MUTABLE_ADDON_FILES,
        )
        if root_key == "addon_crowley":
            crowley_files.append(planned)
        else:
            data_files.append(planned)

    addon_mode = "packaged"
    if request.dev_addon_from is not None:
        checkout = _absolute_path(request.dev_addon_from.expanduser(), "--dev-addon-from")
        dev_addon = checkout / "addon" / "Crowley"
        if not dev_addon.is_dir() or dev_addon.is_symlink():
            raise InstallError(f"--dev-addon-from must contain a real addon/Crowley directory: {dev_addon}")
        addon_mode = "development"
        dev_file = PlannedFile(
            destination=addons_dir / "Crowley",
            relative_name="addon/Crowley",
            symlink_target=dev_addon,
            root_key="addon_crowley",
        )
        crowley_set = DestinationSet(
            "addon_crowley", addons_dir / "Crowley", (dev_file,), "symlink", dev_addon
        )
        crowley_files = [dev_file]
    else:
        crowley_set = DestinationSet(
            "addon_crowley",
            addons_dir / "Crowley",
            tuple(crowley_files),
            "tree",
        )

    data_set = DestinationSet("addon_data", addons_dir / "Crowley_Data", tuple(data_files), "tree")
    launcher = _plan_file(
        launcher_dir / _LAUNCHER_NAME,
        f"launcher/{_LAUNCHER_NAME}",
        render_launcher(target, game_root, host.wine_prefix_path, host.display),
        "launcher",
        mode=0o700,
    )
    destination_sets = (
        DestinationSet("systemd_user", unit_dir, unit_files, "file-set"),
        DestinationSet("config", config_dir, tuple(config_files), "file-set"),
        crowley_set,
        data_set,
        DestinationSet("launcher", launcher_dir, (launcher,), "file-set"),
    )
    all_files = tuple(item for destination_set in destination_sets for item in destination_set.files)
    return InstallPlan(
        files=all_files,
        destination_sets=destination_sets,
        unit_dir=unit_dir,
        flavor_dir=flavor_dir,
        launcher_path=launcher.destination,
        manifest_path=manifest_path,
        roots=roots,
        target=target,
        addon_mode=addon_mode,
    )


def build_plan(
    request: InstallRequest,
    environ: Mapping[str, str] | None = None,
    *,
    host: HostConfig | None = None,
) -> InstallPlan:
    """Resolve an install without writing anything."""

    environment = os.environ if environ is None else environ
    try:
        xdg = XdgDirs.from_environ(environment)
        home_value = environment.get("HOME")
        if not isinstance(home_value, str) or not home_value:
            raise ValueError("HOME must be set")
        home = _absolute_path(Path(home_value), "HOME")
    except ValueError as exc:
        raise InstallError(f"XDG paths are invalid: {exc}") from exc
    target = packaged_game_target()
    resolved_host = _host_for_install(xdg) if host is None else host
    resolved = resolve_ops(target, resolved_host, OpsTunables(), xdg=xdg)
    plan = _build_plan(request, xdg, target, home, host=resolved_host, resolved=resolved)
    _check_foreign(plan, load_manifest(plan.manifest_path), request.force)
    return plan


def build_plan_from_resolved(
    request: InstallRequest,
    environ: Mapping[str, str] | None,
    *,
    resolved: OpsResolvedConfig,
) -> InstallPlan:
    """Build an install plan from the caller's already-composed ops config."""

    environment = os.environ if environ is None else environ
    try:
        xdg = XdgDirs.from_environ(environment)
        home_value = environment.get("HOME")
        if not isinstance(home_value, str) or not home_value:
            raise ValueError("HOME must be set")
        home = _absolute_path(Path(home_value), "HOME")
    except ValueError as exc:
        raise InstallError(f"XDG paths are invalid: {exc}") from exc
    plan = _build_plan(
        request,
        xdg,
        resolved.target,
        home,
        host=resolved.host,
        resolved=resolved,
    )
    _check_foreign(plan, load_manifest(plan.manifest_path), request.force)
    return plan


def _existing_file_bytes(path: Path) -> bytes | None:
    if not _destination_exists(path):
        return None
    if not _lstat_regular(path):
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InstallError(f"cannot read existing install file: {path}") from exc


def _same_file(item: PlannedFile) -> bool:
    if item.mutable:
        return _lstat_regular(item.destination)
    if item.symlink_target is not None:
        return item.destination.is_symlink() and os.readlink(item.destination) == str(item.symlink_target)
    return _existing_file_bytes(item.destination) == item.content


def _same_tree(destination_set: DestinationSet) -> bool:
    destination = destination_set.destination
    if destination_set.kind == "symlink":
        return destination.is_symlink() and os.readlink(destination) == str(destination_set.symlink_target)
    if not destination.is_dir() or destination.is_symlink():
        return False
    for item in destination_set.files:
        parts = Path(item.relative_name).parts
        if len(parts) < 3:
            raise InstallError(f"tree file has no relative child path: {item.relative_name}")
        relative = Path(*parts[2:])
        actual = destination / relative
        compared = PlannedFile(
            actual,
            item.relative_name,
            item.content,
            item.symlink_target,
            item.root_key,
            item.mutable,
            item.mode,
        )
        if not _same_file(compared):
            return False
    return True


def _changed_paths(plan: InstallPlan) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    changed: list[Path] = []
    unchanged: list[Path] = []
    for destination_set in plan.destination_sets:
        if destination_set.kind in {"tree", "symlink"}:
            target_path = destination_set.destination
            (unchanged if _same_tree(destination_set) else changed).append(target_path)
            continue
        for item in destination_set.files:
            (unchanged if _same_file(item) else changed).append(item.destination)
    return tuple(changed), tuple(unchanged)


def _write_stage_file(
    path: Path,
    item: PlannedFile,
    *,
    preserve_existing: bool = True,
    existing_path: Path | None = None,
) -> None:
    if item.content is None:
        raise InstallError(f"file stage has no content: {item.relative_name}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        raise InstallError(f"staged file destination is a directory: {path}")
    source = item.destination if existing_path is None else existing_path
    existing = _existing_file_bytes(source) if item.mutable and preserve_existing else None
    data = existing if existing is not None else item.content
    path.write_bytes(data)
    os.chmod(path, item.mode)


def _stage_set(destination_set: DestinationSet) -> _StagedSet:
    destination = destination_set.destination
    if destination_set.kind == "file-set":
        _ensure_directory(destination, "install destination directory")
    _ensure_directory(destination.parent, "install destination parent")
    stage_dir = Path(tempfile.mkdtemp(prefix=".crowley-stage-", dir=destination.parent))
    try:
        if destination_set.kind == "tree":
            stage_path = stage_dir / destination.name
            stage_path.mkdir()
            for item in destination_set.files:
                relative = item.destination.relative_to(destination)
                target = stage_path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                existing_path = item.destination
                if item.mutable and destination.is_symlink():
                    existing_path = destination / relative
                _write_stage_file(target, item, existing_path=existing_path)
        elif destination_set.kind == "symlink":
            stage_path = stage_dir / destination.name
            if destination_set.symlink_target is None:
                raise InstallError(f"symlink stage has no target: {destination}")
            stage_path.symlink_to(destination_set.symlink_target, target_is_directory=True)
        else:
            stage_path = stage_dir / destination.name
            shutil.copytree(destination, stage_path, symlinks=True)
            for item in destination_set.files:
                _ensure_directory(item.destination.parent, "install file destination parent")
                relative = item.destination.relative_to(destination)
                target = stage_path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_stage_file(target, item)
        return _StagedSet(destination_set, stage_path)
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _remove_path(path: Path) -> None:
    if not _destination_exists(path):
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise InstallError(f"cannot remove install path: {path}")


def _commit_staged(
    staged: _StagedSet,
    records: list[_RollbackRecord],
) -> None:
    destination_set = staged.destination_set
    _publish_staged(destination_set.destination, staged.stage_path, records)


def _publish_staged(destination: Path, source: Path, records: list[_RollbackRecord]) -> None:
    backup_dir = Path(tempfile.mkdtemp(prefix=".crowley-rollback-", dir=destination.parent))
    backup = backup_dir / destination.name
    had_existing = _destination_exists(destination)
    if had_existing:
        try:
            os.replace(destination, backup)
        except OSError as exc:
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise InstallError(f"cannot retain rollback material for: {destination}") from exc
    record = _RollbackRecord(destination, backup if had_existing else None, backup_dir)
    records.append(record)
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise InstallError(f"cannot publish install destination: {destination}") from exc


def _restore(records: list[_RollbackRecord]) -> None:
    for record in reversed(records):
        destination = record.destination
        backup = record.backup
        try:
            _remove_path(destination)
            if backup is not None and _destination_exists(backup):
                os.replace(backup, destination)
        except (OSError, InstallError) as exc:
            raise InstallError(
                f"cannot restore install destination: {destination}; "
                f"rollback material retained at {record.backup_dir}"
            ) from exc


def _cleanup_rollback(records: list[_RollbackRecord]) -> None:
    for record in records:
        shutil.rmtree(record.backup_dir, ignore_errors=True)


def _stage_manifest(plan: InstallPlan) -> tuple[Path, Path]:
    parent = plan.manifest_path.parent
    privfs.ensure_private_dir(parent)
    stage_dir = Path(tempfile.mkdtemp(prefix=".crowley-manifest-", dir=parent))
    try:
        stage_path = stage_dir / plan.manifest_path.name
        stage_path.write_bytes(_manifest_bytes(plan))
        os.chmod(stage_path, 0o600)
        return stage_dir, stage_path
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def install(request: InstallRequest, environ: Mapping[str, str] | None = None) -> InstallResult:
    """Install all resources transactionally, or return its printable diff."""

    plan = build_plan(request, environ)
    changed, unchanged = _changed_paths(plan)
    if request.dry_run:
        return InstallResult(changed=changed, unchanged=unchanged, dry_run=True)

    staged_sets: list[_StagedSet] = []
    stage_manifest_dir: Path | None = None
    records: list[_RollbackRecord] = []
    rollback_preserved = False
    try:
        staged_sets = [_stage_set(destination_set) for destination_set in plan.destination_sets]
        stage_manifest_dir, stage_manifest = _stage_manifest(plan)
        for staged in staged_sets:
            _commit_staged(staged, records)

        _publish_staged(plan.manifest_path, stage_manifest, records)
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("systemd user daemon-reload failed; installation was rolled back") from exc
    except BaseException as exc:
        try:
            _restore(records)
        except InstallError as restore_error:
            rollback_preserved = True
            raise restore_error from exc
        if isinstance(exc, InstallError):
            raise
        raise InstallError("install failed; installation was rolled back") from exc
    finally:
        for staged in staged_sets:
            shutil.rmtree(staged.stage_path.parent, ignore_errors=True)
        if stage_manifest_dir is not None:
            shutil.rmtree(stage_manifest_dir, ignore_errors=True)
        if not rollback_preserved:
            _cleanup_rollback(records)
    return InstallResult(changed=changed, unchanged=unchanged, dry_run=False)


def diff_text(result: InstallResult) -> str:
    """Format the deterministic install diff."""

    lines = [f"{'DRY-RUN ' if result.dry_run else ''}install diff:"]
    lines.extend(f"~ {path}" for path in result.changed)
    lines.extend(f"= {path}" for path in result.unchanged)
    return "\n".join(lines)


__all__ = [
    "DestinationSet",
    "InstallError",
    "InstallPlan",
    "InstallRequest",
    "InstallResult",
    "MAX_RESOURCE_BYTES",
    "build_plan",
    "build_plan_from_resolved",
    "build_image",
    "compose_file_path",
    "diff_text",
    "install",
    "load_manifest",
    "packaged_addon_files",
    "render_launcher",
    "render_units",
    "resolve_game_root",
]
