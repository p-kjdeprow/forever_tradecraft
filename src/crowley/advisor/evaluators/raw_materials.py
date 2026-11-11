"""Raw materials: what gatherable trade goods sell for and how much value flows through them.

Population: the active ``trade_goods`` items of the scan plan whose catalog tags mark them as
gatherable (``state.GATHERABLE_TAGS``: herbs, leather and hides, cloth, ore, stone, fish, ...; bars are
smelted, not gathered, so they are not in it).
For each one the pathway is "gather it, list it at 1c under the forecast-selected sale price"
(or the current lowest in snapshot mode):

* profit per unit is that price net of the AH cut and the deposit (a stack of up to 20), with no
  material cost; the character's time is the real cost and is not priced.
* volume is the observed or forecast-selected demand units/h; ``value_per_hour_flowing`` uses the
  expected sale price and is the whole market's money flow through the item.  It is market
  capacity, not the character's income: it says how much farming the item can absorb.  The pathway
  sits in the ``farming`` section, ordered by it, and has no per-hour profit.
* P(sale) is not measured, so confidence is at most "low" (the common rule).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import Evidence, Pathway, Step, StrategyResult, pathway_confidence
from crowley.trading.fees import deposit

STACK = 20


class RawMaterialsEvaluator:
    name = "raw_materials"

    def evaluate(self, ctx: Context) -> StrategyResult:
        state, config = ctx.state, ctx.config
        out = Collector(self.name)
        for item_id in sorted(state.gatherable):
            key = f"raw_materials:{item_id}"
            if item_id not in state.books:
                out.exclude(
                    key, "no_commodity_book", "never observed as a commodity for this house and build"
                )
                continue
            fact = state.facts.get(item_id)
            if fact is None:
                out.exclude(key, "no_client_facts", "no client item data (sell price unknown)")
                continue
            est = ctx.estimates.estimate(item_id)
            if est is None:
                out.exclude(key, "no_current_book", "not observed in the latest sweep")
                continue
            sale_price = est.sale_price
            if sale_price is None or sale_price <= 1:
                out.exclude(key, "no_current_listing", "nothing listed to undercut")
                continue
            if est.units_per_hour is None or est.units_per_hour <= 0:
                out.exclude(
                    key, "no_observed_demand", f"{est.covered_hours:.1f} h covered, no bottom-walk demand"
                )
                continue
            quantity = min(STACK, fact.max_stack)
            price = sale_price - 1
            deposit_total = deposit(fact.sell_price, quantity, config.listing_hours, state.rules)
            per_unit = price * (1 - state.rules.seller_cut) - Fraction(deposit_total, quantity)
            demand = Evidence("demand_intervals", est.demand_intervals, detail="usable book intervals")
            details: dict[str, Any] = {
                "current_lowest": est.lowest,
                "fair": est.fair,
                "value_per_hour_flowing": est.units_per_hour * sale_price,
                "tags": state.gatherable[item_id],
                "note": "no material cost; the character's time is not priced",
            }
            if est.forecast is not None:
                details.update({"sale_price": {item_id: est.sale_price}, "forecast": {item_id: est.forecast}})
            out.keep(
                Pathway(
                    strategy=self.name,
                    section="farming",
                    key=key,
                    title=f"farm {state.name(item_id)} ({'/'.join(state.gatherable[item_id])})",
                    steps=(
                        Step("gather", item_id=item_id, quantity=quantity, note="farm it"),
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
                    capital=Fraction(deposit_total),
                    profit_per_hour=None,
                    one_off_profit=None,
                    sort_value=est.units_per_hour * sale_price,
                    confidence=pathway_confidence(None, demand),
                    details=details,
                )
            )
        return out.result()
