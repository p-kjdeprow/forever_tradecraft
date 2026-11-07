"""Per-strategy and aggregate backtest reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from fractions import Fraction
from math import floor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .accounts import Account

if TYPE_CHECKING:
    from .engine import BacktestResult

COUNT_KEYS = (
    "units_bought",
    "units_sold",
    "units_vendored",
    "units_vendor_bought",
    "crafted",
    "crafts",
    "craft_truncated",
    "posts",
    "cancels",
    "expiries",
)
FEE_KEYS = ("ah_cut", "deposits_lost", "deposits_paid", "deposits_refunded")
REPORT_CAPACITY_SHARE = Fraction(1, 4)


def _value(value: Any) -> Any:
    if isinstance(value, Fraction):
        return value.numerator if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _drawdown(
    curve: list[tuple[float, Fraction]], initial: Fraction = Fraction(0)
) -> tuple[Fraction, Fraction]:
    peak = initial
    largest = Fraction(0)
    largest_pct = Fraction(0)
    for _, equity in curve:
        if equity > peak:
            peak = equity
        if peak:
            drawdown = peak - equity
            if drawdown > largest:
                largest = drawdown
                largest_pct = drawdown / peak * 100
    return largest, largest_pct


def _trade_list(account: Account) -> list[dict[str, Any]]:
    return sorted(account.closed_trades, key=lambda trade: (trade["profit"], trade["t"]), reverse=True)[:10]


def _capacity_diagnostic(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize held-capacity ratios without losing the per-step evidence."""

    by_item: dict[int, dict[str, Any]] = {}
    report_by_item: dict[int, dict[str, Any]] = {}
    for observation in observations:
        item_id = int(observation["item_id"])
        held = max(0, int(observation["held"]))
        demand = observation.get("demand_units_per_day")
        own_share = observation.get("capacity_share", REPORT_CAPACITY_SHARE)
        own_share = own_share if isinstance(own_share, Fraction) else Fraction(str(own_share))
        demand_value = (
            None if demand is None else (demand if isinstance(demand, Fraction) else Fraction(demand))
        )
        own_capacity = max(1, floor(own_share * demand_value)) if demand_value is not None else 1
        report_capacity = (
            max(1, floor(REPORT_CAPACITY_SHARE * demand_value)) if demand_value is not None else 1
        )
        own_ratio = Fraction(held, own_capacity)
        report_ratio = Fraction(held, report_capacity)
        current = by_item.get(item_id)
        if current is None or own_ratio > current["max_held_over_capacity"]:
            by_item[item_id] = {
                "item_id": item_id,
                "max_held_over_capacity": own_ratio,
                "max_held": held,
                "capacity_units": own_capacity,
            }
        report_current = report_by_item.get(item_id)
        if report_current is None or report_ratio > report_current["max_held_over_capacity"]:
            report_by_item[item_id] = {
                "item_id": item_id,
                "max_held_over_capacity": report_ratio,
                "max_held": held,
                "capacity_units": report_capacity,
            }
    top_items = sorted(
        by_item.values(),
        key=lambda item: (-item["max_held_over_capacity"], item["item_id"]),
    )[:5]
    report_top_items = sorted(
        report_by_item.values(),
        key=lambda item: (-item["max_held_over_capacity"], item["item_id"]),
    )[:5]
    maximum = max(
        (item["max_held_over_capacity"] for item in by_item.values()),
        default=Fraction(0),
    )
    report_maximum = max(
        (item["max_held_over_capacity"] for item in report_by_item.values()),
        default=Fraction(0),
    )
    return {
        "capacity_share": (
            observations[0].get("capacity_share", REPORT_CAPACITY_SHARE)
            if observations
            else REPORT_CAPACITY_SHARE
        ),
        "report_capacity_share": REPORT_CAPACITY_SHARE,
        "max_held_over_capacity": maximum,
        "top_items": top_items,
        "report_max_held_over_capacity": report_maximum,
        "report_top_items": report_top_items,
    }


def _strategy_report(
    name: str,
    account: Account,
    curve: list[tuple[float, Fraction]],
    covered_days: Fraction,
    observations: list[dict[str, Any]],
    wind_down_checkpoint: Mapping[str, Fraction] | None = None,
    stale_marked_items: int = 0,
) -> dict[str, Any]:
    final_equity = curve[-1][1] if curve else Fraction(account.cash_balance + account.pending_cash())
    cash_and_pending = account.cash_balance + account.pending_cash()
    unrealized = final_equity - cash_and_pending
    realized = cash_and_pending - account.start_capital
    drawdown, drawdown_pct = _drawdown(curve, Fraction(account.start_capital))
    net_per_day = (final_equity - account.start_capital) / covered_days if covered_days else None
    counts = {key: account.counts.get(key, 0) for key in COUNT_KEYS}
    fees = {key: account.fees.get(key, 0) for key in FEE_KEYS}
    capacity = _capacity_diagnostic(observations)
    wind_down_checkpoint = wind_down_checkpoint or {}
    return {
        "name": name,
        "start_capital": account.start_capital,
        "equity": final_equity,
        "final_equity": final_equity,
        "return_pct": None
        if account.start_capital == 0
        else (final_equity / account.start_capital - 1) * 100,
        "realized": realized,
        "realized_at_wind_down_start": wind_down_checkpoint.get("realized", Fraction(0)),
        "equity_at_wind_down_start": wind_down_checkpoint.get("equity", Fraction(account.start_capital)),
        "unrealized": unrealized,
        "stale_marked_items": stale_marked_items,
        "fees": fees,
        "counts": counts,
        "rejections": dict(sorted(account.rejections.items())),
        "clicks": account.clicks,
        "max_drawdown": drawdown,
        "max_drawdown_pct": drawdown_pct,
        "equity_curve": [{"t": t, "equity": equity} for t, equity in curve],
        "gold_per_day": None if net_per_day is None else net_per_day / 10_000,
        "top_closed_trades": _trade_list(account),
        "capacity_share": capacity["capacity_share"],
        "max_held_over_capacity": capacity["max_held_over_capacity"],
        "capacity_top_items": capacity["top_items"],
        "capacity_diagnostic": capacity,
    }


def _sum_reports(
    reports: list[dict[str, Any]], curve: list[tuple[float, Fraction]], start: int
) -> dict[str, Any]:
    final_equity = curve[-1][1] if curve else Fraction(start)
    realized = sum((report["realized"] for report in reports), Fraction(0))
    realized_at_wind_down_start = sum(
        (report["realized_at_wind_down_start"] for report in reports), Fraction(0)
    )
    equity_at_wind_down_start = sum((report["equity_at_wind_down_start"] for report in reports), Fraction(0))
    unrealized = sum((report["unrealized"] for report in reports), Fraction(0))
    stale_marked_items = sum(report["stale_marked_items"] for report in reports)
    counts = {key: sum(report["counts"].get(key, 0) for report in reports) for key in COUNT_KEYS}
    fees = {key: sum(report["fees"].get(key, 0) for report in reports) for key in FEE_KEYS}
    rejections: dict[str, int] = {}
    for report in reports:
        for reason, count in report["rejections"].items():
            rejections[reason] = rejections.get(reason, 0) + count
    drawdown, drawdown_pct = _drawdown(curve, Fraction(start))
    covered_days = Fraction(str(curve[-1][0] - curve[0][0])) / 86400 if len(curve) > 1 else Fraction(0)
    return {
        "name": "total",
        "start_capital": start,
        "equity": final_equity,
        "final_equity": final_equity,
        "return_pct": None if start == 0 else (final_equity / start - 1) * 100,
        "realized": realized,
        "realized_at_wind_down_start": realized_at_wind_down_start,
        "equity_at_wind_down_start": equity_at_wind_down_start,
        "unrealized": unrealized,
        "stale_marked_items": stale_marked_items,
        "fees": fees,
        "counts": counts,
        "rejections": dict(sorted(rejections.items())),
        "clicks": sum(report["clicks"] for report in reports),
        "max_drawdown": drawdown,
        "max_drawdown_pct": drawdown_pct,
        "equity_curve": [{"t": t, "equity": equity} for t, equity in curve],
        "gold_per_day": None if not covered_days else (final_equity - start) / covered_days / 10_000,
        "top_closed_trades": sorted(
            (trade for report in reports for trade in report["top_closed_trades"]),
            key=lambda trade: trade["profit"],
            reverse=True,
        )[:10],
        "max_held_over_capacity": max(
            (report["max_held_over_capacity"] for report in reports), default=Fraction(0)
        ),
    }


def build_report(result: BacktestResult) -> dict[str, Any]:
    covered_days = (
        Fraction(str(result.times[-1] - result.times[0])) / 86400 if len(result.times) > 1 else Fraction(0)
    )
    strategies = [
        _strategy_report(
            name,
            account,
            result.equity_curves.get(name, []),
            covered_days,
            result.capacity_observations.get(name, []),
            result.wind_down_checkpoints.get(name),
            result.stale_mark_counts.get(name, 0),
        )
        for name, account in result.accounts.items()
    ]
    return {
        "start_capital": result.start_capital,
        "covered_days": covered_days,
        "metadata": result.metadata,
        "strategies": {report["name"]: report for report in strategies},
        "total": _sum_reports(strategies, result.total_equity_curve, result.start_capital),
    }


def json_report(result: BacktestResult) -> str:
    return json.dumps(_value(build_report(result)), indent=2, sort_keys=True) + "\n"


def write_report(result: BacktestResult, path: str | Path) -> None:
    Path(path).write_text(json_report(result), encoding="utf-8")


def text_summary(report: Mapping[str, Any]) -> str:
    def gold(value: Any) -> str:
        return f"{float(value) / 10_000:.2f}g"

    def percentage(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.1f}%"

    total = report["total"]
    lines = [
        "total: "
        f"start={gold(total['start_capital'])}, "
        f"equity={gold(total['equity'])}, "
        f"realized={gold(total['realized'])}, "
        f"return={percentage(total['return_pct'])}, "
        f"clicks={total['clicks']}, "
        f"max_held_over_capacity={float(total.get('max_held_over_capacity', 0)):.2f}"
    ]
    for name, strategy in report["strategies"].items():
        lines.append(
            f"{name}: start={gold(strategy['start_capital'])}, "
            f"equity={gold(strategy['equity'])}, "
            f"realized={gold(strategy['realized'])}, "
            f"return={percentage(strategy['return_pct'])}, "
            f"clicks={strategy['clicks']}, "
            f"max_held_over_capacity={float(strategy['max_held_over_capacity']):.2f}"
        )
    return "\n".join(lines)


__all__ = ["build_report", "json_report", "text_summary", "write_report"]
