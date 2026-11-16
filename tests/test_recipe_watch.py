"""Pure price-alert, commodity-demand and bounded loader tests for the watch."""

from __future__ import annotations

from contextlib import nullcontext
from fractions import Fraction
from typing import Any

import pytest
from crowley_common.errors import MarketDataNotFound
from crowley_market.analysis import books, recipe_watch
from crowley_market.analysis.books import Book, Group
from crowley_market.analysis.item_books import Auction, ItemBook
from crowley_market.analysis.trading_summary import SweepSamplingPlan

HOUSE = "us-normal-alliance"
BUILD = "1.60.1.70938"
ASOF = 1_000_000
CURRENT_STARTED = ASOF - 10


def _recipe_book(sweep_id: int, started: int, *prices: Fraction | int) -> ItemBook:
    auctions = tuple(
        Auction(index + 1, Fraction(price), 1, "key", "seller", False) for index, price in enumerate(prices)
    )
    return ItemBook(float(started), recipe_watch.RECIPE_ITEM_ID, sweep_id, auctions, {})


def _recipe_observation(
    *,
    started: int = CURRENT_STARTED,
    finished: int = ASOF,
    kind: str = "item",
    coverage: str = "full",
    source_complete: bool = True,
    reason_code: str = "ok",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "query": {"kind": "queried", "startedAt": started, "finishedAt": finished},
        "coverage": coverage,
        "source_complete": source_complete,
        "reason_code": reason_code,
    }


def _potion_observation(
    *,
    started: int = CURRENT_STARTED,
    finished: int = ASOF,
    kind: str = "commodity",
    coverage: str = "full",
    reason_code: str = "ok",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "query": {"kind": "queried", "startedAt": started, "finishedAt": finished},
        "coverage": coverage,
        "source_complete": True,
        "reason_code": reason_code,
    }


def _inputs(
    *,
    recipe_observation: dict[str, Any] | None = None,
    recipe_books: list[ItemBook] | None = None,
    potion_observation: dict[str, Any] | None = None,
    potion_books: list[Book] | None = None,
    plans: dict[int, SweepSamplingPlan] | None = None,
    now: float = ASOF,
) -> recipe_watch.RecipeWatchInputs:
    return recipe_watch.RecipeWatchInputs(
        house=HOUSE,
        client_build=BUILD,
        sweep_id=100,
        finished_epoch=ASOF,
        now_epoch=now,
        recipe_observation=recipe_observation,
        recipe_books=recipe_books or [],
        potion_observation=potion_observation,
        potion_books=potion_books or [],
        sweep_plans=plans or {},
    )


def _priced_recipe_inputs(prices: list[int | Fraction], current: int | Fraction, *, now: float = ASOF):
    books = [
        _recipe_book(index + 1, CURRENT_STARTED - (len(prices) - index) * 900, price)
        for index, price in enumerate(prices)
    ]
    books.append(_recipe_book(100, CURRENT_STARTED, current))
    return _inputs(recipe_observation=_recipe_observation(), recipe_books=books, now=now)


def test_low_price_alert_uses_exact_prior_distinct_sweep_median_and_includes_boundary() -> None:
    response = recipe_watch.build_recipe_watch(_priced_recipe_inputs([100] * 6, 70))
    recipe = response["recipe"]
    assert recipe["status"] == "low_price"
    assert recipe["alert"] is True
    assert recipe["current_price"] == "70"
    assert recipe["baseline_median"] == "100"
    assert recipe["threshold_price"] == "70"
    assert recipe["baseline_observations"] == 6
    assert recipe["query_sweep_id"] == 100


def test_alert_threshold_and_median_keep_fractional_copper_exact() -> None:
    response = recipe_watch.build_recipe_watch(
        _priced_recipe_inputs([99, 100, 101, 102, 103, 104], Fraction(1421, 20))
    )
    recipe = response["recipe"]
    assert recipe["baseline_median"] == "203/2"
    assert recipe["threshold_price"] == "1421/20"
    assert recipe["current_price"] == "1421/20"
    assert recipe["alert"] is True

    above = recipe_watch.build_recipe_watch(
        _priced_recipe_inputs([99, 100, 101, 102, 103, 104], Fraction(71051, 1000))
    )["recipe"]
    assert above["status"] == "normal_price"
    assert above["alert"] is False


def test_current_observation_is_excluded_from_baseline() -> None:
    books = [_recipe_book(index + 1, CURRENT_STARTED - index * 900, 100) for index in range(1, 6)]
    books.append(_recipe_book(100, CURRENT_STARTED, 1))
    recipe = recipe_watch.build_recipe_watch(
        _inputs(recipe_observation=_recipe_observation(), recipe_books=books)
    )["recipe"]
    assert recipe["baseline_observations"] == 5
    assert recipe["baseline_median"] is None
    assert recipe["threshold_price"] is None
    assert recipe["status"] == "building_history"
    assert recipe["alert"] is False


def test_baseline_requires_six_previous_priced_observations_in_the_prior_day() -> None:
    prices = [100] * 5
    books = [
        _recipe_book(index + 1, CURRENT_STARTED - (index + 1) * 900, price)
        for index, price in enumerate(prices)
    ]
    books.append(_recipe_book(99, CURRENT_STARTED - recipe_watch.BUILDING_WINDOW_SECONDS - 1, 100))
    books.append(_recipe_book(100, CURRENT_STARTED, 1))
    recipe = recipe_watch.build_recipe_watch(
        _inputs(recipe_observation=_recipe_observation(), recipe_books=books)
    )["recipe"]
    assert recipe["baseline_observations"] == 5
    assert recipe["status"] == "building_history"
    assert recipe["current_price"] == "1"
    assert recipe["alert"] is False


def test_latest_missing_unqueried_or_failed_query_never_reuses_an_older_offer() -> None:
    old = _recipe_book(1, CURRENT_STARTED - 900, 1)
    for observation, expected in [
        (None, "awaiting_scan"),
        (
            {
                "kind": "item",
                "query": {"kind": "unqueried"},
                "coverage": "unqueried",
                "source_complete": False,
                "reason_code": "not_started",
            },
            "awaiting_scan",
        ),
        (
            _recipe_observation(coverage="incomplete", source_complete=False, reason_code="query_timeout"),
            "query_failed",
        ),
    ]:
        recipe = recipe_watch.build_recipe_watch(_inputs(recipe_observation=observation, recipe_books=[old]))[
            "recipe"
        ]
        assert recipe["status"] == expected
        assert recipe["current_price"] is None
        assert recipe["listing_quantity"] is None
        assert recipe["alert"] is False


def test_empty_recipe_book_is_not_listed_and_stale_quote_never_alerts() -> None:
    empty = _recipe_book(100, CURRENT_STARTED)
    not_listed = recipe_watch.build_recipe_watch(
        _inputs(recipe_observation=_recipe_observation(), recipe_books=[empty])
    )["recipe"]
    assert not_listed["status"] == "not_listed"
    assert not_listed["current_price"] is None
    assert not_listed["listing_quantity"] == 0

    stale_inputs = _priced_recipe_inputs([100] * 6, 1, now=ASOF + recipe_watch.RECIPE_MAX_AGE_SECONDS + 1)
    stale = recipe_watch.build_recipe_watch(stale_inputs)["recipe"]
    assert stale["status"] == "stale"
    assert stale["current_price"] == "1"
    assert stale["alert"] is False


def test_recipe_freshness_boundary_is_inclusive() -> None:
    inputs = _priced_recipe_inputs([100] * 6, 70, now=ASOF + recipe_watch.RECIPE_MAX_AGE_SECONDS)
    recipe = recipe_watch.build_recipe_watch(inputs)["recipe"]
    assert recipe["status"] == "low_price"
    assert recipe["alert"] is True


def test_commodity_recipe_uses_commodity_floor_history_without_mixing_books() -> None:
    books = [
        Book(
            float(CURRENT_STARTED - index * 900),
            [Group(f"old-{index}", Fraction(100), 1, 100_000)],
            Fraction(200),
            recipe_watch.RECIPE_ITEM_ID,
            index + 1,
        )
        for index in range(1, 7)
    ]
    books.append(
        Book(
            float(CURRENT_STARTED),
            [
                Group("current", Fraction(70), 1, 100_000),
                Group("outside-cutoff", Fraction(101), 8, 100_000),
            ],
            Fraction(100),
            recipe_watch.RECIPE_ITEM_ID,
            100,
        )
    )
    observation = _recipe_observation(kind="commodity", coverage="bounded", source_complete=False)
    recipe = recipe_watch.build_recipe_watch(_inputs(recipe_observation=observation, recipe_books=books))[
        "recipe"
    ]
    assert recipe["status"] == "low_price"
    assert recipe["alert"] is True
    assert recipe["baseline_observations"] == 6
    assert recipe["current_price"] == "70"
    assert recipe["listing_quantity"] == 1


def test_empty_bounded_commodity_recipe_is_not_claimed_absent_from_market() -> None:
    current = Book(float(CURRENT_STARTED), [], Fraction(100), recipe_watch.RECIPE_ITEM_ID, 100)
    recipe = recipe_watch.build_recipe_watch(
        _inputs(
            recipe_observation=_recipe_observation(kind="commodity", coverage="bounded"),
            recipe_books=[current],
        )
    )["recipe"]
    assert recipe["status"] == "not_listed"
    assert recipe["current_price"] is None


def test_item_recipe_variants_are_not_mixed_for_current_price_or_baseline() -> None:
    prior = [_recipe_book(index + 1, CURRENT_STARTED - index * 900, 1) for index in range(1, 7)]
    prior = [
        ItemBook(
            book.t,
            book.item_id,
            book.sweep_id,
            tuple(
                Auction(a.result_id, a.unit, a.quantity, "prior-key", a.owner, a.own) for a in book.auctions
            ),
            book.record,
        )
        for book in prior
    ]
    current = ItemBook(
        float(CURRENT_STARTED),
        recipe_watch.RECIPE_ITEM_ID,
        100,
        (
            Auction(1, Fraction(1), 1, "variant-a", "seller-a", False),
            Auction(2, Fraction(1), 1, "variant-b", "seller-b", False),
        ),
        {},
    )
    recipe = recipe_watch.build_recipe_watch(
        _inputs(recipe_observation=_recipe_observation(), recipe_books=[*prior, current])
    )["recipe"]
    assert recipe["status"] == "query_failed"
    assert recipe["current_price"] is None
    assert recipe["baseline_observations"] == 0
    assert recipe["alert"] is False


def _potion_book(sweep: int, at: int, groups: list[Group], *, ceiling=None) -> Book:
    return Book(float(at), groups, ceiling, recipe_watch.POTION_ITEM_ID, sweep)


def test_demand_splits_partial_reductions_from_whole_group_disappearances() -> None:
    books = [
        _potion_book(
            1,
            ASOF - 1800,
            [
                Group("whole", Fraction(100), 3, 100_000),
                Group("partial", Fraction(200), 10, 100_000),
            ],
        ),
        _potion_book(2, ASOF - 900, [Group("partial", Fraction(200), 6, 100_000)]),
        _potion_book(3, ASOF, [Group("partial", Fraction(200), 6, 100_000)]),
    ]
    plans = {sweep: SweepSamplingPlan(900, 0, frozenset()) for sweep in (1, 2, 3)}
    response = recipe_watch.build_recipe_watch(_inputs(potion_books=books, plans=plans))
    daily = response["potion"]["daily"]
    assert daily == {
        "window_hours": 24,
        "observed_units": 7,
        "partial_units": 4,
        "disappearance_units": 3,
        "usable_hours": 0.5,
        "units_per_hour": 14.0,
        "usable_intervals": 2,
    }
    assert response["potion"]["confirmed_sales"] is None
    assert response["potion"]["pilot_batch_units"] == [1, 2]


def test_expiry_possible_group_is_skipped_but_interval_is_measured_zero() -> None:
    books = [
        _potion_book(1, ASOF - 900, [Group("expiring", Fraction(100), 5, 900)]),
        _potion_book(2, ASOF, []),
    ]
    plans = {sweep: SweepSamplingPlan(900, 0, frozenset()) for sweep in (1, 2)}
    daily = recipe_watch.build_recipe_watch(_inputs(potion_books=books, plans=plans))["potion"]["daily"]
    assert daily["observed_units"] == 0
    assert daily["partial_units"] == 0
    assert daily["disappearance_units"] == 0
    assert daily["usable_hours"] == 0.25
    assert daily["units_per_hour"] == 0.0
    assert daily["usable_intervals"] == 1


@pytest.mark.parametrize(
    ("books", "plans", "expected_hours"),
    [
        (
            [
                _potion_book(
                    1,
                    ASOF - 900,
                    [Group("ceiling", Fraction(100), 10, 100_000)],
                    ceiling=Fraction(100),
                ),
                _potion_book(2, ASOF, [], ceiling=Fraction(100)),
            ],
            {1: SweepSamplingPlan(900, 0, frozenset()), 2: SweepSamplingPlan(900, 0, frozenset())},
            None,
        ),
        (
            [
                _potion_book(1, ASOF - 2000, [Group("level", Fraction(100), 10, 100_000)]),
                _potion_book(2, ASOF, [Group("level", Fraction(100), 5, 100_000)]),
            ],
            {1: SweepSamplingPlan(900, 0, frozenset()), 2: SweepSamplingPlan(900, 0, frozenset())},
            None,
        ),
    ],
)
def test_bounded_ceiling_and_unsupported_gap_are_not_demand_evidence(books, plans, expected_hours) -> None:
    daily = recipe_watch.build_recipe_watch(_inputs(potion_books=books, plans=plans))["potion"]["daily"]
    assert daily["usable_hours"] is expected_hours
    assert daily["observed_units"] == 0
    assert daily["units_per_hour"] is None


def test_cold_pair_gap_uses_both_immutable_sweep_plans() -> None:
    books = [
        _potion_book(1, ASOF - 5000, [Group("level", Fraction(100), 10, 100_000)]),
        _potion_book(2, ASOF, [Group("level", Fraction(100), 6, 100_000)]),
    ]
    plans = {
        1: SweepSamplingPlan(900, 6, frozenset({recipe_watch.POTION_ITEM_ID})),
        2: SweepSamplingPlan(900, 0, frozenset()),
    }
    daily = recipe_watch.build_recipe_watch(_inputs(potion_books=books, plans=plans))["potion"]["daily"]
    assert daily["observed_units"] == 4
    assert daily["usable_hours"] == round(5000 / 3600, 3)
    assert daily["usable_intervals"] == 1


def test_no_usable_interval_is_unknown_not_measured_zero() -> None:
    daily = recipe_watch.build_recipe_watch(_inputs())["potion"]["daily"]
    assert daily["observed_units"] == 0
    assert daily["usable_hours"] is None
    assert daily["units_per_hour"] is None
    assert daily["usable_intervals"] == 0


def test_latest_potion_query_failed_clears_current_price_but_keeps_prior_activity() -> None:
    books = [
        _potion_book(1, ASOF - 900, [Group("level", Fraction(100), 10, 100_000)]),
        _potion_book(2, ASOF, [Group("level", Fraction(100), 8, 100_000)]),
    ]
    plans = {sweep: SweepSamplingPlan(900, 0, frozenset()) for sweep in (1, 2)}
    response = recipe_watch.build_recipe_watch(
        _inputs(
            potion_observation=_potion_observation(coverage="incomplete", reason_code="query_timeout"),
            potion_books=books,
            plans=plans,
        )
    )
    potion = response["potion"]
    assert potion["observed_at"] == ASOF
    assert potion["lowest_price"] is None
    assert potion["retained_supply"] is None
    assert potion["daily"]["observed_units"] == 2
    assert any("latest potion query" in note for note in response["notes"])


def test_current_potion_listing_price_and_retained_supply_are_reported() -> None:
    current = _potion_book(
        100,
        CURRENT_STARTED,
        [Group("a", Fraction(5, 2), 3, 100_000), Group("b", Fraction(3), 4, 100_000)],
    )
    potion = recipe_watch.build_recipe_watch(
        _inputs(potion_observation=_potion_observation(), potion_books=[current])
    )["potion"]
    assert potion["observed_at"] == ASOF
    assert potion["lowest_price"] == "5/2"
    assert potion["retained_supply"] == 7


def test_loader_pins_latest_house_build_and_filters_commodity_query_to_potion(monkeypatch) -> None:
    statements: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Conn:
        def transaction(self):
            return nullcontext(self)

        def execute(self, sql, params=None):
            statements.append((sql, params))
            if sql.startswith("SET TRANSACTION"):
                return Result()
            if "ORDER BY ms.finished_at DESC" in sql:
                return Result(one=(100, ASOF, BUILD))
            if "JOIN market_observations AS mo" in sql:
                return Result(
                    rows=[
                        (
                            recipe_watch.RECIPE_ITEM_ID,
                            "item",
                            {"kind": "unqueried"},
                            "unqueried",
                            False,
                            "not_started",
                        ),
                        (
                            recipe_watch.POTION_ITEM_ID,
                            "commodity",
                            {"kind": "unqueried"},
                            "unqueried",
                            False,
                            "not_started",
                        ),
                    ]
                )
            raise AssertionError(f"unexpected SQL: {sql}")

    captured: dict[str, Any] = {}

    def commodity_loader(conn, house_key, **kwargs):
        captured["house"] = house_key
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(recipe_watch, "load_commodity_books", commodity_loader)
    monkeypatch.setattr(recipe_watch, "_load_sweep_sampling_plans", lambda conn, house, build, ids: {})
    response = recipe_watch.load_recipe_watch(Conn(), HOUSE, now_epoch=ASOF)
    assert response["as_of_sweep"] == 100
    assert response["recipe"]["status"] == "awaiting_scan"
    assert captured == {
        "house": HOUSE,
        "start_t": ASOF - recipe_watch.BUILDING_WINDOW_SECONDS,
        "end_t": ASOF,
        "client_build": BUILD,
        "item_ids": [recipe_watch.POTION_ITEM_ID],
    }
    assert statements[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    observation_sql, observation_params = statements[2]
    assert "h.key = %s" in observation_sql
    assert "ms.id = %s" in observation_sql
    assert "ms.client_version || '.' || ms.client_build = %s" in observation_sql
    assert "mo.item_id = ANY(%s)" in observation_sql
    assert observation_params == (
        HOUSE,
        100,
        BUILD,
        [recipe_watch.RECIPE_ITEM_ID, recipe_watch.POTION_ITEM_ID],
    )


def test_loader_reads_recipe_history_as_commodity_when_latest_observation_says_commodity(monkeypatch) -> None:
    class Result:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Conn:
        def transaction(self):
            return nullcontext(self)

        def execute(self, sql, params=None):
            if sql.startswith("SET TRANSACTION"):
                return Result()
            if "ORDER BY ms.finished_at DESC" in sql:
                return Result(one=(100, ASOF, BUILD))
            if "JOIN market_observations AS mo" in sql:
                return Result(
                    rows=[
                        (
                            recipe_watch.RECIPE_ITEM_ID,
                            "commodity",
                            {"kind": "queried", "startedAt": CURRENT_STARTED, "finishedAt": ASOF},
                            "full",
                            True,
                            "ok",
                        ),
                        (
                            recipe_watch.POTION_ITEM_ID,
                            "commodity",
                            {"kind": "unqueried"},
                            "unqueried",
                            False,
                            "not_started",
                        ),
                    ]
                )
            raise AssertionError(f"unexpected SQL: {sql}")

    called: dict[str, Any] = {}
    current = Book(
        float(CURRENT_STARTED),
        [Group("recipe", Fraction(75), 2, 100_000)],
        None,
        recipe_watch.RECIPE_ITEM_ID,
        100,
    )

    def commodity_loader(conn, house_key, **kwargs):
        called["ids"] = kwargs["item_ids"]
        called["house"] = house_key
        return {recipe_watch.RECIPE_ITEM_ID: [current], recipe_watch.POTION_ITEM_ID: []}

    monkeypatch.setattr(recipe_watch, "load_commodity_books", commodity_loader)
    monkeypatch.setattr(recipe_watch, "_load_sweep_sampling_plans", lambda conn, house, build, ids: {})
    response = recipe_watch.load_recipe_watch(Conn(), HOUSE, now_epoch=ASOF)
    assert called == {"house": HOUSE, "ids": [recipe_watch.POTION_ITEM_ID, recipe_watch.RECIPE_ITEM_ID]}
    assert response["recipe"]["current_price"] == "75"
    assert response["recipe"]["listing_quantity"] == 2
    assert response["recipe"]["status"] == "building_history"


def test_loader_uses_captured_floor_for_bounded_item_recipe_history(monkeypatch) -> None:
    statements: list[tuple[str, tuple[object, ...] | None]] = []
    prior_rows = [
        (
            index + 1,
            {"kind": "queried", "startedAt": CURRENT_STARTED - (index + 1) * 900},
            index + 1,
            1,
            100,
            1,
            "recipe-key",
        )
        for index in range(6)
    ]
    current_rows = [
        (
            100,
            {"kind": "queried", "startedAt": CURRENT_STARTED},
            100,
            1,
            70,
            1,
            "recipe-key",
        )
    ]

    class Result:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Conn:
        def transaction(self):
            return nullcontext(self)

        def execute(self, sql, params=None):
            statements.append((sql, params))
            if sql.startswith("SET TRANSACTION"):
                return Result()
            if "ORDER BY ms.finished_at DESC" in sql:
                return Result(one=(100, ASOF, BUILD))
            if "JOIN market_observations AS mo" in sql and "mo.item_id = ANY(%s)" in sql:
                return Result(
                    rows=[
                        (
                            recipe_watch.RECIPE_ITEM_ID,
                            "item",
                            {"kind": "queried", "startedAt": CURRENT_STARTED, "finishedAt": ASOF},
                            "bounded",
                            True,
                            "ok",
                        ),
                        (
                            recipe_watch.POTION_ITEM_ID,
                            "commodity",
                            {"kind": "unqueried"},
                            "unqueried",
                            False,
                            "not_started",
                        ),
                    ]
                )
            if "mo.coverage IN ('full', 'bounded')" in sql and "mo.item_id = %s" in sql:
                return Result(rows=[*prior_rows, *current_rows])
            raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(recipe_watch, "load_commodity_books", lambda conn, house, **kwargs: {})
    monkeypatch.setattr(recipe_watch, "_load_sweep_sampling_plans", lambda conn, house, build, ids: {})
    response = recipe_watch.load_recipe_watch(Conn(), HOUSE, now_epoch=ASOF)
    recipe = response["recipe"]
    assert recipe["status"] == "low_price"
    assert recipe["current_price"] == "70"
    assert recipe["baseline_observations"] == 6
    query_sql, query_params = next(
        (sql, params) for sql, params in statements if "mo.coverage IN ('full', 'bounded')" in sql
    )
    assert "mo.item_id = %s" in query_sql
    assert "mo.source_complete" in query_sql
    assert "mo.reason_code = 'ok'" in query_sql
    assert query_params == (
        HOUSE,
        BUILD,
        recipe_watch.RECIPE_ITEM_ID,
        CURRENT_STARTED - 86_400,
        CURRENT_STARTED,
    )


def test_loader_without_latest_sweep_returns_not_found() -> None:
    class Result:
        def fetchone(self):
            return None

    class Conn:
        def transaction(self):
            return nullcontext(self)

        def execute(self, sql, params=None):
            return Result()

    with pytest.raises(MarketDataNotFound):
        recipe_watch.load_recipe_watch(Conn(), HOUSE, now_epoch=ASOF)


def test_commodity_loader_item_id_scope_is_optional_and_validated() -> None:
    seen: list[tuple[str, tuple[object, ...] | None]] = []

    class Result:
        def fetchone(self):
            return (1,)

        def fetchall(self):
            return []

    class Conn:
        def transaction(self):
            return nullcontext(self)

        def execute(self, sql, params=None):
            seen.append((sql, params))
            return Result()

    books.load_commodity_books(Conn(), HOUSE, client_build=BUILD, item_ids=[recipe_watch.POTION_ITEM_ID])
    query_sql, query_params = seen[2]
    assert "mo.item_id = ANY(%s)" in query_sql
    assert query_params == (HOUSE, BUILD, [recipe_watch.POTION_ITEM_ID])

    seen.clear()
    assert books.load_commodity_books(Conn(), HOUSE, item_ids=[]) == {}
    assert seen == []
    with pytest.raises(ValueError, match="positive integers"):
        books.load_commodity_books(Conn(), HOUSE, item_ids=[True])
