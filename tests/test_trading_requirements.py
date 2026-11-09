from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.analysis.books import Book, Group
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House
from crowley.trading import __main__ as cli
from crowley.trading import paper
from crowley.trading.accounts import Account
from crowley.trading.api import (
    Craft,
    CraftingCapabilities,
    ItemFacts,
    ItemSnapshot,
    Level,
    Recipe,
    RecipeAcquisition,
    SkillRequirement,
    TeachingRequirement,
)
from crowley.trading.data import load_recipes
from crowley.trading.engine import run_backtest
from crowley.trading.execution import ExecutionContext, execute_intent
from crowley.trading.market import Market
from crowley.trading.recipes import validate_recipes
from crowley.trading.requirements import (
    capabilities_from_mapping,
    capabilities_hash,
    recipe_available,
    recipe_gate,
)
from crowley.trading.strategies.crafting import CraftingStrategy

RULES = rules_for_house(House("us", "normal", "alliance"))


def _recipe(**kwargs: object) -> Recipe:
    values: dict[str, object] = {
        "spell_id": 1,
        "professions": frozenset({"engineering"}),
        "reagents": ((10, 1),),
        "outputs": ((11, 1),),
        "cooldown_group": None,
        "cooldown_seconds": 0,
        "learn_evidence": "test",
        "totems": (3938,),
        "totem_categories": (162,),
        "spell_focus": 1,
    }
    values.update(kwargs)
    return Recipe(**values)


def test_validation_preserves_all_requirement_kinds_and_rejects_bool_ids() -> None:
    recipe = validate_recipes((_recipe(),))[0]
    assert recipe.totems == (3938,)
    assert recipe.totem_categories == (162,)
    assert recipe.spell_focus == 1
    with pytest.raises(ValueError, match="positive integer"):
        validate_recipes((_recipe(totems=(True,)),))
    with pytest.raises(ValueError, match="positive integer"):
        validate_recipes((_recipe(totem_categories=(0,)),))
    with pytest.raises(ValueError, match="positive integer"):
        validate_recipes((_recipe(spell_focus=False),))


def test_capabilities_are_canonical_immutable_and_fail_closed() -> None:
    empty = CraftingCapabilities()
    assert capabilities_hash(empty) == "ccdc3b5d95b9c7ec441822f39661fd4b847d22a5f5f21bfd816d81ce8dbc71d1"
    assert not recipe_available(_recipe(), frozenset({"engineering"}), empty)
    complete = CraftingCapabilities.from_values((3938,), (162,), (1,))
    assert recipe_gate(_recipe(), frozenset({"engineering"}), complete) is None
    assert complete.canonical == {
        "spell_focus_ids": [1],
        "tool_category_ids": [162],
        "tool_item_ids": [3938],
    }
    assert capabilities_hash(complete) == capabilities_hash(
        CraftingCapabilities.from_values([3938], [162], [1])
    )


def test_migration_009_hash_literal_matches_runtime_capabilities_hash() -> None:
    migration = (
        Path(__file__).parents[1] / "src" / "crowley" / "server" / "migrations" / "009_paper_capabilities.sql"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"capabilities_hash\s+text\s+NOT NULL\s+DEFAULT\s+'([0-9a-f]{64})'",
        migration,
    )
    assert match is not None, "migration 009 must declare the capabilities hash literal"
    assert match.group(1) == capabilities_hash(CraftingCapabilities())


@pytest.mark.parametrize(
    "bad",
    [
        {"tool_item_ids": [], "tool_category_ids": [], "spell_focus_ids": [], "extra": []},
        {"tool_item_ids": [], "tool_category_ids": [], "spell_focus_ids": None},
        {"tool_item_ids": [], "tool_category_ids": [], "spell_focus_ids": 1},
        {"tool_item_ids": [1, 1], "tool_category_ids": [], "spell_focus_ids": []},
        {"tool_item_ids": [True], "tool_category_ids": [], "spell_focus_ids": []},
    ],
)
def test_capability_json_is_exact_and_strict(bad: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        capabilities_from_mapping(bad)

    with pytest.raises((TypeError, ValueError)):
        CraftingCapabilities.from_values((1, True), (), ())


@pytest.mark.parametrize(
    ("capabilities", "gate"),
    [
        (CraftingCapabilities.from_values((), (162,), (1,)), "tool"),
        (CraftingCapabilities.from_values((3938,), (), (1,)), "category"),
        (CraftingCapabilities.from_values((3938,), (162,), ()), "spell_focus"),
    ],
)
def test_each_missing_requirement_independently_gates_executor(
    capabilities: CraftingCapabilities, gate: str
) -> None:
    context, account = _craft_context(capabilities)
    assert not execute_intent(context, account, Craft(1, 1))
    assert account.rejections == {"capability": 1}
    assert account.ledger[-1].details["gate"] == gate


def test_duplicate_recipe_spells_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate recipe spell id"):
        validate_recipes((_recipe(), _recipe()))


def _skill_line_loader(skill_rows):
    class Cursor:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.one

    class Connection:
        def execute(self, query, _params):
            if "latest_client_export" in query:
                return Cursor(one=(7, "1.0.1"))
            if "FROM client_recipes" in query:
                return Cursor(rows=[(1, 0, 0, None, "no_teaching_item")])
            if "client_recipe_skill_lines" in query:
                return Cursor(rows=skill_rows)
            if "client_recipe_reagents" in query:
                return Cursor(rows=[(1, 10, 1)])
            if "client_recipe_outputs" in query:
                return Cursor(rows=[(1, 11, 1, "fixed")])
            return Cursor()

    return Connection()


def test_loader_collapses_identical_ability_rows_and_rejects_conflicts() -> None:
    # Live 70862 data lists one row per SkillLineAbility; several abilities can
    # teach one spell in the same skill line.
    row = (1, 202, "engineering", 1, 105, 130, 155, 0)
    loaded = load_recipes(_skill_line_loader([row, row]), "house", "1.0.1")
    assert loaded[0].acquisition.skill_lines == (SkillRequirement(202, "engineering", 1, 105, 130, 155, 0),)
    conflicting = (1, 202, "engineering", 5, 105, 130, 155, 0)
    with pytest.raises(ValueError, match="conflicting requirements in skill line 202"):
        load_recipes(_skill_line_loader([row, conflicting]), "house", "1.0.1")


def test_loader_preserves_requirement_rows() -> None:
    class Cursor:
        def __init__(self, rows=(), one=None):
            self.rows = rows
            self.one = one

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.one

    class Connection:
        def execute(self, query, _params):
            if "latest_client_export" in query:
                return Cursor(one=(7, "1.0.1"))
            if "FROM client_recipes" in query:
                return Cursor(rows=[(1, 0, 0, None, "teaching_item_with_item_data")])
            if "client_recipe_skill_lines" in query:
                return Cursor(rows=[(1, 202, "engineering", 100, 110, 120, 130, 1)])
            if "client_recipe_teachers" in query:
                return Cursor(rows=[(1, 55, 202, 300), (1, 77, 202, 225)])
            if "client_recipe_reagents" in query:
                return Cursor(rows=[(1, 10, 1)])
            if "client_recipe_outputs" in query:
                return Cursor(rows=[(1, 11, 1, "fixed")])
            if "client_recipe_totems" in query:
                return Cursor(rows=[(1, 0, 3938)])
            if "client_recipe_totem_categories" in query:
                return Cursor(rows=[(1, 0, 162)])
            if "client_recipe_spell_focus" in query:
                return Cursor(rows=[(1, 1)])
            raise AssertionError(query)

    loaded = load_recipes(Connection(), "house", "1.0.1")
    assert loaded[0].totems == (3938,)
    assert loaded[0].totem_categories == (162,)
    assert loaded[0].spell_focus == 1
    assert loaded[0].acquisition == RecipeAcquisition(
        (55, 77),
        (SkillRequirement(202, "engineering", 100, 110, 120, 130, 1),),
        (TeachingRequirement(55, 202, 300), TeachingRequirement(77, 202, 225)),
    )


def test_reports_bind_capabilities_and_gate_counts() -> None:
    class Strategy:
        name = "crafting_engineering"
        professions = frozenset({"engineering"})
        params = {}

        def decide(self, market, account):
            return []

        def checkpoint(self):
            return {}

        def restore(self, state):
            if state:
                raise ValueError("strategy state must be empty")

    books = {
        11: [
            Book(0, [Group("out", Fraction(20), 1, 1000)], item_id=11, sweep_id=1),
            Book(900, [Group("out", Fraction(20), 1, 1000)], item_id=11, sweep_id=2),
        ]
    }
    facts = {11: ItemFacts(11, 0, None, None, 20, 0)}
    gated = run_backtest(
        books,
        {},
        facts,
        (_recipe(),),
        [Strategy()],
        None,
        1000,
        wind_down_hours=0,
        capabilities=CraftingCapabilities(),
        rules=RULES,
    )
    enabled = run_backtest(
        books,
        {},
        facts,
        (_recipe(),),
        [Strategy()],
        None,
        1000,
        wind_down_hours=0,
        capabilities=CraftingCapabilities.from_values([3938], [162], [1]),
        rules=RULES,
    )
    assert gated.metadata["capabilities_hash"] != enabled.metadata["capabilities_hash"]
    assert gated.metadata["recipe_availability"]["crafting_engineering"]["available"] == 0
    assert enabled.metadata["recipe_availability"]["crafting_engineering"]["available"] == 1


class _StrategyMarket:
    t = 1
    sweep_index = 0
    rules = RULES

    def items(self):
        return {
            10: ItemSnapshot(
                10,
                1,
                (Level(1, 20),),
                1,
                20,
                Fraction(1),
                (Fraction(1), Fraction(1)),
                20,
                0,
                Fraction(20),
                6 * 3600,
                1,
                1,
            ),
            11: ItemSnapshot(
                11,
                1,
                (Level(20, 20),),
                20,
                20,
                Fraction(100),
                (Fraction(90), Fraction(110)),
                20,
                0,
                Fraction(20),
                6 * 3600,
                1,
                1,
            ),
        }

    def facts(self, item_id):
        return (
            ItemFacts(item_id, 0, 1, 1, 20, 0) if item_id == 10 else ItemFacts(item_id, 0, None, None, 20, 0)
        )

    def recipes(self):
        return (_recipe(),)

    def deposit(self, item_id, quantity, duration_hours):
        return 0


class _StrategyAccount:
    name = "craft"
    professions = frozenset({"engineering"})
    crafting_capabilities = CraftingCapabilities.from_values([3938], [162], [1])

    def cash(self):
        return 100_000

    def pending_cash(self):
        return 0

    def inventory(self):
        return {}

    def in_transit(self):
        return {}

    def listings(self):
        return []

    def cooldown_ready(self, group):
        return True


def test_strategy_acquires_reagent_only_when_all_requirements_are_present() -> None:
    intents = CraftingStrategy({"professions": ("engineering",)}).decide(
        _StrategyMarket(), _StrategyAccount()
    )
    assert intents
    assert intents[0].item_id == 10


def test_strategy_profession_mismatch_does_not_acquire_reagent() -> None:
    account = _StrategyAccount()
    account.professions = frozenset({"alchemy"})
    assert CraftingStrategy({"professions": ("engineering",)}).decide(_StrategyMarket(), account) == []


@pytest.mark.parametrize("command", ["backtest", "paper-create"])
@pytest.mark.parametrize("kind", ["missing", "malformed"])
def test_cli_capability_file_errors_happen_before_database_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, kind: str
) -> None:
    path = tmp_path / f"{kind}.json"
    if kind == "malformed":
        path.write_text('{"tool_item_ids": []}', encoding="utf-8")
    monkeypatch.setattr(cli.psycopg, "connect", lambda *_args, **_kwargs: pytest.fail("database connected"))
    args = [
        command,
        "--database-url",
        "postgresql://unused",
        "--house",
        "house",
        "--capabilities-file",
        str(path),
    ]
    if command == "backtest":
        args.extend(["--out", str(tmp_path / "report.json")])
    else:
        args.extend(["--name", "run"])
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2


def test_stored_capabilities_must_be_canonical_even_when_hash_matches() -> None:
    capabilities = CraftingCapabilities.from_values([3938, 4000], [162, 163], [1, 2])
    raw = {
        "tool_item_ids": [4000, 3938],
        "tool_category_ids": [163, 162],
        "spell_focus_ids": [2, 1],
    }
    row = (
        1,
        "run",
        2,
        "house",
        "1.0",
        0,
        100,
        ["vendor_arb"],
        ["1"],
        "1",
        "a" * 64,
        raw,
        capabilities_hash(capabilities),
        "us",
        "normal",
        "alliance",
        "replay-v1",
    )
    with pytest.raises(paper.PaperError, match="canonical"):
        paper._run_from_row(row)


@pytest.mark.parametrize("change", ["identity", "config"])
def test_step_refuses_recreated_or_changed_run_before_writes(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    capabilities = CraftingCapabilities.from_values([3938], [162], [1])
    run = {
        "id": 1,
        "name": "run",
        "house_id": 2,
        "house": "house",
        "client_build": "1.0",
        "start_t": 0,
        "capital_copper": 100,
        "strategies": ["vendor_arb"],
        "weights": ["1"],
        "demand_share": "1",
        "engine_digest": "a" * 64,
        "capabilities": capabilities,
        "capabilities_hash": capabilities_hash(capabilities),
        "engine_contract": "replay-v1",
    }
    live_id = 2 if change == "identity" else 1
    live_capital = 101 if change == "config" else 100
    live_row = (
        live_id,
        "run",
        2,
        "house",
        "1.0",
        0,
        live_capital,
        ["vendor_arb"],
        ["1"],
        "1",
        "a" * 64,
        capabilities.canonical,
        capabilities_hash(capabilities),
        "us",
        "normal",
        "alliance",
        "replay-v1",
    )

    class Connection:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self):
            return self

        def execute(self, query, _params=()):
            self.calls += 1
            assert "FROM paper_runs" in query
            return SimpleNamespace(fetchone=lambda: live_row)

    connection = Connection()
    monkeypatch.setattr(
        paper, "_replay", lambda *_args: (run, SimpleNamespace(step_log=[], accounts={}), 0.1)
    )
    monkeypatch.setattr(paper.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(paper, "engine_digest", lambda: "a" * 64)
    with pytest.raises(paper.PaperError, match="identity|configuration"):
        paper.step_run("unused", "run")
    assert connection.calls == 1


def _craft_context(
    capabilities: CraftingCapabilities, *, recipe: Recipe | None = None
) -> tuple[ExecutionContext, Account]:
    books = {
        11: [
            Book(0, [Group("out", Fraction(20), 1, 1000)], item_id=11, sweep_id=1),
            Book(900, [Group("out", Fraction(20), 1, 1000)], item_id=11, sweep_id=2),
        ]
    }
    facts = {
        10: ItemFacts(10, 0, 1, 1, 20, 0),
        11: ItemFacts(11, 0, None, None, 20, 0),
    }
    market = Market(books, {}, facts, (recipe if recipe is not None else _recipe(),), rules=RULES)
    market.set_step(0, 0, {})
    account = Account(
        "craft",
        frozenset({"engineering"}),
        1000,
        1000,
        crafting_capabilities=capabilities,
    )
    account.add_inventory(10, 1, unit_cost=1)
    return ExecutionContext(market, [account], 0, 0, {}, rules=RULES), account


def test_executor_gate_precedes_reagent_time_and_cooldown_mutation() -> None:
    context, account = _craft_context(CraftingCapabilities())
    before = (
        account.cash_balance,
        dict(account.inventory_balance),
        context.time_used,
        dict(account.cooldowns),
    )
    assert not execute_intent(context, account, Craft(1, 1))
    assert account.rejections == {"capability": 1}
    assert (account.cash_balance, account.inventory_balance, context.time_used, account.cooldowns) == before


def test_executor_allows_capabilities_and_retains_required_tool() -> None:
    context, account = _craft_context(CraftingCapabilities.from_values((3938,), (162,), (1,)))
    assert execute_intent(context, account, Craft(1, 1))
    # Exact tools are explicit pre-held capabilities outside the tradable
    # inventory/lot ledger; the craft does not consume or purchase them.
    assert 3938 not in account.inventory_balance
    assert account.inventory_balance[11] == 1


def test_executor_craft_cost_stays_exact_for_integer_cost_reagent() -> None:
    context, account = _craft_context(
        CraftingCapabilities.from_values((3938,), (162,), (1,)),
        recipe=_recipe(outputs=((11, 2),)),
    )

    assert execute_intent(context, account, Craft(1, 1))
    assert account.lots[11][0].unit_cost == Fraction(1, 2)
    assert isinstance(account.lots[11][0].unit_cost, Fraction)


def test_strategy_does_not_acquire_reagents_for_unavailable_recipe() -> None:
    class MarketView:
        t = 1
        sweep_index = 0
        rules = RULES

        def items(self):
            from crowley.trading.api import ItemSnapshot, Level

            return {
                10: ItemSnapshot(
                    10,
                    1,
                    (Level(1, 20),),
                    1,
                    20,
                    Fraction(1),
                    (Fraction(1), Fraction(1)),
                    20,
                    0,
                    Fraction(20),
                    1000,
                    1,
                    1,
                ),
                11: ItemSnapshot(
                    11,
                    1,
                    (Level(20, 20),),
                    20,
                    20,
                    Fraction(100),
                    (Fraction(90), Fraction(110)),
                    20,
                    0,
                    Fraction(20),
                    1000,
                    1,
                    1,
                ),
            }

        def facts(self, item_id):
            return (
                ItemFacts(item_id, 0, 1, 1, 20, 0)
                if item_id == 10
                else ItemFacts(item_id, 0, None, None, 20, 0)
            )

        def recipes(self):
            return (_recipe(),)

        def deposit(self, item_id, quantity, duration_hours):
            return 0

    class AccountView:
        name = "craft"
        professions = frozenset({"engineering"})
        crafting_capabilities = CraftingCapabilities()

        def cash(self):
            return 100_000

        def pending_cash(self):
            return 0

        def inventory(self):
            return {}

        def in_transit(self):
            return {}

        def listings(self):
            return []

        def cooldown_ready(self, group):
            return True

    assert CraftingStrategy({"professions": ("engineering",)}).decide(MarketView(), AccountView()) == []
