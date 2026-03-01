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

from core.defaults import DATA_DIR
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
        bet_fraction: fraction of cash to bet
        data_dir: session data directory (auto-detected if None)
    """

    name = "VolatilityBargain"
    description = (
        "Buys cheap tokens when high recent BTC volatility makes them "
        "underpriced relative to remaining session time."
    )

    def __init__(
        self,
        lookback_sessions=10,
        min_sessions=5,
        threshold=150.0,
        max_token_price=0.30,
        min_elapsed=0.05,
        bet_fraction=1.0,
        data_dir=None,
    ):
        self.lookback_sessions = lookback_sessions
        self.min_sessions = min_sessions
        self.threshold = threshold
        self.max_token_price = max_token_price
        self.min_elapsed = min_elapsed
        self.bet_fraction = bet_fraction
        self.data_dir = data_dir
        self._params = {
            "lookback_sessions": lookback_sessions,
            "min_sessions": min_sessions,
            "threshold": threshold,
            "max_token_price": max_token_price,
            "min_elapsed": min_elapsed,
            "bet_fraction": bet_fraction,
        }

        # Per-session state
        self._volatility_bps = None  # type: Optional[float]
        self._bought = False

    def _resolve_data_dir(self, metadata):
        # type: (Dict[str, Any]) -> str
        if self.data_dir is not None:
            return self.data_dir
        event_slug = metadata.get("event_slug", "")
        slug_key = event_slug.rsplit("-", 1)[0] if event_slug else ""
        return os.path.join(DATA_DIR, slug_key)

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._volatility_bps = None
        self._bought = False

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

    def on_tick(self, ctx):
        # type: (Any) -> TradeAction
        if self._volatility_bps is None or self._bought:
            return HOLD

        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

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

        # Pick the higher-scoring token (cheaper + better deal)
        if up_score > self.threshold and up_score >= down_score:
            self._bought = True
            self.log("BUY UP: score={:.1f} mid={:.3f} vol={:.2f} elapsed={:.2f}".format(
                up_score, up_mid, self._volatility_bps, ctx.elapsed_fraction))
            return TradeAction(
                side="buy_up",
                amount=ctx.position.cash * self.bet_fraction,
                price=up_mid,
            )

        if down_score > self.threshold:
            self._bought = True
            self.log("BUY DOWN: score={:.1f} mid={:.3f} vol={:.2f} elapsed={:.2f}".format(
                down_score, down_mid, self._volatility_bps, ctx.elapsed_fraction))
            return TradeAction(
                side="buy_down",
                amount=ctx.position.cash * self.bet_fraction,
                price=down_mid,
            )

        return HOLD

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 7 * 5 * 4 * 3 = 420 combos (final polish)
        return {
            "threshold": [70.0, 85.0, 100.0, 120.0, 150.0, 180.0, 200.0],
            "max_token_price": [0.22, 0.26, 0.30, 0.34, 0.38],
            "min_elapsed": [0.03, 0.05, 0.08, 0.10],
            "lookback_sessions": [5, 8, 10],
        }
