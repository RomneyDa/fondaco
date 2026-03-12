"""BTC spot price and Polymarket token price fetching.

Supports concurrent HTTP requests via a ThreadPoolExecutor so the 4 CLOB
calls per sample can run in parallel (~200ms instead of ~800ms).
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Tuple, TYPE_CHECKING

import requests

from core.defaults import API_TIMEOUT_SHORT, CLOB_API_BASE, COINBASE_BTC_URL, COINGECKO_BTC_URL
from core.models import PriceSample

if TYPE_CHECKING:
    from core.rtds_client import RTDSClient

logger = logging.getLogger(__name__)

FUTURE_TIMEOUT_S = 10


class PriceFetcher:
    """Fetches BTC spot price and Polymarket token prices.

    Uses a thread pool for parallel CLOB requests within each sample.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        rtds_client: "Optional[RTDSClient]" = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.rtds = rtds_client
        self._pool = ThreadPoolExecutor(max_workers=20)
        self._last_btc_price = None  # type: Optional[float]

    def get_btc_price(self) -> Optional[float]:
        """Get current BTC/USD price.

        Priority: RTDS Chainlink -> Coinbase -> CoinGecko.
        Chainlink is the actual resolution price source for Polymarket markets.
        """
        if self.rtds is not None:
            price = self.rtds.get_price()
            if price is not None:
                return price

        try:
            resp = self.session.get(COINBASE_BTC_URL, timeout=API_TIMEOUT_SHORT)
            resp.raise_for_status()
            return float(resp.json()["data"]["amount"])
        except (requests.RequestException, ValueError, KeyError):
            pass

        try:
            resp = self.session.get(COINGECKO_BTC_URL, timeout=API_TIMEOUT_SHORT)
            resp.raise_for_status()
            return float(resp.json()["bitcoin"]["usd"])
        except (requests.RequestException, ValueError, KeyError):
            logger.warning("Failed to fetch BTC price from all sources")
            return None

    def get_token_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token from the CLOB API."""
        try:
            resp = self.session.get(
                f"{CLOB_API_BASE}/midpoint",
                params={"token_id": token_id},
                timeout=API_TIMEOUT_SHORT,
            )
            resp.raise_for_status()
            data = resp.json()
            mid = data.get("mid")
            if mid is not None:
                return float(mid)
            return None
        except (requests.RequestException, ValueError, KeyError):
            return None

    def get_token_book_top(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get best bid and ask for a token. Returns (best_bid, best_ask)."""
        try:
            resp = self.session.get(
                f"{CLOB_API_BASE}/book",
                params={"token_id": token_id},
                timeout=API_TIMEOUT_SHORT,
            )
            resp.raise_for_status()
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = max(float(b["price"]) for b in bids) if bids else None
            best_ask = min(float(a["price"]) for a in asks) if asks else None
            return best_bid, best_ask
        except (requests.RequestException, ValueError, KeyError):
            return None, None

    def take_sample(
        self,
        up_token_id: str,
        down_token_id: str,
        window_start_utc: str,
    ) -> PriceSample:
        """Take a complete price sample.

        Fires 4 CLOB requests in parallel via the thread pool (~200ms
        instead of ~800ms sequential).  BTC price comes from the RTDS
        Chainlink WebSocket (zero-cost, no HTTP call).
        """
        now = datetime.now(timezone.utc)
        window_start_dt = datetime.fromisoformat(
            window_start_utc.replace("Z", "+00:00")
        )
        elapsed = (now - window_start_dt).total_seconds()

        btc_price = self.rtds.get_price() if self.rtds is not None else None
        if btc_price is not None:
            self._last_btc_price = btc_price
        else:
            # RTDS stale/disconnected — use last known price, then HTTP fallback
            btc_price = self._last_btc_price
            if btc_price is None:
                btc_price = self.get_btc_price()
                if btc_price is not None:
                    self._last_btc_price = btc_price

        # Fire all 4 CLOB requests in parallel
        f_up_mid = self._pool.submit(self.get_token_midpoint, up_token_id)
        f_down_mid = self._pool.submit(self.get_token_midpoint, down_token_id)
        f_up_book = self._pool.submit(self.get_token_book_top, up_token_id)
        f_down_book = self._pool.submit(self.get_token_book_top, down_token_id)

        up_mid = f_up_mid.result(timeout=FUTURE_TIMEOUT_S)
        down_mid = f_down_mid.result(timeout=FUTURE_TIMEOUT_S)
        up_bid, up_ask = f_up_book.result(timeout=FUTURE_TIMEOUT_S)
        down_bid, down_ask = f_down_book.result(timeout=FUTURE_TIMEOUT_S)

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
        """Close the thread pool and underlying HTTP session."""
        self._pool.shutdown(wait=True)
        self.session.close()
