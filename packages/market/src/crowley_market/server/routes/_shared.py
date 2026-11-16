from __future__ import annotations

from crowley_common.house import House
from fastapi import HTTPException

from crowley_market.store.houses import HouseNotFoundError, house_id


def _house_id(conn, key: str) -> int:
    try:
        house = House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return house_id(conn, house)
    except HouseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
