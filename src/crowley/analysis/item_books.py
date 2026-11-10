"""Item-kind (non-commodity) books and their conservative sale evidence.

An item-kind observation is a list of one-unit auctions; a row's ``quantity`` is the
availability of identical one-unit auctions and its unit price is the per-auction buyout
(ARCHITECTURE, scan format).  There is no persistent price-level group, so evidence comes
from which auctions disappeared between two consecutive ``coverage = 'full'`` observations.
That classification is ``core.volume.compare_observations``'s: grouped scopes, inconsistent
result ids and the expiry classes (a disappearance that could be an expiry is never a sale)
are decided there and only consumed here.

Per interval the cheapest-first walk mirrors ``books.bottom_walk``: auctions are visited in
(price, resultID) order; a disappearance classified as a sale candidate is consumed, one that
could be an expiry is skipped, and the first auction that is still present, changed identity
or has unknown time left ends the walk.  Nothing above that point is inferred.

Owner continuity (live behaviour: a row of N auctions is reissued under a NEW resultID each
time one of its auctions sells, and a cancel-and-repost also changes ids).  A vanished
resultID is therefore not by itself N sales.  Rows are matched across the two observations by
(owner, unit price): only the owner's *decrease* in units at that price is consumed, and an
owner who appears at any price they did not hold before is treated as having cancelled or
reposted, which ends the walk (the units may be the same ones).

Design decision: only fully individual scopes give evidence.  A scope with any grouped row
(several owners, partial or empty ``names``) can hide an owner's reposted units, so
``core.volume`` rejects the whole interval and so does this module; the individual-row prefix
before a grouped row is deliberately NOT used.  Grouped items therefore stay
``output_no_observed_demand``.  The route to calibrating them is own confirmed sales or
deliberate trial listings, not inference from anonymous groups.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from crowley.analysis.metrics import FAIR_WINDOW_SECONDS, _percentile
from crowley.core.volume import MAX_INTERVAL_SECONDS, compare_observations_detailed


@dataclass(frozen=True)
class Auction:
    """One search result: ``quantity`` identical one-unit auctions at ``unit`` copper each."""

    result_id: int
    unit: Fraction
    quantity: int
    item_key: str
    owner: str | None  # the sole owner of an individual row; None for a grouped row
    own: bool  # the row contains one of our own auctions


@dataclass
class ItemBook:
    """One full item-kind observation of one item id in one sweep."""

    t: float  # query.startedAt, the same clock as commodity books
    item_id: int
    sweep_id: int
    auctions: tuple[Auction, ...]
    # Wire-shaped record ({"observation", "envelope"}) so core.volume judges every interval.
    record: dict[str, Any] = field(repr=False, compare=False)

    @property
    def item_keys(self) -> frozenset[str]:
        return frozenset(auction.item_key for auction in self.auctions)

    def scoped(self, item_key: str | None) -> tuple[Auction, ...]:
        return tuple(a for a in self.auctions if item_key is None or a.item_key == item_key)


@dataclass(frozen=True)
class ItemInterval:
    """A comparable consecutive pair of full observations and what its walk consumed."""

    before: ItemBook
    after: ItemBook
    dt: float  # start to start
    exposure: int  # end of the earlier query to end of the later one (core.volume's denominator)
    consumed: tuple[tuple[Fraction, int], ...]  # (unit price, units) in walk order


def floor_price(book: ItemBook, item_key: str | None) -> Fraction | None:
    prices = [auction.unit for auction in book.scoped(item_key)]
    return min(prices) if prices else None


def item_intervals(
    books: Sequence[ItemBook], item_key: str | None, *, max_interval_seconds: int | None = None
) -> list[ItemInterval | None]:
    """One slot per adjacent pair; ``None`` where ``core.volume`` finds no usable evidence.

    ``max_interval_seconds`` overrides ``core.volume``'s 1800 s limit; only the cold-tier
    promotion view passes it, to read depletion across the long gaps between cold observations.
    """

    result: list[ItemInterval | None] = []
    limit = MAX_INTERVAL_SECONDS if max_interval_seconds is None else max_interval_seconds
    for left, right in zip(books, books[1:], strict=False):
        comparison = compare_observations_detailed(
            left.record, right.record, item_key=item_key, max_interval_seconds=limit
        )
        if not comparison["valid"] or comparison["groupedScope"]:
            result.append(None)
            continue
        consumed = _walk(left, right, item_key, comparison["removedClasses"])
        result.append(
            ItemInterval(left, right, right.t - left.t, comparison["comparedSeconds"], tuple(consumed))
        )
    return result


def _walk(
    left: ItemBook, right: ItemBook, item_key: str | None, classes: dict[int, str]
) -> list[tuple[Fraction, int]]:
    """Units the cheapest-first walk consumed between two individual-scope observations."""

    after = right.scoped(item_key)
    before = left.scoped(item_key)
    units_before: dict[tuple[str, Fraction], int] = defaultdict(int)
    units_after: dict[tuple[str, Fraction], int] = defaultdict(int)
    prices_before: dict[str, set[Fraction]] = defaultdict(set)
    prices_after: dict[str, set[Fraction]] = defaultdict(set)
    for auction, units, prices in [(a, units_before, prices_before) for a in before] + [
        (a, units_after, prices_after) for a in after
    ]:
        if auction.owner is None:
            raise ValueError(f"item {left.item_id}: grouped row {auction.result_id} in an individual scope")
        units[(auction.owner, auction.unit)] += auction.quantity
        prices[auction.owner].add(auction.unit)
    remaining = {key: max(count - units_after[key], 0) for key, count in units_before.items()}
    consumed: list[tuple[Fraction, int]] = []
    for auction in sorted(before, key=lambda a: (a.unit, a.result_id)):
        classification = classes.get(auction.result_id)
        if classification is None or classification == "time_left_unknown":
            break  # untouched, changed identity, or unknowable
        assert auction.owner is not None
        if prices_after[auction.owner] - prices_before[auction.owner]:
            break  # the owner posted at a new price: a cancel or repost, not a sale
        key = (auction.owner, auction.unit)
        taken = min(auction.quantity, remaining[key])
        remaining[key] -= taken
        if taken == 0:
            break  # relisted unchanged under a new id: still for sale
        if classification != "expiration_possible":
            consumed.append((auction.unit, taken))
        if units_after[key] > 0:
            break  # the owner still lists units at this price
    return consumed


def cheapest_cost(book: ItemBook, item_key: str | None, quantity: int) -> tuple[Fraction, int] | None:
    """Exact cost of the cheapest ``quantity`` buyable units and the highest buyout used.

    Our own auctions are not supply.  ``None`` when fewer than ``quantity`` units are listed.
    """

    need = quantity
    cost = Fraction(0)
    limit = 0
    for auction in sorted(book.scoped(item_key), key=lambda a: (a.unit, a.result_id)):
        if auction.own:
            continue
        take = min(need, auction.quantity)
        cost += auction.unit * take
        limit = max(limit, int(auction.unit))
        need -= take
        if need == 0:
            return cost, limit
    return None


def item_fair(
    books: Sequence[ItemBook], floors: Sequence[Fraction | None], index: int, *, min_books: int
) -> dict[str, Any]:
    """Strictly-past 24 h fair value of the item-kind floor (median of per-book lowest buyouts)."""

    current = books[index].t
    refs = [
        floor
        for book, floor in zip(books, floors, strict=True)
        if current - FAIR_WINDOW_SECONDS <= book.t < current and floor is not None
    ]
    if len(refs) < min_books:
        return {"value": None, "band": {"p25": None, "p75": None}, "n": len(refs)}
    return {
        "value": _percentile(refs, Fraction(1, 2)),
        "band": {"p25": _percentile(refs, Fraction(1, 4)), "p75": _percentile(refs, Fraction(3, 4))},
        "n": len(refs),
    }


def replay_item_listing(
    books: Sequence[ItemBook],
    intervals: Sequence[ItemInterval | None],
    start: int,
    price: int,
    quantity: int,
    horizon: float,
) -> bool | None:
    """Whether a hypothetical listing of ``quantity`` units at ``price`` sold within ``horizon``.

    ``None`` when ``[t, t + horizon]`` is not an unbroken run of usable intervals; that is decided
    from the structure alone, before and regardless of any outcome.  Otherwise units consumed by
    each interval's walk strictly above ``price`` (ties never fill) accumulate until the whole
    listing is filled, as in ``time_to_sale``.
    """

    start_t = books[start].t
    end = next((i for i in range(start, len(books)) if books[i].t >= start_t + horizon), None)
    if end is None or any(interval is None for interval in intervals[start:end]):
        return None
    filled = 0
    for interval in intervals[start:end]:
        assert interval is not None
        filled += sum(units for unit, units in interval.consumed if unit > price)
        if filled >= quantity:
            return interval.after.t - start_t <= horizon
    return False


def load_item_books(
    conn: Any,
    house_key: str,
    item_ids: Sequence[int],
    *,
    client_build: str,
    start_t: int | None = None,
    end_t: int | None = None,
    sweep_id: int | None = None,
) -> dict[int, list[ItemBook]]:
    """Load ``kind = 'item'``, ``coverage = 'full'`` observations for one house and build.

    Only full coverage is read: a bounded or incomplete item scope proves nothing about the
    auctions it omits.
    """

    if "." not in client_build:
        raise ValueError("item book client_build must be a full client build")
    if not item_ids:
        return {}
    conditions = [
        "h.key = %s",
        "mo.kind = 'item'",
        "mo.coverage = 'full'",
        "mo.source_complete",
        "mo.item_id = ANY(%s)",
        "ms.client_version || '.' || ms.client_build = %s",
    ]
    parameters: list[object] = [house_key, list(item_ids), client_build]
    if start_t is not None:
        conditions.append("(mo.query ->> 'startedAt')::bigint >= %s")
        parameters.append(start_t)
    if end_t is not None:
        conditions.append("(mo.query ->> 'startedAt')::bigint <= %s")
        parameters.append(end_t)
    if sweep_id is not None:
        conditions.append("ms.id = %s")
        parameters.append(sweep_id)
    rows = conn.execute(
        f"""
        SELECT ms.id, ms.client_version, ms.client_build, ms.plan_hash, mo.item_id, mo.source_complete,
               mo.cutoff, mo.query, mcr.result_id, mcr.quantity, mcr.unit_numerator,
               mcr.unit_denominator, mcr.item_key, mcr.row_data
        FROM houses h
        JOIN market_sweeps ms ON ms.house_id = h.id
        JOIN market_observations mo ON mo.sweep_id = ms.id AND mo.house_id = ms.house_id
        LEFT JOIN market_captured_rows mcr ON mcr.sweep_id = mo.sweep_id AND mcr.item_id = mo.item_id
        WHERE {" AND ".join(conditions)}
        ORDER BY mo.item_id, ms.started_at, ms.id, mcr.result_id
        """,
        tuple(parameters),
    ).fetchall()

    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for (
        sweep,
        version,
        build,
        plan_hash,
        item_id,
        source_complete,
        cutoff,
        query,
        result_id,
        quantity,
        numerator,
        denominator,
        row_key,
        row_data,
    ) in rows:
        if not isinstance(query, dict) or query.get("kind") != "queried":
            raise ValueError("item observation query must have kind 'queried'")
        started = query.get("startedAt")
        if type(started) is not int:
            raise ValueError("item observation query.startedAt must be an integer")
        entry = grouped.setdefault(
            (int(item_id), int(sweep)),
            {
                "t": float(started),
                "auctions": [],
                "rows": [],
                "ids": set(),
                "observation": {
                    "kind": "item",
                    "coverage": "full",
                    "sourceComplete": source_complete,
                    "cutoff": cutoff,
                    "query": query,
                    "itemID": int(item_id),
                },
                "envelope": {
                    "house": {"key": house_key},
                    "context": {"clientVersion": version, "clientBuild": build},
                    "planHash": plan_hash,
                },
            },
        )
        if result_id is None:
            continue
        if result_id in entry["ids"]:
            raise ValueError(f"item {item_id} sweep {sweep}: duplicate resultID {result_id}")
        entry["ids"].add(result_id)
        unit = Fraction(int(numerator), int(denominator))
        wire = row_data["unitPrice"]
        if Fraction(wire["numerator"], wire["denominator"]) != unit:
            raise ValueError(
                f"item {item_id} resultID {result_id}: stored unit price disagrees with row_data"
            )
        owners = row_data["owners"]
        names = owners["names"]
        owner = next(iter(names)) if owners["identityKind"] == "individual" and len(names) == 1 else None
        entry["auctions"].append(
            Auction(
                int(result_id),
                unit,
                int(quantity),
                str(row_key),
                owner,
                owners.get("containsOwnerItem") is True,
            )
        )
        entry["rows"].append(row_data)

    books: dict[int, list[ItemBook]] = defaultdict(list)
    for (item_id, sweep), entry in grouped.items():
        entry["observation"]["rows"] = entry["rows"]
        record = {"observation": entry["observation"], "envelope": entry["envelope"]}
        books[item_id].append(ItemBook(entry["t"], item_id, sweep, tuple(entry["auctions"]), record))
    for item_books in books.values():
        item_books.sort(key=lambda book: (book.t, book.sweep_id))
    return dict(books)
