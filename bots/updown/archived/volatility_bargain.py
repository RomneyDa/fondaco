"""Buy cheap tokens when high recent volatility makes them underpriced.

Calculates BTC volatility from the last N sessions, then continuously
evaluates a deal score for each token:

    score = volatility / (token_midpoint * elapsed_fraction)

Higher volatility, cheaper token price, and earlier in the session all
increase the score. If the score exceeds a threshold, buys with full
session cash. Never sells — holds to resolution for the $1 payout.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.defaults import BASE_DIR
from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


def _parse_utc(s):
    # type: (str) -> datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Module-level cache for live-trading session file reads
_session_summary_cache = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]


def _load_session_summaries(data_dir):
    # type: (str) -> Dict[str, Dict[str, Any]]
    if data_dir not in _session_summary_cache:
        _session_summary_cache[data_dir] = {}

    cache = _session_summary_cache[data_dir]
    if not os.path.isdir(data_dir):
        return cache

    for fname in os.listdir(data_dir):
        if not fname.endswith(".json") or fname in cache:
            continue
        stem = fname[:-5]
        try:
            date_part, time_part = stem.split("T", 1)
            time_part = time_part.replace("-", ":")
            ts = _parse_utc(date_part + "T" + time_part)
        except (ValueError, IndexError):
            continue

        filepath = os.path.join(data_dir, fname)
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            if data.get("outcome") is None:
                continue
            cache[fname] = {
                "window_start": ts,
                "initial_btc": data.get("initial_btc_price_usd"),
                "final_btc": data.get("final_btc_price_usd"),
                "outcome": data["outcome"],
            }
        except (json.JSONDecodeError, OSError):
            continue

    return cache


class VolatilityBargain(Strategy):
    """Buy cheap tokens when recent BTC volatility makes them underpriced.

    Computes BTC session-return volatility from the most recent N sessions.
    On each tick, scores each token:

        score = volatility_bps / (token_midpoint * elapsed_fraction)

    If score > threshold, buys that token with full session cash.
    Never sells — holds to resolution.

    Params:
        lookback_sessions: how many recent sessions to use for volatility
        min_sessions: minimum resolved sessions required to activate
        threshold: deal score above which to buy
        max_token_price: don't buy tokens above this midpoint
        min_elapsed: earliest elapsed fraction to start evaluating
        data_dir: session data directory (auto-detected if None)
    """

    name = "VolatilityBargain"
    description = (
        "Buys cheap tokens when high recent BTC volatility makes them "
        "underpriced relative to remaining session time."
    )

    def __init__(
        self,
        lookback_sessions=3,
        min_sessions=3,
        threshold=5.0,
        max_token_price=0.40,
        min_elapsed=0.03,
        slope_lookback=4.5,
        slope_threshold=-0.0001,
        data_dir=None,
    ):
        self.lookback_sessions = lookback_sessions
        self.min_sessions = min_sessions
        self.threshold = threshold
        self.max_token_price = max_token_price
        self.min_elapsed = min_elapsed
        self.slope_lookback = slope_lookback  # seconds
        self.slope_threshold = slope_threshold
        self.data_dir = data_dir
        self._params = {
            "lookback_sessions": lookback_sessions,
            "min_sessions": min_sessions,
            "threshold": threshold,
            "max_token_price": max_token_price,
            "min_elapsed": min_elapsed,
            "slope_lookback": slope_lookback,
            "slope_threshold": slope_threshold,
        }

        # Per-session state
        self._volatility_bps = None  # type: Optional[float]
        self._bought = False
        self._up_midpoints = []    # type: List[float]
        self._down_midpoints = []  # type: List[float]
        self._window_seconds = 300  # type: int

    def _resolve_data_dir(self, metadata):
        # type: (Dict[str, Any]) -> str
        if self.data_dir is not None:
            return self.data_dir
        event_slug = metadata.get("event_slug", "")
        slug_key = event_slug.rsplit("-", 1)[0] if event_slug else ""
        return os.path.join(BASE_DIR, "data", slug_key)

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._volatility_bps = None
        self._bought = False
        self._up_midpoints = []
        self._down_midpoints = []
        self._window_seconds = metadata.get("window", 300)

        # Get prior session summaries
        prior_summaries = metadata.get("_prior_session_summaries")
        if prior_summaries is not None:
            all_summaries = prior_summaries
        else:
            data_dir = self._resolve_data_dir(metadata)
            all_summaries = list(_load_session_summaries(data_dir).values())

        # Sort by time, take the most recent N
        all_summaries = sorted(all_summaries, key=lambda x: x["window_start"])

        # Filter to sessions with valid BTC prices
        valid = [s for s in all_summaries
                 if s.get("initial_btc") and s.get("final_btc")
                 and s["initial_btc"] > 0]

        # Take the last lookback_sessions
        recent = valid[-self.lookback_sessions:]

        if len(recent) < self.min_sessions:
            self.log("Only {} sessions available (need {})".format(
                len(recent), self.min_sessions))
            return

        # Compute volatility: std dev of session BTC returns in bps
        returns_bps = []
        for s in recent:
            ret = (s["final_btc"] - s["initial_btc"]) / s["initial_btc"] * 10000.0
            returns_bps.append(ret)

        mean_ret = sum(returns_bps) / len(returns_bps)
        variance = sum((r - mean_ret) ** 2 for r in returns_bps) / len(returns_bps)
        self._volatility_bps = variance ** 0.5

        if self._volatility_bps <= 0:
            self._volatility_bps = None
            self.log("Zero volatility, skipping")
            return

        self.log("Volatility: {:.2f} bps from {} sessions".format(
            self._volatility_bps, len(recent)))

    @staticmethod
    def _compute_slope(values, lookback):
        # type: (List[float], int) -> Optional[float]
        """Linear regression slope of the last `lookback` values."""
        if len(values) < lookback:
            return None
        recent = values[-lookback:]
        n = len(recent)
        # Closed-form OLS slope: sum_xy / sum_x2 (mean-centered)
        sum_x = n * (n - 1) / 2.0
        sum_x2 = n * (n - 1) * (2 * n - 1) / 6.0
        sum_y = sum(recent)
        sum_xy = sum(i * y for i, y in enumerate(recent))
        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0
        return (n * sum_xy - sum_x * sum_y) / denom

    def on_tick(self, ctx):
        # type: (Any) -> TradeAction
        if self._volatility_bps is None or self._bought:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        # Track midpoint history for slope calculation
        if up_mid is not None:
            self._up_midpoints.append(up_mid)
        if down_mid is not None:
            self._down_midpoints.append(down_mid)

        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        if up_mid is None or down_mid is None:
            return HOLD

        elapsed = max(ctx.elapsed_fraction, 0.01)

        # Score each token
        up_score = 0.0
        down_score = 0.0

        if 0 < up_mid <= self.max_token_price:
            up_score = self._volatility_bps / (up_mid * elapsed)

        if 0 < down_mid <= self.max_token_price:
            down_score = self._volatility_bps / (down_mid * elapsed)

        # Convert slope_lookback from seconds to ticks using observed tick rate
        elapsed_s = ctx.elapsed_fraction * self._window_seconds
        if elapsed_s > 0 and ctx.tick_index > 0:
            tick_rate = ctx.tick_index / elapsed_s
        else:
            tick_rate = 18.0  # fallback
        lookback_ticks = max(int(self.slope_lookback * tick_rate), 2)

        # Pick the higher-scoring token (cheaper + better deal)
        if up_score > self.threshold and up_score >= down_score:
            slope = self._compute_slope(self._up_midpoints, lookback_ticks)
            if slope is not None and slope < self.slope_threshold:
                return HOLD  # midpoint still declining, wait for better price
            self._bought = True
            self.log("BUY UP: score={:.1f} mid={:.3f} vol={:.2f} elapsed={:.2f} slope={:.6f}".format(
                up_score, up_mid, self._volatility_bps, ctx.elapsed_fraction,
                slope if slope is not None else 0.0))
            return TradeAction(
                side="buy_up",
                amount=ctx.position.cash,
                price=up_mid,
            )

        if down_score > self.threshold:
            slope = self._compute_slope(self._down_midpoints, lookback_ticks)
            if slope is not None and slope < self.slope_threshold:
                return HOLD  # midpoint still declining, wait for better price
            self._bought = True
            self.log("BUY DOWN: score={:.1f} mid={:.3f} vol={:.2f} elapsed={:.2f} slope={:.6f}".format(
                down_score, down_mid, self._volatility_bps, ctx.elapsed_fraction,
                slope if slope is not None else 0.0))
            return TradeAction(
                side="buy_down",
                amount=ctx.position.cash,
                price=down_mid,
            )

        return HOLD

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # Centered on optimums: 3 * 5 * 5 * 3 * 2 = 450 combos
        return {
            "lookback_sessions": [3, 5, 7],
            "threshold": [3.0, 5.0, 10.0, 20.0, 40.0],
            "max_token_price": [0.30, 0.35, 0.40, 0.45, 0.50],
            "slope_lookback": [3.0, 4.5, 6.0],
            "slope_threshold": [-0.0001, 0.0],
        }
