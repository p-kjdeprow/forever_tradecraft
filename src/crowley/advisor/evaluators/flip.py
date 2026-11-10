"""Flip / relist: buy units listed far below fair value, relist them at a discount to fair.

The method is the beta prototype's, made repeatable.  Per commodity, at one book ``i``:

* fair value is the strictly-past 24 h median of the 20th-unit reference (books before ``t_i``
  only; the book at ``t_i`` is never in its own fair value).  It is trusted only with at least
  ``MIN_FAIR_N`` reference books, a band ``(p75 - p25) / fair <= MAX_BAND`` and at least
  ``MIN_DEMAND_SECONDS`` of strictly-past demand coverage with units/h above zero (the
  ``trustworthy_fair`` thresholds of the snipe/dip strategies; a test keeps them in step).
* buy every unit listed at or below ``flip_x * fair`` (and where the relist still clears cost,
  cut and deposit), cheapest first, capped at ``max(1, floor(share * past_units_per_hour *
  horizon_hours))`` units.
* relist all of it at ``p = floor(fair * (1 - flip_undercut))``.

Fill evidence is the same rule replayed over every item's history, pooled: per-item histories
are too short (a flip at ``X = 0.7`` is rare), and the rule sizes each position to the item's
flow, so the unit fill rate is comparable across items.  The pooled count, and each item's own
count in the row details, are the evidence; ``flip_min_trades`` is the pooled minimum.  A past
start counts only when units were buyable, ``[t, t + horizon]`` is an unbroken observable run of
books at price ``p`` (decided before, and regardless of, the sale outcome), and it is at least one
horizon after the item's previous counted start (one open position per item).  The outcome is
``time_to_sale.replay_many`` on the books with our bought units removed from their groups, tie
share 0: only demand that walked strictly above ``p`` fills us.  Buy side: a past start may buy
only units whose group still existed at the next sweep, the prototype's stand-in for "still there
when the click lands"; the live recommendation cannot know that, so its units are the current
listings and that risk is not modelled beyond the history's.

The live opportunity is the same decision at the latest book.  Expected profit per unit uses the
current prices with the pooled unit fill rate; unsold units either stay at cost
(``flip_unsold_value="cost"``, the beta calibration's "carry") or are vendor-sold ("vendor").
Profit per hour is that profit per unit times the units/h the rule bought of this item in the
history window (zero when it never triggered there; the one-off is ``expected_profit_now``).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from math import floor

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import AdviceConfig, Evidence, Pathway, Step, StrategyResult, combined_confidence
from crowley.analysis.books import Book, Interval, valid_intervals
from crowley.analysis.common import reference
from crowley.analysis.metrics import FAIR_MIN_BOOKS, FAIR_WINDOW_SECONDS, _percentile
from crowley.analysis.rank import _horizon_observable
from crowley.analysis.time_to_sale import replay_many
from crowley.core.auction_rules import AuctionHouseRules
from crowley.trading.fees import deposit

MIN_FAIR_N = 16
MAX_BAND = Fraction(35, 100)
MIN_DEMAND_SECONDS = 6 * 3600
MIN_BOOKS = 20
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class Series:
    """One item's books with what the decisions need precomputed once."""

    books: tuple[Book, ...]
    intervals: tuple[Interval | None, ...]
    times: tuple[float, ...]
    refs: tuple[Fraction | None, ...]  # 20th-unit reference per book
    units: tuple[int, ...]  # prefix sums of bottom-walk units over valid intervals
    seconds: tuple[float, ...]  # prefix sums of valid interval seconds


def build_series(books: list[Book]) -> Series:
    intervals = valid_intervals(books)
    units = [0]
    seconds = [0.0]
    for interval in intervals:
        units.append(units[-1] + (0 if interval is None else sum(q for _, q in interval.walk.consumed)))
        seconds.append(seconds[-1] + (0.0 if interval is None else interval.dt))
    return Series(
        tuple(books),
        tuple(intervals),
        tuple(book.t for book in books),
        tuple(reference(book) for book in books),
        tuple(units),
        tuple(seconds),
    )


@dataclass(frozen=True)
class FlipContext:
    """The strictly-past facts at book ``i``; nothing here reads book ``i + 1`` or later."""

    index: int
    fair: Fraction
    units_per_hour: Fraction
    covered_seconds: float
    intervals: int  # usable past intervals behind units_per_hour


@dataclass(frozen=True)
class Untrusted:
    reason: str  # a flip exclusion reason
    detail: str


def fair_before(series: Series, index: int) -> tuple[Fraction | None, tuple[Fraction, Fraction] | None, int]:
    """Median, (p25, p75) and count of the references of books in ``[t - 24 h, t)``.

    The same estimate as ``metrics.fair_value(books, index, include_trend=False)`` without
    recomputing every book's reference at every start (a test asserts they agree).
    """

    current = series.times[index]
    lo = bisect_left(series.times, current - FAIR_WINDOW_SECONDS)
    hi = bisect_left(series.times, current)  # strictly before this book
    refs = sorted(ref for ref in series.refs[lo:hi] if ref is not None)
    if len(refs) < FAIR_MIN_BOOKS:
        return None, None, len(refs)
    band = (_percentile(refs, Fraction(1, 4)), _percentile(refs, Fraction(3, 4)))
    return _percentile(refs, Fraction(1, 2)), band, len(refs)


def flip_context(series: Series, index: int) -> FlipContext | Untrusted:
    fair, band, n = fair_before(series, index)
    if fair is None or band is None or fair <= 0:
        return Untrusted("fair_unavailable", f"{n} strictly-past reference books")
    if n < MIN_FAIR_N or (band[1] - band[0]) / fair > MAX_BAND:
        return Untrusted(
            "fair_untrusted", f"fair_n={n} (need {MIN_FAIR_N}), band={float((band[1] - band[0]) / fair):.2f}"
        )
    lo = bisect_left(series.times, series.times[index] - FAIR_WINDOW_SECONDS)
    covered = series.seconds[index] - series.seconds[lo]  # intervals lo..index-1 end at or before t
    units = series.units[index] - series.units[lo]
    if covered < MIN_DEMAND_SECONDS or units <= 0:
        return Untrusted(
            "no_observed_demand", f"{covered / SECONDS_PER_HOUR:.1f} h covered, {units} units walked"
        )
    usable = sum(interval is not None for interval in series.intervals[lo:index])
    return FlipContext(index, fair, Fraction(units) / Fraction(covered) * SECONDS_PER_HOUR, covered, usable)


@dataclass(frozen=True)
class FlipBuy:
    price: int  # the relist price p
    quantity: int
    cost: Fraction
    bought: dict[int | str, int]  # result id -> units bought
    limit_unit: Fraction  # the dearest unit price bought


def plan_buy(
    groups: list[tuple[Fraction, int, int | str]],
    ctx: FlipContext,
    config: AdviceConfig,
    sell_price: int,
    rules: AuctionHouseRules,
) -> FlipBuy | None:
    """Units to buy now from ``groups`` (unit price, units, result id), or None.

    ``groups`` are the buyable groups priced below fair; cheapest first is applied here.
    """

    price = floor(ctx.fair * (1 - config.flip_undercut))
    if price <= 0:
        return None
    cap = max(
        1, floor(config.share * ctx.units_per_hour * Fraction(config.horizon_hours).limit_denominator(1000))
    )
    # Deposit is linear in quantity apart from rounding; one unit's is a safe per-unit bound.
    deposit_unit = Fraction(deposit(sell_price, 1, config.flip_duration_hours, rules))
    limit = min(config.flip_x * ctx.fair, price * (1 - rules.seller_cut) - deposit_unit)
    bought: dict[int | str, int] = {}
    quantity = 0
    cost = Fraction(0)
    highest = Fraction(0)
    for unit, units, result_id in sorted(groups, key=lambda group: (group[0], str(group[2]))):
        if unit > limit or quantity >= cap:
            break
        take = min(units, cap - quantity)
        bought[result_id] = take
        quantity += take
        cost += unit * take
        highest = unit
    if quantity == 0:
        return None
    return FlipBuy(price, quantity, cost, bought, highest)


def current_groups(book: Book, fair: Fraction) -> list[tuple[Fraction, int, int | str]]:
    return [(g.unit, g.quantity, g.result_id) for g in book.groups if g.unit < fair]


def surviving_groups(series: Series, index: int, fair: Fraction) -> list[tuple[Fraction, int, int | str]]:
    """Groups below fair that are still listed at the next book (the buy-side stand-in)."""

    following = {g.result_id: g.quantity for g in series.books[index + 1].groups}
    return [
        (g.unit, min(g.quantity, following[g.result_id]), g.result_id)
        for g in series.books[index].groups
        if following.get(g.result_id, 0) > 0 and g.unit < fair
    ]


def _without(series: Series, start: int, bought: dict[int | str, int], horizon: int) -> list[Book]:
    """Books from ``start`` through the horizon with our bought units removed from their groups."""

    end_t = series.times[start] + horizon
    stop = next(
        (k for k in range(start, len(series.books)) if series.times[k] >= end_t), len(series.books) - 1
    )
    result: list[Book] = []
    for book in series.books[start : stop + 1]:
        groups = []
        for group in book.groups:
            take = bought.get(group.result_id, 0)
            if take:
                left = group.quantity - take
                if left <= 0:
                    continue
                group = type(group)(group.result_id, group.unit, left, group.time_left_seconds, group.owners)
            groups.append(group)
        result.append(Book(book.t, groups, book.ceiling, book.item_id, book.sweep_id))
    return result


@dataclass(frozen=True)
class FlipTrade:
    t: float
    quantity: int
    cost: Fraction
    price: int
    fill: float  # fraction of the units sold within the horizon


def eligible_starts(
    series: Series, config: AdviceConfig, sell_price: int, rules: AuctionHouseRules, first_t: float
) -> list[tuple[int, FlipContext, FlipBuy]]:
    """Past starts the rule would have traded, decided without looking at the sale outcome.

    A start needs a trusted strictly-past context, buyable surviving units, an unbroken observable
    ``[t, t + horizon]`` at the relist price and to be at least one horizon after the previous start.
    """

    horizon = config.horizon_seconds
    starts: list[tuple[int, FlipContext, FlipBuy]] = []
    last_t: float | None = None
    books, intervals = list(series.books), list(series.intervals)
    for index in range(len(series.books) - 1):
        if series.times[index] < first_t or series.intervals[index] is None:
            continue
        ctx = flip_context(series, index)
        if isinstance(ctx, Untrusted):
            continue
        buy = plan_buy(surviving_groups(series, index, ctx.fair), ctx, config, sell_price, rules)
        if buy is None:
            continue
        if not _horizon_observable(books, intervals, index, buy.price, horizon):
            continue
        if last_t is not None and series.times[index] < last_t + horizon:
            continue
        last_t = series.times[index]
        starts.append((index, ctx, buy))
    return starts


def replay_trades(
    series: Series, starts: list[tuple[int, FlipContext, FlipBuy]], config: AdviceConfig
) -> list[FlipTrade]:
    horizon = config.horizon_seconds
    trades: list[FlipTrade] = []
    for index, _, buy in starts:
        books = _without(series, index, buy.bought, horizon)
        fill = replay_many(books, [(0, buy.price)], buy.quantity)[0].checkpoint_fractions[horizon]
        if fill is None:
            raise RuntimeError(
                f"item {series.books[index].item_id}: observable start {index} has no fill result"
            )
        trades.append(FlipTrade(series.times[index], buy.quantity, buy.cost, buy.price, fill))
    return trades


def expected_profit(
    buy: FlipBuy, fill: Fraction, sell_price: int, rules: AuctionHouseRules, config: AdviceConfig
) -> tuple[Fraction, Fraction]:
    """(carry, vendor) expected net of listing ``buy`` at the given unit fill rate.

    Sold units return ``price * (1 - cut)``; the deposit is refunded pro rata on sold units and
    lost on the rest; unsold units are carried at cost, or vendor-sold in the second figure.
    """

    quantity = buy.quantity
    average = buy.cost / quantity
    sold = fill * quantity
    deposit_total = Fraction(deposit(sell_price, quantity, config.flip_duration_hours, rules))
    carry = sold * buy.price * (1 - rules.seller_cut) - sold * average - deposit_total * (1 - fill)
    vendor = carry + min(Fraction(0), sell_price - average) * (quantity - sold)
    return carry, vendor


@dataclass(frozen=True)
class Prepared:
    series: Series
    trades: list[FlipTrade]


@dataclass(frozen=True)
class Pooled:
    """The rule's measured fill over every item's independent past flips."""

    flips: int
    items: int
    units: int
    full: int
    fill: Fraction  # unit fill rate within the horizon


def pool(prepared: dict[int, Prepared]) -> Pooled:
    trades = [trade for item in prepared.values() for trade in item.trades]
    units = sum(trade.quantity for trade in trades)
    filled = sum((Fraction(trade.fill) * trade.quantity for trade in trades), Fraction(0))
    return Pooled(
        flips=len(trades),
        items=sum(bool(item.trades) for item in prepared.values()),
        units=units,
        full=sum(trade.fill >= 1 for trade in trades),
        fill=filled / units if units else Fraction(0),
    )


class FlipEvaluator:
    name = "flip"

    def evaluate(self, ctx: Context) -> StrategyResult:
        state, config = ctx.state, ctx.config
        out = Collector(self.name)
        first_t = state.as_of_t - config.flip_lookback_hours * SECONDS_PER_HOUR
        prepared: dict[int, Prepared] = {}
        for item_id in sorted(state.books):
            books = list(state.books[item_id])
            fact = state.facts.get(item_id)
            if fact is None or len(books) < MIN_BOOKS:
                continue
            series = build_series(books)
            starts = eligible_starts(series, config, fact.sell_price, state.rules, first_t)
            prepared[item_id] = Prepared(series, replay_trades(series, starts, config))
        pooled = pool(prepared)
        if pooled.flips:
            out.notes.append(
                f"fill rate {float(pooled.fill):.2f} of units within {config.horizon_hours:g} h over "
                f"{pooled.flips} independent past flips of {pooled.items} items "
                f"({pooled.full} filled completely)"
            )
        for item_id in sorted(state.books):
            key = f"flip:{item_id}"
            if state.facts.get(item_id) is None:
                out.exclude(key, "no_client_facts", "no client item data (sell price unknown)")
                continue
            if item_id not in prepared:
                out.exclude(key, "too_few_books", f"{len(state.books[item_id])} books (need {MIN_BOOKS})")
                continue
            item = prepared[item_id]
            series = item.series
            if series.books[-1].sweep_id != state.sweep_id:
                out.exclude(key, "no_current_book", "not observed in the latest sweep")
                continue
            now = flip_context(series, len(series.books) - 1)
            if isinstance(now, Untrusted):
                out.exclude(key, now.reason, now.detail)
                continue
            fact = state.facts[item_id]
            buy = plan_buy(
                current_groups(series.books[-1], now.fair), now, config, fact.sell_price, state.rules
            )
            if buy is None:
                out.exclude(
                    key,
                    "no_units_below_threshold",
                    f"nothing listed at or below {float(config.flip_x):.2f} x fair {float(now.fair):.0f}",
                )
                continue
            if pooled.flips < config.flip_min_trades:
                out.exclude(
                    key,
                    "insufficient_independent_evidence",
                    f"{pooled.flips} independent past flips across all items (need {config.flip_min_trades})",
                )
                continue
            out.keep(self._pathway(ctx, item_id, item, now, buy, pooled, first_t))
        return out.result()

    def _pathway(
        self,
        ctx: Context,
        item_id: int,
        item: Prepared,
        now: FlipContext,
        buy: FlipBuy,
        pooled: Pooled,
        first_t: float,
    ) -> Pathway:
        state, config = ctx.state, ctx.config
        series = item.series
        fact = state.facts[item_id]
        carry, vendor = expected_profit(buy, pooled.fill, fact.sell_price, state.rules, config)
        net = carry if config.flip_unsold_value == "cost" else vendor
        own_units = sum(trade.quantity for trade in item.trades)
        lo = bisect_left(series.times, first_t)
        covered_hours = Fraction(round(series.seconds[-1] - series.seconds[lo]), SECONDS_PER_HOUR)
        flipped_per_hour = Fraction(own_units) / covered_hours if covered_hours else Fraction(0)
        per_unit = net / buy.quantity
        evidence = Evidence(
            "independent_starts",
            pooled.flips,
            successes=pooled.full,
            usable=pooled.units,
            detail=(
                f"past flips of {pooled.items} items, one open position per item and horizon; "
                f"{pooled.full} filled completely; unit fill {float(pooled.fill):.2f}"
            ),
        )
        demand = Evidence("demand_intervals", now.intervals, detail="usable past intervals behind units/h")
        deposit_total = deposit(fact.sell_price, buy.quantity, config.flip_duration_hours, state.rules)
        listing = f"{config.flip_duration_hours} h listing"
        return Pathway(
            strategy=self.name,
            key=f"flip:{item_id}",
            title=f"flip {state.name(item_id)}",
            steps=(
                Step(
                    "buy_ah",
                    item_id=item_id,
                    quantity=buy.quantity,
                    price=int(buy.limit_unit),
                    note="cheapest first",
                ),
                Step("list_ah", item_id=item_id, quantity=buy.quantity, price=buy.price, note=listing),
            ),
            profit_per_unit=per_unit,
            profit_per_craft=None,
            volume_per_hour=now.units_per_hour,
            sale_probability=float(pooled.fill),
            evidence=evidence,
            demand_evidence=demand,
            listed_units=sum(group.quantity for group in series.books[-1].groups),
            capital=buy.cost + deposit_total,
            profit_per_hour=per_unit * flipped_per_hour,
            confidence=combined_confidence(evidence, demand),
            details={
                "fair": now.fair,
                "x": config.flip_x,
                "undercut": config.flip_undercut,
                "relist_price": buy.price,
                "average_buy_price": buy.cost / buy.quantity,
                "unsold_value": config.flip_unsold_value,
                "profit_carry": carry / buy.quantity,
                "profit_vendor": vendor / buy.quantity,
                "expected_profit_now": net,
                "unit_fill_rate": pooled.fill,
                "own_past_flips": len(item.trades),
                "own_past_units": own_units,
                "own_full_fills": sum(trade.fill >= 1 for trade in item.trades),
                "units_flipped_per_hour": flipped_per_hour,
                "sale_probability_is": "unit fill within the horizon, pooled over every item's past flips",
            },
        )
