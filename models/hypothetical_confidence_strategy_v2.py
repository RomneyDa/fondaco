#!/usr/bin/env python3
"""
Improved walk-forward hypothetical strategy using regularized logistic regression
+ richer features + parameter search.

No third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

SESSION_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.json$")


def sf(x, d=0.0):
    try:
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return d


def std(vs: Sequence[float]) -> float:
    n = len(vs)
    if n <= 1:
        return 0.0
    m = sum(vs) / n
    return math.sqrt(sum((x - m) ** 2 for x in vs) / (n - 1))


def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def load_sessions(session_dir: Path):
    out = []
    for p in sorted(glob(str(session_dir / "*.json"))):
        pp = Path(p)
        if not SESSION_FILE_RE.search(pp.name):
            continue
        try:
            with pp.open() as f:
                d = json.load(f)
            ts = datetime.strptime(pp.stem, "%Y-%m-%dT%H-%M-%SZ")
            out.append((ts, d, pp))
        except Exception:
            continue
    return out


def get_sample_leq(samples, t):
    chosen = None
    for s in samples:
        if int(sf(s.get("elapsed_seconds"))) <= t:
            chosen = s
        else:
            break
    return chosen


def feature_row(session_data: dict, decision_seconds: int, vol_window: int = 60, mom_window: int = 30):
    outcome = str(session_data.get("outcome", "")).lower().strip()
    if outcome not in {"up", "down"}:
        return None
    y = 1 if outcome == "up" else 0

    samples = session_data.get("samples") or []
    if len(samples) < 20:
        return None

    s0 = samples[0]
    s = get_sample_leq(samples, decision_seconds)
    s_prev = get_sample_leq(samples, max(0, decision_seconds - mom_window))
    if s is None or s_prev is None:
        return None

    start_btc = sf(s0.get("btc_price_usd"))
    btc = sf(s.get("btc_price_usd"))
    btc_prev = sf(s_prev.get("btc_price_usd"))
    up_mid = sf(s.get("up_token_midpoint"))
    down_mid = sf(s.get("down_token_midpoint"))
    up_prev = sf(s_prev.get("up_token_midpoint"))
    down_prev = sf(s_prev.get("down_token_midpoint"))
    up_bid = sf(s.get("up_token_best_bid"), up_mid)
    up_ask = sf(s.get("up_token_best_ask"), up_mid)
    down_bid = sf(s.get("down_token_best_bid"), down_mid)
    down_ask = sf(s.get("down_token_best_ask"), down_mid)

    if min(start_btc, btc, btc_prev, up_mid, down_mid, up_prev, down_prev) <= 0:
        return None

    t = int(sf(s.get("elapsed_seconds")))
    max_t = max(1, int(sf(samples[-1].get("elapsed_seconds"), 1)))

    btc_rets = []
    up_series = []
    down_series = []
    cutoff = t - vol_window
    for q in samples:
        tq = int(sf(q.get("elapsed_seconds")))
        if tq > t:
            break
        if tq >= cutoff:
            b = sf(q.get("btc_price_usd"))
            u = sf(q.get("up_token_midpoint"))
            d = sf(q.get("down_token_midpoint"))
            if b > 0:
                btc_rets.append((b / start_btc) - 1.0)
            if u > 0:
                up_series.append(u)
            if d > 0:
                down_series.append(d)

    x = [
        t / max_t,                          # elapsed_frac
        (btc / start_btc) - 1.0,            # btc_ret
        (btc / btc_prev) - 1.0,             # btc_momentum
        std(btc_rets),                      # btc_vol
        up_mid,                             # up_mid
        down_mid,                           # down_mid
        up_mid - down_mid,                  # skew
        (up_mid / down_mid) - 1.0,          # skew_ratio
        up_mid - up_prev,                   # up_mom
        down_mid - down_prev,               # down_mom
        max(up_ask - up_bid, 0.0),          # up_spread
        max(down_ask - down_bid, 0.0),      # down_spread
        std(up_series),                     # up_vol
        std(down_series),                   # down_vol
    ]

    return x, y, up_mid, down_mid


def fit_scaler(x: List[List[float]]):
    p = len(x[0])
    means, stds = [], []
    for j in range(p):
        col = [r[j] for r in x]
        m = sum(col) / len(col)
        s = std(col)
        if s < 1e-12:
            s = 1.0
        means.append(m)
        stds.append(s)
    return means, stds


def scale_row(x, means, stds):
    return [(x[j] - means[j]) / stds[j] for j in range(len(x))]


def fit_logreg_gd(x: List[List[float]], y: List[int], lr=0.05, epochs=160, l2=1e-3):
    n = len(x)
    p = len(x[0])
    w = [0.0] * (p + 1)  # bias + weights

    for _ in range(epochs):
        gb = 0.0
        gw = [0.0] * p
        for i in range(n):
            z = w[0] + sum(w[j + 1] * x[i][j] for j in range(p))
            pr = sigmoid(z)
            err = pr - y[i]
            gb += err
            for j in range(p):
                gw[j] += err * x[i][j]
        w[0] -= lr * gb / n
        for j in range(p):
            w[j + 1] -= lr * ((gw[j] / n) + l2 * w[j + 1])
    return w


def predict_prob(w, x):
    z = w[0] + sum(w[j + 1] * x[j] for j in range(len(x)))
    return sigmoid(z)


def run_backtest(records, min_train_sessions, stake, min_conf, min_ev, max_price, retrain_interval=5):
    trades = 0
    wins = 0
    pnl = 0.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0

    means = stds = w = None
    for i in range(min_train_sessions, len(records)):
        train = records[:i]
        test = records[i]

        if (i - min_train_sessions) % max(1, retrain_interval) == 0 or w is None:
            xtr = [r[0] for r in train]
            ytr = [r[1] for r in train]
            means, stds = fit_scaler(xtr)
            xtr_s = [scale_row(x, means, stds) for x in xtr]
            w = fit_logreg_gd(xtr_s, ytr)

        x, y, up_mid, down_mid = test
        xs = scale_row(x, means, stds)
        p_up = predict_prob(w, xs)
        p_down = 1.0 - p_up

        conf = abs(p_up - 0.5)
        if conf < min_conf:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
            continue

        side = None
        price = None
        ev_up = (p_up / up_mid) - 1.0 if up_mid > 0 else -1e9
        ev_down = (p_down / down_mid) - 1.0 if down_mid > 0 else -1e9

        if p_up >= 0.5 and up_mid <= max_price and ev_up >= min_ev:
            side = 1
            price = up_mid
        elif p_up < 0.5 and down_mid <= max_price and ev_down >= min_ev:
            side = 0
            price = down_mid

        if side is None:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
            continue

        trades += 1
        tokens = stake / price
        win = int(side == y)
        if win:
            wins += 1
        trade_pnl = (tokens if win else 0.0) - stake
        pnl += trade_pnl
        eq += trade_pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    total_eval = max(0, len(records) - min_train_sessions)
    return {
        "eval_sessions": total_eval,
        "trades": trades,
        "win_rate": wins / trades if trades else 0.0,
        "pnl": pnl,
        "staked": trades * stake,
        "roi": (pnl / (trades * stake)) if trades else 0.0,
        "max_dd": max_dd,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--data-root", default="data/markets")
    ap.add_argument("--min-train-sessions", type=int, default=150)
    ap.add_argument("--stake", type=float, default=1.0)
    args = ap.parse_args()

    session_dir = Path(args.data_root) / args.market / "sessions"
    sessions = load_sessions(session_dir)

    # Build per-decision-time datasets once
    decision_grid = [150, 180, 210]
    datasets = {}
    for ds in decision_grid:
        recs = []
        for _, d, _ in sessions:
            fr = feature_row(d, ds)
            if fr is not None:
                recs.append(fr)
        datasets[ds] = recs

    # Baseline-ish
    base = run_backtest(datasets[180], args.min_train_sessions, args.stake, 0.08, 0.03, 0.90)

    # Small curated search set (runtime-safe)
    candidates = [
        (150, 0.08, 0.02, 0.90),
        (150, 0.12, 0.04, 0.85),
        (180, 0.08, 0.03, 0.90),
        (180, 0.12, 0.04, 0.90),
        (180, 0.16, 0.05, 0.85),
        (210, 0.08, 0.03, 0.90),
        (210, 0.12, 0.04, 0.85),
        (210, 0.16, 0.06, 0.85),
    ]

    best = None
    best_params = None
    for ds, conf, ev, mp in candidates:
        recs = datasets[ds]
        r = run_backtest(recs, args.min_train_sessions, args.stake, conf, ev, mp)
        score = (r["pnl"], r["roi"], r["trades"])
        if r["trades"] >= 40 and (best is None or score > (best["pnl"], best["roi"], best["trades"])):
            best = r
            best_params = (ds, conf, ev, mp)

    print("=== Improved Hypothetical Strategy (v2) ===")
    print(f"Market: {args.market}")
    print(f"Sessions loaded: {len(sessions)}")
    print()
    print("--- Baseline-like config ---")
    print("decision=180, min_conf=0.08, min_ev=0.03, max_price=0.90")
    print(
        f"trades={base['trades']}/{base['eval_sessions']} win={base['win_rate']:.2%} "
        f"pnl=${base['pnl']:.2f} roi={base['roi']:.2%} maxDD=${base['max_dd']:.2f}"
    )

    if best is None:
        print("\nNo viable parameter set found.")
        return

    ds, conf, ev, mp = best_params
    print("\n--- Best found (grid search) ---")
    print(f"decision={ds}, min_conf={conf}, min_ev={ev}, max_price={mp}")
    print(
        f"trades={best['trades']}/{best['eval_sessions']} win={best['win_rate']:.2%} "
        f"pnl=${best['pnl']:.2f} roi={best['roi']:.2%} maxDD=${best['max_dd']:.2f}"
    )


if __name__ == "__main__":
    main()
