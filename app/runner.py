"""Unified Runner: always collects data, optionally trades.

Merges the Collector and TradingBot into a single process. Data collection
happens unconditionally; trading is toggled on/off via enable_trading() /
disable_trading(), taking effect at the next session boundary.

Supports multiple concurrent strategies, each with its own Position and
Trader. All strategies share the same price data and ClobExecutor.
"""

import json
import logging
import os
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.api_logger import LoggingSession
from core.defaults import (
    BALANCE_CACHE_FILE,
    BASE_DIR,
    POLL_INTERVAL_SECONDS,
    RESOLUTION_POLL_INTERVAL,
    RESOLUTION_TIMEOUT_SECONDS,
    STRATEGIES_CONFIG_FILE,
    WINDOW_DURATION_SECONDS,
)
from core import market_discovery
from core.models import SessionData
from core.price_fetcher import PriceFetcher
from core.rtds_client import RTDSClient
from core.session_recorder import SessionRecorder
from core.strategies import ALL_STRATEGIES
from core.trader import ClobExecutor, Position, SimExecutor, Trade, Trader
from core.window_ops import (
    wait_for_next_window,
    fetch_metadata_with_retry,
    wait_for_resolution,
)

logger = logging.getLogger(__name__)

# Paths
BOT_TRADES_FILE = os.path.join(BASE_DIR, "data", "bot_trades.jsonl")
BOT_SESSION_LOG = os.path.join(BASE_DIR, "data", "bot_log.jsonl")
POSITION_DIR = os.path.join(BASE_DIR, "data", "bot_positions")


# ---------------------------------------------------------------------------
# Strategy slot
# ---------------------------------------------------------------------------


@dataclass
class StrategySlot:
    """One independently-trading strategy with its own Position."""
    name: str                           # strategy class name (unique key)
    strategy: Any                       # Strategy instance
    params: Dict[str, Any]              # parsed params dict
    position: Position
    starting_cash: float
    dry_run: bool
    executor: Any                       # Executor (ClobExecutor or SimExecutor)
    cash_explicit: bool = False         # True when cash was explicitly set
    per_session_cash: Optional[float] = None  # if set, reset cash to this at each session start
    session_cap: Optional[float] = None       # if set, max dollars deployed per session (rest held in reserve)
    _session_reserve: float = 0.0             # cash held back from session (internal)


# ---------------------------------------------------------------------------
# Trade serialization for dashboard
# ---------------------------------------------------------------------------


def _trade_to_dashboard_dict(trade):
    # type: (Trade) -> Dict[str, Any]
    """Serialize a Trade to a compact dict for the dashboard."""
    return {
        "tick_index": trade.tick_index,
        "elapsed_seconds": trade.elapsed_seconds,
        "side": trade.side,
        "price": trade.price,
        "amount": round(trade.amount, 4),
        "tokens": round(trade.tokens, 4),
        "success": trade.success,
        "order_id": trade.order_id,
        "error": trade.error,
        "timestamp": trade.timestamp,
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
# Position persistence (per-strategy)
# ---------------------------------------------------------------------------


def _save_slot_position(slot_name, position):
    # type: (str, Position) -> None
    """Atomically write position to disk for a named strategy slot."""
    os.makedirs(POSITION_DIR, exist_ok=True)
    filepath = os.path.join(POSITION_DIR, "{}.json".format(slot_name))
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(position.to_dict(), f)
    os.replace(tmp, filepath)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_AUTH_ERROR_STRINGS = ("Unauthorized", "Invalid api key", "invalid signature")


class Runner:
    """Unified collect + trade loop.

    Always collects data. Trading is toggled via enable_trading() /
    disable_trading(), taking effect at the next session boundary.
    Supports multiple concurrent strategies via StrategySlot list.
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
        starting_cash=None,                             # type: Optional[float]
        dry_run=False,                                  # type: bool
        max_sessions=0,                                 # type: int
        per_session=False,                              # type: bool
        session_cap=None,                               # type: Optional[float]
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
        self._stop_after_session = False
        self.session_count = 0
        self._consecutive_auth_failures = 0

        # Background resolution (so we don't block the next window)
        self._resolution_thread = None   # type: Optional[threading.Thread]
        self._credit_queue = queue.Queue()  # type: queue.Queue

        # Trading state (protected by lock, snapshotted at session start)
        self._lock = threading.Lock()
        self._slots = []  # type: List[StrategySlot]
        self._clob_executor = None  # type: Optional[ClobExecutor]
        self._private_key = None    # type: Optional[str]
        self._safe_address = None   # type: Optional[str]

        # If strategy given at init, enable trading immediately
        if strategy_name is not None:
            self.enable_trading(
                strategy_name,
                strategy_params or {},
                cash=starting_cash,
                dry_run=dry_run,
                per_session=per_session,
                session_cap=session_cap,
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

    def request_stop_after_session(self):
        # type: () -> None
        """Request shutdown after the current session finishes."""
        logger.info("Kill requested — will stop after current session.")
        self._stop_after_session = True

    # ----- Trading toggle -----

    def enable_trading(self, strategy_name, params=None, cash=None, dry_run=False,
                       per_session=False, session_cap=None):
        # type: (str, Optional[Dict[str, Any]], Optional[float], bool, bool, Optional[float]) -> None
        """Add or update a strategy slot. Takes effect next session.

        If per_session=True, cash is reset to the starting amount at the
        beginning of each session (independent budget per session).
        If session_cap is set, each session deploys at most that many dollars
        while the rest stays in reserve (bankroll grows with wins).
        """
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
            # Determine executor for this slot
            if dry_run:
                executor = SimExecutor(execution_mode="book")
                logger.info("[%s] DRY RUN mode — no real orders will be placed.", strategy_name)
            else:
                # Ensure shared ClobExecutor exists
                if self._clob_executor is None:
                    self._init_clob_executor()
                executor = self._clob_executor

                # Fetch real balance for cash validation
                try:
                    real_balance = self._fetch_clob_balance()
                except Exception as e:
                    if cash is None:
                        logger.error("Failed to fetch CLOB balance: %s", e)
                        raise RuntimeError(
                            "Cannot start live trading without balance. "
                            "Provide --cash or fix CLOB connection."
                        )
                    real_balance = None

                # Auto-assign cash from balance when not explicitly given
                if cash is None:
                    if real_balance is not None:
                        cash = real_balance
                    else:
                        raise RuntimeError("Cannot determine cash — balance unavailable")

            starting_cash = float(cash) if cash is not None else 100.0
            cash_explicit = cash is not None

            # Remove existing slot with same name (upsert)
            self._slots = [s for s in self._slots if s.name != strategy_name]

            slot = StrategySlot(
                name=strategy_name,
                strategy=strategy,
                params=parsed_params,
                position=Position(cash=starting_cash),
                starting_cash=starting_cash,
                dry_run=dry_run,
                executor=executor,
                cash_explicit=cash_explicit,
                per_session_cash=starting_cash if per_session else None,
                session_cap=session_cap,
            )
            self._slots.append(slot)

        logger.info(
            "Trading enabled: strategy=%s params=%s cash=$%.2f dry_run=%s per_session=%s session_cap=%s",
            strategy_name, parsed_params, starting_cash, dry_run, per_session, session_cap,
        )
        self._save_strategies_config()

    def _init_clob_executor(self):
        """Initialize shared CLOB executor with auth. Caller must hold self._lock."""
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
        self._clob_executor = ClobExecutor(client, trade_log_file=BOT_TRADES_FILE)

    def _fetch_clob_balance(self):
        # type: () -> float
        """Fetch actual USDC balance from Polymarket CLOB API."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        resp = self._clob_executor.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # Balance is returned in micro-USDC (6 decimals)
        balance = float(resp.get("balance", 0)) / 1_000_000
        logger.info("Fetched CLOB balance: $%.4f", balance)
        return balance

    def set_cash(self, cash, strategy_name=None):
        # type: (float, Optional[str]) -> None
        """Update position cash for a strategy slot (takes effect immediately)."""
        with self._lock:
            if not self._slots:
                raise RuntimeError("Trading not enabled — no position to update")
            if strategy_name is not None:
                slot = self._find_slot(strategy_name)
                if slot is None:
                    raise ValueError("No active slot for strategy '{}'".format(strategy_name))
                slots_to_update = [slot]
            elif len(self._slots) == 1:
                slots_to_update = [self._slots[0]]
            else:
                raise ValueError(
                    "Multiple strategies active — specify which one: {}".format(
                        [s.name for s in self._slots]
                    )
                )
            for slot in slots_to_update:
                old = slot.position.cash
                slot.position.cash = cash
                slot.starting_cash = cash
                slot.cash_explicit = True
                logger.info("[%s] Cash updated: $%.4f -> $%.4f", slot.name, old, cash)

    def disable_trading(self, strategy_name=None):
        # type: (Optional[str],) -> None
        """Disable trading. If strategy_name given, remove only that slot.
        Otherwise clear all slots. Takes effect next session."""
        with self._lock:
            if strategy_name is not None:
                before = len(self._slots)
                self._slots = [s for s in self._slots if s.name != strategy_name]
                if len(self._slots) == before:
                    raise ValueError("No active slot for strategy '{}'".format(strategy_name))
                logger.info("Trading disabled for %s (takes effect next session).", strategy_name)
            else:
                self._slots = []
                logger.info("Trading disabled for all strategies (takes effect next session).")
            self._save_strategies_config()

    def _save_strategies_config(self):
        # type: () -> None
        """Persist current strategy slot configs to disk for --restore."""
        configs = []
        for slot in self._slots:
            configs.append({
                "name": slot.name,
                "params": slot.params,
                "cash": slot.starting_cash,
                "dry_run": slot.dry_run,
                "per_session": slot.per_session_cash is not None,
                "session_cap": slot.session_cap,
            })
        try:
            os.makedirs(os.path.dirname(STRATEGIES_CONFIG_FILE), exist_ok=True)
            tmp = STRATEGIES_CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(configs, f, indent=2)
            os.replace(tmp, STRATEGIES_CONFIG_FILE)
        except Exception:
            logger.exception("Failed to save strategies config")

    def restore_strategies(self):
        # type: () -> None
        """Restore strategy configs from data/strategies.json."""
        if not os.path.exists(STRATEGIES_CONFIG_FILE):
            logger.info("No strategies config file found at %s — nothing to restore.", STRATEGIES_CONFIG_FILE)
            return
        try:
            with open(STRATEGIES_CONFIG_FILE, "r") as f:
                configs = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read strategies config: %s", e)
            return
        if not configs:
            logger.info("Strategies config is empty — nothing to restore.")
            return
        for entry in configs:
            name = entry.get("name")
            if not name:
                continue
            try:
                self.enable_trading(
                    strategy_name=name,
                    params=entry.get("params", {}),
                    cash=entry.get("cash"),
                    dry_run=entry.get("dry_run", False),
                    per_session=entry.get("per_session", False),
                    session_cap=entry.get("session_cap"),
                )
                logger.info("Restored strategy: %s", name)
            except Exception:
                logger.exception("Failed to restore strategy '%s'", name)

    def _find_slot(self, name):
        # type: (str) -> Optional[StrategySlot]
        """Find a slot by strategy name. Caller must hold self._lock."""
        for s in self._slots:
            if s.name == name:
                return s
        return None

    def _snapshot_slots(self):
        # type: () -> List[StrategySlot]
        """Snapshot the current slots list under lock."""
        with self._lock:
            return list(self._slots)

    def _join_pending_resolution(self, timeout=120):
        # type: (int) -> None
        """Wait for any pending background resolution to complete."""
        if self._resolution_thread is not None and self._resolution_thread.is_alive():
            logger.info("Waiting for previous resolution to complete...")
            self._resolution_thread.join(timeout=timeout)
            if self._resolution_thread.is_alive():
                logger.warning(
                    "Previous resolution still running after %ds timeout.", timeout,
                )
            else:
                self._resolution_thread = None

    def _drain_resolution_credits(self):
        # type: () -> int
        """Apply any pending resolution credits to slot positions.

        Returns number of credits applied.
        """
        applied = 0
        while True:
            try:
                slot_name, credit = self._credit_queue.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                slot = self._find_slot(slot_name)
                if slot:
                    old = slot.position.cash
                    slot.position.cash += credit
                    if not slot.dry_run:
                        _save_slot_position(slot_name, slot.position)
                    logger.info(
                        "[%s] Resolution credit applied: $%.4f -> $%.4f (+$%.4f)",
                        slot_name, old, slot.position.cash, credit,
                    )
                    applied += 1
                else:
                    logger.warning(
                        "Resolution credit for removed slot %r: $%.4f (discarded)",
                        slot_name, credit,
                    )
        return applied

    def get_status(self):
        # type: () -> Dict[str, Any]
        """Get current runner status (for HTTP API)."""
        with self._lock:
            strategies = []
            for slot in self._slots:
                info = {
                    "name": slot.name,
                    "params": slot.params,
                    "dry_run": slot.dry_run,
                    "cash": round(slot.position.cash, 4),
                    "starting_cash": round(slot.starting_cash, 4),
                }
                if slot.per_session_cash is not None:
                    info["per_session_cash"] = round(slot.per_session_cash, 4)
                if slot.session_cap is not None:
                    info["session_cap"] = round(slot.session_cap, 4)
                strategies.append(info)

            # Backward-compat: top-level fields from first slot
            first = self._slots[0] if self._slots else None
            status = {
                "running": self.running,
                "stop_after_session": self._stop_after_session,
                "slug_prefix": self.slug_prefix,
                "session_count": self.session_count,
                "trading_enabled": len(self._slots) > 0,
                "strategy": first.name if first else None,
                "dry_run": first.dry_run if first else False,
                "cash": round(first.position.cash, 4) if first else None,
                "strategies": strategies,
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
                    if self._stop_after_session:
                        logger.info("Kill signal received — stopping after session %d.", self.session_count)
                        break
                    if self.max_sessions > 0 and self.session_count >= self.max_sessions:
                        logger.info("Reached max sessions (%d). Stopping.", self.max_sessions)
                        break
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    logger.error("Session error: %s", e, exc_info=True)
                    self.recorder.append_log("Session error", error=str(e))
                    # Check for auth errors in trading mode
                    if self._slots and self._is_auth_error(e):
                        self._handle_auth_failure()
                    time.sleep(10)
        finally:
            # Wait for pending resolution before shutting down
            self._join_pending_resolution(timeout=60)
            self._drain_resolution_credits()
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
        """Clear cached auth, re-derive, and rebuild the shared CLOB executor."""
        from app.auth import _clear_auth_cache, get_auth, build_clob_client

        logger.info("Refreshing auth credentials...")
        _clear_auth_cache()
        auth = get_auth(self._private_key, force_refresh=True)
        client = build_clob_client(self._private_key, auth)
        with self._lock:
            self._safe_address = auth["safe_address"]
            new_executor = ClobExecutor(client, trade_log_file=BOT_TRADES_FILE)
            self._clob_executor = new_executor
            # Propagate to all live (non-dry-run) slots
            for slot in self._slots:
                if not slot.dry_run:
                    slot.executor = new_executor
        self._consecutive_auth_failures = 0
        logger.info("Auth refreshed successfully.")

    # ----- Single session -----

    def _run_one_session(self):
        # type: () -> None
        """Run one session: always collects, optionally trades."""

        # 0. Apply any pending resolution credits from previous session
        self._drain_resolution_credits()

        # 1. Snapshot strategy slots (determines this session's mode)
        slots = self._snapshot_slots()
        mode = "trading" if slots else "collect-only"

        # Re-sync balance from CLOB at session start (only when single live slot, auto-balance)
        # Skip if a background resolution is still pending (CLOB balance won't reflect payout yet)
        has_pending_resolution = (
            self._resolution_thread is not None
            and self._resolution_thread.is_alive()
        )
        live_slots = [s for s in slots if not s.dry_run]
        if len(live_slots) == 1 and not has_pending_resolution:
            slot = live_slots[0]
            if not slot.cash_explicit and slot.position.up_tokens == 0 and slot.position.down_tokens == 0:
                try:
                    real_balance = self._fetch_clob_balance()
                    if abs(real_balance - slot.position.cash) > 0.01:
                        logger.info(
                            "[%s] Balance re-sync: tracked=$%.4f on-chain=$%.4f",
                            slot.name, slot.position.cash, real_balance,
                        )
                        slot.position.cash = real_balance
                        slot.starting_cash = real_balance
                except Exception as e:
                    logger.warning("Balance re-sync failed (using tracked): %s", e)

        # 2. Wait for next window
        last_status_write = [0.0]

        def _on_waiting(next_dt):
            now = time.time()
            if now - last_status_write[0] >= 5.0:
                trading_info = None
                if mode == "trading":
                    # Show first slot for backward compat
                    first = slots[0]
                    trading_info = {
                        "enabled": True,
                        "strategy": first.name,
                        "cash": round(first.position.cash, 4),
                        "strategies": [
                            {"name": s.name, "cash": round(s.position.cash, 4)}
                            for s in slots
                        ],
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
        # initial_btc_price is set from the first tick (no separate API call)
        session = SessionData(
            metadata=metadata,
            initial_btc_price_usd=None,
            final_btc_price_usd=None,
            outcome=None,
            outcome_prices=None,
            collection_started_utc=datetime.now(timezone.utc).isoformat(),
        )

        # 5. Poll loop — branch on mode
        # slot_results: list of (slot, trade_count, trade_dicts, decision_log) per slot
        slot_results = []  # type: List[tuple]
        if mode == "trading":
            slot_results = self._poll_with_trading(
                session, metadata, window_ts, slots,
            )
        else:
            self._poll_collect_only(session, metadata, window_ts)

        # 6. Record final BTC price from last sample
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

        # 8. If trading: resolve in background (don't block next window)
        if mode == "trading" and slot_results:
            # Ensure any previous resolution is done first
            self._join_pending_resolution(timeout=120)
            self._drain_resolution_credits()

            # Snapshot resolution data before modifying positions
            resolution_data = []
            for slot, trade_count, trade_dicts, decision_log in slot_results:
                resolution_data.append({
                    "slot_name": slot.name,
                    "up_tokens": slot.position.up_tokens,
                    "down_tokens": slot.position.down_tokens,
                    "dry_run": slot.dry_run,
                    "starting_cash": slot.starting_cash,
                    "session_end_cash": slot.position.cash,
                    "strategy_params": {
                        k: v for k, v in slot.strategy.__dict__.items()
                        if not k.startswith("_")
                    },
                    "trade_count": trade_count,
                    "trade_dicts": trade_dicts,
                    "decision_log": decision_log,
                })
                # Zero out tokens so next session starts clean
                slot.position.up_tokens = 0.0
                slot.position.down_tokens = 0.0

                # Restore session reserve (cash held back by session_cap)
                if slot._session_reserve > 0:
                    slot.position.cash += slot._session_reserve
                    logger.info(
                        "[%s] Restored reserve $%.2f -> total cash $%.2f",
                        slot.name, slot._session_reserve, slot.position.cash,
                    )
                    slot._session_reserve = 0.0

            # Launch background resolution
            self._resolution_thread = threading.Thread(
                target=self._resolve_in_background,
                args=(
                    session, metadata, window_ts, resolution_data,
                    session.initial_btc_price_usd, filepath,
                ),
                daemon=True,
                name="resolution-{}".format(window_ts),
            )
            self._resolution_thread.start()

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
                        if session.initial_btc_price_usd is None:
                            session.initial_btc_price_usd = sample.btc_price_usd
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
            if now - last_live_write >= 2.0 and session.samples:
                self.recorder.write_live_session(session)
                last_live_write = now

            # Exit once window ended and all in-flight samples collected
            if now >= window_end_ts and not pending:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)

    # ----- Trading poll loop -----

    def _poll_with_trading(self, session, metadata, window_ts, slots):
        # type: (SessionData, Any, int, List[StrategySlot]) -> List[tuple]
        """Single-sample sequential poll loop with strategy tick processing.

        Creates one Trader per slot. Each tick is fed to ALL traders.
        Returns list of (slot, trade_count, trade_dicts) tuples.
        """
        metadata_dict = asdict(metadata)

        # Daily loss tracking: reset reference balance each new UTC day
        today = datetime.now(timezone.utc).date()
        if not hasattr(self, '_day_start_cash') or self._current_day != today:
            # Use sum of all slot starting cash as the day reference
            self._day_start_cash = sum(s.starting_cash for s in slots)
            self._current_day = today

        # Traders are initialized on first tick (need initial BTC price from stream)
        traders = []  # type: List[Trader]
        per_slot_trade_dicts = []  # type: List[List[Dict[str, Any]]]
        per_slot_trade_counts = []  # type: List[int]
        traders_initialized = False

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

                    # Initialize traders on first tick (gets BTC price from stream)
                    if not traders_initialized:
                        initial_btc_price = sample.btc_price_usd or 0.0
                        session.initial_btc_price_usd = initial_btc_price
                        for slot in slots:
                            # Per-session cash: reset to fixed budget each session
                            if slot.per_session_cash is not None:
                                old = slot.position.cash
                                slot.position.cash = slot.per_session_cash
                                slot.starting_cash = slot.per_session_cash
                                if abs(old - slot.per_session_cash) > 0.01:
                                    logger.info(
                                        "[%s] Per-session cash reset: $%.2f -> $%.2f",
                                        slot.name, old, slot.per_session_cash,
                                    )

                            # Session cap: hold back excess cash as reserve
                            slot._session_reserve = 0.0
                            if slot.session_cap is not None and slot.position.cash > slot.session_cap:
                                slot._session_reserve = slot.position.cash - slot.session_cap
                                slot.position.cash = slot.session_cap
                                logger.info(
                                    "[%s] Session cap $%.2f: deploying $%.2f, reserve $%.2f",
                                    slot.name, slot.session_cap,
                                    slot.position.cash, slot._session_reserve,
                                )

                            trader = Trader(slot.strategy, slot.executor)
                            ref_balance = slot.position.cash
                            trader.start_session(
                                metadata_dict, slot.position, initial_btc_price,
                                self.window_duration, reference_balance=ref_balance,
                                capture_decision_log=True,
                            )
                            traders.append(trader)
                            per_slot_trade_dicts.append([])
                            per_slot_trade_counts.append(0)
                        traders_initialized = True

                    # Feed tick to ALL traders
                    for i, (slot, trader) in enumerate(zip(slots, traders)):
                        try:
                            trade = trader.process_tick(sample_dict)
                        except Exception as te:
                            logger.warning("[%s] Tick error: %s", slot.name, te)
                            continue

                        # Log limit order fills from this tick
                        for fill in trader.last_tick_fills:
                            per_slot_trade_dicts[i].append(_trade_to_dashboard_dict(fill))
                            per_slot_trade_counts[i] += 1
                            if fill.success is True and not slot.dry_run:
                                _save_slot_position(slot.name, slot.position)
                            logger.info(
                                "[%s] Limit fill: %s @ %.2f x %.2f | order_id=%s",
                                slot.name, fill.side, fill.price, fill.tokens,
                                fill.order_id,
                            )

                        if trade is not None:
                            per_slot_trade_dicts[i].append(_trade_to_dashboard_dict(trade))
                            if trade.success is not False:
                                per_slot_trade_counts[i] += 1
                                if trade.success is True and not slot.dry_run:
                                    _save_slot_position(slot.name, slot.position)
                                logger.info(
                                    "[%s] Trade executed: %s @ %.2f x %.2f | order_id=%s",
                                    slot.name, trade.side, trade.price, trade.tokens,
                                    trade.order_id,
                                )
                            else:
                                logger.warning("[%s] Trade failed: %s", slot.name, trade.error)

                    logger.debug(
                        "Tick %d: BTC=$%s Up=%s Down=%s",
                        traders[0]._tick_index if traders else 0,
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
            if now - last_live_write >= 2.0 and session.samples:
                first = slots[0]
                strategies_info = []
                for i, slot in enumerate(slots):
                    strategies_info.append({
                        "name": slot.name,
                        "dry_run": slot.dry_run,
                        "cash": round(slot.position.cash, 4),
                        "trades": list(per_slot_trade_dicts[i]),
                    })
                trading_info = {
                    "enabled": True,
                    "strategy": first.name,
                    "dry_run": first.dry_run,
                    "cash": round(first.position.cash, 4),
                    "trades": list(per_slot_trade_dicts[0]),
                    "strategies": strategies_info,
                }
                self.recorder.write_live_session(session, trading_info=trading_info)
                last_live_write = now

            # Exit when window ended and no pending sample
            if time.time() >= window_end_ts and pending_future is None:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)

        # Cancel all pending limit orders before snapshotting position
        for i, (slot, trader) in enumerate(zip(slots, traders)):
            cancelled = trader.cancel_all_pending()
            if cancelled > 0:
                logger.info(
                    "[%s] Cancelled %d pending orders at session end",
                    slot.name, cancelled,
                )
                if not slot.dry_run:
                    _save_slot_position(slot.name, slot.position)

        return [
            (slot, per_slot_trade_counts[i], per_slot_trade_dicts[i], traders[i].decision_log)
            for i, slot in enumerate(slots)
        ]

    # ----- Trading session resolution -----

    def _resolve_in_background(self, session, metadata, window_ts, resolution_data,
                               initial_btc_price, session_filepath):
        # type: (SessionData, Any, int, List[Dict[str, Any]], Optional[float], Optional[str]) -> None
        """Background thread: wait for resolution, queue cash credits, log results.

        Runs in a daemon thread so it doesn't block the main loop from starting
        the next window. Position tokens were already zeroed by the caller;
        winning tokens are queued back via ``_credit_queue``.
        """
        try:
            total_trades = sum(d["trade_count"] for d in resolution_data)
            strategy_names = [d["slot_name"] for d in resolution_data]

            logger.info("Waiting for resolution (background)...")
            self.recorder.write_live_status(
                "resolving",
                trading_info={
                    "strategy": strategy_names[0],
                    "trades": total_trades,
                    "strategies": strategy_names,
                },
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
                        "unavailable. Tokens lost."
                    )

            any_live_trades = False
            sidecar_strategies = []

            for d in resolution_data:
                up = d["up_tokens"]
                down = d["down_tokens"]

                # Compute credit from resolution outcome
                if outcome == "Up":
                    credit = up
                elif outcome == "Down":
                    credit = down
                else:
                    credit = 0.0
                    if up > 0 or down > 0:
                        logger.error(
                            "[%s] Clearing %.4f phantom tokens (no resolution)",
                            d["slot_name"], up + down,
                        )

                # Queue credit for main thread to apply
                if credit > 0:
                    self._credit_queue.put((d["slot_name"], credit))

                final_cash = d["session_end_cash"] + credit
                pnl = final_cash - d["starting_cash"]

                # Log per-slot summary
                summary = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "slug_prefix": self.slug_prefix,
                    "window_ts": window_ts,
                    "strategy": d["slot_name"],
                    "outcome": outcome,
                    "trades": d["trade_count"],
                    "cash": round(final_cash, 4),
                    "session_number": self.session_count,
                }
                try:
                    with open(BOT_SESSION_LOG, "a") as f:
                        f.write(json.dumps(summary) + "\n")
                except Exception as e:
                    logger.warning("Failed to log session summary: %s", e)

                # Append resolution record to trade log so win/loss is visible
                resolution_record = {
                    "type": "resolution",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "window_ts": window_ts,
                    "strategy": d["slot_name"],
                    "outcome": outcome,
                    "starting_cash": round(d["starting_cash"], 4),
                    "final_cash": round(final_cash, 4),
                    "pnl": round(pnl, 4),
                    "credit": round(credit, 4),
                    "trades": d["trade_count"],
                    "up_tokens": round(d["up_tokens"], 4),
                    "down_tokens": round(d["down_tokens"], 4),
                    "dry_run": d["dry_run"],
                }
                try:
                    with open(BOT_TRADES_FILE, "a") as f:
                        f.write(json.dumps(resolution_record) + "\n")
                except Exception as e:
                    logger.warning("Failed to log resolution to trade log: %s", e)

                logger.info(
                    "[%s] Session resolved: %s | Trades: %d | Cash: $%.2f (credit: +$%.4f)",
                    d["slot_name"], outcome, d["trade_count"], final_cash, credit,
                )

                if d["trade_count"] > 0 and not d["dry_run"]:
                    any_live_trades = True

                sidecar_entry = {
                    "strategy": d["slot_name"],
                    "params": d["strategy_params"],
                    "dry_run": d["dry_run"],
                    "starting_cash": round(d["starting_cash"], 4),
                    "final_cash": round(final_cash, 4),
                    "pnl": round(pnl, 4),
                    "outcome": outcome,
                    "trades": d["trade_dicts"],
                }
                if d.get("decision_log"):
                    sidecar_entry["decision_log"] = d["decision_log"]
                sidecar_strategies.append(sidecar_entry)

            # Cache balance for simulator use
            total_credit = sum(
                d["up_tokens"] if outcome == "Up"
                else d["down_tokens"] if outcome == "Down"
                else 0.0
                for d in resolution_data
            )
            total_cash = sum(d["session_end_cash"] for d in resolution_data) + total_credit
            try:
                _save_balance_cache(total_cash, self.slug_prefix, self.session_count)
            except Exception as e:
                logger.warning("Failed to save balance cache: %s", e)

            # Write trading sidecar file
            if session_filepath:
                first = sidecar_strategies[0] if sidecar_strategies else {}
                sidecar = dict(first)  # backward-compat top-level fields
                sidecar["strategies"] = sidecar_strategies
                sidecar_path = session_filepath + ".trading.json"
                try:
                    with open(sidecar_path, "w") as f:
                        json.dump(sidecar, f, indent=2)
                    logger.info("Trading sidecar saved: %s", sidecar_path)
                except Exception as e:
                    logger.warning("Failed to save trading sidecar: %s", e)

            # Auto-claim resolved positions
            if any_live_trades and self._private_key and self._safe_address:
                from app.claim import has_builder_creds, claim_background
                if has_builder_creds():
                    try:
                        claim_background(self._private_key, self._safe_address)
                    except Exception as e:
                        logger.debug("Skipping background claim: %s", e)

        except Exception as e:
            logger.error("Background resolution error: %s", e, exc_info=True)
