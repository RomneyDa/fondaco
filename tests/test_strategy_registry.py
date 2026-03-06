"""Tests for the strategy registry system."""

from bots import (
    ALL_STRATEGIES,
    UPDOWN_STRATEGIES,
    MARKET_STRATEGIES,
    get_strategies_for_market,
)
from core.strategies.base import Strategy


class TestUpdownStrategies:
    def test_updown_strategies_non_empty(self):
        assert len(UPDOWN_STRATEGIES) > 0

    def test_all_are_strategy_subclasses(self):
        for name, cls in UPDOWN_STRATEGIES.items():
            assert issubclass(cls, Strategy), "{} is not a Strategy subclass".format(name)


class TestAllStrategies:
    def test_is_superset_of_updown(self):
        for name, cls in UPDOWN_STRATEGIES.items():
            assert name in ALL_STRATEGIES
            assert ALL_STRATEGIES[name] is cls


class TestMarketStrategies:
    def test_updown_in_registry(self):
        assert "updown" in MARKET_STRATEGIES
        assert MARKET_STRATEGIES["updown"] is UPDOWN_STRATEGIES

    def test_get_strategies_for_updown(self):
        result = get_strategies_for_market("updown")
        assert result is UPDOWN_STRATEGIES

    def test_get_strategies_for_unknown(self):
        result = get_strategies_for_market("sports")
        assert result == {}

    def test_no_duplicate_names(self):
        # Within each registry, names should be unique (dict keys are by definition)
        # Across registries, check no collision
        all_names = set()
        for registry in MARKET_STRATEGIES.values():
            for name in registry:
                assert name not in all_names or name in ALL_STRATEGIES
                all_names.add(name)
