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


def test_single_character_reader_uses_psm10_without_character_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup_ui.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/tesseract")
    calls: list[list[str]] = []
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight"
        "\tconf\ttext\n5\t1\t1\t1\t1\t1\t14\t14\t32\t32\t93.35\tx\n"
    )

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        output = (
            "tesseract 5.5.0\n"
            if "--version" in argv
            else "List of available languages (1):\neng\n"
            if "--list-langs" in argv
            else tsv
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    reader = startup_ui.TesseractReader(monotonic=lambda: 0.0, runner=run)
    document = reader.read_single_character(Image.new("RGB", (60, 60)), captured_monotonic=0.0)
    command = calls[-1]
    assert command[command.index("--psm") + 1] == "10"
    assert "tessedit_char_whitelist" not in command
    assert [(word.text, word.confidence) for word in document.words] == [("x", 93.35)]
