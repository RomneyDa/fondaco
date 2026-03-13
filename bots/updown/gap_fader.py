"""Fade the gap: buy dips in undecided markets, sell on take-profit, repeat.

When the initial price gap between Up and Down tokens is small, the market
is undecided and prices are likely to swing back and forth across the
midline. This strategy:

1. Observes the first few seconds to measure the average Up/Down gap.
2. If the gap is small (< gap_threshold), activates for dip-buying.
3. Buys whichever token dips below a price threshold (always < 0.50).
4. Sells on take-profit (entry_price * (1 + take_profit_pct)).
5. Allows multiple round-trips per session (max_entries).
6. Scales aggression with recent session volatility (higher vol = deeper
   expected swings = buy at slightly higher prices).
7. Decays with elapsed time (later in session = less likely to swing back
   = require cheaper prices to enter).

v1: Initial implementation.

v2: Optimized over 30 sessions. Sharpe 0.278, 27% win rate, 4.8% geo
return/session. Key findings: high take-profit (0.40) essential — low TPs
get killed by fees from round-trips. Loose stop-loss (0.30). Linear time
decay (power=1.0). Vol boost doesn't help (scale=0). Fast cooldown (36
ticks). max_buy_price=0.40 sweet spot. Half-Kelly f*=0.60 → cash_pct=30%.
"""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Param, Strategy
from core.volatility import compute_session_volatility


class GapFader(Strategy):
    """Buy dips in undecided markets, sell on take-profit, repeat."""

    name = "GapFader"
    description = (
        "Buys dips in undecided markets (small initial gap), sells on "
        "take-profit. Multiple round-trips per session, scaled by "
        "volatility and elapsed time."
    )
    version = "7"
    requires_full_session = True

    # --- Tunable params ---
    gap_threshold = Param(default=0.06, min=0.04, max=0.10)
    max_buy_price = Param(default=0.50, min=0.44, max=0.50)
    take_profit_pct = Param(default=0.45, min=0.30, max=0.55)
    stop_loss_pct = Param(default=0.35, min=0.25, max=0.45)
    max_entries = Param(default=5, min=3, max=7)
    time_decay_power = Param(default=2.5, min=1.5, max=3.0)

    # --- Fixed params ---
    observe_seconds = Param(default=15.0, tunable=False)
    cooldown_ms = Param(default=2000, tunable=False)
    no_new_after = Param(default=0.85, tunable=False)
    vol_lookback = Param(default=5, tunable=False)
    min_vol_sessions = Param(default=3, tunable=False)
    vol_boost_scale = Param(default=0.0, tunable=False)
    vol_baseline_bps = Param(default=5.0, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reset_state()

    def _reset_state(self):
        self._gap_samples = []  # type: List[float]
        self._active = False
        self._vol_boost = 1.0
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._entry_side = None  # type: Optional[str]
        self._last_trade_time_s = -999.0
        self._window_seconds = 300

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._reset_state()
        self._window_seconds = metadata.get("window", 300)

        # Compute volatility boost from recent sessions
        self._vol_boost = 1.0
        prior_summaries = metadata.get("_prior_session_summaries")
        vol = compute_session_volatility(
            prior_summaries, self.vol_lookback, self.min_vol_sessions)
        if vol is None:
            if prior_summaries is not None:
                self.log("Insufficient lookback for volatility")
            return

        mean_abs, _ = vol
        vol_ratio = mean_abs / max(self.vol_baseline_bps, 0.1)
        self._vol_boost = 1.0 + self.vol_boost_scale * max(vol_ratio - 1.0, 0.0)
        self._vol_boost = max(1.0, min(self._vol_boost, 1.5))
        self.log("Vol: mean_abs={:.2f}bps ratio={:.2f} boost={:.3f}".format(
            mean_abs, vol_ratio, self._vol_boost))

    def _effective_max_price(self, elapsed_fraction):
        # type: (float) -> float
        """Compute the effective max buy price given elapsed time."""
        remaining = max(1.0 - elapsed_fraction, 0.0)
        time_factor = remaining ** self.time_decay_power
        return self.max_buy_price * self._vol_boost * time_factor

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if up_mid is None or down_mid is None:
            return HOLD

        elapsed_s = ctx.sample.get("elapsed_seconds", 0.0)

        # --- Phase 1: Observation ---
        if not self._active:
            if elapsed_s <= self.observe_seconds:
                gap = abs(up_mid - down_mid)
                self._gap_samples.append(gap)
                return HOLD

            # Observation period over — evaluate
            if not self._gap_samples:
                self.log("No gap samples collected, deactivating")
                self._active = False
                # Prevent re-evaluation
                self._gap_samples.append(999.0)
                return HOLD

            avg_gap = sum(self._gap_samples) / len(self._gap_samples)

            if avg_gap >= self.gap_threshold:
                self.log("Gap too wide: avg={:.4f} >= threshold={:.4f}, skipping".format(
                    avg_gap, self.gap_threshold))
                # Mark as evaluated but not active
                self._gap_samples = []
                return HOLD

            self._active = True
            self.log("Gap OK: avg={:.4f} < {:.4f} from {} samples, activating".format(
                avg_gap, self.gap_threshold, len(self._gap_samples)))

        # --- Manage existing position ---
        held = self._held_side(ctx)
        if held is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid,
                                            self.stop_loss_pct, self.take_profit_pct,
                                            cooldown_ms=self.cooldown_ms)
            if exit_action is not None:
                if exit_action is not HOLD:
                    self._entry_side = None
                return exit_action
            return HOLD

        # --- No position: look for dip to buy ---

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)
        if self._entry_side is not None and self._entry_midpoint == 0.0:
            self._entry_side = None

        if not self._active:
            return HOLD

        # Max entries
        if self._entry_count >= self.max_entries:
            return HOLD

        # Timing guard
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # Cooldown
        if ctx.elapsed_seconds - self._last_trade_time_s < self.cooldown_ms / 1000.0:
            return HOLD

        # Effective max price decays with time, boosted by volatility
        eff_max = self._effective_max_price(ctx.elapsed_fraction)

        # Buy whichever side is cheapest (below effective max)
        # Both must be below 0.50 (undecided market assumption)
        candidates = []
        if 0 < up_mid <= min(eff_max, 0.50):
            candidates.append(("buy_up", "up", up_mid))
        if 0 < down_mid <= min(eff_max, 0.50):
            candidates.append(("buy_down", "down", down_mid))

        if not candidates:
            return HOLD

        # Pick the cheapest
        candidates.sort(key=lambda x: x[2])
        side, side_label, price = candidates[0]

        self._entry_count += 1
        self._entry_midpoint = price
        self._entry_side = side_label
        self._last_trade_time_s = ctx.elapsed_seconds

        dollars = ctx.position.cash

        self.log("BUY {} #{}: mid={:.3f} eff_max={:.3f} vol_boost={:.3f} "
                 "${:.2f} at {:.0f}%".format(
                     side_label.upper(), self._entry_count, price,
                     eff_max, self._vol_boost, dollars,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=price, cancel_after_s=10)


