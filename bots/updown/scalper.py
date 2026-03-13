"""Momentum scalper: drift + velocity confirmation, unlimited round-trips.

Requires both BTC cumulative return from session start (drift) AND velocity
to agree before entering. Exits on conservative take-profit or stop-loss,
then re-enters on the next confirmed momentum signal. No cap on entries.

The dual-confirmation filter (drift + velocity) avoids noise entries that
plague pure-velocity strategies. The unlimited re-entry allows compounding
edge across multiple momentum bursts within a session.

v1: Pure velocity — overfit on 30 sessions, coin-flip on 500.
v2: Add drift confirmation (cumulative BTC return must also exceed threshold
    and agree with velocity direction). Tighter entry filter, stronger signal.
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import ols_slope_bps_timed
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Param, Strategy


class Scalper(Strategy):
    """Momentum scalper with drift+velocity confirmation, unlimited round-trips."""

    name = "Scalper"
    description = (
        "Dual-confirmation scalper. Requires both BTC drift and velocity to "
        "agree before entering. Conservative TP/SL, unlimited entries."
    )
    version = "2"
    requires_full_session = True

    # --- Tunable params ---
    return_threshold_bps = Param(default=4.0, min=2.0, max=10.0)
    velocity_window_ms = Param(default=2000, min=1000, max=5000)
    min_velocity_bps_s = Param(default=0.5, min=0.2, max=1.2)
    take_profit_pct = Param(default=0.06, min=0.03, max=0.15)
    stop_loss_pct = Param(default=0.04, min=0.02, max=0.10)
    max_buy_price = Param(default=0.75, min=0.60, max=0.85)
    min_elapsed = Param(default=0.10, min=0.05, max=0.30)
    cooldown_ms = Param(default=5000, min=2000, max=15000)

    # --- Fixed params ---
    no_new_after = Param(default=0.92, tunable=False)
    bet_fraction = Param(default=1.0, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reset_state()

    def _reset_state(self):
        self._btc_prices = []  # type: List[float]
        self._btc_times = []  # type: List[float]
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_trade_time_s = -999.0

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._reset_state()

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc_prices.append(btc)
            self._btc_times.append(ctx.elapsed_seconds)

        if up_mid is None or down_mid is None:
            return HOLD

        # --- Manage existing position ---
        held = self._held_side(ctx)
        if held is not None:
            exit_action = self._check_exit(
                ctx, up_mid, down_mid,
                self.stop_loss_pct, self.take_profit_pct,
                cooldown_ms=self.cooldown_ms,
            )
            if exit_action is not None:
                return exit_action
            return HOLD

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # --- No position: look for confirmed momentum entry ---

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # Cooldown between trades
        cooldown_s = self.cooldown_ms / 1000.0
        if ctx.elapsed_seconds - self._last_trade_time_s < cooldown_s:
            return HOLD

        # Need initial BTC price and enough history for velocity
        if ctx.initial_btc_price <= 0:
            return HOLD
        window_s = self.velocity_window_ms / 1000.0
        if ctx.elapsed_seconds < window_s or len(self._btc_prices) < 3:
            return HOLD

        # Signal 1: BTC cumulative return from session start
        current_btc = self._btc_prices[-1]
        btc_return_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR

        if abs(btc_return_bps) < self.return_threshold_bps:
            return HOLD

        # Signal 2: BTC velocity over rolling window
        cutoff = ctx.elapsed_seconds - window_s
        idx = next(
            (i for i in range(len(self._btc_times)) if self._btc_times[i] >= cutoff),
            len(self._btc_times),
        )
        velocity = ols_slope_bps_timed(self._btc_times[idx:], self._btc_prices[idx:])
        if velocity is None:
            return HOLD

        if abs(velocity) < self.min_velocity_bps_s:
            return HOLD

        # Both signals must agree on direction
        if (btc_return_bps > 0) != (velocity > 0):
            return HOLD

        # Determine side
        if btc_return_bps > 0:
            side, side_label, mid = "buy_up", "up", up_mid
        else:
            side, side_label, mid = "buy_down", "down", down_mid

        # Price guard — don't buy expensive tokens
        if mid > self.max_buy_price:
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = mid
        self._last_trade_time_s = ctx.elapsed_seconds

        dollars = ctx.position.cash * self.bet_fraction

        self.log("BUY {} #{}: ret={:.1f}bps vel={:.3f}bps/s mid={:.3f} ${:.2f} at {:.0f}%".format(
            side_label.upper(), self._entry_count, btc_return_bps, velocity,
            mid, dollars, ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=mid, cancel_after_s=10)
