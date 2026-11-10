"""One entry point for the CLI and the server route: options in, database read, advice out."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any, Protocol

from crowley.advisor.cache import AdviceCache
from crowley.advisor.engine import advise
from crowley.advisor.model import DEFAULT_PROFESSIONS, STRATEGIES, Advice, AdviceConfig, OptionError
from crowley.advisor.state import MarketState, load_market_state
from crowley.analysis.rank import latest_sweep
from crowley.trading.numbers import as_fraction


def csv(text: str | None, label: str) -> frozenset[str] | None:
    """A comma-separated option as a set; None when the option was not given."""

    if text is None:
        return None
    values = frozenset(part.strip() for part in text.split(",") if part.strip())
    if not values:
        raise OptionError(f"{label} needs at least one value")
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
    max_cold_age_hours: float = 3.0,
    flip_x: float | Fraction = Fraction(7, 10),
    flip_undercut: float | Fraction = Fraction(12, 100),
) -> AdviceConfig:
    """Build an ``AdviceConfig`` from raw option values; raises ``OptionError`` on a bad one."""

    return AdviceConfig(
        strategies=csv(strategies, "strategies") or frozenset(STRATEGIES),
        professions=csv(professions, "professions") or DEFAULT_PROFESSIONS,
        min_profit=min_profit,
        min_volume=as_fraction(min_volume),
        share=as_fraction(share),
        horizon_hours=horizon_hours,
        min_sale_prob=min_sale_prob,
        crafting_price=price_basis,
        max_cold_age_hours=max_cold_age_hours,
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


@dataclass(frozen=True)
class CachedAdvice:
    advice: Advice
    names: dict[int, str]


class ConnectionSource(Protocol):
    """A connection pool: ``connection()`` is a context manager yielding a connection."""

    def connection(self) -> AbstractContextManager[Any]: ...


def cached_advice(
    pool: ConnectionSource,
    cache: AdviceCache[CachedAdvice],
    house_key: str,
    config: AdviceConfig,
    *,
    build: str | None = None,
    as_of: datetime | None = None,
    character: str | None = None,
    validate_house: Any = None,
) -> CachedAdvice:
    """Advice for the latest sweep (at or before ``as_of``), from the cache or computed once.

    Two short database visits and no CPU work under a connection: one resolves the latest sweep
    (the cache key), and on a miss one loads the state and releases the connection before the
    evaluators run.  ``validate_house(conn, key)`` lets the caller check the house first.
    """

    with pool.connection() as conn:
        if validate_house is not None:
            validate_house(conn, house_key)
        conn.commit()
        sweep_id, finished_t, resolved_build = latest_sweep(conn, house_key, build, as_of)
        conn.commit()
    key = (house_key, resolved_build, sweep_id, character, config)

    def compute() -> CachedAdvice:
        # Pin the load to the sweep that was keyed, whatever has been ingested since.
        pinned = datetime.fromtimestamp(finished_t, UTC) + timedelta(milliseconds=1)
        with pool.connection() as conn:
            state = load_market_state(
                conn,
                house_key,
                client_build=resolved_build,
                as_of=pinned,
                character=character,
                professions=config.professions,
                lookback_hours=config.lookback_hours,
            )
        return CachedAdvice(advise(state, config), dict(state.names))

    return cache.get_or_compute(key, compute)


def warm(
    pool: ConnectionSource,
    cache: AdviceCache[CachedAdvice],
    house_key: str,
    *,
    build: str | None = None,
    config: AdviceConfig | None = None,
) -> CachedAdvice:
    """Compute the default advice (or ``config``'s) for the house's latest sweep into ``cache``.

    Intended to be called after a sweep upload is ingested, so the next request is a hit.
    """

    return cached_advice(pool, cache, house_key, make_config() if config is None else config, build=build)


ADVICE_CACHE: AdviceCache[CachedAdvice] = AdviceCache()
