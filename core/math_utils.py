"""Shared math utilities for trading strategies."""

import math

from core.defaults import BPS_DIVISOR


def norm_cdf(x):
    # type: (float) -> float
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ols_slope_bps(prices, window):
    # type: (list, int) -> float
    """OLS slope of the last `window` prices, returned in bps/tick.

    Returns None if insufficient data or degenerate input.
    """
    if len(prices) < window:
        return None
    recent = prices[-window:]
    base = recent[0]
    if base <= 0:
        return None
    n = window
    sum_x = n * (n - 1) / 2.0
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6.0
    sum_y = sum(recent)
    sum_xy = sum(i * y for i, y in enumerate(recent))
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return (slope / base) * BPS_DIVISOR


def ols_slope_bps_timed(times, prices):
    # type: (list, list) -> float
    """OLS slope of prices vs time, returned in bps/second.

    Args:
        times: list of elapsed_seconds values.
        prices: list of prices corresponding to times.

    Returns bps/second or None if insufficient data.
    """
    n = len(prices)
    if n < 2 or len(times) != n:
        return None
    base = prices[0]
    if base <= 0:
        return None
    sum_x = sum(times)
    sum_x2 = sum(t * t for t in times)
    sum_y = sum(prices)
    sum_xy = sum(t * p for t, p in zip(times, prices))
    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    return (slope / base) * BPS_DIVISOR
