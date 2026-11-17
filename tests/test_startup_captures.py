"""Recorded UI regressions; no desktop access or input is performed."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from crowley_automation.battle_net import Box, locate_play_button
from crowley_automation.startup_ui import (
    TesseractReader,
    UIKind,
    classify_battle_net_ui,
    classify_battle_net_updates_capture,
    classify_character_select_capture,
)
from PIL import Image

_FIXTURES = Path(__file__).parent / "fixtures" / "startup"


@pytest.mark.process
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract 5/eng required")
def test_recorded_forever_launcher_verifies_selected_product_and_exact_play(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_THREAD_LIMIT", "1")
    manifest = json.loads((_FIXTURES / "provenance.json").read_text())
    case = next(case for case in manifest["cases"] if case["file"] == "battlenet-play.png")
    path = _FIXTURES / case["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case["file_sha256"]
    image = Image.open(path).convert("RGB")
    # This is offline qualification, not evidence authorizing current desktop input.
    document = TesseractReader(monotonic=lambda: 0.0).read(image)
    button = locate_play_button(
        image,
        Box(*case["window_xywh"]),
        words=document.words,
    )
    evidence = classify_battle_net_ui(
        document.words,
        image,
        expected_product="WoW: Forever",
        play_button=(button.left, button.top, button.width, button.height),
    )
    assert evidence.kind == UIKind.PLAY_READY
    assert evidence.control_text == "Play"
    assert (button.left, button.top, button.width, button.height) == (42, 142, 229, 52)


@pytest.mark.process
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract 5/eng required")
@pytest.mark.parametrize("filename", ["character-select.png", "character-select-hover.png"])
def test_recorded_character_controls_require_the_full_selected_display_name(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMP_THREAD_LIMIT", "1")
    manifest = json.loads((_FIXTURES / "provenance.json").read_text())
    case = next(case for case in manifest["cases"] if case["file"] == filename)
    path = _FIXTURES / case["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case["file_sha256"]
    # Only the recorded control pixels are real. The black surround is a synthetic
    # test background preserving source coordinates, not a complete captured screen.
    image = Image.new("RGB", tuple(case["source_size"]), "black")
    image.paste(Image.open(path).convert("RGB"), tuple(case["crop_xyxy"][:2]))
    # Historical image recognition has no live freshness authority. A fixed test
    # clock separates these OCR regressions from real capture-deadline tests.
    reader = TesseractReader(monotonic=lambda: 0.0)
    document = reader.read(image)
    evidence = classify_character_select_capture(
        reader, document, image, expected_selected_name=case["expected_selected_name"]
    )
    assert evidence.kind == UIKind.CHARACTER_SELECT
    assert evidence.control_text == "Enter World"
    assert evidence.control is not None
    left, top, width, height = evidence.control
    assert 840 <= left < left + width <= 1080
    assert 977 <= top < top + height <= 1027
    assert (
        classify_character_select_capture(reader, document, image, expected_selected_name="Dandune").kind
        == UIKind.UNKNOWN
    )


@pytest.mark.process
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract 5/eng required")
def test_recorded_release_information_panel_binds_heading_release_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_THREAD_LIMIT", "1")
    manifest = json.loads((_FIXTURES / "provenance.json").read_text())
    case = next(case for case in manifest["cases"] if case["file"] == "battlenet-updates.png")
    path = _FIXTURES / case["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case["file_sha256"]
    image = Image.open(path).convert("RGB")
    reader = TesseractReader(monotonic=lambda: 0.0)
    evidence = classify_battle_net_updates_capture(reader, reader.read(image), image)
    assert evidence.kind == UIKind.NEWS_MODAL
    assert evidence.reason == "verified_battle_net_updates_panel"
    assert evidence.control == (875, 7, 10, 10)
    assert evidence.context_fingerprint is not None

    without_heading = image.copy()
    without_heading.paste("black", (20, 24, 420, 89))
    assert (
        classify_battle_net_updates_capture(reader, reader.read(without_heading), without_heading).kind
        == UIKind.UNKNOWN
    )
