"""Read-only price and observed-demand watch for Draught of Water Walking."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from crowley.analysis.books import Book, load_commodity_books, valid_intervals
from crowley.analysis.item_books import Auction, ItemBook
from crowley.analysis.trading_summary import SweepSamplingPlan, _load_sweep_sampling_plans, exact_money
from crowley.core.errors import MarketDataNotFound
from crowley.core.house import House

RECIPE_ITEM_ID = 288206
POTION_ITEM_ID = 8827
REQUIRED_ALCHEMY = 180
WATCH_ID = "draught-of-water-walking"
BUILDING_WINDOW_SECONDS = 24 * 60 * 60
RECENT_WINDOW_SECONDS = 6 * 60 * 60
MIN_BASELINE_OBSERVATIONS = 6
DISCOUNT = Fraction(3, 10)
RECIPE_MAX_AGE_SECONDS = 30 * 60


@dataclass(frozen=True)
class RecipeWatchInputs:
    """Pinned, house/build-scoped data needed to render the watch response."""

    house: str
    client_build: str
    sweep_id: int
    finished_epoch: int
    now_epoch: float
    recipe_observation: Mapping[str, Any] | None
    recipe_books: Sequence[ItemBook | Book]
    potion_observation: Mapping[str, Any] | None
    potion_books: Sequence[Book]
    sweep_plans: Mapping[int, SweepSamplingPlan]


def _query_times(observation: Mapping[str, Any] | None) -> tuple[int, int] | None:
    if observation is None:
        return None
    query = observation.get("query")
    if not isinstance(query, Mapping):
        raise ValueError("watched observation query is not an object")
    if query.get("kind") == "unqueried":
        return None
    started, finished = query.get("startedAt"), query.get("finishedAt")
    if query.get("kind") != "queried" or type(started) is not int or type(finished) is not int:
        raise ValueError("watched queried observation has invalid timestamps")
    if started < 0 or finished < started:
        raise ValueError("watched observation query window is invalid")
    return started, finished


def _observation_state(
    observation: Mapping[str, Any] | None,
    *,
    expected_kind: str,
    coverages: frozenset[str],
    require_source_complete: bool,
) -> str:
    if observation is None:
        return "awaiting_scan"
    query = observation.get("query")
    if not isinstance(query, Mapping):
        raise ValueError("watched observation query is not an object")
    if query.get("kind") == "unqueried":
        return "awaiting_scan"
    _query_times(observation)
    if (
        observation.get("kind") != expected_kind
        or observation.get("reason_code") != "ok"
        or observation.get("coverage") not in coverages
        or (require_source_complete and observation.get("source_complete") is not True)
    ):
        return "query_failed"
    return "success"


def _book_for_sweep(books: Sequence[ItemBook | Book], sweep_id: int) -> ItemBook | Book:
    matches = [book for book in books if book.sweep_id == sweep_id]
    if len(matches) != 1:
        raise ValueError(f"expected one watched book for sweep {sweep_id}, found {len(matches)}")
    return matches[0]


def _book_price(book: ItemBook | Book) -> Fraction | None:
    if isinstance(book, ItemBook):
        if len(book.item_keys) != 1:
            return None
        item_key = next(iter(book.item_keys))
        prices = [auction.unit for auction in book.scoped(item_key)]
        return min(prices) if prices else None
    groups = [group for group in book.groups if book.ceiling is None or group.unit <= book.ceiling]
    if not groups:
        return None
    return min(group.unit for group in groups)


def _median(values: Sequence[Fraction]) -> Fraction | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _recipe_response(inputs: RecipeWatchInputs) -> tuple[dict[str, Any], list[str]]:
    observation = inputs.recipe_observation
    if observation is not None and observation.get("kind") == "commodity":
        state = _observation_state(
            observation,
            expected_kind="commodity",
            coverages=frozenset({"full", "bounded"}),
            require_source_complete=False,
        )
    else:
        state = _observation_state(
            observation,
            expected_kind="item",
            coverages=frozenset({"full", "bounded"}),
            require_source_complete=True,
        )
    query_times = _query_times(observation)
    observed_at = None if query_times is None else query_times[1]
    query_sweep_id = inputs.sweep_id if query_times is not None else None

    current_price: Fraction | None = None
    listing_quantity: int | None = None
    baseline_prices: list[Fraction] = []
    variant_ambiguous = False
    if state == "success":
        assert observation is not None and query_times is not None
        current_started = query_times[0]
        current_book = _book_for_sweep(inputs.recipe_books, inputs.sweep_id)
        is_expected_book = (observation.get("kind") == "item" and isinstance(current_book, ItemBook)) or (
            observation.get("kind") == "commodity" and isinstance(current_book, Book)
        )
        if not is_expected_book or int(current_book.t) != current_started:
            raise ValueError("latest recipe book does not match its pinned query")
        current_item_key: str | None = None
        if isinstance(current_book, ItemBook):
            if len(current_book.item_keys) > 1:
                variant_ambiguous = True
                state = "query_failed"
            elif current_book.item_keys:
                current_item_key = next(iter(current_book.item_keys))
                current_price = _book_price(current_book)
                listing_quantity = sum(auction.quantity for auction in current_book.scoped(current_item_key))
            else:
                current_price = None
                listing_quantity = 0
        else:
            current_price = _book_price(current_book)
            listing_quantity = sum(
                group.quantity
                for group in current_book.groups
                if current_book.ceiling is None or group.unit <= current_book.ceiling
            )

        if state == "success":
            seen_sweeps: set[int] = set()
            for book in inputs.recipe_books:
                if (
                    book.sweep_id == inputs.sweep_id
                    or not current_started - BUILDING_WINDOW_SECONDS <= book.t < current_started
                ):
                    continue
                if type(book) is not type(current_book):
                    continue
                if isinstance(book, ItemBook) and (
                    current_item_key is None or book.item_keys != frozenset({current_item_key})
                ):
                    continue
                if type(book.sweep_id) is not int or book.sweep_id <= 0:
                    raise ValueError("recipe price observation has no valid sweep ID")
                if book.sweep_id in seen_sweeps:
                    raise ValueError(f"duplicate recipe price observation for sweep {book.sweep_id}")
                seen_sweeps.add(book.sweep_id)
                price = _book_price(book)
                if price is not None:
                    baseline_prices.append(price)

    baseline_count = len(baseline_prices)
    baseline = _median(baseline_prices) if baseline_count >= MIN_BASELINE_OBSERVATIONS else None
    threshold = None if baseline is None else baseline * (1 - DISCOUNT)
    is_stale = state == "success" and (
        observed_at is None
        or inputs.now_epoch < observed_at
        or inputs.now_epoch - observed_at > RECIPE_MAX_AGE_SECONDS
    )
    if state == "awaiting_scan":
        status = "awaiting_scan"
    elif state == "query_failed":
        status = "query_failed"
    elif is_stale:
        status = "stale"
    elif current_price is None:
        status = "not_listed"
    elif threshold is None:
        status = "building_history"
    elif current_price <= threshold:
        status = "low_price"
    else:
        status = "normal_price"

    recipe = {
        "item_id": RECIPE_ITEM_ID,
        "name": "Recipe: Draught of Water Walking",
        "required_alchemy": REQUIRED_ALCHEMY,
        "status": status,
        "observed_at": observed_at,
        "query_sweep_id": query_sweep_id,
        "current_price": None if current_price is None else exact_money(current_price),
        "baseline_median": None if baseline is None else exact_money(baseline),
        "threshold_price": None if threshold is None else exact_money(threshold),
        "baseline_observations": baseline_count,
        "minimum_baseline_observations": MIN_BASELINE_OBSERVATIONS,
        "baseline_window_hours": 24,
        "discount_fraction": float(DISCOUNT),
        "freshness_max_seconds": RECIPE_MAX_AGE_SECONDS,
        "alert": status == "low_price",
        "listing_quantity": listing_quantity,
    }
    notes = {
        "awaiting_scan": "The latest sweep has not queried the recipe; previous offers are not used.",
        "query_failed": (
            "The latest recipe query did not produce a complete successful book; "
            "older offers do not trigger an alert."
        ),
        "not_listed": (
            "The latest successful recipe query found no current listing."
            if observation is None or observation.get("coverage") == "full"
            else (
                "The latest successful bounded recipe query retained no current listing "
                "in its observed price scope."
            )
        ),
        "building_history": (
            f"The price baseline is building: {baseline_count} of {MIN_BASELINE_OBSERVATIONS} "
            "prior priced observations in 24 hours."
        ),
        "stale": "The latest recipe observation is older than 30 minutes; the low-price alert is suppressed.",
    }.get(status)
    extra_notes = [] if notes is None else [notes]
    if variant_ambiguous:
        extra_notes.append(
            "The latest recipe book contains multiple item-key variants; no single price was selected."
        )
    return recipe, extra_notes


def _pair_max_gap(item_id: int, before: Book, after: Book, plans: Mapping[int, SweepSamplingPlan]) -> float:
    sweep_ids = (before.sweep_id, after.sweep_id)
    if any(type(sweep_id) is not int or sweep_id <= 0 for sweep_id in sweep_ids):
        raise ValueError(f"item {item_id}: commodity observation has no valid sweep ID")
    before_sweep, after_sweep = before.sweep_id, after.sweep_id
    assert type(before_sweep) is int and type(after_sweep) is int
    try:
        seconds = [
            plans[before_sweep].interval_seconds(item_id),
            plans[after_sweep].interval_seconds(item_id),
        ]
    except KeyError as exc:
        raise ValueError(f"sweep {exc.args[0]}: historical sampling plan is unavailable") from exc
    return max(seconds) * 7 / 5


def _demand_window(
    books: Sequence[Book],
    plans: Mapping[int, SweepSamplingPlan],
    *,
    as_of_epoch: int,
    window_seconds: int,
) -> dict[str, int | float | None]:
    start = as_of_epoch - window_seconds
    observed_units = partial_units = disappearance_units = usable_intervals = 0
    covered_seconds = 0.0
    for before, after in zip(books, books[1:], strict=False):
        if before.t < start or after.t > as_of_epoch:
            continue
        interval = valid_intervals(
            (before, after), max_gap=_pair_max_gap(POTION_ITEM_ID, before, after, plans)
        )[0]
        if interval is None or interval.walk.reached_ceiling:
            continue
        before_groups = {group.result_id: group for group in before.groups}
        after_groups = {group.result_id: group for group in after.groups}
        for result_id, _, quantity in interval.walk.consumed_groups:
            observed_units += quantity
            current = after_groups.get(result_id)
            if current is not None and current.quantity > 0:
                partial_units += quantity
            elif result_id in before_groups:
                disappearance_units += quantity
        covered_seconds += interval.dt
        usable_intervals += 1

    usable_hours = covered_seconds / 3600 if covered_seconds > 0 else None
    units_per_hour = (
        round(float(Fraction(observed_units * 3600, 1) / Fraction(str(covered_seconds))), 2)
        if covered_seconds > 0
        else None
    )
    return {
        "window_hours": window_seconds // 3600,
        "observed_units": observed_units,
        "partial_units": partial_units,
        "disappearance_units": disappearance_units,
        "usable_hours": None if usable_hours is None else round(usable_hours, 3),
        "units_per_hour": units_per_hour,
        "usable_intervals": usable_intervals,
    }


def _potion_response(inputs: RecipeWatchInputs) -> tuple[dict[str, Any], list[str]]:
    observation = inputs.potion_observation
    state = _observation_state(
        observation,
        expected_kind="commodity",
        coverages=frozenset({"full", "bounded"}),
        require_source_complete=False,
    )
    query_times = _query_times(observation)
    observed_at = None if query_times is None else query_times[1]
    lowest_price: Fraction | None = None
    retained_supply: int | None = None
    if state == "success":
        assert query_times is not None
        book = _book_for_sweep(inputs.potion_books, inputs.sweep_id)
        if not isinstance(book, Book) or int(book.t) != query_times[0]:
            raise ValueError("latest potion book does not match its pinned query")
        groups = [group for group in book.groups if book.ceiling is None or group.unit <= book.ceiling]
        if groups:
            lowest_price = min(group.unit for group in groups)
            retained_supply = sum(group.quantity for group in groups)
        elif book.ceiling is None:
            retained_supply = 0

    potion = {
        "item_id": POTION_ITEM_ID,
        "name": "Draught of Water Walking",
        "observed_at": observed_at,
        "lowest_price": None if lowest_price is None else exact_money(lowest_price),
        "retained_supply": retained_supply,
        "daily": _demand_window(
            inputs.potion_books,
            inputs.sweep_plans,
            as_of_epoch=inputs.finished_epoch,
            window_seconds=BUILDING_WINDOW_SECONDS,
        ),
        "recent": _demand_window(
            inputs.potion_books,
            inputs.sweep_plans,
            as_of_epoch=inputs.finished_epoch,
            window_seconds=RECENT_WINDOW_SECONDS,
        ),
        "confirmed_sales": None,
        "pilot_batch_units": [1, 2],
    }
    if state == "awaiting_scan":
        notes = ["The latest sweep has not queried the potion; current price and supply are unknown."]
    elif state == "query_failed":
        notes = [
            "The latest potion query did not produce a complete successful book; "
            "current price and supply are unknown."
        ]
    elif retained_supply is None:
        notes = ["Potion supply is incomplete or unavailable in the latest captured book."]
    else:
        notes = []
    return potion, notes


def build_recipe_watch(inputs: RecipeWatchInputs) -> dict[str, Any]:
    """Build the exact token-free response for one pinned sweep and wall-clock time."""

    if not math.isfinite(inputs.now_epoch) or inputs.now_epoch < 0:
        raise ValueError("recipe watch wall-clock time is invalid")
    recipe, recipe_notes = _recipe_response(inputs)
    potion, potion_notes = _potion_response(inputs)
    return {
        "watch_id": WATCH_ID,
        "house": inputs.house,
        "client_build": inputs.client_build,
        "as_of_sweep": inputs.sweep_id,
        "as_of_epoch": inputs.finished_epoch,
        "recipe": recipe,
        "potion": potion,
        "notes": [
            "Recipe prices are sampled approximately every 15 minutes with the market scan. "
            "A low-price alert needs six prior priced "
            "observations in the previous 24 hours and does not include the current observation.",
            "Potion demand is observed listing depletion over usable intervals, not confirmed sales; "
            "full-group disappearances remain ambiguous.",
            "The suggested one-to-two potion pilot is qualitative; personal sales are unmeasured.",
            *recipe_notes,
            *potion_notes,
        ],
    }


def _load_recipe_item_books(
    conn: Any,
    house_key: str,
    client_build: str,
    start_t: int,
    end_t: int,
) -> list[ItemBook]:
    """Load price-only full/bounded item snapshots for the one watched recipe ID."""

    rows = conn.execute(
        """
        SELECT ms.id, mo.query, mcr.result_id, mcr.quantity,
               mcr.unit_numerator, mcr.unit_denominator, mcr.item_key
        FROM houses AS h
        JOIN market_sweeps AS ms ON ms.house_id = h.id
        JOIN market_observations AS mo ON mo.sweep_id = ms.id AND mo.house_id = ms.house_id
        LEFT JOIN market_captured_rows AS mcr ON mcr.sweep_id = mo.sweep_id AND mcr.item_id = mo.item_id
        WHERE h.key = %s
          AND ms.client_version || '.' || ms.client_build = %s
          AND mo.item_id = %s
          AND mo.kind = 'item'
          AND mo.coverage IN ('full', 'bounded')
          AND mo.source_complete
          AND mo.reason_code = 'ok'
          AND mo.query ->> 'kind' = 'queried'
          AND (mo.query ->> 'startedAt')::bigint >= %s
          AND (mo.query ->> 'startedAt')::bigint <= %s
        ORDER BY (mo.query ->> 'startedAt')::bigint, ms.id, mcr.result_id
        """,
        (house_key, client_build, RECIPE_ITEM_ID, start_t, end_t),
    ).fetchall()

    grouped: dict[tuple[int, int], dict[str, Any]] = {}
    for sweep_id, query, result_id, quantity, numerator, denominator, item_key in rows:
        if type(sweep_id) is not int or sweep_id <= 0:
            raise ValueError("recipe item observation has no valid sweep ID")
        if not isinstance(query, Mapping) or type(query.get("startedAt")) is not int:
            raise ValueError(f"sweep {sweep_id}: recipe item query has no integer start time")
        started_at = query["startedAt"]
        key = (RECIPE_ITEM_ID, sweep_id)
        entry = grouped.setdefault(key, {"t": float(started_at), "auctions": [], "result_ids": set()})
        if entry["t"] != float(started_at):
            raise ValueError(f"sweep {sweep_id}: recipe item query time changed within one book")
        if result_id is None:
            continue
        if type(result_id) is not int or type(quantity) is not int or quantity <= 0:
            raise ValueError(f"sweep {sweep_id}: recipe item listing has invalid result ID or quantity")
        if not isinstance(item_key, str) or not item_key:
            raise ValueError(f"sweep {sweep_id}: recipe item listing has no itemKey")
        if result_id in entry["result_ids"]:
            raise ValueError(f"sweep {sweep_id}: duplicate recipe item result ID {result_id}")
        entry["result_ids"].add(result_id)
        if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
            raise ValueError(f"sweep {sweep_id}: recipe item unit price is invalid")
        unit = Fraction(numerator, denominator)
        entry["auctions"].append(Auction(result_id, unit, quantity, item_key, None, False))

    books = [
        ItemBook(
            t=entry["t"], item_id=item_id, sweep_id=sweep_id, auctions=tuple(entry["auctions"]), record={}
        )
        for (item_id, sweep_id), entry in grouped.items()
    ]
    books.sort(key=lambda book: (book.t, book.sweep_id))
    return books


def _latest_observations(
    conn: Any, house_key: str, sweep_id: int, client_build: str
) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT mo.item_id, mo.kind, mo.query, mo.coverage, mo.source_complete, mo.reason_code
        FROM market_sweeps AS ms
        JOIN houses AS h ON h.id = ms.house_id
        JOIN market_observations AS mo ON mo.sweep_id = ms.id AND mo.house_id = ms.house_id
        WHERE h.key = %s
          AND ms.id = %s
          AND ms.client_version || '.' || ms.client_build = %s
          AND mo.item_id = ANY(%s)
        """,
        (house_key, sweep_id, client_build, [RECIPE_ITEM_ID, POTION_ITEM_ID]),
    ).fetchall()
    observations: dict[int, dict[str, Any]] = {}
    for item_id, kind, query, coverage, source_complete, reason_code in rows:
        item_id = int(item_id)
        if item_id in observations:
            raise ValueError(f"sweep {sweep_id}: duplicate watched item observation {item_id}")
        if not isinstance(query, Mapping):
            raise ValueError(f"sweep {sweep_id}: watched item {item_id} query is not an object")
        observations[item_id] = {
            "kind": str(kind),
            "query": query,
            "coverage": str(coverage),
            "source_complete": source_complete,
            "reason_code": str(reason_code),
        }
    return observations


def load_recipe_watch(conn: Any, house_key: str, *, now_epoch: float | None = None) -> dict[str, Any]:
    """Load the latest watch snapshot and its two-item histories read-only."""

    House.parse(house_key)
    now = time.time() if now_epoch is None else now_epoch
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("recipe watch wall-clock time is invalid")

    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        latest = conn.execute(
            """
            SELECT ms.id, EXTRACT(EPOCH FROM ms.finished_at)::bigint,
                   ms.client_version || '.' || ms.client_build
            FROM market_sweeps AS ms
            JOIN houses AS h ON h.id = ms.house_id
            WHERE h.key = %s
            ORDER BY ms.finished_at DESC, ms.id DESC
            LIMIT 1
            """,
            (house_key,),
        ).fetchone()
        if latest is None:
            raise MarketDataNotFound(f"no market sweep for house {house_key!r}")
        sweep_id, finished_epoch, client_build = int(latest[0]), int(latest[1]), str(latest[2])
        observations = _latest_observations(conn, house_key, sweep_id, client_build)
        recipe_observation = observations.get(RECIPE_ITEM_ID)
        potion_observation = observations.get(POTION_ITEM_ID)

        if recipe_observation is not None and recipe_observation.get("kind") == "commodity":
            recipe_state = _observation_state(
                recipe_observation,
                expected_kind="commodity",
                coverages=frozenset({"full", "bounded"}),
                require_source_complete=False,
            )
        else:
            recipe_state = _observation_state(
                recipe_observation,
                expected_kind="item",
                coverages=frozenset({"full", "bounded"}),
                require_source_complete=True,
            )
        recipe_books: Sequence[ItemBook | Book] = []
        recipe_times = _query_times(recipe_observation)
        if recipe_state == "success" and recipe_times is not None:
            if recipe_observation is not None and recipe_observation.get("kind") == "item":
                recipe_books = _load_recipe_item_books(
                    conn,
                    house_key,
                    client_build,
                    max(0, recipe_times[0] - BUILDING_WINDOW_SECONDS),
                    recipe_times[0],
                )

        commodity_item_ids = [POTION_ITEM_ID]
        commodity_start = max(0, finished_epoch - BUILDING_WINDOW_SECONDS)
        if (
            recipe_state == "success"
            and recipe_observation is not None
            and recipe_observation.get("kind") == "commodity"
            and recipe_times is not None
        ):
            commodity_item_ids.append(RECIPE_ITEM_ID)
            commodity_start = min(commodity_start, max(0, recipe_times[0] - BUILDING_WINDOW_SECONDS))
        commodity_books = load_commodity_books(
            conn,
            house_key,
            start_t=commodity_start,
            end_t=finished_epoch,
            client_build=client_build,
            item_ids=commodity_item_ids,
        )
        watched_potion_books = [
            book
            for book in commodity_books.get(POTION_ITEM_ID, [])
            if book.t >= finished_epoch - BUILDING_WINDOW_SECONDS
        ]
        if (
            recipe_state == "success"
            and recipe_observation is not None
            and recipe_observation.get("kind") == "commodity"
        ):
            assert recipe_times is not None
            recipe_books = [
                book
                for book in commodity_books.get(RECIPE_ITEM_ID, [])
                if recipe_times[0] - BUILDING_WINDOW_SECONDS <= book.t <= recipe_times[0]
            ]
        sweep_ids: set[int] = set()
        for book in watched_potion_books:
            if type(book.sweep_id) is not int or book.sweep_id <= 0:
                raise ValueError("potion commodity observation has no valid sweep ID")
            sweep_ids.add(book.sweep_id)
        plans = _load_sweep_sampling_plans(conn, house_key, client_build, sweep_ids)
        return build_recipe_watch(
            RecipeWatchInputs(
                house=house_key,
                client_build=client_build,
                sweep_id=sweep_id,
                finished_epoch=finished_epoch,
                now_epoch=float(now),
                recipe_observation=recipe_observation,
                recipe_books=recipe_books,
                potion_observation=potion_observation,
                potion_books=watched_potion_books,
                sweep_plans=plans,
            )
        )


__all__ = ["RecipeWatchInputs", "build_recipe_watch", "load_recipe_watch"]
