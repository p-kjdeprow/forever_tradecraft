"""Bounded JSON interface for local automation contracts and operation state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .backend import DesktopBackend
from .config import AutomationConfigError, load_config
from .desktop_input import GuiLease, gui_lock_path
from .ledger import Ledger, LedgerError
from .model import MAX_BATCH_BYTES, Batch, BatchError
from .runner import AdmissionDenied, BatchAuthorization, SessionArm
from .runtime import default_ledger_path, dry_run_batch, run_batch
from .targets import load_profiles

LIVE_QUALIFICATION = "unverified"


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": message[:240]}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)


def _read_batch(path: Path) -> Batch:
    with path.open("rb") as stream:
        source = stream.read(MAX_BATCH_BYTES + 1)
    return Batch.from_json(source)


def _backend_context():
    config = load_config()
    targets, routes = load_profiles(config)
    lease = GuiLease(gui_lock_path())
    backend = DesktopBackend(config, targets, routes, lease)
    return config, lease, backend, routes


@contextmanager
def _leased_backend() -> Iterator[tuple[Any, GuiLease, DesktopBackend, dict[str, Any]]]:
    config, lease, backend, routes = _backend_context()
    try:
        with lease.hold():
            yield config, lease, backend, routes
    finally:
        backend.close()


def _arm(args: argparse.Namespace, batch: Batch, config: Any) -> tuple[BatchAuthorization, SessionArm]:
    now = int(time.time())
    expiry = args.arm_expires
    if type(expiry) is not int or not now < expiry <= now + 120:
        raise AdmissionDenied("--arm-expires must be a Unix time within the next 120 seconds")
    return (
        BatchAuthorization(batch.batch_id, batch.house, batch.character, min(batch.expires_at, expiry)),
        SessionArm(args.arm, batch.house, batch.character, expiry),
    )


def _parser() -> argparse.ArgumentParser:
    parser = _JsonParser(prog="crowley-automation")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_JsonParser)
    inspect = commands.add_parser("inspect", help="validate and print a canonical batch")
    inspect.add_argument("batch", type=Path)
    prepare = commands.add_parser("prepare", help="durably record a validated batch without execution")
    prepare.add_argument("batch", type=Path)
    prepare.add_argument("--ledger", type=Path, default=default_ledger_path())
    status = commands.add_parser("status", help="inspect durable local operation state")
    status.add_argument("batch_id", nargs="?")
    status.add_argument("--ledger", type=Path, default=default_ledger_path())
    observe = commands.add_parser("observe", help="read one fresh native observation")
    observe.add_argument("--ledger", type=Path, default=default_ledger_path())
    for name in ("inventory", "owned", "mail"):
        page = commands.add_parser(name, help=f"read one native {name} inspection page")
        page.add_argument("--page", type=int, default=0)
        page.add_argument("--ledger", type=Path, default=default_ledger_path())
    for name in ("dry-run", "run"):
        operation = commands.add_parser(
            name, help="perform native preflight" if name == "dry-run" else "run one confirmed batch"
        )
        operation.add_argument("batch", type=Path)
        operation.add_argument("--confirm", required=True)
        operation.add_argument("--arm", required=True, help="exact native boot ID from observe")
        operation.add_argument(
            "--arm-expires", type=int, required=True, help="short Unix time expiry, at most 120 seconds"
        )
        operation.add_argument("--ledger", type=Path, default=default_ledger_path())
    stop = commands.add_parser("stop", help="set or explicitly clear the durable input stop")
    stop.add_argument(
        "--clear", action="store_true", help="clear the stop only when no uncertain effect exists"
    )
    stop.add_argument("--ledger", type=Path, default=default_ledger_path())
    reconcile = commands.add_parser(
        "reconcile", help="resolve an uncertain action from a fresh causal native receipt"
    )
    reconcile.add_argument("batch_id")
    reconcile.add_argument("--ordinal", type=int, required=True)
    reconcile.add_argument("--step", type=int, required=True)
    reconcile.add_argument("--ledger", type=Path, default=default_ledger_path())
    return parser


def _observe(args: argparse.Namespace) -> dict[str, object]:
    with _leased_backend() as (_config, _lease, backend, _routes):
        value = backend.observe_native()
    context = value["context"]
    inspection = value["inspection"]
    result: dict[str, object] = {
        "schema": "crowley.automation-observation.v1",
        "liveQualification": LIVE_QUALIFICATION,
        "boot": value["boot"],
        "sequence": value["sequence"],
        "identity": {
            key: context[key]
            for key in ("character", "realm", "playerFaction", "clientBuild", "interface", "nativeRegion")
        },
        "zone": context["zone"],
        "ahOpen": value["ahOpen"],
        "mailOpen": value["mailOpen"],
        "goldCopper": value["goldCopper"],
        "ownership": value["ownership"],
        "motion": value["motion"],
        "operation": value["operation"],
        "blockingUI": value["blockingUI"],
        "chatActive": value["chatEdit"]["active"],
        "inventoryPage": inspection if inspection["kind"] == "inventory" else None,
    }
    return result


def _inspect_page(kind: str, page: int) -> dict[str, object]:
    with _leased_backend() as (_config, _lease, backend, _routes):
        inspection = backend.inspect(kind, page)
    return {
        "schema": "crowley.automation-inspection.v1",
        "liveQualification": LIVE_QUALIFICATION,
        **inspection,
    }


def _reconcile(args: argparse.Namespace) -> dict[str, object]:
    ledger = Ledger(args.ledger)
    status = ledger.status(args.batch_id)
    if type(args.ordinal) is not int or type(args.step) is not int or args.ordinal < 0 or args.step < 0:
        raise LedgerError("ordinal and step must be non-negative integers")
    actions = {row["ordinal"]: row["action"] for row in status["actions"]}
    action = actions.get(args.ordinal)
    if action is None:
        raise LedgerError("batch has no requested action ordinal")
    action_data = cast(dict[str, object], action)
    with _leased_backend() as (_config, _lease, backend, routes):
        value = backend.observe_native()
    native_receipt = value["operation"]["receipt"]
    if action_data["kind"] == "collect_mail":
        limit = cast(int, action_data["messageLimit"])
    elif action_data["kind"] == "move_route":
        route_id = cast(str, action_data["routeId"])
        if route_id not in routes:
            raise LedgerError("uncertain route has no matching measured profile")
        limit = len(routes[route_id].waypoints)
    else:
        limit = 1
    ledger.reconcile_receipt(
        args.batch_id,
        args.ordinal,
        args.step,
        native_receipt,
        boot_id=value["boot"],
        step_limit=limit,
    )
    return {"batch_id": args.batch_id, "reconciled": True, "state": ledger.status(args.batch_id)["state"]}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            batch = _read_batch(args.batch)
            result: object = {
                "batch_id": batch.batch_id,
                "canonical": batch.object(),
                "canonical_json": batch.canonical,
                "action_count": len(batch.actions),
                "runtimeImplemented": True,
                "liveQualification": LIVE_QUALIFICATION,
            }
        elif args.command == "prepare":
            batch = _read_batch(args.batch)
            ledger = Ledger(args.ledger)
            ledger.recover_if_idle()
            state = ledger.prepare(batch)
            result = {
                "batch_id": batch.batch_id,
                "state": state["state"],
                "action_count": len(batch.actions),
                "runtimeImplemented": True,
                "liveQualification": LIVE_QUALIFICATION,
            }
        elif args.command == "status":
            ledger = Ledger(args.ledger)
            ledger.recover_if_idle()
            result = ledger.status(args.batch_id) if args.batch_id else ledger.all_status()
        elif args.command == "observe":
            result = _observe(args)
        elif args.command in {"inventory", "owned", "mail"}:
            result = _inspect_page(args.command, args.page)
        elif args.command in {"dry-run", "run"}:
            batch = _read_batch(args.batch)
            config = load_config()
            authorization, arm = _arm(args, batch, config)
            if args.command == "dry-run":
                result = dry_run_batch(
                    batch, authorization, arm, args.confirm, ledger_path=args.ledger, config=config
                )
                result["liveQualification"] = LIVE_QUALIFICATION
            else:
                result = run_batch(
                    batch, authorization, arm, args.confirm, ledger_path=args.ledger, config=config
                )
                result["liveQualification"] = LIVE_QUALIFICATION
        elif args.command == "stop":
            ledger = Ledger(args.ledger)
            if args.clear:
                ledger.clear_stop()
            else:
                ledger.stop()
            result = {"stopRequested": ledger.stop_requested()}
        else:
            result = _reconcile(args)
        output = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if len(output.encode("utf-8")) > 256_000:
            raise ValueError("JSON response exceeds the 256 KiB output bound")
        print(output)
        return 0
    except (
        BatchError,
        OSError,
        RuntimeError,
        KeyError,
        ValueError,
        sqlite3.Error,
        AutomationConfigError,
    ) as exc:
        print(json.dumps({"error": str(exc).splitlines()[0][:240]}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
