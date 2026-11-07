"""Shared sweep-time calculations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from crowley.analysis.books import Book


def sweep_step_times(books: Mapping[int, Sequence[Book]]) -> dict[int, int]:
    """Return the represented whole-second time for each sweep ID."""

    step_times: dict[int, int] = {}
    for item_books in books.values():
        for book in item_books:
            if book.sweep_id is None:
                continue
            if book.t != int(book.t):
                raise ValueError(f"book time must be whole seconds, got {book.t!r}")
            sweep_id = int(book.sweep_id)
            step_times[sweep_id] = max(step_times.get(sweep_id, int(book.t)), int(book.t))
    return step_times


__all__ = ["sweep_step_times"]
