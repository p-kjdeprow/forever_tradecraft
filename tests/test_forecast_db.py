"""Database-gated end-to-end validation CLI coverage."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
import pytest

from crowley.forecast import __main__ as forecast_cli
from crowley.server.db import migrate
from tests.test_advisor_db import NEW as BUILD
from tests.test_advisor_db import _export, _house, _sweep
from tests.test_analysis_candidates_db import _observe

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]


def test_validate_cli_is_pinned_to_as_of_house_and_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            suffix = os.urandom(4).hex()
            house_key = f"us-normal-alliance-forecast-{suffix}"
            foreign_key = f"us-normal-horde-forecast-{suffix}"
            house = _house(conn, house_key, "alliance")
            foreign = _house(conn, foreign_key, "horde")
            _export(conn, BUILD, output_sell_price=100)
            sweeps: list[int] = []
            for index, minutes in enumerate((0, 15, 30)):
                sweep = _sweep(conn, house, f"forecast-{suffix}-{index}", BUILD, minutes, None)
                sweeps.append(sweep)
                _observe(conn, house, sweep, 7001, 1, "commodity", (10 + index,), minutes=minutes)
                _observe(conn, house, sweep, 7002, 2, "commodity", (3,), minutes=minutes)
            foreign_sweep = _sweep(conn, foreign, f"foreign-{suffix}", BUILD, 45, None)
            _observe(conn, foreign, foreign_sweep, 7999, 1, "commodity", (999,), minutes=45)

            @contextmanager
            def same_connection(*args, **kwargs):
                yield conn

            monkeypatch.setattr(forecast_cli.psycopg, "connect", same_connection)
            as_of = datetime(2026, 11, 9, 12, 20, tzinfo=UTC).isoformat()
            assert (
                forecast_cli.main(
                    [
                        "validate",
                        "--database-url",
                        DB_URL,
                        "--house",
                        house_key,
                        "--as-of",
                        as_of,
                        "--json",
                    ]
                )
                == 0
            )
            payload = json.loads(capsys.readouterr().out)
            assert payload["house"] == house_key
            assert payload["build"] == BUILD
            assert payload["sweep_id"] == sweeps[1]
            assert payload["as_of"] == "2026-11-09T12:20:00+00:00"
            assert payload["items_loaded"] == 2
            assert {item["item_id"] for item in payload["items"]} == {7001, 7002}
            assert set(payload) == {
                "as_of",
                "as_of_epoch",
                "build",
                "error_metrics",
                "house",
                "horizon_hours",
                "items",
                "items_loaded",
                "items_scored",
                "lookback_hours",
                "models",
                "sweep_id",
                "validation_window",
            }
            assert payload["validation_window"] == {
                "start": payload["as_of_epoch"] - 72 * 3600,
                "end": payload["as_of_epoch"],
                "seconds": 72 * 3600,
            }
            assert payload["error_metrics"] == {
                "price": "mean absolute log error",
                "demand": "mean squared error",
            }
            assert set(payload["models"]) == {"price", "demand"}
            assert set(payload["models"]["price"]) == {"last", "median_24h", "ewma_6h", "trend_24h"}
            assert set(payload["models"]["demand"]) == {"trailing", "trailing_6h", "hour_profile"}
            for summaries in payload["models"].values():
                for summary in summaries.values():
                    assert set(summary) == {
                        "items",
                        "paired_origins",
                        "median_skill",
                        "chosen_items",
                        "chosen_share",
                    }
            for item in payload["items"]:
                assert set(item) == {
                    "item_id",
                    "name",
                    "price",
                    "price_choice",
                    "units_per_hour",
                    "demand_choice",
                    "chosen_skill",
                }
                assert set(item["price_choice"]) == {"model", "paired_n", "skill"}
                assert set(item["demand_choice"]) == {"model", "paired_n", "skill"}
