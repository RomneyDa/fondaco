"""Buy a cheap token early, then flip it for profit or cut losses.

Enters in the first entry_window seconds when a token midpoint is in the
target range (min_price..max_price).  After a mandatory hold_seconds cooldown,
exits on either a stop-loss (drop_pct) or a take-profit (gain_mult), with a
BTC gap override that lets winners ride when BTC has moved significantly.
"""

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class BrendaFlipper(Strategy):
    """Params:
        entry_window: max elapsed fraction for entry (default 0.25 = 75s in 300s)
        min_price: minimum midpoint to consider buying
        max_price: maximum midpoint to consider buying
        hold_seconds: seconds to hold before any sell is allowed
        drop_pct: sell if token value drops by this fraction (stop-loss)
        gain_mult: sell if token value reaches this multiple of entry (take-profit)
        btc_gap_override: if BTC has moved >= this many dollars from open,
                          suppress the take-profit sell and let the position ride
        bet_fraction: fraction of cash to bet
    """

    name = "BrendaFlipper"
    description = (
        "Buys a cheap token early, holds briefly, then flips on a gain target "
        "or cuts on a stop-loss. A BTC gap override lets big movers ride."
    )
    version = "1"

    def __init__(
        self,
        entry_window=0.25,
        min_price=0.10,
        max_price=0.29,
        hold_seconds=10.0,
        drop_pct=0.15,
        gain_mult=3.0,
        btc_gap_override=50.0,
        bet_fraction=1.0,
    ):
        self.entry_window = entry_window
        self.min_price = min_price
        self.max_price = max_price
        self.hold_seconds = hold_seconds
        self.drop_pct = drop_pct
        self.gain_mult = gain_mult
        self.btc_gap_override = btc_gap_override
        self.bet_fraction = bet_fraction
        self._params = {
            "entry_window": entry_window,
            "min_price": min_price,
            "max_price": max_price,
            "hold_seconds": hold_seconds,
            "drop_pct": drop_pct,
            "gain_mult": gain_mult,
            "btc_gap_override": btc_gap_override,
            "bet_fraction": bet_fraction,
        }

        # Per-session state
        self._entered = False
        self._exited = False
        self._entry_midpoint = 0.0
        self._entry_side = None  # type: str | None
        self._entry_tick = 0

    def on_session_start(self, metadata):
        self._entered = False
        self._exited = False
        self._entry_midpoint = 0.0
        self._entry_side = None
        self._entry_tick = 0
        self._window_seconds = metadata.get("window", 300)

    def _held_side(self, ctx):
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _elapsed_seconds(self, ctx):
        return ctx.elapsed_fraction * self._window_seconds

    def on_tick(self, ctx):
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # -- Manage existing position --
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                return HOLD

            current_mid = up_mid if held == "up" else down_mid
            sell_side = "sell_up" if held == "up" else "sell_down"
            tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens

            # Enforce minimum hold period
            elapsed_sec = self._elapsed_seconds(ctx)
            entry_sec = (self._entry_tick / max(ctx.tick_index, 1)) * elapsed_sec
            held_seconds = elapsed_sec - entry_sec
            if held_seconds < self.hold_seconds:
                return HOLD

            # Stop-loss: token dropped >= drop_pct from entry
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.drop_pct):
                self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            # Take-profit: token reached gain_mult of entry
            if self._entry_midpoint > 0 and current_mid >= self._entry_midpoint * self.gain_mult:
                # BTC gap override: if BTC moved far enough, let it ride
                btc_gap = abs(btc - ctx.initial_btc_price)
                if btc_gap >= self.btc_gap_override:
                    self.log("gain target hit but BTC gap ${:.0f} >= ${:.0f}, holding".format(
                        btc_gap, self.btc_gap_override,
                    ))
                    return HOLD

                self.log("take-profit: mid={:.3f} up {:.1f}x from entry {:.3f}".format(
                    current_mid,
                    current_mid / self._entry_midpoint,
                    self._entry_midpoint,
                ))
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # Already entered or exited — done for this session
        if self._entered or self._exited:
            return HOLD

        # -- Entry logic: buy a cheap token in the first entry_window --
        if ctx.elapsed_fraction >= self.entry_window:
            return HOLD

        # Find a token in the target price range
        candidates = []
        if self.min_price <= up_mid <= self.max_price:
            candidates.append(("up", up_mid))
        if self.min_price <= down_mid <= self.max_price:
            candidates.append(("down", down_mid))

        if not candidates:
            return HOLD

        # Pick the cheaper token
        direction, midpoint = min(candidates, key=lambda c: c[1])

        self._entered = True
        self._entry_side = direction
        self._entry_midpoint = midpoint
        self._entry_tick = ctx.tick_index

        side = "buy_up" if direction == "up" else "buy_down"
        self.log("ENTRY: {} mid={:.3f} at {:.0f}% elapsed".format(
            side, midpoint, ctx.elapsed_fraction * 100,
        ))

        return TradeAction(
            side=side,
            amount=ctx.position.cash * self.bet_fraction,
            price=midpoint,
        )

    def param_grid(self):
        # 5*5*5*4*3 = 1500 combos, centered on optimized defaults
        return {
            "entry_window": [0.20, 0.22, 0.25, 0.28, 0.30],
            "max_price": [0.25, 0.27, 0.29, 0.31, 0.33],
            "drop_pct": [0.10, 0.12, 0.15, 0.18, 0.20],
            "gain_mult": [2.0, 3.0, 5.0, 100.0],
            "hold_seconds": [5.0, 10.0, 15.0],
        }
