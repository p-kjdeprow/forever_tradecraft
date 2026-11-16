"""Standalone desktop observation, presence, probe, and Battle.net commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence

from crowley_common.frames import MAGIC, PixelDecoder, ProtocolError
from crowley_common.platform.paths import XdgDirs

from . import battle_net
from .config import AutomationConfig, AutomationConfigError, load_config
from .desktop_observe import (
    ObserveError,
    ProcProcessSource,
    ScreenCanvas,
    X11Window,
    crop_canvas,
    x11_environment,
    x11_runner,
)
from .gui.capture import CaptureError
from .gui.input_tool import InputToolError
from .gui.pointer import ExecutorError, PointerExecutionError
from .gui.process import OwnedGameMatcher
from .presence import presence_ok
from .runtime import maintenance_blocker

PROBE_FRAME = 0
PROBE_NO_FRAME = 1
PROBE_ERROR = 3
MAX_FRAME_AGE_SECONDS = 3
FUTURE_FRAME_TOLERANCE_SECONDS = 1


class DesktopObserveError(RuntimeError):
    """The desktop state could not be safely observed."""


def _has_frame_signature(canvas: object) -> bool:
    """Check the frame magic encoded in the first 32 physical status cells."""

    try:
        width, height = canvas.size  # type: ignore[attr-defined]
        if (width, height) != (256, 256):
            raise DesktopObserveError("automation canvas crop is not 256x256")
        for byte_index, value in enumerate(MAGIC):
            for bit_index in range(8):
                cell = byte_index * 8 + bit_index
                left, top = (cell % 128) * 2, (cell // 128) * 2
                samples: list[int] = []
                for dy in range(2):
                    for dx in range(2):
                        red, green, blue, *_ = canvas.getpixel((left + dx, top + dy))  # type: ignore[attr-defined]
                        luminance = (299 * red + 587 * green + 114 * blue) // 1000
                        if luminance not in (0, 255):
                            return False
                        samples.append(luminance)
                expected = 255 if (value >> bit_index) & 1 else 0
                if any(sample != expected for sample in samples):
                    return False
        return True
    except DesktopObserveError:
        raise
    except (AttributeError, IndexError, TypeError, ValueError, OSError) as exc:
        raise DesktopObserveError(f"could not inspect automation canvas header: {exc}") from exc


def _matcher(config: AutomationConfig) -> OwnedGameMatcher:
    return OwnedGameMatcher.for_game(
        game_executable=config.game_executable,
        game_argv0=config.game_argv0,
        wine_prefix=config.wine_prefix,
    )


def _frame(config: AutomationConfig):
    env = x11_environment(config.display)
    process = ProcProcessSource(_matcher(config))
    pids = process.pids()
    if not pids:
        raise DesktopObserveError("no owned game process")
    window = X11Window(config.window_class, config.window_title, x11_runner(env)).observe(pids)
    if not window.ok or window.xid is None:
        raise DesktopObserveError(f"game window is not valid: {window.detail}")
    dirs = XdgDirs.from_environ(os.environ)
    image = ScreenCanvas(dirs.crowley_runtime / "automation-capture").capture(window.display)
    capture_finished = time.time()
    canvas = crop_canvas(image, window.display, config.canvas_crop, "automation canvas")
    if not _has_frame_signature(canvas):
        return window, None
    decoder = PixelDecoder(expected_kind="automation", crop=config.canvas_crop)
    identity = f"{','.join(map(str, pids))}:{window.xid}"
    try:
        sample = decoder.decode(canvas, monotonic=time.monotonic(), window_identity=identity)
    except ProtocolError as exc:
        raise DesktopObserveError(f"automation canvas frame is corrupt: {exc}") from exc
    frame = sample.frame
    if frame is None:
        return window, None
    snapshot = frame.snapshot.value
    wall_time = snapshot["wallTime"]
    if (
        wall_time < capture_finished - MAX_FRAME_AGE_SECONDS
        or wall_time > capture_finished + FUTURE_FRAME_TOLERANCE_SECONDS
    ):
        raise DesktopObserveError("automation publication is stale or future-dated")
    context = snapshot["context"]
    expected = {
        "character": config.character,
        "realm": config.realm,
        "playerFaction": config.faction,
        "clientBuild": config.build.rsplit(".", 1)[1],
        "interface": config.interface,
        "nativeRegion": config.native_region,
    }
    actual = {
        "character": context["character"],
        "realm": context["realm"],
        "playerFaction": context["playerFaction"],
        "clientBuild": context["clientBuild"],
        "interface": context["interface"],
        "nativeRegion": context["nativeRegion"],
    }
    if actual != expected:
        raise DesktopObserveError("automation publication does not match configured game identity")
    return window, snapshot


def _observe(_args: argparse.Namespace) -> int:
    config = load_config()
    window, snapshot = _frame(config)
    result: dict[str, object] = {
        "schema": "crowley.desktop-observation.v1",
        "processAlive": True,
        "windowValid": window.ok,
        "windowFocused": window.focused,
        "window": window.xid,
        "frameDecoded": snapshot is not None,
    }
    if snapshot is not None:
        context = snapshot["context"]
        result["identity"] = {
            key: context[key]
            for key in ("character", "realm", "playerFaction", "clientBuild", "interface", "nativeRegion")
        }
        result["ahOpen"] = snapshot["ahOpen"]
        result["zone"] = context["zone"]
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _probe_frame(_args: argparse.Namespace) -> int:
    """0: a matching automation frame decoded; 1: verified no frame; 3: could not verify."""

    config = load_config()
    _window, snapshot = _frame(config)
    return PROBE_FRAME if snapshot is not None else PROBE_NO_FRAME


def _presence_ok(args: argparse.Namespace) -> int:
    if args.for_input and maintenance_blocker() is not None:
        return 1
    return presence_ok()


def _play(args: argparse.Namespace) -> int:
    evidence = battle_net.click_play(args.window)
    print(f"clicked Battle.net Play; evidence {evidence}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crowley-desktop", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("observe", help="print the allowlisted current game observation").set_defaults(
        func=_observe
    )
    presence = commands.add_parser(
        "presence-ok", help="check physical activity, and optionally active automation state"
    )
    presence.add_argument(
        "--for-input",
        action="store_true",
        help="also fail while local automation is running or has an unresolved effect",
    )
    presence.set_defaults(func=_presence_ok)
    commands.add_parser(
        "probe-frame", help="decode the configured automation canvas once; exit 0, 1, or 3"
    ).set_defaults(func=_probe_frame)
    play = commands.add_parser("play", help="click Battle.net's Play button like a human")
    play.add_argument("--window", type=int, help="Battle.net window id; found when omitted")
    play.set_defaults(func=_play)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        AutomationConfigError,
        battle_net.BattleNetPlayError,
        CaptureError,
        DesktopObserveError,
        ExecutorError,
        InputToolError,
        ObserveError,
        OSError,
        PointerExecutionError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        if args.command == "probe-frame":
            print(f"probe-frame error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return PROBE_ERROR
        print(f"{args.command}: {type(exc).__name__}: {exc}".splitlines()[0], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
