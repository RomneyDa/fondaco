"""Tests for core/math_utils.py: norm_cdf, ols_slope_bps, ols_slope_bps_timed."""

import math
import pytest
from core.math_utils import norm_cdf, ols_slope_bps, ols_slope_bps_timed


# ---------------------------------------------------------------------------
# norm_cdf
# ---------------------------------------------------------------------------


class TestNormCdf:
    def test_zero(self):
        assert norm_cdf(0) == pytest.approx(0.5)

    def test_large_positive(self):
        assert norm_cdf(5.0) == pytest.approx(1.0, abs=1e-6)

    def test_large_negative(self):
        assert norm_cdf(-5.0) == pytest.approx(0.0, abs=1e-6)

    def test_symmetry(self):
        assert norm_cdf(1.0) + norm_cdf(-1.0) == pytest.approx(1.0)

    def test_one_sigma(self):
        # P(Z < 1) ≈ 0.8413
        assert norm_cdf(1.0) == pytest.approx(0.8413, abs=0.001)


# ---------------------------------------------------------------------------
# ols_slope_bps
# ---------------------------------------------------------------------------


class TestOlsSlopeBps:
    def test_uptrend(self):
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        slope = ols_slope_bps(prices, 5)
        assert slope is not None
        assert slope > 0

    def test_downtrend(self):
        prices = [104.0, 103.0, 102.0, 101.0, 100.0]
        slope = ols_slope_bps(prices, 5)
        assert slope is not None
        assert slope < 0

    def test_flat(self):
        prices = [100.0, 100.0, 100.0, 100.0, 100.0]
        slope = ols_slope_bps(prices, 5)
        assert slope is not None
        assert slope == pytest.approx(0.0)

    def test_insufficient_data(self):
        prices = [100.0, 101.0]
        assert ols_slope_bps(prices, 5) is None

    def test_base_zero(self):
        prices = [0.0, 1.0, 2.0]
        assert ols_slope_bps(prices, 3) is None

    def test_uses_last_window(self):
        # Only the last 3 prices should matter
        prices = [50.0, 100.0, 101.0, 102.0]
        slope = ols_slope_bps(prices, 3)
        assert slope is not None
        assert slope > 0


# ---------------------------------------------------------------------------
# ols_slope_bps_timed
# ---------------------------------------------------------------------------


class TestOlsSlopeBpsTimed:
    def test_uptrend(self):
        times = [0.0, 1.0, 2.0, 3.0, 4.0]
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        slope = ols_slope_bps_timed(times, prices)
        assert slope is not None
        assert slope > 0

    def test_downtrend(self):
        times = [0.0, 1.0, 2.0, 3.0, 4.0]
        prices = [104.0, 103.0, 102.0, 101.0, 100.0]
        slope = ols_slope_bps_timed(times, prices)
        assert slope is not None
        assert slope < 0

    def test_flat(self):
        times = [0.0, 1.0, 2.0, 3.0, 4.0]
        prices = [100.0, 100.0, 100.0, 100.0, 100.0]
        slope = ols_slope_bps_timed(times, prices)
        assert slope is not None
        assert slope == pytest.approx(0.0)

    def test_insufficient_data(self):
        assert ols_slope_bps_timed([0.0], [100.0]) is None

    def test_base_zero(self):
        assert ols_slope_bps_timed([0.0, 1.0, 2.0], [0.0, 1.0, 2.0]) is None

    def test_mismatched_lengths(self):
        assert ols_slope_bps_timed([0.0, 1.0], [100.0]) is None

    def test_returns_bps_per_second(self):
        # Price goes from 100 to 110 over 10 seconds = 1 $/s = 100 bps/s
        times = [0.0, 10.0]
        prices = [100.0, 110.0]
        slope = ols_slope_bps_timed(times, prices)
        assert slope is not None
        assert slope == pytest.approx(100.0)
