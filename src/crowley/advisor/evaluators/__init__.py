"""The strategy evaluators and their registry."""

from __future__ import annotations

from crowley.advisor.evaluators.base import Collector, Context, Evaluator
from crowley.advisor.evaluators.crafting import CraftingEvaluator
from crowley.advisor.evaluators.flip import FlipEvaluator
from crowley.advisor.evaluators.raw_materials import RawMaterialsEvaluator
from crowley.advisor.evaluators.recipe_purchase import RecipePurchaseEvaluator
from crowley.advisor.evaluators.vendor_arbitrage import VendorArbitrageEvaluator

EVALUATORS: dict[str, Evaluator] = {
    evaluator.name: evaluator
    for evaluator in (
        CraftingEvaluator(),
        RecipePurchaseEvaluator(),
        FlipEvaluator(),
        RawMaterialsEvaluator(),
        VendorArbitrageEvaluator(),
    )
}

__all__ = ["EVALUATORS", "Collector", "Context", "Evaluator"]
