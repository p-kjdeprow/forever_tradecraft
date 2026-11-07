"""Bounded runtime adapter for the reviewed Rust template matcher.

This module owns only release verification and the closed matcher protocol.  It
does not build code, invoke a shell, use a Python matching fallback, or acquire
desktop input.  The helper can inject :class:`TemplateMatcherRuntime` later.

The focused tests require the retained private release fixture.  Configure
``CROWLEY_TEMPLATE_MATCHER_ARTIFACT_ROOT`` for that release, or install it at
the root returned by :func:`default_artifact_root`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from crowley.platform.paths import XdgDirs

from . import pins

MAGIC = pins.MATCHER_MAGIC
PROTOCOL = pins.MATCHER_PROTOCOL
ARTIFACT_NAME = pins.MATCHER_ARTIFACT_NAME
MANIFEST_NAME = pins.MATCHER_MANIFEST_NAME
TARGET = pins.TARGET_TRIPLE

EXPECTED_ARTIFACT_SHA256 = pins.MATCHER_ARTIFACT_SHA256
EXPECTED_SOURCE_HASHES = pins.MATCHER_SOURCE_HASHES
EXPECTED_SOURCE_SHA256 = pins.MATCHER_SOURCE_SHA256
MAX_INPUT_BYTES = pins.MATCHER_MAX_INPUT_BYTES
MAX_FRAME_DIM = pins.MATCHER_MAX_FRAME_DIM
MAX_TEMPLATE_DIM = pins.MATCHER_MAX_TEMPLATE_DIM
MAX_COMPARISON_BYTES = pins.MATCHER_MAX_COMPARISON_BYTES
MAX_OUTPUT_BYTES = pins.MATCHER_MAX_OUTPUT_BYTES
MIN_SPAWN_REMAINING = 0.001
PROCESS_REAP_TIMEOUT = 0.5


def default_artifact_root() -> Path:
    """Return the reviewed matcher publication root for the current environment."""

    return XdgDirs.from_environ(os.environ).crowley_cache / "template-matcher" / "v1"


class MatcherError(RuntimeError):
    """Base class for closed adapter failures."""


class ArtifactError(MatcherError):
    """The configured release is unavailable or fails its provenance checks."""


class MatcherProtocolError(MatcherError):
    """The native process returned a malformed or inconsistent result."""


class MatcherTimeout(TimeoutError, MatcherError):
    """The supplied absolute deadline expired before completion."""


class MatcherCancelled(MatcherError):
    """The caller cancelled the bounded matcher invocation."""


class CancellationToken:
    """Thread-safe cancellation token for one matcher invocation."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        return self._event.is_set()


class CancellationSource(Protocol):
    """The cancellation interface accepted by the matcher runtime."""

    def is_set(self) -> bool:
        """Return whether the current invocation has been cancelled."""


@dataclass(frozen=True)
class MatcherResult:
    status: str
    sad: int
    count: int
    box: tuple[int, int, int, int]

    @property
    def accepted(self) -> bool:
        return self.status == "matched"

    @property
    def score(self) -> float:
        return self.sad / self.count


@dataclass(frozen=True)
class _Request:
    operation: int
    frame_width: int
    frame_height: int
    template_width: int
    template_height: int
    region: tuple[int, int, int, int]
    payload: bytes


def _private_root(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ArtifactError(f"matcher artifact root is unavailable: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ArtifactError("matcher artifact root is not a directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ArtifactError("matcher artifact root is not private and user-owned")


def _open_private_file(path: Path, *, executable: bool = False) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ArtifactError(f"matcher release file is unavailable: {path.name}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (executable and not info.st_mode & 0o100)
    ):
        os.close(descriptor)
        raise ArtifactError(f"matcher release file is not private and regular: {path.name}")
    return descriptor


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def _read_descriptor_stable(descriptor: int) -> bytes:
    try:
        before = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ArtifactError("matcher release file could not be read") from error
    data = b"".join(chunks)
    if _identity(before) != _identity(after) or len(data) != before.st_size:
        raise ArtifactError("matcher release file changed during verification")
    return data


def _read_private_file(path: Path) -> bytes:
    descriptor = _open_private_file(path)
    try:
        return _read_descriptor_stable(descriptor)
    finally:
        os.close(descriptor)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        path_before = path.lstat()
        if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
            raise ArtifactError("runtime adapter source is not a regular file")
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ArtifactError(f"runtime adapter source is unavailable: {path}") from error
    try:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or _identity(path_before) != _identity(info)
        ):
            raise ArtifactError("runtime adapter source is not a user-owned regular file")
        data = _read_descriptor_stable(descriptor)
        path_after = path.lstat()
        if stat.S_ISLNK(path_after.st_mode) or _identity(path_after) != _identity(info):
            raise ArtifactError("runtime adapter source path changed during verification")
        return _sha256(data)
    finally:
        os.close(descriptor)


def _combined_source_hash(source_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_hashes[name].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ArtifactError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} is not a JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ArtifactError(f"manifest {label} is malformed")
    return value


def _verify_manifest(manifest: dict[str, Any], artifact_digest: str) -> dict[str, str]:
    if manifest.get("schema") != "crowley.template-matcher-release" or manifest.get("schema_version") != 1:
        raise ArtifactError("matcher manifest schema is not reviewed")
    if manifest.get("artifact") != ARTIFACT_NAME or manifest.get("protocol") != PROTOCOL:
        raise ArtifactError("matcher manifest identity is not reviewed")
    if manifest.get("target") != TARGET:
        raise ArtifactError("matcher manifest target is not reviewed")
    if manifest.get("artifact_sha256") != artifact_digest or artifact_digest != EXPECTED_ARTIFACT_SHA256:
        raise ArtifactError("matcher artifact digest is not the reviewed release")
    source_hashes = manifest.get("source_hashes")
    if source_hashes != EXPECTED_SOURCE_HASHES:
        raise ArtifactError("matcher Rust source pins are not reviewed")
    if manifest.get("source_sha256") != EXPECTED_SOURCE_SHA256:
        raise ArtifactError("matcher combined source pin is not reviewed")
    if manifest.get("source_snapshot_sha256") != EXPECTED_SOURCE_SHA256:
        raise ArtifactError("matcher source snapshot pin is not reviewed")
    if manifest.get("workspace_sha256") != pins.MATCHER_WORKSPACE_SHA256:
        raise ArtifactError("matcher workspace source pin is not reviewed")
    return dict(EXPECTED_SOURCE_HASHES)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MatcherProtocolError(f"{label} is malformed")
    return value


def _size(value: Any, label: str, maximum: int) -> tuple[int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise MatcherProtocolError(f"{label} dimensions are malformed")
    width = _integer(value[0], f"{label} width", minimum=1)
    height = _integer(value[1], f"{label} height", minimum=1)
    if width > maximum or height > maximum:
        raise MatcherProtocolError(f"{label} dimensions exceed the fixed bound")
    return width, height


def _box(value: Any, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise MatcherProtocolError(f"{label} is malformed")
    return tuple(_integer(item, f"{label} coordinate", minimum=0) for item in value)  # type: ignore[return-value]


def _validate_deadline(deadline_monotonic: float, clock: Callable[[], float]) -> float:
    if isinstance(deadline_monotonic, bool) or not isinstance(deadline_monotonic, (int, float)):
        raise MatcherTimeout("matcher deadline is malformed")
    deadline = float(deadline_monotonic)
    if not math.isfinite(deadline):
        raise MatcherTimeout("matcher deadline is not finite")
    remaining = deadline - clock()
    if remaining <= MIN_SPAWN_REMAINING:
        raise MatcherTimeout("matcher deadline expired before spawn")
    return deadline


def _cancelled(cancel_event: CancellationSource | None) -> bool:
    if cancel_event is None:
        return False
    return cancel_event.is_set()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=PROCESS_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=PROCESS_REAP_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise MatcherError("matcher process could not be reaped") from error
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class TemplateMatcherRuntime:
    """Verified, bounded access to the reviewed native matcher release."""

    def __init__(
        self,
        artifact_root: Path | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        root = Path(artifact_root) if artifact_root is not None else default_artifact_root()
        root = root.expanduser()
        _private_root(root)
        self._root = root
        self._clock = clock
        self._closed = False
        self._binary_fd: int | None = None
        try:
            manifest_bytes = _read_private_file(root / MANIFEST_NAME)
            manifest = _json_object(manifest_bytes, "matcher manifest")
            descriptor = _open_private_file(root / ARTIFACT_NAME, executable=True)
            self._binary_fd = descriptor
            artifact_bytes = _read_descriptor_stable(descriptor)
            artifact_digest = _sha256(artifact_bytes)
            source_hashes = _verify_manifest(manifest, artifact_digest)
            self._manifest = manifest
            self._artifact_sha256 = artifact_digest
            self._source_hashes = source_hashes
            self._source_bindings = self._make_source_bindings()
        except Exception:
            if self._binary_fd is not None:
                os.close(self._binary_fd)
                self._binary_fd = None
            raise

    def _make_source_bindings(self) -> dict[str, str]:
        try:
            runtime_path = Path(__file__).resolve()
            runtime_digest = _sha256_path(runtime_path)
        except (OSError, ArtifactError) as error:
            raise ArtifactError("runtime adapter source is unavailable") from error
        return {
            "template-matcher-runtime.py": runtime_digest,
            "template-matcher-binary": self._artifact_sha256,
            "template-matcher-Cargo.toml": self._source_hashes["Cargo.toml"],
            "template-matcher-Cargo.lock": self._source_hashes["Cargo.lock"],
            "template-matcher-src/main.rs": self._source_hashes["src/main.rs"],
        }

    @property
    def artifact_root(self) -> Path:
        return self._root

    @property
    def artifact_sha256(self) -> str:
        return self._artifact_sha256

    @property
    def source_hashes(self) -> dict[str, str]:
        return dict(self._source_hashes)

    @property
    def source_bindings(self) -> dict[str, str]:
        return dict(self._source_bindings)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor, self._binary_fd = self._binary_fd, None
        if descriptor is not None:
            os.close(descriptor)

    def __enter__(self) -> TemplateMatcherRuntime:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            descriptor = self._binary_fd
        except AttributeError:
            return
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._binary_fd = None

    def search(
        self,
        frame: bytes,
        frame_size: tuple[int, int],
        template: bytes,
        template_size: tuple[int, int],
        region: tuple[int, int, int, int],
        *,
        deadline_monotonic: float,
        cancel_event: CancellationSource | None = None,
    ) -> MatcherResult:
        request = self._request(1, frame, frame_size, template, template_size, region)
        return self._invoke(request, deadline_monotonic=deadline_monotonic, cancel_event=cancel_event)

    def score_at(
        self,
        frame: bytes,
        frame_size: tuple[int, int],
        template: bytes,
        template_size: tuple[int, int],
        box: tuple[int, int, int, int],
        *,
        deadline_monotonic: float,
        cancel_event: CancellationSource | None = None,
    ) -> MatcherResult:
        request = self._request(2, frame, frame_size, template, template_size, box)
        return self._invoke(request, deadline_monotonic=deadline_monotonic, cancel_event=cancel_event)

    def _request(
        self,
        operation: int,
        frame: bytes,
        frame_size: tuple[int, int],
        template: bytes,
        template_size: tuple[int, int],
        region: tuple[int, int, int, int],
    ) -> _Request:
        if operation not in (1, 2):
            raise MatcherProtocolError("matcher operation is not closed")
        frame_width, frame_height = _size(frame_size, "frame", MAX_FRAME_DIM)
        template_width, template_height = _size(template_size, "template", MAX_TEMPLATE_DIM)
        if not isinstance(frame, bytes) or not isinstance(template, bytes):
            raise MatcherProtocolError("matcher inputs must be raw RGB bytes")
        expected_frame_bytes = frame_width * frame_height * 3
        expected_template_bytes = template_width * template_height * 3
        if len(frame) != expected_frame_bytes or len(template) != expected_template_bytes:
            raise MatcherProtocolError("matcher RGB payload length is malformed")
        left, top, width, height = _box(region, "matcher region")
        if width <= 0 or height <= 0:
            raise MatcherProtocolError("matcher region dimensions must be positive")
        if left + width > frame_width or top + height > frame_height:
            raise MatcherProtocolError("matcher region is outside the frame")
        if template_width > width or template_height > height:
            raise MatcherProtocolError("matcher template is larger than the region")
        if operation == 2 and (width, height) != (template_width, template_height):
            raise MatcherProtocolError("SCORE_AT box must equal template dimensions")
        candidates = (width - template_width + 1) * (height - template_height + 1)
        comparison_bytes = expected_template_bytes if operation == 2 else candidates * expected_template_bytes
        if comparison_bytes > MAX_COMPARISON_BYTES:
            raise MatcherProtocolError("matcher comparison exceeds the fixed bound")
        header = struct.pack(
            "<8s10I",
            MAGIC,
            operation,
            0,
            frame_width,
            frame_height,
            template_width,
            template_height,
            left,
            top,
            width,
            height,
        )
        payload = header + frame + template
        if len(payload) > MAX_INPUT_BYTES:
            raise MatcherProtocolError("matcher input exceeds the fixed bound")
        return _Request(
            operation,
            frame_width,
            frame_height,
            template_width,
            template_height,
            (left, top, width, height),
            payload,
        )

    def _invoke(
        self,
        request: _Request,
        *,
        deadline_monotonic: float,
        cancel_event: CancellationSource | None,
    ) -> MatcherResult:
        if self._closed or self._binary_fd is None:
            raise MatcherError("matcher runtime is closed")
        deadline = _validate_deadline(deadline_monotonic, self._clock)
        if _cancelled(cancel_event):
            raise MatcherCancelled("matcher cancelled before spawn")
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [f"/proc/self/fd/{self._binary_fd}"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(self._binary_fd,),
                start_new_session=True,
                env={"LC_ALL": "C"},
                cwd="/",
            )
            stdout, stderr = self._communicate(process, request.payload, deadline, cancel_event)
        except MatcherTimeout:
            if process is not None:
                _terminate_and_reap(process)
            raise
        except MatcherCancelled:
            if process is not None:
                _terminate_and_reap(process)
            raise
        except (OSError, ValueError) as error:
            if process is not None:
                _terminate_and_reap(process)
            raise MatcherError("matcher process could not start") from error
        finally:
            if process is not None and process.poll() is None:
                _terminate_and_reap(process)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:512]
            raise MatcherProtocolError(f"matcher process failed: {detail or process.returncode}")
        return self._parse_result(stdout, request)

    def _communicate(
        self,
        process: subprocess.Popen[bytes],
        payload: bytes,
        deadline: float,
        cancel_event: CancellationSource | None,
    ) -> tuple[bytes, bytes]:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise MatcherError("matcher pipes are unavailable")
        streams = (process.stdin, process.stdout, process.stderr)
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        selector = selectors.DefaultSelector()
        input_fd = process.stdin.fileno()
        output_fd = process.stdout.fileno()
        error_fd = process.stderr.fileno()
        selector.register(input_fd, selectors.EVENT_WRITE, "input")
        selector.register(output_fd, selectors.EVENT_READ, "output")
        selector.register(error_fd, selectors.EVENT_READ, "error")
        offset = 0
        outputs: dict[str, bytearray] = {"output": bytearray(), "error": bytearray()}
        try:
            while selector.get_map() or process.poll() is None:
                if _cancelled(cancel_event):
                    raise MatcherCancelled("matcher cancelled")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise MatcherTimeout("matcher deadline expired")
                if process.poll() is not None and input_fd in selector.get_map():
                    selector.unregister(input_fd)
                    process.stdin.close()
                for key, _mask in selector.select(min(remaining, 0.05)):
                    role = key.data
                    if role == "input":
                        try:
                            count = os.write(input_fd, payload[offset:])
                        except BrokenPipeError as error:
                            raise MatcherProtocolError("matcher closed stdin early") from error
                        offset += count
                        if offset == len(payload):
                            selector.unregister(input_fd)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 8192)
                        if chunk:
                            outputs[role].extend(chunk)
                            if len(outputs[role]) > MAX_OUTPUT_BYTES:
                                raise MatcherProtocolError("matcher output exceeds the fixed bound")
                        else:
                            selector.unregister(key.fd)
                            (process.stdout if role == "output" else process.stderr).close()
            return bytes(outputs["output"]), bytes(outputs["error"])
        finally:
            selector.close()
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass

    def _parse_result(self, output: bytes, request: _Request) -> MatcherResult:
        try:
            value = json.loads(output.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise MatcherProtocolError("matcher response is not valid JSON") from error
        if not isinstance(value, dict) or set(value) != {"status", "sad", "count", "box"}:
            raise MatcherProtocolError("matcher response schema is not closed")
        status = value["status"]
        if not isinstance(status, str):
            raise MatcherProtocolError("matcher response status is not closed")
        if status not in {"matched", "rejected", "ambiguous"}:
            raise MatcherProtocolError("matcher response status is not closed")
        if request.operation == 2 and status == "ambiguous":
            raise MatcherProtocolError("SCORE_AT response cannot be ambiguous")
        sad = _integer(value["sad"], "matcher SAD")
        count = _integer(value["count"], "matcher count", minimum=1)
        box = _box(value["box"], "matcher response box")
        expected_count = request.template_width * request.template_height * 3
        if count != expected_count or sad > 255 * count:
            raise MatcherProtocolError("matcher response numeric range is inconsistent")
        left, top, region_width, region_height = request.region
        x, y, width, height = box
        if (width, height) != (request.template_width, request.template_height):
            raise MatcherProtocolError("matcher response geometry is inconsistent")
        if x < left or y < top or x + width > left + region_width or y + height > top + region_height:
            raise MatcherProtocolError("matcher response box is outside the requested region")
        threshold = 5 * count
        if status == "matched" and sad > threshold:
            raise MatcherProtocolError("matched response exceeds the fixed threshold")
        if status == "rejected" and sad <= threshold:
            raise MatcherProtocolError("rejected response is inside the fixed threshold")
        if status == "ambiguous" and sad > threshold:
            raise MatcherProtocolError("ambiguous response is outside the fixed threshold")
        if request.operation == 2 and box != request.region:
            raise MatcherProtocolError("SCORE_AT response box differs from its request")
        return MatcherResult(status, sad, count, box)


__all__ = [
    "ARTIFACT_NAME",
    "ArtifactError",
    "CancellationSource",
    "CancellationToken",
    "default_artifact_root",
    "EXPECTED_ARTIFACT_SHA256",
    "EXPECTED_SOURCE_HASHES",
    "EXPECTED_SOURCE_SHA256",
    "MatcherError",
    "MatcherProtocolError",
    "MatcherResult",
    "MatcherTimeout",
    "MatcherCancelled",
    "TemplateMatcherRuntime",
]
