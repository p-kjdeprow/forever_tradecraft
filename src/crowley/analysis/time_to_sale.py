"""Time-to-sale replay, survival estimates, and out-of-sample validation."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import median
from typing import Any

from crowley.trading.numbers import as_fraction

from .books import (
    EXPIRY_SLACK,
    MAX_GAP,
    Book,
    Group,
    Interval,
    common_ceiling,
    valid_intervals,
)

HORIZON = 86400
CHECKPOINTS = (7200, 21600, 43200, 86400)
QUANTITIES = (1, 20, 200)
MULTIPLIERS = (
    ("ref×0.8", Fraction(4, 5)),
    ("ref×0.9", Fraction(9, 10)),
    ("ref×1.0", Fraction(1)),
    ("ref×1.1", Fraction(11, 10)),
    ("ref×1.25", Fraction(5, 4)),
    ("ref×1.5", Fraction(3, 2)),
)
TIE_SHARE = Fraction(0)


@dataclass
class ReplayResult:
    """The result of replaying one hypothetical listing."""

    event_time: float | None
    censor_time: float | None
    checkpoint_fractions: dict[int, float | None]
    filled_fraction_24h: float | None
    observed_to_24h: bool
    filled_units: float = 0.0
    start_index: int | None = None

    @property
    def event(self) -> bool:
        return self.event_time is not None

    @property
    def censored(self) -> bool:
        return self.event_time is None

    @property
    def filled_fraction(self) -> dict[int, float | None]:
        return self.checkpoint_fractions

    @property
    def time(self) -> float | None:
        return self.event_time if self.event_time is not None else self.censor_time

    @property
    def censored_at(self) -> float | None:
        return self.censor_time

    def __getitem__(self, key: str):
        aliases = {
            "event_time": self.event_time,
            "censor_time": self.censor_time,
            "censored_at": self.censor_time,
            "checkpoint_fractions": self.checkpoint_fractions,
            "filled_fraction_24h": self.filled_fraction_24h,
            "observed_to_24h": self.observed_to_24h,
        }
        return aliases[key]


@dataclass
class _PreparedWalk:
    prices: list[Fraction]
    suffix: list[int]
    at_price: dict[Fraction, int]
    reached_ceiling: bool

    def fill_for(self, price: Fraction, tie_share: Fraction) -> Fraction:
        index = bisect_right(self.prices, price)
        above = self.suffix[index]
        at = self.at_price.get(price, 0)
        return Fraction(above) + tie_share * at


@dataclass
class _PriceIndex:
    """Prefix demand for one price, used to keep batch evaluation linear."""

    books: Sequence[Book]
    intervals: Sequence[_PreparedWalk | None]
    price: Fraction
    tie_share: Fraction
    stop_t: float | None
    fills: list[Fraction]
    prefix: list[Fraction]
    ends: list[float]
    breaks: list[str | None]
    next_break: list[int]


def _prepare_walk(interval: Interval) -> _PreparedWalk:
    amounts: dict[Fraction, int] = {}
    for unit, quantity in interval.walk.consumed:
        amounts[unit] = amounts.get(unit, 0) + quantity
    prices = sorted(amounts)
    suffix = [0] * (len(prices) + 1)
    for index in range(len(prices) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + amounts[prices[index]]
    return _PreparedWalk(prices, suffix, amounts, interval.walk.reached_ceiling)


def _prepared_intervals(books: Sequence[Book]) -> list[_PreparedWalk | None]:
    return [None if interval is None else _prepare_walk(interval) for interval in valid_intervals(books)]


def _price_index(
    books: Sequence[Book],
    intervals: Sequence[_PreparedWalk | None],
    price: Fraction,
    tie_share: Fraction,
    stop_t: float | None,
) -> _PriceIndex:
    fills: list[Fraction] = []
    ends: list[float] = []
    breaks: list[str | None] = []
    for index, prepared in enumerate(intervals):
        ends.append(books[index + 1].t)
        if prepared is None:
            fills.append(Fraction(0))
            breaks.append("chain_break")
            continue
        if stop_t is not None and books[index + 1].t > stop_t:
            fills.append(Fraction(0))
            breaks.append("stop")
            continue
        common = common_ceiling(books[index].ceiling, books[index + 1].ceiling)
        if common is not None and (price > common or prepared.reached_ceiling):
            fills.append(Fraction(0))
            breaks.append("unobservable")
            continue
        fills.append(prepared.fill_for(price, tie_share))
        breaks.append(None)
    prefix = [Fraction(0)]
    for fill in fills:
        prefix.append(prefix[-1] + fill)
    next_break = [len(books) - 1] * len(intervals)
    current = len(books) - 1
    for index in range(len(intervals) - 1, -1, -1):
        if breaks[index] is not None:
            current = index
        next_break[index] = current
    return _PriceIndex(books, intervals, price, tie_share, stop_t, fills, prefix, ends, breaks, next_break)


def _fast_replay(index: _PriceIndex, start_index: int, quantity: int) -> ReplayResult:
    fractions: dict[int, float | None] = {point: None for point in CHECKPOINTS}
    books = index.books
    if not books or start_index < 0 or start_index >= len(books):
        return ReplayResult(None, 0.0, fractions, None, False, start_index=start_index)
    if quantity <= 0:
        for point in fractions:
            fractions[point] = 1.0
        return ReplayResult(0.0, None, fractions, 1.0, True, float(quantity), start_index=start_index)
    start_t = books[start_index].t
    if index.stop_t is not None and start_t >= index.stop_t:
        return ReplayResult(None, 0.0, fractions, None, False, start_index=start_index)

    break_index = len(books) - 1 if start_index == len(books) - 1 else index.next_break[start_index]
    horizon_abs = start_t + HORIZON
    usable_end = bisect_right(index.ends, horizon_abs, lo=start_index, hi=break_index)
    if usable_end < break_index:
        observed_until = float(HORIZON)
    elif break_index < len(books) - 1:
        reason = index.breaks[break_index]
        if reason == "stop":
            observed_until = max(0.0, index.stop_t - start_t) if index.stop_t is not None else 0.0
        elif reason == "unobservable":
            observed_until = max(0.0, books[break_index + 1].t - start_t)
        else:
            observed_until = max(0.0, books[break_index].t - start_t)
        observed_until = min(float(HORIZON), observed_until)
    else:
        observed_until = min(float(HORIZON), max(0.0, books[-1].t - start_t))

    target = index.prefix[start_index] + quantity
    event_position = bisect_left(index.prefix, target, lo=start_index + 1, hi=usable_end + 1)
    filled = index.prefix[usable_end] - index.prefix[start_index]
    if event_position <= usable_end:
        event_time = index.ends[event_position - 1] - start_t
        for point in fractions:
            if point < event_time:
                endpoint = bisect_right(index.ends, start_t + point, lo=start_index, hi=event_position)
                fractions[point] = float((index.prefix[endpoint] - index.prefix[start_index]) / quantity)
            else:
                fractions[point] = 1.0
        return ReplayResult(
            event_time,
            None,
            fractions,
            1.0,
            True,
            float(quantity),
            start_index=start_index,
        )

    for point in fractions:
        if point <= observed_until:
            endpoint = bisect_right(index.ends, start_t + point, lo=start_index, hi=usable_end)
            fractions[point] = float((index.prefix[endpoint] - index.prefix[start_index]) / quantity)
    observed = observed_until >= HORIZON
    return ReplayResult(
        None,
        observed_until,
        fractions,
        fractions[HORIZON],
        observed,
        float(filled),
        start_index=start_index,
    )


def _as_tie_share(value: Fraction | int | float | str) -> Fraction:
    result = as_fraction(value)
    if result < 0 or result > 1:
        raise ValueError("tie_share must be between 0 and 1")
    return result


def _filtered_books(books: Sequence[Book], exclude_result_id: int | str | None) -> list[Book]:
    if exclude_result_id is None:
        return list(books)
    return [
        Book(
            t=book.t,
            groups=[group for group in book.groups if group.result_id != exclude_result_id],
            ceiling=book.ceiling,
            item_id=book.item_id,
            sweep_id=book.sweep_id,
        )
        for book in books
    ]


def _put_checkpoint_values(
    fractions: dict[int, float | None], observed_until: float, filled_fraction: float
) -> None:
    for checkpoint in fractions:
        if checkpoint <= observed_until and fractions[checkpoint] is None:
            fractions[checkpoint] = filled_fraction


def _put_checkpoint_before(
    fractions: dict[int, float | None], interval_end: float, filled_fraction: float
) -> None:
    """Attribute a pre-endpoint fill to checkpoints strictly before the endpoint."""

    for checkpoint in fractions:
        if checkpoint < interval_end and fractions[checkpoint] is None:
            fractions[checkpoint] = filled_fraction


def _put_checkpoint_at(
    fractions: dict[int, float | None], interval_end: float, filled_fraction: float
) -> None:
    """Attribute an interval's fill to a checkpoint exactly at its endpoint."""

    for checkpoint in fractions:
        if checkpoint == interval_end and fractions[checkpoint] is None:
            fractions[checkpoint] = filled_fraction


def _replay_prepared(
    books: Sequence[Book],
    intervals: Sequence[_PreparedWalk | None],
    start_index: int,
    price: Fraction,
    quantity: int,
    *,
    tie_share: Fraction,
    stop_t: float | None = None,
    checkpoints: Sequence[int] = CHECKPOINTS,
) -> ReplayResult:
    fractions: dict[int, float | None] = {int(point): None for point in checkpoints}
    if not books or start_index < 0 or start_index >= len(books):
        return ReplayResult(None, 0.0, fractions, None, False, start_index=start_index)
    if quantity <= 0:
        for checkpoint in fractions:
            fractions[checkpoint] = 1.0
        return ReplayResult(0.0, None, fractions, 1.0, True, float(quantity), start_index=start_index)

    start_t = books[start_index].t
    if stop_t is not None and stop_t <= start_t:
        return ReplayResult(None, 0.0, fractions, None, False, start_index=start_index)

    filled = Fraction(0)
    observed_until = 0.0
    n_books = len(books)
    for index in range(start_index, n_books - 1):
        prepared = intervals[index] if index < len(intervals) else None
        before_elapsed = max(0.0, books[index].t - start_t)
        if prepared is None:
            observed_until = min(float(HORIZON), before_elapsed)
            _put_checkpoint_values(fractions, observed_until, float(filled / quantity))
            break

        interval_end = books[index + 1].t
        elapsed = interval_end - start_t
        if stop_t is not None and interval_end > stop_t:
            observed_until = min(float(HORIZON), max(0.0, stop_t - start_t))
            _put_checkpoint_values(fractions, observed_until, float(filled / quantity))
            break
        if elapsed > HORIZON:
            observed_until = float(HORIZON)
            _put_checkpoint_values(fractions, observed_until, float(filled / quantity))
            break
        ceiling = common_ceiling(books[index].ceiling, books[index + 1].ceiling)
        if ceiling is not None and (price > ceiling or prepared.reached_ceiling):
            observed_until = max(0.0, elapsed)
            _put_checkpoint_values(fractions, observed_until, float(filled / quantity))
            break

        _put_checkpoint_before(fractions, elapsed, float(filled / quantity))
        addition = prepared.fill_for(price, tie_share)
        filled += min(Fraction(quantity) - filled, addition)
        observed_until = max(0.0, elapsed)
        if filled >= quantity:
            event_time = observed_until
            for checkpoint in fractions:
                if checkpoint >= event_time:
                    fractions[checkpoint] = 1.0
            _put_checkpoint_values(fractions, observed_until, 1.0)
            return ReplayResult(
                event_time,
                None,
                fractions,
                1.0,
                True,
                float(filled),
                start_index=start_index,
            )
        _put_checkpoint_at(fractions, elapsed, float(filled / quantity))
        if observed_until >= HORIZON:
            break
    else:
        observed_until = min(float(HORIZON), max(0.0, books[-1].t - start_t))
        _put_checkpoint_values(fractions, observed_until, float(filled / quantity))

    observed = observed_until >= HORIZON
    censor_time = min(float(HORIZON), observed_until)
    return ReplayResult(
        None,
        censor_time,
        fractions,
        fractions.get(HORIZON),
        observed,
        float(filled),
        start_index=start_index,
    )


def replay_listing(
    books: Sequence[Book],
    start_index: int | None = None,
    price: Fraction | int | float | str | None = None,
    quantity: int | None = None,
    *,
    k: int | None = None,
    p: Fraction | int | float | str | None = None,
    q: int | None = None,
    tie_share: Fraction | int | float | str = TIE_SHARE,
    exclude_result_id: int | str | None = None,
    stop_t: float | None = None,
    checkpoints: Sequence[int] = CHECKPOINTS,
) -> ReplayResult:
    """Replay a hypothetical listing from ``start_index`` forward."""

    if start_index is None:
        start_index = k
    if start_index is None:
        raise TypeError("replay_listing requires start_index or k")
    if price is None:
        price = p
    if quantity is None:
        quantity = q
    if price is None or quantity is None:
        raise TypeError("replay_listing requires price/p and quantity/q")
    filtered = _filtered_books(books, exclude_result_id)
    prepared = _prepared_intervals(filtered)
    return _replay_prepared(
        filtered,
        prepared,
        start_index,
        as_fraction(price),
        int(quantity),
        tie_share=_as_tie_share(tie_share),
        stop_t=stop_t,
        checkpoints=checkpoints,
    )


def _reference(book: Book) -> Fraction | None:
    groups = sorted(book.groups, key=lambda group: (group.unit, group.result_id))
    seen = 0
    for group in groups:
        seen += group.quantity
        if seen >= 20:
            return group.unit
    return None


def rule_prices(book: Book) -> dict[str, Fraction]:
    """Return the defined posting-rule prices for one book."""

    result: dict[str, Fraction] = {}
    if book.groups:
        minimum = min(group.unit for group in book.groups)
        if minimum >= 2:
            result["undercut"] = minimum - 1
    reference = _reference(book)
    if reference is not None:
        result.update((name, reference * multiplier) for name, multiplier in MULTIPLIERS)
    return result


def _nearest_quantity(quantity: int | float) -> int:
    return min(
        QUANTITIES,
        key=lambda candidate: (abs(math.log(max(quantity, 1)) - math.log(candidate)), candidate),
    )


def _nearest_multiplier(relative: Fraction | float) -> tuple[str, Fraction]:
    value = float(relative)
    return min(MULTIPLIERS, key=lambda pair: (abs(value - float(pair[1])), float(pair[1])))


def _fraction_json(value: Fraction) -> int | str:
    return value.numerator if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _jsonable(value: Any) -> Any:
    """Convert exact analysis values to the stable JSON representation."""

    if isinstance(value, Fraction):
        return _fraction_json(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _item_books(books: dict[Any, Sequence[Book]] | Sequence[Book]) -> dict[Any, list[Book]]:
    if isinstance(books, dict):
        return {item: list(item_books) for item, item_books in books.items()}
    item = books[0].item_id if books else 0
    return {item: list(books)}


def _sample_parts(sample: Any) -> tuple[float, bool]:
    if isinstance(sample, ReplayResult):
        if sample.event_time is not None:
            return float(sample.event_time), True
        return float(sample.censor_time or 0), False
    if isinstance(sample, dict):
        event = sample.get("event_time", sample.get("eventTime"))
        if event is not None:
            return float(event), True
        censor = sample.get("censor_time", sample.get("censorTime", sample.get("time", HORIZON)))
        return float(censor), False
    if isinstance(sample, (tuple, list)) and len(sample) >= 2:
        return float(sample[0]), bool(sample[1])
    raise TypeError("KM samples must be ReplayResult, mapping, or (time, event) pairs")


def kaplan_meier(samples: Iterable[Any], checkpoints: Sequence[int] = CHECKPOINTS) -> dict[str, Any]:
    """Estimate survival with right censoring and return sale probabilities."""

    parts = [_sample_parts(sample) for sample in samples]
    events: dict[float, int] = {}
    censored: dict[float, int] = {}
    for time, is_event in parts:
        target = events if is_event else censored
        target[time] = target.get(time, 0) + 1
    times = sorted(set(events) | set(censored))
    survival = 1.0
    at_risk = len(parts)
    curve: list[dict[str, float]] = []
    checkpoint_probability: dict[int, float] = {}
    checkpoint_index = 0
    median: float | None = None
    for time in times:
        event_count = events.get(time, 0)
        censor_count = censored.get(time, 0)
        while checkpoint_index < len(checkpoints) and checkpoints[checkpoint_index] < time:
            checkpoint_probability[int(checkpoints[checkpoint_index])] = 1.0 - survival
            checkpoint_index += 1
        if event_count and at_risk:
            survival *= 1.0 - event_count / at_risk
            if median is None and survival <= 0.5:
                median = time
        curve.append({"time": time, "survival": survival, "events": event_count, "at_risk": at_risk})
        while checkpoint_index < len(checkpoints) and checkpoints[checkpoint_index] == time:
            checkpoint_probability[int(checkpoints[checkpoint_index])] = 1.0 - survival
            checkpoint_index += 1
        at_risk -= event_count + censor_count
    while checkpoint_index < len(checkpoints):
        checkpoint_probability[int(checkpoints[checkpoint_index])] = 1.0 - survival
        checkpoint_index += 1
    probability_by_checkpoint = {int(point): checkpoint_probability[int(point)] for point in checkpoints}
    return {
        "probabilities": probability_by_checkpoint,
        "probability_by_checkpoint": probability_by_checkpoint,
        "p_full_by_checkpoint": probability_by_checkpoint,
        "sold_by": probability_by_checkpoint,
        "starts": len(parts),
        "events": sum(events.values()),
        "censored": sum(censored.values()),
        "median_time": median,
        "median_time_to_sale": median,
        "curve": curve,
    }


def _metric(results: list[ReplayResult]) -> dict[str, Any]:
    km = kaplan_meier(results)
    observed = [
        result.filled_fraction_24h
        for result in results
        if result.filled_fraction_24h is not None
        and (result.observed_to_24h or (result.event_time is not None and result.event_time <= HORIZON))
    ]
    return {
        **km,
        "mean_filled_fraction_24h": sum(observed) / len(observed) if observed else None,
    }


def evaluate_rules(
    books: dict[Any, Sequence[Book]] | Sequence[Book],
    *,
    stride: int = 1,
    tie_share: Fraction | int | float | str = TIE_SHARE,
    stop_t: float | None = None,
) -> dict[Any, dict[str, dict[int, dict[str, Any]]]]:
    """Evaluate every defined posting rule and requested quantity."""

    if stride < 1:
        raise ValueError("stride must be positive")
    tie = _as_tie_share(tie_share)
    result: dict[Any, dict[str, dict[int, dict[str, Any]]]] = {}
    for item_id, item_books in _item_books(books).items():
        prepared = _prepared_intervals(item_books)
        price_indexes: dict[Fraction, _PriceIndex] = {}
        by_rule: dict[str, dict[int, dict[str, Any]]] = {}
        for index in range(0, len(item_books), stride):
            if stop_t is not None and item_books[index].t >= stop_t:
                continue
            for rule, price in rule_prices(item_books[index]).items():
                quantity_results = by_rule.setdefault(rule, {})
                price_index = price_indexes.get(price)
                if price_index is None:
                    price_index = _price_index(item_books, prepared, price, tie, stop_t)
                    price_indexes[price] = price_index
                for quantity in QUANTITIES:
                    bucket = quantity_results.setdefault(quantity, {"_results": []})
                    replay = _fast_replay(price_index, index, quantity)
                    bucket["_results"].append(replay)
        for rule, quantities in by_rule.items():
            for quantity, bucket in list(quantities.items()):
                results = bucket.pop("_results")
                quantities[quantity] = _metric(results)
                quantities[quantity]["price_rule"] = rule
                quantities[quantity]["quantity"] = quantity
        result[item_id] = by_rule
    return result


@dataclass
class RealListing:
    item_id: Any
    start_index: int
    result_id: int | str
    price: Fraction
    quantity: int
    start_t: float
    event_time: float | None
    censor_time: float | None
    checkpoint_fractions: dict[int, float | None]
    observed_until: float
    rule_bucket: str | None
    relative_price: Fraction | None
    quantity_bucket: int
    censor_reason: str | None = None

    @property
    def event(self) -> bool:
        return self.event_time is not None

    def known_by(self, horizon: int) -> bool:
        return self.event_time is not None and self.event_time <= horizon or self.observed_until >= horizon

    def outcome_by(self, horizon: int) -> int | None:
        if not self.known_by(horizon):
            return None
        return int(self.event_time is not None and self.event_time <= horizon)


def _track_real_listing(
    item_id: Any,
    books: Sequence[Book],
    intervals: Sequence[Interval | None],
    start_index: int,
    group: Group,
) -> RealListing:
    start_t = books[start_index].t
    initial_quantity = group.quantity
    filled = 0
    fractions: dict[int, float | None] = {point: None for point in CHECKPOINTS}
    observed_until = 0.0
    event_time: float | None = None
    censor_time: float | None = None
    censor_reason: str | None = None
    for index in range(start_index, len(books) - 1):
        interval = intervals[index] if index < len(intervals) else None
        before_elapsed = max(0.0, books[index].t - start_t)
        if interval is None:
            observed_until = min(float(HORIZON), before_elapsed)
            _put_checkpoint_values(fractions, observed_until, min(1.0, filled / initial_quantity))
            censor_time = observed_until
            censor_reason = "chain_break"
            break
        elapsed = interval.after.t - start_t
        if elapsed > HORIZON:
            observed_until = float(HORIZON)
            _put_checkpoint_values(fractions, observed_until, min(1.0, filled / initial_quantity))
            censor_time = observed_until
            censor_reason = "horizon"
            break
        common = interval.ceiling
        if common is not None and group.unit > common:
            observed_until = min(float(HORIZON), elapsed)
            _put_checkpoint_values(fractions, observed_until, min(1.0, filled / initial_quantity))
            censor_time = observed_until
            censor_reason = "unobservable_ceiling"
            break

        before_group = next(
            (candidate for candidate in interval.before.groups if candidate.result_id == group.result_id),
            None,
        )
        next_group = next(
            (candidate for candidate in interval.after.groups if candidate.result_id == group.result_id), None
        )
        if before_group is None:
            observed_until = min(float(HORIZON), elapsed)
            _put_checkpoint_values(fractions, observed_until, min(1.0, filled / initial_quantity))
            censor_time = observed_until
            censor_reason = "cancellation_or_expiry"
            break
        before_quantity = before_group.quantity
        after_quantity = next_group.quantity if next_group is not None else 0
        removed = max(before_quantity - after_quantity, 0)
        consumed = next(
            (
                amount
                for result_id, unit, amount in interval.walk.consumed_groups
                if result_id == group.result_id
            ),
            0,
        )

        fraction_before = min(1.0, filled / initial_quantity)
        _put_checkpoint_before(fractions, elapsed, fraction_before)
        if removed > consumed:
            observed_until = min(float(HORIZON), elapsed)
            _put_checkpoint_at(fractions, elapsed, fraction_before)
            censor_time = observed_until
            censor_reason = "cancellation_or_expiry"
            break

        filled += min(removed, consumed)
        fraction_after = min(1.0, filled / initial_quantity)
        if after_quantity == 0 and before_quantity > 0 and consumed == removed == before_quantity:
            event_time = elapsed
            observed_until = min(float(HORIZON), elapsed)
            for point in fractions:
                if point >= elapsed:
                    fractions[point] = 1.0
            break
        observed_until = min(float(HORIZON), max(0.0, elapsed))
        _put_checkpoint_at(fractions, elapsed, fraction_after)
        if observed_until >= HORIZON:
            break
    else:
        observed_until = min(float(HORIZON), max(0.0, books[-1].t - start_t))
        _put_checkpoint_values(fractions, observed_until, min(1.0, filled / initial_quantity))
        if observed_until < HORIZON:
            censor_time = observed_until
            censor_reason = "data_end"

    if event_time is not None:
        observed_until = max(observed_until, min(float(HORIZON), event_time))
    else:
        if censor_time is None:
            censor_time = observed_until
            censor_reason = censor_reason or ("horizon" if observed_until >= HORIZON else "data_end")
    reference = _reference(books[start_index])
    minimum_previous = None
    if start_index:
        previous_prices = [candidate.unit for candidate in books[start_index - 1].groups]
        minimum_previous = min(previous_prices) if previous_prices else None
    undercut = minimum_previous is not None and group.unit < minimum_previous
    relative = None if reference is None else group.unit / reference
    rule_bucket: str | None
    if undercut:
        rule_bucket = "undercut"
    elif relative is None:
        rule_bucket = None
    else:
        rule_bucket = _nearest_multiplier(relative)[0]
    return RealListing(
        item_id=item_id,
        start_index=start_index,
        result_id=group.result_id,
        price=group.unit,
        quantity=group.quantity,
        start_t=start_t,
        event_time=event_time,
        censor_time=censor_time,
        checkpoint_fractions=fractions,
        observed_until=observed_until,
        rule_bucket=rule_bucket,
        relative_price=relative,
        quantity_bucket=_nearest_quantity(group.quantity),
        censor_reason=censor_reason,
    )


def real_listings(books: dict[Any, Sequence[Book]] | Sequence[Book]) -> list[RealListing]:
    """Extract and track genuinely new commodity price-level groups."""

    listings: list[RealListing] = []
    for item_id, item_books in _item_books(books).items():
        intervals = valid_intervals(item_books)
        seen: set[int | str] = set()
        for index, book in enumerate(item_books):
            current_ids = {group.result_id for group in book.groups}
            if index and intervals[index - 1] is not None:
                previous = item_books[index - 1]
                previous_ids = {group.result_id for group in previous.groups}
                for group in book.groups:
                    within_previous_ceiling = previous.ceiling is None or group.unit <= previous.ceiling
                    if (
                        group.result_id not in previous_ids
                        and group.result_id not in seen
                        and within_previous_ceiling
                    ):
                        listings.append(_track_real_listing(item_id, item_books, intervals, index, group))
            seen.update(current_ids)
    return listings


def _calibration(predictions: list[tuple[float, int]]) -> list[dict[str, float | int]]:
    if not predictions:
        return []
    ordered = sorted(predictions, key=lambda pair: pair[0])
    bin_count = min(10, len(ordered))
    table: list[dict[str, float | int]] = []
    for index in range(bin_count):
        start = index * len(ordered) // bin_count
        end = (index + 1) * len(ordered) // bin_count
        values = ordered[start:end]
        table.append(
            {
                "count": len(values),
                "mean_predicted": sum(value for value, _ in values) / len(values),
                "observed_frequency": sum(outcome for _, outcome in values) / len(values),
            }
        )
    return table


def _validation_horizon(
    listings: Iterable[RealListing],
    horizon: int,
    prediction: dict[int, tuple[float | None, float]],
    *,
    cancellation_unsold: bool = False,
) -> dict[str, Any]:
    rows = []
    unscorable_model_bucket = 0
    for listing in listings:
        outcome = listing.outcome_by(horizon)
        if (
            outcome is None
            and cancellation_unsold
            and listing.censor_reason == "cancellation_or_expiry"
            and listing.censor_time is not None
            and listing.censor_time < horizon
        ):
            outcome = 0
        if outcome is None:
            continue
        model, baseline = prediction[id(listing)]
        if model is None:
            unscorable_model_bucket += 1
            continue
        rows.append((model, baseline, outcome, listing))
    model_brier = sum((model - outcome) ** 2 for model, _, outcome, _ in rows) / len(rows) if rows else None
    baseline_brier = (
        sum((baseline - outcome) ** 2 for _, baseline, outcome, _ in rows) / len(rows) if rows else None
    )
    buckets: dict[str, int] = {}
    for _, _, _, listing in rows:
        bucket = f"{listing.rule_bucket or 'undefined'}:q={listing.quantity_bucket}"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    known_sold = sum(outcome for _, _, outcome, _ in rows)
    known_unsold = len(rows) - known_sold
    return {
        "count": len(rows),
        "known_sold": known_sold,
        "known_unsold": known_unsold,
        "brier_model": model_brier,
        "brier_baseline": baseline_brier,
        "calibration": _calibration([(model, outcome) for model, _, outcome, _ in rows]),
        "per_bucket_counts": buckets,
        "unscorable_model_bucket": unscorable_model_bucket,
        "single_class": known_sold == 0 or known_unsold == 0,
    }


def _validation_report(
    listings: Iterable[RealListing],
    horizon: int,
    prediction: dict[int, tuple[float | None, float]],
) -> dict[str, Any]:
    listing_rows = list(listings)
    cancellation_censored = _validation_horizon(listing_rows, horizon, prediction)
    cancellation_unsold = _validation_horizon(listing_rows, horizon, prediction, cancellation_unsold=True)
    return {
        **cancellation_censored,
        "cancellation_censored": cancellation_censored,
        "cancellation_unsold": cancellation_unsold,
    }


def _test_outcomes(listings: Iterable[RealListing]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for listing in listings:
        outcome = "event" if listing.event_time is not None else listing.censor_reason or "unknown"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _km_probability(km: dict[str, Any], horizon: int) -> float:
    probabilities = km["probabilities"]
    if horizon not in probabilities:
        raise KeyError(f"KM result is missing checkpoint {horizon}")
    return float(probabilities[horizon])


def validate(
    books: dict[Any, Sequence[Book]] | Sequence[Book],
    *,
    split_t: float | None = None,
    stride: int = 1,
    tie_share: Fraction | int | float | str = TIE_SHARE,
) -> dict[str, Any]:
    """Train truncated replay KMs and score them on later real listings."""

    if stride < 1:
        raise ValueError("stride must be positive")
    items = _item_books(books)
    all_valid_times = [
        interval.before.t
        for item_books in items.values()
        for interval in valid_intervals(item_books)
        if interval is not None
    ]
    if split_t is None:
        split_t = float(median(all_valid_times)) if all_valid_times else None
    listings = real_listings(items)
    if split_t is None:
        return {
            "split_t": None,
            "training_starts": 0,
            "test_listings": 0,
            "test_outcomes": {},
            "2h": _validation_report([], 7200, {}),
            "6h": _validation_report([], 21600, {}),
            "24h": _validation_report([], 86400, {}),
            "per_bucket_counts": {},
        }

    tie = _as_tie_share(tie_share)
    bucket_samples: dict[tuple[Any, str, int], list[ReplayResult]] = {}
    item_quantity_samples: dict[tuple[Any, int], list[ReplayResult]] = {}
    overall_quantity_samples: dict[int, list[ReplayResult]] = {}
    training_starts = 0
    for item_id, item_books in items.items():
        prepared = _prepared_intervals(item_books)
        price_indexes: dict[Fraction, _PriceIndex] = {}
        for index in range(0, len(item_books), stride):
            if item_books[index].t >= split_t:
                continue
            rules = rule_prices(item_books[index])
            for rule, price in rules.items():
                price_index = price_indexes.get(price)
                if price_index is None:
                    price_index = _price_index(item_books, prepared, price, tie, split_t)
                    price_indexes[price] = price_index
                for quantity in QUANTITIES:
                    replay = _fast_replay(price_index, index, quantity)
                    bucket = (item_id, rule, quantity)
                    bucket_samples.setdefault(bucket, []).append(replay)
                    item_quantity_samples.setdefault((item_id, quantity), []).append(replay)
                    overall_quantity_samples.setdefault(quantity, []).append(replay)
                    training_starts += 1

    bucket_km = {key: kaplan_meier(value) for key, value in bucket_samples.items()}
    item_quantity_km = {key: kaplan_meier(value) for key, value in item_quantity_samples.items()}
    overall_quantity_km = {key: kaplan_meier(value) for key, value in overall_quantity_samples.items()}
    empty_km = kaplan_meier([])
    test = [listing for listing in listings if listing.start_t > split_t]
    predictions: dict[int, dict[int, tuple[float | None, float]]] = {}
    for listing in test:
        bucket_key = (listing.item_id, listing.rule_bucket, listing.quantity_bucket)
        model_km = bucket_km.get(bucket_key)
        baseline_km = item_quantity_km.get((listing.item_id, listing.quantity_bucket))
        if baseline_km is None:
            baseline_km = overall_quantity_km.get(listing.quantity_bucket, empty_km)
        predictions[id(listing)] = {
            horizon: (
                None if model_km is None else _km_probability(model_km, horizon),
                _km_probability(baseline_km, horizon),
            )
            for horizon in (7200, 21600, 86400)
        }

    prediction_2 = {key: value[7200] for key, value in predictions.items()}
    prediction_6 = {key: value[21600] for key, value in predictions.items()}
    prediction_24 = {key: value[86400] for key, value in predictions.items()}
    report_2 = _validation_report(test, 7200, prediction_2)
    report_6 = _validation_report(test, 21600, prediction_6)
    report_24 = _validation_report(test, 86400, prediction_24)
    return {
        "split_t": split_t,
        "training_starts": training_starts,
        "test_listings": len(test),
        "test_outcomes": _test_outcomes(test),
        "2h": report_2,
        "6h": report_6,
        "24h": report_24,
        "per_bucket_counts": report_24["per_bucket_counts"],
    }


__all__ = [
    "CHECKPOINTS",
    "HORIZON",
    "MAX_GAP",
    "EXPIRY_SLACK",
    "MULTIPLIERS",
    "QUANTITIES",
    "RealListing",
    "ReplayResult",
    "evaluate_rules",
    "kaplan_meier",
    "real_listings",
    "replay_listing",
    "rule_prices",
    "validate",
    "_jsonable",
    "_reference",
]
