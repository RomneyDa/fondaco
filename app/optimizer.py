"""Live strategy optimizer.

Runs as a subprocess managed by AppManager. Periodically backtests
running strategies against recent session data and updates params
if improvements are found.
"""

import argparse
import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from core.defaults import BASE_DIR, DEFAULT_RATIO_MAX, MARKETS_DIR, OPTIMIZER_MAX_RATIO, market_sessions_dir
from simulator.engine import Simulator, BacktestResult, load_sessions, load_session_summaries, compute_kelly_sizing, KELLY_FRACTION
from simulator.backtest import build_variants, format_params
from bots import ALL_STRATEGIES

# Optimizer data directory
OPTIMIZER_DIR = os.path.join(BASE_DIR, "data", "optimizer")
OPTIMIZER_LOG_FILE = os.path.join(OPTIMIZER_DIR, "optimizer.log")
OPTIMIZER_EVENTS_FILE = os.path.join(OPTIMIZER_DIR, "events.jsonl")
OPTIMIZER_CONFIG_FILE = os.path.join(OPTIMIZER_DIR, "config.json")
OPTIMIZER_STATUS_FILE = os.path.join(OPTIMIZER_DIR, "status.json")

# Defaults
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_LOOKBACK_SESSIONS = 30
DEFAULT_MIN_SHARPE = 0.2
DEFAULT_STARTING_CASH = 100.0
DEFAULT_MAX_EDGE_ITERS = 2
MIN_SHARPE_IMPROVEMENT = 0.05  # require 0.05 Sharpe improvement to trigger update

logger = logging.getLogger("optimizer")


def _setup_logging():
    """Set up rotating log file for optimizer."""
    os.makedirs(OPTIMIZER_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        OPTIMIZER_LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)


def _log_event(event_type, **kwargs):
    """Append a structured event to events.jsonl with rotation."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **kwargs,
    }
    os.makedirs(OPTIMIZER_DIR, exist_ok=True)
    # Rotate if > 1MB
    try:
        if os.path.exists(OPTIMIZER_EVENTS_FILE):
            size = os.path.getsize(OPTIMIZER_EVENTS_FILE)
            if size > 1 * 1024 * 1024:
                rotated = OPTIMIZER_EVENTS_FILE + ".1"
                if os.path.exists(rotated):
                    os.remove(rotated)
                os.rename(OPTIMIZER_EVENTS_FILE, rotated)
    except OSError:
        pass
    try:
        with open(OPTIMIZER_EVENTS_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to write event: %s", e)


def _write_status(status):
    """Write current optimizer status to status.json."""
    os.makedirs(OPTIMIZER_DIR, exist_ok=True)
    tmp = OPTIMIZER_STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, OPTIMIZER_STATUS_FILE)
    except Exception as e:
        logger.warning("Failed to write status: %s", e)


def _load_config():
    """Load optimizer config, returning defaults if not found."""
    try:
        with open(OPTIMIZER_CONFIG_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(config):
    """Save optimizer config."""
    os.makedirs(OPTIMIZER_DIR, exist_ok=True)
    tmp = OPTIMIZER_CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, OPTIMIZER_CONFIG_FILE)


def _get_config_value(config, key, default):
    """Get config value with fallback to default."""
    return config.get(key, default)


def _fetch_status(port):
    """Fetch app status from control server."""
    import urllib.request
    import urllib.error
    url = "http://localhost:{}/api/control/status".format(port)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error("Failed to fetch status: %s", e)
        return None


def _control_request(port, endpoint, body=None):
    """Send a POST request to the control server."""
    import urllib.request
    import urllib.error
    url = "http://localhost:{}/api/control/{}".format(port, endpoint)
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error("Control request failed (%s): %s", endpoint, e)
        return None


def _detect_edge_params(best_params, grid):
    # type: (Dict[str, Any], Dict[str, list]) -> Dict[str, Any]
    """Detect which best params sit at the edge of their grid range.

    Returns a dict of {param_name: "low" | "high"} for params at boundaries.
    Only checks numeric params with >= 2 grid values.
    """
    edges = {}
    for key, values in grid.items():
        if key not in best_params:
            continue
        best_val = best_params[key]
        # Filter to numeric values only
        numeric_vals = [v for v in values if isinstance(v, (int, float)) and v is not None]
        if len(numeric_vals) < 2 or not isinstance(best_val, (int, float)):
            continue
        sorted_vals = sorted(numeric_vals)
        if best_val <= sorted_vals[0]:
            edges[key] = "low"
        elif best_val >= sorted_vals[-1]:
            edges[key] = "high"
    return edges


def _build_refined_grid(best_params, grid, edges):
    # type: (Dict[str, Any], Dict[str, list], Dict[str, Any]) -> Dict[str, list]
    """Build a refined grid extending beyond edge params.

    For each edge param, generates new values extending in the edge direction
    using the existing grid's step size. Non-edge params are pinned to best value.
    """
    refined = {}
    for key, values in grid.items():
        best_val = best_params.get(key)
        if key not in edges:
            # Pin non-edge params to best value
            refined[key] = [best_val]
            continue

        numeric_vals = sorted(v for v in values if isinstance(v, (int, float)) and v is not None)
        if len(numeric_vals) < 2:
            refined[key] = values
            continue

        # Compute step as median of consecutive differences
        diffs = [numeric_vals[i+1] - numeric_vals[i] for i in range(len(numeric_vals) - 1)]
        step = sorted(diffs)[len(diffs) // 2]
        if step <= 0:
            refined[key] = values
            continue

        direction = edges[key]
        is_int = all(isinstance(v, int) for v in numeric_vals)

        # Generate 5 points extending beyond the edge
        new_vals = []
        if direction == "low":
            # Extend below: 3 new points below, include best + 1 above
            for i in range(3, 0, -1):
                v = best_val - step * i
                new_vals.append(v)
            new_vals.append(best_val)
            new_vals.append(best_val + step)
        else:  # high
            # Extend above: 1 below, include best + 3 new above
            new_vals.append(best_val - step)
            new_vals.append(best_val)
            for i in range(1, 4):
                v = best_val + step * i
                new_vals.append(v)

        if is_int:
            new_vals = [int(round(v)) for v in new_vals]
        else:
            new_vals = [round(v, 6) for v in new_vals]

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for v in new_vals:
            if v not in seen:
                seen.add(v)
                deduped.append(v)
        refined[key] = deduped

    return refined


def _optimize_strategy(strategy_name, slug_key, current_params, current_cash,
                       session_cash_ratio, session_cap,
                       lookback, min_sharpe, starting_cash, port,
                       max_edge_iters=DEFAULT_MAX_EDGE_ITERS):
    """Run optimization for a single strategy. Returns (action, details) tuple."""

    _write_status({
        "state": "optimizing",
        "current_strategy": strategy_name,
        "current_market": slug_key,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    logger.info("[%s/%s] Starting optimization (lookback=%d, min_sharpe=%.2f)",
                slug_key, strategy_name, lookback, min_sharpe)
    _log_event("optimize_start", strategy=strategy_name, market=slug_key,
               current_params=current_params or {},
               lookback=lookback, min_sharpe=min_sharpe)

    if strategy_name not in ALL_STRATEGIES:
        logger.warning("[%s/%s] Strategy not in registry, skipping", slug_key, strategy_name)
        return "skip", {"reason": "not_in_registry"}

    strategy_cls = ALL_STRATEGIES[strategy_name]

    # Load sessions
    sessions_dir = market_sessions_dir(slug_key)
    sessions = load_sessions(sessions_dir, recent=lookback)
    if not sessions:
        logger.warning("[%s/%s] No sessions found", slug_key, strategy_name)
        return "skip", {"reason": "no_sessions"}

    if len(sessions) < 5:
        logger.warning("[%s/%s] Too few sessions (%d), need at least 5",
                       slug_key, strategy_name, len(sessions))
        return "skip", {"reason": "too_few_sessions", "count": len(sessions)}

    logger.info("[%s/%s] Using %d sessions for optimization",
                slug_key, strategy_name, len(sessions))

    # Load lightweight summaries for strategy lookback (no sample data)
    session_summaries = load_session_summaries(sessions_dir)

    # Step 1: Run baseline with current params
    try:
        baseline_strategy = strategy_cls(**current_params) if current_params else strategy_cls()
        baseline_strategy._params = current_params or {}
    except Exception as e:
        logger.error("[%s/%s] Failed to create baseline strategy: %s",
                     slug_key, strategy_name, e)
        return "skip", {"reason": "baseline_error", "error": str(e)}

    sim = Simulator(
        starting_cash=starting_cash,
        execution_mode="book",
        session_summaries=session_summaries,
        compound=False,  # Phase 1: flat cash for clean per-session returns
    )
    baseline_result = sim.run_backtest(baseline_strategy, sessions)

    baseline_info = {
        "sharpe": round(baseline_result.sharpe, 3),
        "pnl": round(baseline_result.total_pnl, 2),
        "win_rate": round(baseline_result.win_rate * 100, 1),
        "geo_return": round(baseline_result.geo_mean_return * 100, 4),
        "final_bankroll": round(baseline_result.final_bankroll, 2),
    }
    logger.info("[%s/%s] Baseline: sharpe=%.3f geo=%.4f%% pnl=%.2f win=%.1f%% final=$%.2f",
                slug_key, strategy_name,
                baseline_result.sharpe, baseline_result.geo_mean_return * 100,
                baseline_result.total_pnl,
                baseline_result.win_rate * 100, baseline_result.final_bankroll)
    _log_event("baseline", strategy=strategy_name, market=slug_key,
               params=current_params or {}, sessions_used=len(sessions),
               **baseline_info)

    # Step 2: Run full param grid (with edge refinement iterations)
    try:
        grid = strategy_cls().param_grid()
    except Exception as e:
        logger.error("[%s/%s] Failed to get param_grid: %s", slug_key, strategy_name, e)
        return "skip", {"reason": "variant_error", "error": str(e)}

    if not grid:
        logger.info("[%s/%s] No param_grid, checking baseline only",
                    slug_key, strategy_name)
        if baseline_result.sharpe < min_sharpe:
            return "disable", {
                "reason": "below_threshold",
                "sharpe": baseline_result.sharpe,
                "min_sharpe": min_sharpe,
                "baseline_pnl": baseline_result.total_pnl,
            }
        return "keep", {
            "sharpe": baseline_result.sharpe,
            "pnl": baseline_result.total_pnl,
        }

    total_variants_tested = 0
    best = None  # type: Optional[BacktestResult]
    current_grid = grid

    for edge_iter in range(max_edge_iters + 1):  # iter 0 = initial, 1..N = refinements
        iter_label = "initial" if edge_iter == 0 else "refine-{}".format(edge_iter)

        try:
            variants = build_variants(strategy_cls, grid_overrides=current_grid)
        except Exception as e:
            logger.error("[%s/%s] Failed to build variants (%s): %s",
                         slug_key, strategy_name, iter_label, e)
            if best is None:
                return "skip", {"reason": "variant_error", "error": str(e)}
            break

        if len(variants) <= 1 and edge_iter == 0:
            # Single variant from grid (all params pinned) — treat as no grid
            if baseline_result.sharpe < min_sharpe:
                return "disable", {
                    "reason": "below_threshold",
                    "sharpe": baseline_result.sharpe,
                    "min_sharpe": min_sharpe,
                    "baseline_pnl": baseline_result.total_pnl,
                }
            return "keep", {
                "sharpe": baseline_result.sharpe,
                "pnl": baseline_result.total_pnl,
            }

        # Cap variants to prevent runaway computation
        if len(variants) > 500:
            logger.warning("[%s/%s] %d variants (%s), capping to 500",
                           slug_key, strategy_name, len(variants), iter_label)
            variants = variants[:500]

        # Log grid summary
        grid_summary = {k: v if len(v) <= 5 else [v[0], "..({})..".format(len(v)), v[-1]]
                        for k, v in current_grid.items()} if current_grid else {}

        logger.info("[%s/%s] Testing %d variants (%s)...",
                    slug_key, strategy_name, len(variants), iter_label)
        _log_event("grid_search", strategy=strategy_name, market=slug_key,
                   variants=len(variants), grid=grid_summary, iteration=iter_label)

        # Run all variants
        results = []  # type: List[BacktestResult]
        for v in variants:
            try:
                r = sim.run_backtest(v, sessions)
                results.append(r)
            except Exception as e:
                logger.debug("[%s/%s] Variant failed: %s", slug_key, strategy_name, e)

        total_variants_tested += len(results)

        if not results:
            logger.warning("[%s/%s] All variants failed (%s)", slug_key, strategy_name, iter_label)
            if best is None:
                return "skip", {"reason": "all_variants_failed"}
            break

        iter_best = max(results, key=lambda r: r.sharpe)

        # Keep the overall best across iterations
        if best is None or iter_best.sharpe > best.sharpe:
            best = iter_best

        logger.info("[%s/%s] Best (%s): sharpe=%.3f geo=%.4f%% pnl=%.2f params=%s",
                    slug_key, strategy_name, iter_label,
                    iter_best.sharpe, iter_best.geo_mean_return * 100,
                    iter_best.total_pnl, format_params(iter_best.params))

        # Check for edge params — only refine if we have iterations left
        if edge_iter >= max_edge_iters:
            break

        edges = _detect_edge_params(iter_best.params, current_grid)
        if not edges:
            logger.info("[%s/%s] No edge params detected, skipping refinement",
                        slug_key, strategy_name)
            break

        logger.info("[%s/%s] Edge params detected: %s — refining grid",
                    slug_key, strategy_name,
                    ", ".join("{}={}({})".format(k, iter_best.params.get(k), d)
                              for k, d in edges.items()))
        _log_event("edge_refine", strategy=strategy_name, market=slug_key,
                   iteration=edge_iter + 1, edge_params=edges,
                   best_params=iter_best.params,
                   best_sharpe=round(iter_best.sharpe, 3))

        current_grid = _build_refined_grid(iter_best.params, current_grid, edges)

    # Log top 3 variants from final iteration for visibility
    sorted_results = sorted(results, key=lambda r: r.sharpe, reverse=True)
    top_variants = []
    for r in sorted_results[:3]:
        top_variants.append({
            "params": r.params,
            "sharpe": round(r.sharpe, 3),
            "geo_return": round(r.geo_mean_return * 100, 4),
            "pnl": round(r.total_pnl, 2),
            "win_rate": round(r.win_rate * 100, 1),
        })

    logger.info("[%s/%s] Best overall: sharpe=%.3f geo=%.4f%% pnl=%.2f win=%.1f%% params=%s "
                "(tested %d variants across %d iteration(s))",
                slug_key, strategy_name,
                best.sharpe, best.geo_mean_return * 100, best.total_pnl,
                best.win_rate * 100,
                format_params(best.params),
                total_variants_tested, min(edge_iter + 1, max_edge_iters + 1))
    _log_event("grid_result", strategy=strategy_name, market=slug_key,
               variants_tested=total_variants_tested, top_variants=top_variants,
               best_params=format_params(best.params),
               best_sharpe=round(best.sharpe, 3),
               best_geo_return=round(best.geo_mean_return * 100, 4),
               baseline_sharpe=round(baseline_result.sharpe, 3),
               edge_iterations=min(edge_iter + 1, max_edge_iters + 1))

    # Step 3: Decide action
    # Optimize for Sharpe (best risk-adjusted edge), then size with Kelly (cash_pct)
    if best.sharpe < min_sharpe:
        logger.info("[%s/%s] Best sharpe %.3f < min %.2f -> disabling",
                    slug_key, strategy_name, best.sharpe, min_sharpe)
        return "disable", {
            "reason": "below_threshold",
            "best_sharpe": best.sharpe,
            "min_sharpe": min_sharpe,
            "best_params": best.params,
            "best_pnl": best.total_pnl,
            "baseline_sharpe": baseline_result.sharpe,
            "baseline_pnl": baseline_result.total_pnl,
        }

    # Check if improvement over baseline
    improved = best.sharpe > baseline_result.sharpe + MIN_SHARPE_IMPROVEMENT
    # Strip cash_pct from comparison since it's not a strategy param
    best_strategy_params = {k: v for k, v in best.params.items() if k != "cash_pct"}
    params_changed = best_strategy_params != (current_params or {})

    if improved and params_changed:
        # Phase 2: compute Kelly sizing from raw session returns
        kelly = compute_kelly_sizing(best.sessions, starting_cash, kelly_multiplier=KELLY_FRACTION)
        best_cash_pct = kelly["cash_pct"]

        new_ratio = None
        if best_cash_pct > 0:
            ratio_val = min(round(best_cash_pct, 2), OPTIMIZER_MAX_RATIO)
            current_max = session_cash_ratio.get("max", DEFAULT_RATIO_MAX) if session_cash_ratio else DEFAULT_RATIO_MAX
            new_ratio = {"ratio": ratio_val, "max": current_max}

        strategy_params = best.params

        logger.info("[%s/%s] Improved! sharpe %.3f -> %.3f, updating params (cash_pct=%.2f)",
                    slug_key, strategy_name,
                    baseline_result.sharpe, best.sharpe,
                    best_cash_pct or 0)
        return "update", {
            "new_params": strategy_params,
            "new_cash_pct": best_cash_pct,
            "new_sharpe": best.sharpe,
            "new_pnl": best.total_pnl,
            "new_win_rate": best.win_rate,
            "new_geo_return": best.geo_mean_return,
            "new_dd_ratio": best.dd_ratio,
            "new_ratio": new_ratio,
            "baseline_sharpe": baseline_result.sharpe,
            "baseline_pnl": baseline_result.total_pnl,
            "baseline_params": current_params or {},
            "variants_tested": len(results),
            "sessions_used": len(sessions),
        }
    else:
        logger.info("[%s/%s] No significant improvement (sharpe %.3f vs %.3f), keeping current",
                    slug_key, strategy_name, best.sharpe, baseline_result.sharpe)
        return "keep", {
            "best_sharpe": best.sharpe,
            "baseline_sharpe": baseline_result.sharpe,
            "best_params": best.params,
            "baseline_pnl": baseline_result.total_pnl,
        }


def _apply_update(strategy_name, slug_key, slug_prefix, details, current_cash, port):
    """Apply an optimization update: reload with new params and optionally update cash ratio."""
    new_params = details["new_params"]
    new_ratio = details.get("new_ratio")

    # Update session-cash-ratio if recommended (never change bankroll amount)
    if new_ratio:
        logger.info("[%s/%s] Updating session-cash-ratio to %.2f:%.2f",
                    slug_key, strategy_name, new_ratio["ratio"], new_ratio["max"])
        resp = _control_request(port, "set-cash", {
            "slug_prefix": slug_prefix,
            "strategy": strategy_name,
            "session_cash_ratio": new_ratio,
        })
        if resp and not resp.get("ok"):
            logger.error("[%s/%s] set-cash failed: %s", slug_key, strategy_name, resp.get("error"))

    # Reload strategy with new params (preserve existing cash/session settings)
    logger.info("[%s/%s] Reloading with new params: %s",
                slug_key, strategy_name, format_params(new_params))

    resp = _control_request(port, "trade-on", {
        "slug_prefix": slug_prefix,
        "strategy": strategy_name,
        "params": {k: str(v) if not isinstance(v, (int, float, bool)) else v
                   for k, v in new_params.items()},
        "cash": current_cash,
        **({"session_cash_ratio": new_ratio} if new_ratio else {}),
    })
    if resp and resp.get("ok"):
        logger.info("[%s/%s] Successfully reloaded with new params", slug_key, strategy_name)
        return True
    else:
        logger.error("[%s/%s] Reload failed: %s", slug_key, strategy_name,
                     resp.get("error") if resp else "no response")
        return False


def _apply_disable(strategy_name, slug_key, slug_prefix, details, port):
    """Disable a strategy that's below the Sharpe threshold."""
    logger.info("[%s/%s] Disabling strategy (sharpe=%.3f < min=%.2f)",
                slug_key, strategy_name,
                details.get("best_sharpe", details.get("sharpe", 0)),
                details.get("min_sharpe", 0))

    resp = _control_request(port, "trade-off", {
        "slug_prefix": slug_prefix,
        "strategy": strategy_name,
    })
    if resp and resp.get("ok"):
        logger.info("[%s/%s] Successfully disabled", slug_key, strategy_name)
        return True
    else:
        logger.error("[%s/%s] Disable failed: %s", slug_key, strategy_name,
                     resp.get("error") if resp else "no response")
        return False


def run_optimizer(port, interval_minutes, lookback, min_sharpe, starting_cash,
                  max_edge_iters=DEFAULT_MAX_EDGE_ITERS):
    """Main optimizer loop."""
    _setup_logging()
    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        logger.info("Shutdown signal received.")
        running = False

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Optimizer started (port=%d, interval=%dm, lookback=%d, min_sharpe=%.2f, "
                "cash=%.0f, max_edge_iters=%d)",
                port, interval_minutes, lookback, min_sharpe, starting_cash, max_edge_iters)
    _log_event("started", interval_minutes=interval_minutes, lookback=lookback,
               min_sharpe=min_sharpe, starting_cash=starting_cash,
               max_edge_iters=max_edge_iters)

    _write_status({
        "state": "running",
        "pid": os.getpid(),
        "interval_minutes": interval_minutes,
        "lookback": lookback,
        "min_sharpe": min_sharpe,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_run": None,
        "next_run": datetime.now(timezone.utc).isoformat(),
    })

    while running:
        # Reload config each cycle (allows live config changes)
        config = _load_config()
        interval_minutes = _get_config_value(config, "interval_minutes", interval_minutes)
        lookback = _get_config_value(config, "lookback", lookback)
        min_sharpe = _get_config_value(config, "min_sharpe", min_sharpe)
        max_edge_iters = _get_config_value(config, "max_edge_iters", max_edge_iters)

        cycle_start = datetime.now(timezone.utc)
        logger.info("=== Optimization cycle starting ===")
        _log_event("cycle_start")

        _write_status({
            "state": "scanning",
            "pid": os.getpid(),
            "interval_minutes": interval_minutes,
            "lookback": lookback,
            "min_sharpe": min_sharpe,
            "started_at": cycle_start.isoformat(),
            "cycle_started_at": cycle_start.isoformat(),
        })

        # Fetch current app status to find running strategies
        status = _fetch_status(port)
        if status is None or not status.get("running"):
            logger.warning("App not running or unreachable, sleeping...")
            _write_status({
                "state": "waiting",
                "pid": os.getpid(),
                "interval_minutes": interval_minutes,
                "lookback": lookback,
                "min_sharpe": min_sharpe,
                "error": "app_not_running",
            })
            _sleep_interruptible(60, lambda: running)
            continue

        markets = status.get("markets", [])
        strategies_optimized = 0
        results_summary = []

        for market in markets:
            slug_key = market.get("slug_key", "")
            slug_prefix = market.get("slug_prefix", slug_key + "-")
            strategies = market.get("strategies", [])

            for strat in strategies:
                if not running:
                    break
                if not strat.get("alive", True):
                    continue  # Skip dead workers

                name = strat["name"]
                raw_params = strat.get("params", {})
                params = {k: (None if v == "None" else v) for k, v in raw_params.items()}
                cash = strat.get("starting_cash", strat.get("cash", starting_cash))
                scr = strat.get("session_cash_ratio")
                sc = strat.get("session_cap")

                try:
                    action, details = _optimize_strategy(
                        name, slug_key, params, cash,
                        scr, sc, lookback, min_sharpe, starting_cash, port,
                        max_edge_iters=max_edge_iters,
                    )
                except Exception as e:
                    logger.exception("[%s/%s] Optimizer crashed", slug_key, name)
                    _log_event("error", strategy=name, market=slug_key,
                               error=str(e))
                    continue

                result_entry = {
                    "strategy": name,
                    "market": slug_key,
                    "action": action,
                    **details,
                }
                results_summary.append(result_entry)

                if action == "update":
                    success = _apply_update(name, slug_key, slug_prefix, details, cash, port)
                    _log_event("update", strategy=name, market=slug_key,
                               success=success, **details)
                elif action == "disable":
                    success = _apply_disable(name, slug_key, slug_prefix, details, port)
                    _log_event("disable", strategy=name, market=slug_key,
                               success=success, **details)
                elif action == "keep":
                    _log_event("keep", strategy=name, market=slug_key, **details)
                elif action == "skip":
                    _log_event("skip", strategy=name, market=slug_key, **details)

                strategies_optimized += 1

        cycle_end = datetime.now(timezone.utc)
        duration = (cycle_end - cycle_start).total_seconds()
        logger.info("=== Cycle complete: %d strategies in %.1fs ===",
                    strategies_optimized, duration)
        _log_event("cycle_end", strategies_optimized=strategies_optimized,
                   duration_seconds=round(duration, 1), results=results_summary)

        next_run = datetime.fromtimestamp(
            cycle_end.timestamp() + interval_minutes * 60, tz=timezone.utc
        )
        _write_status({
            "state": "idle",
            "pid": os.getpid(),
            "interval_minutes": interval_minutes,
            "lookback": lookback,
            "min_sharpe": min_sharpe,
            "last_run": cycle_end.isoformat(),
            "last_duration_seconds": round(duration, 1),
            "last_results": results_summary,
            "next_run": next_run.isoformat(),
            "strategies_optimized": strategies_optimized,
        })

        # Sleep until next cycle
        _sleep_interruptible(interval_minutes * 60, lambda: running)

    logger.info("Optimizer stopped.")
    _log_event("stopped")
    _write_status({"state": "stopped", "pid": os.getpid()})


def _sleep_interruptible(seconds, check_fn, interval=5):
    """Sleep for `seconds`, checking `check_fn` every `interval` seconds."""
    elapsed = 0.0
    while elapsed < seconds and check_fn():
        sleep_time = min(interval, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time


def main():
    parser = argparse.ArgumentParser(description="Live strategy optimizer")
    parser.add_argument("--port", type=int, required=True, help="Control server port")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help="Minutes between optimization cycles (default: {})".format(DEFAULT_INTERVAL_MINUTES))
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_SESSIONS,
                        help="Number of recent sessions to backtest (default: {})".format(DEFAULT_LOOKBACK_SESSIONS))
    parser.add_argument("--min-sharpe", type=float, default=DEFAULT_MIN_SHARPE,
                        help="Minimum Sharpe ratio to keep a strategy (default: {})".format(DEFAULT_MIN_SHARPE))
    parser.add_argument("--cash", type=float, default=DEFAULT_STARTING_CASH,
                        help="Starting cash for backtests (default: {})".format(DEFAULT_STARTING_CASH))
    parser.add_argument("--max-edge-iters", type=int, default=DEFAULT_MAX_EDGE_ITERS,
                        help="Max edge refinement iterations (default: {})".format(DEFAULT_MAX_EDGE_ITERS))

    args = parser.parse_args()

    # Save initial config
    _save_config({
        "interval_minutes": args.interval,
        "lookback": args.lookback,
        "min_sharpe": args.min_sharpe,
        "starting_cash": args.cash,
        "max_edge_iters": args.max_edge_iters,
    })

    run_optimizer(args.port, args.interval, args.lookback, args.min_sharpe, args.cash,
                  max_edge_iters=args.max_edge_iters)


if __name__ == "__main__":
    main()
