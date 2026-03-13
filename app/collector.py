"""Standalone data collector daemon for a single market.

Runs as an independent process that survives app restarts. Each market gets
its own collector with its own RTDS WebSocket, price poller, and tick
broadcaster. Bot workers connect to the tick socket independently.

Signals:
  SIGTERM / SIGINT  — stop immediately (clean shutdown)
  SIGUSR1           — stop after the current session finishes

Usage:
    uv run python -m app.collector --slug-prefix btc-updown-5m- [--window 300]
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

from core.defaults import (
    BASE_DIR,
    POLL_INTERVAL_SECONDS,
    market_data_dir,
    market_sessions_dir,
    market_socket_path,
    collector_pid_path,
    collector_state_path,
)
from core.models import SessionData
from core.price_fetcher import PriceFetcher
from core.session_recorder import SessionRecorder
from core.api_logger import LoggingSession
from core.rtds_client import RTDSClient
from core import market_discovery
from core.tick_broadcast import TickBroadcaster
from core.session_cache import SessionStatsCache
from core.window_ops import wait_for_next_window, fetch_metadata_with_retry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _write_pid(pid_path, pid):
    # type: (str, int) -> None
    d = os.path.dirname(pid_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = pid_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(pid))
    os.replace(tmp, pid_path)


def _remove_pid(pid_path):
    # type: (str) -> None
    try:
        os.unlink(pid_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class Collector:
    """Single-market data collector that runs as a standalone daemon.

    Owns the data stream (RTDS WebSocket, HTTP price polling) and tick
    broadcaster. Writes session data to disk and broadcasts ticks over
    a Unix socket. Does NOT manage bot workers — that's the AppManager's job.
    """

    def __init__(
        self,
        slug_prefix,                                # type: str
        window_duration=None,                       # type: Optional[int]
        poll_interval=POLL_INTERVAL_SECONDS,        # type: float
        max_sessions=0,                             # type: int
        verbose_api_log=False,                      # type: bool
    ):
        self.slug_prefix = slug_prefix
        self.slug_key = slug_prefix.rstrip("-")
        if window_duration is None:
            from core.markets import get_market_config
            window_duration = get_market_config(slug_prefix).window_duration_seconds
        self.window_duration = window_duration
        self.poll_interval = poll_interval
        self.max_sessions = max_sessions

        # Own infrastructure (independent of AppManager)
        self.http_session = LoggingSession(verbose=verbose_api_log)
        self.rtds = RTDSClient(symbol="btc/usd")
        self.rtds.start()
        market_discovery.set_session(self.http_session)

        # Per-market infrastructure
        self.fetcher = PriceFetcher(session=self.http_session, rtds_client=self.rtds)
        self._sample_pool = ThreadPoolExecutor(max_workers=2)
        self.recorder = SessionRecorder(slug_prefix)

        # Socket + broadcaster
        self._socket_path = market_socket_path(self.slug_key)
        d = os.path.dirname(self._socket_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._broadcaster = TickBroadcaster(self._socket_path)

        # State
        self.running = True
        self._stop_after_session = False
        self.session_count = 0
        self._last_window_ts = None  # type: Optional[int]

        # Session stats cache (for broadcasting to bots)
        self._stats_cache = self._init_stats_cache()

        # Paths
        self._pid_path = collector_pid_path(self.slug_key)
        self._state_path = collector_state_path(self.slug_key)

    def _init_stats_cache(self):
        # type: () -> SessionStatsCache
        from simulator.engine import load_session_summaries
        sessions_dir = market_sessions_dir(self.slug_key)
        if os.path.isdir(sessions_dir):
            summaries = load_session_summaries(sessions_dir)
            cache = SessionStatsCache.from_summaries(summaries)
            if len(cache) > 0:
                logger.info("[%s] Stats cache: %d sessions loaded",
                            self.slug_key, len(cache))
            return cache
        return SessionStatsCache()

    # ----- Main loop -----

    def run(self):
        # type: () -> None
        """Main loop — data collection + tick broadcasting."""
        _write_pid(self._pid_path, os.getpid())
        self._broadcaster.start()

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGUSR1, self._handle_stop_after_session)

        completed = self.recorder.count_completed_sessions()
        logger.info(
            "[%s] Collector started (PID %d). %d sessions already collected.",
            self.slug_key, os.getpid(), completed,
        )
        self.recorder.append_log(
            "Collector started",
            slug_prefix=self.slug_prefix,
            existing_sessions=completed,
            pid=os.getpid(),
        )

        try:
            while self.running:
                try:
                    self._run_one_session()
                    self.session_count += 1
                    logger.info("[%s] Sessions completed: %d",
                                self.slug_key, self.session_count)
                    if self._stop_after_session:
                        logger.info("[%s] Stop-after-session — exiting.",
                                    self.slug_key)
                        break
                    if self.max_sessions > 0 and self.session_count >= self.max_sessions:
                        logger.info("[%s] Reached max sessions (%d). Stopping.",
                                    self.slug_key, self.max_sessions)
                        break
                except Exception as e:
                    if not self.running:
                        break
                    logger.error("[%s] Session error: %s", self.slug_key, e, exc_info=True)
                    self.recorder.append_log("Session error", error=str(e))
                    time.sleep(10)
        finally:
            self._broadcaster.stop()
            self._sample_pool.shutdown(wait=True)
            self.fetcher._pool.shutdown(wait=True)
            self.rtds.stop()
            self.http_session.close()
            _remove_pid(self._pid_path)
            self._remove_state_file()
            self.recorder.clear_live_session()
            logger.info("[%s] Collector stopped. %d sessions completed.",
                        self.slug_key, self.session_count)

    def _handle_shutdown(self, signum, frame):
        # type: (int, Any) -> None
        logger.info("[%s] Shutdown signal received (sig=%d).", self.slug_key, signum)
        self.running = False

    def _handle_stop_after_session(self, signum, frame):
        # type: (int, Any) -> None
        logger.info("[%s] Stop-after-session signal received.", self.slug_key)
        self._stop_after_session = True

    # ----- Collector state file -----

    def _write_collector_state(self, state="running"):
        # type: (str) -> None
        """Write collector state file for AppManager to read."""
        data = {
            "pid": os.getpid(),
            "slug_key": self.slug_key,
            "session_count": self.session_count,
            "tick_clients": self._broadcaster.client_count,
            "state": state,
            "stop_after_session": self._stop_after_session,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _remove_state_file(self):
        # type: () -> None
        try:
            os.unlink(self._state_path)
        except OSError:
            pass

    # ----- Single session -----

    def _run_one_session(self):
        # type: () -> None
        last_status_write = [0.0]

        def _on_waiting(next_dt):
            now = time.time()
            if now - last_status_write[0] >= 5.0:
                self.recorder.write_live_status(
                    "waiting",
                    next_window_utc=next_dt.isoformat(),
                )
                self._broadcaster.broadcast({
                    "type": "waiting",
                    "next_window_utc": next_dt.isoformat(),
                })
                self._write_collector_state("waiting")
                last_status_write[0] = now

        window_ts = wait_for_next_window(
            self.window_duration, lambda: self.running, on_waiting=_on_waiting,
            after_window_ts=self._last_window_ts,
        )
        if not self.running:
            return

        metadata = fetch_metadata_with_retry(self.slug_prefix, window_ts)
        if metadata is None:
            logger.warning("[%s] Could not find market for window %d. Skipping.",
                           self.slug_key, window_ts)
            return

        if self.recorder.session_exists(metadata.window_start_utc):
            logger.info("[%s] Session %s already exists. Skipping.",
                        self.slug_key, metadata.window_start_utc)
            return

        logger.info("[%s] COLLECTING: %s", self.slug_key, metadata.event_title)

        session = SessionData(
            metadata=metadata,
            initial_btc_price_usd=None,
            final_btc_price_usd=None,
            outcome=None,
            outcome_prices=None,
            collection_started_utc=datetime.now(timezone.utc).isoformat(),
        )

        self.recorder.clear_live_session()

        metadata_dict = asdict(metadata)
        # Inject prior session stats so bots get lookback data via
        # the broadcast — no disk reads needed in worker processes.
        ws = metadata.window_start_utc if hasattr(metadata, 'window_start_utc') else ""
        if ws and len(self._stats_cache) > 0:
            prior = self._stats_cache.prior(before=ws, n=min(200, len(self._stats_cache)))
            metadata_dict["_prior_session_summaries"] = [s.to_dict() for s in prior]
        self._broadcaster.broadcast({
            "type": "session_start",
            "metadata": metadata_dict,
            "window_ts": window_ts,
            "window_end_ts": window_ts + self.window_duration,
        })

        self._poll_and_broadcast(session, metadata, window_ts)

        if session.samples:
            session.final_btc_price_usd = session.samples[-1].btc_price_usd
        session.collection_ended_utc = datetime.now(timezone.utc).isoformat()

        if session.initial_btc_price_usd and session.final_btc_price_usd:
            session.outcome = (
                "Up" if session.final_btc_price_usd > session.initial_btc_price_usd
                else "Down"
            )
            session.outcome_prices = (
                ["1", "0"] if session.outcome == "Up" else ["0", "1"]
            )

        logger.info(
            "[%s] Window ended. %d samples. BTC: $%s -> $%s.",
            self.slug_key, len(session.samples),
            session.initial_btc_price_usd, session.final_btc_price_usd,
        )

        self.recorder.clear_live_session()
        filepath = self.recorder.save_session(session)

        # Update in-memory stats cache
        if session.outcome and session.initial_btc_price_usd and session.final_btc_price_usd:
            ws = metadata.window_start_utc if hasattr(metadata, 'window_start_utc') else ""
            self._stats_cache.add(
                window_start=ws,
                outcome=session.outcome,
                initial_btc=session.initial_btc_price_usd,
                final_btc=session.final_btc_price_usd,
                source_file=filepath or "",
            )

        logger.info(
            "[%s] Saved: %s | Outcome: %s | Samples: %d | Errors: %d",
            self.slug_key, filepath, session.outcome,
            len(session.samples), session.error_count,
        )
        self.recorder.append_log(
            "Session saved",
            filepath=filepath,
            outcome=session.outcome,
            samples=len(session.samples),
            errors=session.error_count,
        )

        self._broadcaster.broadcast({
            "type": "session_end",
            "outcome": session.outcome,
            "session_filepath": filepath,
            "window_ts": window_ts,
        })

        self._last_window_ts = window_ts

    # ----- Poll loop with broadcasting -----

    def _poll_and_broadcast(self, session, metadata, window_ts):
        # type: (SessionData, Any, int) -> None
        window_end_ts = window_ts + self.window_duration
        hard_deadline_ts = window_end_ts
        next_sample_time = time.time()
        pending = []
        last_live_write = 0.0
        tick_index = 0

        while self.running:
            now = time.time()

            # Hard cutoff — never run past deadline
            if now >= hard_deadline_ts:
                if pending:
                    logger.warning(
                        "[%s] Hard deadline reached with %d pending futures — cancelling.",
                        self.slug_key, len(pending),
                    )
                    for f in pending:
                        f.cancel()
                    pending = []
                break

            if now >= next_sample_time and next_sample_time < window_end_ts:
                pending.append(
                    self._sample_pool.submit(
                        self.fetcher.take_sample,
                        metadata.up_token_id,
                        metadata.down_token_id,
                        metadata.window_start_utc,
                    )
                )
                next_sample_time += self.poll_interval

            # Process futures strictly in submission order to keep
            # samples sorted by elapsed_seconds.  Only pop from the
            # front — if a later future finishes first we wait for the
            # earlier ones so timestamps never reverse.
            while pending and pending[0].done():
                f = pending.pop(0)
                try:
                    sample = f.result()
                    if session.initial_btc_price_usd is None:
                        session.initial_btc_price_usd = sample.btc_price_usd
                    session.samples.append(sample)

                    sample_dict = asdict(sample)
                    self._broadcaster.broadcast({
                        "type": "tick",
                        "sample": sample_dict,
                        "tick_index": tick_index,
                    })
                    tick_index += 1

                    logger.debug(
                        "[%s] Sample %d: BTC=$%s Up=%s Down=%s",
                        self.slug_key, len(session.samples),
                        sample.btc_price_usd,
                        sample.up_token_midpoint,
                        sample.down_token_midpoint,
                    )
                except Exception as e:
                    session.error_count += 1
                    if len(session.errors) < 500:
                        session.errors.append(
                            "{}: {}".format(datetime.now(timezone.utc).isoformat(), e)
                        )
                    logger.warning("[%s] Sample error: %s", self.slug_key, e)

            if now - last_live_write >= 2.0 and session.samples:
                self.recorder.write_live_session(session)
                self._write_collector_state("collecting")
                last_live_write = now

            if now >= window_end_ts and not pending:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Data Collector Daemon")
    parser.add_argument("--slug-prefix", required=True,
                        help='Market slug prefix, e.g. "btc-updown-5m-"')
    parser.add_argument("--window", type=int, default=None,
                        help="Window duration override in seconds (auto-detected from markets.py)")
    parser.add_argument("--max-sessions", type=int, default=0,
                        help="Max sessions to collect (0 = unlimited)")
    parser.add_argument("--verbose-api-log", action="store_true",
                        help="Log all API responses including high-volume hosts")

    args = parser.parse_args()

    slug_key = args.slug_prefix.rstrip("-")
    log_dir = market_data_dir(slug_key)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "collector.log")

    from core.logging_setup import setup_logging
    setup_logging("collector", log_file=log_file, include_stderr=False)

    collector = Collector(
        slug_prefix=args.slug_prefix,
        window_duration=args.window,
        max_sessions=args.max_sessions,
        verbose_api_log=args.verbose_api_log,
    )
    collector.run()


if __name__ == "__main__":
    main()
