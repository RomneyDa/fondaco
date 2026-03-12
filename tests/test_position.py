"""Tests for Position: compute_trade_size, apply_trade, resolve, to/from_dict."""

import math
import pytest
from core.trader import Position


# ---------------------------------------------------------------------------
# compute_trade_size — buys
# ---------------------------------------------------------------------------


class TestComputeTradeSizeBuy:
    def test_basic_buy_up(self):
        pos = Position(cash=100.0)
        tokens, cost = pos.compute_trade_size("buy_up", 0.50, 50.0)
        assert tokens == 100.0  # 50 / 0.50
        assert cost == 50.0     # 100 * 0.50

    def test_buy_clamped_to_cash(self):
        pos = Position(cash=30.0)
        tokens, cost = pos.compute_trade_size("buy_up", 0.50, 100.0)
        assert tokens == 60.0  # 30 / 0.50
        assert cost == 30.0

    def test_buy_with_zero_cash(self):
        pos = Position(cash=0.0)
        assert pos.compute_trade_size("buy_up", 0.50, 50.0) == (0.0, 0.0)

    def test_buy_with_none_price(self):
        pos = Position(cash=100.0)
        assert pos.compute_trade_size("buy_up", None, 50.0) == (0.0, 0.0)

    def test_buy_with_zero_price(self):
        pos = Position(cash=100.0)
        assert pos.compute_trade_size("buy_up", 0.0, 50.0) == (0.0, 0.0)

    def test_buy_with_price_at_one(self):
        """Price >= 1.0 is invalid for binary markets."""
        pos = Position(cash=100.0)
        assert pos.compute_trade_size("buy_up", 1.0, 50.0) == (0.0, 0.0)

    def test_buy_with_price_just_below_one(self):
        pos = Position(cash=100.0)
        tokens, cost = pos.compute_trade_size("buy_up", 0.99, 50.0)
        assert tokens > 0
        assert cost > 0

    def test_buy_down(self):
        pos = Position(cash=100.0)
        tokens, cost = pos.compute_trade_size("buy_down", 0.40, 40.0)
        assert tokens == 100.0  # 40 / 0.40
        assert cost == 40.0

    def test_buy_negative_price(self):
        pos = Position(cash=100.0)
        assert pos.compute_trade_size("buy_up", -0.5, 50.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# compute_trade_size — sells
# ---------------------------------------------------------------------------


class TestComputeTradeSizeSell:
    def test_basic_sell_up(self):
        pos = Position(cash=0.0, up_tokens=100.0)
        tokens, proceeds = pos.compute_trade_size("sell_up", 0.60, 50.0)
        assert tokens == 50.0
        assert proceeds == 30.0  # 50 * 0.60

    def test_sell_clamped_to_holdings(self):
        pos = Position(cash=0.0, up_tokens=20.0)
        tokens, proceeds = pos.compute_trade_size("sell_up", 0.60, 100.0)
        assert tokens == 20.0

    def test_sell_down(self):
        pos = Position(cash=0.0, down_tokens=30.0)
        tokens, proceeds = pos.compute_trade_size("sell_down", 0.70, 30.0)
        assert tokens == 30.0
        assert proceeds == 21.0

    def test_sell_floors_to_4_decimals(self):
        """Sell amounts are floored (never rounded up) to 4 decimal places."""
        pos = Position(cash=0.0, up_tokens=10.12345)
        tokens, _ = pos.compute_trade_size("sell_up", 0.50, 10.12345)
        # math.floor(10.12345 * 10000) / 10000 = 10.1234
        assert tokens == 10.1234

    def test_sell_zero_tokens(self):
        pos = Position(cash=0.0, up_tokens=0.0)
        assert pos.compute_trade_size("sell_up", 0.50, 100.0) == (0.0, 0.0)

    def test_sell_none_price(self):
        pos = Position(cash=0.0, up_tokens=10.0)
        assert pos.compute_trade_size("sell_up", None, 10.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# compute_trade_size — unknown side
# ---------------------------------------------------------------------------


def test_unknown_side():
    pos = Position(cash=100.0)
    assert pos.compute_trade_size("unknown", 0.50, 50.0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# apply_trade
# ---------------------------------------------------------------------------


class TestApplyTrade:
    def test_buy_up_mutates(self):
        pos = Position(cash=100.0)
        pos.apply_trade("buy_up", 0.50, 10.0)
        assert pos.cash == pytest.approx(95.0)
        assert pos.up_tokens == 10.0

    def test_buy_down_mutates(self):
        pos = Position(cash=100.0)
        pos.apply_trade("buy_down", 0.40, 20.0)
        assert pos.cash == pytest.approx(92.0)
        assert pos.down_tokens == 20.0

    def test_sell_up_mutates(self):
        pos = Position(cash=0.0, up_tokens=50.0)
        pos.apply_trade("sell_up", 0.60, 10.0)
        assert pos.up_tokens == 40.0
        assert pos.cash == pytest.approx(6.0)

    def test_sell_down_mutates(self):
        pos = Position(cash=0.0, down_tokens=50.0)
        pos.apply_trade("sell_down", 0.70, 20.0)
        assert pos.down_tokens == 30.0
        assert pos.cash == pytest.approx(14.0)

    def test_zero_tokens_noop(self):
        pos = Position(cash=100.0)
        pos.apply_trade("buy_up", 0.50, 0.0)
        assert pos.cash == 100.0
        assert pos.up_tokens == 0.0

    def test_negative_tokens_noop(self):
        pos = Position(cash=100.0)
        pos.apply_trade("buy_up", 0.50, -5.0)
        assert pos.cash == 100.0


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_up(self):
        pos = Position(cash=50.0, up_tokens=20.0, down_tokens=10.0)
        pos.resolve("Up")
        assert pos.cash == pytest.approx(70.0)  # 50 + 20
        assert pos.up_tokens == 0.0
        assert pos.down_tokens == 0.0

    def test_resolve_down(self):
        pos = Position(cash=50.0, up_tokens=20.0, down_tokens=10.0)
        pos.resolve("Down")
        assert pos.cash == pytest.approx(60.0)  # 50 + 10
        assert pos.up_tokens == 0.0
        assert pos.down_tokens == 0.0

    def test_resolve_none_unchanged(self):
        pos = Position(cash=50.0, up_tokens=20.0, down_tokens=10.0)
        pos.resolve(None)
        assert pos.cash == 50.0
        assert pos.up_tokens == 20.0
        assert pos.down_tokens == 10.0

    def test_resolve_unknown_unchanged(self):
        pos = Position(cash=50.0, up_tokens=20.0, down_tokens=10.0)
        pos.resolve("Draw")
        assert pos.cash == 50.0

    def test_resolve_up_with_zero_up_tokens(self):
        """Up wins but we hold no up tokens — down tokens go to 0, cash unchanged."""
        pos = Position(cash=50.0, up_tokens=0.0, down_tokens=10.0)
        pos.resolve("Up")
        assert pos.cash == 50.0
        assert pos.down_tokens == 0.0


# ---------------------------------------------------------------------------
# to_dict / from_dict
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_roundtrip(self):
        pos = Position(cash=42.5, up_tokens=10.0, down_tokens=5.0)
        d = pos.to_dict()
        restored = Position.from_dict(d)
        assert restored.cash == 42.5
        assert restored.up_tokens == 10.0
        assert restored.down_tokens == 5.0

    def test_from_dict_missing_tokens(self):
        """from_dict defaults missing token keys to 0.0."""
        pos = Position.from_dict({"cash": 100.0})
        assert pos.cash == 100.0
        assert pos.up_tokens == 0.0
        assert pos.down_tokens == 0.0

    def test_to_dict_fields(self):
        d = Position(cash=1.0, up_tokens=2.0, down_tokens=3.0).to_dict()
        assert set(d.keys()) == {"cash", "up_tokens", "down_tokens"}


# ---------------------------------------------------------------------------
# reserve_for_order
# ---------------------------------------------------------------------------


class TestReserveForOrder:
    def test_buy_up_reserves_cash(self):
        pos = Position(cash=100.0)
        dollars, tokens = pos.reserve_for_order("buy_up", 0.20, 50.0)
        assert dollars == pytest.approx(10.0)  # 50 * 0.20
        assert tokens == 0.0
        assert pos.cash == pytest.approx(90.0)

    def test_buy_down_reserves_cash(self):
        pos = Position(cash=100.0)
        dollars, tokens = pos.reserve_for_order("buy_down", 0.30, 100.0)
        assert dollars == pytest.approx(30.0)
        assert pos.cash == pytest.approx(70.0)

    def test_sell_up_reserves_tokens(self):
        pos = Position(cash=0.0, up_tokens=50.0)
        dollars, tokens = pos.reserve_for_order("sell_up", 0.60, 20.0)
        assert dollars == 0.0
        assert tokens == 20.0
        assert pos.up_tokens == pytest.approx(30.0)

    def test_sell_down_reserves_tokens(self):
        pos = Position(cash=0.0, down_tokens=50.0)
        dollars, tokens = pos.reserve_for_order("sell_down", 0.70, 15.0)
        assert dollars == 0.0
        assert tokens == 15.0
        assert pos.down_tokens == pytest.approx(35.0)

    def test_unknown_side_noop(self):
        pos = Position(cash=100.0, up_tokens=10.0)
        dollars, tokens = pos.reserve_for_order("unknown", 0.50, 10.0)
        assert dollars == 0.0
        assert tokens == 0.0
        assert pos.cash == 100.0


# ---------------------------------------------------------------------------
# unreserve_for_order
# ---------------------------------------------------------------------------


class TestUnreserveForOrder:
    def test_buy_restores_cash(self):
        pos = Position(cash=80.0)
        pos.unreserve_for_order("buy_up", 20.0, 0.0)
        assert pos.cash == pytest.approx(100.0)

    def test_sell_up_restores_tokens(self):
        pos = Position(cash=0.0, up_tokens=30.0)
        pos.unreserve_for_order("sell_up", 0.0, 20.0)
        assert pos.up_tokens == pytest.approx(50.0)

    def test_sell_down_restores_tokens(self):
        pos = Position(cash=0.0, down_tokens=35.0)
        pos.unreserve_for_order("sell_down", 0.0, 15.0)
        assert pos.down_tokens == pytest.approx(50.0)

    def test_reserve_unreserve_roundtrip(self):
        """Reserve then unreserve should restore original state."""
        pos = Position(cash=100.0, up_tokens=50.0, down_tokens=30.0)
        # Reserve a buy
        d, t = pos.reserve_for_order("buy_up", 0.25, 40.0)
        assert pos.cash == pytest.approx(90.0)
        # Cancel — unreserve
        pos.unreserve_for_order("buy_up", d, t)
        assert pos.cash == pytest.approx(100.0)
        assert pos.up_tokens == 50.0
        assert pos.down_tokens == 30.0

    def test_reserve_unreserve_sell_roundtrip(self):
        pos = Position(cash=0.0, up_tokens=50.0)
        d, t = pos.reserve_for_order("sell_up", 0.60, 20.0)
        pos.unreserve_for_order("sell_up", d, t)
        assert pos.up_tokens == pytest.approx(50.0)
