from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_documented_offline_self_check_command_uses_the_maintained_contract(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    cache = tmp_path / "cache"
    runtime.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    environment = {
        **os.environ,
        "XDG_CACHE_HOME": str(cache),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    result = subprocess.run(
        [sys.executable, "tools/gnome-telemetry-region/offline_harness.py", "--self-check"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert json.loads(result.stdout) == {
        "binary": str(
            cache / "crowley" / "gnome-telemetry-region-r6" / "prefix" / "bin" / "gnome-screenshot"
        ),
        "self_check": True,
        "self_test": False,
    }
