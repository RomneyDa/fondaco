"""Unified trading abstractions shared by the simulator and live bot.

Contains the core types (TradeAction, Position, TickContext), a unified Trade
record, an Executor ABC with simulation and CLOB implementations, and a Trader
class that drives the tick loop identically for both replay and live trading.
"""

import json
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.defaults import (
    CLOB_MIN_ORDER_TOKENS,
    DEFAULT_EXPIRATION_SECONDS,
    MAX_TOKEN_PRICE,
    PRICE_DECIMALS,
    TICK_SIZE,
    TOKEN_DECIMALS,
    TOKEN_DUST_BUFFER,
    TOKEN_FLOOR_DIVISOR,
    TRADE_CUTOFF_SECONDS,
    WEI_DIVISOR,
    compute_taker_fee,
)

try:
    from py_clob_client.exceptions import PolyApiException
except ImportError:
    PolyApiException = None  # type: ignore[assignment,misc]

# Exception tuple for CLOB API operations (PolyApiException when available,
# plus common parsing/value errors that can surface from response handling).
_CLOB_ERRORS = tuple(
    e for e in (PolyApiException, ValueError, KeyError, TypeError) if e is not None
)  # type: Tuple[type, ...]

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
    expiration_seconds: Optional[float] = DEFAULT_EXPIRATION_SECONDS  # TTL; default 30s GTD


HOLD = TradeAction(side="hold", amount=0.0, expiration_seconds=None)


@dataclass
class OpenOrder:
    """Tracks a pending GTC/GTD limit order."""
    order_id: str
    side: str               # buy_up, buy_down, sell_up, sell_down
    token: str              # "up" or "down"
    price: float
    tokens: float
    dollars_reserved: float  # cash held aside (buys only)
    tokens_reserved: float   # tokens held aside (sells only)
    tick_placed: int
    token_id: Optional[str] = None
    expiration: Optional[float] = None  # sim: elapsed_seconds deadline; live: unix timestamp


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
            tokens = round(dollars / price, TOKEN_DECIMALS)
            if tokens < CLOB_MIN_ORDER_TOKENS:
                return 0.0, 0.0
            cost = tokens * price
            return tokens, cost

        if side in ("sell_up", "sell_down"):
            available = self.up_tokens if side == "sell_up" else self.down_tokens
            tokens = math.floor(min(amount, available) * TOKEN_FLOOR_DIVISOR) / TOKEN_FLOOR_DIVISOR
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

        if self.cash < -TOKEN_DUST_BUFFER or self.up_tokens < -1 / TOKEN_FLOOR_DIVISOR or self.down_tokens < -1 / TOKEN_FLOOR_DIVISOR:
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


class TickContext:
    """Everything a strategy sees at each tick.

    Uses __slots__ instead of @dataclass for faster construction in the
    hot simulation loop (~5400 constructions per session).
    """
    __slots__ = (
        'tick_index', 'sample', 'history', 'position', 'metadata',
        'initial_btc_price', 'elapsed_fraction', 'max_buy_amount',
        'loss_limit_hit', 'open_orders',
    )

    def __init__(self, tick_index, sample, history, position, metadata,
                 initial_btc_price, elapsed_fraction, max_buy_amount=0.0,
                 loss_limit_hit=False, open_orders=None):
        self.tick_index = tick_index
        self.sample = sample
        self.history = history
        self.position = position
        self.metadata = metadata
        self.initial_btc_price = initial_btc_price
        self.elapsed_fraction = elapsed_fraction
        self.max_buy_amount = max_buy_amount
        self.loss_limit_hit = loss_limit_hit
        self.open_orders = open_orders if open_orders is not None else []


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
    strategy_version: Optional[str] = None  # strategy version at time of trade
    fee: Optional[float] = None  # taker fee in dollars (None = pre-fee-tracking)


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

    def __init__(self, execution_mode="book", slippage=0.02, fees=True,
                 limit_fill_margin=None):
        # type: (str, float, bool, Optional[float]) -> None
        self.execution_mode = execution_mode
        self.slippage = slippage
        self.fees = fees
        # Margin the midpoint must cross beyond the limit price before a
        # GTC/GTD order fills.  Defaults to slippage if not set explicitly,
        # but can be tuned independently (e.g. --limit-fill-margin 0.03).
        self.limit_fill_margin = limit_fill_margin if limit_fill_margin is not None else slippage
        self._sim_order_counter = 0

    def execute_trade(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[Trade]
        if action.side == "hold" or action.amount <= 0:
            return None

        price = self._get_execution_price(action.side, sample)
        if price is not None:
            price = min(round(price, PRICE_DECIMALS), MAX_TOKEN_PRICE)
        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)
        if tokens <= 0:
            return None

        # Enforce same minimum order size as CLOB (mirrors live rejection)
        if tokens < CLOB_MIN_ORDER_TOKENS:
            return None

        position.apply_trade(action.side, price, tokens)
        fee = compute_taker_fee(price, tokens, self.fees)
        position.cash -= fee
        return Trade(
            tick_index=tick_index,
            side=action.side,
            price=price,
            amount=dollars,
            tokens=tokens,
            elapsed_seconds=sample.get("elapsed_seconds"),
            fee=fee,
        )

    def _get_execution_price(self, side, sample):
        # type: (str, Any) -> Optional[float]
        """Look up execution price based on mode and action side.

        Uses direct attribute access + if/elif instead of building a dict
        table each call (this is in the hot path for every trade).
        """
        _get = sample.get
        is_up = "up" in side  # buy_up or sell_up
        is_buy = side[0] == "b"  # buy_up or buy_down

        if self.execution_mode == "midpoint_slippage":
            base = _get("up_token_midpoint") if is_up else _get("down_token_midpoint")
            if base is None:
                return None
            return base + self.slippage if is_buy else base - self.slippage

        elif self.execution_mode == "midpoint":
            return _get("up_token_midpoint") if is_up else _get("down_token_midpoint")

        elif self.execution_mode == "book":
            if is_buy:
                return _get("up_token_best_ask") if is_up else _get("down_token_best_ask")
            else:
                return _get("up_token_midpoint") if is_up else _get("down_token_midpoint")

        return None

    def place_limit_order(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[OpenOrder]
        """Simulate posting a GTC/GTD limit order."""
        if action.side == "hold" or action.amount <= 0 or action.price is None:
            return None

        price = round(action.price, PRICE_DECIMALS)
        if price <= 0 or price >= 1.0:
            return None

        tokens, dollars = position.compute_trade_size(action.side, price, action.amount)
        if tokens <= 0 or tokens < CLOB_MIN_ORDER_TOKENS:
            return None

        # For buys, floor to integer (same as ClobExecutor)
        if action.side in ("buy_up", "buy_down"):
            tokens = math.floor(tokens)
            if tokens < CLOB_MIN_ORDER_TOKENS:
                return None

        token = "up" if action.side in ("buy_up", "sell_up") else "down"

        # Reserve funds
        dollars_reserved, tokens_reserved = position.reserve_for_order(
            action.side, price, tokens,
        )

        # Compute GTD expiration deadline (elapsed_seconds)
        expiration = None
        if action.expiration_seconds is not None:
            current_elapsed = sample.get("elapsed_seconds", 0.0)
            expiration = current_elapsed + action.expiration_seconds

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
            expiration=expiration,
        )

    def check_fills(self, open_orders, sample, position):
        # type: (List[OpenOrder], Dict[str, Any], Position) -> List[Trade]
        """Sim fill model with slippage margin for realistic GTC fill rates.

        A limit order doesn't fill the instant the midpoint touches the limit
        price — the market must move *through* the price to find a counterparty.
        We model this by requiring the midpoint to cross the limit price by
        ``self.slippage`` (default 0.02), which brings sim fill rates in line
        with the ~50% observed in production.

        Buy at P fills when midpoint <= P - slippage.
        Sell at P fills when midpoint >= P + slippage.
        """
        fills = []
        remaining = []
        current_elapsed = sample.get("elapsed_seconds", 0.0)
        margin = self.limit_fill_margin

        for order in open_orders:
            # GTD expiration: if past deadline, unreserve and discard
            if order.expiration is not None and current_elapsed >= order.expiration:
                position.unreserve_for_order(
                    order.side, order.dollars_reserved, order.tokens_reserved,
                )
                continue

            mid_key = "up_token_midpoint" if order.token == "up" else "down_token_midpoint"
            midpoint = sample.get(mid_key)
            if midpoint is None:
                remaining.append(order)
                continue

            filled = False
            if order.side in ("buy_up", "buy_down"):
                # Buy limit fills when midpoint moves below our price by margin
                filled = midpoint <= order.price - margin
            elif order.side in ("sell_up", "sell_down"):
                # Sell limit fills when midpoint moves above our price by margin.
                # Cap the required threshold at 0.99 — the midpoint can't
                # exceed 0.99, so sell limits near the ceiling would otherwise
                # become unfillable in simulation.
                threshold = min(order.price + margin, MAX_TOKEN_PRICE)
                filled = midpoint >= threshold

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

                fee = compute_taker_fee(order.price, order.tokens, self.fees)
                position.cash -= fee

                fills.append(Trade(
                    tick_index=0,  # will be set by caller
                    side=order.side,
                    price=order.price,
                    amount=order.tokens * order.price,
                    tokens=order.tokens,
                    order_id=order.order_id,
                    order_type="GTD" if order.expiration is not None else "GTC",
                    elapsed_seconds=sample.get("elapsed_seconds"),
                    fee=fee,
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


FILL_CHECK_INTERVAL = 0.5  # seconds between polling order status
AUTO_EXIT_MIDPOINT_THRESHOLD = MAX_TOKEN_PRICE  # sell when midpoint >= this
SELL_COOLDOWN_SECONDS = 5.0  # min time between sell attempts for the same token


class ClobExecutor(Executor):
    """Executes trades via Polymarket's CLOB API."""

    def __init__(self, clob_client, trade_log_file=None, strategy_name=None):
        # type: (Any, Optional[str], Optional[str]) -> None
        self.client = clob_client
        self.trade_log_file = trade_log_file
        self.strategy_name = strategy_name
        self._last_fill_check = 0.0  # timestamp of last check_fills poll
        self._last_sell_fail = {}  # type: Dict[str, float]  # token_id -> timestamp

    def _check_sell_cooldown(self, token_id):
        # type: (str) -> bool
        """Return True if a sell for this token should be skipped (cooldown active)."""
        last_fail = self._last_sell_fail.get(token_id)
        if last_fail is not None:
            if time.time() - last_fail < SELL_COOLDOWN_SECONDS:
                return True
        return False

    def _record_sell_failure(self, token_id):
        # type: (str) -> None
        self._last_sell_fail[token_id] = time.time()

    def _clear_sell_cooldown(self, token_id):
        # type: (str) -> None
        self._last_sell_fail.pop(token_id, None)

    def _refresh_balance_allowance(self, token_id):
        # type: (str) -> Optional[float]
        """Refresh CLOB's cached conditional token balance after a fill.

        Returns the on-chain token balance (in tokens, not wei) so callers
        can reconcile the internal position, or None on failure.
        """
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        for attempt in range(3):
            try:
                self.client.update_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                    )
                )
                # Read back actual on-chain balance
                try:
                    ba = self.client.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL,
                            token_id=token_id,
                        )
                    )
                    raw = ba.get("balance")
                    logger.info(
                        "CLOB balance after refresh: token=%s balance=%s allowance=%s",
                        token_id[:12], raw, ba.get("allowance"),
                    )
                    if raw is not None:
                        return int(float(raw)) / WEI_DIVISOR
                except (ValueError, KeyError, TypeError):
                    pass
                return None
            except _CLOB_ERRORS as e:
                logger.warning(
                    "Refresh balance allowance attempt %d/3 failed: %s",
                    attempt + 1, e,
                )
                time.sleep(0.5)
        logger.error(
            "Failed to refresh balance allowance after 3 attempts for token %s. "
            "Sells may fail with 'not enough balance / allowance'.",
            token_id,
        )
        return None

    def _reconcile_sell_balance(self, action_side, token_id, position):
        # type: (str, str, Position) -> None
        """Cap internal position to on-chain balance before selling.

        Multiple workers share one wallet, so the on-chain balance may be
        lower than any single worker's position tracker thinks.  Capping
        prevents 'not enough balance / allowance' rejections.

        Tiny buffer (0.01 tokens) covers float↔fixed-point rounding only.
        Concurrent worker races can't be solved with a buffer (min order
        is 5 tokens) — we accept the occasional failure and retry next tick.
        """
        on_chain = self._refresh_balance_allowance(token_id)
        if on_chain is None:
            return

        # Buffer for float↔6-decimal fixed-point rounding
        safe_balance = max(0.0, on_chain - TOKEN_DUST_BUFFER)

        if action_side in ("sell_up", "buy_up"):
            tracked = position.up_tokens
            if tracked > safe_balance:
                logger.warning(
                    "Position desync: tracked %.4f up_tokens but on-chain has %.4f "
                    "(safe=%.4f). Capping to safe balance.",
                    tracked, on_chain, safe_balance,
                )
                position.up_tokens = safe_balance
        else:
            tracked = position.down_tokens
            if tracked > safe_balance:
                logger.warning(
                    "Position desync: tracked %.4f down_tokens but on-chain has %.4f "
                    "(safe=%.4f). Capping to safe balance.",
                    tracked, on_chain, safe_balance,
                )
                position.down_tokens = safe_balance

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
        # Buys cross the spread at best_ask.  Sells use midpoint — on thin
        # 0.01/0.99 books best_bid is always 0.01, so pricing a FAK sell there
        # gives away the position.  Midpoint reflects the real market maker
        # price and is more stable across the 300ms API latency.
        side_map = {
            "buy_up": ("up_token_id", "BUY", "up_token_best_ask", "up_token_best_bid"),
            "buy_down": ("down_token_id", "BUY", "down_token_best_ask", "down_token_best_bid"),
            "sell_up": ("up_token_id", "SELL", "up_token_midpoint", "up_token_best_ask"),
            "sell_down": ("down_token_id", "SELL", "down_token_midpoint", "down_token_best_ask"),
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

        # Sell cooldown: skip if a recent sell on this token failed
        # (avoids spamming the CLOB when balance/allowance is stale)
        if order_side == "SELL" and self._check_sell_cooldown(token_id):
            return None

        # Pre-sell: refresh balance and reconcile internal position to
        # on-chain reality (handles multi-worker wallet sharing).
        if order_side == "SELL":
            self._reconcile_sell_balance(action.side, token_id, position)

        exec_price = sample.get(price_key)
        other_price = sample.get(other_side_key)

        # Exec price must be valid.  For buys, the other side of the book
        # must also exist (spread sanity).  Sells only need the midpoint —
        # the ask side often vanishes on the winning token late in the
        # session, which is exactly when auto-exit needs to sell.
        price_invalid = (exec_price is None or exec_price <= 0 or exec_price >= 1.0)
        if not price_invalid and order_side == "BUY":
            price_invalid = (other_price is None or other_price <= 0 or other_price >= 1.0)
        if price_invalid:
            trade = Trade(
                tick_index=tick_index, side=action.side, price=0, amount=0,
                tokens=0, order_id=None, token_id=token_id, success=False,
                error="No valid price for {}".format(action.side), timestamp=now,
            )
            self._log_trade(trade)
            return trade

        price = min(round(exec_price, PRICE_DECIMALS), MAX_TOKEN_PRICE)
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
                tick_size=TICK_SIZE,
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
                    error_msg = resp.get("errorMsg", "unknown")
                    # Balance/allowance error on sells: likely our tracked
                    # token count is higher than actual (fees deducted from
                    # fill).  Refresh cache and retry once.
                    if (order_side == "SELL"
                            and "balance" in str(error_msg).lower()):
                        logger.warning(
                            "Sell rejected (balance/allowance), refreshing and retrying"
                        )
                        self._refresh_balance_allowance(token_id)
                        time.sleep(0.5)
                        signed_order = self.client.create_order(order_args, options)
                        resp = self.client.post_order(
                            signed_order, orderType=OrderType.FAK,
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            order_id = resp.get("orderID") or resp.get("id")
                        else:
                            raise RuntimeError(
                                "CLOB rejected sell after refresh: {}".format(
                                    resp.get("errorMsg", "unknown")
                                    if isinstance(resp, dict) else resp)
                            )
                    else:
                        raise RuntimeError(
                            "CLOB rejected order: {}".format(error_msg)
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

                    if actual_tokens < CLOB_MIN_ORDER_TOKENS:
                        raise RuntimeError(
                            "FAK order got negligible fill: {:.6f} tokens (min {})".format(
                                actual_tokens, CLOB_MIN_ORDER_TOKENS)
                        )

                    if actual_tokens < tokens:
                        logger.warning(
                            "Partial fill: requested %.4f tokens, got %.4f (%.0f%%)",
                            tokens, actual_tokens, 100 * actual_tokens / tokens,
                        )

            # Update position only on success (shared with simulator)
            position.apply_trade(action.side, price, actual_tokens)
            # Deduct estimated taker fee to keep cash in sync with on-chain
            fee = compute_taker_fee(price, actual_tokens)
            position.cash -= fee
            if order_side == "SELL":
                self._clear_sell_cooldown(token_id)
            trade = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=actual_dollars, tokens=actual_tokens, order_id=order_id,
                token_id=token_id, success=True, error=None, timestamp=now,
                requested_tokens=requested_tokens if actual_tokens != requested_tokens else None,
                elapsed_seconds=sample.get("elapsed_seconds"),
                fee=fee,
            )
        except Exception as e:
            if order_side == "SELL":
                self._record_sell_failure(token_id)
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
            d = asdict(trade)
            if self.strategy_name:
                d["strategy"] = self.strategy_name
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(d, default=str) + "\n")
        except OSError as e:
            logger.error("CRITICAL: Failed to log trade to %s: %s", self.trade_log_file, e)

    def place_limit_order(self, action, tick_index, metadata, sample, position):
        # type: (TradeAction, int, Dict[str, Any], Dict[str, Any], Position) -> Optional[OpenOrder]
        """Post a GTC/GTD limit order via the CLOB API."""
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

        # Sell cooldown: skip if a recent sell on this token failed
        if order_side == "SELL" and self._check_sell_cooldown(token_id):
            return None

        # Pre-sell: refresh balance and reconcile internal position to
        # on-chain reality (handles multi-worker wallet sharing).
        if order_side == "SELL":
            self._reconcile_sell_balance(action.side, token_id, position)

        price = round(action.price, PRICE_DECIMALS)
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

        # Determine order type: GTD if expiration_seconds set, else GTC
        is_gtd = action.expiration_seconds is not None
        order_type_label = "GTD" if is_gtd else "GTC"

        try:
            order_args_kwargs = dict(
                token_id=token_id,
                price=price,
                size=tokens,
                side=order_side,
            )
            if is_gtd:
                order_args_kwargs["expiration"] = str(
                    int(time.time()) + int(action.expiration_seconds)
                )
            order_args = OrderArgs(**order_args_kwargs)
            options = PartialCreateOrderOptions(
                tick_size=TICK_SIZE,
                neg_risk=False,
            )
            signed_order = self.client.create_order(order_args, options)
            clob_order_type = OrderType.GTD if is_gtd else OrderType.GTC
            resp = self.client.post_order(signed_order, orderType=clob_order_type)

            order_id = None
            if isinstance(resp, dict):
                if resp.get("success") is False:
                    error_msg = resp.get("errorMsg", "unknown")
                    # Balance/allowance error on sells: likely on-chain
                    # settlement hasn't completed yet.  Refresh and retry once.
                    if (order_side == "SELL"
                            and "balance" in str(error_msg).lower()):
                        logger.warning(
                            "%s sell rejected (balance/allowance), refreshing and retrying",
                            order_type_label,
                        )
                        self._refresh_balance_allowance(token_id)
                        time.sleep(0.5)
                        signed_order = self.client.create_order(order_args, options)
                        resp = self.client.post_order(
                            signed_order, orderType=clob_order_type,
                        )
                        if isinstance(resp, dict) and resp.get("success") is not False:
                            order_id = resp.get("orderID") or resp.get("id")
                        else:
                            raise RuntimeError(
                                "CLOB rejected {} sell after refresh: {}".format(
                                    order_type_label,
                                    resp.get("errorMsg", "unknown")
                                    if isinstance(resp, dict) else resp)
                            )
                    else:
                        raise RuntimeError(
                            "CLOB rejected {} order: {}".format(
                                order_type_label, error_msg))
                else:
                    order_id = resp.get("orderID") or resp.get("id")

            if order_id is None:
                raise RuntimeError("No order ID in {} response".format(order_type_label))

            # Reserve funds in position
            dollars_reserved, tokens_reserved = position.reserve_for_order(
                action.side, price, tokens,
            )

            # Compute expiration for OpenOrder tracking
            expiration = None
            if is_gtd:
                expiration = time.time() + action.expiration_seconds

            logger.info(
                "%s limit order placed: %s %s %.0f tokens @ %.2f (order_id=%s)",
                order_type_label, order_side, token, tokens, price, order_id,
            )

            # Log the placement
            trade_record = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=dollars, tokens=tokens, order_id=order_id,
                token_id=token_id, success=True, error=None,
                timestamp=now, order_type=order_type_label,
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
                expiration=expiration,
            )

        except Exception as e:
            if order_side == "SELL":
                self._record_sell_failure(token_id)
            logger.error("%s order placement failed: %s", order_type_label, e)
            trade_record = Trade(
                tick_index=tick_index, side=action.side, price=price,
                amount=dollars, tokens=tokens, order_id=None,
                token_id=token_id, success=False, error=str(e),
                timestamp=now, order_type=order_type_label,
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
                    # MATCHED = fully filled. Use order.tokens directly;
                    # the API's size_matched is unreliable (returns 0 for sells).
                    filled_tokens = order.tokens

                    # Order filled — apply to position
                    if order.side in ("buy_up", "buy_down"):
                        if order.side == "buy_up":
                            position.up_tokens += filled_tokens
                        else:
                            position.down_tokens += filled_tokens
                        # Refresh CLOB's cached view of our conditional
                        # token balance so subsequent sells don't fail
                        # with "not enough balance / allowance".
                        self._refresh_balance_allowance(order.token_id)
                    else:
                        position.cash += filled_tokens * order.price

                    # Deduct estimated taker fee
                    fill_fee = compute_taker_fee(order.price, filled_tokens)
                    position.cash -= fill_fee

                    fill_trade = Trade(
                        tick_index=0,
                        side=order.side,
                        price=order.price,
                        amount=filled_tokens * order.price,
                        tokens=filled_tokens,
                        order_id=order.order_id,
                        token_id=order.token_id,
                        success=True,
                        order_type="GTC",
                        fee=fill_fee,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=sample.get("elapsed_seconds"),
                    )
                    self._log_trade(fill_trade)
                    fills.append(fill_trade)
                    logger.info(
                        "GTC order filled: %s %.4f tokens @ %.2f (order_id=%s)",
                        order.side, filled_tokens, order.price, order.order_id,
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

            except _CLOB_ERRORS as e:
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
        except _CLOB_ERRORS as e:
            logger.error("Failed to cancel order %s: %s", order.order_id, e)
            return False

    def cancel_all_orders(self, open_orders, position):
        # type: (List[OpenOrder], Position) -> int
        """Cancel all tracked pending orders, reconciling any last-moment fills.

        Checks each order's status before cancelling to avoid unreserving
        funds for orders that already filled (which would create phantom cash).
        Uses scoped cancel_orders(ids) instead of cancel_all() to avoid
        cancelling other workers' orders on the same CLOB account.
        """
        if not open_orders:
            return 0

        cancelled = 0
        to_cancel = []  # type: List[OpenOrder]

        for order in open_orders:
            try:
                resp = self.client.get_order(order.order_id)
                status = resp.get("status", "").upper() if isinstance(resp, dict) else ""

                if status == "MATCHED":
                    # Order filled between last check_fills and now — apply fill
                    filled_tokens = order.tokens
                    if order.side in ("buy_up", "buy_down"):
                        if order.side == "buy_up":
                            position.up_tokens += filled_tokens
                        else:
                            position.down_tokens += filled_tokens
                        self._refresh_balance_allowance(order.token_id)
                    else:
                        position.cash += filled_tokens * order.price

                    fill_fee = compute_taker_fee(order.price, filled_tokens)
                    position.cash -= fill_fee

                    fill_trade = Trade(
                        tick_index=0, side=order.side, price=order.price,
                        amount=filled_tokens * order.price, tokens=filled_tokens,
                        order_id=order.order_id, token_id=order.token_id,
                        success=True, order_type="GTC",
                        fee=fill_fee,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    self._log_trade(fill_trade)
                    logger.info(
                        "Order filled before cancel: %s %.4f @ %.2f (order_id=%s)",
                        order.side, filled_tokens, order.price, order.order_id,
                    )

                elif status in ("CANCELLED", "EXPIRED"):
                    # Already gone — just unreserve
                    position.unreserve_for_order(
                        order.side, order.dollars_reserved, order.tokens_reserved,
                    )
                    cancelled += 1

                else:
                    # LIVE — needs cancelling
                    to_cancel.append(order)

            except _CLOB_ERRORS as e:
                logger.warning(
                    "Failed to check order %s before cancel: %s",
                    order.order_id, e,
                )
                to_cancel.append(order)

        # Batch-cancel remaining live orders (scoped to our order IDs)
        if to_cancel:
            cancel_ids = [o.order_id for o in to_cancel]
            try:
                self.client.cancel_orders(cancel_ids)
            except _CLOB_ERRORS as e:
                logger.error(
                    "Batch cancel failed: %s. Falling back to individual.", e,
                )
                for order in to_cancel:
                    try:
                        self.client.cancel(order.order_id)
                    except _CLOB_ERRORS as e2:
                        logger.error(
                            "Individual cancel %s failed: %s",
                            order.order_id, e2,
                        )
            for order in to_cancel:
                position.unreserve_for_order(
                    order.side, order.dollars_reserved, order.tokens_reserved,
                )
                cancelled += 1

        open_orders.clear()
        if cancelled > 0:
            logger.info("Cancelled %d pending GTC orders (by ID)", cancelled)
        return cancelled


# ---------------------------------------------------------------------------
# Trader — shared session runner
# ---------------------------------------------------------------------------


class Trader:
    """Drives the tick loop identically for simulation and live trading.

    The caller (simulator or bot) owns the Position and drives timing;
    Trader handles TickContext construction, strategy calls, and execution.
    """

    def __init__(self, strategy, executor, auto_exit_flag_file=None, sim_mode=False):
        # type: (Any, Executor, Optional[str], bool) -> None
        self.strategy = strategy
        self.executor = executor
        self._auto_exit_flag_file = auto_exit_flag_file
        self._sim_mode = sim_mode
        # Only skip history in sim mode when strategy explicitly doesn't use it
        self._uses_history = not sim_mode or getattr(strategy, 'uses_history', True)
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
        # Reusable TickContext for sim mode (avoids allocation per tick)
        self._sim_ctx = None  # type: Optional[TickContext]
        # Cache strategy's on_tick method to avoid attribute lookup per tick
        self._on_tick = strategy.on_tick
    def start_session(self, metadata, position, initial_btc_price, window_duration,
                      reference_balance=None, capture_decision_log=False):
        # type: (Dict[str, Any], Position, float, float, Optional[float], bool) -> None
        """Reset state and notify the strategy of a new session."""
        self._metadata = metadata
        self._position = position
        self._initial_btc_price = initial_btc_price
        self._window_duration = window_duration
        self._history = []
        self._trades = []
        self._tick_index = 0
        self._capture_decision_log = capture_decision_log
        self._decision_log = []
        self._open_orders = []
        self._last_tick_fills = []
        self.strategy._tick_logs = []
        self.strategy.on_session_start(metadata)
        # Pre-allocate reusable TickContext for sim mode
        if self._sim_mode:
            empty_history = self._history if self._uses_history else []
            self._sim_ctx = TickContext(
                tick_index=0,
                sample=None,
                history=empty_history,
                position=position,
                metadata=metadata,
                initial_btc_price=initial_btc_price,
                elapsed_fraction=0.0,
                max_buy_amount=position.cash,
                loss_limit_hit=False,
                open_orders=[],
            )

    def _apply_guardrails(self, action, sample=None):
        # type: (TradeAction, Optional[Dict[str, Any]]) -> TradeAction
        """Cap buys to available cash; block trades near session end."""
        if action.side == "hold":
            return action

        if action.side.startswith("cancel"):
            return action

        # Block all trades in the last few seconds of the session.
        # The CLOB orderbook gets deleted before the window officially ends,
        # so trades near the end fail with "orderbook does not exist" or
        # "not enough balance / allowance" (tokens already settling).
        if sample is not None and self._window_duration > 0:
            elapsed = sample.get("elapsed_seconds", 0.0)
            if elapsed >= self._window_duration - TRADE_CUTOFF_SECONDS:
                return HOLD

        if action.side.startswith("buy"):
            cash = self._position.cash
            if cash <= 0:
                return HOLD
            if action.amount > cash:
                action = TradeAction(side=action.side, amount=cash, price=action.price,
                                     expiration_seconds=action.expiration_seconds)

        return action

    def _is_auto_exit_enabled(self):
        # type: () -> bool
        """Check if auto-exit is enabled via flag file on disk."""
        if self._auto_exit_flag_file is not None:
            return os.path.exists(self._auto_exit_flag_file)
        return False

    def _check_auto_exit(self, sample):
        # type: (Dict[str, Any]) -> TradeAction
        """Sell held tokens when midpoint >= threshold to avoid claiming."""
        threshold = AUTO_EXIT_MIDPOINT_THRESHOLD
        pos = self._position

        if pos.up_tokens >= CLOB_MIN_ORDER_TOKENS:
            mid = sample.get("up_token_midpoint")
            if mid is not None and mid >= threshold:
                return TradeAction(side="sell_up", amount=pos.up_tokens)

        if pos.down_tokens >= CLOB_MIN_ORDER_TOKENS:
            mid = sample.get("down_token_midpoint")
            if mid is not None and mid >= threshold:
                return TradeAction(side="sell_down", amount=pos.down_tokens)

        return HOLD

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
                    fill.strategy_version = getattr(self.strategy, "version", None)
                    self._trades.append(fill)
                self._last_tick_fills = fills
            except NotImplementedError:
                pass

        elapsed_frac = (
            sample["elapsed_seconds"] / self._window_duration
            if self._window_duration > 0 else 0.0
        )
        elapsed_frac = min(elapsed_frac, 1.0)

        # 2. Build TickContext with open_orders snapshot
        # In sim mode, skip the defensive Position copy (strategies never mutate it)
        # and skip history if the strategy doesn't use it.
        if self._sim_mode:
            pos = self._position
            history = self._history if self._uses_history else []
            orders = list(self._open_orders) if self._open_orders else []
        else:
            pos = Position(
                cash=self._position.cash,
                up_tokens=self._position.up_tokens,
                down_tokens=self._position.down_tokens,
            )
            history = self._history
            orders = list(self._open_orders)
        ctx = TickContext(
            tick_index=self._tick_index,
            sample=sample,
            history=history,
            position=pos,
            metadata=self._metadata,
            initial_btc_price=self._initial_btc_price,
            elapsed_fraction=elapsed_frac,
            max_buy_amount=self._position.cash,
            loss_limit_hit=False,
            open_orders=orders,
        )

        # 3. Call strategy
        action = self.strategy.on_tick(ctx)

        # Drain strategy decision log buffer
        tick_logs = getattr(self.strategy, "_tick_logs", None)
        if tick_logs:
            if self._capture_decision_log and len(self._decision_log) < 5000:
                self._decision_log.append({
                    "tick": self._tick_index,
                    "msgs": list(tick_logs),
                })
            tick_logs.clear()

        # 4. Apply guardrails (settlement cooldown, cap buys to cash)
        action = self._apply_guardrails(action, sample)

        # 4b. Auto-exit: sell tokens at midpoint instead of waiting for resolution
        if action.side == "hold" and self._is_auto_exit_enabled():
            action = self._check_auto_exit(sample)

        # 5b. Route cancel actions — cancel pending orders on the target token
        if action.side.startswith("cancel"):
            token = "up" if action.side == "cancel_up" else "down"
            for order in list(self._open_orders):
                if order.token == token:
                    try:
                        self.executor.cancel_order(order, self._position)
                    except NotImplementedError:
                        pass
                    self._open_orders.remove(order)
            if self._uses_history:
                self._history.append(sample)
            self._tick_index += 1
            return None

        # 6. Route: limit order vs FAK
        #    - price set → GTC/GTD limit order (placed on the book)
        #    - no price → FAK at best_ask/best_bid (immediate fill)
        trade = None
        is_limit_order = (
            action.side != "hold"
            and action.price is not None
        )
        if is_limit_order:
            # Limit order path (GTC or GTD with expiration)
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
            # FAK path: immediate fill-or-cancel at best_ask/best_bid.
            trade = self.executor.execute_trade(
                action, self._tick_index, self._metadata, sample, self._position,
            )

        # Stamp strategy version on the trade
        if trade is not None:
            trade.strategy_version = getattr(self.strategy, "version", None)

        # Track successful trades (success is None for sim, True for live)
        if trade is not None and trade.success is not False:
            self._trades.append(trade)

        if self._uses_history:
            self._history.append(sample)
        self._tick_index += 1
        return trade

    def process_tick_sim(self, sample):
        # type: (Any) -> Optional[Trade]
        """Optimized tick processing for backtesting.

        Skips live-only overhead: no auto-exit file check, no defensive
        Position copy, reuses a pre-allocated TickContext. Still handles
        tick logs, limit orders, and guardrails correctly.
        """
        # Check fills on pending limit orders
        if self._open_orders:
            try:
                fills = self.executor.check_fills(
                    self._open_orders, sample, self._position,
                )
                for fill in fills:
                    fill.tick_index = self._tick_index
                    self._trades.append(fill)
            except NotImplementedError:
                pass

        # Update the reusable context in-place (no allocation)
        ctx = self._sim_ctx
        tick_idx = self._tick_index
        ctx.tick_index = tick_idx
        ctx.sample = sample
        # Use attribute access on Sample objects (faster than dict .get())
        elapsed_seconds = sample.elapsed_seconds
        elapsed_frac = elapsed_seconds / self._window_duration if self._window_duration > 0 else 0.0
        if elapsed_frac > 1.0:
            elapsed_frac = 1.0
        ctx.elapsed_fraction = elapsed_frac
        ctx.max_buy_amount = self._position.cash
        if self._open_orders:
            ctx.open_orders = list(self._open_orders)

        # Call strategy (cached method reference)
        action = self._on_tick(ctx)

        # Drain tick log buffer (strategies call self.log())
        tick_logs = self.strategy._tick_logs
        if tick_logs:
            tick_logs.clear()

        # Fast path: most ticks return HOLD
        if action.side == "hold":
            self._tick_index = tick_idx + 1
            return None

        # Guardrail: block trades near session end
        if elapsed_seconds >= self._window_duration - TRADE_CUTOFF_SECONDS:
            self._tick_index = tick_idx + 1
            return None

        # Cancel actions
        if action.side.startswith("cancel"):
            token = "up" if action.side == "cancel_up" else "down"
            for order in list(self._open_orders):
                if order.token == token:
                    try:
                        self.executor.cancel_order(order, self._position)
                    except NotImplementedError:
                        pass
                    self._open_orders.remove(order)
            self._tick_index = tick_idx + 1
            return None

        # Cap buy amount to available cash
        if action.side.startswith("buy"):
            cash = self._position.cash
            if cash <= 0:
                self._tick_index = tick_idx + 1
                return None
            if action.amount > cash:
                action = TradeAction(side=action.side, amount=cash,
                                     price=action.price,
                                     expiration_seconds=action.expiration_seconds)

        # Route: limit order vs FAK
        trade = None
        if action.price is not None:
            # Limit order path
            self._replace_order_on_token(action)
            try:
                order = self.executor.place_limit_order(
                    action, tick_idx, self._metadata, sample, self._position,
                )
                if order is not None:
                    self._open_orders.append(order)
            except NotImplementedError:
                action = TradeAction(side=action.side, amount=action.amount)
                trade = self.executor.execute_trade(
                    action, tick_idx, self._metadata, sample, self._position,
                )
        else:
            trade = self.executor.execute_trade(
                action, tick_idx, self._metadata, sample, self._position,
            )

        if trade is not None:
            trade.strategy_version = getattr(self.strategy, "version", None)
            self._trades.append(trade)

        self._tick_index = tick_idx + 1
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
    def open_orders(self):
        # type: () -> List[OpenOrder]
        """Currently pending GTC/GTD limit orders."""
        return list(self._open_orders)

    @property
    def decision_log(self):
        # type: () -> List[Dict[str, Any]]
        """Sparse decision log: only ticks where the strategy called log()."""
        return self._decision_log
