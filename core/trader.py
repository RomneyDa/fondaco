"""Unified trading abstractions shared by the simulator and live bot.

Contains the core types (TradeAction, Position, TickContext), a unified Trade
record, an Executor ABC with simulation and CLOB implementations, and a Trader
class that drives the tick loop identically for both replay and live trading.
"""

import json
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
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
    price: Optional[float] = None  # if set → GTC limit order; if None → FAK (current behavior)


HOLD = TradeAction(side="hold", amount=0.0)


@dataclass
class OpenOrder:
    """Tracks a pending GTC limit order."""
    order_id: str
    side: str               # buy_up, buy_down, sell_up, sell_down
    token: str              # "up" or "down"
    price: float
    tokens: float
    dollars_reserved: float  # cash held aside (buys only)
    tokens_reserved: float   # tokens held aside (sells only)
    tick_placed: int
    token_id: Optional[str] = None


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
            if tokens < CLOB_MIN_ORDER_TOKENS:
                return 0.0, 0.0
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

    def reserve_for_order(self, side, price, tokens):
        # type: (str, float, float) -> Tuple[float, float]
        """Reserve funds for a pending limit order.

        Deducts cash (for buys) or tokens (for sells) immediately so the
        strategy can't double-spend while the order is pending.

        Returns (dollars_reserved, tokens_reserved).
        """
        if side in ("buy_up", "buy_down"):
            cost = tokens * price
            self.cash -= cost
            return cost, 0.0
        elif side in ("sell_up", "sell_down"):
            if side == "sell_up":
                self.up_tokens -= tokens
            else:
                self.down_tokens -= tokens
            return 0.0, tokens
        return 0.0, 0.0

    def unreserve_for_order(self, side, dollars_reserved, tokens_reserved):
        # type: (str, float, float) -> None
        """Return reserved funds when a limit order is cancelled."""
        if side in ("buy_up", "buy_down"):
            self.cash += dollars_reserved
        elif side in ("sell_up", "sell_down"):
            if side == "sell_up":
                self.up_tokens += tokens_reserved
            else:
                self.down_tokens += tokens_reserved

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
    open_orders: List[OpenOrder] = field(default_factory=list)


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
    order_type: Optional[str] = None  # "FAK", "GTC", or None (backward compat)


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

    def place_limit_order(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[OpenOrder]
        """Post a GTC limit order and reserve funds in position.

        Returns an OpenOrder on success, or None if the order can't be placed.
        """
        raise NotImplementedError("Limit orders not supported by this executor")

    def check_fills(self, open_orders, sample, position):
        # type: (List[OpenOrder], Dict[str, Any], Position) -> List[Trade]
        """Check pending orders for fills. Removes filled orders from the list.

        Returns a list of Trade records for any fills that occurred.
        """
        raise NotImplementedError("Limit orders not supported by this executor")

    def cancel_order(self, order, position):
        # type: (OpenOrder, Position) -> bool
        """Cancel a pending order and unreserve funds. Returns True on success."""
        raise NotImplementedError("Limit orders not supported by this executor")

    def cancel_all_orders(self, open_orders, position):
        # type: (List[OpenOrder], Position) -> int
        """Cancel all pending orders. Returns count of successfully cancelled orders."""
        cancelled = 0
        for order in list(open_orders):
            if self.cancel_order(order, position):
                cancelled += 1
        open_orders.clear()
        return cancelled


# ---------------------------------------------------------------------------
# SimExecutor — backtesting execution
# ---------------------------------------------------------------------------


class SimExecutor(Executor):
    """Executes trades against historical sample data for backtesting."""

    def __init__(self, execution_mode="book", slippage=0.02):
        # type: (str, float) -> None
        self.execution_mode = execution_mode
        self.slippage = slippage
        self._sim_order_counter = 0

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

    def place_limit_order(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[OpenOrder]
        """Simulate posting a GTC limit order."""
        if action.side == "hold" or action.amount <= 0 or action.price is None:
            return None

        price = round(action.price, 2)
        if price <= 0 or price >= 1.0:
            return None

        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)
        if tokens <= 0 or tokens < CLOB_MIN_ORDER_TOKENS:
            return None

        # For buys, floor to integer (same as ClobExecutor)
        if action.side in ("buy_up", "buy_down"):
            tokens = math.floor(tokens)
            dollars = tokens * price
            if tokens < CLOB_MIN_ORDER_TOKENS:
                return None

        token = "up" if action.side in ("buy_up", "sell_up") else "down"

        # Reserve funds
        dollars_reserved, tokens_reserved = position.reserve_for_order(
            action.side, price, tokens,
        )

        self._sim_order_counter += 1
        return OpenOrder(
            order_id="sim-{}".format(self._sim_order_counter),
            side=action.side,
            token=token,
            price=price,
            tokens=tokens,
            dollars_reserved=dollars_reserved,
            tokens_reserved=tokens_reserved,
            tick_placed=tick_index,
        )

    def check_fills(self, open_orders, sample, position):
        # type: (List[OpenOrder], Dict[str, Any], Position) -> List[Trade]
        """Sim fill model: buy at P fills when midpoint <= P, sell at P fills when midpoint >= P.

        When we post a buy limit at price P, we're offering to buy at P. Market
        makers will sell to us when fair value (midpoint) is at or below P.
        Conversely, sell limits at P fill when midpoint >= P.
        """
        fills = []
        remaining = []

        for order in open_orders:
            mid_key = "up_token_midpoint" if order.token == "up" else "down_token_midpoint"
            midpoint = sample.get(mid_key)
            if midpoint is None:
                remaining.append(order)
                continue

            filled = False
            if order.side in ("buy_up", "buy_down"):
                # Buy limit fills when midpoint <= our price (we're overpaying vs fair value)
                filled = midpoint <= order.price
            elif order.side in ("sell_up", "sell_down"):
                # Sell limit fills when midpoint >= our price (we're underselling vs fair value)
                filled = midpoint >= order.price

            if filled:
                # Apply the trade to position (funds were already reserved)
                # For buys: cash was deducted, now add tokens
                # For sells: tokens were deducted, now add cash
                if order.side in ("buy_up", "buy_down"):
                    if order.side == "buy_up":
                        position.up_tokens += order.tokens
                    else:
                        position.down_tokens += order.tokens
                else:
                    position.cash += order.tokens * order.price

                fills.append(Trade(
                    tick_index=0,  # will be set by caller
                    side=order.side,
                    price=order.price,
                    amount=order.tokens * order.price,
                    tokens=order.tokens,
                    order_id=order.order_id,
                    order_type="GTC",
                    elapsed_seconds=sample.get("elapsed_seconds"),
                ))
            else:
                remaining.append(order)

        open_orders[:] = remaining
        return fills

    def cancel_order(self, order, position):
        # type: (OpenOrder, Position) -> bool
        """Cancel a simulated order, returning reserved funds."""
        position.unreserve_for_order(
            order.side, order.dollars_reserved, order.tokens_reserved,
        )
        return True


# ---------------------------------------------------------------------------
# ClobExecutor — live CLOB order execution
# ---------------------------------------------------------------------------


FILL_CHECK_INTERVAL = 2.5  # seconds between polling order status


class ClobExecutor(Executor):
    """Executes trades via Polymarket's CLOB API."""

    def __init__(self, clob_client, trade_log_file=None):
        # type: (Any, Optional[str]) -> None
        self.client = clob_client
        self.trade_log_file = trade_log_file
        self._last_fill_check = 0.0  # timestamp of last check_fills poll

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
        Price = best_ask for buys, best_bid for sells (crosses the spread).
        On success, mutates position via Position.apply_trade().
        """
        # Lazy-import so simulation doesn't require py_clob_client
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

        if action.side == "hold" or action.amount <= 0:
            return None

        now = datetime.now(timezone.utc).isoformat()

        # Map action side to token_id, order side, and book price keys.
        # Buys cross the spread (best_ask), sells cross via best_bid.
        side_map = {
            "buy_up": ("up_token_id", "BUY", "up_token_best_ask", "up_token_best_bid"),
            "buy_down": ("down_token_id", "BUY", "down_token_best_ask", "down_token_best_bid"),
            "sell_up": ("up_token_id", "SELL", "up_token_best_bid", "up_token_best_ask"),
            "sell_down": ("down_token_id", "SELL", "down_token_best_bid", "down_token_best_ask"),
        }

        if action.side not in side_map:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=0, amount=0,
                tokens=0, order_id=None, token_id="", success=False,
                error="Unknown side", timestamp=now,
            )
            self._log_trade(trade)
            return trade

        token_key, order_side, price_key, other_side_key = side_map[action.side]
        token_id = metadata[token_key]
        exec_price = sample.get(price_key)
        other_price = sample.get(other_side_key)

        # Both sides of the book must exist
        if (exec_price is None or exec_price <= 0 or exec_price >= 1.0
                or other_price is None or other_price <= 0 or other_price >= 1.0):
            trade = Trade(
                tick_index=tick_index, side=action.side, price=0, amount=0,
                tokens=0, order_id=None, token_id=token_id, success=False,
                error="No valid price for {}".format(action.side), timestamp=now,
            )
            self._log_trade(trade)
            return trade

        price = round(exec_price, 2)
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

    def place_limit_order(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[OpenOrder]
        """Post a GTC limit order via the CLOB API."""
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

        if action.side == "hold" or action.amount <= 0 or action.price is None:
            return None

        now = datetime.now(timezone.utc).isoformat()

        side_map = {
            "buy_up": ("up_token_id", "BUY"),
            "buy_down": ("down_token_id", "BUY"),
            "sell_up": ("up_token_id", "SELL"),
            "sell_down": ("down_token_id", "SELL"),
        }
        if action.side not in side_map:
            return None

        token_key, order_side = side_map[action.side]
        token_id = metadata[token_key]

        price = round(action.price, 2)
        if price <= 0 or price >= 1.0:
            return None

        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)

        # Floor buys to integer (same as FAK path)
        if action.side in ("buy_up", "buy_down"):
            tokens = math.floor(tokens)
            dollars = tokens * price

        if tokens <= 0 or tokens < CLOB_MIN_ORDER_TOKENS:
            return None

        token = "up" if action.side in ("buy_up", "sell_up") else "down"

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
            resp = self.client.post_order(signed_order, orderType=OrderType.GTC)

            order_id = None
            if isinstance(resp, dict):
                if resp.get("success") is False:
                    raise RuntimeError(
                        "CLOB rejected GTC order: {}".format(resp.get("errorMsg", "unknown"))
                    )
                order_id = resp.get("orderID") or resp.get("id")

            if order_id is None:
                raise RuntimeError("No order ID in GTC response")

            # Reserve funds in position
            dollars_reserved, tokens_reserved = position.reserve_for_order(
                action.side, price, tokens,
            )

            logger.info(
                "GTC limit order placed: %s %s %.0f tokens @ %.2f (order_id=%s)",
                order_side, token, tokens, price, order_id,
            )

            # Log the placement
            trade_record = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=dollars, tokens=tokens, order_id=order_id,
                token_id=token_id, success=True, error=None,
                timestamp=now, order_type="GTC",
                elapsed_seconds=sample.get("elapsed_seconds"),
            )
            self._log_trade(trade_record)

            return OpenOrder(
                order_id=order_id,
                side=action.side,
                token=token,
                price=price,
                tokens=tokens,
                dollars_reserved=dollars_reserved,
                tokens_reserved=tokens_reserved,
                tick_placed=tick_index,
                token_id=token_id,
            )

        except Exception as e:
            logger.error("GTC order placement failed: %s", e)
            trade_record = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=dollars, tokens=tokens, order_id=None,
                token_id=token_id, success=False, error=str(e),
                timestamp=now, order_type="GTC",
                elapsed_seconds=sample.get("elapsed_seconds"),
            )
            self._log_trade(trade_record)
            return None

    def check_fills(self, open_orders, sample, position):
        # type: (List[OpenOrder], Dict[str, Any], Position) -> List[Trade]
        """Poll CLOB for order status. Throttled to every FILL_CHECK_INTERVAL seconds."""
        now = time.time()
        if now - self._last_fill_check < FILL_CHECK_INTERVAL:
            return []
        self._last_fill_check = now

        fills = []
        remaining = []

        for order in open_orders:
            try:
                resp = self.client.get_order(order.order_id)
                status = resp.get("status", "").upper() if isinstance(resp, dict) else ""

                if status == "MATCHED":
                    # Order filled — apply to position
                    if order.side in ("buy_up", "buy_down"):
                        if order.side == "buy_up":
                            position.up_tokens += order.tokens
                        else:
                            position.down_tokens += order.tokens
                    else:
                        position.cash += order.tokens * order.price

                    fills.append(Trade(
                        tick_index=0,
                        side=order.side,
                        price=order.price,
                        amount=order.tokens * order.price,
                        tokens=order.tokens,
                        order_id=order.order_id,
                        token_id=order.token_id,
                        success=True,
                        order_type="GTC",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=sample.get("elapsed_seconds"),
                    ))
                    logger.info(
                        "GTC order filled: %s %.0f tokens @ %.2f (order_id=%s)",
                        order.side, order.tokens, order.price, order.order_id,
                    )

                elif status in ("CANCELLED", "EXPIRED"):
                    # Order gone — unreserve funds
                    position.unreserve_for_order(
                        order.side, order.dollars_reserved, order.tokens_reserved,
                    )
                    logger.info(
                        "GTC order %s: %s (order_id=%s)",
                        status.lower(), order.side, order.order_id,
                    )

                else:
                    # LIVE or other — keep tracking
                    remaining.append(order)

            except Exception as e:
                logger.warning(
                    "Failed to check order %s: %s (will retry)",
                    order.order_id, e,
                )
                remaining.append(order)

        open_orders[:] = remaining
        return fills

    def cancel_order(self, order, position):
        # type: (OpenOrder, Position) -> bool
        """Cancel a pending GTC order via CLOB API."""
        try:
            self.client.cancel(order.order_id)
            position.unreserve_for_order(
                order.side, order.dollars_reserved, order.tokens_reserved,
            )
            logger.info(
                "GTC order cancelled: %s (order_id=%s)", order.side, order.order_id,
            )
            return True
        except Exception as e:
            logger.error("Failed to cancel order %s: %s", order.order_id, e)
            return False

    def cancel_all_orders(self, open_orders, position):
        # type: (List[OpenOrder], Position) -> int
        """Cancel all pending orders via CLOB bulk cancel."""
        if not open_orders:
            return 0
        try:
            self.client.cancel_all()
            for order in open_orders:
                position.unreserve_for_order(
                    order.side, order.dollars_reserved, order.tokens_reserved,
                )
            cancelled = len(open_orders)
            open_orders.clear()
            logger.info("Cancelled all %d pending GTC orders", cancelled)
            return cancelled
        except Exception as e:
            logger.error("Bulk cancel failed: %s. Falling back to individual cancels.", e)
            cancelled = 0
            for order in list(open_orders):
                if self.cancel_order(order, position):
                    cancelled += 1
            open_orders.clear()
            return cancelled


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
        self._capture_decision_log = False
        self._decision_log = []  # type: List[Dict[str, Any]]
        self._open_orders = []  # type: List[OpenOrder]
        self._last_tick_fills = []  # type: List[Trade]

    def start_session(self, metadata, position, initial_btc_price, window_duration,
                      reference_balance=None, capture_decision_log=False):
        # type: (Dict[str, Any], Position, float, float, Optional[float], bool) -> None
        """Reset state and notify the strategy of a new session."""
        self._metadata = metadata
        self._position = position
        self._initial_btc_price = initial_btc_price
        self._window_duration = window_duration
        self._reference_balance = reference_balance if reference_balance is not None else position.cash
        self._history = []
        self._trades = []
        self._tick_index = 0
        self._capture_decision_log = capture_decision_log
        self._decision_log = []
        self._open_orders = []
        self._last_tick_fills = []
        self.strategy._tick_logs = []
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

    def _apply_guardrails(self, action, max_buy_amount, loss_limit_hit, sample=None):
        # type: (TradeAction, float, bool, Optional[Dict[str, Any]]) -> TradeAction
        """Cap buy amounts by position limit and refuse trades if loss limit hit."""
        if action.side == "hold":
            return action

        if loss_limit_hit:
            return HOLD

        if action.side.startswith("buy"):
            if max_buy_amount <= 0:
                return HOLD
            if action.amount > max_buy_amount:
                action = TradeAction(side=action.side, amount=max_buy_amount, price=action.price)

        return action

    def process_tick(self, sample):
        # type: (Dict[str, Any]) -> Optional[Trade]
        """Build TickContext, call strategy, execute trade, update history.

        Returns the Trade (or None) so the caller can inspect/log it.
        For limit order fills, use ``last_tick_fills`` to get all fills.
        """
        # 1. Check fills on pending limit orders (before strategy runs)
        self._last_tick_fills = []
        if self._open_orders:
            try:
                fills = self.executor.check_fills(
                    self._open_orders, sample, self._position,
                )
                for fill in fills:
                    fill.tick_index = self._tick_index
                    self._trades.append(fill)
                self._last_tick_fills = fills
            except NotImplementedError:
                pass

        elapsed_frac = (
            sample["elapsed_seconds"] / self._window_duration
            if self._window_duration > 0 else 0.0
        )
        elapsed_frac = min(elapsed_frac, 1.0)

        # 2. Compute guardrail limits
        max_buy_amount, loss_limit_hit = self._compute_guardrail_limits(sample)

        # 3. Build TickContext with open_orders snapshot
        ctx = TickContext(
            tick_index=self._tick_index,
            sample=sample,
            history=self._history,
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
            open_orders=list(self._open_orders),
        )

        # 4. Call strategy
        action = self.strategy.on_tick(ctx)

        # Drain strategy decision log buffer
        tick_logs = getattr(self.strategy, "_tick_logs", None)
        if tick_logs:
            if self._capture_decision_log:
                self._decision_log.append({
                    "tick": self._tick_index,
                    "msgs": list(tick_logs),
                })
            tick_logs.clear()

        # 5. Apply guardrails
        action = self._apply_guardrails(action, max_buy_amount, loss_limit_hit, sample)

        # 6. Route: limit order vs FAK
        trade = None
        if action.side != "hold" and action.price is not None:
            # Limit order path
            self._replace_order_on_token(action)
            try:
                order = self.executor.place_limit_order(
                    action, self._tick_index, self._metadata, sample, self._position,
                )
                if order is not None:
                    self._open_orders.append(order)
            except NotImplementedError:
                logger.warning("Executor does not support limit orders; falling back to FAK")
                action = TradeAction(side=action.side, amount=action.amount)
                trade = self.executor.execute_trade(
                    action, self._tick_index, self._metadata, sample, self._position,
                )
        else:
            # FAK path (existing behavior)
            trade = self.executor.execute_trade(
                action, self._tick_index, self._metadata, sample, self._position,
            )

        # Track successful trades (success is None for sim, True for live)
        if trade is not None and trade.success is not False:
            self._trades.append(trade)

        self._history.append(sample)
        self._tick_index += 1
        return trade

    def _replace_order_on_token(self, action):
        # type: (TradeAction) -> None
        """Cancel any existing pending order on the same token before placing a new one."""
        token = "up" if action.side in ("buy_up", "sell_up") else "down"
        for order in list(self._open_orders):
            if order.token == token:
                try:
                    self.executor.cancel_order(order, self._position)
                except NotImplementedError:
                    pass
                self._open_orders.remove(order)

    def cancel_all_pending(self):
        # type: () -> int
        """Cancel all pending limit orders. Returns count cancelled."""
        if not self._open_orders:
            return 0
        try:
            return self.executor.cancel_all_orders(self._open_orders, self._position)
        except NotImplementedError:
            return 0

    def end_session(self, outcome):
        # type: (Optional[str]) -> List[Trade]
        """Cancel pending orders, resolve position, and return the list of trades."""
        self.cancel_all_pending()
        self._position.resolve(outcome)
        return list(self._trades)

    @property
    def last_tick_fills(self):
        # type: () -> List[Trade]
        """Limit order fills from the most recent process_tick call."""
        return self._last_tick_fills

    @property
    def decision_log(self):
        # type: () -> List[Dict[str, Any]]
        """Sparse decision log: only ticks where the strategy called log()."""
        return self._decision_log
