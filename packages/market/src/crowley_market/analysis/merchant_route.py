"""Finite, scan-scoped shopping and unlock instructions; never game effects."""

from __future__ import annotations

import json
import math
import time
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from importlib import resources
from typing import Any

from crowley_common.auction_rules import rules_for_house
from crowley_common.errors import MarketDataNotFound
from crowley_common.game_target import canonical_plan
from crowley_common.house import House

from crowley_market.client_data import item_names, load_client_data
from crowley_market.economics import recipe_requirements, vendor_purchase
from crowley_market.models import ItemFacts, Recipe

from .merchant_favor import (
    MAX_PRICE_AGE,
    PriceBook,
    build_comparison,
    catalog,
    load_price_books,
    scan_item_ids,
)
from .merchant_route_solver import allocate
from .trading_summary import exact_money


@lru_cache(maxsize=1)
def writ_catalog() -> dict[str, Any]:
    return json.loads(resources.files("crowley_market.resources").joinpath("merchant_writs.json").read_text())


def writ_item_ids() -> list[int]:
    return sorted({w[k] for w in writ_catalog()["writs"] for k in ("writ_item_id", "required_item_id")})


@dataclass(frozen=True)
class Progress:
    reputation: int = 2150
    favor: int = 30
    level: int = 35
    owned: tuple[int, ...] = ()
    completed_writs: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for value, maximum in [(self.reputation, 42999), (self.favor, 1000000), (self.level, 60)]:
            if type(value) is not int or value < 0 or value > maximum:
                raise ValueError("invalid character progress")
        if self.level == 0:
            raise ValueError("character level must be positive")
        for values in (self.owned, self.completed_writs):
            if any(type(v) is not int or v <= 0 for v in values) or len(set(values)) != len(values):
                raise ValueError("invalid ownership or completed writ IDs")


class Inventory:
    """Reserve shared units and price each new purchase at its marginal depth."""

    def __init__(self, books: dict[int, PriceBook]) -> None:
        self.curves: dict[int, tuple[list[int], list[Fraction]]] = {}
        self.used: dict[int, int] = defaultdict(int)
        self.shopping: dict[int, dict[str, Any]] = {}
        for item_id, book in books.items():
            if book.state != "priced":
                continue
            quantities, costs = [0], [Fraction(0)]
            for price, quantity in sorted(book.levels):
                quantities.append(quantities[-1] + quantity)
                costs.append(costs[-1] + price * quantity)
            self.curves[item_id] = quantities, costs

    def cumulative(self, item_id: int, quantity: int) -> Fraction | None:
        curve = self.curves.get(item_id)
        if curve is None or quantity > curve[0][-1]:
            return None
        if quantity == 0:
            return Fraction(0)
        amounts, costs = curve
        index = bisect_left(amounts, quantity)
        if amounts[index] == quantity:
            return costs[index]
        unit = (costs[index] - costs[index - 1]) / (amounts[index] - amounts[index - 1])
        return costs[index - 1] + (quantity - amounts[index - 1]) * unit

    def quote(self, requirements: dict[int, int]) -> Fraction | None:
        total = Fraction(0)
        for item_id, quantity in requirements.items():
            if type(quantity) is not int or quantity <= 0:
                raise ValueError("shopping quantities must be positive integers")
            after = self.cumulative(item_id, self.used[item_id] + quantity)
            before = self.cumulative(item_id, self.used[item_id])
            if after is None or before is None:
                return None
            total += after - before
        return total

    def buy(self, requirements: dict[int, int], purpose: str) -> Fraction:
        cost = self.quote(requirements)
        if cost is None:
            raise ValueError("cannot reserve unavailable market stock")
        for item_id, quantity in requirements.items():
            old = self.cumulative(item_id, self.used[item_id])
            new = self.cumulative(item_id, self.used[item_id] + quantity)
            assert old is not None and new is not None
            row = self.shopping.setdefault(
                item_id, {"item_id": item_id, "quantity": 0, "cost": Fraction(0), "purposes": set()}
            )
            row["quantity"] += quantity
            row["cost"] += new - old
            row["purposes"].add(purpose)
            self.used[item_id] += quantity
        return cost


def recipe_profitability(
    recipes: tuple[Recipe, ...],
    facts: dict[int, ItemFacts],
    buy_books: dict[int, PriceBook],
    sell_books: dict[int, PriceBook],
    house: str,
    now: float,
) -> list[dict[str, Any]]:
    """One-craft matched-price margins using the retained shared cost assumptions."""
    by_spell = {r.spell_id: r for r in recipes}
    result = []
    rules = rules_for_house(House.parse(house))
    for entry in catalog()["recipes"]:
        row = {**entry, "status": "unpriced", "profit_per_item": None, "missing_item_ids": []}
        recipe = by_spell.get(entry["spell_id"])
        output = sell_books.get(entry["output_item_id"])
        if recipe is None or len(recipe.outputs) != 1 or recipe.outputs[0][0] != entry["output_item_id"]:
            result.append(row)
            continue
        if (
            output is None
            or output.state != "priced"
            or output.observed_at is None
            or now - output.observed_at > MAX_PRICE_AGE
        ):
            row["missing_item_ids"].append(entry["output_item_id"])
            result.append(row)
            continue
        output_fact = facts.get(entry["output_item_id"])
        if output_fact is None or output_fact.bonding not in (0, 2, 3):
            result.append(row)
            continue
        price = math.floor(min(p for p, _ in output.levels))
        material_cost, observed = Fraction(0), [output.observed_at]
        inputs = []
        for item_id, quantity in recipe_requirements(recipe, 1).items():
            book = buy_books.get(item_id)
            ah = None if book is None else book.quote(quantity)
            if book is not None and (book.observed_at is None or now - book.observed_at > MAX_PRICE_AGE):
                ah = None
            vendor = vendor_purchase(facts[item_id], quantity) if item_id in facts else None
            if ah is not None and (vendor is None or ah <= vendor[1]):
                cost, source = ah, "ah"
                observed.append(book.observed_at)  # type: ignore[union-attr]
            elif vendor is not None:
                cost, source = Fraction(vendor[1]), "vendor"
            else:
                row["missing_item_ids"].append(item_id)
                continue
            material_cost += cost
            inputs.append(
                {"item_id": item_id, "quantity": quantity, "source": source, "cost": exact_money(cost)}
            )
        if row["missing_item_ids"]:
            result.append(row)
            continue
        quantity = recipe.outputs[0][1]
        # A successful sale refunds its deposit; only the AH cut is a realized sale fee.
        proceeds = math.floor(Fraction(price * quantity) * (1 - rules.seller_cut))
        profit = Fraction(proceeds) - material_cost
        row.update(
            status="profitable" if profit > 0 else "not_profitable",
            profit_per_item=exact_money(profit / quantity),
            profit_per_craft=exact_money(profit),
            material_cost=exact_money(material_cost),
            deposit_refunded_on_sale=True,
            sale_proceeds=proceeds,
            quantity=quantity,
            matched_unit_price=price,
            observed_at=min(observed),
            reagents=inputs,
        )
        result.append(row)
    result.sort(
        key=lambda r: (
            r["profit_per_item"] is None,
            -Fraction(r["profit_per_item"] or 0),
            r["recipe_item_id"],
        )
    )
    return result


def plan_route(
    progress: Progress,
    economics: list[dict[str, Any]],
    books: dict[int, PriceBook],
    names: dict[int, str],
    *,
    now: float,
    writs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    priorities = [
        r for r in economics if r["status"] == "profitable" and r["recipe_item_id"] not in progress.owned
    ]
    priorities.sort(key=lambda r: (-Fraction(r["profit_per_item"]), r["recipe_item_id"]))
    if not priorities:
        unknown = sum(
            r["status"] == "unpriced" and r["recipe_item_id"] not in progress.owned for r in economics
        )
        return {
            "status": "awaiting_prices" if unknown else "complete",
            "priorities": [],
            "steps": [],
            "shopping": [],
            "message": (
                "No purchase route yet: unowned recipes still lack current price evidence."
                if unknown
                else "No currently profitable unowned faction recipes remain."
            ),
            "unpriced_recipes": sum(r["status"] == "unpriced" for r in economics),
        }
    fresh = {
        i: b
        for i, b in books.items()
        if b.observed_at is not None and 0 <= now - b.observed_at <= MAX_PRICE_AGE
    }
    stock = Inventory(fresh)
    reputation, favor = progress.reputation, progress.favor
    target_rep = max(r["reputation_total"] for r in priorities)
    next_gate = priorities[0]["reputation_total"]
    fill_actions: dict[tuple[int, int], dict[str, Any]] = {}
    forge_actions: dict[int, dict[str, Any]] = {}
    selected_writs, purchases = [], []
    known_refunds, unknown_refunds = 0, False
    eligible_writs = [
        w
        for w in (writs if writs is not None else writ_catalog()["writs"])
        if w["minimum_character_level"] <= progress.level
        and w["faction_eligibility"] in {"both", "alliance"}
        and w["quest_id"] not in progress.completed_writs
    ]
    allocation = allocate(
        fresh,
        reputation=reputation,
        favor=favor,
        level=progress.level,
        goal_rep=next_gate,
        goal_favor=priorities[0]["favor"],
        writs=eligible_writs,
    )
    blocked = allocation.get("error")
    for action in allocation["actions"]:
        data, count = action["data"], action["count"]
        req = {i: q * count for i, q in action["req"].items()}
        cost = stock.buy(
            req, {"writ": "daily writ", "fill": "Favor fill", "forge": "forged return"}[action["kind"]]
        )
        reputation += count * action["rep"]
        favor += count * action["gain"]
        known_refunds += count * action["refund"]
        if action["kind"] == "writ":
            selected_writs.append(
                {
                    **data,
                    "cost": exact_money(cost),
                    "required_item_name": names.get(
                        data["required_item_id"], f"Item {data['required_item_id']}"
                    ),
                }
            )
            unknown_refunds |= data["coin_refund_copper"] is None
        elif action["kind"] == "forge":
            forge_actions[data["item_id"]] = {
                "item_id": data["item_id"],
                "name": data["name"],
                "count": count,
                "cost": cost,
            }
        else:
            option = data["option"]
            fill_actions[(data["item_id"], option["item_id"])] = {
                "crate_id": data["item_id"],
                "crate_name": data["name"],
                "material_id": option["item_id"],
                "material_name": option["name"],
                "per_crate": option["quantity"],
                "count": count,
                "favor": count * data["favor"],
                "refund": count * data["money"],
                "cost": cost,
            }
    remaining = list(priorities)
    if not blocked and reputation >= next_gate and favor >= priorities[0]["favor"]:
        favor -= priorities[0]["favor"]
        purchases.append(remaining.pop(0))
    elif not blocked:
        blocked = (
            "This scan cannot fund the whole next unlock. Complete only the listed steps, "
            "update your progress, and wait for new stock or the next daily reset. "
            "Forged receipts require Friendly."
        )
    shopping = [
        {
            **r,
            "name": names.get(i, f"Item {i}"),
            "cost": exact_money(r["cost"]),
            "purposes": sorted(r["purposes"]),
        }
        for i, r in sorted(stock.shopping.items())
    ]
    total = sum((Fraction(r["cost"]) for r in shopping), Fraction(0))
    steps = []
    if shopping:
        steps.append(
            {
                "kind": "shop",
                "title": "Buy this scan's shopping list at the AH",
                "items": shopping,
                "cost": exact_money(total),
            }
        )
    if selected_writs:
        steps.append(
            {
                "kind": "writs",
                "title": "Deliver these daily writs for reputation first",
                "orders": selected_writs,
            }
        )
    if fill_actions:
        steps.append(
            {
                "kind": "fill",
                "title": "Fill these crates and deliver them to Marcy Baker at Three Corners",
                "fills": [{**a, "cost": exact_money(a["cost"])} for a in fill_actions.values()],
            }
        )
    if forge_actions:
        steps.append(
            {
                "kind": "forge",
                "title": (
                    "At Friendly, buy receipts from Dianne Softstep; stamp only the reserved empty crates "
                    "and return them to Marcy Baker"
                ),
                "crates": [{**a, "cost": exact_money(a["cost"])} for a in forge_actions.values()],
                "favor_cost": sum(a["count"] for a in forge_actions.values()) * 15,
                "reputation": sum(a["count"] for a in forge_actions.values()) * 50,
            }
        )
    if purchases:
        steps.append(
            {
                "kind": "recipes",
                "title": "Buy these recipes from Nina Surefire, in this profit order",
                "recipes": purchases,
            }
        )
    if blocked:
        steps.append({"kind": "blocked", "title": blocked})
    steps.append(
        {
            "kind": "update",
            "title": (
                "After completing the steps, update actual reputation/Favor and mark purchased recipes "
                "and completed writs. Follow the next completed scan's refreshed plan."
            ),
        }
    )
    return {
        "status": "ready" if purchases else "partial",
        "priorities": priorities,
        "focus": priorities[0],
        "target_reputation": target_rep,
        "recipe_favor_total": sum(r["favor"] for r in priorities),
        "receipts_needed_initially": math.ceil(max(0, target_rep - max(progress.reputation, 3000)) / 50),
        "steps": steps,
        "shopping": shopping,
        "upfront_cost": exact_money(total),
        "known_refunds": known_refunds,
        "net_known_cost": exact_money(total - known_refunds),
        "writ_refunds_unknown": unknown_refunds,
        "projected_reputation": reputation,
        "projected_favor": favor,
        "planned_recipe_ids": [r["recipe_item_id"] for r in purchases],
        "remaining_recipe_ids": [r["recipe_item_id"] for r in remaining],
        "remaining_to_friendly": max(0, min(next_gate, 3000) - reputation),
        "unpriced_recipes": sum(r["status"] == "unpriced" for r in economics),
        "method": allocation.get("method", "No qualified stock allocation available."),
    }


def load_merchant_route(
    conn: Any, house: str, progress: Progress, *, now_epoch: float | None = None
) -> dict[str, Any]:
    if house != "us-normal-alliance":
        raise ValueError("Merchant's Favor route supports us-normal-alliance")
    now = time.time() if now_epoch is None else now_epoch
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("invalid route time")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        latest = conn.execute(
            """SELECT ms.id,EXTRACT(EPOCH FROM ms.started_at)::bigint,
             EXTRACT(EPOCH FROM ms.finished_at)::bigint,ms.client_version || '.' || ms.client_build,
             ms.plan_hash
             FROM market_sweeps ms JOIN houses h ON h.id=ms.house_id WHERE h.key=%s
             ORDER BY ms.finished_at DESC,ms.id DESC LIMIT 1""",
            (house,),
        ).fetchone()
        if latest is None:
            raise MarketDataNotFound("No market sweep for the requested route")
        sid, started, finished, build = int(latest[0]), int(latest[1]), int(latest[2]), str(latest[3])
        if build != catalog()["client_build"]:
            raise MarketDataNotFound("Route catalog differs from the client build")
        facts, all_recipes = load_client_data(conn, build)
        spells = {r["spell_id"] for r in catalog()["recipes"]}
        recipes = tuple(r for r in all_recipes if r.spell_id in spells)
        ids = sorted(
            set(scan_item_ids())
            | set(writ_item_ids())
            | {i for r in recipes for i, _ in (*r.reagents, *r.outputs)}
        )
        buy_books = load_price_books(conn, sid, ids, now)
        outputs = sorted({r["output_item_id"] for r in catalog()["recipes"]})
        sell_books = load_price_books(conn, sid, outputs, now, exclude_owned=False)
        names = item_names(conn, build, ids)
        for c in catalog()["crates"]:
            names[c["item_id"]] = c["name"]
        for w in writ_catalog()["writs"]:
            names[w["writ_item_id"]] = w["name"]
        economics = recipe_profitability(recipes, facts, buy_books, sell_books, house, now)
        route = plan_route(progress, economics, buy_books, names, now=now)
        evidence = [b.observed_at for b in buy_books.values() if b.observed_at is not None]
        evidence.extend(r["observed_at"] for r in economics if r.get("observed_at") is not None)
        valid_until = min(evidence, default=finished) + MAX_PRICE_AGE
        checked_at = now if now_epoch is not None else time.time()

        if (
            str(latest[4]) != canonical_plan()["planHash"]
            or not 0 <= checked_at - finished <= MAX_PRICE_AGE
            or checked_at >= valid_until
        ):
            route.update(
                status="awaiting_scan",
                steps=[],
                shopping=[],
                message="Wait for a fresh completed scan with full recipe and writ coverage before buying.",
            )
        return {
            **build_comparison(
                buy_books, house=house, build=build, sweep_id=sid, finished_epoch=finished, now_epoch=now
            ),
            "started_epoch": started,
            "valid_until_epoch": valid_until,
            "next_scan_start_epoch": started + 900,
            "economics": economics,
            "route": route,
            "item_names": names,
            "writ_catalog": writ_catalog(),
            "profit_basis": (
                "Matched AH price, one craft, per produced item after materials and AH cut. "
                "Successful sales refund the deposit; cancellation/expiry costs and sales are not forecast. "
                "Recipe acquisition is separate."
            ),
        }
