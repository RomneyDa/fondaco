"""Tests for GTD (Good-Til-Date) order support in SimExecutor and Trader."""

import pytest
from core.trader import (
    HOLD,
    OpenOrder,
    Position,
    SimExecutor,
    TradeAction,
    Trader,
)
from core.strategies.base import Strategy


# ---------------------------------------------------------------------------
# TradeAction expiration_seconds
# ---------------------------------------------------------------------------


class TestTradeActionExpiration:
    def test_default_is_none(self):
        action = TradeAction(side="buy_up", amount=100.0, price=0.20)
        assert action.expiration_seconds is None

    def test_can_set_expiration(self):
        action = TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=60.0)
        assert action.expiration_seconds == 60.0


# ---------------------------------------------------------------------------
# OpenOrder expiration
# ---------------------------------------------------------------------------


class TestOpenOrderExpiration:
    def test_default_is_none(self):
        order = OpenOrder(
            order_id="test-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0,
        )
        assert order.expiration is None

    def test_can_set_expiration(self):
        order = OpenOrder(
            order_id="test-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0, expiration=120.0,
        )
        assert order.expiration == 120.0


# ---------------------------------------------------------------------------
# SimExecutor.place_limit_order — GTD expiration
# ---------------------------------------------------------------------------


@pytest.fixture
def sample():
    return {
        "up_token_midpoint": 0.50,
        "down_token_midpoint": 0.50,
        "up_token_best_bid": 0.49,
        "up_token_best_ask": 0.51,
        "down_token_best_bid": 0.49,
        "down_token_best_ask": 0.51,
        "elapsed_seconds": 30.0,
    }


@pytest.fixture
def metadata():
    return {"up_token_id": "up-123", "down_token_id": "down-456"}


class TestSimPlaceLimitGTD:
    def test_gtc_order_has_no_expiration(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=100.0, price=0.20)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None
        assert order.expiration is None

    def test_gtd_order_has_expiration(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=90.0)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None
        # Expiration = current elapsed + TTL = 30.0 + 90.0 = 120.0
        assert order.expiration == pytest.approx(120.0)

    def test_gtd_expiration_uses_sample_elapsed(self, metadata):
        """Expiration deadline is relative to current elapsed_seconds."""
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=100.0)
        sample = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 60.0,
        }
        action = TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=30.0)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order.expiration == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# SimExecutor.check_fills — GTD expiration
# ---------------------------------------------------------------------------


class TestSimCheckFillsGTD:
    def test_expired_order_is_removed(self, metadata):
        """Order past its expiration deadline should be removed and funds unreserved."""
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=80.0)  # $20 reserved

        order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0, expiration=100.0,
        )
        open_orders = [order]

        # Sample at elapsed_seconds=100.0 (at or past expiration)
        sample = {
            "up_token_midpoint": 0.15,  # would fill if not expired
            "down_token_midpoint": 0.85,
            "elapsed_seconds": 100.0,
        }

        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 0
        assert len(open_orders) == 0
        # Cash should be restored
        assert pos.cash == pytest.approx(100.0)

    def test_not_expired_order_can_fill(self, metadata):
        """Order before its expiration deadline should fill normally."""
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=80.0)  # $20 reserved

        order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0, expiration=120.0,
        )
        open_orders = [order]

        # Sample at elapsed_seconds=100.0 (before 120.0 expiration), midpoint at 0.20 (fills)
        sample = {
            "up_token_midpoint": 0.20,
            "down_token_midpoint": 0.80,
            "elapsed_seconds": 100.0,
        }

        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 1
        assert fills[0].order_type == "GTD"
        assert fills[0].side == "buy_up"
        assert pos.up_tokens == pytest.approx(100.0)

    def test_not_expired_no_fill(self):
        """Order before expiration with midpoint above price stays pending."""
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=80.0)

        order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0, expiration=120.0,
        )
        open_orders = [order]

        sample = {
            "up_token_midpoint": 0.50,  # above our price, no fill
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 100.0,
        }

        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 0
        assert len(open_orders) == 1  # still pending

    def test_gtc_order_fill_type(self):
        """GTC orders (no expiration) report order_type='GTC'."""
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=80.0)

        order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=100.0, dollars_reserved=20.0,
            tokens_reserved=0.0, tick_placed=0,
        )  # no expiration = GTC
        open_orders = [order]

        sample = {
            "up_token_midpoint": 0.20,
            "down_token_midpoint": 0.80,
            "elapsed_seconds": 100.0,
        }

        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 1
        assert fills[0].order_type == "GTC"


# ---------------------------------------------------------------------------
# Guardrails preserve expiration_seconds
# ---------------------------------------------------------------------------


class TestGuardrailsPreserveExpiration:
    def test_capped_buy_keeps_expiration(self, sample, metadata):
        """When _apply_guardrails caps buy amount, expiration_seconds is preserved."""

        class GTDStrategy(Strategy):
            name = "GTDStub"
            def on_session_start(self, m): pass
            def on_tick(self, ctx):
                return TradeAction(
                    side="buy_up", amount=999.0, price=0.20,
                    expiration_seconds=60.0,
                )

        executor = SimExecutor(execution_mode="book")
        trader = Trader(GTDStrategy(), executor)
        pos = Position(cash=50.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # After guardrails, amount should be capped to 50.0 but expiration preserved
        trader.process_tick(sample)

        # Verify an order was placed with expiration
        assert len(trader._open_orders) == 1
        order = trader._open_orders[0]
        assert order.expiration is not None

    def test_uncapped_buy_keeps_expiration(self, sample, metadata):
        """When buy amount is within cash, expiration_seconds is preserved."""

        class GTDStrategy(Strategy):
            name = "GTDStub"
            def on_session_start(self, m): pass
            def on_tick(self, ctx):
                return TradeAction(
                    side="buy_up", amount=50.0, price=0.20,
                    expiration_seconds=60.0,
                )

        executor = SimExecutor(execution_mode="book")
        trader = Trader(GTDStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        trader.process_tick(sample)
        assert len(trader._open_orders) == 1
        order = trader._open_orders[0]
        assert order.expiration is not None


# ---------------------------------------------------------------------------
# End-to-end: GTD order lifecycle through Trader
# ---------------------------------------------------------------------------


class TestGTDLifecycle:
    def test_order_expires_after_deadline(self, metadata):
        """GTD order placed early should expire when elapsed passes deadline."""
        actions = [
            TradeAction(side="buy_up", amount=100.0, price=0.10, expiration_seconds=30.0),
        ] + [HOLD] * 10

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

        executor = SimExecutor(execution_mode="book")
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Tick 0: place order at elapsed=10s, expires at 40s
        sample0 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample0)
        assert len(trader._open_orders) == 1
        assert trader._open_orders[0].expiration == pytest.approx(40.0)

        # Tick 1: elapsed=35s, midpoint still above price, order stays
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 35.0,
        }
        trader.process_tick(sample1)
        assert len(trader._open_orders) == 1  # still pending

        # Tick 2: elapsed=45s, past 40s deadline — order expires
        sample2 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 45.0,
        }
        trader.process_tick(sample2)
        assert len(trader._open_orders) == 0  # expired
        # Cash should be fully restored
        assert pos.cash == pytest.approx(100.0)

    def test_order_fills_before_deadline(self, metadata):
        """GTD order fills normally when midpoint dips before deadline."""
        actions = [
            TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=60.0),
        ] + [HOLD] * 10

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

        executor = SimExecutor(execution_mode="book")
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Tick 0: place order at elapsed=10s, expires at 70s
        sample0 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample0)
        assert len(trader._open_orders) == 1

        # Tick 1: elapsed=30s, midpoint dips to 0.20 — fills
        sample1 = {
            "up_token_midpoint": 0.20, "down_token_midpoint": 0.80,
            "elapsed_seconds": 30.0,
        }
        trader.process_tick(sample1)
        assert len(trader._open_orders) == 0  # filled
        assert len(trader._last_tick_fills) == 1
        assert trader._last_tick_fills[0].order_type == "GTD"
        assert pos.up_tokens > 0


# ---------------------------------------------------------------------------
# Cancel actions (cancel_up / cancel_down)
# ---------------------------------------------------------------------------


class TestCancelAction:
    def test_cancel_up_removes_order_and_unreserves(self, metadata):
        """cancel_up action removes pending buy_up order and restores cash."""
        actions = [
            TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=120.0),
            TradeAction(side="cancel_up"),
        ]

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

        executor = SimExecutor(execution_mode="book")
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        # Tick 0: place order
        sample0 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample0)
        assert len(trader._open_orders) == 1
        assert pos.cash < 100.0  # cash reserved

        # Tick 1: cancel
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 20.0,
        }
        trader.process_tick(sample1)
        assert len(trader._open_orders) == 0
        assert pos.cash == pytest.approx(100.0)

    def test_cancel_down_removes_order_and_unreserves(self, metadata):
        """cancel_down action removes pending buy_down order and restores cash."""
        actions = [
            TradeAction(side="buy_down", amount=50.0, price=0.25, expiration_seconds=120.0),
            TradeAction(side="cancel_down"),
        ]

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

        executor = SimExecutor(execution_mode="book")
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=50.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        sample0 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample0)
        assert len(trader._open_orders) == 1

        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 20.0,
        }
        trader.process_tick(sample1)
        assert len(trader._open_orders) == 0
        assert pos.cash == pytest.approx(50.0)

    def test_cancel_wrong_token_is_noop(self, metadata):
        """cancel_down when only buy_up order pending does nothing."""
        actions = [
            TradeAction(side="buy_up", amount=100.0, price=0.20, expiration_seconds=120.0),
            TradeAction(side="cancel_down"),  # wrong token
        ]

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

        executor = SimExecutor(execution_mode="book")
        trader = Trader(SeqStrategy(), executor)
        pos = Position(cash=100.0)
        trader.start_session(metadata, pos, 97000.0, 300.0)

        sample = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        trader.process_tick(sample)
        assert len(trader._open_orders) == 1

        trader.process_tick(sample)
        assert len(trader._open_orders) == 1  # still there — wrong token
