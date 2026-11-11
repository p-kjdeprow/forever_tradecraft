"""Cold-pool crafting pathways of the advisor (the candidates view as a pathway)."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

from crowley.advisor import AdviceConfig, advise
from crowley.analysis.books import Book, Group
from tests.test_advisor import by_strategy, state_of
from tests.test_analysis_candidates import (
    COLD_ID,
    COLD_STEP,
    POOL,
    SELLER,
    cold_book,
    cold_inputs,
    reagent_books,
)

CONFIG = AdviceConfig(professions=frozenset({"alchemy"}), batch_crafts=2, strategies=frozenset({"crafting"}))


def cold_state(books, **kwargs):
    return replace(state_of(cold_inputs(books, [SELLER], **kwargs)), cold_pool=POOL)


def test_cold_pool_recipe_is_an_unmeasured_low_confidence_pathway():
    state = cold_state([cold_book(k) for k in range(12)])
    result = by_strategy(advise(state, CONFIG), "crafting")
    assert result.considered == 1 and not result.exclusions
    (pathway,) = result.pathways
    assert pathway.section == "unmeasured"
    assert pathway.details["kind"] == "cold" and pathway.title.endswith("[cold]")
    assert pathway.sale_probability is None and pathway.evidence is None
    assert pathway.confidence == "low" and pathway.demand_evidence is not None
    assert pathway.demand_evidence.kind == "demand_intervals" and pathway.demand_evidence.n == 11
    # priced 1c under the latest observed lowest (95), never above the target; 55 units in 11 hourly
    # gaps = 5 units/h
    assert pathway.steps[-1].price == min(94, pathway.details["headline_prices"][COLD_ID]) == 94
    assert pathway.volume_per_hour == 5
    assert pathway.details["price_undercut_lowest"] == {COLD_ID: 94}
    assert pathway.profit_per_craft is not None
    assert pathway.profit_per_hour == pathway.sort_value == pathway.profit_per_craft * CONFIG.share * 5
    assert pathway.listed_units == 35
    # a price this old must be checked first: the first step says so
    first = pathway.steps[0]
    assert (
        first.action == "verify_price" and first.item_id == COLD_ID and "check the current AH" in first.note
    )
    assert pathway.details["observation_age_hours"] == 0
    # without the cold pool the same recipe is only an excluded one, still counted
    plain = by_strategy(
        advise(state_of(cold_inputs([cold_book(k) for k in range(12)], [SELLER])), CONFIG), "crafting"
    )
    assert [e.reason for e in plain.exclusions] == ["rank:output_no_current_book"]
    assert plain.considered == 1


def test_cold_observation_age_gate():
    books = [cold_book(k) for k in range(12)]
    last_t = 11 * COLD_STEP
    fresh = cold_state(books, as_of=float(last_t + 2 * 3600))  # latest observation 2 h old
    (pathway,) = by_strategy(advise(fresh, CONFIG), "crafting").pathways
    assert pathway.details["observation_age_hours"] == 2
    stale = cold_state(books, as_of=float(last_t + 4 * 3600))  # 4 h old: over the 3 h default
    result = by_strategy(advise(stale, CONFIG), "crafting")
    assert [(e.reason, e.key) for e in result.exclusions] == [("stale_observation", "crafting:1")]
    assert result.considered == 1 and not result.pathways
    # the limit is configurable
    lenient = replace(CONFIG, max_cold_age_hours=6.0)
    assert len(by_strategy(advise(stale, lenient), "crafting").pathways) == 1
    # an old reagent price counts as well as an old output price
    old_reagents = reagent_books(12)[:6]  # last reagent observation at 5 * 3600
    mixed = cold_state(books, reagents=old_reagents)
    assert [e.reason for e in by_strategy(advise(mixed, CONFIG), "crafting").exclusions] == [
        "stale_observation"
    ]


def test_cold_recipe_without_depletion_is_excluded_not_shown_with_no_confidence():
    # the same five units sit there at every observation: nothing was ever bought from the bottom
    still = [
        Book(
            float(k * COLD_STEP),
            [Group("low", Fraction(95), 5, 100_000, 1), Group("ref", Fraction(100), 30, 100_000, 1)],
            None,
            COLD_ID,
            k * 6,
        )
        for k in range(12)
    ]
    result = by_strategy(advise(cold_state(still), CONFIG), "crafting")
    assert [e.reason for e in result.exclusions] == ["no_observed_demand"] and not result.pathways
    assert result.considered == 1


def test_target_priced_cold_candidate_without_a_target_is_excluded_for_that_reason():
    # too few books for a fair value: no target price
    state = cold_state([cold_book(k) for k in range(4)])
    config = replace(CONFIG, crafting_price="target")
    reasons = [e.reason for e in by_strategy(advise(state, config), "crafting").exclusions]
    assert reasons == ["no_target_price"]


def test_cold_candidate_without_a_target_is_excluded_on_the_default_basis_too():
    state = cold_state([cold_book(k) for k in range(4)])
    result = by_strategy(advise(state, CONFIG), "crafting")
    assert [e.reason for e in result.exclusions] == ["no_target_price"] and not result.pathways
