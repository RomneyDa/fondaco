#!/usr/bin/env python3
"""
V3: fee/slippage-aware, calibrated, walk-forward evaluator.

- Past-only training (walk-forward)
- Logistic model + Platt calibration on recent past
- EV_net gating includes execution cost penalty
- Reports global + block-wise stability metrics

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
from statistics import mean
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

    btc_rets, up_series, down_series = [], [], []
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

    # compacted feature set (avoid obvious collinearity explosion)
    x = [
        t / max_t,
        (btc / start_btc) - 1.0,
        (btc / btc_prev) - 1.0,
        std(btc_rets),
        up_mid,
        (up_mid - down_mid),
        max(up_ask - up_bid, 0.0),
        max(down_ask - down_bid, 0.0),
        up_mid - up_prev,
        down_mid - down_prev,
        std(up_series),
        std(down_series),
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


def fit_logreg_gd(x: List[List[float]], y: List[int], lr=0.05, epochs=180, l2=1e-3):
    n = len(x)
    p = len(x[0])
    w = [0.0] * (p + 1)
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


def predict_logit_prob(w, x):
    z = w[0] + sum(w[j + 1] * x[j] for j in range(len(x)))
    return z, sigmoid(z)


def fit_platt(logits: List[float], y: List[int], epochs=220, lr=0.03, l2=1e-4):
    # p_cal = sigmoid(a*logit + b)
    a, b = 1.0, 0.0
    n = len(logits)
    if n < 20:
        return a, b
    for _ in range(epochs):
        ga = 0.0
        gb = 0.0
        for z, t in zip(logits, y):
            p = sigmoid(a * z + b)
            e = p - t
            ga += e * z
            gb += e
        a -= lr * ((ga / n) + l2 * a)
        b -= lr * (gb / n)
    return a, b


def calibrated_prob(logit: float, a: float, b: float) -> float:
    return sigmoid(a * logit + b)


def ev_per_dollar(p: float, price: float, fee_rate: float, slippage_cents: float) -> float:
    # baseline expected return for binary payout token
    raw = (p / price) - 1.0
    # simple conservative execution penalty
    slip = (slippage_cents / 100.0) / max(price, 1e-9)
    penalty = fee_rate + slip
    return raw - penalty


def run_backtest(records, min_train_sessions, lookback, calib_size, stake, min_conf, min_ev, max_price,
                 fee_bps, slippage_cents, retrain_interval=5, block_size=100):
    trades = 0
    wins = 0
    pnl = 0.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    block_rows = []

    means = stds = w = None
    a, b = 1.0, 0.0
    fee_rate = fee_bps / 10000.0

    for i in range(min_train_sessions, len(records)):
        train_start = max(0, i - lookback) if lookback > 0 else 0
        train = records[train_start:i]
        test = records[i]

        if len(train) < 50:
            continue

        if (i - min_train_sessions) % max(1, retrain_interval) == 0 or w is None:
            split = max(30, len(train) - calib_size)
            fit_part = train[:split]
            calib_part = train[split:]

            x_fit = [r[0] for r in fit_part]
            y_fit = [r[1] for r in fit_part]

            means, stds = fit_scaler(x_fit)
            x_fit_s = [scale_row(x, means, stds) for x in x_fit]
            w = fit_logreg_gd(x_fit_s, y_fit)

            if len(calib_part) >= 20:
                cal_logits, cal_y = [], []
                for x, y, _, _ in calib_part:
                    xs = scale_row(x, means, stds)
                    z, _ = predict_logit_prob(w, xs)
                    cal_logits.append(z)
                    cal_y.append(y)
                a, b = fit_platt(cal_logits, cal_y)

        x, y, up_mid, down_mid = test
        xs = scale_row(x, means, stds)
        z, _ = predict_logit_prob(w, xs)
        p_up = calibrated_prob(z, a, b)
        p_down = 1.0 - p_up

        conf = abs(p_up - 0.5)
        side = None
        price = None
        if conf >= min_conf:
            ev_up = ev_per_dollar(p_up, up_mid, fee_rate, slippage_cents)
            ev_dn = ev_per_dollar(p_down, down_mid, fee_rate, slippage_cents)
            if p_up >= 0.5 and up_mid <= max_price and ev_up >= min_ev:
                side, price = 1, up_mid
            elif p_up < 0.5 and down_mid <= max_price and ev_dn >= min_ev:
                side, price = 0, down_mid

        trade_pnl = 0.0
        if side is not None:
            trades += 1
            tokens = stake / price
            win = int(side == y)
            wins += win
            gross = (tokens if win else 0.0) - stake
            # apply explicit per-trade friction in pnl too
            friction = stake * (fee_rate + (slippage_cents / 100.0) / max(price, 1e-9))
            trade_pnl = gross - friction
            pnl += trade_pnl
            eq += trade_pnl

        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

        block_rows.append({"pnl": trade_pnl, "traded": side is not None})

    # block-wise stability
    blocks = []
    for j in range(0, len(block_rows), block_size):
        b_rows = block_rows[j:j + block_size]
        if not b_rows:
            continue
        b_trades = sum(1 for r in b_rows if r["traded"])
        b_pnl = sum(r["pnl"] for r in b_rows)
        blocks.append((b_trades, b_pnl))

    return {
        "eval_sessions": max(0, len(records) - min_train_sessions),
        "trades": trades,
        "win_rate": wins / trades if trades else 0.0,
        "pnl": pnl,
        "staked": trades * stake,
        "roi": (pnl / (trades * stake)) if trades else 0.0,
        "max_dd": max_dd,
        "blocks": blocks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--data-root", default="data/markets")
    ap.add_argument("--decision-seconds", type=int, default=180)
    ap.add_argument("--min-train-sessions", type=int, default=180)
    ap.add_argument("--lookback", type=int, default=450, help="0 = expanding window")
    ap.add_argument("--calib-size", type=int, default=120)
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--min-confidence", type=float, default=0.10)
    ap.add_argument("--min-ev", type=float, default=0.02)
    ap.add_argument("--max-price", type=float, default=0.90)
    ap.add_argument("--fee-bps", type=float, default=20.0)
    ap.add_argument("--slippage-cents", type=float, default=1.0)
    ap.add_argument("--block-size", type=int, default=100)
    args = ap.parse_args()

    session_dir = Path(args.data_root) / args.market / "sessions"
    sessions = load_sessions(session_dir)

    records = []
    for _, d, _ in sessions:
        fr = feature_row(d, args.decision_seconds)
        if fr is not None:
            records.append(fr)

    res = run_backtest(
        records,
        min_train_sessions=args.min_train_sessions,
        lookback=args.lookback,
        calib_size=args.calib_size,
        stake=args.stake,
        min_conf=args.min_confidence,
        min_ev=args.min_ev,
        max_price=args.max_price,
        fee_bps=args.fee_bps,
        slippage_cents=args.slippage_cents,
        block_size=args.block_size,
    )

    print("=== V3 Calibrated Walk-Forward Strategy Eval ===")
    print(f"Sessions usable: {len(records)}")
    print(f"Eval sessions: {res['eval_sessions']}")
    print("--- config ---")
    print(f"decision={args.decision_seconds}s lookback={args.lookback} calib={args.calib_size}")
    print(f"min_conf={args.min_confidence} min_ev={args.min_ev} max_price={args.max_price}")
    print(f"fee_bps={args.fee_bps} slippage_cents={args.slippage_cents}")
    print("--- results ---")
    print(f"trades={res['trades']}/{res['eval_sessions']} ({(res['trades']/res['eval_sessions'] if res['eval_sessions'] else 0):.1%})")
    print(f"win_rate={res['win_rate']:.2%}")
    print(f"pnl=${res['pnl']:.2f}")
    print(f"roi={res['roi']:.2%}")
    print(f"max_dd=${res['max_dd']:.2f}")

    if res["blocks"]:
        b_pnls = [p for _, p in res["blocks"]]
        b_tr = [t for t, _ in res["blocks"]]
        print("--- block stability ---")
        print(f"blocks={len(res['blocks'])} avg_block_pnl=${mean(b_pnls):.2f} block_pnl_std=${std(b_pnls):.2f}")
        print(f"avg_block_trades={mean(b_tr):.1f}")
        print("last_5_blocks:")
        for t, p in res["blocks"][-5:]:
            print(f"  trades={t:3d} pnl=${p:+.2f}")


if __name__ == "__main__":
    main()
