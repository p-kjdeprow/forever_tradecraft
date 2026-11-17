from __future__ import annotations

import subprocess

import pytest
from crowley_automation import startup_ui
from PIL import Image


def test_stale_capture_is_refused_before_starting_ocr() -> None:
    calls: list[object] = []
    reader = startup_ui.TesseractReader(
        monotonic=lambda: 6.0, runner=lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(startup_ui.StartupUIError, match="screen_capture_stale"):
        reader.read(Image.new("RGB", (10, 10)), captured_monotonic=0.0)
    assert calls == []


def test_capture_expiring_during_ocr_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(startup_ui.shutil, "which", lambda *args, **kwargs: "/usr/bin/tesseract")

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            output = "tesseract 5.5.0\n"
        elif "--list-langs" in argv:
            output = "List of available languages (1):\neng\n"
        else:
            now[0] = 6.0
            output = ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    reader = startup_ui.TesseractReader(monotonic=lambda: now[0], runner=run)
    with pytest.raises(startup_ui.StartupUIError, match="screen_capture_stale"):
        reader.read(Image.new("RGB", (10, 10)), captured_monotonic=0.0)
