"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class LateSniper(Strategy):
    """Params:
        min_elapsed: minimum elapsed fraction before considering entry (e.g. 0.7)
        confidence_threshold: minimum midpoint to treat as "confident" (e.g. 0.70)
        max_price: don't buy above this price (limits downside)
        no_new_after: stop new entries after this elapsed fraction
        stop_loss_pct: sell if midpoint drops this fraction below entry (None = disabled)
        take_profit_pct: sell if midpoint rises this fraction above entry (None = disabled)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniper"
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner."
    # v6: 2-stage Sharpe→Kelly optimization over recent 30 sessions.
    # Sharpe 0.669, 70% win rate, 10.5% geo return/session.
    # Lower confidence (0.69) enters more often, max_price cap (0.94) avoids
    # overpaying, wider take-profit (0.30) lets winners run.
    # Half-Kelly cash% = 50%, deploying at quarter-Kelly (25%).
    version = "9"

    def __init__(
        self,
        min_elapsed=0.54,
        confidence_threshold=0.67,
        max_price=0.98,
        no_new_after=0.95,
        stop_loss_pct=0.12,  # type: Optional[float]
        take_profit_pct=None,  # type: Optional[float]
        bet_fraction=1.0,
    ):
        self.min_elapsed = min_elapsed
        self.min_elapsed_hint = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.no_new_after = no_new_after
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.bet_fraction = bet_fraction
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_tick = -999
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "no_new_after": no_new_after,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_tick = -999

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss and take-profit
        if self._held_side(ctx) is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid,
                                            self.stop_loss_pct, self.take_profit_pct)
            return exit_action if exit_action is not None else HOLD

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # Only one entry per session
        if self._entry_count > 0:
            return HOLD

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        if up_mid >= self.confidence_threshold and up_mid <= self.max_price:
            self._entry_count += 1
            self._entry_midpoint = up_mid
            self.log("BUY UP: mid={:.3f} at {:.0f}% elapsed".format(
                up_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction, price=up_mid,
                               expiration_seconds=10)
        elif down_mid >= self.confidence_threshold and down_mid <= self.max_price:
            self._entry_count += 1
            self._entry_midpoint = down_mid
            self.log("BUY DOWN: mid={:.3f} at {:.0f}% elapsed".format(
                down_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction, price=down_mid,
                               expiration_seconds=10)

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.50, 0.52, 0.54, 0.56],
            "confidence_threshold": [0.64, 0.67, 0.70, 0.73],
            "max_price": [0.94, 0.96, 0.98],
            "stop_loss_pct": [None, 0.10, 0.12, 0.14],
            "take_profit_pct": [None, 0.25, 0.35],
        }
