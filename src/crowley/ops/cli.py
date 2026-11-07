"""Command-line entrypoint for Crowley operational tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from crowley.ops import backup, health


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crowley operational tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "health"):
        item = sub.add_parser(name)
        item.add_argument("--repo", type=Path, required=True)
        item.add_argument("--config", type=Path, required=True)
        item.add_argument("--state-root", type=Path, required=True)
        item.add_argument("--backup-root", type=Path, required=True)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--repo", type=Path, required=True)
    snapshot.add_argument("--root", type=Path, default=backup.DEFAULT_ROOT)

    drill = sub.add_parser("drill")
    drill.add_argument("snapshot", type=Path)
    drill.add_argument("--repo", type=Path, required=True)
    drill.add_argument("--root", type=Path, default=backup.DEFAULT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"backup", "health"}:
        try:
            if args.command == "backup":
                result = health.backup_operation(
                    repo=args.repo,
                    config=args.config,
                    state_root=args.state_root,
                    backup_root=args.backup_root,
                )
            else:
                result = health.health_operation(
                    repo=args.repo,
                    config_path=args.config,
                    state_root=args.state_root,
                    backup_root=args.backup_root,
                )
        except (health.OperationsError, OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "schema": health.OPS_SCHEMA,
                        "operation": args.command,
                        "outcome": "failed",
                        "error_category": health.fixed_error(exc),
                        "exit_code": 2,
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "schema": health.OPS_SCHEMA,
                    "operation": args.command,
                    "outcome": result.get("outcome", result.get("classification")),
                    "exit_code": result["exit_code"],
                },
                sort_keys=True,
            )
        )
        return int(result["exit_code"])

    try:
        if args.command == "snapshot":
            result = backup.snapshot(args.repo, args.root)
            print(f"snapshot complete: {result.name}")
            return 0

        result = backup.drill(args.repo, args.snapshot, args.root)
        receipt = backup.strict_json(result.read_bytes())
        print(f"restore drill {receipt['result']}: {result.name}")
        return 0
    except backup.BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print(
            "bounded backup operation failed; inspect private receipt or incomplete snapshot",
            file=sys.stderr,
        )
        return 2
