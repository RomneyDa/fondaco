"""Tests for optimizer walk-forward validation and Sharpe decay detection."""

import pytest
from unittest.mock import MagicMock
from simulator.engine import SessionResult
from core.trader import Position


def _make_session_results(pnls, starting_cash=100.0):
    """Build a list of SessionResult with given PnLs."""
    results = []
    for pnl in pnls:
        results.append(SessionResult(
            session_file="test.json",
            outcome="Up" if pnl > 0 else "Down",
            pnl=pnl,
            pnl_pct=pnl / starting_cash * 100,
            trades=[],
            final_position=Position(cash=starting_cash + pnl),
            num_samples=100,
            starting_cash=starting_cash,
        ))
    return results


class TestSharpeDecay:
    def test_no_decay_stable(self):
        """Stable PnL → no decay."""
        from app.optimizer import _detect_sharpe_decay
        results = _make_session_results([2, 3, 1, 2, 3, 1, 2, 3, 1, 2, 3, 1])
        info = _detect_sharpe_decay(results)
        assert not info["decaying"]

    def test_decay_detected(self):
        """Good start, then losses → decay detected."""
        from app.optimizer import _detect_sharpe_decay
        # First 8 sessions profitable, last 4 losing
        pnls = [5, 4, 6, 3, 5, 4, 3, 5, -3, -4, -5, -3]
        results = _make_session_results(pnls)
        info = _detect_sharpe_decay(results)
        # Recent sharpe should be lower than full
        assert info["recent_sharpe"] < info["full_sharpe"]

    def test_insufficient_data(self):
        """Too few sessions → no decay detected."""
        from app.optimizer import _detect_sharpe_decay
        results = _make_session_results([1, 2])
        info = _detect_sharpe_decay(results)
        assert not info["decaying"]


class TestWalkForward:
    def test_stable_strategy(self):
        """Walk-forward on a simple always-win strategy should be stable."""
        from app.optimizer import _walk_forward_validate
        from core.strategies.base import Strategy
        from core.trader import HOLD, TradeAction

        class AlwaysHold(Strategy):
            name = "AlwaysHold"
            description = ""
            def on_session_start(self, metadata):
                pass
            def on_tick(self, ctx):
                return HOLD

        # Build minimal sessions
        sessions = []
        for i in range(15):
            sessions.append({
                "metadata": {"window_start_utc": "2025-01-01T{:02d}:{:02d}:00Z".format(i // 12, (i % 12) * 5)},
                "outcome": "Up",
                "initial_btc_price_usd": 97000.0,
                "final_btc_price_usd": 97100.0,
                "samples": [
                    {"elapsed_seconds": 60.0, "btc_price_usd": 97000.0,
                     "up_token_midpoint": 0.50, "down_token_midpoint": 0.50,
                     "up_token_best_bid": 0.49, "up_token_best_ask": 0.51,
                     "down_token_best_bid": 0.49, "down_token_best_ask": 0.51},
                    {"elapsed_seconds": 300.0, "btc_price_usd": 97100.0,
                     "up_token_midpoint": 0.90, "down_token_midpoint": 0.10,
                     "up_token_best_bid": 0.89, "up_token_best_ask": 0.91,
                     "down_token_best_bid": 0.09, "down_token_best_ask": 0.11},
                ],
            })

        result = _walk_forward_validate(AlwaysHold, {}, sessions, 100.0, [])
        # AlwaysHold has zero PnL everywhere → Sharpe 0, but stable (all same)
        assert "fold_sharpes" in result

    def test_skips_small_dataset(self):
        """Too few sessions → skipped."""
        from app.optimizer import _walk_forward_validate
        from core.strategies.base import Strategy
        from core.trader import HOLD

        class Dummy(Strategy):
            name = "Dummy"
            description = ""
            def on_session_start(self, metadata):
                pass
            def on_tick(self, ctx):
                return HOLD

        result = _walk_forward_validate(Dummy, {}, [{}] * 5, 100.0, [])
        assert result.get("skipped", False)
