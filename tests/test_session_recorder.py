"""Tests for core/session_recorder.py with custom paths."""

import json
import os

from core.session_recorder import SessionRecorder
from core.models import SessionData, MarketMetadata


def _make_session(window_start="2026-03-05T12:00:00Z"):
    metadata = MarketMetadata(
        event_id="e1",
        event_slug="test-slug",
        event_title="Test",
        condition_id="c1",
        up_token_id="up1",
        down_token_id="down1",
        window_start_utc=window_start,
        window_end_utc="2026-03-05T12:05:00Z",
        resolution_source="chainlink",
    )
    return SessionData(
        metadata=metadata,
        initial_btc_price_usd=100000.0,
        final_btc_price_usd=100050.0,
        outcome="Up",
        outcome_prices=["1", "0"],
    )


class TestCustomPaths:
    def test_custom_sessions_dir(self, tmp_path):
        sessions_dir = str(tmp_path / "custom_sessions")
        live_dir = str(tmp_path / "custom_live")
        log_file = str(tmp_path / "custom.log")
        rec = SessionRecorder(
            "test-", sessions_dir=sessions_dir,
            live_dir=live_dir, log_file=log_file,
        )
        assert rec.market_dir == sessions_dir
        assert os.path.isdir(sessions_dir)
        assert os.path.isdir(live_dir)

    def test_save_and_exists(self, tmp_path):
        sessions_dir = str(tmp_path / "sessions")
        live_dir = str(tmp_path / "live")
        log_file = str(tmp_path / "log.jsonl")
        rec = SessionRecorder(
            "test-", sessions_dir=sessions_dir,
            live_dir=live_dir, log_file=log_file,
        )
        session = _make_session()
        filepath = rec.save_session(session)
        assert os.path.exists(filepath)
        assert rec.session_exists("2026-03-05T12:00:00Z")

    def test_live_session_write_and_clear(self, tmp_path):
        sessions_dir = str(tmp_path / "sessions")
        live_dir = str(tmp_path / "live")
        log_file = str(tmp_path / "log.jsonl")
        rec = SessionRecorder(
            "test-", sessions_dir=sessions_dir,
            live_dir=live_dir, log_file=log_file,
        )
        session = _make_session()
        rec.write_live_session(session)
        assert os.path.exists(rec.live_filepath)
        with open(rec.live_filepath) as f:
            data = json.load(f)
        assert data["outcome"] == "Up"

        rec.clear_live_session()
        assert not os.path.exists(rec.live_filepath)

    def test_append_log(self, tmp_path):
        sessions_dir = str(tmp_path / "sessions")
        live_dir = str(tmp_path / "live")
        log_file = str(tmp_path / "log.jsonl")
        rec = SessionRecorder(
            "test-", sessions_dir=sessions_dir,
            live_dir=live_dir, log_file=log_file,
        )
        rec.append_log("test message", foo="bar")
        assert os.path.exists(log_file)
        with open(log_file) as f:
            entry = json.loads(f.readline())
        assert entry["message"] == "test message"
        assert entry["foo"] == "bar"

    def test_count_completed_sessions(self, tmp_path):
        sessions_dir = str(tmp_path / "sessions")
        live_dir = str(tmp_path / "live")
        log_file = str(tmp_path / "log.jsonl")
        rec = SessionRecorder(
            "test-", sessions_dir=sessions_dir,
            live_dir=live_dir, log_file=log_file,
        )
        assert rec.count_completed_sessions() == 0
        rec.save_session(_make_session("2026-03-05T12:00:00Z"))
        rec.save_session(_make_session("2026-03-05T12:05:00Z"))
        assert rec.count_completed_sessions() == 2

    def test_default_paths_use_market_helpers(self):
        """When no custom paths given, paths should be under data/markets/."""
        rec = SessionRecorder("btc-updown-5m-")
        assert "markets" in rec.market_dir
        assert "btc-updown-5m" in rec.market_dir
