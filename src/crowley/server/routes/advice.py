from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from crowley.advisor.render import advice_payload, plain
from crowley.advisor.service import make_config, run_advice
from crowley.analysis.rank import parse_as_of

from ..deps import Authed
from ._shared import _house_id

router = APIRouter()


def _as_of(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        return parse_as_of(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/houses/{key}/advice", dependencies=[Authed])
def advice(
    key: str,
    request: Request,
    build: str | None = None,
    as_of: str | None = None,
    strategies: str | None = None,
    professions: str | None = None,
    min_profit: int = Query(default=0, ge=0),
    min_volume: float = Query(default=0.0, ge=0),
    share: float = Query(default=0.10, gt=0, le=1),
    horizon_hours: float = 2.0,
    min_sale_prob: float = Query(default=0.5, ge=0, le=1),
    price_basis: Literal["lowest", "target"] = "lowest",
    character: str | None = None,
    limit: int = Query(default=25, ge=1, le=500),
    exclusions: bool = False,
) -> dict[str, Any]:
    """Ranked trading pathways for one house and build, read from the stored scan history.

    The same options and output as ``python -m crowley.advisor --json``.  A call loads the whole
    market state (tens of seconds on the live database); it never writes.
    """

    try:
        config = make_config(
            strategies=strategies,
            professions=professions,
            min_profit=min_profit,
            min_volume=min_volume,
            share=share,
            horizon_hours=horizon_hours,
            min_sale_prob=min_sale_prob,
            price_basis=price_basis,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    moment = _as_of(as_of)
    with request.app.state.pool.connection() as conn:
        _house_id(conn, key)  # 400 for a malformed key, 404 for an unknown house
        conn.commit()  # end the implicit transaction; the state load opens its own read-only one
        try:
            result, state = run_advice(conn, key, config, build=build, as_of=moment, character=character)
        except RuntimeError as exc:
            if not str(exc).startswith(("no market sweep", "no latest client export", "no account")):
                raise  # an invariant failure is a server error, not a missing resource
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return plain(advice_payload(result, dict(state.names), limit, exclusions))
