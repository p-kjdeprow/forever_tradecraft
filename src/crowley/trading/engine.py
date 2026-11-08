"""The concurrent, one-market backtest loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

from crowley.analysis.books import Book, Interval
from crowley.core.auction_rules import AuctionHouseRules

from .accounts import Account, LedgerEvent
from .api import CraftingCapabilities, ItemFacts, ItemSnapshot, Recipe, Strategy
from .execution import ExecutionContext, execute_intent, release_mail, settle_expiry, settle_fills
from .market import Market
from .numbers import FractionInput, as_fraction
from .recipes import validate_recipes
from .report import build_report, text_summary
from .requirements import EMPTY_CAPABILITIES, capabilities_hash, recipe_gate_counts
from .statistics import (
    StatisticsAccumulator,
    StatisticsAccumulatorSeries,
    StatisticsPoint,
    prepare_statistics,
)


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
    engine_states: list[EngineState] = field(default_factory=list)
    ledger_records: list[dict[str, list[LedgerEvent]]] = field(default_factory=list)
    trade_records: list[dict[str, list[dict[str, object]]]] = field(default_factory=list)
    purchase_records: list[dict[tuple[int, int | str], int]] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return build_report(self)

    def summary(self) -> str:
        return text_summary(self.report())


@dataclass
class EngineState:
    """Mutable engine state captured at the end of one completed step."""

    accounts: list[Account]
    purchased: dict[tuple[int, int | str], int]
    next_listing_id: int
    snapshot_history: dict[int, tuple[int, ItemSnapshot]]
    strategy_states: dict[str, Mapping[str, object]] = field(default_factory=dict)
    statistics_accumulators: dict[int, StatisticsAccumulator] = field(default_factory=dict)
    replay_ordinal: int = -1
    step_record: Mapping[str, object] | None = None


def _mark_equity_details(
    account: Account,
    market: Market,
    sweep_index: int,
    snapshots: Mapping[int, Any] | None = None,
    snapshot_history: Mapping[int, tuple[int, ItemSnapshot]] | None = None,
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
    params = strategy.params
    if not isinstance(params, Mapping):
        raise TypeError(f"strategy {strategy.name!r} params must be a mapping")
    value = params.get("capacity_share", Fraction(1, 4))
    if isinstance(value, bool) or not isinstance(value, (Fraction, int, float, Decimal, str)):
        raise TypeError(f"strategy {strategy.name!r} capacity_share must be numeric")
    return as_fraction(cast(FractionInput, value))


def _checkpoint_strategies(strategies: Sequence[Strategy]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for strategy in strategies:
        try:
            checkpoint = strategy.checkpoint
            restore = strategy.restore
        except AttributeError as exc:
            raise TypeError(f"strategy {strategy.name!r} does not implement checkpoint/restore") from exc
        if not callable(checkpoint) or not callable(restore):
            raise TypeError(f"strategy {strategy.name!r} checkpoint/restore must be callable")
        state = checkpoint()
        if not isinstance(state, Mapping):
            raise TypeError(f"strategy {strategy.name!r} checkpoint must be a mapping")
        result[strategy.name] = deepcopy(dict(state))
    return result


def _restore_strategies(strategies: Sequence[Strategy], states: Mapping[str, Mapping[str, object]]) -> None:
    expected = {strategy.name for strategy in strategies}
    if set(states) != expected:
        raise ValueError("engine state strategy checkpoint names do not match strategies")
    for strategy in strategies:
        try:
            strategy.restore(states[strategy.name])
        except AttributeError as exc:
            raise TypeError(f"strategy {strategy.name!r} does not implement checkpoint/restore") from exc


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
    demand_by_item = market.demand_at()
    for strategy, account in zip(strategies, accounts, strict=True):
        held = _held_units(account)
        # Capacity is an auction-house diagnostic. Vendor supplies are excluded
        # even when a corresponding auction-house snapshot happens to exist.
        item_ids = sorted(
            item_id
            for item_id in demand_by_item
            if (
                (fact := market.facts(item_id)) is None
                or fact.vendor_bundle_size is None
                or fact.vendor_bundle_price is None
            )
        )
        share = _strategy_capacity_share(strategy)
        by_item = {
            int(observation["item_id"]): observation
            for observation in observations.setdefault(strategy.name, [])
        }
        for item_id in item_ids:
            observation = {
                "item_id": item_id,
                "held": held.get(item_id, 0),
                "t": current_view.t,
                "replay_ordinal": current_view.replay_ordinal,
                "demand_units_per_day": demand_by_item[item_id],
                "capacity_share": share,
            }
            demand = observation["demand_units_per_day"]
            capacity = max(1, int(share * demand)) if demand is not None else 1
            ratio = Fraction(int(observation["held"]), capacity)
            previous = by_item.get(item_id)
            if previous is None:
                by_item[item_id] = observation
                continue
            previous_demand = previous["demand_units_per_day"]
            previous_capacity = max(1, int(share * previous_demand)) if previous_demand is not None else 1
            previous_ratio = Fraction(int(previous["held"]), previous_capacity)
            if ratio > previous_ratio:
                by_item[item_id] = observation
        observations[strategy.name] = list(by_item.values())


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
    initial_state: EngineState | None = None,
    capture_states: bool = False,
    capture_state_history: bool = True,
    intervals: Mapping[int, Sequence[Interval | None]] | None = None,
    accumulator_series: Mapping[int, StatisticsAccumulatorSeries | Sequence[StatisticsAccumulator]]
    | None = None,
    ordinal_offset: int | None = None,
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
    _checkpoint_strategies(strategy_list)
    if initial_state is not None:
        _restore_strategies(strategy_list, initial_state.strategy_states)
    precomputation = None
    if not statistics or (capture_states and accumulator_series is None):
        precomputation = prepare_statistics(
            books,
            initial_accumulators=initial_state.statistics_accumulators if initial_state is not None else None,
        )
    indexed_statistics = precomputation.statistics if precomputation is not None else statistics
    market = Market(
        books,
        indexed_statistics,
        facts,
        recipes,
        rules=rules,
        intervals=intervals
        if intervals is not None
        else (None if precomputation is None else precomputation.intervals),
        accumulator_series=(
            accumulator_series
            if accumulator_series is not None
            else (None if precomputation is None else precomputation.accumulator_series)
        ),
    )
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
        if initial_state is not None:
            state_accounts = {account.name: account for account in initial_state.accounts}
            if set(state_accounts) != set(names):
                raise ValueError("initial engine state strategy names do not match strategies")
            accounts = state_accounts
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
            engine_states=[],
            ledger_records=[],
            trade_records=[],
            purchase_records=[],
        )
    if initial_state is None:
        ordered_accounts = [accounts[name] for name in names]
        purchased: dict[tuple[int, int | str], int] = {}
        next_listing_id = 1
        snapshot_history: dict[int, tuple[int, ItemSnapshot]] = {}
    else:
        if len(initial_state.accounts) != len(names):
            raise ValueError("initial engine state account count does not match strategies")
        state_accounts = {account.name: account for account in initial_state.accounts}
        if set(state_accounts) != set(names):
            raise ValueError("initial engine state strategy names do not match strategies")
        ordered_accounts = [state_accounts[name] for name in names]
        accounts = state_accounts
        purchased = dict(initial_state.purchased)
        next_listing_id = initial_state.next_listing_id
        snapshot_history = dict(initial_state.snapshot_history)
        strategy_states = dict(initial_state.strategy_states)
        statistics_accumulators = dict(initial_state.statistics_accumulators)
        previous_replay_ordinal = initial_state.replay_ordinal
    if initial_state is None:
        strategy_states = {}
        statistics_accumulators = {}
        previous_replay_ordinal = -1
    curves = {name: [] for name in names}
    total_curve: list[tuple[float, Fraction]] = []
    capacity_observations: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    wind_down_checkpoints: dict[str, dict[str, Fraction]] = {}
    step_log: list[dict[str, Any]] = []
    realized_curves: dict[str, list[tuple[float, Fraction]]] = {name: [] for name in names}
    cash_curves: dict[str, list[tuple[float, int]]] = {name: [] for name in names}
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
    engine_states: list[EngineState] = []
    ledger_records: list[dict[str, list[LedgerEvent]]] = []
    trade_records: list[dict[str, list[dict[str, Any]]]] = []
    purchase_records: list[dict[tuple[int, int | str], int]] = []
    if ordinal_offset is not None:
        base_ordinal = ordinal_offset
    elif initial_state is not None:
        base_ordinal = initial_state.replay_ordinal + 1
    else:
        base_ordinal = 0

    for selected_position, (step, t) in enumerate(steps):
        replay_ordinal = base_ordinal + selected_position
        if initial_state is not None and replay_ordinal <= previous_replay_ordinal:
            raise ValueError("replay ordinal does not advance beyond the checkpoint")
        market.prune_purchases(purchased, step)
        market.set_step(t, step, purchased, replay_ordinal=replay_ordinal)
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
            replay_ordinal=replay_ordinal,
        )
        ledger_starts = {account.name: len(account.ledger) for account in ordered_accounts}
        trade_starts = {account.name: len(account.closed_trades) for account in ordered_accounts}
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
                        replay_ordinal,
                        snapshot_history=snapshot_history,
                    )[0],
                }
                for name, account in accounts.items()
            }
        order = list(range(len(strategy_list)))
        rotation = replay_ordinal % len(order)
        order = order[rotation:] + order[:rotation]
        step_decisions = {
            "t": int(t),
            "replay_ordinal": replay_ordinal,
            "sweep_key": str(market.sweep_ids[step]),
            "strategies": {name: [] for name in names},
        }
        for strategy_index in order:
            strategy = strategy_list[strategy_index]
            account = ordered_accounts[strategy_index]
            market.set_step(t, step, purchased, replay_ordinal=replay_ordinal)
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
                market.set_step(t, step, purchased, replay_ordinal=replay_ordinal)
        held_item_ids = sorted({item_id for account in accounts.values() for item_id in _held_units(account)})
        current_snapshots = market.snapshots_for(held_item_ids)
        is_final_step = step == steps[-1][0]
        for name, account in accounts.items():
            equity, stale_mark_count = _mark_equity_details(
                account,
                market,
                replay_ordinal,
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
        snapshot_history = {
            item_id: value for item_id, value in snapshot_history.items() if item_id in held_item_ids
        }
        for item_id, snapshot in current_snapshots.items():
            snapshot_history[item_id] = (replay_ordinal, snapshot)
        _record_capacity_observations(capacity_observations, strategy_list, ordered_accounts, market)
        total_curve.append((t, sum((curves[name][-1][1] for name in names), Fraction(0))))
        ledger_records.append(
            {name: list(account.ledger[ledger_starts[name] :]) for name, account in accounts.items()}
        )
        trade_records.append(
            {
                name: deepcopy(account.closed_trades[trade_starts[name] :])
                for name, account in accounts.items()
            }
        )
        purchase_records.append(dict(context.purchase_deltas))
        step_log.append(step_decisions)
        if capture_states:
            strategy_states = _checkpoint_strategies(strategy_list)
            statistics_accumulators.update(market.accumulators_at())
            state = EngineState(
                accounts=deepcopy(ordered_accounts),
                purchased=dict(purchased),
                next_listing_id=next_listing_id,
                snapshot_history=deepcopy(snapshot_history),
                strategy_states=deepcopy(strategy_states),
                statistics_accumulators=deepcopy(statistics_accumulators),
                replay_ordinal=replay_ordinal,
                step_record={
                    "t": int(t),
                    "sweep_key": str(market.sweep_ids[step]),
                    "equity": {
                        **{name: curves[name][-1][1] for name in names},
                        "total": sum((curves[name][-1][1] for name in names), Fraction(0)),
                    },
                    "realized": {
                        **{name: realized_curves[name][-1][1] for name in names},
                        "total": sum((realized_curves[name][-1][1] for name in names), Fraction(0)),
                    },
                    "cash": {
                        **{name: cash_curves[name][-1][1] for name in names},
                        "total": sum((cash_curves[name][-1][1] for name in names), 0),
                    },
                    "intents": step_decisions["strategies"],
                },
            )
            if capture_state_history:
                engine_states.append(state)
            else:
                engine_states[:] = [state]

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
        engine_states=engine_states,
        ledger_records=ledger_records,
        trade_records=trade_records,
        purchase_records=purchase_records,
    )


__all__ = ["BacktestResult", "EngineState", "run_backtest"]
