"""Ultra-conservative LateSniper: only enters when outcome is virtually decided."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Param, Strategy


class LateSniperSafe(Strategy):
    """Ultra-conservative late entry — only trades when outcome is virtually decided."""

    name = "LateSniperSafe"
    description = "Ultra-conservative late entry — only trades when outcome is virtually decided."
    version = "5"

    # --- Tunable params ---
    min_elapsed = Param(default=0.85, min=0.75, max=0.92)
    confidence_threshold = Param(default=0.9, min=0.85, max=0.96)
    max_price = Param(default=0.98, min=0.95, max=0.99)
    confirm_ticks = Param(default=18, min=9, max=72)

    # --- Fixed params ---
    bet_fraction = Param(default=1.0, tunable=False)

    def __init__(self, **kwargs):
        defs = self.param_defs()
        for name, p in defs.items():
            setattr(self, name, kwargs.get(name, p.default))
        self._params = {name: getattr(self, name) for name in defs}
        self.min_elapsed_hint = self.min_elapsed
        self._entry_count = 0
        self._confirm_count = 0
        self._confirm_side = None  # type: Optional[str]

    def on_session_start(self, metadata):
        self._entry_count = 0
        self._confirm_count = 0
        self._confirm_side = None

    def on_tick(self, ctx):
        # Already entered — hold to resolution (no stop-loss needed at this confidence)
        if self._entry_count > 0:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Wait for late window
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        # Determine which side (if any) meets the threshold
        best_mid = max(up_mid, down_mid)
        best_side = "up" if up_mid >= down_mid else "down"

        if best_mid >= self.confidence_threshold and best_mid <= self.max_price:
            # Accumulate confirmation ticks — must be same side consistently
            if best_side == self._confirm_side:
                self._confirm_count += 1
            else:
                self._confirm_side = best_side
                self._confirm_count = 1

            if self._confirm_count >= self.confirm_ticks:
                self._entry_count += 1
                buy_side = "buy_up" if best_side == "up" else "buy_down"
                self.log("BUY {}: mid={:.3f} at {:.0f}% elapsed, confirmed {} ticks".format(
                    best_side.upper(), best_mid, ctx.elapsed_fraction * 100,
                    self._confirm_count,
                ))
                return TradeAction(
                    side=buy_side,
                    amount=ctx.position.cash * self.bet_fraction,
                    price=best_mid,
                    expiration_seconds=5,
                )
        else:
            # Reset confirmation if signal drops
            self._confirm_count = 0
            self._confirm_side = None

        return HOLD
