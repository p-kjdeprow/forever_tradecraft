"""Private lease paths for session fixtures."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def private_lease_path(root: Path, name: str = "desktop.lock") -> Path:
    """Return a lease path below an owned directory with mode 0700."""

    directory = root / ".lease"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = directory.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise AssertionError("test lease directory is not a private owned 0700 directory")
    return directory / name
