"""Atomic native-release resolution shared by native helper consumers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_MAX_MANIFEST_BYTES: Final[int] = 256 * 1024
_MAX_RELEASE_FILE_BYTES: Final[int] = 64 * 1024 * 1024


class PublicationReadError(RuntimeError):
    """A current native release is unavailable or fails its contract."""


@dataclass(frozen=True)
class NativePublication:
    """One immutable release resolved through one current-pointer snapshot."""

    root: Path
    release_digest: str
    binary_fd: int
    binary_bytes: bytes
    binary_digest: str
    manifest: dict[str, JSONValue]
    sidecars: dict[str, bytes]


def _reject_duplicate_keys(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _private_directory(descriptor: int, label: str) -> os.stat_result:
    try:
        info = os.fstat(descriptor)
    except OSError as error:
        raise PublicationReadError(f"{label} cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PublicationReadError(f"{label} is not user-owned and private")
    return info


def _open_directory_at(parent: int, name: str, label: str) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        raise PublicationReadError(f"{label} is unavailable") from error
    try:
        _private_directory(descriptor, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_descriptor(descriptor: int, *, maximum: int, label: str) -> bytes:
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
        ):
            raise PublicationReadError(f"{label} is not a private regular file")
        if before.st_size > maximum:
            raise PublicationReadError(f"{label} exceeds its size bound")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise PublicationReadError(f"{label} exceeds its size bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except PublicationReadError:
        raise
    except OSError as error:
        raise PublicationReadError(f"{label} cannot be read") from error
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or total != before.st_size:
        raise PublicationReadError(f"{label} changed during read")
    return b"".join(chunks)


def _open_and_read_at(parent: int, name: str, *, maximum: int, label: str) -> bytes:
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except OSError as error:
        raise PublicationReadError(f"{label} is unavailable") from error
    try:
        return _read_descriptor(descriptor, maximum=maximum, label=label)
    finally:
        os.close(descriptor)


def _read_current_target(root_fd: int) -> str:
    try:
        target = os.readlink("current", dir_fd=root_fd)
    except OSError as error:
        raise PublicationReadError("native publication current pointer is unavailable") from error
    parts = Path(target).parts
    if len(parts) != 2 or parts[0] != "releases":
        raise PublicationReadError("native publication current pointer is not canonical")
    digest = parts[1]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise PublicationReadError("native publication current pointer digest is malformed")
    return digest


def _manifest_object(data: bytes) -> dict[str, JSONValue]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PublicationReadError("native publication manifest is malformed") from error
    if not isinstance(value, dict):
        raise PublicationReadError("native publication manifest is not an object")
    return cast(dict[str, JSONValue], value)


def _canonical_file_name(name: str, label: str) -> None:
    parts = Path(name).parts
    if not name or len(parts) != 1 or parts[0] in {".", ".."}:
        raise PublicationReadError(f"native publication {label} name is not canonical")


def read_current_publication(
    root: str | os.PathLike[str],
    *,
    binary_name: str,
    manifest_name: str,
    sidecar_names: tuple[str, ...],
    validate_manifest: Callable[[dict[str, JSONValue], str], None],
) -> NativePublication:
    """Resolve ``current`` once and read one complete immutable release by dirfd."""

    artifact_root = Path(root).expanduser()
    _canonical_file_name(binary_name, "binary")
    _canonical_file_name(manifest_name, "manifest")
    for sidecar_name in sidecar_names:
        _canonical_file_name(sidecar_name, "sidecar")
    root_fd = -1
    releases_fd = -1
    release_fd = -1
    binary_fd = -1
    try:
        try:
            root_fd = os.open(artifact_root, _DIRECTORY_FLAGS)
        except OSError as error:
            raise PublicationReadError("native publication root is unavailable") from error
        _private_directory(root_fd, "native publication root")
        release_digest = _read_current_target(root_fd)
        releases_fd = _open_directory_at(root_fd, "releases", "native publication release store")
        release_fd = _open_directory_at(
            releases_fd,
            release_digest,
            "native publication release",
        )
        try:
            binary_fd = os.open(binary_name, _FILE_FLAGS, dir_fd=release_fd)
        except OSError as error:
            raise PublicationReadError("native publication binary is unavailable") from error
        binary_bytes = _read_descriptor(
            binary_fd,
            maximum=_MAX_RELEASE_FILE_BYTES,
            label="native publication binary",
        )
        binary_digest = hashlib.sha256(binary_bytes).hexdigest()
        if binary_digest != release_digest:
            raise PublicationReadError("native publication release digest does not match its binary")
        manifest_bytes = _open_and_read_at(
            release_fd,
            manifest_name,
            maximum=_MAX_MANIFEST_BYTES,
            label="native publication manifest",
        )
        manifest = _manifest_object(manifest_bytes)
        validate_manifest(manifest, binary_digest)
        sidecars = {
            name: _open_and_read_at(
                release_fd,
                name,
                maximum=_MAX_RELEASE_FILE_BYTES,
                label=f"native publication sidecar {name}",
            )
            for name in sidecar_names
        }
        return NativePublication(
            root=artifact_root,
            release_digest=release_digest,
            binary_fd=binary_fd,
            binary_bytes=binary_bytes,
            binary_digest=binary_digest,
            manifest=manifest,
            sidecars=sidecars,
        )
    except BaseException as error:
        if binary_fd >= 0:
            os.close(binary_fd)
        if isinstance(error, PublicationReadError):
            raise
        if isinstance(error, OSError):
            raise PublicationReadError("native publication read failed") from error
        raise
    finally:
        for descriptor in (release_fd, releases_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


__all__ = ["JSONValue", "NativePublication", "PublicationReadError", "read_current_publication"]
