"""Versioned economic rules for a single auction house."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .house import House


def _validate_fraction(value: object, label: str) -> Fraction:
    if not isinstance(value, Fraction):
        raise TypeError(f"{label} must be a Fraction")
    return value


def _validate_positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_deposit_policy(value: object) -> DepositPolicy:
    if not isinstance(value, DepositPolicy):
        raise TypeError("deposit policy must be DepositPolicy")
    return value


def _validate_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must not be empty")
    return value


@dataclass(frozen=True)
class DepositPolicy:
    """The deposit formula and the listing durations accepted by the client."""

    rate: Fraction
    base_duration_hours: int
    supported_durations_hours: tuple[int, ...]

    def __post_init__(self) -> None:
        _validate_fraction(self.rate, "deposit rate")
        if self.rate < 0:
            raise ValueError("deposit rate cannot be negative")
        _validate_positive_integer(self.base_duration_hours, "deposit base duration")
        if not self.supported_durations_hours:
            raise ValueError("deposit policy must support at least one duration")
        if any(not _is_positive_integer(duration) for duration in self.supported_durations_hours):
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
        _validate_fraction(self.seller_cut, "seller cut")
        if not 0 <= self.seller_cut < 1:
            raise ValueError("seller cut must be in [0, 1)")
        _validate_deposit_policy(self.deposit_policy)
        _validate_text(self.version, "auction-house rules version")


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
