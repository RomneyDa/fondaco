"""Tests for core/defaults.py market path helpers."""

import os

from core.defaults import (
    BASE_DIR,
    MARKETS_DIR,
    _slug_key,
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
        assert result.endswith("ticks.sock")

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
            market_socket_path,
            market_workers_dir,
            market_strategies_file,
            market_trades_file,
            market_session_log,
            market_backtest_log,
        ]
        for fn in helpers:
            result = fn(slug)
            assert MARKETS_DIR in result, "{} not under MARKETS_DIR".format(fn.__name__)
