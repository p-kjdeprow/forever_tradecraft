"""Battle.net Play button locator tests on synthetic screenshots."""

from __future__ import annotations

import pytest
from crowley_automation.battle_net import (
    BattleNetPlayError,
    Box,
    _safe_control_revalidation,
    inner_target,
    locate_play_button,
)
from PIL import Image, ImageDraw, ImageFont

PLAY_BLUE = (0, 116, 224)
WINDOW = Box(154, 70, 1664, 940)


def _word(text: str, left: int = 282, top: int = 960, line: int = 1):
    from crowley_automation.startup_ui import TextWord

    return TextWord(text, 95, left, top, 50, 20, (1, 1, 1, line))


def _screen() -> Image.Image:
    return Image.new("RGB", (1920, 1080), (20, 20, 24))


def _rect(
    image: Image.Image,
    left: int,
    top: int,
    width: int,
    height: int,
    color=PLAY_BLUE,
    label: str | None = "Play",
) -> None:
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, top, left + width - 1, top + height - 1), fill=color)
    if label:
        font = ImageFont.load_default(size=26)
        draw.text((left + width / 2, top + height / 2), label, fill=(255, 255, 255), font=font, anchor="mm")


def test_locates_play_button_beside_gear_button() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 426, 942, 40, 55, label=None)  # gear button across a dark gap
    assert locate_play_button(image, WINDOW, words=(_word("Play"),)) == Box(192, 942, 230, 55)


def test_inner_target_is_the_centred_sixty_percent() -> None:
    inner = inner_target(Box(192, 942, 230, 55))
    assert (inner.width, inner.height) == (138, 33)
    assert inner.left == 192 + (230 - 138) // 2
    assert inner.top == 942 + (55 - 33) // 2


def test_hover_bbox_may_shift_slightly_only_if_endpoint_stays_inside_safe_target() -> None:
    original = Box(192, 942, 230, 55)
    endpoint = (306, 968)
    assert _safe_control_revalidation(original, Box(195, 940, 229, 58), endpoint)
    assert not _safe_control_revalidation(original, Box(201, 942, 230, 55), endpoint)
    assert not _safe_control_revalidation(original, Box(400, 942, 230, 55), endpoint)


def test_missing_button_fails() -> None:
    with pytest.raises(BattleNetPlayError, match="no blue Play button"):
        locate_play_button(_screen(), WINDOW, words=())


def test_blue_outside_the_bottom_left_quarter_is_ignored() -> None:
    image = _screen()
    _rect(image, 1000, 942, 230, 55)
    _rect(image, 192, 300, 230, 55)
    with pytest.raises(BattleNetPlayError, match="no blue Play button"):
        locate_play_button(image, WINDOW, words=())


def test_another_blue_button_without_the_play_label_is_ignored() -> None:
    # Live 2026-11-09: a second plausible blue region made a relaunch fail.
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 192, 700, 230, 55, label="Play Now")
    assert locate_play_button(
        image,
        WINDOW,
        words=(
            _word("Play"),
            _word("Play", left=282, top=720, line=2),
            _word("Now", left=340, top=720, line=2),
        ),
    ) == Box(192, 942, 230, 55)


def test_two_plausible_buttons_are_ambiguous() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 192, 700, 230, 55)
    with pytest.raises(BattleNetPlayError, match="ambiguous"):
        locate_play_button(image, WINDOW, words=(_word("Play"), _word("Play", top=720, line=2)))


@pytest.mark.parametrize("size", [(100, 55), (230, 20), (500, 55), (230, 200)])
def test_implausible_size_fails(size: tuple[int, int]) -> None:
    image = _screen()
    _rect(image, 192, 800, *size)
    with pytest.raises(BattleNetPlayError, match="implausible"):
        locate_play_button(image, WINDOW, words=())


def test_tiny_blue_specks_are_not_candidates() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 400, 700, 5, 5, label=None)
    assert locate_play_button(image, WINDOW, words=(_word("Play"),)) == Box(192, 942, 230, 55)


@pytest.mark.parametrize("label", ["Stop", "Help", "Open", "News"])
def test_adversarial_blue_button_labels_are_rejected(label: str) -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55, label=None)
    with pytest.raises(BattleNetPlayError, match="exact centered Play label"):
        locate_play_button(image, WINDOW, words=(_word(label, left=280, top=960),))
