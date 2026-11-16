"""Run the scanner-aware host loop and inspect its private status file."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from crowley_automation import desktop_input, desktop_observe
from crowley_automation.gui.input_tool import PinnedInputTool
from crowley_automation.presence import Presence
from crowley_automation.runtime import maintenance_blocker
from crowley_common.platform.paths import XdgDirs

from .act import Actor
from .config import ScanConfig, load_scan_config
from .loop import ScanLoop, load_state, state_path, status_path
from .observe import FileDiskSource, Observer


def build_observer(config: ScanConfig, presence: Presence) -> Observer:
    env = desktop_observe.x11_environment(config.display)
    scratch = XdgDirs.from_environ(os.environ).crowley_runtime / "scan-capture"
    return Observer(
        process=desktop_observe.ProcProcessSource(config.matcher),
        window=desktop_observe.X11Window(
            config.window_class, config.window_title, desktop_observe.x11_runner(env)
        ),
        canvas=desktop_observe.ScreenCanvas(scratch),
        disk=FileDiskSource(config.saved_variables, config.companion_state, config.data_lua),
        presence=presence,
        target=config.target,
        contract=config.contract,
        canvas_crop=config.canvas_crop,
        automation_canvas_crop=config.automation_canvas_crop,
    )


def _run(_args: argparse.Namespace) -> int:
    config = load_scan_config()
    presence = Presence()
    observer = build_observer(config, presence)
    env = desktop_observe.x11_environment(config.display)
    tool = PinnedInputTool.load()
    try:
        actor = Actor(
            keys=desktop_input.XtestKeys(tool, env),
            activate=lambda xid: desktop_input.activate_window(env, xid),
            lease=desktop_input.GuiLease(desktop_input.gui_lock_path()),
            presence=presence,
            target=config.target,
            matcher=config.matcher,
            launcher=desktop_input.LauncherRun(),
            maintenance_blocker=maintenance_blocker,
        )
        loop = ScanLoop(
            observer=observer,
            actor=actor,
            state=load_state(state_path()),
            state_file=state_path(),
            status_file=status_path(),
            maintenance_blocker=maintenance_blocker,
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
        print(f"no status at {path}: crowley-host has not run", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crowley-host", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the scan-aware host loop (tick every two seconds)").set_defaults(
        func=_run
    )
    commands.add_parser("status", help="print scan-status.json").set_defaults(func=_status)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
