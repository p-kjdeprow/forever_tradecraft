"""Command-line report for commodity time-to-sale analysis."""

from __future__ import annotations

import argparse
import json
import sys

import psycopg

from .books import load_commodity_books
from .common import jsonable
from .time_to_sale import evaluate_rules, validate


def _validation_line(label: str, report: dict) -> str:
    parts = []
    for name in ("cancellation_censored", "cancellation_unsold"):
        scoring = report[name]
        if scoring["single_class"]:
            parts.append(f"{name}: single-class, not informative")
        else:
            parts.append(f"{name} Brier model={scoring['brier_model']} baseline={scoring['brier_baseline']}")
    return f"validation {label}: " + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if args_list and args_list[0] == "rank":
        from .rank import main as rank_main

        return rank_main(args_list[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--house", required=True, help="explicit house key")
    parser.add_argument("--out", required=True, help="JSON report path")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--tie-share", type=float, default=0.0)
    parser.add_argument("--split-t", type=float)
    args = parser.parse_args(args_list)
    if args.stride < 1:
        parser.error("--stride must be positive")
    with psycopg.connect(args.database_url) as conn:
        books = load_commodity_books(conn, args.house)
    tables = evaluate_rules(books, stride=args.stride, tie_share=args.tie_share)
    validation = validate(books, split_t=args.split_t, stride=args.stride, tie_share=args.tie_share)
    report = {
        "house": args.house,
        "parameters": {
            "stride": args.stride,
            "tie_share": args.tie_share,
            "split_t": validation["split_t"],
        },
        "items": tables,
        "validation": validation,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(jsonable(report), handle, indent=2, sort_keys=True)
        handle.write("\n")

    undercut = []
    for item_id, rules in tables.items():
        metric = rules.get("undercut", {}).get(20)
        if metric is not None:
            undercut.append((item_id, metric["probabilities"].get(21600, 0.0)))
    undercut.sort(key=lambda pair: (-pair[1], str(pair[0])))
    print(f"items: {len(tables)}")
    print("P(undercut, q=20, sold within 6h):")
    for item_id, probability in undercut:
        print(f"  {item_id}: {probability:.6f}")
    for label in ("2h", "6h", "24h"):
        print(_validation_line(label, validation[label]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
