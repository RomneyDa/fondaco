"""Tests for core/volatility.py: session volatility computations."""

import pytest
from core.volatility import compute_session_volatility, compute_signed_volatility


def _make_summaries(returns_bps, base_price=97000.0):
    """Build summary dicts from a list of absolute return values in bps."""
    summaries = []
    for r in returns_bps:
        final = base_price * (1.0 + r / 10000.0)
        summaries.append({"initial_btc": base_price, "final_btc": final})
    return summaries


# ---------------------------------------------------------------------------
# compute_session_volatility
# ---------------------------------------------------------------------------


class TestComputeSessionVolatility:
    def test_normal_case(self):
        summaries = _make_summaries([10, -20, 15, -5, 30])
        result = compute_session_volatility(summaries, lookback=5, min_sessions=3)
        assert result is not None
        mean_abs, std = result
        assert mean_abs > 0
        assert std >= 0

    def test_insufficient_sessions(self):
        summaries = _make_summaries([10, 20])
        result = compute_session_volatility(summaries, lookback=5, min_sessions=3)
        assert result is None

    def test_none_input(self):
        assert compute_session_volatility(None, lookback=5) is None

    def test_lookback_limits(self):
        summaries = _make_summaries([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        result = compute_session_volatility(summaries, lookback=3, min_sessions=3)
        assert result is not None

    def test_empty_list(self):
        assert compute_session_volatility([], lookback=5, min_sessions=3) is None

    def test_zero_initial_btc_filtered(self):
        """Sessions with initial_btc=0 are filtered out."""
        summaries = [
            {"initial_btc": 0, "final_btc": 100},
            {"initial_btc": 97000, "final_btc": 97010},
            {"initial_btc": 97000, "final_btc": 96990},
            {"initial_btc": 97000, "final_btc": 97020},
        ]
        # lookback=3 → min_required = max(3, int(3*0.9)) = 3, and we have 3 valid
        result = compute_session_volatility(summaries, lookback=3, min_sessions=3)
        assert result is not None


# ---------------------------------------------------------------------------
# compute_signed_volatility
# ---------------------------------------------------------------------------


class TestComputeSignedVolatility:
    def test_positive_drift(self):
        summaries = _make_summaries([10, 20, 30])
        result = compute_signed_volatility(summaries, lookback=3, min_sessions=3)
        assert result is not None
        mean, std = result
        assert mean > 0

    def test_negative_drift(self):
        summaries = _make_summaries([-10, -20, -30])
        result = compute_signed_volatility(summaries, lookback=3, min_sessions=3)
        assert result is not None
        mean, std = result
        assert mean < 0

    def test_insufficient_data(self):
        summaries = _make_summaries([10])
        result = compute_signed_volatility(summaries, lookback=5, min_sessions=3)
        assert result is None

    def test_none_input(self):
        assert compute_signed_volatility(None, lookback=5) is None
