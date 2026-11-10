"""Recipe purchases: buy a teaching item, learn the recipe, craft it.

Population: every recipe rank priced (a ``TradeRow``).  Recipes rank excluded have no margin to
pay a recipe back with and are counted under the crafting strategy; this strategy notes how many.
It needs a character (``--character``): without a capture nothing says which recipes are known,
so every row is excluded as ``no_character``.

Payback is the recipe's cheapest current teaching-item listing divided by the headline margin per
craft (in crafts) and by the P(sale)-weighted crafts per hour at the configured share (in hours).  Rank's own
payback at its target price is kept in the details.  ``profit_per_hour`` is the income the recipe
unlocks, so the row sorts next to the crafting row it would become.
"""

from __future__ import annotations

from fractions import Fraction

from crowley.advisor.evaluators.base import Collector, Context
from crowley.advisor.evaluators.crafting import (
    analyze,
    headline_margin,
    row_pathway,
)
from crowley.advisor.model import Pathway, Step, StrategyResult
from crowley.analysis.rank import TradeRow


def _payback(price: int, margin: Fraction, row: TradeRow) -> tuple[Fraction, Fraction]:
    """Crafts and hours to earn the recipe back; hours use the P(sale)-weighted crafts per hour."""

    crafts = Fraction(price) / margin
    return crafts, crafts / (row.crafts_per_hour * Fraction(row.sale_probability))


class RecipePurchaseEvaluator:
    name = "recipe_purchase"

    def evaluate(self, ctx: Context) -> StrategyResult:
        analysis = ctx.cached("crafting", lambda: analyze(ctx))
        out = Collector(self.name)
        if analysis.evaluation.excluded:
            out.notes.append(
                f"{len(analysis.evaluation.excluded)} recipes rank excluded are counted under crafting"
            )
        for craft in analysis.rows:
            row = craft.row
            key = f"recipe_purchase:{row.spell_id}"
            if ctx.state.account is None:
                out.exclude(key, "no_character", "pass a character to classify recipes")
                continue
            if row.status == "known":
                out.exclude(key, "recipe_already_known", "")
                continue
            if row.recipe_price_state == "trainer":
                out.exclude(key, "trainer_taught", f"recipe status {row.status}")
                continue
            if row.recipe_price_state in ("not_listed", "not_observed"):
                reason = (
                    "recipe_not_listed" if row.recipe_price_state == "not_listed" else "recipe_not_observed"
                )
                out.exclude(key, reason, f"recipe status {row.status}")
                continue
            if row.status != "learnable_now":
                out.exclude(key, "not_learnable_now", f"recipe status {row.status}")
                continue
            if row.recipe_price is None or row.recipe_item_id is None:
                raise RuntimeError(f"spell {row.spell_id}: learnable and listed but no recipe price")
            margin = headline_margin(craft)
            if margin is None:
                out.exclude(key, "no_current_listing", "an output has no current listing to undercut")
                continue
            if row.sale_probability <= 0 or row.sale_probability < ctx.config.min_sale_prob:
                out.exclude(key, "low_sale_probability", f"P(sale) {row.sale_probability:.2f}")
                continue
            if margin <= 0:
                out.exclude(key, "non_positive_profit", f"margin {float(margin):.1f}c per craft")
                continue
            base = row_pathway(ctx, analysis, craft)
            crafts, hours = _payback(row.recipe_price, margin, row)
            steps = (
                Step(
                    "buy_ah",
                    item_id=row.recipe_item_id,
                    quantity=1,
                    price=row.recipe_price,
                    note="teaching item",
                ),
                Step("learn_recipe", spell_id=row.spell_id),
                *base.steps,
            )
            out.keep(
                Pathway(
                    strategy=self.name,
                    section="ranked",
                    key=key,
                    title=f"learn + {base.title}",
                    steps=steps,
                    profit_per_unit=base.profit_per_unit,
                    profit_per_craft=base.profit_per_craft,
                    volume_per_hour=base.volume_per_hour,
                    sale_probability=base.sale_probability,
                    fill_rate=None,
                    evidence=base.evidence,
                    demand_evidence=base.demand_evidence,
                    listed_units=base.listed_units,
                    capital=base.capital + row.recipe_price,
                    profit_per_hour=base.profit_per_hour,
                    one_off_profit=None,
                    sort_value=base.sort_value,
                    confidence=base.confidence,
                    details={
                        **base.details,
                        "recipe_price": row.recipe_price,
                        "recipe_item_id": row.recipe_item_id,
                        "payback_crafts": crafts,
                        "payback_hours": hours,
                        "rank_payback_crafts": row.payback_crafts,
                        "rank_payback_hours": row.payback_hours,
                    },
                )
            )
        return out.result()
