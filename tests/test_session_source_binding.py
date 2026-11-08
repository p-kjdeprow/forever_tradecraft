from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import crowley.session.source_binding as source_binding
from crowley.platform import privfs


def test_declared_safety_closure_is_exact() -> None:
    assert source_binding.compute_safety_closure() == source_binding.SAFETY_MODULES
    binding = source_binding.build_source_binding()
    resource_names = {
        name.removeprefix("resource:") for name in binding.manifest if name.startswith("resource:")
    }
    assert set(binding.manifest) == source_binding.SAFETY_MODULES | {
        f"resource:{name}" for name in resource_names
    }
    assert source_binding.RESOURCE_BOUNDARY_FILES <= resource_names
    selected = (source_binding._read_resource("targets/selected")).decode("ascii").strip()
    assert f"resource:targets/{selected}.json" in binding.manifest
    assert f"resource:routes/{selected}/stormwind.json" in binding.manifest
    assert binding.digest == source_binding.manifest_digest(binding.manifest)


def test_mutating_each_bound_module_changes_manifest_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = source_binding.build_source_binding()
    original_read = source_binding.privfs.read_bounded

    for module_name in sorted(source_binding.SAFETY_MODULES):
        module_path = source_binding._module_path(module_name)

        def mutated_read(
            path: str | os.PathLike[str], max_bytes: int, *, target: Path = module_path
        ) -> bytes:
            data = original_read(path, max_bytes)
            if Path(path) == target:
                return data + b"\n# manifest mutation\n"
            return data

        monkeypatch.setattr(source_binding.privfs, "read_bounded", mutated_read)
        mutated = source_binding.build_source_binding()
        assert mutated.manifest[module_name] != baseline.manifest[module_name]
        assert mutated.digest != baseline.digest
        monkeypatch.setattr(source_binding.privfs, "read_bounded", original_read)


def test_mutating_session_package_initializer_changes_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = source_binding.build_source_binding()
    initializer = source_binding._module_path("crowley.session")
    assert initializer.name == "__init__.py"
    original_read = source_binding.privfs.read_bounded

    def mutated_read(path: str | os.PathLike[str], max_bytes: int) -> bytes:
        data = original_read(path, max_bytes)
        if Path(path) == initializer:
            return data + b"\n# package initializer mutation\n"
        return data

    monkeypatch.setattr(source_binding.privfs, "read_bounded", mutated_read)
    mutated = source_binding.build_source_binding()
    assert mutated.manifest["crowley.session"] != baseline.manifest["crowley.session"]
    assert mutated.digest != baseline.digest


def test_mutating_each_bound_resource_changes_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = source_binding.build_source_binding()
    resource_names = sorted(
        name.removeprefix("resource:") for name in baseline.manifest if name.startswith("resource:")
    )
    original_read = source_binding._read_resource
    monkeypatch.setattr(source_binding, "_resource_names", lambda: frozenset(resource_names))
    for resource_name in resource_names:

        def mutated_read(name: str, *, target: str = resource_name) -> bytes:
            data = original_read(name)
            return data + b"\n# resource binding mutation\n" if name == target else data

        monkeypatch.setattr(source_binding, "_read_resource", mutated_read)
        mutated = source_binding.build_source_binding()
        key = f"resource:{resource_name}"
        assert mutated.manifest[key] != baseline.manifest[key]
        assert mutated.digest != baseline.digest
        monkeypatch.setattr(source_binding, "_read_resource", original_read)


def test_missing_bound_module_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = "crowley.session.driver"
    original_path = source_binding._module_path

    def missing_path(module_name: str) -> Path:
        if module_name == missing:
            raise source_binding.SourceBindingError(f"bound module {module_name} source file is missing")
        return original_path(module_name)

    monkeypatch.setattr(source_binding, "_module_path", missing_path)
    with pytest.raises(source_binding.SourceBindingError, match="driver.*missing"):
        source_binding.build_source_binding()


def test_non_editable_style_package_location_builds_binding(tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / "crowley", package_root / "crowley")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from crowley.session.source_binding import build_source_binding; "
            "binding = build_source_binding(); print(len(binding.manifest))",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(len(source_binding.SAFETY_MODULES) + 4)


def test_fresh_cli_process_has_no_unbound_crowley_source_modules(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path

import crowley.session.cli

def crowley_sources():
    loaded = set()
    for name, module in sys.modules.items():
        if name != "crowley" and not name.startswith("crowley."):
            continue
        try:
            source = module.__file__
        except AttributeError:
            continue
        if isinstance(source, str) and source.endswith(".py") and Path(source).is_file():
            loaded.add(name)
    return loaded

loaded_after_cli = crowley_sources()
from crowley.session.source_binding import build_source_binding

manifest = build_source_binding().manifest
unbound = sorted(crowley_sources() - set(manifest))
if unbound:
    raise SystemExit(f"unbound Crowley source modules: {unbound!r}")
if "crowley.data" not in loaded_after_cli:
    raise SystemExit("crowley.data was not loaded by the session CLI entry module")
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_file_swapped_during_read_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.py"
    replacement = tmp_path / "replacement.py"
    source.write_bytes(b"value = 1\n")
    replacement.write_bytes(b"value = 2\n")
    monkeypatch.setattr(source_binding, "_module_path", lambda _module: source)
    original_read = privfs.os.read
    swapped = False

    def swapping_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        data = original_read(descriptor, count)
        if not swapped:
            source.unlink()
            replacement.rename(source)
            swapped = True
        return data

    monkeypatch.setattr(privfs.os, "read", swapping_read)
    with pytest.raises(source_binding.SourceBindingError, match="source cannot be read"):
        source_binding.build_source_binding()
