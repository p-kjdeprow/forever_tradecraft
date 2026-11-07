"""Auction-house fee calculations shared by views and execution."""

from __future__ import annotations

from crowley.core.auction_rules import AuctionHouseRules


def deposit(sell_price: int, quantity: int, duration_hours: int, rules: AuctionHouseRules) -> int:
    """Return the house-specific deposit for a listing."""

    return rules.deposit_policy.calculate(sell_price, quantity, duration_hours)


__all__ = ["deposit"]
