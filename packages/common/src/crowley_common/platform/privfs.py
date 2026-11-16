"""Private filesystem primitives with descriptor-bound reads and writes."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class PrivateFileError(RuntimeError):
    """Base class for private filesystem safety and I/O failures."""


class PrivatePathError(PrivateFileError):
    """A private path is missing, non-private, or has an unsafe component."""


class PrivateReadError(PrivateFileError):
    """A bounded private read failed or the file changed during the read."""


class PrivateWriteError(PrivateFileError):
    """An atomic private publication failed."""


class PrivateCopyError(PrivateFileError):
    """A retained private copy failed or its source changed."""


@dataclass(frozen=True)
class RetainedFile:
    """Metadata for one atomically retained private file."""

    path: Path
    sha256: str
    size: int


def _require_absolute(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise PrivatePathError(f"{label} must be absolute")


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise PrivatePathError(f"{label} is unavailable") from exc


def _directory_lstat(path: Path) -> os.stat_result:
    return path.lstat()


def _directory_parts(path: Path) -> list[Path]:
    _require_absolute(path, label="private directory")
    current = Path(path.anchor)
    parts: list[Path] = []
    for part in path.parts[1:]:
        current /= part
        parts.append(current)
    return parts


def ensure_private_dir(path: str | os.PathLike[str]) -> None:
    """Create or verify an owned, non-symlink directory with mode 0700.

    Existing ancestors are checked for symlinks and directory type before the
    requested directory is created. Existing ancestors may be system-owned;
    the requested directory itself must be owned by the current user and have
    exactly mode 0700.
    """

    target = Path(path)
    parts = _directory_parts(target)
    if not parts:
        raise PrivatePathError("private directory must not be the filesystem root")
    for current in parts:
        try:
            info = _directory_lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise PrivatePathError(f"cannot create private directory {current}") from exc
            try:
                os.chmod(current, 0o700)
            except OSError as exc:
                raise PrivatePathError(f"cannot secure private directory {current}") from exc
            continue
        except OSError as exc:
            raise PrivatePathError(f"cannot inspect private directory {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PrivatePathError("private directory contains a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise PrivatePathError("private directory contains a non-directory")
        if current == target:
            if info.st_uid != os.getuid():
                raise PrivatePathError("private directory is not owned by the current user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise PrivatePathError("private directory must have mode 0700")


def _validate_limit(max_bytes: object) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise PrivateReadError("max_bytes must be a nonnegative integer")


def _regular_file_stat(path: Path, *, label: str) -> os.stat_result:
    info = _lstat(path, label=label)
    if stat.S_ISLNK(info.st_mode):
        raise PrivatePathError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise PrivatePathError(f"{label} must be a regular file")
    if info.st_nlink != 1:
        raise PrivatePathError(f"{label} must have one link")
    return info


def _open_read_descriptor(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    before = _regular_file_stat(path, label=label)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PrivateReadError(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not _same_file(opened, before):
            raise PrivateReadError(f"{label} changed before read")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise PrivateReadError(f"{label} is not a regular file")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_uid,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_uid,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def read_bounded(path: str | os.PathLike[str], max_bytes: int) -> bytes:
    """Read at most *max_bytes* from a private regular file without following links."""

    _validate_limit(max_bytes)
    target = Path(path)
    _require_absolute(target, label="private file")
    descriptor, before = _open_read_descriptor(target, label="private file")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, max_bytes - total + 1)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PrivateReadError("private file exceeds its size bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if not _same_file(after, before) or total != before.st_size:
            raise PrivateReadError("private file changed during read")
    except PrivateFileError:
        raise
    except OSError as exc:
        raise PrivateReadError("private file cannot be read") from exc
    finally:
        os.close(descriptor)
    try:
        final = target.lstat()
    except OSError as exc:
        raise PrivateReadError("private file changed after read") from exc
    if not _same_file(final, after):
        raise PrivateReadError("private file changed after read")
    return b"".join(chunks)


def _open_private_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise PrivateWriteError(f"private directory {path} cannot be opened") from exc


def _validate_bytes(data: object) -> bytes:
    if not isinstance(data, bytes):
        raise PrivateWriteError("private file data must be bytes")
    return data


def atomic_write_private(path: str | os.PathLike[str], data: bytes) -> None:
    """Atomically publish bytes as a mode-0600 file in its private directory."""

    data = _validate_bytes(data)
    target = Path(path)
    _require_absolute(target, label="private file")
    ensure_private_dir(target.parent)
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise PrivateWriteError("private file cannot be inspected") from exc
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise PrivateWriteError("private file must not be a symlink")
        if not stat.S_ISREG(existing.st_mode):
            raise PrivateWriteError("private file must be a regular file")
    descriptor = -1
    temporary: Path | None = None
    directory_descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        directory_descriptor = _open_private_directory(target.parent)
        os.fsync(directory_descriptor)
    except PrivateFileError:
        raise
    except OSError as exc:
        raise PrivateWriteError("private file publication failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def retain_private_copy(src: str | os.PathLike[str], dst_dir: str | os.PathLike[str]) -> RetainedFile:
    """Copy *src* into *dst_dir*, hashing during the copy and publishing atomically."""

    source = Path(src)
    destination_dir = Path(dst_dir)
    _require_absolute(source, label="source file")
    ensure_private_dir(destination_dir)
    source_before = _regular_file_stat(source, label="source file")
    destination = destination_dir / source.name
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise PrivateCopyError("retained destination cannot be inspected") from exc
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise PrivateCopyError("retained destination must not be a symlink")

    source_descriptor = -1
    temporary_descriptor = -1
    temporary: Path | None = None
    directory_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        try:
            source_descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise PrivateCopyError("source file cannot be opened") from exc
        opened = os.fstat(source_descriptor)
        if not _same_file(opened, source_before):
            raise PrivateCopyError("source file changed before copy")
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination_dir
        )
        temporary = Path(temporary_name)
        os.fchmod(temporary_descriptor, 0o600)
        with os.fdopen(temporary_descriptor, "wb") as output:
            temporary_descriptor = -1
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_descriptor)
        if not _same_file(after, opened) or size != opened.st_size:
            raise PrivateCopyError("source file changed during copy")
        final_source = source.lstat()
        if not _same_file(final_source, after):
            raise PrivateCopyError("source file changed after copy")
        os.close(source_descriptor)
        source_descriptor = -1
        os.replace(temporary, destination)
        temporary = None
        directory_descriptor = _open_private_directory(destination_dir)
        os.fsync(directory_descriptor)
        return RetainedFile(path=destination, sha256=digest.hexdigest(), size=size)
    except PrivateFileError:
        raise
    except OSError as exc:
        raise PrivateCopyError("private copy failed") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def sha256_file(path: str | os.PathLike[str], max_bytes: int) -> str:
    """Hash a private regular file while enforcing a maximum byte count."""

    _validate_limit(max_bytes)
    target = Path(path)
    _require_absolute(target, label="private file")
    descriptor, before = _open_read_descriptor(target, label="private file")
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PrivateReadError("private file exceeds its size bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _same_file(after, before) or total != before.st_size:
            raise PrivateReadError("private file changed during hashing")
    except PrivateFileError:
        raise
    except OSError as exc:
        raise PrivateReadError("private file cannot be hashed") from exc
    finally:
        os.close(descriptor)
    try:
        final = target.lstat()
    except OSError as exc:
        raise PrivateReadError("private file changed after hashing") from exc
    if not _same_file(final, after):
        raise PrivateReadError("private file changed after hashing")
    return digest.hexdigest()


__all__ = [
    "PrivateCopyError",
    "PrivateFileError",
    "PrivatePathError",
    "PrivateReadError",
    "PrivateWriteError",
    "RetainedFile",
    "atomic_write_private",
    "ensure_private_dir",
    "read_bounded",
    "retain_private_copy",
    "sha256_file",
]
