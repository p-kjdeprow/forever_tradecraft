from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.platform import privfs
from crowley.platform.privfs import (
    PrivateCopyError,
    PrivateFileError,
    PrivatePathError,
    PrivateReadError,
    PrivateWriteError,
    atomic_write_private,
    ensure_private_dir,
    read_bounded,
    retain_private_copy,
    sha256_file,
)


def _private_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _changed_stat(
    info: os.stat_result,
    *,
    size: int | None = None,
    mode: int | None = None,
    nlink: int | None = None,
    uid: int | None = None,
    ctime: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=info.st_dev,
        st_ino=info.st_ino,
        st_mode=info.st_mode if mode is None else mode,
        st_nlink=info.st_nlink if nlink is None else nlink,
        st_uid=info.st_uid if uid is None else uid,
        st_size=info.st_size if size is None else size,
        st_mtime_ns=info.st_mtime_ns,
        st_ctime_ns=info.st_ctime_ns + 1 if ctime else info.st_ctime_ns,
    )


def test_ensure_private_dir_creates_exact_mode(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "private"

    ensure_private_dir(target)

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    ensure_private_dir(target)


def test_ensure_private_dir_rejects_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(PrivatePathError, match="symlink"):
        ensure_private_dir(link)


def test_ensure_private_dir_rejects_wrong_mode(tmp_path: Path) -> None:
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    target.chmod(0o755)

    with pytest.raises(PrivatePathError, match="mode 0700"):
        ensure_private_dir(target)


def test_ensure_private_dir_rejects_wrong_owner_through_test_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    real_stat = target.stat()
    fake_stat = type("FakeStat", (), {"st_mode": real_stat.st_mode, "st_uid": os.getuid() + 1})()
    monkeypatch.setattr(privfs, "_directory_lstat", lambda _path: fake_stat)

    with pytest.raises(PrivatePathError, match="owned"):
        ensure_private_dir(target)


def test_ensure_private_dir_rejects_non_directory_parent(tmp_path: Path) -> None:
    parent = tmp_path / "file"
    parent.write_bytes(b"x")

    with pytest.raises(PrivatePathError, match="non-directory"):
        ensure_private_dir(parent / "child")


def test_atomic_write_private_publishes_mode_0600_bytes(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    target = directory / "state.json"

    atomic_write_private(target, b"hello")

    assert target.read_bytes() == b"hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_private_rejects_symlink_target(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    ensure_private_dir(directory)
    real = directory / "real"
    _private_file(real, b"old")
    link = directory / "state"
    link.symlink_to(real)

    with pytest.raises(PrivateWriteError, match="symlink"):
        atomic_write_private(link, b"new")
    assert real.read_bytes() == b"old"


def test_read_bounded_reads_exactly_and_rejects_oversize(tmp_path: Path) -> None:
    target = tmp_path / "data"
    _private_file(target, b"payload")

    assert read_bounded(target, 7) == b"payload"
    with pytest.raises(PrivateReadError, match="size bound"):
        read_bounded(target, 6)


def test_read_bounded_rejects_symlinked_file_and_wrong_mode(tmp_path: Path) -> None:
    target = tmp_path / "data"
    _private_file(target, b"payload")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(PrivatePathError, match="symlink"):
        read_bounded(link, 100)
    target.chmod(0o644)
    assert read_bounded(target, 100) == b"payload"


def test_read_bounded_rejects_truncated_or_changed_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data"
    _private_file(target, b"payload")
    original_fstat = privfs.os.fstat
    calls = 0

    def fake_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        info = original_fstat(fd)
        return _changed_stat(info, size=info.st_size - 1) if calls == 2 else info

    monkeypatch.setattr(privfs.os, "fstat", fake_fstat)

    with pytest.raises(PrivateReadError, match="changed during read"):
        read_bounded(target, 100)


@pytest.mark.parametrize("change", ("mode", "nlink", "uid", "ctime"))
def test_read_bounded_rejects_strict_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    target = tmp_path / "data"
    _private_file(target, b"payload")
    original_fstat = privfs.os.fstat
    calls = 0

    def fake_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        info = original_fstat(fd)
        if calls != 2:
            return info
        if change == "mode":
            return _changed_stat(info, mode=info.st_mode ^ stat.S_IRUSR)
        if change == "nlink":
            return _changed_stat(info, nlink=info.st_nlink + 1)
        if change == "uid":
            return _changed_stat(info, uid=info.st_uid + 1)
        return _changed_stat(info, ctime=True)

    monkeypatch.setattr(privfs.os, "fstat", fake_fstat)

    with pytest.raises(PrivateReadError, match="changed during read"):
        read_bounded(target, 100)


def test_retain_private_copy_hashes_during_atomic_copy(tmp_path: Path) -> None:
    source = tmp_path / "capture.bin"
    _private_file(source, b"retained bytes")
    destination_dir = tmp_path / "evidence"

    retained = retain_private_copy(source, destination_dir)

    assert retained.path == destination_dir / source.name
    assert retained.path.read_bytes() == source.read_bytes()
    assert retained.size == len(b"retained bytes")
    assert retained.sha256 == hashlib.sha256(b"retained bytes").hexdigest()
    assert stat.S_IMODE(retained.path.stat().st_mode) == 0o600


def test_retain_private_copy_rejects_symlinked_source_and_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _private_file(source, b"source")
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source)
    destination_dir = tmp_path / "evidence"

    with pytest.raises(PrivatePathError, match="symlink"):
        retain_private_copy(source_link, destination_dir)

    ensure_private_dir(destination_dir)
    destination = destination_dir / source.name
    destination.symlink_to(source)
    with pytest.raises(PrivateCopyError, match="symlink"):
        retain_private_copy(source, destination_dir)


def test_sha256_file_is_bounded_and_descriptor_checked(tmp_path: Path) -> None:
    target = tmp_path / "data"
    _private_file(target, b"payload")

    assert sha256_file(target, 100) == hashlib.sha256(b"payload").hexdigest()
    with pytest.raises(PrivateReadError, match="size bound"):
        sha256_file(target, 6)


def test_private_operations_reject_relative_paths(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(PrivatePathError, match="absolute"):
        ensure_private_dir(Path("relative"))
    with pytest.raises(PrivatePathError, match="absolute"):
        read_bounded(Path("relative"), 1)
    with pytest.raises(PrivatePathError, match="absolute"):
        atomic_write_private(Path("relative"), b"x")


def test_private_file_error_is_one_exception_hierarchy() -> None:
    assert issubclass(PrivatePathError, PrivateFileError)
    assert issubclass(PrivateReadError, PrivateFileError)
    assert issubclass(PrivateWriteError, PrivateFileError)
    assert issubclass(PrivateCopyError, PrivateFileError)
