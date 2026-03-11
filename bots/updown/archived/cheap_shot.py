"""Buy cheap tokens early when BTC price is near the session opening price.

When BTC hasn't moved far from the opening, both tokens are cheap (~0.50),
giving roughly 2:1 odds. Uses slight BTC momentum to pick the direction.
"""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class CheapShot(Strategy):
    """Buy cheap tokens early when BTC is near the opening price.

    Entry: Within entry_window, if BTC is within max_deviation_bps of the
    opening price, buy the side favored by recent BTC momentum. Only once.

    Exit: Take-profit if midpoint rises take_profit_pct above entry (any time).
    Stop-loss if midpoint drops stop_loss_pct below entry (only after
    stop_loss_after fraction of the window has elapsed, to avoid early noise).
    Otherwise holds to resolution for the $1 payout.

    Params:
        max_deviation_bps: max BTC deviation from opening in basis points
        entry_window: max elapsed fraction for entry
        momentum_lookback: ticks to look back for momentum signal
        min_momentum_bps: min momentum magnitude to pick a direction
        max_token_price: don't buy tokens above this price
        stop_loss_pct: sell if midpoint drops this fraction below entry
        stop_loss_after: min elapsed fraction before stop-loss can trigger
        take_profit_pct: sell if midpoint rises this fraction above entry
        bet_fraction: fraction of cash to bet
    """

    name = "CheapShot"
    description = (
        "Buys cheap tokens early when BTC is near the opening price, "
        "using slight momentum to pick the winning direction at favorable odds."
    )
    version = "1"
    requires_full_session = True

    def __init__(
        self,
        max_deviation_bps=3.0,
        entry_window=0.25,
        momentum_lookback=50,
        min_momentum_bps=0.0,
        max_token_price=0.60,
        stop_loss_pct=0.15,
        stop_loss_after=0.5,
        take_profit_pct=0.20,
        bet_fraction=1.0,
    ):
        self.max_deviation_bps = max_deviation_bps
        self.entry_window = entry_window
        self.momentum_lookback = momentum_lookback
        self.min_momentum_bps = min_momentum_bps
        self.max_token_price = max_token_price
        self.stop_loss_pct = stop_loss_pct
        self.stop_loss_after = stop_loss_after
        self.take_profit_pct = take_profit_pct
        self.bet_fraction = bet_fraction
        self._params = {
            "max_deviation_bps": max_deviation_bps,
            "entry_window": entry_window,
            "momentum_lookback": momentum_lookback,
            "min_momentum_bps": min_momentum_bps,
            "max_token_price": max_token_price,
            "stop_loss_pct": stop_loss_pct,
            "stop_loss_after": stop_loss_after,
            "take_profit_pct": take_profit_pct,
            "bet_fraction": bet_fraction,
        }

        # Per-session state
        self._btc_history = []  # type: list
        self._entered = False
        self._exited = False
        self._entry_midpoint = 0.0
        self._entry_side = None  # type: str | None

    def on_session_start(self, metadata):
        self._btc_history = []
        self._entered = False
        self._exited = False
        self._entry_midpoint = 0.0
        self._entry_side = None

    def _held_side(self, ctx):
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def on_tick(self, ctx):
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        self._btc_history.append(btc)

        # -- Manage existing position: take-profit / stop-loss --
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                return HOLD
            current_mid = up_mid if held == "up" else down_mid
            sell_side = "sell_up" if held == "up" else "sell_down"
            tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

            # Take-profit: can trigger at any time
            if self._entry_midpoint > 0 and current_mid >= self._entry_midpoint * (1.0 + self.take_profit_pct):
                self.log("take-profit: mid={:.3f} up {:.1f}% from entry {:.3f}".format(
                    current_mid,
                    (current_mid / self._entry_midpoint - 1.0) * 100,
                    self._entry_midpoint,
                ))
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            # Stop-loss: only after stop_loss_after fraction of window has elapsed
            if ctx.elapsed_fraction >= self.stop_loss_after:
                if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                    self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                        current_mid,
                        (1.0 - current_mid / self._entry_midpoint) * 100,
                        self._entry_midpoint,
                    ))
                    self._exited = True
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # Already entered or exited — done for this session
        if self._entered or self._exited:
            return HOLD

        # Only enter during early window
        if ctx.elapsed_fraction >= self.entry_window:
            return HOLD

        # Check BTC proximity to opening price
        opening_btc = ctx.initial_btc_price
        if opening_btc <= 0:
            return HOLD

        deviation_bps = abs(btc - opening_btc) / opening_btc * 10000.0
        if deviation_bps > self.max_deviation_bps:
            return HOLD

        # Check token prices are cheap enough
        if up_mid > self.max_token_price and down_mid > self.max_token_price:
            return HOLD

        # Need enough history for momentum
        if len(self._btc_history) < self.momentum_lookback:
            return HOLD

        # Compute momentum over lookback window (bps change)
        lookback_btc = self._btc_history[-self.momentum_lookback]
        momentum_bps = (btc - lookback_btc) / lookback_btc * 10000.0

        # Pick direction based on momentum
        if abs(momentum_bps) < self.min_momentum_bps:
            # Momentum too weak — buy the cheaper token
            if up_mid <= down_mid and up_mid <= self.max_token_price:
                direction = "up"
            elif down_mid <= self.max_token_price:
                direction = "down"
            else:
                return HOLD
        elif momentum_bps > 0:
            direction = "up"
        else:
            direction = "down"

        midpoint = up_mid if direction == "up" else down_mid
        if midpoint > self.max_token_price:
            # Favored side too expensive, try the other
            direction = "down" if direction == "up" else "up"
            midpoint = down_mid if direction == "down" else up_mid
            if midpoint > self.max_token_price:
                return HOLD

        self._entered = True
        self._entry_side = direction
        self._entry_midpoint = midpoint

        side = "buy_up" if direction == "up" else "buy_down"
        self.log("ENTRY: {} mid={:.3f} dev={:.1f}bps mom={:.2f}bps at {:.0f}%".format(
            side, midpoint, deviation_bps, momentum_bps, ctx.elapsed_fraction * 100,
        ))

        return TradeAction(
            side=side,
            amount=ctx.position.cash * self.bet_fraction,
            price=midpoint,
        )

    def param_grid(self):
        # 4 * 3 * 3 * 3 * 3 * 3 = 972 combos
        return {
            "max_deviation_bps": [3.0, 5.0, 8.0, 12.0],
            "entry_window": [0.15, 0.25, 0.35],
            "momentum_lookback": [30, 50, 100],
            "max_token_price": [0.52, 0.55, 0.60],
            "stop_loss_pct": [0.15, 0.20, 0.30],
            "take_profit_pct": [0.20, 0.30, 0.50],
        }
