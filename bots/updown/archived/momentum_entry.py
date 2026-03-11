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
    version = "1"

    def __init__(self, entry_window=0.2, price_threshold_bps=35.0,
                 midpoint_confirm=0.52, stop_loss_pct=0.15, bet_fraction=1.0):
        self.entry_window = entry_window
        self.price_threshold_bps = price_threshold_bps
        self.midpoint_confirm = midpoint_confirm
        self.stop_loss_pct = stop_loss_pct
        self.bet_fraction = bet_fraction
        self._entered = False
        self._entry_midpoint = 0.0
        self._exited = False  # True after stop-loss sell — done for session
        self._params = {
            "entry_window": entry_window,
            "price_threshold_bps": price_threshold_bps,
            "midpoint_confirm": midpoint_confirm,
            "stop_loss_pct": stop_loss_pct,
            "bet_fraction": bet_fraction,
        }

    def on_session_start(self, metadata):
        self._entered = False
        self._entry_midpoint = 0.0
        self._exited = False

    def _held_side(self, ctx):
        """Return which side we hold, or None."""
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        if up_mid is None or down_mid is None:
            return HOLD

        # Manage existing position — check stop-loss
        held = self._held_side(ctx)
        if held is not None:
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.2f} down {:.1f}% from entry {:.2f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)
            return HOLD

        # Already filled and exited (stop-loss) — done for this session
        if self._exited:
            return HOLD

        # Already tried to enter but fill failed — no re-entry
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
            self._entry_midpoint = up_mid
            dollars = ctx.position.cash * self.bet_fraction
            self.log("BTC +{:.1f}bps, up mid={:.2f} confirms, buying up".format(
                move_bps, up_mid,
            ))
            return TradeAction(side="buy_up", amount=dollars, price=up_mid)
        elif move_bps < -self.price_threshold_bps and down_mid >= self.midpoint_confirm:
            self._entered = True
            self._entry_midpoint = down_mid
            dollars = ctx.position.cash * self.bet_fraction
            self.log("BTC {:.1f}bps, down mid={:.2f} confirms, buying down".format(
                move_bps, down_mid,
            ))
            return TradeAction(side="buy_down", amount=dollars, price=down_mid)

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
