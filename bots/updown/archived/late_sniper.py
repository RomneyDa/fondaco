"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from typing import Any, Dict, List, Optional

from core.defaults import MIN_BTC_MOVE_PCT
from core.trader import HOLD, TradeAction
from core.strategies.base import Param, Strategy


class LateSniper(Strategy):
    """Waits until late in the window when the outcome is more predictable, then buys the likely winner."""

    name = "LateSniper"
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner."
    version = "12"

    # --- Tunable params ---
    min_elapsed = Param(default=0.70, min=0.40, max=0.70)
    confidence_threshold = Param(default=0.80, min=0.55, max=0.80)
    max_price = Param(default=0.90, min=0.90, max=0.99)
    stop_loss_pct = Param(default=0.20, min=0.05, max=0.20, nullable=True)
    take_profit_pct = Param(default=None, min=0.15, max=0.45, nullable=True)
    min_btc_move_pct = Param(default=MIN_BTC_MOVE_PCT, min=0.0, max=0.15, nullable=True)

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
        self._last_sell_time_s = -999.0

    def on_session_start(self, metadata):
        self._entry_midpoint = 0.0
        self._entry_count = 0
        self._last_sell_time_s = -999.0

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

        # Skip if BTC hasn't moved enough — flat sessions are coin flips
        if self.min_btc_move_pct is not None and ctx.initial_btc_price and ctx.initial_btc_price > 0:
            btc_now = ctx.sample.get("btc_price_usd")
            if btc_now:
                btc_move = abs(btc_now - ctx.initial_btc_price) / ctx.initial_btc_price * 100
                if btc_move < self.min_btc_move_pct:
                    return HOLD

        if up_mid >= self.confidence_threshold and up_mid <= self.max_price:
            self._entry_count += 1
            self._entry_midpoint = up_mid
            self.log("BUY UP: mid={:.3f} at {:.0f}% elapsed".format(
                up_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction, price=up_mid)

        elif down_mid >= self.confidence_threshold and down_mid <= self.max_price:
            self._entry_count += 1
            self._entry_midpoint = down_mid
            self.log("BUY DOWN: mid={:.3f} at {:.0f}% elapsed".format(
                down_mid, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction, price=down_mid)

        return HOLD

