"""Enter based on BTC price momentum within the session.

Tracks BTC return from session start and its velocity (slope over recent
ticks). Enters when BTC has moved enough AND velocity confirms direction.
Uses the actual underlying signal rather than the derivative (token midpoint).
"""

import math
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class DriftSniper(Strategy):
    """Buy based on BTC price drift from session start.

    Tracks BTC return (bps from initial price) and rolling velocity.
    Enters when return exceeds threshold and velocity confirms direction.

    Params:
        return_threshold_bps: min |BTC return| in bps to trigger entry
        velocity_window: ticks for velocity (slope) calculation
        min_velocity_bps: min velocity magnitude to confirm momentum
        min_elapsed: earliest elapsed fraction to allow entry
        max_price: don't buy token above this midpoint
        stop_loss_pct: sell if midpoint drops this fraction below entry
        bet_fraction: fraction of cash to bet
    """

    name = "DriftSniper"
    description = "Buys when BTC price drifts significantly from session start with confirming velocity."
    # v4: 2-stage Sharpe→Kelly optimization. Kelly-positive variant.
    # Sharpe 0.265, 37% trade rate, ~60% win rate when trading.
    # Kelly_f=1.0, deploying at quarter-Kelly (25% → rounded to 20%).
    # Lower return threshold (4bps) trades more often; tight stop-loss (6%)
    # cuts losses early. Short velocity window (36 ticks ~2s) for fast signal.
    version = "4"
    requires_full_session = True

    def __init__(
        self,
        return_threshold_bps=4.0,
        velocity_window=36,
        min_velocity_bps=0.02,
        min_elapsed=0.50,
        max_price=0.92,
        stop_loss_pct=0.06,  # type: Optional[float]
        bet_fraction=1.0,
    ):
        self.return_threshold_bps = return_threshold_bps
        self.velocity_window = velocity_window
        self.min_velocity_bps = min_velocity_bps
        self.min_elapsed = min_elapsed
        self.max_price = max_price
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self._params = {
            "return_threshold_bps": return_threshold_bps,
            "velocity_window": velocity_window,
            "min_velocity_bps": min_velocity_bps,
            "min_elapsed": min_elapsed,
            "max_price": max_price,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
        }

        self._btc_prices = []  # type: List[float]
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999
        self._entered = False

    def on_session_start(self, metadata):
        self._btc_prices = []
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999
        self._entered = False

    def _has_position(self, ctx):
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _held_side(self, ctx):
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _compute_velocity_bps(self):
        # type: () -> Optional[float]
        """Slope of BTC prices over last velocity_window ticks, in bps/tick."""
        n = self.velocity_window
        if len(self._btc_prices) < n:
            return None
        recent = self._btc_prices[-n:]
        base = recent[0]
        if base <= 0:
            return None
        # OLS slope
        sum_x = n * (n - 1) / 2.0
        sum_x2 = n * (n - 1) * (2 * n - 1) / 6.0
        sum_y = sum(recent)
        sum_xy = sum(i * y for i, y in enumerate(recent))
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return None
        slope = (n * sum_xy - sum_x * sum_y) / denom
        return (slope / base) * 10000.0  # bps per tick

    def on_tick(self, ctx):
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc_prices.append(btc)

        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — stop-loss
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0 and self.stop_loss_pct is not None:
                if current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                    if ctx.tick_index - self._last_sell_tick < 36:
                        return HOLD
                    self._last_sell_tick = ctx.tick_index
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                        current_mid,
                        (1.0 - current_mid / self._entry_midpoint) * 100,
                        self._entry_midpoint,
                    ))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)
            return HOLD

        if self._entered:
            return HOLD

        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        # Need initial BTC price and enough history
        if ctx.initial_btc_price <= 0 or len(self._btc_prices) < self.velocity_window:
            return HOLD

        # BTC return from session start
        current_btc = self._btc_prices[-1]
        btc_return_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * 10000.0

        if abs(btc_return_bps) < self.return_threshold_bps:
            return HOLD

        # Velocity must confirm direction
        velocity = self._compute_velocity_bps()
        if velocity is None:
            return HOLD

        if abs(velocity) < self.min_velocity_bps:
            return HOLD

        # Return and velocity must agree on direction
        if (btc_return_bps > 0) != (velocity > 0):
            return HOLD

        # BTC going up → buy UP, BTC going down → buy DOWN
        if btc_return_bps > 0:
            if up_mid <= self.max_price:
                self._entered = True
                self._entry_midpoint = up_mid
                self.log("BUY UP: btc_ret={:.1f}bps vel={:.3f}bps/tick mid={:.3f} at {:.0f}%".format(
                    btc_return_bps, velocity, up_mid, ctx.elapsed_fraction * 100,
                ))
                return TradeAction(
                    side="buy_up",
                    amount=ctx.position.cash * self.bet_fraction,
                    price=up_mid,
                )
        else:
            if down_mid <= self.max_price:
                self._entered = True
                self._entry_midpoint = down_mid
                self.log("BUY DOWN: btc_ret={:.1f}bps vel={:.3f}bps/tick mid={:.3f} at {:.0f}%".format(
                    btc_return_bps, velocity, down_mid, ctx.elapsed_fraction * 100,
                ))
                return TradeAction(
                    side="buy_down",
                    amount=ctx.position.cash * self.bet_fraction,
                    price=down_mid,
                )

        return HOLD

    def param_grid(self):
        return {
            "return_threshold_bps": [6.0, 8.0, 10.0, 12.0],
            "velocity_window": [108, 126, 144, 162],
            "min_velocity_bps": [0.02, 0.025, 0.03, 0.035],
            "min_elapsed": [0.25, 0.35, 0.45, 0.55],
            "max_price": [0.90, 0.92, 0.94],
            "stop_loss_pct": [None, 0.08],
        }
