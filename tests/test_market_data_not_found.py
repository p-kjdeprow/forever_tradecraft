"""Missing retained market data has a distinct domain error."""

from __future__ import annotations

import pytest
from crowley_common.errors import MarketDataNotFound
from crowley_market.client_data import _latest_export


class EmptyConn:
    """A connection whose every query returns no row."""

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return None


def test_latest_export_raises_the_specific_error():
    with pytest.raises(MarketDataNotFound, match="no latest client export"):
        _latest_export(EmptyConn(), "1.60.1.1")


def test_a_plain_runtime_error_is_not_mistaken_for_missing_data():
    assert not issubclass(RuntimeError, MarketDataNotFound)
    assert issubclass(MarketDataNotFound, RuntimeError)  # existing handlers keep working
