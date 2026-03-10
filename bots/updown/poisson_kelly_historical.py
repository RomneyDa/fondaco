"""PoissonKellyHistorical — Poisson/Kelly with historical session priors.

Extends PoissonKelly by using data from the last N sessions to build
informative priors for the jump model:

1. **Historical jump rate prior**: Estimates baseline up/down jump arrival
   rates from BTC price moves across recent sessions. Instead of starting
   each session with zero jumps, the model begins with a Bayesian prior
   calibrated to recent market behavior.

2. **Trend bias**: Measures the recent win rate (fraction of sessions where
   BTC went up). A strong trend bias shifts the model's probability estimate,
   making it more likely to bet with the trend.

3. **Volatility regime**: Estimates recent BTC volatility from session price
   moves. High-vol regimes use tighter jump thresholds (more jumps detected),
   low-vol regimes use wider thresholds. This adapts the jump_bps parameter
   to current market conditions rather than using a fixed value.

The strategy loads prior session summaries from metadata (injected by the
Simulator in backtests, or loaded from session files in live trading).
"""

import math
import os
import json
import glob
from typing import Any, Dict, List, Optional

from core.defaults import CLOB_MIN_ORDER_TOKENS
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _norm_cdf(x):
    # type: (float) -> float
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class PoissonKellyHistorical(Strategy):
    """Poisson jump model with historical priors + Kelly sizing.

    Params:
        lookback_sessions: number of prior sessions to use for priors
        jump_bps: base jump threshold (adapted by volatility regime)
        vol_adapt: strength of volatility adaptation (0=off, 1=full)
        trend_weight: how much trend bias shifts the probability (0=off)
        prior_strength: weight of historical prior vs current-session data
            (in equivalent "pseudo-jumps" added to counts)
        min_jumps: min total jumps (including prior) before entry
        min_elapsed: earliest entry point (elapsed fraction)
        no_new_after: latest entry point (elapsed fraction)
        min_kelly: minimum Kelly fraction to trigger entry
        max_kelly: cap on Kelly fraction
        edge_min: minimum edge (p_model - market_price)
        stop_loss_pct: sell if midpoint drops this fraction below entry
    """

    name = "PoissonKellyHistorical"
    description = (
        "Poisson jump model with historical session priors for trend, "
        "volatility regime, and baseline jump rates. Kelly-sized bets."
    )
    version = "3"
    requires_full_session = True

    def __init__(
        self,
        lookback_sessions=50,
        jump_bps=0.30,
        vol_adapt=0,
        trend_weight=0.15,
        prior_strength=10.0,
        min_jumps=6,
        min_elapsed=0.30,
        no_new_after=0.70,
        min_kelly=0.10,
        max_kelly=1.0,
        edge_min=0.08,
        stop_loss_pct=0.06,
    ):
        self.lookback_sessions = lookback_sessions
        self.jump_bps = jump_bps
        self.vol_adapt = vol_adapt
        self.trend_weight = trend_weight
        self.prior_strength = prior_strength
        self.min_jumps = min_jumps
        self.min_elapsed = min_elapsed
        self.no_new_after = no_new_after
        self.min_kelly = min_kelly
        self.max_kelly = max_kelly
        self.edge_min = edge_min
        self.stop_loss_pct = stop_loss_pct
        self._params = {
            "lookback_sessions": lookback_sessions,
            "jump_bps": jump_bps,
            "vol_adapt": vol_adapt,
            "trend_weight": trend_weight,
            "prior_strength": prior_strength,
            "min_jumps": min_jumps,
            "min_elapsed": min_elapsed,
            "no_new_after": no_new_after,
            "min_kelly": min_kelly,
            "max_kelly": max_kelly,
            "edge_min": edge_min,
            "stop_loss_pct": stop_loss_pct,
        }

        # Computed from historical data at session start
        self._effective_jump_bps = jump_bps
        self._prior_up_jumps = 0.0
        self._prior_down_jumps = 0.0
        self._trend_bias = 0.0  # -0.5 to +0.5, positive = bullish

        # Cache of loaded session summaries (keyed by file path)
        self._session_cache = {}  # type: Dict[str, Dict[str, Any]]

        # Per-session state
        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None  # type: Optional[str]
        self._entry_midpoint = 0.0
        self._exited = False
        self._exit_sell_attempted = False

    def _load_prior_summaries(self, metadata):
        # type: (Dict[str, Any]) -> List[Dict[str, Any]]
        """Get prior session summaries from metadata.

        In backtests, the Simulator injects '_prior_session_summaries'.
        In live trading, we load from session files on disk.
        """
        prior = metadata.get("_prior_session_summaries")
        if prior is not None:
            # Already injected by simulator — use last N
            n = self.lookback_sessions
            return prior[-n:] if len(prior) > n else prior

        # Live mode: load from session files
        return self._load_sessions_from_disk(metadata)

    def _load_sessions_from_disk(self, metadata):
        # type: (Dict[str, Any]) -> List[Dict[str, Any]]
        """Load recent session summaries from disk for live trading."""
        from core.defaults import BASE_DIR

        # Determine market slug from metadata
        slug = metadata.get("slug", "")
        # Extract slug key (e.g., "btc-updown-5m" from "btc-updown-5m-1234567890")
        # Try common patterns
        slug_key = ""
        for suffix_len in [10, 13]:  # unix timestamp lengths
            if len(slug) > suffix_len:
                candidate = slug[:-suffix_len].rstrip("-")
                sessions_dir = os.path.join(BASE_DIR, "data", "markets", candidate, "sessions")
                if os.path.isdir(sessions_dir):
                    slug_key = candidate
                    break

        if not slug_key:
            # Fallback: try to find from condition_id or market structure
            return []

        sessions_dir = os.path.join(BASE_DIR, "data", "markets", slug_key, "sessions")
        if not os.path.isdir(sessions_dir):
            return []

        pattern = os.path.join(sessions_dir, "**", "*.json")
        files = sorted(glob.glob(pattern, recursive=True))

        # Only load the most recent N+1 files (we'll exclude current session)
        files = files[-(self.lookback_sessions + 5):]

        current_window = metadata.get("window_start_utc", "")
        summaries = []
        for f in files:
            # Use cache for previously loaded files (session data is immutable)
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

        return summaries[-self.lookback_sessions:]

    def _compute_historical_priors(self, summaries):
        # type: (List[Dict[str, Any]]) -> None
        """Compute priors from historical session data."""
        if not summaries:
            self._effective_jump_bps = self.jump_bps
            self._prior_up_jumps = 0.0
            self._prior_down_jumps = 0.0
            self._trend_bias = 0.0
            return

        n = len(summaries)

        # --- Trend bias: exponentially-weighted win rate ---
        # More recent sessions weighted higher
        up_count = 0.0
        total_weight = 0.0
        for i, s in enumerate(summaries):
            # Exponential decay: most recent gets weight 1.0
            weight = math.exp(-0.03 * (n - 1 - i))
            total_weight += weight
            if s["outcome"] == "Up":
                up_count += weight

        win_rate = up_count / total_weight if total_weight > 0 else 0.5
        self._trend_bias = (win_rate - 0.5) * self.trend_weight * 2.0

        # --- Volatility regime: std dev of session returns (bps) ---
        returns_bps = []
        for s in summaries:
            initial = s["initial_btc"]
            final = s["final_btc"]
            if initial > 0:
                ret = (final / initial - 1.0) * 10000.0
                returns_bps.append(ret)

        if len(returns_bps) >= 3:
            mean_ret = sum(returns_bps) / len(returns_bps)
            variance = sum((r - mean_ret) ** 2 for r in returns_bps) / len(returns_bps)
            vol_bps = math.sqrt(variance)

            # Adapt jump_bps: high vol → lower threshold (more jumps),
            # low vol → higher threshold (fewer, more meaningful jumps)
            # Baseline: assume typical session vol is ~5 bps
            typical_vol = 5.0
            if vol_bps > 0:
                vol_ratio = typical_vol / vol_bps
                # Blend between fixed jump_bps and adapted value
                adapted = self.jump_bps * vol_ratio
                # Clamp to reasonable range (0.5x to 2x base)
                adapted = max(self.jump_bps * 0.5, min(adapted, self.jump_bps * 2.0))
                self._effective_jump_bps = (
                    self.jump_bps * (1.0 - self.vol_adapt) +
                    adapted * self.vol_adapt
                )
            else:
                self._effective_jump_bps = self.jump_bps
        else:
            self._effective_jump_bps = self.jump_bps

        # --- Prior jump counts: from average absolute return per session ---
        # Estimate how many jumps of size effective_jump_bps typically occur
        abs_returns = [abs(r) for r in returns_bps] if returns_bps else []
        if abs_returns and self._effective_jump_bps > 0:
            mean_abs_ret = sum(abs_returns) / len(abs_returns)
            # Rough estimate: total jumps ~ abs_return / jump_size * some_factor
            # Use prior_strength to scale
            estimated_total = mean_abs_ret / self._effective_jump_bps
            # Split into up/down based on trend
            up_frac = win_rate
            self._prior_up_jumps = self.prior_strength * estimated_total * up_frac
            self._prior_down_jumps = self.prior_strength * estimated_total * (1.0 - up_frac)
        else:
            # Symmetric weak prior
            self._prior_up_jumps = self.prior_strength * 0.5
            self._prior_down_jumps = self.prior_strength * 0.5

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        summaries = self._load_prior_summaries(metadata)
        self._compute_historical_priors(summaries)

        self._prev_btc = 0.0
        self._up_jumps = 0
        self._down_jumps = 0
        self._entry_side = None
        self._entry_midpoint = 0.0
        self._exited = False
        self._exit_sell_attempted = False

        self.log("hist: {} sessions | trend_bias={:.3f} jump_bps={:.3f} "
                 "prior_up={:.1f} prior_dn={:.1f}".format(
                     len(summaries), self._trend_bias,
                     self._effective_jump_bps,
                     self._prior_up_jumps, self._prior_down_jumps))

    def _held_side(self, ctx):
        # type: (TickContext) -> Optional[str]
        if ctx.position.up_tokens > 0:
            return "up"
        if ctx.position.down_tokens > 0:
            return "down"
        return None

    def _compute_p_up(self, ctx):
        # type: (TickContext) -> Optional[float]
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

        # Total jumps = session observed + historical prior
        total_up = self._up_jumps + self._prior_up_jumps
        total_down = self._down_jumps + self._prior_down_jumps
        total_jumps = total_up + total_down

        if total_jumps < self.min_jumps:
            return None

        # Effective elapsed includes prior (treat prior as coming from
        # prior_strength / (prior_strength + 1) of a "virtual" session)
        prior_total = self._prior_up_jumps + self._prior_down_jumps
        session_jumps = self._up_jumps + self._down_jumps
        if session_jumps > 0:
            # Weight: as session progresses, session data dominates
            effective_elapsed = elapsed
            lambda_up = total_up / (elapsed + prior_total / max(session_jumps / elapsed, 1.0))
            lambda_down = total_down / (elapsed + prior_total / max(session_jumps / elapsed, 1.0))
        else:
            # Pure prior — estimate rate from prior counts over a full session
            lambda_up = self._prior_up_jumps
            lambda_down = self._prior_down_jumps

        mu_up = lambda_up * remaining
        mu_down = lambda_down * remaining

        displacement_bps = (btc / strike - 1.0) * 10000.0
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
        p = _norm_cdf(z)

        # Apply trend bias
        p = max(0.01, min(0.99, p + self._trend_bias))

        return p

    def _kelly_fraction(self, p, market_price):
        # type: (float, float) -> float
        if market_price >= 0.99 or market_price <= 0.01:
            return 0.0
        f = (p - market_price) / (1.0 - market_price)
        return max(0.0, min(f, self.max_kelly))

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        btc = ctx.sample.get("btc_price_usd")
        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        if btc is None or up_mid is None or down_mid is None:
            return HOLD

        # --- Count jumps using adapted threshold ---
        if self._prev_btc > 0:
            ret_bps = (btc / self._prev_btc - 1.0) * 10000.0
            if ret_bps >= self._effective_jump_bps:
                self._up_jumps += 1
            elif ret_bps <= -self._effective_jump_bps:
                self._down_jumps += 1
        self._prev_btc = btc

        # --- Manage existing position: stop-loss ---
        held = self._held_side(ctx)
        if held is not None:
            if self._exited:
                if not self._exit_sell_attempted:
                    self._exit_sell_attempted = True
                    current_mid = up_mid if held == "up" else down_mid
                    sell_side = "sell_up" if held == "up" else "sell_down"
                    tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                    if tokens > 0 and current_mid is not None and current_mid > 0:
                        return TradeAction(side=sell_side, amount=tokens, price=current_mid)
                return HOLD
            current_mid = up_mid if held == "up" else down_mid
            if self._entry_midpoint > 0 and current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                self.log("stop-loss: mid={:.3f} down {:.1f}% from entry {:.3f}".format(
                    current_mid,
                    (1.0 - current_mid / self._entry_midpoint) * 100,
                    self._entry_midpoint,
                ))
                sell_side = "sell_up" if held == "up" else "sell_down"
                tokens = ctx.position.up_tokens if held == "up" else ctx.position.down_tokens
                self._exited = True
                return TradeAction(side=sell_side, amount=tokens, price=current_mid)
            return HOLD

        # Already entered or exited — done
        if self._exited or self._entry_side is not None:
            return HOLD

        # Timing guards
        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD
        if ctx.elapsed_fraction >= self.no_new_after:
            return HOLD

        # --- Compute model probability ---
        p_up = self._compute_p_up(ctx)
        if p_up is None:
            return HOLD

        p_down = 1.0 - p_up

        kelly_up = self._kelly_fraction(p_up, up_mid)
        kelly_down = self._kelly_fraction(p_down, down_mid)
        edge_up = p_up - up_mid
        edge_down = p_down - down_mid

        if kelly_up >= kelly_down and kelly_up >= self.min_kelly and edge_up >= self.edge_min:
            side = "buy_up"
            kelly = kelly_up
            midpoint = up_mid
            edge = edge_up
            p = p_up
        elif kelly_down >= self.min_kelly and edge_down >= self.edge_min:
            side = "buy_down"
            kelly = kelly_down
            midpoint = down_mid
            edge = edge_down
            p = p_down
        else:
            return HOLD

        self._entry_side = "up" if side == "buy_up" else "down"
        self._entry_midpoint = midpoint

        dollars = ctx.max_buy_amount * kelly
        # Ensure at least minimum order size (5 tokens * price), capped at available cash
        min_dollars = CLOB_MIN_ORDER_TOKENS * midpoint
        if dollars < min_dollars:
            dollars = min(min_dollars, ctx.max_buy_amount)

        self.log("ENTRY {}: p={:.3f} mid={:.3f} edge={:.3f} kelly={:.3f} ${:.2f} | "
                 "jumps={}/{} prior={:.0f}/{:.0f} trend={:.3f} at {:.0f}%".format(
                     side, p, midpoint, edge, kelly, dollars,
                     self._up_jumps, self._down_jumps,
                     self._prior_up_jumps, self._prior_down_jumps,
                     self._trend_bias, ctx.elapsed_fraction * 100))

        return TradeAction(side=side, amount=dollars, price=midpoint)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 3 * 4 * 3 * 3 * 3 * 3 = 972 combos
        return {
            "lookback_sessions": [20, 50, 100],
            "jump_bps": [0.10, 0.15, 0.20, 0.30],
            "vol_adapt": [0.0, 0.3, 0.6],
            "trend_weight": [0.0, 0.05, 0.15],
            "min_elapsed": [0.15, 0.20, 0.30],
            "edge_min": [0.05, 0.08, 0.12],
        }
