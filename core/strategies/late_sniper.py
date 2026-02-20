"""Wait until late in the window when outcome is more certain, then buy the likely winner."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class LateSniper(Strategy):
    """Params:
        min_elapsed: minimum elapsed fraction before considering entry (e.g. 0.7)
        confidence_threshold: minimum midpoint to treat as "confident" (e.g. 0.7)
        max_price: don't buy above this price (limits downside)
        bet_fraction: fraction of cash to bet
    """

    name = "LateSniper"

    def __init__(
        self,
        min_elapsed=0.7,
        confidence_threshold=0.70,
        max_price=0.85,
        bet_fraction=0.5,
    ):
        self.min_elapsed = min_elapsed
        self.confidence_threshold = confidence_threshold
        self.max_price = max_price
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "min_elapsed": min_elapsed,
            "confidence_threshold": confidence_threshold,
            "max_price": max_price,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        if up_mid >= self.confidence_threshold and up_mid <= self.max_price:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_mid >= self.confidence_threshold and down_mid <= self.max_price:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.5, 0.6, 0.7, 0.8],
            "confidence_threshold": [0.60, 0.65, 0.70, 0.80],
            "max_price": [0.80, 0.85, 0.90, 0.95],
            "bet_fraction": [0.25, 0.5, 1.0],
        }
