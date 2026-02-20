"""Tests for app helper functions: position persistence, state file, auth cache."""

import json
import os
from unittest.mock import patch

import pytest
from core.trader import Position


# ---------------------------------------------------------------------------
# _save_position / _load_position
# ---------------------------------------------------------------------------


class TestPositionPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        pos_file = str(tmp_path / "bot_position.json")
        with patch("app.runner.POSITION_FILE", pos_file):
            from app.runner import _save_position, _load_position
            pos = Position(cash=42.5, up_tokens=10.0, down_tokens=5.0)
            _save_position(pos)

            loaded = _load_position(100.0)
            assert loaded.cash == 42.5
            assert loaded.up_tokens == 10.0
            assert loaded.down_tokens == 5.0

    def test_load_missing_file_returns_fresh(self, tmp_path):
        pos_file = str(tmp_path / "nonexistent.json")
        with patch("app.runner.POSITION_FILE", pos_file):
            from app.runner import _load_position
            loaded = _load_position(100.0)
            assert loaded.cash == 100.0
            assert loaded.up_tokens == 0.0

    def test_load_corrupted_file_returns_fresh(self, tmp_path):
        pos_file = str(tmp_path / "corrupt.json")
        with open(pos_file, "w") as f:
            f.write("not json!!!")
        with patch("app.runner.POSITION_FILE", pos_file):
            from app.runner import _load_position
            loaded = _load_position(50.0)
            assert loaded.cash == 50.0

    def test_save_is_atomic(self, tmp_path):
        """Verify atomic write: .tmp file should not persist after save."""
        pos_file = str(tmp_path / "bot_position.json")
        with patch("app.runner.POSITION_FILE", pos_file):
            from app.runner import _save_position
            _save_position(Position(cash=99.0))
            assert os.path.exists(pos_file)
            assert not os.path.exists(pos_file + ".tmp")

    def test_save_overwrites(self, tmp_path):
        pos_file = str(tmp_path / "bot_position.json")
        with patch("app.runner.POSITION_FILE", pos_file):
            from app.runner import _save_position, _load_position
            _save_position(Position(cash=10.0))
            _save_position(Position(cash=20.0))
            loaded = _load_position(100.0)
            assert loaded.cash == 20.0


# ---------------------------------------------------------------------------
# _save_balance_cache / _load_balance_cache
# ---------------------------------------------------------------------------


class TestBalanceCache:
    def test_save_and_load(self, tmp_path):
        cache_file = str(tmp_path / "balance.json")
        with patch("app.runner.BALANCE_CACHE_FILE", cache_file):
            from app.runner import _save_balance_cache, _load_balance_cache
            _save_balance_cache(95.5, "btc-updown-5m-", 3)
            data = _load_balance_cache()
            assert data is not None
            assert data["cash"] == 95.5
            assert data["slug_prefix"] == "btc-updown-5m-"
            assert data["session_count"] == 3

    def test_load_missing_returns_none(self, tmp_path):
        cache_file = str(tmp_path / "missing.json")
        with patch("app.runner.BALANCE_CACHE_FILE", cache_file):
            from app.runner import _load_balance_cache
            assert _load_balance_cache() is None


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------


class TestStateFile:
    def test_write_and_read_state(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _write_state, _read_state
            _write_state(os.getpid(), 8420)
            state = _read_state()
            assert state is not None
            assert state["pid"] == os.getpid()
            assert state["port"] == 8420

    def test_read_missing_state(self, tmp_path):
        state_file = str(tmp_path / "nonexistent.json")
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _read_state
            assert _read_state() is None

    def test_stale_state_cleaned_up(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with open(state_file, "w") as f:
            json.dump({"pid": 999999999, "port": 8420}, f)
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _read_state
            result = _read_state()
            assert result is None
            assert not os.path.exists(state_file)

    def test_remove_state(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with open(state_file, "w") as f:
            json.dump({"pid": 12345, "port": 8420}, f)
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _remove_state
            _remove_state()
            assert not os.path.exists(state_file)


# ---------------------------------------------------------------------------
# Auth cache
# ---------------------------------------------------------------------------


class TestAuthCache:
    def test_save_and_load(self, tmp_path):
        cache_file = str(tmp_path / "auth.json")
        with patch("app.auth.AUTH_CACHE_FILE", cache_file):
            from app.auth import _save_auth_cache, _load_auth_cache
            data = {
                "eoa_address": "0xABC",
                "safe_address": "0xDEF",
                "api_key": "key",
                "api_secret": "secret",
                "api_passphrase": "pass",
            }
            _save_auth_cache(data)
            loaded = _load_auth_cache("0xABC")
            assert loaded is not None
            assert loaded["safe_address"] == "0xDEF"

    def test_mismatched_eoa_returns_none(self, tmp_path):
        cache_file = str(tmp_path / "auth.json")
        with patch("app.auth.AUTH_CACHE_FILE", cache_file):
            from app.auth import _save_auth_cache, _load_auth_cache
            _save_auth_cache({"eoa_address": "0xABC"})
            loaded = _load_auth_cache("0xDIFFERENT")
            assert loaded is None

    def test_clear_auth_cache(self, tmp_path):
        cache_file = str(tmp_path / "auth.json")
        with open(cache_file, "w") as f:
            f.write("{}")
        with patch("app.auth.AUTH_CACHE_FILE", cache_file):
            from app.auth import _clear_auth_cache
            _clear_auth_cache()
            assert not os.path.exists(cache_file)

    def test_clear_missing_file_is_noop(self, tmp_path):
        cache_file = str(tmp_path / "missing.json")
        with patch("app.auth.AUTH_CACHE_FILE", cache_file):
            from app.auth import _clear_auth_cache
            _clear_auth_cache()  # should not raise


# ---------------------------------------------------------------------------
# Runner._is_auth_error
# ---------------------------------------------------------------------------


class TestIsAuthError:
    def test_detects_unauthorized(self):
        from app.runner import _AUTH_ERROR_STRINGS
        def _is_auth_error(error):
            msg = str(error)
            return any(s in msg for s in _AUTH_ERROR_STRINGS)

        assert _is_auth_error(Exception("Unauthorized"))
        assert _is_auth_error(Exception("Something Invalid api key something"))
        assert _is_auth_error(Exception("invalid signature from server"))

    def test_non_auth_error(self):
        from app.runner import _AUTH_ERROR_STRINGS
        def _is_auth_error(error):
            msg = str(error)
            return any(s in msg for s in _AUTH_ERROR_STRINGS)

        assert not _is_auth_error(Exception("Connection timeout"))
        assert not _is_auth_error(Exception("Rate limited"))
