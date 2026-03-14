"""Reconcile bot_trades.jsonl fill records against actual CLOB fill amounts.

Fetches trade history via client.get_trades(), builds a map of
order_id -> actual matched_amount, compares against recorded fill amounts,
and rewrites bot_log.jsonl with corrected PnL values. Also updates the
strategy's state.json bankroll.

Usage:
    uv run python -m scripts.reconcile_fills --strategy PoissonKellyGated --market btc-updown-5m [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from app.auth import get_auth, build_clob_client
from core.defaults import BASE_DIR, TOKEN_DECIMALS, market_data_dir

logger = logging.getLogger(__name__)


def _fetch_fill_map(client):
    # type: (...) -> Dict[str, float]
    """Fetch all trades and build order_id -> total matched_amount map.

    Each trade from get_trades() has a maker_orders list where our limit
    orders appear. We aggregate matched_amount per order_id since one order
    can appear across multiple trade events.
    """
    from py_clob_client.clob_types import TradeParams
    print("Fetching trade history from CLOB API...")
    trades = client.get_trades(TradeParams())
    print("  Got {} trade events.".format(len(trades)))

    fill_map = defaultdict(float)  # type: Dict[str, float]
    for trade in trades:
        # Our limit orders show up in maker_orders when we're the maker
        for maker_order in trade.get("maker_orders", []):
            oid = maker_order.get("order_id", "")
            matched = float(maker_order.get("matched_amount", "0"))
            fill_map[oid] += matched

        # If we're the taker, the taker_order_id is ours
        taker_oid = trade.get("taker_order_id", "")
        if taker_oid:
            size = float(trade.get("size", "0"))
            fill_map[taker_oid] += size

    print("  Built fill map for {} unique order IDs.".format(len(fill_map)))
    return dict(fill_map)


def _load_jsonl(path):
    # type: (str) -> List[Dict[str, Any]]
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _write_jsonl(path, entries):
    # type: (str, List[Dict[str, Any]]) -> None
    tmp = path + ".reconcile.tmp"
    with open(tmp, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    # Backup original
    backup = path + ".pre-reconcile.bak"
    if os.path.exists(path) and not os.path.exists(backup):
        os.rename(path, backup)
    elif os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def reconcile(strategy, market, dry_run=False):
    # type: (str, str, bool) -> None
    data_dir = market_data_dir(market)
    trades_file = os.path.join(data_dir, "bot_trades.jsonl")
    log_file = os.path.join(data_dir, "bot_log.jsonl")
    state_file = os.path.join(data_dir, "workers", strategy, "state.json")

    # Load trade records
    all_trades = _load_jsonl(trades_file)
    all_sessions = _load_jsonl(log_file)

    # Find fill records for this strategy (has fee = confirmed fill)
    strategy_fills = [
        t for t in all_trades
        if t.get("strategy") == strategy
        and t.get("type") not in ("resolution",)
        and t.get("success") is not False
        and t.get("fee") is not None
        and t.get("order_id")
    ]

    buy_fills = [
        t for t in strategy_fills
        if "buy" in t.get("side", "")
    ]

    if not buy_fills:
        print("No buy fills found for {}. Nothing to reconcile.".format(strategy))
        return

    print("Found {} buy fills to check against CLOB API.".format(len(buy_fills)))

    # Authenticate and build client
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
        sys.exit(1)

    auth = get_auth(private_key)
    client = build_clob_client(private_key, auth)

    # Fetch all trades in one batch and build order_id -> matched_amount map
    fill_map = _fetch_fill_map(client)

    # Compare recorded fills against actual
    corrections = {}  # order_id -> corrected_tokens
    total_token_diff = 0.0
    total_dollar_diff = 0.0
    not_found = 0

    for i, fill in enumerate(buy_fills):
        order_id = fill["order_id"]
        recorded_tokens = fill.get("tokens", 0)
        price = fill.get("price", 0)

        if order_id not in fill_map:
            not_found += 1
            print("  [{}/{}] {} — not found in trade history".format(
                i + 1, len(buy_fills), order_id[:16]))
            continue

        actual_tokens = fill_map[order_id]

        if abs(actual_tokens - recorded_tokens) > 0.001:
            diff = recorded_tokens - actual_tokens
            total_token_diff += diff
            total_dollar_diff += diff * price
            corrections[order_id] = {
                "recorded": recorded_tokens,
                "actual": actual_tokens,
                "price": price,
                "side": fill.get("side"),
                "timestamp": fill.get("timestamp", ""),
                "diff_tokens": diff,
                "diff_dollars": diff * price,
            }
            print("  [{}/{}] {} MISMATCH: recorded={:.4f} actual={:.4f} diff={:.4f} tokens (${:.4f})".format(
                i + 1, len(buy_fills), order_id[:16],
                recorded_tokens, actual_tokens, diff, diff * price,
            ))
        else:
            if (i + 1) % 10 == 0:
                print("  [{}/{}] checked...".format(i + 1, len(buy_fills)))

    print()
    print("=== Summary ===")
    print("Checked: {} buy fills ({} not found in API)".format(len(buy_fills), not_found))
    print("Mismatches: {}".format(len(corrections)))
    print("Total token difference: {:.4f} (recorded - actual)".format(total_token_diff))
    print("Total dollar difference: ${:.4f}".format(total_dollar_diff))

    if not corrections:
        print("No corrections needed!")
        return

    if dry_run:
        print()
        print("DRY RUN — no files modified.")
        return

    # Apply corrections to bot_trades.jsonl
    print()
    print("Applying corrections to bot_trades.jsonl...")
    trades_modified = 0
    for t in all_trades:
        oid = t.get("order_id")
        if oid and oid in corrections:
            c = corrections[oid]
            t["requested_tokens"] = t.get("tokens")
            t["tokens"] = c["actual"]
            t["amount"] = c["actual"] * c["price"]
            # Recalculate fee based on actual tokens
            from core.defaults import compute_taker_fee
            t["fee"] = compute_taker_fee(c["price"], c["actual"])
            trades_modified += 1
    _write_jsonl(trades_file, all_trades)
    print("  Modified {} trade records.".format(trades_modified))

    # Recalculate session PnL in bot_log.jsonl
    # Group corrected fills by session (match timestamps to session windows)
    print("Recalculating session PnL in bot_log.jsonl...")

    # Get all trades for this strategy (both placements and fills)
    strategy_trades = [
        t for t in all_trades
        if t.get("strategy") == strategy
        and t.get("type") not in ("resolution",)
        and t.get("success") is not False
        and t.get("fee") is not None
    ]

    # Get session boundaries from bot_log
    strategy_sessions = [
        s for s in all_sessions
        if s.get("strategy") == strategy
    ]

    if not strategy_sessions:
        print("  No sessions found — skipping PnL recalculation.")
        return

    # Replay PnL from the first session using corrected trades
    # We need to know the starting cash and session-cash-ratio
    first_session = strategy_sessions[0]
    starting_bankroll = first_session["cash"] - first_session["pnl"]

    # Load session cash ratio from state
    session_cash_ratio = None
    if os.path.exists(state_file):
        with open(state_file) as f:
            state = json.load(f)
            session_cash_ratio = state.get("session_cash_ratio")

    # Recalculate each session's PnL
    bankroll = starting_bankroll
    sessions_modified = 0

    for session in strategy_sessions:
        ts = session["timestamp"]
        session_num = session.get("session_number")

        # Find trades in this session window
        # Match by finding trades between this session and the next
        idx = strategy_sessions.index(session)
        next_ts = strategy_sessions[idx + 1]["timestamp"] if idx + 1 < len(strategy_sessions) else "9999"

        session_trades = [
            t for t in strategy_trades
            if t.get("timestamp", "") >= ts[:16] and t.get("timestamp", "") < next_ts[:16]
        ]

        # Actually, simpler: recalculate from bankroll + trade effects
        # Start of session: deploy from bankroll
        session_start_cash = bankroll
        deploy = session_start_cash
        reserve = 0.0

        if session_cash_ratio:
            ratio = session_cash_ratio["ratio"]
            cap = session_cash_ratio["max"]
            deploy = min(session_start_cash * ratio, cap)
            if session_start_cash > deploy:
                reserve = session_start_cash - deploy

        # Sum up trade effects
        cash = deploy
        up_tokens = 0.0
        down_tokens = 0.0

        for t in session_trades:
            side = t.get("side", "")
            tokens = t.get("tokens", 0)
            price = t.get("price", 0)
            fee = t.get("fee", 0) or 0

            if "buy" in side:
                cash -= tokens * price + fee
                if "up" in side:
                    up_tokens += tokens
                else:
                    down_tokens += tokens
            elif "sell" in side:
                cash += tokens * price - fee
                if "up" in side:
                    up_tokens -= tokens
                else:
                    down_tokens -= tokens

        # Resolve
        outcome = session.get("outcome")
        credit = 0.0
        if outcome == "Up":
            credit = up_tokens
        elif outcome == "Down":
            credit = down_tokens

        cash += credit + reserve
        pnl = cash - session_start_cash

        old_pnl = session.get("pnl", 0)
        if abs(pnl - old_pnl) > 0.001:
            session["pnl"] = round(pnl, TOKEN_DECIMALS)
            session["credit"] = round(credit, TOKEN_DECIMALS)
            session["cash"] = round(cash, TOKEN_DECIMALS)
            sessions_modified += 1

        bankroll = cash

    _write_jsonl(log_file, all_sessions)
    print("  Modified {} session PnL records.".format(sessions_modified))
    print("  New bankroll: ${:.4f} (was ${:.4f})".format(
        bankroll,
        strategy_sessions[-1].get("cash", 0) if strategy_sessions else 0,
    ))

    # Update state.json bankroll
    if os.path.exists(state_file):
        with open(state_file) as f:
            state = json.load(f)
        old_bankroll = state.get("bankroll", 0)
        state["bankroll"] = round(bankroll, TOKEN_DECIMALS)
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
        print("  Updated state.json bankroll: ${:.4f} -> ${:.4f}".format(
            old_bankroll, bankroll,
        ))


def main():
    parser = argparse.ArgumentParser(description="Reconcile fill amounts against CLOB API")
    parser.add_argument("--strategy", required=True, help="Strategy name to reconcile")
    parser.add_argument("--market", default="btc-updown-5m", help="Market slug key")
    parser.add_argument("--dry-run", action="store_true", help="Check only, don't modify files")

    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    reconcile(args.strategy, args.market, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
