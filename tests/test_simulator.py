"""Tests for the Simulator engine: run_session, run_backtest, load_sessions."""

import json
import os

import pytest
from bots.updown.archived.always_buy_up import AlwaysBuyUp
from bots.updown.archived.always_buy_down import AlwaysBuyDown
from core.strategies.base import Strategy
from core.trader import HOLD, TradeAction
from simulator.engine import Simulator, load_sessions


class _FakBuyUp(Strategy):
    """Buy Up via FAK (no price) — for testing execution modes."""
    name = "FakBuyUp"
    description = ""

    def __init__(self):
        self._bought = False

    def on_session_start(self, metadata):
        self._bought = False

    def on_tick(self, ctx):
        if not self._bought:
            self._bought = True
            return TradeAction(side="buy_up", amount=ctx.position.cash)
        return HOLD


def _make_session(outcome="Up", num_samples=5, initial_btc=97000.0, final_btc=97100.0):
    """Build a minimal session dict for testing."""
    samples = []
    for i in range(num_samples):
        elapsed = (i + 1) * 60.0  # 60s apart
        samples.append({
            "timestamp_utc": "2025-01-01T00:0{}:00+00:00".format(i + 1),
            "elapsed_seconds": elapsed,
            "btc_price_usd": initial_btc + i * 20,
            "up_token_midpoint": 0.50,
            "down_token_midpoint": 0.50,
            "up_token_best_bid": 0.49,
            "up_token_best_ask": 0.51,
            "down_token_best_bid": 0.49,
            "down_token_best_ask": 0.51,
        })
    return {
        "metadata": {
            "event_id": "evt-1",
            "event_slug": "btc-updown-5m-1700000100",
            "event_title": "BTC Up/Down",
            "condition_id": "cond-1",
            "up_token_id": "up-123",
            "down_token_id": "down-456",
            "window_start_utc": "2025-01-01T00:00:00Z",
            "window_end_utc": "2025-01-01T00:05:00Z",
            "resolution_source": "chainlink",
        },
        "initial_btc_price_usd": initial_btc,
        "final_btc_price_usd": final_btc,
        "outcome": outcome,
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# run_session
# ---------------------------------------------------------------------------


class TestRunSession:
    def test_always_buy_up_wins_on_up(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        session = _make_session(outcome="Up")
        strategy = AlwaysBuyUp(bet_fraction=1.0)
        result = sim.run_session(strategy, session)

        assert result.outcome == "Up"
        assert result.pnl > 0  # bought at 0.50, resolved at 1.0

    def test_always_buy_up_loses_on_down(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        session = _make_session(outcome="Down")
        strategy = AlwaysBuyUp(bet_fraction=1.0)
        result = sim.run_session(strategy, session)

        assert result.outcome == "Down"
        assert result.pnl < 0  # bought up at 0.50, down wins

    def test_always_buy_down_wins_on_down(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        session = _make_session(outcome="Down")
        strategy = AlwaysBuyDown(bet_fraction=1.0)
        result = sim.run_session(strategy, session)

        assert result.pnl > 0

    def test_pnl_pct_calculation(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        session = _make_session(outcome="Up")
        strategy = AlwaysBuyUp(bet_fraction=1.0)
        result = sim.run_session(strategy, session)

        expected_pct = (result.pnl / 100.0) * 100.0
        assert result.pnl_pct == pytest.approx(expected_pct)

    def test_num_samples(self):
        sim = Simulator()
        session = _make_session(num_samples=3)
        strategy = AlwaysBuyUp()
        result = sim.run_session(strategy, session)
        assert result.num_samples == 3

    def test_slippage_reduces_profit(self):
        """Slippage should reduce profit compared to zero-slippage (FAK path)."""
        session = _make_session(outcome="Up")
        sim_no_slip = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        sim_with_slip = Simulator(starting_cash=100.0, execution_mode="midpoint_slippage", slippage=0.02)

        # Use FAK-only strategy (no price) so execution mode is respected
        strategy1 = _FakBuyUp()
        strategy2 = _FakBuyUp()

        r1 = sim_no_slip.run_session(strategy1, session)
        r2 = sim_with_slip.run_session(strategy2, session)

        assert r1.pnl > r2.pnl  # slippage eats into profit


# ---------------------------------------------------------------------------
# run_backtest
# ---------------------------------------------------------------------------


class TestRunBacktest:
    def test_basic_backtest(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        sessions = [_make_session(outcome="Up"), _make_session(outcome="Down")]
        strategy = AlwaysBuyUp(bet_fraction=1.0)
        result = sim.run_backtest(strategy, sessions)

        assert result.num_sessions == 2
        assert result.strategy_name == "AlwaysBuyUp"
        assert len(result.sessions) == 2

    def test_win_rate(self):
        sim = Simulator(starting_cash=100.0, execution_mode="midpoint", slippage=0.0)
        sessions = [
            _make_session(outcome="Up"),
            _make_session(outcome="Up"),
            _make_session(outcome="Down"),
        ]
        strategy = AlwaysBuyUp(bet_fraction=1.0)
        result = sim.run_backtest(strategy, sessions)
        # 2 wins out of 3 sessions
        assert result.win_rate == pytest.approx(2.0 / 3.0)

    def test_empty_sessions(self):
        sim = Simulator()
        strategy = AlwaysBuyUp()
        result = sim.run_backtest(strategy, [])
        assert result.num_sessions == 0
        assert result.total_pnl == 0.0
        assert result.win_rate == 0.0
        assert result.sharpe == 0.0

    def test_single_session_sharpe_zero(self):
        sim = Simulator()
        sessions = [_make_session(outcome="Up")]
        strategy = AlwaysBuyUp()
        result = sim.run_backtest(strategy, sessions)
        assert result.sharpe == 0.0  # can't compute std with 1 sample


# ---------------------------------------------------------------------------
# load_sessions
# ---------------------------------------------------------------------------


class TestLoadSessions:
    def test_loads_resolved_sessions(self, tmp_path):
        session = _make_session(outcome="Up")
        with open(tmp_path / "s1.json", "w") as f:
            json.dump(session, f)
        sessions = load_sessions(str(tmp_path))
        assert len(sessions) == 1
        assert sessions[0]["outcome"] == "Up"
        assert "_source_file" in sessions[0]

    def test_skips_unresolved(self, tmp_path):
        resolved = _make_session(outcome="Up")
        unresolved = _make_session(outcome=None)  # type: ignore[arg-type]
        with open(tmp_path / "s1.json", "w") as f:
            json.dump(resolved, f)
        with open(tmp_path / "s2.json", "w") as f:
            json.dump(unresolved, f)
        sessions = load_sessions(str(tmp_path))
        assert len(sessions) == 1

    def test_empty_directory(self, tmp_path):
        sessions = load_sessions(str(tmp_path))
        assert sessions == []

    def test_recursive_loading(self, tmp_path):
        subdir = tmp_path / "2025" / "01"
        subdir.mkdir(parents=True)
        session = _make_session(outcome="Down")
        with open(subdir / "s1.json", "w") as f:
            json.dump(session, f)
        sessions = load_sessions(str(tmp_path))
        assert len(sessions) == 1
        assert sessions[0]["outcome"] == "Down"
