"""Momentum entry with trailing stop management. Re-enter after cooldown."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class TrailingStopMomentum(Strategy):
    """Enter on BTC momentum, manage with trailing stop. Re-enter after cooldown.

    Entry: BTC move exceeds momentum_bps from initial price.
    Stop: Fixed stop before min_profit reached; trailing stop (peak - trail_pct) after.
    Exit on stop trigger, can re-enter after cooldown_ticks if momentum persists.

    Params:
        momentum_bps: BTC move threshold for entry (basis points)
        trail_pct: trailing stop distance as fraction of midpoint (e.g. 0.03)
        min_profit: midpoint gain before trailing stop activates (fixed stop before)
        fixed_stop: stop-loss distance before trailing activates
        cooldown_ticks: ticks to wait after exit before re-entry
        bet_fraction: fraction of cash to bet per entry
        max_trades: maximum total trade actions per session
    """

    name = "TrailingStopMomentum"
    description = "Enters on BTC momentum, then manages the position with a trailing stop. Uses a fixed stop initially, switches to trailing after minimum profit is reached. Can re-enter after a cooldown period."

    def __init__(self, momentum_bps=5, trail_pct=0.03, min_profit=0.02,
                 fixed_stop=0.04, cooldown_ticks=200, bet_fraction=1.0, max_trades=8):
        self.momentum_bps = momentum_bps
        self.trail_pct = trail_pct
        self.min_profit = min_profit
        self.fixed_stop = fixed_stop
        self.cooldown_ticks = cooldown_ticks
        self.bet_fraction = bet_fraction
        self.max_trades = max_trades
        self._params = {
            "momentum_bps": momentum_bps,
            "trail_pct": trail_pct,
            "min_profit": min_profit,
            "fixed_stop": fixed_stop,
            "cooldown_ticks": cooldown_ticks,
            "bet_fraction": bet_fraction,
            "max_trades": max_trades,
        }
        # Per-session state
        self._virtual_side = None  # type: str
        self._virtual_tokens = 0.0
        self._entry_midpoint = 0.0
        self._peak_midpoint = 0.0
        self._trade_count = 0
        self._ticks_since_exit = None  # type: int

    def on_session_start(self, metadata):
        self._virtual_side = None
        self._virtual_tokens = 0.0
        self._entry_midpoint = 0.0
        self._peak_midpoint = 0.0
        self._trade_count = 0
        self._ticks_since_exit = None

    def on_tick(self, ctx):
        if self._trade_count >= self.max_trades:
            return HOLD

        btc_price = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if btc_price is None or up_mid is None or down_mid is None:
            return HOLD

        # Track cooldown
        if self._ticks_since_exit is not None:
            self._ticks_since_exit += 1

        # Manage existing position
        if self._virtual_side is not None:
            current_mid = up_mid if self._virtual_side == "up" else down_mid
            pnl = current_mid - self._entry_midpoint

            # Update peak
            if current_mid > self._peak_midpoint:
                self._peak_midpoint = current_mid

            # Determine stop level
            if pnl >= self.min_profit:
                # Trailing stop: peak - trail_pct
                stop_level = self._peak_midpoint - self.trail_pct
            else:
                # Fixed stop
                stop_level = self._entry_midpoint - self.fixed_stop

            if current_mid <= stop_level:
                return self._exit()

            return HOLD

        # No position — check entry conditions
        # Respect cooldown
        if self._ticks_since_exit is not None and self._ticks_since_exit < self.cooldown_ticks:
            return HOLD

        if ctx.initial_btc_price <= 0:
            return HOLD

        move_bps = ((btc_price - ctx.initial_btc_price) / ctx.initial_btc_price) * 10000.0

        if move_bps > self.momentum_bps:
            return self._enter("up", up_mid, ctx)
        elif move_bps < -self.momentum_bps:
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
        if midpoint > 0:
            self._virtual_tokens = dollars / midpoint
        self._virtual_side = direction
        self._entry_midpoint = midpoint
        self._peak_midpoint = midpoint
        self._ticks_since_exit = None
        self._trade_count += 1
        return TradeAction(side=side, amount=dollars)

    def _exit(self):
        """Exit current position via trailing/fixed stop."""
        sell_side = "sell_up" if self._virtual_side == "up" else "sell_down"
        tokens = self._virtual_tokens
        self._virtual_side = None
        self._virtual_tokens = 0.0
        self._entry_midpoint = 0.0
        self._peak_midpoint = 0.0
        self._ticks_since_exit = 0
        self._trade_count += 1
        return TradeAction(side=sell_side, amount=tokens)

    def param_grid(self):
        return {
            "momentum_bps": [3, 5, 10],
            "trail_pct": [0.02, 0.04, 0.08],
            "fixed_stop": [0.03, 0.05, 0.08],
            "cooldown_ticks": [100, 300, 600],
            "bet_fraction": [0.25, 0.4, 0.6],
        }
