"""Tests for gap handling in session cache and data density."""

import pytest
from core.session_cache import SessionStatsCache, SessionStats


def _make_cache_with_gaps():
    """Build a cache with a 2-hour gap in the middle.

    Sessions: T0..T5 (5-min apart), then 2-hour gap, then T6..T10.
    """
    cache = SessionStatsCache()
    # First block: 6 sessions, 5 min apart
    for i in range(6):
        ts = "2025-01-01T00:{:02d}:00Z".format(i * 5)
        cache.add(ts, "Up" if i % 2 == 0 else "Down", 97000.0 + i, 97000.0 + i + 1)
    # 2-hour gap
    # Second block: 5 sessions, 5 min apart starting at 02:30
    for i in range(5):
        ts = "2025-01-01T02:{:02d}:00Z".format(30 + i * 5)
        cache.add(ts, "Up" if i % 2 == 0 else "Down", 97010.0 + i, 97010.0 + i + 1)
    return cache


class TestGapBefore:
    def test_gap_before_contiguous(self):
        """Sessions 5 min apart should report ~300s gap."""
        cache = _make_cache_with_gaps()
        gap = cache.gap_before("2025-01-01T00:05:00Z")
        assert gap is not None
        assert abs(gap - 300.0) < 1.0

    def test_gap_before_after_hole(self):
        """First session after 2-hour gap should report ~7200s+ gap."""
        cache = _make_cache_with_gaps()
        gap = cache.gap_before("2025-01-01T02:30:00Z")
        assert gap is not None
        assert gap > 7000  # ~2 hours

    def test_gap_before_first_session(self):
        """First session has no prior — should return None."""
        cache = _make_cache_with_gaps()
        gap = cache.gap_before("2025-01-01T00:00:00Z")
        assert gap is None

    def test_gap_before_unknown_timestamp(self):
        """Timestamp not in cache should still compute gap to nearest prior."""
        cache = _make_cache_with_gaps()
        gap = cache.gap_before("2025-01-01T00:07:30Z")
        assert gap is not None
        assert gap > 0


class TestDataDensity:
    def test_density_contiguous(self):
        """Contiguous sessions should have density ~1.0."""
        cache = SessionStatsCache()
        for i in range(20):
            ts = "2025-01-01T{:02d}:{:02d}:00Z".format(i // 12, (i % 12) * 5)
            cache.add(ts, "Up", 97000.0, 97001.0)
        density = cache.data_density("2025-01-01T01:40:00Z", n=20, expected_interval_s=300)
        assert density > 0.90

    def test_density_with_gap(self):
        """Sessions with a 2-hour gap should have lower density."""
        cache = _make_cache_with_gaps()
        density = cache.data_density("2025-01-01T02:50:00Z", n=11, expected_interval_s=300)
        assert density < 0.80  # Gap makes it sparse

    def test_density_single_session(self):
        """Single session should return 1.0 (can't compute)."""
        cache = SessionStatsCache()
        cache.add("2025-01-01T00:00:00Z", "Up", 97000.0, 97001.0)
        density = cache.data_density("2025-01-01T00:05:00Z", n=1, expected_interval_s=300)
        assert density == 1.0


class TestPriorContiguous:
    def test_stops_at_gap(self):
        """prior_contiguous should stop at the 2-hour gap."""
        cache = _make_cache_with_gaps()
        # Query from after the gap — should only get the post-gap block
        result = cache.prior_contiguous("2025-01-01T02:50:00Z", n=20, max_gap_s=600)
        # Should get at most the 5 post-gap sessions, not the 6 pre-gap ones
        assert len(result) <= 5
        # All should be from the second block (02:30+)
        for entry in result:
            assert entry.window_start >= "2025-01-01T02:30"

    def test_returns_all_when_contiguous(self):
        """When no gaps, should return up to n sessions."""
        cache = SessionStatsCache()
        for i in range(10):
            ts = "2025-01-01T00:{:02d}:00Z".format(i * 5)
            cache.add(ts, "Up", 97000.0, 97001.0)
        result = cache.prior_contiguous("2025-01-01T00:50:00Z", n=10, max_gap_s=600)
        assert len(result) == 10

    def test_respects_n_limit(self):
        """Even with contiguous data, should not return more than n."""
        cache = SessionStatsCache()
        for i in range(10):
            ts = "2025-01-01T00:{:02d}:00Z".format(i * 5)
            cache.add(ts, "Up", 97000.0, 97001.0)
        result = cache.prior_contiguous("2025-01-01T00:50:00Z", n=3, max_gap_s=600)
        assert len(result) == 3

    def test_oldest_first_ordering(self):
        """Results should be sorted oldest-first."""
        cache = _make_cache_with_gaps()
        result = cache.prior_contiguous("2025-01-01T02:50:00Z", n=20, max_gap_s=600)
        for i in range(len(result) - 1):
            assert result[i].window_start <= result[i + 1].window_start
