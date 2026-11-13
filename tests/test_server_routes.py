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
        ("GET", "/v1/houses/{key}/latest", None),
        ("GET", "/v1/houses/{key}/items/{item_key}/history", None),
        ("GET", "/v1/houses/{key}/items/{item_key}/volume", None),
        ("GET", "/v1/houses/{key}/advice", None),
        ("GET", "/v1/houses/{key}/sweeps/latest", None),
        ("GET", "/v1/houses/{key}/trading-summary", None),
        ("POST", "/v1/item-reference", "require_token"),
        ("GET", "/v1/houses/{key}/item-reference", "require_token"),
        ("POST", "/v1/account-state", "require_token"),
        ("GET", "/v1/houses/{key}/account-state/latest", "require_token"),
        ("GET", "/v1/scan-plan", "require_token"),
        ("POST", "/v1/addon-data", "require_token"),
        ("POST", "/v1/queues", "require_token"),
        ("GET", "/v1/houses/{key}/queue/active", "require_token"),
        ("POST", "/v1/executions", "require_token"),
        ("GET", "/v1/paper/runs", None),
        ("GET", "/v1/paper/runs/{name}", None),
        ("GET", "/dashboard", None),
        ("GET", "/dashboard/advanced", None),
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
        "referenceItemIDs",
    }
    with pytest.raises(ValidationError, match="shoppingLists"):
        AddonDataRequest.model_validate({"shoppingLists": []})


def test_dashboard_paths_and_summary_route_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    trading = dashboard_routes.dashboard()
    advanced = dashboard_routes.advanced_dashboard()
    assert trading.status_code == 200
    assert "Top 10 crafting strategies" in trading.body.decode()
    assert advanced.status_code == 200
    assert "Paper runs" in advanced.body.decode()
    assert trading.headers["content-security-policy"] == advanced.headers["content-security-policy"]

    class Pool:
        def connection(self):
            return nullcontext(object())

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool())))
    monkeypatch.setattr(dashboard_routes, "load_trading_summary", lambda conn, key: {"house": key})
    assert dashboard_routes.trading_summary("us-normal-alliance", request) == {"house": "us-normal-alliance"}
    with pytest.raises(HTTPException) as invalid:
        dashboard_routes.trading_summary("invalid-house", request)
    assert invalid.value.status_code == 400
