"""Standalone desktop observation, presence, probe, and Battle.net commands."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any, cast

from . import battle_net
from .config import AutomationConfig, AutomationConfigError, load_config
from .desktop_observe import NativeWorldObservation, ObserveError, observe_native_world
from .desktop_session import DesktopSessionProvider, DesktopSessionUnavailable
from .gui.capture import CaptureError
from .gui.input_tool import InputToolError
from .gui.pointer import ExecutorError, PointerExecutionError
from .presence import presence_ok
from .runtime import maintenance_blocker

PROBE_FRAME = 0
PROBE_NO_FRAME = 1
PROBE_ERROR = 3
MAX_FRAME_AGE_SECONDS = 3
FUTURE_FRAME_TOLERANCE_SECONDS = 1


class DesktopObserveError(RuntimeError):
    """The desktop state could not be safely observed."""


def observe_native_frame(
    config: AutomationConfig, *, session_provider: DesktopSessionProvider | None = None
) -> NativeWorldObservation:
    """Reusable fresh native-world observation shared with startup recovery."""

    try:
        return observe_native_world(config, session_provider=session_provider)
    except (ObserveError, DesktopSessionUnavailable) as error:
        raise DesktopObserveError(str(error)) from error


def _frame(config: AutomationConfig):
    observation = observe_native_frame(config)
    return observation.window, observation.snapshot


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


def _startup(_args: argparse.Namespace) -> int:
    """Run one bounded startup episode until ready, stop, or a typed refusal."""

    from .startup import StartupRecovery

    config = load_config()
    stopping = False
    received_signal = 0
    previous_handlers: dict[int, object] = {}

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stopping, received_signal
        stopping = True
        received_signal = signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    recovery = StartupRecovery(config, stopping=lambda: stopping)
    previous_status: tuple[object, ...] | None = None
    exit_code = 1
    try:
        while True:
            status = recovery.step()
            signature = (
                status.outcome,
                status.phase,
                status.reason,
                status.action,
                status.deadline_at,
                status.retry_at,
                status.native_ready,
                status.process_identity,
                status.window_xid,
            )
            if signature != previous_status:
                identity = status.process_identity
                payload: dict[str, object] = {
                    "outcome": status.outcome,
                    "phase": status.phase,
                    "reason": status.reason,
                    "action": status.action,
                    "startedAt": status.started_at,
                    "deadlineAt": status.deadline_at,
                    "retryAt": status.retry_at,
                    "nativeReady": status.native_ready,
                    "processIdentity": None
                    if identity is None
                    else {
                        "pid": identity.pid,
                        "startTicks": identity.start_ticks,
                        "uid": identity.uid,
                        "argv0": identity.argv0,
                        "winePrefix": identity.wine_prefix,
                    },
                    "window": status.window_xid,
                }
                print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)
                previous_status = signature
            if status.outcome == "ready":
                exit_code = 0
                break
            if status.outcome == "stopped":
                exit_code = 128 + received_signal if received_signal else 1
                break
            if status.outcome in {"blocked", "failed"}:
                exit_code = 1
                break
            delay = 0.25
            if status.retry_at is not None:
                delay = max(0.05, min(1.0, status.retry_at - time.time()))
            time.sleep(delay)
    finally:
        recovery.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, cast(Any, handler))
    if recovery.active:
        return 1
    return exit_code


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
    commands.add_parser("startup", help="recover the desktop until native world readiness").set_defaults(
        func=_startup
    )
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
        DesktopSessionUnavailable,
        ValueError,
    ) as exc:
        if args.command == "probe-frame":
            print(f"probe-frame error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return PROBE_ERROR
        print(f"{args.command}: {type(exc).__name__}: {exc}".splitlines()[0], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
