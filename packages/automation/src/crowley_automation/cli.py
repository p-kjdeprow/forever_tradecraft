"""Honest prepare, inspect, and status interface; live execution is not integrated."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .ledger import Ledger
from .model import MAX_BATCH_BYTES, Batch, BatchError


def _read_batch(path: Path) -> Batch:
    with path.open("rb") as stream:
        source = stream.read(MAX_BATCH_BYTES + 1)
    return Batch.from_json(source)


def _default_ledger() -> Path:
    return Path.home() / ".local" / "state" / "crowley" / "automation" / "ledger.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crowley-automation")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="validate and print a canonical batch")
    inspect.add_argument("batch", type=Path)
    prepare = commands.add_parser("prepare", help="durably record a validated batch without execution")
    prepare.add_argument("batch", type=Path)
    prepare.add_argument("--ledger", type=Path, default=_default_ledger())
    status = commands.add_parser("status", help="inspect durable local operation state")
    status.add_argument("batch_id", nargs="?")
    status.add_argument("--ledger", type=Path, default=_default_ledger())
    return parser


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
                "execution_available": False,
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
                "execution_available": False,
            }
        else:
            ledger = Ledger(args.ledger)
            ledger.recover_if_idle()
            result = ledger.status(args.batch_id) if args.batch_id else ledger.all_status()
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (BatchError, OSError, RuntimeError, KeyError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
