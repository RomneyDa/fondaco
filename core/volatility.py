"""Session-level volatility computations shared across strategies."""

from typing import Any, Dict, List, Optional, Tuple

from core.defaults import BPS_DIVISOR


def compute_session_volatility(prior_summaries, lookback, min_sessions=3):
    # type: (Optional[List[Dict[str, Any]]], int, int) -> Optional[Tuple[float, float]]
    """Compute mean and std of absolute session returns in bps.

    Args:
        prior_summaries: list of dicts with 'initial_btc' and 'final_btc'
        lookback: number of recent sessions to use
        min_sessions: minimum required sessions (also requires 90% of lookback)

    Returns (mean_abs_bps, std_bps) or None if insufficient data.
    """
    if prior_summaries is None:
        return None

    valid = [s for s in prior_summaries
             if s.get("initial_btc") and s.get("final_btc")
             and s["initial_btc"] > 0]
    recent = valid[-lookback:]

    min_required = max(min_sessions, int(lookback * 0.9))
    if len(recent) < min_required:
        return None

    abs_returns_bps = []
    for s in recent:
        ret = abs(s["final_btc"] - s["initial_btc"]) / s["initial_btc"] * BPS_DIVISOR
        abs_returns_bps.append(ret)

    mean_ret = sum(abs_returns_bps) / len(abs_returns_bps)
    if len(abs_returns_bps) > 1:
        variance = sum((r - mean_ret) ** 2 for r in abs_returns_bps) / (len(abs_returns_bps) - 1)
        vol_std = variance ** 0.5
    else:
        vol_std = mean_ret

    return (mean_ret, vol_std)


def compute_signed_volatility(prior_summaries, lookback, min_sessions=3):
    # type: (Optional[List[Dict[str, Any]]], int, int) -> Optional[Tuple[float, float]]
    """Compute mean and population std of signed session returns in bps.

    Used by PoissonKellyDynamic for vol-regime scaling.

    Returns (mean_bps, std_bps) or None if insufficient data.
    """
    if prior_summaries is None:
        return None

    valid = []
    for s in prior_summaries:
        initial = s.get("initial_btc") or 0.0
        final = s.get("final_btc") or 0.0
        if initial > 0:
            valid.append((final / initial - 1.0) * BPS_DIVISOR)

    recent = valid[-lookback:]

    min_required = max(min_sessions, int(lookback * 0.9))
    if len(recent) < min_required:
        return None

    mean_ret = sum(recent) / len(recent)
    variance = sum((r - mean_ret) ** 2 for r in recent) / len(recent)
    vol_std = variance ** 0.5

    return (mean_ret, vol_std)
