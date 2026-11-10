"""Cold-pool crafting pathways of the advisor (the candidates view as a pathway)."""

from __future__ import annotations

from dataclasses import replace

from crowley.advisor import AdviceConfig, advise
from tests.test_advisor import by_strategy, state_of
from tests.test_analysis_candidates import POOL, SELLER, cold_book, cold_inputs


def test_cold_pool_recipe_becomes_a_low_confidence_pathway_without_a_sale_probability():
    inputs = cold_inputs([cold_book(k) for k in range(12)], [SELLER])
    state = replace(state_of(inputs), cold_pool=POOL)
    config = AdviceConfig(
        professions=frozenset({"alchemy"}), batch_crafts=2, strategies=frozenset({"crafting"})
    )
    result = by_strategy(advise(state, config), "crafting")
    assert result.considered == 1 and not result.exclusions
    (pathway,) = result.pathways
    assert pathway.details["kind"] == "cold" and pathway.title.endswith("[cold]")
    assert pathway.sale_probability is None and pathway.evidence is None
    assert pathway.confidence == "low" and pathway.demand_evidence is not None
    assert pathway.demand_evidence.kind == "demand_intervals" and pathway.demand_evidence.n == 11
    # priced 1c under the latest observed lowest (95); 55 units walked in 11 hourly gaps = 5 units/h
    assert pathway.steps[-1].price == 94 and pathway.volume_per_hour == 5
    assert pathway.details["price_lowest"] == {1001: 94}
    assert pathway.profit_per_craft is not None
    assert pathway.profit_per_hour == pathway.profit_per_craft * config.share * 5
    assert pathway.listed_units == 35
    # without the cold pool the same recipe is only an excluded one, still counted
    plain = by_strategy(advise(state_of(inputs), config), "crafting")
    assert [e.reason for e in plain.exclusions] == ["rank:output_no_current_book"]
    assert plain.considered == 1
