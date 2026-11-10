from __future__ import annotations

import multiprocessing
import multiprocessing.synchronize
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from crowley.gui import capture as capture_module
from crowley.gui.capture import CaptureError, InvalidCaptureOutput

IMAGE_SIZE = (16, 12)


def _png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = IMAGE_SIZE) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


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

    capture_module.wait_for_complete_png(
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
        assert image.size == IMAGE_SIZE


def test_wait_for_complete_png_keeps_clean_exit_with_truncated_output_terminal(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(_png_bytes((61, 71, 81, 255))[:24])
    process = _BoundaryProcess(path)

    with pytest.raises(CaptureError, match="producer exited before PNG completion"):
        capture_module.wait_for_complete_png(
            path,
            IMAGE_SIZE,
            time.monotonic() + 1.0,
            process=process,  # type: ignore[arg-type]
        )

    assert process.poll_calls == 2


def test_wait_for_complete_png_check_failure_wins_before_final_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.png"
    valid = _png_bytes((91, 101, 111, 255))
    path.write_bytes(valid[:24])
    process = _BoundaryProcess(path, payload=valid)
    checks = 0

    def revoke_before_final_read() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise CaptureError("capture revoked before final publication")

    with pytest.raises(CaptureError, match="capture revoked before final publication"):
        capture_module.wait_for_complete_png(
            path,
            IMAGE_SIZE,
            time.monotonic() + 1.0,
            check=revoke_before_final_read,
            process=process,  # type: ignore[arg-type]
        )

    assert process.poll_calls == 1
    assert checks == 2


def test_wrong_format_or_size_refuses_without_waiting_for_deadline(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(_png_bytes((1, 2, 3, 255), size=(4, 4)))
    started = time.monotonic()
    with pytest.raises(InvalidCaptureOutput, match="smaller than the verified client"):
        capture_module.wait_for_complete_png(path, IMAGE_SIZE, time.monotonic() + 5.0)
    assert time.monotonic() - started < 1.0

    jpeg = BytesIO()
    Image.new("RGB", IMAGE_SIZE).save(jpeg, format="JPEG")
    path.write_bytes(jpeg.getvalue())
    with pytest.raises(InvalidCaptureOutput, match="not PNG"):
        capture_module.wait_for_complete_png(path, IMAGE_SIZE, time.monotonic() + 5.0)


def test_larger_desktop_png_is_accepted_for_existing_crop_contract(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(_png_bytes((9, 8, 7, 255), size=(IMAGE_SIZE[0] * 2, IMAGE_SIZE[1] * 2)))
    capture_module.wait_for_complete_png(path, IMAGE_SIZE, time.monotonic() + 1.0)


def test_truncated_output_times_out_by_the_deadline(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(_png_bytes((5, 5, 5, 255))[:24])
    with pytest.raises(CaptureError, match="did not become a complete PNG"):
        capture_module.wait_for_complete_png(path, IMAGE_SIZE, time.monotonic() + 0.1)


def _hold_lock(
    path: str, ready: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
) -> None:
    with capture_module.capture_lock(time.monotonic() + 3.0, lock_path=Path(path)):
        ready.set()
        release.wait(3.0)


def test_capture_lock_serializes_independent_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "capture.lock"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(lock_path), ready, release))
    holder.start()
    try:
        assert ready.wait(2.0)
        started = time.monotonic()
        with pytest.raises(CaptureError, match="lock deadline expired"):
            with capture_module.capture_lock(time.monotonic() + 0.12, lock_path=lock_path):
                raise AssertionError("a second process acquired the capture lock")
        assert time.monotonic() - started >= 0.10
    finally:
        release.set()
        holder.join(3.0)
    assert holder.exitcode == 0


def test_capture_lock_rejects_a_symlinked_lock_path(tmp_path: Path) -> None:
    lock_path = tmp_path / "capture.lock"
    target = tmp_path / "other.lock"
    target.write_bytes(b"")
    lock_path.symlink_to(target)

    with pytest.raises(CaptureError, match="private owned regular file|cannot be opened safely"):
        with capture_module.capture_lock(time.monotonic() + 1.0, lock_path=lock_path):
            raise AssertionError("symlinked capture lock was accepted")


def test_capture_screen_runs_gnome_screenshot_under_the_lock_and_waits_for_the_png(tmp_path: Path) -> None:
    path = tmp_path / "capture.png"
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: float) -> None:
        assert 0 < timeout <= capture_module.DEFAULT_CAPTURE_TIMEOUT_SECONDS
        calls.append(argv)
        path.write_bytes(_png_bytes((3, 2, 1, 255)))

    capture_module.capture_screen(path, IMAGE_SIZE, run=run, lock_path=tmp_path / "capture.lock")

    assert calls == [["gnome-screenshot", "-f", str(path)]]
