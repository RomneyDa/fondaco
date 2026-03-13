"""Centralized session statistics cache.

Pre-computes per-session stats once and serves lookback queries to any
consumer: live bots (file-backed), backtester (in-memory), dashboard (API).

Per-session record is ~200 bytes — thousands of sessions fit in memory.
Raw sample data is never stored.
"""

import json
import os
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.defaults import BPS_DIVISOR, TOKEN_DECIMALS


class SessionStats:
    """Pre-computed stats for one resolved session.

    Uses __slots__ to keep memory tight (thousands of these in memory).
    """
    __slots__ = (
        'window_start', 'outcome', 'initial_btc', 'final_btc',
        'return_bps', 'abs_return_bps', 'source_file',
    )

    def __init__(
        self,
        window_start,   # type: str
        outcome,        # type: str
        initial_btc,    # type: float
        final_btc,      # type: float
        return_bps,     # type: float
        abs_return_bps,  # type: float
        source_file="",  # type: str
    ):
        self.window_start = window_start
        self.outcome = outcome
        self.initial_btc = initial_btc
        self.final_btc = final_btc
        self.return_bps = return_bps
        self.abs_return_bps = abs_return_bps
        self.source_file = source_file

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "window_start": self.window_start,
            "outcome": self.outcome,
            "initial_btc": self.initial_btc,
            "final_btc": self.final_btc,
            "return_bps": round(self.return_bps, TOKEN_DECIMALS),
            "abs_return_bps": round(self.abs_return_bps, TOKEN_DECIMALS),
            "source_file": self.source_file,
        }

    # Dict-style access for backward compatibility with strategies that
    # use summary.get("initial_btc") etc.
    def get(self, key, default=None):
        # type: (str, Any) -> Any
        return getattr(self, key, default)

    def __getitem__(self, key):
        # type: (str) -> Any
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)


def _make_stats(window_start, outcome, initial_btc, final_btc, source_file=""):
    # type: (str, str, float, float, str) -> Optional[SessionStats]
    """Create a SessionStats from raw fields, computing derived values."""
    if not initial_btc or not final_btc or initial_btc <= 0:
        return None
    if not outcome:
        return None
    ret = (final_btc - initial_btc) / initial_btc * BPS_DIVISOR
    return SessionStats(
        window_start=window_start,
        outcome=outcome,
        initial_btc=initial_btc,
        final_btc=final_btc,
        return_bps=ret,
        abs_return_bps=abs(ret),
        source_file=source_file,
    )


class SessionStatsCache:
    """Sorted cache of pre-computed session stats.

    Supports efficient lookback queries via bisect on window_start.
    Always in-memory. save() writes a JSON snapshot for external consumers
    (e.g. dashboard) but the cache itself is never loaded from file.
    """

    def __init__(self):
        # type: () -> None
        self._entries = []  # type: List[SessionStats]
        self._starts = []   # type: List[str]  # parallel sorted list for bisect

    @property
    def entries(self):
        # type: () -> List[SessionStats]
        return self._entries

    def __len__(self):
        # type: () -> int
        return len(self._entries)

    # ── Build from data ──────────────────────────────────────────────

    @classmethod
    def from_sessions(cls, session_dicts):
        # type: (List[Dict[str, Any]]) -> SessionStatsCache
        """Build cache from loaded session dicts (backtest mode).

        Expects the same format as load_sessions() returns: each dict has
        metadata.window_start_utc, outcome, initial/final_btc_price_usd.
        """
        cache = cls()
        for s in session_dicts:
            meta = s.get("metadata", {})
            ws = meta.get("window_start_utc", "")
            stats = _make_stats(
                window_start=ws,
                outcome=s.get("outcome", ""),
                initial_btc=s.get("initial_btc_price_usd") or 0.0,
                final_btc=s.get("final_btc_price_usd") or 0.0,
                source_file=s.get("_source_file", ""),
            )
            if stats:
                cache._entries.append(stats)
        cache._entries.sort(key=lambda e: e.window_start)
        cache._starts = [e.window_start for e in cache._entries]
        return cache

    @classmethod
    def from_summaries(cls, summaries):
        # type: (List[Dict[str, Any]]) -> SessionStatsCache
        """Build cache from session summaries (legacy compatibility).

        Summaries have window_start (datetime), initial_btc, final_btc, outcome.
        """
        cache = cls()
        for s in summaries:
            ws = s.get("window_start")
            if ws is None:
                continue
            # Convert datetime to ISO string if needed
            if hasattr(ws, 'isoformat'):
                ws_str = ws.isoformat()
            else:
                ws_str = str(ws)
            stats = _make_stats(
                window_start=ws_str,
                outcome=s.get("outcome", ""),
                initial_btc=s.get("initial_btc") or 0.0,
                final_btc=s.get("final_btc") or 0.0,
            )
            if stats:
                cache._entries.append(stats)
        cache._entries.sort(key=lambda e: e.window_start)
        cache._starts = [e.window_start for e in cache._entries]
        return cache

    # ── File I/O (dashboard snapshot only) ─────────────────────────

    def save(self, file_path):
        # type: (str) -> None
        """Write cache to JSON file for external consumers (e.g. dashboard).

        Atomic via tmp + replace. This is write-only — the cache is never
        loaded from file. Called once per session (~every 5 min).
        """
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        tmp = file_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([e.to_dict() for e in self._entries], f)
        os.replace(tmp, file_path)

    def add(self, window_start, outcome, initial_btc, final_btc, source_file=""):
        # type: (str, str, float, float, str) -> bool
        """Add a single session's stats. Returns True if added (not duplicate)."""
        # Deduplicate by window_start
        idx = bisect_left(self._starts, window_start)
        if idx < len(self._starts) and self._starts[idx] == window_start:
            return False  # already exists

        stats = _make_stats(window_start, outcome, initial_btc, final_btc, source_file)
        if stats is None:
            return False

        self._entries.insert(idx, stats)
        self._starts.insert(idx, window_start)
        return True

    # ── Lookback queries ─────────────────────────────────────────────

    def prior(self, before, n):
        # type: (str, int) -> List[SessionStats]
        """Return the last N sessions strictly before `before` timestamp.

        Args:
            before: ISO timestamp (e.g. current session's window_start)
            n: max number of prior sessions to return

        Returns sorted oldest-first.
        """
        idx = bisect_left(self._starts, before)
        start = max(0, idx - n)
        return self._entries[start:idx]

    def prior_delta(self, before, n, after):
        # type: (str, int, str) -> List[SessionStats]
        """Return prior sessions the caller doesn't have yet.

        Returns sessions in (after, before) — i.e. newer than `after` and
        older than `before`. Limited to at most N total (counting from
        before backwards). Use this for incremental cache updates:

            already_have = cache.prior(before, n)
            # ... later, new sessions added ...
            new_ones = cache.prior_delta(before, n, after=already_have[-1].window_start)
        """
        end_idx = bisect_left(self._starts, before)
        start_idx = max(0, end_idx - n)
        # Skip entries at or before `after`
        after_idx = bisect_right(self._starts, after)
        actual_start = max(start_idx, after_idx)
        return self._entries[actual_start:end_idx]

    def all_stats(self):
        # type: () -> List[SessionStats]
        """Return all cached stats, sorted oldest-first."""
        return list(self._entries)

    # ── Gap-aware queries ─────────────────────────────────────────────

    @staticmethod
    def _parse_ts(ts_str):
        # type: (str) -> datetime
        """Parse ISO timestamp to datetime."""
        s = ts_str
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def gap_before(self, window_start):
        # type: (str) -> Optional[float]
        """Seconds between the session at `window_start` and the one before it.

        Returns None if there is no prior session.
        """
        idx = bisect_left(self._starts, window_start)
        if idx <= 0:
            return None
        try:
            current = self._parse_ts(window_start)
            prev = self._parse_ts(self._starts[idx - 1])
            return (current - prev).total_seconds()
        except (ValueError, TypeError):
            return None

    def data_density(self, before, n, expected_interval_s):
        # type: (str, int, float) -> float
        """Fraction of expected sessions actually present in the lookback window.

        Uses the time span between first and last session in the window
        divided by expected_interval_s to estimate how many sessions
        *should* exist, then compares to how many actually do.

        Args:
            expected_interval_s: Expected seconds between consecutive sessions
                (e.g. 300 for 5-min markets, 900 for 15-min).

        Returns 1.0 if insufficient data to compute or if n <= 1.
        """
        entries = self.prior(before, n)
        if len(entries) < 2:
            return 1.0
        try:
            first_ts = self._parse_ts(entries[0].window_start)
            last_ts = self._parse_ts(entries[-1].window_start)
        except (ValueError, TypeError):
            return 1.0
        span_s = (last_ts - first_ts).total_seconds()
        if span_s <= 0:
            return 1.0
        expected_count = span_s / expected_interval_s + 1
        if expected_count <= 0:
            return 1.0
        return min(len(entries) / expected_count, 1.0)

    def prior_contiguous(self, before, n, max_gap_s=None):
        # type: (str, int, Optional[float]) -> List[SessionStats]
        """Return prior sessions, stopping at the first large gap.

        Like prior(), but walks backwards from `before` and stops when
        two consecutive sessions are more than max_gap_s apart. This
        ensures the returned window contains only temporally dense data.

        Returns oldest-first (same as prior()).
        """
        idx = bisect_left(self._starts, before)
        result = []  # type: List[SessionStats]
        prev_ts = None  # type: Optional[datetime]
        # Walk backwards
        for i in range(idx - 1, max(idx - n - 1, -1), -1):
            try:
                ts = self._parse_ts(self._starts[i])
            except (ValueError, TypeError):
                break
            if prev_ts is not None:
                gap = (prev_ts - ts).total_seconds()
                if gap > max_gap_s:
                    break
            result.append(self._entries[i])
            prev_ts = ts
        result.reverse()
        return result
