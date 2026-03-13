"""Tests for the MarketRunner class."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from bots import UPDOWN_STRATEGIES


def _make_runner(tmp_path, slug_prefix="btc-updown-5m-", market_type="updown"):
    """Create a MarketRunner with mocked paths."""
    from app.market_runner import MarketRunner

    with patch("app.market_runner.market_data_dir", return_value=str(tmp_path / "market")), \
         patch("app.market_runner.market_workers_dir", return_value=str(tmp_path / "workers")), \
         patch("app.market_runner.market_strategies_file", return_value=str(tmp_path / "strategies.json")), \
         patch("app.market_runner.market_socket_path", return_value=str(tmp_path / "ticks.sock")), \
         patch("app.market_runner.collector_pid_path", return_value=str(tmp_path / "collector.pid")), \
         patch("app.market_runner.collector_state_path", return_value=str(tmp_path / "collector_state.json")):
        runner = MarketRunner(
            slug_prefix=slug_prefix,
            market_type=market_type,
        )
    runner._market_dir = str(tmp_path / "market")
    runner._workers_dir = str(tmp_path / "workers")
    runner._strategies_file = str(tmp_path / "strategies.json")
    return runner


class TestMarketRunnerInit:
    def test_slug_parsing(self, tmp_path):
        runner = _make_runner(tmp_path, "btc-updown-5m-")
        assert runner.slug_key == "btc-updown-5m"
        assert runner.slug_prefix == "btc-updown-5m-"

    def test_market_type(self, tmp_path):
        runner = _make_runner(tmp_path, market_type="updown")
        assert runner.market_type == "updown"
        assert runner._valid_strategies == UPDOWN_STRATEGIES

    def test_unknown_market_type_fallback(self, tmp_path):
        runner = _make_runner(tmp_path, market_type="sports")
        # Falls back to ALL_STRATEGIES
        from bots import ALL_STRATEGIES
        assert runner._valid_strategies == ALL_STRATEGIES


class TestEnableTrading:
    def test_unknown_strategy_raises(self, tmp_path):
        runner = _make_runner(tmp_path)
        with pytest.raises(ValueError, match="Unknown strategy"):
            runner.enable_trading("NonexistentStrategy")

    def test_valid_strategy_spawns_worker(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(strategy_name, cash=50.0, dry_run=True)

        assert len(runner._workers) == 1
        assert runner._workers[0].strategy_name == strategy_name
        assert runner._workers[0].cash == 50.0
        assert runner._workers[0].dry_run is True

    def test_replace_existing_worker(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_p1 = MagicMock()
        mock_p1.pid = 100
        mock_p1.poll.return_value = None

        mock_p2 = MagicMock()
        mock_p2.pid = 200
        mock_p2.poll.return_value = None

        with patch.object(runner, "_spawn_worker", side_effect=[(mock_p1, MagicMock()), (mock_p2, MagicMock())]):
            runner.enable_trading(strategy_name, cash=50.0)
            runner.enable_trading(strategy_name, cash=75.0)

        assert len(runner._workers) == 1
        assert runner._workers[0].cash == 75.0

    def test_param_parsing(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 1
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())) as spawn:
            runner.enable_trading(
                strategy_name,
                params={"threshold": "0.5", "count": "10", "mode": "fast"},
                cash=10.0,
            )
            call_args = spawn.call_args
            parsed = call_args[0][1]  # params arg
            assert parsed["threshold"] == 0.5
            assert parsed["count"] == 10
            assert parsed["mode"] == "fast"


class TestDisableTrading:
    def test_disable_specific_strategy(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategies = list(UPDOWN_STRATEGIES.keys())
        if len(strategies) < 2:
            pytest.skip("Need at least 2 strategies")

        mock_p1 = MagicMock()
        mock_p1.pid = 100
        mock_p1.poll.return_value = None
        mock_p2 = MagicMock()
        mock_p2.pid = 200
        mock_p2.poll.return_value = None

        with patch.object(runner, "_spawn_worker", side_effect=[(mock_p1, MagicMock()), (mock_p2, MagicMock())]):
            runner.enable_trading(strategies[0], cash=10.0)
            runner.enable_trading(strategies[1], cash=20.0)

        runner.disable_trading(strategy_name=strategies[0])
        assert len(runner._workers) == 1
        assert runner._workers[0].strategy_name == strategies[1]

    def test_disable_all(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 100
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(strategy_name, cash=10.0)

        runner.disable_trading()
        assert len(runner._workers) == 0

    def test_disable_nonexistent_raises(self, tmp_path):
        runner = _make_runner(tmp_path)
        with pytest.raises(ValueError, match="No active worker"):
            runner.disable_trading(strategy_name="Nonexistent")


class TestSetCash:
    def test_set_cash_single_worker(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 1
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(strategy_name, cash=50.0)

        state_dir = os.path.join(str(tmp_path / "workers"), strategy_name)
        os.makedirs(state_dir, exist_ok=True)
        state_file = os.path.join(state_dir, "state.json")
        with open(state_file, "w") as f:
            json.dump({"cash": 50.0}, f)

        runner.set_cash(75.0)

        with open(state_file) as f:
            data = json.load(f)
        # set_cash writes to pending, worker picks it up at session boundary
        assert data["pending"]["bankroll"] == 75.0

    def test_set_cash_no_workers_raises(self, tmp_path):
        runner = _make_runner(tmp_path)
        with pytest.raises(RuntimeError, match="No active workers"):
            runner.set_cash(50.0)

    def test_set_cash_multiple_workers_requires_strategy(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategies = list(UPDOWN_STRATEGIES.keys())
        if len(strategies) < 2:
            pytest.skip("Need at least 2 strategies")

        mock_p1 = MagicMock()
        mock_p1.pid = 1
        mock_p1.poll.return_value = None
        mock_p2 = MagicMock()
        mock_p2.pid = 2
        mock_p2.poll.return_value = None

        with patch.object(runner, "_spawn_worker", side_effect=[(mock_p1, MagicMock()), (mock_p2, MagicMock())]):
            runner.enable_trading(strategies[0], cash=10.0)
            runner.enable_trading(strategies[1], cash=20.0)

        with pytest.raises(ValueError, match="Multiple workers"):
            runner.set_cash(50.0)


class TestGetStatus:
    def test_basic_status(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._read_collector_pid = lambda: None
        runner._read_collector_state = lambda: {}
        status = runner.get_status()
        assert status["slug_key"] == "btc-updown-5m"
        assert status["market_type"] == "updown"
        # running is based on collector PID (no collector running in tests)
        assert status["running"] is False
        assert status["collector_running"] is False
        assert status["trading_enabled"] is False
        assert status["strategies"] == []

    def test_status_with_worker(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 123
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(strategy_name, cash=50.0, dry_run=True)

        status = runner.get_status()
        assert status["trading_enabled"] is True
        assert len(status["strategies"]) == 1
        assert status["strategies"][0]["name"] == strategy_name
        assert status["strategies"][0]["cash"] == 50.0
        assert status["strategies"][0]["dry_run"] is True
        assert status["strategies"][0]["pid"] == 123
        assert status["strategies"][0]["alive"] is True


class TestStrategiesConfig:
    def test_save_and_restore(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 1
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(
                strategy_name, cash=50.0, dry_run=True, session_cap=5.0,
            )

        # Verify config was saved
        assert os.path.exists(runner._strategies_file)
        with open(runner._strategies_file) as f:
            configs = json.load(f)
        assert len(configs) == 1
        assert configs[0]["name"] == strategy_name
        assert configs[0]["cash"] == 50.0
        assert configs[0]["session_cap"] == 5.0

    def test_restore_strategies(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        os.makedirs(os.path.dirname(runner._strategies_file), exist_ok=True)
        with open(runner._strategies_file, "w") as f:
            json.dump([{
                "name": strategy_name,
                "params": {},
                "cash": 42.0,
                "dry_run": False,
                "per_session": False,
                "session_cap": None,
            }], f)

        mock_process = MagicMock()
        mock_process.pid = 1
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.restore_strategies()

        assert len(runner._workers) == 1
        assert runner._workers[0].strategy_name == strategy_name
        assert runner._workers[0].cash == 42.0


class TestRequestStop:
    def test_request_stop_kills_workers(self, tmp_path):
        runner = _make_runner(tmp_path)
        strategy_name = list(UPDOWN_STRATEGIES.keys())[0]

        mock_process = MagicMock()
        mock_process.pid = 100
        mock_process.poll.return_value = None

        with patch.object(runner, "_spawn_worker", return_value=(mock_process, MagicMock())):
            runner.enable_trading(strategy_name, cash=10.0)

        assert len(runner._workers) == 1
        runner.request_stop()
        assert len(runner._workers) == 0
