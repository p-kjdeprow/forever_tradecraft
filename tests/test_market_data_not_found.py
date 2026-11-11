"""Missing market data raises the specific MarketDataNotFound, which the advisor maps to a 404."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crowley.advisor.cli import main
from crowley.analysis.rank import latest_sweep
from crowley.core.errors import MarketDataNotFound
from crowley.trading.account_gate import load_account_snapshot
from crowley.trading.data import _latest_export


class EmptyConn:
    """A connection whose every query returns no row."""

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return None


def test_latest_export_raises_the_specific_error():
    with pytest.raises(MarketDataNotFound, match="no latest client export"):
        _latest_export(EmptyConn(), "1.60.1.1")


def test_account_snapshot_raises_the_specific_error():
    with pytest.raises(MarketDataNotFound, match="no account capture"):
        load_account_snapshot(EmptyConn(), "us-normal-alliance", "Nobody")
    with pytest.raises(MarketDataNotFound, match="at or before"):
        load_account_snapshot(
            EmptyConn(), "us-normal-alliance", "Nobody", as_of=datetime(2026, 1, 1, tzinfo=UTC)
        )


def test_latest_sweep_raises_the_specific_error():
    with pytest.raises(MarketDataNotFound, match="no market sweep"):
        latest_sweep(EmptyConn(), "us-normal-alliance", None, None)


def test_a_plain_runtime_error_is_not_mistaken_for_missing_data():
    assert not issubclass(RuntimeError, MarketDataNotFound)
    assert issubclass(MarketDataNotFound, RuntimeError)  # existing handlers keep working


def test_cli_reports_missing_data_and_bad_options_as_usage_errors(monkeypatch, capsys):
    import crowley.advisor.cli as cli

    class NoConnection:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli.psycopg, "connect", lambda *a, **k: NoConnection())

    def missing(*args, **kwargs):
        raise MarketDataNotFound("no market sweep for house 'x'")

    monkeypatch.setattr(cli, "run_advice", missing)
    with pytest.raises(SystemExit) as raised:
        main(["--house", "x", "--database-url", "postgresql://unused"])
    assert raised.value.code == 2
    assert "no market sweep for house 'x'" in capsys.readouterr().err
