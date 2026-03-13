"""Tests for regime classification and regime-gated strategies."""

import pytest
from core.volatility import (
    classify_vol_regime,
    classify_trend_regime,
    classify_regime,
    VOL_LOW_THRESHOLD_BPS,
    VOL_HIGH_THRESHOLD_BPS,
)
from core.trader import HOLD, Position
from tests.conftest import make_tick_ctx


def _make_summaries(returns_bps, base_price=97000.0):
    """Build summary dicts from a list of signed return values in bps."""
    summaries = []
    for r in returns_bps:
        final = base_price * (1.0 + r / 10000.0)
        summaries.append({"initial_btc": base_price, "final_btc": final})
    return summaries


# ---------------------------------------------------------------------------
# classify_vol_regime
# ---------------------------------------------------------------------------


class TestClassifyVolRegime:
    def test_low_vol(self):
        """All small moves → low vol."""
        summaries = _make_summaries([1.0, -1.5, 0.5, 2.0, -0.8, 1.2, 0.3])
        assert classify_vol_regime(summaries, lookback=7, min_sessions=5) == "low"

    def test_high_vol(self):
        """All large moves → high vol."""
        summaries = _make_summaries([15.0, -20.0, 12.0, -18.0, 25.0, -10.0, 14.0])
        assert classify_vol_regime(summaries, lookback=7, min_sessions=5) == "high"

    def test_medium_vol(self):
        """Moderate moves → medium vol."""
        summaries = _make_summaries([5.0, -6.0, 4.0, -5.0, 7.0, -4.0, 5.5])
        assert classify_vol_regime(summaries, lookback=7, min_sessions=5) == "medium"

    def test_none_on_insufficient_data(self):
        summaries = _make_summaries([5.0, 6.0])
        assert classify_vol_regime(summaries, lookback=5, min_sessions=5) is None

    def test_none_input(self):
        assert classify_vol_regime(None) is None


# ---------------------------------------------------------------------------
# classify_trend_regime
# ---------------------------------------------------------------------------


class TestClassifyTrendRegime:
    def test_trending(self):
        """Persistent direction → trending."""
        # Runs of same sign: strong positive autocorrelation
        summaries = _make_summaries([5, 5, 5, 5, -5, -5, -5, -5, 5, 5])
        result = classify_trend_regime(summaries, lookback=10, min_sessions=5)
        assert result == "trending"

    def test_mean_reverting(self):
        """Alternating direction → mean-reverting."""
        summaries = _make_summaries([5, -5, 5, -5, 5, -5, 5, -5, 5, -5])
        result = classify_trend_regime(summaries, lookback=10, min_sessions=5)
        assert result == "mean_reverting"

    def test_flat(self):
        """Near-zero variance → flat."""
        # All exactly the same value — variance = 0, returns "flat"
        summaries = _make_summaries([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        result = classify_trend_regime(summaries, lookback=6, min_sessions=5)
        assert result == "flat"

    def test_none_on_insufficient_data(self):
        summaries = _make_summaries([5.0])
        assert classify_trend_regime(summaries, lookback=5, min_sessions=5) is None


# ---------------------------------------------------------------------------
# classify_regime (combined)
# ---------------------------------------------------------------------------


class TestClassifyRegime:
    def test_clean_data(self):
        """Normal metadata → clean data quality."""
        summaries = _make_summaries([5.0, -6.0, 4.0, -5.0, 7.0, -4.0, 5.5] * 4)
        metadata = {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 300.0,
            "_data_density": 0.95,
        }
        result = classify_regime(metadata)
        assert result["data_quality"] == "clean"
        assert result["vol"] is not None

    def test_stale_on_large_gap(self):
        """Large gap → stale data quality."""
        summaries = _make_summaries([5.0] * 10)
        metadata = {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 7200.0,  # 2 hours
            "_data_density": 0.95,
        }
        result = classify_regime(metadata, max_gap_s=600)
        assert result["data_quality"] == "stale"

    def test_low_density_clean_by_default(self):
        """Low density without min_density → clean (density is opt-in)."""
        summaries = _make_summaries([5.0] * 10)
        metadata = {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 300.0,
            "_data_density": 0.50,
        }
        result = classify_regime(metadata)
        assert result["data_quality"] == "clean"
        assert result["density"] == 0.50  # density still reported

    def test_stale_on_low_density_with_threshold(self):
        """Low density with explicit min_density → stale."""
        summaries = _make_summaries([5.0] * 10)
        metadata = {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 300.0,
            "_data_density": 0.50,
        }
        result = classify_regime(metadata, min_density=0.80)
        assert result["data_quality"] == "stale"


# ---------------------------------------------------------------------------
# RegimeGatedStrategy
# ---------------------------------------------------------------------------


class TestRegimeGatedStrategy:
    def _make_trending_high_vol(self):
        """Metadata that classifies as trending + high vol."""
        summaries = _make_summaries([15, 15, 15, 15, -15, -15, -15, -15, 15, 15,
                                     12, 12, 12, 12, -12, -12, -12, -12, 12, 12])
        return {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 300.0,
            "_data_density": 0.95,
        }

    def _make_flat_low_vol(self):
        """Metadata that classifies as flat + low vol."""
        summaries = _make_summaries([0.5, -0.5, 0.5, -0.5, 0.5,
                                     -0.5, 0.5, -0.5, 0.5, -0.5,
                                     0.5, -0.5, 0.5, -0.5, 0.5,
                                     -0.5, 0.5, -0.5, 0.5, -0.5])
        return {
            "_prior_session_summaries": summaries,
            "_gap_before_seconds": 300.0,
            "_data_density": 0.95,
        }

    def test_trend_drift_activates_in_trending(self):
        from bots.updown.trend_drift import TrendDrift
        s = TrendDrift()
        s.on_session_start(self._make_trending_high_vol())
        assert s._regime_active is True

    def test_trend_drift_skips_flat(self):
        from bots.updown.trend_drift import TrendDrift
        s = TrendDrift()
        s.on_session_start(self._make_flat_low_vol())
        assert s._regime_active is False

    def test_mean_rev_skips_trending(self):
        from bots.updown.mean_rev_fader import MeanRevFader
        s = MeanRevFader()
        s.on_session_start(self._make_trending_high_vol())
        assert s._regime_active is False

    def test_vol_breakout_activates_high_vol(self):
        from bots.updown.vol_breakout import VolBreakout
        s = VolBreakout()
        s.on_session_start(self._make_trending_high_vol())
        assert s._regime_active is True

    def test_vol_breakout_skips_low_vol(self):
        from bots.updown.vol_breakout import VolBreakout
        s = VolBreakout()
        s.on_session_start(self._make_flat_low_vol())
        assert s._regime_active is False

    def test_regime_gated_holds_when_inactive(self):
        """When regime doesn't match, on_tick returns HOLD."""
        from bots.updown.trend_drift import TrendDrift
        s = TrendDrift()
        s.on_session_start(self._make_flat_low_vol())
        ctx = make_tick_ctx(
            elapsed_fraction=0.50,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97100.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 150.0},
            initial_btc_price=97000.0,
        )
        assert s.on_tick(ctx).side == "hold"

    def test_stale_data_skips(self):
        """Stale data quality → regime-gated strategy deactivates."""
        from bots.updown.trend_drift import TrendDrift
        s = TrendDrift()
        metadata = self._make_trending_high_vol()
        metadata["_window_duration"] = 300  # 5-min market
        metadata["_gap_before_seconds"] = 10000.0  # huge gap
        s.on_session_start(metadata)
        assert s._regime_active is False

    def test_confirmed_late_param_grid(self):
        """ConfirmedLate should have expected tunable params."""
        from bots.updown.confirmed_late import ConfirmedLate
        s = ConfirmedLate()
        grid = s.param_grid()
        assert "min_elapsed" in grid
        assert "confidence_threshold" in grid

    def test_all_regime_strategies_registered(self):
        """All regime-gated strategies should be in the registry."""
        from bots import ALL_STRATEGIES
        from core.strategies.base import Strategy
        for name in ["TrendDrift", "MeanRevFader", "VolBreakout", "ConfirmedLate"]:
            assert name in ALL_STRATEGIES
            assert issubclass(ALL_STRATEGIES[name], Strategy)
