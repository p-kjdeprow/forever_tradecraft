"""Reproducible builders for Crowley's native helpers.

Rust builds accept exactly one toolchain source: ``CROWLEY_RUST_TOOLCHAIN``.
It must name a directory containing the pinned ``rustc``, ``cargo`` and
``rustfmt`` binaries.  System libraries for the observer are resolved through
``pkg-config --static x11``; no distribution-specific library path is part of
the source or build contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

from crowley.platform.paths import XdgDirs

from . import pins

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_PACKAGE_RESOURCES = files("crowley.native")
# Package-resource-relative identifier of the single canonical Rust workspace.
WORKSPACE_RESOURCE_ID: Final[str] = "crowley.native/resources/native/Cargo.toml"
NATIVE_ROOT: Final[Path] = Path(str(_PACKAGE_RESOURCES.joinpath("resources", "native"))).resolve()
# The build workspace is disposable and private.  The operating system picks
# its temporary parent; no host cache path is part of the build contract.
BUILD_CACHE_ROOT: Path | None = None
CRATE_SOURCE_FILES: Final[tuple[str, ...]] = ("Cargo.toml", "Cargo.lock", "src/main.rs")
TARGET: Final[str] = pins.TARGET_TRIPLE
PROTOCOL: Final[str] = pins.MATCHER_PROTOCOL
ARTIFACT_NAME: Final[str] = pins.MATCHER_ARTIFACT_NAME
MANIFEST_NAME: Final[str] = pins.MATCHER_MANIFEST_NAME
EXPECTED_RUSTC: Final[str] = pins.EXPECTED_RUSTC
EXPECTED_CARGO: Final[str] = pins.EXPECTED_CARGO
EXPECTED_RUSTFMT: Final[str] = pins.EXPECTED_RUSTFMT
VENDORED_SOURCE_ARCHIVE: Final[Path] = Path(
    str(_PACKAGE_RESOURCES.joinpath("resources", "vendor", "xdotool-source.tar.gz"))
).resolve()


class BuildError(RuntimeError):
    """A native build or publication failed a required invariant."""


@dataclass(frozen=True)
class Toolchain:
    """The one explicitly configured Rust toolchain."""

    directory: Path
    rustc: Path
    cargo: Path
    rustfmt: Path
    rustc_version: str
    cargo_version: str
    rustfmt_version: str
    rustc_verbose: str


@dataclass(frozen=True)
class _SourceSpec:
    package: str
    binary: str
    manifest_keys: tuple[str, ...]
    source_paths: tuple[str, ...]
    manifest_schema: str
    artifact_name: str
    manifest_name: str


_MATCHER_SOURCE = _SourceSpec(
    package="template_matcher",
    binary="template_matcher",
    manifest_keys=("Cargo.toml", "Cargo.lock", "src/main.rs"),
    source_paths=(
        "template_matcher/Cargo.toml",
        "template_matcher/Cargo.lock",
        "template_matcher/src/main.rs",
    ),
    manifest_schema="crowley.template-matcher-release",
    artifact_name=pins.MATCHER_ARTIFACT_NAME,
    manifest_name=pins.MATCHER_MANIFEST_NAME,
)
_OBSERVER_SOURCE = _SourceSpec(
    package="x11_identity_observer",
    binary="x11_identity_observer",
    manifest_keys=(
        "native/x11_identity_observer/Cargo.toml",
        "native/x11_identity_observer/Cargo.lock",
        "native/x11_identity_observer/src/main.rs",
    ),
    source_paths=(
        "x11_identity_observer/Cargo.toml",
        "x11_identity_observer/Cargo.lock",
        "x11_identity_observer/src/main.rs",
    ),
    manifest_schema=pins.OBSERVER_MANIFEST_SCHEMA,
    artifact_name=pins.OBSERVER_ARTIFACT_NAME,
    manifest_name="x11_identity_observer.manifest.json",
)
EXPECTED_OBSERVER_SOURCE_FILES: Final[frozenset[str]] = frozenset({"Cargo.toml", "Cargo.lock", "src/main.rs"})
EXPECTED_OBSERVER_SOURCE_DIRS: Final[frozenset[str]] = frozenset({"src"})


def _regular_file(path: Path, *, executable: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BuildError(f"required path is unavailable: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BuildError(f"path is not a regular file: {path}")
    if executable and not info.st_mode & 0o100:
        raise BuildError(f"path is not executable: {path}")


def _private_file(path: Path, *, executable: bool = False) -> None:
    _regular_file(path, executable=executable)
    if path.stat().st_mode & 0o077:
        raise BuildError(f"publication is not private: {path}")


def _private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BuildError(f"private build directory is unavailable: {path}") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise BuildError(f"private build directory is not user-owned and private: {path}")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_stable_file(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BuildError(f"source file is unavailable: {path}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BuildError(f"source path is not a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        data = b"".join(chunks)
        if identity_before != identity_after or len(data) != before.st_size:
            raise BuildError(f"source file changed during descriptor read: {path}")
        return data
    finally:
        os.close(descriptor)


def _read_source_files(
    source_root: Path, source_files: tuple[str, ...] = CRATE_SOURCE_FILES
) -> dict[str, bytes]:
    return {name: _read_stable_file(source_root / name) for name in source_files}


def _source_hashes(source_files: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha256_bytes(source_files[name]) for name in source_files}


def _combined_source_hash(source_hashes: dict[str, str], order: Iterable[str] | None = None) -> str:
    names = tuple(order) if order is not None else tuple(source_hashes)
    if set(names) != set(source_hashes):
        raise BuildError("source snapshot file set is not canonical")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_hashes[name].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_snapshot_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _prepare_source_snapshot(
    source_root: Path,
    workspace: Path,
    source_files: tuple[str, ...] = CRATE_SOURCE_FILES,
) -> tuple[Path, dict[str, bytes]]:
    before = _read_source_files(source_root, source_files)
    snapshot_root = workspace / "source"
    snapshot_root.mkdir(mode=0o700)
    for name, data in before.items():
        _write_snapshot_file(snapshot_root / name, data)
    after = _read_source_files(source_root, source_files)
    if before != after:
        raise BuildError("source changed while creating the private snapshot")
    snapshotted = _read_source_files(snapshot_root, source_files)
    if before != snapshotted:
        raise BuildError("private source snapshot failed exact verification")
    return snapshot_root, before


def _refuse_ancestor_build_inputs(path: Path) -> None:
    current = path.absolute()
    while True:
        cargo_toml = current / "Cargo.toml"
        if cargo_toml.exists() or cargo_toml.is_symlink():
            raise BuildError(f"refusing ancestor Cargo.toml: {cargo_toml}")
        for config_name in ("config", "config.toml"):
            cargo_config = current / ".cargo" / config_name
            if cargo_config.exists() or cargo_config.is_symlink():
                raise BuildError(f"refusing ancestor Cargo config: {cargo_config}")
        if current.parent == current:
            return
        current = current.parent


def _validate_source_tree(source: Path) -> None:
    """Validate the legacy observer snapshot shape used by focused tests."""

    if not source.is_dir() or source.is_symlink():
        raise BuildError("observer source root is not a directory")
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    for entry in source.rglob("*"):
        relative = entry.relative_to(source)
        if relative.parts and relative.parts[0] == "target":
            continue
        if entry.is_symlink():
            raise BuildError(f"observer source contains a symlink: {relative}")
        if entry.is_dir():
            seen_dirs.add(relative.as_posix())
        elif entry.is_file():
            seen_files.add(relative.as_posix())
        else:
            raise BuildError(f"observer source contains unsupported input: {relative}")
    if seen_files != EXPECTED_OBSERVER_SOURCE_FILES:
        raise BuildError(f"observer source files are not the reviewed three-file set: {sorted(seen_files)}")
    if seen_dirs != EXPECTED_OBSERVER_SOURCE_DIRS:
        raise BuildError(f"observer source directories are not the reviewed layout: {sorted(seen_dirs)}")


def _command_output(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", "") or str(error)
        raise BuildError(f"command failed: {' '.join(command)}\n{details.strip()}") from error
    return result.stdout.strip()


def _required_binary(directory: Path, name: str) -> Path:
    candidate = directory / name
    _regular_file(candidate, executable=True)
    return candidate


def _validate_toolchain_versions(rustc_version: str, cargo_version: str, rustfmt_version: str) -> None:
    expected = (EXPECTED_RUSTC, EXPECTED_CARGO, EXPECTED_RUSTFMT)
    actual = (rustc_version, cargo_version, rustfmt_version)
    if actual != expected:
        raise BuildError(
            "configured Rust toolchain does not match the pinned release: "
            f"rustc={rustc_version!r}, cargo={cargo_version!r}, rustfmt={rustfmt_version!r}"
        )


def required_toolchain() -> Toolchain:
    raw_directory = os.environ.get(pins.RUST_TOOLCHAIN_ENV)
    if not raw_directory:
        raise BuildError(f"{pins.RUST_TOOLCHAIN_ENV} is required for native Rust builds")
    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        raise BuildError(f"{pins.RUST_TOOLCHAIN_ENV} must name an absolute toolchain bin directory")
    if not directory.is_dir() or directory.is_symlink():
        raise BuildError(f"{pins.RUST_TOOLCHAIN_ENV} is not a toolchain bin directory: {directory}")
    rustc = _required_binary(directory, "rustc")
    cargo = _required_binary(directory, "cargo")
    rustfmt = _required_binary(directory, "rustfmt")
    environment = {"PATH": f"{directory}:/usr/bin:/bin", "LC_ALL": "C"}
    cwd = Path.cwd()
    rustc_version = _command_output([str(rustc), "-V"], cwd=cwd, env=environment)
    cargo_version = _command_output([str(cargo), "-V"], cwd=cwd, env=environment)
    rustfmt_version = _command_output([str(rustfmt), "--version"], cwd=cwd, env=environment)
    _validate_toolchain_versions(rustc_version, cargo_version, rustfmt_version)
    rustc_verbose = _command_output([str(rustc), "-vV"], cwd=cwd, env=environment)
    return Toolchain(
        directory, rustc, cargo, rustfmt, rustc_version, cargo_version, rustfmt_version, rustc_verbose
    )


def _build_environment(
    workspace: Path, toolchain: Toolchain, target_dir: Path, extra_flags: str = ""
) -> dict[str, str]:
    environment = {
        "PATH": f"{toolchain.directory}:/usr/bin:/bin",
        "HOME": str(workspace / "home"),
        "CARGO_HOME": str(workspace / "cargo-home"),
        "RUSTUP_HOME": str(workspace / "rustup-home"),
        "TMPDIR": str(workspace / "tmp"),
        "XDG_CONFIG_HOME": str(workspace / "xdg-config"),
        "XDG_CACHE_HOME": str(workspace / "xdg-cache"),
        "RUSTC": str(toolchain.rustc),
        "RUSTC_WRAPPER": "",
        "RUSTC_WORKSPACE_WRAPPER": "",
        "CARGO_BUILD_RUSTC_WRAPPER": "",
        "CARGO_INCREMENTAL": "0",
        "CARGO_PROFILE_RELEASE_DEBUG": "0",
        "CARGO_PROFILE_RELEASE_STRIP": "symbols",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "1",
        "CARGO_PROFILE_RELEASE_LTO": "false",
        "CARGO_PROFILE_RELEASE_PANIC": "unwind",
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TERM_COLOR": "never",
        "RUSTFLAGS": extra_flags,
        "SOURCE_DATE_EPOCH": "0",
        "TZ": "UTC",
        "LC_ALL": "C",
        "CARGO_TARGET_DIR": str(target_dir),
    }
    for name in ("home", "cargo-home", "rustup-home", "tmp", "xdg-config", "xdg-cache"):
        (workspace / name).mkdir(mode=0o700, exist_ok=True)
    target_dir.mkdir(mode=0o700)
    return environment


def _pkg_config_x11(environment: dict[str, str], cwd: Path) -> tuple[str, ...]:
    command = ["pkg-config", "--static", "--libs", "x11"]
    try:
        result = subprocess.run(command, cwd=cwd, env=environment, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", "") or str(error)
        raise BuildError(
            "pkg-config --static --libs x11 is required for the observer build\n" + details
        ) from error
    tokens = tuple(result.stdout.split())
    library_paths = tuple(token[2:] for token in tokens if token.startswith("-L") and len(token) > 2)
    if not library_paths:
        try:
            directory = subprocess.run(
                ["pkg-config", "--variable=libdir", "x11"],
                cwd=cwd,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise BuildError("pkg-config did not provide a static X11 library directory") from error
        if not directory or not Path(directory).is_absolute():
            raise BuildError("pkg-config did not provide an absolute X11 library directory")
        library_paths = (directory,)
    return library_paths


def _static_input_manifest(library_paths: tuple[str, ...]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for name in pins.OBSERVER_STATIC_LIBRARY_NAMES:
        candidates = tuple(Path(directory) / f"lib{name}.a" for directory in library_paths)
        library = next((candidate for candidate in candidates if candidate.is_file()), None)
        if library is None:
            raise BuildError(f"pkg-config did not resolve a static X11 library: lib{name}.a")
        _regular_file(library)
        inputs[f"lib{name}.a"] = _sha256_bytes(_read_stable_file(library))
    return inputs


def _linker_manifest(environment: dict[str, str], cwd: Path) -> dict[str, JSONValue]:
    driver_name = shutil.which("cc", path=environment["PATH"])
    if driver_name is None:
        raise BuildError("cc is required for the observer build")
    driver = Path(os.path.realpath(driver_name))
    _regular_file(driver, executable=True)
    try:
        selected_name = subprocess.run(
            [str(driver), "--print-prog-name=ld"],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("cc could not resolve its linker") from error
    selected_path = Path(selected_name)
    if not selected_path.is_absolute():
        resolved = shutil.which(selected_name, path=environment["PATH"])
        if resolved is None:
            raise BuildError(f"cc selected an unavailable linker: {selected_name!r}")
        selected_path = Path(resolved)
    linker = Path(os.path.realpath(selected_path))
    _regular_file(linker, executable=True)
    try:
        driver_version = subprocess.run(
            [str(driver), "--version"],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()[0]
        linker_version = subprocess.run(
            [str(linker), "--version"],
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()[0]
    except (IndexError, OSError, subprocess.CalledProcessError) as error:
        raise BuildError("compiler or linker version could not be recorded") from error
    return {
        "driver": {
            "name": "cc",
            "sha256": _sha256_bytes(_read_stable_file(driver)),
            "version": driver_version,
        },
        "ld": {
            "name": "ld",
            "sha256": _sha256_bytes(_read_stable_file(linker)),
            "version": linker_version,
        },
    }


def _copy_source_tree(source_root: Path, destination: Path, source_files: tuple[str, ...]) -> None:
    destination.mkdir(mode=0o700)
    for relative in source_files:
        _write_snapshot_file(destination / relative, _read_stable_file(source_root / relative))


def _build_crate(
    spec: _SourceSpec,
    source_root: Path,
    target_dir: Path,
    workspace: Path,
    toolchain: Toolchain,
    *,
    library_paths: tuple[str, ...] = (),
) -> Path:
    flags = [
        "-C debuginfo=0",
        "-C strip=symbols",
        "-C codegen-units=1",
        f"--remap-path-prefix={source_root}=/rustsrc",
    ]
    flags.extend(f"-L native={path}" for path in library_paths)
    environment = _build_environment(workspace, toolchain, target_dir, " ".join(flags))
    _refuse_ancestor_build_inputs(source_root.parent)
    command = [
        str(toolchain.cargo),
        "build",
        "--config",
        "build.rustflags=[]",
        "--config",
        f"target.{TARGET}.rustflags=[]",
        "--config",
        "net.offline=true",
        "--release",
        "--locked",
        "--offline",
        "--target",
        TARGET,
        "--manifest-path",
        str(source_root / "Cargo.toml"),
        "--package",
        spec.package,
    ]
    _command_output(command, cwd=workspace / "cwd", env=environment)
    artifact = target_dir / TARGET / "release" / spec.binary
    _regular_file(artifact, executable=True)
    return artifact


def _manifest_source_snapshot(
    spec: _SourceSpec, source_root: Path, workspace: Path
) -> tuple[Path, dict[str, bytes]]:
    del spec
    snapshot_root, raw = _prepare_source_snapshot(source_root, workspace, pins.NATIVE_SOURCE_FILES)
    return snapshot_root, raw


def _manifest_for_matcher(
    source_hashes: dict[str, str], artifact_sha256: str, toolchain: Toolchain, workspace_sha256: str
) -> dict[str, JSONValue]:
    manifest_hashes = {
        "Cargo.toml": source_hashes["template_matcher/Cargo.toml"],
        "Cargo.lock": source_hashes["template_matcher/Cargo.lock"],
        "src/main.rs": source_hashes["template_matcher/src/main.rs"],
    }
    return {
        "schema": "crowley.template-matcher-release",
        "schema_version": 1,
        "artifact": pins.MATCHER_ARTIFACT_NAME,
        "artifact_sha256": artifact_sha256,
        "source_hashes": manifest_hashes,
        "workspace_sha256": workspace_sha256,
        "source_sha256": _combined_source_hash(manifest_hashes),
        "source_snapshot_sha256": _combined_source_hash(manifest_hashes),
        "protocol": pins.MATCHER_PROTOCOL,
        "target": TARGET,
        "toolchain": {
            "rustc": toolchain.rustc_version,
            "rustc_verbose": toolchain.rustc_verbose,
            "cargo": toolchain.cargo_version,
            "rustfmt": toolchain.rustfmt_version,
        },
        "build": {
            "profile": "release",
            "locked": True,
            "offline": True,
            "incremental": False,
            "debuginfo": 0,
            "strip": "symbols",
            "codegen_units": 1,
            "target_cpu": None,
            "remap_path": "/rustsrc",
        },
    }


def _atomic_publish(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise BuildError(f"refusing to replace a symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_directory(path.parent)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.exists() and _sha256_bytes(_read_stable_file(path)) != _sha256_bytes(data):
            raise BuildError(f"refusing to replace a different published artifact: {path}")
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_matcher(output_dir: Path, artifact: bytes, manifest: dict[str, JSONValue]) -> tuple[Path, Path]:
    _private_directory(output_dir)
    artifact_path = output_dir / pins.MATCHER_ARTIFACT_NAME
    manifest_path = output_dir / pins.MATCHER_MANIFEST_NAME
    if artifact_path.exists() or manifest_path.exists():
        if not artifact_path.exists() or not manifest_path.exists():
            raise BuildError("partial matcher publication exists; refusing to overwrite it")
        _private_file(artifact_path, executable=True)
        _private_file(manifest_path)
        if (
            _read_stable_file(artifact_path) != artifact
            or json.loads(_read_stable_file(manifest_path)) != manifest
        ):
            raise BuildError("existing matcher publication differs from independent rebuild")
        return artifact_path, manifest_path
    _atomic_publish(artifact_path, artifact, 0o700)
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_publish(manifest_path, encoded, 0o600)
    return artifact_path, manifest_path


def default_matcher_artifact_root() -> Path:
    """Return the pinned matcher publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "template-matcher" / "v1"


def default_observer_artifact_path() -> Path:
    """Return the pinned observer publication path for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "x11-identity-observer" / "v1" / "observer"


def default_xdotool_artifact_root() -> Path:
    """Return the pinned xdotool publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "xdotool"


def build_matcher(output_dir: Path | None = None, *, source_root: Path = NATIVE_ROOT) -> dict[str, JSONValue]:
    if output_dir is None:
        output_dir = default_matcher_artifact_root()
    toolchain = required_toolchain()
    output_dir = output_dir.resolve()
    _ensure_output_dir(output_dir)
    workspace_parent = output_dir
    if BUILD_CACHE_ROOT is not None:
        _private_directory(BUILD_CACHE_ROOT)
        workspace_parent = BUILD_CACHE_ROOT
    with tempfile.TemporaryDirectory(prefix="crowley-matcher-build-", dir=workspace_parent) as name:
        workspace = Path(name)
        (workspace / "cwd").mkdir(mode=0o700)
        snapshot, raw = _manifest_source_snapshot(_MATCHER_SOURCE, source_root, workspace)
        source_hashes = _source_hashes(raw)
        workspace_hashes = _source_hashes(raw)
        workspace_sha256 = _combined_source_hash(workspace_hashes)
        first = _build_crate(_MATCHER_SOURCE, snapshot, workspace / "target-a", workspace, toolchain)
        second = _build_crate(_MATCHER_SOURCE, snapshot, workspace / "target-b", workspace, toolchain)
        first_bytes = _read_stable_file(first)
        second_bytes = _read_stable_file(second)
        if first_bytes != second_bytes:
            raise BuildError("independent matcher builds are not byte-identical")
        artifact_sha256 = _sha256_bytes(first_bytes)
        manifest = _manifest_for_matcher(source_hashes, artifact_sha256, toolchain, workspace_sha256)
        artifact_path, manifest_path = _publish_matcher(output_dir, first_bytes, manifest)
    return {
        "artifact": str(artifact_path),
        "manifest": str(manifest_path),
        "artifact_sha256": artifact_sha256,
        "source_sha256": manifest["source_sha256"],
        "toolchain": manifest["toolchain"],
        "target": TARGET,
        "independent_builds": 2,
        "byte_identical": True,
    }


def _observer_manifest(
    source_hashes: dict[str, str],
    artifact: Path,
    toolchain: Toolchain,
    workspace_sha256: str,
    library_paths: tuple[str, ...],
    static_inputs: dict[str, str],
    linker: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    del library_paths
    return {
        "schema": pins.OBSERVER_MANIFEST_SCHEMA,
        "target": TARGET,
        "binary": pins.OBSERVER_ARTIFACT_NAME,
        "binary_sha256": _sha256_bytes(_read_stable_file(artifact)),
        "source_sha256": {
            key: source_hashes[path]
            for key, path in zip(_OBSERVER_SOURCE.manifest_keys, _OBSERVER_SOURCE.source_paths, strict=True)
        },
        "workspace_sha256": workspace_sha256,
        "static_library_resolution": {
            "mechanism": "pkg-config",
            "package": "x11",
            "mode": "static",
            "libraries": [f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES],
        },
        "static_inputs": static_inputs,
        "linker": linker,
        "toolchain": {
            "rustc": toolchain.rustc_version,
            "cargo": toolchain.cargo_version,
            "rustfmt": toolchain.rustfmt_version,
        },
    }


def build_observer(output: Path | None = None, *, source_root: Path = NATIVE_ROOT) -> dict[str, JSONValue]:
    if output is None:
        output = default_observer_artifact_path()
    toolchain = required_toolchain()
    output = output.resolve()
    with tempfile.TemporaryDirectory(prefix="crowley-observer-build-") as name:
        workspace = Path(name)
        (workspace / "cwd").mkdir(mode=0o700)
        snapshot, raw = _manifest_source_snapshot(_OBSERVER_SOURCE, source_root, workspace)
        source_hashes = _source_hashes(raw)
        workspace_sha256 = _combined_source_hash(_source_hashes(raw))
        clean_env = _build_environment(workspace, toolchain, workspace / "probe")
        library_paths = _pkg_config_x11(clean_env, workspace / "cwd")
        static_inputs = _static_input_manifest(library_paths)
        linker = _linker_manifest(clean_env, workspace / "cwd")
        first = _build_crate(
            _OBSERVER_SOURCE,
            snapshot,
            workspace / "target-a",
            workspace,
            toolchain,
            library_paths=library_paths,
        )
        second = _build_crate(
            _OBSERVER_SOURCE,
            snapshot,
            workspace / "target-b",
            workspace,
            toolchain,
            library_paths=library_paths,
        )
        first_bytes = _read_stable_file(first)
        if first_bytes != _read_stable_file(second):
            raise BuildError("independent observer builds are not byte-identical")
        if _sha256_bytes(first_bytes) != pins.OBSERVER_ARTIFACT_SHA256:
            raise BuildError("observer artifact digest does not match the reviewed release")
        if static_inputs != _static_input_manifest(library_paths):
            raise BuildError("static X11 inputs changed during the build")
        if linker != _linker_manifest(clean_env, workspace / "cwd"):
            raise BuildError("compiler or linker inputs changed during the build")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output.parent, 0o700)
        _private_directory(output.parent)
        _atomic_publish(output, first_bytes, 0o700)
    manifest = _observer_manifest(
        source_hashes,
        output,
        toolchain,
        workspace_sha256,
        library_paths,
        static_inputs,
        linker,
    )
    manifest_path = output.with_suffix(".manifest.json")
    _atomic_publish(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    return manifest


def _ensure_output_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _private_directory(path)
    else:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
        _private_directory(path)


def _xdotool_sha256(path: Path) -> str:
    return _sha256_bytes(_read_stable_file(path))


def _verify_xdotool_publication(destination: Path) -> None:
    _private_directory(destination)
    expected_children = {
        pins.XDOTOOL_ARTIFACT_NAME,
        pins.XDOTOOL_ARCHIVE_NAME,
        pins.XDOTOOL_MANIFEST_NAME,
    }
    try:
        children = {entry.name for entry in destination.iterdir()}
    except OSError as error:
        raise BuildError(f"xdotool publication cannot be enumerated: {destination}") from error
    if children != expected_children:
        raise BuildError("xdotool publication is incomplete or contains unreviewed files")
    executable = destination / pins.XDOTOOL_ARTIFACT_NAME
    archive = destination / pins.XDOTOOL_ARCHIVE_NAME
    manifest_path = destination / pins.XDOTOOL_MANIFEST_NAME
    _private_file(executable, executable=True)
    _private_file(archive)
    _private_file(manifest_path)
    try:
        manifest = json.loads(_read_stable_file(manifest_path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BuildError("xdotool publication manifest is malformed") from error
    if not isinstance(manifest, dict):
        raise BuildError("xdotool publication manifest is not an object")
    if set(manifest) != pins.XDOTOOL_MANIFEST_KEYS:
        raise BuildError("xdotool publication manifest fields are not canonical")
    if (
        manifest["commit"] != pins.XDOTOOL_COMMIT
        or manifest["archive_sha256"] != pins.XDOTOOL_ARCHIVE_SHA256
        or manifest["executable"] != pins.XDOTOOL_ARTIFACT_NAME
        or manifest["executable_sha256"] != _xdotool_sha256(executable)
        or _xdotool_sha256(archive) != pins.XDOTOOL_ARCHIVE_SHA256
    ):
        raise BuildError("xdotool publication provenance is not pinned")
    build_inputs = manifest[pins.XDOTOOL_BUILD_INPUTS_KEY]
    if not isinstance(build_inputs, dict) or set(build_inputs) != {"compiler", "linker"}:
        raise BuildError("xdotool publication build inputs are malformed")
    for name in ("compiler", "linker"):
        record = build_inputs[name]
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "sha256", "version"}
            or not isinstance(record["name"], str)
            or not isinstance(record["sha256"], str)
            or not isinstance(record["version"], str)
            or not record["name"]
            or not record["version"]
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in record["sha256"])
        ):
            raise BuildError("xdotool publication build inputs are malformed")


def _assert_xdotool_static_linkage(binary: Path) -> None:
    readelf = shutil.which("readelf", path=os.defpath)
    if readelf is None:
        raise BuildError("readelf is required to verify static XTEST linkage")
    try:
        dynamic = subprocess.run(
            [readelf, "-d", str(binary)], check=True, capture_output=True, text=True
        ).stdout
        symbols = subprocess.run(
            [readelf, "-Ws", str(binary)], check=True, capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("static XTEST linkage verification failed") from error
    if "libxdo" in dynamic:
        raise BuildError("static XTEST linkage includes a NEEDED libxdo dependency")
    if "XTestFakeMotionEvent" not in symbols or "xdo_move_mouse" not in symbols:
        raise BuildError("static XTEST linkage does not contain the reviewed call path")
    objdump = shutil.which("objdump", path=os.defpath)
    if objdump is None:
        raise BuildError("objdump is required to verify static XTEST linkage")
    try:
        disassembly = subprocess.run(
            [objdump, "-d", "--disassemble=xdo_move_mouse", str(binary)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("static XTEST linkage call-path verification failed") from error
    if "XTestFakeMotionEvent" not in disassembly:
        raise BuildError("static XTEST linkage does not reference XTestFakeMotionEvent")


def _xdotool_build_inputs() -> dict[str, JSONValue]:
    compiler_name = shutil.which("cc", path=os.defpath)
    if compiler_name is None:
        raise BuildError("cc is required for the xdotool build")
    compiler = Path(os.path.realpath(compiler_name))
    _regular_file(compiler, executable=True)
    try:
        compiler_version = subprocess.run(
            [str(compiler), "--version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0]
        linker_name = subprocess.run(
            [str(compiler), "--print-prog-name=ld"], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (IndexError, OSError, subprocess.CalledProcessError) as error:
        raise BuildError("xdotool compiler or linker inputs could not be recorded") from error
    linker = Path(linker_name)
    if not linker.is_absolute():
        resolved = shutil.which(linker_name, path=os.defpath)
        if resolved is None:
            raise BuildError(f"cc selected an unavailable linker: {linker_name!r}")
        linker = Path(resolved)
    linker = Path(os.path.realpath(linker))
    _regular_file(linker, executable=True)
    try:
        linker_version = subprocess.run(
            [str(linker), "--version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0]
    except (IndexError, OSError, subprocess.CalledProcessError) as error:
        raise BuildError("xdotool linker version could not be recorded") from error
    return {
        "compiler": {
            "name": "cc",
            "sha256": _sha256_bytes(_read_stable_file(compiler)),
            "version": compiler_version,
        },
        "linker": {
            "name": "ld",
            "sha256": _sha256_bytes(_read_stable_file(linker)),
            "version": linker_version,
        },
    }


def _build_xdotool_once(archive: Path, work: Path) -> tuple[bytes, dict[str, JSONValue]]:
    work.mkdir(mode=0o700, parents=True)
    with tarfile.open(archive, "r:gz") as source:
        expected_root = f"xdotool-{pins.XDOTOOL_COMMIT}"
        roots = [member for member in source.getmembers() if member.name.rstrip("/") == expected_root]
        if len(roots) != 1 or not roots[0].isdir():
            raise BuildError("source archive root is not the pinned commit")
        source.extractall(work, filter="data")
    source_root = work / expected_root
    build_inputs = _xdotool_build_inputs()
    build_env = {
        "PATH": os.defpath,
        "SOURCE_DATE_EPOCH": str(roots[0].mtime),
        "CC": "cc",
        "LC_ALL": "C",
    }
    try:
        subprocess.run(["make", "clean"], cwd=source_root, env=build_env, check=True)
        subprocess.run(["make", "-B", "static"], cwd=source_root, env=build_env, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError("pinned xdotool build failed") from error
    built = source_root / "xdotool.static"
    _regular_file(built, executable=True)
    _assert_xdotool_static_linkage(built)
    return _read_stable_file(built), build_inputs


def build_xdotool(archive: Path = VENDORED_SOURCE_ARCHIVE, destination: Path | None = None) -> Path:
    """Build and publish the pinned XTEST xdotool source archive."""

    archive = archive.resolve()
    if _xdotool_sha256(archive) != pins.XDOTOOL_ARCHIVE_SHA256:
        raise BuildError("source archive digest does not match the pinned upstream archive")
    if destination is None:
        destination = default_xdotool_artifact_root()
    destination = destination.resolve()
    if destination.is_symlink():
        raise BuildError("private artifact destination cannot be a symlink")
    existing_executable: bytes | None = None
    if destination.exists():
        _verify_xdotool_publication(destination)
        _assert_xdotool_static_linkage(destination / pins.XDOTOOL_ARTIFACT_NAME)
        existing_executable = _read_stable_file(destination / pins.XDOTOOL_ARTIFACT_NAME)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_directory(destination.parent)
    with tempfile.TemporaryDirectory(prefix="crowley-xdotool-build-") as temporary_name:
        work = Path(temporary_name)
        first_bytes, first_inputs = _build_xdotool_once(archive, work / "first")
        second_bytes, second_inputs = _build_xdotool_once(archive, work / "second")
        if first_bytes != second_bytes:
            raise BuildError("independent xdotool builds are not byte-identical")
        if first_inputs != second_inputs:
            raise BuildError("xdotool compiler or linker inputs changed during the build")
        if existing_executable is not None:
            if existing_executable != first_bytes:
                raise BuildError("existing xdotool publication differs from independent rebuild")
            return destination
        staging = destination.parent / f".{destination.name}.staging-{os.getpid()}"
        if staging.exists():
            raise BuildError("unexpected existing xdotool publication staging path")
        staging.mkdir(mode=0o700)
        try:
            executable = staging / pins.XDOTOOL_ARTIFACT_NAME
            executable.write_bytes(first_bytes)
            executable.chmod(0o700)
            source_copy = staging / pins.XDOTOOL_ARCHIVE_NAME
            shutil.copyfile(archive, source_copy)
            source_copy.chmod(0o600)
            manifest = {
                "commit": pins.XDOTOOL_COMMIT,
                "archive_sha256": pins.XDOTOOL_ARCHIVE_SHA256,
                "executable": pins.XDOTOOL_ARTIFACT_NAME,
                "executable_sha256": _xdotool_sha256(executable),
                pins.XDOTOOL_BUILD_INPUTS_KEY: first_inputs,
            }
            manifest_path = staging / pins.XDOTOOL_MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            manifest_path.chmod(0o600)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    _verify_xdotool_publication(destination)
    _assert_xdotool_static_linkage(destination / pins.XDOTOOL_ARTIFACT_NAME)
    return destination


def verify(source_root: Path = NATIVE_ROOT) -> dict[str, JSONValue]:
    """Verify the required toolchain and every packaged native source pin."""

    toolchain = required_toolchain()
    if not source_root.is_dir():
        raise BuildError(f"native source root is unavailable: {source_root}")
    source_hashes = {}
    for relative in pins.NATIVE_SOURCE_FILES:
        path = source_root / relative
        _regular_file(path)
        source_hashes[relative] = _sha256_bytes(_read_stable_file(path))
    for relative, expected in pins.NATIVE_WORKSPACE_SOURCE_HASHES.items():
        actual = source_hashes[relative]
        if actual != expected:
            raise BuildError(f"native workspace source pin is stale: {relative}")
    for relative, expected in pins.OBSERVER_SOURCE_HASHES.items():
        actual = source_hashes[relative.removeprefix("native/")]
        if actual != expected:
            raise BuildError(f"observer source pin is stale: {relative}")
    matcher_hashes = {
        relative: source_hashes[f"template_matcher/{relative}"] for relative in pins.MATCHER_SOURCE_FILES
    }
    for relative, expected in pins.MATCHER_SOURCE_HASHES.items():
        actual = matcher_hashes[relative]
        if actual != expected:
            raise BuildError(f"matcher source pin is stale: {relative}")
    workspace_sha256 = _combined_source_hash(source_hashes, pins.NATIVE_SOURCE_FILES)
    if workspace_sha256 != pins.MATCHER_WORKSPACE_SHA256:
        raise BuildError("matcher workspace aggregate pin is stale")
    if workspace_sha256 != pins.OBSERVER_WORKSPACE_SHA256:
        raise BuildError("observer workspace aggregate pin is stale")
    if _combined_source_hash(matcher_hashes, pins.MATCHER_SOURCE_FILES) != pins.MATCHER_SOURCE_SHA256:
        raise BuildError("matcher source aggregate pin is stale")
    if _xdotool_sha256(VENDORED_SOURCE_ARCHIVE) != pins.XDOTOOL_ARCHIVE_SHA256:
        raise BuildError("vendored xdotool archive pin is stale")
    return {
        "toolchain": {
            "directory": str(toolchain.directory),
            "rustc": toolchain.rustc_version,
            "cargo": toolchain.cargo_version,
            "rustfmt": toolchain.rustfmt_version,
        },
        "target": TARGET,
        "source_root": str(source_root),
        "workspace": WORKSPACE_RESOURCE_ID,
        "static_libraries": "pkg-config --static --libs x11",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crowley-native", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("helper", choices=("matcher", "observer", "xdotool"))
    build_parser.add_argument("--output-dir", type=Path)
    build_parser.add_argument("--output", type=Path)
    build_parser.add_argument("--archive", type=Path, default=VENDORED_SOURCE_ARCHIVE)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--source-root", type=Path, default=NATIVE_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(args.source_root.resolve())
        elif args.helper == "matcher":
            result = build_matcher(args.output_dir)
        elif args.helper == "observer":
            result = build_observer(args.output)
        else:
            result = {"artifact": str(build_xdotool(args.archive, args.output_dir))}
    except BuildError as error:
        print(f"crowley-native: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _main() -> int:
    return main()


__all__ = [
    "ARTIFACT_NAME",
    "BUILD_CACHE_ROOT",
    "BuildError",
    "EXPECTED_CARGO",
    "EXPECTED_RUSTC",
    "EXPECTED_RUSTFMT",
    "CRATE_SOURCE_FILES",
    "MANIFEST_NAME",
    "PROTOCOL",
    "TARGET",
    "Toolchain",
    "VENDORED_SOURCE_ARCHIVE",
    "build_matcher",
    "build_observer",
    "build_xdotool",
    "default_matcher_artifact_root",
    "default_observer_artifact_path",
    "default_xdotool_artifact_root",
    "main",
    "required_toolchain",
    "verify",
]


if __name__ == "__main__":
    raise SystemExit(main())
