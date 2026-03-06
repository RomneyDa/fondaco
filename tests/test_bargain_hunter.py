"""Tests for the BargainHunter strategy."""

import pytest
from core.trader import HOLD, Position, TradeAction
from bots.updown.bargain_hunter import BargainHunter
from tests.conftest import make_tick_ctx


# ---------------------------------------------------------------------------
# Side selection
# ---------------------------------------------------------------------------


class TestSideSelection:
    def test_pricier_mode_buys_higher_midpoint(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.55,
            "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_up"

    def test_pricier_mode_buys_down_when_down_higher(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.45,
            "down_token_midpoint": 0.55,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_down"

    def test_cheaper_mode_buys_lower_midpoint(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="cheaper")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.55,
            "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.side == "buy_down"


# ---------------------------------------------------------------------------
# GTD order properties
# ---------------------------------------------------------------------------


class TestGTDOrderOutput:
    def test_returns_limit_order_with_expiration(self):
        s = BargainHunter(buy_price=0.15, expiration_frac=0.34, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        action = s.on_tick(ctx)
        assert action.price == 0.15
        assert action.expiration_seconds is not None
        # TTL = (0.34 - 10/300) * 300 = (0.34 - 0.033) * 300 ≈ 92s
        assert action.expiration_seconds == pytest.approx(
            (0.34 - 10.0 / 300) * 300, abs=1.0,
        )

    def test_uses_full_cash(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=75.0))
        action = s.on_tick(ctx)
        assert action.amount == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# One-shot behavior
# ---------------------------------------------------------------------------


class TestOneShot:
    def test_holds_after_first_order(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        action1 = s.on_tick(ctx)
        assert action1.side != "hold"

        action2 = s.on_tick(ctx)
        assert action2.side == "hold"

    def test_resets_between_sessions(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        s.on_tick(ctx)  # places order

        # New session resets
        s.on_session_start({"window": 300})
        action = s.on_tick(ctx)
        assert action.side != "hold"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_holds_when_midpoints_none(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": None,
            "down_token_midpoint": None,
            "elapsed_seconds": 10.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=10.0 / 300, position=Position(cash=100.0))
        assert s.on_tick(ctx).side == "hold"

    def test_holds_when_past_expiration_frac(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.34, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 150.0,
        }
        # elapsed_fraction = 0.50 > expiration_frac 0.34
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.50, position=Position(cash=100.0))
        assert s.on_tick(ctx).side == "hold"

    def test_holds_at_exactly_expiration_frac(self):
        s = BargainHunter(buy_price=0.20, expiration_frac=0.34, side_mode="pricier")
        s.on_session_start({"window": 300})
        sample = {
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "elapsed_seconds": 102.0,
        }
        ctx = make_tick_ctx(sample=sample, elapsed_fraction=0.34, position=Position(cash=100.0))
        assert s.on_tick(ctx).side == "hold"


# ---------------------------------------------------------------------------
# Param grid
# ---------------------------------------------------------------------------


class TestParamGrid:
    def test_param_grid_keys(self):
        s = BargainHunter()
        grid = s.param_grid()
        assert "buy_price" in grid
        assert "expiration_frac" in grid
        assert "side_mode" in grid

    def test_param_grid_size(self):
        s = BargainHunter()
        grid = s.param_grid()
        total = 1
        for v in grid.values():
            total *= len(v)
        assert total == 968

    def test_param_grid_has_btc_cancel_bps(self):
        s = BargainHunter()
        grid = s.param_grid()
        assert "btc_cancel_bps" in grid


# ---------------------------------------------------------------------------
# BTC price guard
# ---------------------------------------------------------------------------


class TestBTCPriceGuard:
    def test_cancel_when_btc_drops_for_buy_up(self):
        """buy_up order should be cancelled when BTC drops past threshold."""
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier",
                          btc_cancel_bps=10.0)
        s.on_session_start({"window": 300})

        # Tick 0: place order (up is pricier)
        sample0 = {
            "up_token_midpoint": 0.55, "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0, "btc_price_usd": 90000.0,
        }
        ctx0 = make_tick_ctx(sample=sample0, elapsed_fraction=10.0/300,
                             position=Position(cash=100.0), initial_btc_price=90000.0)
        action0 = s.on_tick(ctx0)
        assert action0.side == "buy_up"

        # Tick 1: BTC dropped 15 bps (>10) — should cancel
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 30.0, "btc_price_usd": 89865.0,  # -15 bps
        }
        from core.trader import OpenOrder
        fake_order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=500.0, dollars_reserved=100.0,
            tokens_reserved=0.0, tick_placed=0, expiration=150.0,
        )
        ctx1 = make_tick_ctx(sample=sample1, elapsed_fraction=30.0/300,
                             position=Position(cash=0.0), initial_btc_price=90000.0)
        ctx1.open_orders = [fake_order]
        action1 = s.on_tick(ctx1)
        assert action1.side == "cancel_up"

    def test_no_cancel_when_within_threshold(self):
        """buy_up order should NOT be cancelled when BTC drop is below threshold."""
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier",
                          btc_cancel_bps=10.0)
        s.on_session_start({"window": 300})

        # Tick 0: place order
        sample0 = {
            "up_token_midpoint": 0.55, "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0, "btc_price_usd": 90000.0,
        }
        ctx0 = make_tick_ctx(sample=sample0, elapsed_fraction=10.0/300,
                             position=Position(cash=100.0), initial_btc_price=90000.0)
        s.on_tick(ctx0)

        # Tick 1: BTC dropped only 5 bps (<10) — should hold
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 30.0, "btc_price_usd": 89955.0,  # -5 bps
        }
        from core.trader import OpenOrder
        fake_order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=500.0, dollars_reserved=100.0,
            tokens_reserved=0.0, tick_placed=0, expiration=150.0,
        )
        ctx1 = make_tick_ctx(sample=sample1, elapsed_fraction=30.0/300,
                             position=Position(cash=0.0), initial_btc_price=90000.0)
        ctx1.open_orders = [fake_order]
        action1 = s.on_tick(ctx1)
        assert action1.side == "hold"

    def test_cancel_when_btc_rises_for_buy_down(self):
        """buy_down order should be cancelled when BTC rises past threshold."""
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="cheaper",
                          btc_cancel_bps=10.0)
        s.on_session_start({"window": 300})

        # Tick 0: place buy_down (down is cheaper when up > down)
        sample0 = {
            "up_token_midpoint": 0.55, "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0, "btc_price_usd": 90000.0,
        }
        ctx0 = make_tick_ctx(sample=sample0, elapsed_fraction=10.0/300,
                             position=Position(cash=100.0), initial_btc_price=90000.0)
        action0 = s.on_tick(ctx0)
        assert action0.side == "buy_down"

        # Tick 1: BTC rose 15 bps (>10) — should cancel_down
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 30.0, "btc_price_usd": 90135.0,  # +15 bps
        }
        from core.trader import OpenOrder
        fake_order = OpenOrder(
            order_id="sim-1", side="buy_down", token="down",
            price=0.20, tokens=500.0, dollars_reserved=100.0,
            tokens_reserved=0.0, tick_placed=0, expiration=150.0,
        )
        ctx1 = make_tick_ctx(sample=sample1, elapsed_fraction=30.0/300,
                             position=Position(cash=0.0), initial_btc_price=90000.0)
        ctx1.open_orders = [fake_order]
        action1 = s.on_tick(ctx1)
        assert action1.side == "cancel_down"

    def test_stops_monitoring_after_fill(self):
        """Once order fills (open_orders empty), strategy returns HOLD."""
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier",
                          btc_cancel_bps=10.0)
        s.on_session_start({"window": 300})

        # Tick 0: place order
        sample0 = {
            "up_token_midpoint": 0.55, "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0, "btc_price_usd": 90000.0,
        }
        ctx0 = make_tick_ctx(sample=sample0, elapsed_fraction=10.0/300,
                             position=Position(cash=100.0), initial_btc_price=90000.0)
        s.on_tick(ctx0)

        # Tick 1: order already filled (no open_orders) — should just hold
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 30.0, "btc_price_usd": 89800.0,  # huge drop but irrelevant
        }
        ctx1 = make_tick_ctx(sample=sample1, elapsed_fraction=30.0/300,
                             position=Position(cash=0.0), initial_btc_price=90000.0)
        ctx1.open_orders = []  # filled
        action1 = s.on_tick(ctx1)
        assert action1.side == "hold"

    def test_no_guard_when_btc_cancel_bps_none(self):
        """When btc_cancel_bps is None, no cancel logic runs."""
        s = BargainHunter(buy_price=0.20, expiration_frac=0.50, side_mode="pricier",
                          btc_cancel_bps=None)
        s.on_session_start({"window": 300})

        # Place order
        sample0 = {
            "up_token_midpoint": 0.55, "down_token_midpoint": 0.45,
            "elapsed_seconds": 10.0, "btc_price_usd": 90000.0,
        }
        ctx0 = make_tick_ctx(sample=sample0, elapsed_fraction=10.0/300,
                             position=Position(cash=100.0), initial_btc_price=90000.0)
        s.on_tick(ctx0)

        # Even with huge BTC drop, no cancel (guard disabled)
        sample1 = {
            "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
            "elapsed_seconds": 30.0, "btc_price_usd": 89000.0,  # massive drop
        }
        from core.trader import OpenOrder
        fake_order = OpenOrder(
            order_id="sim-1", side="buy_up", token="up",
            price=0.20, tokens=500.0, dollars_reserved=100.0,
            tokens_reserved=0.0, tick_placed=0, expiration=150.0,
        )
        ctx1 = make_tick_ctx(sample=sample1, elapsed_fraction=30.0/300,
                             position=Position(cash=0.0), initial_btc_price=90000.0)
        ctx1.open_orders = [fake_order]
        action1 = s.on_tick(ctx1)
        assert action1.side == "hold"  # just hold, no cancel
