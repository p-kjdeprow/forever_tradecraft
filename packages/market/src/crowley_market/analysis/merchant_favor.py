"""Read-only, depth-aware comparisons for Alliance trade-faction turn-ins."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from importlib import resources
from typing import Any

from crowley_common.errors import MarketDataNotFound
from crowley_common.house import House

from .trading_summary import exact_money

MAX_PRICE_AGE = 30 * 60


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    data = json.loads(resources.files("crowley_market.resources").joinpath("merchant_favor.json").read_text())
    if data["schema_version"] != 1:
        raise ValueError("unsupported Merchant's Favor catalog")
    return data


def scan_item_ids() -> list[int]:
    return sorted(
        {c["item_id"] for c in catalog()["crates"]}
        | {o["item_id"] for c in catalog()["crates"] for o in c["options"]}
    )


@dataclass(frozen=True)
class PriceBook:
    item_id: int
    state: str
    observed_at: int | None = None
    item_key: str | None = None
    levels: tuple[tuple[Fraction, int], ...] = ()

    def quote(self, quantity: int) -> Fraction | None:
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("quote quantity must be a positive integer")
        if self.state != "priced":
            return None
        remaining, cost = quantity, Fraction(0)
        for price, available in sorted(self.levels):
            count = min(remaining, available)
            cost += price * count
            remaining -= count
            if not remaining:
                return cost
        return None

    @property
    def supply(self) -> int:
        return sum(q for _, q in self.levels) if self.state == "priced" else 0


def build_comparison(
    books: dict[int, PriceBook],
    *,
    house: str,
    build: str,
    sweep_id: int,
    finished_epoch: int,
    now_epoch: float,
) -> dict[str, Any]:
    """Rank complete next-turn-in quotes, never unlimited bulk market supply."""
    if house != "us-normal-alliance":
        raise ValueError("Merchant's Favor catalog is scoped to us-normal-alliance")
    data = catalog()
    fills, forgeries, coverage = [], [], []
    for item_id in scan_item_ids():
        b = books.get(item_id, PriceBook(item_id, "awaiting_scan"))
        coverage.append({"item_id": item_id, "state": b.state, "observed_at": b.observed_at})
    for crate in data["crates"]:
        b = books.get(crate["item_id"], PriceBook(crate["item_id"], "awaiting_scan"))
        crate_price = b.quote(1)
        choices = []
        for option in crate["options"]:
            m = books.get(option["item_id"], PriceBook(option["item_id"], "awaiting_scan"))
            material_price = m.quote(option["quantity"])
            gross = None if crate_price is None or material_price is None else crate_price + material_price
            net = None if gross is None or crate["money"] is None else gross - crate["money"]
            choices.append(
                {
                    **option,
                    "material_state": m.state,
                    "material_item_key": m.item_key,
                    "material_cost": None if material_price is None else exact_money(material_price),
                    "gross_cost": None if gross is None else exact_money(gross),
                    "net_cost": None if net is None else exact_money(net),
                    "cost_per_favor": None if net is None else exact_money(net / crate["favor"]),
                    "available_turnins": min(b.supply, m.supply // option["quantity"]),
                    "observed_at": min(b.observed_at or 0, m.observed_at or 0) or None,
                }
            )
        choices.sort(key=lambda o: (o["net_cost"] is None, Fraction(o["net_cost"] or 0), o["item_id"]))
        fills.append(
            {
                **{k: v for k, v in crate.items() if k != "options"},
                "crate_state": b.state,
                "crate_item_key": b.item_key,
                "crate_price": None if crate_price is None else exact_money(crate_price),
                "crate_supply": b.supply,
                "options": choices,
                "best": next((o for o in choices if o["net_cost"] is not None), None),
            }
        )
        # Filling level does not establish the server-side forging target whitelist.
        forgeries.append(
            {
                "item_id": crate["item_id"],
                "name": crate["name"],
                "item_key": b.item_key,
                "crate_state": b.state,
                "crate_price": None if crate_price is None else exact_money(crate_price),
                "crate_supply": b.supply,
                "fill_min_level": crate["min_level"],
                "favor_cost": data["forgery"]["favor_cost"],
                "reputation": data["forgery"]["reputation"],
                "money": data["forgery"]["money"],
                "observed_at": b.observed_at,
            }
        )
    fills.sort(
        key=lambda c: (
            c["best"] is None,
            Fraction(c["best"]["cost_per_favor"]) if c["best"] else 0,
            c["item_id"],
        )
    )
    forgeries.sort(key=lambda c: (c["crate_price"] is None, Fraction(c["crate_price"] or 0), c["item_id"]))
    return {
        "house": house,
        "client_build": build,
        "sweep_id": sweep_id,
        "finished_epoch": finished_epoch,
        "generated_epoch": now_epoch,
        "max_price_age_seconds": MAX_PRICE_AGE,
        "catalog": data,
        "fills": fills,
        "forgeries": forgeries,
        "coverage": coverage,
        "scope": (
            "Complete next-turn-in AH quotes. Available turn-ins are independent alternatives, "
            "not additive stock. Bulk budgets extrapolate unit costs and are not executable shopping quotes."
        ),
    }


def _book(item_id: int, observation: dict[str, Any], rows: list[tuple], now: float) -> PriceBook:
    query = observation["query"]
    if query.get("kind") == "unqueried":
        return PriceBook(item_id, "unqueried")
    started, finished = query.get("startedAt"), query.get("finishedAt")
    if (
        query.get("kind") != "queried"
        or type(started) is not int
        or type(finished) is not int
        or finished < started
    ):
        raise ValueError("Merchant's Favor query timestamps are invalid")
    if started > now + 5 or now - started > MAX_PRICE_AGE:
        return PriceBook(item_id, "stale", started)
    if (
        observation["reason_code"] != "ok"
        or observation["coverage"] not in {"full", "bounded"}
        or observation["kind"] not in {"commodity", "item"}
        or (observation["kind"] == "item" and observation["source_complete"] is not True)
    ):
        return PriceBook(item_id, "query_failed", started)
    keys, levels = set(), []
    excluded_own = False
    for key, raw, numerator, denominator, quantity, row_data in rows:
        if (
            type(numerator) is not int
            or type(denominator) is not int
            or denominator <= 0
            or type(quantity) is not int
            or quantity <= 0
        ):
            raise ValueError("Merchant's Favor price row is invalid")
        price = Fraction(numerator, denominator)
        if price <= 0:
            continue
        owners = row_data.get("owners", {})
        if (
            type(owners.get("containsOwnerItem")) is not bool
            or type(owners.get("containsAccountItem")) is not bool
        ):
            return PriceBook(item_id, "ownership_unknown", started)
        if owners["containsOwnerItem"] or owners["containsAccountItem"]:
            # Mixed groups do not expose an independently purchasable quantity.
            excluded_own = True
            continue
        if (
            raw.get("itemID") != item_id
            or raw.get("itemSuffix") != 0
            or raw.get("battlePetSpeciesID") != 0
            or raw.get("bonusIDs")
        ):
            return PriceBook(item_id, "ambiguous_variant", started)
        keys.add(key)
        levels.append((price, quantity))
    if len(keys) > 1:
        return PriceBook(item_id, "ambiguous_variant", started)
    if not levels:
        state = (
            "no_buyable_supply"
            if excluded_own
            else ("not_listed" if observation["coverage"] == "full" else "insufficient_depth")
        )
        return PriceBook(item_id, state, started)
    return PriceBook(item_id, "priced", started, next(iter(keys)), tuple(levels))


def load_merchant_favor(conn: Any, house: str, *, now_epoch: float | None = None) -> dict[str, Any]:
    House.parse(house)
    if house != "us-normal-alliance":
        raise ValueError("Merchant's Favor supports the configured Alliance auction house")
    now = time.time() if now_epoch is None else now_epoch
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("invalid comparison time")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        latest = conn.execute(
            """SELECT ms.id, EXTRACT(EPOCH FROM ms.finished_at)::bigint,
                      ms.client_version || '.' || ms.client_build
               FROM market_sweeps ms JOIN houses h ON h.id=ms.house_id
               WHERE h.key=%s ORDER BY ms.finished_at DESC, ms.id DESC LIMIT 1""",
            (house,),
        ).fetchone()
        if latest is None:
            raise MarketDataNotFound(f"no market sweep for house {house!r}")
        sweep_id, finished, build = latest
        if str(build) != catalog()["client_build"]:
            raise MarketDataNotFound("Merchant's Favor catalog does not match the latest client build")
        observations = conn.execute(
            """SELECT item_id, observation FROM market_observations
               WHERE sweep_id=%s AND item_id=ANY(%s)""",
            (sweep_id, scan_item_ids()),
        ).fetchall()
        captured = conn.execute(
            """SELECT item_id, item_key, raw_item_key, unit_numerator, unit_denominator, quantity, row_data
               FROM market_captured_rows WHERE sweep_id=%s AND item_id=ANY(%s)""",
            (sweep_id, scan_item_ids()),
        ).fetchall()
        by_item: dict[int, list[tuple]] = {}
        for item_id, *row in captured:
            by_item.setdefault(int(item_id), []).append(tuple(row))
        # Database observation JSON uses the original camelCase wire contract.
        books = {}
        for item_id, raw in observations:
            normalized = {
                "query": raw["query"],
                "kind": raw["kind"],
                "coverage": raw["coverage"],
                "reason_code": raw["reasonCode"],
                "source_complete": raw["sourceComplete"],
            }
            books[int(item_id)] = _book(int(item_id), normalized, by_item.get(int(item_id), []), now)
        return build_comparison(
            books,
            house=house,
            build=str(build),
            sweep_id=int(sweep_id),
            finished_epoch=int(finished),
            now_epoch=now,
        )
