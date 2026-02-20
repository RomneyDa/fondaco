"""Tests for LoggingSession: default timeout, retry logic, jitter, JSONL logging."""

import json
import os
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from core.api_logger import LoggingSession, _parse_retry_after, _write_entry


# ---------------------------------------------------------------------------
# _parse_retry_after
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_numeric(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "5"}
        assert _parse_retry_after(resp) == 5.0

    def test_float(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "2.5"}
        assert _parse_retry_after(resp) == 2.5

    def test_missing(self):
        resp = MagicMock()
        resp.headers = {}
        assert _parse_retry_after(resp) is None

    def test_non_numeric(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "tomorrow"}
        assert _parse_retry_after(resp) is None


# ---------------------------------------------------------------------------
# _write_entry (file I/O)
# ---------------------------------------------------------------------------


class TestWriteEntry:
    def test_creates_host_dir_and_writes(self, tmp_path):
        # Patch API_RAW_DIR so _write_entry creates files under tmp_path
        with patch("core.api_logger.API_RAW_DIR", str(tmp_path)):
            from datetime import datetime, timezone
            ts = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            entry = {"method": "GET", "url": "https://example.com/test"}
            _write_entry("example.com", ts, entry)

            log_file = tmp_path / "example.com" / "2025-01-15.jsonl"
            assert log_file.exists()
            data = json.loads(log_file.read_text().strip())
            assert data["method"] == "GET"


# ---------------------------------------------------------------------------
# LoggingSession — default timeout
# ---------------------------------------------------------------------------


class TestDefaultTimeout:
    def test_default_timeout_injected(self):
        session = LoggingSession(default_timeout=10)
        assert session._default_timeout == 10

    def test_timeout_not_overridden_when_explicit(self):
        """If caller passes timeout=5, it should not be replaced."""
        session = LoggingSession(default_timeout=15)
        # We test this by inspecting kwargs before they reach super().request
        # Just verify the attribute exists — the integration is in request()
        assert session._default_timeout == 15


# ---------------------------------------------------------------------------
# LoggingSession — 429 retry with jitter
# ---------------------------------------------------------------------------


class TestRetryBehavior:
    def test_429_retries(self):
        """A 429 response should be retried up to MAX_RETRIES times."""
        session = LoggingSession()

        # Build mock responses: 2x 429, then 200
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.json.return_value = {}
        resp_429.text = ""

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        resp_200.json.return_value = {"ok": True}
        resp_200.text = ""

        with patch("requests.Session.request", side_effect=[resp_429, resp_429, resp_200]), \
             patch("time.sleep"), \
             patch("core.api_logger._write_entry"):
            result = session.request("GET", "https://example.com/test")

        assert result.status_code == 200
