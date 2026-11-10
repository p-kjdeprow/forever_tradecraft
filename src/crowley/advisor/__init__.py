"""On-demand trading advisor: ranks pathways across strategy types from the stored scan history.

``load_market_state`` reads one house and build once; ``advise`` runs the evaluators over it.
"""

from crowley.advisor.engine import advise
from crowley.advisor.model import Advice, AdviceConfig, Evidence, Exclusion, Pathway, Step, StrategyResult
from crowley.advisor.state import MarketState, load_market_state

__all__ = [
    "Advice",
    "AdviceConfig",
    "Evidence",
    "Exclusion",
    "MarketState",
    "Pathway",
    "Step",
    "StrategyResult",
    "advise",
    "load_market_state",
]
