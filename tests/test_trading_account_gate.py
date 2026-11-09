from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

import psycopg
import pytest

from crowley.trading import __main__ as cli
from crowley.trading.account_gate import (
    AccountSnapshot,
    classify_recipes,
    known_recipes,
    load_account_snapshot,
    require_captured_professions,
)
from crowley.trading.api import Recipe, RecipeAcquisition, SkillRequirement, TeachingRequirement
from crowley.trading.recipes import validate_recipes

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
TAILORING, LEATHER, ENGINEERING, ALCHEMY = 197, 165, 202, 171
NAMES = {TAILORING: "tailoring", LEATHER: "leatherworking", ENGINEERING: "engineering", ALCHEMY: "alchemy"}
T0 = datetime(2026, 1, 2, tzinfo=UTC)


def _recipe(
    spell_id: int,
    lines: tuple[int, ...],
    teachers: tuple[tuple[int, int | None], ...] = (),
) -> Recipe:
    """``teachers`` are (item id, required rank or None when the item has no item data).
    min_rank is 1 on every skill line, as in the live client export."""

    requirements = tuple(
        TeachingRequirement(item, lines[0], rank) for item, rank in teachers if rank is not None
    )
    if not teachers:
        evidence = "no_teaching_item"
    elif requirements:
        evidence = "teaching_item_with_item_data"
    else:
        evidence = "teaching_item_without_item_data"
    return Recipe(
        spell_id=spell_id,
        professions=frozenset(NAMES[line] for line in lines),
        reagents=((10, 1),),
        outputs=((11, 1),),
        cooldown_group=None,
        cooldown_seconds=0,
        learn_evidence=evidence,
        acquisition=RecipeAcquisition(
            tuple(item for item, _ in teachers),
            tuple(SkillRequirement(line, NAMES[line], 1, 1, 1, 1, 1) for line in sorted(lines)),
            requirements,
        ),
    )


def _snapshot(
    known=frozenset({1}),
    skills=None,
    captured=frozenset({ENGINEERING}),
) -> AccountSnapshot:
    skills = {ENGINEERING: 150, ALCHEMY: 50} if skills is None else skills
    return AccountSnapshot(1, "uid-1", T0, "0" * 64, "Char", known, MappingProxyType(skills), captured)


SNAPSHOT = _snapshot()
RECIPES = (
    _recipe(1, (ENGINEERING,), ((5, 300),)),  # known despite insufficient skill
    _recipe(2, (ENGINEERING,), ((6, 100),)),  # item rank 100 <= 150 although min_rank is 1
    _recipe(3, (ENGINEERING,), ((7, 150),)),
    _recipe(4, (ENGINEERING,), ((8, 300),)),  # min_rank 1 would wrongly say learnable
    _recipe(5, (ALCHEMY,), ((9, 1),)),  # profession held but no recipe record captured
    _recipe(6, (TAILORING,), ((9, 1),)),  # profession not held
    _recipe(7, (ENGINEERING,)),  # trainer-taught: no requirement in client data
    _recipe(8, (ENGINEERING,), ((12, None),)),  # teaching item without item data
    _recipe(9, (ENGINEERING,), ((13, 300), (14, 100))),  # any satisfied teaching item suffices
    _recipe(10, (ENGINEERING,), ((13, 300), (15, None))),  # unmet, but an item has no data
)


def test_classification_uses_teaching_item_ranks_and_capture_coverage() -> None:
    result = {c.spell_id: (c.status, c.source) for c in classify_recipes(RECIPES, SNAPSHOT)}
    assert result == {
        1: ("known", None),
        2: ("learnable_now", "teaching_item"),
        3: ("learnable_now", "teaching_item"),
        4: ("needs_skill", None),
        5: ("recipes_not_captured", None),
        6: ("missing_profession", None),
        7: ("requirement_unknown", None),
        8: ("requirement_unknown", None),
        9: ("learnable_now", "teaching_item"),
        10: ("requirement_unknown", None),
    }
    assert [r.spell_id for r in known_recipes(RECIPES, SNAPSHOT)] == [1]


def test_recipe_in_two_professions_uses_whichever_the_character_has() -> None:
    base = _recipe(461692, (TAILORING, LEATHER), ((20, 150),))  # live spell: tailoring + leatherworking
    # The requirement is the teaching item's own skill line, here leatherworking.
    both = replace(
        base,
        acquisition=replace(base.acquisition, teaching_requirements=(TeachingRequirement(20, LEATHER, 150),)),
    )
    only_leather = _snapshot(known=frozenset(), skills={LEATHER: 200}, captured=frozenset({LEATHER}))
    assert classify_recipes((both,), only_leather)[0].status == "learnable_now"
    uncaptured = _snapshot(known=frozenset(), skills={LEATHER: 200, TAILORING: 5}, captured=frozenset())
    assert classify_recipes((both,), uncaptured)[0].status == "recipes_not_captured"
    knows = _snapshot(known=frozenset({461692}), skills={TAILORING: 5}, captured=frozenset({TAILORING}))
    assert classify_recipes((both,), knows)[0].status == "known"
    assert classify_recipes((both,), _snapshot(known=frozenset(), skills={ALCHEMY: 1}))[0].status == (
        "missing_profession"
    )


def test_capture_with_professions_but_no_recipe_records_never_reads_as_knowing_nothing() -> None:
    empty = _snapshot(known=frozenset(), captured=frozenset())
    statuses = {c.status for c in classify_recipes((RECIPES[1], RECIPES[3], RECIPES[6]), empty)}
    assert statuses == {"recipes_not_captured"}
    with pytest.raises(RuntimeError, match="no recipe record for 'engineering'"):
        require_captured_professions(RECIPES, empty, frozenset({"engineering"}))
    require_captured_professions(RECIPES, SNAPSHOT, frozenset({"engineering"}))
    with pytest.raises(RuntimeError, match="no client recipes for profession 'jewelcrafting'"):
        require_captured_professions(RECIPES, SNAPSHOT, frozenset({"jewelcrafting"}))


def test_classifier_rejects_recipe_without_client_skill_data() -> None:
    bare = replace(RECIPES[0], acquisition=RecipeAcquisition())
    with pytest.raises(ValueError, match="no skill requirements"):
        classify_recipes((bare,), SNAPSHOT)


def test_validation_of_acquisition() -> None:
    good = _recipe(1, (ENGINEERING,), ((3, 100), (4, 200)))
    assert validate_recipes((good,), client_data=True)[0].acquisition == good.acquisition
    line = good.acquisition.skill_lines[0]
    reqs = good.acquisition.teaching_requirements

    def acq(**kw):
        values = {
            "teaching_item_ids": (3, 4),
            "skill_lines": (line,),
            "teaching_requirements": reqs,
        }
        values.update(kw)
        return RecipeAcquisition(**values)

    for bad, match in (
        (acq(teaching_item_ids=(4, 3)), "sorted and unique"),
        (acq(teaching_item_ids=(3, 3)), "sorted and unique"),
        (acq(teaching_item_ids=(0, 4)), "positive integer"),
        (acq(skill_lines=(SkillRequirement(202, "engineering", -1, 0, 0, 0, 1),)), "non-negative"),
        (acq(skill_lines=(line, line)), "sorted and unique"),
        (acq(skill_lines=(SkillRequirement(171, "alchemy", 1, 1, 1, 1, 1),)), "do not match"),
        (acq(teaching_requirements=(TeachingRequirement(3, 202, -1),)), "non-negative"),
        (acq(teaching_requirements=(TeachingRequirement(9, 202, 1),)), "teaching requirements"),
        (acq(skill_lines=()), "professions but no skill lines"),
    ):
        with pytest.raises(ValueError, match=match):
            validate_recipes((replace(good, acquisition=bad),), client_data=True)
    # Learn evidence must agree with the teaching items.
    for evidence in ("no_teaching_item", "teaching_item_without_item_data", "test"):
        with pytest.raises(ValueError, match="learn evidence"):
            validate_recipes((replace(good, learn_evidence=evidence),), client_data=True)
    trainer = _recipe(2, (ENGINEERING,))
    with pytest.raises(ValueError, match="contradicts"):
        validate_recipes((replace(trainer, learn_evidence="teaching_item_with_item_data"),), client_data=True)


class _Cursor:
    def __init__(self, rows=(), one=None):
        self.rows, self.one = rows, one

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


def _conn(capture_row, recipes_marker=None):
    class Conn:
        queries: list[tuple[str, tuple]] = []

        def execute(self, query, params):
            self.queries.append((query, params))
            if "account_captures" in query:
                return _Cursor(one=capture_row)
            assert params == (9,)
            if "account_professions" in query:
                return _Cursor(rows=[(202, 150), (171, 50)])
            return _Cursor(rows=[(1,), (2,)])

    return Conn()


def _row(recipes, professions=None):
    return (9, "uid-9", T0, "a" * 64, recipes, professions or [])


def test_loader_reads_capture_rows_and_captured_skill_lines_without_db() -> None:
    conn = _conn(_row({"202": {"capturedAt": 1, "learned": [1, 2]}}))
    snapshot = load_account_snapshot(conn, "house", "Char", "uid-9")
    assert conn.queries[0][1] == ("house", "Char", "uid-9", "uid-9")
    assert snapshot.capture_uid == "uid-9" and snapshot.content_digest == "a" * 64
    assert snapshot.known_spell_ids == {1, 2}
    assert dict(snapshot.skill_levels) == {202: 150, 171: 50}
    assert snapshot.captured_skill_lines == {202}
    empty = load_account_snapshot(_conn(_row({})), "house", "Char")
    assert empty.captured_skill_lines == frozenset()  # professions present, nothing captured


@pytest.mark.parametrize(
    "marker", [{"kind": "error", "detail": "x"}, {"kind": "invalid_native", "field": "f"}]
)
def test_loader_rejects_error_marker_sections(marker) -> None:
    with pytest.raises(RuntimeError, match="recipes section is an error marker"):
        load_account_snapshot(_conn(_row(marker)), "house", "Char")
    with pytest.raises(RuntimeError, match="professions section is an error marker"):
        load_account_snapshot(_conn(_row({}, marker)), "house", "Char")


def test_loader_fails_loudly_without_capture() -> None:
    with pytest.raises(RuntimeError, match="no account capture"):
        load_account_snapshot(_conn(None), "house", "Char")
    with pytest.raises(RuntimeError, match="no account capture 'nope'"):
        load_account_snapshot(_conn(None), "house", "Char", "nope")


def test_cli_gate_is_opt_in_pinnable_and_fails_loudly() -> None:
    engineering = frozenset({"engineering"})
    assert cli._gate_recipes(_conn(None), "house", None, None, engineering, RECIPES) == (RECIPES, None)
    with pytest.raises(ValueError, match="requires --account-character"):
        cli._gate_recipes(_conn(None), "house", None, "uid", engineering, RECIPES)
    with pytest.raises(RuntimeError, match="no account capture"):
        cli._gate_recipes(_conn(None), "house", "Char", None, engineering, RECIPES)
    # Held professions with nothing captured must not silently drop all crafting.
    with pytest.raises(RuntimeError, match="no recipe record for 'engineering'"):
        cli._gate_recipes(_conn(_row({})), "house", "Char", None, engineering, RECIPES)


def test_cli_gate_keeps_only_known_recipes_and_records_provenance() -> None:
    conn = _conn(_row({"202": {"capturedAt": 1, "learned": [1, 2]}}))
    recipes, meta = cli._gate_recipes(
        conn, "house", "Char", None, frozenset({"engineering"}), (RECIPES[0], RECIPES[1], RECIPES[2])
    )
    assert [r.spell_id for r in recipes] == [1, 2]
    assert meta == {
        "character": "Char",
        "capture_uid": "uid-9",
        "captured_at": T0.isoformat(),
        "content_digest": "a" * 64,
        "recipes_before": 3,
        "recipes_after": 2,
    }


@pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")
def test_loader_reads_latest_and_pinned_capture_from_database() -> None:
    from crowley.core.house import House
    from crowley.server.db import migrate
    from crowley.store.accounts import persist_account_state

    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        with conn.transaction():
            house = House("us", "pvp", "horde")
            house_key = house.key
            # Written the way the server writes captures: through persist_account_state, with the
            # envelope's raw recipes section deciding which skill lines count as captured.
            captures = {}
            for n, (day, level, recipes) in enumerate(
                (
                    (1, 50, {"202": {"capturedAt": 1, "learned": [1]}}),
                    (2, 150, {"202": {"capturedAt": 2, "learned": [1, 2]}}),
                    (3, 150, {}),
                )
            ):
                uid = f"{house_key}-Char{suffix}-{day}-000{n}"
                raw = {"id": uid, "recipes": recipes, "professions": []}
                validated = {
                    "id": uid,
                    "context": {"character": f"Char{suffix}"},
                    "bags": {},
                    "mail": {},
                    "cooldowns": {},
                    "recipes": recipes,
                    "professions": [
                        {"name": "Engineering", "skillLineID": 202, "level": level, "maxLevel": 300}
                    ],
                }
                result = persist_account_state(
                    conn,
                    raw,
                    validated,
                    f"{n:064x}",
                    house,
                    datetime(2026, 1, day, tzinfo=UTC),
                    None,
                )
                captures[day] = result
            character = f"Char{suffix}"
            latest = load_account_snapshot(conn, house_key, character)
            assert latest.capture_id == captures[3].id
            assert latest.captured_skill_lines == frozenset()
            assert dict(latest.skill_levels) == {202: 150}
            pinned = load_account_snapshot(conn, house_key, character, captures[2].capture_uid)
            assert pinned.known_spell_ids == {1, 2}
            assert pinned.captured_skill_lines == {202}
            assert json.dumps(pinned.metadata())  # serializable report metadata
            with pytest.raises(RuntimeError, match="no account capture"):
                load_account_snapshot(conn, house_key, "Nobody")
            with pytest.raises(RuntimeError, match="no account capture"):
                load_account_snapshot(conn, house_key, character, "missing-uid")
            conn.execute("DELETE FROM account_captures WHERE character = %s", (character,))
            conn.execute("DELETE FROM houses WHERE key = %s", (house_key,))


def test_teaching_requirement_in_another_skill_line_is_unknown() -> None:
    # Live 70862: First Aid manuals require skill line 2942 while the recipe and
    # the captured profession use 129, so the requirement is not decidable.
    base = _recipe(7935, (ENGINEERING,), ((6454, 150),))
    recipe = replace(
        base,
        acquisition=replace(base.acquisition, teaching_requirements=(TeachingRequirement(6454, 2942, 150),)),
    )
    snapshot = _snapshot(known=frozenset(), skills={ENGINEERING: 300}, captured=frozenset({ENGINEERING}))
    assert classify_recipes([recipe], snapshot)[0].status == "requirement_unknown"


def test_strategy_profession_not_held_is_reported_as_not_held() -> None:
    with pytest.raises(RuntimeError, match="shows no 'tailoring' profession"):
        require_captured_professions(RECIPES, SNAPSHOT, frozenset({"tailoring"}))
