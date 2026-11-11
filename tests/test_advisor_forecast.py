"""Forecast estimate-provider integration and snapshot compatibility tests."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from crowley.advisor import AdviceConfig, advise, service
from crowley.advisor.cache import AdviceCache
from crowley.advisor.estimates import ForecastEstimates, SnapshotEstimates
from crowley.advisor.model import Advice, StrategyResult
from crowley.advisor.render import advice_payload, plain
from crowley.advisor.state import MarketState
from crowley.analysis.books import Book, Group
from crowley.analysis.item_books import Auction, ItemBook
from crowley.server.routes import advice as advice_route
from tests.test_advisor import BUILD, HOUSE, SELLER, make_inputs, state_of
from tests.test_advisor_route import Harness


def _commodity_state(books: dict[int, list[Book]], *, as_of_t: float, sweep_id: int) -> MarketState:
    return state_of(
        make_inputs([], books=books, facts={}, as_of_t=as_of_t, latest_sweep_id=sweep_id),
    )


def _hourly_books(item_id: int, prices: dict[int, int], *, end: int) -> list[Book]:
    return [
        Book(
            float(index * 3600),
            [Group("level", Fraction(prices[index]), 10, 100_000, 1)],
            None,
            item_id,
            index,
        )
        for index in range(min(prices), end + 1)
    ]


def test_one_book_forecast_keeps_snapshot_values_exactly() -> None:
    item_id = 7001
    book = Book(0.0, [Group("one", Fraction(123), 4, 100_000, 1)], None, item_id, 1)
    state = _commodity_state({item_id: [book]}, as_of_t=0.0, sweep_id=1)
    snapshot = SnapshotEstimates(state).estimate(item_id)
    forecast = ForecastEstimates(state, AdviceConfig(estimates="forecast")).estimate(item_id)
    assert snapshot is not None and forecast is not None
    assert forecast.lowest == snapshot.lowest == 123
    assert forecast.sale_price == snapshot.lowest
    assert forecast.units_per_hour == snapshot.units_per_hour is None
    assert forecast.demand_intervals == snapshot.demand_intervals == 0
    assert forecast.covered_hours == snapshot.covered_hours == 0.0
    assert forecast.forecast is not None
    assert forecast.forecast.price_model == "last"


def test_trending_item_chooses_trend_and_moves_sale_price() -> None:
    item_id = 7002
    books = [
        Book(
            float(index * 3600),
            [Group("level", Fraction(100 + index * 2), 10, 100_000, 1)],
            None,
            item_id,
            index,
        )
        for index in range(121)
    ]
    state = _commodity_state({item_id: books}, as_of_t=120 * 3600.0, sweep_id=120)
    estimate = ForecastEstimates(state, AdviceConfig(estimates="forecast")).estimate(item_id)
    assert estimate is not None and estimate.forecast is not None
    assert estimate.forecast.price_model == "trend_24h"
    assert estimate.sale_price is not None and estimate.lowest is not None
    assert estimate.sale_price > estimate.lowest


def _without_forecast_details(advice: Advice) -> Advice:
    keys = {"sale_price", "forecast", "price_undercut_sale", "margin_at_undercut_sale"}

    def strip_result(result: StrategyResult) -> StrategyResult:
        pathways = tuple(
            replace(
                pathway, details={key: value for key, value in pathway.details.items() if key not in keys}
            )
            for pathway in result.pathways
        )
        return replace(result, pathways=pathways)

    results = tuple(strip_result(result) for result in advice.results)
    pathways = tuple(
        replace(pathway, details={key: value for key, value in pathway.details.items() if key not in keys})
        for pathway in advice.pathways
    )
    return replace(advice, results=results, pathways=pathways)


def test_baseline_forecast_is_equivalent_to_snapshot_after_removing_provenance() -> None:
    state = state_of(
        make_inputs([SELLER]),
        gatherable={1001: ("herb",)},
        names={1001: "Peacebloom"},
    )
    common = AdviceConfig(
        professions=frozenset({"alchemy"}),
        batch_crafts=2,
        strategies=frozenset({"crafting", "raw_materials"}),
    )
    snapshot = advise(state, replace(common, estimates="snapshot"))
    forecast = advise(state, replace(common, estimates="forecast"))
    assert _without_forecast_details(snapshot).results == _without_forecast_details(forecast).results
    assert _without_forecast_details(snapshot).pathways == _without_forecast_details(forecast).pathways


def test_item_kind_estimate_has_no_forecast_note() -> None:
    item_id = 7003
    item_book = ItemBook(
        0.0,
        item_id,
        1,
        (Auction(1, Fraction(250), 1, "item-key", "seller", False),),
        {},
    )
    inputs = make_inputs(
        [], books={}, facts={}, item_books={item_id: [item_book]}, as_of_t=0.0, latest_sweep_id=1
    )
    state = state_of(inputs)
    estimate = ForecastEstimates(state, AdviceConfig(estimates="forecast")).estimate(item_id)
    assert estimate is not None and estimate.kind == "item" and estimate.forecast is None


def test_forecast_validation_and_context_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import crowley.advisor.estimates as estimates_module

    state = state_of(make_inputs([SELLER]))
    calls = {"walk": 0, "context": 0}
    original_walk = estimates_module.walk_forward
    original_context = estimates_module.current_context

    def counted_walk(*args, **kwargs):
        calls["walk"] += 1
        return original_walk(*args, **kwargs)

    def counted_context(*args, **kwargs):
        calls["context"] += 1
        return original_context(*args, **kwargs)

    monkeypatch.setattr(estimates_module, "walk_forward", counted_walk)
    monkeypatch.setattr(estimates_module, "current_context", counted_context)
    provider = ForecastEstimates(state, AdviceConfig(estimates="forecast"))
    provider.estimate(1001)
    provider.summary()
    assert calls == {"walk": 1, "context": 1}


def test_forecast_summary_counts_mixed_population() -> None:
    as_of_t = 120 * 3600.0
    books = {
        7001: [Book(as_of_t, [Group("one", Fraction(123), 4, 100_000, 1)], None, 7001, 120)],
        7002: _hourly_books(7002, {index: 100 + index * 2 for index in range(24, 121)}, end=120),
        7004: _hourly_books(7004, {index: 250 for index in range(24, 121)}, end=120),
        7005: _hourly_books(7005, {index: 300 for index in range(24, 120)}, end=119),
    }
    provider = ForecastEstimates(
        _commodity_state(books, as_of_t=as_of_t, sweep_id=120), AdviceConfig(estimates="forecast")
    )
    trending = provider.estimate(7002)
    summary = provider.summary()
    assert trending is not None and trending.forecast is not None
    assert trending.forecast.price_model == "trend_24h"
    assert summary.items_scored == 3
    assert summary.items_forecast == 3
    assert summary.price_models == {"last": 2, "median_24h": 0, "ewma_6h": 0, "trend_24h": 1}
    assert summary.demand_models == {"trailing": 3, "trailing_6h": 0, "hour_profile": 0}
    assert sum(summary.price_models.values()) == summary.items_forecast
    assert sum(summary.demand_models.values()) == summary.items_forecast
    assert summary.horizon_hours == 2.0
    assert summary.window_hours == 72.0
    assert summary.median_price_skill == trending.forecast.price_skill
    assert summary.median_price_skill is not None
    assert summary.median_demand_skill is None


def test_advice_route_rejects_bad_estimates_and_accepts_both_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=harness.pool)))
    monkeypatch.setattr(
        service, "latest_sweep", lambda conn, key, build, as_of: (harness.sweep, 1_000_000.0, build or BUILD)
    )
    monkeypatch.setattr(
        service,
        "load_market_state",
        lambda conn, key, **kwargs: replace(
            harness.state,
            inputs=replace(harness.state.inputs, latest_sweep_id=harness.sweep),
        ),
    )
    monkeypatch.setattr(advice_route, "_house_id", lambda conn, key: None)
    monkeypatch.setattr(advice_route, "ADVICE_CACHE", AdviceCache())
    kwargs = dict(
        build=None,
        as_of=None,
        strategies="raw_materials",
        professions="alchemy",
        min_profit=0,
        min_volume=0.0,
        share=0.1,
        horizon_hours=2.0,
        min_sale_prob=0.5,
        price_basis="lowest",
        max_cold_age_hours=3.0,
        character=None,
        limit=25,
        exclusions=False,
    )
    with pytest.raises(HTTPException, match="estimates") as raised:
        advice_route.advice(HOUSE, request, estimates="bogus", **kwargs)
    assert raised.value.status_code == 400
    assert (
        advice_route.advice(HOUSE, request, estimates="snapshot", **kwargs)["parameters"]["estimates"]
        == "snapshot"
    )
    assert (
        advice_route.advice(HOUSE, request, estimates="forecast", **kwargs)["parameters"]["estimates"]
        == "forecast"
    )


def test_snapshot_payload_additions_are_the_only_compatibility_changes() -> None:
    state = state_of(make_inputs([SELLER]))
    config = AdviceConfig(estimates="snapshot", strategies=frozenset({"raw_materials"}))
    advice = advise(state, config)
    payload = plain(advice_payload(advice, dict(state.names), None))
    assert payload["parameters"].pop("estimates") == "snapshot"
    assert payload.pop("forecast") is None
    assert all(
        "sale_price" not in pathway["details"]
        for section in payload["sections"].values()
        for pathway in section
    )
