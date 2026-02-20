"""Tests for data models: PriceSample, MarketMetadata, SessionData."""

import json
from dataclasses import asdict

from core.models import PriceSample, MarketMetadata, SessionData


class TestPriceSample:
    def test_all_none_optionals(self):
        s = PriceSample(
            timestamp_utc="2025-01-01T00:00:00Z",
            elapsed_seconds=0.0,
            btc_price_usd=None,
            up_token_midpoint=None,
            down_token_midpoint=None,
            up_token_best_bid=None,
            up_token_best_ask=None,
            down_token_best_bid=None,
            down_token_best_ask=None,
        )
        d = asdict(s)
        assert d["btc_price_usd"] is None
        assert d["up_token_midpoint"] is None

    def test_asdict_roundtrip(self):
        s = PriceSample(
            timestamp_utc="2025-01-01T00:01:00Z",
            elapsed_seconds=60.0,
            btc_price_usd=97000.0,
            up_token_midpoint=0.50,
            down_token_midpoint=0.50,
            up_token_best_bid=0.49,
            up_token_best_ask=0.51,
            down_token_best_bid=0.49,
            down_token_best_ask=0.51,
        )
        d = asdict(s)
        assert d["elapsed_seconds"] == 60.0
        assert d["btc_price_usd"] == 97000.0


class TestSessionData:
    def test_to_json_roundtrip(self):
        metadata = MarketMetadata(
            event_id="evt-1",
            event_slug="btc-updown-5m-100",
            event_title="BTC Up/Down",
            condition_id="cond-1",
            up_token_id="up-123",
            down_token_id="down-456",
            window_start_utc="2025-01-01T00:00:00Z",
            window_end_utc="2025-01-01T00:05:00Z",
            resolution_source="chainlink",
        )
        sd = SessionData(
            metadata=metadata,
            initial_btc_price_usd=97000.0,
            final_btc_price_usd=97100.0,
            outcome="Up",
            outcome_prices=["1", "0"],
        )
        raw = sd.to_json()
        parsed = json.loads(raw)
        assert parsed["outcome"] == "Up"
        assert parsed["metadata"]["up_token_id"] == "up-123"

    def test_default_fields(self):
        """Each instance gets its own default lists, not shared."""
        metadata = MarketMetadata(
            event_id="1", event_slug="s", event_title="t",
            condition_id="c", up_token_id="u", down_token_id="d",
            window_start_utc="", window_end_utc="", resolution_source="",
        )
        s1 = SessionData(metadata=metadata, initial_btc_price_usd=None,
                         final_btc_price_usd=None, outcome=None, outcome_prices=None)
        s2 = SessionData(metadata=metadata, initial_btc_price_usd=None,
                         final_btc_price_usd=None, outcome=None, outcome_prices=None)
        s1.samples.append("test")
        assert len(s2.samples) == 0  # not shared
