#!/usr/bin/env python3
"""
Polymarket Market Data Collector

Continuously discovers and polls Polymarket up/down markets,
recording price data for each session.

Usage:
    python collector.py btc-updown-5m-
    python collector.py btc-updown-1m- --sessions 50 --window 60 --poll 1.0
"""

import argparse
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from api_logger import LoggingSession
from defaults import (
    COLLECTOR_LOG_FILE,
    POLL_INTERVAL_SECONDS,
    RESOLUTION_POLL_INTERVAL,
    RESOLUTION_TIMEOUT_SECONDS,
    TARGET_SESSIONS,
    WINDOW_DURATION_SECONDS,
)
import market_discovery
from market_discovery import compute_window_timestamp, fetch_market_metadata, fetch_resolution
from models import SessionData
from price_fetcher import PriceFetcher
from session_recorder import SessionRecorder

import os

os.makedirs(os.path.dirname(COLLECTOR_LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(COLLECTOR_LOG_FILE),
    ],
)
# Console stays at INFO; file gets everything (DEBUG)
logging.getLogger().handlers[0].setLevel(logging.INFO)
logger = logging.getLogger(__name__)


class Collector:
    def __init__(
        self,
        slug_prefix: str,
        target_sessions: int,
        window_duration: int,
        poll_interval: float,
        resolution_poll_interval: float,
        resolution_timeout: int,
    ):
        self.slug_prefix = slug_prefix
        self.target_sessions = target_sessions
        self.window_duration = window_duration
        self.poll_interval = poll_interval
        self.resolution_poll_interval = resolution_poll_interval
        self.resolution_timeout = resolution_timeout
        self.http_session = LoggingSession()
        self.fetcher = PriceFetcher(session=self.http_session)
        market_discovery.set_session(self.http_session)
        self.recorder = SessionRecorder(slug_prefix)
        self.running = True

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received. Finishing current session...")
        self.running = False

    def run(self):
        """Main collection loop."""
        completed = self.recorder.count_completed_sessions()
        logger.info(
            f"Starting collector for {self.slug_prefix!r}. "
            f"{completed} sessions already collected. Target: {self.target_sessions}"
        )
        self.recorder.append_log(
            "Collector started",
            slug_prefix=self.slug_prefix,
            existing_sessions=completed,
            target=self.target_sessions,
        )

        while self.running and completed < self.target_sessions:
            try:
                completed += self._collect_one_session()
                logger.info(f"Progress: {completed}/{self.target_sessions} sessions")
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Session collection error: {e}", exc_info=True)
                self.recorder.append_log("Session error", error=str(e))
                time.sleep(10)

        logger.info(f"Collection complete. {completed} sessions total.")
        self.recorder.append_log("Collector stopped", total_sessions=completed)
        self.http_session.close()

    def _collect_one_session(self) -> int:
        """Collect data for one session. Returns 1 on success, 0 on skip."""

        # PHASE 1: Wait for the next window to start
        window_ts = self._wait_for_next_window()
        if not self.running:
            return 0

        # PHASE 2: Fetch market metadata
        metadata = self._fetch_metadata_with_retry(window_ts)
        if metadata is None:
            logger.warning(f"Could not find market for window {window_ts}. Skipping.")
            return 0

        # Check if already collected
        if self.recorder.session_exists(metadata.window_start_utc):
            logger.info(f"Session {metadata.window_start_utc} already exists. Skipping.")
            return 0

        logger.info(f"Collecting: {metadata.event_title}")

        # PHASE 3: Create session and take initial BTC price
        session = SessionData(
            metadata=metadata,
            initial_btc_price_usd=self.fetcher.get_btc_price(),
            final_btc_price_usd=None,
            outcome=None,
            outcome_prices=None,
            collection_started_utc=datetime.now(timezone.utc).isoformat(),
        )

        # PHASE 4: Poll prices throughout the window
        window_end_ts = window_ts + self.window_duration
        next_sample_time = time.time()

        while next_sample_time < window_end_ts and self.running:
            try:
                sample = self.fetcher.take_sample(
                    metadata.up_token_id,
                    metadata.down_token_id,
                    metadata.window_start_utc,
                )
                session.samples.append(sample)
                logger.debug(
                    f"Sample {len(session.samples)}: "
                    f"BTC=${sample.btc_price_usd} "
                    f"Up={sample.up_token_midpoint} "
                    f"Down={sample.down_token_midpoint}"
                )
                self.recorder.write_live_session(session)
            except Exception as e:
                session.error_count += 1
                session.errors.append(f"{datetime.now(timezone.utc).isoformat()}: {e}")
                logger.warning(f"Sample error: {e}")

            # Schedule next sample on an absolute clock so HTTP latency
            # doesn't eat into the interval.
            next_sample_time += self.poll_interval
            sleep_for = min(next_sample_time, window_end_ts) - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)

        # PHASE 5: Record final BTC price
        session.final_btc_price_usd = self.fetcher.get_btc_price()
        session.collection_ended_utc = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"Window ended. {len(session.samples)} samples collected. "
            f"BTC: ${session.initial_btc_price_usd} -> ${session.final_btc_price_usd}. "
            f"Waiting for resolution..."
        )

        # PHASE 6: Wait for resolution
        self.recorder.write_live_status("resolving")
        session.outcome = self._wait_for_resolution(window_ts)
        if session.outcome:
            session.outcome_prices = (
                ["1", "0"] if session.outcome == "Up" else ["0", "1"]
            )

        # PHASE 7: Save to disk and clear live file
        self.recorder.clear_live_session()
        filepath = self.recorder.save_session(session)
        logger.info(
            f"Saved: {filepath} | Outcome: {session.outcome} | "
            f"Samples: {len(session.samples)} | Errors: {session.error_count}"
        )
        self.recorder.append_log(
            "Session saved",
            filepath=filepath,
            outcome=session.outcome,
            samples=len(session.samples),
            errors=session.error_count,
        )

        return 1

    def _wait_for_next_window(self) -> int:
        """Wait until the start of the next window. Returns window timestamp."""
        now = time.time()
        wd = self.window_duration
        current_window_start = (int(now) // wd) * wd
        next_window_start = current_window_start + wd

        # If we're in the first 3 seconds of the current window, use it
        if now - current_window_start < 3:
            return current_window_start

        # Otherwise wait for the next window
        wait_seconds = next_window_start - now
        next_dt = datetime.fromtimestamp(next_window_start, tz=timezone.utc)
        logger.info(f"Waiting {wait_seconds:.0f}s for next window at {next_dt.isoformat()}")

        last_status_write = 0.0
        while time.time() < next_window_start and self.running:
            now = time.time()
            if now - last_status_write >= 5.0:
                self.recorder.write_live_status("waiting", next_window_utc=next_dt.isoformat())
                last_status_write = now
            time.sleep(min(1.0, next_window_start - time.time()))

        return next_window_start

    def _fetch_metadata_with_retry(self, window_ts: int, max_retries: int = 5):
        """Fetch metadata with retries (market may not be created yet)."""
        for attempt in range(max_retries):
            try:
                metadata = fetch_market_metadata(self.slug_prefix, window_ts)
                if metadata:
                    return metadata
            except Exception as e:
                logger.warning(f"Metadata fetch attempt {attempt + 1} failed: {e}")
            time.sleep(2)
        return None

    def _wait_for_resolution(self, window_ts: int) -> Optional[str]:
        """Poll for market resolution after the window ends."""
        deadline = time.time() + self.resolution_timeout

        while time.time() < deadline and self.running:
            try:
                outcome = fetch_resolution(self.slug_prefix, window_ts)
                if outcome:
                    return outcome
            except Exception as e:
                logger.warning(f"Resolution check error: {e}")
            time.sleep(self.resolution_poll_interval)

        logger.warning(f"Resolution timeout for window {window_ts}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Collect Polymarket market data for a given slug prefix."
    )
    parser.add_argument(
        "slug_prefix",
        help='Market slug prefix, e.g. "btc-updown-5m-" or "btc-updown-1m-"',
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=TARGET_SESSIONS,
        help=f"Number of sessions to collect (default: {TARGET_SESSIONS})",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=WINDOW_DURATION_SECONDS,
        help=f"Window duration in seconds (default: {WINDOW_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--resolution-poll",
        type=float,
        default=RESOLUTION_POLL_INTERVAL,
        help=f"Resolution poll interval in seconds (default: {RESOLUTION_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--resolution-timeout",
        type=int,
        default=RESOLUTION_TIMEOUT_SECONDS,
        help=f"Resolution timeout in seconds (default: {RESOLUTION_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args()

    collector = Collector(
        slug_prefix=args.slug_prefix,
        target_sessions=args.sessions,
        window_duration=args.window,
        poll_interval=args.poll,
        resolution_poll_interval=args.resolution_poll,
        resolution_timeout=args.resolution_timeout,
    )
    collector.run()


if __name__ == "__main__":
    main()
