"""Minimal independent scanner/game frames and their pixel screenshots."""

from __future__ import annotations

from typing import Any

from PIL import Image

from crowley.gui.frames import PLAN_COUNT, encode_frame, pack_cells

DISPLAY = (1920, 1080)
SCANNER_CROP = (80, 48, 256, 256)
AUTOMATION_CROP = (352, 48, 256, 256)


def snapshot(sequence: int = 1) -> dict[str, Any]:
    """Scanner publication, without any game-control fields."""
    return {
        "schema": 1,
        "kind": "scanner",
        "boot": "scanner-boot-1",
        "sequence": sequence,
        "wallTime": 1_000,
        "uptimeMs": sequence,
        "addonVersion": "0.3.0",
        "house": {"key": "beta-normal-alliance", "source": "auto"},
        "plan": {
            "hash": "a" * 64,
            "count": PLAN_COUNT,
            "cadenceSeconds": 900,
            "sweepCapSeconds": 600,
            "initialPriceLevels": 20,
        },
        "scanner": {
            "phase": "scheduled",
            "startedAt": 0,
            "dueAt": 1_900,
            "ordinal": 0,
            "planned": PLAN_COUNT,
            "cursorItemID": 0,
            "reason": "",
        },
        "lastSaved": {
            "memoryUid": "",
            "finishedAt": 0,
            "planned": 0,
            "attempted": 0,
            "failures": 0,
            "rows": 0,
        },
        "pending": {"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": ""},
        "loaded": {"ackedIds": [], "generatedAt": 0},
    }


def automation_snapshot(sequence: int = 1) -> dict[str, Any]:
    """Game facts, independent of a Crowley scan plan."""
    return {
        "schema": 1,
        "kind": "automation",
        "boot": "automation-boot-1",
        "sequence": sequence,
        "wallTime": 1_000,
        "uptimeMs": sequence,
        "context": {
            "addonVersion": "0.1.0",
            "interface": 16001,
            "clientBuild": "70450",
            "character": "Arthur",
            "realm": "Classic Beta PvE 2",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
        },
        "ahOpen": True,
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": True,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "chatEdit": {"active": False, "focused": False, "text": ""},
    }


def render(value: dict[str, Any]) -> Image.Image:
    """Render one validated status frame as its 256x256 pixel crop."""
    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    assert pixels is not None
    for row, bits in enumerate(pack_cells(encode_frame(value, expected_kind=value["kind"]))):
        for column, bit in enumerate(bits):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    pixels[column * 2 + dx, row * 2 + dy] = color
    return image


def render_screen(
    scanner: dict[str, Any] | None = None,
    automation: dict[str, Any] | None = None,
    *,
    display: tuple[int, int] = DISPLAY,
    scanner_crop: tuple[int, int, int, int] = SCANNER_CROP,
    automation_crop: tuple[int, int, int, int] = AUTOMATION_CROP,
) -> Image.Image:
    screen = Image.new("RGB", display, (17, 17, 17))
    if scanner is not None:
        screen.paste(render(scanner), (scanner_crop[0], scanner_crop[1]))
    if automation is not None:
        screen.paste(render(automation), (automation_crop[0], automation_crop[1]))
    return screen
