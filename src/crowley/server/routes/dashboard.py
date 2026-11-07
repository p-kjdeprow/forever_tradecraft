from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import HTMLResponse

router = APIRouter()

_DASHBOARD_HTML = (Path(__file__).parent.parent / "static" / "dashboard.html").read_text(encoding="utf-8")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(
        _DASHBOARD_HTML,
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
