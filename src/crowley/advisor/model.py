"""Typed advisor output: pathways, exclusions, configuration and the confidence rules.

Every evaluator returns a ``StrategyResult``.  Its invariant, ``considered == len(pathways) +
len(exclusions)``, is the "no silent drops" rule: a candidate either becomes a pathway or an
exclusion with a registered reason.

Sections (a pathway is ranked only against pathways whose value means the same thing):

* ``ranked``: a recurring flow with a measured P(sale).  ``sort_value`` is expected profit per
  hour, ``margin x crafts/h x P(sale)`` (a pathway that sells with probability P earns P of its
  margin per attempt). Crafting crafts/h is capped by recipe cooldown; recipes in one cooldown
  group share a timer. Crafting and recipe purchases.
* ``unmeasured``: a recurring flow whose P(sale) cannot be measured (cold-tier crafts, vendor to
  AH).  ``sort_value`` is the same formula taken at P = 1, so it is an upper bound.
* ``one_off``: an opportunity listed right now (flips, vendor-sell arbitrage).  ``sort_value`` is
  its expected profit once; there is no per-hour figure.
* ``farming``: gatherable goods.  ``sort_value`` is the market's value flowing per hour, a
  capacity figure and not the character's income.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from crowley.analysis.time_to_sale import CHECKPOINTS
from crowley.trading.strategies.common import SECONDS_PER_HOUR

if TYPE_CHECKING:
    from .estimates import ForecastSummary

STRATEGIES = ("crafting", "recipe_purchase", "flip", "raw_materials", "vendor_arbitrage")
SECTIONS = ("ranked", "unmeasured", "one_off", "farming")
DEFAULT_PROFESSIONS = frozenset({"alchemy", "tailoring", "engineering"})

# Step actions: what a human (or later the executor) would do, in order.
ACTIONS = (
    "verify_price",
    "buy_ah",
    "buy_vendor",
    "learn_recipe",
    "craft",
    "list_ah",
    "sell_vendor",
    "gather",
)


class OptionError(ValueError):
    """A request option is invalid (an HTTP 400).  Other ValueErrors are bugs and stay 500s."""


# Reasons an evaluator may exclude a candidate for, by strategy.  ``Exclusion`` rejects others.
# Crafting passes through the reasons of ``analysis.rank`` / ``analysis.candidates`` with a
# ``rank:`` / ``candidate:`` prefix; those modules validate their own reason sets.
GENERIC_REASONS = ("below_min_profit", "below_min_volume", "non_positive_profit")
PASSTHROUGH_PREFIXES = {"crafting": ("rank:", "candidate:")}
EXCLUSION_REASONS: dict[str, tuple[str, ...]] = {
    "crafting": (
        "recipe_not_known",
        "low_sale_probability",
        "no_current_listing",
        "no_target_price",
        "no_observed_demand",
        "stale_observation",
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
CONFIDENCE_ORDER = ("none", "low", "medium", "high")


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


def _weaker(*labels: str) -> str:
    return min(labels, key=CONFIDENCE_ORDER.index)


def pathway_confidence(sale: Evidence | None, demand: Evidence | None) -> str:
    """The one confidence rule, for every strategy.

    * With sale evidence (a measured P or fill from independent starts of the pathway's own item or
      recipe), the label comes from that count, never below "low", and is capped by the demand
      evidence when there is some.  An item with no starts of its own is "low".
    * Without sale evidence the pathway's sale is unmeasured: at best "low", and "none" when even the
      demand evidence is empty.
    """

    demand_label = confidence_label(demand)
    if sale is None:
        return _weaker(demand_label, "low")
    label = _atleast(confidence_label(sale), "low")
    return label if demand is None else _weaker(label, demand_label)


def _atleast(label: str, floor: str) -> str:
    return label if CONFIDENCE_ORDER.index(label) >= CONFIDENCE_ORDER.index(floor) else floor


@dataclass(frozen=True)
class Pathway:
    strategy: str
    section: str
    key: str  # stable identity, e.g. "crafting:123" (item ids and spell ids, never names)
    title: str  # display only
    steps: tuple[Step, ...]
    profit_per_unit: Fraction  # copper per unit sold/handled
    profit_per_craft: Fraction | None  # crafting only
    volume_per_hour: Fraction  # market units/h behind the pathway
    sale_probability: float | None  # P(sale within horizon) of crafting, when measured
    fill_rate: float | None  # flips only: shrunk per-trade unit fill within the horizon (not a P)
    evidence: Evidence | None  # the sale evidence (None: sale not measured)
    demand_evidence: Evidence | None
    listed_units: int | None  # units currently listed on the AH for the product
    capital: Fraction  # copper tied up for one batch / one execution of the steps
    profit_per_hour: Fraction | None  # expected, at ``share``, P-weighted when P is measured
    one_off_profit: Fraction | None  # expected profit of doing it once now (one_off section)
    sort_value: Fraction  # what the section is ordered by, see the module docstring
    confidence: str
    details: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        if self.section not in SECTIONS:
            raise ValueError(f"unknown section {self.section!r}")
        if not self.steps:
            raise ValueError("a pathway needs at least one step")
        if self.section in ("ranked", "unmeasured") and self.profit_per_hour is None:
            raise ValueError(f"{self.section} pathway {self.key} needs a profit per hour")
        if self.section == "one_off" and self.one_off_profit is None:
            raise ValueError(f"one-off pathway {self.key} needs a one-off profit")

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
    estimates: str = "forecast"  # validated demand/sale price; snapshot makes sale_price current lowest
    min_profit: int = 0  # copper, per craft (crafting) or per unit (the rest)
    min_volume: Fraction = Fraction(0)  # market units per hour
    share: Fraction = Fraction(1, 10)  # capture share of the observed market flow
    horizon_hours: float = 2.0
    min_sale_prob: float = 0.5  # crafting rows below it are excluded, not shown
    lookback_hours: float = 24.0  # crafting / demand window
    batch_crafts: int = 10
    min_independent_starts: int = 4
    crafting_price: str = "lowest"  # headline basis: min(sale_price - 1, rank target) | "target"
    listing_hours: int = 12  # one listing duration for every evaluator's deposit
    max_cold_age_hours: float = 3.0  # cold-tier rows older than this are excluded as stale
    # flip (defaults won on the beta calibration split)
    flip_x: Fraction = Fraction(7, 10)
    flip_undercut: Fraction = Fraction(12, 100)
    flip_lookback_hours: float = 72.0  # evidence window of replayed flips
    flip_min_trades: int = 10  # pooled independent past flips
    flip_unsold_value: str = "cost"  # "cost" (carry the leftovers) | "vendor" (dump them)

    def __post_init__(self) -> None:
        unknown = sorted(self.strategies - set(STRATEGIES))
        if unknown:
            raise OptionError(f"unknown strategies {unknown}; known: {list(STRATEGIES)}")
        if not self.strategies:
            raise OptionError("at least one strategy is required")
        if not self.professions:
            raise OptionError("at least one profession is required")
        if self.estimates not in ("snapshot", "forecast"):
            raise OptionError("estimates must be 'snapshot' or 'forecast'")
        if not 0 < self.share <= 1:
            raise OptionError("share must be in (0, 1]")
        if round(self.horizon_hours * SECONDS_PER_HOUR) not in CHECKPOINTS:
            raise OptionError(
                f"horizon_hours must be one of {[c / SECONDS_PER_HOUR for c in CHECKPOINTS]} "
                "(the replay checkpoints)"
            )
        if self.min_profit < 0 or self.min_volume < 0:
            raise OptionError("min_profit and min_volume must be non-negative")
        if not 0 <= self.min_sale_prob <= 1:
            raise OptionError("min_sale_prob must be in [0, 1]")
        if self.crafting_price not in ("lowest", "target"):
            raise OptionError("crafting_price must be 'lowest' or 'target'")
        if self.flip_unsold_value not in ("cost", "vendor"):
            raise OptionError("flip_unsold_value must be 'cost' or 'vendor'")
        if not 0 < self.flip_x <= 1 or not 0 <= self.flip_undercut < 1:
            raise OptionError("flip_x must be in (0, 1] and flip_undercut in [0, 1)")
        if self.flip_min_trades < 1 or self.min_independent_starts < 1 or self.batch_crafts < 1:
            raise OptionError("flip_min_trades, min_independent_starts and batch_crafts must be positive")
        if self.lookback_hours <= 0 or self.flip_lookback_hours <= 0 or self.max_cold_age_hours <= 0:
            raise OptionError("lookback hours and max_cold_age_hours must be positive")
        if self.listing_hours <= 0:
            raise OptionError("listing_hours must be positive")

    @property
    def horizon_seconds(self) -> int:
        return round(self.horizon_hours * SECONDS_PER_HOUR)


@dataclass(frozen=True)
class Advice:
    """The whole run: per-strategy results plus the filtered pathways ordered within sections."""

    house_key: str
    client_build: str
    as_of_t: float
    sweep_id: int | str
    config: AdviceConfig
    results: tuple[StrategyResult, ...]  # after the min-profit / min-volume filters
    pathways: tuple[Pathway, ...]  # grouped by section order, each by descending sort_value
    forecast: ForecastSummary | None = None  # None for snapshot mode

    def section(self, name: str) -> tuple[Pathway, ...]:
        return tuple(pathway for pathway in self.pathways if pathway.section == name)

    def exclusion_counts(self) -> dict[str, dict[str, int]]:
        return {result.strategy: result.exclusion_counts() for result in self.results}
