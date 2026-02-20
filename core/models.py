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
