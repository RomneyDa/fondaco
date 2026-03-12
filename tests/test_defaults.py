"""Tests for core/defaults.py: market path helpers and compute_taker_fee."""

import os
import pytest

from core.defaults import (
    BASE_DIR,
    MARKETS_DIR,
    _slug_key,
    compute_taker_fee,
    market_data_dir,
    market_sessions_dir,
    market_live_dir,
    market_socket_path,
    market_workers_dir,
    market_strategies_file,
    market_trades_file,
    market_session_log,
    market_backtest_log,
)


class TestSlugKey:
    def test_strips_trailing_dash(self):
        assert _slug_key("btc-updown-5m-") == "btc-updown-5m"

    def test_no_trailing_dash(self):
        assert _slug_key("btc-updown-5m") == "btc-updown-5m"

    def test_multiple_trailing_dashes(self):
        assert _slug_key("foo--") == "foo"


class TestMarketPaths:
    def test_market_data_dir(self):
        result = market_data_dir("btc-updown-5m")
        assert result == os.path.join(MARKETS_DIR, "btc-updown-5m")

    def test_market_sessions_dir(self):
        result = market_sessions_dir("btc-updown-5m")
        assert result.endswith(os.path.join("btc-updown-5m", "sessions"))

    def test_market_live_dir(self):
        result = market_live_dir("btc-updown-5m")
        assert result.endswith(os.path.join("btc-updown-5m", "live"))

    def test_market_socket_path(self):
        result = market_socket_path("btc-updown-5m")
        assert "btc-updown-5m" in result
        assert result.startswith("/tmp/")

    def test_market_workers_dir(self):
        result = market_workers_dir("btc-updown-5m")
        assert result.endswith(os.path.join("btc-updown-5m", "workers"))

    def test_market_strategies_file(self):
        result = market_strategies_file("btc-updown-5m")
        assert result.endswith("strategies.json")

    def test_market_trades_file(self):
        result = market_trades_file("btc-updown-5m")
        assert result.endswith("bot_trades.jsonl")

    def test_market_session_log(self):
        result = market_session_log("btc-updown-5m")
        assert result.endswith("bot_log.jsonl")

    def test_market_backtest_log(self):
        result = market_backtest_log("btc-updown-5m")
        assert result.endswith("backtest_log.jsonl")

    def test_all_paths_under_markets_dir(self):
        slug = "test-market"
        helpers = [
            market_data_dir,
            market_sessions_dir,
            market_live_dir,
            # market_socket_path uses /tmp (Unix socket path limit), not MARKETS_DIR
            market_workers_dir,
            market_strategies_file,
            market_trades_file,
            market_session_log,
            market_backtest_log,
        ]
        for fn in helpers:
            result = fn(slug)
            assert MARKETS_DIR in result, "{} not under MARKETS_DIR".format(fn.__name__)


# ---------------------------------------------------------------------------
# compute_taker_fee
# ---------------------------------------------------------------------------


class TestComputeTakerFee:
    def test_normal_fee(self):
        # fee = tokens * price * 0.25 * (price * (1-price))^2
        fee = compute_taker_fee(0.50, 100.0)
        assert fee > 0
        # At p=0.50: fee = 100 * 0.50 * 0.25 * (0.25)^2 = 50 * 0.25 * 0.0625 = 0.78125
        assert fee == pytest.approx(0.78125)

    def test_max_fee_at_midpoint(self):
        """Fee is maximized at price=0.50 for a given token count."""
        fee_50 = compute_taker_fee(0.50, 100.0)
        fee_30 = compute_taker_fee(0.30, 100.0)
        fee_70 = compute_taker_fee(0.70, 100.0)
        assert fee_50 > fee_30
        assert fee_50 > fee_70

    def test_fee_near_zero_price(self):
        fee = compute_taker_fee(0.01, 100.0)
        assert fee > 0
        assert fee < 0.01  # very small

    def test_fee_near_one_price(self):
        fee = compute_taker_fee(0.99, 100.0)
        assert fee > 0
        assert fee < 0.01  # very small

    def test_disabled(self):
        assert compute_taker_fee(0.50, 100.0, enabled=False) == 0.0

    def test_price_zero(self):
        assert compute_taker_fee(0.0, 100.0) == 0.0

    def test_price_one(self):
        assert compute_taker_fee(1.0, 100.0) == 0.0

    def test_price_negative(self):
        assert compute_taker_fee(-0.5, 100.0) == 0.0

    def test_tokens_zero(self):
        assert compute_taker_fee(0.50, 0.0) == 0.0

    def test_tokens_negative(self):
        assert compute_taker_fee(0.50, -10.0) == 0.0

    def test_socket_path_uses_tmp(self):
        result = market_socket_path("btc-updown-5m")
        assert result.startswith("/tmp/")
