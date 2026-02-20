"""Unified trading abstractions shared by the simulator and live bot.

Contains the core types (TradeAction, Position, TickContext), a unified Trade
record, an Executor ABC with simulation and CLOB implementations, and a Trader
class that drives the tick loop identically for both replay and live trading.
"""

import json
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.defaults import (
    CLOB_MIN_ORDER_TOKENS,
    MAX_LOSS_DOLLARS,
    MAX_LOSS_FRACTION,
    MAX_POSITION_DOLLARS,
    MAX_POSITION_FRACTION,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core types (moved from simulator.py)
# ---------------------------------------------------------------------------


@dataclass
class TradeAction:
    """Output from a strategy's on_tick method."""
    side: str  # buy_up, buy_down, sell_up, sell_down, hold
    amount: float = 0.0  # dollars for buys, token count for sells


HOLD = TradeAction(side="hold", amount=0.0)


@dataclass
class Position:
    """Current holdings.

    Provides shared trade execution math used by both the simulator
    (backtesting) and the live trading bot, ensuring identical rounding
    and position logic.
    """
    cash: float
    up_tokens: float = 0.0
    down_tokens: float = 0.0

    def compute_trade_size(self, side, price, amount):
        # type: (str, Optional[float], float) -> Tuple[float, float]
        """Compute tokens and dollar amount for a trade.

        For buys:  amount = dollars to spend.
                   Returns (tokens_to_buy, dollars_to_spend).
        For sells: amount = max tokens to sell.
                   Returns (tokens_to_sell, proceeds).

        Applies position constraints (can't spend more cash than held,
        can't sell more tokens than held).  Rounds to 4 decimals —
        round() for buys, floor for sells (conservative).

        Returns (0.0, 0.0) if the trade cannot execute.
        """
        if price is None or price <= 0 or price >= 1.0:
            return 0.0, 0.0

        if side in ("buy_up", "buy_down"):
            dollars = min(amount, self.cash)
            if dollars <= 0:
                return 0.0, 0.0
            tokens = round(dollars / price, 4)
            cost = tokens * price
            return tokens, cost

        if side in ("sell_up", "sell_down"):
            available = self.up_tokens if side == "sell_up" else self.down_tokens
            tokens = math.floor(min(amount, available) * 10000) / 10000
            if tokens <= 0:
                return 0.0, 0.0
            proceeds = tokens * price
            return tokens, proceeds

        return 0.0, 0.0

    def apply_trade(self, side, price, tokens):
        # type: (str, float, float) -> None
        """Mutate position for a completed trade.

        For buys:  deduct cost (tokens * price) from cash, add tokens.
        For sells: remove tokens, add proceeds (tokens * price) to cash.
        """
        if tokens <= 0:
            return
        if side == "buy_up":
            self.cash -= tokens * price
            self.up_tokens += tokens
        elif side == "buy_down":
            self.cash -= tokens * price
            self.down_tokens += tokens
        elif side == "sell_up":
            self.up_tokens -= tokens
            self.cash += tokens * price
        elif side == "sell_down":
            self.down_tokens -= tokens
            self.cash += tokens * price

        if self.cash < -0.01 or self.up_tokens < -0.0001 or self.down_tokens < -0.0001:
            logger.error(
                "POSITION INTEGRITY VIOLATION: cash=%.4f up=%.4f down=%.4f "
                "after %s %.4f tokens @ %.4f",
                self.cash, self.up_tokens, self.down_tokens, side, tokens, price,
            )

    def resolve(self, outcome):
        # type: (Optional[str]) -> None
        """Resolve position: winning tokens pay $1 each, losing pay $0.

        If outcome is None (timeout), position is left unchanged.
        """
        if outcome == "Up":
            self.cash += self.up_tokens
            self.up_tokens = 0.0
            self.down_tokens = 0.0
        elif outcome == "Down":
            self.cash += self.down_tokens
            self.up_tokens = 0.0
            self.down_tokens = 0.0

    def to_dict(self):
        # type: () -> Dict[str, float]
        return {"cash": self.cash, "up_tokens": self.up_tokens, "down_tokens": self.down_tokens}

    @classmethod
    def from_dict(cls, d):
        # type: (Dict[str, Any]) -> Position
        return cls(cash=d["cash"], up_tokens=d.get("up_tokens", 0.0), down_tokens=d.get("down_tokens", 0.0))


@dataclass
class TickContext:
    """Everything a strategy sees at each tick."""
    tick_index: int
    sample: Dict[str, Any]
    history: List[Dict[str, Any]]
    position: Position
    metadata: Dict[str, Any]
    initial_btc_price: float
    elapsed_fraction: float  # 0.0 to 1.0
    max_buy_amount: float = 0.0    # Max dollars a buy can spend (guardrail-aware)
    loss_limit_hit: bool = False    # True if loss limit reached (all buys blocked)


# ---------------------------------------------------------------------------
# Unified Trade record
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    """Record of an executed trade (simulation or live)."""
    tick_index: int
    side: str
    price: float
    amount: float      # dollars spent/received
    tokens: float      # tokens bought/sold
    # Live-only fields (None in simulation)
    order_id: Optional[str] = None
    token_id: Optional[str] = None
    success: Optional[bool] = None   # None=sim, True/False=live
    error: Optional[str] = None
    timestamp: Optional[str] = None
    requested_tokens: Optional[float] = None  # original requested amount (for partial fill audit)
    elapsed_seconds: Optional[float] = None   # seconds into the window when trade occurred


# ---------------------------------------------------------------------------
# Executor ABC
# ---------------------------------------------------------------------------


class Executor(ABC):
    """Abstract trade executor — pluggable backend for Trader."""

    @abstractmethod
    def execute_trade(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[Trade]
        """Execute a trade, mutating position on success.

        Returns a Trade on execution (success or failure), or None if
        there is nothing to do (hold, zero size).
        """


# ---------------------------------------------------------------------------
# SimExecutor — backtesting execution
# ---------------------------------------------------------------------------


class SimExecutor(Executor):
    """Executes trades against historical sample data for backtesting."""

    def __init__(self, execution_mode="midpoint_slippage", slippage=0.02):
        # type: (str, float) -> None
        self.execution_mode = execution_mode
        self.slippage = slippage

    def execute_trade(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[Trade]
        if action.side == "hold" or action.amount <= 0:
            return None

        price = self._get_execution_price(action.side, sample)
        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)
        if tokens <= 0:
            return None

        # Enforce same minimum order size as CLOB (mirrors live rejection)
        if tokens < CLOB_MIN_ORDER_TOKENS:
            return None

        position.apply_trade(action.side, price, tokens)
        return Trade(
            tick_index=tick_index,
            side=action.side,
            price=price,
            amount=dollars,
            tokens=tokens,
            elapsed_seconds=sample.get("elapsed_seconds"),
        )

    def _get_execution_price(self, side, sample):
        # type: (str, Dict[str, Any]) -> Optional[float]
        """Look up execution price based on mode and action side."""
        if self.execution_mode == "midpoint_slippage":
            table = {
                "buy_up": sample.get("up_token_midpoint"),
                "sell_up": sample.get("up_token_midpoint"),
                "buy_down": sample.get("down_token_midpoint"),
                "sell_down": sample.get("down_token_midpoint"),
            }
            base = table.get(side)
            if base is None:
                return None
            if side.startswith("buy"):
                return base + self.slippage
            else:
                return base - self.slippage

        elif self.execution_mode == "midpoint":
            table = {
                "buy_up": sample.get("up_token_midpoint"),
                "sell_up": sample.get("up_token_midpoint"),
                "buy_down": sample.get("down_token_midpoint"),
                "sell_down": sample.get("down_token_midpoint"),
            }
            return table.get(side)

        elif self.execution_mode == "book":
            table = {
                "buy_up": sample.get("up_token_best_ask"),
                "sell_up": sample.get("up_token_best_bid"),
                "buy_down": sample.get("down_token_best_ask"),
                "sell_down": sample.get("down_token_best_bid"),
            }
            return table.get(side)

        return None


# ---------------------------------------------------------------------------
# ClobExecutor — live CLOB order execution
# ---------------------------------------------------------------------------


class ClobExecutor(Executor):
    """Executes trades via Polymarket's CLOB API."""

    def __init__(self, clob_client, trade_log_file=None):
        # type: (Any, Optional[str]) -> None
        self.client = clob_client
        self.trade_log_file = trade_log_file

    def execute_trade(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[Trade]
        """Execute a trade action via the CLOB API.

        Maps the strategy's TradeAction to a CLOB order:
        - buy_up  -> BUY the Up token
        - buy_down -> BUY the Down token
        - sell_up  -> SELL the Up token
        - sell_down -> SELL the Down token

        Uses Position.compute_trade_size() for size calculation (shared
        with the backtesting simulator) so rounding behaviour is identical.
        Price = token midpoint from sample, rounded to 2 decimals.
        On success, mutates position via Position.apply_trade().
        """
        # Lazy-import so simulation doesn't require py_clob_client
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

        if action.side == "hold" or action.amount <= 0:
            return None

        now = datetime.now(timezone.utc).isoformat()

        # Map action side to token_id and order side
        side_map = {
            "buy_up": ("up_token_id", "BUY", "up_token_midpoint"),
            "buy_down": ("down_token_id", "BUY", "down_token_midpoint"),
            "sell_up": ("up_token_id", "SELL", "up_token_midpoint"),
            "sell_down": ("down_token_id", "SELL", "down_token_midpoint"),
        }

        if action.side not in side_map:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=0, amount=0,
                tokens=0, order_id=None, token_id="", success=False,
                error="Unknown side", timestamp=now,
            )
            self._log_trade(trade)
            return trade

        token_key, order_side, midpoint_key = side_map[action.side]
        token_id = metadata[token_key]
        midpoint = sample.get(midpoint_key)

        if midpoint is None or midpoint <= 0 or midpoint >= 1.0:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=0, amount=0,
                tokens=0, order_id=None, token_id=token_id, success=False,
                error="Invalid midpoint: {}".format(midpoint), timestamp=now,
            )
            self._log_trade(trade)
            return trade

        price = round(midpoint, 2)
        if price <= 0 or price >= 1.0:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=price, amount=0,
                tokens=0, order_id=None, token_id=token_id, success=False,
                error="Price out of range after rounding: {}".format(price),
                timestamp=now,
            )
            self._log_trade(trade)
            return trade

        # Shared size calculation (same logic as backtesting simulator)
        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)

        # CLOB constraint: for buys, maker amount (tokens * price) must have
        # <= 2 decimals.  Floor token count to the nearest integer to guarantee
        # a clean product (integer * 2-decimal price = 2-decimal cost).
        if action.side in ("buy_up", "buy_down"):
            tokens = math.floor(tokens)
            dollars = tokens * price

        if tokens <= 0:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=price, amount=0,
                tokens=0, order_id=None, token_id=token_id, success=False,
                error="No trade possible (insufficient balance)",
                timestamp=now,
            )
            self._log_trade(trade)
            return trade

        # CLOB minimum order size: silently skip if below threshold
        if tokens < CLOB_MIN_ORDER_TOKENS:
            logger.debug(
                "Skipping trade: %d tokens below minimum %d",
                tokens, CLOB_MIN_ORDER_TOKENS,
            )
            return None

        # Place the order (FAK = fill what's available, cancel remainder)
        requested_tokens = tokens
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=tokens,
                side=order_side,
            )
            options = PartialCreateOrderOptions(
                tick_size="0.01",
                neg_risk=False,
            )
            signed_order = self.client.create_order(order_args, options)
            resp = self.client.post_order(signed_order, orderType=OrderType.FAK)

            order_id = None
            actual_tokens = tokens   # default: assume full fill
            actual_dollars = dollars

            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id")

                # Check if the CLOB rejected the order
                if resp.get("success") is False:
                    raise RuntimeError(
                        "CLOB rejected order: {}".format(resp.get("errorMsg", "unknown"))
                    )

                # Parse actual fill amounts from response (6-decimal strings,
                # may be int-like "13188404" or float-like "13188404.0")
                taking = resp.get("takingAmount")
                making = resp.get("makingAmount")
                if taking and making:
                    if order_side == "BUY":
                        actual_tokens = int(float(taking)) / 1_000_000
                        actual_dollars = int(float(making)) / 1_000_000
                    else:  # SELL
                        actual_tokens = int(float(making)) / 1_000_000
                        actual_dollars = int(float(taking)) / 1_000_000

                    if actual_tokens <= 0:
                        raise RuntimeError("FAK order got zero fill (no liquidity)")

                    if actual_tokens < tokens:
                        logger.warning(
                            "Partial fill: requested %.4f tokens, got %.4f (%.0f%%)",
                            tokens, actual_tokens, 100 * actual_tokens / tokens,
                        )

            # Update position only on success (shared with simulator)
            position.apply_trade(action.side, price, actual_tokens)
            trade = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=actual_dollars, tokens=actual_tokens, order_id=order_id,
                token_id=token_id, success=True, error=None, timestamp=now,
                requested_tokens=requested_tokens if actual_tokens != requested_tokens else None,
                elapsed_seconds=sample.get("elapsed_seconds"),
            )
        except Exception as e:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=dollars, tokens=tokens, order_id=None,
                token_id=token_id, success=False, error=str(e), timestamp=now,
                elapsed_seconds=sample.get("elapsed_seconds"),
            )

        self._log_trade(trade)
        return trade

    def _log_trade(self, trade):
        # type: (Trade) -> None
        """Append trade result to the trade log file."""
        if self.trade_log_file is None:
            return
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(asdict(trade), default=str) + "\n")
        except Exception as e:
            logger.error("CRITICAL: Failed to log trade to %s: %s", self.trade_log_file, e)


# ---------------------------------------------------------------------------
# Trader — shared session runner
# ---------------------------------------------------------------------------


class Trader:
    """Drives the tick loop identically for simulation and live trading.

    The caller (simulator or bot) owns the Position and drives timing;
    Trader handles TickContext construction, strategy calls, and execution.
    """

    def __init__(self, strategy, executor):
        # type: (Any, Executor) -> None
        self.strategy = strategy
        self.executor = executor
        self._metadata = {}   # type: Dict[str, Any]
        self._position = None  # type: Optional[Position]
        self._initial_btc_price = 0.0
        self._window_duration = 300.0
        self._history = []    # type: List[Dict[str, Any]]
        self._trades = []     # type: List[Trade]
        self._tick_index = 0

    def start_session(self, metadata, position, initial_btc_price, window_duration,
                      reference_balance=None):
        # type: (Dict[str, Any], Position, float, float, Optional[float]) -> None
        """Reset state and notify the strategy of a new session."""
        self._metadata = metadata
        self._position = position
        self._initial_btc_price = initial_btc_price
        self._window_duration = window_duration
        self._reference_balance = reference_balance if reference_balance is not None else position.cash
        self._history = []
        self._trades = []
        self._tick_index = 0
        self.strategy.on_session_start(metadata)

    def _compute_guardrail_limits(self, sample):
        # type: (Dict[str, Any]) -> Tuple[float, bool]
        """Compute max buy amount and whether loss limit is hit.

        Returns (max_buy_amount, loss_limit_hit).
        """
        pos = self._position
        up_mid = sample.get("up_token_midpoint") or 0
        down_mid = sample.get("down_token_midpoint") or 0
        current_exposure = pos.up_tokens * up_mid + pos.down_tokens * down_mid
        total_value = pos.cash + current_exposure

        # Loss limit check
        max_loss = min(self._reference_balance * MAX_LOSS_FRACTION, MAX_LOSS_DOLLARS)
        if self._reference_balance - total_value > max_loss:
            return 0.0, True

        # Position size cap
        max_exposure = min(total_value * MAX_POSITION_FRACTION, MAX_POSITION_DOLLARS)
        room = max(0.0, max_exposure - current_exposure)
        return room, False

    def _apply_guardrails(self, action, max_buy_amount, loss_limit_hit):
        # type: (TradeAction, float, bool) -> TradeAction
        """Cap buy amounts by position limit and refuse trades if loss limit hit."""
        if action.side == "hold":
            return action

        if loss_limit_hit:
            return HOLD

        if action.side.startswith("buy"):
            if max_buy_amount <= 0:
                return HOLD
            if action.amount > max_buy_amount:
                action = TradeAction(side=action.side, amount=max_buy_amount)

        return action

    def process_tick(self, sample):
        # type: (Dict[str, Any]) -> Optional[Trade]
        """Build TickContext, call strategy, execute trade, update history.

        Returns the Trade (or None) so the caller can inspect/log it.
        """
        elapsed_frac = (
            sample["elapsed_seconds"] / self._window_duration
            if self._window_duration > 0 else 0.0
        )
        elapsed_frac = min(elapsed_frac, 1.0)

        # Compute guardrail limits once per tick
        max_buy_amount, loss_limit_hit = self._compute_guardrail_limits(sample)

        # Defensive copy of position for strategy visibility
        ctx = TickContext(
            tick_index=self._tick_index,
            sample=sample,
            history=list(self._history),
            position=Position(
                cash=self._position.cash,
                up_tokens=self._position.up_tokens,
                down_tokens=self._position.down_tokens,
            ),
            metadata=self._metadata,
            initial_btc_price=self._initial_btc_price,
            elapsed_fraction=elapsed_frac,
            max_buy_amount=max_buy_amount,
            loss_limit_hit=loss_limit_hit,
        )

        action = self.strategy.on_tick(ctx)
        action = self._apply_guardrails(action, max_buy_amount, loss_limit_hit)

        trade = self.executor.execute_trade(
            action, self._tick_index, self._metadata, sample, self._position,
        )

        # Track successful trades (success is None for sim, True for live)
        if trade is not None and trade.success is not False:
            self._trades.append(trade)

        self._history.append(sample)
        self._tick_index += 1
        return trade

    def end_session(self, outcome):
        # type: (Optional[str]) -> List[Trade]
        """Resolve position and return the list of trades."""
        self._position.resolve(outcome)
        return list(self._trades)
