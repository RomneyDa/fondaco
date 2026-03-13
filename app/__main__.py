#!/usr/bin/env python3
"""
Multi-Market Polymarket App

Central app managing multiple markets. Each market has its own data stream
and bot workers.

Usage:
    uv run python -m app start
    uv run python -m app start --restore
    uv run python -m app market-on btc-updown-5m-
    uv run python -m app trade-on btc-updown-5m- --strategy LateSniper --cash 100
    uv run python -m app trade-off btc-updown-5m- --strategy LateSniper
    uv run python -m app status
    uv run python -m app stop
    uv run python -m app claim [--no-wait]
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

from core.defaults import (
    APP_LOG_FILE,
    APP_STATE_FILE,
    BASE_DIR,
    DEFAULT_CONTROL_PORT,
)
from bots import ALL_STRATEGIES

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

from core.logging_setup import setup_logging
logger = setup_logging("app", log_file=APP_LOG_FILE)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def _write_state(pid, port):
    # type: (int, int) -> None
    # Preserve existing markets list so --restore can read it back
    existing_markets = []
    if os.path.exists(APP_STATE_FILE):
        try:
            with open(APP_STATE_FILE, "r") as f:
                existing_markets = json.load(f).get("markets", [])
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    os.makedirs(os.path.dirname(APP_STATE_FILE), exist_ok=True)
    with open(APP_STATE_FILE, "w") as f:
        json.dump({"pid": pid, "port": port, "markets": existing_markets}, f, indent=2)


def _read_state():
    # type: () -> Optional[Dict[str, Any]]
    if not os.path.exists(APP_STATE_FILE):
        return None
    try:
        with open(APP_STATE_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid")
        if pid and _pid_alive(pid):
            return data
        # PID is stale — leave the file so _write_state can preserve
        # the markets list for --restore.
        return None
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def _remove_state():
    # type: () -> None
    """Clear PID/port but preserve markets list so --restore works."""
    try:
        with open(APP_STATE_FILE, "r") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return
    # Keep markets, clear runtime info so _read_state sees it as stale
    state.pop("pid", None)
    state.pop("port", None)
    try:
        with open(APP_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def _pid_alive(pid):
    # type: (int) -> bool
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_params(params_str):
    # type: (str) -> Dict[str, Any]
    params = {}  # type: Dict[str, Any]
    if not params_str:
        return params
    for pair in params_str.split(","):
        k, v = pair.split("=", 1)
        v = v.strip()
        if v == "None":
            params[k.strip()] = None
            continue
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        params[k.strip()] = v
    return params


def _http_request(method, path, body=None):
    # type: (str, str, Optional[Dict[str, Any]]) -> Dict[str, Any]
    state = _read_state()
    if state is None:
        print("App is not running.")
        sys.exit(1)

    port = state["port"]
    import urllib.request
    import urllib.error

    url = "http://localhost:{0}{1}".format(port, path)
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if body else {}

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print("Failed to connect to control server on port {}: {}".format(port, e))
        sys.exit(1)


def _check_resp(resp, success_msg):
    # type: (Dict[str, Any], str) -> None
    """Print success/error from a control server response, exit on failure."""
    if resp.get("ok"):
        print(success_msg)
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_start(args):
    """Start the central app (no markets by default)."""
    from app.app_manager import AppManager
    from app.server import ControlServer

    state = _read_state()
    if state is not None:
        print("App already running (PID {}). Use 'uv run python -m app stop'.".format(
            state["pid"],
        ))
        sys.exit(1)

    port = args.port
    mgr = AppManager(port=port, verbose_api_log=getattr(args, "verbose", False))

    server = ControlServer(mgr, port)
    server.start()

    _write_state(os.getpid(), port)

    if getattr(args, "restore", False):
        mgr.restore()

    try:
        mgr.run()
    finally:
        server.stop()
        _remove_state()


def cmd_market_on(args):
    """Start data collection for a market."""
    body = {
        "slug_prefix": args.slug_prefix,
    }  # type: Dict[str, Any]
    if args.window is not None:
        body["window"] = args.window
    if getattr(args, "market_type", None):
        body["market_type"] = args.market_type
    if getattr(args, "restore", False):
        body["restore"] = True

    resp = _http_request("POST", "/api/control/market-on", body)
    _check_resp(resp, "Market started: {}".format(resp.get("slug_key", args.slug_prefix)))


def cmd_market_off(args):
    """Stop data collection for a market."""
    body = {
        "slug_prefix": args.slug_prefix,
    }  # type: Dict[str, Any]
    if getattr(args, "after_session", False):
        body["after_session"] = True

    resp = _http_request("POST", "/api/control/market-off", body)
    _check_resp(resp, "Market stopped: {}".format(args.slug_prefix))


def cmd_trade_on(args):
    """Enable trading on a running market."""
    body = {
        "slug_prefix": args.slug_prefix,
        "strategy": args.strategy,
        "params": _parse_params(getattr(args, "params", "") or ""),
    }  # type: Dict[str, Any]
    if getattr(args, "cash", None):
        cash_str = args.cash
        if cash_str == "cached":
            from app.market_runner import _load_balance_cache
            cached = _load_balance_cache()
            if cached:
                body["cash"] = cached["cash"]
            else:
                print("No cached balance found.")
                sys.exit(1)
        else:
            body["cash"] = float(cash_str)
    if getattr(args, "dry_run", False):
        body["dry_run"] = True
    if getattr(args, "per_session", False):
        body["per_session"] = True
    session_cap = getattr(args, "session_cap", None)
    if session_cap is not None:
        body["session_cap"] = session_cap
    session_cash_ratio_str = getattr(args, "session_cash_ratio", None)
    if session_cash_ratio_str is not None:
        parts = session_cash_ratio_str.split(":")
        body["session_cash_ratio"] = {"ratio": float(parts[0]), "max": float(parts[1])}

    resp = _http_request("POST", "/api/control/trade-on", body)
    _check_resp(resp, "Trading enabled: strategy={} on {}".format(
        resp.get("strategy"), resp.get("slug_prefix")))


def cmd_trade_off(args):
    """Disable trading on a running market."""
    body = {"slug_prefix": args.slug_prefix}  # type: Dict[str, Any]
    strategy = getattr(args, "strategy", None)
    if strategy:
        body["strategy"] = strategy

    resp = _http_request("POST", "/api/control/trade-off", body)
    msg = "Trading disabled for {} on {}.".format(strategy, args.slug_prefix) if strategy \
        else "All trading disabled on {}.".format(args.slug_prefix)
    _check_resp(resp, msg)


def cmd_set_cash(args):
    """Update cash on a running market."""
    body = {
        "slug_prefix": args.slug_prefix,
        "cash": float(args.amount),
    }  # type: Dict[str, Any]
    strategy = getattr(args, "strategy", None)
    if strategy:
        body["strategy"] = strategy
    session_cash_ratio_str = getattr(args, "session_cash_ratio", None)
    if session_cash_ratio_str is not None:
        parts = session_cash_ratio_str.split(":")
        body["session_cash_ratio"] = {"ratio": float(parts[0]), "max": float(parts[1])}
    session_cap = getattr(args, "session_cap", None)
    if session_cap is not None:
        body["session_cap"] = session_cap
    if getattr(args, "clear_session_limit", False):
        body["clear_session_limit"] = True
    resp = _http_request("POST", "/api/control/set-cash", body)
    _check_resp(resp, "Cash updated: ${}".format(resp.get("cash")))


def cmd_reload(args):
    """Reload (restart) a worker to pick up code changes."""
    body = {
        "slug_prefix": args.slug_prefix,
        "strategy": args.strategy,
    }
    resp = _http_request("POST", "/api/control/reload", body)
    _check_resp(resp, "Reloaded worker: {} on {}".format(args.strategy, args.slug_prefix))


def cmd_kill(args):
    """Signal the app to stop all markets after their current sessions finish."""
    resp = _http_request("POST", "/api/control/kill")
    _check_resp(resp, "Kill signal sent — all markets will stop after their current sessions.")


def cmd_status(args):
    """Check app status."""
    state = _read_state()
    if state is None:
        print("App is not running.")
        return

    resp = _http_request("GET", "/api/control/status")
    print("App running (PID {})".format(state["pid"]))

    markets = resp.get("markets", [])
    if not markets:
        print("  No markets active.")
    else:
        print("  Markets: {}".format(len(markets)))
        for m in markets:
            slug = m.get("slug_prefix", m.get("slug_key", "?"))
            session_count = m.get("session_count", 0)
            tick_clients = m.get("tick_clients", 0)
            stop_tag = ""
            if m.get("stop_after_session"):
                stop_tag = " [STOPPING]"

            col_pid = m.get("collector_pid")
            col_tag = " collector=PID {}".format(col_pid) if col_pid else " [NO COLLECTOR]"

            strategies = m.get("strategies", [])
            print("  {} | sessions={} | tick_clients={}{} |{}".format(
                slug, session_count, tick_clients, stop_tag, col_tag,
            ))

            if strategies:
                for s in strategies:
                    dry_tag = " [DRY RUN]" if s.get("dry_run") else ""
                    alive_tag = "" if s.get("alive", True) else " [DEAD]"
                    pid_tag = " PID={}".format(s["pid"]) if s.get("pid") else ""
                    bankroll = s.get("bankroll", s.get("cash", 0))
                    cap_info = ""
                    if s.get("session_cash_ratio"):
                        r = s["session_cash_ratio"]
                        cap_info = " ({:.0f}% up to ${:.2f})".format(
                            r["ratio"] * 100, r["max"],
                        )
                    elif s.get("session_cap"):
                        cap_info = " (cap=${:.2f})".format(s["session_cap"])
                    print("    - {} | bankroll=${:.2f}{}{}{}{} | params={}".format(
                        s["name"], bankroll, cap_info, dry_tag, pid_tag, alive_tag,
                        s.get("params", {}),
                    ))
            else:
                print("    No workers")


def cmd_stop(args):
    """Stop the running app (workers + control server). Collectors keep running."""
    state = _read_state()
    if state is None:
        print("App is not running.")
        return

    try:
        resp = _http_request("POST", "/api/control/stop")
        if resp.get("ok"):
            print("Stop signal sent (PID {}). Waiting for graceful shutdown...".format(
                state["pid"],
            ))
            print("  Data collectors will keep running. Use 'market-off' to stop them.")
    except SystemExit:
        print("HTTP stop failed, sending SIGTERM to PID {}...".format(state["pid"]))
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except OSError as e:
            print("Failed to signal process: {}".format(e))
            _remove_state()
            return

    pid = state["pid"]
    for _ in range(30):
        if not _pid_alive(pid):
            print("App stopped. Collectors still running (use 'market-off' to stop).")
            _remove_state()
            return
        time.sleep(1)

    print("WARNING: App (PID {}) did not exit within 30s.".format(pid))


def cmd_collector_stop(args):
    """Stop the collector for a market (without needing app running)."""
    from core.defaults import collector_pid_path
    slug_key = args.slug_prefix.rstrip("-")
    pid_path = collector_pid_path(slug_key)

    try:
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        print("No collector running for {}.".format(slug_key))
        return

    if not _pid_alive(pid):
        print("Collector PID {} is stale — not running.".format(pid))
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        return

    print("Stopping collector for {} (PID {})...".format(slug_key, pid))
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print("Failed to signal collector: {}".format(e))
        return

    for _ in range(30):
        if not _pid_alive(pid):
            print("Collector stopped.")
            return
        time.sleep(1)
    print("WARNING: Collector (PID {}) did not exit within 30s.".format(pid))


def cmd_collector_status(args):
    """Show status of all running collectors (works without app running)."""
    from core.defaults import MARKETS_DIR, collector_pid_path, collector_state_path
    import glob

    if not os.path.isdir(MARKETS_DIR):
        print("No markets directory found.")
        return

    found = False
    for entry in sorted(os.listdir(MARKETS_DIR)):
        market_dir = os.path.join(MARKETS_DIR, entry)
        if not os.path.isdir(market_dir):
            continue
        pid_path = collector_pid_path(entry)
        try:
            with open(pid_path, "r") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue

        alive = _pid_alive(pid)
        if not alive:
            continue

        found = True
        state_path = collector_state_path(entry)
        state_info = ""
        try:
            with open(state_path, "r") as f:
                state = json.load(f)
            state_info = " | state={} | sessions={} | tick_clients={}".format(
                state.get("state", "?"),
                state.get("session_count", "?"),
                state.get("tick_clients", "?"),
            )
            if state.get("stop_after_session"):
                state_info += " [STOPPING]"
        except (OSError, json.JSONDecodeError):
            pass

        print("  {} | PID={}{}".format(entry, pid, state_info))

    if not found:
        print("No collectors running.")


def cmd_claim(args):
    """Claim/redeem all resolved winning positions."""
    from app.auth import get_auth
    from app.claim import (
        claim_redeemable,
        fetch_redeemable_positions,
        _get_builder_relay_client,
    )

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY environment variable not set.")
        sys.exit(1)

    try:
        auth = get_auth(private_key)
    except Exception as e:
        print("ERROR: Authentication failed: {}".format(e))
        sys.exit(1)

    safe_address = auth["safe_address"]
    logger.info("Safe wallet: %s", safe_address)

    try:
        _get_builder_relay_client(private_key)
    except ValueError as e:
        print("ERROR: {}".format(e))
        sys.exit(1)

    positions = fetch_redeemable_positions(safe_address)

    if not positions:
        print("Nothing to claim.")
        return

    print("\nRedeemable positions:")
    print("{:<64}  {:>10}  {:>10}  {}".format(
        "Condition ID", "Tokens", "Value ($)", "Market",
    ))
    print("-" * 120)

    total_value = 0.0
    for pos in positions:
        size = pos.get("size", 0)
        title = pos.get("title", "Unknown")
        outcome = pos.get("outcome", "?")
        cond_id = pos.get("conditionId", "?")
        label = "{} ({})".format(title[:50], outcome)
        print("{:<64}  {:>10.2f}  {:>10.2f}  {}".format(
            cond_id, size, size, label,
        ))
        total_value += size

    print("-" * 120)
    print("Total: ${:.2f} across {} position(s)\n".format(total_value, len(positions)))

    wait = not getattr(args, "no_wait", False)
    succeeded, failed, _ = claim_redeemable(
        private_key, safe_address, wait_for_confirm=wait,
    )

    action = "confirmed" if wait else "submitted"
    print("\nClaim complete: {} {}, {} failed".format(succeeded, action, failed))
    if succeeded > 0 and failed == 0:
        print("Redeemed ~${:.2f} USDC.e".format(total_value))


def cmd_optimizer_on(args):
    """Start the live optimizer."""
    body = {}  # type: Dict[str, Any]
    if getattr(args, "interval", None) is not None:
        body["interval"] = args.interval
    if getattr(args, "eval_sessions", None) is not None:
        body["eval_sessions"] = args.eval_sessions
    if getattr(args, "min_sharpe", None) is not None:
        body["min_sharpe"] = args.min_sharpe
    if getattr(args, "cash", None) is not None:
        body["starting_cash"] = float(args.cash)
    if getattr(args, "max_edge_iters", None) is not None:
        body["max_edge_iters"] = args.max_edge_iters

    resp = _http_request("POST", "/api/control/optimizer-on", body)
    _check_resp(resp, "Optimizer started (PID {})".format(resp.get("pid")))


def cmd_optimizer_off(args):
    """Stop the live optimizer."""
    resp = _http_request("POST", "/api/control/optimizer-off")
    _check_resp(resp, "Optimizer stopped.")


def cmd_optimizer_config(args):
    """View or update optimizer config."""
    body = {}  # type: Dict[str, Any]
    if getattr(args, "interval", None) is not None:
        body["interval_minutes"] = args.interval
    if getattr(args, "eval_sessions", None) is not None:
        body["eval_sessions"] = args.eval_sessions
    if getattr(args, "min_sharpe", None) is not None:
        body["min_sharpe"] = args.min_sharpe
    if getattr(args, "cash", None) is not None:
        body["starting_cash"] = float(args.cash)
    if getattr(args, "max_edge_iters", None) is not None:
        body["max_edge_iters"] = args.max_edge_iters

    if body:
        resp = _http_request("POST", "/api/control/optimizer-config", body)
        if resp.get("ok"):
            config = resp.get("config", {})
            print("Optimizer config updated:")
            for k, v in sorted(config.items()):
                print("  {}: {}".format(k, v))
        else:
            print("Error: {}".format(resp.get("error", "unknown")))
            sys.exit(1)
    else:
        # Just show status
        resp = _http_request("GET", "/api/control/optimizer-status")
        print("Optimizer Status:")
        print("  Running: {}".format(resp.get("running", False)))
        if resp.get("pid"):
            print("  PID: {}".format(resp["pid"]))
        if resp.get("state"):
            print("  State: {}".format(resp["state"]))
        if resp.get("interval_minutes"):
            print("  Interval: {}m".format(resp["interval_minutes"]))
        if resp.get("eval_sessions"):
            print("  Eval sessions: {}".format(resp["eval_sessions"]))
        if resp.get("min_sharpe") is not None:
            print("  Min Sharpe: {}".format(resp["min_sharpe"]))
        if resp.get("last_run"):
            print("  Last run: {}".format(resp["last_run"]))
        if resp.get("next_run"):
            print("  Next run: {}".format(resp["next_run"]))
        if resp.get("last_results"):
            print("  Last results:")
            for r in resp["last_results"]:
                action_str = r.get("action", "?")
                sharpe_val = r.get("new_sharpe") or r.get("best_sharpe") or r.get("sharpe") or 0
                print("    {} / {} -> {} (sharpe={:.3f})".format(
                    r.get("market", "?"), r.get("strategy", "?"),
                    action_str, sharpe_val,
                ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Polymarket Multi-Market App")
    subparsers = parser.add_subparsers(dest="command")

    # start
    start_parser = subparsers.add_parser("start", help="Start the central app")
    start_parser.add_argument(
        "--port", type=int, default=DEFAULT_CONTROL_PORT,
        help="Control server port (default: {})".format(DEFAULT_CONTROL_PORT),
    )
    start_parser.add_argument(
        "--restore", action="store_true",
        help="Restore all markets and strategies from last run",
    )
    start_parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose API logging (CLOB, gamma — generates GB/day)",
    )

    # market-on
    market_on_parser = subparsers.add_parser("market-on", help="Start data collection for a market")
    market_on_parser.add_argument(
        "slug_prefix", help='Market slug prefix, e.g. "btc-updown-5m-"',
    )
    market_on_parser.add_argument(
        "--window", type=int, default=None,
        help="Window duration override in seconds (auto-detected from markets.py)",
    )
    market_on_parser.add_argument(
        "--market-type", default="updown",
        help="Market type for strategy validation (default: updown)",
    )
    market_on_parser.add_argument(
        "--restore", action="store_true",
        help="Restore strategies for this market from last run",
    )

    # market-off
    market_off_parser = subparsers.add_parser("market-off", help="Stop a market")
    market_off_parser.add_argument(
        "slug_prefix", help="Market slug prefix to stop",
    )
    market_off_parser.add_argument(
        "--after-session", action="store_true",
        help="Stop after the current session finishes (graceful)",
    )

    # trade-on
    trade_on_parser = subparsers.add_parser("trade-on", help="Enable trading on a market")
    trade_on_parser.add_argument(
        "slug_prefix", help="Market slug prefix",
    )
    trade_on_parser.add_argument(
        "--strategy", required=True,
        help="Strategy name: {}".format(list(ALL_STRATEGIES.keys())),
    )
    trade_on_parser.add_argument(
        "--params", default="",
        help="Strategy params as k=v,k=v",
    )
    trade_on_parser.add_argument(
        "--cash", type=str, default=None,
        help='Starting cash. Use "cached" for .cache/balance.json',
    )
    trade_on_parser.add_argument(
        "--dry-run", action="store_true",
        help="Trade without real orders",
    )
    trade_on_parser.add_argument(
        "--per-session", action="store_true",
        help="Reset cash at the start of each session",
    )
    trade_on_parser.add_argument(
        "--session-cap", type=float, default=None,
        help="Max dollars deployed per session",
    )
    trade_on_parser.add_argument(
        "--session-cash-ratio", default=None,
        help="Deploy ratio:max of bankroll per session, e.g. 0.25:3",
    )

    # trade-off
    trade_off_parser = subparsers.add_parser("trade-off", help="Disable trading on a market")
    trade_off_parser.add_argument(
        "slug_prefix", help="Market slug prefix",
    )
    trade_off_parser.add_argument(
        "--strategy", default=None,
        help="Remove only this strategy (omit to disable all)",
    )

    # set-cash
    set_cash_parser = subparsers.add_parser("set-cash", help="Update cash on a market")
    set_cash_parser.add_argument(
        "slug_prefix", help="Market slug prefix",
    )
    set_cash_parser.add_argument("amount", type=float, help="New cash amount in dollars")
    set_cash_parser.add_argument(
        "--strategy", default=None,
        help="Target strategy (required when multiple active)",
    )
    set_cash_parser.add_argument(
        "--session-cash-ratio", default=None,
        help="Deploy ratio:max of bankroll per session, e.g. 0.25:3",
    )
    set_cash_parser.add_argument(
        "--session-cap", type=float, default=None,
        help="Set fixed session cap",
    )
    set_cash_parser.add_argument(
        "--clear-session-limit", action="store_true",
        help="Clear session cap / ratio",
    )

    # reload
    reload_parser = subparsers.add_parser("reload", help="Reload a worker")
    reload_parser.add_argument(
        "slug_prefix", help="Market slug prefix",
    )
    reload_parser.add_argument(
        "--strategy", required=True,
        help="Strategy worker to reload",
    )

    # kill
    subparsers.add_parser("kill", help="Stop all markets after their current sessions")

    # status
    subparsers.add_parser("status", help="Check app status")

    # stop
    subparsers.add_parser("stop", help="Stop the running app")

    # collector-stop
    col_stop_parser = subparsers.add_parser(
        "collector-stop", help="Stop a collector (works without app running)",
    )
    col_stop_parser.add_argument(
        "slug_prefix", help="Market slug prefix",
    )

    # collector-status
    subparsers.add_parser(
        "collector-status", help="Show running collectors (works without app running)",
    )

    # claim
    claim_parser = subparsers.add_parser("claim", help="Claim/redeem resolved positions")
    claim_parser.add_argument(
        "--no-wait", action="store_true",
        help="Submit without waiting for on-chain confirmation",
    )

    # optimizer-on
    opt_on_parser = subparsers.add_parser("optimizer-on", help="Start the live optimizer")
    opt_on_parser.add_argument("--interval", type=int, default=30,
                               help="Minutes between optimization cycles (default: 30)")
    opt_on_parser.add_argument("--eval-sessions", type=int, default=30,
                               help="Number of recent sessions to evaluate (default: 30)")
    opt_on_parser.add_argument("--min-sharpe", type=float, default=0.2,
                               help="Min Sharpe to keep a strategy (default: 0.2)")
    opt_on_parser.add_argument("--cash", type=float, default=100.0,
                               help="Starting cash for backtests (default: 100)")
    opt_on_parser.add_argument("--max-edge-iters", type=int, default=2,
                               help="Max edge refinement iterations (default: 2)")

    # optimizer-off
    subparsers.add_parser("optimizer-off", help="Stop the live optimizer")

    # optimizer-config
    opt_cfg_parser = subparsers.add_parser("optimizer-config", help="View/update optimizer config")
    opt_cfg_parser.add_argument("--interval", type=int, default=None,
                                help="Update interval (minutes)")
    opt_cfg_parser.add_argument("--eval-sessions", type=int, default=None,
                                help="Update eval sessions count")
    opt_cfg_parser.add_argument("--min-sharpe", type=float, default=None,
                                help="Update min Sharpe threshold")
    opt_cfg_parser.add_argument("--cash", type=float, default=None,
                                help="Update starting cash")
    opt_cfg_parser.add_argument("--max-edge-iters", type=int, default=None,
                                help="Update max edge refinement iterations")

    args = parser.parse_args()

    commands = {
        "start": cmd_start,
        "market-on": cmd_market_on,
        "market-off": cmd_market_off,
        "trade-on": cmd_trade_on,
        "trade-off": cmd_trade_off,
        "set-cash": cmd_set_cash,
        "reload": cmd_reload,
        "kill": cmd_kill,
        "status": cmd_status,
        "stop": cmd_stop,
        "collector-stop": cmd_collector_stop,
        "collector-status": cmd_collector_status,
        "claim": cmd_claim,
        "optimizer-on": cmd_optimizer_on,
        "optimizer-off": cmd_optimizer_off,
        "optimizer-config": cmd_optimizer_config,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
