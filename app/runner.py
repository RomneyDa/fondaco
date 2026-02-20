"""Unified Runner: always collects data, optionally trades.

Merges the Collector and TradingBot into a single process. Data collection
happens unconditionally; trading is toggled on/off via enable_trading() /
disable_trading(), taking effect at the next session boundary.
"""

import json
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.api_logger import LoggingSession
from core.defaults import (
    BALANCE_CACHE_FILE,
    BASE_DIR,
    CLOB_API_BASE,
    POLL_INTERVAL_SECONDS,
    RESOLUTION_POLL_INTERVAL,
    RESOLUTION_TIMEOUT_SECONDS,
    WINDOW_DURATION_SECONDS,
)
from core import market_discovery
from core.models import SessionData
from core.price_fetcher import PriceFetcher
from core.rtds_client import RTDSClient
from core.session_recorder import SessionRecorder
from core.strategies import ALL_STRATEGIES
from core.trader import ClobExecutor, Position, Trade, Trader
from core.window_ops import (
    wait_for_next_window,
    fetch_metadata_with_retry,
    wait_for_resolution,
)

logger = logging.getLogger(__name__)

# Paths
BOT_TRADES_FILE = os.path.join(BASE_DIR, "data", "bot_trades.jsonl")
BOT_SESSION_LOG = os.path.join(BASE_DIR, "data", "bot_log.jsonl")
POSITION_FILE = os.path.join(BASE_DIR, "data", "bot_position.json")


# ---------------------------------------------------------------------------
# Trade serialization for dashboard
# ---------------------------------------------------------------------------


def _trade_to_dashboard_dict(trade):
    # type: (Trade) -> Dict[str, Any]
    """Serialize a Trade to a compact dict for the dashboard."""
    return {
        "elapsed_seconds": trade.elapsed_seconds,
        "side": trade.side,
        "price": trade.price,
        "amount": round(trade.amount, 4),
        "tokens": round(trade.tokens, 4),
        "success": trade.success,
    }


# ---------------------------------------------------------------------------
# Balance cache helpers
# ---------------------------------------------------------------------------


def _save_balance_cache(cash, slug_prefix, session_count):
    # type: (float, str, int) -> None
    """Write current balance to .cache/balance.json for simulator use."""
    os.makedirs(os.path.dirname(BALANCE_CACHE_FILE), exist_ok=True)
    with open(BALANCE_CACHE_FILE, "w") as f:
        json.dump({
            "cash": round(cash, 4),
            "slug_prefix": slug_prefix,
            "session_count": session_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


def _load_balance_cache():
    # type: () -> Optional[Dict[str, Any]]
    """Load cached balance from .cache/balance.json."""
    try:
        with open(BALANCE_CACHE_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Position persistence
# ---------------------------------------------------------------------------


def _save_position(position):
    # type: (Position) -> None
    """Atomically write position to disk (write tmp, then os.replace)."""
    tmp = POSITION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(position.to_dict(), f)
    os.replace(tmp, POSITION_FILE)


def _load_position(starting_cash):
    # type: (float) -> Position
    """Load position from disk if available, else return fresh Position."""
    try:
        with open(POSITION_FILE, "r") as f:
            return Position.from_dict(json.load(f))
    except (OSError, json.JSONDecodeError, KeyError):
        return Position(cash=starting_cash)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_AUTH_ERROR_STRINGS = ("Unauthorized", "Invalid api key", "invalid signature")


class Runner:
    """Unified collect + trade loop.

    Always collects data. Trading is toggled via enable_trading() /
    disable_trading(), taking effect at the next session boundary.
    """

    def __init__(
        self,
        slug_prefix,                                    # type: str
        window_duration=WINDOW_DURATION_SECONDS,        # type: int
        poll_interval=POLL_INTERVAL_SECONDS,            # type: float
        resolution_poll_interval=RESOLUTION_POLL_INTERVAL,  # type: float
        resolution_timeout=RESOLUTION_TIMEOUT_SECONDS,  # type: int
        strategy_name=None,                             # type: Optional[str]
        strategy_params=None,                           # type: Optional[Dict[str, Any]]
        starting_cash=100.0,                            # type: float
        dry_run=False,                                  # type: bool
        max_sessions=0,                                 # type: int
    ):
        self.slug_prefix = slug_prefix
        self.window_duration = window_duration
        self.poll_interval = poll_interval
        self.resolution_poll_interval = resolution_poll_interval
        self.resolution_timeout = resolution_timeout
        self.max_sessions = max_sessions  # 0 = unlimited

        # Shared infrastructure (always on)
        self.http_session = LoggingSession()
        self.rtds = RTDSClient(symbol="btc/usd")
        self.rtds.start()
        self.fetcher = PriceFetcher(session=self.http_session, rtds_client=self.rtds)
        self._sample_pool = ThreadPoolExecutor(max_workers=8)
        market_discovery.set_session(self.http_session)
        self.recorder = SessionRecorder(slug_prefix)

        # State
        self.running = True
        self.session_count = 0
        self._consecutive_auth_failures = 0

        # Trading state (protected by lock, snapshotted at session start)
        self._lock = threading.Lock()
        self._trading_enabled = False
        self._strategy = None       # type: Optional[Any]
        self._strategy_name = None  # type: Optional[str]
        self._executor = None       # type: Optional[Any]
        self._position = None       # type: Optional[Position]
        self._starting_cash = starting_cash
        self._dry_run = dry_run
        self._private_key = None    # type: Optional[str]
        self._safe_address = None   # type: Optional[str]

        # If strategy given at init, enable trading immediately
        if strategy_name is not None:
            self.enable_trading(
                strategy_name,
                strategy_params or {},
                cash=starting_cash,
                dry_run=dry_run,
            )

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received. Finishing current session...")
        self.running = False

    def request_stop(self):
        # type: () -> None
        """Request graceful shutdown (from HTTP server)."""
        logger.info("Stop requested via control server.")
        self.running = False

    # ----- Trading toggle -----

    def enable_trading(self, strategy_name, params=None, cash=None, dry_run=False):
        # type: (str, Optional[Dict[str, Any]], Optional[float], bool) -> None
        """Enable trading with the given strategy. Takes effect next session."""
        if strategy_name not in ALL_STRATEGIES:
            raise ValueError("Unknown strategy '{}'. Available: {}".format(
                strategy_name, list(ALL_STRATEGIES.keys()),
            ))

        parsed_params = {}  # type: Dict[str, Any]
        if params:
            for k, v in params.items():
                # Parse string values to numbers if possible
                if isinstance(v, str):
                    try:
                        v = int(v)
                    except ValueError:
                        try:
                            v = float(v)
                        except ValueError:
                            pass
                parsed_params[k] = v

        strategy = ALL_STRATEGIES[strategy_name](**parsed_params)

        with self._lock:
            self._strategy = strategy
            self._strategy_name = strategy_name
            self._dry_run = dry_run

            if cash is not None:
                self._starting_cash = float(cash)

            # Init position: dry runs always start fresh, live loads from disk
            if self._position is None:
                if dry_run:
                    self._position = Position(cash=self._starting_cash)
                else:
                    self._position = _load_position(self._starting_cash)

            # Init executor (lazy — deferred until we know dry_run)
            if dry_run:
                from core.trader import SimExecutor
                self._executor = SimExecutor(execution_mode="midpoint_slippage", slippage=0.02)
                logger.info("DRY RUN mode — no real orders will be placed.")
            else:
                self._init_clob_executor()

            self._trading_enabled = True

        logger.info(
            "Trading enabled: strategy=%s params=%s cash=$%.2f dry_run=%s",
            strategy_name, parsed_params, self._starting_cash, dry_run,
        )

    def _init_clob_executor(self):
        """Initialize CLOB executor with auth. Caller must hold self._lock."""
        from dotenv import load_dotenv
        load_dotenv()

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in .env")

        from app.auth import get_auth, build_clob_client
        auth = get_auth(private_key)
        client = build_clob_client(private_key, auth)

        self._private_key = private_key
        self._safe_address = auth["safe_address"]
        self._executor = ClobExecutor(client, trade_log_file=BOT_TRADES_FILE)

    def disable_trading(self):
        # type: () -> None
        """Disable trading. Takes effect next session."""
        with self._lock:
            self._trading_enabled = False
        logger.info("Trading disabled (takes effect next session).")

    def _snapshot_trading_state(self):
        # type: () -> tuple
        """Snapshot trading state under lock. Returns (enabled, strategy, executor, position)."""
        with self._lock:
            return (
                self._trading_enabled,
                self._strategy,
                self._executor,
                self._position,
            )

    def get_status(self):
        # type: () -> Dict[str, Any]
        """Get current runner status (for HTTP API)."""
        with self._lock:
            status = {
                "running": self.running,
                "slug_prefix": self.slug_prefix,
                "session_count": self.session_count,
                "trading_enabled": self._trading_enabled,
                "strategy": self._strategy_name,
                "dry_run": self._dry_run,
                "cash": round(self._position.cash, 4) if self._position else None,
            }
        return status

    # ----- Main loop -----

    def run(self):
        # type: () -> None
        """Main loop — runs indefinitely until stopped."""
        completed = self.recorder.count_completed_sessions()
        logger.info(
            "Runner started for %r. %d sessions already collected.",
            self.slug_prefix, completed,
        )
        self.recorder.append_log(
            "Runner started",
            slug_prefix=self.slug_prefix,
            existing_sessions=completed,
        )

        try:
            while self.running:
                try:
                    self._run_one_session()
                    self.session_count += 1
                    logger.info("Sessions completed: %d", self.session_count)
                    if self.max_sessions > 0 and self.session_count >= self.max_sessions:
                        logger.info("Reached max sessions (%d). Stopping.", self.max_sessions)
                        break
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    logger.error("Session error: %s", e, exc_info=True)
                    self.recorder.append_log("Session error", error=str(e))
                    # Check for auth errors in trading mode
                    if self._trading_enabled and self._is_auth_error(e):
                        self._handle_auth_failure()
                    time.sleep(10)
        finally:
            self._sample_pool.shutdown(wait=False)
            self.rtds.stop()
            self.http_session.close()
            logger.info("Runner stopped. %d sessions completed.", self.session_count)

    def _is_auth_error(self, error):
        # type: (Exception) -> bool
        msg = str(error)
        return any(s in msg for s in _AUTH_ERROR_STRINGS)

    def _handle_auth_failure(self):
        # type: () -> None
        """Handle auth failure: refresh or auto-disable after 3 consecutive."""
        self._consecutive_auth_failures += 1
        if self._consecutive_auth_failures >= 3:
            logger.critical(
                "%d consecutive auth failures. Auto-disabling trading (collection continues).",
                self._consecutive_auth_failures,
            )
            self.disable_trading()
            return
        try:
            self._refresh_auth()
        except Exception as e:
            logger.error("Auth refresh failed: %s", e)

    def _refresh_auth(self):
        # type: () -> None
        """Clear cached auth, re-derive, and rebuild the CLOB executor."""
        from app.auth import _clear_auth_cache, get_auth, build_clob_client

        logger.info("Refreshing auth credentials...")
        _clear_auth_cache()
        auth = get_auth(self._private_key, force_refresh=True)
        client = build_clob_client(self._private_key, auth)
        with self._lock:
            self._safe_address = auth["safe_address"]
            self._executor = ClobExecutor(client, trade_log_file=BOT_TRADES_FILE)
        self._consecutive_auth_failures = 0
        logger.info("Auth refreshed successfully.")

    # ----- Single session -----

    def _run_one_session(self):
        # type: () -> None
        """Run one session: always collects, optionally trades."""

        # 1. Snapshot trading state (determines this session's mode)
        trading_enabled, strategy, executor, position = self._snapshot_trading_state()
        mode = "trading" if trading_enabled and strategy and executor else "collect-only"

        # 2. Wait for next window
        last_status_write = [0.0]

        def _on_waiting(next_dt):
            now = time.time()
            if now - last_status_write[0] >= 5.0:
                trading_info = None
                if mode == "trading":
                    trading_info = {
                        "enabled": True,
                        "strategy": self._strategy_name,
                        "cash": round(position.cash, 4) if position else None,
                    }
                self.recorder.write_live_status(
                    "waiting",
                    next_window_utc=next_dt.isoformat(),
                    trading_info=trading_info,
                )
                last_status_write[0] = now

        window_ts = wait_for_next_window(
            self.window_duration, lambda: self.running, on_waiting=_on_waiting,
        )
        if not self.running:
            return

        # 3. Fetch market metadata
        metadata = fetch_metadata_with_retry(self.slug_prefix, window_ts)
        if metadata is None:
            logger.warning("Could not find market for window %d. Skipping.", window_ts)
            return

        # Check if already collected
        if self.recorder.session_exists(metadata.window_start_utc):
            logger.info("Session %s already exists. Skipping.", metadata.window_start_utc)
            return

        logger.info("%s: %s", mode.upper(), metadata.event_title)

        # 4. Create SessionData (always)
        initial_btc_price = self.fetcher.get_btc_price()
        session = SessionData(
            metadata=metadata,
            initial_btc_price_usd=initial_btc_price,
            final_btc_price_usd=None,
            outcome=None,
            outcome_prices=None,
            collection_started_utc=datetime.now(timezone.utc).isoformat(),
        )

        # 5. Poll loop — branch on mode
        trade_dicts = []  # type: List[Dict[str, Any]]
        if mode == "trading":
            trade_count, trade_dicts = self._poll_with_trading(
                session, metadata, window_ts, strategy, executor, position,
                initial_btc_price,
            )
        else:
            self._poll_collect_only(session, metadata, window_ts)
            trade_count = 0

        # 6. Record final BTC price and determine outcome from price comparison
        session.final_btc_price_usd = self.fetcher.get_btc_price()
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
            "Window ended. %d samples. BTC: $%s -> $%s.",
            len(session.samples),
            session.initial_btc_price_usd,
            session.final_btc_price_usd,
        )

        # 7. Save session data (always)
        self.recorder.clear_live_session()
        filepath = self.recorder.save_session(session)
        logger.info(
            "Saved: %s | Outcome: %s | Samples: %d | Errors: %d",
            filepath, session.outcome, len(session.samples), session.error_count,
        )
        self.recorder.append_log(
            "Session saved",
            filepath=filepath,
            outcome=session.outcome,
            samples=len(session.samples),
            errors=session.error_count,
            mode=mode,
        )

        # 8. If trading: resolve, log, claim, write sidecar
        if mode == "trading" and position is not None:
            self._resolve_trading_session(
                session, metadata, window_ts, strategy, executor, position,
                initial_btc_price, trade_count, trade_dicts, filepath,
            )

    # ----- Collect-only poll loop -----

    def _poll_collect_only(self, session, metadata, window_ts):
        # type: (SessionData, Any, int) -> None
        """Multi-sample parallel poll loop (collector's approach, ~18 ticks/sec)."""
        window_end_ts = window_ts + self.window_duration
        next_sample_time = time.time()
        pending = []
        last_live_write = 0.0

        while self.running:
            now = time.time()

            # Submit new sample if it's time
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

            # Collect completed samples
            still_pending = []
            for f in pending:
                if f.done():
                    try:
                        sample = f.result()
                        session.samples.append(sample)
                        logger.debug(
                            "Sample %d: BTC=$%s Up=%s Down=%s",
                            len(session.samples),
                            sample.btc_price_usd,
                            sample.up_token_midpoint,
                            sample.down_token_midpoint,
                        )
                    except Exception as e:
                        session.error_count += 1
                        session.errors.append(
                            "{}: {}".format(datetime.now(timezone.utc).isoformat(), e)
                        )
                        logger.warning("Sample error: %s", e)
                else:
                    still_pending.append(f)
            pending = still_pending

            # Write live session periodically
            if now - last_live_write >= 0.5 and session.samples:
                self.recorder.write_live_session(session)
                last_live_write = now

            # Exit once window ended and all in-flight samples collected
            if now >= window_end_ts and not pending:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)

    # ----- Trading poll loop -----

    def _poll_with_trading(self, session, metadata, window_ts, strategy, executor,
                           position, initial_btc_price):
        # type: (SessionData, Any, int, Any, Any, Position, Optional[float]) -> tuple
        """Single-sample sequential poll loop with strategy tick processing.

        Returns (trade_count, trade_dicts) — trade_dicts is a list of dicts
        for the dashboard sidecar file.
        """
        metadata_dict = asdict(metadata)

        # Daily loss tracking: reset reference balance each new UTC day
        today = datetime.now(timezone.utc).date()
        if not hasattr(self, '_day_start_cash') or self._current_day != today:
            self._day_start_cash = position.cash
            self._current_day = today

        trader = Trader(strategy, executor)
        trader.start_session(
            metadata_dict, position, initial_btc_price or 0.0, self.window_duration,
            reference_balance=self._day_start_cash,
        )
        trade_count = 0
        trade_dicts = []  # type: List[Dict[str, Any]]

        window_end_ts = window_ts + self.window_duration
        next_sample_time = time.time()
        pending_future = None
        last_live_write = 0.0

        while self.running:
            now = time.time()

            # Submit next sample if it's time and window hasn't ended
            if pending_future is None and now >= next_sample_time and now < window_end_ts:
                pending_future = self._sample_pool.submit(
                    self.fetcher.take_sample,
                    metadata.up_token_id,
                    metadata.down_token_id,
                    metadata.window_start_utc,
                )
                next_sample_time += self.poll_interval

            # Process completed sample
            if pending_future is not None and pending_future.done():
                try:
                    sample = pending_future.result()
                    sample_dict = asdict(sample)

                    # Record sample for data collection
                    session.samples.append(sample)

                    # Process tick for trading
                    trade = trader.process_tick(sample_dict)

                    if trade is not None:
                        if trade.success is not False:
                            trade_count += 1
                            trade_dicts.append(_trade_to_dashboard_dict(trade))
                            if trade.success is True and not self._dry_run:
                                _save_position(position)
                            logger.info(
                                "Trade executed: %s @ %.2f x %.2f | order_id=%s",
                                trade.side, trade.price, trade.tokens,
                                trade.order_id,
                            )
                        else:
                            logger.warning("Trade failed: %s", trade.error)

                    logger.debug(
                        "Tick %d: BTC=$%s Up=%s Down=%s",
                        trader._tick_index,
                        sample.btc_price_usd,
                        sample.up_token_midpoint,
                        sample.down_token_midpoint,
                    )
                except Exception as e:
                    session.error_count += 1
                    session.errors.append(
                        "{}: {}".format(datetime.now(timezone.utc).isoformat(), e)
                    )
                    logger.warning("Sample error: %s", e)
                pending_future = None

                # Immediately submit next if behind schedule and window still open
                now = time.time()
                if now >= next_sample_time and now < window_end_ts:
                    pending_future = self._sample_pool.submit(
                        self.fetcher.take_sample,
                        metadata.up_token_id,
                        metadata.down_token_id,
                        metadata.window_start_utc,
                    )
                    next_sample_time += self.poll_interval

            # Write live session periodically (with trading info)
            now = time.time()
            if now - last_live_write >= 0.5 and session.samples:
                trading_info = {
                    "enabled": True,
                    "strategy": self._strategy_name,
                    "dry_run": self._dry_run,
                    "cash": round(position.cash, 4),
                    "trades": list(trade_dicts),
                }
                self.recorder.write_live_session(session, trading_info=trading_info)
                last_live_write = now

            # Exit when window ended and no pending sample
            if time.time() >= window_end_ts and pending_future is None:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)

        return trade_count, trade_dicts

    # ----- Trading session resolution -----

    def _resolve_trading_session(self, session, metadata, window_ts, strategy,
                                 executor, position, initial_btc_price, trade_count,
                                 trade_dicts=None, session_filepath=None):
        # type: (SessionData, Any, int, Any, Any, Position, Optional[float], int, Optional[List[Dict[str, Any]]], Optional[str]) -> None
        """Wait for on-chain resolution, update position, log, and claim."""

        logger.info("Waiting for resolution...")
        self.recorder.write_live_status(
            "resolving",
            trading_info={"strategy": self._strategy_name, "trades": trade_count},
        )

        outcome = wait_for_resolution(
            self.slug_prefix, window_ts,
            self.resolution_timeout, self.resolution_poll_interval,
            lambda: self.running,
        )

        if outcome is None:
            # Fallback: BTC price comparison
            final_btc = session.final_btc_price_usd or 0.0
            init_btc = initial_btc_price or 0.0
            if init_btc > 0 and final_btc > 0:
                outcome = "Up" if final_btc > init_btc else "Down"
                logger.warning(
                    "Resolution timeout — using BTC price fallback: %s (%.2f -> %.2f)",
                    outcome, init_btc, final_btc,
                )
            else:
                logger.error(
                    "RESOLUTION FAILED: No on-chain resolution and BTC price "
                    "unavailable. Zeroing out tokens as lost."
                )

        # Force-resolve phantom tokens if outcome is still None
        if outcome is None and (position.up_tokens > 0 or position.down_tokens > 0):
            lost = position.up_tokens + position.down_tokens
            logger.error("Clearing %.4f phantom tokens (no resolution)", lost)
            position.up_tokens = 0.0
            position.down_tokens = 0.0

        # Create a Trader just for resolution (uses same strategy/executor)
        trader = Trader(strategy, executor)
        trader._position = position
        trader.end_session(outcome)
        if not self._dry_run:
            _save_position(position)

        # Cache balance for simulator use
        try:
            _save_balance_cache(position.cash, self.slug_prefix, self.session_count + 1)
        except Exception as e:
            logger.warning("Failed to save balance cache: %s", e)

        # Log session summary
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "slug_prefix": self.slug_prefix,
            "window_ts": window_ts,
            "strategy": self._strategy_name,
            "outcome": outcome,
            "trades": trade_count,
            "cash": round(position.cash, 4),
            "session_number": self.session_count + 1,
        }
        try:
            with open(BOT_SESSION_LOG, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception as e:
            logger.warning("Failed to log session summary: %s", e)

        logger.info(
            "Session resolved: %s | Trades: %d | Cash: $%.2f",
            outcome, trade_count, position.cash,
        )

        # Write trading sidecar file alongside the session
        if session_filepath and trade_dicts is not None:
            starting_cash = self._starting_cash
            sidecar = {
                "strategy": self._strategy_name,
                "params": {k: v for k, v in (self._strategy.__dict__ if self._strategy else {}).items()
                           if not k.startswith("_")},
                "dry_run": self._dry_run,
                "starting_cash": round(starting_cash, 4),
                "final_cash": round(position.cash, 4),
                "pnl": round(position.cash - starting_cash, 4),
                "outcome": outcome,
                "trades": trade_dicts,
            }
            sidecar_path = session_filepath + ".trading.json"
            try:
                with open(sidecar_path, "w") as f:
                    json.dump(sidecar, f, indent=2)
                logger.info("Trading sidecar saved: %s", sidecar_path)
            except Exception as e:
                logger.warning("Failed to save trading sidecar: %s", e)

        # Auto-claim resolved positions in background
        if trade_count > 0 and self._private_key and self._safe_address:
            from app.claim import has_builder_creds, claim_background
            if has_builder_creds():
                try:
                    claim_background(self._private_key, self._safe_address)
                except Exception as e:
                    logger.debug("Skipping background claim: %s", e)
