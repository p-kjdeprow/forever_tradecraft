from __future__ import annotations

from pathlib import Path

from crowley_common.errors import MarketDataNotFound
from crowley_common.house import House
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse

from crowley_market.analysis.recipe_watch import load_recipe_watch
from crowley_market.analysis.trading_summary import load_trading_summary

from ._shared import _house_id

router = APIRouter()

_STATIC = Path(__file__).parent.parent / "static"
_TRADING_DASHBOARD_HTML = (_STATIC / "trading-dashboard.html").read_text(encoding="utf-8")
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
}


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(
        _TRADING_DASHBOARD_HTML,
        headers=_SECURITY_HEADERS,
    )


@router.get("/v1/houses/{key}/sweeps/latest")
def latest_house_sweep(key: str, request: Request, build: str | None = None) -> dict[str, object]:
    """Return the latest scan identity used by the main dashboard."""

    with request.app.state.pool.connection() as conn:
        house_id = _house_id(conn, key)
        row = conn.execute(
            """
            SELECT id, EXTRACT(EPOCH FROM finished_at), client_version || '.' || client_build
            FROM market_sweeps
            WHERE house_id = %s
              AND (%s::text IS NULL OR client_version || '.' || client_build = %s)
            ORDER BY finished_at DESC, id DESC
            LIMIT 1
            """,
            (house_id, build, build),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no market sweep for house {key!r} build {build!r}")
    return {
        "house": key,
        "sweep_id": int(row[0]),
        "finished_epoch": float(row[1]),
        "client_build": str(row[2]),
    }


@router.get("/v1/houses/{key}/trading-summary")
def trading_summary(key: str, request: Request) -> dict:
    try:
        House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        with request.app.state.pool.connection() as conn:
            return load_trading_summary(conn, key)
    except MarketDataNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/houses/{key}/recipe-watch")
def recipe_watch(key: str, request: Request) -> dict:
    try:
        House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        with request.app.state.pool.connection() as conn:
            return load_recipe_watch(conn, key)
    except MarketDataNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
