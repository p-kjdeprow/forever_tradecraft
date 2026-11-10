"""Raw materials: what gatherable trade goods sell for and how much value flows through them.

Population: the active ``trade_goods`` items of the scan plan whose catalog tags mark them as
gatherable (``state.GATHERABLE_TAGS``: herbs, leather and hides, cloth, ore and bars, stone, ...).
For each one the pathway is "gather it, list it at 1c under the current lowest listing":

* profit per unit is that price net of the AH cut and the deposit (a stack of up to 20), with no
  material cost; the character's time is the real cost and is not priced.
* volume is the observed bottom-walk units/h; ``value_per_hour_flowing`` (units/h at the current
  lowest price) is the whole market's money flow through the item, the number that says how much
  farming it can absorb.  Profit per hour takes the configured share of the flow.
* P(sale) is not measured, so confidence rests on the number of usable demand intervals.
"""

from __future__ import annotations

from fractions import Fraction

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.model import Evidence, Pathway, Step, StrategyResult, combined_confidence
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
            if est.lowest is None or est.lowest <= 1:
                out.exclude(key, "no_current_listing", "nothing listed to undercut")
                continue
            if est.units_per_hour is None or est.units_per_hour <= 0:
                out.exclude(
                    key, "no_observed_demand", f"{est.covered_hours:.1f} h covered, no bottom-walk demand"
                )
                continue
            quantity = min(STACK, fact.max_stack)
            price = est.lowest - 1
            deposit_total = deposit(fact.sell_price, quantity, 12, state.rules)
            per_unit = price * (1 - state.rules.seller_cut) - Fraction(deposit_total, quantity)
            demand = Evidence("demand_intervals", est.demand_intervals, detail="usable book intervals")
            out.keep(
                Pathway(
                    strategy=self.name,
                    key=key,
                    title=f"farm {state.name(item_id)} ({'/'.join(state.gatherable[item_id])})",
                    steps=(
                        Step("gather", item_id=item_id, quantity=quantity, note="farm it"),
                        Step("list_ah", item_id=item_id, quantity=quantity, price=price, note="12 h listing"),
                    ),
                    profit_per_unit=per_unit,
                    profit_per_craft=None,
                    volume_per_hour=est.units_per_hour,
                    sale_probability=None,
                    evidence=None,
                    demand_evidence=demand,
                    listed_units=est.listed_units,
                    capital=Fraction(deposit_total),
                    profit_per_hour=config.share * est.units_per_hour * per_unit,
                    confidence=combined_confidence(demand, cap="medium"),
                    details={
                        "current_lowest": est.lowest,
                        "fair": est.fair,
                        "value_per_hour_flowing": est.units_per_hour * est.lowest,
                        "tags": state.gatherable[item_id],
                        "note": "no material cost; the character's time is not priced",
                    },
                )
            )
        return out.result()
