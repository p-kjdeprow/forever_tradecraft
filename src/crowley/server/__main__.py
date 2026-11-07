"""crowley-server: run the API (default) or manage migrations."""

from __future__ import annotations

import argparse
import logging

import psycopg
import uvicorn

from .config import get_settings
from .db import migrate, migration_status


def main() -> None:
    parser = argparse.ArgumentParser(prog="crowley-server")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "migrate"])
    parser.add_argument(
        "--status", action="store_true", help="show migration status without changing the database"
    )
    args = parser.parse_args()
    if args.status and args.command != "migrate":
        parser.error("--status is only valid with the migrate command")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.command == "migrate":
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            if args.status:
                status = migration_status(conn)
                print("applied:", ", ".join(status.applied) if status.applied else "none")
                print("pending:", ", ".join(status.pending) if status.pending else "none")
                print(
                    "unknown-applied:",
                    ", ".join(status.unknown_applied) if status.unknown_applied else "none",
                )
                print(
                    "checksum-mismatch:",
                    ", ".join(status.checksum_mismatch) if status.checksum_mismatch else "none",
                )
                print(
                    "checksum-missing:",
                    ", ".join(status.checksum_missing) if status.checksum_missing else "none",
                )
                return
            applied = migrate(conn)
        print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
        return

    uvicorn.run("crowley.server.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
