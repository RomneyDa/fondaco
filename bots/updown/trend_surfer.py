"""Buy in the direction of the cross-session BTC trend, sizing dynamically.

Builds on archived BuyTrend by scaling bet size with three factors:
  1. BTC volatility — larger recent session moves → stronger signal → bet more
  2. Token price — cheaper tokens have better payoff asymmetry → bet more
  3. Trend strength — higher direction consistency + bigger BTC change → bet more

Waits until min_elapsed for better price info (vs BuyTrend's tick-0 entry).
Manages position with stop-loss. Holds to resolution otherwise.

v1: Dynamic sizing via volatility × price × trend multipliers.
2-phase Sharpe→Kelly over recent 30 sessions.
Sharpe 0.218, 6.7% win rate, 4.5% geo return/session.
Key findings: low max_price (0.55) essential — only buy cheap tokens.
Steep price discount (power=2.0) and tight stop-loss (8%) help.
Trend strength scale barely matters (0.3 vs 1.5 → ~0.004 Sharpe diff).
Half-Kelly f*=1.0 → cash_pct=50%.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.defaults import BASE_DIR
from core.trader import HOLD, TradeAction, TickContext
from core.strategies.base import Strategy


def _parse_utc(s):
    # type: (str) -> datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


class TrendSurfer(Strategy):
    """Buy the cross-session BTC trend with dynamic position sizing.

    Analyzes recent sessions for a directional BTC trend. If confirmed,
    buys the trend-direction token with size scaled by:
      - BTC volatility (mean abs return across sessions, normalized)
      - Token price (cheaper = bigger bet, via concave price discount)
      - Trend strength (direction consistency × BTC change magnitude)

    Params:
        min_sessions: minimum resolved sessions in lookback to act
        lookback_minutes: how far back to look for sessions
        min_change_bps: minimum overall BTC change (bps) to trigger
        min_direction_pct: minimum fraction of sessions matching trend
        min_elapsed: wait until this elapsed fraction before buying
        max_price: don't buy tokens above this midpoint
        base_bet_fraction: starting fraction of cash (before scaling)
        vol_scale: how much volatility amplifies the bet (0=ignore vol)
        vol_baseline_bps: "normal" session abs return in bps; above → scale up
        price_discount_power: exponent for price discount (higher = steeper)
        trend_strength_scale: how much trend consistency amplifies bet
        stop_loss_pct: sell if midpoint drops this fraction below entry
        data_dir: path to session data directory (auto-detected if None)
    """

    name = "TrendSurfer"
    description = (
        "Buys cross-session BTC trend direction with dynamic sizing: "
        "scales bet with volatility, token price, and trend strength."
    )
    version = "1"
    requires_full_session = False

    def __init__(
        self,
        min_sessions=6,
        lookback_minutes=30,
        min_change_bps=5.0,
        min_direction_pct=0.55,
        min_elapsed=0.10,
        max_price=0.55,
        base_bet_fraction=0.25,
        vol_scale=1.0,
        vol_baseline_bps=5.0,
        price_discount_power=2.0,
        trend_strength_scale=0.3,
        stop_loss_pct=0.08,
        data_dir=None,
    ):
        self.min_sessions = min_sessions
        self.lookback_minutes = lookback_minutes
        self.min_change_bps = min_change_bps
        self.min_direction_pct = min_direction_pct
        self.min_elapsed = min_elapsed
        self.max_price = max_price
        self.base_bet_fraction = base_bet_fraction
        self.vol_scale = vol_scale
        self.vol_baseline_bps = vol_baseline_bps
        self.price_discount_power = price_discount_power
        self.trend_strength_scale = trend_strength_scale
        self.stop_loss_pct = stop_loss_pct
        self.data_dir = data_dir
        self._params = {
            "min_sessions": min_sessions,
            "lookback_minutes": lookback_minutes,
            "min_change_bps": min_change_bps,
            "min_direction_pct": min_direction_pct,
            "min_elapsed": min_elapsed,
            "max_price": max_price,
            "base_bet_fraction": base_bet_fraction,
            "vol_scale": vol_scale,
            "vol_baseline_bps": vol_baseline_bps,
            "price_discount_power": price_discount_power,
            "trend_strength_scale": trend_strength_scale,
            "stop_loss_pct": stop_loss_pct,
        }

        # Per-session state
        self._should_buy = None  # type: Optional[str]
        self._bet_multiplier = 1.0
        self._bought = False
        self._entry_midpoint = 0.0

    def _resolve_data_dir(self, metadata):
        # type: (Dict[str, Any]) -> str
        if self.data_dir is not None:
            return self.data_dir
        event_slug = metadata.get("event_slug", "")
        slug_key = event_slug.rsplit("-", 1)[0] if event_slug else ""
        return os.path.join(BASE_DIR, "data", "markets", slug_key, "sessions")

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._should_buy = None
        self._bet_multiplier = 1.0
        self._bought = False
        self._entry_midpoint = 0.0

        # Parse current session window start
        window_start_str = metadata.get("window_start_utc", "")
        try:
            current_start = _parse_utc(window_start_str)
        except (ValueError, TypeError):
            self.log("Cannot parse window_start_utc: {}".format(window_start_str))
            return

        cutoff = current_start - timedelta(minutes=self.lookback_minutes)

        # Load prior session data
        prior_summaries = metadata.get("_prior_session_summaries")
        if prior_summaries is not None:
            all_summaries_list = prior_summaries
        else:
            data_dir = self._resolve_data_dir(metadata)
            all_summaries_list = list(_load_session_summaries(data_dir).values())

        # Filter to lookback window
        recent = []
        for summary in all_summaries_list:
            ts = summary["window_start"]
            if cutoff <= ts < current_start:
                if summary["initial_btc"] and summary["final_btc"]:
                    recent.append(summary)

        recent.sort(key=lambda x: x["window_start"])

        if len(recent) < self.min_sessions:
            self.log("Only {} sessions in lookback (need {})".format(
                len(recent), self.min_sessions))
            return

        # Require contiguous sessions (max 7 min gaps)
        max_gap = timedelta(minutes=7)
        for i in range(1, len(recent)):
            gap = recent[i]["window_start"] - recent[i - 1]["window_start"]
            if gap > max_gap:
                self.log("Gap: {} -> {} ({:.0f}min), skipping".format(
                    recent[i - 1]["window_start"].strftime("%H:%M"),
                    recent[i]["window_start"].strftime("%H:%M"),
                    gap.total_seconds() / 60))
                return

        # Most recent session must be close to current
        last_to_current = current_start - recent[-1]["window_start"]
        if last_to_current > max_gap:
            self.log("Last session {}min before current, skipping".format(
                int(last_to_current.total_seconds() / 60)))
            return

        # Overall BTC price change
        first_price = recent[0]["initial_btc"]
        last_price = recent[-1]["final_btc"]
        if first_price <= 0:
            return

        change_bps = ((last_price - first_price) / first_price) * 10000.0

        if abs(change_bps) < self.min_change_bps:
            self.log("BTC change {:.1f}bps < threshold {:.1f}bps".format(
                change_bps, self.min_change_bps))
            return

        # Direction consistency
        trend_up = change_bps > 0
        matching = sum(1 for s in recent if (s["outcome"] == "Up") == trend_up)
        direction_pct = matching / len(recent)

        if direction_pct < self.min_direction_pct:
            self.log("Direction consistency {:.0f}% < threshold {:.0f}%".format(
                direction_pct * 100, self.min_direction_pct * 100))
            return

        # --- Compute dynamic bet multiplier ---

        # Factor 1: Volatility — mean absolute session return
        abs_returns = []
        for s in recent:
            if s["initial_btc"] > 0:
                ret = abs(s["final_btc"] - s["initial_btc"]) / s["initial_btc"] * 10000.0
                abs_returns.append(ret)

        if abs_returns:
            mean_abs_return = sum(abs_returns) / len(abs_returns)
        else:
            mean_abs_return = self.vol_baseline_bps

        # vol_mult: ratio of actual vol to baseline, clamped
        # More BTC movement → higher multiplier → bigger bet
        vol_ratio = mean_abs_return / max(self.vol_baseline_bps, 0.1)
        vol_mult = 1.0 + self.vol_scale * (vol_ratio - 1.0)
        vol_mult = max(0.3, min(vol_mult, 3.0))

        # Factor 2: Trend strength — combines direction consistency and magnitude
        # direction_pct ranges from min_direction_pct to 1.0
        # change_bps ranges from min_change_bps upward
        # Normalize both and combine
        consistency_score = (direction_pct - self.min_direction_pct) / max(1.0 - self.min_direction_pct, 0.01)
        magnitude_score = min(abs(change_bps) / (self.min_change_bps * 5), 1.0)
        trend_strength = (consistency_score + magnitude_score) / 2.0
        trend_mult = 1.0 + self.trend_strength_scale * trend_strength
        trend_mult = max(0.5, min(trend_mult, 2.0))

        self._bet_multiplier = vol_mult * trend_mult
        self._should_buy = "buy_up" if trend_up else "buy_down"

        self.log("Trend: BTC {}{:.1f}bps, {:.0f}% confirm, vol={:.1f}bps "
                 "→ vol_m={:.2f} trend_m={:.2f} bet_m={:.2f} -> {}".format(
                     "+" if change_bps > 0 else "", change_bps,
                     direction_pct * 100, mean_abs_return,
                     vol_mult, trend_mult, self._bet_multiplier,
                     self._should_buy))

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction

        up_mid = ctx.sample.get("up_token_midpoint")
        down_mid = ctx.sample.get("down_token_midpoint")

        # --- Manage existing position ---
        if self._bought and self._entry_midpoint > 0:
            if ctx.position.up_tokens > 0:
                held_side, sell_side = "up", "sell_up"
                current_mid = up_mid
                tokens = ctx.position.up_tokens
            elif ctx.position.down_tokens > 0:
                held_side, sell_side = "down", "sell_down"
                current_mid = down_mid
                tokens = ctx.position.down_tokens
            else:
                return HOLD

            if current_mid and tokens > 0 and self.stop_loss_pct is not None:
                if current_mid <= self._entry_midpoint * (1.0 - self.stop_loss_pct):
                    self.log("stop-loss: mid={:.3f} entry={:.3f} drop={:.1f}%".format(
                        current_mid, self._entry_midpoint,
                        (1.0 - current_mid / self._entry_midpoint) * 100))
                    self._entry_midpoint = 0.0
                    return TradeAction(side=sell_side, amount=tokens, price=current_mid)

            return HOLD

        # --- Entry logic ---
        if self._should_buy is None or self._bought:
            return HOLD

        if ctx.elapsed_fraction < self.min_elapsed:
            return HOLD

        if self._should_buy == "buy_up":
            price = up_mid
        else:
            price = down_mid

        if price is None or price <= 0:
            return HOLD

        # Max price filter
        if price > self.max_price:
            self.log("Token price {:.3f} > max {:.3f}, skipping".format(
                price, self.max_price))
            self._should_buy = None
            return HOLD

        # Factor 3: Price discount — cheaper tokens get bigger bets
        # At price 0.50 → discount ~1.0; at price 0.30 → discount ~1.5+
        # Uses (1-price)^power / (1-0.5)^power to normalize around 0.50
        price_discount = ((1.0 - price) / 0.5) ** self.price_discount_power
        price_discount = max(0.3, min(price_discount, 3.0))

        # Final bet fraction
        final_fraction = self.base_bet_fraction * self._bet_multiplier * price_discount
        final_fraction = min(final_fraction, 1.0)  # never bet more than 100%

        dollars = ctx.position.cash * final_fraction

        self._bought = True
        self._entry_midpoint = price

        self.log("BUY: {} at {:.3f}, frac={:.2f} (base={:.2f} × bet_m={:.2f} × price_d={:.2f}) ${:.2f}".format(
            self._should_buy, price, final_fraction,
            self.base_bet_fraction, self._bet_multiplier, price_discount, dollars))

        return TradeAction(side=self._should_buy, amount=dollars, price=price)

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 2 × 2 × 3 × 3 × 3 × 3 × 3 = 972 combos
        return {
            "min_sessions": [3, 5],
            "lookback_minutes": [30, 60],
            "min_change_bps": [3.0, 10.0, 20.0],
            "min_direction_pct": [0.50, 0.65, 0.80],
            "max_price": [0.60, 0.70, 0.85],
            "base_bet_fraction": [0.3, 0.5, 0.75],
            "vol_scale": [0.5, 1.0, 1.5],
        }
