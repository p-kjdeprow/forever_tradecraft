"""The concurrent, one-market backtest loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from fractions import Fraction
from typing import Any

from crowley.analysis.books import Book
from crowley.core.auction_rules import AuctionHouseRules

from .accounts import Account
from .api import CraftingCapabilities, ItemFacts, Recipe, Strategy
from .execution import ExecutionContext, execute_intent, release_mail, settle_expiry, settle_fills
from .market import Market
from .numbers import as_fraction
from .recipes import validate_recipes
from .report import build_report, text_summary
from .requirements import EMPTY_CAPABILITIES, capabilities_hash, recipe_gate_counts
from .statistics import StatisticsPoint, precompute_statistics


def _allocate_capital(capital: int, weights: Sequence[Any]) -> list[int]:
    if not weights or any(as_fraction(weight) <= 0 for weight in weights):
        raise ValueError("weights must be positive")
    fractions = [as_fraction(weight) for weight in weights]
    total = sum(fractions, Fraction(0))
    shares = [int(Fraction(capital) * weight / total) for weight in fractions]
    remainder = capital - sum(shares)
    for index in range(remainder):
        shares[index % len(shares)] += 1
    return shares


@dataclass
class BacktestResult:
    start_capital: int
    accounts: dict[str, Account]
    equity_curves: dict[str, list[tuple[float, Fraction]]] = field(default_factory=dict)
    total_equity_curve: list[tuple[float, Fraction]] = field(default_factory=list)
    times: tuple[float, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    capacity_observations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    wind_down_checkpoints: dict[str, dict[str, Fraction]] = field(default_factory=dict)
    step_log: list[dict[str, Any]] = field(default_factory=list)
    realized_curves: dict[str, list[tuple[float, Fraction]]] = field(default_factory=dict)
    cash_curves: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    stale_mark_counts: dict[str, int] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        return build_report(self)

    def summary(self) -> str:
        return text_summary(self.report())


def _mark_equity_details(
    account: Account,
    market: Market,
    sweep_index: int,
    snapshots: Mapping[int, Any] | None = None,
    snapshot_history: Mapping[int, tuple[int, Any]] | None = None,
) -> tuple[Fraction, int]:
    snapshots = market.view().items() if snapshots is None else snapshots
    snapshot_history = {} if snapshot_history is None else snapshot_history
    holdings: dict[int, int] = {}
    for item_id, quantity in account.inventory_balance.items():
        holdings[item_id] = holdings.get(item_id, 0) + quantity
    for item_id, quantity in account.listed_units().items():
        holdings[item_id] = holdings.get(item_id, 0) + quantity
    for item_id, quantity in account.in_transit().items():
        holdings[item_id] = holdings.get(item_id, 0) + quantity
    equity = Fraction(account.cash_balance + account.pending_cash())
    stale_mark_count = 0
    for item_id, quantity in holdings.items():
        snapshot = snapshots.get(item_id)
        if snapshot is None:
            historical = snapshot_history.get(item_id)
            if historical is not None:
                historical_step, historical_snapshot = historical
                if historical_step <= sweep_index:
                    snapshot = historical_snapshot
                    stale_mark_count += 1
        if snapshot is not None and snapshot.demand_units_per_day is not None:
            liquid_limit = max(0, int(Fraction(1, 4) * snapshot.demand_units_per_day * 2))
        else:
            liquid_limit = 0
        liquid_quantity = min(quantity, liquid_limit)
        if snapshot is not None and snapshot.fair is not None and snapshot.visible_min is not None:
            liquid_mark = min(snapshot.fair, Fraction(snapshot.visible_min)) * (1 - market.rules.seller_cut)
        elif snapshot is not None and snapshot.visible_min is not None:
            liquid_mark = Fraction(snapshot.visible_min) * (1 - market.rules.seller_cut)
        else:
            liquid_mark = Fraction(0)
        fact = market.facts(item_id)
        vendor_mark = Fraction(max(0, fact.sell_price)) if fact is not None else Fraction(0)
        equity += liquid_quantity * liquid_mark + (quantity - liquid_quantity) * vendor_mark
    return equity, stale_mark_count


def _strategy_capacity_share(strategy: Strategy) -> Fraction:
    params = getattr(strategy, "params", {})
    value = params.get("capacity_share", Fraction(1, 4)) if isinstance(params, Mapping) else Fraction(1, 4)
    return as_fraction(value)


def _serialize_intent(intent: Any) -> dict[str, Any]:
    """Serialize one contract intent without losing its field order or type."""

    if not is_dataclass(intent):
        return {"type": type(intent).__name__}
    if isinstance(intent, type):
        raise TypeError("intent dataclass must be an instance")
    return {"type": type(intent).__name__, **asdict(intent)}


def _decision_entry(account: Account, intent: Any, before_counts: Mapping[str, int]) -> dict[str, Any]:
    """Build a decision record from the execution path's ledger and counters."""

    outcome = "executed"
    if account.ledger and account.ledger[-1].kind == "rejection":
        outcome = account.ledger[-1].reason or "rejected"
    detail: dict[str, Any] = {}
    if type_name := type(intent).__name__:
        if type_name == "Buy":
            detail["filled"] = account.counts.get("units_bought", 0) - before_counts.get("units_bought", 0)
        elif type_name == "Craft":
            detail["times"] = account.counts.get("crafts", 0) - before_counts.get("crafts", 0)
    return {"intent": _serialize_intent(intent), "outcome": outcome, "detail": detail}


def _held_units(account: Account) -> dict[int, int]:
    held: dict[int, int] = {}
    for item_id, quantity in account.inventory_balance.items():
        held[item_id] = held.get(item_id, 0) + max(0, quantity)
    for item_id, quantity in account.listed_units().items():
        held[item_id] = held.get(item_id, 0) + max(0, quantity)
    for item_id, quantity in account.in_transit().items():
        held[item_id] = held.get(item_id, 0) + max(0, quantity)
    return held


def _record_capacity_observations(
    observations: dict[str, list[dict[str, Any]]],
    strategies: Sequence[Strategy],
    accounts: Sequence[Account],
    market: Market,
) -> None:
    current_view = market.view()
    snapshots = current_view.items()
    for strategy, account in zip(strategies, accounts, strict=True):
        held = _held_units(account)
        # Capacity is an auction-house diagnostic. Vendor supplies are excluded
        # even when a corresponding auction-house snapshot happens to exist.
        item_ids = sorted(
            item_id
            for item_id in snapshots
            if (
                (fact := market.facts(item_id)) is None
                or fact.vendor_bundle_size is None
                or fact.vendor_bundle_price is None
            )
        )
        share = _strategy_capacity_share(strategy)
        observations.setdefault(strategy.name, []).extend(
            {
                "item_id": item_id,
                "held": held.get(item_id, 0),
                "t": current_view.t,
                "sweep_index": current_view.sweep_index,
                "demand_units_per_day": (
                    snapshots[item_id].demand_units_per_day if item_id in snapshots else None
                ),
                "capacity_share": share,
            }
            for item_id in item_ids
        )


def run_backtest(
    books: Mapping[int, Sequence[Book]] | Sequence[Book],
    statistics: Mapping[int, Sequence[StatisticsPoint]] | None,
    facts: Mapping[int, ItemFacts],
    recipes: Sequence[Recipe],
    strategies: Sequence[Strategy] | Mapping[str, Strategy],
    weights: Sequence[Any] | Mapping[str, Any] | None,
    capital_copper: int = 200_000,
    start_t: float | None = None,
    end_t: float | None = None,
    wind_down_hours: Any = 24,
    demand_share: Any = 1,
    capabilities: CraftingCapabilities | None = None,
    *,
    rules: AuctionHouseRules,
) -> BacktestResult:
    """Run all strategies through one shared sequence of recorded sweeps."""

    if isinstance(strategies, Mapping):
        strategy_list = list(strategies.values())
    else:
        strategy_list = list(strategies)
    if not strategy_list:
        raise ValueError("at least one strategy is required")
    names = [strategy.name for strategy in strategy_list]
    if len(set(names)) != len(names):
        raise ValueError("strategy names must be unique")
    if capital_copper < 0:
        raise ValueError("capital_copper cannot be negative")
    wind_down_hours = as_fraction(wind_down_hours)
    if wind_down_hours < 0:
        raise ValueError("wind_down_hours cannot be negative")
    demand_share = as_fraction(demand_share)
    if not 0 < demand_share <= 1:
        raise ValueError("demand_share must be in (0, 1]")
    recipes = validate_recipes(recipes)
    if capabilities is None:
        capabilities = EMPTY_CAPABILITIES
    elif not isinstance(capabilities, CraftingCapabilities):
        raise TypeError("capabilities must be CraftingCapabilities")
    if not isinstance(rules, AuctionHouseRules):
        raise TypeError("rules must be AuctionHouseRules")
    indexed_statistics = precompute_statistics(books) if not statistics else statistics
    market = Market(books, indexed_statistics, facts, recipes, rules=rules)
    all_times = market.times
    steps = tuple(
        (step, t)
        for step, t in enumerate(all_times)
        if (start_t is None or t >= start_t) and (end_t is None or t <= end_t)
    )
    if weights is None:
        weight_values = [Fraction(1) for _ in strategy_list]
    elif isinstance(weights, Mapping):
        missing = [name for name in names if name not in weights]
        if missing:
            raise ValueError(f"missing strategy weights: {missing}")
        weight_values = [weights[name] for name in names]
    else:
        weight_values = list(weights)
        if len(weight_values) != len(strategy_list):
            raise ValueError("weights must have one value per strategy")
    weight_values = [as_fraction(weight) for weight in weight_values]
    if any(weight <= 0 for weight in weight_values):
        raise ValueError("weights must be positive")
    capital_shares = _allocate_capital(capital_copper, weight_values)
    shared_cooldowns: dict[str, float] = {}
    accounts = {
        name: Account(
            name,
            frozenset(strategy.professions),
            share,
            share,
            cooldowns=shared_cooldowns,
            crafting_capabilities=capabilities,
        )
        for name, strategy, share in zip(names, strategy_list, capital_shares, strict=True)
    }
    metadata = {
        "capital_copper": capital_copper,
        "window": {"start": start_t, "end": end_t},
        "wind_down_hours": wind_down_hours,
        "demand_share": demand_share,
        "auction_rules_version": rules.version,
        "capabilities": capabilities.canonical,
        "capabilities_hash": capabilities_hash(capabilities),
        "strategy_professions": {
            name: sorted(str(profession) for profession in accounts[name].professions)
            for name in sorted(names)
        },
        "strategy_weights": {name: weight for name, weight in sorted(zip(names, weight_values, strict=True))},
        "recipe_availability": {
            name: recipe_gate_counts(recipes, accounts[name].professions, capabilities)
            for name in sorted(names)
        },
    }
    if not steps:
        return BacktestResult(
            capital_copper,
            accounts,
            {name: [] for name in names},
            [],
            (),
            metadata,
            {name: [] for name in names},
            step_log=[],
            realized_curves={name: [] for name in names},
            cash_curves={name: [] for name in names},
        )
    ordered_accounts = [accounts[name] for name in names]
    purchased: dict[tuple[int, Any], int] = {}
    curves = {name: [] for name in names}
    total_curve: list[tuple[float, Fraction]] = []
    capacity_observations: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    wind_down_checkpoints: dict[str, dict[str, Fraction]] = {}
    step_log: list[dict[str, Any]] = []
    realized_curves: dict[str, list[tuple[float, Fraction]]] = {name: [] for name in names}
    cash_curves: dict[str, list[tuple[float, int]]] = {name: [] for name in names}
    snapshot_history: dict[int, tuple[int, Any]] = {}
    stale_mark_counts: dict[str, int] = {name: 0 for name in names}
    wind_down_start_t = Fraction(steps[-1][1]) - wind_down_hours * 3600
    metadata["wind_down_start_t"] = wind_down_start_t
    metadata["selected_sweep_first"] = {
        "t": int(steps[0][1]),
        "key": str(market.sweep_ids[steps[0][0]]),
    }
    metadata["selected_sweep_last"] = {
        "t": int(steps[-1][1]),
        "key": str(market.sweep_ids[steps[-1][0]]),
    }
    wind_down_active = wind_down_hours > 0
    next_listing_id = 1

    for step, t in steps:
        market.set_step(t, step, purchased)
        context = ExecutionContext(
            market,
            ordered_accounts,
            t,
            step,
            purchased,
            next_listing_id=next_listing_id,
            wind_down=wind_down_active and Fraction(t) >= wind_down_start_t,
            demand_share=demand_share,
            rules=rules,
        )
        settle_fills(context)
        settle_expiry(context)
        release_mail(context)
        if wind_down_active and not wind_down_checkpoints and Fraction(t) >= wind_down_start_t:
            wind_down_checkpoints = {
                name: {
                    "realized": Fraction(account.cash_balance + account.pending_cash())
                    - account.start_capital,
                    "equity": _mark_equity_details(
                        account,
                        market,
                        step,
                        snapshot_history=snapshot_history,
                    )[0],
                }
                for name, account in accounts.items()
            }
        order = list(range(len(strategy_list)))
        rotation = step % len(order)
        order = order[rotation:] + order[:rotation]
        step_decisions = {
            "t": int(t),
            "sweep_key": str(market.sweep_ids[step]),
            "strategies": {name: [] for name in names},
        }
        for strategy_index in order:
            strategy = strategy_list[strategy_index]
            account = ordered_accounts[strategy_index]
            market.set_step(t, step, purchased)
            strategy_view = market.view()
            intents = strategy.decide(strategy_view, account.account_view(t))
            if intents is None:
                continue
            for intent in intents:
                before_counts = dict(account.counts)
                execute_intent(context, account, intent)
                step_decisions["strategies"][strategy.name].append(
                    _decision_entry(account, intent, before_counts)
                )
                next_listing_id = context.next_listing_id
                market.set_step(t, step, purchased)
        current_snapshots = market.view().items()
        is_final_step = step == steps[-1][0]
        for name, account in accounts.items():
            equity, stale_mark_count = _mark_equity_details(
                account,
                market,
                step,
                snapshots=current_snapshots,
                snapshot_history=snapshot_history,
            )
            curves[name].append((t, equity))
            if is_final_step:
                stale_mark_counts[name] = stale_mark_count
            realized_curves[name].append(
                (t, Fraction(account.cash_balance + account.pending_cash() - account.start_capital))
            )
            cash_curves[name].append((t, account.cash_balance))
        for item_id, snapshot in current_snapshots.items():
            snapshot_history[item_id] = (step, snapshot)
        _record_capacity_observations(capacity_observations, strategy_list, ordered_accounts, market)
        total_curve.append((t, sum((curves[name][-1][1] for name in names), Fraction(0))))
        step_log.append(step_decisions)

    return BacktestResult(
        capital_copper,
        accounts,
        curves,
        total_curve,
        tuple(t for _, t in steps),
        metadata,
        capacity_observations=capacity_observations,
        wind_down_checkpoints=wind_down_checkpoints,
        step_log=step_log,
        realized_curves=realized_curves,
        cash_curves=cash_curves,
        stale_mark_counts=stale_mark_counts,
    )


__all__ = ["BacktestResult", "run_backtest"]
