"""MarketRunner: data collection + worker management for a single market.

Refactored from the original Runner (app/runner.py) to run as a thread
managed by AppManager. Owns the data stream (PriceFetcher, SessionRecorder,
TickBroadcaster) and spawns/kills bot worker processes.

Does NOT own shared resources (LoggingSession, RTDSClient) — those are
passed in by AppManager and shared across markets.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.defaults import (
    BALANCE_CACHE_FILE,
    BASE_DIR,
    POLL_INTERVAL_SECONDS,
    TOKEN_DECIMALS,
    WINDOW_DURATION_SECONDS,
    market_data_dir,
    market_sessions_dir,
    market_socket_path,
    market_workers_dir,
    market_strategies_file,
)
from core.models import SessionData
from core.price_fetcher import PriceFetcher
from core.session_recorder import SessionRecorder
from bots import ALL_STRATEGIES, get_strategies_for_market
from core.tick_broadcast import TickBroadcaster
from core.session_cache import SessionStatsCache
from core.window_ops import (
    wait_for_next_window,
    fetch_metadata_with_retry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker tracking
# ---------------------------------------------------------------------------


@dataclass
class WorkerInfo:
    """Tracks a spawned bot worker process."""
    strategy_name: str
    params: Dict[str, Any]
    cash: float
    dry_run: bool
    per_session: bool
    session_cap: Optional[float]
    session_cash_ratio: Optional[Dict[str, float]]
    process: Optional[subprocess.Popen]
    started_at: str
    log_fh: Optional[Any] = None  # file handle for worker log

    def close_log(self):
        # type: () -> None
        """Close the log file handle if open."""
        if self.log_fh is not None:
            try:
                self.log_fh.close()
            except OSError:
                pass
            self.log_fh = None


# ---------------------------------------------------------------------------
# Balance cache helpers
# ---------------------------------------------------------------------------


def _save_balance_cache(cash, slug_prefix, session_count):
    # type: (float, str, int) -> None
    os.makedirs(os.path.dirname(BALANCE_CACHE_FILE), exist_ok=True)
    with open(BALANCE_CACHE_FILE, "w") as f:
        json.dump({
            "cash": round(cash, TOKEN_DECIMALS),
            "slug_prefix": slug_prefix,
            "session_count": session_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


def _load_balance_cache():
    # type: () -> Optional[Dict[str, Any]]
    try:
        with open(BALANCE_CACHE_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "cash" not in data:
            logger.warning("Balance cache corrupt (missing 'cash' key), ignoring.")
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Balance cache unreadable, ignoring: %s", exc)
        return None


# ---------------------------------------------------------------------------
# MarketRunner
# ---------------------------------------------------------------------------


class MarketRunner:
    """Data collection + worker process management for one market.

    Runs as a thread managed by AppManager. Owns the data stream and
    tick broadcaster. Trading strategies run as independent worker processes.
    """

    def __init__(
        self,
        slug_prefix,                                    # type: str
        http_session,                                   # type: Any
        rtds_client,                                    # type: Any
        market_type="updown",                           # type: str
        window_duration=WINDOW_DURATION_SECONDS,        # type: int
        poll_interval=POLL_INTERVAL_SECONDS,            # type: float
        max_sessions=0,                                 # type: int
    ):
        self.slug_prefix = slug_prefix
        self.slug_key = slug_prefix.rstrip("-")
        self.market_type = market_type
        self.window_duration = window_duration
        self.poll_interval = poll_interval
        self.max_sessions = max_sessions

        # Shared infrastructure (from AppManager)
        self.http_session = http_session
        self.rtds = rtds_client

        # Per-market infrastructure
        self.fetcher = PriceFetcher(session=http_session, rtds_client=rtds_client)
        self._sample_pool = ThreadPoolExecutor(max_workers=8)
        self.recorder = SessionRecorder(slug_prefix)

        # Per-market paths
        self._market_dir = market_data_dir(self.slug_key)
        self._workers_dir = market_workers_dir(self.slug_key)
        self._strategies_file = market_strategies_file(self.slug_key)
        self._socket_path = market_socket_path(self.slug_key)

        # Tick broadcaster
        os.makedirs(os.path.dirname(self._socket_path), exist_ok=True)
        self._broadcaster = TickBroadcaster(self._socket_path)

        # State
        self.running = True
        self._stop_after_session = False
        self.session_count = 0

        # Session stats cache — loads all existing sessions on startup,
        # updated in-memory after each session resolves. Bots receive stats
        # via the session_start broadcast. File written for dashboard only.
        self._stats_cache = self._init_stats_cache()

        # Worker processes (protected by lock)
        self._lock = threading.Lock()
        self._workers = []  # type: List[WorkerInfo]
        self._live_trades_cache = {}  # type: Dict[str, Dict[str, Any]]

        # Valid strategies for this market type
        self._valid_strategies = get_strategies_for_market(market_type)
        if not self._valid_strategies:
            # Fall back to all strategies if market type unknown
            self._valid_strategies = ALL_STRATEGIES

    def _init_stats_cache(self):
        # type: () -> SessionStatsCache
        """Build session stats cache from all existing session files on disk."""
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

    def request_stop(self):
        # type: () -> None
        logger.info("[%s] Stop requested.", self.slug_key)
        self.running = False

    def request_stop_after_session(self):
        # type: () -> None
        logger.info("[%s] Kill requested — will stop after current session.", self.slug_key)
        self._stop_after_session = True

    # ----- Worker management -----

    def enable_trading(self, strategy_name, params=None, cash=None, dry_run=False,
                       per_session=False, session_cap=None, session_cash_ratio=None):
        # type: (str, Optional[Dict[str, Any]], Optional[float], bool, bool, Optional[float], Optional[Dict[str, float]]) -> None
        """Spawn a bot worker process for the given strategy."""
        if strategy_name not in self._valid_strategies:
            raise ValueError("Unknown strategy '{}' for market type '{}'. Available: {}".format(
                strategy_name, self.market_type, list(self._valid_strategies.keys()),
            ))

        parsed_params = {}  # type: Dict[str, Any]
        if params:
            for k, v in params.items():
                if isinstance(v, str):
                    try:
                        v = int(v)
                    except ValueError:
                        try:
                            v = float(v)
                        except ValueError:
                            pass
                parsed_params[k] = v

        # Determine cash
        if cash is None and not dry_run:
            state_file = os.path.join(self._workers_dir, strategy_name, "state.json")
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r") as f:
                        saved = json.load(f)
                    cash = saved.get("bankroll")
                    if cash is None:
                        # One-time migration from old state format.
                        # Safe to remove this block after 2026-03-12.
                        cash = saved.get("live_cash") or saved.get("cash")
                        if cash is not None:
                            saved["bankroll"] = cash
                            tmp = state_file + ".tmp"
                            with open(tmp, "w") as f:
                                json.dump(saved, f, indent=2)
                            os.replace(tmp, state_file)
                            logger.info("[%s/%s] Migrated state.json to bankroll field",
                                        self.slug_key, strategy_name)
                    logger.info(
                        "[%s/%s] Using saved cash from last run: $%.2f",
                        self.slug_key, strategy_name, cash,
                    )
                except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
                    logger.warning("[%s/%s] Failed to read saved state, will use default: %s",
                                   self.slug_key, strategy_name, exc)
            if cash is None:
                cash = 100.0
                logger.info(
                    "[%s/%s] No cash specified and no saved state, defaulting to $%.2f",
                    self.slug_key, strategy_name, cash,
                )

        starting_cash = float(cash) if cash is not None else 100.0

        with self._lock:
            self._kill_worker_by_name(strategy_name)
            process, log_fh = self._spawn_worker(
                strategy_name, parsed_params, starting_cash,
                dry_run, per_session, session_cap, session_cash_ratio,
            )
            worker = WorkerInfo(
                strategy_name=strategy_name,
                params=parsed_params,
                cash=starting_cash,
                dry_run=dry_run,
                per_session=per_session,
                session_cap=session_cap,
                session_cash_ratio=session_cash_ratio,
                process=process,
                started_at=datetime.now(timezone.utc).isoformat(),
                log_fh=log_fh,
            )
            self._workers.append(worker)

        logger.info(
            "[%s] Worker spawned: strategy=%s params=%s cash=$%.2f dry_run=%s "
            "per_session=%s session_cap=%s session_cash_ratio=%s pid=%s",
            self.slug_key, strategy_name, parsed_params, starting_cash, dry_run,
            per_session, session_cap, session_cash_ratio,
            process.pid if process else None,
        )
        self._save_strategies_config()

    def _spawn_worker(self, strategy_name, params, cash, dry_run, per_session, session_cap, session_cash_ratio=None):
        # type: (str, Dict[str, Any], float, bool, bool, Optional[float], Optional[Dict[str, float]]) -> subprocess.Popen
        """Spawn a bot worker subprocess."""
        cmd = [
            sys.executable, "-m", "app.bot_worker",
            "--strategy", strategy_name,
            "--socket", self._socket_path,
            "--market-dir", self._market_dir,
            "--cash", str(cash),
            "--window", str(self.window_duration),
            "--restore",
        ]
        if params:
            param_str = ",".join("{}={}".format(k, v) for k, v in params.items() if v is not None)
            cmd.extend(["--params", param_str])
        if dry_run:
            cmd.append("--dry-run")
        if per_session:
            cmd.append("--per-session")
        if session_cap is not None:
            cmd.extend(["--session-cap", str(session_cap)])
        if session_cash_ratio is not None:
            cmd.extend(["--session-cash-ratio", "{}:{}".format(
                session_cash_ratio["ratio"], session_cash_ratio["max"],
            )])

        log_dir = os.path.join(self._workers_dir, strategy_name)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "worker.log")
        log_fh = open(log_file, "a")

        process = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
            start_new_session=True,
        )
        logger.info(
            "[%s] Spawned worker %s (PID %d), logging to %s",
            self.slug_key, strategy_name, process.pid, log_file,
        )
        return process, log_fh

    def _kill_worker_by_name(self, strategy_name):
        # type: (str) -> None
        """Kill a worker process by strategy name. Caller must hold lock."""
        remaining = []
        for w in self._workers:
            if w.strategy_name == strategy_name:
                if w.process and w.process.poll() is None:
                    logger.info("[%s] Killing worker %s (PID %d)",
                                self.slug_key, w.strategy_name, w.process.pid)
                    try:
                        os.kill(w.process.pid, signal.SIGTERM)
                        w.process.wait(timeout=10)
                    except (OSError, subprocess.TimeoutExpired) as e:
                        logger.warning("Failed to kill worker %s: %s", w.strategy_name, e)
                        try:
                            w.process.kill()
                        except OSError:
                            pass
                w.close_log()
            else:
                remaining.append(w)
        self._workers = remaining

    def _kill_all_workers(self):
        # type: () -> None
        with self._lock:
            for w in self._workers:
                if w.process and w.process.poll() is None:
                    logger.info("[%s] Killing worker %s (PID %d)",
                                self.slug_key, w.strategy_name, w.process.pid)
                    try:
                        os.kill(w.process.pid, signal.SIGTERM)
                    except OSError:
                        pass
            for w in self._workers:
                if w.process:
                    try:
                        w.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            w.process.kill()
                        except OSError:
                            pass
                w.close_log()
            self._workers.clear()

    def _read_worker_state(self, strategy_name):
        # type: (str) -> Optional[Dict[str, Any]]
        state_file = os.path.join(self._workers_dir, strategy_name, "state.json")
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            if os.path.exists(state_file):
                logger.warning("Worker state file corrupt for %s, ignoring: %s",
                               strategy_name, exc)
            return None

    @staticmethod
    def _state_bankroll(state, fallback):
        # type: (Optional[Dict[str, Any]], float) -> float
        """Extract bankroll from a worker state dict."""
        if not state:
            return fallback
        bankroll = state.get("bankroll")
        return bankroll if bankroll is not None else fallback

    def _read_worker_live_trades(self, strategy_name):
        # type: (str) -> Optional[Dict[str, Any]]
        trades_file = os.path.join(self._workers_dir, strategy_name, "live_trades.json")
        try:
            with open(trades_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            if os.path.exists(trades_file):
                logger.warning("Live trades file corrupt for %s, ignoring: %s",
                               strategy_name, exc)
            return None

    def disable_trading(self, strategy_name=None):
        # type: (Optional[str],) -> None
        with self._lock:
            if strategy_name is not None:
                found = any(w.strategy_name == strategy_name for w in self._workers)
                if not found:
                    raise ValueError("No active worker for strategy '{}'".format(strategy_name))
                self._kill_worker_by_name(strategy_name)
                logger.info("[%s] Worker killed: %s", self.slug_key, strategy_name)
            else:
                for w in self._workers:
                    if w.process and w.process.poll() is None:
                        try:
                            os.kill(w.process.pid, signal.SIGTERM)
                        except OSError:
                            pass
                for w in self._workers:
                    if w.process:
                        try:
                            w.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            try:
                                w.process.kill()
                            except OSError:
                                pass
                self._workers.clear()
                logger.info("[%s] All workers killed.", self.slug_key)
            self._save_strategies_config()

    def set_cash(self, cash=None, strategy_name=None, session_cash_ratio=None,
                 session_cap=None, clear_session_limit=False):
        # type: (Optional[float], Optional[str], Optional[Dict[str, float]], Optional[float], bool) -> None
        with self._lock:
            if not self._workers:
                raise RuntimeError("No active workers to update")
            if strategy_name is not None:
                target = [w for w in self._workers if w.strategy_name == strategy_name]
                if not target:
                    raise ValueError("No active worker for strategy '{}'".format(strategy_name))
            elif len(self._workers) == 1:
                target = [self._workers[0]]
                strategy_name = target[0].strategy_name
            else:
                raise ValueError(
                    "Multiple workers active — specify which one: {}".format(
                        [w.strategy_name for w in self._workers]
                    )
                )

        for w in target:
            pending = {}
            if cash is not None:
                pending["bankroll"] = round(cash, TOKEN_DECIMALS)
                w.cash = round(cash, TOKEN_DECIMALS)
            if session_cash_ratio is not None:
                pending["session_cash_ratio"] = session_cash_ratio
                pending["session_cap"] = None
                w.session_cash_ratio = session_cash_ratio
                w.session_cap = None
            elif session_cap is not None:
                pending["session_cap"] = session_cap
                pending["session_cash_ratio"] = None
                w.session_cap = session_cap
                w.session_cash_ratio = None
            elif clear_session_limit:
                pending["session_cap"] = None
                pending["session_cash_ratio"] = None
                w.session_cap = None
                w.session_cash_ratio = None
            try:
                # Write pending config into state.json atomically.
                # The worker reads and clears it at the next session boundary.
                state_file = os.path.join(self._workers_dir, w.strategy_name, "state.json")
                state = {}
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r") as f:
                            state = json.load(f)
                    except (OSError, json.JSONDecodeError, ValueError) as exc:
                        logger.warning("Worker state corrupt for %s, starting fresh: %s",
                                       w.strategy_name, exc)
                        state = {}
                state["pending"] = pending
                tmp = state_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, state_file)
                if cash is not None:
                    logger.info("[%s/%s] Pending bankroll update: $%.4f",
                                self.slug_key, w.strategy_name, cash)
                else:
                    logger.info("[%s/%s] Pending session limits update",
                                self.slug_key, w.strategy_name)
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to write pending update for %s: %s", w.strategy_name, e)
        self._save_strategies_config()

    def set_auto_exit(self, enabled, strategy_name=None):
        # type: (bool, Optional[str]) -> None
        """Toggle auto-exit for a worker by creating/removing a flag file."""
        if self.market_type != "updown":
            raise ValueError("Auto-exit is only supported for updown markets")
        with self._lock:
            if not self._workers:
                raise RuntimeError("No active workers to update")
            if strategy_name is not None:
                target = [w for w in self._workers if w.strategy_name == strategy_name]
                if not target:
                    raise ValueError("No active worker for strategy '{}'".format(strategy_name))
            elif len(self._workers) == 1:
                target = [self._workers[0]]
            else:
                raise ValueError(
                    "Multiple workers active — specify which one: {}".format(
                        [w.strategy_name for w in self._workers]
                    )
                )

        for w in target:
            flag_file = os.path.join(self._workers_dir, w.strategy_name, "auto_exit.flag")
            if enabled:
                with open(flag_file, "w") as f:
                    f.write("1")
                logger.info("[%s/%s] Auto-exit enabled", self.slug_key, w.strategy_name)
            else:
                try:
                    os.remove(flag_file)
                except OSError:
                    pass
                logger.info("[%s/%s] Auto-exit disabled", self.slug_key, w.strategy_name)

    def _save_strategies_config(self):
        # type: () -> None
        """Save configured strategy settings (not live state) for restore."""
        configs = []
        for w in self._workers:
            configs.append({
                "name": w.strategy_name,
                "params": w.params,
                "cash": w.cash,
                "dry_run": w.dry_run,
                "per_session": w.per_session,
                "session_cap": w.session_cap,
                "session_cash_ratio": w.session_cash_ratio,
            })
        try:
            os.makedirs(os.path.dirname(self._strategies_file), exist_ok=True)
            tmp = self._strategies_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(configs, f, indent=2)
            os.replace(tmp, self._strategies_file)
        except OSError:
            logger.exception("Failed to save strategies config")

    def restore_strategies(self):
        # type: () -> None
        if not os.path.exists(self._strategies_file):
            logger.info("[%s] No strategies config file found — nothing to restore.",
                        self.slug_key)
            return
        try:
            with open(self._strategies_file, "r") as f:
                configs = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error("[%s] Failed to read strategies config: %s", self.slug_key, e)
            return
        if not configs:
            logger.info("[%s] Strategies config is empty — nothing to restore.", self.slug_key)
            return
        for entry in configs:
            name = entry.get("name")
            if not name:
                continue
            try:
                # Sanitize params: convert string "None" to actual None
                raw_params = entry.get("params", {})
                clean_params = {k: (None if v == "None" else v) for k, v in raw_params.items()}
                self.enable_trading(
                    strategy_name=name,
                    params=clean_params,
                    cash=entry.get("cash"),
                    dry_run=entry.get("dry_run", False),
                    per_session=entry.get("per_session", False),
                    session_cap=entry.get("session_cap"),
                    session_cash_ratio=entry.get("session_cash_ratio"),
                )
                logger.info("[%s] Restored strategy: %s", self.slug_key, name)
            except Exception:
                logger.exception("[%s] Failed to restore strategy '%s'", self.slug_key, name)

    def get_status(self):
        # type: () -> Dict[str, Any]
        with self._lock:
            strategies = []
            for w in self._workers:
                alive = w.process is not None and w.process.poll() is None
                state = self._read_worker_state(w.strategy_name)
                bankroll = self._state_bankroll(state, w.cash)
                # Check for pending bankroll update (set-cash not yet picked up by worker)
                pending = state.get("pending", {}) if state else {}
                pending_bankroll = pending.get("bankroll")
                if pending_bankroll is not None:
                    bankroll = pending_bankroll
                info = {
                    "name": w.strategy_name,
                    "params": w.params,
                    "dry_run": w.dry_run,
                    "cash": round(bankroll, TOKEN_DECIMALS),
                    "bankroll": round(bankroll, TOKEN_DECIMALS),
                    "starting_cash": round(w.cash, TOKEN_DECIMALS),
                    "pid": w.process.pid if w.process else None,
                    "alive": alive,
                }
                if w.session_cap is not None:
                    info["session_cap"] = round(w.session_cap, TOKEN_DECIMALS)
                if w.session_cash_ratio is not None:
                    info["session_cash_ratio"] = w.session_cash_ratio
                flag_file = os.path.join(self._workers_dir, w.strategy_name, "auto_exit.flag")
                info["auto_exit"] = os.path.exists(flag_file)
                strategies.append(info)

            return {
                "slug_prefix": self.slug_prefix,
                "slug_key": self.slug_key,
                "market_type": self.market_type,
                "running": self.running,
                "stop_after_session": self._stop_after_session,
                "session_count": self.session_count,
                "trading_enabled": len(self._workers) > 0,
                "strategies": strategies,
                "tick_clients": self._broadcaster.client_count,
            }

    # ----- Main loop -----

    def run(self):
        # type: () -> None
        """Main loop — data collection + tick broadcasting. Runs as thread target."""
        self._broadcaster.start()
        self._last_window_ts = None  # type: Optional[int]

        completed = self.recorder.count_completed_sessions()
        logger.info(
            "[%s] MarketRunner started. %d sessions already collected.",
            self.slug_key, completed,
        )
        self.recorder.append_log(
            "MarketRunner started",
            slug_prefix=self.slug_prefix,
            existing_sessions=completed,
        )

        try:
            while self.running:
                try:
                    self._run_one_session()
                    self.session_count += 1
                    logger.info("[%s] Sessions completed: %d",
                                self.slug_key, self.session_count)
                    if self._stop_after_session:
                        logger.info("[%s] Kill signal — stopping after session %d.",
                                    self.slug_key, self.session_count)
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
            self._kill_all_workers()
            self._broadcaster.stop()
            self._sample_pool.shutdown(wait=True)
            # Only shut down the fetcher's thread pool; don't close the shared HTTP session
            self.fetcher._pool.shutdown(wait=True)
            logger.info("[%s] MarketRunner stopped. %d sessions completed.",
                        self.slug_key, self.session_count)

    # ----- Single session -----

    def _run_one_session(self):
        # type: () -> None
        self._reap_dead_workers()

        last_status_write = [0.0]

        def _on_waiting(next_dt):
            now = time.time()
            if now - last_status_write[0] >= 5.0:
                trading_info = None
                workers = self._get_workers_snapshot()
                if workers:
                    first = workers[0]
                    state = self._read_worker_state(first.strategy_name)
                    first_cash = self._state_bankroll(state, first.cash)
                    trading_info = {
                        "enabled": True,
                        "strategy": first.strategy_name,
                        "cash": round(first_cash, TOKEN_DECIMALS),
                        "strategies": [
                            {
                                "name": w.strategy_name,
                                "cash": round(
                                    self._state_bankroll(self._read_worker_state(w.strategy_name), w.cash),
                                    4,
                                ),
                            }
                            for w in workers
                        ],
                    }
                self.recorder.write_live_status(
                    "waiting",
                    next_window_utc=next_dt.isoformat(),
                    trading_info=trading_info,
                )
                self._broadcaster.broadcast({
                    "type": "waiting",
                    "next_window_utc": next_dt.isoformat(),
                })
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

        # Clear stale position data before broadcasting session_start.
        # Workers delete their live_trades.json on session_end, but that
        # happens asynchronously.  A dashboard poll in the gap can read the
        # old file and re-populate the cache, making stale positions appear
        # in the new session.  Clearing here guarantees a clean slate.
        self.recorder.clear_live_session()
        self._live_trades_cache.clear()

        metadata_dict = asdict(metadata)
        # Inject prior session stats so bots get lookback data via
        # the broadcast — no disk reads needed in worker processes.
        ws = metadata.window_start_utc if hasattr(metadata, 'window_start_utc') else ""
        if ws and len(self._stats_cache) > 0:
            prior = self._stats_cache.prior(before=ws, n=len(self._stats_cache))
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
        self._live_trades_cache.clear()
        filepath = self.recorder.save_session(session)

        # Update in-memory session stats cache with the newly resolved session.
        # Dashboard reads stats via the control server HTTP endpoint.
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

        if self._workers:
            self._save_strategies_config()

    # ----- Poll loop with broadcasting -----

    def _poll_and_broadcast(self, session, metadata, window_ts):
        # type: (SessionData, Any, int) -> None
        window_end_ts = window_ts + self.window_duration
        # Hard deadline: never collect past the window end
        hard_deadline_ts = window_end_ts
        next_sample_time = time.time()
        pending = []
        last_live_write = 0.0
        tick_index = 0

        while self.running:
            now = time.time()

            # Hard cutoff — never run past deadline regardless of pending futures
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

            still_pending = []
            for f in pending:
                if f.done():
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
                else:
                    still_pending.append(f)
            pending = still_pending

            if now - last_live_write >= 2.0 and session.samples:
                trading_info = self._aggregate_worker_trading_info()
                self.recorder.write_live_session(session, trading_info=trading_info)
                last_live_write = now

            if now >= window_end_ts and not pending:
                break

            sleep_for = min(0.01, next_sample_time - time.time())
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _aggregate_worker_trading_info(self):
        # type: () -> Optional[Dict[str, Any]]
        workers = self._get_workers_snapshot()
        if not workers:
            return None

        strategies_info = []
        for w in workers:
            live = self._read_worker_live_trades(w.strategy_name)
            if live is not None:
                self._live_trades_cache[w.strategy_name] = live
            else:
                live = self._live_trades_cache.get(w.strategy_name)
            state = self._read_worker_state(w.strategy_name)
            state_cash = self._state_bankroll(state, w.cash)
            live_bankroll = live.get("bankroll", state_cash) if live else state_cash
            info = {
                "name": w.strategy_name,
                "dry_run": w.dry_run,
                "cash": live_bankroll,
                "bankroll": state_cash,
                "up_tokens": live.get("up_tokens", 0) if live else 0,
                "down_tokens": live.get("down_tokens", 0) if live else 0,
                "starting_cash": live.get("starting_cash", state_cash) if live else state_cash,
                "trades": live.get("trades", []) if live else [],
                "pending_orders": live.get("pending_orders", []) if live else [],
            }
            if w.session_cash_ratio is not None:
                info["session_cash_ratio"] = w.session_cash_ratio
            if w.session_cap is not None:
                info["session_cap"] = w.session_cap
            strategies_info.append(info)

        first = strategies_info[0]
        return {
            "enabled": True,
            "strategy": first["name"],
            "dry_run": first["dry_run"],
            "cash": first["cash"],
            "trades": first["trades"],
            "strategies": strategies_info,
        }

    def _get_workers_snapshot(self):
        # type: () -> List[WorkerInfo]
        with self._lock:
            return list(self._workers)

    def _reap_dead_workers(self):
        # type: () -> None
        with self._lock:
            for w in self._workers:
                if w.process and w.process.poll() is not None:
                    exit_code = w.process.returncode
                    logger.warning(
                        "[%s] Worker %s (PID %d) exited with code %d",
                        self.slug_key, w.strategy_name, w.process.pid, exit_code,
                    )

    @property
    def socket_path(self):
        # type: () -> str
        return self._socket_path
