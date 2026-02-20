"""Buy in the direction BTC is moving if it exceeds a threshold early in the window."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class MomentumEntry(Strategy):
    """Params:
        entry_window: max elapsed fraction to enter (e.g. 0.3 = first 30%)
        price_threshold_bps: BTC move in basis points to trigger entry
        bet_fraction: fraction of cash to bet
    """

    name = "MomentumEntry"

    def __init__(self, entry_window=0.3, price_threshold_bps=5.0, bet_fraction=0.5):
        self.entry_window = entry_window
        self.price_threshold_bps = price_threshold_bps
        self.bet_fraction = bet_fraction
        self._entered = False
        self._params = {
            "entry_window": entry_window,
            "price_threshold_bps": price_threshold_bps,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False

    def on_tick(self, ctx):
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.entry_window:
            return HOLD

        btc_now = ctx.sample.get("btc_price_usd")
        if btc_now is None or ctx.initial_btc_price <= 0:
            return HOLD

        move_bps = ((btc_now - ctx.initial_btc_price) / ctx.initial_btc_price) * 10000.0

        if move_bps > self.price_threshold_bps:
            self._entered = True
            return TradeAction(side="buy_up", amount=ctx.position.cash * self.bet_fraction)
        elif move_bps < -self.price_threshold_bps:
            self._entered = True
            return TradeAction(side="buy_down", amount=ctx.position.cash * self.bet_fraction)

        return HOLD

    def param_grid(self):
        return {
            "entry_window": [0.2, 0.3, 0.5],
            "price_threshold_bps": [2.0, 5.0, 10.0, 20.0],
            "bet_fraction": [0.25, 0.5, 1.0],
        }
