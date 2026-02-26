"""Buy cheap (underdog) tokens early, then manage with stop-loss and take-profit."""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class BargainSniper(Strategy):
    """Buy whichever token is cheap early in the session, then actively manage.

    Entry: In the first entry_window fraction of the session, if either token
    midpoint is in [min_price, max_price], buy it. Only one entry per session.

    Exit — stop-loss: if position value drops stop_loss_pct below entry value,
    sell immediately.

    Exit — take-profit with gap override: if position value rises take_profit_pct
    above entry value, sell — UNLESS the gap between Up and Down midpoints is
    >= hold_gap, in which case hold (market is strongly trending).

    Params:
        min_price: minimum token midpoint to consider buying
        max_price: maximum token midpoint to consider buying
        entry_window: maximum elapsed fraction for entry (0.20 = first 60s of 5m)
        stop_loss_pct: sell if value drops this fraction below entry value
        take_profit_pct: sell if value rises this fraction above entry value
        hold_gap: if midpoint gap >= this, override take-profit and hold
        bet_fraction: fraction of cash to bet
    """

    name = "BargainSniper"
    description = "Buys cheap tokens early in the session when they're in a target price range, then manages with stop-loss and take-profit. Overrides take-profit when the market gap is wide enough to let winners ride."

    def __init__(self, min_price=0.28, max_price=0.36, entry_window=0.20,
                 stop_loss_pct=0.20, take_profit_pct=0.30, hold_gap=0.40,
                 bet_fraction=1.0):
        self.min_price = min_price
        self.max_price = max_price
        self.entry_window = entry_window
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.hold_gap = hold_gap
        self.bet_fraction = bet_fraction
        self._params = {
            "min_price": min_price,
            "max_price": max_price,
            "entry_window": entry_window,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "hold_gap": hold_gap,
            "bet_fraction": bet_fraction,
        }
        self._entered = False
        self._entry_midpoint = 0.0

    def on_session_start(self, metadata):
        self._entered = False
        self._entry_midpoint = 0.0

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

        # Already in a position — check exits
        held = self._held_side(ctx)
        if held is not None:
            held_tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
            current_mid = up_mid if held == "up" else down_mid
            entry_value = self._entry_midpoint * held_tokens
            current_value = current_mid * held_tokens

            # Stop-loss
            if current_value <= entry_value * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: value ${:.2f} down {:.1f}% from entry ${:.2f}".format(
                    current_value, (1.0 - current_value / entry_value) * 100, entry_value,
                ))
                return self._exit(ctx)

            # Take-profit with gap override
            if current_value >= entry_value * (1.0 + self.take_profit_pct):
                gap = abs(up_mid - down_mid)
                if gap >= self.hold_gap:
                    self.log("take-profit hit but gap {:.2f} >= {:.2f}, holding".format(
                        gap, self.hold_gap,
                    ))
                    return HOLD  # market trending hard, let it ride
                self.log("take-profit: value ${:.2f} up {:.1f}% from entry ${:.2f}".format(
                    current_value, (current_value / entry_value - 1.0) * 100, entry_value,
                ))
                return self._exit(ctx)

            return HOLD

        # Not entered yet — look for entry in early window
        if not self._entered and ctx.elapsed_fraction < self.entry_window:
            up_in_range = self.min_price <= up_mid <= self.max_price
            down_in_range = self.min_price <= down_mid <= self.max_price

            if up_in_range and down_in_range:
                # Prefer the cheaper one
                if up_mid <= down_mid:
                    return self._enter("up", up_mid, ctx)
                else:
                    return self._enter("down", down_mid, ctx)
            elif up_in_range:
                return self._enter("up", up_mid, ctx)
            elif down_in_range:
                return self._enter("down", down_mid, ctx)

        return HOLD

    def _enter(self, direction, midpoint, ctx):
        """Enter a position."""
        dollars = ctx.position.cash * self.bet_fraction
        if dollars <= 0:
            return HOLD

        self._entered = True
        self._entry_midpoint = midpoint

        self.log("{} token {:.2f} in buy range [{:.2f}, {:.2f}]".format(
            direction, midpoint, self.min_price, self.max_price,
        ))
        side = "buy_up" if direction == "up" else "buy_down"
        return TradeAction(side=side, amount=dollars, price=midpoint)

    def _exit(self, ctx):
        """Exit current position."""
        held = self._held_side(ctx)
        sell_side = "sell_up" if held == "up" else "sell_down"
        tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        price = up_mid if held == "up" else down_mid
        self._entry_midpoint = 0.0
        return TradeAction(side=sell_side, amount=tokens, price=price)

    def param_grid(self):
        # 3 * 4 * 3 * 3 * 3 * 3 = 972 combos (bet_fraction fixed at 1.0)
        return {
            "min_price": [0.22, 0.28, 0.34],
            "max_price": [0.33, 0.36, 0.39, 0.42],
            "entry_window": [0.10, 0.20, 0.30],
            "stop_loss_pct": [0.15, 0.20, 0.25],
            "take_profit_pct": [0.20, 0.30, 0.40],
            "hold_gap": [0.35, 0.40, 0.45],
        }
