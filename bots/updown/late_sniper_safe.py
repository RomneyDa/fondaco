"""Ultra-conservative LateSniper: only enters when outcome is virtually decided."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class LateSniperSafe(Strategy):
    """Conservative variant of LateSniper that targets ~0% failure rate.

    Enters very late in the window (last ~45s) when midpoint is extremely high,
    meaning the outcome is essentially decided. Requires sustained high midpoint
    over a confirmation window to avoid entering on momentary spikes.

    Trades less often and profits less per trade, but almost never picks the
    wrong side.

    Params:
        min_elapsed: minimum elapsed fraction before considering entry (default 0.78)
        confidence_threshold: minimum midpoint to treat as confident (default 0.93)
        max_price: don't buy above this price (default 0.96)
        confirm_ticks: midpoint must stay above threshold for this many ticks (default 54 ~3s)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniperSafe"
    description = "Ultra-conservative late entry — only trades when outcome is virtually decided."
    version = "2"

    def __init__(
        self,
        min_elapsed=0.85,
        confidence_threshold=0.9,
        max_price=0.98,
        confirm_ticks=18,
        bet_fraction=1.0,
    ):
        self.min_elapsed = min_elapsed
        self.min_elapsed_hint = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.confirm_ticks = confirm_ticks
        self.bet_fraction = bet_fraction
        self._entered = False
        self._confirm_count = 0
        self._confirm_side = None  # type: Optional[str]
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "confirm_ticks": confirm_ticks,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False
        self._confirm_count = 0
        self._confirm_side = None

    def on_tick(self, ctx):
        # Already entered — hold to resolution (no stop-loss needed at this confidence)
        if self._entered:
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
                self._entered = True
                buy_side = "buy_up" if best_side == "up" else "buy_down"
                self.log("BUY {}: mid={:.3f} at {:.0f}% elapsed, confirmed {} ticks".format(
                    best_side.upper(), best_mid, ctx.elapsed_fraction * 100,
                    self._confirm_count,
                ))
                return TradeAction(
                    side=buy_side,
                    amount=ctx.position.cash * self.bet_fraction,
                    price=best_mid,
                )
            else:
                self.log("confirming {}: mid={:.3f} ({}/{} ticks)".format(
                    best_side, best_mid, self._confirm_count, self.confirm_ticks,
                ))
        else:
            # Reset confirmation if signal drops
            self._confirm_count = 0
            self._confirm_side = None

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.80, 0.83, 0.85, 0.87, 0.90],
            "confidence_threshold": [0.88, 0.90, 0.92, 0.94],
            "max_price": [0.96, 0.97, 0.98],
            "confirm_ticks": [18, 36, 54],
        }
