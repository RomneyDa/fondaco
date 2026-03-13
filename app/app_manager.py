"""Central AppManager: manages collectors and worker runners.

Each market gets a standalone collector process (app/collector.py) that
owns the data stream and survives app restarts. The AppManager manages
bot worker processes via MarketRunner objects and the HTTP control server.

`app stop` kills workers but leaves collectors running.
`app market-off` stops both the collector and workers for that market.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from core.defaults import (
    APP_STATE_FILE,
    BASE_DIR,
    DEFAULT_CONTROL_PORT,
    collector_pid_path,
    collector_log_path,
    market_data_dir,
)
from core.markets import get_market_config

from app.market_runner import MarketRunner

logger = logging.getLogger(__name__)


def _pid_alive(pid):
    # type: (int) -> bool
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class AppManager:
    """Central manager for multiple markets.

    Spawns collector processes for data streams and manages worker
    processes via MarketRunner objects.
    """

    def __init__(self, port=DEFAULT_CONTROL_PORT, verbose_api_log=False):
        # type: (int, bool) -> None
        self.port = port
        self.verbose_api_log = verbose_api_log
        self.running = True

        # Markets (protected by lock)
        import threading
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._markets = {}  # type: Dict[str, MarketRunner]
        self._optimizer_process = None  # type: Optional[subprocess.Popen]
        self._optimizer_log_fh = None  # type: Optional[Any]

    # ----- Collector management -----

    def _read_collector_pid(self, slug_key):
        # type: (str) -> Optional[int]
        """Read and validate collector PID from its PID file."""
        pid_path = collector_pid_path(slug_key)
        try:
            with open(pid_path, "r") as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                return pid
        except (OSError, ValueError):
            pass
        return None

    def _ensure_collector(self, slug_prefix, window_duration, max_sessions=0):
        # type: (str, int, int) -> None
        """Start a collector process if one isn't already running."""
        slug_key = slug_prefix.rstrip("-")

        existing_pid = self._read_collector_pid(slug_key)
        if existing_pid is not None:
            logger.info("Collector for %s already running (PID %d)", slug_key, existing_pid)
            return

        log_path = collector_log_path(slug_key)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_fh = open(log_path, "a")

        cmd = [
            sys.executable, "-m", "app.collector",
            "--slug-prefix", slug_prefix,
            "--window", str(window_duration),
        ]
        if max_sessions > 0:
            cmd.extend(["--max-sessions", str(max_sessions)])
        if self.verbose_api_log:
            cmd.append("--verbose-api-log")

        process = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=BASE_DIR,
            start_new_session=True,
        )
        # Close parent's copy of the fd; child has its own via fork
        log_fh.close()

        logger.info("Collector started for %s (PID %d)", slug_key, process.pid)
        # Give the collector a moment to write its PID file
        time.sleep(1.0)

    def _stop_collector(self, slug_key):
        # type: (str) -> None
        """Stop the collector process for a market via SIGTERM."""
        pid = self._read_collector_pid(slug_key)
        if pid is None:
            logger.info("No running collector for %s", slug_key)
            return

        logger.info("Stopping collector %s (PID %d)", slug_key, pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            logger.warning("Failed to signal collector %s: %s", slug_key, e)
            return

        # Wait for clean shutdown
        for _ in range(30):
            if not _pid_alive(pid):
                logger.info("Collector %s stopped.", slug_key)
                return
            time.sleep(1)
        logger.warning("Collector %s (PID %d) did not exit within 30s.", slug_key, pid)

    def _signal_collector_stop_after_session(self, slug_key):
        # type: (str) -> None
        """Signal the collector to stop after the current session finishes."""
        pid = self._read_collector_pid(slug_key)
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGUSR1)
            logger.info("Collector %s signaled to stop after session (PID %d)", slug_key, pid)
        except OSError as e:
            logger.warning("Failed to signal collector %s: %s", slug_key, e)

    # ----- Market management -----

    def start_market(self, slug_prefix, window_duration=None,
                     market_type="updown", max_sessions=0):
        # type: (str, Optional[int], str, int) -> MarketRunner
        """Start data collection and create a worker manager for a market."""
        slug_key = slug_prefix.rstrip("-")

        if window_duration is None:
            window_duration = get_market_config(slug_prefix).window_duration_seconds

        with self._lock:
            if slug_key in self._markets:
                raise ValueError("Market '{}' is already running.".format(slug_key))

        # Start collector if not already running
        self._ensure_collector(slug_prefix, window_duration, max_sessions)

        # Create worker manager (no thread — just an object)
        runner = MarketRunner(
            slug_prefix=slug_prefix,
            market_type=market_type,
            window_duration=window_duration,
        )

        with self._lock:
            self._markets[slug_key] = runner

        logger.info("Started market: %s (type=%s, window=%ds)",
                     slug_key, market_type, window_duration)
        self._save_state()
        return runner

    def stop_market(self, slug_prefix, after_session=False):
        # type: (str, bool) -> None
        """Stop a market (collector + workers)."""
        slug_key = slug_prefix.rstrip("-")

        if after_session:
            # Signal collector to stop after current session finishes
            self._signal_collector_stop_after_session(slug_key)
            # Mark runner for deferred cleanup
            with self._lock:
                runner = self._markets.get(slug_key)
            if runner:
                runner._stop_after_session = True
            logger.info("Market %s will stop after current session.", slug_key)
        else:
            # Kill workers immediately
            with self._lock:
                runner = self._markets.pop(slug_key, None)
            if runner:
                runner.request_stop()

            # Stop collector
            self._stop_collector(slug_key)
            logger.info("Market %s stopped.", slug_key)

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
            "optimizer": self.get_optimizer_status(),
        }

    # ----- Optimizer management (unchanged) -----

    def start_optimizer(self, interval=30, eval_sessions=30, min_sharpe=0.2, starting_cash=100.0,
                        max_edge_iters=2):
        # type: (int, int, float, float, int) -> Dict[str, Any]
        """Start the optimizer subprocess."""
        if self._optimizer_process is not None and self._optimizer_process.poll() is None:
            return {"error": "Optimizer already running (PID {})".format(self._optimizer_process.pid)}

        from core.defaults import OPTIMIZER_DIR
        optimizer_dir = os.path.join(BASE_DIR, "data", "optimizer")
        os.makedirs(optimizer_dir, exist_ok=True)
        log_file = os.path.join(optimizer_dir, "optimizer.log")
        log_fh = open(log_file, "a")

        cmd = [
            sys.executable, "-m", "app.optimizer",
            "--port", str(self.port),
            "--interval", str(interval),
            "--eval-sessions", str(eval_sessions),
            "--min-sharpe", str(min_sharpe),
            "--cash", str(starting_cash),
            "--max-edge-iters", str(max_edge_iters),
        ]

        process = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=os.path.join(BASE_DIR),
            start_new_session=True,
        )
        self._optimizer_process = process
        self._optimizer_log_fh = log_fh
        logger.info("Optimizer started (PID %d)", process.pid)
        return {"ok": True, "pid": process.pid}

    def stop_optimizer(self):
        # type: () -> Dict[str, Any]
        """Stop the optimizer subprocess."""
        if self._optimizer_process is None or self._optimizer_process.poll() is not None:
            return {"error": "Optimizer not running"}
        pid = self._optimizer_process.pid
        try:
            os.kill(pid, signal.SIGTERM)
            self._optimizer_process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Failed to stop optimizer gracefully: %s", e)
            try:
                self._optimizer_process.kill()
            except OSError:
                pass
        self._optimizer_process = None
        if self._optimizer_log_fh is not None:
            try:
                self._optimizer_log_fh.close()
            except OSError:
                pass
            self._optimizer_log_fh = None
        logger.info("Optimizer stopped (PID %d)", pid)
        return {"ok": True, "stopped_pid": pid}

    def get_optimizer_status(self):
        # type: () -> Dict[str, Any]
        """Get optimizer status."""
        alive = (self._optimizer_process is not None and
                 self._optimizer_process.poll() is None)
        result = {
            "running": alive,
            "pid": self._optimizer_process.pid if alive else None,
        }
        # Read status file
        from core.defaults import OPTIMIZER_DIR
        status_file = os.path.join(OPTIMIZER_DIR, "status.json")
        try:
            with open(status_file, "r") as f:
                result.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
        result["running"] = alive  # override with actual process status
        return result

    # ----- Lifecycle -----

    def request_stop(self):
        # type: () -> None
        """Stop workers and optimizer. Collectors keep running."""
        logger.info("App stop requested. Collectors will keep running.")
        self.running = False
        self.stop_optimizer()
        with self._lock:
            runners = list(self._markets.values())
        for runner in runners:
            runner.request_stop()

    def request_stop_after_session(self):
        # type: () -> None
        """Stop all markets after their current sessions finish."""
        logger.info("App kill requested — stopping all markets after session.")
        with self._lock:
            for slug_key, runner in self._markets.items():
                self._signal_collector_stop_after_session(slug_key)
                runner._stop_after_session = True

    def run(self):
        # type: () -> None
        """Block on main thread until stopped. Handles signals."""
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        try:
            while self.running:
                time.sleep(1)
                self._cleanup_stopped_collectors()
        finally:
            self._shutdown()

    def _cleanup_stopped_collectors(self):
        # type: () -> None
        """Clean up markets whose collectors have stopped (after-session)."""
        with self._lock:
            to_remove = []
            for slug_key, runner in self._markets.items():
                if runner._stop_after_session:
                    pid = self._read_collector_pid(slug_key)
                    if pid is None:
                        # Collector has stopped — kill workers and clean up
                        runner.request_stop()
                        to_remove.append(slug_key)
            for slug_key in to_remove:
                self._markets.pop(slug_key, None)
                logger.info("Collector %s stopped after session, cleaned up workers.", slug_key)

    def _handle_shutdown(self, signum, frame):
        # type: (int, Any) -> None
        logger.info("Shutdown signal received.")
        self.request_stop()

    def _shutdown(self):
        # type: () -> None
        """Clean shutdown — stop all workers, close resources.
        Collectors are NOT stopped — they survive app restarts.
        """
        with self._lock:
            runners = list(self._markets.values())

        for runner in runners:
            runner.request_stop()

        # Stop optimizer
        if self._optimizer_process is not None and self._optimizer_process.poll() is None:
            try:
                os.kill(self._optimizer_process.pid, signal.SIGTERM)
                self._optimizer_process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._optimizer_process.kill()
                except OSError:
                    pass

        if self._optimizer_log_fh is not None:
            try:
                self._optimizer_log_fh.close()
            except OSError:
                pass
            self._optimizer_log_fh = None

        logger.info("AppManager shutdown complete. Collectors still running.")

    # ----- State persistence -----

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
            except OSError:
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
                    window_duration=mc.get("window_duration"),
                    market_type=mc.get("market_type", "updown"),
                )
                runner.restore_strategies()
                logger.info("Restored market: %s", slug_prefix)
            except Exception:
                logger.exception("Failed to restore market '%s'", slug_prefix)
