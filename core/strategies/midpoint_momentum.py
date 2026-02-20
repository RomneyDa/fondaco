"""Follow token midpoint trend over recent ticks."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class MidpointMomentum(Strategy):
    """If the Up midpoint is rising above a threshold with momentum, buy Up (and vice versa).

    Params:
        lookback_ticks: number of ticks to measure trend over
        midpoint_threshold: minimum midpoint to consider (e.g. 0.55)
        momentum_threshold: minimum midpoint change over lookback
        entry_deadline: max elapsed fraction to enter
        bet_fraction: fraction of cash to bet
    """

    name = "MidpointMomentum"

    def __init__(
        self,
        lookback_ticks=5,
        midpoint_threshold=0.55,
        momentum_threshold=0.03,
        entry_deadline=0.6,
        bet_fraction=0.5,
    ):
        self.lookback_ticks = lookback_ticks
        self.midpoint_threshold = midpoint_threshold
        self.momentum_threshold = momentum_threshold
        self.entry_deadline = entry_deadline
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "lookback_ticks": lookback_ticks,
            "midpoint_threshold": midpoint_threshold,
            "momentum_threshold": momentum_threshold,
            "entry_deadline": entry_deadline,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.entry_deadline:
            return HOLD
        if len(ctx.history) < self.lookback_ticks:
            return HOLD

        # Current midpoints
        up_now = ctx.sample.get("up_token_midpoint")
        down_now = ctx.sample.get("down_token_midpoint")
        if up_now is None or down_now is None:
            return HOLD

        # Midpoint from lookback_ticks ago
        past = ctx.history[-self.lookback_ticks]
        up_past = past.get("up_token_midpoint")
        down_past = past.get("down_token_midpoint")
        if up_past is None or down_past is None:
            return HOLD

        up_momentum = up_now - up_past
        down_momentum = down_now - down_past

        if up_now >= self.midpoint_threshold and up_momentum >= self.momentum_threshold:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif down_now >= self.midpoint_threshold and down_momentum >= self.momentum_threshold:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "lookback_ticks": [3, 5, 10],
            "midpoint_threshold": [0.52, 0.55, 0.60],
            "momentum_threshold": [0.02, 0.03, 0.05],
            "entry_deadline": [0.4, 0.6, 0.8],
            "bet_fraction": [0.25, 0.5, 1.0],
        }
