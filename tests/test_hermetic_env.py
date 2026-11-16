"""Regression tests for test-run configuration and state isolation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from crowley_scanner.companion.config import config_path


def test_companion_paths_use_private_xdg_roots() -> None:
    config_root = Path(os.environ["XDG_CONFIG_HOME"])
    state_root = Path(os.environ["XDG_STATE_HOME"])
    resolved_config_path = config_path()

    assert resolved_config_path == config_root / "crowley" / "scanner.toml"
    assert stat.S_IMODE(config_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert not resolved_config_path.is_relative_to(Path.home())
    assert {name for name in os.environ if name.startswith("CROWLEY_")} <= {"CROWLEY_TEST_DATABASE_URL"}
