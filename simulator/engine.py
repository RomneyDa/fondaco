"""Core simulation engine for backtesting Polymarket up/down strategies."""

import glob
import json
import os
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
        # Skip unresolved sessions
        if data.get("outcome") is None:
            continue
        data["_source_file"] = f
        sessions.append(data)
    return sessions


class Simulator:
    def __init__(
        self,
        starting_cash=100.0,
        execution_mode="midpoint_slippage",
        slippage=0.02,
    ):
        self.starting_cash = starting_cash
        self.execution_mode = execution_mode
        self.slippage = slippage

    def run_session(self, strategy, session_dict):
        # type: (Any, Dict[str, Any]) -> SessionResult
        """Replay one session through a strategy."""
        samples = session_dict["samples"]
        metadata = session_dict["metadata"]
        outcome = session_dict["outcome"]
        initial_btc = session_dict.get("initial_btc_price_usd") or 0.0
        source_file = session_dict.get("_source_file", "")

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
