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
import logging.handlers
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
    WINDOW_DURATION_SECONDS,
)
from bots import ALL_STRATEGIES

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            APP_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3,
        ),
    ],
)
logging.getLogger().handlers[0].setLevel(logging.INFO)
logger = logging.getLogger(__name__)


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
    mgr = AppManager(port=port)

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
    if args.window != WINDOW_DURATION_SECONDS:
        body["window"] = args.window
    if getattr(args, "market_type", None):
        body["market_type"] = args.market_type
    if getattr(args, "restore", False):
        body["restore"] = True

    resp = _http_request("POST", "/api/control/market-on", body)
    if resp.get("ok"):
        print("Market started: {}".format(resp.get("slug_key", args.slug_prefix)))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_market_off(args):
    """Stop data collection for a market."""
    body = {
        "slug_prefix": args.slug_prefix,
    }  # type: Dict[str, Any]
    if getattr(args, "after_session", False):
        body["after_session"] = True

    resp = _http_request("POST", "/api/control/market-off", body)
    if resp.get("ok"):
        print("Market stopped: {}".format(args.slug_prefix))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


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
    if resp.get("ok"):
        print("Trading enabled: strategy={} on {}".format(
            resp.get("strategy"), resp.get("slug_prefix"),
        ))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_trade_off(args):
    """Disable trading on a running market."""
    body = {"slug_prefix": args.slug_prefix}  # type: Dict[str, Any]
    strategy = getattr(args, "strategy", None)
    if strategy:
        body["strategy"] = strategy

    resp = _http_request("POST", "/api/control/trade-off", body)
    if resp.get("ok"):
        if strategy:
            print("Trading disabled for {} on {}.".format(strategy, args.slug_prefix))
        else:
            print("All trading disabled on {}.".format(args.slug_prefix))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


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
    if resp.get("ok"):
        print("Cash updated: ${}".format(resp.get("cash")))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_reload(args):
    """Reload (restart) a worker to pick up code changes."""
    body = {
        "slug_prefix": args.slug_prefix,
        "strategy": args.strategy,
    }
    resp = _http_request("POST", "/api/control/reload", body)
    if resp.get("ok"):
        print("Reloaded worker: {} on {}".format(args.strategy, args.slug_prefix))
    else:
        print("Error: {}".format(resp.get("error", "unknown")))
        sys.exit(1)


def cmd_kill(args):
    """Signal the app to stop all markets after their current sessions finish."""
    resp = _http_request("POST", "/api/control/kill")
    if resp.get("ok"):
        print("Kill signal sent — all markets will stop after their current sessions.")
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

            strategies = m.get("strategies", [])
            print("  {} | sessions={} | tick_clients={}{}".format(
                slug, session_count, tick_clients, stop_tag,
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
    """Stop the running app."""
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

    # market-on
    market_on_parser = subparsers.add_parser("market-on", help="Start data collection for a market")
    market_on_parser.add_argument(
        "slug_prefix", help='Market slug prefix, e.g. "btc-updown-5m-"',
    )
    market_on_parser.add_argument(
        "--window", type=int, default=WINDOW_DURATION_SECONDS,
        help="Window duration in seconds (default: {})".format(WINDOW_DURATION_SECONDS),
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

    # claim
    claim_parser = subparsers.add_parser("claim", help="Claim/redeem resolved positions")
    claim_parser.add_argument(
        "--no-wait", action="store_true",
        help="Submit without waiting for on-chain confirmation",
    )

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
