from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from crowley.session import driver as driver_module
from crowley.session.driver import DriverError, DriverProfile, WindowIdentity, X11Backend
from tests.test_session_input_tool import fixture_input_tool

IMAGE_SIZE = (16, 12)


def test_telemetry_executable_is_copied_to_an_immutable_memfd(tmp_path: Path) -> None:
    executable = tmp_path / "producer"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()

    descriptor, observed = driver_module._open_sealed_executable(executable, digest)
    try:
        assert observed == digest
        assert "crowley-telemetry-region" in os.readlink(f"/proc/self/fd/{descriptor}")
        assert fcntl.fcntl(descriptor, 1034) & 0xF == 0xF  # F_GET_SEALS
        with pytest.raises(OSError):
            os.write(descriptor, b"mutation")
    finally:
        os.close(descriptor)


def test_telemetry_protocol_rejects_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="duplicate telemetry protocol key"):
        json.loads(
            '{"type":"owner_ready","type":"capture_started"}',
            object_pairs_hook=driver_module._json_unique_pairs,
        )


def _identity(fingerprint: str = "capture-identity") -> WindowIdentity:
    return WindowIdentity(
        0x100,
        os.getpid(),
        "wowb.exe",
        "World of Warcraft",
        "wowb.exe",
        0x101,
        (0, 0, IMAGE_SIZE[0], IMAGE_SIZE[1]),
        (0, 0, IMAGE_SIZE[0], IMAGE_SIZE[1]),
        0x100,
        "capture-test",
        ":capture-test",
        fingerprint,
        0x100,
        0x100,
        frozenset({0x100, 0x101}),
    )


def _image_bytes(
    color: tuple[int, int, int, int], size: tuple[int, int] = IMAGE_SIZE, image_format: str = "PNG"
) -> bytes:
    from io import BytesIO

    output = BytesIO()
    image = Image.new("RGBA", size, color)
    if image_format == "JPEG":
        image = image.convert("RGB")
    image.save(output, format=image_format)
    return output.getvalue()


def _png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = IMAGE_SIZE) -> bytes:
    return _image_bytes(color, size=size)


class _BoundaryProcess:
    """Expose the producer boundary while making poll's publication atomic."""

    def __init__(self, path: Path, *, payload: bytes | None = None, return_code: int = 0) -> None:
        self.path = path
        self.payload = payload
        self.return_code = return_code
        self.poll_calls = 0

    def poll(self) -> int:
        self.poll_calls += 1
        if self.poll_calls == 1 and self.payload is not None:
            self.path.write_bytes(self.payload)
        return self.return_code

    def communicate(self, timeout: float = 0) -> tuple[str, str]:
        del timeout
        return "", ""


def test_wait_for_complete_png_rechecks_after_clean_producer_exit(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    valid = _png_bytes((31, 41, 51, 255))
    path.write_bytes(valid[:24])
    process = _BoundaryProcess(path, payload=valid)
    checks: list[int] = []

    driver_module._wait_for_complete_png(
        path,
        IMAGE_SIZE,
        time.monotonic() + 1.0,
        check=lambda: checks.append(len(checks) + 1),
        process=process,  # type: ignore[arg-type]
    )

    assert process.poll_calls == 1
    assert checks == [1, 2, 3]
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == IMAGE_SIZE


def test_wait_for_complete_png_keeps_clean_exit_with_truncated_output_terminal(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(_png_bytes((61, 71, 81, 255))[:24])
    process = _BoundaryProcess(path)

    with pytest.raises(
        DriverError, match="producer exited before PNG completion: GNOME screenshot is not a complete PNG"
    ):
        driver_module._wait_for_complete_png(
            path,
            IMAGE_SIZE,
            time.monotonic() + 1.0,
            process=process,  # type: ignore[arg-type]
        )

    assert process.poll_calls == 2


def test_wait_for_complete_png_revocation_wins_before_final_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.png"
    valid = _png_bytes((91, 101, 111, 255))
    path.write_bytes(valid[:24])
    process = _BoundaryProcess(path, payload=valid)
    validation_calls: list[int] = []
    original_validate = driver_module._validate_complete_png

    def record_validation(*args: object, **kwargs: object) -> None:
        validation_calls.append(1)
        original_validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(driver_module, "_validate_complete_png", record_validation)
    checks = 0

    def revoke_before_final_read() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise DriverError("capture revoked before final publication")

    with pytest.raises(DriverError, match="capture revoked before final publication"):
        driver_module._wait_for_complete_png(
            path,
            IMAGE_SIZE,
            time.monotonic() + 1.0,
            check=revoke_before_final_read,
            process=process,  # type: ignore[arg-type]
        )

    assert process.poll_calls == 1
    assert validation_calls == [1]
    assert checks == 2


def _region_identity() -> WindowIdentity:
    return WindowIdentity(
        0x100,
        os.getpid(),
        "wowb.exe",
        "World of Warcraft",
        "wowb.exe",
        0x101,
        (0, 0, 1920, 1080),
        (0, 0, 1920, 1080),
        0x100,
        "capture-region",
        ":capture-region",
        "capture-region-fingerprint",
        0x100,
        0x100,
        frozenset({0x100, 0x101}),
    )


def _region_writer(path: Path, *, owner_delay: float = 0.0, owner_variant: str = "valid") -> None:
    """Build an ELF owner so kernel ``/proc/PID/exe`` proof is exercised."""

    if shutil.which("cc") is None:
        pytest.skip("cc is required for the compiled telemetry producer fixture")
    payload = _png_bytes((90, 100, 110, 255), size=(256, 256))
    payload_hex = ",".join(f"0x{value:02x}" for value in payload)
    source = (
        r"""
#include <openssl/sha.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static const unsigned char payload[] = {@PAYLOAD@};
static const double owner_delay = @OWNER_DELAY@;
static const char *owner_variant = "@OWNER_VARIANT@";

static const char *value(int argc, char **argv, const char *name) {
  for (int i = 1; i + 1 < argc; i++)
    if (strcmp(argv[i], name) == 0) return argv[i + 1];
  return NULL;
}

static void hex_digest(const unsigned char *digest, char output[65]) {
  for (int i = 0; i < SHA256_DIGEST_LENGTH; i++)
    snprintf(output + (i * 2), 3, "%02x", digest[i]);
  output[64] = '\0';
}

static int hash_file(const char *path, char output[65]) {
  FILE *stream = fopen(path, "rb");
  unsigned char buffer[4096], digest[SHA256_DIGEST_LENGTH];
  SHA256_CTX context;
  size_t count;
  if (stream == NULL) return 0;
  SHA256_Init(&context);
  while ((count = fread(buffer, 1, sizeof(buffer), stream)) > 0)
    SHA256_Update(&context, buffer, count);
  if (ferror(stream)) { fclose(stream); return 0; }
  fclose(stream);
  SHA256_Final(digest, &context);
  hex_digest(digest, output);
  return 1;
}

static void hash_text(const char *text, char output[65]) {
  unsigned char digest[SHA256_DIGEST_LENGTH];
  SHA256((const unsigned char *)text, strlen(text), digest);
  hex_digest(digest, output);
}

static int process_start(char output[64]) {
  char buffer[4096];
  char *cursor, *token;
  int field = 3;
  FILE *stream = fopen("/proc/self/stat", "r");
  if (stream == NULL || fgets(buffer, sizeof(buffer), stream) == NULL) {
    if (stream != NULL) fclose(stream);
    return 0;
  }
  fclose(stream);
  cursor = strrchr(buffer, ')');
  if (cursor == NULL) return 0;
  cursor += 2;
  token = strtok(cursor, " ");
  while (token != NULL) {
    if (field == 22) { snprintf(output, 64, "%s", token); return 1; }
    field++;
    token = strtok(NULL, " ");
  }
  return 0;
}

int main(int argc, char **argv) {
  const char *fd_text = value(argc, argv, "--telemetry-owner-fd");
  const char *exec_text = value(argc, argv, "--telemetry-exec-fd");
  const char *nonce = value(argc, argv, "--telemetry-nonce");
  const char *output = value(argc, argv, "--file");
  const char *x_text = value(argc, argv, "--telemetry-x");
  const char *y_text = value(argc, argv, "--telemetry-y");
  const char *width_text = value(argc, argv, "--telemetry-width");
  const char *height_text = value(argc, argv, "--telemetry-height");
  char exec_path[64], start[64], exec_digest[65], output_digest[65], image_digest[65];
  char ack[128] = {0}, output_path[4096];
  int fd, exec_fd, x, y, width, height;
  FILE *stream;
  struct timespec now;
  ssize_t read_count;
  if (!fd_text || !exec_text || !nonce || !output || !x_text || !y_text || !width_text || !height_text)
    return 2;
  fd = atoi(fd_text); exec_fd = atoi(exec_text); x = atoi(x_text); y = atoi(y_text);
  width = atoi(width_text); height = atoi(height_text);
  snprintf(exec_path, sizeof(exec_path), "/proc/self/fd/%d", exec_fd);
  if (!process_start(start) || !hash_file(exec_path, exec_digest)) return 3;
  hash_text(output, output_digest);
  if (owner_delay > 0.0) {
    struct timespec pause = {(time_t)owner_delay, (long)((owner_delay - (time_t)owner_delay) * 1000000000.0)};
    nanosleep(&pause, NULL);
  }
  if (strcmp(owner_variant, "eof") == 0) return 7;
  const char *sent_nonce = strcmp(owner_variant, "wrong_nonce") == 0 ? "wrong" : nonce;
  const char *sent_digest = strcmp(owner_variant, "wrong_digest") == 0 ? "00" : exec_digest;
  const char *sent_path_digest = strcmp(owner_variant, "wrong_path") == 0 ? "00" : output_digest;
  const char *sent_protocol = strcmp(owner_variant, "bool_protocol") == 0 ? "true" : "1";
  int sent_pgid = strcmp(owner_variant, "wrong_pgid") == 0 ? getpgrp() + 1 : getpgrp();
  dprintf(fd, "{\"type\":\"owner_ready\",\"protocol\":%s,\"nonce\":\"%s\",\"pid\":%d,\"process_start\":\"%s\",\"ppid\":%d,\"pgid\":%d,\"exec_fd\":%d,\"executable_sha256\":\"%s\",\"primary_owner\":true,\"remote\":false,\"branch\":\"ScreenshotArea\",\"rectangle\":[%d,%d,%d,%d],\"output_path_digest\":\"%s\"}\n", sent_protocol, sent_nonce, getpid(), start, getppid(), sent_pgid, exec_fd, sent_digest, x, y, width, height, sent_path_digest);
  read_count = read(fd, ack, sizeof(ack) - 1);
  if (read_count <= 0 || strstr(ack, nonce) == NULL) return 4;
  if (strcmp(owner_variant, "eof_after_ack") == 0) return 8;
  clock_gettime(CLOCK_MONOTONIC, &now);
  if (strcmp(owner_variant, "future_started") == 0) now.tv_sec += 100;
  dprintf(fd, "{\"type\":\"capture_started\",\"protocol\":1,\"nonce\":\"%s\",\"branch\":\"ScreenshotArea\",\"rectangle\":[%d,%d,%d,%d],\"output_path_digest\":\"%s\",\"capture_started\":%lld.%09ld}\n", nonce, x, y, width, height, output_digest, (long long)now.tv_sec, now.tv_nsec);
  snprintf(output_path, sizeof(output_path), "%s", output);
  stream = fopen(output_path, "wb");
  if (stream == NULL || fwrite(payload, 1, sizeof(payload), stream) != sizeof(payload)) return 5;
  fclose(stream);
  if (!hash_file(output_path, image_digest)) return 6;
  if (strcmp(owner_variant, "wrong_complete_digest") == 0)
    snprintf(image_digest, sizeof(image_digest), "%064d", 0);
  dprintf(fd, "{\"type\":\"capture_complete\",\"protocol\":1,\"nonce\":\"%s\",\"branch\":\"ScreenshotArea\",\"rectangle\":[%d,%d,%d,%d],\"output_path_digest\":\"%s\",\"output_sha256\":\"%s\",\"output_bytes\":%zu}\n", nonce, x, y, width, height, output_digest, image_digest, strcmp(owner_variant, "wrong_complete_size") == 0 ? sizeof(payload) + 1 : sizeof(payload));
  return 0;
}
""".replace("@PAYLOAD@", payload_hex)
        .replace("@OWNER_DELAY@", repr(owner_delay))
        .replace("@OWNER_VARIANT@", owner_variant)
    )
    source_path = path.with_suffix(".c")
    source_path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        ["cc", "-O2", "-Wno-deprecated-declarations", str(source_path), "-lcrypto", "-o", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"compiled producer fixture failed: {result.stderr}")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def test_capture_native_region_uses_sealed_producer_and_exact_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "region-producer"
    _region_writer(executable)
    profile = DriverProfile(
        xauthority=str(tmp_path / "Xauthority"),
        client_width=1920,
        client_height=1080,
        outer_width=1928,
        outer_height=1092,
        canvas_crop=(123, 77, 256, 256),
        model_crop=(80, 48, 640, 480),
        telemetry_region_executable=executable,
        telemetry_region_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    profile.validate()
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = _region_identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")

    target = tmp_path / "capture.png"
    result = backend.capture_native_region(
        target,
        identity=identity,
        rectangle=(123, 77, 256, 256),
        deadline_monotonic=time.monotonic() + 5.0,
        _allow_unobserved_fixture=True,
    )

    assert result.fingerprint == identity.fingerprint
    assert backend.last_region_receipt is not None
    assert backend.last_region_receipt.owner_branch == "ScreenshotArea"
    assert backend.last_region_receipt.attempt_retired is True
    assert not backend._active_processes
    assert not backend.active_process_groups
    assert not tuple(tmp_path.glob(".*.telemetry-*"))
    with Image.open(target) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == (256, 256)


def test_capture_native_region_does_not_claim_retirement_after_directory_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "region-cleanup-producer"
    _region_writer(executable)
    profile = DriverProfile(
        xauthority=str(tmp_path / "Xauthority"),
        client_width=1920,
        client_height=1080,
        outer_width=1928,
        outer_height=1092,
        canvas_crop=(123, 77, 256, 256),
        model_crop=(80, 48, 640, 480),
        telemetry_region_executable=executable,
        telemetry_region_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = _region_identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")
    actual_rmtree = driver_module.shutil.rmtree
    monkeypatch.setattr(driver_module.shutil, "rmtree", lambda *_args, **_kwargs: None)

    target = tmp_path / "capture-with-unretired-attempt.png"
    try:
        backend.capture_native_region(
            target,
            identity=identity,
            rectangle=(123, 77, 256, 256),
            deadline_monotonic=time.monotonic() + 5.0,
            _allow_unobserved_fixture=True,
        )

        assert backend.last_region_receipt is not None
        assert backend.last_region_receipt.attempt_retired is False
        leftovers = tuple(tmp_path.glob("..capture-with-unretired-attempt.png.telemetry-*.retired-*"))
        assert len(leftovers) == 1
    finally:
        monkeypatch.setattr(driver_module.shutil, "rmtree", actual_rmtree)
        for leftover in tmp_path.glob("..capture-with-unretired-attempt.png.telemetry-*.retired-*"):
            actual_rmtree(leftover, ignore_errors=True)


def test_capture_native_region_checks_guardian_before_releasing_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "guardian-region-producer"
    _region_writer(executable)
    profile = DriverProfile(
        xauthority=str(tmp_path / "Xauthority"),
        client_width=1920,
        client_height=1080,
        outer_width=1928,
        outer_height=1092,
        canvas_crop=(80, 48, 256, 256),
        model_crop=(80, 48, 640, 480),
        telemetry_region_executable=executable,
        telemetry_region_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    backend.resolve_identity = lambda **_kwargs: _region_identity()  # type: ignore[method-assign]
    backend.set_admission_health(lambda: (_ for _ in ()).throw(DriverError("input watchdog is unhealthy")))
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")
    with pytest.raises(DriverError, match="watchdog is unhealthy"):
        backend.capture_native_region(
            tmp_path / "guardian-refused.png",
            identity=_region_identity(),
            rectangle=(80, 48, 256, 256),
            deadline_monotonic=time.monotonic() + 5.0,
            _allow_unobserved_fixture=True,
        )
    assert not (tmp_path / "guardian-refused.png").exists()
    assert not backend.active_process_groups
    assert not backend._active_processes


def test_capture_native_region_revoke_before_owner_ack_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "stalled-region-producer"
    _region_writer(executable, owner_delay=0.6)
    profile = DriverProfile(
        xauthority=str(tmp_path / "Xauthority"),
        client_width=1920,
        client_height=1080,
        outer_width=1928,
        outer_height=1092,
        canvas_crop=(80, 48, 256, 256),
        model_crop=(80, 48, 640, 480),
        telemetry_region_executable=executable,
        telemetry_region_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = _region_identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")
    target = tmp_path / "revoked.png"
    failures: list[BaseException] = []

    def capture() -> None:
        try:
            backend.capture_native_region(
                target,
                identity=identity,
                rectangle=(80, 48, 256, 256),
                deadline_monotonic=time.monotonic() + 5.0,
                _allow_unobserved_fixture=True,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports the error
            failures.append(exc)

    thread = threading.Thread(target=capture)
    thread.start()
    deadline = time.monotonic() + 2.0
    while not backend.active_process_groups and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.active_process_groups
    backend.release_held_input()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert failures and isinstance(failures[0], DriverError)
    assert "revoked" in str(failures[0])
    assert not target.exists()
    assert not backend.active_process_groups
    assert not backend._active_processes


@pytest.mark.parametrize(
    ("owner_variant", "error"),
    (
        ("wrong_nonce", "owner proof"),
        ("wrong_digest", "owner proof"),
        ("wrong_path", "owner proof"),
        ("wrong_pgid", "owner proof"),
        ("bool_protocol", "owner proof"),
        ("eof", "protocol closed"),
        ("eof_after_ack", "protocol closed"),
        ("future_started", "capture-start proof"),
        ("wrong_complete_digest", "output digest"),
        ("wrong_complete_size", "output digest"),
    ),
)
def test_capture_native_region_rejects_compiled_owner_proof_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_variant: str, error: str
) -> None:
    executable = tmp_path / f"invalid-{owner_variant}"
    _region_writer(executable, owner_variant=owner_variant)
    profile = DriverProfile(
        xauthority=str(tmp_path / "Xauthority"),
        client_width=1920,
        client_height=1080,
        outer_width=1928,
        outer_height=1092,
        canvas_crop=(80, 48, 256, 256),
        model_crop=(80, 48, 640, 480),
        telemetry_region_executable=executable,
        telemetry_region_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    identity = _region_identity()
    backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")

    started_at = time.monotonic()
    with pytest.raises(DriverError, match=error):
        backend.capture_native_region(
            tmp_path / "invalid.png",
            identity=identity,
            rectangle=(80, 48, 256, 256),
            deadline_monotonic=time.monotonic() + 5.0,
            _allow_unobserved_fixture=True,
        )
    if owner_variant == "eof_after_ack":
        assert time.monotonic() - started_at < 2.0
    assert not (tmp_path / "invalid.png").exists()
    assert not backend.active_process_groups


def _writer(path: Path, payload: bytes, mode: str, delay: float = 0.08) -> Path:
    """Create an owned writer executable that models GNOME's exit/write race."""

    encoded = base64.b64encode(payload).decode("ascii")
    source = f"""#!{sys.executable}
import base64
import pathlib
import subprocess
import sys
import time

target = pathlib.Path(sys.argv[2])
data = base64.b64decode({encoded!r})
mode = {mode!r}
delay = {delay!r}
if mode == "complete":
    target.write_bytes(data)
elif mode == "delayed":
    subprocess.Popen(
        [sys.executable, "-c", "import pathlib,sys,time; time.sleep(float(sys.argv[3])); pathlib.Path(sys.argv[1]).write_bytes(__import__('base64').b64decode(sys.argv[2]))", str(target), {encoded!r}, str(delay)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
elif mode == "partial":
    target.write_bytes(data[:24])
    subprocess.Popen(
        [sys.executable, "-c", "import pathlib,sys,time; time.sleep(float(sys.argv[3])); path=pathlib.Path(sys.argv[1]); path.open('ab').write(__import__('base64').b64decode(sys.argv[2]))", str(target), {base64.b64encode(payload[24:]).decode("ascii")!r}, str(delay)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
elif mode == "truncated":
    target.write_bytes(data[:24])
elif mode == "never":
    pass
else:
    raise SystemExit("unknown writer mode")
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: Path) -> X11Backend:
    xauthority = tmp_path / "Xauthority"
    xauthority.write_bytes(b"")
    profile = DriverProfile(
        xauthority=str(xauthority),
        client_width=IMAGE_SIZE[0],
        client_height=IMAGE_SIZE[1],
        outer_width=IMAGE_SIZE[0],
        outer_height=IMAGE_SIZE[1],
        canvas_crop=(0, 0, 1, 1),
        model_crop=(0, 0, 1, 1),
    )
    backend = X11Backend(profile, input_tool=fixture_input_tool(tmp_path / "tool"))
    current = _identity()
    backend.resolve_identity = lambda **_kwargs: current  # type: ignore[method-assign]
    monkeypatch.setattr(driver_module, "_resolve_wrapped_executable", lambda _command: str(writer))
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", tmp_path / "capture.lock")
    return backend


def _hold_lock(
    path: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
) -> None:
    driver_module.GNOME_CAPTURE_LOCK_PATH = Path(path)
    with driver_module._capture_lock(time.monotonic() + 3.0):
        ready.set()
        release.wait(3.0)


def test_capture_accepts_owned_writer_that_finishes_after_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((10, 20, 30, 255)), "delayed", delay=0.5)
    backend = _backend(tmp_path, monkeypatch, writer)
    target = tmp_path / "capture.png"
    original_run = backend._run
    output_at_process_exit: list[int | None] = []
    attempt_dirs: list[Path] = []
    attempt_dir_modes: list[int] = []

    def record_process_exit(args: list[str], **kwargs: object):
        result = original_run(args, **kwargs)
        stage = Path(args[2])
        output_at_process_exit.append(stage.stat().st_size if stage.exists() else None)
        attempt_dirs.append(stage.parent)
        attempt_dir_modes.append(stat.S_IMODE(stage.parent.stat().st_mode))
        return result

    backend._run = record_process_exit  # type: ignore[method-assign]

    result = backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)

    assert result.fingerprint == "capture-identity"
    assert output_at_process_exit == [None]
    assert attempt_dirs[0] != target.parent
    assert attempt_dir_modes == [0o700]
    with Image.open(target) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == IMAGE_SIZE


def test_capture_started_after_release_uses_the_current_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((12, 22, 32, 255)), "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    backend.release_held_input()

    result = backend.capture(
        tmp_path / "capture.png", identity=_identity(), deadline_monotonic=time.monotonic() + 2.0
    )

    assert result.fingerprint == "capture-identity"


def test_capture_waits_for_partial_png_and_decodes_all_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((40, 50, 60, 255)), "partial", delay=0.5)
    backend = _backend(tmp_path, monkeypatch, writer)
    original_run = backend._run
    output_at_process_exit: list[int | None] = []
    attempt_dirs: list[Path] = []
    attempt_dir_modes: list[int] = []

    def record_process_exit(args: list[str], **kwargs: object):
        result = original_run(args, **kwargs)
        stage = Path(args[2])
        output_at_process_exit.append(stage.stat().st_size if stage.exists() else None)
        attempt_dirs.append(stage.parent)
        attempt_dir_modes.append(stat.S_IMODE(stage.parent.stat().st_mode))
        return result

    backend._run = record_process_exit  # type: ignore[method-assign]

    backend.capture(tmp_path / "capture.png", identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)

    assert output_at_process_exit == [24]
    assert attempt_dir_modes == [0o700]
    assert not list(tmp_path.glob(".*.capture.png"))


@pytest.mark.parametrize("mode", ["never", "truncated"])
def test_capture_refuses_missing_or_truncated_output_by_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((70, 80, 90, 255)), mode)
    backend = _backend(tmp_path, monkeypatch, writer)
    target = tmp_path / "capture.png"
    deadline = time.monotonic() + 1.0

    with pytest.raises(DriverError, match="complete PNG before deadline"):
        backend.capture(target, identity=_identity(), deadline_monotonic=deadline)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.capture.png"))


def test_old_destination_cannot_satisfy_a_failed_new_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _png_bytes((1, 2, 3, 255))
    target = tmp_path / "capture.png"
    target.write_bytes(old)
    writer = _writer(tmp_path / "writer", _png_bytes((4, 5, 6, 255)), "never")
    backend = _backend(tmp_path, monkeypatch, writer)

    with pytest.raises(DriverError, match="complete PNG before deadline"):
        backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 1.0)

    assert target.read_bytes() == old


def test_capture_lock_serializes_independent_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "capture.lock"
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", lock_path)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    try:
        assert ready.wait(2.0)
        started = time.monotonic()
        with pytest.raises(DriverError, match="lock deadline expired"):
            with driver_module._capture_lock(time.monotonic() + 0.12):
                raise AssertionError("a second process acquired the capture lock")
        assert time.monotonic() - started >= 0.10
    finally:
        release.set()
        holder.join(3.0)
    assert holder.exitcode == 0


def test_capture_lock_rejects_a_symlinked_lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "capture.lock"
    target = tmp_path / "other.lock"
    target.write_bytes(b"")
    lock_path.symlink_to(target)
    monkeypatch.setattr(driver_module, "GNOME_CAPTURE_LOCK_PATH", lock_path)

    with pytest.raises(DriverError, match="private owned regular file"):
        with driver_module._capture_lock(time.monotonic() + 1.0):
            raise AssertionError("symlinked capture lock was accepted")


def test_capture_revocation_epoch_blocks_delayed_writer_after_rearm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((15, 25, 35, 255)), "delayed", delay=0.5)
    backend = _backend(tmp_path, monkeypatch, writer)
    original_run = backend._run

    def revoke_after_process_exit(args: list[str], **kwargs: object):
        result = original_run(args, **kwargs)
        backend.release_held_input()
        backend.activate_generation(2)
        return result

    backend._run = revoke_after_process_exit  # type: ignore[method-assign]

    with pytest.raises(DriverError, match="capture was revoked"):
        backend.capture(
            tmp_path / "capture.png", identity=_identity(), deadline_monotonic=time.monotonic() + 2.0
        )
    assert not list(tmp_path.glob(".*.capture-*"))


def test_capture_revocation_epoch_blocks_lock_wait_after_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((16, 26, 36, 255)), "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    lock_path = tmp_path / "capture.lock"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    result: list[BaseException] = []

    def run_capture() -> None:
        try:
            backend.capture(
                tmp_path / "capture.png", identity=_identity(), deadline_monotonic=time.monotonic() + 3.0
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports the exception
            result.append(exc)

    thread = threading.Thread(target=run_capture)
    try:
        assert ready.wait(2.0)
        thread.start()
        time.sleep(0.05)
        backend.release_held_input()
        thread.join(2.0)
    finally:
        release.set()
        holder.join(3.0)
    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], DriverError)
    assert "capture was revoked" in str(result[0])
    assert holder.exitcode == 0


def test_delayed_writer_cannot_recreate_retired_attempt_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _png_bytes((1, 2, 3, 255))
    target = tmp_path / "capture.png"
    target.write_bytes(old)
    writer = _writer(tmp_path / "writer", _png_bytes((17, 27, 37, 255)), "delayed", delay=1.5)
    backend = _backend(tmp_path, monkeypatch, writer)

    with pytest.raises(DriverError, match="complete PNG before deadline"):
        backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 1.0)
    assert target.read_bytes() == old
    assert not list(tmp_path.glob(".*.capture-*"))
    time.sleep(0.75)
    assert not list(tmp_path.glob(".*.capture-*"))

    succeeding = _writer(tmp_path / "succeeding-writer", _png_bytes((18, 28, 38, 255)), "complete")
    monkeypatch.setattr(driver_module, "_resolve_wrapped_executable", lambda _command: str(succeeding))
    backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)
    assert target.read_bytes() != old


def test_larger_desktop_png_is_accepted_for_existing_crop_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((19, 29, 39, 255), size=(20, 16)), "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    target = tmp_path / "capture.png"

    backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)

    with Image.open(target) as image:
        assert image.size == (20, 16)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_png_bytes((20, 30, 40, 255), size=(8, 8)), "smaller than the verified client"),
        (_image_bytes((20, 30, 40, 255), image_format="JPEG"), "format is not PNG"),
    ],
)
def test_wrong_format_or_size_refuses_without_waiting_for_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    writer = _writer(tmp_path / "writer", payload, "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    original_run = backend._run
    process_exit: list[float] = []

    def record_process_exit(args: list[str], **kwargs: object):
        result = original_run(args, **kwargs)
        process_exit.append(time.monotonic())
        return result

    backend._run = record_process_exit  # type: ignore[method-assign]
    started = time.monotonic()
    with pytest.raises(DriverError, match=message):
        backend.capture(tmp_path / "capture.png", identity=_identity(), deadline_monotonic=started + 1.0)

    assert process_exit
    assert time.monotonic() - process_exit[0] < 0.25


def test_lock_contention_consumes_deadline_without_invoking_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((10, 10, 10, 255)), "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    lock_path = tmp_path / "capture.lock"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    calls = 0

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("screenshot subprocess ran while capture lock was held")

    backend._run = unexpected_run  # type: ignore[method-assign]
    try:
        assert ready.wait(2.0)
        with pytest.raises(DriverError, match="lock deadline expired"):
            backend.capture(
                tmp_path / "capture.png",
                identity=_identity(),
                deadline_monotonic=time.monotonic() + 0.12,
            )
    finally:
        release.set()
        holder.join(3.0)
    assert calls == 0
    assert holder.exitcode == 0


def test_identity_change_after_complete_output_refuses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path / "writer", _png_bytes((11, 22, 33, 255)), "complete")
    backend = _backend(tmp_path, monkeypatch, writer)
    identities = iter((_identity(), _identity("changed")))
    backend.resolve_identity = lambda **_kwargs: next(identities)  # type: ignore[method-assign]
    target = tmp_path / "capture.png"

    with pytest.raises(DriverError, match="identity changed during screenshot"):
        backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.capture.png"))


def test_failed_capture_releases_lock_for_successor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = _writer(tmp_path / "failing-writer", _png_bytes((1, 1, 1, 255)), "never")
    succeeding = _writer(tmp_path / "succeeding-writer", _png_bytes((2, 2, 2, 255)), "complete")
    backend = _backend(tmp_path, monkeypatch, failing)
    target = tmp_path / "capture.png"

    with pytest.raises(DriverError, match="complete PNG before deadline"):
        backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 1.0)
    monkeypatch.setattr(driver_module, "_resolve_wrapped_executable", lambda _command: str(succeeding))

    backend.capture(target, identity=_identity(), deadline_monotonic=time.monotonic() + 2.0)
    assert target.exists()
