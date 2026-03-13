"""Tests for CLI argument parsing and command dispatch."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _parse_args(argv):
    """Parse CLI args without executing."""
    from app.__main__ import main
    import argparse
    # Import the module to get the parser
    # We test by calling main() with mocked handlers
    pass


class TestParseParams:
    def test_simple_params(self):
        from app.__main__ import _parse_params
        result = _parse_params("foo=1,bar=2.5,baz=hello")
        assert result == {"foo": 1, "bar": 2.5, "baz": "hello"}

    def test_empty_params(self):
        from app.__main__ import _parse_params
        assert _parse_params("") == {}
        assert _parse_params(None) == {}

    def test_single_param(self):
        from app.__main__ import _parse_params
        result = _parse_params("threshold=0.7")
        assert result == {"threshold": 0.7}


class TestPidAlive:
    def test_current_pid(self):
        from app.__main__ import _pid_alive
        assert _pid_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        from app.__main__ import _pid_alive
        # Use a very large PID unlikely to exist
        assert _pid_alive(999999999) is False


class TestReadWriteState:
    def test_write_and_read(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _write_state, _read_state
            _write_state(os.getpid(), 8420)
            state = _read_state()
            assert state is not None
            assert state["pid"] == os.getpid()
            assert state["port"] == 8420

    def test_read_nonexistent(self, tmp_path):
        state_file = str(tmp_path / "nonexistent.json")
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _read_state
            assert _read_state() is None

    def test_read_stale_pid(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with open(state_file, "w") as f:
            json.dump({"pid": 999999999, "port": 8420}, f)
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _read_state
            # Stale PID returns None but file is preserved for markets list
            assert _read_state() is None
            assert os.path.exists(state_file)

    def test_remove_state(self, tmp_path):
        state_file = str(tmp_path / "app.json")
        with open(state_file, "w") as f:
            json.dump({"pid": 1, "port": 8420, "markets": []}, f)
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import _remove_state
            _remove_state()
            # File preserved with pid/port cleared
            assert os.path.exists(state_file)
            with open(state_file) as f:
                data = json.load(f)
            assert "pid" not in data
            assert "port" not in data


class TestCmdStatus:
    def test_no_running_app(self, tmp_path, capsys):
        state_file = str(tmp_path / "app.json")
        with patch("app.__main__.APP_STATE_FILE", state_file):
            from app.__main__ import cmd_status
            args = MagicMock()
            cmd_status(args)
            output = capsys.readouterr().out
            assert "not running" in output

    def test_running_app_shows_markets(self, tmp_path, capsys):
        state_file = str(tmp_path / "app.json")
        with open(state_file, "w") as f:
            json.dump({"pid": os.getpid(), "port": 8420}, f)

        mock_response = {
            "running": True,
            "markets": [
                {
                    "slug_prefix": "btc-updown-5m-",
                    "slug_key": "btc-updown-5m",
                    "session_count": 5,
                    "tick_clients": 2,
                    "strategies": [],
                },
            ],
        }

        with patch("app.__main__.APP_STATE_FILE", state_file), \
             patch("app.__main__._http_request", return_value=mock_response):
            from app.__main__ import cmd_status
            args = MagicMock()
            cmd_status(args)
            output = capsys.readouterr().out
            assert "btc-updown-5m" in output
            assert "sessions=5" in output
