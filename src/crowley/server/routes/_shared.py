from __future__ import annotations

from fastapi import HTTPException

from crowley.core.house import House


def _house_id(conn, key: str) -> int:
    try:
        house = House.parse(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = conn.execute("SELECT id FROM houses WHERE key = %s", (house.key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"no data for house {house.key}")
    return row[0]
