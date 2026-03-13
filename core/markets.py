"""Market-specific constants for known Polymarket up/down markets.

Global constants (API URLs, price precision, fee rates, etc.) belong in
core/defaults.py. Market-specific constants (window duration, gap thresholds)
belong here.

Each market entry defines timing and detection parameters that vary by market
type. Use get_market_config(slug_prefix) to look up constants for a given
market, falling back to sensible defaults for unknown slugs.
"""

from typing import Dict, Optional


class MarketConfig:
    """Timing and detection constants for a specific market type."""

    __slots__ = (
        "slug_prefix",
        "window_duration_seconds",
        "max_session_gap_seconds",
    )

    def __init__(
        self,
        slug_prefix,            # type: str
        window_duration_seconds,  # type: int
        max_session_gap_seconds=None,  # type: Optional[int]
    ):
        self.slug_prefix = slug_prefix
        self.window_duration_seconds = window_duration_seconds
        # Default: 2x window duration (one fully missed session + jitter)
        self.max_session_gap_seconds = (
            max_session_gap_seconds
            if max_session_gap_seconds is not None
            else window_duration_seconds * 2
        )


# ── Known markets ────────────────────────────────────────────────────────

KNOWN_MARKETS = {
    "btc-updown-5m-": MarketConfig(
        slug_prefix="btc-updown-5m-",
        window_duration_seconds=300,
    ),
    "btc-updown-15m-": MarketConfig(
        slug_prefix="btc-updown-15m-",
        window_duration_seconds=900,
    ),
}  # type: Dict[str, MarketConfig]


def get_market_config(slug_prefix):
    # type: (str) -> MarketConfig
    """Look up market config by slug prefix.

    Raises KeyError if the market is not registered in KNOWN_MARKETS.
    Add new markets to KNOWN_MARKETS before using them.
    """
    return KNOWN_MARKETS[slug_prefix]
