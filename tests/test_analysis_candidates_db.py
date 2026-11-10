"""Database-gated attempted-set handling: what the loaders hand the candidates view as observed."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.analysis.books import load_commodity_books
from crowley.analysis.item_books import load_item_books
from crowley.server.db import migrate

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]

BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
BUILD = "1.60.1.70862"
HOUSE = "us-normal-alliance"
COLD_COMMODITY = 7001
COLD_ITEM = 7002
HOT_COMMODITY = 7003


def _sweep(
    conn: psycopg.Connection,
    house_id: int,
    tag: str,
    minutes: int,
    cold_slice: int,
    plan_body: dict | None = None,
) -> int:
    plan = hashlib.sha256(
        json.dumps(plan_body, sort_keys=True).encode() if plan_body else tag.encode()
    ).hexdigest()
    conn.execute(
        "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (plan, json.dumps(plan_body or {}), Jsonb(plan_body or {})),
    )
    started = BASE + timedelta(minutes=minutes)
    version, number = BUILD.rsplit(".", 1)
    context = {"coldSlice": {"index": cold_slice, "count": 6}}
    return conn.execute(
        """
        INSERT INTO market_sweeps
            (sweep_uid, house_id, plan_hash, started_at, finished_at, character, client_version,
             client_build, interface, house_source, num_rows, num_observations, context, payload,
             content_digest, raw_path)
        VALUES (%s, %s, %s, %s, %s, 'Tester', %s, %s, 1, 'auto', 0, 0, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tag,
            house_id,
            plan,
            started,
            started + timedelta(minutes=5),
            version,
            number,
            Jsonb(context),
            Jsonb({}),
            hashlib.sha256(f"digest-{tag}".encode()).hexdigest(),
            f"/dev/null/{tag}",
        ),
    ).fetchone()[0]


def _observe(
    conn: psycopg.Connection,
    house_id: int,
    sweep_id: int,
    item_id: int,
    ordinal: int,
    kind: str,
    prices: tuple[int, ...],
    *,
    minutes: int,
    coverage: str = "full",
    queried: bool = True,
) -> None:
    query = {
        "kind": "queried",
        "startedAt": int((BASE + timedelta(minutes=minutes)).timestamp()),
        "finishedAt": 2,
    }
    if not queried:
        query = {"kind": "unqueried"}
    conn.execute(
        """
        INSERT INTO market_observations
            (sweep_id, house_id, item_id, item_key, ordinal, kind, query, coverage, source_complete,
             cutoff, higher_price_witness, reason_code, row_count, observation)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ok', %s, %s)
        """,
        (
            sweep_id,
            house_id,
            item_id,
            str(item_id),
            ordinal,
            kind,
            Jsonb(query),
            coverage,
            queried and coverage == "full",
            Jsonb({"kind": "unbounded"}),
            Jsonb({"kind": "unknown"}),
            len(prices),
            Jsonb({}),
        ),
    )
    for index, price in enumerate(prices):
        row_data = {
            "unitPrice": {"numerator": price, "denominator": 1},
            "timeLeft": {"kind": "exact", "seconds": 5000},
            "owners": {
                "totalNumberOfOwners": 1,
                "names": {f"Seller{index}": True},
                "identityKind": "individual",
                "containsOwnerItem": False,
            },
        }
        conn.execute(
            """
            INSERT INTO market_captured_rows
                (sweep_id, item_id, row_key, result_id, item_key, raw_item_key, quantity,
                 unit_numerator, unit_denominator, row_data)
            VALUES (%s, %s, %s, %s, %s, %s, 3, %s, 1, %s)
            """,
            (
                sweep_id,
                item_id,
                f"row:{index}",
                item_id * 100 + index,
                str(item_id),
                Jsonb({"itemID": item_id}),
                price,
                Jsonb(row_data),
            ),
        )


def test_attempted_set_is_the_loaded_observations() -> None:
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house = conn.execute(
                "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, 'us', 'normal', 'alliance') RETURNING id",
                (HOUSE,),
            ).fetchone()[0]
            suffix = uuid.uuid4().hex[:8]
            # Cold slice 0: both cold items attempted, one with listings.
            first = _sweep(conn, house, f"cold-0-{suffix}", 0, 0)
            _observe(conn, house, first, COLD_COMMODITY, 1, "commodity", (50, 60), minutes=0)
            _observe(conn, house, first, COLD_ITEM, 2, "item", (500,), minutes=0)
            _observe(conn, house, first, HOT_COMMODITY, 3, "commodity", (10,), minutes=0)
            # Slice 1: the cold items were not attempted at all (no rows); the hot item was.
            second = _sweep(conn, house, f"cold-1-{suffix}", 15, 1)
            _observe(conn, house, second, HOT_COMMODITY, 1, "commodity", (10,), minutes=15)
            # Slice 0 again, an aborted sweep: the cold items are unqueried tail entries and the
            # hot one is incomplete; none of them is an observation.
            aborted = _sweep(conn, house, f"cold-0b-{suffix}", 90, 0)
            _observe(
                conn,
                house,
                aborted,
                COLD_COMMODITY,
                1,
                "commodity",
                (),
                minutes=90,
                coverage="unqueried",
                queried=False,
            )
            _observe(
                conn,
                house,
                aborted,
                COLD_ITEM,
                2,
                "item",
                (),
                minutes=90,
                coverage="unqueried",
                queried=False,
            )
            _observe(
                conn, house, aborted, HOT_COMMODITY, 3, "commodity", (11,), minutes=90, coverage="incomplete"
            )
            # Slice 0 once more: attempted and empty (a real, empty book).
            last = _sweep(conn, house, f"cold-0c-{suffix}", 180, 0)
            _observe(conn, house, last, COLD_COMMODITY, 1, "commodity", (), minutes=180)
            _observe(conn, house, last, COLD_ITEM, 2, "item", (), minutes=180)

            for start_t, end_t in ((None, None), (0, 2**40)):
                commodities = load_commodity_books(
                    conn, HOUSE, start_t=start_t, end_t=end_t, client_build=BUILD
                )
                cold = commodities[COLD_COMMODITY]
                assert [len(book.groups) for book in cold] == [2, 0]  # attempted-empty is a book
                assert [book.sweep_id for book in cold] == [first, last]  # unqueried and skipped: none
                assert [book.sweep_id for book in commodities[HOT_COMMODITY]] == [
                    first,
                    second,
                ]  # not incomplete

            items = load_item_books(conn, HOUSE, [COLD_ITEM], client_build=BUILD)
            assert [(book.sweep_id, len(book.auctions)) for book in items[COLD_ITEM]] == [
                (first, 1),
                (last, 0),
            ]
            assert load_item_books(conn, HOUSE, [COLD_ITEM], client_build=BUILD, start_t=0, end_t=2**40)

            slices = conn.execute(
                "SELECT context -> 'coldSlice' ->> 'index' FROM market_sweeps WHERE house_id = %s ORDER BY started_at",
                (house,),
            ).fetchall()
            assert [row[0] for row in slices] == ["0", "1", "0", "0"]


OLD_PLAN = {"itemIDs": [COLD_COMMODITY, 2001], "pools": {"core": [COLD_COMMODITY, 2001], "discovery": []}}
COLD_PLAN = {
    "coldSlices": 2,
    "itemIDs": [2001, COLD_COMMODITY],
    "pools": {"core": [2001], "discovery": [], "cold": [COLD_COMMODITY]},
}


def _house_row(conn: psycopg.Connection, key: str, faction: str) -> int:
    return conn.execute(
        "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, 'us', 'normal', %s) RETURNING id",
        (key, faction),
    ).fetchone()[0]


def test_load_candidate_inputs_reads_books_and_per_sweep_cold_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    from crowley.analysis import candidates as cand
    from crowley.analysis.rank import LoadedInputs, RankConfig, RankInputs
    from crowley.core.auction_rules import rules_for_house
    from crowley.core.house import House
    from crowley.trading.api import ItemFacts, Recipe, RecipeAcquisition, SkillRequirement

    assert DB_URL is not None
    config = RankConfig(professions=frozenset({"alchemy"}), batch_crafts=2, min_independent_starts=1)
    craft = Recipe(
        spell_id=1,
        professions=frozenset({"alchemy"}),
        reagents=((2001, 1),),
        outputs=((COLD_COMMODITY, 1),),
        cooldown_group=None,
        cooldown_seconds=0,
        learn_evidence="test",
        acquisition=RecipeAcquisition((), (SkillRequirement(171, "alchemy", 1, 1, 1, 1, 1),), ()),
    )
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            house = _house_row(conn, HOUSE, "alliance")
            other = _house_row(conn, "us-normal-horde", "horde")
            sweeps = []
            for index in range(12):
                minutes = index * 60
                # the first four sweeps ran a plan from before the cold tier, the rest the cold plan
                body = OLD_PLAN if index < 4 else COLD_PLAN
                sweep = _sweep(conn, house, f"e2e-{index}", minutes, index % 2, body)
                sweeps.append(sweep)
                _observe(conn, house, sweep, 2001, 1, "commodity", (10,), minutes=minutes)
                if index < 4 or index % 2 == 0:  # cold slice 0 only after the switch
                    _observe(conn, house, sweep, COLD_COMMODITY, 2, "commodity", (50, 60), minutes=minutes)
            # another house's sweep with a different plan must not leak in
            foreign_plan = {**COLD_PLAN, "pools": {**COLD_PLAN["pools"], "cold": [9]}}
            foreign = _sweep(conn, other, "foreign", 0, 0, foreign_plan)
            _observe(conn, other, foreign, 9, 1, "commodity", (1,), minutes=0)

            last_finish = (BASE + timedelta(minutes=11 * 60 + 5)).timestamp()
            stub = RankInputs(
                house_key=HOUSE,
                as_of_t=last_finish,
                latest_sweep_id=sweeps[-1],
                recipes=(craft,),
                books={},
                facts={
                    COLD_COMMODITY: ItemFacts(COLD_COMMODITY, 10, None, None, 20, 0),
                    2001: ItemFacts(2001, 10, None, None, 20, 0),
                },
                rules=rules_for_house(House.parse(HOUSE)),
                observed_kinds={COLD_COMMODITY: "commodity", 2001: "commodity"},
                item_books={},
            )
            monkeypatch.setattr(
                cand, "load_rank_inputs", lambda *a, **k: LoadedInputs(stub, BUILD, {COLD_COMMODITY: "Cold"})
            )
            loaded = cand.load_candidate_inputs(
                conn, HOUSE, config, client_build=BUILD, character=None, as_of=None
            )
            pool = loaded.cold_pool
            assert set(pool.by_sweep) == set(sweeps)  # this house only
            assert [bool(pool.by_sweep[s]) for s in sweeps] == [False] * 4 + [True] * 8
            assert pool.items == {COLD_COMMODITY} and pool.cold_sweeps(COLD_COMMODITY) == 8

            inputs = loaded.loaded.inputs
            assert len(inputs.books[2001]) == 12
            # attempted by slice: four old-plan sweeps plus every second cold-plan sweep
            assert len(inputs.books[COLD_COMMODITY]) == 4 + 4
            report = cand.build_candidates(inputs, config, pool, min_observations=4)
            assert report.cold_sweeps == {COLD_COMMODITY: 8}
            assert report.population == len(report.candidates) + len(report.excluded) == 1
            (candidate,) = report.candidates
            assert candidate.population[0] == "cold_output"
            assert candidate.outputs[0].observations == 8 and candidate.outputs[0].supply == 6
            assert candidate.outputs[0].target_price is None  # five units a book: no 20th-unit reference

            # a plan that cannot be read fails the load loudly
            bad = _sweep(conn, house, "bad-plan", 10 * 60, 0, {"itemIDs": []})
            with pytest.raises(RuntimeError, match=f"sweep {bad}: stored plan has no pools"):
                cand.load_candidate_inputs(
                    conn, HOUSE, config, client_build=BUILD, character=None, as_of=None
                )
