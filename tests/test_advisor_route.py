"""The advice route: options, cache, connection hygiene and typed error mapping."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from crowley.advisor import advise as real_advise
from crowley.advisor import service
from crowley.advisor.cache import AdviceCache
from crowley.advisor.model import OptionError
from crowley.core.errors import MarketDataNotFound
from crowley.server.config import ServerSettings, get_settings
from crowley.server.deps import require_token
from crowley.server.routes import advice as advice_route
from tests.test_advisor import BUILD, HOUSE, SELLER, make_inputs, state_of


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    def commit(self) -> None:
        pass


class FakePool:
    def __init__(self) -> None:
        self.open = 0
        self.opened = 0

    @contextmanager
    def connection(self):
        self.open += 1
        self.opened += 1
        try:
            yield FakeConn(self)
        finally:
            self.open -= 1


class Harness:
    def __init__(self) -> None:
        self.pool = FakePool()
        self.sweep = 59  # the synthetic books' latest sweep
        self.load_sweep: int | None = None
        self.loads: list[dict] = []
        self.sweeps: list[tuple] = []
        self.houses: list[str] = []
        self.open_during_compute: list[int] = []
        self.fail_load: Exception | None = None
        self.fail_sweep: Exception | None = None
        self.state = state_of(make_inputs([SELLER]), gatherable={1001: ("herb",)}, names={1001: "Peacebloom"})


@pytest.fixture
def harness(monkeypatch):
    h = Harness()
    app = FastAPI()
    app.include_router(advice_route.router)
    app.dependency_overrides[require_token] = lambda: None
    app.state.pool = h.pool

    def latest_sweep(conn, key, build, as_of):
        h.sweeps.append((key, build, as_of))
        if h.fail_sweep:
            raise h.fail_sweep
        return h.sweep, 1_000_000.0, build or BUILD

    def load_market_state(conn, key, **kwargs):
        h.loads.append({"key": key, **kwargs})
        if h.fail_load:
            raise h.fail_load
        return replace(h.state, inputs=replace(h.state.inputs, latest_sweep_id=h.load_sweep or h.sweep))

    def advise(state, config):
        h.open_during_compute.append(h.pool.open)  # CPU work must hold no connection
        return real_advise(state, config)

    monkeypatch.setattr(service, "latest_sweep", latest_sweep)
    monkeypatch.setattr(advice_route, "latest_sweep", latest_sweep)
    monkeypatch.setattr(service, "load_market_state", load_market_state)
    monkeypatch.setattr(service, "advise", advise)
    monkeypatch.setattr(advice_route, "_house_id", lambda conn, key: h.houses.append(key))
    monkeypatch.setattr(advice_route, "ADVICE_CACHE", AdviceCache())
    h.client = TestClient(app, raise_server_exceptions=False)
    return h


def get(h: Harness, **params):
    return h.client.get(f"/v1/houses/{HOUSE}/advice", params={"strategies": "raw_materials", **params})


def test_route_passes_the_options_and_returns_the_cli_json(harness):
    response = get(
        harness,
        build=BUILD,
        as_of="2026-11-09T12:00:00+00:00",
        professions="alchemy",
        min_profit=1,
        share=0.2,
        horizon_hours=6,
        character="Bob",
        limit=3,
        exclusions="true",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert harness.houses == [HOUSE]
    (load,) = harness.loads
    assert (load["key"], load["character"], load["client_build"]) == (HOUSE, "Bob", BUILD)
    assert load["professions"] == {"alchemy"}
    # the load is pinned to the keyed sweep, not "latest at the time of the load"
    assert load["as_of"].timestamp() == pytest.approx(1_000_000.0, abs=0.01)
    assert harness.sweeps[0][2].isoformat() == "2026-11-09T12:00:00+00:00"
    assert body["house"] == HOUSE and body["parameters"]["strategies"] == ["raw_materials"]
    assert body["parameters"]["share"] == pytest.approx(0.2) and body["parameters"]["horizon_hours"] == 6
    assert [p["key"] for p in body["sections"]["farming"]] == ["raw_materials:1001"]
    assert body["sections"]["ranked"] == []
    assert "exclusions" in body["strategies"]["raw_materials"]


def test_no_database_connection_is_held_during_the_cpu_work(harness):
    assert get(harness).status_code == 200
    assert harness.open_during_compute == [0]
    assert harness.pool.open == 0
    assert harness.pool.opened == 2  # one visit to find the sweep, one to load the state


def test_second_identical_request_is_served_from_the_cache(harness):
    first = get(harness).json()
    assert get(harness).json() == first
    assert len(harness.loads) == 1 and harness.open_during_compute == [0]
    # presentation options do not recompute
    assert get(harness, limit=1, exclusions="true").status_code == 200
    assert len(harness.loads) == 1
    # a cache hit still only looks up the sweep: no state load
    assert harness.pool.opened == len(harness.sweeps) + len(harness.loads)


def test_cache_key_is_sweep_and_options(harness):
    get(harness)
    get(harness, share=0.5)  # different options
    assert len(harness.loads) == 2
    harness.sweep = 8  # a new sweep has landed
    get(harness)
    assert len(harness.loads) == 3
    get(harness, character="Bob")
    assert len(harness.loads) == 4
    get(harness)  # the sweep-8 default entry is still there
    assert len(harness.loads) == 4


def test_warm_populates_the_cache_for_the_default_advice(harness):
    cache = AdviceCache()
    harness.state = state_of(make_inputs([SELLER]), gatherable={1001: ("herb",)})
    config = service.make_config(strategies="raw_materials")
    warmed = service.warm(harness.pool, cache, HOUSE, config=config)
    assert len(cache) == 1 and warmed.advice.house_key == harness.state.house_key
    again = service.cached_advice(harness.pool, cache, HOUSE, config)
    assert again.advice == warmed.advice and len(harness.loads) == 1


@pytest.mark.parametrize(
    "params",
    [{"strategies": "bogus"}, {"horizon_hours": 3}, {"as_of": "2026-11-09T12:00:00"}],
)
def test_bad_options_are_400(harness, params):
    response = harness.client.get(f"/v1/houses/{HOUSE}/advice", params=params)
    assert response.status_code == 400
    assert harness.loads == []  # nothing was read for an invalid request


def test_query_ranges_are_validated(harness):
    assert get(harness, share=0).status_code == 422
    assert get(harness, limit=0).status_code == 422


def test_unknown_sweep_build_or_capture_is_404(harness):
    harness.fail_sweep = MarketDataNotFound("no market sweep for house 'x'")
    response = get(harness)
    assert response.status_code == 404 and "no market sweep" in response.json()["detail"]
    harness.fail_sweep = None
    harness.fail_load = MarketDataNotFound("no account capture for character 'Bob'")
    assert get(harness, character="Bob").status_code == 404


def test_latest_sweep_route_returns_only_latest_metadata_and_uses_one_connection(harness):
    response = harness.client.get(f"/v1/houses/{HOUSE}/sweeps/latest", params={"build": BUILD})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "house": HOUSE,
        "sweep_id": harness.sweep,
        "finished_epoch": 1_000_000.0,
        "client_build": BUILD,
    }
    assert harness.houses == [HOUSE]
    assert harness.sweeps == [(HOUSE, BUILD, None)]
    assert harness.pool.opened == 1 and harness.pool.open == 0
    assert harness.loads == []


def test_latest_sweep_route_maps_missing_sweep_house_errors_and_malformed_key(harness, monkeypatch):
    harness.fail_sweep = MarketDataNotFound("no market sweep")
    response = harness.client.get(f"/v1/houses/{HOUSE}/sweeps/latest")
    assert response.status_code == 404 and response.json()["detail"] == "no market sweep"

    def validate_house(conn, key):
        if key == "malformed":
            raise HTTPException(status_code=400, detail="bad house key")
        if key == "unknown":
            raise HTTPException(status_code=404, detail="unknown house")
        return 1

    monkeypatch.setattr(advice_route, "_house_id", validate_house)
    malformed = harness.client.get("/v1/houses/malformed/sweeps/latest")
    unknown = harness.client.get("/v1/houses/unknown/sweeps/latest")
    assert malformed.status_code == 400
    assert unknown.status_code == 404


def test_latest_sweep_route_requires_authentication(harness, monkeypatch):
    overrides = harness.client.app.dependency_overrides
    overrides.pop(require_token)
    monkeypatch.setitem(
        overrides,
        get_settings,
        lambda: ServerSettings(database_url="postgresql://unused", api_token="test-token", host="test"),
    )
    response = harness.client.get(f"/v1/houses/{HOUSE}/sweeps/latest")
    assert response.status_code == 401
    assert harness.pool.opened == 0


def test_unknown_profession_from_the_loader_is_400(harness):
    harness.fail_load = OptionError("no client recipes for profession(s) ['jewelcrafting']")
    response = get(harness, professions="jewelcrafting")
    assert response.status_code == 400 and "jewelcrafting" in response.json()["detail"]


@pytest.mark.parametrize(
    "error", [RuntimeError("no market sweep, but a bug"), ValueError("bad row"), KeyError("x")]
)
def test_other_errors_are_not_mapped_they_are_500s(harness, error):
    harness.fail_load = error  # a plain RuntimeError that merely sounds like a missing sweep is still a bug
    assert get(harness).status_code == 500
    assert harness.pool.open == 0  # and the connection was released
    harness.fail_load = None
    harness.fail_sweep = error
    assert get(harness).status_code == 500


def test_failed_computation_is_not_cached(harness):
    harness.fail_load = RuntimeError("boom")
    assert get(harness).status_code == 500
    harness.fail_load = None
    assert get(harness).status_code == 200
    assert len(harness.loads) == 2


def test_capture_is_read_at_the_callers_as_of_not_the_keyed_sweep(harness):
    assert get(harness, character="Bob").status_code == 200
    (load,) = harness.loads
    # books are pinned to the keyed sweep; the capture is not: no as_of means the latest capture
    assert load["account_follows_as_of"] is False and load["account_as_of"] is None
    assert load["as_of"].timestamp() == pytest.approx(1_000_000.0, abs=0.01)
    when = "2026-11-09T12:00:00+00:00"
    assert get(harness, character="Bob", as_of=when).status_code == 200
    assert harness.loads[-1]["account_as_of"] == datetime(2026, 11, 9, 12, tzinfo=UTC)


def test_a_loaded_sweep_that_is_not_the_keyed_sweep_is_a_500(harness):
    harness.load_sweep = 99
    assert get(harness).status_code == 500


def test_as_of_values_resolving_to_one_sweep_share_a_cache_entry(harness):
    assert get(harness).status_code == 200
    assert get(harness, as_of="2026-11-09T12:00:00+00:00").status_code == 200
    assert get(harness, as_of="2026-11-09T13:00:00+00:00").status_code == 200
    assert len(harness.loads) == 1
    # with a character the capture depends on as_of, so the entries are separate
    get(harness, character="Bob", as_of="2026-11-09T12:00:00+00:00")
    get(harness, character="Bob", as_of="2026-11-09T13:00:00+00:00")
    get(harness, character="Bob", as_of="2026-11-09T13:00:00+00:00")
    assert len(harness.loads) == 3


def test_min_profit_and_min_volume_are_applied_per_request_not_cached(harness):
    everything = get(harness).json()
    strict = get(harness, min_profit=10**9).json()
    assert len(harness.loads) == 1
    assert everything["section_totals"]["farming"] > 0 and strict["section_totals"]["farming"] == 0
    assert (
        strict["strategies"]["raw_materials"]["exclusion_counts"]["below_min_profit"]
        == (everything["section_totals"]["farming"])
    )
    assert (
        strict["strategies"]["raw_materials"]["considered"]
        == everything["strategies"]["raw_materials"]["considered"]
    )
    assert get(harness, min_volume=10**9).json()["section_totals"]["farming"] == 0
    assert get(harness).json() == everything  # the cached unfiltered evaluation is untouched
    assert len(harness.loads) == 1
