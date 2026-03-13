#!/usr/bin/env python3
"""
Decoupled model/strategy workflow.

Mode 1: build-tape
  - Walk-forward train + calibrate model (past-only)
  - Emit prediction tape (one row per evaluable session)

Mode 2: sweep
  - Load prediction tape
  - Sweep strategy thresholds WITHOUT retraining model

This cleanly separates:
  model quality (p_up predictions)
  from
  execution policy (thresholds, EV, price caps)
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
from typing import List, Optional, Sequence

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
            out.append((ts, d, pp.name))
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
    a, b = 1.0, 0.0
    n = len(logits)
    if n < 20:
        return a, b
    for _ in range(epochs):
        ga = gb = 0.0
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


def ev_per_dollar(p: float, price: float, fee_bps: float, slippage_cents: float) -> float:
    raw = (p / price) - 1.0
    fee = fee_bps / 10000.0
    slip = (slippage_cents / 100.0) / max(price, 1e-9)
    return raw - fee - slip


def build_tape(args):
    sessions = load_sessions(Path(args.data_root) / args.market / "sessions")
    records = []
    for ts, d, name in sessions:
        fr = feature_row(d, args.decision_seconds)
        if fr is None:
            continue
        x, y, up_mid, down_mid = fr
        records.append((ts, name, x, y, up_mid, down_mid))

    out_rows = []
    means = stds = w = None
    a, b = 1.0, 0.0

    for i in range(args.min_train_sessions, len(records)):
        train_start = max(0, i - args.lookback) if args.lookback > 0 else 0
        train = records[train_start:i]
        test = records[i]

        if len(train) < 50:
            continue

        if (i - args.min_train_sessions) % max(1, args.retrain_interval) == 0 or w is None:
            split = max(30, len(train) - args.calib_size)
            fit_part = train[:split]
            calib_part = train[split:]

            x_fit = [r[2] for r in fit_part]
            y_fit = [r[3] for r in fit_part]
            means, stds = fit_scaler(x_fit)
            x_fit_s = [scale_row(x, means, stds) for x in x_fit]
            w = fit_logreg_gd(x_fit_s, y_fit)

            if len(calib_part) >= 20:
                cl, cy = [], []
                for _, _, x, y, _, _ in calib_part:
                    xs = scale_row(x, means, stds)
                    z, _ = predict_logit_prob(w, xs)
                    cl.append(z)
                    cy.append(y)
                a, b = fit_platt(cl, cy)

        ts, name, x, y, up_mid, down_mid = test
        xs = scale_row(x, means, stds)
        z, p_raw = predict_logit_prob(w, xs)
        p_cal = calibrated_prob(z, a, b)

        out_rows.append({
            "ts": ts.isoformat() + "Z",
            "session": name,
            "y": y,
            "up_mid": up_mid,
            "down_mid": down_mid,
            "p_raw": p_raw,
            "p_up": p_cal,
        })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")

    print(f"Built tape: {out_path} rows={len(out_rows)}")


def load_tape(path: Path):
    rows = []
    with path.open() as f:
        for ln in f:
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


def eval_policy(rows, min_conf, min_ev, max_price, fee_bps, slippage_cents, stake=1.0, block_size=100):
    pnl = 0.0
    trades = 0
    wins = 0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    block = []

    for r in rows:
        p_up = float(r["p_up"])
        y = int(r["y"])
        up_mid = float(r["up_mid"])
        down_mid = float(r["down_mid"])

        conf = abs(p_up - 0.5)
        traded = False
        tr_pnl = 0.0
        if conf >= min_conf:
            p_dn = 1.0 - p_up
            ev_up = ev_per_dollar(p_up, up_mid, fee_bps, slippage_cents)
            ev_dn = ev_per_dollar(p_dn, down_mid, fee_bps, slippage_cents)
            side = None
            price = None
            if p_up >= 0.5 and up_mid <= max_price and ev_up >= min_ev:
                side, price = 1, up_mid
            elif p_up < 0.5 and down_mid <= max_price and ev_dn >= min_ev:
                side, price = 0, down_mid

            if side is not None:
                traded = True
                trades += 1
                tokens = stake / price
                win = int(side == y)
                wins += win
                gross = (tokens if win else 0.0) - stake
                fee = stake * ((fee_bps / 10000.0) + (slippage_cents / 100.0) / max(price, 1e-9))
                tr_pnl = gross - fee
                pnl += tr_pnl
                eq += tr_pnl

        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        block.append((traded, tr_pnl))

    blocks = []
    for i in range(0, len(block), block_size):
        chunk = block[i:i+block_size]
        b_tr = sum(1 for t, _ in chunk if t)
        b_pnl = sum(p for _, p in chunk)
        blocks.append((b_tr, b_pnl))

    staked = trades * stake
    return {
        "n": len(rows),
        "trades": trades,
        "win_rate": wins / trades if trades else 0.0,
        "pnl": pnl,
        "roi": pnl / staked if staked > 0 else 0.0,
        "max_dd": max_dd,
        "blocks": blocks,
    }


def sweep(args):
    rows = load_tape(Path(args.tape))
    best = None
    best_cfg = None

    conf_grid = [0.08, 0.10, 0.12, 0.15, 0.20]
    ev_grid = [0.01, 0.02, 0.03, 0.04, 0.06]
    price_grid = [0.75, 0.80, 0.85, 0.90]

    for conf in conf_grid:
        for ev in ev_grid:
            for mp in price_grid:
                r = eval_policy(rows, conf, ev, mp, args.fee_bps, args.slippage_cents, stake=args.stake, block_size=args.block_size)
                if r["trades"] < args.min_trades:
                    continue
                # score: prioritize pnl, then roi, then lower dd
                score = (r["pnl"], r["roi"], -r["max_dd"])
                if best is None or score > (best["pnl"], best["roi"], -best["max_dd"]):
                    best, best_cfg = r, (conf, ev, mp)

    if best is None:
        print("No config met min_trades threshold")
        return

    conf, ev, mp = best_cfg
    print("=== Best policy on fixed prediction tape ===")
    print(f"rows={best['n']} trades={best['trades']} ({best['trades']/best['n']:.1%})")
    print(f"min_conf={conf} min_ev={ev} max_price={mp}")
    print(f"win_rate={best['win_rate']:.2%} pnl=${best['pnl']:.2f} roi={best['roi']:.2%} maxDD=${best['max_dd']:.2f}")
    if best["blocks"]:
        bpnls = [p for _, p in best["blocks"]]
        print(f"blocks={len(best['blocks'])} avg_block_pnl=${mean(bpnls):.2f} block_std=${std(bpnls):.2f}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)

    b = sub.add_parser("build-tape")
    b.add_argument("--market", default="btc-updown-5m")
    b.add_argument("--data-root", default="data/markets")
    b.add_argument("--decision-seconds", type=int, default=180)
    b.add_argument("--min-train-sessions", type=int, default=180)
    b.add_argument("--lookback", type=int, default=450)
    b.add_argument("--calib-size", type=int, default=120)
    b.add_argument("--retrain-interval", type=int, default=5)
    b.add_argument("--out", default="models/out/prediction_tape_v1.jsonl")

    s = sub.add_parser("sweep")
    s.add_argument("--tape", default="models/out/prediction_tape_v1.jsonl")
    s.add_argument("--stake", type=float, default=1.0)
    s.add_argument("--fee-bps", type=float, default=20.0)
    s.add_argument("--slippage-cents", type=float, default=1.0)
    s.add_argument("--min-trades", type=int, default=40)
    s.add_argument("--block-size", type=int, default=100)

    args = ap.parse_args()
    if args.mode == "build-tape":
        build_tape(args)
    else:
        sweep(args)


if __name__ == "__main__":
    main()
