"""Regression coverage for the server's public route table."""

from collections.abc import Iterable
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, _IncludedRouter
from pydantic import ValidationError
from starlette.routing import BaseRoute

from crowley.server.app import app
from crowley.server.deps import require_token
from crowley.server.routes import dashboard as dashboard_routes
from crowley.server.routes.addon import AddonDataRequest, AddonDataResponse

EXPECTED_ROUTES = frozenset(
    {
        ("GET", "/healthz", None),
        ("POST", "/v1/scans", "require_token"),
        ("POST", "/v1/probes", "require_token"),
        ("GET", "/v1/houses", None),
        ("GET", "/v1/houses/{key}/sweeps/latest", None),
        ("GET", "/v1/houses/{key}/trading-summary", None),
        ("GET", "/v1/houses/{key}/recipe-watch", None),
        ("GET", "/v1/scan-plan", "require_token"),
        ("POST", "/v1/addon-data", "require_token"),
        ("GET", "/dashboard", None),
    }
)


def _iter_routes(routes: Iterable[BaseRoute]) -> Iterable[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif isinstance(route, _IncludedRouter):
            yield from _iter_routes(route.original_router.routes)


def test_full_route_table_preserves_methods_paths_and_auth() -> None:
    actual = {
        (
            method,
            route.path,
            "require_token" if any(dep.dependency is require_token for dep in route.dependencies) else None,
        )
        for route in _iter_routes(app.routes)
        for method in route.methods
    }
    assert actual == EXPECTED_ROUTES
    assert len(actual) == len(EXPECTED_ROUTES)


def test_addon_data_contract_excludes_shopping_lists() -> None:
    assert set(AddonDataResponse.model_fields) == {
        "generatedAt",
        "ackedScans",
        "scanPlan",
    }
    with pytest.raises(ValidationError, match="shoppingLists"):
        AddonDataRequest.model_validate({"shoppingLists": []})


def test_dashboard_paths_and_summary_route_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    trading = dashboard_routes.dashboard()
    assert trading.status_code == 200
    assert "Top 10 crafting strategies" in trading.body.decode()
    assert "dashboard/advanced" not in trading.body.decode()

    class Pool:
        def connection(self):
            return nullcontext(object())

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool())))
    monkeypatch.setattr(dashboard_routes, "load_trading_summary", lambda conn, key: {"house": key})
    assert dashboard_routes.trading_summary("us-normal-alliance", request) == {"house": "us-normal-alliance"}
    with pytest.raises(HTTPException) as invalid:
        dashboard_routes.trading_summary("invalid-house", request)
    assert invalid.value.status_code == 400


def test_latest_sweep_route_returns_the_dashboard_scan_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def execute(self, query, parameters):
            assert "ORDER BY finished_at DESC, id DESC" in query
            assert parameters[0] == 17 and parameters[1] == parameters[2]
            return self

        def fetchone(self):
            return (42, 1_800_000_000.5, "1.60.1.70938")

    class Pool:
        def connection(self):
            return nullcontext(Connection())

    monkeypatch.setattr(dashboard_routes, "_house_id", lambda conn, key: 17)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool())))
    assert dashboard_routes.latest_house_sweep("us-normal-alliance", request, "1.60.1.70938") == {
        "house": "us-normal-alliance",
        "sweep_id": 42,
        "finished_epoch": 1_800_000_000.5,
        "client_build": "1.60.1.70938",
    }

    class EmptyConnection(Connection):
        def fetchone(self):
            return None

    class EmptyPool:
        def connection(self):
            return nullcontext(EmptyConnection())

    request.app.state.pool = EmptyPool()
    with pytest.raises(HTTPException) as absent:
        dashboard_routes.latest_house_sweep("us-normal-alliance", request)
    assert absent.value.status_code == 404


def test_recipe_watch_route_is_read_only_public_get(monkeypatch: pytest.MonkeyPatch) -> None:
    class Pool:
        def connection(self):
            return nullcontext(object())

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool())))
    payload = {"watch_id": "draught-of-water-walking", "house": "us-normal-alliance"}
    monkeypatch.setattr(dashboard_routes, "load_recipe_watch", lambda conn, key: payload)
    assert dashboard_routes.recipe_watch("us-normal-alliance", request) == payload
    with pytest.raises(HTTPException) as invalid:
        dashboard_routes.recipe_watch("invalid-house", request)
    assert invalid.value.status_code == 400

    def missing(conn, key):
        raise dashboard_routes.MarketDataNotFound("no market sweep")

    monkeypatch.setattr(dashboard_routes, "load_recipe_watch", missing)
    with pytest.raises(HTTPException) as absent:
        dashboard_routes.recipe_watch("us-normal-alliance", request)
    assert absent.value.status_code == 404
