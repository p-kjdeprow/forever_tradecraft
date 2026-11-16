"""Reviewed pins for the XTEST input tool (a pinned xdotool build).

The values in this module are contracts, not discovery hints.  Build and runtime
code imports them from here so a release cannot silently use a different source
archive or manifest shape.
"""

from __future__ import annotations

from typing import Final

XDOTOOL_COMMIT: Final[str] = "14ca26a2c95035f9c635a6a11d075e8d7d0d6529"
XDOTOOL_ARCHIVE_SHA256: Final[str] = "d0ba12e37419bfe12651abea7625ba95b6858e44ba8fc79997bfb84112f64dc3"
XDOTOOL_ARTIFACT_NAME: Final[str] = "crowley-xdotool"
XDOTOOL_MANIFEST_NAME: Final[str] = "manifest.json"
XDOTOOL_ARCHIVE_NAME: Final[str] = "source.tar.gz"
XDOTOOL_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"commit", "archive_sha256", "executable", "executable_sha256", "build_inputs"}
)
XDOTOOL_BUILD_INPUTS_KEY: Final[str] = "build_inputs"

__all__ = [
    "XDOTOOL_ARCHIVE_NAME",
    "XDOTOOL_ARCHIVE_SHA256",
    "XDOTOOL_ARTIFACT_NAME",
    "XDOTOOL_BUILD_INPUTS_KEY",
    "XDOTOOL_COMMIT",
    "XDOTOOL_MANIFEST_KEYS",
]
