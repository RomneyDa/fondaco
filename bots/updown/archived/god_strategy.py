"""Contrarian cheap-token buyer using volatility and BTC direction.

Buys the token opposite to BTC's current move (contrarian). Entry score
uses random-walk time decay:

    score = volatility_bps * sqrt(1 - elapsed) / midpoint

Higher volatility, cheaper token, and more remaining time increase the
score. The sqrt(1-t) factor naturally penalizes late entries based on
random-walk reversal probability.

Includes a take-profit exit: if the purchased token's midpoint reaches
a linear target, sells early to lock in gains. Holds to resolution
otherwise (no re-entry after TP sell).
"""

import math
from typing import Any, Dict, List, Optional

from core.trader import HOLD, TradeAction
from bots.updown.archived.volatility_bargain import VolatilityBargain


class GodStrategy(VolatilityBargain):
    """Contrarian cheap-token buyer with take-profit.

    Uses BTC direction to pick the opposite token (contrarian), scores
    deals using random-walk time decay, and sells early if midpoint
    hits the take-profit target.

    Params (beyond VolatilityBargain volatility computation):
        threshold: minimum deal score to trigger a buy
        max_token_price: don't buy tokens above this midpoint
        min_btc_move_bps: minimum BTC move to establish direction
        max_btc_move_bps: skip already-decided markets
        tp_slope: slope of take-profit formula
        tp_intercept: intercept of take-profit formula
    """

    name = "GodStrategy"
    description = (
        "Contrarian cheap-token buyer. Buys the token opposite BTC's "
        "current direction when volatility makes it underpriced, with "
        "random-walk time decay scoring and take-profit exit."
    )

    def __init__(
        self,
        lookback_sessions=5,
        min_sessions=3,
        threshold=1.0,
        max_token_price=0.70,
        min_elapsed=0.03,
        min_btc_move_bps=0.5,
        max_btc_move_bps=30.0,
        tp_slope=1.5,
        tp_intercept=0.25,
        data_dir=None,
    ):
        # Pass to VolatilityBargain for volatility computation only.
        # slope_lookback/slope_threshold are unused but required by parent.
        super().__init__(
            lookback_sessions=lookback_sessions,
            min_sessions=min_sessions,
            threshold=threshold,
            max_token_price=max_token_price,
            min_elapsed=min_elapsed,
            slope_lookback=0,
            slope_threshold=0,
            data_dir=data_dir,
        )
        self.min_btc_move_bps = min_btc_move_bps
        self.max_btc_move_bps = max_btc_move_bps
        self.tp_slope = tp_slope
        self.tp_intercept = tp_intercept

        # Override _params to reflect our actual params (not parent's slope params)
        self._params = {
            "lookback_sessions": lookback_sessions,
            "min_sessions": min_sessions,
            "threshold": threshold,
            "max_token_price": max_token_price,
            "min_elapsed": min_elapsed,
            "min_btc_move_bps": min_btc_move_bps,
            "max_btc_move_bps": max_btc_move_bps,
            "tp_slope": tp_slope,
            "tp_intercept": tp_intercept,
        }

        # Per-session state
        self._bought_side = None   # type: Optional[str]  # "up" or "down"
        self._buy_price = 0.0
        self._sold = False

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        super().on_session_start(metadata)
        self._bought_side = None
        self._buy_price = 0.0
        self._sold = False

    def on_tick(self, ctx):
        # type: (Any) -> TradeAction
        if self._sold:
            return HOLD

        # If we already bought, check take-profit
        if self._bought_side is not None:
            if self._bought_side == "up":
                current_mid = ctx.sample.get("up_token_midpoint")
            else:
                current_mid = ctx.sample.get("down_token_midpoint")

            if current_mid is not None and self._buy_price > 0:
                tp_target = min(
                    self.tp_slope * self._buy_price + self.tp_intercept, 0.99
                )
                if current_mid >= tp_target:
                    sell_side = "sell_up" if self._bought_side == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if self._bought_side == "up" else ctx.position.down_tokens
                    if tokens > 0:
                        self._sold = True
                        self.log("TAKE PROFIT: mid={:.3f} target={:.3f} buy={:.3f}".format(
                            current_mid, tp_target, self._buy_price))
                        return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # --- Entry logic ---

        if self._volatility_bps is None or self._bought:
            return HOLD

        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        # BTC direction filter (contrarian)
        btc_now = ctx.sample.get("btc_price_usd")
        if btc_now is None or ctx.initial_btc_price <= 0:
            return HOLD

        btc_change_bps = ((btc_now - ctx.initial_btc_price) / ctx.initial_btc_price) * 10000.0
        abs_change = abs(btc_change_bps)

        # Need minimum move to establish direction, skip if too large
        if abs_change < self.min_btc_move_bps or abs_change > self.max_btc_move_bps:
            return HOLD

        # Contrarian: BTC up → buy Down, BTC down → buy Up
        if btc_change_bps > 0:
            target_side = "down"
        else:
            target_side = "up"

        # Get the target token's midpoint
        if target_side == "up":
            mid = ctx.sample.get("up_token_midpoint")
        else:
            mid = ctx.sample.get("down_token_midpoint")

        if mid is None or mid <= 0 or mid > self.max_token_price:
            return HOLD

        # Random-walk time factor: sqrt(1 - elapsed)
        remaining = max(1.0 - ctx.elapsed_fraction, 0.001)
        time_factor = math.sqrt(remaining)

        score = self._volatility_bps * time_factor / mid

        if score <= self.threshold:
            return HOLD

        # Buy the contrarian token
        self._bought = True
        buy_side = "buy_up" if target_side == "up" else "buy_down"
        self._bought_side = target_side
        self._buy_price = mid

        self.log("BUY {}: score={:.1f} mid={:.3f} vol={:.2f} elapsed={:.2f} btc_bps={:.1f}".format(
            target_side.upper(), score, mid, self._volatility_bps,
            ctx.elapsed_fraction, btc_change_bps))

        return TradeAction(
            side=buy_side,
            amount=ctx.position.cash,
            price=mid,
        )

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 4 * 4 * 3 * 3 * 3 * 3 = 1296 combos
        return {
            "threshold": [1.0, 3.0, 5.0, 10.0],
            "max_token_price": [0.50, 0.60, 0.70, 0.80],
            "min_btc_move_bps": [0.5, 1.0, 2.0],
            "max_btc_move_bps": [20.0, 30.0, 50.0],
            "tp_slope": [1.0, 1.25, 1.5],
            "tp_intercept": [0.20, 0.25, 0.30],
        }
