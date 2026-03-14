"""Regime-gated Poisson/Kelly — skips low-vol coin-flip sessions.

PoissonKelly's core model (jump counting → Skellam P(up) → edge entry)
works well when BTC is actually moving, but bleeds in low-vol sessions
where price is flat and the model picks a side on noise. Adding a regime
gate filters those sessions out.

v1: Initial regime-gated version based on PoissonKelly v11.
v2: Cut no_new_after from 0.95 to 0.90 — entries at 90-95% elapsed
    bought at 0.70-0.75 with terrible asymmetry (wins +$35, losses -$100).
    Sharpe 0.031 → 0.056 on 750 sessions. Optimized edge_min=0.25,
    jump_bps=0.18, max_price=0.85, min_elapsed=0.75, stop_loss=0.05.
v3: Circuit breaker — tracks recent trade outcomes across sessions.
    If loss rate exceeds threshold over last N trades, switches to dry-run
    mode (evaluates but doesn't execute). Resumes after a dry-run pick
    proves correct. Prevents compounding losses during bad streaks.
"""

import math
from collections import deque
from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import norm_cdf
from core.strategies.regime_gated import RegimeGatedStrategy
from core.strategies.base import Param
from core.trader import HOLD, TradeAction, TickContext


class PoissonKellyGated(RegimeGatedStrategy):
    """Poisson jump model, regime-gated to skip low-vol sessions."""

    name = "PoissonKellyGated"
    description = (
        "Poisson jump model with regime gate. Skips low-vol (coin-flip) "
        "sessions and stale data. Edge-based entry with stop-loss. "
        "Circuit breaker pauses trading after consecutive losses."
    )
    version = "3"
    requires_full_session = True

    # --- Regime gate (NOT tunable) ---
    required_vol = {"medium", "high"}
    required_trend = None  # any trend is fine
    skip_stale_data = True

    # --- Tunable params ---
    jump_bps = Param(default=0.18, min=0.10, max=0.25)
    min_elapsed = Param(default=0.75, min=0.65, max=0.85)
    edge_min = Param(default=0.25, min=0.15, max=0.35)
    max_price = Param(default=0.85, min=0.80, max=0.92)
    stop_loss_pct = Param(default=0.05, min=0.03, max=0.10)

    # --- Fixed params ---
    min_jumps = Param(default=8, tunable=False)
    no_new_after = Param(default=0.90, tunable=False)
    cb_lookback = Param(default=6, tunable=False)
    cb_max_losses = Param(default=2, tunable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Circuit breaker state — persists across sessions
        self._cb_results = deque(maxlen=self.cb_lookback)  # type: deque  # True=win, False=loss
        self._cb_dry_run = False
        self._last_bought_side = None  # type: Optional[str]  # "up" or "down"
        self._dry_run_side = None  # type: Optional[str]  # shadow pick during dry-run

    def _on_session_start_inner(self, metadata):
        # type: (dict) -> None
        # --- Score previous session's trade ---
        self._score_last_trade(metadata)

        # --- Check circuit breaker ---
        self._update_circuit_breaker()

        # --- Reset per-session state ---
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._stopped_out = False
        self._last_trade_time_s = -999.0
        self._dry_run_side = None

    def _score_last_trade(self, metadata):
        # type: (dict) -> None
        """Check if last session's trade (live or dry-run) was correct."""
        side = self._last_bought_side or self._dry_run_side
        if side is None:
            return

        prior = metadata.get("_prior_session_summaries")
        if not prior:
            self._last_bought_side = None
            return

        last_outcome = prior[-1].get("outcome")  # "Up" or "Down"
        if last_outcome is None:
            self._last_bought_side = None
            return

        won = (side == "up" and last_outcome == "Up") or \
              (side == "down" and last_outcome == "Down")

        if self._cb_dry_run and self._dry_run_side is not None:
            # Dry-run pick: if correct, resume live trading
            if won:
                self._cb_dry_run = False
                self._cb_results.clear()
                self.log("CIRCUIT BREAKER OFF: dry-run pick was correct ({} = {})".format(
                    side, last_outcome))
            else:
                self.log("CIRCUIT BREAKER: dry-run pick wrong ({} != {})".format(
                    side, last_outcome))
        elif self._last_bought_side is not None:
            # Live trade: record result
            self._cb_results.append(won)

        self._last_bought_side = None

    def _update_circuit_breaker(self):
        # type: () -> None
        """Trip circuit breaker if too many recent losses."""
        if self._cb_dry_run:
            return
        if len(self._cb_results) < self.cb_lookback:
            return
        losses = sum(1 for r in self._cb_results if not r)
        if losses >= self.cb_max_losses:
            self._cb_dry_run = True
            self.log("CIRCUIT BREAKER ON: {} losses in last {} trades".format(
                losses, len(self._cb_results)))

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

        lambda_up = self._up_jumps / elapsed
        lambda_down = self._down_jumps / elapsed
        mu_up = lambda_up * remaining
        mu_down = lambda_down * remaining

        displacement_bps = (btc / strike - 1.0) * BPS_DIVISOR
        net_current = displacement_bps / max(self.jump_bps, 0.01)

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

    def on_tick_active(self, ctx):
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

        # No re-entry after stop-loss
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

        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        if edge_up >= edge_down and edge_up >= self.edge_min and up_mid <= self.max_price:
            side = "buy_up"
            picked_side = "up"
            midpoint = up_mid
            edge = edge_up
            p = p_up
        elif edge_down >= self.edge_min and down_mid <= self.max_price:
            side = "buy_down"
            picked_side = "down"
            midpoint = down_mid
            edge = edge_down
            p = p_down
        else:
            return HOLD

        # --- Circuit breaker: dry-run mode ---
        if self._cb_dry_run:
            if self._dry_run_side is None:
                self._dry_run_side = picked_side
                self._entry_count += 1
                self.log("SHADOW {}: p={:.3f} mid={:.3f} edge={:.3f} | "
                         "jumps={}/{} at {:.0f}% (circuit breaker active)".format(
                             side, p, midpoint, edge,
                             self._up_jumps, self._down_jumps,
                             ctx.elapsed_fraction * 100))
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = midpoint
        self._last_trade_time_s = ctx.elapsed_seconds
        self._last_bought_side = picked_side

        dollars = ctx.position.cash

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} ${:.2f} | "
                 "jumps={}/{} at {:.0f}%".format(
                     side, p, midpoint, edge, dollars,
                     self._up_jumps, self._down_jumps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint, cancel_after_s=10)
