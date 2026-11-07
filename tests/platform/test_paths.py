from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.platform import paths
from crowley.platform.paths import XdgDirs, XdgPathError


def _runtime(root: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir()
    runtime.chmod(0o700)
    return runtime


def _environment(root: Path) -> dict[str, str]:
    return {"HOME": str(root / "home"), "XDG_RUNTIME_DIR": str(_runtime(root))}


def test_from_environ_uses_xdg_defaults_and_derives_crowley_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _environment(tmp_path)

    dirs = XdgDirs.from_environ(env)

    assert dirs.config_home == home / ".config"
    assert dirs.data_home == home / ".local" / "share"
    assert dirs.state_home == home / ".local" / "state"
    assert dirs.cache_home == home / ".cache"
    assert dirs.runtime_dir == Path(env["XDG_RUNTIME_DIR"])
    assert dirs.crowley_config == dirs.config_home / "crowley"
    assert dirs.crowley_data == dirs.data_home / "crowley"
    assert dirs.crowley_state == dirs.state_home / "crowley"
    assert dirs.crowley_cache == dirs.cache_home / "crowley"
    assert dirs.crowley_runtime == dirs.runtime_dir / "crowley"


@pytest.mark.parametrize(
    "variable",
    ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"),
)
def test_relative_xdg_values_are_rejected(tmp_path: Path, variable: str) -> None:
    env = _environment(tmp_path)
    env[variable] = "relative/path"

    with pytest.raises(XdgPathError, match=f"{variable} must be absolute"):
        XdgDirs.from_environ(env)


def test_empty_xdg_values_use_their_spec_defaults(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env.update(
        {
            "XDG_CONFIG_HOME": "",
            "XDG_DATA_HOME": "",
            "XDG_STATE_HOME": "",
            "XDG_CACHE_HOME": "",
        }
    )

    dirs = XdgDirs.from_environ(env)

    assert dirs.config_home == Path(env["HOME"]) / ".config"


@pytest.mark.parametrize("missing", ("HOME", "XDG_RUNTIME_DIR"))
def test_required_environment_values_are_rejected(tmp_path: Path, missing: str) -> None:
    env = _environment(tmp_path)
    del env[missing]

    with pytest.raises(XdgPathError, match=f"{missing} must be set"):
        XdgDirs.from_environ(env)


def test_home_must_be_absolute(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["HOME"] = "relative-home"

    with pytest.raises(XdgPathError, match="HOME must be absolute"):
        XdgDirs.from_environ(env)


def test_runtime_dir_must_be_absolute(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["XDG_RUNTIME_DIR"] = "relative-runtime"

    with pytest.raises(XdgPathError, match="XDG_RUNTIME_DIR must be absolute"):
        XdgDirs.from_environ(env)


@pytest.mark.parametrize("mode", (0o755, 0o750))
def test_runtime_dir_must_be_private(tmp_path: Path, mode: int) -> None:
    env = _environment(tmp_path)
    runtime = Path(env["XDG_RUNTIME_DIR"])
    runtime.chmod(mode)

    with pytest.raises(XdgPathError, match="mode 0700"):
        XdgDirs.from_environ(env)


def test_runtime_dir_must_exist_and_not_be_a_symlink(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    runtime = Path(env["XDG_RUNTIME_DIR"])
    runtime.rmdir()
    target = tmp_path / "real-runtime"
    target.mkdir()
    target.chmod(0o700)
    runtime.symlink_to(target, target_is_directory=True)

    with pytest.raises(XdgPathError, match="must not be a symlink"):
        XdgDirs.from_environ(env)


def test_runtime_owner_is_verified_through_the_test_stat_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _environment(tmp_path)
    real_stat = Path(env["XDG_RUNTIME_DIR"]).stat()
    fake_stat = SimpleNamespace(st_mode=real_stat.st_mode, st_uid=os.getuid() + 1)
    monkeypatch.setattr(paths, "_runtime_lstat", lambda _path: fake_stat)

    with pytest.raises(XdgPathError, match="owned by the current user"):
        XdgDirs.from_environ(env)


def test_runtime_dir_must_be_a_directory(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    runtime = Path(env["XDG_RUNTIME_DIR"])
    runtime.rmdir()
    runtime.write_bytes(b"not a directory")
    runtime.chmod(0o600)

    with pytest.raises(XdgPathError, match="must be a directory"):
        XdgDirs.from_environ(env)
