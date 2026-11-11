"""``python -m crowley.forecast validate``: score commodity forecasts from stored history."""

from __future__ import annotations

import argparse
import math
import os
import sys

import psycopg

from crowley.advisor.model import OptionError
from crowley.analysis.books import load_commodity_books
from crowley.analysis.rank import item_names, latest_sweep, parse_as_of
from crowley.analysis.time_to_sale import CHECKPOINTS
from crowley.core.errors import MarketDataNotFound
from crowley.trading.strategies.common import SECONDS_PER_HOUR

from .profile import current_context
from .report import build_report, report_json, report_text
from .series import build_series
from .validate import history_start, walk_forward


def build_parser() -> argparse.ArgumentParser:
    """Build the forecast validation CLI parser."""

    parser = argparse.ArgumentParser(prog="python -m crowley.forecast", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="score models with walk-forward validation")
    validate.add_argument("--house", required=True, help="explicit house key")
    validate.add_argument("--build", help="full client build; default is the latest sweep's build")
    validate.add_argument("--as-of", help="ISO timestamp with offset; default is the latest sweep")
    validate.add_argument("--horizon-hours", type=float, default=2.0)
    validate.add_argument("--lookback-hours", type=float, default=24.0)
    validate.add_argument("--items", help="comma-separated positive item ids")
    validate.add_argument("--limit", type=int, default=20)
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--database-url", help="defaults to $CROWLEY_DATABASE_URL")
    return parser


def _items(text: str | None) -> set[int] | None:
    if text is None:
        return None
    parts = [part.strip() for part in text.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("--items must be comma-separated positive integers")
    try:
        values = {int(part) for part in parts}
    except ValueError as exc:
        raise ValueError("--items must be comma-separated positive integers") from exc
    if any(value <= 0 for value in values):
        raise ValueError("--items must be comma-separated positive integers")
    return values


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not math.isfinite(args.horizon_hours):
        parser.error("--horizon-hours must be finite")
    horizon_s = args.horizon_hours * SECONDS_PER_HOUR
    if horizon_s not in CHECKPOINTS:
        checkpoint_hours = [checkpoint / SECONDS_PER_HOUR for checkpoint in CHECKPOINTS]
        parser.error(f"horizon_hours must be one of {checkpoint_hours}")
    if not math.isfinite(args.lookback_hours) or args.lookback_hours < 1:
        parser.error("lookback_hours must be at least 1")
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        item_ids = _items(args.items)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        as_of = None if args.as_of is None else parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    lookback_s = args.lookback_hours * SECONDS_PER_HOUR
    url = args.database_url or os.environ.get("CROWLEY_DATABASE_URL")
    if not url:
        parser.error("--database-url or CROWLEY_DATABASE_URL is required")

    try:
        with psycopg.connect(url, autocommit=True) as conn:
            sweep_id, finished_t, build = latest_sweep(conn, args.house, args.build, as_of)
            loaded = load_commodity_books(
                conn,
                args.house,
                start_t=int(history_start(finished_t, lookback_s)),
                end_t=int(finished_t),
                client_build=build,
            )
            series_by_item = {
                item_id: build_series(item_id, [book for book in books if book.t <= finished_t])
                for item_id, books in sorted(loaded.items())
            }
            validations = walk_forward(
                series_by_item,
                finished_t,
                horizon_s,
                lookback_s,
                score_item_ids=item_ids,
            )
            context = current_context(series_by_item, finished_t, lookback_s)
            names = item_names(conn, build, sorted(validations))
    except (MarketDataNotFound, OptionError) as exc:
        parser.error(str(exc))

    report = build_report(
        house=args.house,
        build=build,
        sweep_id=sweep_id,
        as_of_t=finished_t,
        horizon_s=horizon_s,
        lookback_s=lookback_s,
        series_by_item=series_by_item,
        validations=validations,
        context=context,
        names=names,
        limit=args.limit,
    )
    print(report_json(report) if args.json else report_text(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate(args, parser)
    raise RuntimeError(f"unsupported forecast command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
