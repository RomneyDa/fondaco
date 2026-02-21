"""Buy in the direction BTC is moving if it exceeds a threshold early in the window."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class MomentumEntry(Strategy):
    """Params:
        entry_window: max elapsed fraction to enter (e.g. 0.3 = first 30%)
        price_threshold_bps: BTC move in basis points to trigger entry
        midpoint_confirm: minimum token midpoint to confirm market agrees
        stop_loss_pct: sell if midpoint drops this fraction below entry
        bet_fraction: fraction of cash to bet
    """

    name = "MomentumEntry"
    description = "Watches BTC price early in the window. If BTC moves more than a threshold and the token midpoint confirms, buys in that direction. Exits on stop-loss."

    def __init__(self, entry_window=0.2, price_threshold_bps=35.0,
                 midpoint_confirm=0.52, stop_loss_pct=0.15, bet_fraction=1.0):
        self.entry_window = entry_window
        self.price_threshold_bps = price_threshold_bps
        self.midpoint_confirm = midpoint_confirm
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self._entered = False
        self._virtual_side = None   # type: str
        self._virtual_tokens = 0.0
        self._entry_midpoint = 0.0
        self._params = {
            "entry_window": entry_window,
            "price_threshold_bps": price_threshold_bps,
            "midpoint_confirm": midpoint_confirm,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False
        self._virtual_side = None
        self._virtual_tokens = 0.0
        self._entry_midpoint = 0.0

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss
        if self._entered and self._virtual_side is not None:
            current_mid = up_mid if self._virtual_side == "up" else down_mid
            if current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.2f} down {:.1f}% from entry {:.2f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                sell_side = "sell_up" if self._virtual_side == "up" else "sell_down"
                tokens = self._virtual_tokens
                self._virtual_side = None
                self._virtual_tokens = 0.0
                return TradeAction(side=sell_side, amount=tokens)
            return HOLD

        # Already entered (and possibly exited via stop-loss) — no re-entry
        if self._entered:
            return HOLD
        if ctx.elapsed_fraction > self.entry_window:
            return HOLD

        btc_now = ctx.sample.get("btc_price_usd")
        if btc_now is None or ctx.initial_btc_price <= 0:
            return HOLD

        move_bps = ((btc_now - ctx.initial_btc_price) / ctx.initial_btc_price) * 10000.0

        if move_bps > self.price_threshold_bps and up_mid >= self.midpoint_confirm:
            self._entered = True
            self._virtual_side = "up"
            self._entry_midpoint = up_mid
            dollars = ctx.position.cash * self.bet_fraction
            if up_mid > 0:
                self._virtual_tokens = dollars / up_mid
            self.log("BTC +{:.1f}bps, up mid={:.2f} confirms, buying up".format(
                move_bps, up_mid,
            ))
            return TradeAction(side="buy_up", amount=dollars)
        elif move_bps < -self.price_threshold_bps and down_mid >= self.midpoint_confirm:
            self._entered = True
            self._virtual_side = "down"
            self._entry_midpoint = down_mid
            dollars = ctx.position.cash * self.bet_fraction
            if down_mid > 0:
                self._virtual_tokens = dollars / down_mid
            self.log("BTC {:.1f}bps, down mid={:.2f} confirms, buying down".format(
                move_bps, down_mid,
            ))
            return TradeAction(side="buy_down", amount=dollars)

        return HOLD

    def param_grid(self):
        # 6 * 6 * 3 * 3 * 3 = 972 combos
        return {
            "entry_window": [0.1, 0.15, 0.2, 0.3, 0.4, 0.5],
            "price_threshold_bps": [15.0, 20.0, 25.0, 30.0, 35.0, 40.0],
            "midpoint_confirm": [0.51, 0.52, 0.54],
            "stop_loss_pct": [0.10, 0.15, 0.20],
            "bet_fraction": [0.3, 0.5, 1.0],
        }
