"""Core simulation engine for backtesting Polymarket up/down strategies."""

import glob
import json
import math
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
    starting_cash: float = 0.0  # bankroll at session start (for compounding)


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
    # Kelly / geometric growth metrics
    geo_mean_return: float = 0.0    # geometric mean per-session return (Kelly maximizes this)
    kelly_fraction: float = 0.0     # estimated optimal Kelly fraction from observed outcomes
    log_growth: float = 0.0         # total log-wealth growth (sum of log(1+r_i))
    final_bankroll: float = 0.0     # ending bankroll in compound mode (0 = non-compound)
    max_drawdown: float = 0.0       # worst peak-to-trough drawdown fraction


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


def _compute_kelly_metrics(results, starting_cash, compound):
    # type: (List[SessionResult], float, bool) -> Dict[str, float]
    """Compute Kelly criterion and geometric growth metrics from session results.

    For a binary-outcome market with repeated bets, the Kelly criterion says
    bet f* = p/a - q/b where p=win prob, q=1-p, b=avg win return, a=avg loss.
    The geometric growth rate g = p*log(1+f*b) + q*log(1-f*a) is what Kelly
    maximizes — it's the metric that determines long-run bankroll growth.
    """
    n = len(results)
    if n == 0:
        return {"geo_mean_return": 0.0, "kelly_fraction": 0.0,
                "log_growth": 0.0, "final_bankroll": starting_cash,
                "max_drawdown": 0.0}

    # Per-session return fractions
    returns = []  # type: List[float]
    for r in results:
        base = r.starting_cash if compound else starting_cash
        if base > 0:
            returns.append(r.pnl / base)
        else:
            returns.append(0.0)

    # Log-wealth growth: sum of log(1 + r_i), skip sessions where bankroll went to 0
    log_growth = 0.0
    for ret in returns:
        if 1.0 + ret > 0:
            log_growth += math.log(1.0 + ret)
        else:
            log_growth = float("-inf")
            break

    # Geometric mean return per session
    if math.isfinite(log_growth) and n > 0:
        geo_mean_return = math.exp(log_growth / n) - 1.0
    else:
        geo_mean_return = -1.0  # total ruin

    # Max drawdown (peak-to-trough on cumulative wealth)
    wealth = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        wealth *= (1.0 + ret)
        if wealth > peak:
            peak = wealth
        dd = (peak - wealth) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # Final bankroll (compound mode)
    final_bankroll = starting_cash
    for ret in returns:
        final_bankroll *= (1.0 + ret)

    # Estimate Kelly fraction from observed win/loss distribution
    # For variable-size bets: f* = mean(r) / mean(r^2)  (continuous Kelly)
    win_returns = [r for r in returns if r > 0]
    loss_returns = [r for r in returns if r < 0]
    if win_returns and loss_returns:
        mean_r = sum(returns) / n
        mean_r2 = sum(r * r for r in returns) / n
        kelly_f = mean_r / mean_r2 if mean_r2 > 0 else 0.0
    elif win_returns:
        kelly_f = 1.0  # all wins, but capped at 1
    else:
        kelly_f = 0.0  # no wins

    return {
        "geo_mean_return": geo_mean_return,
        "kelly_fraction": max(0.0, min(kelly_f, 2.0)),  # cap at 2x Kelly
        "log_growth": log_growth if math.isfinite(log_growth) else -999.0,
        "final_bankroll": max(0.0, final_bankroll),
        "max_drawdown": max_dd,
    }


class Simulator:
    def __init__(
        self,
        starting_cash=100.0,
        execution_mode="book",
        slippage=0.02,
        all_sessions=None,
        compound=False,
    ):
        self.starting_cash = starting_cash
        self.execution_mode = execution_mode
        self.slippage = slippage
        self.compound = compound
        # Pre-build sorted session summaries for strategies that need lookback
        self._session_summaries = (
            _build_session_summaries(all_sessions) if all_sessions else None
        )

    def run_session(self, strategy, session_dict, session_cash=None):
        # type: (Any, Dict[str, Any], Optional[float]) -> SessionResult
        """Replay one session through a strategy.

        If session_cash is provided (compound mode), use it instead of
        self.starting_cash as the bankroll for this session.
        """
        cash = session_cash if session_cash is not None else self.starting_cash
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

        position = Position(cash=cash)
        executor = SimExecutor(self.execution_mode, self.slippage)
        trader = Trader(strategy, executor)
        trader.start_session(metadata, position, initial_btc, window_duration,
                             reference_balance=cash)

        for sample in samples:
            trader.process_tick(sample)

        trades = trader.end_session(outcome)

        pnl = position.cash - cash
        pnl_pct = (pnl / cash) * 100.0 if cash > 0 else 0.0

        return SessionResult(
            session_file=source_file,
            outcome=outcome,
            pnl=pnl,
            pnl_pct=pnl_pct,
            trades=trades,
            final_position=position,
            num_samples=len(samples),
            starting_cash=cash,
        )

    def run_backtest(self, strategy, sessions):
        # type: (Any, List[Dict[str, Any]]) -> BacktestResult
        """Run a strategy across all sessions and aggregate stats."""
        results = []
        bankroll = self.starting_cash

        for s in sessions:
            if self.compound:
                if bankroll < 0.01:
                    break  # ruin — stop
                result = self.run_session(strategy, s, session_cash=bankroll)
                bankroll = result.final_position.cash
            else:
                result = self.run_session(strategy, s)
            results.append(result)

        total_pnl = sum(r.pnl for r in results)
        n = len(results)
        mean_pnl = total_pnl / n if n > 0 else 0.0
        wins = sum(1 for r in results if r.pnl > 0)
        win_rate = wins / n if n > 0 else 0.0

        # Sharpe: mean / std of per-session PnL percentage
        if n > 1:
            pnls = [r.pnl for r in results]
            mean = mean_pnl
            variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            std = variance ** 0.5
            sharpe = mean / std if std > 0 else 0.0
        else:
            sharpe = 0.0

        # Kelly / geometric growth metrics
        kelly = _compute_kelly_metrics(results, self.starting_cash, self.compound)

        return BacktestResult(
            strategy_name=strategy.name,
            params=dict(getattr(strategy, "_params", {})),
            sessions=results,
            total_pnl=total_pnl,
            mean_pnl=mean_pnl,
            win_rate=win_rate,
            sharpe=sharpe,
            num_sessions=n,
            geo_mean_return=kelly["geo_mean_return"],
            kelly_fraction=kelly["kelly_fraction"],
            log_growth=kelly["log_growth"],
            final_bankroll=kelly["final_bankroll"],
            max_drawdown=kelly["max_drawdown"],
        )
