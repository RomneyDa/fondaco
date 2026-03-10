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
from core.defaults import BALANCE_CACHE_FILE, DEFAULT_RATIO_MAX, MARKETS_DIR, TAKER_FEE_BASE_BPS, market_backtest_log, market_sessions_dir
from bots import ALL_STRATEGIES


# -- Process pool worker globals & functions --
_worker_sim = None   # type: Optional[Simulator]
_worker_sessions = None  # type: Optional[list]


def _init_worker(starting_cash, execution_mode, slippage, all_sessions, sessions, compound, fee_bps):
    # type: (float, str, float, list, list, bool, int) -> None
    """Initializer for each worker process — sets up Simulator and sessions once."""
    global _worker_sim, _worker_sessions
    _worker_sim = Simulator(
        starting_cash=starting_cash,
        execution_mode=execution_mode,
        slippage=slippage,
        all_sessions=all_sessions,
        compound=compound,
        fee_bps=fee_bps,
    )
    _worker_sessions = sessions


def _run_one(strategy):
    """Run a single strategy variant in a worker process."""
    return _worker_sim.run_backtest(strategy, _worker_sessions)


def parse_param_overrides(param_args):
    # type: (list,) -> dict
    """Parse --param KEY=v1,v2,... into {key: [values]}."""
    overrides = {}  # type: dict
    for arg in param_args:
        if "=" not in arg:
            print("Invalid --param format: '{}'. Expected KEY=v1,v2,...".format(arg))
            sys.exit(1)
        key, vals_str = arg.split("=", 1)
        values = []
        for v in vals_str.split(","):
            v = v.strip()
            if v.lower() == "none":
                values.append(None)
            elif v.lower() == "true":
                values.append(True)
            elif v.lower() == "false":
                values.append(False)
            else:
                try:
                    values.append(int(v))
                except ValueError:
                    try:
                        values.append(float(v))
                    except ValueError:
                        values.append(v)
        overrides[key] = values
    return overrides


def build_variants(strategy_cls, grid_overrides=None):
    # type: (type, Optional[dict]) -> list
    """Build all parameter combinations from a strategy's param_grid."""
    grid = strategy_cls().param_grid()

    if grid_overrides:
        # Validate override keys exist in grid or constructor
        import inspect
        sig_params = set(inspect.signature(strategy_cls.__init__).parameters.keys()) - {"self"}
        for key in grid_overrides:
            if key not in sig_params:
                print("Unknown param '{}' for {}. Available: {}".format(
                    key, strategy_cls.__name__, ", ".join(sorted(sig_params)),
                ))
                sys.exit(1)
        # Start from existing grid, apply overrides
        if not grid:
            grid = {}
        grid.update(grid_overrides)

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


def print_leaderboard(results, top_n, optimize, compound=False):
    # type: (list, int, str, bool) -> None
    """Print a sorted leaderboard of backtest results."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
        "kelly_growth": lambda r: r.geo_mean_return,
    }.get(optimize, lambda r: r.sharpe)

    results.sort(key=sort_key, reverse=True)
    shown = results[:top_n]

    if compound:
        # Kelly-aware header with geometric growth metrics
        print("\n{:<20} {:>8} {:>8} {:>8} {:>10} {:>8} {:>8} {:>6}  {}".format(
            "Strategy", "GeoRet%", "Kelly_f", "MaxDD%", "Final$", "Fees", "Win%", "N", "Params"
        ))
        print("-" * 115)
        for r in shown:
            print("{:<20} {:>8.3f} {:>8.3f} {:>8.1f} {:>10.2f} {:>8.2f} {:>7.1f}% {:>6}  {}".format(
                r.strategy_name,
                r.geo_mean_return * 100,
                r.kelly_fraction,
                r.max_drawdown * 100,
                r.final_bankroll,
                r.total_fees,
                r.win_rate * 100,
                r.num_sessions,
                format_params(r.params),
            ))
    else:
        # Standard header + Kelly columns
        print("\n{:<20} {:>8} {:>10} {:>10} {:>8} {:>8} {:>8} {:>6}  {}".format(
            "Strategy", "Sharpe", "Total PnL", "Mean PnL", "Fees", "GeoRet%", "Win%", "N", "Params"
        ))
        print("-" * 115)
        for r in shown:
            print("{:<20} {:>8.3f} {:>10.2f} {:>10.2f} {:>8.2f} {:>8.3f} {:>7.1f}% {:>6}  {}".format(
                r.strategy_name,
                r.sharpe,
                r.total_pnl,
                r.mean_pnl,
                r.total_fees,
                r.geo_mean_return * 100,
                r.win_rate * 100,
                r.num_sessions,
                format_params(r.params),
            ))

    print()


def print_strategy_summary(results, optimize, compound=False, starting_cash=100.0):
    # type: (list, str, bool, float) -> None
    """Print best params per strategy."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
        "kelly_growth": lambda r: r.geo_mean_return,
    }.get(optimize, lambda r: r.sharpe)

    # Group by strategy name
    by_strategy = {}  # type: dict
    for r in results:
        by_strategy.setdefault(r.strategy_name, []).append(r)

    print("=" * 70)
    print("BEST PARAMS PER STRATEGY (optimizing: {}{})".format(
        optimize, ", compound" if compound else ""))
    print("=" * 70)

    for name in sorted(by_strategy.keys()):
        variants = by_strategy[name]
        best = max(variants, key=sort_key)
        print("\n  {} ({} variants tested)".format(name, len(variants)))
        print("    Best:  Sharpe={:.3f}  PnL={:.2f}  Fees={:.2f}  Win={:.1f}%  GeoRet={:.3f}%  MaxDD={:.1f}%".format(
            best.sharpe, best.total_pnl, best.total_fees, best.win_rate * 100,
            best.geo_mean_return * 100, best.max_drawdown * 100,
        ))
        if compound:
            print("    Kelly: f*={:.3f}  Final=${:.2f}  LogGrowth={:.3f}".format(
                best.kelly_fraction, best.final_bankroll, best.log_growth,
            ))
        # Recommended session-cash-ratio
        if best.dd_ratio > 0:
            r = best.dd_ratio
            print("    Recommended: --session-cash-ratio {:.2f}:{}  (keeps drawdown <25%, Kelly f*={:.2f})".format(
                r, DEFAULT_RATIO_MAX, best.kelly_fraction,
            ))
        elif best.kelly_fraction > 0:
            half_kelly = best.kelly_fraction / 2.0
            print("    Recommended: --session-cash-ratio {:.2f}:{}  (half-Kelly)".format(
                half_kelly, DEFAULT_RATIO_MAX,
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
            fee_str = " fee={:.2f}".format(t.fee) if t.fee else ""
            trade_parts.append("{}@{:.3f}{}".format(t.side, t.price, fee_str))
        trade_str = " | " + ", ".join(trade_parts) if trade_parts else ""
        fee_str = " fees={:.2f}".format(sr.total_fees) if sr.total_fees > 0 else ""
        print("  {} outcome={:<4} pnl={:>+7.2f}{} trades={:<2}{}".format(
            sr.session_file.split("/")[-1] if sr.session_file else "?",
            sr.outcome,
            sr.pnl,
            fee_str,
            len(sr.trades),
            trade_str,
        ))
    print("  TOTAL: pnl={:+.2f}  mean={:+.2f}  win_rate={:.1f}%  sharpe={:.3f}".format(
        result.total_pnl, result.mean_pnl, result.win_rate * 100, result.sharpe,
    ))


def _resolve_data_dir(raw_path):
    # type: (str) -> str
    """Resolve data_dir: if it looks like a slug key, expand to market sessions dir."""
    # If path exists as-is, use it
    if os.path.isdir(raw_path):
        return raw_path
    # Try as slug key -> data/markets/{slug_key}/sessions
    slug_key = raw_path.rstrip("-")
    market_path = market_sessions_dir(slug_key)
    if os.path.isdir(market_path):
        return market_path
    # Return as-is (will fail with a clear error later)
    return raw_path


def _resolve_backtest_log(data_dir):
    # type: (str) -> str
    """Determine backtest log path based on data_dir location."""
    # If data_dir is under data/markets/{slug}/sessions, use per-market log
    markets_dir = os.path.normpath(MARKETS_DIR)
    norm_dir = os.path.normpath(data_dir)
    if norm_dir.startswith(markets_dir + os.sep):
        # Extract slug_key from path
        rel = os.path.relpath(norm_dir, markets_dir)
        slug_key = rel.split(os.sep)[0]
        return market_backtest_log(slug_key)
    # Fallback: global backtest log (shouldn't happen with new layout)
    from core.defaults import BASE_DIR
    return os.path.join(BASE_DIR, "data", "backtest_log.jsonl")


def log_backtest_run(args, sessions, all_results, optimize, backtest_log_file):
    # type: (argparse.Namespace, list, list, str, str) -> None
    """Append a JSONL entry with the full backtest run inputs and results."""
    sort_key = {
        "sharpe": lambda r: r.sharpe,
        "total_pnl": lambda r: r.total_pnl,
        "win_rate": lambda r: r.win_rate,
        "kelly_growth": lambda r: r.geo_mean_return,
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
            "geo_mean_return": round(best.geo_mean_return, 6),
            "kelly_fraction": round(best.kelly_fraction, 4),
            "dd_ratio": round(best.dd_ratio, 4),
            "max_drawdown": round(best.max_drawdown, 4),
            "final_bankroll": round(best.final_bankroll, 4),
            "total_fees": round(best.total_fees, 4),
        })

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "data_dir": args.data_dir,
            "strategy": args.strategy,
            "execution": args.execution,
            "slippage": args.slippage,
            "starting_cash": args.cash,
            "compound": getattr(args, "compound", False),
            "optimize": optimize,
            "fee_bps": getattr(args, "fee_bps", TAKER_FEE_BASE_BPS),
            "num_sessions": len(sessions),
            "total_variants": len(all_results),
        },
        "best_per_strategy": best_per_strategy,
    }

    os.makedirs(os.path.dirname(backtest_log_file), exist_ok=True)
    try:
        with open(backtest_log_file, "a") as f:
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
        choices=["sharpe", "total_pnl", "win_rate", "kelly_growth"],
        help="Metric to optimize (default: sharpe). kelly_growth = geometric mean return",
    )
    parser.add_argument(
        "--compound",
        action="store_true",
        help="Compound bankroll across sessions (Kelly-realistic). PnL carries forward.",
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
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=v1,v2,...",
        help="Override param grid values (repeatable). e.g. --param min_elapsed=0.4,0.5,0.6",
    )
    parser.add_argument(
        "--fee-bps",
        type=int,
        default=TAKER_FEE_BASE_BPS,
        help="CLOB taker fee base rate in bps (default: {}). 0 to disable.".format(TAKER_FEE_BASE_BPS),
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

    # Resolve data dir (supports slug key shorthand)
    args.data_dir = _resolve_data_dir(args.data_dir)
    backtest_log_file = _resolve_backtest_log(args.data_dir)

    # Load sessions
    all_sessions = load_sessions(args.data_dir)
    if not all_sessions:
        print("No resolved sessions found in: {}".format(args.data_dir))
        sys.exit(1)

    if args.recent is not None:
        sessions = all_sessions[-args.recent:]
    else:
        sessions = all_sessions

    fee_bps = args.fee_bps
    print("Loaded {} resolved sessions from {}".format(len(sessions), args.data_dir))
    print("Execution: {} (slippage={})  Starting cash: ${}{}  Fee: {} bps".format(
        args.execution, args.slippage, starting_cash,
        "  [COMPOUND]" if args.compound else "",
        fee_bps,
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
        compound=args.compound,
        fee_bps=fee_bps,
    )

    # Parse --param overrides
    grid_overrides = parse_param_overrides(args.param) if args.param else None

    # Build all strategy variants upfront
    all_variants = []  # type: list
    for name in strategy_names:
        cls = ALL_STRATEGIES[name]
        all_variants.extend(build_variants(cls, grid_overrides))

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
                      all_sessions, sessions, args.compound, fee_bps),
        ) as pool:
            all_results = list(pool.map(_run_one, all_variants))

    print("Tested {} strategy variants across {} strategies".format(
        total_variants, len(strategy_names),
    ))

    # Output
    print_leaderboard(all_results, args.top, args.optimize, compound=args.compound)
    print_strategy_summary(all_results, args.optimize, compound=args.compound, starting_cash=starting_cash)

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
    log_backtest_run(args, sessions, all_results, args.optimize, backtest_log_file)
    print("Results logged to {}".format(backtest_log_file))


if __name__ == "__main__":
    main()
