"""Regime-gated trend follower: only trades in trending + medium/high vol.

Simple drift-based entry (like DriftSniper) but gated by regime
classification. Activates only when recent sessions show trending
autocorrelation AND sufficient volatility. In low-vol or mean-reverting
regimes, sits out entirely.

The idea: trend-following works well in trending markets but bleeds in
mean-reverting or flat markets. By detecting the regime from prior
sessions and only activating when conditions match, we avoid the losses
that drag down all-time Sharpe while capturing the strong periods.

v1: Initial implementation.
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import ols_slope_bps_timed
from core.strategies.base import Param
from core.strategies.regime_gated import RegimeGatedStrategy
from core.trader import HOLD, TradeAction, TickContext


class TrendDrift(RegimeGatedStrategy):
    """Trend-following entry, only active in trending + medium/high vol."""

    name = "TrendDrift"
    description = (
        "Regime-gated trend follower. Only activates when prior sessions "
        "show trending autocorrelation and medium-to-high volatility. "
        "Enters on BTC drift + velocity confirmation."
    )
    version = "2"
    requires_full_session = True

    # --- Regime gate (NOT tunable) ---
    required_vol = {"medium", "high"}
    required_trend = {"trending"}
    skip_stale_data = True

    # --- Tunable params ---
    return_threshold_bps = Param(default=12.0, min=3.0, max=15.0)
    min_elapsed = Param(default=0.55, min=0.20, max=0.55)
    max_price = Param(default=0.75, min=0.45, max=0.75)
    velocity_window_ms = Param(default=2000, min=1000, max=5000)
    min_velocity_bps_s = Param(default=0.15, min=0.15, max=0.60)

    # --- Fixed params ---
    no_new_after = Param(default=0.95, tunable=False)

    def _on_session_start_inner(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._btc_prices = []  # type: List[float]
        self._btc_times = []   # type: List[float]
        self._entry_midpoint = 0.0
        self._entry_count = 0

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc_prices.append(btc)
            self._btc_times.append(ctx.elapsed_seconds)

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # Already holding — hold to resolution (no stop-loss)
        if self._held_side(ctx) is not None:
            return HOLD

        self._check_expired_entry(ctx)
        if self._entry_count > 0:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        window_s = self.velocity_window_ms / 1000.0
        if ctx.initial_btc_price <= 0 or ctx.elapsed_seconds < window_s:
            return HOLD

        # BTC drift from session start
        current_btc = self._btc_prices[-1]
        drift_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR

        if abs(drift_bps) < self.return_threshold_bps:
            return HOLD

        # Velocity confirmation
        cutoff = ctx.elapsed_seconds - window_s
        idx = next((i for i in range(len(self._btc_times))
                     if self._btc_times[i] >= cutoff), len(self._btc_times))
        velocity = ols_slope_bps_timed(self._btc_times[idx:], self._btc_prices[idx:])
        if velocity is None or abs(velocity) < self.min_velocity_bps_s:
            return HOLD

        if (drift_bps > 0) != (velocity > 0):
            return HOLD

        # Price check
        direction = "up" if drift_bps > 0 else "down"
        signal_mid = up_mid if direction == "up" else down_mid
        if signal_mid > self.max_price:
            return HOLD

        # Enter
        self._entry_count += 1
        self._entry_midpoint = signal_mid
        side = "buy_up" if direction == "up" else "buy_down"

        self.log("ENTRY {}: drift={:.1f}bps vel={:.3f} mid={:.3f} at {:.0f}%".format(
            side, drift_bps, velocity, signal_mid, ctx.elapsed_fraction * 100))

        return TradeAction(
            side=side,
            amount=ctx.position.cash,
            price=signal_mid,
            cancel_after_s=10,
        )
