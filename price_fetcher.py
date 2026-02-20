"""BTC spot price and Polymarket token price fetching."""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

from defaults import CLOB_API_BASE, COINBASE_BTC_URL, COINGECKO_BTC_URL
from models import PriceSample

logger = logging.getLogger(__name__)


class PriceFetcher:
    """Fetches BTC spot price and Polymarket token prices."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get_btc_price(self) -> Optional[float]:
        """Get current BTC/USD price. Coinbase primary, CoinGecko fallback."""
        try:
            resp = self.session.get(COINBASE_BTC_URL, timeout=5)
            resp.raise_for_status()
            return float(resp.json()["data"]["amount"])
        except Exception:
            pass

        try:
            resp = self.session.get(COINGECKO_BTC_URL, timeout=5)
            resp.raise_for_status()
            return float(resp.json()["bitcoin"]["usd"])
        except Exception:
            logger.warning("Failed to fetch BTC price from both sources")
            return None

    def get_token_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token from the CLOB API."""
        try:
            resp = self.session.get(
                f"{CLOB_API_BASE}/midpoint",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            mid = data.get("mid")
            if mid is not None:
                return float(mid)
            return None
        except Exception:
            return None

    def get_token_book_top(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get best bid and ask for a token. Returns (best_bid, best_ask)."""
        try:
            resp = self.session.get(
                f"{CLOB_API_BASE}/book",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            return best_bid, best_ask
        except Exception:
            return None, None

    def take_sample(
        self,
        up_token_id: str,
        down_token_id: str,
        window_start_utc: str,
    ) -> PriceSample:
        """Take a complete price sample (BTC + both tokens).

        Makes up to 5 HTTP requests:
        - 1x BTC spot price
        - 2x token midpoints (up, down)
        - 2x token order books (up, down) for bid/ask
        """
        now = datetime.now(timezone.utc)
        window_start_dt = datetime.fromisoformat(
            window_start_utc.replace("Z", "+00:00")
        )
        elapsed = (now - window_start_dt).total_seconds()

        btc_price = self.get_btc_price()
        up_mid = self.get_token_midpoint(up_token_id)
        down_mid = self.get_token_midpoint(down_token_id)
        up_bid, up_ask = self.get_token_book_top(up_token_id)
        down_bid, down_ask = self.get_token_book_top(down_token_id)

        return PriceSample(
            timestamp_utc=now.isoformat(),
            elapsed_seconds=round(elapsed, 2),
            btc_price_usd=btc_price,
            up_token_midpoint=up_mid,
            down_token_midpoint=down_mid,
            up_token_best_bid=up_bid,
            up_token_best_ask=up_ask,
            down_token_best_bid=down_bid,
            down_token_best_ask=down_ask,
        )

    def close(self):
        """Close the underlying HTTP session (no-op if session is shared)."""
        self.session.close()
