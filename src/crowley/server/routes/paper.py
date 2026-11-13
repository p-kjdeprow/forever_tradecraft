from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crowley.store.paper import list_paper_runs as store_list_paper_runs
from crowley.store.paper import paper_run_detail

router = APIRouter()


_PAPER_COPPER_MAPS = frozenset(("equity", "realized", "cash"))
_PAPER_COPPER_FIELDS = frozenset(("max_unit_price", "unit_price"))


def _paper_copper_string(value: Any) -> str:
    if isinstance(value, bool):
        raise TypeError("paper copper values cannot be boolean")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported paper copper value {value!r}")


def _paper_json(value: Any, *, key: str | None = None) -> Any:
    if key in _PAPER_COPPER_MAPS:
        if not isinstance(value, dict):
            raise TypeError(f"paper {key} must be an object")
        return {name: _paper_copper_string(amount) for name, amount in value.items()}
    if key == "capital_copper" or key in _PAPER_COPPER_FIELDS or (key and key.endswith("_copper")):
        return _paper_copper_string(value)
    if isinstance(value, dict):
        return {name: _paper_json(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [_paper_json(item) for item in value]
    return value


def _paper_run_payload(row: dict[str, Any]) -> dict[str, Any]:
    latest_equity = row["latest_equity"]
    latest_realized = row["latest_realized"]
    return {
        "name": row["name"],
        "house": row["house"],
        "client_build": row["client_build"],
        "start_t": row["start_t"],
        "capital_copper": _paper_copper_string(row["capital_copper"]),
        "strategies": row["strategies"],
        "weights": row["weights"],
        "demand_share": row["demand_share"],
        "created_at": row["created_at"],
        "latest_step_t": row["latest_step_t"],
        "latest_total_equity": None
        if latest_equity is None
        else _paper_copper_string(latest_equity.get("total")),
        "latest_total_realized": None
        if latest_realized is None
        else _paper_copper_string(latest_realized.get("total")),
        "pending_step_t": row["pending_step_t"],
    }


@router.get("/v1/paper/runs")
def list_paper_runs(request: Request) -> list[dict[str, Any]]:
    with request.app.state.pool.connection() as conn:
        rows = store_list_paper_runs(conn)
    return [_paper_run_payload(row) for row in rows]


@router.get("/v1/paper/runs/{name}")
def paper_run(name: str, request: Request) -> dict[str, Any]:
    with request.app.state.pool.connection() as conn:
        with conn.transaction():
            run, curve_rows, intent_rows, pending = paper_run_detail(conn, name)
            if run is None:
                raise HTTPException(status_code=404, detail="unknown paper run")

    payload = _paper_run_payload(run)
    payload.update(
        {
            "curve": [
                {
                    "t": row["step_t"],
                    "equity": _paper_json(row["equity"], key="equity"),
                    "realized": _paper_json(row["realized"], key="realized"),
                    "cash": _paper_json(row["cash"], key="cash"),
                }
                for row in curve_rows
            ],
            "recent_intents": [
                {
                    "t": row["t"],
                    "strategy": row["strategy"],
                    "ordinal": row["ordinal"],
                    "intent": _paper_json(row["intent"]),
                    "outcome": row["outcome"],
                    "detail": _paper_json(row["detail"]),
                }
                for row in intent_rows
            ],
            "pending": None if pending is None else _paper_json(dict(pending)),
        }
    )
    return payload
