"""Reproducible build and publication of the pinned XTEST xdotool input tool.

The vendored xdotool source archive is built twice with the system C toolchain; the
builds must be byte-identical.  The result is published under the private cache root
and read back by ``crowley.gui.input_tool``.
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

from crowley.platform import privfs
from crowley.platform.paths import XdgDirs

from . import pins

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_PACKAGE_RESOURCES = files("crowley.native")
VENDORED_SOURCE_ARCHIVE: Final[Path] = Path(
    str(_PACKAGE_RESOURCES.joinpath("resources", "vendor", "xdotool-source.tar.gz"))
).resolve()


class BuildError(RuntimeError):
    """A native build or publication failed a required invariant."""


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


def _verify_xdotool_staged_publication(root: Path) -> None:
    _verify_xdotool_publication(root)
    _assert_xdotool_static_linkage(root / pins.XDOTOOL_ARTIFACT_NAME)


def default_xdotool_artifact_root() -> Path:
    """Return the pinned xdotool publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "xdotool"


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crowley-native", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("helper", choices=("xdotool",))
    build_parser.add_argument("--output-dir", type=Path)
    build_parser.add_argument("--archive", type=Path, default=VENDORED_SOURCE_ARCHIVE)
    build_parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = {"artifact": str(build_xdotool(args.archive, args.output_dir, replace=args.replace))}
    except (BuildError, privfs.PrivateFileError) as error:
        print(f"crowley-native: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


__all__ = [
    "BuildError",
    "VENDORED_SOURCE_ARCHIVE",
    "build_xdotool",
    "default_xdotool_artifact_root",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
