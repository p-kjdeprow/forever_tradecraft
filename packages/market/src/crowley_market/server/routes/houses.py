from __future__ import annotations

from fastapi import APIRouter, Request

from crowley_market.store.houses import list_houses as store_list_houses

router = APIRouter()


@router.get("/v1/houses")
def list_houses(request: Request) -> list[dict[str, object]]:
    with request.app.state.pool.connection() as conn:
        return store_list_houses(conn)
