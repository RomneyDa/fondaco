#!/usr/bin/env python3
"""
Unified Polymarket App: Collect + Trade

Always collects data. Optionally trades (toggled on/off via CLI or HTTP).

Usage:
    uv run python -m app start btc-updown-5m-
    uv run python -m app start btc-updown-5m- --strategy LateSniper --params min_elapsed=0.7
    uv run python -m app trade-on --strategy LateSniper --params min_elapsed=0.7
    uv run python -m app trade-off
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
    POLL_INTERVAL_SECONDS,
    RESOLUTION_POLL_INTERVAL,
    RESOLUTION_TIMEOUT_SECONDS,
    WINDOW_DURATION_SECONDS,
)
from core.strategies import ALL_STRATEGIES

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(APP_LOG_FILE),
    ],
)
# Console stays at INFO; file gets everything (DEBUG)
logging.getLogger().handlers[0].setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def _write_state(pid, port):
    # type: (int, int) -> None
    """Write process state to data/app.json."""
    os.makedirs(os.path.dirname(APP_STATE_FILE), exist_ok=True)
    with open(APP_STATE_FILE, "w") as f:
        json.dump({"pid": pid, "port": port}, f, indent=2)


def _read_state():
    # type: () -> Optional[Dict[str, Any]]
    """Read state from data/app.json, cleaning up if process is dead."""
    if not os.path.exists(APP_STATE_FILE):
        return None
    try:
        with open(APP_STATE_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid")
        if pid and _pid_alive(pid):
            return data
        # Stale state file — clean up
        os.remove(APP_STATE_FILE)
        return None
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def _remove_state():
    # type: () -> None
    """Remove state file."""
    try:
        os.remove(APP_STATE_FILE)
    except OSError:
        pass


def _pid_alive(pid):
    # type: (int) -> bool
    """Check if a process is alive without signaling it."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_params(params_str):
    # type: (str) -> Dict[str, Any]
    """Parse 'k=v,k=v' string into dict with number coercion."""
    params = {}  # type: Dict[str, Any]
    if not params_str:
        return params
    for pair in params_str.split(","):
        k, v = pair.split("=", 1)
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass  # keep as string
        params[k.strip()] = v
    return params


def _http_request(method, path, body=None):
    # type: (str, str, Optional[Dict[str, Any]]) -> Dict[str, Any]
    """Make an HTTP request to the running control server."""
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print("Failed to connect to control server on port {}: {}".format(port, e))
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_start(args):
    """Start the unified process (collector + optional trading)."""
    from app.runner import Runner
    from app.server import ControlServer

    # Check for existing process
    state = _read_state()
    if state is not None:
        print("App already running (PID {}). Use 'uv run python -m app stop'.".format(
            state["pid"],
        ))
        sys.exit(1)

    port = args.port

    # Parse strategy params
    strategy_name = getattr(args, "strategy", None)
    strategy_params = _parse_params(getattr(args, "params", "") or "")

    # Resolve starting cash
    cash_str = getattr(args, "cash", None)
    dry_run = getattr(args, "dry_run", False)

    if cash_str == "cached":
        from app.runner import _load_balance_cache
        cached = _load_balance_cache()
        if cached:
            starting_cash = cached["cash"]
            logger.info(
                "Loaded cached balance: $%.2f (from %s, session %s)",
                starting_cash, cached.get("slug_prefix"), cached.get("session_count"),
            )
        else:
            print("No cached balance found. Use a numeric value or run first.")
            sys.exit(1)
    elif cash_str is not None:
        starting_cash = float(cash_str)
    elif dry_run:
        # Dry-run with no --cash: default to 100
        starting_cash = 100.0
    else:
        # Live trading: None signals Runner to fetch real CLOB balance
        starting_cash = None
    max_sessions = getattr(args, "sessions", 0)
    per_session = getattr(args, "per_session", False)
    session_cap = getattr(args, "session_cap", None)

    # Create runner
    runner = Runner(
        slug_prefix=args.slug_prefix,
        window_duration=args.window,
        poll_interval=args.poll,
        resolution_poll_interval=args.resolution_poll,
        resolution_timeout=args.resolution_timeout,
        strategy_name=strategy_name,
        strategy_params=strategy_params if strategy_name else None,
        starting_cash=starting_cash,
        dry_run=dry_run,
        max_sessions=max_sessions,
        per_session=per_session,
        session_cap=session_cap,
    )

    # Restore strategies from last run if requested (only when no --strategy given)
    if getattr(args, "restore", False) and strategy_name is None:
        runner.restore_strategies()

    # Start control server
    server = ControlServer(runner, port)
    server.start()

    # Write state file
    _write_state(os.getpid(), port)

    try:
        runner.run()
    finally:
        server.stop()
        _remove_state()


def cmd_trade_on(args):
    """Enable trading on the running process."""
    body = {
        "strategy": args.strategy,
        "params": _parse_params(getattr(args, "params", "") or ""),
    }
    if getattr(args, "cash", None):
        cash_str = args.cash
        if cash_str == "cached":
            from app.runner import _load_balance_cache
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

    resp = _http_request("POST", "/api/control/trade-on", body)
    if resp.get("ok"):
        print("Trading enabled: strategy={}".format(resp.get("strategy")))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_set_cash(args):
    """Update cash on the running process."""
    body = {"cash": float(args.amount)}  # type: Dict[str, Any]
    strategy = getattr(args, "strategy", None)
    if strategy:
        body["strategy"] = strategy
    resp = _http_request("POST", "/api/control/set-cash", body)
    if resp.get("ok"):
        print("Cash updated: ${}".format(resp.get("cash")))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_trade_off(args):
    """Disable trading on the running process."""
    strategy = getattr(args, "strategy", None)
    body = {"strategy": strategy} if strategy else None
    resp = _http_request("POST", "/api/control/trade-off", body)
    if resp.get("ok"):
        if strategy:
            print("Trading disabled for {} (takes effect next session).".format(strategy))
        else:
            print("Trading disabled for all strategies (takes effect next session).")
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_kill(args):
    """Signal the app to stop after the current session finishes."""
    resp = _http_request("POST", "/api/control/kill")
    if resp.get("ok"):
        print("Kill signal sent — app will stop after the current session.")
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_status(args):
    """Check app status."""
    state = _read_state()
    if state is None:
        print("App is not running.")
        return

    resp = _http_request("GET", "/api/control/status")
    print("App running (PID {})".format(state["pid"]))
    print("  Slug: {}".format(resp.get("slug_prefix", "?")))
    print("  Sessions: {}".format(resp.get("session_count", 0)))
    if resp.get("stop_after_session"):
        print("  ** Stopping after current session (kill pending) **")
    strategies = resp.get("strategies", [])
    if strategies:
        print("  Trading: ENABLED ({} {})".format(
            len(strategies), "strategy" if len(strategies) == 1 else "strategies",
        ))
        for s in strategies:
            dry_tag = " [DRY RUN]" if s.get("dry_run") else ""
            # Show total bankroll; add deployed/reserve breakdown if session_cap active
            bankroll = s.get("bankroll", s.get("cash", 0))
            cap_info = ""
            if s.get("session_cap"):
                cap_info = " (deployed=${:.2f}, reserve=${:.2f})".format(
                    s.get("cash", 0), s.get("reserve", 0),
                )
            print("    - {} | bankroll=${:.2f}{}{} | params={}".format(
                s["name"], bankroll, cap_info, dry_tag, s.get("params", {}),
            ))
    else:
        print("  Trading: disabled")


def cmd_stop(args):
    """Stop the running app."""
    state = _read_state()
    if state is None:
        print("App is not running.")
        return

    # Try HTTP stop first
    try:
        resp = _http_request("POST", "/api/control/stop")
        if resp.get("ok"):
            print("Stop signal sent (PID {}). Waiting for graceful shutdown...".format(
                state["pid"],
            ))
    except SystemExit:
        # HTTP failed, fall back to SIGTERM
        print("HTTP stop failed, sending SIGTERM to PID {}...".format(state["pid"]))
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except OSError as e:
            print("Failed to signal process: {}".format(e))
            _remove_state()
            return

    # Wait for process to exit
    pid = state["pid"]
    for _ in range(30):
        if not _pid_alive(pid):
            print("App stopped.")
            _remove_state()
            return
        time.sleep(1)

    print("WARNING: App (PID {}) did not exit within 30s.".format(pid))


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

    # Check builder creds early
    try:
        _get_builder_relay_client(private_key)
    except ValueError as e:
        print("ERROR: {}".format(e))
        sys.exit(1)

    # Query redeemable positions
    positions = fetch_redeemable_positions(safe_address)

    if not positions:
        print("Nothing to claim.")
        return

    # Display summary
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

    # Redeem
    wait = not getattr(args, "no_wait", False)
    succeeded, failed, _ = claim_redeemable(
        private_key, safe_address, wait_for_confirm=wait,
    )

    # Summary
    action = "confirmed" if wait else "submitted"
    print("\nClaim complete: {} {}, {} failed".format(succeeded, action, failed))
    if succeeded > 0 and failed == 0:
        print("Redeemed ~${:.2f} USDC.e".format(total_value))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Polymarket Unified App")
    subparsers = parser.add_subparsers(dest="command")

    # start
    start_parser = subparsers.add_parser("start", help="Start the app (collector + optional trading)")
    start_parser.add_argument(
        "slug_prefix",
        help='Market slug prefix, e.g. "btc-updown-5m-"',
    )
    start_parser.add_argument(
        "--strategy",
        default=None,
        help="Strategy name to enable trading: {}".format(list(ALL_STRATEGIES.keys())),
    )
    start_parser.add_argument(
        "--params",
        default="",
        help="Strategy params as k=v,k=v (e.g. min_elapsed=0.7,bet_fraction=0.5)",
    )
    start_parser.add_argument(
        "--window",
        type=int,
        default=WINDOW_DURATION_SECONDS,
        help="Window duration in seconds (default: {})".format(WINDOW_DURATION_SECONDS),
    )
    start_parser.add_argument(
        "--poll",
        type=float,
        default=POLL_INTERVAL_SECONDS,
        help="Poll interval in seconds (default: {})".format(POLL_INTERVAL_SECONDS),
    )
    start_parser.add_argument(
        "--resolution-poll",
        type=float,
        default=RESOLUTION_POLL_INTERVAL,
        help="Resolution poll interval (default: {})".format(RESOLUTION_POLL_INTERVAL),
    )
    start_parser.add_argument(
        "--resolution-timeout",
        type=int,
        default=RESOLUTION_TIMEOUT_SECONDS,
        help="Resolution timeout (default: {})".format(RESOLUTION_TIMEOUT_SECONDS),
    )
    start_parser.add_argument(
        "--cash",
        type=str,
        default=None,
        help='Starting cash. Omit for live trading (fetches real balance). '
             'Use "cached" for .cache/balance.json. Required for --dry-run.',
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CONTROL_PORT,
        help="Control server port (default: {})".format(DEFAULT_CONTROL_PORT),
    )
    start_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run trading without placing real orders (uses SimExecutor)",
    )
    start_parser.add_argument(
        "--sessions",
        type=int,
        default=0,
        help="Max sessions to run before exiting (default: 0 = unlimited)",
    )
    start_parser.add_argument(
        "--per-session",
        action="store_true",
        help="Reset cash to --cash amount at the start of each session",
    )
    start_parser.add_argument(
        "--session-cap",
        type=float,
        default=None,
        help="Max dollars deployed per session (rest held in reserve)",
    )
    start_parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore strategy configs from last run (data/strategies.json)",
    )

    # trade-on
    trade_on_parser = subparsers.add_parser("trade-on", help="Enable trading on running app")
    trade_on_parser.add_argument(
        "--strategy",
        required=True,
        help="Strategy name: {}".format(list(ALL_STRATEGIES.keys())),
    )
    trade_on_parser.add_argument(
        "--params",
        default="",
        help="Strategy params as k=v,k=v",
    )
    trade_on_parser.add_argument(
        "--cash",
        type=str,
        default=None,
        help='Starting cash. Use "cached" for .cache/balance.json',
    )
    trade_on_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Trade without real orders",
    )
    trade_on_parser.add_argument(
        "--per-session",
        action="store_true",
        help="Reset cash to --cash amount at the start of each session",
    )
    trade_on_parser.add_argument(
        "--session-cap",
        type=float,
        default=None,
        help="Max dollars deployed per session (rest held in reserve)",
    )

    # trade-off
    trade_off_parser = subparsers.add_parser("trade-off", help="Disable trading on running app")
    trade_off_parser.add_argument(
        "--strategy",
        default=None,
        help="Remove only this strategy (omit to disable all)",
    )

    # set-cash
    set_cash_parser = subparsers.add_parser("set-cash", help="Update cash on running app")
    set_cash_parser.add_argument("amount", type=float, help="New cash amount in dollars")
    set_cash_parser.add_argument(
        "--strategy",
        default=None,
        help="Target strategy (required when multiple strategies are active)",
    )

    # kill
    subparsers.add_parser("kill", help="Stop the app after the current session finishes")

    # status
    subparsers.add_parser("status", help="Check app status")

    # stop
    subparsers.add_parser("stop", help="Stop the running app")

    # claim
    claim_parser = subparsers.add_parser("claim", help="Claim/redeem resolved positions")
    claim_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Submit without waiting for on-chain confirmation",
    )

    args = parser.parse_args()

    commands = {
        "start": cmd_start,
        "trade-on": cmd_trade_on,
        "trade-off": cmd_trade_off,
        "set-cash": cmd_set_cash,
        "kill": cmd_kill,
        "status": cmd_status,
        "stop": cmd_stop,
        "claim": cmd_claim,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
