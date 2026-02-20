"""Polymarket RTDS WebSocket client for real-time Chainlink crypto prices.

Runs a background thread that maintains a persistent WebSocket connection to
wss://ws-live-data.polymarket.com and tracks the latest Chainlink price for
a given symbol (default btc/usd).

The Chainlink feed is the actual resolution price source for Polymarket
up/down markets, so using it gives us the exact same price the market
resolves against.

Usage:
    client = RTDSClient(symbol="btc/usd")
    client.start()
    price = client.get_price()   # latest Chainlink BTC/USD or None
    client.stop()
"""

import json
import logging
import threading
import time
from typing import Optional

import websocket

logger = logging.getLogger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
RECONNECT_DELAY_S = 2.0
MAX_RECONNECT_DELAY_S = 30.0
STALE_THRESHOLD_S = 10.0
WATCHDOG_TIMEOUT_S = 30.0  # force reconnect if no ticks for this long


class RTDSClient:
    """Background WebSocket client for Polymarket RTDS Chainlink prices."""

    def __init__(self, symbol="btc/usd"):
        # type: (str) -> None
        self.symbol = symbol.lower()
        self._lock = threading.Lock()
        self._latest_price = None  # type: Optional[float]
        self._latest_ts = 0.0  # monotonic timestamp of last update
        self._running = False
        self._thread = None  # type: Optional[threading.Thread]
        self._ws = None  # type: Optional[websocket.WebSocketApp]
        self._tick_count = 0

    def start(self):
        # type: () -> None
        """Start the background WebSocket thread and watchdog."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        logger.info("RTDS client started for %s", self.symbol)

    def stop(self):
        # type: () -> None
        """Stop the background WebSocket thread."""
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("RTDS client stopped")

    def get_price(self, max_age_s=STALE_THRESHOLD_S):
        # type: (float) -> Optional[float]
        """Return the latest price if fresh enough, else None."""
        with self._lock:
            if self._latest_price is None:
                return None
            age = time.monotonic() - self._latest_ts
            if age > max_age_s:
                logger.debug("RTDS price stale (%.1fs old)", age)
                return None
            return self._latest_price

    @property
    def tick_count(self):
        # type: () -> int
        """Number of price ticks received since start."""
        return self._tick_count

    def _watchdog_loop(self):
        # type: () -> None
        """Kill the WebSocket if no ticks arrive within WATCHDOG_TIMEOUT_S."""
        while self._running:
            time.sleep(5)
            with self._lock:
                age = time.monotonic() - self._latest_ts if self._latest_ts > 0 else 0
            if age > WATCHDOG_TIMEOUT_S and self._ws is not None:
                logger.warning(
                    "RTDS watchdog: no ticks for %.0fs, forcing reconnect", age,
                )
                try:
                    self._ws.close()
                except Exception:
                    pass

    def _run_loop(self):
        # type: () -> None
        """Reconnection loop — runs in the background thread."""
        delay = RECONNECT_DELAY_S
        while self._running:
            ticks_before = self._tick_count
            try:
                self._connect()
            except Exception as e:
                logger.warning("RTDS connection error: %s", e)

            if not self._running:
                break

            # Reset backoff if we received ticks during this connection
            if self._tick_count > ticks_before:
                delay = RECONNECT_DELAY_S
            else:
                delay = min(delay * 1.5, MAX_RECONNECT_DELAY_S)

            logger.info("RTDS reconnecting in %.1fs...", delay)
            time.sleep(delay)

    def _connect(self):
        # type: () -> None
        """Create and run a single WebSocket connection."""

        def on_open(ws):
            # Subscribe to ALL chainlink symbols (no filter) — the server
            # closes the connection early when a symbol filter is used.
            # We filter client-side in on_message instead.
            subscribe_msg = json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "",
                }],
            })
            ws.send(subscribe_msg)
            logger.info("RTDS subscribed to Chainlink (all symbols)")

        def on_message(ws, message):
            if not message or not message.strip():
                return
            try:
                data = json.loads(message)
                if data.get("topic") != "crypto_prices_chainlink":
                    return
                payload = data.get("payload", {})

                # Handle single-tick messages
                if "symbol" in payload and "value" in payload:
                    self._handle_tick(payload)
                    return

                # Handle batch messages (array in payload.data)
                if "data" in payload:
                    for tick in payload["data"]:
                        self._handle_tick(tick)
            except Exception as e:
                logger.debug("RTDS message parse error: %s", e)

        def on_error(ws, error):
            logger.warning("RTDS WebSocket error: %s", error)

        def on_close(ws, close_status_code, close_msg):
            logger.info("RTDS WebSocket closed (code=%s)", close_status_code)

        self._ws = websocket.WebSocketApp(
            RTDS_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _handle_tick(self, tick):
        # type: (dict) -> None
        """Process a single price tick, filtering by symbol."""
        sym = tick.get("symbol", "").lower()
        if sym and sym != self.symbol:
            return
        try:
            price = float(tick["value"])
        except (KeyError, ValueError, TypeError):
            return
        with self._lock:
            self._latest_price = price
            self._latest_ts = time.monotonic()
        self._tick_count += 1
        if self._tick_count <= 3 or self._tick_count % 60 == 0:
            logger.debug(
                "RTDS %s tick #%d: $%.2f",
                self.symbol, self._tick_count, price,
            )
