"""Market discovery and resolution checking via the Gamma Markets API."""

import json
import time
import logging
from typing import Optional

import requests

from core.defaults import GAMMA_API_BASE
from core.models import MarketMetadata

logger = logging.getLogger(__name__)

# Module-level session, set by collector to enable API logging.
_session: Optional[requests.Session] = None


def set_session(session: requests.Session):
    """Set the shared session used for all market discovery requests."""
    global _session
    _session = session


def _get_session() -> requests.Session:
    """Return the configured session, or a plain one as fallback."""
    return _session or requests.Session()


def compute_window_timestamp(window_duration: int, utc_time: Optional[float] = None) -> int:
    """Compute the window start unix timestamp for a given UTC time."""
    t = utc_time or time.time()
    return (int(t) // window_duration) * window_duration


def get_event_slug(slug_prefix: str, window_start_ts: int) -> str:
    """Build the event slug from a prefix and window start timestamp."""
    return f"{slug_prefix}{window_start_ts}"


def fetch_market_metadata(slug_prefix: str, window_start_ts: int) -> Optional[MarketMetadata]:
    """Fetch the market metadata for a specific window.

    Returns None if the event doesn't exist yet.
    """
    slug = get_event_slug(slug_prefix, window_start_ts)
    resp = _get_session().get(
        f"{GAMMA_API_BASE}/events",
        params={"slug": slug},
        timeout=10,
    )
    resp.raise_for_status()
    events = resp.json()

    if not events:
        return None

    event = events[0]
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    if market.get("closed"):
        return None
    clob_token_ids = json.loads(market["clobTokenIds"])

    return MarketMetadata(
        event_id=event["id"],
        event_slug=event["slug"],
        event_title=event["title"],
        condition_id=market["conditionId"],
        up_token_id=clob_token_ids[0],
        down_token_id=clob_token_ids[1],
        window_start_utc=market.get("eventStartTime", market.get("startDate", "")),
        window_end_utc=market.get("endDate", ""),
        resolution_source=market.get("resolutionSource", ""),
    )


def fetch_resolution(slug_prefix: str, window_start_ts: int) -> Optional[str]:
    """Check if a market has resolved. Returns 'Up', 'Down', or None."""
    slug = get_event_slug(slug_prefix, window_start_ts)
    resp = _get_session().get(
        f"{GAMMA_API_BASE}/events",
        params={"slug": slug},
        timeout=10,
    )
    resp.raise_for_status()
    events = resp.json()

    if not events or not events[0].get("markets"):
        return None

    market = events[0]["markets"][0]
    if not market.get("closed"):
        return None

    outcome_prices = market.get("outcomePrices")
    if not outcome_prices:
        return None

    if isinstance(outcome_prices, str):
        prices = json.loads(outcome_prices)
    else:
        prices = outcome_prices

    if prices[0] == "1":
        return "Up"
    elif prices[1] == "1":
        return "Down"
    return None
