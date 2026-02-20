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
import sys

from simulator import Simulator, BacktestResult, load_sessions
from strategies import ALL_STRATEGIES


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
        trade_str = ""
        if sr.trades:
            t = sr.trades[0]
            trade_str = " | {}@{:.3f}".format(t.side, t.price)
        print("  {} outcome={:<4} pnl={:>+7.2f}{}".format(
            sr.session_file.split("/")[-1] if sr.session_file else "?",
            sr.outcome,
            sr.pnl,
            trade_str,
        ))
    print("  TOTAL: pnl={:+.2f}  mean={:+.2f}  win_rate={:.1f}%  sharpe={:.3f}".format(
        result.total_pnl, result.mean_pnl, result.win_rate * 100, result.sharpe,
    ))


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
        default="midpoint_slippage",
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
        type=float,
        default=100.0,
        help="Starting cash per session (default: 100)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show top N parameter combos (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Per-session breakdown for best params",
    )
    args = parser.parse_args()

    # Load sessions
    sessions = load_sessions(args.data_dir)
    if not sessions:
        print("No resolved sessions found in: {}".format(args.data_dir))
        sys.exit(1)

    print("Loaded {} resolved sessions from {}".format(len(sessions), args.data_dir))
    print("Execution: {} (slippage={})  Starting cash: ${}".format(
        args.execution, args.slippage, args.cash,
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
        starting_cash=args.cash,
        execution_mode=args.execution,
        slippage=args.slippage,
    )

    # Run grid search
    all_results = []  # type: list
    total_variants = 0

    for name in strategy_names:
        cls = ALL_STRATEGIES[name]
        variants = build_variants(cls)
        total_variants += len(variants)
        for strat in variants:
            result = sim.run_backtest(strat, sessions)
            all_results.append(result)

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


if __name__ == "__main__":
    main()
