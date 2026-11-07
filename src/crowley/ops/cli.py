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
        sub.add_parser(name)

    sub.add_parser("snapshot")

    drill = sub.add_parser("drill")
    drill.add_argument("snapshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        paths = health.operation_paths()
    except health.OperationsError as exc:
        if args.command in {"backup", "health", "snapshot", "drill"}:
            if args.command in {"backup", "health"}:
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
            else:
                print(str(exc), file=sys.stderr)
            return 2
        raise
    if args.command in {"backup", "health"}:
        try:
            if args.command == "backup":
                result = health.backup_operation(
                    state_root=paths.state_root,
                    backup_root=paths.backup_root,
                )
            else:
                result = health.health_operation(
                    config_path=paths.config_path,
                    state_root=paths.state_root,
                    backup_root=paths.backup_root,
                    companion_state_path=paths.companion_state,
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
            result = backup.snapshot(paths.backup_root)
            print(f"snapshot complete: {result.name}")
            return 0

        result = backup.drill(args.snapshot, paths.backup_root)
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
