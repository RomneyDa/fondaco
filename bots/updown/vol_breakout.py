"""Regime-gated volatility breakout: only trades in high-vol sessions.

When the regime is high-vol (regardless of trend), waits for a strong
BTC displacement and enters aggressively. High-vol sessions have wider
moves, so even moderate midpoint prices can pay off. Uses loose entry
criteria but strict price limits.

v1: Initial implementation.
"""

from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.strategies.base import Param
from core.strategies.regime_gated import RegimeGatedStrategy
from core.trader import HOLD, TradeAction, TickContext


class VolBreakout(RegimeGatedStrategy):
    """Aggressive entry in high-vol sessions on BTC breakout."""

    name = "VolBreakout"
    description = (
        "Regime-gated volatility breakout. Only activates in high-vol "
        "regimes. Enters on strong BTC displacement at moderate prices."
    )
    version = "2"
    requires_full_session = True

    # --- Regime gate (NOT tunable) ---
    required_vol = {"high"}
    required_trend = None  # any trend regime
    skip_stale_data = True

    # --- Tunable params ---
    breakout_bps = Param(default=10.0, min=4.0, max=15.0)
    min_elapsed = Param(default=0.25, min=0.10, max=0.45)
    max_price = Param(default=0.60, min=0.40, max=0.65)
    # --- Fixed params ---
    no_new_after = Param(default=0.90, tunable=False)

    def _on_session_start_inner(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._btc_prices = []  # type: List[float]
        self._entry_midpoint = 0.0
        self._entry_count = 0

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is not None and btc > 0:
            self._btc_prices.append(btc)

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # Hold if already in position
        if self._held_side(ctx) is not None:
            return HOLD

        self._check_expired_entry(ctx)
        if self._entry_count > 0:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        if ctx.initial_btc_price <= 0:
            return HOLD

        # BTC displacement
        current_btc = self._btc_prices[-1]
        disp_bps = (current_btc - ctx.initial_btc_price) / ctx.initial_btc_price * BPS_DIVISOR

        if abs(disp_bps) < self.breakout_bps:
            return HOLD

        direction = "up" if disp_bps > 0 else "down"
        signal_mid = up_mid if direction == "up" else down_mid

        if signal_mid > self.max_price:
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = signal_mid
        side = "buy_up" if direction == "up" else "buy_down"

        self.log("BREAKOUT {}: disp={:.1f}bps mid={:.3f} at {:.0f}%".format(
            side, disp_bps, signal_mid, ctx.elapsed_fraction * 100))

        return TradeAction(
            side=side,
            amount=ctx.position.cash,
            price=signal_mid,
            cancel_after_s=10,
        )
