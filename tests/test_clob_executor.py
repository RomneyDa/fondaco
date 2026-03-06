"""Tests for ClobExecutor with a mocked CLOB client.

The critical safety property: if post_order raises, position must be unchanged.
"""

import json
import math
from unittest.mock import MagicMock, patch

import pytest
from core.trader import (
    ClobExecutor, OpenOrder, Position, SimExecutor, Trade, TradeAction,
    Trader, HOLD,
)


@pytest.fixture
def mock_client():
    """A mock ClobClient that succeeds by default with realistic FAK response.

    Default response simulates a full fill of 200 tokens at 0.50 price:
    BUY: takingAmount = tokens * 1e6, makingAmount = dollars * 1e6
    """
    client = MagicMock()
    client.create_order.return_value = MagicMock()
    client.post_order.return_value = {
        "orderID": "order-abc-123",
        "success": True,
        "status": "matched",
        "takingAmount": "200000000",   # 200 tokens (full fill of 100/0.50)
        "makingAmount": "100000000",   # $100
    }
    return client


@pytest.fixture
def metadata():
    return {
        "up_token_id": "up-token-123",
        "down_token_id": "down-token-456",
    }


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


# ---------------------------------------------------------------------------
# Basic control flow
# ---------------------------------------------------------------------------


def test_hold_returns_none(mock_client, metadata, sample):
    ex = ClobExecutor(mock_client)
    result = ex.execute_trade(HOLD, 0, metadata, sample, Position(cash=100.0))
    assert result is None


def test_zero_amount_returns_none(mock_client, metadata, sample):
    ex = ClobExecutor(mock_client)
    action = TradeAction(side="buy_up", amount=0.0)
    result = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert result is None


def test_unknown_side(mock_client, metadata, sample):
    ex = ClobExecutor(mock_client)
    action = TradeAction(side="fly_up", amount=50.0)
    trade = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert trade is not None
    assert trade.success is False
    assert "Unknown side" in trade.error


# ---------------------------------------------------------------------------
# Price validation
# ---------------------------------------------------------------------------


def test_none_price(mock_client, metadata):
    ex = ClobExecutor(mock_client)
    sample = {"up_token_best_ask": None, "up_token_best_bid": 0.49}
    action = TradeAction(side="buy_up", amount=50.0)
    trade = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert trade.success is False
    assert "No valid price" in trade.error


def test_price_rounds_to_one(mock_client, metadata):
    """If best_ask rounds to 1.0, trade is rejected (out of range)."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_best_ask": 0.999, "up_token_best_bid": 0.98}
    action = TradeAction(side="buy_up", amount=50.0)
    trade = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert trade.success is False
    assert "out of range" in trade.error


def test_wide_spread_trades_at_market(mock_client, metadata):
    """Wide spread (0.01/0.99) is normal for these markets — trade goes through."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_best_ask": 0.99, "up_token_best_bid": 0.01,
              "down_token_best_ask": 0.99, "down_token_best_bid": 0.01}
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    assert trade.success is True


def test_valid_low_price(mock_client, metadata):
    """Best ask 0.02 is valid."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_best_ask": 0.02, "up_token_best_bid": 0.01,
              "down_token_best_ask": 0.99, "down_token_best_bid": 0.98}
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    # At price 0.02, 100/0.02 = 5000 tokens, floor -> 5000, which is >= 5
    assert trade.success is True


# ---------------------------------------------------------------------------
# CRITICAL: Order failure must not mutate position
# ---------------------------------------------------------------------------


def test_order_failure_preserves_position(mock_client, metadata, sample):
    """If post_order raises, position must remain unchanged."""
    mock_client.post_order.side_effect = RuntimeError("CLOB API down")
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    original_cash = pos.cash

    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is False
    assert "CLOB API down" in trade.error
    assert pos.cash == original_cash
    assert pos.up_tokens == 0.0


def test_create_order_failure_preserves_position(mock_client, metadata, sample):
    """If create_order raises, position must remain unchanged."""
    mock_client.create_order.side_effect = RuntimeError("Signing failed")
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)

    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is False
    assert pos.cash == 100.0
    assert pos.up_tokens == 0.0


# ---------------------------------------------------------------------------
# Successful trade
# ---------------------------------------------------------------------------


def test_successful_buy_mutates_position(mock_client, metadata, sample):
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)

    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is True
    assert trade.order_id == "order-abc-123"
    assert pos.cash < 100.0
    assert pos.up_tokens > 0


def test_buy_floors_tokens_to_integer(mock_client, metadata):
    """CLOB buys floor token count to integer for clean maker amounts."""
    ex = ClobExecutor(mock_client)
    # best_ask 0.34 -> 100/0.34 = 294.117...
    sample = {"up_token_best_ask": 0.34, "up_token_best_bid": 0.32,
              "down_token_best_ask": 0.68, "down_token_best_bid": 0.66}
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    assert trade.success is True
    assert trade.tokens == math.floor(trade.tokens)  # integer


def test_below_min_order_size(mock_client, metadata):
    """Orders with < 5 tokens (after floor) don't trade and position is unchanged."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_best_ask": 0.51, "up_token_best_bid": 0.49,
              "down_token_best_ask": 0.51, "down_token_best_bid": 0.49}
    pos = Position(cash=2.0)  # 2.0 / 0.51 = 3.92 tokens, floor -> 3
    action = TradeAction(side="buy_up", amount=2.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    # compute_trade_size returns (0,0) since 3.92 < MIN_ORDER, so no trade
    assert trade is not None
    assert trade.success is False
    assert pos.cash == 2.0  # position unchanged


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------


def test_log_trade_writes_jsonl(mock_client, metadata, sample, tmp_path):
    log_file = str(tmp_path / "trades.jsonl")
    ex = ClobExecutor(mock_client, trade_log_file=log_file)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    ex.execute_trade(action, 0, metadata, sample, pos)

    with open(log_file) as f:
        lines = f.readlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["side"] == "buy_up"
    assert entry["success"] is True


def test_log_trade_none_file_is_noop(mock_client, metadata, sample):
    """No trade_log_file means _log_trade is a no-op."""
    ex = ClobExecutor(mock_client, trade_log_file=None)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    # Should not raise
    ex.execute_trade(action, 0, metadata, sample, pos)


# ---------------------------------------------------------------------------
# FAK partial fill handling
# ---------------------------------------------------------------------------


def test_partial_fill_applies_only_filled_amount(mock_client, metadata, sample):
    """Partial fill: position gets actual tokens, not requested."""
    # Price is 0.51 (best_ask from fixture). 100/0.51 = 196 tokens (floored).
    # Simulate partial fill: requested 196 tokens, got 120
    mock_client.post_order.return_value = {
        "orderID": "order-partial-1",
        "success": True,
        "status": "matched",
        "takingAmount": "120000000",   # 120 tokens (partial)
        "makingAmount": "60000000",    # $60
    }
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)

    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is True
    assert trade.tokens == 120.0
    assert trade.amount == 60.0
    assert trade.requested_tokens == 196  # logged for audit
    assert pos.up_tokens == 120.0
    # Cash deducted by actual_tokens * price (120 * 0.51 = 61.2)
    assert pos.cash == pytest.approx(38.8)


def test_success_false_preserves_position(mock_client, metadata, sample):
    """CLOB returns success:false — position must remain unchanged."""
    mock_client.post_order.return_value = {
        "success": False,
        "errorMsg": "Insufficient balance on exchange",
    }
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)

    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is False
    assert "CLOB rejected order" in trade.error
    assert pos.cash == 100.0
    assert pos.up_tokens == 0.0


def test_zero_fill_preserves_position(mock_client, metadata, sample):
    """Zero fill (no liquidity) — position must remain unchanged."""
    mock_client.post_order.return_value = {
        "orderID": "order-zero-fill",
        "success": True,
        "status": "matched",
        "takingAmount": "0",
        "makingAmount": "0",
    }
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)

    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is False
    assert "negligible fill" in trade.error
    assert pos.cash == 100.0
    assert pos.up_tokens == 0.0


def test_missing_fill_amounts_falls_back_to_requested(mock_client, metadata, sample):
    """If takingAmount/makingAmount missing, fall back to requested amounts."""
    mock_client.post_order.return_value = {
        "orderID": "order-no-fill-info",
    }
    ex = ClobExecutor(mock_client)
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)

    trade = ex.execute_trade(action, 0, metadata, sample, pos)

    assert trade.success is True
    # Falls back to requested: floor(100/0.51) = 196 tokens
    assert trade.tokens == 196
    assert trade.requested_tokens is None  # no partial fill, so not set
    assert pos.up_tokens == 196


# ===========================================================================
# SimExecutor limit order lifecycle tests
# ===========================================================================


class TestSimExecutorLimitOrders:
    """Test the SimExecutor limit order lifecycle: place → fill → cancel."""

    def _make_sample(self, up_mid=0.50, down_mid=0.50, elapsed=60.0):
        return {
            "up_token_midpoint": up_mid,
            "down_token_midpoint": down_mid,
            "up_token_best_bid": 0.01,
            "up_token_best_ask": 0.99,
            "down_token_best_bid": 0.01,
            "down_token_best_ask": 0.99,
            "elapsed_seconds": elapsed,
        }

    def test_place_limit_order_reserves_cash(self):
        """Placing a buy limit order deducts cash immediately."""
        ex = SimExecutor()
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=50.0, price=0.50)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)

        assert order is not None
        assert order.order_id.startswith("sim-")
        assert order.side == "buy_up"
        assert order.token == "up"
        assert order.price == 0.50
        assert order.tokens == 100  # 50 / 0.50 = 100 tokens
        assert order.dollars_reserved == 50.0
        assert pos.cash == 50.0  # cash reserved

    def test_limit_buy_fills_when_midpoint_at_or_below_price(self):
        """Buy limit at 0.55 fills when midpoint drops to 0.55."""
        ex = SimExecutor()
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=55.0, price=0.55)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None

        orders = [order]

        # Midpoint at 0.60 — should NOT fill
        no_fills = ex.check_fills(orders, self._make_sample(up_mid=0.60), pos)
        assert len(no_fills) == 0
        assert len(orders) == 1  # still pending

        # Midpoint drops to 0.55 — should fill
        fills = ex.check_fills(orders, self._make_sample(up_mid=0.55), pos)
        assert len(fills) == 1
        assert fills[0].side == "buy_up"
        assert fills[0].order_type == "GTC"
        assert len(orders) == 0  # removed from list
        assert pos.up_tokens == 100  # 55/0.55 = 100 tokens

    def test_limit_sell_fills_when_midpoint_at_or_above_price(self):
        """Sell limit at 0.60 fills when midpoint rises to 0.60."""
        ex = SimExecutor()
        pos = Position(cash=0.0, up_tokens=100.0)
        action = TradeAction(side="sell_up", amount=100.0, price=0.60)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is not None
        assert pos.up_tokens == 0.0  # tokens reserved
        assert order.tokens_reserved == 100.0

        orders = [order]

        # Midpoint at 0.50 — should NOT fill
        no_fills = ex.check_fills(orders, self._make_sample(up_mid=0.50), pos)
        assert len(no_fills) == 0

        # Midpoint rises to 0.60 — should fill
        fills = ex.check_fills(orders, self._make_sample(up_mid=0.60), pos)
        assert len(fills) == 1
        assert pos.cash == 60.0  # 100 * 0.60
        assert pos.up_tokens == 0.0

    def test_cancel_returns_reserved_cash(self):
        """Cancelling a buy order returns the reserved cash."""
        ex = SimExecutor()
        pos = Position(cash=100.0)
        action = TradeAction(side="buy_up", amount=50.0, price=0.50)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert pos.cash == 50.0

        result = ex.cancel_order(order, pos)
        assert result is True
        assert pos.cash == 100.0  # cash returned

    def test_cancel_returns_reserved_tokens(self):
        """Cancelling a sell order returns the reserved tokens."""
        ex = SimExecutor()
        pos = Position(cash=0.0, up_tokens=100.0)
        action = TradeAction(side="sell_up", amount=100.0, price=0.60)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert pos.up_tokens == 0.0

        result = ex.cancel_order(order, pos)
        assert result is True
        assert pos.up_tokens == 100.0

    def test_place_rejects_below_min_order(self):
        """Orders below CLOB_MIN_ORDER_TOKENS are rejected."""
        ex = SimExecutor()
        pos = Position(cash=2.0)
        action = TradeAction(side="buy_up", amount=2.0, price=0.50)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        order = ex.place_limit_order(action, 0, metadata, sample, pos)
        assert order is None
        assert pos.cash == 2.0  # unchanged

    def test_place_rejects_invalid_price(self):
        """Prices out of 0.01-0.99 range are rejected."""
        ex = SimExecutor()
        pos = Position(cash=100.0)
        sample = self._make_sample()
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}

        for bad_price in [0.0, 1.0, -0.5, 1.5]:
            action = TradeAction(side="buy_up", amount=50.0, price=bad_price)
            order = ex.place_limit_order(action, 0, metadata, sample, pos)
            assert order is None
        assert pos.cash == 100.0


class TestTraderLimitOrders:
    """Test Trader integration with limit orders via SimExecutor."""

    def _make_sample(self, up_mid=0.50, down_mid=0.50, elapsed=60.0):
        return {
            "up_token_midpoint": up_mid,
            "down_token_midpoint": down_mid,
            "up_token_best_bid": 0.01,
            "up_token_best_ask": 0.99,
            "down_token_best_bid": 0.01,
            "down_token_best_ask": 0.99,
            "elapsed_seconds": elapsed,
            "btc_price_usd": 100000.0,
        }

    def _make_strategy(self, actions):
        """Create a mock strategy that returns actions from a list."""
        strategy = MagicMock()
        strategy.name = "TestLimit"
        strategy._tick_logs = []
        strategy.on_tick.side_effect = actions
        return strategy

    def test_limit_order_placed_and_filled(self):
        """Strategy returns price → limit order placed → fills on next tick."""
        actions = [
            TradeAction(side="buy_up", amount=50.0, price=0.50),  # tick 0: place limit
            HOLD,  # tick 1: fill happens in check_fills
        ]
        strategy = self._make_strategy(actions)
        executor = SimExecutor()
        trader = Trader(strategy, executor)
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}
        pos = Position(cash=100.0)

        trader.start_session(metadata, pos, 100000.0, 300.0)

        # Tick 0: place order
        trade = trader.process_tick(self._make_sample(up_mid=0.55))
        assert trade is None  # limit order doesn't return a Trade
        assert len(trader._open_orders) == 1
        assert pos.cash == 50.0  # cash reserved

        # Tick 1: midpoint drops to 0.50 → fills
        trade = trader.process_tick(self._make_sample(up_mid=0.50))
        assert len(trader.last_tick_fills) == 1
        assert trader.last_tick_fills[0].side == "buy_up"
        assert len(trader._open_orders) == 0
        assert pos.up_tokens == 100  # 50/0.50

    def test_end_session_cancels_pending_orders(self):
        """Unfilled orders are cancelled at session end, funds returned."""
        actions = [
            TradeAction(side="buy_up", amount=50.0, price=0.50),
        ]
        strategy = self._make_strategy(actions)
        executor = SimExecutor()
        trader = Trader(strategy, executor)
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}
        pos = Position(cash=100.0)

        trader.start_session(metadata, pos, 100000.0, 300.0)

        # Place order (won't fill because midpoint > price)
        trader.process_tick(self._make_sample(up_mid=0.60))
        assert len(trader._open_orders) == 1
        assert pos.cash == 50.0

        # End session → cancels order, returns cash, resolves
        trades = trader.end_session("Up")
        assert pos.cash == 100.0  # cash returned, no tokens to resolve
        assert len(trader._open_orders) == 0

    def test_replace_order_on_same_token(self):
        """Placing a new limit order on the same token cancels the old one."""
        actions = [
            TradeAction(side="buy_up", amount=50.0, price=0.50),  # tick 0
            TradeAction(side="buy_up", amount=50.0, price=0.45),  # tick 1: replaces
        ]
        strategy = self._make_strategy(actions)
        executor = SimExecutor()
        trader = Trader(strategy, executor)
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}
        pos = Position(cash=500.0)

        trader.start_session(metadata, pos, 100000.0, 300.0)

        # Tick 0: place first order (100 tokens * 0.50 = $50 reserved)
        trader.process_tick(self._make_sample(up_mid=0.60))
        assert len(trader._open_orders) == 1
        assert trader._open_orders[0].price == 0.50
        assert pos.cash == 450.0

        # Tick 1: replace with new price — old order cancelled, new placed
        trader.process_tick(self._make_sample(up_mid=0.60))
        assert len(trader._open_orders) == 1
        assert trader._open_orders[0].price == 0.45
        # Cash: old $50 returned → 500, then reserve floor(50/0.45)=111 * 0.45 = 49.95
        assert pos.cash == pytest.approx(450.05, abs=0.01)

    def test_fak_path_unchanged_when_no_price(self):
        """TradeAction without price uses FAK path (backward compat)."""
        actions = [
            TradeAction(side="buy_up", amount=50.0),  # no price → FAK
        ]
        strategy = self._make_strategy(actions)
        executor = SimExecutor()
        trader = Trader(strategy, executor)
        metadata = {"up_token_id": "up-1", "down_token_id": "down-1"}
        pos = Position(cash=100.0)

        trader.start_session(metadata, pos, 100000.0, 300.0)

        trade = trader.process_tick(self._make_sample())
        assert trade is not None
        assert trade.side == "buy_up"
        # FAK at best_ask=0.99: SimExecutor uses round(50/0.99, 4)=50.5051 tokens
        # 50.5051 * 0.99 ≈ 50.00 spent
        assert pos.cash == pytest.approx(50.0, abs=0.01)
        assert pos.up_tokens == pytest.approx(50.505, abs=0.01)
        assert len(trader._open_orders) == 0
