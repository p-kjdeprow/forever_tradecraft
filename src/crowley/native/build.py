"""Reproducible builders for Crowley's native helpers.

Rust builds accept exactly one toolchain source: ``CROWLEY_RUST_TOOLCHAIN``.
It must name a directory containing the pinned ``rustc``, ``cargo`` and
``rustfmt`` binaries.  System libraries for the observer are resolved through
``pkg-config --static x11``; no distribution-specific library path is part of
the source or build contract.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, cast

from crowley.platform import privfs
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


@dataclass(frozen=True)
class _PublicationFile:
    name: str
    data: bytes
    mode: int


@dataclass(frozen=True)
class _Publication:
    kind: str
    root: Path
    binary_name: str
    files: tuple[_PublicationFile, ...]
    verify_staged: Callable[[Path], None]
    verify_existing: Callable[[Path], None]


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise BuildError(f"{path} cannot be opened for publication durability") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise BuildError(f"{path} cannot be synced after publication") from error
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _publication_lock(parent: Path) -> Iterator[None]:
    lock_path = parent / ".crowley-native-publication.lock"
    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise BuildError(f"native publication lock cannot be inspected: {lock_path}") from error
    if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise BuildError(f"native publication lock is not a regular file: {lock_path}")
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise BuildError(f"native publication lock failed: {lock_path}") from error
        yield
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _publication_digest(publication: _Publication, root: Path) -> str:
    for file in publication.files:
        if file.name == publication.binary_name:
            try:
                return _sha256_bytes(_read_stable_file(root / file.name))
            except BuildError as error:
                raise BuildError(f"{publication.kind} publication binary is unavailable") from error
    raise BuildError(f"{publication.kind} publication binary is not staged")


def _stage_publication(publication: _Publication, parent: Path) -> Path:
    staging: Path | None = None
    success = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{publication.kind}-publication-", dir=parent))
        privfs.ensure_private_dir(staging)
        if staging.stat().st_dev != parent.stat().st_dev:
            raise BuildError(f"{publication.kind} staging directory is not on the destination filesystem")
        names = tuple(file.name for file in publication.files)
        if len(set(names)) != len(names):
            raise BuildError(f"{publication.kind} publication contains duplicate file names")
        for file in publication.files:
            staged_path = staging / file.name
            privfs.atomic_write_private(staged_path, file.data)
            os.chmod(staged_path, file.mode)
            _private_file(staged_path, executable=bool(file.mode & 0o100))
        publication.verify_staged(staging)
        success = True
        return staging
    except privfs.PrivateFileError as error:
        raise BuildError(f"{publication.kind} publication staging failed: {error}") from error
    except OSError as error:
        raise BuildError(f"{publication.kind} publication staging failed") from error
    finally:
        if not success and staging is not None and staging.exists():
            shutil.rmtree(staging)


def _release_digest_from_target(target: str, kind: str) -> str:
    parts = Path(target).parts
    if len(parts) != 2 or parts[0] != "releases":
        raise BuildError(f"existing {kind} publication current pointer is not canonical")
    digest = parts[1]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise BuildError(f"existing {kind} publication current pointer digest is malformed")
    return digest


def _current_release(publication: _Publication) -> Path:
    root = publication.root
    try:
        target = os.readlink(root / "current")
    except OSError as error:
        raise BuildError(f"existing {publication.kind} publication current pointer is unavailable") from error
    digest = _release_digest_from_target(target, publication.kind)
    release = root / "releases" / digest
    if release.is_symlink() or not release.is_dir():
        raise BuildError(f"existing {publication.kind} publication release is unavailable")
    _private_directory(release)
    expected = {file.name for file in publication.files}
    try:
        children = {entry.name for entry in release.iterdir()}
    except OSError as error:
        raise BuildError(f"existing {publication.kind} publication release cannot be enumerated") from error
    if children != expected:
        raise BuildError(f"existing {publication.kind} publication release is incomplete")
    for file in publication.files:
        _private_file(release / file.name, executable=bool(file.mode & 0o100))
    try:
        publication.verify_existing(release)
    except BuildError as error:
        raise BuildError(f"existing {publication.kind} publication failed verification: {error}") from error
    actual_digest = _publication_digest(publication, release)
    if actual_digest != digest:
        raise BuildError(f"existing {publication.kind} publication release is mislabeled")
    return release


def _publication_is_present(publication: _Publication) -> bool:
    root = publication.root
    if not root.exists() and not root.is_symlink():
        return False
    if root.is_symlink():
        raise BuildError(f"existing {publication.kind} publication is a symlink: {root}")
    _private_directory(root)
    try:
        children = {entry.name for entry in root.iterdir()}
    except OSError as error:
        raise BuildError(f"existing {publication.kind} publication cannot be enumerated: {root}") from error
    if not children:
        return False
    if children != {"releases", "current"}:
        raise BuildError(f"existing {publication.kind} publication is incomplete; refusing to replace it")
    releases = root / "releases"
    if releases.is_symlink() or not releases.is_dir():
        raise BuildError(f"existing {publication.kind} publication release store is unsafe")
    _private_directory(releases)
    _current_release(publication)
    return True


def _publication_matches(left: Path, right: Path, names: tuple[str, ...]) -> bool:
    return all(_read_stable_file(left / name) == _read_stable_file(right / name) for name in names)


def _backup_name(root: Path) -> Path:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = root.parent / f"{root.name}.backup-{stamp}"
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = root.parent / f"{root.name}.backup-{stamp}-{suffix}"
        suffix += 1
    return candidate


def _prepare_publication_backup(publication: _Publication, release: Path) -> Path:
    root = publication.root
    parent = root.parent
    backup = _backup_name(root)
    temporary = parent / f".{backup.name}.{os.getpid()}"
    suffix = 1
    while temporary.exists() or temporary.is_symlink():
        temporary = parent / f".{backup.name}.{os.getpid()}-{suffix}"
        suffix += 1
    try:
        relative = os.path.relpath(release, parent)
        os.symlink(relative, temporary)
        os.replace(temporary, backup)
        _fsync_directory(parent)
        return backup
    except OSError as error:
        raise BuildError(f"{publication.kind} backup failed") from error
    finally:
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink(missing_ok=True)


def _install_versioned_release(publication: _Publication, staged: Path, releases: Path) -> tuple[Path, bool]:
    release = releases / _publication_digest(publication, staged)
    if release.exists() or release.is_symlink():
        if release.is_symlink() or not release.is_dir():
            raise BuildError(f"existing {publication.kind} versioned release is unsafe: {release}")
        expected = {file.name for file in publication.files}
        try:
            children = {entry.name for entry in release.iterdir()}
        except OSError as error:
            raise BuildError(f"existing {publication.kind} versioned release cannot be enumerated") from error
        if children != expected:
            raise BuildError(f"existing {publication.kind} versioned release is incomplete")
        for file in publication.files:
            _private_file(release / file.name, executable=bool(file.mode & 0o100))
        publication.verify_existing(release)
        if not _publication_matches(release, staged, tuple(file.name for file in publication.files)):
            raise BuildError(f"existing {publication.kind} versioned release differs from the staged build")
        return release, False
    try:
        os.replace(staged, release)
        _fsync_directory(releases)
    except OSError as error:
        raise BuildError(f"{publication.kind} versioned release installation failed") from error
    return release, True


def _switch_current_pointer(publication: _Publication, release: Path) -> None:
    root = publication.root
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        os.symlink(os.path.relpath(release, root), temporary)
        os.replace(temporary, root / "current")
        temporary = None
        _fsync_directory(root)
    except OSError as error:
        raise BuildError(f"{publication.kind} atomic current-pointer switch failed") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_publication(publication: _Publication, *, replace: bool) -> Path:
    root = publication.root
    parent = root.parent
    privfs.ensure_private_dir(parent)
    if root.exists() and not root.is_dir():
        raise BuildError(f"native publication destination is not a directory: {root}")
    if root.is_symlink():
        raise BuildError(f"refusing to replace a symlink: {root}")
    staged = _stage_publication(publication, parent)
    staging: Path | None = staged
    try:
        with _publication_lock(parent):
            present = _publication_is_present(publication)
            if present:
                current = _current_release(publication)
                if _publication_matches(current, staged, tuple(file.name for file in publication.files)):
                    return root
                if not replace:
                    raise BuildError(
                        f"existing {publication.kind} publication differs; pass --replace to replace it"
                    )
            if present:
                releases = root / "releases"
                release, consumed = _install_versioned_release(publication, staged, releases)
                if consumed:
                    staging = None
                _prepare_publication_backup(publication, current)
                _switch_current_pointer(publication, release)
            else:
                root_staging: Path | None = Path(
                    tempfile.mkdtemp(prefix=f".{root.name}-publication-", dir=parent)
                )
                try:
                    privfs.ensure_private_dir(root_staging)
                    releases = root_staging / "releases"
                    releases.mkdir(mode=0o700)
                    privfs.ensure_private_dir(releases)
                    release, consumed = _install_versioned_release(publication, staged, releases)
                    if consumed:
                        staging = None
                    os.symlink(os.path.relpath(release, root_staging), root_staging / "current")
                    _fsync_directory(root_staging)
                    try:
                        os.replace(root_staging, root)
                    except OSError as error:
                        raise BuildError(
                            f"{publication.kind} atomic publication installation failed"
                        ) from error
                    root_staging = None
                    _fsync_directory(parent)
                    _fsync_directory(root / "releases")
                finally:
                    if root_staging is not None and root_staging.exists():
                        shutil.rmtree(root_staging)
            return root
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def _atomic_publish(path: Path, data: bytes, mode: int) -> None:
    """Publish one legacy file for callers that still test the primitive directly."""

    if path.is_symlink():
        raise BuildError(f"refusing to replace a symlink: {path}")
    try:
        if path.exists() and _sha256_bytes(_read_stable_file(path)) != _sha256_bytes(data):
            raise BuildError(f"refusing to replace a different published artifact: {path}")
        privfs.atomic_write_private(path, data)
        os.chmod(path, mode)
    except privfs.PrivateFileError as error:
        raise BuildError(f"private artifact publication failed: {error}") from error
    except OSError as error:
        raise BuildError(f"private artifact publication failed: {path}") from error


def _manifest_object(path: Path, kind: str) -> dict[str, JSONValue]:
    try:
        value = json.loads(
            _read_stable_file(path).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_manifest_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BuildError(f"{kind} publication manifest is malformed") from error
    if not isinstance(value, dict):
        raise BuildError(f"{kind} publication manifest is not an object")
    return cast(dict[str, JSONValue], value)


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    manifest: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in manifest:
            raise ValueError(f"duplicate manifest key: {key}")
        manifest[key] = value
    return manifest


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BuildError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildError(f"{label} is not a non-empty string")
    return value


def _require_exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise BuildError(f"{label} is not a boolean")
    return value


def _require_exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise BuildError(f"{label} is not an integer")
    return value


def _require_manifest_keys(manifest: dict[str, JSONValue], expected: set[str], kind: str) -> None:
    if set(manifest) != expected:
        raise BuildError(f"{kind} publication manifest fields are not canonical")


def _validate_matcher_manifest(manifest: dict[str, JSONValue]) -> None:
    _require_manifest_keys(
        manifest,
        {
            "schema",
            "schema_version",
            "artifact",
            "artifact_sha256",
            "source_hashes",
            "workspace_sha256",
            "source_sha256",
            "source_snapshot_sha256",
            "protocol",
            "target",
            "toolchain",
            "build",
        },
        "matcher",
    )
    if (
        manifest["schema"] != "crowley.template-matcher-release"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        raise BuildError("matcher publication manifest identity is not canonical")
    if manifest["artifact"] != pins.MATCHER_ARTIFACT_NAME or manifest["protocol"] != pins.MATCHER_PROTOCOL:
        raise BuildError("matcher publication manifest identity is not pinned")
    if manifest["target"] != TARGET:
        raise BuildError("matcher publication manifest target is not pinned")
    source_hashes_value = manifest["source_hashes"]
    if not isinstance(source_hashes_value, dict):
        raise BuildError("matcher publication source hashes are malformed")
    source_hashes_json = cast(dict[str, JSONValue], source_hashes_value)
    if set(source_hashes_json) != set(pins.MATCHER_SOURCE_FILES):
        raise BuildError("matcher publication source hashes are not canonical")
    source_hashes: dict[str, str] = {}
    for name in pins.MATCHER_SOURCE_FILES:
        source_hashes[name] = _require_sha256(source_hashes_json[name], f"matcher source hash {name}")
    _require_sha256(manifest["artifact_sha256"], "matcher artifact digest")
    _require_sha256(manifest["workspace_sha256"], "matcher workspace digest")
    if manifest["source_sha256"] != _combined_source_hash(source_hashes, pins.MATCHER_SOURCE_FILES):
        raise BuildError("matcher publication source digest is inconsistent")
    if manifest["source_snapshot_sha256"] != manifest["source_sha256"]:
        raise BuildError("matcher publication source snapshot digest is inconsistent")
    toolchain = manifest["toolchain"]
    if not isinstance(toolchain, dict) or set(toolchain) != {"rustc", "rustc_verbose", "cargo", "rustfmt"}:
        raise BuildError("matcher publication toolchain provenance is malformed")
    for name in ("rustc", "rustc_verbose", "cargo", "rustfmt"):
        _require_string(cast(dict[str, JSONValue], toolchain)[name], f"matcher toolchain {name}")
    build = manifest["build"]
    if not isinstance(build, dict) or set(build) != {
        "profile",
        "locked",
        "offline",
        "incremental",
        "debuginfo",
        "strip",
        "codegen_units",
        "target_cpu",
        "remap_path",
    }:
        raise BuildError("matcher publication build provenance is not canonical")
    typed_build = cast(dict[str, JSONValue], build)
    if (
        typed_build["profile"] != "release"
        or _require_exact_bool(typed_build["locked"], "matcher build locked") is not True
        or _require_exact_bool(typed_build["offline"], "matcher build offline") is not True
        or _require_exact_bool(typed_build["incremental"], "matcher build incremental") is not False
        or _require_exact_int(typed_build["debuginfo"], "matcher build debuginfo") != 0
        or typed_build["strip"] != "symbols"
        or _require_exact_int(typed_build["codegen_units"], "matcher build codegen_units") != 1
        or typed_build["target_cpu"] is not None
        or typed_build["remap_path"] != "/rustsrc"
    ):
        raise BuildError("matcher publication build provenance is not canonical")


def _validate_observer_manifest(manifest: dict[str, JSONValue]) -> None:
    _require_manifest_keys(manifest, set(pins.OBSERVER_MANIFEST_KEYS), "observer")
    if manifest["schema"] != pins.OBSERVER_MANIFEST_SCHEMA:
        raise BuildError("observer publication manifest schema is not canonical")
    if manifest["target"] != TARGET or manifest["binary"] != pins.OBSERVER_ARTIFACT_NAME:
        raise BuildError("observer publication manifest identity is not pinned")
    _require_sha256(manifest["binary_sha256"], "observer binary digest")
    source_hashes = manifest["source_sha256"]
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(_OBSERVER_SOURCE.manifest_keys):
        raise BuildError("observer publication source hashes are not canonical")
    for name in _OBSERVER_SOURCE.manifest_keys:
        _require_sha256(cast(dict[str, JSONValue], source_hashes)[name], f"observer source hash {name}")
    _require_sha256(manifest["workspace_sha256"], "observer workspace digest")
    if manifest["static_library_resolution"] != {
        "mechanism": "pkg-config",
        "package": "x11",
        "mode": "static",
        "libraries": [f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES],
    }:
        raise BuildError("observer static library provenance is not canonical")
    static_inputs = manifest["static_inputs"]
    expected_inputs = {f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES}
    if not isinstance(static_inputs, dict) or set(static_inputs) != expected_inputs:
        raise BuildError("observer static input provenance is malformed")
    for name in expected_inputs:
        _require_sha256(cast(dict[str, JSONValue], static_inputs)[name], f"observer static input {name}")
    for field in ("linker", "toolchain"):
        value = manifest[field]
        expected_fields = {"driver", "ld"} if field == "linker" else {"rustc", "cargo", "rustfmt"}
        if field == "linker":
            if not isinstance(value, dict) or set(value) != expected_fields:
                raise BuildError("observer linker provenance is malformed")
            records = cast(dict[str, JSONValue], value)
            for name in ("driver", "ld"):
                record = records[name]
                if not isinstance(record, dict) or set(record) != {"name", "sha256", "version"}:
                    raise BuildError("observer linker provenance is malformed")
                typed_record = cast(dict[str, JSONValue], record)
                _require_string(typed_record["name"], f"observer linker {name} name")
                _require_sha256(typed_record["sha256"], f"observer linker {name} digest")
                _require_string(typed_record["version"], f"observer linker {name} version")
        elif not isinstance(value, dict) or set(value) != expected_fields:
            raise BuildError("observer toolchain provenance is malformed")
        else:
            records = cast(dict[str, JSONValue], value)
            for name in expected_fields:
                _require_string(records[name], f"observer toolchain {name}")


def _verify_matcher_publication(root: Path, expected: dict[str, JSONValue] | None) -> None:
    artifact = root / pins.MATCHER_ARTIFACT_NAME
    manifest_path = root / pins.MATCHER_MANIFEST_NAME
    actual = _manifest_object(manifest_path, "matcher")
    _validate_matcher_manifest(actual)
    if expected is not None and actual != expected:
        raise BuildError("matcher publication manifest differs from the staged build")
    expected_digest = actual.get("artifact_sha256")
    if not isinstance(expected_digest, str) or _sha256_bytes(_read_stable_file(artifact)) != expected_digest:
        raise BuildError("matcher publication artifact digest differs from its manifest")


def _verify_observer_publication(
    root: Path,
    artifact_name: str,
    manifest_name: str,
    expected: dict[str, JSONValue] | None,
) -> None:
    artifact = root / artifact_name
    actual = _manifest_object(root / manifest_name, "observer")
    _validate_observer_manifest(actual)
    if expected is not None and actual != expected:
        raise BuildError("observer publication manifest differs from the staged build")
    expected_digest = actual.get("binary_sha256")
    if not isinstance(expected_digest, str) or _sha256_bytes(_read_stable_file(artifact)) != expected_digest:
        raise BuildError("observer publication artifact digest differs from its manifest")


def _verify_xdotool_staged_publication(root: Path) -> None:
    _verify_xdotool_publication(root)
    _assert_xdotool_static_linkage(root / pins.XDOTOOL_ARTIFACT_NAME)


def default_matcher_artifact_root() -> Path:
    """Return the pinned matcher publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "template-matcher" / "v1"


def default_observer_artifact_path() -> Path:
    """Return the pinned observer publication path for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "x11-identity-observer" / "v1" / "observer"


def default_xdotool_artifact_root() -> Path:
    """Return the pinned xdotool publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "xdotool"


def build_matcher(
    output_dir: Path | None = None,
    *,
    source_root: Path = NATIVE_ROOT,
    replace: bool = False,
) -> dict[str, JSONValue]:
    if output_dir is None:
        output_dir = default_matcher_artifact_root()
    output_dir = output_dir.absolute()
    privfs.ensure_private_dir(output_dir.parent)
    toolchain = required_toolchain()
    workspace_parent = output_dir.parent
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
        encoded_manifest = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        publication = _Publication(
            kind="matcher",
            root=output_dir,
            binary_name=pins.MATCHER_ARTIFACT_NAME,
            files=(
                _PublicationFile(pins.MATCHER_ARTIFACT_NAME, first_bytes, 0o700),
                _PublicationFile(pins.MATCHER_MANIFEST_NAME, encoded_manifest, 0o600),
            ),
            verify_staged=lambda root: _verify_matcher_publication(root, manifest),
            verify_existing=lambda root: _verify_matcher_publication(root, None),
        )
        _publish_publication(publication, replace=replace)
        artifact_path = output_dir / "current" / pins.MATCHER_ARTIFACT_NAME
        manifest_path = output_dir / "current" / pins.MATCHER_MANIFEST_NAME
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


def build_observer(
    output: Path | None = None,
    *,
    source_root: Path = NATIVE_ROOT,
    replace: bool = False,
) -> dict[str, JSONValue]:
    if output is None:
        output = default_observer_artifact_path()
    output = output.absolute()
    publication_root = output.parent
    privfs.ensure_private_dir(publication_root.parent)
    toolchain = required_toolchain()
    artifact_name = output.name
    manifest_name = output.with_suffix(".manifest.json").name
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
        manifest = _observer_manifest(
            source_hashes,
            first,
            toolchain,
            workspace_sha256,
            library_paths,
            static_inputs,
            linker,
        )
        encoded_manifest = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        publication = _Publication(
            kind="observer",
            root=publication_root,
            binary_name=artifact_name,
            files=(
                _PublicationFile(artifact_name, first_bytes, 0o700),
                _PublicationFile(manifest_name, encoded_manifest, 0o600),
            ),
            verify_staged=lambda root: _verify_observer_publication(
                root, artifact_name, manifest_name, manifest
            ),
            verify_existing=lambda root: _verify_observer_publication(
                root, artifact_name, manifest_name, None
            ),
        )
        _publish_publication(publication, replace=replace)
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


def build_xdotool(
    archive: Path = VENDORED_SOURCE_ARCHIVE,
    destination: Path | None = None,
    *,
    replace: bool = False,
) -> Path:
    """Build and publish the pinned XTEST xdotool source archive."""

    archive = archive.resolve()
    if _xdotool_sha256(archive) != pins.XDOTOOL_ARCHIVE_SHA256:
        raise BuildError("source archive digest does not match the pinned upstream archive")
    if destination is None:
        destination = default_xdotool_artifact_root()
    destination = destination.absolute()
    if destination.is_symlink():
        raise BuildError("private artifact destination cannot be a symlink")
    privfs.ensure_private_dir(destination.parent)
    with tempfile.TemporaryDirectory(prefix="crowley-xdotool-build-") as temporary_name:
        work = Path(temporary_name)
        first_bytes, first_inputs = _build_xdotool_once(archive, work / "first")
        second_bytes, second_inputs = _build_xdotool_once(archive, work / "second")
        if first_bytes != second_bytes:
            raise BuildError("independent xdotool builds are not byte-identical")
        if first_inputs != second_inputs:
            raise BuildError("xdotool compiler or linker inputs changed during the build")
        executable_digest = _sha256_bytes(first_bytes)
        manifest = {
            "commit": pins.XDOTOOL_COMMIT,
            "archive_sha256": pins.XDOTOOL_ARCHIVE_SHA256,
            "executable": pins.XDOTOOL_ARTIFACT_NAME,
            "executable_sha256": executable_digest,
            pins.XDOTOOL_BUILD_INPUTS_KEY: first_inputs,
        }
        encoded_manifest = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        publication = _Publication(
            kind="xdotool",
            root=destination,
            binary_name=pins.XDOTOOL_ARTIFACT_NAME,
            files=(
                _PublicationFile(pins.XDOTOOL_ARTIFACT_NAME, first_bytes, 0o700),
                _PublicationFile(pins.XDOTOOL_ARCHIVE_NAME, _read_stable_file(archive), 0o600),
                _PublicationFile(pins.XDOTOOL_MANIFEST_NAME, encoded_manifest, 0o600),
            ),
            verify_staged=_verify_xdotool_staged_publication,
            verify_existing=_verify_xdotool_staged_publication,
        )
        _publish_publication(publication, replace=replace)
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
    build_parser.add_argument("--replace", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--source-root", type=Path, default=NATIVE_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(args.source_root.resolve())
        elif args.helper == "matcher":
            result = build_matcher(args.output_dir, replace=args.replace)
        elif args.helper == "observer":
            result = build_observer(args.output, replace=args.replace)
        else:
            result = {"artifact": str(build_xdotool(args.archive, args.output_dir, replace=args.replace))}
    except (BuildError, privfs.PrivateFileError) as error:
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
