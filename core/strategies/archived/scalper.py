"""Mean reversion scalper on token midpoints. Buy dips, sell on recovery or stop-loss."""

from collections import deque

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class Scalper(Strategy):
    """Buy when token midpoint dips below rolling mean, sell on profit target or stop-loss.

    Tracks rolling mean of Up token midpoint. Enters when midpoint drops below
    mean - entry_deviation, exits on profit target or stop-loss. Repeats.

    Params:
        lookback: number of ticks for rolling mean
        entry_deviation: midpoint drop below mean to trigger entry
        profit_target: midpoint rise from entry price to take profit
        stop_loss: midpoint drop from entry price to stop out
        bet_fraction: fraction of cash to bet per entry
        max_trades: maximum total trade actions per session
    """

    name = "Scalper"
    description = "Mean reversion on token midpoints. Buys when a token's midpoint dips below its rolling average, then exits on a profit target or stop-loss. Can make multiple quick round-trips per session."

    def __init__(self, lookback=50, entry_deviation=0.02, profit_target=0.02,
                 stop_loss=0.05, bet_fraction=1.0, max_trades=12):
        self.lookback = lookback
        self.entry_deviation = entry_deviation
        self.profit_target = profit_target
        self.stop_loss = stop_loss
        self.bet_fraction = bet_fraction
        self.max_trades = max_trades
        self._params = {
            "lookback": lookback,
            "entry_deviation": entry_deviation,
            "profit_target": profit_target,
            "stop_loss": stop_loss,
            "bet_fraction": bet_fraction,
            "max_trades": max_trades,
        }
        # Per-session state (bounded deques — only last lookback values kept)
        self._up_mids = deque(maxlen=lookback)  # type: deque
        self._down_mids = deque(maxlen=lookback)  # type: deque
        self._entry_midpoint = 0.0
        self._trade_count = 0

    def on_session_start(self, metadata):
        self._up_mids = deque(maxlen=self.lookback)
        self._down_mids = deque(maxlen=self.lookback)
        self._entry_midpoint = 0.0
        self._trade_count = 0

    def _held_side(self, ctx):
        """Return which side we hold, or None."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _rolling_mean(self, values):
        """Compute mean of values in the deque (already bounded to lookback)."""
        if not values:
            return None
        return sum(values) / len(values)

    def on_tick(self, ctx):
        if self._trade_count >= self.max_trades:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        self._up_mids.append(up_mid)
        self._down_mids.append(down_mid)

        # Need enough data for rolling mean
        if len(self._up_mids) < self.lookback:
            return HOLD

        # Check exits first
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            pnl = current_mid - self._entry_midpoint

            # Take profit
            if pnl >= self.profit_target:
                return self._exit(ctx)

            # Stop loss
            if pnl <= -self.stop_loss:
                return self._exit(ctx)

            return HOLD

        # No position — look for entry signals
        up_mean = self._rolling_mean(self._up_mids)
        down_mean = self._rolling_mean(self._down_mids)
        if up_mean is None or down_mean is None:
            return HOLD

        # Up token dipped below its mean — buy Up (mean reversion: expect recovery)
        if up_mid < up_mean - self.entry_deviation:
            return self._enter("up", up_mid, ctx)

        # Down token dipped below its mean — buy Down
        if down_mid < down_mean - self.entry_deviation:
            return self._enter("down", down_mid, ctx)

        return HOLD

    def _enter(self, direction, midpoint, ctx):
        """Enter a position."""
        if self._trade_count >= self.max_trades:
            return HOLD

        dollars = ctx.position.cash * self.bet_fraction
        if dollars <= 0:
            return HOLD

        side = "buy_up" if direction == "up" else "buy_down"
        self._entry_midpoint = midpoint
        self._trade_count += 1
        return TradeAction(side=side, amount=dollars)

    def _exit(self, ctx):
        """Exit current position."""
        held = self._held_side(ctx)
        sell_side = "sell_up" if held == "up" else "sell_down"
        tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
        self._entry_midpoint = 0.0
        self._trade_count += 1
        return TradeAction(side=sell_side, amount=tokens)

    def param_grid(self):
        return {
            "lookback": [30, 50, 100],
            "entry_deviation": [0.01, 0.02, 0.03],
            "profit_target": [0.01, 0.02, 0.04],
            "stop_loss": [0.03, 0.05, 0.08],
            "bet_fraction": [0.2, 0.3, 0.5],
        }
