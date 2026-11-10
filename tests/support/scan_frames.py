"""A valid schema-6 snapshot and its pixel rendering, for the scan-loop tests."""

from __future__ import annotations

from typing import Any

from PIL import Image

from crowley.gui.frames import PLAN_COUNT, encode_frame, pack_cells


def snapshot(sequence: int = 1) -> dict[str, Any]:
    return {
        "schema": 6,
        "boot": "boot-1",
        "sequence": sequence,
        "wallTime": 1_000,
        "uptimeMs": sequence,
        "context": {
            "addonVersion": "0.2.0",
            "interface": 16001,
            "clientBuild": "70450",
            "character": "Arthur",
            "realm": "Classic Beta PvE 2",
            "playerFaction": "Alliance",
            "zone": "Orgrimmar",
            "subzone": "",
            "locale": "enUS",
            "clientVersion": "1.60.1",
        },
        "house": {"key": "beta-normal-alliance", "source": "auto"},
        "plan": {
            "hash": "a" * 64,
            "count": PLAN_COUNT,
            "cadenceSeconds": 900,
            "sweepCapSeconds": 600,
            "initialPriceLevels": 20,
        },
        "ahOpen": True,
        "hover": {"kind": "none", "name": ""},
        "mailOpen": False,
        "executor": {
            "availability": "none",
            "queueId": "",
            "house": "",
            "character": "",
            "publishedArmed": False,
            "sessionArmed": False,
            "buttonVisible": False,
            "buttonName": "",
            "currentSeq": 0,
            "currentKind": "",
            "phase": "none",
            "inFlight": False,
            "clickReady": False,
            "workDone": 0,
            "workTotal": 0,
            "blocked": False,
            "latched": False,
            "ambiguous": False,
            "clickOrdinal": 0,
            "lastClickSource": "none",
            "resultCount": 0,
            "lastResult": {"seq": 0, "kind": "", "status": "none"},
        },
        "blockingUI": {
            "staticPopupAvailable": True,
            "staticPopupShown": False,
            "gameMenuAvailable": True,
            "gameMenuShown": False,
        },
        "auctioneer": {
            "targetPresent": False,
            "targetName": "",
            "rangeAvailable": False,
            "withinTradeRange": False,
            "bindingAvailable": True,
            "ctrlF12Action": "",
            "interactionMode": "any",
        },
        "scanner": {
            "phase": "ready",
            "activeSweepId": "",
            "startedAt": 0,
            "dueAt": 0,
            "ordinal": 0,
            "planned": PLAN_COUNT,
            "cursorItemID": 0,
            "scheduleFence": {"state": "armed", "reason": "", "command": "/crowley session fence"},
        },
        "lastSaved": {
            "memoryUid": "",
            "finishedAt": 0,
            "planned": 0,
            "attempted": 0,
            "failures": 0,
            "rows": 0,
            "persistence": "unknown",
        },
        "pending": {"ids": [], "count": 0, "cap": 6, "blocked": False, "reason": ""},
        "loaded": {"ackedIds": [], "generatedAt": 0},
        "chatEdit": {"active": False, "focused": False, "text": ""},
        "motion": {
            "position": {"x": 100.25, "y": 200.5, "z": 30.75, "instance": 1},
            "facing": 1.25,
            "speed": 0,
            "combat": False,
            "deadOrGhost": False,
            "bindings": {
                "moveForward": "W",
                "moveBackward": "S",
                "strafeLeft": "A",
                "strafeRight": "D",
                "turnLeft": "Q",
                "jump": "SPACE",
                "turnRight": "E",
            },
            "native": {
                "ctm": {
                    "accessor": "C_CVar.GetCVarBool",
                    "cvar": "autointeract",
                    "state": "disabled",
                    "reason": "",
                },
                "ground": {
                    "state": "grounded",
                    "falling": "false",
                    "flying": "false",
                    "swimming": "false",
                    "submerged": "false",
                    "mounted": "false",
                    "taxi": "false",
                    "reason": "",
                },
                "speedTuple": {
                    "current": 0,
                    "run": 0,
                    "flight": 0,
                    "swim": 0,
                    "unitStatus": "unverified",
                },
            },
        },
        "render": {
            "constructionSeconds": 0.001,
            "updateCount": 100,
            "updateP99Ms": 1,
            "textureCount": 2_048,
            "effectiveScale": 1,
            "cellSize": [2, 2],
            "luaHeap": {
                "method": "collectgarbage_count",
                "available": True,
                "beforeKiB": 1,
                "afterKiB": 2,
                "netKiB": 1,
            },
            "nativePhase": "normal",
            "nativeOrdinal": 0,
        },
    }


def render(value: dict[str, Any]) -> Image.Image:
    """The 256x256 canvas crop the addon would draw for *value*."""

    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    assert pixels is not None
    for row, bits in enumerate(pack_cells(encode_frame(value))):
        for column, bit in enumerate(bits):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    pixels[column * 2 + dx, row * 2 + dy] = color
    return image
