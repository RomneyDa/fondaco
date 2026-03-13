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
import os
import signal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from core.defaults import TOKEN_DECIMALS
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
        "amount": round(trade.amount, TOKEN_DECIMALS),
        "tokens": round(trade.tokens, TOKEN_DECIMALS),
        "success": trade.success,
        "order_id": trade.order_id,
        "error": trade.error,
        "timestamp": trade.timestamp,
    }
    if trade.fee is not None:
        d["fee"] = round(trade.fee, 6)
    if trade.requested_tokens is not None:
        d["requested_tokens"] = round(trade.requested_tokens, TOKEN_DECIMALS)
    return d



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
        window_duration=300,  # type: int  # always passed by MarketRunner
    ):
        self.strategy_name = strategy_name
        self.params = params
        self.socket_path = socket_path
        self.bankroll = cash
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
        self.executor = ClobExecutor(client, trade_log_file=self._bot_trades_file, strategy_name=self.strategy_name)

    def save_state(self):
        # type: () -> None
        """Write worker state to disk atomically.

        ``bankroll`` is the single source of truth for total funds.
        Any pending config updates (from set-cash) are preserved so
        they aren't lost if save_state runs before the worker consumes them.
        """
        filepath = os.path.join(self.state_dir, "state.json")

        # Preserve pending config updates written by the control server
        pending = None
        try:
            with open(filepath, "r") as f:
                existing = json.load(f)
            pending = existing.get("pending")
        except (OSError, json.JSONDecodeError):
            pass

        state = {
            "strategy": self.strategy_name,
            "version": getattr(self.strategy, "version", None),
            "params": self.params,
            "bankroll": round(self.bankroll, TOKEN_DECIMALS),
            "dry_run": self.dry_run,
            "session_cap": self.session_cap,
            "session_cash_ratio": self.session_cash_ratio,
            "per_session": self.per_session,
            "session_count": self.session_count,
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if pending is not None:
            state["pending"] = pending

        tmp = filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, filepath)

    def _check_config_updates(self):
        # type: () -> None
        """Apply pending config updates embedded in state.json by set-cash.

        The control server writes a ``pending`` sub-object into state.json.
        We read it, apply it, and the next ``save_state()`` clears it
        (by not preserving a consumed pending).
        """
        filepath = os.path.join(self.state_dir, "state.json")
        try:
            with open(filepath, "r") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        pending = state.get("pending")
        if not pending:
            return

        new_bankroll = pending.get("bankroll")
        if new_bankroll is not None:
            old = self.bankroll
            self.bankroll = new_bankroll
            self.position.cash = new_bankroll
            self._session_reserve = 0.0
            logger.info(
                "[%s] Config update: bankroll $%.2f -> $%.2f",
                self.strategy_name, old, new_bankroll,
            )
        if "session_cash_ratio" in pending or "session_cap" in pending:
            self.session_cash_ratio = pending.get("session_cash_ratio")
            self.session_cap = pending.get("session_cap")
            logger.info(
                "[%s] Config update: ratio=%s cap=%s",
                self.strategy_name, self.session_cash_ratio, self.session_cap,
            )

        # Clear pending by writing state without it
        state.pop("pending", None)
        state["bankroll"] = round(self.bankroll, TOKEN_DECIMALS)
        state["session_cash_ratio"] = self.session_cash_ratio
        state["session_cap"] = self.session_cap
        tmp = filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, filepath)

    def restore_state(self):
        # type: () -> None
        """Restore session count from last run.

        Bankroll is NOT restored here — the MarketRunner reads ``bankroll``
        from state.json and passes it as ``--cash`` when spawning the worker.
        Restoring it here would overwrite an explicit ``--cash`` value.
        """
        filepath = os.path.join(self.state_dir, "state.json")
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r") as f:
                state = json.load(f)
            self.session_count = state.get("session_count", 0)
            logger.info(
                "[%s] Restored session_count=%d (bankroll=$%.2f from launcher)",
                self.strategy_name, self.session_count, self.bankroll,
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("[%s] Failed to restore state: %s", self.strategy_name, e)

    def run(self):
        # type: () -> None
        """Main loop — connect to tick stream, process sessions."""
        subscriber = TickSubscriber(self.socket_path)
        logger.info(
            "[%s] Bot worker started (PID %d, bankroll=$%.2f, dry_run=%s, market_dir=%s)",
            self.strategy_name, os.getpid(), self.bankroll, self.dry_run,
            self.market_dir,
        )

        trader = None  # type: Optional[Trader]
        metadata_dict = None  # type: Optional[Dict[str, Any]]
        trade_dicts = []  # type: List[Dict[str, Any]]
        trade_count = 0
        trader_initialized = False

        for msg in subscriber.messages(is_running=lambda: self.running):
            if not self.running:
                break

            msg_type = msg.get("type")

            if msg_type == "session_start":
                is_replayed = msg.get("replayed", False)

                # Mid-session rejoin with active trader — preserve everything.
                # Resetting trader would orphan pending GTC orders and lose
                # cash reserved for them.
                if is_replayed and trader is not None and not self.strategy.requires_full_session:
                    logger.info(
                        "[%s] Mid-session rejoin: active trader preserved "
                        "(cash=$%.2f, reserve=$%.2f, up=%.2f, down=%.2f)",
                        self.strategy_name, self.position.cash,
                        self._session_reserve, self.position.up_tokens,
                        self.position.down_tokens,
                    )
                    continue

                # Cancel pending orders from the outgoing trader before
                # resetting, so reserved cash is returned to position.
                if trader is not None:
                    try:
                        cancelled = trader.cancel_all_pending()
                        if cancelled > 0:
                            logger.info(
                                "[%s] Cancelled %d pending orders before session reset",
                                self.strategy_name, cancelled,
                            )
                    except Exception as e:
                        logger.warning(
                            "[%s] Failed to cancel pending orders: %s",
                            self.strategy_name, e,
                        )

                metadata_dict = msg.get("metadata")
                trader = None
                trader_initialized = False
                trade_dicts = []
                trade_count = 0

                # On mid-session rejoin, restore trade state from previous
                # live_trades.json so sidecars and trade counts stay accurate.
                if is_replayed:
                    restored = self._restore_live_trades()
                    if restored is not None:
                        trade_dicts = restored
                        trade_count = len(trade_dicts)
                        if trade_count > 0:
                            logger.info(
                                "[%s] Restored %d trades from previous live_trades.json",
                                self.strategy_name, trade_count,
                            )

                # If this is a replayed session_start (mid-session join) and
                # the strategy needs cumulative data from tick 0, wait for the
                # next fresh session.
                if is_replayed and self.strategy.requires_full_session:
                    logger.info(
                        "[%s] Mid-session join skipped (requires_full_session=True), "
                        "waiting for next session",
                        self.strategy_name,
                    )
                    metadata_dict = None
                    continue

                self._check_config_updates()

                # Reset position to bankroll at session boundary.
                # This is the core invariant: bankroll is the single
                # source of truth, position is derived from it.
                self.position.cash = self.bankroll
                self.position.up_tokens = 0.0
                self.position.down_tokens = 0.0
                self._session_reserve = 0.0
                if self.session_cash_ratio is not None:
                    ratio = self.session_cash_ratio["ratio"]
                    cap = self.session_cash_ratio["max"]
                    deploy = min(self.position.cash * ratio, cap)
                    deploy = round(deploy, TOKEN_DECIMALS)
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

                self.starting_cash = self.bankroll
                self.save_state()
                logger.info(
                    "[%s] Session starting: bankroll=$%.2f deploy=$%.2f reserve=$%.2f",
                    self.strategy_name, self.bankroll,
                    self.position.cash, self._session_reserve,
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
                    logger.info(
                        "[%s] Limit fill: %s @ %.2f x %.2f | order_id=%s",
                        self.strategy_name, fill.side, fill.price, fill.tokens,
                        fill.order_id,
                    )

                if trade is not None:
                    trade_dicts.append(_trade_to_dict(trade))
                    if trade.success is not False:
                        trade_count += 1
                        logger.info(
                            "[%s] Trade executed: %s @ %.2f x %.2f | order_id=%s",
                            self.strategy_name, trade.side, trade.price,
                            trade.tokens, trade.order_id,
                        )
                    else:
                        logger.warning(
                            "[%s] Trade failed: %s", self.strategy_name, trade.error,
                        )

                self._write_live_trades(trade_dicts, trade_count, trader=trader)

            elif msg_type == "session_end":
                if trader is not None:
                    cancelled = trader.cancel_all_pending()
                    if cancelled > 0:
                        logger.info(
                            "[%s] Cancelled %d pending orders at session end",
                            self.strategy_name, cancelled,
                        )

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

        # Update bankroll to reflect post-resolution cash
        self.bankroll = self.position.cash

        pnl = self.bankroll - self.starting_cash
        logger.info(
            "[%s] Session resolved: %s | Trades: %d | Bankroll: $%.2f | PnL: $%.2f",
            self.strategy_name, outcome, trade_count, self.bankroll, pnl,
        )

        strategy_version = getattr(self.strategy, "version", None)
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": self.strategy_name,
            "strategy_version": strategy_version,
            "outcome": outcome,
            "trades": trade_count,
            "cash": round(self.bankroll, TOKEN_DECIMALS),
            "pnl": round(pnl, TOKEN_DECIMALS),
            "credit": round(credit, TOKEN_DECIMALS),
            "session_number": self.session_count,
            "dry_run": self.dry_run,
        }
        try:
            with open(self._bot_session_log, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except OSError as e:
            logger.warning("Failed to log session: %s", e)

        resolution_record = {
            "type": "resolution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": self.strategy_name,
            "strategy_version": strategy_version,
            "outcome": outcome,
            "starting_cash": round(self.starting_cash, TOKEN_DECIMALS),
            "final_cash": round(self.bankroll, TOKEN_DECIMALS),
            "pnl": round(pnl, TOKEN_DECIMALS),
            "credit": round(credit, TOKEN_DECIMALS),
            "trades": trade_count,
            "dry_run": self.dry_run,
        }
        try:
            with open(self._bot_trades_file, "a") as f:
                f.write(json.dumps(resolution_record) + "\n")
        except OSError as e:
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
                    "starting_cash": round(self.starting_cash, TOKEN_DECIMALS),
                    "final_cash": round(self.bankroll, TOKEN_DECIMALS),
                    "pnl": round(pnl, TOKEN_DECIMALS),
                    "outcome": outcome,
                    "trades": trade_dicts,
                }
                if trader and trader.decision_log:
                    sidecar["decision_log"] = trader.decision_log
                with open(sidecar_path, "w") as f:
                    json.dump(sidecar, f, indent=2)
                logger.info("Trading sidecar saved: %s", sidecar_path)
            except OSError as e:
                logger.warning("Failed to save sidecar: %s", e)

        # Auto-claim disabled — relayer rate limit (~100/day) means claims
        # are batched by the separate claim-server on a 15-min interval.
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

    def _restore_live_trades(self):
        # type: () -> Optional[List[Dict[str, Any]]]
        """Restore trade_dicts from live_trades.json written by previous worker instance."""
        filepath = os.path.join(self.state_dir, "live_trades.json")
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            if trades:
                return trades
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return None

    def _write_live_trades(self, trade_dicts, trade_count, trader=None):
        # type: (List[Dict[str, Any]], int, Optional[Any]) -> None
        filepath = os.path.join(self.state_dir, "live_trades.json")
        try:
            pending = []
            if trader is not None:
                for o in trader.open_orders:
                    pending.append({
                        "order_id": o.order_id,
                        "side": o.side,
                        "token": o.token,
                        "price": o.price,
                        "tokens": o.tokens,
                    })
            with open(filepath, "w") as f:
                json.dump({
                    "strategy": self.strategy_name,
                    "strategy_version": getattr(self.strategy, "version", None),
                    "dry_run": self.dry_run,
                    "bankroll": round(self.bankroll, TOKEN_DECIMALS),
                    "up_tokens": round(self.position.up_tokens, TOKEN_DECIMALS),
                    "down_tokens": round(self.position.down_tokens, TOKEN_DECIMALS),
                    "starting_cash": round(self.starting_cash, TOKEN_DECIMALS),
                    "trade_count": trade_count,
                    "trades": trade_dicts[-50:],
                    "pending_orders": pending,
                }, f)
        except OSError:
            pass


def _parse_params(params_str):
    # type: (str) -> Dict[str, Any]
    params = {}  # type: Dict[str, Any]
    if not params_str:
        return params
    for pair in params_str.split(","):
        k, v = pair.split("=", 1)
        v = v.strip()
        if v == "None":
            params[k.strip()] = None
            continue
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
    parser.add_argument("--window", type=int, required=True)
    parser.add_argument("--restore", action="store_true", help="Restore state from disk")

    args = parser.parse_args()
    params = _parse_params(args.params)

    # Ensure worker log directory exists under market dir
    log_dir = os.path.join(args.market_dir, "workers", args.strategy)
    os.makedirs(log_dir, exist_ok=True)

    # Worker stdout/stderr is redirected to worker.log by the parent process,
    # so disable the stderr StreamHandler to avoid every log line appearing
    # twice in the file.
    from core.logging_setup import setup_logging
    setup_logging(
        "bot_worker",
        log_file=os.path.join(log_dir, "worker.log"),
        include_stderr=False,
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
