"""Pre-model commodity market metrics.

The functions in this module deliberately operate on the exact ``Book`` and
``Interval`` objects used by the time-to-sale analysis.  In particular, they
do not infer removals from a different scope or repair a broken observation
chain.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import median, stdev
from typing import Any

import psycopg

from .books import (
    EXPIRY_SLACK,
    Book,
    Group,
    Interval,
    load_commodity_books,
    valid_intervals,
)
from .common import jsonable, median_pairwise_slope, reference
from .time_to_sale import kaplan_meier

FAIR_WINDOW_SECONDS = 86400
FAIR_MIN_BOOKS = 8
DEPTH_QUANTITIES = (1, 20, 100, 500)
FAIR_MULTIPLIERS = (Fraction(7, 10), Fraction(8, 10), Fraction(9, 10), Fraction(1))
OPPORTUNITY_MULTIPLIERS = (Fraction(6, 10), Fraction(7, 10), Fraction(8, 10))
CAPACITY_MULTIPLIERS = (Fraction(9, 10), Fraction(1), Fraction(11, 10))

PARAMETERS = {
    "fair_window_seconds": FAIR_WINDOW_SECONDS,
    "fair_min_books": FAIR_MIN_BOOKS,
    "depth_quantities": DEPTH_QUANTITIES,
    "fair_multipliers": FAIR_MULTIPLIERS,
    "opportunity_multipliers": OPPORTUNITY_MULTIPLIERS,
    "capacity_multipliers": CAPACITY_MULTIPLIERS,
}


@dataclass(frozen=True)
class TrailingDemand:
    """Bottom-walk demand and its exact coverage for a sequence of intervals."""

    units_per_hour: Fraction | None
    usable_intervals: int
    covered_seconds: float


def _items(books: dict[Any, Sequence[Book]] | Sequence[Book]) -> dict[Any, list[Book]]:
    if isinstance(books, dict):
        return {item_id: list(item_books) for item_id, item_books in books.items()}
    item_id = books[0].item_id if books else 0
    return {item_id: list(books)}


def _time_fraction(seconds: float) -> Fraction:
    return Fraction(str(seconds))


def _rate(total: int | Fraction, covered_seconds: float) -> Fraction | None:
    hourly = units_per_hour(total, covered_seconds)
    return None if hourly is None else hourly * 24


def units_per_hour(total: int | Fraction, covered_seconds: float) -> Fraction | None:
    """Return an exact hourly rate, or ``None`` when no time was covered."""

    if covered_seconds <= 0:
        return None
    return Fraction(total) * 3600 / _time_fraction(covered_seconds)


def _multiplier_key(multiplier: Fraction) -> str:
    if multiplier.denominator == 1:
        return str(multiplier.numerator)
    return f"{float(multiplier):g}"


def _group_key(group: Group) -> tuple[Fraction, str]:
    return group.unit, str(group.result_id)


def _scoped_groups(book: Book, ceiling: Fraction | None = None) -> list[Group]:
    limit = book.ceiling if ceiling is None else ceiling
    return sorted(
        (group for group in book.groups if limit is None or group.unit <= limit),
        key=_group_key,
    )


def _find_group(book: Book, result_id: int | str, ceiling: Fraction | None = None) -> Group | None:
    limit = book.ceiling if ceiling is None else ceiling
    for group in book.groups:
        if group.result_id == result_id and (limit is None or group.unit <= limit):
            return group
    return None


def percentile(values: Sequence[Fraction], probability: Fraction) -> Fraction:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = position.numerator // position.denominator
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _fair_at(
    books: Sequence[Book], index: int, *, include_trend: bool = True, min_books: int = FAIR_MIN_BOOKS
) -> dict[str, Any]:
    current_t = books[index].t
    window_start = current_t - FAIR_WINDOW_SECONDS
    observations: list[tuple[float, Fraction]] = []
    for book in books:
        if window_start <= book.t < current_t:
            book_reference = reference(book)
            if book_reference is not None:
                observations.append((book.t, book_reference))
    refs = [value for _, value in observations]
    if len(refs) < min_books:
        return {
            "value": None,
            "band": {"p25": None, "p75": None},
            "trend_per_day": None,
            "n": len(refs),
        }

    trend = None
    if include_trend:
        trend = median_pairwise_slope(
            [(observation_t / 86400, math.log(float(value))) for observation_t, value in observations]
        )
    p25 = percentile(refs, Fraction(1, 4))
    p75 = percentile(refs, Fraction(3, 4))
    return {
        "value": percentile(refs, Fraction(1, 2)),
        "band": {"p25": p25, "p75": p75},
        "trend_per_day": trend,
        "n": len(refs),
    }


def fair_value(
    books: Sequence[Book], index: int = -1, *, include_trend: bool = True, min_books: int = FAIR_MIN_BOOKS
) -> dict[str, Any]:
    """Return the strictly-past 24-hour fair-value estimate at ``index``."""

    if not books:
        return {
            "value": None,
            "band": {"p25": None, "p75": None},
            "trend_per_day": None,
            "n": 0,
        }
    return _fair_at(books, index % len(books), include_trend=include_trend, min_books=min_books)


def _fair_series(books: Sequence[Book]) -> list[dict[str, Any]]:
    return [_fair_at(books, index, include_trend=False) for index in range(len(books))]


def depth_metrics(book: Book, fair: Fraction | None = None) -> dict[str, Any]:
    """Calculate exact depth and fair-relative availability for one book."""

    groups = _scoped_groups(book)
    depths: dict[str, dict[str, Fraction | None]] = {}
    for requested in DEPTH_QUANTITIES:
        remaining = requested
        total_cost = Fraction(0)
        marginal: Fraction | None = None
        for group in groups:
            taken = min(remaining, group.quantity)
            if taken:
                total_cost += group.unit * taken
                marginal = group.unit
                remaining -= taken
            if remaining == 0:
                break
        if remaining:
            depths[str(requested)] = {
                "total_cost": None,
                "vwap": None,
                "marginal_unit_price": None,
            }
        else:
            depths[str(requested)] = {
                "total_cost": total_cost,
                "vwap": total_cost / requested,
                "marginal_unit_price": marginal,
            }

    units_at_or_below: dict[str, int | None] = {}
    for multiplier in FAIR_MULTIPLIERS:
        key = _multiplier_key(multiplier)
        if fair is None:
            units_at_or_below[key] = None
            continue
        threshold = multiplier * fair
        if book.ceiling is not None and threshold > book.ceiling:
            units_at_or_below[key] = None
            continue
        units_at_or_below[key] = sum(group.quantity for group in groups if group.unit <= threshold)
    return {"quantities": depths, "units_at_or_below_fair": units_at_or_below}


def _removals(interval: Interval) -> list[tuple[Fraction, int]]:
    """Return all non-expiring removals in the interval's common scope."""

    after_by_id = {group.result_id: group for group in interval.after.groups}
    removals: list[tuple[Fraction, int]] = []
    for group in _scoped_groups(interval.before, interval.ceiling):
        next_group = after_by_id.get(group.result_id)
        next_quantity = next_group.quantity if next_group is not None else 0
        if next_quantity == 0 and group.time_left_seconds <= interval.dt + EXPIRY_SLACK:
            continue
        removed = max(group.quantity - next_quantity, 0)
        if removed:
            removals.append((group.unit, removed))
    return removals


def demand_metrics(
    books: Sequence[Book], intervals: Sequence[Interval | None] | None = None
) -> dict[str, Any]:
    """Aggregate bottom-walk and all-removal demand over valid intervals."""

    source = valid_intervals(books) if intervals is None else intervals
    valid = [interval for interval in source if interval is not None]
    covered_seconds = sum(interval.dt for interval in valid)
    purchased_units = sum(quantity for interval in valid for _, quantity in interval.walk.consumed)
    purchased_copper = sum(
        (unit * quantity for interval in valid for unit, quantity in interval.walk.consumed),
        Fraction(0),
    )
    upper = [removal for interval in valid for removal in _removals(interval)]
    upper_units = sum(quantity for _, quantity in upper)
    upper_copper = sum((unit * quantity for unit, quantity in upper), Fraction(0))
    non_bottom_units = upper_units - purchased_units
    non_bottom_copper = upper_copper - purchased_copper
    return {
        "covered_seconds": covered_seconds,
        "eligible_covered_seconds": covered_seconds,
        "covered_days": _time_fraction(covered_seconds) / 86400 if covered_seconds else Fraction(0),
        "purchased_units": purchased_units,
        "purchased_copper": purchased_copper,
        "purchased_units_per_day": _rate(purchased_units, covered_seconds),
        "purchased_units_per_day_covered_seconds": covered_seconds,
        "copper_per_day": _rate(purchased_copper, covered_seconds),
        "copper_per_day_covered_seconds": covered_seconds,
        "units_per_day": _rate(purchased_units, covered_seconds),
        "units_per_day_covered_seconds": covered_seconds,
        "non_bottom_units": non_bottom_units,
        "non_bottom_copper": non_bottom_copper,
        "non_bottom_units_per_day": _rate(non_bottom_units, covered_seconds),
        "non_bottom_units_per_day_covered_seconds": covered_seconds,
        "non_bottom_copper_per_day": _rate(non_bottom_copper, covered_seconds),
        "non_bottom_copper_per_day_covered_seconds": covered_seconds,
    }


def trailing_demand(intervals: Sequence[Interval | None]) -> TrailingDemand:
    """Aggregate bottom-walk units over the valid intervals, exactly as the advisor does."""

    valid = [interval for interval in intervals if interval is not None]
    covered_seconds = sum((interval.dt for interval in valid), 0.0)
    units = sum(quantity for interval in valid for _, quantity in interval.walk.consumed)
    per_hour = units_per_hour(units, covered_seconds)
    return TrailingDemand(
        per_hour,
        len(valid),
        covered_seconds,
    )


def _cheapest_groups(book: Book, ceiling: Fraction | None = None) -> list[Group]:
    groups = _scoped_groups(book, ceiling)
    if not groups:
        return []
    minimum = groups[0].unit
    return [group for group in groups if group.unit == minimum]


def _undercut_events(books: Sequence[Book], intervals: Sequence[Interval | None]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, interval in enumerate(intervals):
        if interval is None:
            continue
        before_groups = _scoped_groups(interval.before, interval.ceiling)
        if not before_groups:
            continue
        before_ids = {group.result_id for group in before_groups}
        minimum = before_groups[0].unit
        for group in _scoped_groups(interval.after, interval.ceiling):
            if group.result_id not in before_ids and group.unit < minimum:
                events.append(
                    {
                        "index": index + 1,
                        "time": interval.after.t,
                        "result_id": group.result_id,
                        "price": group.unit,
                        "previous_minimum": minimum,
                        "relative_depth": 1 - group.unit / minimum,
                    }
                )
    return events


def _tenure_samples(books: Sequence[Book], intervals: Sequence[Interval | None]) -> list[tuple[float, bool]]:
    samples: list[tuple[float, bool]] = []
    for start_index in range(1, len(books)):
        start_interval = intervals[start_index - 1]
        if start_interval is None:
            continue
        current_groups = _cheapest_groups(books[start_index], start_interval.ceiling)
        previous_groups = _cheapest_groups(books[start_index - 1], start_interval.ceiling)
        previous_ids = {group.result_id for group in previous_groups}
        for current in current_groups:
            if current.result_id in previous_ids:
                continue
            start_t = books[start_index].t
            for index in range(start_index, len(books) - 1):
                interval = intervals[index]
                if interval is None:
                    samples.append((books[index].t - start_t, False))
                    break
                if interval.ceiling is not None and current.unit > interval.ceiling:
                    samples.append((books[index + 1].t - start_t, False))
                    break
                next_group = _find_group(interval.after, current.result_id, interval.ceiling)
                if next_group is None:
                    samples.append((interval.after.t - start_t, False))
                    break
                if any(
                    group.unit < current.unit for group in _scoped_groups(interval.after, interval.ceiling)
                ):
                    samples.append((interval.after.t - start_t, True))
                    break
            else:
                samples.append((books[-1].t - start_t, False))
    return samples


def competition_metrics(
    books: Sequence[Book], intervals: Sequence[Interval | None] | None = None
) -> dict[str, Any]:
    """Measure undercuts, cheapest-group tenure, and seller counts."""

    valid = valid_intervals(books) if intervals is None else list(intervals)
    valid_only = [interval for interval in valid if interval is not None]
    covered_seconds = sum(interval.dt for interval in valid_only)
    events = _undercut_events(books, valid)
    relative_depths = [event["relative_depth"] for event in events]
    tenure_samples = _tenure_samples(books, valid)
    tenure_km = kaplan_meier(tenure_samples, checkpoints=(3600,))
    seller_group_counts: list[int] = []
    seller_owner_sums: list[int] = []
    for book in books:
        groups = _scoped_groups(book)
        price_levels: list[Fraction] = []
        owner_sum = 0
        group_count = 0
        for group in groups:
            if group.unit not in price_levels:
                if len(price_levels) == 5:
                    break
                price_levels.append(group.unit)
            owner_sum += group.owners
            group_count += 1
        seller_group_counts.append(group_count)
        seller_owner_sums.append(owner_sum)
    median_groups = median(seller_group_counts) if seller_group_counts else None
    median_owners = median(seller_owner_sums) if seller_owner_sums else None
    return {
        "covered_seconds": covered_seconds,
        "eligible_covered_seconds": covered_seconds,
        "undercut_events": len(events),
        "undercut_events_per_hour": (
            Fraction(len(events)) * 3600 / _time_fraction(covered_seconds) if covered_seconds else None
        ),
        "undercut_events_per_hour_covered_seconds": covered_seconds,
        "median_relative_depth": median(relative_depths) if relative_depths else None,
        "p75_relative_depth": percentile(relative_depths, Fraction(3, 4)) if relative_depths else None,
        "tenure": {
            "starts": len(tenure_samples),
            "events": tenure_km["events"],
            "censored": tenure_km["censored"],
            "median_seconds": tenure_km["median_time"],
            "p_undercut_within_1h": tenure_km["probabilities"][3600],
        },
        "median_tenure_seconds": tenure_km["median_time"],
        "p_undercut_within_1h": tenure_km["probabilities"][3600],
        "sellers": {
            "median_groups": median_groups,
            "median_owners_cheapest_5": median_owners,
        },
    }


def _lifetime_sample(
    books: Sequence[Book], intervals: Sequence[Interval | None], start_index: int, group: Group
) -> tuple[float, bool]:
    start_t = books[start_index].t
    for index in range(start_index, len(books) - 1):
        interval = intervals[index]
        if interval is None:
            return books[index].t - start_t, False
        if interval.ceiling is not None and group.unit > interval.ceiling:
            return interval.after.t - start_t, False
        if _find_group(interval.after, group.result_id, interval.ceiling) is None:
            return interval.after.t - start_t, True
    return books[-1].t - start_t, False


def opportunity_metrics(
    books: Sequence[Book],
    fairs: Sequence[dict[str, Any]] | None = None,
    intervals: Sequence[Interval | None] | None = None,
) -> dict[str, Any]:
    """Measure newly arriving groups at fair-relative prices."""

    fair_points = _fair_series(books) if fairs is None else list(fairs)
    valid = valid_intervals(books) if intervals is None else list(intervals)
    covered_seconds = sum(interval.dt for interval in valid if interval is not None)
    eligible_covered_seconds = sum(
        interval.dt
        for index, interval in enumerate(valid)
        if interval is not None
        and index + 1 < len(fair_points)
        and fair_points[index + 1]["value"] is not None
    )
    seen: set[int | str] = set()
    arrivals: dict[Fraction, list[tuple[int, Group]]] = {
        multiplier: [] for multiplier in OPPORTUNITY_MULTIPLIERS
    }
    for index, book in enumerate(books):
        previous_interval = valid[index - 1] if index else None
        previous = books[index - 1] if index else None
        fair = fair_points[index]["value"] if index < len(fair_points) else None
        if previous_interval is not None and previous is not None and fair is not None:
            previous_ids = {group.result_id for group in previous.groups}
            for group in _scoped_groups(book):
                inside_previous_ceiling = previous.ceiling is None or group.unit <= previous.ceiling
                if group.result_id in previous_ids or group.result_id in seen or not inside_previous_ceiling:
                    continue
                for multiplier in OPPORTUNITY_MULTIPLIERS:
                    if group.unit <= multiplier * fair:
                        arrivals[multiplier].append((index, group))
        seen.update(group.result_id for group in book.groups)

    result: dict[str, Any] = {}
    for multiplier, candidates in arrivals.items():
        discount = sum(
            (group.quantity * (fair_points[index]["value"] - group.unit) for index, group in candidates),
            Fraction(0),
        )
        survival_denominator = 0
        survival_numerator = 0
        lifetimes: list[tuple[float, bool]] = []
        for index, group in candidates:
            if index < len(valid) and valid[index] is not None:
                interval = valid[index]
                assert interval is not None
                if interval.ceiling is None or group.unit <= interval.ceiling:
                    survival_denominator += 1
                    if _find_group(interval.after, group.result_id, interval.ceiling) is not None:
                        survival_numerator += 1
            lifetimes.append(_lifetime_sample(books, valid, index, group))
        km = kaplan_meier(lifetimes)
        result[_multiplier_key(multiplier)] = {
            "arrivals": len(candidates),
            "units": sum(group.quantity for _, group in candidates),
            "discount_value": discount,
            "eligible_covered_seconds": eligible_covered_seconds,
            "arrivals_per_day": _rate(len(candidates), eligible_covered_seconds),
            "arrivals_per_day_covered_seconds": eligible_covered_seconds,
            "units_per_day": _rate(sum(group.quantity for _, group in candidates), eligible_covered_seconds),
            "units_per_day_covered_seconds": eligible_covered_seconds,
            "discount_value_per_day": _rate(discount, eligible_covered_seconds),
            "discount_value_per_day_covered_seconds": eligible_covered_seconds,
            "survival": (survival_numerator / survival_denominator if survival_denominator else None),
            "survival_numerator": survival_numerator,
            "survival_denominator": survival_denominator,
            "median_lifetime_seconds": km["median_time"],
            "lifetime_starts": km["starts"],
            "lifetime_events": km["events"],
            "lifetime_censored": km["censored"],
        }
    return {
        "covered_seconds": covered_seconds,
        "eligible_covered_seconds": eligible_covered_seconds,
        "by_multiplier": result,
        **result,
    }


def risk_metrics(
    books: Sequence[Book],
    fairs: Sequence[dict[str, Any]] | None = None,
    intervals: Sequence[Interval | None] | None = None,
) -> dict[str, Any]:
    """Calculate reference volatility and fair-value mean reversion."""

    fair_points = _fair_series(books) if fairs is None else list(fairs)
    valid = valid_intervals(books) if intervals is None else list(intervals)
    scaled_changes: list[float] = []
    regression: list[tuple[float, float, float]] = []
    for index, interval in enumerate(valid):
        if interval is None:
            continue
        before_ref = reference(interval.before)
        after_ref = reference(interval.after)
        if before_ref is None or after_ref is None:
            continue
        log_change = math.log(float(after_ref)) - math.log(float(before_ref))
        scaled_changes.append(log_change / math.sqrt(interval.dt / 3600))
        before_fair = fair_points[index]["value"] if index < len(fair_points) else None
        after_fair = fair_points[index + 1]["value"] if index + 1 < len(fair_points) else None
        if before_fair is not None and after_fair is not None:
            before_dev = math.log(float(before_ref)) - math.log(float(before_fair))
            after_dev = math.log(float(after_ref)) - math.log(float(after_fair))
            regression.append((before_dev, after_dev, interval.dt))
    volatility = stdev(scaled_changes) if len(scaled_changes) > 1 else (0.0 if scaled_changes else None)
    volatility_eligible_seconds = sum(
        interval.dt
        for interval in valid
        if interval is not None
        and reference(interval.before) is not None
        and reference(interval.after) is not None
    )
    if len(regression) > 1:
        x_bar = sum(row[0] for row in regression) / len(regression)
        y_bar = sum(row[1] for row in regression) / len(regression)
        denominator = sum((row[0] - x_bar) ** 2 for row in regression)
        slope = (
            sum((row[0] - x_bar) * (row[1] - y_bar) for row in regression) / denominator
            if denominator
            else None
        )
    else:
        slope = None
    if slope is not None and 0 < slope < 1:
        median_dt_hours = median(row[2] for row in regression) / 3600
        half_life = math.log(0.5) / math.log(slope) * median_dt_hours
    else:
        half_life = None
    return {
        "volatility": volatility,
        "volatility_per_hour": volatility,
        "eligible_covered_seconds": volatility_eligible_seconds,
        "volatility_eligible_covered_seconds": volatility_eligible_seconds,
        "reference_intervals": len(scaled_changes),
        "mean_reversion_b": slope,
        "half_life_hours": half_life,
        "mean_reversion_intervals": len(regression),
    }


def capacity_metrics(
    books: Sequence[Book],
    fairs: Sequence[dict[str, Any]] | None = None,
    intervals: Sequence[Interval | None] | None = None,
) -> dict[str, Any]:
    """Calculate consumed bottom-walk capacity strictly above fair-relative prices."""

    fair_points = _fair_series(books) if fairs is None else list(fairs)
    valid = valid_intervals(books) if intervals is None else list(intervals)
    covered_seconds = sum(interval.dt for interval in valid if interval is not None)
    consumed: dict[Fraction, int] = {multiplier: 0 for multiplier in CAPACITY_MULTIPLIERS}
    eligible_seconds: dict[Fraction, float] = {multiplier: 0 for multiplier in CAPACITY_MULTIPLIERS}
    for index, interval in enumerate(valid):
        if interval is None or index >= len(fair_points) or fair_points[index]["value"] is None:
            continue
        fair = fair_points[index]["value"]
        for multiplier in CAPACITY_MULTIPLIERS:
            threshold = multiplier * fair
            if (
                interval.ceiling is not None and threshold > interval.ceiling
            ) or interval.walk.reached_ceiling:
                continue
            eligible_seconds[multiplier] += interval.dt
            consumed[multiplier] += sum(
                quantity for unit, quantity in interval.walk.consumed if unit > threshold
            )
    by_multiplier = {
        _multiplier_key(multiplier): {
            "units": quantity,
            "units_per_day": _rate(quantity, eligible_seconds[multiplier]),
            "eligible_covered_seconds": eligible_seconds[multiplier],
        }
        for multiplier, quantity in consumed.items()
    }
    return {"covered_seconds": covered_seconds, "by_multiplier": by_multiplier, **by_multiplier}


def compute_metrics(books: dict[Any, Sequence[Book]] | Sequence[Book]) -> dict[Any, dict[str, Any]]:
    """Compute all metrics for each item in a loader result."""

    result: dict[Any, dict[str, Any]] = {}
    for item_id, item_books in _items(books).items():
        fair_points = _fair_series(item_books)
        intervals = valid_intervals(item_books)
        demand = demand_metrics(item_books, intervals)
        competition = competition_metrics(item_books, intervals)
        opportunities = opportunity_metrics(item_books, fair_points, intervals)
        risk = risk_metrics(item_books, fair_points, intervals)
        capacity = capacity_metrics(item_books, fair_points, intervals)
        latest_fair = _fair_at(item_books, len(item_books) - 1) if item_books else fair_value([])
        latest_depth = depth_metrics(item_books[-1], latest_fair["value"]) if item_books else None
        covered_seconds = demand["covered_seconds"]
        result[item_id] = {
            "fair": latest_fair,
            "depth": latest_depth,
            "demand": demand,
            "competition": competition,
            "opportunities": opportunities,
            "risk": risk,
            "capacity": capacity,
            "coverage": {
                "books": len(item_books),
                "valid_intervals": sum(interval is not None for interval in intervals),
                "covered_seconds": covered_seconds,
                "covered_hours": _time_fraction(covered_seconds) / 3600 if covered_seconds else Fraction(0),
            },
        }
    return result


def _copper_sort_value(metric: dict[str, Any]) -> float:
    value = metric["demand"].get("copper_per_day")
    return float(value) if value is not None else float("-inf")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--house", required=True, help="explicit house key")
    parser.add_argument("--out", required=True, help="JSON report path")
    args = parser.parse_args(argv)
    with psycopg.connect(args.database_url) as conn:
        books = load_commodity_books(conn, args.house)
    items = compute_metrics(books)
    report = {"house": args.house, "parameters": PARAMETERS, "items": items}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(jsonable(report), handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(f"items: {len(items)}")
    print("item_id  gold/day")
    ordered = sorted(items.items(), key=lambda pair: (-_copper_sort_value(pair[1]), str(pair[0])))
    for item_id, metric in ordered[:25]:
        copper = metric["demand"].get("copper_per_day")
        gold = "unknown" if copper is None else f"{float(copper) / 10000:.2f}g"
        print(f"{item_id}  {gold}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAPACITY_MULTIPLIERS",
    "DEPTH_QUANTITIES",
    "FAIR_MIN_BOOKS",
    "FAIR_MULTIPLIERS",
    "FAIR_WINDOW_SECONDS",
    "OPPORTUNITY_MULTIPLIERS",
    "PARAMETERS",
    "capacity_metrics",
    "competition_metrics",
    "compute_metrics",
    "demand_metrics",
    "depth_metrics",
    "fair_value",
    "opportunity_metrics",
    "risk_metrics",
    "TrailingDemand",
    "trailing_demand",
    "units_per_hour",
]
