"""Place a cheap GTD limit buy early, catching midpoint dips for $1 payout.

Posts one limit buy order at a low price with a time-to-live (GTD expiration).
If the midpoint dips to the order price before expiration, it fills and holds
to resolution. Otherwise the order expires with $0 PnL.

side_mode controls which token to buy:
  - "cheaper": buy whichever token has the lower midpoint (contrarian)
  - "pricier": buy whichever token has the higher midpoint (momentum)

btc_cancel_bps: if BTC moves this many bps against the order direction,
cancel the pending order to avoid a losing fill.
"""

from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


class BargainHunter(Strategy):
    """Place a cheap GTD limit buy early, catch midpoint dips for $1 payout."""

    name = "BargainHunter"
    description = (
        "Posts one cheap GTD limit buy early in the session. "
        "If midpoint dips to order price, fills and holds to resolution."
    )

    def __init__(
        self,
        buy_price=0.57,
        expiration_frac=0.30,
        side_mode="pricier",
        btc_cancel_bps=2.0,
    ):
        self.buy_price = buy_price
        self.expiration_frac = expiration_frac
        self.side_mode = side_mode
        self.btc_cancel_bps = btc_cancel_bps
        self._params = {
            "buy_price": buy_price,
            "expiration_frac": expiration_frac,
            "side_mode": side_mode,
            "btc_cancel_bps": btc_cancel_bps,
        }

        # Per-session state
        self._order_placed = False
        self._placed_side = None  # type: Optional[str]
        self._window_seconds = 300

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._order_placed = False
        self._placed_side = None
        self._window_seconds = metadata.get("window", 300)

    def on_tick(self, ctx):
        # type: (Any) -> TradeAction

        # --- BTC price guard: cancel pending order if BTC moved against us ---
        if self._placed_side is not None and self.btc_cancel_bps is not None:
            # Check if order already filled (no longer in open_orders)
            open_orders = getattr(ctx, "open_orders", [])
            if not open_orders:
                # Order filled or expired — stop monitoring
                self._placed_side = None
            else:
                btc_now = ctx.sample.get("btc_price_usd")
                if btc_now is not None and ctx.initial_btc_price is not None:
                    initial = ctx.initial_btc_price
                    if self._placed_side == "buy_up":
                        # Cancel if BTC dropped
                        move_bps = (initial - btc_now) / initial * 10000
                    else:
                        # Cancel if BTC rose
                        move_bps = (btc_now - initial) / initial * 10000

                    if move_bps > self.btc_cancel_bps:
                        cancel_side = "cancel_up" if self._placed_side == "buy_up" else "cancel_down"
                        self.log("BTC guard: cancel {} ({:.1f} bps > {:.1f})".format(
                            self._placed_side, move_bps, self.btc_cancel_bps))
                        self._placed_side = None
                        return TradeAction(side=cancel_side)

            return HOLD

        if self._order_placed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if up_mid is None or down_mid is None:
            return HOLD

        # Don't place if we're already past the expiration fraction
        if ctx.elapsed_fraction >= self.expiration_frac:
            return HOLD

        # Pick side based on mode
        if self.side_mode == "cheaper":
            side = "buy_up" if up_mid <= down_mid else "buy_down"
        else:  # "pricier" — momentum: buy the token the market favors
            side = "buy_up" if up_mid >= down_mid else "buy_down"

        # Compute TTL: time from now until expiration_frac
        ttl_seconds = (self.expiration_frac - ctx.elapsed_fraction) * self._window_seconds
        if ttl_seconds <= 0:
            return HOLD

        self._order_placed = True
        if self.btc_cancel_bps is not None:
            self._placed_side = side

        chosen_mid = up_mid if side == "buy_up" else down_mid
        self.log("GTD {} @ {:.2f} (mid={:.3f}, ttl={:.0f}s, mode={})".format(
            side, self.buy_price, chosen_mid, ttl_seconds, self.side_mode))

        return TradeAction(
            side=side,
            amount=ctx.position.cash,
            price=self.buy_price,
            expiration_seconds=ttl_seconds,
        )

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 5 * 5 * 2 * 4 = 200 combos
        return {
            "buy_price": [0.47, 0.52, 0.57, 0.62, 0.67],
            "expiration_frac": [0.25, 0.30, 0.35, 0.40, 0.45],
            "side_mode": ["cheaper", "pricier"],
            "btc_cancel_bps": [2.0, 3.0, 5.0, 7.0],
        }
