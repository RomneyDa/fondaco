#!/usr/bin/env python3
"""
Hypothetical model-driven strategy backtest over existing session files.

- Trains an OLS linear-probability model on past sessions only (walk-forward).
- At a chosen decision time (e.g., 180s into session), predicts P(Up).
- Buys Up/Down tokens only when confidence + expected value conditions pass.
- Reports hypothetical PnL over historical sessions.

No external dependencies.

Usage:
  python models/hypothetical_confidence_strategy.py
  python models/hypothetical_confidence_strategy.py --decision-seconds 180 --stake 2.0 --min-confidence 0.08 --min-ev 0.03
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import deque
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SESSION_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.json$")


def safe_float(x, default=0.0):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def stddev(values: Sequence[float]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


# ---------- minimal linear algebra ----------
def matmul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    n, m, p = len(a), len(a[0]), len(b[0])
    out = [[0.0] * p for _ in range(n)]
    for i in range(n):
        for k in range(m):
            aik = a[i][k]
            bk = b[k]
            for j in range(p):
                out[i][j] += aik * bk[j]
    return out


def transpose(a: List[List[float]]) -> List[List[float]]:
    return list(map(list, zip(*a)))


def invert_matrix(a: List[List[float]]) -> List[List[float]]:
    n = len(a)
    aug = [row[:] + [0.0] * n for row in a]
    for i in range(n):
        aug[i][n + i] = 1.0

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("Singular matrix")
        aug[col], aug[pivot] = aug[pivot], aug[col]

        pv = aug[col][col]
        inv_pv = 1.0 / pv
        for j in range(2 * n):
            aug[col][j] *= inv_pv

        for r in range(n):
            if r == col:
                continue
            f = aug[r][col]
            if f == 0.0:
                continue
            for j in range(2 * n):
                aug[r][j] -= f * aug[col][j]

    return [row[n:] for row in aug]


def fit_ols(x: List[List[float]], y: List[int], ridge: float = 1e-8) -> List[float]:
    x_i = [[1.0] + row for row in x]
    y_col = [[float(v)] for v in y]

    xt = transpose(x_i)
    xtx = matmul(xt, x_i)
    for i in range(len(xtx)):
        xtx[i][i] += ridge
    xtx_inv = invert_matrix(xtx)
    xty = matmul(xt, y_col)
    beta_col = matmul(xtx_inv, xty)
    return [b[0] for b in beta_col]


def predict_one(beta: List[float], x: List[float]) -> float:
    v = beta[0] + sum(beta[i + 1] * x[i] for i in range(len(x)))
    return min(1.0, max(0.0, v))


def standardize_fit(x: List[List[float]]) -> Tuple[List[float], List[float]]:
    p = len(x[0])
    means = [0.0] * p
    stds = [1.0] * p
    for j in range(p):
        col = [r[j] for r in x]
        m = sum(col) / len(col)
        s = stddev(col)
        if s < 1e-12:
            s = 1.0
        means[j] = m
        stds[j] = s
    return means, stds


def standardize_apply(x: List[float], means: List[float], stds: List[float]) -> List[float]:
    return [(x[j] - means[j]) / stds[j] for j in range(len(x))]


# ---------- data ----------
def load_sessions(session_dir: Path) -> List[Tuple[datetime, dict, Path]]:
    paths = [Path(p) for p in glob(str(session_dir / "*.json"))]
    rows = []
    for p in paths:
        if not SESSION_FILE_RE.search(p.name):
            continue
        try:
            with p.open("r") as f:
                d = json.load(f)
            ts = datetime.strptime(p.stem, "%Y-%m-%dT%H-%M-%SZ")
            rows.append((ts, d, p))
        except Exception:
            continue
    rows.sort(key=lambda t: t[0])
    return rows


def feature_at_time(
    session_data: dict,
    decision_seconds: int,
    vol_window_seconds: int,
) -> Optional[Tuple[List[float], float, float, int, int]]:
    """
    Returns (x, up_mid, down_mid, outcome_label, elapsed_used)
    outcome_label: Up=1 Down=0
    """
    outcome = str(session_data.get("outcome", "")).strip().lower()
    if outcome not in {"up", "down"}:
        return None
    label = 1 if outcome == "up" else 0

    samples = session_data.get("samples") or []
    if len(samples) < 20:
        return None

    start_btc = safe_float(samples[0].get("btc_price_usd"), 0.0)
    if start_btc <= 0:
        return None

    chosen = None
    for s in samples:
        t = int(safe_float(s.get("elapsed_seconds"), 0.0))
        if t <= decision_seconds:
            chosen = s
        else:
            break

    if chosen is None:
        return None

    t_chosen = int(safe_float(chosen.get("elapsed_seconds"), 0.0))

    btc_vals = []
    up_vals = []
    down_vals = []
    cutoff = t_chosen - vol_window_seconds

    max_elapsed = safe_float(samples[-1].get("elapsed_seconds"), 1.0)
    if max_elapsed <= 0:
        max_elapsed = 1.0

    for s in samples:
        t = int(safe_float(s.get("elapsed_seconds"), 0.0))
        if t > t_chosen:
            break
        if t >= cutoff:
            btc = safe_float(s.get("btc_price_usd"), 0.0)
            up_m = safe_float(s.get("up_token_midpoint"), 0.0)
            down_m = safe_float(s.get("down_token_midpoint"), 0.0)
            if btc > 0:
                btc_vals.append((btc / start_btc) - 1.0)
            if up_m > 0:
                up_vals.append(up_m)
            if down_m > 0:
                down_vals.append(down_m)

    btc = safe_float(chosen.get("btc_price_usd"), 0.0)
    up_mid = safe_float(chosen.get("up_token_midpoint"), 0.0)
    down_mid = safe_float(chosen.get("down_token_midpoint"), 0.0)
    up_bid = safe_float(chosen.get("up_token_best_bid"), up_mid)
    up_ask = safe_float(chosen.get("up_token_best_ask"), up_mid)
    down_bid = safe_float(chosen.get("down_token_best_bid"), down_mid)
    down_ask = safe_float(chosen.get("down_token_best_ask"), down_mid)

    if btc <= 0 or up_mid <= 0 or down_mid <= 0:
        return None

    btc_ret = (btc / start_btc) - 1.0
    elapsed_frac = t_chosen / max_elapsed

    x = [
        elapsed_frac,
        btc_ret,
        stddev(btc_vals),
        up_mid,
        down_mid,
        up_mid - down_mid,
        max(up_ask - up_bid, 0.0),
        max(down_ask - down_bid, 0.0),
        stddev(up_vals),
        stddev(down_vals),
    ]
    return x, up_mid, down_mid, label, t_chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--data-root", default="data/markets")
    ap.add_argument("--decision-seconds", type=int, default=180)
    ap.add_argument("--vol-window-seconds", type=int, default=60)
    ap.add_argument("--min-train-sessions", type=int, default=150)
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--min-confidence", type=float, default=0.08, help="|p_up - 0.5| minimum")
    ap.add_argument("--min-ev", type=float, default=0.03, help="minimum expected return per $ staked")
    ap.add_argument("--max-price", type=float, default=0.90, help="don't buy super-expensive tokens")
    args = ap.parse_args()

    session_dir = Path(args.data_root) / args.market / "sessions"
    sessions = load_sessions(session_dir)
    if not sessions:
        raise SystemExit(f"No sessions found in {session_dir}")

    records = []
    for ts, data, path in sessions:
        f = feature_at_time(data, args.decision_seconds, args.vol_window_seconds)
        if f is None:
            continue
        x, up_mid, down_mid, y, t_used = f
        records.append({
            "ts": ts,
            "path": path,
            "x": x,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "y": y,
            "t_used": t_used,
        })

    if len(records) <= args.min_train_sessions + 20:
        raise SystemExit("Not enough usable sessions for walk-forward backtest")

    feature_names = [
        "elapsed_frac",
        "btc_return_from_start",
        "btc_recent_vol",
        "up_mid",
        "down_mid",
        "midpoint_diff_up_minus_down",
        "up_spread",
        "down_spread",
        "up_recent_vol",
        "down_recent_vol",
    ]

    trades = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for i in range(args.min_train_sessions, len(records)):
        train = records[:i]
        test = records[i]

        x_train = [r["x"] for r in train]
        y_train = [r["y"] for r in train]

        means, stds = standardize_fit(x_train)
        x_train_s = [standardize_apply(x, means, stds) for x in x_train]

        try:
            beta = fit_ols(x_train_s, y_train)
        except Exception:
            continue

        x_test_s = standardize_apply(test["x"], means, stds)
        p_up = predict_one(beta, x_test_s)
        p_down = 1.0 - p_up
        conf = abs(p_up - 0.5)

        action = "skip"
        side = None
        price = None
        ev = None
        pnl = 0.0

        # Candidate EV per $1 stake: p/price - 1
        ev_up = (p_up / test["up_mid"]) - 1.0 if test["up_mid"] > 0 else -999
        ev_down = (p_down / test["down_mid"]) - 1.0 if test["down_mid"] > 0 else -999

        if conf >= args.min_confidence:
            if p_up >= 0.5 and test["up_mid"] <= args.max_price and ev_up >= args.min_ev:
                action = "buy"
                side = "up"
                price = test["up_mid"]
                ev = ev_up
            elif p_up < 0.5 and test["down_mid"] <= args.max_price and ev_down >= args.min_ev:
                action = "buy"
                side = "down"
                price = test["down_mid"]
                ev = ev_down

        if action == "buy":
            tokens = args.stake / price
            win = (side == "up" and test["y"] == 1) or (side == "down" and test["y"] == 0)
            payout = tokens if win else 0.0
            pnl = payout - args.stake
            equity += pnl

        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

        trades.append({
            "ts": test["ts"],
            "action": action,
            "side": side,
            "price": price,
            "p_up": p_up,
            "confidence": conf,
            "ev": ev,
            "actual": "up" if test["y"] == 1 else "down",
            "pnl": pnl,
            "equity": equity,
            "elapsed_used": test["t_used"],
            "path": str(test["path"]),
        })

    total_sessions = len(trades)
    executed = [t for t in trades if t["action"] == "buy"]
    n_trades = len(executed)
    wins = sum(1 for t in executed if t["pnl"] > 0)
    losses = sum(1 for t in executed if t["pnl"] < 0)

    total_staked = n_trades * args.stake
    total_pnl = sum(t["pnl"] for t in executed)
    roi = (total_pnl / total_staked) if total_staked > 0 else 0.0
    win_rate = (wins / n_trades) if n_trades > 0 else 0.0

    avg_conf = sum(t["confidence"] for t in executed) / n_trades if n_trades else 0.0
    avg_ev = sum(t["ev"] for t in executed if t["ev"] is not None) / n_trades if n_trades else 0.0

    up_trades = [t for t in executed if t["side"] == "up"]
    down_trades = [t for t in executed if t["side"] == "down"]

    print("=== Hypothetical Confidence Strategy Backtest ===")
    print(f"Market: {args.market}")
    print(f"Usable sessions: {len(records)}")
    print(f"Walk-forward eval sessions: {total_sessions} (after {args.min_train_sessions} warmup)")
    print()
    print("--- Rules ---")
    print(f"Decision time: {args.decision_seconds}s")
    print(f"Vol window: {args.vol_window_seconds}s")
    print(f"Stake per trade: ${args.stake:.2f}")
    print(f"Min confidence |p_up-0.5|: {args.min_confidence:.3f}")
    print(f"Min EV: {args.min_ev:.3f}")
    print(f"Max token price: {args.max_price:.2f}")
    print()
    print("--- Results ---")
    print(f"Trades executed: {n_trades} / {total_sessions} ({(n_trades/total_sessions if total_sessions else 0):.1%})")
    print(f"Win rate: {win_rate:.2%}  (W {wins} / L {losses})")
    print(f"Total staked: ${total_staked:.2f}")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"ROI on staked capital: {roi:.2%}")
    print(f"Max drawdown (PnL curve): ${max_dd:.2f}")
    print(f"Avg confidence (trades): {avg_conf:.3f}")
    print(f"Avg model EV at entry: {avg_ev:.3f}")
    print(f"Up trades: {len(up_trades)} | Down trades: {len(down_trades)}")

    print("\n--- Last 10 executed trades ---")
    for t in executed[-10:]:
        print(
            f"{t['ts'].isoformat()}Z side={t['side']:4s} price={t['price']:.3f} "
            f"p_up={t['p_up']:.3f} conf={t['confidence']:.3f} ev={t['ev']:.3f} "
            f"actual={t['actual']:4s} pnl={t['pnl']:+.3f} eq={t['equity']:+.2f}"
        )


if __name__ == "__main__":
    main()
