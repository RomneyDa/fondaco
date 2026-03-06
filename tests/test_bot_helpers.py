"""Tests for app helper functions: position persistence, state file, auth cache."""

import json
import os
from unittest.mock import patch

import pytest
from core.trader import Position


# ---------------------------------------------------------------------------
# _save_position (per-strategy position persistence)
# ---------------------------------------------------------------------------


class TestPositionPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        pos_dir = str(tmp_path / "bot_positions")
        from app.bot_worker import _save_position
        pos = Position(cash=42.5, up_tokens=10.0, down_tokens=5.0)
        _save_position(pos_dir, "TestStrategy", pos)

        filepath = os.path.join(pos_dir, "TestStrategy.json")
        assert os.path.exists(filepath)
        with open(filepath) as f:
            data = json.load(f)
        loaded = Position.from_dict(data)
        assert loaded.cash == 42.5
        assert loaded.up_tokens == 10.0
        assert loaded.down_tokens == 5.0

    def test_save_is_atomic(self, tmp_path):
        """Verify atomic write: .tmp file should not persist after save."""
        pos_dir = str(tmp_path / "bot_positions")
        from app.bot_worker import _save_position
        _save_position(pos_dir, "TestStrategy", Position(cash=99.0))
        filepath = os.path.join(pos_dir, "TestStrategy.json")
        assert os.path.exists(filepath)
        assert not os.path.exists(filepath + ".tmp")

    def test_save_overwrites(self, tmp_path):
        pos_dir = str(tmp_path / "bot_positions")
        from app.bot_worker import _save_position
        _save_position(pos_dir, "TestStrategy", Position(cash=10.0))
        _save_position(pos_dir, "TestStrategy", Position(cash=20.0))
        filepath = os.path.join(pos_dir, "TestStrategy.json")
        with open(filepath) as f:
            data = json.load(f)
        assert data["cash"] == 20.0

    def test_multiple_strategies(self, tmp_path):
        """Different strategy names write to different files."""
        pos_dir = str(tmp_path / "bot_positions")
        from app.bot_worker import _save_position
        _save_position(pos_dir, "StratA", Position(cash=100.0))
        _save_position(pos_dir, "StratB", Position(cash=200.0))
        assert os.path.exists(os.path.join(pos_dir, "StratA.json"))
        assert os.path.exists(os.path.join(pos_dir, "StratB.json"))
        with open(os.path.join(pos_dir, "StratA.json")) as f:
            assert json.load(f)["cash"] == 100.0
        with open(os.path.join(pos_dir, "StratB.json")) as f:
            assert json.load(f)["cash"] == 200.0


# ---------------------------------------------------------------------------
# _save_balance_cache / _load_balance_cache
# ---------------------------------------------------------------------------


class TestBalanceCache:
    def test_save_and_load(self, tmp_path):
        cache_file = str(tmp_path / "balance.json")
        with patch("app.market_runner.BALANCE_CACHE_FILE", cache_file):
            from app.market_runner import _save_balance_cache, _load_balance_cache
            _save_balance_cache(95.5, "btc-updown-5m-", 3)
            data = _load_balance_cache()
            assert data is not None
            assert data["cash"] == 95.5
            assert data["slug_prefix"] == "btc-updown-5m-"
            assert data["session_count"] == 3

    def test_load_missing_returns_none(self, tmp_path):
        cache_file = str(tmp_path / "missing.json")
        with patch("app.market_runner.BALANCE_CACHE_FILE", cache_file):
            from app.market_runner import _load_balance_cache
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
