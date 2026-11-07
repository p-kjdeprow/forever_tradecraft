from __future__ import annotations

import hashlib
from fractions import Fraction

from crowley.analysis.books import Book, Group
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.trading.api import ItemFacts
from crowley.trading.engine import run_backtest
from crowley.trading.report import json_report
from crowley.trading.strategies.vendor_arb import VendorArbStrategy


def _book(t: int, sweep_id: int, quantity: int) -> Book:
    return Book(
        t=t,
        groups=[Group("auction", Fraction(50), quantity, 100_000)] if quantity else [],
        item_id=7,
        sweep_id=sweep_id,
    )


def test_synthetic_backtest_report_is_golden() -> None:
    books = {7: [_book(0, 1, 3), _book(900, 2, 3), _book(1_800, 3, 0)]}
    facts = {7: ItemFacts(7, 100, 1, 1, 20, 0)}
    rules = rules_for_house(House("us", "normal", "alliance"))

    result = run_backtest(
        books,
        {},
        facts,
        (),
        [VendorArbStrategy()],
        None,
        1_000,
        wind_down_hours=0,
        rules=rules,
    )

    report = json_report(result).encode("utf-8")
    assert (
        hashlib.sha256(report).hexdigest()
        == "61bc3816a27b12a154d5d6b798e5986a04ee4694bf5f552069def6e441eb940e"
    )
