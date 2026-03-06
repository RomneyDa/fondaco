"""Tests for backtest path resolution."""

import os
from unittest.mock import patch

from core.defaults import MARKETS_DIR, market_sessions_dir, market_backtest_log


class TestResolveDataDir:
    def test_explicit_path_returned_as_is(self, tmp_path):
        from simulator.backtest import _resolve_data_dir
        d = str(tmp_path / "some_sessions")
        os.makedirs(d)
        assert _resolve_data_dir(d) == d

    def test_slug_key_resolves_to_market_sessions(self, tmp_path):
        from simulator.backtest import _resolve_data_dir
        slug_key = "btc-updown-5m"
        market_path = market_sessions_dir(slug_key)
        os.makedirs(market_path, exist_ok=True)
        result = _resolve_data_dir(slug_key)
        assert result == market_path

    def test_slug_with_trailing_dash(self, tmp_path):
        from simulator.backtest import _resolve_data_dir
        market_path = market_sessions_dir("btc-updown-5m")
        os.makedirs(market_path, exist_ok=True)
        result = _resolve_data_dir("btc-updown-5m-")
        assert result == market_path

    def test_nonexistent_returns_as_is(self):
        from simulator.backtest import _resolve_data_dir
        result = _resolve_data_dir("/nonexistent/path")
        assert result == "/nonexistent/path"


class TestResolveBacktestLog:
    def test_market_data_dir_returns_per_market_log(self):
        from simulator.backtest import _resolve_backtest_log
        sessions_dir = market_sessions_dir("btc-updown-5m")
        result = _resolve_backtest_log(sessions_dir)
        expected = market_backtest_log("btc-updown-5m")
        assert result == expected

    def test_arbitrary_path_returns_global_log(self, tmp_path):
        from simulator.backtest import _resolve_backtest_log
        result = _resolve_backtest_log(str(tmp_path))
        assert "backtest_log.jsonl" in result
        # Should NOT be under MARKETS_DIR
        assert not result.startswith(MARKETS_DIR)
