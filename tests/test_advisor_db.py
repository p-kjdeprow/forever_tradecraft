"""Database-gated: the advisor's MarketState loader is scoped to one house, one build and ``as_of``."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from crowley.advisor import AdviceConfig, advise, load_market_state
from crowley.advisor.model import OptionError
from crowley.core.errors import MarketDataNotFound
from crowley.server.db import migrate
from tests.test_analysis_candidates_db import _observe

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]

BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NEW = "1.60.1.70862"
OLD = "1.60.1.70450"
ALLIANCE = "us-normal-alliance"
HORDE = "us-normal-horde"
OUTPUT, REAGENT, HORDE_ONLY = 7001, 7002, 7999
PROFESSIONS = frozenset({"alchemy"})


def _house(conn: psycopg.Connection, key: str, faction: str) -> int:
    return conn.execute(
        "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, 'us', 'normal', %s) RETURNING id",
        (key, faction),
    ).fetchone()[0]


def _sweep(
    conn: psycopg.Connection, house_id: int, tag: str, build: str, minutes: int, cold: list[int] | None
) -> int:
    body = {"itemIDs": [OUTPUT], "pools": {"core": [REAGENT], "discovery": []}}
    if cold is not None:
        body = {
            "coldSlices": 2,
            "itemIDs": [OUTPUT],
            "pools": {"core": [REAGENT], "discovery": [], "cold": cold},
        }
    plan = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    conn.execute(
        "INSERT INTO market_plans (plan_hash, plan_json, plan_body) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (plan, json.dumps(body), Jsonb(body)),
    )
    started = BASE + timedelta(minutes=minutes)
    version, number = build.rsplit(".", 1)
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
            Jsonb({}),
            Jsonb({}),
            hashlib.sha256(f"digest-{tag}".encode()).hexdigest(),
            f"/dev/null/{tag}",
        ),
    ).fetchone()[0]


def _export(conn: psycopg.Connection, build: str, output_sell_price: int) -> None:
    key = f"advisor-{build}-{uuid.uuid4().hex[:6]}"
    export_id = conn.execute(
        """
        INSERT INTO client_data_exports (export_key, client_build, manifest, manifest_sha256)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (key, build, Jsonb({"test": key}), hashlib.sha256(key.encode()).hexdigest()),
    ).fetchone()[0]
    for item_id, name, sell_price in (
        (OUTPUT, "Advisor output", output_sell_price),
        (REAGENT, "Advisor reagent", 1),
        (HORDE_ONLY, "Advisor horde only", 1),
    ):
        conn.execute(
            """
            INSERT INTO client_items
                (export_id, item_id, status, name, quality, item_level, class_id, subclass_id,
                 sell_price, buy_price, vendor_stack_count, bonding, max_stack, required_skill,
                 required_skill_rank)
            VALUES (%s, %s, 'ok', %s, 1, 1, 0, 0, %s, 0, NULL, 0, 20, NULL, NULL)
            """,
            (export_id, item_id, name, sell_price),
        )
    conn.execute(
        "INSERT INTO client_recipes (export_id, spell_id, name, kind, learn_kind, learn_evidence)"
        " VALUES (%s, 9001, 'Advisor craft', 'item', 'skill_line', 'no_teaching_item')",
        (export_id,),
    )
    conn.execute(
        """
        INSERT INTO client_recipe_skill_lines
            (export_id, spell_id, ability_id, skill_line_id, profession, min_rank, yellow,
             green_derived, grey, acquire_method, flags)
        VALUES (%s, 9001, 9001, 171, 'alchemy', 1, 1, 1, 1, 0, 0)
        """,
        (export_id,),
    )
    conn.execute(
        "INSERT INTO client_recipe_reagents (export_id, spell_id, item_id, count) VALUES (%s, 9001, %s, 1)",
        (export_id, REAGENT),
    )
    conn.execute(
        """
        INSERT INTO client_recipe_outputs (export_id, spell_id, item_id, base_quantity, variance, yield_kind)
        VALUES (%s, 9001, %s, 1, 0, 'fixed')
        """,
        (export_id, OUTPUT),
    )


def test_market_state_is_scoped_to_house_build_and_as_of() -> None:
    assert DB_URL is not None
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction(force_rollback=True):
            alliance = _house(conn, ALLIANCE, "alliance")
            horde = _house(conn, HORDE, "horde")
            _export(conn, NEW, output_sell_price=100)
            _export(conn, OLD, output_sell_price=7)

            old = _sweep(conn, alliance, f"a-old-{suffix}", OLD, -60, None)
            _observe(conn, alliance, old, OUTPUT, 1, "commodity", (5,), minutes=-60)
            a_sweeps = []
            for index, minutes in enumerate((0, 15, 30)):
                sweep = _sweep(
                    conn, alliance, f"a-{index}-{suffix}", NEW, minutes, [OUTPUT] if index == 2 else None
                )
                _observe(conn, alliance, sweep, OUTPUT, 1, "commodity", (10 + index, 90), minutes=minutes)
                _observe(conn, alliance, sweep, REAGENT, 2, "commodity", (3,), minutes=minutes)
                a_sweeps.append(sweep)
            h_sweep = _sweep(conn, horde, f"h-{suffix}", NEW, 45, None)
            _observe(conn, horde, h_sweep, OUTPUT, 1, "commodity", (999,), minutes=45)
            _observe(conn, horde, h_sweep, HORDE_ONLY, 2, "commodity", (8,), minutes=45)

            # a character's first capture arrives AFTER the latest sweep (minute 35)
            tag = f"late-{suffix}"
            capture = conn.execute(
                """
                INSERT INTO account_captures (capture_uid, house_id, character, captured_at, raw, content_digest)
                VALUES (%s, %s, 'Dandune', %s, %s, %s) RETURNING id
                """,
                (
                    tag,
                    alliance,
                    BASE + timedelta(minutes=50),
                    Jsonb({"recipes": {"171": {}}, "professions": {}}),
                    hashlib.sha256(tag.encode()).hexdigest(),
                ),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO account_recipes (capture_id, skill_line_id, spell_id) VALUES (%s, 171, 9001)",
                (capture,),
            )

            sweeps_before = conn.execute("SELECT count(*) FROM market_sweeps").fetchone()[0]

            def load(house: str, build: str | None = NEW, as_of: datetime | None = None):
                return load_market_state(
                    conn, house, client_build=build, as_of=as_of, professions=PROFESSIONS, lookback_hours=24.0
                )

            # house and build: only this house's sweeps at this build, latest one is the as-of
            state = load(ALLIANCE)
            assert (state.house_key, state.client_build, state.sweep_id) == (ALLIANCE, NEW, a_sweeps[2])
            assert set(state.books) == {
                OUTPUT,
                REAGENT,
            }  # nothing of the horde house, nothing of the old build
            assert [book.sweep_id for book in state.books[OUTPUT]] == a_sweeps
            assert [min(g.unit for g in book.groups) for book in state.books[OUTPUT]] == [10, 11, 12]
            assert state.as_of_t == (BASE + timedelta(minutes=35)).timestamp()
            assert state.facts[OUTPUT].sell_price == 100 and state.names[OUTPUT] == "Advisor output"
            assert [recipe.spell_id for recipe in state.recipes] == [9001]
            assert state.account is None and state.rules.seller_cut > 0

            # the cold pool comes from the plans of this house's sweeps only, one pool per sweep
            assert state.cold_pool is not None
            assert dict(state.cold_pool.by_sweep) == {
                a_sweeps[0]: frozenset(),
                a_sweeps[1]: frozenset(),
                a_sweeps[2]: frozenset({OUTPUT}),
            }

            # as_of: a later sweep is invisible, in books, cold pool and the current sweep alike
            early = load(ALLIANCE, as_of=BASE + timedelta(minutes=25))
            assert early.sweep_id == a_sweeps[1]
            assert [book.sweep_id for book in early.books[OUTPUT]] == a_sweeps[:2]
            assert early.as_of_t == (BASE + timedelta(minutes=20)).timestamp()
            assert all(book.t <= early.as_of_t for books in early.books.values() for book in books)
            assert early.cold_pool is not None and set(early.cold_pool.by_sweep) == set(a_sweeps[:2])

            # build: the old build's export, facts and book
            older = load(ALLIANCE, build=OLD)
            assert older.client_build == OLD and older.sweep_id == old
            assert [book.sweep_id for book in older.books[OUTPUT]] == [old]
            assert older.facts[OUTPUT].sell_price == 7

            # the other house sees its own books, not the alliance's
            other = load(HORDE)
            assert (other.house_key, other.sweep_id) == (HORDE, h_sweep)
            assert set(other.books) == {OUTPUT, HORDE_ONLY}
            assert [min(g.unit for g in book.groups) for book in other.books[OUTPUT]] == [999]
            assert other.cold_pool is not None and set(other.cold_pool.by_sweep) == {h_sweep}

            # the capture is read at the caller's as_of, not the sweep's: seen after the sweep, and a
            # capture scoped by the sweep (the CLI with --as-of at the sweep) is correctly absent
            seen = load_market_state(
                conn,
                ALLIANCE,
                client_build=NEW,
                as_of=BASE + timedelta(minutes=36),
                character="Dandune",
                professions=PROFESSIONS,
                account_follows_as_of=False,
            )
            assert seen.sweep_id == a_sweeps[2]
            assert seen.account is not None and seen.account.known_spell_ids == {9001}
            with pytest.raises(MarketDataNotFound, match="at or before"):
                load_market_state(
                    conn,
                    ALLIANCE,
                    client_build=NEW,
                    as_of=BASE + timedelta(minutes=36),
                    character="Dandune",
                    professions=PROFESSIONS,
                )

            # nothing before the first sweep, and no such build, fail loudly
            with pytest.raises(MarketDataNotFound, match="no market sweep"):
                load(ALLIANCE, as_of=BASE - timedelta(days=30))
            with pytest.raises(MarketDataNotFound, match="no market sweep"):
                load(ALLIANCE, build="1.60.1.99999")

            # a profession the client export has no recipes for is a bad option, not a bug
            with pytest.raises(OptionError, match="jewelcrafting"):
                load_market_state(
                    conn,
                    ALLIANCE,
                    client_build=NEW,
                    professions=frozenset({"jewelcrafting"}),
                    lookback_hours=24.0,
                )

            # the loaded state feeds every evaluator, and loading wrote nothing
            advice = advise(state, AdviceConfig(professions=PROFESSIONS, batch_crafts=2))
            assert {result.strategy for result in advice.results} == set(AdviceConfig().strategies)
            for result in advice.results:
                assert result.considered == len(result.pathways) + len(result.exclusions)
            assert conn.execute("SELECT count(*) FROM market_sweeps").fetchone()[0] == sweeps_before
