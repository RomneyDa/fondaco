"""DipSniper — high-conviction directional entry with limit orders and loss cutting.

Combines multiple signals (midpoint level, BTC momentum, midpoint trend) to predict
the winning side early in the session. Buys via limit order at a discount to the
favored token's midpoint. After fill, applies confirmation-based loss management:
holds winners to resolution for the $1 payout, cuts losers early to cap downside.

Key insight: by entering via limit order (not FAK at 0.99), the risk/reward is
dramatically better. Buying at midpoint 0.55 gives +$0.45/token on win vs -$0.55/token
on loss, compared to FAK's +$0.01/token vs -$0.99/token.
"""

from typing import Any, Dict, List, Optional

from core.strategies.base import Strategy
from core.trader import HOLD, TradeAction, TickContext


class DipSniper(Strategy):
    name = "DipSniper"
    description = (
        "High-conviction directional entry with limit orders and "
        "confirmation-based loss cutting."
    )

    def __init__(
        self,
        signal_frac=0.30,
        min_midpoint=0.68,
        min_btc_bps=0.0,
        limit_offset=0.025,
        order_expiry_frac=0.12,
        confirm_secs=50.0,
        soft_threshold=0.01,
        hard_sl=0.18,
        bet_fraction=1.0,
    ):
        # type: (...) -> None
        self.signal_frac = signal_frac
        self.min_midpoint = min_midpoint
        self.min_btc_bps = min_btc_bps
        self.limit_offset = limit_offset
        self.order_expiry_frac = order_expiry_frac
        self.confirm_secs = confirm_secs
        self.soft_threshold = soft_threshold
        self.hard_sl = hard_sl
        self.bet_fraction = bet_fraction
        self._params = {
            "signal_frac": signal_frac,
            "min_midpoint": min_midpoint,
            "min_btc_bps": min_btc_bps,
            "limit_offset": limit_offset,
            "order_expiry_frac": order_expiry_frac,
            "confirm_secs": confirm_secs,
            "soft_threshold": soft_threshold,
            "hard_sl": hard_sl,
            "bet_fraction": bet_fraction,
        }

        # Per-session state
        self._side = None  # type: Optional[str]  # "up" or "down"
        self._order_placed = False
        self._order_seen = False  # True once order appeared in open_orders
        self._filled = False
        self._exited = False
        self._exit_sell_attempted = False
        self._buy_price = 0.0
        self._fill_elapsed = 0.0
        self._confirmed = False
        self._window_secs = 300.0
        self._give_up = False  # stop retrying after placement failure

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._side = None
        self._order_placed = False
        self._order_seen = False
        self._filled = False
        self._exited = False
        self._exit_sell_attempted = False
        self._buy_price = 0.0
        self._fill_elapsed = 0.0
        self._confirmed = False
        self._window_secs = metadata.get("window", 300)
        self._give_up = False

    def _pick_direction(self, ctx):
        # type: (TickContext) -> Optional[str]
        """Return 'up' or 'down' if signals converge, else None."""
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")
        btc_price = ctx.sample.get("btc_price_usd")

        if up_mid is None or down_mid is None:
            return None

        # Primary signal: which midpoint is higher
        if up_mid > down_mid:
            favored = "up"
            favored_mid = up_mid
        elif down_mid > up_mid:
            favored = "down"
            favored_mid = down_mid
        else:
            return None

        # Must exceed minimum midpoint threshold
        if favored_mid < self.min_midpoint:
            return None

        # Optional BTC momentum confirmation
        if self.min_btc_bps > 0 and btc_price and ctx.initial_btc_price > 0:
            btc_bps = (btc_price / ctx.initial_btc_price - 1) * 10000
            if favored == "up" and btc_bps < self.min_btc_bps:
                return None
            if favored == "down" and btc_bps > -self.min_btc_bps:
                return None

        return favored

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        if self._give_up:
            return HOLD

        if self._exited:
            # Try to sell once. The Trader/ClobExecutor handles balance
            # refresh + retry internally. If it still fails, accept loss
            # at resolution rather than spamming the API.
            if not self._exit_sell_attempted:
                tokens = ctx.position.up_tokens if self._side == "up" else ctx.position.down_tokens
                mid_key = "up_token_midpoint" if self._side == "up" else "down_token_midpoint"
                mid = ctx.sample.get(mid_key)
                if tokens > 0 and mid is not None and mid > 0:
                    self._exit_sell_attempted = True
                    return TradeAction(
                        side="sell_up" if self._side == "up" else "sell_down",
                        amount=tokens,
                        price=mid,
                    )
            return HOLD

        # --- Phase: Position management (if filled) ---
        if self._filled:
            mid_key = "up_token_midpoint" if self._side == "up" else "down_token_midpoint"
            mid = ctx.sample.get(mid_key)
            if mid is None:
                return HOLD

            elapsed_secs = ctx.sample.get("elapsed_seconds", 0.0)

            # Hard stop-loss: always active
            if mid <= self._buy_price - self.hard_sl:
                self._exited = True
                tokens = ctx.position.up_tokens if self._side == "up" else ctx.position.down_tokens
                if tokens > 0:
                    self.log("STOP LOSS: mid={:.3f} buy={:.3f} drop={:.3f}".format(
                        mid, self._buy_price, self._buy_price - mid))
                    return TradeAction(
                        side="sell_up" if self._side == "up" else "sell_down",
                        amount=tokens,
                        price=mid,
                    )
                return HOLD

            # Soft confirmation check: after confirm_secs, cut if midpoint below threshold
            if not self._confirmed:
                if elapsed_secs >= self._fill_elapsed + self.confirm_secs:
                    if mid < self._buy_price - self.soft_threshold:
                        self._exited = True
                        tokens = ctx.position.up_tokens if self._side == "up" else ctx.position.down_tokens
                        if tokens > 0:
                            self.log("CUT LOSS: mid={:.3f} buy={:.3f} after {:.0f}s".format(
                                mid, self._buy_price, elapsed_secs - self._fill_elapsed))
                            return TradeAction(
                                side="sell_up" if self._side == "up" else "sell_down",
                                amount=tokens,
                                price=mid,
                            )
                        return HOLD
                    else:
                        self._confirmed = True
                        self.log("CONFIRMED: mid={:.3f} buy={:.3f}".format(mid, self._buy_price))

            # Hold to resolution
            return HOLD

        # --- Phase: Detect limit order fill or expiry ---
        if self._order_placed and not self._filled:
            tokens = ctx.position.up_tokens if self._side == "up" else ctx.position.down_tokens
            if tokens > 0:
                self._filled = True
                self._fill_elapsed = ctx.sample.get("elapsed_seconds", 0.0)
                self.log("FILLED at {:.3f}, elapsed={:.1f}s".format(
                    self._buy_price, self._fill_elapsed))
                return HOLD

            has_open = len(ctx.open_orders) > 0
            if has_open:
                self._order_seen = True
                return HOLD

            # No open orders — either expired naturally or never posted
            if not self._order_seen:
                # Order never appeared in open_orders — placement failed
                # (e.g. CLOB rejected due to insufficient balance).
                # Give up to avoid spamming the API.
                self.log("ORDER FAILED TO PLACE: {} at {:.3f} (giving up)".format(
                    self._side, self._buy_price))
                self._give_up = True
                return HOLD

            # Order was live but expired — reset to allow retry
            self.log("ORDER EXPIRED: {} at {:.3f}, elapsed_frac={:.2f}".format(
                self._side, self._buy_price, ctx.elapsed_fraction))
            self._order_placed = False
            self._order_seen = False
            self._side = None
            self._buy_price = 0.0
            # Fall through to re-evaluate direction below

        # --- Phase: Wait for signal window ---
        if ctx.elapsed_fraction < self.signal_frac:
            return HOLD

        # --- Phase: Direction decision and order placement ---
        direction = self._pick_direction(ctx)
        if direction is None:
            return HOLD

        mid_key = "up_token_midpoint" if direction == "up" else "down_token_midpoint"
        current_mid = ctx.sample.get(mid_key)
        if current_mid is None:
            return HOLD

        # Compute limit price
        limit_price = round(current_mid - self.limit_offset, 2)
        if limit_price <= 0.05 or limit_price >= 0.95:
            return HOLD

        # Compute order expiration
        expiration_secs = self.order_expiry_frac * self._window_secs
        if expiration_secs < 5:
            return HOLD

        self._side = direction
        self._order_placed = True
        self._order_seen = False
        self._buy_price = limit_price

        dollars = ctx.max_buy_amount * self.bet_fraction

        self.log("ORDER: {} at {:.3f} (mid={:.3f} offset={:.3f}) exp={:.0f}s".format(
            direction, limit_price, current_mid, self.limit_offset, expiration_secs))

        return TradeAction(
            side="buy_up" if direction == "up" else "buy_down",
            amount=dollars,
            price=limit_price,
            expiration_seconds=expiration_secs,
        )

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 5 * 4 * 4 * 4 * 3 = 960 combos
        return {
            "signal_frac": [0.26, 0.28, 0.30, 0.31, 0.33],
            "min_midpoint": [0.62, 0.64, 0.65, 0.68],
            "limit_offset": [0.015, 0.018, 0.022, 0.025],
            "order_expiry_frac": [0.12, 0.14, 0.16, 0.18],
            "hard_sl": [0.13, 0.15, 0.18],
        }
