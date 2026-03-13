"""Regime-gated mean-reversion strategy: only trades in mean-reverting + medium/high vol.

Buys dips (tokens priced below a threshold) when the market regime shows
mean-reverting autocorrelation. In trending or flat markets, sits out.

The idea: in mean-reverting regimes, extreme moves tend to snap back.
Buying cheap tokens in these conditions has positive expectancy because
the price overshoots then corrects. In trending regimes this gets
crushed — the "dip" is actually the start of a larger move.

v1: Initial implementation.
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.strategies.base import Param
from core.strategies.regime_gated import RegimeGatedStrategy
from core.trader import HOLD, TradeAction, TickContext


class MeanRevFader(RegimeGatedStrategy):
    """Buy dips in mean-reverting regimes, sell on take-profit."""

    name = "MeanRevFader"
    description = (
        "Regime-gated mean-reversion. Only activates when prior sessions "
        "show mean-reverting autocorrelation. Buys tokens below a price "
        "threshold, sells on take-profit."
    )
    version = "2"
    requires_full_session = True

    # --- Regime gate (NOT tunable) ---
    required_vol = None
    required_trend = {"flat"}
    skip_stale_data = True

    # --- Tunable params ---
    max_buy_price = Param(default=0.50, min=0.35, max=0.50)
    take_profit_pct = Param(default=0.15, min=0.15, max=0.50)
    stop_loss_pct = Param(default=0.08, min=0.08, max=0.40)
    min_elapsed = Param(default=0.25, min=0.05, max=0.30)
    max_entries = Param(default=2, min=1, max=4)

    # --- Fixed params ---
    no_new_after = Param(default=0.80, tunable=False)
    cooldown_ms = Param(default=3000, tunable=False)

    def _on_session_start_inner(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._entry_side = None  # type: Optional[str]
        self._last_trade_time_s = -999.0

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if up_mid is None or down_mid is None:
            return HOLD

        # --- Manage position ---
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

        # --- No position: look for dip ---
        self._check_expired_entry(ctx)
        if self._entry_side is not None and self._entry_midpoint == 0.0:
            self._entry_side = None

        if self._entry_count >= self.max_entries:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        cooldown_s = self.cooldown_ms / 1000.0
        if ctx.elapsed_seconds - self._last_trade_time_s < cooldown_s:
            return HOLD

        # Buy whichever side is cheapest below threshold
        candidates = []
        if 0 < up_mid <= min(self.max_buy_price, 0.50):
            candidates.append(("buy_up", "up", up_mid))
        if 0 < down_mid <= min(self.max_buy_price, 0.50):
            candidates.append(("buy_down", "down", down_mid))

        if not candidates:
            return HOLD

        candidates.sort(key=lambda x: x[2])
        side, side_label, price = candidates[0]

        self._entry_count += 1
        self._entry_midpoint = price
        self._entry_side = side_label
        self._last_trade_time_s = ctx.elapsed_seconds

        self.log("BUY {} #{}: mid={:.3f} at {:.0f}%".format(
            side_label.upper(), self._entry_count, price,
            ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=ctx.position.cash, price=price,
                           cancel_after_s=10)
