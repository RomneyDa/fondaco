"""Shared test fixtures."""

import logging
import logging.handlers

import pytest
from core.trader import Position, TickContext


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Prevent tests from writing to production log files.

    App modules configure the root logger with file handlers at import time.
    Remove those during tests so output doesn't pollute data/app.log.
    """
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.FileHandler)
    ]
    yield
    root.handlers = original_handlers


@pytest.fixture(autouse=True)
def _fail_on_integrity_violation(caplog):
    """Fail any test that triggers a POSITION INTEGRITY VIOLATION."""
    with caplog.at_level(logging.ERROR, logger="core.trader"):
        yield
    for record in caplog.records:
        if "POSITION INTEGRITY VIOLATION" in record.message:
            pytest.fail(
                "Test triggered a POSITION INTEGRITY VIOLATION: {}".format(
                    record.message
                )
            )


@pytest.fixture
def sample_dict():
    """A fully-populated sample dict matching PriceSample fields."""
    return {
        "timestamp_utc": "2025-01-01T00:01:00+00:00",
        "elapsed_seconds": 60.0,
        "btc_price_usd": 97000.0,
        "up_token_midpoint": 0.50,
        "down_token_midpoint": 0.50,
        "up_token_best_bid": 0.49,
        "up_token_best_ask": 0.51,
        "down_token_best_bid": 0.49,
        "down_token_best_ask": 0.51,
    }


@pytest.fixture
def metadata_dict():
    """Minimal metadata dict for trading."""
    return {
        "event_id": "evt-1",
        "event_slug": "btc-updown-5m-1700000100",
        "event_title": "BTC 5m Up/Down",
        "condition_id": "cond-1",
        "up_token_id": "up-token-123",
        "down_token_id": "down-token-456",
        "window_start_utc": "2025-01-01T00:00:00+00:00",
        "window_end_utc": "2025-01-01T00:05:00+00:00",
        "resolution_source": "chainlink",
    }


def make_tick_ctx(
    position=None,
    sample=None,
    tick_index=0,
    elapsed_fraction=0.5,
    elapsed_seconds=None,
    initial_btc_price=97000.0,
    history=None,
    metadata=None,
):
    """Helper to build a TickContext for strategy tests."""
    if position is None:
        position = Position(cash=100.0)
    if sample is None:
        sample = {
            "timestamp_utc": "2025-01-01T00:01:00+00:00",
            "elapsed_seconds": 150.0,
            "btc_price_usd": 97000.0,
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "up_token_best_bid": 0.49,
            "up_token_best_ask": 0.51,
            "down_token_best_bid": 0.49,
            "down_token_best_ask": 0.51,
        }
    if elapsed_seconds is None:
        elapsed_seconds = elapsed_fraction * 300.0
    return TickContext(
        tick_index=tick_index,
        sample=sample,
        history=history or [],
        position=position,
        metadata=metadata or {},
        initial_btc_price=initial_btc_price,
        elapsed_fraction=elapsed_fraction,
        elapsed_seconds=elapsed_seconds,
    )
