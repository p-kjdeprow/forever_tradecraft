"""Persistent deterministic paper-trading runs over recorded market sweeps."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House

from .api import CraftingCapabilities
from .data import load_client_data, load_commodity_books, precompute_statistics
from .engine import BacktestResult, run_backtest
from .requirements import EMPTY_CAPABILITIES, capabilities_from_mapping, capabilities_hash
from .strategies import build

DEFAULT_STRATEGIES = (
    "crafting_alchemy",
    "crafting_engineering",
    "dip_reversion",
    "vendor_arb",
)

PAPER_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "pr.id"),
    ("name", "pr.name"),
    ("house_id", "pr.house_id"),
    ("house", "h.key"),
    ("client_build", "pr.client_build"),
    ("start_t", "pr.start_t"),
    ("capital_copper", "pr.capital_copper"),
    ("strategies", "pr.strategies"),
    ("weights", "pr.weights"),
    ("demand_share", "pr.demand_share"),
    ("engine_digest", "pr.engine_digest"),
    ("capabilities", "pr.capabilities"),
    ("capabilities_hash", "pr.capabilities_hash"),
    ("region", "h.region"),
    ("ruleset", "h.ruleset"),
    ("faction", "h.faction"),
)
PAPER_RUN_COLUMN_INDEX = {name: index for index, (name, _expression) in enumerate(PAPER_RUN_COLUMNS)}
PAPER_RUN_SELECT = ", ".join(f"{expression} AS {name}" for name, expression in PAPER_RUN_COLUMNS)


class PaperError(RuntimeError):
    """A paper run cannot be created or advanced without operator action."""


class _ReadOnlyLoaderConnection:
    """Reuse the caller's repeatable read-only transaction for analysis loaders."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext(self._connection)


def database_url(value: str | None = None) -> str:
    resolved = value or os.environ.get("CROWLEY_DATABASE_URL")
    if not resolved:
        raise PaperError("database URL is required (--database-url or CROWLEY_DATABASE_URL)")
    return resolved


def _fraction(value: Any) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _row(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def _full_build(row: Any) -> str:
    return f"{_row(row, 'client_version', 0)}.{_row(row, 'client_build', 1)}"


def _paper_run_value(row: Any, column: str) -> Any:
    return _row(row, column, PAPER_RUN_COLUMN_INDEX[column])


def _build_rows(conn: Any, house_id: int) -> list[Any]:
    return conn.execute(
        """
        SELECT ms.client_version, ms.client_build, ms.id,
               MAX((mo.query ->> 'startedAt')::bigint) AS step_t
        FROM market_sweeps AS ms
        JOIN market_observations AS mo ON mo.sweep_id = ms.id
            AND mo.house_id = ms.house_id
        WHERE ms.house_id = %s
          AND mo.kind = 'commodity'
          AND mo.coverage IN ('full', 'bounded')
        GROUP BY ms.id, ms.client_version, ms.client_build
        ORDER BY step_t, ms.id
        """,
        (house_id,),
    ).fetchall()


def _source_digest() -> str:
    """Hash every Python source file that can affect a trading replay."""

    source_root = Path(__file__).resolve().parents[2]
    directories = (
        source_root / "crowley" / "trading",
        source_root / "crowley" / "analysis",
    )
    paths = [path for directory in directories for path in directory.rglob("*.py") if path.is_file()]
    paths.extend(
        (
            source_root / "crowley" / "core" / "auction_rules.py",
            source_root / "crowley" / "core" / "house.py",
        )
    )
    paths.sort()
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


engine_digest = _source_digest


def _parse_strategies(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        names = list(DEFAULT_STRATEGIES)
    elif isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    else:
        names = [str(part).strip() for part in value]
    if not names or any(not name for name in names):
        raise ValueError("strategies must be a comma-separated list")
    if len(set(names)) != len(names):
        raise ValueError("strategy names must be unique")
    for name in names:
        build(name)
    return names


def _parse_weights(value: str | Sequence[Any] | None, count: int) -> list[Fraction]:
    if value is None:
        weights = [Fraction(1) for _ in range(count)]
    elif isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if not parts or any(not part for part in parts):
            raise ValueError("weights must be a comma-separated list")
        weights = [_fraction(part) for part in parts]
    else:
        weights = [_fraction(part) for part in value]
    if len(weights) != count:
        raise ValueError("weights must have one value per strategy")
    if any(weight <= 0 for weight in weights):
        raise ValueError("weights must be positive")
    return weights


def _capital_copper(value: Any) -> int:
    capital = Decimal(str(value)) * 10_000
    if capital != capital.to_integral_value() or capital < 0:
        raise ValueError("capital-gold must be a non-negative amount of copper")
    return int(capital)


def _parse_start(value: str | int | float | None) -> int:
    if value is None or str(value).lower() == "now":
        return int(time.time())
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError("start must be an epoch integer or 'now'") from exc
    if parsed < 0:
        raise ValueError("start must be non-negative")
    return parsed


def create_run(
    db_url: str,
    *,
    name: str,
    house: str,
    start: str | int | float | None = None,
    capital_gold: Any = Decimal("20"),
    strategies: str | Sequence[str] | None = None,
    weights: str | Sequence[Any] | None = None,
    demand_share: Any = Fraction("0.5"),
    client_build: str | None = None,
    capabilities: CraftingCapabilities | Mapping[str, Any] | None = None,
) -> int:
    if not name:
        raise ValueError("name must not be empty")
    names = _parse_strategies(strategies)
    parsed_weights = _parse_weights(weights, len(names))
    parsed_demand_share = _fraction(demand_share)
    if not 0 < parsed_demand_share <= 1:
        raise ValueError("demand-share must be in (0, 1]")
    start_t = _parse_start(start)
    capital = _capital_copper(capital_gold)
    digest = engine_digest()
    if capabilities is None:
        parsed_capabilities = EMPTY_CAPABILITIES
    elif isinstance(capabilities, CraftingCapabilities):
        parsed_capabilities = capabilities
    elif isinstance(capabilities, Mapping):
        parsed_capabilities = capabilities_from_mapping(capabilities)
    else:
        raise TypeError("capabilities must be CraftingCapabilities or canonical mapping")
    capabilities_json = parsed_capabilities.canonical
    capabilities_digest = capabilities_hash(parsed_capabilities)

    with psycopg.connect(database_url(db_url)) as conn:
        with conn.transaction():
            existing = conn.execute("SELECT id FROM paper_runs WHERE name = %s", (name,)).fetchone()
            if existing is not None:
                raise PaperError(f"paper run {name!r} already exists")
            house_row = conn.execute("SELECT id FROM houses WHERE key = %s", (house,)).fetchone()
            if house_row is None:
                raise PaperError(f"unknown house {house!r}")
            house_id = int(_row(house_row, "id", 0))
            rows = _build_rows(conn, house_id)
            if not rows:
                raise PaperError(f"no market sweeps found for house {house!r}")
            available_builds = {_full_build(row) for row in rows}
            selected_build = client_build or _full_build(rows[-1])
            if selected_build not in available_builds:
                raise PaperError(f"no market sweeps found for house {house!r} with build {selected_build!r}")
            result = conn.execute(
                """
                INSERT INTO paper_runs
                    (name, house_id, client_build, start_t, capital_copper, strategies,
                     weights, demand_share, engine_digest, capabilities, capabilities_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    name,
                    house_id,
                    selected_build,
                    start_t,
                    capital,
                    Jsonb(names),
                    Jsonb([str(weight) for weight in parsed_weights]),
                    str(parsed_demand_share),
                    digest,
                    Jsonb(capabilities_json),
                    capabilities_digest,
                ),
            ).fetchone()
            return int(_row(result, "id", 0))


def _run_from_row(row: Any) -> dict[str, Any]:
    raw_capabilities = _paper_run_value(row, "capabilities")
    capabilities = capabilities_from_mapping(raw_capabilities)
    if raw_capabilities != capabilities.canonical:
        raise PaperError("paper run capabilities are not in canonical form")
    stored_capabilities_hash = str(_paper_run_value(row, "capabilities_hash"))
    if stored_capabilities_hash != capabilities_hash(capabilities):
        raise PaperError("paper run capabilities changed or have an invalid hash")
    house_identity = House(
        str(_paper_run_value(row, "region")),
        str(_paper_run_value(row, "ruleset")),
        str(_paper_run_value(row, "faction")),
    )
    return {
        "id": int(_paper_run_value(row, "id")),
        "name": str(_paper_run_value(row, "name")),
        "house_id": int(_paper_run_value(row, "house_id")),
        "house": str(_paper_run_value(row, "house")),
        "house_identity": house_identity,
        "rules": rules_for_house(house_identity),
        "client_build": str(_paper_run_value(row, "client_build")),
        "start_t": int(_paper_run_value(row, "start_t")),
        "capital_copper": int(_paper_run_value(row, "capital_copper")),
        "strategies": list(_paper_run_value(row, "strategies")),
        "weights": list(_paper_run_value(row, "weights")),
        "demand_share": str(_paper_run_value(row, "demand_share")),
        "engine_digest": str(_paper_run_value(row, "engine_digest")),
        "capabilities": capabilities,
        "capabilities_hash": stored_capabilities_hash,
    }


def _load_run(conn: Any, name: str) -> dict[str, Any]:
    row = conn.execute(
        f"""
        SELECT {PAPER_RUN_SELECT}
        FROM paper_runs AS pr
        JOIN houses AS h ON h.id = pr.house_id
        WHERE pr.name = %s
        """,
        (name,),
    ).fetchone()
    if row is None:
        raise PaperError(f"unknown paper run {name!r}")
    return _run_from_row(row)


def _immutable_config(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "house_id": run["house_id"],
        "house": run["house"],
        "client_build": run["client_build"],
        "start_t": run["start_t"],
        "capital_copper": run["capital_copper"],
        "strategies": list(run["strategies"]),
        "weights": list(run["weights"]),
        "demand_share": run["demand_share"],
        "engine_digest": run["engine_digest"],
        "capabilities": run["capabilities"].canonical,
        "capabilities_hash": run["capabilities_hash"],
    }


def _book_step_times(books: Mapping[int, Sequence[Any]]) -> dict[int, int]:
    """Return the represented step time for each loaded sweep."""

    step_times: dict[int, int] = {}
    for item_books in books.values():
        for book in item_books:
            if book.sweep_id is None:
                continue
            step_t = int(book.t)
            sweep_id = int(book.sweep_id)
            prior = step_times.get(sweep_id)
            step_times[sweep_id] = step_t if prior is None else max(prior, step_t)
    return step_times


def _replay(db_url: str, name: str) -> tuple[dict[str, Any], BacktestResult, float]:
    started = time.monotonic()
    with psycopg.connect(database_url(db_url)) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET TRANSACTION READ ONLY")
            run = _load_run(conn, name)
            current_digest = engine_digest()
            if run["engine_digest"] != current_digest:
                raise PaperError("code changed since run creation; create a new run")
            rows = conn.execute(
                """
                SELECT ms.client_version, ms.client_build, ms.id, ms.started_at
                FROM market_sweeps AS ms
                WHERE ms.house_id = %s
                ORDER BY ms.started_at, ms.id
                """,
                (run["house_id"],),
            ).fetchall()
            loader = _ReadOnlyLoaderConnection(conn)
            books = load_commodity_books(loader, run["house"])
            represented_times = _book_step_times(books)
            represented_sweeps = {
                int(_row(row, "id", 2)): row
                for row in rows
                if int(_row(row, "id", 2)) in represented_times
                and represented_times[int(_row(row, "id", 2))] >= run["start_t"]
            }
            for row in represented_sweeps.values():
                if _full_build(row) != run["client_build"]:
                    raise PaperError("client build changed since run creation; launch will need a new run")
            names = _parse_strategies(run["strategies"])
            weights = [_fraction(value) for value in run["weights"]]
            facts, recipes = load_client_data(loader, run["house"], run["client_build"])
            statistics = precompute_statistics(books)
            strategies = [build(name) for name in names]
            result = run_backtest(
                books,
                statistics,
                facts,
                recipes,
                strategies,
                weights,
                run["capital_copper"],
                start_t=run["start_t"],
                wind_down_hours=0,
                demand_share=_fraction(run["demand_share"]),
                capabilities=run["capabilities"],
                rules=run["rules"],
            )
            result.metadata.update(
                {
                    "client_build": run["client_build"],
                    "paper_run": run["name"],
                    "house": run["house"],
                    "start_t": run["start_t"],
                    "capital_copper": run["capital_copper"],
                    "capabilities": run["capabilities"].canonical,
                    "capabilities_hash": run["capabilities_hash"],
                    "strategy_professions": result.metadata.get("strategy_professions", {}),
                    "strategy_weights": result.metadata.get("strategy_weights", {}),
                }
            )
    return run, result, time.monotonic() - started


def _copper(value: Any) -> str:
    amount = _fraction(value)
    return str(amount.numerator) if amount.denominator == 1 else f"{amount.numerator}/{amount.denominator}"


def _step_records(result: BacktestResult) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    names = list(result.accounts)
    for index, step in enumerate(result.step_log):
        equity = {name: _copper(result.equity_curves[name][index][1]) for name in names}
        realized = {name: _copper(result.realized_curves[name][index][1]) for name in names}
        cash = {name: _copper(result.cash_curves[name][index][1]) for name in names}
        equity["total"] = _copper(sum((result.equity_curves[name][index][1] for name in names), Fraction(0)))
        realized["total"] = _copper(
            sum((result.realized_curves[name][index][1] for name in names), Fraction(0))
        )
        cash["total"] = _copper(sum((result.cash_curves[name][index][1] for name in names), 0))
        records.append(
            {
                "t": int(step["t"]),
                "sweep_key": str(step["sweep_key"]),
                "equity": equity,
                "realized": realized,
                "cash": cash,
                "intents": step["strategies"],
            }
        )
    return records


def _stored_steps(conn: Any, run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT step_t, sweep_key, equity, realized, cash
        FROM paper_steps
        WHERE run_id = %s
        ORDER BY step_t
        """,
        (run_id,),
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        result[int(_row(row, "step_t", 0))] = {
            "t": int(_row(row, "step_t", 0)),
            "sweep_key": str(_row(row, "sweep_key", 1)),
            "equity": _row(row, "equity", 2),
            "realized": _row(row, "realized", 3),
            "cash": _row(row, "cash", 4),
        }
    return result


def _stored_intents(conn: Any, run_id: int, step_t: int) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT strategy, ordinal, intent, outcome, detail
        FROM paper_intents
        WHERE run_id = %s AND step_t = %s
        ORDER BY strategy, ordinal
        """,
        (run_id, step_t),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strategy = str(_row(row, "strategy", 0))
        result.setdefault(strategy, []).append(
            {
                "intent": _row(row, "intent", 2),
                "outcome": _row(row, "outcome", 3),
                "detail": _row(row, "detail", 4),
            }
        )
    return result


def _validate_existing(conn: Any, run_id: int, records: Sequence[dict[str, Any]]) -> set[int]:
    expected = {record["t"]: record for record in records}
    stored = _stored_steps(conn, run_id)
    for step_t, stored_step in stored.items():
        record = expected.get(step_t)
        if record is None:
            raise PaperError(f"paper run diverged at step {step_t}")
        for key in ("sweep_key", "equity", "realized", "cash"):
            if stored_step[key] != record[key]:
                raise PaperError(f"paper run diverged at step {step_t}")
        expected_intents = {strategy: intents for strategy, intents in record["intents"].items() if intents}
        if _stored_intents(conn, run_id, step_t) != expected_intents:
            raise PaperError(f"paper run diverged at step {step_t}")
    return set(stored)


def _write_records(conn: Any, run_id: int, records: Sequence[dict[str, Any]], existing: set[int]) -> int:
    added = 0
    for record in records:
        if record["t"] in existing:
            continue
        conn.execute(
            """
            INSERT INTO paper_steps (run_id, step_t, sweep_key, equity, realized, cash)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                record["t"],
                record["sweep_key"],
                Jsonb(record["equity"]),
                Jsonb(record["realized"]),
                Jsonb(record["cash"]),
            ),
        )
        for strategy, intents in record["intents"].items():
            for ordinal, entry in enumerate(intents):
                conn.execute(
                    """
                    INSERT INTO paper_intents
                        (run_id, step_t, strategy, ordinal, intent, outcome, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        record["t"],
                        strategy,
                        ordinal,
                        Jsonb(entry["intent"]),
                        entry["outcome"],
                        Jsonb(entry["detail"]),
                    ),
                )
        added += 1
    return added


def _gold(value: Any) -> str:
    return f"{float(_fraction(value)) / 10_000:.2f}g"


def _pending_intents(record: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        strategy: [entry["intent"] for entry in intents] for strategy, intents in record["intents"].items()
    }


def step_run(db_url: str, name: str) -> str:
    run, result, replay_seconds = _replay(db_url, name)
    records = _step_records(result)
    final_records = records[:-1] if records else []
    pending_record = records[-1] if records else None
    with psycopg.connect(database_url(db_url)) as conn:
        with conn.transaction():
            live = conn.execute(
                f"""
                SELECT {PAPER_RUN_SELECT}
                FROM paper_runs AS pr
                JOIN houses AS h ON h.id = pr.house_id
                WHERE pr.name = %s
                FOR UPDATE OF pr
                """,
                (name,),
            ).fetchone()
            if live is None:
                raise PaperError(f"unknown paper run {name!r}")
            live_run = _run_from_row(live)
            if live_run["id"] != run["id"] or live_run["name"] != run["name"]:
                raise PaperError("paper run identity changed; refusing to write")
            if _immutable_config(live_run) != _immutable_config(run):
                raise PaperError("paper run immutable configuration changed; refusing to write")
            if live_run["engine_digest"] != engine_digest():
                raise PaperError("code changed since run creation; create a new run")
            run_id = live_run["id"]
            stored = _validate_existing(conn, run_id, final_records)
            added = _write_records(conn, run_id, final_records, stored)
            conn.execute("DELETE FROM paper_pending WHERE run_id = %s", (run_id,))
            if pending_record is not None:
                conn.execute(
                    """
                    INSERT INTO paper_pending (run_id, step_t, sweep_key, intents)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        pending_record["t"],
                        pending_record["sweep_key"],
                        Jsonb(_pending_intents(pending_record)),
                    ),
                )
            if records:
                latest_t = records[-1]["t"]
                total = records[-1]["equity"]["total"]
            else:
                latest = conn.execute(
                    "SELECT step_t, equity FROM paper_steps WHERE run_id = %s ORDER BY step_t DESC LIMIT 1",
                    (int(_row(live, "id", 0)),),
                ).fetchone()
                latest_t = "none" if latest is None else int(_row(latest, "step_t", 0))
                total = (
                    _copper(run["capital_copper"]) if latest is None else _row(latest, "equity", 1)["total"]
                )
    output = (
        f"steps_added={added} latest_t={latest_t} total_equity={_gold(total)} "
        f"replay_seconds={replay_seconds:.2f}"
    )
    if replay_seconds > 600:
        output += "\nWARNING: paper replay exceeded 600 seconds"
    return output


def status_run(db_url: str, name: str, last_intents: int = 20) -> str:
    if last_intents < 0:
        raise ValueError("last-intents must be non-negative")
    with psycopg.connect(database_url(db_url)) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            run = _load_run(conn, name)
            step = conn.execute(
                """
                SELECT step_t, equity, realized
                FROM paper_steps
                WHERE run_id = %s
                ORDER BY step_t DESC
                LIMIT 1
                """,
                (run["id"],),
            ).fetchone()
            lines = [
                f"paper_run={run['name']}",
                f"client_build={run['client_build']}",
                f"capabilities_hash={run['capabilities_hash']}",
            ]
            if step is None:
                lines.append("latest_t=none")
                lines.append(f"total_equity={_gold(run['capital_copper'])}")
            else:
                step_t = int(_row(step, "step_t", 0))
                equity = _row(step, "equity", 1)
                realized = _row(step, "realized", 2)
                lines.append(f"latest_t={step_t}")
                for strategy in run["strategies"]:
                    lines.append(
                        f"{strategy} equity={_gold(equity[strategy])} realized={_gold(realized[strategy])}"
                    )
                lines.append(f"total_equity={_gold(equity['total'])}")
                intents = conn.execute(
                    """
                    SELECT strategy, ordinal, intent, outcome, detail
                    FROM paper_intents
                    WHERE run_id = %s AND step_t = %s
                    ORDER BY strategy, ordinal
                    """,
                    (run["id"], step_t),
                ).fetchall()
                for row in intents[-last_intents:] if last_intents else ():
                    intent = json.dumps(_row(row, "intent", 2), sort_keys=True, separators=(",", ":"))
                    detail = json.dumps(_row(row, "detail", 4), sort_keys=True, separators=(",", ":"))
                    lines.append(
                        f"intent strategy={_row(row, 'strategy', 0)} ordinal={_row(row, 'ordinal', 1)} "
                        f"intent={intent} outcome={_row(row, 'outcome', 3)} detail={detail}"
                    )
            pending = conn.execute(
                """
                SELECT step_t, sweep_key, intents
                FROM paper_pending
                WHERE run_id = %s
                """,
                (run["id"],),
            ).fetchone()
            if pending is not None:
                lines.append("pending (outcome not final):")
                lines.append(
                    f"pending_t={_row(pending, 'step_t', 0)} "
                    f"pending_sweep_key={_row(pending, 'sweep_key', 1)}"
                )
                pending_values = _row(pending, "intents", 2)
                for strategy, intent_list in pending_values.items():
                    for ordinal, intent_value in enumerate(intent_list):
                        intent = json.dumps(intent_value, sort_keys=True, separators=(",", ":"))
                        lines.append(f"pending intent strategy={strategy} ordinal={ordinal} intent={intent}")
            return "\n".join(lines)


__all__ = [
    "DEFAULT_STRATEGIES",
    "PaperError",
    "create_run",
    "database_url",
    "engine_digest",
    "status_run",
    "step_run",
]
