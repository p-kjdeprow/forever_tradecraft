"""Pinned private XTEST emitter and its immutable execution image.

The installed ``xdotool`` is unsuitable for absolute motion because its
``xdo_move_mouse`` path uses ``XWarpPointer``.  This module owns the one
published private build used by the native backend.  It deliberately has no
PATH or configuration fallback: a missing, changed, writable, symlinked, or
foreign artifact fails admission.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from crowley.native import pins
from crowley.platform.paths import XdgDirs


class InputToolError(RuntimeError):
    """The private input tool is absent, untrusted, or could not be pinned."""


PINNED_COMMIT = pins.XDOTOOL_COMMIT
PINNED_ARCHIVE_SHA256 = pins.XDOTOOL_ARCHIVE_SHA256
PRIVATE_TOOL_BASENAME = pins.XDOTOOL_ARTIFACT_NAME
MANIFEST_BASENAME = pins.XDOTOOL_MANIFEST_NAME
ARCHIVE_BASENAME = pins.XDOTOOL_ARCHIVE_NAME
_ELF_MAGIC = b"\x7fELF"
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)


def private_tool_root() -> Path:
    """Return the pinned xdotool publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "xdotool"


def _memfd_create() -> int:
    flags = 0x0001 | 0x0002  # MFD_CLOEXEC | MFD_ALLOW_SEALING
    # The supported Linux runtime uses glibc's memfd_create directly.  Keep
    # one canonical image-pinning path; a missing symbol is a hard failure.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        creator = libc.memfd_create
    except AttributeError as exc:
        raise InputToolError("glibc memfd_create is unavailable") from exc
    creator.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    creator.restype = ctypes.c_int
    fd = creator(b"crowley-xdotool", flags)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


def _regular_private_file(path: Path, *, label: str, owner: int | None = None) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise InputToolError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(st.st_mode):
        raise InputToolError(f"{label} must be a regular file")
    if owner is not None and st.st_uid != owner:
        raise InputToolError(f"{label} is owned by an unexpected user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InputToolError(f"{label} is writable by group or other users")


def _sha256(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise InputToolError(f"{path.name} exceeds the private artifact size bound")
    except OSError as exc:
        raise InputToolError(f"cannot stat {path.name}") from exc
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise InputToolError(f"cannot read {path.name}") from exc
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactIdentity:
    """Verified on-disk provenance for the private executable."""

    root: Path
    executable: Path
    executable_sha256: str
    archive_sha256: str
    commit: str

    @property
    def version(self) -> str:
        return f"xtest-{self.commit};sha256={self.executable_sha256}"


@dataclass(frozen=True)
class NativeReleaseSpec:
    """Typed watchdog release capability for one sealed executable image."""

    fd: int
    digest: str
    version: str

    def validate(self) -> None:
        if isinstance(self.fd, bool) or not isinstance(self.fd, int) or self.fd < 0:
            raise InputToolError("native release descriptor is malformed")
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise InputToolError("native release digest is malformed")
        if not isinstance(self.version, str) or not self.version:
            raise InputToolError("native release version is malformed")

    def verify_image(self) -> None:
        self.validate()
        try:
            descriptor = os.fstat(self.fd)
            if not stat.S_ISREG(descriptor.st_mode):
                raise InputToolError("native release descriptor is not a regular image")
            link = os.readlink(f"/proc/self/fd/{self.fd}")
            if not link.startswith("/memfd:crowley-xdotool"):
                raise InputToolError("native release descriptor is not the pinned memfd")
            seals = fcntl.fcntl(self.fd, _F_GET_SEALS)
            required = _F_SEAL_WRITE | _F_SEAL_GROW | _F_SEAL_SHRINK | _F_SEAL_SEAL
            if seals & required != required:
                raise InputToolError("native release descriptor is not fully sealed")
            data = os.pread(self.fd, _MAX_ARTIFACT_BYTES + 1, 0)
        except OSError as exc:
            raise InputToolError("native release image could not be inspected") from exc
        if len(data) > _MAX_ARTIFACT_BYTES or hashlib.sha256(data).hexdigest() != self.digest:
            raise InputToolError("native release image digest does not match its identity")


def verify_artifact(root: str | os.PathLike[str] | None = None) -> ArtifactIdentity:
    """Verify a complete private artifact manifest and return its identity.

    ``root`` exists as an explicit test/build injection. Production callers
    resolve the private root from the current XDG environment by omitting it.
    """

    artifact_root = Path(root) if root is not None else private_tool_root()
    try:
        root_stat = artifact_root.lstat()
    except OSError as exc:
        raise InputToolError("private xdotool artifact directory is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise InputToolError("private xdotool artifact directory is not user-owned")
    if root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InputToolError("private xdotool artifact directory is writable by group or other")
    try:
        children = {entry.name for entry in artifact_root.iterdir()}
    except OSError as exc:
        raise InputToolError("private xdotool artifact directory cannot be enumerated") from exc
    if children != {MANIFEST_BASENAME, PRIVATE_TOOL_BASENAME, ARCHIVE_BASENAME}:
        raise InputToolError("private xdotool artifact set is incomplete or contains extras")
    manifest_path = artifact_root / MANIFEST_BASENAME
    _regular_private_file(manifest_path, label="private xdotool manifest", owner=os.getuid())
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputToolError("private xdotool manifest is malformed") from exc
    if not isinstance(manifest, dict):
        raise InputToolError("private xdotool manifest is not an object")
    if set(manifest) != pins.XDOTOOL_MANIFEST_KEYS:
        raise InputToolError("private xdotool manifest fields are not canonical")
    build_inputs = manifest[pins.XDOTOOL_BUILD_INPUTS_KEY]
    if not isinstance(build_inputs, dict) or set(build_inputs) != {"compiler", "linker"}:
        raise InputToolError("private xdotool build inputs are malformed")
    for name in ("compiler", "linker"):
        record = build_inputs[name]
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "sha256", "version"}
            or not isinstance(record["name"], str)
            or not record["name"]
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in record["sha256"])
            or not isinstance(record["version"], str)
            or not record["version"]
        ):
            raise InputToolError("private xdotool build inputs are malformed")
    if manifest["commit"] != PINNED_COMMIT or manifest["archive_sha256"] != PINNED_ARCHIVE_SHA256:
        raise InputToolError("private xdotool source provenance is not pinned")
    executable_name = manifest["executable"]
    if executable_name != PRIVATE_TOOL_BASENAME or not isinstance(executable_name, str):
        raise InputToolError("private xdotool executable basename is not pinned")
    executable = artifact_root / executable_name
    _regular_private_file(executable, label="private xdotool executable", owner=os.getuid())
    if not os.access(executable, os.X_OK):
        raise InputToolError("private xdotool executable is not executable")
    try:
        if executable.read_bytes()[:4] != _ELF_MAGIC:
            raise InputToolError("private xdotool executable is not an ELF image")
    except OSError as exc:
        raise InputToolError("private xdotool executable cannot be read") from exc
    executable_sha256 = _sha256(executable)
    if manifest["executable_sha256"] != executable_sha256:
        raise InputToolError("private xdotool executable digest does not match manifest")
    archive = artifact_root / ARCHIVE_BASENAME
    _regular_private_file(archive, label="private xdotool source archive", owner=os.getuid())
    if _sha256(archive) != PINNED_ARCHIVE_SHA256:
        raise InputToolError("private xdotool source archive digest does not match pin")
    return ArtifactIdentity(
        root=artifact_root,
        executable=executable,
        executable_sha256=executable_sha256,
        archive_sha256=PINNED_ARCHIVE_SHA256,
        commit=PINNED_COMMIT,
    )


class PinnedInputImage:
    """An immutable executable image backed by a sealed Linux memfd."""

    def __init__(self, identity: ArtifactIdentity):
        self.identity = identity
        self.fd = -1
        self.digest = identity.executable_sha256
        try:
            data = identity.executable.read_bytes()
            if len(data) > _MAX_ARTIFACT_BYTES:
                raise InputToolError("private xdotool changed beyond the artifact size bound")
            if hashlib.sha256(data).hexdigest() != identity.executable_sha256:
                raise InputToolError("private xdotool changed while pinning")
            self.fd = _memfd_create()
            view = memoryview(data)
            while view:
                written = os.write(self.fd, view)
                if written <= 0:
                    raise InputToolError("private xdotool memfd write made no progress")
                view = view[written:]
            seals = _F_SEAL_WRITE | _F_SEAL_GROW | _F_SEAL_SHRINK | _F_SEAL_SEAL
            fcntl.fcntl(self.fd, _F_ADD_SEALS, seals)
            if fcntl.fcntl(self.fd, _F_GET_SEALS) & seals != seals:
                raise InputToolError("private xdotool memfd was not fully sealed")
            sealed = os.pread(self.fd, _MAX_ARTIFACT_BYTES + 1, 0)
            sealed_digest = hashlib.sha256(sealed).hexdigest()
            if len(sealed) != len(data) or sealed_digest != identity.executable_sha256:
                raise InputToolError("private xdotool sealed image digest does not match identity")
            os.lseek(self.fd, 0, os.SEEK_SET)
        except Exception:
            self.close()
            raise

    @property
    def proc_path(self) -> str:
        if self.fd < 0:
            raise InputToolError("private xdotool image is closed")
        return f"/proc/self/fd/{self.fd}"

    def duplicate(self) -> int:
        if self.fd < 0:
            raise InputToolError("private xdotool image is closed")
        try:
            return os.dup(self.fd)
        except OSError as exc:
            raise InputToolError("private xdotool image could not be duplicated") from exc

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            finally:
                self.fd = -1


class PinnedInputTool:
    """Verified artifact plus one owner-held immutable executable image."""

    def __init__(self, identity: ArtifactIdentity):
        self.identity = identity
        self.image = PinnedInputImage(identity)

    @classmethod
    def load(cls) -> PinnedInputTool:
        return cls(verify_artifact())

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> PinnedInputTool:
        return cls(verify_artifact(root))

    @property
    def digest(self) -> str:
        return self.identity.executable_sha256

    @property
    def version(self) -> str:
        return self.identity.version

    @property
    def executable(self) -> Path:
        return self.identity.executable

    @property
    def proc_path(self) -> str:
        return self.image.proc_path

    def duplicate_fd(self) -> int:
        return self.image.duplicate()

    def duplicate_release_spec(self) -> NativeReleaseSpec:
        spec = NativeReleaseSpec(self.duplicate_fd(), self.digest, self.version)
        try:
            spec.verify_image()
        except Exception:
            try:
                os.close(spec.fd)
            except OSError:
                pass
            raise
        return spec

    def close(self) -> None:
        self.image.close()


__all__ = [
    "ARCHIVE_BASENAME",
    "ArtifactIdentity",
    "InputToolError",
    "MANIFEST_BASENAME",
    "NativeReleaseSpec",
    "PINNED_ARCHIVE_SHA256",
    "PINNED_COMMIT",
    "PRIVATE_TOOL_BASENAME",
    "PinnedInputImage",
    "PinnedInputTool",
    "private_tool_root",
    "verify_artifact",
]
