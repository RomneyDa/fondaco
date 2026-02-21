"""Rolling OLS regression slope on BTC price. Enter on strong trend, exit on reversal."""

from collections import deque

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class OlsSlope(Strategy):
    """Rolling linear regression on BTC prices. Slope (bps/sec) indicates trend.

    Enter when slope exceeds threshold, exit when slope reverses past exit threshold.
    Can re-enter after exit if slope re-establishes.

    Params:
        window_size: number of ticks for the regression window
        slope_threshold_bps: minimum abs(slope) in bps/sec to enter
        exit_slope_bps: slope reversal threshold to exit (fraction of entry threshold)
        bet_fraction: fraction of cash to bet per entry
        max_trades: maximum total trade actions per session
    """

    name = "OlsSlope"
    description = "Fits a rolling linear regression to recent BTC prices. Enters when the slope (trend strength in bps/tick) exceeds a threshold, exits when slope reverses. Can re-enter on new trends."

    def __init__(self, window_size=50, slope_threshold_bps=0.02, exit_slope_bps=0.0,
                 bet_fraction=0.4, max_trades=10):
        self.window_size = window_size
        self.slope_threshold_bps = slope_threshold_bps
        self.exit_slope_bps = exit_slope_bps
        self.bet_fraction = bet_fraction
        self.max_trades = max_trades
        self._params = {
            "window_size": window_size,
            "slope_threshold_bps": slope_threshold_bps,
            "exit_slope_bps": exit_slope_bps,
            "bet_fraction": bet_fraction,
            "max_trades": max_trades,
        }
        # Per-session state (bounded deque — only last window_size prices kept)
        self._prices = deque(maxlen=window_size)  # type: deque
        self._virtual_side = None  # type: str
        self._virtual_tokens = 0.0
        self._trade_count = 0
        # Precompute OLS denominators for the fixed window
        n = window_size
        self._sum_x = n * (n - 1) / 2.0
        self._sum_x2 = n * (n - 1) * (2 * n - 1) / 6.0
        self._denom = n * self._sum_x2 - self._sum_x * self._sum_x

    def on_session_start(self, metadata):
        self._prices = deque(maxlen=self.window_size)
        self._virtual_side = None
        self._virtual_tokens = 0.0
        self._trade_count = 0

    def _compute_slope_bps(self):
        """Compute OLS slope of recent prices in bps per tick.

        slope = (N * sum(i*p) - sum(i) * sum(p)) / (N * sum(i^2) - sum(i)^2)
        Then convert to bps relative to mean price.
        """
        n = self.window_size
        if len(self._prices) < n or self._denom == 0:
            return None

        sum_p = 0.0
        sum_xp = 0.0
        for i, p in enumerate(self._prices):
            sum_p += p
            sum_xp += i * p

        slope = (n * sum_xp - self._sum_x * sum_p) / self._denom
        mean_price = sum_p / n
        if mean_price <= 0:
            return None

        # Convert to bps per tick
        return (slope / mean_price) * 10000.0

    def on_tick(self, ctx):
        if self._trade_count >= self.max_trades:
            return HOLD

        btc_price = ctx.sample.get("btc_price_usd")
        if btc_price is None or btc_price <= 0:
            return HOLD

        self._prices.append(btc_price)

        # Need full window
        if len(self._prices) < self.window_size:
            return HOLD

        slope_bps = self._compute_slope_bps()
        if slope_bps is None:
            return HOLD

        # Check for exit first
        if self._virtual_side is not None:
            should_exit = False
            if self._virtual_side == "up" and slope_bps < -self.exit_slope_bps:
                should_exit = True
            elif self._virtual_side == "down" and slope_bps > self.exit_slope_bps:
                should_exit = True

            if should_exit:
                sell_side = "sell_up" if self._virtual_side == "up" else "sell_down"
                tokens = self._virtual_tokens
                self._virtual_side = None
                self._virtual_tokens = 0.0
                self._trade_count += 1
                return TradeAction(side=sell_side, amount=tokens)

            return HOLD

        # No position — check for entry
        if slope_bps > self.slope_threshold_bps:
            return self._enter("up", ctx)
        elif slope_bps < -self.slope_threshold_bps:
            return self._enter("down", ctx)

        return HOLD

    def _enter(self, direction, ctx):
        """Enter a position in the given direction."""
        if self._trade_count >= self.max_trades:
            return HOLD

        dollars = ctx.position.cash * self.bet_fraction
        if dollars <= 0:
            return HOLD

        side = "buy_up" if direction == "up" else "buy_down"
        midpoint_key = "up_token_midpoint" if direction == "up" else "down_token_midpoint"
        midpoint = ctx.sample.get(midpoint_key)
        if midpoint is not None and midpoint > 0:
            self._virtual_tokens = dollars / midpoint
        self._virtual_side = direction
        self._trade_count += 1
        return TradeAction(side=side, amount=dollars)

    def param_grid(self):
        return {
            "window_size": [25, 50, 100],
            "slope_threshold_bps": [0.01, 0.02, 0.05, 0.1],
            "bet_fraction": [0.25, 0.4, 0.6],
        }
