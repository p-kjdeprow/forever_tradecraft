"""The evaluator interface and the per-run context evaluators share."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Protocol

from crowley.advisor.estimates import EstimateProvider
from crowley.advisor.model import AdviceConfig, Exclusion, Pathway, StrategyResult
from crowley.advisor.state import MarketState


@dataclass
class Context:
    """Everything one advisor run hands to every evaluator.

    ``state`` is loaded once and read-only; ``estimates`` is the price/demand seam; ``memo``
    caches computations that more than one evaluator needs (the crafting analysis, for one).
    """

    state: MarketState
    estimates: EstimateProvider
    config: AdviceConfig
    memo: dict[str, Any] = field(default_factory=dict)

    def cached[T](self, key: str, build: Callable[[], T]) -> T:
        if key not in self.memo:
            self.memo[key] = build()
        return self.memo[key]


class Evaluator(Protocol):
    name: str

    def evaluate(self, ctx: Context) -> StrategyResult: ...


class Collector:
    """Accumulates one strategy's candidates so that nothing can be dropped silently.

    Each candidate is passed to exactly one of ``keep`` and ``exclude``; ``result`` builds the
    ``StrategyResult``, whose own invariant then re-checks the count.
    """

    def __init__(self, strategy: str) -> None:
        self.strategy = strategy
        self.pathways: list[Pathway] = []
        self.exclusions: list[Exclusion] = []
        self.considered = 0
        self.notes: list[str] = []

    def keep(self, pathway: Pathway) -> None:
        self.considered += 1
        self.pathways.append(pathway)

    def exclude(self, key: str, reason: str, detail: str = "") -> None:
        self.considered += 1
        self.exclusions.append(Exclusion(self.strategy, key, reason, detail))

    def result(self) -> StrategyResult:
        return StrategyResult(
            self.strategy, tuple(self.pathways), tuple(self.exclusions), self.considered, tuple(self.notes)
        )


def fraction_price(value: Fraction | int) -> int:
    """A copper price that must already be whole; anything else is a modelling error."""

    if isinstance(value, Fraction) and value.denominator != 1:
        raise ValueError(f"price {value} is not a whole copper amount")
    return int(value)
