"""Tests for trading strategies via synthetic TickContexts."""

import pytest
from core.trader import HOLD, Position, TradeAction
from core.strategies.always_buy_up import AlwaysBuyUp
from core.strategies.always_buy_down import AlwaysBuyDown
from core.strategies.late_sniper import LateSniper
from core.strategies.fade_the_market import FadeTheMarket
from tests.conftest import make_tick_ctx


# ---------------------------------------------------------------------------
# AlwaysBuyUp
# ---------------------------------------------------------------------------


class TestAlwaysBuyUp:
    def test_buys_on_first_tick(self):
        s = AlwaysBuyUp(bet_fraction=0.5)
        s.on_session_start({})
        ctx = make_tick_ctx(position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_up"
        assert action.amount == pytest.approx(50.0)

    def test_holds_after_first_tick(self):
        s = AlwaysBuyUp()
        s.on_session_start({})
        s.on_tick(make_tick_ctx())
        action = s.on_tick(make_tick_ctx())
        assert action.side == "hold"

    def test_resets_between_sessions(self):
        s = AlwaysBuyUp()
        s.on_session_start({})
        s.on_tick(make_tick_ctx())
        # New session should reset
        s.on_session_start({})
        action = s.on_tick(make_tick_ctx())
        assert action.side == "buy_up"


# ---------------------------------------------------------------------------
# AlwaysBuyDown
# ---------------------------------------------------------------------------


class TestAlwaysBuyDown:
    def test_buys_down_on_first_tick(self):
        s = AlwaysBuyDown(bet_fraction=1.0)
        s.on_session_start({})
        ctx = make_tick_ctx(position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_down"
        assert action.amount == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# LateSniper
# ---------------------------------------------------------------------------


class TestLateSniper:
    def test_holds_before_min_elapsed(self):
        s = LateSniper(min_elapsed=0.7)
        s.on_session_start({})
        ctx = make_tick_ctx(elapsed_fraction=0.5)
        assert s.on_tick(ctx).side == "hold"

    def test_buys_up_when_confident(self):
        s = LateSniper(min_elapsed=0.7, confidence_threshold=0.70, max_price=0.90)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.75,
            "down_token_midpoint": 0.25,
            "elapsed_seconds": 240.0,
        }
        ctx = make_tick_ctx(
            sample=sample,
            elapsed_fraction=0.8,
            position=Position(cash=100.0),
        )
        action = s.on_tick(ctx)
        assert action.side == "buy_up"

    def test_buys_down_when_confident(self):
        s = LateSniper(min_elapsed=0.7, confidence_threshold=0.70, max_price=0.90)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.25,
            "down_token_midpoint": 0.75,
            "elapsed_seconds": 240.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.8, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_down"

    def test_holds_if_above_max_price(self):
        s = LateSniper(min_elapsed=0.7, confidence_threshold=0.70, max_price=0.80)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.85,
            "down_token_midpoint": 0.15,
            "elapsed_seconds": 240.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.8)
        assert s.on_tick(ctx).side == "hold"

    def test_holds_after_entry(self):
        s = LateSniper(min_elapsed=0.7, confidence_threshold=0.70, max_price=0.90)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.75,
            "down_token_midpoint": 0.25,
            "elapsed_seconds": 240.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.8, position=Position(cash=100.0))
        s.on_tick(ctx)  # enters
        assert s.on_tick(ctx).side == "hold"  # already entered

    def test_holds_when_midpoints_none(self):
        s = LateSniper(min_elapsed=0.7)
        s.on_session_start({})
        sample = {"up_token_midpoint": None, "down_token_midpoint": None, "elapsed_seconds": 240.0}
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.8)
        assert s.on_tick(ctx).side == "hold"

    def test_param_grid_has_expected_keys(self):
        s = LateSniper()
        grid = s.param_grid()
        assert "min_elapsed" in grid
        assert "confidence_threshold" in grid
        assert "max_price" in grid
        assert "bet_fraction" in grid


# ---------------------------------------------------------------------------
# FadeTheMarket
# ---------------------------------------------------------------------------


class TestFadeTheMarket:
    def test_fades_high_up(self):
        from core.strategies.fade_the_market import FadeTheMarket
        s = FadeTheMarket(overreaction_threshold=0.70)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.80,
            "down_token_midpoint": 0.20,
            "elapsed_seconds": 60.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.2, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_down"

    def test_fades_high_down(self):
        from core.strategies.fade_the_market import FadeTheMarket
        s = FadeTheMarket(overreaction_threshold=0.70)
        s.on_session_start({})
        sample = {
            "up_token_midpoint": 0.20,
            "down_token_midpoint": 0.80,
            "elapsed_seconds": 60.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.2, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_up"
