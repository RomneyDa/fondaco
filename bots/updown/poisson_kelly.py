"""Poisson jump model for BTC up/down markets.

Models BTC tick-by-tick price changes as a compound Poisson process. Counts
significant up-jumps and down-jumps, estimates arrival rates, then uses the
Skellam distribution (normal approximation for the difference of two Poissons)
to compute the probability of BTC finishing above or below the strike.

v6: Fix stop-loss churning that destroyed Sharpe in v5 (0.03 → 0.19).
- No re-entry after stop-loss — one entry per session max. Churning
  (6-12 trades/session) was the #1 Sharpe killer due to fee multiplication.
- Late entry (min_elapsed=0.65): wait for Poisson model to accumulate signal.
  Early entry at noisy midpoints (0.50-0.65) was picking wrong sides.
- Higher edge threshold (0.10): only enter when model is confident.
- max_price cap (0.90): safety guard, but edge_min already prevents overpaying.
- Half-Kelly sizing: f*=0.67 → cash_pct=33.5%. 38% win rate over 200 sessions.
"""

import math
from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import norm_cdf
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Param, Strategy


class PoissonKelly(Strategy):
    """Poisson jump counting for up/down binary markets."""

    name = "PoissonKelly"
    description = (
        "Models BTC price as a Poisson jump process, estimates win probability "
        "via Skellam distribution. External Kelly sizing via session_cash_ratio."
    )
    version = "11"
    requires_full_session = True

    # --- Tunable params ---
    jump_bps = Param(default=0.18, min=0.10, max=0.30)
    min_elapsed = Param(default=0.65, min=0.50, max=0.80)
    edge_min = Param(default=0.10, min=0.05, max=0.20)
    max_price = Param(default=0.90, min=0.80, max=0.96)
    stop_loss_pct = Param(default=0.10, min=0.05, max=0.15)

    # --- Fixed params ---
    min_jumps = Param(default=8, tunable=False)
    no_new_after = Param(default=0.95, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._reset_state()

    def _reset_state(self):
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._stopped_out = False
        self._last_trade_time_s = -999.0

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._reset_state()

    def _compute_p_up(self, ctx):
        # type: (TickContext) -> Optional[float]
        """Compute P(BTC finishes above strike) using Poisson jump model."""
        btc = ctx.sample.get("btc_price_usd")
        strike = ctx.initial_btc_price
        if btc is None or strike <= 0:
            return None

        elapsed = ctx.elapsed_fraction
        if elapsed <= 0.01:
            return None

        remaining = 1.0 - elapsed
        if remaining <= 0.01:
            return None

        total_jumps = self._up_jumps + self._down_jumps
        if total_jumps < self.min_jumps:
            return None

        # Estimate arrival rates (jumps per unit elapsed fraction)
        lambda_up = self._up_jumps / elapsed
        lambda_down = self._down_jumps / elapsed

        # Expected jumps in remaining time
        mu_up = lambda_up * remaining
        mu_down = lambda_down * remaining

        # Current BTC displacement expressed as equivalent net jumps
        displacement_bps = (btc / strike - 1.0) * BPS_DIVISOR
        net_current = displacement_bps / max(self.jump_bps, 0.01)

        # Skellam distribution: X = Poisson(mu_up) - Poisson(mu_down)
        # P(BTC up at end) ~ P(net_current + X > 0) = Phi(z)
        skellam_mean = mu_up - mu_down
        skellam_var = mu_up + mu_down

        if skellam_var < 1e-10:
            if net_current > 0:
                return 1.0
            elif net_current < 0:
                return 0.0
            return 0.5

        z = (net_current + skellam_mean) / math.sqrt(skellam_var)
        return norm_cdf(z)

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Count jumps ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * BPS_DIVISOR
            if ret_bps >= self.jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self.jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Manage existing position: stop-loss ---
        if self._held_side(ctx) is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid, self.stop_loss_pct)
            if exit_action is not None:
                if exit_action is not HOLD:
                    self._stopped_out = True
                return exit_action
            return HOLD

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # No re-entry after stop-loss — churning destroys Sharpe via fees
        if self._stopped_out:
            return HOLD

        # Only one entry per session
        if self._entry_count > 0:
            return HOLD

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # --- Compute model probability ---
        p_up = self._compute_p_up(ctx)
        if p_up is None:
            return HOLD

        p_down = 1.0 - p_up

        # Edge for each side
        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        # Pick the better side (must clear edge threshold and max_price)
        if edge_up >= edge_down and edge_up >= self.edge_min and up_mid <= self.max_price:
            side = "buy_up"
            midpoint = up_mid
            edge = edge_up
            p = p_up
        elif edge_down >= self.edge_min and down_mid <= self.max_price:
            side = "buy_down"
            midpoint = down_mid
            edge = edge_down
            p = p_down
        else:
            self.log("HOLD: p_up={:.3f} up_mid={:.3f} e_up={:.3f} | "
                     "p_dn={:.3f} dn_mid={:.3f} e_dn={:.3f} | "
                     "jumps={}/{}".format(
                         p_up, up_mid, edge_up,
                         p_down, down_mid, edge_down,
                         self._up_jumps, self._down_jumps))
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = midpoint
        self._last_trade_time_s = ctx.elapsed_seconds

        # Bet full session cash (sizing handled externally via session_cash_ratio)
        dollars = ctx.position.cash

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} ${:.2f} | "
                 "jumps={}/{} at {:.0f}%".format(
                     side, p, midpoint, edge, dollars,
                     self._up_jumps, self._down_jumps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint, cancel_after_s=10)

