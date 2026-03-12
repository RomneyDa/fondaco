"""Core simulation engine for backtesting Polymarket up/down strategies."""

import glob
import hashlib
import json
import math
import os
import pickle
from datetime import datetime, timezone
from dataclasses import dataclass
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
from core.models import Sample


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
    total_fees: float = 0.0    # total taker fees paid this session


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
    kelly_fraction: float = 0.0     # optimal Kelly fraction (maximizes log growth)
    dd_ratio: float = 0.0           # max fraction keeping drawdown <= 25%
    log_growth: float = 0.0         # total log-wealth growth (sum of log(1+r_i))
    final_bankroll: float = 0.0     # ending bankroll in compound mode (0 = non-compound)
    max_drawdown: float = 0.0       # worst peak-to-trough drawdown fraction
    total_fees: float = 0.0          # total taker fees paid across all sessions


def _list_session_files(data_dir):
    # type: (str) -> List[str]
    """List all JSON files in a data directory, sorted by name."""
    pattern = os.path.join(data_dir, "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    if not files:
        pattern = os.path.join(data_dir, "*.json")
        files = glob.glob(pattern)
    return sorted(files)


def _cache_path(data_dir, suffix=""):
    # type: (str, str) -> str
    """Return a cache path for a given data directory."""
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
    dir_hash = hashlib.md5(os.path.normpath(data_dir).encode()).hexdigest()[:12]
    return os.path.join(cache_dir, "sessions_{}{}.pkl".format(dir_hash, suffix))


def _dir_fingerprint(files):
    # type: (List[str]) -> str
    """Quick fingerprint: file count + total size + newest mtime."""
    count = len(files)
    total_size = 0
    newest = 0.0
    for f in files:
        try:
            st = os.stat(f)
            total_size += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
        except OSError:
            pass
    return "{}:{}:{:.6f}".format(count, total_size, newest)


def load_session_summaries(data_dir):
    # type: (str) -> List[Dict[str, Any]]
    """Load lightweight session summaries for all sessions (no sample data).

    Much faster than load_sessions — reads only the metadata fields needed
    for _build_session_summaries. Uses a pickle cache.
    """
    files = _list_session_files(data_dir)
    if not files:
        return []
    cache_file = _cache_path(data_dir, "_summaries")
    fingerprint = _dir_fingerprint(files)

    # Try cache
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as fh:
                cached = pickle.load(fh)
            if cached.get("fingerprint") == fingerprint:
                return cached["summaries"]
        except (pickle.UnpicklingError, EOFError, KeyError, OSError):
            pass

    # Build from JSON (fast — only reads metadata, not samples)
    summaries = []
    for f in files:
        with open(f, "r") as fh:
            data = json.load(fh)
        if data.get("outcome") is None or "samples" not in data:
            continue
        meta = data.get("metadata", {})
        ws = meta.get("window_start_utc", "")
        try:
            window_start = _parse_utc(ws)
        except (ValueError, TypeError):
            continue
        initial_btc = data.get("initial_btc_price_usd")
        final_btc = data.get("final_btc_price_usd")
        outcome = data.get("outcome")
        if initial_btc and final_btc and outcome:
            summaries.append({
                "window_start": window_start,
                "initial_btc": initial_btc,
                "final_btc": final_btc,
                "outcome": outcome,
            })
    summaries.sort(key=lambda x: x["window_start"])

    # Cache
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "wb") as fh:
            pickle.dump({"fingerprint": fingerprint, "summaries": summaries}, fh,
                        protocol=pickle.HIGHEST_PROTOCOL)
    except OSError:
        pass

    return summaries


def load_sessions(data_dir, recent=None):
    # type: (str, Optional[int]) -> List[Dict[str, Any]]
    """Load resolved session JSON files from a directory tree.

    Args:
        data_dir: Path to session files directory.
        recent: If set, only fully load the N most recent sessions.
                Saves significant I/O and memory when --recent is used.

    Samples are kept as raw dicts for fast loading. Conversion to Sample
    objects happens lazily in Simulator.run_session (only for sessions
    actually replayed).
    """
    files = _list_session_files(data_dir)
    if not files:
        return []

    # Filter to session files only (need outcome + samples)
    # We need to identify which files are valid sessions to count correctly.
    # With --recent, only load the last N valid session files.
    if recent is not None and recent < len(files):
        # Load from the end — we need to find the last `recent` valid sessions.
        # Scan backwards to avoid loading all files.
        sessions = []
        for f in reversed(files):
            with open(f, "r") as fh:
                data = json.load(fh)
            if data.get("outcome") is None or "samples" not in data:
                continue
            data["_source_file"] = f
            sessions.append(data)
            if len(sessions) >= recent:
                break
        sessions.reverse()
    else:
        sessions = []
        for f in files:
            with open(f, "r") as fh:
                data = json.load(fh)
            if data.get("outcome") is None or "samples" not in data:
                continue
            data["_source_file"] = f
            sessions.append(data)

    return sessions


def _convert_samples(session_dict):
    # type: (Dict[str, Any]) -> None
    """Convert sample dicts to Sample objects in-place (lazy, once per session).

    Sample objects use __slots__ for faster attribute access in the hot
    tick loop vs dict .get() calls.
    """
    samples = session_dict.get("samples")
    if samples and isinstance(samples[0], dict):
        session_dict["samples"] = [Sample(s) for s in samples]


def convert_all_samples(sessions):
    # type: (List[Dict[str, Any]]) -> None
    """Eagerly convert all session sample dicts to Sample objects.

    Call in the parent process before forking workers so Sample objects
    live in shared CoW memory and workers never trigger page copies.
    """
    for session_dict in sessions:
        _convert_samples(session_dict)


def pre_slice_summaries(sessions, session_summaries=None, stats_cache=None):
    # type: (List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Any) -> None
    """Pre-compute per-session prior summaries and attach to metadata.

    Without this, every variant × session combo filters the full summary
    list in run_session(). Pre-slicing does it once per session in the
    parent process before forking.

    If stats_cache (SessionStatsCache) is provided, uses its efficient
    bisect-based lookup. Falls back to linear scan of session_summaries.
    """
    for session_dict in sessions:
        metadata = session_dict.get("metadata", {})
        ws = metadata.get("window_start_utc", "")
        if not ws:
            continue

        if stats_cache is not None:
            # Efficient bisect lookup — O(log n) per session
            # Use a large N; strategies will slice to their own lookback window
            prior = stats_cache.prior(before=ws, n=len(stats_cache))
            metadata["_prior_session_summaries"] = prior
        elif session_summaries is not None:
            try:
                current_start = _parse_utc(ws)
                prior = [s for s in session_summaries
                         if s["window_start"] < current_start]
                metadata["_prior_session_summaries"] = prior
            except (ValueError, TypeError):
                pass


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

    # Estimate optimal Kelly fraction numerically.
    # Ternary search on concave log-growth function (converges in ~50 iterations
    # vs 100-step linear scan). f* maximizes E[log(1 + f*r)].
    kelly_f = _ternary_kelly_search(returns)

    # Recommended session-cash-ratio: what fraction f of bankroll to deploy
    # per session, where the strategy receives f*bankroll as its cash.
    #
    # The strategy internally sizes its bets, so the average loss per session
    # (as fraction of cash given to the strategy) tells us the real risk.
    # avg_loss_frac = mean(|pnl|/cash) for losing sessions — this is what
    # you actually lose per dollar deployed when the strategy loses.
    #
    # To survive k consecutive average losses within a target drawdown:
    #   (1 - f * avg_loss_frac)^k >= (1 - target)
    #   f <= (1 - (1-target)^(1/k)) / avg_loss_frac
    target_max_dd = 0.25
    # Expected longest losing streak over ~1 month of 5-min sessions.
    # Formula: E[max_streak] ≈ log(N) / log(1/loss_rate)
    # With ~288 traded sessions/day × 30 days ≈ 8640 opportunities.
    win_count = sum(1 for r in returns if r > 0)
    traded_count = sum(1 for r in returns if r != 0)
    loss_rate = 1.0 - (win_count / traded_count) if traded_count > 0 else 0.5
    sessions_per_month = 8640  # ~288/day × 30 days
    if loss_rate > 0 and loss_rate < 1:
        consec_losses = max(5, int(math.ceil(
            math.log(sessions_per_month) / math.log(1.0 / loss_rate)
        )))
    else:
        consec_losses = 10
    # Compute average loss as fraction of cash *deployed to the strategy*
    loss_fracs = []  # type: List[float]
    for r in results:
        if r.pnl < 0:
            base = r.starting_cash if compound else starting_cash
            if base > 0:
                loss_fracs.append(abs(r.pnl) / base)
    if loss_fracs:
        avg_loss_frac = sum(loss_fracs) / len(loss_fracs)
        if avg_loss_frac > 0:
            max_per_session_loss = 1.0 - (1.0 - target_max_dd) ** (1.0 / consec_losses)
            dd_ratio = round(min(max_per_session_loss / avg_loss_frac, 1.0), 2)
        else:
            dd_ratio = 1.0
    else:
        dd_ratio = 1.0  # no losses observed

    return {
        "geo_mean_return": geo_mean_return,
        "kelly_fraction": max(0.0, min(kelly_f, 1.0)),
        "dd_ratio": dd_ratio,
        "log_growth": log_growth if math.isfinite(log_growth) else -999.0,
        "final_bankroll": max(0.0, final_bankroll),
        "max_drawdown": max_dd,
    }


def _log_growth_at_f(f, returns):
    # type: (float, List[float]) -> float
    """Compute E[log(1 + f*r)] for a given f and return series."""
    g = 0.0
    for ret in returns:
        v = 1.0 + f * ret
        if v <= 0:
            return float("-inf")
        g += math.log(v)
    return g


def _ternary_kelly_search(returns, lo=0.001, hi=1.0, tol=0.005):
    # type: (List[float], float, float, float) -> float
    """Find Kelly-optimal f via ternary search on the concave log-growth function.

    Converges in ~log2((hi-lo)/tol) ≈ 8 iterations instead of a 100-step scan.
    """
    if not returns:
        return 0.0
    for _ in range(50):  # more than enough for convergence
        if hi - lo < tol:
            break
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        g1 = _log_growth_at_f(m1, returns)
        g2 = _log_growth_at_f(m2, returns)
        if g1 < g2:
            lo = m1
        else:
            hi = m2
    best_f = (lo + hi) / 2.0
    # Only return positive f if it actually has positive growth
    if _log_growth_at_f(best_f, returns) <= 0:
        return 0.0
    return best_f


# Default fractional Kelly multiplier (half-Kelly).
# Full Kelly is usually too aggressive due to estimation error and regime shifts.
# Half-Kelly gives ~75% of the growth rate with much less variance.
KELLY_FRACTION = 0.5


def compute_kelly_sizing(results, starting_cash, kelly_multiplier=KELLY_FRACTION):
    # type: (List[SessionResult], float, float) -> Dict[str, float]
    """Compute optimal position sizing from raw (unscaled) session returns.

    Phase 2 of the two-phase backtest: given a strategy's per-session return
    series from flat-cash backtesting (Phase 1), compute the Kelly-optimal
    fraction of bankroll to deploy per session.

    Returns include full Kelly f*, the recommended fractional Kelly cash_pct,
    and projected compound growth at that sizing.

    The return series includes zeros for no-trade sessions — this is correct
    because the decision not to trade is part of the strategy and represents
    a real deployment opportunity.

    Laplace smoothing (Rule of Three): when few or no losses are observed,
    the empirical distribution underestimates tail risk. With 0 losses in
    n trials, the 95% CI upper bound on the true loss rate is ~3/n (the
    "rule of three" from frequentist statistics; equivalently, the 95th
    percentile of the Beta(1, n+1) posterior). We inject synthetic total-loss
    events to match this upper bound, preventing Kelly from recommending
    dangerously large positions when the sample happens to contain no losses.

    Why this matters: with 1 fixed synthetic loss (the old approach), the
    implied loss rate shrinks as 1/n — so 200 sessions → 0.5% loss rate →
    Kelly f*=0.55 → half-Kelly 28%. With rule-of-three scaling, 200 sessions
    → 3 synthetic losses → ~1.5% loss rate → much more conservative sizing.
    """
    # Extract per-session return fractions (on deployed capital)
    returns = []  # type: List[float]
    for r in results:
        if r.starting_cash > 0:
            returns.append(r.pnl / r.starting_cash)
        else:
            returns.append(0.0)

    n = len(returns)
    if n == 0:
        return {"kelly_f": 0.0, "cash_pct": 0.0, "projected_geo_return": 0.0,
                "projected_final": starting_cash, "projected_max_dd": 0.0}

    # Laplace smoothing via Rule of Three.
    traded_returns = [r for r in returns if r != 0.0]
    loss_count = sum(1 for r in traded_returns if r < 0)
    if loss_count == 0 and len(traded_returns) > 0:
        n_traded = len(traded_returns)
        n_inject = max(1, round(3.0 * n_traded / (n_traded + 3)))
        smoothed_returns = returns + [-1.0] * n_inject
    else:
        smoothed_returns = returns

    # Ternary search for Kelly-optimal f (replaces 1000-step linear scan)
    kelly_f = _ternary_kelly_search(smoothed_returns)
    cash_pct = round(kelly_f * kelly_multiplier, 4)

    # Project compound growth at recommended cash_pct
    bankroll = starting_cash
    peak = starting_cash
    max_dd = 0.0
    for ret in returns:
        session_cash = bankroll * cash_pct
        pnl = session_cash * ret
        bankroll += pnl
        if bankroll > peak:
            peak = bankroll
        dd = (peak - bankroll) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    geo_return = 0.0
    if bankroll > 0 and starting_cash > 0 and n > 0:
        geo_return = math.exp(math.log(bankroll / starting_cash) / n) - 1.0

    return {
        "kelly_f": kelly_f,
        "cash_pct": cash_pct,
        "kelly_multiplier": kelly_multiplier,
        "projected_geo_return": geo_return,
        "projected_final": bankroll,
        "projected_max_dd": max_dd,
    }


class Simulator:
    def __init__(
        self,
        starting_cash=100.0,
        execution_mode="book",
        slippage=0.02,
        all_sessions=None,
        compound=False,
        fees=True,
        session_cash_ratio=None,
        session_summaries=None,
        limit_fill_margin=None,
        latency_ms=0,
    ):
        self.starting_cash = starting_cash
        self.execution_mode = execution_mode
        self.slippage = slippage
        self.limit_fill_margin = limit_fill_margin
        self.latency_ms = latency_ms
        self.compound = compound
        self.fees = fees
        self.session_cash_ratio = session_cash_ratio  # (ratio, max) or None
        # Pre-build sorted session summaries for strategies that need lookback.
        # Accept pre-computed summaries (from parent process) to avoid pickling
        # all_sessions into every worker.
        if session_summaries is not None:
            self._session_summaries = session_summaries
        elif all_sessions is not None:
            self._session_summaries = _build_session_summaries(all_sessions)
        else:
            self._session_summaries = None

    def run_session(self, strategy, session_dict, session_cash=None):
        # type: (Any, Dict[str, Any], Optional[float]) -> SessionResult
        """Replay one session through a strategy.

        If session_cash is provided (compound mode), use it instead of
        self.starting_cash as the bankroll for this session.
        """
        cash = session_cash if session_cash is not None else self.starting_cash
        # Lazy conversion: convert sample dicts to Sample objects on first use
        _convert_samples(session_dict)
        samples = session_dict["samples"]
        metadata = session_dict["metadata"]
        outcome = session_dict["outcome"]
        initial_btc = session_dict.get("initial_btc_price_usd") or 0.0
        source_file = session_dict.get("_source_file", "")

        # Inject prior session summaries for strategies that need lookback.
        # Skip if already pre-sliced by pre_slice_summaries() in parent.
        if (self._session_summaries is not None
                and "_prior_session_summaries" not in metadata):
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
            last = samples[-1]
            window_duration = last.elapsed_seconds
        else:
            window_duration = 300.0

        position = Position(cash=cash)
        executor = SimExecutor(self.execution_mode, self.slippage, fees=self.fees,
                               limit_fill_margin=self.limit_fill_margin,
                               latency_ms=self.latency_ms)
        trader = Trader(strategy, executor, sim_mode=True)
        trader.start_session(metadata, position, initial_btc, window_duration,
                             reference_balance=cash)

        # P5: Skip early ticks for strategies that don't need them.
        # Only skip when requires_full_session=False and strategy has a
        # min_elapsed_hint > 0. Safe because the strategy returns HOLD for
        # all ticks before min_elapsed anyway.
        min_hint = getattr(strategy, 'min_elapsed_hint', 0.0)
        skip_early = (
            min_hint > 0
            and not getattr(strategy, 'requires_full_session', False)
            and window_duration > 0
        )

        # Use fast sim path (reusable TickContext, no auto-exit check, etc.)
        process = trader.process_tick_sim

        if skip_early:
            # Find the first sample index at or after min_elapsed_hint
            target_elapsed = min_hint * window_duration
            # Binary search for the start index
            lo_idx, hi_idx = 0, len(samples)
            while lo_idx < hi_idx:
                mid_idx = (lo_idx + hi_idx) // 2
                s = samples[mid_idx]
                el = s.elapsed_seconds
                if el < target_elapsed:
                    lo_idx = mid_idx + 1
                else:
                    hi_idx = mid_idx
            start_idx = lo_idx
            # Set tick_index to match so strategy sees correct tick_index
            trader._tick_index = start_idx
            for sample in samples[start_idx:]:
                process(sample)
        else:
            for sample in samples:
                process(sample)

        trades = trader.end_session(outcome)

        pnl = position.cash - cash
        pnl_pct = (pnl / cash) * 100.0 if cash > 0 else 0.0
        total_fees = sum(t.fee or 0.0 for t in trades)

        return SessionResult(
            session_file=source_file,
            outcome=outcome,
            pnl=pnl,
            pnl_pct=pnl_pct,
            trades=trades,
            final_position=position,
            num_samples=len(samples),
            starting_cash=cash,
            total_fees=total_fees,
        )

    def run_backtest(self, strategy, sessions):
        # type: (Any, List[Dict[str, Any]]) -> BacktestResult
        """Run a strategy across all sessions and aggregate stats.

        With compound=False (Phase 1): flat cash per session for clean returns.
        With compound=True: full bankroll carried forward (rarely used directly;
        Phase 2 sizing is done by compute_kelly_sizing instead).
        """
        results = []
        bankroll = self.starting_cash

        for s in sessions:
            if self.compound:
                if bankroll < 0.01:
                    break  # ruin — stop
                if self.session_cash_ratio is not None:
                    ratio, cap = self.session_cash_ratio
                    session_cash = min(bankroll * ratio, cap)
                else:
                    session_cash = bankroll
                result = self.run_session(strategy, s, session_cash=session_cash)
                bankroll = (bankroll - session_cash) + result.final_position.cash
            else:
                result = self.run_session(strategy, s)
            results.append(result)

        total_pnl = sum(r.pnl for r in results)
        total_fees = sum(r.total_fees for r in results)
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
            dd_ratio=kelly["dd_ratio"],
            log_growth=kelly["log_growth"],
            final_bankroll=kelly["final_bankroll"],
            max_drawdown=kelly["max_drawdown"],
            total_fees=total_fees,
        )
