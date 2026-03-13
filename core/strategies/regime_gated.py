"""Base class for regime-gated strategies.

A regime-gated strategy wraps entry logic with a regime filter that
checks volatility, trend, and data quality conditions before allowing
any trades. If the current regime doesn't match the strategy's required
conditions, on_tick returns HOLD without evaluating entry logic.

Regime classification happens once per session in on_session_start()
and doesn't change mid-session. This prevents the strategy from
flip-flopping between active/inactive within a single 5-minute window.

IMPORTANT: Regime thresholds are NOT tunable params — they are fixed
per strategy class. If you optimize the gate itself, you're back to
overfitting. Only the entry/exit logic params are tunable.
"""

from typing import Any, Dict, List, Optional

from core.strategies.base import Strategy
from core.trader import HOLD, TradeAction, TickContext
from core.volatility import classify_regime


class RegimeGatedStrategy(Strategy):
    """Strategy that only activates when the market regime matches.

    Subclasses set class-level regime filters:
        required_vol: set of allowed vol regimes {'low', 'medium', 'high'}
                      or None to accept any
        required_trend: set of allowed trend regimes
                        {'trending', 'mean_reverting', 'flat'} or None
        skip_stale_data: if True, deactivate when data quality is 'stale'

    Then implement on_tick_active() instead of on_tick().
    """

    # --- Regime filter config (set by subclasses, NOT tunable) ---
    required_vol = None         # type: Optional[set]
    required_trend = None       # type: Optional[set]
    skip_stale_data = True      # type: bool
    regime_lookback = 20        # type: int
    regime_min_sessions = 5     # type: int
    min_density = None          # type: Optional[float]  # None = no density cutoff, float = minimum density

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._regime_active = False
        self._regime_info = {}  # type: Dict[str, Any]

    def on_session_start(self, metadata):
        # type: (Dict[str, Any]) -> None
        """Classify regime and decide whether to activate for this session."""
        # Compute market-aware gap threshold from window duration if available
        wd = metadata.get("_window_duration")
        max_gap_s = float(wd * 2) if wd else None
        self._regime_info = classify_regime(
            metadata, self.regime_lookback, self.regime_min_sessions,
            min_density=self.min_density,
            max_gap_s=max_gap_s,
        )

        # Check regime gates
        self._regime_active = True

        if self.skip_stale_data and self._regime_info.get("data_quality") == "stale":
            self._regime_active = False
            self.log("REGIME SKIP: stale data (gap={}, density={:.2f})".format(
                self._regime_info.get("gap_seconds"), self._regime_info.get("density", 0)))
            self._on_session_start_inner(metadata)
            return

        vol = self._regime_info.get("vol")
        if self.required_vol is not None and vol not in self.required_vol:
            self._regime_active = False
            self.log("REGIME SKIP: vol={} not in {}".format(vol, self.required_vol))
            self._on_session_start_inner(metadata)
            return

        trend = self._regime_info.get("trend")
        if self.required_trend is not None and trend not in self.required_trend:
            self._regime_active = False
            self.log("REGIME SKIP: trend={} not in {}".format(trend, self.required_trend))
            self._on_session_start_inner(metadata)
            return

        self.log("REGIME ACTIVE: vol={} trend={} quality={}".format(
            vol, trend, self._regime_info.get("data_quality")))
        self._on_session_start_inner(metadata)

    def on_tick(self, ctx):
        # type: (TickContext) -> TradeAction
        if not self._regime_active:
            return HOLD
        return self.on_tick_active(ctx)

    def _on_session_start_inner(self, metadata):
        # type: (Dict[str, Any]) -> None
        """Override to reset per-session state. Called after regime check."""
        pass

    def on_tick_active(self, ctx):
        # type: (TickContext) -> TradeAction
        """Core strategy logic — only called when regime matches."""
        raise NotImplementedError
