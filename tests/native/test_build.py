from __future__ import annotations

import hashlib
import multiprocessing
import os
import subprocess
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from crowley.native import build


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


def _hold_publication_lock(parent_name: str, ready: Connection, release: Connection) -> None:
    with build._publication_lock(Path(parent_name)):
        ready.send(True)
        release.recv()


def _attempt_publication_lock(parent_name: str, acquired: Connection) -> None:
    with build._publication_lock(Path(parent_name)):
        acquired.send(True)


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

    def refuse(*_args: object, replace: bool = False) -> Path:
        del replace
        raise build.BuildError("existing xdotool publication differs; pass --replace to replace it")

    monkeypatch.setattr(build, "build_xdotool", refuse)
    result = build.main(["build", "xdotool", "--output-dir", str(destination)])
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


def test_cli_refuses_unsafe_output_directory_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o755)
    unsafe_parent.chmod(0o755)

    result = build.main(["build", "xdotool", "--output-dir", str(unsafe_parent / "publication")])

    assert result == 1
    error = capsys.readouterr().err
    assert "private directory must have mode 0700" in error
    assert "Traceback" not in error


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


def test_clean_wheel_native_cli_runs_outside_the_checkout(tmp_path: Path) -> None:
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
        [str(virtualenv / "bin" / "crowley-native"), "build", "--help"],
        cwd=outside,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "xdotool" in result.stdout
    assert build.VENDORED_SOURCE_ARCHIVE.is_file()
