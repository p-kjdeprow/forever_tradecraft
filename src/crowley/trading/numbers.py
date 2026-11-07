"""Exact numeric coercions shared by trading calculations."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

type FractionInput = Fraction | int | float | Decimal | str


def as_fraction(value: FractionInput) -> Fraction:
    """Convert an accepted trading number without binary float expansion."""

    if isinstance(value, Fraction):
        return value
    return Fraction(str(value))


__all__ = ["FractionInput", "as_fraction"]
