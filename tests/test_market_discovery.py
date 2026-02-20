"""Tests for market_discovery: pure functions and HTTP-mocked lookups."""

import json
from unittest.mock import MagicMock, patch

import pytest
from core.market_discovery import (
    compute_window_timestamp,
    get_event_slug,
    fetch_market_metadata,
    fetch_resolution,
    set_session,
)


# ---------------------------------------------------------------------------
# Pure functions (no HTTP)
# ---------------------------------------------------------------------------


class TestComputeWindowTimestamp:
    def test_basic(self):
        # 1700000150 // 300 = 5666667, * 300 = 1700000100
        ts = compute_window_timestamp(300, 1700000150.0)
        assert ts == 1700000100

    def test_exact_boundary(self):
        ts = compute_window_timestamp(300, 1700000100.0)
        assert ts == 1700000100

    def test_different_window_size(self):
        ts = compute_window_timestamp(900, 1700000500.0)
        assert ts == 1700000100  # 1700000500 // 900 = 1888889, * 900 = 1700000100


class TestGetEventSlug:
    def test_basic(self):
        assert get_event_slug("btc-updown-5m-", 1700000100) == "btc-updown-5m-1700000100"


# ---------------------------------------------------------------------------
# fetch_market_metadata (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchMarketMetadata:
    def _mock_session(self, json_response):
        session = MagicMock()
        resp = MagicMock()
        resp.json.return_value = json_response
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp
        return session

    def test_returns_metadata(self):
        event = {
            "id": "evt-1",
            "slug": "btc-updown-5m-1700000100",
            "title": "BTC 5m Up/Down",
            "markets": [{
                "conditionId": "cond-1",
                "clobTokenIds": json.dumps(["up-token-id", "down-token-id"]),
                "eventStartTime": "2025-01-01T00:00:00Z",
                "endDate": "2025-01-01T00:05:00Z",
                "resolutionSource": "chainlink",
            }],
        }
        session = self._mock_session([event])
        set_session(session)

        metadata = fetch_market_metadata("btc-updown-5m-", 1700000100)
        assert metadata is not None
        assert metadata.up_token_id == "up-token-id"
        assert metadata.down_token_id == "down-token-id"

    def test_returns_none_for_empty(self):
        session = self._mock_session([])
        set_session(session)
        assert fetch_market_metadata("btc-updown-5m-", 1700000100) is None

    def test_returns_none_for_no_markets(self):
        event = {"id": "evt-1", "slug": "test", "title": "Test", "markets": []}
        session = self._mock_session([event])
        set_session(session)
        assert fetch_market_metadata("btc-updown-5m-", 1700000100) is None


# ---------------------------------------------------------------------------
# fetch_resolution (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchResolution:
    def _mock_session(self, json_response):
        session = MagicMock()
        resp = MagicMock()
        resp.json.return_value = json_response
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp
        return session

    def test_resolved_up(self):
        event = {
            "markets": [{"closed": True, "outcomePrices": ["1", "0"]}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) == "Up"

    def test_resolved_down(self):
        event = {
            "markets": [{"closed": True, "outcomePrices": ["0", "1"]}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) == "Down"

    def test_not_closed_returns_none(self):
        event = {
            "markets": [{"closed": False, "outcomePrices": ["0.5", "0.5"]}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) is None

    def test_no_outcome_prices_returns_none(self):
        event = {
            "markets": [{"closed": True, "outcomePrices": None}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) is None

    def test_outcome_prices_as_json_string(self):
        """outcomePrices can be a JSON string or a list."""
        event = {
            "markets": [{"closed": True, "outcomePrices": '["1","0"]'}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) == "Up"

    def test_empty_events_returns_none(self):
        session = self._mock_session([])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) is None

    def test_indeterminate_returns_none(self):
        """Prices like ["0.5", "0.5"] (not resolved cleanly) return None."""
        event = {
            "markets": [{"closed": True, "outcomePrices": ["0.5", "0.5"]}],
        }
        session = self._mock_session([event])
        set_session(session)
        assert fetch_resolution("btc-updown-5m-", 1700000100) is None
