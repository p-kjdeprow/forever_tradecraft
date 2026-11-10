"""`crowley-scan`: run the loop, show its status, gate the launcher on presence."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from crowley.gui.input_tool import PinnedInputTool
from crowley.platform.paths import XdgDirs

from .act import Actor, GuiLease, LauncherRun, XtestKeys, activate_window, gui_lock_path
from .config import ScanConfig, load_scan_config
from .loop import ScanLoop, load_state, state_path, status_path
from .observe import (
    FileDiskSource,
    Observer,
    ProcProcessSource,
    ScreenCanvas,
    X11Window,
    x11_environment,
    x11_runner,
)
from .presence import Presence, presence_ok


def build_observer(config: ScanConfig, presence: Presence) -> Observer:
    env = x11_environment(config.display)
    scratch = XdgDirs.from_environ(os.environ).crowley_runtime / "scan-capture"
    return Observer(
        process=ProcProcessSource(config.matcher),
        window=X11Window(config.window_class, config.window_title, x11_runner(env)),
        canvas=ScreenCanvas(config.canvas_crop, scratch),
        disk=FileDiskSource(config.saved_variables, config.companion_state, config.data_lua),
        presence=presence,
        target=config.target,
        contract=config.contract,
        canvas_crop=config.canvas_crop,
    )


def _run(_args: argparse.Namespace) -> int:
    config = load_scan_config()
    presence = Presence()
    observer = build_observer(config, presence)
    env = x11_environment(config.display)
    tool = PinnedInputTool.load()
    try:
        actor = Actor(
            keys=XtestKeys(tool, env),
            activate=lambda xid: activate_window(env, xid),
            lease=GuiLease(gui_lock_path()),
            presence=presence,
            target=config.target,
            matcher=config.matcher,
            launcher=LauncherRun(),
        )
        loop = ScanLoop(
            observer=observer,
            actor=actor,
            state=load_state(state_path()),
            state_file=state_path(),
            status_file=status_path(),
        )
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        loop.run(stop=lambda: stopping)
    finally:
        tool.close()
    return 0


def _status(_args: argparse.Namespace) -> int:
    path: Path = status_path()
    try:
        sys.stdout.write(path.read_text(encoding="utf-8") + "\n")
    except FileNotFoundError:
        print(f"no status at {path}: crowley-scan has not run", file=sys.stderr)
        return 1
    return 0


def _presence_ok(_args: argparse.Namespace) -> int:
    return presence_ok()


PROBE_FRAME = 0
PROBE_NO_FRAME = 1
PROBE_ERROR = 3  # argparse uses 2


def _probe_frame(_args: argparse.Namespace) -> int:
    """0: a frame decoded.  1: verified no frame.  3: could not verify (the reason is on stderr)."""

    try:
        config = load_scan_config()
        decoded = build_observer(config, Presence()).probe_frame()
    except Exception as exc:  # every failure is mapped explicitly to the error code
        print(f"probe-frame error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return PROBE_ERROR
    return PROBE_FRAME if decoded else PROBE_NO_FRAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crowley-scan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the scan loop (tick every 2 s)").set_defaults(func=_run)
    sub.add_parser("status", help="print scan-status.json").set_defaults(func=_status)
    sub.add_parser(
        "presence-ok", help="exit 0 when no human input in the last 5 minutes, else 1"
    ).set_defaults(func=_presence_ok)
    sub.add_parser(
        "probe-frame", help="capture and decode once; exit 0 frame decoded, 1 verified no frame, 3 error"
    ).set_defaults(func=_probe_frame)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
