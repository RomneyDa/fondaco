"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Param, Strategy


class LateSniper(Strategy):
    """Waits until late in the window when the outcome is more predictable, then buys the likely winner."""

    name = "LateSniper"
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner."
    version = "10"

    # --- Tunable params ---
    min_elapsed = Param(default=0.54, min=0.40, max=0.70)
    confidence_threshold = Param(default=0.67, min=0.55, max=0.80)
    max_price = Param(default=0.98, min=0.90, max=0.99)
    stop_loss_pct = Param(default=0.12, min=0.05, max=0.20, nullable=True)
    take_profit_pct = Param(default=None, min=0.15, max=0.45, nullable=True)

    # --- Fixed params ---
    no_new_after = Param(default=0.95, tunable=False)
    bet_fraction = Param(default=1.0, tunable=False)

    def __init__(self, **kwargs):
        defs = self.param_defs()
        for name, p in defs.items():
            setattr(self, name, kwargs.get(name, p.default))
        self._params = {name: getattr(self, name) for name in defs}
        self.min_elapsed_hint = self.min_elapsed
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_tick = -999

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

