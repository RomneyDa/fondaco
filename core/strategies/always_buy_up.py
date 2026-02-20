"""Baseline: buy Up tokens at first tick, hold to resolution."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class AlwaysBuyUp(Strategy):

    name = "AlwaysBuyUp"

    def __init__(self, bet_fraction=1.0):
        self.bet_fraction = bet_fraction
        self._bought = False
        self._params = {"bet_fraction": bet_fraction}

    def on_session_start(self, metadata):
        self._bought = False

    def on_tick(self, ctx):
        if not self._bought:
            self._bought = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        return HOLD

    def param_grid(self):
        return {"bet_fraction": [0.25, 0.5, 0.75, 1.0]}
