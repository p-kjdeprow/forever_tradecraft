"""Commodity books and the observed bottom-walk demand proxy."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, NamedTuple

MAX_GAP = 1260
EXPIRY_SLACK = 60
MAX_EXACT_SECONDS = 172800


@dataclass(frozen=True)
class Group:
    """A persistent commodity price-level group in one observed book."""

    result_id: int | str
    unit: Fraction
    quantity: int
    time_left_seconds: int | float
    owners: int = 0

    @property
    def time_left(self) -> int | float:
        return self.time_left_seconds


@dataclass
class Book:
    """One commodity observation, scoped to the retained price ceiling."""

    t: float
    groups: list[Group] = field(default_factory=list)
    ceiling: Fraction | None = None
    item_id: int | str | None = None
    sweep_id: int | str | None = None

    @property
    def started_at(self) -> float:
        return self.t


@dataclass
class BottomWalk:
    """Observed consumption from the bottom of one book to the next."""

    consumed: list[tuple[Fraction, int]]
    reached_ceiling: bool = False
    consumed_groups: list[tuple[int | str, Fraction, int]] = field(default_factory=list)

    def __iter__(self):
        """Allow the useful ``consumed, reached = bottom_walk(...)`` spelling."""

        yield self.consumed
        yield self.reached_ceiling

    def __getitem__(self, index: int):
        return (self.consumed, self.reached_ceiling)[index]


@dataclass
class Interval:
    """A valid consecutive observation interval and its precomputed walk."""

    before: Book
    after: Book
    dt: float
    ceiling: Fraction | None
    walk: BottomWalk

    @property
    def t(self) -> float:
        return self.before.t

    @property
    def start(self) -> Book:
        return self.before

    @property
    def end(self) -> Book:
        return self.after

    @property
    def consumed(self) -> list[tuple[Fraction, int]]:
        return self.walk.consumed

    @property
    def reached_ceiling(self) -> bool:
        return self.walk.reached_ceiling


def common_ceiling(left: Fraction | None, right: Fraction | None) -> Fraction | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _group_map(book: Book) -> dict[int | str, Group]:
    return {group.result_id: group for group in book.groups}


def bottom_walk(before: Book, after: Book, *, dt: float | None = None) -> BottomWalk:
    """Walk depletion from the cheapest retained groups.

    The function intentionally does not try to infer purchases above the first
    untouched or partially retained group.
    """

    common = common_ceiling(before.ceiling, after.ceiling)
    elapsed = after.t - before.t if dt is None else dt
    after_by_id = _group_map(after)
    in_scope = sorted(
        (group for group in before.groups if common is None or group.unit <= common),
        key=lambda group: (group.unit, group.result_id),
    )
    last_in_scope = in_scope[-1].result_id if in_scope else None
    consumed: list[tuple[Fraction, int]] = []
    consumed_groups: list[tuple[int | str, Fraction, int]] = []
    reached_ceiling = False

    for group in in_scope:
        next_group = after_by_id.get(group.result_id)
        next_quantity = next_group.quantity if next_group is not None else 0
        removed = max(group.quantity - next_quantity, 0)
        if next_quantity == 0 and group.time_left_seconds <= elapsed + EXPIRY_SLACK:
            # Expiry is explicitly skipped and does not stop the walk.
            continue
        if removed == 0:
            break
        consumed.append((group.unit, removed))
        consumed_groups.append((group.result_id, group.unit, removed))
        if group.result_id == last_in_scope and common is not None:
            reached_ceiling = True
        if next_quantity > 0:
            break

    return BottomWalk(consumed, reached_ceiling, consumed_groups)


def valid_intervals(books: Iterable[Book], max_gap: float = MAX_GAP) -> list[Interval | None]:
    """Return one slot per adjacent pair; ``None`` marks a chain break."""

    ordered = list(books)
    intervals: list[Interval | None] = []
    for before, after in zip(ordered, ordered[1:], strict=False):
        dt = after.t - before.t
        if 0 < dt <= max_gap:
            common = common_ceiling(before.ceiling, after.ceiling)
            intervals.append(Interval(before, after, dt, common, bottom_walk(before, after)))
        else:
            intervals.append(None)
    return intervals


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _fraction(numerator: Any, denominator: Any) -> Fraction:
    return Fraction(int(numerator), int(denominator))


def _cutoff(cutoff: Any) -> Fraction | None:
    value = _json_value(cutoff)
    if not isinstance(value, dict):
        raise ValueError("commodity cutoff is not an object")
    kind = value.get("kind")
    if kind == "unbounded":
        return None
    if kind != "price":
        raise ValueError(f"unsupported commodity cutoff kind: {kind!r}")
    return _fraction(value["numerator"], value["denominator"])


def _exact_seconds(row_data: Any) -> int | float:
    data = _json_value(row_data)
    if not isinstance(data, dict) or not isinstance(data.get("timeLeft"), dict):
        raise ValueError("commodity row has no exact timeLeft")
    time_left = data["timeLeft"]
    if time_left.get("kind") != "exact":
        raise ValueError("commodity row has non-exact timeLeft")
    seconds = time_left.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("commodity exact timeLeft seconds is not numeric")
    if seconds > MAX_EXACT_SECONDS:
        raise ValueError("commodity exact timeLeft seconds exceeds corrected contract")
    return seconds


def _owners(row_data: Any) -> int:
    data = _json_value(row_data)
    if not isinstance(data, dict) or not isinstance(data.get("owners"), dict):
        raise ValueError("commodity row has no owners object")
    value = data["owners"].get("totalNumberOfOwners")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("commodity totalNumberOfOwners is not a nonnegative integer")
    if isinstance(value, str) and (not value or not value.isascii() or not value.isdecimal()):
        raise ValueError("commodity totalNumberOfOwners is not a nonnegative integer")
    owners = int(value)
    if owners < 0:
        raise ValueError("commodity totalNumberOfOwners is not a nonnegative integer")
    return owners


class _CommodityRow(NamedTuple):
    sweep_id: int
    started_at: object
    item_id: int
    kind: str
    coverage: str
    cutoff: object
    query: object
    result_id: int | str | None
    quantity: int
    unit_numerator: int
    unit_denominator: int
    row_data: object


def load_commodity_books(conn: Any, house_key: str) -> dict[int, list[Book]]:
    """Load one house's corrected commodity observations in one read-only transaction."""

    books: dict[int, list[Book]] = defaultdict(list)
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        migration = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = %s",
            ("003_item_unit_price_and_time_left.sql",),
        ).fetchone()
        if migration is None:
            raise RuntimeError("required migration 003_item_unit_price_and_time_left.sql is not applied")

        rows = conn.execute(
            """
            SELECT ms.id, ms.started_at, mo.item_id, mo.kind, mo.coverage,
                   mo.cutoff, mo.query, mcr.result_id, mcr.quantity,
                   mcr.unit_numerator, mcr.unit_denominator, mcr.row_data
            FROM houses h
            JOIN market_sweeps ms ON ms.house_id = h.id
            JOIN market_observations mo ON mo.sweep_id = ms.id
                AND mo.house_id = ms.house_id
            LEFT JOIN market_captured_rows mcr ON mcr.sweep_id = mo.sweep_id
                AND mcr.item_id = mo.item_id
            WHERE h.key = %s
              AND mo.kind = 'commodity'
              AND mo.coverage IN ('full', 'bounded')
            ORDER BY mo.item_id, ms.started_at, ms.id, mcr.result_id
            """,
            (house_key,),
        ).fetchall()

    typed_rows = tuple(_CommodityRow(*row) for row in rows)

    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in typed_rows:
        sweep_id = row.sweep_id
        item_id = int(row.item_id)
        coverage = row.coverage
        query = _json_value(row.query)
        if not isinstance(query, dict) or query.get("kind") != "queried":
            raise ValueError("commodity observation query must have kind 'queried'")
        started_at = query.get("startedAt")
        if type(started_at) is not int:
            raise ValueError("commodity observation query.startedAt must be an integer")
        started = float(started_at)
        cutoff_value = _json_value(row.cutoff)
        if not isinstance(cutoff_value, dict):
            raise ValueError("commodity observation cutoff is not an object")
        cutoff_kind = cutoff_value.get("kind")
        if coverage == "full":
            if cutoff_kind != "unbounded":
                raise ValueError("full commodity observation must have an unbounded cutoff")
            cutoff = None
        elif coverage == "bounded":
            if cutoff_kind != "price":
                raise ValueError("bounded commodity observation must have a price cutoff")
            cutoff = _cutoff(cutoff_value)
        else:
            raise ValueError(f"unsupported commodity coverage: {coverage!r}")
        key = (item_id, int(sweep_id))
        entry = grouped.setdefault(
            key,
            {
                "item_id": item_id,
                "sweep_id": sweep_id,
                "t": started,
                "ceiling": cutoff,
                "groups": {},
            },
        )
        result_id = row.result_id
        if result_id is None:
            continue
        quantity = int(row.quantity)
        unit = _fraction(row.unit_numerator, row.unit_denominator)
        row_data = row.row_data
        time_left = _exact_seconds(row_data)
        owners = _owners(row_data)
        prior = entry["groups"].get(result_id)
        if prior is None:
            entry["groups"][result_id] = Group(result_id, unit, quantity, time_left, owners)
        else:
            if prior.unit != unit:
                raise ValueError("one commodity result_id has multiple unit prices")
            if prior.owners != owners:
                raise ValueError("one commodity result_id has multiple owner counts")
            entry["groups"][result_id] = Group(
                result_id, unit, prior.quantity + quantity, min(prior.time_left_seconds, time_left), owners
            )

    for entry in grouped.values():
        book = Book(
            t=entry["t"],
            groups=list(entry["groups"].values()),
            ceiling=entry["ceiling"],
            item_id=entry["item_id"],
            sweep_id=entry["sweep_id"],
        )
        books[entry["item_id"]].append(book)
    for item_books in books.values():
        item_books.sort(key=lambda book: (book.t, str(book.sweep_id)))
    return dict(books)
