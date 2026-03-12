"""Tests for active trading strategies: entry, exit, timing, state reset."""

import pytest
from core.trader import HOLD, Position, TradeAction
from core.strategies.base import Strategy
from tests.conftest import make_tick_ctx


# ---------------------------------------------------------------------------
# WaveRider
# ---------------------------------------------------------------------------


class TestWaveRider:
    def _make(self, **kw):
        from bots.updown.wave_rider import WaveRider
        return WaveRider(**kw)

    def test_session_start_resets_state(self):
        s = self._make()
        s._entry_count = 5
        s._entry_midpoint = 0.60
        s.on_session_start({})
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._trend_dir is None
        assert s._btc_prices == []

    def test_holds_before_min_elapsed(self):
        s = self._make(min_elapsed=0.30)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.1,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 30.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_holds_after_no_new_after(self):
        s = self._make(no_new_after=0.95)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.96,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 288.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_param_grid_has_tunable_keys(self):
        s = self._make()
        grid = s.param_grid()
        assert "trend_threshold_bps" in grid
        assert "pullback_bps" in grid
        assert "trend_window" not in grid  # not tunable

    def test_requires_full_session(self):
        from bots.updown.wave_rider import WaveRider
        assert WaveRider.requires_full_session is True


# ---------------------------------------------------------------------------
# DriftSniper
# ---------------------------------------------------------------------------


class TestDriftSniper:
    def _make(self, **kw):
        from bots.updown.drift_sniper import DriftSniper
        return DriftSniper(**kw)

    def test_session_start_resets(self):
        s = self._make()
        s._entry_count = 1
        s.on_session_start({})
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._btc_prices == []

    def test_holds_before_min_elapsed(self):
        s = self._make(min_elapsed=0.50)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.2,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 60.0},
            initial_btc_price=97000.0,
        )
        assert s.on_tick(ctx).side == "hold"

    def test_holds_with_no_drift(self):
        """No BTC drift → no entry."""
        s = self._make(min_elapsed=0.50, return_threshold_bps=4.0)
        s.on_session_start({})
        # Feed flat BTC prices to build history
        for i in range(200):
            ctx = make_tick_ctx(
                tick_index=i,
                elapsed_fraction=0.1 + i * 0.004,
                position=Position(cash=100.0),
                sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                        "down_token_midpoint": 0.50, "elapsed_seconds": 30.0 + i * 1.0},
                initial_btc_price=97000.0,
            )
            action = s.on_tick(ctx)
        assert action.side == "hold"

    def test_param_grid_keys(self):
        s = self._make()
        grid = s.param_grid()
        assert "return_threshold_bps" in grid
        assert "min_elapsed" in grid

    def test_requires_full_session(self):
        from bots.updown.drift_sniper import DriftSniper
        assert DriftSniper.requires_full_session is True


# ---------------------------------------------------------------------------
# GapFader
# ---------------------------------------------------------------------------


class TestGapFader:
    def _make(self, **kw):
        from bots.updown.gap_fader import GapFader
        return GapFader(**kw)

    def test_session_start_resets(self):
        s = self._make()
        s._entry_count = 3
        s.on_session_start({"window_duration": 300})
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._active is False
        assert s._gap_samples == []

    def test_observation_phase_holds(self):
        """During first 15s (observation), strategy should hold."""
        s = self._make(observe_seconds=15.0)
        s.on_session_start({"window_duration": 300})
        ctx = make_tick_ctx(
            elapsed_fraction=0.03,
            position=Position(cash=100.0),
            sample={"up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
                    "elapsed_seconds": 10.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_deactivates_on_large_gap(self):
        """Large initial gap → strategy stays inactive for session."""
        s = self._make(gap_threshold=0.10, observe_seconds=15.0)
        s.on_session_start({"window_duration": 300})
        # Feed observation samples with large gap
        for i in range(10):
            ctx = make_tick_ctx(
                tick_index=i,
                elapsed_fraction=i * 0.01,
                position=Position(cash=100.0),
                sample={"up_token_midpoint": 0.80, "down_token_midpoint": 0.20,
                        "elapsed_seconds": i * 1.5},
            )
            s.on_tick(ctx)
        # Past observation: should be inactive
        ctx = make_tick_ctx(
            tick_index=20,
            elapsed_fraction=0.10,
            position=Position(cash=100.0),
            sample={"up_token_midpoint": 0.40, "down_token_midpoint": 0.40,
                    "elapsed_seconds": 30.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_param_grid_keys(self):
        s = self._make()
        grid = s.param_grid()
        assert "gap_threshold" in grid
        assert "max_buy_price" in grid

    def test_requires_full_session(self):
        from bots.updown.gap_fader import GapFader
        assert GapFader.requires_full_session is True


# ---------------------------------------------------------------------------
# LateSniperSafe
# ---------------------------------------------------------------------------


class TestLateSniperSafe:
    def _make(self, **kw):
        from bots.updown.late_sniper_safe import LateSniperSafe
        return LateSniperSafe(**kw)

    def test_session_start_resets(self):
        s = self._make()
        s._entry_count = 1
        s._confirm_count = 10
        s.on_session_start({})
        assert s._entry_count == 0
        assert s._confirm_count == 0
        assert s._confirm_side is None

    def test_holds_before_min_elapsed(self):
        s = self._make(min_elapsed=0.85)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.50,
            position=Position(cash=100.0),
            sample={"up_token_midpoint": 0.95, "down_token_midpoint": 0.05,
                    "elapsed_seconds": 150.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_holds_below_confidence(self):
        """Below confidence threshold → hold even if timing is right."""
        s = self._make(min_elapsed=0.85, confidence_threshold=0.90)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.90,
            position=Position(cash=100.0),
            sample={"up_token_midpoint": 0.80, "down_token_midpoint": 0.20,
                    "elapsed_seconds": 270.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_enters_after_confirmation(self):
        """After enough confirm ticks at high confidence, should buy."""
        s = self._make(min_elapsed=0.85, confidence_threshold=0.90,
                       max_price=0.98, confirm_ticks=3)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.95, "down_token_midpoint": 0.05,
            "up_token_best_ask": 0.96, "down_token_best_ask": 0.06,
            "elapsed_seconds": 270.0,
        }
        for i in range(3):
            ctx = make_tick_ctx(
                tick_index=i,
                elapsed_fraction=0.90,
                position=Position(cash=100.0),
                sample=sample,
            )
            action = s.on_tick(ctx)
        # After 3 confirm ticks, should enter
        assert action.side == "buy_up"

    def test_no_double_entry(self):
        """After entry, should not enter again."""
        s = self._make(min_elapsed=0.85, confidence_threshold=0.90,
                       max_price=0.98, confirm_ticks=1)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.95, "down_token_midpoint": 0.05,
            "up_token_best_ask": 0.96, "down_token_best_ask": 0.06,
            "elapsed_seconds": 270.0,
        }
        # First tick: enters
        ctx = make_tick_ctx(
            tick_index=0, elapsed_fraction=0.90,
            position=Position(cash=100.0), sample=sample,
        )
        action = s.on_tick(ctx)
        assert action.side == "buy_up"

        # Second tick: already entered, holds
        ctx2 = make_tick_ctx(
            tick_index=1, elapsed_fraction=0.91,
            position=Position(cash=50.0, up_tokens=50.0), sample=sample,
        )
        assert s.on_tick(ctx2).side == "hold"

    def test_requires_full_session_false(self):
        from bots.updown.late_sniper_safe import LateSniperSafe
        assert LateSniperSafe.requires_full_session is False

    def test_param_grid_keys(self):
        s = self._make()
        grid = s.param_grid()
        assert "min_elapsed" in grid
        assert "confidence_threshold" in grid


# ---------------------------------------------------------------------------
# PoissonKelly
# ---------------------------------------------------------------------------


class TestPoissonKelly:
    def _make(self, **kw):
        from bots.updown.poisson_kelly import PoissonKelly
        return PoissonKelly(**kw)

    def test_session_start_resets(self):
        s = self._make()
        s._up_jumps = 10
        s._down_jumps = 5
        s._entry_count = 1
        s.on_session_start({})
        assert s._up_jumps == 0
        assert s._down_jumps == 0
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._stopped_out is False

    def test_holds_before_min_elapsed(self):
        s = self._make(min_elapsed=0.65)
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.30,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 90.0},
            initial_btc_price=97000.0,
        )
        assert s.on_tick(ctx).side == "hold"

    def test_holds_with_no_jumps(self):
        """Flat BTC → no jumps → no signal → hold."""
        s = self._make(min_elapsed=0.50, jump_bps=0.10)
        s.on_session_start({})
        for i in range(100):
            ctx = make_tick_ctx(
                tick_index=i,
                elapsed_fraction=0.50 + i * 0.004,
                position=Position(cash=100.0),
                sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                        "down_token_midpoint": 0.50, "elapsed_seconds": 150.0 + i},
                initial_btc_price=97000.0,
            )
            action = s.on_tick(ctx)
        assert action.side == "hold"

    def test_requires_full_session(self):
        from bots.updown.poisson_kelly import PoissonKelly
        assert PoissonKelly.requires_full_session is True

    def test_param_grid_keys(self):
        s = self._make()
        grid = s.param_grid()
        assert "jump_bps" in grid
        assert "edge_min" in grid
        assert "min_jumps" not in grid  # not tunable
