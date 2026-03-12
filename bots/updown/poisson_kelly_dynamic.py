"""PoissonKellyDynamic — Poisson/Kelly with volatility and trend adaptation.

Extends PoissonKelly by adapting key parameters to recent market conditions
using session-level statistics from the last N sessions:

1. **Volatility regime**: Scales jump_bps proportional to recent BTC volatility.
   High vol -> higher threshold (filters out noise), low vol -> lower threshold
   (captures real signal). Also scales stop_loss_pct (wider stops in high vol
   to avoid premature exits from normal swings).

2. **Trend bias**: EMA of recent session outcomes (Up/Down) shifts the model's
   P(up) estimate. In sustained trends, this nudges entry toward the trend
   direction when the Poisson model is uncertain.

3. **Multi-entry**: Allows multiple buy/sell cycles per session. The Poisson
   model's remaining-time math naturally requires stronger signals for later
   entries (Skellam variance shrinks with less remaining time). Edge escalation
   (edge_step) further penalizes subsequent entries to prevent churning.
   Cooldown between trades ensures new data accumulates before re-entry.
"""

import math
import os
import json
import glob
from typing import Any, Dict, List, Optional

from core.defaults import BPS_DIVISOR
from core.math_utils import norm_cdf
from core.volatility import compute_signed_volatility
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


# Reference session-level BTC volatility (std dev of 5-min returns in bps).
# Measured from ~500 sessions: rolling-20 mean ≈ 15 bps, range 8-37 bps.
_TYPICAL_SESSION_VOL_BPS = 15.0


class PoissonKellyDynamic(Strategy):
    """Poisson jump model with dynamic volatility and trend adaptation.

    Pure signal generator: detects directional edge via Poisson model with
    parameters adapted to recent market conditions. Bets full session cash;
    position sizing handled externally via session_cash_ratio.

    Allows multiple entries per session with cooldown and edge escalation.
    After a stop-loss, the model can re-enter if new data supports it.
    The Poisson model naturally handles remaining time — as the session
    progresses, Skellam variance shrinks, requiring stronger displacement
    for the same edge threshold.

    Params:
        jump_bps: base jump threshold (scaled by vol regime)
        min_jumps: minimum total jumps before allowing entry (warmup)
        min_elapsed: earliest entry point (elapsed fraction)
        no_new_after: latest entry point (elapsed fraction)
        edge_min: minimum edge (p_model - market_price) for entry
        edge_step: additional edge required per prior entry (anti-churn)
        max_price: don't buy tokens above this midpoint
        max_entries: maximum buy entries per session
        cooldown_ticks: minimum ticks between any two trades
        stop_loss_pct: base stop-loss fraction (scaled by vol regime)
        lookback: number of prior sessions for adaptation
        vol_scale: strength of volatility adaptation (0=off, 1=full)
        trend_strength: strength of trend bias on p_up (0=off)
    """

    name = "PoissonKellyDynamic"
    description = (
        "Poisson jump model with volatility-adapted jump threshold and "
        "trend bias from recent sessions. External Kelly sizing."
    )
    version = "4"
    requires_full_session = True

    def __init__(
        self,
        jump_bps=0.18,
        min_jumps=8,
        min_elapsed=0.65,
        no_new_after=0.95,
        edge_min=0.10,
        edge_step=0.03,
        max_price=0.90,
        max_entries=3,
        cooldown_ticks=72,
        stop_loss_pct=0.10,
        lookback=30,
        vol_scale=0.5,
        trend_strength=0.04,
    ):
        self.jump_bps = jump_bps
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.edge_min = edge_min
        self.edge_step = edge_step
        self.max_price = max_price
        self.max_entries = max_entries
        self.cooldown_ticks = cooldown_ticks
        self.stop_loss_pct = stop_loss_pct
        self.lookback = lookback
        self.vol_scale = vol_scale
        self.trend_strength = trend_strength
        self._params = {
            "jump_bps": jump_bps,
            "min_jumps": min_jumps,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "edge_min": edge_min,
            "edge_step": edge_step,
            "max_price": max_price,
            "max_entries": max_entries,
            "cooldown_ticks": cooldown_ticks,
            "stop_loss_pct": stop_loss_pct,
            "lookback": lookback,
            "vol_scale": vol_scale,
            "trend_strength": trend_strength,
        }

        # Adapted parameters (computed at session start from history)
        self._effective_jump_bps = jump_bps
        self._effective_stop_loss = stop_loss_pct
        self._trend_bias = 0.0  # applied to p_up: positive = bullish

        # Cache for live trading disk reads
        self._session_cache = {}  # type: Dict[str, Optional[Dict[str, Any]]]

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._last_trade_tick = -999

    # ------------------------------------------------------------------ #
    #  Historical data loading (same pattern as PoissonKellyHistorical)
    # ------------------------------------------------------------------ #

    def _load_prior_summaries(self, metadata):
        # type: (Dict[str, Any]) -> List[Dict[str, Any]]
        """Get prior session summaries from metadata or disk."""
        prior = metadata.get("_prior_session_summaries")
        if prior is not None:
            n = self.lookback
            return prior[-n:] if len(prior) > n else prior
        return self._load_sessions_from_disk(metadata)

    def _load_sessions_from_disk(self, metadata):
        # type: (Dict[str, Any]) -> List[Dict[str, Any]]
        """Load recent session summaries from disk for live trading."""
        from core.defaults import BASE_DIR

        slug = metadata.get("slug", "")
        slug_key = ""
        for suffix_len in [10, 13]:
            if len(slug) > suffix_len:
                candidate = slug[:-suffix_len].rstrip("-")
                sessions_dir = os.path.join(BASE_DIR, "data", "markets", candidate, "sessions")
                if os.path.isdir(sessions_dir):
                    slug_key = candidate
                    break

        if not slug_key:
            return []

        sessions_dir = os.path.join(BASE_DIR, "data", "markets", slug_key, "sessions")
        if not os.path.isdir(sessions_dir):
            return []

        pattern = os.path.join(sessions_dir, "**", "*.json")
        files = sorted(glob.glob(pattern, recursive=True))
        files = files[-(self.lookback + 5):]

        current_window = metadata.get("window_start_utc", "")
        summaries = []
        for f in files:
            if f in self._session_cache:
                cached = self._session_cache[f]
                if cached is not None and cached.get("_ws", "") < current_window:
                    summaries.append(cached)
                continue
            try:
                with open(f, "r") as fh:
                    data = json.load(fh)
                if data.get("outcome") is None:
                    self._session_cache[f] = None
                    continue
                meta = data.get("metadata", {})
                ws = meta.get("window_start_utc", "")
                initial_btc = data.get("initial_btc_price_usd")
                final_btc = data.get("final_btc_price_usd")
                outcome = data.get("outcome")
                if initial_btc and final_btc and outcome:
                    entry = {
                        "initial_btc": initial_btc,
                        "final_btc": final_btc,
                        "outcome": outcome,
                        "_ws": ws,
                    }
                    self._session_cache[f] = entry
                    if ws < current_window:
                        summaries.append(entry)
                else:
                    self._session_cache[f] = None
            except (json.JSONDecodeError, OSError):
                continue

        return summaries[-self.lookback:]

    # ------------------------------------------------------------------ #
    #  Dynamic parameter adaptation
    # ------------------------------------------------------------------ #

    def _adapt_params(self, summaries):
        # type: (List[Dict[str, Any]]) -> None
        """Compute adapted parameters from recent session history."""
        vol = compute_signed_volatility(summaries, self.lookback)
        if vol is not None:
            _, recent_vol = vol
            if recent_vol > 0:
                vol_ratio = max(0.5, min(recent_vol / _TYPICAL_SESSION_VOL_BPS, 2.5))
            else:
                vol_ratio = 1.0
            scale = 1.0 + self.vol_scale * (vol_ratio - 1.0)
            self._effective_jump_bps = self.jump_bps * scale
            self._effective_stop_loss = self.stop_loss_pct * scale
        else:
            self._effective_jump_bps = self.jump_bps
            self._effective_stop_loss = self.stop_loss_pct

        # --- Trend bias ---
        if self.trend_strength > 0 and summaries:
            n = len(summaries)
            alpha = 2.0 / (min(n, self.lookback) + 1)
            ema = 0.5
            for s in summaries:
                val = 1.0 if s.get("outcome") == "Up" else 0.0
                ema = alpha * val + (1.0 - alpha) * ema
            self._trend_bias = (ema - 0.5) * self.trend_strength
        else:
            self._trend_bias = 0.0

    # ------------------------------------------------------------------ #
    #  Session lifecycle
    # ------------------------------------------------------------------ #

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        summaries = self._load_prior_summaries(metadata)
        self._adapt_params(summaries)

        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_count = 0
        self._entry_midpoint = 0.0
        self._last_trade_tick = -999

        self.log("dynamic: {} sessions | jump_bps={:.3f} stop={:.3f} trend={:.4f}".format(
            len(summaries), self._effective_jump_bps,
            self._effective_stop_loss, self._trend_bias))

    # ------------------------------------------------------------------ #
    #  Core Poisson model
    # ------------------------------------------------------------------ #

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
        net_current = displacement_bps / max(self._effective_jump_bps, 0.01)

        skellam_mean = mu_up - mu_down
        skellam_var = mu_up + mu_down

        if skellam_var < 1e-10:
            if net_current > 0:
                return 1.0
            elif net_current < 0:
                return 0.0
            return 0.5

        z = (net_current + skellam_mean) / math.sqrt(skellam_var)
        p = norm_cdf(z)

        # Apply trend bias
        if self._trend_bias != 0.0:
            p = max(0.01, min(0.99, p + self._trend_bias))

        return p

    # ------------------------------------------------------------------ #
    #  Tick processing
    # ------------------------------------------------------------------ #

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Count jumps using adapted threshold ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * BPS_DIVISOR
            if ret_bps >= self._effective_jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self._effective_jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Manage existing position: stop-loss ---
        if self._held_side(ctx) is not None:
            exit_action = self._check_exit(ctx, up_mid, down_mid,
                                            self._effective_stop_loss,
                                            cooldown=self.cooldown_ticks)
            return exit_action if exit_action is not None else HOLD

        # --- No position: look for entry ---

        # Buy order expired without filling — reset entry state
        self._check_expired_entry(ctx)

        # Cooldown between trades
        if ctx.tick_index - self._last_trade_tick < self.cooldown_ticks:
            return HOLD

        # Max entries cap
        if self._entry_count >= self.max_entries:
            return HOLD

        # Timing guards
        if self._check_timing_guards(ctx, self.min_elapsed, self.no_new_after):
            return HOLD

        # --- Compute model probability ---
        p_up = self._compute_p_up(ctx)
        if p_up is None:
            return HOLD

        p_down = 1.0 - p_up

        # Edge escalation: require more edge for each subsequent entry
        effective_edge = self.edge_min + self.edge_step * self._entry_count

        # Edge for each side
        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        # Pick the better side (must clear edge threshold and max_price)
        if edge_up >= edge_down and edge_up >= effective_edge and up_mid <= self.max_price:
            side = "buy_up"
            midpoint = up_mid
            edge = edge_up
            p = p_up
        elif edge_down >= effective_edge and down_mid <= self.max_price:
            side = "buy_down"
            midpoint = down_mid
            edge = edge_down
            p = p_down
        else:
            return HOLD

        self._entry_count += 1
        self._entry_midpoint = midpoint
        self._last_trade_tick = ctx.tick_index

        dollars = ctx.position.cash

        self.log("ENTRY #{} {}: p={:.3f} mid={:.3f} edge={:.3f} (req={:.3f}) ${:.2f} | "
                 "jumps={}/{} jbps={:.3f} at {:.0f}%".format(
                     self._entry_count, side, p, midpoint, edge, effective_edge,
                     dollars, self._up_jumps, self._down_jumps,
                     self._effective_jump_bps,
                     ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint,
                           expiration_seconds=10)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 3*3*3*2*3*2*3*2 = 648 combos
        return {
            "jump_bps": [0.15, 0.18, 0.20],
            "min_elapsed": [0.60, 0.65, 0.70],
            "edge_min": [0.08, 0.10, 0.12],
            "edge_step": [0.0, 0.03],
            "max_entries": [1, 10],
            "stop_loss_pct": [0.08, 0.10],
            "vol_scale": [0.0, 0.5, 1.0],
            "trend_strength": [0.0, 0.04],
        }
