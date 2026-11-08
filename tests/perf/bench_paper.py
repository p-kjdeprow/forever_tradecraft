"""Benchmark the in-memory paper backtest on deterministic synthetic history.

This module is intentionally not collected by the default test suite.  Run it
with ``uv run python tests/perf/bench_paper.py`` from the repository root.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections.abc import Sequence
from fractions import Fraction

from crowley.analysis.books import Book, Group
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.core.watchlist import load_catalog
from crowley.trading import paper
from crowley.trading.api import ItemFacts, Recipe
from crowley.trading.engine import run_backtest
from crowley.trading.statistics import prepare_statistics
from crowley.trading.strategies import build

DEFAULT_SEED = 4411
DEFAULT_SWEEPS = (200, 2000)
RULES = rules_for_house(House("us", "normal", "alliance"))


def synthetic_history(
    *, item_ids: Sequence[int], sweeps: int, seed: int
) -> tuple[dict[int, list[Book]], dict[int, ItemFacts]]:
    """Build a stable, bounded commodity history for performance comparisons."""

    if not item_ids or sweeps <= 1:
        raise ValueError("item_ids must not be empty and sweeps must exceed one")
    generator = random.Random(seed)
    books: dict[int, list[Book]] = {}
    facts: dict[int, ItemFacts] = {}
    for item_id in item_ids:
        base_price = 100 + generator.randrange(10_000)
        item_books: list[Book] = []
        for sweep in range(sweeps):
            groups = [
                Group(
                    result_id=level,
                    unit=Fraction(base_price + level * 5 + generator.randrange(-2, 3)),
                    quantity=20 + generator.randrange(10),
                    time_left_seconds=172_800,
                )
                for level in range(20)
            ]
            item_books.append(
                Book(
                    t=sweep * 900,
                    groups=groups,
                    item_id=item_id,
                    sweep_id=sweep,
                )
            )
        books[item_id] = item_books
        facts[item_id] = ItemFacts(item_id, 1, None, None, 20, 0)
    return books, facts


def benchmark(*, item_ids: Sequence[int], sweeps: int, seed: int) -> tuple[float, float, float, int]:
    books, facts = synthetic_history(item_ids=item_ids, sweeps=sweeps, seed=seed)
    recipes = (
        Recipe(1, frozenset({"alchemy"}), ((1, 1),), ((2, 1),), None, 0, "benchmark"),
        Recipe(2, frozenset({"engineering"}), ((3, 1),), ((4, 1),), None, 0, "benchmark"),
    )
    strategies = [build(name) for name in paper.DEFAULT_STRATEGIES]
    started = time.perf_counter()
    precomputation = prepare_statistics(books)
    precompute_seconds = time.perf_counter() - started
    print(
        f"items={len(item_ids)} sweeps={sweeps} seed={seed} precompute_seconds={precompute_seconds:.6f}",
        flush=True,
    )
    run_started = time.perf_counter()
    result = run_backtest(
        books,
        precomputation.statistics,
        facts,
        recipes,
        strategies,
        None,
        200_000,
        wind_down_hours=0,
        rules=RULES,
        intervals=precomputation.intervals,
        accumulator_series=precomputation.accumulator_series,
        capture_states=True,
        capture_state_history=False,
    )
    backtest_seconds = time.perf_counter() - run_started
    checkpoint_size = len(
        json.dumps(
            paper._state_blob(result.engine_states[-1]),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return precompute_seconds, backtest_seconds, precompute_seconds + backtest_seconds, checkpoint_size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--items",
        type=int,
        help="override the real catalog with the first N catalog items (for local smoke tests)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--sweeps", type=int, nargs="+", default=DEFAULT_SWEEPS)
    args = parser.parse_args()
    catalog_ids = tuple(item.item_id for item in load_catalog().items if item.status == "active")
    item_ids = catalog_ids if args.items is None else catalog_ids[: args.items]
    for sweeps in args.sweeps:
        precompute_seconds, backtest_seconds, total_seconds, checkpoint_size = benchmark(
            item_ids=item_ids,
            sweeps=sweeps,
            seed=args.seed,
        )
        print(
            f"items={len(item_ids)} sweeps={sweeps} seed={args.seed} "
            f"precompute_seconds={precompute_seconds:.6f} "
            f"backtest_seconds={backtest_seconds:.6f} "
            f"total_seconds={total_seconds:.6f} "
            f"per_step_seconds={total_seconds / sweeps:.9f} "
            f"checkpoint_size_bytes={checkpoint_size}",
            flush=True,
        )


if __name__ == "__main__":
    main()
