"""Persistent deterministic paper-trading runs over recorded market sweeps."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple, cast

import psycopg
from psycopg.types.json import Jsonb

from crowley.analysis.books import MAX_GAP, load_commodity_books
from crowley.core.auction_rules import rules_for_house
from crowley.core.house import House

from .accounts import Account, LedgerEvent, Listing, Lot, Mail
from .api import CraftingCapabilities, ItemSnapshot, Level
from .data import load_client_data
from .engine import BacktestResult, EngineState, run_backtest
from .numbers import as_fraction
from .requirements import EMPTY_CAPABILITIES, capabilities_from_mapping, capabilities_hash
from .statistics import StatisticsAccumulator
from .steps import sweep_step_times
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
    ("engine_contract", "pr.engine_contract"),
)
PAPER_RUN_SELECT = ", ".join(f"{expression} AS {name}" for name, expression in PAPER_RUN_COLUMNS)


class PaperError(RuntimeError):
    """A paper run cannot be created or advanced without operator action."""


ENGINE_DIGEST_VERSION = 2
ENGINE_STATE_VERSION = 1
MAX_STATE_TIMESTAMP = 10**12
REPLAY_OVERLAP_ORDINALS = math.ceil((86400 + MAX_GAP) / 900) + 2
REPLAY_MODULES: tuple[str, ...] = (
    "crowley/__init__.py",
    "crowley/analysis/__init__.py",
    "crowley/analysis/books.py",
    "crowley/analysis/common.py",
    "crowley/analysis/time_to_sale.py",
    "crowley/core/__init__.py",
    "crowley/core/auction_rules.py",
    "crowley/core/errors.py",
    "crowley/core/game_target.py",
    "crowley/core/house.py",
    "crowley/core/watchlist.py",
    "crowley/data/__init__.py",
    "crowley/trading/accounts.py",
    "crowley/trading/api.py",
    "crowley/trading/canonical.py",
    "crowley/trading/data.py",
    "crowley/trading/engine.py",
    "crowley/trading/execution.py",
    "crowley/trading/fees.py",
    "crowley/trading/market.py",
    "crowley/trading/numbers.py",
    "crowley/trading/recipes.py",
    "crowley/trading/requirements.py",
    "crowley/trading/report.py",
    "crowley/trading/statistics.py",
    "crowley/trading/paper.py",
    "crowley/trading/steps.py",
    "crowley/trading/__init__.py",
    "crowley/trading/strategies/__init__.py",
    "crowley/trading/strategies/common.py",
    "crowley/trading/strategies/crafting.py",
    "crowley/trading/strategies/dip_reversion.py",
    "crowley/trading/strategies/pricing.py",
    "crowley/trading/strategies/snipe_flip.py",
    "crowley/trading/strategies/vendor_arb.py",
)
REPLAY_RESOURCES: tuple[str, ...] = ("crowley/data/first_run.json",)


def _row_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperError(f"paper database field {field!r} is not an integer")
    return value


def _row_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PaperError(f"paper database field {field!r} is not text")
    return value


def _json_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise PaperError(f"paper database field {field!r} is not a JSON list")
    return cast(list[object], value)


def _json_string_list(value: object, field: str) -> list[str]:
    values = _json_list(value, field)
    if any(not isinstance(item, str) for item in values):
        raise PaperError(f"paper database field {field!r} contains a non-text value")
    return cast(list[str], values)


def _json_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PaperError(f"paper database field {field!r} is not a JSON object")
    return cast(Mapping[str, object], value)


class _IdRow(NamedTuple):
    id: int


class _BuildRow(NamedTuple):
    client_version: str
    client_build: int | str
    id: int
    step_t: int


class _SweepRow(NamedTuple):
    client_version: str
    client_build: int | str
    id: int
    started_at: object


class _PaperRunRow(NamedTuple):
    id: int
    name: str
    house_id: int
    house: str
    client_build: str
    start_t: int
    capital_copper: int
    strategies: object
    weights: object
    demand_share: object
    engine_digest: str
    capabilities: object
    capabilities_hash: str
    region: str
    ruleset: str
    faction: str
    engine_contract: str


class _StoredStepRow(NamedTuple):
    replay_ordinal: int
    step_t: int
    sweep_key: str
    equity: object
    realized: object
    cash: object
    previous_digest: str
    step_digest: str


class _StoredIntentRow(NamedTuple):
    strategy: str
    ordinal: int
    intent: object
    outcome: str
    detail: object


class _LatestStepRow(NamedTuple):
    step_t: int
    equity: object


class _StatusStepRow(NamedTuple):
    step_t: int
    equity: object
    realized: object


class _PendingRow(NamedTuple):
    step_t: int
    sweep_key: str
    intents: object


class _EngineStateRow(NamedTuple):
    replay_ordinal: int
    step_t: int
    state_version: int
    state: object
    state_digest: str


class _ReadOnlyLoaderConnection:
    """Reuse the caller's repeatable read-only transaction for analysis loaders."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def transaction(self):
        return nullcontext(self._connection)


def database_url(value: str | None = None) -> str:
    resolved = value or os.environ.get("CROWLEY_DATABASE_URL")
    if not resolved:
        raise PaperError("database URL is required (--database-url or CROWLEY_DATABASE_URL)")
    return resolved


def _full_build(row: _BuildRow | _SweepRow) -> str:
    return f"{row.client_version}.{row.client_build}"


def _build_rows(conn: Any, house_id: int) -> list[_BuildRow]:
    rows = conn.execute(
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
    return [_BuildRow(*row) for row in rows]


def _module_path(module_name: str) -> Path:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise PaperError(f"replay dependency {module_name!r} cannot be imported") from exc
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        raise PaperError(f"replay dependency {module_name!r} has no Python source")
    return Path(spec.origin)


def _relative_import_base(module_name: str, level: int, *, is_package: bool) -> str:
    parts = module_name.split(".")
    package_parts = parts if is_package else parts[:-1]
    if level > len(package_parts):
        raise PaperError(f"replay dependency {module_name!r} has an invalid relative import")
    return ".".join(package_parts[: len(package_parts) - level + 1])


def _is_replay_module(module_name: str) -> bool:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError):
        return False
    return spec is not None and spec.origin is not None and spec.origin.endswith(".py")


def _module_imports(module_name: str, source: bytes, *, is_package: bool) -> frozenset[str]:
    try:
        tree = ast.parse(source.decode("utf-8"), filename=module_name)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise PaperError(f"replay dependency {module_name!r} is not valid UTF-8 Python") from exc
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name
                for alias in node.names
                if alias.name == "crowley" or alias.name.startswith("crowley.")
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = _relative_import_base(module_name, node.level, is_package=is_package)
            target = f"{base}.{node.module}" if node.module else base
        else:
            target = node.module or ""
        if target != "crowley" and not target.startswith("crowley."):
            continue
        if _is_replay_module(target):
            imported.add(target)
        imported.update(
            candidate
            for candidate in (f"{target}.{alias.name}" for alias in node.names)
            if _is_replay_module(candidate)
        )
    return frozenset(imported)


def _package_initializers(module_name: str) -> frozenset[str]:
    parts = module_name.split(".")
    return frozenset(".".join(parts[:index]) for index in range(1, len(parts)))


def _declared_module_names() -> frozenset[str]:
    names: set[str] = set()
    for relative in REPLAY_MODULES:
        if not relative.endswith(".py"):
            raise PaperError(f"declared replay module has an invalid path: {relative}")
        without_suffix = relative[:-3]
        if without_suffix.endswith("/__init__"):
            without_suffix = without_suffix[: -len("/__init__")]
        names.add(without_suffix.replace("/", "."))
    return frozenset(names)


def _compute_replay_closure() -> frozenset[str]:
    roots = {"crowley.trading.paper", "crowley.data"}
    pending = list(roots)
    discovered = set(roots)
    while pending:
        module_name = pending.pop()
        path = _module_path(module_name)
        source = path.read_bytes()
        imports = _package_initializers(module_name) | _module_imports(
            module_name, source, is_package=path.name == "__init__.py"
        )
        for imported in imports:
            if imported not in discovered:
                discovered.add(imported)
                pending.append(imported)
    return frozenset(discovered)


def _source_digest() -> str:
    """Hash the exact computed replay import/resource closure."""

    source_root = Path(__file__).resolve().parents[2]
    computed = _compute_replay_closure()
    declared = _declared_module_names()
    if computed != declared:
        raise PaperError(
            f"declared replay closure drifted; missing={sorted(computed - declared)!r}, "
            f"unexpected={sorted(declared - computed)!r}"
        )
    digest = hashlib.sha256()
    digest.update(f"engine-digest-version:{ENGINE_DIGEST_VERSION}\n".encode("ascii"))
    for module_name in sorted(computed):
        relative = str(_module_path(module_name).relative_to(source_root)).replace(os.sep, "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_module_path(module_name).read_bytes())
        digest.update(b"\0")
    for relative in sorted(REPLAY_RESOURCES):
        path = source_root / relative
        if not path.is_file():
            raise PaperError(f"declared replay resource is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


engine_digest = _source_digest


def _parse_strategies(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        names = [str(name) for name in DEFAULT_STRATEGIES]
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
        weights = [as_fraction(part) for part in parts]
    else:
        weights = [as_fraction(part) for part in value]
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
    parsed_demand_share = as_fraction(demand_share)
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
            house_id = _IdRow(*house_row).id
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
                     weights, demand_share, engine_digest, capabilities, capabilities_hash,
                     engine_contract)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'replay-v1')
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
            if result is None:
                raise PaperError("paper run insert did not return an id")
            return _row_int(result[0], "paper run id")


def _run_from_row(row: tuple[object, ...]) -> dict[str, Any]:
    if len(row) != len(PAPER_RUN_COLUMNS):
        raise PaperError("paper run query returned an unexpected number of columns")
    parsed = _PaperRunRow(
        _row_int(row[0], "id"),
        _row_str(row[1], "name"),
        _row_int(row[2], "house_id"),
        _row_str(row[3], "house"),
        _row_str(row[4], "client_build"),
        _row_int(row[5], "start_t"),
        _row_int(row[6], "capital_copper"),
        row[7],
        row[8],
        row[9],
        _row_str(row[10], "engine_digest"),
        row[11],
        _row_str(row[12], "capabilities_hash"),
        _row_str(row[13], "region"),
        _row_str(row[14], "ruleset"),
        _row_str(row[15], "faction"),
        _row_str(row[16], "engine_contract"),
    )
    raw_capabilities = _json_mapping(parsed.capabilities, "capabilities")
    capabilities = capabilities_from_mapping(cast(Mapping[str, Any], raw_capabilities))
    if raw_capabilities != capabilities.canonical:
        raise PaperError("paper run capabilities are not in canonical form")
    stored_capabilities_hash = str(parsed.capabilities_hash)
    if stored_capabilities_hash != capabilities_hash(capabilities):
        raise PaperError("paper run capabilities changed or have an invalid hash")
    house_identity = House(
        str(parsed.region),
        str(parsed.ruleset),
        str(parsed.faction),
    )
    return {
        "id": int(parsed.id),
        "name": str(parsed.name),
        "house_id": int(parsed.house_id),
        "house": str(parsed.house),
        "house_identity": house_identity,
        "rules": rules_for_house(house_identity),
        "client_build": str(parsed.client_build),
        "start_t": int(parsed.start_t),
        "capital_copper": int(parsed.capital_copper),
        "strategies": _json_string_list(parsed.strategies, "strategies"),
        "weights": _json_list(parsed.weights, "weights"),
        "demand_share": str(parsed.demand_share),
        "engine_digest": str(parsed.engine_digest),
        "capabilities": capabilities,
        "capabilities_hash": stored_capabilities_hash,
        "engine_contract": parsed.engine_contract,
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
    return _run_from_row(cast(tuple[object, ...], row))


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
        "engine_contract": run["engine_contract"],
    }


def _encode_value(value: object) -> object:
    if isinstance(value, Fraction):
        return {"type": "fraction", "numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PaperError("engine state float must be finite")
        return {"type": "float", "value": repr(value)}
    if isinstance(value, Mapping):
        pairs = [[_encode_value(key), _encode_value(item)] for key, item in value.items()]
        pairs.sort(key=lambda pair: json.dumps(pair[0], sort_keys=True, separators=(",", ":")))
        return {"type": "mapping", "items": pairs}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return {"type": "list", "items": [_encode_value(item) for item in value]}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise PaperError(f"unsupported engine state value type: {type(value).__name__}")


def _decode_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if not isinstance(value, Mapping):
        raise PaperError("engine state value is not a JSON value")
    value_type = value.get("type")
    if value_type == "fraction":
        if set(value) != {"type", "numerator", "denominator"}:
            raise PaperError("engine state fraction has an unexpected schema")
        numerator = value.get("numerator")
        denominator = value.get("denominator")
        if (
            isinstance(numerator, bool)
            or not isinstance(numerator, int)
            or isinstance(denominator, bool)
            or not isinstance(denominator, int)
            or denominator <= 0
        ):
            raise PaperError("engine state fraction is invalid")
        return Fraction(numerator, denominator)
    if value_type == "float":
        if set(value) != {"type", "value"}:
            raise PaperError("engine state float has an unexpected schema")
        raw = value.get("value")
        if not isinstance(raw, str):
            raise PaperError("engine state float is invalid")
        try:
            parsed = float(raw)
        except ValueError as exc:
            raise PaperError("engine state float is invalid") from exc
        if not math.isfinite(parsed) or repr(parsed) != raw:
            raise PaperError("engine state float is not canonical or finite")
        return parsed
    if value_type in {"mapping", "tuple", "list"}:
        if set(value) != {"type", "items"}:
            raise PaperError("engine state collection has an unexpected schema")
        items = value.get("items")
        if not isinstance(items, list):
            raise PaperError("engine state collection is invalid")
        if value_type == "mapping":
            decoded_pairs: list[tuple[object, object]] = []
            for pair in items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise PaperError("engine state mapping entry is invalid")
                decoded_pairs.append((_decode_value(pair[0]), _decode_value(pair[1])))
            encoded_keys = {
                json.dumps(_encode_value(key), sort_keys=True, separators=(",", ":"))
                for key, _ in decoded_pairs
            }
            if len(encoded_keys) != len(decoded_pairs):
                raise PaperError("engine state mapping contains duplicate keys")
            try:
                return dict(decoded_pairs)
            except TypeError as exc:
                raise PaperError("engine state mapping key is not hashable") from exc
        decoded_items = [_decode_value(item) for item in items]
        return tuple(decoded_items) if value_type == "tuple" else decoded_items
    raise PaperError(f"unsupported engine state value tag: {value_type!r}")


def _state_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PaperError(f"engine state field {field!r} is not an object")
    return cast(Mapping[str, object], value)


def _state_keys(data: Mapping[str, object], expected: set[str], field: str) -> None:
    if set(data) != expected:
        missing = sorted(expected - set(data))
        extra = sorted(set(data) - expected)
        raise PaperError(f"engine state field {field!r} has invalid keys (missing={missing}, extra={extra})")


def _state_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperError(f"engine state field {field!r} is not an integer")
    return value


def _state_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PaperError(f"engine state field {field!r} is not text")
    return value


def _state_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperError(f"engine state field {field!r} is not numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise PaperError(f"engine state field {field!r} is not finite")
    return parsed


def _state_timestamp(value: object, field: str) -> float:
    parsed = _state_float(value, field)
    if parsed < 0 or parsed > MAX_STATE_TIMESTAMP:
        raise PaperError(f"engine state field {field!r} is outside the timestamp range")
    return parsed


def _state_fraction(value: object, field: str) -> Fraction:
    decoded = _decode_value(value)
    if isinstance(decoded, Fraction):
        return decoded
    raise PaperError(f"engine state field {field!r} is not a canonical fraction")


def _state_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise PaperError(f"engine state field {field!r} is not a list")
    return value


def _encoded_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _encode_value(item) for key, item in sorted(value.items())}


def _encode_snapshot(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, ItemSnapshot):
        raise PaperError("engine state snapshot has an invalid type")
    return {
        "item_id": snapshot.item_id,
        "t": snapshot.t,
        "levels": [[level.unit_price, level.quantity] for level in snapshot.levels],
        "visible_min": snapshot.visible_min,
        "visible_units": snapshot.visible_units,
        "fair": _encode_value(snapshot.fair),
        "fair_band": _encode_value(snapshot.fair_band),
        "fair_n": snapshot.fair_n,
        "trend_per_day": _encode_value(snapshot.trend_per_day),
        "demand_units_per_day": _encode_value(snapshot.demand_units_per_day),
        "demand_eligible_seconds": snapshot.demand_eligible_seconds,
        "volatility_per_hour": _encode_value(snapshot.volatility_per_hour),
        "half_life_hours": _encode_value(snapshot.half_life_hours),
    }


def _decode_snapshot(value: object) -> ItemSnapshot:
    data = _state_mapping(value, "snapshot")
    _state_keys(
        data,
        {
            "item_id",
            "t",
            "levels",
            "visible_min",
            "visible_units",
            "fair",
            "fair_band",
            "fair_n",
            "trend_per_day",
            "demand_units_per_day",
            "demand_eligible_seconds",
            "volatility_per_hour",
            "half_life_hours",
        },
        "snapshot",
    )
    levels: list[Level] = []
    for raw_level in _state_list(data.get("levels"), "snapshot.levels"):
        level = _state_list(raw_level, "snapshot.level")
        if len(level) != 2:
            raise PaperError("engine state snapshot level must have two values")
        levels.append(
            Level(
                _state_int(level[0], "snapshot.level.unit_price"),
                _state_int(level[1], "snapshot.level.quantity"),
            )
        )
    if any(level.unit_price <= 0 or level.quantity <= 0 for level in levels):
        raise PaperError("engine state snapshot levels have an invalid range")
    if levels != sorted(levels, key=lambda level: (level.unit_price, level.quantity)):
        raise PaperError("engine state snapshot levels are not sorted")
    fair = _decode_value(data.get("fair"))
    fair_band = _decode_value(data.get("fair_band"))
    if fair is not None and not isinstance(fair, Fraction):
        raise PaperError("engine state snapshot fair value is invalid")
    if fair_band is not None and (
        not isinstance(fair_band, tuple)
        or len(fair_band) != 2
        or not all(isinstance(value, Fraction) for value in fair_band)
    ):
        raise PaperError("engine state snapshot fair band is invalid")
    demand = _decode_value(data.get("demand_units_per_day"))
    if demand is not None and not isinstance(demand, Fraction):
        raise PaperError("engine state snapshot demand is invalid")
    trend = _decode_value(data.get("trend_per_day"))
    volatility = _decode_value(data.get("volatility_per_hour"))
    half_life = _decode_value(data.get("half_life_hours"))
    for field_name, field_value in (
        ("trend_per_day", trend),
        ("volatility_per_hour", volatility),
        ("half_life_hours", half_life),
    ):
        if field_value is not None and not isinstance(field_value, float):
            raise PaperError(f"engine state snapshot {field_name} is invalid")
    item_id = _state_int(data["item_id"], "snapshot.item_id")
    timestamp = _state_int(data["t"], "snapshot.t")
    visible_units = _state_int(data["visible_units"], "snapshot.visible_units")
    fair_n = _state_int(data["fair_n"], "snapshot.fair_n")
    if item_id <= 0 or timestamp < 0 or timestamp > MAX_STATE_TIMESTAMP or visible_units < 0 or fair_n < 0:
        raise PaperError("engine state snapshot has an invalid range")
    if visible_units != sum(level.quantity for level in levels):
        raise PaperError("engine state snapshot visible_units does not match levels")
    visible_min = (
        None if data["visible_min"] is None else _state_int(data["visible_min"], "snapshot.visible_min")
    )
    if visible_min != (levels[0].unit_price if levels else None):
        raise PaperError("engine state snapshot visible_min does not match levels")
    if visible_min is not None and visible_min <= 0:
        raise PaperError("engine state snapshot visible_min is out of range")
    if fair_band is not None and fair_band[0] > fair_band[1]:
        raise PaperError("engine state snapshot fair_band is not ordered")
    demand_eligible_seconds = _state_float(
        data["demand_eligible_seconds"], "snapshot.demand_eligible_seconds"
    )
    if demand_eligible_seconds < 0:
        raise PaperError("engine state snapshot demand_eligible_seconds is negative")
    return ItemSnapshot(
        item_id=item_id,
        t=timestamp,
        levels=tuple(levels),
        visible_min=visible_min,
        visible_units=visible_units,
        fair=cast(Fraction | None, fair),
        fair_band=cast(tuple[Fraction, Fraction] | None, fair_band),
        fair_n=fair_n,
        trend_per_day=cast(float | None, trend),
        demand_units_per_day=cast(Fraction | None, demand),
        demand_eligible_seconds=demand_eligible_seconds,
        volatility_per_hour=cast(float | None, volatility),
        half_life_hours=cast(float | None, half_life),
    )


def _encode_account(account: Account) -> dict[str, object]:
    return {
        "name": account.name,
        "professions": sorted(account.professions),
        "start_capital": account.start_capital,
        "cash_balance": account.cash_balance,
        "inventory": [list(item) for item in sorted(account.inventory_balance.items())],
        "listings": [
            {
                "listing_id": listing.listing_id,
                "item_id": listing.item_id,
                "unit_price": listing.unit_price,
                "quantity": listing.quantity,
                "original_quantity": listing.original_quantity,
                "posted_t": float(listing.posted_t),
                "posted_step": listing.posted_step,
                "expires_t": float(listing.expires_t),
                "deposit_paid": listing.deposit_paid,
                "deposit_refunded": listing.deposit_refunded,
            }
            for listing in sorted(account.listings_by_id.values(), key=lambda value: value.listing_id)
        ],
        "mail": [
            {
                "arrival_t": float(parcel.arrival_t),
                "cash": parcel.cash,
                "items": [list(item) for item in sorted(parcel.items.items())],
            }
            for parcel in account.mail
        ],
        "ledger": [
            {
                "t": event.t,
                "kind": event.kind,
                "item_id": event.item_id,
                "quantity": event.quantity,
                "copper": event.copper,
                "unit_price": event.unit_price,
                "listing_id": event.listing_id,
                "reason": event.reason,
                "details": _encode_value(event.details),
            }
            for event in account.ledger
        ],
        "lots": [
            {
                "item_id": lot.item_id,
                "quantity": lot.quantity,
                "unit_cost": _encode_value(lot.unit_cost),
                "acquired_t": float(lot.acquired_t),
                "source": lot.source,
            }
            for lots in account.lots.values()
            for lot in lots
        ],
        "counts": _encoded_mapping(account.counts),
        "fees": _encoded_mapping(account.fees),
        "rejections": _encoded_mapping(account.rejections),
        "closed_trades": _encode_value(account.closed_trades),
        "clicks": account.clicks,
        "cooldowns": _encoded_mapping(account.cooldowns),
        "capabilities": account.crafting_capabilities.canonical,
    }


def _decode_account(value: object, cooldowns: dict[str, float] | None) -> Account:
    data = _state_mapping(value, "account")
    raw_capabilities = _state_mapping(data.get("capabilities"), "account.capabilities")
    capabilities = capabilities_from_mapping(cast(Mapping[str, Any], raw_capabilities))
    raw_cooldowns = _state_mapping(data.get("cooldowns"), "account.cooldowns")
    decoded_cooldowns = {
        key: _state_float(_decode_value(item), f"account.cooldowns.{key}")
        for key, item in raw_cooldowns.items()
    }
    if any(value < 0 for value in decoded_cooldowns.values()):
        raise PaperError("engine state cooldown timestamp cannot be negative")
    if cooldowns is None:
        cooldowns = decoded_cooldowns
    elif cooldowns != decoded_cooldowns:
        raise PaperError("engine state accounts do not share cooldown state")
    account = Account(
        name=_state_str(data.get("name"), "account.name"),
        professions=frozenset(
            _state_str(item, "account.profession")
            for item in _state_list(data.get("professions"), "account.professions")
        ),
        start_capital=_state_int(data.get("start_capital"), "account.start_capital"),
        cash_balance=_state_int(data.get("cash_balance"), "account.cash_balance"),
        cooldowns=cooldowns,
        crafting_capabilities=capabilities,
    )
    for raw_item in _state_list(data.get("inventory"), "account.inventory"):
        item = _state_list(raw_item, "account.inventory entry")
        if len(item) != 2:
            raise PaperError("engine state inventory entry must have two values")
        account.inventory_balance[_state_int(item[0], "account.inventory.item_id")] = _state_int(
            item[1], "account.inventory.quantity"
        )
    for raw_listing in _state_list(data.get("listings"), "account.listings"):
        listing = _state_mapping(raw_listing, "account.listing")
        parsed = Listing(
            listing_id=_state_int(listing.get("listing_id"), "listing.listing_id"),
            item_id=_state_int(listing.get("item_id"), "listing.item_id"),
            unit_price=_state_int(listing.get("unit_price"), "listing.unit_price"),
            quantity=_state_int(listing.get("quantity"), "listing.quantity"),
            original_quantity=_state_int(listing.get("original_quantity"), "listing.original_quantity"),
            posted_t=_state_float(listing.get("posted_t"), "listing.posted_t"),
            posted_step=_state_int(listing.get("posted_step"), "listing.posted_step"),
            expires_t=_state_float(listing.get("expires_t"), "listing.expires_t"),
            deposit_paid=_state_int(listing.get("deposit_paid"), "listing.deposit_paid"),
            deposit_refunded=_state_int(listing.get("deposit_refunded"), "listing.deposit_refunded"),
        )
        account.listings_by_id[parsed.listing_id] = parsed
    for raw_parcel in _state_list(data.get("mail"), "account.mail"):
        parcel = _state_mapping(raw_parcel, "account.mail entry")
        items: dict[int, int] = {}
        for raw_item in _state_list(parcel.get("items"), "mail.items"):
            item = _state_list(raw_item, "mail.item")
            if len(item) != 2:
                raise PaperError("engine state mail item must have two values")
            items[_state_int(item[0], "mail.item_id")] = _state_int(item[1], "mail.quantity")
        account.mail.append(
            Mail(
                _state_float(parcel.get("arrival_t"), "mail.arrival_t"),
                _state_int(parcel.get("cash"), "mail.cash"),
                items,
            )
        )
    for raw_event in _state_list(data.get("ledger"), "account.ledger"):
        event = _state_mapping(raw_event, "account.ledger entry")
        details = _decode_value(event.get("details"))
        if not isinstance(details, Mapping):
            raise PaperError("engine state ledger details are invalid")
        account.ledger.append(
            LedgerEvent(
                t=_state_float(event.get("t"), "ledger.t"),
                kind=_state_str(event.get("kind"), "ledger.kind"),
                item_id=None
                if event.get("item_id") is None
                else _state_int(event.get("item_id"), "ledger.item_id"),
                quantity=_state_int(event.get("quantity"), "ledger.quantity"),
                copper=_state_int(event.get("copper"), "ledger.copper"),
                unit_price=None
                if event.get("unit_price") is None
                else _state_int(event.get("unit_price"), "ledger.unit_price"),
                listing_id=None
                if event.get("listing_id") is None
                else _state_int(event.get("listing_id"), "ledger.listing_id"),
                reason=None
                if event.get("reason") is None
                else _state_str(event.get("reason"), "ledger.reason"),
                details=cast(Mapping[str, object], details),
            )
        )
    for raw_lot in _state_list(data.get("lots"), "account.lots"):
        lot = _state_mapping(raw_lot, "account.lot")
        item_id = _state_int(lot.get("item_id"), "lot.item_id")
        unit_cost = _decode_value(lot.get("unit_cost"))
        if not isinstance(unit_cost, (int, Fraction)) or isinstance(unit_cost, bool):
            raise PaperError("engine state lot unit cost is invalid")
        account.lots.setdefault(item_id, []).append(
            Lot(
                item_id,
                _state_int(lot.get("quantity"), "lot.quantity"),
                unit_cost,
                _state_float(lot.get("acquired_t"), "lot.acquired_t"),
                _state_str(lot.get("source"), "lot.source"),
            )
        )
    for field_name, target in (
        ("counts", account.counts),
        ("fees", account.fees),
        ("rejections", account.rejections),
    ):
        raw_values = _state_mapping(data.get(field_name), f"account.{field_name}")
        for key, item in raw_values.items():
            decoded = _decode_value(item)
            target[key] = _state_int(decoded, f"account.{field_name}.{key}")
    closed_trades = _decode_value(data.get("closed_trades"))
    if not isinstance(closed_trades, list):
        raise PaperError("engine state closed trades are invalid")
    account.closed_trades = [cast(dict[str, Any], trade) for trade in closed_trades]
    account.clicks = _state_int(data.get("clicks"), "account.clicks")
    return account


def _encode_checkpoint_account(account: Account) -> dict[str, object]:
    """Encode bounded live account state; ledgers and trades are append-only outputs."""

    return {
        "name": account.name,
        "professions": sorted(account.professions),
        "start_capital": account.start_capital,
        "cash_balance": account.cash_balance,
        "inventory": [list(item) for item in sorted(account.inventory_balance.items())],
        "listings": [
            {
                "listing_id": listing.listing_id,
                "item_id": listing.item_id,
                "unit_price": listing.unit_price,
                "quantity": listing.quantity,
                "original_quantity": listing.original_quantity,
                "posted_t": float(listing.posted_t),
                "posted_step": listing.posted_step,
                "expires_t": float(listing.expires_t),
                "deposit_paid": listing.deposit_paid,
                "deposit_refunded": listing.deposit_refunded,
            }
            for listing in sorted(account.listings_by_id.values(), key=lambda value: value.listing_id)
        ],
        "mail": [
            {
                "arrival_t": float(parcel.arrival_t),
                "cash": parcel.cash,
                "items": [list(item) for item in sorted(parcel.items.items())],
            }
            for parcel in account.mail
        ],
        "lots": [
            {
                "item_id": lot.item_id,
                "quantity": lot.quantity,
                "unit_cost": _encode_value(lot.unit_cost),
                "acquired_t": float(lot.acquired_t),
                "source": lot.source,
            }
            for _item_id, lots in sorted(account.lots.items())
            for lot in lots
        ],
        "counts": _encoded_mapping(account.counts),
        "fees": _encoded_mapping(account.fees),
        "rejections": _encoded_mapping(account.rejections),
        "clicks": account.clicks,
        "cooldowns": _encoded_mapping(account.cooldowns),
        "capabilities": account.crafting_capabilities.canonical,
    }


def _decode_checkpoint_account(value: object, cooldowns: dict[str, float] | None) -> Account:
    data = _state_mapping(value, "account")
    _state_keys(
        data,
        {
            "name",
            "professions",
            "start_capital",
            "cash_balance",
            "inventory",
            "listings",
            "mail",
            "lots",
            "counts",
            "fees",
            "rejections",
            "clicks",
            "cooldowns",
            "capabilities",
        },
        "account",
    )
    raw_capabilities = _state_mapping(data["capabilities"], "account.capabilities")
    capabilities = capabilities_from_mapping(cast(Mapping[str, Any], raw_capabilities))
    if raw_capabilities != capabilities.canonical:
        raise PaperError("engine state capabilities are not canonical")
    raw_cooldowns = _state_mapping(data["cooldowns"], "account.cooldowns")
    decoded_cooldowns = {
        key: _state_timestamp(_decode_value(item), f"account.cooldowns.{key}")
        for key, item in raw_cooldowns.items()
    }
    if cooldowns is None:
        cooldowns = decoded_cooldowns
    elif cooldowns != decoded_cooldowns:
        raise PaperError("engine state accounts do not share cooldown state")
    account = Account(
        name=_state_str(data["name"], "account.name"),
        professions=frozenset(
            _state_str(item, "account.profession")
            for item in _state_list(data["professions"], "account.professions")
        ),
        start_capital=_state_int(data["start_capital"], "account.start_capital"),
        cash_balance=_state_int(data["cash_balance"], "account.cash_balance"),
        cooldowns=cooldowns,
        crafting_capabilities=capabilities,
    )
    if account.start_capital < 0 or account.cash_balance < 0:
        raise PaperError("engine state account capital cannot be negative")
    inventory_ids: set[int] = set()
    for raw_item in _state_list(data["inventory"], "account.inventory"):
        item = _state_list(raw_item, "account.inventory entry")
        if len(item) != 2:
            raise PaperError("engine state inventory entry must have two values")
        item_id = _state_int(item[0], "account.inventory.item_id")
        quantity = _state_int(item[1], "account.inventory.quantity")
        if item_id <= 0 or quantity < 0 or item_id in inventory_ids:
            raise PaperError("engine state inventory contains a duplicate or invalid item")
        inventory_ids.add(item_id)
        account.inventory_balance[item_id] = quantity
    listing_ids: set[int] = set()
    for raw_listing in _state_list(data["listings"], "account.listings"):
        listing = _state_mapping(raw_listing, "account.listing")
        _state_keys(
            listing,
            {
                "listing_id",
                "item_id",
                "unit_price",
                "quantity",
                "original_quantity",
                "posted_t",
                "posted_step",
                "expires_t",
                "deposit_paid",
                "deposit_refunded",
            },
            "account.listing",
        )
        parsed = Listing(
            listing_id=_state_int(listing["listing_id"], "listing.listing_id"),
            item_id=_state_int(listing["item_id"], "listing.item_id"),
            unit_price=_state_int(listing["unit_price"], "listing.unit_price"),
            quantity=_state_int(listing["quantity"], "listing.quantity"),
            original_quantity=_state_int(listing["original_quantity"], "listing.original_quantity"),
            posted_t=_state_timestamp(listing["posted_t"], "listing.posted_t"),
            posted_step=_state_int(listing["posted_step"], "listing.posted_step"),
            expires_t=_state_timestamp(listing["expires_t"], "listing.expires_t"),
            deposit_paid=_state_int(listing["deposit_paid"], "listing.deposit_paid"),
            deposit_refunded=_state_int(listing["deposit_refunded"], "listing.deposit_refunded"),
        )
        if (
            parsed.listing_id <= 0
            or parsed.listing_id in listing_ids
            or parsed.item_id <= 0
            or parsed.unit_price <= 0
            or parsed.quantity < 0
            or parsed.original_quantity <= 0
            or parsed.quantity > parsed.original_quantity
            or parsed.posted_t < 0
            or parsed.posted_step < 0
            or parsed.expires_t < parsed.posted_t
            or parsed.deposit_paid < 0
            or parsed.deposit_refunded < 0
            or parsed.deposit_refunded > parsed.deposit_paid
        ):
            raise PaperError("engine state listing has an invalid range or duplicate id")
        listing_ids.add(parsed.listing_id)
        account.listings_by_id[parsed.listing_id] = parsed
    for raw_parcel in _state_list(data["mail"], "account.mail"):
        parcel = _state_mapping(raw_parcel, "account.mail entry")
        _state_keys(parcel, {"arrival_t", "cash", "items"}, "account.mail entry")
        items: dict[int, int] = {}
        for raw_item in _state_list(parcel["items"], "mail.items"):
            item = _state_list(raw_item, "mail.item")
            if len(item) != 2:
                raise PaperError("engine state mail item must have two values")
            item_id = _state_int(item[0], "mail.item_id")
            quantity = _state_int(item[1], "mail.quantity")
            if item_id <= 0 or quantity < 0 or item_id in items:
                raise PaperError("engine state mail contains an invalid or duplicate item")
            items[item_id] = quantity
        cash = _state_int(parcel["cash"], "mail.cash")
        if cash < 0:
            raise PaperError("engine state mail cash cannot be negative")
        arrival_t = _state_timestamp(parcel["arrival_t"], "mail.arrival_t")
        account.mail.append(Mail(arrival_t, cash, items))
    for raw_lot in _state_list(data["lots"], "account.lots"):
        lot = _state_mapping(raw_lot, "account.lot")
        _state_keys(lot, {"item_id", "quantity", "unit_cost", "acquired_t", "source"}, "account.lot")
        item_id = _state_int(lot["item_id"], "lot.item_id")
        quantity = _state_int(lot["quantity"], "lot.quantity")
        unit_cost = _decode_value(lot["unit_cost"])
        if (
            item_id <= 0
            or quantity <= 0
            or not isinstance(unit_cost, (int, Fraction))
            or isinstance(unit_cost, bool)
        ):
            raise PaperError("engine state lot is invalid")
        acquired_t = _state_timestamp(lot["acquired_t"], "lot.acquired_t")
        account.lots.setdefault(item_id, []).append(
            Lot(
                item_id,
                quantity,
                unit_cost,
                acquired_t,
                _state_str(lot["source"], "lot.source"),
            )
        )
    for field_name, target in (
        ("counts", account.counts),
        ("fees", account.fees),
        ("rejections", account.rejections),
    ):
        raw_values = _state_mapping(data[field_name], f"account.{field_name}")
        for key, item in raw_values.items():
            decoded = _decode_value(item)
            target[key] = _state_int(decoded, f"account.{field_name}.{key}")
            if target[key] < 0:
                raise PaperError(f"engine state account.{field_name}.{key} is negative")
    account.clicks = _state_int(data["clicks"], "account.clicks")
    if account.clicks < 0:
        raise PaperError("engine state clicks cannot be negative")
    return account


def _encode_accumulator(accumulator: StatisticsAccumulator) -> dict[str, object]:
    return {
        "last_t": accumulator.last_t,
        "covered_seconds": _encode_value(accumulator.covered_seconds),
        "purchased_units": accumulator.purchased_units,
        "purchased_copper": _encode_value(accumulator.purchased_copper),
        "volatility_eligible_seconds": _encode_value(accumulator.volatility_eligible_seconds),
        "volatility_count": accumulator.volatility_count,
        "volatility_sum": _encode_value(accumulator.volatility_sum),
        "volatility_sum_squares": _encode_value(accumulator.volatility_sum_squares),
        "regression_count": accumulator.regression_count,
        "regression_sum_x": _encode_value(accumulator.regression_sum_x),
        "regression_sum_y": _encode_value(accumulator.regression_sum_y),
        "regression_sum_xx": _encode_value(accumulator.regression_sum_xx),
        "regression_sum_xy": _encode_value(accumulator.regression_sum_xy),
        "duration_counts": list(accumulator.duration_counts),
        "duration_other": list(accumulator.duration_other),
        "fair_window": [[t, _encode_value(reference)] for t, reference in accumulator.fair_window],
    }


def _decode_accumulator(value: object, field: str) -> StatisticsAccumulator:
    data = _state_mapping(value, field)
    expected = {
        "last_t",
        "covered_seconds",
        "purchased_units",
        "purchased_copper",
        "volatility_eligible_seconds",
        "volatility_count",
        "volatility_sum",
        "volatility_sum_squares",
        "regression_count",
        "regression_sum_x",
        "regression_sum_y",
        "regression_sum_xx",
        "regression_sum_xy",
        "duration_counts",
        "duration_other",
        "fair_window",
    }
    _state_keys(data, expected, field)
    raw_copper = _decode_value(data["purchased_copper"])
    if not isinstance(raw_copper, Fraction):
        raise PaperError(f"{field}.purchased_copper is not a fraction")
    counts = tuple(
        _state_int(item, f"{field}.duration_counts") for item in _state_list(data["duration_counts"], field)
    )
    if len(counts) != int(MAX_GAP) + 1 or any(item < 0 for item in counts):
        raise PaperError(f"{field}.duration_counts has an invalid shape")
    other = tuple(
        _state_float(item, f"{field}.duration_other") for item in _state_list(data["duration_other"], field)
    )
    fair_window: list[tuple[float, Fraction | None]] = []
    for raw_entry in _state_list(data["fair_window"], field):
        entry = _state_list(raw_entry, f"{field}.fair_window entry")
        if len(entry) != 2:
            raise PaperError(f"{field}.fair_window entry is invalid")
        reference = _decode_value(entry[1])
        if reference is not None and not isinstance(reference, Fraction):
            raise PaperError(f"{field}.fair_window reference is invalid")
        fair_window.append((_state_timestamp(entry[0], f"{field}.fair_window.t"), reference))
    values = {
        name: _state_fraction(data[name], f"{field}.{name}")
        for name in (
            "covered_seconds",
            "volatility_eligible_seconds",
            "volatility_sum",
            "volatility_sum_squares",
            "regression_sum_x",
            "regression_sum_y",
            "regression_sum_xx",
            "regression_sum_xy",
        )
    }
    ints = {
        name: _state_int(data[name], f"{field}.{name}")
        for name in ("purchased_units", "volatility_count", "regression_count")
    }
    if (
        any(
            value < 0
            for value in (
                values["covered_seconds"],
                values["volatility_eligible_seconds"],
                values["volatility_sum_squares"],
                *ints.values(),
            )
        )
        or raw_copper < 0
    ):
        raise PaperError(f"{field} contains a negative accumulator")
    if values["volatility_eligible_seconds"] > values["covered_seconds"]:
        raise PaperError(f"{field}.volatility_eligible_seconds exceeds covered_seconds")
    if ints["purchased_units"] == 0 and raw_copper != 0:
        raise PaperError(f"{field}.purchased_copper is nonzero without purchased_units")
    if ints["regression_count"] > ints["volatility_count"]:
        raise PaperError(f"{field}.regression_count exceeds volatility_count")
    if ints["volatility_count"] == 0 and any(
        values[name] != 0 for name in ("volatility_sum", "volatility_sum_squares")
    ):
        raise PaperError(f"{field}.volatility sums are nonzero without observations")
    if ints["regression_count"] == 0 and any(
        values[name] != 0
        for name in ("regression_sum_x", "regression_sum_y", "regression_sum_xx", "regression_sum_xy")
    ):
        raise PaperError(f"{field}.regression sums are nonzero without observations")
    if ints["volatility_count"] and (
        values["volatility_sum_squares"]
        < values["volatility_sum"] * values["volatility_sum"] / ints["volatility_count"]
    ):
        raise PaperError(f"{field}.volatility_sum_squares is inconsistent with volatility_sum")
    if values["regression_sum_xx"] < 0:
        raise PaperError(f"{field}.regression_sum_xx is negative")
    if ints["regression_count"] and (
        values["regression_sum_xx"]
        < values["regression_sum_x"] * values["regression_sum_x"] / ints["regression_count"]
    ):
        raise PaperError(f"{field}.regression sums are inconsistent")
    if other:
        raise PaperError(f"{field}.duration_other must be empty for bounded state")
    if ints["regression_count"] != sum(counts) + len(other):
        raise PaperError(f"{field}.duration counts do not match regression_count")
    raw_last_t = data["last_t"]
    last_t = None if raw_last_t is None else _state_timestamp(raw_last_t, f"{field}.last_t")
    fair_times = [timestamp for timestamp, _reference in fair_window]
    if fair_times != sorted(set(fair_times)):
        raise PaperError(f"{field}.fair_window is not ordered or contains duplicates")
    if last_t is not None and any(timestamp > last_t for timestamp in fair_times):
        raise PaperError(f"{field}.fair_window extends beyond last_t")
    if fair_times and fair_times[-1] - fair_times[0] > 86400:
        raise PaperError(f"{field}.fair_window exceeds its 24-hour horizon")
    return StatisticsAccumulator(
        last_t=last_t,
        covered_seconds=values["covered_seconds"],
        purchased_units=ints["purchased_units"],
        volatility_eligible_seconds=values["volatility_eligible_seconds"],
        volatility_count=ints["volatility_count"],
        volatility_sum=values["volatility_sum"],
        volatility_sum_squares=values["volatility_sum_squares"],
        regression_count=ints["regression_count"],
        regression_sum_x=values["regression_sum_x"],
        regression_sum_y=values["regression_sum_y"],
        regression_sum_xx=values["regression_sum_xx"],
        regression_sum_xy=values["regression_sum_xy"],
        duration_counts=counts,
        duration_other=other,
        fair_window=tuple(fair_window),
        purchased_copper=raw_copper,
    )


def _state_blob(state: EngineState) -> dict[str, object]:
    return {
        "version": ENGINE_STATE_VERSION,
        "replay_ordinal": state.replay_ordinal,
        "next_listing_id": state.next_listing_id,
        "accounts": [_encode_checkpoint_account(account) for account in state.accounts],
        "snapshot_history": [
            [item_id, step, _encode_snapshot(snapshot)]
            for item_id, (step, snapshot) in sorted(state.snapshot_history.items())
        ],
        "strategies": [
            [name, _encode_value(state.strategy_states[name])] for name in sorted(state.strategy_states)
        ],
        "statistics": [
            [item_id, _encode_accumulator(accumulator)]
            for item_id, accumulator in sorted(state.statistics_accumulators.items())
        ],
    }


def _state_digest(blob: Mapping[str, object]) -> str:
    encoded = json.dumps(blob, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_CHAIN_ROOT = "0" * 64


def _step_chain_digest(previous: str, record: Mapping[str, Any]) -> str:
    content = {
        "cash": record["cash"],
        "equity": record["equity"],
        "realized": record["realized"],
        "replay_ordinal": record["replay_ordinal"],
        "step_t": record["t"],
        "sweep_key": record["sweep_key"],
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{previous}:{encoded}".encode()).hexdigest()


def _decode_state(blob: object) -> EngineState:
    data = _state_mapping(blob, "state")
    _state_keys(
        data,
        {
            "version",
            "replay_ordinal",
            "next_listing_id",
            "accounts",
            "snapshot_history",
            "strategies",
            "statistics",
        },
        "state",
    )
    if _state_int(data["version"], "version") != ENGINE_STATE_VERSION:
        raise PaperError("paper engine state version is unsupported")
    replay_ordinal = _state_int(data["replay_ordinal"], "replay_ordinal")
    next_listing_id = _state_int(data["next_listing_id"], "next_listing_id")
    if replay_ordinal < 0 or next_listing_id <= 0:
        raise PaperError("paper engine state ordinal or listing id is out of range")
    accounts: list[Account] = []
    cooldowns: dict[str, float] | None = None
    names: set[str] = set()
    listing_ids: set[int] = set()
    max_listing_id = 0
    for raw_account in _state_list(data["accounts"], "accounts"):
        account = _decode_checkpoint_account(raw_account, cooldowns)
        if account.name in names:
            raise PaperError("paper engine accounts contain duplicate names")
        names.add(account.name)
        for listing_id, listing in account.listings_by_id.items():
            if listing_id in listing_ids:
                raise PaperError("paper engine listings contain a duplicate id")
            if listing.posted_step > replay_ordinal:
                raise PaperError("paper engine listing was posted after the checkpoint")
            listing_ids.add(listing_id)
            max_listing_id = max(max_listing_id, listing_id)
        cooldowns = account.cooldowns
        accounts.append(account)
    snapshot_history: dict[int, tuple[int, ItemSnapshot]] = {}
    for raw_snapshot in _state_list(data["snapshot_history"], "snapshot_history"):
        snapshot = _state_list(raw_snapshot, "snapshot history entry")
        if len(snapshot) != 3:
            raise PaperError("paper engine snapshot history entry is invalid")
        item_id = _state_int(snapshot[0], "snapshot history.item_id")
        snapshot_step = _state_int(snapshot[1], "snapshot history.step")
        if item_id <= 0 or snapshot_step < 0 or snapshot_step > replay_ordinal or item_id in snapshot_history:
            raise PaperError("paper engine snapshot history contains a duplicate or invalid item")
        decoded_snapshot = _decode_snapshot(snapshot[2])
        if decoded_snapshot.item_id != item_id:
            raise PaperError("paper engine snapshot history item identity does not match")
        snapshot_history[item_id] = (
            snapshot_step,
            decoded_snapshot,
        )
    strategy_states: dict[str, Mapping[str, object]] = {}
    for raw_strategy in _state_list(data["strategies"], "strategies"):
        strategy = _state_list(raw_strategy, "strategy state")
        if len(strategy) != 2:
            raise PaperError("paper engine strategy state entry is invalid")
        name = _state_str(strategy[0], "strategy state name")
        decoded = _decode_value(strategy[1])
        if name in strategy_states or not isinstance(decoded, Mapping):
            raise PaperError("paper engine strategy state is duplicated or not an object")
        strategy_states[name] = cast(Mapping[str, object], decoded)
    statistics_accumulators: dict[int, StatisticsAccumulator] = {}
    for raw_accumulator in _state_list(data["statistics"], "statistics"):
        entry = _state_list(raw_accumulator, "statistics entry")
        if len(entry) != 2:
            raise PaperError("paper engine statistics entry is invalid")
        item_id = _state_int(entry[0], "statistics.item_id")
        if item_id <= 0 or item_id in statistics_accumulators:
            raise PaperError("paper engine statistics contains a duplicate or invalid item")
        statistics_accumulators[item_id] = _decode_accumulator(entry[1], f"statistics.{item_id}")
    if next_listing_id <= max_listing_id:
        raise PaperError("paper engine next_listing_id does not exceed live listing ids")
    state = EngineState(
        accounts=accounts,
        purchased={},
        next_listing_id=next_listing_id,
        snapshot_history=snapshot_history,
        strategy_states=strategy_states,
        statistics_accumulators=statistics_accumulators,
        replay_ordinal=replay_ordinal,
    )
    if _state_blob(state) != dict(data):
        raise PaperError("paper engine state is not canonical")
    return state


def _load_engine_state(conn: Any, run_id: int) -> tuple[int, EngineState] | None:
    row = conn.execute(
        """
        SELECT replay_ordinal, step_t, state_version, state, state_digest
        FROM paper_engine_state
        WHERE run_id = %s
        ORDER BY replay_ordinal DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    parsed = _EngineStateRow(
        _row_int(row[0], "engine state replay_ordinal"),
        _row_int(row[1], "engine state step_t"),
        _row_int(row[2], "engine state version"),
        row[3],
        _row_str(row[4], "engine state digest"),
    )
    if parsed.state_version != ENGINE_STATE_VERSION:
        raise PaperError("paper engine state version is unsupported")
    blob = _state_mapping(parsed.state, "state")
    if _state_digest(blob) != parsed.state_digest:
        raise PaperError(f"paper engine state digest mismatch at step {parsed.step_t}")
    decoded = _decode_state(blob)
    if decoded.replay_ordinal != parsed.replay_ordinal or _state_blob(decoded) != dict(blob):
        raise PaperError(f"paper engine state is not canonical at step {parsed.step_t}")
    return parsed.step_t, decoded


def _load_purchases(
    conn: Any, run_id: int, first_ordinal: int, last_ordinal: int
) -> dict[tuple[int, int | str], int]:
    rows = conn.execute(
        """
        SELECT item_id, result_id, quantity
        FROM paper_purchases
        WHERE run_id = %s AND replay_ordinal BETWEEN %s AND %s
        ORDER BY item_id, result_key, replay_ordinal
        """,
        (run_id, first_ordinal, last_ordinal),
    ).fetchall()
    result: dict[tuple[int, int | str], int] = {}
    for item_id, raw_result_id, quantity in rows:
        item = _row_int(item_id, "paper purchase item_id")
        result_id = _decode_value(raw_result_id)
        if not isinstance(result_id, (int, str)) or isinstance(result_id, bool):
            raise PaperError("paper purchase result_id is invalid")
        amount = _row_int(quantity, "paper purchase quantity")
        if item <= 0 or amount < 0:
            raise PaperError("paper purchase state is invalid")
        key = (item, result_id)
        result[key] = result.get(key, 0) + amount
    return result


def _replay(db_url: str, name: str) -> tuple[dict[str, Any], BacktestResult, float]:
    started = time.monotonic()
    with psycopg.connect(database_url(db_url)) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET TRANSACTION READ ONLY")
            run = _load_run(conn, name)
            if run["engine_contract"] != "replay-v1":
                raise PaperError("legacy paper runs are read-only and cannot be resumed")
            current_digest = engine_digest()
            if run["engine_digest"] != current_digest:
                raise PaperError("code changed since run creation; create a new run")
            checkpoint = _load_engine_state(conn, run["id"])
            anchor_t = run["start_t"] if checkpoint is None else checkpoint[0]
            window_start = max(0, anchor_t - 86400 - MAX_GAP)
            rows = conn.execute(
                """
                SELECT ms.client_version, ms.client_build, ms.id,
                       MAX((mo.query ->> 'startedAt')::bigint) AS step_t
                FROM market_sweeps AS ms
                JOIN market_observations AS mo ON mo.sweep_id = ms.id
                    AND mo.house_id = ms.house_id
                WHERE ms.house_id = %s
                  AND mo.kind = 'commodity'
                  AND mo.coverage IN ('full', 'bounded')
                  AND (mo.query ->> 'startedAt')::bigint >= %s
                GROUP BY ms.id, ms.client_version, ms.client_build
                ORDER BY step_t, ms.id
                """,
                (run["house_id"], window_start),
            ).fetchall()
            typed_rows = tuple(_SweepRow(*row) for row in rows)
            for row in typed_rows:
                if _full_build(row) != run["client_build"]:
                    raise PaperError("client build changed since run creation; launch will need a new run")
            loader = _ReadOnlyLoaderConnection(conn)
            books = load_commodity_books(
                loader,
                run["house"],
                start_t=window_start,
                client_build=run["client_build"],
            )
            represented_times = sweep_step_times(books)
            if checkpoint is None:
                replay_start = run["start_t"]
                initial_state = None
            else:
                replay_start = min(
                    (step_t for step_t in represented_times.values() if step_t > checkpoint[0]),
                    default=checkpoint[0] + 1,
                )
                initial_state = checkpoint[1]
                initial_state.purchased = _load_purchases(
                    conn,
                    run["id"],
                    max(0, initial_state.replay_ordinal - REPLAY_OVERLAP_ORDINALS),
                    initial_state.replay_ordinal,
                )
            names = _parse_strategies(run["strategies"])
            weights = [as_fraction(value) for value in run["weights"]]
            facts, recipes = load_client_data(loader, run["house"], run["client_build"])
            strategies = [build(name) for name in names]
            result = run_backtest(
                books,
                None,
                facts,
                recipes,
                strategies,
                weights,
                run["capital_copper"],
                start_t=replay_start,
                wind_down_hours=0,
                demand_share=as_fraction(run["demand_share"]),
                capabilities=run["capabilities"],
                rules=run["rules"],
                initial_state=initial_state,
                capture_states=True,
                ordinal_offset=0 if checkpoint is None else checkpoint[1].replay_ordinal + 1,
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
                    "hash_overlap_first": 0
                    if checkpoint is None
                    else max(0, checkpoint[1].replay_ordinal - REPLAY_OVERLAP_ORDINALS),
                    "hash_overlap_last": -1 if checkpoint is None else checkpoint[1].replay_ordinal,
                }
            )
    return run, result, time.monotonic() - started


def _copper(value: Any) -> str:
    amount = as_fraction(value)
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
                "replay_ordinal": int(step["replay_ordinal"]),
                "sweep_key": str(step["sweep_key"]),
                "equity": equity,
                "realized": realized,
                "cash": cash,
                "intents": step["strategies"],
            }
        )
    return records


def _record_from_engine_state(state: EngineState) -> dict[str, Any]:
    raw = state.step_record
    if raw is None:
        raise PaperError("paper engine state has no completed step record")
    data = _state_mapping(raw, "step record")
    equity_raw = _state_mapping(data.get("equity"), "step record.equity")
    realized_raw = _state_mapping(data.get("realized"), "step record.realized")
    cash_raw = _state_mapping(data.get("cash"), "step record.cash")
    intents = _state_mapping(data.get("intents"), "step record.intents")

    def copper_mapping(values: Mapping[str, object]) -> dict[str, str]:
        return {key: _copper(value) for key, value in values.items() if key != "total"} | {
            "total": _copper(values["total"])
        }

    return {
        "t": _state_int(data.get("t"), "step record.t"),
        "sweep_key": _state_str(data.get("sweep_key"), "step record.sweep_key"),
        "equity": copper_mapping(equity_raw),
        "realized": copper_mapping(realized_raw),
        "cash": copper_mapping(cash_raw),
        "intents": cast(dict[str, list[dict[str, object]]], dict(intents)),
    }


def _stored_steps(conn: Any, run_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT replay_ordinal, step_t, sweep_key, equity, realized, cash,
               previous_digest, step_digest
        FROM paper_steps
        WHERE run_id = %s
        ORDER BY step_t
        """,
        (run_id,),
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for raw_row in rows:
        row = _StoredStepRow(*raw_row)
        result[row.replay_ordinal] = {
            "replay_ordinal": row.replay_ordinal,
            "t": row.step_t,
            "sweep_key": row.sweep_key,
            "equity": row.equity,
            "realized": row.realized,
            "cash": row.cash,
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
    for raw_row in rows:
        row = _StoredIntentRow(*raw_row)
        strategy = row.strategy
        result.setdefault(strategy, []).append(
            {
                "intent": row.intent,
                "outcome": row.outcome,
                "detail": row.detail,
            }
        )
    return result


def _validate_existing(conn: Any, run_id: int, records: Sequence[dict[str, Any]]) -> set[int]:
    if not records:
        return set()
    expected = {record["replay_ordinal"]: record for record in records}
    first_ordinal = min(expected)
    last_ordinal = max(expected)
    rows = conn.execute(
        """
        SELECT ps.replay_ordinal, ps.step_t, ps.sweep_key, ps.equity, ps.realized, ps.cash,
               ps.previous_digest, ps.step_digest,
               pi.strategy, pi.ordinal, pi.intent, pi.outcome, pi.detail
        FROM paper_steps AS ps
        LEFT JOIN paper_intents AS pi
          ON pi.run_id = ps.run_id AND pi.step_t = ps.step_t
        WHERE ps.run_id = %s
          AND ps.replay_ordinal BETWEEN %s AND %s
        ORDER BY ps.replay_ordinal, pi.strategy, pi.ordinal
        """,
        (run_id, first_ordinal, last_ordinal),
    ).fetchall()
    stored: dict[int, dict[str, Any]] = {}
    for raw_row in rows:
        replay_ordinal = _row_int(raw_row[0], "stored paper replay_ordinal")
        step_t = _row_int(raw_row[1], "stored paper step_t")
        record = stored.setdefault(
            replay_ordinal,
            {
                "replay_ordinal": replay_ordinal,
                "t": step_t,
                "sweep_key": raw_row[2],
                "equity": raw_row[3],
                "realized": raw_row[4],
                "cash": raw_row[5],
                "previous_digest": _row_str(raw_row[6], "stored paper previous_digest"),
                "step_digest": _row_str(raw_row[7], "stored paper step_digest"),
                "intents": {},
            },
        )
        if raw_row[8] is not None:
            strategy = _row_str(raw_row[8], "stored paper strategy")
            record["intents"].setdefault(strategy, []).append(
                {
                    "intent": raw_row[10],
                    "outcome": _row_str(raw_row[11], "stored paper outcome"),
                    "detail": raw_row[12],
                }
            )
    unexpected = sorted(set(stored) - set(expected))
    if unexpected:
        raise PaperError(f"paper run diverged in replay ordinals (extra={unexpected})")
    previous = _CHAIN_ROOT
    if first_ordinal > 0:
        prior = conn.execute(
            """
            SELECT step_digest
            FROM paper_steps
            WHERE run_id = %s AND replay_ordinal = %s
            """,
            (run_id, first_ordinal - 1),
        ).fetchone()
        if prior is None:
            raise PaperError(f"paper run hash chain has no predecessor at replay ordinal {first_ordinal}")
        previous = _row_str(prior[0], "paper predecessor step_digest")
    for replay_ordinal in sorted(stored):
        stored_step = stored[replay_ordinal]
        record = expected.get(replay_ordinal)
        if record is None:
            raise PaperError(f"paper run diverged at replay ordinal {replay_ordinal}")
        for key in ("replay_ordinal", "t", "sweep_key", "equity", "realized", "cash"):
            if stored_step[key] != record[key]:
                raise PaperError(f"paper run diverged at replay ordinal {replay_ordinal}")
        expected_intents = {strategy: intents for strategy, intents in record["intents"].items() if intents}
        if stored_step["intents"] != expected_intents:
            raise PaperError(f"paper run diverged at replay ordinal {replay_ordinal}")
        if stored_step["previous_digest"] != previous or stored_step["step_digest"] != _step_chain_digest(
            previous, record
        ):
            raise PaperError(f"paper run hash chain diverged at replay ordinal {replay_ordinal}")
        previous = stored_step["step_digest"]
    return set(stored)


def _validate_hash_chain_overlap(conn: Any, run_id: int, first_ordinal: int, last_ordinal: int) -> None:
    """Validate persisted chain content before replaying from its checkpoint."""

    if last_ordinal < first_ordinal:
        return
    rows = conn.execute(
        """
        SELECT replay_ordinal, step_t, sweep_key, equity, realized, cash,
               previous_digest, step_digest
        FROM paper_steps
        WHERE run_id = %s AND replay_ordinal BETWEEN %s AND %s
        ORDER BY replay_ordinal
        """,
        (run_id, first_ordinal, last_ordinal),
    ).fetchall()
    if len(rows) != last_ordinal - first_ordinal + 1:
        raise PaperError("paper run hash chain overlap is incomplete")
    previous = _CHAIN_ROOT
    if first_ordinal > 0:
        predecessor = conn.execute(
            """
            SELECT step_digest
            FROM paper_steps
            WHERE run_id = %s AND replay_ordinal = %s
            """,
            (run_id, first_ordinal - 1),
        ).fetchone()
        if predecessor is None:
            raise PaperError(f"paper run hash chain has no predecessor at replay ordinal {first_ordinal}")
        previous = _row_str(predecessor[0], "paper predecessor step_digest")
    for expected_ordinal, raw_row in enumerate(rows, first_ordinal):
        row = _StoredStepRow(*raw_row)
        if row.replay_ordinal != expected_ordinal:
            raise PaperError(f"paper run hash chain has a gap at replay ordinal {expected_ordinal}")
        record = {
            "replay_ordinal": row.replay_ordinal,
            "t": row.step_t,
            "sweep_key": row.sweep_key,
            "equity": row.equity,
            "realized": row.realized,
            "cash": row.cash,
        }
        expected_digest = _step_chain_digest(previous, record)
        if row.previous_digest != previous or row.step_digest != expected_digest:
            raise PaperError(f"paper run hash chain diverged at replay ordinal {row.replay_ordinal}")
        previous = row.step_digest


def _ledger_json(event: LedgerEvent) -> dict[str, object]:
    return {
        "t": event.t,
        "kind": event.kind,
        "item_id": event.item_id,
        "quantity": event.quantity,
        "copper": event.copper,
        "unit_price": event.unit_price,
        "listing_id": event.listing_id,
        "reason": event.reason,
        "details": _encode_value(event.details),
    }


def _write_records(conn: Any, run_id: int, records: Sequence[dict[str, Any]], existing: set[int]) -> int:
    pending = [record for record in records if record["replay_ordinal"] not in existing]
    if not pending:
        return 0
    first_ordinal = min(record["replay_ordinal"] for record in pending)
    predecessor = conn.execute(
        """
        SELECT step_digest
        FROM paper_steps
        WHERE run_id = %s AND replay_ordinal < %s
        ORDER BY replay_ordinal DESC
        LIMIT 1
        """,
        (run_id, first_ordinal),
    ).fetchone()
    previous = (
        _CHAIN_ROOT if predecessor is None else _row_str(predecessor[0], "paper predecessor step_digest")
    )
    chain_rows: list[tuple[Any, ...]] = []
    for record in sorted(pending, key=lambda item: item["replay_ordinal"]):
        step_digest = _step_chain_digest(previous, record)
        chain_rows.append(
            (
                run_id,
                record["replay_ordinal"],
                record["t"],
                record["sweep_key"],
                Jsonb(record["equity"]),
                Jsonb(record["realized"]),
                Jsonb(record["cash"]),
                previous,
                step_digest,
            )
        )
        previous = step_digest
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO paper_steps
                (run_id, replay_ordinal, step_t, sweep_key, equity, realized, cash,
                 previous_digest, step_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            chain_rows,
        )
    intent_rows = [
        (
            run_id,
            record["t"],
            strategy,
            ordinal,
            Jsonb(entry["intent"]),
            entry["outcome"],
            Jsonb(entry["detail"]),
        )
        for record in pending
        for strategy, intents in record["intents"].items()
        for ordinal, entry in enumerate(intents)
    ]
    if intent_rows:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO paper_intents
                    (run_id, step_t, strategy, ordinal, intent, outcome, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                intent_rows,
            )
    ledger_rows = [
        (
            run_id,
            record["replay_ordinal"],
            account_name,
            ordinal,
            Jsonb(_ledger_json(event)),
        )
        for record in pending
        for account_name, events in record["ledger"].items()
        for ordinal, event in enumerate(events)
    ]
    if ledger_rows:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO paper_ledger
                    (run_id, replay_ordinal, account_name, event_ordinal, event)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ledger_rows,
            )
    trade_rows = [
        (
            run_id,
            record["replay_ordinal"],
            account_name,
            ordinal,
            Jsonb(_encode_value(trade)),
        )
        for record in pending
        for account_name, trades in record["trades"].items()
        for ordinal, trade in enumerate(trades)
    ]
    if trade_rows:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO paper_trades
                    (run_id, replay_ordinal, account_name, trade_ordinal, trade)
                VALUES (%s, %s, %s, %s, %s)
                """,
                trade_rows,
            )
    purchase_rows = []
    for record in pending:
        for (item_id, result_id), quantity in sorted(
            record["purchases"].items(), key=lambda pair: str(pair[0])
        ):
            encoded_result = _encode_value(result_id)
            purchase_rows.append(
                (
                    run_id,
                    record["replay_ordinal"],
                    item_id,
                    json.dumps(encoded_result, sort_keys=True, separators=(",", ":")),
                    Jsonb(encoded_result),
                    quantity,
                )
            )
    if purchase_rows:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO paper_purchases
                    (run_id, replay_ordinal, item_id, result_key, result_id, quantity)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                purchase_rows,
            )
    return len(pending)


def _validate_engine_states(
    conn: Any,
    run_id: int,
    state_records: Sequence[tuple[int, int, EngineState]],
) -> set[int]:
    if not state_records:
        return set()
    rows = conn.execute(
        """
        SELECT replay_ordinal, step_t, state_version, state, state_digest
        FROM paper_engine_state
        WHERE run_id = %s
          AND replay_ordinal = ANY(%s)
        ORDER BY replay_ordinal
        """,
        (run_id, [replay_ordinal for replay_ordinal, _step_t, _state in state_records]),
    ).fetchall()
    expected = {replay_ordinal: state for replay_ordinal, _step_t, state in state_records}
    stored: set[int] = set()
    for raw_row in rows:
        row = _EngineStateRow(
            _row_int(raw_row[0], "stored engine state replay_ordinal"),
            _row_int(raw_row[1], "stored engine state step_t"),
            _row_int(raw_row[2], "stored engine state version"),
            raw_row[3],
            _row_str(raw_row[4], "stored engine state digest"),
        )
        expected_state = expected.get(row.replay_ordinal)
        if expected_state is None:
            raise PaperError(f"paper engine state diverged at replay ordinal {row.replay_ordinal}")
        blob = _state_blob(expected_state)
        if (
            row.state_version != ENGINE_STATE_VERSION
            or row.state_digest != _state_digest(blob)
            or row.state != blob
        ):
            raise PaperError(f"paper engine state diverged at replay ordinal {row.replay_ordinal}")
        stored.add(row.replay_ordinal)
    return stored


def _write_engine_states(
    conn: Any,
    run_id: int,
    state_records: Sequence[tuple[int, int, EngineState]],
    existing: set[int],
) -> None:
    pending = [record for record in state_records if record[0] not in existing]
    if not pending:
        return
    rows = []
    for replay_ordinal, step_t, state in pending:
        blob = _state_blob(state)
        rows.append((run_id, replay_ordinal, step_t, ENGINE_STATE_VERSION, Jsonb(blob), _state_digest(blob)))
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO paper_engine_state
                (run_id, replay_ordinal, step_t, state_version, state, state_digest)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            rows,
        )


def _gold(value: Any) -> str:
    return f"{float(as_fraction(value)) / 10_000:.2f}g"


def _pending_intents(record: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        strategy: [entry["intent"] for entry in intents] for strategy, intents in record["intents"].items()
    }


def step_run(db_url: str, name: str) -> str:
    run, result, replay_seconds = _replay(db_url, name)
    records = _step_records(result)
    for index, record in enumerate(records):
        record["ledger"] = result.ledger_records[index] if index < len(result.ledger_records) else {}
        record["trades"] = result.trade_records[index] if index < len(result.trade_records) else {}
        record["purchases"] = result.purchase_records[index] if index < len(result.purchase_records) else {}
    final_records = records[:-1] if records else []
    pending_record = records[-1] if records else None
    state_records: list[tuple[int, int, EngineState]] = []
    if isinstance(result, BacktestResult) and records:
        if len(result.engine_states) != len(records):
            raise PaperError("paper replay did not produce one engine state per step")
        state_records = [
            (int(record["replay_ordinal"]), int(record["t"]), state)
            for record, state in zip(final_records, result.engine_states[:-1], strict=True)
        ]
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
            _validate_hash_chain_overlap(
                conn,
                run_id,
                int(result.metadata["hash_overlap_first"]),
                int(result.metadata["hash_overlap_last"]),
            )
            stored = _validate_existing(conn, run_id, final_records)
            added = _write_records(conn, run_id, final_records, stored)
            stored_states = _validate_engine_states(conn, run_id, state_records)
            _write_engine_states(conn, run_id, state_records, stored_states)
            conn.execute("DELETE FROM paper_pending WHERE run_id = %s", (run_id,))
            if pending_record is not None:
                conn.execute(
                    """
                    INSERT INTO paper_pending (run_id, replay_ordinal, step_t, sweep_key, intents)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        pending_record["replay_ordinal"],
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
                    (live_run["id"],),
                ).fetchone()
                if latest is None:
                    latest_t = "none"
                    total = _copper(run["capital_copper"])
                else:
                    latest_row = _LatestStepRow(*latest)
                    latest_t = latest_row.step_t
                    total = _json_mapping(latest_row.equity, "equity")["total"]
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
                status_step = _StatusStepRow(*step)
                step_t = status_step.step_t
                equity = _json_mapping(status_step.equity, "equity")
                realized = _json_mapping(status_step.realized, "realized")
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
                for raw_row in intents[-last_intents:] if last_intents else ():
                    row = _StoredIntentRow(*raw_row)
                    intent = json.dumps(row.intent, sort_keys=True, separators=(",", ":"))
                    detail = json.dumps(row.detail, sort_keys=True, separators=(",", ":"))
                    lines.append(
                        f"intent strategy={row.strategy} ordinal={row.ordinal} "
                        f"intent={intent} outcome={row.outcome} detail={detail}"
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
                pending_row = _PendingRow(*pending)
                lines.append("pending (outcome not final):")
                lines.append(f"pending_t={pending_row.step_t} pending_sweep_key={pending_row.sweep_key}")
                pending_values = _json_mapping(pending_row.intents, "pending intents")
                for strategy, intent_list in pending_values.items():
                    for ordinal, intent_value in enumerate(_json_list(intent_list, "pending intent list")):
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
