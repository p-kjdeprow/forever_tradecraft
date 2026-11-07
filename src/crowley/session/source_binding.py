"""Package-derived source bindings for the session safety closure.

The closure starts at the session safety roots, follows every statically
declared import whose module belongs to the installed ``crowley`` package,
and includes each package initializer on those import paths.  This boundary
deliberately includes non-session Crowley modules such as ``crowley.core`` and
``crowley.platform`` because their source executes as part of the safety
runtime.  The explicitly listed ``crowley.data`` package is a resource
loader boundary: ``importlib.resources.files`` imports it by package name
without a Python import edge in the caller.  Third-party dependencies and
non-source resource files are outside the boundary.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from crowley.platform import privfs

_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_SHA256_LENGTH = 64

SAFETY_ROOT_MODULES = frozenset(
    {
        "crowley.session.daemon",
        "crowley.session.driver",
        "crowley.session.cli",
        "crowley.session.decoder",
        "crowley.session.protocol",
        "crowley.session.guardian",
        "crowley.session.input_broker",
        "crowley.session.pointer_execution",
        "crowley.session.input_events",
        "crowley.session.input_tool",
        "crowley.session.startup",
        "crowley.session.executor",
        "crowley.session.calibration",
        "crowley.session.controller",
        "crowley.session.journal",
        "crowley.session.evidence",
    }
)

# Some packaged resources are imported by name at runtime rather than through
# a Python import statement.  Bind those package initializers explicitly.
RESOURCE_BOUNDARY_MODULES = frozenset({"crowley.data"})

# This is the reviewed Crowley-package safety closure.  It must be updated
# together with code that changes the closure; _assert_declared_closure makes
# an omission or an accidental addition fail at manifest construction time.
SAFETY_MODULES = frozenset(
    {
        "crowley",
        "crowley.core",
        "crowley.core.auction_rules",
        "crowley.core.companion_state",
        "crowley.core.game_target",
        "crowley.core.house",
        "crowley.core.savedvars",
        "crowley.core.scanformat",
        "crowley.core.watchlist",
        "crowley.data",
        "crowley.platform",
        "crowley.platform.privfs",
        "crowley.session",
        "crowley.session.calibration",
        "crowley.session.cli",
        "crowley.session.config",
        "crowley.session.controller",
        "crowley.session.daemon",
        "crowley.session.decoder",
        "crowley.session.driver",
        "crowley.session.evidence",
        "crowley.session.executor",
        "crowley.session.guardian",
        "crowley.session.input_broker",
        "crowley.session.input_events",
        "crowley.session.input_tool",
        "crowley.session.journal",
        "crowley.session.mail_loop",
        "crowley.session.model",
        "crowley.session.motion",
        "crowley.session.pointer_execution",
        "crowley.session.preferences",
        "crowley.session.process_admission",
        "crowley.session.protocol",
        "crowley.session.route_walk",
        "crowley.session.source_binding",
        "crowley.session.startup",
    }
)


class SourceBindingError(RuntimeError):
    """A source closure or its bytes cannot be trusted as runtime evidence."""


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """The canonical source manifest and its digest."""

    manifest: Mapping[str, str]
    digest: str

    def __post_init__(self) -> None:
        normalized = dict(self.manifest)
        _validate_manifest(normalized)
        expected = manifest_digest(normalized)
        if self.digest != expected:
            raise SourceBindingError("source binding digest does not match its canonical manifest")
        object.__setattr__(self, "manifest", MappingProxyType(normalized))


def _validate_manifest(manifest: Mapping[str, str]) -> None:
    if not manifest:
        raise SourceBindingError("source binding manifest is empty")
    for module_name, digest in manifest.items():
        if not isinstance(module_name, str) or (
            module_name != "crowley" and not module_name.startswith("crowley.")
        ):
            raise SourceBindingError("source binding manifest contains an invalid module name")
        if not isinstance(digest, str) or len(digest) != _SHA256_LENGTH:
            raise SourceBindingError(f"source binding digest for {module_name} is malformed")
        if any(character not in "0123456789abcdef" for character in digest):
            raise SourceBindingError(f"source binding digest for {module_name} is malformed")


def canonical_manifest(manifest: Mapping[str, str]) -> dict[str, str]:
    """Return a normalized copy of a source manifest."""

    normalized = dict(manifest)
    _validate_manifest(normalized)
    return dict(sorted(normalized.items()))


def canonical_manifest_bytes(manifest: Mapping[str, str]) -> bytes:
    """Serialize a source manifest in the one accepted canonical form."""

    normalized = canonical_manifest(manifest)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def manifest_digest(manifest: Mapping[str, str]) -> str:
    """Return the SHA-256 of the canonical source manifest bytes."""

    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def _module_path(module_name: str) -> Path:
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise SourceBindingError(f"bound module {module_name} cannot be imported") from exc
    source_name = module.__file__
    if source_name is None:
        raise SourceBindingError(f"bound module {module_name} has no source file")
    path = Path(source_name)
    if not path.is_absolute() or path.suffix != ".py":
        raise SourceBindingError(f"bound module {module_name} does not have a source file")
    return path


def _read_module(module_name: str) -> tuple[Path, bytes]:
    path = _module_path(module_name)
    try:
        source = privfs.read_bounded(path, _MAX_SOURCE_BYTES)
    except privfs.PrivateFileError as exc:
        raise SourceBindingError(f"bound module {module_name} source cannot be read") from exc
    return path, source


def _relative_import_base(module_name: str, level: int, *, is_package: bool) -> str:
    parts = module_name.split(".")
    package_parts = parts if is_package else parts[:-1]
    if level > len(package_parts):
        raise SourceBindingError(f"bound module {module_name} has an invalid relative import")
    return ".".join(package_parts[: len(package_parts) - level + 1])


def _is_crowley_module(module_name: str) -> bool:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError):
        return False
    return spec is not None and spec.origin is not None and spec.origin.endswith(".py")


def _crowley_imports(module_name: str, source: bytes, *, is_package: bool) -> frozenset[str]:
    try:
        tree = ast.parse(source.decode("utf-8"), filename=module_name)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise SourceBindingError(f"bound module {module_name} is not valid UTF-8 Python") from exc
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name
                for alias in node.names
                if alias.name == "crowley" or alias.name.startswith("crowley.")
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = _relative_import_base(module_name, node.level, is_package=is_package)
            target = f"{base}.{node.module}" if node.module else base
        else:
            target = node.module or ""
        if target != "crowley" and not target.startswith("crowley."):
            continue
        if _is_crowley_module(target):
            imported.add(target)
        imported.update(
            candidate
            for candidate in (f"{target}.{alias.name}" for alias in node.names)
            if _is_crowley_module(candidate)
        )
    return frozenset(imported)


def _package_initializers(module_name: str) -> frozenset[str]:
    parts = module_name.split(".")
    return frozenset(".".join(parts[:index]) for index in range(1, len(parts)))


def compute_safety_closure() -> frozenset[str]:
    """Compute the transitive local-Crowley import closure from the roots."""

    pending = list(SAFETY_ROOT_MODULES | RESOURCE_BOUNDARY_MODULES)
    discovered = set(SAFETY_ROOT_MODULES | RESOURCE_BOUNDARY_MODULES)
    while pending:
        module_name = pending.pop()
        path, source = _read_module(module_name)
        for imported in _package_initializers(module_name) | _crowley_imports(
            module_name, source, is_package=path.name == "__init__.py"
        ):
            if imported not in discovered:
                discovered.add(imported)
                pending.append(imported)
    return frozenset(discovered)


def _assert_declared_closure() -> frozenset[str]:
    computed = compute_safety_closure()
    if computed != SAFETY_MODULES:
        missing = sorted(SAFETY_MODULES - computed)
        unexpected = sorted(computed - SAFETY_MODULES)
        raise SourceBindingError(
            f"declared session safety closure drifted; missing={missing!r}, unexpected={unexpected!r}"
        )
    return computed


def build_source_binding() -> SourceBinding:
    """Read and hash the complete reviewed session safety closure."""

    modules = _assert_declared_closure()
    manifest: dict[str, str] = {}
    for module_name in sorted(modules):
        _path, source = _read_module(module_name)
        manifest[module_name] = hashlib.sha256(source).hexdigest()
    canonical = canonical_manifest(manifest)
    return SourceBinding(manifest=canonical, digest=manifest_digest(canonical))


__all__ = [
    "SAFETY_MODULES",
    "SAFETY_ROOT_MODULES",
    "RESOURCE_BOUNDARY_MODULES",
    "SourceBinding",
    "SourceBindingError",
    "build_source_binding",
    "canonical_manifest",
    "canonical_manifest_bytes",
    "compute_safety_closure",
    "manifest_digest",
]
