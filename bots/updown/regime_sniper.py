"""Drift-based entry with session filtering and adaptive pricing.

Improvement over DriftSniper based on empirical findings from 500+ sessions:
1. NO stop-loss: data shows winning side spends 25% of time below 0.50.
   Stop-losses trigger on temporary dips that recover, locking in losses
   on eventual winners. With strict entry, hold to resolution.
2. SLIDING max_price: early entries get cheap tokens (high payoff), later
   entries accept higher prices (more confirmed). This captures both
   early strong-signal and late confirmed-signal opportunities.
   effective_max_price = max_price + price_slope * (elapsed - min_elapsed)
3. Consistency filter: requires the winning side to have held the lead
   for most of the recent window. Filters choppy/reversing sessions.
4. BTC displacement + velocity: requires BTC to have moved significantly
   from session open AND velocity to confirm the direction.

v4: Sliding max_price for adaptive entry timing.
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import ols_slope_bps_timed
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Param, Strategy


class RegimeSniper(Strategy):
    """Drift-based entry with sliding price and consistency filter."""

    name = "RegimeSniper"
    description = (
        "Enters on BTC displacement + velocity + midpoint consistency. "
        "Sliding max_price allows cheap early entry and confirmed late "
        "entry. Holds to resolution — no stop-loss."
    )
    version = "7"
    requires_full_session = True

    # --- Tunable params ---
    min_elapsed = Param(default=0.20, min=0.20, max=0.40)
    min_displacement_bps = Param(default=12.0, min=5.0, max=12.0)
    min_consistency = Param(default=0.70, min=0.50, max=0.70)
    max_price = Param(default=0.48, min=0.45, max=0.55)
    price_slope = Param(default=0.60, min=0.30, max=0.60)

    # --- Fixed params ---
    consistency_window_ms = Param(default=5500, tunable=False)
    velocity_window_ms = Param(default=1000, tunable=False)
    min_velocity_bps_s = Param(default=0.55, tunable=False)
    no_new_after = Param(default=0.95, tunable=False)
    bet_fraction = Param(default=1.0, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reset_state()

    def _reset_state(self):
        self._btc_prices = []  # type: List[float]
        self._btc_times = []  # type: List[float]
        self._midpoint_signs = []  # type: List[int]
        self._midpoint_times = []  # type: List[float]
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_time_s = -999.0

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._reset_state()

    def _compute_consistency(self, direction, elapsed_seconds):
        # type: (str, float) -> float
        """Fraction of recent samples where midpoint favored the given direction."""
        cutoff = elapsed_seconds - self.consistency_window_ms / 1000.0
        recent = [s for t, s in zip(self._midpoint_times, self._midpoint_signs) if t >= cutoff]
        if not recent:
            return 0.0
        if direction == "up":
            count = sum(1 for v in recent if v > 0)
        else:
            count = sum(1 for v in recent if v < 0)
        return count / len(recent)

    def _effective_max_price(self, elapsed_fraction):
        # type: (float) -> float
        """Sliding max price: increases with time elapsed."""
        time_past_min = max(elapsed_fraction - self.min_elapsed, 0.0)
        return min(self.max_price + self.price_slope * time_past_min, 0.92)

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc_prices.append(btc)
            self._btc_times.append(ctx.elapsed_seconds)

        if up_mid is not None:
            self._midpoint_signs.append(1 if up_mid > 0.50 else -1)
            self._midpoint_times.append(ctx.elapsed_seconds)

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Position held: hold to resolution (no stop-loss) ---
        if self._held_side(ctx) is not None:
            return HOLD

        # Buy order expired without filling
        self._check_expired_entry(ctx)

        # Single entry per session
        if self._entry_count > 0:
            return HOLD

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        velocity_window_s = self.velocity_window_ms / 1000.0
        if ctx.initial_btc_price <= 0 or ctx.elapsed_seconds < velocity_window_s:
            return HOLD

        # ---- Signal 1: BTC displacement ----
        current_btc = self._btc_prices[-1]
        displacement_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR

        if abs(displacement_bps) < self.min_displacement_bps:
            return HOLD

        direction = "up" if displacement_bps > 0 else "down"

        # ---- Signal 2: BTC velocity confirms ----
        cutoff = ctx.elapsed_seconds - velocity_window_s
        idx = next((i for i in range(len(self._btc_times)) if self._btc_times[i] >= cutoff), len(self._btc_times))
        velocity = ols_slope_bps_timed(self._btc_times[idx:], self._btc_prices[idx:])
        if velocity is None:
            return HOLD

        if abs(velocity) < self.min_velocity_bps_s:
            return HOLD

        if (displacement_bps > 0) != (velocity > 0):
            return HOLD

        # ---- Signal 3: Midpoint consistency ----
        consistency = self._compute_consistency(direction, ctx.elapsed_seconds)
        if consistency < self.min_consistency:
            return HOLD

        # ---- Price check (sliding) ----
        signal_mid = up_mid if direction == "up" else down_mid
        eff_max = self._effective_max_price(ctx.elapsed_fraction)
        if signal_mid > eff_max:
            return HOLD

        # ---- All signals confirmed — ENTER ----
        self._entry_count += 1
        self._entry_midpoint = signal_mid

        side = "buy_up" if direction == "up" else "buy_down"
        dollars = ctx.position.cash * self.bet_fraction

        self.log(
            "ENTRY {}: disp={:.1f}bps vel={:.3f} consist={:.2f} "
            "mid={:.3f} eff_max={:.3f} at {:.0f}%".format(
                side, displacement_bps, velocity,
                consistency, signal_mid, eff_max,
                ctx.elapsed_fraction * 100,
            )
        )

        return TradeAction(
            side=side,
            amount=dollars,
            price=signal_mid,
            cancel_after_s=10,
        )
