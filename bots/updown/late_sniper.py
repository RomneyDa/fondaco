"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class LateSniper(Strategy):
    """Params:
        min_elapsed: minimum elapsed fraction before considering entry (e.g. 0.7)
        confidence_threshold: minimum midpoint to treat as "confident" (e.g. 0.70)
        max_price: don't buy above this price (limits downside)
        stop_loss_pct: sell if midpoint drops this fraction below entry (None = disabled)
        take_profit_pct: sell if midpoint rises this fraction above entry (None = disabled)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniper"
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner."

    def __init__(
        self,
        min_elapsed=0.51,
        confidence_threshold=0.74,
        max_price=0.96,
        stop_loss_pct=0.06,  # type: Optional[float]
        take_profit_pct=None,  # type: Optional[float]
        bet_fraction=1.0,
    ):
        self.min_elapsed = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.bet_fraction = bet_fraction
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999  # throttle sell retries (~2s between attempts)
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999

    def _has_position(self, ctx):
        """Check if we actually hold tokens (fill succeeded)."""
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _held_side(self, ctx):
        """Return which side we hold, or None."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss and take-profit
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0:
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

                # Stop-loss
                if self.stop_loss_pct is not None and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                    if ctx.tick_index - self._last_sell_tick < 36:
                        return HOLD
                    self._last_sell_tick = ctx.tick_index
                    self.log("stop-loss: mid={:.2f} down {:.1f}% from entry {:.2f}".format(
                        current_mid,
                        (1.0 - current_mid / self._entry_midpoint) * 100,
                        self._entry_midpoint,
                    ))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

                # Take-profit
                if self.take_profit_pct is not None and current_mid >= self._entry_midpoint * (1.0 + self.take_profit_pct):
                    if ctx.tick_index - self._last_sell_tick < 36:
                        return HOLD
                    self._last_sell_tick = ctx.tick_index
                    self.log("take-profit: mid={:.2f} up {:.1f}% from entry {:.2f}".format(
                        current_mid,
                        (current_mid / self._entry_midpoint - 1.0) * 100,
                        self._entry_midpoint,
                    ))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # Already entered (limit order pending or filled)
        if self._entry_midpoint > 0:
            return HOLD

        # Wait for late window
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        if up_mid >= self.confidence_threshold and up_mid <= self.max_price:
            self._entry_midpoint = up_mid
            self.log("BUY UP: mid={:.2f} at {:.0f}% elapsed".format(
                up_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction, price=up_mid)
        elif down_mid >= self.confidence_threshold and down_mid <= self.max_price:
            self._entry_midpoint = down_mid
            self.log("BUY DOWN: mid={:.2f} at {:.0f}% elapsed".format(
                down_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction, price=down_mid)

        # Log why we held
        best_mid = max(up_mid, down_mid)
        best_side = "up" if up_mid >= down_mid else "down"
        reasons = []
        if best_mid < self.confidence_threshold:
            reasons.append("low confidence {:.2f}<{:.2f}".format(best_mid, self.confidence_threshold))
        if best_mid > self.max_price:
            reasons.append("mid too high {:.2f}>{:.2f}".format(best_mid, self.max_price))
        self.log("HOLD at {:.0f}%: best={} mid={:.2f} | {}".format(
            ctx.elapsed_fraction * 100,
            best_side,
            best_mid,
            ", ".join(reasons) if reasons else "waiting",
        ))

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.45, 0.47, 0.49, 0.51, 0.53],
            "confidence_threshold": [0.68, 0.71, 0.74],
            "max_price": [0.94, 0.96, 0.97],
            "stop_loss_pct": [None, 0.03, 0.05, 0.08],
            "take_profit_pct": [None, 0.15, 0.25, 0.40],
        }
