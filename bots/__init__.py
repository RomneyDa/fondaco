"""Bot strategy registries, organized by market type.

Shared base class and utilities live in core/strategies/.
Concrete strategy implementations live here, grouped by market type.
"""

from typing import Dict, Type

from bots.updown import STRATEGIES as UPDOWN_STRATEGIES

# Per-market-type strategy registries
MARKET_STRATEGIES = {
    "updown": UPDOWN_STRATEGIES,
}  # type: Dict[str, Dict[str, type]]

# Combined registry (all market types)
ALL_STRATEGIES = dict(UPDOWN_STRATEGIES)  # type: Dict[str, type]


def get_strategies_for_market(market_type):
    # type: (str) -> Dict[str, type]
    """Return the strategy registry for a given market type."""
    return MARKET_STRATEGIES.get(market_type, {})
