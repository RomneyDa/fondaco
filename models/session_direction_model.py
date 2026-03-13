#!/usr/bin/env python3
"""
Train a simple linear-probability model (OLS) to predict whether a session
resolves Up (1) or Down (0) from in-session features.

No third-party dependencies required.

Usage:
  python models/session_direction_model.py
  python models/session_direction_model.py --market btc-updown-5m --step-seconds 20 --vol-window-seconds 60
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import deque, defaultdict
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


SESSION_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.json$")


# -----------------------------
# Lightweight matrix utilities
# -----------------------------
def matmul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    n, m, p = len(a), len(a[0]), len(b[0])
    out = [[0.0] * p for _ in range(n)]
    for i in range(n):
        ai = a[i]
        for k in range(m):
            aik = ai[k]
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
            raise ValueError("Matrix is singular or near-singular")
        aug[col], aug[pivot] = aug[pivot], aug[col]

        pivot_val = aug[col][col]
        inv_pivot = 1.0 / pivot_val
        for j in range(2 * n):
            aug[col][j] *= inv_pivot

        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for j in range(2 * n):
                aug[r][j] -= factor * aug[col][j]

    return [row[n:] for row in aug]


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# -----------------------------
# Feature extraction
# -----------------------------
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


def load_sessions(session_dir: Path) -> List[Tuple[datetime, dict, Path]]:
    paths = [Path(p) for p in glob(str(session_dir / "*.json"))]
    items = []
    for p in paths:
        if not SESSION_FILE_RE.search(p.name):
            continue  # skip *.trading.*.json and other files
        try:
            with p.open("r") as f:
                data = json.load(f)
            stamp = datetime.strptime(p.stem, "%Y-%m-%dT%H-%M-%SZ")
            items.append((stamp, data, p))
        except Exception:
            continue
    items.sort(key=lambda t: t[0])
    return items


def extract_rows_from_session(
    session_data: dict,
    label: int,
    step_seconds: int,
    vol_window_seconds: int,
) -> List[Tuple[List[float], int, int]]:
    samples = session_data.get("samples") or []
    if len(samples) < 20:
        return []

    max_elapsed = safe_float(samples[-1].get("elapsed_seconds"), 0.0)
    if max_elapsed <= 0:
        return []

    start_btc = safe_float(samples[0].get("btc_price_usd"), 0.0)
    if start_btc <= 0:
        return []

    # Rolling containers for volatility features
    btc_window = deque()  # (elapsed, btc_return)
    up_window = deque()   # (elapsed, up_mid)
    down_window = deque() # (elapsed, down_mid)

    rows = []
    seen_buckets = set()

    for s in samples:
        t = int(safe_float(s.get("elapsed_seconds"), 0.0))
        bucket = t // max(step_seconds, 1)
        if bucket in seen_buckets:
            continue

        btc = safe_float(s.get("btc_price_usd"), 0.0)
        up_mid = safe_float(s.get("up_token_midpoint"), 0.0)
        down_mid = safe_float(s.get("down_token_midpoint"), 0.0)
        up_bid = safe_float(s.get("up_token_best_bid"), up_mid)
        up_ask = safe_float(s.get("up_token_best_ask"), up_mid)
        down_bid = safe_float(s.get("down_token_best_bid"), down_mid)
        down_ask = safe_float(s.get("down_token_best_ask"), down_mid)

        if btc <= 0 or up_mid <= 0 or down_mid <= 0:
            continue

        btc_ret = (btc / start_btc) - 1.0

        btc_window.append((t, btc_ret))
        up_window.append((t, up_mid))
        down_window.append((t, down_mid))

        cutoff = t - vol_window_seconds
        while btc_window and btc_window[0][0] < cutoff:
            btc_window.popleft()
        while up_window and up_window[0][0] < cutoff:
            up_window.popleft()
        while down_window and down_window[0][0] < cutoff:
            down_window.popleft()

        btc_vol = stddev([v for _, v in btc_window])
        up_vol = stddev([v for _, v in up_window])
        down_vol = stddev([v for _, v in down_window])

        elapsed_frac = t / max_elapsed
        up_spread = max(up_ask - up_bid, 0.0)
        down_spread = max(down_ask - down_bid, 0.0)
        midpoint_diff = up_mid - down_mid

        # Feature vector (no intercept here; added later)
        x = [
            elapsed_frac,
            btc_ret,
            btc_vol,
            up_mid,
            down_mid,
            midpoint_diff,
            up_spread,
            down_spread,
            up_vol,
            down_vol,
        ]

        rows.append((x, label, t))
        seen_buckets.add(bucket)

    return rows


# -----------------------------
# Modeling and metrics
# -----------------------------
def standardize(
    x_train: List[List[float]],
    x_test: List[List[float]],
) -> Tuple[List[List[float]], List[List[float]], List[float], List[float]]:
    p = len(x_train[0])
    means = [0.0] * p
    stds = [0.0] * p

    for j in range(p):
        col = [row[j] for row in x_train]
        m = sum(col) / len(col)
        s = stddev(col)
        if s < 1e-12:
            s = 1.0
        means[j] = m
        stds[j] = s

    def transform(rows):
        out = []
        for r in rows:
            out.append([(r[j] - means[j]) / stds[j] for j in range(p)])
        return out

    return transform(x_train), transform(x_test), means, stds


def fit_ols_with_stats(
    x: List[List[float]],
    y: List[int],
    ridge: float = 1e-8,
) -> Dict:
    n = len(x)
    k = len(x[0]) + 1  # + intercept

    x_i = [[1.0] + row for row in x]
    y_col = [[float(v)] for v in y]

    xt = transpose(x_i)
    xtx = matmul(xt, x_i)

    for i in range(k):
        xtx[i][i] += ridge

    xtx_inv = invert_matrix(xtx)
    xty = matmul(xt, y_col)
    beta_col = matmul(xtx_inv, xty)
    beta = [b[0] for b in beta_col]

    yhat = [sum(beta[j] * x_i[r][j] for j in range(k)) for r in range(n)]
    resid = [float(y[r]) - yhat[r] for r in range(n)]

    dof = max(n - k, 1)
    sse = sum(e * e for e in resid)
    sigma2 = sse / dof

    cov_beta = [[sigma2 * v for v in row] for row in xtx_inv]
    se = [math.sqrt(max(cov_beta[i][i], 0.0)) for i in range(k)]

    t_stats = []
    p_values = []
    for b, s in zip(beta, se):
        if s <= 1e-12:
            t = 0.0
            p = 1.0
        else:
            t = b / s
            p = 2.0 * (1.0 - normal_cdf(abs(t)))
        t_stats.append(t)
        p_values.append(p)

    return {
        "beta": beta,
        "se": se,
        "t": t_stats,
        "p": p_values,
        "sigma2": sigma2,
    }


def predict(beta: List[float], x: List[List[float]]) -> List[float]:
    out = []
    for row in x:
        yhat = beta[0] + sum(beta[j + 1] * row[j] for j in range(len(row)))
        yhat = min(1.0, max(0.0, yhat))
        out.append(yhat)
    return out


def accuracy(y_true: List[int], y_prob: List[float], threshold: float = 0.5) -> float:
    correct = sum((1 if p >= threshold else 0) == y for y, p in zip(y_true, y_prob))
    return correct / len(y_true) if y_true else 0.0


def brier(y_true: List[int], y_prob: List[float]) -> float:
    if not y_true:
        return 0.0
    return sum((p - y) ** 2 for y, p in zip(y_true, y_prob)) / len(y_true)


def log_loss(y_true: List[int], y_prob: List[float], eps: float = 1e-9) -> float:
    if not y_true:
        return 0.0
    s = 0.0
    for y, p in zip(y_true, y_prob):
        p = min(1.0 - eps, max(eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return s / len(y_true)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--data-root", default="data/markets")
    ap.add_argument("--step-seconds", type=int, default=30)
    ap.add_argument("--vol-window-seconds", type=int, default=60)
    ap.add_argument("--train-ratio", type=float, default=0.8)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)

    session_dir = Path(args.data_root) / args.market / "sessions"
    sessions = load_sessions(session_dir)
    if not sessions:
        raise SystemExit(f"No session files found in {session_dir}")

    # Build rows by session so we can split by session time (avoids leakage)
    session_rows: List[Tuple[datetime, List[Tuple[List[float], int, int]]]] = []
    skipped = 0

    for stamp, data, _path in sessions:
        outcome = str(data.get("outcome", "")).strip().lower()
        if outcome not in {"up", "down"}:
            skipped += 1
            continue
        y = 1 if outcome == "up" else 0
        rows = extract_rows_from_session(
            data,
            label=y,
            step_seconds=args.step_seconds,
            vol_window_seconds=args.vol_window_seconds,
        )
        if not rows:
            skipped += 1
            continue
        session_rows.append((stamp, rows))

    if len(session_rows) < 20:
        raise SystemExit("Not enough usable sessions to train/evaluate.")

    split_idx = int(len(session_rows) * args.train_ratio)
    split_idx = max(1, min(split_idx, len(session_rows) - 1))

    train_sessions = session_rows[:split_idx]
    test_sessions = session_rows[split_idx:]

    x_train, y_train, t_train = [], [], []
    x_test, y_test, t_test = [], [], []

    for _, rows in train_sessions:
        for x, y, t in rows:
            x_train.append(x)
            y_train.append(y)
            t_train.append(t)

    for _, rows in test_sessions:
        for x, y, t in rows:
            x_test.append(x)
            y_test.append(y)
            t_test.append(t)

    x_train_s, x_test_s, means, stds = standardize(x_train, x_test)

    fit = fit_ols_with_stats(x_train_s, y_train)
    beta = fit["beta"]

    p_train = predict(beta, x_train_s)
    p_test = predict(beta, x_test_s)

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

    print("=== Session Direction Model (Linear Probability via OLS) ===")
    print(f"Market: {args.market}")
    print(f"Session files read: {len(sessions)}")
    print(f"Usable sessions: {len(session_rows)} (skipped: {skipped})")
    print(f"Train sessions: {len(train_sessions)} | Test sessions: {len(test_sessions)}")
    print(f"Train rows: {len(x_train)} | Test rows: {len(x_test)}")
    print()

    print("--- Coefficients (standardized features) ---")
    print(f"{'Feature':38s} {'Coef':>10s} {'SE':>10s} {'t':>10s} {'p-value':>12s}")

    all_names = ["intercept"] + feature_names
    for name, b, se, t, p in zip(all_names, fit["beta"], fit["se"], fit["t"], fit["p"]):
        print(f"{name:38s} {b:10.4f} {se:10.4f} {t:10.3f} {p:12.4g}")

    print()
    print("--- Predictive performance ---")
    print(f"Train accuracy: {accuracy(y_train, p_train):.4f}")
    print(f"Train brier:    {brier(y_train, p_train):.4f}")
    print(f"Train logloss:  {log_loss(y_train, p_train):.4f}")
    print(f"Test accuracy:  {accuracy(y_test, p_test):.4f}")
    print(f"Test brier:     {brier(y_test, p_test):.4f}")
    print(f"Test logloss:   {log_loss(y_test, p_test):.4f}")

    # Accuracy by elapsed bucket on test data
    bucket_stats = defaultdict(lambda: [0, 0])  # bucket -> [correct, total]
    for t, y, p in zip(t_test, y_test, p_test):
        b = (t // max(args.step_seconds, 1)) * max(args.step_seconds, 1)
        pred = 1 if p >= 0.5 else 0
        bucket_stats[b][0] += int(pred == y)
        bucket_stats[b][1] += 1

    print()
    print("--- Test accuracy by time into session ---")
    print(f"{'Elapsed_s':>10s} {'Accuracy':>10s} {'N':>8s}")
    for b in sorted(bucket_stats.keys())[:25]:
        c, n = bucket_stats[b]
        print(f"{b:10d} {c/n:10.4f} {n:8d}")


if __name__ == "__main__":
    main()
