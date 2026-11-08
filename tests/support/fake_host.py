"""Stable repository-derived identities for tests that do not access host files."""

from __future__ import annotations

from pathlib import Path

TESTS_ROOT = Path(__file__).parents[1]
FAKE_HOST_ROOT = TESTS_ROOT / "fixtures" / "fake-host"
FAKE_XAUTHORITY = FAKE_HOST_ROOT / "Xauthority"
FAKE_WINE_PREFIX = FAKE_HOST_ROOT / "wine"
FAKE_MODEL_DIR = FAKE_HOST_ROOT / "neutral"
