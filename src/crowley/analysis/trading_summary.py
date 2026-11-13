"""Fast, read-only trading dashboard summary for one pinned market sweep.

This is a compact if-sold margin view, not a sales forecast.  It uses the
shared scan books and client-data loaders, keeps market identities separate,
and leaves turnover unknown when the interval evidence is incomplete.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from crowley.advisor.cache import AdviceCache
from crowley.analysis.books import Book, load_commodity_books, valid_intervals
from crowley.analysis.item_books import ItemBook, item_intervals, load_item_books
from crowley.analysis.rank import item_names
from crowley.core.auction_rules import AuctionHouseRules, rules_for_house
from crowley.core.errors import MarketDataNotFound
from crowley.core.house import House
from crowley.trading.api import ItemFacts, Recipe
from crowley.trading.data import load_client_data
from crowley.trading.fees import deposit
from crowley.trading.strategies.common import vendor_purchase
from crowley.trading.strategies.pricing import recipe_requirements

ALCHEMY_SKILL = 225
TAILORING_SKILL = 150
TAILORING_PLANNING_CAP = 225
BUDGET_COPPER = 100_000
SHARE = Fraction(1, 10)
PRICE_MAX_AGE_SECONDS = 3 * 60 * 60
MAX_EVIDENCE_GAP_SECONDS = 1_260
WINDOWS_HOURS = (2, 6)
WINDOW_COVERAGE = Fraction(7, 10)
LISTING_HOURS = 12
MAX_CRAFTS = 5
PROFILE_KEY = ("alchemy", ALCHEMY_SKILL, "tailoring", TAILORING_SKILL, TAILORING_PLANNING_CAP, BUDGET_COPPER)

TRADING_SUMMARY_CACHE: AdviceCache[dict[str, Any]] = AdviceCache(max_entries=8, ttl_seconds=900)


@dataclass(frozen=True)
class TradingSummaryInputs:
    house: str
    client_build: str
    sweep_id: int
    finished_epoch: int
    scan: Mapping[str, int]
    recipes: Sequence[Recipe]
    facts: Mapping[int, ItemFacts]
    commodity_books: Mapping[int, Sequence[Book]]
    item_books: Mapping[int, Sequence[ItemBook]]
    names: Mapping[int, str]
    rules: AuctionHouseRules


@dataclass(frozen=True)
class _Quote:
    kind: str
    observed_at: float
    lowest: Fraction
    commodity_book: Book | None = None
    item_book: ItemBook | None = None
    item_key: str | None = None


@dataclass(frozen=True)
class _Activity:
    rate: Fraction | None
    usable_hours: float | None
    status: str
    label: str


@dataclass(frozen=True)
class _Eligibility:
    profession: str
    required_skill: int | None
    needs_leveling: bool
    note: str


def exact_money(value: Fraction | int) -> str:
    """Serialize copper without converting through binary floating point."""

    amount = Fraction(value)
    if amount.denominator == 1:
        return str(amount.numerator)
    return f"{amount.numerator}/{amount.denominator}"


def _latest_quote(inputs: TradingSummaryInputs, item_id: int) -> _Quote | None:
    commodities = [
        book for book in inputs.commodity_books.get(item_id, ()) if book.t <= inputs.finished_epoch
    ]
    items = [book for book in inputs.item_books.get(item_id, ()) if book.t <= inputs.finished_epoch]
    if commodities and items:
        return None
    if commodities:
        book = max(commodities, key=lambda entry: (entry.t, str(entry.sweep_id)))
        if not book.groups:
            return None
        return _Quote("commodity", book.t, min(group.unit for group in book.groups), commodity_book=book)
    if items:
        keys = frozenset(key for book in items for key in book.item_keys)
        if len(keys) != 1:
            return None
        item_key = next(iter(keys))
        book = max(items, key=lambda entry: (entry.t, entry.sweep_id))
        auctions = book.scoped(item_key)
        if not auctions:
            return None
        return _Quote(
            "item", book.t, min(auction.unit for auction in auctions), item_book=book, item_key=item_key
        )
    return None


def _fresh_quote(inputs: TradingSummaryInputs, item_id: int) -> _Quote | None:
    quote = _latest_quote(inputs, item_id)
    if quote is None:
        return None
    age = inputs.finished_epoch - quote.observed_at
    if age < 0:
        raise ValueError(f"item {item_id} book is newer than its pinned sweep")
    if age > PRICE_MAX_AGE_SECONDS:
        return None
    return quote


def _target_price(quote: _Quote) -> int:
    """Use the current lowest observed listing, undercut by one whole copper."""

    return math.floor(quote.lowest) - 1


def _depth_cost(quote: _Quote, quantity: int) -> Fraction | None:
    if quantity <= 0:
        raise ValueError("reagent quantity must be positive")
    remaining = quantity
    cost = Fraction(0)
    if quote.kind == "commodity":
        assert quote.commodity_book is not None
        levels = sorted(quote.commodity_book.groups, key=lambda group: (group.unit, str(group.result_id)))
        for group in levels:
            take = min(remaining, group.quantity)
            cost += group.unit * take
            remaining -= take
            if remaining == 0:
                return cost
        return None
    assert quote.item_book is not None
    auctions = sorted(
        quote.item_book.scoped(quote.item_key), key=lambda auction: (auction.unit, auction.result_id)
    )
    for auction in auctions:
        if auction.own:
            continue
        take = min(remaining, auction.quantity)
        cost += auction.unit * take
        remaining -= take
        if remaining == 0:
            return cost
    return None


def _buy_reagent(
    inputs: TradingSummaryInputs, item_id: int, quantity: int, quote: _Quote | None
) -> tuple[str, Fraction, int] | None:
    ah_cost = None if quote is None else _depth_cost(quote, quantity)
    fact = inputs.facts.get(item_id)
    vendor = None if fact is None else vendor_purchase(fact, quantity)
    vendor_cost = None if vendor is None else Fraction(vendor[1])
    if ah_cost is not None and (vendor_cost is None or ah_cost <= vendor_cost):
        return "ah", ah_cost, quantity
    if vendor is not None:
        return "vendor", Fraction(vendor[1]), vendor[0]
    return None


def _commodity_activity(
    books: Sequence[Book], target_price: int, finished_epoch: int
) -> tuple[tuple[int, float], tuple[int, float]]:
    intervals = valid_intervals(books)
    measured: list[tuple[int, float]] = []
    for hours in WINDOWS_HOURS:
        start = finished_epoch - hours * 3600
        units = 0
        covered_seconds = 0.0
        for interval in intervals:
            if interval is None or interval.before.t < start or interval.after.t > finished_epoch:
                continue
            if interval.ceiling is not None and target_price > interval.ceiling:
                continue
            if interval.walk.reached_ceiling:
                continue
            covered_seconds += interval.dt
            units += sum(count for price, count in interval.walk.consumed if price > target_price)
        measured.append((units, covered_seconds))
    return measured[0], measured[1]


def _item_activity(
    books: Sequence[ItemBook], item_key: str, target_price: int, finished_epoch: int
) -> tuple[tuple[int, float], tuple[int, float]]:
    intervals = item_intervals(books, item_key)
    measured: list[tuple[int, float]] = []
    for hours in WINDOWS_HOURS:
        start = finished_epoch - hours * 3600
        units = 0
        covered_seconds = 0.0
        for interval in intervals:
            if (
                interval is None
                or interval.dt > MAX_EVIDENCE_GAP_SECONDS
                or interval.before.t < start
                or interval.after.t > finished_epoch
            ):
                continue
            covered_seconds += interval.dt
            units += sum(count for price, count in interval.consumed if price > target_price)
        measured.append((units, covered_seconds))
    return measured[0], measured[1]


def _activity(inputs: TradingSummaryInputs, item_id: int, quote: _Quote, target_price: int) -> _Activity:
    if quote.kind == "commodity":
        windows = _commodity_activity(inputs.commodity_books[item_id], target_price, inputs.finished_epoch)
    else:
        assert quote.item_key is not None
        books = inputs.item_books[item_id]
        windows = _item_activity(books, quote.item_key, target_price, inputs.finished_epoch)
    rates: list[Fraction] = []
    coverage_hours: list[float] = []
    qualified = True
    any_units = False
    for (units, covered_seconds), hours in zip(windows, WINDOWS_HOURS, strict=True):
        coverage_hours.append(covered_seconds / 3600)
        required = float(WINDOW_COVERAGE * hours * 3600)
        if covered_seconds < required:
            qualified = False
        if covered_seconds > 0:
            rates.append(Fraction(units * 3600, 1) / Fraction(str(covered_seconds)))
        else:
            rates.append(Fraction(0))
        any_units = any_units or units > 0
    usable_hours = min(coverage_hours) if max(coverage_hours) > 0 else None
    if not qualified:
        return _Activity(
            None, usable_hours, "unknown", "Insufficient 2h/6h interval coverage; turnover unknown"
        )
    conservative = min(rates)
    if any_units and conservative > 0:
        return _Activity(
            conservative,
            usable_hours,
            "observed_activity",
            "Observed listing depletion; sales unconfirmed",
        )
    return _Activity(
        Fraction(0),
        usable_hours,
        "observed_no_depletion",
        "Qualified books showed no bottom-walk depletion; sales unconfirmed",
    )


def _eligibility(recipe: Recipe) -> _Eligibility | None:
    """Resolve learning requirements without treating a yellow point as an exact rank."""

    profile = {"alchemy": ALCHEMY_SKILL, "tailoring": TAILORING_SKILL}
    caps = {"alchemy": ALCHEMY_SKILL, "tailoring": TAILORING_PLANNING_CAP}
    candidates: list[_Eligibility] = []
    for profession in sorted(recipe.professions & profile.keys()):
        current = profile[profession]
        cap = caps[profession]
        skill_lines = [line for line in recipe.acquisition.skill_lines if line.profession == profession]
        matching_ids = {line.skill_line_id for line in skill_lines}
        exact_ranks = [
            req.rank for req in recipe.acquisition.teaching_requirements if req.skill_line_id in matching_ids
        ]
        if exact_ranks:
            rank = min(exact_ranks)
            if rank <= current:
                candidates.append(_Eligibility(profession, rank, False, f"{profession.title()} {rank}"))
            elif rank <= cap:
                candidates.append(
                    _Eligibility(profession, rank, True, f"{profession.title()} {rank}; leveling target")
                )
            continue
        yellows = [line.yellow for line in skill_lines if line.yellow > 0]
        if not yellows:
            continue
        yellow = min(yellows)
        if yellow > cap:
            continue
        if yellow > current:
            candidates.append(
                _Eligibility(
                    profession,
                    None,
                    True,
                    f"Eligibility by {yellow}; learning rank unknown; verify training",
                )
            )
        else:
            candidates.append(
                _Eligibility(
                    profession,
                    None,
                    False,
                    f"Eligibility by {yellow}; learning rank unknown; verify training",
                )
            )
    if not candidates:
        return None
    return min(
        candidates, key=lambda value: (value.needs_leveling, value.required_skill or 0, value.profession)
    )


def _stack_sizes(quantity: int, max_stack: int) -> Sequence[int]:
    if quantity < 0 or max_stack < 1:
        raise ValueError("output quantity and stack limit are invalid")
    stacks: list[int] = []
    remaining = quantity
    while remaining:
        stack = min(remaining, max_stack)
        stacks.append(stack)
        remaining -= stack
    return stacks


def _listing_costs(
    fact: ItemFacts, quantity: int, unit_list_price: int, rules: AuctionHouseRules
) -> tuple[int, int]:
    proceeds = 0
    deposits = 0
    for stack in _stack_sizes(quantity, fact.max_stack):
        gross = unit_list_price * stack
        proceeds += math.floor(Fraction(gross) * (1 - rules.seller_cut))
        deposits += deposit(fact.sell_price, stack, LISTING_HOURS, rules)
    return proceeds, deposits


def _eligibility_note(eligibility: _Eligibility, profession: str) -> str:
    if eligibility.profession != profession:
        raise ValueError("eligibility profession disagrees with row profession")
    return eligibility.note


def _opportunity(
    inputs: TradingSummaryInputs,
    recipe: Recipe,
    eligibility: _Eligibility,
    output_quote: _Quote,
    activity: _Activity,
    list_price: int,
) -> tuple[dict[str, Any], int, Fraction] | None:
    if len(recipe.outputs) != 1:
        return None
    item_id, output_per_craft = recipe.outputs[0]
    if output_per_craft <= 0:
        return None
    output_fact = inputs.facts.get(item_id)
    if output_fact is None or output_fact.bonding not in (0, 2, 3):
        return None
    if activity.rate is None or activity.rate <= 0 or recipe.cooldown_seconds > 0:
        desired_crafts = 1
    else:
        expected = activity.rate * SHARE * 2 / output_per_craft
        desired_crafts = max(1, min(MAX_CRAFTS, math.floor(expected)))
    reagent_per_craft = recipe_requirements(recipe, 1)
    reagent_quotes: dict[int, _Quote | None] = {}
    for reagent_id in reagent_per_craft:
        reagent_quotes[reagent_id] = _fresh_quote(inputs, reagent_id)
    chosen: tuple[int, Fraction, Fraction, int, list[dict[str, Any]]] | None = None
    for crafts in range(desired_crafts, 0, -1):
        reagents = recipe_requirements(recipe, crafts)
        reagent_rows: list[dict[str, Any]] = []
        material_cost = Fraction(0)
        possible = True
        for reagent_id, quantity in sorted(reagents.items()):
            source = _buy_reagent(inputs, reagent_id, quantity, reagent_quotes[reagent_id])
            if source is None:
                possible = False
                break
            source_name, cost, bought_quantity = source
            material_cost += cost
            reagent_rows.append(
                {
                    "item_id": reagent_id,
                    "name": inputs.names.get(reagent_id, f"Item {reagent_id}"),
                    "quantity": quantity,
                    "source": source_name,
                    "cost": exact_money(cost),
                    "bundle_quantity": bought_quantity,
                }
            )
        if not possible:
            continue
        output_quantity = output_per_craft * crafts
        sale_proceeds, listing_deposit = _listing_costs(
            output_fact, output_quantity, list_price, inputs.rules
        )
        total_cost = material_cost + listing_deposit
        if total_cost > BUDGET_COPPER:
            continue
        profit = Fraction(sale_proceeds) - total_cost
        if profit <= 0:
            continue
        chosen = (crafts, material_cost, total_cost, listing_deposit, reagent_rows)
        break
    if chosen is None:
        return None
    crafts, material_cost, total_cost, listing_deposit, reagent_rows = chosen
    output_quantity = output_per_craft * crafts
    sale_proceeds, _ = _listing_costs(output_fact, output_quantity, list_price, inputs.rules)
    profit = Fraction(sale_proceeds) - total_cost
    row = {
        "spell_id": recipe.spell_id,
        "item_id": item_id,
        "name": inputs.names.get(item_id, f"Item {item_id}"),
        "profession": eligibility.profession,
        "required_skill": eligibility.required_skill,
        "skill_note": _eligibility_note(eligibility, eligibility.profession),
        "needs_leveling": eligibility.needs_leveling,
        "crafts": crafts,
        "quantity": output_quantity,
        "unit_list_price": str(list_price),
        "cost": exact_money(total_cost),
        "material_cost": exact_money(material_cost),
        "deposit_cost": exact_money(listing_deposit),
        "sale_proceeds": exact_money(sale_proceeds),
        "profit": exact_money(profit),
        "market_units_per_hour": None if activity.rate is None else round(float(activity.rate), 2),
        "volume_status": activity.status,
        "usable_hours": activity.usable_hours,
        "sale_probability": None,
        "evidence_label": activity.label,
        "price_age_seconds": max(0, math.floor(inputs.finished_epoch - output_quote.observed_at)),
        "cooldown_seconds": recipe.cooldown_seconds,
        "cooldown_group": recipe.cooldown_group,
        "requirements": {
            "tools": list(recipe.totems),
            "tool_categories": list(recipe.totem_categories),
            "focus": recipe.spell_focus,
        },
        "reagents": reagent_rows,
    }
    flow = Fraction(0)
    if activity.rate is not None:
        craft_rate = activity.rate * SHARE / output_per_craft
        if recipe.cooldown_seconds > 0:
            craft_rate = min(craft_rate, Fraction(3600, recipe.cooldown_seconds))
        flow = (profit / crafts) * craft_rate
    tier = 2 if eligibility.needs_leveling else (0 if activity.rate is not None and activity.rate > 0 else 1)
    return row, tier, flow


def build_trading_summary(inputs: TradingSummaryInputs) -> dict[str, Any]:
    """Calculate up to ten positive-margin opportunities from synthetic or loaded inputs."""

    House.parse(inputs.house)
    candidates: list[tuple[int, Fraction, Fraction, int, dict[str, Any]]] = []
    for recipe in inputs.recipes:
        eligibility = _eligibility(recipe)
        if eligibility is None:
            continue
        if len(recipe.outputs) != 1:
            continue
        item_id, output_count = recipe.outputs[0]
        if output_count <= 0:
            continue
        output_quote = _fresh_quote(inputs, item_id)
        if output_quote is None:
            continue
        list_price = _target_price(output_quote)
        if list_price <= 0:
            continue
        output_fact = inputs.facts.get(item_id)
        if output_fact is None or output_fact.bonding not in (0, 2, 3):
            continue
        activity = _activity(inputs, item_id, output_quote, list_price)
        result = _opportunity(inputs, recipe, eligibility, output_quote, activity, list_price)
        if result is None:
            continue
        row, tier, flow = result
        profit = Fraction(row["profit"])
        candidates.append((tier, -flow, -profit, recipe.spell_id, row))
    candidates.sort(key=lambda entry: entry[:4])
    return {
        "house": inputs.house,
        "client_build": inputs.client_build,
        "sweep_id": inputs.sweep_id,
        "finished_epoch": inputs.finished_epoch,
        "scan": dict(inputs.scan),
        "profile": {
            "alchemy": ALCHEMY_SKILL,
            "tailoring": TAILORING_SKILL,
            "tailoring_planning_cap": TAILORING_PLANNING_CAP,
            "budget_copper": BUDGET_COPPER,
            "recipes_assumed_owned": True,
        },
        "opportunities": [entry[4] for entry in candidates[:10]],
        "notes": [
            "The 10 g bankroll and recipe ownership are assumptions; "
            "recipe acquisition and leveling costs are excluded.",
            "Cost includes reagent purchases and a full 12-hour listing deposit. "
            "Sale proceeds deduct the house cut; profit assumes every listed output sells.",
            "Market activity is observed listing depletion, not confirmed sales. "
            "Sale probability is unknown; grouped, stale, and long-gap evidence is excluded.",
            "Current listing targets undercut the lowest observed price by one copper. "
            "Tools, categories, and focus requirements are shown per recipe.",
            "Ready-now positive-margin strategies with observed activity rank before unmeasured "
            "trials and marked leveling targets; all skill planning stays within 225.",
            "Recipes with the same cooldown group share one timer; rows are alternatives, not a "
            "combined portfolio forecast.",
        ],
    }


def _load_summary_in_snapshot(conn: Any, inputs: tuple[str, str, int, int, dict[str, int]]) -> dict[str, Any]:
    house_key, build, sweep_id, finished_epoch, scan = inputs
    facts, recipes_all = load_client_data(conn, house_key, build)
    recipes = tuple(recipe for recipe in recipes_all if recipe.professions & {"alchemy", "tailoring"})
    item_ids = sorted({item_id for recipe in recipes for item_id, _ in (*recipe.outputs, *recipe.reagents)})
    start_t = max(0, finished_epoch - 7 * 3600)
    commodity_books = load_commodity_books(
        conn,
        house_key,
        start_t=start_t,
        end_t=finished_epoch,
        client_build=build,
    )
    item_books = load_item_books(
        conn,
        house_key,
        item_ids,
        client_build=build,
        start_t=start_t,
        end_t=finished_epoch,
    )
    names = item_names(conn, build, item_ids)
    return build_trading_summary(
        TradingSummaryInputs(
            house=house_key,
            client_build=build,
            sweep_id=sweep_id,
            finished_epoch=finished_epoch,
            scan=scan,
            recipes=recipes,
            facts=facts,
            commodity_books=commodity_books,
            item_books=item_books,
            names=names,
            rules=rules_for_house(House.parse(house_key)),
        )
    )


def load_trading_summary(conn: Any, house_key: str) -> dict[str, Any]:
    """Load the latest pinned summary in one bounded repeatable-read snapshot."""

    House.parse(house_key)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        latest = conn.execute(
            """
            SELECT ms.id, EXTRACT(EPOCH FROM ms.finished_at)::bigint,
                   ms.client_version || '.' || ms.client_build, ms.num_observations
            FROM market_sweeps AS ms
            JOIN houses AS h ON h.id = ms.house_id
            WHERE h.key = %s
            ORDER BY ms.finished_at DESC, ms.id DESC
            LIMIT 1
            """,
            (house_key,),
        ).fetchone()
        if latest is None:
            raise MarketDataNotFound(f"no market sweep for house {house_key!r}")
        sweep_id, finished_epoch, build, planned = (
            int(latest[0]),
            int(latest[1]),
            str(latest[2]),
            int(latest[3]),
        )
        counts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE query ->> 'kind' = 'queried'),
                   count(*) FILTER (WHERE query ->> 'kind' = 'queried' AND reason_code = 'ok')
            FROM market_observations
            WHERE sweep_id = %s
            """,
            (sweep_id,),
        ).fetchone()
        queried, successful = int(counts[0]), int(counts[1])
        if planned < 0 or queried > planned or successful > queried:
            raise ValueError("latest sweep observation counts violate their bounds")
        scan = {"planned": planned, "queried": queried, "successful": successful}
        key = (house_key, build, sweep_id, PROFILE_KEY)
        return TRADING_SUMMARY_CACHE.get_or_compute(
            key,
            lambda: _load_summary_in_snapshot(conn, (house_key, build, sweep_id, finished_epoch, scan)),
        )


__all__ = [
    "TradingSummaryInputs",
    "build_trading_summary",
    "exact_money",
    "load_trading_summary",
]
