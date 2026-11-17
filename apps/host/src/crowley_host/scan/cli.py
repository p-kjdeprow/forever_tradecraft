"""Run the scanner-aware host loop and inspect its private status file."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from crowley_automation import desktop_input, desktop_observe
from crowley_automation.desktop_session import (
    DesktopSession,
    DesktopSessionProvider,
    DesktopSessionUnavailable,
)
from crowley_automation.gui.control_child import ControlChildRunner
from crowley_automation.gui.input_tool import PinnedInputTool
from crowley_automation.presence import Presence
from crowley_automation.runtime import maintenance_blocker
from crowley_common.platform.paths import XdgDirs

from .act import ActionNotAdmitted, Actor
from .config import ScanConfig, load_scan_config
from .loop import ScanLoop, load_state, state_path, status_path
from .observe import FileDiskSource, Observer


class _SessionEnvironment:
    """Small call-time environment view refreshed at every session boundary."""

    def __init__(self) -> None:
        self._values: dict[str, str] | None = None

    def refresh(self, session: DesktopSession) -> None:
        self._values = dict(session.environment)

    def __call__(self) -> Mapping[str, str]:
        if self._values is None:
            raise DesktopSessionUnavailable("desktop_session_unavailable")
        return dict(self._values)


def build_observer(
    config: ScanConfig,
    presence: Presence,
    session_provider: DesktopSessionProvider,
    environment: _SessionEnvironment,
) -> Observer:
    scratch = XdgDirs.from_environ(os.environ).crowley_runtime / "scan-capture"
    return Observer(
        process=desktop_observe.ProcProcessSource(config.matcher),
        window=desktop_observe.X11Window(
            config.window_class, config.window_title, desktop_observe.x11_runner(environment)
        ),
        canvas=desktop_observe.ScreenCanvas(scratch, environment),
        disk=FileDiskSource(config.saved_variables, config.companion_state, config.data_lua),
        presence=presence,
        target=config.target,
        contract=config.contract,
        canvas_crop=config.canvas_crop,
        automation_canvas_crop=config.automation_canvas_crop,
        session_provider=session_provider,
        session_environment=environment,
    )


def _run(_args: argparse.Namespace) -> int:
    config = load_scan_config()
    session_provider = DesktopSessionProvider(config.display)
    environment = _SessionEnvironment()

    def run_in_session(argv: list[str], timeout: float) -> str:
        try:
            current = dict(environment())
        except DesktopSessionUnavailable as exc:
            raise OSError(exc.reason) from None
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=current,
        )
        if done.returncode != 0:
            raise subprocess.CalledProcessError(done.returncode, argv, output=done.stdout, stderr=done.stderr)
        return done.stdout

    presence = Presence(runner=run_in_session)
    observer = build_observer(config, presence, session_provider, environment)
    tool = PinnedInputTool.load()
    stopping = False

    def is_stopping() -> bool:
        return stopping

    recovery = None
    actor = None
    try:
        from crowley_automation.startup import StartupRecovery

        recovery = StartupRecovery(
            config.automation,
            stopping=is_stopping,
            session_provider=session_provider,
        )
        lease = desktop_input.GuiLease(desktop_input.gui_lock_path())
        actor_ref: list[Actor] = []

        def guard() -> None:
            if not actor_ref:
                raise desktop_input.ActError("action guard is not initialized")
            actor_ref[0].before_emit()

        def during() -> None:
            if not actor_ref:
                raise desktop_input.ActError("action guard is not initialized")
            actor_ref[0].during_emit()

        control = ControlChildRunner(
            lease,
            tool,
            environment=environment,
            stopping=is_stopping,
            guard=guard,
            during=during,
        )
        keys = desktop_input.XtestKeys(
            tool,
            environment,
            run=control.run,
            query_run=subprocess.run,
        )

        def activate(xid: int) -> None:
            try:
                current = dict(environment())
                result = control.run(
                    [tool.proc_path, "windowactivate", "--sync", str(xid)],
                    env=current,
                    pass_fds=(tool.image.fd,),
                )
            except DesktopSessionUnavailable as exc:
                raise ActionNotAdmitted(exc.reason) from None
            if result.returncode != 0:
                raise desktop_input.ActError(f"window activation exited {result.returncode}")

        actor = Actor(
            keys=keys,
            activate=activate,
            lease=lease,
            presence=presence,
            target=config.target,
            matcher=config.matcher,
            window=observer.window_source,
            session_provider=session_provider,
            session_environment=environment,
            scan_observer=observer,
            native_observe=lambda: desktop_observe.observe_native_world(
                config.automation,
                session_provider=session_provider,
            ),
            recovery=recovery,
            stopping=is_stopping,
            control_runner=control,
            maintenance_blocker=maintenance_blocker,
        )
        actor_ref.append(actor)
        loop = ScanLoop(
            observer=observer,
            actor=actor,
            state=load_state(state_path()),
            state_file=state_path(),
            status_file=status_path(),
            recovery=recovery,
            maintenance_blocker=maintenance_blocker,
        )

        def on_signal(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        loop.run(stop=is_stopping)
    finally:
        if actor is not None:
            actor.close()
        if recovery is not None:
            recovery.close()
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
