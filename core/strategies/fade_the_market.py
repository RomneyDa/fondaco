"""Mean reversion: if midpoint swings to an extreme early, bet against it."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class FadeTheMarket(Strategy):
    """Params:
        overreaction_threshold: midpoint level considered overreaction (e.g. 0.70)
        max_elapsed: only fade early moves (e.g. 0.3 = first 30%)
        bet_fraction: fraction of cash to bet
    """

    name = "FadeTheMarket"
    description = "Mean reversion contrarian play. If a token midpoint spikes to an extreme early in the window, bets against it — expecting the market overreacted and the price will revert."

    def __init__(
        self,
        overreaction_threshold=0.70,
        max_elapsed=0.3,
        bet_fraction=0.5,
    ):
        self.overreaction_threshold = overreaction_threshold
        self.max_elapsed = max_elapsed
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "overreaction_threshold": overreaction_threshold,
            "max_elapsed": max_elapsed,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.max_elapsed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Check actual execution prices — don't buy if ask is unreasonable
        up_ask = ctx.sample.get("up_token_best_ask")
        down_ask = ctx.sample.get("down_token_best_ask")

        # If Up is overreacting high, fade it by buying Down
        if up_mid >= self.overreaction_threshold:
            if down_ask is not None and down_ask <= (1.0 - self.overreaction_threshold):
                self._entered = True
                self.log("fading up mid={:.2f}, buying down ask={:.2f}".format(up_mid, down_ask))
                return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)
        # If Down is overreacting high, fade it by buying Up
        elif down_mid >= self.overreaction_threshold:
            if up_ask is not None and up_ask <= (1.0 - self.overreaction_threshold):
                self._entered = True
                self.log("fading down mid={:.2f}, buying up ask={:.2f}".format(down_mid, up_ask))
                return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "overreaction_threshold": [0.65, 0.70, 0.75, 0.80],
            "max_elapsed": [0.2, 0.3, 0.4],
            "bet_fraction": [0.25, 0.5, 1.0],
        }
