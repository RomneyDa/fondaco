"""Shared window lifecycle operations used by both the collector and trading bot.

Provides the three core functions for each market session:
1. Wait for the next window to start
2. Fetch market metadata with retries
3. Poll for resolution after the window ends
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from core.market_discovery import fetch_market_metadata, fetch_resolution

logger = logging.getLogger(__name__)


def wait_for_next_window(window_duration, is_running, on_waiting=None):
    # type: (int, Callable[[], bool], Optional[Callable[[datetime], None]]) -> int
    """Wait until the start of the next window. Returns window timestamp.

    Args:
        window_duration: window length in seconds (e.g. 300 for 5m)
        is_running: callable returning False to abort the wait
        on_waiting: optional callback(next_dt) called each second while
                    waiting — useful for writing live status updates
    """
    now = time.time()
    current_window_start = (int(now) // window_duration) * window_duration
    next_window_start = current_window_start + window_duration

    # If we're in the first 3 seconds of the current window, use it
    if now - current_window_start < 3:
        return current_window_start

    # Otherwise wait for the next window
    wait_seconds = next_window_start - now
    next_dt = datetime.fromtimestamp(next_window_start, tz=timezone.utc)
    logger.info("Waiting %.0fs for next window at %s", wait_seconds, next_dt.isoformat())

    mono_deadline = time.monotonic() + wait_seconds
    while time.monotonic() < mono_deadline and is_running():
        if on_waiting is not None:
            on_waiting(next_dt)
        remaining = mono_deadline - time.monotonic()
        time.sleep(min(1.0, max(0, remaining)))

    return next_window_start


def fetch_metadata_with_retry(slug_prefix, window_ts, max_retries=5):
    # type: (str, int, int) -> Optional[Any]
    """Fetch market metadata with retries (market may not be created yet)."""
    for attempt in range(max_retries):
        try:
            metadata = fetch_market_metadata(slug_prefix, window_ts)
            if metadata:
                return metadata
        except Exception as e:
            logger.warning("Metadata fetch attempt %d failed: %s", attempt + 1, e)
        time.sleep(2)
    return None


def wait_for_resolution(slug_prefix, window_ts, timeout, poll_interval, is_running):
    # type: (str, int, int, float, Callable[[], bool]) -> Optional[str]
    """Poll for market resolution after the window ends.

    Args:
        slug_prefix: market slug prefix
        window_ts: window start unix timestamp
        timeout: max seconds to wait for resolution
        poll_interval: seconds between resolution checks
        is_running: callable returning False to abort
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline and is_running():
        try:
            outcome = fetch_resolution(slug_prefix, window_ts)
            if outcome:
                return outcome
        except Exception as e:
            logger.warning("Resolution check error: %s", e)
        time.sleep(poll_interval)

    logger.warning("Resolution timeout for window %d", window_ts)
    return None
