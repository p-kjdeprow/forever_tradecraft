"""Background warming of the shared trading-summary cache after new scan ingest."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from starlette.background import BackgroundTasks

from crowley.server.routes import scans

HOUSE = "us-normal-horde"


@dataclass
class IngestResult:
    status: str
    house: str


class FakeRequest:
    def __init__(self, pool: Any) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(pool=pool))

    async def body(self) -> bytes:
        return b"{}"


class FakePool:
    def __init__(self) -> None:
        self.connection_count = 0
        self.connection_object = object()

    @contextmanager
    def connection(self):
        self.connection_count += 1
        yield self.connection_object


def _settings(*, warm_advice: bool) -> SimpleNamespace:
    return SimpleNamespace(warm_advice=warm_advice, data_dir=Path("/unused"))


def _upload(pool: Any, tasks: BackgroundTasks, *, warm_advice: bool) -> dict[str, Any]:
    return asyncio.run(scans.upload_scan(FakeRequest(pool), tasks, _settings(warm_advice=warm_advice)))


def test_created_upload_schedules_summary_before_advice_and_duplicate_schedules_neither(monkeypatch) -> None:
    pool = object()
    events: list[tuple[str, Any, str]] = []
    results = iter([IngestResult("created", HOUSE), IngestResult("duplicate", HOUSE)])
    monkeypatch.setattr(scans, "_persist_market_sweep_request", lambda *args: next(results))
    monkeypatch.setattr(
        scans,
        "_warm_trading_summary",
        lambda active_pool, house: events.append(("summary", active_pool, house)),
    )
    monkeypatch.setattr(
        scans, "_warm_advice", lambda active_pool, house: events.append(("advice", active_pool, house))
    )

    created_tasks = BackgroundTasks()
    created = _upload(pool, created_tasks, warm_advice=True)
    assert created["status"] == "created"
    assert [task.func for task in created_tasks.tasks] == [scans._warm_trading_summary, scans._warm_advice]
    asyncio.run(created_tasks())
    assert events == [("summary", pool, HOUSE), ("advice", pool, HOUSE)]

    duplicate_tasks = BackgroundTasks()
    duplicate = _upload(pool, duplicate_tasks, warm_advice=True)
    assert duplicate["status"] == "duplicate"
    assert duplicate_tasks.tasks == []
    assert events == [("summary", pool, HOUSE), ("advice", pool, HOUSE)]


def test_summary_warms_through_pool_connection_using_shared_loader(monkeypatch) -> None:
    pool = FakePool()
    calls: list[tuple[Any, str]] = []
    monkeypatch.setattr(scans, "load_trading_summary", lambda conn, house: calls.append((conn, house)))

    scans._warm_trading_summary(pool, HOUSE)

    assert calls == [(pool.connection_object, HOUSE)]
    assert pool.connection_count == 1


def test_summary_warm_is_independent_of_advice_setting(monkeypatch) -> None:
    pool = object()
    monkeypatch.setattr(scans, "_persist_market_sweep_request", lambda *args: IngestResult("created", HOUSE))
    summary_calls: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        scans, "_warm_trading_summary", lambda active_pool, house: summary_calls.append((active_pool, house))
    )
    monkeypatch.setattr(
        scans, "_warm_advice", lambda *args: (_ for _ in ()).throw(AssertionError("advice should stay off"))
    )

    tasks = BackgroundTasks()
    response = _upload(pool, tasks, warm_advice=False)
    assert response["status"] == "created"
    assert [task.func for task in tasks.tasks] == [scans._warm_trading_summary]
    asyncio.run(tasks())
    assert summary_calls == [(pool, HOUSE)]


def test_summary_warm_failure_is_logged_and_does_not_skip_advice(monkeypatch, caplog) -> None:
    pool = FakePool()
    monkeypatch.setattr(scans, "_persist_market_sweep_request", lambda *args: IngestResult("created", HOUSE))

    def fail_load(conn: Any, house: str) -> dict[str, Any]:
        raise RuntimeError("summary warm failed")

    monkeypatch.setattr(scans, "load_trading_summary", fail_load)
    advice_calls: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        scans, "_warm_advice", lambda active_pool, house: advice_calls.append((active_pool, house))
    )

    tasks = BackgroundTasks()
    response = _upload(pool, tasks, warm_advice=True)
    assert response["status"] == "created"
    asyncio.run(tasks())

    assert pool.connection_count == 1
    assert advice_calls == [(pool, HOUSE)]
    assert any(
        record.name == "crowley.server.routes.scans"
        and f"Failed to warm trading summary for house {HOUSE}" in record.getMessage()
        for record in caplog.records
    )
