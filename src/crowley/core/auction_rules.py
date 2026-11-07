"""Versioned economic rules for a single auction house."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .house import House


@dataclass(frozen=True)
class DepositPolicy:
    """The deposit formula and the listing durations accepted by the client."""

    rate: Fraction
    base_duration_hours: int
    supported_durations_hours: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Fraction):
            raise TypeError("deposit rate must be a Fraction")
        if self.rate < 0:
            raise ValueError("deposit rate cannot be negative")
        if (
            isinstance(self.base_duration_hours, bool)
            or not isinstance(self.base_duration_hours, int)
            or self.base_duration_hours <= 0
        ):
            raise ValueError("deposit base duration must be positive")
        if not self.supported_durations_hours:
            raise ValueError("deposit policy must support at least one duration")
        if any(
            isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0
            for duration in self.supported_durations_hours
        ):
            raise ValueError("deposit durations must be positive integers")
        if tuple(sorted(set(self.supported_durations_hours))) != self.supported_durations_hours:
            raise ValueError("deposit durations must be sorted and unique")

    def calculate(self, sell_price: int, quantity: int, duration_hours: int) -> int:
        """Return the integer-copper deposit for one listing."""

        if duration_hours not in self.supported_durations_hours:
            raise ValueError(f"unsupported duration: {duration_hours}")
        return math.ceil(
            Fraction(sell_price * quantity) * self.rate * duration_hours / self.base_duration_hours
        )


@dataclass(frozen=True)
class AuctionHouseRules:
    """Immutable economic rules bound to one version of the auction-house model."""

    seller_cut: Fraction
    deposit_policy: DepositPolicy
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.seller_cut, Fraction):
            raise TypeError("seller cut must be a Fraction")
        if not 0 <= self.seller_cut < 1:
            raise ValueError("seller cut must be in [0, 1)")
        if not isinstance(self.deposit_policy, DepositPolicy):
            raise TypeError("deposit policy must be DepositPolicy")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("auction-house rules version must not be empty")


_DEPOSIT_POLICY = DepositPolicy(
    rate=Fraction(15, 100),
    base_duration_hours=12,
    supported_durations_hours=(12, 24, 48),
)
_RULES_BY_FACTION = {
    "alliance": AuctionHouseRules(Fraction(5, 100), _DEPOSIT_POLICY, "auction-rules-v1"),
    "horde": AuctionHouseRules(Fraction(5, 100), _DEPOSIT_POLICY, "auction-rules-v1"),
    "neutral": AuctionHouseRules(Fraction(15, 100), _DEPOSIT_POLICY, "auction-rules-v1"),
}


def rules_for_house(house: House) -> AuctionHouseRules:
    """Return the exact economic rules for an already validated house identity."""

    try:
        return _RULES_BY_FACTION[house.faction]
    except KeyError as exc:
        raise ValueError(f"unknown faction {house.faction!r}") from exc


__all__ = ["AuctionHouseRules", "DepositPolicy", "rules_for_house"]
