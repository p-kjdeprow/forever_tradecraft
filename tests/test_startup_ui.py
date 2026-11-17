from __future__ import annotations

import pytest
from crowley_automation.startup_ui import (
    OCRDocument,
    StartupUIError,
    TextWord,
    UIKind,
    classify_battle_net_ui,
    classify_battle_net_updates_capture,
    classify_character_select,
)
from PIL import Image, ImageDraw


def _word(text: str, left: int, top: int, *, line: int, width: int = 50, height: int = 20) -> TextWord:
    return TextWord(text, 96, left, top, width, height, (1, 1, 1, line))


class _UpdatesReader:
    def __init__(self, close_words: tuple[TextWord, ...] = (), *, stale_close: bool = False) -> None:
        self.close_words = close_words
        self.stale_close = stale_close
        self.captured: float | None = None

    def read(self, image: Image.Image, *, captured_monotonic: float | None = None) -> OCRDocument:
        assert image.size == (800, 130)
        self.captured = captured_monotonic
        return OCRDocument(
            (
                TextWord("Battle.net", 96, 12, 18, 120, 30, (1, 1, 1, 1)),
                TextWord("Updates", 96, 138, 18, 90, 30, (1, 1, 1, 1)),
            ),
            "header",
            captured_monotonic or 0.0,
            captured_monotonic or 0.0,
            image.size,
        )

    def read_single_character(
        self, image: Image.Image, *, captured_monotonic: float | None = None
    ) -> OCRDocument:
        assert image.size == (60, 60)
        assert image.mode == "RGB"
        assert captured_monotonic == self.captured
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((30, 30)) == (0, 0, 0)
        if self.stale_close:
            raise StartupUIError("screen_capture_stale")
        return OCRDocument(self.close_words, "close", 1.0, 1.1, image.size)


def _updates_image() -> Image.Image:
    image = Image.new("RGB", (900, 600), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((837, 0, 899, 24), fill=(150, 30, 30))
    draw.line((875, 7, 885, 17), fill=(245, 245, 245), width=2)
    draw.line((885, 7, 875, 17), fill=(245, 245, 245), width=2)
    return image


def _updates_document() -> OCRDocument:
    return OCRDocument(
        (
            _word("PATCH", 42, 122, line=1, width=58, height=25),
            _word("2.53.4", 101, 122, line=1, width=100, height=25),
            _word("10/01/2026", 43, 151, line=2, width=90, height=10),
        ),
        "body",
        1.0,
        1.1,
        (900, 600),
    )


def test_updates_close_uses_one_measured_single_character_glyph() -> None:
    reader = _UpdatesReader((_word("x", 14, 14, line=1, width=32, height=32),))
    evidence = classify_battle_net_updates_capture(reader, _updates_document(), _updates_image())  # type: ignore[arg-type]
    assert evidence.kind == UIKind.NEWS_MODAL
    assert evidence.control == (875, 7, 11, 11)
    assert evidence.control_text == "Close"


@pytest.mark.parametrize(
    "words",
    [
        (_word("+", 14, 14, line=1, width=32, height=32),),
        (_word("Oo", 14, 14, line=1, width=32, height=32),),
        (_word("Xx", 6, 6, line=1, width=54, height=51),),
        (_word("x", 14, 14, line=1, width=32, height=32), _word("x", 35, 14, line=2, width=32, height=32)),
        (TextWord("x", 84.9, 14, 14, 32, 32, (1, 1, 1, 1)),),
    ],
)
def test_updates_close_rejects_other_duplicate_or_low_confidence_glyphs(
    words: tuple[TextWord, ...],
) -> None:
    evidence = classify_battle_net_updates_capture(
        _UpdatesReader(words),
        _updates_document(),
        _updates_image(),  # type: ignore[arg-type]
    )
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "battle_net_updates_close_unverified"


def test_updates_close_rejects_stale_single_glyph_ocr() -> None:
    evidence = classify_battle_net_updates_capture(
        _UpdatesReader(stale_close=True),
        _updates_document(),
        _updates_image(),  # type: ignore[arg-type]
    )
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "battle_net_updates_close_unverified"


def test_marketing_and_desktop_toast_do_not_mask_selected_play() -> None:
    image = Image.new("RGB", (1920, 1080), (20, 20, 24))
    words = (
        _word("Firmware", 750, 50, line=1),
        _word("Updater", 815, 50, line=1),
        _word("Firmware", 700, 82, line=2),
        _word("update", 770, 82, line=2),
        _word("available", 830, 82, line=2),
        _word("Upgrade", 940, 310, line=3),
        _word("Available", 1010, 310, line=3),
        _word("WoW:", 192, 911, line=4, width=40, height=10),
        _word("Forever", 232, 911, line=4, width=51, height=10),
        _word("WoW:", 1050, 200, line=6, width=45),
        _word("Forever", 1095, 200, line=6, width=70),
        _word("Play", 260, 958, line=5, width=65, height=20),
    )
    evidence = classify_battle_net_ui(
        words,
        image,
        expected_product="WoW: Forever",
        play_button=(186, 942, 229, 52),
    )
    assert evidence.kind == UIKind.PLAY_READY
    assert evidence.control_text == "Play"


def test_wrong_selected_product_and_distant_expected_banner_do_not_authorize_play() -> None:
    words = (
        _word("WoW:", 192, 911, line=1, width=40, height=10),
        _word("Classic", 232, 911, line=1, width=51, height=10),
        _word("WoW:", 1050, 200, line=2, width=45),
        _word("Forever", 1095, 200, line=2, width=70),
    )
    evidence = classify_battle_net_ui(
        words,
        Image.new("RGB", (1920, 1080), "black"),
        expected_product="WoW: Forever",
        play_button=(186, 942, 229, 52),
    )
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "expected_product_not_visibly_selected"


def test_expected_product_as_distant_menu_or_body_text_does_not_authorize_play() -> None:
    words = (
        _word("WoW:", 1100, 500, line=1, width=45),
        _word("Forever", 1145, 500, line=1, width=70),
    )
    evidence = classify_battle_net_ui(
        words,
        Image.new("RGB", (1920, 1080), "black"),
        expected_product="WoW: Forever",
        play_button=(186, 942, 229, 52),
    )
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "expected_product_not_visibly_selected"


def test_news_heading_and_close_glyph_without_verified_panel_is_not_dismissed() -> None:
    image = Image.new("RGB", (1000, 700), "black")
    evidence = classify_battle_net_ui(
        (
            _word("What's", 300, 80, line=1),
            _word("New", 370, 80, line=1),
            _word("×", 900, 50, line=2, width=16, height=16),
        ),
        image,
    )
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "news_modal_panel_unverified"


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ((_word("Log", 300, 200, line=1), _word("In", 350, 200, line=1)), UIKind.LOGIN_REQUIRED),
        ((_word("Account", 300, 200, line=1), _word("Conflict", 380, 200, line=1)), UIKind.ACCOUNT_CONFLICT),
        ((_word("Downloading", 300, 200, line=1),), UIKind.UPDATE_BUSY),
    ],
)
def test_launcher_login_conflict_and_update_states_are_classified_without_controls(
    words: tuple[TextWord, ...], expected: UIKind
) -> None:
    evidence = classify_battle_net_ui(words, Image.new("RGB", (1920, 1080), "black"))
    assert evidence.kind == expected
    assert evidence.control is None


def test_character_select_requires_exact_centered_selected_name_and_enabled_button() -> None:
    image = Image.new("RGB", (1920, 1080), "black")
    ImageDraw.Draw(image).rectangle((840, 977, 1080, 1027), fill=(158, 8, 18))
    evidence = classify_character_select(
        (
            _word("Dandune", 830, 941, line=1, width=90),
            _word("Hillmont", 925, 941, line=1, width=105),
            _word("Dandune", 1567, 160, line=2, width=90),
            _word("Hillmont", 1657, 160, line=2, width=105),
            _word("Enter", 885, 990, line=3, width=65),
            _word("World", 952, 990, line=3, width=80),
        ),
        image,
        expected_selected_name="Dandune Hillmont",
    )
    assert evidence.kind == UIKind.CHARACTER_SELECT
    assert evidence.control_text == "Enter World"


def test_character_list_name_does_not_substitute_for_selected_name() -> None:
    image = Image.new("RGB", (1920, 1080), "black")
    ImageDraw.Draw(image).rectangle((840, 977, 1080, 1027), fill=(158, 8, 18))
    words = (
        _word("Dandune", 830, 941, line=1, width=90),
        _word("Dandune", 1567, 160, line=2, width=90),
        _word("Hillmont", 1657, 160, line=2, width=105),
        _word("Enter", 885, 990, line=3, width=65),
        _word("World", 952, 990, line=3, width=80),
    )
    evidence = classify_character_select(words, image, expected_selected_name="Dandune Hillmont")
    assert evidence.kind == UIKind.UNKNOWN
    assert evidence.reason == "selected_character_name_not_uniquely_identified"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Queue position 3", UIKind.QUEUE),
        ("Disconnected", UIKind.DISCONNECTED),
        ("Loading", UIKind.WORLD_LOADING),
    ],
)
def test_queue_disconnect_and_loading_states_have_no_enter_control(text: str, expected: UIKind) -> None:
    evidence = classify_character_select(
        (_word(text, 400, 400, line=1, width=180),),
        Image.new("RGB", (1920, 1080), "black"),
        expected_selected_name="Dandune Hillmont",
    )
    assert evidence.kind == expected
    assert evidence.control is None
