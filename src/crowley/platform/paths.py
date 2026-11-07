"""Strict XDG directory resolution for Crowley."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class XdgPathError(ValueError):
    """The environment does not describe safe absolute XDG directories."""


def _runtime_lstat(path: Path) -> os.stat_result:
    """Provide the runtime stat seam used by tests without reading the environment."""

    return path.lstat()


def _absolute_path(value: str, *, name: str) -> Path:
    if not value:
        raise XdgPathError(f"{name} must not be empty")
    if "\x00" in value:
        raise XdgPathError(f"{name} contains a NUL byte")
    path = Path(value)
    if not path.is_absolute():
        raise XdgPathError(f"{name} must be absolute")
    return path


@dataclass(frozen=True)
class XdgDirs:
    """Resolved XDG roots and their Crowley subdirectories.

    ``from_environ`` takes an explicit mapping so callers and tests choose the
    environment boundary. It never reads ``os.environ`` implicitly. The four
    XDG roots follow the XDG Base Directory defaults relative to ``HOME``;
    ``XDG_RUNTIME_DIR`` has no default and is checked as a private directory.

    The digest canonical form elsewhere in the repository uses compact,
    sorted-key JSON encoded as UTF-8 with ``ensure_ascii=False``; this class
    only resolves paths and does not serialize them.
    """

    config_home: Path
    data_home: Path
    state_home: Path
    cache_home: Path
    runtime_dir: Path
    crowley_config: Path
    crowley_data: Path
    crowley_state: Path
    crowley_cache: Path
    crowley_runtime: Path

    _ROOT_DEFAULTS: ClassVar[dict[str, str]] = {
        "XDG_CONFIG_HOME": ".config",
        "XDG_DATA_HOME": ".local/share",
        "XDG_STATE_HOME": ".local/state",
        "XDG_CACHE_HOME": ".cache",
    }

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> XdgDirs:
        """Resolve directories from *env*, validating every required root."""

        home_value = env.get("HOME")
        if not isinstance(home_value, str) or not home_value:
            raise XdgPathError("HOME must be set")
        home = _absolute_path(home_value, name="HOME")

        roots: dict[str, Path] = {}
        for variable, suffix in cls._ROOT_DEFAULTS.items():
            configured = env.get(variable)
            value = configured if isinstance(configured, str) and configured else str(home / suffix)
            roots[variable] = _absolute_path(value, name=variable)

        runtime_value = env.get("XDG_RUNTIME_DIR")
        if not isinstance(runtime_value, str) or not runtime_value:
            raise XdgPathError("XDG_RUNTIME_DIR must be set")
        runtime = _absolute_path(runtime_value, name="XDG_RUNTIME_DIR")
        try:
            runtime_stat = _runtime_lstat(runtime)
        except OSError as exc:
            raise XdgPathError("XDG_RUNTIME_DIR is unavailable") from exc
        if stat.S_ISLNK(runtime_stat.st_mode):
            raise XdgPathError("XDG_RUNTIME_DIR must not be a symlink")
        if not stat.S_ISDIR(runtime_stat.st_mode):
            raise XdgPathError("XDG_RUNTIME_DIR must be a directory")
        if runtime_stat.st_uid != os.getuid():
            raise XdgPathError("XDG_RUNTIME_DIR must be owned by the current user")
        if stat.S_IMODE(runtime_stat.st_mode) != 0o700:
            raise XdgPathError("XDG_RUNTIME_DIR must have mode 0700")

        config_home = roots["XDG_CONFIG_HOME"]
        data_home = roots["XDG_DATA_HOME"]
        state_home = roots["XDG_STATE_HOME"]
        cache_home = roots["XDG_CACHE_HOME"]
        return cls(
            config_home=config_home,
            data_home=data_home,
            state_home=state_home,
            cache_home=cache_home,
            runtime_dir=runtime,
            crowley_config=config_home / "crowley",
            crowley_data=data_home / "crowley",
            crowley_state=state_home / "crowley",
            crowley_cache=cache_home / "crowley",
            crowley_runtime=runtime / "crowley",
        )


__all__ = ["XdgDirs", "XdgPathError"]
