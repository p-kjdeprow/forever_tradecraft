"""crowley-companion: sync the game folder with the Crowley server."""

from __future__ import annotations

import argparse
import logging

import httpx

from .config import CompanionSettings, config_path
from .sync import State, state_file_lock


def cmd_doctor(_: argparse.Namespace) -> None:
    config_file = config_path()
    print(f"config file: {config_file} ({'exists' if config_file.exists() else 'missing'})")
    settings = CompanionSettings()
    if settings.state_file is None:
        raise ValueError("resolved companion state_file is required")
    print(f"state file:  {settings.state_file}")
    with state_file_lock(settings.state_file):
        state = State.load(settings.state_file)
    print(f"wow_dir: {settings.wow_path} ({'ok' if settings.wow_path.is_dir() else 'MISSING'})")
    print(f"wtf_account: {settings.wtf_account}")
    for name in ("Crowley", "Crowley_Data"):
        target = settings.addons_dir / name
        print(f"addon {name}: {'installed' if target.exists() else 'not installed'}")
    files = [settings.saved_variables]
    print(f"SavedVariables files: {len(files) or 'none yet (log in and /reload once)'}")
    for path in files:
        print(f"  {path}")
    print(f"rejections: {len(state.rejections)}")
    for rejection in sorted(state.rejections.values(), key=lambda item: (item.last_seen, item.key.record_id)):
        print(
            f"  {rejection.key.record_kind}/{rejection.key.record_id}: "
            f"HTTP {rejection.status} {rejection.error_code}: {rejection.error_message}"
        )

    try:
        response = httpx.get(f"{settings.server_url.rstrip('/')}/healthz", timeout=5)
        print(f"server {settings.server_url}: {response.status_code} {response.text}")
    except httpx.HTTPError as exc:
        print(f"server {settings.server_url}: unreachable ({exc})")


def cmd_run(_: argparse.Namespace) -> None:
    from .sync import Companion

    Companion(CompanionSettings()).run()


def cmd_once(_: argparse.Namespace) -> None:
    from .sync import Companion

    Companion(CompanionSettings()).once()


def cmd_retry_rejected(args: argparse.Namespace) -> None:
    from .sync import Companion

    count = Companion(CompanionSettings()).retry_rejected(args.id)
    print(f"retrying {count} rejected record(s)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="crowley-companion")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="watch SavedVariables and sync continuously").set_defaults(fn=cmd_run)
    sub.add_parser("once", help="upload pending scans and write Data.lua once").set_defaults(fn=cmd_once)
    sub.add_parser("doctor", help="check paths, config and server").set_defaults(fn=cmd_doctor)
    retry = sub.add_parser("retry-rejected", help="retry permanently rejected records")
    retry.add_argument("--id", help="retry only the matching record id")
    retry.set_defaults(fn=cmd_retry_rejected)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args.fn(args)


if __name__ == "__main__":
    main()
