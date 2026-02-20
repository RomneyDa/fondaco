"""Tests for ClobExecutor with a mocked CLOB client.

The critical safety property: if post_order raises, position must be unchanged.
"""

import json
import math
from unittest.mock import MagicMock, patch

import pytest
from core.trader import ClobExecutor, Position, TradeAction, HOLD


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


def test_none_midpoint(mock_client, metadata):
    ex = ClobExecutor(mock_client)
    sample = {"up_token_midpoint": None, "down_token_midpoint": 0.50}
    action = TradeAction(side="buy_up", amount=50.0)
    trade = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert trade.success is False
    assert "Invalid midpoint" in trade.error


def test_midpoint_rounds_to_one(mock_client, metadata):
    """If midpoint rounds to 1.0, trade is rejected."""
    ex = ClobExecutor(mock_client)
    # round(0.999, 2) = 1.0 in Python — triggers "out of range"
    sample = {"up_token_midpoint": 0.999, "down_token_midpoint": 0.50}
    action = TradeAction(side="buy_up", amount=50.0)
    trade = ex.execute_trade(action, 0, metadata, sample, Position(cash=100.0))
    assert trade.success is False
    assert "out of range" in trade.error


def test_valid_low_midpoint(mock_client, metadata):
    """Midpoint 0.01 is valid."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_midpoint": 0.01, "down_token_midpoint": 0.99}
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    # At price 0.01, 100/0.01 = 10000 tokens, floor -> 10000, which is >= 5
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
    # 50 / 0.50 = 100.0 tokens, but midpoint 0.33 -> 100/0.33 = 303.0303
    sample = {"up_token_midpoint": 0.33, "down_token_midpoint": 0.67}
    pos = Position(cash=100.0)
    action = TradeAction(side="buy_up", amount=100.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    assert trade.success is True
    assert trade.tokens == math.floor(trade.tokens)  # integer


def test_below_min_order_size(mock_client, metadata):
    """Orders with < 5 tokens (after floor) are rejected."""
    ex = ClobExecutor(mock_client)
    sample = {"up_token_midpoint": 0.50, "down_token_midpoint": 0.50}
    pos = Position(cash=2.0)  # 2.0 / 0.50 = 4 tokens, floor -> 4
    action = TradeAction(side="buy_up", amount=2.0)
    trade = ex.execute_trade(action, 0, metadata, sample, pos)
    assert trade.success is False
    assert "minimum order size" in trade.error


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
    # Simulate partial fill: requested 200 tokens, got 120
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
    assert trade.requested_tokens == 200.0  # logged for audit
    assert pos.up_tokens == 120.0
    # Cash deducted by actual_tokens * price (120 * 0.50 = 60)
    assert pos.cash == pytest.approx(40.0)


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
    assert "zero fill" in trade.error
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
    # Falls back to requested: floor(100/0.50) = 200 tokens
    assert trade.tokens == 200.0
    assert trade.requested_tokens is None  # no partial fill, so not set
    assert pos.up_tokens == 200.0
