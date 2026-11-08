from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.native import build, matcher, pins
from crowley.native import publication as publication_module
from crowley.session import input_tool
from tools import native_calibration_operator as operator


def _simple_publication(root: Path, artifact: bytes, manifest: bytes) -> build._Publication:
    return build._Publication(
        kind="test-helper",
        root=root,
        binary_name="artifact",
        files=(
            build._PublicationFile("artifact", artifact, 0o700),
            build._PublicationFile("manifest.json", manifest, 0o600),
        ),
        verify_staged=lambda staged: _verify_simple_publication(staged, artifact, manifest),
        verify_existing=_verify_simple_shape,
    )


def _verify_simple_publication(root: Path, artifact: bytes, manifest: bytes) -> None:
    if (root / "artifact").read_bytes() != artifact or (root / "manifest.json").read_bytes() != manifest:
        raise build.BuildError("test publication bytes differ")


def _verify_simple_shape(root: Path) -> None:
    if not (root / "artifact").is_file() or not (root / "manifest.json").is_file():
        raise build.BuildError("test publication files are missing")


def _publication_with_files(
    root: Path,
    *,
    kind: str,
    binary_name: str,
    files: tuple[build._PublicationFile, ...],
) -> build._Publication:
    return build._Publication(
        kind=kind,
        root=root,
        binary_name=binary_name,
        files=files,
        verify_staged=lambda _staged: None,
        verify_existing=lambda _existing: None,
    )


def _release_digest(binary: bytes) -> str:
    return hashlib.sha256(binary).hexdigest()


def _run_reader_switches(
    publication: build._Publication,
    release_digests: tuple[str, str],
    read_once: Callable[[str], str],
    *,
    read_after_switch: bool = False,
) -> dict[str, int]:
    iterations = 64
    ready = threading.Barrier(2)
    switched = threading.Barrier(2)
    finished = threading.Barrier(2)
    observations = {release_digests[0]: 0, release_digests[1]: 0}
    errors: list[BaseException] = []

    def switch_releases() -> None:
        try:
            for index in range(iterations):
                ready.wait(timeout=5.0)
                digest = release_digests[index % 2]
                build._switch_current_pointer(publication, publication.root / "releases" / digest)
                switched.wait(timeout=5.0)
                finished.wait(timeout=5.0)
        except BaseException as error:
            errors.append(error)

    def read_releases() -> None:
        try:
            for index in range(iterations):
                ready.wait(timeout=5.0)
                digest = release_digests[index % 2]
                if read_after_switch:
                    switched.wait(timeout=5.0)
                observed = read_once(digest)
                if observed not in observations:
                    raise AssertionError(f"reader observed an incomplete release: {observed}")
                observations[observed] += 1
                if not read_after_switch:
                    switched.wait(timeout=5.0)
                finished.wait(timeout=5.0)
        except BaseException as error:
            errors.append(error)

    switcher = threading.Thread(target=switch_releases)
    reader = threading.Thread(target=read_releases)
    switcher.start()
    reader.start()
    switcher.join(timeout=15.0)
    reader.join(timeout=15.0)
    assert not switcher.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert observations[release_digests[0]] >= iterations // 4
    assert observations[release_digests[1]] >= iterations // 4
    return observations


def _run_operator_reader_switches(
    publication_to_switch: build._Publication,
    release_digests: tuple[str, str],
    manifest_name: str,
    read_once: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    iterations = 64
    seam = threading.Barrier(2)
    observations = {release_digests[0]: 0, release_digests[1]: 0}
    errors: list[BaseException] = []
    original_open_and_read_at = publication_module._open_and_read_at

    def pause_before_manifest_read(parent: int, name: str, *, maximum: int, label: str) -> bytes:
        if name == manifest_name:
            seam.wait(timeout=5.0)
            seam.wait(timeout=5.0)
        return original_open_and_read_at(parent, name, maximum=maximum, label=label)

    monkeypatch.setattr(publication_module, "_open_and_read_at", pause_before_manifest_read)

    def switch_releases() -> None:
        try:
            for index in range(iterations):
                current_index = 1 if index % 2 == 0 else 0
                next_index = 1 - current_index
                seam.wait(timeout=5.0)
                build._switch_current_pointer(
                    publication_to_switch,
                    publication_to_switch.root / "releases" / release_digests[next_index],
                )
                seam.wait(timeout=5.0)
        except BaseException as error:
            errors.append(error)

    def read_releases() -> None:
        try:
            for index in range(iterations):
                current_index = 1 if index % 2 == 0 else 0
                observed = read_once(release_digests[current_index])
                if observed not in observations:
                    raise AssertionError(f"operator observed an incomplete release: {observed}")
                observations[observed] += 1
        except BaseException as error:
            errors.append(error)

    switcher = threading.Thread(target=switch_releases)
    reader = threading.Thread(target=read_releases)
    switcher.start()
    reader.start()
    switcher.join(timeout=15.0)
    reader.join(timeout=15.0)
    assert not switcher.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert observations[release_digests[0]] >= iterations // 4
    assert observations[release_digests[1]] >= iterations // 4
    return observations


def _hold_publication_lock(parent_name: str, ready: Connection, release: Connection) -> None:
    with build._publication_lock(Path(parent_name)):
        ready.send(True)
        release.recv()


def _attempt_publication_lock(parent_name: str, acquired: Connection) -> None:
    with build._publication_lock(Path(parent_name)):
        acquired.send(True)


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
    observer = tmp_path / pins.OBSERVER_ARTIFACT_NAME
    build.build_observer(observer)
    observer_path = tmp_path / "current" / pins.OBSERVER_ARTIFACT_NAME
    digest = hashlib.sha256(observer_path.read_bytes()).hexdigest()
    screen = type("Screen", (), {"backend": type("Backend", (), {"resolver_kind": "pinned-rust-v1"})()})()
    bindings = operator._verify_observer_manifest(screen, observer_path, digest)
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


def test_publication_failure_after_binary_staging_leaves_destination_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "publication"
    old_artifact = b"old artifact"
    old_manifest = b"old manifest"
    build._publish_publication(_simple_publication(destination, old_artifact, old_manifest), replace=False)
    before = (
        (destination / "current" / "artifact").read_bytes(),
        (destination / "current" / "manifest.json").read_bytes(),
    )
    original_write = build.privfs.atomic_write_private

    def fail_manifest(path: str | os.PathLike[str], data: bytes) -> None:
        if Path(path).name == "manifest.json":
            raise build.privfs.PrivateWriteError("injected after binary staging")
        original_write(path, data)

    monkeypatch.setattr(build.privfs, "atomic_write_private", fail_manifest)
    publication = _simple_publication(destination, b"new artifact", b"new manifest")
    with pytest.raises(build.BuildError, match="injected after binary staging"):
        build._publish_publication(publication, replace=True)

    assert (
        (destination / "current" / "artifact").read_bytes(),
        (destination / "current" / "manifest.json").read_bytes(),
    ) == before


def test_differing_publication_refuses_without_replace_and_main_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "publication"
    first = _simple_publication(destination, b"first", b"first manifest")
    build._publish_publication(first, replace=False)
    second = _simple_publication(destination, b"second", b"second manifest")

    with pytest.raises(build.BuildError, match="pass --replace"):
        build._publish_publication(second, replace=False)
    assert (destination / "current" / "artifact").read_bytes() == b"first"

    def refuse(_output: Path | None = None, *, replace: bool = False) -> dict[str, build.JSONValue]:
        del replace
        raise build.BuildError("existing matcher publication differs; pass --replace to replace it")

    monkeypatch.setattr(build, "build_matcher", refuse)
    result = build.main(["build", "matcher", "--output-dir", str(destination)])
    assert result == 1
    assert "pass --replace" in capsys.readouterr().err


def test_replace_keeps_dated_backup_and_publishes_the_new_set(tmp_path: Path) -> None:
    destination = tmp_path / "publication"
    first = _simple_publication(destination, b"first", b"first manifest")
    build._publish_publication(first, replace=False)
    second = _simple_publication(destination, b"second", b"second manifest")
    build._publish_publication(second, replace=True)

    assert (destination / "current" / "artifact").read_bytes() == b"second"
    assert (destination / "current" / "manifest.json").read_bytes() == b"second manifest"
    backups = sorted(tmp_path.glob("publication.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "artifact").read_bytes() == b"first"
    assert (backups[0] / "manifest.json").read_bytes() == b"first manifest"


def test_replacements_keep_content_addressed_releases_across_rollback(tmp_path: Path) -> None:
    destination = tmp_path / "publication"
    publications = (
        _simple_publication(destination, b"first", b"first manifest"),
        _simple_publication(destination, b"second", b"second manifest"),
        _simple_publication(destination, b"first", b"first manifest"),
        _simple_publication(destination, b"second", b"second manifest"),
    )
    build._publish_publication(publications[0], replace=False)
    for publication in publications[1:]:
        build._publish_publication(publication, replace=True)

    first_digest = hashlib.sha256(b"first").hexdigest()
    second_digest = hashlib.sha256(b"second").hexdigest()
    assert {entry.name for entry in (destination / "releases").iterdir()} == {
        first_digest,
        second_digest,
    }
    assert (destination / "current" / "artifact").read_bytes() == b"second"
    assert (destination / "releases" / first_digest / "artifact").read_bytes() == b"first"
    assert (destination / "releases" / second_digest / "artifact").read_bytes() == b"second"


def test_runtime_readers_see_only_complete_releases_across_many_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_a = Path(sys.executable).read_bytes()
    binary_b = binary_a + b"crowley-release-b"

    matcher_root = tmp_path / "matcher"
    matcher_manifest = b"{}\n"
    matcher_a = _publication_with_files(
        matcher_root,
        kind="matcher-test",
        binary_name=pins.MATCHER_ARTIFACT_NAME,
        files=(
            build._PublicationFile(pins.MATCHER_ARTIFACT_NAME, binary_a, 0o700),
            build._PublicationFile(pins.MATCHER_MANIFEST_NAME, matcher_manifest, 0o600),
        ),
    )
    matcher_b = _publication_with_files(
        matcher_root,
        kind="matcher-test",
        binary_name=pins.MATCHER_ARTIFACT_NAME,
        files=(
            build._PublicationFile(pins.MATCHER_ARTIFACT_NAME, binary_b, 0o700),
            build._PublicationFile(pins.MATCHER_MANIFEST_NAME, matcher_manifest, 0o600),
        ),
    )
    build._publish_publication(matcher_a, replace=False)
    build._publish_publication(matcher_b, replace=True)
    matcher_digests = (_release_digest(binary_a), _release_digest(binary_b))

    def accepted_matcher_manifest(_manifest: dict[str, object], _digest: str) -> dict[str, str]:
        return dict(pins.MATCHER_SOURCE_HASHES)

    monkeypatch.setattr(matcher, "_verify_manifest", accepted_matcher_manifest)

    def read_matcher(_expected_digest: str) -> str:
        runtime = matcher.TemplateMatcherRuntime(matcher_root)
        try:
            return runtime.artifact_sha256
        finally:
            runtime.close()

    matcher_observations = _run_reader_switches(matcher_a, matcher_digests, read_matcher)

    input_root = tmp_path / "input-tool"
    archive = build.VENDORED_SOURCE_ARCHIVE.read_bytes()

    def input_manifest(digest: str) -> bytes:
        return json.dumps(
            {
                "commit": pins.XDOTOOL_COMMIT,
                "archive_sha256": pins.XDOTOOL_ARCHIVE_SHA256,
                "executable": pins.XDOTOOL_ARTIFACT_NAME,
                "executable_sha256": digest,
                "build_inputs": {
                    "compiler": {"name": "cc", "sha256": "a" * 64, "version": "cc"},
                    "linker": {"name": "ld", "sha256": "b" * 64, "version": "ld"},
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    input_a = _publication_with_files(
        input_root,
        kind="input-tool-test",
        binary_name=pins.XDOTOOL_ARTIFACT_NAME,
        files=(
            build._PublicationFile(pins.XDOTOOL_ARTIFACT_NAME, binary_a, 0o700),
            build._PublicationFile(
                pins.XDOTOOL_MANIFEST_NAME, input_manifest(_release_digest(binary_a)), 0o600
            ),
            build._PublicationFile(pins.XDOTOOL_ARCHIVE_NAME, archive, 0o600),
        ),
    )
    input_b = _publication_with_files(
        input_root,
        kind="input-tool-test",
        binary_name=pins.XDOTOOL_ARTIFACT_NAME,
        files=(
            build._PublicationFile(pins.XDOTOOL_ARTIFACT_NAME, binary_b, 0o700),
            build._PublicationFile(
                pins.XDOTOOL_MANIFEST_NAME, input_manifest(_release_digest(binary_b)), 0o600
            ),
            build._PublicationFile(pins.XDOTOOL_ARCHIVE_NAME, archive, 0o600),
        ),
    )
    build._publish_publication(input_a, replace=False)
    build._publish_publication(input_b, replace=True)
    input_digests = (_release_digest(binary_a), _release_digest(binary_b))

    def read_input(_expected_digest: str) -> str:
        return input_tool._verify_artifact(input_root).identity.executable_sha256

    input_observations = _run_reader_switches(input_a, input_digests, read_input)

    observer_root = tmp_path / "observer"
    observer_a_path = tmp_path / "observer-a"
    observer_b_path = tmp_path / "observer-b"
    observer_a_path.write_bytes(binary_a)
    observer_b_path.write_bytes(binary_b)
    observer_a_path.chmod(0o700)
    observer_b_path.chmod(0o700)
    observer_source_hashes = {
        name.removeprefix("native/"): digest for name, digest in pins.OBSERVER_SOURCE_HASHES.items()
    }
    static_inputs = {f"lib{name}.a": "a" * 64 for name in pins.OBSERVER_STATIC_LIBRARY_NAMES}
    linker = {
        "driver": {"name": "cc", "sha256": "b" * 64, "version": "cc test"},
        "ld": {"name": "ld", "sha256": "c" * 64, "version": "ld test"},
    }
    toolchain = _test_toolchain(tmp_path / "observer-toolchain")
    observer_manifest_a = build._observer_manifest(
        observer_source_hashes,
        observer_a_path,
        toolchain,
        pins.OBSERVER_WORKSPACE_SHA256,
        (),
        static_inputs,
        linker,
    )
    observer_manifest_b = build._observer_manifest(
        observer_source_hashes,
        observer_b_path,
        toolchain,
        pins.OBSERVER_WORKSPACE_SHA256,
        (),
        static_inputs,
        linker,
    )
    observer_name = pins.OBSERVER_ARTIFACT_NAME
    observer_manifest_name = f"{observer_name}.manifest.json"

    def observer_files(binary: bytes, manifest: dict[str, object]) -> tuple[build._PublicationFile, ...]:
        return (
            build._PublicationFile(observer_name, binary, 0o700),
            build._PublicationFile(
                observer_manifest_name,
                (json.dumps(manifest, sort_keys=True) + "\n").encode(),
                0o600,
            ),
        )

    observer_a = _publication_with_files(
        observer_root,
        kind="observer-test",
        binary_name=observer_name,
        files=observer_files(binary_a, observer_manifest_a),
    )
    observer_b = _publication_with_files(
        observer_root,
        kind="observer-test",
        binary_name=observer_name,
        files=observer_files(binary_b, observer_manifest_b),
    )
    build._publish_publication(observer_a, replace=False)
    build._publish_publication(observer_b, replace=True)
    observer_digests = (_release_digest(binary_a), _release_digest(binary_b))
    screen = SimpleNamespace(backend=SimpleNamespace(resolver_kind="pinned-rust-v1"))
    observer_path = observer_root / "current" / observer_name

    def read_observer(expected_digest: str) -> str:
        bindings = operator._verify_observer_manifest(screen, observer_path, expected_digest)
        return bindings["identity-observer-binary"]

    observer_observations = _run_operator_reader_switches(
        observer_a,
        observer_digests,
        observer_manifest_name,
        read_observer,
        monkeypatch,
    )

    assert all(count >= 16 for count in matcher_observations.values())
    assert all(count >= 16 for count in input_observations.values())
    assert all(count >= 16 for count in observer_observations.values())


def test_cli_refuses_unsafe_output_directory_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o755)
    unsafe_parent.chmod(0o755)

    result = build.main(["build", "matcher", "--output-dir", str(unsafe_parent / "publication")])

    assert result == 1
    error = capsys.readouterr().err
    assert "private directory must have mode 0700" in error
    assert "Traceback" not in error


def test_matcher_existing_manifest_requires_canonical_fields(tmp_path: Path) -> None:
    artifact = b"matcher artifact"
    root = tmp_path / "matcher"
    root.mkdir(mode=0o700)
    artifact_path = root / pins.MATCHER_ARTIFACT_NAME
    artifact_path.write_bytes(artifact)
    artifact_path.chmod(0o700)
    source_hashes = {f"template_matcher/{name}": "a" * 64 for name in pins.MATCHER_SOURCE_FILES}
    manifest = build._manifest_for_matcher(
        source_hashes,
        hashlib.sha256(artifact).hexdigest(),
        _test_toolchain(tmp_path),
        pins.MATCHER_WORKSPACE_SHA256,
    )
    del manifest["schema"]
    manifest_path = root / pins.MATCHER_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(build.BuildError, match="fields are not canonical"):
        build._verify_matcher_publication(root, None)


def test_observer_existing_manifest_requires_canonical_fields(tmp_path: Path) -> None:
    root = tmp_path / "observer"
    root.mkdir(mode=0o700)
    artifact = root / pins.OBSERVER_ARTIFACT_NAME
    artifact.write_bytes(b"observer artifact")
    artifact.chmod(0o700)
    source_hashes = {name: "a" * 64 for name in build._OBSERVER_SOURCE.source_paths}
    static_inputs = {f"lib{name}.a": "b" * 64 for name in pins.OBSERVER_STATIC_LIBRARY_NAMES}
    linker = {
        "driver": {"name": "cc", "sha256": "c" * 64, "version": "cc test"},
        "ld": {"name": "ld", "sha256": "d" * 64, "version": "ld test"},
    }
    manifest = build._observer_manifest(
        source_hashes,
        artifact,
        _test_toolchain(tmp_path),
        pins.OBSERVER_WORKSPACE_SHA256,
        ("/lib",),
        static_inputs,
        linker,
    )
    del manifest["schema"]
    manifest_path = root / "observer.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(build.BuildError, match="fields are not canonical"):
        build._verify_observer_publication(root, pins.OBSERVER_ARTIFACT_NAME, manifest_path.name, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("build", {"profile": "release"}),
    ],
)
def test_matcher_manifest_rejects_non_exact_scalar_or_shape(
    tmp_path: Path, field: str, value: object
) -> None:
    root = tmp_path / "matcher"
    root.mkdir(mode=0o700)
    artifact = root / pins.MATCHER_ARTIFACT_NAME
    artifact.write_bytes(b"matcher artifact")
    artifact.chmod(0o700)
    source_hashes = {f"template_matcher/{name}": "a" * 64 for name in pins.MATCHER_SOURCE_FILES}
    manifest = build._manifest_for_matcher(
        source_hashes,
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        _test_toolchain(tmp_path),
        pins.MATCHER_WORKSPACE_SHA256,
    )
    manifest[field] = value  # type: ignore[assignment]
    manifest_path = root / pins.MATCHER_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(build.BuildError):
        build._verify_matcher_publication(root, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [("debuginfo", False), ("debuginfo", 0.0), ("codegen_units", True), ("codegen_units", 1.0)],
)
def test_matcher_manifest_rejects_bool_or_float_for_integer_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    root = tmp_path / "matcher"
    root.mkdir(mode=0o700)
    artifact = root / pins.MATCHER_ARTIFACT_NAME
    artifact.write_bytes(b"matcher artifact")
    artifact.chmod(0o700)
    source_hashes = {f"template_matcher/{name}": "a" * 64 for name in pins.MATCHER_SOURCE_FILES}
    manifest = build._manifest_for_matcher(
        source_hashes,
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        _test_toolchain(tmp_path),
        pins.MATCHER_WORKSPACE_SHA256,
    )
    cast_build = manifest["build"]
    assert isinstance(cast_build, dict)
    cast_build[field] = value
    manifest_path = root / pins.MATCHER_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(build.BuildError):
        build._verify_matcher_publication(root, None)


def test_concurrent_publication_lock_is_cross_process_serialized(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    ready_reader, ready_writer = context.Pipe(duplex=False)
    release_reader, release_writer = context.Pipe(duplex=False)
    first = context.Process(
        target=_hold_publication_lock,
        args=(str(tmp_path), ready_writer, release_reader),
    )
    first.start()
    assert ready_reader.recv() is True

    acquired_reader, acquired_writer = context.Pipe(duplex=False)
    second = context.Process(
        target=_attempt_publication_lock,
        args=(str(tmp_path), acquired_writer),
    )
    second.start()
    try:
        assert not acquired_reader.poll(0.2)
        release_writer.send(True)
        assert acquired_reader.poll(2.0)
        assert acquired_reader.recv() is True
    finally:
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        if first.is_alive():
            first.kill()
        if second.is_alive():
            second.kill()
    assert first.exitcode == 0
    assert second.exitcode == 0


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
