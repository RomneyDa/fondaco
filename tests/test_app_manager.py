"""Tests for the AppManager central manager."""

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


def _import_app_manager():
    from app import app_manager
    return app_manager


def _blocking_run():
    """A mock run() that blocks until the runner's running flag is cleared."""
    # This is set as a side_effect, so 'self' is the mock itself.
    # We use a simple sleep loop instead.
    while True:
        time.sleep(0.05)


def _make_blocking_runner(slug_prefix="btc-updown-5m-"):
    """Create a mock runner whose run() blocks until stopped."""
    stop_event = threading.Event()
    mock_runner = MagicMock()
    mock_runner.slug_prefix = slug_prefix
    mock_runner.slug_key = slug_prefix.rstrip("-")
    mock_runner.market_type = "updown"
    mock_runner.window_duration = 300
    mock_runner.run.side_effect = lambda: stop_event.wait()
    mock_runner.request_stop.side_effect = lambda: stop_event.set()
    mock_runner._stop_event = stop_event
    return mock_runner


class TestStartMarket:
    def test_start_creates_runner(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            MockRunner.return_value = mock_runner

            runner = mgr.start_market("btc-updown-5m-")
            try:
                assert runner is mock_runner
                assert "btc-updown-5m" in mgr._markets
                MockRunner.assert_called_once()
            finally:
                mgr.request_stop()
                time.sleep(0.2)

    def test_duplicate_market_raises(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            MockRunner.return_value = mock_runner

            mgr.start_market("btc-updown-5m-")
            try:
                with pytest.raises(ValueError, match="already running"):
                    mgr.start_market("btc-updown-5m-")
            finally:
                mgr.request_stop()
                time.sleep(0.2)


class TestGetMarket:
    def test_get_existing(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            MockRunner.return_value = mock_runner

            mgr.start_market("btc-updown-5m-")
            try:
                result = mgr.get_market("btc-updown-5m-")
                assert result is mock_runner
            finally:
                mgr.request_stop()
                time.sleep(0.2)

    def test_get_nonexistent_raises(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            with pytest.raises(ValueError, match="not running"):
                mgr.get_market("nonexistent-")


class TestGetStatus:
    def test_empty_status(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            status = mgr.get_status()
            assert status["running"] is True
            assert status["markets"] == []

    def test_status_with_market(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            mock_runner.get_status.return_value = {
                "slug_key": "btc-updown-5m",
                "running": True,
                "strategies": [],
            }
            MockRunner.return_value = mock_runner

            mgr.start_market("btc-updown-5m-")
            try:
                status = mgr.get_status()
                assert len(status["markets"]) == 1
                assert status["markets"][0]["slug_key"] == "btc-updown-5m"
            finally:
                mgr.request_stop()
                time.sleep(0.2)


class TestRequestStop:
    def test_stops_all_runners(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            runners = []
            for slug in ["btc-updown-5m-", "btc-updown-15m-"]:
                mock_runner = _make_blocking_runner(slug)
                MockRunner.return_value = mock_runner
                mgr.start_market(slug)
                runners.append(mock_runner)

            mgr.request_stop()
            assert mgr.running is False
            for r in runners:
                r.request_stop.assert_called()
            time.sleep(0.2)


class TestRestore:
    def test_restore_from_state_file(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        state = {
            "pid": os.getpid(),
            "port": 8420,
            "markets": [
                {
                    "slug_prefix": "btc-updown-5m-",
                    "slug_key": "btc-updown-5m",
                    "market_type": "updown",
                    "window_duration": 300,
                },
            ],
        }
        with open(state_file, "w") as f:
            json.dump(state, f)

        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", state_file), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            MockRunner.return_value = mock_runner

            mgr.restore()
            try:
                assert "btc-updown-5m" in mgr._markets
                # Give restore time to call restore_strategies
                time.sleep(1)
                mock_runner.restore_strategies.assert_called_once()
            finally:
                mgr.request_stop()
                time.sleep(0.2)

    def test_restore_no_state_file(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "nonexistent.json")):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            mgr.restore()
            assert len(mgr._markets) == 0


class TestStopMarket:
    def test_stop_nonexistent_raises(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            with pytest.raises(ValueError, match="not running"):
                mgr.stop_market("nonexistent-")

    def test_stop_existing_market(self, tmp_path):
        with patch("app.app_manager.LoggingSession"), \
             patch("app.app_manager.RTDSClient"), \
             patch("app.app_manager.market_discovery"), \
             patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "app.json")), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_blocking_runner()
            MockRunner.return_value = mock_runner

            mgr.start_market("btc-updown-5m-")
            assert "btc-updown-5m" in mgr._markets

            mgr.stop_market("btc-updown-5m-")
            time.sleep(0.3)
            # After stop + thread cleanup, market should be gone
            assert "btc-updown-5m" not in mgr._markets
