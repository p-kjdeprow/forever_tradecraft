from __future__ import annotations

from fastapi import APIRouter, Request

from crowley.store.health import check_connection

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    with request.app.state.pool.connection() as conn:
        check_connection(conn)
    return {"status": "ok"}
