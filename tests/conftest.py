"""Shared test-run safety guards and hermetic test-environment setup."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from crowley.core.game_target import GameTarget
    from crowley.core.watchlist import Catalog


class _CatalogLoader(Protocol):
    def __call__(self, *, enforce_target: bool = True) -> Catalog: ...


_REAL_SELECTED_TARGET: Callable[[], GameTarget] | None = None
_REAL_LOAD_CATALOG: _CatalogLoader | None = None
_FIXTURE_CATALOG: Catalog | None = None


@lru_cache(maxsize=1)
def _fixture_target() -> GameTarget:
    from crowley.core.game_target import load_game_target

    return load_game_target(Path(__file__).parent / "fixtures" / "targets" / "verified-beta.json")


def _fixture_load_catalog(*, enforce_target: bool = True) -> Catalog:
    del enforce_target
    if _FIXTURE_CATALOG is None:
        raise RuntimeError("test target pin was not initialized before catalog loading")
    return _FIXTURE_CATALOG


def _pin_fixture_target() -> None:
    global _FIXTURE_CATALOG, _REAL_LOAD_CATALOG, _REAL_SELECTED_TARGET

    from crowley.core import game_target, watchlist
    from crowley.core.watchlist import Catalog

    _REAL_SELECTED_TARGET = game_target.selected_target
    _REAL_LOAD_CATALOG = watchlist.load_catalog
    watchlist.load_catalog.cache_clear()
    game_target.packaged_game_target.cache_clear()
    game_target.canonical_plan.cache_clear()

    fixture_path = Path(__file__).parent / "fixtures" / "catalogs" / "verified-beta.json"
    _FIXTURE_CATALOG = Catalog.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    _fixture_target().validate_catalog(_FIXTURE_CATALOG)
    game_target.selected_target = _fixture_target
    game_target.packaged_game_target = _fixture_target
    watchlist.load_catalog = _fixture_load_catalog


@pytest.fixture(scope="session")
def packaged_selection_target() -> GameTarget:
    """Return the real target named by the packaged selection file."""

    if _REAL_SELECTED_TARGET is None:
        raise RuntimeError("test target pin was not initialized before fixture use")
    return _REAL_SELECTED_TARGET()


@pytest.fixture(scope="session")
def packaged_catalog() -> Catalog:
    """Return the real packaged catalog for selection-binding tests."""

    if _REAL_LOAD_CATALOG is None:
        raise RuntimeError("test target pin was not initialized before fixture use")
    return _REAL_LOAD_CATALOG(enforce_target=False)


# This value is intentionally preserved: the DB URL controls the xdist guard.
_PRESERVED_ENVIRONMENT = frozenset({"CROWLEY_TEST_DATABASE_URL"})
_original_environment: dict[str, str] | None = None
_temporary_roots: tuple[tempfile.TemporaryDirectory[str], tempfile.TemporaryDirectory[str]] | None = None
_removed_environment_names: tuple[str, ...] = ()


def _create_private_root(prefix: str) -> tempfile.TemporaryDirectory[str]:
    temporary_directory = tempfile.TemporaryDirectory(prefix=prefix)
    Path(temporary_directory.name).chmod(0o700)
    return temporary_directory


def _install_hermetic_environment() -> None:
    global _original_environment, _removed_environment_names, _temporary_roots

    if _original_environment is not None:
        return

    _original_environment = dict(os.environ)
    _removed_environment_names = tuple(
        sorted(
            name for name in os.environ if name.startswith("CROWLEY_") and name not in _PRESERVED_ENVIRONMENT
        )
    )
    for name in _removed_environment_names:
        del os.environ[name]

    config_root = _create_private_root("crowley-tests-config-")
    state_root = _create_private_root("crowley-tests-state-")
    _temporary_roots = (config_root, state_root)
    os.environ["XDG_CONFIG_HOME"] = config_root.name
    os.environ["XDG_STATE_HOME"] = state_root.name


def _assert_hermetic_environment() -> None:
    if _temporary_roots is None:
        raise RuntimeError("hermetic test environment was not initialized before test collection")
    if os.environ.get("XDG_CONFIG_HOME") != _temporary_roots[0].name:
        raise RuntimeError("XDG_CONFIG_HOME changed after hermetic test environment initialization")
    if os.environ.get("XDG_STATE_HOME") != _temporary_roots[1].name:
        raise RuntimeError("XDG_STATE_HOME changed after hermetic test environment initialization")


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment() -> Iterator[None]:
    """Keep the pre-collection environment setup active for the whole test session."""

    _assert_hermetic_environment()
    yield


def pytest_report_header(config: pytest.Config) -> str:
    """Report cleared environment names without exposing their values."""

    del config
    removed = ", ".join(_removed_environment_names) or "<none>"
    return (
        "crowley hermetic env: removed CROWLEY_* names: "
        f"{removed}; preserved {', '.join(sorted(_PRESERVED_ENVIRONMENT))} for required test infrastructure"
    )


def pytest_configure(config: pytest.Config) -> None:
    """Install hermetic roots before collection and guard the shared DB fixture."""

    _install_hermetic_environment()
    _pin_fixture_target()

    database_url = os.environ.get("CROWLEY_TEST_DATABASE_URL")
    workers = getattr(config.option, "numprocesses", None)
    if not database_url or workers in (None, 0, "0"):
        return
    raise pytest.UsageError(
        "CROWLEY_TEST_DATABASE_URL uses schema-reset fixtures and cannot run with xdist; "
        "rerun the database tests with -n0 (see docs/TESTING.md)"
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore the caller's environment after pytest has finished."""

    del config
    global _original_environment, _temporary_roots

    if _temporary_roots is not None:
        for temporary_root in _temporary_roots:
            temporary_root.cleanup()
        _temporary_roots = None

    if _original_environment is not None:
        os.environ.clear()
        os.environ.update(_original_environment)
        _original_environment = None
