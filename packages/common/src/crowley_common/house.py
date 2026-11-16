"""Auction house identity.

WoW Forever is realmless: a house is a (region, ruleset, faction) triple, where faction
includes the shared goblin "neutral" houses. Every scan is attributed to exactly one house.
"""

from __future__ import annotations

from dataclasses import dataclass

from .auction_rules import rules_for_house

REGIONS = frozenset({"us", "eu", "kr", "tw", "cn", "beta"})
RULESETS = frozenset({"normal", "pvp", "rp", "hardcore"})
FACTIONS = frozenset({"alliance", "horde", "neutral"})


@dataclass(frozen=True)
class House:
    region: str
    ruleset: str
    faction: str

    def __post_init__(self) -> None:
        if self.region not in REGIONS:
            raise ValueError(f"unknown region {self.region!r}")
        if self.ruleset not in RULESETS:
            raise ValueError(f"unknown ruleset {self.ruleset!r}")
        if self.faction not in FACTIONS:
            raise ValueError(f"unknown faction {self.faction!r}")

    @classmethod
    def parse(cls, key: str) -> House:
        region, ruleset, faction = key.split("-")
        return cls(region, ruleset, faction)

    @property
    def key(self) -> str:
        return f"{self.region}-{self.ruleset}-{self.faction}"

    @property
    def ah_cut(self) -> float:
        """Compatibility view of the seller cut as a float."""

        return float(rules_for_house(self).seller_cut)

    def __str__(self) -> str:
        return self.key
