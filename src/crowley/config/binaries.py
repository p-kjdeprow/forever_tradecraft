"""Explicit runtime locations for reviewed external binaries."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crowley.native import pins
from crowley.platform.paths import XdgDirs

CODEX_PATH_ENV = "CROWLEY_CODEX_PATH"
CODEX_MODEL = "gpt-6-luna"
CODEX_CLI_VERSION = "codex-cli 0.162.1"


class BinaryConfigError(ValueError):
    """Raised when a required pinned binary location is not configured."""


@dataclass(frozen=True, slots=True)
class PinnedBinaries:
    """Pinned model contract and the operator-provisioned executable path."""

    codex_path: Path
    model: str
    cli_version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PinnedObserver:
    """The reviewed identity observer publication and its immutable digest pin."""

    path: Path
    sha256: str
    version: str
    provenance: str = pins.OBSERVER_PROVENANCE
    manifest_path: Path | None = None


def resolve_pinned_observer(xdg: XdgDirs) -> PinnedObserver:
    """Resolve the one reviewed observer publication under the XDG cache root.

    ``crowley-native build observer`` publishes immutable releases under
    ``v<protocol>/releases/<digest>/`` behind the atomic ``v<protocol>/current``
    pointer, so readers resolve the binary and its manifest through ``current``.
    """

    current = xdg.crowley_cache / "x11-identity-observer" / f"v{pins.OBSERVER_PROTOCOL_VERSION}" / "current"
    return PinnedObserver(
        path=current / "observer",
        sha256=pins.OBSERVER_ARTIFACT_SHA256,
        version=str(pins.OBSERVER_PROTOCOL_VERSION),
        provenance=pins.OBSERVER_PROVENANCE,
        manifest_path=current / "observer.manifest.json",
    )


def load_pinned_binaries(environ: Mapping[str, str] | None = None) -> PinnedBinaries:
    """Load the explicit Codex location without discovering a fallback binary."""

    values = os.environ if environ is None else environ
    raw_path = values.get(CODEX_PATH_ENV)
    if raw_path is None or not raw_path:
        raise BinaryConfigError(f"{CODEX_PATH_ENV} is required for the pinned Codex CLI")
    path = Path(raw_path)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise BinaryConfigError(f"{CODEX_PATH_ENV} must be an absolute path without traversal")
    return PinnedBinaries(path, CODEX_MODEL, CODEX_CLI_VERSION, pins.CODEX_ARTIFACT_SHA256)


__all__ = [
    "CODEX_CLI_VERSION",
    "CODEX_MODEL",
    "CODEX_PATH_ENV",
    "BinaryConfigError",
    "PinnedBinaries",
    "PinnedObserver",
    "load_pinned_binaries",
    "resolve_pinned_observer",
]
