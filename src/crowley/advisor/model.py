"""Typed advisor output: pathways, exclusions, configuration and the confidence label.

Every evaluator returns a ``StrategyResult``.  Its invariant, ``considered == len(pathways) +
len(exclusions)``, is the "no silent drops" rule: a candidate either becomes a pathway or an
exclusion with a registered reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from crowley.analysis.time_to_sale import CHECKPOINTS

STRATEGIES = ("crafting", "recipe_purchase", "flip", "raw_materials", "vendor_arbitrage")
DEFAULT_PROFESSIONS = frozenset({"alchemy", "tailoring", "engineering"})
SECONDS_PER_HOUR = 3600

# Step actions: what a human (or later the executor) would do, in order.
ACTIONS = ("buy_ah", "buy_vendor", "learn_recipe", "craft", "list_ah", "sell_vendor", "gather")

# Reasons an evaluator may exclude a candidate for, by strategy.  ``Exclusion`` rejects others.
# Crafting passes through the reasons of ``analysis.rank`` / ``analysis.candidates`` with a
# ``rank:`` / ``candidate:`` prefix; those modules validate their own reason sets.
GENERIC_REASONS = ("below_min_profit", "below_min_volume", "non_positive_profit")
PASSTHROUGH_PREFIXES = {"crafting": ("rank:", "candidate:")}
EXCLUSION_REASONS: dict[str, tuple[str, ...]] = {
    "crafting": (
        "recipe_not_known",
        "low_sale_probability",
        "candidate_not_flagged",
        "no_current_listing",
    ),
    "recipe_purchase": (
        "no_character",
        "recipe_already_known",
        "not_learnable_now",
        "trainer_taught",
        "recipe_not_listed",
        "recipe_not_observed",
        "low_sale_probability",
        "no_current_listing",
    ),
    "flip": (
        "no_current_book",
        "no_client_facts",
        "too_few_books",
        "fair_unavailable",
        "fair_untrusted",
        "no_observed_demand",
        "no_units_below_threshold",
        "insufficient_independent_evidence",
    ),
    "raw_materials": (
        "no_commodity_book",
        "no_current_book",
        "no_current_listing",
        "no_observed_demand",
        "no_client_facts",
    ),
    "vendor_arbitrage": (
        "no_vendor_value",
        "no_current_book",
        "no_opportunity",
        "no_observed_demand",
    ),
}


@dataclass(frozen=True)
class Step:
    """One concrete action.  ``price`` is the unit price limit (buy) or listing price (list), copper."""

    action: str
    item_id: int | None = None
    spell_id: int | None = None
    quantity: int | None = None
    price: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown step action {self.action!r}")


@dataclass(frozen=True)
class Evidence:
    """The counts behind a probability or a rate.

    ``kind`` names what ``n`` counts, because the confidence thresholds differ:
    ``independent_starts`` (replayed listings at least one horizon apart; ``successes`` sold),
    ``arrival_groups`` (distinct cheap listings seen appearing) and ``demand_intervals``
    (usable consecutive-book intervals behind a units/h figure).
    """

    kind: str
    n: int
    successes: int | None = None
    usable: int | None = None
    detail: str = ""


# (high, medium) minimum counts per evidence kind; below medium is "low", no evidence "none".
CONFIDENCE_THRESHOLDS = {
    "independent_starts": (12, 6),
    "arrival_groups": (12, 6),
    "demand_intervals": (48, 24),
}


def confidence_label(evidence: Evidence | None) -> str:
    if evidence is None or evidence.n <= 0:
        return "none"
    try:
        high, medium = CONFIDENCE_THRESHOLDS[evidence.kind]
    except KeyError:
        raise ValueError(f"no confidence thresholds for evidence kind {evidence.kind!r}") from None
    if evidence.n >= high:
        return "high"
    if evidence.n >= medium:
        return "medium"
    return "low"


CONFIDENCE_ORDER = ("none", "low", "medium", "high")


def combined_confidence(*evidence: Evidence | None, cap: str = "high") -> str:
    """The weakest label among the given evidence, never above ``cap``.

    A pathway is as sure as its weakest count.  Pathways whose P(sale) is not measured pass
    ``cap="medium"`` (volume alone cannot make them high); stale cold-tier prices pass ``"low"``.
    """

    labels = [confidence_label(item) for item in evidence] + [cap]
    return min(labels, key=CONFIDENCE_ORDER.index)


@dataclass(frozen=True)
class Pathway:
    strategy: str
    key: str  # stable identity, e.g. "crafting:spell:123" (item ids and spell ids, never names)
    title: str  # display only
    steps: tuple[Step, ...]
    profit_per_unit: Fraction  # copper per unit sold/handled
    profit_per_craft: Fraction | None  # crafting only
    volume_per_hour: Fraction  # market units/h behind the pathway
    sale_probability: float | None  # P(sale within horizon) when measurable
    evidence: Evidence | None  # the sale-probability evidence (None: P not measured)
    demand_evidence: Evidence | None
    listed_units: int | None  # units currently listed on the AH for the product
    capital: Fraction  # copper tied up for one batch / one execution of the steps
    profit_per_hour: Fraction  # at ``share`` of the market flow
    confidence: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        if not self.steps:
            raise ValueError("a pathway needs at least one step")

    @property
    def threshold_profit(self) -> Fraction:
        """The figure ``--min-profit`` applies to: per craft for crafting, per unit otherwise."""

        return self.profit_per_unit if self.profit_per_craft is None else self.profit_per_craft


@dataclass(frozen=True)
class Exclusion:
    strategy: str
    key: str
    reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason in GENERIC_REASONS or self.reason in EXCLUSION_REASONS.get(self.strategy, ()):
            return
        if self.reason.startswith(PASSTHROUGH_PREFIXES.get(self.strategy, ())):
            return
        raise ValueError(f"unregistered {self.strategy} exclusion reason {self.reason!r}")


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    pathways: tuple[Pathway, ...]
    exclusions: tuple[Exclusion, ...]
    considered: int
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.considered != len(self.pathways) + len(self.exclusions):
            raise RuntimeError(
                f"{self.strategy}: {self.considered} considered but {len(self.pathways)} pathways "
                f"and {len(self.exclusions)} exclusions"
            )

    def exclusion_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.exclusions).items()))


@dataclass(frozen=True)
class AdviceConfig:
    strategies: frozenset[str] = frozenset(STRATEGIES)
    professions: frozenset[str] = DEFAULT_PROFESSIONS
    min_profit: int = 0  # copper, per craft (crafting) or per unit (the rest)
    min_volume: Fraction = Fraction(0)  # market units per hour
    share: Fraction = Fraction(1, 10)  # capture share of the observed market flow
    horizon_hours: float = 2.0
    min_sale_prob: float = 0.5  # crafting rows below it are excluded, not shown
    lookback_hours: float = 24.0  # crafting / demand window
    batch_crafts: int = 10
    min_independent_starts: int = 4
    crafting_price: str = "lowest"  # headline basis: "lowest" (1c under the lowest listing) | "target"
    # flip (defaults won on the beta calibration split)
    flip_x: Fraction = Fraction(7, 10)
    flip_undercut: Fraction = Fraction(12, 100)
    flip_lookback_hours: float = 72.0  # evidence window of replayed flips
    flip_min_trades: int = 10  # pooled independent past flips
    flip_duration_hours: int = 12
    flip_unsold_value: str = "cost"  # "cost" (carry the leftovers) | "vendor" (dump them)

    def __post_init__(self) -> None:
        unknown = sorted(self.strategies - set(STRATEGIES))
        if unknown:
            raise ValueError(f"unknown strategies {unknown}; known: {list(STRATEGIES)}")
        if not self.strategies:
            raise ValueError("at least one strategy is required")
        if not self.professions:
            raise ValueError("at least one profession is required")
        if not 0 < self.share <= 1:
            raise ValueError("share must be in (0, 1]")
        if round(self.horizon_hours * SECONDS_PER_HOUR) not in CHECKPOINTS:
            raise ValueError(
                f"horizon_hours must be one of {[c / SECONDS_PER_HOUR for c in CHECKPOINTS]} "
                "(the replay checkpoints)"
            )
        if self.min_profit < 0 or self.min_volume < 0:
            raise ValueError("min_profit and min_volume must be non-negative")
        if not 0 <= self.min_sale_prob <= 1:
            raise ValueError("min_sale_prob must be in [0, 1]")
        if self.crafting_price not in ("lowest", "target"):
            raise ValueError("crafting_price must be 'lowest' or 'target'")
        if self.flip_unsold_value not in ("cost", "vendor"):
            raise ValueError("flip_unsold_value must be 'cost' or 'vendor'")
        if not 0 < self.flip_x <= 1 or not 0 <= self.flip_undercut < 1:
            raise ValueError("flip_x must be in (0, 1] and flip_undercut in [0, 1)")
        if self.flip_min_trades < 1 or self.min_independent_starts < 1 or self.batch_crafts < 1:
            raise ValueError("flip_min_trades, min_independent_starts and batch_crafts must be positive")
        if self.lookback_hours <= 0 or self.flip_lookback_hours <= 0:
            raise ValueError("lookback hours must be positive")

    @property
    def horizon_seconds(self) -> int:
        return round(self.horizon_hours * SECONDS_PER_HOUR)


@dataclass(frozen=True)
class Advice:
    """The whole run: per-strategy results plus the filtered, sorted pathway list."""

    house_key: str
    client_build: str
    as_of_t: float
    sweep_id: int | str
    config: AdviceConfig
    results: tuple[StrategyResult, ...]  # after the min-profit / min-volume filters
    pathways: tuple[Pathway, ...]  # all strategies, sorted by profit per hour

    def exclusion_counts(self) -> dict[str, dict[str, int]]:
        return {result.strategy: result.exclusion_counts() for result in self.results}
