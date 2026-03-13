"""Regime-gated late confirmation entry: only trades in clean data, medium+ vol.

Ultra-conservative late-session entry that requires high confidence AND
clean data (no gaps). Sits out in low-vol (coin-flip) sessions and when
data quality is stale. This is designed to be the strategy that almost
never loses — high selectivity, low frequency.

v1: Initial implementation.
"""

from typing import Any, Dict, Optional

from core.strategies.base import Param
from core.strategies.regime_gated import RegimeGatedStrategy
from core.trader import HOLD, TradeAction, TickContext


class ConfirmedLate(RegimeGatedStrategy):
    """Late-session high-confidence entry, regime-gated."""

    name = "ConfirmedLate"
    description = (
        "Regime-gated late confirmation. Only activates in medium-to-high "
        "vol with clean data. Requires sustained high confidence before entry."
    )
    version = "3"
    requires_full_session = False
    min_elapsed_hint = 0.80

    # --- Regime gate (NOT tunable) ---
    required_vol = {"medium", "high"}
    required_trend = None  # any trend is OK for confirmed late entries
    skip_stale_data = True

    # --- Tunable params ---
    min_elapsed = Param(default=0.80, min=0.75, max=0.90)
    confidence_threshold = Param(default=0.94, min=0.90, max=0.97)
    max_price = Param(default=0.97, min=0.95, max=0.98)
    confirm_ms = Param(default=1000, min=500, max=2000)

    # --- Fixed params ---
    no_new_after = Param(default=0.96, tunable=False)

    def _on_session_start_inner(self, metadata):
        # type: (dict) -> None
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._confirm_side = None   # type: Optional[str]
        self._confirm_start_s = -999.0

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if up_mid is None or down_mid is None:
            return HOLD

        # Already holding
        if self._held_side(ctx) is not None:
            return HOLD

        self._check_expired_entry(ctx)
        if self._entry_count > 0:
            return HOLD

        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # Determine confident side
        if up_mid >= self.confidence_threshold:
            side = "up"
            signal_mid = up_mid
        elif down_mid >= self.confidence_threshold:
            side = "down"
            signal_mid = down_mid
        else:
            # Below confidence — reset confirmation
            self._confirm_side = None
            self._confirm_start_s = -999.0
            return HOLD

        if signal_mid > self.max_price:
            return HOLD

        # Confirmation timer
        if self._confirm_side != side:
            self._confirm_side = side
            self._confirm_start_s = ctx.elapsed_seconds
            return HOLD

        confirm_s = self.confirm_ms / 1000.0
        if ctx.elapsed_seconds - self._confirm_start_s < confirm_s:
            return HOLD

        # All gates passed — enter
        self._entry_count += 1
        self._entry_midpoint = signal_mid
        buy_side = "buy_up" if side == "up" else "buy_down"

        self.log("CONFIRMED {}: mid={:.3f} confirmed for {:.0f}ms at {:.0f}%".format(
            buy_side, signal_mid, (ctx.elapsed_seconds - self._confirm_start_s) * 1000,
            ctx.elapsed_fraction * 100))

        return TradeAction(
            side=buy_side,
            amount=ctx.position.cash,
            price=signal_mid,
            cancel_after_s=5,
        )
