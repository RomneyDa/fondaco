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
        assert ex._get_execution_price("sell_up", sample) == 0.50  # midpoint (book sells at mid)

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

    def test_fees_enabled(self, sample, metadata):
        """Fees should reduce cash below simple cost calculation."""
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0, fees=True)
        pos = Position(cash=100.0)
        trade = ex.execute_trade(
            TradeAction(side="buy_up", amount=100.0), 0, metadata, sample, pos,
        )
        assert trade is not None
        assert trade.fee > 0
        # Cash should be less than 100 - (tokens * price) due to fee
        assert pos.cash < 0.0  # spent all 100 on tokens, then fee deducted

    def test_fees_disabled(self, sample, metadata):
        """With fees=False, no fee should be charged."""
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0, fees=False)
        pos = Position(cash=100.0)
        trade = ex.execute_trade(
            TradeAction(side="buy_up", amount=100.0), 0, metadata, sample, pos,
        )
        assert trade is not None
        assert trade.fee == 0.0
        assert pos.cash == pytest.approx(0.0)

    def test_sell_execution(self, sample, metadata):
        """Selling tokens should increase cash and decrease token balance."""
        ex = SimExecutor(execution_mode="midpoint", slippage=0.0, fees=False)
        pos = Position(cash=0.0, up_tokens=100.0)
        trade = ex.execute_trade(
            TradeAction(side="sell_up", amount=100.0), 0, metadata, sample, pos,
        )
        assert trade is not None
        assert trade.side == "sell_up"
        assert pos.up_tokens == 0.0
        assert pos.cash == pytest.approx(50.0)  # 100 tokens * 0.50


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------


class TestPlaceLimitOrder:
    def test_buy_reserves_cash(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=100.0, price=0.20)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None
        assert order.dollars_reserved == pytest.approx(100.0)
        assert pos.cash == pytest.approx(0.0)

    def test_sell_reserves_tokens(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=0.0, up_tokens=50.0)
        action = TradeAction(side="sell_up", amount=50.0, price=0.60)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None
        assert order.tokens_reserved == pytest.approx(50.0)
        assert pos.up_tokens == pytest.approx(0.0)

    def test_below_min_tokens_rejected(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=1.0)
        # 1.0 / 0.50 = 2 tokens, below CLOB_MIN_ORDER_TOKENS (5)
        action = TradeAction(side="buy_up", amount=1.0, price=0.50)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is None
        assert pos.cash == pytest.approx(1.0)  # unchanged


# ---------------------------------------------------------------------------
# check_fills — additional cases
# ---------------------------------------------------------------------------


class TestCheckFillsExtra:
    def test_sell_side_limit_fill(self):
        ex = SimExecutor(execution_mode="book", slippage=0.0)
        pos = Position(cash=0.0)  # tokens already reserved
        from core.trader import OpenOrder
        order = OpenOrder(
            order_id="sim-1", side="sell_up", token="up",
            price=0.70, tokens=20.0, dollars_reserved=0.0,
            tokens_reserved=20.0, tick_placed=0,
        )
        open_orders = [order]
        sample = {
            "up_token_midpoint": 0.70,  # at price, fills with margin=0
            "down_token_midpoint": 0.30,
            "elapsed_seconds": 60.0,
        }
        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 1
        assert fills[0].side == "sell_up"
        assert pos.cash > 0  # got proceeds

    def test_multiple_orders_partial_fill(self):
        ex = SimExecutor(execution_mode="book", slippage=0.0)
        pos = Position(cash=0.0)
        from core.trader import OpenOrder
        order1 = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=50.0, dollars_reserved=10.0,
            tokens_reserved=0.0, tick_placed=0,
        )
        order2 = OpenOrder(
            order_id="sim-2", side="buy_up", token="up",
            price=0.10, tokens=100.0, dollars_reserved=10.0,
            tokens_reserved=0.0, tick_placed=0,
        )
        open_orders = [order1, order2]
        # Midpoint at 0.15: fills order1 (price 0.20) but not order2 (price 0.10)
        sample = {
            "up_token_midpoint": 0.15,
            "down_token_midpoint": 0.85,
            "elapsed_seconds": 60.0,
        }
        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 1
        assert fills[0].order_id == "sim-1"
        assert len(open_orders) == 1  # order2 still pending
        assert open_orders[0].order_id == "sim-2"

    def test_limit_fill_margin_effect(self):
        """With margin=0.05, order at 0.20 needs midpoint <= 0.15 to fill."""
        ex = SimExecutor(execution_mode="book", slippage=0.0, limit_fill_margin=0.05)
        pos = Position(cash=0.0)
        from core.trader import OpenOrder
        order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=50.0, dollars_reserved=10.0,
            tokens_reserved=0.0, tick_placed=0,
        )
        # Midpoint at 0.18: above threshold (0.20 - 0.05 = 0.15), no fill
        open_orders = [order]
        sample = {"up_token_midpoint": 0.18, "down_token_midpoint": 0.82, "elapsed_seconds": 60.0}
        fills = ex.check_fills(open_orders, sample, pos)
        assert len(fills) == 0

        # Midpoint at 0.14: below threshold, fills
        sample2 = {"up_token_midpoint": 0.14, "down_token_midpoint": 0.86, "elapsed_seconds": 70.0}
        fills = ex.check_fills(open_orders, sample2, pos)
        assert len(fills) == 1


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_cancel_unreserves_funds(self, sample, metadata):
        ex = SimExecutor(execution_mode="book")
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=50.0, price=0.20)
        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert pos.cash < 100.0
        result = ex.cancel_order(order, pos)
        assert result is True
        assert pos.cash == pytest.approx(100.0)
