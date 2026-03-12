"""Tests for Strategy base class and Param descriptor."""

import pytest
from core.trader import HOLD, Position, TradeAction
from core.strategies.base import Param, Strategy
from tests.conftest import make_tick_ctx


# ---------------------------------------------------------------------------
# Concrete stub for testing base class helpers
# ---------------------------------------------------------------------------


class StubStrategy(Strategy):
    """Minimal concrete strategy for testing base helpers."""

    name = "Stub"

    def on_session_start(self, metadata):
        pass

    def on_tick(self, ctx):
        return HOLD


class ParamStrategy(Strategy):
    """Strategy with declared Param descriptors."""

    name = "ParamStub"
    alpha = Param(default=0.5, min=0.0, max=1.0)
    beta = Param(default=10, min=5, max=15)
    fixed = Param(default=100, tunable=False)

    def on_session_start(self, metadata):
        pass

    def on_tick(self, ctx):
        return HOLD


class ChildParamStrategy(ParamStrategy):
    """Inherits ParamStrategy and adds a param."""

    name = "ChildParamStub"
    gamma = Param(default=0.1, min=0.0, max=0.5)


# ---------------------------------------------------------------------------
# Param.grid
# ---------------------------------------------------------------------------


class TestParamGrid:
    def test_float_range_n3(self):
        p = Param(default=0.5, min=0.0, max=1.0)
        grid = p.grid(n=3)
        assert grid == [0.0, 0.5, 1.0]

    def test_float_range_n2(self):
        p = Param(default=0.5, min=0.0, max=1.0)
        grid = p.grid(n=2)
        # n=2 produces [0.0, 1.0], then default 0.5 replaces nearest (0.0)
        assert grid == [0.5, 1.0]

    def test_default_included_when_not_on_grid(self):
        p = Param(default=0.3, min=0.0, max=1.0)
        grid = p.grid(n=3)
        assert 0.3 in grid
        assert len(grid) == 3

    def test_int_range_small(self):
        p = Param(default=2, min=1, max=4)
        grid = p.grid(n=3)
        # 4 ints > n=3, so picks 3 evenly spaced: [1, 3, 4]
        # then default=2 replaces nearest (1) → [2, 3, 4]
        assert 2 in grid
        assert 4 in grid
        assert len(grid) == 3

    def test_int_range_all_fit(self):
        p = Param(default=2, min=1, max=3)
        grid = p.grid(n=5)
        assert grid == [1, 2, 3]

    def test_nullable_prepends_none(self):
        p = Param(default=0.5, min=0.0, max=1.0, nullable=True)
        grid = p.grid(n=3)
        assert grid[0] is None
        assert len(grid) == 4  # None + 3 numeric

    def test_no_range_returns_default(self):
        p = Param(default=42)
        grid = p.grid(n=3)
        assert grid == [42]

    def test_min_equals_max(self):
        p = Param(default=5.0, min=5.0, max=5.0)
        grid = p.grid(n=3)
        assert grid == [5.0]

    def test_nullable_no_range(self):
        p = Param(default=0.5, nullable=True)
        grid = p.grid(n=3)
        assert grid == [None, 0.5]

    def test_default_none_no_range(self):
        p = Param(default=None)
        grid = p.grid(n=3)
        assert grid == []


# ---------------------------------------------------------------------------
# Param.__repr__
# ---------------------------------------------------------------------------


class TestParamRepr:
    def test_basic(self):
        p = Param(default=0.5, min=0.0, max=1.0)
        assert "default=0.5" in repr(p)
        assert "min=0.0" in repr(p)
        assert "max=1.0" in repr(p)

    def test_nullable(self):
        p = Param(default=1, nullable=True)
        assert "nullable=True" in repr(p)

    def test_not_tunable(self):
        p = Param(default=1, tunable=False)
        assert "tunable=False" in repr(p)

    def test_tunable_not_shown(self):
        p = Param(default=1, tunable=True)
        assert "tunable" not in repr(p)


# ---------------------------------------------------------------------------
# Strategy.param_defs / defaults
# ---------------------------------------------------------------------------


class TestParamDefs:
    def test_picks_up_params(self):
        defs = ParamStrategy.param_defs()
        assert "alpha" in defs
        assert "beta" in defs
        assert "fixed" in defs

    def test_no_non_param_attrs(self):
        defs = ParamStrategy.param_defs()
        assert "name" not in defs

    def test_inheritance(self):
        defs = ChildParamStrategy.param_defs()
        assert "alpha" in defs  # from parent
        assert "gamma" in defs  # from child

    def test_defaults(self):
        d = ParamStrategy.defaults()
        assert d["alpha"] == 0.5
        assert d["beta"] == 10
        assert d["fixed"] == 100

    def test_stub_no_params(self):
        assert StubStrategy.param_defs() == {}
        assert StubStrategy.defaults() == {}


# ---------------------------------------------------------------------------
# Strategy.param_grid
# ---------------------------------------------------------------------------


class TestStrategyParamGrid:
    def test_excludes_non_tunable(self):
        s = ParamStrategy()
        grid = s.param_grid()
        assert "fixed" not in grid

    def test_includes_tunable(self):
        s = ParamStrategy()
        grid = s.param_grid()
        assert "alpha" in grid
        assert "beta" in grid

    def test_no_params_returns_empty(self):
        s = StubStrategy()
        assert s.param_grid() == {}

    def test_max_variants_scales_down(self):
        """With a very low max_variants, grid should scale to n=2."""

        class ManyParams(Strategy):
            name = "Many"
            a = Param(default=0.5, min=0.0, max=1.0)
            b = Param(default=0.5, min=0.0, max=1.0)
            c = Param(default=0.5, min=0.0, max=1.0)
            d = Param(default=0.5, min=0.0, max=1.0)
            e = Param(default=0.5, min=0.0, max=1.0)
            f = Param(default=0.5, min=0.0, max=1.0)
            g = Param(default=0.5, min=0.0, max=1.0)
            h = Param(default=0.5, min=0.0, max=1.0)

            def on_session_start(self, m):
                pass

            def on_tick(self, ctx):
                return HOLD

        s = ManyParams()
        # 8 params × 3 pts = 6561 variants > 5000, should scale to n=2
        grid = s.param_grid(max_variants=5000)
        for key, values in grid.items():
            assert len(values) == 2


# ---------------------------------------------------------------------------
# _held_side / _has_position
# ---------------------------------------------------------------------------


class TestHeldSide:
    def test_up_tokens(self):
        s = StubStrategy()
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0))
        assert s._held_side(ctx) == "up"
        assert s._has_position(ctx) is True

    def test_down_tokens(self):
        s = StubStrategy()
        ctx = make_tick_ctx(position=Position(cash=0, down_tokens=10.0))
        assert s._held_side(ctx) == "down"
        assert s._has_position(ctx) is True

    def test_both_tokens_returns_up(self):
        s = StubStrategy()
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=5.0, down_tokens=5.0))
        assert s._held_side(ctx) == "up"

    def test_no_tokens(self):
        s = StubStrategy()
        ctx = make_tick_ctx(position=Position(cash=100.0))
        assert s._held_side(ctx) is None
        assert s._has_position(ctx) is False


# ---------------------------------------------------------------------------
# _check_timing_guards
# ---------------------------------------------------------------------------


class TestTimingGuards:
    def test_too_early(self):
        s = StubStrategy()
        ctx = make_tick_ctx(elapsed_fraction=0.1)
        assert s._check_timing_guards(ctx, min_elapsed=0.2, no_new_after=0.95) is True

    def test_too_late(self):
        s = StubStrategy()
        ctx = make_tick_ctx(elapsed_fraction=0.96)
        assert s._check_timing_guards(ctx, min_elapsed=0.2, no_new_after=0.95) is True

    def test_in_window(self):
        s = StubStrategy()
        ctx = make_tick_ctx(elapsed_fraction=0.5)
        assert s._check_timing_guards(ctx, min_elapsed=0.2, no_new_after=0.95) is False

    def test_exactly_at_min(self):
        s = StubStrategy()
        ctx = make_tick_ctx(elapsed_fraction=0.2)
        assert s._check_timing_guards(ctx, min_elapsed=0.2, no_new_after=0.95) is False

    def test_exactly_at_no_new_after(self):
        s = StubStrategy()
        ctx = make_tick_ctx(elapsed_fraction=0.95)
        assert s._check_timing_guards(ctx, min_elapsed=0.2, no_new_after=0.95) is True


# ---------------------------------------------------------------------------
# _check_expired_entry
# ---------------------------------------------------------------------------


class TestCheckExpiredEntry:
    def test_resets_when_no_position(self):
        s = StubStrategy()
        s._entry_midpoint = 0.55
        ctx = make_tick_ctx(position=Position(cash=100.0))
        s._check_expired_entry(ctx)
        assert s._entry_midpoint == 0.0

    def test_no_reset_when_holding(self):
        s = StubStrategy()
        s._entry_midpoint = 0.55
        ctx = make_tick_ctx(position=Position(cash=50.0, up_tokens=10.0))
        s._check_expired_entry(ctx)
        assert s._entry_midpoint == 0.55

    def test_no_attr_is_noop(self):
        s = StubStrategy()
        ctx = make_tick_ctx(position=Position(cash=100.0))
        s._check_expired_entry(ctx)  # no _entry_midpoint attr — should not raise

    def test_zero_entry_midpoint_is_noop(self):
        s = StubStrategy()
        s._entry_midpoint = 0.0
        ctx = make_tick_ctx(position=Position(cash=100.0))
        s._check_expired_entry(ctx)
        assert s._entry_midpoint == 0.0


# ---------------------------------------------------------------------------
# _check_exit
# ---------------------------------------------------------------------------


class TestCheckExit:
    def _make_strategy(self, entry_mid=0.50, last_tick=-999):
        s = StubStrategy()
        s._entry_midpoint = entry_mid
        s._last_sell_tick = last_tick
        return s

    def test_no_position_returns_none(self):
        s = self._make_strategy()
        ctx = make_tick_ctx(position=Position(cash=100.0))
        assert s._check_exit(ctx, 0.40, 0.60, stop_loss_pct=0.20) is None

    def test_zero_entry_midpoint_returns_none(self):
        s = self._make_strategy(entry_mid=0.0)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0))
        assert s._check_exit(ctx, 0.40, 0.60, stop_loss_pct=0.20) is None

    def test_stop_loss_triggers_sell(self):
        s = self._make_strategy(entry_mid=0.50)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0), tick_index=100)
        # mid=0.35 is 30% below 0.50, stop_loss at 20%
        action = s._check_exit(ctx, 0.35, 0.65, stop_loss_pct=0.20)
        assert action.side == "sell_up"
        assert action.amount == 10.0
        assert s._entry_midpoint == 0.0

    def test_take_profit_triggers_sell(self):
        s = self._make_strategy(entry_mid=0.50)
        ctx = make_tick_ctx(position=Position(cash=0, down_tokens=20.0), tick_index=100)
        # down_mid=0.70 is 40% above 0.50, take_profit at 30%
        action = s._check_exit(ctx, 0.30, 0.70, stop_loss_pct=0.20, take_profit_pct=0.30)
        assert action.side == "sell_down"
        assert action.amount == 20.0
        assert s._entry_midpoint == 0.0

    def test_cooldown_blocks_sell(self):
        s = self._make_strategy(entry_mid=0.50, last_tick=90)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0), tick_index=100)
        # Stop loss triggered but within cooldown (100-90=10 < 36)
        action = s._check_exit(ctx, 0.35, 0.65, stop_loss_pct=0.20, cooldown=36)
        assert action is HOLD

    def test_no_exit_condition_returns_none(self):
        s = self._make_strategy(entry_mid=0.50)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0), tick_index=100)
        # mid=0.48 is only 4% below, stop_loss at 20% — not triggered
        result = s._check_exit(ctx, 0.48, 0.52, stop_loss_pct=0.20)
        assert result is None

    def test_stop_loss_none_skips(self):
        s = self._make_strategy(entry_mid=0.50)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0), tick_index=100)
        # mid dropped hard but stop_loss is None
        result = s._check_exit(ctx, 0.10, 0.90, stop_loss_pct=None)
        assert result is None

    def test_updates_last_sell_tick(self):
        s = self._make_strategy(entry_mid=0.50)
        ctx = make_tick_ctx(position=Position(cash=0, up_tokens=10.0), tick_index=200)
        s._check_exit(ctx, 0.35, 0.65, stop_loss_pct=0.20)
        assert s._last_sell_tick == 200


# ---------------------------------------------------------------------------
# _update_last_trade_tick
# ---------------------------------------------------------------------------


class TestUpdateLastTradeTick:
    def test_updates_sell_tick(self):
        s = StubStrategy()
        s._last_sell_tick = 0
        s._update_last_trade_tick(42)
        assert s._last_sell_tick == 42

    def test_updates_trade_tick(self):
        s = StubStrategy()
        s._last_trade_tick = 0
        s._update_last_trade_tick(42)
        assert s._last_trade_tick == 42

    def test_no_attr_is_noop(self):
        s = StubStrategy()
        s._update_last_trade_tick(42)  # should not raise


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestLog:
    def test_appends_message(self):
        s = StubStrategy()
        s.log("first")
        s.log("second")
        assert s._tick_logs == ["first", "second"]

    def test_creates_list_if_missing(self):
        s = StubStrategy()
        assert not hasattr(s, "_tick_logs")
        s.log("hello")
        assert hasattr(s, "_tick_logs")
        assert s._tick_logs == ["hello"]
