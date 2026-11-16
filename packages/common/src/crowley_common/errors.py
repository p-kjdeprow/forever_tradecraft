"""Exceptions that mean "the thing asked for is not in the database", as opposed to a bug."""

from __future__ import annotations


class MarketDataNotFound(RuntimeError):
    """No sweep, client export or account capture matches the request (an HTTP 404, not a 500)."""
