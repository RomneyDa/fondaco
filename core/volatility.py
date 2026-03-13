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


# ── Regime classification ─────────────────────────────────────────────


# Volatility regime thresholds (in bps of absolute session return).
# Derived from empirical distribution of 5-min BTC sessions.
# Low: price barely moves (coin-flip territory).
# High: strong directional moves (trend-following territory).
VOL_LOW_THRESHOLD_BPS = 3.0
VOL_HIGH_THRESHOLD_BPS = 8.0

# Autocorrelation threshold for trend vs mean-reversion classification.
# > threshold → trending (returns persist); < -threshold → mean-reverting.
AUTOCORR_TREND_THRESHOLD = 0.15


def classify_vol_regime(prior_summaries, lookback=20, min_sessions=5):
    # type: (Optional[List[Any]], int, int) -> Optional[str]
    """Classify volatility regime from prior sessions.

    Returns 'low', 'medium', or 'high', or None if insufficient data.
    """
    vol = compute_session_volatility(prior_summaries, lookback, min_sessions)
    if vol is None:
        return None
    mean_abs, _ = vol
    if mean_abs < VOL_LOW_THRESHOLD_BPS:
        return "low"
    if mean_abs >= VOL_HIGH_THRESHOLD_BPS:
        return "high"
    return "medium"


def classify_trend_regime(prior_summaries, lookback=20, min_sessions=5):
    # type: (Optional[List[Any]], int, int) -> Optional[str]
    """Classify trend regime using lag-1 autocorrelation of signed returns.

    Positive autocorrelation → outcomes persist (trending).
    Negative autocorrelation → outcomes reverse (mean-reverting).
    Near zero → flat/random.

    Returns 'trending', 'mean_reverting', or 'flat', or None if insufficient data.
    """
    if prior_summaries is None:
        return None

    returns = []  # type: List[float]
    for s in prior_summaries:
        initial = s.get("initial_btc") or 0.0
        final = s.get("final_btc") or 0.0
        if initial > 0:
            returns.append((final / initial - 1.0) * BPS_DIVISOR)

    recent = returns[-lookback:]
    if len(recent) < min_sessions:
        return None

    # Lag-1 autocorrelation
    mean_r = sum(recent) / len(recent)
    n = len(recent)
    var = sum((r - mean_r) ** 2 for r in recent) / n
    if var < 1e-12:
        return "flat"
    cov = sum((recent[i] - mean_r) * (recent[i + 1] - mean_r)
              for i in range(n - 1)) / (n - 1)
    autocorr = cov / var

    if autocorr > AUTOCORR_TREND_THRESHOLD:
        return "trending"
    if autocorr < -AUTOCORR_TREND_THRESHOLD:
        return "mean_reverting"
    return "flat"


def classify_regime(metadata, lookback=20, min_sessions=5, min_density=None,
                    max_gap_s=None):
    # type: (dict, int, int, Optional[float], Optional[float]) -> dict
    """Classify the current market regime from session metadata.

    Args:
        min_density: Minimum data density to consider data "clean".
            None = only use gap detection (no density cutoff).
            Strategies can pass their own threshold here.
        max_gap_s: Max seconds between consecutive sessions before data is
            considered stale. Required for stale-data detection — callers
            should pass market_config.max_session_gap_seconds.

    Returns a dict with:
        vol: 'low' | 'medium' | 'high' | None
        trend: 'trending' | 'mean_reverting' | 'flat' | None
        data_quality: 'clean' | 'stale' | None
        density: float (fraction of expected sessions present)
        gap_seconds: float or None
    """
    prior = metadata.get("_prior_session_summaries")
    gap = metadata.get("_gap_before_seconds")
    density = metadata.get("_data_density", 1.0)

    vol = classify_vol_regime(prior, lookback, min_sessions)
    trend = classify_trend_regime(prior, lookback, min_sessions)

    # Data quality: stale if there's a large gap before this session.
    # Density check is opt-in per strategy — pass min_density to enable.
    if max_gap_s is not None and gap is not None and gap > max_gap_s:
        data_quality = "stale"
    elif min_density is not None and density < min_density:
        data_quality = "stale"
    else:
        data_quality = "clean"

    return {
        "vol": vol,
        "trend": trend,
        "data_quality": data_quality,
        "density": density,
        "gap_seconds": gap,
    }

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
