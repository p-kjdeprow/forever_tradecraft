"""Process-lifetime input lease and guardian errors."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from pathlib import Path

from crowley.platform import privfs


class GuardianError(RuntimeError):
    """Lease, generation, deadline, or parent-death failure."""


def _open_private_parent(path: Path) -> int:
    """Open a validated private parent by walking every component safely."""

    if not path.is_absolute() or ".." in path.parts:
        raise GuardianError("input lease lock directory path is not normalized and absolute")
    try:
        privfs.ensure_private_dir(path)
    except privfs.PrivateFileError as exc:
        raise GuardianError("input lease lock directory is not a private directory") from exc

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            try:
                os.close(descriptor)
            except BaseException:
                os.close(child)
                raise
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise GuardianError("input lease lock directory is not a private owned directory")
        return descriptor
    except GuardianError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise GuardianError("input lease lock directory cannot be opened safely") from exc


class InputLease:
    """One process-lifetime non-blocking flock."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._fd: int | None = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        with self._lock:
            if self._fd is not None:
                return
            parent_fd = _open_private_parent(self.path.parent)
            try:
                try:
                    existing = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                except OSError as exc:
                    raise GuardianError("input lease lock path cannot be inspected") from exc
                if existing is not None and (
                    stat.S_ISLNK(existing.st_mode)
                    or not stat.S_ISREG(existing.st_mode)
                    or existing.st_uid != os.getuid()
                    or stat.S_IMODE(existing.st_mode) != 0o600
                ):
                    raise GuardianError("input lease lock path is not a private owned regular file")
                flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
                try:
                    fd = os.open(self.path.name, flags, 0o600, dir_fd=parent_fd)
                except OSError as exc:
                    raise GuardianError("input lease lock cannot be opened safely") from exc
                try:
                    metadata = os.fstat(fd)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                    ):
                        raise GuardianError("input lease lock is not a private owned regular file")
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        raise GuardianError("another process owns the desktop input lease") from exc
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            finally:
                os.close(parent_fd)

    def release(self) -> None:
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


__all__ = ["GuardianError", "InputLease"]
