"""The shared, read-only market state every evaluator consumes.

``load_market_state`` reads one house and one client build as of one time, once.  Evaluators
get the resulting ``MarketState`` and never touch the database.  It wraps the analysis
package's ``RankInputs`` (commodity books, item-kind books, recipes, facts, rules, account)
and adds the cold pool, display names and the gatherable-goods catalog tags.

Scoping: the latest sweep at or before ``as_of`` for the house and build fixes ``as_of_t``;
every book, the cold pool and the account snapshot are restricted to that house, that build
and ``<= as_of_t``.  Nothing from a later sweep is loaded.  The character capture follows ``as_of``
by default; with ``account_follows_as_of=False`` it is read at ``account_as_of`` instead (None: the
latest capture), because a capture can be taken after the sweep it is used with.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from crowley.advisor.model import OptionError
from crowley.analysis.books import Book
from crowley.analysis.candidates import ColdPool, load_candidate_inputs
from crowley.analysis.item_books import ItemBook
from crowley.analysis.rank import RankConfig, RankInputs, item_names
from crowley.core.auction_rules import AuctionHouseRules
from crowley.core.watchlist import load_catalog
from crowley.trading.account_gate import AccountSnapshot
from crowley.trading.api import ItemFacts, Recipe

# Catalog tags of trade goods a character can gather or farm (leather, hides, herbs, cloth,
# ore, stone, scales and similar; bars are smelted, so they are not here).  The catalog is the
# plan's own classification.
GATHERABLE_TAGS = frozenset(
    {
        "herb",
        "leather",
        "specialty-leather",
        "cloth",
        "silk",
        "ore",
        "stone",
        "scale",
        "pearl",
        "fish",
        "elemental",
        "beast-reagent",
        "venom",
    }
)
FAIR_LOOKBACK_HOURS = 72.0  # candidates' fair-value history; the books are loaded this far back


@dataclass(frozen=True)
class MarketState:
    inputs: RankInputs
    client_build: str
    professions: frozenset[str]
    lookback_hours: float
    cold_pool: ColdPool | None  # None only for hand-built states in tests
    names: Mapping[int, str]  # display only; nothing is ever keyed by name
    gatherable: Mapping[int, tuple[str, ...]]  # item id -> its gatherable catalog tags

    @property
    def house_key(self) -> str:
        return self.inputs.house_key

    @property
    def as_of_t(self) -> float:
        return self.inputs.as_of_t

    @property
    def sweep_id(self) -> int | str:
        return self.inputs.latest_sweep_id

    @property
    def books(self) -> Mapping[int, Sequence[Book]]:
        return self.inputs.books

    @property
    def item_books(self) -> Mapping[int, Sequence[ItemBook]]:
        return self.inputs.item_books or {}

    @property
    def recipes(self) -> Sequence[Recipe]:
        return self.inputs.recipes

    @property
    def facts(self) -> Mapping[int, ItemFacts]:
        return self.inputs.facts

    @property
    def rules(self) -> AuctionHouseRules:
        return self.inputs.rules

    @property
    def account(self) -> AccountSnapshot | None:
        return self.inputs.account

    @property
    def vendor_supplies(self) -> Mapping[int, ItemFacts]:
        """Items a vendor sells (bundle size and price known)."""

        return {item_id: fact for item_id, fact in self.facts.items() if fact.vendor_bundle_price is not None}

    def name(self, item_id: int) -> str:
        return self.names.get(item_id, f"item {item_id}")


def gatherable_items() -> dict[int, tuple[str, ...]]:
    """Active trade goods of the plan that carry a gatherable tag."""

    catalog = load_catalog()
    return {
        item.item_id: tuple(sorted(GATHERABLE_TAGS.intersection(item.tags)))
        for item in catalog.items
        if item.status == "active"
        and item.category == "trade_goods"
        and GATHERABLE_TAGS.intersection(item.tags)
    }


def load_market_state(
    conn: Any,
    house_key: str,
    *,
    client_build: str | None = None,
    as_of: datetime | None = None,
    character: str | None = None,
    professions: frozenset[str],
    lookback_hours: float = 24.0,
    account_as_of: datetime | None = None,
    account_follows_as_of: bool = True,
) -> MarketState:
    """Read one house and build as of ``as_of`` (default: its latest sweep), read-only.

    ``conn`` must be a plain connection; the reads run in one read-only transaction.
    """

    config = RankConfig(professions=professions, lookback_hours=lookback_hours)
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        loaded = load_candidate_inputs(
            conn,
            house_key,
            config,
            client_build=client_build,
            character=character,
            as_of=as_of,
            fair_lookback_hours=FAIR_LOOKBACK_HOURS,
            account_as_of=account_as_of,
            account_follows_as_of=account_follows_as_of,
        )
        inputs = loaded.loaded.inputs
        known = {profession for recipe in inputs.recipes for profession in recipe.professions}
        if professions - known:
            raise OptionError(
                f"no client recipes for profession(s) {sorted(professions - known)}; "
                f"known here: {sorted(known)}"
            )
        scanned: set[int] = set(inputs.books) | set(inputs.item_books or {})
        names = {**item_names(conn, loaded.loaded.client_build, sorted(scanned)), **loaded.loaded.names}
    return MarketState(
        inputs=loaded.loaded.inputs,
        client_build=loaded.loaded.client_build,
        professions=professions,
        lookback_hours=lookback_hours,
        cold_pool=loaded.cold_pool,
        names=names,
        gatherable=gatherable_items(),
    )
