"""Tests for active trading strategies: entry, exit, timing, state reset."""

import pytest
from core.trader import HOLD, Position, TradeAction
from core.strategies.base import Strategy
from tests.conftest import make_tick_ctx

from bots.updown.drift_sniper import DriftSniper
from bots.updown.gap_fader import GapFader
from bots.updown.poisson_kelly import PoissonKelly


ALL_STRATS = [DriftSniper, GapFader, PoissonKelly]


@pytest.mark.parametrize("StratCls", ALL_STRATS, ids=lambda c: c.name)
class TestCommonBehavior:
    """Tests shared across all active strategies."""

    def test_requires_full_session(self, StratCls):
        assert StratCls.requires_full_session is True

    def test_param_grid_returns_dict(self, StratCls):
        s = StratCls()
        grid = s.param_grid()
        assert isinstance(grid, dict)
        # All grid keys should be tunable params
        defs = s.param_defs()
        for key in grid:
            assert key in defs

    def test_holds_before_min_elapsed(self, StratCls):
        s = StratCls()
        s.on_session_start({})
        ctx = make_tick_ctx(
            elapsed_fraction=0.05,
            position=Position(cash=100.0),
            sample={"btc_price_usd": 97000.0, "up_token_midpoint": 0.50,
                    "down_token_midpoint": 0.50, "elapsed_seconds": 15.0},
            initial_btc_price=97000.0,
        )
        assert s.on_tick(ctx).side == "hold"


# ---------------------------------------------------------------------------
# DriftSniper
# ---------------------------------------------------------------------------


class TestDriftSniper:
    def test_session_start_resets(self):
        s = DriftSniper()
        s._entry_count = 1
        s.on_session_start({})
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._btc_prices == []

    def test_holds_with_no_drift(self):
        """No BTC drift -> no entry."""
        s = DriftSniper(min_elapsed=0.50, return_threshold_bps=4.0)
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
        s = DriftSniper()
        grid = s.param_grid()
        assert "return_threshold_bps" in grid
        assert "min_elapsed" in grid


# ---------------------------------------------------------------------------
# GapFader
# ---------------------------------------------------------------------------


class TestGapFader:
    def test_session_start_resets(self):
        s = GapFader()
        s._entry_count = 3
        s.on_session_start({"window_duration": 300})
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._active is False
        assert s._gap_samples == []

    def test_observation_phase_holds(self):
        """During first 15s (observation), strategy should hold."""
        s = GapFader(observe_seconds=15.0)
        s.on_session_start({"window_duration": 300})
        ctx = make_tick_ctx(
            elapsed_fraction=0.03,
            position=Position(cash=100.0),
            sample={"up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
                    "elapsed_seconds": 10.0},
        )
        assert s.on_tick(ctx).side == "hold"

    def test_deactivates_on_large_gap(self):
        """Large initial gap -> strategy stays inactive for session."""
        s = GapFader(gap_threshold=0.10, observe_seconds=15.0)
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
        s = GapFader()
        grid = s.param_grid()
        assert "gap_threshold" in grid
        assert "max_buy_price" in grid


# ---------------------------------------------------------------------------
# PoissonKelly
# ---------------------------------------------------------------------------


class TestPoissonKelly:
    def test_session_start_resets(self):
        s = PoissonKelly()
        s._up_jumps = 10
        s._down_jumps = 5
        s._entry_count = 1
        s.on_session_start({})
        assert s._up_jumps == 0
        assert s._down_jumps == 0
        assert s._entry_count == 0
        assert s._entry_midpoint == 0.0
        assert s._stopped_out is False

    def test_holds_with_no_jumps(self):
        """Flat BTC -> no jumps -> no signal -> hold."""
        s = PoissonKelly(min_elapsed=0.50, jump_bps=0.10)
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

    def test_param_grid_keys(self):
        s = PoissonKelly()
        grid = s.param_grid()
        assert "jump_bps" in grid
        assert "edge_min" in grid
        assert "min_jumps" not in grid  # not tunable
