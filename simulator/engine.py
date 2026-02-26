"""Core simulation engine for backtesting Polymarket up/down strategies."""

import glob
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Re-export core types from core.trader for backward compatibility.
from core.trader import (
    TradeAction,
    HOLD,
    Position,
    TickContext,
    Trade,
    SimExecutor,
    Trader,
)


def _parse_utc(s):
    # type: (str) -> datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_session_summaries(sessions):
    # type: (List[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Build sorted list of session summaries from loaded session dicts."""
    summaries = []
    for s in sessions:
        meta = s.get("metadata", {})
        ws = meta.get("window_start_utc", "")
        try:
            window_start = _parse_utc(ws)
        except (ValueError, TypeError):
            continue
        initial_btc = s.get("initial_btc_price_usd")
        final_btc = s.get("final_btc_price_usd")
        outcome = s.get("outcome")
        if initial_btc and final_btc and outcome:
            summaries.append({
                "window_start": window_start,
                "initial_btc": initial_btc,
                "final_btc": final_btc,
                "outcome": outcome,
            })
    summaries.sort(key=lambda x: x["window_start"])
    return summaries


@dataclass
class SessionResult:
    """Result for one session."""
    session_file: str
    outcome: str
    pnl: float
    pnl_pct: float
    trades: List[Trade]
    final_position: Position
    num_samples: int


@dataclass
class BacktestResult:
    """Aggregate results across sessions."""
    strategy_name: str
    params: Dict[str, Any]
    sessions: List[SessionResult]
    total_pnl: float
    mean_pnl: float
    win_rate: float
    sharpe: float
    num_sessions: int


def load_sessions(data_dir):
    # type: (str) -> List[Dict[str, Any]]
    """Load all resolved session JSON files from a directory tree."""
    pattern = os.path.join(data_dir, "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    if not files:
        # Also try non-recursive (flat directory)
        pattern = os.path.join(data_dir, "*.json")
        files = glob.glob(pattern)
    sessions = []
    for f in sorted(files):
        with open(f, "r") as fh:
            data = json.load(fh)
        # Skip unresolved sessions and non-session files (e.g. .trading.json)
        if data.get("outcome") is None or "samples" not in data:
            continue
        data["_source_file"] = f
        sessions.append(data)
    return sessions


class Simulator:
    def __init__(
        self,
        starting_cash=100.0,
        execution_mode="book",
        slippage=0.02,
        all_sessions=None,
    ):
        self.starting_cash = starting_cash
        self.execution_mode = execution_mode
        self.slippage = slippage
        # Pre-build sorted session summaries for strategies that need lookback
        self._session_summaries = (
            _build_session_summaries(all_sessions) if all_sessions else None
        )

    def run_session(self, strategy, session_dict):
        # type: (Any, Dict[str, Any]) -> SessionResult
        """Replay one session through a strategy."""
        samples = session_dict["samples"]
        metadata = session_dict["metadata"]
        outcome = session_dict["outcome"]
        initial_btc = session_dict.get("initial_btc_price_usd") or 0.0
        source_file = session_dict.get("_source_file", "")

        # Inject prior session summaries for strategies that need lookback
        if self._session_summaries is not None:
            ws = metadata.get("window_start_utc", "")
            try:
                current_start = _parse_utc(ws)
                prior = [s for s in self._session_summaries
                         if s["window_start"] < current_start]
                metadata = dict(metadata)
                metadata["_prior_session_summaries"] = prior
            except (ValueError, TypeError):
                pass

        # Compute window duration from samples
        if len(samples) >= 2:
            window_duration = samples[-1]["elapsed_seconds"]
        else:
            window_duration = 300.0

        position = Position(cash=self.starting_cash)
        executor = SimExecutor(self.execution_mode, self.slippage)
        trader = Trader(strategy, executor)
        trader.start_session(metadata, position, initial_btc, window_duration,
                             reference_balance=self.starting_cash)

        for sample in samples:
            trader.process_tick(sample)

        trades = trader.end_session(outcome)

        pnl = position.cash - self.starting_cash
        pnl_pct = (pnl / self.starting_cash) * 100.0 if self.starting_cash > 0 else 0.0

        return SessionResult(
            session_file=source_file,
            outcome=outcome,
            pnl=pnl,
            pnl_pct=pnl_pct,
            trades=trades,
            final_position=position,
            num_samples=len(samples),
        )

    def run_backtest(self, strategy, sessions):
        # type: (Any, List[Dict[str, Any]]) -> BacktestResult
        """Run a strategy across all sessions and aggregate stats."""
        results = []
        for s in sessions:
            results.append(self.run_session(strategy, s))

        total_pnl = sum(r.pnl for r in results)
        n = len(results)
        mean_pnl = total_pnl / n if n > 0 else 0.0
        wins = sum(1 for r in results if r.pnl > 0)
        win_rate = wins / n if n > 0 else 0.0

        # Sharpe: mean / std of per-session PnL
        if n > 1:
            pnls = [r.pnl for r in results]
            mean = mean_pnl
            variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            std = variance ** 0.5
            sharpe = mean / std if std > 0 else 0.0
        else:
            sharpe = 0.0

        return BacktestResult(
            strategy_name=strategy.name,
            params=dict(getattr(strategy, "_params", {})),
            sessions=results,
            total_pnl=total_pnl,
            mean_pnl=mean_pnl,
            win_rate=win_rate,
            sharpe=sharpe,
            num_sessions=n,
        )
