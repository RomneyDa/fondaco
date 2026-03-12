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


# ---------------------------------------------------------------------------
# Guardrails (_apply_guardrails)
# ---------------------------------------------------------------------------


class TestGuardrails:
    def test_hold_passes_through(self, metadata):
        strategy = StubStrategy([HOLD])
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        result = trader._apply_guardrails(HOLD)
        assert result is HOLD

    def test_cancel_passes_through(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        action = TradeAction(side="cancel_up")
        result = trader._apply_guardrails(action)
        assert result.side == "cancel_up"

    def test_buy_capped_to_cash(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=30.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        action = TradeAction(side="buy_up", amount=100.0)
        result = trader._apply_guardrails(action)
        assert result.amount == 30.0
        assert result.side == "buy_up"

    def test_buy_blocked_at_zero_cash(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=0.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        action = TradeAction(side="buy_up", amount=50.0)
        result = trader._apply_guardrails(action)
        assert result is HOLD

    def test_trade_blocked_near_session_end(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        # elapsed 296s with 300s window → within TRADE_CUTOFF_SECONDS (5s)
        sample = {"elapsed_seconds": 296.0}
        action = TradeAction(side="buy_up", amount=50.0)
        result = trader._apply_guardrails(action, sample=sample)
        assert result is HOLD

    def test_sell_not_capped(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=0.0, up_tokens=50.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        action = TradeAction(side="sell_up", amount=50.0)
        result = trader._apply_guardrails(action)
        assert result.side == "sell_up"
        assert result.amount == 50.0

    def test_buy_preserves_expiration(self, metadata):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        pos = Position(cash=30.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        action = TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=60.0)
        result = trader._apply_guardrails(action)
        assert result.amount == 30.0
        assert result.expiration_seconds == 60.0


# ---------------------------------------------------------------------------
# Auto-exit (_check_auto_exit)
# ---------------------------------------------------------------------------


class TestAutoExit:
    def _make_trader(self, metadata, pos):
        strategy = StubStrategy()
        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(strategy, executor)
        trader.start_session(metadata, pos, 97000.0, 300.0)
        return trader

    def test_sells_up_tokens_at_threshold(self, metadata):
        pos = Position(cash=0.0, up_tokens=10.0)
        trader = self._make_trader(metadata, pos)
        sample = {"up_token_midpoint": 0.99, "down_token_midpoint": 0.01}
        action = trader._check_auto_exit(sample)
        assert action.side == "sell_up"
        assert action.amount == 10.0

    def test_sells_down_tokens_at_threshold(self, metadata):
        pos = Position(cash=0.0, down_tokens=10.0)
        trader = self._make_trader(metadata, pos)
        sample = {"up_token_midpoint": 0.01, "down_token_midpoint": 0.99}
        action = trader._check_auto_exit(sample)
        assert action.side == "sell_down"
        assert action.amount == 10.0

    def test_holds_below_threshold(self, metadata):
        pos = Position(cash=0.0, up_tokens=10.0)
        trader = self._make_trader(metadata, pos)
        sample = {"up_token_midpoint": 0.90, "down_token_midpoint": 0.10}
        action = trader._check_auto_exit(sample)
        assert action is HOLD

    def test_holds_below_min_order(self, metadata):
        pos = Position(cash=0.0, up_tokens=3.0)  # < CLOB_MIN_ORDER_TOKENS (5)
        trader = self._make_trader(metadata, pos)
        sample = {"up_token_midpoint": 0.99, "down_token_midpoint": 0.01}
        action = trader._check_auto_exit(sample)
        assert action is HOLD

    def test_holds_when_midpoint_none(self, metadata):
        pos = Position(cash=0.0, up_tokens=10.0)
        trader = self._make_trader(metadata, pos)
        sample = {"up_token_midpoint": None, "down_token_midpoint": None}
        action = trader._check_auto_exit(sample)
        assert action is HOLD


# ---------------------------------------------------------------------------
# End session with pending orders
# ---------------------------------------------------------------------------


class TestEndSessionPendingOrders:
    def test_pending_orders_cancelled_on_end(self, metadata):
        """Open orders should be cancelled and funds unreserved at session end."""
        strategy = StubStrategy([
            TradeAction(side="buy_up", amount=50.0, price=0.10, expiration_seconds=120.0),
        ])
        executor = SimExecutor(execution_mode="book")
        trader = Trader(strategy, executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Tick 0: place limit order
        sample = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample)
        assert len(trader._open_orders) == 1
        assert pos.cash < 100.0

        # End session — order should be cancelled, cash restored
        trader.end_session("Up")
        assert len(trader._open_orders) == 0
        assert pos.cash == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Limit order lifecycle through process_tick
# ---------------------------------------------------------------------------


class TestLimitOrderLifecycle:
    def test_order_placed_fills_on_later_tick(self, metadata):
        """Limit order placed on tick 0 fills when midpoint reaches price."""
        actions = [
            TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=120.0),
        ] + [HOLD] * 5

        class SeqStrategy(Strategy):
            name = "Seq"
            def __init__(self):
                self._i = 0
            def on_session_start(self, m):
                self._i = 0
            def on_tick(self, ctx):
                if self._i < len(actions):
                    a = actions[self._i]
                    self._i += 1
                    return a
                return HOLD

        executor = SimExecutor(execution_mode="book", slippage=0.0)
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Tick 0: place order (midpoint too high to fill)
        s0 = {"up_token_midpoint": 0.50, "down_token_midpoint": 0.50, "elapsed_seconds": 10.0}
        trader.process_tick(s0)
        assert len(trader._open_orders) == 1

        # Tick 1: midpoint drops to 0.20 (equals order price, fills with margin=0)
        s1 = {"up_token_midpoint": 0.20, "down_token_midpoint": 0.80, "elapsed_seconds": 20.0}
        trader.process_tick(s1)
        assert len(trader._open_orders) == 0
        assert pos.up_tokens > 0
        assert len(trader._last_tick_fills) == 1


# ---------------------------------------------------------------------------
# History tracking
# ---------------------------------------------------------------------------


class TestHistoryTracking:
    def test_history_disabled_in_sim_mode(self, sample, metadata):
        """When sim_mode=True and uses_history=False, history stays empty."""

        class NoHistoryStrategy(Strategy):
            name = "NoHistory"
            uses_history = False
            def on_session_start(self, m): pass
            def on_tick(self, ctx): return HOLD

        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(NoHistoryStrategy(), executor, sim_mode=True)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        trader.process_tick(sample)
        trader.process_tick(sample)
        assert trader._history == []

    def test_history_enabled(self, sample, metadata):
        """When uses_history=True, samples accumulate."""

        class HistoryStrategy(Strategy):
            name = "WithHistory"
            uses_history = True
            def on_session_start(self, m): pass
            def on_tick(self, ctx): return HOLD

        executor = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trader = Trader(HistoryStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        trader.process_tick(sample)
        trader.process_tick(sample)
        assert len(trader._history) == 2
