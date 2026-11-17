"""Strict screenshot evidence for the bounded startup controller.

Tesseract 5 with English TSV is the one supported OCR path.  OCR output is
validated before classification and is never copied into public status/logs.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps

MAX_OCR_BYTES = 4 * 1024 * 1024
MAX_OCR_ROWS = 20_000
MAX_IMAGE_PIXELS = 33_554_432
MAX_CAPTURE_AGE_SECONDS = 5.0
MIN_CONTROL_CONFIDENCE = 85.0
_PRODUCT_PLAY_MAX_LEFT_DELTA = 12
_PRODUCT_PLAY_MAX_GAP = 24
_UPDATES_CLOSE_GLYPH_ROI = (870, 2, 890, 22)
_UPDATES_CLOSE_GLYPH_SCALE = 3
_TSV_HEADER = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


class StartupUIError(RuntimeError):
    """OCR or positive UI evidence is unavailable or malformed."""


class UIKind(StrEnum):
    UNKNOWN = "unknown"
    LOGIN_REQUIRED = "login_required"
    ACCOUNT_CONFLICT = "account_conflict"
    UPDATE_BUSY = "update_busy"
    NEWS_MODAL = "news_modal"
    PLAY_READY = "play_ready"
    CHARACTER_SELECT = "character_select"
    WORLD_LOADING = "world_loading"
    QUEUE = "queue"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class TextWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int, int]

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[float, float]:
        return self.left + self.width / 2, self.top + self.height / 2


@dataclass(frozen=True, slots=True)
class OCRDocument:
    words: tuple[TextWord, ...]
    image_sha256: str
    captured_monotonic: float
    completed_monotonic: float
    image_size: tuple[int, int]


@dataclass(frozen=True, slots=True)
class UIEvidence:
    kind: UIKind
    reason: str
    control: tuple[int, int, int, int] | None = None
    control_text: str | None = None
    context_fingerprint: str | None = None


class TesseractReader:
    """One checked Tesseract 5/English TSV reader with no engine fallback."""

    def __init__(
        self,
        *,
        executable: str = "tesseract",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        temp_root: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._executable = executable
        self._runner = runner
        self._clock = monotonic
        self._temp_root = temp_root
        self._environment = environment

    def read(self, image: Image.Image, *, captured_monotonic: float | None = None) -> OCRDocument:
        return self._read(image, captured_monotonic=captured_monotonic, psm=11)

    def read_single_character(
        self, image: Image.Image, *, captured_monotonic: float | None = None
    ) -> OCRDocument:
        """Read one measured glyph with Tesseract's single-character segmentation."""

        return self._read(image, captured_monotonic=captured_monotonic, psm=10)

    def _read(
        self,
        image: Image.Image,
        *,
        captured_monotonic: float | None,
        psm: Literal[10, 11],
    ) -> OCRDocument:
        captured = self._clock() if captured_monotonic is None else captured_monotonic
        if not math.isfinite(captured) or self._clock() - captured > MAX_CAPTURE_AGE_SECONDS:
            raise StartupUIError("screen_capture_stale")
        if (
            image.mode not in {"RGB", "RGBA"}
            or image.width <= 0
            or image.height <= 0
            or image.width * image.height > MAX_IMAGE_PIXELS
        ):
            raise StartupUIError("screen_capture_invalid")
        executable = shutil.which(self._executable, path=(self._environment or {}).get("PATH"))
        if executable is None:
            raise StartupUIError("tesseract_missing")
        environment = self._environment
        version = self._run([executable, "--version"], timeout=3.0, environment=environment)
        match = re.match(r"^tesseract\s+([0-9]+)\.[0-9]+(?:\.[0-9]+)?(?:\s|$)", version.stdout)
        if version.returncode != 0 or match is None or match.group(1) != "5":
            raise StartupUIError("tesseract_version_unsupported")
        languages = self._run([executable, "--list-langs"], timeout=3.0, environment=environment)
        if languages.returncode != 0 or "eng" not in languages.stdout.splitlines()[1:]:
            raise StartupUIError("tesseract_english_missing")

        scratch: tempfile.TemporaryDirectory[str] | None = None
        try:
            scratch = tempfile.TemporaryDirectory(prefix="startup-ocr-", dir=self._temp_root)
            path = Path(scratch.name) / "capture.png"
            image.convert("RGB").save(path, format="PNG", optimize=False)
            payload = path.read_bytes()
            if len(payload) > 32 * 1024 * 1024:
                raise StartupUIError("screen_capture_oversized")
            digest = hashlib.sha256(payload).hexdigest()
            completed = self._run(
                [executable, str(path), "stdout", "-l", "eng", "--psm", str(psm), "tsv"],
                timeout=10.0,
                environment=environment,
            )
            finished = self._clock()
        except StartupUIError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise StartupUIError("tesseract_capture_failed") from error
        finally:
            if scratch is not None:
                scratch.cleanup()
        if completed.returncode != 0:
            raise StartupUIError("tesseract_read_failed")
        if finished - captured > MAX_CAPTURE_AGE_SECONDS:
            raise StartupUIError("screen_capture_stale")
        words = parse_tsv(completed.stdout, image.size)
        return OCRDocument(words, digest, captured, finished, image.size)

    def _run(
        self, argv: Sequence[str], *, timeout: float, environment: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                list(argv),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise StartupUIError("tesseract_unavailable") from error


def parse_tsv(raw: str, image_size: tuple[int, int]) -> tuple[TextWord, ...]:
    """Validate the entire bounded TSV document before returning word rows."""

    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > MAX_OCR_BYTES:
        raise StartupUIError("tesseract_output_oversized")
    rows = raw.splitlines()
    if not rows or tuple(rows[0].split("\t")) != _TSV_HEADER or len(rows) > MAX_OCR_ROWS:
        raise StartupUIError("tesseract_output_malformed")
    words: list[TextWord] = []
    width, height = image_size
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) != len(_TSV_HEADER):
            raise StartupUIError("tesseract_output_malformed")
        try:
            level, page, block, paragraph, line, word_index, left, top, box_width, box_height = (
                int(cell) for cell in cells[:10]
            )
            confidence = float(cells[10])
        except ValueError as error:
            raise StartupUIError("tesseract_output_malformed") from error
        text = cells[11]
        if not math.isfinite(confidence):
            raise StartupUIError("tesseract_output_malformed")
        if level != 5:
            if text or confidence != -1:
                raise StartupUIError("tesseract_output_malformed")
            continue
        if (
            page != 1
            or min(block, paragraph, line, word_index) < 1
            or min(left, top) < 0
            or min(box_width, box_height) <= 0
            or left + box_width > width
            or top + box_height > height
            or not 0 <= confidence <= 100
            or not text
            or len(text) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in text)
        ):
            raise StartupUIError("tesseract_output_malformed")
        words.append(
            TextWord(text, confidence, left, top, box_width, box_height, (page, block, paragraph, line))
        )
    return tuple(words)


def line_texts(words: Sequence[TextWord], *, minimum_confidence: float = 0) -> dict[tuple[int, ...], str]:
    grouped: dict[tuple[int, ...], list[TextWord]] = {}
    for word in words:
        if word.confidence >= minimum_confidence:
            grouped.setdefault(word.line, []).append(word)
    return {
        key: " ".join(word.text for word in sorted(items, key=lambda item: item.left))
        for key, items in grouped.items()
    }


def classify_battle_net_ui(
    words: Sequence[TextWord],
    image: Image.Image,
    *,
    expected_product: str = "World of Warcraft",
    play_button: tuple[int, int, int, int] | None = None,
) -> UIEvidence:
    """Classify only exact, positively evidenced launcher UI states."""

    lines = tuple(line_texts(words, minimum_confidence=MIN_CONTROL_CONFIDENCE).values())
    normalized = {_normalize(line) for line in lines}
    joined = "\n".join(normalized)
    if any(label in normalized for label in {"account conflict", "account already active"}):
        return UIEvidence(UIKind.ACCOUNT_CONFLICT, "account_conflict_visible")
    if any(label in normalized for label in {"log in", "sign in", "login required", "log in to continue"}):
        return UIEvidence(UIKind.LOGIN_REQUIRED, "login_required_visible")
    if {"email", "password"}.issubset({word.text.casefold() for word in words}):
        return UIEvidence(UIKind.LOGIN_REQUIRED, "login_required_visible")
    if any(
        text in joined
        for text in (
            "updating",
            "installing",
            "downloading",
            "update required",
            "waiting for update",
            "update in progress",
        )
    ):
        return UIEvidence(UIKind.UPDATE_BUSY, "update_or_install_visible")
    if any(_normalize(line) in {"what's new", "whats new"} for line in lines):
        # A heading and a nearby glyph do not establish that this is a modal
        # surface. Keep this read-only until a real captured panel layout can
        # prove both the panel boundary and its unique close control.
        return UIEvidence(UIKind.UNKNOWN, "news_modal_panel_unverified")
    product_visible = False
    if play_button is not None:
        play_left, play_top, _play_width, _play_height = play_button
        expected_line = _normalize(expected_product)
        qualified_product_boxes = []
        for line_id, line_text in line_texts(words, minimum_confidence=MIN_CONTROL_CONFIDENCE).items():
            if _normalize(line_text) != expected_line:
                continue
            product_box = _word_box(
                [word for word in words if word.line == line_id and word.confidence >= MIN_CONTROL_CONFIDENCE]
            )
            if product_box is None:
                continue
            product_left, product_top, _product_width, product_height = product_box
            gap = play_top - (product_top + product_height)
            if (
                abs(product_left - play_left) <= _PRODUCT_PLAY_MAX_LEFT_DELTA
                and 0 <= gap <= _PRODUCT_PLAY_MAX_GAP
            ):
                qualified_product_boxes.append(product_box)
        product_visible = len(qualified_product_boxes) == 1
    if play_button is not None and product_visible:
        return UIEvidence(UIKind.PLAY_READY, "exact_play_label_and_blue_control", play_button, "Play")
    if play_button is not None:
        return UIEvidence(UIKind.UNKNOWN, "expected_product_not_visibly_selected")
    return UIEvidence(UIKind.UNKNOWN, "launcher_ui_unrecognized")


def classify_battle_net_updates_capture(
    reader: TesseractReader,
    document: OCRDocument,
    image: Image.Image,
) -> UIEvidence:
    """Recognize only the measured 900x600 release-information child panel."""

    if image.size != (900, 600):
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_geometry_unverified")
    try:
        header = image.crop((20, 24, 420, 89)).resize((800, 130), Image.Resampling.LANCZOS)
        header_document = reader.read(header, captured_monotonic=document.captured_monotonic)
    except StartupUIError:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_header_ocr_unavailable")
    header_words = _map_region_words(header_document.words, offset=(20, 24), scale=2, page=2)
    header_lines = line_texts(header_words, minimum_confidence=MIN_CONTROL_CONFIDENCE)
    headings = [(key, text) for key, text in header_lines.items() if _normalize(text) == "battle.net updates"]
    if len(headings) != 1:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_heading_unverified")
    heading_words = [
        word
        for word in header_words
        if word.line == headings[0][0] and word.confidence >= MIN_CONTROL_CONFIDENCE
    ]
    heading_box = _word_box(heading_words)
    if heading_box is None or heading_box[0] > 80 or not 20 <= heading_box[1] <= 65:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_heading_layout_unverified")

    body_lines = line_texts(document.words, minimum_confidence=MIN_CONTROL_CONFIDENCE)
    patches = [
        (key, text)
        for key, text in body_lines.items()
        if re.fullmatch(r"patch \d+\.\d+\.\d+", _normalize(text)) is not None
    ]
    dates = [
        (key, text)
        for key, text in body_lines.items()
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", _normalize(text)) is not None
    ]
    if len(patches) != 1 or len(dates) != 1:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_release_layout_unverified")
    patch_words = [
        word
        for word in document.words
        if word.line == patches[0][0] and word.confidence >= MIN_CONTROL_CONFIDENCE
    ]
    date_words = [
        word
        for word in document.words
        if word.line == dates[0][0] and word.confidence >= MIN_CONTROL_CONFIDENCE
    ]
    patch_box = _word_box(patch_words)
    date_box = _word_box(date_words)
    if patch_box is None or date_box is None:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_release_layout_unverified")
    if (
        patch_box[0] > image.width * 0.10
        or patch_box[1] < image.height * 0.12
        or patch_box[1] > image.height * 0.40
        or abs(patch_box[0] - date_box[0]) > image.width * 0.05
        or not 0 <= date_box[1] - (patch_box[1] + patch_box[3]) <= 40
    ):
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_release_layout_unverified")

    close_image = _updates_close_glyph_image(image)
    try:
        close_document = reader.read_single_character(
            close_image, captured_monotonic=document.captured_monotonic
        )
    except StartupUIError:
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_close_unverified")
    close_words = _map_region_words(
        close_document.words,
        offset=(_UPDATES_CLOSE_GLYPH_ROI[0], _UPDATES_CLOSE_GLYPH_ROI[1]),
        scale=_UPDATES_CLOSE_GLYPH_SCALE,
        page=3,
    )
    close_box = _word_box(close_words)
    if (
        len(close_words) != 1
        or close_box is None
        or _normalize(close_words[0].text) not in {"x", "×"}
        or close_words[0].confidence < MIN_CONTROL_CONFIDENCE
        or not 8 <= close_box[2] <= 13
        or not 8 <= close_box[3] <= 13
        or not 875 <= close_words[0].center[0] <= 886
        or not 7 <= close_words[0].center[1] <= 18
    ):
        return UIEvidence(UIKind.UNKNOWN, "battle_net_updates_close_unverified")
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                "battle.net updates",
                _normalize(patches[0][1]),
                _normalize(dates[0][1]),
                "panel:900x600",
                "heading:top-left",
                "release:patch-and-date-left-aligned",
                "close:unique-upper-right",
            )
        ).encode("utf-8")
    ).hexdigest()
    return UIEvidence(
        UIKind.NEWS_MODAL,
        "verified_battle_net_updates_panel",
        close_box,
        "Close",
        fingerprint,
    )


def _updates_close_glyph_image(image: Image.Image) -> Image.Image:
    left, top, right, bottom = _UPDATES_CLOSE_GLYPH_ROI
    green = image.crop((left, top, right, bottom)).convert("RGB").getchannel("G")
    midpoint = [0 if pixel >= 128 else 255 for pixel in range(256)]
    normalized = ImageOps.autocontrast(green, cutoff=0).point(midpoint)
    size = (
        (right - left) * _UPDATES_CLOSE_GLYPH_SCALE,
        (bottom - top) * _UPDATES_CLOSE_GLYPH_SCALE,
    )
    return normalized.resize(size, Image.Resampling.LANCZOS).convert("RGB")


def classify_character_select(
    words: Sequence[TextWord],
    image: Image.Image,
    *,
    expected_selected_name: str,
) -> UIEvidence:
    """Require the exact selected-name display, its central layout and enabled button."""

    lines = line_texts(words, minimum_confidence=MIN_CONTROL_CONFIDENCE)
    normalized_lines = {key: _normalize(value) for key, value in lines.items()}
    normalized = tuple(normalized_lines.values())
    if any(
        "queue" in line and any(token in line for token in ("position", "wait", "queue"))
        for line in normalized
    ):
        return UIEvidence(UIKind.QUEUE, "login_queue_visible")
    if any(
        token in line
        for line in normalized
        for token in ("disconnected", "connection lost", "unable to connect")
    ):
        return UIEvidence(UIKind.DISCONNECTED, "disconnect_visible")
    if any(
        token in line
        for line in normalized
        for token in ("loading", "retrieving characters", "retrieving character list")
    ):
        return UIEvidence(UIKind.WORLD_LOADING, "loading_or_character_list_visible")

    width, height = image.size
    selected: list[tuple[TextWord, ...]] = []
    for key, text in normalized_lines.items():
        if text != _normalize(expected_selected_name):
            continue
        group = [word for word in words if word.line == key and word.confidence >= MIN_CONTROL_CONFIDENCE]
        if not group:
            continue
        box = _word_box(group)
        if box is None:
            continue
        center_x, center_y = box[0] + box[2] / 2, box[1] + box[3] / 2
        if width * 0.30 <= center_x <= width * 0.70 and height * 0.50 <= center_y <= height * 0.93:
            selected.append(tuple(group))
    if len(selected) != 1:
        return UIEvidence(UIKind.UNKNOWN, "selected_character_name_not_uniquely_identified")
    enter_lines = [key for key, text in normalized_lines.items() if text == "enter world"]
    if len(enter_lines) != 1:
        return UIEvidence(UIKind.UNKNOWN, "enter_world_control_not_unique")
    enter_words = [
        word for word in words if word.line == enter_lines[0] and word.confidence >= MIN_CONTROL_CONFIDENCE
    ]
    button_box = _word_box(enter_words)
    if button_box is None:
        return UIEvidence(UIKind.UNKNOWN, "enter_world_control_malformed")
    selected_box = _word_box(selected[0])
    if selected_box is None:
        return UIEvidence(UIKind.UNKNOWN, "selected_character_name_malformed")
    char_x = selected_box[0] + selected_box[2] / 2
    char_y = selected_box[1] + selected_box[3] / 2
    button_x, button_y = button_box[0] + button_box[2] / 2, button_box[1] + button_box[3] / 2
    if (
        abs(char_x - width / 2) > width * 0.20
        or abs(button_x - width / 2) > width * 0.20
        or char_y >= button_box[1]
        or button_y < height * 0.55
        or not _looks_enabled(image, button_box)
    ):
        return UIEvidence(UIKind.UNKNOWN, "enter_world_control_not_enabled_or_layout_unverified")
    return UIEvidence(
        UIKind.CHARACTER_SELECT,
        "expected_selected_name_and_enabled_enter_world",
        button_box,
        "Enter World",
    )


def classify_character_select_capture(
    reader: TesseractReader,
    document: OCRDocument,
    image: Image.Image,
    *,
    expected_selected_name: str,
) -> UIEvidence:
    """Read the measured 1920x1080 name and button crops, then classify exactly."""

    if image.size != (1920, 1080):
        return UIEvidence(UIKind.UNKNOWN, "character_select_capture_invalid")
    try:
        name_region = _gold_text_region(image.crop((820, 930, 1100, 973)))
        name_enlarged = name_region.resize(
            (name_region.width * 4, name_region.height * 4), Image.Resampling.LANCZOS
        )
        name_document = reader.read(name_enlarged, captured_monotonic=document.captured_monotonic)
        enter_region = image.crop((880, 986, 1040, 1017))
        enter_enlarged = enter_region.resize(
            (enter_region.width * 4, enter_region.height * 4), Image.Resampling.LANCZOS
        )
        enter_document = reader.read(enter_enlarged, captured_monotonic=document.captured_monotonic)
    except StartupUIError:
        return UIEvidence(UIKind.UNKNOWN, "character_select_region_ocr_unavailable")
    original_words = tuple(
        word
        for word in document.words
        if not (
            _box_intersects((word.left, word.top, word.width, word.height), (820, 930, 280, 43))
            or _box_intersects((word.left, word.top, word.width, word.height), (880, 986, 160, 31))
        )
    )
    mapped = (
        *_map_region_words(name_document.words, offset=(820, 930), scale=4, page=2),
        *_map_region_words(enter_document.words, offset=(880, 986), scale=4, page=3),
    )
    return classify_character_select(
        (*original_words, *mapped),
        image,
        expected_selected_name=expected_selected_name,
    )


def _gold_text_region(image: Image.Image) -> Image.Image:
    """Isolate measured yellow name glyph pixels without OCR or fuzzy matching."""

    source = image.convert("RGB")
    pixels = source.load()
    if pixels is None:
        raise StartupUIError("character_select_capture_invalid")
    target = Image.new("RGB", source.size, "white")
    output = target.load()
    if output is None:
        raise StartupUIError("character_select_capture_invalid")
    for y in range(source.height):
        for x in range(source.width):
            pixel = pixels[x, y]
            if not isinstance(pixel, tuple) or len(pixel) < 3:
                raise StartupUIError("character_select_capture_invalid")
            red, green, blue = pixel[:3]
            output[x, y] = (0, 0, 0) if red > 160 and green > 140 and blue < 110 else (255, 255, 255)
    return target


def _map_region_words(
    words: Sequence[TextWord], *, offset: tuple[int, int], scale: int, page: int
) -> tuple[TextWord, ...]:
    return tuple(
        TextWord(
            word.text,
            word.confidence,
            offset[0] + round(word.left / scale),
            offset[1] + round(word.top / scale),
            max(1, round(word.width / scale)),
            max(1, round(word.height / scale)),
            (page, *word.line[1:]),
        )
        for word in words
    )


def _box_intersects(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    left, top, width, height = first
    other_left, other_top, other_width, other_height = second
    return (
        left < other_left + other_width
        and other_left < left + width
        and top < other_top + other_height
        and other_top < top + height
    )


def _looks_enabled(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """Require a chromatic, high-contrast control patch; gray disabled text fails."""

    left, top, width, height = box
    pad_x, pad_y = max(4, height), max(3, height // 2)
    bounds = (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(image.width, left + width + pad_x),
        min(image.height, top + height + pad_y),
    )
    patch = image.convert("RGB").crop(bounds)
    pixels = patch.load()
    if pixels is None or patch.width * patch.height < 16:
        return False
    saturated = contrast = 0
    total = patch.width * patch.height
    for y in range(patch.height):
        for x in range(patch.width):
            pixel = pixels[x, y]
            if not isinstance(pixel, tuple) or len(pixel) < 3:
                return False
            red, green, blue = pixel[:3]
            if max(red, green, blue) >= 70 and max(red, green, blue) - min(red, green, blue) >= 28:
                saturated += 1
            if max(red, green, blue) - min(red, green, blue) >= 20:
                contrast += 1
    return saturated / total >= 0.12 and contrast / total >= 0.18


def _word_box(words: Sequence[TextWord]) -> tuple[int, int, int, int] | None:
    if not words:
        return None
    left = min(word.left for word in words)
    top = min(word.top for word in words)
    right = max(word.right for word in words)
    bottom = max(word.bottom for word in words)
    return left, top, right - left, bottom - top


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


__all__ = [
    "OCRDocument",
    "StartupUIError",
    "TesseractReader",
    "TextWord",
    "UIEvidence",
    "UIKind",
    "classify_battle_net_ui",
    "classify_battle_net_updates_capture",
    "classify_character_select",
    "classify_character_select_capture",
    "line_texts",
    "parse_tsv",
]
