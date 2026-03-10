"""Buy in the direction of recent BTC price trend across prior sessions."""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.defaults import BASE_DIR
from core.trader import HOLD, TradeAction
from core.strategies.base import Strategy


def _parse_utc(s):
    # type: (str) -> datetime
    """Parse an ISO timestamp, handling the 'Z' suffix that Python 3.10 rejects."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Module-level incremental cache: data_dir -> {filename -> summary_dict}
# Survives across strategy instances within a single process run, so grid
# search over 700+ param combos doesn't re-read every session file.
_session_summary_cache = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]


def _load_session_summaries(data_dir):
    # type: (str) -> Dict[str, Dict[str, Any]]
    """Load session summaries incrementally from a directory.

    Only reads files not yet in the cache.  Unresolved sessions are skipped
    (and retried on subsequent calls, so live collection picks them up once
    they resolve).
    """
    if data_dir not in _session_summary_cache:
        _session_summary_cache[data_dir] = {}

    cache = _session_summary_cache[data_dir]
    if not os.path.isdir(data_dir):
        return cache

    for fname in os.listdir(data_dir):
        if not fname.endswith(".json") or fname in cache:
            continue

        # Parse timestamp from filename (colons replaced with dashes)
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
                continue  # unresolved — will retry next call
            cache[fname] = {
                "window_start": ts,
                "initial_btc": data.get("initial_btc_price_usd"),
                "final_btc": data.get("final_btc_price_usd"),
                "outcome": data["outcome"],
            }
        except (json.JSONDecodeError, OSError):
            continue

    return cache


class BuyTrend(Strategy):
    """Check recent session history for BTC price trend and buy in that direction.

    Params:
        min_sessions: minimum resolved sessions in lookback window to act
        lookback_minutes: how far back to look for sessions
        min_change_bps: minimum overall BTC price change (basis points) to trigger
        min_direction_pct: minimum fraction of sessions matching trend direction
        bet_fraction: fraction of cash to bet
        data_dir: path to session data directory (auto-detected if None)
    """

    name = "BuyTrend"
    description = (
        "Checks recent session history for a BTC price trend. "
        "If enough sessions confirm a directional move exceeding a threshold, "
        "buys in that direction at the start of the session."
    )
    version = "1"

    def __init__(self, min_sessions=5, lookback_minutes=60,
                 min_change_bps=5.0, min_direction_pct=0.75,
                 bet_fraction=1.0, data_dir=None):
        self.min_sessions = min_sessions
        self.lookback_minutes = lookback_minutes
        self.min_change_bps = min_change_bps
        self.min_direction_pct = min_direction_pct
        self.bet_fraction = bet_fraction
        self.data_dir = data_dir
        self._should_buy = None  # type: Optional[str]
        self._bought = False
        self._params = {
            "min_sessions": min_sessions,
            "lookback_minutes": lookback_minutes,
            "min_change_bps": min_change_bps,
            "min_direction_pct": min_direction_pct,
            "bet_fraction": bet_fraction,
        }

    def _resolve_data_dir(self, metadata):
        # type: (Dict[str, Any]) -> str
        """Determine session data directory from metadata or constructor arg."""
        if self.data_dir is not None:
            return self.data_dir

        # Infer from event_slug: "btc-updown-5m-1771585200" -> "btc-updown-5m"
        event_slug = metadata.get("event_slug", "")
        slug_key = event_slug.rsplit("-", 1)[0] if event_slug else ""
        return os.path.join(BASE_DIR, "data", slug_key)

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        self._should_buy = None
        self._bought = False

        # Parse current session's window start
        window_start_str = metadata.get("window_start_utc", "")
        try:
            current_start = _parse_utc(window_start_str)
        except (ValueError, TypeError):
            self.log("Cannot parse window_start_utc: {}".format(window_start_str))
            return

        cutoff = current_start - timedelta(minutes=self.lookback_minutes)

        # Use injected summaries from backtest engine if available,
        # otherwise fall back to reading session files from disk (live trading).
        prior_summaries = metadata.get("_prior_session_summaries")
        if prior_summaries is not None:
            all_summaries_list = prior_summaries
        else:
            data_dir = self._resolve_data_dir(metadata)
            all_summaries_list = list(_load_session_summaries(data_dir).values())

        # Filter to sessions in the lookback window, strictly before current
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

        # Require contiguous sessions — no gaps allowed.
        # Sessions run every 5 minutes; allow up to 7 min between consecutive
        # sessions to tolerate minor timing drift.
        max_gap = timedelta(minutes=7)
        for i in range(1, len(recent)):
            gap = recent[i]["window_start"] - recent[i - 1]["window_start"]
            if gap > max_gap:
                self.log("Gap in session data: {} -> {} ({:.0f}min gap), holding off".format(
                    recent[i - 1]["window_start"].strftime("%H:%M"),
                    recent[i]["window_start"].strftime("%H:%M"),
                    gap.total_seconds() / 60))
                return

        # Also require the most recent session is close to current (no gap right before)
        last_to_current = current_start - recent[-1]["window_start"]
        if last_to_current > max_gap:
            self.log("Most recent session {}min before current, holding off".format(
                int(last_to_current.total_seconds() / 60)))
            return

        # Overall BTC price change: earliest initial -> latest final
        first_price = recent[0]["initial_btc"]
        last_price = recent[-1]["final_btc"]
        if first_price <= 0:
            return

        change_bps = ((last_price - first_price) / first_price) * 10000.0

        if abs(change_bps) < self.min_change_bps:
            self.log("BTC change {:.1f}bps < threshold {:.1f}bps".format(
                change_bps, self.min_change_bps))
            return

        # Check direction consistency
        trend_up = change_bps > 0
        matching = sum(1 for s in recent if (s["outcome"] == "Up") == trend_up)
        direction_pct = matching / len(recent)

        if direction_pct < self.min_direction_pct:
            self.log("Direction consistency {:.0f}% < threshold {:.0f}%".format(
                direction_pct * 100, self.min_direction_pct * 100))
            return

        self._should_buy = "buy_up" if trend_up else "buy_down"
        self.log("Trend: BTC {}{:.1f}bps over {} sessions, {:.0f}% confirm -> {}".format(
            "+" if change_bps > 0 else "", change_bps, len(recent),
            direction_pct * 100, self._should_buy))

    def on_tick(self, ctx):
        # type: (Any) -> TradeAction
        if self._should_buy is not None and not self._bought:
            self._bought = True
            if self._should_buy == "buy_up":
                price = ctx.sample.get("up_token_midpoint")
            else:
                price = ctx.sample.get("down_token_midpoint")
            return TradeAction(
                side=self._should_buy,
                amount=ctx.position.cash * self.bet_fraction,
                price=price,
            )
        return HOLD

    def param_grid(self):
        # type: () -> Dict[str, List[Any]]
        # 4 * 3 * 5 * 4 * 3 = 720 combos
        return {
            "min_sessions": [3, 5, 7, 10],
            "lookback_minutes": [30, 60, 120],
            "min_change_bps": [3.0, 5.0, 10.0, 20.0, 50.0],
            "min_direction_pct": [0.5, 0.6, 0.75, 0.9],
            "bet_fraction": [0.5, 0.75, 1.0],
        }
