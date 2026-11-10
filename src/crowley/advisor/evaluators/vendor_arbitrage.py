"""Vendor arbitrage in both directions, one pathway per item and direction.

* ``sell``: listings priced below the item's vendor sell price.  Buy them, vendor-sell them.  This is
  ``VendorArbStrategy``'s rule (a level strictly below the vendor price is a buy); a vendor sale
  carries no AH cut, so the margin is ``sell_price - unit_price``.  The units listed now are a
  one-off (section ``one_off``, no per-hour figure); the history's arrival rate of such listings
  (new groups below the vendor price, summed over valid intervals) is shown as its volume.
* ``buy``: items a vendor sells (client facts: bundle size and price) that list on the AH above the
  vendor price.  Buy bundles, list 1c under the lowest listing; the AH cut and the deposit come off,
  and the rate is the observed bottom-walk units/h at the configured share.
"""

from __future__ import annotations

from fractions import Fraction
from math import floor

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import Evidence, Pathway, Step, StrategyResult, pathway_confidence
from crowley.analysis.books import MAX_GAP, Book
from crowley.trading.fees import deposit
from crowley.trading.strategies.common import SECONDS_PER_HOUR


def arrivals_below(books: list[Book], limit: int, first_t: float) -> tuple[int, int, float]:
    """(new groups, units in them, covered hours) of listings priced below ``limit`` that appeared.

    A group counts when its result id was absent from the previous book of a valid interval
    (0 < gap <= MAX_GAP) starting at or after ``first_t``.
    """

    groups = units = 0
    covered = 0.0
    for before, after in zip(books, books[1:], strict=False):
        gap = after.t - before.t
        if before.t < first_t or not 0 < gap <= MAX_GAP:
            continue
        covered += gap
        seen = {group.result_id for group in before.groups}
        for group in after.groups:
            if group.result_id not in seen and group.unit < limit:
                groups += 1
                units += group.quantity
    return groups, units, covered / SECONDS_PER_HOUR


class VendorArbitrageEvaluator:
    name = "vendor_arbitrage"

    def evaluate(self, ctx: Context) -> StrategyResult:
        out = Collector(self.name)
        self._sell(ctx, out)
        self._buy(ctx, out)
        return out.result()

    def _sell(self, ctx: Context, out: Collector) -> None:
        state, config = ctx.state, ctx.config
        first_t = state.as_of_t - config.lookback_hours * SECONDS_PER_HOUR
        for item_id in sorted(state.books):
            key = f"vendor_arbitrage:sell:{item_id}"
            fact = state.facts.get(item_id)
            if fact is None:
                out.exclude(key, "no_vendor_value", "no client item data (vendor sell price unknown)")
                continue
            if fact.sell_price <= 0:
                out.exclude(key, "no_vendor_value", "vendor sell price is zero")
                continue
            books = list(state.books[item_id])
            latest = books[-1]
            if latest.sweep_id != state.sweep_id:
                out.exclude(key, "no_current_book", "not observed in the latest sweep")
                continue
            cheap = sorted((g.unit, g.quantity) for g in latest.groups if g.unit < fact.sell_price)
            if not cheap:
                out.exclude(key, "no_opportunity", f"nothing listed below the vendor price {fact.sell_price}")
                continue
            quantity = sum(units for _, units in cheap)
            cost = sum((unit * units for unit, units in cheap), Fraction(0))
            per_unit = (fact.sell_price * quantity - cost) / quantity
            groups, units, hours = arrivals_below(books, fact.sell_price, first_t)
            rate = Fraction(units) / Fraction(hours).limit_denominator(1000) if hours else Fraction(0)
            evidence = Evidence(
                "arrival_groups", groups, detail=f"cheap listings seen appearing in {hours:.1f} h"
            )
            out.keep(
                Pathway(
                    strategy=self.name,
                    section="one_off",
                    key=key,
                    title=f"buy {state.name(item_id)} below vendor price, vendor-sell",
                    steps=(
                        Step("buy_ah", item_id=item_id, quantity=quantity, price=int(cheap[-1][0])),
                        Step("sell_vendor", item_id=item_id, quantity=quantity, price=fact.sell_price),
                    ),
                    profit_per_unit=per_unit,
                    profit_per_craft=None,
                    volume_per_hour=rate,
                    sale_probability=None,
                    fill_rate=None,
                    evidence=None,
                    demand_evidence=evidence,
                    listed_units=quantity,
                    capital=cost,
                    profit_per_hour=None,
                    one_off_profit=per_unit * quantity,
                    sort_value=per_unit * quantity,
                    confidence=pathway_confidence(None, evidence),
                    details={
                        "direction": "ah_to_vendor",
                        "vendor_sell_price": fact.sell_price,
                        "one_off_profit": per_unit * quantity,
                        "arrival_units_per_hour": rate,
                        "note": "a vendor sale has no AH cut; volume is the arrival rate of cheap listings",
                    },
                )
            )

    def _buy(self, ctx: Context, out: Collector) -> None:
        state, config = ctx.state, ctx.config
        for item_id, fact in sorted(state.vendor_supplies.items()):
            key = f"vendor_arbitrage:buy:{item_id}"
            if item_id not in state.books:
                out.exclude(key, "no_current_book", "vendor supply never observed as a commodity")
                continue
            est = ctx.estimates.estimate(item_id)
            if est is None:
                out.exclude(key, "no_current_book", "not observed in the latest sweep")
                continue
            if est.lowest is None or est.lowest <= 1:
                out.exclude(key, "no_opportunity", "nothing listed to undercut")
                continue
            if est.units_per_hour is None or est.units_per_hour <= 0:
                out.exclude(
                    key, "no_observed_demand", f"{est.covered_hours:.1f} h covered, no bottom-walk demand"
                )
                continue
            if fact.vendor_bundle_size is None or fact.vendor_bundle_price is None:
                raise RuntimeError(f"vendor supply {item_id} has no bundle size or price")
            unit_cost = Fraction(fact.vendor_bundle_price, fact.vendor_bundle_size)
            price = est.lowest - 1
            quantity = max(
                1,
                floor(
                    config.share * est.units_per_hour * Fraction(config.horizon_hours).limit_denominator(1000)
                ),
            )
            deposit_total = deposit(fact.sell_price, quantity, config.listing_hours, state.rules)
            per_unit = price * (1 - state.rules.seller_cut) - Fraction(deposit_total, quantity) - unit_cost
            if per_unit <= 0:
                out.exclude(key, "non_positive_profit", f"vendor {float(unit_cost):.0f}c vs AH net {price}c")
                continue
            demand = Evidence("demand_intervals", est.demand_intervals, detail="usable book intervals")
            out.keep(
                Pathway(
                    strategy=self.name,
                    section="unmeasured",
                    key=key,
                    title=f"buy {state.name(item_id)} from vendor, list on the AH",
                    steps=(
                        Step(
                            "buy_vendor",
                            item_id=item_id,
                            quantity=quantity,
                            price=-(-fact.vendor_bundle_price // fact.vendor_bundle_size),
                            note=f"bundles of {fact.vendor_bundle_size}",
                        ),
                        Step(
                            "list_ah",
                            item_id=item_id,
                            quantity=quantity,
                            price=price,
                            note=f"{config.listing_hours} h listing",
                        ),
                    ),
                    profit_per_unit=per_unit,
                    profit_per_craft=None,
                    volume_per_hour=est.units_per_hour,
                    sale_probability=None,
                    fill_rate=None,
                    evidence=None,
                    demand_evidence=demand,
                    listed_units=est.listed_units,
                    capital=unit_cost * quantity + deposit_total,
                    profit_per_hour=config.share * est.units_per_hour * per_unit,
                    one_off_profit=None,
                    sort_value=config.share * est.units_per_hour * per_unit,
                    confidence=pathway_confidence(None, demand),
                    details={
                        "direction": "vendor_to_ah",
                        "vendor_unit_cost": unit_cost,
                        "current_lowest": est.lowest,
                    },
                )
            )
