"""Bot worker process — runs a single strategy independently.

Connects to the supervisor's tick broadcast socket and processes ticks
using the shared core/ trading logic. Each worker has its own Position,
Executor, and Trader instance.

Lifecycle:
  1. Connect to tick socket
  2. Wait for session_start message
  3. Initialize Trader on first tick (gets initial BTC price)
  4. Process ticks via Trader.process_tick()
  5. On session_end: resolve, save state, loop back to 2

Spawned and managed by the MarketRunner. Can be killed/restarted
independently without affecting the data stream.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from core.defaults import BASE_DIR
from bots import ALL_STRATEGIES
from core.trader import ClobExecutor, Position, SimExecutor, Trade, Trader
from core.tick_broadcast import TickSubscriber

logger = logging.getLogger(__name__)


def _trade_to_dict(trade):
    # type: (Trade) -> Dict[str, Any]
    d = {
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
    if trade.fee is not None:
        d["fee"] = round(trade.fee, 6)
    if trade.requested_tokens is not None:
        d["requested_tokens"] = round(trade.requested_tokens, 4)
    return d


def _save_position(position_dir, slot_name, position):
    # type: (str, str, Position) -> None
    os.makedirs(position_dir, exist_ok=True)
    filepath = os.path.join(position_dir, "{}.json".format(slot_name))
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(position.to_dict(), f)
    os.replace(tmp, filepath)


class BotWorker:
    """Standalone bot worker that runs one strategy via the tick stream."""

    def __init__(
        self,
        strategy_name,       # type: str
        params,              # type: Dict[str, Any]
        socket_path,         # type: str
        cash,                # type: float
        market_dir,          # type: str
        dry_run=False,       # type: bool
        session_cap=None,    # type: Optional[float]
        session_cash_ratio=None,  # type: Optional[Dict[str, float]]
        per_session=False,   # type: bool
        window_duration=300, # type: int
    ):
        self.strategy_name = strategy_name
        self.params = params
        self.socket_path = socket_path
        self.cash = cash
        self.market_dir = market_dir
        self.dry_run = dry_run
        self.session_cap = session_cap
        self.session_cash_ratio = session_cash_ratio
        self.per_session = per_session
        self.window_duration = window_duration
        self.running = True

        # Per-market paths
        self._workers_dir = os.path.join(market_dir, "workers")
        self._bot_trades_file = os.path.join(market_dir, "bot_trades.jsonl")
        self._bot_session_log = os.path.join(market_dir, "bot_log.jsonl")
        self._position_dir = os.path.join(market_dir, "bot_positions")

        # Position and cash
        self.position = Position(cash=cash)
        self.starting_cash = cash
        self._session_reserve = 0.0
        self.session_count = 0

        # Strategy
        if strategy_name not in ALL_STRATEGIES:
            raise ValueError("Unknown strategy '{}'. Available: {}".format(
                strategy_name, list(ALL_STRATEGIES.keys()),
            ))
        self.strategy = ALL_STRATEGIES[strategy_name](**params)

        # Executor
        self._private_key = None  # type: Optional[str]
        self._safe_address = None  # type: Optional[str]
        if dry_run:
            self.executor = SimExecutor(execution_mode="book")
        else:
            self._init_clob_executor()

        # Worker state directory
        self.state_dir = os.path.join(self._workers_dir, strategy_name)
        os.makedirs(self.state_dir, exist_ok=True)

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("[%s] Shutdown signal received.", self.strategy_name)
        self.running = False

    def _init_clob_executor(self):
        # type: () -> None
        from app.auth import get_auth, build_clob_client

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in .env")
        auth = get_auth(private_key)
        client = build_clob_client(private_key, auth)
        self._private_key = private_key
        self._safe_address = auth["safe_address"]
        self.executor = ClobExecutor(client, trade_log_file=self._bot_trades_file)

    def save_state(self):
        # type: () -> None
        """Write worker state to disk for supervisor to read."""
        bankroll = self.position.cash + self._session_reserve
        state = {
            "strategy": self.strategy_name,
            "version": getattr(self.strategy, "version", None),
            "params": self.params,
            "cash": round(bankroll, 4),
            "dry_run": self.dry_run,
            "session_cap": self.session_cap,
            "session_cash_ratio": self.session_cash_ratio,
            "per_session": self.per_session,
            "session_count": self.session_count,
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        filepath = os.path.join(self.state_dir, "state.json")
        tmp = filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, filepath)

    def _reload_session_config(self):
        # type: () -> None
        """Re-read cash, session_cash_ratio, and session_cap from state.json.

        This allows set-cash to update these values while the worker is running.
        """
        filepath = os.path.join(self.state_dir, "state.json")
        try:
            with open(filepath, "r") as f:
                state = json.load(f)
            new_cash = state.get("cash")
            new_ratio = state.get("session_cash_ratio")
            new_cap = state.get("session_cap")
            changed = False
            if new_cash is not None:
                bankroll = self.position.cash + self._session_reserve
                if abs(new_cash - bankroll) > 0.001:
                    self.position.cash = new_cash
                    self._session_reserve = 0.0
                    logger.info(
                        "[%s] Reloaded cash: $%.2f -> $%.2f",
                        self.strategy_name, bankroll, new_cash,
                    )
                    changed = True
            if new_ratio != self.session_cash_ratio or new_cap != self.session_cap:
                self.session_cash_ratio = new_ratio
                self.session_cap = new_cap
                logger.info(
                    "[%s] Reloaded session config: ratio=%s cap=%s",
                    self.strategy_name, self.session_cash_ratio, self.session_cap,
                )
                changed = True
        except (OSError, json.JSONDecodeError):
            pass

    def restore_state(self):
        # type: () -> None
        """Restore session count from last run.

        Cash is NOT restored here — the MarketRunner already handles cash
        restoration when ``--cash`` is not explicitly provided.  Restoring
        cash here would overwrite an explicit ``--cash`` value passed via
        ``trade-on``.
        """
        filepath = os.path.join(self.state_dir, "state.json")
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r") as f:
                state = json.load(f)
            self.session_count = state.get("session_count", 0)
            logger.info(
                "[%s] Restored session_count=%d (cash=$%.2f from launcher)",
                self.strategy_name, self.session_count, self.position.cash,
            )
        except Exception as e:
            logger.warning("[%s] Failed to restore state: %s", self.strategy_name, e)

    def run(self):
        # type: () -> None
        """Main loop — connect to tick stream, process sessions."""
        subscriber = TickSubscriber(self.socket_path)
        logger.info(
            "[%s] Bot worker started (PID %d, cash=$%.2f, dry_run=%s, market_dir=%s)",
            self.strategy_name, os.getpid(), self.position.cash, self.dry_run,
            self.market_dir,
        )

        trader = None  # type: Optional[Trader]
        metadata_dict = None  # type: Optional[Dict[str, Any]]
        trade_dicts = []  # type: List[Dict[str, Any]]
        trade_count = 0
        trader_initialized = False
        skip_session = False

        for msg in subscriber.messages(is_running=lambda: self.running):
            if not self.running:
                break

            msg_type = msg.get("type")

            if msg_type == "session_start":
                metadata_dict = msg.get("metadata")
                trader = None
                trader_initialized = False
                trade_dicts = []
                trade_count = 0
                skip_session = False

                # If this is a replayed session_start (mid-session join) and
                # the strategy needs cumulative data from tick 0, wait for the
                # next fresh session.
                if msg.get("replayed") and self.strategy.requires_full_session:
                    logger.info(
                        "[%s] Mid-session join skipped (requires_full_session=True), "
                        "waiting for next session",
                        self.strategy_name,
                    )
                    skip_session = True
                    metadata_dict = None
                    continue

                self._reload_session_config()

                if self.per_session:
                    self.position.cash = self.cash
                    self.starting_cash = self.cash

                self._session_reserve = 0.0
                if self.session_cash_ratio is not None:
                    ratio = self.session_cash_ratio["ratio"]
                    cap = self.session_cash_ratio["max"]
                    deploy = min(self.position.cash * ratio, cap)
                    deploy = round(deploy, 4)
                    if self.position.cash > deploy:
                        self._session_reserve = self.position.cash - deploy
                        self.position.cash = deploy
                        logger.info(
                            "[%s] Session ratio %.0f%% up to $%.2f: deploying $%.2f, reserve $%.2f",
                            self.strategy_name, ratio * 100, cap,
                            self.position.cash, self._session_reserve,
                        )
                elif self.session_cap is not None and self.position.cash > self.session_cap:
                    self._session_reserve = self.position.cash - self.session_cap
                    self.position.cash = self.session_cap
                    logger.info(
                        "[%s] Session cap $%.2f: deploying $%.2f, reserve $%.2f",
                        self.strategy_name, self.session_cap,
                        self.position.cash, self._session_reserve,
                    )

                self.starting_cash = self.position.cash + self._session_reserve
                self.save_state()
                logger.info(
                    "[%s] Session starting: cash=$%.2f (reserve=$%.2f)",
                    self.strategy_name, self.position.cash, self._session_reserve,
                )

            elif msg_type == "tick":
                if metadata_dict is None:
                    continue

                sample_dict = msg.get("sample")
                if sample_dict is None:
                    continue

                if not trader_initialized:
                    initial_btc_price = sample_dict.get("btc_price_usd") or 0.0
                    self.strategy = ALL_STRATEGIES[self.strategy_name](**self.params)
                    auto_exit_flag = os.path.join(self.state_dir, "auto_exit.flag")
                    trader = Trader(
                        self.strategy, self.executor,
                        auto_exit_flag_file=auto_exit_flag,
                    )
                    ref_balance = self.position.cash
                    trader.start_session(
                        metadata_dict, self.position, initial_btc_price,
                        self.window_duration, reference_balance=ref_balance,
                        capture_decision_log=True,
                    )
                    trader_initialized = True

                try:
                    trade = trader.process_tick(sample_dict)
                except Exception as e:
                    logger.warning("[%s] Tick error: %s", self.strategy_name, e)
                    continue

                for fill in trader.last_tick_fills:
                    trade_dicts.append(_trade_to_dict(fill))
                    trade_count += 1
                    if fill.success is True and not self.dry_run:
                        _save_position(self._position_dir, self.strategy_name, self.position)
                    logger.info(
                        "[%s] Limit fill: %s @ %.2f x %.2f | order_id=%s",
                        self.strategy_name, fill.side, fill.price, fill.tokens,
                        fill.order_id,
                    )

                if trade is not None:
                    trade_dicts.append(_trade_to_dict(trade))
                    if trade.success is not False:
                        trade_count += 1
                        if trade.success is True and not self.dry_run:
                            _save_position(self._position_dir, self.strategy_name, self.position)
                        logger.info(
                            "[%s] Trade executed: %s @ %.2f x %.2f | order_id=%s",
                            self.strategy_name, trade.side, trade.price,
                            trade.tokens, trade.order_id,
                        )
                    else:
                        logger.warning(
                            "[%s] Trade failed: %s", self.strategy_name, trade.error,
                        )

                self._write_live_trades(trade_dicts, trade_count)

            elif msg_type == "session_end":
                if trader is not None:
                    cancelled = trader.cancel_all_pending()
                    if cancelled > 0:
                        logger.info(
                            "[%s] Cancelled %d pending orders at session end",
                            self.strategy_name, cancelled,
                        )
                        if not self.dry_run:
                            _save_position(self._position_dir, self.strategy_name, self.position)

                    outcome = msg.get("outcome")
                    session_filepath = msg.get("session_filepath")
                    self._resolve(
                        outcome, trade_count, trade_dicts, trader, session_filepath,
                    )

                trader = None
                metadata_dict = None
                self.session_count += 1
                self.save_state()

        # Cleanup: cancel any pending GTC orders left from an interrupted session
        if trader is not None:
            try:
                cancelled = trader.cancel_all_pending()
                if cancelled > 0:
                    logger.info(
                        "[%s] Cancelled %d pending orders on shutdown",
                        self.strategy_name, cancelled,
                    )
                    if not self.dry_run:
                        _save_position(
                            self._position_dir, self.strategy_name,
                            self.position,
                        )
            except Exception as e:
                logger.error(
                    "[%s] Failed to cancel orders on shutdown: %s",
                    self.strategy_name, e,
                )
            self.save_state()

        subscriber.close()
        logger.info("[%s] Bot worker stopped.", self.strategy_name)

    def _resolve(self, outcome, trade_count, trade_dicts, trader, session_filepath):
        # type: (Optional[str], int, List[Dict[str, Any]], Trader, Optional[str]) -> None
        if outcome == "Up":
            credit = self.position.up_tokens
        elif outcome == "Down":
            credit = self.position.down_tokens
        else:
            credit = 0.0

        self.position.up_tokens = 0.0
        self.position.down_tokens = 0.0

        if self._session_reserve > 0:
            self.position.cash += self._session_reserve
            logger.info(
                "[%s] Restored reserve $%.2f -> total cash $%.2f",
                self.strategy_name, self._session_reserve, self.position.cash,
            )
            self._session_reserve = 0.0

        if credit > 0:
            old = self.position.cash
            self.position.cash += credit
            logger.info(
                "[%s] Resolution credit: $%.4f -> $%.4f (+$%.4f) [%s]",
                self.strategy_name, old, self.position.cash, credit, outcome,
            )

        pnl = self.position.cash - self.starting_cash
        logger.info(
            "[%s] Session resolved: %s | Trades: %d | Cash: $%.2f | PnL: $%.2f",
            self.strategy_name, outcome, trade_count, self.position.cash, pnl,
        )

        strategy_version = getattr(self.strategy, "version", None)
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": self.strategy_name,
            "strategy_version": strategy_version,
            "outcome": outcome,
            "trades": trade_count,
            "cash": round(self.position.cash, 4),
            "pnl": round(pnl, 4),
            "credit": round(credit, 4),
            "session_number": self.session_count,
        }
        try:
            with open(self._bot_session_log, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception as e:
            logger.warning("Failed to log session: %s", e)

        resolution_record = {
            "type": "resolution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": self.strategy_name,
            "strategy_version": strategy_version,
            "outcome": outcome,
            "starting_cash": round(self.starting_cash, 4),
            "final_cash": round(self.position.cash, 4),
            "pnl": round(pnl, 4),
            "credit": round(credit, 4),
            "trades": trade_count,
            "up_tokens": round(self.position.up_tokens, 4),
            "down_tokens": round(self.position.down_tokens, 4),
            "dry_run": self.dry_run,
        }
        try:
            with open(self._bot_trades_file, "a") as f:
                f.write(json.dumps(resolution_record) + "\n")
        except Exception as e:
            logger.warning("Failed to log resolution: %s", e)

        if session_filepath and trade_count > 0:
            sidecar_path = "{}.trading.{}.json".format(
                session_filepath, self.strategy_name,
            )
            try:
                sidecar = {
                    "strategy": self.strategy_name,
                    "strategy_version": strategy_version,
                    "params": self.params,
                    "dry_run": self.dry_run,
                    "starting_cash": round(self.starting_cash, 4),
                    "final_cash": round(self.position.cash, 4),
                    "pnl": round(pnl, 4),
                    "outcome": outcome,
                    "trades": trade_dicts,
                }
                if trader and trader.decision_log:
                    sidecar["decision_log"] = trader.decision_log
                with open(sidecar_path, "w") as f:
                    json.dump(sidecar, f, indent=2)
                logger.info("Trading sidecar saved: %s", sidecar_path)
            except Exception as e:
                logger.warning("Failed to save sidecar: %s", e)

        # Auto-redemption disabled — handled by separate claim server
        # if trade_count > 0 and not self.dry_run and self._private_key:
        #     try:
        #         from app.claim import has_builder_creds, claim_background
        #         if has_builder_creds():
        #             claim_background(self._private_key, self._safe_address)
        #     except Exception:
        #         pass

        live_path = os.path.join(self.state_dir, "live_trades.json")
        try:
            os.remove(live_path)
        except OSError:
            pass

    def _write_live_trades(self, trade_dicts, trade_count):
        # type: (List[Dict[str, Any]], int) -> None
        filepath = os.path.join(self.state_dir, "live_trades.json")
        try:
            with open(filepath, "w") as f:
                bankroll = self.position.cash + self._session_reserve
                json.dump({
                    "strategy": self.strategy_name,
                    "strategy_version": getattr(self.strategy, "version", None),
                    "dry_run": self.dry_run,
                    "cash": round(bankroll, 4),
                    "up_tokens": round(self.position.up_tokens, 4),
                    "down_tokens": round(self.position.down_tokens, 4),
                    "starting_cash": round(self.starting_cash, 4),
                    "trade_count": trade_count,
                    "trades": trade_dicts[-50:],
                }, f)
        except Exception:
            pass


def _parse_params(params_str):
    # type: (str) -> Dict[str, Any]
    params = {}  # type: Dict[str, Any]
    if not params_str:
        return params
    for pair in params_str.split(","):
        k, v = pair.split("=", 1)
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        params[k.strip()] = v
    return params


def main():
    parser = argparse.ArgumentParser(description="Bot Worker Process")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--params", default="")
    parser.add_argument("--socket", required=True, help="Tick broadcast socket path")
    parser.add_argument("--market-dir", required=True, help="Per-market data directory")
    parser.add_argument("--cash", type=float, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--session-cap", type=float, default=None)
    parser.add_argument("--session-cash-ratio", default=None,
                        help="ratio:max e.g. 0.25:3")
    parser.add_argument("--per-session", action="store_true")
    parser.add_argument("--window", type=int, default=300)
    parser.add_argument("--restore", action="store_true", help="Restore state from disk")

    args = parser.parse_args()
    params = _parse_params(args.params)

    # Ensure worker log directory exists under market dir
    log_dir = os.path.join(args.market_dir, "workers", args.strategy)
    os.makedirs(log_dir, exist_ok=True)

    # Worker stdout/stderr is redirected to worker.log by the parent process,
    # so only use the RotatingFileHandler here (no StreamHandler) to avoid
    # every log line appearing twice in the file.
    log_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "worker.log"),
        maxBytes=5 * 1024 * 1024, backupCount=2,
    )
    log_handler.setLevel(logging.DEBUG)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[log_handler],
    )

    session_cash_ratio = None
    if args.session_cash_ratio:
        parts = args.session_cash_ratio.split(":")
        session_cash_ratio = {"ratio": float(parts[0]), "max": float(parts[1])}

    worker = BotWorker(
        strategy_name=args.strategy,
        params=params,
        socket_path=args.socket,
        cash=args.cash,
        market_dir=args.market_dir,
        dry_run=args.dry_run,
        session_cap=args.session_cap,
        session_cash_ratio=session_cash_ratio,
        per_session=args.per_session,
        window_duration=args.window,
    )

    if args.restore:
        worker.restore_state()

    worker.save_state()
    worker.run()


if __name__ == "__main__":
    main()
