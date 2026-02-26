"""Dual EMA crossover on BTC price. Flips between Up and Down on crossovers."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class EmaCrossover(Strategy):
    """Classic dual exponential moving average crossover.

    Fast EMA crosses above slow EMA -> buy Up.
    Fast EMA crosses below slow EMA -> sell, then buy Down.
    Can flip positions on crossover reversals.

    Params:
        fast_span: fast EMA span (ticks)
        slow_span: slow EMA span (ticks)
        bet_fraction: fraction of cash to bet per entry
        max_trades: maximum total trade actions per session
    """

    name = "EmaCrossover"
    description = "Classic dual EMA crossover on BTC price. When the fast EMA crosses above the slow, buys Up; when it crosses below, sells and buys Down. Can flip positions multiple times per session."

    def __init__(self, fast_span=25, slow_span=150, bet_fraction=1.0, max_trades=10):
        self.fast_span = fast_span
        self.slow_span = slow_span
        self.bet_fraction = bet_fraction
        self.max_trades = max_trades
        self._params = {
            "fast_span": fast_span,
            "slow_span": slow_span,
            "bet_fraction": bet_fraction,
            "max_trades": max_trades,
        }
        # Per-session state
        self._fast_ema = None  # type: float
        self._slow_ema = None  # type: float
        self._fast_alpha = 2.0 / (fast_span + 1)
        self._slow_alpha = 2.0 / (slow_span + 1)
        self._prev_signal = None  # type: str  # "up" or "down"
        self._trade_count = 0
        self._warmup_done = False

    def on_session_start(self, metadata):
        self._fast_ema = None
        self._slow_ema = None
        self._prev_signal = None
        self._trade_count = 0
        self._warmup_done = False

    def _held_side(self, ctx):
        """Return which side we hold, or None."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        if self._trade_count >= self.max_trades:
            return HOLD

        btc_price = ctx.sample.get("btc_price_usd")
        if btc_price is None or btc_price <= 0:
            return HOLD

        # Initialize EMAs on first tick
        if self._fast_ema is None:
            self._fast_ema = btc_price
            self._slow_ema = btc_price
            return HOLD

        # Update EMAs
        self._fast_ema = self._fast_alpha * btc_price + (1 - self._fast_alpha) * self._fast_ema
        self._slow_ema = self._slow_alpha * btc_price + (1 - self._slow_alpha) * self._slow_ema

        # Need enough ticks for EMAs to be meaningful
        if ctx.tick_index < self.slow_span:
            # Track signal direction during warmup for crossover detection
            if self._fast_ema > self._slow_ema:
                self._prev_signal = "up"
            elif self._fast_ema < self._slow_ema:
                self._prev_signal = "down"
            return HOLD

        # Determine current signal
        if self._fast_ema > self._slow_ema:
            current_signal = "up"
        elif self._fast_ema < self._slow_ema:
            current_signal = "down"
        else:
            return HOLD

        held = self._held_side(ctx)

        # Detect crossover
        if self._prev_signal is not None and current_signal != self._prev_signal:
            self._prev_signal = current_signal

            # Sell current position first
            if held is not None:
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._trade_count += 1
                return TradeAction(side=sell_side, amount=tokens)

            # No position to sell — enter directly
            return self._enter(current_signal, ctx)

        # If we just sold on crossover and have no position, enter on next tick
        if self._prev_signal is not None and held is None and self._trade_count > 0:
            return self._enter(current_signal, ctx)

        # First entry (no prior crossover, just initial signal after warmup)
        if held is None and self._prev_signal is None:
            self._prev_signal = current_signal
            return self._enter(current_signal, ctx)

        self._prev_signal = current_signal
        return HOLD

    def _enter(self, direction, ctx):
        """Enter a position in the given direction."""
        if self._trade_count >= self.max_trades:
            return HOLD

        dollars = ctx.position.cash * self.bet_fraction
        if dollars <= 0:
            return HOLD

        side = "buy_up" if direction == "up" else "buy_down"
        self._trade_count += 1
        return TradeAction(side=side, amount=dollars)

    def param_grid(self):
        return {
            "fast_span": [10, 25, 50],
            "slow_span": [75, 150, 300],
            "bet_fraction": [0.2, 0.3, 0.5],
        }
