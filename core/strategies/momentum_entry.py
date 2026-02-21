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
    description = "Watches BTC price early in the window. If BTC moves more than a threshold (in basis points), buys in that direction. Only enters during the first portion of the window to catch early trends."

    def __init__(self, entry_window=0.2, price_threshold_bps=25.0, bet_fraction=0.5):
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
        # 8 × 8 × 4 = 256 combos
        return {
            "entry_window": [0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6],
            "price_threshold_bps": [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0],
            "bet_fraction": [0.3, 0.5, 0.75, 1.0],
        }
