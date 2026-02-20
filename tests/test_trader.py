"""Tests for the Trader tick loop, guardrails, and session lifecycle."""

import pytest
from core.trader import (
    HOLD,
    Position,
    SimExecutor,
    Trade,
    TradeAction,
    Trader,
)
from core.strategies.base import Strategy


# ---------------------------------------------------------------------------
# Stub strategy for controlled testing
# ---------------------------------------------------------------------------


class StubStrategy(Strategy):
    """Returns a pre-configured action sequence."""

    name = "Stub"

    def __init__(self, actions=None):
        self.actions = actions or []
        self._tick = 0

    def on_session_start(self, metadata):
        self._tick = 0

    def on_tick(self, ctx):
        if self._tick < len(self.actions):
            action = self.actions[self._tick]
            self._tick += 1
            return action
        return HOLD


@pytest.fixture
def sample():
    return {
        "elapsed_seconds": 150.0,
        "up_token_midpoint": 0.50,
        "down_token_midpoint": 0.50,
        "up_token_best_bid": 0.49,
        "up_token_best_ask": 0.51,
        "down_token_best_bid": 0.49,
        "down_token_best_ask": 0.51,
        "btc_price_usd": 97000.0,
    }


@pytest.fixture
def metadata():
    return {"up_token_id": "up-123", "down_token_id": "down-456"}


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def test_start_resets_state(self, sample, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)

        trader.start_session(metadata, pos, 97000.0, 300.0)
        assert trader._tick_index == 0
        assert trader._history == []
        assert trader._trades == []

    def test_process_tick_increments_index(self, sample, metadata):
        strategy = StubStrategy([HOLD, HOLD])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        trader.process_tick(sample)
        assert trader._tick_index == 1
        trader.process_tick(sample)
        assert trader._tick_index == 2

    def test_history_accumulates(self, sample, metadata):
        strategy = StubStrategy([HOLD])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        trader.process_tick(sample)
        assert len(trader._history) == 1

    def test_end_session_resolves(self, metadata):
        strategy = StubStrategy([TradeAction(side="buy_up", amount=100.0)])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        sample = {
            "elapsed_seconds": 150.0,
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "btc_price_usd": 97000.0,
        }
        trader.start_session(metadata, pos, 97000.0, 300.0)
        trader.process_tick(sample)

        trades = trader.end_session("Up")
        # After resolve("Up"): cash += up_tokens, tokens -> 0
        assert pos.up_tokens == 0.0
        assert pos.cash > 0


# ---------------------------------------------------------------------------
# Elapsed fraction
# ---------------------------------------------------------------------------


class TestElapsedFraction:
    def test_normal(self, metadata):
        strategy = StubStrategy([HOLD])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Capture the elapsed_fraction the strategy sees
        class CaptureFraction(Strategy):
            name = "Capture"
            captured = None
            def on_session_start(self, m): pass
            def on_tick(self, ctx):
                CaptureFraction.captured = ctx.elapsed_fraction
                return HOLD

        trader.strategy = CaptureFraction()
        sample = {"elapsed_seconds": 150.0, "up_token_midpoint": 0.50, "down_token_midpoint": 0.50}
        trader.process_tick(sample)
        assert CaptureFraction.captured == pytest.approx(0.5)

    def test_capped_at_one(self, metadata):
        strategy = StubStrategy([HOLD])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        class CaptureFraction(Strategy):
            name = "Capture"
            captured = None
            def on_session_start(self, m): pass
            def on_tick(self, ctx):
                CaptureFraction.captured = ctx.elapsed_fraction
                return HOLD

        trader.strategy = CaptureFraction()
        sample = {"elapsed_seconds": 600.0, "up_token_midpoint": 0.50, "down_token_midpoint": 0.50}
        trader.process_tick(sample)
        assert CaptureFraction.captured == 1.0


# ---------------------------------------------------------------------------
# Defensive position copy
# ---------------------------------------------------------------------------


def test_strategy_sees_copy_of_position(sample, metadata):
    """Strategy receives a copy of Position, not the real one."""
    real_pos = Position(cash=100.0, up_tokens=5.0)

    class MutateStrategy(Strategy):
        name = "Mutate"
        def on_session_start(self, m): pass
        def on_tick(self, ctx):
            # Try to cheat by mutating the context position
            ctx.position.cash = 999.0
            return HOLD

    executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
    trader = Trader(MutateStrategy(), executor)
    trader.start_session(metadata, real_pos, 97000.0, 300.0)
    trader.process_tick(sample)

    # Real position should be untouched
    assert real_pos.cash == 100.0


# ---------------------------------------------------------------------------
# Trade tracking
# ---------------------------------------------------------------------------


def test_successful_sim_trade_tracked(sample, metadata):
    strategy = StubStrategy([TradeAction(side="buy_up", amount=100.0)])
    executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
    trader = Trader(strategy, executor)
    pos = Position(cash=100.0)
    trader.start_session(metadata, pos, 97000.0, 300.0)

    trade = trader.process_tick(sample)
    assert trade is not None
    assert len(trader._trades) == 1


def test_hold_not_tracked(sample, metadata):
    strategy = StubStrategy([HOLD])
    executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
    trader = Trader(strategy, executor)
    pos = Position(cash=100.0)
    trader.start_session(metadata, pos, 97000.0, 300.0)

    trade = trader.process_tick(sample)
    assert trade is None
    assert len(trader._trades) == 0
