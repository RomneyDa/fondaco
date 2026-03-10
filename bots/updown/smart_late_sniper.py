"""Buy the likely winner using a combined time×confidence score instead of independent thresholds."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class SmartLateSniper(Strategy):
    """Instead of requiring elapsed >= X AND midpoint >= Y independently,
    uses a combined score: elapsed_fraction * midpoint >= entry_threshold.

    This lets us enter earlier when confidence is very high, or later with
    moderate confidence, capturing more opportunities than hard cutoffs.

    A min_elapsed floor still prevents extremely early entries where the
    midpoint is noisy and unreliable.

    Params:
        entry_threshold: minimum elapsed * midpoint product to trigger entry (e.g. 0.42)
        min_elapsed: absolute floor on elapsed fraction to avoid noisy early entries
        max_price: don't buy above this price (limits downside)
        stop_loss_pct: sell if midpoint drops this fraction below entry (None = disabled)
        take_profit_pct: sell if midpoint rises this fraction above entry (None = disabled)
        bet_fraction: fraction of cash to bet
    """

    name = "SmartLateSniper"
    description = "Uses elapsed * midpoint score for adaptive entry timing — high confidence enters earlier, moderate confidence waits."
    version = "3"

    def __init__(
        self,
        entry_threshold=0.60,
        min_elapsed=0.30,
        max_price=0.99,
        stop_loss_pct=0.07,  # type: Optional[float]
        take_profit_pct=0.15,  # type: Optional[float]
        bet_fraction=1.0,
    ):
        self.entry_threshold = entry_threshold
        self.min_elapsed = min_elapsed
        self.max_price = max_price
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.bet_fraction = bet_fraction
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999
        self._params = {
            "entry_threshold": entry_threshold,
            "min_elapsed": min_elapsed,
            "max_price": max_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entry_midpoint = 0.0
        self._last_sell_tick = -999

    def _has_position(self, ctx):
        return ctx.position.up_tokens > 0 or ctx.position.down_tokens > 0

    def _held_side(self, ctx):
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

        # Manage existing position — stop-loss and take-profit
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

        # Cooldown after a sell — don't re-enter the same session after stop-loss/take-profit
        if ctx.tick_index - self._last_sell_tick < 36:
            return HOLD

        # Absolute floor — midpoint is too noisy before this
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        # Pick the stronger side
        if up_mid >= down_mid:
            best_mid, best_side, buy_side = up_mid, "up", "buy_up"
        else:
            best_mid, best_side, buy_side = down_mid, "down", "buy_down"

        # Combined score: time × confidence
        score = ctx.elapsed_fraction * best_mid

        if score >= self.entry_threshold and best_mid <= self.max_price:
            self._entry_midpoint = best_mid
            self.log("BUY {}: mid={:.2f} elapsed={:.0f}% score={:.3f}>={:.3f}".format(
                best_side.upper(), best_mid, ctx.elapsed_fraction * 100,
                score, self.entry_threshold,
            ))
            return TradeAction(side=buy_side, amount=ctx.position.cash * self.bet_fraction, price=best_mid)

        # Log why we held
        reasons = []
        if score < self.entry_threshold:
            reasons.append("score {:.3f}<{:.3f}".format(score, self.entry_threshold))
        if best_mid > self.max_price:
            reasons.append("mid too high {:.2f}>{:.2f}".format(best_mid, self.max_price))
        self.log("HOLD at {:.0f}%: best={} mid={:.2f} score={:.3f} | {}".format(
            ctx.elapsed_fraction * 100,
            best_side,
            best_mid,
            score,
            ", ".join(reasons) if reasons else "waiting",
        ))

        return HOLD

    def param_grid(self):
        return {
            "entry_threshold": [0.54, 0.57, 0.60, 0.63, 0.66],
            "min_elapsed": [0.25, 0.30, 0.35, 0.40],
            "max_price": [0.96, 0.98, 0.99],
            "stop_loss_pct": [None, 0.05, 0.07, 0.10],
            "take_profit_pct": [None, 0.10, 0.15, 0.20],
        }
