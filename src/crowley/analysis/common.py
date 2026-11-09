"""Small helpers shared by the analysis and trading packages."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .books import Book

REFERENCE_UNITS = 20


def reference(book: Book) -> Fraction | None:
    """Unit price of the 20th cheapest unit in a book, or None when fewer are visible."""

    groups = sorted(book.groups, key=lambda group: (group.unit, group.result_id))
    seen = 0
    for group in groups:
        seen += group.quantity
        if seen >= REFERENCE_UNITS:
            return group.unit
    return None


def _fraction_json(value: Fraction) -> int | str:
    return value.numerator if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def jsonable(value: Any) -> Any:
    """Convert exact analysis values to the stable JSON representation."""

    if isinstance(value, Fraction):
        return _fraction_json(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


__all__ = ["REFERENCE_UNITS", "jsonable", "reference"]
