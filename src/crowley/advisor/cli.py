"""``python -m crowley.advisor`` / ``crowley-advise``: ranked pathways from the stored scan history."""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

from crowley.advisor.model import DEFAULT_PROFESSIONS, STRATEGIES
from crowley.advisor.render import advice_json, format_table
from crowley.advisor.service import make_config, run_advice
from crowley.analysis.rank import parse_as_of


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crowley-advise", description=__doc__)
    parser.add_argument("--database-url", help="defaults to $CROWLEY_DATABASE_URL")
    parser.add_argument("--house", required=True, help="explicit house key, e.g. us-normal-alliance")
    parser.add_argument("--build", help="full client build, e.g. 1.60.1.70862 (default: the latest sweep's)")
    parser.add_argument("--as-of", help="ISO timestamp with offset; default is the latest sweep")
    parser.add_argument(
        "--strategies", help=f"comma-separated subset of {','.join(STRATEGIES)} (default all)"
    )
    parser.add_argument(
        "--professions",
        help=f"comma-separated crafting professions (default {','.join(sorted(DEFAULT_PROFESSIONS))})",
    )
    parser.add_argument("--min-profit", type=int, default=0, help="copper, per craft (crafting) or per unit")
    parser.add_argument("--min-volume", type=float, default=0.0, help="market units per hour")
    parser.add_argument("--share", type=float, default=0.10, help="capture share of the market flow")
    parser.add_argument("--horizon-hours", type=float, default=2.0, help="one of 2, 6, 12, 24")
    parser.add_argument("--min-sale-prob", type=float, default=0.5, help="crafting P(sale) floor")
    parser.add_argument(
        "--price-basis",
        choices=("lowest", "target"),
        default="lowest",
        help="crafting headline price: 1c under the current lowest listing, or rank's target",
    )
    parser.add_argument(
        "--max-cold-age-hours", type=float, default=3.0, help="exclude cold-tier rows with older prices"
    )
    parser.add_argument("--flip-x", type=float, default=0.7, help="buy units at or below this x fair value")
    parser.add_argument("--flip-undercut", type=float, default=0.12, help="relist at fair x (1 - this)")
    parser.add_argument("--character", help="classify recipes against this character's latest capture")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--steps", action="store_true", help="print each pathway's steps under its row")
    parser.add_argument("--exclusions", action="store_true", help="with --json: list every exclusion")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = make_config(
            strategies=args.strategies,
            professions=args.professions,
            min_profit=args.min_profit,
            min_volume=args.min_volume,
            share=args.share,
            horizon_hours=args.horizon_hours,
            min_sale_prob=args.min_sale_prob,
            price_basis=args.price_basis,
            max_cold_age_hours=args.max_cold_age_hours,
            flip_x=args.flip_x,
            flip_undercut=args.flip_undercut,
        )
        as_of = None if args.as_of is None else parse_as_of(args.as_of)
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.exclusions and not args.json:
        parser.error("--exclusions requires --json")
    url = args.database_url or os.environ.get("CROWLEY_DATABASE_URL")
    if not url:
        parser.error("--database-url or CROWLEY_DATABASE_URL is required")
    with psycopg.connect(url, autocommit=True) as conn:
        advice, state = run_advice(
            conn, args.house, config, build=args.build, as_of=as_of, character=args.character
        )
    if args.json:
        print(advice_json(advice, dict(state.names), args.limit, args.exclusions))
    else:
        print(format_table(advice, dict(state.names), args.limit, args.steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
