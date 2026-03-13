"""Tests for the AppManager central manager."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest


def _import_app_manager():
    from app import app_manager
    return app_manager


def _make_mock_runner(slug_prefix="btc-updown-5m-"):
    """Create a mock MarketRunner."""
    mock_runner = MagicMock()
    mock_runner.slug_prefix = slug_prefix
    mock_runner.slug_key = slug_prefix.rstrip("-")
    mock_runner.market_type = "updown"
    mock_runner.window_duration = 300
    mock_runner._stop_after_session = False
    return mock_runner


def _patch_manager(tmp_path):
    """Context manager that patches away collector spawning and state file."""
    return patch.multiple(
        "app.app_manager",
        APP_STATE_FILE=str(tmp_path / "app.json"),
    )


class TestStartMarket:
    def test_start_creates_runner(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                runner = mgr.start_market("btc-updown-5m-")

            assert runner is mock_runner
            assert "btc-updown-5m" in mgr._markets
            MockRunner.assert_called_once()

    def test_duplicate_market_raises(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.start_market("btc-updown-5m-")
                with pytest.raises(ValueError, match="already running"):
                    mgr.start_market("btc-updown-5m-")


class TestGetMarket:
    def test_get_existing(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.start_market("btc-updown-5m-")

            result = mgr.get_market("btc-updown-5m-")
            assert result is mock_runner

    def test_get_nonexistent_raises(self, tmp_path):
        with _patch_manager(tmp_path):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            with pytest.raises(ValueError, match="not running"):
                mgr.get_market("nonexistent-")


class TestGetStatus:
    def test_empty_status(self, tmp_path):
        with _patch_manager(tmp_path):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            status = mgr.get_status()
            assert status["running"] is True
            assert status["markets"] == []

    def test_status_with_market(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            mock_runner.get_status.return_value = {
                "slug_key": "btc-updown-5m",
                "running": True,
                "strategies": [],
            }
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.start_market("btc-updown-5m-")

            status = mgr.get_status()
            assert len(status["markets"]) == 1
            assert status["markets"][0]["slug_key"] == "btc-updown-5m"


class TestRequestStop:
    def test_stops_all_runners(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            runners = []
            for slug in ["btc-updown-5m-", "btc-updown-15m-"]:
                mock_runner = _make_mock_runner(slug)
                MockRunner.return_value = mock_runner
                with patch.object(mgr, "_ensure_collector"):
                    mgr.start_market(slug)
                runners.append(mock_runner)

            mgr.request_stop()
            assert mgr.running is False
            for r in runners:
                r.request_stop.assert_called()


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

        with patch("app.app_manager.APP_STATE_FILE", state_file), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.restore()

            assert "btc-updown-5m" in mgr._markets
            mock_runner.restore_strategies.assert_called_once()

    def test_restore_no_state_file(self, tmp_path):
        with patch("app.app_manager.APP_STATE_FILE", str(tmp_path / "nonexistent.json")):
            mod = _import_app_manager()
            mgr = mod.AppManager()
            mgr.restore()
            assert len(mgr._markets) == 0


class TestStopMarket:
    def test_stop_existing_market(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.start_market("btc-updown-5m-")

            assert "btc-updown-5m" in mgr._markets

            with patch.object(mgr, "_stop_collector"):
                mgr.stop_market("btc-updown-5m-")

            assert "btc-updown-5m" not in mgr._markets
            mock_runner.request_stop.assert_called_once()

    def test_stop_after_session(self, tmp_path):
        with _patch_manager(tmp_path), \
             patch("app.app_manager.MarketRunner") as MockRunner:
            mod = _import_app_manager()
            mgr = mod.AppManager()

            mock_runner = _make_mock_runner()
            MockRunner.return_value = mock_runner

            with patch.object(mgr, "_ensure_collector"):
                mgr.start_market("btc-updown-5m-")

            with patch.object(mgr, "_signal_collector_stop_after_session") as mock_signal:
                mgr.stop_market("btc-updown-5m-", after_session=True)

            # Market still in dict (cleaned up later by _cleanup_stopped_collectors)
            assert "btc-updown-5m" in mgr._markets
            assert mock_runner._stop_after_session is True
            mock_signal.assert_called_once_with("btc-updown-5m")
