"""Central AppManager: manages multiple MarketRunners.

One process, one port. Each market runs as a thread with its own
data stream and worker processes. Shared resources (HTTP session,
RTDS client) are owned here and passed to runners.
"""

import json
import logging
import os
import signal
import threading
import time
from typing import Any, Dict, List, Optional

from core.api_logger import LoggingSession
from core.defaults import (
    APP_STATE_FILE,
    DEFAULT_CONTROL_PORT,
    MARKETS_DIR,
    WINDOW_DURATION_SECONDS,
)
from core import market_discovery
from core.rtds_client import RTDSClient

from app.market_runner import MarketRunner

logger = logging.getLogger(__name__)


class AppManager:
    """Central manager for multiple markets.

    Owns shared resources and delegates per-market work to MarketRunner
    instances running on threads.
    """

    def __init__(self, port=DEFAULT_CONTROL_PORT):
        # type: (int) -> None
        self.port = port
        self.running = True

        # Shared infrastructure
        self.http_session = LoggingSession()
        self.rtds = RTDSClient(symbol="btc/usd")
        self.rtds.start()
        market_discovery.set_session(self.http_session)

        # Markets (protected by lock)
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._markets = {}  # type: Dict[str, MarketRunner]
        self._threads = {}  # type: Dict[str, threading.Thread]

    def start_market(self, slug_prefix, window_duration=WINDOW_DURATION_SECONDS,
                     market_type="updown", max_sessions=0):
        # type: (str, int, str, int) -> MarketRunner
        """Start data collection for a market. Returns the runner."""
        slug_key = slug_prefix.rstrip("-")

        with self._lock:
            if slug_key in self._markets:
                raise ValueError("Market '{}' is already running.".format(slug_key))

        runner = MarketRunner(
            slug_prefix=slug_prefix,
            http_session=self.http_session,
            rtds_client=self.rtds,
            market_type=market_type,
            window_duration=window_duration,
            max_sessions=max_sessions,
        )

        thread = threading.Thread(
            target=self._run_market,
            args=(slug_key, runner),
            daemon=True,
            name="market-{}".format(slug_key),
        )

        with self._lock:
            self._markets[slug_key] = runner
            self._threads[slug_key] = thread

        thread.start()
        logger.info("Started market: %s (type=%s, window=%ds)",
                     slug_key, market_type, window_duration)
        self._save_state()
        return runner

    def _run_market(self, slug_key, runner):
        # type: (str, MarketRunner) -> None
        """Thread target — runs the market runner and cleans up on exit."""
        try:
            runner.run()
        except Exception:
            logger.exception("Market %s crashed", slug_key)
        finally:
            with self._lock:
                self._markets.pop(slug_key, None)
                self._threads.pop(slug_key, None)
            self._save_state()
            logger.info("Market %s stopped and cleaned up.", slug_key)

    def stop_market(self, slug_prefix, after_session=False):
        # type: (str, bool) -> None
        """Stop a running market (kills bots + data stream)."""
        slug_key = slug_prefix.rstrip("-")
        with self._lock:
            runner = self._markets.get(slug_key)
            thread = self._threads.get(slug_key)
        if runner is None:
            raise ValueError("Market '{}' is not running.".format(slug_key))

        if after_session:
            runner.request_stop_after_session()
        else:
            runner.request_stop()

        # Wait for thread to finish (with timeout)
        if thread is not None:
            thread.join(timeout=30)
            if thread.is_alive():
                logger.warning("Market %s thread did not exit within 30s.", slug_key)

    def get_market(self, slug_prefix):
        # type: (str) -> MarketRunner
        """Get a running market runner. Raises ValueError if not found."""
        slug_key = slug_prefix.rstrip("-")
        with self._lock:
            runner = self._markets.get(slug_key)
        if runner is None:
            raise ValueError("Market '{}' is not running.".format(slug_key))
        return runner

    def get_status(self):
        # type: () -> Dict[str, Any]
        """Aggregate status from all markets."""
        with self._lock:
            market_statuses = []
            for slug_key, runner in self._markets.items():
                try:
                    market_statuses.append(runner.get_status())
                except Exception as e:
                    market_statuses.append({
                        "slug_key": slug_key,
                        "error": str(e),
                    })

        return {
            "running": self.running,
            "markets": market_statuses,
        }

    def request_stop(self):
        # type: () -> None
        """Stop everything — all markets and their workers."""
        logger.info("App stop requested.")
        self.running = False
        with self._lock:
            runners = list(self._markets.values())
        for runner in runners:
            runner.request_stop()

    def request_stop_after_session(self):
        # type: () -> None
        """Stop all markets after their current sessions finish."""
        logger.info("App kill requested — stopping all markets after session.")
        with self._lock:
            runners = list(self._markets.values())
        for runner in runners:
            runner.request_stop_after_session()

    def run(self):
        # type: () -> None
        """Block on main thread until stopped. Handles signals."""
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        try:
            while self.running:
                time.sleep(1)
                # Check if all markets exited
                with self._lock:
                    if not self._markets and not self.running:
                        break
        finally:
            self._shutdown()

    def _handle_shutdown(self, signum, frame):
        # type: (int, Any) -> None
        logger.info("Shutdown signal received.")
        self.request_stop()

    def _shutdown(self):
        # type: () -> None
        """Clean shutdown — stop all markets, wait for threads, close shared resources."""
        with self._lock:
            runners = list(self._markets.values())
            threads = list(self._threads.values())

        for runner in runners:
            runner.request_stop()

        for thread in threads:
            thread.join(timeout=30)

        self.rtds.stop()
        self.http_session.close()
        logger.info("AppManager shutdown complete.")

    def _save_state(self):
        # type: () -> None
        """Write app.json with current state."""
        with self._lock:
            market_configs = []
            for slug_key, runner in self._markets.items():
                market_configs.append({
                    "slug_prefix": runner.slug_prefix,
                    "slug_key": slug_key,
                    "market_type": runner.market_type,
                    "window_duration": runner.window_duration,
                })

        state = {
            "pid": os.getpid(),
            "port": self.port,
            "markets": market_configs,
        }
        with self._save_lock:
            try:
                os.makedirs(os.path.dirname(APP_STATE_FILE), exist_ok=True)
                tmp = APP_STATE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, APP_STATE_FILE)
            except Exception:
                logger.exception("Failed to save app state")

    def restore(self):
        # type: () -> None
        """Restore markets and strategies from app.json."""
        if not os.path.exists(APP_STATE_FILE):
            logger.info("No app state file — nothing to restore.")
            return
        try:
            with open(APP_STATE_FILE, "r") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read app state: %s", e)
            return

        markets = state.get("markets", [])
        if not markets:
            logger.info("App state has no markets — nothing to restore.")
            return

        for mc in markets:
            slug_prefix = mc.get("slug_prefix")
            if not slug_prefix:
                continue
            try:
                runner = self.start_market(
                    slug_prefix=slug_prefix,
                    window_duration=mc.get("window_duration", WINDOW_DURATION_SECONDS),
                    market_type=mc.get("market_type", "updown"),
                )
                # Give the runner a moment to start before restoring strategies
                time.sleep(0.5)
                runner.restore_strategies()
                logger.info("Restored market: %s", slug_prefix)
            except Exception:
                logger.exception("Failed to restore market '%s'", slug_prefix)
