from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from crowley.advisor.model import OptionError
from crowley.advisor.render import advice_payload, plain
from crowley.advisor.service import ADVICE_CACHE, cached_advice, make_config
from crowley.analysis.rank import latest_sweep, parse_as_of
from crowley.core.errors import MarketDataNotFound

from ..deps import Authed
from ._shared import _house_id

router = APIRouter()


def _as_of(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        return parse_as_of(text)
    except ValueError as exc:
        raise OptionError(str(exc)) from exc


@router.get("/v1/houses/{key}/advice", dependencies=[Authed])
def advice(
    key: str,
    request: Request,
    build: str | None = None,
    as_of: str | None = None,
    estimates: str = "forecast",
    strategies: str | None = None,
    professions: str | None = None,
    min_profit: int = Query(default=0, ge=0),
    min_volume: float = Query(default=0.0, ge=0),
    share: float = Query(default=0.10, gt=0, le=1),
    horizon_hours: float = 2.0,
    min_sale_prob: float = Query(default=0.5, ge=0, le=1),
    price_basis: Literal["lowest", "target"] = "lowest",
    max_cold_age_hours: float = Query(default=3.0, gt=0),
    character: str | None = None,
    limit: int = Query(default=25, ge=1, le=500),
    exclusions: bool = False,
) -> dict[str, Any]:
    """Ranked trading pathways for one house and build, read from the stored scan history.

    The same options and output as ``python -m crowley.advisor --json``.  Results are cached per
    (house, build, latest sweep, character, options; ``min_profit``, ``min_volume`` and the display
    options are applied per request): a miss loads the market state (tens of
    seconds), holding a database connection only for that read, and computes outside any
    connection; a hit does no database work beyond finding the latest sweep. ``advisor.service.warm``
    precomputes the default advice in the scan upload background task. Books are pinned to the keyed sweep;
    a character's capture is read at ``as_of`` (default: the latest), so a capture taken after the
    sweep is used, and a cached character entry picks up a newer capture when its TTL (15 minutes)
    expires.

    Errors: an invalid option is a 400, an unknown house, sweep, build, export or character capture
    is a 404, and anything else (a failed invariant) is a 500.
    """

    try:
        config = make_config(
            estimates=estimates,
            strategies=strategies,
            professions=professions,
            min_profit=min_profit,
            min_volume=min_volume,
            share=share,
            horizon_hours=horizon_hours,
            min_sale_prob=min_sale_prob,
            price_basis=price_basis,
            max_cold_age_hours=max_cold_age_hours,
        )
        result = cached_advice(
            request.app.state.pool,
            ADVICE_CACHE,
            key,
            config,
            build=build,
            as_of=_as_of(as_of),
            character=character,
            validate_house=_house_id,  # 400 for a malformed key, 404 for an unknown house
        )
    except OptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MarketDataNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return plain(advice_payload(result.advice, result.names, limit, exclusions))


@router.get("/v1/houses/{key}/sweeps/latest", dependencies=[Authed])
def latest_house_sweep(key: str, request: Request, build: str | None = None) -> dict[str, Any]:
    """Return the latest sweep identity and finish time without computing advice."""

    try:
        with request.app.state.pool.connection() as conn:
            _house_id(conn, key)
            conn.commit()
            sweep_id, finished_epoch, client_build = latest_sweep(conn, key, build, None)
            conn.commit()
    except MarketDataNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "house": key,
        "sweep_id": sweep_id,
        "finished_epoch": finished_epoch,
        "client_build": client_build,
    }
