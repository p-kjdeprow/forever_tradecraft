from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from crowley.native import build, matcher, pins
from tools import native_calibration_operator as operator


def _test_toolchain(tmp_path: Path) -> build.Toolchain:
    directory = tmp_path / "toolchain"
    return build.Toolchain(
        directory=directory,
        rustc=directory / "rustc",
        cargo=directory / "cargo",
        rustfmt=directory / "rustfmt",
        rustc_version=pins.EXPECTED_RUSTC,
        cargo_version=pins.EXPECTED_CARGO,
        rustfmt_version=pins.EXPECTED_RUSTFMT,
        rustc_verbose=pins.EXPECTED_RUSTC,
    )


def test_native_workspace_has_one_packaged_source_tree() -> None:
    repository = Path(__file__).resolve().parents[2]
    packaged = build.NATIVE_ROOT
    assert packaged == repository / "src" / "crowley" / "native" / "resources" / "native"
    assert packaged.is_dir()
    assert not repository.joinpath("native").exists()
    actual_files = {path.relative_to(packaged).as_posix() for path in packaged.rglob("*") if path.is_file()}
    assert actual_files == set(pins.NATIVE_SOURCE_FILES)


def test_xdg_cache_home_redirects_native_artifact_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    cache = tmp_path / "cache"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    expected = cache / "crowley"
    assert matcher.default_artifact_root() == expected / "template-matcher" / "v1"
    assert build.default_observer_artifact_path() == expected / "x11-identity-observer" / "v1" / "observer"
    assert build.default_xdotool_artifact_root() == expected / "xdotool"


def test_required_toolchain_fails_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pins.RUST_TOOLCHAIN_ENV, raising=False)

    with pytest.raises(build.BuildError, match="CROWLEY_RUST_TOOLCHAIN is required"):
        build.required_toolchain()


def test_required_toolchain_rejects_relative_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pins.RUST_TOOLCHAIN_ENV, "relative/toolchain/bin")

    with pytest.raises(build.BuildError, match="must name an absolute"):
        build.required_toolchain()


def test_verify_requires_every_workspace_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / "native"
    source_root.mkdir(mode=0o700)
    for relative in pins.NATIVE_SOURCE_FILES[:-1]:
        path = source_root / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(b"source")
    toolchain_dir = tmp_path / "toolchain"
    toolchain = build.Toolchain(
        directory=toolchain_dir,
        rustc=toolchain_dir / "rustc",
        cargo=toolchain_dir / "cargo",
        rustfmt=toolchain_dir / "rustfmt",
        rustc_version=pins.EXPECTED_RUSTC,
        cargo_version=pins.EXPECTED_CARGO,
        rustfmt_version=pins.EXPECTED_RUSTFMT,
        rustc_verbose=pins.EXPECTED_RUSTC,
    )
    monkeypatch.setattr(build, "required_toolchain", lambda: toolchain)

    with pytest.raises(build.BuildError, match="required path is unavailable"):
        build.verify(source_root)


def test_verify_rejects_stale_workspace_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / "native"
    shutil.copytree(build.NATIVE_ROOT, source_root)
    source_root.joinpath("Cargo.lock").write_bytes(b"stale workspace lock")
    monkeypatch.setattr(build, "required_toolchain", lambda: _test_toolchain(tmp_path))

    with pytest.raises(build.BuildError, match="native workspace source pin is stale: Cargo.lock"):
        build.verify(source_root)


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("template_matcher/src/main.rs", "matcher source pin is stale"),
        ("x11_identity_observer/src/main.rs", "observer source pin is stale"),
    ],
)
def test_verify_rejects_stale_helper_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    message: str,
) -> None:
    source_root = tmp_path / "native"
    shutil.copytree(build.NATIVE_ROOT, source_root)
    path = source_root / relative
    path.write_bytes(path.read_bytes() + b"stale")
    monkeypatch.setattr(build, "required_toolchain", lambda: _test_toolchain(tmp_path))

    with pytest.raises(build.BuildError, match=message):
        build.verify(source_root)


def test_verify_rejects_stale_vendored_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / "native"
    shutil.copytree(build.NATIVE_ROOT, source_root)
    archive = tmp_path / "source.tar.gz"
    shutil.copyfile(build.VENDORED_SOURCE_ARCHIVE, archive)
    archive.write_bytes(archive.read_bytes() + b"stale")
    monkeypatch.setattr(build, "VENDORED_SOURCE_ARCHIVE", archive)
    monkeypatch.setattr(build, "required_toolchain", lambda: _test_toolchain(tmp_path))

    with pytest.raises(build.BuildError, match="vendored xdotool archive pin is stale"):
        build.verify(source_root)


def test_matcher_source_hash_rejects_same_size_path_swap_after_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"reviewed source\n")
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(source.read_bytes())
    original_read = matcher._read_descriptor_stable

    def read_then_swap(descriptor: int) -> bytes:
        data = original_read(descriptor)
        source.unlink()
        replacement.rename(source)
        return data

    monkeypatch.setattr(matcher, "_read_descriptor_stable", read_then_swap)
    with pytest.raises(matcher.ArtifactError, match="path changed"):
        matcher._sha256_path(source)


def test_observer_builder_manifest_is_accepted_by_production_verifier(
    tmp_path: Path,
) -> None:
    observer = tmp_path / "observer"
    build.build_observer(observer)
    digest = hashlib.sha256(observer.read_bytes()).hexdigest()
    screen = type("Screen", (), {"backend": type("Backend", (), {"resolver_kind": "pinned-rust-v1"})()})()
    bindings = operator._verify_observer_manifest(screen, observer, digest)
    assert bindings["identity-observer-binary"] == digest


def test_observer_manifest_is_independent_of_installation_paths(tmp_path: Path) -> None:
    artifact_a = tmp_path / "one" / "observer"
    artifact_b = tmp_path / "two" / "observer"
    artifact_a.parent.mkdir(mode=0o700)
    artifact_b.parent.mkdir(mode=0o700)
    artifact_a.write_bytes(b"observer")
    artifact_b.write_bytes(b"observer")
    static_inputs = {f"lib{name}.a": "a" * 64 for name in pins.OBSERVER_STATIC_LIBRARY_NAMES}
    linker = {
        "driver": {"name": "cc", "sha256": "b" * 64, "version": "cc test"},
        "ld": {"name": "ld", "sha256": "c" * 64, "version": "ld test"},
    }
    observer_source_hashes = {
        name.removeprefix("native/"): digest for name, digest in pins.OBSERVER_SOURCE_HASHES.items()
    }

    first = build._observer_manifest(
        observer_source_hashes,
        artifact_a,
        _test_toolchain(tmp_path / "first"),
        pins.OBSERVER_WORKSPACE_SHA256,
        ("/first/lib",),
        static_inputs,
        linker,
    )
    second = build._observer_manifest(
        observer_source_hashes,
        artifact_b,
        _test_toolchain(tmp_path / "second"),
        pins.OBSERVER_WORKSPACE_SHA256,
        ("/second/lib",),
        static_inputs,
        linker,
    )

    assert first == second
    assert "/first" not in str(first)
    assert "/second" not in str(second)


def test_xdotool_builder_rejects_different_isolated_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def build_once(_archive: Path, _work: Path) -> tuple[bytes, dict[str, build.JSONValue]]:
        nonlocal calls
        calls += 1
        return b"same bytes", {
            "compiler": {"name": "cc", "sha256": "a" * 64, "version": f"compiler-{calls}"},
            "linker": {"name": "ld", "sha256": "b" * 64, "version": "linker"},
        }

    monkeypatch.setattr(build, "_build_xdotool_once", build_once)
    with pytest.raises(build.BuildError, match="compiler or linker inputs changed"):
        build.build_xdotool(build.VENDORED_SOURCE_ARCHIVE, tmp_path / "xdotool")
    assert calls == 2


def test_clean_wheel_native_verify_uses_packaged_resources(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheel"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheel_dir)], cwd=repository, check=True)
    wheel = next(wheel_dir.glob("crowley-*.whl"))
    virtualenv = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(virtualenv)], cwd=tmp_path, check=True)
    python = virtualenv / "bin" / "python"
    subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=tmp_path, check=True)
    outside = tmp_path / "outside-checkout"
    outside.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(virtualenv / "bin" / "crowley-native"), "verify"],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f'"workspace":"{build.WORKSPACE_RESOURCE_ID}"' in result.stdout
