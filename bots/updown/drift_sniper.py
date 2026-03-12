"""Enter based on BTC price momentum within the session.

Tracks BTC return from session start and its velocity (slope over recent
ticks). Enters when BTC has moved enough AND velocity confirms direction.
Uses the actual underlying signal rather than the derivative (token midpoint).
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import ols_slope_bps
from core.trader import HOLD, TradeAction
from core.strategies.base import Param, Strategy


class DriftSniper(Strategy):
    """Buy based on BTC price drift from session start."""

    name = "DriftSniper"
    description = "Buys when BTC price drifts significantly from session start with confirming velocity."
    version = "7"
    requires_full_session = True

    # --- Tunable params ---
    return_threshold_bps = Param(default=4.0, min=2.0, max=15.0)
    velocity_window = Param(default=36, min=18, max=180)
    min_velocity_bps = Param(default=0.02, min=0.01, max=0.05)
    min_elapsed = Param(default=0.50, min=0.20, max=0.70)
    max_price = Param(default=0.92, min=0.85, max=0.96)
    stop_loss_pct = Param(default=0.06, min=0.04, max=0.15, nullable=True)

    # --- Fixed params ---
    no_new_after = Param(default=0.95, tunable=False)
    bet_fraction = Param(default=1.0, tunable=False)

    def __init__(self, **kwargs):
        defs = self.param_defs()
        for name, p in defs.items():
            setattr(self, name, kwargs.get(name, p.default))
        self._params = {name: getattr(self, name) for name in defs}

        self._btc_prices = []  # type: List[float]
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_tick = -999

    def on_session_start(self, metadata):
        self._btc_prices = []
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_tick = -999

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
            exit_action = self._check_exit(ctx, up_mid, down_mid, self.stop_loss_pct)
            return exit_action if exit_action is not None else HOLD

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # Only one entry per session
        if self._entry_count > 0:
            return HOLD

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # Need initial BTC price and enough history
        if ctx.initial_btc_price <= 0 or len(self._btc_prices) < self.velocity_window:
            return HOLD

        # BTC return from session start
        current_btc = self._btc_prices[-1]
        btc_return_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR

        if abs(btc_return_bps) < self.return_threshold_bps:
            return HOLD

        # Velocity must confirm direction
        velocity = ols_slope_bps(self._btc_prices, self.velocity_window)
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
                self._entry_count += 1
                self._entry_midpoint = up_mid
                self.log("BUY UP: btc_ret={:.1f}bps vel={:.3f}bps/tick mid={:.3f} at {:.0f}%".format(
                    btc_return_bps, velocity, up_mid, ctx.elapsed_fraction * 100,
                ))
                return TradeAction(
                    side="buy_up",
                    amount=ctx.position.cash * self.bet_fraction,
                    price=up_mid,
                    expiration_seconds=10,
                )
        else:
            if down_mid <= self.max_price:
                self._entry_count += 1
                self._entry_midpoint = down_mid
                self.log("BUY DOWN: btc_ret={:.1f}bps vel={:.3f}bps/tick mid={:.3f} at {:.0f}%".format(
                    btc_return_bps, velocity, down_mid, ctx.elapsed_fraction * 100,
                ))
                return TradeAction(
                    side="buy_down",
                    amount=ctx.position.cash * self.bet_fraction,
                    price=down_mid,
                    expiration_seconds=10,
                )

        return HOLD

