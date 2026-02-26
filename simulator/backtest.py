#!/usr/bin/env python3
"""CLI runner for backtesting Polymarket up/down trading strategies.

Usage:
    python backtest.py data/sessions/btc-updown-5m
    python backtest.py data/sessions/btc-updown-5m --strategy MomentumEntry
    python backtest.py data/sessions/btc-updown-5m --strategy all --top 10
    python backtest.py data/sessions/btc-updown-5m --execution midpoint --slippage 0.03
"""

import argparse
import itertools
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone

from simulator.engine import Simulator, BacktestResult, load_sessions
from core.defaults import BALANCE_CACHE_FILE, BACKTEST_LOG_FILE
from core.strategies import ALL_STRATEGIES


# -- Process pool worker globals & functions --
_worker_sim = None   # type: Optional[Simulator]
_worker_sessions = None  # type: Optional[list]


def _init_worker(starting_cash, execution_mode, slippage, all_sessions, sessions):
    # type: (float, str, float, list, list) -> None
    """Initializer for each worker process — sets up Simulator and sessions once."""
    global _worker_sim, _worker_sessions
    _worker_sim = Simulator(
        starting_cash=starting_cash,
        execution_mode=execution_mode,
        slippage=slippage,
        all_sessions=all_sessions,
    )
    _worker_sessions = sessions


def _run_one(strategy):
    """Run a single strategy variant in a worker process."""
    return _worker_sim.run_backtest(strategy, _worker_sessions)


def build_variants(strategy_cls):
    # type: (type,) -> list
    """Build all parameter combinations from a strategy's param_grid."""
    grid = strategy_cls().param_grid()
    if not grid:
        return [strategy_cls()]

    keys = sorted(grid.keys())
    values = [grid[k] for k in keys]
    variants = []
    for combo in itertools.product(*values):
        kwargs = dict(zip(keys, combo))
        s = strategy_cls(**kwargs)
        s._params = kwargs
        variants.append(s)
    return variants


def format_params(params):
    # type: (dict,) -> str
    if not params:
        return "-"
    parts = []
    for k, v in sorted(params.items()):
        if isinstance(v, float):
            parts.append("{}={:.3g}".format(k, v))
        else:
            parts.append("{}={}".format(k, v))
    return ", ".join(parts)


def print_leaderboard(results, top_n, optimize):
    # type: (list, int, str) -> None
    """Print a sorted leaderboard of backtest results."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
    }.get(optimize, lambda r: r.sharpe)

    results.sort(key=sort_key, reverse=True)
    shown = results[:top_n]

    # Header
    print("\n{:<20} {:>8} {:>10} {:>10} {:>8} {:>6}  {}".format(
        "Strategy", "Sharpe", "Total PnL", "Mean PnL", "Win%", "N", "Params"
    ))
    print("-" * 95)

    for r in shown:
        print("{:<20} {:>8.3f} {:>10.2f} {:>10.2f} {:>7.1f}% {:>6}  {}".format(
            r.strategy_name,
            r.sharpe,
            r.total_pnl,
            r.mean_pnl,
            r.win_rate * 100,
            r.num_sessions,
            format_params(r.params),
        ))

    print()


def print_strategy_summary(results, optimize):
    # type: (list, str) -> None
    """Print best params per strategy."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
    }.get(optimize, lambda r: r.sharpe)

    # Group by strategy name
    by_strategy = {}  # type: dict
    for r in results:
        by_strategy.setdefault(r.strategy_name, []).append(r)

    print("=" * 70)
    print("BEST PARAMS PER STRATEGY (optimizing: {})".format(optimize))
    print("=" * 70)

    for name in sorted(by_strategy.keys()):
        variants = by_strategy[name]
        best = max(variants, key=sort_key)
        print("\n  {} ({} variants tested)".format(name, len(variants)))
        print("    Best:  Sharpe={:.3f}  PnL={:.2f}  Win={:.1f}%".format(
            best.sharpe, best.total_pnl, best.win_rate * 100,
        ))
        print("    Params: {}".format(format_params(best.params)))

    print()


def print_verbose(result):
    # type: (BacktestResult,) -> None
    """Print per-session breakdown for a single result."""
    print("\n--- {} [{}] ---".format(result.strategy_name, format_params(result.params)))
    for sr in result.sessions:
        trade_parts = []
        for t in sr.trades:
            trade_parts.append("{}@{:.3f}".format(t.side, t.price))
        trade_str = " | " + ", ".join(trade_parts) if trade_parts else ""
        print("  {} outcome={:<4} pnl={:>+7.2f} trades={:<2}{}".format(
            sr.session_file.split("/")[-1] if sr.session_file else "?",
            sr.outcome,
            sr.pnl,
            len(sr.trades),
            trade_str,
        ))
    print("  TOTAL: pnl={:+.2f}  mean={:+.2f}  win_rate={:.1f}%  sharpe={:.3f}".format(
        result.total_pnl, result.mean_pnl, result.win_rate * 100, result.sharpe,
    ))


def log_backtest_run(args, sessions, all_results, optimize):
    # type: (argparse.Namespace, list, list, str) -> None
    """Append a JSONL entry with the full backtest run inputs and results."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
    }.get(optimize, lambda r: r.sharpe)

    # Only log the best params per strategy (not every variant)
    by_strategy = {}  # type: dict
    for r in all_results:
        by_strategy.setdefault(r.strategy_name, []).append(r)

    best_per_strategy = []
    for name in sorted(by_strategy.keys()):
        best = max(by_strategy[name], key=sort_key)
        best_per_strategy.append({
            "strategy": best.strategy_name,
            "params": best.params,
            "variants_tested": len(by_strategy[name]),
            "sharpe": round(best.sharpe, 4),
            "total_pnl": round(best.total_pnl, 4),
            "mean_pnl": round(best.mean_pnl, 4),
            "win_rate": round(best.win_rate, 4),
            "num_sessions": best.num_sessions,
        })

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "data_dir": args.data_dir,
            "strategy": args.strategy,
            "execution": args.execution,
            "slippage": args.slippage,
            "starting_cash": args.cash,
            "optimize": optimize,
            "num_sessions": len(sessions),
            "total_variants": len(all_results),
        },
        "best_per_strategy": best_per_strategy,
    }

    os.makedirs(os.path.dirname(BACKTEST_LOG_FILE), exist_ok=True)
    try:
        with open(BACKTEST_LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print("Warning: failed to write backtest log: {}".format(e))


def main():
    parser = argparse.ArgumentParser(
        description="Backtest trading strategies on Polymarket session data."
    )
    parser.add_argument(
        "data_dir",
        help="Path to session files directory",
    )
    parser.add_argument(
        "--strategy",
        default="all",
        help="Strategy name or 'all' (default: all)",
    )
    parser.add_argument(
        "--optimize",
        default="sharpe",
        choices=["sharpe", "total_pnl", "win_rate"],
        help="Metric to optimize (default: sharpe)",
    )
    parser.add_argument(
        "--execution",
        default="book",
        choices=["midpoint_slippage", "midpoint", "book"],
        help="Execution mode (default: midpoint_slippage)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.02,
        help="Half-spread for midpoint_slippage mode (default: 0.02)",
    )
    parser.add_argument(
        "--cash",
        type=str,
        default="100",
        help='Starting cash per session (default: 100). Use "cached" to load from .cache/balance.json',
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show top N parameter combos (default: 10)",
    )
    parser.add_argument(
        "--recent",
        type=int,
        default=None,
        help="Only use the N most recent sessions",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Per-session breakdown for best params",
    )
    args = parser.parse_args()

    # Resolve starting cash
    if args.cash == "cached":
        try:
            with open(BALANCE_CACHE_FILE, "r") as _f:
                _cached = json.load(_f)
            starting_cash = _cached["cash"]
            print("Loaded cached balance: ${:.2f} (from {}, session {})".format(
                starting_cash, _cached.get("slug_prefix"), _cached.get("session_count"),
            ))
        except (OSError, KeyError, json.JSONDecodeError):
            print("No cached balance found. Use a numeric value or run the bot first.")
            sys.exit(1)
    else:
        starting_cash = float(args.cash)
    # Update args.cash so log_backtest_run logs the numeric value
    args.cash = starting_cash

    # Load sessions
    all_sessions = load_sessions(args.data_dir)
    if not all_sessions:
        print("No resolved sessions found in: {}".format(args.data_dir))
        sys.exit(1)

    if args.recent is not None:
        sessions = all_sessions[-args.recent:]
    else:
        sessions = all_sessions

    print("Loaded {} resolved sessions from {}".format(len(sessions), args.data_dir))
    print("Execution: {} (slippage={})  Starting cash: ${}".format(
        args.execution, args.slippage, starting_cash,
    ))

    # Select strategies
    if args.strategy == "all":
        strategy_names = list(ALL_STRATEGIES.keys())
    else:
        if args.strategy not in ALL_STRATEGIES:
            print("Unknown strategy: {}. Available: {}".format(
                args.strategy, ", ".join(ALL_STRATEGIES.keys()),
            ))
            sys.exit(1)
        strategy_names = [args.strategy]

    sim = Simulator(
        starting_cash=starting_cash,
        execution_mode=args.execution,
        slippage=args.slippage,
        all_sessions=all_sessions,
    )

    # Build all strategy variants upfront
    all_variants = []  # type: list
    for name in strategy_names:
        cls = ALL_STRATEGIES[name]
        all_variants.extend(build_variants(cls))

    total_variants = len(all_variants)

    # Run grid search — parallelize when there are multiple variants
    if total_variants <= 1:
        all_results = [sim.run_backtest(v, sessions) for v in all_variants]
    else:
        num_workers = min(total_variants, os.cpu_count() or 1)
        print("Running {} variants across {} workers...".format(
            total_variants, num_workers,
        ))
        with ProcessPoolExecutor(
            max_workers=num_workers,
            initializer=_init_worker,
            initargs=(starting_cash, args.execution, args.slippage,
                      all_sessions, sessions),
        ) as pool:
            all_results = list(pool.map(_run_one, all_variants))

    print("Tested {} strategy variants across {} strategies".format(
        total_variants, len(strategy_names),
    ))

    # Output
    print_leaderboard(all_results, args.top, args.optimize)
    print_strategy_summary(all_results, args.optimize)

    if args.verbose:
        # Show per-session breakdown for the best overall result
        sort_key = {
            "sharpe": lambda r: r.sharpe,
            "total_pnl": lambda r: r.total_pnl,
            "win_rate": lambda r: r.win_rate,
        }.get(args.optimize, lambda r: r.sharpe)
        best = max(all_results, key=sort_key)
        print_verbose(best)

    # Persist full run to JSONL log
    log_backtest_run(args, sessions, all_results, args.optimize)
    print("Results logged to {}".format(BACKTEST_LOG_FILE))


if __name__ == "__main__":
    main()
