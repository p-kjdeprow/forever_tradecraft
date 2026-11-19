"""Finite stock and profit-first recipe unlock regressions."""

from fractions import Fraction

import pytest
from crowley_market.analysis.merchant_favor import PriceBook, catalog
from crowley_market.analysis.merchant_route import (
    Inventory,
    Progress,
    plan_route,
    recipe_profitability,
    writ_catalog,
    writ_item_ids,
)
from crowley_market.models import ItemFacts, Recipe

NOW = 1010


def book(i, price, qty=1000, at=990):
    return PriceBook(i, "priced", at, str(i), ((Fraction(price), qty),))


def goal(i=1, profit=100, rep=0, favor=45):
    return {
        "recipe_item_id": i,
        "name": f"Recipe {i}",
        "status": "profitable",
        "profit_per_item": str(profit),
        "reputation_total": rep,
        "reputation": "Neutral" if rep == 0 else "Honored",
        "favor": favor,
        "skill": 300,
    }


def plan(goals, books, progress=None, **kwargs):
    return plan_route(progress or Progress(), goals, books, {}, now=NOW, **kwargs)


def test_depth_reservations_are_marginal_and_atomic():
    inv = Inventory(
        {1: PriceBook(1, "priced", 990, "1", ((Fraction(1), 2), (Fraction(10), 3))), 2: book(2, 5, 1)}
    )
    assert inv.buy({1: 2}, "first") == 2
    assert inv.quote({1: 2}) == 20
    with pytest.raises(ValueError):
        inv.buy({1: 1, 2: 2}, "incomplete")
    assert inv.used[1] == 2 and inv.shopping[1]["quantity"] == 2
    assert inv.buy({1: 3, 2: 1}, "second") == 35
    assert inv.quote({1: 1}) is None


def test_priority_ignores_skill_gold_and_only_plans_next_unlock():
    goals = [goal(3, 100), goal(2, 200), {**goal(4, 500), "status": "not_profitable"}, goal(5, 900)]
    route = plan(goals, {}, Progress(favor=100000, owned=(5,)))
    assert [r["recipe_item_id"] for r in route["priorities"]] == [2, 3]
    assert route["planned_recipe_ids"] == [2]
    assert route["remaining_recipe_ids"] == [3]
    assert route["shopping"] == [] and route["projected_favor"] == 99955


def test_neutral_focus_does_not_farm_for_lower_priority_honored_recipe():
    route = plan([goal(1, 200), goal(2, 100, 9000)], {}, Progress(favor=45))
    assert route["planned_recipe_ids"] == [1]
    assert route["projected_reputation"] == 2150
    assert route["target_reputation"] == 9000


def test_no_prices_is_not_complete_and_missing_stock_has_no_phantom_buy():
    assert (
        plan([{**goal(), "status": "unpriced", "profit_per_item": None}], {})["status"] == "awaiting_prices"
    )
    route = plan([goal()], {})
    assert route["status"] == "partial" and not route["shopping"]
    assert not route["planned_recipe_ids"]


def test_writs_are_daily_distinct_and_forging_requires_friendly():
    writs = writ_catalog()["writs"]
    books = {i: book(i, 10) for i in writ_item_ids()}
    route = plan([goal(rep=9000)], books, Progress(completed_writs=(writs[0]["quest_id"],)))
    orders = next(s for s in route["steps"] if s["kind"] == "writs")["orders"]
    ids = [w["quest_id"] for w in orders]
    assert len(ids) == len(set(ids)) and writs[0]["quest_id"] not in ids
    assert route["projected_reputation"] >= 3000
    assert not any(s["kind"] == "forge" for s in route["steps"])
    blocked = plan(
        [goal(rep=9000)], {catalog()["crates"][0]["item_id"]: book(catalog()["crates"][0]["item_id"], 1)}
    )
    assert not blocked["shopping"] and blocked["projected_reputation"] == 2150


def test_shared_empty_stock_is_not_both_filled_and_forged():
    crate = next(c for c in catalog()["crates"] if c["money"] is not None)
    option = crate["options"][0]
    books = {crate["item_id"]: book(crate["item_id"], 100, 2), option["item_id"]: book(option["item_id"], 10)}
    route = plan([goal(rep=3050)], books, Progress(reputation=3000, favor=15))
    assert next(s for s in route["steps"] if s["kind"] == "forge")["crates"][0]["count"] == 1
    assert next(s for s in route["steps"] if s["kind"] == "fill")["fills"][0]["count"] == 1
    assert next(r for r in route["shopping"] if r["item_id"] == crate["item_id"])["quantity"] == 2
    assert route["status"] == "partial"


def test_stale_supply_cannot_generate_purchases():
    c = next(c for c in catalog()["crates"] if c["money"] is not None)
    o = c["options"][0]
    route = plan_route(
        Progress(favor=0),
        [goal()],
        {c["item_id"]: book(c["item_id"], 1, at=0), o["item_id"]: book(o["item_id"], 1, at=0)},
        {},
        now=4000,
    )
    assert route["shopping"] == []


def test_one_craft_profit_matches_price_and_normalizes_yield_after_cut():
    entry = catalog()["recipes"][0]
    out = entry["output_item_id"]
    recipe = Recipe(
        entry["spell_id"], frozenset({"Alchemy"}), ((90001, 2), (90002, 1)), ((out, 2),), None, 0, "client"
    )
    facts = {
        out: ItemFacts(out, 10000, None, None, 20, 0),
        90001: ItemFacts(90001, 0, None, None, 20, 0),
        90002: ItemFacts(90002, 0, 5, 10, 20, 0),
    }
    rows = recipe_profitability(
        (recipe,), facts, {90001: book(90001, 20)}, {out: book(out, 100)}, "us-normal-alliance", NOW
    )
    row = next(r for r in rows if r["recipe_item_id"] == entry["recipe_item_id"])
    assert row["matched_unit_price"] == 100
    assert row["material_cost"] == "42" and row["sale_proceeds"] == 190
    assert row["profit_per_item"] == "74" and row["deposit_refunded_on_sale"]
    assert row["reagents"][1]["source"] == "vendor"
    assert row["reagents"][1]["vendor_purchase_quantity"] == 5
    assert row["reagents"][1]["vendor_purchase_cost"] == 10
    negative = recipe_profitability(
        (recipe,), facts, {90001: book(90001, 100)}, {out: book(out, 100)}, "us-normal-alliance", NOW
    )
    loss = next(r for r in negative if r["recipe_item_id"] == entry["recipe_item_id"])
    assert loss["status"] == "not_profitable" and Fraction(loss["profit_per_item"]) < 0
    missing = recipe_profitability(
        (recipe,), facts, {90001: book(90001, 20)}, {out: book(out, 100, at=0)}, "us-normal-alliance", 4000
    )
    assert all(r["status"] == "unpriced" for r in missing)


def test_writ_cohort_and_all_recipe_outputs_are_core(packaged_catalog):
    core = {i.item_id for i in packaged_catalog.items if i.status == "active" and i.pool == "core"}
    assert set(writ_item_ids()) <= core
    assert {r["output_item_id"] for r in catalog()["recipes"]} <= core
    assert len(writ_catalog()["writs"]) == 11
    assert sum(w["base_rep"] for w in writ_catalog()["writs"]) == 1025


def test_joint_allocation_preserves_fillable_crates_for_funding():
    a = next(c for c in catalog()["crates"] if c["money"] is not None)
    b = next(c for c in catalog()["crates"] if c["item_id"] != a["item_id"])
    option = a["options"][0]
    books = {
        a["item_id"]: book(a["item_id"], 1, 2),
        b["item_id"]: book(b["item_id"], 2, 100),
        option["item_id"]: book(option["item_id"], 10),
    }
    route = plan([goal(rep=3100, favor=10)], books, Progress(reputation=3000, favor=30))
    assert route["status"] == "ready"
    assert route["planned_recipe_ids"] == [1]
    assert sum(s["count"] for step in route["steps"] if step["kind"] == "fill" for s in step["fills"]) == 2


def test_writ_subset_beats_greedy_and_continues_after_friendly():
    from crowley_market.analysis.merchant_route_solver import allocate

    base = writ_catalog()["writs"][0]
    writs = [
        {**base, "quest_id": 1, "writ_item_id": 1, "required_item_id": 3, "base_rep": 75},
        {**base, "quest_id": 2, "writ_item_id": 2, "required_item_id": 3, "base_rep": 125},
    ]
    books = {1: book(1, 59, 1), 2: book(2, 84, 1), 3: book(3, 1, 2)}
    for reputation, goal_rep in [(2900, 3000), (3000, 3100)]:
        result = allocate(
            books, reputation=reputation, favor=45, level=35, goal_rep=goal_rep, goal_favor=45, writs=writs
        )
        assert len(result["actions"]) == 1
        assert result["actions"][0]["data"]["quest_id"] == 2
        assert result["rep_shortfall"] == 0


def test_writs_replacing_forges_cannot_cause_excess_fill_farming():
    from crowley_market.analysis.merchant_route_solver import allocate

    c = next(c for c in catalog()["crates"] if c["money"] is not None)
    o = c["options"][0]
    w = {**writ_catalog()["writs"][0], "base_rep": 1000}
    books = {
        c["item_id"]: book(c["item_id"], 1),
        o["item_id"]: book(o["item_id"], 0),
        w["writ_item_id"]: book(w["writ_item_id"], 1),
        w["required_item_id"]: book(w["required_item_id"], 1),
    }
    result = allocate(books, reputation=2150, favor=45, level=35, goal_rep=3000, goal_favor=45, writs=[w])
    assert [a["kind"] for a in result["actions"]] == ["writ"]


@pytest.mark.parametrize(
    "plan_matches,query_at,now,actionable",
    [(True, 990, 1010, True), (False, 990, 1010, False), (True, 0, 1810, False)],
)
def test_loader_is_read_only_pinned_and_expires_original_query(
    monkeypatch, plan_matches, query_at, now, actionable
):
    from contextlib import nullcontext

    from crowley_market.analysis import merchant_route as module

    class Result:
        def fetchone(self):
            return (7, 900, 1000, "1.60.1.70959", "current" if plan_matches else "old")

    class Conn:
        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=None):
            assert not any(v in sql.upper().split() for v in ["INSERT", "UPDATE", "DELETE"])
            if "JOIN houses" in sql:
                assert params == ("us-normal-alliance",)
            return Result()

    def books(conn, sid, ids, at, **kwargs):
        assert sid == 7 and at == now
        return {1: book(1, 1, at=query_at)}

    monkeypatch.setattr(module, "canonical_plan", lambda: {"planHash": "current"})
    monkeypatch.setattr(module, "load_client_data", lambda conn, build, **kwargs: ({}, ()))
    monkeypatch.setattr(module, "item_names", lambda conn, build, ids: {1: "Tracked"})
    monkeypatch.setattr(module, "load_price_books", books)
    monkeypatch.setattr(module, "recipe_profitability", lambda *args: [{**goal(), "observed_at": query_at}])
    result = module.load_merchant_route(Conn(), "us-normal-alliance", Progress(favor=45), now_epoch=now)
    assert result["sweep_id"] == 7 and result["item_names"][1] == "Tracked"
    assert result["valid_until_epoch"] == query_at + 1800
    assert bool(result["route"]["steps"]) is actionable
    if not actionable:
        assert result["route"]["status"] == "awaiting_scan"


def test_optimizer_timeout_never_emits_purchase_instructions(monkeypatch):
    from types import SimpleNamespace

    from crowley_market.analysis import merchant_route_solver as solver

    def timeout(*args, **kwargs):
        assert kwargs["options"]["threads"] == 1
        return SimpleNamespace(status=1)

    monkeypatch.setattr(solver, "milp", timeout)
    c = next(c for c in catalog()["crates"] if c["money"] is not None)
    o = c["options"][0]
    route = plan([goal()], {c["item_id"]: book(c["item_id"], 100), o["item_id"]: book(o["item_id"], 10)})
    assert not route["shopping"] and not route["planned_recipe_ids"]
    assert any("optimization did not finish" in s["title"] for s in route["steps"])


def test_explicit_vial_vendor_admission_is_local_and_uses_native_bundle():
    from crowley_market.client_data import _load_facts

    class Result:
        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [(18256, 1500, 30000, 5, 20, 0), (1000, 1, 200, 5, 20, 0)]

    class Conn:
        def execute(self, sql, params):
            return Result()

    ordinary = _load_facts(Conn(), "1.60.1.70959")
    assert ordinary[18256].vendor_bundle_price is None
    route = _load_facts(Conn(), "1.60.1.70959", extra_vendor_supply_ids=frozenset({18256}))
    assert route[18256].vendor_bundle_price == 30000 and route[18256].vendor_bundle_size == 5
    assert route[1000].vendor_bundle_price is None


def test_solver_relaxes_only_price_tiers_and_reconstructs_exact_depth(monkeypatch):
    import numpy as np
    from crowley_market.analysis import merchant_route_solver as solver

    c = {
        "item_id": 1,
        "name": "Test crate",
        "favor": 5,
        "money": 0,
        "min_level": 1,
        "options": [{"item_id": 2, "quantity": 1}],
    }
    monkeypatch.setattr(solver, "catalog", lambda: {"crates": [c]})
    real = solver.milp

    def inspect(*args, **kwargs):
        mask = kwargs["integrality"]
        # First variable is the fill count, followed by the four price tiers.
        assert mask[0] == 1 and np.all(mask[1:5] == 0) and np.all(mask[5:] == 1)
        return real(*args, **kwargs)

    monkeypatch.setattr(solver, "milp", inspect)
    books = {i: PriceBook(i, "priced", 990, str(i), ((Fraction(1), 1), (Fraction(10), 1))) for i in (1, 2)}
    result = solver.allocate(
        books, reputation=3000, favor=0, level=35, goal_rep=3000, goal_favor=10, writs=[]
    )
    assert not result.get("error") and result["actions"][0]["count"] == 2
    assert Inventory(books).buy({1: 2, 2: 2}, "fill") == 22


def test_solver_rejects_fractional_action_counts(monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    from crowley_market.analysis import merchant_route_solver as solver

    def fractional(objective, **kwargs):
        values = np.zeros(len(objective))
        values[0] = 0.5
        return SimpleNamespace(status=0, x=values)

    monkeypatch.setattr(solver, "milp", fractional)
    c = next(c for c in catalog()["crates"] if c["money"] is not None)
    o = c["options"][0]
    with pytest.raises(ValueError, match="fractional action"):
        solver.allocate(
            {c["item_id"]: book(c["item_id"], 1), o["item_id"]: book(o["item_id"], 1)},
            reputation=3000,
            favor=0,
            level=35,
            goal_rep=3000,
            goal_favor=5,
            writs=[],
        )
