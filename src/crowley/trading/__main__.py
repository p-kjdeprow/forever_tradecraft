"""Command line entry point for the trading backtester."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

import httpx
import psycopg

from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.core.queue import QueueFormatError

from .api import CraftingCapabilities
from .data import load_client_data, load_commodity_books
from .engine import run_backtest
from .numbers import as_fraction
from .paper import DEFAULT_STRATEGIES, PaperError, create_run, database_url, status_run, step_run
from .queue_cli import (
    QueueCLIError,
    from_paper,
    preview_spec,
    publish_spec,
    reconcile_from_database,
)
from .queue_cli import (
    status as queue_status,
)
from .report import text_summary, write_report
from .requirements import capabilities_from_mapping, capabilities_hash
from .statistics import precompute_statistics
from .steps import sweep_step_times
from .strategies import build


class _ReadOnlyLoaderConnection:
    """Let the analysis loader reuse the caller's already-open read-only block."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def transaction(self):
        return nullcontext(self._connection)


def _weights(value: str | None) -> list[Fraction] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("--weights must be a comma-separated list")
    return [as_fraction(part) for part in parts]


def _capability_ids(value: str | None, label: str) -> list[int] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{label} must be a comma-separated list of positive integer ids")
    result: list[int] = []
    for part in parts:
        if not part.isdigit() or int(part) <= 0:
            raise ValueError(f"{label} must contain positive integer ids")
        result.append(int(part))
    return result


def _capabilities(
    path: Path | None,
    tool_item_ids: str | None,
    tool_category_ids: str | None,
    spell_focus_ids: str | None,
) -> CraftingCapabilities:
    # Always start with the canonical empty object.  A file is validated as a
    # complete canonical object before explicit CLI fields override it.
    values: dict[str, object] = {
        "tool_item_ids": [],
        "tool_category_ids": [],
        "spell_focus_ids": [],
    }
    if path is not None:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("--capabilities-file must contain a JSON object")
        parsed = capabilities_from_mapping(loaded)
        values.clear()
        values.update(parsed.canonical)
    overrides = {
        "tool_item_ids": _capability_ids(tool_item_ids, "--tool-item-ids"),
        "tool_category_ids": _capability_ids(tool_category_ids, "--tool-category-ids"),
        "spell_focus_ids": _capability_ids(spell_focus_ids, "--spell-focus-ids"),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    return capabilities_from_mapping(values)


class _SweepRow(NamedTuple):
    id: int
    started_at: object
    client_version: str
    client_build: int | str


def _select_sweeps(
    conn,
    house: str,
    start: float | None,
    end: float | None,
    build: str | None,
    books=None,
):
    raw_rows = conn.execute(
        """
        SELECT ms.id, ms.started_at, ms.client_version, ms.client_build
        FROM market_sweeps AS ms
        JOIN houses AS h ON h.id = ms.house_id
        WHERE h.key = %s
        ORDER BY ms.started_at, ms.id
        """,
        (house,),
    ).fetchall()
    rows = tuple(_SweepRow(*row) for row in raw_rows)
    if not rows:
        raise RuntimeError(f"no market sweeps found for house {house!r}")
    step_times = sweep_step_times(books) if books is not None else {}
    represented_rows = [row for row in rows if books is None or row.id in step_times]
    excluded_without_books = len(rows) - len(represented_rows)
    window_rows = []
    for row in represented_rows:
        sweep_id = row.id
        if books is not None:
            timestamp = step_times[sweep_id]
        else:
            started_at = row.started_at
            if isinstance(started_at, datetime):
                timestamp = started_at.timestamp()
            elif isinstance(started_at, (int, float)):
                timestamp = float(started_at)
            else:
                raise RuntimeError(f"sweep {row.id} has an invalid start timestamp")
        if (start is None or timestamp >= start) and (end is None or timestamp <= end):
            window_rows.append(row)
    selected = []
    for row in window_rows:
        full_build = f"{row.client_version}.{row.client_build}"
        if build is None or full_build == build:
            selected.append(row)
    if not selected:
        qualifier = f" and build {build!r}" if build else ""
        raise RuntimeError(f"no market sweeps match house {house!r}{qualifier}")
    builds = {f"{row.client_version}.{row.client_build}" for row in selected}
    if len(builds) != 1:
        joined = ", ".join(sorted(builds))
        raise RuntimeError(
            f"selected sweeps contain multiple client builds ({joined}); "
            "restrict --start/--end or use --build"
        )
    return rows, window_rows, selected, next(iter(builds)), excluded_without_books


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest concurrent auction-house strategies")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("--database-url")
    backtest.add_argument("--house", required=True)
    backtest.add_argument("--capital-gold", type=Decimal, default=Decimal("20"))
    backtest.add_argument("--strategies", default="vendor_arb")
    backtest.add_argument("--weights")
    backtest.add_argument("--start", type=float)
    backtest.add_argument("--end", type=float)
    backtest.add_argument("--wind-down-hours", type=Fraction, default=Fraction(24))
    backtest.add_argument("--demand-share", type=Fraction, default=Fraction(1))
    backtest.add_argument("--build", help="full client build, for example 1.60.1.70450")
    backtest.add_argument("--out", required=True)
    backtest.add_argument("--capabilities-file", type=Path)
    backtest.add_argument("--tool-item-ids")
    backtest.add_argument("--tool-category-ids")
    backtest.add_argument("--spell-focus-ids")
    paper_create = subparsers.add_parser("paper-create")
    paper_create.add_argument("--database-url")
    paper_create.add_argument("--name", required=True)
    paper_create.add_argument("--house", required=True)
    paper_create.add_argument("--start", default="now")
    paper_create.add_argument("--capital-gold", type=Decimal, default=Decimal("20"))
    paper_create.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    paper_create.add_argument("--weights")
    paper_create.add_argument("--demand-share", type=Fraction, default=Fraction("0.5"))
    paper_create.add_argument("--build", dest="client_build")
    paper_create.add_argument("--capabilities-file", type=Path)
    paper_create.add_argument("--tool-item-ids")
    paper_create.add_argument("--tool-category-ids")
    paper_create.add_argument("--spell-focus-ids")
    paper_step = subparsers.add_parser("paper-step")
    paper_step.add_argument("--database-url")
    paper_step.add_argument("--name", required=True)
    paper_status = subparsers.add_parser("paper-status")
    paper_status.add_argument("--database-url")
    paper_status.add_argument("--name", required=True)
    paper_status.add_argument("--last-intents", type=int, default=20)
    queue = subparsers.add_parser("queue", help="publish and reconcile executor queues")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_publish = queue_sub.add_parser("publish")
    queue_publish.add_argument("--spec", type=Path, required=True)
    queue_publish.add_argument("--armed", action="store_true")
    queue_publish.add_argument("--confirm-real-trades")
    queue_publish.add_argument(
        "--server-url", default=os.environ.get("CROWLEY_SERVER_URL", "http://127.0.0.1:8080")
    )
    queue_publish.add_argument("--api-token", default=os.environ.get("CROWLEY_API_TOKEN"))
    queue_preview = queue_sub.add_parser("preview")
    queue_preview.add_argument("--spec", type=Path, required=True)
    queue_preview.add_argument("--armed", action="store_true")
    queue_preview.add_argument("--out", type=Path, required=True)
    queue_paper = queue_sub.add_parser("from-paper")
    queue_paper.add_argument("--run", required=True)
    queue_paper.add_argument("--character", required=True)
    queue_paper.add_argument("--expires-minutes", type=int, default=30)
    queue_paper.add_argument("--database-url", default=os.environ.get("CROWLEY_DATABASE_URL"))
    queue_paper.add_argument(
        "--server-url", default=os.environ.get("CROWLEY_SERVER_URL", "http://127.0.0.1:8080")
    )
    queue_paper.add_argument("--api-token", default=os.environ.get("CROWLEY_API_TOKEN"))
    queue_status_parser = queue_sub.add_parser("status")
    queue_status_parser.add_argument("--house", required=True)
    queue_status_parser.add_argument("--character", required=True)
    queue_status_parser.add_argument(
        "--server-url", default=os.environ.get("CROWLEY_SERVER_URL", "http://127.0.0.1:8080")
    )
    queue_status_parser.add_argument("--api-token", default=os.environ.get("CROWLEY_API_TOKEN"))
    queue_reconcile = queue_sub.add_parser("reconcile")
    queue_reconcile.add_argument("--queue", dest="queue_id", required=True)
    queue_reconcile.add_argument("--database-url", default=os.environ.get("CROWLEY_DATABASE_URL"))
    args = parser.parse_args(argv)
    if args.command == "queue":
        try:
            if args.queue_command == "publish":
                result = publish_spec(
                    json.loads(args.spec.read_text(encoding="utf-8")),
                    server_url=args.server_url,
                    api_token=args.api_token,
                    armed=args.armed,
                    confirm_real_trades=args.confirm_real_trades,
                )
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.queue_command == "preview":
                preview_spec(
                    json.loads(args.spec.read_text(encoding="utf-8")),
                    args.out,
                    armed=args.armed,
                )
            elif args.queue_command == "from-paper":
                if not args.database_url:
                    raise QueueCLIError("database URL is required (--database-url or CROWLEY_DATABASE_URL)")
                _, _, result = from_paper(
                    args.database_url,
                    args.run,
                    args.character,
                    expires_minutes=args.expires_minutes,
                    server_url=args.server_url,
                    api_token=args.api_token,
                )
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.queue_command == "status":
                queue_status(
                    args.house,
                    args.character,
                    server_url=args.server_url,
                    api_token=args.api_token,
                )
            else:
                if not args.database_url:
                    raise QueueCLIError("database URL is required (--database-url or CROWLEY_DATABASE_URL)")
                reconcile_from_database(args.database_url, args.queue_id)
        except (QueueCLIError, QueueFormatError, ValueError, OSError, httpx.HTTPError, psycopg.Error) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "paper-create":
        try:
            capabilities = _capabilities(
                args.capabilities_file,
                args.tool_item_ids,
                args.tool_category_ids,
                args.spell_focus_ids,
            )
            run_id = create_run(
                database_url(args.database_url),
                name=args.name,
                house=args.house,
                start=args.start,
                capital_gold=args.capital_gold,
                strategies=args.strategies,
                weights=args.weights,
                demand_share=args.demand_share,
                client_build=args.client_build,
                capabilities=capabilities,
            )
        except (PaperError, RuntimeError, ValueError, OSError, psycopg.Error) as exc:
            parser.error(str(exc))
        print(f"paper_run_created={args.name} id={run_id}")
        return 0
    if args.command == "paper-step":
        try:
            output = step_run(database_url(args.database_url), args.name)
        except (PaperError, RuntimeError, ValueError, psycopg.Error) as exc:
            parser.error(str(exc))
        print(output)
        return 0
    if args.command == "paper-status":
        try:
            output = status_run(
                database_url(args.database_url),
                args.name,
                args.last_intents,
            )
        except (PaperError, RuntimeError, ValueError, psycopg.Error) as exc:
            parser.error(str(exc))
        print(output)
        return 0
    names = [name.strip() for name in args.strategies.split(",")]
    if not names or any(not name for name in names):
        parser.error("--strategies must be a comma-separated list")
    try:
        capabilities = _capabilities(
            args.capabilities_file,
            args.tool_item_ids,
            args.tool_category_ids,
            args.spell_focus_ids,
        )
        strategies = [build(name) for name in names]
        weights = _weights(args.weights)
        capital = int(args.capital_gold * 10_000)
        with psycopg.connect(database_url(args.database_url)) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                loader_connection = _ReadOnlyLoaderConnection(conn)
                all_books = load_commodity_books(loader_connection, args.house)
                (
                    all_sweeps,
                    window_sweeps,
                    selected_sweeps,
                    client_build,
                    excluded_without_books,
                ) = _select_sweeps(conn, args.house, args.start, args.end, args.build, all_books)
                selected_ids = {row.id for row in selected_sweeps}
                books = {}
                for item_id, item_books in all_books.items():
                    selected_books = []
                    for book in item_books:
                        if book.sweep_id is None:
                            raise RuntimeError(f"book for item {item_id} has no sweep id")
                        if int(book.sweep_id) in selected_ids:
                            selected_books.append(book)
                    books[item_id] = selected_books
                books = {item_id: item_books for item_id, item_books in books.items() if item_books}
                facts, recipes = load_client_data(loader_connection, args.house, client_build)
                represented_sweeps = len(all_sweeps) - excluded_without_books
                excluded_by_window = represented_sweeps - len(window_sweeps)
                excluded_by_build = len(window_sweeps) - len(selected_sweeps)
        statistics = precompute_statistics(books)
        result = run_backtest(
            books,
            statistics,
            facts,
            recipes,
            strategies,
            weights,
            capital,
            None,
            None,
            wind_down_hours=args.wind_down_hours,
            demand_share=args.demand_share,
            capabilities=capabilities,
            rules=rules_for_house(House.parse(args.house)),
        )
        result.metadata.update(
            {
                "house": args.house,
                "client_build": client_build,
                "window": {"start": args.start, "end": args.end, "build": args.build},
                "capital_copper": capital,
                "capabilities": capabilities.canonical,
                "capabilities_hash": capabilities_hash(capabilities),
                "wind_down_hours": args.wind_down_hours,
                "demand_share": args.demand_share,
                "selected_sweeps": len(selected_sweeps),
                "excluded_sweeps_by_window": excluded_by_window,
                "excluded_sweeps_by_build": excluded_by_build,
                "excluded_sweeps_without_books": excluded_without_books,
            }
        )
        write_report(result, args.out)
        print(text_summary(result.report()))
    except (PaperError, RuntimeError, ValueError, OSError, psycopg.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
