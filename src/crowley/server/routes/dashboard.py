from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse

from crowley.analysis.trading_summary import load_trading_summary
from crowley.core.errors import MarketDataNotFound
from crowley.core.house import House

router = APIRouter()

_STATIC = Path(__file__).parent.parent / "static"
_ADVANCED_DASHBOARD_HTML = (_STATIC / "dashboard.html").read_text(encoding="utf-8")
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


@router.get("/dashboard/advanced", response_class=HTMLResponse)
def advanced_dashboard() -> HTMLResponse:
    return HTMLResponse(_ADVANCED_DASHBOARD_HTML, headers=_SECURITY_HEADERS)


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
