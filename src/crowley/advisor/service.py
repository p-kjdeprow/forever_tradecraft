"""One entry point for the CLI and the server route: options in, database read, advice out."""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Any

from crowley.advisor.engine import advise
from crowley.advisor.model import DEFAULT_PROFESSIONS, STRATEGIES, Advice, AdviceConfig
from crowley.advisor.state import MarketState, load_market_state
from crowley.trading.numbers import as_fraction


def csv(text: str | None, label: str) -> frozenset[str] | None:
    """A comma-separated option as a set; None when the option was not given."""

    if text is None:
        return None
    values = frozenset(part.strip() for part in text.split(",") if part.strip())
    if not values:
        raise ValueError(f"{label} needs at least one value")
    return values


def make_config(
    *,
    strategies: str | None = None,
    professions: str | None = None,
    min_profit: int = 0,
    min_volume: float | Fraction = 0,
    share: float | Fraction = Fraction(1, 10),
    horizon_hours: float = 2.0,
    min_sale_prob: float = 0.5,
    price_basis: str = "lowest",
    flip_x: float | Fraction = Fraction(7, 10),
    flip_undercut: float | Fraction = Fraction(12, 100),
) -> AdviceConfig:
    """Build an ``AdviceConfig`` from raw option values; raises ValueError on a bad one."""

    return AdviceConfig(
        strategies=csv(strategies, "strategies") or frozenset(STRATEGIES),
        professions=csv(professions, "professions") or DEFAULT_PROFESSIONS,
        min_profit=min_profit,
        min_volume=as_fraction(min_volume),
        share=as_fraction(share),
        horizon_hours=horizon_hours,
        min_sale_prob=min_sale_prob,
        crafting_price=price_basis,
        flip_x=as_fraction(flip_x),
        flip_undercut=as_fraction(flip_undercut),
    )


def run_advice(
    conn: Any,
    house_key: str,
    config: AdviceConfig,
    *,
    build: str | None = None,
    as_of: datetime | None = None,
    character: str | None = None,
) -> tuple[Advice, MarketState]:
    """Load the state once (read-only) and run every configured evaluator over it."""

    state = load_market_state(
        conn,
        house_key,
        client_build=build,
        as_of=as_of,
        character=character,
        professions=config.professions,
        lookback_hours=config.lookback_hours,
    )
    return advise(state, config), state
