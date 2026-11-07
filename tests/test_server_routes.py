"""Regression coverage for the server's public route table."""

from collections.abc import Iterable

from fastapi.routing import APIRoute, _IncludedRouter
from starlette.routing import BaseRoute

from crowley.server.app import app
from crowley.server.deps import require_token

EXPECTED_ROUTES = frozenset(
    {
        ("GET", "/healthz", None),
        ("POST", "/v1/scans", "require_token"),
        ("POST", "/v1/probes", "require_token"),
        ("GET", "/v1/houses", "require_token"),
        ("GET", "/v1/houses/{key}/latest", "require_token"),
        ("GET", "/v1/houses/{key}/items/{item_key}/history", "require_token"),
        ("GET", "/v1/houses/{key}/items/{item_key}/volume", "require_token"),
        ("POST", "/v1/item-reference", "require_token"),
        ("GET", "/v1/houses/{key}/item-reference", "require_token"),
        ("POST", "/v1/account-state", "require_token"),
        ("GET", "/v1/houses/{key}/account-state/latest", "require_token"),
        ("GET", "/v1/scan-plan", "require_token"),
        ("POST", "/v1/addon-data", "require_token"),
        ("POST", "/v1/queues", "require_token"),
        ("GET", "/v1/houses/{key}/queue/active", "require_token"),
        ("POST", "/v1/executions", "require_token"),
        ("GET", "/v1/paper/runs", "require_token"),
        ("GET", "/v1/paper/runs/{name}", "require_token"),
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
