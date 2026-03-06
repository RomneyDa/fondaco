"""Tests for the HTTP control server with multi-market endpoints."""

import json
import threading
import time
import urllib.request
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def _make_mock_app_manager(markets=None):
    """Create a mock AppManager for testing."""
    mgr = MagicMock()
    mgr.running = True
    mgr.get_status.return_value = {
        "running": True,
        "markets": markets or [],
    }
    return mgr


def _start_server(mgr, port=0):
    """Start a ControlServer on a free port, return (server, port)."""
    from app.server import ControlServer
    # Use port 0 for OS-assigned free port
    server = ControlServer(mgr, port or 0)
    # Get actual port after binding
    actual_port = server._server.server_address[1]
    server.start()
    time.sleep(0.1)
    return server, actual_port


def _request(port, method, path, body=None):
    """Make an HTTP request and return parsed JSON."""
    url = "http://localhost:{}{}".format(port, path)
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestStatusEndpoint:
    def test_returns_app_status(self):
        mgr = _make_mock_app_manager()
        server, port = _start_server(mgr)
        try:
            result = _request(port, "GET", "/api/control/status")
            assert result["running"] is True
            assert "markets" in result
        finally:
            server.stop()


class TestMarketOn:
    def test_missing_slug_returns_400(self):
        mgr = _make_mock_app_manager()
        server, port = _start_server(mgr)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _request(port, "POST", "/api/control/market-on", {})
            assert exc_info.value.code == 400
        finally:
            server.stop()

    def test_success(self):
        mgr = _make_mock_app_manager()
        runner = MagicMock()
        runner.slug_key = "btc-updown-5m"
        mgr.start_market.return_value = runner
        server, port = _start_server(mgr)
        try:
            result = _request(port, "POST", "/api/control/market-on", {
                "slug_prefix": "btc-updown-5m-",
            })
            assert result["ok"] is True
            mgr.start_market.assert_called_once()
        finally:
            server.stop()


class TestTradeOn:
    def test_missing_slug_returns_400(self):
        mgr = _make_mock_app_manager()
        server, port = _start_server(mgr)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _request(port, "POST", "/api/control/trade-on", {
                    "strategy": "LateSniper",
                })
            assert exc_info.value.code == 400
        finally:
            server.stop()

    def test_missing_strategy_returns_400(self):
        mgr = _make_mock_app_manager()
        server, port = _start_server(mgr)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _request(port, "POST", "/api/control/trade-on", {
                    "slug_prefix": "btc-updown-5m-",
                })
            assert exc_info.value.code == 400
        finally:
            server.stop()

    def test_unknown_market_returns_400(self):
        mgr = _make_mock_app_manager()
        mgr.get_market.side_effect = ValueError("Market not running")
        server, port = _start_server(mgr)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _request(port, "POST", "/api/control/trade-on", {
                    "slug_prefix": "nonexistent-",
                    "strategy": "LateSniper",
                })
            assert exc_info.value.code == 400
        finally:
            server.stop()

    def test_success(self):
        mgr = _make_mock_app_manager()
        runner = MagicMock()
        mgr.get_market.return_value = runner
        server, port = _start_server(mgr)
        try:
            result = _request(port, "POST", "/api/control/trade-on", {
                "slug_prefix": "btc-updown-5m-",
                "strategy": "LateSniper",
                "cash": 50,
            })
            assert result["ok"] is True
            runner.enable_trading.assert_called_once()
        finally:
            server.stop()


class TestTradeOff:
    def test_kills_all_bots_on_market(self):
        mgr = _make_mock_app_manager()
        runner = MagicMock()
        mgr.get_market.return_value = runner
        server, port = _start_server(mgr)
        try:
            result = _request(port, "POST", "/api/control/trade-off", {
                "slug_prefix": "btc-updown-5m-",
            })
            assert result["ok"] is True
            runner.disable_trading.assert_called_once_with(strategy_name=None)
        finally:
            server.stop()


class TestSetCash:
    def test_slug_extracted_from_body(self):
        mgr = _make_mock_app_manager()
        runner = MagicMock()
        mgr.get_market.return_value = runner
        server, port = _start_server(mgr)
        try:
            result = _request(port, "POST", "/api/control/set-cash", {
                "slug_prefix": "btc-updown-5m-",
                "cash": 75.0,
                "strategy": "LateSniper",
            })
            assert result["ok"] is True
            runner.set_cash.assert_called_once_with(75.0, strategy_name="LateSniper")
        finally:
            server.stop()


class TestStopEndpoint:
    def test_stop(self):
        mgr = _make_mock_app_manager()
        server, port = _start_server(mgr)
        try:
            result = _request(port, "POST", "/api/control/stop")
            assert result["ok"] is True
            mgr.request_stop.assert_called_once()
        finally:
            server.stop()
