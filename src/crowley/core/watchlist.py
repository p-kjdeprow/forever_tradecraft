"""The reviewed item catalog and the first-run plan delivered to the companion.

Catalog admission authorizes observation, never buying. Recipe availability, liquidity
and profitability still need live-client and market evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .game_target import FIXED_COLLECTION_CADENCE_SECONDS, CatalogLike, packaged_game_target, target_scan_plan

Status = Literal["active", "research", "excluded"]
Category = Literal[
    "trade_goods",
    "components",
    "consumables",
    "containers",
    "enhancements",
    "recipes",
    "equipment",
    "utility",
]
Profession = Literal[
    "alchemy",
    "blacksmithing",
    "cooking",
    "enchanting",
    "engineering",
    "first-aid",
    "leatherworking",
    "tailoring",
    "mining",
    "herbalism",
    "skinning",
    "fishing",
]


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogItem(CatalogModel):
    item_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    category: Category
    tags: list[str] = Field(min_length=1)
    professions: list[Profession]
    roles: list[Literal["input", "output", "intermediate", "recipe", "discovery"]] = Field(min_length=1)
    status: Status
    pool: Literal["core", "discovery", "cold"]
    reason: str = Field(min_length=1)
    evidence: Literal["client_data", "live_client"]
    auction_eligibility: Literal["unverified", "client_tradeable", "live_verified", "not_tradeable"]
    binding: Literal["unknown", "none_in_tooltip", "bind_on_equip", "bind_on_pickup"]
    source_urls: list[HttpUrl] = Field(min_length=1)

    @model_validator(mode="after")
    def check_admission(self) -> CatalogItem:
        if self.status == "active" and (
            self.auction_eligibility == "not_tradeable" or self.binding == "bind_on_pickup"
        ):
            raise ValueError(f"nontradeable item {self.item_id} cannot be active")
        for name in ("tags", "professions", "roles", "source_urls"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"item {self.item_id} has duplicate {name}")
        return self


class Catalog(CatalogModel):
    schema_version: Literal[1]
    id: Literal["first-run"]
    revision: int = Field(gt=0)
    client_build: str = Field(pattern=r"^\d+\.\d+\.\d+\.\d+$")
    reviewed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_price_levels: int = Field(gt=0)
    items: list[CatalogItem] = Field(min_length=1)

    @model_validator(mode="after")
    def check_identity(self) -> Catalog:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate item IDs: merge memberships into one entry")
        if ids != sorted(ids):
            raise ValueError("catalog items must be sorted by item_id")
        if not any(item.status == "active" for item in self.items):
            raise ValueError("the first-run plan must contain active items")
        return self

    @property
    def catalog_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def scan_plan(
        self, *, cadence_seconds: int = FIXED_COLLECTION_CADENCE_SECONDS, cold_slices: int = 0
    ) -> dict:
        """Only active IDs enter the plan; research/excluded entries remain reviewable.

        ``cold_slices`` (N) comes from the target.  Cold items rotate: cold item i of
        ``pools.cold`` belongs to slice ``i % N``.  N > 0 exactly when cold items exist.
        """
        if isinstance(cold_slices, bool) or not isinstance(cold_slices, int) or cold_slices < 0:
            raise ValueError("cold_slices must be an integer >= 0")
        if (
            isinstance(cadence_seconds, bool)
            or not isinstance(cadence_seconds, int)
            or cadence_seconds != FIXED_COLLECTION_CADENCE_SECONDS
        ):
            raise ValueError(
                f"cadence_seconds must use the fixed collection policy {FIXED_COLLECTION_CADENCE_SECONDS}"
            )
        active = [item for item in self.items if item.status == "active"]
        has_cold = any(item.pool == "cold" for item in active)
        if (cold_slices > 0) != has_cold:
            raise ValueError(
                f"cold_slices={cold_slices} requires cold items and cold items require cold_slices > 0 "
                f"(active cold items: {sum(item.pool == 'cold' for item in active)})"
            )
        body = {
            "schemaVersion": 2,
            "id": self.id,
            "revision": self.revision,
            "catalogHash": self.catalog_hash,
            "targetClientBuild": self.client_build,
            "cadenceSeconds": cadence_seconds,
            "coldSlices": cold_slices,
            "initialPriceLevels": self.initial_price_levels,
            "itemIDs": [item.item_id for item in active],
            "pools": {
                pool: [item.item_id for item in active if item.pool == pool]
                for pool in ("core", "discovery", "cold")
            },
        }
        plan_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return {
            **body,
            "planHash": hashlib.sha256(plan_json.encode("utf-8")).hexdigest(),
            "planJSON": plan_json,
        }


def _read_catalog(source: Any) -> Catalog:
    return Catalog.model_validate_json(source.read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def load_catalog(*, enforce_target: bool = True) -> Catalog:
    source = resources.files("crowley.data").joinpath("first_run.json")
    catalog = _read_catalog(source)
    if enforce_target:
        packaged_game_target().validate_catalog(cast(CatalogLike, catalog))
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(prog="crowley-watchlist")
    parser.add_argument(
        "--catalog", type=Path, help="validate/inspect this file instead of the packaged catalog"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary", help="validate the catalog and show coverage")
    sub.add_parser("export", help="print the active scan plan as JSON")
    items = sub.add_parser("items", help="list catalog entries as tab-separated rows")
    items.add_argument("--status", choices=("active", "research", "excluded", "all"), default="active")
    items.add_argument("--profession", choices=get_args(Profession))
    items.add_argument("--category", choices=get_args(Category))
    args = parser.parse_args()
    catalog = (
        Catalog.model_validate_json(args.catalog.read_text(encoding="utf-8"))
        if args.catalog is not None
        else load_catalog()
    )
    if args.command == "export":
        if args.catalog is None:
            plan = target_scan_plan(cast(CatalogLike, catalog))
        else:
            plan = catalog.scan_plan()
        print(json.dumps(plan, indent=2))
    elif args.command == "summary":
        active = [item for item in catalog.items if item.status == "active"]
        print(f"{catalog.id} revision {catalog.revision}; client build {catalog.client_build}")
        print(f"catalog hash: {catalog.catalog_hash}")
        print(
            "status: "
            + ", ".join(f"{k}={v}" for k, v in sorted(Counter(i.status for i in catalog.items).items()))
        )
        print(
            "active categories: "
            + ", ".join(f"{k}={v}" for k, v in sorted(Counter(i.category for i in active).items()))
        )
        professions = Counter(p for item in active for p in item.professions)
        print(
            "active profession memberships (overlap): "
            + ", ".join(f"{k}={v}" for k, v in sorted(professions.items()))
        )
    else:
        print("item_id\tname\tstatus\tcategory\tprofessions\treason")
        for item in catalog.items:
            if args.status != "all" and item.status != args.status:
                continue
            if args.profession and args.profession not in item.professions:
                continue
            if args.category and item.category != args.category:
                continue
            print(
                f"{item.item_id}\t{item.name}\t{item.status}\t{item.category}\t{','.join(item.professions)}\t{item.reason}"
            )


if __name__ == "__main__":
    main()
