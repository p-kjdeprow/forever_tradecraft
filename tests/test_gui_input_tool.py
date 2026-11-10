from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from crowley.gui.input_tool import (
    ARCHIVE_BASENAME,
    MANIFEST_BASENAME,
    PINNED_ARCHIVE_SHA256,
    PINNED_COMMIT,
    PRIVATE_TOOL_BASENAME,
    InputToolError,
    PinnedInputTool,
    verify_artifact,
)
from crowley.native import build as native_build
from crowley.native import pins

VENDORED_SOURCE_ARCHIVE = native_build.VENDORED_SOURCE_ARCHIVE
_TEST_BUILD_INPUTS = {
    "compiler": {"name": "cc", "sha256": "0" * 64, "version": "test compiler"},
    "linker": {"name": "ld", "sha256": "1" * 64, "version": "test linker"},
}


def _artifact_copy(tmp_path: Path, executable_source: str = "") -> Path:
    root = tmp_path / "xdotool"
    root.mkdir(mode=0o700, parents=True)
    executable = Path(executable_source or "/bin/true")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    releases = root / "releases"
    releases.mkdir(mode=0o700)
    releases.chmod(0o700)
    release = releases / digest
    release.mkdir(mode=0o700)
    shutil.copyfile(executable, release / PRIVATE_TOOL_BASENAME)
    shutil.copyfile(VENDORED_SOURCE_ARCHIVE, release / ARCHIVE_BASENAME)
    (release / MANIFEST_BASENAME).write_text(
        json.dumps(
            {
                "commit": PINNED_COMMIT,
                "archive_sha256": PINNED_ARCHIVE_SHA256,
                "executable": PRIVATE_TOOL_BASENAME,
                "executable_sha256": digest,
                pins.XDOTOOL_BUILD_INPUTS_KEY: _TEST_BUILD_INPUTS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    for child in release.iterdir():
        child.chmod(
            stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if child.name == PRIVATE_TOOL_BASENAME else 0)
        )
    (root / "current").symlink_to(Path("releases") / digest)
    return root


def _current_release(root: Path) -> Path:
    return root / "current"


def fixture_input_tool(tmp_path: Path, executable_source: str = "/bin/true") -> PinnedInputTool:
    """Build a test-only pinned image around a harmless no-input executable."""

    return PinnedInputTool.from_root(_artifact_copy(tmp_path, executable_source))


def test_vendored_source_archive_is_the_pinned_upstream_source() -> None:
    archive = VENDORED_SOURCE_ARCHIVE
    assert archive.stat().st_size == 115654
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == PINNED_ARCHIVE_SHA256


def test_private_artifact_and_sealed_memfd_identity(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    identity = verify_artifact(root)
    tool = PinnedInputTool.from_root(root)
    try:
        assert identity.executable_sha256 == tool.digest
        assert os.readlink(f"/proc/{os.getpid()}/fd/{tool.image.fd}") == "/memfd:crowley-xdotool (deleted)"
        assert tool.proc_path.startswith("/proc/self/fd/")
    finally:
        tool.close()


def test_native_release_spec_rejects_matching_unsealed_memfd() -> None:
    from crowley.gui.input_tool import NativeReleaseSpec, _memfd_create

    fd = _memfd_create()
    data = Path("/bin/true").read_bytes()
    os.write(fd, data)
    spec = NativeReleaseSpec(fd, hashlib.sha256(data).hexdigest(), "fixture")
    try:
        with pytest.raises(InputToolError, match="fully sealed"):
            spec.verify_image()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_sealed_image_executes_after_published_path_is_removed(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path, "/bin/true")
    tool = PinnedInputTool.from_root(root)
    try:
        (_current_release(root) / PRIVATE_TOOL_BASENAME).unlink()
        result = subprocess.run(
            [tool.proc_path, "ignored"],
            check=False,
            pass_fds=(tool.image.fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert result.returncode == 0
        with pytest.raises(InputToolError):
            verify_artifact(root)
    finally:
        tool.close()


def test_builder_rechecks_existing_artifact_linkage(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


def test_builder_idempotent_existing_artifact_rechecks_real_static_call_path(tmp_path: Path) -> None:
    destination = tmp_path / "built"
    first = native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, destination)
    digest = hashlib.sha256((first / "current" / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    second = native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, destination)
    assert second == destination
    assert hashlib.sha256((second / "current" / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest() == digest


def test_builder_rejects_existing_dynamic_libxdo_dependency(tmp_path: Path) -> None:
    library_source = tmp_path / "xdo.c"
    library = tmp_path / "libxdo.so"
    executable_source = tmp_path / "dynamic.c"
    executable = tmp_path / "dynamic"
    library_source.write_text("void xdo_move_mouse(void) {}\n", encoding="utf-8")
    executable_source.write_text(
        "extern void xdo_move_mouse(void); int main(void) { xdo_move_mouse(); return 0; }\n",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-shared", "-fPIC", str(library_source), "-o", str(library)], check=True)
    subprocess.run(
        [
            "cc",
            str(executable_source),
            "-L",
            str(tmp_path),
            "-Wl,-rpath," + str(tmp_path),
            "-lxdo",
            "-o",
            str(executable),
        ],
        check=True,
    )
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir(mode=0o700)
    root = _artifact_copy(artifact_root, str(executable))
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


def test_builder_rejects_existing_non_xtest_move_call_path(tmp_path: Path) -> None:
    source = tmp_path / "callpath.c"
    executable = tmp_path / "callpath"
    source.write_text(
        "void xdo_move_mouse(void) {} int main(void) { xdo_move_mouse(); return 0; }\n",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir(mode=0o700)
    root = _artifact_copy(artifact_root, str(executable))
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


@pytest.mark.parametrize("mutation", ["symlink", "writable", "tampered", "foreign"])
def test_private_artifact_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    root = _artifact_copy(tmp_path)
    executable = _current_release(root) / PRIVATE_TOOL_BASENAME
    if mutation == "symlink":
        executable.unlink()
        executable.symlink_to("/bin/true")
    elif mutation == "writable":
        executable.chmod(executable.stat().st_mode | stat.S_IWGRP)
    elif mutation == "tampered":
        executable.write_bytes(executable.read_bytes() + b"tampered")
    else:
        # lchown is unavailable to an unprivileged test user, so make the
        # directory fail the ownership check through a foreign mode contract.
        root.chmod(0o777)
    with pytest.raises(InputToolError):
        verify_artifact(root)


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    manifest = json.loads((_current_release(root) / MANIFEST_BASENAME).read_text(encoding="utf-8"))
    manifest["fallback"] = "/usr/bin/xdotool"
    (_current_release(root) / MANIFEST_BASENAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputToolError, match="fields"):
        verify_artifact(root)


def test_manifest_rejects_legacy_missing_build_inputs(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    manifest = json.loads((_current_release(root) / MANIFEST_BASENAME).read_text(encoding="utf-8"))
    del manifest[pins.XDOTOOL_BUILD_INPUTS_KEY]
    (_current_release(root) / MANIFEST_BASENAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputToolError, match="fields"):
        verify_artifact(root)


def test_private_artifact_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    current_uid = os.getuid()
    monkeypatch.setattr("crowley.gui.input_tool.os.getuid", lambda: current_uid + 1)
    with pytest.raises(InputToolError, match="not user-owned"):
        verify_artifact(root)
