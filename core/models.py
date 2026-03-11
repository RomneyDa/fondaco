"""Data classes for the Polymarket BTC 5-minute data collector."""

from dataclasses import dataclass, field, asdict
from typing import List, Optional
import json


@dataclass
class PriceSample:
    """A single polling sample taken during a session."""
    timestamp_utc: str
    elapsed_seconds: float
    btc_price_usd: Optional[float]
    up_token_midpoint: Optional[float]
    down_token_midpoint: Optional[float]
    up_token_best_bid: Optional[float]
    up_token_best_ask: Optional[float]
    down_token_best_bid: Optional[float]
    down_token_best_ask: Optional[float]


class Sample:
    """Lightweight sample with __slots__ for fast attribute access in backtesting.

    Drop-in replacement for sample dicts — supports both attribute access
    (sample.btc_price_usd) and dict-style access (sample["key"], sample.get("key")).
    Uses __slots__ to avoid per-instance __dict__, saving memory and speeding up
    attribute lookups vs dict hash lookups.
    """
    __slots__ = (
        'timestamp_utc', 'elapsed_seconds', 'btc_price_usd',
        'up_token_midpoint', 'down_token_midpoint',
        'up_token_best_bid', 'up_token_best_ask',
        'down_token_best_bid', 'down_token_best_ask',
    )

    def __init__(self, d):
        # type: (dict) -> None
        self.timestamp_utc = d.get('timestamp_utc')
        self.elapsed_seconds = d.get('elapsed_seconds', 0.0)
        self.btc_price_usd = d.get('btc_price_usd')
        self.up_token_midpoint = d.get('up_token_midpoint')
        self.down_token_midpoint = d.get('down_token_midpoint')
        self.up_token_best_bid = d.get('up_token_best_bid')
        self.up_token_best_ask = d.get('up_token_best_ask')
        self.down_token_best_bid = d.get('down_token_best_bid')
        self.down_token_best_ask = d.get('down_token_best_ask')

    def get(self, key, default=None):
        # type: (str, object) -> object
        """Dict-style .get() for backward compatibility."""
        return getattr(self, key, default)

    def __getitem__(self, key):
        # type: (str) -> object
        """Dict-style ['key'] access for backward compatibility."""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)


@dataclass
class MarketMetadata:
    """Static info about a single 5-minute market."""
    event_id: str
    event_slug: str
    event_title: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_start_utc: str
    window_end_utc: str
    resolution_source: str


@dataclass
class SessionData:
    """Complete data for one 5-minute session."""
    metadata: MarketMetadata
    initial_btc_price_usd: Optional[float]
    final_btc_price_usd: Optional[float]
    outcome: Optional[str]
    outcome_prices: Optional[List[str]]
    samples: List[PriceSample] = field(default_factory=list)
    collection_started_utc: str = ""
    collection_ended_utc: str = ""
    error_count: int = 0
    errors: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)
