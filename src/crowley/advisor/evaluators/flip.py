"""Flip / relist: buy units listed far below fair value, relist them at a discount to fair.

The method is the beta prototype's, made repeatable.  Per commodity, at one book ``i``:

* fair value is the strictly-past 24 h median of the 20th-unit reference (books before ``t_i``
  only; the book at ``t_i`` is never in its own fair value).  It is trusted only with at least
  ``MIN_FAIR_N`` reference books, a band ``(p75 - p25) / fair <= MAX_BAND_WIDTH`` and at least
  ``MIN_DEMAND_ELIGIBLE_SECONDS`` of strictly-past demand coverage with units/h above zero (the
  ``trustworthy_fair`` thresholds, imported from the one place they are defined).
* buy every unit listed at or below ``flip_x * fair`` (and where the relist still clears cost,
  cut and deposit), cheapest first, capped at ``max(1, floor(share * past_units_per_hour *
  horizon_hours))`` units.
* relist all of it at ``p = floor(fair * (1 - flip_undercut))``.

The live opportunity and every past start are the same decision (``flip_context`` then
``plan_buy``), made from books up to and including ``t_i`` only.

Fill evidence is the rule replayed over every item's history.  A past start is eligible when the
decision above buys something, ``[t, t + horizon]`` is an unbroken run of valid intervals whose
price cutoff covers ``p``, and it is at least one horizon after the item's previous eligible start
(one open position per item).  None of that looks at demand after ``t``.  The outcome is
``time_to_sale.replay_many`` on the books with our bought units removed from their groups, tie
share 0: only demand that walked strictly above ``p`` fills us.

Two biases are left in, both documented rather than hidden:

* Buy side, optimistic: a past start assumes it obtains every unit it decides to buy at ``t``.  A
  real click can lose a race for the cheapest units.  The earlier prototype required the cheap
  group to survive to the next sweep, which peeked at the outcome window; that filter is gone.
* Sale side, pessimistic: an interval whose bottom walk reached the book's price cutoff is kept, but
  sales above the cutoff are not visible, so its fill is a lower bound.

The statistic is the per-trade unit fill (units sold within the horizon over units bought), not a
unit-weighted pool: one big trade must not dominate.  Each item's own mean fill is shrunk toward the
pool, ``(sum of own fills + PRIOR_STRENGTH * pool) / (own trades + PRIOR_STRENGTH)``, a beta-binomial
posterior mean with ``PRIOR_STRENGTH`` pseudo-trades of prior at the pool mean (fills are fractions
in [0, 1] and are treated as fractional successes).  The pool is the mean of the items' mean fills,
each item weighted by ``min(own trades, POOL_ITEM_CAP)``, so no item dominates it.

Expected profit uses the current prices and that shrunk fill; unsold units either stay at cost
(``flip_unsold_value="cost"``, the beta calibration's "carry") or are vendor-sold ("vendor").  A flip
is a one-off opportunity: it has an expected profit now and no per-hour figure.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from math import floor
from statistics import median

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import AdviceConfig, Evidence, Pathway, Step, StrategyResult, pathway_confidence
from crowley.analysis.books import Book, Group, Interval, valid_intervals
from crowley.analysis.common import reference
from crowley.analysis.metrics import FAIR_MIN_BOOKS, FAIR_WINDOW_SECONDS, percentile
from crowley.analysis.time_to_sale import replay_many
from crowley.core.auction_rules import AuctionHouseRules
from crowley.trading.fees import deposit
from crowley.trading.strategies.common import (
    MAX_BAND_WIDTH,
    MIN_DEMAND_ELIGIBLE_SECONDS,
    MIN_FAIR_N,
    SECONDS_PER_HOUR,
)

MIN_BOOKS = 20
PRIOR_STRENGTH = 4  # pseudo-trades of shrinkage toward the pooled fill
POOL_ITEM_CAP = 5  # most trades one item counts for in the pooled fill


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
    band = (percentile(refs, Fraction(1, 4)), percentile(refs, Fraction(3, 4)))
    return percentile(refs, Fraction(1, 2)), band, len(refs)


def flip_context(series: Series, index: int) -> FlipContext | Untrusted:
    fair, band, n = fair_before(series, index)
    if fair is None or band is None or fair <= 0:
        return Untrusted("fair_unavailable", f"{n} strictly-past reference books")
    width = (band[1] - band[0]) / fair
    if n < MIN_FAIR_N or width > MAX_BAND_WIDTH:
        return Untrusted("fair_untrusted", f"fair_n={n} (need {MIN_FAIR_N}), band={float(width):.2f}")
    lo = bisect_left(series.times, series.times[index] - FAIR_WINDOW_SECONDS)
    covered = series.seconds[index] - series.seconds[lo]  # intervals lo..index-1 end at or before t
    units = series.units[index] - series.units[lo]
    if covered < MIN_DEMAND_ELIGIBLE_SECONDS or units <= 0:
        hours = covered / SECONDS_PER_HOUR
        return Untrusted("no_observed_demand", f"{hours:.1f} h covered, {units} units walked")
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
    book: Book,
    ctx: FlipContext,
    config: AdviceConfig,
    sell_price: int,
    rules: AuctionHouseRules,
) -> FlipBuy | None:
    """Units to buy at ``book`` (the book at the decision time), cheapest first, or None."""

    price = floor(ctx.fair * (1 - config.flip_undercut))
    if price <= 0:
        return None
    horizon_hours = Fraction(config.horizon_seconds, SECONDS_PER_HOUR)
    cap = max(1, floor(config.share * ctx.units_per_hour * horizon_hours))
    # Deposit is linear in quantity apart from rounding; one unit's is a safe per-unit bound.
    deposit_unit = Fraction(deposit(sell_price, 1, config.listing_hours, rules))
    limit = min(config.flip_x * ctx.fair, price * (1 - rules.seller_cut) - deposit_unit)
    groups = sorted(
        ((g.unit, g.quantity, g.result_id) for g in book.groups if g.unit < ctx.fair),
        key=lambda group: (group[0], str(group[2])),
    )
    bought: dict[int | str, int] = {}
    quantity = 0
    cost = Fraction(0)
    highest = Fraction(0)
    for unit, units, result_id in groups:
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


def observable(series: Series, index: int, price: int, horizon: int) -> bool:
    """Whether ``[t, t + horizon]`` from book ``index`` is unbroken at ``price``.

    Every interval must be valid (no gap above the chain limit) and its price cutoff, if any, must
    cover ``price``.  Nothing here depends on how much demand there was.
    """

    end_t = series.times[index] + horizon
    end = next((k for k in range(index, len(series.books)) if series.times[k] >= end_t), None)
    if end is None:
        return False
    for interval in series.intervals[index:end]:
        if interval is None:
            return False
        if interval.ceiling is not None and price > interval.ceiling:
            return False
    return True


def _without(series: Series, start: int, bought: dict[int | str, int], horizon: int) -> list[Book]:
    """Books from ``start`` through the horizon with our bought units removed from their groups."""

    end_t = series.times[start] + horizon
    stop = next(
        (k for k in range(start, len(series.books)) if series.times[k] >= end_t), len(series.books) - 1
    )
    result: list[Book] = []
    for book in series.books[start : stop + 1]:
        groups: list[Group] = []
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
    """Past starts the rule would have traded, decided without looking at demand after ``t``.

    A start needs a trusted strictly-past context, units to buy at ``t``, an observable
    ``[t, t + horizon]`` at the relist price and to be at least one horizon after the previous start.
    """

    horizon = config.horizon_seconds
    starts: list[tuple[int, FlipContext, FlipBuy]] = []
    last_t: float | None = None
    for index in range(len(series.books) - 1):
        if series.times[index] < first_t:
            continue
        ctx = flip_context(series, index)
        if isinstance(ctx, Untrusted):
            continue
        buy = plan_buy(series.books[index], ctx, config, sell_price, rules)
        if buy is None or not observable(series, index, buy.price, horizon):
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
            item = series.books[index].item_id
            raise RuntimeError(f"item {item}: observable start {index} has no fill result")
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
    deposit_total = Fraction(deposit(sell_price, quantity, config.listing_hours, rules))
    carry = sold * buy.price * (1 - rules.seller_cut) - sold * average - deposit_total * (1 - fill)
    vendor = carry + min(Fraction(0), sell_price - average) * (quantity - sold)
    return carry, vendor


@dataclass(frozen=True)
class Prepared:
    series: Series
    trades: list[FlipTrade]


@dataclass(frozen=True)
class Pooled:
    """The rule's measured per-trade fill over every item's independent past flips."""

    flips: int
    items: int
    full: int  # trades that sold everything
    partial: int
    zero: int  # trades that sold nothing
    fill: Fraction  # pooled mean of the items' mean fills, each item capped at POOL_ITEM_CAP trades
    median_fill: float  # median per-trade fill, uncapped

    def __post_init__(self) -> None:
        if self.flips != self.full + self.partial + self.zero:
            raise RuntimeError("flip outcome counts do not add up")


def pool(prepared: dict[int, Prepared]) -> Pooled:
    fills = [trade.fill for item in prepared.values() for trade in item.trades]
    weighted = Fraction(0)
    weight = 0
    for item in prepared.values():
        if not item.trades:
            continue
        w = min(len(item.trades), POOL_ITEM_CAP)
        weighted += w * sum((Fraction(t.fill) for t in item.trades), Fraction(0)) / len(item.trades)
        weight += w
    return Pooled(
        flips=len(fills),
        items=sum(bool(item.trades) for item in prepared.values()),
        full=sum(f >= 1 for f in fills),
        partial=sum(0 < f < 1 for f in fills),
        zero=sum(f <= 0 for f in fills),
        fill=weighted / weight if weight else Fraction(0),
        median_fill=float(median(fills)) if fills else 0.0,
    )


def shrunk_fill(trades: list[FlipTrade], prior: Pooled) -> Fraction:
    """The item's own mean fill pulled toward ``prior`` by ``PRIOR_STRENGTH`` pseudo-trades.

    ``prior`` must be the pool of the OTHER items, so the item's own trades are not counted twice.
    With no other item's trades there is no prior at all and the result is the own mean.
    """

    own = sum((Fraction(trade.fill) for trade in trades), Fraction(0))
    if prior.flips == 0:
        if not trades:
            raise ValueError("no trades of the item and none of any other: there is no fill to estimate")
        return own / len(trades)
    return (own + PRIOR_STRENGTH * prior.fill) / (len(trades) + PRIOR_STRENGTH)


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
                f"per-trade unit fill in {config.horizon_hours:g} h: pooled mean {float(pooled.fill):.2f}, "
                f"median {pooled.median_fill:.2f}; {pooled.flips} independent past flips of {pooled.items} "
                f"items: {pooled.full} sold everything, {pooled.partial} partly, {pooled.zero} nothing"
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
            buy = plan_buy(series.books[-1], now, config, fact.sell_price, state.rules)
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
            out.keep(self._pathway(ctx, item_id, item, now, buy, pooled, prepared))
        return out.result()

    def _pathway(
        self,
        ctx: Context,
        item_id: int,
        item: Prepared,
        now: FlipContext,
        buy: FlipBuy,
        pooled: Pooled,
        prepared: dict[int, Prepared],
    ) -> Pathway:
        state, config = ctx.state, ctx.config
        series = item.series
        fact = state.facts[item_id]
        others = pool({other: p for other, p in prepared.items() if other != item_id})
        fill = shrunk_fill(item.trades, others)
        carry, vendor = expected_profit(buy, fill, fact.sell_price, state.rules, config)
        net = carry if config.flip_unsold_value == "cost" else vendor
        own_full = sum(trade.fill >= 1 for trade in item.trades)
        own_zero = sum(trade.fill <= 0 for trade in item.trades)
        own = len(item.trades)
        evidence = Evidence(
            "independent_starts",
            own,
            successes=sum(trade.fill > 0 for trade in item.trades),
            usable=sum(trade.quantity for trade in item.trades),
            detail=(
                f"this item's own past flips ({own_full} sold everything, {own_zero} nothing); "
                "usable is the units those flips bought"
            ),
        )
        demand = Evidence("demand_intervals", now.intervals, detail="usable past intervals behind units/h")
        deposit_total = deposit(fact.sell_price, buy.quantity, config.listing_hours, state.rules)
        listing = f"{config.listing_hours} h listing"
        return Pathway(
            strategy=self.name,
            section="one_off",
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
            profit_per_unit=net / buy.quantity,
            profit_per_craft=None,
            volume_per_hour=now.units_per_hour,
            sale_probability=None,
            fill_rate=float(fill),
            evidence=evidence,
            demand_evidence=demand,
            listed_units=sum(group.quantity for group in series.books[-1].groups),
            capital=buy.cost + deposit_total,
            profit_per_hour=None,
            one_off_profit=net,
            sort_value=net,
            confidence=pathway_confidence(evidence, demand),
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
                "unit_fill_rate": fill,
                "pooled_fill": pooled.fill,
                "prior_fill_excluding_item": others.fill,
                "pooled_flips": pooled.flips,
                "pooled_full_partial_zero": (pooled.full, pooled.partial, pooled.zero),
                "own_past_flips": own,
                "own_full_partial_zero": (
                    own_full,
                    own - own_full - own_zero,
                    own_zero,
                ),
                "fill_shrinkage": (
                    f"{PRIOR_STRENGTH} pseudo-trades toward the pool; "
                    f"pool weight capped at {POOL_ITEM_CAP} per item"
                ),
            },
        )
