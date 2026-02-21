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
    description = "Waits until late in the window when the outcome is more predictable, then buys the likely winner. Only enters when the token midpoint shows high confidence and the price isn't too expensive. Trades accuracy for lower profit per bet."

    def __init__(
        self,
        min_elapsed=0.7,
        confidence_threshold=0.60,
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

        # Use best_ask (actual execution price) for price cap, midpoint for confidence
        up_ask = ctx.sample.get("up_token_best_ask")
        down_ask = ctx.sample.get("down_token_best_ask")

        if up_mid >= self.confidence_threshold and up_ask is not None and up_ask <= self.max_price:
            self._entered = True
            self.log("up token mid={:.2f} ask={:.2f} >= {:.2f} confidence at {:.0f}% elapsed".format(
                up_mid, up_ask, self.confidence_threshold, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_mid >= self.confidence_threshold and down_ask is not None and down_ask <= self.max_price:
            self._entered = True
            self.log("down token mid={:.2f} ask={:.2f} >= {:.2f} confidence at {:.0f}% elapsed".format(
                down_mid, down_ask, self.confidence_threshold, ctx.elapsed_fraction * 100,
            ))
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "min_elapsed": [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
            "confidence_threshold": [0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70],
            "max_price": [0.80, 0.83, 0.85, 0.87, 0.90, 0.93],
            "bet_fraction": [0.3, 0.4, 0.5, 0.6, 0.7],
        }
