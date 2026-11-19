"""Command-line entrypoint for Crowley operational tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from crowley_market import backup

from crowley_host.ops import doctor, health, install


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crowley operational tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "health"):
        sub.add_parser(name)

    sub.add_parser("snapshot")

    drill = sub.add_parser("drill")
    drill.add_argument("snapshot", type=Path)

    install_parser = sub.add_parser("install", help="install units and the packaged addon")
    install_parser.add_argument("--game-root", type=Path)
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--force", action="store_true")
    install_parser.add_argument("--dev-addon-from", type=Path)
    sub.add_parser("doctor", help="check the installed runtime")
    sub.add_parser("compose-file", help="print the packaged Docker Compose file")
    image = sub.add_parser("image", help="build the Crowley server image")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_build = image_commands.add_parser("build", help="build from a wheel or source checkout")
    image_input = image_build.add_mutually_exclusive_group(required=True)
    image_input.add_argument("--source", type=Path)
    image_input.add_argument("--wheel", type=Path, help="crowley-market wheel")
    image_build.add_argument("--common-wheel", type=Path, help="matching crowley-common wheel")
    watch = sub.add_parser("watch", help="wait locally for consecutive accepted full scans; no game input")
    watch.add_argument("--after-sweep", type=int, required=True, help="accepted sweep ID before this work")
    watch.add_argument("--count", type=int, default=2)
    watch.add_argument("--timeout", type=float, default=2400, help="maximum wait in seconds")
    watch.add_argument("--poll", type=float, default=20, help="local polling interval in seconds")
    watch.add_argument("--items", help="comma-separated numeric item IDs; default: all canonical core items")
    watch.add_argument("--output", type=Path, help="private JSON evidence path")
    usage = sub.add_parser("usage", help="summarize local Codex usage without model calls")
    usage.add_argument("--hours", type=float, default=24)
    scope = usage.add_mutually_exclusive_group()
    scope.add_argument("--project", type=Path, default=Path.cwd())
    scope.add_argument("--all-projects", action="store_true")
    usage.add_argument("--sessions", type=Path, default=Path.home() / ".codex" / "sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "usage":
        from crowley_host.ops.usage import usage

        try:
            result = usage(
                args.sessions, hours=args.hours, project=None if args.all_projects else args.project.resolve()
            )
        except (OSError, ValueError) as exc:
            print(f"usage: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "watch":
        from crowley_common.game_target import canonical_plan

        from crowley_host.ops.watch import watch

        try:
            items = (
                tuple(sorted({int(v) for v in args.items.split(",")}))
                if args.items
                else tuple(cast(dict[str, Any], canonical_plan()["pools"])["core"])
            )
            result = watch(
                after=args.after_sweep,
                count=args.count,
                timeout=args.timeout,
                poll=args.poll,
                items=items,
                output=args.output,
                emit=lambda event: print(json.dumps(event, sort_keys=True), flush=True),
            )
        except KeyboardInterrupt:
            return 130
        except (health.OperationsError, OSError, ValueError) as exc:
            print(f"watch: {exc}", file=sys.stderr)
            return 2
        return result["exit_code"]
    if args.command == "install":
        try:
            result = install.install(
                install.InstallRequest(
                    game_root=args.game_root,
                    dev_addon_from=args.dev_addon_from,
                    dry_run=args.dry_run,
                    force=args.force,
                )
            )
        except install.InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(install.diff_text(result))
        return 0
    if args.command == "doctor":
        report = doctor.doctor()
        if report.ok:
            print("doctor: OK")
            return 0
        for problem in report.problems:
            print(f"doctor: {problem}", file=sys.stderr)
        return 1
    if args.command == "compose-file":
        try:
            print(install.compose_file_path())
        except install.InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if args.command == "image":
        try:
            tag = install.build_image(wheel=args.wheel, common_wheel=args.common_wheel, source=args.source)
        except install.InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"image built: {tag}")
        return 0

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
                    state_root=paths.state_root,
                    backup_root=paths.backup_root,
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
