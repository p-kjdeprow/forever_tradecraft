"""The maintained application sources must pass Pyright without allowances."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_application_types_are_clean() -> None:
    completed = subprocess.run(
        ["pyright", "--project", "pyproject.toml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
