"""Tests for window_ops: wait_for_next_window, fetch_metadata_with_retry, wait_for_resolution."""

from unittest.mock import patch, MagicMock

import pytest
from core.window_ops import (
    wait_for_next_window,
    fetch_metadata_with_retry,
    wait_for_resolution,
)


# ---------------------------------------------------------------------------
# wait_for_next_window
# ---------------------------------------------------------------------------


class TestWaitForNextWindow:
    def test_within_first_3_seconds_returns_current(self):
        """If we're within 3s of current window start, use it immediately."""
        # current_window_start = 1700000100 (for 300s windows at t=1700000101)
        with patch("core.window_ops.time") as mock_time:
            mock_time.time.return_value = 1700000101.0  # 1s into window
            mock_time.monotonic.return_value = 1000.0
            result = wait_for_next_window(300, lambda: True)
            assert result == 1700000100

    def test_waits_for_next_window(self):
        """If past the 3s grace period, waits for the next window."""
        with patch("core.window_ops.time") as mock_time:
            # 100s into current window -> wait ~200s for next
            mock_time.time.return_value = 1700000200.0
            mono_start = 1000.0
            mock_time.monotonic.side_effect = [
                mono_start,       # first monotonic check (compute deadline)
                mono_start + 200, # loop: time >= deadline -> exit
            ]
            mock_time.sleep = MagicMock()

            from datetime import datetime, timezone
            mock_time.fromtimestamp = MagicMock()  # not needed

            result = wait_for_next_window(300, lambda: True)
            # Next window after 1700000200 is 1700000400
            assert result == 1700000400

    def test_aborts_when_not_running(self):
        """If is_running returns False, exits the wait loop."""
        with patch("core.window_ops.time") as mock_time:
            mock_time.time.return_value = 1700000200.0
            mono_start = 1000.0
            mock_time.monotonic.side_effect = [mono_start, mono_start]
            mock_time.sleep = MagicMock()

            result = wait_for_next_window(300, lambda: False)
            assert result == 1700000400  # still returns the target

    def test_on_waiting_callback(self):
        """on_waiting callback is called during wait."""
        callback = MagicMock()
        with patch("core.window_ops.time") as mock_time:
            mock_time.time.return_value = 1700000200.0
            mono_start = 1000.0
            # Run loop once then exit
            mock_time.monotonic.side_effect = [
                mono_start,      # compute deadline
                mono_start,      # first check: < deadline, enter loop
                mono_start + 1,  # remaining calc
                mono_start + 200, # second check: >= deadline, exit
            ]
            mock_time.sleep = MagicMock()

            wait_for_next_window(300, lambda: True, on_waiting=callback)
            assert callback.call_count >= 1


# ---------------------------------------------------------------------------
# fetch_metadata_with_retry
# ---------------------------------------------------------------------------


class TestFetchMetadataWithRetry:
    def test_returns_on_first_success(self):
        metadata = MagicMock()
        with patch("core.window_ops.fetch_market_metadata", return_value=metadata), \
             patch("core.window_ops.time.sleep"):
            result = fetch_metadata_with_retry("btc-updown-5m-", 1700000100)
            assert result is metadata

    def test_retries_on_failure(self):
        metadata = MagicMock()
        with patch("core.window_ops.fetch_market_metadata", side_effect=[None, None, metadata]), \
             patch("core.window_ops.time.sleep"):
            result = fetch_metadata_with_retry("btc-updown-5m-", 1700000100, max_retries=5)
            assert result is metadata

    def test_returns_none_after_all_retries(self):
        with patch("core.window_ops.fetch_market_metadata", return_value=None), \
             patch("core.window_ops.time.sleep"):
            result = fetch_metadata_with_retry("btc-updown-5m-", 1700000100, max_retries=3)
            assert result is None


# ---------------------------------------------------------------------------
# wait_for_resolution
# ---------------------------------------------------------------------------


class TestWaitForResolution:
    def test_returns_outcome_when_resolved(self):
        with patch("core.window_ops.fetch_resolution", return_value="Up"), \
             patch("core.window_ops.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.0]
            mock_time.sleep = MagicMock()
            result = wait_for_resolution("btc-", 100, timeout=60, poll_interval=5, is_running=lambda: True)
            assert result == "Up"

    def test_returns_none_on_timeout(self):
        with patch("core.window_ops.fetch_resolution", return_value=None), \
             patch("core.window_ops.time") as mock_time:
            # Start at 0, deadline at 10, immediately past
            mock_time.monotonic.side_effect = [0.0, 100.0]
            mock_time.sleep = MagicMock()
            result = wait_for_resolution("btc-", 100, timeout=10, poll_interval=5, is_running=lambda: True)
            assert result is None

    def test_aborts_when_not_running(self):
        with patch("core.window_ops.fetch_resolution", return_value=None), \
             patch("core.window_ops.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 0.0]
            mock_time.sleep = MagicMock()
            result = wait_for_resolution("btc-", 100, timeout=60, poll_interval=5, is_running=lambda: False)
            assert result is None
