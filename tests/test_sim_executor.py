"""Tests for SimExecutor: execution price modes and trade execution."""

import pytest
from core.trader import Position, SimExecutor, TradeAction, HOLD


@pytest.fixture
def sample():
    return {
        "up_token_midpoint": 0.50,
        "down_token_midpoint": 0.50,
        "up_token_best_bid": 0.49,
        "up_token_best_ask": 0.51,
        "down_token_best_bid": 0.49,
        "down_token_best_ask": 0.51,
        "elapsed_seconds": 60.0,
    }


@pytest.fixture
def metadata():
    return {"up_token_id": "up-123", "down_token_id": "down-456"}


# ---------------------------------------------------------------------------
# _get_execution_price
# ---------------------------------------------------------------------------


class TestGetExecutionPrice:
    def test_midpoint_slippage_buy(self, sample):
        ex = SimExecutor(execution_mode="midpoint_slippage", slippage=0.02)
        price = ex._get_execution_price("buy_up", sample)
        assert price == pytest.approx(0.52)

    def test_midpoint_slippage_sell(self, sample):
        ex = SimExecutor(execution_mode="midpoint_slippage", slippage=0.02)
        price = ex._get_execution_price("sell_up", sample)
        assert price == pytest.approx(0.48)

    def test_midpoint_mode(self, sample):
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0)
        assert ex._get_execution_price("buy_up", sample) == 0.50
        assert ex._get_execution_price("sell_down", sample) == 0.50

    def test_book_mode(self, sample):
        ex = SimExecutor(execution_mode="book")
        assert ex._get_execution_price("buy_up", sample) == 0.51  # best ask
        assert ex._get_execution_price("sell_up", sample) == 0.49  # best bid

    def test_missing_midpoint(self):
        ex = SimExecutor(execution_mode="midpoint_slippage")
        sample = {"up_token_midpoint": None, "down_token_midpoint": 0.50}
        assert ex._get_execution_price("buy_up", sample) is None

    def test_unknown_mode(self, sample):
        ex = SimExecutor(execution_mode="unknown")
        assert ex._get_execution_price("buy_up", sample) is None


# ---------------------------------------------------------------------------
# execute_trade
# ---------------------------------------------------------------------------


class TestExecuteTrade:
    def test_hold_returns_none(self, sample, metadata):
        ex = SimExecutor()
        result = ex.execute_trade(HOLD, 0, metadata, sample, Position(cash=100.0))
        assert result is None

    def test_zero_amount_returns_none(self, sample, metadata):
        ex = SimExecutor()
        action = TradeAction(side="buy_up", amount=0.0)
        result = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
        assert result is None

    def test_successful_buy(self, sample, metadata):
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0)
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=50.0)
        trade = ex.execute_trade(action, 0, metadata, sample, pos)
        assert trade is not None
        assert trade.side == "buy_up"
        assert trade.tokens > 0
        assert pos.up_tokens > 0
        assert pos.cash < 100.0

    def test_none_price_returns_none(self, metadata):
        ex = SimExecutor(execution_mode="midpoint")
        sample = {"up_token_midpoint": None, "down_token_midpoint": 0.50, "elapsed_seconds": 60.0}
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=50.0)
        result = ex.execute_trade(action, 0, metadata, sample, pos)
        assert result is None
        assert pos.cash == 100.0  # unchanged

    def test_below_min_order_size(self, metadata):
        """Orders with < 5 tokens are rejected (mirrors CLOB)."""
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0)
        sample = {"up_token_midpoint": 0.50, "down_token_midpoint": 0.50, "elapsed_seconds": 60.0}
        pos = Position(cash=2.0)  # 2.0 / 0.50 = 4 tokens (below 5)
        action = TradeAction(side="buy_up", amount=2.0)
        result = ex.execute_trade(action, 0, metadata, sample, pos)
        assert result is None
        assert pos.cash == 2.0

    def test_mutates_position(self, sample, metadata):
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0)
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=100.0)
        trade = ex.execute_trade(action, 0, metadata, sample, pos)
        assert trade is not None
        assert pos.cash < 100.0
        assert pos.up_tokens == trade.tokens

    def test_trade_has_no_live_fields(self, sample, metadata):
        """SimExecutor trades have None for live-only fields."""
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0)
        trade = ex.execute_trade(
            TradeAction(side="buy_up", amount=100.0), 0, metadata, sample, Position(cash=100.0),
        )
        assert trade.order_id is None
        assert trade.success is None  # sim, not live
        assert trade.token_id is None
